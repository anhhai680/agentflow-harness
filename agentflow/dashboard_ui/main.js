"use strict";

// ------------------------------------------------------------------ refresh and live updates

// The stream says what changed; the page refetches only that, at most every `boardRefreshMs`. `dirty` is cleared when
// a fetch starts, so an event that arrives during it marks it again and earns one more round.
const dirty = { board: true, agents: true, inbox: false, runs: new Set() };
let boardRefreshMs = 2000;  // `/api/info`'s board_refresh_s, once it answers

async function refreshBoardNow() {
  if (nothingToShow(ui.repos, ui.multi, ui.repo)) { $("summary").textContent = NO_REPO_TEXT; return; }  // the flags stay for when one comes
  const repo = ui.repo;  // a switch while the fetches run makes what they bring another repository's
  const which = viewRepo();  // the Inbox and Agents: every repository (null) or the selected one, as of this round
  const wantBoard = dirty.board;
  const wantAgents = ui.view === "agents" || dirty.agents;
  const wantWorkflow = !ui.workflow;  // fetched once, and again after Settings saves the config
  const wantInbox = ui.view === "inbox" && (wantBoard || dirty.inbox);  // with the Board's round, while the Inbox is shown
  dirty.board = false;
  dirty.inbox = false;
  if (wantAgents) dirty.agents = false;
  if (!wantBoard && !wantAgents && !wantWorkflow && !wantInbox) return;
  const got = await settleRound({ board: wantBoard ? api("/api/board") : null, agents: wantAgents ? api("/api/agents", undefined, which) : null,
    workflow: wantWorkflow ? api("/api/workflow") : null, inbox: wantInbox ? api("/api/inbox", undefined, which) : null });
  if (ui.repo !== repo || viewRepo() !== which) return;  // applyRepo and setAllRepos marked everything for the new round
  drawRound(got, dirty, roundDraw, ui.multi);  // a failed part is marked again alone; the others are drawn
}

// How each part of a round is drawn, and where a failure of each shows: the Board's and the Workflow's in the summary
// line, the Inbox's and the Agents' in their own views (so one broken repository never blanks another's).
const roundDraw = {
  board: (data) => { renderBoard(data); if (!ui.multi) setInboxBadge(data.needs_human.length); if (ui.view === "metrics") loadMetrics(); if (ui.view === "lessons") loadLessons(); },  // several: loadRepos counts every one
  inbox: (data) => { ui.inbox.failed = ""; renderInbox(data); },
  agents: (data) => renderAgents(data),
  workflow: (data) => { ui.workflow = data; renderWorkflow(); },
  fail: (name, err) => {
    const text = "Could not load: " + err.message;
    if (name === "inbox") inboxFailed(text);
    else if (name === "agents") $("agents-summary").textContent = text;
    else $("summary").textContent = text;
  },
};

// Only the open run and the peeked run are refetched, and only when an event of theirs arrived.
async function refreshRunsNow() {
  const jobs = [];
  if (ui.run && ui.run.detail !== null && dirty.runs.has(ui.run.id)) {  // a missing run is not asked again
    jobs.push(loadRun(ui.run.id));
    if (isArtifactTab(ui.run.tab)) jobs.push(loadArtifacts(ui.run.id));  // new revisions and findings
    if (ui.run.tab === PLAN_TAB && planMode(ui.run.sub, true) === "tasks") jobs.push(loadPlan(ui.run.id));  // the task board
    if (ui.run.tab === FILES_TAB) jobs.push(loadFiles(ui.run.id));  // the documents and the branch's changes
    if (ui.run.tab === ACTIVITY_TAB) jobs.push(loadActivity(ui.run.id, false));  // the newest page, merged into the lines shown
    if (ui.run.tab === SESSIONS_TAB) { jobs.push(loadSessions(ui.run.id)); transcriptCatchUp(); }  // the sessions, and an open live transcript
  }
  if (ui.peek && dirty.runs.has(ui.peek)) jobs.push(loadPeek(ui.peek));
  dirty.runs.clear();  // a run that is not shown is fetched when it is opened
  await Promise.all(jobs);
}

const boardRefresh = throttle(refreshBoardNow, () => boardRefreshMs);
const runRefresh = throttle(refreshRunsNow, () => boardRefreshMs);
const repoRefresh = throttle(loadRepos, () => boardRefreshMs);  // the chips' counts, on a page of several

// After the page's own action: what it changed is fetched in the next round. `repo` is the run's repository
// (undefined: the selected one); a run of another repository is not the open run or the peek, whatever its id.
function refreshAfter(runId, repo) {
  dirty.board = true;
  if (runId && streamOwn({ repo: repo === undefined ? ui.repo : repo }, ui.multi, ui.repo)) dirty.runs.add(runId);
  boardRefresh();
  runRefresh();
}

// One SSE message, `{repo, run_id, event}`. An agent's activity patches its card's line in place and rebuilds nothing;
// anything else marks the Board and that run (one the page has not seen, or `backlog`, too) for the next round. On a
// page of several, a message of another repository only refreshes the chips and an Inbox or Agents that show every
// repository: never the selected repository's cards, open run or peek, even when a run id matches.
function onStreamMessage(msg) {
  if (ui.multi) repoRefresh();
  if (!streamOwn(msg, ui.multi, ui.repo)) {
    if (ui.allRepos && (ui.view === "inbox" || ui.view === "agents")) {
      if (ui.view === "inbox") dirty.inbox = true;
      else dirty.agents = true;
      boardRefresh();
    }
    return;
  }
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
  stream = new EventSource("/api/events?all=1&quiet=1&token=" + encodeURIComponent(TOKEN));  // every repository's, tagged
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

const VIEWS = ["inbox", "board", "workflow", "agents", "metrics", "lessons", "settings"];

// `view` is a header tab's name, or "run" for the Run page, which no tab marks.
function show(view) {
  ui.view = view;
  setInboxKeys(view === "inbox");
  for (const name of VIEWS) {
    $(name + "-view").hidden = view !== name;
    $("tab-" + name).setAttribute("aria-pressed", String(view === name));
  }
  $("run-view").hidden = view !== "run";
  if (view === "run") return;
  try { localStorage.setItem("agentflow-view", view); } catch (err) { console.warn("agentflow: local storage unavailable", err); }
  if (view === "settings" && TOKEN) {  // unsaved edits stay, each form's own
    if (!userSettings.edits.size) loadUserSettings();
    if (!settings.edits.size && !settings.yamlDirty) showRepoSettings();
  }
  if (view === "metrics") loadMetrics();
  if (view === "lessons" && TOKEN) loadLessons();
  if (view === "agents" && TOKEN) boardRefresh();  // the Agents table is fetched while it is shown
  if (view === "inbox" && TOKEN) { dirty.inbox = true; boardRefresh(); }  // so is the Inbox
}

// The hash is the page's place: a tab's `#/<view>` or a run's `#/run/<id>/<tab>`, so a reload and Back keep it. On a
// page of several it starts with the repository (`#/repo/<id>/…`). A hash naming no live repository is never sent: it
// is rewritten under the repository the page shows, and a run of a repository it named that way goes to the Board.
function route() {
  if (TOKEN && !reposTried) return;  // the first route waits for /api/repos, so nothing is fetched for an unchecked id
  let where = parseRoute(location.hash);
  const want = ui.multi ? routeRepo(ui.repos, where.repo, ui.repo, storedRepo()) : null;
  if (where.repo !== want) {
    const only = !ui.multi && ui.repos && ui.repos.repos.length === 1 ? ui.repos.repos[0].id : null;
    const keepRun = where.repo === null || where.repo === only;  // an unprefixed hash is the shown repository's
    const fixed = where.view === "run" && !keepRun ? { view: "board", repo: want } : Object.assign({}, where, { repo: want });
    history.replaceState(null, "", routeHash(fixed));
    where = parseRoute(location.hash);
  }
  if (want !== ui.repo) {
    if (!settingsMayLeave()) {  // kept: the address goes back to what the page shows
      history.replaceState(null, "", routeHash(shownRoute()));
      return;
    }
    applyRepo(want);
  }
  closePeek();
  if (where.view === "run") {
    openRun(where.run, where.tab, where.sub);
    return;
  }
  ui.run = null;
  if (where.view === "board") {  // the URL's filters; a filter written by the bar is already in `ui.boardQuery`
    ui.boardQuery = where.query;
    if (ui.board) renderBoard(ui.board);
  }
  show(where.view);
}

// The route of what the page shows now, in the selected repository.
function shownRoute() {
  if (ui.view === "run" && ui.run) return { view: "run", run: ui.run.id, tab: ui.run.tab, sub: ui.run.sub };
  return { view: ui.view, query: ui.boardQuery };
}

restoreMetricsChoice();
for (const name of VIEWS) $("tab-" + name).addEventListener("click", () => {
  const hash = routeHash({ view: name });  // under the selected repository's prefix on a page of several
  if (location.hash === "#/" + name) route();  // the same hash fires no hashchange: a click on the open tab reloads it
  else if (location.hash === hash) route();
  else location.hash = hash;
});
$("new-task").addEventListener("click", newTaskDialog);
initInbox();
initBoardFilters();
document.addEventListener("keydown", (ev) => { if (ev.key === "Escape" && !$("dlg").open) closePeek(); });
window.addEventListener("hashchange", route);
if (!location.hash) {  // a first visit, or the #token= the boot removed: the tab this browser showed last
  let saved = null;
  try { saved = localStorage.getItem("agentflow-view"); } catch (err) { console.warn("agentflow: local storage unavailable", err); }
  history.replaceState(null, "", routeHash({ view: VIEWS.includes(saved) ? saved : "board", repo: null }));  // route() adds the repository
}
route();
async function loadInfo() {
  try {
    const info = await api("/api/info");
    if (typeof info.board_refresh_s === "number" && info.board_refresh_s > 0) boardRefreshMs = 1000 * info.board_refresh_s;
    $("version").textContent = "v" + info.version;
    $("version").title = info.name + " " + info.version;
    $("version").hidden = false;
    if (Number.isInteger(info.done_shown) && info.done_shown > 0 && info.done_shown !== ui.doneShown) {
      ui.doneShown = info.done_shown;
      if (ui.board) renderBoard(ui.board);
    }
    const legacy = info.inbox_legacy === true;
    if (legacy !== ui.inboxLegacy) { ui.inboxLegacy = legacy; if (ui.board) renderBoard(ui.board); }
  } catch (err) { console.warn("agentflow: info unavailable", err); }  // the badge is optional; the refresh keeps 2 s
}

if (TOKEN) {
  loadRepos().then(() => {  // the selection first: every request after it goes to the repository shown
    if (emptyStart(ui.repos)) {  // no repository yet: Settings, to add the folder that holds them
      history.replaceState(null, "", routeHash({ view: "settings", repo: null }));
      userSettings.focusRoots = true;
    }
    route();
    loadInfo();
    boardRefresh();
    connect();
  });
  setInterval(() => {  // ages and idle markers move on even without new events; Settings may change the refresh
    dirty.board = true;
    if (ui.run) dirty.runs.add(ui.run.id);
    if (ui.peek) dirty.runs.add(ui.peek);
    boardRefresh();
    runRefresh();
    loadInfo();
    repoRefresh();
  }, 30000);
} else {
  $("summary").textContent = "No token: open the URL printed by `agentflow dashboard` (it ends in #token=...).";
  $("live").textContent = "offline";
}
