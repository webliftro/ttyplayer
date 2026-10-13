# Changelog

## 0.14.1 — 2026-10-12

- `ttyplayer selfcheck` probes YouTube with a song that exists, searched once per run; when that search fails, the dependent checks skip instead of failing three times.

## 0.14.0 — 2026-10-11

- ListenBrainz scrobbling (`ttyplayer listenbrainz login`).
- `ttyplayer selfcheck`: every live integration tried once, with a table of results.

## 0.13.0 — 2026-10-11

- Last.fm scrobbling (`ttyplayer lastfm login`).
- Podcast OPML import and export.

## 0.12.0 — 2026-10-11

- Long tracks resume where they were left (`--from-start` to override).
- Podcast subscriptions: `subscribe`, `subscriptions`, `new`.

## 0.11.0 — 2026-10-11

- Gapless playback: the next track is prefetched.
- Podcasts: `ttyplayer podcast search | episodes`, `play --source podcast`.

## 0.10.0 — 2026-10-10

- Lyrics tab in the TUI (synced when available).
- The web remote shows album art, levels and lyrics.

## 0.9.0 — 2026-10-10

- Loudness normalization and configurable key steps.
- Album art in the TUI with the `ttyplayer[art]` extra.
- Homebrew formula generator.

## 0.8.0 — 2026-10-10

- `serve --stream` works on Windows.
- Radio mode: `--radio` keeps going with related tracks when the queue runs out.

## 0.7.0 — 2026-10-10

- SoundCloud search: `--source soundcloud`.
- Sleep timer: `ttyplayer sleep 30m | 1h | end | off`, with a fade-out.

## 0.6.0 — 2026-10-10

- `ttyplayer serve --stream`: the sound goes to the web remote, so a phone can listen.
- Spotify playlist import.
- Stereo level meter.
- Windows fixes.

## 0.5.0 — 2026-10-09

- `ttyplayer serve`: play headless, driven over HTTP from any device.
- The web remote, and `ttyplayer tui --remote`.
- Search returns videos only; unavailable tracks are skipped.

## 0.4.0 — 2026-10-09

- Settings: `ttyplayer config`.
- Named playlists: `ttyplayer playlist ...`.

## 0.3.1 — 2026-10-09

- Volume keys `-` and `+` in the terminal player.
- Clearer key tables in the README.

## 0.3.0 — 2026-10-09

- First release on PyPI: play/search/history/favorites, the terminal player with a volume meter, the TUI, `doctor`, `install.sh`/`install.ps1`.
