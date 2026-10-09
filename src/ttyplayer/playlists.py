"""Named lists the user curates, one JSON-lines file per playlist, in the order the user keeps."""

import re
from pathlib import Path

from ttyplayer.models import Video
from ttyplayer.utils import append_entries, data_path, read_entries, video_entry, video_from_info, write_entries

NAME = re.compile(r"[A-Za-z0-9 _-]{1,40}")  # also keeps a name from escaping the playlists dir
NAME_RULE = "1 to 40 letters, digits, spaces, _ or -"


class PlaylistError(Exception):
    """A bad playlist name, a missing playlist, or a position out of range."""


def playlists_dir() -> Path:
    return data_path("playlists")


def playlist_path(name: str) -> Path:
    if not NAME.fullmatch(name):
        raise PlaylistError(f"Bad playlist name {name!r}: use {NAME_RULE}")
    return playlists_dir() / f"{name}.jsonl"


def require(name: str) -> Path:
    """The path of an existing playlist; PlaylistError if there is none by that name."""
    path = playlist_path(name)
    if not path.exists():
        raise PlaylistError(f"No playlist named {name}")
    return path


def sanitize(title: str) -> str:
    """A playlist name made from a title: other characters dropped, spaces collapsed, cut to 40."""
    kept = re.sub(r"[^A-Za-z0-9 _-]", " ", title)
    return " ".join(kept.split())[:40].strip()


def names() -> list[str]:
    directory = playlists_dir()
    if not directory.is_dir():
        return []
    return sorted(path.stem for path in directory.glob("*.jsonl"))


def load(name: str) -> list[Video]:
    """The playlist's videos in file order, repeats kept."""
    return [video_from_info(entry) for entry in read_entries(require(name))]


def create(name: str):
    path = playlist_path(name)
    if path.exists():
        raise PlaylistError(f"A playlist named {name} already exists")
    write_entries(path, [])


def delete(name: str):
    require(name).unlink()


def add(name: str, videos: list[Video]) -> int:
    """Append videos to the playlist. Returns how many were added."""
    append_entries(require(name), [video_entry(video, "added_at") for video in videos])
    return len(videos)


def remove(name: str, number: int) -> Video:
    """Drop the `number`th video (1-indexed) and return it."""
    path = require(name)
    entries = read_entries(path)
    _check_position(name, number, entries)
    removed = entries.pop(number - 1)
    write_entries(path, entries)
    return video_from_info(removed)


def move(name: str, source: int, target: int) -> Video:
    """Move the `source`th video (1-indexed) to position `target` and return it."""
    path = require(name)
    entries = read_entries(path)
    _check_position(name, source, entries)
    _check_position(name, target, entries)
    moved = entries.pop(source - 1)
    entries.insert(target - 1, moved)
    write_entries(path, entries)
    return video_from_info(moved)


def replace(name: str, videos: list[Video]):
    """Make the playlist exactly these videos, creating it if it does not exist."""
    write_entries(playlist_path(name), [video_entry(video, "added_at") for video in videos])


def _check_position(name, number, entries):
    if not 1 <= number <= len(entries):
        raise PlaylistError(f"No video number {number} in {name}")
