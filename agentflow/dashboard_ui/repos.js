"use strict";

// ------------------------------------------------------------------ the workspace: which repository the page shows

// `/api/repos` lists every repository the server holds. On a page of several, the header draws one chip per
// repository and the page shows one of them (`ui.repo`): its id prefixes every per-repository request (`repoPath`) and
// every route (`#/repo/<id>/…`). On a page of one, `ui.repo` stays null and the page works as it always has. The page
// only ever selects an id `/api/repos` lists as live; any other id (a stale bookmark, a typed one) is never sent.
// The pure helpers come first and touch no DOM, so a node test loads this file alone.

const NO_REPO_TEXT = "No repository to show: add a folder to workspace.roots in your user config, or start agentflow dashboard inside a repository.";
const REPO_KEY = "agentflow-repo";  // the tab's selection, in sessionStorage: a reload keeps it, another tab has its own
const REPO_NOTE = { not_set_up: "not set up: run agentflow init in it", missing: "missing: no longer in the workspace, its runs still show" };

// What `ui.multi` is set from: the server holds more than one repository, whatever their states.
const isMulti = (view) => !!view && Array.isArray(view.repos) && view.repos.length > 1;
const selectable = (row) => !!row && row.state === "live";
const repoRow = (view, id) => (view && Array.isArray(view.repos) && typeof id === "string" ? view.repos.find((r) => r.id === id) || null : null);

// The repository to show: the first live one of the hash's, the stored one, the server's default, then the first live row.
function pickRepo(view, hashRepo, stored) {
  for (const id of [hashRepo, stored, view && view.default]) if (selectable(repoRow(view, id))) return id;
  const live = ((view && view.repos) || []).find(selectable);
  return live ? live.id : null;
}

// The repository a route lands in: the hash's when it is live; else the selection, which never moves by itself (a
// selected repository whose pack broke stays selected, and says so); with none, `pickRepo` from the stored one.
function routeRepo(view, hashRepo, current, stored) {
  if (selectable(repoRow(view, hashRepo))) return hashRepo;
  if (current !== null && current !== undefined) return current;
  return pickRepo(view, null, stored);
}

// The chips, as data: the live, broken and missing repositories in the server's order, then the ones not set up.
function chipModel(view, selected) {
  const rows = (view && Array.isArray(view.repos)) ? view.repos : [];
  const ordered = [...rows.filter((r) => r.state !== "not_set_up"), ...rows.filter((r) => r.state === "not_set_up")];
  return ordered.map((r) => ({ id: r.id, name: r.name || r.id, waiting: typeof r.waiting === "number" ? r.waiting : 0, state: r.state,
    selected: r.id === selected, disabled: !selectable(r), note: r.error || null }));
}

function tabTitle(view) {
  const rows = (view && Array.isArray(view.repos)) ? view.repos : [];
  if (rows.length === 1) return "agentflow · " + (rows[0].name || rows[0].id);
  return rows.length ? "agentflow · " + rows.length + " repositories" : "agentflow";
}

const waitingTotal = (view) => ((view && view.repos) || []).reduce((sum, r) => sum + (typeof r.waiting === "number" ? r.waiting : 0), 0);

// A stream message is the page's own when the page shows one repository, or when it is the selected repository's.
const streamOwn = (msg, multi, repo) => !multi || (!!msg && msg.repo === repo);

// Nothing to fetch: a page of several with no repository selected, or a page with no repository at all. A page of one
// whose repository is not live still asks the legacy routes, so it shows their reason, as it always has; so does a
// page of several whose selected repository stopped being live.
const nothingToShow = (view, multi, repo) => !!view && (multi ? repo === null : !(Array.isArray(view.repos) && view.repos.length));

const repoName = (id) => { const row = repoRow(ui.repos, id); return row && row.name ? row.name : String(id === null || id === undefined ? "" : id); };

// A merged view's `errors` (`[{repo, error}]`: a repository whose read failed) as lines of text, `<repo name>: <error>`.
const viewErrorLines = (errors) => (Array.isArray(errors) ? errors : []).map((e) => repoName(e && e.repo) + ": " + String((e && e.error) || ""));

// Which repository the Inbox and Agents ask for: on a page of several with the All chip on, every one (a workspace
// route, null); otherwise the selected one (undefined), which is tagged when the server holds several.
const viewRepo = () => (ui.multi && ui.allRepos ? null : undefined);

// ---- the selection, kept for the tab

function storedRepo() {
  try { return sessionStorage.getItem(REPO_KEY); } catch (err) { return null; }
}

function storeRepo(id) {
  try { if (typeof id === "string") sessionStorage.setItem(REPO_KEY, id); } catch (err) { console.warn("agentflow: session storage unavailable", err); }
}

// ---- the page: the list, the chips and a switch

let reposTried = false;  // the first /api/repos answer (or failure) came: until then route() waits

// Fetch `/api/repos` (a workspace route), then settle the selection, the tab title, the chips and, on a page of several,
// the header's Inbox count. When the set changes under an open page the view stays: a second repository appearing
// selects the default (the one the page already showed), and a selected repository that stops being live stays selected.
async function loadRepos() {
  let view = null;
  try { view = await api("/api/repos", undefined, null); } catch (err) { console.warn("agentflow: repositories unavailable", err); }
  const first = !reposTried;
  reposTried = true;
  if (!view || !Array.isArray(view.repos)) return;
  const before = ui.repo;
  const wasMulti = ui.multi;
  ui.repos = view;
  ui.multi = isMulti(view);
  let reset = false;
  if (!ui.multi) {
    if (ui.repo !== null) {  // down to one repository: the legacy routes serve it
      if (view.repos.length === 1 && view.repos[0].id === ui.repo) ui.repo = null;
      else { applyRepo(null); reset = true; }
    }
  } else if (ui.repo === null) {
    // a page that just became one of several takes the default, which is what the legacy routes were showing
    const pick = pickRepo(view, parseRoute(location.hash).repo, first || wasMulti ? storedRepo() : null);
    if (pick !== null && (first || (!wasMulti && pick === view.default))) { ui.repo = pick; storeRepo(pick); }  // what is shown already is its
    else if (pick !== null) { applyRepo(pick); reset = true; }
  }
  document.title = tabTitle(view);
  if (ui.multi) setInboxBadge(waitingTotal(view));
  renderRepos();
  renderAllChips();
  if (first || ui.repo === before) return;
  if (reset) route();
  else history.replaceState(null, "", routeHash(Object.assign(parseRoute(location.hash), { repo: ui.repo })));  // the prefix alone
}

// The "All repositories" chips of the Inbox and Agents: shown on a page of several, one choice for both.
function renderAllChips() {
  for (const id of ["inbox-all", "agents-all"]) {
    const chip = $(id);
    if (!chip) continue;
    chip.hidden = !ui.multi;
    chip.setAttribute("aria-pressed", String(ui.allRepos));
  }
}

// A click on either chip: the other views of the same rows change with it, so what was drawn from the old choice is
// dropped (an item missing from the new view is not "decided elsewhere") and the next round fetches the new one.
function setAllRepos(on) {
  if (!!on === ui.allRepos) return;
  ui.allRepos = !!on;
  disarmInbox();
  ui.inbox.view = null;
  ui.inbox.gone.clear();
  ui.inbox.sent.clear();
  ui.agents = null;
  dirty.inbox = true;
  dirty.agents = true;
  renderAllChips();
  boardRefresh();
}

function repoChipNode(chip) {
  const title = chip.disabled ? chip.note || REPO_NOTE[chip.state] || chip.state : (chip.waiting ? chip.waiting + " waiting on you" : null);
  return el("button", { type: "button", class: "repo-chip" + (chip.state === "not_set_up" ? " greyed" : ""), "aria-pressed": String(chip.selected),
    disabled: chip.disabled ? "disabled" : null, title: title, "data-state": chip.state, onclick: () => selectRepo(chip.id) },
  chip.name, chip.waiting > 0 ? el("span", { class: "repo-badge", text: String(chip.waiting) }) : null);
}

function renderRepos() {
  const nav = $("repos");
  if (!nav) return;
  nav.hidden = !ui.multi;
  nav.replaceChildren(...(ui.multi ? chipModel(ui.repos, ui.repo).map(repoChipNode) : []));
}

// Whether the page may leave the selected repository: Settings holds no unsaved edit, or the reader lets them go (then
// they are dropped here, so the route that follows does not ask again). A chip click and a route both ask.
function settingsMayLeave() {
  if (!settings.edits.size && !settings.yamlDirty) return true;
  if (!confirm("Discard the unsaved Settings changes in " + repoName(ui.repo) + "?")) return false;
  settings.edits.clear();
  settings.yamlDirty = false;
  return true;
}

// A chip click: the same view under the new prefix (a Run page goes to the Board). Nothing on the server changes.
function selectRepo(id) {
  if (id === ui.repo || !selectable(repoRow(ui.repos, id))) return;
  if (!settingsMayLeave()) return;
  storeRepo(id);
  const where = parseRoute(location.hash);
  location.hash = routeHash(where.view === "run" ? { view: "board", query: ui.boardQuery, repo: id } : Object.assign(where, { repo: id }));
}

// Show `id` (null: the legacy routes): what the page holds is the old repository's, so it is dropped, and nothing of it
// is drawn or acted on under the new one. route() calls it when the hash's repository differs from the selection.
function applyRepo(id) {
  if (id === ui.repo) return;
  ui.repo = id;
  storeRepo(id);
  ui.board = null;
  ui.workflow = null;
  ui.agents = null;
  ui.run = null;
  ui.artifact = null;
  ui.files = null;
  ui.sessions = null;
  ui.plan = null;
  ui.activity = null;
  ui.comments.clear();  // keyed by run id, path and sha: another repository's run of the same id is another run
  lastActivity.clear();
  ui.inbox.view = null;
  ui.inbox.gone.clear();
  ui.inbox.armed = null;
  disarmBacklog();  // an idea armed in the old repository never starts the new one's idea of the same key
  closePeek();
  settings.request++;  // a /api/config answer still out is the old repository's: it is dropped
  settings.data = null;
  settings.edits.clear();
  settings.yamlDirty = false;
  $("columns").replaceChildren();
  $("needs").replaceChildren();
  $("summary").textContent = id === null ? "loading…" : "loading " + repoName(id) + "…";
  dirty.board = true;
  dirty.agents = true;
  dirty.inbox = true;
  dirty.runs.clear();
  renderRepos();
  loadInfo();
  boardRefresh();
}
