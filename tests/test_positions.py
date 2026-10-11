import json

import pytest

from ttyplayer import positions, utils


def test_set_then_get_round_trips(tmp_path):
    path = tmp_path / "positions.jsonl"
    positions.set("a", 754.25, path)
    positions.set("b", 60, path)
    assert positions.get("a", path) == 754.2
    assert positions.get("b", path) == 60


def test_get_without_a_saved_position_is_none(tmp_path):
    path = tmp_path / "positions.jsonl"
    assert positions.get("a", path) is None
    positions.set("b", 60, path)
    assert positions.get("a", path) is None


def test_set_keeps_one_entry_per_video_newest_at_the_bottom(tmp_path):
    path = tmp_path / "positions.jsonl"
    positions.set("a", 10, path)
    positions.set("b", 20, path)
    positions.set("a", 30, path)
    assert [json.loads(line) for line in path.read_text().splitlines()] == [
        {"id": "b", "position": 20},
        {"id": "a", "position": 30},
    ]


def test_clear_forgets_one_video(tmp_path):
    path = tmp_path / "positions.jsonl"
    positions.set("a", 10, path)
    positions.set("b", 20, path)
    assert positions.clear("a", path) is True
    assert positions.get("a", path) is None
    assert positions.get("b", path) == 20


def test_clear_of_an_unsaved_video_writes_nothing(tmp_path):
    path = tmp_path / "positions.jsonl"
    assert positions.clear("a", path) is False
    assert not path.exists()


def test_the_newest_500_are_kept():
    assert positions.KEPT == 500


def test_only_the_newest_entries_are_kept(tmp_path, monkeypatch):
    path = tmp_path / "positions.jsonl"
    monkeypatch.setattr(positions, "KEPT", 3)
    for number in range(5):
        positions.set(str(number), number, path)
    assert [json.loads(line)["id"] for line in path.read_text().splitlines()] == ["2", "3", "4"]
    assert positions.get("0", path) is None


def test_corrupt_lines_are_skipped_and_dropped_on_the_next_write(tmp_path):
    path = tmp_path / "positions.jsonl"
    path.write_text('{"id": "a", "position": 12.5}\nnot json\n["a list"]\n{"id": "b", "position": "soon"}\n{"id"\n')
    assert positions.get("a", path) == 12.5
    assert positions.get("b", path) is None  # a position that is not a number
    positions.set("c", 3, path)
    assert [json.loads(line)["id"] for line in path.read_text().splitlines()] == ["a", "b", "c"]


def test_a_write_replaces_the_file_through_a_temp_file(tmp_path, monkeypatch):
    path = tmp_path / "positions.jsonl"
    positions.set("a", 10, path)
    replaced = []
    real_replace = utils.os.replace
    monkeypatch.setattr(utils.os, "replace", lambda src, dst: replaced.append((src, dst)) or real_replace(src, dst))
    positions.set("b", 20, path)
    [(temp, target)] = replaced
    assert target == path and temp != str(path) and str(tmp_path) in temp
    assert sorted(p.name for p in tmp_path.iterdir()) == ["positions.jsonl"]  # no temp file left behind


def test_a_failed_write_leaves_the_file_as_it_was_and_no_temp_file(tmp_path, monkeypatch):
    path = tmp_path / "positions.jsonl"
    positions.set("a", 10, path)

    def fail(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(utils.os, "replace", fail)
    with pytest.raises(OSError):
        positions.set("b", 20, path)
    assert positions.get("a", path) == 10 and positions.get("b", path) is None
    assert sorted(p.name for p in tmp_path.iterdir()) == ["positions.jsonl"]


def test_the_file_lives_in_the_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    positions.set("a", 10)
    assert (tmp_path / "ttyplayer" / "positions.jsonl").exists()
    assert positions.get("a") == 10


@pytest.mark.parametrize(
    "duration, minimum, long",
    [
        (600, 10, True),
        (599, 10, False),
        (3600, 10, True),
        (None, 10, False),  # unknown duration: a live stream, or yt-dlp did not say
        (3600, 0, False),  # 0 turns resuming off
        (60, 1, True),
    ],
)
def test_is_long(duration, minimum, long):
    assert positions.is_long(duration, minimum) is long
