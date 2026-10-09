# ttyplayer

A modern YouTube player for the terminal. Search YouTube, queue videos and playlists, and play audio, or video too, through mpv: from the command line, or from a full-screen TUI. Keep a history and favorites, and drive the player from any other terminal.

## Usage

```
ttyplayer play <url>                 play a video link, audio only
ttyplayer play <playlist url>        queue every entry of a playlist
ttyplayer play <words...>            search, pick one or more results, play them in order
ttyplayer play ... --video           open a video window as well
ttyplayer play ... --limit 10        show more search results

ttyplayer search <words...>          list results with durations
ttyplayer history                    recently played, newest first
ttyplayer history --play             pick from history and play again
ttyplayer history --clear            forget the history
ttyplayer favorite <url | words...>  add a link, a playlist, or search picks to favorites
ttyplayer favorites                  favorites, newest first
ttyplayer favorites --play           pick from favorites and play them
ttyplayer favorites --remove 2       drop the second favorite as listed
ttyplayer favorites --clear          forget all favorites
ttyplayer playlist ...               your own named playlists (see Playlists below)
ttyplayer tui [--video]              full-screen: search box, results list, now-playing bar
ttyplayer serve [--host] [--port]    play headless, driven over HTTP from any device (see Server below)
ttyplayer config                     list the settings (see Settings below)
ttyplayer doctor                     check Python, yt-dlp, mpv and ttyplayer's folders
ttyplayer version
```

While ttyplayer plays in one terminal, any other terminal can drive it:

```
ttyplayer pause                      pause / resume
ttyplayer next                       next in the queue
ttyplayer prev                       previous in the queue
ttyplayer stop                       quit the player
ttyplayer status                     1:23 / 4:56  Playing  <title>
```

## Playlists

Playlists are named lists you keep, in the order you choose, separate from history and favorites. A name is 1 to 40 letters, digits, spaces, `_` or `-`.

```
ttyplayer playlist list                          every playlist and how many videos it holds
ttyplayer playlist show <name>                   its videos, numbered
ttyplayer playlist create <name>                 a new, empty playlist
ttyplayer playlist add <name> <url | words...>   append a link, every entry of a playlist link, or search picks
ttyplayer playlist remove <name> 3               drop the third video
ttyplayer playlist move <name> 3 1               move the third video to the top
ttyplayer playlist delete <name> [--yes]         delete it (asks first unless --yes)
ttyplayer playlist play <name> [--video] [--shuffle]   queue the whole playlist, in order or shuffled
ttyplayer playlist import <playlist url> [--as <name>] save a YouTube playlist, named after its title
ttyplayer playlist save-queue <name>             save what the playing ttyplayer has queued, replacing <name>
```

`save-queue` works from another terminal while ttyplayer plays, like `pause` and `status`; it creates the playlist if there is none by that name. `import` refuses a name that is already taken; give another with `--as`.

Keys in `ttyplayer play` (the terminal player; `ttyplayer tui` has its own table below):

| Key | Action |
|---|---|
| space | pause / resume |
| left / right, `,` / `.` | seek 5 seconds |
| up / down, `-` / `+` | volume |
| `n` / `p` | next / previous in the queue |
| `q` or Ctrl-C | quit, restores the terminal and stops mpv |

On Windows (Windows Terminal, PowerShell) the keys, arrows included, map the same way.

The volume meter, and the volume keys here and in the TUI, follow the Mac's system volume on macOS (the Mac's own volume keys move the meter within 2 seconds) and mpv's device (per-app) volume on Linux and Windows.

Picks accept several numbers at once: `1 3 5` queues those three in that order. After a search, `m` lists the next batch of results.

`TTYPLAYER_TIMING=1 ttyplayer play <words>` prints how long the YouTube lookup took and adds `started in 2.4s` (from `loadfile` to the first sound) to the status line.

## TUI

`ttyplayer tui` opens a full-screen player: a search box, Search / Queue / History / Favorites / Playlists tabs, and a now-playing panel. Type a search or paste a link and press Enter. Ctrl-P opens the command palette (search, playlists, save queue as playlist, next theme, settings, help, quit, pause, next, previous, mute, and Textual's own theme picker); `?` lists every key and command.

| Where | Key | Action |
|---|---|---|
| table | `↑` / `↓` | move through the list (volume is `-` / `+`) |
| anywhere | `/` | focus the search box (`esc` returns to the table) |
| anywhere | `?` | help: every key and command (`esc` closes) |
| anywhere | `1` `2` `3` `4` `5` | Search / Queue / History / Favorites / Playlists tab |
| anywhere | Ctrl-C | quit and stop mpv |
| anywhere | Ctrl-P | command palette |
| anywhere | `t` | next theme (remembered for next time) |
| table | `P` | save the queue as a playlist (asks for a name; an existing playlist of that name is replaced) |
| table | `S` | settings: Enter flips a true / false one, `esc` closes |
| table | `q` | quit and stop mpv (in the search box it is just a letter) |
| table | `space` | pause / resume |
| table | `n` / `p` | next / previous |
| table | `,` / `.` | seek −5 s / +5 s |
| table | `<` / `>` | seek −30 s / +30 s |
| table | `-` / `+` | volume −5 / +5 |
| table | `M` | mute / unmute (🔇 in the panel) |
| table | `f` | favorite / unfavorite this row (the track playing when there is no row) |
| Search · History · Favorites row | Enter | play this one, then the rows after it |
| Search · History · Favorites row | `a` | add to the queue |
| Search · History · Favorites · Queue row | `A` | add to a playlist (pick one, or New playlist…) |
| Search | `m` | more results |
| Queue row | Enter | jump to this item |
| Queue row | `d` | remove from the queue |
| Queue row | `K` / `J` (or `shift+↑` / `shift+↓`) | move up / down |
| Queue | `c` | clear the queue (keeps the current track playing) |
| Favorites row | `d` | remove from favorites |
| Playlists | Enter | open the playlist (`esc` or Backspace goes back to the list) |
| Playlists | `d` | delete the playlist (asks first: `y` or Enter deletes, `esc` keeps it) |
| playlist track | Enter | play the whole playlist from this track |
| playlist track | `a` | add to the queue |
| playlist track | `d` | remove from the playlist |
| playlist track | `K` / `J` (or `shift+↑` / `shift+↓`) | move up / down in the playlist |
| open playlist | `s` | shuffle-play the playlist |

In the search box, letters, digits, `/` and `?` are typed as text; `esc` leaves it for the table.

## Settings

ttyplayer keeps its preferences in `~/.config/ttyplayer/settings.toml` (`$XDG_CONFIG_HOME/ttyplayer/` when that is set, `%APPDATA%\ttyplayer\` on Windows). Every key is optional; a missing file means the defaults.

| Key | Default | What it does |
|---|---|---|
| `show_clock` | `true` | the clock in the TUI's header |
| `theme` | `textual-dark` | the TUI's color theme; `t` in the TUI picks the next one and saves it |
| `search_limit` | `10` | how many results a TUI search fetches, and `m` adds (1–50) |
| `server_host` | `127.0.0.1` | the address `ttyplayer serve` listens on; `0.0.0.0` opens it to the network |
| `server_port` | `7700` | the port `ttyplayer serve` listens on (1–65535) |
| `server_token` | *generated* | the token every API request needs; `ttyplayer serve` makes one on first use |

```
ttyplayer config                     every setting, (default) when unchanged
ttyplayer config get <key>           one setting's value
ttyplayer config set <key> <value>   change it: ttyplayer config set show_clock false
ttyplayer config path                where the file is
```

In the TUI, `S` (or Settings… in Ctrl-P) lists the settings: Enter on a true / false one flips it and saves it (the clock shows or hides at once); the others are set with `ttyplayer config set`.

## Server

`ttyplayer serve` runs the player headless on the machine with the speakers (no keyboard, no TUI) and lets any device on the network drive it through a small HTTP + WebSocket API. The other terminals' `ttyplayer pause`, `next`, `status` and `stop` keep working too.

```
ttyplayer serve                      listen on 127.0.0.1:7700 (this machine only)
ttyplayer serve --host 0.0.0.0       listen on every address, so a phone on the LAN can reach it
ttyplayer serve --port 8000          another port
```

It prints the address to open, with the token, and a QR code of it for a phone:

```
Serving ttyplayer at http://192.168.1.20:7700/?token=…
```

Open that address on a phone and it is a remote: what plays (with a moving progress bar), previous / play-pause / next, a volume slider and mute, a search box (a pasted link plays at once; each result has **Play** and **Queue**), the queue (tap a row to jump there, **×** removes it, **Clear** keeps only what plays), your favorites (the heart on any row adds or drops one) and your playlists. It follows the phone's dark or light mode, reconnects by itself when the server restarts, and "Add to Home Screen" makes it an app icon. The page keeps the token only for that browser tab; opened without one (from the home screen, say) it asks you to paste the token `serve` printed. Ctrl-C (or `ttyplayer stop`, or a `stop` command) stops the server and the player.

Every `/api/…` request and the socket need the token, as `Authorization: Bearer <token>` or `?token=<token>`; without it the reply is `401 {"error": "unauthorized"}`. Replies are JSON; errors are `{"error": "…"}`.

```
curl -H "Authorization: Bearer $TOKEN" http://host:7700/api/status
curl -H "Authorization: Bearer $TOKEN" -d '{"query": "lofi beats"}' http://host:7700/api/play
curl -H "Authorization: Bearer $TOKEN" -d '{"name": "volume", "value": -5}' http://host:7700/api/command
```

| Method | Path | Body / reply |
|---|---|---|
| GET | `/api/status` | the player's status, plus `queue` (the videos) and `index` (1-based) |
| POST | `/api/play` | `{"url": "…"}` or `{"query": "…"}`: the link's videos, or the first search result, replace the queue and play |
| POST | `/api/queue` | `{"url": "…"}` or `{"query": "…"}`: appended to the queue (played at once when nothing plays) |
| POST | `/api/command` | `{"name": "pause"\|"next"\|"prev"\|"stop"\|"mute"\|"seek"\|"volume"\|"jump"\|"remove"\|"clear_others", "value"?}`; `seek` and `volume` take a number of seconds / steps, `jump` and `remove` a 0-based queue row; `clear_others` keeps only the current track → the new status |
| GET | `/api/commands` | the command names `/api/command` takes |
| GET | `/api/search?q=…` | the search results (`search_limit` of them) |
| GET | `/api/favorites` | the favorites, newest first |
| POST | `/api/favorites/<id>` | unfavorites that video, or favorites it (the body is the video: `{"title", "uploader", "duration"}`) → the favorites |
| GET | `/api/playlists` | `[{"name": …, "count": …}]` |
| POST | `/api/playlists/<name>/play` | that playlist becomes the queue |
| GET, PATCH | `/api/settings` | every setting but the token; PATCH `{"key": value}` changes and saves them, checked like `config set` |
| WS | `/ws?token=…` | sends the status on connect and on every change; takes the same `{"name", "value"?}` commands as `/api/command` and answers each with the status |
| GET | `/` | the web remote (needs no token itself; it reads the token from its address); its files are under `/static/`, plus `/manifest.webmanifest` |

Security: the token is the only gate, and plain HTTP carries it in clear text. That is fine on a home network you trust; it is why `serve` listens on 127.0.0.1 unless told otherwise. To reach it beyond your LAN (a VPS, say), put it behind a reverse proxy with HTTPS. Anyone with the token can control the player, including stopping it; change the token with `ttyplayer config set server_token <new>` and restart.

## Install

One command installs everything ttyplayer needs.

macOS and Linux:

```
curl -LsSf https://raw.githubusercontent.com/webliftro/ttyplayer/main/install.sh | sh
```

Windows (PowerShell):

```
irm https://raw.githubusercontent.com/webliftro/ttyplayer/main/install.ps1 | iex
```

Or clone the repository and run `./install.sh` (macOS, Linux) or `powershell -ExecutionPolicy ByPass -File .\install.ps1` (Windows) inside it; that installs the checkout you cloned.

The script:

1. installs [uv](https://docs.astral.sh/uv/) with its official installer if `uv` is missing (uv brings its own Python 3.13+; it runs as you, without sudo),
2. installs mpv if it is missing: Homebrew on macOS, your package manager on Linux (`apt-get`, `dnf`, `pacman`, `zypper` or `apk`, with `sudo`), winget on Windows,
3. installs ttyplayer with `uv tool install`,
4. runs `ttyplayer doctor`: every line should start with ✓.

It prints every command before running it. `TTYPLAYER_INSTALL_DRY_RUN=1 ./install.sh` and `install.ps1 -DryRun` only print them. If Homebrew (macOS) or winget (Windows) is missing, the script tells you how to install it and stops. If `ttyplayer` is not found in a new terminal, run `uv tool update-shell`.

<details>
<summary>By hand</summary>

Three steps on every system: install uv, install mpv, install ttyplayer. Then run `ttyplayer doctor`.

macOS:

```
curl -LsSf https://astral.sh/uv/install.sh | sh
brew install mpv
uv tool install ttyplayer
```

Linux:

```
curl -LsSf https://astral.sh/uv/install.sh | sh
sudo apt-get install -y mpv
uv tool install ttyplayer
```

On Fedora use `sudo dnf install -y mpv`, on Arch `sudo pacman -S --noconfirm mpv`, on openSUSE `sudo zypper install -y mpv`, on Alpine `sudo apk add mpv`.

Windows:

```
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
winget install -e --id shinchiro.mpv
uv tool install ttyplayer
```

Instead of winget, `scoop install mpv` (from the `extras` bucket) or `choco install mpv` work too.

`uv tool install ttyplayer` installs from PyPI once ttyplayer is published there. Until then install straight from GitHub:

```
uv tool install git+https://github.com/webliftro/ttyplayer
```

</details>

Upgrade with `uv tool upgrade ttyplayer`. Uninstall with `uv tool uninstall ttyplayer`.

## Development

Install from a checkout, with mpv installed as above:

```
uv tool install --editable .
```

The `--editable` flag means edits to the source are live without reinstalling.

```
uv run pytest        # unit tests, no network and no mpv needed
uv run ttyplayer ...   # run from the checkout
```

The live tests talk to the real YouTube through `ttyplayer.youtube`, so a yt-dlp or YouTube change shows up as a failing test. They are skipped unless `TTYPLAYER_LIVE=1` is set:

```
uv run pytest -q                                      # default suite, live tests skipped
TTYPLAYER_LIVE=1 uv run pytest -q tests/test_live.py    # live tests, needs network
```

History lives in `$XDG_DATA_HOME/ttyplayer/history.jsonl`, by default `~/.local/share/ttyplayer/history.jsonl` (`%LOCALAPPDATA%\ttyplayer\history.jsonl` on Windows). Favorites live next to it in `favorites.jsonl`, and each playlist in `playlists/<name>.jsonl`. History and favorites kept under the player's earlier name are moved here on the first run.

### Releasing

CI (`.github/workflows/tests.yml`) runs the suite on Linux and macOS on every push. Pushing a `v*` tag runs `.github/workflows/release.yml`, which builds with `uv build` and publishes to PyPI through trusted publishing, so there is no token to store.

One-time setup on PyPI: create an account, then under "Publishing" add a pending GitHub publisher for the project `ttyplayer` with owner `webliftro`, repository `ttyplayer` and workflow `release.yml`. The first tagged release creates the project.

To release, bump the version (`uv version --bump minor`, or edit `version` in `pyproject.toml`), commit, then:

```
git tag v0.3.0 && git push --tags
```

## How it works

yt-dlp resolves what to play: a search or a playlist becomes a list of videos, a link becomes one. mpv produces the sound, started in the background with a private IPC socket. ttyplayer talks to it in JSON over that socket, owns the keyboard, and redraws a one-line status. See `docs/architecture.md`.

## License

MIT. See `LICENSE`.
