"""Videos the user chose to keep, one JSON object per line, newest at the bottom."""

from pathlib import Path

from ttyplayer.models import Video
from ttyplayer.utils import append_entries, data_path, read_entries, stored_video, video_entry, write_entries


def favorites_path() -> Path:
    return data_path("favorites.jsonl")


def add(video: Video, path: Path | None = None) -> bool:
    """Favorite a video. Returns False if it already was one."""
    path = path or favorites_path()
    if any(entry["id"] == video.id for entry in _entries(path)):
        return False
    append_entries(path, [video_entry(video, "favorited_at")])
    return True


def remove(number: int, path: Path | None = None) -> Video | None:
    """Drop the `number`th favorite as load() lists it (1-indexed). None if out of range."""
    path = path or favorites_path()
    videos = load(path)
    if not 1 <= number <= len(videos):
        return None
    removed = videos[number - 1]
    remove_id(removed.id, path)
    return removed


def remove_id(video_id: str, path: Path | None = None) -> bool:
    """Drop the favorite with this id. Returns False if it was not one."""
    path = path or favorites_path()
    entries = _entries(path)
    kept = [entry for entry in reversed(entries) if entry["id"] != video_id]
    if len(kept) == len(entries):
        return False
    write_entries(path, kept)
    return True


def ids(path: Path | None = None) -> set[str]:
    """The id of every favorite."""
    return {entry["id"] for entry in _entries(path or favorites_path())}


def clear(path: Path | None = None) -> int:
    """Forget every favorite. Returns how many there were."""
    path = path or favorites_path()
    count = len(load(path))
    path.unlink(missing_ok=True)
    return count


def load(path: Path | None = None, limit: int | None = None) -> list[Video]:
    """Most recently added first, each video once, at most `limit` of them (all if None)."""
    path = path or favorites_path()
    videos = [stored_video(entry) for entry in _entries(path)]
    return videos[:limit]


def _entries(path: Path) -> list[dict]:
    """Stored entries newest first, one per video id, skipping corrupt lines."""
    seen = set()
    entries = []
    for entry in reversed(read_entries(path)):
        if entry["id"] in seen:
            continue
        seen.add(entry["id"])
        entries.append(entry)
    return entries
