"""Album art: a track's thumbnail fetched once, kept on disk, decoded for the TUI's panel.

PIL and textual_image come with the optional extra ttyplayer[art]; they load here on use only,
so the base install never imports them.
"""

import contextlib
import hashlib
import io
import os
import urllib.request

from ttyplayer.utils import data_path

TIMEOUT = 3  # seconds for one thumbnail; playback never waits on it
CACHE_FILES = 200  # the cache keeps this many thumbnails, the least recently used go first
INSTALL_HINT = "uv tool install 'ttyplayer[art]'"


def available():
    """Whether the art extra is installed: textual_image and PIL both import."""
    try:
        import PIL  # noqa: F401
        import textual_image  # noqa: F401
    except ImportError:
        return False
    return True


def image_widget():
    """textual_image's Image widget class. Its import asks the terminal what it can draw (TGP, Sixel,
    else half-cells), which only works before Textual owns the terminal: call it before the app runs."""
    from textual_image.widget import Image

    return Image


def cache_dir():
    return data_path("art")


def fetch(url, opener=urllib.request.urlopen):
    """The image bytes at url, from the cache or the network (then cached); None on any error, never raises."""
    try:
        path = cache_dir() / hashlib.sha1(url.encode()).hexdigest()  # data_path may migrate the old dir
    except Exception:
        return None
    try:
        data = path.read_bytes()
        os.utime(path)  # a hit is recent use
        return data
    except OSError:
        pass
    try:
        with opener(url, timeout=TIMEOUT) as response:
            data = response.read()
    except Exception:
        return None
    with contextlib.suppress(OSError):
        store(path, data)
    return data


def store(path, data):
    """Write data to path, readable by the user only, then trim the cache to CACHE_FILES."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb") as file:
        file.write(data)
    trim(path.parent)


def trim(directory, keep=CACHE_FILES):
    """Delete all but the `keep` most recently used files in directory."""
    files = sorted(directory.iterdir(), key=lambda file: file.stat().st_mtime, reverse=True)
    for file in files[keep:]:
        with contextlib.suppress(OSError):
            file.unlink()


def decode(data):
    """data as a loaded PIL image, or None when there is none or Pillow cannot read it."""
    if not data:
        return None
    from PIL import Image

    try:
        image = Image.open(io.BytesIO(data))
        image.load()
    except Exception:  # Pillow raises many kinds for a bad file
        return None
    return image
