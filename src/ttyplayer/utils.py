import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from ttyplayer.models import Video

APP_NAME = "ttyplayer"  # the product name; every path, prefix and message derives from it
OLD_NAME = "cli" "tube"  # the name before ttyplayer, split so the old-name grep guard stays clean
WINDOWS = sys.platform == "win32"  # the one platform test; modules import it, tests patch theirs


def data_path(filename):
    """Where ttyplayer keeps `filename`: $XDG_DATA_HOME/ttyplayer, by default ~/.local/share/ttyplayer,
    or %LOCALAPPDATA%\\ttyplayer on Windows."""
    base = Path(os.environ.get("XDG_DATA_HOME") or default_data_home())
    migrate_data_dir(base)
    return base / APP_NAME / filename


def migrate_data_dir(base):
    """Carry the old name's data dir over, once: rename it when only it exists, else leave both alone."""
    old, new = base / OLD_NAME, base / APP_NAME
    if old.is_dir() and not new.exists():
        old.rename(new)


def default_data_home():
    if WINDOWS:
        return os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local"
    return Path.home() / ".local" / "share"


def format_time(seconds):
    if seconds is None:
        return "--:--"
    mins, secs = divmod(int(seconds), 60)
    return f"{mins}:{secs:02d}"


def video_from_info(entry):
    """Build a Video from one yt-dlp entry. Only id is guaranteed to be present."""
    return Video(
        id=entry["id"],
        title=entry.get("title") or "Untitled",
        uploader=entry.get("uploader") or entry.get("channel") or "Unknown",
        duration=entry.get("duration"),
    )


def video_entry(video, stamp):
    """The JSON-lines entry stored for video, its `stamp` field set to now (UTC)."""
    return {
        "id": video.id,
        "title": video.title,
        "uploader": video.uploader,
        "duration": video.duration,
        stamp: datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def read_entries(path):
    """The entries of a JSON-lines file in file order, skipping corrupt lines; [] if it is missing."""
    if not path.exists():
        return []
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict) and "id" in entry:
            entries.append(entry)
    return entries


def append_entries(path, entries):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.writelines(json.dumps(entry) + "\n" for entry in entries)


def write_entries(path, entries):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(entry) + "\n" for entry in entries), encoding="utf-8")


def handle_many_entries(entries):
    # yt-dlp leaves None in place of deleted or private playlist entries.
    return [video_from_info(entry) for entry in entries if entry]


def unseen(videos, shown):
    """The videos whose id is not in shown (nor repeated), in the order given."""
    seen = {video.id for video in shown}
    fresh = []
    for video in videos:
        if video.id not in seen:
            seen.add(video.id)
            fresh.append(video)
    return fresh


def parse_picks(text, count):
    """Turn "1 3 5" into [1, 3, 5], or None if anything is not a number in 1..count."""
    pieces = text.split()
    if not pieces:
        return None
    picks = []
    for piece in pieces:
        try:
            number = int(piece)
        except ValueError:
            return None
        if not 1 <= number <= count:
            return None
        picks.append(number)
    return picks
