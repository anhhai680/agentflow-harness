# Slice FT: Fewer build turns (checks the implementer runs itself, and a code map the run keeps) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make each build session take fewer turns:
- Stop gate-fix rounds over lint errors the implementer could have caught itself.
- Stop each fresh session from searching again for code that the planner or an earlier session already found.

**Architecture:**
- **Fix 1: checks the implementer runs itself.**
  - A gate can say `agent_check: true`. The build prompts render the step's flagged gates, filled through a new `{self_checks}` placeholder, as commands the implementer runs before it writes its result.
  - With no flagged gate, `{self_checks}` renders today's sentence word for word, so an existing pack sees no change.
  - The runner still runs every gate afterwards. A self-check only moves the first failure into the same session.
- **Fix 2: a code map.**
  - The plan skill asks for an `**Interfaces:**` list per task, giving the existing code the task calls as `path:line` plus its signature.
  - Any step's result may carry a `code_map` list. The runner keeps it as `code_noted` events, deduplicated and capped, the same way it keeps resolver `facts`.
  - Every later prompt shows the map in the preamble, through a `{code_map}` slot that is dropped when empty.
  - The build and review prompts stop telling a session to re-read the plan, and point it at the Interfaces list and the map instead.

**Tech Stack:** Python 3.12, stdlib + `pyyaml`, pytest with `fake_agent.py` and the fixture packs.

**Spec:** this plan's **Evidence** section: the turn analysis of run `20261108-1516-feature-gap-loop`, done on 2026-11-08.

## Evidence (why)

The analysis covered build tasks 1–12 of run `20261108-1516-feature-gap-loop` in a downstream repository:
- **Volume:** 27 sessions, 418 model calls and 741 tool calls.
- **Exploring dominated:** 62% of tool calls were exploring (Read, `grep`, `sed -n`, `cat`, `ls`); edits were 13%.
  - Implementer sessions made 7–58 exploring calls each, with a median of about 24.
  - The spec was re-read in 18 sessions and the plan in 16.
  - Task 11 made 57 exploring calls before its first edit, mostly hunting for code the plan named without a path.
- **Gate-fix rounds:** 5 of 12 tasks failed the `build` gates. Four were the lint gate and one was the type checker. Each failure:
  - resumed a session holding 100k–180k tokens of context
  - reran every build gate

  The engine's own wording, "Do not run the linters, type checkers or other quality gates yourself", in `prompts/task.md:19`, `prompts/task_next.md:10` and `skills/agentflow-build/SKILL.md:21`, guarantees this.
- **Nothing carries over:** no session handed what it had found to the next. `facts` are kept only from resolver sessions, with `RUN_FACTS_MAX = 8`.

## Global Constraints

- **Dependencies:** engine dependencies stay stdlib + `pyyaml>=6`.
- **Portability:** `agentflow/tests/test_portability.py` must pass. No engine prompt or skill may contain `src/`, `docs/specs`, `SPEC-` or any other `DENYLIST` match. Examples use `app/names.py`.
- **Golden files:** `agentflow/tests/fixtures/explain/*.txt` stay byte-identical (`explain` prints no prompt text).
- **Config keys:** the one new key, `gates.<name>.agent_check`, sits under the already-protected `gates`. It still ships with:
  - `validate` checks
  - a `tests/test_config_edit.py` case
  - a playbook §14 line
- **Engine defaults** (`agentflow/defaults/config.yaml`) and the starters do not change. No gate is an agent check unless a pack says so.
- **No behaviour change for a pack that adopts nothing.** With no `agent_check` gate and no `code_map` in any result, every rendered prompt is byte-identical to today's. Task 2 and Task 4 pin this.
- **Pack prompt overrides keep working.** The fixture packs and this repository's pack override `task.md`/`task_next.md` without the new placeholders. Leave those fixture prompts unchanged: they are the test that an old override still renders. Task 6 updates this repository's own pack.
- **Model text is untrusted.** Each `code_map` entry goes through `hitl.clean_fact` and is capped exactly like `facts`:
  - it is NFC-normalised
  - control and format characters are dropped
  - it is cut at `FACT_MAX = 300`

  Braces in an entry go into the prompt verbatim, because `fmt` fills placeholders in one pass.
- **Changelog:** each task adds its line under `## [Unreleased]` in `CHANGELOG.md`.
- **Commit messages:** one imperative sentence, ending with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- **Test command shape:** `uv run --no-project --with pyyaml --with pytest pytest <file>[::<test>] --import-mode=prepend -q`. Run the full suite before finishing.

## Review Focus

1. **A pack that adopts nothing.** With no `agent_check` gate, the rendered `task.md` and `task_next.md` lines equal today's text exactly. With no `code_map` events, the preamble equals today's. Test: Task 2, Task 4.
2. **A flagged gate the runner would skip, or one outside the step's set.** An `agent_check` gate whose `when_exists` target is missing in the worktree, or that isn't in the step's `gates:`, is not listed. Test: Task 2.
3. **A flagged gate the agent can't run as written:**
   - `validate` refuses `agent_check: true` on a gate with `cwd`, a `run` holding a newline or a backtick, or a value that isn't a bool.
   - A self-check denied at run time makes the agent say so, not stop as `blocked`. This is pinned by the rendered wording.

   Test: Task 1, Task 2.
4. **Hostile or sprawling `code_map` text:**
   - a bidi override, a zero-width character, `{step_id}`, a 2,000-character entry, or 12 entries in one result
   - the same entry in a different case from two sessions
   - more than `CODE_MAP_MAX` entries over the run

   Each entry is cleaned, cut, kept at 8 per result, deduplicated by casefold and kept newest-first up to the cap, with braces verbatim. Test: Task 3, Task 4.
5. **Stops and old runs.** A step that ends `blocked`, `needs_human` or `effort_change` records no code map. A `state.json` written before `code_map` existed replays, and builds the same prompt. A plan whose `**Interfaces:**` lines hold backticked paths gets the same `task_files` and `plan_batches` as without them. Test: Task 3, Task 4, Task 5.

---

### Task 1: `gates.<name>.agent_check`, parsed and validated

**Depends on:** none

**Files:**
- Modify: `agentflow/config.py`:
  - `Gate` (around line 281)
  - `_build_gates` (around line 672)
  - the gates loop in the validator (around line 1371)
- Modify: `docs/agentflow-playbook.md` §14, after the `gates.<name>.cwd` line (around line 442)
- Modify: `CHANGELOG.md`
- Test: `agentflow/tests/test_config.py`, `agentflow/tests/test_config_edit.py`

**Interfaces:**
- **Uses:**
  - `config._optional(data, key, path, kind, default)` (`config.py:477`)
  - the validator's `self.error(path, message)` and `_join(path, key)`
- **Produces:** `Gate.agent_check: bool = False`, the last field of the dataclass.

- [ ] **Step 1: Write the failing tests** in `test_config.py`, using the `tmp_cfg` fixture with a patch on `gates`:

```python
def test_a_gate_is_no_agent_check_unless_it_says_so(tmp_cfg): ...
    # load(tmp_cfg()).gates["floor"].agent_check is False
    # load(tmp_cfg({"gates": {"floor": {"run": "true", "agent_check": True}}})).gates["floor"].agent_check is True

def test_agent_check_must_be_a_bool(tmp_cfg): ...
    # {"agent_check": "yes"} → ConfigError naming gates.floor.agent_check

@pytest.mark.parametrize("gate", [
    {"run": "true", "cwd": "web", "agent_check": True},
    {"run": "a\nb", "agent_check": True},
    {"run": "echo `x`", "agent_check": True},
])
def test_validate_refuses_an_agent_check_the_agent_cannot_run_as_written(tmp_cfg, gate): ...
    # validate(...) has an error at "gates.floor.agent_check" whose message contains "worktree root"
```

  In `test_config_edit.py`, add to `test_yaml_text_may_change_anything_but_the_protected_settings`'s list:
  `('floor:        {run: "true"}', 'floor:        {run: "true", agent_check: true}')`.

- [ ] **Step 2: Run them and expect failures.** `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_config.py agentflow/tests/test_config_edit.py --import-mode=prepend -q`. Expected: FAIL. The config_edit case may already pass, because `gates` is protected; keep it as the pin.

- [ ] **Step 3: Implement.**
  - **Parse:** `agent_check=_optional(raw, "agent_check", path, bool, False)` in `_build_gates`.
  - **Validate:** inside the existing gates loop, before the `baseline` early `continue`, when `gate.agent_check` is true and any of these holds:
    - `gate.cwd is not None`
    - `"\n" in gate.run.strip()`
    - `` "`" in gate.run ``

    then `self.error(_join(path, "agent_check"), "an agent check runs from the worktree root as one line, exactly as written: drop `cwd` and keep `run` to one line with no backtick, or turn agent_check off")`.
  - **Playbook §14 line:** "`gates.<name>.agent_check`: `true` lists the gate in the build prompt as a check the implementer runs itself before writing its result (the `{self_checks}` placeholder). Only for a fast gate, such as a linter, a formatter check or a type checker, never a whole test suite. The runner still runs the gate afterwards. It needs a one-line `run` with no backtick and no `cwd`, otherwise `validate` refuses it. Default `false`. Protected, as `gates` is."
  - **Changelog (Added):** "`gates.<name>.agent_check`: a gate the implementer runs itself before it finishes, so a lint failure is fixed in the same session instead of in a gate-fix round."

- [ ] **Step 4: Run Step 2's command.** Expected: PASS. Then run `bin/agentflow validate` and expect `0 warnings`.

- [ ] **Step 5: Commit** `agentflow/config.py`, the two test files, the playbook and the changelog: "Add gates.<name>.agent_check, a gate the implementer may run itself, with its validate checks."

---

### Task 2: The build prompts list the step's agent checks (`{self_checks}`)

**Depends on:** Task 1

**Files:**
- Modify: `agentflow/gates.py`: add `agent_checks` next to `resolve` (line 66)
- Modify: `agentflow/lanes.py`: `_values` (line 97), plus a new `_self_checks`
- Modify: `agentflow/config.py`: add `"self_checks"` to `PROMPT_PLACEHOLDERS` (line 106)
- Modify: `agentflow/prompts/task.md:19`, `agentflow/prompts/task_next.md:10`
- Modify: `agentflow/skills/agentflow-build/SKILL.md`: step 4 and the rule on line 21
- Modify: `CHANGELOG.md`
- Test: `agentflow/tests/test_agent_step.py`

**Interfaces:**
- **Uses:**
  - `gates.resolve(cfg, names) -> list[Gate]` (`gates.py:66`), which raises `KeyError` on an unknown name
  - `Step.gates: str | list[str] | None` (`config.py:308`)
  - `run.state["worktree"]`
  - the `_prompt(flow_repo)` and `make_step(**kw)` helpers in `test_agent_step.py`, whose `flow_repo` resolves the engine's own prompts
- **From earlier tasks:** `Gate.agent_check`.
- **Produces:**
  - `gates.agent_checks(cfg: Config, names: str | list[str] | None, worktree: Path) -> list[Gate]`
  - `lanes.SELF_CHECKS_NONE: str`
  - `lanes._self_checks(cfg: Config, step: Step, worktree: Path) -> str`
  - the `{self_checks}` placeholder

- [ ] **Step 1: Write the failing tests** in `test_agent_step.py`. They build `task.md` prompts with `make_step(prompt="task.md", gates="build")` on a `flow_repo` whose config patches gates:

```python
OLD_LINT_LINE = ("Do not run the linters, type checkers or other quality gates yourself: the runner's build gates "
                 "run them right after you, and a failure comes back to this session with its output.")

def test_with_no_agent_check_the_task_prompt_says_what_it_says_today(flow_repo): ...
    # for task.md and task_next.md: the prompt contains
    # "format what you changed if the repository has a formatter. " + OLD_LINT_LINE, and no "{self_checks}"

def test_agent_checks_are_listed_in_gate_order_as_one_fenced_block(flow_repo): ...
    # gates: py_lint {run: "lint .", agent_check: true}, py_types {run: "types", agent_check: true}, floor unflagged
    # the prompt contains "```sh\nlint .\ntypes\n```", does not contain OLD_LINT_LINE,
    # and does contain "the runner runs every build gate right after you"

def test_an_agent_check_the_runner_would_skip_or_another_step_owns_is_not_listed(flow_repo): ...
    # a flagged gate with when_exists: missing.txt → absent; a flagged gate in gate_sets.verify only → absent

def test_a_denied_self_check_is_reported_not_a_stop(flow_repo): ...
    # with one flagged gate, the prompt contains "If one is denied, skip it and say so in your summary"

def test_a_pack_override_without_the_placeholder_still_renders(flow_repo): ...
    # copy tests/fixtures/packs/python/.agentflow/prompts/task.md into the pack; flagged gates;
    # the prompt has OLD_LINT_LINE and no "{self_checks}"
```

  `test_every_engine_template_fills_with_every_placeholder` covers the new placeholder once it is in `PROMPT_PLACEHOLDERS`.

- [ ] **Step 2: Run them and expect failures.** `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_agent_step.py --import-mode=prepend -q`. Expected: FAIL.

- [ ] **Step 3: Implement.**
  - **`gates.agent_checks`:** `[]` when `names` is falsy. Otherwise, the gates `resolve(cfg, names)` returns (an unknown name gives `[]`), keeping only those with `agent_check` true and with either no `when_exists` or `(worktree / when_exists).exists()`. Keep the order and drop duplicates by name.
  - **`lanes._self_checks`:** `SELF_CHECKS_NONE` (equal to `OLD_LINT_LINE`) when `agent_checks` is empty. Otherwise exactly:

    ```
    Before you write the result, run each of these checks once, after your last edit. They are the runner's own build gates for this step: run them from the worktree root exactly as written, one Bash call per line.

    ```sh
    <run of each gate, one per line>
    ```

    Fix what they report and run the failing one again. If one is denied, skip it and say so in your summary: do not stop as `blocked` for it. Do not run any other gate or the whole suite: the runner runs every build gate right after you, and a failure comes back to this session with its output.
    ```
  - **`_values`:** `values["self_checks"] = _self_checks(cfg, step, Path(run.state.get("worktree") or ctx.repo) if run is not None else ctx.repo)`.
  - **`task.md:19` and `task_next.md:10`:** replace the sentence after "…if the repository has a formatter." with ` {self_checks}`, keeping the single space.
  - **`agentflow-build` SKILL.md:**
    - Step 4: "Format what you changed if the repository has a formatter, then run the checks the prompt lists for you, if it lists any."
    - Line 21: "Never run the whole suite, or a gate the prompt does not list for you. The runner's gates run them right after you, and a failure comes back to this session with its output."
  - **Changelog (Changed):** "The build prompts list a step's `agent_check` gates for the implementer to run before it finishes; with none, they read as before."

- [ ] **Step 4: Run Step 2's command and `agentflow/tests/test_portability.py`.** Expected: PASS.

- [ ] **Step 5: Commit**: "List a build step's agent-check gates in its prompt, so the implementer fixes lint failures in the same session."

---

### Task 3: A result may carry `code_map`, and the run keeps it

**Depends on:** none

**Files:**
- Modify: `agentflow/hitl.py`:
  - `StepResult` (line 84)
  - `_parse_facts` (line 106)
  - `read_result` (line 174)
- Modify: `agentflow/state.py`:
  - the constants (line 31)
  - `initial_state` (line 73)
  - a reducer next to `_on_run_fact` (line 478)
  - the reducer table (line 735)
  - the required-fields table (line 779)
- Modify: `agentflow/lanes.py`: `_finish` (line 815)
- Modify: `CHANGELOG.md`
- Test: `agentflow/tests/test_hitl.py`, `agentflow/tests/test_state.py`, `agentflow/tests/test_agent_step.py`

**Interfaces:**
- **Uses:**
  - `hitl.clean_fact(text) -> str`
  - `hitl.FACT_MAX = 300`
  - `state._on_run_fact`'s casefold-dedupe pattern
  - `Run.emit(type, **fields)`
- **Produces:**
  - `hitl.CODE_MAP_PER_RESULT = 8`
  - `StepResult.code_map: list[str]`
  - `state.CODE_MAP_MAX = 40`
  - the event `code_noted {text, key}`
  - `state["code_map"]: list[{"text": str, "key": str}]`, oldest first

- [ ] **Step 1: Write the failing tests.**

```python
# test_hitl.py
def test_result_code_map_is_cleaned_cut_and_capped(tmp_path): ...
    # 12 entries, one with "\u202e" and one of 2000 chars → 8 kept; no "\u202e"; every len <= FACT_MAX
def test_result_code_map_must_be_a_list_of_strings(tmp_path): ...   # {"code_map": "x"} → ResultError
def test_a_result_with_no_code_map_has_an_empty_one(tmp_path): ...

# test_state.py
def test_code_map_starts_empty_and_old_states_still_replay(run): ...
def test_code_noted_appends_dedupes_by_casefold_and_keeps_the_newest(run): ...
    # CODE_MAP_MAX + 3 distinct notes → the last CODE_MAP_MAX kept; a repeat in other case moves to the end
def test_code_noted_needs_its_text_and_key(run): ...

# test_agent_step.py (fake agent writes the result)
def test_a_done_step_records_its_code_map_once(flow_repo): ...
    # result {"status": "done", "code_map": ["`app/names.py:12` `normalise(name: str) -> str`: trims"]}
    # → one code_noted event with key == the step key; run it again with the same entry → no second event
def test_a_stopped_step_records_no_code_map(flow_repo): ...   # status "blocked" with code_map → no code_noted
```

- [ ] **Step 2: Run them and expect failures.** `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_hitl.py agentflow/tests/test_state.py agentflow/tests/test_agent_step.py --import-mode=prepend -q`. Expected: FAIL.

- [ ] **Step 3: Implement.**
  - **`hitl.py`:** generalise `_parse_facts` to `_parse_lines(raw, name: str, cap: int)`, with `facts` and `code_map` both using it, so the error says `"code_map must be a list of strings"`.
  - **`state.py`:** `_on_code_noted` mirrors `_on_run_fact` on `state["code_map"]`, with `CODE_MAP_MAX`. Register `"code_noted": ("text", "key")`.
  - **`lanes.py`:** in `_finish`, before `step_finished` is emitted, call `_remember_code_map(sr, result)`. It emits `code_noted` for each entry whose casefold isn't already in `run.state.get("code_map") or []`. Only `_finish` calls it, so stops never record.
  - **Changelog (Added):** "A step's result may carry `code_map`: up to 8 lines naming existing code (`path:line`, signature, use). The run keeps the newest 40 for every later session."

- [ ] **Step 4: Run Step 2's command.** Expected: PASS.

- [ ] **Step 5: Commit**: "Keep the code map a step's result reports, cleaned and capped, for the rest of the run."

---

### Task 4: Every session sees the code map, and the build and review prompts use it instead of re-reading

**Depends on:** Task 3

**Files:**
- Modify: `agentflow/lanes.py`:
  - `RUN_FACTS_LINE` (line 68), which becomes `EMPTY_SLOT`
  - `_values` (line 97)
  - a new `_code_map` next to `_run_facts` (line 119)
  - `build_prompt` (line 191)
- Modify: `agentflow/config.py`: add `"code_map"` to `PROMPT_PLACEHOLDERS`
- Modify: `agentflow/prompts/_preamble.md`: a `{code_map}` line after `{run_facts}`
- Modify: `agentflow/prompts/_result.md`: one bullet
- Modify: `agentflow/prompts/task.md:13`, `agentflow/prompts/task_next.md:9`, `agentflow/prompts/task_review.md:13`
- Modify: `agentflow/skills/agentflow-build/SKILL.md`: step 1
- Modify: `docs/agentflow-playbook.md`: after the `facts` paragraph (around line 299), plus `CHANGELOG.md`
- Test: `agentflow/tests/test_agent_step.py`

**Interfaces:**
- **Uses:**
  - `_run_facts(run)` and the three prompt tests at `test_agent_step.py:1068-1093`, which are the pattern to copy
- **From earlier tasks:** `state["code_map"]` and the `code_noted` event.
- **Produces:**
  - `lanes._code_map(run: Run | None) -> str`
  - the `{code_map}` placeholder
  - `EMPTY_SLOT = re.compile(r"^\{(run_facts|code_map)\}\n\n", re.MULTILINE)`, which drops each empty slot

- [ ] **Step 1: Write the failing tests.**

```python
def test_without_a_code_map_the_preamble_is_unchanged(flow_repo): ...
    # same assertions as test_without_facts_…; "Code this run has mapped" and "{code_map}" absent
def test_a_run_with_no_code_map_key_builds_the_same_prompt(flow_repo): ...
def test_the_code_map_follows_the_facts_before_the_step(flow_repo): ...
    # emit run_fact "a cause" and code_noted "`app/names.py:12` `f() {step_id}`"
    # → "- a cause\n\n## Code this run has mapped\n\n" + CODE_MAP_LEAD + "\n\n- `app/names.py:12` `f() {step_id}`\n\n## The step"
def test_task_prompts_point_at_interfaces_and_the_map_not_the_plan(flow_repo): ...
    # engine task.md and task_next.md: "do not open the plan again" and "**Interfaces**" in the text;
    # task_review.md contains "code map"
def test_the_result_instructions_describe_code_map(): ...   # _result.md mentions `code_map` and "up to 8"
```

- [ ] **Step 2: Run them and expect failures.** `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_agent_step.py --import-mode=prepend -q`. Expected: FAIL.

- [ ] **Step 3: Implement.**
  - **`_code_map(run)`:** `""` with no entries. Otherwise `"## Code this run has mapped\n\n" + CODE_MAP_LEAD + "\n\n" + "\n".join("- " + text)`, where `CODE_MAP_LEAD = "Earlier sessions of this run found where this code lives. Open it at the line given instead of searching for it, and check a signature before you rely on it."`
  - **`build_prompt`:** drop each empty slot through `EMPTY_SLOT`, in place of the `run_facts`-only drop.
  - **`_result.md`, new bullet after `facts`:** "- `code_map`: optional, in any step that read code. Up to 8 one-line entries for existing code you had to search for and that a later task will likely need, each as `path:line` `signature`: what it is for. Leave out what the plan's Interfaces list already names. Later sessions of this run see these at the top of their prompt."
  - **`task.md:13`** becomes: "The task text above is the plan's, so do not open the plan again. Its **Interfaces** list and the code map at the top of this prompt, when there are any, say where the code you need lives: open those files at those lines, and search only for what neither names. Read the repository's quality rules, and only the spec sections the task cites. Write the failing test first and run it, then write the code, then run the task's tests."
  - **`task_next.md:9`** becomes: "The task text above is the plan's, so do not open the plan again. Use its **Interfaces** list and the code map, and read only the spec sections it cites that you have not read yet. Write the failing test first and run it, then write the code, then run the task's tests."
  - **`task_review.md`:** after line 13, add "The task's **Interfaces** list and the code map at the top of this prompt, when there are any, say where existing code lives: use them before you search."
  - **`agentflow-build` SKILL.md step 1:** "Read the task and the files it lists. Its **Interfaces** list and the run's code map, when there are any, say where the code it uses lives: open those at the line given, and search only for what neither names. Read only the spec sections the task cites."
  - **Playbook:** one paragraph after the `facts` one. It says that any step's result may carry `code_map`, that the runner keeps it as `code_noted` events (newest 40, deduplicated), and that every later session sees it under "Code this run has mapped".
  - **Changelog (Changed):** "Every session's prompt shows the run's code map, and the build prompts stop sending a fresh session back to the plan."

- [ ] **Step 4: Run Step 2's command and `agentflow/tests/test_portability.py` and `agentflow/tests/test_workflow.py`.** Expected: PASS.

- [ ] **Step 5: Commit**: "Show the run's code map in every prompt, and point build and review sessions at it instead of the plan."

---

### Task 5: Plans name the code each task uses (`**Interfaces:**`)

**Depends on:** Task 3 (the plan prompt asks for `code_map`)

**Files:**
- Modify: `agentflow/skills/agentflow-plan/SKILL.md`:
  - the Format section and its example
  - Limits
  - the rule on line 41
- Modify: `agentflow/prompts/plan.md`: line 4 and a new last line
- Modify: `CHANGELOG.md`
- Test: `agentflow/tests/test_sources.py`, `agentflow/tests/test_agent_step.py`

**Interfaces:**
- **Uses:**
  - `sources.parse_plan_text(text) -> list[PlanTask]`
  - `sources.task_files(task) -> list[str]`
  - `sources.plan_batches(tasks, max_tasks, max_files) -> list[list[int]]`
  - `_FILE_LINE` (`sources.py:28`), which matches only Create/Modify/Test/Delete/Rename
- **From earlier tasks:** `code_map` in a result.
- **Produces:** the plan format's `**Interfaces:**` list, with `Uses:`, `From earlier tasks:` and `Produces:` lines.

- [ ] **Step 1: Write the failing tests.**

```python
# test_sources.py
INTERFACES = ("**Interfaces:**\n- Uses: `app/names.py:12` `normalise(name: str) -> str`\n"
              "- From earlier tasks: none\n- Produces: `app/greet.py` `greet(name: str) -> str`\n")
def test_an_interfaces_list_names_no_files_and_changes_no_batch(): ...
    # the same 3-task plan with and without INTERFACES under each **Files:** list:
    # task_files equal, task_size equal, plan_batches(…, 3, 3) equal, depends_on equal

# test_agent_step.py
def test_the_plan_skill_and_prompt_ask_for_interfaces_and_a_code_map(): ...
    # agentflow-plan SKILL.md contains "**Interfaces:**", "Uses:", "path:line"; plan.md contains "`code_map`"
```

- [ ] **Step 2: Run them and expect a failure.** `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_sources.py agentflow/tests/test_agent_step.py --import-mode=prepend -q`. Expected: the skill test FAILs, and the sources test passes as a pin.

- [ ] **Step 3: Edit the plan skill and prompt.**
  - **Format:** after the `**Files:**` bullet, add a bullet. "An `**Interfaces:**` list:
    - `Uses:` each piece of existing code the task calls, as `path:line` and its signature, for example `` `app/names.py:12` `normalise(name: str) -> str` ``. You read this code while planning, so writing it down saves every later session from searching for it.
    - `From earlier tasks:` the names and signatures it takes from tasks before it.
    - `Produces:` the names and signatures later tasks rely on.

    Write `none` for an empty line." Add the list to the example task too, as in `INTERFACES`.
  - **Limits:** "Keep the Interfaces list to what the task really calls: at most 10 lines."
  - **Line 41:** "Say what each task changes and why, not how to type the code. Give a signature wherever a task calls or defines one, in its Interfaces list, and a snippet only where exact text matters."
  - **`plan.md` line 4:** "…with files, an **Interfaces** list (the existing code each task calls, as `path:line` and signature, and what it takes from and gives to other tasks) and exact test commands."
  - **`plan.md`, new last line:** "In your result, put the existing interfaces most tasks will use, at most 8, in `code_map`."
  - **Changelog (Changed):** "The bundled plan skill asks for an `**Interfaces:**` list per task, naming the existing code the task calls by `path:line` and signature."

- [ ] **Step 4: Run Step 2's command and `agentflow/tests/test_portability.py`.** Expected: PASS.

- [ ] **Step 5: Commit**: "Ask plans for an Interfaces list per task that names the existing code it calls by path, line and signature."

---

### Task 6: Adopt it in this repository's pack, then run the full suite

**Depends on:** Task 2, Task 4, Task 5

This task edits `.agentflow/**`, which is always forbidden inside an agentflow run. A person does it, or an interactive session outside a run.

**Files:**
- Modify: `.agentflow/config.yaml`: `portability` gets `agent_check: true`
- Modify: `.agentflow/prompts/task.md`, `.agentflow/prompts/task_next.md`: the same line changes as Task 2 and Task 4, keeping this pack's own skill wording

- [ ] **Step 1: Make the three edits.** Then run `bin/agentflow validate` and expect `0 warnings`.
- [ ] **Step 2: Check the build prompt.** Run `bin/agentflow explain feature --module x` and expect the golden output unchanged, then `bin/agentflow run feature --module x --dry-run`. Then render a `task.md` prompt through a test or a REPL and check that the `portability` command appears in the `sh` block.
- [ ] **Step 3: Run the full suite.** `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests --import-mode=prepend -q`. Expected: all pass.
- [ ] **Step 4: Commit**: "Let this repository's implementer run the portability check itself."

---

## Rollout and how we'll know it worked

- **Downstream pack** (the repository whose run gave the evidence): set `agent_check: true` on `floor`, `ruff_check`, `ruff_format` and `pyright`, which are all one line with no `cwd`. Leave `biome` and `types` alone, because they need `cwd`.
- **Measure** the next feature run of 10 or more tasks against the evidence run, with the same transcript classification:
  - **Gate-fix rounds:** `fix_attempt` events per build task, from 5/12 to at most 1/10.
  - **Exploring:** exploring tool calls per first implementer session, median from about 24 to at most 12.
  - **Spec and plan re-reads:** sessions that open the plan file, from 16 to near 0.
- **Not in this slice:**
  - deduplicating `agent_turn` events per message id (the 2.5× turn-count inflation)
  - wider batching (`sources.plan_batches`)
  - `agentflow init` proposing `agent_check` for detected lint and type gates
