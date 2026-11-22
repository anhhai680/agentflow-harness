# Throughput slice T6 (Slots and multi-run) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Several runs of one repository can be live at once without sharing a port, a database or a lock. Each run gets its own slot number, which the pack writes into `defaults.env` and gate commands as `{slot}`. A gate's `resource` lock now holds across runs. The queue keeps `queue.human_slot` slots free for a run started by hand, and the board shows each run's slot and parked items.

**Architecture:**
- **A new module, `agentflow/slots.py`, owns the slot lease.** A slot is a number from `0` to `slots.count - 1`. A run takes one when it is created (`pipeline.start`), records it as `slot_acquired{slot}`, and holds it until its status is terminal (`state.TERMINAL`: `done`, `failed` or `cancelled`). Who holds what is read from the runs' own `state.json` files. Nothing else stores it. A choice is made under one flock, `<runs root>/slots.lock`, so two starts never take the same number.
- **`{slot}` is rendered once per runner, into the config.** `_Loop` (pipeline.py) renders its `Config` with the run's slot (`slots.render`) before any phase, and again on every `reload()`. Every agent env, gate command, digest, preflight check and conformance run downstream reads `self.cfg`, so they all see the same rendered values without any change of their own.
- **Resource locks move from a `threading.Lock` to a file lock.** `gates.run_gates` takes `<state dir>/locks/<resource>.lock` with a polled `fcntl.flock` around each gate that names a `resource`. It does this on the plain sequential path too, not only between groups. Two separate opens conflict within one process as well as across processes, so the same lock covers both cases.
- **Off means today.** With `slots.count: 1`, the engine default, every run renders `{slot}` as `0`. No run is ever refused, no `slot_acquired` is written, the queue ignores `queue.human_slot`, and nothing in `explain` changes.

**Tech Stack:** Python 3.12 stdlib (`fcntl`, `re`, `dataclasses`), PyYAML, pytest, node for the board helper test. No new dependency.

**Spec:** `docs/specs/agentflow-throughput-proposal.md`. This plan covers slice **T6** (§8):
- §4.6 (Lever F): resource slots with `{slot}`, `queue.human_slot`, and parked items on the board;
- the cross-run half of §4.5.1's `resource`;
- §6's event `slot_acquired`;
- the config table's `slots.count` and `queue.human_slot`;
- the "T6" verification.

Out of scope:
- per-worker slots inside one run. T5's sub-agents run no Docker or integration tests (§8), and a run's gates already share its slot;
- runner-level worktrees (§4.4.9);
- a cross-run KPI for "features in flight". It isn't a per-run signal, and `agentflow status` already lists the live runs.

## Global Constraints

- Engine dependencies stay stdlib + `pyyaml>=6`. No project strings in the engine (`test_portability.py` `DENYLIST`). The spec's example names (`tivacare-test`, port `55432`) must never appear in engine code, docs examples inside `agentflow/` or bundled skills. Use `app-test-{slot}` and `5543{slot}` in tests and the playbook.
- The golden files `agentflow/tests/fixtures/explain/*.txt` stay byte-identical. `explain` prints templates, never a rendered `{slot}`.
- **Off means today.** With `slots.count: 1`:
  - no `slot_acquired` event and no `slot` key in `state.json` (add no key to `state.initial_state`; read it with `state.get("slot")`);
  - no run is refused, and the queue starts runs exactly as before;
  - `{slot}` renders as `0`.
- **Slot numbers:** `0 … slots.count - 1`. `slots.count` is a whole number from 1 (off) to `SLOTS_MAX = 16`. `queue.human_slot` is a whole number ≥ 0, default `1`.
- **The `{slot}` token** is replaced only in `defaults.env` values, `gates.<n>.run` and `gates.<n>.affected.run`. It is never replaced in `cwd`, `when_exists`, `inputs`, prompts or `agents.*`. The token is matched by `SLOT_TOKEN = re.compile(r"(?<![{$])\{slot\}(?!\})")`, so a shell `${slot}` and a doubled `{{slot}}` stay as written.
- **The slot is held while the run isn't terminal.** That includes `waiting_human`, `awaiting_approval`, `paused`, `ready`, `error`, a stalled runner and every hold, `ci_pending` too: the run's containers are still up on that port. A slot number at or above a lowered `slots.count` is held by nobody.
- **The runner is the only writer of a run's events.** `slots.acquire` emits on the run it was given, under the slots flock and never from a gate thread.
- The new keys ship with all four companions (CLAUDE.md): a Settings Form field, `validate` checks, a `test_config_edit.py` case and a playbook §14 line. Each user-visible change adds a line under `## [Unreleased]` in `CHANGELOG.md`.
- New events:

  | Event | Fields | `state.REQUIRED` | Reducer |
  |---|---|---|---|
  | `slot_acquired` (new) | `{slot: int, count: int, previous: int?}`. `previous` is present only when a resumed run took a different number | `("slot",)` | `state["slot"] = ev["slot"]` |
  | `gate_result` | `resource_wait_s` (float, 3 decimals), only when the gate's first try at its resource lock failed | unchanged | unchanged |

- Tests run as `uv run --no-project --with pyyaml --with pytest pytest <path> --import-mode=prepend -q`. Run `uvx ruff@0.17.4 check .` before each commit. Never create or edit `uv.lock`.

## Review Focus

1. **Two runs start in the same second with one slot free.** Exactly one gets the slot. The other is refused before its run directory exists, or, if it lost the race after `Run.create`, fails with `run_failed` and no worktree. Never two runs on one number. (Task 2: `test_two_processes_never_take_the_same_slot`.)
2. **A run that is waiting on a person, paused, or held watching CI keeps its slot.** Its database is still up on that port, so the queue must not hand the number to a new run. (Task 2: `test_a_waiting_paused_or_held_run_keeps_its_slot`.)
3. **A pack's own shell variable is named `slot`.** `${slot}` in a gate command, and `{{slot}}` in an `affected.run` template, must reach the shell exactly as written. (Task 2: `test_render_leaves_a_shell_dollar_brace_and_a_doubled_brace_alone`.)
4. **The runner is interrupted while a gate waits on a resource another run holds.** The waiting gate gives up and never starts its command later, and no thread is left blocked on the lock. (Task 4: `test_a_gate_waiting_on_a_resource_stops_when_the_run_leaves`.)
5. **A failed run is retried after another run took its old slot.** It takes another free number (`previous` on the event), and its earlier gate passes aren't reused under the new slot's commands. (Task 2: `test_render_changes_the_gate_digest_with_the_slot`; Task 3: `test_a_retried_run_takes_a_free_slot_again`.)

---

### Task 1: The `slots` and `queue` keys

**Depends on:** none

**Files:**
- Modify: `agentflow/defaults/config.yaml` (add `slots:` and `queue:` blocks after `parallel:`)
- Modify: `agentflow/config.py` (`Slots`, `Queue`, `Config.slots`, `Config.queue`, `_build_slots`, `_build_queue`, `_Validator.slots()`, and the `affected.run` placeholder rule)
- Modify: `agentflow/config_edit.py` (one Form section)
- Modify: `docs/agentflow-playbook.md` (§14)
- Modify: `CHANGELOG.md`
- Test: `agentflow/tests/test_config.py`
- Test: `agentflow/tests/test_config_edit.py`

**Interfaces:**
- Produces:
  - `SLOTS_MAX = 16`, `SLOTS_KEYS = ("count",)`, `QUEUE_KEYS = ("human_slot",)`;
  - `@dataclass class Slots: count: int = 1`, `@dataclass class Queue: human_slot: int = 1`;
  - `Config.slots: Slots` and `Config.queue: Queue`.
- Engine defaults:
  ```yaml
  slots:
    count: 1        # live runs that each get their own {slot} (0 … count-1) in defaults.env and gate commands; 1 = off, at most 16
  queue:
    human_slot: 1   # with slots on, `agentflow queue` leaves this many slots free for a run you start by hand
  ```

- [ ] **Step 1: Write the failing tests.**
  - In `test_config.py`:
    - `validate` errors on `slots.count` values `0`, `17`, `true` and `"2"` (`at most 16`), and on an unknown `slots.foo`. It accepts `1` and `16`;
    - it errors on `queue.human_slot` values `-1`, `true` and `"1"`, and on an unknown `queue.foo`. It accepts `0`;
    - it **warns** when `slots.count: 2` and no `defaults.env` value, gate `run` or `affected.run` holds `{slot}`. The message contains `uses {slot}`. With `defaults.env: {PORT: "5543{slot}"}` there is no warning;
    - it **warns** when `slots.count: 2` and `queue.human_slot: 2` (`never starts a run`);
    - with `slots.count: 1`, neither warning fires, whatever `human_slot` is;
    - an `affected.run` of `pytest -p app-{slot} {paths}` is valid, and `pytest {other} {paths}` is still an error;
    - a dataclass smoothing test: `slots: {count: "x"}` loads as `Slots(count=1)`, so a typo never turns slots on (validate still names it).
  - In `test_config_edit.py`, `test_the_form_sets_the_slot_keys`, shaped like `test_the_form_sets_the_parallel_keys`:
    - `("slots", "count")` is `kind: int` with `min: 1` and `max: 16`;
    - `("queue", "human_slot")` is `kind: int` with `min: 0` and `max: 16`;
    - saving `count: 3` and `human_slot: 0` round-trips through `load_config`;
    - `0`, `17`, `True` and `"3"` for count raise `EditError`.
- [ ] **Step 2: Run them and see them fail.** `… pytest agentflow/tests/test_config.py agentflow/tests/test_config_edit.py -k "slot or queue" …` fails on the missing attributes.
- [ ] **Step 3: Implement it.**
  - `_build_slots(raw) -> Slots` and `_build_queue(raw) -> Queue` follow `_build_parallel`: a value of the wrong type loads as the default.
  - `_Validator.slots()` reads the raw text, as `parallel()` does, and covers both blocks. Call it next to `v.parallel()`.
  - The `{slot}` scan for the warning uses `slots.SLOT_TOKEN`'s pattern. Define the regex in `config.py` as `SLOT_TOKEN`, and have Task 2's `slots.py` import it from there: `config` must not import `slots`.
  - In `_Validator.affected`, the allowed fields become `("paths", "", None)` and `("slot", "", None)`. The message becomes `affected.run is a non-empty command whose only placeholders are `{paths}` and `{slot}`; write a literal brace as `{{` or `}}``. Update any test that matched the old wording.
  - Form section, after "Parallel build": `("Several runs at once", "Slots give each live run its own number for ports and databases ({slot}); the queue leaves some for you.", [ (("slots", "count"), "Slots (1 = off, at most 16)", {"kind": "int", "min": 1, "max": 16}), (("queue", "human_slot"), "Slots the queue keeps free for a run you start", {"kind": "int", "min": 0, "max": 16}) ])`.
  - Playbook §14 gets two lines, next to the `parallel.*` lines:
    - `slots.count`: 1 is off; the numbers; when a run takes and gives back its slot; refused starts; the validate warning. Not protected.
    - `queue.human_slot`: only with slots on; the queue starts a run only while more than this many slots are free. Not protected.
  - `CHANGELOG.md`: one `Added` line for the two keys.
- [ ] **Step 4: Run them and see them pass.** Also run `bin/agentflow validate` (expect "0 warnings"), `test_explain_golden.py` and `test_layers.py`.
- [ ] **Step 5: Commit.** `git commit -m "Add slots.count and queue.human_slot, off by default, and allow {slot} in a gate's affected command."`

---

### Task 2: The slot lease and rendering (`slots.py`)

**Depends on:** Task 1

**Files:**
- Create: `agentflow/slots.py`
- Modify: `agentflow/state.py` (reducer and `REQUIRED` for `slot_acquired`)
- Test: `agentflow/tests/test_slots.py` (new)

**Interfaces:**
- Consumes: `Config.slots.count`, `Config.queue.human_slot`, `config.SLOT_TOKEN` (Task 1); `state.list_runs`, `state.runs_root`, `state.TERMINAL`, `Run.emit`, `Run.refresh`.
- Produces (in `agentflow/slots.py`):
  - `class SlotsFull(Exception)`. Its message names every holder: `all 2 slots are held: 0 by <run id>, 1 by <run id>; cancel one of them or raise slots.count`;
  - `enabled(cfg: Config) -> bool`, which is `cfg.slots.count > 1`;
  - `render(cfg: Config, slot: int) -> Config`. It returns a copy (`dataclasses.replace`) with `SLOT_TOKEN` replaced by `str(slot)` in `defaults.env` values, each `Gate.run` and each `Gate.affected.run`. Everything else, `cfg.path` and `cfg.raw` included, is unchanged, and `cfg` itself is not mutated;
  - `held(cfg: Config, repo: Path) -> dict[int, str]`: slot → run id, for runs whose status isn't in `TERMINAL` and whose `state.get("slot")` is an int below `cfg.slots.count`;
  - `free(cfg: Config, repo: Path) -> list[int]`: the numbers not in `held`, ascending;
  - `check_free(cfg: Config, repo: Path) -> None`: with slots on and `free` empty, it raises `SlotsFull`. Otherwise it does nothing;
  - `acquire(cfg: Config, run: Run) -> int`:
    - off: it returns `0` and writes nothing;
    - on, under `_lock(run.repo)`, after `run.refresh()`:
      - if the run's own slot is below `count` and no *other* run holds it, it returns that slot and writes nothing;
      - otherwise it takes the lowest free number and emits `slot_acquired{slot, count, previous?}`;
      - with no free number, it raises `SlotsFull`;
  - `slot_of(run: Run) -> int`: `state.get("slot")` when it is an int, else `0`;
  - `queue_hold(cfg: Config, repo: Path) -> str | None`. Off: `None`. On: `None` while `len(free) > cfg.queue.human_slot`, else `waiting for a free slot: <held> of <count> held, queue.human_slot keeps <h> free`;
  - `_lock(repo: Path)`: a context manager holding `fcntl.flock(LOCK_EX)` on `runs_root(repo) / "slots.lock"`. The file is opened with `os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW`, mode `0o600`.

- [ ] **Step 1: Write the failing tests** in `test_slots.py`. Use the `pipe`-style `flow_repo` fixture and `Run.create`, and emit `slot_acquired` and status events directly where a test needs a state:
  - `test_off_takes_slot_zero_and_writes_nothing`: with `count: 1`, `acquire` returns `0`, the run has no `slot_acquired`, `"slot" not in run.state`, and `check_free` never raises, even with 5 live runs;
  - `test_runs_take_the_lowest_free_number`: with `count: 3`, three runs get `0`, `1` and `2`, and `held` maps each number to its run;
  - `test_a_full_house_raises_slots_full_naming_the_holders`: the fourth `acquire` raises `SlotsFull`, and the message contains both run ids and `raise slots.count`. `check_free` raises the same;
  - `test_a_waiting_paused_or_held_run_keeps_its_slot`: runs in `waiting_human`, `paused` and `running` with a `ci_pending` hold, and one in `error`, all still count in `held`;
  - `test_a_terminal_run_gives_its_slot_back`: after `run_finished`, `run_failed` or `run_cancelled`, its number is in `free`;
  - `test_acquire_keeps_a_runs_own_slot`: a second `acquire` on the same run returns the same number, and there is still exactly one `slot_acquired`;
  - `test_a_retried_run_takes_another_slot_when_its_own_is_taken`: run A held `0` and failed, run B took `0`, and A's `acquire` returns `1` with `previous: 0`;
  - `test_a_slot_above_a_lowered_count_is_given_up`: a run holding `2` under `count: 2` is held by nobody, and its `acquire` takes a number below 2;
  - `test_two_processes_never_take_the_same_slot`: two `subprocess` children each create a run and call `acquire` after waiting on a shared `go` file. With `count: 2` and one other live run, exactly one child prints a slot and the other prints `SlotsFull`;
  - `test_render_replaces_the_token_in_env_and_gate_commands`: `defaults.env: {PORT: "5543{slot}"}`, `run: "docker compose -p app-test-{slot} up"` and `affected.run: "pytest -p app-{slot} {paths}"` render with slot `2` as `"55432"`, `app-test-2` and `app-2 {paths}`, and the original `cfg` is unchanged;
  - `test_render_leaves_a_shell_dollar_brace_and_a_doubled_brace_alone`: `echo ${slot} {slot}` renders as `echo ${slot} 3`, and `{{slot}}` stays `{{slot}}`;
  - `test_render_changes_the_gate_digest_with_the_slot`: `gates.command_digest(g, env)` of the gate rendered with slot 0 differs from the one rendered with slot 1, and is equal for the same slot;
  - `test_queue_hold_keeps_the_human_slot`: with `count: 3` and `human_slot: 1`, `queue_hold` is `None` with 0 or 1 held. It is the `waiting for a free slot` text with 2 held. With slots off it is always `None`.
- [ ] **Step 2: Run them and see them fail** (`ModuleNotFoundError: agentflow.slots`).
- [ ] **Step 3: Implement `agentflow/slots.py` and the `slot_acquired` reducer** in `state.py`: `state["slot"] = ev["slot"]`, `REQUIRED["slot_acquired"] = ("slot",)`. The module docstring states the lease rule from Global Constraints, in the style of `gates.py`'s docstring.
- [ ] **Step 4: Run them and see them pass.** Also run `test_state.py`.
- [ ] **Step 5: Commit.** `git commit -m "Lease each live run a slot number and render {slot} into a run's config."`

---

### Task 3: Runs take, keep and render their slot

**Depends on:** Task 2

**Files:**
- Modify: `agentflow/pipeline.py` (`start`, `_set_up`, `advance`, `_Loop.__init__`, `_Loop.reload`)
- Modify: `agentflow/flow.py` (`cmd_run` prints `SlotsFull` as an error, `_print_run_detail` prints a `slot:` line)
- Modify: `CHANGELOG.md`
- Test: `agentflow/tests/test_slots.py` (the pipeline tests; reuse `begin` and `go` the way `test_pipeline.py` defines them)

**Interfaces:**
- Consumes: `slots.check_free`, `slots.acquire`, `slots.render`, `slots.slot_of`, `slots.SlotsFull` (Task 2).
- Produces: `_Loop.slot: int`. After `_Loop.__init__`, `self.cfg` is always the rendered config.

- [ ] **Step 1: Write the failing tests** (`flow_repo.configure({"slots": {"count": 2}, "defaults": {"env": {"AF_PORT": "5543{slot}"}}, "gate_sets": {"build": ["ok"], "verify": ["ok"]}})`, with `AGENTFLOW_FAKE_ENV=AF_PORT` set by `monkeypatch.setenv`):
  - `test_two_live_runs_get_different_slots_in_their_agents_env`: `begin` two runs and `go` each to `awaiting_approval` at `define`. Their `state["slot"]` values are `{0, 1}`, and the `AF_PORT` values in `flow_repo.calls()` env are `{"55430", "55431"}`;
  - `test_a_third_run_is_refused_before_it_exists`: with both slots held, `begin` raises `SlotsFull`, and `runs_root` holds no new run directory and `git worktree list` no new worktree. `main(["run", "feature", "--module", "x"])` prints `error: all 2 slots are held` and returns non-zero;
  - `test_a_cancelled_run_frees_its_slot_for_the_next_start`: cancel one run (`run_control.cancel`), and a third `begin` succeeds with that run's number;
  - `test_a_retried_run_takes_a_free_slot_again`: run A (slot 0) fails, run B starts and takes 0, then A is retried (`run_control.retry`) and advanced. A has `slot_acquired` with `slot: 1, previous: 0`, and its agents see `55431`;
  - `test_resume_with_no_free_slot_changes_nothing`: as above, but with both slots held when A is retried. `advance` raises `SlotsFull`, A's status stays what `retry` left, and no `runner_error` or `slot_acquired` is written;
  - `test_a_reloaded_config_is_rendered_again`: edit the pack's `defaults.env` between phases to `"6543{slot}"`. The next phase's agent sees `6543<slot>`, never the raw `{slot}`;
  - `test_slots_off_leaves_a_run_as_it_was`: with `count: 1`, two runs both render `55430`, neither has `slot_acquired`, and `"slot" not in run.state`;
  - `test_status_prints_the_slot`: `main(["status", run.id])` prints `slot:     1` when on, and no `slot:` line when off.
- [ ] **Step 2: Run them and see them fail.**
- [ ] **Step 3: Implement it.**
  - `start`: call `slots.check_free(cfg, repo)` after `_check_new_module` and before `Run.create`, so the common refusal leaves nothing behind.
  - `_set_up`: call `slots.acquire(cfg, run)` first, before `save` and the worktree. The existing `except` in `start` turns a lost race into `run_failed`.
  - `advance`: right after `lock.acquire()` succeeds, call `slot = slots.acquire(cfg, run)`. On `SlotsFull`, release the lock and re-raise, outside the `try` that writes `runner_error`.
  - Pass `slot` into `_Loop(run, cfg, opts, slot)`. `__init__` sets `self.slot` and `self.cfg = slots.render(cfg, slot)` before `make_agents`. `reload()` sets `self.cfg = slots.render(cfg, self.slot)` before `make_agents`. `_config_sha` reads `cfg.path`, which `render` keeps.
  - `flow.py`: `SlotsFull` joins the errors `cmd_run` and the resume commands print as `error: …` (see `_human_errors`). `_print_run_detail` prints `slot:     <n>` when `st.get("slot")` is an int.
  - `CHANGELOG.md`: an `Added` line saying runs take a slot, a full house refuses a start, and `status` shows the slot.
- [ ] **Step 4: Run them and see them pass.** Also run `test_pipeline.py`, `test_explain_golden.py` and `test_control_cmds.py`.
- [ ] **Step 5: Commit.** `git commit -m "Give every run a slot at start, render it into the runner's config, and refuse a start when no slot is free."`

---

### Task 4: Resource locks across runs

**Depends on:** none

**Files:**
- Modify: `agentflow/gates.py` (`resource_lock`, `run_gates`, `GateResult.waited_s`, `record_results`, the module docstring)
- Modify: `agentflow/config.py` (the `Gate.resource` comment says "in a run or across runs")
- Modify: `docs/agentflow-playbook.md` (§14 `gates.<name>.resource`: drop "locks across runs come later")
- Modify: `CHANGELOG.md`
- Test: `agentflow/tests/test_gates.py`

**Interfaces:**
- Consumes: `pack.state_dir(cwd)`. It resolves a linked worktree to its main checkout's state dir.
- Produces:
  - `RESOURCE_POLL_S = 0.2`;
  - `resource_lock(cwd: Path, name: str, live: _Live | None = None) -> ContextManager[float]`. It yields the seconds it waited, `0.0` when the first `LOCK_EX | LOCK_NB` try succeeded. The lock file is `pack.state_dir(cwd) / "locks" / f"{name}.lock"`, opened with `O_RDWR | O_CREAT | O_NOFOLLOW`, mode `0o600`, in a directory created with mode `0o700`. Between tries it sleeps `RESOURCE_POLL_S`. When `live` is closed it raises a private `_Left(Exception)`, which `run_group` catches as "stop this group", the way it already returns on `live.closed`;
  - `GateResult.waited_s: float = 0.0`. `record_results` writes `resource_wait_s` only when `waited_s > 0`.

- [ ] **Step 1: Write the failing tests** in `test_gates.py`, in the "groups and resources" section:
  - `test_a_resource_holds_across_two_processes`: two `subprocess` children each run `run_gates` on their own worktree of one repo (`git worktree add`). The gate has `resource: "db"` and runs `mkdir <shared>/db.lock || exit 1; sleep 0.5; rmdir <shared>/db.lock`. Both children start after a shared `go` file appears. Both gates pass, and one result has `waited_s > 0`;
  - `test_without_a_resource_two_processes_do_overlap`: the same children with no `resource`. Exactly one gate fails, which proves the test above can see an overlap;
  - `test_an_ungrouped_gate_with_a_resource_still_takes_the_lock`: hold the lock from the test with `resource_lock(tmp_git, "db")` in a thread for 0.6 s. A single ungrouped gate with `resource: "db"` passes with `waited_s >= 0.4`;
  - `test_a_gate_waiting_on_a_resource_stops_when_the_run_leaves`: hold the lock from the test. Patch `_collect` to raise `KeyboardInterrupt` once the waiting thread has polled at least twice. Then `run_gates` raises, the gate's command never ran (its marker file is absent), and `pool.shutdown` returned within `RESOURCE_POLL_S * 5`;
  - `test_a_waited_gate_records_its_wait`: `record_results` of a result with `waited_s=1.25` writes `resource_wait_s: 1.25`, and one with `0.0` writes no such field;
  - `test_a_shared_resource_never_overlaps` (existing) still passes unchanged.
- [ ] **Step 2: Run them and see them fail.**
- [ ] **Step 3: Implement it.**
  - Remove `locks = {…threading.Lock()…}`. In `run`, wrap the call to `_run_one` in `resource_lock(cwd, gate.resource, live)` when the gate has a `resource`, on both the plain path and the group path. Set `waited_s` on the result. The lock covers only the gate's command, never the `_tiered` decision. A gate skipped at its tier takes no lock.
  - Keep `duration_s` as the command's time only. It is measured inside `_run_one`, after the lock.
  - Update the module docstring's "Groups" paragraph: a `resource` lock holds across runs of the repository (the state dir's `locks/`), on every path.
  - `CHANGELOG.md`: a `Changed` line saying `gates.<name>.resource` now holds across runs.
- [ ] **Step 4: Run them and see them pass.** Run all of `test_gates.py`, plus `test_init.py`: `--baseline` runs gates in a worktree in the state dir.
- [ ] **Step 5: Commit.** `git commit -m "Hold a gate's resource lock across runs with a file lock in the state dir, and record how long a gate waited for it."`

---

### Task 5: The queue keeps the human slot

**Depends on:** Task 2

**Files:**
- Modify: `agentflow/module_queue.py` (`queue_tick`)
- Modify: `docs/agentflow-playbook.md` (§9: a paragraph on slots and the human slot; §14 `ci.watch_max_h`: replace "until slots exist (T6)" with the slot rule)
- Modify: `CHANGELOG.md`
- Test: `agentflow/tests/test_next_queue.py`

**Interfaces:**
- Consumes: `slots.queue_hold(cfg, repo)` (Task 2), and the existing `_report(entry, reason, prior, say)`.

- [ ] **Step 1: Write the failing tests**, using the file's existing `queue_tick` fixtures and fake `start`:
  - `test_the_queue_leaves_the_human_slot_free`: with `slots.count: 3`, `human_slot: 1`, `--max-parallel 5` and four ready modules, one tick starts two. The third module's entry has `blocked` starting `waiting for a free slot`, and that line is logged once;
  - `test_the_slot_wait_is_said_once_until_it_changes`: a second tick with nothing changed logs nothing new;
  - `test_a_freed_slot_lets_the_queue_start_again`: finish one started run (`run_finished`), and the next tick starts the waiting module and clears its `blocked`;
  - `test_max_parallel_still_caps_the_queue_with_slots_on`: with `count: 4`, `human_slot: 0` and `max_parallel: 2`, a tick starts two;
  - `test_slots_off_ignores_the_human_slot`: with `count: 1` and `human_slot: 1`, a tick starts as many as `max_parallel` allows, as today.

  The fake `start` must create a real run holding a slot (`Run.create` + `slots.acquire`), so that `held` sees it.
- [ ] **Step 2: Run them and see them fail.**
- [ ] **Step 3: Implement it.** In `queue_tick`'s start loop, after the `hold`/`occupied` check, compute `wait = slots.queue_hold(cfg, repo)`. When it isn't `None`, call `_report(entry, wait, found.prior, say)` and `break`. It is evaluated per start, so a start the same tick made counts. Playbook §9: a short paragraph saying:
  - a slot is held while the run is live, a CI watch included;
  - so with slots on, `--max-parallel` and `slots.count - queue.human_slot` both cap the queue;
  - a hand start may take the reserved slot.

  `CHANGELOG.md`: an `Added` line.
- [ ] **Step 4: Run them and see them pass.** Also run the whole of `test_next_queue.py`.
- [ ] **Step 5: Commit.** `git commit -m "Keep queue.human_slot slots free in agentflow queue when slots are on."`

---

### Task 6: The board shows each run's slot and parked items

**Depends on:** Task 2

**Files:**
- Modify: `agentflow/views.py` (`_card`)
- Modify: `agentflow/dashboard_ui/board.js` (a pure `parkedLabels(card)` and its node in the card)
- Modify: `CHANGELOG.md`
- Test: `agentflow/tests/test_dashboard.py`
- Test: `agentflow/tests/test_dashboard_js.py`

**Interfaces:**
- Consumes: `state["parked"]` (T4: `{key: {kind, key, phase, summary, since, …}}`) and `state.get("slot")`.
- Produces:
  - card fields `slot: int | None`, which is `None` when the state has none;
  - card field `parked: list[{key, label, kind, summary, since}]`, ordered by `since` then `key`. `label` is `step_title(key)` (views.py), and `summary` is cut to 200 characters;
  - `parkedLabels(card) -> [{text, title}]`, with `text` `"parked: " + (label || key)` and `title` the summary. It returns `[]` when `card.parked` is empty or missing.

- [ ] **Step 1: Write the failing tests:**
  - `test_dashboard.py` `test_a_card_lists_its_parked_items_and_its_slot`: a run with two `step_parked` events and `slot_acquired{slot: 1}` gives a `board()` card with `slot == 1`, and `parked` holds both keys in `since` order with their kinds. A run with neither has `slot is None` and `parked == []`;
  - `test_dashboard_js.py` `test_parked_labels_name_each_parked_step`: `js(["board.js"], "parkedLabels(args.card)", {"card": {...}})` returns one label per item, with its summary as the title, and `[]` for a card with no `parked`.
- [ ] **Step 2: Run them and see them fail.**
- [ ] **Step 3: Implement it.** In the card's `el("article", …)`, after the review-skip line:
  - one `div.sub` of `span.badge` per `parkedLabels` entry;
  - a `span.badge.lock` `slot N` when `card.slot` is a number.

  Use the existing classes. Add no new CSS unless a badge is unreadable. `CHANGELOG.md`: an `Added` line.
- [ ] **Step 4: Run them and see them pass.** Also run `test_dashboard.py` whole.
- [ ] **Step 5: Commit.** `git commit -m "Show each run's slot and parked items on its board card."`

---

### Task 7: The whole slice

**Depends on:** Task 3, Task 4, Task 5, Task 6

- [ ] **Step 1: Write the docs.**
  - Playbook §8 or §9: a "Several runs at once" paragraph covering:
    - `slots.count` and `{slot}`, with the example `POSTGRES_HOST_PORT: "5543{slot}"` and `docker compose -p app-test-{slot} …`;
    - when a slot is taken and given back;
    - the refusal message and how to free a slot (`agentflow cancel`);
    - `resource` for what can't be multiplied;
    - the human slot;
    - parked items on the board.
  - Add `slot_acquired` and `resource_wait_s` to the playbook's event list, if it has one (`grep -n 'slot_acquired\|wave_started' docs/agentflow-playbook.md`).
  - Check that every user-visible change has its `CHANGELOG.md` line.
- [ ] **Step 2: Run the full suite.** `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests --import-mode=prepend -q`. Everything passes, and `git diff --stat agentflow/tests/fixtures/explain` is empty.
- [ ] **Step 3: Lint and validate.** `uvx ruff@0.17.4 check .` prints `All checks passed!`, and `bin/agentflow validate` prints "0 warnings".
- [ ] **Step 4: Check against the spec's "T6" verification:**

  | Check | Covered by |
  |---|---|
  | Two concurrent fixture runs with `slots.count: 2` get different `{slot}` values | Task 3 `test_two_live_runs_get_different_slots_in_their_agents_env` |
  | A `resource`-locked gate never overlaps across them | Task 4 `test_a_resource_holds_across_two_processes` (with its control) |
  | `slots.count` 1 is today's behaviour; `explain` unchanged | Task 3 `test_slots_off_leaves_a_run_as_it_was`; Step 2's golden diff |
  | `queue.human_slot` stays free | Task 5 |
- [ ] **Step 5: Release.** Per CLAUDE.md:
  - raise `pyproject.toml` `[project] version` and `agentflow/pack.py` `ENGINE_VERSION` from `3.1` to `3.2` (bump again if `main` has moved past it);
  - rename `## [Unreleased]` to `## [3.2] - <today>` and add a fresh, empty `## [Unreleased]` above it;
  - run `test_packaging.py`.

  In the spec's §8 table, set T6's Status to `shipped <date> (PR #<n>)` once it merges. Commit with `git commit -m "Release engine 3.2, with slots off by default."`
- [ ] **Step 6: A step for a person: the measured repository's pack.** `.agentflow/**` is forbidden to a run, so a person makes this edit:
  - set `slots: {count: 3}`;
  - write `{slot}` into its database port in `defaults.env` and into its compose project name in the `postgres_up` gate;
  - mark any gate that still shares a single thing with a `resource`;
  - run `bin/agentflow validate` there and expect "0 warnings".

  Then run `agentflow queue --max-parallel 3` on three ready modules. Compare the conformance SLO, `human_blocked_share` and the flaky-stop count against the frozen baseline before calling the slice green (§6).

---

## Notes: accepted trade-offs

- **A run keeps its slot through a CI watch.** §4.5.5 says a CI-watching run "costs no slot", meaning the queue's `--max-parallel` slot, and `FREE_HOLDS` already frees that one. The resource slot stays held, because a failing check reopens fix sessions and gates on the same port, and the run's containers are still up. A pack that wants more runs in flight raises `slots.count`.
- **A retried run may come back on another number.** Its earlier gate passes were digested with the old slot's commands, so they aren't reused, and conformance runs the full gates again on the final tree. That is slower, but never weaker evidence. Preferring the run's old number makes this rare.
- **Resource waits are a `gate_result` field, not a `slot_acquired{resource}` event.** §6 lists `slot_acquired{slot, resource}`. Gates run in threads that hold no `Run`, and the runner is the only writer of events, so the wait travels on the result the runner already records. `slot_acquired` carries the run's slot only.
- **A resource lock is one per name for the whole repository, never per slot.** §4.6: a resource is what *can't* be multiplied. What can be multiplied is a `{slot}`.
- **A hand start with no free slot is refused, not held.** A hold would need a waker for a run that has no runner yet. The refusal names the holders, and `agentflow cancel` or a higher `slots.count` is the person's call.
- **`{slot}` isn't rendered for commands that run outside a run.** That covers `init --baseline`, `doctor`'s program lookup and `preflight.recheck_waiting`'s probe: they read program names, which a slot number doesn't change.
