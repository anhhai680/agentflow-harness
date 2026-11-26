"use strict";

// ------------------------------------------------------------------ board

// What the Board shows is a pure function of the cards and the query in the URL (`#/board?lane=&status=&needs=1&q=`).
// A search is plain text: lower-cased, one leading `#` dropped, then `includes`. It is never compiled to a pattern.

function filterCards(cards, query, lanes, statuses) {
  const want = query || {};
  const lane = want.lane && lanes.includes(want.lane) ? want.lane : null;  // a value the board does not know filters nothing
  const status = want.status && statuses.includes(want.status) ? want.status : null;
  let text = String(want.q || "").trim().toLowerCase();
  if (text.startsWith("#")) text = text.slice(1);
  return cards.filter((card) => {
    if (lane && card.lane !== lane) return false;
    if (status && card.status !== status) return false;
    if (want.needs === true && !card.waiting) return false;
    if (!text) return true;
    return [card.module, card.issue, card.branch, card.run_id].some((field) =>
      field !== null && field !== undefined && String(field).toLowerCase().includes(text));
  });
}

// The Done cards, the newest first (by the time each ended, then by run id), cut to `shown` unless `all`.
function doneCards(cards, shown, all) {
  const sorted = cards.slice().sort((a, b) => {
    const x = a.ended_at || "";
    const y = b.ended_at || "";
    if (x !== y) return x < y ? 1 : -1;
    return a.run_id < b.run_id ? 1 : a.run_id > b.run_id ? -1 : 0;
  });
  if (all || sorted.length <= shown) return { cards: sorted, hidden: 0 };
  return { cards: sorted.slice(0, shown), hidden: sorted.length - shown };
}

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

// One label per parked step of the card: "parked: Task 3 · implement", its summary as the title.
function parkedLabels(card) {
  return ((card && card.parked) || []).map((p) => ({ text: "parked: " + (p.label || p.key), title: p.summary || "" }));
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
  const parked = parkedLabels(card);
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
    parked.length ? el("div", { class: "sub" }, parked.map((p) => el("span", { class: "badge", text: p.text, title: p.title }))) : null,
    typeof card.slot === "number" ? el("span", { class: "badge lock", text: "slot " + card.slot }) : null,
    card.backlog_note ? el("div", { class: "sub", text: card.backlog_note }) : null,
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

const BACKLOG_ARM_MS = 4000;  // an idea's first Start click arms it this long; a second one in that time confirms. The Inbox's Approve does the same.
let backlogArmTimer = null;

function disarmBacklog() {
  clearTimeout(backlogArmTimer);
  backlogArmTimer = null;
  if (ui.backlogArm !== null) { ui.backlogArm = null; if (ui.board) renderBoard(ui.board); }
}

// An idea (a backlog-file item whose status is `backlog`) starts on a second click: the first arms the card, and the
// start then carries `confirm: true`, which the server asks for.
function armedFor(item) { return ui.backlogArm === item.key && Date.now() - ui.backlogArmAt < BACKLOG_ARM_MS; }

async function startItem(item) {
  const idea = item.planning === "backlog";
  if (idea && !armedFor(item)) {
    clearTimeout(backlogArmTimer);
    ui.backlogArm = item.key;
    ui.backlogArmAt = Date.now();
    backlogArmTimer = setTimeout(disarmBacklog, BACKLOG_ARM_MS);
    toast(item.label + " is an idea: press Start again to confirm");
    if (ui.board) renderBoard(ui.board);
    return;
  }
  if (idea) { clearTimeout(backlogArmTimer); backlogArmTimer = null; ui.backlogArm = null; }
  try {
    const body = item.kind === "module" ? { module: item.id } : { item: item.key };
    if (idea) body.confirm = true;
    await api("/api/backlog/start", body);
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
  if (item.status === "no_fix") return el("span", { class: "badge lock", text: "No fix yet" });
  if (item.status === "in_progress") return el("span", { class: "badge", text: "In progress" });
  return el("span", { class: "badge ready-mark", text: "Ready" });
}

// A synced issue's or alert's link: the server keeps only an https URL, and the title stays text.
function linkNode(url, text) {
  if (!/^https:\/\//.test(url)) return null;
  const link = el("a", { href: url, target: "_blank", rel: "noopener noreferrer", text: text,
    onclick: (ev) => ev.stopPropagation() });
  link.addEventListener("keydown", (ev) => ev.stopPropagation());
  return el("div", {}, link);
}

// Only the four severities Dependabot reports become a CSS class; anything else draws no pill.
function severityClass(sev) {
  return ["low", "medium", "high", "critical"].includes(sev) ? "sev-" + sev : null;
}

// The lines under an alert card's title: how many alerts it groups, where, and each open Dependabot PR.
function alertLines(item) {
  const n = item.alerts.length;
  return [`${n} alert${n === 1 ? "" : "s"} · ${item.manifest}`,
    ...item.dependabot_prs.map((k) => "Dependabot PR #" + k + " open")];
}

function backlogNode(item) {
  const canStart = (item.status === "parallel" || item.status === "ready") && item.startable !== false;
  const names = (list) => list.map((p) => p.label).join(", ");
  const buttons = [];
  if (canStart) buttons.push(el("button", { type: "button", class: "primary", text: item.armed ? "Confirm start" : "Start", onclick: (ev) => { ev.stopPropagation(); startItem(item); } }));
  else if (item.startable === false) buttons.push(el("button", { type: "button", class: "primary", text: "Start", disabled: "disabled", title: item.note || "" }));
  if (item.removable) buttons.push(el("button", { type: "button", class: "danger", text: "Remove", onclick: (ev) => { ev.stopPropagation(); removeItem(item); } }));
  if (isOpenFileCard(item)) buttons.push(...fileButtons(item));
  if (item.kind === "issue" && item.addable) buttons.push(el("button", { type: "button", text: "Add to backlog", onclick: (ev) => { ev.stopPropagation(); addIssue(item); } }));
  const isAlert = item.kind === "alert";
  const sev = isAlert ? severityClass(item.severity) : null;
  const node = el("article", { class: "card backlog " + item.status + (item.failure ? " failed-start" : ""), draggable: canStart ? "true" : null },
    el("div", { class: "title", text: item.label }),
    sev ? el("span", { class: "pill " + sev, text: item.severity }) : null,
    backlogMark(item),
    item.planning === "backlog" ? el("span", { class: "badge", text: "idea", title: "status: backlog. Start asks for a second click." }) : null,
    item.item && item.local === true ? el("span", { class: "badge", text: LOCAL_BADGE, title: LOCAL_TITLE }) : null,
    isAlert ? alertLines(item).map((line) => el("div", { class: "sub", text: line })) : null,
    item.url ? linkNode(item.url, isAlert ? "Alert ↗" : "Issue ↗") : null,
    item.blocked_by.length ? el("div", { class: "sub", text: "Runs after: " + item.blocked_by.map((b) => b.text).join(", ") }) : null,
    item.blocks && item.blocks.length ? el("div", { class: "sub", text: "Blocks: " + names(item.blocks) }) : null,
    item.parallel_with.length ? el("div", { class: "sub", text: "Parallel with: " + names(item.parallel_with) }) : null,
    item.note ? el("div", { class: "sub", text: item.note }) : null,
    item.failure ? el("div", { class: "sub gate-bad", text: "Could not start: " + item.failure + (canStart ? " — Start tries again" : "") }) : null,
    buttons.length ? el("div", { class: "moves" }, buttons) : null);
  if (canStart) dragFrom(node, { start: item }, item.key);
  return node;
}

// ------------------------------------------------------------------ another machine's claim

// How long ago a claim last moved, from the seconds the server computed: "3 d", "2 h", "10 min"; "" when it has no age
// (an unreadable claim).
function claimAge(seconds) {
  if (typeof seconds !== "number" || !isFinite(seconds) || seconds < 0) return "";
  if (seconds < 60) return "just now";
  if (seconds < 3600) return Math.floor(seconds / 60) + " min";
  if (seconds < 86400) return Math.floor(seconds / 3600) + " h";
  return Math.floor(seconds / 86400) + " d";
}

// A claim another machine holds on `origin`, as a read-only card: no Start, Edit or drag, and every field is text (the
// claimant, the run and the reason of an unreadable claim come from a file anyone with push access to origin can write).
function remoteClaimNode(claim) {
  const unreadable = !!claim.unreadable;
  const where = [claim.phase, claim.status].filter(Boolean).join(" · ");
  const line = (text, props) => (text ? el("div", Object.assign({ class: "sub", text: text }, props || {})) : null);
  return el("article", { class: "card backlog claim" + (claim.stale ? " stale" : "") + (unreadable ? " failed-start" : "") },
    el("div", { class: "title", text: claim.id }),
    claim.title && claim.title !== claim.id ? line(claim.title) : null,
    el("span", { class: "badge lock", text: unreadable ? "Claim unreadable" : "Claimed elsewhere" }),
    claim.stale ? el("span", { class: "badge stale-mark", text: "stale", title: "No phase or status change for a while: the claimant may have stopped" }) : null,
    unreadable ? el("div", { class: "sub gate-bad", text: claim.unreadable }) : null,
    line(claim.by ? "by " + claim.by : ""),
    line(where),
    line(claimAge(claim.age_s), { title: claim.updated ? String(claim.updated) : null }),
    claim.run ? line("run " + claim.run, { title: String(claim.run) }) : null);
}

// A claim sits in its own phase column. An unreadable one, or one whose phase this Board does not have (or `Done`, which
// no claim has), sits in Backlog rather than vanishing: a claim that blocks a start must stay visible.
function claimColumn(data, claim) {
  const known = claim.column !== "Done" && (data.columns || []).includes(claim.column);
  return claim.unreadable || !known ? "Backlog" : claim.column;
}

const claimsIn = (data, name) => (data.remote_claims || []).filter((claim) => claimColumn(data, claim) === name);

const claimEntry = (claim) => entry(claim.key || "claim:" + claim.id, claim, remoteClaimNode);

function showClaimsNote(note) {
  $("claims-note").hidden = !note;
  $("claims-note").textContent = note || "";
}

// ------------------------------------------------------------------ the backlog file
//
// In a `backlog` pack the Board edits `backlog.yml` in the working tree (never a commit) through `POST /api/backlog/file`.
// Every write carries the sha-256 the page read (`backlog_sha256`); the server refuses it with a 409 when the file has
// changed since. Every value reaches the DOM as text or a control's `.value`, never as HTML.

const LOCAL_BADGE = "local: commit to share";
const LOCAL_TITLE = "This item is not in backlog.yml on the base branch yet: commit and push the file to share it.";
const FILE_STALE = "backlog.yml changed on disk since the page read it: reload";
const FILE_STATUSES = ["backlog", "ready", "done", "dropped"];
const RESP_TITLE_MAX = 200;

const fileSha = () => (ui.board && ui.board.backlog_sha256) || "";

// A write is built from what the page showed, so it carries the sha of the render that showed it: a card's `sha` (set when
// the Backlog column is drawn), never the one of a later refresh. A refresh that comes while a dialog is open must not
// let a save past the server's stale-file check.
const shaOf = (card) => (typeof card.sha === "string" ? card.sha : fileSha());

// A card read from the file whose item is still open: the only kind the page edits and moves.
const isOpenFileCard = (card) => !!(card && card.kind === "module" && card.item && ["backlog", "ready"].includes(card.item.status));

const sameSet = (a, b) => { const x = a.slice().sort(); const y = b.slice().sort(); return x.length === y.length && x.every((v, i) => v === y[i]); };

// The `edit` body of `form` (what the dialog holds) against `card.item` (what the page read): only the keys that differ.
function editBody(card, form, sha) {
  const was = card.item || {};
  const fields = {};
  for (const key of ["title", "lane", "status", "spec"]) {
    const now = String(form[key] === undefined || form[key] === null ? "" : form[key]).trim();
    if (now !== String(was[key] === undefined || was[key] === null ? "" : was[key])) fields[key] = now;
  }
  const notes = String(form.notes === undefined || form.notes === null ? "" : form.notes);
  if (notes !== String(was.notes || "")) fields.notes = notes;
  const deps = Array.isArray(form.depends_on) ? form.depends_on : [];
  if (!sameSet(deps, was.depends_on || [])) fields.depends_on = deps;
  const issue = String(form.issue === undefined || form.issue === null ? "" : form.issue).trim();
  if (issue !== (was.issue === null || was.issue === undefined ? "" : String(was.issue))) fields.issue = issue === "" ? null : /^\d+$/.test(issue) ? Number(issue) : issue;
  return { op: "edit", id: card.id, fields: fields, sha256: sha };
}

const moveBody = (card, up, sha) => ({ op: "move", id: card.id, direction: up ? "up" : "down", sha256: sha });

const addFromSpecBody = (path, sha) => ({ op: "add", item: { spec: path, status: "backlog" }, sha256: sha });

// A synced issue becomes an item: the page sends only the number, the server builds the item from its cache.
const addIssueBody = (number, sha) => ({ op: "add_issue", number: String(number), sha256: sha });

// One line of at most `RESP_TITLE_MAX` characters, cut with an ellipsis.
function oneLine(text) {
  const line = String(text || "").replace(/\s+/g, " ").trim();
  return line.length > RESP_TITLE_MAX ? line.slice(0, RESP_TITLE_MAX - 1) + "…" : line;
}

// A new module of the New task form, as an item of the file: ready to start, its title the responsibility as one line.
function newModuleBody(form, sha) {
  const item = { id: String(form.module || "").trim(), title: oneLine(form.responsibility), depends_on: form.deps || [], status: "ready" };
  const issue = String(form.issue === undefined || form.issue === null ? "" : form.issue).trim();
  if (issue) item.issue = /^\d+$/.test(issue) ? Number(issue) : issue;
  return { op: "add", item: item, sha256: sha };
}

// The server's message, except that a bare status line from `api` reads as the stale-file text.
function fileWriteError(err) {
  if (err.status === 409) return err.message && !/^HTTP \d+$/.test(err.message) ? err.message : FILE_STALE;
  return "Could not save: " + err.message;
}

// One write: "saved", "stale" (a 409: the page shows what is on disk now) or "failed". The board refreshes after any of them.
async function writeBacklogFile(body, said) {
  let result = "saved";
  try {
    const reply = await api("/api/backlog/file", body);
    // what the server did besides the edit (a claim left or released when an item is reopened) goes in the same message, as text
    const notes = reply && Array.isArray(reply.notes) ? reply.notes.filter((n) => typeof n === "string" && n) : [];
    toast([said, ...notes].join(". "));
  } catch (err) {
    toast(fileWriteError(err));
    result = err.status === 409 ? "stale" : "failed";
  }
  refreshAfter();
  return result;
}

const moveItem = (card, up) => writeBacklogFile(moveBody(card, up, shaOf(card)), card.label + " moved " + (up ? "up" : "down"));

const addSpec = (path, sha) => writeBacklogFile(addFromSpecBody(path, sha), "Added " + path + " to backlog.yml as an idea");

const addIssue = (card) => writeBacklogFile(addIssueBody(card.id, shaOf(card)), "Added issue #" + card.id + " to backlog.yml");

// The Edit dialog's save: the changed keys against the item the dialog showed, with the sha from when it opened.
async function saveEdit(card, form, sha) {
  const wanted = editBody(card, form, sha);
  if (!Object.keys(wanted.fields).length) { toast("Nothing changed"); return true; }
  return (await writeBacklogFile(wanted, "Saved " + card.id + " in backlog.yml")) !== "failed";  // a stale file closes the dialog: edit again from what the page now shows
}

function fileButtons(card) {
  const button = (text, label, onclick) => el("button", { type: "button", text: text, "aria-label": label + " " + card.label,
    onclick: (ev) => { ev.stopPropagation(); onclick(); } });
  return [button("Edit", "Edit", () => editDialog(card)), button("Move up", "Move up", () => moveItem(card, true)),
    button("Move down", "Move down", () => moveItem(card, false))];
}

function editDialog(card) {
  const data = ui.board;
  const sha = shaOf(card);  // of the render that drew this card, whatever a refresh brings while the dialog is open
  const item = card.item;
  const control = (id, label, input) => { input.setAttribute("id", id); return [el("label", { for: id, text: label }), input]; };
  const lanes = Array.from(new Set([...(data.lanes || []), item.lane].filter(Boolean)));  // every lane of the pack, not only the New task form's
  const choose = (id, multi, choices, selected) => {
    const select = el("select", { id: id, multiple: multi ? "multiple" : null }, choices.map((c) => el("option", { value: c.key, text: c.label })));
    for (const option of Array.from(select.options)) option.selected = selected.includes(option.value);
    return select;
  };
  const deps = Array.from(new Set([...(data.modules || []).filter((m) => m !== card.id), ...(item.depends_on || [])]));
  const inputs = {
    title: el("input", { type: "text", maxlength: 200 }),
    lane: choose("ei-lane", false, [{ key: "", label: "(the module lane)" }, ...lanes.map((l) => ({ key: l, label: workLabel(l) }))], [item.lane || ""]),
    status: choose("ei-status", false, FILE_STATUSES.map((v) => ({ key: v, label: v })), [item.status]),
    depends_on: choose("ei-deps", true, deps.map((d) => ({ key: d, label: d })), item.depends_on || []),
    issue: el("input", { type: "text", maxlength: 12 }),
    spec: el("input", { type: "text", maxlength: 400 }),
    notes: el("textarea", { maxlength: 4000 }),
  };
  inputs.title.value = item.title || "";
  inputs.issue.value = item.issue === null || item.issue === undefined ? "" : String(item.issue);
  inputs.spec.value = item.spec || "";
  inputs.notes.value = item.notes || "";
  const body = [...control("ei-title", "Title", inputs.title), ...control("ei-lane", "Lane", inputs.lane), ...control("ei-status", "Status", inputs.status),
    ...control("ei-deps", "Depends on", inputs.depends_on), ...control("ei-issue", "Issue number", inputs.issue),
    ...control("ei-spec", "Spec", inputs.spec), ...control("ei-notes", "Notes", inputs.notes)];
  dialog("Edit " + card.id, body, async () => {
    const form = { title: inputs.title.value, lane: inputs.lane.value, status: inputs.status.value, issue: inputs.issue.value, spec: inputs.spec.value,
      notes: inputs.notes.value, depends_on: Array.from(inputs.depends_on.selectedOptions).map((o) => o.value) };
    return saveEdit(card, form, sha);
  });
}

// ------------------------------------------------------------------ New task

// A new module of a `backlog` pack goes to the file, ready to start, so the form leaves out "Stop after" and "Runs after".
const addsToFile = (data, name) => data.work_source_kind === "backlog" && (data.new_task[name] || []).includes("new_module");

// The form's fields for one kind of work, in order. Each input is also recorded in `inputs`, by its id.
function taskFormParts(data, name, inputs) {
  const wanted = data.new_task[name];
  const parts = [];
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
  if (wanted.includes("issue") && wanted.includes("new_module")) parts.push(el("div", { class: "sub", text: "Fill in the issue number, a new module, or both: an issue marks it on GitHub and links the pull request." }));
  if (wanted.includes("issue")) parts.push(...text("nt-issue", "Issue number", false, 12));
  if (wanted.includes("task")) parts.push(...text("nt-task", "Task (paste a log here if it helps)", true, 16000));
  if (wanted.includes("topic")) parts.push(...text("nt-topic", "Topic", false, 400));
  if (wanted.includes("new_module")) {
    parts.push(...text("nt-module", "Module id (lowercase, - or _)", false, 60));
    parts.push(...text("nt-resp", "What it does", "short", 400));
    parts.push(...pick("nt-deps", "Depends on", true, data.modules.map((m) => ({ key: m, label: m })), data.work_source_kind === "specs" ? "Only modules already in the spec files." : data.work_source_kind === "backlog" ? "Only items already in backlog.yml." : "Only modules already in the map."));
  }
  if (addsToFile(data, name)) return parts.filter(Boolean);
  const phases = data.lane_phases[name] || [];
  parts.push(...pick("nt-stop", "Stop after", false, [{ key: "", label: "(run to the end)" }, ...phases.map((p) => ({ key: p, label: p }))]));
  if (wanted.includes("after")) parts.push(...pick("nt-after", "Runs after (optional)", true, data.after_choices, "The card stays locked until each one is merged."));
  return parts.filter(Boolean);
}

function newTaskDialog() {
  const data = ui.board;
  const sha = fileSha();  // when the form opened: a refresh while it is open does not move it
  const kinds = Object.keys(data.new_task);
  if (!kinds.length) return;
  const radios = kinds.map((name, index) => el("input", { type: "radio", name: "nt-kind", value: name, checked: index === 0 ? "checked" : null }));
  const kind = () => radios.find((radio) => radio.checked).value;
  const fields = el("div", {});
  let inputs = {};
  const build = () => {
    inputs = {};
    const parts = taskFormParts(data, kind(), inputs);
    fields.replaceChildren(...parts.filter(Boolean));  // `replaceChildren` prints a null as "null"
  };
  for (const radio of radios) radio.addEventListener("change", build);
  build();
  const chosen = (id) => (inputs[id] ? Array.from(inputs[id].selectedOptions).map((o) => o.value) : []);
  const picker = el("fieldset", {}, el("legend", { text: "What kind of work?" }),
    kinds.map((name, index) => el("label", { class: "kind" }, radios[index], workLabel(name))));
  dialog("New task", [picker, fields], async () => {
    const wanted = data.new_task[kind()];
    if (addsToFile(data, kind()) && (inputs["nt-module"].value.trim() || inputs["nt-resp"].value.trim())) {
      const form = { module: inputs["nt-module"].value, responsibility: inputs["nt-resp"].value, deps: chosen("nt-deps"),
        issue: wanted.includes("issue") ? inputs["nt-issue"].value : "" };
      const result = await writeBacklogFile(newModuleBody(form, sha), "Added " + form.module.trim() + " to backlog.yml: press Start when it is ready");
      return result !== "failed";  // a stale file closes the dialog: add again from what the page now shows
    }
    const body = { lane: kind(), fields: {}, after: wanted.includes("after") ? chosen("nt-after") : [], to_phase: inputs["nt-stop"] ? inputs["nt-stop"].value || null : null };
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

function columnEntries(data, name, shownCards) {
  const cards = name === "Backlog" ? [] : shownCards.filter((c) => c.column === name);
  const claims = claimsIn(data, name);
  const count = (name === "Backlog" ? data.backlog.length : cards.length) + claims.length;
  const entries = [entry("col-head", { name: name, count: count }, (head) => el("h2", {}, el("span", { text: head.name }), el("span", { text: String(head.count) })))];
  if (name === "Backlog") {
    // A card carries the sha of this render, so a node drawn before the file changed is drawn again, never reused.
    entries.push(...data.backlog.map((item) => entry(item.key || item.id,
      Object.assign({}, item, { sha: data.backlog_sha256 || "" }, item.planning === "backlog" ? { armed: armedFor(item) } : {}), backlogNode)));
    entries.push(...claims.map(claimEntry));
    if (data.done_note) entries.push(entry("done-note", data.done_note, (note) => el("div", { class: "muted gate-bad", text: "Done marks not read: " + note })));
    if ((data.backlog_done || []).length) entries.push(entry("done", data.backlog_done, () => doneNode(data.backlog_done)));
    if ((data.backlog_dropped || []).length) entries.push(entry("dropped", data.backlog_dropped, droppedNode));
    if ((data.backlog_skipped || []).length) entries.push(entry("skipped", { names: data.backlog_skipped, sha: data.backlog_sha256 || "" }, (skipped) => skippedNode(skipped.names, data.work_source_kind, skipped.sha)));
  }
  if (name === "Done") {
    const page = doneCards(cards, ui.doneShown, ui.doneAll);
    entries.push(...page.cards.map((card) => entry(card.run_id, card, cardNode)));
    if (page.hidden > 0) entries.push(entry("done-more", { total: cards.length }, (more) => el("button", { type: "button", class: "done-more", text: "Show all (" + more.total + ")",
      onclick: () => { ui.doneAll = true; if (ui.board) renderBoard(ui.board); } })));
    else if (ui.doneAll && cards.length > ui.doneShown) entries.push(entry("done-less", { shown: ui.doneShown }, (less) => el("button", { type: "button", class: "done-more", text: "Show newest " + less.shown,
      onclick: () => { ui.doneAll = false; if (ui.board) renderBoard(ui.board); } })));
    return entries;
  }
  entries.push(...cards.map((card) => entry(card.run_id, card, cardNode)));
  if (name !== "Backlog") entries.push(...claims.map(claimEntry));  // Backlog drew its own, above its notes
  return entries;
}

// ------------------------------------------------------------------ the filter bar

// The bar is in the page (`#board-filters`) and is never rebuilt: only its choices of lane and status are patched, and
// a value is written to a control only when it differs, so the search box keeps its focus and caret across a refresh.

const boardLanes = (data) => Array.from(new Set([...Object.keys(data.lane_labels || {}), ...data.cards.map((c) => c.lane).filter(Boolean)]));
const boardStatuses = (data) => Array.from(new Set(data.cards.map((c) => c.status).filter(Boolean))).sort();

function setChoices(select, all, values, label) {
  const want = ["", ...values].join("\n");
  if (select.getAttribute("data-choices") !== want) {
    select.setAttribute("data-choices", want);
    select.replaceChildren(el("option", { value: "", text: all }), ...values.map((v) => el("option", { value: v, text: label(v) })));
  }
}

function syncBoardFilters(data) {
  const query = ui.boardQuery;
  setChoices($("bf-lane"), "Any lane", boardLanes(data), workLabel);
  setChoices($("bf-status"), "Any status", boardStatuses(data), (v) => v.replace(/_/g, " "));
  const text = (node, value) => { if (node.value !== value) node.value = value; };
  text($("bf-lane"), query.lane || "");
  text($("bf-status"), query.status || "");
  text($("bf-q"), query.q || "");
  $("bf-needs").checked = query.needs === true;
}

// The query is the URL's: `replaceState` rewrites it without a history entry per keystroke and fires no hashchange.
function setBoardQuery(patch) {
  ui.boardQuery = Object.assign({}, ui.boardQuery, patch);
  history.replaceState(null, "", routeHash({ view: "board", query: ui.boardQuery }));
  if (ui.board) renderBoard(ui.board);
}

function initBoardFilters() {
  $("bf-lane").addEventListener("change", (ev) => setBoardQuery({ lane: ev.target.value || null }));
  $("bf-status").addEventListener("change", (ev) => setBoardQuery({ status: ev.target.value || null }));
  $("bf-needs").addEventListener("change", (ev) => setBoardQuery({ needs: ev.target.checked }));
  $("bf-q").addEventListener("input", (ev) => setBoardQuery({ q: ev.target.value || null }));
}

function clearBoardFilters() { setBoardQuery({ lane: null, status: null, needs: false, q: null }); }

function emptyResultNote() {
  const box = $("board-empty");
  if (box.childElementCount === 0) {  // built once: a focused button survives the live refresh
    box.replaceChildren(el("p", { class: "muted", text: "No card matches these filters" }),
      el("button", { type: "button", text: "Clear filters", onclick: clearBoardFilters }));
  }
}

// Modules that are finished, with why. One line each and no Start button: the open state survives the SSE re-render.
function doneNode(done) {
  if (!done.length) return null;
  return el("details", { class: "done-modules", open: ui.doneOpen ? "true" : null, ontoggle: (ev) => { ui.doneOpen = ev.target.open; } },
    el("summary", { text: "Done (" + done.length + ")" }),
    done.map((item) => el("div", { class: "muted", text: item.id + " · " + item.reason })));
}

// Spec files no item names. A backlog pack calls them "Not on the backlog" and folds them away; other kinds say "Not modules".
function skippedNode(names, kind, sha) {
  if (kind !== "backlog") return el("div", { class: "muted", text: "Not modules: " + names.join(", ") });
  return el("details", { class: "done-modules", open: ui.skippedOpen ? "true" : null, ontoggle: (ev) => { ui.skippedOpen = ev.target.open; } },
    el("summary", { text: "Not on the backlog (" + names.length + ")" }),
    names.map((name) => el("div", { class: "muted", text: name },
      el("button", { type: "button", text: "Add", "aria-label": "Add " + name + " to the backlog", onclick: () => addSpec(name, sha) }))));
}

// Dropped backlog items: never cards. One line each (id and title as text), folded away; the open state survives the SSE re-render.
function droppedNode(dropped) {
  if (!dropped.length) return null;
  return el("details", { class: "done-modules", open: ui.droppedOpen ? "true" : null, ontoggle: (ev) => { ui.droppedOpen = ev.target.open; } },
    el("summary", { text: "Dropped (" + dropped.length + ")" }),
    dropped.map((item) => el("div", { class: "muted", text: item.id + (item.title ? " · " + item.title : "") })));
}

// The usage window as the header shows it. A pure function of `board().usage`: the pill (`text` empty hides it), its
// hover (`title`) and the note under it (the interactive-use warning, else a broken window file, else nothing).
const two = (n) => ("0" + n).slice(-2);
const clockText = (epoch) => { const d = new Date(epoch * 1000); return two(d.getHours()) + ":" + two(d.getMinutes()); };

function usageLabel(usage) {
  const names = { five_hour: "5h", seven_day: "7d" };
  const shown = Object.entries(names).filter(([key]) => usage.windows && usage.windows[key] && !usage.windows[key].stale);
  const mode = usage.mode === "observe" || usage.mode === "off" ? " (" + usage.mode + ")" : "";
  const text = shown.length ? shown.map(([key, name]) => name + " " + Math.round(usage.windows[key].utilization * 100) + "%").join(" · ") + mode : "";
  let title = shown.length ? "resets " + clockText(usage.windows[shown[0][0]].resets_at) : "";
  if (usage.waiting) title += " · " + usage.waiting + (usage.waiting === 1 ? " run waits" : " runs wait");
  const note = usage.note || (usage.error ? "usage window file unreadable: " + usage.error : "");
  return { text, level: usage.level, title, note };
}

function renderBoard(data) {
  ui.board = data;
  const counts = Object.entries(data.counts).map(([k, v]) => k.replace("_", " ") + " " + v).join(" · ") || "no runs";
  const byModel = Object.entries(data.agents_running.by_model).map(([k, v]) => k + " " + v).join(" · ");
  $("summary").textContent = counts + " | agents running " + data.agents_running.total + (byModel ? " (" + byModel + ")" : "") +
    " | today " + money(data.spend_today) + " (budget per run: soft " + money(data.run_budget_usd) + " · hard " + money(data.run_budget_hard_usd) + ")";
  const label = usageLabel(data.usage || { windows: {} });
  $("usage").hidden = !label.text;
  $("usage").textContent = label.text;
  $("usage").title = label.title;
  $("usage").className = "pill" + (label.level === "soft" || label.level === "hard" ? " " + label.level : "");
  $("usage-note").hidden = !label.note;
  $("usage-note").textContent = label.note;
  $("usage-note").title = label.note;
  syncBoardFilters(data);
  const shown = filterCards(data.cards, ui.boardQuery, boardLanes(data), boardStatuses(data));
  const noMatch = data.cards.length > 0 && shown.length === 0;
  $("columns").hidden = noMatch;
  $("board-empty").hidden = !noMatch;
  if (noMatch) emptyResultNote();
  const columns = data.columns.map((name) => entry(name, name, columnNode));
  $("new-task").hidden = !Object.keys(data.new_task).length;
  $("sync-note").hidden = !data.sync_note;
  $("sync-note").textContent = data.sync_note ? "Base branch not updated: " + data.sync_note : "";
  $("backlog-note").hidden = !data.backlog_note;
  $("backlog-note").textContent = data.backlog_note || "";
  showClaimsNote(data.claims_note);
  $("issues-note").hidden = !data.issues_note;
  $("issues-note").textContent = data.issues_note || "";
  $("alerts-note").hidden = !data.alerts_note;
  $("alerts-note").textContent = data.alerts_note || "";
  $("retro-due").hidden = !data.retro_due;
  $("retro-due").replaceChildren(...(data.retro_due ? [document.createTextNode("Retro due: " + data.retro_due + " → run "), el("code", { text: "agentflow retro" })] : []));
  if (!ui.dragging) {  // a patch mid-drag would lose the drop highlights; dragend draws what arrived
    patchChildren($("columns"), columns, entryKey, entryNode);
    for (const name of data.columns) patchChildren(childByKey($("columns"), name), columnEntries(data, name, shown), entryKey, entryNode);
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
