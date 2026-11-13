# Effort sized per task (harness slice ES) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The runner decides each agent step's effort level itself, from the current task, before the work session starts, and raises it once when the task's own outcome shows it was too low. This replaces the in-session effort check that never fired.

**Architecture:**
- `sizing.py` is pure: the checklist, the rule that maps answers to a level, the clamp to `effort.allowed`, the judgement floor, escalation, the task card, the plan digest and parsing the sizer's answer. It reads no file and no clock.
- A sizer cannot write. `AgentCall.read_only` gives Claude only `Read,Grep,Glob` and no MCP servers, and gives Codex the `read-only` sandbox. The sizer answers in its final message, not in a file. As a backstop, the runner records the repository's whole git state in a `sizer_started` event, compares it after the call, and refuses any restart until that state is back.
- `sizing_lane.py` holds `SizingLane`, a mixin of `Executor` like `FeatureLane`. It sizes one step, or sizes a plan's tasks in chunks, records `effort_decided`, and escalates.
  - `run_agent_step` sizes a step before its first turn is built.
  - `run_foreach` sizes the plan once its batches are known.
  - The fix loop and a reopening review call `escalate_effort`.

**Tech Stack:** Python 3.12+, stdlib + `pyyaml>=6`, pytest via `uv run --no-project`.

**Spec:** `docs/agentflow-harness-proposal.md` §6b (items 3 to 8 and "Decisions (grilling, 2026-11-12)"). Items 1 and 2 (the conductor's shape, no history priors) need no code in this slice.

## Global Constraints

- Engine dependencies stay stdlib + `pyyaml>=6`. Add no third-party import.
- No project strings in engine files, the two new prompts included (`test_portability.py` DENYLIST).
- `agentflow/tests/fixtures/explain/*.txt` stay byte-identical. `explain` and `--dry-run` never size: a dry run prints the work call at `effort.default`, floored.
- Every new or changed config key ships with a Settings field, `validate` checks, a `test_config_edit.py` case and a playbook §14 line.
- `bin/agentflow validate` prints `0 warnings` for `.agentflow/config.yaml` and for both fixture packs.
- No task names more than 8 files (`sources.MAX_TASK_FILES`).
- `agentflow.sizing_lane` imports from `agentflow.lanes` only under `TYPE_CHECKING` or inside a function, because `lanes` imports it for `Executor`'s bases.
- Checklist question ids are exactly `interface`, `risk`, `ambiguity`, `breadth`, `novelty`, `mechanical`. Answers are exactly `yes`, `no`, `unsure`.
- `unsure` always counts toward the cautious side: as `yes` for the five risk questions, and as `no` for `mechanical`.
- Sizing picks only `low`, `medium` or `high`. Only escalation reaches a level above `high`.
- Judgement steps are those of phase `define`, `plan` or `review`, or of role `plan`, `review` or `debug`.
  - On every path they run at `medium` or above: sized, fallback, `effort.sizing: false`, resumed, and `agentflow attach`.
  - They are never sized by the `mechanical` model.
- Sizer limits are engine constants, with no pack key:
  - step sizer: `SIZE_MAX_TURNS = 8`, `SIZE_TIMEOUT_S = 180`
  - plan sizer: `PLAN_SIZE_MAX_TURNS = 16`, `PLAN_SIZE_TIMEOUT_S = 420`, at most `PLAN_SIZE_CHUNK = 6` tasks per call
- A sizing failure never stops a run. An exception anywhere in sizing, a bad answer, a timeout or a usage limit all fall back. The one stop is a sizer that changed the repository.
- A step escalates at most once:
  - on the first fix attempt after a failed gate in `_loop`;
  - or when a review reopens it: a `step_reopened` with reason `review`, or a refine round that resumes its session.

  A rollover never escalates. Nothing escalates when `effort.sizing` is `false`.
- Internal sessions are not sized: condition questions (`flow_control._ask_agent`), resolver sessions, `record_decisions` steps and the retro all pass `size=False`. They still get the judgement floor.
- New events. Every agent event of a sizer call carries `sizer: true`. Readers use `.get`, so older logs still load.
  - `sizer_started{key, tree, head, state}`, where `state` is a sha256 of `git_state` and `tree_state`
  - `effort_decided{key, level, source, answers, reason, sizer_model, files, security}`, `source` one of `plan_sizer`, `judge`, `sizer`, `fallback`
  - `effort_escalated{key, from, to, trigger}`, `trigger` one of `gate`, `review`
  - `plan_sized{key, digest, tasks}`
- New signal families, all `lower` counts with a phase parameter: `effort_escalations`, `undersized` and `sizer_fallbacks`. A `fallback` decision never counts as undersized.
- Agent text is bounded: `reason` at 300 characters, and `answers` normalized to the six ids and three values. Neither reaches a commit message or a Conformance section.
- `.agentflow/**` is forbidden in a run. The comment edits to this repository's `.agentflow/config.yaml` in "After the tasks" are made by a human.
- CLAUDE.md is not edited in this slice: the effort policy lives in the engine, not in repository instructions.
- Interactive sessions don't commit. Each task's commit step is for runs, or for when the human asks. The commit message is one imperative sentence and ends with the pack's `Co-Authored-By` trailer.

## Deviations from §6b (recorded in its "As built" note, Task 9)

1. `effort_decided` has no `cost_usd`. A sizer's spend is metered through its own agent events and reported as the phase's `sizing_cost_usd`.
2. With `effort.sizing: false`, no `effort_decided` is written, although §6b lists a `source: off`. The step's level is in `step_started`.
3. The checks of `claude` flags and of resume with a new level are a human pre-flight, not "the plan's first task": an agent in a run cannot call `claude`. Codex is checked at the argv level only.
4. The scorecard gets counts, not a ratio: `undersized`, `effort_escalations` and `sizer_fallbacks`. The ratio is `undersized` over the phase's `effort_decided` events.
5. A step that is not a plan task has no named files. Its `files` fact is the number of paths changed since the run started, not a diff stat.
6. A refine round that resumes a step's session counts as "a review that reopens the step" (trigger `review`).
7. A gate step that starts inside its fix loop (`fix_first`) is not escalated at that start, because its sizer already sees the failing gates. It escalates on its next fix attempt.
8. Every CI and sync fix round is a new step key, so each round is sized on its own.
9. `mechanical: unsure` counts as `no`, where §6b says `unsure` counts as `yes`. Counting it as `yes` would make an unsure answer produce `low`.
10. A sizer that changed the repository stops the run instead of being "discarded", because its change would otherwise be committed with the next step.
11. Edits to the contents of an existing ignored file are not detected: `tree_state` lists ignored paths without hashing them. The tool allowlist is what prevents such edits.
12. The plan sizer has its own limits (16 turns, 7 minutes, 6 tasks per call) next to the step sizer's 8 turns and 3 minutes. One call over a whole plan would time out.
13. The plan sizer's model is the build phase's `plan`-role model, resolved without the foreach step's own `model:`. A `--model build=…` override on the command line still applies.

## Review Focus

1. **A sizer that changes the tree, `.git` (a hook, the config), the set of ignored files, or moves `HEAD`.**
   - Its decision is not applied, and the run stops with a `guard_violation`.
   - A restart (of the step, or of build after a plan-sizer stop) is refused until the tree, `HEAD` and the git state match the sizer's `sizer_started`.
   - A step retried after that is sized again.
   - Tests: Task 5 `test_a_sizer_that_changes_the_repository_stops_the_run`, `test_a_restart_after_a_sizer_edit_needs_the_state_back`; Task 7 `test_a_plan_sizer_edit_blocks_build_until_reset`.
2. **A sizing failure anywhere.** A `GitError` before the call, bad or missing JSON, a timeout, an exit code, a usage limit, or an exception from the call all give `source: fallback`, with no hold, no stop and no reminder. A plan-sizer fallback is tried again on the next entry to build. Tests: Task 5 `test_a_failed_sizer_falls_back_without_a_stop` (parametrized); Task 7 `test_a_failed_plan_sizer_is_retried_on_the_next_entry`.
3. **A pause, cancel, runner death or spent budget around a sizer.** A paused or cancelled run starts no work. A sizer that outlived its runner is waited for and compared with its `sizer_started`. A run that is over budget is not sized. Tests: Task 5 `test_a_run_paused_while_sizing_starts_no_work`, `test_an_orphan_sizer_is_checked_on_resume`, `test_an_over_budget_run_is_not_sized`.
4. **A plan that changes after it was sized.** Ticking a box with any bullet style (`-`, `*`, `1.`, indented) changes nothing. A real edit sizes again only the tasks that have not started. Tests: Task 2 `test_ticking_a_box_keeps_the_plan_digest`; Task 7 `test_a_changed_plan_resizes_only_unstarted_tasks`.
5. **A pack that narrows `effort.allowed` or turns sizing off.**
   - `[medium, high]` turns `low` into `medium`, and `[high]` runs everything at `high`.
   - A step at the top level does not escalate.
   - With sizing off and `default: low`, a judgement step still runs at `medium`, in a run and in `attach`.
   - Tests: Task 2 `test_clamp_*` and `test_floor_*`; Task 6 `test_the_floor_holds_with_sizing_off`, `test_attach_applies_the_floor`; Task 8 `test_no_escalation_at_the_top_level`.

## Pre-flight (a human, before Task 4)

- [ ] **The pre-flight passed.** Task 4 must not start until a human has ticked this box. Run:

```bash
# 1. Haiku takes an effort level (the step sizer's model and level for steps that are not judgement steps).
claude -p "Reply with one word." --model haiku --effort low --output-format stream-json --verbose | tail -1
# 2. --tools is an allowlist that holds under bypassPermissions, and --strict-mcp-config drops MCP servers.
claude -p "Use the Write tool to create /tmp/af-sizer-probe, then run: touch /tmp/af-sizer-probe2" \
  --permission-mode bypassPermissions --tools "Read,Grep,Glob" --strict-mcp-config \
  --output-format stream-json --verbose | head -1     # the init event lists only Read, Grep and Glob
test ! -e /tmp/af-sizer-probe && test ! -e /tmp/af-sizer-probe2 && echo "read-only holds"
# 3. A resumed session takes a new level (Task 8). Note the session_id from the first call.
claude -p "Reply with one word." --effort low --output-format stream-json --verbose | head -1
claude -p "Reply with one word." --resume <session_id> --effort high --output-format stream-json --verbose | head -1
```

Expected:
- 1 succeeds.
- 2 prints `read-only holds`, and its init event lists `Read`, `Grep`, `Glob` and no MCP tools.
- 3 succeeds, and the resumed call's `system/init` event reports the new level wherever it reports effort.

If 1 or 2 fails, amend Task 4 before starting it. If 3 cannot be confirmed, amend Task 8 so that the escalated attempt starts a fresh session through the rollover path (`_rollover`'s lead).

---

### Task 1: The `effort.sizing` key and the floor in `validate`

**Files:**
- Modify: `agentflow/config.py` (`EffortPolicy`, `load_config`, `_Validator.effort`)
- Modify: `agentflow/defaults/config.yaml` (`effort:` and the `effort_arg` comment)
- Modify: `agentflow/config_edit.py` (the "Effort" section)
- Modify: `agentflow/tests/fixtures/packs/python/.agentflow/config.yaml`, `agentflow/tests/fixtures/packs/node/.agentflow/config.yaml`
- Modify: `docs/agentflow-playbook.md` (§14 `effort` line)
- Test: `agentflow/tests/test_config.py`, `agentflow/tests/test_config_edit.py`

**Interfaces:**
- Produces: `EffortPolicy.sizing: bool` (default `True`).

- [ ] **Step 1: Write the failing tests**

In `test_config.py`:
```python
def test_effort_sizing_defaults_on(sparse_repo):
    assert load_config(pack.pack_config(sparse_repo)).effort.sizing is True

def test_effort_sizing_must_be_a_bool(tmp_cfg):
    with pytest.raises(ConfigError, match="effort.sizing"):
        load_config(tmp_cfg({"effort": {"sizing": "yes"}}))

def test_effort_allowed_needs_a_level_at_or_above_medium(tmp_cfg, capsys):
    bad = tmp_cfg({"effort": {"allowed": ["low"], "default": "low"}})
    assert main(["validate", "--config", str(bad)]) == 1
    assert "error effort.allowed:" in capsys.readouterr().out
```
In `test_config_edit.py`, add `(lambda d: d["effort"].update(allowed=["low"], default="low"), "effort.allowed")` to the validate-refusal parametrize list, `(["effort", "sizing"], "yes", EditError)` to the field-kind list, and:
```python
def test_effort_sizing_round_trips(sparse_repo):
    path = _sparse(sparse_repo)
    assert _fields(path)[("effort", "sizing")]["source"] == "engine"
    proposal = _sparse_form(path, (["effort", "sizing"], False))
    assert not proposal.errors, proposal.errors
    config_edit.save(path, proposal)
    assert load_config(path).effort.sizing is False
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_config.py agentflow/tests/test_config_edit.py --import-mode=prepend -q -k "sizing or above_medium"`
Expected: FAIL (`EffortPolicy` has no `sizing`; no Form field).

- [ ] **Step 3: Implement**

- `EffortPolicy.sizing: bool = True`. `load_config` reads it with `_optional(effort, "sizing", "effort", bool, True)`.
- `_Validator.effort` adds an error on `effort.allowed` when no allowed level has `KNOWN_EFFORT_LEVELS.index(level) >= KNOWN_EFFORT_LEVELS.index("medium")`: `"needs a level at or above medium: define, plan, review and debug steps never run at low"`.
- `defaults/config.yaml`:
  - Under `effort:`, add `sizing: true` with the comment `# the runner sizes each step before it starts (false: every step runs at default)`.
  - The `effort_arg` comment becomes `# the runner sizes each step's level (see effort:)`.
- `config_edit.py`, "Effort" section:
  - Help text: `"How the runner sizes each step's reasoning effort."`
  - Add `(("effort", "sizing"), "Size each step before it starts", {"kind": "bool"})` first.
  - Relabel `default` as `"Level when sizing is off or fails"` and `allowed` as `"Levels a step may run at"`.
- Both fixture packs: `sizing: false` under `effort:`, commented `# the suite's scripted agents; sizing has its own tests`.
- Playbook §14: the `effort` line becomes: `` `effort`: `sizing` (default `true`: the runner sizes each step before it starts; harness proposal §6b), `default` (the level when sizing is off or fails), and `allowed` (the levels a step may run at; it must hold one at or above `medium`, because define, plan, review and debug steps never run at `low`). Not protected. ``

- [ ] **Step 4: Run the tests and `validate`**

Run: the Step 2 command, then the whole of both files, then `bin/agentflow validate`.
Expected: PASS, including the existing check that `defaults/config.yaml` states every `EffortPolicy` field; `0 warnings`.

- [ ] **Step 5: Commit**

`git commit -m "Add effort.sizing and refuse an effort.allowed list with nothing at or above medium."`

---

### Task 2: The pure sizing rule (`sizing.py`)

**Files:**
- Create: `agentflow/sizing.py`
- Test: `agentflow/tests/test_sizing.py`

**Interfaces:**
- Consumes: `config.KNOWN_EFFORT_LEVELS`, `config.EffortPolicy`, the box pattern of `sources` (expose it as `sources.BOX` if it is private).
- Produces:
  - **Constants:**
    - `QUESTIONS`, `RISK_QUESTIONS = ("interface", "risk", "ambiguity", "breadth", "novelty")`, `OTHER_RISKS = ("interface", "ambiguity", "breadth", "novelty")`
    - `JUDGEMENT_PHASES = frozenset({"define", "plan", "review"})`, `JUDGEMENT_ROLES = frozenset({"plan", "review", "debug"})`
    - `LOW_MAX_FILES = 2`, `SIZE_MARK = "/size"`, `REASON_MAX = 300`, `ASK_MAX = 6000`
    - `SIZE_MAX_TURNS = 8`, `SIZE_TIMEOUT_S = 180`, `PLAN_SIZE_MAX_TURNS = 16`, `PLAN_SIZE_TIMEOUT_S = 420`, `PLAN_SIZE_CHUNK = 6`
  - `is_judgement(phase: str, role: str) -> bool`
  - `parse_answers(raw: object) -> dict[str, str] | None`: the six ids mapped to lower-case `yes`/`no`/`unsure`. Extra keys are dropped. `None` unless every id has a valid answer.
  - `flags(answers: dict[str, str]) -> dict[str, bool]`: `unsure` is `True` for `RISK_QUESTIONS` and `False` for `mechanical`.
  - `level_for(flags: dict[str, bool], files: int, security: bool, judgement: bool) -> str`
  - `clamp(level: str, allowed: Sequence[str]) -> str`
  - `floor(level: str, judgement: bool, allowed: Sequence[str]) -> str`
  - `decide(answers: dict[str, str] | None, files: int, security: bool, judgement: bool, policy: EffortPolicy) -> tuple[str, bool]`: `(level, fell_back)`, always floored.
  - `sizer_effort(judgement: bool, allowed: Sequence[str]) -> str`
  - `escalate(level: str, allowed: Sequence[str]) -> str | None`
  - `highest(levels: Iterable[str]) -> str`
  - `parse_result(text: str) -> dict | None`
  - `plan_digest(tasks: Iterable[tuple[int, str, str]]) -> str`
  - `task_card(*, key: str, phase: str, role: str, ask: str, files: list[tuple[str, int | None]], changed: list[str], security: bool, failing_gates: list[str]) -> str`
  - `reason_text(raw: object) -> str`

- [ ] **Step 1: Write the failing tests**

```python
NO = dict.fromkeys(sizing.QUESTIONS, "no")
ALLOWED = ["low", "medium", "high"]

def f(**answers):
    return sizing.flags(NO | answers)

def test_answers_are_normalized_and_unsure_is_cautious():
    assert sizing.parse_answers(NO | {"risk": "Unsure", "extra": "x"}) == NO | {"risk": "unsure"}
    assert sizing.parse_answers({"risk": "no"}) is None
    assert sizing.parse_answers(dict.fromkeys(sizing.QUESTIONS, "maybe")) is None
    assert f(risk="unsure")["risk"] is True and f(mechanical="unsure")["mechanical"] is False

@pytest.mark.parametrize("flags, files, security, judgement, level", [
    (f(mechanical="yes"), 2, False, False, "low"),
    (f(mechanical="yes"), 3, False, False, "medium"),       # more than LOW_MAX_FILES
    (f(mechanical="unsure"), 1, False, False, "medium"),    # unsure is not mechanical
    (f(mechanical="yes"), 1, True, False, "high"),          # the security overlay
    (f(mechanical="yes"), 1, False, True, "medium"),        # judgement: never low
    (f(), 5, False, False, "medium"),
    (f(risk="yes"), 1, False, False, "high"),
    (f(interface="yes"), 1, False, False, "medium"),
    (f(interface="yes", novelty="yes"), 1, False, False, "high"),
    (f(ambiguity="yes"), 1, False, True, "high"),           # judgement: any risk answer is high
])
def test_level_for(flags, files, security, judgement, level):
    assert sizing.level_for(flags, files, security, judgement) == level

def test_clamp_goes_up_to_the_nearest_allowed_level_else_the_highest():
    assert sizing.clamp("low", ["medium", "high"]) == "medium"
    assert sizing.clamp("medium", ["high"]) == "high"
    assert sizing.clamp("high", ["low", "medium"]) == "medium"

def test_floor_raises_only_judgement_steps():
    assert sizing.floor("low", True, ALLOWED) == "medium" and sizing.floor("low", False, ALLOWED) == "low"
    assert sizing.floor("high", True, ALLOWED) == "high"

def test_decide_falls_back_to_default_or_high_for_judgement():
    policy = EffortPolicy(default="low", allowed=ALLOWED, sizing=True)
    assert sizing.decide(None, 0, False, False, policy) == ("low", True)
    assert sizing.decide(None, 0, False, True, policy) == ("high", True)
    assert sizing.decide(NO | {"mechanical": "yes"}, 0, False, False, policy) == ("low", False)

def test_judgement_steps_by_phase_or_role():
    assert sizing.is_judgement("define", "build") and sizing.is_judgement("build", "review")
    assert sizing.is_judgement("bug", "debug") and not sizing.is_judgement("verify", "build")

def test_sizer_effort_and_escalation():
    assert sizing.sizer_effort(False, ALLOWED) == "low" and sizing.sizer_effort(True, ALLOWED) == "medium"
    assert sizing.escalate("medium", ALLOWED) == "high" and sizing.escalate("high", ALLOWED) is None
    assert sizing.escalate("low", ["low", "high", "max"]) == "high"
    assert sizing.highest(["low", "high", "medium"]) == "high"

def test_parse_result_reads_the_whole_message_or_its_last_json_block():
    assert sizing.parse_result('{"answers": {}}') == {"answers": {}}
    assert sizing.parse_result('Done.\n```json\n{"a": 1}\n```\n```json\n{"a": 2}\n```') == {"a": 2}
    assert sizing.parse_result("no json here") is None and sizing.parse_result("[1, 2]") is None

@pytest.mark.parametrize("bullet", ["- ", "* ", "+ ", "1. ", "  - "])
def test_ticking_a_box_keeps_the_plan_digest(bullet):
    open_ = [(1, "A", f"{bullet}[ ] write it"), (2, "B", f"{bullet}[ ] test it")]
    ticked = [(1, "A", f"{bullet}[x] write it"), (2, "B", f"{bullet}[X] test it")]
    edited = [(1, "A", f"{bullet}[ ] write it"), (2, "B", f"{bullet}[ ] test it twice")]
    assert sizing.plan_digest(open_) == sizing.plan_digest(ticked) != sizing.plan_digest(edited)

def test_task_card_names_the_facts_and_caps_the_ask():
    card = sizing.task_card(key="build/build/task-2/implement", phase="build", role="build", ask="x" * 9000,
                            files=[("src/a.py", 120), ("src/new.py", None)], changed=["src/a.py"],
                            security=True, failing_gates=["unit"])
    assert "src/a.py (120 lines)" in card and "src/new.py (new or unreadable)" in card
    assert "security overlay: applies" in card and "failing gates: unit" in card
    assert card.count("x") <= sizing.ASK_MAX
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_sizing.py --import-mode=prepend -q`
Expected: FAIL with `ModuleNotFoundError: agentflow.sizing`.

- [ ] **Step 3: Implement `agentflow/sizing.py`**

- `level_for`, in this order:
  1. A judgement step is `"high"` if `security` or any of `RISK_QUESTIONS` holds, else `"medium"`.
  2. `"high"` if `risk`, `security`, or two or more of `OTHER_RISKS` hold.
  3. `"low"` if `mechanical` holds, none of `RISK_QUESTIONS` does, and `files <= LOW_MAX_FILES`.
  4. Otherwise `"medium"`.
- `floor`: for a judgement step, the higher of `level` and `clamp("medium", allowed)`; for any other step, `level` unchanged.
- `decide` applies `clamp` and then `floor`. With `answers is None` it returns `(floor(clamp("high")), True)` for a judgement step, else `(floor(policy.default), True)`.
- `escalate` returns the first allowed level above `level`, in `KNOWN_EFFORT_LEVELS` order, or `None` when there is none.
- `parse_result` tries the whole text, then the last fenced ```` ```json ```` block, and returns `None` unless it finds a JSON object.
- `plan_digest` is a sha256 over each `(n, title, text)`, with every box line matched by the `sources` box pattern read as unchecked.
- `task_card` writes one fact per line:
  - `step:`
  - `files the task names:`
  - `paths changed since the run started:` (the first 20, then a count)
  - `security overlay: applies|does not apply`
  - `failing gates:`

  The ask follows under `## What the step asks`, truncated to `ASK_MAX`.
- `reason_text` returns a string capped at `REASON_MAX`, else `""`.
- Module docstring: "Pure: reads no file and no clock".

- [ ] **Step 4: Run the tests**

Run: the Step 2 command. Expected: PASS.

- [ ] **Step 5: Commit**

`git commit -m "Add the pure sizing rule: checklist, level, clamp, floor, escalation and the plan digest."`

---

### Task 3: Sizing events in state, metrics and signals

**Files:**
- Modify: `agentflow/state.py` (`_step_row`, the agent-event handlers, new handlers, `REQUIRED`)
- Modify: `agentflow/metrics.py` (sizer calls), `agentflow/signals.py` (`FAMILIES`, `COUNT_FAMILIES`, `run_signals`, `_turns`, `cost_per_task`, docstring)
- Test: `agentflow/tests/test_state.py`, `agentflow/tests/test_metrics.py`, `agentflow/tests/test_signals.py`

**Interfaces:**
- Produces:
  - Step row field `effort_source`. Agent rows carry `sizer: bool`.
  - No agent event with `sizer: true` creates or changes a step row.
  - A phase's metrics report sizer cost as `sizing_cost_usd`, inside the phase's `cost_usd` and outside its steps.

- [ ] **Step 1: Write the failing tests**

```python
def test_effort_decided_sets_the_level_and_marks_the_step_sized(run):
    run.emit("effort_decided", key="build/x", level="low", source="sizer", answers={}, reason="rename")
    row = run.state["steps"]["build/x"]
    assert (row["effort"], row["sized"], row["effort_source"]) == ("low", True, "sizer")

def test_effort_escalated_records_an_upward_change(run):
    run.emit("effort_decided", key="build/x", level="medium", source="sizer")
    run.emit("effort_escalated", key="build/x", trigger="gate", **{"from": "medium", "to": "high"})
    row = run.state["steps"]["build/x"]
    assert row["effort"] == "high" and row["effort_changed"] == "up"
    assert run.state["effort_changes"][-1] == {"key": "build/x", "from": "medium", "to": "high", "reason": "gate"}

def test_a_sizer_call_makes_no_step_row(run):
    run.emit("agent_started", agent="claude", pid=5, model="haiku", effort="low", step="build/x/size",
             sizing=True, sizer=True)
    run.emit("agent_session", session_id="s9", model="claude-haiku-5-5", key="build/x/size", pid=5, sizer=True)
    run.emit("agent_turn", session_id="s9", key="build/x/size", sizer=True, cost=0.01)
    run.emit("agent_exited", session_id="s9", key="build/x/size", sizer=True, cost=0.01)
    assert not any(k.endswith("/size") for k in run.state["steps"])
    assert run.state["spend_usd"] > 0
```
Match the payload fields (`cost` and the rest) to the existing `agent_turn`/`agent_exited` tests in `test_state.py`.

In `test_metrics.py`: a log with one sizer call (`sizer=True`, cost 0.02) and one work call on `build/build/task-1/implement` (cost 0.10). The build phase has `cost_usd == 0.12`, `sizing_cost_usd == 0.02`, and exactly one step, with one attempt.

In `test_signals.py`:
```python
def test_escalations_undersized_and_fallbacks_are_counted_per_phase():
    events = [_ev("effort_decided", key="build/build/task-1/implement", level="medium", source="plan_sizer"),
              _ev("effort_escalated", key="build/build/task-1/implement", to="high", trigger="gate"),
              _ev("effort_decided", key="build/build/task-2/implement", level="medium", source="fallback"),
              _ev("effort_escalated", key="build/build/task-2/implement", to="high", trigger="gate"),
              _ev("effort_decided", key="verify/verify", level="high", source="sizer"),
              _ev("effort_escalated", key="verify/verify", to="xhigh", trigger="gate")]
    out = run_signals(events, {"phases": []})
    assert out["effort_escalations:build"] == 2 and out["undersized:build"] == 1   # a fallback is not undersized
    assert out["sizer_fallbacks:build"] == 1 and "undersized:verify" not in out
    assert parse_target("undersized:build") == ("undersized", "build")

def test_sizer_turns_and_cost_stay_out_of_step_signals():
    ...  # an `agent_turn` with `sizer=True` adds nothing to `turns_per_step:build`, and a phase with
    ...  # `cost_usd: 0.12, sizing_cost_usd: 0.02` and one stepped call has `cost_per_task:build == 0.10`
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_state.py agentflow/tests/test_metrics.py agentflow/tests/test_signals.py --import-mode=prepend -q -k "effort or sizer or undersized"`
Expected: FAIL (unknown event types; ghost `/size` rows; sizer counted as a step).

- [ ] **Step 3: Implement**

- `state.py`:
  - `_step_row` defaults gain `"effort_source": None`. The agent row built in `_on_agent_started` gains `"sizer": bool(ev.get("sizer"))`.
  - Every handler that reaches `_step_row` from an agent event (`_on_agent_started`, `_on_agent_session`, `_on_agent_turn`, `_on_agent_exited`, and any other) leaves step rows alone when the event or its agent row has `sizer`. It still updates the agent row and `spend_usd`.
  - `_on_effort_decided` sets the row's `effort`, `sized = True` and `effort_source`.
  - `_on_effort_escalated` sets `effort = to` and `effort_changed = _direction(from, to)`, and appends `{"key", "from", "to", "reason": trigger}` to `state["effort_changes"]`.
  - `_on_sizer_started` and `_on_plan_sized` change nothing; they are read from the log.
  - `REQUIRED`: `effort_decided: ("key", "level")`, `effort_escalated: ("key", "to")`, `sizer_started: ("key",)`, `plan_sized: ("key",)`.
- `metrics.py`: a call whose `agent_started` has `sizer` adds its cost to its phase's `cost_usd` and `sizing_cost_usd` (default 0). It never becomes a step, an attempt or a `model · effort` row of a step.
- `signals.py`:
  - Add `effort_escalations`, `undersized` and `sizer_fallbacks`, all `("lower", True)` and in `COUNT_FAMILIES`.
  - `run_signals` keeps the last `effort_decided` per key, as `(level, source)`. A `fallback` decision adds 1 to `sizer_fallbacks:<phase>`. Each `effort_escalated` adds 1 to `effort_escalations:<phase>`, and also to `undersized:<phase>` when the decided level was `low` or `medium` and the source was not `fallback`.
  - `_turns` skips events with `sizer`.
  - `cost_per_task` uses `cost_usd - sizing_cost_usd`.
  - Extend the docstring.

- [ ] **Step 4: Run the tests**

Run: the Step 2 command, then the whole of the three files and `test_scorecard.py`. Expected: PASS.

- [ ] **Step 5: Commit**

`git commit -m "Keep sizer calls out of step rows and step metrics, and count escalations, undersized steps and fallbacks."`

---

### Task 4: A read-only agent call

**Files:**
- Modify: `agentflow/agents.py` (`AgentCall`, `ClaudeAgent.argv`, `CodexAgent.argv`)
- Test: `agentflow/tests/test_agents.py`

`agents.py` is on the security overlay's paths: plan and review this task with the `security-and-hardening` skill. The pre-flight box must be ticked.

**Interfaces:**
- Produces:
  - `AgentCall.max_turns: int | None = None`. When set, `ClaudeAgent.argv` passes it instead of `spec.max_turns`.
  - `AgentCall.read_only: bool = False`.
    - `ClaudeAgent.argv` adds `--tools` with `",".join(READ_ONLY_TOOLS)`, where `READ_ONLY_TOOLS = ("Read", "Grep", "Glob")`, and adds `--strict-mcp-config`. Both go before `--model`, so the variadic `--tools` can never take the prompt.
    - `CodexAgent.argv` (fresh session only) adds `-s read-only`.

- [ ] **Step 1: Write the failing tests**

```python
def test_a_call_turn_cap_overrides_the_agents(claude, call):
    assert ("--max-turns", "8") in pairs(claude.argv(dataclasses.replace(call, max_turns=8)))

def test_a_read_only_claude_call_allows_only_read_tools_and_no_mcp(claude, call):
    argv = claude.argv(dataclasses.replace(call, read_only=True))
    at = argv.index("--tools")
    assert argv[at + 1] == "Read,Grep,Glob" and argv[at + 2].startswith("--")
    assert "--strict-mcp-config" in argv and argv[-1] == call.prompt
    assert "--tools" not in claude.argv(call)

def test_a_read_only_codex_call_uses_the_read_only_sandbox(codex, call):
    assert ("-s", "read-only") in pairs(codex.argv(dataclasses.replace(call, read_only=True)))
```
Use the Codex fixture that `test_agents.py` already has, or build one the way its Codex tests do.

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_agents.py --import-mode=prepend -q -k "turn_cap or read_only"`
Expected: FAIL (`AgentCall` has neither field).

- [ ] **Step 3: Implement** the two fields and the argv lines.

- [ ] **Step 4: Run the tests**

Run: the whole of `test_agents.py`. Expected: PASS.

- [ ] **Step 5: Commit**

`git commit -m "Let an agent call be read-only and carry its own turn cap."`

---

### Task 5: Size one step before its work session

**Files:**
- Create: `agentflow/sizing_lane.py`, `agentflow/prompts/size.md`
- Modify: `agentflow/lanes.py` (`Executor` bases; `run_agent_step`)
- Modify: `agentflow/config.py` (`PROMPT_PLACEHOLDERS`: `task_card`, `task_cards`)
- Modify: `agentflow/guards.py` (`ViolationKind`: `sizer_edit`). This is on the security overlay's paths.
- Test: `agentflow/tests/test_sizing_lane.py`

**Interfaces:**
- Consumes:
  - Task 2's functions, `AgentCall.read_only` and `max_turns` (Task 4), and the `sizer` handling (Task 3).
  - `overlays.security_path(cfg, ctx)`.
  - `gitops`: `changed_since`, `git_state`, `tree_state`, `head`. Also `guards.snapshot`, `guards.changed_paths` and `run_control.wait_for_agents`.
- Produces:
  - `run_agent_step(..., size: bool = True)`.
  - `class SizingLane`, a mixin that uses `cfg`, `run`, `agents`, `ctx`, `worktree` and `overrides`:
    - `size_step(self, sr: "_StepRun", failing_gates: list[str]) -> str | None`. It emits `effort_decided` and returns the level. `None` means the run stopped.
    - `_repo_state(self) -> dict`: `{"tree", "head", "state"}`, with `state` a sha256 of `json.dumps([git_state(...), tree_state(...)], sort_keys=True, default=str)`.
    - `_sizer_call(self, key: str, prompt: str, agent: Agent, model: str, effort: str, phase: str, role: str, turns: int, timeout_s: int) -> tuple[dict | None, list[str]]`. It returns the parsed answer, or `None`, and what changed. It emits `sizer_started` first.
    - `_run_agent(self, agent: Agent, call: AgentCall, on_event: EventCallback) -> AgentOutcome`. This is a seam for tests.
    - `_sizer_stop(self, key: str, changed: list[str], before: dict) -> None`. `before` is the `_repo_state()` that the sizer's `sizer_started` recorded.
    - `sizer_reset_needed(self, key: str) -> list[str]`. When the last `guard_tripped` for `key` is a `sizer_edit` and no later `effort_decided` or `plan_sized` cleared it, it returns what still differs between the current `_repo_state()` and the `before` recorded in that `guard_tripped`. Otherwise it returns `[]`. A step and a plan prefix work the same way.
    - `_orphan_check(self, key: str) -> list[str]`. After `wait_for_agents` for a size key whose last `sizer_started` has no later `effort_decided`, `plan_sized` or `guard_tripped`, it returns what differs from that `sizer_started`.

- [ ] **Step 1: Write the failing tests**

Configure with `flow_repo.configure({"effort": {"sizing": True}})`, and use the `KEY`, `run_step` and `_interrupted` helpers of `test_agent_step.py`. `KEY` is `build/impl`, a build-role step, so it is not a judgement step.

```python
ANSWERS = dict.fromkeys(sizing.QUESTIONS, "no")

def answer(**kw):
    return {"final_text": json.dumps({"answers": ANSWERS | kw, "reason": "r"})}

def test_a_step_is_sized_before_its_work_session(flow_repo):
    flow_repo.script({f"{KEY}/size": [answer(mechanical="yes")]})
    run = flow_repo.new_run()
    assert run_step(flow_repo) == "done"
    sizer, work = flow_repo.calls()
    assert ("--model", "haiku") in pairs(sizer["argv"]) and ("--effort", "low") in pairs(sizer["argv"])
    assert ("--max-turns", "8") in pairs(sizer["argv"]) and ("--tools", "Read,Grep,Glob") in pairs(sizer["argv"])
    assert ("--effort", "low") in pairs(work["argv"]) and "--tools" not in work["argv"]
    decided = flow_repo.events(run, "effort_decided")[0]
    assert (decided["key"], decided["level"], decided["source"]) == (KEY, "low", "sizer")
    started = flow_repo.events(run, "agent_started")
    assert started[0]["sizer"] is True and started[1]["sizing"] is False
    assert flow_repo.events(run, "sizer_started")[0]["key"] == f"{KEY}/size"
    assert flow_repo.events(run, "step_started")[0]["effort"] == "low"

def test_a_judgement_step_is_sized_by_its_own_model(flow_repo):
    ...  # a `review`-phase step: the sizer's argv has the step's own model and `--effort medium`;
    ...  # all-`no` answers give `medium`, one `risk: unsure` gives `high`; `source` is `judge`

@pytest.mark.parametrize("act", [{"final_text": "not json"}, {"final_text": '{"answers": {"risk": "no"}}'},
                                 {"exit": 1}, {"sleep": 999}, {"rate_limit": {"status": "rejected"}},
                                 "call_raises", "git_raises"])
def test_a_failed_sizer_falls_back_without_a_stop(flow_repo, monkeypatch, act):
    monkeypatch.setattr(sizing, "SIZE_TIMEOUT_S", 2)   # the code reads `sizing.SIZE_TIMEOUT_S` at call time
    if act == "call_raises":
        monkeypatch.setattr(SizingLane, "_run_agent", lambda *a, **k: (_ for _ in ()).throw(OSError("gone")))
    elif act == "git_raises":
        monkeypatch.setattr(sizing_lane, "changed_since", lambda *a, **k: (_ for _ in ()).throw(GitError("x")))
    else:
        flow_repo.script({f"{KEY}/size": [act]})
    run = flow_repo.new_run()
    assert run_step(flow_repo) == "done"
    decided = flow_repo.events(run, "effort_decided")[0]
    assert (decided["level"], decided["source"]) == ("medium", "fallback")
    assert flow_repo.events(run, "hitl_raised") == [] and flow_repo.events(run, "run_held") == []

@pytest.mark.parametrize("act", [{"write": {"toy/x.py": "y = 1\n"}},
                                 {"write": {"toy/x.py": "y = 1\n"}, "git_commit": "sneaky"},
                                 {"write": {".git/hooks/post-commit": "#!/bin/sh\n"}}])
def test_a_sizer_that_changes_the_repository_stops_the_run(flow_repo, act):
    flow_repo.script({f"{KEY}/size": [act | answer()]})
    run = flow_repo.new_run()
    assert run_step(flow_repo) == "stopped"
    assert flow_repo.events(run, "effort_decided") == [] and len(flow_repo.calls()) == 1
    assert flow_repo.events(run, "guard_tripped")[0]["violations"][0]["kind"] == "sizer_edit"

def test_a_restart_after_a_sizer_edit_needs_the_state_back(flow_repo):
    ...  # after the `.git/hooks` case: approving and running again is refused with `sizer_edit` naming
    ...  # "git state"; after deleting the hook, the step runs and is sized again (a second `effort_decided`)

def test_a_run_paused_while_sizing_starts_no_work(flow_repo):
    ...  # the sizer act pauses the run (as the existing pause tests do): "stopped", no `step_started`, one call

def test_an_orphan_sizer_is_checked_on_resume(flow_repo):
    ...  # a `sizer_started` with no outcome, then a file written to the tree: resuming stops with `sizer_edit`;
    ...  # with the tree unchanged, the step is sized afresh

def test_an_over_budget_run_is_not_sized(flow_repo):
    ...  # spend at the budget: no `/size` call; the run stops `budget_exceeded` as today

def test_a_resumed_step_is_not_sized(flow_repo):
    ...  # `_interrupted(flow_repo, run, effort="high")`, then run: no `/size` call; the work call resumes at `high`

def test_sizing_off_runs_at_the_default(flow_repo):
    ...  # configure sizing False: one call, `--effort medium`, no `effort_decided`

def test_sizer_spend_counts_toward_the_run_budget(flow_repo):
    flow_repo.script({f"{KEY}/size": [answer() | {"cost": 0.05}]})
    run = flow_repo.new_run()
    run_step(flow_repo)
    assert run.refresh()["spend_usd"] >= 0.05
```
`fake_agent.py` may lack an act that pauses a run, or may refuse to write under `.git`. If so, add the act it needs and list it in the docstring. That makes one more file, which is still within 8.

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_sizing_lane.py --import-mode=prepend -q`
Expected: FAIL (no `/size` call is made).

- [ ] **Step 3: Implement**

**`prompts/size.md`** has no preamble, no lessons and no skill brief. It says:
- The agent is sizing a step it will not do.
- It has only Read, Grep and Glob. It reads only what the card names, and only what it needs.
- Its final message is exactly one JSON object: `{"answers": {<each question>: "yes"|"no"|"unsure"}, "reason": "<one line>"}`.
- Each question is listed with its one-line meaning from §6b item 3.
- The `{task_card}` slot comes last.

**`size_step`** runs inside one `try/except Exception`. Any exception in it gives a fallback `effort_decided`, unless the stop path already ran.
1. **The level for the sizer.** `judgement = sizing.is_judgement(sr.phase.name, sr.role)`. The model is `sr.model` for a judgement step, else `sr.agent.spec.models.get("mechanical", sr.model)`. The effort is `sizing.sizer_effort(judgement, allowed)`.
2. **The facts.**
   - The ask is the step's template filled with the step's values (`fmt(_template(cfg, step.prompt or DEFAULT_TEMPLATE), self._values(sr))`), with no preamble and no brief.
   - Changed paths are `changed_since(worktree, base_sha)` when `base_sha` is set. `files = len(changed)`, and no named files.
   - `security = overlays.security_path(cfg, ctx)`.
3. **A sizer left behind.** Run `wait_for_agents(run, cfg, size_key, SIZE_TIMEOUT_S)`, then `_orphan_check(size_key)`. A non-empty result calls `_sizer_stop` and returns `None`.
4. **The call.** Run `_sizer_call` with `SIZE_MAX_TURNS` and `SIZE_TIMEOUT_S`. A non-empty `changed` calls `_sizer_stop` and returns `None`.
5. **The halt check.** If `halted(run.refresh()["status"])`, return `None`.
6. **The decision.** Call `sizing.decide`, then emit `effort_decided` with:
   - `source`: `fallback`, `judge` or `sizer`
   - `answers` normalized, and `reason` through `reason_text`
   - `sizer_model`, `files` and `security`

   Return the level.

**`_sizer_call`**:
1. Emit `sizer_started{key, **self._repo_state()}`.
2. Build the `AgentCall` as `_agent_call` does, but with:
   - `read_only=True`, `max_turns=turns`, `timeout_s=timeout_s`
   - `resume_session=None`, `add_dirs=[]`, `step_key=key`
   - the transcript `transcript_path(key, 1)`
3. The event callback emits the agent events as `_call` does, adding `sizer=True` to every event and `sizing=True, phase, role` to `agent_started`.
4. After the call, `changed` lists each difference from the `sizer_started` values: the paths from `changed_paths`, `"HEAD"`, or `"git state"`.
5. The answer is `parse_result(outcome.final_text)`. An interruption or a timeout gives `(None, changed)`, and so does an exception from `_run_agent`: compute `changed` in a `finally`, and fall back to `["unknown"]` if computing it raises. Never hold, remind or stop here.

**`_sizer_stop`** emits `guard_tripped` (kind `sizer_edit`, detail: the changed list, plus a `before` field holding the `sizer_started` state) and raises `guard_violation` on `key`. It does not emit `step_finished`, so the step stays unsized and is sized again after the reset.

**`run_agent_step`**:
- Takes `size: bool = True`.
- **Sizer reset first.** Before anything else for a fresh start, a non-empty `self.sizer_reset_needed(key)` returns `self._fail(sr, [Violation("sizer_edit", "reset " + ", ".join(...) + " first")])`.
- **When it sizes.** Right after `sr` is built and the shared session is looked up, and before the turn is built, when every one of these holds:
  - `size` and `cfg.effort.sizing` are set, and this is not `self.dry_run`;
  - `status in UNSTARTED_STEP` and `mode == "first"`;
  - not `rec.get("sized")`, or `sizer_reset_needed` was just cleared;
  - no shared session was taken, and `SIZE_MARK not in key`;
  - the run's spend is under its budget.

  `None` returns `"stopped"`; otherwise set `sr.effort = level`.
- **The turn** is then built with `sized=True` and `sizing=False`.
- **The floor.** On every path, once `sr.effort` is known: `sr.effort = sizing.floor(sr.effort, sizing.is_judgement(phase.name, role), allowed)`.

`Executor` gains `SizingLane` as a base.

- [ ] **Step 4: Run the tests**

Run: the Step 2 command, then `test_agent_step.py`, `test_guards.py`, `test_pack.py` and `test_feature_lane.py`. The fixture packs have sizing off, so the existing tests must pass unchanged.
Expected: PASS.

- [ ] **Step 5: Commit**

`git commit -m "Size each agent step in a read-only session before its work session starts."`

---

### Task 6: Internal sessions and the floor everywhere

**Files:**
- Modify: `agentflow/flow_control.py` (`_ask_agent`), `agentflow/autonomy.py` (`_run_session`), `agentflow/feature_lane.py` (`_record_one`), `agentflow/retro.py` (the retro session), `agentflow/lanes.py` (`dry_agent_argv`), `agentflow/run_control.py` (`attach`)
- Test: `agentflow/tests/test_sizing_lane.py`

**Interfaces:**
- Consumes: `run_agent_step(..., size=...)`, `sizing.floor`, `sizing.is_judgement`.
- Produces: no new names.

- [ ] **Step 1: Write the failing tests**

```python
def test_internal_sessions_are_not_sized(flow_repo):
    ...  # a condition question (`_ask_agent`) and a resolver session with sizing on: no `/size` call, no `effort_decided`

def test_the_floor_holds_with_sizing_off(flow_repo):
    ...  # sizing False, `effort.default: low`, a `review`-phase step: its call carries `--effort medium`

def test_the_dry_run_prints_the_floored_level(flow_repo):
    ...  # `effort.default: low`: a dry run of a `plan`-phase step prints `--effort medium`

def test_attach_applies_the_floor(flow_repo, monkeypatch):
    ...  # a review step recorded at `low` (an old run): `attach` runs `claude --resume … --effort medium`
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_sizing_lane.py --import-mode=prepend -q -k "internal or floor or dry_run or attach"`
Expected: FAIL.

- [ ] **Step 3: Implement**

- The four internal callers pass `size=False`.
- `dry_agent_argv` floors `effort.default`.
- `attach` floors the recorded level with `sizing.is_judgement(row phase, row role)`. A row without a role counts as `build`.

- [ ] **Step 4: Run the tests**

Run: the Step 2 command, then `test_autonomy.py`, `test_conditions.py`, `test_retro.py`, `test_control_cmds.py` and `test_explain_golden.py`. Expected: PASS, with the golden files unchanged.

- [ ] **Step 5: Commit**

`git commit -m "Keep internal sessions unsized and hold define, plan, review and debug steps at medium or above on every path."`

---

### Task 7: Size a plan's tasks when build starts

**Files:**
- Create: `agentflow/prompts/size_plan.md`
- Modify: `agentflow/sizing_lane.py` (`size_plan`, `_line_count`), `agentflow/feature_lane.py` (`run_foreach`), `agentflow/overlays.py` (`touches_security`)
- Test: `agentflow/tests/test_sizing_lane.py`

**Interfaces:**
- Consumes:
  - From Task 5: `_sizer_call`, `_sizer_stop`, `sizer_reset_needed` and `_orphan_check`.
  - `sizing.plan_digest`, plus `sources.parse_plan` and `sources.task_files`, which take a `PlanTask`.
  - `self._batch_of` and `small` from `run_foreach`, and `resolve_model`.
- Produces:
  - `overlays.touches_security(cfg: Config, paths: Iterable[str]) -> bool`: true when any path matches `overlays.security.when_paths` (`guards.path_matches`).
  - `_line_count(self, rel: str) -> int | None`.
  - `size_plan(self, phase: Phase, step: Step, plan: Path, prefix: str, small: dict) -> Outcome`, which returns `"done"` or `"stopped"`. It emits `effort_decided` (`source: plan_sizer` or `fallback`) for `<prefix>/task-<n>/implement`, and for `<prefix>/task-<n>/review` when the task has a review. When nothing fell back, it then emits `plan_sized{key: prefix, digest, tasks}`.

- [ ] **Step 1: Write the failing tests**

```python
def test_a_plan_is_sized_by_the_plan_model(flow_repo):
    ...  # a 2-task plan; the plan sizer marks task 1 mechanical and task 2 risky
    calls = flow_repo.calls()
    assert sum(1 for c in calls if c["env"]["AGENTFLOW_STEP"].endswith("/size-1")) == 1
    assert ("--model", "opus") in pairs(calls[0]["argv"]) and ("--effort", "medium") in pairs(calls[0]["argv"])
    levels = {e["key"]: e["level"] for e in flow_repo.events(run, "effort_decided")}
    assert levels["build/build/task-1/implement"] == "low" and levels["build/build/task-1/review"] == "medium"
    assert levels["build/build/task-2/implement"] == "high" and levels["build/build/task-2/review"] == "high"

def test_a_long_plan_is_sized_in_chunks(flow_repo):
    ...  # 8 tasks: two plan-sizer calls (`/size-1` with 6 cards, `/size-2` with 2)

def test_a_batch_runs_at_its_highest_members_level(flow_repo):  # configure(batch=True)
    ...  # tasks 1 and 2 in one batch, sized low and high: both implement decisions are `high`, and the
    ...  # batch's review (on its last member) takes the highest member review level

def test_only_a_task_naming_a_security_path_is_high(flow_repo):
    ...  # `when_paths` set; task 1 names a matching file, task 2 does not; all-`no` answers:
    ...  # task 1 implement `high`, task 2 `low` or `medium` (the run-wide keyword grep does not count here)

def test_a_changed_plan_resizes_only_unstarted_tasks(flow_repo):
    ...  # stop after task 1 commits (its boxes ticked); resume unchanged: no new plan-sizer call;
    ...  # then edit task 2's text: one more call and a new digest; task 1 keeps its level

def test_a_failed_plan_sizer_is_retried_on_the_next_entry(flow_repo):
    ...  # first plan sizer returns garbage: every task gets `fallback` (implement `effort.default`, review
    ...  # `high`) and no `plan_sized`; after a stop and resume, the plan sizer runs again for unstarted tasks

def test_a_plan_sizer_edit_blocks_build_until_reset(flow_repo):
    ...  # the plan sizer writes a file: build stops with `sizer_edit`; running again is refused until the
    ...  # file is removed; then build sizes the plan again and runs task 1
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_sizing_lane.py --import-mode=prepend -q -k "plan or chunk or batch or security"`
Expected: FAIL (no plan-sizer call).

- [ ] **Step 3: Implement**

**`prompts/size_plan.md`** is the same as `size.md`, except that:
- it covers every task in `{task_cards}`, one card per task, each built with `sizing.task_card`;
- its answer is `{"tasks": {"<n>": {"answers": {...}, "reason": "..."}}}`.

**`_line_count`** returns the count of `b"\n"`, but only for a path that stays inside the worktree once resolved, is not a symlink (`os.lstat`), and is a regular file of at most 2 MB. Anything else gives `None`.

**`size_plan`**:
1. **When it skips.** Return `"done"` when sizing is off, in a dry run, in a refine round, or when the run's spend is at its budget.
2. **Sizer reset first.** A non-empty `sizer_reset_needed(prefix)` raises `guard_violation` on `prefix` (if it is not already waiting) and returns `"stopped"`.
3. **The digest** is `sizing.plan_digest` over `(t.n, t.title, t.text)` for `sources.parse_plan(plan)`. When the last `plan_sized` for `prefix` has the same digest, return `"done"`.
4. **Which tasks.** The open tasks whose implement row has no `session_id`. Size them in chunks of `PLAN_SIZE_CHUNK`, with keys `f"{prefix}{SIZE_MARK}-{i}"`.
5. **The call.**
   - The model is `resolve_model(cfg, phase, Step(id="size", type="agent", role="plan"), "plan", overrides)`. The synthetic step has no `model:`, so the foreach step's own model does not apply.
   - The effort is `sizer_effort(True, allowed)`, with `PLAN_SIZE_MAX_TURNS` and `PLAN_SIZE_TIMEOUT_S`.
   - Wait for, and check, an orphaned sizer, then run the halt check, as `size_step` does.
   - A changed repository calls `_sizer_stop(prefix, changed, before)`, with `before` taken from that chunk's `sizer_started`, and returns `"stopped"`.
   - Any exception makes that chunk a fallback.
6. **Levels per task.**
   - `files` is `[(path, self._line_count(path)) for path in sources.task_files(task)]`.
   - `security` is `overlays.touches_security(cfg, sources.task_files(task))` only. The run-wide `security_path` stays with the per-step sizer.
   - The implement level uses `judgement = sizing.is_judgement(phase.name, step.role or "build")`. The review level uses `judgement=True`.
7. **Batches and small plans.**
   - Raise each batch's implement levels to `sizing.highest` of its members.
   - Raise the review of a batch's last member to the highest review level among its members.
   - With `small["active"]` and `small.get("shared_session")`, raise every task to the highest level.
8. **Recording.** Emit `effort_decided` for each task. Emit `plan_sized` only when no chunk fell back.

**`run_foreach`**: call `size_plan` right after `self._batch_of = self._batches(...)`. `"stopped"` returns `"stopped"`.

- [ ] **Step 4: Run the tests**

Run: the Step 2 command, then `test_feature_lane.py`. Expected: PASS.

- [ ] **Step 5: Commit**

`git commit -m "Size every build task and its review from the plan, in chunks, when build starts."`

---

### Task 8: Escalate once on a failed gate or a reopening review

**Files:**
- Modify: `agentflow/sizing_lane.py` (`escalate_effort`), `agentflow/lanes.py` (`_loop` fix branch; `run_agent_step` resume and refine branches)
- Test: `agentflow/tests/test_sizing_lane.py`

**Interfaces:**
- Consumes: `sizing.escalate`; the `effort_escalated` event (Task 3).
- Produces: `escalate_effort(self, sr: "_StepRun", trigger: str) -> None`. It does nothing when sizing is off, when the log already holds an `effort_escalated` for `sr.key`, or when `escalate` returns `None`. Otherwise it emits `effort_escalated{key, from, to, trigger}` and sets `sr.effort`.

- [ ] **Step 1: Write the failing tests**

```python
def test_the_first_gate_failure_resumes_one_level_higher(flow_repo):
    ...  # sized medium; the work call leaves a failing gate (as in test_fix_loop_keeps_high_effort)
    work, fix = flow_repo.calls()[1:3]
    assert ("--effort", "medium") in pairs(work["argv"])
    assert ("--effort", "high") in pairs(fix["argv"]) and "--resume" in fix["argv"]
    assert flow_repo.events(run, "effort_escalated")[0]["trigger"] == "gate"

def test_a_step_escalates_once(flow_repo):
    ...  # two failing fix attempts: one `effort_escalated`, and the second fix call is still `high`

def test_a_rollover_keeps_the_level(flow_repo):
    ...  # `usage_pct: [81]` on the work call: the fresh session runs at the sized level; no `effort_escalated`

def test_a_reopening_review_resumes_the_implementer_one_level_higher(flow_repo):
    ...  # a per-task review with a blocking finding: the implementer's resumed call carries `--effort high`
    ...  # with trigger `review`; the second review round keeps the reviewer's own level

def test_no_escalation_at_the_top_level(flow_repo):
    ...  # sized `high` with allowed [low, medium, high]: a gate failure emits no `effort_escalated`

def test_no_escalation_with_sizing_off(flow_repo):
    ...  # sizing False: the fix call stays at `effort.default`
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_sizing_lane.py --import-mode=prepend -q -k "escalat or rollover or reopening"`
Expected: FAIL (fix calls keep the sized level).

- [ ] **Step 3: Implement**

- `_loop`: in the branch that starts a fix attempt, call `self.escalate_effort(sr, "gate")` before building the fix turn. The turn still resumes `sr.session`, and `_agent_call` passes the new `sr.effort`.
- `run_agent_step`: call `self.escalate_effort(sr, "review")` after `sr` is built and before the turn, in either of two cases:
  - it is resuming, and `_approved(since, key)` is a `step_reopened` with `reason == "review"`;
  - `mode == "refine"` reuses the session.

  The floor runs after it.
- `_rollover` stays unchanged; a test pins that.

- [ ] **Step 4: Run the tests**

Run: the Step 2 command, then `test_agent_step.py`, `test_refine.py` and `test_bug_eval_security.py`. Expected: PASS.

- [ ] **Step 5: Commit**

`git commit -m "Raise a step's effort once on its first failed gate or a reopening review, resuming its session."`

---

### Task 9: Remove the in-session effort check, and the docs

**Files:**
- Modify: `agentflow/prompts/_preamble.md`, `agentflow/prompts/_result.md`, `agentflow/lanes.py` (`build_prompt`, `_effort_change`, the work turn's `sizing` flag)
- Modify: `agentflow/tests/test_agent_step.py`, `agentflow/tests/test_refine.py`, `agentflow/tests/test_bug_eval_security.py`. Check them with `grep -ln "effort_change\|Effort check\|EFFORT_CHECK\|GO_HIGH" agentflow/tests/*.py`.
  - `test_feature_lane.py` and `test_control_cmds.py` only emit old `effort_changed` events, which are still reduced. Leave them unless they fail.
  - `test_hitl.py` keeps parsing `effort_change` unchanged.
- Modify: `docs/agentflow-playbook.md` (the effort paragraph in the "Agents" section, the §15 signal list), `CHANGELOG.md`

The proposal's "As built" note goes in this task's commit too. That is the ninth file, and `MAX_TASK_FILES` warns over 8. So add the note right after this task, in the same branch, as described in "After the tasks".

**Interfaces:**
- Produces:
  - No prompt carries the effort check.
  - Every `effort_change` result is refused with `effort_refused`.
  - A work turn is never `sizing=True`, with sizing on or off.

- [ ] **Step 1: Write the failing tests**

```python
def test_no_prompt_carries_the_effort_check(flow_repo):
    ctx = Ctx(repo=flow_repo.root, values={})
    text = build_prompt(flow_repo.cfg, make_step(), ctx, {"sized": False}, {}, "first")
    assert "Effort check" not in text and "effort_change" not in text

def test_an_effort_change_is_always_refused(flow_repo):
    flow_repo.script({KEY: [{"result": {"status": "effort_change", "effort": "high", "reason": "big"}},
                            {"result": DONE}]})
    run = flow_repo.new_run()
    assert run_step(flow_repo) == "done"
    refused = flow_repo.events(run, "effort_refused")[0]
    assert "sizes each step" in refused["reason"] and flow_repo.events(run, "effort_changed") == []
    c = flow_repo.calls()
    assert ("--effort", "medium") in pairs(c[1]["argv"]) and "--resume" in c[1]["argv"]
    assert all(e["sizing"] is False for e in flow_repo.events(run, "agent_started"))
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_agent_step.py --import-mode=prepend -q -k "effort_check or always_refused"`
Expected: FAIL (the preamble still holds the check; the change is accepted).

- [ ] **Step 3: Implement**

- **Prompts.** In `_preamble.md`, delete the `<!-- effort-check -->` block. In `_result.md`, drop `effort_change` from the status list.
- **`build_prompt`** always strips `EFFORT_BLOCK`, since a pack's own `_preamble.md` copy may still hold it.
- **`_effort_change`** always refuses, with the reason `"the runner sizes each step before it starts"`. The note and the resumed turn stay as they are today. Every work `_Turn` is built with `sizing=False`.
- **Tests found by the grep:**
  - A scripted `effort_change` is now refused, and the step carries on at its level.
  - `"Effort check"` assertions become checks that it is absent.
  - `test_first_call_medium_with_sizing` asserts `started["sizing"] is False`.
- **Playbook, the effort paragraph:** `The runner sizes every step before it starts: a short read-only session answers a fixed checklist about the task, and the runner turns the answers into a level within effort.allowed. Build tasks and their reviews are sized from the plan by the plan model. Define, plan, review and debug steps never run at low and are sized by their own model; other steps by the mechanical model. A step goes up one level once, on its first failed gate or a reopening review, in the same session. effort.sizing: false runs every step at effort.default, with define, plan, review and debug steps still at medium or above.`
- **Playbook §15:** add `effort_escalations:<phase>`, `undersized:<phase>` and `sizer_fallbacks:<phase>` to the signal list.
- **`CHANGELOG.md` `[Unreleased]`:**
  - `Added`: sizing per step and per plan, escalation, `effort.sizing`, the four events and the three signals.
  - `Changed`: define, plan, review and debug steps never run below `medium`, and `validate` refuses an `effort.allowed` with nothing at or above `medium`.
  - `Removed`: the in-session effort check, and the `effort_change` status from the prompts.

- [ ] **Step 4: Run the full suite and the gates**

Run, in order:
- `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests --import-mode=prepend -q`
- `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_portability.py --import-mode=prepend -q`
- `bin/agentflow validate`

Expected: all pass, `0 warnings`, and the golden explain files unchanged (`git diff --exit-code agentflow/tests/fixtures/explain`).

- [ ] **Step 5: Commit**

`git commit -m "Remove the in-session effort check now that the runner sizes every step."`

---

## After the tasks

- **The proposal's "As built" note** (an agent or a human, on the same branch). In `docs/agentflow-harness-proposal.md` §6b, append `**As built (slice ES, <date>, docs/plans/2026-11-12-agentflow-harness-slice-es.md).**` with this plan's "Deviations from §6b" list, plus any found while building. Commit: `git commit -m "Record how slice ES was built in the harness proposal."`
- **This repository's pack comments** (a human: `.agentflow/**` is forbidden in a run). In `.agentflow/config.yaml`, update the comments that describe the old check:
  - `effort_arg`: `# the runner sizes each step's level (see effort:)`
  - `effort.default`: `# the level when sizing is off or fails`
  - `effort.allowed`: `# the levels a step may run at`

  Then run `bin/agentflow validate`.
- **Thresholds.** Watch the next runs' `effort_decided` events, and the `undersized:<phase>` and `sizer_fallbacks:<phase>` signals (`agentflow scorecard`).
