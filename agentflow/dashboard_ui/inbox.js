"use strict";

// The Inbox tab: every decision waiting on a person, as a card with its words, text, facts and forms. Every string
// from a run (titles, texts, facts, notes) goes in through el()'s textContent or the markdown renderer: never as markup.
// The pure helpers come first and touch no DOM and no other script at load time; the drawing follows.

const INBOX_ARM_MS = 4000;  // a click on Approve arms the button this long; a second click in that time sends
const INBOX_SEVERITIES = ["critical", "high", "medium", "low", "unrated"];

// ------------------------------------------------------------------ pure helpers

const inboxItemKey = (item) => item.run_id + "\0" + item.key;

// What a decision's target is: the phase for a phase approval, else the item's key.
const inboxTarget = (item) => (item.kind === "phase_approval" ? item.phase : item.key);

// Each question starts on the option the agent recommended ("" when it recommended none).
function answerDefaults(questions) {
  const out = {};
  for (const q of questions || []) {
    const pick = (q.options || []).find((o) => o.recommended);
    out[q.id] = pick ? pick.key : "";
  }
  return out;
}

// `form` is `{note}`. A blocked stop that repeated needs a note, a phase approval takes none, and any other note is sent
// only when written. A phase approval carries what this page showed of the spec and plan (informational).
function approvePayload(item, form) {
  const note = String((form && form.note) || "").trim();
  if (item.approve_note === "required" && !note) {
    return { ok: false, error: "This stop blocked again with nothing changed: say what changed or what to do." };
  }
  const body = { target: inboxTarget(item) };
  if (item.approve_note !== "none" && note) body.note = note;
  if (item.kind === "phase_approval") {
    const viewed = typeof viewedList === "function" ? viewedList(item.run_id) : [];
    if (viewed.length) body.viewed = viewed;
  }
  return { ok: true, body: body };
}

// `form` is `{[qid]: {choice, other}}`. A written answer beats the chosen option; a question with neither is left out.
function answerPayload(item, form) {
  const answers = {};
  for (const [qid, a] of Object.entries(form || {})) {
    const other = String((a && a.other) || "").trim();
    const choice = String((a && a.choice) || "");
    if (other) answers[qid] = other;
    else if (choice) answers[qid] = choice;
  }
  if (!Object.keys(answers).length) return { ok: false, error: "Choose or write at least one answer." };
  return { ok: true, body: { key: item.key, answers: answers } };
}

function rejectPayload(item, note) {
  const text = String(note || "").trim();
  if (!text) return { ok: false, error: "A rejection needs a note." };
  return { ok: true, body: { target: inboxTarget(item), note: text } };
}

// "2 high, 1 medium": the open findings by severity, the worst first.
function findingsText(findings) {
  const names = Object.keys(findings || {}).filter((k) => findings[k] > 0);
  names.sort((a, b) => {
    const rank = (k) => (INBOX_SEVERITIES.includes(k) ? INBOX_SEVERITIES.indexOf(k) : INBOX_SEVERITIES.length);
    return rank(a) - rank(b) || (a < b ? -1 : 1);
  });
  return names.map((k) => findings[k] + " " + k).join(", ");
}

const inboxPlural = (n, one, many) => n + " " + (n === 1 ? one : many);

// ------------------------------------------------------------------ state: drafts and the armed Approve

// What a reader typed lives here, outside the nodes: a patch rebuilds a card with it.
function inboxDraft(item) {
  const key = inboxItemKey(item);
  let draft = ui.inbox.drafts.get(key);
  if (!draft) {
    const defaults = answerDefaults(item.questions);
    const answers = {};
    for (const q of item.questions || []) answers[q.id] = { choice: defaults[q.id], other: "" };
    draft = { note: "", answers: answers, reject: { open: false, note: "" }, error: "" };
    ui.inbox.drafts.set(key, draft);
  }
  return draft;
}

let inboxArmTimer = null;
let inboxSeq = 0;

function redrawInbox() { if (ui.inbox.view) renderInbox(ui.inbox.view); }

function disarmInbox() {
  clearTimeout(inboxArmTimer);
  inboxArmTimer = null;
  if (ui.inbox.armed !== null) { ui.inbox.armed = null; redrawInbox(); }
}

// The first call arms `key` for INBOX_ARM_MS; a second call for the same key in that time runs `send`. Returns whether it sent.
function armOrSend(key, send) {
  if (ui.inbox.armed === key) {
    clearTimeout(inboxArmTimer);
    inboxArmTimer = null;
    ui.inbox.armed = null;
    send();
    return true;
  }
  clearTimeout(inboxArmTimer);
  ui.inbox.armed = key;
  inboxArmTimer = setTimeout(disarmInbox, INBOX_ARM_MS);
  redrawInbox();
  return false;
}

// Run a decision's action once at a time. A success forgets the draft; the refresh that follows drops the card.
async function inboxSend(item, action, body) {
  const key = inboxItemKey(item);
  if (ui.inbox.busy.has(key)) return;
  ui.inbox.busy.add(key);
  try {
    if (await act(item.run_id, action, body)) ui.inbox.drafts.delete(key);
  } finally {
    ui.inbox.busy.delete(key);
    redrawInbox();
  }
}

// A refused payload shows its error on the card and sends nothing.
function inboxRefuse(item, error) {
  inboxDraft(item).error = error;
  redrawInbox();
}

// The Approve click, shared with the keyboard: check the payload, arm the button, send on the second click.
function approveItem(item) {
  const draft = inboxDraft(item);
  const got = approvePayload(item, { note: draft.note });
  if (!got.ok) { disarmInbox(); inboxRefuse(item, got.error); return; }
  draft.error = "";
  armOrSend(inboxItemKey(item), () => {
    const again = approvePayload(item, { note: inboxDraft(item).note });  // the note may have changed after arming
    if (again.ok) inboxSend(item, "approve", again.body);
    else inboxRefuse(item, again.error);
  });
}

function answerItem(item) {
  const got = answerPayload(item, inboxDraft(item).answers);
  if (!got.ok) { inboxRefuse(item, got.error); return; }
  inboxDraft(item).error = "";
  inboxSend(item, "answer", got.body);
}

function rejectItem(item) {
  const got = rejectPayload(item, inboxDraft(item).reject.note);
  if (!got.ok) { inboxRefuse(item, got.error); return; }
  inboxDraft(item).error = "";
  inboxSend(item, "reject", got.body);
}

function toggleReject(item) {
  const draft = inboxDraft(item);
  draft.reject.open = !draft.reject.open;
  draft.error = "";
  redrawInbox();
}

// ------------------------------------------------------------------ one card

// A section the reader can fold. Its body is built on the first open, so a long text costs nothing while closed.
function inboxFold(key, title, build) {
  let filled = false;
  const body = el("div", { class: "fold" });
  const node = el("details", { "data-key": key }, el("summary", { text: title }), body);
  node.fill = () => {  // also called after a patch: a rebuilt section is set open before it is in the page
    if (!node.open || filled) return;
    filled = true;
    body.append(...build());
  };
  node.addEventListener("toggle", node.fill);
  return node;
}

function inboxTextNodes(item) {
  const nodes = [el("div", { class: "md" }, markdownNodes(parseMarkdown(item.text)))];
  if (item.text_truncated) {
    const kb = Math.max(1, Math.round(item.text.length / 1024));
    nodes.push(el("div", { class: "muted", text: "Showing the first " + kb + " KB" }));
  }
  return nodes;
}

function inboxArtifacts(item) {
  const rows = (item.artifacts || []).map((a) => {
    const count = (a.revisions || []).length;
    return el("div", { class: "i-art" },
      el("span", { text: a.path + " · " + inboxPlural(count, "revision", "revisions") }),
      isArtifactTab(a.kind)
        ? el("button", { type: "button", text: "Read the " + a.kind, onclick: () => goRun(item.run_id, a.kind, a.path) })
        : null);
  });
  if (item.kind === "phase_approval") {
    const found = findingsText(item.findings);
    rows.push(el("div", { class: "sub", text: found ? "Open findings: " + found : "No open findings." }));
  }
  return rows;
}

function inboxBlast(blast) {
  return "blocks: " + (ranges(blast.blocks) || "none") + " · independent: " + (ranges(blast.independent) || "none");
}

function inboxQuestion(item, q, draft) {
  const answer = draft.answers[q.id] || (draft.answers[q.id] = { choice: "", other: "" });
  const name = "ans-" + inboxId(item) + "-" + q.id;
  const options = (q.options || []).map((o) => {
    const radio = el("input", { type: "radio", name: name, value: o.key, checked: answer.choice === o.key ? "checked" : null,
      "data-field": "q:" + q.id + ":" + o.key });
    radio.addEventListener("change", () => { if (radio.checked) answer.choice = o.key; });
    return el("label", { class: "i-opt" }, radio, el("span", { text: o.label + (o.recommended ? " (recommended)" : "") }));
  });
  const other = el("input", { type: "text", placeholder: "Other: write your own answer", "aria-label": "Other answer for " + q.id,
    "data-field": "other:" + q.id });
  other.value = answer.other;
  other.addEventListener("input", () => { answer.other = other.value; });
  return el("fieldset", { class: "i-question" },
    el("legend", { text: q.id + ": " + q.question }),
    q.evidence && q.evidence.length ? el("div", { class: "sub", text: "see " + q.evidence.join(", ") }) : null,
    options, el("div", { class: "i-opt" }, other));
}

function inboxId(item) {
  const key = inboxItemKey(item);
  if (!ui.inbox.ids.has(key)) ui.inbox.ids.set(key, ++inboxSeq);
  return ui.inbox.ids.get(key);
}

function inboxNote(item, draft) {
  const required = item.approve_note === "required";
  const area = el("textarea", { class: "short", "data-field": "note", "aria-required": required ? "true" : null,
    "aria-label": "Note to the agent" });
  area.value = draft.note;
  area.addEventListener("input", () => { draft.note = area.value; });
  return el("div", { class: "i-form" },
    el("div", { class: "i-label", text: "Note to the agent (what changed or what to do)" + (required ? " (required)" : "") }), area);
}

function inboxRejectBox(item, draft) {
  const area = el("textarea", { class: "short", "data-field": "reject", "aria-label": "What should change",
    placeholder: "What should change? The phase runs again with this note." });
  area.value = draft.reject.note;
  area.addEventListener("input", () => { draft.reject.note = area.value; });
  return el("div", { class: "i-form" }, area,
    el("div", { class: "moves" }, el("button", { type: "button", text: "Send request", "data-field": "reject-send", onclick: () => rejectItem(item) })));
}

function inboxCard(item) {
  const key = inboxItemKey(item);
  const draft = inboxDraft(item);
  const actions = item.actions || [];
  const questions = item.questions || [];
  const armed = ui.inbox.armed === key;
  const blast = item.blast_radius;
  const blocks = blast && (blast.blocks || []).length ? inboxPlural(blast.blocks.length, "task", "tasks") : "";
  const head = el("div", { class: "i-head" },
    el("h3", { class: "i-title", text: item.title }),
    el("div", { class: "sub", text: [item.run_label, item.phase, ago(item.age_s)].filter((p) => p).join(" · ") }),
    blocks ? el("span", { class: "badge", text: "blocks " + blocks }) : null);
  const facts = item.facts || [];
  const body = el("div", { class: "i-body" },
    item.summary ? el("div", { class: "i-summary", text: item.summary }) : null,
    item.text ? inboxFold("text", "Full text", () => inboxTextNodes(item)) : null,
    (item.cli || []).length ? inboxFold("cli", "CLI equivalent", () => item.cli.map((c) => el("pre", { text: c }))) : null,
    facts.length ? inboxFold("facts", "Facts the resolvers found (" + facts.length + ")",
      () => [el("ul", {}, facts.map((f) => el("li", { text: typeof f === "string" ? f : String((f && f.text) || "") })))]) : null,
    inboxArtifacts(item),
    blast ? el("div", { class: "sub", text: inboxBlast(blast) }) : null);
  const forms = [];
  if (actions.includes("approve") && item.approve_note !== "none") forms.push(inboxNote(item, draft));
  if (actions.includes("answer")) for (const q of questions) forms.push(inboxQuestion(item, q, draft));
  if (actions.includes("reject") && draft.reject.open) forms.push(inboxRejectBox(item, draft));
  const buttons = [];
  if (actions.includes("approve")) {
    const label = armed ? "Confirm approve" : item.kind === "phase_approval" ? "Approve" : "Approve and resume";
    buttons.push(el("button", { type: "button", class: "primary", text: label, "data-field": "approve", onclick: () => approveItem(item) }));
  }
  if (actions.includes("answer")) buttons.push(el("button", { type: "button", class: "primary", text: "Answer", "data-field": "answer", onclick: () => answerItem(item) }));
  if (actions.includes("reject")) buttons.push(el("button", { type: "button", text: "Request changes…", "data-field": "reject-toggle", "aria-pressed": draft.reject.open ? "true" : "false",
    onclick: () => toggleReject(item) }));
  buttons.push(el("button", { type: "button", text: "Open run", onclick: () => goRun(item.run_id, "overview", "decision") }));
  return el("article", { class: "icard " + (item.group || "stop") + (armed ? " armed" : "") },
    head, body, forms, draft.error ? el("div", { class: "gate-bad", role: "alert", text: draft.error }) : null,
    el("div", { class: "moves" }, buttons));
}

// One muted row for a run that waits on a limit or lost its runner: nothing to decide, so no buttons.
function inboxWatchRow(w) {
  const since = Date.parse(w.since || "");
  const when = Number.isNaN(since) ? "" : " · " + ago(Math.max(0, (Date.now() - since) / 1000));
  return el("div", { class: "muted i-watch" },
    el("a", { href: "#/run/" + encodeURIComponent(w.run_id) + "/overview", text: w.run_label || w.run_id }),
    " · " + w.reason + (w.text ? " · " + w.text : "") + when);
}

// ------------------------------------------------------------------ the tab

// The items of the view as patchChildren entries: the cards, then the Watch group.
function inboxEntries(view) {
  const entries = view.items.map((item) => {
    const key = inboxItemKey(item);
    const draft = inboxDraft(item);
    // What the card shows but the item does not hold: typed text is not here, a patch rebuilds with it from the draft.
    const shown = Object.assign({}, item, { age_s: Math.floor((item.age_s || 0) / 60) * 60 });  // the card reads minutes: it is rebuilt once a minute, not on every refresh
    return entry("item:" + key, { item: shown, armed: ui.inbox.armed === key, error: draft.error, rejecting: draft.reject.open }, (d) => inboxCard(d.item));
  });
  if (!view.items.length) entries.push(entry("empty", "empty", () => el("p", { class: "muted", text: "Nothing is waiting on you." })));
  if ((view.watch || []).length) {
    entries.push(entry("watch-head", "Watch", () => el("h2", { class: "lane", text: "Watch" })));
    entries.push(...view.watch.map((w) => entry("watch:" + w.run_id, w, inboxWatchRow)));
  }
  return entries;
}

function setInboxBadge(count) {
  const badge = $("inbox-count");
  if (badge) badge.textContent = count > 0 ? "(" + count + ")" : "";
}

// The field the reader is in, so a rebuilt card gives it back: `[card key, field name, selection]`.
function inboxFocus() {
  const active = document.activeElement;
  if (!active || !$("inbox-list").contains(active)) return null;
  const card = active.closest("[data-key]");
  const field = active.getAttribute("data-field");
  if (!card || !field) return null;
  return [card.getAttribute("data-key"), field, active.selectionStart, active.selectionEnd];
}

function inboxRefocus(saved) {
  if (!saved || document.activeElement !== document.body) return;
  const card = childByKey($("inbox-list"), saved[0]);
  if (!card) return;
  const found = Array.from(card.querySelectorAll("[data-field]")).find((n) => n.getAttribute("data-field") === saved[1]);
  if (!found) return;
  found.focus({ preventScroll: true });
  if (typeof saved[2] === "number" && typeof found.setSelectionRange === "function") {
    try { found.setSelectionRange(saved[2], saved[3]); } catch (err) { /* a radio has no selection */ }
  }
}

function renderInbox(view) {
  ui.inbox.view = view;
  setInboxBadge(view.items.length);
  const live = new Set(view.items.map(inboxItemKey));
  for (const key of Array.from(ui.inbox.drafts.keys())) if (!live.has(key)) ui.inbox.drafts.delete(key);  // decided elsewhere
  if (ui.inbox.armed !== null && !live.has(ui.inbox.armed)) ui.inbox.armed = null;
  const saved = inboxFocus();
  patchChildren($("inbox-list"), inboxEntries(view), entryKey, entryNode);
  for (const fold of $("inbox-list").querySelectorAll("details")) if (typeof fold.fill === "function") fold.fill();
  inboxRefocus(saved);
}

async function loadInbox() {
  renderInbox(await api("/api/inbox"));
}
