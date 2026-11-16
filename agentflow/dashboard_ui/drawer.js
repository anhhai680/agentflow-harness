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
function openPeek(runId) {
  ui.peek = runId;
  const peek = $("drawer");
  peek.hidden = false;
  if (peek.getAttribute("data-peek") !== runId) {  // another run's facts never stay on screen under this one's name
    peek.setAttribute("data-peek", runId);
    peek.replaceChildren(el("p", { class: "muted", text: "Loading " + runId + "…" }));
  }
  return loadPeek(runId);
}

// Fetch the peeked run and patch the peek by key, as the Run page is: an open Inbox stays open. A failure keeps a
// peek already drawn and says so in a toast.
async function loadPeek(runId) {
  const peek = $("drawer");
  let detail;
  try {
    detail = await api(runPath(runId));
  } catch (err) {
    if (ui.peek !== runId) return;
    if (err.status !== 404 && childByKey(peek, "head")) { toast("Could not refresh " + runId + ": " + err.message); return; }
    peek.replaceChildren(
      el("p", { text: (err.status === 404 ? "No run " + runId : "Could not load " + runId + ": " + err.message) }),
      el("button", { type: "button", text: "Close", onclick: closePeek }));
    return;
  }
  if (ui.peek !== runId) return;
  const facts = detail.facts;
  const rows = [["status", detail.status], ["work", workLabel(detail.lane)], ["phase", facts.phase], ["step", facts.step],
    ["branch", facts.branch], ["pull request", facts.pr_url], ["spend", money(facts.spend_usd) + limits(facts.soft_budget_usd, facts.budget_usd)],
    ["refine", facts.refine ? "round " + facts.refine.round + ": " + facts.refine.note : ""], ["failure", facts.failure]]
    .filter(([, v]) => v).map(([k, v]) => [k, String(v)]);
  const asked = decisionData(detail);
  const controls = detail.actions.filter((a) => !DECISION_ACTIONS.includes(a));
  patchChildren(peek, [
    entry("head", { label: detail.label, id: runId }, (h) => el("div", {}, el("h2", { text: h.label }), el("div", { class: "muted", text: h.id }))),
    entry("facts", rows, (list) => el("div", {}, list.map(([k, v]) => el("div", {}, el("span", { class: "muted", text: k + ": " }), v)))),
    asked.waiting || asked.actions.length
      ? entry("decision", asked, () => decisionNode(detail))
      : entry("no-decision", null, () => el("p", { class: "muted", text: "Nothing is waiting on you." })),
    entry("actions", { actions: controls, label: detail.label, live: (detail.live_agents || []).map((a) => a.label) }, () => el("div", { class: "actions" },
      actionNodes(detail, controls),
      el("button", { type: "button", class: "primary", text: "Open run", onclick: () => { closePeek(); goRun(runId); } }),
      el("button", { type: "button", text: "Close", onclick: closePeek }))),
  ], entryKey, entryNode);
}

function closePeek() {
  ui.peek = null;
  $("drawer").removeAttribute("data-peek");
  $("drawer").hidden = true;
}
