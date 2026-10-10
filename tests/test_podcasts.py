import email.message
import io
import pathlib
import socket
import urllib.error
import urllib.parse

import pytest

from ttyplayer import podcasts
from ttyplayer.models import Video

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
SEARCH = (FIXTURES / "itunes_search.json").read_bytes()  # the shape in Apple's Search API docs
ITUNES_FEED = (FIXTURES / "rss_itunes.xml").read_bytes()  # an RSS feed with the itunes: namespace
PLAIN_FEED = (FIXTURES / "rss_plain.xml").read_bytes()  # RSS 2.0 alone
FEED_URL = "https://lexfridman.com/feed/podcast/"


class FakeOpener:
    """Stands in for urllib.request.urlopen: answers by host with bytes, or raises; records (url, headers, timeout)."""

    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def __call__(self, request, timeout):
        self.calls.append((request.full_url, dict(request.header_items()), timeout))
        answer = self.answers[urllib.parse.urlsplit(request.full_url).netloc]
        if isinstance(answer, Exception):
            raise answer
        return io.BytesIO(answer)


def opener(feed=ITUNES_FEED, search=SEARCH):
    return FakeOpener({"itunes.apple.com": search, "lexfridman.com": feed, "plain.example.org": PLAIN_FEED})


@pytest.fixture(autouse=True)
def data_home(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    return tmp_path


# --- AC1: shows from iTunes ---------------------------------------------------


def test_search_shows_asks_itunes_for_podcasts_with_a_user_agent_and_a_5s_timeout():
    fake = opener()
    shows = podcasts.search_shows("lex fridman", 5, fake)
    url, headers, timeout = fake.calls[0]
    parts = urllib.parse.urlsplit(url)
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == "https://itunes.apple.com/search"
    assert dict(urllib.parse.parse_qsl(parts.query)) == {"media": "podcast", "term": "lex fridman", "limit": "5"}
    assert headers["User-agent"].startswith("ttyplayer/")
    assert timeout == 5
    assert shows[0] == podcasts.Show(
        name="Lex Fridman Podcast",
        author="Lex Fridman",
        feed_url="https://lexfridman.com/feed/podcast/",
        artwork="https://is1-ssl.mzstatic.com/image/thumb/Podcasts/lex/100x100bb.jpg",
        id=1434243584,
    )


def test_search_shows_leaves_out_a_show_without_a_feed():
    shows = podcasts.search_shows("anything", 5, opener())
    assert [show.name for show in shows] == ["Lex Fridman Podcast", "The Daily"]


def http_error(url, code):
    return urllib.error.HTTPError(url, code, "Forbidden", email.message.Message(), io.BytesIO(b""))


@pytest.mark.parametrize(
    "answer, message",
    [
        (http_error("https://itunes.apple.com/search", 403), "HTTP 403 from itunes.apple.com"),
        (urllib.error.URLError(socket.gaierror(-2, "Name or service not known")), "Name or service not known"),
        (TimeoutError("timed out"), "timed out"),
        (b"<html>not json</html>", "unexpected reply from iTunes"),
        (b'{"errorMessage": "Invalid value(s) for key(s): [mediaType]"}', "unexpected reply from iTunes"),
        (b'{"results": null}', "unexpected reply from iTunes"),
        (b'[{"feedUrl": "https://lexfridman.com/feed/podcast/"}]', "unexpected reply from iTunes"),
        (b"[" * 100_000, "unexpected reply from iTunes"),
        (urllib.error.URLError("Name or service\nnot known"), "Name or service not known"),
    ],
)
def test_a_failed_search_is_one_line_of_podcast_error(answer, message):
    with pytest.raises(podcasts.PodcastError) as caught:
        podcasts.search_shows("x", 5, opener(search=answer))
    assert message in str(caught.value) and "\n" not in str(caught.value)


def test_search_shows_leaves_out_results_that_are_not_shows_and_reads_odd_fields_as_missing():
    reply = b"""{"results": [null, 3, "x", {"feedUrl": 7}, {"feedUrl": "https://plain.example.org/rss",
        "collectionName": ["not", "text"], "artistName": "  ", "artworkUrl100": {}, "collectionId": "12"}]}"""
    shows = podcasts.search_shows("x", 5, opener(search=reply))
    assert shows == [podcasts.Show("Untitled", "Unknown", "https://plain.example.org/rss", None, 0)]


# --- AC1: episodes from the feed -----------------------------------------------


def test_episodes_of_an_itunes_feed_are_audio_videos_newest_first():
    videos = podcasts.episodes(FEED_URL, 10, opener())
    assert [video.title for video in videos] == [
        "#402 – Newest Episode",
        "#401 – Middle Episode",
        "#400 – Older Episode",
        "Seconds only",
    ]  # the video and the post without an enclosure are left out
    assert videos[0] == Video(
        id=podcasts.hashlib.sha1(b"https://media.example.com/lex/402.mp3").hexdigest()[:11],
        title="#402 – Newest Episode",
        uploader="Lex Fridman Podcast",
        duration=9000,
        source="podcast",
        link="https://media.example.com/lex/402.mp3",
        thumbnail="https://lexfridman.com/402.jpg",  # the item's own image
    )
    assert videos[0].url == "https://media.example.com/lex/402.mp3"
    assert [video.duration for video in videos] == [9000, 45 * 60 + 30, 3723, 3600]  # H:MM:SS, MM:SS, H:MM:SS, s
    assert videos[1].thumbnail == "https://lexfridman.com/cover.jpg"  # the show's image


def test_episodes_are_capped_at_limit_after_sorting():
    assert [video.title for video in podcasts.episodes(FEED_URL, 1, opener())] == ["#402 – Newest Episode"]


def test_episodes_of_a_plain_feed_have_no_duration_and_the_channel_image():
    videos = podcasts.episodes("https://plain.example.org/rss", 10, opener())
    assert [video.title for video in videos] == ["Episode two", "Episode one, no type"]  # the PDF is left out
    assert [video.duration for video in videos] == [None, None]
    assert {video.thumbnail for video in videos} == {"https://plain.example.org/logo.png"}
    assert videos[1].link == "https://plain.example.org/files/one.mp3?source=rss"  # .mp3 without a type is audio
    assert {video.uploader for video in videos} == {"Plain Radio Show"}


def test_dated_episodes_carry_the_publication_date():
    dated = podcasts.dated_episodes(FEED_URL, 10, opener())
    assert dated[0][0].isoformat() == "2026-10-09T18:00:00+00:00"


@pytest.mark.parametrize(
    "value, seconds",
    [("1:02:03", 3723), ("45:30", 2730), ("3600", 3600), ("90.5", 90), ("", None), (None, None), ("soon", None),
     ("1:2:3:4", None), ("NaN", None), ("inf", None), ("1:inf", None), ("1e308:1e308:1e308", None), ("-5", None)],
)
def test_parse_duration(value, seconds):
    assert podcasts.parse_duration(value) == seconds


@pytest.mark.parametrize(
    "body, message",
    [
        (b"<html><body>", "not an RSS feed"),
        (b"<feed xmlns='http://www.w3.org/2005/Atom'/>", "not an RSS feed"),
        (b'<?xml version="1.0" encoding="bogus"?><rss/>', "not an RSS feed"),
        (b'<?xml version="1.0" encoding="UTF-7"?><rss><channel/></rss>', "not an RSS feed"),
    ],
)
def test_a_body_that_is_not_rss_is_a_podcast_error(body, message):
    with pytest.raises(podcasts.PodcastError, match=message):
        podcasts.episodes(FEED_URL, 10, opener(feed=body))


@pytest.mark.parametrize("feed_url", ["not a url", "file:///etc/passwd", "ftp://example.org/feed", ""])
def test_a_feed_url_that_is_not_a_web_address_is_a_podcast_error_without_a_request(feed_url):
    fake = opener()
    with pytest.raises(podcasts.PodcastError, match="not a web address"):
        podcasts.episodes(feed_url, 10, fake)
    assert fake.calls == []


def test_a_request_urllib_cannot_build_is_a_podcast_error():
    with pytest.raises(podcasts.PodcastError):
        podcasts.episodes("http://[::1/feed", 10, opener())


def test_a_nan_or_infinite_duration_is_none_and_the_episode_still_listed():
    feed = b"""<rss xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"><channel><title>S</title>
        <item><title>A</title><enclosure url="https://plain.example.org/a.mp3" type="audio/mpeg"/>
        <itunes:duration>NaN</itunes:duration></item>
        <item><title>B</title><enclosure url="https://plain.example.org/b.mp3" type="audio/mpeg"/>
        <itunes:duration>inf</itunes:duration></item></channel></rss>"""
    videos = podcasts.episodes(FEED_URL, 10, opener(feed=feed))
    assert [(video.title, video.duration) for video in videos] == [("A", None), ("B", None)]


def test_dates_without_a_zone_or_in_far_off_years_still_sort():
    feed = b"""<rss><channel><title>S</title>
        <item><title>Old</title><enclosure url="https://plain.example.org/a.mp3" type="audio/mpeg"/>
        <pubDate>Mon, 01 Jan 0100 00:00:00 -0000</pubDate></item>
        <item><title>New</title><enclosure url="https://plain.example.org/b.mp3" type="audio/mpeg"/>
        <pubDate>Fri, 31 Dec 9999 23:59:59 -2359</pubDate></item></channel></rss>"""
    dated = podcasts.dated_episodes(FEED_URL, 10, opener(feed=feed))
    assert [video.title for _, video in dated] == ["New", "Old"]
    assert all(published.tzinfo is not None for published, _ in dated)


def test_a_feed_over_5_mb_is_refused_unparsed():
    huge = b"<rss><channel>" + b" " * podcasts.MAX_BYTES + b"</channel></rss>"
    with pytest.raises(podcasts.PodcastError, match="larger than 5 MB"):
        podcasts.episodes(FEED_URL, 10, opener(feed=huge))


def test_a_feed_with_an_external_entity_does_not_read_it(tmp_path):
    secret = tmp_path / "secret"
    secret.write_text("TOP SECRET")
    body = (
        f'<?xml version="1.0"?><!DOCTYPE rss [<!ENTITY x SYSTEM "file://{secret}">]>'
        '<rss><channel><title>t</title><item><title>&x;</title>'
        '<enclosure url="https://a.example/1.mp3" type="audio/mpeg"/></item></channel></rss>'
    ).encode()
    try:
        videos = podcasts.episodes(FEED_URL, 10, opener(feed=body))
    except podcasts.PodcastError:
        return  # refused: fine too
    assert "TOP SECRET" not in videos[0].title


def test_latest_is_the_first_shows_newest_episodes():
    fake = opener()
    show, videos = podcasts.latest("lex", 2, fake)
    assert show.name == "Lex Fridman Podcast"
    assert [video.title for video in videos] == ["#402 – Newest Episode", "#401 – Middle Episode"]
    assert dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(fake.calls[0][0]).query))["limit"] == "1"


def test_latest_without_a_show_is_none_and_nothing():
    assert podcasts.latest("zzz", 2, opener(search=b'{"resultCount": 0, "results": []}')) == (None, [])


def test_the_last_search_is_remembered_by_number():
    shows = podcasts.search_shows("anything", 5, opener())
    podcasts.remember(shows)
    assert podcasts.remembered(2) == shows[1]
    assert podcasts.remembered(3) is None
    assert podcasts.remembered(0) is None


# --- AC2: an episode is stored like any other video -----------------------------


def test_an_episode_round_trips_through_history_favorites_and_playlists():
    from ttyplayer import favorites, history, playlists

    episode = podcasts.episodes(FEED_URL, 1, opener())[0]
    history.record(episode)
    favorites.add(episode)
    playlists.create("pods")
    playlists.add("pods", [episode])
    assert history.load() == [episode]
    assert favorites.load() == [episode]
    assert playlists.load("pods") == [episode]
    assert history.load()[0].url == episode.link
