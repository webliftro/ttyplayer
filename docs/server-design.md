# ttyplayer server — design

The one place that says how `ttyplayer serve` works: a player that runs where the speakers are and
is driven from any device on the network (phone, laptop, another ttyplayer), and — in a second
step — can stream what it plays to a device that has no speakers of its own nearby.

## Goal

1. **Control from any device.** `ttyplayer serve` runs the player headless (no keyboard, no TUI),
   publishes a small HTTP + WebSocket API on the LAN, and serves a phone-sized web remote. The TUI
   can attach to a server instead of starting its own mpv (`ttyplayer tui --remote host`).
2. **Hear it on another device.** The server can also expose the audio as an HTTP stream the web
   remote plays in the browser. Optional, decided by a spike (latency, CPU, ffmpeg dependency).

Everything stays on the same `MpvClient`, `youtube`, `history`, `favorites`, `playlists`,
`settings` and `control` modules; the server is a new front end, like the TUI.

## Shape

```
 phone browser ──HTTP/WS──┐
 laptop ttyplayer tui ────┤──► ttyplayer serve ──► MpvClient ──► mpv ──► speakers
 ttyplayer pause (CLI) ───┘        │                   │
        (loopback token)           └─ /stream (opus) ◄─┘  (step 2, via ffmpeg)
```

- `ttyplayer serve [--host 0.0.0.0] [--port 7700]` (the token lives in settings `server_token`): starts `MpvClient` with
  `on_state` → broadcast, `control.serve` (so the local CLI still works), and the HTTP server.
- Transport: Python stdlib `http.server` + `wsgiref`? No — WebSockets need a real library. Use
  **`aiohttp`** (one dependency, asyncio, HTTP + WS + static files in one), run in its own thread
  so the player's threads are untouched.
- Auth: a bearer token (settings `server_token`, generated on first `serve` and printed with a QR
  code in the terminal for the phone); every request carries it (`Authorization: Bearer …` or
  `?token=` for the first page load, which then stores it). Loopback without token stays the
  local control socket's job.
- Discovery: `serve` prints `http://<lan-ip>:7700/?token=…` and the QR code; mDNS later.

## API (v1)

| Method | Path | Body / reply |
|---|---|---|
| GET | `/api/status` | `status()` as JSON (+ `queue`: list, `index`) |
| POST | `/api/play` | `{"query": "…"}` or `{"url": "…"}` → resolves and replaces the queue |
| POST | `/api/queue` | `{"url"|"query"}` → appends |
| POST | `/api/command` | `{"name": "pause"|"next"|"prev"|"stop"|"mute"|"sleep"|"seek"|"volume"|"jump"|"remove"|"move"|"clear_others", "value"?}` → the new status; `sleep` takes `ttyplayer sleep`'s text (`"30m"`, `"end"`, `"off"`; a bad one is a 400 with `parse_sleep()`'s error) and goes through `handle_control("sleep <text>")`, as the control socket's does; `jump`/`remove` take a 0-based queue row, `move` two (`[from, to]`) |
| GET | `/api/commands` | the command table's names (the page's and the server's tests both read it) |
| GET | `/api/favorites` | the favorites, newest first; each video carries `url`, what its Play/Queue send |
| POST | `/api/favorites/{id}` | toggles: unfavorites, or favorites the video in the body → the favorites |
| GET | `/api/search?q=` | `youtube.search` results (from `search_source`), each with its `url` |
| GET/POST | `/api/playlists…` | list / play a playlist |
| GET | `/api/settings`, PATCH | read / change settings |
| WS | `/ws` | server → client: every `on_state` status as one JSON message (with `queue` when it changed since the last one); client → server: the same commands as `/api/command` |
| GET | `/` | the web remote (one HTML file + one JS file + one CSS file, no build step); `/static/*`, `/manifest.webmanifest` |
| GET | `/stream` | `serve --stream` only (404 otherwise): `audio/ogg` Opus stream, token-gated like `/api` (`?token=` for an `<audio src>`) |

All POSTs are idempotent-ish and reply with the new status.

## Web remote

One static page (vanilla JS, no framework, no build): search box, results, queue, big now-playing
card (title, uploader, progress bar, time, volume slider, play/pause/next/prev/mute, `zz 27:13` and a
Sleep 30m / Sleep off button for the sleep timer), favorites and
playlists lists. Dark/light follows the phone. Works from the phone's browser and as a home-screen
app (manifest). The page talks WS for state and `fetch` for actions.

## TUI as a client

`ttyplayer tui --remote http://host:7700` (or settings `remote_url`) uses a `RemoteClient` that
implements the same methods `TtyplayerApp` calls on `MpvClient` (`add`, `play_current`, `next`,
`prev`, `jump`, `remove`, `move`, `clear_others`, `toggle_pause`, `seek`, `change_volume`,
`toggle_mute`, `status`, `queue`, `index`) over the API, with `on_state` fed by the WebSocket. The
TUI code does not change; the factory does.

## Audio stream (step 2) — measured, decided

**Decision (stream-spike, 2026-10-09): ship candidate (3), the headless "music box" mode.**
`ttyplayer serve --stream` (or `stream_enabled = true`) starts mpv with `--ao=pcm` writing raw PCM
into a pipe to ttyplayer (see the two PCM sources below), so **nothing plays on the server**; ffmpeg encodes it to Ogg Opus and `/stream`
serves it to every listener. Candidates (1) and (2) are not built.

| | (1) mpv encodes | (2) virtual device + ffmpeg capture | (3) `mpv --ao=pcm` → ffmpeg → `/stream` |
|---|---|---|---|
| Works? | **no** for a live stream: mpv has no encoding *audio output*; its encoding mode (`--o=…`) is a file transcoder (no pacing, the output ends with the playlist, mpv calls it experimental) | **needs setup**: works wherever the user installs a loopback device; zero code in mpv | **yes**, measured below; Linux and macOS (mpv needs `/dev/stdout`), not Windows |
| Platforms | — | macOS (BlackHole), Linux (PulseAudio/PipeWire null sink), Windows (VB-Cable) | Linux, macOS |
| Local sound | — | yes (mpv plays to the device; the user monitors it) | **no**: the server is silent |
| Latency, server side (method below) | not measurable here (no mpv) | not measurable here (no sound server in the sandbox; the static ffmpeg has no `pulse` input) | **81 ms median, 107 ms p95, 128 ms max**; first byte to a new listener 3–9 ms |
| Latency the listener hears | — | — | the above + the 341 ms the PCM pipe lets mpv run ahead + the browser's own buffer: **developer to measure** (expected 0.5–2 s) |
| CPU (one 20-second run, Linux aarch64, % of one core) | — | — | ffmpeg (libopus 128k) **7–8 %**; ttyplayer's pacer + reader + server **2 %** with 1 listener, **7 %** with 10 (both include the test clients, which ran in the same process) |
| What the user installs | — | the loopback driver + ffmpeg, and route mpv to it by hand | **ffmpeg** (`brew install ffmpeg`, `sudo apt-get install -y ffmpeg`, …); `ttyplayer doctor` reports it |

**How it was measured (Linux sandbox, no mpv, no sound device).** ffmpeg's `-f lavfi -i sine`
stood in for mpv, writing s16le 48 kHz stereo to a pipe as fast as the pipe took it (as mpv's
`--ao=pcm` does); the real `stream.Streamer` paced it into ffmpeg (from `imageio-ffmpeg`, a static
7.0.2 build, used for the measurement only) and the real aiohttp app served `/stream` to 1, 2 and
10 HTTP clients for 15 s each. Latency per Ogg page = the time it reached a client − the time the
pacer handed ffmpeg the page's last sample (its granule position − the 312-sample pre-skip). A
client joining 3 s late received a stream ffmpeg decodes without errors as the 440 Hz tone.

What the ffmpeg arguments are for, measured the same way:

| ffmpeg arguments | first byte | latency (median) |
|---|---|---|
| the spec's: `-f s16le -ar 48000 -ac 2 -i - -c:a libopus -b:a 128k -f ogg -` | 3.3 s | 1.06 s (the Ogg muxer's default 1 s pages) |
| + `-probesize 32 -analyzeduration 0` | 4 ms | 1.07 s |
| + `-page_duration 20000 -flush_packets 1` (what ships) | 3–9 ms | 81 ms |

**How (3) works.** mpv's `--ao=pcm` does not wait for a sound card, so a pacer thread hands ffmpeg
one 20 ms chunk per 20 ms of clock, and **silence while mpv has none** (paused, between tracks, idle),
so a listener's stream never stalls and never bursts. One reader thread splits ffmpeg's output into
Ogg pages and offers each to every listener's bounded queue (5 s); a slow listener loses pages, the
others do not notice. The Opus header pages are kept and sent first to every listener who joins
later. Stopping the server stops mpv first (its pipe ends), then ffmpeg.

**Two PCM sources (stream-windows, 2026-10-10).** The pacer reads mpv through a `stream.PcmSource`;
`stream.pcm_source()` picks one per OS. On macOS and Linux it is `StdoutPipe`: mpv writes to
`/dev/stdout`, a pipe polled with `select`, as measured above. Windows has neither `/dev/stdout`
nor `select` on pipes, so `NamedPipe` creates an inbound pipe `\\.\pipe\ttyplayer-pcm-<pid>-<random>`
(stdlib `_winapi`, one instance, created as the name's first so a squatter makes it fail rather
than share it), gives mpv that path, and polls it with `PeekNamedPipe`. mpv opens the file only
once a track plays and may close it when playback stops, so the pipe waits for mpv without
blocking (silence meanwhile) and is created anew whenever mpv closes it. The wait is bounded by
track time, not wall time: once a track has sounded (past its `playback-restart`, not paused) for
`CONNECT_TIMEOUT` (5 s) with the pipe still unopened, the pacer prints one line
(`Streaming stopped: mpv did not open the pipe …`) and the stream ends, rather than sending
silence forever. Closing the pipe cancels a pending connect and waits for Windows to finish it
before the handle closes.

**For the developer (needs macOS, speakers, two devices):**

1. The real pipeline with mpv, and its CPU, on the Mac and on a Linux box / Raspberry Pi:
   ```
   ttyplayer serve --stream --host 0.0.0.0
   # open the printed address on a phone, play something, press Listen here
   top -pid $(pgrep -n ffmpeg)        # macOS;  Linux: top -p $(pgrep -n ffmpeg)
   ```
2. What the listener hears late: play a track with a sharp start (or press pause) on the phone's remote while
   listening, and time from the tap to the sound stopping (a stopwatch or a slow-motion video of
   the phone). Note the browser (Safari on iOS, Chrome on Android).
3. Candidate (2), only to fill its row, on macOS:
   ```
   brew install blackhole-2ch ffmpeg
   mpv --audio-device=help | grep -i blackhole        # its device name, for the next line
   mpv --audio-device='coreaudio/<that name>' 'https://www.youtube.com/watch?v=…' &
   ffmpeg -f avfoundation -i ":BlackHole 2ch" -c:a libopus -b:a 128k -f ogg - | mpv -   # hear the capture
   ```
   and on Linux: `pactl load-module module-null-sink sink_name=ttyplayer`, `mpv --audio-device=pulse/ttyplayer …`,
   `ffmpeg -f pulse -i ttyplayer.monitor -c:a libopus -b:a 128k -f ogg - | mpv -`.

## Settings keys this adds

`server_host`, `server_port`, `server_token` (generated), `remote_url` (for the TUI client),
`stream_enabled` (`serve` streams instead of playing, as `--stream`).

## Ordered tasks

1. `serve-api` — `aiohttp` server thread, token auth, `/api/*` + `/ws`, `ttyplayer serve`, QR code
   in the terminal; tests with `aiohttp`'s test client and a `FakeClient`.
2. `web-remote` — the static page; tests: served files, token flow, a headless browser is out of
   reach — API contract tests + a manual checklist for the developer's phone.
3. ~~`tui-remote`~~ **done** — `RemoteClient` + `--remote`; the TUI suite runs against it with a fake server.
4. ~~`stream-spike`~~ **done** — measured the three candidates (table above); shipped (3) as `serve --stream`.

## Security notes

- The token is the only gate; HTTPS is out of scope on a LAN (document the risk; offer a reverse
  proxy note for VPS use). Rate-limit nothing in v1; bind to `127.0.0.1` by default and require
  `--host 0.0.0.0` (or the setting) to expose on the LAN.
- The web page never embeds the token in HTML; it reads it from the URL once and keeps it in
  `sessionStorage`.
