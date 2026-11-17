"use strict";

// ------------------------------------------------------------------ the Plan tab's Tasks mode

// The plan's tasks as a board: each with its status, tries, review and cost, and what a stopped one holds up. Fed by
// `/api/runs/<id>/plan`. Everything from the server reaches the page as text. The pure helpers sit first and touch no
// DOM, so a test runs them under node.

const PLAN_TAB = "plan";  // the Plan tab's id in RUN_TABS
const STATUS_LABEL = { committed: "✓ committed", blocked: "⛔ blocked", parked: "◆ parked", running: "● running", failed: "✗ failed",
  waiting: "○ waiting on", independent: "◇ independent", pending: "○ pending", deferred: "deferred", done_before: "✓ done before" };
const STATUS_ORDER = ["blocked", "parked", "failed", "running", "waiting", "independent", "pending", "deferred", "committed", "done_before"];
const PLAN_STATUS_CLASS = { committed: "st-ok", done_before: "st-ok", blocked: "st-bad", failed: "st-bad", parked: "st-wait", running: "st-run" };
const SUMMARY_ORDER = [["committed", "committed"], ["running", "running"], ["blocked", "blocked"], ["parked", "parked"], ["failed", "failed"],
  ["waiting", "waiting on it"], ["independent", "independent"], ["deferred", "deferred"]];
const PLAN_FILTERS = [["all", "All"], ["stopped", "Stopped"], ["findings", "With findings"]];
const PLAN_GROUPS = [["status", "Status"], ["wave", "Wave"]];

// `[10,11,12,20,22,24]` -> "10–12, 20, 22, 24": a run of three or more consecutive numbers is a range.
function ranges(ns) {
  const sorted = Array.from(new Set(ns || [])).sort((a, b) => a - b);
  const out = [];
  for (let i = 0; i < sorted.length;) {
    let j = i;
    while (j + 1 < sorted.length && sorted[j + 1] === sorted[j] + 1) j += 1;
    if (j - i >= 2) out.push(sorted[i] + "–" + sorted[j]);
    else for (let k = i; k <= j; k += 1) out.push(String(sorted[k]));
    i = j + 1;
  }
  return out.join(", ");
}

function taskSummary(plan) {
  const s = (plan && plan.summary) || {};
  const parts = [(s.total || 0) + " tasks"];
  for (const [key, label] of SUMMARY_ORDER) if (s[key]) parts.push(s[key] + " " + label);
  return parts.join(" · ");
}

function filterTasks(tasks, filter) {
  if (filter === "stopped") return tasks.filter((t) => t.status === "blocked" || t.status === "parked");
  if (filter === "findings") return tasks.filter((t) => t.review && (t.review.findings || []).length > 0);
  return tasks;
}

function groupTasks(tasks, by) {
  const groups = [];
  if (by === "wave") {
    const waves = Array.from(new Set(tasks.filter((t) => t.wave !== null && t.wave !== undefined).map((t) => t.wave))).sort((a, b) => a - b);
    for (const w of waves) groups.push({ key: "wave-" + w, label: "Wave " + (w + 1), tasks: tasks.filter((t) => t.wave === w) });
    const cycle = tasks.filter((t) => t.wave === null || t.wave === undefined);
    if (cycle.length) groups.push({ key: "cycle", label: "Cycle", tasks: cycle });
    return groups;
  }
  const known = tasks.map((t) => t.status);
  const order = STATUS_ORDER.concat(Array.from(new Set(known)).filter((s) => !STATUS_ORDER.includes(s)));
  for (const status of order) {
    const mine = tasks.filter((t) => t.status === status);
    if (mine.length) groups.push({ key: status, label: STATUS_LABEL[status] || status, tasks: mine });
  }
  return groups;
}

// The mode the Plan tab shows: `tasks`, `document`, or (nothing named) by whether the plan has started.
function planMode(sub, started) {
  if (sub === "tasks" || /^task-\d+$/.test(sub || "")) return "tasks";
  if (sub) return "document";
  return started ? "tasks" : "document";
}

const statusText = (t) => (STATUS_LABEL[t.status] || t.status) + (t.status === "waiting" && (t.blocked_by || []).length ? " " + ranges(t.blocked_by) : "");
const tries = (t) => (t.attempts || 0) + ((t.attempts || 0) === 1 ? " try" : " tries") + (t.resolvers ? " · " + t.resolvers + " resolvers" : "");
function reviewText(review) {
  if (!review) return "—";
  if (review.skipped) return "skipped";
  return review.blocking > 0 ? review.blocking + " blocking" : "✓ 0";
}

// ---- state: the filter and the grouping stay across refreshes and runs; the fetched plan is per run

function planState(runId) {
  if (!ui.plan) ui.plan = { filter: "all", group: "status" };
  const st = ui.plan;
  if (st.runId !== runId) { st.runId = runId; st.data = null; st.error = null; st.inflight = false; st.scrolled = null; }
  return st;
}

// The plan, fetched. A failure keeps the plan already drawn (and says so); a first one is told in the tab.
async function loadPlan(runId) {
  const st = ui.plan;
  if (!st || st.runId !== runId || st.inflight) return;
  st.inflight = true;
  try {
    const data = await api(runPath(runId) + "/plan");
    if (ui.plan !== st || st.runId !== runId) return;
    st.data = data;
    st.error = null;
  } catch (err) {
    if (ui.plan !== st || st.runId !== runId) return;
    if (st.data) toast("Could not refresh the tasks: " + err.message);
    else st.error = err.message;
  } finally {
    st.inflight = false;
  }
  if (ui.plan === st) {
    redrawArtifact(runId);
    scrollToTask(runId);
  }
}

// `#/run/<id>/plan/task-<n>` opens and shows its row, once the plan is drawn.
function scrollToTask(runId) {
  const st = ui.plan;
  const run = ui.run;
  if (!st || !run || run.id !== runId || run.tab !== PLAN_TAB || !/^task-\d+$/.test(run.sub || "") || st.scrolled === run.sub) return;
  if (!$("run-body").querySelector("[data-key]")) return;
  st.scrolled = run.sub;
  scrollToSub($("run-body"), run.sub);
}

// ---- drawing

const planNote = (key, text, runId) => entry(key, { text: text, runId: runId }, (d) => el("div", { class: "plan-part plan-note" },
  el("p", { class: "muted art-note", text: d.text }), documentSwitch(d.runId)));

const documentSwitch = (runId) => chip("Document", () => goRun(runId, PLAN_TAB, "document"), false);

function headerNode(d) {
  const set = (key, value) => () => { ui.plan[key] = value; redrawArtifact(d.runId); };
  const group = (label, key, options) => el("span", { class: "chips", role: "group", "aria-label": label },
    el("span", { class: "muted", text: label + ": " }), options.map(([value, text]) => chip(text, set(key, value), d[key] === value)));
  return el("div", { class: "plan-part plan-head" }, el("strong", { text: d.summary }),
    group("Filter", "filter", PLAN_FILTERS), group("Group", "group", PLAN_GROUPS), documentSwitch(d.runId));
}

function taskBody(t) {
  const parts = [];
  if (t.text) parts.push(el("div", { class: "md plan-text" }, markdownNodes(parseMarkdown(t.text))));
  if ((t.files || []).length) parts.push(el("div", {}, el("div", { class: "muted", text: "Files" }), el("ul", { class: "plan-list" }, t.files.map((f) => el("li", {}, el("code", { text: f }))))));
  if ((t.sessions || []).length) {
    parts.push(el("div", {}, el("div", { class: "muted", text: "Sessions" }), el("ul", { class: "plan-list" }, t.sessions.map((s) => el("li", {},
      el("span", { text: s.label + (s.live ? " (live)" : "") }),
      el("span", { class: "muted", text: " · " + [s.model, s.effort].filter(Boolean).join(" · ") + " · exit " + (s.exit_code === null || s.exit_code === undefined ? "-" : s.exit_code) + " · " + money(s.spend) }))))));
  }
  if ((t.gates || []).length) {
    parts.push(el("div", {}, el("div", { class: "muted", text: "Gates" }), el("ul", { class: "plan-list" }, t.gates.map((g) => el("li", {},
      el("span", { class: g.skipped ? "muted" : g.passed ? "st-ok" : "st-bad", text: (g.skipped ? "skipped" : g.passed ? "passed" : "failed") + " " }), el("span", { text: g.gate }))))));
  }
  const findings = (t.review && t.review.findings) || [];
  if (findings.length) {
    parts.push(el("div", {}, el("div", { class: "muted", text: "Review findings (round " + (t.review.round === null || t.review.round === undefined ? "?" : t.review.round) + ")" }),
      el("ul", { class: "findings" }, findings.map((f) => el("li", { class: "finding" }, dotNode(f.severity),
        f.file ? el("code", { text: f.file + (f.line ? ":" + f.line : "") + " " }) : null, el("span", { text: f.text || "" }))))));
  }
  return parts;
}

function taskNode(d) {
  const t = d.task;
  const cells = [el("span", { class: "plan-n", text: "#" + t.n }), el("span", { class: "plan-title", text: t.title || "" }),
    el("span", { class: "muted", text: (t.depends_on || []).length ? "after " + ranges(t.depends_on) : "" }),
    el("span", { class: PLAN_STATUS_CLASS[t.status] || "muted", text: statusText(t) }),
    el("span", { text: tries(t) }), el("span", { text: reviewText(t.review) }), el("span", { text: money(t.cost) })];
  const stopped = t.status === "blocked" || t.status === "parked";
  const stop = stopped ? el("div", { class: "plan-stop" },
    t.stop && t.stop.summary ? el("span", { text: t.stop.summary + " " }) : null,
    el("span", { class: "muted", text: "blocks: " + (ranges(t.blocks) || "none") + " · independent: " + (d.independent || "none") + " " }),
    el("a", { href: routeHash({ view: "run", run: d.runId, tab: "overview", sub: "decision" }), text: "Open decision" })) : null;
  return el("details", { class: "plan-task", "data-key": "task-" + t.n },
    el("summary", {}, cells), stop, el("div", { class: "plan-body" }, taskBody(t)));
}

// The Tasks mode's parts, keyed: rows patch by `task-<n>`, so an open row and the scroll survive a refresh.
function planEntries(detail, plan, sub) {
  const st = planState(detail.run_id);
  const runId = detail.run_id;
  if (plan.path === null || plan.path === undefined) return [planNote("plan:none", "No plan for this run yet.", runId)];
  const tasks = plan.tasks || [];
  if (!tasks.length) return [planNote("plan:empty", "The runner found no `### Task N:` heading in " + plan.path + ".", runId)];
  const independent = ranges(tasks.filter((t) => t.status === "independent").map((t) => t.n));
  const parts = [entry("plan:head", { summary: taskSummary(plan), filter: st.filter, group: st.group, runId: runId }, headerNode)];
  const shown = filterTasks(tasks, st.filter);
  if (!shown.length) parts.push(planNote("plan:filtered", "No task matches this filter.", runId));
  for (const g of groupTasks(shown, st.group)) {
    parts.push(entry("plan:group:" + g.key, { label: g.label, count: g.tasks.length }, (h) => el("h3", { class: "plan-part plan-group", text: h.label + " (" + h.count + ")" })));
    for (const t of g.tasks) {
      const stopped = t.status === "blocked" || t.status === "parked";
      parts.push(entry("task-" + t.n, { task: t, runId: runId, independent: stopped ? independent : "" }, taskNode));
    }
  }
  return parts;
}

// What the Plan tab draws, or null for the Document mode (U1's viewer). The plan is fetched first, so the mode is
// known: with no `sub`, a started plan shows Tasks.
function planTabEntries(detail, sub) {
  const st = planState(detail.run_id);
  if (sub && planMode(sub, false) === "document") return null;  // a path or `document`: no plan fetch needed
  if (st.data === null) {
    if (st.error) return sub ? [planNote("plan:error", "Could not load the tasks: " + st.error, detail.run_id)] : null;  // no `sub`: the Document
    loadPlan(detail.run_id);
    return [entry("art:loading", "Loading the plan…", (t) => el("p", { class: "muted art-note", text: t }))];
  }
  if (planMode(sub, !!st.data.started) === "document") return null;
  return planEntries(detail, st.data, sub);
}
