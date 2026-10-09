"""Spotify playlists as metadata: PKCE login, the user's playlists, a playlist's tracks resolved on YouTube.

No Spotify audio is ever touched; every track is searched on YouTube as "<first artist> <title>".
"""

import base64
import email.utils
import hashlib
import itertools
import json
import os
import re
import math
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer

from ttyplayer import playlists, settings, youtube

AUTHORIZE_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
API_URL = "https://api.spotify.com/v1"
PORT = 8765
REDIRECT_URI = f"http://127.0.0.1:{PORT}/callback"  # registered in the user's Spotify app (README)
SCOPES = "playlist-read-private playlist-read-collaborative"
LOGIN_TIMEOUT = 300  # seconds the listener waits for the browser to come back
EXPIRY_MARGIN = 60  # refresh this many seconds before the access token runs out
LOGIN_AGAIN = "Run ttyplayer spotify login again"
# open.spotify.com/playlist/<id>?si=…, open.spotify.com/intl-de/playlist/<id>, spotify:playlist:<id>, or <id>
PLAYLIST_REF = re.compile(
    r"(?:(?:https?://)?open\.spotify\.com/(?:[\w-]+/)?playlist/|spotify:playlist:)?([A-Za-z0-9]{22})(?:[/?#].*)?"
)

sleep = time.sleep  # tests swap it out for 429 handling


class SpotifyError(Exception):
    """Not logged in, a refused login or refresh, a bad playlist reference, or Spotify unreachable."""


class SpotifyRefused(SpotifyError):
    """Spotify answered with an HTTP error."""


class RateLimited(SpotifyRefused):
    def __init__(self, retry_after):
        super().__init__("429 Too Many Requests")
        self.retry_after = retry_after


@dataclass(frozen=True)
class Track:
    artist: str
    title: str

    @property
    def query(self):
        return f"{self.artist} {self.title}"

    @property
    def label(self):
        return f"{self.artist} – {self.title}"


def token_path():
    """spotify.json next to settings.toml: the tokens are kept apart from the settings, 0600."""
    return settings.settings_path().parent / "spotify.json"


def pkce_pair():
    """(verifier, challenge): a random code verifier and its S256 challenge, both base64url without padding."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def authorize_url(client_id, challenge, state):
    query = urllib.parse.urlencode({
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "code_challenge_method": "S256",
        "code_challenge": challenge,
        "state": state,
        "scope": SCOPES,
    })
    return f"{AUTHORIZE_URL}?{query}"


def login(client_id, echo) -> str:
    """Send the user to Spotify's consent page, take the code the browser brings back, store the tokens.

    Returns the user's display name.
    """
    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(16)
    with listen() as server:
        url = authorize_url(client_id, challenge, state)
        echo(f"Opening Spotify in your browser; if it does not open, visit:\n{url}")
        webbrowser.open(url)
        code = receive_code(server, state)
    tokens = request_tokens(client_id, {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "code_verifier": verifier,
    })
    save_tokens(tokens)
    me = _get("/me")
    return me.get("display_name") or me.get("id") or "Unknown"


class CallbackServer(HTTPServer):
    params = None  # the /callback query, once the browser came back
    timed_out = False

    def handle_timeout(self):
        self.timed_out = True


class CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path != "/callback":
            self.send_error(404)
            return
        self.server.params = dict(urllib.parse.parse_qsl(parsed.query))
        body = b"ttyplayer: you can close this tab and go back to the terminal.\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass  # the code is in the request line; nothing is logged


def listen(port=PORT):
    """The one-shot callback listener on 127.0.0.1:port, bound and ready."""
    try:
        return CallbackServer(("127.0.0.1", port), CallbackHandler)
    except OSError as error:
        raise SpotifyError(f"Cannot listen on 127.0.0.1:{port} for the login: {error.strerror or error}") from error


def receive_code(server, state, timeout=LOGIN_TIMEOUT):
    """The authorization code from the browser's /callback request; other paths are answered 404 and ignored."""
    server.timeout = timeout
    while server.params is None:
        server.handle_request()
        if server.timed_out:
            raise SpotifyError(f"No answer from the browser in {timeout} s; {LOGIN_AGAIN}")
    params = server.params
    if params.get("state") != state:
        raise SpotifyError(f"The login answer did not match this login; {LOGIN_AGAIN}")
    if "code" not in params:
        raise SpotifyError(f"Spotify refused the login: {params.get('error', 'no code')}")
    return params["code"]


def request_tokens(client_id, form, old=None) -> dict:
    """POST form to the token endpoint; the stored token record (client id, tokens, expires_at)."""
    body = urllib.parse.urlencode({**form, "client_id": client_id}).encode("ascii")
    request = urllib.request.Request(
        TOKEN_URL, data=body, headers={"Content-Type": "application/x-www-form-urlencoded"}
    )
    reply = _send(request)
    return {
        "client_id": client_id,
        "access_token": reply["access_token"],
        "refresh_token": reply.get("refresh_token") or (old or {}).get("refresh_token"),
        "expires_at": time.time() + int(reply.get("expires_in", 3600)),
    }


def save_tokens(tokens):
    path = token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as f:
        json.dump(tokens, f)
    os.chmod(path, 0o600)  # an older file keeps its mode through O_CREAT


def load_tokens() -> dict:
    try:
        return json.loads(token_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SpotifyError("Not logged in to Spotify: run ttyplayer spotify login") from None
    except (OSError, ValueError) as error:
        raise SpotifyError(f"Cannot read {token_path()}: {error}; {LOGIN_AGAIN}") from error


def access_token() -> str:
    """The stored access token, refreshed and saved first when it has run out."""
    tokens = load_tokens()
    if tokens["expires_at"] - EXPIRY_MARGIN > time.time():
        return tokens["access_token"]
    form = {"grant_type": "refresh_token", "refresh_token": tokens.get("refresh_token") or ""}
    try:
        fresh = request_tokens(tokens["client_id"], form, old=tokens)
    except SpotifyError as error:
        reason = str(error).removesuffix(f". {LOGIN_AGAIN}")  # a 401 already says it
        raise SpotifyError(f"Cannot refresh the Spotify login ({reason}). {LOGIN_AGAIN}") from error
    save_tokens(fresh)
    return fresh["access_token"]


def _get(path):
    """GET an API path (/me) or a full paging URL with the access token, as JSON."""
    url = path if path.startswith("https://") else API_URL + path
    return _send(urllib.request.Request(url, headers={"Authorization": f"Bearer {access_token()}"}))


def _send(request) -> dict:
    """Send a request to Spotify (API or token endpoint); its JSON reply, or a one-line SpotifyError.

    A 429 waits its Retry-After once and sends again; a second 429 fails.
    """
    try:
        return _open(request)
    except RateLimited as error:
        sleep(error.retry_after)
    try:
        return _open(request)
    except RateLimited as error:
        raise SpotifyError("Spotify is limiting requests; try again in a minute") from error


def _open(request) -> dict:
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        if error.code == 429:
            raise RateLimited(retry_after(error.headers.get("Retry-After"))) from error
        if error.code == 401:
            raise SpotifyRefused(f"Spotify did not accept the login. {LOGIN_AGAIN}") from error
        raise SpotifyRefused(f"Spotify answered {error.code}: {_reason(error)}") from error
    except (urllib.error.URLError, OSError, ValueError) as error:
        raise SpotifyError(f"Cannot reach Spotify: {getattr(error, 'reason', None) or error}") from error


def retry_after(value):
    """Seconds to wait from a Retry-After header: delay-seconds or an HTTP date; 1 when absent or unreadable."""
    value = (value or "").strip()
    if value.isdigit():
        return int(value)
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return 1
    return max(0, math.ceil(when.timestamp() - time.time()))


def _reason(error):
    """The message in Spotify's error body, {"error": {"message"}} or {"error_description"}, else the HTTP reason."""
    try:
        body = json.load(error)
    except (ValueError, OSError):
        return error.reason
    detail = body.get("error")
    if isinstance(detail, dict):
        return detail.get("message") or error.reason
    return body.get("error_description") or detail or error.reason


def _pages(path):
    """Every item of a paged listing, following next; lazily, so a caller that stops early fetches no more pages."""
    while path:
        page = _get(path)
        yield from page.get("items") or []
        path = page.get("next")


def user_playlists() -> list[tuple[str, int, str]]:
    """(name, track count, id) for every playlist the user owns or follows."""
    return [
        (item.get("name") or "", (item.get("tracks") or {}).get("total", 0), item["id"])
        for item in _pages("/me/playlists?limit=50")
        if item
    ]


def playlist_id(ref):
    match = PLAYLIST_REF.fullmatch(ref.strip())
    if not match:
        raise SpotifyError(f"Not a Spotify playlist link or id: {ref}")
    return match.group(1)


def playlist(ref, limit=None) -> tuple[str, list[Track]]:
    """The playlist's name and its tracks (at most limit), local files and podcast episodes skipped."""
    spotify_id = playlist_id(ref)
    name = _get(f"/playlists/{spotify_id}?fields=name").get("name") or ""
    tracks = (track for item in _pages(f"/playlists/{spotify_id}/tracks?limit=100") if (track := playable(item)))
    return name, list(itertools.islice(tracks, limit))


def playable(item):
    """A Track for a playlist item that is a Spotify track, else None (a local file, an episode, a gone track)."""
    track = (item or {}).get("track")
    if not track or track.get("is_local") or track.get("type") != "track" or not track.get("name"):
        return None
    artists = track.get("artists") or [{}]
    return Track(artist=artists[0].get("name") or "", title=track["name"])


def import_tracks(tracks, name, echo) -> int:
    """Search each track on YouTube and append the first hit to playlist name (created if missing), one line per track.

    Ends with the "Saved N of M" line and returns N. A YouTubeError stops it after that line; what was added stays added.
    """
    if not playlists.playlist_path(name).exists():
        playlists.create(name)
    saved = 0
    try:
        for number, track in enumerate(tracks, start=1):
            prefix = f"[{number}/{len(tracks)}]"
            found = youtube.search(track.query.strip(), 1)
            if found:
                saved += playlists.add(name, found[:1])
                echo(f"{prefix} ✓ {track.label} → {found[0].title}")
            else:
                echo(f"{prefix} ✗ {track.label} not found")
    finally:
        echo(f"Saved {saved} of {len(tracks)} tracks to {name}")
    return saved
