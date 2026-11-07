# Slice QW: quick wins for fewer human stops and less wasted spend — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A feature run gets through `build` without a human unless something truly needs one. Five changes get it there:
- the runner no longer passes on environment variables that leak in from the shell that started it;
- what one resolver session finds is remembered by the rest of the run;
- rejecting one task's stop refines only that task, and a task that changes nothing isn't reviewed again;
- the budget has a soft limit that only notifies and a hard limit that stops;
- recommended answers are taken in every phase, except for decisions the docs reserve for a human.

**Why (evidence):** run `20261107-1342-feature-eval` (`customers-support-agentic-ai`) took 3 h 54 min and was still in `build`.
- 42% of that (~99 min) was waiting for a human. 9 of its 17 stops needed one.
- Five resolver sessions found the same cause from scratch: the shell's `.env` database URLs leaked into the run.
- Rejecting one stop reran all of `build`: 13 implement sessions ended `unchanged`, 18 reviews ran (15 of them on an empty diff), and it cost $13.81.
- The run stopped at the $25 budget even though it was progressing normally.
- A plan-phase question with a recommended option still waited for a human, because `plan` is not a `resolve: true` phase.

**Architecture:**
- **Environment.** A new module, `agentflow/run_env.py`, builds the base environment for every agent session and every gate of a run. It is this process's environment minus the names `defaults.env_inherit` drops; `defaults.env` still goes on top. `doctor` reports what it would drop.
- **Run facts.** A resolver's result may carry `facts`. The runner records each one as a `run_fact` event. `state["facts"]` holds them, and `_preamble.md` shows them to every later session through a `{run_facts}` placeholder.
- **Refine scope.** A rejection of a stop inside one plan task records that task as the round's `scope`. The feature lane refines only that task. In a refine round, an implementer that answers `unchanged` with an unchanged tree skips its review and its gates.
- **Two budgets.** `defaults.run_budget_usd` becomes the **soft** limit (notify once, go on). A new key, `defaults.run_budget_hard_usd`, is the **hard** limit (`budget_exceeded`, which a human approves). `state["budget_usd"]` keeps meaning "the limit that stops", and `state["soft_budget_usd"]` is new. Sizing from the plan raises only the soft limit.
- **Recommended answers.** `autonomy.eligible` drops the `phase.resolve` condition for `needs_human`. A new question kind, `boundary`, plus a new key, `autonomy.human_question_kinds` (default `[boundary]`), keep a human's decisions with the human.

**Tech Stack:** Python 3.12, stdlib + `pyyaml`, pytest with `fake_agent.py` and the fixture packs.

**Line numbers** below are as of commit `0de51e1`. Find each site by its name if they have moved.

## Global Constraints

- **Dependencies:** engine dependencies stay stdlib + `pyyaml>=6`.
- **No project strings in the engine:** `agentflow/tests/test_portability.py` passes. Don't name `postgres`, a database variable or any project in engine code, prompts or skills. Write "a database URL" only in tests.
- **Golden files:** `agentflow/tests/fixtures/explain/*.txt` stay byte-identical. No task changes `needs_approval`, a phase's steps or a condition. `git diff --stat agentflow/tests/fixtures/explain/` is empty at the end.
- **Config keys:** every new or changed key ships, in the same task, with:
  - its Settings Form field or a `_PROTECTED_KEYS` entry (`config_edit.py`),
  - its `validate` checks,
  - a `tests/test_config_edit.py` case,
  - its playbook §14 line.

  The keys in this slice:
  - `defaults.run_budget_hard_usd` (new, Form field, user-file key)
  - `defaults.run_budget_usd` (meaning changed to the soft limit)
  - `defaults.task_budget_usd` (engine default changed from 0 to 2)
  - `defaults.env_inherit` (new, protected)
  - `autonomy.human_question_kinds` (new, Form field)
  - `autonomy.answer_recommended` (meaning widened to every phase)
- **Files no task may name:** `.agentflow/**`, `.claude/**` and the explain fixtures are forbidden in runs, and since `d49bec5` a plan task naming one stops `build` before its first task. Pack edits are listed under "After the build (a human)", outside every task.
- **Untrusted text:** dotenv files, result `facts` and question text come from the repository or from a model.
  - Parsers are linear, with no nested quantifiers.
  - Input is cut to a fixed size before matching.
  - Control characters are stripped.
  - Events and notifications record variable **names**, never values.
- **Security path:** Task 1 changes `agents.py`, which is an overlay path. Plan and review it with `security-and-hardening`.
- **Old runs:** a run whose state has no `soft_budget_usd`, `facts` or refine `scope` behaves exactly as today.
- **Commit messages:** one imperative sentence ending with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- **Test command shape:** `uv run --no-project --with pyyaml --with pytest pytest <file>[::<test>] --import-mode=prepend -q`.

## Decisions taken in this plan

1. **Approving a hard-limit stop with no `--budget`** sets the hard limit to `spend + defaults.run_budget_usd`, which is one more soft budget's worth ($25 by default). `--budget <usd>` still sets it exactly. The soft limit is left alone.
2. **The soft notification fires once per soft value.** If plan sizing later raises the soft limit and spend crosses the new value, it fires again. It never stops the run and never needs approval.
3. **Plan sizing never raises the hard limit.** If the estimate (`spend + task_budget_usd × open tasks + 25% of run_budget_usd`) is above the hard limit, the runner sends one notification (`budget_estimate_over_hard`) when `build` starts and goes on.
4. **The engine default for `task_budget_usd` becomes 2.0.** The observed run cost ~$2.4 per task, and this repository's pack sets 1.0.
5. **Dropping dotenv variables is on by default** (`drop_dotenv: [".env"]`). A variable inherited with the **same name and value** as a line in the main checkout's `.env` was almost certainly loaded by an IDE or shell, not meant for the run. `defaults.env` always wins, and `PATH`, `HOME`, `USER`, `LOGNAME`, `SHELL`, `TMPDIR`, `LANG`, `LC_*` and `TERM` are never dropped.
6. **Facts come only from resolver sessions** (keys with `/resolve-`). There are at most 5 per result, at most 300 characters each, and the run keeps the newest 8, deduplicated by normalised text.
7. **Phase-level rejection is unchanged.** So are the runner's own findings rounds (whole phase) and a rejection of a stop that isn't inside a plan task.
8. **A `boundary` question** is one where the repository's docs reserve the decision for a human, such as an "ask first" or "never" boundary. It is never auto-answered while `boundary` is in `human_question_kinds`. A human's answer to it is recorded in the commit body, as an `ambiguity` answer is.

## Review Focus

1. **No value of an environment variable** ever reaches an event, a log line, the inbox or a notification. Only names do. Test: Task 1.
2. **`defaults.env` always wins over a drop.** A variable named in both `drop` and `defaults.env` reaches the session with the pack's value. Test: Task 1.
3. **A scoped refine round never leaves the phase unfinishable.** Out-of-scope steps keep their status and the phase still completes. A later phase-level rejection still refines every task. Test: Task 3.
4. **The hard limit is the only budget stop.** Crossing the soft limit never sets `waiting`. Test: Task 4.
5. **A `boundary` question is never auto-answered** while `boundary` is in `human_question_kinds`, in any phase, parked or not. Test: Task 5.

---

### Task 1: A run's agents and gates no longer inherit leaked environment variables

**Files:**
- Create: `agentflow/run_env.py`
- Modify: `agentflow/agents.py`: `AgentCall` :36 (new field `env_base`), `_CliAgent.run` :443 (`os.environ | self.env(call)` → `call.env_base | self.env(call)`, falling back to `os.environ` when `env_base` is None)
- Modify: `agentflow/lanes.py`: the `AgentCall(...)` construction near :605 passes `env_base=run_env.base(self.cfg)`
- Modify: `agentflow/gates.py`: `run_gates` :135. When `env_base` is None it uses `run_env.base(cfg)`, so every in-run call site (`bug_eval`, `ci_lane`, `feature_lane`, `flow_control`, `lanes`, `sync_lane`) is covered without edits. `init.py` keeps passing its own `env_base`.
- Modify: `agentflow/config.py`: `Defaults` :225 (new `env_inherit: EnvInherit` with `drop: list[str]` and `drop_dotenv: list[str]`), the loader :805, and the validator: `drop` holds names or `fnmatch` globs; `drop_dotenv` holds relative paths with no `..` that don't start with `/`.
- Modify: `agentflow/defaults/config.yaml`: `defaults.env_inherit: {drop: [], drop_dotenv: [".env"]}`
- Modify: `agentflow/config_edit.py`: add `"defaults.env_inherit"` to `_PROTECTED_KEYS` :47
- Modify: `agentflow/doctor.py`: a new check, `inherited environment`, in `run_checks` :256
- Modify: `agentflow/pipeline.py`: emit `env_dropped` (names only) when a run starts or resumes and the dropped set differs from the last one recorded
- Modify: `agentflow/state.py`: reduce `env_dropped` into `state["env_dropped"]`, a list of names
- Modify: `docs/agentflow-playbook.md`: a §14 line for `defaults.env_inherit` (protected), and a row in the §10 / troubleshooting table: "a gate fails only inside a run" → `agentflow doctor`, the `inherited environment` check.
- Test: `agentflow/tests/test_run_env.py` (new), `agentflow/tests/test_gates.py`, `agentflow/tests/test_agents.py`, `agentflow/tests/test_doctor.py`, `agentflow/tests/test_config.py`, `agentflow/tests/test_config_edit.py`

**`run_env.base(cfg) -> dict[str, str]`** and **`run_env.dropped(cfg) -> list[tuple[str, str]]`** (name, reason):
- Main checkout: `pack.main_checkout(Path(cfg.path).parent)`.
- Dotenv parsing (`_dotenv(path) -> dict[str, str]`):
  - read at most 256 KB, and skip anything that isn't a regular file (`os.lstat`; never follow a symlink);
  - one `KEY=VALUE` per line, with an optional leading `export `;
  - a value may be single- or double-quoted; an unquoted value ends at ` #`;
  - a key must `fullmatch` `[A-Za-z_][A-Za-z0-9_]{0,127}`; anything else on a line is ignored.
- A name is dropped when either holds and it isn't in `NEVER_DROPPED`:
  - it matches a `drop` glob;
  - its inherited value equals that name's value in a `drop_dotenv` file.
- Cache per `(cfg.path, the dotenv files' mtime_ns and size)`, so each call is cheap.

- [ ] **Step 1: Write the failing tests** in `test_run_env.py`:
  - a variable with the same name and value as in `.env` is dropped;
  - a variable whose value differs from `.env` is kept (the user set it on purpose);
  - a `drop` glob such as `*_DATABASE_URL` removes the matching names;
  - `NEVER_DROPPED` names survive both kinds of drop;
  - `defaults.env` still sets a dropped name to the pack's value;
  - a symlinked or oversized `.env` is ignored;
  - quoting and `export` forms parse;
  - a 10,000-character line returns quickly.
- [ ] **Step 2: Implement `run_env.py`** and the config dataclass, loader and validate. Run `test_run_env.py` and `test_config.py`.
- [ ] **Step 3: Wire `agents.py`, `lanes.py` and `gates.py`.** Add a test in `test_gates.py`: a gate `run: env` doesn't print a dropped name. Add a test in `test_agents.py`: the argv/env built for a fake agent call has no dropped name and has `AGENTFLOW_RUN`.
- [ ] **Step 4: Add the `env_dropped` event and its reducer.** Test: start a run with `.env` holding `X_URL=a` and `X_URL=a` in the environment. One `env_dropped` event names `X_URL`, the event file never contains `a`, and resuming with the same set emits nothing new.
- [ ] **Step 5: Add the `doctor` check.** It reports `warn`, names the variables and the file they match, and suggests the fix: "they are dropped from runs; to keep one, set it in `defaults.env`". It reports `ok` when nothing is dropped. Test in `test_doctor.py`.
- [ ] **Step 6: Add the `_PROTECTED_KEYS` entry and its `test_config_edit.py` case** (the page refuses `defaults.env_inherit` with the "protected" message), then the playbook §14 line. Run `test_config_edit.py` and `test_portability.py`.
- [ ] **Step 7: Commit.**

### Task 2: What a resolver finds is remembered by every later session of the run

**Depends on:** Task 1

**Files:**
- Modify: `agentflow/hitl.py`: `StepResult` :68 (new `facts: list[str]`), `read_result` :125. `facts` is optional; not a list of strings is a `ResultError`. Each entry is NFC-normalised, has control characters stripped, has whitespace collapsed and is cut to `FACT_MAX = 300`. Only the first `FACTS_PER_RESULT = 5` are kept.
- Modify: `agentflow/autonomy.py`: in `Resolver._run_session` :208, after a `done` outcome, read the resolver session's result and emit one `run_fact` event per new fact (`text`, `key` = the session key). Deduplicate against `state["facts"]` by `casefold` text. Constant `RUN_FACTS_MAX = 8`.
- Modify: `agentflow/state.py`: the `run_fact` reducer appends to `state["facts"]` (`[{text, key}]`) and keeps the newest `RUN_FACTS_MAX`. The default state gets `"facts": []`.
- Modify: `agentflow/lanes.py`: `_values` :95 adds `run_facts`, which is `""` when there are none. Otherwise it is:

  ```
  ## What this run already found

  Earlier sessions of this run established these. Check one before relying on it; do not diagnose it again.

  - <fact>
  ```

  `Executor.stop_report` :886 appends `### Earlier resolver findings in this run`, the last 3 `resolve_finished` summaries for **other** keys, each cut to 600 characters.
- Modify: `agentflow/prompts/_preamble.md`: add `{run_facts}` on its own line just before `## The step`.
- Modify: `agentflow/prompts/_result.md`: document `facts`: "only in a resolver session: up to 5 one-line statements that hold for the whole run (an environment cause, a tool quirk), so later sessions do not rediscover them".
- Modify: `agentflow/prompts/resolve.md`: one line asking for `facts` when the cause is not specific to this step.
- Test: `agentflow/tests/test_autonomy.py`, `agentflow/tests/test_hitl.py`, `agentflow/tests/test_state.py`, `agentflow/tests/test_agent_step.py`

- [ ] **Step 1: Write the failing tests:**
  - `read_result` accepts `facts`, cuts and cleans them, and refuses a non-list;
  - a scripted resolver result with `facts: ["the runner's shell sets X_URL to an unreachable host"]` produces one `run_fact`;
  - the **next** task's prompt (from `fake_agent`'s recorded argv/prompt) contains the fact under `## What this run already found`;
  - the same fact from a second resolver is not added twice;
  - `facts` in a non-resolver step's result are ignored;
  - with no facts, the prompt has no extra heading and no stray blank lines (compare against today's prompt for one step).
- [ ] **Step 2: Implement the schema, the event, the reducer and the placeholder.** Run the four test files.
- [ ] **Step 3: Add the `stop_report` section and its test.** A second resolver's `extra["stop_report"]` holds the first resolver's summary for a different task.
- [ ] **Step 4: Run `test_portability.py` and `test_explain_golden.py`. Commit.**

### Task 3: Rejecting one task's stop refines only that task, and a task that changes nothing is not reviewed again

**Depends on:** Task 2

**Files:**
- Modify: `agentflow/hitl.py`: `reject` :500. When the target is a stop (not a phase) whose key matches `^<phase>/<step>/task-(\d+)(/|$)`, pass `scope = "<phase>/<step>/task-<n>"` to `open_refine`, which puts it on `phase_rejected`. `_how_to_go_on` :340: for such a key the reject line says "sends task N back for a refine round".
- Modify: `agentflow/state.py`: `_on_phase_rejected` :574. With `scope`, only steps whose key starts with `scope + "/"` (or equals it) become `refine_pending`, and `state["refine"]` gets `"scope": scope`. Without `scope`, nothing changes.
- Modify: `agentflow/feature_lane.py`:
  - `_plan_pass` :215: in a refine round with a `scope`, a committed task outside the scope is passed over, as if it were already committed in the round. Check that the out-of-scope steps' statuses keep the phase completable; see `pipeline` and `state` for how a phase's end is decided.
  - `_run_task` :351: in a refine round, once `_task_agent` returns `done` for the implementer, check whether its step status is `unchanged` **and** `snapshot(self.worktree)` equals the `start_tree` of its last `step_started`. If so, emit `review_skipped` (`key`, `reason: "unchanged"`) and go straight to `_commit_task` with `gates=None` (`NothingToCommit` already gives `sha=None`).
  - The module docstring :22 describes both rules.
- Modify: `agentflow/views.py` / `agentflow/dashboard.html`: show `review_skipped` as a skipped review on the task card (an existing "skipped" style).
- Test: `agentflow/tests/test_refine.py`, `agentflow/tests/test_feature_lane.py`, `agentflow/tests/test_state.py`, `agentflow/tests/test_hitl.py`

- [ ] **Step 1: Write the failing tests:**
  - a 3-task plan with tasks committed: rejecting a stop on `build/build/task-2/implement` reruns only task 2's implementer and reviewer;
  - tasks 1 and 3 get no `agent_started`, and the phase still finishes and moves to `verify`;
  - a phase-level `reject <run> build` still reruns all three (existing tests in `test_refine.py` stay green unchanged);
  - in a phase-level refine round, an implementer that answers `unchanged` and leaves the tree alone gets no review session and no gate run, and the task is recorded as committed in the round with `sha: null`;
  - an implementer that answers `unchanged` but changed a file is still reviewed and gated.
- [ ] **Step 2: Implement the scope** in `hitl.reject`, `open_refine`, the reducer and `_plan_pass`. Run `test_refine.py` and `test_state.py`.
- [ ] **Step 3: Implement the skipped review** in `_run_task`. Run `test_feature_lane.py`.
- [ ] **Step 4: Add the page display** and its test in `test_dashboard.py` (the payload carries the skip). Commit.

### Task 4: A soft budget that only notifies and a hard budget that stops

**Depends on:** Task 3

**Files:**
- Modify: `agentflow/config.py`:
  - `Defaults` :225: `run_budget_usd` is documented as the soft limit; new `run_budget_hard_usd: float = 65`.
  - Loader :805: `_optional(defaults, "run_budget_hard_usd", "defaults", (int, float), 65)`.
  - Validator `agents()` :1231: both budgets are finite and `> 0`, and `run_budget_hard_usd >= run_budget_usd`. Each is an error naming the key.
- Modify: `agentflow/defaults/config.yaml`:
  - `run_budget_usd: 25  # soft: notify once when spend crosses it; the run goes on`
  - `run_budget_hard_usd: 65  # hard: the run stops for a human's approval`
  - `task_budget_usd: 2  # the soft budget grows to fit the plan`
- Modify: `agentflow/layers.py`: `USER_KEYS` :26 adds `defaults.run_budget_hard_usd`.
- Modify: `agentflow/state.py`: `start_run` :869 emits `run_started` with `budget_usd = run_budget_hard_usd` and `soft_budget_usd = run_budget_usd`. The reducers for `run_started` and `run_updated` (:174, :184) carry `soft_budget_usd`. The default state gets `"soft_budget_usd": None`.
- Modify: `agentflow/lanes.py`: `_loop` :496, before the hard check:

  ```python
  soft = self.run.state.get("soft_budget_usd")
  if soft and spend >= soft and not self._soft_noted(soft):
      self.run.emit("budget_soft_reached", spend_usd=spend, soft_usd=soft, hard_usd=budget)
      notify_run(self.cfg, self.run, f"spent ${spend:.2f}: past the soft budget ${soft:.2f}; "
                                     f"it goes on and stops at ${budget:.2f}")
  ```

  `_soft_noted(soft)` is true when a `budget_soft_reached` event already has that `soft_usd`. The hard check and its `budget_exceeded` summary are unchanged, except that the summary says "hard budget".
- Modify: `agentflow/feature_lane.py`: `_scale_budget` :170. It raises `soft_budget_usd` (via `run_updated(soft_budget_usd=...)`), never `budget_usd`. If `target > budget_usd` it emits `budget_estimate_over_hard` (`estimate`, `hard`) and notifies once. `budget_scaled` keeps its fields and adds `soft: true`. A run with no `soft_budget_usd` (started before this change) keeps today's behaviour.
- Modify: `agentflow/hitl.py`:
  - `approve` :487: a `budget_exceeded` stop with no `budget` sets the hard limit to `spend + cfg.defaults.run_budget_usd`. `approve` doesn't take `cfg` today, so either pass the soft step from the callers (`flow.py` :385, `dashboard.py` :144) as a new `budget_step` argument, or keep `spend + state["soft_budget_usd"]` when it's set and `spend + budget_usd` otherwise. Pick whichever needs no new import cycle, and test it.
  - `_how_to_go_on` :347: the text names the hard limit and what a plain approve adds.
- Modify: `agentflow/flow.py` :177 (`_spend`), `agentflow/views.py` :239 :300 :676 :732, `agentflow/dashboard.html` :419 :543 :678 :1001, and `feature_lane.render_pr_body` :862: show `spend / soft · hard` when there is a soft budget, and today's `spend / budget` otherwise.
- Modify: `agentflow/config_edit.py` :120:
  - relabel `run_budget_usd` "Soft budget (USD, new runs): notify only";
  - add `(("defaults", "run_budget_hard_usd"), "Hard budget (USD, new runs): the run stops for approval", {"kind": "number", "min": 0, "exclusive_min": True})`;
  - help text for `task_budget_usd`: "grows the soft budget to fit the plan".
- Modify: `docs/agentflow-playbook.md`:
  - the §3 budget paragraph :36;
  - the stop table row :275 (`budget_exceeded`: the hard budget);
  - the user-file key list :393;
  - §14 lines for `run_budget_usd`, `run_budget_hard_usd` and `task_budget_usd`.
- Test: `agentflow/tests/test_agent_step.py`, `agentflow/tests/test_control.py`, `agentflow/tests/test_feature_lane.py`, `agentflow/tests/test_autonomy.py`, `agentflow/tests/test_config.py`, `agentflow/tests/test_config_edit.py`, `agentflow/tests/test_layers.py`, `agentflow/tests/test_state.py`, `agentflow/tests/test_dashboard.py`

- [ ] **Step 1: Write the failing tests:**
  - soft 0.015 and hard 1: spend crosses the soft limit, there is one `budget_soft_reached`, one notification and no `waiting`, and the run finishes;
  - hard 0.015: a `budget_exceeded` stop, as today;
  - a plain approve at the hard stop gives `hard = spend + soft`;
  - `--budget 5` sets exactly 5;
  - plan sizing raises the soft limit only, and an estimate above the hard limit notifies once without stopping;
  - validate refuses hard < soft and non-positive values;
  - the Settings form has the hard field and the user file may set it;
  - `start_run` records both values.
- [ ] **Step 2: Update the existing budget tests**, which set only `run_budget_usd` and expect a stop, to set `run_budget_hard_usd` (`test_agent_step.py::test_budget_exceeded_stops`, `test_control.py` :330 :343 :353, `test_autonomy.py::test_a_budget_stop_is_never_settled`, `test_feature_lane.py` :765-807). Their assertions keep their meaning: each test checks the limit that now stops.
- [ ] **Step 3: Implement config, defaults, layers and state.** Run `test_config.py`, `test_layers.py` and `test_state.py`.
- [ ] **Step 4: Implement the soft notification in `lanes._loop`, the sizing change and the approve rule.** Run `test_agent_step.py`, `test_control.py` and `test_feature_lane.py`.
- [ ] **Step 5: Implement the displays, the Settings field and the playbook lines.** Run `test_dashboard.py`, `test_config_edit.py` and `test_control_cmds.py` (`config show --effective` lists the new key with its layer). Commit.

### Task 5: Recommended answers are taken in every phase, except for decisions the docs give to a human

**Depends on:** Task 4

**Files:**
- Modify: `agentflow/hitl.py`: `QUESTION_KINDS` :29 adds `"boundary"`. If `config.py` needs the tuple for validation, move it to `config.py` and import it into `hitl.py`, so there is one definition and no cycle.
- Modify: `agentflow/config.py`: `AUTONOMY_KEYS` :54 adds `human_question_kinds`. The validator checks that it is a list of known kinds. The default comes from the defaults file.
- Modify: `agentflow/defaults/config.yaml`: under `autonomy:` add `human_question_kinds: [boundary]  # never answered by the runner`.
- Modify: `agentflow/autonomy.py`: `eligible` :130. For `QUESTIONS_KIND`, return `answer_recommended and recommended_answers(questions) is not None and no question's kind is in human_question_kinds`, **before** the `phase.resolve` test. Keep the `RESOLVE_MARK` test first. Other kinds are unchanged. The module docstring says so.
- Modify: `agentflow/lanes.py` :548. Before `park_hitl`, if `eligible(...)` holds for the parked questions, answer them with `answer(self.run, sr.key, answers, AUTO_BY)` and let the step resume instead of parking.
- Modify: `agentflow/autonomy.py` `Resolver.take_over` :181. After an automatic `answer`, `notify_run` one line: "took the recommended answer(s) q1=A in plan/plan; the decision is recorded".
- Modify: `agentflow/feature_lane.py`:
  - `DEFAULT_RECORD` :82 adds `"boundary": "commit_body"`;
  - `render_pr_body` :854 gets a section, "Decisions the runner took", listing each `hitl_answered` with `by == AUTO_BY` (key, question id, option), so they are reviewed with the pull request.
- Modify: `agentflow/prompts/_preamble.md`: the `needs_human` rule adds, "Use kind `boundary` when the repository's docs reserve the decision for a human (an 'ask first' or 'never' rule); the runner then always asks a human. Otherwise the runner takes your recommended option, so make it the safe one."
- Modify: `agentflow/prompts/_result.md`: the kinds list adds `boundary`.
- Modify: `agentflow/config_edit.py` :141: help text for `answer_recommended` ("in every phase, except kinds in human_question_kinds"), and a new Form field `(("autonomy", "human_question_kinds"), "Question kinds only a human answers", {"kind": "list", "options": list(QUESTION_KINDS)})`.
- Modify: `docs/agentflow-playbook.md`: §14 lines for `autonomy.answer_recommended` (now every phase) and `autonomy.human_question_kinds`, and the HITL section on question kinds.
- Test: `agentflow/tests/test_autonomy.py`, `agentflow/tests/test_hitl.py`, `agentflow/tests/test_feature_lane.py`, `agentflow/tests/test_config.py`, `agentflow/tests/test_config_edit.py`

- [ ] **Step 1: Write the failing tests:**
  - a `plan/plan` `needs_human` with one recommended option per question is answered by the runner (`hitl_answered` with `by: runner`) and the plan step resumes; a `spec_gap` answer still runs `record_step`;
  - the same with one `boundary` question stops for a human and notifies;
  - `human_question_kinds: []` lets the runner answer it;
  - `answer_recommended: false` stops as today;
  - a parked task question with a recommended option is answered, not parked;
  - the PR body lists the runner's decisions;
  - validate refuses an unknown kind;
  - `read_result` accepts `boundary`.
- [ ] **Step 2: Update the existing eligibility tests** in `test_autonomy.py` (around :116 and :477) that assume a non-resolve phase is never eligible for `needs_human`. Keep them for the other kinds.
- [ ] **Step 3: Implement it.** Run the five test files, then `test_portability.py` and `test_explain_golden.py`.
- [ ] **Step 4: Commit.**

### Task 6: Docs and the full suite

**Depends on:** Task 5

**Files:**
- Modify: `CLAUDE.md`, "Engine architecture":
  - one line for `run_env.py` ("the base environment of every agent and gate in a run; `defaults.env_inherit` decides what is dropped");
  - in the `autonomy.py` line, mention run facts and that recommended answers apply in every phase.
- Modify: `docs/agentflow-playbook.md` §10 cheat sheet:
  - "soft budget notifies, hard budget stops";
  - "`reject <run> <task-stop-key>` refines that task only";
  - "`doctor` lists inherited variables a run drops".

- [ ] **Step 1: Make the doc edits.**
- [ ] **Step 2: Run the full suite** (`uv run --no-project --with pyyaml --with pytest pytest agentflow/tests --import-mode=prepend -q`, 25-60 min), `ruff check .` and `bin/agentflow validate` (it must print "0 warnings").
- [ ] **Step 3:** `git diff --stat agentflow/tests/fixtures/explain/` is empty. Commit.

---

## After the build (a human)

These edits are in forbidden paths, so no agent makes them in a run:
- In this repository's pack, `.agentflow/config.yaml` `defaults`, add `run_budget_hard_usd: 65` and reword the `run_budget_usd` comment as the soft limit. Leave `task_budget_usd: 1.0` or raise it to 2. Run `bin/agentflow validate`.
- Optionally, in this pack's `.agentflow/prompts/resolve.md` (which overrides the engine's), add the same `facts` line as Task 2.
- `customers-support-agentic-ai` needs no pack change. It inherits 25 / 65, `task_budget_usd: 2` and `drop_dotenv: [".env"]`. Reinstall its engine with `uv tool install --reinstall --from <this checkout> agentflow`.

## Critical files

`agentflow/run_env.py` (new), `agents.py`, `gates.py`, `lanes.py`, `autonomy.py`, `hitl.py`, `state.py`, `feature_lane.py`, `config.py`, `config_edit.py`, `layers.py`, `doctor.py`, `pipeline.py`, `defaults/config.yaml`, `prompts/_preamble.md`, `prompts/_result.md`, `prompts/resolve.md`, `views.py`, `dashboard.html`, `flow.py`, `docs/agentflow-playbook.md`, `CLAUDE.md`.

## Verification

- The full suite, `ruff check .`, `bin/agentflow validate` with 0 warnings, and unchanged explain golden files.
- **Replay the observed failures with `fake_agent`** in one integration test in `test_feature_lane.py`. Use a 4-task plan:
  - a `.env` whose `X_URL=bad` is also in the environment;
  - a gate that fails when `X_URL=bad`;
  - a plan-phase question with a recommended option;
  - soft 0.01 and hard 10.

  Expected: no stop for a human, one soft notification, one auto-answered question listed in the PR body, and the gate passing.
- **By hand:** `agentflow doctor` in `customers-support-agentic-ai`, from a VS Code terminal, warns about the three database URL names. `agentflow run feature --module <id> --dry-run` shows no change in the printed commands.
