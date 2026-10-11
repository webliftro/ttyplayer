"""Last.fm scrobbling: the desktop login, now-playing and scrobbles, and a queue of the scrobbles not sent yet.

Every call is signed with the user's shared secret (Last.fm API 2.0). The session, with the key and secret it
belongs to, is kept in lastfm.json next to settings.toml, 0600; the queue holds only artist, track and timestamp.
"""

import hashlib
import json
import threading
import urllib.error
import urllib.parse
import urllib.request

from ttyplayer import lyrics
from ttyplayer.utils import data_path, read_entries, write_atomic

API_URL = "https://ws.audioscrobbler.com/2.0/"
AUTH_URL = "https://www.last.fm/api/auth/"
UNSIGNED = ("format", "callback")  # the two parameters api_sig leaves out
SCROBBLE_AFTER = 240  # seconds: a track is scrobbled after half its length, or this long when that comes first
SHORTEST = 30  # seconds: Last.fm takes no scrobble of a track this short or shorter
QUEUE_KEPT = 500  # the newest unsent scrobbles kept
BATCH = 50  # the most scrobbles one track.scrobble call takes
QUEUED_KEYS = ("artist", "track", "timestamp")  # all a queued scrobble keeps
LOGIN_AGAIN = "run ttyplayer lastfm login again"
SESSION_ERRORS = {4, 9, 14, 15}  # bad or unauthorized token, invalid session key, expired token
SESSION_KEYS = ("api_key", "secret", "session_key", "user")  # what lastfm.json holds
RETRY_LATER = {5}  # ignoredMessage codes worth sending again: the daily scrobble limit; 1-4 (artist, track, too old, too new) never pass

queue_lock = threading.Lock()  # one sender at a time: the now-playing worker and the scrobble timer both send the queue


class ScrobbleError(Exception):
    """Not logged in, Last.fm refused a call, or Last.fm unreachable; one line, for stderr or a toast."""


def session_path():
    """lastfm.json next to settings.toml: the session and shared secret are kept apart from the settings, 0600."""
    from ttyplayer import settings  # settings imports the player, which imports this module

    return settings.settings_path().parent / "lastfm.json"


def queue_path():
    return data_path("scrobble-queue.jsonl")


def sign(params, secret):
    """api_sig: the md5 of every parameter but format and callback as name + value, sorted by name, then the secret."""
    text = "".join(f"{name}{value}" for name, value in sorted(params.items()) if name not in UNSIGNED)
    return hashlib.md5((text + secret).encode("utf-8")).hexdigest()


def call(method, params, api_key, secret, session_key=None) -> dict:
    """POST one signed API method over HTTPS; its JSON reply, or a one-line ScrobbleError."""
    params = {**params, "method": method, "api_key": api_key} | ({"sk": session_key} if session_key else {})
    body = urllib.parse.urlencode({**params, "api_sig": sign(params, secret), "format": "json"}).encode("utf-8")
    request = urllib.request.Request(API_URL, data=body, headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            reply = json.load(response)
    except urllib.error.HTTPError as error:
        reply = error_reply(error)
    except (urllib.error.URLError, OSError, ValueError) as error:
        raise ScrobbleError(f"Cannot reach Last.fm: {getattr(error, 'reason', None) or error}") from error
    if not isinstance(reply, dict):
        raise ScrobbleError(f"Last.fm sent an unexpected reply to {method}")
    if "error" in reply:
        code = reply["error"]
        if code in SESSION_ERRORS:
            raise ScrobbleError(f"Last.fm did not accept the login ({reply.get('message', code)}); {LOGIN_AGAIN}")
        raise ScrobbleError(f"Last.fm answered error {code}: {reply.get('message') or 'no message'}")
    return reply


def error_reply(error):
    """Last.fm's {"error", "message"} body of an HTTP error, else the HTTP status as one."""
    try:
        reply = json.load(error)
    except (ValueError, OSError):
        reply = None
    return reply if isinstance(reply, dict) and "error" in reply else {"error": error.code, "message": error.reason}


def field(reply, *names):
    """reply[names[0]][names[1]]…, a non-empty string; a ScrobbleError when Last.fm's reply does not hold one."""
    for name in names:
        reply = reply.get(name) if isinstance(reply, dict) else None
    if not (isinstance(reply, str) and reply):
        raise ScrobbleError(f"Last.fm sent an unexpected reply (no {'.'.join(names)})")
    return reply


def login(api_key, secret, open_browser, echo=print, wait=input) -> str:
    """Get a token, let the user allow ttyplayer in the browser, then trade the token for a session and store it.

    Returns the Last.fm user name.
    """
    token = field(call("auth.getToken", {}, api_key, secret), "token")
    url = f"{AUTH_URL}?{urllib.parse.urlencode({'api_key': api_key, 'token': token})}"
    echo(f"Opening Last.fm in your browser; if it does not open, visit:\n{url}")
    open_browser(url)
    wait("Press Enter once you have allowed ttyplayer… ")
    reply = call("auth.getSession", {"token": token}, api_key, secret)
    name = field(reply, "session", "name")
    save_session({"api_key": api_key, "secret": secret, "session_key": field(reply, "session", "key"), "user": name})
    return name


def save_session(session):
    """A new lastfm.json in place of the old: write_atomic's temp file is 0600 from its creation (mkstemp), so the
    secret is never on disk under an older file's wider mode."""
    write_atomic(session_path(), json.dumps(session))


def load_session() -> dict:
    try:
        session = json.loads(session_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ScrobbleError("Not logged in to Last.fm: run ttyplayer lastfm login") from None
    except (OSError, ValueError) as error:
        raise ScrobbleError(f"Cannot read {session_path()}: {error}; {LOGIN_AGAIN}") from error
    if not (isinstance(session, dict) and all(isinstance(session.get(key), str) and session[key] for key in SESSION_KEYS)):
        raise ScrobbleError(f"{session_path()} is not a Last.fm login; {LOGIN_AGAIN}")
    return session


def logout() -> bool:
    """Forget the session and secret; False when there was none."""
    try:
        session_path().unlink()
    except FileNotFoundError:
        return False
    return True


def user():
    """The logged-in Last.fm user, or None."""
    try:
        return load_session()["user"]
    except ScrobbleError:
        return None


def active(settings) -> bool:
    """Whether a new player scrobbles: the scrobble setting is on and someone is logged in."""
    return settings.scrobble and session_path().exists()


def wanted(video, podcasts) -> bool:
    """Whether video is scrobbled: every track, a podcast episode only with scrobble_podcasts."""
    return podcasts or video.source != "podcast"


def threshold(duration):
    """Seconds video must play before it is scrobbled: half its duration, or SCROBBLE_AFTER when that comes first
    (Last.fm's rule; SCROBBLE_AFTER when the duration is unknown). None for a track Last.fm does not take."""
    if duration is None:
        return SCROBBLE_AFTER
    if duration <= SHORTEST:
        return None
    return min(SCROBBLE_AFTER, duration / 2)


def track(video) -> dict:
    """artist, track and (when known) duration: a podcast's show is its artist, anything else is lyrics.guess()ed."""
    artist, title = (video.uploader, video.title) if video.source == "podcast" else lyrics.guess(video.title, video.uploader)
    return {"artist": artist, "track": title} | ({"duration": round(video.duration)} if video.duration else {})


def now_playing(video):
    """Tell Last.fm video started; then send the queued scrobbles, now that it answers."""
    session = load_session()
    call("track.updateNowPlaying", track(video), session["api_key"], session["secret"], session["session_key"])
    send_queued()


def scrobble(video, started_at):
    """Scrobble video, which started at started_at (epoch seconds), after the queued scrobbles; what is not sent is queued."""
    send_queued([track(video) | {"timestamp": int(started_at)}])


def send_queued(extra=()):
    """Send the queued scrobbles and extra, BATCH per call, oldest first; a failure queues what was not sent and raises.

    Last.fm answers each scrobble: one it ignored for the daily limit stays queued, one it will never take is dropped;
    either is reported as one ScrobbleError once the rest is sent.
    """
    with queue_lock:
        entries = queued_entries() + list(extra)
        if not entries:
            return
        kept, ignored = [], []
        try:
            session = load_session()
            while entries:
                batch = entries[:BATCH]
                params = {f"{name}[{number}]": value for number, entry in enumerate(batch) for name, value in entry.items()}
                reply = call("track.scrobble", params, session["api_key"], session["secret"], session["session_key"])
                for entry, (code, message) in zip(batch, ignored_messages(reply, len(batch))):
                    if code:
                        ignored.append(message)
                        if code in RETRY_LATER:
                            kept.append(entry)
                entries = entries[BATCH:]
        finally:
            save_queue(kept + entries)
        if ignored:
            later = f"; {len(kept)} kept to send again" if kept else ""
            raise ScrobbleError(f"Last.fm ignored {len(ignored)} scrobble(s): {ignored[0]}{later}")


def ignored_messages(reply, count):
    """Last.fm's (code, message) for each of the count scrobbles of a track.scrobble reply, code 0 when it took it.

    The JSON reply holds one object, not a list, for a single scrobble. A ScrobbleError when it does not answer
    each scrobble with an ignoredMessage code that is a whole number (an int, or a string of ASCII digits; never a
    float or a bool), so the batch stays queued rather than lost.
    """
    unexpected = ScrobbleError("Last.fm sent an unexpected reply to track.scrobble")
    results = reply.get("scrobbles", {}).get("scrobble") if isinstance(reply.get("scrobbles"), dict) else None
    results = [results] if isinstance(results, dict) else results
    if not (isinstance(results, list) and len(results) == count and all(isinstance(result, dict) for result in results)):
        raise unexpected
    messages = []
    for result in results:
        message = result.get("ignoredMessage")
        code = message.get("code") if isinstance(message, dict) else None
        if isinstance(code, str) and code.isascii() and code.isdigit():
            try:
                code = int(code)
            except ValueError:  # more digits than Python's int conversion limit
                raise unexpected from None
        if type(code) is not int:  # not isinstance: a bool is an int
            raise unexpected
        messages.append((code, message.get("#text") or f"code {code}"))
    return messages


def queued_entries():
    try:
        entries = read_entries(queue_path(), key="timestamp")
    except OSError as error:
        raise ScrobbleError(f"Cannot read the Last.fm scrobble queue: {error.strerror or error}") from error
    return [entry for entry in entries if all(key in entry for key in QUEUED_KEYS)]


def save_queue(entries):
    """The unsent scrobbles, the newest QUEUE_KEPT of them, artist, track and timestamp only; no file when none."""
    lines = [json.dumps({key: entry[key] for key in QUEUED_KEYS}) + "\n" for entry in entries[-QUEUE_KEPT:]]
    try:
        if lines:
            write_atomic(queue_path(), "".join(lines))
        else:
            queue_path().unlink(missing_ok=True)
    except OSError as error:
        raise ScrobbleError(f"Cannot write the Last.fm scrobble queue: {error.strerror or error}") from error


def queued() -> int:
    """How many scrobbles wait to be sent."""
    return len(queued_entries())
