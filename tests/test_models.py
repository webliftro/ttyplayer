from ttyplayer.models import Video

def test_url_is_built_from_id():
    video = Video(id="abc123", title="t", uploader="u", duration=10)
    result = video.url
    expected = "https://www.youtube.com/watch?v=abc123"
    assert result == expected