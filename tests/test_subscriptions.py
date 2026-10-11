import datetime
import io
import json
import pathlib
import threading
import urllib.parse

import pytest

from ttyplayer import podcasts, subscriptions, utils
from ttyplayer.podcasts import Show

DAILY = Show("The Daily", "The New York Times", "https://daily.example.org/rss", "https://daily.example.org/a.jpg", 1)
LEX = Show("Lex Fridman Podcast", "Lex Fridman", "https://lex.example.org/rss", None, 2)
BROKEN = Show("Gone", "Nobody", "https://gone.example.org/rss", None, 3)


def date(day, hour=12):
    return datetime.datetime(2026, 10, day, hour, tzinfo=datetime.timezone.utc)


def feed(show, *days):
    """An RSS feed of show with one episode per day of October 2026, in the order given."""
    items = "".join(
        f"<item><title>{show.name} {day}</title><pubDate>{date(day).strftime('%a, %d %b %Y %H:%M:%S +0000')}</pubDate>"
        f'<enclosure url="https://media.example.org/{show.id}/{day}.mp3" type="audio/mpeg"/></item>'
        for day in days
    )
    return (
        '<rss xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">'
        f"<channel><title>{show.name}</title><itunes:author>{show.author}</itunes:author>{items}</channel></rss>"
    ).encode()


class FakeOpener:
    """urllib.request.urlopen answering by host with bytes, or raising; records the URLs and the threads."""

    def __init__(self, answers):
        self.answers = answers
        self.urls = []
        self.threads = set()

    def __call__(self, request, timeout):
        self.urls.append(request.full_url)
        self.threads.add(threading.get_ident())
        answer = self.answers[urllib.parse.urlsplit(request.full_url).netloc]
        if isinstance(answer, Exception):
            raise answer
        return io.BytesIO(answer)


@pytest.fixture(autouse=True)
def data_home(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    return tmp_path


def subscribe(show, added):
    """Subscribe to show as if on day added."""
    subscriptions.add(show)
    entries = json.loads(subscriptions.subscriptions_path().read_text())
    entries[show.feed_url]["added"] = added.isoformat()
    subscriptions.subscriptions_path().write_text(json.dumps(entries))


# --- AC1: the store ------------------------------------------------------------


def test_add_stores_the_show_by_feed_url_and_all_reads_it_back():
    assert subscriptions.add(DAILY) is True
    assert subscriptions.add(LEX) is True
    stored = json.loads(subscriptions.subscriptions_path().read_text())
    assert list(stored) == [DAILY.feed_url, LEX.feed_url]
    assert stored[DAILY.feed_url]["name"] == "The Daily" and stored[DAILY.feed_url]["last_seen"] is None
    listed = subscriptions.all()
    assert [subscription.show for subscription in listed] == [DAILY, LEX]
    assert listed[0].last_seen is None and listed[0].added.tzinfo is not None
    assert listed[0].since == listed[0].added


def test_add_does_not_repeat_a_subscription():
    subscriptions.add(DAILY)
    assert subscriptions.add(DAILY) is False
    assert len(subscriptions.all()) == 1


def test_remove_drops_the_show_and_reports_one_it_did_not_have():
    subscriptions.add(DAILY)
    subscriptions.add(LEX)
    assert subscriptions.remove(DAILY.feed_url) is True
    assert subscriptions.remove(DAILY.feed_url) is False
    assert [subscription.show for subscription in subscriptions.all()] == [LEX]


def test_mark_seen_moves_last_seen_forward_only():
    subscriptions.add(DAILY)
    assert subscriptions.mark_seen(DAILY.feed_url, date(5)) is True
    assert subscriptions.get(DAILY.feed_url).last_seen == date(5)
    assert subscriptions.get(DAILY.feed_url).since == date(5)
    assert subscriptions.mark_seen(DAILY.feed_url, date(3)) is False
    assert subscriptions.mark_seen(DAILY.feed_url, None) is False
    assert subscriptions.get(DAILY.feed_url).last_seen == date(5)


def test_mark_seen_of_a_show_not_followed_writes_nothing():
    assert subscriptions.mark_seen(DAILY.feed_url, date(5)) is False
    assert not subscriptions.subscriptions_path().exists()
    assert subscriptions.get(DAILY.feed_url) is None


def test_the_file_is_replaced_atomically(monkeypatch):
    subscriptions.add(DAILY)

    def fail(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(utils.os, "replace", fail)
    with pytest.raises(OSError):
        subscriptions.add(LEX)
    assert [subscription.show for subscription in subscriptions.all()] == [DAILY]
    assert [path.name for path in subscriptions.subscriptions_path().parent.iterdir()] == ["subscriptions.json"]


@pytest.mark.parametrize(
    "content",
    ["{not json", "[1, 2]", '{"https://a.example/rss": 3}', "\udcff"],
    ids=["not-json", "a-list", "an-entry-not-a-map", "undecodable"],
)
def test_a_corrupt_file_reads_as_empty_with_one_warning_line(content, capsys):
    path = subscriptions.subscriptions_path()
    path.parent.mkdir(parents=True)
    path.write_bytes(content.encode("utf-8", "surrogateescape"))
    assert subscriptions.all() == []
    warning = capsys.readouterr().err
    assert warning.startswith("Ignoring unreadable subscriptions file") and warning.count("\n") == 1


def test_odd_fields_in_an_entry_read_as_missing():
    path = subscriptions.subscriptions_path()
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({DAILY.feed_url: {"name": 7, "id": "1", "added": "yesterday", "last_seen": 3}}))
    [subscription] = subscriptions.all()
    assert subscription.show == Show("Untitled", "Unknown", DAILY.feed_url, None, 0)
    assert subscription.last_seen is None and subscription.added.tzinfo is not None


def test_new_text():
    assert subscriptions.new_text(3) == "3 new"
    assert subscriptions.new_text(0) == "0 new"
    assert subscriptions.new_text(None) == "failed"


# --- AC2: refresh -----------------------------------------------------------------


def opener():
    return FakeOpener({
        "daily.example.org": feed(DAILY, 2, 9, 4),
        "lex.example.org": feed(LEX, 10, 1),
        "gone.example.org": podcasts.PodcastError("HTTP 404 from gone.example.org"),
    })


def test_refresh_counts_the_episodes_newer_than_last_seen_or_than_added_and_sorts_by_newest_episode():
    subscribe(DAILY, date(1))
    subscribe(LEX, date(1))
    subscriptions.mark_seen(LEX.feed_url, date(1))
    subscriptions.mark_seen(DAILY.feed_url, date(4))
    fake = opener()
    results = subscriptions.refresh(opener=fake)
    assert [(show.name, [video.title for video in videos], count) for show, videos, count in results] == [
        ("Lex Fridman Podcast", ["Lex Fridman Podcast 10", "Lex Fridman Podcast 1"], 1),  # day 1 is last_seen, not new
        ("The Daily", ["The Daily 9", "The Daily 4", "The Daily 2"], 1),
    ]
    assert sorted(fake.urls) == [DAILY.feed_url, LEX.feed_url]


def test_refresh_before_a_show_is_opened_counts_what_came_after_it_was_added():
    subscribe(DAILY, date(3))
    [(_, _, count)] = subscriptions.refresh(opener=opener())
    assert count == 2  # days 9 and 4


def test_a_failed_feed_is_listed_last_with_no_episodes_and_no_count_and_reported():
    subscribe(BROKEN, date(1))
    subscribe(DAILY, date(1))
    failed = []
    results = subscriptions.refresh(opener=opener(), on_error=lambda show, error: failed.append((show, str(error))))
    assert [(show, count) for show, _, count in results] == [(DAILY, 3), (BROKEN, None)]
    assert results[1][1] == []
    assert failed == [(BROKEN, "HTTP 404 from gone.example.org")]


def test_refresh_keeps_the_newest_limit_episodes_and_dated_pairs():
    subscribe(DAILY, date(1))
    [(show, dated, count)] = subscriptions.refresh_dated(limit=2, opener=opener())
    assert [published for published, _ in dated] == [date(9), date(4)]
    assert count == 2


def test_refresh_counts_every_new_episode_of_a_long_feed():
    subscribe(DAILY, date(1, hour=0))
    [(_, videos, count)] = subscriptions.refresh(opener=FakeOpener({"daily.example.org": feed(DAILY, *range(25, 0, -1))}))
    assert (len(videos), count) == (25, 25)


def test_refresh_fetches_in_at_most_four_threads(monkeypatch):
    shows = [Show(f"Show {n}", "a", f"https://daily.example.org/{n}", None, n) for n in range(6)]
    for show in shows:
        subscriptions.add(show)
    workers = []
    real_pool = subscriptions.concurrent.futures.ThreadPoolExecutor
    monkeypatch.setattr(subscriptions.concurrent.futures, "ThreadPoolExecutor",
                        lambda max_workers: workers.append(max_workers) or real_pool(max_workers))
    fake = opener()
    results = subscriptions.refresh(opener=fake)
    assert workers == [4]
    assert len(results) == 6 and len(fake.urls) == 6
    assert threading.get_ident() not in fake.threads


def test_refresh_without_subscriptions_is_empty():
    assert subscriptions.refresh(opener=opener()) == []


def test_is_new():
    assert subscriptions.is_new(date(5), date(4))
    assert not subscriptions.is_new(date(4), date(4))
    assert not subscriptions.is_new(None, date(4))
    assert not subscriptions.is_new(date(5), None)


# --- OPML ----------------------------------------------------------------------

OPML = (pathlib.Path(__file__).parent / "fixtures" / "subscriptions.opml").read_bytes()  # an app's export: folders, odd outlines

EXPORTED = """<?xml version="1.0" encoding="UTF-8"?>
<opml version="2.0">
  <head>
    <title>ttyplayer subscriptions</title>
    <dateCreated>Sun, 11 Oct 2026 12:00:00 +0000</dateCreated>
  </head>
  <body>
    <outline type="rss" text="Lex Fridman Podcast" title="Lex Fridman Podcast" xmlUrl="https://lex.example.org/rss" />
    <outline type="rss" text="Rock &amp; &quot;Roll&quot; &lt;3" title="Rock &amp; &quot;Roll&quot; &lt;3" xmlUrl="https://rock.example.org/rss?a=1&amp;b=2" />
    <outline type="rss" text="The Daily" title="The Daily" xmlUrl="https://daily.example.org/rss" />
  </body>
</opml>
"""


def test_export_opml_lists_every_show_by_name_escaped(monkeypatch):
    monkeypatch.setattr(subscriptions, "_now", lambda: date(11))
    for show in (DAILY, Show('Rock & "Roll" <3', "Band", "https://rock.example.org/rss?a=1&b=2", None, 4), LEX):
        subscriptions.add(show)
    assert subscriptions.export_opml() == EXPORTED


def test_export_opml_without_subscriptions_has_an_empty_body():
    assert "<body />" in subscriptions.export_opml()


def test_an_export_imports_back_as_the_same_shows(tmp_path):
    subscriptions.add(DAILY)
    subscriptions.add(LEX)
    other = tmp_path / "other.json"
    assert subscriptions.import_opml(subscriptions.export_opml(), other) == (2, 0, 0)
    assert [(each.show.name, each.show.feed_url) for each in subscriptions.all(other)] == [
        (LEX.name, LEX.feed_url), (DAILY.name, DAILY.feed_url),
    ]


def test_import_opml_flattens_folders_skips_duplicates_and_outlines_without_a_feed():
    subscriptions.add(LEX)
    assert subscriptions.import_opml(OPML) == (3, 2, 2)
    shows = [each.show for each in subscriptions.all()]
    assert [(show.name, show.feed_url) for show in shows] == [
        ("Lex Fridman Podcast", LEX.feed_url),  # already followed: kept as it was
        ("The Daily", "https://daily.example.org/rss"),
        ("Science & Stuff", "https://science.example.org/feed.xml"),
        ("Untitled", "https://nameless.example.org/rss"),
    ]
    assert shows[1] == Show("The Daily", "Unknown", "https://daily.example.org/rss", None, 0)
    assert subscriptions.import_opml(OPML) == (0, 5, 2)


def test_opml_shows_lists_what_import_would_add_and_writes_nothing():
    shows, already, skipped = subscriptions.opml_shows(OPML)
    assert ([show.name for show in shows], already, skipped) == (
        ["The Daily", "Lex Fridman Podcast", "Science & Stuff", "Untitled"], 1, 2,
    )
    assert not subscriptions.subscriptions_path().exists()


def test_import_opml_follows_a_feed_outline_that_holds_others():
    opml = (
        '<opml><body><outline text="Parent" xmlUrl="https://parent.example.org/rss">'
        '<outline text="Child" xmlUrl="https://child.example.org/rss"/></outline></body></opml>'
    )
    assert subscriptions.import_opml(opml) == (2, 0, 0)
    assert [each.show.name for each in subscriptions.all()] == ["Parent", "Child"]


def test_import_opml_counts_a_malformed_feed_address_as_without_a_feed():
    opml = '<opml><body><outline text="Bad" xmlUrl="https://[bad/feed"/></body></opml>'
    assert subscriptions.import_opml(opml) == (0, 0, 1)


def test_import_opml_caps_text_at_5_mb_of_utf8():
    opml = "<opml><body>" + "é" * (podcasts.MAX_BYTES // 2) + "</body></opml>"  # fewer characters than bytes
    assert len(opml) < podcasts.MAX_BYTES
    with pytest.raises(podcasts.PodcastError, match=r"^not an OPML file \(larger than 5 MB\)$"):
        subscriptions.import_opml(opml)


def test_refresh_reads_the_author_of_an_imported_show_from_its_feed():
    opml = f'<opml><body><outline text="The Daily" xmlUrl="{DAILY.feed_url}"/></body></opml>'
    subscriptions.import_opml(opml)
    [(show, _, _)] = subscriptions.refresh_dated(opener=opener())
    assert show.author == "The New York Times"
    assert subscriptions.get(DAILY.feed_url).show.author == "The New York Times"


def test_refresh_keeps_a_known_author_and_writes_nothing():
    subscriptions.add(Show("The Daily", "Someone else", DAILY.feed_url, None, 1))
    before = subscriptions.subscriptions_path().read_text()
    [(show, _, _)] = subscriptions.refresh_dated(opener=opener())
    assert show.author == "Someone else"
    assert subscriptions.subscriptions_path().read_text() == before


def test_import_opml_takes_text_too_and_a_name_from_title():
    opml = '<opml><body><outline title="Only a title" xmlUrl="https://t.example.org/rss"/></body></opml>'
    assert subscriptions.import_opml(opml) == (1, 0, 0)
    assert subscriptions.all()[0].show.name == "Only a title"


@pytest.mark.parametrize("body, reason", [
    (b"<opml><body>", "not an OPML file (no element found"),
    (b"<rss><channel/></rss>", "not an OPML file (no body)"),
    (b"<opml>\n<body>\n</opml>", "not an OPML file (mismatched tag"),
    (b'<!DOCTYPE opml [<!ENTITY x SYSTEM "file:///etc/passwd">]><opml><body><outline text="&x;"/></body></opml>',
     "not an OPML file (it declares entities)"),
    (b"<opml>" + b" " * podcasts.MAX_BYTES + b"</opml>", "not an OPML file (larger than 5 MB)"),
], ids=["truncated", "not-opml", "mismatched", "entities", "too-large"])  # ids: the 5 MB body must not become one
def test_import_opml_refuses_what_is_not_opml_in_one_line(body, reason):
    with pytest.raises(podcasts.PodcastError) as error:
        subscriptions.import_opml(body)
    assert str(error.value).startswith(reason) and "\n" not in str(error.value)
    assert not subscriptions.subscriptions_path().exists()
