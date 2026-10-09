import pytest
from yt_dlp.utils import DownloadError

from ttyplayer import youtube
from ttyplayer.youtube import is_url


def test_is_url_is_true():
    assert is_url("https://www.youtube.com") is True


def test_is_url_is_false():
    assert is_url("nothttp") is False


# Flat search and playlist entries, as yt-dlp gives them: ie_key says what each one is.
ENTRY = {"ie_key": "Youtube", "id": "abc", "title": "Song", "uploader": "Band", "duration": 200}
OTHER = {"ie_key": "Youtube", "id": "def", "title": "Other", "uploader": "Band", "duration": 100}
CHANNEL = {"ie_key": "YoutubeTab", "_type": "url", "id": "UCMK037TfgXabcdefghijklm", "title": "Band"}
PLAYLIST = {"ie_key": "YoutubeTab", "_type": "url", "id": "PLabcdefghijklmnopqrstuvwxyz012345", "title": "Band mix"}


class FakeYoutubeDL:
    """Stands in for yt_dlp.YoutubeDL; records the target and returns canned info."""

    info = None
    error = None
    calls = []

    def __init__(self, opts):
        self.opts = opts

    def extract_info(self, target, download):
        FakeYoutubeDL.calls.append((target, download))
        if FakeYoutubeDL.error is not None:
            raise FakeYoutubeDL.error
        return FakeYoutubeDL.info


@pytest.fixture
def fake_ydl(monkeypatch):
    FakeYoutubeDL.info = None
    FakeYoutubeDL.error = None
    FakeYoutubeDL.calls = []
    monkeypatch.setattr(youtube, "YoutubeDL", FakeYoutubeDL)
    return FakeYoutubeDL


def test_search_builds_videos_from_entries(fake_ydl):
    fake_ydl.info = {"entries": [ENTRY, OTHER]}
    videos = youtube.search("song", limit=2)
    assert [v.id for v in videos] == ["abc", "def"]
    assert videos[0].title == "Song"


def test_search_keeps_only_the_videos(fake_ydl):
    fake_ydl.info = {"entries": [CHANNEL, ENTRY, PLAYLIST, None, OTHER]}
    assert [v.id for v in youtube.search("band", limit=5)] == ["abc", "def"]


def test_search_of_only_channels_and_playlists_finds_nothing(fake_ydl):
    fake_ydl.info = {"entries": [CHANNEL, PLAYLIST]}
    assert youtube.search("band", limit=2) == []


def test_search_asks_for_the_requested_number_of_results(fake_ydl):
    fake_ydl.info = {"entries": []}
    youtube.search("some song", limit=7)
    assert fake_ydl.calls == [("ytsearch7:some song", False)]


def test_fetch_single_video_returns_a_one_item_list(fake_ydl):
    fake_ydl.info = ENTRY
    videos = youtube.fetch("https://www.youtube.com/watch?v=abc")
    assert [v.id for v in videos] == ["abc"]


def test_fetch_playlist_returns_every_entry(fake_ydl):
    fake_ydl.info = {"entries": [ENTRY, OTHER], "playlist_count": 2}
    videos = youtube.fetch("https://www.youtube.com/playlist?list=xyz")
    assert [v.id for v in videos] == ["abc", "def"]


def test_search_wraps_download_errors(fake_ydl):
    fake_ydl.error = DownloadError("ERROR: no internet")
    with pytest.raises(youtube.YouTubeError):
        youtube.search("song")


def test_fetch_wraps_download_errors(fake_ydl):
    fake_ydl.error = DownloadError("ERROR: video unavailable")
    with pytest.raises(youtube.YouTubeError):
        youtube.fetch("https://www.youtube.com/watch?v=abc")


def test_fetch_playlist_returns_the_title_and_every_entry(fake_ydl):
    fake_ydl.info = {"title": "Road Trip", "entries": [ENTRY, None, OTHER]}
    title, videos = youtube.fetch_playlist("https://www.youtube.com/playlist?list=xyz")
    assert title == "Road Trip"
    assert [v.id for v in videos] == ["abc", "def"]


def test_fetch_playlist_of_a_single_video_has_no_title(fake_ydl):
    fake_ydl.info = ENTRY
    title, videos = youtube.fetch_playlist("https://www.youtube.com/watch?v=abc")
    assert title is None
    assert [v.id for v in videos] == ["abc"]


def test_fetch_playlist_wraps_download_errors(fake_ydl):
    fake_ydl.error = DownloadError("ERROR: playlist does not exist")
    with pytest.raises(youtube.YouTubeError, match="^playlist does not exist$"):
        youtube.fetch_playlist("https://www.youtube.com/playlist?list=nope")


SC_ENTRY = {"ie_key": "Soundcloud", "id": "123", "title": "Roygbiv", "uploader": "warp", "duration": 151.0,
            "url": "https://api.soundcloud.com/tracks/123", "webpage_url": "https://soundcloud.com/warp/roygbiv"}
SC_SET = {"ie_key": "SoundcloudSet", "_type": "url", "id": "987", "title": "Album"}


def test_search_on_soundcloud_asks_yt_dlp_for_scsearch(fake_ydl):
    fake_ydl.info = {"entries": [SC_ENTRY, SC_SET]}
    videos = youtube.search("boards of canada", 7, source="soundcloud")
    assert fake_ydl.calls == [("scsearch7:boards of canada", False)]
    assert [(v.source, v.url) for v in videos] == [("soundcloud", "https://soundcloud.com/warp/roygbiv")]


def test_search_defaults_to_youtube(fake_ydl):
    fake_ydl.info = {"entries": []}
    youtube.search("lofi", 3)
    assert fake_ydl.calls == [("ytsearch3:lofi", False)]


def test_search_of_an_unknown_source_names_the_valid_ones(fake_ydl):
    with pytest.raises(ValueError, match="Unknown source 'bandcamp'; valid sources: youtube, soundcloud"):
        youtube.search("lofi", 3, source="bandcamp")
    assert fake_ydl.calls == []


def test_every_source_has_a_prefix_and_a_name():
    assert sorted(youtube.PREFIXES.values()) == sorted(youtube.SOURCES) == sorted(youtube.SOURCE_NAMES)


@pytest.mark.parametrize(
    "text, source, query",
    [
        ("sc: boards of canada", "soundcloud", "boards of canada"),
        ("SC:boards", "soundcloud", "boards"),
        ("yt: boards", "youtube", "boards"),
        ("boards of canada", "soundcloud", "boards of canada"),
        ("https://soundcloud.com/warp/roygbiv", "soundcloud", "https://soundcloud.com/warp/roygbiv"),
        ("bc: boards", "soundcloud", "bc: boards"),
    ],
)
def test_split_source_reads_a_two_letter_prefix(text, source, query):
    assert youtube.split_source(text, "soundcloud") == (source, query)
