"use strict";

// ------------------------------------------------------------------ refresh and live updates

let refreshTimer = null;
function scheduleRefresh() {
  if (refreshTimer) return;
  refreshTimer = setTimeout(() => { refreshTimer = null; refresh(); }, 250);
}

async function refresh() {
  try {
    const [boardData, agentsData, workflow] = await Promise.all([api("/api/board"), api("/api/agents"), ui.workflow || api("/api/workflow")]);
    renderBoard(boardData);
    renderAgents(agentsData);
    ui.workflow = workflow;
    renderWorkflow();
    if (ui.peek && !$("dlg").open) openPeek(ui.peek);
    if (ui.run && ui.run.detail !== null && !$("dlg").open) openRun(ui.run.id, ui.run.tab, ui.run.sub);  // a missing run is not asked again
    if (ui.view === "metrics") loadMetrics();
    if (ui.view === "lessons") loadLessons();
  } catch (err) {
    $("summary").textContent = "Could not load: " + err.message;
  }
}

let backoff = 1000;
let stream = null;
function connect() {
  stream = new EventSource("/api/events?token=" + encodeURIComponent(TOKEN));
  stream.onopen = () => { backoff = 1000; $("live").textContent = "live"; $("live").classList.add("live"); scheduleRefresh(); };
  stream.onmessage = () => scheduleRefresh();
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
for (const name of VIEWS) $("tab-" + name).addEventListener("click", () => { location.hash = "#/" + name; });
$("new-task").addEventListener("click", newTaskDialog);
document.addEventListener("keydown", (ev) => { if (ev.key === "Escape" && !$("dlg").open) closePeek(); });
window.addEventListener("hashchange", route);
if (!location.hash) {  // a first visit, or the #token= the boot removed: the tab this browser showed last
  let saved = null;
  try { saved = localStorage.getItem("agentflow-view"); } catch (err) { console.warn("agentflow: local storage unavailable", err); }
  history.replaceState(null, "", "#/" + (VIEWS.includes(saved) ? saved : "board"));
}
route();
async function loadVersion() {
  try {
    const info = await api("/api/info");
    $("version").textContent = "v" + info.version;
    $("version").title = info.name + " " + info.version;
    $("version").hidden = false;
  } catch (err) { console.warn("agentflow: version unavailable", err); }  // the badge is optional
}

if (TOKEN) {
  loadVersion();
  refresh();
  connect();
  setInterval(refresh, 30000);  // ages and idle markers move on even without new events
} else {
  $("summary").textContent = "No token: open the URL printed by `agentflow dashboard` (it ends in #token=...).";
  $("live").textContent = "offline";
}
