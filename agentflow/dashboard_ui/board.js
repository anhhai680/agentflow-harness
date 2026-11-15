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
  moves.addEventListener("keydown", (ev) => ev.stopPropagation());  // Enter on a button must not open the drawer too
  return moves;
}

function prNode(card) {
  const link = el("a", { href: card.pr_link, target: "_blank", rel: "noopener noreferrer", text: "PR ↗",
    onclick: (ev) => ev.stopPropagation() });
  link.addEventListener("keydown", (ev) => ev.stopPropagation());
  return el("div", {}, link);
}

function cardNode(card) {
  const agent = card.agent
    ? el("div", { class: "sub", title: card.agent.reason ? "effort: " + card.agent.reason : null },
        (card.agent.model || "?") + " · " + (card.agent.effort || "?") + arrow(card.agent.arrow))
    : el("div", { class: "sub", text: "no agent" });
  const where = card.task ? "Task " + card.task + " ▸ " + (card.step || "").split("/").pop() : (card.step || "not started");
  const gates = el("div", { class: "sub" }, card.gates.map((g) =>
    el("span", { class: g.skipped ? "gate-skip" : g.passed ? "gate-ok" : "gate-bad", text: (g.skipped ? "– " : g.passed ? "✓ " : "✗ ") + g.gate + " " })));
  // A refine round's implementer changed nothing, so its task was not reviewed again.
  const skips = (card.review_skipped || []).map((r) => el("span", { class: "gate-skip", title: "review skipped: " + r.reason, text: "– review task " + r.task + " (" + r.reason + ") " }));
  const node = el("article", { class: "card " + card.status, tabindex: "0", role: "button", draggable: card.ready ? "true" : null, onclick: () => openDrawer(card.run_id) },
    el("div", { class: "title", text: card.module }),
    el("div", { class: "sub", text: where }),
    el("span", { class: "badge", text: card.badge }),
    card.refine_round ? el("span", { class: "badge", text: "refine " + card.refine_round }) : null,
    agent,
    el("div", { class: "sub", text: "ctx " + (typeof card.context_pct === "number" ? Math.round(card.context_pct) + "%" : "-") + " · " + money(card.spend) + limits(card.soft_budget, card.budget) }),
    card.gates.length ? gates : null,
    skips.length ? el("div", { class: "sub" }, skips) : null,
    el("div", { class: "sub", text: "in phase " + ago(card.time_in_phase_s) + " · total " + dur(card.wall_s) }),
    card.pr_link ? prNode(card) : null,
    card.ready ? movesNode(card) : null);
  node.addEventListener("keydown", (ev) => { if (ev.key === "Enter") openDrawer(card.run_id); });
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
  scheduleRefresh();
}

async function removeItem(item) {
  if (!confirm("Remove " + item.label + " from Backlog?")) return;
  try {
    await api("/api/backlog/remove", { item: item.key });
    toast("Removed " + item.label);
  } catch (err) {
    toast("Could not remove: " + err.message);
  }
  scheduleRefresh();
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
      scheduleRefresh();
      return true;
    } catch (err) {
      toast("Could not add: " + err.message);
      return false;
    }
  });
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
  const columns = data.columns.map((name) => {
    const cards = name === "Backlog" ? [] : data.cards.filter((c) => c.column === name);
    const col = el("div", { class: "col" }, el("h2", {}, el("span", { text: name }), el("span", { text: String(name === "Backlog" ? data.backlog.length : cards.length) })));
    dropOn(col, name);
    if (name === "Backlog") col.append(...data.backlog.map(backlogNode));
    if (name === "Backlog" && data.done_note) col.append(el("div", { class: "muted gate-bad", text: "Done marks not read: " + data.done_note }));
    if (name === "Backlog" && (data.backlog_done || []).length) col.append(doneNode(data.backlog_done));
    if (name === "Backlog" && (data.backlog_skipped || []).length) col.append(el("div", { class: "muted", text: "Not modules: " + data.backlog_skipped.join(", ") }));
    for (const card of cards) col.append(cardNode(card));
    return col;
  });
  $("new-task").hidden = !Object.keys(data.new_task).length;
  $("sync-note").hidden = !data.sync_note;
  $("sync-note").textContent = data.sync_note ? "Base branch not updated: " + data.sync_note : "";
  $("issues-note").hidden = !data.issues_note;
  $("issues-note").textContent = data.issues_note || "";
  $("retro-due").hidden = !data.retro_due;
  $("retro-due").replaceChildren(...(data.retro_due ? [document.createTextNode("Retro due: " + data.retro_due + " → run "), el("code", { text: "agentflow retro" })] : []));
  if (!ui.dragging) $("columns").replaceChildren(...columns);  // new columns would lose the drop highlights mid-drag
  $("needs").replaceChildren(...(data.needs_human.length ? data.needs_human.map(needNode) : [el("p", { class: "muted", text: "Nothing is waiting on you." })]));
}

function needNode(item) {
  const questions = item.questions || [];
  const what = el("div", { class: "what" },
    el("strong", { text: item.run_id }), " · ", item.key, " · ", item.kind, " · ", ago(item.age_s) + " ago",
    item.summary ? el("div", { class: "sub", text: item.summary }) : null,
    questions.length ? el("ul", {}, questions.map((q) => el("li", { text: q.id + ": " + q.question }))) : null);
  const buttons = [];
  const target = item.kind === "phase_approval" ? item.phase : item.key;
  if (questions.length) {
    buttons.push(el("button", { type: "button", class: "primary", text: "Answer…", onclick: () => answerDialog(item) }));
  } else {
    buttons.push(el("button", { type: "button", class: "primary", text: "Approve", onclick: () => {
      if (item.kind === "blocked" || item.kind === "blocked_repeat") {
        const required = item.kind === "blocked_repeat";
        const note = prompt("Tell the agent what changed or what to do" + (required ? " (required: it blocked again with nothing changed)" : " (optional)") + ":", "");
        if (note !== null) act(item.run_id, "approve", note.trim() ? { target: target, note: note.trim() } : { target: target });
      } else if (confirm("Approve " + item.key + " for " + item.run_id + "?")) {
        act(item.run_id, "approve", { target: target });
      }
    } }));
  }
  if (item.kind !== "parked") {
    buttons.push(el("button", { type: "button", text: "Reject…", onclick: () => rejectDialog(item.run_id, target) }));
  }
  buttons.push(el("button", { type: "button", text: "Open", onclick: () => openDrawer(item.run_id) }));
  return el("div", { class: "need" }, what, ...buttons);
}
