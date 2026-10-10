from pathlib import Path

import pytest

from ttyplayer import utils


def test_parse_picks_empty():
    assert utils.parse_picks("", 3) is None


def test_parse_picks_valid():
    assert utils.parse_picks("1 3", 3) == [1, 3]


def test_parse_picks_non_numeric():
    assert utils.parse_picks("avc", 3) is None


def test_parse_picks_out_of_range():
    assert utils.parse_picks("9", 3) is None
    assert utils.parse_picks("0", 3) is None
    assert utils.parse_picks("-1", 3) is None


def test_handle_many_entries():
    first_entry = {
        "id": "aaaaaaaa123",
        "title": "Uploaded video title",
        "uploader": "Mr_uploader",
        "duration": 600,
    }
    second_entry = {
        "id": "bbbbbbbb432",
        "title": "Second uploaded video title",
        "uploader": "Second uploader",
        "duration": 400,
    }
    entries = [first_entry, second_entry]
    result = utils.handle_many_entries(entries)
    assert len(result) == 2
    assert result[0].id == "aaaaaaaa123"
    assert result[1].id == "bbbbbbbb432"


def test_handle_many_entries_empty_input():
    assert utils.handle_many_entries([]) == []


def test_video_from_info_tolerates_missing_uploader_and_duration():
    video = utils.video_from_info({"id": "x", "title": "Live stream"})
    assert video.uploader == "Unknown"
    assert video.duration is None


def test_video_from_info_tolerates_null_entries():
    # yt-dlp can put None entries in a playlist for deleted or private videos
    assert [v.id for v in utils.handle_many_entries([None, {"id": "dQw4w9WgXcQ", "title": "t"}])] == ["dQw4w9WgXcQ"]


def test_handle_many_entries_keeps_only_videos():
    video = {"ie_key": "Youtube", "_type": "url", "id": "dQw4w9WgXcQ", "title": "Song"}
    channel = {"ie_key": "YoutubeTab", "_type": "url", "id": "UCMK037TfgXabcdefghijklm", "title": "Band"}
    playlist = {"ie_key": "YoutubeTab", "_type": "url", "id": "PLabcdefghijklmnopqrstuvwxyz012345", "title": "Mix"}
    assert [v.id for v in utils.handle_many_entries([video, channel, playlist, None])] == ["dQw4w9WgXcQ"]


def test_handle_many_entries_of_no_videos_is_empty():
    channel = {"ie_key": "YoutubeTab", "id": "UCMK037TfgXabcdefghijklm", "title": "Band"}
    assert utils.handle_many_entries([channel, None]) == []


@pytest.mark.parametrize(
    "entry, video",
    [
        ({"id": "dQw4w9WgXcQ"}, True),  # no ie_key: an 11-character id is a video's
        ({"id": "a-b_c-d_e-f"}, True),
        ({"id": "UCMK037TfgXabcdefghijklm"}, False),  # a channel's
        ({"id": "dQw4w9WgXc"}, False),
        ({"id": "dQw4w9WgXc!"}, False),
        ({"title": "no id"}, False),
        ({"ie_key": "Youtube", "id": "abc"}, True),  # ie_key, when present, decides
        ({"ie_key": "YoutubeTab", "id": "dQw4w9WgXcQ"}, False),
        (None, False),
        ({}, False),
    ],
)
def test_is_video(entry, video):
    assert utils.is_video(entry) is video


def test_data_path_defaults_to_local_share_on_posix(monkeypatch, tmp_path):
    monkeypatch.setattr(utils, "WINDOWS", False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))  # data_path may migrate a data dir; never the real one
    assert utils.data_path("history.json") == Path.home() / ".local" / "share" / "ttyplayer" / "history.json"


def test_data_path_defaults_to_local_app_data_on_windows(monkeypatch, tmp_path):
    monkeypatch.setattr(utils, "WINDOWS", True)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert utils.data_path("history.json") == tmp_path / "ttyplayer" / "history.json"


def test_data_path_prefers_xdg_data_home_on_windows_too(monkeypatch, tmp_path):
    monkeypatch.setattr(utils, "WINDOWS", True)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setenv("LOCALAPPDATA", "elsewhere")
    assert utils.data_path("history.json") == tmp_path / "ttyplayer" / "history.json"


def test_data_path_moves_the_old_data_dir_over(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    (tmp_path / utils.OLD_NAME).mkdir()
    (tmp_path / utils.OLD_NAME / "history.jsonl").write_text("old history\n", encoding="utf-8")
    assert utils.data_path("history.jsonl").read_text(encoding="utf-8") == "old history\n"
    assert not (tmp_path / utils.OLD_NAME).exists()


def test_data_path_leaves_both_data_dirs_alone(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    for name in (utils.OLD_NAME, "ttyplayer"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "favorites.jsonl").write_text(f"{name}\n", encoding="utf-8")
    assert utils.data_path("favorites.jsonl").read_text(encoding="utf-8") == "ttyplayer\n"
    assert (tmp_path / utils.OLD_NAME / "favorites.jsonl").read_text(encoding="utf-8") == f"{utils.OLD_NAME}\n"


def test_data_path_without_an_old_data_dir_creates_nothing(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    assert utils.data_path("history.jsonl") == tmp_path / "ttyplayer" / "history.jsonl"
    assert list(tmp_path.iterdir()) == []


# Recorded entry shapes (trimmed): a SoundCloud flat search entry has an API url, its page in webpage_url.
SC_SEARCH_ENTRY = {
    "_type": "url",
    "ie_key": "Soundcloud",
    "id": "1234567",
    "url": "https://api.soundcloud.com/tracks/1234567",
    "title": "Roygbiv",
    "uploader": "warp-records",
    "duration": 151.0,
    "webpage_url": "https://soundcloud.com/warp-records/roygbiv",
}
# A SoundCloud link fetched in full: extractor_key, no ie_key.
SC_FULL_ENTRY = {
    "extractor_key": "Soundcloud",
    "id": "1234567",
    "title": "Roygbiv",
    "uploader": "warp-records",
    "duration": 151.0,
    "webpage_url": "https://soundcloud.com/warp-records/roygbiv",
}
YT_SEARCH_ENTRY = {
    "_type": "url",
    "ie_key": "Youtube",
    "id": "dQw4w9WgXcQ",
    "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    "title": "Song",
    "channel": "Band",
    "duration": 212,
}


@pytest.mark.parametrize("entry", [SC_SEARCH_ENTRY, SC_FULL_ENTRY])
def test_video_from_info_of_a_soundcloud_entry_keeps_its_page_link(entry):
    video = utils.video_from_info(entry)
    assert (video.source, video.id, video.uploader) == ("soundcloud", "1234567", "warp-records")
    assert video.link == video.url == "https://soundcloud.com/warp-records/roygbiv"


def test_video_from_info_of_a_youtube_entry_has_no_link():
    video = utils.video_from_info(YT_SEARCH_ENTRY)
    assert (video.source, video.link, video.uploader) == ("youtube", None, "Band")
    assert video.url == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


def test_video_from_info_of_a_soundcloud_entry_without_webpage_url_uses_its_url():
    entry = {key: value for key, value in SC_SEARCH_ENTRY.items() if key != "webpage_url"}
    assert utils.video_from_info(entry).link == "https://api.soundcloud.com/tracks/1234567"


@pytest.mark.parametrize(
    "key, video",
    [
        ("Youtube", True),
        ("Soundcloud", True),
        ("YoutubeTab", False),
        ("SoundcloudSet", False),
        ("SoundcloudUser", False),
        ("SoundcloudPlaylist", False),
    ],
)
@pytest.mark.parametrize("field", ["ie_key", "extractor_key"])
def test_is_video_keeps_single_tracks_of_both_sources(field, key, video):
    assert utils.is_video({field: key, "id": "1234567"}) is video


def test_handle_many_entries_of_a_soundcloud_search_drops_sets_and_users():
    user = {"_type": "url", "ie_key": "SoundcloudUser", "id": "warp-records", "title": "Warp"}
    sound_set = {"_type": "url", "ie_key": "SoundcloudSet", "id": "987", "title": "Album"}
    assert [v.id for v in utils.handle_many_entries([user, SC_SEARCH_ENTRY, sound_set])] == ["1234567"]


def test_track_extractors_are_the_yt_dlp_sources():
    from ttyplayer import youtube

    assert tuple(key.lower() for key in utils.TRACK_EXTRACTORS) == youtube.YTDLP_SOURCES


def test_a_stored_entry_round_trips_its_source_and_link():
    video = utils.video_from_info(SC_SEARCH_ENTRY)
    assert utils.video_from_info(utils.video_entry(video, "played_at")) == video


def test_a_stored_entry_without_source_loads_as_youtube():
    old = {"id": "dQw4w9WgXcQ", "title": "Song", "uploader": "Band", "duration": 212, "played_at": "2026-01-01T00:00:00+00:00"}
    video = utils.video_from_info(old)
    assert (video.source, video.link) == ("youtube", None)


def thumbs(*widths):
    return [{"url": f"https://i.example/{width}.jpg", "width": width} for width in widths]


@pytest.mark.parametrize(
    "fields, thumbnail",
    [
        ({"thumbnails": thumbs(168, 480, 336, 1280)}, "https://i.example/480.jpg"),
        ({"thumbnails": thumbs(720, 1280)}, "https://i.example/1280.jpg"),
        ({"thumbnails": [{"url": "https://i.example/a.jpg"}, {"url": "https://i.example/b.jpg"}]}, "https://i.example/b.jpg"),
        ({"thumbnails": [{"url": "https://i.example/a.jpg"}, *thumbs(120)]}, "https://i.example/120.jpg"),
        ({"thumbnails": [], "thumbnail": "https://i.example/one.jpg"}, "https://i.example/one.jpg"),
        ({}, "https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg"),
    ],
)
def test_video_from_info_picks_the_widest_thumbnail_up_to_480(fields, thumbnail):
    entry = {"id": "dQw4w9WgXcQ", "title": "Song", **fields}
    assert utils.video_from_info(entry).thumbnail == thumbnail


def test_a_soundcloud_entry_without_artwork_has_no_thumbnail():
    entry = {"id": "1234567", "ie_key": "Soundcloud", "title": "Song", "url": "https://soundcloud.com/a/b"}
    assert utils.video_from_info(entry).thumbnail is None
    artwork = {**entry, "thumbnail": "https://i1.sndcdn.com/artworks-x-t500x500.jpg"}
    assert utils.video_from_info(artwork).thumbnail == "https://i1.sndcdn.com/artworks-x-t500x500.jpg"


def test_a_stored_entry_round_trips_its_thumbnail():
    video = utils.video_from_info({"id": "dQw4w9WgXcQ", "thumbnails": thumbs(336)})
    entry = utils.video_entry(video, "played_at")
    assert entry["thumbnail"] == "https://i.example/336.jpg"
    assert utils.stored_video(entry) == video


def test_a_stored_entry_from_before_thumbnails_has_none():
    old = {"id": "dQw4w9WgXcQ", "title": "Song", "uploader": "Band", "duration": 212, "source": "youtube", "link": None}
    assert utils.stored_video(old).thumbnail is None
