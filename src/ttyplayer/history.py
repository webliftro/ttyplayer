"""What has been played, one JSON object per line, newest at the bottom."""

from pathlib import Path

from ttyplayer.models import Video
from ttyplayer.utils import append_entries, data_path, read_entries, stored_video, video_entry


def history_path() -> Path:
    return data_path("history.jsonl")


def record(video: Video, path: Path | None = None):
    append_entries(path or history_path(), [video_entry(video, "played_at")])


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
    seen = set()
    videos = []
    for entry in reversed(read_entries(path or history_path())):
        if entry["id"] in seen:
            continue
        seen.add(entry["id"])
        videos.append(stored_video(entry))
        if len(videos) == limit:
            break
    return videos
