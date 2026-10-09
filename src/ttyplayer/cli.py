import os
import shutil
import subprocess
import sys
import tempfile
import time
from importlib import metadata

import typer

from ttyplayer import control, favorites, history, player, settings
from ttyplayer.utils import APP_NAME, data_path, format_time, parse_picks, unseen

app = typer.Typer()
config_app = typer.Typer()
app.add_typer(config_app, name="config")

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
        typer.echo(f"{'✓' if ok else '✗'} {text}")
        passed = passed and ok
    if not passed:
        raise typer.Exit(code=1)


def doctor_checks():
    """(ok, text) per check; nothing here touches the network."""
    python = ".".join(map(str, sys.version_info[:3]))
    yield sys.version_info >= (3, 13), f"Python {python}"
    yield True, f"{APP_NAME} {metadata.version(APP_NAME)}"
    yield check_yt_dlp()
    yield check_mpv()
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
    try:
        return True, mpv_version(path)
    except (OSError, subprocess.SubprocessError, IndexError) as error:
        return False, f"mpv at {path} did not answer --version: {error}"


def mpv_version(path):
    """The first line `mpv --version` prints, e.g. mpv v0.39.0 Copyright ..."""
    result = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=5, check=True)
    return result.stdout.splitlines()[0]


def mpv_install_hint(platform=sys.platform):
    """One line telling the user how to install mpv on this platform."""
    for prefix, (command, *others) in MPV_INSTALL.items():
        if platform.startswith(prefix):
            alternatives = f" (or: {', '.join(others)})" if others else ""
            return f"Install it with: {command}{alternatives}"
    return "Install it from https://mpv.io/installation/"


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
def play(target: list[str], video: bool = False, limit: int = 5):
    """Play a YouTube link or playlist, or search and pick what to play.

    Keys while playing: space pause, left/right or , . seek, up/down or - + volume,
    n next, p previous, q quit.
    """
    start_playback(resolve(target, limit), video)


@app.command()
def search(query: list[str], limit: int = 5):
    """Search YouTube and list the results"""
    from ttyplayer import youtube  # yt-dlp loads on use, so doctor runs without it

    videos = lookup(youtube.search, " ".join(query), limit)
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
def tui(video: bool = False):
    """Open the full-screen player: search, pick and play in one screen.

    Keys: / search, Enter play, a add to queue, f favorite, m more results,
    space pause, , . seek 5s, < > seek 30s, - + volume, M mute, n next,
    p previous, 1-4 tabs, t theme, ? help, Ctrl-P commands, q quit.
    """
    from ttyplayer import tui as screen  # Textual loads only for this command

    screen.TtyplayerApp(
        client_factory=player.MpvClient, resolve=screen.resolve, video=video, settings=load_settings()
    ).run()


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


def resolve(target, limit):
    """Turn a link into its videos, or search words into the videos the user picks."""
    from ttyplayer import youtube

    target_text = " ".join(target)
    if youtube.is_url(target_text):
        return lookup(youtube.fetch, target_text)

    def more(shown):
        return unseen(lookup(youtube.search, target_text, len(shown) + limit), shown)

    return pick_from(lookup(youtube.search, target_text, limit), more)


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
    try:
        client = player.MpvClient(with_video, on_play=history.record)
    except FileNotFoundError:
        fail(f"mpv is not installed. {mpv_install_hint()}")
    except RuntimeError as error:
        fail(str(error))
    for video in videos:
        client.add(video)
    client.play_current()
    client.run()


def print_videos(videos, start=1):
    for number, video in enumerate(videos, start=start):
        typer.echo(f"{number:>2}. {video.title}  ({format_time(video.duration)})  {video.uploader}")


def lookup(func, *args):
    """Run a youtube.* call, turning its errors into a one-line message and exit 1."""
    from ttyplayer import youtube

    began = time.monotonic()
    try:
        videos = func(*args)
    except youtube.YouTubeError as error:
        fail(f"YouTube lookup failed: {error}")
    if player.timing():
        typer.echo(f"lookup took {time.monotonic() - began:.1f}s", err=True)
    return videos


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
