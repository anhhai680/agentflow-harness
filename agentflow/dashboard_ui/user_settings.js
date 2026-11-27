"use strict";

// ------------------------------------------------------------------ Your settings: the personal keys of your user file

// The section above the repository's form. It edits only the fields `/api/user-config` lists (an allow-list the
// server keeps), sends field changes only (there is no YAML tab), and needs no repository: an empty dashboard shows it.
// Its edits are its own: a repository switch, a rescan or a save of the repository's form never drops them, and the
// switch's question about unsaved Settings changes is the repository form's alone (`settingsMayLeave`).
// The pure helpers come first and touch no DOM.

const userSettings = { data: null, edits: new Map(), rows: new Map(), request: 0, update: updateUserBar, focusRoots: false };

// The page opens on Settings: `/api/repos` answered and lists no repository at all.
const emptyStart = (view) => !!view && Array.isArray(view.repos) && view.repos.length === 0;

const repoCount = (view) => (view && Array.isArray(view.repos) ? view.repos.length : 0);

// The toast after a save: what took effect at once, and what waits for a dashboard restart.
function userSaveNote(result) {
  const r = result || {};
  const restart = Array.isArray(r.restart) ? r.restart.map(String) : [];
  let note = "Saved to your user config";
  if (r.rescanned) note += "; the workspace was scanned again";
  if (restart.length) note += "; restart agentflow dashboard for " + restart.join(", ") + " to take effect";
  return note;
}

function updateUserBar() {
  const d = userSettings.data;
  const n = userSettings.edits.size;
  $("user-review").disabled = !d || !d.editable || n === 0;
  $("user-discard").disabled = n === 0;
  if (!d) return;
  const pending = n ? n + " unsaved change" + (n === 1 ? "" : "s") : "no unsaved changes";
  const file = d.exists ? "sha256 " + String(d.sha256 || "").slice(0, 12) : "the file does not exist yet";
  const problem = d.error ? (d.editable ? "Your user config does not load: " + d.error + " · " : String(d.error) + " · ") : "";
  $("user-status").textContent = problem + pending + " · " + file;
  $("user-status").classList.toggle("st-bad", !!d.error);
}

function renderUserSettings() {
  const d = userSettings.data;
  if (!d) return;
  userSettings.rows.clear();
  $("user-path").textContent = d.path || "your user config";
  const locked = d.editable ? null : (d.error || "your user config can't be edited here; fix it by hand");
  $("user-form").replaceChildren(...(d.sections || []).map((section) => {
    const workspace = section.fields.some((f) => f.key[0] === "workspace");
    const note = workspace && d.rescans === false
      ? section.note + " This dashboard serves one repository, so it takes a workspace change only after a restart." : section.note;
    return el("div", { class: "set-section" }, el("h3", { text: section.title }), el("p", { class: "muted", text: note }),
      section.fields.map((f) => fieldRow(locked && !f.readonly ? Object.assign({}, f, { readonly: locked }) : f, userSettings)));
  }));
  $("user-prompt").hidden = !emptyStart(ui.repos);
  updateUserBar();
  if (userSettings.focusRoots) {  // the empty start: the cursor waits in the folder list
    userSettings.focusRoots = false;
    const roots = $("ucfg-workspace-roots");
    if (roots && !roots.disabled) roots.focus();
  }
}

async function loadUserSettings() {
  const request = ++userSettings.request;  // an older answer is dropped
  try {
    const data = await api("/api/user-config", undefined, null);  // no repository's route
    if (request !== userSettings.request) return;
    userSettings.data = data;
    userSettings.edits.clear();
    renderUserSettings();
  } catch (err) {
    if (request !== userSettings.request) return;
    $("user-status").textContent = "Could not load your user config: " + err.message;
  }
}

// After a save: the form is read again, the Workflow tab rebuilt (models and budgets reach it), and the repositories
// listed again (the server rescanned inside the save). A page that had none wakes up; the repository form below is
// read again when it holds no unsaved change, since a field your file now sets turns read only there.
async function afterUserSave() {
  const before = repoCount(ui.repos);
  ui.workflow = null;
  await loadUserSettings();
  await loadRepos();
  if (before === 0 && repoCount(ui.repos) > 0) {
    loadInfo();
    boardRefresh();
  }
  if (ui.view === "settings" && !settings.edits.size && !settings.yamlDirty) showRepoSettings();
}

async function reviewUserSettings() {
  const d = userSettings.data;
  if (!d || !d.editable || !userSettings.edits.size) return;
  const body = { base_sha256: d.sha256, changes: [...userSettings.edits.values()] };
  let result;
  try {
    result = await api("/api/user-config/preview", body, null);
  } catch (err) {
    toast("Not saved: " + err.message);
    return;
  }
  if (!result.changed) { toast("Nothing in the file changes"); return; }
  const issues = (result.issues || []).map((i) => el("li", { class: i.level === "error" ? "st-bad" : "muted",
    text: i.level + " " + (i.path ? i.path + ": " : "") + i.message }));
  const verdict = result.ok ? "It loads, with every repository." : "It does not load: fix the errors below first.";
  dialog("Save your user config?", [
    el("p", { text: verdict, class: result.ok ? "st-ok" : "st-bad" }),
    diffNode(result.diff),
    issues.length ? el("ul", {}, issues) : null,
  ], async () => {
    if (!result.ok) { toast("Fix the errors first"); return false; }
    let saved;
    try {
      saved = await api("/api/user-config/save", body, null);
    } catch (err) {
      toast("Save failed: " + err.message);
      return false;
    }
    toast(userSaveNote(saved));
    await afterUserSave();
    return true;
  }, result.ok ? "Save" : "Close", true);
}

function discardUserSettings() {
  if (!confirm("Discard your unsaved changes to your user config?")) return;
  userSettings.edits.clear();
  renderUserSettings();
}

$("user-review").addEventListener("click", reviewUserSettings);
$("user-discard").addEventListener("click", discardUserSettings);
$("user-reload").addEventListener("click", () => {
  if (userSettings.edits.size && !confirm("Reload your user config and drop your unsaved changes?")) return;
  loadUserSettings();
});
