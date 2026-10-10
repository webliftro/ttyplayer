"""Podcasts: shows found through the iTunes Search API (no key), episodes read from the show's RSS feed.

An episode is a Video of source "podcast" whose link is the enclosure's URL, which mpv plays directly: yt-dlp
never sees it.
"""

import dataclasses
import datetime
import email.utils
import hashlib
import json
import math
import mimetypes
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass

from ttyplayer.lyrics import user_agent
from ttyplayer.models import Video
from ttyplayer.utils import data_path, read_entries, write_entries

SEARCH_API = "https://itunes.apple.com/search"
TIMEOUT = 5  # seconds for one request
MAX_BYTES = 5 * 1024 * 1024  # a feed (or a search reply) larger than this is refused, not parsed
EPISODES = 10  # episodes listed per show unless asked for more
ITUNES = "{http://www.itunes.com/dtds/podcast-1.0.dtd}"
LAST_SEARCH = "podcast_search.jsonl"  # the shows `podcast search` listed, for `podcast episodes <number>`


class PodcastError(Exception):
    """The search or the feed failed: no network, an HTTP error, a body that is not JSON or RSS. Its message is one
    line, whatever the cause's message held."""

    def __init__(self, message):
        super().__init__(" ".join(str(message).split()))


@dataclass(frozen=True)
class Show:
    name: str
    author: str
    feed_url: str
    artwork: str | None
    id: int  # iTunes' collectionId


def search_shows(query, limit=5, opener=urllib.request.urlopen) -> list[Show]:
    """Up to limit shows matching query; a result that is not a show with a feed is left out."""
    query_string = urllib.parse.urlencode({"media": "podcast", "term": query, "limit": limit})
    try:
        reply = json.loads(get(f"{SEARCH_API}?{query_string}", opener))
    except (ValueError, RecursionError) as error:  # not JSON, or nested too deep to read
        raise PodcastError(f"unexpected reply from iTunes ({error})") from error
    results = reply.get("results") if isinstance(reply, dict) else None
    if not isinstance(results, list):
        raise PodcastError("unexpected reply from iTunes (no results list)")
    return [show_from_result(result) for result in results if isinstance(result, dict) and string(result, "feedUrl")]


def show_from_result(result):
    collection_id = result.get("collectionId")
    return Show(
        name=string(result, "collectionName") or "Untitled",
        author=string(result, "artistName") or "Unknown",
        feed_url=string(result, "feedUrl"),
        artwork=string(result, "artworkUrl100"),
        id=collection_id if type(collection_id) is int else 0,
    )


def string(result, key):
    """result[key] when it is a non-empty string; None for anything else iTunes might send."""
    value = result.get(key)
    return (value.strip() or None) if isinstance(value, str) else None


def episodes(feed_url, limit=EPISODES, opener=urllib.request.urlopen) -> list[Video]:
    """The feed's newest limit episodes, newest first."""
    return [video for _, video in dated_episodes(feed_url, limit, opener)]


def dated_episodes(feed_url, limit=EPISODES, opener=urllib.request.urlopen):
    """episodes() as (published, video) pairs; published is a datetime, None when the item has no date."""
    return parse_feed(get(feed_url, opener))[:limit]


def latest(query, limit=EPISODES, opener=urllib.request.urlopen):
    """(the first show matching query, its newest limit episodes); (None, []) when no show matches."""
    shows = search_shows(query, 1, opener)
    if not shows:
        return None, []
    return shows[0], episodes(shows[0].feed_url, limit, opener)


def get(url, opener):
    """The body at url (http or https only), at most MAX_BYTES; PodcastError with one line for anything that goes
    wrong."""
    try:
        if urllib.parse.urlsplit(url).scheme.lower() not in ("http", "https"):
            raise PodcastError(f"not a web address: {url}")
        request = urllib.request.Request(url, headers={"User-Agent": user_agent()})
        with opener(request, timeout=TIMEOUT) as response:
            body = response.read(MAX_BYTES + 1)
    except urllib.error.HTTPError as error:
        raise PodcastError(f"HTTP {error.code} from {urllib.parse.urlsplit(url).netloc}") from error
    except urllib.error.URLError as error:
        raise PodcastError(str(error.reason)) from error
    except (OSError, ValueError) as error:  # a timeout, a reset connection, a URL urllib cannot open
        raise PodcastError(str(error) or type(error).__name__) from error
    if len(body) > MAX_BYTES:
        raise PodcastError(f"{url} is larger than {MAX_BYTES // (1024 * 1024)} MB")
    return body


def parse_feed(body):
    """[(published, video)] of an RSS feed's items with an audio enclosure, newest first.

    ElementTree resolves no external entities and fetches no DTD, so an untrusted feed cannot make it read files
    or the network; the body's size is capped by get().
    """
    try:
        channel = ElementTree.fromstring(body).find("channel")
    # LookupError: an encoding Python does not know; ValueError: one the parser cannot read (UTF-7, multi-byte)
    except (ElementTree.ParseError, LookupError, ValueError) as error:
        raise PodcastError(f"not an RSS feed ({error})") from error
    if channel is None:
        raise PodcastError("not an RSS feed (no channel)")
    show = text(channel, "title") or "Unknown"
    artwork = image(channel) or text(channel, "image/url")
    items = [episode(item, show, artwork) for item in channel.iter("item")]
    dated = [each for each in items if each is not None]
    # Newest first; a stable sort keeps the feed's order among the undated, which go last.
    dated.sort(key=lambda pair: pair[0].timestamp() if pair[0] else float("-inf"), reverse=True)
    return dated


def episode(item, show, artwork):
    """(published, video) of one item; None when it has no audio enclosure."""
    enclosure = item.find("enclosure")
    url = enclosure is not None and (enclosure.get("url") or "").strip()
    if not url or not is_audio(url, enclosure.get("type")):
        return None
    video = Video(
        id=hashlib.sha1(url.encode()).hexdigest()[:11],
        title=text(item, "title") or "Untitled",
        uploader=show,
        duration=parse_duration(text(item, f"{ITUNES}duration")),
        source="podcast",
        link=url,
        thumbnail=image(item) or artwork,
    )
    return published(text(item, "pubDate")), video


def is_audio(url, kind):
    """Whether an enclosure is sound: its type says audio/…, or, without a type, its file name's extension does."""
    kind = kind or mimetypes.guess_type(urllib.parse.urlsplit(url).path)[0] or ""
    return kind.startswith("audio/")


def text(element, path):
    found = element.find(path)
    return found.text.strip() if found is not None and found.text else None


def image(element):
    found = element.find(f"{ITUNES}image")
    return found.get("href") if found is not None else None


def parse_duration(value):
    """Seconds of an itunes:duration: H:MM:SS, MM:SS or a number of seconds; None for anything else (more parts, a
    negative, NaN or an infinite number)."""
    if not value or value.count(":") > 2:
        return None
    try:
        seconds = 0
        for part in value.split(":"):
            seconds = seconds * 60 + float(part)
    except ValueError:
        return None
    return int(seconds) if math.isfinite(seconds) and seconds >= 0 else None


def published(value):
    """A pubDate's datetime (RFC 822), UTC when it names no zone; None when it is missing or unreadable."""
    try:
        date = email.utils.parsedate_to_datetime(value) if value else None
    except (TypeError, ValueError):
        return None
    # An aware datetime's timestamp() is arithmetic; a naive one asks the OS, which refuses far-off years.
    return date.replace(tzinfo=datetime.timezone.utc) if date and date.tzinfo is None else date


def remember(shows):
    """Keep the shows a search listed, so `podcast episodes <number>` can name one by its number."""
    write_entries(data_path(LAST_SEARCH), [dataclasses.asdict(show) for show in shows])


def remembered(number):
    """Show number (from 1) of the last search; None when there is none."""
    shows = read_entries(data_path(LAST_SEARCH))
    if not 1 <= number <= len(shows):
        return None
    return Show(**{field.name: shows[number - 1].get(field.name) for field in dataclasses.fields(Show)})
