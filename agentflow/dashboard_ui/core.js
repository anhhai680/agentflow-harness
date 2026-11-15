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

const ui = { view: "board", peek: null, run: null, board: null, agents: null, workflow: null, dragging: null, doneOpen: false };
const $ = (id) => document.getElementById(id);
const workLabel = (lane) => (ui.board && ui.board.lane_labels[lane]) || lane;  // a lane with no label, or an old run's, shows its name

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
    scheduleRefresh();
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

const money = (value) => (typeof value === "number" ? "$" + value.toFixed(2) : "-");
// " / soft · hard" for a run with a soft budget, " / budget" for one started before it, "" for none.
const limits = (soft, hard) => (hard ? " / " + (soft ? money(soft) + " · " : "") + money(hard) : "");
const arrow = (direction) => (direction === "up" ? " ↑" : direction === "down" ? " ↓" : "");
