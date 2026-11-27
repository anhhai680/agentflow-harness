# Workspace, slice W4 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Settings has two levels. A **Your settings** section, visible whenever the page is open (even with no repository), edits the personal keys and saves them to the user file. The repository's form keeps saving to that repository's pack. Every field says where it saves. A dashboard with no repository and no workspace opens on Settings and asks for a folder, and the repositories in that folder appear without a restart.

**Architecture:** `config_edit.py` gets a second, narrower write path for the user file (`user_form_fields`, `propose_user`, `save_user`). It reuses the form's text edits (comments and anchors kept), the sha256 conflict check and the atomic replace. It adds a fixed allow-list (`USER_FIELD_KEYS`), refuses symlinks and hardlinks, and checks the candidate text before writing it: the candidate has to load through `layers.load_user_settings` and through every live repository's pack. Both loaders gain a parameter that points them at a candidate file. `dashboard.py` serves `/api/user-config` as a route that needs no repository. After a save it reloads every live repository's config, and when a workspace key changed it rescans at once. On the page, `settings.js`'s field widgets take a scope (the repository's or the user's), and a new `user_settings.js` draws the section, its save flow and the empty-state prompt.

**Tech Stack:** Python 3.12, stdlib + `pyyaml`, pytest (real tmp git repos, `http.client` against a real server on port 0). The page is plain browser JavaScript with no build step, and its pure helpers are tested under `node` through `conftest.py`'s `js`.

**Spec:** `docs/specs/agentflow-workspace-proposal.md` (approved 2026-11-26), slice **W4**: section 5, the `/api/user-config` row of section 4.3, the last row of the 4.1 table, the user-file sentence of section 2, the Security bullets on the user-file write, success criteria 6 and 7, and the "Settings" bullet of the Testing strategy. Read it alongside `docs/plans/2026-11-26-agentflow-harness-workspace-w2.md` (the `Snapshot`, `rescan` and `_scan_lock` this slice uses).

**Order and execution:** branch from an up-to-date `main` (3.16, with W3 merged). W4 changes `layers.py` and the `config_edit.py` write path, so the security overlay applies. Tasks 1 and 2 are planned with `security-and-hardening` and reviewed with the `security-auditor` agent.

### What W4 does not do

- No YAML tab for the user file. The User section sends field changes only, so the allow-list can't be bypassed. Anything else in the file is edited by hand.
- No new config key, and no change to `validate`, `layers.USER_KEYS` or `_PROTECTED_KEYS`.
- No run cap and no merged digest (W5).

### Plan decisions

- **The allow-list (`USER_FIELD_KEYS`) is exactly the personal keys that section 5 routes to the user file:** `workspace.roots`, `workspace.repos`, `workspace.exclude`, `dashboard.port`, `dashboard.board_refresh_s`, `dashboard.done_shown`, `defaults.run_budget_usd`, `defaults.run_budget_hard_usd`, `defaults.task_budget_usd`, `hitl.remind_after_h`, the five `usage.*` keys (`governor`, `five_hour_soft`, `five_hour_hard`, `seven_day_soft`, `finish_first`), and `agents.<name>.models` for each agent the **engine defaults** define (`claude`, `codex`). The `usage.*` keys are on the list because section 5's rule is "the other personal keys the page already has fields for", and the page has them. Its parenthesis names only examples. `agents.<name>.models` is limited to the engine's agents because a user file that names an agent some pack lacks breaks that pack's load. `hitl.notify`, `agents.claude.user_settings` and `dashboard.host` are never on the list.
- **`workspace` stays in `_PROTECTED_KEYS`.** There it protects the *pack* form: a pack must never carry the workspace. The User section's allow-list is a separate list. So the section 5 rule "everything in `_PROTECTED_KEYS` is not editable" applies to every key except `workspace.*`, which is the User section's own. Task 4 amends the Security bullet to say so.
- **The repository form keeps the personal fields it has.** A pack legitimately states the team's default models, budgets and usage marks, and the existing tests pin that. Those fields still save to the pack. Where the user file sets one, the field stays read-only, as today, and its note now points to **Your settings**. Every field row carries a badge: `saves to: your user config` in the User section, `saves to: <repository name>` in the repository form. Task 4 amends the second row of the section 5 table to match.
- **Clearing a user field deletes your personal value.** Every user field is `optional`, and `null` removes the key from the user file, so the pack or the engine decides again.
- **A user file the dashboard can't use still opens.** When the text parses as a YAML mapping (within `MAX_CONFIG_BYTES`), the section is editable and shows the load error. A save is accepted once the new text loads, so a bad `workspace.roots` entry can be fixed from the page. When the file is a symlink, a hardlink, not a regular file, not UTF-8 or not YAML, the section shows the reason with every field read-only (`editable: false`), and the file is fixed by hand. Task 4 amends section 2's "the page that fixes it".
- **The whole-stack check compares before and after.** A pack that loads with the current user file and fails with the candidate is an error that names the repository. A pack that already fails with the current file blocks nothing. The packs checked are the server's live contexts at the time of the request. A temp-file path never appears in a message: it is replaced with the real user-file path.
- **Only a newly added `workspace.roots` entry must exist** (`Path(entry).expanduser().is_dir()`). An entry already in the file may have gone away, and saving another field must still work. The existence check is one `stat`, and nothing is listed or read. Every entry, old or new, also passes `layers.workspace_settings`' shape rules (`/…`, `~`, `~/…`, printable, at most 256).
- **Routes:** `GET /api/user-config`, `POST /api/user-config/preview` and `POST /api/user-config/save`. This is the `/api/config` trio, and they need no repository (as `/api/repos`). Under `/api/repos/<id>/` they are 404. A body with `text` is a 400. A save answers `{ok, sha256, restart, rescanned}`. `restart` lists `dashboard.port` when it changed, plus the changed `workspace.*` keys on a server that doesn't rescan (a single-repository or `--config` dashboard serves a workspace only after a restart).
- **A new user file is created private:** the folder through `safeio.ensure_dir` (which refuses a symlinked folder), the file with mode `0o600`. An existing file keeps its mode.
- **Empty start:** when `/api/repos` lists no repository, the first route lands on `#/settings`, the repository part of Settings is hidden, and a prompt above `workspace.roots` asks for a folder. After a save the page reads `/api/repos` again. The server rescanned inside the save, so the new repositories are already listed.

## Global Constraints

- Engine dependencies stay stdlib + `pyyaml>=6`. No project strings in the engine (`test_portability.DENYLIST`), and that includes `dashboard_ui/*.js`.
- The page never writes `hitl.notify`, `agents.*.user_settings` or `dashboard.host`. A change key outside `USER_FIELD_KEYS` is a 400, whatever the body says, and the file stays byte for byte as it was.
- The user-file write touches `layers.user_config_path()` only. No request value becomes a path, except the existence `stat` of a newly added root.
- A single-repository dashboard answers every existing route byte for byte as before. Every existing `test_config_edit.py`, `test_dashboard.py` and `test_dashboard_js.py` test passes unchanged, and the golden `explain` files don't change.
- Every string from the user file, a repository or an error reaches the page through `el()`'s `textContent`, never as markup.
- `uvx ruff@0.17.4 check .` must print `All checks passed!` before every commit. Tests run with `uv run --no-project --with pyyaml --with pytest pytest <file> --import-mode=prepend -q`.
- Every user-visible change adds one line under `## [Unreleased]` in `CHANGELOG.md`. Task 4 releases 3.17 (or the next version past whatever `main` has by then). Commit messages are one imperative sentence ending with the trailer the executing session is given (`Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>` below).

## Review Focus

1. **A first-time user file:** there is no file and no `~/.config/agentflow/` folder, or the file is empty, or it holds `workspace:` with only commented-out children. Adding a root creates or edits the file, keeps every other line and comment (a hand-written `hitl.notify` included), and a new file is `0o600` (Task 1).
2. **A root that has gone away:** the file lists `/gone`. Saving `dashboard.done_shown` works, and saving roots `[/gone, <new existing folder>]` works. Only a new `/nope` is refused (Task 1).
3. **A user edit that would break one live repository:** a pack that removes `codex`, and a user edit setting `agents.codex.models`. The save is refused with an error that names the repository and never the temp file. A repository whose pack was already broken blocks nothing (Tasks 1 and 2).
4. **A user file the dashboard can't use:** a relative `workspace.roots` entry is editable and fixable from the form. A symlinked, hardlinked or invalid-YAML file shows its reason read-only. The page opens on Settings either way (Tasks 1 and 3).
5. **Unsaved User edits are not a repository's:** switching repositories, a rescan, or a repository-form save never drops them, and the switch's "Discard the unsaved Settings changes" question is asked only for the repository form's edits (Task 3).

---

### Task 1: The user-file form and its guarded write

**Files:**
- Modify: `agentflow/layers.py` (`load_user_settings` takes `path`)
- Modify: `agentflow/config.py` (`load_config` and `_merge_layers` take `user_file`)
- Modify: `agentflow/config_edit.py` (the user form, `read_user`, `propose_user`, `save_user`; `_apply_form` split around a shared `_apply_changes`; `_edit` handles an empty document and a null parent; pack fields carry `target: "project"`; the module docstring)
- Modify: `CHANGELOG.md` (an `Added` line under `[Unreleased]`: the user-file write path, with its allow-list)
- Test: `agentflow/tests/test_config_edit.py`, `agentflow/tests/test_layers.py`

**Interfaces:**
- Consumes: `layers.USER_KEYS`, `layers.workspace_settings`, `layers.parse_config`, `layers.read_config_text`, `layers.base_layers`, `layers.merge`, `layers.MAX_CONFIG_BYTES`, `safeio.ensure_dir`, the existing `_check_value`, `_set_deep`, `_edit`, `_own_entries`, `_comments`, `Proposal`, `Conflict`, `Refused`, `EditError`, `_LOCK`.
- Produces:
  ```python
  # layers.py
  def load_user_settings(path: Path | None = None) -> UserSettings
      # `path` given: that file is the user file (a candidate the page checks); a missing one is no file
  # config.py
  def load_config(path: Path, *, layered: bool = True, user: bool = True, user_file: Path | None = None) -> Config
      # `user_file` given: read it instead of `layers.user_config_path()` (ignored when `user` is False)
  # config_edit.py
  USER_FIELD_KEYS: tuple[tuple[str, ...], ...]          # the allow-list, in form order
  def read_user(path: Path) -> tuple[str, str, bool]   # (text, sha256, exists); missing -> ("", sha256(""), False);
                                                       # a symlink, hardlink (st_nlink > 1), non-regular or oversized file -> EditError
  def user_form_fields(path: Path | None = None) -> dict
      # {"path", "sha256", "exists", "editable", "error": str | None,
      #  "sections": [{"title", "note", "fields": [{key, label, kind, ..., "optional": True, "value", "present",
      #                "source": "user" | "engine", "help", "target": "user", "lines"?: True}]}]}
      # never raises for a bad file: `editable` False and `error` say why
  def propose_user(base_sha256: str, changes: list, packs: Sequence[tuple[str, Path]] = (),
                   path: Path | None = None) -> Proposal      # raises Conflict, Refused, EditError
  def save_user(proposal: Proposal, path: Path | None = None) -> str   # the new sha256; Conflict when the file moved
  ```
  `path=None` means `layers.user_config_path()` everywhere. `packs` is `(repository name, pack config path)` per live repository. Every field of `form_fields` (the pack form) gains `"target": "project"`, and the read-only note of a user-set field ends `; change it under Your settings`.

- [ ] **Step 1: Write the failing tests**

In `test_layers.py`:
- `test_load_user_settings_reads_the_file_it_is_given(tmp_path)`: a file at `tmp_path / "cand.yaml"` holding `workspace: {roots: [<tmp_path>]}` gives `roots == (str(tmp_path),)` while `layers.user_config_path()` holds a different root. A missing `path` gives `path is None` and empty settings.
- `test_load_config_reads_the_user_file_it_is_given(sparse_repo, tmp_path)`: a candidate holding `defaults: {run_budget_usd: 7}` gives `load_config(pack, user_file=cand).defaults.run_budget_usd == 7`. A candidate holding `gates: {}` raises `ConfigError` naming the candidate.

In `test_config_edit.py`, with `XDG_CONFIG_HOME` from `conftest.py`, so `layers.user_config_path()` is per test:
- `test_the_user_form_holds_the_allow_list_and_nothing_else`: the field keys of `user_form_fields()` equal `set(USER_FIELD_KEYS)`. Every key matches a `layers.USER_KEYS` pattern. None is `hitl.notify`, `agents.claude.user_settings` or `dashboard.host`, and none falls under a `_PROTECTED_KEYS` entry other than `workspace`. Every field has `target == "user"` and `optional is True`. The `agents` keys are exactly `claude` and `codex` `models`. The `workspace.*` fields have `lines is True`.
- `test_a_missing_user_file_is_an_empty_form_and_the_first_save_creates_it_private`: with no folder, `read_user(p) == ("", sha256(""), False)` and `user_form_fields()["exists"] is False`. A save of `workspace.roots = [str(root)]` creates the folder and the file, `stat.S_IMODE(mode) == 0o600`, and `yaml.safe_load` gives `{"workspace": {"roots": [str(root)]}}`.
- `test_a_commented_out_workspace_block_takes_a_new_root`: the text `"hitl:\n  notify: [desktop]  # mine\nworkspace:\n  # roots:\n  #   - ~/old\n"` saves `roots` with `hitl.notify` unchanged and `# mine` and `# roots:` still in the text. An empty file (`""`) takes a root too.
- `test_keys_outside_the_allow_list_are_refused_and_change_nothing`: each of `["hitl", "notify"]`, `["agents", "claude", "user_settings"]`, `["dashboard", "host"]`, `["gates", "unit"]`, `["workspace"]` and `["agents", "mine", "models"]` raises `Refused`, and the file's bytes are unchanged.
- `test_a_new_root_must_be_an_existing_folder_and_an_old_one_may_be_gone`: with `/gone` in the file, `dashboard.done_shown = 20` saves, and roots `["/gone", str(existing)]` save. A new `"/nope"` raises `EditError` matching `not a folder`. `"code"` (relative) and `"~bob/x"` raise `EditError` matching `absolute path`.
- `test_a_stale_base_is_a_conflict`: `propose_user("0" * 64, …)` raises `Conflict`. A save whose file changed after the proposal raises `Conflict` too.
- `test_a_symlinked_or_hardlinked_user_file_is_shown_read_only_and_never_written`: a symlink at the path gives `user_form_fields()["editable"] is False` with `"symlink"` in `error`, and `propose_user` raises `EditError`. The same holds for an `os.link` hardlink (`"hardlink"`). With a symlinked `agentflow` folder, `save_user` raises `EditError` and the link's target is untouched.
- `test_an_unparseable_user_file_is_read_only_and_a_loadable_fix_is_accepted`: `"::: [\n"` gives `editable is False`. A file holding `workspace: {roots: [code]}` gives `editable is True`, an `error` naming `workspace.roots`, and saving roots `[str(existing)]` succeeds.
- `test_a_user_edit_that_breaks_a_live_pack_is_an_error_naming_it`: pack A is the sparse pack plus `agents: {codex: null}`. If that pack does not load on its own, choose another value of a `USER_FIELD_KEYS` field that A's load refuses and the engine accepts. Pack B is an already-broken pack (`version: 99`). `propose_user(…, changes=[agents.codex.models = {"build": "x"}], packs=[("A", a), ("B", b)])` has exactly one error, which contains `A:` and does not contain `.config-check-`. Saving it raises `EditError`.
- `test_the_pack_form_says_each_field_saves_to_the_pack`: every `form_fields` field has `target == "project"`. With `_user_file({"dashboard": {"done_shown": 20}})`, that field's `readonly` contains `user config` and `Your settings`.

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_config_edit.py agentflow/tests/test_layers.py --import-mode=prepend -q`
Expected: the new tests FAIL (`unexpected keyword argument 'user_file'`, `has no attribute 'user_form_fields'`, and similar). Every existing test passes.

- [ ] **Step 3: Implement the loaders' parameters, then the user form and its write**

Add `path` to `load_user_settings` and `user_file` to `load_config`/`_merge_layers`, replacing the user path only where it is looked up today.

In `config_edit.py`, build the user sections from a fixed table: Workspace (`roots`, `repos`, `exclude`, kind `list`, `lines: True`), Dashboard, Budgets, Usage window, Reminders, Models. The labels and bounds of every key `_SECTIONS` already has come from `_SECTIONS`, looked up by key, so there is one source. The user `_Layers` comes from the user file's parsed text over `layers.merge(layers.base_layers(None))`, with no `check_user` (so a bad file still shows values): `project` is the file's data, `below` is the engine merge, and `comments` are the engine's and the file's.

Split `_apply_form`: `_apply_changes(text, changes, lay, index) -> str` holds today's loop and parse-back check. `_apply_form` builds the pack's `lay` and index and calls it. `propose_user` builds the user ones and calls it. Treat `yaml.safe_load(text)` as `{}` when it is `None`.

`_edit` changes in two places. A document with no node (empty, or comments only) gets the key appended as `key: <flow value>` on a new line at the end. A parent whose value is a null scalar (`workspace:` with only comments under it) has that null replaced by the rest of the key as a flow mapping, with one space after the colon.

`propose_user` runs these checks in order:
1. `read_user` and the sha comparison.
2. Every change key is in `USER_FIELD_KEYS`; otherwise `Refused`.
3. Each value passes `_check_value`. A `workspace.*` value also passes `layers.workspace_settings({key: value})`. Each `roots` entry that the file's current list lacks passes `is_dir`.
4. After `_apply_changes`, the parsed old text and the parsed new text, each with every `USER_FIELD_KEYS` leaf removed, are equal; otherwise `Refused`.
5. The candidate is written to `.config-check-*.yaml` in the user file's folder, removed in a `finally`. It must pass `load_user_settings(temp)`. Then, for each pack that loads with the current file, it must pass `load_config(pack, user_file=temp)`.

The issues of step 5 name the repository and say `str(path)` wherever the message said the temp path.

`save_user` holds `_LOCK`, calls `read_user` again and compares the sha, then calls `safeio.ensure_dir(folder)` (a `SymlinkRefused` becomes an `EditError`). It writes a `mkstemp` file in the folder and applies `chmod` (the old mode, or `0o600`) before `os.replace`. Update the module docstring with a paragraph on the user file.

- [ ] **Step 4: Run the tests to verify they pass**

Run the Step 2 command, then `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_config.py agentflow/tests/test_portability.py --import-mode=prepend -q`
Expected: PASS.

- [ ] **Step 5: Lint and commit**

```bash
uvx ruff@0.17.4 check .
git add agentflow/layers.py agentflow/config.py agentflow/config_edit.py agentflow/tests/test_config_edit.py agentflow/tests/test_layers.py CHANGELOG.md
git commit -m "Edit the allow-listed personal keys of the user file with the form's checks, refusing symlinks and any text a live pack would not load.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: `/api/user-config`, and what a save changes on the server

**Files:**
- Modify: `agentflow/dashboard.py` (`USER_ROUTES`, `_handle`, `_scope`, `_user_config_get`/`_user_config_post`, `DashboardServer.user_saved`, the module docstring's routes)
- Modify: `CHANGELOG.md` (an `Added` line: the three routes, needing no repository)
- Test: `agentflow/tests/test_dashboard.py`

**Interfaces:**
- Consumes: Task 1's `user_form_fields`, `propose_user`, `save_user`, `Proposal.as_dict`; `DashboardServer.rescan`, `_scan_lock`, `live_contexts`; `layers.load_user_settings`; `load_config`.
- Produces:
  ```python
  USER_ROUTES = ("/api/user-config", "/api/user-config/preview", "/api/user-config/save")
  class DashboardServer:
      def user_saved(self, keys: Collection[str]) -> bool
          # under _scan_lock: reload each live context's cfg (stored as a pack save stores it) and the snapshot's
          # `user` (user_error None); then, on a workspace server with a `workspace.*` key in `keys`, `rescan()`.
          # True when it rescanned. A context whose reload fails is logged with its name and keeps its cfg. Never raises.
  ```
  `GET /api/user-config` is `user_form_fields()` plus `"rescans": server.workspace`. `POST …/preview` answers `proposal.as_dict()`. `POST …/save` answers `{"ok": True, "sha256", "restart": [dotted], "rescanned": bool}`, or 400 `{…as_dict(), "error": "the change does not validate"}`. The status codes are 409 for a `Conflict`, 400 for an `EditError` and for a body with `text` or without `base_sha256` and a list `changes`, and 404 for the wrong method.

- [ ] **Step 1: Write the failing tests**

- `test_the_user_settings_answer_with_no_repository(empty_dash)`: `GET /api/user-config` is 200. Its section titles start with `Workspace`, `rescans is True`, and `exists is False`.
- `test_adding_a_root_on_an_empty_dashboard_writes_only_the_user_file_and_the_repository_appears(empty_dash, tmp_path)`: `ws_repo(tmp_path / "ws", "a")`. Then save `workspace.roots = [str(tmp_path / "ws")]` with the GET's sha. The answer is 200 with `rescanned is True` and `restart == []`. `/api/repos` lists `id_of(a)` as `live`. The user file parses to `{"workspace": {"roots": [...]}}`, and `a/.agentflow/config.yaml` is byte for byte as before.
- `test_the_page_can_never_write_notify_user_settings_host_or_a_protected_key(dash)`: each of `["hitl","notify"]`, `["agents","claude","user_settings"]`, `["dashboard","host"]`, `["gates","unit"]` and `["guardrails","forbidden_paths"]` sent to `…/save` (and `…/preview`) is 400, and the user file is still absent.
- `test_a_user_config_text_body_or_stale_sha_is_refused(dash)`: a body with `text` is 400. A wrong `base_sha256` is 409.
- `test_user_config_is_not_a_repository_route(two)`: `/api/repos/<a>/user-config` and `/api/repos/<a>/user-config/save` are 404.
- `test_a_user_save_reloads_every_repository_and_the_page_info(two)`: saving `defaults.run_budget_usd = 7` and then `dashboard.done_shown = 25` gives `two.dash.server.repos[x].cfg.defaults.run_budget_usd == 7` for both contexts, and `/api/info`'s `done_shown == 25`.
- `test_a_user_edit_that_breaks_a_live_repository_is_refused_naming_it(two)`: write `agents: {codex: null}` (or the substitute Task 1 found) into B's pack and reload B. Saving `agents.codex.models` is 400, with an `issues` entry whose message names B's `name`. The user file is unchanged.
- `test_a_port_or_a_workspace_change_on_a_single_repository_server_asks_for_a_restart(dash, tmp_path)`: saving `dashboard.port = 7799` and `workspace.roots = [str(tmp_path)]` answers `restart == ["dashboard.port", "workspace.roots"]` (in change order) and `rescanned is False`, and the server's repos are unchanged.
- `test_a_user_save_and_a_rescan_never_lose_each_other(wsd, monkeypatch)`: mirror `test_a_settings_save_and_a_rescan_never_lose_the_save`. Make `rescan` hold `_scan_lock` across a save. After both finish, the snapshot's `user` reflects the save.

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_dashboard.py --import-mode=prepend -q -k "user"`
Expected: the new tests FAIL (404 on `/api/user-config`). The existing tests pass.

- [ ] **Step 3: Implement the routes and `user_saved`**

In `_handle`, serve `USER_ROUTES` like `/api/repos`: before the default's refusal, with `self._ctx = None`. A GET on the first route and a POST on the other two are routed, and anything else is 404 with `close_connection`. `_scope` returns 404 for a `rest` of `user-config` or `user-config/…`.

The POST reads `self._body()` (the 64 KB limit). It calls `propose_user(base, changes, [(c.name, Path(c.cfg.path)) for c in live_contexts(self._snap)])`. A save then calls `save_user`, and when `proposal.changed`, `self.server.user_saved([".".join(c["key"]) for c in changes])`. `restart` is computed from the change keys and `server.workspace`. Log `user config saved from the Settings page (sha256 %s)`. Add the three routes to the module docstring, next to `/api/config`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_dashboard.py agentflow/tests/test_repo_context.py agentflow/tests/test_portability.py --import-mode=prepend -q`
Expected: PASS.

- [ ] **Step 5: Lint and commit**

```bash
uvx ruff@0.17.4 check .
git add agentflow/dashboard.py agentflow/tests/test_dashboard.py CHANGELOG.md
git commit -m "Serve the user settings form and its guarded save on /api/user-config, reloading every repository and rescanning on a workspace change.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: The page: Your settings, save badges and the empty start

**Files:**
- Create: `agentflow/dashboard_ui/user_settings.js`
- Modify: `agentflow/dashboard_ui/settings.js` (field widgets take a scope; `linesValue`, `saveBadge`; the badge on every row; the repository part hidden when there is nothing to show)
- Modify: `agentflow/dashboard_ui/main.js` (`show("settings")` also loads the user settings; the empty-start route)
- Modify: `agentflow/dashboard_ui/repos.js` (`NO_REPO_TEXT` points to Settings)
- Modify: `agentflow/dashboard.html` (the `#user-settings` block above the repository bar: heading **Your settings**, `#user-path`, `#user-status`, `#user-prompt` (hidden), `#user-form`, and the buttons `#user-reload`, `#user-discard`, `#user-review`; a `#settings-repo` wrapper with a heading naming the repository around today's bar and form; CSS for `.save-badge` and `textarea.lines`)
- Modify: `agentflow/dashboard.py` (`UI_SCRIPTS` gains `"user_settings.js"` right after `"settings.js"`)
- Modify: `CHANGELOG.md` (an `Added` line for the section and the badges, and a `Changed` line: with no repository the page opens on Settings)
- Test: `agentflow/tests/test_dashboard_js.py`, `agentflow/tests/test_dashboard.py` (the shell lists the new script)

**Interfaces:**
- Consumes: Task 2's routes and their bodies; `api`, `el`, `$`, `toast`, `dialog`, `diffNode` (core.js and dialogs.js); `ui.repos`, `repoName`, `nothingToShow`, `loadRepos`, `settingsMayLeave` (repos.js).
- Produces:
  ```js
  // settings.js (pure helpers first)
  const linesValue = (text) => string[]              // one trimmed entry per line, blank lines dropped
  function saveBadge(field, repoName) -> string      // "saves to: your user config" | "saves to: " + repoName
  function fieldRow(field, scope)                    // scope: {edits: Map, rows: Map, update: () => void}
  // user_settings.js
  const userSettings = { data: null, edits: new Map(), rows: new Map(), request: 0, update: updateUserBar }
  const emptyStart = (view) => boolean               // /api/repos lists no repository
  function userSaveNote(result) -> string            // the toast after a save, from `restart` and `rescanned`
  async function loadUserSettings()
  ```
  A field with `lines: true` renders as `textarea.lines` and sends `linesValue(textarea.value)`. A field with `editable: false` data renders disabled.

- [ ] **Step 1: Write the failing JS tests**

In `test_dashboard_js.py`, loading `[DOM_STUB, "settings.js"]` or `[DOM_STUB, "repos.js", "settings.js", "user_settings.js"]`:
- `test_each_settings_field_says_where_it_saves`: `saveBadge({target: "user"}, "billing") == "saves to: your user config"`, and `saveBadge({target: "project"}, "billing") == "saves to: billing"`.
- `test_a_lines_field_sends_one_entry_per_line`: `linesValue(" ~/a \n\n/b\n") == ["~/a", "/b"]`, and `linesValue("") == []`.
- `test_an_empty_workspace_starts_on_settings`: `emptyStart({repos: []})` is true. `emptyStart({repos: [{id: "a"}]})`, `emptyStart(null)` and `emptyStart({})` are false.
- `test_a_user_save_says_what_needs_a_restart`: `userSaveNote({restart: ["dashboard.port"], rescanned: false})` contains `dashboard.port` and `restart`. `userSaveNote({restart: [], rescanned: true})` contains `scanned`. `userSaveNote({restart: [], rescanned: false})` is `"Saved to your user config"`.
- `test_unsaved_user_edits_never_hold_up_a_repository_switch`: with `userSettings.edits` holding one entry, `settings.edits` empty, and `confirm` replaced by a function that throws, `settingsMayLeave()` returns true and `userSettings.edits.size` is still 1.

In `test_dashboard.py`, `test_the_page_shell_loads_user_settings_after_settings`: the shell's script tags list `user_settings.js` right after `settings.js`.

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_dashboard_js.py agentflow/tests/test_dashboard.py --import-mode=prepend -q -k "saves or lines or empty_workspace or user"`
Expected: the new tests FAIL (`saveBadge is not defined`, `user_settings.js` not found).

- [ ] **Step 3: Implement the section, the badges and the empty start**

In `settings.js`, thread `scope` through `setEdit`, `fieldInput`, `mapInput` and `fieldRow`. The repository form passes `{edits: settings.edits, rows: settings.rows, update: updateSettingsBar}`, so its behaviour doesn't change. `fieldRow` appends `el("span", {class: "save-badge", text: saveBadge(field, repoName(ui.repo === null && ui.repos && ui.repos.repos.length === 1 ? ui.repos.repos[0].id : ui.repo))})`. `loadSettings` hides `#settings-repo` and skips `/api/config` when `nothingToShow(ui.repos, ui.multi, ui.repo)`, and otherwise shows it with the repository's name in its heading.

`user_settings.js` mirrors `loadSettings`, `reviewSettings` and `discardSettings` for `/api/user-config`, form only, with its own `request` counter. It shows `data.error` in `#user-status` as text, and disables every input when `data.editable` is false. After a save it toasts `userSaveNote(result)`, reloads the user form, sets `ui.workflow = null` (models and budgets reach the Workflow tab), and awaits `loadRepos()`. When the repositories went from none to some, it also calls `loadInfo()` and `boardRefresh()`. `applyRepo` and `settingsMayLeave` leave `userSettings` alone.

In `main.js`, `show("settings")` also calls `loadUserSettings()` when `userSettings.edits` is empty. On the first `route()` after `loadRepos`, when `emptyStart(ui.repos)` holds, it replaces the hash with `#/settings`. The User section then shows `#user-prompt` ("Add the folder that holds your repositories, one path per line, like ~/code, then Review and save.") and focuses the `workspace.roots` textarea. `NO_REPO_TEXT` becomes `"No repository to show yet: add a folder under Settings → Your settings → Workspace, or start agentflow dashboard inside a repository."`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_dashboard_js.py agentflow/tests/test_dashboard.py agentflow/tests/test_portability.py --import-mode=prepend -q`
Expected: PASS. Then open a dashboard with `--port 0` in a scratch `XDG_CONFIG_HOME` with no workspace and no repository, and check by hand that the page lands on Settings, that adding a folder lists its repositories with no restart, and that every badge reads right.

- [ ] **Step 5: Lint and commit**

```bash
uvx ruff@0.17.4 check .
git add agentflow/dashboard_ui/user_settings.js agentflow/dashboard_ui/settings.js agentflow/dashboard_ui/main.js agentflow/dashboard_ui/repos.js agentflow/dashboard.html agentflow/dashboard.py agentflow/tests/test_dashboard_js.py agentflow/tests/test_dashboard.py CHANGELOG.md
git commit -m "Add the Your settings section with a save badge on every field, and open an empty dashboard on Settings to add a folder.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: Documentation, spec amendments and release 3.17

**Files:**
- Modify: `docs/agentflow-playbook.md` (§17: replace the line "Settings that save personal keys … (W4), not shipped yet." with a short "Settings" passage covering the two sections, the badges, the allow-list, what a broken user file shows, restart versus rescan, and the empty start. §14: one sentence after the user-file paragraph (around line 582) saying that the Settings page's Your settings section edits the allow-listed keys and never `hitl.notify`, `agents.claude.user_settings` or `dashboard.host`. The playbook's Settings-page passage: the badge, and the pointer from a read-only personal field)
- Modify: `docs/specs/agentflow-workspace-proposal.md` (the amendments from the plan decisions: section 2's broken-file sentence, the second row of the section 5 table, the routes in section 4.3's `/api/user-config` row, and the Security bullet's `workspace` exception. Mark W4 done in the Testing strategy's Settings bullet, as W3's bullets were marked)
- Modify: `CLAUDE.md` (the `config_edit.py` sentence: `USER_FIELD_KEYS` lists what the Your settings section may write to the user file)
- Modify: `pyproject.toml`, `agentflow/pack.py` (`3.16` → `3.17`), `CHANGELOG.md` (`## [Unreleased]` → `## [3.17] - <today>`, with a fresh empty `## [Unreleased]` above it)

**Interfaces:**
- Consumes: the behaviour of Tasks 1 to 3, as built.
- Produces: no code.

- [ ] **Step 1: Write the docs and the spec amendments**

State each behaviour as built. Where the build differs from a plan decision, the doc follows the build and the final report says so.

- [ ] **Step 2: Bump the version and release the changelog**

If `main` moved past 3.16, rebase and bump past it.

- [ ] **Step 3: Verify**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_packaging.py agentflow/tests/test_portability.py --import-mode=prepend -q`, then `bin/agentflow validate` (it must print `0 warnings`), then `uvx ruff@0.17.4 check .` (`All checks passed!`), then the full suite: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests --import-mode=prepend -q`.
Expected: all PASS.

- [ ] **Step 4: Commit**

```bash
git add docs/agentflow-playbook.md docs/specs/agentflow-workspace-proposal.md CLAUDE.md pyproject.toml agentflow/pack.py CHANGELOG.md
git commit -m "Document two-level Settings and release 3.17.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```
