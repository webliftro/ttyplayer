import os
import shutil
import subprocess
import sys
import tomllib
from importlib import metadata
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ttyplayer import cli, control, favorites, history, player, utils, youtube
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
    monkeypatch.setattr(youtube, "search", lambda query, limit=5: [])
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

    def __init__(self, video=False, on_play=None):
        self.video = video
        self.on_play = on_play
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


def test_play_queues_the_picks_in_order_and_records_history(monkeypatch, tmp_path):
    videos = [Video(id=str(n), title=f"v{n}", uploader="u", duration=1) for n in range(3)]
    monkeypatch.setattr(youtube, "search", lambda query, limit=5: videos)
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
    monkeypatch.setattr(youtube, "search", lambda query, limit=5: ONE_VIDEO)
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
    monkeypatch.setattr(youtube, "search", lambda query, limit=5: [ALPHA, BETA])
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

    def __call__(self, query, limit=5):
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


@pytest.mark.parametrize("command", ["pause", "next", "prev", "stop", "status"])
def test_remote_commands_with_nothing_playing(command, monkeypatch):
    monkeypatch.setattr(control, "send", no_player)
    result = runner.invoke(app, [command])
    assert result.exit_code == 1
    assert result.stderr == "No ttyplayer is playing\n"
    assert result.stdout == ""


def test_remote_command_refused_prints_the_players_error(monkeypatch):
    monkeypatch.setattr(control, "send", fake_send({"ok": False, "error": "unknown command x"}))
    result = runner.invoke(app, ["pause"])
    assert result.exit_code == 1
    assert result.stderr == "unknown command x\n"


def test_tui_runs_the_app_with_the_real_player(monkeypatch):
    from ttyplayer import tui

    built = []
    monkeypatch.setattr(tui.TtyplayerApp, "run", lambda self: built.append(self))
    result = runner.invoke(app, ["tui", "--video"])
    assert result.exit_code == 0, result.output
    [screen] = built
    assert screen.client_factory is player.MpvClient
    assert screen.resolve is tui.resolve
    assert screen.video is True


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
    assert len(lines) == 6
    assert lines[2].startswith("✗ yt-dlp")


def test_lookup_prints_its_time_with_timing(monkeypatch):
    monkeypatch.setenv("TTYPLAYER_TIMING", "1")
    ticks = iter([5.0, 6.8])
    monkeypatch.setattr("ttyplayer.cli.time.monotonic", lambda: next(ticks))
    monkeypatch.setattr(youtube, "search", lambda query, limit=5: ONE_VIDEO)

    result = runner.invoke(app, ["search", "song"])

    assert result.exit_code == 0
    assert result.stderr == "lookup took 1.8s\n"
    assert result.output.index("lookup took") < result.output.index("1. t")


def test_lookup_prints_nothing_without_timing(monkeypatch):
    monkeypatch.delenv("TTYPLAYER_TIMING", raising=False)
    monkeypatch.setattr(youtube, "search", lambda query, limit=5: ONE_VIDEO)

    result = runner.invoke(app, ["search", "song"])

    assert result.exit_code == 0
    assert result.stderr == ""


ROOT = Path(__file__).resolve().parents[1]


def healthy(monkeypatch, tmp_path):
    """A machine where every doctor check passes, without touching the real one."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setattr(shutil, "which", lambda name: f"/opt/bin/{name}")
    monkeypatch.setattr(cli, "mpv_version", lambda path: "mpv v0.39.0 Copyright © 2000-2024 mpv/MPlayer/mplayer2 projects")
    monkeypatch.setattr(control, "private_dir", lambda directory: None)


def test_doctor_all_green(monkeypatch, tmp_path):
    healthy(monkeypatch, tmp_path)
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert len(lines) == 6
    assert all(line.startswith("✓ ") for line in lines)
    assert f"✓ ttyplayer {metadata.version('ttyplayer')}" in lines
    assert any(line.startswith("✓ yt-dlp 20") for line in lines)
    assert "✓ mpv v0.39.0 Copyright © 2000-2024 mpv/MPlayer/mplayer2 projects" in lines
    assert f"✓ data dir {tmp_path / 'ttyplayer'}" in lines


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
