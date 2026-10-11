"""ttyplayer selfcheck: every live integration tried once, each a named check run with a timeout.

doctor checks the install without the network; these reach YouTube, SoundCloud, iTunes, LRCLIB, Last.fm,
ListenBrainz and Spotify, serve the API on a spare port and play a second of silence through mpv. Every check
calls the product's own functions, passed in as defaults so tests swap in fakes. The checks only read: logins are
used, never changed.
"""

import dataclasses
import io
import os
import secrets
import tempfile
import threading
import time
import urllib.error
import urllib.request
import wave
from collections import Counter

from ttyplayer import art, cli, favorites, history, lyrics, player, playlists, podcasts, positions, scrobble, server
from ttyplayer import settings, spotify, subscriptions, youtube
from ttyplayer.models import Video
from ttyplayer.utils import APP_NAME, data_path, parse_lines

STATUSES = tuple(cli.MARKS)  # ok, warn, fail, skip
TIMEOUT = 10  # seconds one check may take
BUDGET = 120  # seconds the whole run may take; the checks left after it are skipped
PLAYBACK_WAIT = 3  # seconds mpv may take from loading the silent WAV to its playback-restart
QUICK_SKIP = "skipped with --quick"
QUERY = "ttyplayer selfcheck"  # the YouTube search; its first result is also the stream and the thumbnail checked
SOUNDCLOUD_QUERY = "lofi"
PODCAST_QUERY = "npr news"
LYRICS_TRACK = ("Queen", "Bohemian Rhapsody")  # a track LRCLIB has synced lyrics for
LOCALHOST = "127.0.0.1"
# The server check talks to this machine only: a proxy from the environment must not see the request.
direct_open = urllib.request.build_opener(urllib.request.ProxyHandler({})).open


@dataclasses.dataclass(frozen=True)
class Result:
    name: str
    status: str  # one of STATUSES
    detail: str  # one line
    seconds: float


class CheckFailed(Exception):
    """A check's one-line reason to fail."""


def run(checks, quick=False, timeout=TIMEOUT, budget=BUDGET, clock=time.monotonic) -> list[Result]:
    """A Result per (name, check) in checks, in order; never raises.

    Each check(quick) runs on a worker thread for at most timeout seconds and answers (status, detail). One that
    raises fails with the exception's first line; one still running at its timeout fails and is left to its daemon
    thread. Once budget seconds have gone by, the checks left are skipped.
    """
    began = clock()
    results = []
    for name, check in checks:
        left = budget - (clock() - began)
        if left <= 0:
            results.append(Result(name, "skip", f"no time left of the {budget:g} s budget", 0.0))
        else:
            results.append(run_one(name, check, quick, min(timeout, left), clock))
    return results


def run_one(name, check, quick, timeout, clock):
    answers = []

    def work():
        try:
            status, detail = check(quick)
            if status not in STATUSES:
                raise CheckFailed(f"unknown status {status!r}")
            answers.append((status, one_line(detail)))
        except Exception as error:
            answers.append(("fail", one_line(error) or type(error).__name__))

    started = clock()
    worker = threading.Thread(target=work, daemon=True)
    worker.start()
    worker.join(timeout)
    seconds = round(clock() - started, 1)
    if not answers:
        return Result(name, "fail", f"no answer in {timeout:g} s", seconds)
    status, detail = answers[0]
    return Result(name, status, detail, seconds)


def one_line(value):
    lines = str(value).strip().splitlines()
    return lines[0] if lines else ""


def summary(results, seconds):
    """N ok, N warn, N fail, N skipped in Ns"""
    counts = Counter(result.status for result in results)
    return f"{counts['ok']} ok, {counts['warn']} warn, {counts['fail']} fail, {counts['skip']} skipped in {seconds:.0f}s"


def from_doctor(row):
    """A doctor check's (ok, text) as (status, text)."""
    ok, text = row
    return cli.DOCTOR_STATUS[ok], text


def first_video(search):
    """The first YouTube result for QUERY; CheckFailed when there is none."""
    videos = search(QUERY, 1)
    if not videos:
        raise CheckFailed(f"YouTube found nothing for {QUERY!r}")
    return videos[0]


# --- mpv ----------------------------------------------------------------


def silent_wav(seconds=1, rate=8000):
    """A WAV file's bytes: seconds of 16-bit mono silence."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(bytes(2 * rate * seconds))
    return buffer.getvalue()


def play_silence(client_factory=player.MpvClient, wait=PLAYBACK_WAIT):
    """The started_in mpv reports for a second of silence played through an MpvClient, from the load to its
    playback-restart; CheckFailed when mpv could not play it, or did not start it within wait seconds."""
    started = threading.Event()

    def on_state(status):
        if status["started_in"] is not None or status["error"]:
            started.set()

    with tempfile.TemporaryDirectory() as directory:
        path = os.path.join(directory, "silence.wav")
        with open(path, "wb") as file:
            file.write(silent_wav())
        client = client_factory(False, on_state=on_state, levels=False, prefetch=False, resume=False)
        try:
            client.add(Video(id="selfcheck", title="selfcheck silence", uploader=APP_NAME, duration=1, source="file", link=path))
            client.play_current()
            if not started.wait(wait):
                raise CheckFailed(f"mpv did not start a 1 s WAV within {wait} s")
            if client.error:
                raise CheckFailed(client.error)
            return client.started_in
        finally:
            client.quit()


def check_mpv(quick, doctor=cli.check_mpv, play=play_silence):
    status, text = from_doctor(doctor())
    if status != "ok":
        return status, text
    if quick:
        return "ok", f"{text}; playback {QUICK_SKIP}"
    return "ok", f"{text}; played 1 s of silence, started in {play()} s"


# --- lookups ------------------------------------------------------------


def check_youtube_search(quick, search=youtube.search):
    return "ok", f"found {first_video(search).title}"


def check_youtube_stream(quick, search=youtube.search, resolve=youtube.stream_url):
    if quick:
        return "skip", QUICK_SKIP
    video = first_video(search)
    if not resolve(video.url):
        raise CheckFailed(f"yt-dlp found no stream for {video.url}")
    return "ok", f"resolved {video.title}"


def check_soundcloud(quick, search=youtube.search):
    videos = search(SOUNDCLOUD_QUERY, 1, source="soundcloud")
    if not videos:
        raise CheckFailed(f"SoundCloud found nothing for {SOUNDCLOUD_QUERY!r}")
    return "ok", f"found {videos[0].title}"


def check_podcasts(quick, latest=podcasts.latest):
    show, episodes = latest(PODCAST_QUERY, 1)
    if show is None:
        raise CheckFailed(f"iTunes found no show for {PODCAST_QUERY!r}")
    if not episodes:
        raise CheckFailed(f"the feed of {show.name} lists no episodes")
    return "ok", f"{show.name}: {episodes[0].title}"


def check_lyrics(quick, request=lyrics.request):
    artist, track = LYRICS_TRACK
    found = request(artist, track, None, urllib.request.urlopen)
    if found is None:
        raise CheckFailed(lyrics.not_found(artist, track))
    if not found.synced:
        return "warn", f"only plain lyrics for {lyrics.name(artist, track)}"
    return "ok", f"{len(found.synced)} synced lines for {lyrics.name(artist, track)}"


def check_art(quick, doctor=cli.check_art, search=youtube.search, fetch=art.download):
    status, text = from_doctor(doctor())
    if status != "ok":
        return status, text
    video = first_video(search)
    if not video.thumbnail:
        raise CheckFailed(f"{video.title} has no thumbnail")
    data = fetch(video.thumbnail)
    if not data:
        raise CheckFailed(f"cannot fetch the thumbnail {video.thumbnail}")
    return "ok", f"{text}; a {len(data)} byte thumbnail"


# --- logins -------------------------------------------------------------


def not_logged_in(name):
    return "skip", f"not logged in ({APP_NAME} {name} login)"


def check_lastfm(quick, target=scrobble.LASTFM):
    if not target.logged_in():
        return not_logged_in(target.name)
    reply = target.signed("user.getInfo", {}, target.load_session())
    return "ok", f"logged in as {scrobble.field(reply, 'user', 'name')}"


def check_listenbrainz(quick, target=scrobble.LISTENBRAINZ):
    if not target.logged_in():
        return not_logged_in(target.name)
    session = target.load_session()
    reply = target.request("validate-token", session["token"])
    if reply.get("valid") is not True:
        return "warn", f"ListenBrainz no longer accepts the token; {target.login_again()}"
    return "ok", f"logged in as {reply.get('user_name') or session['user_name']}"


def check_spotify(quick, load=settings.load, token_path=spotify.token_path, user_name=spotify.user_name):
    if not load().spotify_client_id:
        return "skip", "no spotify_client_id set (see the README's Spotify section)"
    if not token_path().exists():
        return not_logged_in("spotify")
    try:
        return "ok", f"logged in as {user_name(refresh=False)}"  # an expired token is reported, not refreshed
    except spotify.SpotifyError as error:
        message = one_line(error)
        return "warn", message if spotify.LOGIN_AGAIN in message else f"{message}. {spotify.LOGIN_AGAIN}"


# --- this machine -------------------------------------------------------


class IdleClient:
    """Stands in for the player behind check_server's app: /api/status needs a status and a queue, and nothing plays."""

    def status(self):
        return {"idle": True}

    def queue_listing(self):
        return {"videos": []}


def check_server(quick, opener=direct_open):
    """serve's app on a spare local port, with a token of its own: the token gets /api/status, a wrong one a 401."""
    token = secrets.token_urlsafe(server.TOKEN_BYTES)
    current = dataclasses.replace(settings.DEFAULTS, server_token=token)
    web = server.ServerThread(server.make_app(IdleClient(), current), LOCALHOST, 0)
    try:
        port = web.runner.addresses[0][1]
        address = f"http://{LOCALHOST}:{port}/api/status"
        with opener(urllib.request.Request(address, headers={"Authorization": f"Bearer {token}"}), timeout=TIMEOUT):
            pass
        try:
            opener(urllib.request.Request(address, headers={"Authorization": f"Bearer x{token}"}), timeout=TIMEOUT).close()
        except urllib.error.HTTPError as error:
            if error.code != 401:
                raise CheckFailed(f"a wrong token got HTTP {error.code}, not 401") from None
        else:
            raise CheckFailed("a wrong token got in")
    finally:
        web.stop()
    return "ok", f"served /api/status on port {port}; a wrong token got 401"


def check_ffmpeg(quick, doctor=cli.check_ffmpeg):
    return from_doctor(doctor())


def check_data(quick):
    """Every file in the data dir parses: how many entries each holds; fail naming the files that do not."""
    problems = []

    def count(path):
        lines = parse_lines(path)
        corrupt = lines.count(None)
        if corrupt:
            problems.append(f"{path.name}: {corrupt} corrupt line{'s' if corrupt > 1 else ''}")
        return len(lines) - corrupt

    paths = [playlists.playlist_path(name) for name in playlists.names()]
    counts = [
        f"history {count(history.history_path())}",
        f"favorites {count(favorites.favorites_path())}",
        f"playlists {len(paths)} ({sum(count(path) for path in paths)} videos)",
        f"positions {count(positions.positions_path())}",
    ]
    try:
        counts.append(f"subscriptions {len(subscriptions.read_strict())}")
    except subscriptions.Unreadable as error:
        problems.append(f"{subscriptions.subscriptions_path().name}: {error}")
    if problems:
        return "fail", f"{'; '.join(problems)} in {data_path('')}"
    return "ok", f"{', '.join(counts)} in {data_path('')}"


CHECKS = [
    ("mpv", check_mpv),
    ("YouTube search", check_youtube_search),
    ("YouTube stream", check_youtube_stream),
    ("SoundCloud", check_soundcloud),
    ("Podcasts", check_podcasts),
    ("Lyrics", check_lyrics),
    ("Album art", check_art),
    ("Last.fm", check_lastfm),
    ("ListenBrainz", check_listenbrainz),
    ("Spotify", check_spotify),
    ("Server", check_server),
    ("Stream", check_ffmpeg),
    ("Data dir", check_data),
]
