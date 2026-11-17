"use strict";

// ------------------------------------------------------------------ board

// A ready card moves one phase on, or to Done. The buttons and drag and drop are two ways to the same two actions.

const continueCard = (card) => act(card.run_id, "resume");

function markDone(card) {
  const skips = card.ready.skips;
  const warning = skips.length ? "\n\nThese phases will not run: " + skips.join(", ") + ". The PR says so." : "";
  if (confirm("Mark " + card.module + " done and open a draft PR?" + warning)) act(card.run_id, "done");
}

function canDrop(name) {
  const drag = ui.dragging;
  if (!drag) return false;
  if (drag.start) return name === drag.start.first_phase;  // the first phase with work for it (a finished spec: plan), never Done
  return name === "Done" || name === drag.ready.next;
}

function showDropTargets() {
  for (const col of document.querySelectorAll(".col")) col.classList.toggle("drop", canDrop(col.dataset.column));
}

function dropOn(col, name) {
  col.dataset.column = name;
  col.addEventListener("dragover", (ev) => {
    if (!canDrop(name)) return;
    ev.preventDefault();
    ev.dataTransfer.dropEffect = "move";
  });
  col.addEventListener("drop", (ev) => {
    if (!canDrop(name)) return;
    ev.preventDefault();
    const drag = ui.dragging;
    setTimeout(() => {  // confirm() must not block the drag's end
      if (drag.start) startItem(drag.start);
      else if (name === "Done") markDone(drag);
      else continueCard(drag);
    }, 0);
  });
}

function dragFrom(node, dragged, id) {
  node.addEventListener("dragstart", (ev) => {
    ui.dragging = dragged;
    ev.dataTransfer.effectAllowed = "move";
    ev.dataTransfer.setData("text/plain", id);
    showDropTargets();
  });
  node.addEventListener("dragend", () => {
    ui.dragging = null;
    showDropTargets();
    if (ui.board) renderBoard(ui.board);  // what arrived during the drag
  });
}

function movesNode(card) {
  const button = (text, primary, onclick) => el("button", { type: "button", class: primary ? "primary" : null, text: text,
    onclick: (ev) => { ev.stopPropagation(); onclick(card); } });
  const next = card.ready.next;
  const moves = el("div", { class: "moves" },
    next ? button("Continue → " + next, true, continueCard) : null,
    button("Mark done", !next, markDone));
  moves.addEventListener("keydown", (ev) => ev.stopPropagation());  // Enter or Space on a button must not open the run too
  return moves;
}

function prNode(card) {
  const link = el("a", { href: card.pr_link, target: "_blank", rel: "noopener noreferrer", text: "PR ↗",
    onclick: (ev) => ev.stopPropagation() });
  link.addEventListener("keydown", (ev) => ev.stopPropagation());
  return el("div", {}, link);
}

// The last `agent_activity` of each run: a card rebuilt by a Board refresh keeps showing it.
const lastActivity = new Map();

const activityText = (ctx, tool) => "ctx " + (typeof ctx === "number" ? Math.round(ctx) + "%" : "-") + (tool ? " · " + tool : "");

// An agent's turn: only its card's activity line changes. The card is found by its data-run, never by a selector
// built from the run id.
function patchActivity(runId, event) {
  const before = lastActivity.get(runId) || {};
  const now = {
    context_pct: typeof event.context_pct === "number" ? event.context_pct : before.context_pct,
    last_tool: event.last_tool || before.last_tool || null,
  };
  lastActivity.set(runId, now);
  for (const card of document.querySelectorAll("[data-run]")) {
    if (card.getAttribute("data-run") !== runId) continue;
    const line = card.querySelector(".activity");
    if (line) line.textContent = activityText(now.context_pct, now.last_tool);
  }
}

// One small segment per phase of the card's lane, classed strip-<state>; the phase the run is in also carries its task.
function stripNode(card) {
  return el("div", { class: "strip", role: "img", "aria-label": "Phase progress" }, card.strip.map((seg) =>
    el("span", { class: "strip-seg strip-" + seg.state, title: seg.phase + ": " + seg.state },
      seg.phase === card.phase && card.task ? el("span", { class: "strip-task", text: card.task }) : null)));
}

// "● needs you · 12 min": the card waits on a person, and for how long.
function needsYouNode(card) {
  const since = Date.parse(card.waiting.since || "");
  const when = Number.isNaN(since) ? "" : " · " + ago(Math.max(0, (Date.now() - since) / 1000));
  return el("div", { class: "needs-you", text: "● needs you" + when, title: card.waiting.summary || null });
}

function cardNode(card) {
  const many = card.agents && card.agents.length > 1;
  const agent = many
    ? el("div", { class: "sub", title: card.agents.map((a) => a.label + ": " + (a.model || "?") + " · " + (a.effort || "?")).join("\n"),
        text: card.agents.length + " agents: " + card.agents.map((a) => a.label).join(", ") })
    : card.agent
    ? el("div", { class: "sub", title: card.agent.reason ? "effort: " + card.agent.reason : null },
        (card.agent.model || "?") + " · " + (card.agent.effort || "?") + arrow(card.agent.arrow))
    : el("div", { class: "sub", text: "no agent" });
  const where = card.task ? "Task " + card.task + " ▸ " + (card.step || "").split("/").pop() : (card.step_label || "not started");
  const gates = el("div", { class: "sub" }, card.gates.map((g) =>
    el("span", { class: g.skipped ? "gate-skip" : g.passed ? "gate-ok" : "gate-bad", text: (g.skipped ? "– " : g.passed ? "✓ " : "✗ ") + g.gate + " " })));
  // A refine round's implementer changed nothing, so its task was not reviewed again.
  const skips = (card.review_skipped || []).map((r) => el("span", { class: "gate-skip", title: "review skipped: " + r.reason, text: "– review task " + r.task + " (" + r.reason + ") " }));
  if (!card.agent) lastActivity.delete(card.run_id);  // no live agent: its last tool is history
  const seen = lastActivity.get(card.run_id);
  const node = el("article", { class: "card " + card.status, tabindex: "0", role: "button", draggable: card.ready ? "true" : null,
    "data-run": card.run_id, onclick: () => goRun(card.run_id) },
    el("div", { class: "title", text: card.module }),
    el("div", { class: "sub", text: where }),
    card.strip && card.strip.length ? stripNode(card) : null,
    card.waiting ? needsYouNode(card) : null,
    el("span", { class: "badge peekable", text: card.badge, title: "Peek (Space)", onclick: (ev) => { ev.stopPropagation(); openPeek(card.run_id); } }),
    card.refine_round ? el("span", { class: "badge", text: "refine " + card.refine_round }) : null,
    agent,
    el("div", { class: "sub" }, el("span", { class: "activity", text: activityText(seen ? seen.context_pct : card.context_pct, seen && seen.last_tool) }),
      " · " + money(card.spend) + limits(card.soft_budget, card.budget)),
    card.gates.length ? gates : null,
    skips.length ? el("div", { class: "sub" }, skips) : null,
    el("div", { class: "sub", text: "in phase " + ago(card.time_in_phase_s) + " · total " + dur(card.wall_s) }),
    card.pr_link ? prNode(card) : null,
    card.ready ? movesNode(card) : null);
  node.addEventListener("keydown", (ev) => {  // Enter opens the Run page, Space a peek at it
    if (ev.key === "Enter") goRun(card.run_id);
    else if (ev.key === " ") { ev.preventDefault(); openPeek(card.run_id); }
  });
  if (card.ready) dragFrom(node, card, card.run_id);
  return node;
}

// ------------------------------------------------------------------ backlog

// Nothing starts by itself: a developer drags a card to its first phase or presses Start. A blocked card is locked.

async function startItem(item) {
  try {
    await api("/api/backlog/start", item.kind === "module" ? { module: item.id } : { item: item.key });
    toast("Starting " + item.label + "…");
  } catch (err) {
    toast("Could not start " + item.label + ": " + err.message);
  }
  refreshAfter();
}

async function removeItem(item) {
  if (!confirm("Remove " + item.label + " from Backlog?")) return;
  try {
    await api("/api/backlog/remove", { item: item.key });
    toast("Removed " + item.label);
  } catch (err) {
    toast("Could not remove: " + err.message);
  }
  refreshAfter();
}

function backlogMark(item) {
  if (item.status === "parallel") return el("span", { class: "badge p", text: "P", title: "Can run in parallel", "aria-label": "Can run in parallel", role: "img" });
  if (item.status === "blocked") return el("span", { class: "badge lock", text: "Blocked" });
  if (item.status === "starting") return el("span", { class: "badge", text: "Starting…" });
  if (item.status === "taken") return el("span", { class: "badge lock", text: "Taken elsewhere" });
  return el("span", { class: "badge ready-mark", text: "Ready" });
}

// A synced issue's link: the server keeps only an https URL of that issue, and the title stays text.
function issueNode(url) {
  if (!/^https:\/\//.test(url)) return null;
  const link = el("a", { href: url, target: "_blank", rel: "noopener noreferrer", text: "Issue ↗",
    onclick: (ev) => ev.stopPropagation() });
  link.addEventListener("keydown", (ev) => ev.stopPropagation());
  return el("div", {}, link);
}

function backlogNode(item) {
  const canStart = item.status === "parallel" || item.status === "ready";
  const names = (list) => list.map((p) => p.label).join(", ");
  const buttons = [];
  if (canStart) buttons.push(el("button", { type: "button", class: "primary", text: "Start", onclick: (ev) => { ev.stopPropagation(); startItem(item); } }));
  if (item.removable) buttons.push(el("button", { type: "button", class: "danger", text: "Remove", onclick: (ev) => { ev.stopPropagation(); removeItem(item); } }));
  const node = el("article", { class: "card backlog " + item.status + (item.failure ? " failed-start" : ""), draggable: canStart ? "true" : null },
    el("div", { class: "title", text: item.label }),
    backlogMark(item),
    item.url ? issueNode(item.url) : null,
    item.blocked_by.length ? el("div", { class: "sub", text: "Runs after: " + item.blocked_by.map((b) => b.text).join(", ") }) : null,
    item.blocks && item.blocks.length ? el("div", { class: "sub", text: "Blocks: " + names(item.blocks) }) : null,
    item.parallel_with.length ? el("div", { class: "sub", text: "Parallel with: " + names(item.parallel_with) }) : null,
    item.note ? el("div", { class: "sub", text: item.note }) : null,
    item.failure ? el("div", { class: "sub gate-bad", text: "Could not start: " + item.failure + (canStart ? " — Start tries again" : "") }) : null,
    buttons.length ? el("div", { class: "moves" }, buttons) : null);
  if (canStart) dragFrom(node, { start: item }, item.key);
  return node;
}

function newTaskDialog() {
  const data = ui.board;
  const kinds = Object.keys(data.new_task);
  if (!kinds.length) return;
  const radios = kinds.map((name, index) => el("input", { type: "radio", name: "nt-kind", value: name, checked: index === 0 ? "checked" : null }));
  const kind = () => radios.find((radio) => radio.checked).value;
  const fields = el("div", {});
  const inputs = {};
  const pick = (id, label, multi, choices, hint) => {
    const select = el("select", { id: id, multiple: multi ? "multiple" : null }, choices.map((c) => el("option", { value: c.key, text: c.label })));
    inputs[id] = select;
    return [el("label", { for: id, text: label }), hint ? el("div", { class: "sub", text: hint }) : null, select];
  };
  const text = (id, label, area, max) => {
    const input = area ? el("textarea", { id: id, maxlength: max, class: area === "short" ? "short" : null }) : el("input", { type: "text", id: id, maxlength: max });
    inputs[id] = input;
    return [el("label", { for: id, text: label }), input];
  };
  const build = () => {
    const wanted = data.new_task[kind()];
    const parts = [];
    if (wanted.includes("issue") && wanted.includes("new_module")) parts.push(el("div", { class: "sub", text: "Fill in the issue number, a new module, or both: an issue marks it on GitHub and links the pull request." }));
    if (wanted.includes("issue")) parts.push(...text("nt-issue", "Issue number", false, 12));
    if (wanted.includes("task")) parts.push(...text("nt-task", "Task (paste a log here if it helps)", true, 16000));
    if (wanted.includes("topic")) parts.push(...text("nt-topic", "Topic", false, 400));
    if (wanted.includes("new_module")) {
      parts.push(...text("nt-module", "Module id (lowercase, - or _)", false, 60));
      parts.push(...text("nt-resp", "What it does", "short", 400));
      parts.push(...pick("nt-deps", "Depends on", true, data.modules.map((m) => ({ key: m, label: m })), data.work_source_kind === "specs" ? "Only modules already in the spec files." : "Only modules already in the map."));
    }
    const phases = data.lane_phases[kind()] || [];
    parts.push(...pick("nt-stop", "Stop after", false, [{ key: "", label: "(run to the end)" }, ...phases.map((p) => ({ key: p, label: p }))]));
    if (wanted.includes("after")) parts.push(...pick("nt-after", "Runs after (optional)", true, data.after_choices, "The card stays locked until each one is merged."));
    fields.replaceChildren(...parts.filter(Boolean));
  };
  for (const radio of radios) radio.addEventListener("change", build);
  build();
  const chosen = (id) => Array.from(inputs[id].selectedOptions).map((o) => o.value);
  const picker = el("fieldset", {}, el("legend", { text: "What kind of work?" }),
    kinds.map((name, index) => el("label", { class: "kind" }, radios[index], workLabel(name))));
  dialog("New task", [picker, fields], async () => {
    const wanted = data.new_task[kind()];
    const body = { lane: kind(), fields: {}, after: wanted.includes("after") ? chosen("nt-after") : [], to_phase: inputs["nt-stop"].value || null };
    if (wanted.includes("issue")) body.fields.issue = inputs["nt-issue"].value.trim();
    if (wanted.includes("task")) body.fields.task = inputs["nt-task"].value;
    if (wanted.includes("topic")) body.fields.topic = inputs["nt-topic"].value.trim();
    if (wanted.includes("new_module") && (inputs["nt-module"].value.trim() || inputs["nt-resp"].value.trim()))  // an issue alone is enough
      body.fields.new_module = { module: inputs["nt-module"].value.trim(), responsibility: inputs["nt-resp"].value, deps: chosen("nt-deps") };
    for (const name of Object.keys(body.fields)) if (body.fields[name] === "" || (typeof body.fields[name] === "string" && !body.fields[name].trim())) delete body.fields[name];
    try {
      await api("/api/backlog", body);
      toast("Added to Backlog: press Start or drag it to " + ((data.lane_phases[kind()] || [])[0] || "its first phase"));
      refreshAfter();
      return true;
    } catch (err) {
      toast("Could not add: " + err.message);
      return false;
    }
  });
}

// A column keeps its node, and its drop handlers, across refreshes: only its cards are patched, keyed by run id or
// by backlog item key.
function columnNode(name) {
  const col = el("div", { class: "col" });
  dropOn(col, name);
  return col;
}

function columnEntries(data, name) {
  const cards = name === "Backlog" ? [] : data.cards.filter((c) => c.column === name);
  const count = name === "Backlog" ? data.backlog.length : cards.length;
  const entries = [entry("col-head", { name: name, count: count }, (head) => el("h2", {}, el("span", { text: head.name }), el("span", { text: String(head.count) })))];
  if (name === "Backlog") {
    entries.push(...data.backlog.map((item) => entry(item.key || item.id, item, backlogNode)));
    if (data.done_note) entries.push(entry("done-note", data.done_note, (note) => el("div", { class: "muted gate-bad", text: "Done marks not read: " + note })));
    if ((data.backlog_done || []).length) entries.push(entry("done", data.backlog_done, () => doneNode(data.backlog_done)));
    if ((data.backlog_skipped || []).length) entries.push(entry("skipped", data.backlog_skipped, (names) => el("div", { class: "muted", text: "Not modules: " + names.join(", ") })));
  }
  entries.push(...cards.map((card) => entry(card.run_id, card, cardNode)));
  return entries;
}

// Modules that are finished, with why. One line each and no Start button: the open state survives the SSE re-render.
function doneNode(done) {
  if (!done.length) return null;
  return el("details", { class: "done-modules", open: ui.doneOpen ? "true" : null, ontoggle: (ev) => { ui.doneOpen = ev.target.open; } },
    el("summary", { text: "Done (" + done.length + ")" }),
    done.map((item) => el("div", { class: "muted", text: item.id + " · " + item.reason })));
}

function renderBoard(data) {
  ui.board = data;
  const counts = Object.entries(data.counts).map(([k, v]) => k.replace("_", " ") + " " + v).join(" · ") || "no runs";
  const byModel = Object.entries(data.agents_running.by_model).map(([k, v]) => k + " " + v).join(" · ");
  $("summary").textContent = counts + " | agents running " + data.agents_running.total + (byModel ? " (" + byModel + ")" : "") +
    " | today " + money(data.spend_today) + " (budget per run: soft " + money(data.run_budget_usd) + " · hard " + money(data.run_budget_hard_usd) + ")";
  const columns = data.columns.map((name) => entry(name, name, columnNode));
  $("new-task").hidden = !Object.keys(data.new_task).length;
  $("sync-note").hidden = !data.sync_note;
  $("sync-note").textContent = data.sync_note ? "Base branch not updated: " + data.sync_note : "";
  $("issues-note").hidden = !data.issues_note;
  $("issues-note").textContent = data.issues_note || "";
  $("retro-due").hidden = !data.retro_due;
  $("retro-due").replaceChildren(...(data.retro_due ? [document.createTextNode("Retro due: " + data.retro_due + " → run "), el("code", { text: "agentflow retro" })] : []));
  if (!ui.dragging) {  // a patch mid-drag would lose the drop highlights; dragend draws what arrived
    patchChildren($("columns"), columns, entryKey, entryNode);
    for (const name of data.columns) patchChildren(childByKey($("columns"), name), columnEntries(data, name), entryKey, entryNode);
  }
  $("needs-title").hidden = !ui.inboxLegacy;
  const waiting = data.needs_human.length;
  const inbox = waiting === 0 ? el("p", { class: "muted", text: "Nothing is waiting on you." })
    : el("p", {}, el("a", { href: "#/inbox", text: inboxPlural(waiting, "decision waits", "decisions wait") + " in the Inbox" }));
  // the old list stays behind `dashboard.inbox_legacy`; otherwise one line points to the Inbox tab
  $("needs").replaceChildren(...(ui.inboxLegacy ? (waiting ? data.needs_human.map(needNode) : [inbox]) : [inbox]));
}

// What an approval needs: a blocked stop takes a note (required when it blocked again with nothing changed).
const needTarget = (item) => (item.kind === "phase_approval" ? item.phase : item.key);

function approveFlow(item) {
  const target = needTarget(item);
  if (item.kind === "blocked" || item.kind === "blocked_repeat") {
    const required = item.kind === "blocked_repeat";
    const note = prompt("Tell the agent what changed or what to do" + (required ? " (required: it blocked again with nothing changed)" : " (optional)") + ":", "");
    if (note === null) return;
    if (required && !note.trim()) { toast("Approving " + item.key + " needs a note: it blocked again with nothing changed"); return; }
    act(item.run_id, "approve", note.trim() ? { target: target, note: note.trim() } : { target: target });
  } else if (confirm("Approve " + item.key + " for " + item.run_id + "?")) {
    const body = { target: target };
    if (item.kind === "phase_approval") {
      const viewed = viewedList(item.run_id);  // the spec and plan revisions this page showed; informational
      if (viewed.length) body.viewed = viewed;
    }
    act(item.run_id, "approve", body);
  }
}

function needNode(item) {
  const questions = item.questions || [];
  const what = el("div", { class: "what" },
    el("strong", { text: item.run_id }), " · ", item.key, " · ", item.kind, " · ", ago(item.age_s) + " ago",
    item.summary ? el("div", { class: "sub", text: item.summary }) : null,
    questions.length ? el("ul", {}, questions.map((q) => el("li", { text: q.id + ": " + q.question }))) : null);
  const buttons = [];
  if (questions.length) {
    buttons.push(el("button", { type: "button", class: "primary", text: "Answer…", onclick: () => answerDialog(item) }));
  } else {
    buttons.push(el("button", { type: "button", class: "primary", text: "Approve", onclick: () => approveFlow(item) }));
  }
  if (item.kind !== "parked") {
    buttons.push(el("button", { type: "button", text: "Reject…", onclick: () => rejectDialog(item.run_id, needTarget(item)) }));
  }
  buttons.push(el("button", { type: "button", text: "Open", onclick: () => goRun(item.run_id, "overview", "decision") }));
  return el("div", { class: "need" }, what, ...buttons);
}
