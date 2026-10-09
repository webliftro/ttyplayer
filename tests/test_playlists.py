import json

import pytest

from ttyplayer import playlists
from ttyplayer.models import Video
from ttyplayer.playlists import PlaylistError

A = Video(id="a", title="First", uploader="u", duration=10)
B = Video(id="b", title="Second", uploader="u", duration=None)
C = Video(id="c", title="Third", uploader="u", duration=3)


@pytest.fixture(autouse=True)
def data_home(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    return tmp_path


def ids(name):
    return [video.id for video in playlists.load(name)]


def test_a_playlist_is_one_jsonl_file_under_the_data_dir(data_home):
    playlists.create("chill")
    playlists.add("chill", [A])
    path = data_home / "ttyplayer" / "playlists" / "chill.jsonl"
    assert playlists.playlist_path("chill") == path
    entry = json.loads(path.read_text())
    assert {key: entry[key] for key in ("id", "title", "uploader", "duration")} == {
        "id": "a",
        "title": "First",
        "uploader": "u",
        "duration": 10,
    }
    assert "added_at" in entry


def test_names_are_sorted_and_empty_without_a_dir():
    assert playlists.names() == []
    for name in ("road trip", "Chill", "b-sides"):
        playlists.create(name)
    assert playlists.names() == sorted(["road trip", "Chill", "b-sides"])


def test_create_makes_an_empty_playlist():
    playlists.create("chill")
    assert playlists.load("chill") == []


def test_create_refuses_an_existing_name():
    playlists.create("chill")
    playlists.add("chill", [A])
    with pytest.raises(PlaylistError, match="A playlist named chill already exists"):
        playlists.create("chill")
    assert ids("chill") == ["a"]


@pytest.mark.parametrize("name", ["", "../escape", "a/b", "dot.name", "x" * 41, "tab\tname", "ü"])
def test_bad_names_are_refused(name):
    with pytest.raises(PlaylistError, match="Bad playlist name"):
        playlists.create(name)
    assert playlists.names() == []


def test_the_longest_and_widest_name_is_allowed():
    name = "Az09 _-" + "x" * 33
    playlists.create(name)
    assert playlists.names() == [name]


@pytest.mark.parametrize(
    "call",
    [
        lambda: playlists.load("nope"),
        lambda: playlists.delete("nope"),
        lambda: playlists.add("nope", [A]),
        lambda: playlists.remove("nope", 1),
        lambda: playlists.move("nope", 1, 1),
        lambda: playlists.require("nope"),
    ],
)
def test_a_missing_playlist_is_one_error(call):
    with pytest.raises(PlaylistError) as raised:
        call()
    assert str(raised.value) == "No playlist named nope"


def test_add_appends_in_order_keeps_repeats_and_counts():
    playlists.create("chill")
    assert playlists.add("chill", [A, B]) == 2
    assert playlists.add("chill", [A]) == 1
    assert ids("chill") == ["a", "b", "a"]


def test_load_skips_corrupt_lines(data_home):
    playlists.create("chill")
    playlists.add("chill", [A])
    path = playlists.playlist_path("chill")
    path.write_text(path.read_text() + "not json\n[1]\n")
    playlists.add("chill", [B])
    assert ids("chill") == ["a", "b"]


def test_delete_removes_the_file():
    playlists.create("chill")
    playlists.delete("chill")
    assert playlists.names() == []
    assert not playlists.playlist_path("chill").exists()


def test_remove_drops_the_nth_and_returns_it():
    playlists.create("chill")
    playlists.add("chill", [A, B, C])
    assert playlists.remove("chill", 2) == B
    assert ids("chill") == ["a", "c"]
    assert playlists.remove("chill", 2) == C
    assert ids("chill") == ["a"]


def test_remove_keeps_when_each_video_was_added():
    playlists.create("chill")
    playlists.add("chill", [A, B])
    path = playlists.playlist_path("chill")
    stamp = json.loads(path.read_text().splitlines()[1])["added_at"]
    playlists.remove("chill", 1)
    assert json.loads(path.read_text())["added_at"] == stamp


@pytest.mark.parametrize("number", [0, 4, -1])
def test_remove_out_of_range_changes_nothing(number):
    playlists.create("chill")
    playlists.add("chill", [A, B, C])
    with pytest.raises(PlaylistError, match=f"No video number {number} in chill"):
        playlists.remove("chill", number)
    assert ids("chill") == ["a", "b", "c"]


@pytest.mark.parametrize(
    "source, target, expected",
    [(1, 3, ["b", "c", "a"]), (3, 1, ["c", "a", "b"]), (2, 2, ["a", "b", "c"]), (2, 3, ["a", "c", "b"])],
)
def test_move_puts_the_video_at_the_target_position(source, target, expected):
    playlists.create("chill")
    playlists.add("chill", [A, B, C])
    assert playlists.move("chill", source, target).id == ["a", "b", "c"][source - 1]
    assert ids("chill") == expected


@pytest.mark.parametrize("source, target", [(0, 1), (1, 4), (4, 1), (1, 0)])
def test_move_out_of_range_changes_nothing(source, target):
    playlists.create("chill")
    playlists.add("chill", [A, B, C])
    with pytest.raises(PlaylistError, match="No video number"):
        playlists.move("chill", source, target)
    assert ids("chill") == ["a", "b", "c"]


def test_replace_overwrites_an_existing_playlist():
    playlists.create("chill")
    playlists.add("chill", [A, B])
    playlists.replace("chill", [C, A])
    assert ids("chill") == ["c", "a"]


def test_replace_creates_a_missing_playlist():
    playlists.replace("new one", [B])
    assert playlists.names() == ["new one"]
    assert ids("new one") == ["b"]


def test_replace_refuses_a_bad_name():
    with pytest.raises(PlaylistError, match="Bad playlist name"):
        playlists.replace("../x", [A])


@pytest.mark.parametrize(
    "title, name",
    [
        ("Lo-fi: beats / study", "Lo-fi beats study"),
        ("  Café  del  Mar  ", "Caf del Mar"),
        ("x" * 39 + " y", "x" * 39),
        ("日本の歌", ""),
        ("good_name-1", "good_name-1"),
    ],
)
def test_sanitize_turns_a_title_into_a_valid_name(title, name):
    assert playlists.sanitize(title) == name
    if name:
        assert playlists.NAME.fullmatch(name)
