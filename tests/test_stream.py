import asyncio
import io
import os
import subprocess
import tempfile
import threading
import time

import pytest

from conftest import posix_only
from ttyplayer import player, stream


def pcm_file(data=b""):
    """A file mpv's stdout could be: select always finds it ready, so no silence is slipped in."""
    file = tempfile.TemporaryFile()
    file.write(data)
    file.seek(0)
    return file


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


def test_mpv_headless_pcm_writes_that_format_to_stdout():
    argv = player.build_argv(video=False, socket_path="/tmp/x.sock", headless_pcm=True)
    assert "--ao=pcm" in argv
    assert "--ao-pcm-file=/dev/stdout" in argv
    assert "--ao-pcm-waveheader=no" in argv
    assert "--audio-format=s16" in argv
    assert f"--audio-samplerate={stream.SAMPLE_RATE}" in argv
    assert "--no-video" in argv


def test_mpv_without_headless_pcm_plays_on_the_speakers():
    assert not [arg for arg in player.build_argv(False, "/tmp/x.sock") if arg.startswith("--ao")]


def test_mpv_client_pipes_stdout_only_when_headless(monkeypatch):
    seen = []

    class Started(Exception):
        pass

    def popen(argv, stdout=None):
        seen.append(stdout)
        raise Started

    monkeypatch.setattr(player.subprocess, "Popen", popen)
    for headless in (False, True):
        with pytest.raises(Started):
            player.MpvClient(headless_pcm=headless)
    assert seen == [None, subprocess.PIPE]


# --- pipe wiring and shutdown -------------------------------------------------------


@posix_only
def test_mpvs_pcm_reaches_ffmpeg_and_stop_waits_for_it(ffmpeg):
    pcm = bytes(range(256)) * 100
    streamer, process = streamer_on(pcm_file(pcm), ffmpeg)
    assert process.argv == stream.ffmpeg_argv("/opt/bin/ffmpeg")
    streamer.pacer.join(timeout=2)  # mpv's stdout ended: the pacer closed ffmpeg's stdin
    streamer.stop()
    assert process.log == ["wait", ("pcm", len(pcm))]
    assert not streamer.pacer.is_alive() and not streamer.reader.is_alive()


def test_stop_kills_an_ffmpeg_that_does_not_exit(ffmpeg):
    streamer, process = streamer_on(pcm_file(), ffmpeg)

    def hangs(timeout=None):
        process.log.append("wait")
        if timeout is not None:
            raise subprocess.TimeoutExpired("ffmpeg", timeout)
        process.output.close()

    process.wait = hangs
    streamer.stop()
    assert process.log == ["wait", "kill", "wait"]


@posix_only
def test_the_pacer_holds_pcm_to_the_speed_it_plays(ffmpeg):
    now, slept = [0.0], []

    def sleep(seconds):
        slept.append(round(seconds, 3))
        now[0] += seconds

    second = stream.BYTES_PER_SECOND
    streamer, process = streamer_on(pcm_file(bytes(second)), ffmpeg, clock=lambda: now[0], sleep=sleep)
    streamer.pacer.join(timeout=2)
    streamer.stop()
    assert len(slept) == second // stream.PCM_CHUNK  # each chunk after the first, and the end, waits its 20 ms
    assert set(slept) == {0.02}


@posix_only
def test_falling_behind_restarts_the_pacers_clock_instead_of_bursting(ffmpeg):
    now, slept = [0.0], []
    pcm = pcm_file(bytes(stream.PCM_CHUNK * 3))

    def sleep(seconds):
        slept.append(seconds)
        now[0] += seconds + (60 if len(slept) == 1 else 0)  # the machine stalls a minute in the first wait

    streamer, process = streamer_on(pcm, ffmpeg, clock=lambda: now[0], sleep=sleep)
    streamer.pacer.join(timeout=2)
    streamer.stop()
    assert slept == [pytest.approx(0.02)] * 2  # without the restart the third chunk would not wait


@posix_only
def test_while_mpv_has_no_pcm_ffmpeg_gets_silence_then_mpvs_pcm(ffmpeg):
    pcm_read, pcm_write = os.pipe()
    with open(pcm_read, "rb") as pcm, open(pcm_write, "wb") as mpv:
        streamer, process = streamer_on(pcm, ffmpeg, sleep=time.sleep)
        time.sleep(0.2)  # paused, or between tracks
        mpv.write(b"\x01\x02\x03\x04" * 1000 + b"\x05\x06")  # half a frame at the end
        mpv.flush()
        time.sleep(0.05)
        mpv.write(b"\x07\x08")  # the frame's other half
        mpv.flush()
        time.sleep(0.1)
    streamer.pacer.join(timeout=2)
    streamer.stop()
    received = bytes(process.received)
    assert received.startswith(bytes(stream.PCM_CHUNK * 5))  # 0.2 s of silence: at least 0.1 s of it
    sound = received.lstrip(b"\x00")
    assert sound.startswith(b"\x01\x02\x03\x04" * 1000 + b"\x05\x06\x07\x08")  # no silence inside a frame
    assert len(received) % stream.FRAME == 0


# --- fan-out --------------------------------------------------------------------


def test_every_listener_gets_every_page(ffmpeg, loop):
    streamer, process = streamer_on(pcm_file(), ffmpeg)
    first, second = streamer.listen(loop), streamer.listen(loop)
    audio = [page(960 * n, sequence=n + 1) for n in range(1, 4)]
    process.say(HEAD, TAGS, *audio)
    assert collect(loop, first, 5) == [HEAD, TAGS, *audio]
    assert collect(loop, second, 5) == [HEAD, TAGS, *audio]
    streamer.stop()
    assert collect(loop, first, 1) == []  # ffmpeg's end ends every listener


def test_a_late_listener_gets_the_header_pages_first(ffmpeg, loop):
    streamer, process = streamer_on(pcm_file(), ffmpeg)
    early = streamer.listen(loop)
    process.say(HEAD, TAGS, page(960), page(1920))
    collect(loop, early, 4)
    late = streamer.listen(loop)
    process.say(page(2880))
    assert collect(loop, late, 3) == [HEAD, TAGS, page(2880)]
    streamer.stop()


def test_a_slow_listener_loses_pages_and_the_other_does_not(ffmpeg, loop):
    streamer, process = streamer_on(pcm_file(), ffmpeg)
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
    streamer, process = streamer_on(pcm_file(), ffmpeg)
    listener = streamer.listen(loop)
    audio = [page(960 * n) for n in range(1, stream.BACKLOG + 5)]
    process.say(*audio)
    threading.Event().wait(0.2)  # the reader offers them all
    streamer.end_listeners()
    assert collect(loop, listener, stream.BACKLOG) == audio[1 : stream.BACKLOG]  # the oldest made room for the end
    streamer.stop()


def test_a_listener_after_the_end_ends_at_once(ffmpeg, loop):
    streamer, process = streamer_on(pcm_file(), ffmpeg)
    process.say(HEAD, TAGS)
    streamer.stop()
    assert collect(loop, streamer.listen(loop), 5) == [HEAD, TAGS]


def test_a_listener_whose_loop_closed_does_not_stop_the_reader(ffmpeg):
    streamer, process = streamer_on(pcm_file(), ffmpeg)
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
