from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

from ttyplayer.models import Video
from ttyplayer.utils import handle_many_entries, video_from_info

class _Silent:
    """yt-dlp prints errors to stderr even when quiet; we report them ourselves."""

    def debug(self, message):
        pass

    def info(self, message):
        pass

    def warning(self, message):
        pass

    def error(self, message):
        pass


# extract_flat: list playlist/search entries without fetching each video in full.
OPTIONS = {"extract_flat": True, "quiet": True, "no_warnings": True, "logger": _Silent()}


class YouTubeError(Exception):
    """yt-dlp could not resolve the target: no network, bad link, private video."""


def _extract(target):
    try:
        return YoutubeDL(OPTIONS).extract_info(target, download=False)
    except DownloadError as error:
        raise YouTubeError(_clean(str(error))) from error


def _clean(message):
    # yt-dlp prefixes its messages with "ERROR: "; the CLI adds its own framing.
    return message.removeprefix("ERROR: ").strip()


def search(query, limit=5) -> list[Video]:
    info = _extract(f"ytsearch{limit}:{query}")
    return handle_many_entries(info.get("entries", []))


def fetch(url) -> list[Video]:
    """A single video link gives a one-item list; a playlist link gives every entry."""
    return fetch_playlist(url)[1]


def fetch_playlist(url) -> tuple[str | None, list[Video]]:
    """fetch(url) plus the playlist's title; the title is None for a single video link."""
    info = _extract(url)
    if "entries" in info:
        return info.get("title"), handle_many_entries(info["entries"])
    return None, [video_from_info(info)]


def is_url(text):
    return text.startswith("http")
