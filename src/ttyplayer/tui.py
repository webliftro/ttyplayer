"""A full-screen ttyplayer: search bar, tabs with a results table, a now-playing panel, help, command palette."""

import contextlib
import functools
import io
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
    ProgressBar,
    Static,
    TabbedContent,
    TabPane,
)
from textual.worker import get_current_worker

from ttyplayer import control, favorites, history, player, youtube
from ttyplayer.utils import APP_NAME, format_time, unseen

SEARCH_LIMIT = 10
HISTORY_LIMIT = 50
FAVORITE_MARK = " ♥"
IDLE_TEXT = "Nothing playing — press / to search"
LONG_SEEK_SECONDS = 30

# The command palette's entries and the help modal's Commands section: (name, app action, help).
COMMANDS = [
    ("Search…", "focus_search", "Focus the search box"),
    ("Next theme", "next_theme", "Switch to the next color theme"),
    ("Help", "help", "Every key and command"),
    ("Quit", "quit", "Quit and stop mpv"),
    ("Pause / resume", "toggle_pause", "Pause or resume playback"),
    ("Next", "next", "Play the next track in the queue"),
    ("Previous", "prev", "Play the previous track in the queue"),
    ("Mute", "toggle_mute", "Mute or unmute"),
]


def resolve(text, limit=SEARCH_LIMIT):
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
    ]

    def __init__(self, **kwargs):
        super().__init__(zebra_stripes=True, cursor_type="row", **kwargs)

    def on_mount(self):
        self.add_column("#", key="number")
        self.add_columns("Title", "Uploader", "Length")

    def fill(self, rows):
        """Replace every row with rows (cells in column order), the cursor kept in place."""
        row = self.cursor_row
        self.clear()
        for cells in rows:
            self.add_row(*cells)
        self.move_cursor(row=min(row, self.row_count - 1))


class PickTable(VideoTable):
    """Videos to pick from (Search, History, Favorites): videos holds them in row order."""

    BINDINGS = [
        Binding("enter", "select_cursor", "Play from here", show=False),
        Binding("a", "app.append", "Add"),
    ]

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
        Binding("t", "next_theme", "Next theme", show=False),
        # Not a priority binding: the search box must be able to take a typed q.
        Binding("q", "quit", "Quit"),
        Binding("ctrl+c", "quit", "Quit", show=False, priority=True),
    ]

    def __init__(self, client_factory=player.MpvClient, resolve=resolve, video=False):
        super().__init__()
        self.client_factory = client_factory
        self.resolve = resolve
        self.video = video
        self.searched = None  # the text behind the Search tab's rows, for m
        self.favorite_ids = set()
        self.history_stale = False
        self.queue_shown = ([], None)
        self.client = None
        self.remote = None
        self.closing = False

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
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
        yield NowPlaying()
        yield Footer()

    def on_mount(self):
        self.app_thread = threading.get_ident()
        self.set_searching(False)
        self.show_favorites()  # first: the other tables read favorite_ids for their ♥
        self.show_history()
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
            if shown is None:
                videos = self.resolve(text)
            else:
                videos = unseen(self.resolve(text, len(shown) + SEARCH_LIMIT), shown)
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
        client = self.ensure_client()
        if client is None:
            return
        client.queue.clear()
        client.index = 0
        for video in event.data_table.videos[event.cursor_row :]:
            client.add(video)
        client.play_current()

    def picked(self):
        """The video under the cursor of the focused Search/History/Favorites table, or None."""
        table = self.focused
        return table.picked() if isinstance(table, PickTable) else None

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

    def playing_video(self):
        client = self.client
        if client is None or client.idle or not 0 <= client.index < len(client.queue):
            return None
        return client.queue[client.index]

    def mark_playing(self):
        """▸ in the # cell of the Search/History/Favorites row being played, a blank everywhere else."""
        playing = self.playing_video()
        for table in self.query(PickTable):
            for number, (row_key, video) in enumerate(zip(table.rows, table.videos), start=1):
                table.update_cell(row_key, "number", number_cell(number, video == playing))

    # --- history and favorites ------------------------------------------

    @on(TabbedContent.TabActivated)
    def refresh_library_tab(self, event: TabbedContent.TabActivated):
        if event.pane.id == "history":
            self.show_history()
        elif event.pane.id == "favorites":
            self.show_favorites()

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

    # --- queue --------------------------------------------------------

    def show_queue(self):
        """Rebuild the Queue tab from client.queue / client.index (no ▸ when idle), the cursor kept in place.

        Skipped when neither changed: on_state also fires for every time-pos tick.
        """
        videos, current = [], None
        if self.client is not None:
            with self.client.queue_lock:
                videos = list(self.client.queue)
                current = None if self.client.idle else self.client.index
        if (videos, current) == self.queue_shown:
            return
        self.queue_shown = (videos, current)
        self.query_one(QueueTable).fill(
            video_cells(video, index + 1, index == current, video.id in self.favorite_ids)
            for index, video in enumerate(videos)
        )
        self.query_one(TabbedContent).get_tab("queue").label = tab_label("Queue", len(videos))

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
