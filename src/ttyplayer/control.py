import hmac
import json
import os
import secrets
import socket
import stat
import sys
import tempfile
import threading

from ttyplayer.utils import APP_NAME, WINDOWS, data_path

CLIENT_TIMEOUT = 3  # seconds to wait for the player's reply
CONNECTION_TIMEOUT = 1  # seconds the server waits for one request line
ACCEPT_TIMEOUT = 0.2  # how often the server loop checks whether it was stopped


class ControlError(Exception):
    """No player answered on the control socket."""


def control_path():
    """$XDG_RUNTIME_DIR/ttyplayer/control.sock, or a per-user dir under the temp dir."""
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        return os.path.join(runtime, APP_NAME, "control.sock")
    return os.path.join(tempfile.gettempdir(), f"{APP_NAME}-{os.getuid()}", "control.sock")


def control_endpoint(path=None):
    """Where the player listens and clients connect: a Unix socket, or loopback TCP plus a token on Windows."""
    return LoopbackEndpoint(path) if WINDOWS else SocketEndpoint(path)


class SocketEndpoint:
    """The control socket at path (control_path() by default), in a dir only this user can enter."""

    def __init__(self, path=None):
        self.path = path or control_path()
        self.inode = None

    def prepare(self):
        private_dir(os.path.dirname(self.path))

    def listen(self):
        self.prepare()
        if os.path.lexists(self.path):
            os.unlink(self.path)  # left behind by a player that crashed
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.bind(self.path)
            sock.listen()
        except OSError:
            sock.close()
            raise
        # A newer player may replace the file; remove() must not remove that one.
        self.inode = os.stat(self.path).st_ino
        return sock

    def connect(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(CLIENT_TIMEOUT)
        try:
            sock.connect(self.path)
        except OSError:
            sock.close()
            raise
        return sock

    def request(self, name):
        return {"command": name}

    def authorize(self, request):
        return True  # the dir's permissions already kept other users out

    def remove(self):
        try:
            if os.stat(self.path).st_ino == self.inode:
                os.unlink(self.path)
        except FileNotFoundError:
            pass


class LoopbackEndpoint:
    """127.0.0.1 on an ephemeral port; "<port> <token>" in path (%LOCALAPPDATA%\\ttyplayer\\control.txt).

    Any local process can reach the port, so every request must carry the token,
    which only processes that can read the user's profile can know.
    """

    def __init__(self, path=None):
        self.path = path or str(data_path("control.txt"))
        self.token = None
        self.written = None

    def prepare(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)

    def listen(self):
        self.prepare()
        sock = socket.create_server(("127.0.0.1", 0))
        self.token = secrets.token_hex(16)
        self.written = f"{sock.getsockname()[1]} {self.token}"
        try:
            with open(self.path, "w") as file:
                file.write(self.written)
        except OSError:
            sock.close()
            raise
        return sock

    def connect(self):
        with open(self.path) as file:
            text = file.read()
        try:
            port, self.token = text.split()
            address = ("127.0.0.1", int(port))
        except ValueError:
            raise OSError(f"{self.path} does not hold a port and a token") from None
        return socket.create_connection(address, timeout=CLIENT_TIMEOUT)

    def request(self, name):
        return {"command": name, "token": self.token}

    def authorize(self, request):
        token = request.get("token")
        if not isinstance(token, str):
            return False
        # As bytes: compare_digest refuses non-ASCII str, and JSON can carry lone surrogates.
        return hmac.compare_digest(token.encode("utf-8", "surrogatepass"), self.token.encode())

    def remove(self):
        try:
            with open(self.path) as file:
                if file.read() != self.written:
                    return  # a newer player wrote its own
            os.unlink(self.path)
        except FileNotFoundError:
            pass


def ok(**fields):
    return {"ok": True, **fields}


def failure(message):
    return {"ok": False, "error": message}


# --- client -------------------------------------------------------------


def send(name, path=None):
    """Send one command to the running player and return its reply."""
    endpoint = control_endpoint(path)
    try:
        with endpoint.connect() as sock, sock.makefile("rw") as stream:
            stream.write(json.dumps(endpoint.request(name)) + "\n")
            stream.flush()
            text = stream.readline()
    except OSError as error:  # no socket file, refused, or no reply in time
        raise ControlError(str(error)) from error
    if not text:
        raise ControlError("the player closed the connection")
    return json.loads(text)


# --- server -------------------------------------------------------------


def private_dir(directory):
    """Create directory 0o700, then refuse it unless it is a real dir only this user can enter."""
    os.makedirs(directory, mode=0o700, exist_ok=True)
    info = os.lstat(directory)  # lstat: a symlink could point anywhere
    if not stat.S_ISDIR(info.st_mode):
        raise OSError(f"{directory} is not a directory")
    if info.st_uid != os.getuid():
        raise OSError(f"{directory} belongs to another user")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise OSError(f"{directory} is open to other users")


def reply_to(line, handler, authorize):
    """The reply to one request line; a malformed or unauthorized line gets ok: false, never an exception."""
    try:
        request = json.loads(line)
        name = request["command"]
    except (ValueError, TypeError, KeyError):
        return failure("malformed request")
    if not isinstance(name, str):
        return failure("malformed request")
    if not authorize(request):
        return failure("bad token")
    return handler(name)


class Server:
    """Answers one request per connection on a daemon thread with handler(name) -> dict."""

    def __init__(self, handler, path=None):
        self.handler = handler
        self.endpoint = control_endpoint(path)
        self.path = self.endpoint.path
        self.sock = self.endpoint.listen()
        self.sock.settimeout(ACCEPT_TIMEOUT)
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def serve(self):
        while not self.stopped.is_set():
            try:
                conn, _ = self.sock.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            with conn:
                self.answer(conn)

    def answer(self, conn):
        conn.settimeout(CONNECTION_TIMEOUT)
        try:
            # Undecodable bytes become U+FFFD, so they fail as malformed JSON, not here.
            with conn.makefile("rw", encoding="utf-8", errors="replace") as stream:
                reply = reply_to(stream.readline(), self.handler, self.endpoint.authorize)
                stream.write(json.dumps(reply) + "\n")
        except OSError:
            pass  # the client went away; wait for the next one

    def stop(self):
        self.stopped.set()
        self.thread.join(timeout=1)
        self.sock.close()
        self.endpoint.remove()


def serve(handler, path=None):
    """Start a Server, or warn on stderr and return None: playing never depends on it."""
    try:
        return Server(handler, path)
    except OSError as error:
        print(f"Remote control is off: {error}", file=sys.stderr)
        return None
