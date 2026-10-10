import os
import random
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from importlib import metadata

import typer

from ttyplayer import control, favorites, history, player, playlists, settings
from ttyplayer.utils import APP_NAME, data_path, format_time, parse_picks, unseen, video_from_info

app = typer.Typer()
config_app = typer.Typer()
app.add_typer(config_app, name="config")
playlist_app = typer.Typer()
app.add_typer(playlist_app, name="playlist")
spotify_app = typer.Typer()
app.add_typer(spotify_app, name="spotify")

# play, search and playlist add: where a search looks; empty means the search_source setting.
SOURCE_OPTION = typer.Option("", "--source", help="Search youtube or soundcloud (default: search_source)")
shuffler = random.Random()  # playlist play --shuffle; tests swap in a seeded one

# How to install mpv, per sys.platform prefix; the README's Install section and the
# install.sh / install.ps1 scripts repeat these, and tests keep all three in step.
MPV_INSTALL = {
    "darwin": ["brew install mpv"],
    "linux": [
        "sudo apt-get install -y mpv",
        "sudo dnf install -y mpv",
        "sudo pacman -S --noconfirm mpv",
        "sudo zypper install -y mpv",
        "sudo apk add mpv",
    ],
    "win32": ["winget install -e --id shinchiro.mpv", "scoop install mpv", "choco install mpv"],
}
# The same for ffmpeg, which only serve --stream needs.
FFMPEG_INSTALL = {
    "darwin": ["brew install ffmpeg"],
    "linux": [
        "sudo apt-get install -y ffmpeg",
        "sudo dnf install -y ffmpeg-free",
        "sudo pacman -S --noconfirm ffmpeg",
        "sudo apk add ffmpeg",
    ],
    "win32": ["winget install -e --id Gyan.FFmpeg", "scoop install ffmpeg", "choco install ffmpeg"],
}


@app.callback()
def callback():
    """ttyplayer is a terminal YouTube player"""


@app.command()
def version():
    """Print the version"""
    typer.echo(f"{APP_NAME} {metadata.version(APP_NAME)}")


@app.command()
def doctor():
    """Check that ttyplayer has everything it needs to play"""
    passed = True
    for ok, text in doctor_checks():
        typer.echo(f"{DOCTOR_MARKS[ok]} {text}")
        passed = passed and ok is not False
    if not passed:
        raise typer.Exit(code=1)


DOCTOR_MARKS = {True: "✓", False: "✗", None: "-"}  # None: an optional part is missing, which is no failure


def doctor_checks():
    """(ok, text) per check; nothing here touches the network."""
    python = ".".join(map(str, sys.version_info[:3]))
    yield sys.version_info >= (3, 13), f"Python {python}"
    yield True, f"{APP_NAME} {metadata.version(APP_NAME)}"
    yield check_yt_dlp()
    yield check_mpv()
    yield check_ffmpeg()
    yield check_data_dir()
    yield check_control_dir()


def check_yt_dlp():
    try:
        from yt_dlp.version import __version__
    except ImportError as error:
        return False, f"yt-dlp cannot be imported: {error}"
    return True, f"yt-dlp {__version__}"


def check_mpv():
    path = shutil.which("mpv")
    if path is None:
        return False, f"mpv not found on PATH. {mpv_install_hint()}"
    return check_version("mpv", path)


def check_ffmpeg():
    """ffmpeg is optional: missing, it is a "-" row that does not fail the doctor."""
    path = shutil.which("ffmpeg")
    if path is None:
        return None, f"ffmpeg not found on PATH; only serve --stream needs it. {ffmpeg_install_hint()}"
    return check_version("ffmpeg", path, flag="-version")


def check_version(name, path, flag="--version"):
    try:
        return True, tool_version(path, flag)
    except (OSError, subprocess.SubprocessError, IndexError) as error:
        return False, f"{name} at {path} did not answer {flag}: {error}"


def tool_version(path, flag):
    """The first line `<path> <flag>` prints, e.g. mpv v0.39.0 Copyright ... (ffmpeg's flag is -version)"""
    result = subprocess.run([path, flag], capture_output=True, text=True, timeout=5, check=True)
    return result.stdout.splitlines()[0]


def mpv_install_hint(platform=sys.platform):
    """One line telling the user how to install mpv on this platform."""
    return install_hint(MPV_INSTALL, "https://mpv.io/installation/", platform)


def ffmpeg_install_hint(platform=sys.platform):
    return install_hint(FFMPEG_INSTALL, "https://ffmpeg.org/download.html", platform)


def install_hint(commands, page, platform):
    """One line telling the user how to install a tool on this platform: the commands' first, or page."""
    for prefix, (command, *others) in commands.items():
        if platform.startswith(prefix):
            alternatives = f" (or: {', '.join(others)})" if others else ""
            return f"Install it with: {command}{alternatives}"
    return f"Install it from {page}"


def check_data_dir():
    directory = data_path("")
    try:
        os.makedirs(directory, exist_ok=True)
        tempfile.TemporaryFile(dir=directory).close()
    except OSError as error:
        return False, f"data dir {directory} is not writable: {error}"
    return True, f"data dir {directory}"


def check_control_dir():
    endpoint = control.control_endpoint()
    directory = os.path.dirname(endpoint.path)
    try:
        endpoint.prepare()
    except OSError as error:
        return False, f"control socket dir: {error}"
    return True, f"control socket dir {directory}"


@app.command()
def play(target: list[str], video: bool = False, limit: int = 5, source: str = SOURCE_OPTION):
    """Play a YouTube link or playlist, or search and pick what to play.

    Keys while playing: space pause, left/right or , . seek, up/down or - + volume,
    n next, p previous, q quit.
    """
    start_playback(resolve(target, limit, source), video)


@app.command()
def search(query: list[str], limit: int = 5, source: str = SOURCE_OPTION):
    """Search YouTube or SoundCloud and list the results"""
    from ttyplayer import youtube  # yt-dlp loads on use, so doctor runs without it

    videos = lookup(youtube.search, " ".join(query), limit, search_source(source))
    exit_if_empty(videos)
    print_videos(videos)


@app.command(name="history")
def history_command(limit: int = 20, play: bool = False, video: bool = False, clear: bool = False):
    """List recently played videos; --play to pick some and play them again, --clear to forget"""
    if clear:
        typer.echo(f"Forgot {history.clear()} plays")
        return
    videos = history.load(limit=limit)
    if not videos:
        fail("Nothing played yet")
    if play:
        start_playback(pick_from(videos), video)
    else:
        print_videos(videos)


@app.command()
def favorite(target: list[str], limit: int = 5):
    """Add a YouTube link or playlist to favorites, or search and pick what to add"""
    videos = resolve(target, limit)
    exit_if_empty(videos)
    for video in videos:
        if favorites.add(video):
            typer.echo(f"Favorited: {video.title}")
        else:
            typer.echo(f"Already a favorite: {video.title}")


@app.command(name="favorites")
def favorites_command(
    play: bool = False, video: bool = False, remove: int | None = None, clear: bool = False
):
    """List favorites; --play to pick some and play them, --remove N to drop one, --clear to forget all"""
    if clear:
        typer.echo(f"Forgot {favorites.clear()} favorites")
        return
    if remove is not None:
        removed = favorites.remove(remove)
        if removed is None:
            fail(f"No favorite number {remove}")
        typer.echo(f"Removed: {removed.title}")
        return
    videos = favorites.load()
    if not videos:
        fail("No favorites yet")
    if play:
        start_playback(pick_from(videos), video)
    else:
        print_videos(videos)


@app.command()
def tui(
    video: bool = False,
    remote_url: str = typer.Option("", "--remote", help="Drive the ttyplayer serve at this URL (default: remote_url)"),
    token: str = typer.Option("", help="The server's token (default: server_token)"),
):
    """Open the full-screen player: search, pick and play in one screen.

    Keys: / search, Enter play, a add to queue, f favorite, m more results,
    space pause, , . seek 5s, < > seek 30s, - + volume, M mute, n next,
    p previous, 1-4 tabs, t theme, ? help, Ctrl-P commands, q quit.

    With --remote (or the remote_url setting) the server plays and this screen drives it.
    """
    from ttyplayer import tui as screen  # Textual loads only for this command

    current = load_settings()
    url = remote_url or current.remote_url
    if url:
        from ttyplayer.remote import RemoteApp  # aiohttp loads only for a remote

        ui = RemoteApp(url, token or current.server_token, resolve=screen.resolve, video=video, settings=current)
    else:
        ui = screen.TtyplayerApp(client_factory=player.MpvClient, resolve=screen.resolve, video=video, settings=current)
    ui.run()


@app.command()
def serve(
    host: str | None = None,
    port: int | None = None,
    stream: bool = typer.Option(False, "--stream", help="No sound here: stream it to the web remote (default: stream_enabled)"),
):
    """Play headless and take commands over HTTP and WebSocket, gated by a token.

    Prints the address to open (with its token) and a QR code of it; Ctrl-C stops.
    With --stream nothing plays on this machine: the web remote's Listen here plays it (needs ffmpeg).
    """
    from ttyplayer import server  # aiohttp loads only for this command

    try:
        current = server.ensure_token(load_settings())
    except settings.SettingsError as error:
        fail(str(error))
    host = host or current.server_host
    port = port or current.server_port
    ffmpeg = stream_ffmpeg() if stream or current.stream_enabled else None
    hub = server.Broadcaster()
    client = start_mpv(False, on_state=hub, pcm=pcm_source() if ffmpeg else None)
    streamer = start_streamer(client, ffmpeg) if ffmpeg else None
    remote = control.serve(client.handle_control)
    try:
        web = server.ServerThread(server.make_app(client, current, hub, streamer), host, port)
    except OSError as error:
        stop_serving(None, remote, client, streamer)
        fail(f"Cannot serve on {host}:{port}: {error.strerror or error}")
    address = server.url(host, port, current.server_token)
    typer.echo(f"Serving ttyplayer at {address}")
    if streamer:
        typer.echo("Streaming: no sound plays here; press Listen here on the web remote")
    print_qr(address)
    old_handler = signal.signal(signal.SIGTERM, signal.default_int_handler)  # a kill stops it like Ctrl-C
    try:
        while True:
            time.sleep(3600)  # Ctrl-C, SIGTERM and `stop` all land here as KeyboardInterrupt
    except KeyboardInterrupt:
        pass
    finally:
        signal.signal(signal.SIGTERM, old_handler)
        stop_serving(web, remote, client, streamer)


def stream_ffmpeg():
    """The ffmpeg serve --stream encodes with, or a one-line message and exit 1 when it cannot stream."""
    from ttyplayer import stream  # asyncio loads only for this mode

    ffmpeg = stream.find_ffmpeg()
    if ffmpeg is None:
        fail(f"serve --stream needs ffmpeg, which is not on PATH. {ffmpeg_install_hint()}")
    return ffmpeg


def pcm_source():
    """The stream.PcmSource mpv writes into for serve --stream, or a one-line message and exit 1."""
    from ttyplayer import stream

    try:
        return stream.pcm_source()
    except OSError as error:
        fail(f"Cannot create the pipe mpv streams through: {error.strerror or error}")


def start_streamer(client, ffmpeg):
    """The Streamer encoding client's sound, or a one-line message and exit 1 (mpv stopped) when ffmpeg fails."""
    from ttyplayer import stream

    try:
        return stream.Streamer(client.pcm, ffmpeg)
    except OSError as error:
        client.quit()
        fail(f"Cannot start ffmpeg at {ffmpeg}: {error.strerror or error}")


def stop_serving(web, remote, client, streamer=None):
    """Stop the HTTP server (None if it never started), then remote control, then mpv, then ffmpeg.

    mpv goes before ffmpeg so that its pipe ends rather than breaks.
    """
    if web:
        web.stop()
    if remote:
        remote.stop()
    client.quit()
    if streamer:
        streamer.stop()


def print_qr(text):
    import qrcode

    code = qrcode.QRCode(border=1)
    code.add_data(text)
    code.print_ascii()


@config_app.callback(invoke_without_command=True)
def config(context: typer.Context):
    """List the settings, or get, set or locate them"""
    if context.invoked_subcommand is not None:
        return
    current = load_settings()
    for key in settings.KEYS:
        value = getattr(current, key)
        default = "  (default)" if value == getattr(settings.DEFAULTS, key) else ""
        typer.echo(f"{key} = {settings.display(value)}{default}")


@config_app.command(name="get")
def config_get(key: str):
    """Print one setting's value"""
    try:
        settings.check_key(key)
    except settings.SettingsError as error:
        fail(str(error))
    typer.echo(settings.display(getattr(load_settings(), key)))


@config_app.command(name="set")
def config_set(key: str, value: str):
    """Change one setting and save it"""
    try:
        changed = settings.update(key, value)
    except settings.SettingsError as error:
        fail(str(error))
    typer.echo(f"{key} = {settings.display(getattr(changed, key))}")


@config_app.command(name="path")
def config_path():
    """Print where the settings file lives"""
    typer.echo(settings.settings_path())


def load_settings():
    """The saved settings, or a one-line message and exit 1 when the file is broken."""
    try:
        return settings.load()
    except settings.SettingsError as error:
        fail(str(error))


@playlist_app.callback()
def playlist():
    """Make, edit and play your own playlists"""


@playlist_app.command(name="list")
def playlist_list():
    """List the playlists and how many videos each holds"""
    names = playlists.names()
    if not names:
        fail("No playlists yet")
    for name in names:
        typer.echo(f"{name}  ({len(on_playlist(playlists.load, name))} videos)")


@playlist_app.command(name="show")
def playlist_show(name: str):
    """List a playlist's videos, numbered"""
    videos = on_playlist(playlists.load, name)
    if not videos:
        typer.echo(f"{name} is empty")
    print_videos(videos)


@playlist_app.command(name="create")
def playlist_create(name: str):
    """Make a new, empty playlist"""
    on_playlist(playlists.create, name)
    typer.echo(f"Created playlist {name}")


@playlist_app.command(name="add")
def playlist_add(name: str, target: list[str], limit: int = 5, source: str = SOURCE_OPTION):
    """Add a YouTube link or playlist, or search picks, to the end of a playlist"""
    on_playlist(playlists.require, name)
    count = on_playlist(playlists.add, name, resolve(target, limit, source))
    typer.echo(f"Added {count} videos to {name}")


@playlist_app.command(name="remove")
def playlist_remove(name: str, number: int):
    """Drop the video at this position"""
    removed = on_playlist(playlists.remove, name, number)
    typer.echo(f"Removed: {removed.title}")


@playlist_app.command(name="move")
def playlist_move(name: str, source: int, target: int):
    """Move the video at one position to another"""
    moved = on_playlist(playlists.move, name, source, target)
    typer.echo(f"Moved to {target}: {moved.title}")


@playlist_app.command(name="delete")
def playlist_delete(name: str, yes: bool = typer.Option(False, "--yes", help="Do not ask first")):
    """Delete a playlist, after asking"""
    on_playlist(playlists.require, name)
    if not yes and not typer.confirm(f"Delete playlist {name}?", default=False):
        typer.echo(f"Kept {name}")
        return
    on_playlist(playlists.delete, name)
    typer.echo(f"Deleted playlist {name}")


@playlist_app.command(name="play")
def playlist_play(name: str, video: bool = False, shuffle: bool = False):
    """Play a whole playlist, in order or shuffled"""
    videos = on_playlist(playlists.load, name)
    if shuffle:
        shuffler.shuffle(videos)
    start_playback(videos, video)


@playlist_app.command(name="import")
def playlist_import(url: str, name: str | None = typer.Option(None, "--as", help="Name it this, not its title")):
    """Save a YouTube playlist as a playlist of your own"""
    from ttyplayer import youtube

    title, videos = lookup(youtube.fetch_playlist, url)
    if title is None:
        fail(f"Not a playlist link: {url}")
    name = import_name(title, name)
    exit_if_empty(videos)
    on_playlist(playlists.create, name)
    on_playlist(playlists.add, name, videos)
    typer.echo(f"Imported {len(videos)} videos as {name}")


def import_name(title, name):
    """The --as name, else title sanitized into a playlist name; a one-line message and exit 1 when nothing is left."""
    name = name or playlists.sanitize(title)
    if not name:
        fail("The playlist title has nothing to name it by; name it with --as")
    return name


@playlist_app.command(name="save-queue")
def playlist_save_queue(name: str):
    """Save the playing ttyplayer's queue as a playlist, replacing what it held"""
    on_playlist(playlists.playlist_path, name)
    videos = [video_from_info(entry) for entry in remote("queue")["videos"]]
    if not videos:
        fail("The queue is empty")
    on_playlist(playlists.replace, name, videos)
    typer.echo(f"Saved {len(videos)} videos to {name}")


def on_playlist(func, *args):
    """Run a playlists.* call, turning its errors into a one-line message and exit 1."""
    try:
        return func(*args)
    except playlists.PlaylistError as error:
        fail(str(error))


@spotify_app.callback()
def spotify_group():
    """Bring your Spotify playlists over: each track is found on YouTube (no Spotify audio)"""


@spotify_app.command(name="login")
def spotify_login():
    """Log in to Spotify in the browser, once (needs the spotify_client_id setting)"""
    from ttyplayer import spotify

    client_id = load_settings().spotify_client_id
    if not client_id:
        fail("Set spotify_client_id first: ttyplayer config set spotify_client_id <id> (see the README's Spotify section)")
    typer.echo(f"Logged in as {on_spotify(spotify.login, client_id, typer.echo)}")


@spotify_app.command(name="playlists")
def spotify_playlists():
    """List your Spotify playlists: name, tracks, id"""
    from ttyplayer import spotify

    found = on_spotify(spotify.user_playlists)
    if not found:
        fail("No Spotify playlists")
    for name, total, spotify_id in found:
        typer.echo(f"{name}  {total}  {spotify_id}")


@spotify_app.command(name="import")
def spotify_import(
    playlist: str,
    name: str | None = typer.Option(None, "--as", help="Name it this, not its Spotify name"),
    limit: int | None = typer.Option(None, min=1, help="Import only the first N tracks"),
):
    """Save a Spotify playlist (link, spotify:playlist:<id> or id) as a playlist, each track found on YouTube"""
    from ttyplayer import spotify

    title, tracks = on_spotify(spotify.playlist, playlist, limit)
    name = import_name(title, name)
    on_spotify(spotify.import_tracks, tracks, name, typer.echo)


def on_spotify(func, *args):
    """Run a spotify.* call, turning its errors (and the YouTube and playlist ones it meets) into one line and exit 1."""
    from ttyplayer import spotify, youtube

    try:
        return func(*args)
    except (spotify.SpotifyError, playlists.PlaylistError) as error:
        fail(str(error))
    except youtube.YouTubeError as error:
        fail(youtube_failed(error))


@app.command()
def pause():
    """Pause or resume the ttyplayer playing in another terminal"""
    remote("pause")


@app.command(name="next")
def next_command():
    """Skip to the next video in the playing ttyplayer's queue"""
    remote("next")


@app.command()
def prev():
    """Go back to the previous video in the playing ttyplayer's queue"""
    remote("prev")


@app.command()
def stop():
    """Stop the ttyplayer playing in another terminal"""
    remote("stop")


@app.command(name="sleep")
def sleep_command(spec: str = typer.Argument("", help=f"{player.SLEEP_FORMS}; none shows the timer")):
    """Stop the playing ttyplayer after a while, or when its track ends"""
    typer.echo(remote(f"sleep {spec}".strip())["message"])


@app.command()
def status():
    """Show what the playing ttyplayer is playing"""
    typer.echo(player.status_line(remote("status")))


def remote(name):
    """Send name to the playing ttyplayer; its reply, or a one-line message and exit 1."""
    try:
        reply = control.send(name)
    except control.ControlError:
        fail("No ttyplayer is playing")
    if not reply.get("ok"):
        fail(reply.get("error", "The player refused"))
    return reply


def resolve(target, limit, source=""):
    """Turn a link into its videos, or search words (on source, by default the setting's) into the videos the user picks."""
    from ttyplayer import youtube

    target_text = " ".join(target)
    if youtube.is_url(target_text):
        return lookup(youtube.fetch, target_text)
    source = search_source(source)

    def more(shown):
        return unseen(lookup(youtube.search, target_text, len(shown) + limit, source), shown)

    return pick_from(lookup(youtube.search, target_text, limit, source), more)


def search_source(source):
    """source, or the search_source setting when it is empty; a one-line message and exit 1 when it is unknown."""
    from ttyplayer import youtube

    source = source or load_settings().search_source
    try:
        youtube.search_key(source)
    except ValueError as error:
        fail(str(error))
    return source


def pick_from(videos, more=None):
    """Show a numbered list and return the videos the user picked, in their order.

    With more, the user can type m to list the next batch more(videos) returns.
    """
    exit_if_empty(videos)
    videos = list(videos)
    print_videos(videos)
    return [videos[pick - 1] for pick in ask_for_picks(videos, more)]


def ask_for_picks(videos, more=None):
    """Prompt until the answer is valid picks; m appends more's batch to videos and lists it."""
    prompt = "Pick one or more numbers (1 or 1 3 5)"
    if more:
        prompt += ", m for more"
    while True:
        answer = typer.prompt(prompt)
        if more and answer.strip().lower() == "m":
            show_more(videos, more)
            continue
        picks = parse_picks(answer, len(videos))
        if picks is not None:
            return picks
        typer.echo(f"Enter numbers between 1 and {len(videos)}, separated by spaces.", err=True)


def show_more(videos, more):
    fresh = more(videos)
    if not fresh:
        typer.echo("No more results", err=True)
        return
    print_videos(fresh, start=len(videos) + 1)
    videos.extend(fresh)


def start_playback(videos, with_video):
    exit_if_empty(videos)
    client = start_mpv(with_video)
    for video in videos:
        client.add(video)
    client.play_current()
    client.run()


def start_mpv(with_video, on_state=None, pcm=None):
    """An MpvClient that keeps history, or a one-line message and exit 1 when mpv cannot start."""
    try:
        return player.MpvClient(
            with_video,
            on_play=history.record,
            on_state=on_state,
            pcm=pcm,
            levels=load_settings().show_levels,
        )
    except FileNotFoundError:
        fail(f"mpv is not installed. {mpv_install_hint()}")
    except RuntimeError as error:
        fail(str(error))


def print_videos(videos, start=1):
    for number, video in enumerate(videos, start=start):
        typer.echo(f"{number:>2}. {video.tagged_title}  ({format_time(video.duration)})  {video.uploader}")


def lookup(func, *args):
    """Run a youtube.* call, turning its errors into a one-line message and exit 1."""
    from ttyplayer import youtube

    began = time.monotonic()
    try:
        videos = func(*args)
    except youtube.YouTubeError as error:
        fail(youtube_failed(error))
    if player.timing():
        typer.echo(f"lookup took {time.monotonic() - began:.1f}s", err=True)
    return videos


def youtube_failed(error):
    return f"YouTube lookup failed: {error}"


def exit_if_empty(videos):
    if not videos:
        fail("No videos found")


def fail(message):
    typer.echo(message, err=True)
    raise typer.Exit(code=1)


def utf8_streams():
    """Pipes and redirects on Windows default to a legacy code page; the ticks and meters need UTF-8."""
    for stream in (sys.stdout, sys.stderr):
        if stream.encoding.lower().replace("-", "") != "utf8":
            stream.reconfigure(encoding="utf-8", errors="replace")


def main():
    utf8_streams()
    app()
