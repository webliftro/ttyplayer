import dataclasses
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from ttyplayer.models import Video

APP_NAME = "ttyplayer"  # the product name; every path, prefix and message derives from it
OLD_NAME = "cli" "tube"  # the name before ttyplayer, split so the old-name grep guard stays clean
WINDOWS = sys.platform == "win32"  # the one platform test; modules import it, tests patch theirs
VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")  # channel (UC…, 24) and playlist ids are longer
# yt-dlp's key for a single track of each of youtube.YTDLP_SOURCES; a test keeps the two in step.
TRACK_EXTRACTORS = ("Youtube", "Soundcloud")
THUMBNAIL_WIDTH = 480  # the widest thumbnail worth fetching for a 10-cell column


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


def extractor(entry):
    """The yt-dlp extractor behind an entry: ie_key in a flat list, extractor_key in a full one."""
    return entry.get("ie_key") or entry.get("extractor_key")


def video_from_info(entry):
    """Build a Video from one yt-dlp entry, or a stored one (which names its source). Only id is guaranteed to be present."""
    source = entry.get("source") or (extractor(entry) or "youtube").lower()
    return Video(
        id=entry["id"],
        title=entry.get("title") or "Untitled",
        uploader=entry.get("uploader") or entry.get("channel") or "Unknown",
        duration=entry.get("duration"),
        source=source,
        # a SoundCloud search entry's url is an API one; webpage_url is the page people share
        link=None if source == "youtube" else entry.get("link") or entry.get("webpage_url") or entry.get("url"),
        thumbnail=thumbnail_url(entry, source),
    )


def stored_video(entry):
    """The Video of a line video_entry() wrote: its thumbnail as saved, None in a line from before thumbnails."""
    return dataclasses.replace(video_from_info(entry), thumbnail=entry.get("thumbnail"))


def thumbnail_url(entry, source):
    """A yt-dlp entry's cover: the widest of its thumbnails up to THUMBNAIL_WIDTH (else the last one),
    else its thumbnail, else for YouTube the hqdefault.jpg every video id has; None without one."""
    thumbnails = [each for each in entry.get("thumbnails") or [] if each.get("url")]
    narrow = [each for each in thumbnails if (each.get("width") or THUMBNAIL_WIDTH + 1) <= THUMBNAIL_WIDTH]
    if narrow:
        return max(narrow, key=lambda each: each["width"])["url"]
    if thumbnails:
        return thumbnails[-1]["url"]
    if entry.get("thumbnail"):
        return entry["thumbnail"]
    if source == "youtube":
        return f"https://i.ytimg.com/vi/{entry['id']}/hqdefault.jpg"
    return None


def video_entry(video, stamp):
    """The JSON-lines entry stored for video, its `stamp` field set to now (UTC)."""
    return {
        "id": video.id,
        "title": video.title,
        "uploader": video.uploader,
        "duration": video.duration,
        "source": video.source,
        "link": video.link,
        "thumbnail": video.thumbnail,
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


def is_video(entry):
    """Whether a yt-dlp entry is a playable video: a search also returns channels and playlists."""
    if not entry:  # yt-dlp leaves None in place of deleted or private playlist entries
        return False
    if extractor(entry):
        # "YoutubeTab", "SoundcloudSet", "SoundcloudUser"… for channels and playlists
        return extractor(entry) in TRACK_EXTRACTORS
    return bool(VIDEO_ID.fullmatch(entry.get("id") or ""))


def handle_many_entries(entries):
    return [video_from_info(entry) for entry in entries if is_video(entry)]


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
