import pytest
from yt_dlp.utils import DownloadError

from ttyplayer import youtube
from ttyplayer.youtube import is_url


def test_is_url_is_true():
    assert is_url("https://www.youtube.com") is True


def test_is_url_is_false():
    assert is_url("nothttp") is False


ENTRY = {"id": "abc", "title": "Song", "uploader": "Band", "duration": 200}
OTHER = {"id": "def", "title": "Other", "uploader": "Band", "duration": 100}


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
