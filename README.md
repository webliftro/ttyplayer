# ttyplayer

A modern YouTube player for the terminal. Search YouTube, queue videos and playlists, and play audio, or video too, through mpv: from the command line, or from a full-screen TUI. Keep a history and favorites, and drive the player from any other terminal.

## Usage

```
ttyplayer play <url>                 play a video link, audio only
ttyplayer play <playlist url>        queue every entry of a playlist
ttyplayer play <words...>            search, pick one or more results, play them in order
ttyplayer play ... --video           open a video window as well
ttyplayer play ... --limit 10        show more search results
ttyplayer play ... --source soundcloud   search SoundCloud instead of YouTube
ttyplayer play ... --radio           when the queue runs out, go on with related tracks

ttyplayer search <words...>          list results with durations
ttyplayer search --source soundcloud <words...>   the same, on SoundCloud
ttyplayer history                    recently played, newest first
ttyplayer history --play             pick from history and play again
ttyplayer history --clear            forget the history
ttyplayer favorite <url | words...>  add a link, a playlist, or search picks to favorites
ttyplayer favorites                  favorites, newest first
ttyplayer favorites --play           pick from favorites and play them
ttyplayer favorites --remove 2       drop the second favorite as listed
ttyplayer favorites --clear          forget all favorites
ttyplayer playlist ...               your own named playlists (see Playlists below)
ttyplayer spotify ...                bring Spotify playlists over, found on YouTube (see Spotify below)
ttyplayer tui [--video]              full-screen: search box, results list, now-playing bar
ttyplayer serve [--host] [--port]    play headless, driven over HTTP from any device (see Server below)
ttyplayer serve --stream             the same, but the sound goes to the web remote, not the speakers
ttyplayer config                     list the settings (see Settings below)
ttyplayer doctor                     check Python, yt-dlp, mpv, ffmpeg (optional) and ttyplayer's folders
ttyplayer version
```

Channels and playlists in search results are skipped; a track that cannot be played is reported and skipped.

Search looks on YouTube or SoundCloud. `--source soundcloud` (on `play`, `search` and `playlist add`) picks SoundCloud for one search; `ttyplayer config set search_source soundcloud` makes it the default everywhere. SoundCloud rows carry an `SC` tag in every list, and history, favorites and playlists keep their SoundCloud link. Links from any site yt-dlp knows play as they are.

While ttyplayer plays in one terminal, any other terminal can drive it:

```
ttyplayer pause                      pause / resume
ttyplayer next                       next in the queue
ttyplayer prev                       previous in the queue
ttyplayer stop                       quit the player
ttyplayer sleep 30m                  Sleeping in 30:00 (also 1h, 1h30m, 90 seconds)
ttyplayer sleep end                  stop when the current track ends
ttyplayer sleep off                  cancel it (no argument: show it)
ttyplayer radio on                   go on with related tracks when the queue runs out
ttyplayer radio off                  stop at the end of the queue again (no argument: show it)
ttyplayer status                     1:23 / 4:56  Playing  <title>
ttyplayer lyrics                     the playing track's lyrics, from LRCLIB
```

When the sleep timer runs out, the volume fades to nothing over 5 seconds and the player stops as if `q` was pressed; the volume is put back first, so the next play starts at the old level. Paused or idle, it stops at once. `ttyplayer status`, the TUI and the web remote show `zz 27:13` (or `zz end`) while a timer is armed.

Radio mode keeps the music going: when the last queued track ends, ttyplayer looks up YouTube's mix for it, skips what you played recently (the last 50 in history) and what is already queued, appends the next 5 tracks and plays them; at their end it does it again. `play --radio` turns it on for one run, `ttyplayer radio on|off` for the running player, and `ttyplayer config set radio true` for every new player. While it looks tracks up, `ttyplayer status`, the TUI and the web remote show `∞ fetching…` (a second or two), else `∞` while it is on. Radio goes on only from a YouTube track: after a SoundCloud one it says so and stops, as does a lookup that fails or finds nothing new.

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
| left / right, `,` / `.` | seek 5 seconds (`seek_seconds`) |
| up / down, `-` / `+` | volume, 5 at a time (`volume_step`) |
| `n` / `p` | next / previous in the queue |
| `q` or Ctrl-C | quit, restores the terminal and stops mpv |

On Windows (Windows Terminal, PowerShell) the keys, arrows included, map the same way.

The volume meter, and the volume keys here and in the TUI, follow the Mac's system volume on macOS (the Mac's own volume keys move the meter within 2 seconds) and mpv's device (per-app) volume on Linux and Windows.

Picks accept several numbers at once: `1 3 5` queues those three in that order. After a search, `m` lists the next batch of results.

`TTYPLAYER_TIMING=1 ttyplayer play <words>` prints how long the YouTube lookup took and adds `started in 2.4s` (from `loadfile` to the first sound) to the status line.

## Spotify

ttyplayer can bring a Spotify playlist over as one of its own playlists. It reads only the playlist's *track list* from Spotify, never its audio (Spotify's audio is DRM-protected): each track is searched on YouTube as `<first artist> <title>`, and the first hit is added. A track YouTube has no hit for is reported and skipped; local files and podcast episodes in the playlist are skipped too. The result is a normal playlist: `ttyplayer playlist play <name>`, or the Playlists tab in the TUI.

```
ttyplayer spotify login                                      log in to Spotify in the browser, once
ttyplayer spotify playlists                                  your Spotify playlists: name, tracks, id
ttyplayer spotify import <link | id> [--as <name>] [--limit N]  find each track on YouTube and save it
```

`import` takes a playlist link (`https://open.spotify.com/playlist/<id>`), a `spotify:playlist:<id>` URI, or the bare id. It names the playlist after the Spotify one (unless `--as`), creating it, or appending to it if one by that name exists; it prints `[3/40] ✓ Artist – Title → <YouTube title>` (or `✗ … not found`) per track, and `Saved N of M tracks to <name>` at the end. Each YouTube search takes a second or two, so a long playlist takes a while; `--limit N` imports only the first N tracks. If the network fails part way, what was saved stays saved.

Logging in needs a Spotify app of your own (free, once):

1. Open the [Spotify developer dashboard](https://developer.spotify.com/dashboard), log in, and press **Create app**.
2. Give it any name and description, set the **Redirect URI** to exactly `http://127.0.0.1:8765/callback`, tick **Web API**, and save.
3. Open the app's **Settings**, copy its **Client ID**, and run `ttyplayer config set spotify_client_id <client id>`.
4. Run `ttyplayer spotify login`: the browser opens Spotify's consent page; approve it, and the terminal says `Logged in as <your name>`.

ttyplayer asks only for read access to your playlists (`playlist-read-private playlist-read-collaborative`) and uses no client secret (the login is OAuth with PKCE). The login is kept in `spotify.json` next to `settings.toml`, readable only by you; it holds a refresh token, so treat it like a password. To revoke ttyplayer's access, remove the app under **Manage apps** on your Spotify account page (spotify.com/account/apps) and delete `spotify.json`.

## TUI

`ttyplayer tui` opens a full-screen player: a search box, Search / Queue / History / Favorites / Playlists / Lyrics tabs, and a now-playing panel. Type a search or paste a link and press Enter; start the search with `sc:` (`sc: boards of canada`) to search SoundCloud, or `yt:` for YouTube, whatever `search_source` says. The Search tab's heading then reads `SoundCloud results` or `YouTube results`. Ctrl-P opens the command palette (search, playlists, save queue as playlist, next theme, settings, help, quit, pause, next, previous, mute, sleep, radio, and Textual's own theme picker); **Sleep…** asks for a sleep timer (`30m` filled in; `end`, `off`, `1h30m`, … work as on the command line; Esc leaves the timer alone); `?` lists every key and command.

Under the volume, the panel's level meter shows two bars, `L` and `R`, that follow the sound's peaks about ten times a second (empty at −60 dBFS and below, full at 0 dBFS; empty while paused). `ttyplayer config set show_levels false`, or Enter on `show_levels` in the Settings screen, hides them and takes mpv's measuring filter out.

Looks: left of those lines the panel shows the playing track's cover, its YouTube thumbnail or SoundCloud artwork, in a 10-column box. Terminals that draw pictures (Kitty, WezTerm, iTerm2 and other Sixel ones) show the real image; elsewhere, Terminal.app for one, it is a mosaic of colored half-blocks. Covers come with the `art` extra, which the install scripts include; `ttyplayer doctor` says whether you have it. Each cover is downloaded once and kept in the data dir's `art` folder (the 200 most recent). `ttyplayer config set show_art false`, or Enter on `show_art` in the Settings screen, removes the box.

Read along: the Lyrics tab (`6`) shows the playing track's lyrics, the line being sung in the accent color and kept in the middle as the song goes on (lyrics without timings just scroll). They come from [LRCLIB](https://lrclib.net), a free, open lyrics database: ttyplayer guesses the artist and title from the video's title and uploader (`Artist - Song (Official Video)`, `Song | Artist`, an `Artist - Topic` channel, …) and asks LRCLIB once per track, only while the tab is shown; the answer, a miss included, is kept in the data dir's `lyrics` folder. With nothing found the tab says `No lyrics found for "<artist> – <title>"`, so you see what it looked for. `ttyplayer lyrics` prints the same lyrics in another terminal. `ttyplayer config set show_lyrics false`, or Enter on `show_lyrics` in the Settings screen, stops the lookups (the tab says `Lyrics are off (show_lyrics)`). Lyrics courtesy of LRCLIB; ttyplayer sends it only the guessed artist, title and length.

| Where | Key | Action |
|---|---|---|
| table | `↑` / `↓` | move through the list (volume is `-` / `+`) |
| anywhere | `/` | focus the search box (`esc` returns to the table) |
| anywhere | `?` | help: every key and command (`esc` closes) |
| anywhere | `1` `2` `3` `4` `5` `6` | Search / Queue / History / Favorites / Playlists / Lyrics tab |
| anywhere | Ctrl-C | quit and stop mpv |
| anywhere | Ctrl-P | command palette |
| anywhere | `t` | next theme (remembered for next time) |
| table | `P` | save the queue as a playlist (asks for a name; an existing playlist of that name is replaced) |
| table | `S` | settings: Enter flips a true / false one or asks for a number, `esc` closes |
| table | `R` | radio on / off (`∞` in the panel; `∞ fetching…` while it looks up related tracks) |
| table | `q` | quit and stop mpv (in the search box it is just a letter) |
| table | `space` | pause / resume |
| table | `n` / `p` | next / previous |
| table | `,` / `.` | seek −5 s / +5 s (`seek_seconds`; `?` shows the configured number) |
| table | `<` / `>` | seek −30 s / +30 s |
| table | `-` / `+` | volume −5 / +5 (`volume_step`) |
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
| `remote_url` | *none* | the server `ttyplayer tui` drives instead of playing itself, e.g. `http://host:7700` |
| `stream_enabled` | `false` | `ttyplayer serve` streams the sound to the web remote instead of playing it, as `--stream` does |
| `spotify_client_id` | *none* | the Client ID of your own Spotify app, which `ttyplayer spotify login` needs (see Spotify above) |
| `show_levels` | `true` | the TUI's level meter: mpv measures the sound's peaks and the now-playing panel shows them |
| `show_art` | `true` | the track's cover in the TUI's now-playing panel (needs the `art` extra; without it nothing changes) |
| `show_lyrics` | `true` | the TUI's Lyrics tab looks the playing track up on LRCLIB; `false` turns the lookups off |
| `search_source` | `youtube` | where a search looks: `youtube` or `soundcloud`; `--source` and the TUI's `sc:` / `yt:` prefix override it once |
| `radio` | `false` | a new player goes on with related YouTube tracks when its queue runs out, as `play --radio` does |
| `normalize_loudness` | `false` | mpv evens out loud and quiet tracks (a `loudnorm` filter to −16 LUFS); the stream of `serve --stream` gets it too |
| `prefetch` | `true` | mpv buffers the queue's next track while the current one plays and goes on to it with no gap (gapless where the source allows); `false` loads each track only when the last one ends |
| `seek_seconds` | `5` | how far `,` / `.` (and left / right in `ttyplayer play`) seek (1–300) |
| `volume_step` | `5` | how much `-` / `+` (and up / down in `ttyplayer play`) change the volume (1–50) |

```
ttyplayer config                     every setting, (default) when unchanged
ttyplayer config get <key>           one setting's value
ttyplayer config set <key> <value>   change it: ttyplayer config set show_clock false
ttyplayer config path                where the file is
```

In the TUI, `S` (or Settings… in Ctrl-P) lists the settings: Enter on a true / false one flips it and saves it (the clock, the level meter and the cover show or hide at once, the loudness filter goes in or out of the running mpv); Enter on a number asks for a new one, checked as `config set` checks it; the others are set with `ttyplayer config set`. A `seek_seconds` / `volume_step` changed there applies from the next key press, and the help shows the new step.

## Server

`ttyplayer serve` runs the player headless on the machine with the speakers (no keyboard, no TUI) and lets any device on the network drive it through a small HTTP + WebSocket API. The other terminals' `ttyplayer pause`, `next`, `status` and `stop` keep working too.

```
ttyplayer serve                      listen on 127.0.0.1:7700 (this machine only)
ttyplayer serve --host 0.0.0.0       listen on every address, so a phone on the LAN can reach it
ttyplayer serve --port 8000          another port
ttyplayer serve --stream             no sound here: Listen here on the web remote plays it (see below)
```

It prints the address to open, with the token, and a QR code of it for a phone:

```
Serving ttyplayer at http://192.168.1.20:7700/?token=…
```

Open that address on a phone and it is a remote: what plays (with a moving progress bar), previous / play-pause / next, a volume slider and mute, a search box (a pasted link plays at once; each result has **Play** and **Queue**), the queue (tap a row to jump there, **×** removes it, **Clear** keeps only what plays), your favorites (the heart on any row adds or drops one) and your playlists. The card shows the track's art and the two level bars, and a **Lyrics** section (closed until you open it; the page remembers) follows the song as the TUI's Lyrics tab does, the current line highlighted. The art comes to the phone straight from YouTube or SoundCloud, so on a network without internet it shows a placeholder. It follows the phone's dark or light mode, reconnects by itself when the server restarts, and "Add to Home Screen" makes it an app icon. The page keeps the token only for that browser tab; opened without one (from the home screen, say) it asks you to paste the token `serve` printed. Ctrl-C (or `ttyplayer stop`, or a `stop` command) stops the server and the player.

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
| POST | `/api/command` | `{"name": "pause"\|"next"\|"prev"\|"stop"\|"mute"\|"sleep"\|"radio"\|"seek"\|"volume"\|"jump"\|"remove"\|"move"\|"clear_others", "value"?}`; `sleep` takes the text `ttyplayer sleep` does (`"30m"`, `"end"`, `"off"`), `radio` `"on"`, `"off"` or `"toggle"`, `seek` and `volume` a number of seconds / steps, `jump` and `remove` a 0-based queue row, `move` two (`[from, to]`; the current track stays current); `clear_others` keeps only the current track → the new status |
| GET | `/api/commands` | the command names `/api/command` takes |
| GET | `/api/search?q=…` | the search results (`search_limit` of them) |
| GET | `/api/favorites` | the favorites, newest first |
| POST | `/api/favorites/<id>` | unfavorites that video, or favorites it (the body is the video: `{"title", "uploader", "duration"}`) → the favorites |
| GET | `/api/lyrics` | the playing track's lyrics: `{"artist", "track", "synced": [[seconds, text], …] or null, "plain": text or null, "source_url"}` (both null: none found); 404 when nothing plays or `show_lyrics` is false |
| GET | `/api/playlists` | `[{"name": …, "count": …}]` |
| POST | `/api/playlists/<name>/play` | that playlist becomes the queue |
| GET, PATCH | `/api/settings` | every setting but the token; PATCH `{"key": value}` changes and saves them, checked like `config set` |
| WS | `/ws?token=…` | sends the status on connect and on every change; takes the same `{"name", "value"?}` commands as `/api/command` and answers each with the status |
| GET | `/stream` | with `serve --stream`: the sound as an `audio/ogg` (Opus) stream, for as long as the client listens; 404 otherwise |
| GET | `/` | the web remote (needs no token itself; it reads the token from its address); its files are under `/static/`, plus `/manifest.webmanifest` |

### Listen on another device

`ttyplayer serve --stream` is the "music box on a server" mode, for a machine with no speakers of its own (a Raspberry Pi, a VPS, a closet PC): nothing plays on the server; the web remote gets a **Listen here** button that plays what the server plays, in that browser, on any device. Press it again (**Stop listening**) to stop. Several devices can listen at once; one on a bad connection skips, the others do not. Pausing and changing tracks keep the stream open (you hear silence in between). What you hear runs a little behind the remote's controls: the server adds about half a second, the browser its own buffer. `ttyplayer config set stream_enabled true` makes it the default for `serve`.

```
ttyplayer serve --stream --host 0.0.0.0
```

It needs ffmpeg, which nothing else in ttyplayer does (`ttyplayer doctor` shows whether it is there):

| System | Install ffmpeg |
|---|---|
| macOS | `brew install ffmpeg` |
| Debian, Ubuntu, Raspberry Pi OS | `sudo apt-get install -y ffmpeg` |
| Fedora | `sudo dnf install -y ffmpeg-free` |
| Arch | `sudo pacman -S --noconfirm ffmpeg` |
| Alpine | `sudo apk add ffmpeg` |
| Windows | `winget install -e --id Gyan.FFmpeg` (or `scoop install ffmpeg`, `choco install ffmpeg`) |

The stream is gated by the same token as the API and is plain HTTP; the security notes below apply to it too.

### The TUI as a remote

`ttyplayer tui --remote http://host:7700` opens the same screen, with the same keys and tabs, on another machine (a laptop, say) while the server plays: Enter on a result makes the *server's* speakers play, and the now-playing panel and the Queue tab follow the server over its socket (the header says `remote: host:7700`). The token is the `server_token` setting, so copy it into the laptop's settings with `ttyplayer config set server_token <token>`; `--token <token>` works too, but leaves it in your shell history. `ttyplayer config set remote_url http://host:7700` makes the remote the default, so a plain `ttyplayer tui` drives it.

Searching still happens on the laptop; the server looks each picked video up again before it plays or queues it, so a long queue fills in over a few seconds. History stays the server's: the laptop records nothing. When the server cannot be reached the TUI says so once and keeps trying; a refused token says `Server rejected the token`.

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

With Homebrew (macOS), once the `webliftro/homebrew-tap` tap exists, one command installs ttyplayer and mpv:

```
brew install webliftro/tap/ttyplayer
```

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

For covers in the TUI, as the install scripts set up, install `'ttyplayer[art]'` in place of `ttyplayer` (it adds Pillow and textual-image).

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

### Packaging

After the PyPI upload, `release.yml` regenerates the Homebrew formula with `scripts/brew_formula.py <version>` (standard library only: the released sdist and every runtime dependency's sdist, with versions from `uv.lock`, url and sha256 from PyPI) and commits it to `webliftro/homebrew-tap` as `Formula/ttyplayer.rb`. That needs the empty repository `webliftro/homebrew-tap` and a token with write access to it, stored as the Actions secret `HOMEBREW_TAP_TOKEN`; without the secret the formula is attached to the workflow run as an artifact. To try a formula by hand:

```
mkdir -p Formula && python3 -I scripts/brew_formula.py 0.8.0 > Formula/ttyplayer.rb
brew install --build-from-source ./Formula/ttyplayer.rb
```

## How it works

yt-dlp resolves what to play: a search or a playlist becomes a list of videos, a link becomes one. mpv produces the sound, started in the background with a private IPC socket. ttyplayer talks to it in JSON over that socket, owns the keyboard, and redraws a one-line status. See `docs/architecture.md`.

## License

MIT. See `LICENSE`.
