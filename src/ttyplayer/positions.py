"""Where each long track was left, one JSON object per line, newest at the bottom: the player resumes there."""

import json
import os
import tempfile
from pathlib import Path

from ttyplayer.utils import data_path, read_entries

RESUME_MINIMUM_MINUTES = 10  # the resume_minimum_minutes default: tracks this long or longer resume
RESUME_TAIL = 30  # seconds: a track left this close to its end counts as finished, and starts over next time
KEPT = 500  # the newest entries kept


def positions_path() -> Path:
    return data_path("positions.jsonl")


def is_long(duration, minimum_minutes) -> bool:
    """Whether a track of duration seconds (None: unknown) resumes, for resume_minimum_minutes (0: none does)."""
    return minimum_minutes > 0 and duration is not None and duration >= minimum_minutes * 60


def get(video_id: str, path: Path | None = None) -> float | None:
    """The seconds video_id was left at; None when none is saved."""
    for entry in reversed(read_entries(path or positions_path())):
        if entry["id"] == video_id:
            return entry.get("position") if isinstance(entry.get("position"), (int, float)) else None
    return None


def set(video_id: str, seconds: float, path: Path | None = None):
    """Save where video_id was left, as the newest entry; the oldest past KEPT are dropped."""
    path = path or positions_path()
    entries = _without(read_entries(path), video_id) + [{"id": video_id, "position": round(seconds, 1)}]
    _write(path, entries[-KEPT:])


def clear(video_id: str, path: Path | None = None) -> bool:
    """Forget where video_id was left. Returns False, writing nothing, if nothing was saved."""
    path = path or positions_path()
    entries = read_entries(path)
    kept = _without(entries, video_id)
    if len(kept) == len(entries):
        return False
    _write(path, kept)
    return True


def _without(entries, video_id):
    return [entry for entry in entries if entry["id"] != video_id]


def _write(path, entries):
    """Write entries to a temp file beside path, then replace path with it: a reader never sees half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.writelines(json.dumps(entry) + "\n" for entry in entries)
        os.replace(temp, path)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise
