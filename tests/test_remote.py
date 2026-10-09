import inspect
import json
import re
import socket
import threading
import time
from pathlib import Path

import pytest
from aiohttp import web

from ttyplayer import player, remote, server, settings, tui, youtube
from ttyplayer.models import Video

TOKEN = "secret-token"
VIDEOS = [Video(id=f"v{n}", title=f"Song {n}", uploader="u", duration=60) for n in range(3)]
BY_URL = {video.url: video for video in VIDEOS}


class FakePlayer(player.MpvClient):
    """The server's player: MpvClient's real queue, commands and status, never mpv; keeps what it would send mpv."""

    def __init__(self):
        self.on_play = None
        self.on_state = None
        self.queue_lock = threading.RLock()
        self.state = {}
        self.queue = []
        self.index = 0
        self.sent = []

    def send(self, command, on_reply=None):
        self.sent.append(command)


class Served:
    """`ttyplayer serve` on a free port: the real app over a FakePlayer; every API request it took is kept."""

    def __init__(self, port=None):
        self.player = FakePlayer()
        self.requests = []
        self.port = port or free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.start()

    def start(self):
        hub = server.Broadcaster()
        self.player.on_state = hub
        app = server.make_app(self.player, settings.Settings(server_token=TOKEN), hub)
        app.middlewares.append(self.record)
        self.web = server.ServerThread(app, "127.0.0.1", self.port)

    @web.middleware
    async def record(self, request, handler):
        if request.path.startswith("/api/"):
            body = await request.json() if request.can_read_body else None
            self.requests.append((request.method, request.path, body))
        return await handler(request)

    def posts(self):
        return [(path, body) for method, path, body in self.requests if method == "POST"]

    def stop(self):
        self.web.stop()


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def until(condition, timeout=5):
    """Wait for condition() to hold, as the threads get there; fails the test after timeout seconds."""
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            pytest.fail("timed out waiting")
        time.sleep(0.02)


@pytest.fixture(autouse=True)
def lookups(monkeypatch):
    """The server's YouTube lookups answer from VIDEOS, never the network."""
    monkeypatch.setattr(youtube, "fetch", lambda url: [BY_URL[url]])
    monkeypatch.setattr(remote, "RETRY_SECONDS", (0.05,))


@pytest.fixture
def served():
    served = Served()
    yield served
    served.stop()


@pytest.fixture
def connect():
    """connect(url, token=TOKEN) -> a RemoteClient whose states and errors land in .states and .errors."""
    made = []

    def connect(url, token=TOKEN):
        states, errors = [], []
        client = remote.RemoteClient(url, token, on_state=states.append, on_error=errors.append)
        client.states, client.errors = states, errors
        made.append(client)
        return client

    yield connect
    for client in made:
        client.quit()


def connected(client):
    until(lambda: client.states)
    return client


# --- AC1: the calls TtyplayerApp makes ---------------------------------------------


# What TtyplayerApp reads or calls on its player, from tui.py itself, so a new call cannot be missed.
TUI_USES = set(re.findall(r"\bclient\.(\w+)", Path(tui.__file__).read_text(encoding="utf-8")))
SPEC_NAMES = {"add", "play_current", "next", "prev", "jump", "remove", "move", "clear_others", "toggle_pause",
              "seek", "change_volume", "toggle_mute", "handle_control", "status", "queue", "index", "idle", "quit"}


def test_remote_client_answers_what_tui_uses_as_mpv_client_does(connect):
    client = connect(f"http://127.0.0.1:{free_port()}")
    mpv_client = vars(player.MpvClient)
    for name in TUI_USES | SPEC_NAMES:
        assert hasattr(client, name), name
        method = mpv_client.get(name)
        if callable(method):
            assert callable(getattr(client, name)), name
            assert params(getattr(remote.RemoteClient, name)) == params(method), name
    assert TUI_USES - {"queue", "index", "queue_lock", "idle"} <= set(mpv_client)


def params(function):
    return list(inspect.signature(function).parameters)


@pytest.mark.parametrize(
    "method, args, command, sent",
    [
        ("toggle_pause", (), {"name": "pause"}, ["cycle", "pause"]),
        ("toggle_mute", (), {"name": "mute"}, ["cycle", "mute"]),
        ("seek", (5,), {"name": "seek", "value": 5}, ["seek", 5]),
        ("change_volume", (-5,), {"name": "volume", "value": -5}, ["add", "volume", -5]),
        ("next", (), {"name": "next"}, ["loadfile", VIDEOS[2].url]),
        ("prev", (), {"name": "prev"}, ["loadfile", VIDEOS[0].url]),
        ("jump", (0,), {"name": "jump", "value": 0}, ["loadfile", VIDEOS[0].url]),
        ("remove", (1,), {"name": "remove", "value": 1}, ["loadfile", VIDEOS[2].url]),
    ],
)
def test_each_call_is_one_server_command(served, connect, method, args, command, sent):
    served.player.queue[:] = VIDEOS
    served.player.jump(1)
    client = connected(connect(served.url))
    assert getattr(client, method)(*args) is True
    assert served.posts() == [("/api/command", command)]
    assert served.player.sent[-1] == sent
    assert client.errors == []


def test_clear_others_keeps_the_current_track_and_the_queue_follows(served, connect):
    served.player.queue[:] = VIDEOS
    served.player.jump(1)
    client = connected(connect(served.url))
    assert client.clear_others() is True
    assert served.posts() == [("/api/command", {"name": "clear_others"})]
    assert client.queue == [VIDEOS[1]]
    assert client.index == 0


def test_handle_control_does_the_command_on_the_server(served, connect):
    client = connected(connect(served.url))
    assert client.handle_control("pause") == {"ok": True}
    assert client.handle_control("dance")["ok"] is False
    assert served.posts() == [("/api/command", {"name": "pause"})]


def test_move_reorders_the_servers_queue_and_the_current_track_stays_current(served, connect):
    served.player.queue[:] = VIDEOS
    served.player.jump(0)
    client = connected(connect(served.url))
    until(lambda: client.queue == VIDEOS)
    assert client.move(0, 2) is True
    assert served.posts() == [("/api/command", {"name": "move", "value": [0, 2]})]
    assert served.player.queue == [VIDEOS[1], VIDEOS[2], VIDEOS[0]]
    assert served.player.index == 2  # still Song 0, now last
    assert client.queue == served.player.queue and client.index == 2
    assert client.errors == []


@pytest.mark.parametrize("source, target", [(0, 0), (2, 3), (-1, 0), (0, 5)])
def test_a_move_off_the_queue_sends_nothing(served, connect, source, target):
    served.player.queue[:] = VIDEOS
    client = connected(connect(served.url))
    until(lambda: client.queue == VIDEOS)
    assert client.move(source, target) is False
    assert served.posts() == []
    assert client.errors == []


def test_a_command_the_server_does_not_list_is_not_sent(served, connect):
    client = connected(connect(served.url))
    until(lambda: client.commands is not None)
    assert client.commands == set(server.COMMANDS)
    client.commands = client.commands - {"mute"}
    assert client.toggle_mute() is False
    assert client.errors == ["The server has no mute command"]
    assert served.posts() == []


def test_on_play_is_accepted_and_never_called(served, connect):
    played = []
    client = remote.RemoteClient(served.url, TOKEN, on_play=played.append)
    try:
        client.add(VIDEOS[0])
        client.play_current()
        until(lambda: served.player.queue == VIDEOS[:1])
        assert played == []
    finally:
        client.quit()


# --- AC2: state over /ws ----------------------------------------------------------------


def test_the_queue_index_and_idle_mirror_the_server(served, connect):
    client = connected(connect(served.url))
    assert client.queue == [] and client.idle is True
    assert client.status() == client.states[-1]
    assert "queue" not in client.status()
    with served.player.queue_lock:
        served.player.queue[:] = VIDEOS
    served.player.jump(2)  # as another remote would: the server broadcasts it
    until(lambda: client.index == 2)
    assert client.queue == VIDEOS
    assert client.idle is False
    assert client.states[-1]["title"] == "Song 2"


def test_a_track_the_server_could_not_play_reaches_the_status_and_the_queue_moves_on(served, connect):
    client = connected(connect(served.url))
    with served.player.queue_lock:
        served.player.queue[:] = VIDEOS
    served.player.jump(0)
    served.player.handle_message({"event": "end-file", "reason": "error", "file_error": "loading failed"})
    until(lambda: client.index == 1)
    assert client.status()["error"] == "Could not play Song 0: loading failed"


def test_it_reconnects_after_the_server_drops_the_socket(connect):
    first = Served()
    client = connected(connect(first.url))
    first.stop()
    second = Served(first.port)
    try:
        with second.player.queue_lock:
            second.player.queue[:] = VIDEOS
        until(lambda: client.queue == VIDEOS)  # the new server's first message, on the new socket
        second.player.jump(1)
        until(lambda: client.index == 1)
    finally:
        second.stop()


def test_quit_hangs_up_and_the_server_plays_on(served, connect):
    client = connected(connect(served.url))
    client.quit()
    assert not client.listener.is_alive()
    until(lambda: not client.poster.is_alive())
    assert ["quit"] not in served.player.sent


def test_quit_stops_posting_once_the_lookup_in_flight_returns(served, connect, monkeypatch):
    gate = threading.Event()

    def fetch(url):
        if url == VIDEOS[0].url:
            gate.wait(5)
        return [BY_URL[url]]

    monkeypatch.setattr(youtube, "fetch", fetch)
    client = connected(connect(served.url))
    for video in VIDEOS:
        client.add(video)
    client.play_current()  # v0 held at the gate; v1 and v2 wait behind it
    until(lambda: len(served.posts()) == 1)
    client.quit()
    gate.set()
    until(lambda: not client.poster.is_alive())
    assert served.posts() == [("/api/play", {"url": VIDEOS[0].url})]


# --- AC3: queue edits ---------------------------------------------------------------------


def test_play_current_makes_the_added_videos_the_servers_queue(served, connect):
    client = connected(connect(served.url))
    client.queue.clear()
    for video in VIDEOS:
        client.add(video)
    client.index = 0
    client.play_current()
    until(lambda: len(served.posts()) == 3)
    assert served.posts() == [
        ("/api/play", {"url": VIDEOS[0].url}),
        ("/api/queue", {"url": VIDEOS[1].url}),
        ("/api/queue", {"url": VIDEOS[2].url}),
    ]
    until(lambda: client.queue == VIDEOS)
    assert served.player.queue == VIDEOS
    assert served.player.index == 0
    assert served.player.sent[-1] == ["loadfile", VIDEOS[0].url]


def test_play_current_from_a_later_index_posts_from_there(served, connect):
    client = connected(connect(served.url))
    for video in VIDEOS:
        client.add(video)
    client.index = 1
    client.play_current()
    until(lambda: served.player.queue == VIDEOS[1:])
    assert [path for path, body in served.posts()] == ["/api/play", "/api/queue"]


def test_play_current_with_nothing_added_replays_the_current_row(served, connect):
    served.player.queue[:] = VIDEOS
    served.player.jump(2)
    client = connected(connect(served.url))
    until(lambda: client.index == 2)
    client.play_current()
    assert served.posts() == [("/api/command", {"name": "jump", "value": 2})]


def test_notify_appends_what_was_added(served, connect):
    served.player.queue[:] = VIDEOS[:1]
    served.player.jump(0)
    client = connected(connect(served.url))
    until(lambda: client.queue == VIDEOS[:1])
    client.add(VIDEOS[1])
    client.notify()
    assert client.states[-1] == client.status()  # the TUI redraws at once, from the mirror
    until(lambda: served.player.queue == VIDEOS[:2])
    assert served.posts() == [("/api/queue", {"url": VIDEOS[1].url})]
    assert served.player.index == 0


def test_a_newer_play_stops_an_older_one_still_posting(served, connect, monkeypatch):
    gate = threading.Event()
    slow = {VIDEOS[1].url}

    def fetch(url):
        if url in slow:
            gate.wait(5)
        return [BY_URL[url]]

    monkeypatch.setattr(youtube, "fetch", fetch)
    client = connected(connect(served.url))
    for video in VIDEOS:
        client.add(video)
    client.play_current()  # v0, then v1 (held at the gate), then v2
    until(lambda: len(served.posts()) == 2)
    client.queue.clear()
    client.add(VIDEOS[2])
    client.index = 0
    client.play_current()
    gate.set()
    until(lambda: len(served.posts()) == 3)
    until(lambda: served.player.queue == VIDEOS[2:])
    assert [body["url"] for path, body in served.posts()] == [VIDEOS[0].url, VIDEOS[1].url, VIDEOS[2].url]
    assert [path for path, body in served.posts()] == ["/api/play", "/api/queue", "/api/play"]


# --- AC5: errors -----------------------------------------------------------------------------


def test_a_wrong_token_is_reported_once_and_commands_say_so(served, connect):
    client = connect(served.url, token="wrong")
    until(lambda: client.errors)
    time.sleep(0.2)  # several retries
    assert client.errors == ["Server rejected the token"]
    assert client.next() is False
    assert client.errors[-1] == "Server rejected the token"


def test_an_unreachable_server_is_reported_once_and_retried(connect):
    url = f"http://127.0.0.1:{free_port()}"
    client = connect(url)
    until(lambda: client.errors)
    time.sleep(0.2)
    assert client.errors == [f"Server unreachable at {url}"]
    assert client.toggle_pause() is False
    assert client.errors[-1] == f"Server unreachable at {url}"


def test_a_server_started_later_is_found_by_the_retries(connect):
    port = free_port()
    client = connect(f"http://127.0.0.1:{port}")
    until(lambda: client.errors)
    late = Served(port)
    try:
        until(lambda: client.states)
        assert client.errors == [f"Server unreachable at http://127.0.0.1:{port}"]
    finally:
        late.stop()


def test_a_lookup_error_is_the_tuis_youtube_toast(served, connect, monkeypatch):
    def fetch(url):
        raise youtube.YouTubeError("Video unavailable\nmore detail")

    monkeypatch.setattr(youtube, "fetch", fetch)
    client = connected(connect(served.url))
    client.add(VIDEOS[0])
    client.play_current()
    until(lambda: client.errors)
    assert client.errors == ["YouTube lookup failed: Video unavailable"]


def test_other_refusals_show_the_servers_error_line(served, connect):
    client = connected(connect(served.url))
    client.commands = None  # as before the first connect: the server judges the name
    assert client.command("dance") is False
    assert client.errors == ["unknown command dance"]


def test_error_text_reads_the_json_error_line():
    class Reply:
        def __init__(self, data):
            self.data = data

        def read(self, *args):
            return self.data

    assert remote.error_text(Reply(json.dumps({"error": "boom"}).encode())) == "boom"
    assert remote.error_text(Reply(b"<html>")) == ""
