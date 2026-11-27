"use strict";

// ------------------------------------------------------------------ settings

// The form sends only the fields that changed ({key, value}); the YAML tab sends the whole text. Either way the
// server checks the result as `agentflow validate` does and refuses protected settings, and the dialog shows the diff.
// The field widgets serve two forms, each with its own scope ({edits, rows, update}): this repository's form, which
// saves to its pack, and Your settings (user_settings.js), which saves to your user file. A field's `target` says which.

const settings = { data: null, mode: "form", edits: new Map(), rows: new Map(), yamlDirty: false, request: 0 };  // `request`: the newest load
const keyId = (key) => JSON.stringify(key);
const same = (a, b) => JSON.stringify(a === undefined ? null : a) === JSON.stringify(b === undefined ? null : b);

// A `lines` field's text as the list it sends: one trimmed entry per line, blank lines dropped.
const linesValue = (text) => String(text || "").split("\n").map((line) => line.trim()).filter(Boolean);

// Where a field's save goes, as its row's badge says: your user file, or the named repository's pack.
function saveBadge(field, repoName) {
  if (field && field.target === "user") return "saves to: your user config";
  return "saves to: " + (repoName || "this repository");
}

// The repository the form below Your settings is: the selected one, or on a page of one the only one listed.
function settingsRepoName() {
  const only = ui.repo === null && ui.repos && Array.isArray(ui.repos.repos) && ui.repos.repos.length === 1 ? ui.repos.repos[0].id : ui.repo;
  return repoName(only);
}

// The repository form's scope: its edits, its rows and its bar.
const repoScope = () => ({ edits: settings.edits, rows: settings.rows, update: updateSettingsBar });

function pendingCount() {
  return settings.mode === "form" ? settings.edits.size : (settings.yamlDirty ? 1 : 0);
}

function updateSettingsBar() {
  const n = pendingCount();
  const d = settings.data;
  $("set-review").disabled = n === 0;
  $("set-discard").disabled = settings.edits.size === 0 && !settings.yamlDirty;
  if (!d) return;
  const pending = settings.mode === "form" ? (n ? n + " unsaved change" + (n === 1 ? "" : "s") : "no unsaved changes")
    : (n ? "unsaved edits in the YAML" : "no unsaved edits");
  $("settings-status").textContent = pending + " · sha256 " + d.sha256.slice(0, 12);
}

// An edit exists only while the value differs from the field's. A user field your file does not set, cleared, is no
// edit either: there is no personal value to delete.
function setEdit(field, value, scope) {
  const id = keyId(field.key);
  const unset = value === null && field.target === "user" && !field.present;
  if (unset || same(value, field.value)) scope.edits.delete(id); else scope.edits.set(id, { key: field.key, value: value });
  const row = scope.rows.get(id);
  if (row) row.classList.toggle("changed", scope.edits.has(id));
  scope.update();
}

function numberOf(input, kind) {
  if (input.value.trim() === "") return null;
  const value = Number(input.value);
  if (kind === "number") return Number.isFinite(value) ? value : input.value;  // a non-number stays text: the server refuses it
  return kind === "int" && Number.isInteger(value) ? value : (kind === "int" ? input.value : value);
}

// One map value as the server wants it: a number for an `int` or `number` map, text otherwise. `undefined` for an
// empty `number` value: its entry is left out, so the layers below decide it.
function mapValue(input, kind) {
  if (kind === "number") {
    const value = numberOf(input, "number");
    return value === null ? undefined : value;
  }
  return kind === "int" ? numberOf(input, "int") : input.value;
}

function mapInput(field, id, scope) {
  const rows = el("div", { class: "map-rows" });
  const pairs = Object.entries(field.value || {});
  const isInt = field.value_kind === "int";
  const isNumeric = isInt || field.value_kind === "number";
  const collect = () => {
    const out = {};
    for (const row of rows.children) {
      const [k, v] = row.querySelectorAll("input");
      if (!k.value.trim()) continue;
      const value = mapValue(v, field.value_kind);
      if (value !== undefined) out[k.value.trim()] = value;
    }
    // Every row removed in Your settings deletes your key (and the parents it empties): an empty map would stay in
    // your file, and one for an agent a pack lacks keeps that repository from loading.
    const empty = field.target === "user" && Object.keys(out).length === 0;
    setEdit(field, empty ? null : out, scope);
  };
  const addRow = (key, value) => {
    const k = el("input", { type: "text", value: key, "aria-label": field.label + ": name", placeholder: "name", disabled: !!field.readonly });
    const v = el("input", { type: isNumeric ? "number" : "text", value: value === null || value === undefined ? "" : value,
      "aria-label": field.label + ": value", placeholder: "value", step: isInt ? "1" : (isNumeric ? "any" : null), min: field.min, disabled: !!field.readonly });
    const remove = el("button", { type: "button", text: "×", title: "Remove", "aria-label": "Remove " + (key || "row"), disabled: !!field.readonly,
      onclick: () => { row.remove(); collect(); } });
    const row = el("div", { class: "map-row" }, k, v, remove);
    k.addEventListener("input", collect);
    v.addEventListener("input", collect);
    rows.append(row);
    return k;
  };
  for (const [k, v] of pairs) addRow(k, v);
  const add = el("button", { type: "button", text: "+ Add", disabled: !!field.readonly, onclick: () => addRow("", "").focus() });
  return el("div", { class: "full", id: id }, rows, el("div", { class: "opts" }, add));
}

function fieldInput(field, id, scope) {
  const ro = !!field.readonly;
  const kind = field.kind;
  const edit = (value) => setEdit(field, value, scope);
  if (kind === "bool") {
    const box = el("input", { type: "checkbox", id: id, disabled: ro });
    box.checked = field.value === true;
    const set = field.present || (field.value !== null && field.value !== undefined);  // in the pack or inherited
    box.addEventListener("change", () => edit(box.checked || set ? box.checked : null));  // unset stays unset
    return box;
  }
  if (kind === "int" || kind === "number") {
    const input = el("input", { type: "number", id: id, min: field.min, max: field.max, step: kind === "int" ? "1" : "any",
      value: field.value === null || field.value === undefined ? "" : field.value, disabled: ro });
    input.addEventListener("input", () => edit(numberOf(input, kind)));
    return input;
  }
  if (kind === "enum") {
    const options = [...field.options];
    if (field.value !== null && !options.includes(field.value)) options.unshift(field.value);
    const select = el("select", { id: id, disabled: ro }, options.map((o) => el("option", { value: o, text: o })));
    select.value = field.value === null ? "" : field.value;
    select.addEventListener("change", () => edit(select.value));
    return select;
  }
  if (kind === "list" && field.options) {
    const chosen = new Set(field.value || []);
    const boxes = field.options.map((o) => {
      const box = el("input", { type: "checkbox", disabled: ro });
      box.checked = chosen.has(o);
      box.dataset.option = o;
      return el("label", {}, box, o);
    });
    const group = el("div", { class: "checks", id: id, role: "group", "aria-label": field.label }, boxes);
    group.addEventListener("change", () => edit([...group.querySelectorAll("input")].filter((b) => b.checked).map((b) => b.dataset.option)));
    return group;
  }
  if (kind === "list" && field.lines) {  // one entry per line; cleared, an optional field sends null (your value is deleted)
    const box = el("textarea", { id: id, class: "lines", spellcheck: "false", placeholder: field.optional ? "not set" : "", disabled: ro });
    box.value = (field.value || []).join("\n");
    box.addEventListener("input", () => {
      const list = linesValue(box.value);
      edit(!list.length && field.optional ? null : list);
    });
    return box;
  }
  if (kind === "list") {
    const input = el("input", { type: "text", id: id, value: (field.value || []).join(", "), placeholder: "comma-separated", disabled: ro });
    input.addEventListener("input", () => edit(input.value.split(",").map((v) => v.trim()).filter(Boolean)));
    return input;
  }
  if (kind === "map") return mapInput(field, id, scope);
  const input = el("input", { type: "text", id: id, value: field.value === null || field.value === undefined ? "" : field.value,
    placeholder: field.optional ? "not set" : "", disabled: ro });
  input.addEventListener("input", () => edit(field.optional && !input.value.trim() ? null : input.value));
  return input;
}

function fieldRow(field, scope) {
  const id = (field.target === "user" ? "ucfg-" : "cfg-") + field.key.join("-").replace(/[^A-Za-z0-9_-]/g, "_");  // one page, two forms
  const input = fieldInput(field, id, scope);
  const label = field.kind === "map" || (field.kind === "list" && field.options)
    ? el("label", { text: field.label }) : el("label", { for: id, text: field.label });
  const where = field.key.join(".") + (field.source && field.source !== "project" ? " · from " + field.source : "");
  const row = el("div", { class: "set-field" }, label,
    field.kind === "map" ? el("span", { class: "set-key", text: where }) : input,
    field.kind === "map" ? input : el("span", { class: "set-key full", text: where }),
    el("span", { class: "save-badge", text: saveBadge(field, field.target === "user" ? "" : settingsRepoName()) }),
    field.help ? el("div", { class: "sub full", text: field.help }) : null,
    field.readonly ? el("div", { class: "sub full", text: field.readonly }) : null,
    field.hint ? el("div", { class: "sub full set-hint", text: field.hint }) : null);
  scope.rows.set(keyId(field.key), row);
  return row;
}

function renderSettings() {
  const d = settings.data;
  if (!d) return;
  settings.rows.clear();
  $("settings-path").textContent = d.path;
  const from = d.protected_sources || {};
  $("settings-protected").textContent = "Protected, so only the file can change them: "
    + d.protected.map((k) => from[k] ? k + " (from " + from[k] + ")" : k).join("; ") + ".";
  const scope = repoScope();
  $("settings-form").replaceChildren(...d.sections.map((section) => el("div", { class: "set-section" },
    el("h3", { text: section.title }), el("p", { class: "muted", text: section.note }), section.fields.map((f) => fieldRow(f, scope)))));
  $("settings-yaml").value = d.text;
  updateSettingsBar();
}

function settingsMode(mode) {
  settings.mode = mode;
  $("settings-form").hidden = mode !== "form";
  $("settings-text").hidden = mode !== "yaml";
  $("set-mode-form").setAttribute("aria-pressed", String(mode === "form"));
  $("set-mode-yaml").setAttribute("aria-pressed", String(mode === "yaml"));
  updateSettingsBar();
}

async function loadSettings() {
  const request = ++settings.request;  // an older answer (another repository's, after a switch) is dropped
  try {
    const data = await api("/api/config");
    if (request !== settings.request) return;
    settings.data = data;
    settings.edits.clear();
    settings.yamlDirty = false;
    renderSettings();
  } catch (err) {
    if (request !== settings.request) return;
    $("settings-status").textContent = "Could not load the config: " + err.message;
  }
}

// Settings opened, or the repositories changed under it: the repository's form, or none when the page has no
// repository to show (an empty workspace), so nothing asks for a config that is not there.
function showRepoSettings() {
  const none = nothingToShow(ui.repos, ui.multi, ui.repo);
  $("settings-repo").hidden = none;
  if (none) {
    settings.request++;  // an answer still out is dropped
    settings.data = null;
    return;
  }
  const name = settingsRepoName();
  $("settings-repo-title").textContent = name ? "Repository: " + name : "This repository";
  loadSettings();
}

async function reviewSettings() {
  const d = settings.data;
  if (!d) return;
  const body = settings.mode === "form"
    ? { base_sha256: d.sha256, changes: [...settings.edits.values()] }
    : { base_sha256: d.sha256, text: $("settings-yaml").value };
  let result;
  try {
    result = await api("/api/config/preview", body);
  } catch (err) {
    toast("Not saved: " + err.message);
    return;
  }
  if (!result.changed) { toast("Nothing in the file changes"); return; }
  const issues = result.issues.map((i) => el("li", { class: i.level === "error" ? "st-bad" : "muted",
    text: i.level + " " + (i.path ? i.path + ": " : "") + i.message }));
  const verdict = result.ok ? "It validates." : "It does not validate: fix the errors below first.";
  dialog("Save config.yaml?", [
    el("p", { text: verdict, class: result.ok ? "st-ok" : "st-bad" }),
    diffNode(result.diff),
    issues.length ? el("ul", {}, issues) : null,
  ], async () => {
    if (!result.ok) { toast("Fix the errors first"); return false; }
    try {
      await api("/api/config/save", body);
    } catch (err) {
      toast("Save failed: " + err.message);
      return false;
    }
    toast("Saved: a running run takes it at its next phase");
    ui.workflow = null;  // the Workflow tab is built from the config
    await loadSettings();
    refreshAfter();
    return true;
  }, result.ok ? "Save" : "Close", true);
}

function discardSettings() {
  if (!confirm("Discard your unsaved changes?")) return;
  settings.edits.clear();
  settings.yamlDirty = false;
  renderSettings();
}

$("set-mode-form").addEventListener("click", () => settingsMode("form"));
$("set-mode-yaml").addEventListener("click", () => settingsMode("yaml"));
$("set-review").addEventListener("click", reviewSettings);
$("set-discard").addEventListener("click", discardSettings);
$("set-reload").addEventListener("click", () => {
  if ((settings.edits.size || settings.yamlDirty) && !confirm("Reload from the file and drop your unsaved changes?")) return;
  loadSettings();
});
$("settings-yaml").addEventListener("input", () => { settings.yamlDirty = $("settings-yaml").value !== (settings.data || {}).text; updateSettingsBar(); });
$("settings-yaml").addEventListener("keydown", (ev) => {
  if (ev.key !== "Tab" || ev.shiftKey) return;  // two spaces, as YAML indents; Shift+Tab still leaves the box
  ev.preventDefault();
  ev.target.setRangeText("  ", ev.target.selectionStart, ev.target.selectionEnd, "end");
  ev.target.dispatchEvent(new Event("input"));
});
