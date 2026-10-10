import dataclasses
import os
import random
import shutil
import subprocess
import sys
import tomllib
import zipfile
from importlib import metadata
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ttyplayer import art, cli, control, favorites, history, lyrics, player, playlists, settings, spotify, utils, youtube
from ttyplayer.cli import app
from ttyplayer.models import Video

runner = CliRunner()

ONE_VIDEO = [Video(id="abc", title="t", uploader="u", duration=1)]


def test_version_prints_the_installed_version():
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert result.output == f"ttyplayer {metadata.version('ttyplayer')}\n"
    assert result.output != "ttyplayer \n"


def test_history_clear_forgets_everything(monkeypatch, tmp_path):
    path = tmp_path / "h.jsonl"
    monkeypatch.setattr(history, "history_path", lambda: path)
    history.record(Video(id="a", title="Alpha", uploader="u", duration=1), path)
    result = runner.invoke(app, ["history", "--clear"])
    assert result.exit_code == 0
    assert result.output == "Forgot 1 plays\n"
    assert not path.exists()


def test_play_exits_when_nothing_found(monkeypatch):
    monkeypatch.setattr(youtube, "search", lambda query, limit=5, source="youtube": [])
    result = runner.invoke(app, ["play", "xyzzyqwerty"])
    assert result.exit_code == 1
    assert result.stderr == "No videos found\n"
    assert "Pick" not in result.output


def raise_youtube_error(*args, **kwargs):
    raise youtube.YouTubeError("no internet")


def test_play_reports_youtube_errors_plainly(monkeypatch):
    monkeypatch.setattr(youtube, "search", raise_youtube_error)
    result = runner.invoke(app, ["play", "some song"])
    assert result.exit_code == 1
    assert "no internet" in result.stderr
    assert "Traceback" not in result.output


def test_search_reports_youtube_errors_plainly(monkeypatch):
    monkeypatch.setattr(youtube, "search", raise_youtube_error)
    result = runner.invoke(app, ["search", "some song"])
    assert result.exit_code == 1
    assert "no internet" in result.stderr


def test_play_with_a_link_reports_youtube_errors_plainly(monkeypatch):
    monkeypatch.setattr(youtube, "fetch", raise_youtube_error)
    result = runner.invoke(app, ["play", "https://www.youtube.com/watch?v=abc"])
    assert result.exit_code == 1
    assert "no internet" in result.stderr


def test_play_reports_missing_mpv(monkeypatch):
    monkeypatch.setattr(youtube, "fetch", lambda url: ONE_VIDEO)

    def no_mpv(video=False, **kwargs):
        raise FileNotFoundError("mpv")

    monkeypatch.setattr(player, "MpvClient", no_mpv)
    result = runner.invoke(app, ["play", "https://www.youtube.com/watch?v=abc"])
    assert result.exit_code == 1
    assert "mpv is not installed" in result.stderr


def test_play_reports_mpv_that_never_answered(monkeypatch):
    monkeypatch.setattr(youtube, "fetch", lambda url: ONE_VIDEO)

    def stuck_mpv(video=False, **kwargs):
        raise RuntimeError("mpv did not start")

    monkeypatch.setattr(player, "MpvClient", stuck_mpv)
    result = runner.invoke(app, ["play", "https://www.youtube.com/watch?v=abc"])
    assert result.exit_code == 1
    assert "mpv did not start" in result.stderr


class FakeClient:
    """Stands in for MpvClient: records what was queued and played, never runs mpv."""

    instances = []

    def __init__(
        self, video=False, on_play=None, on_state=None, pcm=None, levels=True, radio=False, normalize=False,
        seek_seconds=player.SEEK_SECONDS, volume_step=player.VOLUME_STEP,
    ):
        self.video = video
        self.radio = radio
        self.normalize = normalize
        self.steps = (seek_seconds, volume_step)
        self.pcm = pcm
        self.headless_pcm = pcm is not None
        self.levels = levels
        self.on_play = on_play
        self.on_state = on_state
        self.queue = []
        self.ran = False
        FakeClient.instances.append(self)

    def add(self, video):
        self.queue.append(video)

    def play_current(self):
        if self.on_play:
            self.on_play(self.queue[0])

    def run(self):
        self.ran = True


@pytest.mark.parametrize("saved, levels", [(None, True), ("false", False)])
def test_start_mpv_follows_the_show_levels_setting(saved, levels, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    if saved:
        settings.update("show_levels", saved)
    monkeypatch.setattr(player, "MpvClient", FakeClient)
    assert cli.start_mpv(False).levels is levels


def test_start_mpv_follows_the_playback_tuning_settings(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr(player, "MpvClient", FakeClient)
    default = cli.start_mpv(False)
    assert (default.normalize, default.steps) == (False, (5, 5))
    for key, value in [("normalize_loudness", "true"), ("seek_seconds", "10"), ("volume_step", "2")]:
        settings.update(key, value)
    tuned = cli.start_mpv(False)
    assert (tuned.normalize, tuned.steps) == (True, (10, 2))


@pytest.mark.parametrize("saved, flag, radio", [(None, False, False), (None, True, True), ("true", False, True)])
def test_start_mpv_turns_the_radio_on_with_the_flag_or_the_radio_setting(saved, flag, radio, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    if saved:
        settings.update("radio", saved)
    monkeypatch.setattr(player, "MpvClient", FakeClient)
    assert cli.start_mpv(False, radio=flag).radio is radio


def test_play_radio_starts_the_player_with_the_radio_on(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr(youtube, "fetch", lambda url: [Video(id="a" * 11, title="Song", uploader="u", duration=1)])
    monkeypatch.setattr(player, "MpvClient", FakeClient)
    monkeypatch.setattr(history, "history_path", lambda: tmp_path / "h.jsonl")
    FakeClient.instances = []
    assert runner.invoke(app, ["play", "--radio", "https://youtu.be/x"]).exit_code == 0
    assert runner.invoke(app, ["play", "https://youtu.be/x"]).exit_code == 0
    assert [client.radio for client in FakeClient.instances] == [True, False]


def test_play_queues_the_picks_in_order_and_records_history(monkeypatch, tmp_path):
    videos = [Video(id=str(n), title=f"v{n}", uploader="u", duration=1) for n in range(3)]
    monkeypatch.setattr(youtube, "search", lambda query, limit=5, source="youtube": videos)
    monkeypatch.setattr(player, "MpvClient", FakeClient)
    monkeypatch.setattr(history, "history_path", lambda: tmp_path / "h.jsonl")
    FakeClient.instances = []

    result = runner.invoke(app, ["play", "some", "song", "--video"], input="3 1\n")

    assert result.exit_code == 0, result.output
    client = FakeClient.instances[0]
    assert [v.id for v in client.queue] == ["2", "0"]
    assert client.video is True
    assert client.ran
    assert [v.id for v in history.load(tmp_path / "h.jsonl")] == ["2"]


def test_play_asks_again_on_bad_picks(monkeypatch):
    monkeypatch.setattr(youtube, "search", lambda query, limit=5, source="youtube": ONE_VIDEO)
    monkeypatch.setattr(player, "MpvClient", FakeClient)
    result = runner.invoke(app, ["play", "song"], input="9\nx\n1\n")
    assert result.exit_code == 0, result.output
    assert result.stderr.count("Enter numbers between 1 and 1") == 2


def test_history_lists_recent_videos(monkeypatch, tmp_path):
    path = tmp_path / "h.jsonl"
    monkeypatch.setattr(history, "history_path", lambda: path)
    history.record(Video(id="a", title="Alpha", uploader="u", duration=61), path)
    history.record(Video(id="b", title="Beta", uploader="u", duration=None), path)
    result = runner.invoke(app, ["history"])
    assert result.exit_code == 0
    assert result.output.splitlines() == [" 1. Beta  (--:--)  u", " 2. Alpha  (1:01)  u"]


def test_history_when_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(history, "history_path", lambda: tmp_path / "h.jsonl")
    result = runner.invoke(app, ["history"])
    assert result.exit_code == 1
    assert "Nothing played yet" in result.stderr


def test_history_play_replays_a_pick(monkeypatch, tmp_path):
    path = tmp_path / "h.jsonl"
    monkeypatch.setattr(history, "history_path", lambda: path)
    monkeypatch.setattr(player, "MpvClient", FakeClient)
    history.record(Video(id="a", title="Alpha", uploader="u", duration=1), path)
    FakeClient.instances = []
    result = runner.invoke(app, ["history", "--play"], input="1\n")
    assert result.exit_code == 0, result.output
    assert [v.id for v in FakeClient.instances[0].queue] == ["a"]


ALPHA = Video(id="a", title="Alpha", uploader="u", duration=61)
BETA = Video(id="b", title="Beta", uploader="u", duration=None)


def use_tmp_favorites(monkeypatch, tmp_path):
    path = tmp_path / "f.jsonl"
    monkeypatch.setattr(favorites, "favorites_path", lambda: path)
    return path


def test_favorite_a_link_adds_every_video(monkeypatch, tmp_path):
    path = use_tmp_favorites(monkeypatch, tmp_path)
    monkeypatch.setattr(youtube, "fetch", lambda url: [ALPHA, BETA])
    result = runner.invoke(app, ["favorite", "https://www.youtube.com/playlist?list=x"])
    assert result.exit_code == 0, result.output
    assert result.output == "Favorited: Alpha\nFavorited: Beta\n"
    assert [v.id for v in favorites.load(path)] == ["b", "a"]


def test_favorite_search_words_adds_the_picks(monkeypatch, tmp_path):
    path = use_tmp_favorites(monkeypatch, tmp_path)
    monkeypatch.setattr(youtube, "search", lambda query, limit=5, source="youtube": [ALPHA, BETA])
    result = runner.invoke(app, ["favorite", "some", "song"], input="2\n")
    assert result.exit_code == 0, result.output
    assert result.output.splitlines()[-1] == "Favorited: Beta"
    assert [v.id for v in favorites.load(path)] == ["b"]


def test_favorite_twice_says_already_a_favorite(monkeypatch, tmp_path):
    path = use_tmp_favorites(monkeypatch, tmp_path)
    monkeypatch.setattr(youtube, "fetch", lambda url: [ALPHA])
    runner.invoke(app, ["favorite", "https://www.youtube.com/watch?v=a"])
    result = runner.invoke(app, ["favorite", "https://www.youtube.com/watch?v=a"])
    assert result.exit_code == 0, result.output
    assert result.output == "Already a favorite: Alpha\n"
    assert len(favorites.load(path)) == 1


def test_favorite_reports_youtube_errors_plainly(monkeypatch, tmp_path):
    use_tmp_favorites(monkeypatch, tmp_path)
    monkeypatch.setattr(youtube, "search", raise_youtube_error)
    result = runner.invoke(app, ["favorite", "some song"])
    assert result.exit_code == 1
    assert "no internet" in result.stderr


def test_favorites_lists_newest_first(monkeypatch, tmp_path):
    path = use_tmp_favorites(monkeypatch, tmp_path)
    favorites.add(ALPHA, path)
    favorites.add(BETA, path)
    result = runner.invoke(app, ["favorites"])
    assert result.exit_code == 0
    assert result.output.splitlines() == [" 1. Beta  (--:--)  u", " 2. Alpha  (1:01)  u"]


def test_favorites_play_plays_the_picks(monkeypatch, tmp_path):
    path = use_tmp_favorites(monkeypatch, tmp_path)
    monkeypatch.setattr(history, "history_path", lambda: tmp_path / "h.jsonl")
    monkeypatch.setattr(player, "MpvClient", FakeClient)
    favorites.add(ALPHA, path)
    favorites.add(BETA, path)
    FakeClient.instances = []
    result = runner.invoke(app, ["favorites", "--play", "--video"], input="2 1\n")
    assert result.exit_code == 0, result.output
    assert " 1. Beta  (--:--)  u" in result.output
    client = FakeClient.instances[0]
    assert [v.id for v in client.queue] == ["a", "b"]
    assert client.video is True
    assert client.ran


def test_favorites_remove_drops_the_nth(monkeypatch, tmp_path):
    path = use_tmp_favorites(monkeypatch, tmp_path)
    favorites.add(ALPHA, path)
    favorites.add(BETA, path)
    result = runner.invoke(app, ["favorites", "--remove", "2"])
    assert result.exit_code == 0, result.output
    assert result.output == "Removed: Alpha\n"
    assert [v.id for v in favorites.load(path)] == ["b"]


def test_favorites_remove_out_of_range_fails_without_changes(monkeypatch, tmp_path):
    path = use_tmp_favorites(monkeypatch, tmp_path)
    favorites.add(ALPHA, path)
    before = path.read_text()
    result = runner.invoke(app, ["favorites", "--remove", "3"])
    assert result.exit_code == 1
    assert result.stderr == "No favorite number 3\n"
    assert result.stdout == ""
    assert path.read_text() == before


def test_favorites_clear_forgets_everything(monkeypatch, tmp_path):
    path = use_tmp_favorites(monkeypatch, tmp_path)
    favorites.add(ALPHA, path)
    favorites.add(BETA, path)
    result = runner.invoke(app, ["favorites", "--clear"])
    assert result.exit_code == 0
    assert result.output == "Forgot 2 favorites\n"
    assert favorites.load(path) == []


def test_favorites_when_empty(monkeypatch, tmp_path):
    use_tmp_favorites(monkeypatch, tmp_path)
    for args in (["favorites"], ["favorites", "--play"]):
        result = runner.invoke(app, args, input="1\n")
        assert result.exit_code == 1
        assert result.stderr == "No favorites yet\n"
        assert result.stdout == ""


def test_favorites_and_history_are_separate(monkeypatch, tmp_path):
    favorites_file = use_tmp_favorites(monkeypatch, tmp_path)
    history_file = tmp_path / "h.jsonl"
    monkeypatch.setattr(history, "history_path", lambda: history_file)
    history.record(ALPHA, history_file)
    result = runner.invoke(app, ["favorites"])
    assert result.exit_code == 1
    assert "No favorites yet" in result.stderr
    favorites.add(BETA, favorites_file)
    assert [v.id for v in history.load()] == ["a"]
    assert [v.id for v in favorites.load()] == ["b"]


def numbered(start, stop):
    return [Video(id=str(n), title=f"v{n}", uploader="u", duration=1) for n in range(start, stop + 1)]


class FakeSearch:
    """Stands in for youtube.search: records (query, limit) and returns what results() gives."""

    def __init__(self, results=lambda limit: numbered(1, limit)):
        self.results = results
        self.calls = []

    def __call__(self, query, limit=5, source="youtube"):
        self.calls.append((query, limit))
        return self.results(limit)


SEARCH_PROMPT = "Pick one or more numbers (1 or 1 3 5), m for more"


def test_search_prompt_offers_more(monkeypatch, tmp_path):
    use_tmp_favorites(monkeypatch, tmp_path)
    monkeypatch.setattr(history, "history_path", lambda: tmp_path / "h.jsonl")
    monkeypatch.setattr(youtube, "search", FakeSearch())
    monkeypatch.setattr(player, "MpvClient", FakeClient)
    for command in ("play", "favorite"):
        result = runner.invoke(app, [command, "song"], input="1\n")
        assert f"{SEARCH_PROMPT}: " in result.output


def test_history_and_favorites_prompts_do_not_offer_more(monkeypatch, tmp_path):
    use_tmp_favorites(monkeypatch, tmp_path)
    monkeypatch.setattr(history, "history_path", lambda: tmp_path / "h.jsonl")
    monkeypatch.setattr(player, "MpvClient", FakeClient)
    history.record(ALPHA)
    favorites.add(ALPHA)
    for command in ("history", "favorites"):
        result = runner.invoke(app, [command, "--play"], input="1\n")
        assert "Pick one or more numbers (1 or 1 3 5): " in result.output
        assert "m for more" not in result.output


def test_more_lists_only_the_next_batch_numbered_on(monkeypatch, tmp_path):
    monkeypatch.setattr(history, "history_path", lambda: tmp_path / "h.jsonl")
    search = FakeSearch()
    monkeypatch.setattr(youtube, "search", search)
    monkeypatch.setattr(player, "MpvClient", FakeClient)

    result = runner.invoke(app, ["play", "some", "song"], input=" M \n1\n")

    assert result.exit_code == 0, result.output
    assert search.calls == [("some song", 5), ("some song", 10)]
    rows = [line for line in result.output.splitlines() if ". v" in line]
    assert rows == [f"{n:>2}. v{n}  (0:01)  u" for n in range(1, 11)]
    assert result.output.count(SEARCH_PROMPT) == 2


def test_more_skips_videos_already_shown_and_keeps_numbering(monkeypatch, tmp_path):
    monkeypatch.setattr(history, "history_path", lambda: tmp_path / "h.jsonl")
    first = numbered(1, 5)
    second = [first[2], *numbered(6, 6), first[0], *numbered(7, 7), *numbered(6, 6), *first]
    search = FakeSearch(lambda limit: first if limit == 5 else second)
    monkeypatch.setattr(youtube, "search", search)
    monkeypatch.setattr(player, "MpvClient", FakeClient)

    result = runner.invoke(app, ["play", "song"], input="m\n1\n")

    assert result.exit_code == 0, result.output
    rows = [line.split("  ")[0] for line in result.output.splitlines() if ". v" in line]
    assert rows == [" 1. v1", " 2. v2", " 3. v3", " 4. v4", " 5. v5", " 6. v6", " 7. v7"]


def test_picks_after_more_index_the_whole_list(monkeypatch, tmp_path):
    monkeypatch.setattr(youtube, "search", FakeSearch())
    monkeypatch.setattr(player, "MpvClient", FakeClient)
    monkeypatch.setattr(history, "history_path", lambda: tmp_path / "h.jsonl")
    FakeClient.instances = []

    result = runner.invoke(app, ["play", "song"], input="m\n11\n7 2\n")

    assert result.exit_code == 0, result.output
    assert "Enter numbers between 1 and 10, separated by spaces." in result.stderr
    assert [v.id for v in FakeClient.instances[0].queue] == ["7", "2"]


def test_favorite_picks_after_more(monkeypatch, tmp_path):
    use_tmp_favorites(monkeypatch, tmp_path)
    monkeypatch.setattr(youtube, "search", FakeSearch())

    result = runner.invoke(app, ["favorite", "song"], input="m\n7 2\n")

    assert result.exit_code == 0, result.output
    assert [v.id for v in favorites.load()] == ["2", "7"]


def test_more_with_nothing_new_says_so_and_asks_again(monkeypatch, tmp_path):
    monkeypatch.setattr(history, "history_path", lambda: tmp_path / "h.jsonl")
    search = FakeSearch(lambda limit: numbered(1, 5))
    monkeypatch.setattr(youtube, "search", search)
    monkeypatch.setattr(player, "MpvClient", FakeClient)
    FakeClient.instances = []

    result = runner.invoke(app, ["play", "song"], input="m\n5\n")

    assert result.exit_code == 0, result.output
    assert result.stderr == "No more results\n"
    assert result.output.count(SEARCH_PROMPT) == 2
    assert result.output.count(". v") == 5
    assert [v.id for v in FakeClient.instances[0].queue] == ["5"]


def test_more_is_not_a_command_at_history_play(monkeypatch, tmp_path):
    search = FakeSearch()
    monkeypatch.setattr(youtube, "search", search)
    monkeypatch.setattr(history, "history_path", lambda: tmp_path / "h.jsonl")
    monkeypatch.setattr(player, "MpvClient", FakeClient)
    history.record(ALPHA)

    result = runner.invoke(app, ["history", "--play"], input="m\n1\n")

    assert result.exit_code == 0, result.output
    assert result.stderr == "Enter numbers between 1 and 1, separated by spaces.\n"
    assert search.calls == []


def test_more_reports_youtube_errors_plainly(monkeypatch):
    def fail_on_more(limit):
        if limit > 5:
            raise youtube.YouTubeError("no internet")
        return numbered(1, 5)

    monkeypatch.setattr(youtube, "search", FakeSearch(fail_on_more))

    result = runner.invoke(app, ["play", "song"], input="m\n")

    assert result.exit_code == 1
    assert result.stderr == "YouTube lookup failed: no internet\n"
    assert "Traceback" not in result.output


def fake_send(reply):
    """A control.send stand-in that records the command names and answers reply."""

    def send(name, path=None):
        send.names.append(name)
        return reply

    send.names = []
    return send


def no_player(name, path=None):
    raise control.ControlError("no socket")


@pytest.mark.parametrize("command", ["pause", "next", "prev", "stop"])
def test_remote_commands_send_their_name_and_print_nothing(command, monkeypatch):
    send = fake_send({"ok": True})
    monkeypatch.setattr(control, "send", send)
    result = runner.invoke(app, [command])
    assert result.exit_code == 0
    assert result.output == ""
    assert send.names == [command]


STATUS = {
    "ok": True,
    "title": "Song",
    "position": 83.4,
    "duration": 296.0,
    "paused": True,
    "volume": 70.0,
    "muted": False,
}


def test_status_prints_the_player_status_line(monkeypatch):
    monkeypatch.setattr(control, "send", fake_send({**STATUS, "index": 1, "total": 1}))
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert result.output == "1:23 / 4:56  Paused  🔊 ▮▮▮▮▮▮▮▯▯▯ 70%  Song\n"


def test_status_shows_the_queue_position_when_there_is_a_queue(monkeypatch):
    reply = {**STATUS, "paused": False, "position": None, "index": 2, "total": 3}
    monkeypatch.setattr(control, "send", fake_send(reply))
    result = runner.invoke(app, ["status"])
    assert result.output == "--:-- / 4:56  Playing  🔊 ▮▮▮▮▮▮▮▯▯▯ 70%  Song  [2/3]\n"


LYRICS_STATUS = {**STATUS, "title": "Queen - Bohemian Rhapsody (Official Video)", "uploader": "Queen Official", "idle": False}


def fake_lookup(found):
    """A lyrics.lookup stand-in that records its arguments and answers found."""

    def lookup(artist, track, duration=None):
        lookup.calls.append((artist, track, duration))
        return found

    lookup.calls = []
    return lookup


@pytest.mark.parametrize(
    ("found", "printed"),
    [
        (lyrics.Lyrics([(1.0, "Is this the real life?"), (4.5, "Is this just fantasy?")], None, "url"), "Is this the real life?\nIs this just fantasy?\n"),
        (lyrics.Lyrics(None, "Is this the real life?\nIs this just fantasy?", "url"), "Is this the real life?\nIs this just fantasy?\n"),
    ],
)
def test_lyrics_prints_the_playing_tracks_words_without_stamps(found, printed, monkeypatch):
    send, lookup = fake_send(LYRICS_STATUS), fake_lookup(found)
    monkeypatch.setattr(control, "send", send)
    monkeypatch.setattr(lyrics, "lookup", lookup)
    result = runner.invoke(app, ["lyrics"])
    assert result.exit_code == 0
    assert result.output == printed
    assert send.names == ["status"]
    assert lookup.calls == [("Queen", "Bohemian Rhapsody", 296.0)]


def test_lyrics_not_found_is_one_line_and_exit_1(monkeypatch):
    monkeypatch.setattr(control, "send", fake_send(LYRICS_STATUS))
    monkeypatch.setattr(lyrics, "lookup", fake_lookup(None))
    result = runner.invoke(app, ["lyrics"])
    assert result.exit_code == 1
    assert result.stderr == 'No lyrics found for "Queen – Bohemian Rhapsody"\n'
    assert result.stdout == ""


@pytest.mark.parametrize("reply", [{**LYRICS_STATUS, "idle": True}, {**LYRICS_STATUS, "title": ""}])
def test_lyrics_with_the_player_idle_is_one_line_and_exit_1(reply, monkeypatch):
    lookup = fake_lookup(None)
    monkeypatch.setattr(control, "send", fake_send(reply))
    monkeypatch.setattr(lyrics, "lookup", lookup)
    result = runner.invoke(app, ["lyrics"])
    assert result.exit_code == 1
    assert result.stderr == "Nothing is playing\n"
    assert lookup.calls == []


@pytest.mark.parametrize("command", ["pause", "next", "prev", "stop", "status", "lyrics"])
def test_remote_commands_with_nothing_playing(command, monkeypatch):
    monkeypatch.setattr(control, "send", no_player)
    result = runner.invoke(app, [command])
    assert result.exit_code == 1
    assert result.stderr == "No ttyplayer is playing\n"
    assert result.stdout == ""


@pytest.mark.parametrize("args, sent", [(["sleep", "20m"], "sleep 20m"), (["sleep"], "sleep")])
def test_sleep_sends_its_text_and_prints_the_players_line(args, sent, monkeypatch):
    send = fake_send({"ok": True, "message": "Sleeping in 20:00"})
    monkeypatch.setattr(control, "send", send)
    result = runner.invoke(app, args)
    assert result.exit_code == 0
    assert result.output == "Sleeping in 20:00\n"
    assert send.names == [sent]


def test_sleep_with_bad_text_prints_the_players_error(monkeypatch):
    monkeypatch.setattr(control, "send", fake_send({"ok": False, "error": "Sleep takes 30m, …, not 'soon'"}))
    result = runner.invoke(app, ["sleep", "soon"])
    assert result.exit_code == 1
    assert result.stderr == "Sleep takes 30m, …, not 'soon'\n"


def test_sleep_reaches_the_players_sleep(monkeypatch):
    """ttyplayer sleep against MpvClient.handle_control: the CLI's text becomes the player's sleep()."""
    calls = []
    fake_player = player.MpvClient.__new__(player.MpvClient)  # no mpv; sleep() only records
    fake_player.sleep = calls.append
    monkeypatch.setattr(control, "send", lambda name, path=None: fake_player.handle_control(name))
    result = runner.invoke(app, ["sleep", "end"])
    assert result.exit_code == 0
    assert calls == ["end"]


@pytest.mark.parametrize("args, sent", [(["radio"], "radio"), (["radio", "on"], "radio on"), (["radio", "off"], "radio off")])
def test_radio_sends_its_state_and_prints_the_players_line(args, sent, monkeypatch):
    send = fake_send({"ok": True, "message": "Radio on"})
    monkeypatch.setattr(control, "send", send)
    result = runner.invoke(app, args)
    assert (result.exit_code, result.output) == (0, "Radio on\n")
    assert send.names == [sent]


def test_radio_reaches_the_players_radio(monkeypatch):
    fake_player = player.MpvClient.__new__(player.MpvClient)  # no mpv; set_radio() only records
    calls = []
    fake_player.set_radio = calls.append
    monkeypatch.setattr(control, "send", lambda name, path=None: fake_player.handle_control(name))
    assert runner.invoke(app, ["radio", "on"]).exit_code == 0
    assert calls == [True]


def test_status_shows_the_sleep_timer(monkeypatch):
    monkeypatch.setattr(player.time, "time", lambda: 1000.0)
    reply = {**STATUS, "index": 1, "total": 1, "sleep": {"ends_at": 1600.0}}
    monkeypatch.setattr(control, "send", fake_send(reply))
    assert runner.invoke(app, ["status"]).output == "1:23 / 4:56  Paused  zz 10:00  🔊 ▮▮▮▮▮▮▮▯▯▯ 70%  Song\n"


def test_remote_command_refused_prints_the_players_error(monkeypatch):
    monkeypatch.setattr(control, "send", fake_send({"ok": False, "error": "unknown command x"}))
    result = runner.invoke(app, ["pause"])
    assert result.exit_code == 1
    assert result.stderr == "unknown command x\n"


@pytest.fixture
def settings_file(monkeypatch, tmp_path):
    path = tmp_path / "settings.toml"
    monkeypatch.setattr(settings, "settings_path", lambda: path)
    return path


def test_tui_runs_the_app_with_the_real_player(monkeypatch, settings_file):
    from ttyplayer import tui

    settings.save(settings.Settings(search_limit=20))
    built = []
    monkeypatch.setattr(tui.TtyplayerApp, "run", lambda self: built.append(self))
    result = runner.invoke(app, ["tui", "--video"])
    assert result.exit_code == 0, result.output
    [screen] = built
    assert screen.client_factory is player.MpvClient
    assert screen.resolve is tui.resolve
    assert screen.video is True
    assert screen.settings == settings.Settings(search_limit=20)


@pytest.mark.parametrize(
    "args, saved, url, token",
    [
        (["--remote", "http://box:7700", "--token", "given"], {}, "http://box:7700", "given"),
        (["--remote", "http://box:7700"], {"server_token": "saved"}, "http://box:7700", "saved"),
        ([], {"remote_url": "http://pi:7700", "server_token": "saved"}, "http://pi:7700", "saved"),
        (["--remote", "http://box:7700"], {"remote_url": "http://pi:7700"}, "http://box:7700", ""),
    ],
)
def test_tui_remote_drives_a_server_instead_of_mpv(monkeypatch, settings_file, args, saved, url, token):
    from ttyplayer import remote, tui

    settings.save(settings.Settings(**saved))
    built = []
    monkeypatch.setattr(tui.TtyplayerApp, "run", lambda self: built.append(self))
    made = []
    monkeypatch.setattr(remote, "RemoteClient", lambda *args, **kwargs: made.append((args, kwargs)) or "client")
    result = runner.invoke(app, ["tui", *args])
    assert result.exit_code == 0, result.output
    [screen] = built
    assert isinstance(screen, remote.RemoteApp)
    assert screen.resolve is tui.resolve
    assert screen.settings == settings.Settings(**saved)
    assert screen.sub_title == f"remote: {url.removeprefix('http://')}"
    assert screen.client_factory(False, on_play=screen.on_play, on_state=screen.on_player_state) == "client"
    assert made == [
        ((url, token), {"on_play": screen.on_play, "on_state": screen.on_player_state, "on_error": screen.server_error})
    ]


def test_tui_with_a_broken_settings_file_fails_in_one_line(monkeypatch, settings_file):
    from ttyplayer import tui

    monkeypatch.setattr(tui.TtyplayerApp, "run", lambda self: pytest.fail("the app ran"))
    settings_file.write_text("show_clock = maybe", encoding="utf-8")
    result = runner.invoke(app, ["tui"])
    assert result.exit_code == 1
    assert result.stderr.startswith(f"{settings_file} is not valid TOML")
    assert result.stderr.count("\n") == 1


def no_network(monkeypatch):
    monkeypatch.setattr(youtube, "search", lambda *args: pytest.fail("config used the network"))
    monkeypatch.setattr(youtube, "fetch", lambda *args: pytest.fail("config used the network"))


def test_config_lists_every_setting_marking_the_defaults(monkeypatch, settings_file):
    no_network(monkeypatch)
    settings.save(settings.Settings(show_clock=False))
    result = runner.invoke(app, ["config"])
    assert result.exit_code == 0, result.output
    assert result.output.splitlines()[:3] == ["show_clock = false", "theme = textual-dark  (default)", "search_limit = 10  (default)"]


def test_config_without_a_file_lists_the_defaults(settings_file):
    result = runner.invoke(app, ["config"])
    assert result.output.splitlines() == [
        f"{key} = {settings.display(getattr(settings.DEFAULTS, key))}  (default)" for key in settings.KEYS
    ]
    assert not settings_file.exists()


def test_config_get_prints_the_value(settings_file):
    settings.save(settings.Settings(theme="nord"))
    assert runner.invoke(app, ["config", "get", "theme"]).output == "nord\n"
    assert runner.invoke(app, ["config", "get", "show_clock"]).output == "true\n"


def test_config_set_saves_and_prints_the_setting(monkeypatch, settings_file):
    no_network(monkeypatch)
    result = runner.invoke(app, ["config", "set", "show_clock", "false"])
    assert result.exit_code == 0, result.output
    assert result.output == "show_clock = false\n"
    assert settings.load() == settings.Settings(show_clock=False)


def test_config_path_prints_the_file(settings_file):
    assert runner.invoke(app, ["config", "path"]).output == f"{settings_file}\n"


@pytest.mark.parametrize(
    "args, message",
    [
        (["get", "clock"], "Unknown setting 'clock'; valid keys: show_clock, theme, search_limit, server_host, server_port, server_token, remote_url, stream_enabled, spotify_client_id, show_levels, show_art, show_lyrics, search_source, radio, normalize_loudness, seek_seconds, volume_step\n"),
        (["set", "clock", "1"], "Unknown setting 'clock'; valid keys: show_clock, theme, search_limit, server_host, server_port, server_token, remote_url, stream_enabled, spotify_client_id, show_levels, show_art, show_lyrics, search_source, radio, normalize_loudness, seek_seconds, volume_step\n"),
        (["set", "search_limit", "99"], "search_limit must be between 1 and 50, not 99\n"),
        (["set", "show_clock", "nope"], "show_clock must be true or false, not 'nope'; valid keys: show_clock, theme, search_limit, server_host, server_port, server_token, remote_url, stream_enabled, spotify_client_id, show_levels, show_art, show_lyrics, search_source, radio, normalize_loudness, seek_seconds, volume_step\n"),
    ],
)
def test_config_errors_are_one_line_and_exit_1(settings_file, args, message):
    result = runner.invoke(app, ["config", *args])
    assert result.exit_code == 1
    assert result.stderr == message
    assert not settings_file.exists()


@pytest.mark.parametrize("args", [[], ["get", "theme"], ["set", "theme", "nord"]])
def test_config_with_a_broken_file_is_one_line_and_exit_1(settings_file, args):
    settings_file.write_text("theme = 3", encoding="utf-8")
    result = runner.invoke(app, ["config", *args])
    assert result.exit_code == 1
    assert result.stderr == "theme must be str, not 3\n"


@pytest.mark.parametrize("args", [[], ["get", "theme"], ["set", "theme", "nord"]])
def test_config_with_an_unreadable_file_is_one_line_and_exit_1(settings_file, args):
    settings_file.mkdir(parents=True)
    result = runner.invoke(app, ["config", *args])
    assert result.exit_code == 1
    assert result.stderr.startswith(f"Cannot read {settings_file}: ")
    assert result.stderr.count("\n") == 1


def test_config_set_with_an_unwritable_file_is_one_line_and_exit_1(monkeypatch, settings_file):
    def refuse(*args, **kwargs):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(settings.Path, "write_text", refuse)
    result = runner.invoke(app, ["config", "set", "theme", "nord"])
    assert result.exit_code == 1
    assert result.stderr == f"Cannot write {settings_file}: Permission denied\n"


def test_cli_starts_without_loading_textual():
    code = "import sys, ttyplayer.cli; print('textual' in sys.modules)"
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert result.stdout == "False\n"


def test_doctor_runs_without_yt_dlp(tmp_path):
    # Through main(), the real entry point: it is what makes the output UTF-8 on a Windows pipe.
    code = (
        "import sys; sys.modules['yt_dlp'] = None; sys.argv = ['ttyplayer', 'doctor']; "
        "from ttyplayer.cli import main; main()"
    )
    env = {**os.environ, "XDG_DATA_HOME": str(tmp_path)}
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, encoding="utf-8", env=env)
    assert result.returncode == 1, result.stderr
    lines = result.stdout.splitlines()
    assert len(lines) == 8
    assert lines[2].startswith("✗ yt-dlp")


def test_lookup_prints_its_time_with_timing(monkeypatch):
    monkeypatch.setenv("TTYPLAYER_TIMING", "1")
    ticks = iter([5.0, 6.8])
    monkeypatch.setattr("ttyplayer.cli.time.monotonic", lambda: next(ticks))
    monkeypatch.setattr(youtube, "search", lambda query, limit=5, source="youtube": ONE_VIDEO)

    result = runner.invoke(app, ["search", "song"])

    assert result.exit_code == 0
    assert result.stderr == "lookup took 1.8s\n"
    assert result.output.index("lookup took") < result.output.index("1. t")


def test_lookup_prints_nothing_without_timing(monkeypatch):
    monkeypatch.delenv("TTYPLAYER_TIMING", raising=False)
    monkeypatch.setattr(youtube, "search", lambda query, limit=5, source="youtube": ONE_VIDEO)

    result = runner.invoke(app, ["search", "song"])

    assert result.exit_code == 0
    assert result.stderr == ""


ROOT = Path(__file__).resolve().parents[1]
VERSIONS = {
    "mpv": "mpv v0.39.0 Copyright © 2000-2024 mpv/MPlayer/mplayer2 projects",
    "ffmpeg": "ffmpeg version 7.0.2 Copyright (c) 2000-2024 the FFmpeg developers",
}
VERSION_FLAGS = {"mpv": "--version", "ffmpeg": "-version"}  # real ffmpeg exits 8 on --version


def answer_version(argv, **kwargs):
    """A fake subprocess.run for mpv and ffmpeg that, like the real ones, only knows its own version flag."""
    path, flag = argv
    name = os.path.basename(path)
    if flag != VERSION_FLAGS[name]:
        raise subprocess.CalledProcessError(8, argv)
    return subprocess.CompletedProcess(argv, 0, stdout=VERSIONS[name] + "\nbuilt with gcc\n")


def healthy(monkeypatch, tmp_path):
    """A machine where every doctor check passes, without touching the real one."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setattr(shutil, "which", lambda name: f"/opt/bin/{name}")
    monkeypatch.setattr(subprocess, "run", answer_version)
    monkeypatch.setattr(control, "private_dir", lambda directory: None)
    monkeypatch.setattr(art, "available", lambda: True)


def test_doctor_all_green(monkeypatch, tmp_path):
    healthy(monkeypatch, tmp_path)
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert len(lines) == 8
    assert all(line.startswith("✓ ") for line in lines)
    assert f"✓ ttyplayer {metadata.version('ttyplayer')}" in lines
    assert any(line.startswith("✓ yt-dlp 20") for line in lines)
    assert f"✓ {VERSIONS['mpv']}" in lines
    assert f"✓ {VERSIONS['ffmpeg']}" in lines
    assert f"✓ data dir {tmp_path / 'ttyplayer'}" in lines
    assert "✓ album art" in lines


def test_doctor_without_the_art_extra_shows_how_to_get_it_and_passes(monkeypatch, tmp_path):
    healthy(monkeypatch, tmp_path)
    monkeypatch.setattr(art, "available", lambda: False)
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "- album art not installed: uv tool install 'ttyplayer[art]'" in result.output.splitlines()


def test_doctor_without_ffmpeg_shows_a_dash_row_and_passes(monkeypatch, tmp_path):
    healthy(monkeypatch, tmp_path)
    monkeypatch.setattr(shutil, "which", lambda name: None if name == "ffmpeg" else f"/opt/bin/{name}")
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output
    assert f"- ffmpeg not found on PATH; only serve --stream needs it. {cli.ffmpeg_install_hint()}" in result.output.splitlines()


def test_doctor_reports_an_ffmpeg_that_does_not_answer(monkeypatch, tmp_path):
    healthy(monkeypatch, tmp_path)

    def run(argv, **kwargs):
        if argv[0].endswith("ffmpeg"):
            raise OSError("Exec format error")
        return answer_version(argv, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 1
    assert "✗ ffmpeg at /opt/bin/ffmpeg did not answer -version: Exec format error" in result.output.splitlines()


def test_doctor_without_mpv_prints_the_install_hint_and_fails(monkeypatch, tmp_path):
    healthy(monkeypatch, tmp_path)
    monkeypatch.setattr(shutil, "which", lambda name: None)
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 1
    [mpv] = [line for line in result.output.splitlines() if line.startswith("✗")]
    assert mpv == f"✗ mpv not found on PATH. {cli.mpv_install_hint()}"


@pytest.mark.skipif(utils.WINDOWS, reason="the POSIX control socket dir")
def test_doctor_prints_why_the_control_dir_is_unsafe(monkeypatch, tmp_path):
    healthy(monkeypatch, tmp_path)

    def unsafe(directory):
        raise OSError(f"{directory} is open to other users")

    monkeypatch.setattr(control, "private_dir", unsafe)
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 1
    assert "is open to other users" in result.output.splitlines()[-1]
    assert result.output.splitlines()[-1].startswith("✗ control socket dir: ")


def test_doctor_on_windows_checks_the_control_file_dir(monkeypatch, tmp_path):
    healthy(monkeypatch, tmp_path)
    monkeypatch.setattr(control, "WINDOWS", True)
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output
    assert result.output.splitlines()[-1] == f"✓ control socket dir {tmp_path / 'ttyplayer'}"


def test_doctor_reports_an_unwritable_data_dir(monkeypatch, tmp_path):
    healthy(monkeypatch, tmp_path)
    blocker = tmp_path / "file"
    blocker.write_text("")
    monkeypatch.setenv("XDG_DATA_HOME", str(blocker))
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 1
    assert any(line.startswith("✗ data dir ") for line in result.output.splitlines())


@pytest.mark.parametrize(
    "platform, hint",
    [
        ("darwin", "Install it with: brew install mpv"),
        (
            "linux",
            "Install it with: sudo apt-get install -y mpv (or: sudo dnf install -y mpv, "
            "sudo pacman -S --noconfirm mpv, sudo zypper install -y mpv, sudo apk add mpv)",
        ),
        ("win32", "Install it with: winget install -e --id shinchiro.mpv (or: scoop install mpv, choco install mpv)"),
        ("sunos5", "Install it from https://mpv.io/installation/"),
    ],
)
def test_mpv_install_hint_per_platform(platform, hint):
    assert cli.mpv_install_hint(platform) == hint


@pytest.mark.parametrize(
    "platform, hint",
    [
        ("darwin", "Install it with: brew install ffmpeg"),
        (
            "linux",
            "Install it with: sudo apt-get install -y ffmpeg (or: sudo dnf install -y ffmpeg-free, "
            "sudo pacman -S --noconfirm ffmpeg, sudo apk add ffmpeg)",
        ),
        ("win32", "Install it with: winget install -e --id Gyan.FFmpeg (or: scoop install ffmpeg, choco install ffmpeg)"),
        ("sunos5", "Install it from https://ffmpeg.org/download.html"),
    ],
)
def test_ffmpeg_install_hint_per_platform(platform, hint):
    assert cli.ffmpeg_install_hint(platform) == hint


def test_readme_shows_every_ffmpeg_install_command():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for commands in cli.FFMPEG_INSTALL.values():
        for command in commands:
            assert command in readme


def test_readme_shows_every_mpv_install_command():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for commands in cli.MPV_INSTALL.values():
        for command in commands:
            assert command in readme


def test_wheel_keeps_the_tui_stylesheet():
    """uv_build ships package data by default; nothing in pyproject may exclude tui.tcss."""
    assert (ROOT / "src" / "ttyplayer" / "tui.tcss").is_file()
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert pyproject["build-system"]["build-backend"] == "uv_build"
    backend = pyproject.get("tool", {}).get("uv", {}).get("build-backend", {})
    assert not backend.get("wheel-exclude")
    assert not backend.get("source-exclude")


PRODUCT_DOCS = ["README.md", "docs/architecture.md", "docs/tui-design.md"]


@pytest.mark.parametrize("doc", PRODUCT_DOCS)
def test_product_docs_read_as_product_docs(doc):
    text = (ROOT / doc).read_text(encoding="utf-8").lower()
    for word in ["relayflow", "builder", "reviewer", "orchestrator", "agent"]:
        assert word not in text, f"{doc} mentions {word!r}"


def test_the_old_name_is_gone():
    """Only the lab-only history files may still say the old name."""
    old = utils.OLD_NAME
    paths = [ROOT / "src", ROOT / "tests", ROOT / ".github", ROOT / "pyproject.toml", *(ROOT / doc for doc in PRODUCT_DOCS)]
    files = [file for path in paths for file in ([path] if path.is_file() else path.rglob("*")) if file.is_file()]
    assert len(files) > 10
    for file in files:
        if "__pycache__" not in file.parts:
            assert old not in file.read_bytes().decode("utf-8", errors="replace").lower(), file


class FakeStream:
    def __init__(self, encoding):
        self.encoding = encoding
        self.reconfigured = []

    def reconfigure(self, **kwargs):
        self.reconfigured.append(kwargs)


def test_utf8_streams_reconfigures_only_legacy_encodings(monkeypatch):
    from ttyplayer import cli

    legacy, modern = FakeStream("cp1252"), FakeStream("UTF-8")
    monkeypatch.setattr(cli.sys, "stdout", legacy)
    monkeypatch.setattr(cli.sys, "stderr", modern)
    cli.utf8_streams()
    assert legacy.reconfigured == [{"encoding": "utf-8", "errors": "replace"}]
    assert modern.reconfigured == []


def test_license_holder_is_the_company_and_the_text_ships():
    assert "Copyright (c) 2026 Weblift SRL" in (ROOT / "LICENSE").read_text(encoding="utf-8")
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert pyproject["project"]["license-files"] == ["LICENSE"]


# --- playlist ---------------------------------------------------------------

ONE = Video(id="1", title="One", uploader="u", duration=60)
TWO = Video(id="2", title="Two", uploader="u", duration=120)
THREE = Video(id="3", title="Three", uploader="u", duration=None)


@pytest.fixture
def data_home(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    return tmp_path


def playlist_ids(name):
    return [video.id for video in playlists.load(name)]


def make_playlist(name, videos):
    playlists.create(name)
    playlists.add(name, videos)


def test_playlist_list_shows_each_name_and_count(data_home):
    make_playlist("chill", [ONE, TWO])
    playlists.create("empty")
    result = runner.invoke(app, ["playlist", "list"])
    assert result.exit_code == 0, result.output
    assert result.output == "chill  (2 videos)\nempty  (0 videos)\n"


def test_playlist_list_when_there_are_none(data_home):
    result = runner.invoke(app, ["playlist", "list"])
    assert result.exit_code == 1
    assert result.stderr == "No playlists yet\n"


def test_playlist_show_numbers_the_videos_in_order(data_home):
    make_playlist("chill", [TWO, ONE, TWO])
    result = runner.invoke(app, ["playlist", "show", "chill"])
    assert result.exit_code == 0, result.output
    assert result.output == " 1. Two  (2:00)  u\n 2. One  (1:00)  u\n 3. Two  (2:00)  u\n"


def test_playlist_show_an_empty_playlist(data_home):
    playlists.create("chill")
    result = runner.invoke(app, ["playlist", "show", "chill"])
    assert result.exit_code == 0
    assert result.output == "chill is empty\n"


@pytest.mark.parametrize(
    "args",
    [
        ["show", "nope"],
        ["add", "nope", "https://www.youtube.com/watch?v=1"],
        ["remove", "nope", "1"],
        ["move", "nope", "1", "2"],
        ["delete", "nope", "--yes"],
        ["play", "nope"],
    ],
)
def test_playlist_commands_on_a_missing_playlist(args, data_home, monkeypatch):
    monkeypatch.setattr(youtube, "fetch", lambda url: pytest.fail("looked up before checking"))
    result = runner.invoke(app, ["playlist", *args])
    assert result.exit_code == 1
    assert result.stderr == "No playlist named nope\n"
    assert result.stdout == ""


def test_playlist_create(data_home):
    result = runner.invoke(app, ["playlist", "create", "road trip"])
    assert result.exit_code == 0, result.output
    assert result.output == "Created playlist road trip\n"
    assert playlists.names() == ["road trip"]


def test_playlist_create_twice_fails(data_home):
    playlists.create("chill")
    result = runner.invoke(app, ["playlist", "create", "chill"])
    assert result.exit_code == 1
    assert result.stderr == "A playlist named chill already exists\n"


def test_playlist_create_with_a_bad_name_fails_in_one_line(data_home):
    result = runner.invoke(app, ["playlist", "create", "../x"])
    assert result.exit_code == 1
    assert result.stderr == "Bad playlist name '../x': use 1 to 40 letters, digits, spaces, _ or -\n"
    assert playlists.names() == []


def test_playlist_add_a_link_adds_every_video(data_home, monkeypatch):
    make_playlist("chill", [ONE])
    monkeypatch.setattr(youtube, "fetch", lambda url: [TWO, THREE])
    result = runner.invoke(app, ["playlist", "add", "chill", "https://www.youtube.com/playlist?list=x"])
    assert result.exit_code == 0, result.output
    assert result.output == "Added 2 videos to chill\n"
    assert playlist_ids("chill") == ["1", "2", "3"]


def test_playlist_add_search_words_adds_the_picks(data_home, monkeypatch):
    playlists.create("chill")
    searched = []

    def search(query, limit=5, source="youtube"):
        searched.append((query, limit))
        return [ONE, TWO, THREE]

    monkeypatch.setattr(youtube, "search", search)
    result = runner.invoke(app, ["playlist", "add", "chill", "some", "song", "--limit", "3"], input="3 1\n")
    assert result.exit_code == 0, result.output
    assert result.output.splitlines()[-1] == "Added 2 videos to chill"
    assert searched == [("some song", 3)]
    assert playlist_ids("chill") == ["3", "1"]


def test_playlist_add_reports_youtube_errors_plainly(data_home, monkeypatch):
    playlists.create("chill")
    monkeypatch.setattr(youtube, "fetch", raise_youtube_error)
    result = runner.invoke(app, ["playlist", "add", "chill", "https://www.youtube.com/watch?v=1"])
    assert result.exit_code == 1
    assert result.stderr == "YouTube lookup failed: no internet\n"
    assert playlists.load("chill") == []


def test_playlist_remove(data_home):
    make_playlist("chill", [ONE, TWO])
    result = runner.invoke(app, ["playlist", "remove", "chill", "1"])
    assert result.exit_code == 0, result.output
    assert result.output == "Removed: One\n"
    assert playlist_ids("chill") == ["2"]


def test_playlist_remove_out_of_range(data_home):
    make_playlist("chill", [ONE])
    result = runner.invoke(app, ["playlist", "remove", "chill", "2"])
    assert result.exit_code == 1
    assert result.stderr == "No video number 2 in chill\n"
    assert playlist_ids("chill") == ["1"]


def test_playlist_move(data_home):
    make_playlist("chill", [ONE, TWO, THREE])
    result = runner.invoke(app, ["playlist", "move", "chill", "3", "1"])
    assert result.exit_code == 0, result.output
    assert result.output == "Moved to 1: Three\n"
    assert playlist_ids("chill") == ["3", "1", "2"]


def test_playlist_move_out_of_range(data_home):
    make_playlist("chill", [ONE, TWO])
    result = runner.invoke(app, ["playlist", "move", "chill", "1", "5"])
    assert result.exit_code == 1
    assert result.stderr == "No video number 5 in chill\n"
    assert playlist_ids("chill") == ["1", "2"]


def test_playlist_delete_asks_and_yes_deletes(data_home):
    playlists.create("chill")
    result = runner.invoke(app, ["playlist", "delete", "chill"], input="y\n")
    assert result.exit_code == 0, result.output
    assert "Delete playlist chill? [y/N]" in result.output
    assert result.output.endswith("Deleted playlist chill\n")
    assert playlists.names() == []


@pytest.mark.parametrize("answer", ["n\n", "\n"])
def test_playlist_delete_keeps_it_unless_the_answer_is_yes(answer, data_home):
    playlists.create("chill")
    result = runner.invoke(app, ["playlist", "delete", "chill"], input=answer)
    assert result.exit_code == 0, result.output
    assert result.output.endswith("Kept chill\n")
    assert playlists.names() == ["chill"]


def test_playlist_delete_yes_does_not_ask(data_home):
    playlists.create("chill")
    result = runner.invoke(app, ["playlist", "delete", "chill", "--yes"])
    assert result.exit_code == 0, result.output
    assert result.output == "Deleted playlist chill\n"
    assert playlists.names() == []


def test_playlist_play_queues_the_whole_playlist_in_order(data_home, monkeypatch):
    make_playlist("chill", [TWO, ONE, TWO])
    monkeypatch.setattr(player, "MpvClient", FakeClient)
    FakeClient.instances = []
    result = runner.invoke(app, ["playlist", "play", "chill", "--video"])
    assert result.exit_code == 0, result.output
    client = FakeClient.instances[0]
    assert [v.id for v in client.queue] == ["2", "1", "2"]
    assert client.video is True
    assert client.ran
    assert [v.id for v in history.load()] == ["2"]


def test_playlist_play_shuffle_shuffles_with_the_shuffler(data_home, monkeypatch):
    videos = [Video(id=str(n), title=f"v{n}", uploader="u", duration=1) for n in range(8)]
    make_playlist("chill", videos)
    monkeypatch.setattr(player, "MpvClient", FakeClient)
    monkeypatch.setattr(cli, "shuffler", random.Random(7))
    expected = list(videos)
    random.Random(7).shuffle(expected)
    FakeClient.instances = []
    result = runner.invoke(app, ["playlist", "play", "chill", "--shuffle"])
    assert result.exit_code == 0, result.output
    assert FakeClient.instances[0].queue == expected
    assert expected != videos
    assert playlist_ids("chill") == [v.id for v in videos]


def test_playlist_play_an_empty_playlist(data_home, monkeypatch):
    playlists.create("chill")
    monkeypatch.setattr(player, "MpvClient", FakeClient)
    FakeClient.instances = []
    result = runner.invoke(app, ["playlist", "play", "chill"])
    assert result.exit_code == 1
    assert result.stderr == "No videos found\n"
    assert FakeClient.instances == []


def test_playlist_import_names_it_after_the_sanitized_title(data_home, monkeypatch):
    urls = []

    def fetch_playlist(url):
        urls.append(url)
        return "Road Trip: '90s / hits!", [ONE, TWO]

    monkeypatch.setattr(youtube, "fetch_playlist", fetch_playlist)
    result = runner.invoke(app, ["playlist", "import", "https://www.youtube.com/playlist?list=x"])
    assert result.exit_code == 0, result.output
    assert result.output == "Imported 2 videos as Road Trip 90s hits\n"
    assert urls == ["https://www.youtube.com/playlist?list=x"]
    assert playlist_ids("Road Trip 90s hits") == ["1", "2"]


def test_playlist_import_as_a_given_name(data_home, monkeypatch):
    monkeypatch.setattr(youtube, "fetch_playlist", lambda url: ("Whatever", [ONE]))
    result = runner.invoke(app, ["playlist", "import", "https://www.youtube.com/playlist?list=x", "--as", "roadtrip"])
    assert result.exit_code == 0, result.output
    assert playlists.names() == ["roadtrip"]


def test_playlist_import_a_title_with_nothing_usable_asks_for_as(data_home, monkeypatch):
    monkeypatch.setattr(youtube, "fetch_playlist", lambda url: ("日本の歌", [ONE]))
    result = runner.invoke(app, ["playlist", "import", "https://www.youtube.com/playlist?list=x"])
    assert result.exit_code == 1
    assert result.stderr == "The playlist title has nothing to name it by; name it with --as\n"
    assert playlists.names() == []


def test_playlist_import_a_single_video_link_fails(data_home, monkeypatch):
    monkeypatch.setattr(youtube, "fetch_playlist", lambda url: (None, [ONE]))
    result = runner.invoke(app, ["playlist", "import", "https://www.youtube.com/watch?v=1"])
    assert result.exit_code == 1
    assert result.stderr == "Not a playlist link: https://www.youtube.com/watch?v=1\n"
    assert playlists.names() == []


def test_playlist_import_onto_an_existing_name_fails_without_changes(data_home, monkeypatch):
    make_playlist("roadtrip", [THREE])
    monkeypatch.setattr(youtube, "fetch_playlist", lambda url: ("roadtrip", [ONE]))
    result = runner.invoke(app, ["playlist", "import", "https://www.youtube.com/playlist?list=x"])
    assert result.exit_code == 1
    assert result.stderr == "A playlist named roadtrip already exists\n"
    assert playlist_ids("roadtrip") == ["3"]


def test_playlist_import_reports_youtube_errors_plainly(data_home, monkeypatch):
    monkeypatch.setattr(youtube, "fetch_playlist", raise_youtube_error)
    result = runner.invoke(app, ["playlist", "import", "https://www.youtube.com/playlist?list=x"])
    assert result.exit_code == 1
    assert result.stderr == "YouTube lookup failed: no internet\n"


@pytest.fixture
def config_home(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    return tmp_path / "config"


def raise_spotify_error(*args):
    raise spotify.SpotifyError("Not logged in to Spotify: run ttyplayer spotify login")


def test_spotify_login_without_a_client_id_points_at_the_readme(config_home, monkeypatch):
    monkeypatch.setattr(spotify, "login", lambda *args: pytest.fail("login ran"))
    result = runner.invoke(app, ["spotify", "login"])
    assert result.exit_code == 1
    assert result.stderr == (
        "Set spotify_client_id first: ttyplayer config set spotify_client_id <id> (see the README's Spotify section)\n"
    )


def test_spotify_login_logs_in_with_the_client_id_and_greets(config_home, monkeypatch):
    settings.update("spotify_client_id", "cid")
    calls = []

    def login(client_id, echo):
        calls.append(client_id)
        echo("Opening Spotify in your browser; if it does not open, visit:\nhttps://accounts.spotify.com/authorize?x")
        return "Ana"

    monkeypatch.setattr(spotify, "login", login)
    result = runner.invoke(app, ["spotify", "login"])
    assert result.exit_code == 0, result.output
    assert calls == ["cid"]
    assert result.output.endswith("Logged in as Ana\n")


def test_spotify_login_reports_a_refusal_in_one_line(config_home, monkeypatch):
    settings.update("spotify_client_id", "cid")

    def refused(*args):
        raise spotify.SpotifyError("Spotify refused the login: access_denied")

    monkeypatch.setattr(spotify, "login", refused)
    result = runner.invoke(app, ["spotify", "login"])
    assert result.exit_code == 1
    assert result.stderr == "Spotify refused the login: access_denied\n"


def test_spotify_playlists_lists_name_tracks_and_id(monkeypatch):
    monkeypatch.setattr(spotify, "user_playlists", lambda: [("Road Trip", 12, "p1"), ("Chill", 3, "p2")])
    result = runner.invoke(app, ["spotify", "playlists"])
    assert result.exit_code == 0, result.output
    assert result.output == "Road Trip  12  p1\nChill  3  p2\n"


def test_spotify_playlists_with_none_or_an_error_fails(monkeypatch):
    monkeypatch.setattr(spotify, "user_playlists", lambda: [])
    assert runner.invoke(app, ["spotify", "playlists"]).stderr == "No Spotify playlists\n"
    monkeypatch.setattr(spotify, "user_playlists", raise_spotify_error)
    result = runner.invoke(app, ["spotify", "playlists"])
    assert result.exit_code == 1
    assert result.stderr == "Not logged in to Spotify: run ttyplayer spotify login\n"


def fake_spotify_playlist(monkeypatch, title, tracks):
    calls = []

    def playlist(ref, limit=None):
        calls.append((ref, limit))
        return title, tracks[:limit]

    monkeypatch.setattr(spotify, "playlist", playlist)
    return calls


TRACKS = [spotify.Track("Ann", "One"), spotify.Track("Bob", "Two")]


def test_spotify_import_resolves_each_track_and_saves_under_the_sanitized_name(data_home, monkeypatch):
    calls = fake_spotify_playlist(monkeypatch, "Road Trip: '90s!", TRACKS)
    monkeypatch.setattr(youtube, "search", lambda query, limit=5, source="youtube": [ONE] if query == "Ann One" else [])
    result = runner.invoke(app, ["spotify", "import", "spotify:playlist:x"])
    assert result.exit_code == 0, result.output
    assert calls == [("spotify:playlist:x", None)]
    assert result.output == (
        "[1/2] ✓ Ann – One → One\n"
        "[2/2] ✗ Bob – Two not found\n"
        "Saved 1 of 2 tracks to Road Trip 90s\n"
    )
    assert playlist_ids("Road Trip 90s") == ["1"]


def test_spotify_import_as_a_name_with_a_limit_appends_to_it(data_home, monkeypatch):
    make_playlist("roadtrip", [THREE])
    calls = fake_spotify_playlist(monkeypatch, "Whatever", TRACKS)
    monkeypatch.setattr(youtube, "search", lambda query, limit=5, source="youtube": [ONE])
    result = runner.invoke(app, ["spotify", "import", "x", "--as", "roadtrip", "--limit", "1"])
    assert result.exit_code == 0, result.output
    assert calls == [("x", 1)]
    assert result.output.endswith("Saved 1 of 1 tracks to roadtrip\n")
    assert playlist_ids("roadtrip") == ["3", "1"]


def test_spotify_import_a_name_with_nothing_usable_asks_for_as(data_home, monkeypatch):
    fake_spotify_playlist(monkeypatch, "日本の歌", TRACKS)
    result = runner.invoke(app, ["spotify", "import", "x"])
    assert result.exit_code == 1
    assert result.stderr == "The playlist title has nothing to name it by; name it with --as\n"
    assert playlists.names() == []


def test_spotify_import_a_bad_as_name_fails_in_one_line(data_home, monkeypatch):
    fake_spotify_playlist(monkeypatch, "Whatever", TRACKS)
    result = runner.invoke(app, ["spotify", "import", "x", "--as", "../evil"])
    assert result.exit_code == 1
    assert result.stderr.startswith("Bad playlist name '../evil'")


def test_spotify_import_reports_spotify_errors_plainly(data_home, monkeypatch):
    monkeypatch.setattr(spotify, "playlist", raise_spotify_error)
    result = runner.invoke(app, ["spotify", "import", "x"])
    assert result.exit_code == 1
    assert result.stderr == "Not logged in to Spotify: run ttyplayer spotify login\n"
    assert playlists.names() == []


def test_spotify_import_keeps_what_it_added_and_sums_up_when_youtube_fails(data_home, monkeypatch):
    fake_spotify_playlist(monkeypatch, "road", TRACKS)

    def search(query, limit=5, source="youtube"):
        if query == "Bob Two":
            raise youtube.YouTubeError("no internet")
        return [ONE]

    monkeypatch.setattr(youtube, "search", search)
    result = runner.invoke(app, ["spotify", "import", "x"])
    assert result.exit_code == 1
    assert result.stdout == "[1/2] ✓ Ann – One → One\nSaved 1 of 2 tracks to road\n"
    assert result.stderr == "YouTube lookup failed: no internet\n"
    assert playlist_ids("road") == ["1"]


def test_spotify_import_limit_must_be_positive():
    result = runner.invoke(app, ["spotify", "import", "x", "--limit", "0"])
    assert result.exit_code == 2

QUEUE_REPLY = {
    "ok": True,
    "videos": [
        {"id": "1", "title": "One", "uploader": "u", "duration": 60},
        {"id": "2", "title": "Two", "uploader": "u", "duration": None},
    ],
    "index": 2,
}


def test_playlist_save_queue_replaces_the_playlist_with_the_queue(data_home, monkeypatch):
    make_playlist("chill", [THREE])
    send = fake_send(QUEUE_REPLY)
    monkeypatch.setattr(control, "send", send)
    result = runner.invoke(app, ["playlist", "save-queue", "chill"])
    assert result.exit_code == 0, result.output
    assert result.output == "Saved 2 videos to chill\n"
    assert send.names == ["queue"]
    # the queue's entries name no thumbnail, so each gets its video id's hqdefault.jpg, which the playlist keeps
    assert playlists.load("chill") == [
        dataclasses.replace(ONE, thumbnail="https://i.ytimg.com/vi/1/hqdefault.jpg"),
        Video(id="2", title="Two", uploader="u", duration=None, thumbnail="https://i.ytimg.com/vi/2/hqdefault.jpg"),
    ]


def test_playlist_save_queue_creates_a_new_playlist(data_home, monkeypatch):
    monkeypatch.setattr(control, "send", fake_send(QUEUE_REPLY))
    result = runner.invoke(app, ["playlist", "save-queue", "new one"])
    assert result.exit_code == 0, result.output
    assert playlist_ids("new one") == ["1", "2"]


def test_playlist_save_queue_with_nothing_playing(data_home, monkeypatch):
    make_playlist("chill", [THREE])
    monkeypatch.setattr(control, "send", no_player)
    result = runner.invoke(app, ["playlist", "save-queue", "chill"])
    assert result.exit_code == 1
    assert result.stderr == "No ttyplayer is playing\n"
    assert playlist_ids("chill") == ["3"]


def test_playlist_save_queue_of_an_empty_queue_changes_nothing(data_home, monkeypatch):
    make_playlist("chill", [THREE])
    monkeypatch.setattr(control, "send", fake_send({"ok": True, "videos": [], "index": 1}))
    result = runner.invoke(app, ["playlist", "save-queue", "chill"])
    assert result.exit_code == 1
    assert result.stderr == "The queue is empty\n"
    assert playlist_ids("chill") == ["3"]


def test_playlist_save_queue_with_a_bad_name_asks_no_player(data_home, monkeypatch):
    send = fake_send(QUEUE_REPLY)
    monkeypatch.setattr(control, "send", send)
    result = runner.invoke(app, ["playlist", "save-queue", "a/b"])
    assert result.exit_code == 1
    assert result.stderr.startswith("Bad playlist name 'a/b'")
    assert send.names == []


class FakeServeClient(FakeClient):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.handle_control = lambda name: control.ok()

    def quit(self):
        serve_log.append("client.quit")


class FakeRemote:
    def stop(self):
        serve_log.append("remote.stop")


class FakeServerThread:
    def __init__(self, app, host, port):
        serve_log.append(("server.start", host, port))
        self.app = app

    def stop(self):
        serve_log.append("server.stop")


serve_log = []


@pytest.fixture
def serve_fakes(monkeypatch, settings_file):
    from ttyplayer import server

    serve_log.clear()
    FakeClient.instances = []
    monkeypatch.setattr(player, "MpvClient", FakeServeClient)
    monkeypatch.setattr(control, "serve", lambda handler: serve_log.append("remote.serve") or FakeRemote())
    monkeypatch.setattr(server, "ServerThread", FakeServerThread)

    def interrupted(seconds):
        serve_log.append("waiting")
        raise KeyboardInterrupt

    monkeypatch.setattr(cli.time, "sleep", interrupted)
    return server


def test_serve_wires_the_player_remote_control_and_server_then_stops_them_in_order(serve_fakes, settings_file):
    result = runner.invoke(app, ["serve"])
    assert result.exit_code == 0, result.output
    token = settings.load().server_token
    assert token
    assert result.output.startswith(f"Serving ttyplayer at http://127.0.0.1:7700/?token={token}\n")
    assert "█" in result.output or "▀" in result.output  # the QR code
    [client] = FakeClient.instances
    assert client.video is False
    assert client.on_play is history.record
    assert isinstance(client.on_state, serve_fakes.Broadcaster)
    assert serve_log == [
        "remote.serve", ("server.start", "127.0.0.1", 7700), "waiting", "server.stop", "remote.stop", "client.quit"
    ]


def test_serve_takes_host_and_port_from_the_flags_then_the_settings(serve_fakes, settings_file):
    settings.save(settings.Settings(server_host="0.0.0.0", server_port=8800, server_token="kept"))
    assert runner.invoke(app, ["serve"]).exit_code == 0
    assert serve_log[1] == ("server.start", "0.0.0.0", 8800)
    serve_log.clear()
    result = runner.invoke(app, ["serve", "--host", "127.0.0.2", "--port", "9900"])
    assert serve_log[1] == ("server.start", "127.0.0.2", 9900)
    assert "http://127.0.0.2:9900/?token=kept" in result.output
    assert settings.load().server_token == "kept"


def test_serve_reports_missing_mpv(monkeypatch, serve_fakes):
    def no_mpv(*args, **kwargs):
        raise FileNotFoundError("mpv")

    monkeypatch.setattr(player, "MpvClient", no_mpv)
    result = runner.invoke(app, ["serve"])
    assert result.exit_code == 1
    assert result.stderr.startswith("mpv is not installed.")
    assert result.stderr.count("\n") == 1
    assert serve_log == []


def test_serve_on_a_port_in_use_stops_the_rest_and_says_so(monkeypatch, serve_fakes):
    def in_use(app, host, port):
        raise OSError(98, "Address already in use")

    monkeypatch.setattr(serve_fakes, "ServerThread", in_use)
    result = runner.invoke(app, ["serve"])
    assert result.exit_code == 1
    assert result.stderr == "Cannot serve on 127.0.0.1:7700: Address already in use\n"
    assert serve_log == ["remote.serve", "remote.stop", "client.quit"]


class FakeStreamer:
    def __init__(self, pcm, ffmpeg):
        serve_log.append(("streamer.start", pcm, ffmpeg))

    def stop(self):
        serve_log.append("streamer.stop")


@pytest.fixture
def stream_fakes(monkeypatch, serve_fakes):
    from ttyplayer import stream

    monkeypatch.setattr(stream, "Streamer", FakeStreamer)
    monkeypatch.setattr(stream, "find_ffmpeg", lambda: "/opt/bin/ffmpeg")
    monkeypatch.setattr(stream, "WINDOWS", False)
    return stream


def test_serve_stream_pipes_mpv_into_ffmpeg_and_stops_mpv_before_ffmpeg(stream_fakes, serve_fakes):
    result = runner.invoke(app, ["serve", "--stream"])
    assert result.exit_code == 0, result.output
    assert "Streaming: no sound plays here" in result.output
    [client] = FakeClient.instances
    assert isinstance(client.pcm, stream_fakes.StdoutPipe)
    assert serve_log == [
        ("streamer.start", client.pcm, "/opt/bin/ffmpeg"), "remote.serve", ("server.start", "127.0.0.1", 7700),
        "waiting", "server.stop", "remote.stop", "client.quit", "streamer.stop",
    ]  # fmt: skip


def test_serve_streams_when_stream_enabled_is_set(stream_fakes, serve_fakes):
    settings.save(settings.Settings(stream_enabled=True, server_token="kept"))
    assert runner.invoke(app, ["serve"]).exit_code == 0
    assert FakeClient.instances[0].headless_pcm is True
    assert "streamer.stop" in serve_log


def test_serve_without_stream_changes_nothing(stream_fakes, serve_fakes):
    result = runner.invoke(app, ["serve"])
    assert FakeClient.instances[0].headless_pcm is False
    assert "Streaming" not in result.output
    assert not [entry for entry in serve_log if "streamer" in str(entry)]


def test_serve_stream_without_ffmpeg_says_how_to_install_it_and_starts_nothing(monkeypatch, stream_fakes, serve_fakes):
    monkeypatch.setattr(stream_fakes, "find_ffmpeg", lambda: None)
    result = runner.invoke(app, ["serve", "--stream"])
    assert result.exit_code == 1
    assert result.stderr == f"serve --stream needs ffmpeg, which is not on PATH. {cli.ffmpeg_install_hint()}\n"
    assert FakeClient.instances == [] and serve_log == []


def test_serve_stream_on_windows_streams_through_a_named_pipe(monkeypatch, stream_fakes, serve_fakes, fake_winapi):
    monkeypatch.setattr(stream_fakes, "WINDOWS", True)
    result = runner.invoke(app, ["serve", "--stream"])
    assert result.exit_code == 0, result.output
    [client] = FakeClient.instances
    assert isinstance(client.pcm, stream_fakes.NamedPipe)
    assert serve_log[0] == ("streamer.start", client.pcm, "/opt/bin/ffmpeg")


def test_serve_stream_whose_pipe_name_is_taken_says_so_and_starts_nothing(monkeypatch, stream_fakes, serve_fakes, fake_winapi):
    monkeypatch.setattr(stream_fakes, "WINDOWS", True)
    fake_winapi.taken = True
    result = runner.invoke(app, ["serve", "--stream"])
    assert result.exit_code == 1
    assert result.stderr == "Cannot create the pipe mpv streams through: Access is denied\n"
    assert FakeClient.instances == [] and serve_log == []


def test_serve_stream_when_ffmpeg_cannot_start_stops_mpv(monkeypatch, stream_fakes, serve_fakes):
    def broken(pcm, ffmpeg):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(stream_fakes, "Streamer", broken)
    result = runner.invoke(app, ["serve", "--stream"])
    assert result.exit_code == 1
    assert result.stderr == "Cannot start ffmpeg at /opt/bin/ffmpeg: Permission denied\n"
    assert serve_log == ["client.quit"]


def test_wheel_ships_the_server_page(tmp_path):
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is not installed")
    subprocess.run([uv, "build", "--wheel", "--out-dir", str(tmp_path), str(ROOT)], check=True, capture_output=True)
    [wheel] = tmp_path.glob("*.whl")
    static = ROOT / "src" / "ttyplayer" / "static"
    assert {path.name for path in static.iterdir()} >= {"index.html", "remote.js", "remote.css", "manifest.webmanifest", "icon.svg"}
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
    for path in static.iterdir():
        assert f"ttyplayer/static/{path.name}" in names


# --- search sources ----------------------------------------------------------

SC_VIDEO = Video(id="123", title="Roygbiv", uploader="warp", duration=151, source="soundcloud", link="https://soundcloud.com/warp/roygbiv")


def recording_search(monkeypatch, videos):
    """Fakes youtube.search; returns the list of sources it was asked for."""
    sources = []

    def search(query, limit=5, source="youtube"):
        sources.append(source)
        return videos

    monkeypatch.setattr(youtube, "search", search)
    return sources


def test_search_source_flag_searches_soundcloud_and_tags_its_rows(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    sources = recording_search(monkeypatch, [SC_VIDEO, *ONE_VIDEO])
    result = runner.invoke(app, ["search", "--source", "soundcloud", "boards", "of", "canada"])
    assert result.exit_code == 0, result.output
    assert sources == ["soundcloud"]
    assert result.output == " 1. SC Roygbiv  (2:31)  warp\n 2. t  (0:01)  u\n"


@pytest.mark.parametrize("command", [["search"], ["play"], ["playlist", "add", "chill"]])
def test_the_search_source_setting_is_the_default(command, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    settings.update("search_source", "soundcloud")
    playlists.create("chill")
    monkeypatch.setattr(player, "MpvClient", FakeClient)
    sources = recording_search(monkeypatch, [SC_VIDEO])
    result = runner.invoke(app, [*command, "boards"], input="1\n")
    assert result.exit_code == 0, result.output
    assert sources == ["soundcloud"]


@pytest.mark.parametrize("command", [["play"], ["playlist", "add", "chill"]])
def test_the_source_flag_beats_the_setting(command, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    settings.update("search_source", "soundcloud")
    playlists.create("chill")
    monkeypatch.setattr(player, "MpvClient", FakeClient)
    sources = recording_search(monkeypatch, ONE_VIDEO)
    result = runner.invoke(app, [*command, "boards", "--source", "youtube"], input="m\n1\n")
    assert result.exit_code == 0, result.output
    assert sources == ["youtube", "youtube"]  # m searches the same source again


def test_an_unknown_source_is_one_line_and_exit_1(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    sources = recording_search(monkeypatch, ONE_VIDEO)
    result = runner.invoke(app, ["search", "--source", "bandcamp", "boards"])
    assert result.exit_code == 1
    assert result.stderr == "Unknown source 'bandcamp'; valid sources: youtube, soundcloud\n"
    assert sources == []
