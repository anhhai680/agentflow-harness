"use strict";

// ------------------------------------------------------------------ the Run page and its routes

// Every view has a URL: `#/<view>` for a header tab, `#/run/<id>[/<tab>[/<sub>]]` for one run. A reload or Back
// lands where the reader was. `sub` names a phase, `decision` or a file the tab opens and scrolls to.

const RUN_TABS = ["overview", "spec", "plan", "activity", "gates", "files"];
const RUN_TAB_LABEL = { overview: "Overview", spec: "Spec", plan: "Plan", activity: "Activity", gates: "Gates", files: "Files" };
const DECISION_ACTIONS = ["approve", "answer", "reject"];
const RAIL_MARK = { done: "✓", approved: "✓", failed: "✗", waiting: "◆", awaiting_approval: "◆", running: "●", refining: "●", skipped: "–" };

function parseRoute(hash) {
  const board = { view: "board", run: null, tab: null, sub: null };
  if (!/^#\//.test(hash || "")) return board;
  let parts;
  try { parts = hash.slice(2).split("/").map(decodeURIComponent); } catch (err) { return board; }  // a malformed % escape
  if (parts[0] === "run" && parts[1]) {
    const known = RUN_TABS.includes(parts[2]);
    return { view: "run", run: parts[1], tab: known ? parts[2] : "overview", sub: known && parts[3] ? parts[3] : null };
  }
  if (parts.length === 1 && VIEWS.includes(parts[0])) return { view: parts[0], run: null, tab: null, sub: null };
  return board;
}

function routeHash(route) {
  if (route.view !== "run") return "#/" + encodeURIComponent(route.view || "board");
  const parts = [route.run, route.tab || "overview"];
  if (route.sub) parts.push(route.sub);
  return "#/run/" + parts.map(encodeURIComponent).join("/");
}

// Go to a run's page. Already there: only scroll to `sub` again, since the hash does not change.
function goRun(id, tab, sub) {
  const hash = routeHash({ view: "run", run: id, tab: tab || "overview", sub: sub || null });
  if (location.hash === hash) scrollToSub($("run-body"), sub);
  else location.hash = hash;
}

// The inbox item a decision button acts on: the run's wait, or for `answer` a parked step's questions.
function decisionItem(detail, action) {
  const w = detail.waiting;
  if (w && (action !== "answer" || (w.questions || []).length)) {
    return { run_id: detail.run_id, key: w.key, kind: w.kind, phase: w.phase, summary: w.summary, questions: w.questions || [] };
  }
  return ((ui.board && ui.board.needs_human) || []).find((i) => i.run_id === detail.run_id && (i.questions || []).length) || null;
}

function withDecision(detail, action, flow) {
  const item = decisionItem(detail, action);
  if (item) flow(item);
  else toast("Nothing to " + action + " now: the Board's Needs human list has it once it is raised");
}

// The only Pause, Resume, Retry, Mark done and Cancel buttons on the Run page and the peek. Which ones show, and in
// which order, is the server's `detail.actions`: a button the run's state would refuse is never drawn.
const ACTION_BUTTONS = {
  approve: { label: "Approve", cls: "primary", run: (detail) => withDecision(detail, "approve", approveFlow) },
  answer: { label: "Answer…", cls: "primary", run: (detail) => withDecision(detail, "answer", answerDialog) },
  reject: { label: "Reject…", run: (detail) => withDecision(detail, "reject", (item) => rejectDialog(item.run_id, needTarget(item))) },
  resume: { label: "Resume", cls: "primary", run: (detail) => act(detail.run_id, "resume") },
  done: { label: "Mark done", run: (detail) => {
    const card = ((ui.board && ui.board.cards) || []).find((c) => c.run_id === detail.run_id);
    const skips = card && card.ready ? card.ready.skips : [];
    const warning = skips.length ? " These phases will not run: " + skips.join(", ") + ". The PR says so." : "";
    dialog("Mark done", [el("p", { text: "Mark " + detail.label + " done and open a draft PR?" + warning })], () => act(detail.run_id, "done"), "Mark done");
  } },
  pause: { label: "Pause", run: (detail) => act(detail.run_id, "pause") },
  retry: { label: "Retry failed step", run: (detail) => act(detail.run_id, "retry") },
  cancel: { label: "Cancel run…", cls: "danger", run: (detail) => {
    const live = detail.live_agents || [];
    const stops = live.length
      ? "This stops " + live.length + " live agent" + (live.length === 1 ? "" : "s") + ": " + live.map((a) => a.label).join(", ")
      : "No agent is running.";
    dialog("Cancel run", [el("p", { text: "Cancel " + detail.label + "? " + stops })], () => act(detail.run_id, "cancel"), "Cancel run");
  } },
};

// `actions` is a list of action names in the server's order (a filter of `detail.actions`).
function actionNodes(detail, actions) {
  return actions.filter((name) => ACTION_BUTTONS[name]).map((name) => {
    const button = ACTION_BUTTONS[name];
    return el("button", { type: "button", class: button.cls, text: button.label, onclick: () => button.run(detail) });
  });
}

function sinceSeconds(iso) {
  const at = Date.parse(iso || "");
  return isNaN(at) ? null : Math.max(0, (Date.now() - at) / 1000);
}

function recordsText(detail) {
  return Object.entries(detail.steps || {}).map(([key, row]) => key + "  " + row.status + (row.skip_reason ? "  (" + row.skip_reason + ")" : "") + (row.result ? "  — " + row.result : "")).join("\n");
}

// What decisionNode shows, as data: its entry is rebuilt when any of it changes, and kept (with an open Inbox) when not.
function decisionData(detail) {
  const w = detail.waiting;
  return { age: w ? ago(sinceSeconds(w.since)) : "", waiting: w, inbox: detail.inbox,
    actions: detail.actions.filter((a) => DECISION_ACTIONS.includes(a)) };
}

// The decision a run waits on, with its approve, reject and answer buttons: shown on Overview and in the peek.
function decisionNode(detail) {
  const w = detail.waiting;
  const buttons = actionNodes(detail, detail.actions.filter((a) => DECISION_ACTIONS.includes(a)));
  if (!w && !buttons.length) return null;
  const questions = (w && w.questions) || [];
  return el("section", { class: "decision", "data-key": "decision" },
    el("h3", { text: w ? "Waiting on you " + ago(sinceSeconds(w.since)) + " · " + w.key + " (" + w.kind + ")" : "A parked step has questions" }),
    w && w.summary ? el("p", { text: w.summary }) : null,
    questions.length ? el("ul", {}, questions.map((q) => el("li", { text: q.id + ": " + q.question }))) : null,
    detail.inbox ? el("details", { "data-key": "inbox" }, el("summary", { text: "Inbox" }), el("pre", { text: detail.inbox })) : null,
    buttons.length ? el("div", { class: "actions" }, buttons) : null);
}

function stageRailNode(rail, runId) {
  return el("div", { class: "rail" }, (rail || []).map((stage) => el("button", {
    type: "button", class: "rail-chip", "data-key": "phase:" + stage.phase, title: stage.status,
    onclick: () => goRun(runId, "overview", stage.phase),
  },
    el("span", { class: STATUS_CLASS[stage.status] || "muted", text: (RAIL_MARK[stage.status] || "○") + " " }),
    el("strong", { text: stage.phase }),
    typeof stage.wall_s === "number" ? el("span", { class: "muted", text: " " + dur(stage.wall_s) }) : null,
    stage.note ? el("span", { class: "d", text: stage.note }) : null)));
}

function runHeadNodes(detail) {
  const f = detail.facts;
  const spend = money(f.spend_usd) + limits(f.soft_budget_usd, f.budget_usd).replace(" / ", " of ");
  const facts = [spend, detail.metrics ? dur(detail.metrics.wall_s) + " wall" : "", f.branch ? "branch " + f.branch : ""].filter(Boolean).join(" · ");
  return [
    el("div", { class: "run-title" },
      el("a", { href: "#/board", text: "← Board" }),
      el("h2", { text: detail.label }),
      el("span", { class: "badge", text: workLabel(detail.lane) }),
      el("span", { class: "badge " + (STATUS_CLASS[detail.status] || "muted"), text: String(detail.status).replace("_", " ") })),
    el("div", { class: "muted run-facts" }, facts,
      f.pr_link ? [" · ", el("a", { href: f.pr_link, target: "_blank", rel: "noopener noreferrer", text: "PR ↗" })] : null),
  ];
}

function bannerNodes(detail) {
  const w = detail.waiting;
  return [
    el("span", { class: "grow" }, el("strong", { class: "st-wait", text: "Waiting on you " + ago(sinceSeconds(w.since)) }), " — " + (w.summary || w.key)),
    el("button", { type: "button", class: "primary", text: "Open decision", onclick: () => goRun(detail.run_id, "overview", "decision") }),
  ];
}

function attachNode(detail) {
  const attach = el("input", { type: "text", value: detail.attach, readonly: "readonly", "aria-label": "attach command" });
  const copy = el("button", { type: "button", text: "Copy", onclick: () => {
    navigator.clipboard.writeText(detail.attach).then(() => toast("Copied: run it in your terminal"), () => { attach.select(); toast("Press Ctrl-C / ⌘C to copy"); });
  } });
  return el("div", { class: "attach" }, attach, copy);
}

// A timeline, newest first, patched line by line (keyed by `seq`): lines already shown stay as they are.
function patchTimeline(list, events) {
  patchChildren(list, events.slice().reverse(), (e) => "seq:" + e.seq, (e) => el("li", { text: describe(e) }));
}

const timelineShell = () => el("ol", { class: "timeline" });

function gateNode(g) {
  const mark = g.skipped ? "– " : g.passed ? "✓ " : "✗ ";
  return el("details", { open: !g.skipped && !g.passed ? "open" : null },  // a failure starts open
    el("summary", {}, el("span", { class: g.skipped ? "gate-skip" : g.passed ? "gate-ok" : "gate-bad", text: mark }), g.key + " " + g.gate),
    el("pre", { text: g.log_tail || "(no log)" }));
}

// Gates keyed by `<key>/<gate>/<i>`, where `i` counts the earlier runs of that gate at that step.
function gateEntries(gates) {
  const seen = new Map();
  return gates.map((g) => {
    const name = g.key + "/" + g.gate;
    const i = seen.get(name) || 0;
    seen.set(name, i + 1);
    return entry(name + "/" + i, g, gateNode);
  });
}

function phaseNode(phase, open) {
  return el("details", { class: "wf-phase", "data-key": "phase:" + phase.name, open: open ? "open" : null },
    el("summary", {}, phase.name + " ", el("span", { class: STATUS_CLASS[phase.status] || "muted", text: phase.status.replace("_", " ") }),
      phase.reason ? el("span", { class: "muted", text: " · " + phase.reason }) : null),
    el("ul", { class: "wf-run" }, phase.steps.map(runStepNode)));
}

// The tab body is patched by key, so what the reader opened and where they scrolled survive a refresh.
function runTabEntries(detail, tab, sub) {
  const heading = (key, text, cls) => entry(key, { text: text, cls: cls }, (h) => el("h3", { class: h.cls, text: h.text }));
  if (isArtifactTab(tab)) return artifactEntries(detail, tab, sub);
  if (tab === "activity") {
    return [heading("title", "Timeline (" + detail.timeline.length + " events)"), entry("timeline", null, timelineShell)];
  }
  if (tab === "gates") {
    const reviews = Object.keys(detail.reviews).length ? JSON.stringify(detail.reviews, null, 2) : "";
    return [heading("title", "Gates"),
      ...(detail.gates.length ? gateEntries(detail.gates) : [entry("no-gates", null, () => el("p", { class: "muted", text: "No gate has run yet." }))]),
      entry("reviews", reviews, (text) => section("Review findings", text, false, "reviews"))];
  }
  if (tab === "files") {
    return fileEntries(detail, sub);
  }
  const current = sub ? null : detail.facts.phase;  // with nothing named, the phase the run is in starts open
  const asked = decisionData(detail);
  const decision = asked.waiting || asked.actions.length ? [entry("decision", asked, () => decisionNode(detail))] : [];
  return [...decision,
    heading("workflow", "Workflow", "wf-title"),
    ...detail.workflow.map((phase) => entry("phase:" + phase.name, phase, (p) => phaseNode(p, p.name === current))),
    entry("metrics", detail.metrics, metricsSection),
    heading("recent-title", "Recent activity", "wf-title"), entry("recent", null, timelineShell)];
}

function renderRunTab(detail, tab, sub) {
  const body = $("run-body");
  body.classList.toggle("art-tab", isArtifactTab(tab));
  patchChildren(body, runTabEntries(detail, tab, sub), entryKey, entryNode);
  if (isArtifactTab(tab)) return;
  if (tab === "activity") patchTimeline(childByKey(body, "timeline"), detail.timeline);
  else if (tab !== "gates" && tab !== "files") patchTimeline(childByKey(body, "recent"), detail.timeline.slice(-10));
}

function renderRun(detail) {
  const run = ui.run;
  $("run-head").replaceChildren(...runHeadNodes(detail));
  $("run-banner").hidden = !detail.waiting;
  $("run-banner").replaceChildren(...(detail.waiting ? bannerNodes(detail) : []));
  $("run-rail").replaceChildren(stageRailNode(detail.stage_rail, detail.run_id));
  $("run-actions").replaceChildren(...actionNodes(detail, detail.actions.filter((a) => !DECISION_ACTIONS.includes(a))), attachNode(detail));
  $("run-tabs").replaceChildren(...visibleTabs(detail).map((tab) => el("a", {
    href: routeHash({ view: "run", run: detail.run_id, tab: tab }), text: RUN_TAB_LABEL[tab], "aria-current": tab === run.tab ? "page" : null,
  })));
  renderRunTab(detail, run.tab, run.sub);
}

function renderRunNote(text) {
  $("run-head").replaceChildren(el("div", { class: "run-title" }, el("a", { href: "#/board", text: "← Board" }), el("h2", { text: text })));
  $("run-banner").hidden = true;
  for (const part of ["run-banner", "run-rail", "run-actions", "run-tabs", "run-body"]) $(part).replaceChildren();
}

// The node in `container` that `sub` names (by its data-key, never by a selector built from it): opened and shown.
function scrollToSub(container, sub) {
  if (!sub) return;
  const node = Array.from(container.querySelectorAll("[data-key]")).find((n) => n.dataset.key === sub || n.dataset.key.endsWith(":" + sub));
  if (!node) return;
  if (node.tagName === "DETAILS") node.open = true;
  node.scrollIntoView({ block: "start" });
}

let runRequest = 0;

// Go to a run's route. On the same run with its detail in hand (another tab or `sub`), draw from that at once and
// refetch behind it; on another run, say it is loading. A new route scrolls to `sub` once it is drawn.
function openRun(id, tab, sub) {
  const prev = ui.run;
  const same = !!prev && prev.id === id;
  ui.run = { id: id, tab: tab, sub: sub, detail: same ? prev.detail : undefined, unshown: true };
  show("run");
  if (same && prev.detail) showRun(prev.detail);
  else if (!same) renderRunNote("Loading " + id + "…");
  loadRun(id);
  if (reopensArtifact(prev, id, tab, ui.artifact)) loadArtifacts(id);  // revisions and findings that came meanwhile
  if (same && prev.tab !== tab) {  // the tasks and the files change while a stopped run sends no event: fetch them again on return
    if (tab === PLAN_TAB && ui.plan && ui.plan.runId === id && planMode(sub, !!(ui.plan.data && ui.plan.data.started)) === "tasks") loadPlan(id);
    if (tab === FILES_TAB && ui.files && ui.files.runId === id) { ui.files.error = null; loadFiles(id); }  // a first-load error gets its retry here
  }
}

function showRun(detail) {
  renderRun(detail);
  if (!ui.run.unshown) return;
  ui.run.unshown = false;
  scrollToSub($("run-body"), ui.run.sub);
}

// Fetch the open run and patch its page. A failure keeps a page already drawn (and what is open on it) and says so
// in a toast; only a first load that fails shows the error in its place.
async function loadRun(id) {
  const request = ++runRequest;
  let detail;
  try {
    detail = await api(runPath(id));
  } catch (err) {
    if (request !== runRequest || !ui.run || ui.run.id !== id) return;
    if (err.status === 404) {
      ui.run.detail = null;  // gone for good: a refresh never asks again
      renderRunNote("No run " + id);
    } else if (ui.run.detail) {
      toast("Could not refresh " + id + ": " + err.message);
    } else {
      renderRunNote("Could not load " + id + ": " + err.message);
    }
    return;
  }
  if (request !== runRequest || !ui.run || ui.run.id !== id) return;
  ui.run.detail = detail;
  showRun(detail);
}
