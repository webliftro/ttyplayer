import asyncio
import json
import re
import shutil
import socket
import subprocess
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict
from html.parser import HTMLParser
from pathlib import Path

import pytest
import aiohttp
from aiohttp import WSMsgType
from aiohttp.test_utils import TestClient, TestServer

from ttyplayer import control, favorites, playlists, server, settings, youtube
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

    def remove(self, index):
        self.calls.append(("remove", index))
        return True

    def clear_others(self):
        self.calls.append("clear_others")
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


@pytest.mark.parametrize(
    "path, content_type",
    [
        ("/", "text/html"),
        ("/static/index.html", "text/html"),
        ("/static/remote.js", "text/javascript"),
        ("/static/remote.css", "text/css"),
        ("/static/icon.svg", "image/svg+xml"),
        ("/manifest.webmanifest", "application/manifest+json"),
    ],
)
def test_the_page_and_static_files_need_no_token(fake, path, content_type):
    async def test(http):
        response = await http.get(path)
        return response.status, response.content_type, await response.text()

    status, served_type, text = with_http(server.make_app(fake, current()), test)
    assert (status, served_type) == (200, content_type)
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


@pytest.mark.parametrize(
    "name, value, call",
    [("seek", -5, ("seek", -5)), ("volume", 2.5, ("volume", 2.5)), ("jump", 1, ("play", 1)), ("remove", 0, ("remove", 0))],
)
def test_command_with_a_value_calls_the_player(fake, name, value, call):
    fake.queue[:] = VIDEOS
    assert api(fake, "POST", "/api/command", json={"name": name, "value": value})[0] == 200
    assert fake.calls == [call]


def test_clear_others_keeps_only_the_current_track(fake):
    status, body = api(fake, "POST", "/api/command", json={"name": "clear_others"})
    assert status == 200
    assert fake.calls == ["clear_others"]
    assert body == server.full_status(fake)


def test_commands_lists_the_command_table(fake):
    assert api(fake, "GET", "/api/commands") == (200, server.COMMANDS)


# A value each command takes, so every name in the table is exercised.
COMMAND_VALUES = {"seek": 5, "volume": -5, "jump": 0, "remove": 0}


@pytest.mark.parametrize("name", server.COMMANDS)
def test_every_listed_command_is_accepted(fake, name):
    fake.queue[:] = VIDEOS
    body = {"name": name, "value": COMMAND_VALUES[name]} if name in COMMAND_VALUES else {"name": name}
    assert api(fake, "POST", "/api/command", json=body)[0] == 200
    assert len(fake.calls) == 1


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
        ({"name": "jump"}, "jump needs a queue row number"),
        ({"name": "jump", "value": 1.5}, "jump needs a queue row number"),
        ({"name": "remove", "value": False}, "remove needs a queue row number"),
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


@pytest.fixture
def favorites_file(monkeypatch, tmp_path):
    path = tmp_path / "favorites.jsonl"
    monkeypatch.setattr(favorites, "favorites_path", lambda: path)
    return path


def test_favorites_lists_the_newest_first(fake, favorites_file):
    assert api(fake, "GET", "/api/favorites") == (200, [])
    favorites.add(VIDEOS[0])
    favorites.add(VIDEOS[1])
    assert api(fake, "GET", "/api/favorites") == (200, [asdict(VIDEOS[1]), asdict(VIDEOS[0])])


def test_posting_a_favorite_toggles_it(fake, favorites_file):
    video = asdict(VIDEOS[1])
    assert api(fake, "POST", "/api/favorites/v1", json=video) == (200, [video])
    assert favorites.load() == [VIDEOS[1]]
    assert api(fake, "POST", "/api/favorites/v1") == (200, [])  # no body needed to unfavorite
    assert favorites.load() == []


def test_a_favorite_keeps_the_id_of_its_path(fake, favorites_file):
    status, body = api(fake, "POST", "/api/favorites/v2", json={"id": "other", "title": "Song 2", "uploader": "u"})
    assert status == 200
    assert [video["id"] for video in body] == ["v2"]


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
    assert second == {"title": "from the player", "queue": first["queue"]}  # the first broadcast carries it


def test_a_broadcast_carries_the_queue_only_when_it_changed(fake):
    hub = server.Broadcaster()
    fake.queue[:] = VIDEOS[:2]
    fake.idle = False

    async def test(http):
        ws = await http.ws_connect(f"/ws?token={TOKEN}")
        await ws.receive_json(timeout=2)
        heard = []
        for queue in (VIDEOS[:2], VIDEOS[:2], [VIDEOS[0], VIDEOS[2]]):  # last: same length, index and title
            fake.queue[:] = queue
            hub(fake.status())
            heard.append(await ws.receive_json(timeout=2))
        await ws.close()
        return heard

    first, unchanged, replaced = with_http(server.make_app(fake, current(), hub), test)
    assert first["queue"] == [asdict(video) for video in VIDEOS[:2]]
    assert "queue" not in unchanged
    assert replaced["queue"] == [asdict(VIDEOS[0]), asdict(VIDEOS[2])]


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
    assert heard == {"title": "still here", "queue": []}


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


def test_stopping_the_server_closes_the_open_sockets(fake):
    port = free_port()
    web = server.ServerThread(server.make_app(fake, current()), "127.0.0.1", port)

    async def listen():
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(f"http://127.0.0.1:{port}/ws?token={TOKEN}") as ws:
                await ws.receive_json(timeout=2)
                stopping = asyncio.get_running_loop().run_in_executor(None, web.stop)
                closing = await ws.receive(timeout=2)
                await stopping  # raises what stop() raised
                return closing.type, closing.data

    assert asyncio.run(listen()) == (WSMsgType.CLOSE, aiohttp.WSCloseCode.GOING_AWAY)  # the page reconnects


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


# --- the page -------------------------------------------------------------


class PageParser(HTMLParser):
    """The ids, the <link>/<script>/<meta> tags and any inline handlers or scripts of a page."""

    def __init__(self):
        super().__init__()
        self.ids, self.links, self.scripts, self.metas, self.handlers, self.inline = set(), {}, [], {}, [], []
        self.in_script = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "id" in attrs:
            self.ids.add(attrs["id"])
        self.handlers += [name for name in attrs if name.startswith("on")]
        if tag == "link":
            self.links[attrs["rel"]] = attrs["href"]
        elif tag == "script":
            self.scripts.append(attrs.get("src"))
            self.in_script = True
        elif tag == "meta" and "name" in attrs:
            self.metas[attrs["name"]] = attrs["content"]

    def handle_endtag(self, tag):
        if tag == "script":
            self.in_script = False

    def handle_data(self, data):
        if self.in_script and data.strip():
            self.inline.append(data)


def page():
    parser = PageParser()
    parser.feed((server.STATIC_DIR / "index.html").read_text(encoding="utf-8"))
    return parser


def remote_js():
    return (server.STATIC_DIR / "remote.js").read_text(encoding="utf-8")


def test_the_page_has_every_element_the_script_reads():
    used = set(re.findall(r'\$\("([\w-]+)"\)', remote_js()))
    assert used <= page().ids
    assert {"now-playing", "np-title", "np-uploader", "np-progress", "np-elapsed", "np-duration", "np-state",
            "np-position", "volume", "mute", "prev", "play-pause", "next", "search-form", "search-input",
            "search-results", "queue", "queue-list", "queue-clear", "favorites", "favorites-list", "playlists",
            "playlists-list", "banner", "token-form", "token-input", "connection"} <= page().ids


def test_the_page_links_its_script_style_and_manifest_and_nothing_inline():
    parsed = page()
    assert parsed.scripts == ["/static/remote.js"]
    assert parsed.links["stylesheet"] == "/static/remote.css"
    assert parsed.links["manifest"] == "/manifest.webmanifest"
    assert "width=device-width" in parsed.metas["viewport"]
    assert parsed.handlers == []
    assert parsed.inline == []


def test_the_page_sends_only_commands_the_server_knows():
    used = set(re.findall(r'command\("(\w+)"', remote_js()))
    assert used == {"pause", "next", "prev", "mute", "volume", "jump", "remove", "clear_others"}
    assert used <= set(server.COMMANDS)


def run_page(messages):
    """remote.js run by Node on a stub DOM, fed messages over /ws: the queue rows it shows after each.

    Each row is its title, with a leading "▸" when it is marked as playing.
    """
    node = shutil.which("node")
    if node is None:
        pytest.skip("needs Node to run the page's script")
    harness = Path(__file__).with_name("remote_page.mjs")
    result = subprocess.run([node, str(harness), str(server.STATIC_DIR / "remote.js")], input=json.dumps(messages),
                            capture_output=True, encoding="utf-8", timeout=30, check=True)
    return json.loads(result.stdout)


def status(queue, index, idle=False):
    """A status as the server broadcasts it; queue=None leaves the queue out."""
    message = {"title": VIDEOS[index - 1].title, "index": index, "total": 2, "idle": idle}
    if queue is not None:
        message["queue"] = [asdict(video) for video in queue]
    return message


def test_the_page_shows_a_queue_replaced_by_another_remote():
    shown = run_page([status(VIDEOS[:2], 1), status([VIDEOS[0], VIDEOS[2]], 1)])
    assert shown == [["▸Song 0", "Song 1"], ["▸Song 0", "Song 2"]]


def test_the_page_moves_and_drops_the_playing_mark_without_a_queue():
    shown = run_page([status(VIDEOS[:2], 1), status(None, 2), status(None, 2, idle=True)])
    assert shown == [["▸Song 0", "Song 1"], ["Song 0", "▸Song 1"], ["Song 0", "Song 1"]]


def test_the_manifest_makes_an_installable_app():
    manifest = json.loads((server.STATIC_DIR / "manifest.webmanifest").read_text(encoding="utf-8"))
    assert manifest["display"] == "standalone"
    assert {"name", "start_url", "theme_color", "background_color"} <= manifest.keys()
    assert {icon["sizes"] for icon in manifest["icons"]} == {"192x192", "512x512"}
    for icon in manifest["icons"]:
        assert (server.STATIC_DIR / icon["src"].removeprefix("/static/")).is_file()


def test_the_style_takes_its_colors_from_variables_and_fills_the_screen():
    css = (server.STATIC_DIR / "remote.css").read_text(encoding="utf-8")
    rules_outside_root = re.sub(r":root\s*\{[^}]*\}", "", css)
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", rules_outside_root)
    assert "prefers-color-scheme: dark" in css
    assert "100dvh" in css
