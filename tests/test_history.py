import json

from ttyplayer import history
from ttyplayer.models import Video

A = Video(id="a", title="First", uploader="u", duration=10)
B = Video(id="b", title="Second", uploader="u", duration=None)


def test_record_appends_one_json_line_per_play(tmp_path):
    path = tmp_path / "history.jsonl"
    history.record(A, path)
    history.record(B, path)
    lines = path.read_text().splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["id"] == "a"
    assert first["title"] == "First"
    assert "played_at" in first


def test_load_returns_most_recent_first(tmp_path):
    path = tmp_path / "history.jsonl"
    history.record(A, path)
    history.record(B, path)
    assert [v.id for v in history.load(path)] == ["b", "a"]


def test_load_keeps_one_entry_per_video(tmp_path):
    path = tmp_path / "history.jsonl"
    history.record(A, path)
    history.record(B, path)
    history.record(A, path)
    assert [v.id for v in history.load(path)] == ["a", "b"]


def test_load_respects_limit(tmp_path):
    path = tmp_path / "history.jsonl"
    for number in range(5):
        history.record(Video(id=str(number), title="t", uploader="u", duration=1), path)
    assert len(history.load(path, limit=2)) == 2


def test_load_missing_file_is_empty(tmp_path):
    assert history.load(tmp_path / "nope.jsonl") == []


def test_load_skips_corrupt_lines(tmp_path):
    path = tmp_path / "history.jsonl"
    history.record(A, path)
    with path.open("a") as f:
        f.write("not json\n")
    assert [v.id for v in history.load(path)] == ["a"]


def test_record_creates_parent_directories(tmp_path):
    path = tmp_path / "deep" / "er" / "history.jsonl"
    history.record(A, path)
    assert path.exists()


def test_clear_removes_everything_and_reports_the_count(tmp_path):
    path = tmp_path / "history.jsonl"
    history.record(A, path)
    history.record(B, path)
    assert history.clear(path) == 2
    assert history.load(path) == []
    assert history.clear(path) == 0


def test_default_path_is_under_the_data_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    assert history.history_path() == tmp_path / "ttyplayer" / "history.jsonl"


SC = Video(id="123", title="Roygbiv", uploader="warp", duration=151, source="soundcloud", link="https://soundcloud.com/warp/roygbiv")


def test_a_soundcloud_play_keeps_its_source_and_link(tmp_path):
    path = tmp_path / "history.jsonl"
    history.record(SC, path)
    history.record(A, path)
    assert history.load(path) == [A, SC]


def test_an_old_line_without_source_loads_as_youtube(tmp_path):
    path = tmp_path / "history.jsonl"
    path.write_text('{"id": "a", "title": "First", "uploader": "u", "duration": 10, "played_at": "2026-01-01T00:00:00+00:00"}\n')
    assert history.load(path) == [A]
    assert history.load(path)[0].url == "https://www.youtube.com/watch?v=a"


T = Video(id="t", title="Cover", uploader="u", duration=1, thumbnail="https://i.ytimg.com/vi/t/hqdefault.jpg")


def test_a_thumbnail_round_trips_and_an_old_line_has_none(tmp_path):
    path = tmp_path / "history.jsonl"
    history.record(T, path)
    assert history.load(path) == [T]
    path.write_text('{"id": "a", "title": "First", "uploader": "u", "duration": 10, "source": "youtube"}\n')
    assert history.load(path)[0].thumbnail is None
