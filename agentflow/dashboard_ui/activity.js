"use strict";

// ------------------------------------------------------------------ the Activity tab

// A run's whole log told as sentences (`/api/runs/<id>/events`, newest first, in pages of 200 entries, by kind). The
// newest page is refetched on every refresh and merged into what is shown, so new lines appear at the top and the
// older pages already loaded stay. Everything from the server reaches the page as text. The pure helpers sit first
// and touch no DOM, so a test runs them under node.

const ACTIVITY_TAB = "activity";  // the Activity tab's id in RUN_TABS
const ACTIVITY_PAGE = 200;  // the entries asked for at a time
const ACTIVITY_FILTERS = [["all", "All"], ["decisions", "Decisions"], ["agents", "Agents"], ["gates", "Gates"], ["commits", "Commits"], ["stops", "Stops"]];

// The query of one page: the filter (none for "all"), the cursor of an older page, and the agent turns left unfolded.
function activityQuery(state) {
  let query = "?limit=" + ACTIVITY_PAGE;
  if (state.filter && state.filter !== "all") query += "&kinds=" + encodeURIComponent(state.filter);
  if (state.before !== null && state.before !== undefined) query += "&before=" + encodeURIComponent(state.before);
  if (state.turns) query += "&turns=1";
  return query;
}

// `have` and `page` as one list, newest first, one entry per `seq`: an entry seen again (a refetched newest page)
// replaces the old one. A folded `agent_activity` grows at its newer end, so its `seq` moves: the one with the same
// `first_seq` is the same fold, and the old copy goes.
function mergePages(have, page) {
  const bySeq = new Map();
  for (const e of have || []) bySeq.set(e.seq, e);
  for (const e of page || []) {
    if (e.type === "agent_activity" && e.first_seq !== undefined) {
      for (const [seq, old] of bySeq) if (old.type === "agent_activity" && old.first_seq === e.first_seq) bySeq.delete(seq);
    }
    bySeq.set(e.seq, e);
  }
  return Array.from(bySeq.values()).sort((a, b) => b.seq - a.seq);
}

// ---- state: per run. The filter, the entries loaded so far and the cursor of the page before them.

function activityState(runId) {
  let st = ui.activity;
  if (!st || st.runId !== runId) {
    st = ui.activity = { runId: runId, filter: "all", turns: false, entries: [], before: null, total: 0, open: new Set(),
      loaded: false, error: null, loading: false, loadingOlder: false, deeper: false, epoch: 0 };
  }
  return st;
}

// A new filter, or the agent turns shown or folded, starts the list again: its entries and cursor are another view's.
// A page still out for the old view is dropped when it answers (a new epoch), and does not hold up the new one.
function resetActivity(st, change) {
  Object.assign(st, change, { entries: [], before: null, total: 0, loaded: false, error: null, loading: false, loadingOlder: false,
    deeper: false, epoch: st.epoch + 1 });
  loadActivity(st.runId, false);
  redrawActivity(st.runId);
}

function redrawActivity(runId) {
  const run = ui.run;
  if (run && run.id === runId && run.detail && run.tab === ACTIVITY_TAB) renderRun(run.detail);
}

// Take one fetched page into the state. The newest page (`older` false) is merged at the top; the cursor stays where
// the entries held reach, unless there are none yet, or the page does not touch them (more events than a page came
// between two refreshes): then the page replaces them. An older page is merged below and moves the cursor back.
function takePage(st, page, older, asked) {
  const entries = Array.isArray(page.entries) ? page.entries : [];
  const cursor = typeof page.before === "number" ? page.before : null;
  st.total = typeof page.total === "number" ? page.total : st.total;
  if (older) {
    st.entries = mergePages(st.entries, entries);
    if (st.before === asked) st.before = cursor;
    st.deeper = true;
    return;
  }
  const newest = st.entries.length ? st.entries[0].seq : null;
  if (newest === null || (cursor !== null && cursor > newest)) {
    st.entries = mergePages([], entries);
    st.before = cursor;
    st.deeper = false;
  } else {
    st.entries = mergePages(st.entries, entries);
  }
}

// The newest page (a refresh), or the page before the oldest shown. A failure keeps what is drawn (and says so); a
// first one is told in the tab. A page that answers for another view of the list, or after the run changed, is dropped.
async function loadActivity(runId, older) {
  const st = activityState(runId);
  if (older ? st.loadingOlder || st.before === null : st.loading) return;
  const asked = older ? st.before : null;
  const epoch = st.epoch;
  if (older) st.loadingOlder = true;
  else st.loading = true;
  let page = null;
  try {
    page = await api(runPath(runId) + "/events" + activityQuery({ filter: st.filter, turns: st.turns, before: asked }));
  } catch (err) {
    if (ui.activity === st && st.epoch === epoch) {
      if (st.loaded) toast("Could not refresh the activity: " + err.message);
      else st.error = err.message;
    }
  }
  if (ui.activity !== st || st.epoch !== epoch) return;  // another run, or another view of the list
  if (older) st.loadingOlder = false;
  else st.loading = false;
  if (page !== null) {
    takePage(st, page, older, asked);
    st.loaded = true;
    st.error = null;
  }
  redrawActivity(runId);
}

// ---- drawing

const activityTime = (e) => (e.ts ? String(e.ts).slice(11, 19) : "");

// A sentence: its headline, then its detail in `<em>` when it has one. The Activity tab's lines and Overview's
// "Recent activity" both draw it.
function sentenceNodes(e) {
  return [el("span", { class: "act-headline", text: e.headline || e.type || "" }), e.detail ? el("em", { text: e.detail }) : null];
}

function activityLine(e, open, st) {
  const raw = e.raw
    ? el("details", { class: "act-raw", "data-key": "raw", open: open ? "open" : null,
      ontoggle: (ev) => { if (ev.target.open) st.open.add(e.seq); else st.open.delete(e.seq); } },
    el("summary", { text: "Raw" }), el("pre", { text: e.raw }))
    : null;
  return el("div", { class: "act-line" }, el("span", { class: "act-time muted", text: activityTime(e), title: e.ts || null }), sentenceNodes(e), raw);
}

function activityFilters(d) {
  const st = ui.activity;
  const pick = (value) => () => { if (st && ui.activity === st) { resetActivity(st, { filter: value }); focusById("act-chip-" + value); } };
  const box = el("input", { type: "checkbox", id: "act-turns", checked: d.turns ? "checked" : null });
  box.addEventListener("change", () => { if (st && ui.activity === st) { resetActivity(st, { turns: box.checked }); focusById("act-turns"); } });
  return el("div", { class: "act-filters" },
    el("span", { class: "chips", role: "group", "aria-label": "Show" },
      ACTIVITY_FILTERS.map(([value, label]) => chip(label, pick(value), d.filter === value, { id: "act-chip-" + value }))),
    el("label", { class: "act-turns" }, box, " Show agent turns"));
}

// The rebuilt filter bar is a new node: the control the reader used gets the focus back.
function focusById(id) {
  const node = document.getElementById(id);
  if (node && typeof node.focus === "function") node.focus({ preventScroll: true });
}

const activityNote = (key, text) => entry(key, text, (t) => el("p", { class: "muted art-note", text: t }));

// The Activity tab. Lines are keyed `seq:<seq>`, so a refresh adds the new ones at the top and leaves the rest, and a
// Raw section the reader opened stays open (its seq is in `open`, which outlives the node).
function activityEntries(detail) {
  const st = activityState(detail.run_id);
  if (!st.loaded && !st.loading && !st.error) loadActivity(detail.run_id, false);
  const parts = [entry("activity:filters", { filter: st.filter, turns: st.turns }, activityFilters)];
  if (!st.loaded) {
    parts.push(activityNote("activity:note", st.error ? "Could not load the activity: " + st.error : "Loading the activity…"));
    return parts;
  }
  parts.push(entry("activity:count", st.entries.length + " of " + st.total, (text) => el("p", { class: "muted act-count", text: text })));
  if (!st.entries.length) {
    parts.push(activityNote("activity:end", "No event matches this filter."));
    return parts;
  }
  for (const e of st.entries) parts.push(entry("seq:" + e.seq, e, (line) => activityLine(line, st.open.has(line.seq), st)));
  if (st.before !== null) {
    parts.push(entry("activity:end", { busy: st.loadingOlder }, (d) => el("button", { type: "button", class: "act-end", text: d.busy ? "Loading…" : "Load older",
      disabled: d.busy ? "disabled" : null, onclick: () => loadActivity(detail.run_id, true) })));
  } else {
    parts.push(activityNote("activity:end", "The first event of this run."));
  }
  return parts;
}
