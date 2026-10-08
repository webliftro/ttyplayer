import _thread
import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time

from ttyplayer import control
from ttyplayer.models import Video
from ttyplayer.utils import APP_NAME, WINDOWS, format_time

if WINDOWS:
    import msvcrt
else:
    import termios
    import tty

SEEK_SECONDS = 5
VOLUME_STEP = 5
VOLUME_POLL_SECONDS = 2
VOLUME_CELLS = 10
VOLUME_SOURCES = {"ao-volume": "device", "volume": "player"}  # in order of preference
CONNECT_ATTEMPTS = 30  # x 0.1s = 3s for mpv to create its socket
KEY_POLL_SECONDS = 0.1  # how often the Windows key reader looks for a key, or a remote stop

# What each key does, by the key's name: the character typed, or the arrow's direction.
KEYS = {
    " ": ("toggle_pause",),
    ",": ("seek", -SEEK_SECONDS),
    ".": ("seek", SEEK_SECONDS),
    "left": ("seek", -SEEK_SECONDS),
    "right": ("seek", SEEK_SECONDS),
    "up": ("change_volume", VOLUME_STEP),
    "down": ("change_volume", -VOLUME_STEP),
    "n": ("next",),
    "p": ("prev",),
}
TERMINAL_ARROWS = {"[D": "left", "[C": "right", "[A": "up", "[B": "down"}  # after ESC
CONSOLE_ARROWS = {"K": "left", "M": "right", "H": "up", "P": "down"}  # after \xe0 or \x00


def build_argv(video, socket_path):
    argv = ["mpv", "--idle", "--no-terminal", f"--input-ipc-server={socket_path}"]
    if not video:
        # Audio only: also tell yt-dlp not to pick (and buffer) a video stream,
        # which shortens the wait before sound starts.
        argv += ["--no-video", "--ytdl-format=bestaudio/best"]
    return argv


def timing():
    """Whether TTYPLAYER_TIMING=1 asks for start-up times on screen."""
    return os.environ.get("TTYPLAYER_TIMING") == "1"


def volume_meter(volume, muted=False):
    """🔊 (🔇 when muted) and ten cells filled to the volume, then the number; -- while mpv has not said."""
    icon = "🔇" if muted else "🔊"
    if volume is None:
        return f"{icon} {'▯' * VOLUME_CELLS} --"
    filled = max(0, min(VOLUME_CELLS, round(volume / 100 * VOLUME_CELLS)))
    return f"{icon} {'▮' * filled}{'▯' * (VOLUME_CELLS - filled)} {round(volume)}%"


def status_line(status):
    """The status line's first part, from a status() dict: times, state, volume, title, [i/n]."""
    position = format_time(status["position"])
    duration = format_time(status["duration"])
    state = "Paused" if status["paused"] else "Playing"
    volume = volume_meter(status["volume"], status["muted"])
    # The meter goes before the title, so a narrow terminal cuts the title, not the meter.
    line = f"{position} / {duration}  {state}  {volume}  {status['title']}"
    if status["total"] > 1:
        line += f"  [{status['index']}/{status['total']}]"
    if timing() and status.get("started_in") is not None:
        line += f"  started in {status['started_in']:.1f}s"
    return line


def interrupt_main():
    """Ctrl-C the main thread, even while it blocks in sys.stdin.read(1).

    Windows has no pthread_kill; there the KeyboardInterrupt lands on the key
    reader's next poll tick.
    """
    if WINDOWS:
        _thread.interrupt_main()
    else:
        signal.pthread_kill(threading.main_thread().ident, signal.SIGINT)


def ipc_path():
    """(private dir to remove on exit, path for --input-ipc-server): a named pipe on Windows."""
    if WINDOWS:
        return None, rf"\\.\pipe\{APP_NAME}-{os.getpid()}-{secrets.token_hex(4)}"
    directory = tempfile.mkdtemp(prefix=f"{APP_NAME}-")
    return directory, os.path.join(directory, "mpv.sock")


class SocketTransport:
    """mpv's IPC over its Unix socket."""

    def __init__(self, path):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self.sock.connect(path)
        except OSError:
            self.sock.close()
            raise
        self.reader = self.sock.makefile("rb")

    def write(self, data):
        self.sock.sendall(data)

    def readline(self):
        return self.reader.readline()

    def close(self):
        self.reader.close()
        self.sock.close()


class PipeTransport:
    """mpv's IPC over its Windows named pipe, opened as a file."""

    def __init__(self, path):
        self.pipe = open(path, "r+b", buffering=0)

    def write(self, data):
        while data:
            data = data[self.pipe.write(data) :]  # an unbuffered write may take only part

    def readline(self):
        return self.pipe.readline()

    def close(self):
        self.pipe.close()


def connect(path):
    """The transport to mpv at path, retried while mpv creates it; None if it never appears."""
    transport = PipeTransport if WINDOWS else SocketTransport
    for _ in range(CONNECT_ATTEMPTS):
        try:
            return transport(path)
        except (FileNotFoundError, ConnectionRefusedError):
            time.sleep(0.1)
    return None


def terminal_keys():
    """Key names from a POSIX terminal in cbreak mode; restores the terminal when closed."""
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        while True:
            key = sys.stdin.read(1)
            if key == "\x1b":  # arrow keys arrive as ESC [ letter
                key = TERMINAL_ARROWS.get(sys.stdin.read(2))
            yield key
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def console_keys():
    """Key names from the Windows console, polled so a remote stop needs no key press."""
    while True:
        if not msvcrt.kbhit():
            time.sleep(KEY_POLL_SECONDS)
            continue
        key = msvcrt.getwch()
        if key in ("\xe0", "\x00"):  # arrows and other special keys arrive as a prefix, then a letter
            key = CONSOLE_ARROWS.get(msvcrt.getwch())
        elif key == "\x03":
            raise KeyboardInterrupt
        yield key


class MpvClient:
    """Runs mpv in the background and drives it over its JSON IPC socket.

    One request per line is sent with send(); a listener thread reads replies and
    events, keeps the latest observed properties in self.state, and notifies on
    every change. The main thread owns the keyboard in run().
    """

    on_state = None
    loaded_at = None  # monotonic time of the last loadfile, until playback restarts
    started_in = None
    idle = True  # nothing loaded: before the first track, after the queue ran out
    request_id = 0  # of the last command sent
    poller = None

    def __init__(self, video=False, on_play=None, on_state=None):
        # on_play(video) is called whenever a queued video starts; the CLI uses
        # it to keep history, the player itself knows nothing about files.
        self.on_play = on_play
        # on_state(status) replaces the printed status line, for a UI that owns
        # the terminal itself.
        self.on_state = on_state
        # A private socket (or pipe) per client, so two ttyplayers never share one mpv.
        self.socket_dir, self.socket_path = ipc_path()
        try:
            self.process = subprocess.Popen(build_argv(video, self.socket_path))
        except FileNotFoundError:
            self._remove_socket_dir()
            raise
        self.ipc = connect(self.socket_path)
        if self.ipc is None:
            self.process.kill()
            self._remove_socket_dir()
            raise RuntimeError("mpv did not start")
        # Keys, listener, control socket and TUI all move through the queue.
        self.queue_lock = threading.RLock()
        self.send_lock = threading.Lock()
        self.reply_lock = threading.Lock()
        self.replies = {}  # request_id -> callback(data), until the reply arrives
        self.state = {}
        self.queue = []
        self.index = 0

        self.listener = threading.Thread(target=self.listen, daemon=True)
        self.listener.start()
        for number, name in enumerate(["time-pos", "duration", "pause", "media-title", "volume", "mute", "ao-volume"], start=1):
            self.send(["observe_property", number, name])
        self.stop_polling = threading.Event()
        self.poller = threading.Thread(target=self.poll_volume, daemon=True)
        self.poller.start()

    # --- wire protocol -------------------------------------------------

    def send(self, command, on_reply=None):
        """Send one command; on_reply(data), if given, gets its reply's data, or None on an error."""
        with self.send_lock:
            self.request_id += 1
            if on_reply:
                # Registered before sending, so the reply can't arrive first.
                with self.reply_lock:
                    self.replies[self.request_id] = on_reply
            line = json.dumps({"command": command, "request_id": self.request_id}) + "\n"
            self.ipc.write(line.encode())

    def get_property(self, name, callback):
        self.send(["get_property", name], callback)

    def read(self):
        text = self.ipc.readline()
        if not text:
            return None  # mpv closed the connection
        return json.loads(text)

    def listen(self):
        while True:
            message = self.read()
            if message is None:
                break
            self.handle_message(message)

    def handle_message(self, message):
        if "request_id" in message:
            with self.reply_lock:
                callback = self.replies.pop(message["request_id"], None)
            if callback:  # replies nobody asked for (observe_property, loadfile, ...) are dropped
                callback(message.get("data") if message.get("error") == "success" else None)
        elif message.get("event") == "property-change":
            self.state[message["name"]] = message.get("data")
            self.notify()
        elif message.get("event") == "playback-restart":
            if self._mark_started():
                self.notify()
        elif message.get("event") == "end-file" and message.get("reason") == "eof":
            # At the end of the queue this still notifies, to tell a UI it went idle.
            self._edit(self._next_or_idle)

    def _mark_started(self):
        """Store started_in on the first restart after a loadfile; later ones are seeks.

        Under queue_lock, so a track change can't land between reading the clock and
        reading the stamp it is measured from.
        """
        with self.queue_lock:
            if self.loaded_at is None:
                return False
            self.started_in = round(time.monotonic() - self.loaded_at, 1)
            self.loaded_at = None
            return True

    def poll_volume(self):
        """Refresh ao-volume every VOLUME_POLL_SECONDS while a track is loaded, until quit().

        mpv sends no property-change for ao-volume when the OS changes it (the system
        volume on coreaudio, the app's stream volume on PulseAudio/PipeWire), so it is read.
        """
        while not self.stop_polling.wait(VOLUME_POLL_SECONDS):
            if not self.idle:
                self.get_property("ao-volume", self._ao_volume_read)

    def _ao_volume_read(self, value):
        """Store a polled ao-volume, None when the output has none; notify only on a change."""
        if value != self.state.get("ao-volume"):
            self.state["ao-volume"] = value
            self.notify()

    # --- lifecycle ------------------------------------------------------

    def quit(self):
        if self.poller:
            self.stop_polling.set()  # before the socket closes under it
            self.poller.join(timeout=1)
        self.send(["quit"])
        try:
            self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.process.kill()
        self.ipc.close()
        self._remove_socket_dir()

    def _remove_socket_dir(self):
        if self.socket_dir:
            shutil.rmtree(self.socket_dir, ignore_errors=True)

    def run(self):
        """Own the keyboard until q, Ctrl-C or `ttyplayer stop`. Always restores the terminal.

        Meanwhile the control socket takes commands from other terminals.
        """
        keys = console_keys() if WINDOWS else terminal_keys()
        remote = control.serve(self.handle_control)
        try:
            for key in keys:
                if key == "q":
                    self.quit()
                    break
                self.press(key)
        except KeyboardInterrupt:
            self.quit()
        finally:
            if remote:
                remote.stop()
            keys.close()  # restores the terminal
            print()  # leave the status line behind instead of overwriting it

    def press(self, key):
        """Do what KEYS says the key named key does; other keys do nothing."""
        if key in KEYS:
            method, *args = KEYS[key]
            getattr(self, method)(*args)

    def toggle_pause(self):
        self.send(["cycle", "pause"])

    def seek(self, seconds):
        self.send(["seek", seconds])

    def change_volume(self, step):
        """Change the volume the meter shows: the device's when mpv exposes it, else mpv's own."""
        name, _ = self.shown_volume()
        self.send(["add", name or "volume", step])

    def toggle_mute(self):
        self.send(["cycle", "mute"])

    # --- remote control ---------------------------------------------------

    def handle_control(self, name):
        """Answer a command from the control socket with the action its key performs."""
        if name == "pause":
            self.toggle_pause()
        elif name == "next":
            self.next()
        elif name == "prev":
            self.prev()
        elif name == "mute":
            self.toggle_mute()
        elif name == "stop":
            interrupt_main()  # run()'s Ctrl-C path quits and restores the terminal
        elif name == "status":
            return control.ok(**self.status())
        else:
            return control.failure(f"unknown command {name}")
        return control.ok()

    # --- queue ----------------------------------------------------------

    def add(self, video: Video):
        self.queue.append(video)

    def load(self, url):
        self.send(["loadfile", url])

    def play_current(self):
        self._move(0)

    def next(self):
        return self._move(1)

    def prev(self):
        return self._move(-1)

    def jump(self, index):
        """Play queue[index]; False, doing nothing, out of range."""
        return self._edit(self._play_index, index)

    def remove(self, index):
        """Drop queue[index]; removing the current track plays the next one, else the previous."""
        return self._edit(self._remove, index)

    def move(self, source, target):
        """Move queue[source] to target; the current track stays the current track."""
        return self._edit(self._reorder, source, target)

    def clear_others(self):
        """Keep only the current track, or empty the queue when idle."""
        return self._edit(self._clear_others)

    def _move(self, step):
        """Step the queue and load that video; False, doing nothing, past either end."""
        return self._edit(lambda: self._play_index(self.index + step))

    def _edit(self, change, *args):
        """Run change(*args) under queue_lock; notify() after releasing it if it changed something.

        notify() runs after queue_lock is released: a UI's on_state may wait for its
        own thread, which may itself be waiting here to move the queue.
        """
        with self.queue_lock:
            changed = change(*args)
        if changed:
            self.notify()
        return changed

    # The changes below run under queue_lock, through _edit().

    def _play_index(self, index):
        if not 0 <= index < len(self.queue):
            return False
        self.index = index
        video = self.queue[index]
        self.idle = False
        self.started_in = None
        self.loaded_at = time.monotonic()
        self.load(video.url)
        if self.on_play:
            self.on_play(video)
        return True

    def _next_or_idle(self):
        if not self._play_index(self.index + 1):
            self.idle = True
        return True

    def _remove(self, index):
        if not 0 <= index < len(self.queue):
            return False
        del self.queue[index]
        if index < self.index:
            self.index -= 1
        elif index == self.index:
            if not (self._play_index(index) or self._play_index(index - 1)):
                self.index = 0
                self.idle = True
                self.started_in = None
                self.loaded_at = None
                self.send(["stop"])
        return True

    def _reorder(self, source, target):
        size = len(self.queue)
        if source == target or not (0 <= source < size and 0 <= target < size):
            return False
        current = self.index
        self.queue.insert(target, self.queue.pop(source))
        if source == current:
            self.index = target
        elif source < current <= target:
            self.index -= 1
        elif target <= current < source:
            self.index += 1
        return True

    def _clear_others(self):
        kept = [] if self.idle else self.queue[self.index : self.index + 1]
        if len(kept) == len(self.queue):
            return False
        self.queue[:] = kept
        self.index = 0
        return True

    def current_title(self):
        # The queue knows the title before mpv does, so no file-name flicker on load.
        if self.queue:
            return self.queue[self.index].title
        if self.idle:
            return None  # mpv's media-title outlives the track it named
        return self.state.get("media-title")

    def current_uploader(self):
        if self.queue:
            return self.queue[self.index].uploader
        return None

    def up_next(self):
        if self.index + 1 < len(self.queue):
            return self.queue[self.index + 1].title
        return None

    # --- display --------------------------------------------------------

    def shown_volume(self):
        """(property, value) of the first VOLUME_SOURCES property mpv gave a number for; (None, None) if none."""
        for name in VOLUME_SOURCES:
            value = self.state.get(name)
            if isinstance(value, (int, float)):
                return name, value
        return None, None

    def status(self):
        volume_property, volume = self.shown_volume()
        return {
            "title": self.current_title(),
            "uploader": self.current_uploader(),
            "position": self.state.get("time-pos"),
            "duration": self.state.get("duration"),
            "paused": bool(self.state.get("pause")),
            "index": self.index + 1,
            "total": len(self.queue),
            "volume": volume,
            "volume_source": VOLUME_SOURCES.get(volume_property),
            "muted": bool(self.state.get("mute")),
            "up_next": self.up_next(),
            "started_in": self.started_in,
            "idle": self.idle,
        }

    def notify(self):
        """Hand the new status to on_state if a UI set one, else redraw the status line."""
        if self.on_state:
            self.on_state(self.status())
        else:
            self.render()

    def render(self):
        status = self.status()
        line = status_line(status)
        if status["total"] > 1:
            up_next = status["up_next"]
            line += f"  Up next: {up_next}" if up_next else "  End of queue"

        width = shutil.get_terminal_size(fallback=(80, 23)).columns
        line = line[: max(width - 1, 1)]
        print(f"\r{line}\x1b[K", end="", flush=True)
