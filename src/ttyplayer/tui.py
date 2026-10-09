"""A full-screen ttyplayer: search bar, tabs with a results table, a now-playing panel, help, command palette."""

import contextlib
import dataclasses
import datetime
import functools
import io
import random
import threading

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.command import DiscoveryHit, Hit, Provider
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
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
    TabPane,
)
from textual.worker import get_current_worker

from ttyplayer import control, favorites, history, player, playlists, youtube
from ttyplayer import settings as config
from ttyplayer.utils import APP_NAME, format_time, unseen

HISTORY_LIMIT = 50
FAVORITE_MARK = " ♥"
IDLE_TEXT = "Nothing playing — press / to search"
LONG_SEEK_SECONDS = 30
VIDEO_COLUMNS = ("Title", "Uploader", "Length")
PLAYLIST_COLUMNS = ("Name", "Tracks", "Length")
NEW_PLAYLIST = "New playlist…"  # … is not allowed in a name, so it can never be a playlist's
shuffler = random.Random()  # s on an open playlist; tests swap in a seeded one

# The command palette's entries and the help modal's Commands section: (name, app action, help).
COMMANDS = [
    ("Search…", "focus_search", "Focus the search box"),
    ("Playlists", "playlists", "Show your playlists"),
    ("Save queue as playlist…", "save_queue", "Save the queue as a playlist"),
    ("Next theme", "next_theme", "Switch to the next color theme"),
    ("Settings…", "settings", "Show and change the settings"),
    ("Help", "help", "Every key and command"),
    ("Quit", "quit", "Quit and stop mpv"),
    ("Pause / resume", "toggle_pause", "Pause or resume playback"),
    ("Next", "next", "Play the next track in the queue"),
    ("Previous", "prev", "Play the previous track in the queue"),
    ("Mute", "toggle_mute", "Mute or unmute"),
]


def resolve(text, limit):
    """A link's videos, or the first `limit` search results for words."""
    if youtube.is_url(text):
        return youtube.fetch(text)
    return youtube.search(text, limit)


def number_cell(number, playing):
    return f"{'▸' if playing else ' '}{number}"


def video_cells(video, number, playing=False, favorite=False):
    """One table row in column order: #, Title (♥ when a favorite), Uploader, Length."""
    title = video.title + FAVORITE_MARK if favorite else video.title
    return number_cell(number, playing), Text(title), Text(video.uploader), format_time(video.duration)


def tab_label(title, count):
    return f"{title} ({count})" if count else title


def total_length(videos):
    """The sum of the known durations."""
    return sum(video.duration for video in videos if video.duration is not None)


def saved_playlists():
    """(name, videos) of every playlist; a file whose name is not a playlist name is left out."""
    listed = []
    for name in playlists.names():
        with contextlib.suppress(playlists.PlaylistError):
            listed.append((name, playlists.load(name)))
    return listed


def queue_text(status):
    if status["total"] <= 1:
        return ""
    up_next = status["up_next"]
    return f"Up next: {up_next}" if up_next else "End of queue"


def timing_text(status):
    started_in = status.get("started_in")
    if player.timing() and started_in is not None:
        return f"started in {started_in:.1f}s"
    return ""


class NowPlaying(Vertical):
    """Three lines built from MpvClient.status(); show() is the only writer."""

    BORDER_TITLE = "Now playing"

    def compose(self) -> ComposeResult:
        yield Static(IDLE_TEXT, id="np-idle", markup=False)
        with Horizontal(classes="np-line"):
            yield Static(id="np-state")
            yield Static(id="np-title", markup=False)
            yield Static(id="np-uploader", markup=False)
            yield Static(id="np-position")
        with Horizontal(classes="np-line"):
            yield ProgressBar(id="np-progress", show_percentage=False, show_eta=False)
            yield Static(id="np-time")
        with Horizontal(classes="np-line"):
            yield Static(id="np-volume")
            yield Static(id="np-next", markup=False)
            yield Static(id="np-timing")

    def on_mount(self):
        self.show(None)

    def show(self, status):
        idle = status is None or status["idle"]
        self.set_class(idle, "-idle")
        if idle:
            return
        self.query_one("#np-state", Static).update("⏸" if status["paused"] else "▶")
        self.query_one("#np-title", Static).update(status["title"])
        self.query_one("#np-uploader", Static).update(f" · {status['uploader']}" if status.get("uploader") else "")
        position = f"[{status['index']}/{status['total']}]" if status["total"] > 1 else ""
        self.query_one("#np-position", Static).update(position)
        self.query_one(ProgressBar).update(total=status["duration"], progress=status["position"] or 0)
        self.query_one("#np-time", Static).update(
            f"{format_time(status['position'])} / {format_time(status['duration'])}"
        )
        self.query_one("#np-volume", Static).update(player.volume_meter(status["volume"], status["muted"]))
        self.query_one("#np-next", Static).update(queue_text(status))
        self.query_one("#np-timing", Static).update(timing_text(status))


class SearchBox(Input):
    BINDINGS = [Binding("escape", "app.focus_results", "Back to the table", show=False)]


class VideoTable(DataTable):
    """A table of videos in the design's columns, with the playback keys."""

    BINDINGS = [
        Binding("space", "app.toggle_pause", "Pause"),
        Binding("n", "app.next", "Next"),
        Binding("p", "app.prev", "Prev"),
        Binding("comma", f"app.seek({-player.SEEK_SECONDS})", f"Seek −{player.SEEK_SECONDS}s", show=False),
        Binding("full_stop", f"app.seek({player.SEEK_SECONDS})", f"Seek +{player.SEEK_SECONDS}s", show=False),
        Binding("less_than_sign", f"app.seek({-LONG_SEEK_SECONDS})", f"Seek −{LONG_SEEK_SECONDS}s", show=False),
        Binding("greater_than_sign", f"app.seek({LONG_SEEK_SECONDS})", f"Seek +{LONG_SEEK_SECONDS}s", show=False),
        Binding("minus", f"app.change_volume({-player.VOLUME_STEP})", f"Volume −{player.VOLUME_STEP}", show=False),
        Binding("plus", f"app.change_volume({player.VOLUME_STEP})", f"Volume +{player.VOLUME_STEP}", show=False),
        Binding("M", "app.toggle_mute", "Mute", show=False),
        Binding("f", "app.favorite", "Fav"),
        Binding("A", "app.add_to_playlist", "To playlist"),
    ]

    def __init__(self, **kwargs):
        super().__init__(zebra_stripes=True, cursor_type="row", **kwargs)
        self.labels = None

    def on_mount(self):
        self.set_columns(VIDEO_COLUMNS)

    def set_columns(self, labels):
        """The # column, then labels; the table is rebuilt only when they change."""
        if labels == self.labels:
            return
        self.labels = labels
        self.clear(columns=True)
        self.add_column("#", key="number")
        self.add_columns(*labels)

    def fill(self, rows):
        """Replace every row with rows (cells in column order), the cursor kept in place."""
        row = self.cursor_row
        self.clear()
        for cells in rows:
            self.add_row(*cells)
        self.move_cursor(row=min(row, self.row_count - 1))


class VideoList(VideoTable):
    """A table whose rows are videos: videos holds them in row order, picked() is the cursor's."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.videos = []

    def show(self, videos, playing, favorite_ids):
        self.videos = list(videos)
        rows = [
            video_cells(video, number, video == playing, video.id in favorite_ids)
            for number, video in enumerate(self.videos, start=1)
        ]
        self.fill(rows)

    def picked(self):
        return self.videos[self.cursor_row] if self.videos else None


class PickTable(VideoList):
    """Videos to pick from (Search, History, Favorites)."""

    BINDINGS = [
        Binding("enter", "select_cursor", "Play from here", show=False),
        Binding("a", "app.append", "Add"),
    ]


class ResultsTable(PickTable):
    BINDINGS = [Binding("m", "app.more", "More")]


class HistoryTable(PickTable):
    pass


class FavoritesTable(PickTable):
    BINDINGS = [Binding("d", "app.unfavorite", "Unfav")]


class QueueTable(VideoTable):
    BINDINGS = [
        Binding("enter", "app.jump", "Jump here", show=False),
        Binding("d", "app.remove", "Remove"),
        Binding("K,shift+up", "app.move_in_queue(-1)", "Move up"),
        Binding("J,shift+down", "app.move_in_queue(1)", "Move down"),
        Binding("c", "app.clear_queue", "Clear"),
    ]


class PlaylistTable(VideoList):
    """Tab 5: every playlist (names), or the tracks of the one opened, in the same table."""

    BINDINGS = [
        Binding("enter", "select_cursor", "Open / play from here", show=False),
        Binding("a", "app.append", "Add"),
        Binding("d", "app.remove_from_playlist", "Remove"),
        Binding("K,shift+up", "app.move_in_playlist(-1)", "Move up"),
        Binding("J,shift+down", "app.move_in_playlist(1)", "Move down"),
        Binding("s", "app.shuffle_playlist", "Shuffle"),
        Binding("escape,backspace", "app.close_playlist", "Back to the playlists", show=False),
    ]

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.opened = None
        self.names = []

    def show_names(self, listed):
        """One row per (name, videos): #, Name, Tracks, Length."""
        self.opened, self.videos = None, []
        self.names = [name for name, _ in listed]
        self.set_columns(PLAYLIST_COLUMNS)
        self.fill(
            (number_cell(number, False), Text(name), str(len(videos)), format_time(total_length(videos)))
            for number, (name, videos) in enumerate(listed, start=1)
        )

    def show_tracks(self, name, videos, playing, favorite_ids):
        self.opened, self.names = name, []
        self.set_columns(VIDEO_COLUMNS)
        self.show(videos, playing, favorite_ids)


class TtyplayerCommands(Provider):
    """The command palette's ttyplayer entries, from COMMANDS; each runs the action its key runs."""

    def run(self, action):
        return functools.partial(self.app.run_action, action)

    async def discover(self):
        for name, action, help in COMMANDS:
            yield DiscoveryHit(name, self.run(action), help=help)

    async def search(self, query):
        matcher = self.matcher(query)
        for name, action, help in COMMANDS:
            score = matcher.match(name)
            if score > 0:
                yield Hit(score, matcher.highlight(name), self.run(action), help=help)


class HelpScreen(ModalScreen):
    """Every key and command, generated from the BINDINGS lists and COMMANDS so it can never drift from them."""

    BINDINGS = [
        Binding("escape", "dismiss", "Close", show=False),
        Binding("question_mark", "dismiss", "Close", show=False),
    ]

    def __init__(self, sections):
        super().__init__()
        self.sections = sections

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="help") as help:
            help.border_title = "Keys"
            for heading, bindings in self.sections:
                yield Static(heading, classes="help-heading")
                for binding in bindings:
                    yield Static(f"{self.app.get_key_display(binding):>8}  {binding.description}", markup=False)
            yield Static("Commands (Ctrl-P)", classes="help-heading")
            for name, _, help in COMMANDS:
                yield Static(f"{name}  {help}", markup=False)


class NameScreen(ModalScreen):
    """Asks for a playlist name: Enter runs save(name), whose PlaylistError stays on screen until a name works."""

    BINDINGS = [Binding("escape", "dismiss", "Cancel", show=False)]

    def __init__(self, title, name, save):
        super().__init__()
        self.title_text = title
        self.prefill = name
        self.save = save

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog") as box:
            box.border_title = self.title_text
            yield Input(value=self.prefill, placeholder=playlists.NAME_RULE)
            yield Static(id="name-error", markup=False)

    @on(Input.Submitted)
    def submit(self, event: Input.Submitted):
        event.stop()  # not a search
        name = event.value.strip()
        try:
            self.save(name)
        except playlists.PlaylistError as error:
            self.query_one("#name-error", Static).update(str(error))
            return
        self.dismiss(name)


class PlaylistPicker(ModalScreen):
    """Which playlist: one option per playlist, then New playlist…; Enter picks it, Esc cancels."""

    BINDINGS = [Binding("escape", "dismiss", "Cancel", show=False)]

    def __init__(self, names):
        super().__init__()
        self.choices = [*names, NEW_PLAYLIST]

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog") as box:
            box.border_title = "Add to playlist"
            yield OptionList(*(Text(choice) for choice in self.choices))

    @on(OptionList.OptionSelected)
    def pick(self, event: OptionList.OptionSelected):
        self.dismiss(self.choices[event.option_index])


class ConfirmScreen(ModalScreen):
    """A yes / no question: y or Enter answers True, Esc False."""

    BINDINGS = [
        Binding("y,enter", "dismiss(True)", "Yes", show=False),
        Binding("escape", "dismiss(False)", "No", show=False),
    ]

    def __init__(self, question):
        super().__init__()
        self.question = question

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Static(self.question, markup=False)
            yield Static("y or Enter: yes · Esc: no", classes="dialog-hint")


class SettingsScreen(ModalScreen):
    """Every setting with its value and default, one row per Settings field; Enter flips a true/false one."""

    BINDINGS = [Binding("escape", "dismiss", "Close", show=False)]

    def compose(self) -> ComposeResult:
        with Vertical(id="settings") as box:
            box.border_title = "Settings"
            yield DataTable(cursor_type="row")
            yield Static(id="settings-hint", markup=False)

    def on_mount(self):
        table = self.query_one(DataTable)
        table.add_columns("Setting", "Value", "Default")
        self.show()
        table.focus()

    def show(self):
        """One row per setting; the file's text can be anything, so the cells are plain Text."""
        table = self.query_one(DataTable)
        row = table.cursor_row
        table.clear()
        for key in config.KEYS:
            value, default = (config.display(getattr(each, key)) for each in (self.app.settings, config.DEFAULTS))
            table.add_row(Text(key), Text(value), Text(f"(default {default})"))
        table.move_cursor(row=row)

    @on(DataTable.RowSelected)
    def change(self, event: DataTable.RowSelected):
        key = config.KEYS[event.cursor_row]
        value = getattr(self.app.settings, key)
        if isinstance(value, bool):
            self.app.change_setting(key, not value)
            self.show()
        else:
            self.query_one("#settings-hint", Static).update(f"set with: ttyplayer config set {key} <value>")


class TtyplayerApp(App):
    TITLE = APP_NAME
    CSS_PATH = "tui.tcss"
    # Textual's own (themes, keys, quit, …) plus ttyplayer's.
    COMMANDS = App.COMMANDS | {TtyplayerCommands}
    BINDINGS = [
        Binding("slash", "focus_search", "Search"),
        Binding("question_mark", "help", "Help"),
        Binding("1", "show_tab('search')", "Search tab", show=False),
        Binding("2", "show_tab('queue')", "Queue tab", show=False),
        Binding("3", "show_tab('history')", "History tab", show=False),
        Binding("4", "show_tab('favorites')", "Favorites tab", show=False),
        Binding("5", "playlists", "Playlists tab", show=False),
        Binding("P", "save_queue", "Save queue"),
        Binding("t", "next_theme", "Next theme", show=False),
        Binding("S", "settings", "Settings"),
        # Not a priority binding: the search box must be able to take a typed q.
        Binding("q", "quit", "Quit"),
        Binding("ctrl+c", "quit", "Quit", show=False, priority=True),
    ]

    def __init__(self, client_factory=player.MpvClient, resolve=resolve, video=False, settings=None):
        super().__init__()
        self.settings = settings if settings is not None else config.load()
        self.client_factory = client_factory
        self.resolve = resolve
        self.video = video
        self.searched = None  # the text behind the Search tab's rows, for m
        self.favorite_ids = set()
        self.history_stale = False
        self.queue_shown = ([], None)
        self.player_error = None  # the player's last error, toasted once
        self.client = None
        self.remote = None
        self.closing = False

    def compose(self) -> ComposeResult:
        yield Header(show_clock=self.settings.show_clock)
        with Horizontal(id="search-row"):
            yield SearchBox(placeholder="Search YouTube or paste a link…")
            yield LoadingIndicator()
        with TabbedContent():
            with TabPane("Search", id="search"):
                yield ResultsTable()
            with TabPane("Queue", id="queue"):
                yield QueueTable()
            with TabPane("History", id="history"):
                yield HistoryTable()
            with TabPane("Favorites", id="favorites"):
                yield FavoritesTable()
            with TabPane("Playlists", id="playlists"):
                yield PlaylistTable()
        yield NowPlaying()
        yield Footer()

    def on_mount(self):
        self.app_thread = threading.get_ident()
        self.apply_theme()
        self.set_searching(False)
        self.show_favorites()  # first: the other tables read favorite_ids for their ♥
        self.show_history()
        self.show_playlists()
        self.query_one(SearchBox).focus()

    def on_unmount(self):
        self.shut_down()

    def toast(self, message, severity):
        # Messages carry titles and yt-dlp errors, whose [brackets] are not markup.
        self.notify(message, severity=severity, markup=False)

    # --- search ---------------------------------------------------------

    def set_searching(self, searching):
        self.query_one(LoadingIndicator).display = searching

    def on_input_submitted(self, event: Input.Submitted):
        text = event.value.strip()
        if text:
            self.set_searching(True)
            self.lookup(text)

    def action_more(self):
        """The next batch of the last search, after the rows already shown."""
        if self.searched is None:
            return
        self.set_searching(True)
        self.lookup(self.searched, self.query_one(ResultsTable).videos)

    @work(thread=True, exclusive=True)
    def lookup(self, text, shown=None):
        """Search for text; with shown, for the results after those (as the CLI's m does)."""
        videos, error = None, None
        try:
            limit = self.settings.search_limit
            if shown is None:
                videos = self.resolve(text, limit)
            else:
                videos = unseen(self.resolve(text, len(shown) + limit), shown)
        except youtube.YouTubeError as caught:
            error = caught
        if not get_current_worker().is_cancelled:  # a newer search replaced this one
            self.call_from_thread(self.search_done, text, videos, error, shown)

    def search_done(self, text, videos, error, shown):
        self.set_searching(False)
        if error is not None:
            self.toast(f"YouTube lookup failed: {error}", severity="error")
        elif not videos:
            self.toast("No more results" if shown else "No videos found", severity="warning")
        elif shown:
            self.query_one(ResultsTable).show(shown + videos, self.playing_video(), self.favorite_ids)
        else:
            self.searched = text
            self.show_results(videos)

    def show_results(self, videos):
        table = self.query_one(ResultsTable)
        table.show(videos, self.playing_video(), self.favorite_ids)
        table.move_cursor(row=0)
        table.focus()

    # --- playing --------------------------------------------------------

    @on(DataTable.RowSelected, "PickTable")
    def play_from_row(self, event: DataTable.RowSelected):
        self.play_queue(event.data_table.videos[event.cursor_row :])

    def play_queue(self, videos, start=0):
        """The queue becomes videos, playing videos[start]."""
        client = self.ensure_client()
        if client is None:
            return
        client.queue.clear()
        for video in videos:
            client.add(video)
        client.index = start
        client.play_current()

    def picked(self):
        """The video under the cursor of the focused Search/History/Favorites/playlist table, or None."""
        table = self.focused
        return table.picked() if isinstance(table, VideoList) else None

    def action_append(self):
        video = self.picked()
        if video is None:
            return
        client = self.ensure_client()
        if client is None:
            return
        was_empty = not client.queue
        client.add(video)
        if was_empty:
            client.play_current()
        else:
            client.notify()  # the Queue tab and the panel's [i/n] follow

    def ensure_client(self):
        """The player, created (with remote control) on first use; None and a toast if mpv fails."""
        if self.client is not None:
            return self.client
        try:
            self.client = self.client_factory(self.video, on_play=self.on_play, on_state=self.on_player_state)
        except FileNotFoundError:
            self.toast("mpv is not installed. Install it with: brew install mpv", severity="error")
            return None
        except RuntimeError as error:
            self.toast(str(error), severity="error")
            return None
        self.start_remote()
        return self.client

    def start_remote(self):
        # Textual owns the terminal, so serve()'s stderr warning becomes a toast.
        warning = io.StringIO()
        with contextlib.redirect_stderr(warning):
            self.remote = control.serve(self.client.handle_control)
        if self.remote is None:
            self.toast(warning.getvalue().strip(), severity="warning")

    def on_player_state(self, status):
        """on_state for the player: runs on its listener thread, or on ours via play_current()."""
        if self.closing:
            return
        if threading.get_ident() == self.app_thread:
            self.show_status(status)
            return
        try:
            self.call_from_thread(self.show_status, status)
        except RuntimeError:
            pass  # the app stopped while this status was on its way

    def on_play(self, video):
        """on_play for the player: the CLI's history.record; the History tab follows on the next on_state.

        It runs under the player's queue_lock, so it must not wait for the app thread.
        """
        history.record(video)
        self.history_stale = True

    def show_status(self, status):
        if self.history_stale:
            self.history_stale = False
            self.show_history()
        self.query_one(NowPlaying).show(status)
        self.mark_playing()
        self.show_queue()
        self.toast_player_error(status and status.get("error"))

    def toast_player_error(self, error):
        if error and error != self.player_error:
            self.toast(error, severity="error")
        self.player_error = error

    def playing_video(self):
        client = self.client
        if client is None or client.idle or not 0 <= client.index < len(client.queue):
            return None
        return client.queue[client.index]

    def mark_playing(self):
        """▸ in the # cell of the Search/History/Favorites row being played, a blank everywhere else."""
        playing = self.playing_video()
        for table in self.query(VideoList):
            for number, (row_key, video) in enumerate(zip(table.rows, table.videos), start=1):
                table.update_cell(row_key, "number", number_cell(number, video == playing))

    # --- history and favorites ------------------------------------------

    @on(TabbedContent.TabActivated)
    def refresh_library_tab(self, event: TabbedContent.TabActivated):
        if event.pane.id == "history":
            self.show_history()
        elif event.pane.id == "favorites":
            self.show_favorites()
        elif event.pane.id == "playlists":
            self.show_playlists()

    def show_library(self, table_type, pane, title, videos):
        self.query_one(table_type).show(videos, self.playing_video(), self.favorite_ids)
        self.query_one(TabbedContent).get_tab(pane).label = tab_label(title, len(videos))

    def show_history(self):
        self.show_library(HistoryTable, "history", "History", history.load(limit=HISTORY_LIMIT))

    def show_favorites(self):
        self.favorite_ids = favorites.ids()
        self.show_library(FavoritesTable, "favorites", "Favorites", favorites.load())

    def action_favorite(self):
        """Toggle the row under the cursor in favorites, or the track playing when there is none."""
        video = self.picked() or self.playing_video()
        if video is None:
            return
        if video.id in self.favorite_ids:
            self.unfavorite(video)
        else:
            favorites.add(video)
            self.toast(f"Favorited: {video.title}", severity="information")
            self.favorites_changed()

    def action_unfavorite(self):
        video = self.picked()
        if video is not None:
            self.unfavorite(video)

    def unfavorite(self, video):
        favorites.remove_id(video.id)
        self.toast(f"Removed from favorites: {video.title}", severity="information")
        self.favorites_changed()

    def favorites_changed(self):
        """The Favorites tab and every table's ♥ follow the file."""
        self.show_favorites()
        playing = self.playing_video()
        for table in (self.query_one(ResultsTable), self.query_one(HistoryTable)):
            table.show(table.videos, playing, self.favorite_ids)
        self.queue_shown = None  # the Queue tab redraws its titles too
        self.show_queue()
        self.show_playlists()

    # --- queue --------------------------------------------------------

    def show_queue(self):
        """Rebuild the Queue tab from client.queue / client.index (no ▸ when idle), the cursor kept in place.

        Skipped when neither changed: on_state also fires for every time-pos tick.
        """
        videos, current = self.queue_snapshot()
        if (videos, current) == self.queue_shown:
            return
        self.queue_shown = (videos, current)
        self.query_one(QueueTable).fill(
            video_cells(video, index + 1, index == current, video.id in self.favorite_ids)
            for index, video in enumerate(videos)
        )
        self.query_one(TabbedContent).get_tab("queue").label = tab_label("Queue", len(videos))

    def queue_snapshot(self):
        """(a copy of client.queue, the current index or None when idle); ([], None) before a player."""
        if self.client is None:
            return [], None
        with self.client.queue_lock:
            return list(self.client.queue), None if self.client.idle else self.client.index

    def action_jump(self):
        if self.client:
            self.client.jump(self.query_one(QueueTable).cursor_row)

    def action_remove(self):
        if self.client:
            self.client.remove(self.query_one(QueueTable).cursor_row)

    def action_move_in_queue(self, step):
        table = self.query_one(QueueTable)
        row = table.cursor_row
        if self.client and self.client.move(row, row + step):
            table.move_cursor(row=row + step)

    def action_clear_queue(self):
        if self.client and self.client.clear_others():
            self.toast("Queue cleared", severity="information")

    # --- playlists ------------------------------------------------------

    def show_playlists(self):
        """Tab 5 from the files: the open playlist's tracks, or every playlist with its count and length."""
        table = self.query_one(PlaylistTable)
        if table.opened is not None:
            try:
                videos = playlists.load(table.opened)
            except playlists.PlaylistError:  # deleted from another terminal
                table.opened = None
            else:
                table.show_tracks(table.opened, videos, self.playing_video(), self.favorite_ids)
                label = tab_label(f"Playlists › {table.opened}", len(videos))
        if table.opened is None:
            listed = saved_playlists()
            table.show_names(listed)
            label = tab_label("Playlists", len(listed))
        self.query_one(TabbedContent).get_tab("playlists").label = label

    def change_playlist(self, done, change, *args):
        """Run a playlists.* change, toast done(its result) unless done is None, redraw tab 5.

        True when it worked; a PlaylistError (say, the playlist was deleted from another terminal) is an error toast.
        """
        try:
            result = change(*args)
        except playlists.PlaylistError as error:
            self.toast(str(error), severity="error")
            return False
        finally:
            self.show_playlists()
        if done is not None:
            self.toast(done(result), severity="information")
        return True

    @on(DataTable.RowSelected, "PlaylistTable")
    def open_or_play(self, event: DataTable.RowSelected):
        """Enter on a playlist opens it; on a track, the whole playlist plays from there."""
        table = event.data_table
        if table.videos:
            self.play_queue(table.videos, event.cursor_row)
        elif table.names:
            self.open_playlist(table.names[event.cursor_row])

    def open_playlist(self, name):
        table = self.query_one(PlaylistTable)
        table.opened = name
        self.show_playlists()
        table.move_cursor(row=0)

    def action_close_playlist(self):
        table = self.query_one(PlaylistTable)
        name = table.opened
        if name is None:
            return
        table.opened = None
        self.show_playlists()
        if name in table.names:
            table.move_cursor(row=table.names.index(name))

    def action_remove_from_playlist(self):
        """d: the track under the cursor leaves the open playlist; on the list, the playlist is deleted after a yes."""
        table = self.query_one(PlaylistTable)
        row = table.cursor_row
        name = table.opened
        if table.videos:
            self.change_playlist(lambda removed: f"Removed from {name}: {removed.title}", playlists.remove, name, row + 1)
        elif table.names:
            name = table.names[row]
            self.push_screen(ConfirmScreen(f"Delete playlist {name}?"), lambda yes: yes and self.delete_playlist(name))

    def delete_playlist(self, name):
        self.change_playlist(lambda _: f"Deleted playlist {name}", playlists.delete, name)

    def action_move_in_playlist(self, step):
        table = self.query_one(PlaylistTable)
        row = table.cursor_row
        if not 0 <= row + step < len(table.videos):
            return
        if self.change_playlist(None, playlists.move, table.opened, row + 1, row + 1 + step):
            table.move_cursor(row=row + step)

    def action_shuffle_playlist(self):
        videos = list(self.query_one(PlaylistTable).videos)
        if videos:
            shuffler.shuffle(videos)
            self.play_queue(videos)

    def action_playlists(self):
        self.action_show_tab("playlists")

    def action_save_queue(self):
        """P: the queue saved under a name the user confirms, replacing a playlist of that name."""
        if self.screen is not self.screen_stack[0]:
            return
        videos, current = self.queue_snapshot()
        if current is None:  # no player, or idle: a queue played to its end is not saved
            self.toast("Nothing to save", severity="warning")
            return
        name = playlists.sanitize(videos[0].title) if len(videos) == 1 else f"Queue {datetime.date.today()}"

        def saved(name):
            if name is not None:
                self.toast(f"Saved {len(videos)} tracks to {name}", severity="information")
                self.show_playlists()

        self.push_screen(NameScreen("Save queue as playlist", name, lambda name: playlists.replace(name, videos)), saved)

    def row_video(self):
        """The video under the cursor of the focused table, the Queue tab's included; None without one."""
        table = self.focused
        if isinstance(table, QueueTable):
            videos = self.queue_shown[0]
            return videos[table.cursor_row] if videos else None
        return self.picked()

    def action_add_to_playlist(self):
        """A: the row's video appended to a playlist picked from a list, or to a new one."""
        video = self.row_video()
        if video is None:
            return

        def picked(name):
            if name == NEW_PLAYLIST:
                self.push_screen(NameScreen("New playlist", "", playlists.create), picked)
            elif name is not None:
                self.change_playlist(lambda _: f"Added to {name}", playlists.add, name, [video])

        self.push_screen(PlaylistPicker(playlists.names()), picked)

    # --- keys -----------------------------------------------------------

    def action_toggle_pause(self):
        if self.client:
            self.client.toggle_pause()

    def action_next(self):
        if self.client:
            self.client.next()

    def action_prev(self):
        if self.client:
            self.client.prev()

    def action_seek(self, seconds):
        if self.client:
            self.client.seek(seconds)

    def action_change_volume(self, step):
        if self.client:
            self.client.change_volume(step)

    def action_toggle_mute(self):
        if self.client:
            self.client.toggle_mute()

    def action_next_theme(self):
        themes = sorted(self.available_themes)
        self.theme = themes[(themes.index(self.theme) + 1) % len(themes)]
        self.toast(f"Theme: {self.theme}", severity="information")
        self.change_setting("theme", self.theme)

    # --- settings -------------------------------------------------------

    def apply_theme(self):
        if self.settings.theme in self.available_themes:
            self.theme = self.settings.theme
            return
        self.theme = config.DEFAULTS.theme
        unknown = f'Unknown theme "{self.settings.theme}" in settings, using {config.DEFAULTS.theme}'
        self.toast(unknown, severity="warning")

    def change_setting(self, key, value):
        """Set key to value for this app and in the file; the clock shows or hides at once."""
        self.settings = dataclasses.replace(self.settings, **{key: value})
        try:
            config.change(key, value)
        except config.SettingsError as error:
            self.toast(f"Could not save settings: {error}", severity="error")
        if key == "show_clock":
            self.show_clock()

    def show_clock(self):
        """Header's show_clock is fixed when it is built, so a new Header replaces the old one."""
        main = self.screen_stack[0]
        main.query_one(Header).remove()
        main.mount(Header(show_clock=self.settings.show_clock), before=0)

    def action_settings(self):
        if not isinstance(self.screen, SettingsScreen):
            self.push_screen(SettingsScreen())

    def action_focus_search(self):
        self.query_one(SearchBox).focus()

    def action_focus_results(self):
        pane = self.query_one(TabbedContent).active_pane
        if pane is not None:
            pane.query_one(DataTable).focus()

    def action_show_tab(self, tab):
        self.query_one(TabbedContent).active = tab
        self.action_focus_results()

    def action_help(self):
        self.push_screen(
            HelpScreen(
                [
                    ("Anywhere", self.BINDINGS),
                    ("Search box", SearchBox.BINDINGS),
                    ("Tables", VideoTable.BINDINGS),
                    ("Search, History, Favorites tables", PickTable.BINDINGS),
                    ("Search table", ResultsTable.BINDINGS),
                    ("Queue table", QueueTable.BINDINGS),
                    ("Favorites table", FavoritesTable.BINDINGS),
                    ("Playlists table", PlaylistTable.BINDINGS),
                ]
            )
        )

    # --- quitting -------------------------------------------------------

    def shut_down(self):
        """Stop remote control, then mpv. Runs once, however the app exits."""
        if self.closing:
            return
        self.closing = True
        if self.remote:
            self.remote.stop()
        if self.client:
            self.client.quit()
