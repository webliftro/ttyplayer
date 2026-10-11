"""Podcast subscriptions: the shows the user follows, in one JSON file, and which of their episodes are new.

The file maps each feed URL to the show (name, author, artwork, id), when it was added, and last_seen: the pubDate of
the newest episode the user has seen. An episode is new when it was published after last_seen, or, before the show
was ever opened, after it was added. Fetching and parsing feeds stay in podcasts.
"""

import concurrent.futures
import datetime
import json
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from ttyplayer import podcasts
from ttyplayer.podcasts import Show
from ttyplayer.utils import data_path, write_atomic

WORKERS = 4  # feeds fetched at once by refresh()
LAST_LISTING = "podcast_subscriptions.jsonl"  # the shows `podcast subscriptions` listed, for unsubscribe <number>


@dataclass(frozen=True)
class Subscription:
    show: Show
    added: datetime.datetime
    last_seen: datetime.datetime | None

    @property
    def since(self):
        """Episodes published after this are new."""
        return self.last_seen or self.added


def subscriptions_path() -> Path:
    return data_path("subscriptions.json")


def add(show: Show, path: Path | None = None) -> bool:
    """Subscribe to show. Returns False if it already was a subscription."""
    path = path or subscriptions_path()
    entries = _read(path)
    if show.feed_url in entries:
        return False
    entries[show.feed_url] = {
        "name": show.name,
        "author": show.author,
        "artwork": show.artwork,
        "id": show.id,
        "added": _now().isoformat(timespec="seconds"),
        "last_seen": None,
    }
    _write(path, entries)
    return True


def remove(feed_url: str, path: Path | None = None) -> bool:
    """Unsubscribe from the show at feed_url. Returns False, writing nothing, if it was not a subscription."""
    path = path or subscriptions_path()
    entries = _read(path)
    if entries.pop(feed_url, None) is None:
        return False
    _write(path, entries)
    return True


def all(path: Path | None = None) -> list[Subscription]:
    """Every subscription, oldest first."""
    return [_subscription(feed_url, entry) for feed_url, entry in _read(path or subscriptions_path()).items()]


def get(feed_url: str, path: Path | None = None) -> Subscription | None:
    """The subscription to feed_url; None when the user does not follow it."""
    entry = _read(path or subscriptions_path()).get(feed_url)
    return None if entry is None else _subscription(feed_url, entry)


def mark_seen(feed_url: str, pub_date: datetime.datetime | None, path: Path | None = None) -> bool:
    """The user has seen the show's episodes up to pub_date; last_seen never moves back. Returns False, writing
    nothing, when feed_url is not a subscription or pub_date (None: no dated episode) changes nothing."""
    path = path or subscriptions_path()
    entries = _read(path)
    entry = entries.get(feed_url)
    if entry is None or pub_date is None:
        return False
    seen = _date(entry.get("last_seen"))
    if seen is not None and seen >= pub_date:
        return False
    entry["last_seen"] = pub_date.isoformat()
    _write(path, entries)
    return True


def is_new(published, since) -> bool:
    """Whether an episode published then (None: undated) is new to a subscription seen up to since (None: no
    subscription, so nothing is new)."""
    return published is not None and since is not None and published > since


def new_text(count) -> str:
    """The New cell of a subscription: "3 new", or "failed" when its feed could not be read (count None)."""
    return "failed" if count is None else f"{count} new"


def refresh(limit=None, opener=urllib.request.urlopen, on_error=None):
    """[(show, [video], new_count)] of every subscription, newest episode first; see refresh_dated()."""
    return [(show, [video for _, video in dated], count) for show, dated, count in refresh_dated(limit, opener, on_error)]


def refresh_dated(limit=None, opener=urllib.request.urlopen, on_error=None):
    """[(show, [(published, video)], new_count)] of every subscription: its newest limit episodes (None: all the
    feed holds), fetched WORKERS feeds at a time, and how many of those are new. A feed that fails gives (show, [], None) and
    on_error(show, error); the shows whose newest episode is newest come first, the failed and empty ones last."""

    def fetch(subscription):
        try:
            dated = podcasts.dated_episodes(subscription.show.feed_url, limit, opener)
        except podcasts.PodcastError as error:
            if on_error:
                on_error(subscription.show, error)
            return subscription.show, [], None
        return subscription.show, dated, sum(is_new(published, subscription.since) for published, _ in dated)

    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as pool:
        results = list(pool.map(fetch, all()))
    return sorted(results, key=_newest, reverse=True)


def _newest(result):
    """The sort key of a refresh result: its newest episode's timestamp, -inf without a dated one."""
    _, dated, _ = result
    return dated[0][0].timestamp() if dated and dated[0][0] else float("-inf")


def _subscription(feed_url, entry):
    show = Show(
        name=podcasts.string(entry, "name") or "Untitled",
        author=podcasts.string(entry, "author") or "Unknown",
        feed_url=feed_url,
        artwork=podcasts.string(entry, "artwork"),
        id=entry.get("id") if type(entry.get("id")) is int else 0,
    )
    return Subscription(show, _date(entry.get("added")) or _now(), _date(entry.get("last_seen")))


def _date(value):
    """The aware datetime an ISO string stored; None for anything else."""
    try:
        date = datetime.datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return date if date.tzinfo else date.replace(tzinfo=datetime.timezone.utc)


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _read(path):
    """{feed_url: entry} of the file; {} when it is missing, and with one warning line when it is unreadable."""
    try:
        entries = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, RecursionError) as error:
        return _corrupt(path, error)
    if not isinstance(entries, dict) or any(not isinstance(entry, dict) for entry in entries.values()):
        return _corrupt(path, "not a map of feeds to shows")
    return entries


def _corrupt(path, reason):
    print(f"Ignoring unreadable subscriptions file {path}: {' '.join(str(reason).split())}", file=sys.stderr)
    return {}


def _write(path, entries):
    write_atomic(path, json.dumps(entries, indent=2) + "\n")
