// Runs the web remote's script on a stub DOM for tests/test_server.py: node remote_page.mjs <remote.js>.
// stdin: a JSON list of messages the server sends over /ws, or "dismiss" for a click on the banner's ×,
// "listen" for a click on Listen here, "sleep" for a click on the Sleep button, "stream-error" for the <audio> failing, {"search": text, "reply": videos}
// for a search the server answers with videos, {"favorites": videos} for opening Favorites, and
// {"click": label, "list": id} for that button on the first row of a list.
// stdout: after each, {queue, banner, listen, sleep, posted}: the queue rows the page shows, each its title with a leading "▸"
// when marked as playing, the banner's text (null while it is hidden), the Listen here button
// ({shown, label, src, playing}), the sleep timer ({text, label}: its zz text and the button's),
// and the [path, body] of each POST the page sent.
import { readFileSync } from "node:fs";
import vm from "node:vm";

class Element {
  constructor() {
    this.children = [];
    this.classes = new Set();
    this.listeners = {};
    this.attributes = {};
    this.dataset = {};
    this.style = { setProperty() {} };
    this.textContent = "";
    this.paused = true; // as an <audio>
  }
  getAttribute(name) {
    return this.attributes[name] ?? null;
  }
  removeAttribute(name) {
    delete this.attributes[name];
  }
  set src(url) {
    this.attributes.src = url;
  }
  get src() {
    return this.attributes.src ?? "";
  }
  play() {
    this.paused = false;
    this.listeners.play?.();
    return Promise.resolve();
  }
  pause() {
    this.paused = true;
    this.listeners.pause?.();
  }
  load() {}
  set className(names) {
    this.classes = new Set(names.split(" "));
  }
  get classList() {
    return { add: (name) => this.classes.add(name) };
  }
  setAttribute(name, value) {
    this.attributes[name] = value;
  }
  addEventListener(type, listener) {
    this.listeners[type] = listener;
  }
  append(...children) {
    this.children.push(...children);
  }
  replaceChildren(...children) {
    this.children = children;
  }
  focus() {}
}

const elements = {};
const element = (id) => (elements[id] ??= new Element());
let socket = null;
let reply = [];
let posted = [];

class WebSocket {
  constructor() {
    this.listeners = {};
    socket = this;
  }
  addEventListener(type, listener) {
    this.listeners[type] = listener;
  }
  close() {}
}

const storage = new Map();
const context = vm.createContext({
  document: {
    getElementById: element,
    createElement: () => new Element(),
    querySelectorAll: () => [],
  },
  location: { search: "?token=t", pathname: "/", hash: "", protocol: "http:", host: "remote" },
  history: { replaceState() {} },
  sessionStorage: { getItem: (key) => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, value), removeItem: (key) => storage.delete(key) },
  fetch: async (path, options) => {
    if (options.method === "POST") posted.push([path, JSON.parse(options.body ?? "null")]);
    return { ok: true, status: 200, json: async () => (options.method === "GET" ? reply : {}) };
  },
  WebSocket,
  URLSearchParams,
  performance,
  setTimeout: () => 0,
  clearTimeout() {},
  setInterval: () => 0,
});
vm.runInContext(readFileSync(process.argv[2], "utf-8"), context);

const settle = () => new Promise((resolve) => setImmediate(resolve));
const shown = [];
for (const message of JSON.parse(readFileSync(0, "utf-8"))) {
  if (message === "dismiss") element("banner-close").listeners.click();
  else if (message === "listen") element("listen").listeners.click();
  else if (message === "sleep") element("sleep").listeners.click();
  else if (message === "stream-error") element("listen-audio").listeners.error();
  else if (message.search !== undefined) {
    reply = message.reply;
    element("search-input").value = message.search;
    await element("search-form").listeners.submit({ preventDefault() {} });
  } else if (message.favorites !== undefined) {
    reply = message.favorites;
    element("favorites").open = true;
    element("favorites").listeners.toggle();
  } else if (message.click !== undefined) {
    const actions = element(message.list).children[0].children[1];
    actions.children.find((child) => child.textContent === message.click).listeners.click({ stopPropagation() {} });
  } else socket.listeners.message({ data: JSON.stringify(message) });
  await settle();
  shown.push({
    queue: element("queue-list").children.map((row) => (row.classes.has("current") ? "▸" : "") + row.children[0].children[0].textContent),
    banner: element("banner").hidden ? null : element("banner-text").textContent,
    listen: {
      shown: !element("listen-row").hidden,
      label: element("listen").textContent,
      src: element("listen-audio").getAttribute("src"),
      playing: !element("listen-audio").paused,
    },
    sleep: { text: element("np-sleep").textContent, label: element("sleep").textContent },
    posted,
  });
  posted = [];
}
process.stdout.write(JSON.stringify(shown));
