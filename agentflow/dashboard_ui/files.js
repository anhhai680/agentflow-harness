"use strict";

// ------------------------------------------------------------------ the Files tab

// The run's own documents (`/api/runs/<id>/files`) and the branch's changes with a diff per file (`/changes`). Every
// string from the server reaches the page as text. A name or path from the URL goes to the server only as a query
// parameter, and the server checks it against its own list. The pure helpers sit first and touch no DOM.

const FILES_TAB = "files";
const GROUP_ORDER = ["request", "handoffs", "refine", "pull request", "notes"];
const MAX_CHANGES = 2000;  // the server lists no more files than this

function fileSize(bytes) {
  if (bytes < 1024) return bytes + " B";
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " KB";
  return (bytes / (1024 * 1024)).toFixed(1) + " MB";
}

// The list as `[{group, files}]`: in the server's group order, any group it names that is not known after those.
function groupFiles(list) {
  const names = Array.from(new Set((list || []).map((f) => f.group)));
  const order = GROUP_ORDER.filter((g) => names.includes(g)).concat(names.filter((g) => !GROUP_ORDER.includes(g)));
  return order.map((group) => ({ group: group, files: list.filter((f) => f.group === group) }));
}

// The file the tab shows: the one the route names, or the first of the list.
function pickFile(list, sub) {
  if (sub) return sub;
  return list && list.length ? list[0].name : null;
}

function changesSummary(changes) {
  const files = (changes && changes.files) || [];
  const added = files.reduce((sum, f) => sum + (f.added || 0), 0);
  const deleted = files.reduce((sum, f) => sum + (f.deleted || 0), 0);
  return "Changes since the base: " + files.length + (files.length === 1 ? " file" : " files") + ", +" + added + " −" + deleted;
}

const countsText = (f) => (f.added === null || f.added === undefined ? "binary" : "+" + f.added + " −" + (f.deleted || 0));

// ---- state: per run. Texts by name, diffs by `path@head`, so an open diff survives a refresh and a new head refetches.

function filesState(runId) {
  let st = ui.files;
  if (!st || st.runId !== runId) {
    st = ui.files = { runId: runId, texts: new Map(), diffs: new Map(), list: null, changes: null, error: null, changesError: null,
      mode: "rendered", open: new Set(), failed: new Map(), busy: new Set(), inflight: false };
  }
  return st;
}

const live = (st) => ui.files === st;

function redrawFiles(runId) {
  const run = ui.run;
  if (run && run.id === runId && run.detail && run.tab === FILES_TAB) renderRun(run.detail);
}

// Fetch one cached read once. A failure is told and not retried until the next refresh; what is drawn stays.
async function fetchCached(st, cache, key, url, what) {
  if (cache.has(key) || st.failed.has(key) || st.busy.has(key)) return;
  st.busy.add(key);
  try {
    const result = await api(runPath(st.runId) + url);
    if (!live(st)) return;
    cache.set(key, result);
  } catch (err) {
    if (!live(st)) return;
    st.failed.set(key, err.message);
    toast("Could not read " + what + ": " + err.message);
  } finally {
    st.busy.delete(key);
  }
  redrawFiles(st.runId);
}

const fetchText = (st, name) => fetchCached(st, st.texts, name, "/files?name=" + encodeURIComponent(name), name);
const fetchDiff = (st, path, head) => fetchCached(st, st.diffs, path + "@" + head, "/changes?path=" + encodeURIComponent(path), "the diff of " + path);

// The list, the changes and the chosen file, fetched again. A failure keeps what is drawn (and says so); a first one
// says why in the tab. No branch yet (a 404 from /changes) is an answer, not a failure.
async function loadFiles(runId) {
  const st = ui.files;
  if (!st || st.runId !== runId || st.inflight) return;
  st.inflight = true;
  st.failed.clear();  // a refresh tries a failed read again
  const base = runPath(runId);
  const [list, changes] = await Promise.all([
    api(base + "/files").then((data) => ({ data: data }), (err) => ({ err: err })),
    api(base + "/changes").then((data) => ({ data: data }), (err) => ({ err: err }))]);
  if (!live(st)) return;
  if (list.err) { if (st.list) toast("Could not refresh the files: " + list.err.message); else st.error = list.err.message; }
  else { st.list = Array.isArray(list.data) ? list.data : []; st.error = null; }
  if (changes.err) {
    if (changes.err.status === 404) { st.changes = null; st.changesError = "none"; }
    else if (st.changes) toast("Could not refresh the changes: " + changes.err.message);
    else st.changesError = changes.err.message;
  } else { st.changes = changes.data; st.changesError = null; }
  const jobs = [];
  const run = ui.run;
  const name = pickFile(st.list, run && run.id === runId ? run.sub : null);
  if (name && st.list && st.list.length) {  // the open document is read again: a handoff grows while its step runs
    jobs.push(api(base + "/files?name=" + encodeURIComponent(name)).then((text) => { if (live(st)) st.texts.set(name, text); }, (err) => {
      if (live(st) && st.texts.has(name)) toast("Could not refresh " + name + ": " + err.message);
      else if (live(st)) st.failed.set(name, err.message);
    }));
  }
  if (st.changes) for (const f of st.changes.files || []) if (st.open.has(f.path)) st.diffs.has(f.path + "@" + st.changes.head) || jobs.push(fetchDiff(st, f.path, st.changes.head));
  await Promise.all(jobs);
  st.inflight = false;
  if (live(st)) redrawFiles(runId);
}

// ---- drawing

const note = (key, text) => entry(key, text, (t) => el("p", { class: "muted art-note", text: t }));

function listNode(d) {
  return el("div", { class: "files-list" }, d.groups.map((g) => el("div", { class: "files-group" },
    el("h3", { class: "files-group-name", text: g.group }),
    el("ul", { class: "files-names" }, g.files.map((f) => el("li", {},
      el("a", { href: routeHash({ view: "run", run: d.runId, tab: FILES_TAB, sub: f.name }), text: f.name, "aria-current": f.name === d.name ? "page" : null }),
      el("span", { class: "muted", text: " " + fileSize(f.size) })))))));
}

function fileDocNode(d) {
  const markdown = /\.md$/i.test(d.name);
  const raw = d.mode === "raw" || !markdown;
  const toggle = markdown ? el("button", { type: "button", class: "chip", text: raw ? "Rendered" : "Raw", "aria-pressed": "false",
    onclick: () => { if (ui.files) { ui.files.mode = raw ? "rendered" : "raw"; redrawFiles(ui.files.runId); } } }) : null;
  const kb = Math.max(1, Math.round(d.text.length / 1024));
  return el("section", { class: "files-doc" },
    el("div", { class: "files-doc-head" }, el("strong", { text: d.name }), el("span", { class: "muted", text: " " + fileSize(d.size) + " " }), toggle,
      d.truncated ? el("span", { class: "st-wait", text: "Showing the first " + kb + " KB." }) : null),
    raw ? el("pre", { class: "files-raw", text: d.text || "(empty)" }) : el("div", { class: "md" }, markdownNodes(parseMarkdown(d.text))));
}

function changeNode(f, st, head) {
  const path = f.path;
  const cached = st.diffs.get(path + "@" + head);
  const failure = st.failed.get(path + "@" + head);
  let body;
  if (cached) body = [cached.diff ? diffNode(cached.diff) : el("p", { class: "muted", text: "(no textual diff)" }),
    cached.truncated ? el("p", { class: "st-wait", text: "The diff is cut here." }) : null];
  else body = [el("p", { class: failure ? "st-bad" : "muted", text: failure ? "Could not read the diff: " + failure : "Loading the diff…" })];
  return el("details", { class: "change", "data-key": "change:" + path, ontoggle: (ev) => {
    const now = ui.files;
    if (!now) return;
    if (ev.target.open) { now.open.add(path); fetchDiff(now, path, head); } else now.open.delete(path);
  } }, el("summary", {}, el("span", { class: "change-status", text: f.status }), el("code", { class: "change-path", text: path }),
    el("span", { class: "muted", text: " " + countsText(f) })), el("div", { class: "change-diff" }, body));
}

function changesNode(d) {
  const st = ui.files;
  return el("section", { class: "files-changes" }, el("h3", { text: d.summary }),
    d.complete ? null : el("p", { class: "st-wait", text: "Only the first " + MAX_CHANGES + " files are listed." }),
    d.files.map((f) => changeNode(f, st, d.head)));
}

function changesEntry(st) {
  if (st.changesError === "none") return note("files:changes", "No branch yet: nothing has changed.");
  if (!st.changes) return note("files:changes", st.changesError ? "Could not load the changes: " + st.changesError : "Loading the changes…");
  const diffs = (st.changes.files || []).map((f) => { const c = st.diffs.get(f.path + "@" + st.changes.head); return c ? c.diff.length + ":" + !!c.truncated : st.failed.get(f.path + "@" + st.changes.head) || ""; });
  return entry("files:changes", { summary: changesSummary(st.changes), complete: st.changes.complete !== false, head: st.changes.head, files: st.changes.files || [], diffs: diffs }, changesNode);
}

function fileEntries(detail, sub) {
  const st = filesState(detail.run_id);
  const tail = [entry("files:transcript", { title: "Transcript tail" + (detail.transcript_file ? " (" + detail.transcript_file + ")" : ""), text: detail.transcript },
    (f) => section(f.title, f.text, false, "files:transcript")),
  entry("files:records", { title: "Step records", text: recordsText(detail) }, (f) => section(f.title, f.text, false, "files:records"))];
  if (st.list === null) {
    if (!st.error) loadFiles(detail.run_id);
    return [note("files:list", st.error ? "Could not load the files: " + st.error : "Loading the files…"), changesEntry(st), ...tail];
  }
  const parts = [];
  const name = pickFile(st.list, sub);
  if (!st.list.length) parts.push(note("files:list", "This run has no documents yet."));
  else parts.push(entry("files:list", { groups: groupFiles(st.list), runId: detail.run_id, name: name }, listNode));
  if (name) {
    const text = st.texts.get(name);
    if (text) parts.push(entry("files:doc:" + name, { name: name, text: text.text || "", size: text.size, truncated: !!text.truncated, mode: st.mode }, fileDocNode));
    else {
      fetchText(st, name);
      const failure = st.failed.get(name);
      parts.push(note("files:doc:" + name, failure ? "Could not read " + name + ": " + failure : "Loading " + name + "…"));
    }
  }
  parts.push(changesEntry(st), ...tail);
  return parts;
}
