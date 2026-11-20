# agentflow cost, slice C5 (carry the implementer across plan tasks) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A `foreach: plan_task` step can let the next task's implementer resume the previous task's session (`carry`). This only happens while that session is small, clean and young. The default stays off (`{max_tasks: 1}`), so a run that doesn't set `carry` is unchanged.

**Architecture:**
- **The decision is a pure function.** `carry.decide(...)` in a new `agentflow/carry.py` reads the run's events and step rows and returns `(share_key | None, reason)`. It reads no file and no clock, as `signals.py` does.
- **The wiring.** `FeatureLane._plan_pass` already passes `share` (a step key whose session a new step continues) to `_run_task`, for a small plan's `shared_session` and for batch members. A task that neither of those shares asks `_carry` instead.
  - `_carry` calls `carry.decide` once per task key and records the answer: `session_carried` or `carry_declined`.
  - On a carry it passes the previous implement key as `share`, so `run_agent_step`'s existing `takes_shared` path resumes that session. That path keeps the session's effort, brief pin and read-only setting.
  - The carried prompt is the step's own task prompt, built as a continuation, so the skill isn't inlined again.
- **What stays per task:** the reviewer (always a fresh session), the gates, the guards, the commit and the attribution. All of them are already keyed per task and don't look at `share`.

**Tech Stack:** Python 3.12 stdlib + PyYAML; pytest with `tests/fake_agent.py` (its `usage_pct` and `compact` act keys already cover the context and compaction rules, so the fake needs no change).

**Spec:** `docs/specs/agentflow-cost-proposal.md`:

| Section | What this plan implements from it |
|---|---|
| §3.3 L3a | all of it: the five carry conditions, fresh otherwise, per-task reviewer and commit, ships off (Q7) |
| §4 | the row `phases.<p>.steps[].carry` |
| §5, §8 | review independence and per-task commits unchanged; the drift risk's mitigations (40% context, no `risk` task, no compaction, fresh reviewer) |
| §7 | the "C5 Carry" row |

## Decided in this plan

The spec leaves these choices open. Confirm them at review. Task 1 records each one in the spec as an "Amended for the C5 plan" note under L3a.

1. **Carry is only for tasks that nothing else shares.**
   - A task in a batch of two or more (`_batch_of`) never carries and is never carried into, and neither is a task of an active small plan. Their own sharing rules stay as they are and take precedence.
   - **Why:** a batch already has one session and one review over several tasks, and a carried batch would break the batch's review range.
2. **"Committed cleanly"** means all of these:
   - the previous task's implementer ran in this same walk of the plan (`_plan_pass`);
   - that task was committed in round 0;
   - no task was held (parked, or waiting on a parked task) between the two tasks;
   - no `hitl_raised` and no `step_parked` names the previous task's key or any key under it. That covers its implementer, its reviewer, its commit and its resolver.
   - A task committed in an earlier walk, or by an earlier runner process, isn't a carry source: the walk's `previous` only counts tasks attempted in this walk. That keeps a restart from resuming a session it never watched end.
   - **A refine round never carries.** Each task resumes its own session, as it does today.
3. **Context** is the `context_pct` of the session's last `agent_turn` event. Carry needs it strictly below `max_context_pct`. A session with no `agent_turn` event counts as 0.
4. **Rolled over** means a `rolled_over` event on the previous task's implement key. **Compacted** means any `compacted` event with the session's id.
5. **Effort.**
   - **The rule:** a carried task runs at the session's effort, as every shared session does (`takes_shared`). Carry is declined (`effort`) when the plan sizer put the next task above the session's effort.
   - **What "put" means:** the level of the latest `effort_decided` on its implement key, ranked with `sizing.highest`.
   - **Why:** without this, a task sized `high` would run at the `low` of a mechanical predecessor.
   - **The `risk` rule** reads the same plan-sizer answers (`sizing.flags(answers)["risk"]`). A task with no answers isn't `risk`.
6. **Counting tasks.** A session holds its first task plus one task per `session_carried` event that names its `session_id`. Carry needs that count below `max_tasks`. So `{max_tasks: 4}` gives up to four tasks per session, and `{max_tasks: 1}` gives today's one task per session.
7. **One decision per task key, recorded.**
   - The first decision for an implement key is an event:
     - `session_carried {key, from, session_id, tasks}`, where `tasks` is the session's count including this task;
     - `carry_declined {key, from, reason}`.
   - A later call for the same key returns the recorded answer, so a restarted runner makes the same choice.
   - `carry_declined` is written only when `max_tasks > 1`, so a run with no `carry` writes no new event.
   - The reasons, checked in this order: `no_session`, `not_clean`, `max_tasks`, `rolled_over`, `compacted`, `context`, `risk`, `effort`.
8. **The key's values.**
   - An absent `carry` is `{max_tasks: 1}`. A missing `max_context_pct` is 40, and a missing `max_tasks` is 1.
   - `validate` (the `batch` checks' pattern) checks that:
     - `carry` is a mapping that holds only `max_tasks` and `max_context_pct`;
     - `max_tasks` is a whole number of at least 1;
     - `max_context_pct` is a number from 10 to 70;
     - the step is a `foreach: plan_task` step.
   - The engine's `defaults/config.yaml` doesn't gain the key.
   - **The Settings page:** it doesn't edit a step (`phases.<name>` is protected), so `carry` is protected with the workflow, as `steps[].max_turns` is.
9. **The carried prompt** is the task prompt (`step.prompt`) with `continuation=True`, not `batch.prompt`.
   - **Why:** a batch prompt says "next task in this same session" about a batch, and a carried task has a review of its own.
   - The `task_briefs` the task names are still added. `_impl_step` computes them per task.

## Global Constraints

- Engine dependencies stay stdlib + `pyyaml>=6`.
- No project strings in the engine (`test_portability.py`'s `DENYLIST`).
- The golden files `agentflow/tests/fixtures/explain/*.txt` stay byte-identical. `explain` shows no new step, and `carry` isn't in its output.
- Every new config key ships with: a Settings form field or protection (`config_edit.py`), `validate` checks, a `tests/test_config_edit.py` case, a playbook §14 line.
- `CHANGELOG.md` gets one `Added` line under `## [Unreleased]`.
- **Review independence:** every reviewer starts in a fresh session; nothing in this slice passes `share` to a review step.
- **Commits:** only the runner commits, once per task, with guards over every step's tree.
- Default `carry: {max_tasks: 1}` (off); the default flips only in a later release, on a scorecard showing `gate_first_pass:*` and the review blocking-finding rate didn't get worse (Q7).
- `uvx ruff@0.17.4 check .` prints `All checks passed!` and `bin/agentflow validate` prints `0 warnings`.

## Review Focus

1. **A restart between the carry decision and the carried step's first call.** The runner dies after `session_carried` but before the next task's `agent_started`. A resumed run must carry from the same session (the recorded `from`) and must not write a second decision. Task 3 tests it.
2. **A previous task that stopped and was approved.** A `review_unresolved` stop approved by a human, or a resolver taking a stop: that task was committed, but not cleanly. The next task must start fresh (`not_clean`). Task 3 tests it.
3. **A parked task between two tasks.** Task 2 parks and task 3 runs. Task 3 must not carry task 1's session across the held task. Task 3 tests it.
4. **A refine round after a carried build.** Several committed tasks share one session id. In a phase-level refine round, each task's implementer resumes that session in plan order with its own note, writes no new carry event, and gets a fresh reviewer over its own refine diff. Task 3 tests that a refine round writes no `session_carried`.
5. **Defaults untouched.** A pack with no `carry`, the default batches and a small plan runs exactly as before: the same sessions and no `session_carried` or `carry_declined` events. Task 3 tests it.

---

### Task 1: The `carry` key, its checks and its docs

**Files:**
- Modify: `agentflow/config.py` (the step-key tuple near line 97, the `Step` dataclass near line 590, `Step` loading near line 1023, the validator near lines 1806 and 1868)
- Modify: `docs/agentflow-playbook.md` (§14, next to the `steps[].max_turns` line, ~545)
- Modify: `docs/specs/agentflow-cost-proposal.md` (L3a: an "Amended for the C5 plan" note recording Decided 1–9)
- Modify: `CHANGELOG.md`
- Test: `agentflow/tests/test_config.py` (or the file that holds the `batch` validate tests: `grep -ln "batch must be a mapping" agentflow/tests`)
- Test: `agentflow/tests/test_config_edit.py`

**Interfaces:**
- Produces: `Step.carry: dict | None = None`, the raw mapping as written (validated, not normalised). Also `config.carry_limits(step: Step) -> tuple[int, int]`, which returns `(max_tasks, max_context_pct)` with the defaults of Decided 8 (`(1, 40)` for `carry=None`).

- [ ] **Step 1: Write the failing tests**

```python
def test_carry_limits_default_off():
    assert carry_limits(step_with(carry=None)) == (1, 40)
    assert carry_limits(step_with(carry={"max_tasks": 4})) == (4, 40)
    assert carry_limits(step_with(carry={"max_tasks": 4, "max_context_pct": 30})) == (4, 30)

@pytest.mark.parametrize("carry, message", [
    ("yes", "carry must be a mapping"),
    ({"max_tasks": 0}, "max_tasks must be a whole number of at least 1"),
    ({"max_tasks": True}, "max_tasks must be a whole number of at least 1"),
    ({"max_context_pct": 9}, "max_context_pct must be a number from 10 to 70"),
    ({"max_context_pct": 71}, "max_context_pct must be a number from 10 to 70"),
    ({"max_task": 4}, "carry takes only max_tasks and max_context_pct"),
])
def test_validate_refuses_bad_carry(carry, message): ...   # error at phases.build.steps[<i>].carry[...]

def test_validate_refuses_carry_off_a_plan_task_step(): ...  # "only a `foreach: plan_task` step has `carry`"
def test_validate_takes_carry_on_the_build_step(): ...        # {max_tasks: 4, max_context_pct: 40}: no error, no warning
```

In `test_config_edit.py`, next to the step `max_turns` refusal (~line 1186):

```python
with pytest.raises(Refused):  # a step's carry is in the workflow, which the page does not edit
    form(shipped, (["phases", "build", "steps", "0", "carry"], {"max_tasks": 4}))
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_config.py agentflow/tests/test_config_edit.py --import-mode=prepend -q -k carry`
Expected: FAIL. `carry_limits` isn't defined, and `carry` is an unknown step key. The config_edit case may already pass, because `phases.<name>` is protected. Keep it as the pin.

- [ ] **Step 3: Implement**

- Add `"carry"` to the step-key tuple, `carry: dict | None = None` to `Step`, and `carry=raw.get("carry")` to loading.
- Add `carry_limits` beside the other `config.py` helpers.
- Add `Validator.carry(carry, path, step)`, modelled on `Validator.batch`, with the messages above, and call it where `batch` is called.

- [ ] **Step 4: Write the docs**

- **Playbook §14:** one line, `phases.<name>.steps[].carry`. It says:
  - on a `foreach: plan_task` step, the next task's implementer resumes the previous task's session while all five L3a conditions hold, plus Decided 5's effort rule;
  - batches and small plans keep their own sharing;
  - the reviewer stays fresh, and commits stay per task;
  - the defaults `{max_tasks: 1}` (off) and `max_context_pct: 40`, and the `validate` ranges;
  - `session_carried` / `carry_declined` and their reasons;
  - it is protected with the workflow.
- **CHANGELOG `Added`:** "`carry` on a `foreach: plan_task` step lets the next task's implementer resume the previous task's session while it is small and clean (off by default)."
- **The spec's amendment note** under L3a, after "Decided (Q7)".

- [ ] **Step 5: Run the tests, lint and validate**

Run the Step 2 command (expected: PASS), then `uvx ruff@0.17.4 check .` (`All checks passed!`) and `bin/agentflow validate` (`0 warnings`).

- [ ] **Step 6: Commit**

```bash
git add agentflow/config.py agentflow/tests/test_config.py agentflow/tests/test_config_edit.py docs/agentflow-playbook.md docs/specs/agentflow-cost-proposal.md CHANGELOG.md
git commit -m "Add the carry key to plan-task steps, off by default, with its checks and docs."
```

---

### Task 2: `carry.decide`, the pure carry rule

**Files:**
- Create: `agentflow/carry.py`
- Test: `agentflow/tests/test_carry.py`

**Interfaces:**
- Consumes: `sizing.parse_answers`, `sizing.flags`, `sizing.highest`.
- Produces:

```python
REASONS = ("no_session", "not_clean", "max_tasks", "rolled_over", "compacted", "context", "risk", "effort")

def recorded(events: list[dict], key: str) -> tuple[str | None, str] | None:
    """The first `session_carried` / `carry_declined` for implement key `key`: `(from_key, "carried")`, or
    `(None, reason)`; None when there is none."""

def decide(events: list[dict], steps: dict[str, dict], prev_tkey: str, key: str,
           max_tasks: int, max_context_pct: int) -> tuple[str | None, str]:
    """`(prev implement key, "carried")` or `(None, reason)` for implement key `key` carrying `prev_tkey`'s
    session, by Decided 2–6; the first failing check in `REASONS` order wins."""
```

`prev_tkey` is the task key (`<prefix>/task-<n>`), and the previous implement key is `f"{prev_tkey}/implement"`. `steps` is `run.state["steps"]`, where the session is `steps[prev_impl]["session_id"]` and its effort is `steps[prev_impl]["effort"]`. The caller has already checked "ran in this walk, committed, nothing held between" (Task 3), so `not_clean` here is only the `hitl_raised` / `step_parked` check. `decide` doesn't emit anything.

- [ ] **Step 1: Write the failing tests**

Build the events with a small helper, `ev(type, **fields)`, and check one reason per test against a base case that carries:

```python
BASE_STEPS = {"b/task-1/implement": {"session_id": "S", "effort": "medium"}}

def test_a_clean_small_young_session_carries():
    assert decide([turn("S", 12.0)], BASE_STEPS, "b/task-1", "b/task-2/implement", 4, 40) == ("b/task-1/implement", "carried")

def test_no_session_declines(): ...            # steps row without session_id -> (None, "no_session")
def test_a_stop_on_the_previous_task_declines(): ...  # hitl_raised key "b/task-1/review" -> "not_clean"; step_parked "b/task-1/implement" -> "not_clean"
def test_a_stop_on_another_task_does_not_count(): ... # hitl_raised key "b/task-10/implement" -> "carried" (prefix match is on "b/task-1/" and "b/task-1")
def test_max_tasks_counts_the_session_carries(): ...  # two session_carried with session_id "S", max_tasks 3 -> "max_tasks"; max_tasks 4 -> "carried"
def test_max_tasks_one_never_carries(): ...           # max_tasks 1 -> "max_tasks"
def test_a_rollover_declines(): ...                   # rolled_over key "b/task-1/implement" -> "rolled_over"
def test_a_compaction_declines(): ...                 # compacted session_id "S" -> "compacted"; compacted session_id "T" -> "carried"
def test_context_at_the_limit_declines(): ...         # last agent_turn of S at 40.0 with limit 40 -> "context"; 39.9 -> "carried"; only the LAST turn of S counts
def test_no_turn_counts_as_empty(): ...               # no agent_turn -> "carried"
def test_a_risk_task_declines(): ...                  # effort_decided key "b/task-2/implement", answers risk "yes" (others "no") -> "risk"; "unsure" also -> "risk"
def test_a_task_sized_above_the_session_declines(): ... # effort_decided level "high" for task-2, session effort "medium" -> "effort"; "low" -> "carried"
def test_reasons_are_checked_in_order(): ...          # a compaction and a risk answer together -> "compacted"
def test_recorded_returns_the_first_decision(): ...   # session_carried then carry_declined for the same key -> ("b/task-1/implement", "carried"); none -> None
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_carry.py --import-mode=prepend -q`
Expected: FAIL with `ModuleNotFoundError: agentflow.carry`.

- [ ] **Step 3: Implement `agentflow/carry.py`**

- Start with a module docstring that states the rule (Decided 2–7).
- Read the sizer answers and level from the latest `effort_decided` on `key`, as `FeatureLane._sized_answers` does.
- Keep it pure: no `Run`, no file I/O, no clock.

- [ ] **Step 4: Run the tests and lint**

Run the Step 2 command (expected: PASS), then `uvx ruff@0.17.4 check .` (`All checks passed!`), and the portability gate: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_portability.py --import-mode=prepend -q` (PASS).

- [ ] **Step 5: Commit**

```bash
git add agentflow/carry.py agentflow/tests/test_carry.py
git commit -m "Add the pure rule that decides when a plan task carries the previous task's session."
```

---

### Task 3: Carry the implementer in the plan walk

**Files:**
- Modify: `agentflow/feature_lane.py` (`_plan_pass` ~263–307, `_run_task` ~426–441, a new `_carry` method, the module docstring's task list ~10–11)
- Test: `agentflow/tests/test_feature_lane.py` (a new section, `# ---- carrying the implementer`, after the batch tests ~1252)

**Interfaces:**
- Consumes: `config.carry_limits(step) -> (int, int)` (Task 1); `carry.decide(...)`, `carry.recorded(...)` (Task 2); `run_agent_step(..., share_session_of=...)`, whose `takes_shared` path already exists.
- Produces: `FeatureLane._carry(step: Step, tkey: str, carry_prev: str | None) -> str | None`, where `carry_prev` is the task key this walk may carry from, or None. It returns the implement key to share, or None, and records the decision the first time (Decided 7). The events `session_carried` and `carry_declined` are new types that `state.py` doesn't reduce. `signals` and `metrics` ignore unknown types; check with `grep -n "def reduce\|_HANDLERS" agentflow/state.py` that an unhandled type is skipped and doesn't raise.

- [ ] **Step 1: Write the failing flow tests**

Configure with `flow_repo.configure(BIG, edit=...)`. The edit sets the build step's `carry`, and `batch=False` (the default) keeps batching out.

```python
def test_carry_resumes_one_session_until_the_context_limit(flow_repo, git):
    # carry {max_tasks: 4}; task 3's act reports usage_pct [45] (over the default 40)
    run = begin_build(carrying, plan_text(count=4))
    assert build(carrying, run) == ("done", True)
    s1 = session_of(carrying, run, IMPL.format(n=1))
    assert {session_of(carrying, run, IMPL.format(n=n)) for n in (2, 3)} == {s1}
    assert session_of(carrying, run, IMPL.format(n=4)) != s1
    calls = carrying.calls()
    impl2 = next(c for c in calls if c["step"] == IMPL.format(n=2))
    assert ("--resume", s1) in pairs(impl2["argv"])
    reviews = [c for c in calls if c["step"].endswith("/review")]
    assert len(reviews) == 4 and not any("--resume" in c["argv"] for c in reviews)   # every reviewer fresh
    assert subjects(git, carrying.root)[:4] == [f"Task {n}: {TITLES[n]}" for n in (4, 3, 2, 1)]  # one commit per task
    assert [(e["key"], e["tasks"]) for e in carrying.events(run, "session_carried")] == [(IMPL.format(n=2), 2), (IMPL.format(n=3), 3)]
    assert [(e["key"], e["reason"]) for e in carrying.events(run, "carry_declined")] == [(IMPL.format(n=4), "context")]

def test_carry_goes_fresh_at_max_tasks(...): ...        # {max_tasks: 2}, 4 tasks: sessions {1,2} and {3,4}; task 3 declined "max_tasks"
def test_a_carried_prompt_is_a_continuation(...): ...   # build_prompt spy as in test_only_a_batch_continuation_is_marked_as_one: [False, True, True] for 3 tasks
def test_no_carry_key_changes_nothing(...): ...         # plain `lane`, 3 tasks: three distinct sessions, no session_carried, no carry_declined
def test_a_stopped_task_is_not_carried(...): ...        # task 1 review blocks twice -> review_unresolved; approve; task 2 starts fresh, declined "not_clean"
def test_carry_never_crosses_a_parked_task(...): ...    # hitl.park_independent, task 2 parks (needs_human can_continue): task 3 gets a fresh session and no carry event naming task 1
def test_a_risk_task_starts_fresh(...): ...             # emit effort_decided {key: IMPL n=2, level: "medium", answers: risk "yes"} before build: task 2 declined "risk"
def test_a_compacted_session_is_not_carried(...): ...   # task 1 act {"compact": True}: task 2 declined "compacted"
def test_carry_decision_survives_a_restart(...): ...    # 2-task plan; monkeypatch `run_agent_step` to return "stopped" for IMPL-2 after `_carry` recorded it
                                                        # (the runner died before task 2's first call);
                                                        # then executor._carry(step, IMPL-2's tkey, None) (a new walk after a restart):
                                                        # returns IMPL-1's key from the record, and still exactly one session_carried for task 2
                                                        # (if task 2's session already started, `_carry` returns None and the step resumes its own)
def test_a_refine_round_writes_no_carry_event(...): ... # carried build, then reject build phase-level: the refine round's
                                                        # implementers resume their own sessions; session_carried count unchanged
def test_batched_tasks_do_not_carry(batched, ...): ...  # configure(BIG, batch=True) plus carry {max_tasks: 4}: plan_batched [[1,2,3]], no session_carried
```

Use the helpers already in the file: `begin_build`, `build`, `session_of`, `pairs`, `steps`, `review`, `needs_human` and `FINDING`. For the stop and refine tests, copy the stop and approve sequence from the existing `review_unresolved` and refine tests (`grep -n "review_unresolved\|reject" agentflow/tests/test_feature_lane.py agentflow/tests/test_refine.py`).

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_feature_lane.py --import-mode=prepend -q -k "carr"`
Expected: FAIL. Every task gets its own session, and no `session_carried` event is written. `test_no_carry_key_changes_nothing` and `test_batched_tasks_do_not_carry` already pass. Keep them as pins.

- [ ] **Step 3: Implement**

- **`_plan_pass`:** keep a `carry_prev: str | None` (the task key of the last task this walk ran and committed). Set it after a `_run_task` that returns `"done"`. Clear it whenever a task is held, parked, or passed over as committed earlier. When a task doesn't share by `shared_session` or batch, isn't in a batch, and the walk isn't a refine round, set `share = self._carry(step, tkey, carry_prev)`.
- **`_carry`:** these checks run first and return None, writing no event:
  - the step's implement row has already started (its `session_id` is set);
  - `carry_limits(step)[0] == 1`.

  Otherwise `carry.recorded` wins when it is set. Next, `carry_prev is None` is a recorded `not_clean`. Else `carry.decide(...)`, then emit `session_carried` (with `session_id` and `tasks`) or `carry_declined`.
- **`_run_task`:** take a `carried: bool` keyword. When it is true and the shared session exists (`continues`), build `impl_step` with `continuation=True`. That is the same branch as `shared_session`, and it doesn't use `batch.prompt` (Decided 9).
- **Docstring:** update the module docstring's item 1 to name `carry`.

- [ ] **Step 4: Run the tests, the neighbouring suites and the golden files**

Run:
`uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_feature_lane.py agentflow/tests/test_refine.py agentflow/tests/test_sizing_lane.py agentflow/tests/test_route_lane.py agentflow/tests/test_explain.py agentflow/tests/test_state.py --import-mode=prepend -q`
Expected: PASS. If the explain test file has another name, find it with `grep -ln "fixtures/explain" agentflow/tests`. Then run `git diff --exit-code agentflow/tests/fixtures/explain/`, which should print nothing, and `uvx ruff@0.17.4 check .` (`All checks passed!`).

- [ ] **Step 5: Commit**

```bash
git add agentflow/feature_lane.py agentflow/tests/test_feature_lane.py
git commit -m "Let the next plan task resume the previous task's implementer session when carry allows it."
```

---

## Verification

- The full suite: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests --import-mode=prepend -q` (25–60 min) passes.
- `uvx ruff@0.17.4 check .` prints `All checks passed!`, and `bin/agentflow validate` prints `0 warnings`.
- `git diff --exit-code agentflow/tests/fixtures/explain/` is empty.
- **The PR description says:**
  - `carry` ships off;
  - the default flips only after a real run with `carry: {max_tasks: 4, max_context_pct: 40}` shows, in `agentflow scorecard` against the C0 baseline, that `gate_first_pass:*` and the review blocking-finding rate didn't get worse (Q7);
  - that run's `session_carried` / `carry_declined` counts.
- **Security overlay:** this slice changes no file in `overlays.security.when_paths`, because `feature_lane.py`, `config.py` and the new `carry.py` aren't listed. So it doesn't trigger the overlay.
