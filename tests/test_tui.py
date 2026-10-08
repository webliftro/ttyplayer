import asyncio
import functools
import importlib.resources
import pathlib
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
    ProgressBar,
    Static,
    TabbedContent,
)

from ttyplayer import control, favorites, history, player, tui, youtube
from ttyplayer.models import Video

VIDEOS = [
    Video(id="a", title="Alpha", uploader="Ann", duration=61),
    Video(id="b", title="Beta", uploader="Bob", duration=None),
    Video(id="c", title="Gamma", uploader="Gus", duration=3600),
]


class FakeClient(player.MpvClient):
    """Stands in for MpvClient: the real queue edits and status(), never mpv; records the calls."""

    def __init__(self, video=False, on_play=None, on_state=None):
        self.video = video
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

    def send(self, command):
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


@pytest.fixture
def served(monkeypatch):
    """control.serve replaced by a fake; the handlers it was given are collected here."""
    handlers = []
    monkeypatch.setattr(control, "serve", lambda handler: handlers.append(handler) or FakeRemote([]))
    return handlers


@pytest.fixture
def clients():
    made = []

    def factory(video=False, on_play=None, on_state=None):
        made.append(FakeClient(video, on_play, on_state))
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


def make_app(clients, resolve=lambda text: VIDEOS, video=False):
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
            "index": 1, "total": 1, "volume": 60, "volume_source": "player", "muted": False, "up_next": None, "started_in": None, "idle": False}
    return base | fields


# --- AC1: layout --------------------------------------------------------


@drive
async def test_layout_header_search_tabs_panel_footer(clients, served):
    app = make_app(clients)
    async with run(app) as pilot:
        await pilot.pause()
        assert app.query_one(Header).query("HeaderClock")
        search_box = app.query_one(Input)
        assert search_box.placeholder == "Search YouTube or paste a link…"
        assert app.focused is search_box
        assert search_box.parent is app.query_one(LoadingIndicator).parent
        tabs = app.query_one(TabbedContent)
        assert [str(tabs.get_tab(pane).label) for pane in tabs.query("TabPane")] == [
            "Search", "Queue", "History", "Favorites"
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
    monkeypatch.setattr(youtube, "search", lambda query, limit: [("searched", query, limit)])
    assert tui.resolve("https://youtu.be/x") == [("fetched", "https://youtu.be/x")]
    assert tui.resolve("lofi beats") == [("searched", "lofi beats", 10)]


@drive
async def test_search_fills_the_table_and_focuses_it(clients, served):
    asked = []
    app = make_app(clients, resolve=lambda text: asked.append(text) or VIDEOS)
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
    app = make_app(clients, resolve=lambda text: answers.pop(0))
    async with run(app) as pilot:
        await search(pilot, "first")
        await pilot.press("down", "down")
        await search(pilot, "second")
        assert rows(app) == [[" 1", "Gamma", "Gus", "60:00"]]
        assert table(app).cursor_row == 0


@drive
async def test_titles_are_shown_as_typed_not_as_markup(clients, served):
    odd = Video(id="x", title="[bold]Live[/bold] [x]", uploader="[DJ]", duration=1)
    app = make_app(clients, resolve=lambda text: [odd])
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
        for key, pane in [("2", "queue"), ("3", "history"), ("4", "favorites"), ("1", "search")]:
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

    def slow(text):
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
    app = make_app(clients, resolve=lambda text: answers.pop(0))
    async with run(app) as pilot:
        await search(pilot, "first")
        await search(pilot, "nothing")
        assert toasts(app) == [("No videos found", "warning")]
        assert table(app).row_count == 3
        assert app.query_one(LoadingIndicator).display is False


@drive
async def test_youtube_error_is_an_error_toast_and_keeps_the_table(clients, served):
    def failing(text):
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
                *tui.VideoTable.BINDINGS, *tui.PickTable.BINDINGS, *tui.ResultsTable.BINDINGS,
                *tui.QueueTable.BINDINGS, *tui.FavoritesTable.BINDINGS,
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
            for binding in tui.VideoTable.BINDINGS + tui.ResultsTable.BINDINGS + tui.TtyplayerApp.BINDINGS
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

    def resolve(text, limit=tui.SEARCH_LIMIT):
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
        assert resolve.asked == [("lofi", tui.SEARCH_LIMIT), ("lofi", 3 + tui.SEARCH_LIMIT)]
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

    def slow(text, limit=tui.SEARCH_LIMIT):
        if limit != tui.SEARCH_LIMIT:
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
        assert resolve.asked == [("lofi", tui.SEARCH_LIMIT)]
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
async def test_panel_meter_follows_the_device_volume(clients, served):
    app = make_app(clients)
    async with run(app) as pilot:
        app.on_player_state(status(volume=30.0, volume_source="device"))
        await pilot.pause()
        assert text(app, "#np-volume") == "🔊 ▮▮▮▯▯▯▯▯▯▯ 30%"
        app.on_player_state(status(volume=75.0, volume_source="device"))
        await pilot.pause()
        assert text(app, "#np-volume") == "🔊 ▮▮▮▮▮▮▮▮▯▯ 75%"


@drive
async def test_palette_provider_offers_every_command_and_runs_its_action(clients, served, monkeypatch):
    app = make_app(clients)
    async with run(app) as pilot:
        provider = tui.TtyplayerCommands(app.screen)
        hits = [hit async for hit in provider.discover()]
        assert [hit.text for hit in hits] == [
            "Search…", "Next theme", "Help", "Quit", "Pause / resume", "Next", "Previous", "Mute"
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
        tui.ResultsTable, tui.FavoritesTable, tui.QueueTable,
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
