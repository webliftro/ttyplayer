"""Videos the user chose to keep, one JSON object per line, newest at the bottom."""

import json
from datetime import datetime, timezone
from pathlib import Path

from ttyplayer.models import Video
from ttyplayer.utils import data_path, video_from_info


def favorites_path() -> Path:
    return data_path("favorites.jsonl")


def add(video: Video, path: Path | None = None) -> bool:
    """Favorite a video. Returns False if it already was one."""
    path = path or favorites_path()
    if any(entry["id"] == video.id for entry in _entries(path)):
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "id": video.id,
        "title": video.title,
        "uploader": video.uploader,
        "duration": video.duration,
        "favorited_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")
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
    path.write_text("".join(json.dumps(entry) + "\n" for entry in kept), encoding="utf-8")
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
    videos = [video_from_info(entry) for entry in _entries(path)]
    return videos[:limit]


def _entries(path: Path) -> list[dict]:
    """Stored entries newest first, one per video id, skipping corrupt lines."""
    if not path.exists():
        return []
    seen = set()
    entries = []
    for line in reversed(path.read_text(encoding="utf-8").splitlines()):
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict) or "id" not in entry or entry["id"] in seen:
            continue
        seen.add(entry["id"])
        entries.append(entry)
    return entries
