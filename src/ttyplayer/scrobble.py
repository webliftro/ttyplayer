"""Scrobbling to Last.fm and ListenBrainz: the logins, now-playing and scrobbles, and one queue of the scrobbles not sent yet.

Each target is a Target: LastFm signs every call with the user's shared secret (Last.fm API 2.0); ListenBrainz sends the
user's token. Each keeps its login in its own file next to settings.toml, 0600 (lastfm.json, listenbrainz.json); the
queue holds only artist, track and timestamp, and the target each scrobble is for.
"""

import hashlib
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from typing import Protocol

from ttyplayer import lyrics
from ttyplayer.utils import APP_NAME, app_version, data_path, read_entries, write_atomic

API_URL = "https://ws.audioscrobbler.com/2.0/"
AUTH_URL = "https://www.last.fm/api/auth/"
LISTENBRAINZ_URL = "https://api.listenbrainz.org/1/"
UNSIGNED = ("format", "callback")  # the two parameters api_sig leaves out
SCROBBLE_AFTER = 240  # seconds: a track is scrobbled after half its length, or this long when that comes first
SHORTEST = 30  # seconds: Last.fm takes no scrobble of a track this short or shorter
QUEUE_KEPT = 500  # the newest unsent scrobbles kept, per target
QUEUED_KEYS = ("artist", "track", "timestamp")  # all a queued scrobble keeps, with its target
DEFAULT_TARGET = "lastfm"  # the target of a queue line without one: the queue was Last.fm's alone before ListenBrainz
SESSION_ERRORS = {4, 9, 14, 15}  # bad or unauthorized token, invalid session key, expired token
RETRY_LATER = {5}  # ignoredMessage codes worth sending again: the daily scrobble limit; 1-4 (artist, track, too old, too new) never pass
LONGEST_WAIT = 60  # seconds: the longest ListenBrainz rate limit waited out on the worker; a longer one queues the scrobble

queue_lock = threading.Lock()  # one reader-and-writer of the queue file at a time, whichever target it is


class ScrobbleError(Exception):
    """Not logged in, a target refused a call, or it is unreachable; one line, for stderr or a toast."""


class Target(Protocol):
    """Where the player's hooks send what plays; every method raises a one-line ScrobbleError."""

    def now_playing(self, video): ...

    def scrobble(self, video, started_at): ...

    def send_queued(self, extra=()): ...


def queue_path():
    return data_path("scrobble-queue.jsonl")


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


class Account:
    """What every target shares: its login file next to settings.toml, and sending the queue.

    A subclass names itself (name, label, the keys its login file holds, the one that is the user) and sends a
    batch: post(batch, login, single) answers (entry, message, keep) for each scrobble the target ignored.
    """

    name = label = user_key = ""
    keys = ()
    batch = 1

    def __init__(self):
        self.lock = threading.Lock()  # one sender per target: the now-playing worker and the scrobble timer both send

    def login_again(self):
        return f"run {APP_NAME} {self.name} login again"

    def session_path(self):
        """<name>.json next to settings.toml: the login is kept apart from the settings, 0600."""
        from ttyplayer import settings  # settings imports the player, which imports this module

        return settings.settings_path().parent / f"{self.name}.json"

    def save_session(self, session):
        """A new login file in place of the old: write_atomic's temp file is 0600 from its creation (mkstemp), so the
        credential is never on disk under an older file's wider mode."""
        write_atomic(self.session_path(), json.dumps(session))

    def load_session(self) -> dict:
        path = self.session_path()
        try:
            session = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise ScrobbleError(f"Not logged in to {self.label}: run {APP_NAME} {self.name} login") from None
        except (OSError, ValueError) as error:
            raise ScrobbleError(f"Cannot read {path}: {error}; {self.login_again()}") from error
        if not (isinstance(session, dict) and all(isinstance(session.get(key), str) and session[key] for key in self.keys)):
            raise ScrobbleError(f"{path} is not a {self.label} login; {self.login_again()}")
        return session

    def logout(self) -> bool:
        """Forget the login; False when there was none."""
        try:
            self.session_path().unlink()
        except FileNotFoundError:
            return False
        return True

    def user(self):
        """The logged-in user, or None."""
        try:
            return self.load_session()[self.user_key]
        except ScrobbleError:
            return None

    def logged_in(self) -> bool:
        """Whether there is a login file; a broken one is reported by the first call that reads it."""
        return self.session_path().exists()

    def scrobble(self, video, started_at):
        """Scrobble video, which started at started_at (epoch seconds), after the queued scrobbles; what is not sent is queued."""
        self.send_queued([track(video) | {"timestamp": int(started_at)}])

    def send_queued(self, extra=()):
        """Send this target's queued scrobbles and extra, batch per call, oldest first; a failure queues what was not
        sent and raises. A scrobble the target ignored stays queued when post says to keep it, else it is dropped;
        either is reported as one ScrobbleError once the rest is sent."""
        with self.lock:
            calls = self.batches(queued_entries(self.name), [entry | {"target": self.name} for entry in extra])
            if not calls:
                return
            kept, ignored = [], []
            try:
                session = self.load_session()
                while calls:
                    batch, single = calls[0]
                    for entry, message, keep in self.post(batch, session, single):
                        ignored.append(message)
                        if keep:
                            kept.append(entry)
                    calls.pop(0)
            finally:
                replace_queued(self.name, kept + [entry for batch, _ in calls for entry in batch])
            if ignored:
                later = f"; {len(kept)} kept to send again" if kept else ""
                raise ScrobbleError(f"{self.label} ignored {len(ignored)} scrobble(s): {ignored[0]}{later}")

    def batches(self, queued, new) -> list:
        """The calls that send the queued scrobbles and the new ones, oldest first: (batch, single) for each, single when
        the batch is a new scrobble sent on its own. Every batch holds up to batch scrobbles, the new ones with the queued."""
        entries = queued + new
        return [(entries[start : start + self.batch], False) for start in range(0, len(entries), self.batch)]

    def post(self, batch, session, single):
        raise NotImplementedError


class LastFm(Account):
    """Last.fm API 2.0: a signed POST per call; the desktop login in the browser."""

    name, label, user_key = "lastfm", "Last.fm", "user"
    keys = ("api_key", "secret", "session_key", "user")  # what lastfm.json holds
    batch = 50  # the most scrobbles one track.scrobble call takes

    @staticmethod
    def sign(params, secret):
        """api_sig: the md5 of every parameter but format and callback as name + value, sorted by name, then the secret."""
        text = "".join(f"{name}{value}" for name, value in sorted(params.items()) if name not in UNSIGNED)
        return hashlib.md5((text + secret).encode("utf-8")).hexdigest()

    def call(self, method, params, api_key, secret, session_key=None) -> dict:
        """POST one signed API method over HTTPS; its JSON reply, or a one-line ScrobbleError."""
        params = {**params, "method": method, "api_key": api_key} | ({"sk": session_key} if session_key else {})
        body = urllib.parse.urlencode({**params, "api_sig": self.sign(params, secret), "format": "json"}).encode("utf-8")
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
                raise ScrobbleError(f"Last.fm did not accept the login ({reply.get('message', code)}); {self.login_again()}")
            raise ScrobbleError(f"Last.fm answered error {code}: {reply.get('message') or 'no message'}")
        return reply

    def signed(self, method, params, session):
        return self.call(method, params, session["api_key"], session["secret"], session["session_key"])

    def login(self, api_key, secret, open_browser, echo=print, wait=input) -> str:
        """Get a token, let the user allow ttyplayer in the browser, then trade the token for a session and store it.

        Returns the Last.fm user name.
        """
        token = field(self.call("auth.getToken", {}, api_key, secret), "token")
        url = f"{AUTH_URL}?{urllib.parse.urlencode({'api_key': api_key, 'token': token})}"
        echo(f"Opening Last.fm in your browser; if it does not open, visit:\n{url}")
        open_browser(url)
        wait("Press Enter once you have allowed ttyplayer… ")
        reply = self.call("auth.getSession", {"token": token}, api_key, secret)
        name = field(reply, "session", "name")
        self.save_session({"api_key": api_key, "secret": secret, "session_key": field(reply, "session", "key"), "user": name})
        return name

    def now_playing(self, video):
        """Tell Last.fm video started; then send the queued scrobbles, now that it answers."""
        self.signed("track.updateNowPlaying", track(video), self.load_session())
        self.send_queued()

    def post(self, batch, session, single):
        """One track.scrobble; Last.fm answers each scrobble: one ignored for the daily limit is kept."""
        params = {f"{name}[{number}]": value for number, entry in enumerate(batch) for name, value in entry.items() if name != "target"}
        reply = self.signed("track.scrobble", params, session)
        return [(entry, message, code in RETRY_LATER) for entry, (code, message) in zip(batch, ignored_messages(reply, len(batch))) if code]


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Follows no redirect: urllib would send the token header on to wherever it points, plain HTTP or another host.
    The redirect is raised as the HTTPError it is."""

    def redirect_request(self, *args):
        return None


class ListenBrainz(Account):
    """ListenBrainz API 1: JSON POSTs to submit-listens with the user's token; the login is the token, validated."""

    name, label, user_key = "listenbrainz", "ListenBrainz", "user_name"
    keys = ("token", "user_name")  # what listenbrainz.json holds
    batch = 1000  # the most listens one submit-listens call takes
    sleep = staticmethod(time.sleep)  # waits out a rate limit; tests swap it
    urlopen = staticmethod(urllib.request.build_opener(NoRedirect).open)  # tests swap it

    def request(self, endpoint, token, body=None) -> dict:
        """GET (or POST body as JSON) one endpoint over HTTPS with the token; its JSON object, or a one-line ScrobbleError.

        A 429 is waited out once, for X-RateLimit-Reset-In seconds when that is LONGEST_WAIT or less; a redirect is an
        HTTP error like any other (NoRedirect).
        """
        headers = {"Authorization": f"Token {token}"} | ({"Content-Type": "application/json"} if body is not None else {})
        data = json.dumps(body).encode("utf-8") if body is not None else None
        for attempt in range(2):
            request = urllib.request.Request(LISTENBRAINZ_URL + endpoint, data=data, headers=headers)
            try:
                with self.urlopen(request, timeout=10) as response:
                    reply = json.load(response)
                break
            except urllib.error.HTTPError as error:
                wait = rate_limit_wait(error)
                if attempt == 0 and wait is not None:
                    self.sleep(wait)
                    continue
                raise ScrobbleError(self.refusal(error)) from None
            except (urllib.error.URLError, OSError) as error:
                raise ScrobbleError(f"Cannot reach ListenBrainz: {getattr(error, 'reason', None) or error}") from error
            except ValueError:
                reply = None
                break
        if not isinstance(reply, dict):
            raise ScrobbleError(f"ListenBrainz sent an unexpected reply to {endpoint}")
        return reply

    def refusal(self, error):
        """The one line for an HTTP error: ListenBrainz's {"code", "error"} message, else the HTTP reason."""
        try:
            reply = json.load(error)
        except (ValueError, OSError):
            reply = None
        message = reply.get("error") if isinstance(reply, dict) else None
        line = f"ListenBrainz answered HTTP {error.code}: {message if isinstance(message, str) and message else error.reason}"
        line = " ".join(line.split())
        return f"{line}; {self.login_again()}" if error.code == 401 else line

    def login(self, token) -> str:
        """Validate the token and store it with its user; returns the user name."""
        reply = self.request("validate-token", token)
        name = reply.get("user_name")
        if reply.get("valid") is not True or not (isinstance(name, str) and name):
            raise ScrobbleError("ListenBrainz did not accept the token; copy it again from listenbrainz.org/settings")
        self.save_session({"token": token, "user_name": name})
        return name

    def submit(self, listen_type, listens, session):
        reply = self.request("submit-listens", session["token"], {"listen_type": listen_type, "payload": listens})
        if reply.get("status") != "ok":
            raise ScrobbleError("ListenBrainz sent an unexpected reply to submit-listens")

    def now_playing(self, video):
        """Tell ListenBrainz video started; then send the queued scrobbles, now that it answers."""
        self.submit("playing_now", [listen(track(video))], self.load_session())
        self.send_queued()

    def batches(self, queued, new) -> list:
        """The queued scrobbles go as imports of up to batch, then each new one as a single listen."""
        return super().batches(queued, []) + [([entry], True) for entry in new]

    def post(self, batch, session, single):
        """One submit-listens: a new scrobble as a single listen, queued ones as an import; ListenBrainz takes all or none."""
        self.submit("single" if single else "import", [listen(entry) for entry in batch], session)
        return []


def listen(entry) -> dict:
    """A ListenBrainz listen of a track() entry: listened_at from its timestamp, absent for playing_now."""
    info = {"media_player": APP_NAME, "submission_client": APP_NAME, "submission_client_version": app_version()}
    info |= {"duration_ms": entry["duration"] * 1000} if entry.get("duration") else {}
    metadata = {"artist_name": entry["artist"], "track_name": entry["track"], "additional_info": info}
    return ({"listened_at": entry["timestamp"]} if "timestamp" in entry else {}) | {"track_metadata": metadata}


def rate_limit_wait(error):
    """Seconds to wait before trying a 429 again: its X-RateLimit-Reset-In, when that is 0 to LONGEST_WAIT; else None."""
    if error.code != 429:
        return None
    try:
        seconds = float((error.headers or {}).get("X-RateLimit-Reset-In", ""))
    except ValueError:
        return None
    return seconds if 0 <= seconds <= LONGEST_WAIT else None


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


LASTFM = LastFm()
LISTENBRAINZ = ListenBrainz()
TARGETS = (LASTFM, LISTENBRAINZ)


def targets() -> list:
    """The targets someone is logged in to; the player sends each what plays."""
    return [target for target in TARGETS if target.logged_in()]


def active(settings) -> bool:
    """Whether a new player scrobbles: the scrobble setting is on and someone is logged in to a target."""
    return settings.scrobble and bool(targets())


def queued_entries(target=None):
    """The queued scrobbles, each with its target (a line without one is Last.fm's); only target's when given."""
    with queue_lock:
        return read_queue(target)


def read_queue(target=None):
    try:
        entries = read_entries(queue_path(), key="timestamp")
    except OSError as error:
        raise ScrobbleError(f"Cannot read the scrobble queue: {error.strerror or error}") from error
    entries = [{"target": DEFAULT_TARGET} | entry for entry in entries if all(key in entry for key in QUEUED_KEYS)]
    return [entry for entry in entries if target in (None, entry["target"])]


def replace_queued(target, entries):
    """target's queued scrobbles become entries; the other targets' are kept as they are."""
    with queue_lock:
        save_queue([entry for entry in read_queue() if entry["target"] != target] + entries)


def save_queue(entries):
    """The unsent scrobbles, the newest QUEUE_KEPT per target, artist, track, timestamp and target only; no file when none."""
    counted = Counter()
    lines = []
    for entry in reversed(entries):
        entry = {"target": DEFAULT_TARGET} | entry
        counted[entry["target"]] += 1
        if counted[entry["target"]] <= QUEUE_KEPT:
            lines.append(json.dumps({key: entry[key] for key in (*QUEUED_KEYS, "target")}) + "\n")
    lines.reverse()
    try:
        if lines:
            write_atomic(queue_path(), "".join(lines))
        else:
            queue_path().unlink(missing_ok=True)
    except OSError as error:
        raise ScrobbleError(f"Cannot write the scrobble queue: {error.strerror or error}") from error


def queued(target=None) -> int:
    """How many scrobbles wait to be sent; only target's when given."""
    return len(queued_entries(target))
