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
| POST | `/api/command` | `{"name": "pause"|"next"|"prev"|"stop"|"mute"|"seek"|"volume"|"jump"|"remove"|"move"|"clear_others", "value"?}` → the new status; `jump`/`remove` take a 0-based queue row, `move` two (`[from, to]`) |
| GET | `/api/commands` | the command table's names (the page's and the server's tests both read it) |
| GET | `/api/favorites` | the favorites, newest first |
| POST | `/api/favorites/{id}` | toggles: unfavorites, or favorites the video in the body → the favorites |
| GET | `/api/search?q=` | `youtube.search` results |
| GET/POST | `/api/playlists…` | list / play a playlist |
| GET | `/api/settings`, PATCH | read / change settings |
| WS | `/ws` | server → client: every `on_state` status as one JSON message (with `queue` when it changed since the last one); client → server: the same commands as `/api/command` |
| GET | `/` | the web remote (one HTML file + one JS file + one CSS file, no build step); `/static/*`, `/manifest.webmanifest` |
| GET | `/stream` | step 2: `audio/ogg` Opus stream |

All POSTs are idempotent-ish and reply with the new status.

## Web remote

One static page (vanilla JS, no framework, no build): search box, results, queue, big now-playing
card (title, uploader, progress bar, time, volume slider, play/pause/next/prev/mute), favorites and
playlists lists. Dark/light follows the phone. Works from the phone's browser and as a home-screen
app (manifest). The page talks WS for state and `fetch` for actions.

## TUI as a client

`ttyplayer tui --remote http://host:7700` (or settings `remote_url`) uses a `RemoteClient` that
implements the same methods `TtyplayerApp` calls on `MpvClient` (`add`, `play_current`, `next`,
`prev`, `jump`, `remove`, `move`, `clear_others`, `toggle_pause`, `seek`, `change_volume`,
`toggle_mute`, `status`, `queue`, `index`) over the API, with `on_state` fed by the WebSocket. The
TUI code does not change; the factory does.

## Audio stream (step 2, spike first)

mpv can tee its output: `--ao=pcm --ao-pcm-file=<fifo>` kills the local sound, so instead run mpv
with `--lavfi-complex` or use a second mpv? Candidates to measure in the spike:
1. mpv `--ao=lavc`-style encoding is not available; use `--af=lavfi=[asplit[a][b];[b]…]` — not a
   sink either.
2. **Two outputs via a virtual device** (BlackHole on macOS, PulseAudio null sink + monitor on
   Linux, VB-Cable on Windows): mpv plays to the virtual device; ffmpeg reads the device and serves
   Opus over HTTP. Platform setup, but zero code in mpv.
3. **Server-side-only mode**: on a headless server (Raspberry Pi, VPS) there are no speakers
   anyway: mpv `--ao=pcm --ao-pcm-file=/dev/stdout | ffmpeg -f s16le … -c:a libopus -f ogg` →
   stream. Simplest and the most common real deployment ("my music box at home / my VPS").
Decide after measuring 1–3 on the developer's Mac + a Linux box; (3) is the likely first ship.

## Settings keys this adds

`server_host`, `server_port`, `server_token` (generated), `remote_url` (for the TUI client),
`stream_enabled` (step 2).

## Ordered tasks

1. `serve-api` — `aiohttp` server thread, token auth, `/api/*` + `/ws`, `ttyplayer serve`, QR code
   in the terminal; tests with `aiohttp`'s test client and a `FakeClient`.
2. `web-remote` — the static page; tests: served files, token flow, a headless browser is out of
   reach — API contract tests + a manual checklist for the developer's phone.
3. ~~`tui-remote`~~ **done** — `RemoteClient` + `--remote`; the TUI suite runs against it with a fake server.
4. `stream-spike` — measure the three candidates; a report in the baton; then `stream` if it holds.

## Security notes

- The token is the only gate; HTTPS is out of scope on a LAN (document the risk; offer a reverse
  proxy note for VPS use). Rate-limit nothing in v1; bind to `127.0.0.1` by default and require
  `--host 0.0.0.0` (or the setting) to expose on the LAN.
- The web page never embeds the token in HTML; it reads it from the URL once and keeps it in
  `sessionStorage`.
