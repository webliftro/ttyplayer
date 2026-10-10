import email.message
import hashlib
import io
import json
import pathlib
import sys
import urllib.error
import urllib.parse

import pytest

from ttyplayer import lyrics

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
GET = (FIXTURES / "lrclib_get.json").read_bytes()  # the shapes in https://lrclib.net/docs
SEARCH = (FIXTURES / "lrclib_search.json").read_bytes()
NOT_FOUND = (FIXTURES / "lrclib_not_found.json").read_bytes()


def http_error(url, code, body=NOT_FOUND):
    return urllib.error.HTTPError(url, code, "Not Found", email.message.Message(), io.BytesIO(body))


class FakeOpener:
    """Stands in for urllib.request.urlopen: answers by endpoint (get, search) with bytes, or raises;
    records (endpoint, query, headers, timeout) of every call."""

    def __init__(self, get=GET, search=SEARCH):
        self.answers = {"get": get, "search": search}
        self.calls = []

    def __call__(self, request, timeout):
        url = urllib.parse.urlsplit(request.full_url)
        endpoint = url.path.rsplit("/", 1)[1]
        self.calls.append((endpoint, dict(urllib.parse.parse_qsl(url.query)), dict(request.header_items()), timeout))
        answer = self.answers[endpoint]
        if isinstance(answer, int):
            raise http_error(request.full_url, answer)
        if isinstance(answer, Exception):
            raise answer
        return io.BytesIO(answer)

    def endpoints(self):
        return [call[0] for call in self.calls]


@pytest.fixture(autouse=True)
def data_home(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    return tmp_path


# --- AC1: guessing the artist and the track ---------------------------------

GUESSES = [
    ("Rick Astley - Never Gonna Give You Up (Official Music Video)", "Rick Astley", ("Rick Astley", "Never Gonna Give You Up")),
    ("Daft Punk – Get Lucky [Lyrics]", "Daft Punk", ("Daft Punk", "Get Lucky")),
    ("Bohemian Rhapsody | Queen", "Queen Official", ("Queen", "Bohemian Rhapsody")),
    ("Adele - Hello | Official Video", "AdeleVEVO", ("Adele", "Hello")),
    ("Yesterday (Live at Royal Albert Hall)", "Paul McCartney", ("Paul McCartney", "Yesterday")),
    ("Blinding Lights", "The Weeknd - Topic", ("The Weeknd", "Blinding Lights")),
    ("Shake It Off", "TaylorSwiftVEVO", ("Taylor Swift", "Shake It Off")),
    ("Halsey - Without Me (Audio)", "HalseyVEVO", ("Halsey", "Without Me")),
    ('Coldplay - "Yellow" (Official Video) [HD]', "Coldplay", ("Coldplay", "Yellow")),
    ("Nirvana — Smells Like Teen Spirit (Remastered)", "Nirvana", ("Nirvana", "Smells Like Teen Spirit")),
    ("Billie Eilish - bad guy (feat. Justin Bieber)", "Billie Eilish", ("Billie Eilish", "bad guy (feat. Justin Bieber)")),
    ("Lose Yourself [HD]", "Eminem", ("Eminem", "Lose Yourself")),
    ("AC/DC - Back In Black (Official 4K Video)", "acdcVEVO", ("AC/DC", "Back In Black")),
    ("Dua Lipa - Levitating (Official Lyric Video)", "Dua Lipa", ("Dua Lipa", "Levitating")),
    ("Jay-Z - Empire State of Mind", "JayZVEVO", ("Jay-Z", "Empire State of Mind")),
    ("lofi beats to study to", "", ("", "lofi beats to study to")),
]


@pytest.mark.parametrize(("title", "uploader", "expected"), GUESSES)
def test_guess_splits_real_world_titles(title, uploader, expected):
    assert lyrics.guess(title, uploader) == expected


def test_guess_names_the_track_for_the_user():
    assert lyrics.not_found("Queen", "Bohemian Rhapsody") == 'No lyrics found for "Queen – Bohemian Rhapsody"'
    assert lyrics.not_found("", "lofi") == 'No lyrics found for "lofi"'


# --- AC2: LRC ---------------------------------------------------------------


def test_parse_lrc_reads_both_stamp_forms_several_tags_and_blank_lines():
    text = "[ar:Someone]\n[00:01.50]first\n\n[00:03]second\n[00:05.25][01:00.00]chorus\n[00:07.00]\nno stamp\n"
    assert lyrics.parse_lrc(text) == [(1.5, "first"), (3.0, "second"), (5.25, "chorus"), (7.0, ""), (60.0, "chorus")]


@pytest.mark.parametrize("text", [None, "", "just words\nno stamps"])
def test_parse_lrc_of_nothing_is_empty(text):
    assert lyrics.parse_lrc(text) == []


@pytest.mark.parametrize(("position", "expected"), [(None, None), (0, None), (1.5, 0), (2.9, 0), (3, 1), (99, 2)])
def test_current_line_is_the_last_one_started(position, expected):
    assert lyrics.current_line([(1.5, "a"), (3.0, "b"), (5.0, "c")], position) == expected


def test_text_is_the_plain_lyrics_else_the_synced_words():
    assert lyrics.text(lyrics.Lyrics([(1.0, "a")], "plain", "url")) == "plain"
    assert lyrics.text(lyrics.Lyrics([(1.0, "a"), (2.0, "b")], None, "url")) == "a\nb"


# --- AC2: lookup ------------------------------------------------------------


def test_lookup_gets_the_exact_match_with_the_duration_a_user_agent_and_a_3s_timeout():
    opener = FakeOpener()
    found = lyrics.lookup("Borislav Slavov", "I Want to Live", 233.4, opener)
    assert opener.calls == [
        (
            "get",
            {"artist_name": "Borislav Slavov", "track_name": "I Want to Live", "duration": "233"},
            {"User-agent": lyrics.user_agent()},
            3,
        )
    ]
    assert lyrics.user_agent().startswith("ttyplayer/") and "(https://github.com/webliftro/ttyplayer)" in lyrics.user_agent()
    assert found.synced == [
        (17.12, "I feel your breath upon my neck"),
        (21.4, "A soft caress as cold as death"),
        (25.88, ""),
        (28.03, "I want to live"),
    ]
    assert found.plain.startswith("I feel your breath")
    assert found.source_url == "https://lrclib.net/api/get/3396226"


def test_lookup_leaves_the_duration_out_when_it_is_unknown():
    opener = FakeOpener()
    lyrics.lookup("Borislav Slavov", "I Want to Live", None, opener)
    assert "duration" not in opener.calls[0][1]


def test_lookup_searches_after_a_404_and_takes_the_first_hit_within_5s():
    opener = FakeOpener(get=404)
    found = lyrics.lookup("Borislav Slavov", "I Want to Live", 230, opener)
    assert opener.endpoints() == ["get", "search"]
    assert opener.calls[1][1] == {"artist_name": "Borislav Slavov", "track_name": "I Want to Live"}
    assert found == lyrics.Lyrics(None, "I feel your breath upon my neck\nA soft caress as cold as death", "https://lrclib.net/api/get/3396226")


def test_lookup_takes_the_first_search_hit_when_the_duration_is_unknown():
    found = lyrics.lookup("Borislav Slavov", "I Want to Live", None, FakeOpener(get=404))
    assert found.source_url == "https://lrclib.net/api/get/3396300"


@pytest.mark.parametrize("search", [b"[]", SEARCH])  # no hits; hits, none within 5 s of 100
def test_lookup_without_a_close_enough_hit_is_none(search):
    assert lyrics.lookup("Borislav Slavov", "I Want to Live", 100, FakeOpener(get=404, search=search)) is None


def test_an_instrumental_has_no_lyrics():
    record = json.loads(GET) | {"instrumental": True, "plainLyrics": None, "syncedLyrics": None}
    assert lyrics.lookup("a", "b", None, FakeOpener(get=json.dumps(record).encode())) is None


@pytest.mark.parametrize(
    "get",
    [500, OSError("no route"), TimeoutError(), b"not json", b"[]", b'{"no": "id", "plainLyrics": "x"}'],
)
def test_lookup_is_none_on_any_network_or_json_error(get):
    opener = FakeOpener(get=get)
    assert lyrics.lookup("a", "b", 200, opener) is None
    assert opener.endpoints() == ["get"]  # only a 404 searches


# --- AC2: the cache ---------------------------------------------------------

TITLE, UPLOADER = "Borislav Slavov - I Want to Live (Official Audio)", "Borislav Slavov"


def cache_file(data_home, video_id):
    return data_home / "ttyplayer" / "lyrics" / hashlib.sha1(video_id.encode()).hexdigest()


def test_find_looks_up_the_guess_once_and_then_reads_the_cache(data_home):
    opener = FakeOpener()
    found = lyrics.find("abc", TITLE, UPLOADER, 233, opener)
    assert opener.calls[0][1]["track_name"] == "I Want to Live"
    path = cache_file(data_home, "abc")
    assert path.exists()
    if sys.platform != "win32":
        assert path.stat().st_mode & 0o777 == 0o600
    again = FakeOpener()
    assert lyrics.find("abc", TITLE, UPLOADER, 233, again) == found
    assert again.calls == []


def test_a_miss_is_cached_as_none_and_not_asked_again(data_home):
    opener = FakeOpener(get=404, search=b"[]")
    assert lyrics.find("abc", TITLE, UPLOADER, 233, opener) is None
    assert cache_file(data_home, "abc").read_text() == "none"
    again = FakeOpener()
    assert lyrics.find("abc", TITLE, UPLOADER, 233, again) is None
    assert again.calls == []


def test_a_network_error_is_not_cached_so_the_next_play_asks_again(data_home):
    assert lyrics.find("abc", TITLE, UPLOADER, 233, FakeOpener(get=OSError("offline"))) is None
    with pytest.raises(OSError):
        lyrics.fetch("abc", TITLE, UPLOADER, 233, FakeOpener(get=OSError("offline")))  # told apart from a miss
    assert not cache_file(data_home, "abc").exists()
    assert lyrics.find("abc", TITLE, UPLOADER, 233, FakeOpener()) is not None


def test_a_broken_cache_file_is_looked_up_again(data_home):
    path = cache_file(data_home, "abc")
    path.parent.mkdir(parents=True)
    path.write_text("{broken")
    assert lyrics.find("abc", TITLE, UPLOADER, 233, FakeOpener()).source_url.endswith("/3396226")


def test_find_still_answers_when_the_cache_cannot_be_written(data_home):
    (data_home / "ttyplayer").mkdir()
    (data_home / "ttyplayer" / "lyrics").write_text("a file where the dir should be")
    assert lyrics.find("abc", TITLE, UPLOADER, 233, FakeOpener()) is not None


# --- AC5: docs --------------------------------------------------------------


def test_the_docs_cover_the_tab_the_command_the_cache_and_credit_lrclib():
    root = pathlib.Path(__file__).parent.parent
    readme = (root / "README.md").read_text(encoding="utf-8")
    assert "ttyplayer lyrics" in readme and "[LRCLIB](https://lrclib.net)" in readme and "Lyrics tab (`6`)" in readme
    design = (root / "docs" / "tui-design.md").read_text(encoding="utf-8")
    for state in ("`Nothing playing`", "`Looking up…`", "`No lyrics found for", "`Lyrics are off (show_lyrics)`"):
        assert state in design
    architecture = (root / "docs" / "architecture.md").read_text(encoding="utf-8")
    assert "lyrics.py" in architecture and 'data_path("lyrics")' in architecture
