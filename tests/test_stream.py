import asyncio
import io
import os
import subprocess
import threading
import types

import pytest

from conftest import posix_only
from ttyplayer import player, stream


class FakePcm(stream.PcmSource):
    """An in-memory PcmSource: each read_ready() takes in the next of writes (b"": mpv wrote nothing),
    as does a read() that finds nothing taken in; after the last of them mpv quits.
    """

    def __init__(self, *writes):
        self.writes = list(writes)
        self.taken = bytearray()
        self.closed = False

    def read_ready(self):
        if self.writes:
            self.taken += self.writes.pop(0)
            return bool(self.taken)
        return True  # mpv quit: read() says so at once

    def read(self, size):
        if not self.taken and self.writes:  # a read waits for mpv's next write
            self.taken += self.writes.pop(0)
        chunk = bytes(self.taken[:size])
        del self.taken[:size]
        return chunk

    def close(self):
        self.closed = True


def page(granule, body=b"x", sequence=0):
    """One Ogg page with a one-segment body."""
    return stream.OGG_HEADER.pack(b"OggS", 0, 0, granule, 1, sequence, 0, 1) + bytes([len(body)]) + body


HEAD, TAGS = page(0, b"OpusHead"), page(0, b"OpusTags", 1)


class FakeFfmpeg:
    """Popen stand-in for ffmpeg, wired with real pipes: the test plays its output side."""

    def __init__(self, argv, stdin, stdout):
        assert (stdin, stdout) == (subprocess.PIPE, subprocess.PIPE)
        self.argv = argv
        pcm_read, pcm_write = os.pipe()
        out_read, out_write = os.pipe()
        self.stdin = open(pcm_write, "wb")
        self.received = bytearray()  # what ffmpeg was given, read as it comes like ffmpeg would
        self.drain = threading.Thread(target=self.take, args=(open(pcm_read, "rb"),), daemon=True)
        self.drain.start()
        self.stdout = open(out_read, "rb")
        self.output = open(out_write, "wb")  # what ffmpeg says
        self.log = []

    def say(self, *pages):
        self.output.write(b"".join(pages))
        self.output.flush()

    def take(self, pcm):
        with pcm:
            while chunk := pcm.read1():
                self.received += chunk

    def wait(self, timeout=None):
        self.log.append("wait")
        self.drain.join(timeout=2)  # stdin closed: ffmpeg reads to its end, then exits
        self.log.append(("pcm", len(self.received)))
        self.output.close()
        return 0

    def kill(self):
        self.log.append("kill")


@pytest.fixture
def ffmpeg(monkeypatch):
    made = []
    monkeypatch.setattr(stream.subprocess, "Popen", lambda argv, **kwargs: made.append(FakeFfmpeg(argv, **kwargs)) or made[-1])
    return made


def streamer_on(pcm, ffmpeg, **kwargs):
    """A Streamer reading pcm, its fake ffmpeg; no clock waits unless kwargs bring their own."""
    kwargs.setdefault("sleep", lambda seconds: None)
    streamer = stream.Streamer(pcm, "/opt/bin/ffmpeg", **kwargs)
    return streamer, ffmpeg[-1]


def collect(loop, listener, until):
    """listener's pages until it has until of them or its stream ends, on loop."""

    async def take():
        pages = []
        while len(pages) < until and (got := await asyncio.wait_for(listener.next(), 2)) is not None:
            pages.append(got)
        return pages

    return loop.run_until_complete(take())


@pytest.fixture
def loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


# --- argument lines -----------------------------------------------------------


def test_ffmpeg_reads_mpvs_pcm_and_writes_ogg_opus():
    argv = stream.ffmpeg_argv("/opt/bin/ffmpeg")
    assert argv[0] == "/opt/bin/ffmpeg"
    line = " ".join(argv)
    assert "-f s16le -ar 48000 -ac 2 -i -" in line
    assert "-c:a libopus -b:a 128k" in line
    assert line.endswith("-f ogg -")


def test_mpv_with_a_pcm_target_writes_that_format_there():
    argv = player.build_argv(video=False, socket_path="/tmp/x.sock", pcm_target=r"\\.\pipe\x")
    assert "--ao=pcm" in argv
    assert r"--ao-pcm-file=\\.\pipe\x" in argv
    assert [arg for arg in argv if arg.startswith("--ao-pcm-file")] == [r"--ao-pcm-file=\\.\pipe\x"]
    assert "--ao-pcm-waveheader=no" in argv
    assert "--audio-format=s16" in argv
    assert f"--audio-samplerate={stream.SAMPLE_RATE}" in argv
    assert "--no-video" in argv


def test_mpv_without_headless_pcm_plays_on_the_speakers():
    assert not [arg for arg in player.build_argv(False, "/tmp/x.sock") if arg.startswith("--ao")]


def test_mpv_client_starts_mpv_as_its_pcm_source_needs(monkeypatch, fake_winapi):
    seen = []

    class Started(Exception):
        pass

    def popen(argv, stdout=None):
        seen.append((next((arg for arg in argv if arg.startswith("--ao-pcm-file")), None), stdout))
        raise Started

    monkeypatch.setattr(player.subprocess, "Popen", popen)
    named = stream.NamedPipe()
    for pcm in (None, stream.StdoutPipe(), named):
        with pytest.raises(Started):
            player.MpvClient(pcm=pcm)
    assert seen == [
        (None, None), ("--ao-pcm-file=/dev/stdout", subprocess.PIPE), (f"--ao-pcm-file={named.name}", None)
    ]  # stdout is a pipe only for StdoutPipe


def test_mpv_client_hands_the_started_mpv_to_its_pcm_source(monkeypatch):
    class Started(Exception):
        pass

    process = types.SimpleNamespace(stdout="mpv-stdout", kill=lambda: None)
    monkeypatch.setattr(player.subprocess, "Popen", lambda argv, stdout=None: process)
    monkeypatch.setattr(player, "connect", lambda path: None)  # then stop: mpv "did not start"
    pcm = stream.StdoutPipe()
    with pytest.raises(RuntimeError):
        player.MpvClient(pcm=pcm)
    assert pcm.pipe == "mpv-stdout"


# --- the PCM sources ------------------------------------------------------------


@pytest.mark.parametrize("windows, source", [(False, stream.StdoutPipe), (True, stream.NamedPipe)])
def test_pcm_source_is_a_named_pipe_on_windows_only(monkeypatch, fake_winapi, windows, source):
    monkeypatch.setattr(stream, "WINDOWS", windows)
    assert type(stream.pcm_source()) is source


@posix_only
def test_stdout_pipe_finds_mpvs_stdout_ready_only_when_it_has_pcm():
    source = stream.StdoutPipe()
    assert source.mpv_target() == "/dev/stdout" and source.popen_stdout == subprocess.PIPE
    pcm_read, pcm_write = os.pipe()
    source.attach(types.SimpleNamespace(process=types.SimpleNamespace(stdout=open(pcm_read, "rb"))))
    with open(pcm_write, "wb") as mpv:
        assert not source.read_ready()
        mpv.write(b"\x01\x02\x03\x04")
        mpv.flush()
        assert source.read_ready()
        assert source.read(100) == b"\x01\x02\x03\x04"
    assert source.read_ready() and source.read(100) == b""  # mpv quit
    source.close()
    assert source.pipe.closed


def test_named_pipe_is_inbound_one_instance_first_and_random(fake_winapi):
    winapi = fake_winapi
    first, second = stream.NamedPipe(), stream.NamedPipe()
    assert first.mpv_target() == first.name and first.popen_stdout is None
    assert first.name.startswith(f"\\\\.\\pipe\\ttyplayer-pcm-{os.getpid()}-") and first.name != second.name
    name, open_mode, pipe_mode, max_instances, out_size, in_size, timeout, security = winapi.made
    assert open_mode == winapi.PIPE_ACCESS_INBOUND | winapi.FILE_FLAG_FIRST_PIPE_INSTANCE | winapi.FILE_FLAG_OVERLAPPED
    assert (pipe_mode, max_instances, in_size) == (winapi.PIPE_WAIT, 1, stream.PIPE_BUFFER)


def test_named_pipe_is_silent_until_mpv_opens_it_then_reads_what_it_writes(fake_winapi):
    source = stream.NamedPipe()
    assert fake_winapi.log == [("create", "pipe1"), ("connect", "pipe1")]
    assert not source.read_ready()  # nothing plays yet: mpv has not opened it, and nothing waited for it
    fake_winapi.connected = True
    assert not source.read_ready()  # opened, nothing written
    fake_winapi.written += b"\x01\x02\x03\x04\x05\x06"
    assert source.read_ready()
    assert source.read(4) == b"\x01\x02\x03\x04"
    assert source.read(4) == b"\x05\x06"
    assert not source.read_ready()


def test_named_pipe_starts_over_when_mpv_closes_it(fake_winapi):
    source = stream.NamedPipe()
    fake_winapi.connected = True
    fake_winapi.written += b"\x01\x02\x03\x04"
    fake_winapi.gone = True  # playback stopped: mpv closed the file after writing
    assert source.read_ready() and source.read(100) == b"\x01\x02\x03\x04"  # what it wrote still comes
    assert not source.read_ready()
    assert fake_winapi.log[2:] == [("close", "pipe1"), ("create", "pipe2"), ("connect", "pipe2")]
    assert not source.read_ready()  # waiting for mpv to open it again
    fake_winapi.connected = True
    fake_winapi.written += b"\x05\x06\x07\x08"
    assert source.read_ready() and source.read(100) == b"\x05\x06\x07\x08"


def test_named_pipe_read_cut_short_by_mpv_closing_it_ends_the_chunk_and_starts_over(fake_winapi):
    source = stream.NamedPipe()
    fake_winapi.connected = True
    fake_winapi.written += b"\x01\x02"  # half a frame
    assert source.read_ready() and source.read(100) == b"\x01\x02"
    fake_winapi.gone = True
    assert source.read(2) == b""  # the pacer's wait for the frame's other half
    assert fake_winapi.log[2:] == [("close", "pipe1"), ("create", "pipe2"), ("connect", "pipe2")]


def test_named_pipe_close_stops_waiting_for_mpv_then_closes_the_pipe(fake_winapi):
    source = stream.NamedPipe()
    source.close()
    source.close()  # a second close (Streamer.stop after a failed reopen) does nothing
    assert fake_winapi.log[2:] == [("cancel", "pipe1"), ("cancelled", "pipe1"), ("close", "pipe1")]


def test_fake_winapi_refuses_to_close_a_pipe_whose_cancelled_connect_was_not_waited_for(fake_winapi):
    source = stream.NamedPipe()
    source.connecting.cancel()
    with pytest.raises(AssertionError, match="cancelled connect"):
        fake_winapi.CloseHandle(source.handle)
    source.close()  # waits for the cancellation, then the close goes through
    assert fake_winapi.log[-2:] == [("cancelled", "pipe1"), ("close", "pipe1")]


def test_named_pipe_close_after_mpv_opened_it_closes_at_once(fake_winapi):
    source = stream.NamedPipe()
    fake_winapi.connected = True
    assert not source.read_ready()
    source.close()
    assert fake_winapi.log[2:] == [("close", "pipe1")]


def unopened_pipe(sounding):
    """A NamedPipe mpv never opens, its clock at 0 and sounding() read from sounding[0]."""
    source = stream.NamedPipe()
    source.now = 0
    source.clock = lambda: source.now
    source.attach(types.SimpleNamespace(sounding=lambda: sounding[0]))
    return source


def test_named_pipe_waits_for_mpv_as_long_as_nothing_sounds(fake_winapi):
    sounding = [False]
    source = unopened_pipe(sounding)
    source.now = 3600  # an hour idle, loading a track, or paused
    assert not source.read_ready()
    sounding[0] = True
    assert not source.read_ready()  # the track's wait starts now
    source.now += stream.CONNECT_TIMEOUT
    assert not source.read_ready()
    sounding[0] = False  # paused just in time: the next track's wait starts over
    assert not source.read_ready()
    sounding[0] = True
    source.now += stream.CONNECT_TIMEOUT * 2
    assert not source.read_ready()
    source.now += stream.CONNECT_TIMEOUT
    fake_winapi.connected = True  # mpv opened it in time
    assert not source.read_ready()
    assert source.unopened_since is None


def test_named_pipe_times_out_when_a_track_sounds_and_mpv_never_opens_it(fake_winapi):
    sounding = [True]
    source = unopened_pipe(sounding)
    assert not source.read_ready()
    source.now = stream.CONNECT_TIMEOUT + 0.1
    with pytest.raises(TimeoutError, match=rf"mpv did not open the pipe .*ttyplayer-pcm-.* within {stream.CONNECT_TIMEOUT} s"):
        source.read_ready()


def test_a_pipe_timeout_ends_the_stream_with_one_line(ffmpeg, fake_winapi, capsys):
    sounding = [True]
    source = unopened_pipe(sounding)
    source.read_ready()  # the wait starts
    source.now = stream.CONNECT_TIMEOUT + 1
    streamer, process = streamer_on(source, ffmpeg)
    streamer.pacer.join(timeout=2)
    assert not streamer.pacer.is_alive() and process.stdin.closed  # ffmpeg's input ends: so does the stream
    streamer.stop()
    lines = capsys.readouterr().err.splitlines()
    assert len(lines) == 1 and lines[0].startswith("Streaming stopped: mpv did not open the pipe")
    assert fake_winapi.log[-3:] == [("cancel", "pipe1"), ("cancelled", "pipe1"), ("close", "pipe1")]


def test_named_pipe_whose_name_is_taken_fails_and_never_falls_back(fake_winapi):
    fake_winapi.taken = True
    with pytest.raises(PermissionError):
        stream.NamedPipe()
    assert fake_winapi.log == []


def test_a_taken_name_on_reopen_ends_the_pacer_and_stop_still_works(ffmpeg, fake_winapi):
    source = stream.NamedPipe()
    fake_winapi.connected = fake_winapi.gone = True
    fake_winapi.taken = True  # another process grabbed the name while mpv had it closed
    streamer, process = streamer_on(source, ffmpeg)
    streamer.pacer.join(timeout=2)
    assert not streamer.pacer.is_alive()
    streamer.stop()
    assert fake_winapi.log[-1] == ("close", "pipe1")


# --- pipe wiring and shutdown -------------------------------------------------------


def test_mpvs_pcm_reaches_ffmpeg_and_stop_waits_for_it(ffmpeg):
    pcm = bytes(range(256)) * 100
    source = FakePcm(pcm)
    streamer, process = streamer_on(source, ffmpeg)
    assert process.argv == stream.ffmpeg_argv("/opt/bin/ffmpeg")
    streamer.pacer.join(timeout=2)  # mpv's PCM ended: the pacer closed ffmpeg's stdin
    streamer.stop()
    assert process.log == ["wait", ("pcm", len(pcm))]
    assert not streamer.pacer.is_alive() and not streamer.reader.is_alive()
    assert source.closed


def test_stop_kills_an_ffmpeg_that_does_not_exit(ffmpeg):
    streamer, process = streamer_on(FakePcm(), ffmpeg)

    def hangs(timeout=None):
        process.log.append("wait")
        if timeout is not None:
            raise subprocess.TimeoutExpired("ffmpeg", timeout)
        process.output.close()

    process.wait = hangs
    streamer.stop()
    assert process.log == ["wait", "kill", "wait"]


def test_the_pacer_holds_pcm_to_the_speed_it_plays(ffmpeg):
    now, slept = [0.0], []

    def sleep(seconds):
        slept.append(round(seconds, 3))
        now[0] += seconds

    second = stream.BYTES_PER_SECOND
    streamer, process = streamer_on(FakePcm(bytes(second)), ffmpeg, clock=lambda: now[0], sleep=sleep)
    streamer.pacer.join(timeout=2)
    streamer.stop()
    assert len(slept) == second // stream.PCM_CHUNK  # each chunk after the first, and the end, waits its 20 ms
    assert set(slept) == {0.02}


def test_falling_behind_restarts_the_pacers_clock_instead_of_bursting(ffmpeg):
    now, slept = [0.0], []
    pcm = FakePcm(bytes(stream.PCM_CHUNK * 3))

    def sleep(seconds):
        slept.append(seconds)
        now[0] += seconds + (60 if len(slept) == 1 else 0)  # the machine stalls a minute in the first wait

    streamer, process = streamer_on(pcm, ffmpeg, clock=lambda: now[0], sleep=sleep)
    streamer.pacer.join(timeout=2)
    streamer.stop()
    assert slept == [pytest.approx(0.02)] * 2  # without the restart the third chunk would not wait


def test_while_mpv_has_no_pcm_ffmpeg_gets_silence_then_mpvs_pcm(ffmpeg):
    sound = b"\x01\x02\x03\x04" * 10 + b"\x05\x06"  # half a frame at the end
    pcm = FakePcm(b"", b"", b"", sound, b"\x07\x08")  # paused for three chunks; then the frame's other half
    streamer, process = streamer_on(pcm, ffmpeg)
    streamer.pacer.join(timeout=2)
    streamer.stop()
    assert bytes(process.received) == stream.SILENCE * 3 + sound + b"\x07\x08"  # no silence inside a frame


# --- fan-out --------------------------------------------------------------------


def test_every_listener_gets_every_page(ffmpeg, loop):
    streamer, process = streamer_on(FakePcm(), ffmpeg)
    first, second = streamer.listen(loop), streamer.listen(loop)
    audio = [page(960 * n, sequence=n + 1) for n in range(1, 4)]
    process.say(HEAD, TAGS, *audio)
    assert collect(loop, first, 5) == [HEAD, TAGS, *audio]
    assert collect(loop, second, 5) == [HEAD, TAGS, *audio]
    streamer.stop()
    assert collect(loop, first, 1) == []  # ffmpeg's end ends every listener


def test_a_late_listener_gets_the_header_pages_first(ffmpeg, loop):
    streamer, process = streamer_on(FakePcm(), ffmpeg)
    early = streamer.listen(loop)
    process.say(HEAD, TAGS, page(960), page(1920))
    collect(loop, early, 4)
    late = streamer.listen(loop)
    process.say(page(2880))
    assert collect(loop, late, 3) == [HEAD, TAGS, page(2880)]
    streamer.stop()


def test_a_slow_listener_loses_pages_and_the_other_does_not(ffmpeg, loop):
    streamer, process = streamer_on(FakePcm(), ffmpeg)
    slow, fast = streamer.listen(loop), streamer.listen(loop)
    audio = [page(960 * n) for n in range(1, stream.BACKLOG + 20)]
    received = []
    for audio_page in audio:  # the fast one takes each page as it comes; the slow one takes none
        process.say(audio_page)
        received += collect(loop, fast, 1)
    assert received == audio
    assert collect(loop, slow, stream.BACKLOG) == audio[: stream.BACKLOG]
    assert slow.dropped == len(audio) - stream.BACKLOG
    streamer.stop()


def test_end_listeners_ends_them_even_when_full(ffmpeg, loop):
    streamer, process = streamer_on(FakePcm(), ffmpeg)
    listener = streamer.listen(loop)
    audio = [page(960 * n) for n in range(1, stream.BACKLOG + 5)]
    process.say(*audio)
    threading.Event().wait(0.2)  # the reader offers them all
    streamer.end_listeners()
    assert collect(loop, listener, stream.BACKLOG) == audio[1 : stream.BACKLOG]  # the oldest made room for the end
    streamer.stop()


def test_a_listener_after_the_end_ends_at_once(ffmpeg, loop):
    streamer, process = streamer_on(FakePcm(), ffmpeg)
    process.say(HEAD, TAGS)
    streamer.stop()
    assert collect(loop, streamer.listen(loop), 5) == [HEAD, TAGS]


def test_a_listener_whose_loop_closed_does_not_stop_the_reader(ffmpeg):
    streamer, process = streamer_on(FakePcm(), ffmpeg)
    gone = asyncio.new_event_loop()
    streamer.listen(gone)
    gone.close()
    loop = asyncio.new_event_loop()
    alive = streamer.listen(loop)
    process.say(HEAD)
    assert collect(loop, alive, 1) == [HEAD]
    streamer.stop()
    loop.close()


def test_read_page_rejects_what_is_not_ogg():
    with pytest.raises(ValueError):
        stream.read_page(io.BytesIO(b"RIFF" + bytes(40)))
    assert stream.read_page(io.BytesIO(HEAD[:-1])) is None
    assert stream.read_page(io.BytesIO(HEAD)) == HEAD
