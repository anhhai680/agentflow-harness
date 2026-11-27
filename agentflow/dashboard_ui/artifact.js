"use strict";

// ------------------------------------------------------------------ the Spec and Plan tabs

// A run's spec and plan, as the run's own commits made them: every revision a phase committed, rendered or raw, a diff
// with the previous one, and the review findings beside it. The text of a `(path, sha)` never changes, so it is
// fetched once. Everything from the server reaches the page as text (`el`'s `text`, markdown.js's text nodes).

const SEVERITY_RANK = ["critical", "high", "medium", "low"];
const ARTIFACT_KINDS = ["spec", "plan"];
const isArtifactTab = (tab) => ARTIFACT_KINDS.includes(tab);
const HEAD_SHA = "head";
const NOTE_MAX = 16384;  // equal to dashboard.TEXT_MAX: the longest note the server takes
const VIEWED_CAP = 100;  // dashboard.VIEWED_MAX

// ---- pure helpers: no DOM, so a test runs them under node

// The entry `path` names when it is of `kind`; else, of that kind, `.md` first, then most revisions, then by path.
function pickArtifact(list, kind, path) {
  const mine = (list || []).filter((a) => a.kind === kind);
  if (path) {
    const named = mine.find((a) => a.path === path);
    if (named) return named;
  }
  const md = (a) => (/\.md$/i.test(a.path) ? 0 : 1);
  const revs = (a) => (a.revisions || []).length;
  mine.sort((a, b) => md(a) - md(b) || revs(b) - revs(a) || (a.path < b.path ? -1 : a.path > b.path ? 1 : 0));
  return mine[0] || null;
}

function worseSeverity(a, b) {
  const rank = (s) => { const i = SEVERITY_RANK.indexOf(s); return i < 0 ? SEVERITY_RANK.length : i; };
  return rank(b) < rank(a) ? b : a;
}

// The worst severity of the open rows' findings on `path`, by line. A finding with no line is left out.
function rawMarks(rows, path) {
  const marks = {};
  for (const row of rows || []) {
    if (!row.open) continue;
    for (const f of row.findings || []) {
      if (f.file !== path || typeof f.line !== "number" || !(f.line > 0) || SEVERITY_RANK.indexOf(f.severity) < 0) continue;
      marks[f.line] = marks[f.line] ? worseSeverity(marks[f.line], f.severity) : f.severity;
    }
  }
  return marks;
}

// The same, keyed by the start line of the block that holds the line. A line past the last block is left out.
function gutterMarks(blocks, rows, path) {
  const marks = {};
  const raw = rawMarks(rows, path);
  for (const key of Object.keys(raw)) {
    const n = Number(key);
    const block = blocks.find((b) => b.line <= n && n <= b.end);
    if (!block) continue;
    marks[block.line] = marks[block.line] ? worseSeverity(marks[block.line], raw[key]) : raw[key];
  }
  return marks;
}

// What the document part shows. `ready`: the wanted text is in hand. Else a failed read keeps the last drawn document
// (`stale`, with the toast saying why) or, with none drawn yet, says so (`error`); a read still going is `loading`.
function docChoice(ready, failure, hasShown) {
  if (ready) return "ready";
  if (failure !== null && failure !== undefined) return hasShown ? "stale" : "error";
  return "loading";
}

// The reject note of a reader's line comments: sorted by line, blank ones dropped, whitespace collapsed. The only place
// the note's format lives. No comment, no note.
function composeNote(path, sha, comments) {
  const lines = (comments || []).map((c) => [c[0], String(c[1]).replace(/\s+/g, " ").trim()]).filter((c) => c[1]).sort((a, b) => a[0] - b[0]);
  if (!lines.length) return "";
  return ["Comments on " + path + " at " + String(sha).slice(0, 7) + ":"].concat(lines.map((c) => "L" + c[0] + ": " + c[1])).join("\n");
}

// Whether opening `tab` of run `id` refetches the list and findings kept from an earlier visit (`st`): the reader comes
// back to an artifact tab from another tab, view or run, so what changed meanwhile shows. Another file or a scroll
// target on the same tab does not. The reader's picked revision stays while the list still has it.
function reopensArtifact(prev, id, tab, st) {
  if (!isArtifactTab(tab) || !st || st.runId !== id) return false;
  return !(prev && prev.id === id && prev.tab === tab);
}

// The UTF-8 length of `s`, as the server counts it (a lone surrogate is U+FFFD's 3 bytes).
function utf8Bytes(s) {
  let n = 0;
  for (const ch of s) { const c = ch.codePointAt(0); n += c < 0x80 ? 1 : c < 0x800 ? 2 : c < 0x10000 ? 3 : 4; }
  return n;
}

// The head's "Showing the first N KB" for the document the mode draws: the text (`text`, a read of /artifact) in
// Rendered and Raw, the diff (`diff`, a read of /artifact/diff) in Diff; either is undefined while it is read. N rounds
// up, so a cut just under a 16 KB cap reads 16 KB.
function sizeNote(mode, text, diff) {
  const doc = mode === "diff" ? diff : text;
  if (!doc || !doc.truncated) return { truncated: false, kb: 0 };
  return { truncated: true, kb: Math.ceil(utf8Bytes(String((mode === "diff" ? doc.diff : doc.text) || "")) / 1024) };
}

// The resolved sha of the revision a read shows (a text's `sha`, a diff's `to`), else the one asked for. `head` is the
// run branch's tip, which only a read resolves.
function shownSha(asked, read) {
  const got = read && (typeof read.to === "string" && read.to ? read.to : read.sha);
  return typeof got === "string" && got ? got : asked;
}

// The findings rows for an artifact of `phase` whose files are `paths`: the rows of that phase's steps, and any other
// row with a finding on one of those files. A step's key starts with its phase.
function findingRows(rows, phase, paths) {
  return (rows || []).filter((row) => String(row.key || "").split("/")[0] === phase
    || (row.findings || []).some((f) => paths.includes(f.file)));
}

// RUN_TABS minus Spec and Plan where the lane does not walk them.
function visibleTabs(detail) {
  const has = (detail && detail.artifact_tabs) || [];
  return RUN_TABS.filter((tab) => !isArtifactTab(tab) || has.includes(tab));
}

// ---- state: per run, kept while the reader is on it

function artifactState(runId, kind) {
  let st = ui.artifact;
  if (!st || st.runId !== runId) {
    st = ui.artifact = { runId: runId, kind: kind, path: null, sha: null, mode: "rendered", texts: new Map(), diffs: new Map(),
      list: null, findings: null, findingsError: null, shown: null, error: null, inflight: false, failed: new Map(), busy: new Set(), crev: 0 };
  } else if (st.kind !== kind) {  // another tab of the same run: its own pick, and a fresh list
    st.kind = kind; st.path = null; st.sha = null; st.mode = "rendered"; st.list = null; st.error = null; st.shown = null;
  }
  return st;
}

function redrawArtifact(runId) {
  const run = ui.run;
  if (run && run.id === runId && run.detail && isArtifactTab(run.tab)) renderRun(run.detail);
}

// The list and the findings, together. A failure keeps what is drawn (and says so); a first one says why in the tab.
async function loadArtifacts(runId) {
  const st = ui.artifact;
  if (!st || st.runId !== runId || st.inflight) return;
  st.inflight = true;
  st.failed.clear();  // a refresh tries a failed read again
  try {
    const base = runPath(runId);
    let findingsFailure = null;
    const [list, findings] = await Promise.all([api(base + "/artifacts"), api(base + "/findings").catch((err) => { findingsFailure = err.message; return null; })]);
    if (ui.artifact !== st) return;
    st.list = Array.isArray(list) ? list : [];
    st.error = null;
    if (Array.isArray(findings)) { st.findings = findings; st.findingsError = null; }
    else if (st.findings !== null) toast("Could not refresh the findings: " + (findingsFailure || "unreadable response"));  // the last ones stay
    else st.findingsError = findingsFailure || "unreadable response";

  } catch (err) {
    if (ui.artifact !== st) return;
    if (st.list) toast("Could not refresh the " + st.kind + ": " + err.message);
    else st.error = err.message;
  } finally {
    st.inflight = false;
  }
  if (ui.artifact === st) redrawArtifact(runId);
}

const artifactQuery = (path, sha) => "/artifact?path=" + encodeURIComponent(path) + "&sha=" + encodeURIComponent(sha);
const diffQuery = (path, from, to) => "/artifact/diff?path=" + encodeURIComponent(path) + "&from=" + encodeURIComponent(from) + "&to=" + encodeURIComponent(to);

// Fetch `cache[key]` once. A failure keeps the text already shown, toasts, and is not retried until the next refresh.
async function fetchOnce(st, cache, key, url, what) {
  if (cache.has(key) || st.failed.has(key) || st.busy.has(key)) return;
  st.busy.add(key);
  try {
    const result = await api(runPath(st.runId) + url);
    if (ui.artifact !== st) return;
    cache.set(key, result);
  } catch (err) {
    if (ui.artifact !== st) return;
    st.failed.set(key, err.message);
    toast("Could not read " + what + ": " + err.message);
  } finally {
    st.busy.delete(key);
  }
  redrawArtifact(st.runId);  // a failure too: the document part falls back to the last one drawn, or says why
}

// ---- what the reader did: line comments and the revisions viewed. Kept in `ui`, outside every node, so a re-render rebuilds them.

const commentKey = (runId, path, sha) => runId + "\0" + path + "\0" + sha;

function commentsFor(runId, path, sha) {
  const key = commentKey(runId, path, sha);
  let map = ui.comments.get(key);
  if (!map) { map = new Map(); ui.comments.set(key, map); }
  return map;
}

const commentPairs = (map) => Array.from(map.entries());
const countComments = (map) => commentPairs(map).filter((c) => c[1].trim()).length;

// Both are keyed by core.js's `runKey`: the same run id in another repository is another run. `repo` undefined is the
// selected one. A node test may load this file without core.js, where a run is its id alone.
const viewedKey = (runId, repo) => (typeof runKey === "function" ? runKey(repo === undefined ? ui.repo : repo, runId) : runId);

function recordViewed(runId, path, sha, repo) {
  const run = viewedKey(runId, repo);
  let seen = ui.viewed.get(run);
  if (!seen) { seen = new Map(); ui.viewed.set(run, seen); }
  const key = path + "\0" + sha;
  if (!seen.has(key)) seen.set(key, { path: path, sha: sha });
}

// What `approve` sends as `viewed`: only what the server accepts (a sha of 7-64 hex digits, a path of 1-1024 characters, at most 100).
function viewedList(runId, repo) {
  const seen = ui.viewed.get(viewedKey(runId, repo));
  if (!seen) return [];
  const ok = (v) => /^[0-9a-f]{7,64}$/.test(v.sha) && v.path.length >= 1 && v.path.length <= 1024;
  return Array.from(seen.values()).filter(ok).slice(-VIEWED_CAP).map((v) => ({ path: v.path, sha: v.sha }));
}

// ---- drawing

const dotNode = (severity) => el("span", { class: "sev" + (severity ? " sev-" + severity : ""), title: severity || null, "aria-hidden": "true" });

function chip(text, onclick, pressed, extra) {
  return el("button", Object.assign({ type: "button", class: "chip", text: text, onclick: onclick, "aria-pressed": pressed ? "true" : "false" }, extra || {}));
}

// A gutter cell: the line (or the block's start line) a click comments on.
function gutterNode(d, st, line) {
  const open = () => openComment(st, d, line);
  const cell = el("span", { class: "gutter clickable", text: String(line), role: "button", tabindex: "0", title: "Comment on line " + line });
  cell.addEventListener("click", open);
  cell.addEventListener("keydown", (ev) => { if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); open(); } });
  return cell;
}

function openComment(st, d, line) {
  const map = commentsFor(st.runId, d.path, d.rsha);
  if (!map.has(line)) { map.set(line, ""); st.crev += 1; redrawArtifact(st.runId); }
  const box = $("run-body").querySelector('.art-comment[data-line="' + line + '"] textarea');
  if (box) box.focus();
}

function commentBox(d, st, line, map) {
  const area = el("textarea", { "aria-label": "Comment on line " + line, placeholder: "Comment on line " + line, spellcheck: "true" });
  area.value = map.get(line) || "";
  area.addEventListener("input", () => { map.set(line, area.value); syncDecision(); });
  return el("div", { class: "art-comment", "data-line": String(line) }, area,
    el("button", { type: "button", class: "chip", text: "Remove", onclick: () => { map.delete(line); st.crev += 1; redrawArtifact(st.runId); } }));
}

function docNode(d, st) {
  const box = el("div", { class: "art-doc scroll" + (d.mode === "rendered" ? " md" : "") });
  if (d.mode === "diff") {
    const diff = st.diffs.get(d.diffKey);
    box.append(diffNode(diff.diff || ""));
    return box;
  }
  const text = st.texts.get(d.path + "@" + d.sha).text || "";
  const comments = commentsFor(st.runId, d.path, d.rsha);
  const withBox = (line, row) => { if (comments.has(line)) row.append(commentBox(d, st, line, comments)); return row; };
  if (d.mode === "raw") {
    text.replace(/\n$/, "").split("\n").forEach((line, i) => {
      box.append(withBox(i + 1, el("div", { class: "art-row", "data-line": String(i + 1) },
        gutterNode(d, st, i + 1), dotNode(d.raw[i + 1]), el("code", { class: "art-raw", text: line }))));
    });
    return box;
  }
  for (const block of parseMarkdown(text)) {
    box.append(withBox(block.line, el("div", { class: "art-row", "data-line": String(block.line) },
      gutterNode(d, st, block.line), dotNode(d.gutter[block.line]), el("div", { class: "art-block" }, markdownNodes([block])))));
  }
  return box;
}

// Scroll the open document to a line: the last row that starts at or before it.
function scrollToArtifactLine(line) {
  const st = ui.artifact;
  if (!st) return;
  if (st.mode === "diff") { st.mode = "rendered"; redrawArtifact(st.runId); }
  const rows = Array.from($("run-body").querySelectorAll(".art-doc .art-row"));
  let target = null;
  for (const row of rows) if (Number(row.dataset.line) <= line) target = row; else break;
  if (!target) return;
  target.scrollIntoView({ block: "center" });
  target.classList.add("art-hit");
  setTimeout(() => target.classList.remove("art-hit"), 1600);
}

function findingNode(f) {
  const line = typeof f.line === "number" ? f.line : null;
  return el("li", { class: "finding" + (line ? " clickable" : ""), onclick: line ? () => scrollToArtifactLine(line) : null },
    dotNode(f.severity), line ? el("span", { class: "muted", text: "L" + line + " " }) : null, el("span", { text: f.text || "" }));
}

function findingsNode(d) {
  const sections = [];
  const order = d.rows.slice().sort((a, b) => (b.open ? 1 : 0) - (a.open ? 1 : 0));  // open rows first (a stable sort)
  for (const row of order) {
    const here = (row.findings || []).filter((f) => f.file === d.path);
    const other = (row.findings || []).filter((f) => f.file !== d.path);
    sections.push(el("details", { "data-key": "round:" + row.key + ":" + row.round, open: row.open ? "open" : null },
      el("summary", {}, el("strong", { text: row.label }), el("span", { class: "muted", text: " · round " + row.round + (row.open ? "" : " · earlier") + " · " + (row.findings || []).length + " findings" })),
      here.length ? el("ul", { class: "findings" }, here.map(findingNode)) : null,
      other.length ? el("div", {}, el("div", { class: "muted", text: "Other files" }),
        el("ul", { class: "findings" }, other.map((f) => el("li", { class: "finding" }, dotNode(f.severity),
          el("code", { text: (f.file || "(no file)") + (f.line ? ":" + f.line : "") + " " }), el("span", { text: f.text || "" }))))) : null,
      !here.length && !other.length ? el("p", { class: "muted", text: "No findings." }) : null));
  }
  return el("aside", { class: "art-findings" }, el("h3", { text: "Review findings" }),
    d.error ? el("p", { class: "muted", text: "Could not load the findings: " + d.error }) :
    sections.length ? sections : el("p", { class: "muted", text: "No review has run on this " + d.kind + " yet." }));
}

function headNode(d) {
  const st = ui.artifact;
  const pick = (sha) => () => { st.sha = sha; if (st.mode === "diff") st.mode = "rendered"; redrawArtifact(st.runId); };
  const chips = d.revisions.map((r) => chip(r.label || r.sha.slice(0, 7), pick(r.sha), r.sha === d.sha, { title: r.sha.slice(0, 7) }));
  if (d.merged) chips.push(chip("as merged", pick(HEAD_SHA), d.sha === HEAD_SHA, { title: "the run branch's tip" }));
  const picker = d.paths.length > 1 ? (() => {
    const select = el("select", { "aria-label": d.kind + " file" }, d.paths.map((p) => el("option", { value: p, text: p })));
    select.value = d.path;
    select.addEventListener("change", () => { st.path = select.value; st.sha = null; st.mode = "rendered"; goRun(st.runId, d.kind, select.value); });
    return select;
  })() : null;
  const previous = d.previous;
  return el("div", { class: "art-head" },
    d.kind === PLAN_TAB ? chip("Tasks", () => goRun(st.runId, PLAN_TAB, "tasks"), false) : null,
    picker, el("span", { class: "chips" }, chips),
    el("button", { type: "button", class: "chip", text: "Diff with previous", disabled: previous ? null : "disabled", "aria-pressed": d.mode === "diff" ? "true" : "false",
      onclick: () => { st.mode = st.mode === "diff" ? "rendered" : "diff"; redrawArtifact(st.runId); } }),
    chip(d.mode === "raw" ? "Rendered" : "Raw", () => { st.mode = d.mode === "raw" ? "rendered" : "raw"; redrawArtifact(st.runId); }, false),
    el("button", { type: "button", class: "chip", text: "Copy path", onclick: () => {
      navigator.clipboard.writeText(d.path).then(() => toast("Copied " + d.path), () => toast("Press Ctrl-C / ⌘C to copy " + d.path));
    } }),
    d.lines !== null ? el("span", { class: "muted", text: d.lines + " lines" }) : null,
    d.truncated ? el("span", { class: "st-wait", text: "Showing the first " + d.shownKb + " KB of " + (d.mode === "diff" ? "the diff of " : "") + d.path + "." }) : null);
}

// ---- the decision bar, under the document it decides on

// The comments on the drawn document's revision: in Diff, the revision diffed to (the selected one).
function noteOfShown(st) {
  const shown = st.shown && st.shown.data;
  if (!shown || !shown.rsha) return { count: 0, note: "" };
  const map = commentsFor(st.runId, shown.path, shown.rsha);
  return { count: countComments(map), note: composeNote(shown.path, shown.rsha, commentPairs(map)) };
}

const requestLabel = (n) => "Request changes (" + n + (n === 1 ? " comment)" : " comments)");

// Typing in a comment box changes the count and the note without a redraw, which would take the caret away.
function syncDecision() {
  const st = ui.artifact;
  const body = $("run-body");
  if (!st || !body) return;
  const { count, note } = noteOfShown(st);
  const button = body.querySelector(".art-decision [data-role=request]");
  if (button) button.textContent = requestLabel(count);
  const pre = body.querySelector(".art-decision pre.art-decision-note");
  if (pre) pre.textContent = note;
}

function requestChanges(detail) {
  const st = ui.artifact;
  const note = st ? noteOfShown(st).note : "";
  if (Array.from(note).length > NOTE_MAX) { toast("The comments make a note of " + Array.from(note).length + " characters, over the " + NOTE_MAX + " the server takes: shorten them"); return; }
  withDecision(detail, "reject", (item) => rejectDialog(item.run_id, needTarget(item), note));
}

function artifactDecisionNode(d) {
  const detail = () => (ui.run && ui.run.detail && ui.run.detail.run_id === d.runId ? ui.run.detail : null);
  if (d.waiting) {
    return el("div", { class: "art-decision" }, el("strong", { text: "Waiting on you: approve " + d.phase }),
      el("button", { type: "button", class: "primary", text: "Approve " + d.kind, onclick: () => { const x = detail(); if (x) withDecision(x, "approve", approveFlow); } }),
      d.canReject ? el("button", { type: "button", "data-role": "request", text: requestLabel(d.count), onclick: () => { const x = detail(); if (x) requestChanges(x); } }) : null);
  }
  return el("div", { class: "art-decision" }, el("strong", { text: "This phase is no longer waiting on you" }),
    el("span", { class: "muted", text: " Your comments are kept here as text you can select:" }),
    el("pre", { class: "art-decision-note", text: d.note }));
}

function artifactNote(key, text) {
  return [entry(key, text, (t) => el("p", { class: "muted art-note", text: t }))];
}

// The tab's parts, keyed: the head, the document and the findings are patched apart, so a refresh that changes only
// the findings leaves the document (and where the reader scrolled it) alone.
function artifactEntries(detail, kind, sub) {
  if (kind === PLAN_TAB) {  // Tasks or Document: planTabEntries answers null for the Document
    const tasks = planTabEntries(detail, sub);
    if (tasks) return tasks;
    if (sub === "document") sub = null;  // the mode's name, not a file
  }
  const st = artifactState(detail.run_id, kind);
  if (st.list === null) {
    if (st.error) return artifactNote("art:error", "Could not load the " + kind + ": " + st.error);
    loadArtifacts(detail.run_id);
    return artifactNote("art:loading", "Loading the " + kind + "…");
  }
  const file = pickArtifact(st.list, kind, sub || st.path);
  if (!file) return artifactNote("art:empty", "No " + kind + " yet: this phase has not committed one.");
  if (st.path !== file.path) { st.path = file.path; st.sha = null; }
  const revisions = (file.revisions || []).map((r) => ({ sha: r.sha, label: r.label }));
  const merged = detail.status === "done";
  const newest = revisions.length ? revisions[revisions.length - 1].sha : (merged ? HEAD_SHA : null);
  const known = (sha) => sha === HEAD_SHA ? merged : revisions.some((r) => r.sha === sha);
  const sha = st.sha && known(st.sha) ? st.sha : newest;  // the newest, until the reader picks another
  const index = revisions.findIndex((r) => r.sha === sha);
  const previous = sha === HEAD_SHA ? (revisions.length ? revisions[revisions.length - 1].sha : null) : index > 0 ? revisions[index - 1].sha : null;
  if (st.mode === "diff" && !previous) st.mode = "rendered";
  const mode = st.mode;
  const paths = st.list.filter((a) => a.kind === kind).map((a) => a.path);
  const loaded = sha ? st.texts.get(file.path + "@" + sha) : null;
  const diffKey = file.path + "@" + previous + ".." + sha;
  const rows = findingRows(st.findings, file.phase, paths);
  const parts = [];
  const size = sizeNote(mode, loaded, mode === "diff" ? st.diffs.get(diffKey) : null);
  parts.push(entry("art:head", { kind: kind, path: file.path, paths: paths, revisions: revisions, sha: sha, merged: merged, mode: mode, previous: previous,
    lines: loaded ? loaded.lines : null, truncated: size.truncated, shownKb: size.kb }, headNode));
  if (!sha) return [...parts, ...artifactNote("art:nodoc", "This " + kind + " has no revision to show.")];
  let body;
  const note = (key, text) => entry(key, text, (t) => el("p", { class: "muted art-note", text: t }));
  const draw = (key, data) => { st.shown = { key: key, data: data }; return entry(key, data, (d) => docNode(d, st)); };
  const choose = (cache, cacheKey, what, make) => {
    const failure = st.failed.has(cacheKey) ? st.failed.get(cacheKey) : null;
    const choice = docChoice(cache.has(cacheKey), failure, !!st.shown);
    if (choice === "ready") return make();
    if (choice === "stale") return entry(st.shown.key, st.shown.data, (d) => docNode(d, st));  // the same key and sig: the node stays
    if (choice === "error") return note("art:doc-error", "Could not read " + what + ": " + failure);
    return note("art:wait", "Loading " + what + "…");
  };
  if (mode === "diff") {
    fetchOnce(st, st.diffs, diffKey, diffQuery(file.path, previous, sha), "the diff");
    body = choose(st.diffs, diffKey, "the diff", () => draw("art:doc:" + diffKey + ":diff",
      { mode: "diff", diffKey: diffKey, path: file.path, rsha: shownSha(sha, st.diffs.get(diffKey)) }));
  } else {
    const textKey = file.path + "@" + sha;
    fetchOnce(st, st.texts, textKey, artifactQuery(file.path, sha), file.path);
    body = choose(st.texts, textKey, file.path, () => {
      const blocks = mode === "rendered" ? parseMarkdown(loaded.text || "") : [];
      const rsha = shownSha(sha, loaded);  // the resolved sha: `head` is the run branch's tip
      recordViewed(st.runId, file.path, rsha);
      return draw("art:doc:" + textKey + ":" + mode,
        { mode: mode, path: file.path, sha: sha, rsha: rsha, crev: st.crev, raw: mode === "raw" ? rawMarks(rows, file.path) : {}, gutter: mode === "rendered" ? gutterMarks(blocks, rows, file.path) : {} });
    });
  }
  parts.push(body);
  parts.push(entry("art:findings", { rows: rows, path: file.path, kind: kind, error: st.findings === null ? st.findingsError : null }, findingsNode));
  const waiting = !!(detail.waiting && detail.waiting.kind === "phase_approval" && detail.waiting.phase === file.phase);
  const decided = noteOfShown(st);
  const count = decided.count;
  if (waiting || count) {
    parts.push(entry("art:decision", { runId: detail.run_id, phase: file.phase, kind: kind, waiting: waiting, count: count, note: decided.note,
      canReject: (detail.actions || []).includes("reject") }, artifactDecisionNode));
  }
  return parts;
}
