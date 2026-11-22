# Throughput slice T5 (Parallel build) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Plan tasks that the plan marks `**Parallel group:**`, and that the runner proves independent, are built together. One wave session fans them out to concurrent `Task` sub-agents. The runner then attributes the files to each task, gates the wave once, reviews it as a wave and commits each task on its own, in plan order.

**Architecture:**
- **The plan proposes and the runner disposes.** `sources.py` parses `**Parallel group:** <name>`. The pure module `agentflow/wave.py` decides which members of a group form a wave (§4.4.2), checks the wave's reports, attributes changed paths to members and checks the sub-agent dispatch. A member that fails a check is `parallel_demoted` and runs alone, as today.
- **The wave lifecycle is a new mixin.** `agentflow/wave_lane.py` (`WaveLane`, mixed into `lanes.Executor` beside `FeatureLane`) runs the wave through stages. Each stage is idempotent, keyed on its events, so a resumed runner picks the wave up where it stopped:
  1. `wave_started`, with the members and the start `HEAD` and tree.
  2. The runner writes one card file per member. Each card is the member's own rendered `task.md` (with its brief and lessons) plus `_wave_member.md`.
  3. `<prefix>/wave-<n>/implement`: one `run_agent_step` session with `task_wave.md` and no gates. It dispatches one sub-agent per card in one message.
  4. The dispatch check. Then attribution (`wave_attributed`): stray paths go back to the wave start (`scope_violation`), and members that reported `blocked`/`needs_human`, sent no report or strayed are set aside (`parallel_demoted`).
  5. The `build` gates, once, on the combined tree. On a red result, the wave session is reopened with the gate report, and its own fix loop runs.
  6. Projection. Each kept member gets a `step_started`/`step_finished` on its own `task-<m>/implement` key, so all of today's per-task code (review rounds, `_gates_hold`, the commit message, parking) works on it unchanged.
  7. `<prefix>/wave-<n>/review`: one read-only session that fans out one reviewer per member. Its findings are recorded as each member's round-1 `review_result`.
  8. Each kept member goes through `_run_task` in plan order: the fix rounds and re-reviews of today, then one commit holding only its own files.
- **A wave session is never resumed or salvaged.** Any stop of a wave's implement session (and a session the runner was killed in) **dissolves** the wave. Its tree goes into a shelf ref, the paths go back to the wave start, and every member runs alone (`wave_dissolved`). The one exception is a guard stop where `HEAD` moved: that stays the run's wait, as in T4. A review-wave stop that isn't a guard trip is **dropped** (`review_wave_dropped`), and each member gets its own review session.
- **Other members' files are held.** `FeatureLane._held_paths` gains the files that other uncommitted members of the task's wave own. Because of that, a member's commit, park and shelf take only its own files.

**Tech Stack:** Python 3.12 stdlib, PyYAML, pytest. `fake_agent.py` gains a concurrent sub-agent act. No new dependency.

**Spec:** `docs/specs/agentflow-throughput-proposal.md`. This plan covers slice **T5** (§8):
- §4.4.1–§4.4.7 (Lever D), except §4.4.9 (runner-level worktrees, which is later);
- §4.3.2's review wave, deferred from T3, with profile L keeping per-task review sessions;
- §6's events `wave_started`, `parallel_demoted` and `scope_violation`, and the KPI `agent_parallelism`;
- §10 decisions 1, 2, 4 and 6;
- the "T5" verification.

Out of scope: Codex waves (with `agents.codex` every task runs alone); slots and multi-run (T6); the dashboard's group-by-wave view; raising the engine default of `parallel.max_subagents` to 3 (that happens after 5 green runs, per decision 4).

## Global Constraints

- Engine dependencies stay stdlib + `pyyaml>=6`. No project strings in the engine (`test_portability.py` `DENYLIST`), and that includes the new prompts.
- The golden files `agentflow/tests/fixtures/explain/*.txt` stay byte-identical. Wave lines appear in `explain` only when `parallel.max_subagents > 1`.
- **Off means today.** With `parallel.max_subagents: 1` (the engine default), nothing is written that wasn't written before: no `**Parallel group:**` effect, no new event, unchanged batches, and an empty `{parallel_candidates}`.
- **Waves run only where they can:**
  - the build step's agent is `claude`;
  - it is the first pass, not a refine round;
  - `small_plan` isn't active;
  - the task isn't in a batch. With `max_subagents > 1`, `plan_batches` leaves out grouped tasks.
- **Decision 2:** a task the plan sizer flagged `risk`, or whose files match `overlays.security.when_paths` (`overlays.touches_security`), never joins a wave, and it keeps its own fresh review session.
- **Decision 6:** the runner never puts tasks in a group. It only points out `parallel_candidates` to `plan_review`.
- **The engine floor holds:**
  - the runner is the only committer, and a sub-agent's commit is the existing `agent_commit` guard;
  - `.agentflow/**` and `forbidden_paths` are refused for wave sessions as for any step;
  - conformance is unchanged (`reviewed` is `step:branch_review`, and `verified` is the final full gate run).
- A wave session's `max_budget_usd` is the role's cap × its member count. Its timeout is `defaults.step_timeout_min + WAVE_TIMEOUT_MARGIN_MIN` (15). It never rolls over: a rollover stops it, and that stop dissolves the wave.
- Sub-agents inherit the wave session's model. The cards say "omit `model`". The dispatch check accepts a sub-agent whose `model` is `inherit` or the wave's exact model, and whose description starts `Task <n>:` (or `Review Task <n>:`) for a member `n`. Anything else is `guard_tripped` with kind `wave_dispatch`, and the wave dissolves.
- Model text shown to a person or written into a commit message (report summaries, sub-agent descriptions) goes through `hitl.clean_fact`.
- No engine prompt text is dropped or reworded. `plan.md`, `plan_review.md` and the `agentflow-plan` skill only gain lines, so no `pack_fix.py` `Retired` entry is needed.
- `agents.py` is on the security overlay's paths. Review Task 5 with the `security-and-hardening` skill and the `security-auditor` agent.
- New and changed events:

  | Event | Fields | `state.REQUIRED` |
  |---|---|---|
  | `wave_started` (new) | `{key: <prefix>/wave-<n>, wave, tasks, start_head, start_tree, model, effort}` | `("key", "wave")` |
  | `parallel_demoted` (new) | `{key: <task key>, task, reason, wave}`. `reason` is one of `wave.REASONS`. Emitted once per task and reason | `("key",)` |
  | `wave_attributed` (new) | `{key, wave, owners: {n: [paths]}, out: {n: reason}, strays: [paths], shelf}` | `("key",)` |
  | `scope_violation` (new) | `{key: <task key or wave key>, task, path, wave}` | `("key", "path")` |
  | `wave_dissolved` (new) | `{key, wave, tasks, reason, shelf}`. It clears a wait under `key`, and sets the `<key>/implement` row to `skipped` | `("key", "wave")` |
  | `review_wave_dropped` (new) | `{key, wave, reason}`. It clears a wait under `<key>/review`, and sets that row to `skipped` | `("key",)` |
  | `wave_finished` (new) | `{key, wave, committed, parked}` | `("key",)` |
  | `parallel_candidates` (new) | `{key: <prefix>, pairs}`, once per run, only with waves on | `("key",)` |
  | `step_started`, `step_finished`, `review_result` | `wave` on the rows a wave projects | unchanged |
  | `subagent_started` / `subagent_finished` | `description` (cut to 200 characters); `finished` also gets `cost_usd` | unchanged |
  | `effort_decided` | `source: "wave"` for a wave's own steps | unchanged |
- Every new config key ships in the task that adds it, with all four companions. Each user-visible change adds a line under `## [Unreleased]` in `CHANGELOG.md`.
- Tests run as `uv run --no-project --with pyyaml --with pytest pytest <path> --import-mode=prepend -q`. Run `uvx ruff@0.17.4 check .` before each commit. Never create or edit `uv.lock`.

## Review Focus

1. **A runner killed while a wave session runs, or after its result but before attribution.** On resume, the session must not be resumed, and nothing may be committed twice. A wave whose implement step started but isn't `done` is waited for (`wait_for_agents`) and dissolved (`reason: interrupted`). A wave whose session is `done` but has no `wave_attributed` is attributed from the recorded `wave_started` start. (Task 9: `test_a_runner_killed_mid_wave_dissolves_it_on_resume` and `test_a_runner_killed_before_attribution_attributes_on_resume`.)
2. **A member's single-task fix session edits another uncommitted member's file.** The edit goes back to the tree from before the fix and is recorded as `scope_violation`, so the other member's commit holds only its own work. (Task 8: `test_a_member_fix_that_strays_is_reverted`.)
3. **A member parks at review while another member's work is uncommitted.** Its shelf holds only its own files, and the other member still commits with its files intact. (Task 9: `test_a_member_parked_at_review_shelves_only_its_own_files`.)
4. **The wave session ignores the runner.** It dispatches a task that isn't in the wave, picks another model, or writes a file itself. The wave dissolves, its tree is kept in the shelf ref, no commit takes any of its work, and every member runs alone. (Task 9: `test_a_wrong_dispatch_dissolves_the_wave`.)
5. **A member's re-review sees the other members' uncommitted diff.** Its range is cut to its own files: `task_start_sha` is `<sha> -- <files>`, so the prompt's `git diff {task_start_sha}` shows only that member. (Task 8: `test_a_members_review_range_holds_only_its_files`.)

---

### Task 0: The baseline and the human pre-flight

**Depends on:** none

- [ ] **Step 1: Check the baseline.** Run `bin/agentflow scorecard`. Its header must name a frozen baseline dated before T1's first commit. If there is none, stop and ask the person to run `agentflow scorecard --freeze`. Don't freeze one yourself (spec §6).
- [ ] **Step 2: Ask the person to run the pre-flight with a real `claude -p`** (decision 1), as `! <command>` so its output lands in the session:

  ```bash
  time claude -p --output-format stream-json --verbose --permission-mode bypassPermissions \
    "In ONE message, dispatch two Task sub-agents with descriptions 'Task 1: a' and 'Task 2: b'. Each runs \`sleep 20; date +%s\` with Bash and replies with the number. Then reply with both numbers." \
    > /tmp/agentflow-t5-preflight.jsonl
  ```

  Then answer three questions from `/tmp/agentflow-t5-preflight.jsonl`:
  1. Are both `Task`/`Agent` `tool_use` blocks in one `assistant` line, with the two `date` values within 5 s of each other and `real` under 35 s?
  2. Do the sub-agents' own `assistant` lines carry `parent_tool_use_id` (Task 5's per-task cost depends on it)?
  3. Does the tool's input schema (`"type":"system","subtype":"init"`, its `tools`) take an effort field?
- [ ] **Step 3: Record the answers** as one line under this step: `Result: concurrent=<yes|no>, parent_ids=<yes|no>, effort_field=<yes|no>`.
  - If `concurrent=no`, **stop the slice** and tell the person: the spec sends the pack to runner-level worktrees (§4.4.9).
  - If `parent_ids=no`, drop `cost_usd` from Task 5 and say so in its commit message.
  - With `effort_field=yes`, nothing changes in T5. Note it for a later slice: the same-model rule stays.

  Result (2026-11-22, `claude-opus-5-5`): concurrent=yes, parent_ids=yes, effort_field=no.
  - Both `Agent` tool uses came in one assistant message, which the CLI streams as two lines with the same message id. Both `date` values are `1795313458`, and `real` is 37.2 s: one 20 s sleep, since both ran at once, plus CLI start-up and the model turns. That is over the plan's 35 s line, but the equal timestamps settle it.
  - The init event lists tool names only. The `Agent` input carried `description`, `prompt`, `subagent_type` and `run_in_background` (false), and no effort field.

---

### Task 1: `**Parallel group:**` in plans

**Depends on:** none

**Files:**
- Modify: `agentflow/sources.py`
- Modify: `agentflow/prompts/plan.md`
- Modify: `agentflow/skills/agentflow-plan/SKILL.md`
- Test: `agentflow/tests/test_sources.py`

**Interfaces:**
- Produces, in `sources.py`:
  - `PlanTask.group: str | None = None`: the stripped text after `**Parallel group:**` on its own line, outside a code fence, cut to 40 characters. A value of `none` (any case), or an empty one, gives `None`.
  - `_GROUP = re.compile(r"^\*\*Parallel group:\*\*\s*(.*?)\s*$")`.

- [ ] **Step 1: Write the failing tests** in `test_sources.py`:

```python
def test_a_task_names_its_parallel_group():
    tasks = parse_plan_text("### Task 1: A\n\n**Depends on:** none\n**Parallel group:** B\n- [ ] x\n")
    assert tasks[0].group == "B"

def test_no_group_line_or_none_is_no_group():   # no line; `**Parallel group:** none`; an empty value
    assert [t.group for t in parse_plan_text(text)] == [None, None, None]

def test_a_group_line_in_a_code_fence_is_not_read():
    assert parse_plan_text(fenced)[0].group is None
```

- [ ] **Step 2: Run them and see them fail.** Run `… pytest agentflow/tests/test_sources.py --import-mode=prepend -q`. Expected: `AttributeError: … 'group'`.
- [ ] **Step 3: Implement the field.** Parse it in `parse_plan_text` the way `_depends_on` reads its line, over `body[1:]` and skipping fenced lines. Then add the instruction:
  - in `plan.md`, after the `**Depends on:**` paragraph: tasks that can be built at the same time get the same `**Parallel group:** <letter>` line under `**Depends on:**`. Such tasks depend on none of each other, share no file, change no contract the others use, and each one names its files. The runner checks the files and dependencies itself and may still build them one at a time;
  - in the skill's Format list, one bullet with the same rule, and the line in the example.
- [ ] **Step 4: Run them and see them pass,** together with `test_portability.py` and `test_explain_golden.py`.
- [ ] **Step 5: Commit.** `git commit -m "Read a plan task's Parallel group line and ask the planner to mark tasks that can be built together."`

---

### Task 2: Forming a wave (`wave.py`, part 1)

**Depends on:** Task 1

**Files:**
- Create: `agentflow/wave.py`
- Test: `agentflow/tests/test_wave.py`

**Interfaces:**
- Consumes: `sources.PlanTask` (with `group`), `sources.task_files`, `guards.path_matches`.
- Produces, in `agentflow/wave.py` (pure: it reads no file, emits nothing and has no clock):
  - `REASONS = ("no_files", "serial_path", "risk", "security", "depends_on_member", "dependency_open", "shared_files", "model", "dissolved", "blocked", "needs_human", "no_report", "scope_violation")`;
  - `@dataclass(frozen=True) class Demotion: task: int; reason: str`;
  - `depends_transitively(by_n: Mapping[int, PlanTask], start: int, target: int) -> bool`;
  - `group_candidates(tasks: Sequence[PlanTask], at: PlanTask, skip: Collection[int]) -> list[PlanTask]`: `at`, then the open tasks after it in plan order with `at.group`, minus those in `skip`. It returns `[]` when `at.group` is None;
  - `form_wave(candidates, by_n, *, committed: Collection[int], serial_paths: Sequence[str], risky: Collection[int], security: Collection[int], model_of: Mapping[int, str], cap: int) -> tuple[list[int], list[Demotion]]`. It checks in candidate order, and each candidate gets the first reason that applies:
    1. `no_files`;
    2. `serial_path`;
    3. `risk`;
    4. `security`;
    5. `depends_on_member` (an accepted member is reachable through `depends_on`);
    6. `dependency_open` (a direct dependency not in `committed`);
    7. `shared_files` (with an accepted member);
    8. `model` (different from the first accepted member's).

    It stops adding at `cap` members, and the rest aren't demoted. If `candidates[0]` fails, the result is `([], [its demotion])`. A wave of one is `([], demotions of the others)`;
  - `candidates(tasks, serial_paths) -> list[tuple[int, int]]`: pairs `(a, b)` with `a < b`, where both are open and ungrouped, both name files, the files are disjoint, neither touches a serial path, and neither depends transitively on the other;
  - `candidates_text(pairs) -> str`: `""` for none. Otherwise one line per pair, at most 10 lines, `- Tasks 6 and 7 could share a **Parallel group:** line: no dependency between them and no shared file.` It starts with the lead line `The runner found tasks with no dependency and no shared file between them. Check whether they change the same contract before suggesting a group:`;
  - `predict_waves(tasks, cap, serial_paths, security: Collection[int]) -> list[list[int]]`. It walks in plan order, treating every task before the one it is at as committed. This is for `explain`.

- [ ] **Step 1: Write the failing tests** in `test_wave.py`. Build the tasks with `sources.parse_plan_text`:

```python
def test_two_independent_grouped_tasks_form_a_wave():   # 2 and 3 in group A, Depends on: Task 1, disjoint files
    assert form_wave(group_candidates(tasks, t2, ()), by_n, committed={1}, **open_cfg) == ([2, 3], [])

@pytest.mark.parametrize("case, reason", [("shares src/a.py", "shared_files"), ("depends on Task 2", "depends_on_member"),
    ("names no file", "no_files"), ("touches migrations/0001.sql", "serial_path"), ("depends on open Task 4", "dependency_open")])
def test_a_member_failing_the_check_is_demoted(case, reason):
    assert form_wave(...) == ([], [Demotion(3, reason)])

def test_a_risky_or_security_task_never_joins():   # risky={3}; then security={3}
    assert form_wave(...)[1] == [Demotion(3, "risk")]

def test_the_cap_leaves_extras_for_the_next_wave():   # 4 grouped, cap 3
    assert form_wave(..., cap=3) == ([2, 3, 4], [])

def test_a_failing_first_candidate_forms_no_wave():
    assert form_wave(...) == ([], [Demotion(2, "no_files")])

def test_candidates_are_ungrouped_independent_disjoint_pairs():
    assert candidates(tasks, []) == [(6, 7)]
    assert candidates_text([]) == ""

def test_predict_waves_for_explain():
    assert predict_waves(tasks, 2, [], set()) == [[2, 3]]
```

- [ ] **Step 2: Run them and see them fail.** Expected: `ModuleNotFoundError: agentflow.wave`.
- [ ] **Step 3: Implement the functions** in `wave.py`. A serial path matches with `guards.path_matches(path, glob)`.
- [ ] **Step 4: Run them and see them pass,** with `test_portability.py`.
- [ ] **Step 5: Commit.** `git commit -m "Decide which tasks of a parallel group form a wave, and which ungrouped tasks could."`

---

### Task 3: Reports, attribution and the dispatch check (`wave.py`, part 2)

**Depends on:** Task 2

**Files:**
- Modify: `agentflow/wave.py`
- Test: `agentflow/tests/test_wave.py`

**Interfaces:**
- Produces, in `wave.py`:
  - `@dataclass(frozen=True) class Report: n: int; status: str; summary: str; files: tuple[str, ...]; tests_run: tuple[str, ...]`;
  - `REPORT_STATUSES = ("done", "blocked", "needs_human")`;
  - `parse_reports(raw: object, members: Collection[int]) -> dict[int, Report]`:
    - `raw` is the result JSON's `tasks` value;
    - an entry is kept only when `n` is a member, `status` is in `REPORT_STATUSES`, `summary` is a string, and `files` and `tests_run` are lists of strings (missing means `[]`);
    - a member named twice gets no report;
    - anything that isn't a list gives `{}`.

    It fails closed: a member with no report is set aside (`no_report`);
  - `parse_review_reports(raw: object, members) -> dict[int, list[dict]]`: entries `{n, findings: [dict, ...]}`, under the same rules;
  - `is_test_path(path: str) -> bool`: the basename matches `test_*`, `*_test.*`, `*.test.*` or `*.spec.*`;
  - `allowed_dirs(task: PlanTask) -> set[str]`: the directories of the task's declared files that are test paths;
  - `@dataclass(frozen=True) class Attribution: owners: dict[int, list[str]]; strays: dict[str, int | None]`;
  - `attribute(changed: Sequence[str], members: Sequence[PlanTask], reports: Mapping[int, Report], plan_rel: str) -> Attribution`. For each changed path, in this order:
    1. a declared file of exactly one member makes it that member's;
    2. a test path in one member's `allowed_dirs` makes it that member's;
    3. if several members' `allowed_dirs` hold it, it goes to the one member whose report names it;
    4. otherwise it is a stray. A stray is charged to the first member, in plan order, whose report names it, else `None`. `plan_rel` is always a stray;
  - `TASK_DESCRIPTION = re.compile(r"^\s*(?:Review\s+)?Task\s+(\d+)\s*:")`;
  - `dispatch_violations(subagents: Sequence[Mapping], members: Collection[int], model: str) -> list[str]`. Each entry is one human-readable line, for a description that names no member, or a `model` that is neither `inherit`, missing, nor equal to `model`.

- [ ] **Step 1: Write the failing tests:**

```python
def test_reports_fail_closed():   # duplicate n, unknown status, n not a member, files not a list, tasks not a list
    assert parse_reports([{"n": 2, ...}, {"n": 2, ...}, {"n": 9, ...}], {2, 3}) == {}
    assert parse_reports("x", {2}) == {}

def test_attribution_by_declared_files_and_test_dirs():
    att = attribute(["src/a.py", "tests/test_a_extra.py", "README.md"], [t2, t3], reports, "docs/plans/p.md")
    assert att.owners == {2: ["src/a.py", "tests/test_a_extra.py"], 3: []}
    assert att.strays == {"README.md": None}

def test_a_stray_is_charged_to_the_member_that_reports_it():
    assert attribute(["src/z.py"], [t2, t3], {3: Report(3, "done", "", ("src/z.py",), ())}, "p.md").strays == {"src/z.py": 3}

def test_a_test_file_in_a_shared_dir_goes_to_the_member_that_reports_it():
    ...

def test_the_plan_file_is_always_a_stray():
    ...

def test_dispatch_violations():
    assert dispatch_violations([{"description": "Task 2: x", "model": "inherit"}], {2, 3}, "sonnet") == []
    assert dispatch_violations([{"description": "Task 5: x", "model": "inherit"}], {2, 3}, "sonnet")
    assert dispatch_violations([{"description": "Task 2: x", "model": "opus"}], {2, 3}, "sonnet")
```

- [ ] **Step 2: Run them and see them fail.**
- [ ] **Step 3: Implement them.**
- [ ] **Step 4: Run them and see them pass.**
- [ ] **Step 5: Commit.** `git commit -m "Read a wave's reports, attribute its changed paths to members, and check its sub-agent dispatch."`

---

### Task 4: The parallel keys

**Depends on:** none

**Files:**
- Modify: `agentflow/defaults/config.yaml` (a `parallel:` block, `skills.map.build_wave`, and `L: {wave_review: false}`)
- Modify: `agentflow/config.py` (`Parallel`, `Config.parallel`, `RouteProfile.wave_review`, `_build_route`, `_Validator.parallel()`, and `PROMPT_PLACEHOLDERS`)
- Modify: `agentflow/config_edit.py`
- Modify: `docs/agentflow-playbook.md` (§14)
- Modify: `CHANGELOG.md`
- Test: `agentflow/tests/test_config.py`
- Test: `agentflow/tests/test_config_edit.py`

**Interfaces:**
- Produces:
  - `@dataclass class Parallel: max_subagents: int = 1; serial_paths: list[str] = field(default_factory=list)`, and `Config.parallel: Parallel`;
  - `PARALLEL_MAX = 6`;
  - `RouteProfile.wave_review: bool = True`;
  - `cfg.skills_map["build_wave"]`, which defaults to `agentflow-build`;
  - `PROMPT_PLACEHOLDERS` gains `wave_title`, `wave_cards`, `allowed_files` and `parallel_candidates`.
- Engine defaults: `parallel: {max_subagents: 1, serial_paths: []}`. The engine's `route.profiles.L` becomes `{wave_review: false}`.

- [ ] **Step 1: Write the failing tests:**
  - in `test_config.py`:
    - `validate` refuses `parallel.max_subagents: 7` (`at most 6`), `0`, `true` and `"3"`, and accepts `1` and `6`;
    - it refuses `serial_paths: "x"` and `[""]`, and an unknown `parallel.foo`;
    - it refuses `route.profiles.M.wave_review: "no"`;
    - it **warns** when `max_subagents > 1` and the build phase's agent isn't `claude` (`waves need the claude agent`);
    - it **warns** when `max_subagents > 1` and the pack's own `prompts/plan.md` doesn't contain `Parallel group` (`plans written with this prompt never form a wave`);
    - `bin/agentflow validate` on this repository's pack still prints 0 warnings.
  - in `test_config_edit.py`:
    - `parallel.max_subagents` and `parallel.serial_paths` save and round-trip as form fields;
    - an edit to `skills.map.build_wave` and to `route.profiles.L.wave_review` is `Refused`, since both sit under protected blocks.
- [ ] **Step 2: Run them and see them fail.**
- [ ] **Step 3: Implement it.**
  - The form fields:
    - `(("parallel", "max_subagents"), "Plan tasks a wave builds at once (1 = off, at most 6)", …)`, declared as an integer the way `watchdog.no_progress_min` is;
    - `(("parallel", "serial_paths"), "Paths that never go in a wave", {"kind": "list"})`.
  - `skills` and `route.profiles` are already in `_PROTECTED_KEYS`, so they are the companion for those two keys. The spec's "Form field" for `skills.map.build_wave` gives way to the existing protection.
  - Playbook §14 gets four lines:
    - `parallel.max_subagents`: 1 is off; at most 6; waves need `claude`;
    - `parallel.serial_paths`: globs, such as migrations, lockfiles, `package.json` and generated clients;
    - `skills.map.build_wave`;
    - `route.profiles.<n>.wave_review`: `false` keeps a fresh review session per task, which is the engine's L.
  - `CHANGELOG.md` gets `Added` lines.
- [ ] **Step 4: Run them and see them pass.** Also run `bin/agentflow validate` (expect "0 warnings"), `test_explain_golden.py` and `test_layers.py`.
- [ ] **Step 5: Commit.** `git commit -m "Add parallel.max_subagents, parallel.serial_paths, skills.map.build_wave and a route profile's wave_review, off by default."`

---

### Task 5: Sub-agent descriptions and cost, and concurrent sub-agents in the fake agent

**Depends on:** none

**Files:**
- Modify: `agentflow/agents.py` (`StreamAccumulator`, `parse_claude_line`)
- Modify: `agentflow/tests/fake_agent.py`
- Test: `agentflow/tests/test_agents.py`

**Interfaces:**
- Produces:
  - `subagent_started` gains `description: str` (the `Task` input's `description`, cut to 200 characters, `""` when it is missing);
  - `subagent_finished` gains `description` and `cost_usd: float`. The cost is `estimate_cost(acc.prices, …)` over the `assistant` lines whose `parent_tool_use_id` is that call's id, kept in `StreamAccumulator.subagent_usage: dict[str, dict[str, tuple[str, dict]]]`;
  - the fake-agent act `parallel_subagents: [{description, model?, subagent_type?, write?: {path: text}, output_tokens?}]`. It writes:
    1. **one** `assistant` line holding every `Agent` `tool_use` block;
    2. then, for each sub-agent, one `assistant` line with `parent_tool_use_id` set to its id, carrying usage, with its `write`s applied;
    3. then one `user` line holding every `tool_result`.

    Document the act in the module docstring's act list.

- [ ] **Step 1: Write the failing tests** in `test_agents.py`:

```python
def test_two_task_calls_in_one_message_start_before_either_finishes():   # fake act parallel_subagents with two entries
    names = [n for n, _ in events if n.startswith("subagent_")]
    assert names == ["subagent_started", "subagent_started", "subagent_finished", "subagent_finished"]
    assert [p["description"] for n, p in events if n == "subagent_started"] == ["Task 2: a", "Task 3: b"]

def test_a_subagents_cost_comes_from_its_own_usage_lines():
    finished = [p for n, p in events if n == "subagent_finished"]
    assert finished[0]["cost_usd"] > 0 and finished[0]["cost_usd"] != finished[1]["cost_usd"]   # different output_tokens
```

- [ ] **Step 2: Run them and see them fail.**
- [ ] **Step 3: Implement it.** Keep today's sequential `subagents` act exactly as it is: existing tests pin it.
- [ ] **Step 4: Run them and see them pass,** together with the whole of `test_agents.py` and `test_agent_step.py`.
- [ ] **Step 5: Commit.** `git commit -m "Record each sub-agent's description and estimated cost, and let the fake agent run sub-agents concurrently."`

---

### Task 6: What a wave step needs from the step lifecycle

**Depends on:** none

**Files:**
- Modify: `agentflow/config.py` (`Step.wave_size`)
- Modify: `agentflow/lanes.py`:
  - `_set_caps`, `_timeout_s` and `_agent_call`;
  - `_rollover`;
  - `run_agent_step`'s choice of turn;
  - `_continue_prompt`;
  - `build_prompt`.
- Modify: `agentflow/hitl.py` (`WAVE_STOP`, and the notify rule in `raise_hitl`)
- Modify: `agentflow/state.py` (the reducers and `REQUIRED` for `wave_dissolved` and `review_wave_dropped`, and `REQUIRED` for the rest of the new events)
- Test: `agentflow/tests/test_agent_step.py`
- Test: `agentflow/tests/test_state.py`
- Test: `agentflow/tests/test_hitl.py`

**Interfaces:**
- Produces:
  - `Step.wave_size: int = 0`, set at run time only (like `continuation`) and never read from config: the member count of a wave step;
  - `Executor._timeout_s(self, wave: bool = False) -> int`, which adds `WAVE_TIMEOUT_MARGIN_MIN = 15` minutes when `wave` is set;
  - with `wave_size`, `_set_caps` multiplies `sr.budget_usd` by `wave_size`, and `_rollover` calls `self._stop(sr, "max_rollovers", "A wave session never rolls over: its members run alone.")` and returns None;
  - `_continue_prompt(sr, answers, approved, result: bool = True)`: with `result=False` it leaves out `_result_partial`;
  - in `run_agent_step`, a step that is resuming with **no session** and whose last `_approved(since, key)` is a `step_reopened` gets `_Turn(f"{self._continue_prompt(sr, {}, reopened, result=False)}\n\n{self._prompt(sr)}", None)`. This is how a wave member's review fix reaches it. Every other path is unchanged;
  - `build_prompt(..., card: bool = False)`: with `card`, `_result.md` is left out and the rest is built as for a fresh session with no brief pin;
  - in `hitl.py`, `WAVE_STOP = re.compile(r"/wave-\d+/(implement|review)$")`. `raise_hitl`'s default notify is quiet for a key `WAVE_STOP` matches;
  - in `state.py`:
    - `_on_wave_dissolved`: if the wait's key is `ev["key"]` or under it, clear the wait, then set row `<key>/implement` to `skipped` with `skip_reason` `wave dissolved: <reason>`;
    - `_on_review_wave_dropped`: the same for `<key>/review`.

- [ ] **Step 1: Write the failing tests:**
  - `test_a_wave_step_gets_the_wave_timeout_and_budget`. With `wave_size=3` and `session_budget_usd: {build: 2}`, the call's `max_budget_usd` is `6.0`, and its timeout is `step_timeout_min*60 + 900`.
  - `test_a_wave_step_never_rolls_over`. A `usage_pct: [95]` act stops the step as `max_rollovers`, with no second session.
  - `test_a_reopened_step_with_no_session_gets_the_note_in_a_fresh_prompt`. Emit `step_started` and `step_finished` for the key with no `agent_started`, then `step_reopened` with `note="fix F1"`. The next call has no `--resume`, and its prompt starts with `fix F1`.
  - `test_a_card_prompt_has_no_result_contract`.
  - In `test_state.py`, `test_wave_dissolved_clears_its_wait_only`: a wait on `build/build/wave-1/implement` clears, and a wait on `build/build/task-2/implement` stays.
  - In `test_hitl.py`, `test_a_wave_stop_raises_quietly`.
- [ ] **Step 2: Run them and see them fail.**
- [ ] **Step 3: Implement it.**
- [ ] **Step 4: Run them and see them pass,** with `test_agent_step.py`, `test_state.py`, `test_hitl.py` and `test_explain_golden.py`.
- [ ] **Step 5: Commit.** `git commit -m "Give a wave step its budget, timeout and no rollover, a reopened step with no session its note, and quiet wave stops."`

---

### Task 7: The wave in the plan walk: session, attribution, gates, projection and commits

**Depends on:** Task 3, Task 4, Task 5, Task 6

**Files:**
- Create: `agentflow/wave_lane.py`
- Create: `agentflow/prompts/task_wave.md`
- Create: `agentflow/prompts/_wave_member.md`
- Modify: `agentflow/lanes.py` (add `WaveLane` to `Executor`'s bases)
- Modify: `agentflow/feature_lane.py` (`_plan_pass`, `_batches`, `_held_paths`, and the module docstring)
- Modify: `agentflow/sources.py` (`plan_batches(..., skip: Collection[int] = ())`)
- Modify: `CHANGELOG.md`
- Test: `agentflow/tests/test_wave_lane.py`
- Test: `agentflow/tests/test_feature_lane.py` (`plan_text(groups=...)`)

**Interfaces:**
- Consumes:
  - `wave.group_candidates`, `form_wave`, `parse_reports`, `attribute` and `dispatch_violations`;
  - `gitops.shelf_ref`, `shelve` and `restore_paths`;
  - `gates.run_gates_reusing`, `record_results`, `gates_ok` and `failure_report`;
  - `run_control.wait_for_agents`;
  - `lanes.build_prompt(card=True)`;
  - `Step.wave_size`.
- Produces, in `wave_lane.py` (`class WaveLane`, relying on the same `Executor` attributes `FeatureLane` names):
  - `WAVE_PROMPT = "task_wave.md"`, `MEMBER_RULES = "_wave_member.md"`;
  - `waves_on(self, phase: Phase, step: Step, small: dict) -> bool`. All of these must hold: `cfg.parallel.max_subagents > 1`, the phase's agent is `claude`, it isn't a refine round, and `small_plan` isn't active;
  - `wave_at(self, phase, step, plan, rel, prefix, task, tasks, held: set[int], small) -> Literal["none", "ran", "stopped"]`:
    1. If an open wave (`_open_wave`) holds `task`, continue it.
    2. Otherwise, form one at `task`. `skip` = the walk's `held` and the tasks `park.waits_on` holds, the tasks of a dissolved wave, and those set aside by an attribution. `committed` = the tasks with `task_committed`, plus those that aren't open.
    3. Emit each new `parallel_demoted` (once per task and reason). Return `"none"` when no wave of two or more forms.
  - `wave_held(self, tkey: str) -> set[str]`: the paths that the other kept members of `tkey`'s last attributed wave own (`wave_attributed.owners`), for members with no `task_committed` after that event;
  - `wave_owned(self, tkey: str) -> list[str] | None`: the member's owned paths plus its declared files, or None when `tkey` is in no wave.
- Wave keys and steps:
  - `W = f"{prefix}/wave-{n}"`, where `n` = 1 + the number of earlier `wave_started` under `prefix`;
  - the session key is `f"{W}/implement"`;
  - its step is `dataclasses.replace(impl_step, skill=cfg.skills_map["build_wave"], prompt=WAVE_PROMPT, gates=None, review=None, batch=None, small_plan=None, carry=None, task_briefs={}, continuation=False, wave_size=len(members))`;
  - its effort is the highest of the members' `effort_decided` levels, in `cfg.effort.allowed` order, emitted as `effort_decided(source="wave")`. The step runs with `size=False`.
- The cards: `run.agent_dir / "wave" / f"wave-{n}" / f"task-{m}.md"`, written with `safeio.write_text`. Each holds `build_prompt(cfg, member_step, ctx, {}, member_extra, "first", card=True)` followed by `_wave_member.md`, filled with `{task_n}` and `{allowed_files}` (the declared files and their test directories).
- `task_wave.md` and `_wave_member.md` carry the rules of spec §4.4.3:
  - the wave session doesn't edit code;
  - in **one message**, it dispatches one `Task` per card. The description is `Task <n>: <title>`, the prompt is "Read `<card path>` and follow it", and `model` is omitted;
  - it waits for all of them, then writes `result.json` with `status: done` and `tasks: [{n, status, summary, files, tests_run}]`;
  - each sub-agent:
    - edits and formats only its allowed files;
    - runs only its own unit tests: no integration suite, no Docker, no database;
    - never commits;
    - writes no result file. That rule wins over the card above it;
    - ends with its report;
  - placeholders: `{wave_title}` (`Tasks 2, 3`) and `{wave_cards}` (one line per member: `- Task 2: <title>: \`<card path>\``).
- Attribution:
  - `changed = changed_paths(worktree, start_tree, snapshot)`, minus `_held_paths` of parked tasks;
  - before any path moves, the whole tree goes into the shelf `shelf_ref(run.id, W)` (`shelve`);
  - strays, and the owned paths of the members that are set aside, go back to `start_head` (`restore_paths`);
  - then `wave_attributed`, one `scope_violation` per stray, and a `parallel_demoted` per member set aside (`blocked`, `needs_human`, `no_report` or `scope_violation`).
- The gates: `run_gates_reusing(cfg, run, f"{W}/implement", impl_step.gates, …)` on the combined tree. On a red result:
  1. emit `fix_attempt{attempt: 1}` and `step_reopened{key: W/implement, reason: "gates", note}`. The note is the failure report plus "dispatch one fix `Task` per member the failure points to, with the same rules and description";
  2. run the step again with `gates=impl_step.gates`, so its fix loop runs;
  3. attribute again after it, and any new stray dissolves the wave (`scope_violation`).
- The projection: for each kept member without a `done` implement row, emit:
  - `step_started{key: <tkey>/implement, phase, agent, model, effort, mode: "first", start_head, start_tree, task: _task_payload, wave: n}`;
  - `step_finished{key: <tkey>/implement, status: "done", summary: clean_fact(report.summary), tree: snapshot, wave: n}`.
- Then each kept member, in plan order, goes through `_task_outcome(tkey, partial(self._run_task, …))`. Its commit holds only its own files, because `_held_paths` now includes `wave_held(tkey)`. Emit `wave_finished` once every kept member is committed or parked.
- The walk in `_plan_pass`: before the `batch = …` line, when `waves_on`, call `wave_at`:
  - `"stopped"` returns stopped;
  - after `"ran"`, refresh the state. If `task` is committed, move on as the existing code does after a commit (`carry_prev = None`). If it is parked, add it to `held`. Otherwise (it was set aside), fall through and run it alone.
- In `_batches`, when `max_subagents > 1`, `skip` is the grouped tasks.

- [ ] **Step 1: Write the failing tests** in `test_wave_lane.py`:
  - **Fixture.** The python fixture pack, configured with `parallel: {max_subagents: 2}`. The plan comes from `plan_text(count=3, depends={1: "none", 2: "Task 1", 3: "Task 1"}, groups={2: "A", 3: "A"}, files={1: ["src/one.py"], 2: ["src/two.py"], 3: ["src/three.py"]})`.
  - **The wave's script.** The act for `build/build/wave-1/implement` is `parallel_subagents` writing `src/two.py` and `src/three.py`, with a result whose `tasks` reports both as done.
  - **The tests:**

```python
def test_two_grouped_tasks_build_in_one_wave_session(lane, git):   # the spec's first T5 check
    assert steps(calls) == [IMPL.format(n=1), REVIEW.format(n=1), "build/build/wave-1/implement", REVIEW.format(n=2), REVIEW.format(n=3)]  # review wave: Task 8
    assert started_before_finished(events, 2)   # two subagent_started before either subagent_finished
    assert subjects(git, root)[-2:] == ["Task 2: …", "Task 3: …"]
    assert files_of(git, root, "Task 2: …") == ["docs/plans/…", "src/two.py"]   # only its own files and the plan

def test_the_build_gates_run_once_for_the_wave(lane, git):
    assert [e["key"] for e in gate_results(events, "build")] == ["build/build/wave-1/implement"]  # no gate run on task-2/commit, task-3/commit

def test_a_red_wave_gate_reopens_the_wave_session(lane, git):   # gate fails once, the wave's fix act fixes it
    assert sessions_of(flow_repo, run, "build/build/wave-1/implement")[0] == sessions_of(...)[1]   # resumed, not fresh

def test_a_grouped_task_sharing_a_file_runs_alone(lane, git):   # 3 also modifies src/two.py
    assert demoted(events) == [(3, "shared_files")]
    assert "build/build/wave-1/implement" not in steps(calls)

def test_a_member_writing_outside_its_files_is_reverted_and_runs_alone(lane, git):   # sub-agent 3 also writes src/zz.py
    assert scope_violations(events) == [(3, "src/zz.py")]
    assert not (root / "src/zz.py").exists() or "src/zz.py" not in files_of(git, root, "Task 2: …")
    assert IMPL.format(n=3) in steps(calls)

def test_a_blocked_member_is_set_aside_and_the_other_commits(lane, git):   # report 3 blocked; then its alone run blocks too
    assert "Task 2: …" in subjects(git, root)
    assert any(e["type"] == "step_parked" and e["key"].startswith("build/build/task-3/") for e in events)

def test_a_risky_task_never_joins_and_keeps_its_own_review(lane, git):   # plan-sizer answers risk: yes for task 3
    assert demoted(events) == [(3, "risk")]
    assert REVIEW.format(n=3) in steps(calls)

def test_max_subagents_1_changes_nothing(flow_repo, git):   # same plan and groups, parallel off
    assert not [e for e in events if e["type"] in ("wave_started", "parallel_demoted", "parallel_candidates")]

def test_codex_builds_every_task_alone(...):
```

  `IMPL` and `REVIEW` are `test_feature_lane`'s key templates (`IMPL.format(n=2)`). Add `groups: dict[int, str] | None` to `test_feature_lane.plan_text`, writing `**Parallel group:** <g>\n\n` after the `Depends on` line.
- [ ] **Step 2: Run them and see them fail.** Run `… pytest agentflow/tests/test_wave_lane.py --import-mode=prepend -q`.
- [ ] **Step 3: Implement `wave_lane.py`, the two prompts, and the walk changes.** Each stage first checks its own event, so it is safe to call again. Update the `feature_lane.py` module docstring with a "Waves" paragraph.
- [ ] **Step 4: Run them and see them pass,** with `test_feature_lane.py`, `test_refine.py`, `test_sizing_lane.py`, `test_portability.py` and `test_explain_golden.py`.
- [ ] **Step 5: Commit.** `git commit -m "Build a parallel group's independent tasks in one wave session that fans them out to sub-agents, then gate the wave once and commit each task alone."`

---

### Task 8: The review wave and member-scoped reviews

**Depends on:** Task 7

**Files:**
- Create: `agentflow/prompts/task_review_wave.md`
- Create: `agentflow/prompts/_wave_reviewer.md`
- Modify: `agentflow/wave_lane.py` (`_review_wave`)
- Modify: `agentflow/feature_lane.py` (`_review_task`)
- Test: `agentflow/tests/test_wave_lane.py`

**Interfaces:**
- Consumes: `wave.parse_review_reports`, `autonomy.blocking_of`, `route.applied`, `FeatureLane._route_drops_review`, and `wave_owned` (Task 7).
- Produces:
  - `REVIEW_WAVE_PROMPT = "task_review_wave.md"`, `REVIEWER_RULES = "_wave_reviewer.md"`.
  - The members to review, `R`: the kept members whose review `_route_drops_review` doesn't drop (for each one it does drop, emit `review_skipped` as today).
  - The review wave runs only when **all** of these hold:
    - `len(R) >= 2`;
    - `profile.wave_review` is true for the applied profile, or there is no applied profile;
    - no `f"{tkey}/review"` row of `R` has started.
  - Its session:
    - key `f"{W}/review"`, step `dataclasses.replace(step.review, prompt=REVIEW_WAVE_PROMPT, wave_size=len(R))`;
    - effort: the highest of the members' review `effort_decided` levels;
    - cards: `…/wave-<n>/review-<m>.md`, each holding the member's rendered `task_review.md` (`card=True`, with the scoped extras below) followed by `_wave_reviewer.md`. The reviewer writes nothing, and its final message is `{"n": m, "findings": [...]}`;
    - result: `{status: done, summary, findings: [], tasks: [{n, findings}]}`.
  - After it is `done`, for each member that has a parsed report and no round-1 `review_result` in `_round_events`, emit `review_result{key: <tkey>/review, round: 1, findings, blocking, blocking_findings, tree, wave: n}`. A member with no report gets nothing, so `_review_round` gives it a fresh session of its own. That is the fallback.
  - Any other stop of the review wave emits `review_wave_dropped`, and every member falls back the same way. A guard stop is the exception: it stays the run's wait.
  - `_review_task` for a wave member (`wave_owned(tkey)` isn't None):
    - `task_start_sha = f"{sha} -- {' '.join(shlex.quote(p) for p in files)}"`;
    - `diff_range = f"{sha}..worktree, limited to {', '.join(files)}"`;
    - around `_fix_round`: take `before = snapshot(worktree)`. Afterwards, every path changed since `before` that isn't in `wave_owned(tkey)` and isn't a test path in `wave.allowed_dirs` goes back to `before` (`restore_paths(worktree, paths, before)`), with one `scope_violation{key: tkey, task, path}` each.

- [ ] **Step 1: Write the failing tests:**

```python
def test_one_review_wave_reviews_both_members(lane, git):
    assert "build/build/wave-1/review" in steps(calls) and REVIEW.format(n=2) not in steps(calls)
    assert {e["key"] for e in review_results(events)} == {REVIEW.format(n=2), REVIEW.format(n=3)}

def test_a_blocking_finding_reopens_only_its_member(lane, git):   # wave review: F1 on 3, none on 2
    assert IMPL.format(n=3) in steps(calls) and REVIEW.format(n=3) in steps(calls)          # fix in a fresh single-task session, then re-review
    assert "F1" in prompt_of(calls, IMPL.format(n=3))
    assert subjects(git, root)[-2:] == ["Task 2: …", "Task 3: …"]

def test_a_members_review_range_holds_only_its_files(lane, git):   # Review Focus 5
    assert "-- src/three.py" in prompt_of(calls, REVIEW.format(n=3))

def test_a_member_fix_that_strays_is_reverted(lane, git):   # Review Focus 2: 3's fix also edits src/two.py
    assert scope_violations(events)[-1] == (3, "src/two.py")
    assert files_of(git, root, "Task 2: …") == [..., "src/two.py"] and two_py_is_the_wave_version

def test_a_missing_reviewer_report_falls_back_to_its_own_review(lane, git):   # tasks lists only n=2
    assert REVIEW.format(n=3) in steps(calls)

def test_profile_L_keeps_a_review_session_per_task(lane, git):   # route.mode on, applied L
    assert "build/build/wave-1/review" not in steps(calls) and {REVIEW.format(n=2), REVIEW.format(n=3)} <= set(steps(calls))
```

- [ ] **Step 2: Run them and see them fail.**
- [ ] **Step 3: Implement it.** Update Task 7's first test's expected steps to the review wave.
- [ ] **Step 4: Run them and see them pass,** with `test_feature_lane.py`, `test_refine.py` and `test_route*.py`.
- [ ] **Step 5: Commit.** `git commit -m "Review a wave's members in one session that fans out a reviewer per task, with each member's range cut to its own files."`

---

### Task 9: When a wave fails: dissolve, resume and park

**Depends on:** Task 8

**Files:**
- Modify: `agentflow/wave_lane.py` (`_dissolve`, the dispatch check, and the resume rules)
- Test: `agentflow/tests/test_wave_lane.py`

**Interfaces:**
- Produces: `_dissolve(self, W: str, reason: str) -> bool`, where False means the wave stays the run's wait. In this order:
  1. When `head(worktree) != runner_head(run)`, add the wait to `handled_waits`, call `_tell_once`, and return False.
  2. If the tree changed since `start_tree`, write the shelf to `shelf_ref(run.id, W)`.
  3. `restore_paths` every path changed since `start_tree` back to `start_head`.
  4. Emit `wave_dissolved{key: W, wave, tasks, reason, shelf}`, which clears the wait, and `parallel_demoted{reason: "dissolved"}` for each member.

  A crash between any two of these repeats them on resume, with the same outcome.
- Dissolve reasons:
  - any stop of `W/implement` (its kind);
  - `wave_dispatch`, checked on the `subagents` of the wave sessions' rows after each `run_agent_step` returns `done`, with one `guard_tripped{key: W/implement, violations: [{kind: "wave_dispatch", detail}]}`;
  - `scope_violation`, from the attribution after a fix;
  - `interrupted`: `wave_at` finds a `step_started` for `W/implement` that isn't `done`. It calls `wait_for_agents(run, cfg, key, self._timeout_s(wave=True))` first.
- A review wave found started but not `done` is `review_wave_dropped{reason: "interrupted"}`.

- [ ] **Step 1: Write the failing tests:**

```python
def test_a_wave_session_stop_dissolves_it_and_members_run_alone(lane, git):   # wave act exit 1 / fix_attempts_exhausted
    assert dissolved(events) == [("build/build/wave-1", "fix_attempts_exhausted")]
    assert run.state["waiting"] is None or not run.state["waiting"]["key"].startswith("build/build/wave-")
    assert {IMPL.format(n=2), IMPL.format(n=3)} <= set(steps(calls)) and shelves(git, root) == ["refs/agentflow/shelf/<run>/build.build.wave-1"]

def test_a_wrong_dispatch_dissolves_the_wave(lane, git):   # Review Focus 4: description "Task 7: x", or model "opus"
    assert any(e["type"] == "guard_tripped" and e["violations"][0]["kind"] == "wave_dispatch" for e in events)
    assert "Task 2: …" in subjects(git, root) and files_of(git, root, "Task 2: …") == [..., "src/two.py"]  # rebuilt alone

def test_a_moved_head_keeps_the_wave_stop(lane, git):   # the wave act git_commits
    assert run.state["waiting"]["key"] == "build/build/wave-1/implement" and not dissolved(events)

def test_a_runner_killed_mid_wave_dissolves_it_on_resume(lane, git):   # Review Focus 1: step_started, no outcome, then a fresh executor
    assert dissolved(events)[-1][1] == "interrupted" and no_resume_of_the_wave_session(calls)

def test_a_runner_killed_before_attribution_attributes_on_resume(lane, git):   # step_finished done, then a fresh executor
    assert len([e for e in events if e["type"] == "wave_attributed"]) == 1 and commits_once_each(git, root)

def test_a_member_parked_at_review_shelves_only_its_own_files(lane, git):   # Review Focus 3: 2's review stays unresolved
    parked = next(e for e in events if e["type"] == "step_parked")
    assert parked["shelved"] == ["src/two.py"]
    assert files_of(git, root, "Task 3: …") == [..., "src/three.py"]

def test_a_dissolved_wave_never_forms_again(lane, git):
    assert len([e for e in events if e["type"] == "wave_started"]) == 1
```

- [ ] **Step 2: Run them and see them fail.**
- [ ] **Step 3: Implement it.**
- [ ] **Step 4: Run them and see them pass,** with `test_feature_lane.py` and `test_pipeline.py`.
- [ ] **Step 5: Commit.** `git commit -m "Dissolve a wave that stops, dispatches wrongly or was interrupted, and build its members alone; keep a moved HEAD the run's wait."`

---

### Task 10: `explain` shows waves, and `plan_review` hears of candidates

**Depends on:** Task 2, Task 4

**Files:**
- Modify: `agentflow/conditions.py` (`_explain_steps`, next to the `per-task review` line)
- Modify: `agentflow/lanes.py` (`_values`: `parallel_candidates`)
- Modify: `agentflow/feature_lane.py` (`run_foreach`: the `parallel_candidates` event)
- Modify: `agentflow/prompts/plan_review.md`
- Modify: `CHANGELOG.md`
- Test: `agentflow/tests/test_conditions.py` (the `explain` tests)
- Test: `agentflow/tests/test_wave_lane.py`

**Interfaces:**
- Produces:
  - in `explain`, after a `foreach: plan_task` step's lines, one `ExplainLine(phase, f"{key}/wave-{i}", "run", f"wave-{i}: tasks 4, 5", model)` per `wave.predict_waves(...)` wave. It appears only when `max_subagents > 1`, the agent is `claude` and a plan exists. `security` there is `overlays.touches_security` per task;
  - `values["parallel_candidates"]`: with `max_subagents > 1`, it is `wave.candidates_text(wave.candidates(...))` for the plan at `worktree / (plan or plan_target)`, and `""` otherwise, or when that file can't be read;
  - in `plan_review.md`:
    - a line `{parallel_candidates}` after `{earlier_findings}`;
    - one bullet under "Look for": "a `**Parallel group:**` whose tasks depend on each other, share a file or change a contract another member uses";
  - `parallel_candidates{key: prefix, pairs}`, emitted once per run by `run_foreach` when waves are on.

- [ ] **Step 1: Write the failing tests:**
  - `test_explain_shows_a_wave`: with groups and `max_subagents: 2`, a line `build/wave-1` with the reason `wave-1: tasks 2, 3`.
  - `test_explain_is_unchanged_with_waves_off_or_codex`.
  - `test_plan_review_hears_of_ungrouped_candidates` (the spec's T5 check): the `plan/plan_review` prompt contains `Tasks 6 and 7 could share`.
  - `test_candidates_are_counted_once`.
- [ ] **Step 2: Run them and see them fail.**
- [ ] **Step 3: Implement it.**
- [ ] **Step 4: Run them and see them pass,** with `test_explain_golden.py`. `git diff --stat agentflow/tests/fixtures/explain` must be empty.
- [ ] **Step 5: Commit.** `git commit -m "Show predicted waves in explain and point plan review at tasks that could share a parallel group."`

---

### Task 11: The parallel signals

**Depends on:** Task 9

**Files:**
- Modify: `agentflow/signals.py`
- Test: `agentflow/tests/test_signals.py`

**Interfaces:**
- Produces, in `signals.py` (pure). Each entry gives its direction, then whether it is a count family:
  - `waves` (`higher`, count): the number of `wave_started` events;
  - `parallel_demoted` (`lower`, count): one per demoted task and reason, leaving out `dissolved`;
  - `scope_violations` (`lower`, count);
  - `waves_dissolved` (`lower`, count);
  - `parallel_candidates` (`lower`, count): the pairs in the run's last `parallel_candidates`;
  - `agent_parallelism` (`higher`, not a count; present only when build had agent time). Over the build window, from `phase_started{build}` to the next `phase_started`, it is:

    (main-session seconds + sub-agent seconds) ÷ wall seconds.

    Main sessions are paired as `metrics.py` pairs them for agent time. Sub-agents are paired from `subagent_started` → `subagent_finished` by `tool_use_id`, using their `ts`.

  Add the counts to `COUNT_FAMILIES`, and describe each key in the module docstring.

- [ ] **Step 1: Write the failing tests:**
  - `test_two_overlapping_subagents_give_parallelism_above_one`. The build lasts 100 s. The main session covers 100 s, and two sub-agents each cover 80 s. Expected: `agent_parallelism == 2.6`.
  - `test_wave_counts`.
  - `test_a_run_with_no_wave_has_zero_counts_and_no_parallelism_key_without_build`.
- [ ] **Step 2: Run them and see them fail.**
- [ ] **Step 3: Implement it.**
- [ ] **Step 4: Run them and see them pass,** with `test_scorecard.py` and `test_metrics.py`.
- [ ] **Step 5: Commit.** `git commit -m "Count waves, demotions, scope violations and dissolves, and measure agent parallelism in build."`

---

### Task 12: The whole slice

**Depends on:** Task 10, Task 11

- [ ] **Step 1: Write the docs.**
  - In the playbook, next to the paragraph on batches (`grep -n 'batch' docs/agentflow-playbook.md`), add a "Waves" paragraph covering:
    - the `**Parallel group:**` line;
    - the runner's checks and `parallel_demoted`;
    - the wave session and its cards;
    - attribution and `scope_violation`;
    - one gate run;
    - the review wave, and profile L;
    - dissolving and the shelf ref `refs/agentflow/shelf/<run>/<prefix>.wave-<n>`;
    - that a wave session is never resumed.
  - Add the new events to the playbook's event list, if it has one.
  - Add one `CHANGELOG.md` line for each user-visible change that isn't in yet.
  - In the spec's §8 table, set T5's Status to `shipped <date> (PR #<n>)` once it merges.
- [ ] **Step 2: Run the full suite.** `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests --import-mode=prepend -q`. Everything passes, and `git diff --stat agentflow/tests/fixtures/explain` is empty.
- [ ] **Step 3: Lint and validate.** `uvx ruff@0.17.4 check .` prints `All checks passed!`, and `bin/agentflow validate` prints "0 warnings".
- [ ] **Step 4: Check against the spec's "T5" verification:**

  | Check | Covered by |
  |---|---|
  | One wave session, two concurrent `subagent_started` | Task 5; Task 7 `test_two_grouped_tasks_build_in_one_wave_session` |
  | Commits `Task 2` then `Task 3`, each with its own files | the same test |
  | A shared file or a dependency → `parallel_demoted`, runs alone | Task 2; Task 7 `test_a_grouped_task_sharing_a_file_runs_alone` |
  | Writing outside its files → `scope_violation`, reverted, reruns alone | Task 7 |
  | A `blocked` member is parked, and the other commits | Task 7 `test_a_blocked_member_is_set_aside_and_the_other_commits` |
  | With `agents.codex` or `max_subagents: 1`, `explain` is unchanged | Task 10 |
  | `risk` → `parallel_demoted{reason: risk}`, alone, its own review session | Task 7 |
  | `parallel_candidates` in `plan_review`'s input | Task 10 |
  | `validate` refuses `parallel.max_subagents: 7` | Task 4 |
- [ ] **Step 5: A step for a person: the measured repository's pack.** `.agentflow/**` is forbidden to a run, so a person makes this edit:
  - set `parallel: {max_subagents: 2, serial_paths: [<migrations>, <lockfiles>, package.json, <generated clients>]}`;
  - add the `**Parallel group:**` paragraph to the pack's `prompts/plan.md` override (the `validate` warning from Task 4 names it);
  - then run `bin/agentflow validate` there and expect "0 warnings".

  Per decision 4 and §8, keep `max_subagents` at 2 until the scorecard is green on 5 feature runs. Compare `agent_parallelism`, `scope_violations`, `waves_dissolved` and the conformance SLO against the frozen baseline.

---

## Notes: accepted trade-offs

- **A failed wave is dissolved whole, not salvaged.** The spec's §4.4.6 lets the finished members go on. But the stops that leave reports behind are gate stops, and there the runner can't tell which member broke the combined tree. A timeout, crash or rollover leaves no report at all. So every member reruns alone, and the alone path's own gates and the T4 park find the faulty one: this is the ladder's "targeted rerun". The cost is one rerun per member, and the `waves_dissolved` signal shows how often it happens.
- **A member that reports `blocked` or `needs_human` is set aside, not parked straight away.** It reruns alone, and its stop there gets T4's whole path: the resolver, the park, the blast radius and the shelf. Parking from the report would need a stop with no session behind it, which neither the resolver nor `approve` can resume.
- **Implement-time escalation isn't per member.** Spec §4.4.4's "first fix attempt is escalated one level" applies to the wave session's own fix loop, because a sub-agent's effort can't be set (unless Task 0 finds that it can).
- **"Test files next to them" means a test-named file in the directory of one of the member's declared test files.** A file several members could claim goes to the member whose report names it; otherwise it is a stray.
- **A wave member's review range is cut through the `{task_start_sha}` value** (`<sha> -- <files>`) rather than a new prompt slot. That way a pack's own `task_review.md` override is scoped too, with no edit.
- **`skills.map.build_wave` and `route.profiles.<n>.wave_review` are protected keys,** because they sit under blocks that are already protected. The spec's "Form field" for `build_wave` gives way to that.
- **The member rows a wave projects (`step_started`/`step_finished` with no `agent_started`)** are what let today's review, commit, park and refine code treat a wave member as an ordinary task. In a refine round, a member is refined alone, in a fresh session (`_fresh_refine`), because it has no session of its own.
- **Security overlay per task, not per run.** A member is excluded when its own files match `when_paths` (`touches_security`). The run-level `security_path` still adds its review agent at branch review.
