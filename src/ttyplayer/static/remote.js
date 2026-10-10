// The ttyplayer web remote: the player's state comes over /ws, every action goes through fetch.
// No framework, no build: the server sends this file as it is.
"use strict";

const TOKEN_KEY = "ttyplayer-token";
const RECONNECT_FIRST = 1000; // ms; doubles after each failed attempt
const RECONNECT_MAX = 10000;
const SLEEP_FOR = "30m"; // what the Sleep button arms
const ART_PLACEHOLDER = "/static/icon.svg"; // shown with no art, or art the phone cannot load
const LYRICS_OPEN_KEY = "ttyplayer-lyrics-open";

const $ = (id) => document.getElementById(id);

let token = takeToken();
let state = {}; // the last status the server sent, queue included
let stateAt = 0; // performance.now() when state.position arrived
let favoriteIds = new Set();
let socket = null;
let retryDelay = RECONNECT_FIRST;
let retryTimer = null;
let volumeHeld = false; // the slider is being dragged: the player's updates must not move it
let artFailed = null; // the art URL that would not load: the placeholder stays until the art changes
let lyricsId; // the id of the track whose lyrics are shown or loading (null: nothing playing)
let lyricsLines = []; // [[seconds, element]] of the shown synced lyrics
let lyricsCurrent = -1; // the index in lyricsLines of the highlighted line

// --- the token ----------------------------------------------------------

// The token comes in the URL once: keep it for this tab and take it out of the address bar.
function takeToken() {
  const params = new URLSearchParams(location.search);
  if (params.has("token")) {
    sessionStorage.setItem(TOKEN_KEY, params.get("token"));
    params.delete("token");
    const rest = params.toString();
    history.replaceState(null, "", location.pathname + (rest ? `?${rest}` : "") + location.hash);
  }
  return sessionStorage.getItem(TOKEN_KEY);
}

function askForToken() {
  sessionStorage.removeItem(TOKEN_KEY);
  token = null;
  if (socket) socket.close();
  clearTimeout(retryTimer);
  $("app").hidden = true;
  $("token-form").hidden = false;
  $("token-input").focus();
}

function useToken(event) {
  event.preventDefault();
  token = $("token-input").value.trim();
  if (!token) return;
  sessionStorage.setItem(TOKEN_KEY, token);
  $("token-input").value = "";
  start();
}

// --- the API --------------------------------------------------------------

class Unauthorized extends Error {}

// One request with the token; the reply's JSON, or an Error carrying the server's message.
async function api(method, path, body) {
  const options = { method, headers: { Authorization: `Bearer ${token}` } };
  if (body !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  const reply = await response.json().catch(() => ({}));
  if (response.status === 401) throw new Unauthorized("The token was refused: paste the current one.");
  if (!response.ok) throw new Error(reply.error || `The server answered ${response.status}`);
  return reply;
}

// What a failed request says to the user; a 401 also asks for the token again.
function failure(error) {
  if (error instanceof Unauthorized) askForToken();
  return error instanceof TypeError ? "The server cannot be reached." : error.message;
}

// Run an action; its failure becomes the banner.
async function act(action) {
  try {
    return await action();
  } catch (error) {
    showBanner(failure(error));
    return undefined;
  }
}

// The player answers every command with its new status.
function command(name, value) {
  const body = value === undefined ? { name } : { name, value };
  return act(async () => setState(await api("POST", "/api/command", body)));
}

function play(body) {
  return act(async () => setState(await api("POST", "/api/play", body)));
}

function enqueue(body) {
  return act(async () => {
    setState(await api("POST", "/api/queue", body));
    showBanner("Added to the queue.");
  });
}

// --- the live state -------------------------------------------------------

function connect() {
  clearTimeout(retryTimer);
  if (!token) return;
  const scheme = location.protocol === "https:" ? "wss:" : "ws:";
  socket = new WebSocket(`${scheme}//${location.host}/ws?token=${encodeURIComponent(token)}`);
  socket.addEventListener("open", () => {
    retryDelay = RECONNECT_FIRST;
    $("connection").textContent = "";
  });
  socket.addEventListener("message", (event) => setState(JSON.parse(event.data)));
  socket.addEventListener("close", reconnect);
}

// A closed socket is retried with a growing delay; the status call tells a wrong token from a
// server that is not back yet (a refused socket shows no status code).
function reconnect() {
  socket = null;
  if (!token) return;
  $("connection").textContent = "Reconnecting…";
  retryTimer = setTimeout(async () => {
    try {
      await api("GET", "/api/status");
      connect();
    } catch (error) {
      if (error instanceof Unauthorized) {
        askForToken();
        showBanner(error.message);
        return;
      }
      retryDelay = Math.min(retryDelay * 2, RECONNECT_MAX);
      reconnect();
    }
  }, retryDelay);
}

// Merge a status into state and redraw. A status carries the queue only when it changed; the playing
// mark also moves with the index, and goes away when the player goes idle.
function setState(status) {
  if (!("idle" in status)) {
    showBanner(status.error); // a failed command's reply: {"error": …} alone
    return;
  }
  if (status.error && status.error !== state.error) showBanner(status.error); // a track the player could not play
  const markMoved = status.index !== state.index || status.idle !== state.idle;
  state = { ...state, ...status };
  stateAt = performance.now();
  renderNowPlaying();
  if (status.queue || markMoved) renderQueue();
  followLyrics();
}

// --- drawing ----------------------------------------------------------------

function formatTime(seconds) {
  if (typeof seconds !== "number" || !Number.isFinite(seconds)) return "--:--";
  const whole = Math.max(0, Math.floor(seconds));
  const h = Math.floor(whole / 3600);
  const m = Math.floor((whole % 3600) / 60);
  const s = String(whole % 60).padStart(2, "0");
  return h ? `${h}:${String(m).padStart(2, "0")}:${s}` : `${m}:${s}`;
}

function playing() {
  return !state.idle && !state.paused && typeof state.position === "number";
}

// Where the track is now: the last position plus the time since it arrived, while playing.
function position() {
  if (typeof state.position !== "number") return null;
  const now = playing() ? state.position + (performance.now() - stateAt) / 1000 : state.position;
  return typeof state.duration === "number" ? Math.min(now, state.duration) : now;
}

function renderProgress() {
  const now = position();
  const fraction = now !== null && state.duration > 0 ? now / state.duration : 0;
  $("np-progress").style.setProperty("--progress", fraction);
  $("np-elapsed").textContent = formatTime(now);
  $("np-duration").textContent = formatTime(state.duration);
}

function renderNowPlaying() {
  const idle = state.idle || !state.title;
  $("np-title").textContent = idle ? "Nothing playing" : state.title;
  $("np-uploader").textContent = idle ? "" : state.uploader || "";
  renderArt(idle ? null : state.thumbnail);
  const label = idle ? "Stopped" : state.paused ? "Paused" : "Playing";
  $("np-state").textContent = { Stopped: "■", Paused: "⏸", Playing: "▶" }[label];
  $("np-state").setAttribute("aria-label", label);
  $("play-pause").textContent = label === "Playing" ? "⏸" : "▶";
  $("np-position").textContent = state.total ? `${state.index} / ${state.total}` : "";
  const hasVolume = typeof state.volume === "number";
  $("volume").disabled = !hasVolume;
  if (hasVolume && !volumeHeld) $("volume").value = Math.round(state.volume);
  $("volume-value").textContent = hasVolume ? Math.round(state.volume) : "";
  $("mute").textContent = state.muted ? "🔇" : "🔊";
  $("mute").setAttribute("aria-pressed", String(Boolean(state.muted)));
  $("listen-row").hidden = !state.stream;
  $("sleep").textContent = state.sleep ? "Sleep off" : `Sleep ${SLEEP_FOR}`;
  // radio is false, true or "fetching" while the player looks up related tracks.
  $("radio").textContent = state.radio === "fetching" ? "∞ fetching…" : state.radio ? "∞ Radio on" : "Radio off";
  $("radio").setAttribute("aria-pressed", String(Boolean(state.radio)));
  renderProgress();
  renderSleep();
  renderLevels();
}

// The phone loads the art itself; the src changes only with the URL, so a status tick never reloads it.
function renderArt(url) {
  const src = url && url !== artFailed ? url : ART_PLACEHOLDER;
  if ($("np-art").getAttribute("src") !== src) $("np-art").src = src;
}

function artFailedToLoad() {
  const src = $("np-art").getAttribute("src");
  if (src === ART_PLACEHOLDER) return;
  artFailed = src;
  $("np-art").src = ART_PLACEHOLDER;
}

// Each channel's peak as a bar, empty at the player's levels_floor and full at 0 dBFS; no levels, no bars.
function renderLevels() {
  const [left, right] = (!state.idle && state.levels) || [null, null];
  $("level-left").style.setProperty("--level", levelFraction(left));
  $("level-right").style.setProperty("--level", levelFraction(right));
}

function levelFraction(level) {
  const floor = state.levels_floor;
  if (typeof level !== "number" || typeof floor !== "number") return 0;
  return Math.min(1, Math.max(0, (level - floor) / -floor));
}

// zz and the time left on the player's sleep timer (zz end: when the track ends), as the TUI shows it.
function renderSleep() {
  const sleep = state.sleep;
  const left = sleep && (sleep.after ? "end" : formatTime(Math.ceil(sleep.ends_at - Date.now() / 1000)));
  $("np-sleep").textContent = left ? `zz ${left}` : "";
}

function button(label, onClick, className = "small", ariaLabel = label) {
  const element = document.createElement("button");
  element.type = "button";
  element.className = className;
  element.textContent = label;
  element.setAttribute("aria-label", ariaLabel);
  element.addEventListener("click", (event) => {
    event.stopPropagation();
    onClick();
  });
  return element;
}

function heart(video) {
  const element = button("♡", () => toggleFavorite(video), "small heart", "Favorite");
  element.dataset.id = video.id;
  paintHeart(element);
  return element;
}

function paintHeart(element) {
  const on = favoriteIds.has(element.dataset.id);
  element.textContent = on ? "♥" : "♡";
  element.setAttribute("aria-pressed", String(on));
}

// The one row every list of videos uses: title, uploader · duration, a heart and the given buttons.
// onTap, when given, runs on a tap anywhere else on the row.
function videoRow(video, buttons, onTap) {
  const row = document.createElement("li");
  row.className = "video";
  const text = document.createElement("div");
  text.className = "video-text";
  const title = document.createElement("div");
  title.className = "title";
  title.textContent = video.title;
  const meta = document.createElement("div");
  meta.className = "meta";
  meta.textContent = video.duration ? `${video.uploader} · ${formatTime(video.duration)}` : video.uploader;
  text.append(title, meta);
  const actions = document.createElement("div");
  actions.className = "actions";
  actions.append(heart(video), ...buttons);
  row.append(text, actions);
  if (onTap) {
    row.classList.add("tappable");
    row.addEventListener("click", onTap);
  }
  return row;
}

function playOrQueueButtons(video) {
  return [button("Play", () => play({ url: video.url })), button("Queue", () => enqueue({ url: video.url }))];
}

function emptyRow(text) {
  const row = document.createElement("li");
  row.className = "empty";
  row.textContent = text;
  return row;
}

function renderList(list, rows, emptyText) {
  $(list).replaceChildren(...(rows.length ? rows : [emptyRow(emptyText)]));
}

function renderQueue() {
  const rows = (state.queue || []).map((video, row) => {
    const element = videoRow(video, [button("×", () => command("remove", row), "small", "Remove")], () => command("jump", row));
    if (!state.idle && row === state.index - 1) {
      element.classList.add("current");
      element.setAttribute("aria-current", "true");
    }
    return element;
  });
  renderList("queue-list", rows, "The queue is empty.");
}

function renderFavorites(videos) {
  favoriteIds = new Set(videos.map((video) => video.id));
  renderList("favorites-list", videos.map((video) => videoRow(video, playOrQueueButtons(video))), "No favorites yet.");
  document.querySelectorAll(".heart").forEach(paintHeart);
}

function renderPlaylists(playlists) {
  const rows = playlists.map((playlist) => {
    const row = document.createElement("li");
    row.className = "video";
    const text = document.createElement("div");
    text.className = "video-text";
    text.textContent = `${playlist.name} (${playlist.count})`;
    const actions = document.createElement("div");
    actions.className = "actions";
    actions.append(button("Play", () => playPlaylist(playlist.name)));
    row.append(text, actions);
    return row;
  });
  renderList("playlists-list", rows, "No playlists yet.");
}

// --- the lyrics -------------------------------------------------------------

function playingVideo() {
  return (!state.idle && (state.queue || [])[state.index - 1]) || null;
}

// While the Lyrics section is open it follows the playing track: fetched on opening and on a new track.
function followLyrics() {
  if (!$("lyrics").open) return;
  const video = playingVideo();
  const id = video ? video.id : null;
  if (id === lyricsId) {
    renderLyricsLine();
    return;
  }
  lyricsId = id;
  if (id === null) showLyrics([], "Nothing playing");
  else loadLyrics(id);
}

async function loadLyrics(id) {
  showLyrics([], "Loading…");
  let found;
  try {
    found = await api("GET", "/api/lyrics");
  } catch (error) {
    found = { error: failure(error) };
  }
  if (id !== lyricsId) return; // the track changed meanwhile
  if (found.error) showLyrics([], found.error.charAt(0).toUpperCase() + found.error.slice(1));
  else if (found.synced) showLyrics(found.synced);
  else showLyrics([], found.plain || "No lyrics found");
}

// Synced lines, each [seconds, text], or one block of text; text nodes only, never markup.
function showLyrics(synced, text) {
  const line = (words) => {
    const element = document.createElement("div");
    element.className = "lyric";
    element.textContent = words;
    return element;
  };
  lyricsLines = synced.map(([seconds, words]) => [seconds, line(words || "♪")]);
  lyricsCurrent = -1;
  $("lyrics-body").replaceChildren(...(text === undefined ? lyricsLines.map(([, element]) => element) : [line(text)]));
  renderLyricsLine();
}

// The line sung now, by the status position: highlighted and scrolled to the middle of the section.
function renderLyricsLine() {
  const now = position();
  const index = now === null ? -1 : lyricsLines.findLastIndex(([seconds]) => seconds <= now);
  if (index === lyricsCurrent) return;
  if (lyricsCurrent >= 0) lyricsLines[lyricsCurrent][1].classList.remove("current");
  lyricsCurrent = index;
  if (index < 0) return;
  const line = lyricsLines[index][1];
  const body = $("lyrics-body");
  line.classList.add("current");
  body.scrollTop = line.offsetTop - (body.clientHeight - line.offsetHeight) / 2;
}

function toggleLyrics() {
  localStorage.setItem(LYRICS_OPEN_KEY, $("lyrics").open ? "open" : "");
  lyricsId = undefined; // opening fetches anew
  followLyrics();
}

// --- the lists ------------------------------------------------------------

function loadFavorites() {
  return act(async () => renderFavorites(await api("GET", "/api/favorites")));
}

function loadPlaylists() {
  return act(async () => renderPlaylists(await api("GET", "/api/playlists")));
}

// Unfavorite, or favorite: the server needs the video's title and uploader to keep it.
function toggleFavorite(video) {
  return act(async () => renderFavorites(await api("POST", `/api/favorites/${encodeURIComponent(video.id)}`, video)));
}

function playPlaylist(name) {
  return act(async () => setState(await api("POST", `/api/playlists/${encodeURIComponent(name)}/play`)));
}

function isUrl(text) {
  return /^https?:\/\//i.test(text);
}

// A pasted link plays at once; anything else is a search whose rows play or queue.
async function search(event) {
  event.preventDefault();
  const text = $("search-input").value.trim();
  if (!text) return;
  if (isUrl(text)) {
    await play({ url: text });
    return;
  }
  $("search-button").disabled = true;
  $("search-results").replaceChildren(emptyRow("Searching…"));
  const videos = await act(() => api("GET", `/api/search?q=${encodeURIComponent(text)}`));
  $("search-button").disabled = false;
  renderList("search-results", (videos || []).map((video) => videoRow(video, playOrQueueButtons(video))), "No results.");
}

// --- listening here (serve --stream) ---------------------------------------

// The server's sound in this tab. Track changes leave the stream alone: the server sends silence
// between tracks, so it stays open until Stop listening closes it.
function toggleListening() {
  const audio = $("listen-audio");
  if (audio.paused) {
    audio.src = `/stream?token=${encodeURIComponent(token)}`;
    audio.play().catch(() => showBanner("This browser would not play the stream."));
  } else {
    stopListening();
  }
  renderListening();
}

function stopListening() {
  const audio = $("listen-audio");
  audio.pause();
  audio.removeAttribute("src");
  audio.load(); // drops the connection
}

function renderListening() {
  const listening = !$("listen-audio").paused;
  $("listen").textContent = listening ? "Stop listening" : "Listen here";
  $("listen").setAttribute("aria-pressed", String(listening));
}

// The stream broke (the server stopped, say): say so, and offer Listen here again.
function streamFailed() {
  if (!$("listen-audio").getAttribute("src")) return; // stopListening() emptied it on purpose
  stopListening();
  renderListening();
  showBanner("The stream stopped. Press Listen here to try again.");
}

// --- the banner -----------------------------------------------------------

function showBanner(text) {
  $("banner-text").textContent = text;
  $("banner").hidden = false;
}

// --- start ------------------------------------------------------------------

// The slider sends the difference from the volume the player reported.
function changeVolume() {
  volumeHeld = false;
  const target = Number($("volume").value);
  if (typeof state.volume === "number" && target !== Math.round(state.volume)) {
    command("volume", target - state.volume);
  }
}

function start() {
  if (!token) {
    askForToken();
    return;
  }
  $("token-form").hidden = true;
  $("app").hidden = false;
  $("banner").hidden = true;
  connect();
  loadFavorites();
  loadPlaylists();
}

function wire() {
  $("token-form").addEventListener("submit", useToken);
  $("banner-close").addEventListener("click", () => {
    $("banner").hidden = true;
  });
  $("prev").addEventListener("click", () => command("prev"));
  $("play-pause").addEventListener("click", () => command("pause"));
  $("next").addEventListener("click", () => command("next"));
  $("mute").addEventListener("click", () => command("mute"));
  $("sleep").addEventListener("click", () => command("sleep", state.sleep ? "off" : SLEEP_FOR));
  $("radio").addEventListener("click", () => command("radio", "toggle"));
  $("volume").addEventListener("input", () => {
    volumeHeld = true;
    $("volume-value").textContent = $("volume").value;
  });
  $("volume").addEventListener("change", changeVolume);
  $("search-form").addEventListener("submit", search);
  $("listen").addEventListener("click", toggleListening);
  $("listen-audio").addEventListener("play", renderListening);
  $("listen-audio").addEventListener("pause", renderListening);
  $("listen-audio").addEventListener("error", streamFailed);
  renderListening();
  $("np-art").addEventListener("error", artFailedToLoad);
  $("lyrics").open = localStorage.getItem(LYRICS_OPEN_KEY) === "open";
  $("lyrics").addEventListener("toggle", toggleLyrics);
  $("queue-clear").addEventListener("click", (event) => {
    event.preventDefault(); // a button in a <summary> would also fold the section
    command("clear_others");
  });
  $("favorites").addEventListener("toggle", () => $("favorites").open && loadFavorites());
  $("playlists").addEventListener("toggle", () => $("playlists").open && loadPlaylists());
  setInterval(() => {
    if (playing()) {
      renderProgress();
      renderLyricsLine();
    }
    renderSleep();
  }, 1000);
}

wire();
start();
