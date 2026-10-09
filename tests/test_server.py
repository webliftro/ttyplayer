import asyncio
import json
import socket
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict

import pytest
from aiohttp import WSMsgType
from aiohttp.test_utils import TestClient, TestServer

from ttyplayer import control, playlists, server, settings, youtube
from ttyplayer.models import Video

TOKEN = "secret-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
VIDEOS = [Video(id=f"v{n}", title=f"Song {n}", uploader="u", duration=60) for n in range(3)]


class FakeClient:
    """Stands in for MpvClient: the queue, a status, and a log of every call; never runs mpv."""

    def __init__(self):
        self.queue = []
        self.index = 0
        self.idle = True
        self.queue_lock = threading.RLock()
        self.calls = []

    def status(self):
        return {"title": self.queue[self.index].title if self.queue else None, "index": self.index + 1,
                "total": len(self.queue), "idle": self.idle}

    def queue_listing(self):
        return {"videos": [asdict(video) for video in self.queue], "index": self.index + 1}

    def handle_control(self, name):
        self.calls.append(name)
        return control.ok()

    def seek(self, seconds):
        self.calls.append(("seek", seconds))

    def change_volume(self, step):
        self.calls.append(("volume", step))

    def play_current(self):
        self.idle = False
        self.calls.append(("play", self.index))

    def jump(self, index):
        self.index = index
        self.idle = False
        self.calls.append(("play", index))
        return True

    def notify(self):
        self.calls.append("notify")


@pytest.fixture
def fake():
    return FakeClient()


@pytest.fixture
def settings_file(monkeypatch, tmp_path):
    path = tmp_path / "settings.toml"
    monkeypatch.setattr(settings, "settings_path", lambda: path)
    return path


def current(**values):
    return settings.Settings(server_token=TOKEN, **values)


def with_http(app, test):
    """Run test(http) against app on a test server, in a fresh event loop; its result."""

    async def go():
        async with TestClient(TestServer(app)) as http:
            return await test(http)

    return asyncio.run(go())


def request(app, method, path, **kwargs):
    """(status, JSON body) of one request."""

    async def test(http):
        response = await http.request(method, path, **kwargs)
        return response.status, await response.json()

    return with_http(app, test)


def api(fake, method, path, settings_=None, **kwargs):
    return request(server.make_app(fake, settings_ or current()), method, path, headers=AUTH, **kwargs)


# --- the token ------------------------------------------------------------


@pytest.mark.parametrize(
    "path, headers",
    [
        ("/api/status", {}),
        ("/api/status", {"Authorization": "Bearer wrong"}),
        ("/api/status", {"Authorization": TOKEN}),
        ("/api/status?token=wrong", {}),
        ("/api/settings", {}),
        ("/ws", {}),
        ("/api/nothing-here", {}),
    ],
)
def test_api_and_socket_without_the_token_are_401(fake, path, headers):
    assert request(server.make_app(fake, current()), "GET", path, headers=headers) == (401, {"error": "unauthorized"})
    assert fake.calls == []


def test_an_empty_token_lets_nobody_in(fake):
    app = server.make_app(fake, settings.Settings())
    assert request(app, "GET", "/api/status", headers={"Authorization": "Bearer "}) == (401, {"error": "unauthorized"})


def test_the_token_works_as_a_header_or_in_the_query(fake):
    async def test(http):
        by_header = await http.get("/api/status", headers=AUTH)
        by_query = await http.get(f"/api/status?token={TOKEN}")
        return by_header.status, by_query.status

    assert with_http(server.make_app(fake, current()), test) == (200, 200)


def test_the_page_and_static_files_need_no_token(fake):
    async def test(http):
        page = await http.get("/")
        static = await http.get("/static/index.html")
        return page.status, await page.text(), static.status

    status, text, static_status = with_http(server.make_app(fake, current()), test)
    assert (status, static_status) == (200, 200)
    assert "the web remote arrives in the next release" in text.lower()
    assert TOKEN not in text


def test_ensure_token_generates_and_saves_a_token_once(tmp_path):
    path = tmp_path / "settings.toml"
    first = server.ensure_token(settings.load(path), path)
    assert len(first.server_token) >= 32
    assert settings.load(path).server_token == first.server_token
    assert server.ensure_token(settings.load(path), path) == first


def test_ensure_token_keeps_a_saved_token_and_writes_nothing(tmp_path):
    path = tmp_path / "settings.toml"
    assert server.ensure_token(current(), path).server_token == TOKEN
    assert not path.exists()


# --- the API ----------------------------------------------------------------


def test_status_adds_the_queue(fake):
    fake.queue[:] = VIDEOS[:2]
    status, body = api(fake, "GET", "/api/status")
    assert status == 200
    assert body["queue"] == [asdict(video) for video in VIDEOS[:2]]
    assert body["index"] == 1
    assert body["total"] == 2


@pytest.mark.parametrize("name", ["pause", "next", "prev", "stop", "mute"])
def test_command_reuses_handle_control(fake, name):
    status, body = api(fake, "POST", "/api/command", json={"name": name})
    assert status == 200
    assert fake.calls == [name]
    assert body == server.full_status(fake)


@pytest.mark.parametrize("name, value, call", [("seek", -5, ("seek", -5)), ("volume", 2.5, ("volume", 2.5))])
def test_command_with_a_value_calls_the_player(fake, name, value, call):
    assert api(fake, "POST", "/api/command", json={"name": name, "value": value})[0] == 200
    assert fake.calls == [call]


@pytest.mark.parametrize(
    "body, error",
    [
        ({"name": "dance"}, "unknown command dance"),
        ({}, 'a command needs a "name"'),
        ({"name": []}, 'a command needs a "name"'),
        ({"name": {}}, 'a command needs a "name"'),
        ({"name": "seek"}, "seek needs a numeric value"),
        ({"name": "volume", "value": "5"}, "volume needs a numeric value"),
        ({"name": "seek", "value": True}, "seek needs a numeric value"),
    ],
)
def test_bad_commands_are_400(fake, body, error):
    assert api(fake, "POST", "/api/command", json=body) == (400, {"error": error})
    assert fake.calls == []


@pytest.mark.parametrize("data", ["not json", "[1, 2]"])
def test_a_body_that_is_not_a_json_object_is_400(fake, data):
    status, body = api(fake, "POST", "/api/command", data=data)
    assert status == 400
    assert body["error"].startswith("the body must be")


def test_an_unknown_api_path_is_a_json_404(fake):
    assert api(fake, "GET", "/api/nothing-here") == (404, {"error": "Not Found"})


def test_play_resolves_a_query_on_a_worker_thread_and_plays_its_first_result(monkeypatch, fake):
    calls = []

    def search(query, limit=5):
        calls.append((query, limit, threading.current_thread() is threading.main_thread()))
        return VIDEOS

    monkeypatch.setattr(youtube, "search", search)
    fake.queue[:] = [Video(id="old", title="Old", uploader="u", duration=1)]
    fake.index = 0
    status, body = api(fake, "POST", "/api/play", current(search_limit=7), json={"query": "lofi"})
    assert status == 200
    assert calls == [("lofi", 7, False)]  # off the loop's (here the main) thread
    assert fake.queue == VIDEOS[:1]
    assert fake.calls == [("play", 0)]
    assert body["title"] == "Song 0"


def test_play_a_url_replaces_the_queue_with_every_video(monkeypatch, fake):
    monkeypatch.setattr(youtube, "fetch", lambda url: VIDEOS if url == "https://x" else [])
    fake.queue[:] = [Video(id="old", title="Old", uploader="u", duration=1)]
    fake.index = 0
    assert api(fake, "POST", "/api/play", json={"url": "https://x"})[0] == 200
    assert fake.queue == VIDEOS
    assert fake.calls == [("play", 0)]


def test_queue_appends_and_plays_when_idle(monkeypatch, fake):
    monkeypatch.setattr(youtube, "fetch", lambda url: VIDEOS[1:])
    fake.queue[:] = VIDEOS[:1]
    status, body = api(fake, "POST", "/api/queue", json={"url": "https://x"})
    assert status == 200
    assert fake.queue == VIDEOS
    assert fake.calls == [("play", 1)]
    assert [video["id"] for video in body["queue"]] == ["v0", "v1", "v2"]


def test_queue_appends_a_whole_search_while_playing(monkeypatch, fake):
    monkeypatch.setattr(youtube, "search", lambda query, limit=5: VIDEOS[1:])
    fake.queue[:] = VIDEOS[:1]
    fake.idle = False
    assert api(fake, "POST", "/api/queue", json={"query": "more"})[0] == 200
    assert fake.queue == VIDEOS
    assert fake.calls == ["notify"]


@pytest.mark.parametrize("path", ["/api/play", "/api/queue"])
def test_play_and_queue_need_a_url_or_a_query(fake, path):
    assert api(fake, "POST", path, json={"q": "x"}) == (400, {"error": 'the body needs a "url" or a "query"'})


def test_play_with_no_results_is_404(monkeypatch, fake):
    monkeypatch.setattr(youtube, "search", lambda query, limit=5: [])
    assert api(fake, "POST", "/api/play", json={"query": "nothing"}) == (404, {"error": "No videos found"})
    assert fake.calls == []


def test_youtube_errors_are_502_in_one_line(monkeypatch, fake):
    def fail(*args):
        raise youtube.YouTubeError("no internet\nmore detail")

    monkeypatch.setattr(youtube, "search", fail)
    assert api(fake, "POST", "/api/play", json={"query": "x"}) == (502, {"error": "no internet"})
    assert api(fake, "GET", "/api/search?q=x") == (502, {"error": "no internet"})


def test_search_lists_videos_with_the_search_limit(monkeypatch, fake):
    calls = []

    def search(query, limit=5):
        calls.append((query, limit))
        return VIDEOS

    monkeypatch.setattr(youtube, "search", search)
    assert api(fake, "GET", "/api/search?q=lofi+beats", current(search_limit=3)) == (
        200, [asdict(video) for video in VIDEOS]
    )
    assert calls == [("lofi beats", 3)]
    assert api(fake, "GET", "/api/search?q=") == (400, {"error": "search needs ?q="})


@pytest.fixture
def playlist_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(playlists, "playlists_dir", lambda: tmp_path / "playlists")
    playlists.create("Road")
    playlists.add("Road", VIDEOS)
    playlists.create("Empty")


def test_playlists_lists_names_and_counts(fake, playlist_dir):
    assert api(fake, "GET", "/api/playlists") == (200, [{"name": "Empty", "count": 0}, {"name": "Road", "count": 3}])


def test_playing_a_playlist_makes_it_the_queue(fake, playlist_dir):
    status, body = api(fake, "POST", "/api/playlists/Road/play")
    assert status == 200
    assert fake.queue == VIDEOS
    assert fake.calls == [("play", 0)]
    assert body["total"] == 3


def test_playing_a_missing_or_empty_playlist_is_404(fake, playlist_dir):
    assert api(fake, "POST", "/api/playlists/Nope/play") == (404, {"error": "No playlist named Nope"})
    assert api(fake, "POST", "/api/playlists/Empty/play") == (404, {"error": "No videos found"})
    assert fake.calls == []


def test_settings_never_show_the_token(fake):
    status, body = api(fake, "GET", "/api/settings")
    assert status == 200
    assert body == {key: getattr(current(), key) for key in settings.KEYS if key != "server_token"}
    assert TOKEN not in json.dumps(body)


def test_patch_settings_validates_and_saves(fake, settings_file):
    settings.save(current())
    status, body = api(fake, "PATCH", "/api/settings", json={"search_limit": 25, "show_clock": False, "theme": "nord"})
    assert status == 200
    assert (body["search_limit"], body["show_clock"], body["theme"]) == (25, False, "nord")
    assert settings.load() == current(search_limit=25, show_clock=False, theme="nord")


@pytest.mark.parametrize(
    "body, error",
    [
        ({"search_limit": 99}, "search_limit must be between 1 and 50, not 99"),
        ({"volume": 3}, "Unknown setting 'volume'"),
        ({"server_token": "mine"}, "server_token cannot be changed here"),
        ({"theme": "nord", "show_clock": "maybe"}, "show_clock must be true or false"),
    ],
)
def test_patch_settings_rejects_bad_values_and_saves_nothing(fake, settings_file, body, error):
    status, reply = api(fake, "PATCH", "/api/settings", json=body)
    assert status == 400
    assert reply["error"].startswith(error)
    assert not settings_file.exists()


# --- the socket ----------------------------------------------------------


def test_socket_sends_the_status_on_connect_and_broadcasts_on_state(fake):
    hub = server.Broadcaster()
    fake.queue[:] = VIDEOS[:1]

    async def test(http):
        ws = await http.ws_connect(f"/ws?token={TOKEN}")
        first = await ws.receive_json(timeout=2)
        feeder = threading.Thread(target=hub, args=({"title": "from the player"},))  # a player thread
        feeder.start()
        feeder.join()
        second = await ws.receive_json(timeout=2)
        await ws.close()
        return first, second

    first, second = with_http(server.make_app(fake, current(), hub), test)
    assert first == server.full_status(fake)
    assert second == {"title": "from the player"}


def test_socket_commands_reply_with_the_status_or_an_error(fake):
    async def test(http):
        ws = await http.ws_connect("/ws", headers=AUTH)
        await ws.receive_json(timeout=2)
        replies = []
        texts = ['{"name": "next"}', '{"name": "volume", "value": 5}', '{"name": "dance"}', "nope", "[1]",
                 '{"name": []}', '{"name": {}}', '{"name": "pause"}']
        for text in texts:
            await ws.send_str(text)
            replies.append(await ws.receive_json(timeout=2))
        await ws.close()
        return replies

    replies = with_http(server.make_app(fake, current()), test)
    assert replies[:2] == [server.full_status(fake)] * 2
    assert replies[2:7] == [
        {"error": "unknown command dance"},
        {"error": "a command must be JSON"},
        {"error": "a command must be a JSON object"},
        {"error": 'a command needs a "name"'},
        {"error": 'a command needs a "name"'},
    ]
    assert replies[7] == server.full_status(fake)  # the socket still works after the errors
    assert fake.calls == ["next", ("volume", 5), "pause"]


def test_a_closed_socket_is_pruned_and_the_others_still_hear(fake):
    hub = server.Broadcaster()

    async def test(http):
        gone = await http.ws_connect(f"/ws?token={TOKEN}")
        stays = await http.ws_connect(f"/ws?token={TOKEN}")
        await gone.receive_json(timeout=2)
        await stays.receive_json(timeout=2)
        await gone.close()
        for _ in range(50):  # until the server has seen the close
            if len(hub.sockets) == 1:
                break
            await asyncio.sleep(0.01)
        count = len(hub.sockets)
        hub({"title": "still here"})
        heard = await stays.receive_json(timeout=2)
        await stays.close()
        return count, heard

    count_after_close, heard = with_http(server.make_app(fake, current(), hub), test)
    assert count_after_close == 1
    assert heard == {"title": "still here"}


class BrokenSocket:
    closed = False

    async def send_str(self, text):
        raise ConnectionResetError("gone")


class ClosedSocket:
    closed = True

    async def send_str(self, text):
        pytest.fail("sent to a closed socket")


def test_broadcast_prunes_closed_and_broken_sockets():
    hub = server.Broadcaster()

    async def test():
        hub.loop = asyncio.get_running_loop()
        hub.sockets.update({BrokenSocket(), ClosedSocket()})
        hub({"title": "x"})
        for _ in range(5):
            await asyncio.sleep(0)

    asyncio.run(test())
    assert hub.sockets == set()


def test_broadcast_before_the_server_runs_does_nothing():
    server.Broadcaster()({"title": "x"})  # no loop yet: dropped, no error


# --- the thread -------------------------------------------------------------


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_server_thread_serves_until_stopped(fake):
    port = free_port()
    hub = server.Broadcaster()
    web = server.ServerThread(server.make_app(fake, current(), hub), "127.0.0.1", port)
    try:
        assert web.thread.is_alive()
        assert hub.loop is not None
        reply = urllib.request.urlopen(
            urllib.request.Request(f"http://127.0.0.1:{port}/api/status", headers=AUTH), timeout=5
        )
        assert json.load(reply) == server.full_status(fake)
    finally:
        web.stop()
    assert not web.thread.is_alive()
    assert hub.loop is None
    hub({"title": "after the stop"})  # a late status from the player is dropped
    with pytest.raises(urllib.error.URLError):
        urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1)


def test_server_thread_on_a_port_in_use_raises_oserror(fake):
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        with pytest.raises(OSError):
            server.ServerThread(server.make_app(fake, current()), "127.0.0.1", taken.getsockname()[1])


def test_url_names_the_host_or_the_lan_address_for_a_wildcard():
    assert server.url("127.0.0.1", 7700, "t") == "http://127.0.0.1:7700/?token=t"
    assert not server.url("0.0.0.0", 7700, "t").startswith("http://0.0.0.0")



@pytest.mark.parametrize("host, address", [("::1", "[::1]"), ("fe80::1", "[fe80::1]"), ("myhost", "myhost")])
def test_url_brackets_an_ipv6_host(host, address):
    parsed = urllib.parse.urlsplit(server.url(host, 7700, "t"))
    assert parsed.netloc == f"{address}:7700"
    assert parsed.port == 7700


@pytest.mark.parametrize("token", ["a&b", "a+b c", "x=y#z", "%41"])
def test_url_carries_any_token_back_unchanged(token):
    query = urllib.parse.urlsplit(server.url("127.0.0.1", 7700, token)).query
    assert urllib.parse.parse_qs(query) == {"token": [token]}
