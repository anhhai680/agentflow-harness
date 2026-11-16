"use strict";

// ------------------------------------------------------------------ the Spec and Plan tabs

// A run's spec and plan, as the run's own commits made them: every revision a phase committed, rendered or raw, a diff
// with the previous one, and the review findings beside it. The text of a `(path, sha)` never changes, so it is
// fetched once. Everything from the server reaches the page as text (`el`'s `text`, markdown.js's text nodes).

const SEVERITY_RANK = ["critical", "high", "medium", "low"];
const ARTIFACT_KINDS = ["spec", "plan"];
const isArtifactTab = (tab) => ARTIFACT_KINDS.includes(tab);
const HEAD_SHA = "head";

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
      list: null, findings: [], error: null, inflight: false, failed: new Set(), busy: new Set() };
  } else if (st.kind !== kind) {  // another tab of the same run: its own pick, and a fresh list
    st.kind = kind; st.path = null; st.sha = null; st.mode = "rendered"; st.list = null; st.error = null;
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
    const [list, findings] = await Promise.all([api(base + "/artifacts"), api(base + "/findings").catch(() => null)]);
    if (ui.artifact !== st) return;
    st.list = Array.isArray(list) ? list : [];
    if (Array.isArray(findings)) st.findings = findings;
    st.error = null;
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
    st.failed.add(key);
    toast("Could not read " + what + ": " + err.message);
    return;
  } finally {
    st.busy.delete(key);
  }
  redrawArtifact(st.runId);
}

// ---- drawing

const dotNode = (severity) => el("span", { class: "sev" + (severity ? " sev-" + severity : ""), title: severity || null, "aria-hidden": "true" });

function chip(text, onclick, pressed, extra) {
  return el("button", Object.assign({ type: "button", class: "chip", text: text, onclick: onclick, "aria-pressed": pressed ? "true" : "false" }, extra || {}));
}

function docNode(d, st) {
  const box = el("div", { class: "art-doc scroll" + (d.mode === "rendered" ? " md" : "") });
  if (d.mode === "diff") {
    const diff = st.diffs.get(d.diffKey);
    box.append(diffNode(diff.diff || ""));
    return box;
  }
  const text = st.texts.get(d.path + "@" + d.sha).text || "";
  if (d.mode === "raw") {
    text.replace(/\n$/, "").split("\n").forEach((line, i) => {
      box.append(el("div", { class: "art-row", "data-line": String(i + 1) },
        el("span", { class: "gutter", text: String(i + 1) }), dotNode(d.raw[i + 1]), el("code", { class: "art-raw", text: line })));
    });
    return box;
  }
  for (const block of parseMarkdown(text)) {
    box.append(el("div", { class: "art-row", "data-line": String(block.line) },
      el("span", { class: "gutter", text: String(block.line) }), dotNode(d.gutter[block.line]), el("div", { class: "art-block" }, markdownNodes([block]))));
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
    picker, el("span", { class: "chips" }, chips),
    el("button", { type: "button", class: "chip", text: "Diff with previous", disabled: previous ? null : "disabled", "aria-pressed": d.mode === "diff" ? "true" : "false",
      onclick: () => { st.mode = st.mode === "diff" ? "rendered" : "diff"; redrawArtifact(st.runId); } }),
    chip(d.mode === "raw" ? "Rendered" : "Raw", () => { st.mode = d.mode === "raw" ? "rendered" : "raw"; redrawArtifact(st.runId); }, false),
    el("button", { type: "button", class: "chip", text: "Copy path", onclick: () => {
      navigator.clipboard.writeText(d.path).then(() => toast("Copied " + d.path), () => toast("Press Ctrl-C / ⌘C to copy " + d.path));
    } }),
    d.lines !== null ? el("span", { class: "muted", text: d.lines + " lines" }) : null,
    d.truncated ? el("span", { class: "st-wait", text: "Showing the first " + d.shownKb + " KB of " + d.path + "." }) : null);
}

function artifactNote(key, text) {
  return [entry(key, text, (t) => el("p", { class: "muted art-note", text: t }))];
}

// The tab's parts, keyed: the head, the document and the findings are patched apart, so a refresh that changes only
// the findings leaves the document (and where the reader scrolled it) alone.
function artifactEntries(detail, kind, sub) {
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
  const rows = st.findings;
  const parts = [];
  const shown = loaded;
  parts.push(entry("art:head", { kind: kind, path: file.path, paths: paths, revisions: revisions, sha: sha, merged: merged, mode: mode, previous: previous,
    lines: shown ? shown.lines : null, truncated: !!(shown && shown.truncated),
    shownKb: shown ? Math.floor(new TextEncoder().encode(shown.text || "").length / 1024) : 0 }, headNode));
  if (!sha) return [...parts, ...artifactNote("art:nodoc", "This " + kind + " has no revision to show.")];
  let body;
  if (mode === "diff") {
    const diffKey = file.path + "@" + previous + ".." + sha;
    fetchOnce(st, st.diffs, diffKey, diffQuery(file.path, previous, sha), "the diff");
    body = st.diffs.has(diffKey)
      ? entry("art:doc:" + diffKey + ":diff", { mode: "diff", diffKey: diffKey, path: file.path }, (d) => docNode(d, st))
      : entry("art:wait", "Loading the diff…", (t) => el("p", { class: "muted art-note", text: t }));
  } else {
    fetchOnce(st, st.texts, file.path + "@" + sha, artifactQuery(file.path, sha), file.path);
    if (loaded) {
      const blocks = mode === "rendered" ? parseMarkdown(loaded.text || "") : [];
      body = entry("art:doc:" + file.path + "@" + sha + ":" + mode,
        { mode: mode, path: file.path, sha: sha, raw: mode === "raw" ? rawMarks(rows, file.path) : {}, gutter: mode === "rendered" ? gutterMarks(blocks, rows, file.path) : {} },
        (d) => docNode(d, st));
    } else {
      body = entry("art:wait", "Loading " + file.path + "…", (t) => el("p", { class: "muted art-note", text: t }));
    }
  }
  parts.push(body);
  parts.push(entry("art:findings", { rows: rows, path: file.path, kind: kind }, findingsNode));
  return parts;
}
