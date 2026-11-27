# Workspace, slice W2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One `agentflow dashboard` process, one port and one token serves every repository of the user's workspace, from any directory. Every repository stays live in the background, the API reaches each one under `/api/repos/<id>/…`, and the page and the legacy routes keep showing the default repository.

**Architecture:** A new module `repo_context.py` holds `RepoContext` (one repository's `repo`, `cfg`, `config` and state together), the pure rescan that turns a `RepoSet` into a map of contexts, and the start-up decision table. `DashboardServer` holds that map (replaced whole on each rescan, never mutated), and each request resolves exactly one context from the server's own map before any handler runs. Every existing handler reads `ctx.repo` and `ctx.cfg` instead of `server.repo` and `server.cfg`; `make_server(repo, cfg, …)` wraps its pair in a one-entry map, so every single-repository caller and test is unchanged. The page's scripts are not touched (W3).

**Tech Stack:** Python 3.12, stdlib + `pyyaml`, pytest (real tmp git repos, `http.client` against a real server on port 0).

**Spec:** `docs/specs/agentflow-workspace-proposal.md` (approved 2026-11-26), slice **W2**: sections 4.1 to 4.3, 6 (nothing: W3), 7 and the Rollout row, plus the section "Before W2 starts".

**Order and execution (decided with the user, 2026-11-26):** the PRs land in sequence, each branched from an up-to-date `main`: #60 (W1, 3.13) merges first, then `docs/plans/2026-11-26-agentflow-harness-pack-size-bound.md` (3.14), then this plan (3.15). If #60 stalls, the pack-size bound may be stacked on `feat/workspace-w1` as 3.14. This plan is executed with `subagent-driven-development` (a fresh implementer and reviewer per task, with the `security-auditor` agent on the dashboard tasks).

### What W2 does not do

- No page change: no switcher, no `repos.js`, no `core.js` prefix, no tab title (W3). The page and the legacy routes show the default repository.
- No merged Inbox and Agents (see the first plan decision), no notification-title change (W3).
- No `/api/user-config`, no User settings section, no empty-state page, no `workspace.*` writes (W4). `server.rescan()` is the call W4's save will make.
- No run cap and no merged digest (W5).

### Plan decisions

- **Prefix routes map onto the legacy paths.** `/api/repos/<id>/<rest>` is served as the legacy `/api/<rest>` with that repository's context (`/api/repos/<id>/runs/<run>` is `/api/runs/<run>`). The dispatcher rewrites the path once, so no route's own matching changes, and it sets `self._scoped = True` so a handler that must tell `/api/repos/<id>/events` from the legacy `/api/events` can. `<rest>` may not be `repos` or start with `repos/`. `<id>` is percent-decoded and looked up in the server's map; it is never joined to a path.
- **A legacy path stays the default repository's, byte for byte, until W3 moves the page** (the user accepted this departure from the spec's W2 scope, 2026-11-26). The spec's table has the legacy `/api/inbox`, `/api/agents` and `/api/events` return every repository's items or events with a `repo` field when several are live. The W2 page can't use that: it shows another repository's inbox rows, which its actions cannot reach, and `main.js`'s `onStreamMessage` marks the Board and the open run dirty for every message's `run_id` (run ids are `YYYYMMDD-HHMM-slug`, so a run of the same module and minute in another repository would refresh the default's card). So in W2: `/api/inbox`, `/api/agents` and `/api/events` serve the default repository, untagged; each repository's own copies are `/api/repos/<id>/inbox|agents|events` (events tagged with `repo`); and the merged, tagged stream is `/api/events?all=1` (every `live` and `missing` context). W3 adds the merged inbox and agents and switches the page to `?all=1`; Task 8 amends the spec's W3 row and section 4.3 to say so. A repository that joins an open stream starts at its logs' current end, so its history is never replayed.
- **One snapshot, swapped whole.** The server's `repos`, `default`, `skipped`, `user` and `user_error` live in one frozen `Snapshot` assigned to `server.snap` in a single statement; a request reads `snap = server.snap` once and uses only that. `repos`, `default`, `skipped` and `user_error` on the server are read-only properties of the current snapshot. A rescan builds the next snapshot off to the side (the scan and the pack loads happen before the swap) and takes `_scan_lock` only to keep two rescans from interleaving.
- **Non-live contexts stay in the map.** A context is `live`, `not_set_up`, `missing` or `error` (its pack failed to load, or it shares a runs directory with an earlier repository; the text is kept and shown in `/api/repos`, and a rescan tries again). A per-repository route on any state but `live` answers 409 with the reason, except `/api/info`, which answers from the user settings when the context has no `cfg`. An unknown id is 404. A legacy route with no default is 404 `no repository`. The default is **sticky**: once a snapshot has a default id it never moves to another repository (the W2 page names no repository, so a silent switch would send an Approve or a Start meant for A to B, under B's pack). It is chosen once, when it is `None`: the preferred repository if `usable`, else the first `usable` one. While the default's context is not `usable` (a pack removed by a `git checkout`, a pack that broke) its legacy routes answer 409 `<name> is not live: <reason>`, and when it has left the workspace they answer 404 `no repository`; it recovers by itself when the repository does. A restart, or W3's explicit switcher, is how the page changes repository.
- **A scanned repository's files are untrusted input, so per-repository work never raises into the server.** Loading a pack (a `.agentflow` that is a symlink is an `error`), counting runs and checking for live runners catch every `Exception` and turn it into an `error` context, zero counts or a kept `missing` context (`in_flight` answers `True` when it cannot tell). A pack with invalid UTF-8, a corrupt `events.jsonl` or a hostile `paths.state_dir` costs that repository its place, never the dashboard's start-up, the rescan thread or `/api/repos`. Counting is per run: a run that can't be loaded is skipped and the others still count.
- **A runs directory belongs to one context, checked at build time and on every use.** Every context records `runs`, its resolved runs directory, when it is built (`load_context`, `reconcile`, `plan_start`); a directory that can't be resolved (`~nosuchuser`, a NUL byte) makes the context `error`. `settle_dirs(contexts)` (given every context, `missing` ones included, and returning copies via `dataclasses.replace`, never mutating a context a request may hold) then refuses three things: a workspace context whose runs directory lies inside its own checkout (a clone can ship a state directory holding a run that waits on an environment stop, which the background passes would resume; decided with the user, 2026-11-26; the CLI and a single-repository dashboard are unaffected), a runs directory that lies under `pack.user_state_root()` but is not the context's own default `<repo id>` directory is refused (that is how a pack aims at another repository's state, served, excluded or not), and a directory held by several contexts stays with the one for which it is its own default (`pack.user_state_root()/<repo id>/runs`), and every other holder becomes `error` naming it; when none holds it at its default, all of them become `error`. `usable(ctx)` is `ctx.state == "live"` and `runs_dir(ctx) == ctx.runs`; `runs` is `None` for a single-repository server (`make_server`, `--config`), whose `usable` is just `live`, so today's behavior of following a changed `paths.state_dir` is untouched; every handler and every background pass (`live_contexts`, reminders, refresh, `prepare`, the streams) uses `usable`, so a pack edited after load (a pull that changes `paths.state_dir`) is refused at once with 409 `its state directory changed; waiting for the next rescan`, and the next rescan rebuilds that context. `plan_start` runs the same `settle_dirs`, so the guard holds from the first request, not from the first rescan. A run id of one repository is never reachable through another.
- **`missing` means in flight.** A repository that leaves the resolved set is kept as `missing` while any of its runs holds a live runner lock, and dropped once none does. A missing context's routes answer 409; its runs still stream on `/api/events?all=1` (the legacy stream is the default repository's alone).
- **`server.repo`, `server.cfg` and `server.config` stay** as properties on the default context (getter and setter for `cfg`), because 19 existing test lines assign and read them.
- **Rescan is explicit and workspace-only.** `DashboardServer.rescan()` re-reads the user file, resolves it and swaps in a new map. A thread calls it every `RESCAN_EVERY_S = 60.0` in workspace mode only. A dashboard started in a repository with no workspace (today's behavior) never rescans.
- **`--config` keeps meaning one repository.** With `--config` the dashboard serves only the repository it starts in, with that pack, as today; the workspace is not read.
- **`prepare` (settling an interrupted card start) runs at start-up for the contexts live then, not for a repository that joins later.** A later one would be settled while another dashboard might be mid-start in it. The same hazard exists at start-up when a second dashboard is started (the spec's `use --port 0` hint invites it): its `prepare` marks the first dashboard's in-flight card starts failed, now in every repository rather than one. Accepted as the spec's design, and the playbook says so (Task 8).
- **Trade-offs the review raised and the plan accepts.** (a) Every repository under a root joins the background work (reminders, environment rechecks, refresh), not only the one the user chose: this is spec decision 12 and Assumption 5, and `workspace.exclude` is the opt-out. (b) The refresh round is sequential by design, so one slow repository delays the next repositories' refresh in that round, and the reminder and recheck pass is one thread too, so a repository on a hung mount delays the others' reminders; `backlog`'s process-wide start lock also serializes card starts across repositories. Success criterion 8 of the spec is reworded accordingly (Task 8): a failing repository never stops another, a slow one may delay it. (c) A live context keeps the config it loaded; a changed user file reaches a context when its pack is next loaded (a Settings save, or a rescan after an error). (d) `/api/repos` reads every repository's runs on each call; W3 must cache the counts or feed them from the stream before the page polls it. (e) `rescan` holds its lock through the scan, so a W4 save waits for at most one scan (up to `MAX_CHILDREN` `git rev-parse` calls per root).
- **A pack's size is bounded by a separate PR that ships first (decided with the user, 2026-11-26).** `docs/plans/2026-11-26-agentflow-harness-pack-size-bound.md` releases 3.14: a pack whose nested aliases expand past `layers.MAX_EXPANDED_NODES` (100,000 values; the largest shipped config is 845) is a load error, for the CLI as well. W2 depends on it, because W2 loads every repository's pack at start-up and on every rescan, and it is released as 3.15.
- **The "Before W2 starts" items are settled** as: a root's child whose main checkout is outside that root is `Skipped` (Task 1); a child git refuses (a submodule, a separate git dir) is `Skipped` with its folder name (Task 1); `UserSettings.dashboard` is range-checked by `info_numbers` and `port_of` (Task 7). Each `Skipped` line is printed by `workspace list` and `--all`, which already print every `Skipped`.
- **A port in use** adds the hint `another dashboard may be running; use --port 0` to the existing OSError line, in both modes.

## Global Constraints

- Engine dependencies stay stdlib + `pyyaml>=6`. No project strings in the engine (`test_portability.DENYLIST`).
- Single-repository behavior is byte-identical: `make_server(repo, cfg, …)`, every legacy route, `/api/info`'s keys and the page. The existing `test_dashboard.py`, `test_dashboard_js.py` and the golden `explain` files pass unchanged.
- The dashboard binds `127.0.0.1` only, with one 128-bit token, the same-site check on every request and the token never logged.
- No request value becomes a path. A repository is reached only by an id the server's own map holds, and a handler uses one `RepoContext`'s `repo`, `cfg` and `config` together, never a mix.
- A rescan's scan reads directory names and `.git` and pack-file existence, and starts only `git rev-parse --git-common-dir` (as `repo_set` does). Building a context then reads that repository's pack with `load_config` and reads its runs, as the CLI does, and every such read is guarded (see the plan decision on untrusted files). The scan and the loads execute nothing in a repository; the background passes are the existing per-repository jobs (`git fetch` and `gh` in the refresh, notification commands in the reminders, environment rechecks that resume runners) now run for every usable repository, as the spec decided.
- Every repository name, setting entry, reason or exception text that reaches the terminal goes through `doctor.printable`, and `RepoContext.error` is cut to 300 characters.
- `uvx ruff@0.17.4 check .` must print `All checks passed!` before every commit. Tests run with `uv run --no-project --with pyyaml --with pytest pytest <file> --import-mode=prepend -q`.
- Every user-visible change adds one line under `## [Unreleased]` in `CHANGELOG.md`; the release (3.15, after the pack-size-bound PR's 3.14) is the last task. Commit messages are one imperative sentence ending with the trailer the executing session is given (`project.commit_trailer` inside an agentflow run; in an interactive session the harness's, `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`, as in the commands below).
- W2 changes dashboard request handling and token scope: plan and review it with `security-and-hardening` and the `security-auditor` agent (spec, Security). `repo_set.py` and `dashboard.py` are not on the overlay's path list; the review is by the spec's rule.

## Review Focus

1. **A repository id that is not one.** `/api/repos/..%2f..%2fetc/board`, `/api/repos/%00/board`, an id of 5000 characters, an empty id (`/api/repos//board`), another repository's id with different case: each is a 404 and reads no file (Task 4).
2. **A run id of repository A sent to B, and a handler that still reads the default's `cfg`** (`/api/repos/<B>/runs/<A's run>`, and the same for an action POST, including a run id that exists in both; every route that reads a repository, compared A against B): 404, and nothing changes in A or B (Tasks 3, 4).
3. **A repository whose files are hostile or broken** (no pack, invalid UTF-8 in the pack, a corrupt `events.jsonl`, a `paths.state_dir` that points at another repository's state, at start-up and after a later edit, a folder whose name is not valid UTF-8, a directory that vanished): the server starts, `/api/repos` answers, the rescan thread lives, the others serve, and fixing the repository brings it back on the next rescan (Tasks 2, 4, 6).
4. **The stream and the unchanged page:** the legacy `/api/events` carries only the default repository's events, untagged; the merged stream tags every message; a repository that disappears while a run is live stays `missing`, and one that joins an open stream is not replayed (Tasks 2, 5).
5. **One repository's refresh, reminder or pack raising:** the others still run in the same round, the log line names the failing repository, and a start repository that isn't in the workspace is said so rather than dropped silently (Tasks 6, 7).

---

### Task 1: A root's child must stay inside its root, and a refused child is reported

**Files:**
- Modify: `agentflow/repo_set.py` (`resolve`, `_children`)
- Modify: `CHANGELOG.md` (one `Changed` line under `[Unreleased]`: `workspace list` and `--all` now report a root's child that git refuses, whose main checkout is outside the root, or whose path is not valid UTF-8)
- Test: `agentflow/tests/test_repo_set.py`

**Interfaces:**
- Consumes: `pack.checkout_root`, `Skipped`, `_main_checkout`.
- Produces: `resolve` behaves as before, plus two new `Skipped` lines (`entry` is the root as written, `reason` begins with the child's folder name). No new name or signature.

- [ ] **Step 1: Write the failing tests**

- `test_a_linked_worktree_collapses_into_its_main_checkout` (line 78) changes, because it builds `root/wt-out`, a worktree of `outer/main-out`, and asserts `main-out` joins: it is now skipped. The result is `[main-in]` (`names(...) == ["main-in"]`), `wt-in` still collapses silently, and the one `Skipped` has `entry == str(root)` and a `reason` that starts `wt-out:` and contains `outside this root`.
- `test_a_root_reached_through_a_symlink_keeps_its_children`: `link -> root` (the setting names `link`); both children of `root` are listed and `skipped == ()`.
- `test_a_submodule_is_not_a_checkout` (line 242) changes, because its two last assertions encode the silence this task ends: with `repos=[sub]` the skipped entries are now `[str(root), str(sub)]` (the root child first, because root children are candidates before `repos` entries: its `reason` starts `sub:` and contains `not a standalone git checkout`; then the named entry, `is not a git checkout`), and `resolve(settings([root])).skipped` is the single root line instead of `()`. The repository list (`["ok", "sup"]`) is unchanged.
- `test_a_folder_whose_name_is_not_valid_utf8_is_skipped`: `_main_checkout` is monkeypatched to return `Path(os.fsdecode(b"/x/bad\xff"))` for a real root child (no filesystem needs to accept the name, so the test runs on macOS too): the repository is not in `repos`, and one `Skipped` says its path is not valid UTF-8 (such a name can't be put in JSON or addressed in a URL).

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_repo_set.py --import-mode=prepend -q`
Expected: the changed worktree and submodule tests and the new UTF-8 test FAIL; the symlink test already passes (it pins behavior the change must keep).

- [ ] **Step 3: Implement in `agentflow/repo_set.py`**

`resolve` keeps, for each root child, the root it came from (the resolved root path and the entry as written). After `_main_checkout(child)`: `None` appends `Skipped(root_entry, f"{child.name}: not a standalone git checkout (a submodule, or git refused it)")`; a checkout whose `.parent` is not the resolved root appends `Skipped(root_entry, f"{child.name}: its main checkout {checkout} is outside this root")`; both `continue`. A checkout whose path does not encode as UTF-8 appends `Skipped(<entry or root entry>, "<name>: its path is not valid UTF-8")` and is dropped. `workspace.repos` entries are otherwise unchanged (the developer named them).

- [ ] **Step 4: Run the tests to verify they pass**

Run the Step 2 command. Expected: PASS. Then `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_workspace_cli.py --import-mode=prepend -q`; Expected: PASS (the `skipped` lines print through the existing loops).

- [ ] **Step 5: Lint and commit**

```bash
uvx ruff@0.17.4 check .
git add agentflow/repo_set.py agentflow/tests/test_repo_set.py CHANGELOG.md
git commit -m "Keep a root's children inside the root and report the ones git refuses.

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 2: `RepoContext`, the pure rescan and the counts

**Files:**
- Create: `agentflow/repo_context.py`
- Test: `agentflow/tests/test_repo_context.py` (new)

**Interfaces:**
- Consumes: `repo_set.RepoRef`, `repo_set.RepoSet`, `pack.pack_config`, `config.load_config`, `config.ConfigError`, `pack.PackError`, `layers.LayerError`, `hitl.inbox_items`, `state.list_runs`, `state.display_status`, `events.lock_state`.
- Produces:
  ```python
  State = Literal["live", "not_set_up", "missing", "error"]
  @dataclass
  class RepoContext:
      id: str; name: str; repo: Path
      cfg: Config | None = None; config: Path | None = None
      state: State = "live"; error: str | None = None
      runs: Path | None = None                                 # the resolved runs directory when a workspace context was built; None for a single-repository context (never frozen)
  ERROR_MAX = 300
  def load_context(ref: RepoRef) -> RepoContext                # never raises
  def single_context(repo: Path, cfg: Config, config: Path | None = None) -> RepoContext   # one live context from a loaded cfg (make_server, --config, a single repository)
  def counts(ctx: RepoContext) -> dict[str, int]          # {"waiting", "active", "failed"}; zeros when not live or on ANY exception
  def in_flight(ctx: RepoContext) -> bool                 # any run of ctx.repo with lock_state == "live"; a run that can't be loaded is skipped; True (keep it) when the runs can't be read at all
  def runs_dir(ctx: RepoContext) -> Path | None           # state.runs_root(ctx.repo).resolve(); None on any exception
  def usable(ctx: RepoContext) -> bool                    # state == "live" and (ctx.runs is None or runs_dir(ctx) == ctx.runs)
  def settle_dirs(contexts: list[RepoContext]) -> list[RepoContext]   # the shared-runs-directory rule (see the plan decision); order-independent
  def reconcile(current: dict[str, RepoContext], found: RepoSet,
                busy: Callable[[RepoContext], bool] = in_flight) -> dict[str, RepoContext]   # calls settle_dirs
  def repos_view(contexts: Iterable[RepoContext], skipped: Iterable[Skipped], default: str | None,
                 user_error: str | None) -> dict
  def engine_user_settings() -> layers.UserSettings       # the engine defaults alone: no workspace, the `dashboard` block, path None
  ```

- [ ] **Step 1: Write the failing tests** (real tmp git repos with `SPARSE_PACK`, as `test_workspace_cli.make_repo` does)

- `test_load_context_loads_a_live_pack`: `state == "live"`, `cfg.path == <repo>/.agentflow/config.yaml`, `config is None`, `error is None`. A `not_set_up` ref gives `cfg is None`, `state == "not_set_up"`. A pack holding `phases: 3` (fails to load) gives `state == "error"`, `cfg is None` and a non-empty one-line `error`; so does a pack file holding the bytes `\xff` (a `UnicodeDecodeError`, which `config` does not catch), and a pack whose `paths.state_dir` is `~nosuchuser/x` or holds a NUL (the runs directory can't be resolved). An `error` text longer than 300 characters or spanning lines is cut to one line of at most `ERROR_MAX`.
- `test_counts`: a repository with one run waiting on a person, one running with a live lock, one `failed` and one done gives `{"waiting": 1, "active": 1, "failed": 1}` (use `Run.create` and `run.emit` as `test_dashboard.ready` does; a `done` run counts in none); a `not_set_up` context gives zeros; a context whose `repo` was deleted gives zeros and does not raise; a run whose `events.jsonl` is corrupt (an `EventError` from `Run.load`) is skipped while the other runs still count (the waiting one above still gives `waiting == 1`), and `in_flight` still sees another run's live lock. `waiting` counts every `hitl.inbox_items` entry, parked stops included.
- `test_reconcile_keeps_a_live_context_by_identity_and_follows_workspace_order`: `reconcile({a: ctx_a}, found)` where `found` lists `b`, `a` returns a dict whose keys are `[b, a]` and `result[a] is ctx_a`.
- `test_reconcile_retries_an_error_context_and_promotes_a_pack_that_appears`: an `error` context whose pack is now fixed comes back `live`; a `not_set_up` context whose `.agentflow/config.yaml` now exists comes back `live`.
- `test_reconcile_keeps_a_vanished_repository_only_while_it_is_busy`: with `busy=lambda ctx: True` the vanished context is in the result with `state == "missing"` (after the found ones); with `busy=lambda ctx: False` it is gone.
- `test_settle_dirs_gives_a_shared_runs_directory_to_the_repository_it_belongs_to`: `aaa-evil`'s pack sets `paths.state_dir` to `billing`'s default state dir: `billing` stays `live`, `aaa-evil` is `error` naming `billing`, whichever order they are given in; two packs that share a custom folder neither of them owns are both `error`; a context whose `runs_dir` is `None` is `error` (cannot resolve), and two such contexts are not treated as sharing. `reconcile` applies it.
- `test_usable_is_false_once_the_packs_state_dir_changes`: a live workspace context whose pack is then rewritten to set another `paths.state_dir` is not `usable` (and is again after `reconcile` rebuilds it); a context that is not `live` is never `usable`; a `single_context` (its `runs` is `None`) stays `usable` after the same rewrite.
- `test_settle_dirs_refuses_a_directory_under_the_user_state_root_that_is_another_repositorys`: a pack whose `paths.state_dir` is `<user_state_root>/other-1a2b3c4d/runs`'s parent is `error` even when no served repository is `other` (an excluded or unlisted one); a pack that points at its own default directory, or at a folder outside the state root that nobody else uses, is `live`. A new repository whose directory equals that of a `missing` context (a repository that just left) is `error`: `settle_dirs` is given the missing contexts too.
- `test_a_runs_directory_inside_its_own_checkout_is_an_error_for_a_workspace_context`: a pack whose `paths.state_dir` is `.state` (relative, so inside the checkout) is `error` (`its state directory is inside the repository`) after `settle_dirs`, and so is an absolute path under the checkout; the same pack through `single_context` stays `live`.
- `test_a_symlinked_agentflow_directory_is_an_error_context`: `<repo>/.agentflow` is a symlink to another repository's `.agentflow`: `load_context` gives `error` naming the symlink and never loads the target's pack.
- `test_in_flight_keeps_a_context_it_cannot_read`: `list` of the runs root raising `PermissionError` makes `in_flight` `True` (the repository stays `missing`, never dropped on a transient error).
- `test_reconcile_never_reads_a_repository_that_left`: `busy` is called with the vanished context only (a spy), and nothing is loaded for it.
- `test_engine_user_settings_is_the_defaults_without_a_user_file`: empty workspace, `dashboard["port"] == 7788`, `dashboard["host"] == "127.0.0.1"`, `path is None`, whatever the user's file holds (conftest points the config home at an empty tmp dir; write a user file with a workspace and assert the result ignores it).
- `test_repos_view`: for a live, a `not_set_up`, an `error` and a `missing` context returns `{"repos": [{"id", "name", "state", "pack_version", "waiting", "active", "failed", "error"}, …], "skipped": [{"entry", "reason"}], "default": <id or None>, "user_config_error": <text or None>}`; `pack_version` is `cfg.version` for a live context and `None` otherwise; `error` is the text for `error` and `None` otherwise; there is no `path` key, and a live or `not_set_up` context's checkout path appears nowhere in the JSON (a `skipped` entry is the setting as written and an `error` text may name a file: both are for the token holder and are documented as such).

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_repo_context.py --import-mode=prepend -q`
Expected: FAIL (`agentflow.repo_context` does not exist).

- [ ] **Step 3: Implement `agentflow/repo_context.py`**

`load_context`: `live` refs go through `load_config(pack.pack_config(ref.path))`; any `Exception` becomes an `error` context whose text is `" ".join(str(exc).split())`. `counts` and `in_flight` load the runs one at a time (a `_runs(ctx)` helper that scans the runs root and guards each `Run.load`), then `waiting = sum(len(hitl.inbox_items(repo, [run])) for run in runs)` with each run's call guarded on its own (a malformed run costs only its own items), `active` = runs whose `display_status` is `running`, `refining` or `held`, `failed` = runs whose status is `failed` or whose `display_status` is `stalled`; an error outside a single run (the runs root unreadable) gives zeros. `single_context` sets `runs` from `runs_dir`; `load_context` and `reconcile` set it too. `reconcile` builds the new dict in `found.repos` order: a ref that is `live` and has a `live` context keeps that object; every other ref goes through `load_context`; then `settle_dirs` over the found contexts and the `missing` ones together; a kept live context that is no longer `usable` is rebuilt with `load_context`; then each current context that is not in `found` is appended as `state="missing"` (a copy via `dataclasses.replace`, `cfg` kept) when `busy(ctx)`. A `missing` context in `current` that is not busy now is dropped. The module imports no `dashboard`, `flow` or `http` name.

- [ ] **Step 4: Run the tests to verify they pass**

Run the Step 2 command. Expected: PASS.

- [ ] **Step 5: Lint and commit**

```bash
uvx ruff@0.17.4 check .
git add agentflow/repo_context.py agentflow/tests/test_repo_context.py
git commit -m "Add RepoContext, the pure rescan and each repository's counts.

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 3: The server holds a map of contexts and every handler uses one

**Files:**
- Modify: `agentflow/dashboard.py` (`DashboardServer`, `make_server`, `_Handler` and every method that reads `self.server.repo`, `.cfg` or `.config`, `_start_worker`)
- Test: `agentflow/tests/test_dashboard.py` (new section at the end; nothing existing changes)

**Interfaces:**
- Consumes: `repo_context.RepoContext`.
- Produces:
  ```python
  @dataclass(frozen=True)
  class Snapshot:
      repos: dict[str, RepoContext]    # never mutated after the snapshot is published
      default: str | None; skipped: tuple[Skipped, ...]
      user: layers.UserSettings; user_error: str | None
  class DashboardServer(ThreadingHTTPServer):
      snap: Snapshot; prefer: str | None; workspace: bool
      repos / default / skipped / user / user_error          # read-only properties of `snap`
      def default_context(self, snap: Snapshot | None = None) -> RepoContext | None
      def live_contexts(self, snap: Snapshot | None = None) -> list[RepoContext]   # `repo_context.usable` ones, map order
      repo / cfg / config                                    # properties of the default context; cfg has a setter
  def make_server(repo, cfg, host, port, token, spawn=None, spawn_run=None, config=None) -> DashboardServer  # unchanged signature
  def make_workspace_server(contexts: list[RepoContext], user: layers.UserSettings, host: str, port: int, token: str,
                            *, prefer: str | None = None, user_error: str | None = None,
                            skipped: tuple[Skipped, ...] = (), spawn=None, spawn_run=None) -> DashboardServer
  def choose_default(repos: dict[str, RepoContext], prefer: str | None, current: str | None) -> str | None   # `current` if not None (sticky); else `prefer` if usable; else the first usable; else None
  def info_numbers(dashboard: dict) -> tuple[float, int]   # (board_refresh_s, done_shown); never raises; each falls back to DEFAULT_BOARD_REFRESH_S / DEFAULT_DONE_SHOWN when absent or out of range
  ```
  `make_server` and `make_workspace_server` both call one `_checked_bind(host, token)`, which raises `ValueError` for a host other than `127.0.0.1` and for a token under `MIN_TOKEN_CHARS` (today's two checks, moved, not changed).
  `Snapshot.skipped` (the last rescan's, or what `make_workspace_server` was given, empty for `make_server`) is read by Task 4. `/api/info` is answered here: `info_numbers(ctx.cfg.dashboard)` when the default context has a `cfg`, else `info_numbers(snap.user.dashboard)`, with the same keys and values as today.
  The handler gets `self._ctx: RepoContext` per request (set by the dispatcher of Task 4; in this task by `_handle` from `server.default_context()`).

- [ ] **Step 1: Write the failing tests**

- `test_a_workspace_server_over_two_repositories_answers_the_legacy_routes_for_the_default` (fixture `two`: `flow_repo` as A and a `sparse_repo`-style repository B with its own run, both as live contexts, `prefer=B`): `GET /api/board` lists B's cards and not A's; `GET /api/runs/<A run>` is 404, `<B run>` is 200.
- `test_the_default_is_the_first_live_context_when_nothing_is_preferred`: a `not_set_up` context first and a live one second give `default` equal to the live one.
- `test_with_no_default_a_legacy_route_is_404_and_info_still_answers`: `make_workspace_server([], …)`: `/api/board` is 404 with `error` `no repository`, `/api/info` is 200 with the engine version, `/api/workflow` is 404.
- `test_server_repo_cfg_and_config_follow_the_default_and_cfg_can_be_replaced`: `server.repo`, `server.cfg`, `server.config` equal the default context's; assigning `server.cfg = other` changes `default_context().cfg`.
- `test_the_handler_never_reads_the_default_through_the_server`: an `ast` walk of `_Handler` finds no `Attribute` node whose attr is `repo`, `cfg` or `config` and whose value is `self.server` or a local name bound from `self.server` (so `server = self.server; server.cfg` is caught, `self.server.repos` and `self._ctx.cfg` are not), and no call to `self.server.default_context()` outside the dispatcher; the three properties remain for callers outside the handler. (A missed substitution would pass every single-repository test and serve the default's data on another repository's route.)
- `test_info_numbers`: `info_numbers({"board_refresh_s": 5, "done_shown": 20})` is `(5.0, 20)`; `info_numbers({})` is `(2.0, 10)`; `{"board_refresh_s": 0.1, "done_shown": 0}` is `(2.0, 10)`; `board_refresh_s: float("nan")` and `done_shown: True` fall back too. The existing `test_info_names_the_engine_version` passes unchanged.
- `test_a_workspace_server_binds_127_0_0_1_with_a_long_token_only`: `make_workspace_server([], user, "0.0.0.0", 0, TOKEN, …)` and the same with `host="127.0.0.1"` and a 10-character token each raise `ValueError`, as `make_server` does.
- `test_a_default_whose_state_dir_changed_after_load_is_refused`: a workspace server whose default is B; B's pack is then rewritten to set another `paths.state_dir`: a legacy route answers 409 containing `state directory changed`, `/api/info` still answers, and `live_contexts()` no longer lists B (the same check for `/api/repos/<id>/…` is in Task 4).
- `test_the_default_is_sticky`: `choose_default(repos, prefer=A, current=None)` is A when A is usable, else the first usable; with `current=B` it is B whatever `prefer` and the states are (B `not_set_up`, `error` or absent from `repos`); with every context unusable and `current=None` it is `None`. A legacy route whose default is `not_set_up` answers 409, and whose default has left the map answers 404 `no repository`; neither falls over to another repository.
- `test_make_server_wraps_one_repository_exactly_as_before`: `dash.server.repos` has one live context, `dash.server.default` is its id, `dash.server.workspace is False`. The whole existing `test_dashboard.py` is the second proof: run it unchanged.

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_dashboard.py --import-mode=prepend -q` (the whole file: the new tests, which are all in the section at its end, fail and every existing test passes)
Expected: the new tests FAIL (`make_workspace_server` and `repos` do not exist).

- [ ] **Step 3: Refactor `dashboard.py`**

`DashboardServer.__init__` takes `(address, contexts, token, spawn, spawn_run, *, user, prefer, user_error, workspace)` and builds the first `Snapshot`: `repos` keyed by `ctx.id` in the given order, `default = choose_default(repos, prefer, None)`: `prefer` if that context is `usable`, else the first `usable` context, else `None`; later calls pass the snapshot's current default, which stays (Task 6's rescan reuses it). `make_server` builds its one context with `repo_context.single_context(repo, cfg, config)` and `user=repo_context.engine_user_settings()` (Task 2). In `_Handler` the first line of `_handle`'s routing sets `self._ctx = self.server.default_context()` and a route that needs a repository answers 404 `no repository` when it is `None`, 409 (`<name> is not live: <reason>`) when its state is not `live`, and 409 `<name>: its state directory changed; waiting for the next rescan` when it is live but not `usable`; `/api/info`, `/` and `/ui/*` need none. Replace every `self.server.repo`, `.cfg` and `.config` in handlers with `self._ctx.repo`, `.cfg` and `.config`; `_backlog_post`, `_config_post` and `_start_worker` read the server through a local `server = self.server` today, so a search for `self.server.repo` misses them: each takes `ctx = self._ctx` instead, and `_start_worker(self, job, ctx)` takes the context; `_config_post` stores the reloaded config on `ctx.cfg`. `_remind_loop`, `refresh_once` and `prepare` keep working on the default context in this task (Task 6 widens them).

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_dashboard.py agentflow/tests/test_dashboard_js.py agentflow/tests/test_config_edit.py --import-mode=prepend -q`
Expected: PASS, every existing test unchanged.

- [ ] **Step 5: Lint and commit**

```bash
uvx ruff@0.17.4 check .
git add agentflow/dashboard.py agentflow/tests/test_dashboard.py
git commit -m "Hold a map of repository contexts in the dashboard server and route each handler through one.

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 4: `/api/repos` and the `/api/repos/<id>/…` prefix

**Files:**
- Modify: `agentflow/dashboard.py` (`_handle`, a new `_dispatch(path)`, `_get`)
- Test: `agentflow/tests/test_dashboard.py`

**Interfaces:**
- Consumes: Task 3's `self._ctx`, `repo_context.repos_view`.
- Produces: `GET /api/repos` (the `repos_view` payload from one `snap`); `/api/repos/<id>/<rest>` for GET and POST served as `/api/<rest>` with `<id>`'s context, and `self._scoped` set to `True` (`False` for every other path). Constants `REPOS_ROUTE = re.compile(r"^/api/repos/([^/]*)/(.+)$")` and `REPO_ID_MAX = 512` (a 255-byte folder name plus `-` and 8 hex).

- [ ] **Step 1: Write the failing tests** (fixture `two` from Task 3, packs differing: A is the `python` pack and forbids `legacy/**` in `guardrails.forbidden_paths`, B is sparse and does not)

- `test_api_repos_lists_every_repository_with_counts`: both ids with `state == "live"`, `waiting` 1 for A (it has a phase approval pending) and 0 for B, `default` equal to the server's, `skipped == []`, no filesystem path anywhere in the body, 403 without the token.
- `test_every_repository_route_is_scoped_to_its_context` (parametrized over every GET route the handler serves for a repository: `board`, `workflow`, `agents`, `inbox`, `lessons`, `metrics`, `scorecard`, `runs/<id>`, `runs/<id>/{artifacts,plan,files,changes,sessions,events,findings}`, and every POST: `backlog`, `backlog/file`, `backlog/start`, `backlog/remove`, `config/preview`, `config/save`, `runs/<id>/<action>`): each is sent for A and for B (with a run id and a `backlog.yml` that exist in only one), and the answer or the changed file is that repository's own. The `backlog/file` case checks A's `backlog.yml` is byte-identical after a POST to B.
- `test_the_same_run_id_in_two_repositories_is_each_repositorys_own`: `Run.create(..., run_id=<one id>)` in A (a `feature` run) and in B (a `bug` run): `/api/repos/<A>/runs/<id>` and `/api/repos/<B>/runs/<id>` return their own pipeline, and a prefixed approve, answer or cancel on one leaves the other's events byte-identical.
- `test_json_bytes_survives_a_lone_surrogate`: `_json_bytes({"name": "a\udcffb"})` returns bytes (the helper encodes with `errors="replace"`; text that was valid encodes exactly as before).
- `test_a_repository_whose_state_dir_changed_after_load_is_refused_under_its_prefix`: B's pack is rewritten to another `paths.state_dir`: every `/api/repos/<B>/…` route answers 409 containing `state directory changed`, `/api/repos/<A>/…` and `/api/info` still answer, `/api/repos` still lists B.
- `test_a_settings_save_lands_on_the_current_context`: a rescan replaces B's context between the request's snapshot and the save (a hook in `config_edit.save`): the reloaded `cfg` is stored on the context the current `server.snap` holds for B's id, not only on the old object.
- `test_a_symlinked_agentflow_directory_cannot_be_saved_through`: a context built over a repository whose `.agentflow` is a symlink is `error` (Task 2) and `POST /api/repos/<id>/config/save` for it is 409 and writes nothing.
- `test_a_prefixed_route_serves_that_repository`: `/api/repos/<A>/board` and `/api/repos/<B>/board` list their own cards; `/api/repos/<B>/info` answers 200; `/api/repos/<A>/workflow` equals `workflow_view(A.cfg)` and differs from B's.
- `test_a_run_id_of_one_repository_under_another_is_404`: `GET /api/repos/<B>/runs/<A run>` and `…/runs/<A run>/plan` are 404; `POST /api/repos/<B>/runs/<A run>/approve` is 404 and A's run still waits (`Run.load(A, id).state["waiting"]` unchanged).
- `test_an_action_changes_only_the_repository_it_names`: `POST /api/repos/<A>/runs/<A run>/approve` is 200 and B's runs' events are byte-identical before and after; the spawn recorded by the fake runs in A's checkout (`cwd == A.root`).
- `test_an_id_the_map_does_not_hold_is_404_and_reads_nothing` (parametrized): `..%2f..%2fetc`, `%00`, `a%0ab`, `%2e%2e`, `` (empty: `/api/repos//board`), `<A id>.upper()`, `"x" * 5000`, `<A>/../<B>` (a `/` inside), and a `~`-style `%7Eroot`. Each is 404 with `error`. After the server starts, `os.stat`, `os.lstat`, `os.scandir`, `os.listdir`, `io.open` and `builtins.open` are wrapped to record `os.fspath` of their first argument (every `Path` method and `glob` reaches the filesystem through one of them). The wrappers record only on request-handler threads (`threading.current_thread().name` contains `process_request_thread`), so the test runner's own file access is not counted; a 404 for an unknown id touches no file, so the assertion is that nothing at all was recorded during each such request.
- `test_a_repository_that_cannot_serve_answers_409_with_the_reason`: contexts of every state; `not_set_up` → 409 with `not set up`; `error` → 409 containing its load error; `missing` → 409; all three still appear in `/api/repos`; the live one serves. `/api/repos/<not_set_up>/info` is 200 and carries the user settings' two numbers (no `cfg` to read).
- `test_the_settings_form_edits_only_its_own_pack`: `GET /api/repos/<A>/config` returns A's text (it contains the `legacy/**` line) and B's does not; a save to B (`POST /api/repos/<B>/config/save` with a harmless change) changes B's file and not A's (`sha256` of A's file unchanged) and replaces only B's `ctx.cfg`; a save that changes a protected key in A is 400 and A's file is unchanged.
- `test_repos_is_not_a_prefixed_route`: `/api/repos/<A>/repos` and `/api/repos/<A>/repos/<B>/board` are 404.
- `test_start_uses_the_named_repository`: `POST /api/repos/<B>/backlog/start` (a map-module start, with `spawn_run` recorded) calls `spawn_run` with B's `repo` and B's `cfg`'s lane, never A's.

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_dashboard.py --import-mode=prepend -q`
Expected: the new tests FAIL (404 on every `/api/repos…`); every older test passes.

- [ ] **Step 3: Implement**

`_json_bytes` encodes with `errors="replace"`. `_config_post` stores the reloaded config on the context that `self.server.snap.repos.get(ctx.id)` returns at save time (falling back to `ctx`). `_handle` after the token check: `path == "/api/repos"` (GET only) answers `repos_view(snap.repos.values(), snap.skipped, snap.default, snap.user_error)` for one `snap = server.snap`, where the snapshot carries the last `RepoSet.skipped`. A path matching `REPOS_ROUTE` takes `unquote(id)` (answer 404 for an id longer than `REPO_ID_MAX` or empty before any lookup; any other text, control characters included, is looked up and is simply absent), reads `ctx = snap.repos.get(id)` once from the request's `snap` (an absent id is 404 `no such repository`), refuses a `rest` equal to `repos` or beginning `repos/` with 404, sets `self._ctx = ctx` and routes `"/api/" + rest`. Every other path keeps `self._ctx` as the default context. The id is never passed to a filesystem call; only `ctx.repo`, which the rescan produced, is.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_dashboard.py agentflow/tests/test_repo_context.py --import-mode=prepend -q`
Expected: PASS.

- [ ] **Step 5: Lint and commit**

```bash
uvx ruff@0.17.4 check .
git add agentflow/dashboard.py agentflow/tests/test_dashboard.py
git commit -m "Serve each repository under /api/repos/<id> and list them at /api/repos.

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 5: One event stream over every live repository, tagged

**Files:**
- Modify: `agentflow/dashboard.py` (`_events`, `_coalesce`, `_logs`)
- Test: `agentflow/tests/test_dashboard.py`

**Interfaces:**
- Consumes: Task 3's `server.snap`, `server.live_contexts()` and `choose_default`, Task 4's `self._ctx` for the prefixed form.
- Produces: `GET /api/events` streams the default context only and its messages are exactly what they were (`{"run_id", "event"}`, no `repo`); `GET /api/repos/<id>/events` (`self._scoped`) streams that context only and `GET /api/events?all=1` (the query sent exactly once with the value `1`; any other `all` is ignored, as every query parameter of this route was before; on a prefixed path `all` is ignored too, so `/api/repos/<B>/events?all=1` is still B's alone) streams every `usable` and every `missing` context, both with `{"repo": <ctx id>, "run_id": …, "event": …}`. The quiet coalescing key becomes `(repo id, run id, session id)` in every mode.

- [ ] **Step 1: Write the failing tests** (reading the stream as the existing SSE tests do, lines ~900 to 980)

- `test_the_legacy_stream_is_the_default_repositorys_alone_and_untagged`: with default A, an event emitted in B never arrives on `/api/events`, one in A arrives with exactly the keys `run_id` and `event`; the existing SSE tests stay green.
- `test_the_all_stream_tags_each_message_with_its_repository`: `/api/events?all=1` receives an event of A and one of B, each with its own `repo`; `?all=2` and `?all=1&all=1` are ignored and behave as the legacy stream (default only, untagged).
- `test_a_repository_prefixed_stream_carries_only_that_repository`: `/api/repos/<B>/events` receives B's event, tagged, and never A's.
- `test_a_run_of_the_same_id_in_two_repositories_does_not_collide_under_quiet`: two `agent_turn` events with the same run id and session id in A and B on `?quiet=1` both arrive as `agent_activity` (the coalescing key includes the repository).
- `test_a_repository_that_joins_mid_stream_is_not_replayed`: with an `?all=1` stream open, `server.snap` is replaced (a helper `publish(server, repos)` in the test, the way `rescan` will) by a snapshot holding a new repository whose `events.jsonl` already has three events; none is sent, and a fourth emitted afterwards is.
- `test_a_missing_repository_still_streams_its_runs`: a context in state `missing` whose run emits an event is delivered.
- `test_a_prefixed_stream_ignores_all`: `/api/repos/<B>/events?all=1` receives B's event and never A's.
- `test_a_repository_that_raises_does_not_end_the_stream`: with an `?all=1` stream open, A's runs root is deleted, and then `agentflow.dashboard.runs_root` (the name the stream code calls, so the context stays `usable`) is patched to raise `RuntimeError` for A's checkout (what an unresolvable `paths.state_dir` does) and `ValueError` the pass after; B's next event still arrives each time. (The test fails if the per-repository `try/except` is removed.)

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_dashboard.py --import-mode=prepend -q`
Expected: the new tests FAIL; every older test passes.

- [ ] **Step 3: Implement**

Each loop pass reads `server.snap` once and takes the contexts to stream (the default context; or the prefixed one; or, with `all=1`, every `live` and `missing` one). The `repo` key is added only for the scoped and `all` modes. Offsets are keyed `(ctx.id, run name)` and `(ctx.id, BACKLOG_LOG)`. A context id not seen before in this stream gets its offsets seeded from the current file sizes before it is read (the first pass's contexts are seeded by the existing start-of-stream code). Each repository's `runs_root`, `_logs` and reads sit inside `try/except Exception` that skips it for that pass (an `OSError` from a deleted runs root, but equally the `RuntimeError` or `ValueError` an unresolvable `paths.state_dir` raises). `_logs(root)` and `_new_events` are unchanged.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_dashboard.py --import-mode=prepend -q`
Expected: PASS.

- [ ] **Step 5: Lint and commit**

```bash
uvx ruff@0.17.4 check .
git add agentflow/dashboard.py agentflow/tests/test_dashboard.py
git commit -m "Stream every live repository's events on one connection, each tagged with its repository.

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 6: Every repository stays live: reminders, staggered refresh and the rescan

**Files:**
- Modify: `agentflow/dashboard.py` (`_remind_loop`, `refresh_once`, `_refresh_loop`, `prepare`, `serve`, new `serve_workspace`, `DashboardServer.rescan`, `RESCAN_EVERY_S`)
- Test: `agentflow/tests/test_dashboard.py`

**Interfaces:**
- Consumes: `repo_context.reconcile`, `repo_set.resolve`, `layers.load_user_settings`.
- Produces:
  ```python
  RESCAN_EVERY_S = 60.0
  DashboardServer.rescan(self) -> None          # never raises; holds a lock; workspace mode re-reads the user file
  def refresh_once(server, ctx: RepoContext | None = None) -> None   # ctx None: the default context, as before
  def refresh_round(server) -> None                      # refresh_once for each live context in turn; one loop pass
  def remind_once(server) -> None                        # remind_due + recheck_waiting for each live context; one loop pass
  def prepare(server, ctx: RepoContext | None = None) -> list[str]   # same
  def serve_workspace(contexts, user, host, port, token, *, prefer=None, user_error=None, skipped=(), announce=print) -> None
  ```

- [ ] **Step 1: Write the failing tests**

- `test_rescan_adds_a_repository_and_keeps_the_others_by_identity`: a workspace server over a root holding `a`; create `b` in the root, write nothing else, `server.rescan()`; `/api/repos` lists both, and `server.repos[a]` is the same object as before.
- `test_rescan_keeps_a_vanished_repository_while_its_run_is_live_and_drops_it_after`: remove `b` from the root's settings with a run holding a live lock (`run.events` lock held by the test process as the existing stalled/live tests do) → `state == "missing"`; release the lock, rescan → gone from `/api/repos`.
- `test_rescan_never_raises_and_the_thread_survives_a_hostile_pack`: a pack holding `\xff` and a run with a corrupt `events.jsonl` in two repositories: `rescan()` returns, both are `error` or zero-count, `/api/repos` answers 200, and the rescan thread's next pass still runs (spy on `rescan`).
- `test_rescan_with_an_unusable_user_file_keeps_the_map_and_reports_it`: a user file that fails `load_user_settings` leaves `server.repos` as it was and `/api/repos` carries `user_config_error` naming the file; fixing the file and rescanning clears it.
- `test_a_default_stays_put_when_it_leaves_or_breaks_and_is_adopted_once_when_there_is_none`: the default repository vanishes, goes `missing`, or its pack breaks (`error`) → `server.default` does NOT change (its legacy routes answer 409 or 404, per Task 3); a server that started with no default adopts the first usable repository the rescan finds, once, and keeps it when a repository that sorts before it joins.
- `test_a_request_sees_one_snapshot`: `server.repos = {}` and `server.default = "x"` raise `AttributeError` (read-only properties); a rescan assigns `server.snap` exactly once (the test wraps the attribute and counts); and a reader thread that takes `snap = server.snap` and checks `(sorted(snap.repos), snap.user_error)` against the two consistent pairs of two alternating user files, while the main thread calls `rescan()` 200 times, never sees a mixed pair.
- `test_a_single_repository_server_never_rescans`: `make_server(...).rescan()` leaves the map alone (workspace is False).
- `test_reminders_and_rechecks_run_in_every_live_repository`: with `hitl.remind_due` and `preflight.recheck_waiting` replaced by spies, one `remind_once(server)` calls each once per live context with that context's `repo` and `cfg`, and never for a `not_set_up` or `error` one.
- `test_one_repository_failing_does_not_stop_the_others_and_is_named`: `module_queue.backlog_settings` is patched truthy (`refresh_once` calls `backlog.refresh` only for a pack with a `dashboard.backlog` block, which the toy packs lack) and `backlog.refresh` raises for A; one `refresh_round(server)` still calls it for B, the captured stderr has a line containing A's name and `backlog refresh failed`, and the same holds for a raising `hitl.remind_due` in `remind_once`. With a single context the line is exactly the old `dashboard: backlog refresh failed: <exc>`.
- `test_refreshes_run_one_repository_at_a_time`: two spies record entry and exit; no overlap in one `refresh_round`.
- `test_prepare_settles_every_repository_that_is_live_at_start`: an interrupted card start (`item_starting` without an outcome) in B is marked failed by `serve_workspace`'s start-up path, once; a repository added afterwards is not touched. A `prepare` that raises for A (`Backlog.load` patched) is logged with A's name and B is still settled.
- `test_a_context_that_stops_being_usable_leaves_every_background_pass`: B's pack is rewritten to another `paths.state_dir`: the next `remind_once`, `refresh_round` and a new stream skip B and still serve A.
- `test_log_and_announce_lines_escape_a_hostile_name`: a repository folder named `a\x1b[31mb` (skipped on a filesystem that refuses it) appears as an escaped name in the failure line and the announce text, never as a raw ESC byte.
- `test_serve_workspace_announces_how_many_repositories_it_covers` (announce captured; `serve_forever` replaced to raise `KeyboardInterrupt`): the text contains the URL with `#token=` and `2 repositories`; a single-repository `serve` prints exactly what it printed before.

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_dashboard.py --import-mode=prepend -q`
Expected: the new tests FAIL; every older test passes.

- [ ] **Step 3: Implement**

`rescan`: when `workspace` is False return. Else, under `self._scan_lock` and inside one `try/except Exception` (an unexpected error is logged as `dashboard: rescan failed: <exc>` and the old snapshot stays): `layers.load_user_settings()`; a `LayerError` publishes the old snapshot with `user_error` set to its one-line text; otherwise `found = repo_set.resolve(settings.workspace)` and `repos = reconcile(snap.repos, found)` are computed first, then one new `Snapshot(repos, choose_default(repos, self.prefer, snap.default), found.skipped, settings, None)` is assigned to `self.snap`. The rescan thread's loop also wraps each pass in `try/except Exception`. `_remind_loop` and `_refresh_loop` call `remind_once` and `refresh_round`, which take one `snap`, iterate its live contexts, wrap each repository's work in `try/except Exception` that writes `dashboard: <printable name>: <what> failed: <printable exc>` (`doctor.printable`; the name is omitted when the snapshot holds one context, so a single-repository terminal reads as before), and check `server.stopping` between repositories. `serve` and `serve_workspace` share one helper that runs `prepare` once for every usable context, each inside `try/except Exception` that logs `dashboard: <name>: prepare failed: <exc>` and goes on, starts the reminder and refresh threads (and, in workspace mode only, a rescan thread waiting `RESCAN_EVERY_S`), prints the announce text and serves; the workspace announce adds one line with the repository count and that the token reaches all of them. `serve_workspace` passes `skipped` to `make_workspace_server`, so `/api/repos` shows it from the first request.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_dashboard.py agentflow/tests/test_repo_context.py --import-mode=prepend -q`
Expected: PASS.

- [ ] **Step 5: Lint and commit**

```bash
uvx ruff@0.17.4 check .
git add agentflow/dashboard.py agentflow/tests/test_dashboard.py
git commit -m "Keep every repository live: reminders, one-at-a-time refresh and a minute-by-minute rescan.

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 7: `agentflow dashboard` starts anywhere

**Files:**
- Modify: `agentflow/repo_context.py` (`StartPlan`, `plan_start`)
- Modify: `agentflow/dashboard.py` (`port_of`)
- Modify: `agentflow/flow.py` (`cmd_dashboard`)
- Test: `agentflow/tests/test_repo_context.py`, `agentflow/tests/test_workspace_cli.py`, `agentflow/tests/test_dashboard.py`

**Interfaces:**
- Consumes: Tasks 2 to 6; `flow._find_repo`.
- Produces:
  ```python
  class StartPlan(NamedTuple):
      contexts: list[RepoContext]; prefer: str | None; workspace: bool
      user: layers.UserSettings; user_error: str | None
      skipped: tuple[Skipped, ...] = (); left_out: str | None = None
  def plan_start(start: Path | None, config: Path | None) -> StartPlan   # raises ConfigError / PackError as the CLI does today
  def port_of(dashboard: dict) -> int                        # 0..65535 else ValueError naming dashboard.port
  ```

- [ ] **Step 1: Write the failing tests**

`test_repo_context.py`, `plan_start` (the four rows of spec 4.1, plus `--config`):
- no workspace, inside a repository → one `live` context (from `single_context`), `workspace is False`, `prefer` its id (today's mode). With no pack in that repository `plan_start` raises `PackError` (`no .agentflow/config.yaml`), and `cmd_dashboard` prints `error: …` and returns 1, as today.
- workspace set, inside one of its repositories → every repository's context, `workspace is True`, `prefer` the start repository's id; when that repository's pack is broken or missing its context is `error` or `not_set_up`, `prefer` is still its id, and `left_out` is `None`.
- workspace set where a clone `aaa-evil` sets `paths.state_dir` to `billing`'s default state dir: `plan_start` already returns `aaa-evil` as `error` and `billing` as `live` (the shared-directory rule holds from the first request, before any rescan).
- workspace set, inside a repository that is not in it → the workspace only (the start repository is not added), `prefer` is `None`.
- workspace set, `start is None` → the workspace, `prefer is None`.
- no workspace, `start is None` → no contexts, `workspace is True`, no error (the empty start).
- `config` given → one context from that pack (`single_context(start, load_config(config), config)`, so `ctx.config == config`), `workspace is False`, the user file's workspace not read (a bad user file does not matter); `start is None` with `config` raises `ConfigError` `not inside a git checkout`.
- an unusable user file with `start is None` → no contexts, `workspace is True`, `user_error` names the file; with a repository to start in and no `config` it raises as `load_config` does today.

`test_dashboard.py`, `port_of`:
- `port_of({"port": 7788})` is 7788 and `port_of({"port": 0})` is 0; `port_of({})` raises `ValueError` mentioning `dashboard.port` (the engine's default block always carries a port, so a block without one is a caller bug); `"port": -1`, `65536`, `True`, `"80"`, `1.5` each raise the same.
- `test_info_for_a_server_with_no_default_uses_the_user_settings`: a workspace server over no repositories with `user.dashboard = {"port": 7788, "board_refresh_s": 5, "done_shown": 20}` answers `/api/info` with `board_refresh_s == 5.0`, `done_shown == 20` and `inbox_legacy` false; a bad `port` in that block changes neither number.

`test_workspace_cli.py`, `cmd_dashboard` (`dashboard.serve` and `serve_workspace` replaced by recorders, cwd as each case needs):
- `test_dashboard_in_a_repository_with_no_workspace_serves_that_repository_as_before`: `serve` is called with the repository, its cfg, `127.0.0.1`, the pack's port.
- `test_dashboard_with_a_workspace_serves_it_from_a_directory_that_is_no_repository`: `serve_workspace` is called with the contexts of `alpha`, `beta` and `gamma` (not set up), host `127.0.0.1` (from the user settings' `dashboard.host`), the user file's `dashboard.port`, `prefer is None`, and the plan's `skipped`; no `not inside a git checkout` error.
- `test_dashboard_warns_when_the_repository_you_start_in_is_not_live`: started in `alpha` (in the workspace) whose pack is broken, the output has `warning: alpha is not live: <reason>; showing <first live repository> instead`, `serve_workspace` is still called, and `prefer` is `alpha`'s id (it takes the default back once it loads).
- `test_dashboard_with_no_workspace_and_no_repository_starts_with_nothing`: `serve_workspace` is called with no contexts, and the output tells the user to set `workspace.roots` in the user file.
- `test_dashboard_says_when_it_serves_nothing_or_not_the_repository_you_are_in`: with a workspace set and the start repository outside it, the output has one line `note: <name> is not in your workspace and is not served; add its folder to workspace.roots in <user file>`; with a workspace whose every entry was skipped (a mistyped root) it prints each `skipped <entry>: <reason>` and `no repositories found: …` instead of staying silent; every name and entry in these lines is escaped by `doctor.printable`; with a not-set-up repository first in the workspace the default is still the first live one.
- `test_dashboard_with_a_bad_user_port_is_an_error`: a user file with `dashboard: {port: 99999}` and a workspace prints `error: dashboard.port …` and returns 1 without serving.
- `test_dashboard_port_in_use_adds_the_hint`: bind a socket, run `cmd_dashboard` with `--port <it>` for a real `serve`; the output ends `another dashboard may be running; use --port 0` and the code is 1, in both modes.
- `test_dashboard_with_config_ignores_the_workspace`: `--config <pack>` inside a repository with a workspace set calls `serve`, not `serve_workspace`.

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_repo_context.py agentflow/tests/test_workspace_cli.py agentflow/tests/test_dashboard.py --import-mode=prepend -q`
Expected: the new tests FAIL; every older test passes.

- [ ] **Step 3: Implement**

`plan_start` follows the cases above and reads the workspace with `layers.load_user_settings()` and `repo_set.resolve` (never `from_user_config`, which raises for the empty case); workspace contexts come from `load_context` and are passed through `settle_dirs`; the single and `--config` modes build theirs with `single_context` (so a missing or broken pack raises as today); `prefer` is `pack.repo_id(start)` only when that id is in the resolved set. `port_of` is used by the CLI only; `host` is `str(plan.user.dashboard.get("host", "127.0.0.1"))`, and `make_workspace_server` refuses anything else. `cmd_dashboard`: find the start repository (a failure is `None`), call `plan_start`, then `serve` when `not plan.workspace` (the code path of today, port from the loaded `cfg`) or `serve_workspace` (port `args.port` if given, else `port_of(plan.user.dashboard)`); with `plan.user_error` print `warning: <text>`; with a start repository whose context is not live print the `warning:` line; with a start repository that `plan_start` left out print the `note:` line, and with a workspace that resolved to nothing print its `Skipped` lines (so `StartPlan` also carries `skipped` and `left_out: str | None`, the start repository's name when it was not added); with no contexts and no error print `no repositories yet: set workspace.roots in <user config file>; the dashboard is open without one`. Catch `OSError` and, for `errno.EADDRINUSE`, print the hint after the error line.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_repo_context.py agentflow/tests/test_workspace_cli.py agentflow/tests/test_dashboard.py agentflow/tests/test_config_edit.py --import-mode=prepend -q`
Expected: PASS.

- [ ] **Step 5: Lint and commit**

```bash
uvx ruff@0.17.4 check .
git add agentflow/repo_context.py agentflow/dashboard.py agentflow/flow.py agentflow/tests/test_repo_context.py agentflow/tests/test_workspace_cli.py agentflow/tests/test_dashboard.py
git commit -m "Start the dashboard from any directory and serve the whole workspace from one process.

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 8: Documentation, the spec's rollout and the 3.15 release

**Files:**
- Modify: `docs/agentflow-playbook.md` (§10, §12, §14 `paths.state_dir`, §17), `docs/specs/agentflow-workspace-proposal.md`, `CLAUDE.md`
- Modify: `agentflow/dashboard.py` (the module docstring's route list)
- Modify: `CHANGELOG.md`, `pyproject.toml`, `agentflow/pack.py`
- Test: `agentflow/tests/test_packaging.py`, `agentflow/tests/test_portability.py`, the whole suite

- [ ] **Step 1: Write the text**

Playbook §10: `agentflow dashboard` works from any directory when a workspace is set. §17 "The dashboard": replace the paragraph that says it serves one repository with how it starts (the four start-up cases and `--config`), that it is one process, one port and one token for every repository (and that a leaked token now reaches all of them), `/api/repos` and the `/api/repos/<id>/…` prefix, that every repository stays live and a rescan runs every minute, what `not set up`, `error` and `missing` mean, that a submodule or a worktree whose main checkout is outside a root is reported by `workspace list`, that the page still shows the default repository until W3, that a second dashboard's start-up settles the first one's in-flight card starts as failed (so use `--port 0` only when no other dashboard is running), and that every repository under a root joins the background work (`workspace.exclude` is the opt-out), that a repository whose `paths.state_dir` is inside its own checkout is shown as an error by the dashboard (move the state dir out of the repository), and that a pack whose nested aliases expand past a million values is refused (§12 troubleshooting gets one line for each). The playbook also states the known limits the user accepted on 2026-11-26: a repository on a hung mount can delay the other repositories' reminders and refresh and `/api/repos` (a failing one never stops another); `--config` serves the one repository you start in and ignores the workspace; the legacy routes keep the default repository until W3's switcher, and the default never moves on its own; and two narrow races (a pack rewritten inside one request, and a stale lock PID that keeps a vanished repository `missing`). Also correct the §17 sentences that say a linked worktree "counts once, as its main checkout" (add: unless that checkout is outside the root, when it is reported) and that a submodule is "skipped silently". Spec: Rollout W3 gains "the merged `/api/inbox` and `/api/agents` (each item with `repo`) and the page's switch to `/api/events?all=1`", section 4.3's table notes that `/api/inbox`, `/api/agents` and `/api/events` are the default repository's until W3 and that the merged, tagged stream is `/api/events?all=1` from W2, the Testing-strategy bullet (the legacy `/api/inbox` and `/api/agents` returning each item with `repo`) is moved to W3's list, success criterion 8 is reworded to "one repository's failing refresh, pack or `gh` never stops another repository's refresh, reminders or runs; a slow one may delay the next within a round", and "Before W2 starts" gains a closing line "settled in the W2 plan" for each of its three bullets. `CLAUDE.md`: one line in "Setting up a repository" or "Safety and the UI" naming `repo_context.py` (the map of `RepoContext`s the dashboard serves, the pure `reconcile`, `plan_start`). The dashboard module docstring lists the new routes and the tagged stream. `CHANGELOG.md`: `Added` lines for the one-process multi-repository dashboard, `/api/repos` and the prefix, the tagged event stream and the port hint; (the `Changed` line for `workspace list` and `--all` was added by Task 1; the pack-size-bound `Fixed` line shipped in 3.14); then rename `## [Unreleased]` to `## [3.15] - 2026-11-26` and open a fresh empty one. Set `version = "3.15"` in `pyproject.toml` and `ENGINE_VERSION = "3.15"` in `agentflow/pack.py`.

- [ ] **Step 2: Run the checks**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_packaging.py agentflow/tests/test_portability.py agentflow/tests/test_explain_golden.py agentflow/tests/test_dashboard.py agentflow/tests/test_dashboard_js.py --import-mode=prepend -q`, then `bin/agentflow validate` (it must print `0 warnings`), then `uvx ruff@0.17.4 check .` (`All checks passed!`).
Expected: PASS.

- [ ] **Step 3: Run the full suite once**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests --import-mode=prepend -q` in the background (25 to 60 minutes).
Expected: PASS. A failure outside `test_dashboard.py`, `test_repo_set.py`, `test_repo_context.py` or `test_workspace_cli.py` is a regression through a shared fixture or `flow.main`: fix it before the commit.

- [ ] **Step 4: Commit**

```bash
git add docs/agentflow-playbook.md docs/specs/agentflow-workspace-proposal.md CLAUDE.md agentflow/dashboard.py CHANGELOG.md pyproject.toml agentflow/pack.py
git commit -m "Document the multi-repository dashboard and release 3.15.

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```
