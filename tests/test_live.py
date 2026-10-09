"""Live tests against the real YouTube; opt in with TTYPLAYER_LIVE=1.

They go through ttyplayer.youtube only, so a yt-dlp change shows up as a ttyplayer failure.
"""

import os

import pytest

from ttyplayer import youtube

pytestmark = pytest.mark.skipif(
    os.environ.get("TTYPLAYER_LIVE") != "1",
    reason="set TTYPLAYER_LIVE=1 to run the live YouTube tests",
)

KNOWN_ID = "dQw4w9WgXcQ"


def watch_url(video_id):
    return f"https://www.youtube.com/watch?v={video_id}"


def test_search_returns_the_requested_number_of_videos():
    videos = youtube.search("never gonna give you up", 3)
    assert len(videos) == 3
    for video in videos:
        assert video.id
        assert video.title
        assert video.duration is None or isinstance(video.duration, int)


def test_fetch_a_known_video():
    url = watch_url(KNOWN_ID)
    assert youtube.is_url(url)
    [video] = youtube.fetch(url)
    assert video.id == KNOWN_ID
    assert video.uploader
    assert video.duration > 0


def test_fetch_a_missing_video_raises_a_clean_error():
    with pytest.raises(youtube.YouTubeError) as error:
        youtube.fetch(watch_url("00000000000"))
    message = str(error.value)
    assert "\n" not in message
    assert not message.startswith("ERROR:")


def test_fetch_playlist_of_a_known_video_has_no_title():
    title, [video] = youtube.fetch_playlist(watch_url(KNOWN_ID))
    assert title is None
    assert video.id == KNOWN_ID
