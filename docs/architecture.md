# ttyplayer Architecture

## Overview

ttyplayer is a thin, well-structured controller around two external tools:

- **yt-dlp** resolves what to play: turns a query into a list of videos, a playlist link into its entries, and a video link into one video.
- **mpv** produces sound and video. It runs in the background and is driven over its JSON IPC socket.

ttyplayer itself never decodes audio or talks to YouTube's HTML directly.

## Modules

```
src/ttyplayer/
  cli.py       Typer app: play, search, history, favorite, favorites, playlist, tui, serve, config,
               version, pause, next, prev, stop, status. Glue only.
  tui.py       TtyplayerApp (Textual): header, search bar, Search/Queue/History/Favorites tables,
               now-playing panel, help and settings modals, command palette, themes, toasts over MpvClient;
               styles in tui.tcss (see docs/tui-design.md).
  server.py    ttyplayer serve: make_app(client, settings) builds the aiohttp app (token middleware,
               /api/*, /ws, the page under static/); Broadcaster is the player's on_state;
               ServerThread runs the app on its own thread and event loop; ensure_token.
  stream.py    ttyplayer serve --stream: Streamer paces mpv's PCM (MpvClient(headless_pcm=True)) into
               ffmpeg (Ogg Opus) and fans its pages out to each /stream Listener; MPV_PCM_OPTIONS.
  remote.py    ttyplayer tui --remote: RemoteClient answers TtyplayerApp's MpvClient calls over the
               server's /api/* (urllib) and mirrors /ws (aiohttp, own thread); RemoteApp is the TUI
               with it as the player and no control socket.
  youtube.py   is_url, search(query, limit, source), fetch -> list[Video], fetch_playlist -> (title, list[Video]),
               related(video_id, limit=10) -> list[Video]: the RD<id> mix (the one place its URL is built),
               the seed dropped; radio mode's source. A mix pages on without end, so yt-dlp reads only its
               first 2 * limit + 1 entries (fetch_playlist(url, end) -> playlistend).
               SOURCES = ("youtube", "soundcloud") is the one list of search sources (settings checks
               search_source against it); PREFIXES maps each source's two letters (yt, sc), which are both
               yt-dlp's search key (ytsearchN:, scsearchN:) and the TUI's sc:/yt: prefix (split_source).
               Wraps yt-dlp errors in YouTubeError. Every entry list goes through
               utils.handle_many_entries, so only videos come back (see utils.is_video).
  player.py    MpvClient: spawn mpv, IPC socket, listener thread, keys, queue, status line,
               handle_control for the control socket; append(videos), the one "add to the end, play if
               idle" path (the server's /api/queue and radio mode use it); radio mode (see below).
  control.py   Control socket: control_path, Server(handler), send(name) -> reply dict.
  history.py   JSON lines record of what was played; load() newest first, one per video.
  favorites.py JSON lines list the user curates; add() dedupes by id, remove(n) by listed position,
               remove_id(id), ids() for the ♥ markers.
  playlists.py One JSON lines file per named playlist under data_path("playlists"), in the user's
               order, repeats kept; names, load, create, delete, add, remove(n), move(i, j), replace;
               names checked against NAME, PlaylistError when bad or missing.
  settings.py  Settings dataclass (show_clock, theme, search_limit, server_host, server_port,
               server_token, remote_url, stream_enabled, spotify_client_id, show_levels, search_source, radio), settings_path, load, save,
               update(key, text), change(key, value); a flat settings.toml, SettingsError when broken.
  spotify.py   ttyplayer spotify: login (OAuth PKCE, a one-shot callback listener on 127.0.0.1:8765, the
               tokens in spotify.json next to settings.toml, 0600), _get(path) (the token, refreshed when
               expired, and one Retry-After wait on a 429), user_playlists, playlist(ref) -> (name, [Track]),
               import_tracks: youtube.search per track, playlists.add of the first hit. SpotifyError.
  models.py    Video dataclass: id, title, uploader, duration, source ("youtube" by default) and link (the
               page URL yt-dlp reported, for every other source). url is the only place that builds a URL:
               the watch URL from id for youtube, else link; tagged_title puts "SC " before a SoundCloud title.
  utils.py     data_path, format_time, is_video, video_from_info, handle_many_entries, unseen, parse_picks;
               video_entry, read_entries, append_entries, write_entries for the JSON lines files.
```

Outside the package, `scripts/brew_formula.py <version>` prints the Homebrew formula for a release
(standard library only): the sdist of ttyplayer and of each runtime dependency, followed from the
wheel's Requires-Dist through PyPI's JSON with the versions `uv.lock` pins. `release.yml` runs it
after the PyPI upload and commits the result to the `webliftro/homebrew-tap` tap.

Dependencies point one way:

```
cli  ->  youtube, player, history, favorites, playlists, control, settings, tui (imported only by the tui command),
         server (imported only by the serve command), remote (imported only by tui --remote),
         spotify (imported only by the spotify commands)
spotify  ->  youtube, playlists, settings (for the config dir only)
remote  ->  tui, server, control, models      (tui -> remote -> (HTTP) server: the server plays)
server  ->  player, youtube, playlists, settings, control, history (through the client and callbacks cli wires)
tui  ->  youtube, player, history, favorites, control, settings, utils, models
settings  ->  utils
player  ->  control
youtube, player, history, favorites, playlists  ->  models, utils
```

`youtube.py`, `player.py`, `history.py`, `favorites.py` and `playlists.py` know nothing about each other or about Typer; nothing below `cli`, `tui` and `server` imports `settings.py`, except `spotify.py`, which keeps its tokens beside it (`settings_path()`'s folder) and reads no setting. The settings are read once per process: the `tui` command loads them and passes them to `TtyplayerApp(settings=…)`. `control.py` knows nothing about Typer or mpv: it moves JSON lines and calls a `handler(name) -> dict`. No business logic lives in Typer command bodies.

## Data flow

```
user input
   |
   v
cli.play(target, --video, --limit)
   |
   +-- is URL? --> youtube.fetch(url) -> [Video]        (1 or many, playlists expand)
   |
   +-- else ----> youtube.search(query, limit) -> [Video]
                  print numbered list, ask for picks "1 3 5" -> [Video]
   |
   v
cli.start_playback(videos, with_video)
   |
   v
player.MpvClient(video, on_play=history.record)
   add() each video, play_current(), run()
   |
   v
key loop (main thread)         listener thread
  space/arrows/n/p/q  ---->    reads one JSON line at a time:
  seek/change_volume/            handle_message(message):
  toggle_pause -> send()           property-change -> state, notify()
                                   playback-restart -> started_in (first one after loadfile)
                                   end-file eof    -> next(), or at the end radio (below) or idle, notify()
                                   end-file error  -> error = "Could not play <title>: <file_error>",
                                                      then as eof
                                 notify(): on_state(status()) if set, else render()
```

## Radio mode

`MpvClient.radio` (the `radio` setting, `play --radio`, or `radio on|off|toggle` on the control
channel at run time) decides what happens when the queue runs out. `_next_or_idle()`, the branch an
eof or a failed load takes past the last track, calls `_start_radio()` under `queue_lock`:

```
radio off, or empty queue      -> idle, as before
last track not from youtube    -> error = RADIO_NEEDS_YOUTUBE, idle
else                           -> radio_fetching = True (status()["radio"] == "fetching"; idle stays False,
                                  so the bars keep the last track), a worker thread runs _fetch_radio(seed)
_fetch_radio (worker thread)   -> youtube.related(seed.id) (never on the reader thread), history.load(50)
                                  -> under queue_lock: dropped if the radio was turned off, quit() ran or
                                     a track started meanwhile (_play_index clears radio_fetching), or a
                                     newer lookup took over (radio_worker is no longer this thread);
                                     else unseen(related, recent + queue)[:RADIO_BATCH]
                                  -> append(fresh): the path /api/queue takes (idle -> jump to the first)
                                  -> nothing fresh or YouTubeError: error = "Radio: …", idle, notify()
```

## TUI

`ttyplayer tui` runs `tui.TtyplayerApp(client_factory=player.MpvClient, resolve=tui.resolve, video=...)`. Only `tui.py` imports Textual, and `cli.py` imports `tui` inside the command, so the other commands start without it.

Layout, top to bottom: `Header` (clock, unless `show_clock` is off); the search row (`SearchBox` + a `LoadingIndicator` shown only while a lookup runs); a `TabbedContent` with four `DataTable`s of the same columns (`#`, `Title`, `Uploader`, `Length`): `ResultsTable`, `QueueTable`, `HistoryTable`, `FavoritesTable`; the `NowPlaying` panel (three lines: state · title · uploader · `[i/n]`, the progress bar and time, 🔊/🔇 volume · up next · start-up time); Textual's `Footer`. `?` pushes `HelpScreen`, `S` pushes `SettingsScreen`; Ctrl-P opens Textual's command palette.

Threads: the app thread owns every widget. The `lookup` worker thread runs `youtube.*`; the player's listener thread (and the control socket's thread, through `handle_control`) reach the screen only through `on_state` → `call_from_thread`. Actions on the app thread call `MpvClient` methods directly; those take `queue_lock` and call `notify()` after releasing it.

```
Enter in the search box -> spinner on; lookup worker thread: split_source(text, search_source) -> source, query
                           resolve(query, search_limit, source)
                             is_url? youtube.fetch : youtube.search(query, search_limit, source)
                           -> call_from_thread: spinner off; refill the table, cursor on row 1,
                              focus it (or a toast: No videos found / YouTube lookup failed)
m on the Search table   -> spinner on; the same worker: unseen(resolve(text, shown + search_limit), shown)
                           -> call_from_thread: rows appended, numbered on (or No more results)
Enter / a on a row      -> first use: client_factory(video, on_play=app.on_play,
   (Search, History,                                 on_state=on_player_state), control.serve(...)
    Favorites)             Enter: queue = that row and the rows after it in that table, play_current();
                           a: add(), play if it was empty, else notify()
f on a row / d          -> favorites.add / remove_id, toast; the Favorites table and every ♥ redrawn
  (f with no row: the track playing)
on_play (under queue_lock) -> history.record(video), mark the History table stale (never waits)
tab 3 / 4 shown         -> History / Favorites table reloaded from history.load(50) / favorites.load()
keys on the table       -> BINDINGS -> app actions -> toggle_pause / seek / change_volume / toggle_mute
                           / next / prev
start                   -> Header(show_clock); App.theme = the saved theme (unknown: the default, a toast)
S                       -> SettingsScreen: one row per Settings field; Enter on a bool: change_setting
                           (settings.change saves it; show_clock mounts a new Header), else a hint
t / Ctrl-P              -> action_next_theme: App.theme = the next of sorted(available_themes), saved, toast /
                           CommandPalette: TtyplayerCommands (from COMMANDS) + Textual's system commands
keys on the Queue table -> Enter jump(row) / d remove(row) / K J move(row, row∓1) / c clear_others()
listener thread         -> on_state(status) -> call_from_thread -> History reloaded if stale,
                           NowPlaying.show(status), ▸ on the rows,
                           show_queue(): the Queue table rebuilt from client.queue / client.index
q, Ctrl-C, ttyplayer stop -> app exits -> on_unmount: remote.stop(), then client.quit()
```

The screen and keys are specified in `docs/tui-design.md`. `NowPlaying.show(status)` is the only writer of the now-playing panel, and it reads nothing but `MpvClient.status()`. Every message is a toast (`App.notify`, markup off so titles and yt-dlp errors print as they are); the panel never shows messages and the table keeps its rows on an error. The Queue tab is a view of `client.queue` / `client.index`, never a second list: every edit is an `MpvClient` method, and its `notify()` rebuilds the table (only when the queue or index changed, so time-pos ticks leave it alone), the tab title `Queue (n)` and the panel's `[i/n]` / `Up next`. The History and Favorites tabs read the files the CLI writes, through `history.load()` / `favorites.load()` / `favorites.ids()` only; each `PickTable` (Search, History, Favorites) keeps its own `videos` list in row order, and `m` uses the CLI's `utils.unseen`. `on_play` runs under the player's `queue_lock`, so it records and flags the History tab, which reloads on the `on_state` that follows instead of waiting for the app thread. Keys are defined once in the `BINDINGS` lists of the app, `SearchBox`, `VideoTable` (the playback keys and `f` every table shares), `PickTable` (Enter, `a`), `ResultsTable` (`m`), `FavoritesTable` (`d`) and `QueueTable`: the Footer shows the ones with `show=True`, and the `?` help modal lists all of them from the same lists. The command palette (Ctrl-P) keeps Textual's own provider (theme picker, keys, quit) and adds `TtyplayerCommands`, built from the one `COMMANDS` list of (name, action, help): each entry runs `app.run_action(action)`, the same `action_*` its key runs, and the help modal lists the same `COMMANDS` under "Commands". `t` cycles `App.theme` through `sorted(available_themes)`; nothing about the theme is stored between runs. Styles live in `src/ttyplayer/tui.tcss` (theme variables only), loaded through `CSS_PATH` and shipped in the wheel.

`ttyplayer stop` sends SIGINT as before; under Textual's asyncio loop that cancels the app, which unmounts normally. `q`, `/`, `?` and `1`–`4` are ordinary app bindings: the focused search box consumes them as letters, a table does not, so they act from a table (Esc first from the box); Ctrl-C is a priority binding and quits from anywhere. If remote control cannot start, its stderr warning is captured and shown as a warning toast.

## Server

`ttyplayer serve [--host] [--port]` (defaults: settings `server_host` 127.0.0.1, `server_port` 7700) plays headless: no keyboard loop, no TUI. `cli.serve` wires it and `server.py` holds everything else; the design and the API table are in `docs/server-design.md`.

```
cli.serve -> settings.load, server.ensure_token (a token_urlsafe saved on first serve)
          -> hub = server.Broadcaster()
          -> player.MpvClient(video=False, on_play=history.record, on_state=hub)    (no run())
          -> control.serve(client.handle_control)                                   (the CLI still works)
          -> server.ServerThread(server.make_app(client, settings, hub), host, port)
          -> prints the URL with ?token= and its QR code, sleeps until KeyboardInterrupt
Ctrl-C, SIGTERM, `stop` (CLI, /api/command, /ws) -> KeyboardInterrupt in the main thread
          -> ServerThread.stop(), remote.stop(), client.quit()   (in that order)
```

Threads: the main thread only sleeps. The aiohttp thread runs its own event loop and is the only place requests and sockets live; it calls `MpvClient`'s thread-safe methods (`handle_control`, `seek`, `change_volume`, `status`, `queue_listing`, `play_current`, `jump`, `notify`, and the queue under `queue_lock`). `youtube.*` runs in the loop's executor, never on the loop. The player's listener and poller threads reach the sockets only through `Broadcaster.__call__` → `loop.call_soon_threadsafe` → one send per open socket; a closed or broken socket is dropped without touching the others.

Every `/api/*` request and `/ws` passes the `require_token` middleware (`Authorization: Bearer <token>` or `?token=`, compared with `hmac.compare_digest`); `GET /` and `/static/*` do not. Errors are JSON `{"error": "<one line>"}` through the `errors_as_json` middleware. `/api/command` and the socket's text messages share `run_command`: `pause|next|prev|stop|mute` go to `handle_control`, `seek|volume` to `seek` / `change_volume` with a numeric `value`. `jump|remove` take a queue row (`is_row`), `move` a `[source, target]` pair of them, `clear_others` nothing. Replies and a new socket's first message are `full_status`: `status()` plus `queue` (the `queue_listing` videos); broadcasts are each `on_state` status as the player sends it. A socket command that fails gets `{"error": …}` alone; a status also has an `error` key (the player's, usually `null`), so the page and `RemoteClient` tell them apart by `idle`, which every status has.

### The TUI as a remote

```
cli.tui --remote URL (or settings remote_url) [--token T, else settings server_token]
          -> remote.RemoteApp(url, token, …)          TtyplayerApp, client_factory -> RemoteClient
             on mount: ensure_client()                the panel follows the server from the start
             start_remote(): nothing                  this machine plays nothing: no control socket
TtyplayerApp --(the MpvClient calls)--> RemoteClient --HTTP--> /api/command  (TUI thread, 2 s timeout)
                                                     --HTTP--> /api/play, /api/queue  (worker thread, in order)
                                        listener thread <--WS-- /ws  -> mirror(status) -> on_state
```

`add()` only buffers: the next `play_current()` posts the buffered videos from `index` on (`/api/play` for the first, `/api/queue` for the rest), the next `notify()` appends them; a newer play stops an older batch. `queue`, `index` and `idle` mirror the last status the server sent (a command's reply or a `/ws` message). Failures reach `RemoteApp.server_error` as one toast each (`Server rejected the token`, `Server unreachable at …`, `YouTube lookup failed: …`); the listener reconnects with backoff and reports a failure once until it connects again. `on_play` is accepted and ignored: history is the server's. `move(i, j)` sends the server's `move` command (`[i, j]`), or nothing, returning False, when a row is off the mirrored queue. `quit()` bumps the batch too, so nothing more is posted once the lookup in flight returns.

## mpv IPC

mpv is started with `--idle --no-terminal --input-ipc-server=<private socket>`; `--no-video` unless asked. The socket lives in a per-client temp dir so two ttyplayers never share an mpv, and the dir is removed on every exit path.

Messages are newline-delimited JSON:

- request: `{"command": ["set_property", "pause", true]}`
- reply: `{"request_id": 0, "error": "success"}`
- event: `{"event": "property-change", "id": 1, "name": "time-pos", "data": 12.3}`

mpv only reports property changes you subscribe to, so `__init__` sends `observe_property` for `time-pos`, `duration`, `pause`, `media-title`, `volume`, `mute` and `ao-volume`. Commands used: `loadfile`, `cycle pause`, `seek`, `add volume` / `add ao-volume`, `cycle mute`, `get_property`, `af add` / `af remove`, `quit`.

mpv exposes no raw audio over IPC, but a labeled lavfi filter in its `--af` chain publishes its metadata: `build_argv()` adds `LEVELS_FILTER` (`@levels`, an `astats` filter writing each channel's `Peak_level`) unless `show_levels` is off or under `headless_pcm`, and while a track plays (not paused, not idle) the poller reads `af-metadata/levels` every `LEVELS_INTERVAL` (0.1 s, the volumes then every 20th tick); `parse_levels()` turns it into `status()["levels"]`, `[left, right]` dBFS (`-inf` is `-90.0`, mono is one channel twice, anything else `None`), notifying only on a change; `set_levels()` adds or removes the filter in a running mpv. With `normalize_loudness`, `NORMALIZE_FILTER` (`@norm`, `loudnorm`) comes first in the chain, so the meter measures the normalized sound; it stays under `headless_pcm` (the stream's listeners hear it), and `set_normalize()` puts it back at the front with `af pre`.

`send()` tags every command with an increasing `request_id`. `get_property(name, callback)` registers the callback for that id before sending; `handle_message` hands a reply's `data` to it (`None` on an error) and drops replies nobody registered (`observe_property`, `loadfile`, ...).

mpv has two volumes: `volume`, its own gain, and `ao-volume`, the audio output's (the app's stream volume on PulseAudio/PipeWire and WASAPI, mpv's own output level on coreaudio; `property unavailable` where the output has none). On macOS mpv cannot see the system volume, so the meter shows it from `osascript` instead: `read_system_volume()` and `write_system_volume(n)` are the only two places that run it, each with a 1 s timeout and a fixed script. `volume_backend()` is the one decision of what the meter shows: the first of `system-volume` (macOS only), `ao-volume`, `volume` that holds a number. `status()["volume"]` is its value, with `volume_source` `"system"`, `"device"` or `"player"`; `change_volume()` moves the same one, so the keys move the meter that is shown: on macOS it queues the step for a writer thread (`write_volumes()`, started only on macOS and stopped by `quit()`, which drops the steps not yet written), which applies the steps in order, sets the system volume to the clamped (0-100) sum and notifies as soon as `osascript` succeeds — so a key, even on the TUI's app thread, never waits on `osascript` — elsewhere it sends `add ao-volume` / `add volume`. Neither mpv nor macOS says when the OS changes its volume, so a daemon thread polls every `VOLUME_POLL_SECONDS` (2) while a track is loaded: on macOS it runs `read_system_volume()` (a failure stores `None`, and the meter falls back to `ao-volume`), and everywhere it reads `ao-volume` with `get_property`; it notifies only on a change, and `quit()` stops it before closing the socket. The poll's read and the writer's write each hold `system_volume_lock` from `osascript` until the value is stored (notifying after releasing it), so they never overlap and a read begun before a write can't store its older volume over the new one. coreaudio's first `ao-volume` `property-change` carries no `data`; `handle_message()` ignores it rather than wipe a known value. Linux and Windows are unchanged: the meter shows mpv's device (per-app) volume.

The connect is retried for up to 3 seconds because mpv creates the socket a moment after it starts. `quit` waits 2 seconds for mpv to exit, then kills it.

## Control socket

While `run()` owns the keyboard, the player also listens on a Unix socket so another terminal can drive it: `ttyplayer pause`, `next`, `prev`, `stop`, `status`, and `playlist save-queue` (which sends `queue`). The socket is `$XDG_RUNTIME_DIR/ttyplayer/control.sock`, or `<tempdir>/ttyplayer-<uid>/control.sock` without `XDG_RUNTIME_DIR`; the dir is created `0700`, so only the same user can send commands. A dir that already exists must be a real directory (not a symlink) owned by the user with no group/other permissions, or the server refuses it before touching anything inside. A stale socket file is replaced; the most recently started player owns the path.

One request per connection, newline-delimited JSON:

- request: `{"command": "pause"}`
- reply: `{"ok": true}`, `{"ok": true, "title": ..., "position": ..., "duration": ..., "paused": false, "index": 1, "total": 3, "started_in": 2.4}` for `status`, `{"ok": true, "videos": [{"id": ..., "title": ..., "uploader": ..., "duration": ...}, ...], "index": 1}` for `queue`, or `{"ok": false, "error": "unknown command x"}`

`control.Server` runs on a daemon thread and calls `MpvClient.handle_control(name)`, which performs what the keys do: `pause` is the space key, `mute` the TUI's `M` (no CLI command sends it yet), `next`/`prev` move the queue, `queue` returns `MpvClient.queue_listing()` (every queued video, read under `queue_lock`, and the 1-based current index), `status` returns `MpvClient.status()`, the same values `render()` draws (`player.status_line` formats them for both). `stop` sends SIGINT to the main thread, so `run()` leaves through its Ctrl-C path. `run()` stops the server and removes the socket in the same `finally` that restores the terminal. Bytes that are not UTF-8 count as a malformed request. If the dir is unsafe or the socket cannot be bound, one warning goes to stderr and playback continues without remote control.

`MpvClient.send()` is called from three threads now (keyboard, listener, control), plus the TUI. `send_lock` makes each command line one atomic write, and the `queue_lock` RLock guards every queue change (`play_current()`, `next()`, `prev()`, `jump()`, `remove()`, `move()`, `clear_others()`, the `eof` step), so two `n` presses from different threads move the queue once each instead of racing on `index`. The same lock covers the `playback-restart` measurement, so a track change can't land inside it. Callbacks run after the lock is released: the TUI's `on_state` waits for the app thread, which may itself be waiting to move the queue.

## Queue

ttyplayer owns the queue: `MpvClient.queue` is a list of `Video`, `index` the current one. `next`/`prev` move and load; the listener steps on `end-file` with reason `eof`, or marks the client `idle` at the end of the queue (`idle` is also true before anything plays). The TUI's edits are `jump(i)` (play that index), `remove(i)` (removing the current track plays the next one, else the previous one, else sends `stop` and leaves an empty idle queue), `move(i, j)` (the current track stays current wherever it lands) and `clear_others()` (keep only the current track, or empty the queue when idle); each returns whether something changed. All of them, and `next`/`prev`/`play_current`, go through `_edit(change)`: the change runs under `queue_lock`, and `notify()` runs after the lock is released, only if something changed. `_play_index(i)` is the one place that loads a track, resets `started_in`/`loaded_at` and calls `on_play`. The status line shows the current title from the queue, not from mpv, so there is no file-name flicker while yt-dlp resolves the stream. `status()` carries `idle`; while idle with an empty queue the title is None (mpv's `media-title` outlives its track), and the TUI's panel and both `▸` markers treat an idle client as playing nothing.

`play_current()` stamps `time.monotonic()` when it sends `loadfile`; the first `playback-restart` event after it stores the difference as `started_in` in `status()` (mpv sends another restart after every seek, which is ignored). With `TTYPLAYER_TIMING=1` (read by `player.timing()`), `status_line` appends `started in <n>s` and `cli.lookup()` prints `lookup took <n>s` to stderr.

When mpv cannot load a track (`end-file` with reason `error`), `_skip_failed()` stores `error = "Could not play <title>: <reason>"` (the title from the queue, the reason mpv's `file_error`, else `unavailable`) and moves on as at an `eof`; one `notify()` carries both. `status()["error"]` is that text, `None` by default, and goes back to `None` on the first `playback-restart` after a loadfile or on `play_current()`. The text is built only there; consumers show it once per change: the TUI as an error toast, the web remote in its banner, the CLI's `render()` as one stderr line above the status line. `status_line()` leaves it out, so `ttyplayer status` is unchanged.

`on_play(video)` fires whenever a queued video starts. The CLI passes `history.record` (the TUI a wrapper around it); the player knows nothing about files.

`on_state(status)` is for a UI that owns the terminal itself. When set, `notify()` (called on every property change, every queue change, and an `eof` at the end of the queue) hands it `status()` and nothing is printed; when `None`, `notify()` calls `render()`, which prints the status line. `status()` is the one source for `render()`, `on_state` and the control socket's `status` reply; it also carries `volume` and `muted` (the observed `mute` property), which `status_line()` and the TUI panel both draw with `player.volume_meter()`, and `up_next`, which `render()` adds after the status line. `seek()`, `change_volume()`, `toggle_mute()` and `toggle_pause()` are the only places that encode those mpv commands, so keys, remote control and a UI all go through them.

## Platforms

`utils.WINDOWS` (`sys.platform == "win32"`) is the one platform test; `player` and `control` import it, and tests patch each module's copy to run the Windows branches on every OS. There are two seams, and nothing above them (the CLI, the TUI, `send()`/`listen()`/`quit()`, `serve`/`send`) knows which side it is on.

- **Player.** `ipc_path()` gives mpv a socket in a private temp dir on POSIX, or `\\.\pipe\ttyplayer-<pid>-<random>` on Windows (no dir to remove). `connect()` retries either for 3 seconds and returns a transport with `write(bytes)`, `readline() -> bytes` and `close()`: `SocketTransport` (the Unix socket) or `PipeTransport` (the pipe opened as a binary file, `open(path, "r+b", buffering=0)`). Keys come from `terminal_keys()` (stdin in cbreak mode, `termios`/`tty` imported on POSIX only) or `console_keys()` (`msvcrt`, `kbhit()` polled every `KEY_POLL_SECONDS` so a remote `stop` lands without a key press; arrows arrive as `\xe0`/`\x00` plus a letter, `\x03` is Ctrl-C). Both yield key names (a character, or `left`/`right`/`up`/`down`), and `MpvClient.press()` looks them up in its table from `keys(seek_seconds, volume_step)` (`KEYS` is the default one). `interrupt_main()` is `pthread_kill(SIGINT)` on POSIX and `_thread.interrupt_main()` on Windows, where it lands on the next poll tick.
- **Control.** `control_endpoint()` is what `Server` and `send` use: `SocketEndpoint` is the Unix socket above; `LoopbackEndpoint` listens on `127.0.0.1` on an ephemeral port, writes `"<port> <token>"` (a random 32-hex token) to `%LOCALAPPDATA%\ttyplayer\control.txt` (`utils.data_path`), and requires `{"command": ..., "token": ...}`; a missing or wrong token gets `{"ok": false, "error": "bad token"}`. Any local process can reach the port, so the token, readable only through the user's profile, is what gates commands (other users with admin rights are out of scope). A player's `stop()` removes the file only if it still holds its own port and token.

`utils.data_path` falls back to `%LOCALAPPDATA%\ttyplayer` on Windows when `XDG_DATA_HOME` is unset. CI runs the suite on ubuntu, macOS and Windows.

## Error handling

- mpv missing or never answering: one-line message, exit 1.
- yt-dlp failures (no network, bad link, private video) raise `YouTubeError`; the CLI prints one line and exits 1. yt-dlp's own stderr output is silenced.
- Zero results: "No videos found", exit 1, before any prompt. A search result is a video only: `utils.is_video(entry)` keeps an entry whose `ie_key` (or `extractor_key`) is one of `utils.TRACK_EXTRACTORS`, `"Youtube"` or `"Soundcloud"` (channels and playlists are `"YoutubeTab"`, `"SoundcloudSet"`, `"SoundcloudUser"`…), or, without either key, whose id is 11 characters of `[A-Za-z0-9_-]`; `None` entries are dropped. A search of only channels and playlists finds nothing.
- A track mpv cannot play: reported (`status()["error"]`, see Queue) and skipped; the queue moves on.
- Bad picks: re-prompt with the valid range.
- Ctrl-C, `q`, and `ttyplayer stop` all go through `quit` and the `finally` that restores the terminal.
- Remote commands with no player running: "No ttyplayer is playing", exit 1.

## Testing

Tests never touch the network or start mpv.

- `youtube.py`: a `FakeYoutubeDL` monkeypatched in place of the real class.
- `player.py`: `build_argv` directly; queue, title, and `handle_control` logic on a client built with `__new__` and a recording `load`/`send`; `run()` around a fake stdin and stubbed termios.
- `control.py`: a real Unix socket in a short temp dir, with a recording handler.
- `history.py`, `favorites.py`, `playlists.py`: real files under pytest's `tmp_path` (playlists through `XDG_DATA_HOME`).
- `cli.py`: Typer `CliRunner` with `youtube.*`, `history_path`, `favorites_path`, and `player.MpvClient` monkeypatched; a `FakeClient` records what was queued.
- `utils.py`: pure functions, direct assertions.
- `tui.py`: Textual's `run_test()` pilot, headless, with a fake `resolve` and a recording `FakeClient` factory; `control.serve` monkeypatched. Async test bodies run under `asyncio.run` (no pytest plugin).
