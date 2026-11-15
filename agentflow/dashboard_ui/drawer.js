"use strict";

// ------------------------------------------------------------------ run detail drawer

function section(title, text, open) {
  return el("details", { open: open }, el("summary", { text: title }), el("pre", { text: text || "(empty)" }));
}

function describe(event) {
  const bits = [event.ts ? event.ts.slice(11, 19) : "", event.type];
  for (const key of ["phase", "key", "kind", "status", "gate", "reason", "summary", "note"]) {
    if (event[key] !== undefined && event[key] !== null && event[key] !== "") bits.push(key + "=" + (typeof event[key] === "string" ? event[key] : JSON.stringify(event[key])));
  }
  if (event.by && event.by !== "runner") bits.push("by " + event.by);
  return bits.join("  ");
}

async function openDrawer(runId) {
  ui.drawer = runId;
  const drawer = $("drawer");
  drawer.hidden = false;
  let detail;
  try {
    detail = await api(runPath(runId));
  } catch (err) {
    drawer.replaceChildren(el("p", { text: "Could not load " + runId + ": " + err.message }), el("button", { type: "button", text: "Close", onclick: closeDrawer }));
    return;
  }
  if (ui.drawer !== runId) return;
  const facts = detail.facts;
  const waiting = detail.waiting;
  const attach = el("input", { type: "text", value: detail.attach, readonly: "readonly", "aria-label": "attach command" });
  const copy = el("button", { type: "button", text: "Copy", onclick: () => {
    navigator.clipboard.writeText(detail.attach).then(() => toast("Copied: run it in your terminal"), () => { attach.select(); toast("Press Ctrl-C / ⌘C to copy"); });
  } });
  const actions = el("div", { class: "actions" },
    el("button", { type: "button", text: "Pause", onclick: () => act(runId, "pause") }),
    el("button", { type: "button", text: "Resume", onclick: () => act(runId, "resume") }),
    el("button", { type: "button", text: "Retry failed step", onclick: () => act(runId, "retry") }),
    el("button", { type: "button", class: "danger", text: "Cancel run", onclick: () => { if (confirm("Cancel " + runId + "? Live agents are stopped.")) act(runId, "cancel"); } }),
    el("button", { type: "button", text: "Close", onclick: closeDrawer }));
  const records = Object.entries(detail.steps || {}).map(([key, row]) => key + "  " + row.status + (row.skip_reason ? "  (" + row.skip_reason + ")" : "") + (row.result ? "  — " + row.result : ""));
  const gates = detail.gates.map((g) => (g.skipped ? "– " : g.passed ? "✓ " : "✗ ") + g.key + " " + g.gate + (g.log_tail ? "\n" + g.log_tail : "")).join("\n\n");
  const rows = [["status", detail.status], ["work", workLabel(detail.lane)], ["phase", facts.phase], ["step", facts.step],
    ["branch", facts.branch], ["worktree", facts.worktree], ["pull request", facts.pr_url], ["spend", money(facts.spend_usd) + limits(facts.soft_budget_usd, facts.budget_usd)],
    ["refine", facts.refine ? "round " + facts.refine.round + ": " + facts.refine.note : null], ["failure", facts.failure]]
    .filter(([, v]) => v).map(([k, v]) => el("div", {}, el("span", { class: "muted", text: k + ": " }), String(v)));
  drawer.replaceChildren(
    el("h2", { text: runId }), ...rows,
    el("div", { class: "attach" }, attach, copy), actions,
    metricsSection(detail.metrics),
    ...(waiting ? [section("Waiting on " + waiting.key + " (" + waiting.kind + ")", detail.inbox || waiting.summary, true)] : []),
    runWorkflow(detail.workflow),
    section("Step records", records.join("\n"), false),
    el("details", {}, el("summary", { text: "Timeline (" + detail.timeline.length + " events)" }),
      el("ol", { class: "timeline" }, detail.timeline.slice().reverse().map((e) => el("li", { text: describe(e) })))),
    section("Gates", gates, false),
    section("Review findings", Object.keys(detail.reviews).length ? JSON.stringify(detail.reviews, null, 2) : "", false),
    section("progress.md", detail.progress, false),
    section("handoff.md", detail.handoff, false),
    section("Transcript tail" + (detail.transcript_file ? " (" + detail.transcript_file + ")" : ""), detail.transcript, false));
}

function closeDrawer() {
  ui.drawer = null;
  $("drawer").hidden = true;
}
