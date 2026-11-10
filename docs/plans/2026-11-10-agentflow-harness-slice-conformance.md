# Obligations, conformance audit and scorecard (harness slice 1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the quality bar explicit (`policy.obligations`) and prove before every push that the final tree carries passing evidence for it, plus a scorecard against a frozen baseline. The fixed workflow itself doesn't change.

**Architecture:**
- Obligations are typed config: `Policy`, `Obligation` and `Evidence` in `config.py`, merged by id across the layers, with an engine floor added in code.
- `policy.py` decides which obligations apply. `conformance.py` matches them against tree-bound events, with pure functions, and runs the audit at `finish` (one impure entry point).
- `scorecard.py` reuses `signals`, `metrics` and `retro.finished_runs`, and only adds a baseline file and a comparison.

**Tech Stack:** Python 3.12+, stdlib + `pyyaml>=6`, pytest via `uv run --no-project`.

**Spec:** `docs/agentflow-conformance-proposal.md`. The design source is `docs/agentflow-harness-proposal.md` §3.2, §3.5, §4 and §6.

## Global Constraints

- Engine dependencies stay stdlib + `pyyaml>=6`. Add no third-party import.
- No project strings in engine files (`test_portability.py` DENYLIST).
- `agentflow/tests/fixtures/explain/*.txt` stay byte-identical. Nothing in this slice prints from `explain`.
- Every new config key ships with: a `_PROTECTED_KEYS` entry, `validate` checks, a `test_config_edit.py` case and a playbook §14 line.
- `bin/agentflow validate` prints `0 warnings` for `.agentflow/config.yaml` and for both fixture packs.
- The event field is named `tree` (a `guards.snapshot` hash), never `tree_sha`.
- Every reader of a new event field uses `.get`, because older `events.jsonl` files must still load.
- The obligation statuses are exactly `satisfied`, `waived`, `missing` and `not_applicable`. `policy.enforce` takes `report` or `block`, with default `report`.
- The floor id is `engine_floor`, with `require: [gates:verify]`. No layer can remove it, and it is never `waived`.
- The new event is `conformance_checked`. The new stop kind is `conformance_failed`, which goes in `NEVER_RESOLVED`.
- The new signal families are `conformance_missing` and `conformance_waived`: both `lower`, both counts.
- The baseline file is `<state_dir>/runs/scorecard_baseline.json`, written through `safeio`.
- The model never decides conformance. Agent-written text (summaries, finding text) never goes into a Conformance section, beyond what the existing PR sections already carry.
- Interactive sessions don't commit. Each task's commit step is for runs, or for when the human asks. The commit message is one imperative sentence and ends with the pack's `Co-Authored-By` trailer.

## Review Focus

1. **A `block` stop followed by `agentflow retry <run> review/branch_review`, then resume.** `finish` audits again, passes and pushes. It must not stay stopped, and it must not push without the audit. Test: Task 4 `test_retry_after_conformance_stop_audits_again_and_pushes`.
2. **`finish` resumed after `pr_opened`.** It returns early as it does today, writes no second `conformance_checked` and pushes nothing again. Test: Task 4 `test_resumed_finish_after_pr_opened_does_not_audit_again`.
3. **A run started before the upgrade, whose `gate_result` events have no `tree`.** The audit treats the gate evidence as missing and runs the gates. It never crashes or counts them as satisfied. Test: Task 3 `test_gate_result_without_tree_is_not_evidence`, plus Task 4 `test_stale_gate_tree_makes_the_runner_run_the_gates`.
4. **A step summary or finding text written to look like a Conformance row.** The section is built only from config ids, statuses, seqs and trees. Test: Task 4 `test_agent_text_never_reaches_the_conformance_section`.
5. **`agentflow scorecard` with no baseline, and a baseline path that is a symlink.** The first prints one line naming `--freeze` and exits 0. The second is refused by `safeio` and never written through. Test: Task 5 `test_scorecard_without_baseline_says_how_to_freeze` and `test_freeze_refuses_a_symlinked_baseline`.

---

### Task 1: The `policy` config: dataclasses, layers, defaults, validate, Settings, playbook

**Files:**
- Modify: `agentflow/config.py`:
  - add the dataclasses next to `IssuePolicy` (around line 292)
  - add the `policy` field to `Config` (line 370)
  - build it in `_Builder` (next to `memory`, line ~849/898)
  - add a `_Validator.policy()` method and call it in `validate` (line 1731)
  - add `"conformance_failed"` to `NEVER_RESOLVED` (line 166)
- Modify: `agentflow/layers.py:30`: `_WHOLE_PARENTS = ("conditions", "gates", "policy.obligations")`. `_is_whole` must then match a 3-segment key `("policy", "obligations", <id>)`. Generalize it to "a parent tuple prefix plus exactly one more segment".
- Modify: `agentflow/defaults/config.yaml`: a `policy:` block.
- Modify: `.agentflow/config.yaml`, `agentflow/tests/fixtures/packs/python/.agentflow/config.yaml`, `agentflow/tests/fixtures/packs/node/.agentflow/config.yaml`: only what each needs for 0 warnings, plus the additions below.
- Modify: `agentflow/config_edit.py:47`: add `"policy"` to `_PROTECTED_KEYS`.
- Modify: `docs/agentflow-playbook.md` §14 "Pack reference": one line each for `policy.enforce` and `policy.obligations`, plus the 4-row evidence table from the spec.
- Test: `agentflow/tests/test_config.py`, `agentflow/tests/test_layers.py`, `agentflow/tests/test_config_edit.py`

**Interfaces:**
- Produces, in `agentflow/config.py`:
  - `FLOOR_ID = "engine_floor"`
  - `EVIDENCE_KINDS = ("gates", "step", "approval", "expect")`
  - `ENFORCE_MODES = ("report", "block")`
  - `@dataclass(frozen=True) class Evidence: kind: str; arg: str`, whose `__str__` gives `"kind:arg"`
  - `def parse_evidence(text: str) -> Evidence | None`. It returns None for an unknown kind, an empty arg, or `expect:` with any arg other than `red_then_green`.
  - `@dataclass(frozen=True) class Obligation: id: str; require: tuple[Evidence, ...]; when: str | None = None; lanes: tuple[str, ...] = ()`
  - `@dataclass class Policy: enforce: str = "report"; obligations: dict[str, Obligation] = field(default_factory=dict)`. The builder always inserts `FLOOR_ID` last, as `Obligation(FLOOR_ID, (Evidence("gates", "verify"),))`, whatever the layers say.
  - the field `Config.policy: Policy = field(default_factory=Policy)`

**Default obligations** (`defaults/config.yaml`, the default workflow's step ids):
```yaml
policy:
  enforce: report
  obligations:
    verified:       {lanes: [feature, bug], require: [step:verify]}
    reviewed:       {lanes: [feature, bug], require: [step:branch_review]}
    plan_approved:  {lanes: [feature], require: [approval:plan]}
    red_then_green: {lanes: [bug], require: ["expect:red_then_green"]}
```

The pack additions:
- This repository's pack: `security_reviewed: {when: security_path, require: [step:security_review]}`.
- The python fixture pack: `eval_gated: {when: eval_required, require: [step:eval_gate]}`, plus a `null` (or the right step) for any default that names a step its workflow lacks.

- [ ] **Step 1: Write the failing tests**

`test_config.py`:
- `test_policy_defaults_and_floor`: loading the python fixture pack gives `cfg.policy.enforce == "report"`, and `list(cfg.policy.obligations)[-1] == "engine_floor"`, with `require == (Evidence("gates", "verify"),)`.
- `test_a_pack_cannot_remove_the_floor`: a pack with `policy: {obligations: {engine_floor: null}}` still has the floor, and `validate` gives a warning at path `policy.obligations.engine_floor` whose message contains `"cannot be removed"`.
- `test_parse_evidence`: parametrize `("gates:verify", Evidence("gates","verify"))`, `("step:", None)`, `("expect:green", None)`, `("merge:x", None)`.
- `test_validate_policy_rules`, parametrized over a bad value per rule, with the expected path and level:
  - `enforce: strict` gives an error at `policy.enforce`
  - an unknown `when` gives an error at `policy.obligations.x.when`
  - a pack-written `lanes: [nope]` gives an error
  - `require: [gates:nope]` gives an error
  - `require: [step:nope]` gives an error
  - `require: [approval:build]`, where `build` has `needs_approval: false`, gives an error
  - an engine default whose step the pack lacks gives an error whose message contains `"set policy.obligations.<id>: null"`
  - a `null` default gives a warning that names the id
- `test_conformance_failed_is_never_resolved`: a pack whose `autonomy.resolve_kinds` includes `conformance_failed` gives a validate error, through the existing `NEVER_RESOLVED` check.
- `test_every_pack_validates_clean`: `.agentflow/config.yaml` and both fixture packs give `[]` from `validate`, with no errors and no warnings.

`test_layers.py`:
- `test_policy_obligations_merge_by_id`: an engine layer with `{a: {...}, b: {...}}` and a pack layer with `{b: {require: [step:x]}, c: {...}}` give `a`, the pack's whole `b` (no leftover keys from the engine's `b`) and `c`.
- `test_the_user_layer_refuses_policy`: `check_user` refuses `policy.enforce`, extending the existing parametrize at `test_layers.py:228`.

`test_config_edit.py`:
- `test_policy_is_protected`: `propose` with a change to `policy.enforce` or `policy.obligations.reviewed` raises `Refused` with the existing protected message, the same way the existing protected-key cases do.

- [ ] **Step 2: Run them and confirm they fail**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_config.py agentflow/tests/test_layers.py agentflow/tests/test_config_edit.py --import-mode=prepend -q -k "policy or floor or evidence or never_resolved or validates_clean"`
Expected: FAIL (`ImportError: Evidence`, or an `AttributeError` on `policy`).

- [ ] **Step 3: Implement**

Build the dataclasses, the builder, `_Validator.policy()`, the layers change, the defaults, the pack edits, the protected key and the playbook lines.
- Validate tells an engine default from a pack-written obligation by `cfg.layer_origins[("policy", "obligations", id)] == "engine"`.
- "A step some applicable lane walks" means the step id appears in `for_lane(<phase steps, recursively including nested `steps`>, lane)` for a lane in `obligation.lanes or cfg.lanes` that is also in `cfg.lanes`.

- [ ] **Step 4: Run them and confirm they pass**, then run `bin/agentflow validate` and check that it prints `0 warnings`.

Run: the Step 2 command (without `-k`), and `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_explain_golden.py agentflow/tests/test_portability.py --import-mode=prepend -q`
Expected: PASS.

- [ ] **Step 5: Commit**: "Add the policy config: obligations merged by id, the engine floor, validate and protection."

---

### Task 2: Every gate, review and expectation event carries `tree`

**Files:**
- Modify: `agentflow/gates.py`: add `record_results`.
- Modify: the 4 `gate_result` emit sites, each snapshotting before its gates run:
  - `agentflow/flow_control.py:502-506` (`_emit_gates` and its callers at `:399` and `:510`)
  - `agentflow/lanes.py:843-855` (`tail` kept for failures)
  - `agentflow/feature_lane.py:628-633` (it already holds `tree`)
  - `agentflow/sync_lane.py:207-218`
- Modify: `agentflow/feature_lane.py:528` (`review_result`), `agentflow/pipeline.py:390` (`step_findings`) and `agentflow/bug_eval.py:175` (`expectation_checked`): add `tree=snapshot(<worktree>)`.
- Test: `agentflow/tests/test_gates.py`, `agentflow/tests/test_bug_eval_security.py`

**Interfaces:**
- Produces `gates.record_results(run: Run, key: str, results: list[GateResult], tree: str, *, with_tail: bool = False) -> None`. It emits one `gate_result` per result with the existing fields (`key, gate, passed, skipped, reason, log, duration_s`), plus `tree`, plus `tail` only when `with_tail` is set and the gate failed. Import `Run` under `TYPE_CHECKING`.

- [ ] **Step 1: Write the failing tests**
  - `test_gates.py::test_record_results_writes_tree_and_tail_only_on_failure`: a fake `run` with a recording `emit`, and two results (one passed, one failed): both events have `tree == "t1"`, and only the failed one has `tail`, and only with `with_tail=True`.
  - `test_bug_eval_security.py::test_every_evidence_event_of_a_finished_run_has_a_tree`: reuse the body of `test_eval_lane_ends_clean_through_finish` (`:434`), then assert that every event of type `gate_result`, `review_result`, `step_findings` and `expectation_checked` in `run.events.read()` has a non-empty `tree`.
- [ ] **Step 2: Run them and confirm they fail.** Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_gates.py agentflow/tests/test_bug_eval_security.py --import-mode=prepend -q -k "record_results or has_a_tree"`. Expected: FAIL (a `KeyError: tree`, or a missing attribute).
- [ ] **Step 3: Implement** `record_results` and switch the 4 sites to it. Each site calls `snapshot(worktree)` **before** `run_gates`, and the gates helper takes that value. Add `tree` to the other three events.
- [ ] **Step 4: Run them and confirm they pass**, along with the regression files: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_gates.py agentflow/tests/test_bug_eval_security.py agentflow/tests/test_feature_lane.py agentflow/tests/test_sync.py agentflow/tests/test_agent_step.py agentflow/tests/test_signals.py agentflow/tests/test_retro.py --import-mode=prepend -q`. Expected: PASS.
- [ ] **Step 5: Commit**: "Record the tree each gate, review and expectation ran on."

---

### Task 3: Which obligations apply, and their evidence (pure)

**Files:**
- Create: `agentflow/policy.py`
- Create: `agentflow/conformance.py` (the pure part only)
- Test: `agentflow/tests/test_conformance.py`

**Interfaces:**
- Consumes `Policy`, `Obligation`, `Evidence` and `FLOOR_ID` (Task 1), and the `tree` on events (Task 2).
- Produces, in `policy.py`:
  - `def applicable(cfg: Config, ctx: Ctx, lane: str) -> list[tuple[Obligation, str]]`: the obligations whose `lanes` include `lane` (or have none) and whose `when` evaluates to anything other than `False` (`conditions.evaluate(cfg, when, ctx).value is not False`). The `str` is the reason (`"always"`, or the condition's text). The floor is always included.
  - `def step_phase(cfg: Config, step_id: str) -> str | None`: the phase whose steps, at any nesting, hold `step_id`.
- Produces, in `conformance.py`:
  - `Status = Literal["satisfied", "waived", "missing", "not_applicable"]`
  - `@dataclass class Finding: id: str; require: list[str]; status: Status; evidence: list[int]; note: str`
  - `def last_code_change(events: list[dict]) -> int`: the seq of the last `step_committed` or `task_committed`, or of a `base_synced` with a non-empty `conflicts`. 0 when there is none.
  - `def evaluate(cfg: Config, events: list[dict], obligations: list[Obligation], final_tree: str, not_applicable: list[Obligation] = ()) -> list[Finding]`: pure. The rules for each evidence kind are in the spec's evidence table. An obligation is satisfied when every one of its `require` items is.
  - `def missing_gates(cfg: Config, findings: list[Finding]) -> list[str]`: the gate names, in `gate_sets` order and without duplicates, behind the `gates:` items of the findings that are `missing`.

**The rules that need pinning (the rest follow from the spec's table):**
- `step:<id>` matches events whose `key.split("/")[-1] == id`. "Clean" means: the last `step_findings` for that key after the step's last finish holds no finding that `blocking_of(cfg, findings)` marks as blocking, unless a `phase_approved` for that key's phase, with `by != "runner"`, comes after that `step_findings`. That second case gives a note of `"human override"`.
- `approval:<phase>` uses `phase_approved` with `by != "runner"`, or a `phase_skipped{phase}` whose reason doesn't start with `"marked done"`. That second case gives a note of `"on base"`.
- **Waived:** the obligation would be `missing`, a `run_marked_done` event exists, and for each unmet item the phase that holds it (`step_phase` for `step:`; the phase itself for `approval:`; the phase of the step with `expect: green` for `expect:`) has a `phase_skipped` after that `run_marked_done`'s seq. The evidence is then `[that run_marked_done seq]`, with a note of `"waived by <by>"`. `gates:` items and the floor are never waived.

- [ ] **Step 1: Write the failing tests** in `test_conformance.py`, each over a hand-built event list (a helper `ev(seq, type, **fields)`) on the python fixture `cfg`. The names and assertions:
  - `test_gates_evidence_needs_the_final_tree`: a passing `verify` gate set on tree `"t1"` gives `satisfied` for final `"t1"`, and `missing` for final `"t2"`.
  - `test_gate_result_without_tree_is_not_evidence`: a passing `gate_result` with no `tree` gives `missing`.
  - `test_a_skipped_gate_counts_with_its_reason`: the finding's note contains the skip reason.
  - `test_step_after_last_code_change`: `verify` finished at seq 10 with a commit at seq 12 gives `missing`. With the commit at seq 8, it gives `satisfied` with `evidence == [10]`.
  - `test_blocking_findings_make_a_step_unclean_unless_a_human_approved`
  - `test_runner_approval_is_not_approval`, and `test_skip_if_skip_is_on_base`
  - `test_conflicted_sync_is_a_code_change_and_clean_is_not`: `base_synced{conflicts: ["a.py"]}` after the review gives `missing`. `conflicts: []` gives `satisfied`.
  - `test_red_then_green`: red then green, with green after the last change, gives `satisfied`. Green alone, or green before a later commit, gives `missing`.
  - `test_marked_done_waives_skipped_phases_but_never_the_floor`: `run_marked_done{phase: build, by: "me"}`, then `phase_skipped` for verify and review: `verified` and `reviewed` give `waived` with the note `"waived by me"`, and `engine_floor` with no gate events gives `missing`.
  - `test_unknown_when_applies`: with a `when` condition whose check returns unknown (a `changed_paths` with `ctx.base_ref` set to a ref that doesn't exist), `applicable` includes the obligation.
  - `test_missing_gates_lists_each_gate_once_in_set_order`
- [ ] **Step 2: Run them and confirm they fail.** Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_conformance.py --import-mode=prepend -q`. Expected: FAIL (`ModuleNotFoundError: agentflow.conformance`).
- [ ] **Step 3: Implement** `policy.py` and the pure part of `conformance.py`. Each module starts with a docstring. `conformance.py` must not import `feature_lane`, which would be a cycle.
- [ ] **Step 4: Run them and confirm they pass.** Same command. Expected: PASS.
- [ ] **Step 5: Commit**: "Decide which obligations apply and match each to tree-bound evidence."

---

### Task 4: The audit at `finish`, the PR section and the `block` stop

**Files:**
- Modify: `agentflow/conformance.py`: add `audit` and `render_section`.
- Modify: `agentflow/feature_lane.py:748-791` (`run_finish`): call the audit after `runner_commits_only` and before `write_pr_body`. In `render_pr_body` (`:926`), add the section after `Gates`.
- Modify: `agentflow/flow_control.py` (`_dry_steps`): for a `finish` step in a dry run, print one line per applicable obligation: `"    obligation <id>: <require…> (<reason>)"`.
- Modify: `agentflow/hitl.py` (`_how_to_go_on`): for kind `conformance_failed`, add the line naming `agentflow retry <run> <key>` for each missing step.
- Test: `agentflow/tests/test_conformance.py` (integration, using `flow_repo` and `gh` as `test_feature_lane.py:550-600` does, reusing `begin_finish`), and `agentflow/tests/test_feature_lane.py` (the PR body).

**Interfaces:**
- Consumes `applicable`, `evaluate` and `missing_gates` (Task 3), and `gates.record_results` (Task 2).
- Produces:
  - `conformance.audit(cfg: Config, run: Run, worktree: Path, ctx: Ctx, key: str) -> bool`. True means `finish` goes on. False means it stopped, with `gate_failed` or `conformance_failed` raised. Its order is:
    1. snapshot the final tree
    2. `applicable`
    3. `evaluate`
    4. run `missing_gates` once with `record_results`. A failure raises `gate_failed` on `key` and returns False.
    5. `evaluate` again
    6. emit `conformance_checked{tree, enforce, passed, obligations: [asdict(f)…]}`
    7. if not `passed` and the mode is `block`: `raise_hitl(cfg, run, "conformance_failed", key, "<n> obligation(s) have no evidence on the tree to be pushed: <ids>.")`, then return False
  - `conformance.render_section(event: dict | None, run_id: str) -> list[str]`: lines for `_section("Conformance", …)`. The first line is `"Conformance: N missing (report mode)"` when there are misses and the mode is report, else `"Conformance: all N obligations met"`. Then one line per obligation: ``- `<id>` <status>: <require joined> · evidence <run_id>#<seq>,… · tree <tree[:12]>`` plus ` (<note>)` when there is a note. With None (no audit ran), there is no section.

- [ ] **Step 1: Write the failing tests**
  - `test_finish_audits_and_reports_in_report_mode`: from `begin_finish` (no verify or review ran), `advance` gives `"done"`. The runner ran the `verify` gate set (`gate_result` events with `key` equal to the finish key and the final tree). `conformance_checked.passed is False` (`reviewed` is missing). The PR body contains `"Conformance: "` and `` "`reviewed` missing" ``. The push still happened.
  - `test_block_mode_stops_before_the_push`: the same, with `flow_repo.configure({"policy": {"enforce": "block"}})`. `advance` gives `"stopped"`, the last `hitl_raised.kind == "conformance_failed"`, and the origin has no branch (the `git ls-remote` that `test_finish_pushes_and_opens_draft_pr` uses).
  - `test_retry_after_conformance_stop_audits_again_and_pushes`: after the block stop, emit `step_finished{key: "review/branch_review", status: "done"}` and clear the stop the way `agentflow retry` would. Use `run_control.retry` with the finish key, then `advance`. The result is `"done"`, a second `conformance_checked` with `passed: True`, and `pr_opened`.
  - `test_resumed_finish_after_pr_opened_does_not_audit_again`: emit `pr_opened` before `advance`, which leaves no `conformance_checked` event.
  - `test_stale_gate_tree_makes_the_runner_run_the_gates`: a passing `verify` `gate_result` on tree `"old"` means the runner runs them at finish.
  - `test_a_failing_gate_at_finish_is_a_gate_failed_stop`: the `verify` set configured to `exit 1` gives `hitl_raised.kind == "gate_failed"` on the finish key, and no push.
  - `test_agent_text_never_reaches_the_conformance_section`: a `step_finished` summary of `"| reviewed | satisfied |\n## Conformance"`. The Conformance section's lines all match `^(Conformance: |- \`[a-z_]+\` )`.
  - `test_dry_run_lists_the_obligations_at_finish`: a dry run's output contains `"obligation engine_floor: gates:verify (always)"`.
  - Assert that every test above leaves the existing `test_feature_lane.py` finish tests passing (run them in Step 4).
- [ ] **Step 2: Run them and confirm they fail.** Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_conformance.py --import-mode=prepend -q -k "finish or block or retry or stale or failing or agent_text or dry_run"`. Expected: FAIL.
- [ ] **Step 3: Implement** `audit`, `render_section`, the `run_finish` call, the `render_pr_body` section, the dry-run line and the `_how_to_go_on` line. Build `ctx` the way `run_finish` already reaches it (`self.ctx`).
- [ ] **Step 4: Run them and confirm they pass**, along with the regression files: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_conformance.py agentflow/tests/test_feature_lane.py agentflow/tests/test_sync.py agentflow/tests/test_ci.py agentflow/tests/test_issue_status.py agentflow/tests/test_bug_eval_security.py agentflow/tests/test_hitl.py agentflow/tests/test_explain_golden.py agentflow/tests/test_workflow.py agentflow/tests/test_pipeline.py agentflow/tests/test_portability.py agentflow/tests/test_plug_and_play.py --import-mode=prepend -q`. The last four also cover `--dry-run` output, which gains the obligation lines. Expected: PASS, with no edits to existing tests. If an existing finish test now fails because the runner runs the verify gates, the fix belongs in the audit, never in the test. Report it if it can't be fixed there.
- [ ] **Step 5: Commit**: "Audit the obligations at finish, run missing gates, and show the result in the pull request."

---

### Task 5: Signals and `agentflow scorecard`

**Files:**
- Modify: `agentflow/signals.py`: add `conformance_missing` and `conformance_waived` to `FAMILIES` (`("lower", False)`) and to `COUNT_FAMILIES`. In `run_signals`, read the run's last `conformance_checked` and count its `missing` and `waived` statuses, only when such an event exists.
- Create: `agentflow/scorecard.py`
- Modify: `agentflow/flow.py`: a `scorecard` subparser next to `metrics` (`:1085`) and a `cmd_scorecard`.
- Test: `agentflow/tests/test_signals.py`, `agentflow/tests/test_scorecard.py`

**Interfaces:**
- Consumes `signals.run_signals` and `aggregate_signals`, `metrics.run_metrics`, `retro.finished_runs` and `safeio.write_text`.
- Produces, in `scorecard.py`:
  - `BASELINE = "scorecard_baseline.json"`, under `state.runs_root(repo)`
  - `def run_row(run: Run, now: datetime) -> dict[str, float]`: `run_signals`, plus `cost_usd`, `wall_h` (`wall_s / 3600`) and `human_wait_h` (`buckets["human"] / 3600`). Add `cost_usd`, `wall_h` and `human_wait_h` to `signals.FAMILIES` as `("lower", False)`, so that direction lives in one place.
  - `def freeze(repo: Path, lane: str | None, date_from: date | None, date_to: date | None, replace: bool, now: datetime) -> dict`: per lane, `{"runs": [ids], "from", "to", "engine": pack.ENGINE_VERSION, "signals": aggregate_signals(rows)}`. It raises `FileExistsError` when the file exists and `replace` is false.
  - `def load_baseline(repo: Path) -> dict | None`
  - `def scorecard_view(repo: Path, lane: str | None, date_from: date | None, date_to: date | None, now: datetime) -> dict`: `{"baseline": bool, "lanes": {lane: [{"signal", "baseline", "baseline_n", "current", "current_n", "delta", "verdict", "slo"}]}}`. `verdict` is one of `better`, `worse` or `same`, using `FAMILIES` direction. `slo` is true only for `conformance_missing` with a current mean above 0, and those rows sort first. By default, the current runs are those that ended after the baseline's `to`.
  - `def scorecard_table(view: dict) -> str`. With no baseline it is the single line `"No baseline yet: run \`agentflow scorecard --freeze\` first."`

- [ ] **Step 1: Write the failing tests**
  - `test_signals.py::test_conformance_counts_come_from_the_last_check`: two `conformance_checked` events, of which the last has 1 missing and 2 waived, give `{"conformance_missing": 1, "conformance_waived": 2}`. With no event, neither key is present.
  - `test_scorecard.py`:
    - `test_freeze_then_refuse_then_replace`
    - `test_compare_marks_direction_and_slo`: synthetic rows, monkeypatching `run_row`. A rise in `gate_first_pass:lint` is `better`, a rise in `cost_usd` is `worse`, and `conformance_missing` above 0 is the first row with `slo` true.
    - `test_scorecard_without_baseline_says_how_to_freeze`
    - `test_freeze_refuses_a_symlinked_baseline`: the baseline path is a symlink to a file outside, and `freeze(..., replace=True)` raises `safeio`'s refusal and leaves the target unchanged.
    - `test_cli_scorecard_json`: `flow.main(["scorecard", "--json"])` prints valid JSON with `"baseline": false`.
- [ ] **Step 2: Run them and confirm they fail.** Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_signals.py agentflow/tests/test_scorecard.py --import-mode=prepend -q`. Expected: FAIL.
- [ ] **Step 3: Implement** the signals change, `scorecard.py` and the CLI. The flags are `--lane`, `--from`, `--to`, `--json`, `--freeze` and `--replace`, with the date parsing `cmd_metrics` already does (`flow.py:792`).
- [ ] **Step 4: Run them and confirm they pass**, along with `agentflow/tests/test_retro.py` and `agentflow/tests/test_lessons.py`, because both read `FAMILIES`. Expected: PASS.
- [ ] **Step 5: Commit**: "Add the scorecard: signals against a frozen baseline, with conformance as the hard SLO."

---

### Task 6: The Scorecard section in the Metrics tab, and the docs

**Files:**
- Modify: `agentflow/dashboard.py`: `GET /api/scorecard`, next to `/api/metrics` (`:422`), with the same token check and the same `from`/`to`/`lane` parsing, returning `scorecard_view`.
- Modify: `agentflow/dashboard.html`: in `#metrics-view`, a "Scorecard" section rendered with the existing `el()` helper (text nodes only, never `innerHTML`). It shows one table per lane, and the no-baseline line when `baseline` is false.
- Modify: `docs/agentflow-playbook.md` §10 (the cheat sheet line for `agentflow scorecard [--freeze]`) and §15 (one paragraph: the conformance signals reach the retro and the scorecard).
- Modify: `CLAUDE.md`, the engine architecture list: one bullet for `policy.py`, `conformance.py` and `scorecard.py`, and the `scorecard` line under Commands.
- Modify: `CHANGELOG.md` `[Unreleased]` → `Added`: one line for obligations and the conformance audit, and one for `agentflow scorecard`.
- Test: `agentflow/tests/test_dashboard.py`

**Interfaces:**
- Consumes `scorecard.scorecard_view` (Task 5).

- [ ] **Step 1: Write the failing tests**
  - `test_dashboard.py::test_scorecard_api_needs_the_token_and_returns_the_view`: without the token the API returns 401 (or whatever the existing API tests expect for a missing token). With it, it returns 200, and the JSON has the keys `baseline` and `lanes`.
  - `test_dashboard.py::test_metrics_view_has_a_scorecard_section`: the served HTML contains `id="scorecard"`.
- [ ] **Step 2: Run them and confirm they fail.** Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_dashboard.py --import-mode=prepend -q -k scorecard`. Expected: FAIL.
- [ ] **Step 3: Implement** the route, the section and the docs.
- [ ] **Step 4: Run them and confirm they pass**, then run the full suite: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests --import-mode=prepend -q` (25-60 min), and `bin/agentflow validate`, which must print `0 warnings`. Expected: PASS, with `test_packaging` and `test_portability` green.
- [ ] **Step 5: Commit**: "Show the scorecard in the dashboard's Metrics tab and document the slice."

---

## Self-review notes

- **Spec coverage:**
  - acceptance 1 is Task 2
  - acceptance 2 and 3 are Task 1
  - acceptance 4, 6 and 7 are Task 4
  - acceptance 5 is the Task 4 regression run, plus the Task 6 full suite
  - acceptance 8 is Tasks 5 and 6
  - acceptance 9 is checked in the Task 1, 4 and 6 runs
  - the resolved questions:
    - the conflicted merge, mark done, no `plan_written` and `on base` are in Task 3
    - `report` as the default is in Task 1
    - the baseline location is in Task 5
- **Spec corrections made before this plan:** 4 emit sites, not 5; the baseline uses `retro.finished_runs`; validate's lane rule applies only to pack-written obligations; and the test file is `test_layers.py`.
- **Security:** this slice changes `layers.py`, so the overlay applies.
  - `policy` is protected and never a user key.
  - The floor is added in code.
  - `conformance_failed` is in `NEVER_RESOLVED`, so a resolver model can never settle it.
  - The baseline is written through `safeio`.
  - The dashboard renders text nodes only.
  - Evidence is read only from `events.jsonl`, which sits in the run directory, outside every directory an agent may write (`workspace.py`).
