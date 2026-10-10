"""Lyrics from LRCLIB (https://lrclib.net): artist and track guessed from a video's title, looked up once
per track, kept on disk by video id; synced lyrics come as LRC, [mm:ss.xx] before each line.

Nothing here raises: a failed lookup is None, and the caller shows that there are no lyrics.
"""

import bisect
import contextlib
import hashlib
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from importlib import metadata

from ttyplayer import art
from ttyplayer.utils import APP_NAME, data_path

API = "https://lrclib.net/api"
TIMEOUT = 3  # seconds for one request; playback never waits on it
DURATION_SLACK = 5  # seconds a search hit's length may differ from the track's
MISS = "none"  # the cache file of a track LRCLIB has no lyrics for, so it is not asked again
SEPARATORS = re.compile(r"\s+[-–—]\s+")  # Artist - Song, Artist – Song
PIPE = re.compile(r"\s+\|\s+")  # Song | Artist
# (Official Video), [Lyrics], (Live at Wembley), …: the words that mark a group as no part of the track's name.
NOISE = re.compile(
    r"\s*[(\[][^()\[\]]*\b(official|video|audio|lyrics?|live|visuali[sz]er|hd|hq|4k|remaster(ed)?|explicit|clip|mv)\b[^()\[\]]*[)\]]",
    re.IGNORECASE,
)
BRACKETS = re.compile(r"\s*\[[^\]]*\]")  # [anything] is never part of a track's name
UPLOADER_NOISE = re.compile(r"\s*-\s*Topic$|\s*VEVO$", re.IGNORECASE)
TAG = re.compile(r"\[(\d+):(\d{1,2}(?:\.\d+)?)\]")  # [mm:ss] or [mm:ss.xx]


@dataclass(frozen=True)
class Lyrics:
    synced: list | None  # [(seconds, text)] in time order
    plain: str | None
    source_url: str


def guess(title, uploader):
    """(artist, track) from a video's title and uploader: Artist - Song, Song | Artist, or the uploader as the artist."""
    title = clean_title(title)
    uploader = clean_uploader(uploader or "")
    title = PIPE.split(title, maxsplit=1)[0] if SEPARATORS.search(title) else title
    parts = SEPARATORS.split(title, maxsplit=1)
    if len(parts) == 2:
        artist, track = parts
    elif len(pipe := PIPE.split(title, maxsplit=1)) == 2:
        track, artist = pipe
    else:
        artist, track = uploader, title
    return unquote(artist), unquote(track)


def clean_title(title):
    return " ".join(BRACKETS.sub("", NOISE.sub("", title or "")).split())


def clean_uploader(uploader):
    """The uploader without - Topic or VEVO; ArtistNameVEVO becomes Artist Name."""
    cleaned = UPLOADER_NOISE.sub("", uploader.strip())
    if cleaned != uploader.strip() and " " not in cleaned:
        cleaned = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", cleaned)
    return cleaned


def unquote(text):
    return text.strip().strip("\"'“”").strip()


def name(artist, track):
    """The guess as the user reads it: Artist – Song."""
    return f"{artist} – {track}" if artist else track


def not_found(artist, track):
    return f'No lyrics found for "{name(artist, track)}"'


def parse_lrc(text):
    """[(seconds, line)] of LRC text in time order; a line with several tags comes once per tag, untagged lines go."""
    synced = []
    for line in (text or "").splitlines():
        stamps = []
        while match := TAG.match(line):
            stamps.append(int(match[1]) * 60 + float(match[2]))
            line = line[match.end() :]
        synced.extend((stamp, line.strip()) for stamp in stamps)
    return sorted(synced, key=lambda pair: pair[0])


def current_line(synced, position):
    """The index of the last line whose time is at or before position; None before the first one."""
    if position is None:
        return None
    index = bisect.bisect_right([stamp for stamp, _ in synced], position) - 1
    return index if index >= 0 else None


def text(lyrics):
    """The words alone: the plain lyrics, else the synced ones without their stamps."""
    return lyrics.plain or "\n".join(line for _, line in lyrics.synced)


def lookup(artist, track, duration=None, opener=urllib.request.urlopen):
    """The track's Lyrics from LRCLIB, or None when it has none or on any error."""
    try:
        return request(artist, track, duration, opener)
    except Exception:
        return None


def request(artist, track, duration, opener):
    """lookup() that raises on network and JSON errors, so find() caches only a true miss."""
    query = {"artist_name": artist, "track_name": track}
    try:
        record = get_json("get", query | ({"duration": round(duration)} if duration else {}), opener)
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
        record = pick(get_json("search", query, opener), duration)
    return from_record(record) if record else None


def get_json(endpoint, query, opener):
    url = f"{API}/{endpoint}?{urllib.parse.urlencode(query)}"
    with opener(urllib.request.Request(url, headers={"User-Agent": user_agent()}), timeout=TIMEOUT) as response:
        return json.loads(response.read())


def user_agent():
    """LRCLIB asks every client to say who it is."""
    try:
        version = metadata.version(APP_NAME)
    except metadata.PackageNotFoundError:
        version = "dev"
    return f"{APP_NAME}/{version} (https://github.com/webliftro/ttyplayer)"


def pick(hits, duration):
    """The first search hit within DURATION_SLACK of duration, or the first one when duration is unknown."""
    for hit in hits:
        if not duration or abs((hit.get("duration") or 0) - duration) <= DURATION_SLACK:
            return hit
    return None


def from_record(record):
    """Lyrics from one LRCLIB record; None for an instrumental or one without words."""
    synced = parse_lrc(record.get("syncedLyrics")) or None
    plain = record.get("plainLyrics") or None
    if synced is None and plain is None:
        return None
    return Lyrics(synced, plain, f"{API}/get/{record['id']}")


def find(video_id, title, uploader, duration=None, opener=urllib.request.urlopen):
    """The video's Lyrics from the cache, else looked up (and cached, a miss too); None when there are none."""
    try:
        path = data_path("lyrics") / hashlib.sha1(video_id.encode()).hexdigest()
    except Exception:
        path = None
    with contextlib.suppress(OSError, ValueError, TypeError, KeyError):
        if path is not None:
            cached = path.read_text(encoding="utf-8")
            os.utime(path)  # a hit is recent use
            return None if cached == MISS else from_cache(json.loads(cached))
    try:
        found = request(*guess(title, uploader), duration, opener)
    except Exception:
        return None  # not a miss: asked again on the next play
    if path is not None:
        with contextlib.suppress(OSError):
            art.store(path, (MISS if found is None else json.dumps(found.__dict__)).encode())
    return found


def from_cache(saved):
    synced = [tuple(pair) for pair in saved["synced"]] if saved["synced"] else None
    return Lyrics(synced, saved["plain"], saved["source_url"])
