"use strict";
// Every string from a run (agent output, notes, issue text) is set with textContent: never as markup.

const TOKEN = (function () {
  // `agentflow dashboard` prints /#token=...: a fragment is never sent to the server, its log or a Referer.
  // The token is kept in sessionStorage for this tab, so a reload or a restored tab still works.
  const match = /^#token=([A-Za-z0-9_-]+)$/.exec(location.hash);
  let token = match ? match[1] : "";
  try {
    if (token) { sessionStorage.setItem("agentflow-token", token); } else { token = sessionStorage.getItem("agentflow-token") || ""; }
  } catch (err) {
    console.warn("agentflow: session storage unavailable", err);
  }
  if (match) { history.replaceState(null, "", location.pathname); }  // keep the token out of the address bar; a route stays
  return token;
})();

const ui = { view: "board", peek: null, run: null, board: null, agents: null, workflow: null, dragging: null, doneOpen: false,
  // the Board's filters (the URL's `#/board?…`) and how many Done cards it draws: `dashboard.done_shown`, or all of them
  boardQuery: { lane: null, status: null, needs: false, q: null }, doneShown: 10, doneAll: false, comments: new Map(), viewed: new Map(),
  // the Inbox: its last view, what was typed per card (outside the nodes), the armed Approve (and when), the focused card
  // (its index in `order`, the cards as shown, and its key), the grouping, the cards kept after a decision made elsewhere
  // (`gone`) and the answers this page sent itself (`sent`)
  inbox: { view: null, drafts: new Map(), armed: null, armedAt: 0, focus: 0, focusKey: null, order: [], group: "run", ids: new Map(),
    busy: new Set(), gone: new Map(), sent: new Set() }, inboxLegacy: false };
const $ = (id) => document.getElementById(id);
const workLabel = (lane) => (ui.board && ui.board.lane_labels[lane]) || lane;  // a lane with no label, or an old run's, shows its name

// ------------------------------------------------------------------ live updates that keep the reader's place

// Make `parent`'s children the rendered `items`, in order, without rebuilding what has not changed. Each node carries
// `data-key` (from `keyOf`) and `data-sig` (the item as JSON: a function in an item, its builder, is not part of it).
// A child whose key and sig are unchanged is the same node, so its open sections, focus and typed text stay. A changed
// child is replaced, and each `details[data-key]` inside it (and the node itself, when it is a `details`) keeps the
// open or closed state the reader left it in, and each scroll box in it (`pre`, `.scroll`) its scroll offsets: a
// reader at the bottom of a growing log stays at its bottom. Children whose keys are gone are removed; a node already in its place is
// never moved. A repeated key gets a `~<n>` suffix, so no item is lost and no node is used twice.
function patchChildren(parent, items, keyOf, render) {
  const old = new Map();
  for (const child of Array.from(parent.children)) {
    const key = child.getAttribute("data-key");
    if (key !== null && !old.has(key)) old.set(key, child);
  }
  const active = document.activeElement;
  let refocus = null;
  const scrolls = [];  // [new node, the old node's scroll offsets], set once the new node is in the document
  const used = new Map();
  const nodes = [];
  for (const item of items) {
    let key = String(keyOf(item));
    const count = (used.get(key) || 0) + 1;
    used.set(key, count);
    if (count > 1) key += "~" + count;
    const sig = JSON.stringify(item);
    const prev = old.get(key);
    if (prev && prev.getAttribute("data-sig") === sig) { nodes.push(prev); continue; }
    const node = render(item);
    if (!(node instanceof Element)) continue;
    node.setAttribute("data-key", key);
    node.setAttribute("data-sig", sig);
    if (prev) {
      keepOpen(prev, node);
      scrolls.push([node, scrollOffsets(prev)]);
      if (prev === active) refocus = node;
    }
    nodes.push(node);
  }
  const keep = new Set(nodes);
  for (const child of Array.from(parent.childNodes)) if (!keep.has(child)) parent.removeChild(child);
  let cursor = parent.firstChild;  // only kept nodes are left, in their old order
  for (const node of nodes) {
    if (node === cursor) cursor = cursor.nextSibling;
    else parent.insertBefore(node, cursor);
  }
  for (const [node, offsets] of scrolls) keepScroll(node, offsets);  // a detached element ignores scrollTop
  if (refocus && typeof refocus.focus === "function") refocus.focus({ preventScroll: true });
}

// The scroll boxes of a node (itself first, when it is one), in document order: matched by position across a rebuild.
const scrollBoxes = (node) => [node, ...node.querySelectorAll("pre, .scroll")].filter((n, i) => i > 0 || n.tagName === "PRE" || n.classList.contains("scroll"));

function scrollOffsets(node) {
  return scrollBoxes(node).map((n) => ({ top: n.scrollTop, left: n.scrollLeft, bottom: n.scrollTop > 0 && n.scrollTop + n.clientHeight >= n.scrollHeight - 2 }));
}

function keepScroll(node, offsets) {
  const boxes = scrollBoxes(node);
  for (let i = 0; i < Math.min(boxes.length, offsets.length); i++) {
    boxes[i].scrollTop = offsets[i].bottom ? boxes[i].scrollHeight : offsets[i].top;
    boxes[i].scrollLeft = offsets[i].left;
  }
}

// Carry the open state of `prev`'s keyed sections (and of `prev` itself) to the node replacing it.
function keepOpen(prev, node) {
  if (prev.tagName === "DETAILS" && node.tagName === "DETAILS") node.open = prev.open;
  const was = new Map();
  for (const d of prev.querySelectorAll("details[data-key]")) if (!was.has(d.getAttribute("data-key"))) was.set(d.getAttribute("data-key"), d.open);
  for (const d of node.querySelectorAll("details[data-key]")) if (was.has(d.getAttribute("data-key"))) d.open = was.get(d.getAttribute("data-key"));
}

// An item for patchChildren: `data` is what the node shows (its sig), `build(data)` makes the node.
const entry = (key, data, build) => ({ key: key, data: data, build: build });
const entryKey = (item) => item.key;
const entryNode = (item) => item.build(item.data);
const childByKey = (parent, key) => Array.from(parent.children).find((n) => n.getAttribute("data-key") === key) || null;

// Run `fn` now when its last run started at least `ms()` ago, else once at that time. Calls in between collapse into
// that one run, and a call while `fn` is still running collapses into one run after it: two runs never overlap.
function throttle(fn, ms) {
  let last = -Infinity;
  let timer = null;
  let running = false;
  let again = false;
  const run = async () => {
    timer = null;
    running = true;
    last = performance.now();
    try { await fn(); } catch (err) { console.warn("agentflow: refresh failed", err); }
    running = false;
    if (again) { again = false; call(); }
  };
  const call = () => {
    if (running) { again = true; return; }
    if (timer !== null) return;
    const wait = last + ms() - performance.now();
    if (wait <= 0) run();
    else timer = setTimeout(run, wait);
  };
  return call;
}

function el(tag, props, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = String(value);
    else if (key === "title") node.title = String(value);
    else if (key === "onclick") node.addEventListener("click", value);
    else if (key === "ontoggle") node.addEventListener("toggle", value);
    else if (/^(type|value|name|for|id|role|tabindex|aria-[a-z]+|readonly|checked|placeholder|open|draggable|href|target|rel|disabled|maxlength|multiple|min|max|step|spellcheck|data-[a-z-]+)$/.test(key)) node.setAttribute(key, String(value));
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

// A modal dialog sits in the top layer, above any z-index, so a #toast raised by its OK would be hidden under
// it. While that OK runs, `dialogSaid` collects the message instead: a failure shows it in the dialog's own line,
// a success floats it once the dialog has closed. `dialogRun` counts opens, so a late answer touches no newer one.
let dialogSaid = null;
let dialogRun = 0;

function clearDialogMessage() { const msg = $("dlg-msg"); msg.textContent = ""; msg.hidden = true; }

function showDialogMessage(text) { const msg = $("dlg-msg"); msg.textContent = text; msg.hidden = false; }

function toast(text) {
  if (dialogSaid !== null) { dialogSaid = text; return; }
  const box = $("toast");
  box.textContent = text;
  box.classList.add("show");
  setTimeout(() => box.classList.remove("show"), 2600);
}

async function api(path, body) {
  const options = { headers: { "X-Agentflow-Token": TOKEN }, cache: "no-store" };
  if (body !== undefined) {
    options.method = "POST";
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  const text = await response.text();
  let data = {};
  try { data = text ? JSON.parse(text) : {}; } catch (err) { data = { error: "unreadable response" }; }
  if (!response.ok) {
    const error = new Error(data.error || (response.status === 403 ? "forbidden: open the URL with the token printed by `agentflow dashboard`" : "HTTP " + response.status));
    error.status = response.status;  // a 404 tells the Run page the run does not exist
    throw error;
  }
  return data;
}

const runPath = (id) => "/api/runs/" + encodeURIComponent(id);

function actionToast(action, result) {
  if (action === "answer" && result.complete === false) return "Recorded; still waiting on " + result.pending.join(", ");
  if (action === "approve" && !result.resumed) return "Approved: the card is ready";
  if (action === "done" && result.resumed) return "Marked done: the draft PR opens in the background";
  return action + " recorded" + (result.resumed ? "; the run resumes in the background" : "");
}

async function act(runId, action, body) {
  try {
    toast(actionToast(action, await api(runPath(runId) + "/" + action, body || {})));
    refreshAfter(runId);
    return true;
  } catch (err) {
    toast(action + " failed: " + err.message);
    return false;
  }
}

function ago(seconds) {
  if (seconds === null || seconds === undefined) return "-";
  const minutes = Math.floor(seconds / 60);
  if (minutes < 1) return "just now";
  if (minutes < 60) return minutes + " min";
  return Math.floor(minutes / 60) + " h " + (minutes % 60) + " min";
}

function diffNode(text) {
  const lines = text.replace(/\n$/, "").split("\n").map((line) => el("span", {
    class: line.startsWith("@@") ? "d-hunk" : line.startsWith("+") && !line.startsWith("+++") ? "d-add"
      : line.startsWith("-") && !line.startsWith("---") ? "d-del" : null, text: line + "\n" }));
  return el("pre", { class: "diff" }, lines);
}

const money = (value) => (typeof value === "number" ? "$" + value.toFixed(2) : "-");
// " / soft · hard" for a run with a soft budget, " / budget" for one started before it, "" for none.
const limits = (soft, hard) => (hard ? " / " + (soft ? money(soft) + " · " : "") + money(hard) : "");
const arrow = (direction) => (direction === "up" ? " ↑" : direction === "down" ? " ↓" : "");
