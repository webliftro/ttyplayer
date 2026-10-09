import concurrent.futures
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time

import pytest

from ttyplayer import control, player, utils
from ttyplayer.models import Video

# Unix sockets and termios: the Windows paths are tested on every OS with WINDOWS patched.
posix_only = pytest.mark.skipif(utils.WINDOWS, reason="POSIX socket or terminal")
REAL_INTERRUPT_MAIN = player.interrupt_main  # make_remote_client replaces it


@pytest.fixture(autouse=True)
def not_macos(monkeypatch):
    """Off macOS by default, so no test runs the real osascript on the developer's Mac."""
    monkeypatch.setattr(player.sys, "platform", "linux")


def test_format_time_zero():
    assert player.format_time(0) == "0:00"


def test_format_time_minutes_and_seconds():
    assert player.format_time(65) == "1:05"
    assert player.format_time(328) == "5:28"


def test_format_time_none():
    assert player.format_time(None) == "--:--"


def test_build_argv_audio_only():
    argv = player.build_argv(video=False, socket_path="/tmp/x.sock")
    assert argv[0] == "mpv"
    assert "--no-video" in argv
    assert "--input-ipc-server=/tmp/x.sock" in argv
    assert "--idle" in argv
    assert "--no-terminal" in argv


def test_build_argv_audio_only_asks_for_an_audio_stream():
    argv = player.build_argv(video=False, socket_path="/tmp/x.sock")
    assert "--ytdl-format=bestaudio/best" in argv


def test_build_argv_with_video():
    argv = player.build_argv(video=True, socket_path="/tmp/x.sock")
    assert "--no-video" not in argv
    assert not any(a.startswith("--ytdl-format") for a in argv)


LEVELS_ARG = "--af=@levels:lavfi=[astats=metadata=1:reset=1:measure_overall=none:measure_perchannel=Peak_level]"


def test_build_argv_adds_the_level_filter_unless_levels_is_off():
    with_levels = player.build_argv(video=False, socket_path="/tmp/x.sock")
    without = player.build_argv(video=False, socket_path="/tmp/x.sock", levels=False)
    assert LEVELS_ARG in with_levels
    assert [arg for arg in with_levels if arg != LEVELS_ARG] == without
    assert not [arg for arg in without if arg.startswith("--af")]


def test_build_argv_headless_pcm_has_no_level_filter():
    assert player.build_argv(False, "/tmp/x.sock", headless_pcm=True) == player.build_argv(
        False, "/tmp/x.sock", headless_pcm=True, levels=False
    )


@pytest.mark.parametrize(
    "metadata, levels",
    [
        ({"lavfi.astats.1.Peak_level": "-12.500000", "lavfi.astats.2.Peak_level": "-3.25"}, [-12.5, -3.25]),
        ({"lavfi.astats.1.Peak_level": "-6.0"}, [-6.0, -6.0]),  # mono
        ({"lavfi.astats.1.Peak_level": "-inf", "lavfi.astats.2.Peak_level": "-inf"}, [-90.0, -90.0]),
        ({"lavfi.astats.1.Peak_level": "-120.0", "lavfi.astats.2.Peak_level": "0.000000"}, [-90.0, 0.0]),
        ({}, None),  # the filter has not run yet
        (None, None),  # the property is unavailable: the reply's error
        ({"lavfi.astats.1.Peak_level": "loud"}, None),
        ({"lavfi.astats.1.Peak_level": "nan"}, None),
        ({"lavfi.astats.1.Peak_level": "-1.0", "lavfi.astats.2.Peak_level": None}, None),
        ("not a dict", None),
    ],
)
def test_parse_levels_reads_each_channels_peak(metadata, levels):
    assert player.parse_levels(metadata) == levels


@pytest.mark.parametrize(
    "level, bar",
    [
        (None, "L ▯▯▯▯▯▯▯▯▯▯"),
        (-90.0, "L ▯▯▯▯▯▯▯▯▯▯"),
        (-60.0, "L ▯▯▯▯▯▯▯▯▯▯"),
        (-30.0, "L ▮▮▮▮▮▯▯▯▯▯"),
        (-6.0, "L ▮▮▮▮▮▮▮▮▮▯"),
        (0.0, "L ▮▮▮▮▮▮▮▮▮▮"),
        (3.0, "L ▮▮▮▮▮▮▮▮▮▮"),  # a clipped peak stays full
    ],
)
def test_level_meter_fills_from_minus_sixty_to_zero_dbfs(level, bar):
    assert player.level_meter("L", level, width=12) == bar


def test_level_meter_narrower_than_its_label_has_no_cells():
    assert player.level_meter("L", 0.0, width=1) == "L "


def make_client(videos):
    """A MpvClient with no mpv behind it: queue only, load() just records urls."""
    client = player.MpvClient.__new__(player.MpvClient)
    client.queue = []
    client.index = 0
    client.state = {}
    client.on_play = None
    client.on_state = None
    client.queue_lock = threading.RLock()
    client.fade_lock = threading.RLock()
    client.send_lock = threading.Lock()
    client.reply_lock = threading.Lock()
    client.replies = {}
    client.loaded = []
    client.load = client.loaded.append
    for v in videos:
        client.add(v)
    return client


def test_play_current_tells_the_on_play_hook():
    client = make_client([A, B])
    played = []
    client.on_play = played.append
    client.play_current()
    client.next()
    assert played == [A, B]


A = Video(id="a", title="First", uploader="u", duration=1)
B = Video(id="b", title="Second", uploader="u", duration=2)
C = Video(id="c", title="Third", uploader="u", duration=3)


def test_play_current_loads_the_current_url():
    client = make_client([A, B])
    client.play_current()
    assert client.loaded == [A.url]


def test_next_advances_and_loads():
    client = make_client([A, B])
    client.next()
    assert client.index == 1
    assert client.loaded == [B.url]


def test_next_at_end_of_queue_does_nothing():
    client = make_client([A])
    client.next()
    assert client.index == 0
    assert client.loaded == []


def test_prev_at_start_does_nothing():
    client = make_client([A, B])
    client.prev()
    assert client.index == 0
    assert client.loaded == []


def test_up_next_title():
    client = make_client([A, B])
    assert client.up_next() == "Second"
    client.index = 1
    assert client.up_next() is None


def test_current_title_prefers_the_queue_over_mpv():
    client = make_client([A])
    client.state["media-title"] = "watch?v=a"
    assert client.current_title() == "First"


def test_current_title_falls_back_to_mpv_when_queue_is_empty():
    client = make_client([])
    client.idle = False
    client.state["media-title"] = "Something"
    assert client.current_title() == "Something"


def make_remote_client(videos, monkeypatch):
    """make_client plus a recording send() and interrupt_main()."""
    client = make_client(videos)
    client.sent = []
    client.send = client.sent.append
    client.interrupted = []
    monkeypatch.setattr(player, "interrupt_main", lambda: client.interrupted.append(True))
    return client


def test_handle_control_pause_cycles_pause_like_the_space_key(monkeypatch):
    client = make_remote_client([A], monkeypatch)
    assert client.handle_control("pause") == {"ok": True}
    assert client.sent == [["cycle", "pause"]]


def test_handle_control_next_and_prev_move_through_the_queue(monkeypatch):
    client = make_remote_client([A, B], monkeypatch)
    assert client.handle_control("next") == {"ok": True}
    assert client.index == 1
    assert client.handle_control("prev") == {"ok": True}
    assert client.index == 0
    assert client.loaded == [B.url, A.url]


def test_handle_control_status_reports_what_render_shows(monkeypatch):
    client = make_remote_client([A, B], monkeypatch)
    client.index = 1
    client.state.update({"time-pos": 83.5, "duration": 296.0, "pause": True})
    assert client.handle_control("status") == {
        "ok": True,
        "title": "Second",
        "uploader": "u",
        "position": 83.5,
        "duration": 296.0,
        "paused": True,
        "index": 2,
        "total": 2,
        "volume": None,
        "volume_source": None,
        "muted": False,
        "up_next": None,
        "started_in": None,
        "idle": True,
        "error": None,
        "stream": False,
        "levels": None,
        "sleep": None,
    }


def test_handle_control_status_before_mpv_reports_anything(monkeypatch):
    client = make_remote_client([A], monkeypatch)
    reply = client.handle_control("status")
    assert (reply["position"], reply["duration"], reply["paused"]) == (None, None, False)


def test_handle_control_stop_interrupts_the_main_thread(monkeypatch):
    client = make_remote_client([A], monkeypatch)
    assert client.handle_control("stop") == {"ok": True}
    assert client.interrupted == [True]
    assert client.sent == []


def test_handle_control_unknown_command(monkeypatch):
    client = make_remote_client([A], monkeypatch)
    assert client.handle_control("dance") == {"ok": False, "error": "unknown command dance"}


TIMED = {
    "title": "Song",
    "position": 83,
    "duration": 296,
    "paused": False,
    "index": 1,
    "total": 1,
    "volume": 70,
    "muted": False,
}


def test_status_line_leaves_the_error_out():
    # ttyplayer status prints this line: a failed track does not change it.
    assert player.status_line({**TIMED, "error": "Could not play Song: unavailable"}) == player.status_line(TIMED)


def test_status_line_matches_the_player_shape():
    status = {**TIMED, "paused": True}
    assert player.status_line(status) == "1:23 / 4:56  Paused  🔊 ▮▮▮▮▮▮▮▯▯▯ 70%  Song"
    status.update(paused=False, total=3)
    assert player.status_line(status) == "1:23 / 4:56  Playing  🔊 ▮▮▮▮▮▮▮▯▯▯ 70%  Song  [1/3]"


@pytest.mark.parametrize(
    "volume, muted, meter",
    [(30, False, "🔊 ▮▮▮▯▯▯▯▯▯▯ 30%"), (75, True, "🔇 ▮▮▮▮▮▮▮▮▯▯ 75%"), (None, False, "🔊 ▯▯▯▯▯▯▯▯▯▯ --")],
)
def test_status_line_shows_the_volume_meter(volume, muted, meter):
    status = {**TIMED, "volume": volume, "muted": muted}
    assert player.status_line(status) == f"1:23 / 4:56  Playing  {meter}  Song"


def test_status_line_follows_the_device_volume_over_the_player_volume(monkeypatch):
    client = make_remote_client([A], monkeypatch)
    client.handle_message({"event": "property-change", "name": "volume", "data": 100.0})
    assert "🔊 ▮▮▮▮▮▮▮▮▮▮ 100%" in player.status_line(client.status())
    client.handle_message({"event": "property-change", "name": "ao-volume", "data": 40.0})
    assert "🔊 ▮▮▮▮▯▯▯▯▯▯ 40%" in player.status_line(client.status())
    client.handle_message({"event": "property-change", "name": "ao-volume", "data": None})
    assert "🔊 ▮▮▮▮▮▮▮▮▮▮ 100%" in player.status_line(client.status())


@pytest.mark.parametrize(
    "volume, meter",
    [(0, "🔊 ▯▯▯▯▯▯▯▯▯▯ 0%"), (100, "🔊 ▮▮▮▮▮▮▮▮▮▮ 100%"), (130, "🔊 ▮▮▮▮▮▮▮▮▮▮ 130%"), (None, "🔊 ▯▯▯▯▯▯▯▯▯▯ --")],
)
def test_volume_meter(volume, meter):
    assert player.volume_meter(volume) == meter


@pytest.mark.parametrize("volume, meter", [(60, "🔇 ▮▮▮▮▮▮▯▯▯▯ 60%"), (None, "🔇 ▯▯▯▯▯▯▯▯▯▯ --")])
def test_volume_meter_while_muted(volume, meter):
    assert player.volume_meter(volume, muted=True) == meter


class FakeStdin:
    """Plays the given keys (or raises them, if an exception) one read at a time."""

    def __init__(self, keys, on_read=None):
        self.keys = list(keys)
        self.on_read = on_read

    def fileno(self):
        return 0

    def read(self, count):
        if self.on_read:
            self.on_read()
        key = self.keys.pop(0)
        if isinstance(key, BaseException):
            raise key
        return key


def run_with_keys(client, keys, monkeypatch, path, on_read=None):
    """Run client.run() with no TTY, the control socket at path, and quit() recorded."""
    monkeypatch.setattr(player.control, "control_path", lambda: path)
    monkeypatch.setattr(player.termios, "tcgetattr", lambda fd: [])
    monkeypatch.setattr(player.termios, "tcsetattr", lambda fd, when, old: None)
    monkeypatch.setattr(player.tty, "setcbreak", lambda fd: None)
    monkeypatch.setattr(player.sys, "stdin", FakeStdin(keys, on_read))
    client.quits = []
    client.quit = lambda: client.quits.append(True)
    client.run()
    return path


@posix_only
@pytest.mark.parametrize("keys", [["q"], [KeyboardInterrupt()]], ids=["q", "ctrl-c"])
def test_run_serves_the_control_socket_and_removes_it_on_exit(keys, monkeypatch):
    client = make_remote_client([A], monkeypatch)
    # Not tmp_path: Unix socket paths max out near 104 bytes on macOS.
    path = os.path.join(tempfile.mkdtemp(prefix="ct-"), "control.sock")
    answers = []

    def ask_status():
        answers.append(control.send("status", path))

    run_with_keys(client, keys, monkeypatch, path, on_read=ask_status)
    assert answers[0]["title"] == "First"
    assert client.quits == [True]
    assert not os.path.exists(path)
    os.rmdir(os.path.dirname(path))


def test_on_state_gets_the_status_and_nothing_is_printed(monkeypatch, capsys):
    client = make_remote_client([A, B], monkeypatch)
    states = []
    client.on_state = states.append
    client.play_current()
    client.handle_message({"event": "property-change", "name": "pause", "data": True})
    assert states == [
        {**client.status(), "paused": False},
        client.status(),
    ]
    assert states[1]["paused"] is True
    assert capsys.readouterr().out == ""


def test_without_on_state_each_change_prints_the_status_line(monkeypatch, capsys):
    client = make_remote_client([A], monkeypatch)
    client.play_current()
    client.handle_message({"event": "property-change", "name": "pause", "data": True})
    out = capsys.readouterr().out
    assert out.count("\r") == 2
    assert out.endswith("\r--:-- / --:--  Paused  🔊 ▯▯▯▯▯▯▯▯▯▯ --  First\x1b[K")


def test_seek_volume_and_pause_send_one_mpv_command_each(monkeypatch):
    client = make_remote_client([A], monkeypatch)
    client.seek(5)
    client.seek(-5)
    client.change_volume(5)
    client.toggle_pause()
    assert client.sent == [["seek", 5], ["seek", -5], ["add", "volume", 5], ["cycle", "pause"]]


@posix_only
def test_keys_route_through_the_command_methods(monkeypatch):
    client = make_remote_client([A], monkeypatch)
    calls = []
    client.seek = lambda seconds: calls.append(("seek", seconds))
    client.change_volume = lambda step: calls.append(("volume", step))
    client.toggle_pause = lambda: calls.append(("pause",))
    keys = [",", ".", "\x1b", "[D", "\x1b", "[C", "\x1b", "[A", "\x1b", "[B", " ", "q"]
    path = os.path.join(tempfile.mkdtemp(prefix="ct-"), "control.sock")
    run_with_keys(client, keys, monkeypatch, path)
    os.rmdir(os.path.dirname(path))
    assert calls == [
        ("seek", -player.SEEK_SECONDS),
        ("seek", player.SEEK_SECONDS),
        ("seek", -player.SEEK_SECONDS),
        ("seek", player.SEEK_SECONDS),
        ("volume", player.VOLUME_STEP),
        ("volume", -player.VOLUME_STEP),
        ("pause",),
    ]


def test_status_reports_the_current_videos_uploader():
    client = make_client([A, Video(id="d", title="Fourth", uploader="Dee", duration=4)])
    assert client.status()["uploader"] == "u"
    client.index = 1
    assert client.status()["uploader"] == "Dee"


def test_status_uploader_is_none_with_an_empty_queue():
    assert make_client([]).status()["uploader"] is None


def test_status_reports_volume_and_up_next_but_the_line_leaves_up_next_to_render(monkeypatch):
    client = make_remote_client([A, B], monkeypatch)
    client.handle_message({"event": "property-change", "name": "volume", "data": 70.0})
    status = client.status()
    assert (status["volume"], status["up_next"]) == (70.0, "Second")
    assert player.status_line(status) == "--:-- / --:--  Playing  🔊 ▮▮▮▮▮▮▮▯▯▯ 70%  First  [1/2]"


@pytest.mark.parametrize(
    "index, line",
    [
        (0, "1:23 / 4:56  Playing  🔊 ▯▯▯▯▯▯▯▯▯▯ --  First  [1/2]  Up next: Second"),
        (1, "1:23 / 4:56  Playing  🔊 ▯▯▯▯▯▯▯▯▯▯ --  Second  [2/2]  End of queue"),
    ],
)
def test_render_pins_the_full_line(index, line, monkeypatch, capsys):
    monkeypatch.setattr(player.shutil, "get_terminal_size", lambda fallback: os.terminal_size((120, 24)))
    client = make_remote_client([A, B], monkeypatch)
    client.index = index
    client.state.update({"time-pos": 83, "duration": 296})
    client.render()
    assert capsys.readouterr().out == f"\r{line}\x1b[K"


def test_eof_at_the_last_video_still_notifies_once(monkeypatch):
    client = make_remote_client([A, B], monkeypatch)
    client.index = 1
    states = []
    client.on_state = states.append
    client.handle_message({"event": "end-file", "reason": "eof"})
    assert client.index == 1
    assert client.loaded == []
    assert states == [client.status()]


def test_eof_before_the_end_plays_the_next_video(monkeypatch):
    client = make_remote_client([A, B], monkeypatch)
    states = []
    client.on_state = states.append
    client.handle_message({"event": "end-file", "reason": "eof"})
    assert client.index == 1
    assert client.loaded == [B.url]
    assert len(states) == 1


FAILED = {"event": "end-file", "reason": "error", "file_error": "loading failed"}


def test_a_track_mpv_could_not_load_is_reported_and_skipped(monkeypatch):
    client = make_remote_client([A, B], monkeypatch)
    client.play_current()
    states = []
    client.on_state = states.append
    client.handle_message(FAILED)
    assert client.index == 1
    assert client.loaded == [A.url, B.url]
    assert states == [client.status()]
    assert states[0]["error"] == "Could not play First: loading failed"
    assert client.handle_control("status")["error"] == "Could not play First: loading failed"


def test_a_failed_load_without_file_error_is_unavailable(monkeypatch):
    client = make_remote_client([A], monkeypatch)
    client.play_current()
    client.handle_message({"event": "end-file", "reason": "error"})
    assert client.status()["error"] == "Could not play First: unavailable"
    assert client.idle is True  # the queue ran out, as at an eof


def test_the_error_clears_when_the_next_track_starts(monkeypatch):
    client = make_remote_client([A, B, C], monkeypatch)
    client.play_current()
    client.handle_message(FAILED)
    client.handle_message({"event": "property-change", "name": "time-pos", "data": None})
    assert client.status()["error"] == "Could not play First: loading failed"  # B is still loading
    states = []
    client.on_state = states.append
    client.handle_message({"event": "playback-restart"})
    assert client.status()["error"] is None
    assert states == [client.status()]


def test_play_current_clears_the_error(monkeypatch):
    client = make_remote_client([A], monkeypatch)
    client.play_current()
    client.handle_message(FAILED)
    client.play_current()
    assert client.status()["error"] is None


def test_the_error_is_none_until_a_track_fails(monkeypatch):
    assert make_remote_client([A], monkeypatch).status()["error"] is None


def test_without_on_state_a_failed_track_prints_one_line_above_the_status_line(monkeypatch, capsys):
    client = make_remote_client([A, B], monkeypatch)
    client.play_current()
    capsys.readouterr()
    client.handle_message(FAILED)
    client.handle_message({"event": "property-change", "name": "pause", "data": True})
    out, err = capsys.readouterr()
    assert err == "\rCould not play First: loading failed\x1b[K\n"  # once, though two lines were drawn
    assert out.count("\r") == 2


def fake_clock(monkeypatch, *times):
    """Make time.monotonic return the given times, one per call."""
    ticks = iter(times)
    monkeypatch.setattr(player.time, "monotonic", lambda: next(ticks))


def test_started_in_is_the_time_from_loadfile_to_playback_restart(monkeypatch):
    client = make_remote_client([A, B], monkeypatch)
    fake_clock(monkeypatch, 10.0, 12.4)
    client.play_current()
    assert client.status()["started_in"] is None
    client.handle_message({"event": "playback-restart"})
    assert client.status()["started_in"] == 2.4


def test_started_in_ignores_later_restarts_from_seeks(monkeypatch):
    client = make_remote_client([A], monkeypatch)
    fake_clock(monkeypatch, 10.0, 12.4, 99.0)
    client.play_current()
    client.handle_message({"event": "playback-restart"})
    client.handle_message({"event": "playback-restart"})
    assert client.status()["started_in"] == 2.4


def test_a_new_loadfile_resets_started_in(monkeypatch):
    client = make_remote_client([A, B], monkeypatch)
    fake_clock(monkeypatch, 10.0, 12.4, 20.0)
    client.play_current()
    client.handle_message({"event": "playback-restart"})
    client.next()
    assert client.status()["started_in"] is None


def test_handle_control_status_includes_started_in(monkeypatch):
    client = make_remote_client([A], monkeypatch)
    fake_clock(monkeypatch, 10.0, 11.5)
    client.play_current()
    client.handle_message({"event": "playback-restart"})
    assert client.handle_control("status")["started_in"] == 1.5




def test_status_line_shows_started_in_with_timing(monkeypatch):
    monkeypatch.setenv("TTYPLAYER_TIMING", "1")
    assert player.status_line({**TIMED, "started_in": 2.4}) == "1:23 / 4:56  Playing  🔊 ▮▮▮▮▮▮▮▯▯▯ 70%  Song  started in 2.4s"
    assert player.status_line({**TIMED, "started_in": None}) == "1:23 / 4:56  Playing  🔊 ▮▮▮▮▮▮▮▯▯▯ 70%  Song"


def test_status_line_hides_started_in_without_timing(monkeypatch):
    monkeypatch.delenv("TTYPLAYER_TIMING", raising=False)
    assert player.status_line({**TIMED, "started_in": 2.4}) == "1:23 / 4:56  Playing  🔊 ▮▮▮▮▮▮▮▯▯▯ 70%  Song"


def run_threads(*targets, timeout=2):
    """Run each target on its own thread; fail on a hang or on any thread's exception."""
    errors = []

    def guarded(target):
        try:
            target()
        except Exception as error:
            errors.append(error)

    threads = [threading.Thread(target=guarded, args=(t,), daemon=True) for t in targets]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout)
    assert not any(thread.is_alive() for thread in threads), "a thread hung"
    assert errors == []


class CheckBarrierQueue(list):
    """A queue whose length check waits for a second caller, to open next()'s check-then-move window."""

    def __init__(self, videos):
        super().__init__(videos)
        self.barrier = threading.Barrier(2, timeout=0.2)

    def __len__(self):
        try:
            self.barrier.wait()
        except threading.BrokenBarrierError:
            pass  # the other thread is held off by the lock, as it should be
        return super().__len__()


def test_next_from_two_threads_moves_once(monkeypatch):
    client = make_remote_client([A, B], monkeypatch)
    client.queue = CheckBarrierQueue(client.queue)
    loaded = []

    def slow_load(url):
        time.sleep(0.05)
        loaded.append(url)

    client.load = slow_load
    run_threads(client.next, client.next)
    assert client.index == 1
    assert loaded == [B.url]


def test_a_restart_racing_a_track_change_never_measures_the_new_track(monkeypatch):
    client = make_remote_client([A, B], monkeypatch)
    restart_has_the_clock = threading.Event()
    release_restart = threading.Event()
    ticks = iter([10.0, 12.4, 20.0, 23.0])

    def clock():
        now = next(ticks)
        if now == 12.4:  # A's restart has read the clock; let B load meanwhile
            restart_has_the_clock.set()
            release_restart.wait(2)
        return now

    monkeypatch.setattr(player.time, "monotonic", clock)
    client.play_current()
    restart = threading.Thread(target=client.handle_message, args=({"event": "playback-restart"},))
    restart.start()
    assert restart_has_the_clock.wait(2)
    change = threading.Thread(target=client.next)
    change.start()
    change.join(0.2)  # without the lock, B loads (stamp 20.0) inside A's measurement
    release_restart.set()
    restart.join(2)
    change.join(2)
    assert client.status()["started_in"] is None  # A's 2.4 was replaced by B's reset
    client.handle_message({"event": "playback-restart"})
    assert client.status()["started_in"] == 3.0


def test_on_state_may_wait_for_a_ui_thread_that_moves_the_queue(monkeypatch):
    # The TUI's on_state blocks in call_from_thread until the app thread runs it,
    # and that app thread may be inside next() at that moment.
    client = make_remote_client([A, B, C], monkeypatch)
    app = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    app_thread = app.submit(threading.get_ident).result()
    shown = []
    pressed = []

    def on_state(status):
        if threading.get_ident() == app_thread:
            shown.append(status["index"])
            return
        if not pressed:
            pressed.append(app.submit(client.next))  # the user presses n right now
        app.submit(shown.append, status["index"]).result(timeout=1)

    client.on_state = on_state
    try:
        run_threads(lambda: client.handle_message({"event": "end-file", "reason": "eof"}))
        pressed[0].result(timeout=1)
    finally:
        app.shutdown(wait=False)
    assert client.index == 2
    assert client.loaded == [B.url, C.url]
    assert sorted(shown) == [2, 3]


# --- queue edits: jump, remove, move, clear_others ------------------------

D = Video(id="d", title="Fourth", uploader="u", duration=4)


def playing(videos, index, monkeypatch):
    """A client playing videos[index], with its loads, sends and notifications recorded from here on."""
    client = make_remote_client(videos, monkeypatch)
    client.jump(index)
    client.loaded.clear()
    client.states = []
    client.on_state = client.states.append
    return client


def ids(client):
    return [video.id for video in client.queue]


def test_jump_plays_that_index_and_notifies(monkeypatch):
    client = playing([A, B, C], 0, monkeypatch)
    played = []
    client.on_play = played.append
    assert client.jump(2) is True
    assert client.index == 2
    assert client.loaded == [C.url]
    assert played == [C]
    assert client.started_in is None and client.loaded_at is not None
    assert client.states == [client.status()]


@pytest.mark.parametrize("index", [-1, 3])
def test_jump_out_of_range_does_nothing(index, monkeypatch):
    client = playing([A, B, C], 1, monkeypatch)
    assert client.jump(index) is False
    assert client.index == 1
    assert client.loaded == [] and client.states == []


def test_remove_before_the_current_track_keeps_it_current(monkeypatch):
    client = playing([A, B, C], 2, monkeypatch)
    assert client.remove(0) is True
    assert ids(client) == ["b", "c"]
    assert client.index == 1
    assert client.loaded == []
    assert client.states == [client.status()]
    assert client.status()["index"] == 2 and client.status()["total"] == 2


def test_remove_after_the_current_track_changes_up_next(monkeypatch):
    client = playing([A, B, C], 0, monkeypatch)
    assert client.remove(1) is True
    assert ids(client) == ["a", "c"]
    assert client.index == 0
    assert client.up_next() == "Third"
    assert client.loaded == []


def test_remove_the_current_track_plays_the_next_one(monkeypatch):
    client = playing([A, B, C], 1, monkeypatch)
    client.started_in = 2.4
    assert client.remove(1) is True
    assert ids(client) == ["a", "c"]
    assert client.index == 1
    assert client.loaded == [C.url]
    assert client.started_in is None and client.loaded_at is not None
    assert len(client.states) == 1


def test_remove_the_current_last_track_plays_the_previous_one(monkeypatch):
    client = playing([A, B, C], 2, monkeypatch)
    assert client.remove(2) is True
    assert ids(client) == ["a", "b"]
    assert client.index == 1
    assert client.loaded == [B.url]


def test_remove_the_only_track_stops_and_leaves_an_empty_idle_queue(monkeypatch):
    client = playing([A], 0, monkeypatch)
    client.started_in = 2.4
    client.sent.clear()
    assert client.remove(0) is True
    assert client.queue == [] and client.index == 0
    assert client.idle is True
    assert client.sent == [["stop"]]
    assert client.started_in is None and client.loaded_at is None
    assert client.loaded == []
    assert client.status()["total"] == 0 and client.status()["up_next"] is None
    assert client.states == [client.status()]


def test_remove_the_finished_last_track_after_eof_plays_the_previous_one(monkeypatch):
    client = playing([A, B], 1, monkeypatch)
    client.handle_message({"event": "end-file", "reason": "eof"})
    assert client.idle is True
    assert client.remove(1) is True
    assert ids(client) == ["a"]
    assert client.index == 0
    assert client.loaded == [A.url]
    assert client.idle is False


def test_remove_before_anything_played_plays_the_next_one(monkeypatch):
    client = make_remote_client([A, B, C], monkeypatch)
    assert client.remove(0) is True
    assert ids(client) == ["b", "c"]
    assert client.index == 0
    assert client.loaded == [B.url]
    assert client.idle is False


def test_status_is_idle_with_no_title_once_the_only_track_is_removed(monkeypatch):
    client = playing([A], 0, monkeypatch)
    client.state["media-title"] = "First"
    assert client.status()["idle"] is False
    client.remove(0)
    assert client.status()["idle"] is True
    assert client.status()["title"] is None


@pytest.mark.parametrize("index", [-1, 3])
def test_remove_out_of_range_does_nothing(index, monkeypatch):
    client = playing([A, B, C], 1, monkeypatch)
    assert client.remove(index) is False
    assert ids(client) == ["a", "b", "c"] and client.states == []


@pytest.mark.parametrize(
    "source, target, order, index",
    [
        (1, 2, "acbd", 2),  # the current track itself, down
        (1, 0, "bacd", 0),  # the current track itself, up
        (0, 2, "bcad", 0),  # from before it to after it
        (3, 0, "dabc", 2),  # from after it to before it
        (2, 3, "abdc", 1),  # both after it
        (0, 1, "bacd", 0),  # swapped with it from above
    ],
)
def test_move_keeps_the_current_track_current(source, target, order, index, monkeypatch):
    client = playing([A, B, C, D], 1, monkeypatch)
    assert client.move(source, target) is True
    assert "".join(ids(client)) == order
    assert client.index == index
    assert client.queue[client.index] is B
    assert client.loaded == []
    assert client.states == [client.status()]


@pytest.mark.parametrize("source, target", [(1, 1), (-1, 0), (0, 4), (4, 0)])
def test_move_that_changes_nothing_does_nothing(source, target, monkeypatch):
    client = playing([A, B, C, D], 1, monkeypatch)
    assert client.move(source, target) is False
    assert ids(client) == ["a", "b", "c", "d"] and client.states == []


def test_clear_others_keeps_only_the_current_track(monkeypatch):
    client = playing([A, B, C], 1, monkeypatch)
    queue = client.queue
    assert client.clear_others() is True
    assert client.queue is queue and ids(client) == ["b"]
    assert client.index == 0
    assert client.loaded == []
    assert client.states == [client.status()]
    assert client.clear_others() is False


def test_clear_others_empties_the_queue_when_idle(monkeypatch):
    client = playing([A, B], 1, monkeypatch)
    client.handle_message({"event": "end-file", "reason": "eof"})
    assert client.clear_others() is True
    assert client.queue == [] and client.index == 0
    assert client.loaded == []
    assert client.clear_others() is False


def test_clear_others_before_anything_played_empties_the_queue():
    client = make_client([A, B])
    assert client.idle is True
    assert client.clear_others() is True
    assert client.queue == []


def test_idle_follows_playing_and_the_end_of_the_queue(monkeypatch):
    client = make_remote_client([A, B], monkeypatch)
    assert client.idle is True
    client.play_current()
    assert client.idle is False
    client.handle_message({"event": "end-file", "reason": "eof"})
    assert client.idle is False  # B started
    client.handle_message({"event": "end-file", "reason": "eof"})
    assert client.idle is True
    client.prev()
    assert client.idle is False


def test_handle_control_status_sees_queue_edits(monkeypatch):
    client = playing([A, B, C], 0, monkeypatch)
    client.remove(1)
    client.move(1, 0)
    assert client.handle_control("status") == control.ok(**client.status())
    assert (client.status()["index"], client.status()["total"]) == (2, 2)


# --- mute -------------------------------------------------------------


class FakeMpv:
    """Popen stand-in: listens on the --input-ipc-server socket and keeps every command sent to it."""

    def __init__(self, argv):
        path = next(arg.split("=", 1)[1] for arg in argv if arg.startswith("--input-ipc-server="))
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(path)
        self.server.listen(1)
        self.commands = []
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def serve(self):
        connection, _ = self.server.accept()
        with connection, connection.makefile("r") as lines:
            for line in lines:
                self.commands.append(json.loads(line)["command"])

    def kill(self):
        pass


@posix_only
def test_mute_is_observed_after_the_other_properties(monkeypatch):
    made = []
    monkeypatch.setattr(player.subprocess, "Popen", lambda argv, stdout=None: made.append(FakeMpv(argv)) or made[-1])
    client = player.MpvClient()
    client.stop_polling.set()
    client.ipc.sock.shutdown(socket.SHUT_RDWR)  # EOF for both ends; the listener stops
    made[0].thread.join(timeout=2)
    assert not made[0].thread.is_alive()
    client.ipc.close()
    client._remove_socket_dir()
    assert made[0].commands == [
        ["observe_property", 1, "time-pos"],
        ["observe_property", 2, "duration"],
        ["observe_property", 3, "pause"],
        ["observe_property", 4, "media-title"],
        ["observe_property", 5, "volume"],
        ["observe_property", 6, "mute"],
        ["observe_property", 7, "ao-volume"],
    ]


def test_toggle_mute_cycles_mute(monkeypatch):
    client = make_remote_client([A], monkeypatch)
    client.toggle_mute()
    assert client.sent == [["cycle", "mute"]]


def test_status_reports_muted_from_the_observed_property(monkeypatch):
    client = make_remote_client([A], monkeypatch)
    assert client.status()["muted"] is False
    client.handle_message({"event": "property-change", "name": "mute", "data": True})
    assert client.status()["muted"] is True
    client.handle_message({"event": "property-change", "name": "mute", "data": False})
    assert client.status()["muted"] is False


def test_handle_control_mute_toggles_mute(monkeypatch):
    client = make_remote_client([A], monkeypatch)
    assert client.handle_control("mute") == {"ok": True}
    assert client.sent == [["cycle", "mute"]]


# --- volume: request ids, ao-volume ---------------------------------------


class RecordingTransport:
    """A transport stand-in for send(): keeps every request mpv would have read."""

    def __init__(self):
        self.requests = []

    def write(self, data):
        self.requests.append(json.loads(data))


def wired_client(videos=()):
    """make_client with the real send() writing to a RecordingTransport, and every notify() counted."""
    client = make_client(videos)
    client.ipc = RecordingTransport()
    client.notified = []
    client.on_state = client.notified.append
    return client


def test_send_tags_every_command_with_an_increasing_request_id():
    client = wired_client()
    client.send(["cycle", "pause"])
    client.send(["seek", 5])
    assert client.ipc.requests == [
        {"command": ["cycle", "pause"], "request_id": 1},
        {"command": ["seek", 5], "request_id": 2},
    ]


def test_get_property_registers_its_callback_and_asks_mpv():
    client = wired_client()
    client.get_property("ao-volume", print)
    assert client.ipc.requests == [{"command": ["get_property", "ao-volume"], "request_id": 1}]
    assert client.replies == {1: print}


def test_a_successful_reply_hands_its_data_to_the_callback_once():
    client = wired_client()
    got = []
    client.get_property("ao-volume", got.append)
    client.handle_message({"request_id": 1, "error": "success", "data": 42.0})
    client.handle_message({"request_id": 1, "error": "success", "data": 43.0})
    assert got == [42.0]
    assert client.replies == {}


def test_an_error_reply_hands_none_to_the_callback():
    client = wired_client()
    got = []
    client.get_property("ao-volume", got.append)
    client.handle_message({"request_id": 1, "error": "property unavailable"})
    assert got == [None]


def test_replies_nobody_asked_for_are_ignored():
    client = wired_client()
    got = []
    client.get_property("ao-volume", got.append)
    client.handle_message({"request_id": 7, "error": "success"})
    client.handle_message({"request_id": 0, "error": "success", "data": 1})
    assert got == [] and client.replies == {1: got.append}
    assert client.notified == [] and client.state == {}


def test_replies_reach_the_callback_registered_for_their_id():
    client = wired_client()
    first, second = [], []
    client.get_property("ao-volume", first.append)
    client.get_property("volume", second.append)
    client.handle_message({"request_id": client.request_id, "error": "success", "data": 80.0})
    client.handle_message({"request_id": 1, "error": "success", "data": 30.0})
    assert (first, second) == ([30.0], [80.0])


@pytest.mark.parametrize(
    "state, volume, source",
    [
        ({"ao-volume": 35.0, "volume": 100.0}, 35.0, "device"),
        ({"ao-volume": None, "volume": 100.0}, 100.0, "player"),
        ({"volume": 70.0}, 70.0, "player"),
        ({}, None, None),
        ({"ao-volume": None, "volume": None}, None, None),
    ],
)
def test_status_shows_the_device_volume_when_mpv_has_one(state, volume, source):
    client = make_client([A])
    client.state.update(state)
    assert (client.status()["volume"], client.status()["volume_source"]) == (volume, source)


def test_ao_volume_is_kept_from_its_property_changes():
    client = wired_client([A])
    client.handle_message({"event": "property-change", "name": "ao-volume", "data": 55.0})
    assert client.status()["volume"] == 55.0
    assert client.notified[-1]["volume_source"] == "device"


class OneTick:
    """stop_polling stand-in: the poll loop runs `ticks` times without sleeping, then stops."""

    def __init__(self, ticks=1):
        self.ticks = ticks
        self.waited = []

    def wait(self, timeout):
        self.waited.append(timeout)
        self.ticks -= 1
        return self.ticks < 0


def poll(client, ticks=1):
    client.stop_polling = OneTick(ticks)
    client.poll()
    return client.stop_polling.waited


def test_the_poll_asks_for_ao_volume_every_two_seconds_while_playing():
    client = wired_client([A])
    client.play_current()
    assert poll(client, ticks=2) == [player.VOLUME_POLL_SECONDS] * 3
    assert [r["command"] for r in client.ipc.requests] == [["get_property", "ao-volume"]] * 2


def test_the_poll_asks_nothing_while_idle():
    client = wired_client([A])
    poll(client)
    assert client.ipc.requests == []


def test_a_polled_change_updates_the_meter_and_notifies():
    client = wired_client([A])
    client.play_current()
    client.state.update({"ao-volume": 50.0, "volume": 100.0})
    poll(client)
    client.handle_message({"request_id": 1, "error": "success", "data": 20.0})
    assert len(client.notified) == 2  # play_current, then the new volume
    assert (client.notified[-1]["volume"], client.notified[-1]["volume_source"]) == (20.0, "device")


def test_a_polled_unchanged_value_does_not_notify():
    client = wired_client([A])
    client.play_current()
    client.state.update({"ao-volume": 50.0, "volume": 100.0})
    poll(client)
    client.handle_message({"request_id": 1, "error": "success", "data": 50.0})
    assert len(client.notified) == 1  # play_current only


def test_a_polled_error_falls_back_to_the_player_volume():
    client = wired_client([A])
    client.play_current()
    client.state.update({"ao-volume": 50.0, "volume": 100.0})
    poll(client)
    client.handle_message({"request_id": 1, "error": "property unavailable"})
    assert client.state["ao-volume"] is None
    assert (client.notified[-1]["volume"], client.notified[-1]["volume_source"]) == (100.0, "player")


def levels_client():
    """wired_client playing A with the level filter on."""
    client = wired_client([A])
    client.show_levels = True
    client.play_current()
    client.ipc.requests.clear()
    return client


STEREO = {"lavfi.astats.1.Peak_level": "-20.000000", "lavfi.astats.2.Peak_level": "-inf"}


def test_the_poll_asks_for_the_levels_every_tenth_of_a_second_while_playing():
    client = levels_client()
    assert poll(client, ticks=3) == [player.LEVELS_INTERVAL] * 4
    assert [r["command"] for r in client.ipc.requests] == [["get_property", "af-metadata/levels"]] * 3


def test_with_levels_shown_the_volume_is_still_polled_every_two_seconds():
    client = levels_client()
    poll(client, ticks=40)
    commands = [r["command"] for r in client.ipc.requests]
    assert commands.count(["get_property", "ao-volume"]) == 2
    assert commands.index(["get_property", "ao-volume"]) == 20  # after the 20th levels read


def test_a_levels_reply_is_kept_shown_in_status_and_notified():
    client = levels_client()
    poll(client)
    client.handle_message({"request_id": client.request_id, "error": "success", "data": STEREO})
    assert client.levels == [-20.0, -90.0]
    assert client.notified[-1]["levels"] == [-20.0, -90.0]
    assert json.loads(json.dumps(client.status()))["levels"] == [-20.0, -90.0]


def test_an_unchanged_levels_reply_does_not_notify():
    client = levels_client()
    client.levels = [-20.0, -90.0]
    notified = len(client.notified)
    poll(client)
    client.handle_message({"request_id": client.request_id, "error": "success", "data": STEREO})
    assert len(client.notified) == notified


@pytest.mark.parametrize("reply", [{"error": "property unavailable"}, {"error": "success", "data": {}}])
def test_missing_levels_are_none_and_the_poll_keeps_going(reply):
    client = levels_client()
    client.levels = [-20.0, -20.0]
    poll(client)
    client.handle_message({"request_id": client.request_id, **reply})
    assert client.levels is None
    poll(client)
    assert [r["command"] for r in client.ipc.requests] == [["get_property", "af-metadata/levels"]] * 2


def test_paused_there_are_no_levels_and_none_are_asked_for():
    client = levels_client()
    client.levels = [-20.0, -20.0]
    client.handle_message({"event": "property-change", "name": "pause", "data": True})
    poll(client)
    assert client.ipc.requests == []
    assert client.levels is None
    assert client.notified[-1]["levels"] is None


def test_a_reply_that_lands_after_pausing_is_dropped():
    client = levels_client()
    poll(client)
    client.handle_message({"event": "property-change", "name": "pause", "data": True})
    client.handle_message({"request_id": client.request_id, "error": "success", "data": STEREO})
    assert client.levels is None


def test_idle_there_are_no_levels():
    client = wired_client([A])
    client.show_levels = True
    client.levels = [-20.0, -20.0]
    poll(client)
    assert client.ipc.requests == []
    assert client.status()["levels"] is None


def test_without_levels_the_poll_never_asks_for_them():
    client = wired_client([A])
    client.play_current()
    poll(client, ticks=2)
    assert ["get_property", "af-metadata/levels"] not in [r["command"] for r in client.ipc.requests]


def test_set_levels_removes_and_adds_the_filter_in_the_running_mpv():
    client = levels_client()
    client.levels = [-20.0, -20.0]
    client.set_levels(False)
    assert client.levels is None and client.notified[-1]["levels"] is None
    client.set_levels(False)  # already off: nothing sent
    client.set_levels(True)
    assert [r["command"] for r in client.ipc.requests] == [
        ["af", "remove", "@levels"],
        ["af", "add", LEVELS_ARG.removeprefix("--af=")],
    ]
    assert client.show_levels


def test_set_levels_does_nothing_under_headless_pcm():
    client = wired_client([A])
    client.headless_pcm = True
    client.set_levels(True)
    assert client.ipc.requests == [] and not client.show_levels


def test_handle_control_status_carries_the_levels(monkeypatch):
    client = make_remote_client([A], monkeypatch)
    client.levels = [-1.5, -2.5]
    assert client.handle_control("status")["levels"] == [-1.5, -2.5]


def test_quit_stops_the_poll_before_closing_the_socket():
    client = wired_client()
    client.stop_polling = threading.Event()
    client.poller = threading.Thread(target=client.poll, daemon=True)
    client.poller.start()
    client.process = type("Process", (), {"wait": lambda self, timeout: None})()
    client.ipc.close = lambda: client.ipc.requests.append("closed")
    client.socket_dir = tempfile.mkdtemp()
    client.quit()
    assert not client.poller.is_alive()
    assert client.ipc.requests == [{"command": ["quit"], "request_id": 1}, "closed"]


@pytest.mark.parametrize(
    "state, target",
    [
        ({"ao-volume": 35.0, "volume": 100.0}, "ao-volume"),
        ({"ao-volume": None, "volume": 100.0}, "volume"),
        ({"volume": 100.0}, "volume"),
        ({}, "volume"),
    ],
)
def test_change_volume_moves_the_volume_the_meter_shows(state, target, monkeypatch):
    client = make_remote_client([A], monkeypatch)
    client.state.update(state)
    client.change_volume(5)
    client.change_volume(-5)
    assert client.sent == [["add", target, 5], ["add", target, -5]]


def test_an_ao_volume_event_without_data_keeps_the_polled_value():
    client = wired_client([A])
    client.play_current()
    client.state.update({"ao-volume": 80.0, "volume": 100.0})
    client.handle_message({"event": "property-change", "id": 7, "name": "ao-volume"})
    assert (client.status()["volume"], client.status()["volume_source"]) == (80.0, "device")


def test_an_ao_volume_event_without_data_leaves_it_unknown():
    client = wired_client([A])
    client.handle_message({"event": "property-change", "id": 7, "name": "ao-volume"})
    assert "ao-volume" not in client.state


# --- volume: macOS system volume --------------------------------------------


def mac_client(monkeypatch, system_volume=60):
    """wired_client on darwin, osascript faked: reads return system_volume, writes are kept in client.written."""
    monkeypatch.setattr(player.sys, "platform", "darwin")
    client = wired_client([A])
    client.system_volume = system_volume
    client.written = []

    def write(volume):
        client.written.append(volume)
        client.system_volume = volume
        return True

    monkeypatch.setattr(player, "read_system_volume", lambda: client.system_volume)
    monkeypatch.setattr(player, "write_system_volume", write)
    client.start_volume_writer()
    client.play_current()
    return client


def change_volume(client, step):
    """change_volume(), then wait for the volume writer to finish it."""
    client.change_volume(step)
    client.volume_steps.join()


def test_the_poll_stores_the_system_volume_on_macos_and_notifies(monkeypatch):
    client = mac_client(monkeypatch, system_volume=62)
    client.state.update({"ao-volume": 80.0, "volume": 100.0})
    poll(client)
    assert client.state["system-volume"] == 62
    assert (client.notified[-1]["volume"], client.notified[-1]["volume_source"]) == (62, "system")
    assert [r["command"] for r in client.ipc.requests] == [["get_property", "ao-volume"]]


def test_an_unchanged_system_volume_does_not_notify(monkeypatch):
    client = mac_client(monkeypatch, system_volume=62)
    poll(client)
    notified = len(client.notified)
    poll(client)
    assert len(client.notified) == notified


def test_a_failed_system_volume_read_falls_back_to_ao_volume(monkeypatch):
    client = mac_client(monkeypatch, system_volume=62)
    client.state.update({"ao-volume": 80.0, "volume": 100.0})
    poll(client)
    client.system_volume = None
    poll(client)
    assert (client.notified[-1]["volume"], client.notified[-1]["volume_source"]) == (80.0, "device")


def test_the_system_volume_is_ignored_off_macos(monkeypatch):
    monkeypatch.setattr(player, "read_system_volume", lambda: pytest.fail("osascript off macOS"))
    client = wired_client([A])
    client.play_current()
    client.state.update({"system-volume": 30, "ao-volume": 80.0})
    poll(client)
    assert (client.status()["volume"], client.status()["volume_source"]) == (80.0, "device")


def test_change_volume_on_macos_sets_the_system_volume_and_notifies(monkeypatch):
    client = mac_client(monkeypatch, system_volume=60)
    client.state.update({"ao-volume": 80.0, "volume": 100.0})
    poll(client)
    client.ipc.requests.clear()
    change_volume(client, 5)
    assert client.written == [65]
    assert (client.notified[-1]["volume"], client.notified[-1]["volume_source"]) == (65, "system")
    assert client.ipc.requests == []  # mpv's volumes are left alone


@pytest.mark.parametrize("start, step, written", [(98, 5, 100), (3, -5, 0)])
def test_change_volume_on_macos_clamps_to_0_100(start, step, written, monkeypatch):
    client = mac_client(monkeypatch, system_volume=start)
    poll(client)
    change_volume(client, step)
    assert client.written == [written]
    assert client.status()["volume"] == written


def test_change_volume_on_macos_writes_off_the_callers_thread_in_order(monkeypatch):
    client = mac_client(monkeypatch, system_volume=60)
    poll(client)
    threads = []
    write = player.write_system_volume
    monkeypatch.setattr(player, "write_system_volume", lambda volume: threads.append(threading.get_ident()) or write(volume))
    client.change_volume(5)
    client.change_volume(5)
    client.change_volume(-20)
    client.volume_steps.join()
    assert client.written == [65, 70, 50]
    assert threading.get_ident() not in threads
    assert [s["volume"] for s in client.notified[-3:]] == [65, 70, 50]


def test_a_failed_system_volume_write_keeps_the_meter(monkeypatch):
    client = mac_client(monkeypatch, system_volume=60)
    poll(client)
    monkeypatch.setattr(player, "write_system_volume", lambda volume: False)
    notified = len(client.notified)
    change_volume(client, 5)
    assert client.status()["volume"] == 60
    assert len(client.notified) == notified


def test_a_system_volume_read_in_flight_holds_a_write_back(monkeypatch):
    """The poll's read began at 60 and a key writes before it returns: the write waits, then builds on 60."""
    client = mac_client(monkeypatch, system_volume=60)
    poll(client)
    reading, release = threading.Event(), threading.Event()

    def slow_read():
        volume = client.system_volume
        reading.set()
        release.wait(timeout=5)
        return volume

    monkeypatch.setattr(player, "read_system_volume", slow_read)
    poller = threading.Thread(target=poll, args=(client,))
    poller.start()
    assert reading.wait(timeout=5)
    client.change_volume(5)
    time.sleep(0.2)  # time for a writer that doesn't wait for the read to write 65
    assert client.written == []
    release.set()
    poller.join(timeout=5)
    client.volume_steps.join()
    assert client.written == [65]
    assert client.status()["volume"] == 65
    poll(client)  # reads 65, no older value
    change_volume(client, 5)
    assert client.written == [65, 70]
    assert client.status()["volume"] == 70


def test_the_volume_writer_runs_only_on_macos(monkeypatch):
    client, _ = piped_client(monkeypatch)
    client.quit()
    assert client.volume_writer is None


def test_quit_stops_the_volume_writer_and_drops_the_steps_not_yet_written(monkeypatch):
    monkeypatch.setattr(player.sys, "platform", "darwin")
    writing, release = threading.Event(), threading.Event()
    written = []

    def slow_write(volume):
        writing.set()
        release.wait(timeout=5)
        written.append(volume)
        return True

    monkeypatch.setattr(player, "write_system_volume", slow_write)
    client, _ = piped_client(monkeypatch)
    assert client.volume_writer.is_alive()
    client.state["system-volume"] = 60
    client.change_volume(5)
    assert writing.wait(timeout=5)
    client.change_volume(5)
    client.change_volume(5)
    quitting = threading.Thread(target=client.quit)
    quitting.start()
    assert client.stop_writing.wait(timeout=5)
    release.set()
    quitting.join(timeout=5)
    assert not client.volume_writer.is_alive()
    assert written == [65]


def test_change_volume_on_macos_without_a_system_volume_moves_ao_volume(monkeypatch):
    client = mac_client(monkeypatch, system_volume=None)
    client.state.update({"ao-volume": 80.0})
    client.change_volume(5)
    assert client.written == []
    assert client.ipc.requests[-1]["command"] == ["add", "ao-volume", 5]


@pytest.mark.parametrize(
    "run, volume",
    [
        (lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="62\n"), 62),
        (lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="missing value\n"), None),
        (lambda *a, **k: subprocess.CompletedProcess(a, 1, stdout=""), None),
        (lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("osascript")), None),
        (lambda *a, **k: (_ for _ in ()).throw(subprocess.TimeoutExpired("osascript", 1)), None),
    ],
)
def test_read_system_volume(run, volume, monkeypatch):
    monkeypatch.setattr(player.subprocess, "run", run)
    assert player.read_system_volume() == volume


def test_write_system_volume_runs_one_fixed_script(monkeypatch):
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs["timeout"]))
        return subprocess.CompletedProcess(argv, 0, stdout="")

    monkeypatch.setattr(player.subprocess, "run", run)
    assert player.write_system_volume(65) is True
    assert calls == [(["osascript", "-e", "set volume output volume 65"], player.SYSTEM_VOLUME_TIMEOUT)]


# --- platforms (windows) ----------------------------------------------------------


def routed_calls(client):
    """Replace the command methods with recorders; returns the list they append to."""
    calls = []
    client.seek = lambda seconds: calls.append(("seek", seconds))
    client.change_volume = lambda step: calls.append(("volume", step))
    client.toggle_pause = lambda: calls.append(("pause",))
    client.next = lambda: calls.append(("next",))
    client.prev = lambda: calls.append(("prev",))
    return calls


EVERY_KEY_CALLS = [
    ("seek", -player.SEEK_SECONDS),
    ("seek", player.SEEK_SECONDS),
    ("seek", -player.SEEK_SECONDS),
    ("seek", player.SEEK_SECONDS),
    ("volume", player.VOLUME_STEP),
    ("volume", -player.VOLUME_STEP),
    ("pause",),
    ("next",),
    ("prev",),
]


def test_press_does_what_the_shared_key_table_says(monkeypatch):
    client = make_remote_client([A], monkeypatch)
    calls = routed_calls(client)
    for key in [",", ".", "left", "right", "up", "down", " ", "n", "p", "x", None]:
        client.press(key)
    assert calls == EVERY_KEY_CALLS


def test_terminal_and_console_name_the_arrows_the_same():
    assert set(player.TERMINAL_ARROWS.values()) == set(player.CONSOLE_ARROWS.values()) == {"left", "right", "up", "down"}
    assert {name for name in player.KEYS if len(name) > 1} == set(player.CONSOLE_ARROWS.values())


class FakeMsvcrt:
    """msvcrt stand-in: kbhit() is False once before every key (a poll tick), then getwch() plays it.

    With no keys left kbhit() stays False and calls on_idle, if given.
    """

    def __init__(self, keys, on_idle=None):
        self.keys = list(keys)
        self.polls = 0
        self.on_idle = on_idle

    def kbhit(self):
        if not self.keys:
            if self.on_idle:
                self.on_idle()
            return False
        self.polls += 1
        return self.polls % 2 == 0

    def getwch(self):
        return self.keys.pop(0)  # a prefix's letter follows without another kbhit()


def run_on_windows(client, msvcrt, monkeypatch):
    """Run client.run() as on Windows: console keys from msvcrt, no control server, quit() recorded."""
    monkeypatch.setattr(player, "WINDOWS", True)
    monkeypatch.setattr(player, "msvcrt", msvcrt, raising=False)
    monkeypatch.setattr(player.control, "serve", lambda handler: None)
    client.quits = []
    client.quit = lambda: client.quits.append(True)
    client.run()


def test_console_keys_route_like_the_terminal_keys(monkeypatch, capsys):
    client = make_remote_client([A], monkeypatch)
    calls = routed_calls(client)
    keys = [",", ".", "\xe0", "K", "\xe0", "M", "\x00", "H", "\xe0", "P", "\xe0", "S", " ", "n", "p", "q"]
    monkeypatch.setattr(player.time, "sleep", lambda seconds: None)
    run_on_windows(client, FakeMsvcrt(keys), monkeypatch)
    assert calls == EVERY_KEY_CALLS
    assert client.quits == [True]


def test_console_ctrl_c_quits(monkeypatch, capsys):
    client = make_remote_client([A], monkeypatch)
    monkeypatch.setattr(player.time, "sleep", lambda seconds: None)
    run_on_windows(client, FakeMsvcrt(["\x03", "n"]), monkeypatch)
    assert client.quits == [True]


def test_console_keys_poll_instead_of_blocking(monkeypatch):
    sleeps = []
    monkeypatch.setattr(player.time, "sleep", sleeps.append)
    monkeypatch.setattr(player, "msvcrt", FakeMsvcrt(["n"]), raising=False)
    assert next(player.console_keys()) == "n"
    assert sleeps == [player.KEY_POLL_SECONDS]


def stop_from_another_thread(sent):
    """What `ttyplayer stop` does: the control thread calls interrupt_main(); sent gets the time it did."""

    def stop():
        sent.append(time.monotonic())
        player.interrupt_main()

    threading.Thread(target=stop, daemon=True).start()


def test_stop_ends_run_on_windows_by_the_next_poll_tick(monkeypatch, capsys):
    client = make_remote_client([A], monkeypatch)
    monkeypatch.setattr(player, "interrupt_main", REAL_INTERRUPT_MAIN)
    sent = []
    msvcrt = FakeMsvcrt([], on_idle=lambda: sent or stop_from_another_thread(sent))
    run_on_windows(client, msvcrt, monkeypatch)
    assert client.quits == [True]
    assert time.monotonic() - sent[0] < 0.2


@posix_only
def test_stop_ends_run_on_posix_at_once(monkeypatch):
    client = make_remote_client([A], monkeypatch)
    monkeypatch.setattr(player, "interrupt_main", REAL_INTERRUPT_MAIN)
    sent = []

    def blocking_read():
        stop_from_another_thread(sent)
        time.sleep(5)  # a read waiting for a key; SIGINT cuts it short

    # No control server: its stop() waits out an accept tick, which is not the stop's latency.
    monkeypatch.setattr(player.control, "serve", lambda handler: None)
    run_with_keys(client, [], monkeypatch, "unused", on_read=blocking_read)
    assert client.quits == [True]
    assert time.monotonic() - sent[0] < 0.2


def test_interrupt_main_uses_thread_interrupt_main_on_windows(monkeypatch):
    called = []
    monkeypatch.setattr(player, "WINDOWS", True)
    monkeypatch.setattr(player._thread, "interrupt_main", lambda: called.append(True))
    monkeypatch.setattr(player.signal, "pthread_kill", lambda *args: called.append("pthread_kill"), raising=False)
    player.interrupt_main()
    assert called == [True]


def test_ipc_path_is_a_socket_in_a_private_dir_on_posix(monkeypatch):
    monkeypatch.setattr(player, "WINDOWS", False)
    directory, path = player.ipc_path()
    try:
        assert path == os.path.join(directory, "mpv.sock")
        assert os.path.isdir(directory)
    finally:
        os.rmdir(directory)


def test_ipc_path_is_a_named_pipe_on_windows(monkeypatch):
    monkeypatch.setattr(player, "WINDOWS", True)
    directory, path = player.ipc_path()
    assert directory is None
    prefix = f"\\\\.\\pipe\\ttyplayer-{os.getpid()}-"
    assert path.startswith(prefix)
    assert len(path) == len(prefix) + 8
    assert player.ipc_path()[1] != path


def test_pipe_transport_reads_lines_and_writes_bytes(tmp_path):
    pipe = tmp_path / "pipe"
    pipe.write_bytes(b'{"event": "idle"}\n{"event": "x"}\n')
    transport = player.PipeTransport(str(pipe))
    try:
        assert transport.readline() == b'{"event": "idle"}\n'
        assert json.loads(transport.readline()) == {"event": "x"}
        assert transport.readline() == b""
        transport.write(b"tail\n")
    finally:
        transport.close()
    assert pipe.read_bytes().endswith(b"tail\n")


def test_pipe_transport_writes_everything_even_in_pieces():
    written = []

    class OneByteAtATime:
        def write(self, data):
            written.append(bytes(data[:1]))
            return 1

    transport = player.PipeTransport.__new__(player.PipeTransport)
    transport.pipe = OneByteAtATime()
    transport.write(b"abc")
    assert written == [b"a", b"b", b"c"]


def test_connect_waits_for_the_pipe_to_appear(monkeypatch, tmp_path):
    monkeypatch.setattr(player, "WINDOWS", True)
    pipe = tmp_path / "pipe"
    waits = []

    def sleep(seconds):
        waits.append(seconds)
        if len(waits) == 3:
            pipe.write_bytes(b"")  # mpv got round to creating it

    monkeypatch.setattr(player.time, "sleep", sleep)
    transport = player.connect(str(pipe))
    transport.close()
    assert isinstance(transport, player.PipeTransport)
    assert waits == [0.1] * 3


def test_connect_gives_up_when_the_pipe_never_appears(monkeypatch, tmp_path):
    monkeypatch.setattr(player, "WINDOWS", True)
    monkeypatch.setattr(player.time, "sleep", lambda seconds: None)
    assert player.connect(str(tmp_path / "never")) is None


class FakePipe:
    """PipeTransport stand-in: records the path and every request; mpv says nothing back."""

    made = []

    def __init__(self, path):
        self.path = path
        self.requests = []
        self.closed = False
        FakePipe.made.append(self)

    def write(self, data):
        self.requests.append(json.loads(data)["command"])

    def readline(self):
        return b""  # EOF: the listener stops

    def close(self):
        self.closed = True


def piped_client(monkeypatch):
    """A real MpvClient() over a FakePipe, as on Windows, and the argvs it started mpv with; no mpv runs."""
    monkeypatch.setattr(player, "WINDOWS", True)
    monkeypatch.setattr(player, "PipeTransport", FakePipe)
    FakePipe.made.clear()
    argvs = []
    process = type("Process", (), {"wait": lambda self, timeout: None})()
    monkeypatch.setattr(player.subprocess, "Popen", lambda argv, stdout=None: argvs.append(argv) or process)
    return player.MpvClient(), argvs


def test_mpv_client_starts_mpv_with_the_level_filter_unless_levels_is_off(monkeypatch):
    client, argvs = piped_client(monkeypatch)
    client.quit()
    assert LEVELS_ARG in argvs[0] and client.show_levels
    monkeypatch.setattr(player.subprocess, "Popen", lambda argv, stdout=None: argvs.append(argv) or client.process)
    quiet = player.MpvClient(levels=False)
    quiet.quit()
    assert LEVELS_ARG not in argvs[1] and not quiet.show_levels


def test_mpv_client_on_windows_talks_over_a_named_pipe(monkeypatch):
    client, argvs = piped_client(monkeypatch)
    client.quit()
    [pipe] = FakePipe.made
    assert f"--input-ipc-server={pipe.path}" in argvs[0]
    assert pipe.path.startswith("\\\\.\\pipe\\ttyplayer-")
    assert client.socket_dir is None
    assert pipe.requests[0] == ["observe_property", 1, "time-pos"]
    assert pipe.requests[-1] == ["quit"]
    assert pipe.closed


def test_the_modules_import_on_windows_without_termios():
    # A fresh interpreter that looks like Windows: no termios or tty, a stand-in msvcrt.
    # Only ttyplayer's own modules; typer and textual have their own Windows imports, and
    # the windows-latest CI run imports everything for real.
    code = """
import json, secrets, signal, socket, subprocess, sys, tempfile, threading, types
sys.platform = "win32"  # after the stdlib that picks its own Windows modules by it
sys.modules["termios"] = None
sys.modules["tty"] = None
sys.modules["msvcrt"] = types.ModuleType("msvcrt")
from ttyplayer import control, player
assert player.WINDOWS and control.WINDOWS and player.msvcrt is sys.modules["msvcrt"]
assert not hasattr(player, "termios")
"""
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@posix_only
def test_minus_plus_and_equals_change_the_volume(monkeypatch):
    client = make_remote_client([A], monkeypatch)
    calls = []
    client.change_volume = lambda step: calls.append(step)
    path = os.path.join(tempfile.mkdtemp(prefix="ct-"), "control.sock")
    run_with_keys(client, ["-", "+", "=", "q"], monkeypatch, path)
    os.rmdir(os.path.dirname(path))
    assert calls == [-player.VOLUME_STEP, player.VOLUME_STEP, player.VOLUME_STEP]


def test_handle_control_queue_lists_the_queued_videos(monkeypatch):
    client = make_remote_client([A, B, A], monkeypatch)
    client.index = 1
    assert client.handle_control("queue") == {
        "ok": True,
        "videos": [
            {"id": A.id, "title": A.title, "uploader": A.uploader, "duration": A.duration, "source": "youtube", "link": None},
            {"id": B.id, "title": B.title, "uploader": B.uploader, "duration": B.duration, "source": "youtube", "link": None},
            {"id": A.id, "title": A.title, "uploader": A.uploader, "duration": A.duration, "source": "youtube", "link": None},
        ],
        "index": 2,
    }
    assert client.sent == []


def test_handle_control_queue_when_empty(monkeypatch):
    client = make_remote_client([], monkeypatch)
    assert client.handle_control("queue") == {"ok": True, "videos": [], "index": 1}


# --- sleep timer --------------------------------------------------------


@pytest.mark.parametrize(
    "text, spec",
    [("30m", 1800), ("1h", 3600), ("1h30m", 5400), ("90", 90), ("2H5M", 7500), (" end ", "end"), ("off", None)],
)
def test_parse_sleep_reads_durations_end_and_off(text, spec):
    assert player.parse_sleep(text) == spec


@pytest.mark.parametrize("text", ["", "0", "0m", "30s", "1h30", "m", "-5", "1.5h", "soon", "٣"])
def test_parse_sleep_refuses_anything_else_with_the_accepted_forms(text):
    with pytest.raises(ValueError, match="30m, 1h, 1h30m, 90 \\(seconds\\), end or off"):
        player.parse_sleep(text)


def fixed_now(monkeypatch, now=1000.0):
    monkeypatch.setattr(player.time, "time", lambda: now)


@pytest.mark.parametrize(
    "sleep, text, message",
    [
        (None, "", "Sleep timer off"),
        ({"after": "track"}, "zz end", "Sleeping after this track"),
        ({"ends_at": 2200.0}, "zz 20:00", "Sleeping in 20:00"),
        ({"ends_at": 1000.2}, "zz 0:01", "Sleeping in 0:01"),  # a part second left still shows
        ({"ends_at": 990.0}, "zz 0:00", "Sleeping in 0:00"),  # fading
    ],
)
def test_sleep_text_and_message(sleep, text, message, monkeypatch):
    fixed_now(monkeypatch)
    assert player.sleep_text(sleep) == text
    assert player.sleep_message(sleep) == message


def test_status_line_shows_the_sleep_timer_after_the_state(monkeypatch):
    fixed_now(monkeypatch)
    status = {"position": 1, "duration": 2, "paused": False, "volume": None, "muted": False, "title": "T", "total": 1}
    assert "Playing  zz 20:00  🔊" in player.status_line({**status, "sleep": {"ends_at": 2200.0}})
    assert "zz" not in player.status_line(status)


class FakeTimer:
    """threading.Timer stand-in: never runs by itself; fire() runs the callback on the caller's thread."""

    def __init__(self, interval, function, args=()):
        self.interval, self.function, self.args = interval, function, args
        self.started = self.cancelled = False

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True

    def join(self, timeout=None):
        pass

    def fire(self):
        self.function(*self.args)


def sleeping_client(monkeypatch, videos=(A, B)):
    """make_remote_client playing videos[0] at player volume 50, FakeTimers kept in .timers, fade waits in .waits."""
    client = make_remote_client(list(videos), monkeypatch)
    client.timers, client.waits = [], []
    client.timer = lambda *args, **kwargs: client.timers.append(FakeTimer(*args, **kwargs)) or client.timers[-1]
    client.fade_wait = lambda cancelled, seconds: client.waits.append(seconds)
    client.play_current()
    client.state.update({"volume": 50.0, "pause": False})
    fixed_now(monkeypatch)
    return client


def test_sleep_arms_one_timer_and_status_says_when_it_ends(monkeypatch):
    client = sleeping_client(monkeypatch)
    client.sleep(1200)
    [timer] = client.timers
    assert (timer.interval, timer.started, timer.cancelled) == (1200, True, False)
    assert client.status()["sleep"] == {"ends_at": 2200.0}


def test_a_new_sleep_replaces_the_timer_and_none_cancels_it(monkeypatch):
    client = sleeping_client(monkeypatch)
    client.sleep(1200)
    client.sleep(60)
    assert [timer.cancelled for timer in client.timers] == [True, False]
    assert client.status()["sleep"] == {"ends_at": 1060.0}
    client.sleep(None)
    assert client.timers[1].cancelled
    assert client.status()["sleep"] is None


def test_sleep_end_stops_at_the_end_of_the_track_instead_of_playing_the_next(monkeypatch):
    client = sleeping_client(monkeypatch)
    client.sleep(1200)
    client.sleep("end")
    assert client.timers[0].cancelled
    assert client.status()["sleep"] == {"after": "track"}
    client.handle_message({"event": "end-file", "reason": "eof"})
    assert client.interrupted == [True]
    assert client.loaded == [A.url]  # B never loads
    assert client.status()["sleep"] is None


def test_without_sleep_end_the_next_track_plays(monkeypatch):
    client = sleeping_client(monkeypatch)
    client.handle_message({"event": "end-file", "reason": "eof"})
    assert client.interrupted == []
    assert client.loaded == [A.url, B.url]


def test_the_timer_fades_the_volume_out_pauses_restores_it_then_stops(monkeypatch):
    client = sleeping_client(monkeypatch)
    client.sleep(60)
    client.timers[0].fire()
    steps = [["add", "volume", -5.0]] * player.SLEEP_FADE_STEPS
    assert client.sent == steps + [["cycle", "pause"], ["add", "volume", 50.0]]
    assert client.waits == [player.SLEEP_FADE_SECONDS / player.SLEEP_FADE_STEPS] * player.SLEEP_FADE_STEPS
    assert sum(client.waits) == pytest.approx(player.SLEEP_FADE_SECONDS)
    assert client.interrupted == [True]


def test_the_fade_moves_the_volume_the_keys_move(monkeypatch):
    client = sleeping_client(monkeypatch)
    client.state["ao-volume"] = 35.0
    client.sleep(60)
    client.timers[0].fire()
    assert client.sent[0] == ["add", "ao-volume", -3.5]
    assert client.sent[-1] == ["add", "ao-volume", 35.0]


@pytest.mark.parametrize("state, idle", [({"pause": True}, False), ({}, True), ({"volume": 0.0}, False)])
def test_the_timer_stops_at_once_when_paused_idle_or_silent(state, idle, monkeypatch):
    client = sleeping_client(monkeypatch)
    client.state.update(state)
    client.idle = idle
    client.sleep(60)
    client.timers[0].fire()
    assert client.sent == []
    assert client.interrupted == [True]


def test_sleep_off_during_the_fade_restores_the_volume_and_plays_on(monkeypatch):
    client = sleeping_client(monkeypatch)
    client.sleep(60)
    client.fade_wait = lambda cancelled, seconds: len(client.sent) == 3 and client.sleep(None)
    client.timers[0].fire()
    assert client.sent == [["add", "volume", -5.0]] * 3 + [["add", "volume", 15.0]]
    assert client.interrupted == []


def test_an_older_timer_firing_late_does_nothing(monkeypatch):
    client = sleeping_client(monkeypatch)
    client.sleep(60)
    client.sleep(None)
    client.timers[0].fire()  # cancel() raced the timer
    assert client.sent == []
    assert client.interrupted == []


def ready_to_quit(client):
    """Stand-ins for what quit() tears down after the timer: no poller or writer, a finished process, a no-op ipc."""
    client.poller = client.volume_writer = None
    client.process = subprocess.Popen([sys.executable, "-c", ""])
    client.ipc = type("Ipc", (), {"close": lambda self: None})()
    client.socket_dir = None


def test_quit_cancels_the_timer(monkeypatch):
    client = sleeping_client(monkeypatch)
    client.sleep(60)
    client.sleep("end")
    client.sleep(60)
    ready_to_quit(client)
    client.quit()
    assert client.timers[-1].cancelled
    assert client.status()["sleep"] is None


@pytest.mark.parametrize("then", ["off", "replaced", "quit"])
def test_quit_waits_for_a_cancelled_fade_to_restore_the_volume(then, monkeypatch):
    client = sleeping_client(monkeypatch)
    ready_to_quit(client)
    client.sleep(60)
    stepped, restoring, go_on = threading.Event(), threading.Event(), threading.Event()
    client.fade_wait = lambda cancelled, seconds: (stepped.set(), cancelled.wait(60))  # only a cancel ends the step
    record = client.send

    def slow_restore(command):  # a restore that takes longer than one step + 1 s, as osascript on macOS may
        if command == ["add", "volume", 5.0]:
            restoring.set()
            go_on.wait(5)
        record(command)

    client.send = slow_restore
    fader = threading.Thread(target=client.timers[0].fire)  # the timer's own thread, fading
    fader.start()
    assert stepped.wait(5)  # waiting after the first step
    if then != "quit":
        client.sleep({"off": None, "replaced": 60}[then])
    quitter = threading.Thread(target=client.quit)
    quitter.start()
    assert restoring.wait(1)  # the cancel cut the step short: the volume comes back at once
    quitter.join(timeout=player.SLEEP_FADE_SECONDS / player.SLEEP_FADE_STEPS + 1.2)  # past round 2's 1.5 s cap
    assert quitter.is_alive()  # quit() still waits for the restore
    go_on.set()
    fader.join(5)
    quitter.join(5)
    assert client.sent == [["add", "volume", -5.0], ["add", "volume", 5.0], ["quit"]]
    assert client.interrupted == []


@pytest.mark.parametrize(
    "name, reply",
    [
        ("sleep 20m", {"ok": True, "message": "Sleeping in 20:00"}),
        ("sleep end", {"ok": True, "message": "Sleeping after this track"}),
        ("sleep off", {"ok": True, "message": "Sleep timer off"}),
        ("sleep", {"ok": True, "message": "Sleep timer off"}),
        ("sleep soon", {"ok": False, "error": f"Sleep takes {player.SLEEP_FORMS}, not 'soon'"}),
        ("sleepy", {"ok": False, "error": "unknown command sleepy"}),
    ],
)
def test_handle_control_sleep_replies_the_human_line(name, reply, monkeypatch):
    client = sleeping_client(monkeypatch)
    assert client.handle_control(name) == reply


def test_handle_control_sleep_with_no_text_shows_the_armed_timer(monkeypatch):
    client = sleeping_client(monkeypatch)
    client.handle_control("sleep 1h")
    assert client.handle_control("sleep") == {"ok": True, "message": "Sleeping in 60:00"}
    assert client.handle_control("status")["sleep"] == {"ends_at": 4600.0}
    assert len(client.timers) == 1
