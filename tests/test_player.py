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


def make_client(videos):
    """A MpvClient with no mpv behind it: queue only, load() just records urls."""
    client = player.MpvClient.__new__(player.MpvClient)
    client.queue = []
    client.index = 0
    client.state = {}
    client.on_play = None
    client.on_state = None
    client.queue_lock = threading.RLock()
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
    monkeypatch.setattr(player.subprocess, "Popen", lambda argv: made.append(FakeMpv(argv)) or made[-1])
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
    client.handle_message({"request_id": 2, "error": "success", "data": 80.0})
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
    client.poll_volume()
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


def test_quit_stops_the_poll_before_closing_the_socket():
    client = wired_client()
    client.stop_polling = threading.Event()
    client.poller = threading.Thread(target=client.poll_volume, daemon=True)
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


def test_mpv_client_on_windows_talks_over_a_named_pipe(monkeypatch):
    monkeypatch.setattr(player, "WINDOWS", True)
    monkeypatch.setattr(player, "PipeTransport", FakePipe)
    FakePipe.made.clear()
    argvs = []
    process = type("Process", (), {"wait": lambda self, timeout: None})()
    monkeypatch.setattr(player.subprocess, "Popen", lambda argv: argvs.append(argv) or process)
    client = player.MpvClient()
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
