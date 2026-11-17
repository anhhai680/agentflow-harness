"use strict";

// The Inbox tab: every decision waiting on a person, as a card with its words, text, facts and forms. Every string
// from a run (titles, texts, facts, notes) goes in through el()'s textContent or the markdown renderer: never as markup.
// The pure helpers come first and touch no DOM and no other script at load time; the drawing follows.

const INBOX_ARM_MS = 4000;  // an Approve click (or an `a`) arms the card this long; a second one in that time sends. inboxKey checks the same 4000.
const INBOX_KINDS = [["approval", "Approvals"], ["question", "Questions"], ["stop", "Stops"]];
const INBOX_NONE = "No longer waiting on you";
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

// Items (oldest first, as given) as `[{key, label, items}]`. By run: one group per run, in the order of its oldest item.
// By kind: Approvals, Questions and Stops in that order, an empty one dropped.
function groupItems(items, by) {
  const groups = [];
  if (by === "kind") {
    const known = INBOX_KINDS.map((k) => k[0]);
    for (const [key, label] of INBOX_KINDS) {
      const own = items.filter((i) => (known.includes(i.group) ? i.group : "stop") === key);
      if (own.length) groups.push({ key: key, label: label, items: own });
    }
    return groups;
  }
  const byRun = new Map();
  for (const item of items) {
    let group = byRun.get(item.run_id);
    if (!group) {
      group = { key: item.run_id, label: item.run_label || item.run_id, items: [] };
      byRun.set(item.run_id, group);
      groups.push(group);
    }
    group.items.push(item);
  }
  return groups;
}

const inboxTwo = (n) => (n < 10 ? "0" : "") + n;

// What became of an item that is no longer in the Inbox: the newest `recent` entry of its run with its key (or, for a phase
// approval, its phase), as "Approved from the CLI by alex at 14:02". No entry reads INBOX_NONE. The time is the reader's local one.
function goneText(item, recent) {
  const phaseApproval = item.kind === "phase_approval";
  let best = null;
  let bestAt = -Infinity;
  for (const r of recent || []) {
    if (r.run_id !== item.run_id) continue;
    const same = phaseApproval ? r.phase === item.phase && (r.key === null || r.key === undefined || r.key === item.phase) : r.key === item.key;
    if (!same) continue;
    const at = Date.parse(r.ts || "");
    const rank = Number.isNaN(at) ? -Infinity : at;
    if (best === null || rank > bestAt) { best = r; bestAt = rank; }
  }
  if (best === null) return INBOX_NONE;
  const verb = { approved: "Approved", answered: "Answered", rejected: "Rejected" }[best.action];
  if (!verb) return INBOX_NONE;
  const where = best.by === "dashboard" ? "from the dashboard" : best.by === "runner" ? "by the runner" : "from the CLI" + (best.by ? " by " + best.by : "");
  const when = Number.isNaN(Date.parse(best.ts || "")) ? null : new Date(best.ts);
  const at = when ? " at " + inboxTwo(when.getHours()) + ":" + inboxTwo(when.getMinutes()) : "";
  if (best.by === "questions.md") return "Answered in questions.md" + at;  // the answers were filled into the file
  return verb + " " + where + at;
}

// Whether `live` is a new raise under the key of the kept card `kept` (its `since` differs), so what was typed on the
// kept card does not carry over to it.
function inboxReturned(kept, live) {
  return !!kept && !!live && kept.since !== live.since;
}

// Whether a reader wrote anything on the card: a note, a request for changes, or an answer of their own words.
function inboxHasText(draft) {
  if (!draft) return false;
  const wrote = (t) => String(t || "").trim() !== "";
  return wrote(draft.note) || wrote(draft.reject && draft.reject.note) || Object.values(draft.answers || {}).some((a) => wrote(a && a.other));
}

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
  ui.inbox.armedAt = Date.now();
  inboxArmTimer = setTimeout(disarmInbox, INBOX_ARM_MS);
  redrawInbox();
  return false;
}

// Whether a sent action's draft is forgotten once it succeeded. An answer is kept: `/answer` also succeeds when a question
// is still blank (`complete: false`), and the card rebuilt from the changed inbox text must still hold what was chosen
// and written, or the next Answer would post the agent's recommendation over it. A completed answer leaves the view,
// and renderInbox drops the draft of an item no longer in it.
const inboxForgets = (action) => action !== "answer";

// Run a decision's action once at a time. A success forgets the draft (see inboxForgets); the refresh that follows drops the card.
async function inboxSend(item, action, body) {
  const key = inboxItemKey(item);
  if (ui.inbox.busy.has(key)) return;
  ui.inbox.busy.add(key);
  try {
    if (await act(item.run_id, action, body)) {
      if (inboxForgets(action)) ui.inbox.drafts.delete(key);
      else ui.inbox.sent.add(key);  // when the answered item leaves the view its card goes with it: this page decided it
    }
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

// Drop a kept card and what was typed on it.
function dismissItem(item) {
  const key = inboxItemKey(item);
  ui.inbox.gone.delete(key);
  ui.inbox.drafts.delete(key);
  ui.inbox.sent.delete(key);
  redrawInbox();
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

function inboxQuestion(item, q, draft, locked) {
  const answer = draft.answers[q.id] || (draft.answers[q.id] = { choice: "", other: "" });
  const name = "ans-" + inboxId(item) + "-" + q.id;
  const options = (q.options || []).map((o) => {
    const radio = el("input", { type: "radio", name: name, value: o.key, checked: answer.choice === o.key ? "checked" : null, disabled: locked ? "disabled" : null,
      "data-field": "q:" + q.id + ":" + o.key });
    radio.addEventListener("change", () => { if (radio.checked) answer.choice = o.key; });
    return el("label", { class: "i-opt" }, radio, el("span", { text: o.label + (o.recommended ? " (recommended)" : "") }));
  });
  const other = el("input", { type: "text", placeholder: "Other: write your own answer", "aria-label": "Other answer for " + q.id,
    "data-field": "other:" + q.id });
  other.value = answer.other;
  other.readOnly = locked;  // a kept card's text stays selectable and copyable
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

function inboxNote(item, draft, locked) {
  const required = item.approve_note === "required";
  const area = el("textarea", { class: "short", "data-field": "note", "aria-required": required ? "true" : null,
    "aria-label": "Note to the agent" });
  area.value = draft.note;
  area.readOnly = locked;
  area.addEventListener("input", () => { draft.note = area.value; });
  return el("div", { class: "i-form" },
    el("div", { class: "i-label", text: "Note to the agent (what changed or what to do)" + (required ? " (required)" : "") }), area);
}

function inboxRejectBox(item, draft, locked) {
  const area = el("textarea", { class: "short", "data-field": "reject", "aria-label": "What should change",
    placeholder: "What should change? The phase runs again with this note." });
  area.value = draft.reject.note;
  area.readOnly = locked;
  area.addEventListener("input", () => { draft.reject.note = area.value; });
  return el("div", { class: "i-form" }, area,
    locked ? null : el("div", { class: "moves" }, el("button", { type: "button", text: "Send request", "data-field": "reject-send", onclick: () => rejectItem(item) })));
}

// `item.goneText`, set on an item that left the Inbox while the reader typed on it, makes a kept card: the same words
// and the typed text, in read-only fields, with the line saying what became of it and a Dismiss in place of its buttons.
function inboxCard(item) {
  const key = inboxItemKey(item);
  const draft = inboxDraft(item);
  const gone = typeof item.goneText === "string";
  const actions = item.actions || [];
  const questions = item.questions || [];
  const armed = !gone && ui.inbox.armed === key;
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
  if (actions.includes("approve") && item.approve_note !== "none" && (!gone || draft.note.trim())) forms.push(inboxNote(item, draft, gone));
  if (actions.includes("answer")) for (const q of questions) forms.push(inboxQuestion(item, q, draft, gone));
  if (actions.includes("reject") && (gone ? draft.reject.note.trim() : draft.reject.open)) forms.push(inboxRejectBox(item, draft, gone));
  const card = (extra, ...parts) => el("article", { class: "icard " + (item.group || "stop") + extra, onclick: () => focusInboxCard(key) }, ...parts);
  if (gone) {
    return card(" gone", head, el("div", { class: "i-gone", role: "status", text: item.goneText }), body, forms,
      el("div", { class: "moves" }, el("button", { type: "button", text: "Dismiss", "data-field": "dismiss", onclick: () => dismissItem(item) })));
  }
  const buttons = [];
  if (actions.includes("approve")) {
    const label = armed ? "Confirm approve" : item.kind === "phase_approval" ? "Approve" : "Approve and resume";
    buttons.push(el("button", { type: "button", class: "primary", text: label, "data-field": "approve", onclick: () => approveItem(item) }));
  }
  if (actions.includes("answer")) buttons.push(el("button", { type: "button", class: "primary", text: "Answer", "data-field": "answer", onclick: () => answerItem(item) }));
  if (actions.includes("reject")) buttons.push(el("button", { type: "button", text: "Request changes…", "data-field": "reject-toggle", "aria-pressed": draft.reject.open ? "true" : "false",
    onclick: () => toggleReject(item) }));
  buttons.push(el("button", { type: "button", text: "Open run", onclick: () => goRun(item.run_id, "overview", "decision") }));
  return card(armed ? " armed" : "", head, body, forms, draft.error ? el("div", { class: "gate-bad", role: "alert", text: draft.error }) : null,
    armed ? el("div", { class: "i-armed", role: "status", text: "Press a again to approve" }) : null,
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

// What the tab shows: the view's items with the kept cards put back where they were, grouped as the reader chose.
// `flat` is the cards in the order they are drawn, which is the order j and k walk.
function inboxShown(view) {
  const items = view.items.slice();
  const kept = Array.from(ui.inbox.gone.values()).sort((a, b) => a.at - b.at);
  for (const g of kept) items.splice(Math.min(g.at, items.length), 0, Object.assign({}, g.item, { goneText: g.text }));
  const groups = groupItems(items, ui.inbox.group);
  return { groups: groups, flat: groups.flatMap((g) => g.items) };
}

// The items of the view as patchChildren entries: a heading and the cards of each group, then the Watch group.
function inboxEntries(view, shown) {
  const entries = [];
  for (const group of shown.groups) {
    entries.push(entry("group:" + ui.inbox.group + ":" + group.key, group.label, (label) => el("h2", { class: "lane", text: label })));
    for (const item of group.items) {
      const key = inboxItemKey(item);
      const draft = inboxDraft(item);
      // What the card shows but the item does not hold: typed text is not here, a patch rebuilds with it from the draft.
      const card = Object.assign({}, item, { age_s: Math.floor((item.age_s || 0) / 60) * 60 });  // the card reads minutes: it is rebuilt once a minute, not on every refresh
      entries.push(entry("item:" + key, { item: card, armed: ui.inbox.armed === key, error: draft.error, rejecting: draft.reject.open }, (d) => inboxCard(d.item)));
    }
  }
  if (!shown.flat.length) entries.push(entry("empty", "empty", () => el("p", { class: "muted", text: "Nothing is waiting on you." })));
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

// An item that left the view while a reader had typed on it stays as a kept card, in the place it had. A card this page
// itself answered goes with its item, and so does one nobody wrote on.
function inboxKeepGone(view, prev) {
  const live = new Set(view.items.map(inboxItemKey));
  if (prev) {
    prev.items.forEach((item, at) => {
      const key = inboxItemKey(item);
      if (live.has(key) || ui.inbox.gone.has(key)) return;
      if (ui.inbox.sent.delete(key)) return;
      if (inboxHasText(ui.inbox.drafts.get(key))) ui.inbox.gone.set(key, { item: item, at: at, text: INBOX_NONE });
    });
  }
  for (const [key, g] of Array.from(ui.inbox.gone.entries())) {
    if (live.has(key)) {  // it came back: the live card takes over, with a fresh draft when it is a new raise
      if (inboxReturned(g.item, view.items.find((i) => inboxItemKey(i) === key))) { ui.inbox.drafts.delete(key); ui.inbox.sent.delete(key); }
      ui.inbox.gone.delete(key);
      continue;
    }
    const text = goneText(g.item, view.recent);
    if (text !== INBOX_NONE || g.text === INBOX_NONE) g.text = text;  // what once said who decided is not forgotten when `recent` moves on
  }
  for (const key of Array.from(ui.inbox.drafts.keys())) if (!live.has(key) && !ui.inbox.gone.has(key)) ui.inbox.drafts.delete(key);  // decided elsewhere, nothing typed
  for (const key of Array.from(ui.inbox.sent)) if (!live.has(key)) ui.inbox.sent.delete(key);
  return live;
}

// The focused card is the one under `focusKey`; when it is gone the same place is kept.
function inboxSettleFocus(flat) {
  const at = flat.findIndex((i) => inboxItemKey(i) === ui.inbox.focusKey);
  ui.inbox.focus = at >= 0 ? at : Math.max(0, Math.min(ui.inbox.focus, flat.length - 1));
  ui.inbox.focusKey = flat.length ? inboxItemKey(flat[ui.inbox.focus]) : null;
}

// The ring is a class on the node, not part of the card's sig: moving it rebuilds nothing.
function markInboxFocus() {
  const want = ui.inbox.focusKey === null ? null : "item:" + ui.inbox.focusKey;
  for (const node of Array.from($("inbox-list").children)) node.classList.toggle("focused", want !== null && node.getAttribute("data-key") === want);
}

function setInboxGroupButtons() {
  for (const by of ["run", "kind"]) {
    const button = $("inbox-group-" + by);
    if (button) button.setAttribute("aria-pressed", String(ui.inbox.group === by));
  }
}

function setInboxGroup(by) {
  ui.inbox.group = by;
  setInboxGroupButtons();
  redrawInbox();
}

function focusInboxCard(key) {
  ui.inbox.focusKey = key;
  const at = ui.inbox.order.findIndex((i) => inboxItemKey(i) === key);
  if (at >= 0) ui.inbox.focus = at;
  markInboxFocus();
}

function renderInbox(view) {
  const prev = ui.inbox.view;
  ui.inbox.view = view;
  setInboxBadge(view.items.length);
  const live = inboxKeepGone(view, prev);
  if (ui.inbox.armed !== null && !live.has(ui.inbox.armed)) ui.inbox.armed = null;
  const shown = inboxShown(view);
  ui.inbox.order = shown.flat;
  inboxSettleFocus(shown.flat);
  setInboxGroupButtons();
  const saved = inboxFocus();
  patchChildren($("inbox-list"), inboxEntries(view, shown), entryKey, entryNode);
  for (const fold of $("inbox-list").querySelectorAll("details")) if (typeof fold.fill === "function") fold.fill();
  inboxRefocus(saved);
  markInboxFocus();
}

// ------------------------------------------------------------------ the keyboard

// The shortcut keys work while the Inbox is shown: main.js binds this on `document` for that time only. They never act
// inside a field, a dialog or with a modifier held, and `a` approves only on its second press within 4 s.
function inboxKey(ev) {
  if (ui.view !== "inbox") return;
  const target = ev.target;
  const tag = target && target.tagName;
  if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || (target && target.isContentEditable)) return;
  if ($("dlg").open) return;
  if (ev.ctrlKey || ev.metaKey || ev.altKey || ev.shiftKey) return;
  if ((ev.key === "Enter" || ev.key === " ") && (tag === "BUTTON" || tag === "A" || tag === "SUMMARY")) return;  // that press is a click on a control
  if (ev.key === "a" && ev.repeat) return;  // a held `a` is not a second press
  if (ui.inbox.armed !== null && (ev.key !== "a" || Date.now() - ui.inbox.armedAt >= 4000)) disarmInbox();  // any other key, or the time passing
  const card = ui.inbox.order[ui.inbox.focus];
  const item = card && !card.goneText ? ui.inbox.view.items.find((i) => inboxItemKey(i) === inboxItemKey(card)) : null;
  if (ev.key === "j" || ev.key === "k") {
    if (!ui.inbox.order.length) return;
    ev.preventDefault();
    const to = Math.max(0, Math.min(ui.inbox.order.length - 1, ui.inbox.focus + (ev.key === "j" ? 1 : -1)));
    focusInboxCard(inboxItemKey(ui.inbox.order[to]));
    const node = childByKey($("inbox-list"), "item:" + ui.inbox.focusKey);
    if (node && typeof node.scrollIntoView === "function") node.scrollIntoView({ block: "nearest" });
  } else if (ev.key === "o" && card) {
    ev.preventDefault();
    goRun(card.run_id, "overview", "decision");
  } else if (ev.key === "r" && item && (item.actions || []).includes("reject")) {
    ev.preventDefault();  // or the `r` would be typed into the box this opens
    inboxDraft(item).reject.open = true;
    inboxDraft(item).error = "";
    redrawInbox();
    const node = childByKey($("inbox-list"), "item:" + inboxItemKey(item));
    const box = node && node.querySelector('[data-field="reject"]');
    if (box) box.focus();
  } else if (ev.key === "a" && item && (item.actions || []).includes("approve")) {
    ev.preventDefault();
    approveItem(item);
  }
}

let inboxKeysBound = false;

// Bind the shortcuts while the Inbox is shown and unbind them when it is not (a second bind or unbind does nothing).
function setInboxKeys(on) {
  if (on === inboxKeysBound) return;
  inboxKeysBound = on;
  if (on) document.addEventListener("keydown", inboxKey);
  else { document.removeEventListener("keydown", inboxKey); disarmInbox(); }
}

function initInbox() {
  $("inbox-group-run").addEventListener("click", () => setInboxGroup("run"));
  $("inbox-group-kind").addEventListener("click", () => setInboxGroup("kind"));
  setInboxGroupButtons();
}

async function loadInbox() {
  renderInbox(await api("/api/inbox"));
}
