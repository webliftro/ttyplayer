"""What has been played, one JSON object per line, newest at the bottom."""

import json
from datetime import datetime, timezone
from pathlib import Path

from ttyplayer.models import Video
from ttyplayer.utils import data_path


def history_path() -> Path:
    return data_path("history.jsonl")


def record(video: Video, path: Path | None = None):
    path = path or history_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "id": video.id,
        "title": video.title,
        "uploader": video.uploader,
        "duration": video.duration,
        "played_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def clear(path: Path | None = None) -> int:
    """Forget everything. Returns how many entries were there."""
    path = path or history_path()
    if not path.exists():
        return 0
    count = len(path.read_text(encoding="utf-8").splitlines())
    path.unlink()
    return count


def load(path: Path | None = None, limit: int = 20) -> list[Video]:
    """Most recently played first, each video once, at most `limit` of them."""
    path = path or history_path()
    if not path.exists():
        return []
    seen = set()
    videos = []
    for line in reversed(path.read_text(encoding="utf-8").splitlines()):
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("id") in seen or "id" not in entry:
            continue
        seen.add(entry["id"])
        videos.append(
            Video(
                id=entry["id"],
                title=entry.get("title") or "Untitled",
                uploader=entry.get("uploader") or "Unknown",
                duration=entry.get("duration"),
            )
        )
        if len(videos) == limit:
            break
    return videos
