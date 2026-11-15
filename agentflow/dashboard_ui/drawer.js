"use strict";

// ------------------------------------------------------------------ the peek: a run's facts and its decision

function section(title, text, open, key) {
  return el("details", { open: open, "data-key": key }, el("summary", { text: title }), el("pre", { text: text || "(empty)" }));
}

function describe(event) {
  const bits = [event.ts ? event.ts.slice(11, 19) : "", event.type];
  for (const key of ["phase", "key", "kind", "status", "gate", "reason", "summary", "note"]) {
    if (event[key] !== undefined && event[key] !== null && event[key] !== "") bits.push(key + "=" + (typeof event[key] === "string" ? event[key] : JSON.stringify(event[key])));
  }
  if (event.by && event.by !== "runner") bits.push("by " + event.by);
  return bits.join("  ");
}

// Space on a card, or a click on its badge: the card's facts and the decision it waits on, without leaving the Board.
async function openPeek(runId) {
  ui.peek = runId;
  const peek = $("drawer");
  peek.hidden = false;
  let detail;
  try {
    detail = await api(runPath(runId));
  } catch (err) {
    if (ui.peek !== runId) return;
    peek.replaceChildren(el("p", { text: (err.status === 404 ? "No run " + runId : "Could not load " + runId + ": " + err.message) }),
      el("button", { type: "button", text: "Close", onclick: closePeek }));
    return;
  }
  if (ui.peek !== runId) return;
  const facts = detail.facts;
  const rows = [["status", detail.status], ["work", workLabel(detail.lane)], ["phase", facts.phase], ["step", facts.step],
    ["branch", facts.branch], ["pull request", facts.pr_url], ["spend", money(facts.spend_usd) + limits(facts.soft_budget_usd, facts.budget_usd)],
    ["refine", facts.refine ? "round " + facts.refine.round + ": " + facts.refine.note : ""], ["failure", facts.failure]]
    .filter(([, v]) => v).map(([k, v]) => el("div", {}, el("span", { class: "muted", text: k + ": " }), String(v)));
  const decision = decisionNode(detail) || el("p", { class: "muted", text: "Nothing is waiting on you." });
  const controls = actionNodes(detail, detail.actions.filter((a) => !DECISION_ACTIONS.includes(a)));
  peek.replaceChildren(
    el("h2", { text: detail.label }), el("div", { class: "muted", text: runId }), ...rows,
    decision,
    el("div", { class: "actions" }, controls,
      el("button", { type: "button", class: "primary", text: "Open run", onclick: () => { closePeek(); goRun(runId); } }),
      el("button", { type: "button", text: "Close", onclick: closePeek })));
}

function closePeek() {
  ui.peek = null;
  $("drawer").hidden = true;
}
