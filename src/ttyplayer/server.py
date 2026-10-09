"""ttyplayer serve: an HTTP + WebSocket API over one MpvClient, gated by one token.

aiohttp runs on its own thread with its own event loop; the player's threads only hand it
statuses through Broadcaster, and blocking yt-dlp lookups run in the loop's executor.
"""

import asyncio
import hmac
import json
import secrets
import socket
import threading
from dataclasses import asdict
from pathlib import Path
from urllib.parse import urlencode

from aiohttp import WSCloseCode, WSMsgType, web

from ttyplayer import favorites, playlists, settings, youtube
from ttyplayer.utils import video_from_info

STATIC_DIR = Path(__file__).parent / "static"
SECRET_KEY = "server_token"  # the one setting the API never shows nor changes
SHUTDOWN_TIMEOUT = 1  # seconds aiohttp waits for open requests when the server stops
TOKEN_BYTES = 24

# The /api/command and /ws command names: handle_control's own (sleep with its text, "30m"), then MpvClient methods
# taking a number, a 0-based queue row, two rows, or nothing. COMMANDS is all of them, as /api/commands lists them.
CONTROL_COMMANDS = {"pause", "next", "prev", "stop", "mute"}
SLEEP_COMMAND = "sleep"
VALUE_COMMANDS = {"seek": "seek", "volume": "change_volume"}  # name -> MpvClient method
ROW_COMMANDS = {"jump": "jump", "remove": "remove"}
PAIR_COMMANDS = {"move": "move"}  # [source, target]
PLAIN_COMMANDS = {"clear_others": "clear_others"}
COMMANDS = sorted(
    CONTROL_COMMANDS | {SLEEP_COMMAND} | VALUE_COMMANDS.keys() | ROW_COMMANDS.keys() | PAIR_COMMANDS.keys() | PLAIN_COMMANDS.keys()
)


class ApiError(Exception):
    """A request the API refuses: status is the HTTP status, the message its one-line error."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


class Broadcaster:
    """The player's on_state: hands each status to the server's loop, which sends it to every socket.

    A status carries the client's queue when it differs from the last one sent, so every remote
    sees each queue edit, whoever made it, without the whole queue riding on each time-pos tick.
    """

    loop = None  # the server's loop while it runs
    client = None  # the player whose queue rides along; make_app sets it

    def __init__(self):
        self.sockets = set()
        self.queue_sent = None

    def __call__(self, status):
        loop = self.loop
        if loop is None:
            return
        queue = self.client.queue_listing()["videos"] if self.client else None
        try:
            loop.call_soon_threadsafe(self.send_status, status, queue)
        except RuntimeError:
            pass  # the loop closed between the check and the call

    def send_status(self, status, queue):
        if queue is not None and queue != self.queue_sent:
            self.queue_sent = queue
            status = {**status, "queue": queue}
        self.send_all(json.dumps(status))

    def send_all(self, text):
        for ws in list(self.sockets):
            if ws.closed:
                self.sockets.discard(ws)
            else:
                asyncio.ensure_future(self.send(ws, text))

    async def send(self, ws, text):
        try:
            await ws.send_str(text)
        except (ConnectionError, RuntimeError):
            self.sockets.discard(ws)  # broken: the others still get theirs


CLIENT = web.AppKey("client", object)
SETTINGS = web.AppKey("settings", dict)  # {"current": Settings}: PATCH replaces what it holds
HUB = web.AppKey("hub", Broadcaster)
STREAMER = web.AppKey("streamer", object)  # a stream.Streamer under serve --stream, else None


def ensure_token(current, path=None):
    """current, with a fresh server_token generated and saved if it has none."""
    if current.server_token:
        return current
    return settings.change(SECRET_KEY, secrets.token_urlsafe(TOKEN_BYTES), path)


def lan_host(host):
    """The address a phone can reach for host: this machine's LAN address when host is a wildcard."""
    if host not in ("0.0.0.0", "::", ""):
        return host
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        try:
            probe.connect(("10.255.255.255", 1))  # UDP connect sends nothing; it only picks the route
            return probe.getsockname()[0]
        except OSError:
            return socket.gethostname()


def url(host, port, token):
    """The page's address with the token, as printed and put in the QR code."""
    host = lan_host(host)
    if ":" in host:
        host = f"[{host}]"  # an IPv6 literal
    return f"http://{host}:{port}/?{urlencode({'token': token})}"


# --- requests -----------------------------------------------------------


def error_reply(status, message):
    lines = str(message).splitlines()
    return web.json_response({"error": lines[0] if lines else ""}, status=status)


@web.middleware
async def errors_as_json(request, handler):
    try:
        return await handler(request)
    except ApiError as error:
        return error_reply(error.status, error)
    except web.HTTPException as error:  # no such route, wrong method
        if error.status < 400:
            raise
        return error_reply(error.status, error.reason)


@web.middleware
async def require_token(request, handler):
    if (request.path.startswith("/api/") or request.path in ("/ws", "/stream")) and not authorized(request):
        raise ApiError(401, "unauthorized")
    return await handler(request)


def authorized(request):
    token = live_settings(request).server_token
    header = request.headers.get("Authorization", "")
    given = header.removeprefix("Bearer ") if header.startswith("Bearer ") else request.query.get("token", "")
    return bool(token) and hmac.compare_digest(given.encode(), token.encode())


async def json_body(request):
    try:
        body = await request.json()
    except ValueError:
        raise ApiError(400, "the body must be JSON") from None
    if not isinstance(body, dict):
        raise ApiError(400, "the body must be a JSON object")
    return body


def full_status(client):
    """status() plus the queue: what /api/status, every command and a new socket get."""
    return {**client.status(), "queue": client.queue_listing()["videos"]}


def is_row(value):
    """Whether a JSON value is a queue row number: an int, and not a bool."""
    return isinstance(value, int) and not isinstance(value, bool)


def run_command(client, body):
    """Do what {"name": …, "value"?} asks, as /api/command and /ws share it; the new status."""
    name = body.get("name")
    if not isinstance(name, str):
        raise ApiError(400, 'a command needs a "name"')
    value = body.get("value")
    if name in CONTROL_COMMANDS:
        client.handle_control(name)
    elif name == SLEEP_COMMAND:
        if not isinstance(value, str):
            raise ApiError(400, f'{name} needs a text value, like "30m", "end" or "off"')
        reply = client.handle_control(f"{name} {value}")
        if not reply["ok"]:
            raise ApiError(400, reply["error"])
    elif name in VALUE_COMMANDS:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ApiError(400, f"{name} needs a numeric value")
        getattr(client, VALUE_COMMANDS[name])(value)
    elif name in ROW_COMMANDS:
        if not is_row(value):
            raise ApiError(400, f"{name} needs a queue row number")
        getattr(client, ROW_COMMANDS[name])(value)
    elif name in PAIR_COMMANDS:
        if not (isinstance(value, list) and len(value) == 2 and all(is_row(row) for row in value)):
            raise ApiError(400, f"{name} needs two queue row numbers")
        getattr(client, PAIR_COMMANDS[name])(*value)
    elif name in PLAIN_COMMANDS:
        getattr(client, PLAIN_COMMANDS[name])()
    else:
        raise ApiError(400, f"unknown command {name}")
    return full_status(client)


async def lookup(func, *args):
    """func(*args) from youtube, run on a worker thread; YouTubeError becomes a 502."""
    try:
        return await asyncio.get_running_loop().run_in_executor(None, func, *args)
    except youtube.YouTubeError as error:
        raise ApiError(502, error) from None


async def resolve(request, first=False):
    """The videos a {"url"} or {"query"} body names; only a query's first result when first is set."""
    body = await json_body(request)
    if isinstance(body.get("url"), str):
        videos = await lookup(youtube.fetch, body["url"])
    elif isinstance(body.get("query"), str):
        videos = await lookup(youtube.search, body["query"], *search_args(request))
        videos = videos[:1] if first else videos
    else:
        raise ApiError(400, 'the body needs a "url" or a "query"')
    return require_videos(videos)


def search_args(request):
    """youtube.search's limit and source, from the live settings."""
    current = live_settings(request)
    return current.search_limit, current.search_source


def require_videos(videos):
    if not videos:
        raise ApiError(404, "No videos found")
    return videos


def play_videos(client, videos):
    """The queue becomes videos, playing the first."""
    with client.queue_lock:
        client.queue[:] = videos
        client.index = 0
    client.play_current()


def append_videos(client, videos):
    """Add videos to the end of the queue; when nothing plays, play the first of them."""
    with client.queue_lock:
        first = len(client.queue)
        client.queue.extend(videos)
    if client.idle:
        client.jump(first)
    else:
        client.notify()  # the remotes' queue follows


def live_settings(request):
    return request.app[SETTINGS]["current"]


def public_settings(current):
    return {key: getattr(current, key) for key in settings.KEYS if key != SECRET_KEY}


routes = web.RouteTableDef()


@routes.get("/")
async def index(request):
    return web.FileResponse(STATIC_DIR / "index.html")


@routes.get("/manifest.webmanifest")
async def manifest(request):
    return web.FileResponse(STATIC_DIR / "manifest.webmanifest", headers={"Content-Type": "application/manifest+json"})


@routes.get("/api/commands")
async def get_commands(request):
    return web.json_response(COMMANDS)


@routes.get("/api/status")
async def get_status(request):
    return web.json_response(full_status(request.app[CLIENT]))


@routes.post("/api/command")
async def post_command(request):
    return web.json_response(run_command(request.app[CLIENT], await json_body(request)))


@routes.post("/api/play")
async def post_play(request):
    client = request.app[CLIENT]
    play_videos(client, await resolve(request, first=True))
    return web.json_response(full_status(client))


@routes.post("/api/queue")
async def post_queue(request):
    client = request.app[CLIENT]
    append_videos(client, await resolve(request))
    return web.json_response(full_status(client))


@routes.get("/api/search")
async def get_search(request):
    query = request.query.get("q", "").strip()
    if not query:
        raise ApiError(400, "search needs ?q=")
    videos = await lookup(youtube.search, query, *search_args(request))
    return web.json_response([video_json(video) for video in videos])


@routes.get("/api/playlists")
async def get_playlists(request):
    return web.json_response(
        [{"name": name, "count": len(on_playlist(playlists.load, name))} for name in playlists.names()]
    )


@routes.post("/api/playlists/{name}/play")
async def post_playlist_play(request):
    client = request.app[CLIENT]
    play_videos(client, require_videos(on_playlist(playlists.load, request.match_info["name"])))
    return web.json_response(full_status(client))


def on_playlist(func, *args):
    """Run a playlists.* call; a bad name or a missing playlist becomes a 404."""
    try:
        return func(*args)
    except playlists.PlaylistError as error:
        raise ApiError(404, error) from None


@routes.get("/api/favorites")
async def get_favorites(request):
    return web.json_response(favorite_listing())


@routes.post("/api/favorites/{id}")
async def post_favorite(request):
    """Unfavorite the video with this id, or favorite it: the body then holds the video ({"title", …})."""
    video_id = request.match_info["id"]
    if not favorites.remove_id(video_id):
        body = await json_body(request) if request.can_read_body else {}
        favorites.add(video_from_info({**body, "id": video_id}))
    return web.json_response(favorite_listing())


def favorite_listing():
    return [video_json(video) for video in favorites.load()]


def video_json(video):
    """A video as the page gets it: its fields and the URL that plays it, so Play/Queue need not know its site."""
    return {**asdict(video), "url": video.url}


@routes.get("/api/settings")
async def get_settings(request):
    return web.json_response(public_settings(live_settings(request)))


@routes.patch("/api/settings")
async def patch_settings(request):
    body = await json_body(request)
    try:
        values = {key: parse_setting(key, value) for key, value in body.items()}
        for key, value in values.items():
            request.app[SETTINGS]["current"] = settings.change(key, value)
    except settings.SettingsError as error:
        raise ApiError(400, error) from None
    return web.json_response(public_settings(live_settings(request)))


def parse_setting(key, value):
    """value for key, checked as config set checks it; JSON true / 25 are taken as their text."""
    if key == SECRET_KEY:
        raise settings.SettingsError(f"{SECRET_KEY} cannot be changed here")
    settings.check_key(key)
    return settings.parse(key, value if isinstance(value, str) else settings.display(value))


@routes.get("/ws")
async def websocket(request):
    client, hub = request.app[CLIENT], request.app[HUB]
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    hub.sockets.add(ws)
    try:
        await ws.send_json(full_status(client))
        async for message in ws:
            if message.type == WSMsgType.TEXT:
                await ws.send_json(socket_command(client, message.data))
    finally:
        hub.sockets.discard(ws)
    return ws


@routes.get("/stream")
async def audio_stream(request):
    """What the player plays, as Ogg Opus, until the client leaves or the server stops."""
    streamer = request.app[STREAMER]
    if streamer is None:
        raise ApiError(404, "this server does not stream; start it with ttyplayer serve --stream")
    listener = streamer.listen(asyncio.get_running_loop())
    response = web.StreamResponse(headers={"Content-Type": "audio/ogg", "Cache-Control": "no-store"})
    try:
        await response.prepare(request)
        while (page := await listener.next()) is not None:
            await response.write(page)
    except ConnectionError:
        pass  # the client left
    finally:
        streamer.unlisten(listener)
    return response


def socket_command(client, text):
    """The reply to one command a socket sent: the new status, or {"error": …}."""
    try:
        body = json.loads(text)
        if not isinstance(body, dict):
            raise ApiError(400, "a command must be a JSON object")
        return run_command(client, body)
    except ValueError:
        return {"error": "a command must be JSON"}
    except ApiError as error:
        return {"error": str(error)}


# --- the app and its thread ---------------------------------------------


def make_app(client, current, hub=None, streamer=None):
    """The aiohttp app driving client; current holds server_token and search_limit; streamer serves /stream."""
    app = web.Application(middlewares=[errors_as_json, require_token])
    app[CLIENT] = client
    app[SETTINGS] = {"current": current}
    app[HUB] = hub or Broadcaster()
    app[HUB].client = client
    app[STREAMER] = streamer
    app.add_routes(routes)
    app.router.add_static("/static", STATIC_DIR)
    app.on_startup.append(attach_hub)
    app.on_shutdown.append(close_clients)
    return app


async def attach_hub(app):
    app[HUB].loop = asyncio.get_running_loop()


async def close_clients(app):
    hub = app[HUB]
    hub.loop = None  # the player's later statuses go nowhere
    for ws in list(hub.sockets):
        await ws.close(code=WSCloseCode.GOING_AWAY, message=b"ttyplayer stopped")
    if app[STREAMER]:
        app[STREAMER].end_listeners()


class ServerThread:
    """Serves app on host:port from its own thread and event loop until stop()."""

    def __init__(self, app, host, port):
        self.loop = asyncio.new_event_loop()
        self.runner = web.AppRunner(app, shutdown_timeout=SHUTDOWN_TIMEOUT)
        try:
            # Bound here, on the caller's thread, so a port in use is the caller's OSError.
            self.loop.run_until_complete(self.start(host, port))
        except BaseException:
            self.loop.run_until_complete(self.runner.cleanup())
            self.loop.close()
            raise
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()

    async def start(self, host, port):
        await self.runner.setup()
        await web.TCPSite(self.runner, host, port).start()

    def stop(self):
        asyncio.run_coroutine_threadsafe(self.runner.cleanup(), self.loop).result(timeout=SHUTDOWN_TIMEOUT + 5)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=1)
        self.loop.close()
