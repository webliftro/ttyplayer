import asyncio
import dataclasses
import datetime
import functools
import importlib.resources
import pathlib
import random
import re
import sys
import threading
import time

import pytest
from textual.binding import Binding
from textual.widgets import (
    DataTable,
    Footer,
    Header,
    Input,
    LoadingIndicator,
    OptionList,
    ProgressBar,
    Static,
    TabbedContent,
)

from ttyplayer import art, control, favorites, history, lyrics, player, playlists, remote, settings, tui, youtube
from ttyplayer.models import Video

VIDEOS = [
    Video(id="a", title="Alpha", uploader="Ann", duration=61),
    Video(id="b", title="Beta", uploader="Bob", duration=None),
    Video(id="c", title="Gamma", uploader="Gus", duration=3600),
]


class FakeClient(player.MpvClient):
    """Stands in for MpvClient: the real queue edits and status(), never mpv; records the calls."""

    def __init__(self, video=False, on_play=None, on_state=None, levels=True, radio=False, normalize=False, prefetch=True):
        self.video = video
        self.prefetch_asked = prefetch  # the setting the app passed; this fake mirrors nothing into mpv
        self.show_levels = levels
        self.normalize = normalize
        self.radio = radio
        self.on_play = on_play
        self.on_state = on_state
        self.queue_lock = threading.RLock()
        self.state = {}
        self.queue = []
        self.index = 0
        self.calls = []

    def play_current(self):
        self.calls.append(("play_current", self.queue[self.index].id))
        super().play_current()

    def _play_index(self, index):
        # The real one minus mpv and on_play (history.record writes a file).
        if not 0 <= index < len(self.queue):
            return False
        self.index = index
        self.idle = False
        return True

    def send(self, command, on_reply=None):
        self.calls.append(("send", *command))

    def toggle_pause(self):
        self.calls.append(("toggle_pause",))

    def next(self):
        self.calls.append(("next",))

    def prev(self):
        self.calls.append(("prev",))

    def seek(self, seconds):
        self.calls.append(("seek", seconds))

    def change_volume(self, step):
        self.calls.append(("change_volume", step))

    def toggle_mute(self):
        self.calls.append(("toggle_mute",))

    def handle_control(self, name):
        return control.ok()

    def quit(self):
        self.calls.append(("quit",))


class FakeRemote:
    def __init__(self, log):
        self.log = log

    def stop(self):
        self.log.append("remote.stop")


@pytest.fixture(autouse=True)
def library(monkeypatch, tmp_path):
    """history and favorites in tmp_path files, as tests/test_cli.py does; never the user's own."""
    paths = {"history": tmp_path / "history.jsonl", "favorites": tmp_path / "favorites.jsonl"}
    monkeypatch.setattr(history, "history_path", lambda: paths["history"])
    monkeypatch.setattr(favorites, "favorites_path", lambda: paths["favorites"])
    return paths


@pytest.fixture(autouse=True)
def playlists_dir(monkeypatch, tmp_path):
    """Playlists in tmp_path too: tab 5, P and A write them."""
    path = tmp_path / "playlists"
    monkeypatch.setattr(playlists, "playlists_dir", lambda: path)
    return path


@pytest.fixture(autouse=True)
def settings_file(monkeypatch, tmp_path):
    """The settings file in tmp_path too: t and the Settings screen write it."""
    path = tmp_path / "settings.toml"
    monkeypatch.setattr(settings, "settings_path", lambda: path)
    return path


@pytest.fixture(autouse=True)
def no_art(monkeypatch):
    """No art extra, whatever this venv has: the panel's widths stay the same; the art tests opt in."""
    monkeypatch.setattr(art, "available", lambda: False)


class FakeFind:
    """Stands in for lyrics.find: answers each video id from answers (None when absent), after its gate
    opens if it has one; records the ids asked for."""

    def __init__(self):
        self.answers = {}
        self.gates = {}
        self.calls = []

    def __call__(self, video_id, title, uploader, duration=None):
        self.calls.append(video_id)
        if video_id in self.gates:
            self.gates[video_id].wait(5)
        return self.answers.get(video_id)


@pytest.fixture(autouse=True)
def find_lyrics(monkeypatch):
    """No test reaches LRCLIB: lyrics.find is a fake that finds nothing unless told otherwise."""
    find = FakeFind()
    monkeypatch.setattr(lyrics, "find", find)
    return find


@pytest.fixture
def served(monkeypatch):
    """control.serve replaced by a fake; the handlers it was given are collected here."""
    handlers = []
    monkeypatch.setattr(control, "serve", lambda handler: handlers.append(handler) or FakeRemote([]))
    return handlers


@pytest.fixture
def clients():
    made = []

    def factory(video=False, on_play=None, on_state=None, levels=True, radio=False, normalize=False, prefetch=True):
        made.append(FakeClient(video, on_play, on_state, levels, radio, normalize, prefetch))
        return made[-1]

    factory.made = made
    return factory


# Every playback key on a table and the player call it makes.
PLAYBACK_KEYS = [
    ("space", ("toggle_pause",)),
    ("n", ("next",)),
    ("p", ("prev",)),
    ("comma", ("seek", -player.SEEK_SECONDS)),
    ("full_stop", ("seek", player.SEEK_SECONDS)),
    ("less_than_sign", ("seek", -tui.LONG_SEEK_SECONDS)),
    ("greater_than_sign", ("seek", tui.LONG_SEEK_SECONDS)),
    ("minus", ("change_volume", -player.VOLUME_STEP)),
    ("plus", ("change_volume", player.VOLUME_STEP)),
    ("M", ("toggle_mute",)),
]


def drive(test):
    """Run an async test body with asyncio.run, so no async pytest plugin is needed."""

    @functools.wraps(test)
    def wrapper(*args, **kwargs):
        asyncio.run(test(*args, **kwargs))

    return wrapper


SEARCH_LIMIT = settings.DEFAULTS.search_limit


def make_app(clients, resolve=lambda text, limit, source: VIDEOS, video=False):
    return tui.TtyplayerApp(client_factory=clients, resolve=resolve, video=video)


def run(app):
    """run_test with toasts on (Textual turns them off by default in tests)."""
    return app.run_test(size=(100, 40), notifications=True)


def text(app, selector):
    return str(app.query_one(selector, Static).render())


def table(app):
    return app.query_one(tui.ResultsTable)


def rows(app, kind=tui.ResultsTable):
    results = app.query_one(kind)
    return [[str(cell) for cell in results.get_row_at(i)] for i in range(results.row_count)]


def toasts(app):
    """(message, severity) of every toast on screen."""
    return [
        (str(toast.render()), severity)
        for toast in app.query("Toast")
        for severity in ("information", "warning", "error")
        if toast.has_class(f"-{severity}")
    ]


async def search(pilot, text="lofi"):
    search_box = pilot.app.query_one(Input)
    search_box.focus()
    search_box.value = text
    await pilot.press("enter")
    await pilot.app.workers.wait_for_complete()
    await pilot.pause()


def status(**fields):
    base = {"title": "Song", "uploader": "Singer", "position": 3, "duration": 201, "paused": False,
            "index": 1, "total": 1, "volume": 60, "volume_source": "player", "muted": False, "up_next": None, "started_in": None, "idle": False,
            "error": None}
    return base | fields


# --- AC1: layout --------------------------------------------------------


@drive
async def test_layout_header_search_tabs_panel_footer(clients, served):
    app = make_app(clients)
    async with run(app) as pilot:
        await pilot.pause()
        assert app.query_one(Header).query("HeaderClock")
        search_box = app.query_one(Input)
        assert search_box.placeholder == "Search (sc: SoundCloud, yt: YouTube) or paste a link…"
        assert app.focused is search_box
        assert search_box.parent is app.query_one(LoadingIndicator).parent
        tabs = app.query_one(TabbedContent)
        assert [str(tabs.get_tab(pane).label) for pane in tabs.query("TabPane")] == [
            "Search", "Queue", "History", "Favorites", "Playlists", "Lyrics"
        ]
        assert tabs.active == "search"
        assert tabs.get_pane("queue").query_one(tui.QueueTable)
        assert tabs.get_pane("history").query_one(tui.HistoryTable)
        assert tabs.get_pane("favorites").query_one(tui.FavoritesTable)
        panel = app.query_one(tui.NowPlaying)
        assert panel.border_title == "Now playing"
        widgets = [app.query_one(Header), search_box, tabs, panel, app.query_one(Footer)]
        assert [w.region.y for w in widgets] == sorted(w.region.y for w in widgets)
        assert panel.region.bottom == app.query_one(Footer).region.y


def test_stylesheet_ships_in_the_package_and_uses_theme_colors_only():
    stylesheet = importlib.resources.files("ttyplayer").joinpath(tui.TtyplayerApp.CSS_PATH)
    assert stylesheet.is_file()
    colors = re.findall(r"(?:color|background|border[\w-]*)\s*:\s*([^;]+);", stylesheet.read_text(encoding="utf-8"))
    assert colors
    assert all("$" in value for value in colors)


# --- AC2: the results table ----------------------------------------------


def test_resolve_fetches_links_and_searches_words(monkeypatch):
    monkeypatch.setattr(youtube, "fetch", lambda url: [("fetched", url)])
    monkeypatch.setattr(youtube, "search", lambda query, limit, source: [("searched", query, limit, source)])
    assert tui.resolve("https://youtu.be/x", 10, "soundcloud") == [("fetched", "https://youtu.be/x")]
    assert tui.resolve("lofi beats", 10, "soundcloud") == [("searched", "lofi beats", 10, "soundcloud")]


@drive
async def test_search_fills_the_table_and_focuses_it(clients, served):
    asked = []
    app = make_app(clients, resolve=lambda text, limit, source: asked.append(text) or VIDEOS)
    async with run(app) as pilot:
        await search(pilot, "lofi")
        results = table(app)
        assert asked == ["lofi"]
        assert [str(column.label) for column in results.columns.values()] == ["#", "Title", "Uploader", "Length"]
        assert results.zebra_stripes is True
        assert results.cursor_type == "row"
        assert rows(app) == [
            [" 1", "Alpha", "Ann", "1:01"],
            [" 2", "Beta", "Bob", "--:--"],
            [" 3", "Gamma", "Gus", "60:00"],
        ]
        assert results.cursor_row == 0
        assert app.focused is results


@drive
async def test_a_new_search_refills_the_table_from_the_top(clients, served):
    answers = [VIDEOS, VIDEOS[2:]]
    app = make_app(clients, resolve=lambda text, limit, source: answers.pop(0))
    async with run(app) as pilot:
        await search(pilot, "first")
        await pilot.press("down", "down")
        await search(pilot, "second")
        assert rows(app) == [[" 1", "Gamma", "Gus", "60:00"]]
        assert table(app).cursor_row == 0


@drive
async def test_titles_are_shown_as_typed_not_as_markup(clients, served):
    odd = Video(id="x", title="[bold]Live[/bold] [x]", uploader="[DJ]", duration=1)
    app = make_app(clients, resolve=lambda text, limit, source: [odd])
    async with run(app) as pilot:
        await search(pilot)
        assert rows(app) == [[" 1", "[bold]Live[/bold] [x]", "[DJ]", "0:01"]]


@drive
async def test_enter_plays_that_row_and_queues_the_rows_after_it(clients, served):
    app = make_app(clients, video=True)
    async with run(app) as pilot:
        await search(pilot)
        await pilot.press("down", "enter")
        await pilot.pause()
        client = clients.made[0]
        assert client.video is True
        assert client.on_play == app.on_play
        assert client.on_state == app.on_player_state
        assert [v.id for v in client.queue] == ["b", "c"]
        assert client.calls == [("play_current", "b")]


@drive
async def test_enter_again_replaces_the_queue_with_one_player(clients, served):
    app = make_app(clients)
    async with run(app) as pilot:
        await search(pilot)
        await pilot.press("enter", "a", "down", "down", "enter")
        await pilot.pause()
        assert len(clients.made) == 1
        client = clients.made[0]
        assert [v.id for v in client.queue] == ["c"]
        assert client.index == 0
        assert client.calls[-1] == ("play_current", "c")


@drive
async def test_a_appends_and_starts_playing_an_empty_queue(clients, served):
    app = make_app(clients)
    async with run(app) as pilot:
        await search(pilot)
        await pilot.press("a", "down", "a")
        await pilot.pause()
        client = clients.made[0]
        assert [v.id for v in client.queue] == ["a", "b"]
        assert client.calls == [("play_current", "a")]


@drive
async def test_a_mirrors_the_added_track_into_mpv_as_the_next_one(clients, served):
    app = make_app(clients)
    async with run(app) as pilot:
        await search(pilot)
        await pilot.press("a")
        await pilot.pause()
        client = clients.made[0]
        client.prefetch = True  # as the real client, which this fake's prefetch_asked stands for
        await pilot.press("down", "a")
        await pilot.pause()
        assert client.calls[1:] == [("send", "loadfile", VIDEOS[1].url, "append"), ("send", "get_property", "playlist/1/id")]
        assert client.prefetched == VIDEOS[1].url


@drive
async def test_the_row_being_played_is_marked_on_every_state_change(clients, served):
    app = make_app(clients)
    async with run(app) as pilot:
        await search(pilot)
        await pilot.press("down", "enter")
        await pilot.pause()
        client = clients.made[0]
        client.on_state(status())
        assert [row[0] for row in rows(app)] == [" 1", "▸2", " 3"]
        client.index = 1
        client.on_state(status())
        assert [row[0] for row in rows(app)] == [" 1", " 2", "▸3"]


@drive
async def test_digits_switch_tabs_and_slash_and_escape_move_focus(clients, served):
    app = make_app(clients)
    async with run(app) as pilot:
        await search(pilot)
        tabs = app.query_one(TabbedContent)
        for key, pane in [("2", "queue"), ("3", "history"), ("4", "favorites"), ("6", "lyrics"), ("1", "search")]:
            await pilot.press(key)
            await pilot.pause()
            assert tabs.active == pane
        await pilot.press("slash")
        assert app.focused is app.query_one(Input)
        await pilot.press("escape")
        assert app.focused is table(app)


@drive
async def test_escape_on_the_library_tabs_focuses_their_table(clients, served):
    app = make_app(clients)
    async with run(app) as pilot:
        for pane, kind in (("history", tui.HistoryTable), ("favorites", tui.FavoritesTable)):
            app.query_one(TabbedContent).active = pane
            await pilot.pause()
            app.query_one(Input).focus()
            await pilot.press("escape")
            assert app.focused is app.query_one(kind)


# --- AC3: the spinner and search toasts -----------------------------------


@drive
async def test_spinner_shows_only_while_the_worker_runs(clients, served):
    release = threading.Event()

    def slow(text, limit, source):
        release.wait(5)
        return VIDEOS

    app = make_app(clients, resolve=slow)
    async with run(app) as pilot:
        spinner = app.query_one(LoadingIndicator)
        assert spinner.display is False
        app.query_one(Input).value = "lofi"
        await pilot.press("enter")
        await pilot.pause()
        assert spinner.display is True
        release.set()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert spinner.display is False
        assert table(app).row_count == 3


@drive
async def test_zero_results_is_a_warning_toast_and_keeps_the_table(clients, served):
    answers = [VIDEOS, []]
    app = make_app(clients, resolve=lambda text, limit, source: answers.pop(0))
    async with run(app) as pilot:
        await search(pilot, "first")
        await search(pilot, "nothing")
        assert toasts(app) == [("No videos found", "warning")]
        assert table(app).row_count == 3
        assert app.query_one(LoadingIndicator).display is False


@drive
async def test_youtube_error_is_an_error_toast_and_keeps_the_table(clients, served):
    def failing(text, limit, source):
        if text == "bad":
            raise youtube.YouTubeError("[youtube] no internet")
        return VIDEOS

    app = make_app(clients, resolve=failing)
    async with run(app) as pilot:
        await search(pilot, "good")
        await search(pilot, "bad")
        assert toasts(app) == [("YouTube lookup failed: [youtube] no internet", "error")]
        assert table(app).row_count == 3
        assert app.query_one(LoadingIndicator).display is False


# --- AC4: the now-playing panel -------------------------------------------


def panel(app):
    """The panel's lines as the eye reads them, plus its progress bar."""
    now = app.query_one(tui.NowPlaying)
    if now.has_class("-idle"):
        return [text(app, "#np-idle")], None
    lines = [
        [text(app, f"#np-{part}") for part in ("state", "title", "uploader", "position")],
        [text(app, "#np-time")],
        [text(app, f"#np-{part}") for part in ("volume", "next", "timing")],
    ]
    return lines, app.query_one(ProgressBar)


@drive
async def test_panel_idle_before_anything_plays(clients, served):
    app = make_app(clients)
    async with run(app) as pilot:
        await pilot.pause()
        assert panel(app) == (["Nothing playing — press / to search"], None)
        assert app.query_one("#np-idle").display is True
        assert all(line.display is False for line in app.query(".np-line"))


@drive
async def test_panel_while_playing_a_queue(clients, served):
    app = make_app(clients)
    async with run(app) as pilot:
        app.on_player_state(status(index=2, total=5, position=83, duration=213, up_next="Next one"))
        await pilot.pause()
        lines, progress = panel(app)
        assert lines == [
            ["▶", "Song", " · Singer", "[2/5]"],
            ["1:23 / 3:33"],
            ["🔊 ▮▮▮▮▮▮▯▯▯▯ 60%", "Up next: Next one", ""],
        ]
        assert (progress.total, progress.progress) == (213, 83)
        assert progress.show_percentage is False and progress.show_eta is False
        assert app.query_one("#np-idle").display is False


@drive
async def test_a_track_the_player_could_not_play_is_toasted_once(clients, served):
    app = make_app(clients)
    failed = "Could not play Song [live]: loading failed"  # brackets: not markup
    async with run(app) as pilot:
        app.on_player_state(status())
        app.on_player_state(status(error=failed))
        app.on_player_state(status(error=failed, position=4))  # unchanged: no second toast
        await pilot.pause()
        assert toasts(app) == [(failed, "error")]
        app.on_player_state(status())  # the next track started
        app.on_player_state(status(error=failed))  # failed again: a new occurrence
        await pilot.pause()
        assert toasts(app) == [(failed, "error"), (failed, "error")]


@drive
async def test_panel_paused_alone_at_the_end_of_a_queue(clients, served):
    app = make_app(clients)
    async with run(app) as pilot:
        app.on_player_state(status(paused=True))
        await pilot.pause()
        assert panel(app)[0][0] == ["⏸", "Song", " · Singer", ""]
        assert panel(app)[0][2][1] == ""
        app.on_player_state(status(index=2, total=2))
        await pilot.pause()
        assert panel(app)[0][2][1] == "End of queue"


@drive
async def test_panel_with_unknown_duration_volume_and_uploader(clients, served):
    app = make_app(clients)
    async with run(app) as pilot:
        app.on_player_state(status(duration=None, position=None, volume=None, uploader=None))
        await pilot.pause()
        lines, progress = panel(app)
        assert lines[0][2] == ""
        assert lines[1] == ["--:-- / --:--"]
        assert lines[2][0] == "🔊 ▯▯▯▯▯▯▯▯▯▯ --"
        assert progress.total is None


@pytest.mark.parametrize("timing, shown", [(True, "started in 2.4s"), (False, "")])
@drive
async def test_panel_shows_started_in_only_with_timing(timing, shown, clients, served, monkeypatch):
    monkeypatch.setattr(player, "timing", lambda: timing)
    app = make_app(clients)
    async with run(app) as pilot:
        app.on_player_state(status(started_in=2.4))
        await pilot.pause()
        assert panel(app)[0][2][2] == shown
        app.on_player_state(status(started_in=None))
        await pilot.pause()
        assert panel(app)[0][2][2] == ""


@drive
async def test_on_state_from_the_listener_thread_updates_the_panel(clients, served):
    app = make_app(clients)
    async with run(app) as pilot:
        await search(pilot)
        await pilot.press("enter")
        await pilot.pause()
        listener = threading.Thread(target=clients.made[0].on_state, args=(status(total=2, up_next="B"),))
        listener.start()
        while listener.is_alive():
            await pilot.pause()
        await pilot.pause()
        assert panel(app)[0][2][1] == "Up next: B"


@drive
async def test_on_state_from_the_app_thread_updates_the_panel(clients, served):
    app = make_app(clients)
    async with run(app) as pilot:
        await search(pilot)
        await pilot.press("enter")
        await pilot.pause()
        clients.made[0].on_state(status(paused=True))
        assert panel(app)[0][0][0] == "⏸"


# --- AC5: messages are toasts ----------------------------------------------


@pytest.mark.parametrize(
    "error, message",
    [
        (FileNotFoundError("mpv"), "mpv is not installed. Install it with: brew install mpv"),
        (RuntimeError("mpv did not start"), "mpv did not start"),
    ],
)
@drive
async def test_player_that_cannot_start_is_an_error_toast(error, message, served):
    def broken(video=False, **kwargs):
        raise error

    app = make_app(broken)
    async with run(app) as pilot:
        await search(pilot)
        await pilot.press("enter")
        await pilot.pause()
        assert toasts(app) == [(message, "error")]
        assert panel(app)[0] == ["Nothing playing — press / to search"]
        assert app.is_running
        assert served == []


@pytest.fixture
def refused(monkeypatch):
    """control.serve that cannot set up its socket: one stderr warning and None."""

    def refuse(handler):
        print("Remote control is off: busy", file=sys.stderr)
        return None

    monkeypatch.setattr(control, "serve", refuse)


@drive
async def test_remote_control_that_cannot_start_is_a_warning_toast(clients, refused, capsys):
    app = make_app(clients)
    async with run(app) as pilot:
        await search(pilot)
        await pilot.press("enter")
        await pilot.pause()
        client = clients.made[0]
        assert client.calls == [("play_current", "a")]
        assert toasts(app) == [("Remote control is off: busy", "warning")]
        client.on_state(status(paused=True))  # what play_current() does right away
        assert panel(app)[0][0][:2] == ["⏸", "Song"]
    assert capsys.readouterr().err == ""


# --- AC6: help and footer ---------------------------------------------------


@drive
async def test_help_lists_every_binding_and_closes(clients, served):
    app = make_app(clients)
    async with run(app) as pilot:
        await search(pilot)
        for close in ("escape", "question_mark"):
            await pilot.press("question_mark")
            await pilot.pause()
            assert isinstance(app.screen, tui.HelpScreen)
            lines = [str(s.render()) for s in app.screen.query(Static)]
            tables = [
                *tui.VideoTable.BINDINGS, *tui.step_bindings(settings.DEFAULTS), *tui.PickTable.BINDINGS, *tui.ResultsTable.BINDINGS,
                *tui.QueueTable.BINDINGS, *tui.FavoritesTable.BINDINGS, *tui.PlaylistTable.BINDINGS,
            ]
            for binding in [*tui.TtyplayerApp.BINDINGS, *tui.SearchBox.BINDINGS, *tables]:
                assert f"{app.get_key_display(binding):>8}  {binding.description}" in lines
            await pilot.press(close)
            await pilot.pause()
            assert not isinstance(app.screen, tui.HelpScreen)


@drive
async def test_footer_shows_the_main_keys_from_a_table(clients, served):
    app = make_app(clients)
    async with run(app) as pilot:
        await search(pilot)
        await pilot.pause()
        shown = {(key.key, key.description) for key in app.query("FooterKey") if key.description}
        assert shown >= {
            ("space", "Pause"), ("n", "Next"), ("p", "Prev"), ("a", "Add"),
            ("slash", "Search"), ("question_mark", "Help"), ("q", "Quit"),
        }
        hidden = {
            binding.key
            for binding in [
                *tui.VideoTable.BINDINGS, *tui.step_bindings(settings.DEFAULTS), *tui.ResultsTable.BINDINGS,
                *tui.TtyplayerApp.BINDINGS,
            ]
            if not binding.show
        }
        assert not hidden & {key.key for key in app.query("FooterKey")}


# --- AC7: unchanged behavior ------------------------------------------------


@pytest.mark.parametrize(
    "key, call",
    PLAYBACK_KEYS,
)
@drive
async def test_playback_keys_call_the_player(key, call, clients, served):
    app = make_app(clients)
    async with run(app) as pilot:
        await search(pilot)
        await pilot.press("enter", key)
        await pilot.pause()
        assert clients.made[0].calls[-1] == call


@drive
async def test_playback_keys_before_a_player_do_nothing(clients, served):
    app = make_app(clients)
    async with run(app) as pilot:
        await search(pilot)
        await pilot.press(*[key for key, _ in PLAYBACK_KEYS])
        await pilot.pause()
        assert clients.made == []
        assert app.is_running


@drive
async def test_playback_and_tab_keys_type_into_the_search_box(clients, served):
    app = make_app(clients)
    async with run(app) as pilot:
        await search(pilot)
        await pilot.press("enter", "slash")
        await pilot.press("n", "p", "space", "2", "slash", "question_mark")
        await pilot.pause()
        assert app.query_one(Input).value == "np 2/?"  # focusing selects the old text
        assert app.query_one(TabbedContent).active == "search"
        assert clients.made[0].calls == [("play_current", "a")]
        assert app.is_running


@drive
async def test_player_creation_starts_remote_control(clients, served):
    app = make_app(clients)
    async with run(app) as pilot:
        await search(pilot)
        assert served == []
        await pilot.press("enter", "down", "enter")
        await pilot.pause()
        assert served == [clients.made[0].handle_control]


@pytest.mark.parametrize(
    "key, focus", [("q", "table"), ("ctrl+c", "table"), ("ctrl+c", "search")]
)
@drive
async def test_quit_stops_remote_control_then_the_player(key, focus, clients, monkeypatch):
    log = []
    monkeypatch.setattr(control, "serve", lambda handler: FakeRemote(log))
    app = make_app(clients)
    async with run(app) as pilot:
        await search(pilot)
        await pilot.press("enter")
        await pilot.pause()
        clients.made[0].quit = lambda: log.append("client.quit")
        if focus == "search":
            await pilot.press("slash")
            assert app.focused is app.query_one(Input)
        await pilot.press(key)
        await pilot.pause()
    assert log == ["remote.stop", "client.quit"]
    assert not app.is_running


@drive
async def test_q_in_the_search_box_is_a_letter(clients, monkeypatch):
    log = []
    monkeypatch.setattr(control, "serve", lambda handler: FakeRemote(log))
    app = make_app(clients)
    async with run(app) as pilot:
        await search(pilot)
        await pilot.press("enter")
        await pilot.pause()
        clients.made[0].quit = lambda: log.append("client.quit")
        await pilot.press("slash")
        await pilot.press("q")
        await pilot.pause()
        assert app.is_running
        assert app.query_one(Input).value.endswith("q")
        await pilot.press("escape", "q")
        await pilot.pause()
    assert log == ["remote.stop", "client.quit"]
    assert not app.is_running


@pytest.mark.parametrize("keys", [("escape", "q"), ("ctrl+c",)])
@drive
async def test_quit_before_a_player_exists(keys, clients, served):
    app = make_app(clients)
    async with run(app) as pilot:
        assert app.focused is app.query_one(Input)
        await pilot.press(*keys)
        await pilot.pause()
    assert not app.is_running
    assert clients.made == []
    assert served == []


def test_stop_from_another_terminal_exits_through_cleanup(clients, monkeypatch):
    log = []
    monkeypatch.setattr(control, "serve", lambda handler: FakeRemote(log))
    app = make_app(clients)

    def stop_from_elsewhere():
        time.sleep(0.5)
        app.call_from_thread(app.show_results, VIDEOS)
        app.call_from_thread(app.action_append)  # once the results table has the focus
        player.interrupt_main()  # what MpvClient.handle_control("stop") does

    threading.Thread(target=stop_from_elsewhere, daemon=True).start()
    app.run(headless=True)  # returns: no KeyboardInterrupt escapes

    assert log == ["remote.stop"]
    assert clients.made[0].calls == [("play_current", "a"), ("quit",)]


# --- the Queue tab (tui-queue) ------------------------------------------------


def queue_rows(app):
    return rows(app, tui.QueueTable)


def queue_title(app):
    return app.query_one(TabbedContent).get_tab("queue").label.plain


async def queued(pilot, *keys):
    """Search, run keys on the results (e.g. Enter on a row), then go to the Queue tab."""
    await search(pilot)
    await pilot.press(*keys)
    await pilot.pause()
    await pilot.press("2")
    await pilot.pause()
    assert pilot.app.focused is pilot.app.query_one(tui.QueueTable)
    return pilot.app.client


@drive
async def test_queue_tab_is_empty_before_a_player(clients, served):
    app = make_app(clients)
    async with run(app) as pilot:
        await search(pilot)
        await pilot.press("2")
        await pilot.pause()
        queue = app.query_one(tui.QueueTable)
        assert [str(column.label) for column in queue.columns.values()] == ["#", "Title", "Uploader", "Length"]
        assert queue.zebra_stripes is True and queue.cursor_type == "row"
        assert queue_rows(app) == []
        assert queue_title(app) == "Queue"
        await pilot.press("enter", "d", "K", "J", "c")
        await pilot.pause()
        assert clients.made == [] and toasts(app) == []


@drive
async def test_queue_tab_lists_the_queue_and_marks_the_current_track(clients, served):
    app = make_app(clients)
    async with run(app) as pilot:
        await queued(pilot, "down", "enter")
        assert queue_rows(app) == [["▸1", "Beta", "Bob", "--:--"], [" 2", "Gamma", "Gus", "60:00"]]
        assert queue_title(app) == "Queue (2)"


@drive
async def test_queue_follows_every_state_change(clients, served):
    app = make_app(clients)
    async with run(app) as pilot:
        client = await queued(pilot, "enter")
        client.index = 2  # what the listener's eof -> next() leaves behind
        client.on_state(status())
        assert [row[0] for row in queue_rows(app)] == [" 1", " 2", "▸3"]


@drive
async def test_a_and_enter_on_search_refresh_the_queue_tab(clients, served):
    app = make_app(clients)
    async with run(app) as pilot:
        await search(pilot)
        await pilot.press("down", "down", "enter")
        await pilot.pause()
        assert [row[1] for row in queue_rows(app)] == ["Gamma"]
        assert queue_title(app) == "Queue (1)"
        await pilot.press("up", "up", "a")
        await pilot.pause()
        assert [row[:2] for row in queue_rows(app)] == [["▸1", "Gamma"], [" 2", "Alpha"]]
        assert queue_title(app) == "Queue (2)"
        assert panel(app)[0][0][3] == "[1/2]" and panel(app)[0][2][1] == "Up next: Alpha"
        await pilot.press("enter")
        await pilot.pause()
        assert [row[:2] for row in queue_rows(app)] == [["▸1", "Alpha"], [" 2", "Beta"], [" 3", "Gamma"]]
        assert queue_title(app) == "Queue (3)"


@drive
async def test_enter_on_the_queue_jumps(clients, served):
    app = make_app(clients)
    async with run(app) as pilot:
        client = await queued(pilot, "enter")
        await pilot.press("down", "enter")
        await pilot.pause()
        assert client.index == 1
        assert [row[0] for row in queue_rows(app)] == [" 1", "▸2", " 3"]
        assert app.query_one(tui.QueueTable).cursor_row == 1
        assert panel(app)[0][0][1:] == ["Beta", " · Bob", "[2/3]"]
        assert panel(app)[0][2][1] == "Up next: Gamma"


@drive
async def test_d_removes_and_the_cursor_stays_in_place(clients, served):
    app = make_app(clients)
    async with run(app) as pilot:
        client = await queued(pilot, "enter")
        queue = app.query_one(tui.QueueTable)
        await pilot.press("down", "d")
        await pilot.pause()
        assert [v.id for v in client.queue] == ["a", "c"]
        assert queue.cursor_row == 1
        assert queue_title(app) == "Queue (2)"
        assert panel(app)[0][0][3] == "[1/2]" and panel(app)[0][2][1] == "Up next: Gamma"
        await pilot.press("d")
        await pilot.pause()
        assert queue_rows(app) == [["▸1", "Alpha", "Ann", "1:01"]]
        assert queue.cursor_row == 0  # clamped to the last row
        assert panel(app)[0][0][3] == "" and panel(app)[0][2][1] == ""


@drive
async def test_d_on_the_current_track_plays_the_next_one(clients, served):
    app = make_app(clients)
    async with run(app) as pilot:
        client = await queued(pilot, "enter")
        await pilot.press("d")
        await pilot.pause()
        assert queue_rows(app)[0][:2] == ["▸1", "Beta"]
        assert panel(app)[0][0][1:] == ["Beta", " · Bob", "[1/2]"]


@drive
async def test_d_on_the_only_track_empties_the_queue(clients, served):
    app = make_app(clients)
    async with run(app) as pilot:
        client = await queued(pilot, "down", "down", "enter")
        await pilot.press("d")
        await pilot.pause()
        assert client.queue == []
        assert client.calls[-1] == ("send", "stop")
        assert queue_rows(app) == []
        assert queue_title(app) == "Queue"


@drive
async def test_the_end_of_the_queue_unmarks_the_last_track_and_idles_the_panel(clients, served):
    app = make_app(clients)
    async with run(app) as pilot:
        client = await queued(pilot, "down", "enter")
        client.state["media-title"] = "Gamma"
        client.handle_message({"event": "end-file", "reason": "eof"})
        client.handle_message({"event": "end-file", "reason": "eof"})
        await pilot.pause()
        assert [row[0] for row in queue_rows(app)] == [" 1", " 2"]
        assert panel(app) == (["Nothing playing — press / to search"], None)
        assert [row[0] for row in rows(app)] == [" 1", " 2", " 3"]
        await pilot.press("down", "d")  # the finished Gamma: nothing after it, so Beta plays
        await pilot.pause()
        assert queue_rows(app) == [["▸1", "Beta", "Bob", "--:--"]]
        assert panel(app)[0][0][1] == "Beta"


@pytest.mark.parametrize("keys", [["d"], ["c"]])
@drive
async def test_emptying_the_queue_idles_the_panel_despite_mpvs_last_title(keys, clients, served):
    app = make_app(clients)
    async with run(app) as pilot:
        client = await queued(pilot, "down", "down", "enter")
        client.state["media-title"] = "Gamma"
        if keys == ["c"]:
            client.handle_message({"event": "end-file", "reason": "eof"})
        await pilot.press(*keys)
        await pilot.pause()
        assert client.queue == []
        assert client.status()["title"] is None
        assert panel(app) == (["Nothing playing — press / to search"], None)
        assert queue_rows(app) == [] and queue_title(app) == "Queue"


@drive
async def test_shift_j_and_k_move_the_row_and_the_cursor_follows(clients, served):
    app = make_app(clients)
    async with run(app) as pilot:
        client = await queued(pilot, "enter")
        queue = app.query_one(tui.QueueTable)
        await pilot.press("J")
        await pilot.pause()
        assert [row[:2] for row in queue_rows(app)] == [[" 1", "Beta"], ["▸2", "Alpha"], [" 3", "Gamma"]]
        assert queue.cursor_row == 1
        assert panel(app)[0][0][3] == "[2/3]" and panel(app)[0][2][1] == "Up next: Gamma"
        await pilot.press("shift+down")
        await pilot.pause()
        assert [v.id for v in client.queue] == ["b", "c", "a"]
        assert queue.cursor_row == 2
        assert panel(app)[0][2][1] == "End of queue"
        await pilot.press("J")  # already last: nothing moves
        await pilot.pause()
        assert [v.id for v in client.queue] == ["b", "c", "a"] and queue.cursor_row == 2
        await pilot.press("K", "shift+up")
        await pilot.pause()
        assert [v.id for v in client.queue] == ["a", "b", "c"]
        assert queue.cursor_row == 0
        assert client.index == 0
        await pilot.press("K")  # already first
        await pilot.pause()
        assert [v.id for v in client.queue] == ["a", "b", "c"] and queue.cursor_row == 0


@drive
async def test_c_keeps_only_the_current_track_with_a_toast(clients, served):
    app = make_app(clients)
    async with run(app) as pilot:
        client = await queued(pilot, "enter")
        await pilot.press("down", "enter", "c")
        await pilot.pause()
        assert [v.id for v in client.queue] == ["b"]
        assert queue_rows(app) == [["▸1", "Beta", "Bob", "--:--"]]
        assert queue_title(app) == "Queue (1)"
        assert toasts(app) == [("Queue cleared", "information")]
        assert panel(app)[0][0][3] == "" and panel(app)[0][2][1] == ""


@pytest.mark.parametrize(
    "key, call",
    PLAYBACK_KEYS,
)
@drive
async def test_playback_keys_work_on_the_queue_tab(key, call, clients, served):
    app = make_app(clients)
    async with run(app) as pilot:
        client = await queued(pilot, "enter")
        await pilot.press(key)
        await pilot.pause()
        assert client.calls[-1] == call


@drive
async def test_footer_on_the_queue_tab_shows_its_keys(clients, served):
    app = make_app(clients)
    async with run(app) as pilot:
        await queued(pilot, "enter")
        shown = {(key.key, key.description) for key in app.query("FooterKey") if key.description}
        assert shown >= {("d", "Remove"), ("K", "Move up"), ("J", "Move down"), ("c", "Clear"), ("space", "Pause")}
        assert ("a", "Add") not in shown


@drive
async def test_a_status_tick_keeps_the_queue_table_as_it_is(clients, served):
    app = make_app(clients)
    async with run(app) as pilot:
        client = await queued(pilot, "enter")
        queue = app.query_one(tui.QueueTable)
        cleared = []
        queue.clear = lambda *args: cleared.append(args)
        client.on_state(status(position=4))
        assert cleared == []
        client.jump(1)
        assert cleared == [()]


# --- History and Favorites tabs, f, d and m (tui-library) ----------------------

MORE = [Video(id="d", title="Delta", uploader="Dee", duration=5), Video(id="e", title="Epsilon", uploader="Eve", duration=7)]


def tab_title(app, pane):
    return app.query_one(TabbedContent).get_tab(pane).label.plain


async def on_tab(pilot, key):
    """Leave the search box (digits are letters there), then switch to tab key."""
    await pilot.press("escape", key)
    await pilot.pause()


@drive
async def test_history_tab_lists_what_was_played_newest_first(clients, served):
    for video in (VIDEOS[0], VIDEOS[1], VIDEOS[0]):
        history.record(video)
    favorites.add(VIDEOS[1])
    app = make_app(clients)
    async with run(app) as pilot:
        await pilot.pause()
        assert tab_title(app, "history") == "History (2)"
        assert [row[1] for row in rows(app, tui.HistoryTable)] == ["Alpha", "Beta ♥"]
        await on_tab(pilot, "3")
        assert app.focused is app.query_one(tui.HistoryTable)
        assert rows(app, tui.HistoryTable) == [[" 1", "Alpha", "Ann", "1:01"], [" 2", "Beta ♥", "Bob", "--:--"]]


@drive
async def test_history_tab_shows_at_most_50_and_no_count_when_empty(clients, served):
    app = make_app(clients)
    async with run(app) as pilot:
        await pilot.pause()
        assert tab_title(app, "history") == "History"
        assert rows(app, tui.HistoryTable) == []
        for number in range(60):
            history.record(Video(id=str(number), title=f"T{number}", uploader="u", duration=1))
        await on_tab(pilot, "3")
        assert tab_title(app, "history") == "History (50)"
        assert rows(app, tui.HistoryTable)[0][1] == "T59"
        assert app.query_one(tui.HistoryTable).row_count == 50


@drive
async def test_history_tab_refreshes_each_time_it_is_shown(clients, served):
    app = make_app(clients)
    async with run(app) as pilot:
        await on_tab(pilot, "3")
        history.record(VIDEOS[2])  # e.g. the CLI playing in another terminal
        await on_tab(pilot, "1")
        await on_tab(pilot, "3")
        assert [row[1] for row in rows(app, tui.HistoryTable)] == ["Gamma"]
        assert tab_title(app, "history") == "History (1)"


@drive
async def test_on_play_records_history_and_the_history_tab_follows(clients, served):
    app = make_app(clients)
    async with run(app) as pilot:
        await search(pilot)
        await pilot.press("enter")
        await pilot.pause()
        client = clients.made[0]
        client.on_play(VIDEOS[0])  # on the app's thread, as play_current() does
        client.notify()
        assert [row[1] for row in rows(app, tui.HistoryTable)] == ["Alpha"]

        def eof_step():  # the listener thread: on_play under queue_lock, on_state after it
            with client.queue_lock:
                client.on_play(VIDEOS[1])

        listener = threading.Thread(target=eof_step)
        listener.start()
        listener.join(2)  # the app loop is blocked here: on_play must not wait for it
        assert not listener.is_alive()
        assert [v.id for v in history.load()] == ["b", "a"]
        await asyncio.to_thread(client.notify)
        await pilot.pause()
        assert [row[1] for row in rows(app, tui.HistoryTable)] == ["Beta", "Alpha"]
        assert tab_title(app, "history") == "History (2)"


@drive
async def test_favorites_tab_lists_favorites_newest_first_and_refreshes_when_shown(clients, served):
    favorites.add(VIDEOS[0])
    app = make_app(clients)
    async with run(app) as pilot:
        await pilot.pause()
        assert tab_title(app, "favorites") == "Favorites (1)"
        favorites.add(VIDEOS[2])  # the CLI's ttyplayer favorite, meanwhile
        await on_tab(pilot, "4")
        assert app.focused is app.query_one(tui.FavoritesTable)
        assert rows(app, tui.FavoritesTable) == [[" 1", "Gamma ♥", "Gus", "60:00"], [" 2", "Alpha ♥", "Ann", "1:01"]]
        assert tab_title(app, "favorites") == "Favorites (2)"


@pytest.mark.parametrize("tab", ["3", "4"])
@drive
async def test_enter_and_a_on_library_rows_play_and_queue_like_search(tab, clients, served):
    for video in reversed(VIDEOS):  # listed newest first: Alpha, Beta, Gamma
        history.record(video)
        favorites.add(video)
    app = make_app(clients)
    async with run(app) as pilot:
        await on_tab(pilot, tab)
        await pilot.press("a")
        await pilot.pause()
        client = clients.made[0]
        assert [v.id for v in client.queue] == ["a"]
        assert client.calls == [("play_current", "a")]
        await pilot.press("down", "enter")
        await pilot.pause()
        assert len(clients.made) == 1
        assert [v.id for v in client.queue] == ["b", "c"]
        assert client.calls[-1] == ("play_current", "b")
        table = app.query_one(tui.HistoryTable if tab == "3" else tui.FavoritesTable)
        assert [str(table.get_row_at(i)[0]) for i in range(3)] == [" 1", "▸2", " 3"]


@drive
async def test_f_on_a_search_row_toggles_it_with_a_toast_and_a_heart_everywhere(clients, served):
    history.record(VIDEOS[1])
    app = make_app(clients)
    async with run(app) as pilot:
        await search(pilot)
        await pilot.press("down", "f")
        await pilot.pause()
        assert favorites.ids() == {"b"}
        assert toasts(app) == [("Favorited: Beta", "information")]
        assert [row[1] for row in rows(app)] == ["Alpha", "Beta ♥", "Gamma"]
        assert table(app).cursor_row == 1
        assert [row[1] for row in rows(app, tui.HistoryTable)] == ["Beta ♥"]
        assert [row[1] for row in rows(app, tui.FavoritesTable)] == ["Beta ♥"]
        assert tab_title(app, "favorites") == "Favorites (1)"
        await pilot.press("f")
        await pilot.pause()
        assert favorites.ids() == set()
        assert ("Removed from favorites: Beta", "information") in toasts(app)
        assert [row[1] for row in rows(app)] == ["Alpha", "Beta", "Gamma"]
        assert [row[1] for row in rows(app, tui.HistoryTable)] == ["Beta"]
        assert rows(app, tui.FavoritesTable) == [] and tab_title(app, "favorites") == "Favorites"


@drive
async def test_f_on_history_and_favorites_rows(clients, served):
    history.record(VIDEOS[2])
    favorites.add(VIDEOS[0])
    app = make_app(clients)
    async with run(app) as pilot:
        await on_tab(pilot, "3")
        await pilot.press("f")
        await pilot.pause()
        assert favorites.ids() == {"a", "c"}
        assert toasts(app) == [("Favorited: Gamma", "information")]
        await on_tab(pilot, "4")
        assert [row[1] for row in rows(app, tui.FavoritesTable)] == ["Gamma ♥", "Alpha ♥"]
        await pilot.press("down", "f")
        await pilot.pause()
        assert favorites.ids() == {"c"}
        assert ("Removed from favorites: Alpha", "information") in toasts(app)
        assert [row[1] for row in rows(app, tui.FavoritesTable)] == ["Gamma ♥"]


@drive
async def test_f_on_the_queue_tab_toggles_the_track_playing(clients, served):
    app = make_app(clients)
    async with run(app) as pilot:
        await queued(pilot, "down", "enter")
        await pilot.press("down", "f")  # the cursor row does not matter here
        await pilot.pause()
        assert favorites.ids() == {"b"}
        assert toasts(app) == [("Favorited: Beta", "information")]
        assert [row[1] for row in queue_rows(app)] == ["Beta ♥", "Gamma"]
        assert [row[1] for row in rows(app)] == ["Alpha", "Beta ♥", "Gamma"]
        await pilot.press("f")
        await pilot.pause()
        assert favorites.ids() == set()
        assert [row[1] for row in queue_rows(app)] == ["Beta", "Gamma"]


@drive
async def test_f_with_no_row_toggles_the_track_playing_or_does_nothing(clients, served):
    app = make_app(clients)
    async with run(app) as pilot:
        await on_tab(pilot, "4")
        await pilot.press("f")  # nothing playing, no row
        await pilot.pause()
        assert favorites.ids() == set() and toasts(app) == []
        assert clients.made == []
        await search(pilot)
        await pilot.press("enter")
        await pilot.pause()
        await on_tab(pilot, "3")  # empty: FakeClient keeps history.record out of it
        await pilot.press("f")
        await pilot.pause()
        assert favorites.ids() == {"a"}
        assert toasts(app) == [("Favorited: Alpha", "information")]


@drive
async def test_d_on_a_favorites_row_removes_it_with_a_toast(clients, served):
    for video in reversed(VIDEOS):
        favorites.add(video)
    app = make_app(clients)
    async with run(app) as pilot:
        await on_tab(pilot, "4")
        await pilot.press("down", "d")
        await pilot.pause()
        assert favorites.ids() == {"a", "c"}
        assert toasts(app) == [("Removed from favorites: Beta", "information")]
        assert [row[1] for row in rows(app, tui.FavoritesTable)] == ["Alpha ♥", "Gamma ♥"]
        assert app.query_one(tui.FavoritesTable).cursor_row == 1
        assert tab_title(app, "favorites") == "Favorites (2)"
        await pilot.press("d", "d", "d")  # down to empty, then nothing to remove
        await pilot.pause()
        assert favorites.ids() == set() and tab_title(app, "favorites") == "Favorites"


@drive
async def test_d_on_the_search_and_history_tabs_does_not_unfavorite(clients, served):
    favorites.add(VIDEOS[0])
    history.record(VIDEOS[0])
    app = make_app(clients)
    async with run(app) as pilot:
        await search(pilot)
        await pilot.press("d")
        await on_tab(pilot, "3")
        await pilot.press("d")
        await pilot.pause()
        assert favorites.ids() == {"a"}


def recording_resolver(*answers):
    """A fake resolve: hands out answers in order and records (text, limit) of every call."""
    answers = list(answers)

    def resolve(text, limit=SEARCH_LIMIT, source="youtube"):
        resolve.asked.append((text, limit))
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    resolve.asked = []
    return resolve


@drive
async def test_m_appends_the_unseen_results_of_a_bigger_search_numbered_on(clients, served):
    resolve = recording_resolver(VIDEOS, [*VIDEOS[:2], MORE[0], VIDEOS[2], MORE[1], MORE[0]])
    app = make_app(clients, resolve=resolve)
    async with run(app) as pilot:
        await search(pilot, "lofi")
        await pilot.press("down", "m")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert resolve.asked == [("lofi", SEARCH_LIMIT), ("lofi", 3 + SEARCH_LIMIT)]
        assert [row[:2] for row in rows(app)] == [
            [" 1", "Alpha"], [" 2", "Beta"], [" 3", "Gamma"], [" 4", "Delta"], [" 5", "Epsilon"]
        ]
        assert table(app).cursor_row == 1
        await pilot.press("down", "down", "enter")  # Enter on an appended row
        await pilot.pause()
        assert [v.id for v in clients.made[0].queue] == ["d", "e"]


@drive
async def test_m_shows_the_spinner_while_it_runs(clients, served):
    release = threading.Event()

    def slow(text, limit=SEARCH_LIMIT, source="youtube"):
        if limit != SEARCH_LIMIT:
            release.wait(5)
            return VIDEOS + MORE
        return VIDEOS

    app = make_app(clients, resolve=slow)
    async with run(app) as pilot:
        await search(pilot)
        spinner = app.query_one(LoadingIndicator)
        await pilot.press("m")
        await pilot.pause()
        assert spinner.display is True
        release.set()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert spinner.display is False
        assert table(app).row_count == 5


@drive
async def test_m_with_nothing_new_is_a_toast_and_keeps_the_rows(clients, served):
    resolve = recording_resolver(VIDEOS, VIDEOS, youtube.YouTubeError("[youtube] offline"))
    app = make_app(clients, resolve=resolve)
    async with run(app) as pilot:
        await search(pilot)
        await pilot.press("m")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert toasts(app) == [("No more results", "warning")]
        await pilot.press("m")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert ("YouTube lookup failed: [youtube] offline", "error") in toasts(app)
        assert table(app).row_count == 3
        assert app.query_one(LoadingIndicator).display is False


@drive
async def test_m_before_a_search_on_other_tabs_and_in_the_search_box_is_ignored(clients, served):
    history.record(VIDEOS[0])
    favorites.add(VIDEOS[0])
    resolve = recording_resolver(VIDEOS)
    app = make_app(clients, resolve=resolve)
    async with run(app) as pilot:
        await pilot.press("escape", "m")  # the empty results table, before any search
        await pilot.pause()
        assert resolve.asked == []
        await search(pilot)
        for tab in ("2", "3", "4"):
            await on_tab(pilot, tab)
            await pilot.press("m")
        await pilot.press("slash", "m")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert resolve.asked == [("lofi", SEARCH_LIMIT)]
        assert app.query_one(Input).value == "m"  # typed over the selected text
        assert table(app).row_count == 3


# --- tui-polish: themes, long seek, mute, command palette, docs --------------


@drive
async def test_t_cycles_every_theme_in_sorted_order_and_wraps(clients, served):
    app = make_app(clients)
    async with run(app) as pilot:
        await search(pilot)
        themes = sorted(app.available_themes)
        start = themes.index(app.theme)
        expected = themes[start + 1 :] + themes[: start + 1]
        seen = []
        for _ in themes:
            await pilot.press("t")
            await pilot.pause()
            seen.append(app.theme)
            assert toasts(app)[-1] == (f"Theme: {app.theme}", "information")
        assert seen == expected  # every built-in theme once, back to the first
        assert app.is_running  # the stylesheet resolved under every one of them


@drive
async def test_t_in_the_search_box_is_a_letter(clients, served):
    app = make_app(clients)
    async with run(app) as pilot:
        theme = app.theme
        await pilot.press("t")
        await pilot.pause()
        assert app.query_one(Input).value == "t"
        assert app.theme == theme


def test_stylesheet_has_no_hard_coded_colors():
    stylesheet = importlib.resources.files("ttyplayer").joinpath(tui.TtyplayerApp.CSS_PATH).read_text(encoding="utf-8")
    assert not re.search(r"#[0-9a-fA-F]{6}\b|rgb\(", stylesheet)


@drive
async def test_panel_shows_the_muted_speaker_while_muted(clients, served):
    app = make_app(clients)
    async with run(app) as pilot:
        app.on_player_state(status(muted=True))
        await pilot.pause()
        assert text(app, "#np-volume") == "🔇 ▮▮▮▮▮▮▯▯▯▯ 60%"
        app.on_player_state(status(muted=False))
        await pilot.pause()
        assert text(app, "#np-volume") == "🔊 ▮▮▮▮▮▮▯▯▯▯ 60%"


@drive
async def test_volume_keys_on_macos_set_the_system_volume_off_the_app_thread(monkeypatch, served):
    """The real change_volume(), osascript faked: the write leaves the Textual thread, the meter follows."""
    monkeypatch.setattr(player.sys, "platform", "darwin")
    writes = []  # (volume, thread ident) per osascript write

    def write(volume):
        writes.append((volume, threading.get_ident()))
        return True

    monkeypatch.setattr(player, "write_system_volume", write)

    class SystemVolumeClient(FakeClient):
        change_volume = player.MpvClient.change_volume

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.state["system-volume"] = 60
            self.start_volume_writer()

    made = []
    app = make_app(lambda *args, **kwargs: made.append(SystemVolumeClient(*args, **kwargs)) or made[-1])
    async with run(app) as pilot:
        await search(pilot)
        await pilot.press("enter", "plus", "plus")
        await asyncio.to_thread(made[0].volume_steps.join)
        await pilot.pause()
        assert [volume for volume, _ in writes] == [65, 70]
        assert app.app_thread not in {thread for _, thread in writes}
        assert text(app, "#np-volume") == "🔊 ▮▮▮▮▮▮▮▯▯▯ 70%"


@drive
async def test_panel_meter_follows_the_device_volume(clients, served):
    app = make_app(clients)
    async with run(app) as pilot:
        app.on_player_state(status(volume=30.0, volume_source="device"))
        await pilot.pause()
        assert text(app, "#np-volume") == "🔊 ▮▮▮▯▯▯▯▯▯▯ 30%"
        app.on_player_state(status(volume=75.0, volume_source="device"))
        await pilot.pause()
        assert text(app, "#np-volume") == "🔊 ▮▮▮▮▮▮▮▮▯▯ 75%"


def level_bars(app):
    return [text(app, "#np-level-left"), text(app, "#np-level-right")]


@drive
async def test_panel_shows_the_levels_as_two_bars_under_the_volume(clients, served):
    app = make_app(clients)
    async with run(app) as pilot:
        app.on_player_state(status(levels=[-30.0, 0.0]))
        await pilot.pause()
        left, right = level_bars(app)
        cells = app.query_one("#np-level-left").size.width - 2
        assert cells == 96 - 2  # the panel's inner width: 100 columns less border and padding
        assert left == "L " + "▮" * (cells // 2) + "▯" * (cells // 2)
        assert right == "R " + "▮" * cells
        assert app.query_one("#np-level-left").region.y == app.query_one("#np-volume").region.y + 1
        assert app.query_one("#np-level-right").region.y == app.query_one("#np-volume").region.y + 2


@drive
async def test_panel_level_bars_are_empty_not_hidden_without_levels(clients, served):
    app = make_app(clients)
    async with run(app) as pilot:
        app.on_player_state(status(levels=[-30.0, 0.0]))
        await pilot.pause()
        panel_height = app.query_one(tui.NowPlaying).region.height
        app.on_player_state(status(paused=True, levels=None))
        await pilot.pause()
        cells = app.query_one("#np-level-left").size.width - 2
        assert level_bars(app) == ["L " + "▯" * cells, "R " + "▯" * cells]
        assert app.query_one(tui.NowPlaying).region.height == panel_height == 7
        app.on_player_state(status())  # a status without the key at all, as an older server sends
        await pilot.pause()
        assert level_bars(app) == ["L " + "▯" * cells, "R " + "▯" * cells]


@drive
async def test_idle_the_level_bars_stay_shown_and_empty(clients, served):
    app = make_app(clients)
    async with run(app) as pilot:
        await pilot.pause()
        bars = [app.query_one("#np-level-left"), app.query_one("#np-level-right")]
        cells = bars[0].size.width - 2
        empty = ["L " + "▯" * cells, "R " + "▯" * cells]
        assert all(bar.display and bar.region.height == 1 for bar in bars)
        assert level_bars(app) == empty
        app.on_player_state(status(levels=[-30.0, 0.0]))
        await pilot.pause()
        app.on_player_state(status(idle=True, levels=[-30.0, 0.0]))
        await pilot.pause()
        assert all(bar.display and bar.region.height == 1 for bar in bars)
        assert level_bars(app) == empty
        assert app.query_one(tui.NowPlaying).region.height == 7


@drive
async def test_with_show_levels_false_the_bars_are_hidden_and_mpv_starts_without_them(clients, served):
    app = make_app_with(clients, settings.Settings(show_levels=False))
    async with run(app) as pilot:
        await search(pilot)
        await pilot.press("enter")
        await pilot.pause()
        assert clients.made[0].show_levels is False
        assert not app.query_one("#np-level-left").display and not app.query_one("#np-level-right").display
        assert app.query_one(tui.NowPlaying).region.height == 5


@drive
async def test_enter_on_show_levels_flips_it_the_bars_and_mpvs_filter_follow(clients, served):
    app = make_app_with(clients, settings.Settings())
    async with run(app) as pilot:
        await search(pilot)
        await pilot.press("enter")
        await pilot.pause()
        client = clients.made[0]
        assert client.show_levels is True
        await pilot.press("S")
        await pilot.pause()
        await pilot.press(*["down"] * settings.KEYS.index("show_levels"), "enter")
        await pilot.pause()
        main = app.screen_stack[0]
        assert settings.load().show_levels is False
        assert not main.query_one("#np-level-left").display
        assert client.calls[-1] == ("send", "af", "remove", "@levels")
        await pilot.press("enter")
        await pilot.pause()
        assert settings.load().show_levels is True
        assert main.query_one("#np-level-left").display
        assert client.calls[-1] == ("send", "af", "add", player.LEVELS_FILTER)


def help_lines(app):
    return [str(s.render()) for s in app.screen.query(Static)]


@drive
async def test_with_normalize_loudness_mpv_starts_with_the_filter_and_enter_toggles_it_live(clients, served):
    app = make_app_with(clients, settings.Settings(normalize_loudness=True))
    async with run(app) as pilot:
        await search(pilot)
        await pilot.press("enter")
        await pilot.pause()
        client = clients.made[0]
        assert client.normalize is True
        await pilot.press("S")
        await pilot.pause()
        await pilot.press(*["down"] * settings.KEYS.index("normalize_loudness"), "enter")
        await pilot.pause()
        assert settings.load().normalize_loudness is False
        assert client.calls[-1] == ("send", "af", "remove", "@norm")
        await pilot.press("enter")
        await pilot.pause()
        assert settings.load().normalize_loudness is True
        assert client.calls[-1] == ("send", "af", "pre", player.NORMALIZE_FILTER)


@drive
async def test_the_seek_and_volume_keys_and_help_follow_the_configured_steps(clients, served):
    app = make_app_with(clients, settings.Settings(seek_seconds=10, volume_step=2))
    async with run(app) as pilot:
        await search(pilot)
        await pilot.press("enter", "full_stop", "comma", "plus", "minus")
        await pilot.pause()
        assert clients.made[0].calls[-4:] == [("seek", 10), ("seek", -10), ("change_volume", 2), ("change_volume", -2)]
        await pilot.press("question_mark")
        await pilot.pause()
        lines = help_lines(app)
        for shown in ("Seek −10s", "Seek +10s", "Seek −30s", "Seek +30s", "Volume −2", "Volume +2"):
            assert any(line.endswith(f"  {shown}") for line in lines), shown
        assert not any("Seek +5s" in line or "Volume +5" in line for line in lines)


@drive
async def test_a_changed_step_setting_applies_on_the_next_keypress_in_every_table(clients, served):
    app = make_app(clients)
    async with run(app) as pilot:
        await search(pilot)
        await pilot.press("enter", "full_stop")
        await pilot.pause()
        client = clients.made[0]
        assert client.calls[-1] == ("seek", player.SEEK_SECONDS)
        app.change_setting("seek_seconds", 15)
        app.change_setting("volume_step", 7)
        await pilot.press("full_stop", "plus")
        await pilot.pause()
        assert client.calls[-2:] == [("seek", 15), ("change_volume", 7)]
        assert (settings.load().seek_seconds, settings.load().volume_step) == (15, 7)
        await pilot.press("2", "comma")  # the Queue table follows too
        await pilot.pause()
        assert client.calls[-1] == ("seek", -15)
        await pilot.press("question_mark")
        await pilot.pause()
        assert any(line.endswith("  Seek +15s") for line in help_lines(app))


@drive
async def test_palette_provider_offers_every_command_and_runs_its_action(clients, served, monkeypatch):
    app = make_app(clients)
    async with run(app) as pilot:
        provider = tui.TtyplayerCommands(app.screen)
        hits = [hit async for hit in provider.discover()]
        assert [hit.text for hit in hits] == [
            "Search…", "Playlists", "Save queue as playlist…", "Next theme", "Settings…", "Help", "Quit", "Pause / resume", "Next", "Previous", "Mute",
            "Sleep…", "Radio",
        ]
        ran = []
        for _, action, _ in tui.COMMANDS:
            monkeypatch.setattr(app, f"action_{action}", functools.partial(ran.append, action))
        for hit in hits:
            await hit.command()
        assert ran == [action for _, action, _ in tui.COMMANDS]
        assert [hit.text async for hit in provider.search("mute")] == ["Mute"]
        await pilot.pause()


@drive
async def test_ctrl_p_mute_runs_the_same_action_as_the_key(clients, served):
    app = make_app(clients)
    async with run(app) as pilot:
        await search(pilot)
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("ctrl+p")
        await pilot.pause()
        await pilot.press(*"mute")
        await pilot.pause(0.5)
        await pilot.press("enter")
        await pilot.pause(0.2)
        assert clients.made[0].calls[-1] == ("toggle_mute",)


@drive
async def test_help_lists_the_palette_commands(clients, served):
    app = make_app(clients)
    async with run(app) as pilot:
        await search(pilot)
        await pilot.press("question_mark")
        await pilot.pause()
        lines = [str(s.render()) for s in app.screen.query(Static)]
        heading = lines.index("Commands (Ctrl-P)")
        assert lines[heading + 1 :] == [f"{name}  {help}" for name, _, help in tui.COMMANDS]


def readme_tui_section():
    readme = (pathlib.Path(__file__).parent.parent / "README.md").read_text(encoding="utf-8")
    return readme.split("## TUI\n", 1)[1].split("\n## ", 1)[0]


@drive
async def test_readme_documents_every_key_the_footer_shows(clients, served):
    owners = [
        tui.TtyplayerApp, tui.SearchBox, tui.VideoTable, tui.PickTable,
        tui.ResultsTable, tui.FavoritesTable, tui.QueueTable, tui.PlaylistTable,
    ]
    app = make_app(clients)
    async with run(app):
        shown = [
            app.get_key_display(Binding(key, ""))
            for owner in owners
            for binding in owner.BINDINGS
            if binding.show
            for key in binding.key.split(",")
        ]
    section = readme_tui_section()
    assert len(shown) > 10
    assert [key for key in shown if f"`{key}`" not in section] == []


# --- settings: clock, theme, search size, the Settings screen -----------------


def make_app_with(clients, saved, resolve=lambda text, limit, source: VIDEOS):
    """saved written to the (tmp_path) settings file first, so the app reads it at start."""
    settings.save(saved)
    return make_app(clients, resolve=resolve)


@drive
async def test_the_clock_is_hidden_when_show_clock_is_false(clients, served):
    app = make_app_with(clients, settings.Settings(show_clock=False))
    async with run(app) as pilot:
        await pilot.pause()
        assert not app.query_one(Header).query("HeaderClock")


@drive
async def test_the_saved_theme_is_applied_at_start(clients, served):
    app = make_app_with(clients, settings.Settings(theme="nord"))
    async with run(app) as pilot:
        await pilot.pause()
        assert app.theme == "nord"
        assert toasts(app) == []


@drive
async def test_an_unknown_saved_theme_is_a_toast_and_the_default(clients, served, settings_file):
    app = make_app_with(clients, settings.Settings(theme="no-such-theme"))
    async with run(app) as pilot:
        await pilot.pause()
        assert app.theme == "textual-dark"
        assert toasts(app) == [('Unknown theme "no-such-theme" in settings, using textual-dark', "warning")]
    assert settings.load().theme == "no-such-theme"  # the file is left as the user wrote it


@drive
async def test_t_saves_the_new_theme(clients, served):
    app = make_app_with(clients, settings.Settings(show_clock=False))
    async with run(app) as pilot:
        await search(pilot)
        await pilot.press("t")
        await pilot.pause()
        assert settings.load() == settings.Settings(show_clock=False, theme=app.theme)
        assert app.theme != "textual-dark"


@drive
async def test_search_and_m_fetch_search_limit_results(clients, served):
    resolve = recording_resolver(VIDEOS, VIDEOS + MORE)
    app = make_app_with(clients, settings.Settings(search_limit=25), resolve=resolve)
    async with run(app) as pilot:
        await search(pilot, "lofi")
        await pilot.press("m")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert resolve.asked == [("lofi", 25), ("lofi", 3 + 25)]


async def open_settings(pilot):
    await search(pilot)
    await pilot.press("S")
    await pilot.pause()
    assert isinstance(pilot.app.screen, tui.SettingsScreen)


def settings_rows(app):
    table = app.screen.query_one(DataTable)
    return [[str(cell) for cell in table.get_row_at(i)] for i in range(table.row_count)]


@drive
async def test_s_lists_every_setting_with_its_default(clients, served):
    app = make_app_with(clients, settings.Settings(search_limit=20))
    async with run(app) as pilot:
        await open_settings(pilot)
        assert settings_rows(app) == [
            ["show_clock", "true", "(default true)"],
            ["theme", "textual-dark", "(default textual-dark)"],
            ["search_limit", "20", "(default 10)"],
            ["server_host", "127.0.0.1", "(default 127.0.0.1)"],
            ["server_port", "7700", "(default 7700)"],
            ["server_token", "", "(default )"],
            ["remote_url", "", "(default )"],
            ["stream_enabled", "false", "(default false)"],
            ["spotify_client_id", "", "(default )"],
            ["show_levels", "true", "(default true)"],
            ["show_art", "true", "(default true)"],
            ["show_lyrics", "true", "(default true)"],
            ["search_source", "youtube", "(default youtube)"],
            ["radio", "false", "(default false)"],
            ["normalize_loudness", "false", "(default false)"],
            ["prefetch", "true", "(default true)"],
            ["seek_seconds", "5", "(default 5)"],
            ["volume_step", "5", "(default 5)"],
        ]
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, tui.SettingsScreen)


@drive
async def test_enter_on_show_clock_flips_it_saves_it_and_the_header_follows(clients, served):
    app = make_app_with(clients, settings.Settings())
    async with run(app) as pilot:
        await open_settings(pilot)
        main = app.screen_stack[0]
        assert main.query_one(Header).query("HeaderClock")
        await pilot.press("enter")
        await pilot.pause()
        assert settings.load().show_clock is False
        assert settings_rows(app)[0] == ["show_clock", "false", "(default true)"]
        assert len(main.query(Header)) == 1
        assert not main.query_one(Header).query("HeaderClock")
        await pilot.press("enter")
        await pilot.pause()
        assert settings.load().show_clock is True
        assert main.query_one(Header).query("HeaderClock")
        await pilot.press("escape")
        await pilot.pause()
        assert app.query_one(Header).region.y == 0


@drive
async def test_enter_on_a_text_setting_shows_how_to_set_it(clients, served, settings_file):
    app = make_app_with(clients, settings.Settings())
    async with run(app) as pilot:
        await open_settings(pilot)
        saved = settings_file.read_text(encoding="utf-8")
        await pilot.press("down", "enter")
        await pilot.pause()
        assert str(app.screen.query_one("#settings-hint", Static).render()) == "set with: ttyplayer config set theme <value>"
        assert settings_file.read_text(encoding="utf-8") == saved


async def type_number(pilot, key, text):
    """Enter on key's row in Settings, then its number box emptied and text typed and submitted."""
    table = pilot.app.screen.query_one(DataTable)
    table.move_cursor(row=settings.KEYS.index(key))
    await pilot.press("enter")
    await pilot.pause()
    assert isinstance(pilot.app.screen, tui.NameScreen)
    await pilot.press(*["backspace"] * len(pilot.app.screen.query_one(Input).value), *text, "enter")
    await pilot.pause()


@drive
async def test_enter_on_a_number_setting_asks_for_it_and_refuses_a_bad_one(clients, served, settings_file):
    app = make_app_with(clients, settings.Settings())
    async with run(app) as pilot:
        await open_settings(pilot)
        saved = settings_file.read_text(encoding="utf-8")
        for text, error in (("0", "seek_seconds must be between 1 and 300, not 0"), ("ten", "seek_seconds must be a whole number")):
            await type_number(pilot, "seek_seconds", text)
            assert str(app.screen.query_one("#name-error", Static).render()).startswith(error)
            assert settings_file.read_text(encoding="utf-8") == saved
            await pilot.press("escape")  # cancels: nothing changes
            await pilot.pause()
            assert isinstance(app.screen, tui.SettingsScreen)
        assert app.settings.seek_seconds == player.SEEK_SECONDS
        await type_number(pilot, "search_limit", "20")
        assert isinstance(app.screen, tui.SettingsScreen)
        assert settings_rows(app)[settings.KEYS.index("search_limit")] == ["search_limit", "20", "(default 10)"]
        assert settings.load().search_limit == 20


@drive
async def test_seek_and_volume_steps_set_in_settings_apply_on_the_next_keypress_and_in_the_help(clients, served):
    app = make_app(clients)
    async with run(app) as pilot:
        await search(pilot)
        await pilot.press("enter")
        await pilot.press("S")
        await pilot.pause()
        await type_number(pilot, "seek_seconds", "12")
        await type_number(pilot, "volume_step", "3")
        assert settings_rows(app)[-2:] == [["seek_seconds", "12", "(default 5)"], ["volume_step", "3", "(default 5)"]]
        assert (settings.load().seek_seconds, settings.load().volume_step) == (12, 3)
        await pilot.press("escape", "full_stop", "minus")
        await pilot.pause()
        assert clients.made[0].calls[-2:] == [("seek", 12), ("change_volume", -3)]
        await pilot.press("question_mark")
        await pilot.pause()
        lines = help_lines(app)
        for shown in ("Seek +12s", "Seek −12s", "Volume +3", "Volume −3"):
            assert any(line.endswith(f"  {shown}") for line in lines), shown


@drive
async def test_settings_is_in_the_palette_and_the_help(clients, served):
    app = make_app(clients)
    async with run(app) as pilot:
        await pilot.press("escape")
        await pilot.press("ctrl+p")
        await pilot.pause()
        await pilot.press(*"settings")
        await pilot.pause(0.5)
        await pilot.press("enter")
        await pilot.pause(0.2)
        assert isinstance(app.screen, tui.SettingsScreen)
        await pilot.press("S")  # once open, S does not stack a second one
        await pilot.pause()
        assert len(app.screen_stack) == 2


@drive
async def test_s_in_the_search_box_is_a_letter(clients, served):
    app = make_app(clients)
    async with run(app) as pilot:
        await pilot.press("S")
        await pilot.pause()
        assert app.query_one(Input).value == "S"
        assert not isinstance(app.screen, tui.SettingsScreen)


# --- the Playlists tab, P and A (tui-playlists) --------------------------------


def make_playlist(name, videos):
    playlists.create(name)
    playlists.add(name, videos)


def playlist_rows(app):
    return rows(app, tui.PlaylistTable)


def playlist_ids(name):
    return [video.id for video in playlists.load(name)]


async def open_playlists(pilot):
    await on_tab(pilot, "5")
    assert pilot.app.focused is pilot.app.query_one(tui.PlaylistTable)


async def open_playlist(pilot, *keys):
    """Tab 5, keys to reach a playlist's row, Enter to open it."""
    await open_playlists(pilot)
    await pilot.press(*keys, "enter")
    await pilot.pause()


@drive
async def test_playlists_tab_lists_every_playlist_with_its_count_and_length(clients, served):
    make_playlist("mix", [VIDEOS[0], VIDEOS[1], VIDEOS[0]])
    playlists.create("empty")
    app = make_app(clients)
    async with run(app) as pilot:
        await pilot.pause()
        assert tab_title(app, "playlists") == "Playlists (2)"
        await open_playlists(pilot)
        table = app.query_one(tui.PlaylistTable)
        assert [str(column.label) for column in table.columns.values()] == ["#", "Name", "Tracks", "Length"]
        assert playlist_rows(app) == [[" 1", "empty", "0", "0:00"], [" 2", "mix", "3", "2:02"]]
        make_playlist("new", [VIDEOS[2]])  # from another terminal: shown the next time the tab is
        await on_tab(pilot, "1")
        await on_tab(pilot, "5")
        assert [row[1] for row in playlist_rows(app)] == ["empty", "mix", "new"]
        assert tab_title(app, "playlists") == "Playlists (3)"


@drive
async def test_playlists_tab_without_playlists(clients, served):
    app = make_app(clients)
    async with run(app) as pilot:
        await open_playlists(pilot)
        assert tab_title(app, "playlists") == "Playlists"
        assert playlist_rows(app) == []
        await pilot.press("enter", "d", "s", "a", "K", "J", "escape")
        await pilot.pause()
        assert clients.made == [] and toasts(app) == [] and len(app.screen_stack) == 1


@drive
async def test_enter_opens_a_playlist_and_escape_or_backspace_goes_back(clients, served):
    make_playlist("a list", [VIDEOS[0]])
    make_playlist("mix", [VIDEOS[2], VIDEOS[1]])
    favorites.add(VIDEOS[1])
    app = make_app(clients)
    async with run(app) as pilot:
        for back in ("escape", "backspace"):
            await open_playlist(pilot, "down")
            table = app.query_one(tui.PlaylistTable)
            assert [str(column.label) for column in table.columns.values()] == ["#", "Title", "Uploader", "Length"]
            assert playlist_rows(app) == [[" 1", "Gamma", "Gus", "60:00"], [" 2", "Beta ♥", "Bob", "--:--"]]
            assert tab_title(app, "playlists") == "Playlists › mix (2)"
            await pilot.press(back)
            await pilot.pause()
            assert [row[1] for row in playlist_rows(app)] == ["a list", "mix"]
            assert tab_title(app, "playlists") == "Playlists (2)"
            assert table.cursor_row == 1  # back on the playlist it came from


@drive
async def test_enter_on_a_track_plays_the_whole_playlist_from_it_and_marks_it(clients, served):
    make_playlist("mix", [VIDEOS[0], VIDEOS[1], VIDEOS[2], VIDEOS[0]])
    app = make_app(clients)
    async with run(app) as pilot:
        await open_playlist(pilot)
        await pilot.press("down", "enter")
        await pilot.pause()
        client = clients.made[0]
        assert [video.id for video in client.queue] == ["a", "b", "c", "a"]
        assert client.index == 1 and client.calls == [("play_current", "b")]
        assert [row[0] for row in playlist_rows(app)] == [" 1", "▸2", " 3", " 4"]
        assert queue_title(app) == "Queue (4)"


@drive
async def test_a_on_a_track_appends_it_to_the_queue(clients, served):
    make_playlist("mix", VIDEOS)
    app = make_app(clients)
    async with run(app) as pilot:
        await open_playlist(pilot)
        await pilot.press("down", "a", "down", "a")
        await pilot.pause()
        client = clients.made[0]
        assert [video.id for video in client.queue] == ["b", "c"]
        assert client.calls == [("play_current", "b")]


@drive
async def test_s_shuffle_plays_the_playlist(clients, served, monkeypatch):
    make_playlist("mix", VIDEOS)
    monkeypatch.setattr(tui, "shuffler", random.Random(4))
    expected = list(VIDEOS)
    random.Random(4).shuffle(expected)
    app = make_app(clients)
    async with run(app) as pilot:
        await open_playlist(pilot)
        await pilot.press("s")
        await pilot.pause()
        client = clients.made[0]
        assert client.queue == expected and client.index == 0
        assert client.calls == [("play_current", expected[0].id)]
        assert playlist_ids("mix") == ["a", "b", "c"]  # the file keeps its order


@drive
async def test_d_k_and_j_inside_a_playlist_edit_the_file(clients, served):
    make_playlist("mix", VIDEOS)
    app = make_app(clients)
    async with run(app) as pilot:
        await open_playlist(pilot)
        table = app.query_one(tui.PlaylistTable)
        await pilot.press("J")
        await pilot.pause()
        assert playlist_ids("mix") == ["b", "a", "c"] and table.cursor_row == 1
        await pilot.press("J", "J")  # already last: stays
        await pilot.pause()
        assert playlist_ids("mix") == ["b", "c", "a"] and table.cursor_row == 2
        await pilot.press("K")
        await pilot.pause()
        assert playlist_ids("mix") == ["b", "a", "c"] and table.cursor_row == 1
        assert [row[1] for row in playlist_rows(app)] == ["Beta", "Alpha", "Gamma"]
        await pilot.press("d")
        await pilot.pause()
        assert playlist_ids("mix") == ["b", "c"]
        assert toasts(app) == [("Removed from mix: Alpha", "information")]
        assert [row[1] for row in playlist_rows(app)] == ["Beta", "Gamma"]
        assert tab_title(app, "playlists") == "Playlists › mix (2)"


@drive
async def test_an_open_playlist_deleted_from_another_terminal_goes_back_to_the_list(clients, served):
    make_playlist("mix", VIDEOS)
    make_playlist("other", VIDEOS)
    app = make_app(clients)
    async with run(app) as pilot:
        await open_playlist(pilot)
        playlists.delete("mix")
        await pilot.press("d")
        await pilot.pause()
        assert toasts(app) == [("No playlist named mix", "error")]
        assert [row[1] for row in playlist_rows(app)] == ["other"]


@pytest.mark.parametrize("answer, kept", [("y", False), ("enter", False), ("escape", True)])
@drive
async def test_d_on_a_playlist_deletes_it_after_a_confirm(answer, kept, clients, served):
    make_playlist("mix", VIDEOS)
    make_playlist("other", VIDEOS)
    app = make_app(clients)
    async with run(app) as pilot:
        await open_playlists(pilot)
        await pilot.press("d")
        await pilot.pause()
        assert isinstance(app.screen, tui.ConfirmScreen)
        assert str(app.screen.query(Static).first().render()) == "Delete playlist mix?"
        await pilot.press(answer)
        await pilot.pause()
        assert not isinstance(app.screen, tui.ConfirmScreen)
        assert ("mix" in playlists.names()) is kept
        assert len(playlist_rows(app)) == (2 if kept else 1)
        assert toasts(app) == ([] if kept else [("Deleted playlist mix", "information")])


async def name_dialog(pilot, name):
    """Type name over the name modal's prefill and press Enter."""
    assert isinstance(pilot.app.screen, tui.NameScreen)
    pilot.app.screen.query_one(Input).value = name
    await pilot.press("enter")
    await pilot.pause()


@drive
async def test_p_with_nothing_queued_says_so(clients, served):
    app = make_app(clients)
    async with run(app) as pilot:
        await pilot.press("escape", "P")
        await pilot.pause()
        assert toasts(app) == [("Nothing to save", "warning")]
        assert len(app.screen_stack) == 1 and playlists.names() == []


@drive
async def test_p_after_the_queue_played_to_its_end_says_nothing_to_save(clients, served):
    app = make_app(clients)
    async with run(app) as pilot:
        client = await queued(pilot, "enter")
        for _ in range(3):
            client.handle_message({"event": "end-file", "reason": "eof"})
        await pilot.pause()
        assert client.idle and len(client.queue) == 3
        await pilot.press("P")
        await pilot.pause()
        assert toasts(app) == [("Nothing to save", "warning")]
        assert len(app.screen_stack) == 1 and playlists.names() == []


@drive
async def test_p_saves_the_queue_under_a_valid_name(clients, served):
    resolve = recording_resolver(VIDEOS)
    app = make_app(clients, resolve=resolve)
    async with run(app) as pilot:
        await queued(pilot, "enter")
        await pilot.press("P")
        await pilot.pause()
        assert app.screen.query_one(Input).value == f"Queue {datetime.date.today()}"
        await name_dialog(pilot, "bad/name")
        error = str(app.screen.query_one("#name-error", Static).render())
        assert error == f"Bad playlist name 'bad/name': use {playlists.NAME_RULE}"
        assert playlists.names() == []
        await name_dialog(pilot, "road trip")
        assert not isinstance(app.screen, tui.NameScreen)
        assert playlist_ids("road trip") == ["a", "b", "c"]
        assert toasts(app) == [("Saved 3 tracks to road trip", "information")]
        assert tab_title(app, "playlists") == "Playlists (1)"
        await app.workers.wait_for_complete()
        assert resolve.asked == [("lofi", SEARCH_LIMIT)]  # Enter in the modal is not a search


@drive
async def test_p_with_one_track_suggests_its_title_and_replaces_a_playlist_of_that_name(clients, served):
    make_playlist("Gamma", VIDEOS)
    app = make_app(clients, resolve=lambda text, limit, source: [Video(id="c", title="Gamma: live!", uploader="Gus", duration=1)])
    async with run(app) as pilot:
        await queued(pilot, "enter")
        await pilot.press("P")
        await pilot.pause()
        assert app.screen.query_one(Input).value == "Gamma live"
        await name_dialog(pilot, "Gamma")
        assert playlist_ids("Gamma") == ["c"]


@drive
async def test_escape_cancels_the_name_modal(clients, served):
    app = make_app(clients)
    async with run(app) as pilot:
        await queued(pilot, "enter")
        await pilot.press("P")
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert len(app.screen_stack) == 1 and playlists.names() == [] and toasts(app) == []


def picker_options(app):
    options = app.screen.query_one(OptionList)
    return [str(options.get_option_at_index(i).prompt) for i in range(options.option_count)]


@drive
async def test_a_capital_adds_a_search_row_to_an_existing_playlist(clients, served):
    make_playlist("mix", [VIDEOS[0]])
    playlists.create("chill")
    app = make_app(clients)
    async with run(app) as pilot:
        await search(pilot)
        await pilot.press("down", "A")
        await pilot.pause()
        assert isinstance(app.screen, tui.PlaylistPicker)
        assert picker_options(app) == ["chill", "mix", "New playlist…"]
        await pilot.press("down", "enter")
        await pilot.pause()
        assert len(app.screen_stack) == 1
        assert playlist_ids("mix") == ["a", "b"]
        assert toasts(app) == [("Added to mix", "information")]
        assert [row[:3] for row in playlist_rows(app)] == [[" 1", "chill", "0"], [" 2", "mix", "2"]]


@drive
async def test_a_capital_to_a_new_playlist_from_history_favorites_and_the_queue(clients, served):
    history.record(VIDEOS[1])
    favorites.add(VIDEOS[2])
    app = make_app(clients)
    async with run(app) as pilot:
        await queued(pilot, "enter")
        await pilot.press("A")
        await pilot.pause()
        assert picker_options(app) == ["New playlist…"]
        await pilot.press("enter")
        await pilot.pause()
        await name_dialog(pilot, "")
        assert str(app.screen.query_one("#name-error", Static).render()).startswith("Bad playlist name ''")
        await name_dialog(pilot, "picks")
        assert playlist_ids("picks") == ["a"]
        assert toasts(app) == [("Added to picks", "information")]
        for tab in ("3", "4"):
            await on_tab(pilot, tab)
            await pilot.press("A", "enter")
            await pilot.pause()
        assert playlist_ids("picks") == ["a", "b", "c"]
        await on_tab(pilot, "4")
        await pilot.press("A", "end", "enter")  # New playlist… with a name taken
        await pilot.pause()
        await name_dialog(pilot, "picks")
        assert str(app.screen.query_one("#name-error", Static).render()) == "A playlist named picks already exists"
        await pilot.press("escape")
        await pilot.pause()
        assert len(app.screen_stack) == 1 and playlist_ids("picks") == ["a", "b", "c"]


@drive
async def test_a_capital_with_no_row_does_nothing(clients, served):
    app = make_app(clients)
    async with run(app) as pilot:
        for tab in ("1", "2", "3", "4", "5"):
            await on_tab(pilot, tab)
            await pilot.press("A")
            await pilot.pause()
        assert len(app.screen_stack) == 1


@drive
async def test_playlist_keys_are_in_the_palette_footer_and_help(clients, served):
    make_playlist("mix", VIDEOS)
    app = make_app(clients)
    async with run(app) as pilot:
        await pilot.press("escape", "ctrl+p")
        await pilot.pause()
        await pilot.press(*"playlists")
        await pilot.pause(0.5)
        await pilot.press("enter")
        await pilot.pause(0.2)
        assert app.query_one(TabbedContent).active == "playlists"
        assert app.focused is app.query_one(tui.PlaylistTable)
        shown = {(key.key, key.description) for key in app.query("FooterKey") if key.description}
        assert shown >= {("d", "Remove"), ("s", "Shuffle"), ("A", "To playlist"), ("P", "Save queue")}
        await pilot.press("question_mark")
        await pilot.pause()
        lines = [str(s.render()) for s in app.screen.query(Static)]
        assert "Playlists table" in lines


def test_the_design_doc_lists_the_playlist_keys():
    design = (pathlib.Path(__file__).parent.parent / "docs" / "tui-design.md").read_text(encoding="utf-8")
    for key in ("`5`", "`P`", "`A`", "`s`", "Backspace"):
        assert key in design.split("## Keys", 1)[1].split("\n## ", 1)[0]


# --- tui-remote: the same app driving ttyplayer serve -----------------------------


@pytest.fixture
def remote_served(monkeypatch):
    """ttyplayer serve over a fake player (tests/test_remote.py's), its lookups answered from VIDEOS."""
    from test_remote import Served

    by_url = {video.url: video for video in VIDEOS}
    monkeypatch.setattr(youtube, "fetch", lambda url: [by_url[url]])
    monkeypatch.setattr(remote, "RETRY_SECONDS", (0.05,))
    served = Served()
    yield served
    served.stop()


async def settle(pilot, condition, timeout=5):
    """Let the app run until condition() holds, as the remote's threads get there."""
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            pytest.fail("timed out waiting")
        await pilot.pause(0.02)


def remote_app(url, token="secret-token"):
    return remote.RemoteApp(url, token, resolve=lambda text, limit, source: VIDEOS)


@drive
async def test_the_app_runs_unchanged_against_a_server(remote_served, served, library):
    app = remote_app(remote_served.url)
    async with run(app) as pilot:
        await settle(pilot, lambda: app.client is not None and app.client.status() is not None)
        assert app.sub_title == f"remote: 127.0.0.1:{remote_served.port}"
        assert served == []  # no control socket: this machine plays nothing
        await search(pilot)
        await pilot.press("down", "enter")
        await settle(pilot, lambda: remote_served.player.queue == VIDEOS[1:])
        assert remote_served.player.index == 0 and remote_served.player.idle is False
        await settle(pilot, lambda: [row[1] for row in rows(app, tui.QueueTable)] == ["Beta", "Gamma"])
        assert text(app, "#np-title") == "Beta"
        await pilot.press("n")
        await settle(pilot, lambda: remote_served.player.index == 1)
        await settle(pilot, lambda: text(app, "#np-title") == "Gamma")
        assert toasts(app) == []
    assert not library["history"].exists()  # history is the server's


@drive
async def test_shift_j_and_k_reorder_the_servers_queue(remote_served, served, library):
    app = remote_app(remote_served.url)
    async with run(app) as pilot:
        await settle(pilot, lambda: app.client is not None and app.client.status() is not None)
        await queued(pilot, "enter")
        await settle(pilot, lambda: [row[1] for row in queue_rows(app)] == ["Alpha", "Beta", "Gamma"])
        queue = app.query_one(tui.QueueTable)
        await pilot.press("J")
        await settle(pilot, lambda: [row[:2] for row in queue_rows(app)] == [[" 1", "Beta"], ["▸2", "Alpha"], [" 3", "Gamma"]])
        assert remote_served.player.queue == [VIDEOS[1], VIDEOS[0], VIDEOS[2]]
        assert remote_served.player.index == 1  # Alpha is still the one playing
        assert queue.cursor_row == 1
        await pilot.press("shift+down", "J")  # the second is already last: nothing is sent
        await settle(pilot, lambda: [v.id for v in remote_served.player.queue] == ["b", "c", "a"])
        await pilot.pause(0.1)
        assert queue.cursor_row == 2
        await pilot.press("K", "shift+up")
        await settle(pilot, lambda: [v.id for v in remote_served.player.queue] == ["a", "b", "c"])
        await settle(pilot, lambda: queue.cursor_row == 0)
        assert remote_served.player.index == 0
        moves = [body["value"] for path, body in remote_served.posts() if body.get("name") == "move"]
        assert moves == [[0, 1], [1, 2], [2, 1], [1, 0]]
        assert toasts(app) == []


@drive
async def test_an_unreachable_server_is_one_toast_and_the_app_runs_on(served, monkeypatch):
    from test_remote import free_port

    monkeypatch.setattr(remote, "RETRY_SECONDS", (0.05,))
    url = f"http://127.0.0.1:{free_port()}"
    app = remote_app(url)
    async with run(app) as pilot:
        await settle(pilot, lambda: toasts(app))
        await pilot.pause(0.3)  # several retries
        assert toasts(app) == [(f"Server unreachable at {url}", "error")]
        await pilot.press("escape", "space")
        await pilot.pause(0.1)
        assert toasts(app)[-1] == (f"Server unreachable at {url}", "error")
        assert app.is_running


@drive
async def test_a_wrong_token_is_one_toast(remote_served, served):
    app = remote_app(remote_served.url, token="wrong")
    async with run(app) as pilot:
        await settle(pilot, lambda: toasts(app))
        await pilot.pause(0.3)
        assert toasts(app) == [("Server rejected the token", "error")]


# --- search sources: the sc: / yt: prefix, the heading, the SC tag -----------

SC_VIDEO = Video(id="123", title="Roygbiv", uploader="warp", duration=151, source="soundcloud", link="https://soundcloud.com/warp/roygbiv")


def search_heading(app):
    return str(app.query_one(TabbedContent).get_tab("search").label)


@drive
async def test_an_sc_prefix_searches_soundcloud_and_the_heading_and_rows_say_so(clients, served):
    asked = []
    answers = [[SC_VIDEO], [SC_VIDEO, *VIDEOS[:1]], VIDEOS]
    app = make_app(clients, resolve=lambda text, limit, source: asked.append((text, source)) or answers.pop(0))
    async with run(app) as pilot:
        await search(pilot, "sc: boards of canada")
        assert search_heading(app) == "SoundCloud results"
        assert rows(app) == [[" 1", "SC Roygbiv", "warp", "2:31"]]
        await pilot.press("m")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert rows(app)[1] == [" 2", "Alpha", "Ann", "1:01"]
        await search(pilot, "lofi")
        assert search_heading(app) == "YouTube results"
        assert asked == [("boards of canada", "soundcloud"), ("boards of canada", "soundcloud"), ("lofi", "youtube")]


@drive
async def test_the_search_source_setting_is_the_tuis_default_and_yt_overrides_it(clients, served):
    asked = []
    app = make_app_with(clients, settings.Settings(search_source="soundcloud"), resolve=lambda text, limit, source: asked.append((text, source)) or VIDEOS)
    async with run(app) as pilot:
        await search(pilot, "boards")
        assert search_heading(app) == "SoundCloud results"
        await search(pilot, "yt: boards")
        assert search_heading(app) == "YouTube results"
        assert asked == [("boards", "soundcloud"), ("boards", "youtube")]


@drive
async def test_a_link_keeps_the_search_heading(clients, served):
    app = make_app(clients)
    async with run(app) as pilot:
        await search(pilot, "https://soundcloud.com/warp/roygbiv")
        assert search_heading(app) == "Search"


# --- sleep timer ----------------------------------------------------------


@drive
async def test_the_panel_shows_the_sleep_timer_while_it_is_armed(clients, served):
    app = make_app(clients)
    async with run(app) as pilot:
        app.on_player_state(status(sleep={"ends_at": time.time() + 600}))
        await pilot.pause()
        assert re.fullmatch(r"zz (10:00|9:5\d)", text(app, "#np-sleep"))
        app.on_player_state(status(sleep={"after": "track"}))
        await pilot.pause()
        assert text(app, "#np-sleep") == "zz end"
        app.on_player_state(status(sleep=None))
        await pilot.pause()
        assert text(app, "#np-sleep") == ""


class NoTimer:
    def __init__(self, interval, function, args=()):
        pass

    def start(self):
        pass

    def cancel(self):
        pass

    def join(self, timeout=None):
        pass


async def sleep_dialog(pilot):
    """Ctrl-P's Sleep…, as the palette runs it: the dialog, prefilled."""
    await pilot.app.run_action("sleep")
    await pilot.pause()
    assert isinstance(pilot.app.screen, tui.NameScreen)
    assert pilot.app.screen.query_one(Input).value == "30m"


@drive
async def test_sleep_command_arms_the_players_timer_and_says_so(clients, served):
    app = make_app(clients)
    async with run(app) as pilot:
        client = await queued(pilot, "enter")
        client.handle_control = functools.partial(player.MpvClient.handle_control, client)  # the real sleep wiring
        client.timer = NoTimer
        await sleep_dialog(pilot)
        await pilot.press("enter")
        await pilot.pause()
        assert not isinstance(app.screen, tui.NameScreen)
        assert client.sleep_status() == {"ends_at": client.sleep_ends_at}
        [(message, severity)] = toasts(app)
        assert re.fullmatch(r"Sleeping in (30:00|29:59)", message) and severity == "information"
        assert text(app, "#np-sleep").startswith("zz ")
        await sleep_dialog(pilot)
        await name_dialog(pilot, "soon")
        error = str(app.screen.query_one("#name-error", Static).render())
        assert error == f"Sleep takes {player.SLEEP_FORMS}, not 'soon'"
        await name_dialog(pilot, "off")
        assert client.sleep_status() is None
        assert text(app, "#np-sleep") == ""


@drive
async def test_esc_leaves_the_sleep_timer_alone(clients, served):
    app = make_app(clients)
    async with run(app) as pilot:
        client = await queued(pilot, "enter")
        client.handle_control = functools.partial(player.MpvClient.handle_control, client)
        await sleep_dialog(pilot)
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, tui.NameScreen)
        assert client.sleep_status() is None
        assert toasts(app) == []


@drive
async def test_sleep_with_nothing_playing_says_so_in_the_dialog(clients, served):
    app = make_app(clients)
    async with run(app) as pilot:
        await sleep_dialog(pilot)
        await pilot.press("enter")
        await pilot.pause()
        assert str(app.screen.query_one("#name-error", Static).render()) == "Nothing is playing"
        assert clients.made == []


# --- radio ----------------------------------------------------------------


@drive
async def test_the_panel_shows_the_radio_and_while_it_fetches(clients, served):
    app = make_app(clients)
    async with run(app) as pilot:
        for radio, shown in [(True, "∞"), ("fetching", "∞ fetching…"), (False, "")]:
            app.on_player_state(status(radio=radio))
            await pilot.pause()
            assert text(app, "#np-radio") == shown


@drive
async def test_capital_r_toggles_the_players_radio_and_says_so(clients, served):
    app = make_app(clients)
    async with run(app) as pilot:
        client = await queued(pilot, "enter")
        client.handle_control = functools.partial(player.MpvClient.handle_control, client)  # the real radio wiring
        await pilot.press("R")
        await pilot.pause()
        assert client.radio is True
        assert text(app, "#np-radio") == "∞"
        await pilot.press("R")
        await pilot.pause()
        assert client.radio is False
        assert text(app, "#np-radio") == ""
        assert toasts(app) == [("Radio on", "information"), ("Radio off", "information")]


@drive
async def test_the_radio_setting_starts_the_player_with_the_radio_on(clients, served):
    app = make_app_with(clients, settings.Settings(radio=True))
    async with run(app) as pilot:
        await queued(pilot, "enter")
        assert clients.made[-1].radio is True


# --- album art --------------------------------------------------------------

ART = [dataclasses.replace(video, thumbnail=f"https://i.ytimg.com/vi/{video.id}/hqdefault.jpg") for video in VIDEOS]


class FakeImage(Static):
    """Stands in for textual_image's Image widget: holds what it was given."""

    image = None


class FakeFetch:
    """Stands in for art.fetch: answers each url, after its gate opens if it has one; records the calls."""

    def __init__(self, answer=b"image bytes"):
        self.answer = answer
        self.calls = []
        self.gates = {}

    def __call__(self, url):
        self.calls.append(url)
        if url in self.gates:
            self.gates[url].wait(5)
        return self.answer


@pytest.fixture
def with_art(monkeypatch):
    """The art extra as installed, with a fake fetcher and image widget; decode returns what it got."""
    fetch = FakeFetch()
    monkeypatch.setattr(art, "available", lambda: True)
    monkeypatch.setattr(art, "image_widget", lambda: FakeImage)
    monkeypatch.setattr(art, "fetch", fetch)
    monkeypatch.setattr(art, "decode", lambda data: data and ("decoded", data))
    return fetch


def art_image(app):
    return app.query_one(tui.Art).query_one(app.image_widget).image


async def play_art(pilot, row=0):
    """Search, play the given row of ART, and let its art load."""
    await search(pilot)
    await pilot.press(*["down"] * row, "enter")
    await pilot.app.workers.wait_for_complete()
    await pilot.pause()


@drive
async def test_the_art_column_sits_left_of_the_lines_and_the_panel_keeps_its_height(clients, served, with_art):
    app = make_app(clients, resolve=lambda text, limit, source: ART)
    async with run(app) as pilot:
        await pilot.pause()
        column, lines = app.query_one(tui.Art), app.query_one("#np-text")
        now = app.query_one(tui.NowPlaying)
        assert (column.display, column.size.width, column.size.height) == (True, 10, 5)
        assert column.region.right < lines.region.x and column.region.y == lines.region.y
        assert now.region.height == 7
        assert art_image(app) is None  # idle: blank
        await play_art(pilot)
        assert with_art.calls == [ART[0].thumbnail]
        assert art_image(app) == ("decoded", b"image bytes")
        assert now.region.height == 7 and column.size == (10, 5)
        clients.made[0].jump(1)
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert with_art.calls == [ART[0].thumbnail, ART[1].thumbnail]
        assert now.region.height == 7 and column.size == (10, 5)
        clients.made[0].idle = True
        clients.made[0].notify()
        await pilot.pause()
        assert art_image(app) is None
        assert now.region.height == 7 and column.size == (10, 5)


@drive
async def test_the_art_column_is_as_tall_as_the_panel_without_levels(clients, served, with_art):
    app = make_app_with(clients, settings.Settings(show_levels=False))
    async with run(app) as pilot:
        await pilot.pause()
        assert app.query_one(tui.NowPlaying).region.height == 5
        assert app.query_one(tui.Art).size == (10, 3)


@drive
async def test_the_art_is_blank_while_loading_and_a_stale_result_is_dropped(clients, served, with_art):
    first, second = threading.Event(), threading.Event()
    with_art.gates = {ART[0].thumbnail: first, ART[1].thumbnail: second}
    app = make_app(clients, resolve=lambda text, limit, source: ART)
    async with run(app) as pilot:
        await search(pilot)
        await pilot.press("enter")
        await pilot.pause()
        assert art_image(app) is None  # loading
        client = clients.made[0]
        client.jump(1)  # the track changed while its art was on the way
        await pilot.pause()
        first.set()
        await asyncio.to_thread(time.sleep, 0.1)
        await pilot.pause()
        assert art_image(app) is None  # the first track's art came too late
        second.set()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert art_image(app) == ("decoded", b"image bytes")


@drive
async def test_a_failed_fetch_or_a_track_without_a_thumbnail_leaves_the_column_blank(clients, served, with_art):
    with_art.answer = None
    tracks = [ART[0], VIDEOS[1]]
    app = make_app(clients, resolve=lambda text, limit, source: tracks)
    async with run(app) as pilot:
        await play_art(pilot)
        assert art_image(app) is None
        clients.made[0].jump(1)
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert with_art.calls == [ART[0].thumbnail]  # VIDEOS[1] has no thumbnail: nothing to fetch
        assert art_image(app) is None
        assert app.query_one(tui.Art).size == (10, 5)


@drive
async def test_without_the_art_extra_there_is_no_column_and_nothing_is_fetched(clients, served, monkeypatch):
    fetch = FakeFetch()
    monkeypatch.setattr(art, "fetch", fetch)
    app = make_app(clients, resolve=lambda text, limit, source: ART)
    async with run(app) as pilot:
        await play_art(pilot)
        assert app.image_widget is None
        assert app.query_one(tui.Art).display is False
        assert not app.query_one(tui.Art).children
        assert app.query_one("#np-text").region.x == app.query_one(tui.NowPlaying).content_region.x
        assert app.query_one(tui.NowPlaying).region.height == 7
        assert fetch.calls == []


@drive
async def test_show_art_false_hides_the_column_and_the_settings_screen_brings_it_back(clients, served, with_art):
    settings.save(settings.Settings(show_art=False))
    app = make_app(clients, resolve=lambda text, limit, source: ART)
    async with run(app) as pilot:
        await play_art(pilot)
        column = app.query_one(tui.Art)
        assert column.display is False
        assert with_art.calls == []
        assert app.query_one("#np-text").region.x == app.query_one(tui.NowPlaying).content_region.x
        app.change_setting("show_art", True)  # what Enter on its Settings row does
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert column.display is True and column.size == (10, 5)
        assert with_art.calls == [ART[0].thumbnail]
        assert art_image(app) == ("decoded", b"image bytes")
        app.change_setting("show_art", False)
        await pilot.pause()
        assert column.display is False
        assert settings.load().show_art is False
        assert app.query_one(tui.NowPlaying).region.height == 7


@drive
async def test_enter_on_show_art_in_the_settings_screen_toggles_the_column(clients, served, with_art):
    app = make_app(clients)
    async with run(app) as pilot:
        await open_settings(pilot)
        app.screen.query_one(DataTable).move_cursor(row=settings.KEYS.index("show_art"))
        await pilot.press("enter")
        await pilot.pause()
        assert settings.load().show_art is False
        assert app.screen_stack[0].query_one(tui.Art).display is False


@drive
async def test_the_real_image_widget_shows_a_2x2_png(clients, served, monkeypatch):
    pytest.importorskip("PIL")
    pytest.importorskip("textual_image")
    from textual_image.widget import Image

    monkeypatch.setattr(art, "available", lambda: True)
    monkeypatch.setattr(art, "fetch", FakeFetch(answer=png_2x2()))
    app = make_app(clients, resolve=lambda text, limit, source: ART)
    async with run(app) as pilot:
        await pilot.pause()
        assert app.image_widget is Image
        height = app.query_one(tui.NowPlaying).region.height
        await play_art(pilot)
        image = art_image(app)
        assert image.size == (2, 2)
        column = app.query_one(tui.Art)
        assert column.size == (10, 5)
        assert 0 < column.query_one(Image).size.width <= 10 and 0 < column.query_one(Image).size.height <= 5
        assert app.query_one(tui.NowPlaying).region.height == height == 7


def png_2x2():
    import io

    from PIL import Image

    out = io.BytesIO()
    Image.new("RGB", (2, 2), "red").save(out, "PNG")
    return out.getvalue()


# --- lyrics ---------------------------------------------------------------------

SONGS = [
    Video(id="s1", title="Queen - Bohemian Rhapsody (Official Video)", uploader="Queen Official", duration=355),
    Video(id="s2", title="Get Lucky", uploader="Daft Punk - Topic", duration=248),
]
SYNCED = lyrics.Lyrics([(float(second), f"line {second}") for second in range(0, 200, 5)], None, "https://lrclib.net/api/get/1")


def lyrics_view(app):
    return app.query_one(tui.LyricsView)


def lyrics_message(app):
    view = lyrics_view(app)
    message = view.query_one("#lyrics-message", Static)
    return str(message.render()) if message.display else None


def lyrics_lines(app):
    """The shown lines, the highlighted one marked with a *."""
    return [("*" if line.has_class("-current") else "") + str(line.render()) for line in lyrics_view(app).lines]


async def play_song(pilot, row=0):
    await search(pilot)
    await pilot.press(*["down"] * row, "enter")
    await pilot.app.workers.wait_for_complete()
    await pilot.pause()


async def show_lyrics_tab(pilot):
    await pilot.press("6")
    await pilot.app.workers.wait_for_complete()
    await pilot.pause()


async def tick(pilot, client, position):
    """A status tick from the player at position seconds."""
    client.state["time-pos"] = position
    client.notify()
    await pilot.pause()


@drive
async def test_lyrics_tab_says_nothing_playing_and_focuses_on_6(clients, served, find_lyrics):
    app = make_app(clients, resolve=lambda text, limit, source: SONGS)
    async with run(app) as pilot:
        await pilot.press("escape", "6")
        await pilot.pause()
        assert app.query_one(TabbedContent).active == "lyrics"
        assert app.focused is lyrics_view(app)
        assert lyrics_message(app) == "Nothing playing"
        assert find_lyrics.calls == []


@drive
async def test_lyrics_are_looked_up_only_while_the_tab_is_shown_and_kept_while_hidden(clients, served, find_lyrics):
    find_lyrics.answers = {"s1": SYNCED}
    app = make_app(clients, resolve=lambda text, limit, source: SONGS)
    async with run(app) as pilot:
        await play_song(pilot)
        await tick(pilot, clients.made[0], 12)
        assert find_lyrics.calls == []  # hidden: nothing fetched
        app.query_one(TabbedContent).active = "lyrics"
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert find_lyrics.calls == ["s1"]
        assert lyrics_message(app) is None
        assert lyrics_lines(app)[:4] == ["line 0", "line 5", "*line 10", "line 15"]
        app.query_one(TabbedContent).active = "search"
        await tick(pilot, clients.made[0], 30)
        app.query_one(TabbedContent).active = "lyrics"
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert find_lyrics.calls == ["s1"]  # the same track: kept, not fetched again
        assert lyrics_lines(app)[6] == "*line 30"


@drive
async def test_lyrics_look_up_shows_looking_up_then_the_lines_and_drops_a_stale_result(clients, served, find_lyrics):
    first, second = threading.Event(), threading.Event()
    find_lyrics.gates = {"s1": first, "s2": second}
    find_lyrics.answers = {"s1": SYNCED, "s2": lyrics.Lyrics(None, "plain one\nplain two", "url")}
    app = make_app(clients, resolve=lambda text, limit, source: SONGS)
    async with run(app) as pilot:
        await search(pilot)
        await pilot.press("enter", "6")
        await pilot.pause()
        assert lyrics_message(app) == "Looking up…"
        clients.made[0].jump(1)  # the track changed while its lyrics were on the way
        await pilot.pause()
        first.set()
        await asyncio.to_thread(time.sleep, 0.1)
        await pilot.pause()
        assert lyrics_message(app) == "Looking up…"  # the first track's lyrics came too late
        second.set()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert find_lyrics.calls == ["s1", "s2"]
        assert lyrics_message(app) is None
        assert lyrics_lines(app) == ["plain one", "plain two"]  # plain lyrics: nothing highlighted
        await tick(pilot, clients.made[0], 100)
        assert lyrics_lines(app) == ["plain one", "plain two"]


@drive
async def test_no_lyrics_names_the_guessed_artist_and_track(clients, served, find_lyrics):
    app = make_app(clients, resolve=lambda text, limit, source: SONGS)
    async with run(app) as pilot:
        await play_song(pilot)
        await show_lyrics_tab(pilot)
        assert lyrics_message(app) == 'No lyrics found for "Queen – Bohemian Rhapsody"'
        clients.made[0].jump(1)
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert lyrics_message(app) == 'No lyrics found for "Daft Punk – Get Lucky"'
        clients.made[0].idle = True
        clients.made[0].notify()
        await pilot.pause()
        assert lyrics_message(app) == "Nothing playing"


@drive
async def test_the_highlight_follows_the_position_and_stays_in_the_middle(clients, served, find_lyrics):
    find_lyrics.answers = {"s1": SYNCED}
    app = make_app(clients, resolve=lambda text, limit, source: SONGS)
    async with run(app) as pilot:
        await play_song(pilot)
        await show_lyrics_tab(pilot)
        client, view = clients.made[0], lyrics_view(app)
        await tick(pilot, client, 2)
        assert lyrics_lines(app)[:2] == ["*line 0", "line 5"]
        assert view.scroll_y == 0  # the top line cannot go higher than the top
        for position in (102, 104.9, 125):  # mid-song: near the end the view stops at its last line
            await tick(pilot, client, position)
            await tick(pilot, client, position)  # the second tick scrolls with the new lines laid out
            index = int(position // 5)
            assert [n for n, line in enumerate(lyrics_lines(app)) if line.startswith("*")] == [index]
            line = view.lines[index]
            middle = view.region.y + view.region.height // 2
            assert abs(line.region.y - middle) <= 1
        await tick(pilot, client, None)
        assert not any(line.startswith("*") for line in lyrics_lines(app))


@drive
async def test_show_lyrics_off_stops_the_look_up_and_turning_it_on_brings_it_back(clients, served, find_lyrics):
    settings.save(settings.Settings(show_lyrics=False))
    find_lyrics.answers = {"s1": SYNCED}
    app = make_app(clients, resolve=lambda text, limit, source: SONGS)
    async with run(app) as pilot:
        await play_song(pilot)
        await show_lyrics_tab(pilot)
        assert lyrics_message(app) == "Lyrics are off (show_lyrics)"
        assert find_lyrics.calls == []
        app.change_setting("show_lyrics", True)  # what Enter on its Settings row does
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert find_lyrics.calls == ["s1"]
        assert lyrics_message(app) is None
        app.change_setting("show_lyrics", False)
        await pilot.pause()
        assert lyrics_message(app) == "Lyrics are off (show_lyrics)"
        assert lyrics_lines(app) == []
        assert settings.load().show_lyrics is False


@drive
async def test_enter_on_show_lyrics_in_the_settings_screen_turns_the_open_tab_off(clients, served, find_lyrics):
    find_lyrics.answers = {"s1": SYNCED}
    app = make_app(clients, resolve=lambda text, limit, source: SONGS)
    async with run(app) as pilot:
        await play_song(pilot)
        await show_lyrics_tab(pilot)
        await pilot.press("S")
        await pilot.pause()
        app.screen.query_one(DataTable).move_cursor(row=settings.KEYS.index("show_lyrics"))
        await pilot.press("enter")
        await tick(pilot, clients.made[0], 12)  # a status while the modal is on top
        assert settings.load().show_lyrics is False
        assert lyrics_message(app) == "Lyrics are off (show_lyrics)"


@drive
async def test_a_new_player_follows_the_prefetch_setting(clients, served):
    app = make_app_with(clients, settings.Settings(prefetch=False))
    async with run(app) as pilot:
        await search(pilot)
        await pilot.press("enter")
        await pilot.pause()
        assert clients.made[0].prefetch_asked is False
