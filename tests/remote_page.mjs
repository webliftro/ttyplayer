// Runs the web remote's script on a stub DOM for tests/test_server.py: node remote_page.mjs <remote.js>.
// stdin: a JSON list of messages the server sends over /ws, or "dismiss" for a click on the banner's ×.
// stdout: after each, {queue, banner}: the queue rows the page shows, each its title with a leading "▸"
// when marked as playing, and the banner's text (null while it is hidden).
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
  }
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
let socket = null;

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
    getElementById: (id) => (elements[id] ??= new Element()),
    createElement: () => new Element(),
    querySelectorAll: () => [],
  },
  location: { search: "?token=t", pathname: "/", hash: "", protocol: "http:", host: "remote" },
  history: { replaceState() {} },
  sessionStorage: { getItem: (key) => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, value), removeItem: (key) => storage.delete(key) },
  fetch: async () => ({ ok: true, status: 200, json: async () => [] }),
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
  if (message === "dismiss") elements["banner-close"].listeners.click();
  else socket.listeners.message({ data: JSON.stringify(message) });
  await settle();
  shown.push({
    queue: elements["queue-list"].children.map((row) => (row.classes.has("current") ? "▸" : "") + row.children[0].children[0].textContent),
    banner: elements["banner"].hidden ? null : elements["banner-text"].textContent,
  });
}
process.stdout.write(JSON.stringify(shown));
