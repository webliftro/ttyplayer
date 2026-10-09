"""ttyplayer tui --remote: the TUI driving a `ttyplayer serve` elsewhere instead of its own mpv.

RemoteClient answers the calls TtyplayerApp makes on MpvClient over the server's API: commands go
out with urllib on the caller's thread (the TUI's) with a short timeout; play and queue requests,
which make the server look each video up on YouTube, go out in order on a worker thread. A second
thread holds /ws open with aiohttp and mirrors every status the server sends.
"""

import asyncio
import contextlib
import json
import queue
import threading
import urllib.error
import urllib.request
from urllib.parse import urlsplit

import aiohttp
from aiohttp import WSMsgType

from ttyplayer import control, server, tui
from ttyplayer.models import Video

REQUEST_TIMEOUT = 2  # seconds a command may hold the TUI's thread
LOOKUP_TIMEOUT = 60  # seconds the server may take to look a video up, off the TUI's thread
RETRY_SECONDS = (0.5, 1, 2, 5, 10)  # waits before each reconnect in a row; the last one repeats
HEARTBEAT_SECONDS = 10  # a server gone without closing the socket is noticed within this


class ServerError(Exception):
    """A request the server refused or never answered; the message is the toast's."""


def error_text(error):
    """The {"error": …} line of an HTTP error reply, or "" when it has none."""
    try:
        return json.load(error).get("error", "")
    except (ValueError, AttributeError, OSError):
        return ""


class RemoteClient:
    """Stands in for MpvClient: the queue, index and idle mirror the server's last status.

    add() only buffers: the next play_current() makes the buffered videos the server's queue,
    the next notify() appends them, as the TUI calls the two after its add()s; meanwhile the
    server's statuses leave the queue and index alone.
    """

    idle = True
    commands = None  # the server's command names, read from /api/commands on each connect
    last_status = None

    def __init__(self, url, token, on_play=None, on_state=None, on_error=None):
        self.url = url.rstrip("/")
        self.headers = {"Authorization": f"Bearer {token}"}
        self.on_play = on_play  # accepted for MpvClient's signature: history belongs to the server
        self.on_state = on_state
        self.on_error = on_error  # on_error(message) for a request that failed, from either thread
        self.queue_lock = threading.RLock()
        self.queue = []
        self.index = 0
        self.pending = []  # added since the last play_current() or notify()
        self.batch = 0  # bumped by each play_current() that replaces the queue, and by quit(): older posts stop
        self.jobs = queue.Queue()
        self.poster = threading.Thread(target=self.post_jobs, daemon=True)
        self.poster.start()
        self.loop = asyncio.new_event_loop()
        self.task = self.loop.create_task(self.listen())
        self.listener = threading.Thread(target=self.run_listener, daemon=True)
        self.listener.start()

    # --- HTTP -----------------------------------------------------------

    def request(self, path, body=None, timeout=REQUEST_TIMEOUT):
        """The JSON reply to GET path, or to POST body; a ServerError when it fails."""
        data = None if body is None else json.dumps(body).encode()
        headers = {**self.headers, "Content-Type": "application/json"}
        try:
            with urllib.request.urlopen(urllib.request.Request(self.url + path, data, headers), timeout=timeout) as reply:
                return json.load(reply)
        except urllib.error.HTTPError as error:
            with error:  # an error reply is an open response too
                message = self.failure(error.code, error_text(error))
            raise ServerError(message) from None
        except (OSError, ValueError):  # refused, timed out, or not JSON
            raise ServerError(self.failure()) from None

    def failure(self, status=None, message=""):
        """The toast for a reply with this HTTP status and error line; status None: no reply at all."""
        if status is None:
            return f"Server unreachable at {self.url}"
        if status == 401:
            return "Server rejected the token"
        if status == 502:
            return f"YouTube lookup failed: {message}"
        return message or f"Server error {status}"

    def report(self, error):
        if self.on_error:
            self.on_error(str(error))

    def command(self, name, value=None):
        """Run one of the server's commands and mirror the status it replies; False, reported, when it fails."""
        if self.commands is not None and name not in self.commands:
            self.report(f"The server has no {name} command")
            return False
        body = {"name": name} if value is None else {"name": name, "value": value}
        try:
            self.mirror(self.request("/api/command", body))
        except ServerError as error:
            self.report(error)
            return False
        return True

    # --- the calls TtyplayerApp makes ------------------------------------

    def toggle_pause(self):
        return self.command("pause")

    def next(self):
        return self.command("next")

    def prev(self):
        return self.command("prev")

    def toggle_mute(self):
        return self.command("mute")

    def seek(self, seconds):
        return self.command("seek", seconds)

    def change_volume(self, step):
        return self.command("volume", step)

    def jump(self, index):
        return self.command("jump", index)

    def remove(self, index):
        return self.command("remove", index)

    def clear_others(self):
        return self.command("clear_others")

    def move(self, source, target):
        """Move a row of the server's queue; False, sending nothing, when either row is off the queue, as MpvClient."""
        with self.queue_lock:
            size = len(self.queue)
        if source == target or not (0 <= source < size and 0 <= target < size):
            return False
        return self.command("move", [source, target])

    def handle_control(self, name):
        """The control socket's player commands, done on the server."""
        if name not in server.CONTROL_COMMANDS:
            return control.failure(f"unknown command {name}")
        return control.ok() if self.command(name) else control.failure(f"{name} failed on the server")

    def add(self, video: Video):
        with self.queue_lock:
            self.queue.append(video)
            self.pending.append(video)

    def play_current(self):
        """Make the buffered videos from index on the server's queue; with none buffered, replay index."""
        with self.queue_lock:
            videos, self.pending = self.pending[self.index :], []
            if videos:
                self.batch += 1
        if not videos:
            self.jump(self.index)
            return
        self.post(["/api/play"] + ["/api/queue"] * (len(videos) - 1), videos)

    def notify(self):
        """Append the buffered videos to the server's queue; on_state gets what is known meanwhile."""
        with self.queue_lock:
            videos, self.pending = self.pending, []
        if videos:
            self.post(["/api/queue"] * len(videos), videos)
        if self.on_state:
            self.on_state(self.status())

    def status(self):
        return self.last_status

    def quit(self):
        """Hang up; the server plays on. Once is enough; a second call does nothing."""
        if self.loop.is_closed():
            return
        with self.queue_lock:
            self.batch += 1  # the posts still queued, or left of one in flight, are not sent
        self.jobs.put(None)
        self.loop.call_soon_threadsafe(self.task.cancel)
        self.listener.join(timeout=1)
        if not self.listener.is_alive():
            self.loop.close()

    # --- play and queue, on the worker thread -----------------------------

    def post(self, paths, videos):
        self.jobs.put((self.batch, list(zip(paths, videos))))

    def post_jobs(self):
        """Post each job's videos in order; a job stops at its first failure or once a newer play replaced it."""
        while (job := self.jobs.get()) is not None:
            batch, requests = job
            for path, video in requests:
                if batch != self.batch:
                    break
                try:
                    self.mirror(self.request(path, {"url": video.url}, LOOKUP_TIMEOUT))
                except ServerError as error:
                    self.report(error)
                    break

    # --- state, on the listener thread ------------------------------------

    def mirror(self, status):
        """Take a status from the server: the queue it carries becomes ours; on_state gets the rest."""
        status = dict(status)
        videos = status.pop("queue", None)
        with self.queue_lock:
            if not self.pending:  # else the TUI is between its add()s and play_current(): its queue stands
                if videos is not None:
                    self.queue[:] = [Video(**video) for video in videos]
                self.index = status["index"] - 1
                self.idle = status["idle"]
            self.last_status = status
        if self.on_state:
            self.on_state(status)

    def run_listener(self):
        with contextlib.suppress(asyncio.CancelledError):
            self.loop.run_until_complete(self.task)

    async def listen(self):
        """Follow /ws until quit(), reconnecting after a wait; a failure is reported once until it connects again."""
        failures = 0
        timeout = aiohttp.ClientTimeout(total=None, connect=REQUEST_TIMEOUT)
        async with aiohttp.ClientSession(headers=self.headers, timeout=timeout) as session:
            while True:
                try:
                    await self.follow(session)
                    failures = 0
                except ServerError as error:
                    if failures == 0:
                        self.report(error)
                    failures += 1
                await asyncio.sleep(RETRY_SECONDS[min(failures, len(RETRY_SECONDS) - 1)])

    async def follow(self, session):
        """Read the command names, then mirror every status /ws sends until the server closes it."""
        try:
            async with session.get(self.url + "/api/commands", timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)) as reply:
                reply.raise_for_status()
                self.commands = set(await reply.json())
            async with session.ws_connect(self.url + "/ws", heartbeat=HEARTBEAT_SECONDS) as ws:
                async for message in ws:
                    if message.type == WSMsgType.TEXT:
                        status = json.loads(message.data)
                        if "idle" in status:  # else a failed command's {"error": …}
                            self.mirror(status)
        except aiohttp.ClientResponseError as error:  # also a refused /ws handshake
            raise ServerError(self.failure(error.status)) from None
        except (aiohttp.ClientError, OSError, asyncio.TimeoutError, ValueError):
            raise ServerError(self.failure()) from None


class RemoteApp(tui.TtyplayerApp):
    """The TUI with a RemoteClient for its player: the server plays, so this machine serves no control socket."""

    def __init__(self, url, token, **kwargs):
        super().__init__(client_factory=self.connect, **kwargs)
        self.url = url
        self.token = token
        self.sub_title = f"remote: {urlsplit(url).netloc or url}"

    def connect(self, video=False, on_play=None, on_state=None):
        return RemoteClient(self.url, self.token, on_play=on_play, on_state=on_state, on_error=self.server_error)

    def on_mount(self):
        # Textual runs TtyplayerApp.on_mount as well; once it has, connect, so the panel follows the server from the start.
        self.call_after_refresh(self.ensure_client)

    def start_remote(self):
        """No control socket: this machine plays nothing."""

    def server_error(self, message):
        """on_error for RemoteClient: a toast, whichever thread the failure happened on."""
        if self.closing:
            return
        if threading.get_ident() == self.app_thread:
            self.toast(message, severity="error")
            return
        try:
            self.call_from_thread(self.toast, message, severity="error")
        except RuntimeError:
            pass  # the app stopped while this message was on its way
