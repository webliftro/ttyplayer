# ttyplayer TUI — design

The one place that says what the `ttyplayer tui` screen looks like and how it behaves.
Change it here first.

## Goal

A modern, full-screen player you would actually leave open: search, queue, history and favorites
in one place, a now-playing panel that moves, discoverable keys, and nothing that blocks the UI.
Textual 8 (the app, `tui.py`) over the same `MpvClient`, `youtube`, `history`, `favorites` and
`control` modules the CLI uses — the TUI never re-implements player logic.

## Layout

```
┌─ ttyplayer ──────────────────────────────────────────────────────── 14:02 ─┐  Header (clock)
│ 🔍 Search (sc: SoundCloud, yt: YouTube) or paste a link…           ◐     │  search bar + spinner
│ ┌ Search ──┬ Queue ──┬ History ──┬ Favorites ──┬ Playlists ──┐          │  TabbedContent (1–5)
│ │  #  Title                          Uploader          Length           │  DataTable, zebra,
│ │ ▸1  lofi hip hop radio             Lofi Girl          --:--           │  row cursor
│ │  2  Never Gonna Give You Up        Rick Astley        3:33            │
│ │  3  …                                                                 │
│ └───────────────────────────────────────────────────────────────────────┘
│ ┌ Now playing ──────────────────────────────────────────────────────────┐
│ │ ▓▓▓▓▓▓▓▓▓▓ ▶  Never Gonna Give You Up · Rick Astley             [2/5] │  art (show_art) · title bold accent
│ │ ▓▓▓▓▓▓▓▓▓▓ ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━  1:23 / 3:33 │  ProgressBar
│ │ ▓▓▓▓▓▓▓▓▓▓ 🔊 ▮▮▮▮▮▮▯▯▯▯ 60%  zz 27:13  ∞  Up next: lofi hip…  started 2.4s │  volume · sleep · radio · queue · timing
│ │ ▓▓▓▓▓▓▓▓▓▓ L ▮▮▮▮▮▮▮▮▮▮▮▮▮▮▮▮▮▮▮▮▮▮▮▮▮▮▮▮▮▯▯▯▯▯▯▯▯▯▯▯▯▯▯▯▯▯▯▯▯▯▯▯▯▯▯▯ │  level meter (show_levels)
│ │ ▓▓▓▓▓▓▓▓▓▓ R ▮▮▮▮▮▮▮▮▮▮▮▮▮▮▮▮▮▮▮▮▮▮▮▮▯▯▯▯▯▯▯▯▯▯▯▯▯▯▯▯▯▯▯▯▯▯▯▯▯▯▯▯▯▯ │
│ └───────────────────────────────────────────────────────────────────────┘
│ space Pause  n Next  p Prev  a Add  f Fav  / Search  ? Help  q Quit      │  Footer (from BINDINGS)
└──────────────────────────────────────────────────────────────────────────┘
```

- The search bar is always visible; `/` focuses it from anywhere, Esc leaves it for the table.
  A query starting with `sc:` searches SoundCloud, `yt:` YouTube, whatever `search_source` says;
  after a search the Search tab's heading reads `SoundCloud results` or `YouTube results`.
- Tabs: **Search** (results), **Queue** (what will play, reorderable), **History**, **Favorites**,
  **Playlists**. Digits `1`–`5` switch tabs. Every tab is a `DataTable` with the same columns so the eye never
  re-learns the screen: `#`, `Title`, `Uploader`, `Length`. The row being played shows `▸` in `#`.
- **Playlists** lists the playlists (`#`, `Name`, `Tracks`, `Length` = the sum of the known
  durations), tab title `Playlists (n)`. Enter opens one in the same table: its tracks in the
  columns above, tab title `Playlists › <name> (n)`; Esc or Backspace goes back to the list. The tab
  is read from the `playlists` files each time it is shown and after every change.
- Small centered modals: the playlist name (`P`, New playlist…; a bad name is shown under the box
  until a good one is typed), the sleep timer (Sleep…, the same modal), the playlist picker (`A`),
  and the yes / no before deleting a playlist.
- **Now playing** is docked above the Footer, five lines (three with `show_levels` off), always
  present. Idle it reads
  `Nothing playing — press / to search` (dim). It is built only from `MpvClient.status()`:
  `title`, `uploader`, `position`, `duration`, `paused`, `index`, `total`, `up_next`, `volume`,
  `started_in` (the last one only when `player.timing()`), `levels`, `sleep`, `radio`.
- After the volume, `zz 27:13` (`zz end` for `sleep end`) while a sleep timer is armed, from
  `player.sleep_text(status["sleep"])`; empty otherwise. It refreshes with each status, like the
  time. The palette's **Sleep…** opens a one-line modal prefilled with `30m` (`player.parse_sleep()`'s
  error stays under the box; `off` cancels; Esc leaves the timer alone) and toasts the player's reply.
- After the sleep timer, `∞` while radio mode is on and `∞ fetching…` while the player looks up
  related tracks at the end of the queue (the panel keeps the last track meanwhile), from
  `player.radio_text(status["radio"])`; empty while it is off. `R` (palette **Radio**) sends
  `radio toggle` through `handle_control`, the control socket's path, and toasts the reply
  (`Radio on` / `Radio off`); the `radio` setting is what a new player starts with.
- Left of the lines, the art column (`Art`): the playing track's thumbnail, 10 cells wide and as
  tall as the panel's lines (five, three without levels), on `$panel`, one cell before the text.
  `textual_image`'s `Image` widget draws it: the real picture over the Kitty graphics protocol (TGP)
  or Sixel where the terminal answers for one (Kitty, WezTerm, iTerm2, …), else a mosaic of colored
  half-cells (Terminal.app), fitted to the column with its shape kept. The widget class is imported
  before the app runs (`art.image_widget()`), because that import asks the terminal what it can draw.
  When the playing video's id changes the column goes blank and a worker thread loads `Video.thumbnail`
  through `art.fetch` (the disk cache, else the network, 3 s) and `art.decode`; a result for a track
  that is no longer playing is dropped, and only the app thread touches the widget. It stays blank
  while loading, idle, for a track without a thumbnail, or when the fetch fails. The column is not
  there at all (the lines take the whole width) without the `art` extra (`art.available()`) or with
  `show_art = false`; either way the panel's height and lines are the same.
- The last two lines are the level meter: `L` and `R` bars of the volume meter's cells
  (`player.level_meter()`), as wide as the panel, empty at −60 dBFS, full at 0 dBFS, in `$accent`.
  `levels` is `None` while paused (and before mpv has measured): the bars are empty, not hidden, so
  the panel never jumps. They update with each status, about every `LEVELS_INTERVAL` (0.1 s) while
  playing. `show_levels = false` hides both lines (the panel shrinks to three) and, in a running TUI,
  takes the filter out of mpv at once (`af remove`).
- Settings the TUI reads (the `S` modal lists every key with its value and default): `show_clock`,
  `theme`, `search_limit`, `search_source`, `remote_url`, `show_levels`, `show_art` (Enter in the modal
  hides or shows the art column at once), `radio`, `normalize_loudness`
  (a new player starts with the loudness filter; Enter in the modal sends `set_normalize`, `af pre` /
  `af remove @norm`), `seek_seconds` and `volume_step`. The last two build the tables' `,` `.` `-` `+`
  bindings (`step_bindings()`, from `player.keys()`'s numbers) when a table mounts and again when
  either changes, so the next key press uses the new step and `?` shows it.
- The Footer is Textual's own, fed by `BINDINGS` — key hints are never typed by hand twice. `?`
  opens a help modal that lists every binding with its description, generated from the same
  `BINDINGS`.
- Feedback is a toast (`App.notify`): "Added to queue", "Favorited", "No videos found",
  "YouTube lookup failed: …", "mpv is not installed…", "Remote control is off: …". The
  now-playing panel never shows messages; the table never goes blank because of an error.
- Searching never blocks: `youtube.*` runs in a thread worker; a `LoadingIndicator` next to the
  search box is visible only while it runs. Player events reach widgets through
  `call_from_thread` only.

## Keys

| Where | Key | Action |
|---|---|---|
| anywhere | `/` | focus the search box |
| anywhere | `?` | help modal (Esc closes) |
| anywhere | `1` `2` `3` `4` `5` | Search / Queue / History / Favorites / Playlists tab |
| anywhere | Ctrl-C | quit |
| anywhere | Ctrl-P | command palette (Textual built-in: search, theme, help, quit) |
| anywhere | `t` | next theme (cycles `App.available_themes`, saved to the settings file) |
| table | `P` | save the queue as a playlist: a name modal (one track: its title; else `Queue <date>`), a playlist of that name is replaced; `Nothing to save` with no queue |
| table | `S` | Settings modal: Enter flips a true / false setting or asks for a whole number one (checked like `config set`), Esc closes |
| table | `q` | quit (in the search box `q` is a letter) |
| table | space | pause / resume |
| table | `n` / `p` | next / previous |
| table | `,` / `.` | seek −`seek_seconds` / +`seek_seconds` (5 s by default) |
| table | `<` / `>` | seek −30 s / +30 s |
| table | `-` / `+` | volume −`volume_step` / +`volume_step` (5 by default) |
| table | `M` | mute / unmute |
| Search · History · Favorites row | Enter | play this one (the queue becomes this row and the rows after it in that table) |
| Search · History · Favorites row | `a` | add to the queue (starts playing if the queue was empty) |
| Search · History · Favorites · Queue row | `A` | add to a playlist: a picker of the playlists plus `New playlist…` (then the name modal) |
| Search · History · Favorites row | `f` | favorite / unfavorite this row |
| Search | `m` | more results (the next batch, same dedupe as the CLI's `m`) |
| Queue row | Enter | jump to this item |
| Queue row | `d` | remove from the queue |
| Queue row | `K` / `J` | move up / down |
| Queue | `c` | clear the queue (keeps the current track playing) |
| Favorites row | `d` | remove from favorites |
| Playlists row | Enter | open the playlist |
| Playlists row | `d` | delete the playlist after a confirm (`y` / Enter deletes, Esc keeps) |
| open playlist | Esc / Backspace | back to the list of playlists |
| playlist track | Enter | play the whole playlist from this track (the queue becomes the playlist) |
| playlist track | `a` | add to the queue |
| playlist track | `d` | remove from the playlist |
| playlist track | `K` / `J` | move up / down in the playlist |
| open playlist | `s` | shuffle-play the playlist |

Playback keys do nothing (no error) before a player exists.

## Look

- Stylesheet: `src/ttyplayer/tui.tcss`, loaded with `CSS_PATH`; shipped inside the package.
- Theme: Textual's built-in themes; default `textual-dark`; `t` cycles, the command palette
  lists them. Colors come from theme variables (`$primary`, `$accent`, `$surface`, `$panel`),
  never hard-coded, so every theme looks right.
- Panels: `border: round $primary` with a `border-title` (`Now playing`); tables use
  `zebra_stripes`, `cursor_type = "row"`; the now-playing title is `bold` in `$accent`, the
  uploader and idle text `dim`.
- Icons are plain Unicode that every terminal font has: `▶` playing, `⏸` paused, `▸` current
  row, `🔊`/`🔇` volume, `♥` favorite marker in a column-free way (title suffix ` ♥`). A SoundCloud
  row's title starts with the two-letter tag `SC ` (in every table and in the CLI's lists), so mixed
  lists read; YouTube rows have no tag.
- Progress: `ProgressBar(show_percentage=False, show_eta=False)`, `total=duration`,
  `progress=position`; indeterminate while `duration` is `None`. Volume: ten cells
  `▮`/`▯` plus the number; `--` while unknown.

## Testing

Tests run headless with `App.run_test()` and fakes (`FakeClient`, a fake resolver), never mpv
or the network.
