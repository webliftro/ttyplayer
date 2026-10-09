from ttyplayer.models import Video

def test_url_is_built_from_id():
    video = Video(id="abc123", title="t", uploader="u", duration=10)
    result = video.url
    expected = "https://www.youtube.com/watch?v=abc123"
    assert result == expected


SC_LINK = "https://soundcloud.com/warp-records/boards-of-canada-roygbiv"


def test_a_video_is_youtube_without_a_link_by_default():
    video = Video(id="abc123", title="t", uploader="u", duration=10)
    assert (video.source, video.link) == ("youtube", None)


def test_a_soundcloud_url_is_its_link():
    video = Video(id="123", title="t", uploader="u", duration=10, source="soundcloud", link=SC_LINK)
    assert video.url == SC_LINK


def test_tagged_title_marks_soundcloud_only():
    assert Video(id="1", title="Roygbiv", uploader="u", duration=1, source="soundcloud", link=SC_LINK).tagged_title == "SC Roygbiv"
    assert Video(id="abc123", title="Roygbiv", uploader="u", duration=1).tagged_title == "Roygbiv"
