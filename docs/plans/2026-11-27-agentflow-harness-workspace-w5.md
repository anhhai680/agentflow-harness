# Workspace, slice W5 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add two personal keys.
- `workspace.max_active_runs` caps how many runs run an agent session or a gate set at once on this machine. A run past the cap is held until a slot frees, never refused.
- `workspace.digest` turns every notification into one line over every workspace repository.

**Architecture:**
- **Task 1** adds the two keys to the user layer (`layers.py`), `validate`, Your settings (`config_edit.py`) and the dashboard's restart and rescan rules.
- **Task 2** adds `agentflow/run_cap.py`. It keeps one `flock`ed file per slot under `pack.user_state_root()/run_cap/`, taken under a short mutex. `advance` opens a `run_cap.Turn` for its turn in a context variable. Three places take a slot from that turn for the length of one piece of work: the agent-session call in `lanes`, the sizer call in `sizing_lane`, and `gates.run_gates`. Holding works the way the usage governor holds: `run_held` and `run_hold_ended`, polling, failing open.
- **Task 3** adds `hitl.counts_of_states`, a pure function shared with `digest_text`. It also adds `workspace_digest_text`, which reads other repositories' `state.json` files read-only from their default state dirs, and has `notify_run` prefer that line when `workspace.digest` is on. Task 3 also releases 3.18.

**Tech Stack:** Python 3.12, stdlib + `pyyaml` (`fcntl`, `contextvars`), pytest with the fake agent and real tmp git repos. The page is plain browser JavaScript, tested under `node` through `conftest.py`'s `js`.

**Spec:** `docs/specs/agentflow-workspace-proposal.md`, slice **W5**. The main source is **section 9**, which fixes every value this plan uses: read it first. Section 9 was written on 2026-11-27 from the developer's answers: build both, hold rather than refuse, a user key for the digest, and a slot per agent session or gate set. Two doubt reviews of this plan then revised it. Also read section 6's last sentence, the section 7 table, the Rollout row and the Boundaries' "Never" line.

**Order and execution:**
- **Step 0.** From an up-to-date `main` (3.17, with W4 merged), create `feat/workspace-w5`. Commit spec section 9 and this plan there first:
  ```bash
  git add docs/specs/agentflow-workspace-proposal.md docs/plans/2026-11-27-agentflow-harness-workspace-w5.md
  git commit -m "Specify and plan workspace slice W5: the run cap and the merged digest."
  ```
- **Run Tasks 1, 2 and 3 in that order.** Tasks 2 and 3 use Task 1's fields. All three edit `CHANGELOG.md` and playbook §17, so they must not run in parallel.
- **Task 1 is under the security overlay**, because it changes `layers.py`. Plan it with `security-and-hardening` and review it with the `security-auditor` agent.
- **Task 3 also touches `pack.py`** (the `ENGINE_VERSION` line only), so the whole-branch review at the end uses the `security-auditor` too.

### What W5 does not do

- No ranking of the runs that wait for a slot (the governor's "finish first"): the first check after a slot frees wins.
- No change to `agentflow queue`, to `slots.count` (the per-repository `{slot}` numbers, a different thing), to the usage governor or to the metrics buckets. A cap wait counts in `hold`.
- No new `doctor` or `workspace list` lines.
- No change to the per-repository `hitl.digest` when `workspace.digest` is off.
- No second usage-window check after a cap wait, no stop of a gate set whose wait a cancel ended, no kill of a dead runner's children when its slot frees, and no cap across different `XDG_STATE_HOME`s. All four are accepted costs in section 9.
- No new Metrics label. Cap waits show in the `hold` bucket, labelled "usage-limit hold", and a held CI-fix gate set still shows "waiting for CI" on the board.

### Plan decisions

- **Slots come from the turn.**
  - `advance` builds its `_Loop` inside its `try`, as it does today, and runs `go()` inside `with run_cap.turn(run, cfg):`. That `with` statement is also inside the `try`.
  - When the turn ends, returns or raises, `Turn.close()` releases whatever it holds.
  - With no turn (`agentflow init`'s baseline, a test calling an executor directly, the retro pipeline), `run_cap.current()` is None and nothing is capped.
- **The three take sites** are `with run_cap.working(key, stop_on_halt=...) as go_on:` blocks:
  - **`lanes`:** immediately before `called = self._call(sr, turn)` (`lanes.py:992`), so it covers every fresh session and every resume, Claude and Codex alike. It uses `stop_on_halt=True`, and returns `"stopped"` when `go_on` is False. This is after the governor's hard check, at the same place.
  - **`sizing_lane._sizer_call`:** around the sizer call, after its governor check. It uses `stop_on_halt=True`, and returns `(None, [])` when `go_on` is False.
  - **`gates.run_gates`:** around its whole body. It uses `stop_on_halt=False`: a pause or cancel ends the wait, and that gate set runs without a slot. `run_gates_reusing` reaches the slot through `run_gates`.
  - The hold key is `f"{phase}/{run_cap.CAP_STEP}"`. `phase` is `sr.phase.name` in `lanes`, the sizer's `phase` argument in `sizing_lane`, and `run.state["phase"]` (else `"run"`) in `gates`.
  - `working` is re-entrant, keeping a depth count on the `Turn`. A nested take while the turn holds a slot just goes on.
  - Speculation runs in the same thread, so it is capped like any other work.
- **`run_cap` imports only `state`, `layers`, `pack` and `safeio` at module level.**
  - `flow_control` imports `gates`, which imports `run_cap`, so importing `flow_control` from `run_cap` would be circular and break every import of the engine.
  - The halt check is inline: `status in state.TERMINAL or status == "paused"`. This is `flow_control.halted`'s rule, and a test pins it as equal.
  - `hitl.notify_run` is imported inside the hold.
- **A gate set with no gate takes nothing.** `run_gates` takes a slot only after its names resolve to at least one gate.
- **`take` runs under the mutex** `run_cap/cap.lock`. The mutex is taken with `LOCK_EX | LOCK_NB` retries for up to `MUTEX_WAIT_S = 2.0`, and fails open after that. Under the mutex, `take`:
  1. counts each existing `<n>.lock` (`n < MAX_ACTIVE_RUNS`) whose `LOCK_EX | LOCK_NB` fails as held, and unlocks the ones it probed successfully;
  2. returns None when `cap` or more locks are held;
  3. otherwise locks the lowest free `n < cap`, creating the file if needed, and returns that `Slot`.

  The mutex closes the probe race and the overshoot of a lowered cap.
- **`take` fails open as a whole.** It returns an unheld `Slot(None, None)`, releasing any descriptor it took, when:
  - the folder is a symlink;
  - any file it opens is a symlink, a hardlink (`st_nlink != 1`) or not a regular file;
  - the mutex can't be taken within `MUTEX_WAIT_S`;
  - any `OSError` occurs.

  Files are opened `O_RDWR | O_CREAT | O_NOFOLLOW | O_NONBLOCK`, mode `0o600`, as `slots._lock` opens its lock. The first fail-open of a process writes one line to stderr, `agentflow: the run cap can't be used (<reason>); runs are not capped`, and later fail-opens stay silent. The module flag `_warned: bool` tracks this, and tests reset it.
- **The cap is read at each take** through `layers.load_user_settings()`. Any exception from that read means no cap.
- **Holding never raises and never leaks.**
  - Any exception after a lock was taken releases it.
  - The `run_hold_ended` emit in the hold's `finally` is wrapped in `try/except Exception`, as `governor_lane.py:99-106` does.
  - Any unexpected exception in `working` ends as "go on uncapped".
- **An open-ended hold.** The cap hold's `until` is its start time (ISO, seconds), because `state._validate` refuses an empty required field.
  - `state.py` gets `OPEN_HOLDS = ("run cap",)`, and `run_cap.HOLD_REASON` is `state.OPEN_HOLDS[0]`.
  - For a reason in `OPEN_HOLDS`, three readers drop ` until …`: `views.py:291`'s badge (`held (run cap)`), `flow.py:302`'s `held:` line, and `inbox_view._watch`.
  - Every other hold's text is unchanged, and `test_dashboard.test_a_held_run_says_why_and_until_when` still passes.
- **Merged digest repositories.**
  - **The run's own repository** is read as `digest_text` reads it today, through `pack.state_dir` and `list_runs`. A failure there raises, so `notify_run` falls back to the plain message.
  - **Every other `live` `RepoRef`** of `repo_set.from_user_config()`, other than the run's own `pack.repo_id`, is checked by `_uses_default_state(path) -> bool`. That function never calls `pack.state_dir`, which reads the pack with a plain `open`.
    - It reads that repository's pack file and nothing else of its checkout.
    - `os.lstat(path / ".agentflow")` must be a directory and not a symlink.
    - Then `safeio.read_bytes(path / ".agentflow" / "config.yaml", layers.MAX_CONFIG_BYTES)` (no symlink, FIFO or device; non-blocking), then `yaml.safe_load`.
    - It returns True only for a mapping whose `paths.state_dir` is absent or empty.
  - **Its runs** come from `pack.user_state_root() / pack.repo_id(path) / "runs"`, read directly. Each entry must be a real directory (`os.lstat`, not a symlink) holding `events.jsonl`, the same set `list_runs` counts. Its `state.json` is read through `safeio.read_bytes(…, STATE_MAX_BYTES)`, with `STATE_MAX_BYTES = 8 * 1024 * 1024`, then `json.loads`. Runs are never read through `Run.load`, which would take locks and rewrite `state.json`.
  - **Not counted:** a repository for which `_uses_default_state` is False, or for which anything in counting it raises or returns None. That covers a missing `state.json`, a non-dict, missing keys and a parked item with no `key`. The whole per-repository count is one `try/except Exception`.
  - **No workspace:** any exception from `from_user_config()` means the run's own repository only.
  - **Labels:** the `RepoRef.name`, or `_one_line(folder name)` for the run's own repository when it is outside the workspace.
  - **The on-check:** `_workspace_digest_on()` catches every `Exception` and returns False.
- **Delivery.** `notify` returns how many entries delivered. That is a new `int` return value; current callers ignore it. The merged line is written to `workspace_digest.txt` only when delivery was above 0, and it is cut to `MESSAGE_MAX` before the compare and the write. The per-repository digest is unchanged.
- **Your settings.** Both keys go in a new section, `"Across repositories"`, after Workspace.
  - The page's restart note, the save's `restart` list and the save's rescan concern only `workspace.roots`, `repos` and `exclude` (`layers.WORKSPACE_LISTS`).
  - Putting the two keys on the page widens the user-level allow-list, which the spec's Boundaries list under "Ask first". The developer settled that on 2026-11-27: "editable on Your settings" for the digest, and the cap goes alongside it as a bounded integer (spec section 9).

## Global Constraints

- Engine dependencies stay stdlib + `pyyaml>=6`. No project strings in the engine (`test_portability.DENYLIST`), `dashboard_ui/*.js` included.
- `workspace.*` is read from the user file only. In a pack it gets a `validate` warning and is ignored, and it stays in `_PROTECTED_KEYS`.
  - `max_active_runs` is an integer from `1` to `64`, or absent. A bool is refused.
  - `digest` is `true` or `false`, default `false`.
- The cap and the digest never raise into a run. The cap fails open and the digest falls back, as section 9 says. Nothing of another repository is locked or written.
- With neither key set, behaviour is unchanged. Every existing test passes except one that Task 1 changes on purpose (`test_the_user_form_holds_the_allow_list_and_nothing_else`). The golden `explain` files don't change.
- Every new config key ships with a Settings field, `validate` checks, a `test_config_edit.py` case and a playbook §14 line (CLAUDE.md).
- Before every commit, `uvx ruff@0.17.4 check .` prints `All checks passed!` and `bin/agentflow validate` prints `0 warnings`. Tests run with `uv run --no-project --with pyyaml --with pytest pytest <file> --import-mode=prepend -q`.
- Each task adds its user-visible lines under `## [Unreleased]` in `CHANGELOG.md`. Task 3 releases **3.18**, or the next version past whatever `main` has by then. Commit messages are one imperative sentence ending with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

1. **A runner that dies holding a slot** (killed, crashed, laptop lid): the next piece of work gets that slot with nothing to clean up, because the `flock` drops with the process (Task 2, `test_a_killed_holder_frees_its_slot`).
2. **Work that slips past the cap:** a refine round, the ship phase's sync and CI-fix gates, preflight gates, a triage session and a resumed session all take a slot, because they reach the three take sites (Task 2, `test_every_kind_of_work_takes_a_slot`).
3. **Waits hold nothing.** A turn that runs no agent session and no gate set (an answered checkpoint that raises its approval, a run on CI) never waits and never holds a slot. A slot never outlives its piece of work, even when the work raises (Task 2).
4. **A cap lowered under busy runs:** going from 3 held to a cap of 1, no new work starts until two have finished. Two takers racing through the mutex never overshoot (Task 2).
5. **A broken or hostile workspace repository** (unreadable runs, or a `paths.state_dir` pointing elsewhere): the merged digest still reaches the person and shows that repository as not counted. Nothing of it is locked, written or followed, and the runner never falls back to sending every note (Task 3).

---

### Task 1: The two keys

**Files:**
- Modify: `agentflow/layers.py` (`USER_KEYS`, `WORKSPACE_LISTS`, `WORKSPACE_KEYS`, `MAX_ACTIVE_RUNS`, `WorkspaceSettings`, `workspace_settings`)
- Modify: `agentflow/config.py`: in `_Validator.workspace`, the warning loop covers the new keys, and the docstring says so.
- Modify: `agentflow/config_edit.py` (a new `_USER_SECTIONS` entry)
- Modify: `agentflow/dashboard.py`:
  - the user save's `restart` list and `user_saved`'s rescan trigger use only the list keys;
  - the module docstring (lines ~64-73) and the `user_saved` docstring say "a `workspace.roots`, `repos` or `exclude` change" where they say `workspace.*` today.
- Modify: `agentflow/dashboard_ui/user_settings.js`: the restart note goes only on a section that holds a list workspace key.
- Modify: `docs/agentflow-playbook.md`:
  - §14: the user-file key list (~line 582), and two new key lines after `workspace.exclude`;
  - §17: the opening paragraph's key list (~line 955), the Your settings allow-list (~line 1022), and "Restart or rescan" (~line 1026), which must say that only the three list keys rescan or need a restart.
- Modify: `CHANGELOG.md`: an `Added` line for the keys, and a `Changed` line saying that a `workspace.*` save rescans or asks for a restart only for `roots`, `repos` and `exclude`.
- Test: `agentflow/tests/test_layers.py`, `test_config.py`, `test_config_edit.py`, `test_dashboard.py`, `test_dashboard_js.py`

**Interfaces:**
- Produces:
  ```python
  # layers.py
  WORKSPACE_LISTS = ("roots", "repos", "exclude")
  WORKSPACE_KEYS = (*WORKSPACE_LISTS, "max_active_runs", "digest")
  MAX_ACTIVE_RUNS = 64
  class WorkspaceSettings(NamedTuple):
      roots: tuple[str, ...]
      repos: tuple[str, ...]
      exclude: tuple[str, ...]
      max_active_runs: int | None = None  # None: no cap
      digest: bool = False
  # USER_KEYS gains "workspace.max_active_runs", "workspace.digest"
  ```
  `workspace_settings` raises:
  - `LayerError("workspace.max_active_runs", "must be a whole number from 1 to 64")` for a bool, a non-int or a value out of range. `None` means unset.
  - `LayerError("workspace.digest", "must be true or false")` for a non-bool. `None` is `False`.

- [ ] **Step 1: Write the failing tests**

```python
# test_layers.py
def test_workspace_settings_reads_the_cap_and_the_digest():
    got = layers.workspace_settings({"max_active_runs": 3, "digest": True})
    assert (got.max_active_runs, got.digest, got.roots) == (3, True, ())
    assert layers.workspace_settings(None) == layers.WorkspaceSettings((), (), (), None, False)

@pytest.mark.parametrize("block, path", [({"max_active_runs": 0}, "workspace.max_active_runs"),
    ({"max_active_runs": 65}, "workspace.max_active_runs"), ({"max_active_runs": True}, "workspace.max_active_runs"),
    ({"max_active_runs": "3"}, "workspace.max_active_runs"), ({"digest": "yes"}, "workspace.digest")])
def test_workspace_settings_refuses_a_bad_cap_or_digest(block, path):
    with pytest.raises(layers.LayerError) as err:
        layers.workspace_settings(block)
    assert err.value.path == path
```

Also:
- **`test_layers.py`:** extend `test_the_workspace_and_dashboard_keys_are_personal_settings` with the two keys.
- **`test_config.py`, `test_a_pack_that_sets_the_cap_or_the_digest_gets_a_warning`:** a pack with `workspace: {max_active_runs: 2, digest: true}` gets warnings at exactly `workspace.max_active_runs` and `workspace.digest`.
- **`test_config_edit.py`, `test_the_cap_and_the_digest_save_to_the_user_file`:** both save to the user file. A `max_active_runs` of `0`, `65` or `true` is an `EditError`, and `null` removes the key.
- **`test_config_edit.py`, `test_the_pack_form_cannot_set_the_cap`:** a `workspace.max_active_runs` change through the pack's `propose` is refused as protected.
- **`test_config_edit.py`, changed on purpose:** in `test_the_user_form_holds_the_allow_list_and_nothing_else` (~line 1611), `if key[0] == "workspace":` becomes `if key[0] == "workspace" and key[1] in layers.WORKSPACE_LISTS:`.
- **`test_dashboard.py`, `test_a_cap_or_digest_change_needs_no_restart_and_no_rescan`:**
  - On the workspace fixture `two` (through `two.dash`), saving `(["workspace", "max_active_runs"], 2)` and `(["workspace", "digest"], True)` gives `restart == []` and `rescanned is False`, and a `workspace.roots` save still gives `rescanned is True`.
  - On `dash`, the same two saves give `restart == []`.
- **`test_dashboard_js.py`, `test_only_the_workspace_folders_section_carries_the_restart_note`:** modelled on `test_a_user_config_that_cannot_be_edited_renders_every_field_disabled`, with `rescans: False` and two sections: Workspace (holding `workspace.roots`) and "Across repositories" (holding `workspace.max_active_runs`, kind `int`). Only the first section's note contains "only after a restart".

- [ ] **Step 2: Run them and see them fail**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_layers.py agentflow/tests/test_config.py agentflow/tests/test_config_edit.py agentflow/tests/test_dashboard.py agentflow/tests/test_dashboard_js.py --import-mode=prepend -q -k "cap or digest or personal_settings or restart_note or allow_list"`
Expected: FAIL. Look for `unknown key` from `workspace_settings`, the missing fields, and a non-empty `restart`.

- [ ] **Step 3: Implement**

- **`layers.workspace_settings`:** the list loop runs over `WORKSPACE_LISTS`, the two scalars are checked after it, and the unknown-key check uses `WORKSPACE_KEYS`.
- **`config_edit._USER_SECTIONS`:** after Workspace, add `("Across repositories", "Machine-wide, read by each run when it needs them; no restart.", [...])` with these rows:
  - `(("workspace", "max_active_runs"), "Most runs working at once on this machine: agent sessions and gates (empty = no cap)", {"kind": "int", "min": 1, "max": 64})`
  - `(("workspace", "digest"), "One notification line over every repository (replaces each repository's digest)", {"kind": "bool"})`
- **`dashboard.py`:** replace `key.startswith("workspace.")`, in both places, with a membership test in `{f"workspace.{k}" for k in layers.WORKSPACE_LISTS}`.
- **`user_settings.js`:** the section test becomes `f.key[0] === "workspace" && f.kind === "list"`.
- **Docs:** the playbook lines listed under Files, in each section's existing style. The two key lines give the range and the default, end with "User file only: a pack that sets it gets a `validate` warning and the value is ignored. Protected.", and point to §17.

- [ ] **Step 4: Run the touched files whole, then lint and validate**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_layers.py agentflow/tests/test_config.py agentflow/tests/test_config_edit.py agentflow/tests/test_dashboard.py agentflow/tests/test_dashboard_js.py agentflow/tests/test_portability.py --import-mode=prepend -q && uvx ruff@0.17.4 check . && bin/agentflow validate`
Expected: all tests pass, then `All checks passed!` and `0 warnings`.

- [ ] **Step 5: Security review, then commit**

Dispatch the `security-auditor` agent on the diff: the `layers.py` user-key surface and the wider allow-list. Fix every confirmed finding, then:
```bash
git add agentflow/layers.py agentflow/config.py agentflow/config_edit.py agentflow/dashboard.py agentflow/dashboard_ui/user_settings.js agentflow/tests docs/agentflow-playbook.md CHANGELOG.md
git commit -m "Add the personal workspace.max_active_runs and workspace.digest keys, editable on Your settings."
```

---

### Task 2: The run cap

**Files:**
- Create: `agentflow/run_cap.py`
- Modify: `agentflow/pipeline.py`: `advance` runs `go()` in `run_cap.turn`; update the module docstring.
- Modify: `agentflow/lanes.py` (~line 992), `agentflow/sizing_lane.py` (`_sizer_call`), `agentflow/gates.py` (`run_gates`): the three take sites.
- Modify: `agentflow/state.py` (`OPEN_HOLDS`), `agentflow/views.py:291`, `agentflow/flow.py:302`, `agentflow/inbox_view.py` (`_watch`)
- Modify: `docs/agentflow-playbook.md` (§17: a "Run cap" subsection), `CHANGELOG.md` (`Added`)
- Test: `agentflow/tests/test_run_cap.py` (new)

**Interfaces:**
- Consumes: `layers.load_user_settings().workspace.max_active_runs` and `layers.MAX_ACTIVE_RUNS` (Task 1), `pack.user_state_root()`, `safeio.ensure_dir`, `state.TERMINAL`, and `hitl.notify_run` (imported lazily).
- Produces:
  ```python
  # state.py
  OPEN_HOLDS = ("run cap",)    # hold reasons with no end time: readers show no "until"
  # run_cap.py
  CAP_DIR, MUTEX = "run_cap", "cap.lock"
  CAP_STEP = "run_cap"         # the hold key's step: `<phase>/run_cap`
  HOLD_REASON = state.OPEN_HOLDS[0]
  POLL_S, MUTEX_WAIT_S = 10.0, 2.0
  @dataclass
  class Slot:
      number: int | None       # None: nothing is held (no cap, or the cap could not be enforced)
      fd: int | None
      def release(self) -> None   # closes fd once; idempotent; never raises
  def limit() -> int | None                                    # the user file's cap; None on no key or any error
  def take(cap: int, root: Path | None = None) -> Slot | None  # None when `cap` or more locks are held
  class Turn:                                                  # one `advance` turn; holds at most one slot
      def __init__(self, run: Run, cfg: Config) -> None
      def close(self) -> None                                  # releases what it holds; idempotent
  @contextmanager
  def turn(run: Run, cfg: Config) -> Iterator[Turn]            # sets `current()` for the block, closes on exit
  def current() -> Turn | None
  @contextmanager
  def working(key: str, stop_on_halt: bool) -> Iterator[bool]  # True: go on (holding, or uncapped);
      # False only with stop_on_halt, when the run was paused or cancelled while held; no turn -> True, nothing taken
  ```
  `root` defaults to `pack.user_state_root()`, and the lock files are `<root>/run_cap/<n>.lock`.

- [ ] **Step 1: Write the failing tests** in `test_run_cap.py`

**Setup:**
- `monkeypatch.setattr(run_cap, "POLL_S", 0.01)`.
- Import `after_hold`, `held_then` and `wait_for` from `test_governor_flow`, and `begin` and `go` from `test_pipeline`.
- Write the user file at `layers.user_config_path()`.
- The step-level tests call `flow_repo.new_run()` first, so `flow_repo.run` exists. They then run `run_step(flow_repo)` (from `test_agent_step`) inside `with run_cap.turn(flow_repo.run, flow_repo.cfg):`.
- A thread starts with an empty context. So where the work runs in a thread, the thread's target opens the turn itself: `def target(): with run_cap.turn(...): box["out"] = run_step(flow_repo)`. A turn opened in the test's own thread caps nothing in the worker.

```python
def test_the_engine_imports_with_run_cap_wired_in():
    # a fresh `python -I -c "import agentflow.gates, agentflow.flow_control, agentflow.pipeline"` (cwd: the repository
    # root) exits 0; and run_cap's inline halt rule equals flow_control.halted for each of "running", "paused", "waiting_human", "held", "error", "done", "failed", "cancelled"

def test_slots_are_taken_lowest_first_and_freed_on_release(tmp_path):
    a, b = run_cap.take(2, tmp_path), run_cap.take(2, tmp_path)
    assert (a.number, b.number) == (0, 1) and run_cap.take(2, tmp_path) is None
    a.release(); a.release()                        # idempotent
    assert run_cap.take(2, tmp_path).number == 0

def test_a_lowered_cap_drains_before_anyone_new_starts(tmp_path):
    held = [run_cap.take(3, tmp_path) for _ in range(3)]
    held[0].release()
    assert run_cap.take(1, tmp_path) is None        # locks 1 and 2 still count against a cap of 1
    held[1].release(); held[2].release()
    assert run_cap.take(1, tmp_path).number == 0

def test_takers_racing_through_the_mutex_never_overshoot(tmp_path):
    # MUTEX_WAIT_S patched to 5.0 (no fail-open under load); 8 threads each take(2, tmp_path) at once:
    # exactly two Slots with a number, six None

def test_a_killed_holder_frees_its_slot(tmp_path):
    # a child python flocks <tmp_path>/run_cap/0.lock, prints "held", sleeps; take(1) is None; kill it; take(1).number == 0

@pytest.mark.parametrize("plant", ["symlinked_dir", "symlinked_lock", "hardlinked_lock", "fifo_lock", "held_mutex"])
def test_an_unusable_lock_fails_open_and_says_so_once(tmp_path, plant, capsys, monkeypatch):
    # monkeypatch run_cap._warned = False; held_mutex: another descriptor holds cap.lock, MUTEX_WAIT_S patched to 0.05
    assert run_cap.take(1, tmp_path) == run_cap.Slot(None, None)
    # a second take is also Slot(None, None); stderr holds "the run cap can't be used" exactly once

def test_limit_reads_the_user_file_and_a_bad_file_is_no_cap():
    # no file -> None; {workspace: {max_active_runs: 2}} -> 2; ": : :" -> None

def test_without_a_turn_nothing_is_capped(flow_repo, tmp_path):
    # cap 1 held by the test; gates.run_gates(cfg, [<a fixture gate>], ...) outside any turn returns at once, no run_held

def test_an_agent_session_past_the_cap_waits_until_a_slot_frees(flow_repo, monkeypatch):
    # cap 1 held by the test; run_step in a thread inside a turn; after_hold releases the test's slot
    (held,) = held_events
    assert held["reason"] == "run cap" and held["key"].endswith("/run_cap") and held["until"]
    assert step result == "done" and the fake agent was called once, after run_hold_ended

def test_a_pause_while_a_session_waits_stops_the_step(flow_repo, monkeypatch):
    # cap 1 held; after_hold emits run_paused (as test_governor_flow does); run_step returns "stopped"; no agent call;
    # one run_hold_ended; then the test releases its slot and take(1) is not None
def test_an_empty_gate_set_never_waits(flow_repo, monkeypatch):
    # inside a turn, cap 1 held by the test: run_gates(cfg, [], ...) returns [] at once with no run_held
def test_a_pause_while_a_gate_set_waits_runs_that_set_once(flow_repo, monkeypatch):
    # inside a turn, run_gates waits; after_hold emits run_paused; the gate results are returned; one run_hold_ended
def test_the_slot_is_given_back_after_each_piece_of_work(flow_repo, monkeypatch):
    # cap 1 inside a turn: after run_step returns, take(1) is not None; with the fake agent raising, the same
def test_every_kind_of_work_takes_a_slot(flow_repo, monkeypatch):
    # spy on run_cap.take: a fresh session, a resume (a two-call script), a sizer call and a run_gates call each call it
def test_a_turn_that_runs_no_work_takes_no_slot(flow_repo, monkeypatch):
    # pipeline._Loop.walk stubbed to ("stopped", False) and preflight off; cap 1 held by the test; go(begin(...)) returns
    # promptly with no run_held
def test_the_turn_releases_its_slot_when_advance_raises(flow_repo, monkeypatch):
    # cap 1; walk stubbed to take a slot via working() then raise RuntimeError; go() raises; take(1) is not None
def test_an_open_hold_shows_no_until(flow_repo):
    # a run_held with reason "run cap" while the test holds run.lock() (display_status "held", as
    # test_dashboard.test_a_held_run_says_why_and_until_when does): the views badge is "held (run cap)";
    # `agentflow status <id>` prints "held:     run cap, step define/run_cap"; inbox_view._watch's text is "run cap"
```

- [ ] **Step 2: Run them and see them fail**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_run_cap.py --import-mode=prepend -q`
Expected: FAIL with an `ImportError` (no `run_cap` module).

- [ ] **Step 3: Implement `run_cap.py`, then wire it in**

**`run_cap.py`:**
- **Module docstring:** state section 9's rules: what takes a slot, the lock files and the mutex, hold rather than refuse, pause at a session versus a gate set, fail open, counting every held lock, no ranking.
- **`take`:** follows the plan decisions.
- **The context variable:** `current()` reads a module-level `contextvars.ContextVar[Turn | None]` that `turn()` sets and resets.

**`working(key, stop_on_halt)`:**
1. With no turn, or a turn already holding a slot (depth > 0), yield True and count the depth.
2. Otherwise read `cap = limit()`. With none, yield True.
3. Call `take(cap)`. If it gives a slot, hold it for the block.
4. If it doesn't:
   - emit `run_held(key=key, reason=HOLD_REASON, until=<now ISO>, cap=cap)` once;
   - call `notify_run(cfg, run, f"{key} is held: {cap} run{'s' if cap != 1 else ''} already working on this machine (workspace.max_active_runs); it carries on by itself.")`;
   - poll every `POLL_S`. Re-read `limit()` each time (None means go on uncapped) and try `take` again. When `halted(run.refresh()["status"])`, yield False with `stop_on_halt`, or else go on uncapped.
5. On leaving the block, release the slot at depth 0.

Failure handling follows the plan decisions.

**`pipeline.advance`:** inside the existing `try`, use `with keepawake.holding(), run_cap.turn(run, cfg): return _Loop(run, cfg, _effective(run, opts), slot).go()`. Add one docstring sentence.

**The three take sites:** wire them as the plan decision says.

**`state.OPEN_HOLDS` and its three readers:** add the constant, and have each reader drop ` until …` for those reasons.

**Playbook §17, "Run cap":**
- what takes a slot, and what doesn't (a person, CI, an approval, the time between two steps);
- the held badge;
- pause and cancel at a session versus a gate set;
- the 10-second check;
- fail open and its stderr line;
- a lowered cap;
- no ranking;
- `init` and retro never capped.

- [ ] **Step 4: Run the new file and the neighbours**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_run_cap.py agentflow/tests/test_pipeline.py agentflow/tests/test_governor_flow.py agentflow/tests/test_agent_step.py agentflow/tests/test_gates.py agentflow/tests/test_dashboard.py agentflow/tests/test_inbox_view.py agentflow/tests/test_portability.py --import-mode=prepend -q && uvx ruff@0.17.4 check .`
Expected: all tests pass, then `All checks passed!`. If one of these files doesn't exist, drop it from the command, and run `ls agentflow/tests` to find the file that covers that module.

- [ ] **Step 5: Commit**

```bash
git add agentflow/run_cap.py agentflow/pipeline.py agentflow/lanes.py agentflow/sizing_lane.py agentflow/gates.py agentflow/state.py agentflow/views.py agentflow/flow.py agentflow/inbox_view.py agentflow/tests/test_run_cap.py docs/agentflow-playbook.md CHANGELOG.md
git commit -m "Hold an agent session or gate set past workspace.max_active_runs until a machine-wide slot frees."
```

---

### Task 3: The merged digest, and the 3.18 release

**Files:**
- Modify: `agentflow/hitl.py`. Add `DigestCounts`, `counts_of_states`, `digest_counts`, `digest_line`, `workspace_digest_text`, `WORKSPACE_DIGEST_FILE`, `STATE_MAX_BYTES` and `_workspace_digest_on`. Keep `digest_text`. `notify` now returns a delivered count, and `notify_run` changes.
- Modify: `docs/agentflow-playbook.md`:
  - the §14 `hitl.digest` line: "unless `workspace.digest` is on";
  - §17 "Notification titles" (~line 999): the merged digest is plainly `agentflow`;
  - a new §17 subsection, "One digest".
- Modify: `docs/specs/agentflow-workspace-proposal.md` (the Rollout row: "built in 3.18")
- Modify: `CHANGELOG.md`, `pyproject.toml`, `agentflow/pack.py` (the release)
- Test: `agentflow/tests/test_hitl.py`

**Interfaces:**
- Consumes: `layers.load_user_settings().workspace.digest` (Task 1), `repo_set.from_user_config`, `pack.repo_id`, `pack.checkout_root`, `pack.state_dir`, `pack.user_state_root`, `safeio.read_bytes`, `safeio.ensure_dir`, `MESSAGE_MAX`.
- Produces:
  ```python
  class DigestCounts(NamedTuple):
      items: int; holding: int; going: int
  def counts_of_states(states: Iterable[dict]) -> DigestCounts   # pure: what inbox_items and PROGRESSING count
  def digest_counts(repo: Path) -> DigestCounts                  # counts_of_states over list_runs(repo)'s states
  def digest_line(c: DigestCounts) -> str           # today's exact line; digest_text(repo) == digest_line(digest_counts(repo))
  WORKSPACE_DIGEST_FILE = "workspace_digest.txt"    # under pack.user_state_root()
  STATE_MAX_BYTES = 8 * 1024 * 1024
  def workspace_digest_text(repo: Path) -> str      # raises only when `repo`'s own runs can't be read; at most MESSAGE_MAX
  def notify(cfg: Config, title: str, message: str, run: Run | None = None) -> int   # entries that delivered
  ```

- [ ] **Step 1: Write the failing tests** in `test_hitl.py`, after the digest block

Build the repositories under `tmp_path / "ws"` with `test_workspace_cli.make_repo`, and write the user file as `{workspace: {roots: [<ws>], digest: true}}`. The `cfg` and `Run.create` setup is the one `test_the_digest_counts_items_and_runs` uses.

```python
def test_counts_of_states_matches_the_inbox(repo, run, cfg):
    # with a parked item, a needs_human wait and a done run: counts_of_states(r.state for r in list_runs(repo))
    # == DigestCounts(len(inbox_items(repo)), <non-parked items>, <runs in PROGRESSING>)

def test_the_workspace_digest_counts_every_repository(...):
    assert hitl.workspace_digest_text(alpha) == "Nothing needs you; 1 run progressing in 2 repositories"
    # park an item on alpha's run (it still progresses); two beta runs, each stopped on a needs_human wait:
    assert hitl.workspace_digest_text(alpha) == (
        "3 items need you (2 hold a run): beta 2, alpha 1; 1 run progressing in 2 repositories")

def test_another_repository_is_read_without_load_lock_or_write(...):
    # monkeypatch state.Run.load to fail for beta's path; beta's state.json mtimes and bytes are unchanged afterwards,
    # and beta still counts

def test_more_than_three_repositories_with_items_are_summed_up(...):
    # five repositories with one item each: labels a 1, b 1, c 1, then "+2 more"

def test_the_run_s_own_repository_counts_when_it_is_outside_the_workspace(...):
    # `repo` fixture outside <ws>: "... in 3 repositories", and its folder name labels its items

def test_a_repository_with_its_own_state_dir_or_a_bad_state_file_is_not_counted(...):
    # beta's pack sets paths.state_dir to a folder holding a planted runs/ tree: nothing there is read, and the line ends
    # "in 1 repository; 1 not counted"; a beta state.json that is not JSON, over STATE_MAX_BYTES, a JSON list, or missing
    # "waiting", a symlinked beta/.agentflow, and a FIFO at beta/.agentflow/config.yaml (the test must not hang) each
    # give the same tail; a symlinked runs/<id> in beta's default dir is skipped, not followed

def test_a_long_line_is_cut_and_kept_whole(...):
    # long repository names: the line is at most hitl.MESSAGE_MAX, and sending it twice sends once

def test_workspace_digest_on_sends_the_merged_line_whatever_the_pack_says(repo, run, cfg, macos_calls):
    # hitl.digest false in cfg; user file digest: true -> one call titled exactly "agentflow" holding the merged line,
    # written to pack.user_state_root() / "workspace_digest.txt"; the same note again sends nothing

def test_a_line_no_notifier_delivered_is_not_kept(...):
    # a cfg with no enabled notifier: notify_run sends nothing and writes no workspace_digest.txt; a later run with macos
    # sends the line

def test_a_merged_digest_that_cannot_be_built_sends_the_plain_message(...):
    # workspace_digest_text raising -> plain message sent, notify_failed(notifier="digest"), no workspace_digest.txt

def test_digest_off_or_an_unreadable_user_file_keeps_the_per_repository_digest(...):
    # digest: false, and ": : :", each with cfg digest on -> the per-repository line in state_dir/notify_digest.txt
```
Every existing digest test stays as it is and passes.

- [ ] **Step 2: Run them and see them fail**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_hitl.py --import-mode=prepend -q -k "digest or counts_of_states"`
Expected: the new tests FAIL (`AttributeError`) and the existing ones pass.

- [ ] **Step 3: Implement**

- **The counts:** `digest_counts` and `digest_text` are rebuilt on `counts_of_states` and `digest_line`, and their output is unchanged.
- **`workspace_digest_text`:** builds section 9's line from the repositories the plan decision counts.
- **`notify`:** counts each entry that `_send` returned from without raising.
- **`notify_run`:** checks `_workspace_digest_on()` first. The merged path mirrors the per-repository one:
  1. Build the text and `path = safeio.ensure_dir(pack.user_state_root()) / WORKSPACE_DIGEST_FILE`, both inside the `try`. On a failure, log `notify_failed` and send the plain message.
  2. When the line is unchanged, send nothing.
  3. Otherwise call `delivered = notify(cfg, DIGEST_TITLE, text, run=run)`, and write `path` only when `delivered` is truthy (a monkeypatched `notify` returns None).
- **The docstrings:** update `notify_run`'s and `notify`'s.
- **Docs:** the playbook lines listed under Files. "One digest" uses section 9's example line, the not-counted rule and the delivery rule.

- [ ] **Step 4: Run the touched files**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_hitl.py agentflow/tests/test_portability.py --import-mode=prepend -q && uvx ruff@0.17.4 check .`
Expected: all tests pass, then `All checks passed!`.

- [ ] **Step 5: Release 3.18**

1. Bump `pyproject.toml`'s `[project] version` and `pack.ENGINE_VERSION` to `3.18` (or past `main`, if it has moved).
2. Rename `## [Unreleased]` to `## [3.18] - <today>`, open a fresh `## [Unreleased]` above it, and edit the spec's Rollout row.
3. Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_packaging.py --import-mode=prepend -q && bin/agentflow validate && uvx ruff@0.17.4 check .`
   Expected: pass, `0 warnings`, `All checks passed!`.
4. Run the full suite (`pytest agentflow/tests`, 25 to 60 minutes) before calling the slice done.

- [ ] **Step 6: Commit**

```bash
git add agentflow/hitl.py agentflow/tests/test_hitl.py docs CHANGELOG.md pyproject.toml agentflow/pack.py
git commit -m "Send one merged digest line over every workspace repository when workspace.digest is on, and release 3.18."
```
