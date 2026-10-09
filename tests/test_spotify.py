import base64
import email.utils
import hashlib
import http.client
import io
import json
import re
import sys
import threading
import time
import urllib.error
import urllib.parse

import pytest

from conftest import posix_only
from ttyplayer import playlists, spotify, youtube
from ttyplayer.models import Video
from ttyplayer.spotify import SpotifyError, Track

API = spotify.API_URL
PLAYLIST_ID = "37i9dQZF1DXcBWIGoYBM5M"


@pytest.fixture
def homes(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    return tmp_path


class FakeSpotify:
    """urlopen for spotify: answers by URL from queued replies (a dict is JSON, an int is an HTTP error)."""

    def __init__(self, monkeypatch):
        self.replies = {}
        self.requests = []
        monkeypatch.setattr(spotify.urllib.request, "urlopen", self)

    def on(self, url, *replies):
        self.replies.setdefault(url, []).extend(replies)

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        queued = self.replies.get(request.full_url)
        if not queued:
            raise AssertionError(f"unexpected request {request.full_url}")
        reply = queued.pop(0)
        if isinstance(reply, Exception):
            raise reply
        if isinstance(reply, tuple):
            code, headers, body = reply
            raise urllib.error.HTTPError(request.full_url, code, "Error", headers, io.BytesIO(json.dumps(body).encode()))
        return io.BytesIO(json.dumps(reply).encode())

    def urls(self):
        return [request.full_url for request in self.requests]


@pytest.fixture
def fake(monkeypatch):
    return FakeSpotify(monkeypatch)


def logged_in(expires_in=3600, refresh_token="R1"):
    spotify.save_tokens({
        "client_id": "cid", "access_token": "A1", "refresh_token": refresh_token, "expires_at": time.time() + expires_in,
    })


def bearer(request):
    return request.get_header("Authorization")


def form(request):
    return dict(urllib.parse.parse_qsl(request.data.decode()))


def test_pkce_verifier_and_challenge_have_the_rfc_7636_shape():
    verifier, challenge = spotify.pkce_pair()
    assert re.fullmatch(r"[A-Za-z0-9._~-]{43,128}", verifier)
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert challenge == expected
    assert "=" not in challenge
    assert spotify.pkce_pair()[0] != verifier


def test_authorize_url_asks_for_a_pkce_code_with_read_only_playlist_scopes():
    url = spotify.authorize_url("cid", "chal", "st")
    query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
    assert url.startswith("https://accounts.spotify.com/authorize?")
    assert query == {
        "client_id": "cid", "response_type": "code", "redirect_uri": "http://127.0.0.1:8765/callback",
        "code_challenge_method": "S256", "code_challenge": "chal", "state": "st",
        "scope": "playlist-read-private playlist-read-collaborative",
    }


def browse(server, *paths):
    """Request each path from the callback listener in a thread, the way a browser would; the statuses."""
    statuses = []

    def run():
        for path in paths:
            connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
            connection.request("GET", path)
            statuses.append(connection.getresponse().status)
            connection.close()

    thread = threading.Thread(target=run)
    thread.start()
    return thread, statuses


def test_the_callback_listener_ignores_other_paths_and_returns_the_code():
    with spotify.listen(0) as server:
        thread, statuses = browse(server, "/favicon.ico", "/callback?code=abc&state=st")
        assert spotify.receive_code(server, "st") == "abc"
        thread.join()
    assert statuses == [404, 200]


@pytest.mark.parametrize(
    "query, message",
    [
        ("code=abc&state=other", "The login answer did not match this login; Run ttyplayer spotify login again"),
        ("error=access_denied&state=st", "Spotify refused the login: access_denied"),
    ],
)
def test_the_callback_listener_rejects_a_wrong_state_or_a_refusal(query, message):
    with spotify.listen(0) as server:
        thread, _ = browse(server, f"/callback?{query}")
        with pytest.raises(SpotifyError) as caught:
            spotify.receive_code(server, "st")
        thread.join()
    assert str(caught.value) == message


def test_the_callback_listener_gives_up_after_its_timeout():
    with spotify.listen(0) as server:
        with pytest.raises(SpotifyError, match="No answer from the browser"):
            spotify.receive_code(server, "st", timeout=0.05)


def test_listen_on_a_busy_port_is_one_line():
    with spotify.listen(0) as busy:
        with pytest.raises(SpotifyError, match=r"^Cannot listen on 127.0.0.1:\d+ for the login: "):
            spotify.listen(busy.server_address[1])


def test_the_callback_listener_reuses_addresses_only_off_windows():
    assert spotify.CallbackServer.allow_reuse_address is (sys.platform != "win32")


def test_login_exchanges_the_code_with_the_verifier_and_stores_the_tokens(homes, fake, monkeypatch):
    server = spotify.listen(0)
    monkeypatch.setattr(spotify, "listen", lambda: server)
    opened, threads = [], []

    def open_browser(url):
        opened.append(url)
        state = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))["state"]
        threads.append(browse(server, f"/callback?code=C0DE&state={state}")[0])

    monkeypatch.setattr(spotify.webbrowser, "open", open_browser)
    fake.on(spotify.TOKEN_URL, {"access_token": "A1", "refresh_token": "R1", "expires_in": 3600})
    fake.on(f"{API}/me", {"display_name": "Ana", "id": "ana1"})
    echoed = []

    assert spotify.login("cid", echoed.append) == "Ana"
    threads[0].join()

    assert echoed == [f"Opening Spotify in your browser; if it does not open, visit:\n{opened[0]}"]
    token_request, me_request = fake.requests
    challenge = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(opened[0]).query))["code_challenge"]
    sent = form(token_request)
    assert sent["grant_type"] == "authorization_code"
    assert sent["code"] == "C0DE"
    assert sent["client_id"] == "cid"
    assert sent["redirect_uri"] == spotify.REDIRECT_URI
    assert base64.urlsafe_b64encode(hashlib.sha256(sent["code_verifier"].encode()).digest()).rstrip(b"=").decode() == challenge
    assert bearer(me_request) == "Bearer A1"
    stored = json.loads(spotify.token_path().read_text())
    assert stored["client_id"] == "cid"
    assert stored["access_token"] == "A1"
    assert stored["refresh_token"] == "R1"
    assert stored["expires_at"] > time.time() + 3000
    assert spotify.token_path() == homes / "config" / "ttyplayer" / "spotify.json"


@posix_only
def test_the_token_file_is_private_even_when_it_existed(homes):
    spotify.token_path().parent.mkdir(parents=True)
    spotify.token_path().write_text("{}")
    spotify.token_path().chmod(0o644)
    logged_in()
    assert spotify.token_path().stat().st_mode & 0o777 == 0o600


def test_api_calls_use_the_stored_token(homes, fake):
    logged_in()
    fake.on(f"{API}/me", {"id": "ana1"})
    assert spotify._get("/me") == {"id": "ana1"}
    assert bearer(fake.requests[0]) == "Bearer A1"


def test_an_expired_token_is_refreshed_and_saved_and_the_refresh_token_kept(homes, fake):
    logged_in(expires_in=-10)
    fake.on(spotify.TOKEN_URL, {"access_token": "A2", "expires_in": 3600})
    fake.on(f"{API}/me", {"id": "ana1"})
    spotify._get("/me")
    refresh, me = fake.requests
    assert form(refresh) == {"grant_type": "refresh_token", "refresh_token": "R1", "client_id": "cid"}
    assert bearer(me) == "Bearer A2"
    stored = json.loads(spotify.token_path().read_text())
    assert (stored["access_token"], stored["refresh_token"]) == ("A2", "R1")


def test_a_failed_refresh_says_to_log_in_again(homes, fake):
    logged_in(expires_in=-10)
    fake.on(spotify.TOKEN_URL, (400, {}, {"error": "invalid_grant", "error_description": "Refresh token revoked"}))
    with pytest.raises(SpotifyError) as caught:
        spotify._get("/me")
    assert str(caught.value) == (
        "Cannot refresh the Spotify login (Spotify answered 400: Refresh token revoked). Run ttyplayer spotify login again"
    )


def test_a_refresh_without_network_says_to_log_in_again(homes, fake):
    logged_in(expires_in=-10)
    fake.on(spotify.TOKEN_URL, urllib.error.URLError("offline"))
    with pytest.raises(SpotifyError) as caught:
        spotify._get("/me")
    assert str(caught.value) == "Cannot refresh the Spotify login (Cannot reach Spotify: offline). Run ttyplayer spotify login again"


def test_a_refresh_refused_with_401_says_to_log_in_again_once(homes, fake):
    logged_in(expires_in=-10)
    fake.on(spotify.TOKEN_URL, (401, {}, {}))
    with pytest.raises(SpotifyError) as caught:
        spotify._get("/me")
    assert str(caught.value) == (
        "Cannot refresh the Spotify login (Spotify did not accept the login). Run ttyplayer spotify login again"
    )


def test_without_a_login_it_says_to_log_in(homes, fake):
    with pytest.raises(SpotifyError) as caught:
        spotify._get("/me")
    assert str(caught.value) == "Not logged in to Spotify: run ttyplayer spotify login"
    assert fake.requests == []


def test_no_network_is_one_line(homes, fake):
    logged_in()
    fake.on(f"{API}/me", urllib.error.URLError("Name or service not known"))
    with pytest.raises(SpotifyError) as caught:
        spotify._get("/me")
    assert str(caught.value) == "Cannot reach Spotify: Name or service not known"


def test_an_api_error_carries_spotifys_message(homes, fake):
    logged_in()
    fake.on(f"{API}/playlists/{PLAYLIST_ID}?fields=name", (404, {}, {"error": {"status": 404, "message": "Resource not found"}}))
    with pytest.raises(SpotifyError) as caught:
        spotify.playlist(PLAYLIST_ID)
    assert str(caught.value) == "Spotify answered 404: Resource not found"


def test_a_429_waits_retry_after_once_then_retries(homes, fake, monkeypatch):
    slept = []
    monkeypatch.setattr(spotify, "sleep", slept.append)
    logged_in()
    fake.on(f"{API}/me", (429, {"Retry-After": "3"}, {}), {"id": "ana1"})
    assert spotify._get("/me") == {"id": "ana1"}
    assert slept == [3]


def test_a_429_with_an_http_date_waits_until_that_date(homes, fake, monkeypatch):
    slept = []
    monkeypatch.setattr(spotify, "sleep", slept.append)
    logged_in()
    now = int(time.time())
    monkeypatch.setattr(spotify.time, "time", lambda: now)
    fake.on(f"{API}/me", (429, {"Retry-After": email.utils.formatdate(now + 7, usegmt=True)}, {}), {"id": "a"})
    assert spotify._get("/me") == {"id": "a"}
    assert slept == [7]


@pytest.mark.parametrize(
    ("value", "seconds"),
    [("4", 4), (" 4 ", 4), ("Thu, 15 Jan 2027 08:00:05 GMT", 5), ("Thu, 15 Jan 2027 07:59:00 GMT", 0),
     (None, 1), ("", 1), ("soon", 1)],
)
def test_retry_after_reads_seconds_or_an_http_date(monkeypatch, value, seconds):
    monkeypatch.setattr(spotify.time, "time", lambda: email.utils.parsedate_to_datetime("Thu, 15 Jan 2027 08:00:00 GMT").timestamp())
    assert spotify.retry_after(value) == seconds


def test_a_429_without_retry_after_waits_one_second(homes, fake, monkeypatch):
    slept = []
    monkeypatch.setattr(spotify, "sleep", slept.append)
    logged_in()
    fake.on(f"{API}/me", (429, {}, {}), {"id": "b"})
    assert spotify._get("/me") == {"id": "b"}
    assert slept == [1]


def test_a_429_from_the_token_endpoint_waits_and_retries(homes, fake, monkeypatch):
    slept = []
    monkeypatch.setattr(spotify, "sleep", slept.append)
    logged_in(expires_in=-10)
    fake.on(spotify.TOKEN_URL, (429, {"Retry-After": "2"}, {}), {"access_token": "A2", "expires_in": 3600})
    fake.on(f"{API}/me", {"id": "ana1"})
    assert spotify._get("/me") == {"id": "ana1"}
    assert slept == [2]
    first, second, me = fake.requests
    assert form(first) == form(second)
    assert bearer(me) == "Bearer A2"


def test_a_second_429_from_the_token_endpoint_fails_in_one_line(homes, fake, monkeypatch):
    slept = []
    monkeypatch.setattr(spotify, "sleep", slept.append)
    logged_in(expires_in=-10)
    fake.on(spotify.TOKEN_URL, (429, {"Retry-After": "2"}, {}), (429, {"Retry-After": "2"}, {}))
    with pytest.raises(SpotifyError) as caught:
        spotify._get("/me")
    assert str(caught.value) == (
        "Cannot refresh the Spotify login (Spotify is limiting requests; try again in a minute). Run ttyplayer spotify login again"
    )
    assert slept == [2]


def test_a_second_429_fails_in_one_line(homes, fake, monkeypatch):
    slept = []
    monkeypatch.setattr(spotify, "sleep", slept.append)
    logged_in()
    fake.on(f"{API}/me", (429, {"Retry-After": "2"}, {}), (429, {"Retry-After": "2"}, {}))
    with pytest.raises(SpotifyError) as caught:
        spotify._get("/me")
    assert str(caught.value) == "Spotify is limiting requests; try again in a minute"
    assert slept == [2]


def test_user_playlists_pages_through_every_page(homes, fake):
    logged_in()
    second = f"{API}/me/playlists?offset=50&limit=50"
    fake.on(f"{API}/me/playlists?limit=50", {
        "items": [{"name": "Road", "id": "p1", "tracks": {"total": 12}}], "next": second,
    })
    fake.on(second, {"items": [{"name": "Chill", "id": "p2", "tracks": {"total": 3}}], "next": None})
    assert spotify.user_playlists() == [("Road", 12, "p1"), ("Chill", 3, "p2")]
    assert fake.urls() == [f"{API}/me/playlists?limit=50", second]


@pytest.mark.parametrize(
    "ref",
    [
        f"https://open.spotify.com/playlist/{PLAYLIST_ID}",
        f"https://open.spotify.com/playlist/{PLAYLIST_ID}?si=abc123",
        f"open.spotify.com/intl-de/playlist/{PLAYLIST_ID}",
        f"spotify:playlist:{PLAYLIST_ID}",
        PLAYLIST_ID,
        f"  {PLAYLIST_ID}\n",
    ],
)
def test_playlist_id_takes_a_link_a_uri_or_a_bare_id(ref):
    assert spotify.playlist_id(ref) == PLAYLIST_ID


@pytest.mark.parametrize(
    "ref",
    ["", "abc", f"https://open.spotify.com/album/{PLAYLIST_ID}", f"spotify:track:{PLAYLIST_ID}", f"{PLAYLIST_ID}x"],
)
def test_playlist_id_rejects_anything_else(ref):
    with pytest.raises(SpotifyError) as caught:
        spotify.playlist_id(ref)
    assert str(caught.value) == f"Not a Spotify playlist link or id: {ref}"


def item(title, artist="Artist", **track):
    return {"track": {"name": title, "artists": [{"name": artist}, {"name": "Feat"}], "is_local": False, "type": "track", **track}}


def serve_playlist(fake, *pages):
    """The playlist's name call and its track pages, each page linked to the next."""
    fake.on(f"{API}/playlists/{PLAYLIST_ID}?fields=name", {"name": "Road Trip"})
    urls = [f"{API}/playlists/{PLAYLIST_ID}/tracks?limit=100"] + [
        f"{API}/playlists/{PLAYLIST_ID}/tracks?offset={100 * n}&limit=100" for n in range(1, len(pages))
    ]
    for url, next_url, items in zip(urls, urls[1:] + [None], pages):
        fake.on(url, {"items": items, "next": next_url})
    return urls


def test_playlist_pages_its_tracks_skipping_local_files_episodes_and_gone_tracks(homes, fake):
    logged_in()
    urls = serve_playlist(
        fake,
        [item("One", "Ann"), item("Mine", is_local=True), item("Podcast", type="episode"), {"track": None}],
        [item("Two", "Bob")],
    )
    assert spotify.playlist(f"spotify:playlist:{PLAYLIST_ID}") == ("Road Trip", [Track("Ann", "One"), Track("Bob", "Two")])
    assert fake.urls()[1:] == urls


def test_playlist_with_a_limit_stops_fetching_pages(homes, fake):
    logged_in()
    serve_playlist(fake, [item("One"), item("Two")], [item("Three")])
    assert spotify.playlist(PLAYLIST_ID, limit=2) == ("Road Trip", [Track("Artist", "One"), Track("Artist", "Two")])
    assert len(fake.requests) == 2


def video(video_id, title):
    return Video(id=video_id, title=title, uploader="u", duration=60)


def fake_search(monkeypatch, answers):
    """youtube.search answering from answers by query: a list of videos, or an exception to raise."""
    queries = []

    def search(query, limit, source):  # no default: import_tracks names the source itself
        queries.append((query, limit, source))
        answer = answers[query]
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(youtube, "search", search)
    return queries


def ids(name):
    return [found.id for found in playlists.load(name)]


def test_import_tracks_searches_each_track_and_saves_the_first_hit(homes, monkeypatch):
    queries = fake_search(monkeypatch, {
        "Ann One": [video("v1", "Ann - One (Official)"), video("v9", "other")],
        "Bob Two": [],
        "Cy Three": [video("v3", "Three")],
    })
    lines = []
    tracks = [Track("Ann", "One"), Track("Bob", "Two"), Track("Cy", "Three")]
    assert spotify.import_tracks(tracks, "Road Trip", lines.append) == 2
    assert queries == [("Ann One", 1, "youtube"), ("Bob Two", 1, "youtube"), ("Cy Three", 1, "youtube")]
    assert lines == [
        "[1/3] ✓ Ann – One → Ann - One (Official)",
        "[2/3] ✗ Bob – Two not found",
        "[3/3] ✓ Cy – Three → Three",
        "Saved 2 of 3 tracks to Road Trip",
    ]
    assert ids("Road Trip") == ["v1", "v3"]


def test_import_tracks_appends_to_an_existing_playlist(homes, monkeypatch):
    playlists.create("road")
    playlists.add("road", [video("old", "Old")])
    fake_search(monkeypatch, {"Ann One": [video("v1", "One")]})
    spotify.import_tracks([Track("Ann", "One")], "road", lambda line: None)
    assert ids("road") == ["old", "v1"]


def test_import_tracks_keeps_what_was_added_and_sums_up_when_youtube_fails(homes, monkeypatch):
    fake_search(monkeypatch, {"Ann One": [video("v1", "One")], "Bob Two": youtube.YouTubeError("no internet")})
    lines = []
    with pytest.raises(youtube.YouTubeError):
        spotify.import_tracks([Track("Ann", "One"), Track("Bob", "Two"), Track("Cy", "Three")], "road", lines.append)
    assert lines == ["[1/3] ✓ Ann – One → One", "Saved 1 of 3 tracks to road"]
    assert ids("road") == ["v1"]
