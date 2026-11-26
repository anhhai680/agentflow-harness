# Workspace, slice W1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A developer sets `workspace.roots` (and optionally `workspace.repos`, `workspace.exclude`) in their user `config.yaml`, then runs `agentflow -C <path> <cmd>`, `agentflow status|inbox|agents|next|doctor --all` and `agentflow workspace list` from any directory.

**Architecture:** Three new personal keys join `layers.USER_KEYS`, and `layers.load_user_settings()` reads them (engine defaults plus the user file, no pack). A new leaf-ish module `repo_set.py` turns the settings into repositories and `Skipped` entries. `flow.py` gets `-C` (a `chdir` before dispatch), a `--all` loop that runs each existing command once per repository from inside it, and `workspace list`. No command's own code changes, so single-repository output stays byte-identical. The dashboard is not touched (W2 to W4).

**Tech Stack:** Python 3.12, stdlib + `pyyaml`, pytest (real tmp git repos, `capsys`, `monkeypatch.chdir`).

**Spec:** `docs/specs/agentflow-workspace-proposal.md` (approved 2026-11-26), slice **W1**: sections 1, 2, 3 and 8, and the Rollout row.

### What W1 does not do

- No dashboard change, no `/api/repos`, no repository switcher (W2, W3), no Settings page change beyond protecting the new key (W4).
- No `workspace add|scan|remove`: the workspace is edited by hand in the user file until W4.
- No `--all` on a command that writes (`run`, `queue`, `answer`, `approve`, …); they take `-C` only.
- No run cap, no merged digest (W5).

### Plan decisions

The spec leaves these open or gets them wrong; the spec was amended the same day to match.

- **The module is `agentflow/repo_set.py`, not `workspace.py`.** `agentflow/workspace.py` already exists (the per-run agent directory, `Run.agent_dir`) and `tests/test_workspace.py` tests it. The user-facing word and the config key stay `workspace`; the code names are `repo_set`, `RepoSet`, `RepoRef`, `Skipped`.
- **`--all` runs the existing command per repository.** It does `chdir` into each repository, calls the command's own function with `all=False`, restores the directory, and prints a `== <name> (-C <path>) ==` heading first. The commands are not refactored, so their single-repository output cannot change. The spec's "one table with a `REPO` column" becomes "each repository's output under its heading".
- **`-C <path>` is `os.chdir(path)` before dispatch**, as in `git -C`: a relative `--config` or path argument then resolves against the new directory.
- **Entry shapes are checked once, in `layers.workspace_settings`.** `roots` and `repos` entries must be `/…`, `~` or `~/…` (never `~user`, never relative), at most 256 entries per list; `exclude` entries are a path (same rule) or a bare folder name. `repo_set.resolve` can therefore assume a valid shape.
- **A pack that sets `workspace.*` is ignored, not an error.** `load_user_settings` reads the engine defaults and the user file only; `validate` warns when a pack set one.
- **Duplicates collapse silently.** Two entries that resolve to one checkout (a linked worktree under a root, say) are one repository. The spec's doctor line about them is dropped; `doctor` reports `Skipped` entries and repositories that are not set up.
- **`workspace` joins `config_edit._PROTECTED_KEYS`** (the repository form never writes it). The user-level form arrives in W4. `dashboard.board_refresh_s` and `dashboard.done_shown` keep their form fields; the form already shows a field read-only when the user file sets it.
- **Printed names are escaped.** A repository's folder name comes from the filesystem, so every name or path printed goes through `doctor.printable`.

## Global Constraints

- Engine dependencies stay stdlib + `pyyaml>=6`. No project strings in the engine (`test_portability.DENYLIST`).
- The golden `explain` files stay byte-identical; every existing single-repository command prints exactly what it printed.
- `layers.py` stays a leaf module: it imports nothing from `config.py` or from the engine's other modules.
- A scan reads directory names and `.git` / `.agentflow/config.yaml` existence only. It opens and runs nothing in a scanned repository; the only subprocess it starts is `git rev-parse --git-common-dir` through `pack.checkout_root`.
- `workspace.*` is a personal key: only the user file may set it. A repository's pack never carries it.
- `uvx ruff@0.17.4 check .` must print `All checks passed!` before every commit. Tests run with `uv run --no-project --with pyyaml --with pytest pytest <file> --import-mode=prepend -q`.
- Every user-visible change adds one line under `## [Unreleased]` in `CHANGELOG.md`; the release (3.13) is the last task.
- Commit messages are one imperative sentence ending with `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>` (the `project.commit_trailer` of this pack).
- `layers.py`, `config_edit.py` and `init.py` trigger the security overlay: plan and review with `security-and-hardening` and the `security-auditor` agent.

## Review Focus

1. **A hostile `workspace` value in the user file**: a relative path, `~otheruser/x`, a bare string where a list belongs, a non-string entry, an empty string, 257 entries, an unknown key under `workspace`. Each is a `LayerError` that names `workspace.<key>` and the user file, never a crash or a silent accept (Task 1).
2. **A root's children that are not plain repositories**: a hidden folder, a symlink to a repository elsewhere, a regular file, a folder with no `.git`. None is entered, followed or reported as a repository (Task 2).
3. **A root with thousands of children.** The scan stops at `MAX_CHILDREN` and says so; it never lists more than that (Task 2).
4. **A repository folder named with a newline or an escape sequence.** `--all` headings, `workspace list` and `doctor` print it escaped, one line each (Tasks 3, 4).
5. **One repository fails in `--all`** (a broken pack, or an unexpected exception): the loop goes on, the working directory is the one it started in, and the exit code is 1 (Task 3).

---

### Task 1: The personal keys, the user-level loader and the `validate` checks

**Files:**
- Modify: `agentflow/layers.py` (`USER_KEYS`, new `WorkspaceSettings`, `workspace_settings`, `UserSettings`, `load_user_settings`)
- Modify: `agentflow/config.py` (`Validator.workspace`, called next to `v.dashboard()` at line 3687)
- Modify: `agentflow/config_edit.py` (`_PROTECTED_KEYS`, line 52)
- Test: `agentflow/tests/test_layers.py`, `agentflow/tests/test_config.py`, `agentflow/tests/test_config_edit.py`

**Interfaces:**
- Consumes: `layers.base_layers`, `layers.merge`, `layers.check_user`, `layers.user_config_path`, `LayerError`.
- Produces:
  ```python
  class WorkspaceSettings(NamedTuple):
      roots: tuple[str, ...]; repos: tuple[str, ...]; exclude: tuple[str, ...]
  def workspace_settings(block: object) -> WorkspaceSettings  # None -> all empty; raises LayerError("workspace.<key>", msg)
  class UserSettings(NamedTuple):
      workspace: WorkspaceSettings
      dashboard: dict          # the merged `dashboard` block: host, port, and board_refresh_s / done_shown when set
      path: Path | None        # the user file, when it exists
  def load_user_settings() -> UserSettings  # engine defaults + user file; raises LayerError (the user file's name in the message)
  MAX_WORKSPACE_ENTRIES = 256
  ```

- [ ] **Step 1: Write the failing tests**

`test_layers.py`:
- `test_the_workspace_and_dashboard_keys_are_personal_settings`: `check_user({"workspace": {"roots": ["/a"], "repos": ["~/b"], "exclude": ["x"]}, "dashboard": {"board_refresh_s": 5, "done_shown": 20}}, {}, USER_FILE)` passes; `{"workspace": {"nope": []}}` raises `LayerError` (`not a personal setting`).
- `test_workspace_settings_reads_three_lists`: `workspace_settings({"roots": ["/a", "~/b"], "repos": ["~"], "exclude": ["tmp", "/c"]}) == WorkspaceSettings(("/a", "~/b"), ("~",), ("tmp", "/c"))`; `workspace_settings(None)` is three empty tuples.
- `test_workspace_settings_refuses_a_bad_shape` (parametrized): `{"roots": "/a"}`, `{"roots": [1]}`, `{"roots": [""]}`, `{"roots": ["rel/path"]}`, `{"roots": ["~otheruser/x"]}`, `{"repos": ["./x"]}`, `{"exclude": [""]}`, `{"roots": ["/a"] * 257}`, `{"unknown": []}`, and a non-mapping block (`["/a"]`). Each raises `LayerError` whose `path` starts with `workspace`. `{"exclude": ["bare-name"]}` is accepted.
- `test_load_user_settings_reads_the_user_file_and_not_a_pack`: with a user file at `layers.user_config_path()` holding `workspace.roots` and `dashboard.board_refresh_s: 5`, `load_user_settings()` returns those roots, a `dashboard` dict with `port` 7788 (the engine default) and `board_refresh_s` 5, and `path` equal to the file. With no user file: empty workspace, `path is None`. An empty file is fine.
- `test_load_user_settings_refuses_what_check_user_refuses`: a user file holding `gates:` raises `LayerError`; a file that is a list raises `LayerError`.

`test_config.py` (use `issues_for`, `errors`):
- `test_a_pack_that_sets_workspace_gets_a_warning_and_a_user_value_does_not`: `issues_for(tmp_cfg, {"workspace": {"roots": ["/x"]}})` has one `warning` at path `workspace.roots` that mentions "user config" and no error; a user file (`layers.user_config_path()` written first) setting it gives no issue.
- `test_a_malformed_workspace_block_is_an_error`: `{"workspace": {"roots": ["rel"]}}` gives `[("error", "workspace.roots")]`.
- `test_user_dashboard_keys_are_still_range_checked`: a user file with `dashboard.board_refresh_s: 0.1` makes `validate` report `dashboard.board_refresh_s`.
- `test_load_config_accepts_the_workspace_key_from_the_user_file`: `load_config(path).merged["workspace"]["roots"] == ["/x"]` and `agentflow config show --effective workspace` is exercised in Task 3's tests, not here.

`test_config_edit.py`:
- `test_workspace_is_protected`: `"workspace" in config_edit.PROTECTED` and `form_fields(shipped)["protected"]` has it; proposing a YAML text that adds `workspace: {roots: ["/x"]}` raises `Refused` matching `protected`.
- `test_a_dashboard_field_the_user_file_sets_is_read_only`: with the user file setting `dashboard.done_shown`, the field `["dashboard", "done_shown"]` in `form_fields(shipped)["sections"]` carries a `readonly` text that names the user config.

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_layers.py agentflow/tests/test_config.py agentflow/tests/test_config_edit.py --import-mode=prepend -q -k "workspace or user_dashboard or user_settings"`
Expected: FAIL (`WorkspaceSettings` and `load_user_settings` are not defined, `workspace` is not a personal key).

- [ ] **Step 3: Implement in `agentflow/layers.py`**

Add to `USER_KEYS`: `"workspace.roots", "workspace.repos", "workspace.exclude", "dashboard.board_refresh_s", "dashboard.done_shown"`. Implement `WorkspaceSettings`, `workspace_settings`, `UserSettings` and `load_user_settings` as above. `workspace_settings` rules: `None` is empty; a non-mapping is `LayerError("workspace", "must be a mapping of roots, repos and exclude")`; an unknown key is `LayerError("workspace.<key>", "unknown key; known: roots, repos, exclude")`; each value is a list of at most `MAX_WORKSPACE_ENTRIES` non-empty strings; a `roots` or `repos` entry, and an `exclude` entry that contains `/` or starts with `~`, must be `/…`, `~` or `~/…`. `load_user_settings` merges `base_layers(None)` with the user file (empty file counts as `{}`, read with `yaml.safe_load`; no home directory (`RuntimeError`) means no user file), runs `check_user` against the engine layers, and prefixes a `LayerError` raised for a user-file key with the file name as `check_user` does.

- [ ] **Step 4: Implement the `validate` check and the protected key**

`Validator.workspace()` in `agentflow/config.py`: read `self.cfg.merged.get("workspace")`; a `LayerError` from `layers.workspace_settings` becomes `self.error(exc.path, exc.message)`; for each of `roots`, `repos`, `exclude` whose origin (`layers.origin_of(self.cfg.layer_origins, "workspace.<key>")`) is `project` or a starter, add a warning at `workspace.<key>`: "is a personal setting and is ignored in a pack; set it in your user config (`<layers.user_config_path()>`)". Call it from `validate` beside `v.dashboard()`. Add `"workspace"` to `config_edit._PROTECTED_KEYS`.

- [ ] **Step 5: Run the tests to verify they pass**

Run the Step 2 command without `-k`.
Expected: PASS.

- [ ] **Step 6: Lint and commit**

```bash
uvx ruff@0.17.4 check .
git add agentflow/layers.py agentflow/config.py agentflow/config_edit.py agentflow/tests/test_layers.py agentflow/tests/test_config.py agentflow/tests/test_config_edit.py
git commit -m "Add the personal workspace keys and a user-level settings loader.

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 2: `repo_set.py`, the repositories a workspace holds

**Files:**
- Create: `agentflow/repo_set.py`
- Test: `agentflow/tests/test_repo_set.py`

**Interfaces:**
- Consumes: `layers.WorkspaceSettings`, `layers.load_user_settings`, `layers.LayerError`, `pack.checkout_root`, `pack.repo_id`, `pack.PACK_DIR`.
- Produces:
  ```python
  MAX_CHILDREN = 2000
  @dataclass(frozen=True)
  class RepoRef: id: str; name: str; path: Path; state: Literal["live", "not_set_up"]
  @dataclass(frozen=True)
  class Skipped: entry: str; reason: str
  @dataclass(frozen=True)
  class RepoSet:
      settings: layers.WorkspaceSettings; repos: tuple[RepoRef, ...]; skipped: tuple[Skipped, ...]
      @property
      def live(self) -> tuple[RepoRef, ...]
  class NoWorkspace(Exception)                       # str(exc): "no workspace: set workspace.roots in <user config path>"
  def resolve(settings: layers.WorkspaceSettings) -> RepoSet
  def from_user_config() -> RepoSet                  # raises NoWorkspace (roots and repos both empty) or layers.LayerError
  ```

- [ ] **Step 1: Write the failing tests**

Helpers in the test file: `make_repo(parent, name, pack=True)` runs `git init -q -b main` in `parent/name` and, with `pack`, writes `.agentflow/config.yaml` (any text; existence is all `repo_set` reads).
- `test_a_root_lists_its_child_repositories_one_level_deep_in_name_order`: root holds `b`, `a` (repos), `c/nested` (a repo two levels down), a plain folder `docs`, a file `f.txt`. `resolve(WorkspaceSettings((str(root),), (), ()))` gives repos named `a`, `b` in that order, `state == "live"`, `id == pack.repo_id(path)`, `path` resolved; `c` and `docs` are not repositories and not `Skipped`.
- `test_a_repository_with_no_pack_is_not_set_up`: `make_repo(root, "x", pack=False)` gives `state == "not_set_up"`; `live` omits it.
- `test_hidden_folders_and_symlinks_are_never_entered`: `.hidden` (a repo) and `link` (a symlink to a repo outside the root) are absent from `repos` and from `skipped`.
- `test_a_linked_worktree_collapses_into_its_main_checkout`: a worktree child of the root whose main checkout is also a child gives one `RepoRef` (the main checkout's path); one whose main checkout is outside the root gives that main checkout.
- `test_repos_entries_join_and_are_deduplicated`: a repo listed in `repos` that is also under a root appears once; one outside any root appears after the root's repos.
- `test_exclude_matches_a_name_or_a_path`: `exclude=("a",)` hides the repository named `a`; `exclude=(str(path_b),)` hides `b`.
- `test_a_missing_root_and_a_non_checkout_repos_entry_are_skipped_with_a_reason`: `Skipped(entry=<the entry as written>, reason=...)` for each; the rest still resolve.
- `test_two_repositories_with_one_folder_name_are_told_apart`: `p1/app` and `p2/app` become `app (p1)` and `app (p2)`.
- `test_the_scan_stops_at_max_children`: `monkeypatch.setattr(repo_set, "MAX_CHILDREN", 3)` and five repos give three repositories and one `Skipped` whose reason says the rest were ignored.
- `test_resolve_starts_only_git_rev_parse`: record `subprocess.run` argv lists during `resolve`; every one equals `["git", "rev-parse", "--git-common-dir"]`.
- `test_from_user_config_reads_the_user_file`: write `workspace.roots` to `layers.user_config_path()`; `from_user_config().repos` is as expected.
- `test_no_workspace_names_the_user_file`: with no user file, `from_user_config()` raises `NoWorkspace` and `str(exc)` contains `layers.user_config_path()` and `workspace.roots`.

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_repo_set.py --import-mode=prepend -q`
Expected: FAIL (`ModuleNotFoundError: agentflow.repo_set`).

- [ ] **Step 3: Implement `agentflow/repo_set.py`**

Rules, in this order. Expand `~` with `Path(entry).expanduser()`. A `roots` entry that is not a directory is `Skipped("is not a folder")`. A root's children come from `os.scandir`, sorted by name, at most `MAX_CHILDREN` examined (then one `Skipped(root, "more than N entries; the rest are ignored")`); a child counts only when it is not hidden, not a symlink, a directory and holds `.git` (a folder or a file). A `repos` entry must be an existing directory holding `.git`, else `Skipped("is not a git checkout")`; it is resolved even when it is a symlink, because the developer named it. Every candidate goes through `pack.checkout_root`, and a candidate already seen (by resolved path) is dropped silently, first one wins: roots in order, then `repos`. `state` is `live` when `<checkout>/.agentflow/config.yaml` is a file, else `not_set_up`. `exclude` is applied after resolution: an entry with `/` or a leading `~` is resolved and compared with the checkout path, any other entry with the folder name. Names are the folder name, made `name (parent folder)` for each repository whose name another repository shares. `from_user_config` calls `layers.load_user_settings()` and `resolve`, and raises `NoWorkspace` when both `roots` and `repos` are empty. Do not read or open anything inside a repository.

- [ ] **Step 4: Run the tests to verify they pass**

Run the Step 2 command. Expected: PASS.

- [ ] **Step 5: Lint and commit**

```bash
uvx ruff@0.17.4 check .
git add agentflow/repo_set.py agentflow/tests/test_repo_set.py
git commit -m "Resolve the workspace's repositories from the user's settings.

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 3: `-C`, `--all` and `workspace list`

**Files:**
- Modify: `agentflow/flow.py` (`build_parser`, `main`; new `_run_all`, `cmd_workspace_list`)
- Test: `agentflow/tests/test_workspace_cli.py` (create)

**Interfaces:**
- Consumes: `repo_set.from_user_config`, `repo_set.NoWorkspace`, `layers.LayerError`, `doctor.printable`, each command's existing `cmd_*` function.
- Produces: the top-level `-C PATH`; `--all` on `status`, `inbox`, `agents`, `next`, `doctor`; `agentflow workspace list [--json]`. JSON shape: `{"repos": [{"id", "name", "path", "state"}], "skipped": [{"entry", "reason"}]}`.

- [ ] **Step 1: Write the failing tests**

The test file builds a workspace in `tmp_path`: `ws/alpha` and `ws/beta` (git repos with the `SPARSE_PACK` from `conftest`, written as `.agentflow/config.yaml`), `ws/gamma` (a git repo with no pack), and a user file at `layers.user_config_path()` with `workspace: {roots: [<ws>]}`. A helper `cli(*argv)` runs `flow.main(list(argv))` and returns `(code, capsys.readouterr().out)`; tests start from `monkeypatch.chdir(tmp_path)` (not a repository).
- `test_dash_c_runs_the_command_in_that_repository`: `cli("-C", str(alpha), "status")` equals the output of `monkeypatch.chdir(alpha)` then `cli("status")`.
- `test_dash_c_with_a_bad_path_is_an_error`: `-C <missing>` returns 1 and prints `error:` naming the path.
- `test_status_all_runs_once_per_repository_under_a_heading`: `cli("status", "--all")` returns 0; output holds, in order, `== alpha (-C <path>) ==`, `No runs yet.`, `== beta …`, `No runs yet.`, `== gamma …` and `not set up`.
- `test_all_leaves_the_working_directory_alone`: `Path.cwd()` is the same before and after, including after the broken-pack test below.
- `test_a_broken_pack_does_not_stop_the_others`: write invalid YAML into `beta`'s pack; `cli("agents", "--all")` returns 1, the output has `beta`'s `error:` line and `alpha`'s agents table, and `gamma` still follows.
- `test_an_unexpected_exception_in_one_repository_is_one_error_line`: `monkeypatch.setattr` the `agents` command's view function to raise `RuntimeError("boom")` for `alpha` only; the output has `error: alpha: RuntimeError: boom`, `beta` still runs, code 1.
- `test_all_with_no_workspace_says_where_to_set_it`: no user file gives code 1 and a message with `workspace.roots`.
- `test_all_refuses_c_config_watch_and_a_run_argument`: `-C x status --all`, `status --all --config f`, `status --all --watch` and `status <run> --all` each return 1 and print why, running nothing.
- `test_inbox_all_and_next_all_and_doctor_all_run_per_repository`: `inbox --all` prints `Nothing is waiting on you.` under each live heading; `next --all` and `doctor --all` print a heading per live repository (`doctor` may return 1 on this machine's missing tools: assert the headings, not the code).
- `test_a_folder_name_with_a_newline_is_printed_escaped`: a repository folder named `evil\nname`; the heading line holds `evil\\nname` and no raw newline inside it.
- `test_workspace_list_text_and_json`: `workspace list` prints the roots, each repository with `live` / `not set up` and its path, and a `skipped` line for a missing `repos` entry; `--json` parses to the documented shape; with no workspace it returns 1 with the same message as `--all`.
- `test_config_show_effective_reports_the_workspace_layer`: `cli("-C", str(alpha), "config", "show", "--effective", "workspace")` prints `workspace.roots: ["…"]  [user]`.

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_workspace_cli.py --import-mode=prepend -q`
Expected: FAIL (`unrecognized arguments: -C`).

- [ ] **Step 3: Implement in `agentflow/flow.py`**

`build_parser`: add `parser.add_argument("-C", dest="chdir", metavar="PATH", help="run as if started in PATH (like `git -C`)")`; add `--all` (`store_true`, help "run once for every repository of your workspace (`workspace.roots` in your user config)") to `status`, `inbox`, `agents`, `next` and `doctor`; add `workspace` with a required `list` sub-command (`--json`). `main`: if `args.chdir`, `os.chdir` it (an `OSError` prints `error: -C <path>: <reason>` and returns 1); then, when `getattr(args, "all", False)`, return `_run_all(args)`. `_run_all(args) -> int`: refuse `-C`, `--config`, `--watch` and a `run` argument with one printed reason each (return 1); load `repo_set.from_user_config()` (a `NoWorkspace` or `LayerError` prints `error: <message>` and returns 1); for each repository print `== <printable name> (-C <printable path>) ==`; a `not_set_up` one prints `not set up: no .agentflow/config.yaml here (run agentflow init in it)` and is skipped; a live one runs `args.func(Namespace(**{**vars(args), "all": False}))` after `os.chdir(path)`, in a `try` that catches `Exception` (print `error: <name>: <Type>: <message>`, count as failed) with `finally: os.chdir(start)`; finish by printing each `Skipped` as `skipped <entry>: <reason>` and return 1 when any command returned non-zero. `cmd_workspace_list` prints and returns as the tests say. Lazy-import `repo_set` and `layers` inside the functions, as the module does for the rest.

- [ ] **Step 4: Run the tests to verify they pass**

Run the Step 2 command, then `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_pack.py agentflow/tests/test_control_cmds.py --import-mode=prepend -q` (the existing CLI tests).
Expected: PASS.

- [ ] **Step 5: Lint and commit**

```bash
uvx ruff@0.17.4 check .
git add agentflow/flow.py agentflow/tests/test_workspace_cli.py
git commit -m "Add -C, --all on the read-only views and workspace list to the CLI.

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 4: `doctor` and `init` say what the workspace holds

**Files:**
- Modify: `agentflow/doctor.py` (`run_checks`, new `_workspace_check`)
- Modify: `agentflow/init.py` (the `next steps` block, new `_workspace_note`)
- Test: `agentflow/tests/test_doctor.py`, `agentflow/tests/test_init.py`

**Interfaces:**
- Consumes: `repo_set.from_user_config`, `repo_set.NoWorkspace`, `layers.LayerError`, `doctor.printable`, `Check`.
- Produces: `doctor._workspace_check() -> Check | None` (`None` when no workspace is configured); `init._workspace_note(repo: Path, out) -> None`.

- [ ] **Step 1: Write the failing tests**

`test_doctor.py`:
- `test_doctor_has_no_workspace_check_without_a_workspace`: the existing `{c.name: c for c in run_checks(cfg, repo)}` has no `workspace` key (conftest keeps the user file empty).
- `test_doctor_workspace_check_is_ok_when_every_repository_is_live`: user file with a root holding two packed repositories gives `Check("workspace", "ok", …)` whose detail says `2 repositories`.
- `test_doctor_workspace_check_warns_on_a_skipped_entry_or_a_repository_not_set_up`: a missing `repos` entry, or a pack-less repository, gives `status == "warn"` with the reason and the fix (`edit workspace.* in <user file>` / `agentflow init`); a folder name with a newline is printed escaped.
- `test_doctor_workspace_check_warns_when_the_user_file_does_not_load`: a user file with a bad `workspace` value gives `warn`, detail naming the key.

`test_init.py`:
- `test_init_notes_a_repository_outside_the_workspace`: with a workspace that does not hold the repository, the output ends with one `note:` line naming `workspace.roots` and the user file; with the repository inside the workspace, or with no workspace configured, there is no such line (existing `init` tests stay green and unchanged).

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_doctor.py agentflow/tests/test_init.py --import-mode=prepend -q -k workspace`
Expected: FAIL.

- [ ] **Step 3: Implement**

`_workspace_check`: `None` when `NoWorkspace`; a `LayerError` gives `Check("workspace", "warn", printable(message), "fix workspace.* in <user file>")`; otherwise `ok` with `"<n> repositories"` when nothing is skipped or not set up, else `warn` listing each `Skipped` (`entry: reason`) and each not-set-up name, with fix "edit workspace.* in <user file>, or run agentflow init in a repository that is not set up". Append it in `run_checks` after the state dir check. `_workspace_note`: when a workspace is configured and `repo` (resolved) is not among `repos` paths, print `note: this repository is not in your workspace; add its folder to workspace.roots in <user file> to include it in --all views` after the numbered next steps; any `NoWorkspace` or `LayerError` prints nothing.

- [ ] **Step 4: Run the tests to verify they pass**

Run the Step 2 command without `-k`. Expected: PASS.

- [ ] **Step 5: Lint and commit**

```bash
uvx ruff@0.17.4 check .
git add agentflow/doctor.py agentflow/init.py agentflow/tests/test_doctor.py agentflow/tests/test_init.py
git commit -m "Report the workspace in doctor and note a repository outside it in init.

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Documentation and the 3.13 release

**Files:**
- Modify: `docs/agentflow-playbook.md` (§10 cheat sheet, §14 pack reference, a new `## 17. Several repositories`)
- Modify: `CHANGELOG.md`, `pyproject.toml`, `agentflow/pack.py`
- Test: `agentflow/tests/test_packaging.py` (existing), `agentflow/tests/test_portability.py` (existing)

- [ ] **Step 1: Write the playbook text**

§10: add `agentflow -C <path> <cmd>`, `agentflow status|inbox|agents|next|doctor --all` and `agentflow workspace list` lines to the cheat sheet. §14: in the user-file sentence (the one listing `dashboard.port` and the usage-window keys) add `workspace.roots`, `workspace.repos`, `workspace.exclude`, `dashboard.board_refresh_s` and `dashboard.done_shown`, and add one line per new key: `workspace.roots` (folders whose immediate child git repositories join the workspace; `/…`, `~` or `~/…`; at most 256), `workspace.repos` (repositories by path), `workspace.exclude` (a path or a folder name), each "user file only; a pack that sets it gets a validate warning; protected". §17 "Several repositories": the user file example, what `--all` prints (one heading per repository, each command unchanged inside it), `-C`, `workspace list`, that write commands are single-repository, and one paragraph saying the dashboard still serves one repository until slices W2 to W4 ship.

- [ ] **Step 2: Release**

`CHANGELOG.md`: under `## [Unreleased]` add `Added` lines for the three workspace keys, `-C`, `--all`, `workspace list` and the `doctor` and `init` notes; then rename it `## [3.13] - 2026-11-26` and open a fresh empty `## [Unreleased]`. Set `version = "3.13"` in `pyproject.toml` and `ENGINE_VERSION = "3.13"` in `agentflow/pack.py`.

- [ ] **Step 3: Run the checks**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_packaging.py agentflow/tests/test_portability.py agentflow/tests/test_explain_golden.py --import-mode=prepend -q`, then `bin/agentflow validate` (it must print `0 warnings`), then `uvx ruff@0.17.4 check .` (`All checks passed!`).
Expected: PASS.

- [ ] **Step 4: Run the full suite once**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests --import-mode=prepend -q` (25 to 60 minutes; run it in the background).
Expected: PASS. Any failure in a test W1 did not touch is a regression in a shared fixture or in `flow.main`: fix it before the commit.

- [ ] **Step 5: Commit**

```bash
git add docs/agentflow-playbook.md CHANGELOG.md pyproject.toml agentflow/pack.py
git commit -m "Document the workspace and release 3.13.

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```
