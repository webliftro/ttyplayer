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


EPISODE = Video(id="0123456789a", title="Ep 1", uploader="Show", duration=60, source="podcast",
                link="https://media.example.com/ep1.mp3")


def test_a_podcast_url_is_its_enclosure_and_its_title_is_tagged_pc():
    assert EPISODE.url == "https://media.example.com/ep1.mp3"
    assert EPISODE.tagged_title == "PC Ep 1"
