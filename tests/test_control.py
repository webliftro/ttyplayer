import contextlib
import json
import os
import shutil
import socket
import stat
import tempfile
from pathlib import Path

import pytest

from ttyplayer import control, utils

# Unix sockets, uids and modes: the Windows transport has its own tests below.
posix_only = pytest.mark.skipif(utils.WINDOWS, reason="POSIX control socket")


@pytest.fixture
def short_dir():
    # AF_UNIX paths max out near 104 bytes on macOS; pytest's tmp_path can be longer.
    directory = tempfile.mkdtemp(prefix="ct-")
    yield directory
    shutil.rmtree(directory, ignore_errors=True)


@pytest.fixture
def path(short_dir):
    return os.path.join(short_dir, "c.sock")


@pytest.fixture
def server(path):
    calls = []

    def handler(name):
        calls.append(name)
        if name == "boom":
            return control.failure(f"unknown command {name}")
        return control.ok(echo=name)

    server = control.Server(handler, path)
    server.calls = calls
    yield server
    server.stop()


def raw_request(path, data):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(control.CLIENT_TIMEOUT)
        sock.connect(path)
        sock.sendall(data)
        # The server may answer and close before this runs; macOS then raises ENOTCONN.
        with contextlib.suppress(OSError):
            sock.shutdown(socket.SHUT_WR)
        return sock.makefile("r").readline()


@posix_only
def test_send_round_trip_reaches_the_handler(server, path):
    assert control.send("pause", path) == {"ok": True, "echo": "pause"}
    assert server.calls == ["pause"]


@posix_only
def test_handler_failure_comes_back_as_its_error(server, path):
    assert control.send("boom", path) == {"ok": False, "error": "unknown command boom"}


@posix_only
def test_malformed_line_gets_an_error_and_the_server_lives_on(server, path):
    assert '"ok": false' in raw_request(path, b"not json\n")
    assert '"ok": false' in raw_request(path, b'{"nope": 1}\n')
    assert '"ok": false' in raw_request(path, b'{"command": 3}\n')
    assert '"ok": false' in raw_request(path, b"")
    assert '"ok": false' in raw_request(path, b"\xff\n")
    assert control.send("next", path)["ok"] is True
    assert server.calls == ["next"]


@posix_only
def test_send_raises_control_error_when_nothing_listens(path):
    with pytest.raises(control.ControlError):
        control.send("pause", path)


@posix_only
def test_send_raises_control_error_when_refused(path):
    # A socket file nobody listens on: what a crashed player leaves behind.
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.bind(path)
        with pytest.raises(control.ControlError):
            control.send("pause", path)


@posix_only
def test_stop_removes_the_socket_file(path):
    server = control.Server(lambda name: control.ok(), path)
    assert stat.S_ISSOCK(os.stat(path).st_mode)
    server.stop()
    assert not os.path.exists(path)
    assert not server.thread.is_alive()
    with pytest.raises(control.ControlError):
        control.send("status", path)


@posix_only
def test_server_replaces_a_stale_socket_file(path):
    with open(path, "w") as stale:
        stale.write("left behind")
    server = control.Server(lambda name: control.ok(), path)
    try:
        assert control.send("status", path) == {"ok": True}
    finally:
        server.stop()


@posix_only
def test_stop_leaves_a_newer_players_socket_alone(path):
    older = control.Server(lambda name: control.ok(who="older"), path)
    newer = control.Server(lambda name: control.ok(who="newer"), path)
    older.stop()
    try:
        assert control.send("status", path)["who"] == "newer"
    finally:
        newer.stop()


@posix_only
def test_server_creates_its_dir_private(short_dir):
    path = os.path.join(short_dir, "d", "c.sock")
    server = control.Server(lambda name: control.ok(), path)
    server.stop()
    assert stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode) == 0o700


@posix_only
def test_server_survives_undecodable_bytes(server, path):
    assert raw_request(path, b'{"command": "\xff"}\n') == '{"ok": true, "echo": "\\ufffd"}\n'
    assert raw_request(path, b"\xff\xfe\n").startswith('{"ok": false')
    assert server.thread.is_alive()
    assert control.send("pause", path) == {"ok": True, "echo": "pause"}


def refused(directory, capsys):
    path = os.path.join(directory, "c.sock")
    assert control.serve(lambda name: control.ok(), path) is None
    assert capsys.readouterr().err.startswith("Remote control is off:")
    assert not os.path.lexists(path)


@posix_only
def test_server_refuses_an_existing_dir_open_to_others(short_dir, capsys):
    directory = os.path.join(short_dir, "d")
    os.mkdir(directory)
    os.chmod(directory, 0o777)
    refused(directory, capsys)


@posix_only
def test_server_refuses_a_symlinked_dir(short_dir, capsys):
    target = os.path.join(short_dir, "real")
    os.mkdir(target, 0o700)
    link = os.path.join(short_dir, "d")
    os.symlink(target, link)
    refused(link, capsys)
    assert os.listdir(target) == []


@posix_only
def test_server_refuses_a_dir_owned_by_another_user(short_dir, capsys, monkeypatch):
    uid = os.getuid()
    monkeypatch.setattr(control.os, "getuid", lambda: uid + 1)
    refused(short_dir, capsys)


@posix_only
def test_server_leaves_a_stale_file_alone_in_an_unsafe_dir(short_dir, capsys):
    os.chmod(short_dir, 0o755)
    stale = os.path.join(short_dir, "c.sock")
    open(stale, "w").close()
    assert control.serve(lambda name: control.ok(), stale) is None
    assert os.path.exists(stale)


@posix_only
def test_serve_warns_and_returns_none_when_it_cannot_bind(tmp_path, capsys):
    blocker = tmp_path / "file"
    blocker.write_text("")
    assert control.serve(lambda name: control.ok(), str(blocker / "c.sock")) is None
    assert capsys.readouterr().err.startswith("Remote control is off:")


@posix_only
def test_control_path_uses_xdg_runtime_dir(monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
    assert control.control_path() == "/run/user/1000/ttyplayer/control.sock"


@posix_only
def test_control_path_falls_back_to_a_per_user_temp_dir(monkeypatch):
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.setattr(control.tempfile, "gettempdir", lambda: "/tmp")
    assert control.control_path() == f"/tmp/ttyplayer-{os.getuid()}/control.sock"


def test_control_endpoint_follows_the_platform(monkeypatch):
    monkeypatch.setattr(control, "WINDOWS", False)
    assert isinstance(control.control_endpoint("/x/c.sock"), control.SocketEndpoint)
    monkeypatch.setattr(control, "WINDOWS", True)
    assert isinstance(control.control_endpoint("/x/control.txt"), control.LoopbackEndpoint)


# --- Windows: loopback TCP plus a token -----------------------------------------


@pytest.fixture
def loopback(monkeypatch, tmp_path):
    """The Windows endpoint on any OS: control.txt in tmp_path, and a server answering through it."""
    monkeypatch.setattr(control, "WINDOWS", True)
    path = str(tmp_path / "ttyplayer" / "control.txt")
    calls = []
    server = control.Server(lambda name: calls.append(name) or control.ok(echo=name), path)
    server.calls = calls
    yield server
    server.stop()


def loopback_request(path, request):
    port, _ = Path(path).read_text().split()
    with socket.create_connection(("127.0.0.1", int(port)), timeout=2) as sock:
        sock.sendall(json.dumps(request).encode() + b"\n")
        return json.loads(sock.makefile("r").readline())


def test_loopback_server_writes_its_port_and_a_random_token(loopback):
    port, token = Path(loopback.path).read_text().split()
    assert loopback.sock.getsockname() == ("127.0.0.1", int(port))
    assert len(token) == 32 and int(token, 16) >= 0


def test_loopback_send_round_trip_carries_the_token(loopback):
    assert control.send("pause", loopback.path) == {"ok": True, "echo": "pause"}
    assert loopback.calls == ["pause"]


@pytest.mark.parametrize(
    "token",
    [None, "0" * 32, 7, "é", "\ud800"],
    ids=["missing", "wrong", "not-a-string", "non-ascii", "lone-surrogate"],
)
def test_loopback_server_rejects_a_bad_token(loopback, token):
    request = {"command": "stop"} if token is None else {"command": "stop", "token": token}
    assert loopback_request(loopback.path, request) == {"ok": False, "error": "bad token"}
    assert loopback.calls == []
    # The server is still up: the real client gets through afterwards.
    assert control.send("pause", loopback.path) == {"ok": True, "echo": "pause"}
    assert loopback.calls == ["pause"]


def test_loopback_stop_removes_the_file_and_send_then_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(control, "WINDOWS", True)
    path = str(tmp_path / "control.txt")
    server = control.Server(lambda name: control.ok(), path)
    server.stop()
    assert not os.path.exists(path)
    with pytest.raises(control.ControlError):
        control.send("status", path)


def test_loopback_stop_leaves_a_newer_players_file_alone(monkeypatch, tmp_path):
    monkeypatch.setattr(control, "WINDOWS", True)
    path = str(tmp_path / "control.txt")
    older = control.Server(lambda name: control.ok(who="older"), path)
    newer = control.Server(lambda name: control.ok(who="newer"), path)
    older.stop()
    try:
        assert control.send("status", path)["who"] == "newer"
    finally:
        newer.stop()


@pytest.mark.parametrize("text", ["", "12345", "port token", "1 2 3"])
def test_loopback_send_raises_control_error_on_a_garbled_file(monkeypatch, tmp_path, text):
    monkeypatch.setattr(control, "WINDOWS", True)
    path = tmp_path / "control.txt"
    path.write_text(text)
    with pytest.raises(control.ControlError):
        control.send("status", str(path))


def test_loopback_file_lives_in_local_app_data(monkeypatch, tmp_path):
    monkeypatch.setattr(control, "WINDOWS", True)
    monkeypatch.setattr(utils, "WINDOWS", True)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert control.control_endpoint().path == str(tmp_path / "ttyplayer" / "control.txt")
