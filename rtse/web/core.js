// Shared by every page: DOM helpers, the API client, toasts, the shared `state`, and the shell hooks.
// Imports nothing from app.js or the page modules, so nothing here can form an import cycle.
import { manufacturerColor } from "./meta.js";
import { emblemSvg, ui } from "./icons.js";

const token = new URLSearchParams(location.search).get("t") || "";

export const state = {
  items: [],
  itemsKey: "",
  search: "",
  selected: null,
  detail: null,
  open: { stats: false, info: false, charinfo: false, ammoinfo: false }, // which dropdown sections are expanded
  mode: localStorage.getItem("rtse.mode") || "legal",
  picker: null,
  page: "gear", // "gear" (item editor) or "character"
  character: null,
  ammo: null,
  effects: new Map(),
  owners: {}, // "weapon" | "item" -> Map(part id -> [how many guns roll it, first few gun names])
  view3d: localStorage.getItem("rtse.view3d") !== "off", // 3D gun view (falls back to the blueprint)
};

/** The shell (app.js) sets these at boot. Feature modules call them rather than importing app.js. */
export const shell = { renderWorkspace: () => {}, renderPinned: () => {} };

// viewer3d.js (and three.js) load on demand; if WebGL or the models are unavailable we fall back.
export const v3d = { module: null, failed: false };

export async function ensureViewerModule() {
  if (v3d.module || v3d.failed) return v3d.module;
  try {
    v3d.module = await import("./viewer3d.js");
  } catch (error) {
    console.warn("3D viewer unavailable:", error);
    v3d.failed = true;
  }
  return v3d.module;
}

// ---------- helpers ----------

export function h(tag, props = {}, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") el.className = value;
    else if (key === "text") el.textContent = value;
    else if (key === "html") el.innerHTML = value;
    else if (key === "style") Object.assign(el.style, value);
    else if (key === "vars") for (const [k, v] of Object.entries(value)) el.style.setProperty(k, v);
    else if (key.startsWith("on")) el.addEventListener(key.slice(2), value);
    else el.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child.nodeType ? child : document.createTextNode(String(child)));
  }
  return el;
}

export const $ = (selector) => document.querySelector(selector);

/** Replaces a parent's children, flattening arrays and dropping null/false (which the DOM would print). */
export function fill(parent, ...nodes) {
  parent.replaceChildren(...nodes.flat(Infinity).filter((node) => node !== null && node !== undefined && node !== false));
}

export const icon = (name, size = 16) => h("span", { class: "icon", style: { display: "inline-flex" }, html: ui(name, size) });
export const emblem = (key, size = 16) => h("span", { style: { display: "inline-flex" }, html: emblemSvg(key, manufacturerColor(key), size) });

const RETRY_EVERY_MS = 1000; // a POST the game is still applying (504 pending) is retried this often...
const RETRY_LIMIT = 30; // ...for at most this many retries, then the server's error is shown

/** A fresh Idempotency-Key for one user action: A-Z, a-z, 0-9, _ and - only (see rtse/idempotency.py). */
function newIdempotencyKey() {
  if (globalThis.crypto && crypto.randomUUID) return crypto.randomUUID();
  return Array.from(crypto.getRandomValues(new Uint8Array(16)), (b) => b.toString(16).padStart(2, "0")).join("");
}

/**
 * One API call. A POST sends one Idempotency-Key for the whole call, so a retry cannot apply the change
 * twice. On 504 {pending: true} the game is still applying it: the same request goes out again with the
 * same key, once a second, until the game answers or the retries run out.
 */
export async function api(method, path, body) {
  const key = method === "POST" ? newIdempotencyKey() : null;
  const headers = { "X-RTSE-Token": token, "Content-Type": "application/json" };
  if (key) headers["Idempotency-Key"] = key;
  const init = { method, headers, body: body === undefined ? undefined : JSON.stringify(body) };
  for (let retry = 0; ; retry += 1) {
    const response = await fetch(path, init);
    const data = await response.json();
    if (response.status === 504 && data.pending && key && retry < RETRY_LIMIT) {
      await new Promise((resolve) => setTimeout(resolve, RETRY_EVERY_MS));
      continue;
    }
    if (!response.ok) {
      const message = data.error || `HTTP ${response.status}`;
      // 503 applied:false means the game never started the change: say so plainly.
      const unchanged = response.status === 503 && data.applied === false && !/nothing was changed/.test(message);
      throw new Error(unchanged ? `${message} - nothing was changed` : message);
    }
    return data;
  }
}

/** A small muted tag for a value the game did not confirm; the reason shows on hover. */
export function unverifiedBadge(reason) {
  return reason ? h("span", { class: "unv", title: reason, text: "unverified" }) : null;
}

export function toast(message, isError = false) {
  const el = h("div", { class: `toast${isError ? " err" : ""}`, text: message });
  $("#toasts").append(el);
  setTimeout(() => el.remove(), isError ? 7000 : 3200);
}

export async function guarded(action) {
  try {
    return await action();
  } catch (error) {
    toast(error.message, true);
    return null;
  }
}

/** A collapsible section. Open/closed state survives re-renders. */
export function dropdown(key, title, badge, content) {
  return h(
    "details",
    { class: "drop", open: state.open[key] || null, ontoggle: (event) => { state.open[key] = event.target.open; } },
    h("summary", {}, icon("chevron", 20), h("h3", { text: title }), badge && h("em", { text: badge })),
    h("div", { class: "drop-body" }, content),
  );
}

export async function dump(path) {
  const result = await guarded(() => api("GET", path));
  if (result) toast(`Saved ${result.saved_to}`);
}

/** What a feature page gets: the helpers above, plus `rerender` (re-draws the workspace). */
export const pageCtx = { h, fill, icon, api, toast, guarded, dump, dropdown, rerender: () => shell.renderWorkspace() };
