"use strict";

// ------------------------------------------------------------------ settings

// The form sends only the fields that changed ({key, value}); the YAML tab sends the whole text. Either way the
// server checks the result as `agentflow validate` does and refuses protected settings, and the dialog shows the diff.

const settings = { data: null, mode: "form", edits: new Map(), rows: new Map(), yamlDirty: false, request: 0 };  // `request`: the newest load
const keyId = (key) => JSON.stringify(key);
const same = (a, b) => JSON.stringify(a === undefined ? null : a) === JSON.stringify(b === undefined ? null : b);

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

function setEdit(field, value) {
  const id = keyId(field.key);
  if (same(value, field.value)) settings.edits.delete(id); else settings.edits.set(id, { key: field.key, value: value });
  const row = settings.rows.get(id);
  if (row) row.classList.toggle("changed", settings.edits.has(id));
  updateSettingsBar();
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

function mapInput(field, id) {
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
    setEdit(field, out);
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

function fieldInput(field, id) {
  const ro = !!field.readonly;
  const kind = field.kind;
  if (kind === "bool") {
    const box = el("input", { type: "checkbox", id: id, disabled: ro });
    box.checked = field.value === true;
    const set = field.present || (field.value !== null && field.value !== undefined);  // in the pack or inherited
    box.addEventListener("change", () => setEdit(field, box.checked || set ? box.checked : null));  // unset stays unset
    return box;
  }
  if (kind === "int" || kind === "number") {
    const input = el("input", { type: "number", id: id, min: field.min, max: field.max, step: kind === "int" ? "1" : "any",
      value: field.value === null || field.value === undefined ? "" : field.value, disabled: ro });
    input.addEventListener("input", () => setEdit(field, numberOf(input, kind)));
    return input;
  }
  if (kind === "enum") {
    const options = [...field.options];
    if (field.value !== null && !options.includes(field.value)) options.unshift(field.value);
    const select = el("select", { id: id, disabled: ro }, options.map((o) => el("option", { value: o, text: o })));
    select.value = field.value === null ? "" : field.value;
    select.addEventListener("change", () => setEdit(field, select.value));
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
    group.addEventListener("change", () => setEdit(field, [...group.querySelectorAll("input")].filter((b) => b.checked).map((b) => b.dataset.option)));
    return group;
  }
  if (kind === "list") {
    const input = el("input", { type: "text", id: id, value: (field.value || []).join(", "), placeholder: "comma-separated", disabled: ro });
    input.addEventListener("input", () => setEdit(field, input.value.split(",").map((v) => v.trim()).filter(Boolean)));
    return input;
  }
  if (kind === "map") return mapInput(field, id);
  const input = el("input", { type: "text", id: id, value: field.value === null || field.value === undefined ? "" : field.value,
    placeholder: field.optional ? "not set" : "", disabled: ro });
  input.addEventListener("input", () => setEdit(field, field.optional && !input.value.trim() ? null : input.value));
  return input;
}

function fieldRow(field) {
  const id = "cfg-" + field.key.join("-").replace(/[^A-Za-z0-9_-]/g, "_");
  const input = fieldInput(field, id);
  const label = field.kind === "map" || (field.kind === "list" && field.options)
    ? el("label", { text: field.label }) : el("label", { for: id, text: field.label });
  const where = field.key.join(".") + (field.source && field.source !== "project" ? " · from " + field.source : "");
  const row = el("div", { class: "set-field" }, label,
    field.kind === "map" ? el("span", { class: "set-key", text: where }) : input,
    field.kind === "map" ? input : el("span", { class: "set-key full", text: where }),
    field.help ? el("div", { class: "sub full", text: field.help }) : null,
    field.readonly ? el("div", { class: "sub full", text: field.readonly }) : null);
  settings.rows.set(keyId(field.key), row);
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
  $("settings-form").replaceChildren(...d.sections.map((section) => el("div", { class: "set-section" },
    el("h3", { text: section.title }), el("p", { class: "muted", text: section.note }), section.fields.map(fieldRow))));
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
