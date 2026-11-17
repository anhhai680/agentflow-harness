"use strict";

// ------------------------------------------------------------------ refresh and live updates

// The stream says what changed; the page refetches only that, at most every `boardRefreshMs`. `dirty` is cleared when
// a fetch starts, so an event that arrives during it marks it again and earns one more round.
const dirty = { board: true, agents: true, runs: new Set() };
let boardRefreshMs = 2000;  // `/api/info`'s board_refresh_s, once it answers

async function refreshBoardNow() {
  const wantBoard = dirty.board;
  const wantAgents = ui.view === "agents" || dirty.agents;
  const wantWorkflow = !ui.workflow;  // fetched once, and again after Settings saves the config
  dirty.board = false;
  if (wantAgents) dirty.agents = false;
  if (!wantBoard && !wantAgents && !wantWorkflow) return;
  try {
    const [boardData, agentsData, workflow] = await Promise.all([wantBoard ? api("/api/board") : null,
      wantAgents ? api("/api/agents") : null, wantWorkflow ? api("/api/workflow") : null]);
    if (boardData) renderBoard(boardData);
    if (agentsData) renderAgents(agentsData);
    if (workflow) { ui.workflow = workflow; renderWorkflow(); }
    if (wantBoard && ui.view === "metrics") loadMetrics();
    if (wantBoard && ui.view === "lessons") loadLessons();
  } catch (err) {
    if (wantBoard) dirty.board = true;  // the next round tries again
    if (wantAgents) dirty.agents = true;
    $("summary").textContent = "Could not load: " + err.message;
  }
}

// Only the open run and the peeked run are refetched, and only when an event of theirs arrived.
async function refreshRunsNow() {
  const jobs = [];
  if (ui.run && ui.run.detail !== null && dirty.runs.has(ui.run.id)) {  // a missing run is not asked again
    jobs.push(loadRun(ui.run.id));
    if (isArtifactTab(ui.run.tab)) jobs.push(loadArtifacts(ui.run.id));  // new revisions and findings
    if (ui.run.tab === PLAN_TAB && planMode(ui.run.sub, true) === "tasks") jobs.push(loadPlan(ui.run.id));  // the task board
    if (ui.run.tab === FILES_TAB) jobs.push(loadFiles(ui.run.id));  // the documents and the branch's changes
    if (ui.run.tab === SESSIONS_TAB) { jobs.push(loadSessions(ui.run.id)); transcriptCatchUp(); }  // the sessions, and an open live transcript
  }
  if (ui.peek && dirty.runs.has(ui.peek)) jobs.push(loadPeek(ui.peek));
  dirty.runs.clear();  // a run that is not shown is fetched when it is opened
  await Promise.all(jobs);
}

const boardRefresh = throttle(refreshBoardNow, () => boardRefreshMs);
const runRefresh = throttle(refreshRunsNow, () => boardRefreshMs);

// After the page's own action: what it changed is fetched in the next round.
function refreshAfter(runId) {
  dirty.board = true;
  if (runId) dirty.runs.add(runId);
  boardRefresh();
  runRefresh();
}

// One SSE message, `{run_id, event}`. An agent's activity patches its card's line in place and rebuilds nothing;
// anything else marks the Board and that run (one the page has not seen, or `backlog`, too) for the next round.
function onStreamMessage(msg) {
  const runId = msg && msg.run_id !== undefined && msg.run_id !== null ? String(msg.run_id) : "";
  const event = (msg && msg.event) || {};
  if (event.type === "agent_activity") {
    patchActivity(runId, event);
    dirty.agents = true;
    if (ui.view === "agents") boardRefresh();
    if (sessionActivity(runId, event)) transcriptFollow();  // the open session took a turn: its transcript follows
    return;
  }
  dirty.board = true;
  if (runId) dirty.runs.add(runId);
  boardRefresh();
  runRefresh();
}

function streamMessage(data) {
  try { return JSON.parse(data); } catch (err) { return {}; }  // unreadable: still worth a refresh
}

let backoff = 1000;
let stream = null;
function connect() {
  stream = new EventSource("/api/events?quiet=1&token=" + encodeURIComponent(TOKEN));
  stream.onopen = () => {
    backoff = 1000;
    $("live").textContent = "live";
    $("live").classList.add("live");
    // Events sent while the stream was down are lost: fetch everything shown once.
    dirty.board = true;
    dirty.agents = true;
    if (ui.run) dirty.runs.add(ui.run.id);
    if (ui.peek) dirty.runs.add(ui.peek);
    boardRefresh();
    runRefresh();
  };
  stream.onmessage = (ev) => onStreamMessage(streamMessage(ev.data));
  stream.onerror = () => {
    $("live").textContent = "reconnecting…";
    $("live").classList.remove("live");
    stream.close();
    setTimeout(connect, backoff);
    backoff = Math.min(backoff * 2, 15000);
  };
}

const VIEWS = ["board", "workflow", "agents", "metrics", "lessons", "settings"];

// `view` is a header tab's name, or "run" for the Run page, which no tab marks.
function show(view) {
  ui.view = view;
  for (const name of VIEWS) {
    $(name + "-view").hidden = view !== name;
    $("tab-" + name).setAttribute("aria-pressed", String(view === name));
  }
  $("run-view").hidden = view !== "run";
  if (view === "run") return;
  try { localStorage.setItem("agentflow-view", view); } catch (err) { console.warn("agentflow: local storage unavailable", err); }
  if (view === "settings" && TOKEN && !settings.edits.size && !settings.yamlDirty) loadSettings();  // unsaved edits stay
  if (view === "metrics") loadMetrics();
  if (view === "lessons" && TOKEN) loadLessons();
  if (view === "agents" && TOKEN) boardRefresh();  // the Agents table is fetched while it is shown
}

// The hash is the page's place: a tab's `#/<view>` or a run's `#/run/<id>/<tab>`, so a reload and Back keep it.
function route() {
  const where = parseRoute(location.hash);
  closePeek();
  if (where.view === "run") {
    openRun(where.run, where.tab, where.sub);
    return;
  }
  ui.run = null;
  show(where.view);
}

restoreMetricsChoice();
for (const name of VIEWS) $("tab-" + name).addEventListener("click", () => {
  if (location.hash === "#/" + name) route();  // the same hash fires no hashchange: a click on the open tab reloads it
  else location.hash = "#/" + name;
});
$("new-task").addEventListener("click", newTaskDialog);
document.addEventListener("keydown", (ev) => { if (ev.key === "Escape" && !$("dlg").open) closePeek(); });
window.addEventListener("hashchange", route);
if (!location.hash) {  // a first visit, or the #token= the boot removed: the tab this browser showed last
  let saved = null;
  try { saved = localStorage.getItem("agentflow-view"); } catch (err) { console.warn("agentflow: local storage unavailable", err); }
  history.replaceState(null, "", "#/" + (VIEWS.includes(saved) ? saved : "board"));
}
route();
async function loadInfo() {
  try {
    const info = await api("/api/info");
    if (typeof info.board_refresh_s === "number" && info.board_refresh_s > 0) boardRefreshMs = 1000 * info.board_refresh_s;
    $("version").textContent = "v" + info.version;
    $("version").title = info.name + " " + info.version;
    $("version").hidden = false;
  } catch (err) { console.warn("agentflow: info unavailable", err); }  // the badge is optional; the refresh keeps 2 s
}

if (TOKEN) {
  loadInfo();
  boardRefresh();
  connect();
  setInterval(() => {  // ages and idle markers move on even without new events; Settings may change the refresh
    dirty.board = true;
    if (ui.run) dirty.runs.add(ui.run.id);
    if (ui.peek) dirty.runs.add(ui.peek);
    boardRefresh();
    runRefresh();
    loadInfo();
  }, 30000);
} else {
  $("summary").textContent = "No token: open the URL printed by `agentflow dashboard` (it ends in #token=...).";
  $("live").textContent = "offline";
}
