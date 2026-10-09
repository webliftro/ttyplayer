import json

from ttyplayer import favorites
from ttyplayer.models import Video

A = Video(id="a", title="First", uploader="u", duration=10)
B = Video(id="b", title="Second", uploader="u", duration=None)
C = Video(id="c", title="Third", uploader="u", duration=3)


def test_add_appends_one_json_line_per_favorite(tmp_path):
    path = tmp_path / "favorites.jsonl"
    assert favorites.add(A, path) is True
    assert favorites.add(B, path) is True
    lines = path.read_text().splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["id"] == "a"
    assert first["title"] == "First"
    assert "favorited_at" in first


def test_add_does_not_repeat_a_favorite(tmp_path):
    path = tmp_path / "favorites.jsonl"
    favorites.add(A, path)
    favorites.add(B, path)
    assert favorites.add(A, path) is False
    assert len(path.read_text().splitlines()) == 2
    assert [v.id for v in favorites.load(path)] == ["b", "a"]


def test_add_creates_parent_directories(tmp_path):
    path = tmp_path / "deep" / "er" / "favorites.jsonl"
    favorites.add(A, path)
    assert path.exists()


def test_load_returns_most_recent_first(tmp_path):
    path = tmp_path / "favorites.jsonl"
    favorites.add(A, path)
    favorites.add(B, path)
    loaded = favorites.load(path)
    assert loaded == [B, A]


def test_load_keeps_one_entry_per_video(tmp_path):
    path = tmp_path / "favorites.jsonl"
    favorites.add(A, path)
    with path.open("a") as f:
        f.write(json.dumps({"id": "a", "title": "First"}) + "\n")
    assert [v.id for v in favorites.load(path)] == ["a"]


def test_load_respects_limit(tmp_path):
    path = tmp_path / "favorites.jsonl"
    for video in (A, B, C):
        favorites.add(video, path)
    assert [v.id for v in favorites.load(path, limit=2)] == ["c", "b"]


def test_load_missing_file_is_empty(tmp_path):
    assert favorites.load(tmp_path / "nope.jsonl") == []


def test_load_skips_corrupt_lines(tmp_path):
    path = tmp_path / "favorites.jsonl"
    favorites.add(A, path)
    with path.open("a") as f:
        f.write("not json\n")
    assert [v.id for v in favorites.load(path)] == ["a"]


def test_remove_drops_the_nth_as_listed(tmp_path):
    path = tmp_path / "favorites.jsonl"
    for video in (A, B, C):
        favorites.add(video, path)
    assert favorites.remove(2, path) == B
    assert [v.id for v in favorites.load(path)] == ["c", "a"]


def test_remove_out_of_range_changes_nothing(tmp_path):
    path = tmp_path / "favorites.jsonl"
    favorites.add(A, path)
    before = path.read_text()
    assert favorites.remove(0, path) is None
    assert favorites.remove(2, path) is None
    assert path.read_text() == before


def test_remove_from_an_empty_store_is_out_of_range(tmp_path):
    path = tmp_path / "favorites.jsonl"
    assert favorites.remove(1, path) is None
    assert not path.exists()


def test_remove_id_drops_that_video_and_keeps_the_order(tmp_path):
    path = tmp_path / "favorites.jsonl"
    for video in (A, B, C):
        favorites.add(video, path)
    assert favorites.remove_id("b", path) is True
    assert [v.id for v in favorites.load(path)] == ["c", "a"]
    assert favorites.add(B, path) is True


def test_remove_id_of_a_video_that_is_not_a_favorite(tmp_path):
    path = tmp_path / "favorites.jsonl"
    assert favorites.remove_id("a", path) is False
    assert not path.exists()
    favorites.add(A, path)
    before = path.read_text()
    assert favorites.remove_id("zz", path) is False
    assert path.read_text() == before


def test_ids_lists_every_favorite_once(tmp_path):
    path = tmp_path / "favorites.jsonl"
    assert favorites.ids(path) == set()
    for video in (A, B, A):
        favorites.add(video, path)
    with path.open("a") as f:
        f.write("not json\n")
    assert favorites.ids(path) == {"a", "b"}


def test_ids_and_remove_id_default_to_the_data_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    favorites.add(A)
    assert favorites.ids() == {"a"}
    assert favorites.remove_id("a") is True
    assert favorites.ids() == set()


def test_clear_removes_everything_and_reports_the_count(tmp_path):
    path = tmp_path / "favorites.jsonl"
    favorites.add(A, path)
    favorites.add(B, path)
    assert favorites.clear(path) == 2
    assert favorites.load(path) == []
    assert favorites.clear(path) == 0


def test_default_path_is_under_the_data_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    assert favorites.favorites_path() == tmp_path / "ttyplayer" / "favorites.jsonl"


SC = Video(id="123", title="Roygbiv", uploader="warp", duration=151, source="soundcloud", link="https://soundcloud.com/warp/roygbiv")


def test_a_soundcloud_favorite_keeps_its_source_and_link(tmp_path):
    path = tmp_path / "favorites.jsonl"
    favorites.add(SC, path)
    assert favorites.load(path) == [SC]


def test_an_old_favorite_without_source_loads_as_youtube(tmp_path):
    path = tmp_path / "favorites.jsonl"
    path.write_text('{"id": "a", "title": "First", "uploader": "u", "duration": 10, "favorited_at": "2026-01-01T00:00:00+00:00"}\n')
    assert favorites.load(path) == [A]
