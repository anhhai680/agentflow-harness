"use strict";

// ------------------------------------------------------------------ the Agents tab and the transcript viewer

// Every agent session a run started (`/api/runs/<id>/sessions`, newest first) and one session's transcript as
// conversation turns (`/sessions/<sid>/transcript?after=<n>`, read in pages of whole lines). Everything from the
// server reaches the page as text: a tool's input and its result only ever as a `<pre>`'s text. The pure helpers sit
// first and touch no DOM, so a test runs them under node.

const SESSIONS_TAB = "agents";  // the Agents tab's id in RUN_TABS
const FOLLOW_SLACK_PX = 200;  // a reader this close to the bottom is following; further up, new turns wait below

// The rows the filter keeps. `failed` keeps a row whose exit code is set and is not 0.
function filterSessions(rows, f) {
  const want = f || {};
  return (rows || []).filter((r) => (!want.phase || r.phase === want.phase) && (!want.role || r.role === want.role)
    && (!want.failed || (r.exit_code !== null && r.exit_code !== undefined && r.exit_code !== 0)));
}

// `[{phase, rows}]`: the live rows first, as phase "live", then one group per phase in the order of its newest
// session. The rows come newest first and keep that order.
function groupSessions(rows) {
  const groups = [];
  const live = (rows || []).filter((r) => r.live);
  if (live.length) groups.push({ phase: "live", rows: live });
  const byPhase = new Map();
  for (const r of rows || []) {
    if (r.live) continue;
    const phase = r.phase === undefined ? null : r.phase;
    if (!byPhase.has(phase)) { byPhase.set(phase, []); groups.push({ phase: phase, rows: byPhase.get(phase) }); }
    byPhase.get(phase).push(r);
  }
  return groups;
}

const largerSeconds = (a, b) => (typeof a !== "number" ? (typeof b === "number" ? b : null) : typeof b !== "number" ? a : Math.max(a, b));

// `have` with the next page's turns after it. A `tool_result` goes into the `tool` call with its id (from any page) as
// `result` and `is_error`; one with no call stays a turn of its own. A wait right after a wait joins it. Turns are
// only appended or filled in, never moved, so a turn's index is its key for good.
function mergeTurns(have, page) {
  const out = (have || []).slice();
  const calls = new Map();
  out.forEach((t, i) => { if (t && t.kind === "tool" && t.id !== null && t.id !== undefined) calls.set(String(t.id), i); });
  for (const t of page || []) {
    if (!t || typeof t !== "object") continue;
    if (t.kind === "tool_result" && t.id !== null && t.id !== undefined && calls.has(String(t.id))) {
      const i = calls.get(String(t.id));
      out[i] = Object.assign({}, out[i], { result: t.text, is_error: !!t.is_error });
      continue;
    }
    const last = out[out.length - 1];
    if (t.kind === "wait" && last && last.kind === "wait") {
      out[out.length - 1] = Object.assign({}, last, { records: (last.records || 0) + (t.records || 0), seconds: largerSeconds(last.seconds, t.seconds) });
      continue;
    }
    out.push(Object.assign({}, t));
    if (t.kind === "tool" && t.id !== null && t.id !== undefined) calls.set(String(t.id), out.length - 1);
  }
  return out;
}

function waitText(turn) {
  if (typeof turn.seconds === "number") return "waited " + Math.round(turn.seconds) + " s";
  const n = turn.records || 0;
  return "waited (" + n + (n === 1 ? " update)" : " updates)");
}

// The status mark of a row: `● live`, `✓ exit 0`, `✗ exit <n>`, or `○ no exit` for one that ended without saying.
function sessionMark(r) {
  if (r.live) return { cls: "st-run", text: "● live" };
  if (r.exit_code === 0) return { cls: "st-ok", text: "✓ exit 0" };
  if (r.exit_code === null || r.exit_code === undefined) return { cls: "muted", text: "○ no exit" };
  return { cls: "st-bad", text: "✗ exit " + r.exit_code };
}

const sessionsTotal = (rows) => (rows || []).reduce((sum, r) => sum + (typeof r.cost === "number" ? r.cost : 0), 0);
const kbLeft = (st) => Math.max(1, Math.ceil((st.size - st.next) / 1024));
const uniqueOf = (rows, field) => Array.from(new Set((rows || []).map((r) => r[field]).filter((v) => v !== null && v !== undefined)));

// ---- state: per run. The filter, the list, and the one open session's turns.

function sessionsState(runId) {
  let st = ui.sessions;
  if (!st || st.runId !== runId) {
    st = ui.sessions = { runId: runId, rows: null, filter: { phase: null, role: null, failed: false }, open: null, turns: [], next: 0,
      size: 0, done: false, missing: false, session: null, loaded: false, error: null, turnsError: null, inflight: false, reading: false,
      below: false, epoch: 0 };
  }
  return st;
}

// Show `sid`'s transcript from its start (null: the list). A session opened again is read again. Each opening is a
// new epoch: a read still out from an earlier one is dropped when it answers, and does not hold up this one's reads.
function openTranscript(st, sid) {
  if (st.open === sid) return;
  Object.assign(st, { open: sid, turns: [], next: 0, size: 0, done: false, missing: false, session: null, loaded: false, turnsError: null, below: false,
    epoch: st.epoch + 1, reading: false });
  if (sid) readTranscript(st, false);
}

// The open session's row: the list's, fresher than the one the last page carried.
const openSessionRow = (st) => (st.rows || []).find((r) => r.id === st.open) || st.session;

function redrawSessions(runId) {
  const run = ui.run;
  if (run && run.id === runId && run.detail && run.tab === SESSIONS_TAB) renderRun(run.detail);
}

// The list, fetched. A failure keeps the list already drawn (and says so); a first one is told in the tab.
async function loadSessions(runId) {
  const st = ui.sessions;
  if (!st || st.runId !== runId || st.inflight) return;
  st.inflight = true;
  let rows = null;
  try {
    rows = await api(runPath(runId) + "/sessions");
  } catch (err) {
    if (ui.sessions === st) {
      if (st.rows) toast("Could not refresh the sessions: " + err.message);
      else st.error = err.message;
    }
  }
  st.inflight = false;
  if (ui.sessions !== st) return;
  if (rows !== null) { st.rows = Array.isArray(rows) ? rows : []; st.error = null; }
  redrawSessions(runId);
}

// The next page of the open transcript, from `next`, appended. `follow`: keep the page at its bottom afterwards.
// A page that answers after the session was closed or opened again (another epoch), or after `next` moved, is dropped.
async function readTranscript(st, follow) {
  const sid = st.open;
  if (!sid || st.reading) return;
  st.reading = true;
  const epoch = st.epoch;
  const after = st.next;
  const current = () => ui.sessions === st && st.epoch === epoch && st.open === sid && st.next === after;
  let page = null;
  try {
    page = await api(runPath(st.runId) + "/sessions/" + encodeURIComponent(sid) + "/transcript?after=" + after);
  } catch (err) {
    if (current()) {
      if (st.loaded) toast("Could not read more of the transcript: " + err.message);
      else st.turnsError = err.message;
    }
  }
  if (st.epoch !== epoch) return;  // `reading` is the newer epoch's now
  st.reading = false;
  if (!current()) return;
  const grew = !!page && ((page.turns || []).length > 0 || page.next !== st.next);
  if (page) {
    st.turns = mergeTurns(st.turns, page.turns || []);
    Object.assign(st, { next: page.next || 0, size: page.size || 0, done: !!page.done, missing: !!page.missing, session: page.session || st.session,
      loaded: true, turnsError: null });
  }
  redrawSessions(st.runId);
  if (follow && grew) toPageBottom();
}

const nearPageBottom = () => window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - FOLLOW_SLACK_PX;
const toPageBottom = () => window.scrollTo(0, document.documentElement.scrollHeight);

// True while the open session may still grow: it is live, or its last page said there is more.
function transcriptGrows(st) {
  const row = openSessionRow(st);
  return !!st.open && st.loaded && ((row && row.live) || !st.done);
}

// The open session took a turn. Within FOLLOW_SLACK_PX of the bottom, fetch it (at most every `boardRefreshMs`) and
// stay at the bottom; further up, say "New turns below" and leave the page where it is.
let followThrottled = null;
function transcriptFollow() {
  if (!followThrottled) followThrottled = throttle(followTranscript, () => boardRefreshMs);
  followThrottled();
}

async function followTranscript() {
  const st = ui.sessions;
  const run = ui.run;
  if (!st || !st.open || !st.loaded || !run || run.id !== st.runId || run.tab !== SESSIONS_TAB) return;
  if (!nearPageBottom()) {
    if (!st.below) { st.below = true; redrawSessions(st.runId); }
    return;
  }
  st.below = false;
  await readTranscript(st, true);
}

// A run refresh: a session whose `session_id` the activity stream cannot name yet is caught up here, and only when
// the reader is at the bottom. A reader further up is not told of turns no one has seen arrive.
function transcriptCatchUp() {
  const st = ui.sessions;
  const run = ui.run;
  if (!st || !run || run.id !== st.runId || run.tab !== SESSIONS_TAB || !transcriptGrows(st) || st.below || !nearPageBottom()) return;
  readTranscript(st, true);
}

// An `agent_activity` message: the row of its session takes the new context and tool at once. True when it is the
// session open in the viewer, whose transcript then follows.
function sessionActivity(runId, event) {
  const st = ui.sessions;
  if (!st || st.runId !== runId || !event || event.session_id === null || event.session_id === undefined) return false;
  const row = (st.rows || []).find((r) => r.session_id === event.session_id && r.live);
  if (row) {
    const pct = typeof event.context_pct === "number" ? event.context_pct : row.context_pct;
    const tool = event.last_tool || row.last_tool;
    if (pct !== row.context_pct || tool !== row.last_tool) {  // a redraw only when the row would read differently
      row.context_pct = pct;
      row.last_tool = tool;
      redrawSessions(runId);
    }
  }
  const open = st.open ? openSessionRow(st) : null;
  return !!open && open.session_id === event.session_id && !!ui.run && ui.run.id === runId && ui.run.tab === SESSIONS_TAB;
}

// ---- drawing

const sessionNote = (key, text) => entry(key, text, (t) => el("p", { class: "muted art-note", text: t }));

function sessionFilterNode(d) {
  const set = (field, value) => () => { if (ui.sessions) { ui.sessions.filter[field] = value; redrawSessions(d.runId); } };
  const group = (label, field, values) => el("span", { class: "chips", role: "group", "aria-label": label },
    el("span", { class: "muted", text: label + ": " }), chip("All", set(field, null), d.filter[field] === null),
    values.map((v) => chip(String(v), set(field, v), d.filter[field] === v)));
  return el("div", { class: "sessions-head" }, el("strong", { text: d.summary }),
    group("Phase", "phase", d.phases), group("Role", "role", d.roles),
    chip("Failed only", set("failed", !d.filter.failed), d.filter.failed));
}

// One session as a row: its mark, label, model and effort, time, cost, context and result; a live one also its last
// tool and the attach box. In the list the row opens the session; in the viewer it is the header.
function sessionRowNode(d) {
  const r = d.row;
  const mark = sessionMark(r);
  const when = r.live ? ago(sinceSeconds(r.started_at)) : typeof r.duration_s === "number" ? dur(r.duration_s) : "-";
  const model = [r.model, r.effort].filter(Boolean).join(" · ") || "-";
  const facts = [when, money(r.cost), typeof r.context_pct === "number" ? "ctx " + Math.round(r.context_pct) + "%" : null,
    (r.subagents || []).length ? r.subagents.length + ((r.subagents.length === 1) ? " subagent" : " subagents") : null].filter(Boolean).join(" · ");
  const href = routeHash({ view: "run", run: d.runId, tab: SESSIONS_TAB, sub: r.id });
  const node = el("div", { class: "session-row" + (d.link ? " clickable" : "") + (r.live ? " session-live" : "") },
    el("div", { class: "session-line" },
      el("span", { class: "session-mark " + mark.cls, text: mark.text }),
      d.link ? el("a", { href: href, class: "session-label", text: r.label || r.id }) : el("strong", { class: "session-label", text: r.label || r.id }),
      el("span", { class: "muted", text: model, title: r.effort_reason ? "chose " + r.effort + ": " + r.effort_reason : null }),
      el("span", { class: "muted", text: facts }),
      r.transcript ? null : el("span", { class: "muted", text: "· no transcript" })),
    r.summary ? el("div", { class: "session-summary", text: "“" + r.summary + "”" }) : null,
    r.live && r.last_tool ? el("div", { class: "muted session-tool", text: "last: " + r.last_tool }) : null,
    r.live && d.attach ? attachNode(d.attach) : null);
  if (d.link) {
    node.addEventListener("click", (ev) => {
      if (ev.target.closest && ev.target.closest(".attach, a")) return;  // the link navigates itself; the attach box is for copying
      goRun(d.runId, SESSIONS_TAB, r.id);
    });
  }
  return node;
}

// One turn of a transcript. Text from the agent is a paragraph's text; a tool's input and result are `<pre>` text.
function turnNode(t) {
  const cls = (name) => "turn " + name + (t.parent ? " turn-sub" : "");
  if (t.kind === "text") return el("p", { class: cls("turn-text"), text: t.text || "" });
  if (t.kind === "thinking") return el("details", { class: cls("turn-fold turn-thinking") }, el("summary", { text: "Thinking" }), el("p", { class: "turn-text", text: t.text || "" }));
  if (t.kind === "tool") {
    const hasResult = t.result !== undefined && t.result !== null;
    return el("details", { class: cls("turn-fold turn-tool") },
      el("summary", {}, el("code", { text: t.line || t.name || "tool" }), t.is_error ? el("span", { class: "st-bad", text: " ✗" }) : null),
      el("div", { class: "muted", text: "Input" }), el("pre", { text: t.input || "" }),
      el("div", { class: "muted", text: hasResult ? "Result" : "No result yet" }), hasResult ? el("pre", { class: t.is_error ? "turn-error" : null, text: t.result || "(empty)" }) : null);
  }
  if (t.kind === "tool_result") {
    return el("details", { class: cls("turn-fold turn-tool") },
      el("summary", {}, el("span", { text: "Tool result" }), t.is_error ? el("span", { class: "st-bad", text: " ✗" }) : null),
      el("pre", { class: t.is_error ? "turn-error" : null, text: t.text || "(empty)" }));
  }
  if (t.kind === "wait") return el("div", { class: cls("turn-wait muted"), text: waitText(t) });
  if (t.kind === "result") {
    return el("div", { class: cls("turn-result" + (t.is_error ? " turn-result-error" : "")) },
      el("div", {}, el("strong", { text: t.is_error ? "Result (error)" : "Result" }), el("span", { class: "muted", text: " · " + money(t.cost) })),
      t.text ? el("p", { class: "turn-text", text: t.text }) : null);
  }
  if (t.kind === "session") return el("div", { class: cls("turn-wait muted"), text: "Session started" + (t.model ? " · " + t.model : "") });
  if (t.kind === "skipped") return el("div", { class: cls("turn-wait muted"), text: "A record of " + ((t.bytes || 0) / (1024 * 1024)).toFixed(1) + " MB was left out" });
  return el("pre", { class: cls("turn-raw muted"), text: t.text || "" });
}

function transcriptEnd(st) {
  const row = openSessionRow(st);
  if (st.missing) return sessionNote("turns:end", row && row.live ? "No transcript yet. Following this session live" : "No transcript was written.");
  if (!st.done && st.next < st.size) {
    return entry("turns:end", { left: kbLeft(st) }, (d) => el("div", { class: "turns-end" },
      el("button", { type: "button", text: "Load more (" + d.left + " KB left)", onclick: () => { if (ui.sessions) readTranscript(ui.sessions, false); } })));
  }
  if (row && row.live) return sessionNote("turns:end", "Following this session live");
  return null;
}

function transcriptEntries(detail, st, sub) {
  const runId = detail.run_id;
  openTranscript(st, sub);
  const parts = [entry("viewer:back", { runId: runId }, (d) => el("p", { class: "session-back" },
    el("a", { href: routeHash({ view: "run", run: d.runId, tab: SESSIONS_TAB }), text: "← All sessions" })))];
  const row = openSessionRow(st);
  if (row) parts.push(entry("session:" + row.id, { row: row, runId: runId, link: false, attach: detail.attach }, sessionRowNode));
  if (!st.loaded) {
    parts.push(sessionNote("turns:loading", st.turnsError ? "Could not read the transcript: " + st.turnsError : "Loading the transcript…"));
    return parts;
  }
  st.turns.forEach((t, i) => parts.push(entry("turn:" + i, t, turnNode)));
  const end = transcriptEnd(st);
  if (end) parts.push(end);
  if (st.below) {
    parts.push(entry("turns:below", null, () => el("button", { type: "button", class: "primary turns-below", text: "New turns below", onclick: () => {
      const now = ui.sessions;
      if (!now) return;
      now.below = false;
      toPageBottom();
      readTranscript(now, true);
    } })));
  }
  return parts;
}

// The Agents tab: the session list (no `sub`) or one session's transcript (`sub` is its id, from
// `#/run/<id>/agents/<sid>`). Parts are keyed, so an open fold and the scroll survive a refresh.
function sessionEntries(detail, sub) {
  const st = sessionsState(detail.run_id);
  if (st.rows === null && !st.error) loadSessions(detail.run_id);
  if (sub) return transcriptEntries(detail, st, sub);
  openTranscript(st, null);
  if (st.rows === null) return [sessionNote("sessions:note", st.error ? "Could not load the sessions: " + st.error : "Loading the sessions…")];
  const rows = st.rows;
  const live = rows.filter((r) => r.live).length;
  const summary = "Agents · " + rows.length + (rows.length === 1 ? " session" : " sessions") + " · " + live + " live · " + money(sessionsTotal(rows));
  const parts = [entry("sessions:head", { summary: summary, filter: Object.assign({}, st.filter), runId: detail.run_id,
    phases: uniqueOf(rows, "phase"), roles: uniqueOf(rows, "role") }, sessionFilterNode)];
  if (!rows.length) return parts.concat([sessionNote("sessions:note", "No agent session has started yet.")]);
  const groups = groupSessions(filterSessions(rows, st.filter));
  if (!groups.length) parts.push(sessionNote("sessions:note", "No session matches this filter."));
  for (const g of groups) {
    const name = g.phase === "live" ? "Live" : g.phase === null ? "(no phase)" : String(g.phase);
    parts.push(entry("sessions:group:" + String(g.phase), { name: name, count: g.rows.length }, (h) => el("h3", { class: "sessions-group", text: h.name + " (" + h.count + ")" })));
    for (const r of g.rows) parts.push(entry("session:" + r.id, { row: r, runId: detail.run_id, link: true, attach: r.live ? detail.attach : null }, sessionRowNode));
  }
  return parts;
}
