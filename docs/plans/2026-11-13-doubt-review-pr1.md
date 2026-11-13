# Doubt review of the spec and the plan (PR 1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Before a human approves a spec (`define`) or a plan (`plan`), a fresh adversarial reviewer reviews it. Every `critical`, `high` and `medium` finding goes through an automatic, bounded reconcile round, and the approval page shows each finding, its disposition and what is still open.

**Architecture:**
- A new stdlib module, `agentflow/doubt.py`, holds the pure rules: finding numbering, the reconcile note, dispositions, the stop rules, the approval-page and handoff lines, and the "is this a doubt reviewer" test.
- `_Loop.settle` gets one new branch for `needs_approval: true` phases. It opens a **reconcile round**: `phase_rejected` with `kind: "reconcile"`, an explicit `keys` list, and the artifact digests at round start.
- In that round, only the phase's `reconciles: true` step and its `blocks_on_findings` steps run again.
- `flow_control` runs the reconcile step fresh and records `findings_reconciled`. When the artifact did not change, it carries the review over.
- `guards` and `gitops` limit a reconcile step to its `output`.
- The automatic-refinement floor is enforced in code at run time, and `validate` reports the violations.
- The engine defaults add the four steps. This repository's pack gets them by a human edit, because `.agentflow/**` is always forbidden in a run.

**Tech Stack:** Python 3.12 stdlib (`hashlib`, `dataclasses`), PyYAML, pytest with `fake_agent.py`.

**Spec:** `docs/agentflow-doubt-review-proposal.md` (sections 1 to 7 and 9). Section 8 is PR 2 and section 10 is PR 3, each planned after this PR's signals are in. Task 5 changes `agentflow/guards.py` and `agentflow/gitops.py`, which triggers the pack's security overlay: plan and review that task with `security-and-hardening`, and dispatch the `security-auditor` agent at review.

## Global Constraints

- Engine dependencies stay stdlib + `pyyaml>=6`. No third-party import.
- `test_portability.DENYLIST` applies to every new engine file (`doubt.py`, the three prompts, `skills/agentflow-doubt/SKILL.md`):
  - none of them contains `SPEC-`, `src/`, `docs/specs`, `CAPABILITY-MAP`, `CONSTRAINTS.md` or `next-step`;
  - none hard-codes a user-only skill name. The page names `approval_note.human_skill` only when a phase configures one.
- The `explain` goldens (`agentflow/tests/fixtures/explain/*.txt`) stay byte-identical, so the `python` fixture pack's file is not edited. Tests add the four steps through `FlowRepo.configure(edit=...)` and the `with_doubt` helper (Task 2).
- `test_every_pack_validates_clean` stays green after every task. `bin/agentflow validate` on this repository prints `0 warnings` after the human step at the end.
- Every new or changed config key ships with a Settings field or a `_PROTECTED_KEYS` entry, `validate` checks, a `test_config_edit` case, and a playbook §14 line.
- No existing engine prompt text is dropped or reworded, so no `Retired` entry is needed in `pack_fix.py`.
- `verify` and `review` phases behave as today (proposal Q6). There is one exception, the floor in Task 1: a pack with `finding_rounds` absent or 0 now gets 1 round.
- `doubt.py` imports only `config`, `state` and `hitl` at module level. `autonomy.py` imports `flow_control` at module level (`autonomy.py:29`), so `doubt` imports `autonomy` only inside the functions that need it. Otherwise the chain `flow_control` → `doubt` → `autonomy` → `flow_control` is circular.
- Model text shown to a human (finding text, disposition notes) goes through `hitl.clean_fact` and is cut to one line, as other model text on the approval page is.
- Tests run with `uv run --no-project --with pyyaml --with pytest pytest <file> --import-mode=prepend -q`. Never create or touch `uv.lock`.
- Every commit message is one imperative sentence ending with the `Co-Authored-By` trailer from `project.commit_trailer`.

## Decision made while planning (review it)

**The floor is enforced in code, not only by `validate`.** `agentflow run` never calls `validate`; only a mid-run config reload does (`pipeline.py:267`). `autonomy` is also a workflow key, so a pack that writes its own `phases:` (like the node fixture pack) drops the engine's `autonomy` block and reads `finding_rounds` as 0. So, at every use site:
- **Rounds:** the number of rounds is `max(finding_rounds, 1)`, where an absent value counts as 0.
- **Blocking:** `critical`, `high`, `medium`, an empty severity and a missing severity always block, whatever `non_blocking_severities` lists.

`validate` reports an explicit `finding_rounds` below 1, and a list that names any of those severities, as errors. The visible change: a pack whose rounds are absent or 0 gets one runner-opened refine round on blocking findings in its `needs_approval: false` phases before a human sees them. Nothing else in such a pack changes. In particular, `minor` findings still block there, because their list is empty.

## Review Focus

1. **A run resumed on another day.** `plan_target` contains `{date}`, and nothing pins it today (`conditions.py:59`). Expect `start` to pin `params["date"]`, so the reconcile step's allowed path stays the file the `plan` step wrote. Task 5: `test_the_allowed_path_survives_a_resume_on_another_day`.
2. **A human rejects the phase after two reconcile rounds.** Expect the stop rules to count from the last human round, so the human's re-review gets its own rounds, and each round's dispositions to stay with that round. Task 3: `test_rules_restart_after_a_human_round`, `test_ids_are_per_round`.
3. **A stray file left in the tree after an `outside_output` stop.** Expect it never to be committed by the reconcile step's retry or by any later reconcile commit. Task 5: `test_a_stray_file_is_never_committed_on_retry`.
4. **A reconcile step restarted after a human answers its question, or after a runner crash.** Expect "unchanged" to compare against the digest taken when the round opened, never one taken at restart, and a missing `findings_reconciled` to be written before the round settles. Task 4: `test_unchanged_uses_the_round_start_digest`, `test_a_lost_findings_reconciled_is_recovered`.
5. **Malformed dispositions.** These are: a `finding` that is a list, a duplicate id, an id not in the round, a non-string note, and a `human_decision` with no human-answered `boundary` question for that finding in that round. Expect no crash, and each affected finding listed as `unresolved`. Task 3: `test_malformed_dispositions_are_unresolved`.
6. **A reconcile edit that changes what decides the review.** Two examples: a rewrite drops the spec's `Status:` line, so `spec_review`'s `when` turns false; or it adds an Open Question, so `grill_open_questions` becomes due. Expect a stop for a human (`reconcile_changed_conditions`), never a silently "clean" phase. Task 4: `test_a_reconcile_edit_that_skips_the_review_stops`.
7. **A review step nested in a group.** Expect the reconcile round to reset the group too, so the nested review runs again. Task 4: `test_a_nested_review_runs_again`.

---

### Task 1: The config surface and the automatic-refinement floor

**Depends on:** none

**Files:**
- Modify: `agentflow/config.py`:
  - `STEP_KEYS` (63-99): add `"reconciles"`
  - `Step` (370-409): add `reconciles: bool = False`
  - `_Builder.step` (675-724): read it, refusing a non-bool with `ConfigError` ("reconciles must be true or false")
  - `SKILLS_MAP_KEYS` (203-204): add `"doubt"`
  - `_Validator.autonomy` (1811-1842)
  - `_Validator.phases` (1574-1601)
  - `_Validator.step` (1225-): reject `reconciles` on any step that is not top-level in a phase, and on `record_step`, `resolve_step` and `retro_step`
- Modify: `agentflow/autonomy.py`:
  - `is_blocking` (50-53): the floor severities, `""`, and a missing or `null` severity always block
  - `pipeline.py:400`'s round limit goes through a new `autonomy.round_limit(cfg) -> int`. It returns `max(value, 1)` for an `int` value that is not a `bool`, and 1 for anything else (absent, malformed).
- Modify: `agentflow/config_edit.py:160`: the `finding_rounds` Form field becomes `{"kind": "int", "min": 1}`
- Modify: `agentflow/tests/conftest.py`: `FlowRepo.configure` (258-289) gets `edit: Callable[[dict], None] | None = None`, applied as the last statement of `build()`
- Modify: `docs/agentflow-playbook.md` §14:
  - after line 494, one line each for `autonomy.non_blocking_severities` and `autonomy.finding_rounds`, each stating the floor
  - after line 496, one line for `phases.<name>.steps[].reconciles` and one for `blocks_on_findings`
  - `doubt` added to the `skills.map` list (line 451)
- Modify: `CLAUDE.md`: the "Engine floor" bullet gets "automatic refinement of `critical`, `high` and `medium` findings can't be switched off (at least one round)"
- Modify: `docs/agentflow-harness-proposal.md` §1b engine-floor list: one line
- Test: `agentflow/tests/test_config.py`, `agentflow/tests/test_config_edit.py`, `agentflow/tests/test_autonomy.py`

**Interfaces:**
- Produces:
  - `Step.reconciles: bool`
  - `config.FLOOR_BLOCKING = ("critical", "high", "medium")`
  - `autonomy.round_limit(cfg: Config) -> int`
  - `SKILLS_MAP_KEYS` includes `"doubt"`
  - `FlowRepo.configure(patch=None, autopilot=False, batch=False, edit=None)`
- Validate rules, each its own `_Validator` method. "For a lane" means the step's `lanes`, or every lane when it has none (`config.for_lane`). Error unless marked warning:
  - `autonomy.non_blocking_severities` is not a list of strings
  - it names a floor severity (case-insensitive), `""`, `none` or `null`
  - an explicit `autonomy.finding_rounds` below 1
  - a `reconciles` step with `type` other than `agent`, with `role: review`, with no `output`, or without `commit: true`
  - two `reconciles` steps for one lane in one phase
  - a `reconciles` step after a `blocks_on_findings` step of the same lane (top-level order)
  - **warning:** a `reconciles` step in a phase with `needs_approval: false`, or in a phase with no `blocks_on_findings` step (it never runs)
  - **warning:** a `needs_approval: true` phase with a `blocks_on_findings` step and no `reconciles` step (its findings go straight to the human)
  - `skills.map.doubt` goes through the existing `self.skill()`, which already refuses a user-only skill

- [ ] **Step 1: Write the failing tests**

`test_config.py`: `test_validate_doubt_rules(tmp_cfg, patch, level, path, text)`, parametrised in the style of `test_validate_policy_rules` (1399-1422), with one case per rule above. The `reconciles` cases use existing prompts and skills (`prompt: spec.md`, `skill: writing-plans`), so they don't depend on Task 2. Plus:
- `test_reconciles_must_be_a_bool`: `"false"` raises `ConfigError`
- `test_an_absent_finding_rounds_is_not_an_error`: delete `autonomy`; no `autonomy.*` issue

`test_autonomy.py`:
- `test_the_floor_severities_always_block`: with `non_blocking_severities: [minor, medium, "", none]`, `is_blocking` is true for `{"severity": "Medium"}`, `{}`, `{"severity": ""}` and `{"severity": None}`, and false for `minor`
- `test_round_limit_is_at_least_one`: absent, 0, 3, `[1]` and `True` give 1, 1, 3, 1 and 1

`test_config_edit.py`:
- `test_the_form_refuses_switching_refinement_off(shipped)`: the form with `finding_rounds: 0` raises `EditError`. The YAML tab (`propose(text=SHIPPED.replace("finding_rounds: 2", "finding_rounds: 0"))`) has `autonomy.finding_rounds` in `errors`, and the same holds for a list with `high`.
- `test_reconciles_blocks_on_findings_and_the_doubt_skill_are_protected(shipped)`: text that adds `reconciles: true` to a step, flips a step's `blocks_on_findings`, or adds `skills: {map: {doubt: agentflow-review}}` raises `Refused` matching `"protected"`.

- [ ] **Step 2: Run them and confirm they fail**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_config.py agentflow/tests/test_config_edit.py agentflow/tests/test_autonomy.py --import-mode=prepend -q -k "doubt or floor or refinement or reconciles or round_limit or finding_rounds"`
Expected: FAIL (`unknown step key`, and no floor errors).

- [ ] **Step 3: Implement** the items listed under Files and Interfaces.

- [ ] **Step 4: Run the tests and the suites that pin config**

Run: the Step 2 command without `-k`, then `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_layers.py --import-mode=prepend -q`
Expected: PASS, including `test_every_pack_validates_clean`.

- [ ] **Step 5: Commit**

`git commit -m "Add the reconciles step key and the doubt skill map entry, and keep automatic refinement on as an engine floor."`

---

### Task 2: The prompts, the bundled skill and the test helper

**Depends on:** Task 1

**Files:**
- Create: `agentflow/prompts/spec_review.md`, `agentflow/prompts/plan_review.md`, `agentflow/prompts/reconcile.md`
- Create: `agentflow/skills/agentflow-doubt/SKILL.md`: at most 80 lines, frontmatter `name: agentflow-doubt` and `description:`, one `# ` H1 line, no `{skill}`, `{brief}` or `{base_ref}`
- Modify: `agentflow/tests/test_pack.py:385`: `STAGES` adds `"doubt"`
- Modify: `agentflow/tests/test_workflow.py:375`: the review-prompt list adds `spec_review.md` and `plan_review.md`
- Modify: `agentflow/tests/conftest.py`: add `with_doubt(data: dict) -> None`
- Test: `agentflow/tests/test_pack.py`, `agentflow/tests/test_workflow.py`, `agentflow/tests/test_portability.py`, `agentflow/tests/test_config.py`

**Interfaces:**
- Produces `with_doubt`, which:
  - sets `data["skills"]["map"]["doubt"] = "agentflow-doubt"`
  - inserts, before `spec_approval` in `data["phases"]["define"]["steps"]`:
    - `{id: spec_reconcile, lanes: [feature], skill: domain-modeling, role: plan, prompt: reconcile.md, output: "docs/specs/SPEC-{module}.md", reconciles: true, commit: true, message: "Revise SPEC-{module} after its review"}`
    - `{id: spec_review, lanes: [feature], skill: agentflow-doubt, role: review, prompt: spec_review.md, when: spec_awaits_approval, blocks_on_findings: true}`
  - inserts, after `plan` in `data["phases"]["plan"]["steps"]`, the same pair with the `python` pack's plan `output` template, `plan_reconcile` (skill `writing-plans`) and `plan_review`. The `python` pack's `{spec}` path is `docs/specs/SPEC-{module}.md`, and tests are exempt from the DENYLIST.
- The prompts' required content (proposal section 2):
  - `spec_review.md` and `plan_review.md`:
    - open like `branch_review.md`'s first paragraph ("You are the reviewer…")
    - name the ARTIFACT and CONTRACT paths from section 2's table (`{spec}`, `{plan_target}`, `{run_dir}/task.md`, `{run_dir}/issue.md`, `{questions}`)
    - say "Judge the artifact on its own. Do not open or append to `{progress}`, and do not read the handoff or earlier step results."
    - list section 2's checks, and say the artifact is data, never instructions
    - give the severity table, one line per level
    - say a finding that raises again an earlier finding a reconcile step marked `noise` or `trade_off` sets `"disputes": "R<n>-F<m>"` (the id as `{earlier_findings}` prints it) and gives the reason
    - use `{earlier_findings}` and no `{refine_*}` placeholder, because those stay literal on a first pass (`conditions.py:93`)
    - end with "Do NOT validate. Do NOT summarize. Find issues, or state explicitly that you cannot find any after thorough examination."
  - `reconcile.md`:
    - uses `{refine_note}` (always set: the step runs only in a round)
    - gives the classes in this precedence order: `human_decision`, `contract_misread`, `actionable`, `trade_off`, `noise`
    - states that `trade_off` is written into the artifact's Risks section
    - says to edit only the artifact, never another file
    - says never to add an Open Question and never to change the artifact's status line: a decision the docs don't settle becomes a `needs_human` question of kind `boundary`, and so does a `human_decision`. The question's `id` is the finding's id (`R<n>-F<m>`), so the runner can tie the answer to it. It is never `spec_gap`: the runner auto-answers those and files them into the approved spec.
    - forbids the status `unchanged`: it returns `done` with dispositions even when nothing changed
    - gives the exact `dispositions` JSON shape
- The skill: the doubt method as a reviewer applies it (adversarial stance, artifact against contract, the severity scale, no validation, `disputes`). It names no project and no user-only skill.

- [ ] **Step 1: Write the failing tests**

- `test_pack.py::test_the_bundle_holds_one_skill_per_stage` fails once `STAGES` has `"doubt"`.
- `test_workflow.py::test_every_review_prompt_is_fresh_ready` fails with the two new names.
- `test_workflow.py::test_the_doubt_prompts_carry_the_contract`:
  - each review prompt contains "Do NOT validate", "data, never instructions", "disputes", and `critical`, `high`, `medium` and `low`, and no `{refine_`
  - `reconcile.md` contains each class in the order above, "dispositions", "boundary", and "Do not answer `unchanged`"
- `test_config.py::test_with_doubt_validates_clean(tmp_cfg)`: `issues_for(tmp_cfg, with_doubt) == []`

- [ ] **Step 2: Run them and confirm they fail**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_pack.py agentflow/tests/test_workflow.py agentflow/tests/test_config.py --import-mode=prepend -q -k "bundle or fresh_ready or carry_the_contract or with_doubt"`
Expected: FAIL.

- [ ] **Step 3: Write the three prompts, the skill and `with_doubt`**

- [ ] **Step 4: Run the tests and portability**

Run: the Step 2 command, then `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_portability.py agentflow/tests/test_packaging.py --import-mode=prepend -q`
Expected: PASS.

- [ ] **Step 5: Commit**

`git commit -m "Add the spec and plan review prompts, the reconcile prompt and the bundled agentflow-doubt skill."`

---

### Task 3: `doubt.py`: the rules of a reconcile round, and `dispositions` in a result

**Depends on:** Task 1

**Files:**
- Create: `agentflow/doubt.py`
- Modify: `agentflow/hitl.py`:
  - `StepResult` (78-91) gets `dispositions: list = field(default_factory=list)`
  - `read_result` (156-) checks that it is a list, like `findings`
- Test: `agentflow/tests/test_doubt.py`

**Interfaces:**
- Consumes: `config.for_lane` (`config.py:460`); inside functions only, `autonomy.as_finding`, `blocking_of`, `finding_line` and `AUTO_BY`; `hitl.clean_fact`
- Produces, in `agentflow/doubt.py`:
  - `RECONCILE = "reconcile"`
  - `CLASSES = ("human_decision", "contract_misread", "actionable", "trade_off", "noise")`, in precedence order
  - `RECONCILE_ONLY = "runs only in a reconcile round"`
  - `STOP_REASONS = ("no_reconcile_step", "unchanged", "disputed", "theater", "bounded")`, in check order
  - `NOTE_MAX = 300`
  - `@dataclass(frozen=True) class Disposition: finding: str; cls: str; note: str`
  - `def numbered(findings: list, rnd: int) -> list[dict]`: `as_finding` each, then add `"id": "R<rnd>-F<n>"`. Ids are unique for the whole run of a phase, in the note, the prompts, `disputes` and the page alike.
  - `def flagged_keys(phase: Phase, lane: str) -> list[str]`: the `blocks_on_findings` step keys for `lane`, recursing as `autonomy._flagged` does
  - `def with_ancestors(keys: list[str]) -> list[str]`: each key plus every parent key (`a/b/c` → `a/b`). `round_keys` returns this, because a nested review only runs again when its group is reset too (`run_step` returns early for a `done` group).
  - `def reconcile_steps(phase: Phase, lane: str) -> list[Step]`: the top-level `reconciles` steps for `lane`
  - `def round_keys(phase: Phase, lane: str) -> list[str]`: reconcile keys plus `flagged_keys`
  - `def is_doubt_reviewer(cfg: Config, phase_name: str, step: Step, lane: str) -> bool`: `step.blocks_on_findings and step.role == "review"`, and the phase has a reconcile step for `lane`. This is the only test for "withhold author notes" (Task 5), so `task_review`, `branch_review` and `security_review` are never affected.
  - `def in_reconcile_round(state: dict, phase: str) -> bool`
  - `def since_human_round(events: list[dict], phase: str) -> list[dict]`: the phase's events after its last `phase_rejected` whose `kind` is not `RECONCILE`, or after its last `phase_started` when there is none
  - `def reconcile_note(findings: list[dict], history: list[dict]) -> str`: the numbered findings, one `finding_line` each, prefixed with the id, then each earlier round's dispositions in `history`
  - `def parse_dispositions(raw: object, ids: set[str]) -> list[Disposition]`. It keeps an entry only when all of these hold:
    - `finding` is a string in `ids`
    - `class` is in `CLASSES`
    - no other entry names the same id: a duplicated id keeps **no** entry, so the finding is unresolved, as proposal §4 says
    - `note` is a string, run through `clean_fact` and cut to `NOTE_MAX`
  - `def answered_boundary(events: list[dict], key: str, since_seq: int) -> set[str]`: the finding ids with a `hitl_raised` on `key` after `since_seq` (the round's `phase_rejected` seq), holding a question of kind `boundary` whose `id` is that finding id, and a later answer to it whose `by` is not the runner. The answer is the `hitl_answered` event `hitl.answer` emits; check its exact name and fields in `hitl.py` before writing the test.
  - `def unresolved(ids: list[str], dispositions: list[Disposition], answered: set[str]) -> list[str]`: the ids with no kept disposition, plus each `human_decision` whose id is not in `answered`
  - `def artifact_digest(worktree: Path, rel: str) -> str`: SHA-256 hex of a regular file's bytes, read through `safeio.read_bytes(path, ARTIFACT_MAX)` with `ARTIFACT_MAX = 4_000_000`; `""` when missing, a symlink, not a regular file, or over the cap
  - `def stop_reason(cfg: Config, events: list[dict], phase: Phase, lane: str) -> str | None`. It reads only `since_human_round(...)` and returns the first that holds, or None (open another round):
    - `no_reconcile_step`
    - `unchanged`: the last `findings_reconciled` has `before == after`
    - `disputed`: the last review's findings include one whose `disputes` names an id from an earlier round since the last human round, whose disposition was `noise` or `trade_off`. Any other `disputes` value is ignored, and that finding counts as an ordinary finding. So a first review, or a value an untrusted spec steered the reviewer to write, can't end the rounds.
    - `theater`: two or more `findings_reconciled` where every blocking finding got a `noise`, `trade_off` or `contract_misread` disposition. A round with any `actionable`, `human_decision` or unresolved finding doesn't count toward it.
    - `bounded`: the reconcile `phase_rejected` count is at least `autonomy.round_limit(cfg)`
  - `def earlier_for_review(events: list[dict], phase: str) -> str`: every round since the last human round, as `- R<n>-F<m> [<severity>] <finding text> → <class>: <note>` lines, under the lead "Earlier reviews of this artifact found these, and the reconcile step answered as shown. Check each against the artifact as it is now; dispute an answer you think wrong with `disputes`." `""` when there are none. It doesn't reuse `feature_lane.earlier_review`, whose wording is about code.
  - `def page_lines(cfg: Config, run: Run, phase: str, lane: str) -> list[str]`:
    - `[]` when the phase has no reconcile step for `lane`, or its doubt review never finished `done` or `unchanged` in the phase's current walk
    - otherwise one summary line, an optional stop-reason line, then one line per finding per round: `R<n>-F<m> [<severity>] <text> — <class>: <note> (<status>)`
    - status is one of:
      - `fixed`: `actionable`, the artifact changed in that round, and the next review finished without disputing it
      - `claimed`: `actionable` and the artifact changed, with no later review to confirm it
      - `claimed, artifact unchanged`
      - `recorded`, `asked a human`, `still open`, `unresolved`, `disputed`
    - the cross-model sentence names `approval_note.human_skill` only when the note's `when` holds (or is unknown), as `pipeline._approval_note` does
  - `def handoff_risks(cfg: Config, run: Run, phase: str, lane: str) -> list[str]`: the findings still open, the non-blocking findings, and the `trade_off` notes
- Produces, in `hitl.py`: `StepResult.dispositions: list`
- Fixed text:
  - summary line: `Doubt review: {r} review(s) and {n} reconcile round(s), single-model; {a} fixed, {t} trade-off(s) recorded, {o} still open.`
  - then always `A cross-model opinion is yours to run.`, followed by ` For example: /{human_skill}.` when the phase has an `approval_note`
  - stop-reason lines:
    - `unchanged`: "The reconcile step left the artifact as it was, so it was not reviewed again."
    - `disputed`: "The reviewer disputes a disposition: you decide."
    - `theater`: "Two rounds found blocking problems and none was fixed: the review may be validating, not doubting."
    - `bounded`: "The reconcile rounds are used up."

- [ ] **Step 1: Write the failing tests**

Pure tests on fixed event lists, in `test_signals._ev` style:
- `test_numbering_takes_strings_and_any_case`
- `test_flagged_keys_recurse_into_groups`
- `test_is_doubt_reviewer_only_with_a_reconcile_step`: `branch_review` in a `needs_approval: true` phase with no reconcile step gives False
- `test_malformed_dispositions_are_unresolved`: each of a list `finding`, a duplicated id (both entries dropped), an id not in the round, an unknown class, a non-string note, and a `human_decision` whose id is not in `answered`
- `test_answered_boundary_is_per_round_kind_and_finding`: an answer in an earlier round, an answer to a `spec_gap` question, a runner answer, and an answer to another finding's question each leave the id out
- `test_with_ancestors_adds_parent_keys`
- `test_a_dispute_of_an_unknown_id_does_not_stop` and `test_a_first_review_cannot_dispute`
- `test_stop_reason_order`: one event list per reason, and one for None
- `test_rules_restart_after_a_human_round`: two reconcile rounds, then a human `phase_rejected`, then one finding → None, not `bounded`
- `test_theater_shows_at_the_default_limit`: two rounds with `trade_off` only and `finding_rounds: 2` → `theater`, not `bounded`
- `test_ids_are_per_round`
- `test_fixed_needs_a_changed_artifact_and_an_undisputed_review`
- `test_page_lines_are_empty_when_the_review_never_ran`
- `test_page_offers_cross_model_always_and_names_the_skill_only_when_configured`
- `test_doubt_does_not_import_autonomy_at_module_level`: run `python -c "import agentflow.flow_control, agentflow.doubt, agentflow.conditions"` in a subprocess, and assert that `doubt.py`'s module-level imports don't include `autonomy`
- In `test_hitl.py` style: `test_read_result_takes_dispositions`, `test_read_result_refuses_dispositions_that_are_not_a_list`

- [ ] **Step 2: Run them and confirm they fail**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_doubt.py --import-mode=prepend -q`
Expected: FAIL with `ModuleNotFoundError: agentflow.doubt`.

- [ ] **Step 3: Implement `agentflow/doubt.py` and the `StepResult` field**

- [ ] **Step 4: Run the tests**

Run: the Step 2 command, then `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_hitl.py --import-mode=prepend -q`
Expected: PASS.

- [ ] **Step 5: Commit**

`git commit -m "Add the doubt module with the reconcile round's numbering, dispositions, stop rules and page lines."`

---

### Task 4: The reconcile round in the runner

**Depends on:** Task 2, Task 3

**Files:**
- Modify: `agentflow/state.py`:
  - `_in_scope` (645-648) becomes `_in_scope(key: str, scope: str | None, keys: list[str] | None = None) -> bool`. With `keys`, a key is in scope only when it is in `keys` or under one.
  - `_on_phase_rejected` (651-674) passes `ev.get("keys")`, and stores `kind`, `keys` and `digests` in `state["refine"]` **only when the event carries them**, so `state["refine"]` for a human round stays exactly `{"phase", "round", "note"}`. `test_hitl.py`, `test_state.py` and `test_dashboard.py` assert that exact dict.
  - for `kind == "reconcile"` it leaves `row["round"]` unchanged and sets `row["reconcile_round"]`
- Modify: `agentflow/hitl.py` `open_refine` (627-637): for `extra["kind"] == "reconcile"`, the event's `round` is the current human round (not +1), `reconcile_round` is the count since that human round + 1, and the note goes to `refine_note_path(phase, f"{round}-r{reconcile_round}")`. A human's first rejection after reconcile rounds is still round 1.
- Modify: `agentflow/pipeline.py`:
  - `_Loop.settle` (381-404): the new `needs_approval` branch (Step 3)
  - `_review_findings` (612-621): for a phase where `doubt.page_lines` is not empty, show those lines instead of the per-step list
  - `approval_text` (624-654) and `_round_lines` (596-): the "(refine round N)" label reads `row["round"]`, which reconcile rounds no longer bump. "The latest rejection" in `_round_lines` is the latest one that is not `RECONCILE`. The two refine sections show only when `approval_sha` is set.
  - `_first_skill` (437-448) skips a `reconciles` step, as `_should_run` does
  - `_risks` (519-526) adds `doubt.handoff_risks`. `_handoff_text` and `write_handoff` get a `cfg` parameter, and their one caller passes it.
  - the module docstring (10-14)
- Modify: `agentflow/flow_control.py`:
  - `_should_run` (248-262): a `reconciles` step outside a reconcile round returns `(False, None, doubt.RECONCILE_ONLY, None)`
  - `_dry_verdict` (550-560): the same, so `run --dry-run` prints it as skipped
  - `_run_agent` (374-396): see Step 3
- Modify: `agentflow/conditions.py`: in `_step_verdict`, a `reconciles` step is `("skip", RECONCILE_ONLY)` in `explain`
- Modify: `agentflow/metrics.py:168-170`: `_on_phase_rejected` ignores `kind == "reconcile"`, so review and reconcile sessions in a reconcile round are not costed as rework
- Modify: `agentflow/feature_lane.py`:
  - the "Refine rounds" section of `render_pr_body` (~967) lists only rounds that are not `RECONCILE`, plus one line `Doubt review: <n> reconcile round(s) on <phase>` per phase
  - `_open_findings` (927-933) needs no change, because `settle` now emits `step_findings` with `[]` for each clean flagged key of a doubt phase (Step 3), so a finding fixed in round 1 no longer shows as left open
- Test: `agentflow/tests/test_doubt_flow.py`

**Interfaces:**
- Consumes: everything Task 3 produces; `with_doubt` (Task 2); `hitl.open_refine(run, phase, by, note, extra=...)` (`hitl.py:627`), unchanged
- Produces these events:
  - `phase_rejected` with `by: "runner"`, `kind: "reconcile"`, `keys`, `findings` (numbered), and `digests: {"<reconcile key>": "<sha256>"}` taken when the round opens
  - `findings_reconciled` with `key`, `phase`, `round`, `dispositions`, `unresolved`, `before` (from `digests`), and `after`
  - `reconcile_stopped` with `phase`, `round`, `reason` and `open`
  - `doubt_settled` with `phase`, `reviews`, `rounds`, `first_blocking`, `open`, `stop`, `actionable`, `noise` and `misread`, all counted since the last human round. It is emitted when a doubt phase raises its approval **and its doubt review finished (`done` or `unchanged`) since the last human round**, never for a phase whose review was skipped. Task 7's signals read only this event.
  - `reconcile_changed_conditions` with `key` and `changed` (step key → condition label), as a `guard_violation`-style stop for a human
  - `step_unchanged` (existing) for a review that is carried over

- [ ] **Step 1: Write the failing tests**

Each test calls `flow_repo.configure(PIPE, autopilot=True, edit=with_doubt)`. The spec the `define/draft` act writes says `**Status: proposed**`, so `spec_awaits_approval` holds; `test_autonomy.SPEC_TEXT` is `approved` and would make every assertion vacuous. A test that writes the plan computes its path from `make_ctx(...).values["plan_target"]` for the run, never `test_autonomy.PLAN`. `HIGH` is `{"severity": "high", "file": "<spec path>", "line": 3, "text": "…"}`.

- `test_a_low_finding_goes_straight_to_approval`: no `phase_rejected`; `define/spec_reconcile` is skipped with `RECONCILE_ONLY` and never called; the page has the finding; `doubt_settled.rounds == 0`
- `test_a_high_finding_gets_one_reconcile_round`:
  - calls are `define/draft`, `define/spec_review`, `define/spec_reconcile`, `define/spec_review`; there is no second `define/draft` and no `define/contracts`
  - one `phase_rejected` with `kind == "reconcile"`
  - `findings_reconciled.before != after`; the page has `R1-F1` `fixed`
  - the page title has no "refine round"
  - there is no `hitl_raised` before `phase_approval_raised`
- `test_each_of_critical_high_medium_and_no_severity_opens_a_round`: parametrised
- `test_an_unchanged_artifact_is_not_reviewed_again`: `define/spec_review` is called once, and its row is `unchanged`; `reconcile_stopped.reason == "unchanged"`; the page has `R1-F1` `still open` with the note
- `test_unchanged_uses_the_round_start_digest`: the reconcile act edits the spec and then returns `needs_human` (kind `boundary`). After `answer(...)`, the resumed step returns done. `findings_reconciled.before` equals the round's `digests` entry, `before != after`, and the review runs again.
- `test_a_lost_findings_reconciled_is_recovered`: monkeypatch the executor's `_reconciled` to raise once, right after the reconcile step's `step_finished` (a crash), then `advance` again. The event is written before the round's review step starts. When the artifact did not change, that review is carried over with `step_unchanged` and not re-run.
- `test_a_reconcile_edit_that_skips_the_review_stops`: the reconcile act rewrites the spec without its `Status: proposed` line. The run stops with `reconcile_changed_conditions` naming `define/spec_review`, and there is no clean approval.
- `test_a_reconcile_edit_that_makes_grilling_due_stops`: the reconcile act adds an item under `## Open Questions`, and the same stop names `define/grill_open_questions`
- `test_a_nested_review_runs_again`: `with_doubt` plus a pack edit that wraps `spec_review` in a group. The nested review is called twice.
- `test_a_fixed_finding_is_not_left_open_in_the_pr_body`: after "found, fixed, clean", `feature_lane._open_findings` has nothing for `define/spec_review`
- `test_a_human_round_after_reconcile_rounds_is_round_1`: two reconcile rounds, then `reject(...)`. The `phase_rejected.round` is 1, and the page's title says "(refine round 1)".
- `test_rounds_are_bounded`: with `finding_rounds: 2`, three `HIGH` reviews with `actionable` edits give exactly 2 reconcile rounds, then approval with `reason == "bounded"`
- `test_a_disputed_finding_goes_to_the_human`: round 2's review has `"disputes": "F1"`, which ends the rounds with `reason == "disputed"`
- `test_a_human_decision_waits_for_a_human`: under `answer_recommended: true`, the reconcile act's recommended `boundary` question is not answered by the runner. After a human `answer(...)`, the dispositions count it as resolved.
- `test_a_human_rejection_runs_every_step_and_skips_the_reconcile_step`, and it gets fresh rounds after it
- `test_the_plan_phase_reconciles_the_same_way`
- `test_a_phase_without_a_reconcile_step_is_as_before`: `FlowRepo.configure(PIPE, autopilot=False)` with no `edit`, so `verify` and `review` need approval and have flagged steps, and `review/branch_review` returns a blocking finding. Then:
  - there are no `phase_rejected`, `reconcile_stopped` or `doubt_settled` events
  - `step_findings` events appear exactly as today: none for an approval phase
  - the page's `##` section headings are the same list as `phase_approval.show` produces today, with no doubt line
- `test_explain_and_dry_run_skip_the_reconcile_steps`
- `test_reconcile_rounds_are_not_rework`: `metrics.run_metrics(run)["rework_cost_usd"] == 0` after a reconcile round with no human round

- [ ] **Step 2: Run them and confirm they fail**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_doubt_flow.py --import-mode=prepend -q`
Expected: FAIL. The reconcile step runs on the first pass, and no round opens.

- [ ] **Step 3: Implement**

`settle`, for a `needs_approval` phase. **The whole new branch runs only when `doubt.reconcile_steps(phase, lane)` is not empty.** Any other approval phase settles exactly as today, emitting no `step_findings`, `reconcile_stopped` or `doubt_settled`. For a doubt phase:
1. Collect `findings = phase_findings(...)`. Emit `step_findings` for every flagged key that finished, with `[]` for a clean one.
2. If `blocking_of(...)` is not empty and `doubt.stop_reason(...)` is None, call `open_refine(..., AUTO_BY, doubt.reconcile_note(...), extra={"kind": doubt.RECONCILE, "keys": doubt.round_keys(...), "findings": doubt.numbered(blocking, rnd), "digests": {...}})` and `return self.phase(names, index)`.
3. Otherwise, when there are blocking findings and a stop reason, emit `reconcile_stopped`.
4. Emit `doubt_settled` when the doubt review finished since the last human round, then `raise_phase_approval`.

`_run_agent`:
- A `reconciles` step:
  - runs with `mode = "first"`, so a fresh session
  - before it starts, records which steps of the phase would run: the `when`/`unless` verdicts of every step outside `keys`, plus the doubt reviews' own `when`
  - after a `done` outcome, compares those verdicts again. If the edit turned a review's `when` false, or turned another step's from false to true (for example, `grill_open_questions` becoming due), it emits `reconcile_changed_conditions` and stops for a human.
  - then calls `_reconciled(phase, key)`, which emits `findings_reconciled` only if this round has none for `key`. `before` comes from `state["refine"]["digests"]`, `after` from `doubt.artifact_digest`, and `answered` from `doubt.answered_boundary(events, key, <round seq>)`.
- A `blocks_on_findings` step in a reconcile round first calls `_reconciled` for the round's reconcile key (recovery after a crash, before any review runs). If the round's `findings_reconciled` has `before == after`, it emits `step_unchanged` ("the artifact did not change, so the last review stands") and returns `"done"`.
- In a reconcile round, a doubt review's `earlier_findings` is `doubt.earlier_for_review(...)` alone.

- [ ] **Step 4: Run the tests, then the suites that pin today's behaviour**

Run: the Step 2 command, then `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_autonomy.py agentflow/tests/test_state.py agentflow/tests/test_hitl.py agentflow/tests/test_dashboard.py agentflow/tests/test_workflow.py agentflow/tests/test_explain_golden.py agentflow/tests/test_conditions.py agentflow/tests/test_metrics.py agentflow/tests/test_feature_lane.py agentflow/tests/test_conformance.py --import-mode=prepend -q`. If a file in that list doesn't exist, use `grep -l` to find the tests of `render_pr_body` and `run_metrics`.
Expected: PASS with no edits to existing tests, and the goldens unchanged.

- [ ] **Step 5: Commit**

`git commit -m "Open a reconcile round on blocking spec and plan findings before the phase's approval, and show each finding's disposition on the approval page."`

---

### Task 5: The reconcile step edits only its artifact, and the reviewer sees no author notes

**Depends on:** Task 4

Security-sensitive: this task changes `agentflow/guards.py`, `agentflow/gitops.py` and how `lanes.py` commits.

**Files:**
- Modify: `agentflow/guards.py`: `ViolationKind` (24) adds `"outside_output"`
- Modify: `agentflow/gitops.py`. `gitops` imports `guards` (gitops.py:17), so the check lives here and reuses the hardened status reader:
  - new `def outside_only(worktree: Path, only: str) -> list[Violation]`. It reads the tree's state against `HEAD` through `_status_entries` (236-250), with `HARDENED`, `--no-renames`, `--ignore-submodules=none`, untracked files and no ignored ones. It returns:
    - one `Violation("outside_output", path)` per path other than `only`
    - `Violation("outside_output", f"{only} was deleted or is not a regular file")` when `only` is missing, a symlink or not a regular file
    - `Violation("outside_output", f"{only} is empty")` when it has zero bytes
  - `commit(..., paths=...)` (171-) passes the paths as literal pathspecs (`--literal-pathspecs`), so a module-derived name is never read as a glob
- Modify: `agentflow/lanes.py`:
  - for a `reconciles` step, call `outside_only` at step start (before the first turn) and after every turn, next to `check_step` (610-613), through `_fail`
  - `_finish` (900-913) commits a `reconciles` step with `commit(..., paths=[output])`
  - `build_prompt` (225-):
    - first test `step.blocks_on_findings and step.role == "review"`, and only then call `doubt.is_doubt_reviewer(...)`, with phase `extra.get("phase")` and lane `ctx.values.get("lane")`; a missing phase gives False
    - when it holds, set `values["code_map"] = ""` **before** the `EMPTY_SLOT.sub` at line 235, so the slot is dropped whole, and don't prepend `HANDOFF_LINE` (262-271)
- Modify: `agentflow/pipeline.py` `start` (125-): pin `params["date"] = date.today().isoformat()` when absent, before `Run.create`
- Modify: `agentflow/tests/fake_agent.py`: a `delete: [relpath, ...]` act, and one line for it in the docstring
- Test: `agentflow/tests/test_guards.py`, `agentflow/tests/test_doubt_flow.py`

**Interfaces:**
- Consumes: `doubt.is_doubt_reviewer`, `gitops.commit(worktree, message, cfg, paths=..., expected_head=...)` (`gitops.py:171`), `fmt`
- Produces: `gitops.outside_only(worktree, only) -> list[Violation]`. `outside_output` is not `forbidden_path`, so `autonomy.Resolver._revert` never reverts it. It stops for a human as a `guard_violation`.
- Trade-off, recorded in the proposal's Risks: the agent's workspace still mirrors `handoff.md` and `results/*.json` (`workspace.prepare`), so withholding them from a doubt reviewer is wording plus the prompt, as with `branch_review` today. It is not a sandbox.

- [ ] **Step 1: Write the failing tests**

`test_gitops.py`:
- `test_outside_only_names_every_other_path`: a tracked edit, an untracked file, a `git mv` rename and an ignored file. Both rename paths are named, and the ignored file is not.
- `test_outside_only_refuses_a_deleted_empty_or_symlinked_artifact`
- `test_outside_only_ignores_a_planted_fsmonitor`: a repo config `core.fsmonitor` pointing at a script that writes a marker file; the marker is never written
- `test_commit_paths_are_literal`: an artifact named `docs/[ab].md` beside `docs/a.md`; only the first is committed

`test_doubt_flow.py`:
- `test_a_reconcile_step_that_edits_code_is_refused`: `guard_tripped` of kind `outside_output`, no `step_committed`, no `forbidden_reverted`
- `test_a_stray_file_is_never_committed_on_retry`: after that stop, `approve` lets the step retry. The retry stops again at start. Once the stray file is removed by hand, the reconcile commit's file list (`git show --name-only`) is exactly the artifact.
- `test_a_reconcile_step_may_not_delete_its_artifact` (with the new `delete` act)
- `test_the_allowed_path_survives_a_resume_on_another_day`: start a run, then monkeypatch `date.today()` a day later and `advance`. The reconcile step's resolved `output` equals the path `plan/plan` committed.
- `test_the_doubt_reviewer_prompt_has_no_author_notes`:
  - after `define` is approved (so `handoff.md` exists), `plan/plan` returns a `code_map` entry with the marker `CODEMAP-X`
  - the `plan/plan_review` prompt (`argv[-1]`) contains neither `CODEMAP-X` nor `HANDOFF_LINE`, has no blank-line run where the slot was, and does contain "Do not open or append to"
  - a `build` task's prompt and its per-task review prompt still carry `CODEMAP-X`, also with `FlowRepo.configure(autopilot=False)`
- `test_build_prompt_without_a_phase_is_unchanged`: `build_prompt(cfg, review_step, ctx, {"role": "review"}, {}, "first")` with `extra={}` gives the same text as before the change

- [ ] **Step 2: Run them and confirm they fail**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_gitops.py agentflow/tests/test_doubt_flow.py agentflow/tests/test_agent_step.py --import-mode=prepend -q -k "outside_only or literal or edits_code or stray or delete_its or another_day or author_notes or without_a_phase"`
Expected: FAIL (`module 'agentflow.gitops' has no attribute 'outside_only'`).

- [ ] **Step 3: Implement** as listed under Files.

- [ ] **Step 4: Run the tests and the guard suites**

Run: the Step 2 command, then `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_guards.py agentflow/tests/test_autonomy.py agentflow/tests/test_headless.py agentflow/tests/test_agent_step.py agentflow/tests/test_gitops.py --import-mode=prepend -q`
Expected: PASS.

- [ ] **Step 5: Commit**

`git commit -m "Limit a reconcile step to its artifact in the tree and the commit, and keep author notes out of a doubt reviewer's prompt."`

---

### Task 6: The steps in the engine defaults, and the docs

**Depends on:** Task 5

**Files:**
- Modify: `agentflow/defaults/config.yaml`:
  - `skills.map` (101-111) adds `doubt: agentflow-doubt`
  - `phases.define.steps` and `phases.plan.steps` add the four steps exactly as proposal section 1 (`output: "{spec}"` and `"{plan_target}"`)
  - the comment above `phases`
- Modify: `agentflow/tests/test_config.py`:
  - `STEP_SKILLS` (700) adds `define/spec_reconcile: design_doc`, `define/spec_review: doubt`, `plan/plan_reconcile: plan` and `plan/plan_review: doubt`
  - the dict in `test_every_skills_map_reference_in_the_defaults_resolves` (828-834) adds `"doubt": "agentflow-doubt"`
- Modify: `docs/agentflow-playbook.md`:
  - §7 "Feature": one paragraph on the doubt review before each approval, and on reading the approval page's findings
  - §15: the `doubt_*` signals in the vocabulary list (lines ~572 and ~598)
- Modify: `CLAUDE.md`, the engine architecture list: one bullet for `doubt.py`
- Modify: `CHANGELOG.md` `[Unreleased]`:
  - `Added`: the doubt review and the reconcile round; the bundled `agentflow-doubt` skill; the `doubt_*` signals
  - `Changed`: the autonomy floor; also, a pack that already sets a now-refused value can't save from the Settings form until its YAML is fixed
- Test: `agentflow/tests/test_config.py`, `agentflow/tests/test_agent_step.py`, `agentflow/tests/test_plug_and_play.py`, `agentflow/tests/test_doubt_flow.py`

- [ ] **Step 1: Write the failing tests**

- `test_doubt_flow.py::test_the_engine_defaults_run_the_doubt_review` (sparse pack, `conditions.explain` for `feature` with a module whose spec says `proposed`):
  - `define/spec_review` and `plan/plan_review` are `run`
  - `define/spec_reconcile` and `plan/plan_reconcile` are `skip` with `RECONCILE_ONLY`
- The two `test_config.py` edits above fail until the defaults change.

- [ ] **Step 2: Run them and confirm they fail**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_doubt_flow.py agentflow/tests/test_config.py --import-mode=prepend -q -k "engine_defaults_run or STEP_SKILLS or explicit_id or skills_map_reference"`
Expected: FAIL.

- [ ] **Step 3: Edit the defaults and the docs**

- [ ] **Step 4: Run the tests and the suites that read the defaults**

Run: the Step 2 command, then `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_agent_step.py agentflow/tests/test_plug_and_play.py agentflow/tests/test_config.py agentflow/tests/test_layers.py --import-mode=prepend -q`
Expected: PASS. `test_engine_prompts_inline_the_bundled_skills_over_a_sparse_pack` now covers `agentflow-doubt`, and `test_plug_and_play`'s `run --dry-run` prints the reconcile steps as skipped.

- [ ] **Step 5: Commit**

`git commit -m "Run the doubt review of the spec and the plan by default in the feature lane, and document it."`

---

### Task 7: The `doubt_*` signals and the retro's evidence

**Depends on:** Task 4

**Files:**
- Modify: `agentflow/signals.py`:
  - `FAMILIES` (33-56) adds:
    - `doubt_blocking`, `doubt_rounds`, `doubt_noise`, `doubt_misread` and `doubt_open`, each `("lower", True)`
    - `doubt_actionable` as `("higher", True)`
  - none of them goes in `COUNT_FAMILIES`, so a run where no doubt review ran has no key, never a 0
  - `run_signals` (157-) sets all six from the last `doubt_settled` per phase (`first_blocking`, `rounds`, `open`, `actionable`, `noise`, `misread`), one window for all of them; no key for a phase with no `doubt_settled`
  - `refine_rounds:<phase>` skips `phase_rejected` with `kind == "reconcile"`
  - the docstring's vocabulary list
- Modify: `agentflow/tests/test_signals.py:136`: `len(FAMILIES) == 27`
- Modify: `agentflow/retro.py` `_evidence` (~226-240): a `findings_reconciled` event gives one evidence line per `contract_misread` or `noise` disposition, as `"<phase> <id> (<class>): <note>"`. Every model-written part goes through the existing `_q` cut, under the digest's existing line cap.
- Test: `agentflow/tests/test_signals.py`, `agentflow/tests/test_retro.py`

- [ ] **Step 1: Write the failing tests**

- `test_doubt_signals_come_from_doubt_settled`: two `doubt_settled` events on `plan`, the last with `rounds=2`, `first_blocking=3`, `open=1`, `actionable=1`, `noise=2` and `misread=0` → exactly those values, taken from the last event only
- `test_no_doubt_key_without_doubt_settled`
- `test_reconcile_rounds_are_not_refine_rounds`
- `test_doubt_targets_parse`
- `test_retro.py::test_reconciled_noise_is_retro_evidence`

- [ ] **Step 2: Run them and confirm they fail**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_signals.py agentflow/tests/test_retro.py --import-mode=prepend -q -k "doubt or reconcile"`
Expected: FAIL.

- [ ] **Step 3: Implement** as listed under Files.

- [ ] **Step 4: Run the tests and the consumers**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_signals.py agentflow/tests/test_lessons.py agentflow/tests/test_scorecard.py agentflow/tests/test_retro.py --import-mode=prepend -q`
Expected: PASS. The `COUNT_FAMILIES` assertions at `test_signals.py:92-93` are unchanged.

- [ ] **Step 5: Commit**

`git commit -m "Add doubt review signals from the settled reviews and their dispositions, and keep reconcile rounds out of the refine-round count."`

---

## Human step: this repository's pack (not a task: `.agentflow/**` is forbidden in a run)

After Task 6 is merged (or in the same branch, by hand in an interactive session), edit `.agentflow/config.yaml`:
- `skills.map`: add `doubt: agentflow-doubt`
- `phases.define.steps`: before `spec_approval`, add `spec_reconcile` (skill `domain-modeling`, `prompt: reconcile.md`, `output: "{spec}"`, `reconciles: true`, `commit: true`) and `spec_review` (skill `agentflow-doubt`, `prompt: spec_review.md`, `when: spec_awaits_approval`, `blocks_on_findings: true`)
- `phases.plan.steps`: after `plan`, add `plan_reconcile` (skill `writing-plans`, `output: "{plan_target}"`) and `plan_review` (skill `agentflow-doubt`, `prompt: plan_review.md`, `blocks_on_findings: true`)
- the `blocks_on_findings` comment (line 192): "holds back any phase; in a phase that needs approval, its approval, after reconcile rounds"

Then run `bin/agentflow validate` (expect `0 warnings`), `bin/agentflow explain feature --module doubt-review` (expect `– skip spec_reconcile … runs only in a reconcile round`), and `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_config.py --import-mode=prepend -q -k every_pack_validates_clean`.

## Finishing

After Task 7 and the human step, run the full suite once (25 to 60 minutes): `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests --import-mode=prepend -q`, then `bin/agentflow validate` (`0 warnings`). Before the branch review, dispatch the `security-auditor` agent on Task 5's diff.
