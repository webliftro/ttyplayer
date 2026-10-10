from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

from ttyplayer.models import Video
from ttyplayer.utils import handle_many_entries, video_from_info

SOURCES = ("youtube", "soundcloud")  # where search can look; the search_source setting is one of these
# Each source's two letters: yt-dlp's search key (ytsearch5:, scsearch5:) and the TUI's yt:/sc: prefix.
PREFIXES = dict(zip(("yt", "sc"), SOURCES))
SOURCE_NAMES = dict(zip(SOURCES, ("YouTube", "SoundCloud")))


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


def _extract(target, **options):
    try:
        return YoutubeDL({**OPTIONS, **options}).extract_info(target, download=False)
    except DownloadError as error:
        raise YouTubeError(_clean(str(error))) from error


def _clean(message):
    # yt-dlp prefixes its messages with "ERROR: "; the CLI adds its own framing.
    return message.removeprefix("ERROR: ").strip()


def search(query, limit=5, source="youtube") -> list[Video]:
    info = _extract(f"{search_key(source)}search{limit}:{query}")
    return handle_many_entries(info.get("entries", []))


def search_key(source):
    """The two letters of a source; ValueError naming the valid ones for anything else."""
    for prefix, known in PREFIXES.items():
        if known == source:
            return prefix
    raise ValueError(f"Unknown source {source!r}; valid sources: {', '.join(SOURCES)}")


def split_source(text, default):
    """(source, query) for search box text: a leading yt: or sc: picks the source, else default."""
    prefix, colon, rest = text.partition(":")
    if colon and prefix.strip().lower() in PREFIXES:
        return PREFIXES[prefix.strip().lower()], rest.strip()
    return default, text


def fetch(url) -> list[Video]:
    """A single video link gives a one-item list; a playlist link gives every entry."""
    return fetch_playlist(url)[1]


def fetch_playlist(url, end=None) -> tuple[str | None, list[Video]]:
    """fetch(url) plus the playlist's title; the title is None for a single video link.

    end, if given, stops yt-dlp after that many playlist entries instead of paging through all of them.
    """
    info = _extract(url, playlistend=end)
    if "entries" in info:
        return info.get("title"), handle_many_entries(info["entries"])
    return None, [video_from_info(info)]


def related(video_id, limit=10) -> list[Video]:
    """Up to limit tracks YouTube's mix for video_id lists after it: the radio's next batch.

    A mix pages on and on, so yt-dlp reads only 2 * limit + 1 entries: room for the seed and the odd
    non-video, and (limit=10 → 21) still inside the first page of ~25.
    """
    url = f"https://www.youtube.com/watch?v={video_id}&list=RD{video_id}"
    _, videos = fetch_playlist(url, end=2 * limit + 1)
    return [video for video in videos if video.id != video_id][:limit]


def is_url(text):
    return text.startswith("http")
