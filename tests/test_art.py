import builtins
import hashlib
import io
import os
import sys

import pytest

from ttyplayer import art

URL = "https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg"


class FakeOpener:
    """Stands in for urllib.request.urlopen: answers every url with data, or raises error."""

    def __init__(self, data=b"jpeg bytes", error=None):
        self.data, self.error = data, error
        self.calls = []

    def __call__(self, url, timeout):
        self.calls.append((url, timeout))
        if self.error:
            raise self.error
        return io.BytesIO(self.data)


@pytest.fixture(autouse=True)
def data_home(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    return tmp_path


def test_fetch_downloads_with_a_3s_timeout_and_caches_by_sha1(data_home):
    opener = FakeOpener()
    assert art.fetch(URL, opener) == b"jpeg bytes"
    assert opener.calls == [(URL, 3)]
    cached = data_home / "ttyplayer" / "art" / hashlib.sha1(URL.encode()).hexdigest()
    assert cached.read_bytes() == b"jpeg bytes"
    if sys.platform != "win32":
        assert cached.stat().st_mode & 0o777 == 0o600


def test_fetch_reads_the_cache_before_the_network():
    art.fetch(URL, FakeOpener())
    opener = FakeOpener(data=b"other")
    assert art.fetch(URL, opener) == b"jpeg bytes"
    assert opener.calls == []


@pytest.mark.parametrize("error", [OSError("no route"), TimeoutError(), ValueError("unknown url type")])
def test_fetch_returns_none_on_any_error_and_caches_nothing(error):
    assert art.fetch(URL, FakeOpener(error=error)) is None
    assert not art.cache_dir().exists()


def test_fetch_still_returns_the_bytes_when_the_cache_cannot_be_written(data_home):
    (data_home / "ttyplayer").mkdir()
    (data_home / "ttyplayer" / "art").write_text("a file where the dir should be")
    assert art.fetch(URL, FakeOpener()) == b"jpeg bytes"


def test_fetch_returns_none_when_the_cache_path_cannot_be_set_up(data_home, monkeypatch):
    (data_home / "cli" "tube").mkdir()  # the old name's data dir, so data_path migrates it

    def refuse(*args):
        raise PermissionError("read-only data dir")

    monkeypatch.setattr("pathlib.Path.rename", refuse)
    opener = FakeOpener()
    assert art.fetch(URL, opener) is None
    assert opener.calls == []


def test_trim_keeps_the_most_recently_used():
    directory = art.cache_dir()
    directory.mkdir(parents=True)
    for age, name in enumerate(["new", "used", "old", "oldest"]):
        (directory / name).write_bytes(b"x")
        os.utime(directory / name, (1000 - age, 1000 - age))
    art.trim(directory, keep=3)
    assert sorted(path.name for path in directory.iterdir()) == ["new", "old", "used"]


def test_storing_the_201st_trims_the_least_recently_used():
    assert art.CACHE_FILES == 200
    directory = art.cache_dir()
    directory.mkdir(parents=True)
    for number in range(art.CACHE_FILES):
        (directory / f"{number:03}").write_bytes(b"x")
        os.utime(directory / f"{number:03}", (number + 1, number + 1))
    art.fetch(URL, FakeOpener())
    names = {path.name for path in directory.iterdir()}
    assert len(names) == art.CACHE_FILES
    assert "000" not in names and hashlib.sha1(URL.encode()).hexdigest() in names


def test_a_cache_hit_counts_as_recent_use():
    art.fetch(URL, FakeOpener())
    path = art.cache_dir() / hashlib.sha1(URL.encode()).hexdigest()
    os.utime(path, (1, 1))
    art.fetch(URL, FakeOpener())
    assert path.stat().st_mtime > 1


def hide_modules(monkeypatch, *names):
    """Make importing any of names fail, as without the art extra."""
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name.split(".")[0] in names:
            raise ImportError(f"No module named {name!r}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)


@pytest.mark.parametrize("missing", ["PIL", "textual_image"])
def test_available_needs_both_pillow_and_textual_image(monkeypatch, missing):
    hide_modules(monkeypatch, missing)
    assert art.available() is False


def test_available_with_the_extra_installed():
    pytest.importorskip("PIL")
    pytest.importorskip("textual_image")
    assert art.available() is True


def png(size=(2, 2)):
    """A tiny PNG, as the tests' thumbnail."""
    from PIL import Image

    out = io.BytesIO()
    Image.new("RGB", size, "red").save(out, "PNG")
    return out.getvalue()


def test_decode_reads_an_image_and_none_for_anything_else():
    pytest.importorskip("PIL")
    assert art.decode(png()).size == (2, 2)
    assert art.decode(b"not an image") is None
    assert art.decode(None) is None


def test_the_base_modules_import_neither_pillow_nor_textual_image():
    import subprocess

    code = "import ttyplayer.cli, ttyplayer.art, ttyplayer.tui, sys; print(sorted(m for m in ('PIL', 'textual_image') if m in sys.modules))"
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert result.stdout == "[]\n"
