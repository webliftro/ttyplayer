"""Podcast subscriptions: the shows the user follows, in one JSON file, and which of their episodes are new.

The file maps each feed URL to the show (name, author, artwork, id), when it was added, and last_seen: the pubDate of
the newest episode the user has seen. An episode is new when it was published after last_seen, or, before the show
was ever opened, after it was added. Fetching and parsing feeds stay in podcasts. export_opml / import_opml carry
the subscriptions to and from other podcast apps as OPML.
"""

import concurrent.futures
import datetime
import email.utils
import json
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass, replace
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
    entries[show.feed_url] = _entry(show)
    _write(path, entries)
    return True


def _entry(show):
    return {
        "name": show.name,
        "author": show.author,
        "artwork": show.artwork,
        "id": show.id,
        "added": _now().isoformat(timespec="seconds"),
        "last_seen": None,
    }


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


def export_opml(path: Path | None = None) -> str:
    """Every subscription as an OPML 2.0 document, the shows in name order."""
    opml = ElementTree.Element("opml", version="2.0")
    head = ElementTree.SubElement(opml, "head")
    ElementTree.SubElement(head, "title").text = "ttyplayer subscriptions"
    ElementTree.SubElement(head, "dateCreated").text = email.utils.format_datetime(_now())
    body = ElementTree.SubElement(opml, "body")
    for show in sorted((each.show for each in all(path)), key=lambda show: (show.name.casefold(), show.feed_url)):
        ElementTree.SubElement(body, "outline", type="rss", text=show.name, title=show.name, xmlUrl=show.feed_url)
    ElementTree.indent(opml)
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ElementTree.tostring(opml, encoding="unicode") + "\n"


def opml_shows(text, path: Path | None = None):
    """(the shows of an OPML document not yet followed, how many it lists that are, how many outlines have no feed).

    Folder outlines are walked, not counted; a show listed twice counts once as new, then as followed. An outline
    without an http(s) xmlUrl has no feed. A show's name is its text, else its title; its author is unknown until
    refresh_dated() reads it from the feed.
    """
    return _new_shows(text, _read(path or subscriptions_path()))


def import_opml(text, path: Path | None = None):
    """Follow every show of an OPML document not yet followed, in one write; (added, already, skipped) counts as
    opml_shows() gives them. PodcastError when text is not OPML."""
    path = path or subscriptions_path()
    entries = _read(path)
    shows, already, skipped = _new_shows(text, entries)
    if shows:
        entries.update((show.feed_url, _entry(show)) for show in shows)
        _write(path, entries)
    return len(shows), already, skipped


def _new_shows(text, entries):
    new, already, skipped = {}, 0, 0
    for outline in _outlines(text):
        feed_url = (outline.get("xmlUrl") or "").strip()
        if not _is_web(feed_url):
            skipped += 1
        elif feed_url in entries or feed_url in new:
            already += 1
        else:
            name = podcasts.string(outline.attrib, "text") or podcasts.string(outline.attrib, "title") or "Untitled"
            new[feed_url] = Show(name=name, author="Unknown", feed_url=feed_url, artwork=None, id=0)
    return list(new.values()), already, skipped


def _is_web(url):
    try:
        return urllib.parse.urlsplit(url).scheme.lower() in ("http", "https")
    except ValueError:  # a malformed address, as https://[bad/feed
        return False


def _outlines(text):
    """The show outlines of an OPML document (str or bytes, at most podcasts.MAX_BYTES in UTF-8), nested folders
    flattened: every outline with an xmlUrl, and every other one that holds no outline. PodcastError with one line
    when text is not OPML."""
    size = len(text.encode("utf-8", "surrogatepass") if isinstance(text, str) else text)
    if size > podcasts.MAX_BYTES:
        raise podcasts.PodcastError(f"not an OPML file (larger than {podcasts.MAX_BYTES // (1024 * 1024)} MB)")
    # ElementTree never fetches a DTD or resolves an external entity; OPML needs no entities, so none is let in.
    if ("<!ENTITY" if isinstance(text, str) else b"<!ENTITY") in text:
        raise podcasts.PodcastError("not an OPML file (it declares entities)")
    try:
        body = ElementTree.fromstring(text).find("body")
    # LookupError, ValueError: an encoding the parser cannot read, as in podcasts.parse_channel
    except (ElementTree.ParseError, LookupError, ValueError) as error:
        raise podcasts.PodcastError(f"not an OPML file ({error})") from error
    if body is None:
        raise podcasts.PodcastError("not an OPML file (no body)")
    return [outline for outline in body.iter("outline") if "xmlUrl" in outline.attrib or outline.find("outline") is None]


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
    on_error(show, error); the shows whose newest episode is newest come first, the failed and empty ones last. A show
    whose author is unknown (one imported from OPML) takes the feed's, stored in one write."""

    def fetch(subscription):
        show = subscription.show
        try:
            fetched, dated = podcasts.feed(show.feed_url, limit, opener)
        except podcasts.PodcastError as error:
            if on_error:
                on_error(show, error)
            return show, [], None
        if show.author == "Unknown":
            show = replace(show, author=fetched.author)
        return show, dated, sum(is_new(published, subscription.since) for published, _ in dated)

    subscribed = all()
    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as pool:
        results = list(pool.map(fetch, subscribed))
    _learn_authors(
        {show.feed_url: show.author for (show, _, _), each in zip(results, subscribed) if show.author != each.show.author}
    )
    return sorted(results, key=_newest, reverse=True)


def _learn_authors(authors):
    """Store the {feed_url: author} refresh_dated() read from the feeds; nothing is written when there is none."""
    if not authors:
        return
    path = subscriptions_path()
    entries = _read(path)
    for feed_url, author in authors.items():
        if feed_url in entries:
            entries[feed_url]["author"] = author
    _write(path, entries)


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


class Unreadable(Exception):
    """The subscriptions file exists but cannot be read as a map of feeds to shows."""


def _read(path):
    """{feed_url: entry} of the file; {} when it is missing, and with one warning line when it is unreadable."""
    try:
        return read_strict(path)
    except Unreadable as error:
        print(f"Ignoring unreadable subscriptions file {path}: {error}", file=sys.stderr)
        return {}


def read_strict(path: Path | None = None) -> dict:
    """{feed_url: entry} of the file; {} when it is missing, Unreadable (one line) when it cannot be read."""
    path = path or subscriptions_path()
    try:
        entries = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, RecursionError) as error:
        raise Unreadable(" ".join(str(error).split())) from error
    if not isinstance(entries, dict) or any(not isinstance(entry, dict) for entry in entries.values()):
        raise Unreadable("not a map of feeds to shows")
    return entries


def _write(path, entries):
    write_atomic(path, json.dumps(entries, indent=2) + "\n")
