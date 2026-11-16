"use strict";

// ------------------------------------------------------------------ time and cost

// Every figure comes from the runs' event logs, so a finished or archived run keeps its numbers. A run's time is split
// into buckets: what it was doing (an agent call, gates, waiting on a person, CI, a usage-limit hold, no runner going).

const BUCKET_LABEL = { agent: "agent", gates: "gates", human: "waiting on a human", ci: "CI", hold: "usage-limit hold", down: "runner down", runner: "runner" };
const shortModel = (model) => String(model || "?").replace(/^claude-/, "");
const modelLabel = (m) => shortModel(m.model) + "·" + (m.effort || "?");

function dur(seconds) {
  if (typeof seconds !== "number" || !isFinite(seconds) || seconds <= 0) return "0m";
  if (seconds < 60) return Math.round(seconds) + "s";
  const minutes = Math.round(seconds / 60);
  return minutes < 60 ? minutes + "m" : Math.floor(minutes / 60) + "h " + String(minutes % 60).padStart(2, "0") + "m";
}

const pct = (part, whole) => (whole ? Math.round((part / whole) * 100) : 0);
const cost = (value, estimated) => (estimated ? "~" : "") + money(value);

function bucketOrder() {
  return (metricsUi.data && metricsUi.data.buckets) || Object.keys(BUCKET_LABEL);
}

// A horizontal stacked bar: one segment per bucket, the whole bar `scale` (0..1) of the widest bar it is compared with.
function stackNode(buckets, scale) {
  const total = Object.values(buckets || {}).reduce((a, b) => a + b, 0);
  const bar = el("div", { class: "stack", role: "img", "aria-label": bucketOrder().filter((b) => buckets[b]).map((b) => BUCKET_LABEL[b] + " " + dur(buckets[b])).join(", ") });
  bar.style.width = Math.max(2, Math.round((scale === undefined ? 1 : scale) * 100)) + "%";
  for (const name of bucketOrder()) {
    const value = (buckets || {})[name] || 0;
    if (!value || !total) continue;
    const seg = el("span", { class: "b-" + name, title: BUCKET_LABEL[name] + ": " + dur(value) + " (" + pct(value, total) + "%)" });
    seg.style.flexGrow = String(value);
    bar.append(seg);
  }
  return bar;
}

function legendNode(used) {
  return el("div", { class: "legend" }, bucketOrder().filter((b) => !used || used[b]).map((b) =>
    el("span", {}, el("span", { class: "swatch b-" + b }), BUCKET_LABEL[b])));
}

function modelChips(models) {
  return (models || []).map((m) => el("span", { class: "chip", title: (m.agent || "?") + " · " + m.sessions + " session(s) · " + dur(m.agent_s) + " · " + money(m.cost_usd) },
    modelLabel(m) + " ×" + m.sessions));
}

function table(head, rows, numeric) {
  return el("div", { class: "scroll" }, el("table", {},
    el("thead", {}, el("tr", {}, head.map((h, i) => el("th", { class: numeric && numeric.includes(i) ? "num" : null, text: h })))),
    el("tbody", {}, rows)));
}

const metricsUi = { data: null, request: 0, range: "month", from: "", to: "", work: "", shown: "" };

function isoDay(day) {
  return day.getFullYear() + "-" + String(day.getMonth() + 1).padStart(2, "0") + "-" + String(day.getDate()).padStart(2, "0");
}

function metricsQuery() {
  const today = new Date();
  const back = (days) => new Date(today.getFullYear(), today.getMonth(), today.getDate() - days);
  const params = new URLSearchParams();
  const range = metricsUi.range;
  if (range === "all") params.set("all", "1");
  else if (range === "last_month") {
    params.set("from", isoDay(new Date(today.getFullYear(), today.getMonth() - 1, 1)));
    params.set("to", isoDay(new Date(today.getFullYear(), today.getMonth(), 0)));
  } else if (range === "d7") params.set("from", isoDay(back(6)));
  else if (range === "d30") params.set("from", isoDay(back(29)));
  else if (range === "custom") {
    if (metricsUi.from) params.set("from", metricsUi.from);
    if (metricsUi.to) params.set("to", metricsUi.to);
    if (!metricsUi.from && !metricsUi.to) params.set("all", "1");
  } else params.set("from", isoDay(new Date(today.getFullYear(), today.getMonth(), 1)));
  if (metricsUi.work) params.set("lane", metricsUi.work);
  return params.toString();
}

function saveMetricsChoice() {
  try { localStorage.setItem("agentflow-metrics", JSON.stringify({ range: metricsUi.range, from: metricsUi.from, to: metricsUi.to, work: metricsUi.work })); } catch (err) { console.warn("agentflow: local storage unavailable", err); }
}

function restoreMetricsChoice() {
  try {
    const saved = JSON.parse(localStorage.getItem("agentflow-metrics") || "{}");
    for (const key of ["range", "from", "to", "work"]) if (typeof saved[key] === "string") metricsUi[key] = saved[key];
  } catch (err) { console.warn("agentflow: local storage unavailable", err); }
  $("met-range").value = metricsUi.range;
  $("met-from").value = metricsUi.from;
  $("met-to").value = metricsUi.to;
  $("met-custom").hidden = metricsUi.range !== "custom";
}

async function loadMetrics() {
  if (!TOKEN) return;
  loadScorecard();
  const request = ++metricsUi.request;  // the latest request wins: a filter change is never lost behind a refresh
  try {
    const data = await api("/api/metrics?" + metricsQuery());
    if (request !== metricsUi.request) return;
    const shown = JSON.stringify({ ...data, generated_at: null });
    metricsUi.data = data;
    if (shown !== metricsUi.shown) {  // a live refresh with nothing new keeps the tables (and a click on them) as they are
      metricsUi.shown = shown;
      renderMetrics();
    } else {
      $("met-status").textContent = $("met-status").textContent.replace(/updated \S+$/, "updated " + data.generated_at.slice(11, 19));
    }
  } catch (err) {
    if (request === metricsUi.request) $("met-status").textContent = "Could not load: " + err.message;
  }
}

function kpi(value, label, title) {
  return el("div", { class: "kpi", title: title }, el("div", { class: "v", text: value }), el("div", { class: "k", text: label }));
}

function laneSelect(data) {
  const select = $("met-lane");
  const options = [el("option", { value: "", text: "All work" }), ...data.lanes.map((lane) => el("option", { value: lane, text: workLabel(lane) }))];
  select.replaceChildren(...options);
  select.value = metricsUi.work;
}

function phaseRows(phases, order) {
  const names = order.filter((name) => phases[name] && (phases[name].wall_s.total >= 1 || phases[name].cost_usd.total > 0));
  const widest = Math.max(1, ...names.map((name) => phases[name].wall_s.total));
  return names.map((name) => {
    const p = phases[name];
    return el("tr", {},
      el("td", { text: name }), el("td", { class: "num", text: p.runs }),
      el("td", { class: "stack-cell" }, stackNode(p.buckets, p.wall_s.total / widest)),
      el("td", { class: "num", text: dur(p.wall_s.total) }), el("td", { class: "num", text: dur(p.wall_s.median) }), el("td", { class: "num", text: dur(p.wall_s.p90) }),
      el("td", { class: "num", text: money(p.cost_usd.total) }), el("td", { class: "num", text: money(p.cost_usd.median) }), el("td", { class: "num", text: money(p.cost_usd.p90) }),
      el("td", { class: "num", text: money(p.rework_cost_usd) }), el("td", { class: "num", text: p.attempts }),
      el("td", { class: "muted", text: bucketOrder().filter((b) => p.buckets[b]).map((b) => BUCKET_LABEL[b] + " " + pct(p.buckets[b], p.wall_s.total) + "%").join(" · ") }));
  });
}

function runPhaseChips(metrics) {
  return metrics.phases.filter((p) => p.wall_s >= 1 || p.cost_usd > 0).map((p) => el("span", {
    class: "chip", title: p.models.map((m) => modelLabel(m) + " ×" + m.sessions + " " + money(m.cost_usd)).join(", ") || "no agent call",
  }, p.phase + " " + dur(p.wall_s) + " · " + money(p.cost_usd) + (p.models.length ? " · " + p.models.map(modelLabel).join(", ") : "")));
}

function stepRows(steps) {
  return steps.map((s) => el("tr", { class: "row-link", title: "open " + s.run_id, onclick: () => goRun(s.run_id) },
    el("td", { text: s.module }), el("td", { text: s.key }), el("td", { class: "num", text: dur(s.wall_s) }), el("td", { class: "num", text: money(s.cost_usd) }),
    el("td", { class: "num", text: s.attempts + (s.rework_attempts ? " (" + s.rework_attempts + " rework)" : "") }),
    el("td", { class: "num", text: s.review_rounds || "-" }), el("td", { class: "num", text: s.fixes || "-" }),
    el("td", {}, s.models.map((m) => el("span", { class: "chip", text: shortModel(m) })))));
}

function renderMetrics() {
  const data = metricsUi.data;
  if (!data) return;
  laneSelect(data);
  const t = data.totals;
  const span = data.all_time ? "all time" : (data.from || "…") + " to " + (data.to || "today");
  $("met-status").textContent = t.runs + " run" + (t.runs === 1 ? "" : "s") + " started " + span + " · updated " + data.generated_at.slice(11, 19);
  const body = [];
  const wall = Object.values(t.buckets).reduce((a, b) => a + b, 0);
  body.push(el("div", { class: "kpis" },
    kpi(String(t.runs), "runs"),
    kpi(money(t.cost_usd), "total cost"),
    kpi(dur(t.wall_s.median), "median run time", "p90 " + dur(t.wall_s.p90)),
    kpi(dur(t.wall_s.p90), "p90 run time"),
    kpi(t.rework_pct + "%", "of cost spent on rework", money(t.rework_cost_usd) + " on fix, re-review and refine rounds"),
    kpi(pct(t.buckets.human || 0, wall) + "%", "of time waiting on a human", dur(t.buckets.human)),
    kpi(pct(t.buckets.down || 0, wall) + "%", "of time with no runner going", dur(t.buckets.down))));
  if (!t.runs) {
    body.push(el("p", { class: "muted", text: "No run started in this range." }));
    $("met-body").replaceChildren(...body);
    return;
  }
  body.push(el("h2", { class: "lane", text: "Where the time goes, by phase" }), legendNode(t.buckets));
  for (const [lane, row] of Object.entries(t.lanes)) {
    body.push(el("h3", { class: "wf-title", text: workLabel(lane) + " · " + row.runs + " run" + (row.runs === 1 ? "" : "s") + " · median " + dur(row.wall_s.median) + " / " + money(row.cost_usd.median) + " · p90 " + dur(row.wall_s.p90) + " / " + money(row.cost_usd.p90) }));
    body.push(table(["Phase", "Runs", "Time by bucket", "Time", "Median", "p90", "Cost", "Median", "p90", "Rework", "Calls", "Share"],
      phaseRows(row.phases, data.phases), [1, 3, 4, 5, 6, 7, 8, 9, 10]));
  }
  body.push(el("h2", { class: "lane", text: "Model and effort by phase" }));
  const phaseCost = {};
  for (const m of t.models) phaseCost[m.phase] = (phaseCost[m.phase] || 0) + m.cost_usd;
  const models = t.models.slice().sort((a, b) => data.phases.indexOf(a.phase) - data.phases.indexOf(b.phase) || b.cost_usd - a.cost_usd);
  body.push(table(["Phase", "Model · effort", "Sessions", "Agent time", "Cost", "Share of phase cost"], models.map((m) => el("tr", {},
    el("td", { text: m.phase }), el("td", { text: shortModel(m.model) }), el("td", { class: "num", text: m.sessions }),
    el("td", { class: "num", text: dur(m.agent_s) }), el("td", { class: "num", text: money(m.cost_usd) }),
    el("td", { class: "num", text: pct(m.cost_usd, phaseCost[m.phase]) + "%" }))), [2, 3, 4, 5]));
  body.push(el("h2", { class: "lane", text: "Runs" }));
  const widest = Math.max(1, ...data.runs.map((r) => r.metrics.wall_s));
  body.push(table(["Started", "Run", "Work", "Status", "Time", "Time by bucket", "Cost", "Phases (time · cost · model·effort)"], data.runs.map((r) => {
    const m = r.metrics;
    const flags = [m.live ? "live" : "", m.stalled ? "stalled" : "", m.approx ? "gate time estimated" : ""].filter(Boolean).join(", ");
    return el("tr", { class: "row-link", title: "open " + r.run_id + (flags ? " (" + flags + ")" : ""), onclick: () => goRun(r.run_id) },
      el("td", { text: r.started_on || "-" }), el("td", { text: r.module }), el("td", { text: workLabel(r.lane) }),
      el("td", { text: r.status + (r.archived ? " (archived)" : "") + (m.stalled ? " · stalled" : "") }),
      el("td", { class: "num", text: dur(m.wall_s) }), el("td", { class: "stack-cell" }, stackNode(m.buckets, m.wall_s / widest)),
      el("td", { class: "num", text: cost(m.cost_usd, m.cost_estimated) + limits(r.soft_budget, r.budget) }),
      el("td", { class: "wrap" }, runPhaseChips(m)));
  }), [4, 6]));
  const stepHead = ["Run", "Step", "Time", "Cost", "Calls", "Review rounds", "Fixes", "Model · effort"];
  body.push(el("h2", { class: "lane", text: "Costliest steps" }), table(stepHead, stepRows(t.costliest_steps), [2, 3, 4, 5, 6]));
  body.push(el("h2", { class: "lane", text: "Slowest steps" }), table(stepHead, stepRows(t.slowest_steps), [2, 3, 4, 5, 6]));
  $("met-body").replaceChildren(...body);
}

// The scorecard: a lane's signals against the frozen baseline. All text goes in through el()'s textContent.
const scNum = (v) => (v === null || v === undefined ? "-" : String(Math.round(v * 1000) / 1000));
const scCell = (v, n) => scNum(v) + (v === null || v === undefined ? "" : " (" + n + ")");

function renderScorecard(view) {
  const body = [];
  if (!view.baseline) {
    body.push(el("p", { class: "muted", text: "No baseline is frozen yet. Run `agentflow scorecard --freeze` to freeze one." }));
  }
  for (const [lane, rows] of Object.entries(view.lanes || {})) {
    body.push(el("h3", { class: "wf-title", text: workLabel(lane) }));
    if (!rows.length) { body.push(el("p", { class: "muted", text: "No signals." })); continue; }
    body.push(table(["Signal", "Baseline", "Current", "Delta", "Verdict"], rows.map((r) => el("tr", { class: r.slo ? "sc-slo" : null },
      el("td", { text: r.signal + (r.slo ? " (SLO)" : "") }), el("td", { class: "num", text: scCell(r.baseline, r.baseline_n) }),
      el("td", { class: "num", text: scCell(r.current, r.current_n) }), el("td", { class: "num", text: scNum(r.delta) }),
      el("td", { class: r.verdict === "worse" ? "st-bad" : r.verdict === "better" ? "st-ok" : "muted", text: r.verdict + (r.slo ? " - SLO BREACH" : "") }))), [1, 2, 3]));
  }
  $("sc-body").replaceChildren(...body);
}

// The scorecard compares with the runs after the baseline, as `agentflow scorecard` does, unless the range is custom:
// the Metrics tab's presets (this month, ...) would include the baseline's own runs.
function scorecardQuery() {
  const params = new URLSearchParams();
  const custom = metricsUi.range === "custom" && (metricsUi.from || metricsUi.to);
  if (custom && metricsUi.from) params.set("from", metricsUi.from);
  if (custom && metricsUi.to) params.set("to", metricsUi.to);
  if (metricsUi.work) params.set("lane", metricsUi.work);
  $("sc-range").textContent = custom
    ? "Runs that ended " + (metricsUi.from || "at any time") + " to " + (metricsUi.to || "today")
    : "Runs after the baseline";
  return params.toString();
}

async function loadScorecard() {
  if (!TOKEN) return;
  const params = new URLSearchParams(scorecardQuery());
  try {
    renderScorecard(await api("/api/scorecard?" + params.toString()));
  } catch (err) {
    $("sc-body").replaceChildren(el("p", { class: "muted", text: "Could not load the scorecard: " + err.message }));
  }
}

$("met-range").addEventListener("change", () => {
  metricsUi.range = $("met-range").value;
  $("met-custom").hidden = metricsUi.range !== "custom";
  saveMetricsChoice();
  loadMetrics();
});
for (const id of ["met-from", "met-to"]) {
  $(id).addEventListener("change", () => {
    metricsUi.from = $("met-from").value;
    metricsUi.to = $("met-to").value;
    saveMetricsChoice();
    loadMetrics();
  });
}
$("met-lane").addEventListener("change", () => { metricsUi.work = $("met-lane").value; saveMetricsChoice(); loadMetrics(); });

// The Run page's view of one run: a row per phase, and its steps with every agent call (model, effort, role, cost).
function metricsSection(m) {
  if (!m) return el("p", { class: "muted", text: "No time and cost yet." });
  const flags = [m.live ? "still running: figures up to now" : "", m.stalled ? "stalled: no event for longer than a step may run" : "",
    m.approx ? "gate time estimated for runs older than gate timing" : "", m.cost_estimated ? "~ cost partly estimated" : ""].filter(Boolean);
  const phases = m.phases.filter((p) => p.wall_s >= 1 || p.cost_usd > 0);
  const widest = Math.max(1, ...phases.map((p) => p.wall_s));
  const order = bucketOrder().filter((b) => m.buckets[b]);  // only the buckets this run used: keep the table narrow
  const phaseTable = table(["Phase", "Time", "By bucket", ...order.map((b) => BUCKET_LABEL[b]), "Cost", "Rework", "Calls", "Model · effort"],
    phases.map((p) => el("tr", {},
      el("td", { text: p.phase }), el("td", { class: "num", text: dur(p.wall_s) }),
      el("td", { class: "stack-cell" }, stackNode(p.buckets, p.wall_s / widest)),
      ...order.map((b) => el("td", { class: "num", text: p.buckets[b] ? dur(p.buckets[b]) : "-" })),
      el("td", { class: "num", text: money(p.cost_usd) }), el("td", { class: "num", text: p.rework_cost_usd ? money(p.rework_cost_usd) : "-" }),
      el("td", { class: "num", text: p.attempts + (p.rework_attempts ? " (" + p.rework_attempts + " rework)" : "") }),
      el("td", { class: "wrap" }, modelChips(p.models)))), [1, ...order.map((_, i) => i + 3), order.length + 3, order.length + 4, order.length + 5]);
  const stepBody = [];
  for (const p of phases) {
    for (const s of p.steps) {
      if (!s.wall_s && !s.cost_usd && !s.sessions.length) continue;
      const changes = s.effort_changes.map((c) => (c.from || "?") + " → " + (c.to || "?") + (c.reason ? ": " + c.reason : "")).join("; ");
      stepBody.push(el("tr", { title: changes ? "effort changed " + changes : null },
        el("td", { text: s.key + (s.approx ? " (gate time estimated)" : "") }), el("td", { class: "num", text: dur(s.wall_s) }),
        el("td", { class: "stack-cell" }, stackNode(s.buckets, 1)), el("td", { class: "num", text: money(s.cost_usd) }),
        el("td", { class: "num", text: s.attempts + (s.rework_attempts ? " (" + s.rework_attempts + " rework)" : "") }),
        el("td", { text: [s.review_rounds ? "review rounds " + s.review_rounds : "", s.fixes ? "fixes " + s.fixes : "", s.retries ? "retries " + s.retries : "", changes ? "effort " + changes : ""].filter(Boolean).join(" · ") || "-" })));
      s.sessions.forEach((c, i) => stepBody.push(el("tr", { class: "sub" },
        el("td", { text: "  └ call " + (i + 1) + ": " + (c.agent || "?") + " " + shortModel(c.model) + " · " + (c.effort || "?") + (c.role ? " · " + c.role : "") + (c.sizing ? " · sizing" : "") + (c.rework ? " · rework" : "") + (c.live ? " · live" : "") }),
        el("td", { class: "num", text: dur(c.agent_s) }), el("td", { text: "" }), el("td", { class: "num", text: cost(c.cost_usd, c.cost_estimated) }),
        el("td", { text: "" }), el("td", { class: "muted", text: (c.started_at || "").slice(11, 19) + (c.exit_code !== undefined && c.exit_code !== null ? " · exit " + c.exit_code : "") }))));
    }
  }
  return el("details", { open: "open" },
    el("summary", { text: "Time & cost · " + dur(m.wall_s) + " · " + cost(m.cost_usd, m.cost_estimated) + (m.rework_cost_usd ? " (" + money(m.rework_cost_usd) + " rework)" : "") }),
    flags.length ? el("p", { class: "muted", text: flags.join(" · ") }) : "",
    legendNode(m.buckets), phaseTable,
    el("details", { "data-key": "metrics:steps" }, el("summary", { text: "Steps and agent calls" }),
      table(["Step / call", "Time", "By bucket", "Cost", "Calls", "Notes"], stepBody, [1, 3, 4])));
}
