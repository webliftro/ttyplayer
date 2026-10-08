from pathlib import Path

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
        "id": "1234",
        "title": "Uploaded video title",
        "uploader": "Mr_uploader",
        "duration": 600,
    }
    second_entry = {
        "id": "4321",
        "title": "Second uploaded video title",
        "uploader": "Second uploader",
        "duration": 400,
    }
    entries = [first_entry, second_entry]
    result = utils.handle_many_entries(entries)
    assert len(result) == 2
    assert result[0].id == "1234"
    assert result[1].id == "4321"


def test_handle_many_entries_empty_input():
    assert utils.handle_many_entries([]) == []


def test_video_from_info_tolerates_missing_uploader_and_duration():
    video = utils.video_from_info({"id": "x", "title": "Live stream"})
    assert video.uploader == "Unknown"
    assert video.duration is None


def test_video_from_info_tolerates_null_entries():
    # yt-dlp can put None entries in a playlist for deleted or private videos
    assert utils.handle_many_entries([None, {"id": "x", "title": "t"}]) != []
    assert [v.id for v in utils.handle_many_entries([None, {"id": "x", "title": "t"}])] == ["x"]


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
