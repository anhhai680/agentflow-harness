# Throughput slice T4 (Park and continue) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A stop inside a plan task no longer stops the run. The runner parks the task, puts its unfinished work aside, and builds every task that doesn't wait on it. The inbox item says what the park blocks. A constraint only a person may lift takes one touch.

**Architecture:**
- **One park, decided by the runner, after the resolver.** Today `lanes._loop` parks only a `needs_human` result the agent marked `can_continue_without`. In T4, every task stop goes through `FeatureLane._settle_task_stop` inside the plan walk:
  1. the resolver tries first (`Resolver.take_over(notify=False)`);
  2. then the runner parks it when the kind is in `hitl.park_kinds` and some other task can still run;
  3. otherwise the stop stays, and a person is told once.

  The agent's flag is still parsed, but it no longer decides anything.
- **Independence is pure code.** The new module `agentflow/park.py` decides which tasks wait on a parked one: `Depends on:` (transitively) plus any file both tasks name (`sources.task_files`). The plan walk, the blast radius and the dashboard's plan view all use this one rule.
- **Parked work is shelved, not left in the tree.** The task's uncommitted changes go into a commit object under `refs/agentflow/shelf/<run>/<task key>`, and its paths go back to `HEAD`. Later tasks therefore build, gate and commit on a clean tree. Before the task runs again, its change is re-applied with `git apply --index`. A shelf that no longer applies stops the run on `<task>/unshelve`, and nothing is overwritten.
- **A parked item is settled the same way as a wait.** `approve`, `answer` and `retry` accept a parked key. A run whose last runnable task is gone *promotes* the first parked item to its wait, as today. When a person settles another parked item, the promoted wait is put back in the parked list (`demote_promoted`), so the run goes on.
- **The constraint override.** `agentflow approve <run> <key> --constraint` (and a dashboard button) records a person's approval. The runner then writes the override commit itself, between steps, with the pack's trailer and the person's name. It records `constraint_approved`, which moves `runner_head` and which `foreign_commits` accepts.
- **The digest** (`hitl.digest: true`, off by default). Each notification becomes one machine-wide line, sent only when it changed: "2 items need you (1 holds a run); 3 runs progressing".

**Tech Stack:** Python 3.12 stdlib, PyYAML, pytest; dashboard JS tested with node (`test_dashboard_js.py`). No new dependency.

**Spec:** `docs/specs/agentflow-throughput-proposal.md`. This plan covers slice **T4** (§8):
- §4.1.1: parking on by default, every task stop, the runner decides independence, batch members park alone, the blast radius;
- §4.1.5: batched approvals only. Speculative planning and the `observe → auto` ramp are T7;
- §4.1.6: the human override for project guards;
- §4.1.7: the digest;
- §6: the park events and the `park_resume_rework` signal;
- the "T4" verification;
- decision 5.

Out of scope:
- `environment` stops. T1 already makes them one machine item, which is rechecked by itself. A broken machine would fail every task, so parking one would only park the next.
- A resolver session's own stop (`…/resolve-N`). It stays a stop: `Resolver.finish_open` resumes the open resolve at the start of every walk, so a parked resolver would run again at once.
- `stop_class: spec_gap` routing. A `spec_gap` question is already answered by `answer_recommended` and filed by `record_step`.

## Global Constraints

- Engine dependencies stay stdlib + `pyyaml>=6`.
- No project strings in the engine (`test_portability.py` `DENYLIST`). Tests and docs call the measured repository "the measured repository" and the pack's guard gate "a constraint gate", never by its file name.
- The golden files `agentflow/tests/fixtures/explain/*.txt` stay byte-identical. Nothing in T4 changes `explain`.
- **Off means today, except the default.** `hitl.park_independent: false` keeps today's behaviour: no park, no shelf, and every stop notifies as it does now. The engine default becomes `true` (spec §4.1.1).
- **The engine floor holds.** Parking never lets a run reach `verify` with a parked task. Every parked task is built, gated, reviewed and committed by the runner before `run_foreach` returns `done`.
- **Only a person lifts a constraint.** `approve(..., constraint=True)` is refused when:
  - `by` is the runner's (`autonomy.AUTO_BY`);
  - it runs inside an agent session (`AGENTFLOW_STEP` is set in the environment);
  - the item has questions;
  - no gate of `guardrails.constraint_override.gates` failed on that key.

  The trailer is written only by `gitops.commit_constraint`, and only for a recorded approval. `gitops.commit` keeps refusing it, as do `check_message` and `reject_trailers`.
- Every new config key ships in the task that first reads it, with all four companions: a Settings form field or a `_PROTECTED_KEYS` entry, `validate` checks, a `test_config_edit.py` case, and a playbook §14 line.
- No engine prompt text is dropped or reworded, so no `pack_fix.py` `Retired` entry is needed. `can_continue_without` stays in `_result.md` as a hint.
- New and changed events:

  | Event | Fields added | `state.REQUIRED` |
  |---|---|---|
  | `step_parked` (spec §6's `unit_parked`) | `stop_class`, `blast_radius: {blocks, continue}`, `shelf`, `shelved`, `demoted`; `paths` is now always `[]` | `("key",)`, unchanged |
  | `unit_unparked` (new) | `{key, shelf, paths, conflict, by}`; `by` is set only when a person settled a conflict | `("key",)` |
  | `constraint_approved` (new) | `{key, by, sha, gates}` | `("key", "sha")` |
  | `hitl_raised` | `promoted` (from `_stop_for_parked`), `notified` | unchanged |
  | `hitl_approved` | `constraint` | unchanged |
  | `plan_batched` (Task 6) | `split` | unchanged |

- Model text shown to a person goes through `hitl.clean_fact`. A name written into a trailer is `clean_fact(by)[:100]`, and it may not be empty.
- Each user-visible change adds a line under `## [Unreleased]` in `CHANGELOG.md`.
- Tests run as `uv run --no-project --with pyyaml --with pytest pytest <path> --import-mode=prepend -q`. Run `uvx ruff@0.17.4 check .` before each commit. Never create or edit `uv.lock`.

## Review Focus

1. **A parked task's shelf meets a file a later task changed without naming it.** The unshelve must refuse, leave the tree as it is, and stop on `<task>/unshelve` with the shelf's ref in the summary. Approving that stop takes the tree as the person left it. (Task 5: `test_an_unshelve_conflict_stops_and_overwrites_nothing`.)
2. **A person settles parked item B while the run waits on promoted item A.** The run must go on with B's task, and A must be parked again rather than lost or answered. (Task 3: `test_settling_a_parked_item_demotes_the_promoted_wait`; Task 5: `test_approving_a_parked_stop_while_another_waits_runs_its_task`.)
3. **A runner killed after `step_parked` and before the shelved paths were reset.** On resume the tree still holds the parked work, and the next task's commit would take it. `run_foreach` must finish the shelving before any task runs. (Task 5: `test_a_runner_killed_mid_park_finishes_the_shelving`.)
4. **An agent session runs `agentflow approve --constraint` on its own stop.** It must be refused before any event is written. (Task 7: `test_a_constraint_approval_from_an_agent_session_is_refused`.)
5. **A `guard_violation` where an agent moved `HEAD`.** It must not park: every later task commit would be refused (`expected_head`). It stays a stop, as today. (Task 5: `test_a_guard_stop_with_a_foreign_head_never_parks`.)

---

### Task 0: The baseline exists

- [ ] **Step 1: Check the baseline.** Run `bin/agentflow scorecard`. Its header names a frozen baseline dated before T1's first commit. If there is none, stop and ask the person to run `agentflow scorecard --freeze`. T4 must not freeze one itself (spec §6).

---

### Task 1: The independence rule (`park.py`) and the plan view on it

**Files:**
- Create: `agentflow/park.py`
- Modify: `agentflow/plan_view.py` (`run_plan`'s `blocks` and `blocked_by`)
- Test: `agentflow/tests/test_park.py` (new), `agentflow/tests/test_plan_view.py`

**Interfaces:**
- Produces, in `agentflow/park.py` (pure; imports only `sources`):
  - `waits_on(task: PlanTask, held: Collection[int], by_n: Mapping[int, PlanTask]) -> bool`: `task.depends_on` meets `held`, or `task_files(task)` shares a path with a held task's `task_files`. A task that names no file shares none.
  - `held_tasks(tasks: Sequence[PlanTask], parked: Collection[int], done: Collection[int]) -> set[int]`: walk in plan order. A task not in `done` is held when it is in `parked` or `waits_on` the tasks held so far. This is exactly what `_plan_pass` does.
  - `blocks(tasks, n: int, done) -> list[int]`: `held_tasks(tasks, {n}, done) - {n}`, sorted.
  - `blast_radius(tasks, parked: Collection[int], n: int, done) -> dict[str, list[int]]`, which returns:
    - `blocks`: `[n, *blocks(tasks, n, done)]`;
    - `continue`: the open tasks not in `done` and not in `held_tasks(tasks, {*parked, n}, done)`.
  - `ranges(numbers: Sequence[int]) -> str`: `"10–13, 15–23"`, with an en dash. It gives the same output as `plan.js` `ranges`.
  - `radius_line(radius: Mapping[str, list[int]]) -> str`.

- [ ] **Step 1: Write the failing tests** in `test_park.py`. They build tasks with `sources.parse_plan_text`:

```python
def test_a_dependent_waits_transitively():   # 3 depends on 2, 4 on 3, 5 none
    assert held_tasks(tasks, {2}, set()) == {2, 3, 4}

def test_a_task_naming_a_held_tasks_file_waits():   # 2 and 5 both `- Modify: \`src/a.py\``, 5 depends on none
    assert held_tasks(tasks, {2}, set()) == {2, 5}

def test_a_task_naming_no_file_shares_none():
    assert held_tasks(tasks, {2}, set()) == {2}

def test_done_tasks_never_wait():
    assert held_tasks(tasks, {2}, {3}) == {2, 4}   # 4 depends on 3, which is done, and on 2

def test_the_blast_radius_reads_like_the_spec():   # 23 tasks, all `none` but 14 depends on 9
    radius = blast_radius(tasks, set(), 9, set(range(1, 9)))
    assert radius == {"blocks": [9, 14], "continue": [10, 11, 12, 13, *range(15, 24)]}
    assert radius_line(radius) == "blocks tasks 9, 14; tasks 10–13, 15–23 continue"
    assert radius_line({"blocks": [9], "continue": [10]}) == "blocks task 9; task 10 continues"
    assert radius_line({"blocks": [9, 14], "continue": []}) == "blocks tasks 9, 14; no other task can run"
```

  In `test_plan_view.py`, add `test_a_task_sharing_a_file_with_a_stopped_task_waits_on_it`. Task 2 is `blocked`, and task 4 (`Depends on: none`) names task 2's file. Expected: task 4's `status == "waiting"`, its `blocked_by == [2]`, and task 2's `blocks` contains 4.

- [ ] **Step 2: Run them and see them fail.** `… pytest agentflow/tests/test_park.py agentflow/tests/test_plan_view.py --import-mode=prepend -q`. Expected: `ModuleNotFoundError: agentflow.park`, then the plan view assertion.
- [ ] **Step 3: Implement `park.py`, then the plan view.** In `run_plan`:
  - the row's `blocks` becomes `park.blocks(tasks, task.n, done)`;
  - `blocked_by` becomes `sorted(m for m in stopped if task.n in park.blocks(tasks, m, done))`.

  `plan_view.dependents` stays for its other callers. The inbox's `_blast_radius` reads those rows, so it follows by itself.
- [ ] **Step 4: Run them and see them pass,** together with `test_inbox_view.py` and `test_dashboard_js.py`.
- [ ] **Step 5: Commit.** `git add agentflow/park.py agentflow/plan_view.py agentflow/tests/test_park.py agentflow/tests/test_plan_view.py`, then `git commit -m "Decide which tasks wait on a stopped one from their dependencies and their shared files."`

---

### Task 2: The shelf (`gitops`)

**Files:**
- Modify: `agentflow/gitops.py`
- Test: `agentflow/tests/test_gitops.py`

**Interfaces:**
- Produces, in `gitops.py`:
  - `shelf_ref(run_id: str, tkey: str) -> str`: returns `f"refs/agentflow/shelf/{run_id}/{tkey.replace('/', '.')}"`. It raises `ValueError` when `git check-ref-format` refuses the result.
  - `shelve(worktree: Path, tree: str, ref: str, message: str) -> str`: `commit-tree <tree> -p HEAD -m <message>`, then `update-ref <ref> <sha>`. It returns the sha and **doesn't touch the index or the tree**.
  - `unshelve(worktree: Path, shelf: str, paths: list[str]) -> list[str]`: applies `git diff --binary <shelf>^ <shelf> -- <paths>` with `git apply --index`. It runs `--check` first and returns `[]` once the change is applied. When the check fails, it returns the sorted `paths` and changes nothing.
  - `drop_shelf(worktree: Path, ref: str) -> None`: `update-ref -d`; a missing ref is fine.
  - Every call uses `*HARDENED`, like `snapshot`.

- [ ] **Step 1: Write the failing tests:**

```python
def test_shelve_keeps_the_work_out_of_the_tree(repo):        # edit a.py, add new b.py
    tree = snapshot(repo); sha = shelve(repo, tree, ref, "Shelve task 2")
    restore_paths(repo, ["a.py", "b.py"], "HEAD")
    assert uncommitted_paths(repo) == [] and rev_parse(repo, ref) == sha
    assert show(repo, f"{sha}:b.py") == "new\n"

def test_unshelve_puts_the_work_back_after_other_commits(repo):   # commit c.py in between
    assert unshelve(repo, sha, ["a.py", "b.py"]) == []
    assert read(repo / "b.py") == "new\n" and read(repo / "c.py") == "c\n"

def test_unshelve_refuses_a_conflict_and_changes_nothing(repo):   # HEAD rewrote a.py's same line
    before = snapshot(repo)
    assert unshelve(repo, sha, ["a.py", "b.py"]) == ["a.py", "b.py"]
    assert snapshot(repo) == before

def test_a_shelf_ref_names_the_task():
    assert shelf_ref("r1", "build/build/task-9") == "refs/agentflow/shelf/r1/build.build.task-9"
```

- [ ] **Step 2: Run them and see them fail** with `ImportError`.
- [ ] **Step 3: Implement the four functions.** `unshelve` feeds the patch to `git apply` on stdin (`subprocess.run(..., input=patch)`). An empty patch returns `[]`.
- [ ] **Step 4: Run `test_gitops.py` and see it pass.**
- [ ] **Step 5: Commit**: `git commit -m "Shelve a task's uncommitted work in a ref and apply it back, refusing a conflict."`

---

### Task 3: Parked stops in the state, the inbox and the ways out

**Files:**
- Modify:
  - `agentflow/state.py`: the `step_parked`, `hitl_raised`, `hitl_approved` and `step_retry` reducers; `REQUIRED["unit_unparked"]`;
  - `agentflow/hitl.py`: `park_wait` (new), `_resolve`, `approve`, `answer`, `reject`, `demote_promoted`, `inbox_items`, `raise_hitl`'s `notified`;
  - `agentflow/run_control.py`: `retry` on a parked key;
  - `agentflow/flow.py`: `cmd_inbox`'s parked line, and `_continue` with a live runner;
  - `agentflow/metrics.py`: `step_parked` in `WAIT_ENDS`.
- Test: `agentflow/tests/test_state.py`, `agentflow/tests/test_hitl.py`, `agentflow/tests/test_metrics.py`

**Interfaces:**
- Consumes: `park.radius_line` (Task 1).
- Produces:
  - `hitl.park_wait(cfg, run, wait: dict, *, blast_radius: dict, shelf: str | None, shelved: list[str], stop_class: str | None, extra: str = "") -> Path`. It is new; today's `park_hitl` stays as it is until Task 5 removes it together with its only caller, `lanes._loop`. It does four things:
    - renders the inbox with `wait`'s summary and questions, then the ways out, `Blast radius: <radius_line>` and `extra` (the stop's report);
    - emits `step_parked` with the fields of the events table;
    - notifies `parked <key>: <radius_line>` through `notify_run`;
    - returns the inbox path.

    The ways out are `answer --key` when the wait has questions, and otherwise `approve <run> <key> [--note]`, or `retry` once the run is paused.
  - `hitl.demote_promoted(run, settled: str) -> None`. It acts when the run's wait has `promoted` and its key isn't `settled`: it re-emits the last `step_parked` of that key, with `demoted: True`.
  - `hitl.approve(...)` and `hitl.answer(...)` accept a parked key, and run `demote_promoted` after their event. `reject` on a parked key raises `NotWaiting("<key> is parked: approve, answer or retry it; reject it once it is the run's wait")`.
  - `run_control.retry` accepts a parked key when no runner is live. Its `step_retry` drops the parked entry, and it then runs `demote_promoted`.
  - `inbox_items`: a parked item gains `stop_kind` (the park's kind) and `blast_radius`.
  - `state`, in each reducer:
    - `_on_step_parked`: a wait on the same key is cleared (`_clear_wait`), and the row becomes `parked` unless it is `failed`;
    - `_on_hitl_raised`: a `failed` row stays `failed`, and the wait keeps `promoted`;
    - `_on_hitl_approved`: on a parked key that isn't the wait, it pops the entry, and a `parked` or `waiting_human` row becomes `running`;
    - `_on_step_retry`: it pops the parked key.

- [ ] **Step 1: Write the failing tests:**

```python
# test_state.py
def test_parking_a_stop_clears_its_wait_and_keeps_a_failed_row(run):
    run.emit("hitl_raised", kind="guard_violation", key=K, phase="build", summary="x", questions=[])
    run.emit("step_finished", key=K, status="failed", summary="x")
    run.emit("step_parked", kind="guard_violation", key=K, summary="x", questions=[], paths=[], shelf="abc",
             blast_radius={"blocks": [2], "continue": [3]})
    assert run.state["waiting"] is None and run.state["status"] == "running"
    assert run.state["steps"][K]["status"] == "failed" and run.state["parked"][K]["blast_radius"]["blocks"] == [2]
    run.emit("hitl_approved", key=K, kind="guard_violation", by="me", summary="x")
    assert run.state["parked"] == {} and run.state["steps"][K]["status"] == "failed"   # its restart checks the tree

def test_a_retry_drops_the_parked_entry(run): ...

# test_hitl.py
def test_a_parked_stop_is_approved_by_its_key(run): ...            # hitl_approved{key, kind: "blocked"}, run not waiting
def test_a_parked_blocked_repeat_needs_a_note(run): ...            # NotWaiting without --note
def test_a_parked_item_cannot_be_rejected(run): ...
def test_settling_a_parked_item_demotes_the_promoted_wait(run):    # Review Focus 2
    # A parked, then promoted (hitl_raised promoted=True); B parked; approve(B)
    assert run.state["waiting"] is None and set(run.state["parked"]) == {A}
    assert run.events.read()[-1]["type"] == "step_parked" and run.events.read()[-1]["demoted"] is True
def test_the_parked_inbox_file_names_the_blast_radius(run, cfg): ...   # "Blast radius: blocks task 2; task 3 continues"
def test_inbox_items_carry_the_stop_kind_of_a_parked_item(run): ...

# test_metrics.py
def test_a_park_ends_the_human_wait(): ...   # hitl_raised at t0, step_parked at t0+60: the human bucket is 60 s
```

  In `test_flow_cli` style, or in `test_hitl.py` through `main`, add `test_approving_while_a_runner_is_live_only_records`. Hold the run lock, call `main(["approve", run.id, K])`, and expect exit 0, the printed line `the runner working on <id> picks it up`, and a recorded `hitl_approved`.
- [ ] **Step 2: Run them and see them fail.**
- [ ] **Step 3: Implement it.**
  - `_resolve` returns `("parked", key)` for a key that is parked and isn't the wait.
  - `approve` takes its item from `state["parked"][key]` in that case. Every existing rule (questions, `blocked_repeat` needs a note, `CLEARED_BY_APPROVE`) reads the item's kind.
  - `raise_hitl` records `notified=<bool>` on the event and the wait.
  - In `_continue`, `lock_state(run.dir) == "live"` prints `recorded; the runner working on <id> picks it up` and returns 0.
- [ ] **Step 4: Run them and see them pass,** together with the existing park tests: `test_state.py`, `test_hitl.py` and `test_feature_lane.py -k park`.
- [ ] **Step 5: Commit**: `git commit -m "Let a parked stop be approved, answered or retried by its key, and park a stop's wait away."`

---

### Task 4: The park keys

**Files:**
- Modify:
  - `agentflow/defaults/config.yaml`: the `hitl:` block;
  - `agentflow/hitl.py`: `PARKABLE`, `park_kinds`, `parks_first`;
  - `agentflow/config.py`: `_Validator.hitl()`, called from `validate`;
  - `agentflow/config_edit.py`: two form fields;
  - `docs/agentflow-playbook.md` §14;
  - `CHANGELOG.md`.
- Test: `agentflow/tests/test_config.py`, `agentflow/tests/test_config_edit.py`, `agentflow/tests/test_hitl.py`

**Interfaces:**
- Produces, in `hitl.py`:
  - `PARKABLE = ("needs_human", "blocked", "blocked_repeat", "gate_failed", "fix_attempts_exhausted", "review_unresolved", "guard_violation", "step_timeout", "turn_cap", "budget_cap")`;
  - `TASK_STOP = re.compile(r"/task-\d+/(implement|review|commit)$")`;
  - `park_kinds(cfg) -> tuple[str, ...]`: `hitl.park_kinds`, or `PARKABLE` when unset;
  - `parks_first(cfg, kind: str, key: str) -> bool`: `hitl.park_independent is True`, the kind is in `park_kinds(cfg)`, and `TASK_STOP.search(key)`.
- Engine defaults: `hitl: {park_independent: true, park_kinds: [<PARKABLE>]}`.

- [ ] **Step 1: Write the failing tests:**
  - `test_parks_first_only_for_a_task_steps_parkable_stop`. It is true for `blocked` on `build/build/task-2/implement` and for `gate_failed` on `…/task-2/commit`. It is false for:
    - `…/task-2/implement/resolve-1`;
    - `budget_exceeded`;
    - `environment`;
    - `park_independent: false`.
  - In `test_config.py`, `validate` refuses `hitl.park_kinds: [budget_exceeded]` (`not a stop a task can park`) and a non-bool `park_independent`, and accepts `[]`.
  - In `test_config_edit.py`, both form fields save and round-trip, the same way `park_independent` does today.
- [ ] **Step 2: Run them and see them fail.**
- [ ] **Step 3: Implement it.**
  - Form fields:
    - `(("hitl", "park_kinds"), "Stops that park a task", {"kind": "list"})`;
    - rename the `park_independent` label to "Park a stopped task and go on".
  - Playbook §14:
    - rewrite the `hitl.park_independent` line: the default is now `true`, and it parks any stop in `park_kinds`, decided by the runner;
    - add a `hitl.park_kinds` line listing `PARKABLE` and saying `[]` parks nothing.
- [ ] **Step 4: Run them and see them pass.** Also run `bin/agentflow validate` (expect "0 warnings") and the golden `explain` tests.
- [ ] **Step 5: Commit**: `git commit -m "Add hitl.park_kinds, turn parking on by default, and name the stops a task may park on."`

---

### Task 5: Park every task stop in the plan walk

**Files:**
- Modify:
  - `agentflow/feature_lane.py`: `_plan_pass`, `_parked`, `_stop_for_parked`, the new park helpers, and the module docstring's parking paragraph;
  - `agentflow/lanes.py`: drop the early park in `_loop`, and `may_park`;
  - `agentflow/autonomy.py`: `Resolver.take_over(notify: bool = True)`;
  - `agentflow/hitl.py`: `raise_hitl`'s default notify rule;
  - `agentflow/pipeline.py`: `_Loop.walk` skips a wait the walk already handled.
- Test: `agentflow/tests/test_feature_lane.py`; update `test_parking_runs_independent_task` and `test_parked_question_answered_while_running_resumes_it`. Also `agentflow/tests/test_pipeline.py`.

**Interfaces:**
- Consumes: `park.held_tasks`, `park.waits_on`, `park.blast_radius` (Task 1); `gitops.shelf_ref/shelve/unshelve/drop_shelf` and `restore_paths` (Task 2); `hitl.park_wait` and `demote_promoted` (Task 3); `hitl.parks_first` (Task 4).
- Removes: `hitl.park_hitl`, `FeatureLane.may_park`, and the early park in `lanes._loop`.
- Produces, on `FeatureLane`:
  - `handled_waits: set[tuple[str, str]]`, keyed by `(wait key, wait since)`;
  - `handled(wait: dict) -> bool`, which `pipeline.walk` calls;
  - `_settle_task_stop(tkey: str, rerun: Callable[[], Outcome]) -> Literal["done", "parked", "stopped"]`;
  - `_park(tkey: str, wait: dict) -> bool`;
  - `_open_shelf(tkey: str) -> dict | None`: the last `step_parked` under `tkey` with a `shelf` and no later `unit_unparked` for that shelf;
  - `_unshelve(tkey: str) -> bool`, where False means stopped on `<tkey>/unshelve`;
  - `_finish_shelving(prefix: str) -> None`.
- `raise_hitl`'s default becomes `notify = not runner_settles(...) and not parks_first(cfg, kind, key)`.

**What `_settle_task_stop` does,** in a loop, when `_run_task` (or `_refine_task`) returned `"stopped"`:
1. It returns `"stopped"` when the run is halted, when it has no wait, when the wait's key isn't under `tkey`, or when the wait is already `handled`.
2. It runs `Resolver(self.cfg, self.run, self).take_over(notify=False)`. When that is true, it calls `rerun()` and starts again; `rerun() == "done"` returns `"done"`.
3. It adds the wait to `handled_waits`. When `self._park(tkey, wait)` succeeds, it returns `"parked"`.
4. Otherwise, it notifies `notice_text(wait)` when `wait.get("notified")` is false and the wait isn't `promoted`, and returns `"stopped"`.

**What `_park` does.** It returns False, and changes nothing, unless all of these hold:
- `parks_first(cfg, wait["kind"], wait["key"])`;
- for `guard_violation`: the key's last `guard_tripped` lists only `forbidden_path` violations, and `head(worktree) == runner_head(run)`;
- the task isn't in a batch of more than one (Task 6 relaxes this);
- with `done` = the tasks that aren't open, and `parked` = the parked task numbers plus this one, some open task outside `done` isn't in `park.held_tasks(tasks, parked, done)`.

Then, in this order (Review Focus 3):
1. `paths` = `changed_paths(worktree, "HEAD^{tree}", snapshot(worktree))`, minus `_held_paths(tkey)` (the `paths` of parks made by an older engine).
2. When there are paths, `sha = shelve(worktree, snapshot, shelf_ref(run.id, tkey), f"Shelve {tkey}")`.
3. `park_wait(...)`. Its `stop_class` is the last `stop_classified` for the key; its `extra` is the last `hitl_raised` `extra` for the key.
4. `restore_paths(worktree, paths, "HEAD")`.

**The rest of the walk.**
- `_plan_pass` holds a task when it is `_parked(tkey)`, or when it is `park.waits_on(task, held, by_n)`. It no longer looks at `depends_on` alone.
- Before `_run_task` or `_refine_task`, it runs `_unshelve(tkey)` for a task with an `_open_shelf`:
  - when the shelf applies (`[]`): `unit_unparked{conflict: false}`, then `drop_shelf`;
  - on a conflict, with an approval of `<tkey>/unshelve` already recorded since: `unit_unparked{conflict: true, by}`, and the walk goes on with the tree as it is;
  - on a conflict otherwise: a `blocked` stop on `<tkey>/unshelve`, whose summary names the paths and the shelf ref.
- `_finish_shelving` runs at the start of `run_foreach`. For each open shelf whose task is still parked, it resets to `HEAD` every shelved path that still differs from `HEAD`.
- `_parked(tkey)` is true when any key in `state["parked"]` is `tkey` or under `tkey/`.
- `_stop_for_parked` emits `hitl_raised(..., promoted=True, notified=True)` and adds the wait to `handled_waits`.

- [ ] **Step 1: Write the failing tests.** They use plans whose tasks say `**Depends on:** none` and name their files; a missing `Depends on` line means the previous task.

```python
def test_a_blocked_task_parks_and_the_independent_ones_finish(lane, git):   # spec "T4" verification, 5 tasks
    lane.script({IMPL.format(n=2): [{"write": {"src/two.py": "half\n"},
                                     "result": {"status": "blocked", "summary": "stuck"}}]})
    assert build(lane, run) == ("stopped", True)
    assert {"Task 1: First thing", "Task 3: …", "Task 4: …", "Task 5: …"} <= set(subjects(git, lane.root))
    assert not (lane.root / "src/two.py").exists()                          # shelved, not in the tree
    (parked,) = lane.events(run, "step_parked")
    assert parked["blast_radius"] == {"blocks": [2], "continue": [3, 4, 5]} and parked["shelf"]
    assert lane.events(run, "resolve_started")[0]["seq"] < parked["seq"]    # the resolver went first
    assert run.state["waiting"]["key"] == IMPL.format(n=2) and run.state["waiting"]["promoted"] is True
    assert "Blast radius: blocks task 2" in run.inbox_path(IMPL.format(n=2)).read_text()
    approve(run, IMPL.format(n=2), by="me")
    assert build(lane, run) == ("done", True)
    assert steps(lane.calls()[n_before:]) == [IMPL.format(n=2), REVIEW.format(n=2)]   # task 2 only
    assert lane.events(run, "unit_unparked")[0]["conflict"] is False
    assert "src/two.py" in git(lane.root, "show", commit_of(git, lane.root, "Task 2: Second thing"), "--name-only")

def test_a_task_naming_the_parked_tasks_file_waits(lane, git): ...
def test_a_gate_failure_before_the_commit_parks(lane, git): ...           # `<task>/commit` gate_failed
def test_no_park_when_every_other_task_waits_on_it(lane, notified): ...   # one stop, one notification, no step_parked
def test_a_guard_stop_with_a_foreign_head_never_parks(lane, git): ...     # Review Focus 5
def test_an_unshelve_conflict_stops_and_overwrites_nothing(lane, git): ...  # Review Focus 1
def test_a_runner_killed_mid_park_finishes_the_shelving(lane, git): ...     # Review Focus 3: emit step_parked by hand, leave the file
def test_approving_a_parked_stop_while_another_waits_runs_its_task(lane, git): ...  # Review Focus 2
def test_a_question_parks_without_can_continue_without(lane, git): ...
def test_parking_off_stops_at_once(lane, git): ...  # existing; add a `blocked` case
```

  `notified` is the fixture of the same name in `test_autonomy.py`: copy it into `test_feature_lane.py`.

  In `test_pipeline.py`, add `test_verify_never_starts_while_a_task_is_parked`. A full `advance` with task 2 blocked never emits `phase_started{phase: "verify"}` until task 2 is approved and committed.

  Update the two existing tests. `src/one.py` is now shelved and comes back on resume. `parked["paths"] == []`, and `parked["shelved"] == ["src/one.py"]`.
- [ ] **Step 2: Run them and see them fail.** `… pytest agentflow/tests/test_feature_lane.py agentflow/tests/test_pipeline.py -k "park or shelf or unshelve" --import-mode=prepend -q`.
- [ ] **Step 3: Implement it** as described above.
  - `lanes._loop` raises every `needs_human` with `raise_hitl`.
  - `pipeline.walk`, after `run_steps` returns `"stopped"`, returns at once when `executor.handled(run.state["waiting"])`, before `take_over`.
  - Grep the suite for plans with `Depends on:** none` whose task stops: each one now parks. Update each assertion so it says what the new behaviour is.
- [ ] **Step 4: Run them and see them pass.** Then run `test_feature_lane.py`, `test_refine.py`, `test_autonomy.py`, `test_carry.py`, `test_plan_view.py` and `test_inbox_view.py` in full.
- [ ] **Step 5: Commit**: `git commit -m "Park any stopped plan task the runner can work around, shelving its work, and build the tasks that do not wait on it."`

---

### Task 6: A batch member parks alone

**Files:**
- Modify: `agentflow/feature_lane.py` (`_park`, `_batches`)
- Test: `agentflow/tests/test_feature_lane.py` (replaces `test_a_batch_member_stops_instead_of_parking`)

**Interfaces:**
- Consumes: `_park` (Task 5).
- Produces: in `_park`, a member of a batch of several that isn't the batch's last is split out first. The runner emits `plan_batched{key: prefix, batches: <the batch without n, then [n]>, split: n}` and sets `self._batch_of` from it. `_batches` already reads the last `plan_batched`, so a resume sees the split, and so do `hitl.rejected_scope` and `review`.
- The batch's last member never parks. Its review covers the members committed before it without a review of their own.

- [ ] **Step 1: Write the failing tests:**

```python
def test_a_batch_member_parks_alone(batched):   # batch [1, 2, 3], all `none`; task 2 blocked
    assert build(batched, run) == ("stopped", True)
    assert batched.events(run, "plan_batched")[-1]["split"] == 2
    assert subjects(git, batched.root)[0] == "Task 3: Third thing"
    assert steps(batched.calls()).count(REVIEW.format(n=3)) == 1             # the batch's one review, without 2
    approve(run, IMPL.format(n=2), by="me"); assert build(batched, run) == ("done", True)
    assert steps(batched.calls())[-1] == REVIEW.format(n=2)                   # 2 alone gets its own review

def test_a_batchs_last_member_stops(batched):
    assert batched.events(run, "step_parked") == [] and run.state["waiting"]["key"] == IMPL.format(n=3)
```

- [ ] **Step 2: Run them and see them fail.**
- [ ] **Step 3: Implement the split.**
- [ ] **Step 4: Run them and see them pass,** together with every `batched` test in `test_feature_lane.py`.
- [ ] **Step 5: Commit**: `git commit -m "Split a stopped batch member out of its batch and park it alone."`

---

### Task 7: The person's override for a constraint gate

**Files:**
- Modify:
  - `agentflow/config.py`: `Guardrails.constraint_trailer` and `constraint_gates`, loading, and `_Validator.guardrails()`;
  - `agentflow/gitops.py`: `commit_constraint`;
  - `agentflow/commit_checks.py`: `constraint_approved` in `RUNNER_COMMITS` and `HEAD_FIELDS`, and `foreign_commits`;
  - `agentflow/hitl.py`: `constraint_gates_failed`, `approve(..., constraint)`, and a `_how_to_go_on` line;
  - `agentflow/constraint.py` (new): `pending` and `write`;
  - `agentflow/pipeline.py` (`walk`) and `agentflow/feature_lane.py` (`_plan_pass`): call `constraint.write`;
  - `agentflow/lanes.py`: `_baseline` rebases on a `constraint_approved` too;
  - `agentflow/flow.py`: `approve --constraint`;
  - `agentflow/state.py`: `REQUIRED`;
  - `agentflow/config_edit.py`: covered by the protected `guardrails` block, plus a test;
  - playbook §14 and §10;
  - `CHANGELOG.md`.
- Test: `agentflow/tests/test_constraint.py` (new), `agentflow/tests/test_config.py`, `agentflow/tests/test_config_edit.py`, `agentflow/tests/test_gitops.py`

**Interfaces:**
- Config: `guardrails.constraint_override: {trailer: str, gates: [str]}`, unset by default. `validate` refuses it when:
  - the trailer isn't in `guardrails.reject_trailers` ("a trailer only a person writes must be one the runner refuses from agents");
  - a gate isn't in `gates`;
  - one of the two is missing.

  It is protected, because the whole `guardrails` block is.
- `hitl.constraint_gates_failed(cfg, run, key) -> list[str]`: the constraint gates whose last `gate_result` at `key`, or under `key/`, has `passed: false`.
- `hitl.approve(..., constraint: bool = False)` records `hitl_approved{..., constraint: True}` after the refusals in Global Constraints.
- `gitops.commit_constraint(worktree, message: str, expected_head: str) -> str`: `commit-tree HEAD^{tree} -p HEAD`, then `update-ref HEAD <new> <expected_head>`. It never touches the index or the tree, and it raises `GitError` when `HEAD` isn't `expected_head`.
- `constraint.pending(events) -> list[dict]`: the `hitl_approved` events with `constraint: True` that have no `constraint_approved` for the same key after them.
- `constraint.write(cfg, run, worktree) -> bool`: one commit per pending approval, then `constraint_approved{key, by, sha, gates}`. A `GitError` raises a `blocked` stop on the key, and the function returns False. The message is:

  ```
  Approve the <gates> constraint for <key>

  <by> approved what `<gates>` refused in `<key>` of run <run id>.

  <trailer>: <by>
  ```
- `foreign_commits` drops the configured trailer's rejection for a sha that a `constraint_approved` recorded, and only for that trailer.

- [ ] **Step 1: Write the failing tests:**

```python
def test_the_override_commits_once_with_no_agent_commit_trip(lane, git):   # spec "T4" verification
    # a constraint gate fails on task 2's commit; approve(run, K, by="Hải", constraint=True)
    assert build(lane, run) == ("done", True)
    (ok,) = lane.events(run, "constraint_approved")
    assert git(lane.root, "log", "-1", "--format=%B", ok["sha"]).rstrip().endswith("Constraints-Approved-By: Hải")
    assert [e for e in lane.events(run, "guard_tripped")] == []
    assert foreign_commits(run, lane.cfg, lane.root, run.state["base_sha"]) == []

def test_a_constraint_approval_from_an_agent_session_is_refused(run, monkeypatch):   # Review Focus 4
    monkeypatch.setenv("AGENTFLOW_STEP", "build/build/task-2/implement")
    with pytest.raises(NotWaiting, match="agent session"): approve(run, K, by="me", constraint=True)
    assert not [e for e in run.events.read() if e["type"] == "hitl_approved"]

def test_the_runner_cannot_approve_a_constraint(run): ...       # by=AUTO_BY refused
def test_no_constraint_approval_without_a_failed_constraint_gate(run): ...
def test_a_rejected_trailer_on_any_other_commit_still_fails_finish(lane, git): ...
def test_validate_needs_the_trailer_in_reject_trailers(): ...
def test_the_settings_page_refuses_constraint_override(): ...   # test_config_edit.py
```

- [ ] **Step 2: Run them and see them fail.**
- [ ] **Step 3: Implement it.**
  - `constraint.write` runs in `walk` before each `run_steps`, and at the top of `_plan_pass`.
  - In `_baseline`, `others` takes `constraint_approved` events as well as other keys' `step_committed`. That way a step resumed after an override rebases instead of tripping `agent_commit`.
  - The `--constraint` flag joins `approve`'s parser.
  - `_how_to_go_on` adds `Only a person can lift this constraint: \`agentflow approve <run> <key> --constraint\`.` when `constraint_gates_failed`. `park_wait` repeats the line.
- [ ] **Step 4: Run them and see them pass,** together with `test_gitops.py`, `test_feature_lane.py` and the finish and conformance tests (`-k "finish or foreign or conformance"`). Run `bin/agentflow validate`.
- [ ] **Step 5: Commit**: `git commit -m "Let a person lift a constraint gate in one step, with the runner writing the trailer commit."`

---

### Task 8: The dashboard: parked stops, the constraint button and batched approvals

**Files:**
- Modify:
  - `agentflow/inbox_view.py`: `_card`, `approve_note`;
  - `agentflow/dashboard.py`: `_approve` reads `constraint`;
  - `agentflow/dashboard_ui/inbox.js`.
- Test: `agentflow/tests/test_inbox_view.py`, `agentflow/tests/test_dashboard.py`, `agentflow/tests/test_dashboard_js.py`

**Interfaces:**
- `_card`'s actions:
  - a parked item with questions gets `["answer"]`;
  - a parked item without questions gets `["approve"]`;
  - add `"approve_constraint"` when `hitl.constraint_gates_failed(cfg, run, key)`, for a wait or a parked item.
- `approve_note` reads `item.get("stop_kind") or item["kind"]`.
- `POST /api/runs/<id>/approve` with `{"target", "constraint": true}` passes `constraint=True`. Any other type is a 400.
- In `inbox.js`:
  - `batchApprovals(items) -> items`: the `group === "approval"` items with `approve` in their actions and `approve_note !== "required"`;
  - the approval group's header gets "Approve all N", which arms on the first press and sends one approve per item on the second;
  - a card with `approve_constraint` gets "Approve the constraint", which also arms on the first press.

- [ ] **Step 1: Write the failing tests:**
  - in `test_inbox_view.py`:
    - `test_a_parked_stop_card_offers_approve`, whose `approve_note` follows the stop kind (`required` for `blocked_repeat`);
    - `test_a_failed_constraint_gate_offers_the_constraint_button`;
  - in `test_dashboard.py`, `test_approve_passes_constraint` and `test_a_non_bool_constraint_is_a_400`;
  - in `test_dashboard_js.py`, `batchApprovals` keeps two spec approvals from two runs, and drops a stop and a `required` note.
- [ ] **Step 2: Run them and see them fail.**
- [ ] **Step 3: Implement it.**
- [ ] **Step 4: Run them and see them pass,** along with the whole of `test_dashboard*.py` and `test_inbox_view.py`.
- [ ] **Step 5: Commit**: `git commit -m "Show parked stops with their way on, the constraint button and approve-all for waiting approvals in the inbox."`

---

### Task 9: The park signals

**Files:**
- Modify: `agentflow/signals.py` (vocabulary, `run_signals`, module docstring); playbook §15's signal list
- Test: `agentflow/tests/test_signals.py`

**Interfaces:**
- `parked_units`: `("lower", False)`. It counts the `step_parked` events without `demoted`, and is always present.
- `park_resume_rework`: `("lower", False)`. It counts the tasks that were parked and afterwards had one of these, and is always present:
  - a `unit_unparked` with `conflict: true`;
  - a `step_retry` on one of their keys.

- [ ] **Step 1: Write the failing tests:**
  - `test_parked_units_skip_demoted_parks`;
  - `test_rework_counts_a_conflict_and_a_retry_once_per_task`;
  - `test_a_run_with_no_park_has_zero_of_both`.
- [ ] **Step 2: Run them and see them fail.**
- [ ] **Step 3: Implement it.**
- [ ] **Step 4: Run `test_signals.py` and `test_scorecard.py` and see them pass.**
- [ ] **Step 5: Commit**: `git commit -m "Count parks and the tasks redone after one in the run's signals."`

---

### Task 10: The notification digest

**Files:**
- Modify:
  - `agentflow/hitl.py`: `digest_text`, and `notify_run`;
  - `agentflow/config.py`: `validate` for `hitl.digest`;
  - `agentflow/config_edit.py`: a form field;
  - playbook §14;
  - `CHANGELOG.md`.
- Test: `agentflow/tests/test_hitl.py`, `agentflow/tests/test_config.py`, `agentflow/tests/test_config_edit.py`

**Interfaces:**
- `hitl.digest`: bool, `false` by default.
- `digest_text(repo: Path) -> str` reads `inbox_items(repo)` and `list_runs(repo)`:
  - `"2 items need you (1 holds a run); 3 runs progressing"`;
  - `"Nothing needs you; 1 run progressing"`.

  An item "holds a run" when it is a run's wait rather than a parked item.
- With `hitl.digest: true`, `notify_run(cfg, run, message)` sends `digest_text(run.repo)` instead of `message`, titled `agentflow`. It sends only when the text differs from the last one sent, which is kept in `pack.state_dir(run.repo) / "notify_digest.txt"` through `safeio`. When the file can't be read, the digest is sent.

- [ ] **Step 1: Write the failing tests:**
  - `test_the_digest_counts_items_and_runs`;
  - `test_an_unchanged_digest_is_not_sent_again`: two `notify_run` calls with no state change in between send once;
  - `test_a_runner_note_never_reaches_the_digest`: a "took the recommended answer" note changes no count, so nothing is sent;
  - `validate` refuses a non-bool `digest`;
  - the form field round-trips.
- [ ] **Step 2: Run them and see them fail.**
- [ ] **Step 3: Implement it.** The form field is `(("hitl", "digest"), "One digest line instead of a notification per stop", {"kind": "bool"})`.
- [ ] **Step 4: Run them and see them pass.** Run `bin/agentflow validate`.
- [ ] **Step 5: Commit**: `git commit -m "Add hitl.digest: one machine-wide notification line per change in what needs a person."`

---

### Task 11: The whole slice

- [ ] **Step 1: Write the docs.**
  - In the playbook, rewrite §8's "Parked questions are not stops either" paragraph for parked stops: the blast radius, the shelf, `<task>/unshelve`, and settling a parked item while another waits. Add `approve --constraint` to §10.
  - In the feature lane's module docstring, rewrite the parking paragraph.
  - Add one `CHANGELOG.md` line per user-visible change that isn't in yet.
  - In the spec's §8 table, set T4's Status to `shipped <date> (PR #<n>)` once it merges.
- [ ] **Step 2: Run the full suite.** `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests --import-mode=prepend -q`. Everything passes, and `git diff --stat agentflow/tests/fixtures/explain` is empty.
- [ ] **Step 3: Lint and validate.** `uvx ruff@0.17.4 check .` prints `All checks passed!`, and `bin/agentflow validate` prints "0 warnings".
- [ ] **Step 4: Check against the spec's "T4" verification.**
  - Task 2 blocked, and tasks 1, 3, 4 and 5 finish: Task 5's first test.
  - The guard override commits once, with no `agent_commit` trip: Task 7.
  - The item lists the blast radius: Task 5 (the inbox file) and Task 8 (the card).
  - Answering it resumes task 2 only: Task 5.
  - `verify` doesn't start until nothing is parked: Task 5 (`test_pipeline.py`).
- [ ] **Step 5: A step for a person: the measured repository's pack.** `.agentflow/**` is forbidden to a run, so a person makes this edit:

  ```yaml
  guardrails:
    constraint_override: {trailer: Constraints-Approved-By, gates: [<its constraint gate>]}
  ```

  Parking is now on by default, with nothing to set. Then run `bin/agentflow validate` there and expect "0 warnings". Compare `human_blocked_share`, `parked_units` and `park_resume_rework` against the frozen baseline over 5 feature runs (spec §6) before T5 starts.

---

## Notes: accepted trade-offs

- **`unit_parked` is `step_parked`.** The park keeps the existing event and gains fields. `state`, `carry.py`, the plan view and the inbox already read it, so a second park event would only give them two paths. `unit_unparked` is new, because nothing records an unshelve today.
- **The shelf replaces "held paths" for new parks.** Before T4, a parked question's half-done files stayed in the tree, and later commits left them out (`_held_paths`). With every stop kind able to park, a half-done file can break the later tasks' gates. So new parks shelve their work, and `paths` is `[]`. `_held_paths` stays only for runs that parked under an older engine.
- **The resolver runs inside the plan walk.** The walk used to hand a task's stop to `Resolver.take_over` only after the whole plan walk had unwound. A park needs the resolver's answer first (spec: "a resolver that couldn't clear the stop" parks), and parking must happen inside the walk so the next task can run. `pipeline.walk` skips a wait the plan walk already handled, so a stop is never resolved twice.
- **No park when nothing else can run.** Parking then would only promote the item back to the run's wait at once, and tell the person twice.
- **A parked item can't be rejected.** `reject` opens a refine round, which is phase state that the running plan walk reads. Rejecting is allowed once the item is the run's wait.
- **A batch's last member stops instead of parking.** Its review is the only review of the members committed before it, so splitting it out would leave them unreviewed.
- **The override's protection is the environment check plus the audit.** An agent runs as the same user, so no local rule fully stops a determined session from calling the CLI. The check refuses inside any session the runner started (`AGENTFLOW_STEP`). The person's name sits in the trailer, the approval and the commit are events, and `foreign_commits` accepts only the trailer a `constraint_approved` recorded. A `Bash(agentflow approve*)` deny rule would change `agents.py` (the security overlay) and every session's argv for little extra; a later slice can add it if a scorecard run ever shows an agent calling `approve`.
- **The digest is opt-in.** It replaces the stop's own words with counts, which some people will want and some won't. The spec's §5 rule (opt-in until proven) wins over §4.1.7's tone.
- **Batched approvals are a dashboard action, not a new endpoint.** "Approve all" sends the existing approve once per item. A partial failure leaves the failed cards in place with their error, as a single approval does.
