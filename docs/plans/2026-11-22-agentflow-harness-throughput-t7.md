# Throughput slice T7 (Speculative planning and the approval ramp) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A person's last synchronous wait stops costing wall time. While the spec waits for its approval, the runner writes the plan (and runs the plan's doubt review) from the proposed spec. If the spec is approved unchanged, the plan is kept. If the person rejects it and the spec changes, the plan is re-planned from the diff. Separately, each phase approval gets an `off → observe → auto` ramp. In `observe` the runner records whether it would have approved. In `auto` it approves by itself, but only after `ramp_samples` human decisions in a row agreed with it and the scorecard is green.

**Architecture:**
- **Speculation is a look-ahead inside the same runner, with the approval kept as the run's wait.** When a `needs_approval: true` phase raises its approval, `_Loop.speculate` checks `speculation.due`. If speculation is due, the runner records `speculation_started` and walks the next phase while `state["waiting"]` is still that approval. Two reducer changes keep the wait in place: a refine or reconcile round of *another* phase leaves a pending approval alone, and a stop raised under the speculated phase is queued behind it quietly (`hitl_raised{speculative: true}`). Every exit of `settle` that would approve the speculated phase or raise its approval returns `"awaiting_approval"` instead. The runner never goes past that one phase, so `build` can't start before `approval:define`.
- **Between steps, the runner reads what the person did.** `Executor.yield_if` is checked after `halted` in `run_step`. An approval lets the walk go on as a normal phase. A rejection, or an approval with `hold`, makes it yield at the next step boundary. After a rejection, the same runner runs the refine round of the approval phase, and re-speculates when it raises the approval again.
- **The spec is pinned by its bytes.** `speculation.basis` is `{path: sha256}` of the approval phase's committed `output`s. A phase planned on an older basis is sent back once, as a runner refine round of kind `replan` whose note names the changed paths and the `git diff` to read. That happens either when speculation starts again or, if no speculation ran, before the phase walks after the approval.
- **The ramp is a pure function of event logs.** `approval_ramp.decide` runs in `settle` just before an approval is raised. It records `would_auto_approve` with the checks that held and those that didn't. Under `auto` it may emit `phase_approved{by: AUTO_BY, ramp}`, which `conformance` accepts as evidence for `approval:<phase>`. Eligibility is read from the finished runs of the same lane, route profile and phase, plus the scorecard baseline.
- **Off means today.** With `autonomy.speculate: []` and no `autonomy.approvals`, which are the engine defaults:
  - none of the new events is written;
  - `settle`, `phase` and every reducer behave as they do now;
  - `explain` doesn't change.

**Tech Stack:** Python 3.12 stdlib (`hashlib`, `dataclasses`), PyYAML, pytest. No new dependency.

**Spec:** `docs/specs/agentflow-throughput-proposal.md`. This plan covers slice **T7** (§8):
- §4.1.5, "Plan speculatively" and "Autonomy ramp for each approval kind". "Batch approvals" shipped in T4;
- §6's event `would_auto_approve`;
- §9's risk row "Speculative plans waste money": speculation only after the approval phase's review is clean, and at most one speculation per spec revision;
- the config table's `autonomy.approvals.<kind>`.

Out of scope:
- showing the ramp's verdict on the approval page. The person must not see it, so their decision stays an independent sample;
- a dashboard view of the ramp. `agentflow scorecard` prints it;
- speculating more than one phase ahead, or into a phase that changes code;
- turning either feature on in this repository's pack. `.agentflow/**` is forbidden in runs, so a person does that after the slice ships.

## Global Constraints

- Engine dependencies stay stdlib + `pyyaml>=6`. No project strings in the engine (`test_portability.py` `DENYLIST`).
- The golden files `agentflow/tests/fixtures/explain/*.txt` stay byte-identical. The fixture `python` pack's `config.yaml` is not edited: tests switch the features on with `flow_repo.configure(patch=..., edit=with_doubt)`.
- **Off means today.** With `autonomy.speculate: []` and `autonomy.approvals: {}`, no `speculation_*` or `would_auto_approve` event is written, and no key is added to `state.initial_state`. Read `state.get("speculation")`.
- **The engine floor holds:**
  - the runner never walks past the one speculated phase while the approval waits;
  - a speculated phase holds only artifact steps (validate);
  - an agent step with an `output` in a speculated phase is fenced to that output exactly as a reconcile step is (`outside_output`);
  - `NEVER_AUTO = ("goal",)` and a run where the security overlay holds are never approved by the ramp;
  - the ramp's own decisions never count towards its promotion.
- **New config keys** (all under `autonomy`, which is already in `layers.WORKFLOW_KEYS`, so `layers.py` isn't touched):

  | Key | Default | Rule | Settings |
  |---|---|---|---|
  | `autonomy.speculate` | `[]` | list of phase names, each not the first phase, with a `needs_approval: true` phase right before it in `phases:`, and holding only top-level agent steps that have an `output`, or `role: review` steps without `commit` | Form field (list) |
  | `autonomy.approvals` | `{}` | map of a `needs_approval: true` phase to `off`, `observe` or `auto`. `auto` is refused for a name in `NEVER_AUTO` | `_PROTECTED_KEYS` |
  | `autonomy.ramp_samples` | `5` | a whole number from 3 to 50 | `_PROTECTED_KEYS` |

- **New and changed events:**

  | Event | Fields | `state.REQUIRED` | Reducer |
  |---|---|---|---|
  | `speculation_started` (new) | `{phase, after, basis: {path: sha256}, sha}`. `sha` is HEAD when it started | `("phase", "after", "basis")` | `state["speculation"] = {phase, after, basis, sha, since}` |
  | `speculation_ended` (new) | `{phase, outcome}`, outcome in `finished, stopped, approved, rejected, halted` | `("phase", "outcome")` | `state["speculation"] = None` |
  | `would_auto_approve` (new) | `{phase, mode, verdict: bool, held: [check], missing: [check], profile: str?, eligible: bool?, why: str?}` | `("phase", "verdict")` | none |
  | `phase_rejected` | `kind: "replan"`, `basis`, `from_sha` | unchanged | the kind is kept on `state["refine"]`, as today |
  | `hitl_raised` | `speculative: true` | unchanged | copied onto the wait |
  | `phase_approved` | `ramp: {samples: int, profile: str}` | unchanged | unchanged |

- **Check names** (`approval_ramp.CHECKS`, in this order):
  - `no_open_findings`: no blocking finding is left in the phase;
  - `small_route`: the run's route profile is in `RAMP_PROFILES = ROUTE_PROFILES[:2]`, which is `XS` or `S`;
  - `no_security_overlay`: `overlays.security_path` is False;
  - `first_raise`: no person rejected this phase in this run.
- Tests run as `uv run --no-project --with pyyaml --with pytest pytest <path> --import-mode=prepend -q`. Run `uvx ruff@0.17.4 check .` before each commit. Never create or edit `uv.lock`. Each user-visible change adds a line under `## [Unreleased]` in `CHANGELOG.md`.

## Review Focus

1. **The person approves the spec while the plan session is still running.** The walk goes on as a normal `plan` phase. Define's handoff is written before plan settles, there's no second stop, and `build` starts after plan settles. (Task 3: `test_approving_mid_speculation_goes_on_into_build`.)
2. **The person rejects the spec mid-speculation.** The runner yields at the next step boundary and runs define's refine round itself. Plan's queued stops are dropped, and plan is re-planned once, with the `replan` note, on the new spec. (Task 4: `test_rejecting_mid_speculation_refines_the_spec_then_replans`.)
3. **The speculative plan stops with a question while the spec waits.** Nobody is told, and the inbox shows only the spec. After the approval, the question is the run's wait and a person is told once. (Task 3: `test_a_speculative_stop_waits_quietly_behind_the_spec`.)
4. **The runner dies, or the person pauses, mid-speculation.** On `advance`, the speculation goes on where it stopped: no second `speculation_started` for the same basis and no build step. (Task 3: `test_a_runner_that_died_mid_speculation_resumes_it`.)
5. **A phase under `auto` whose run touches a security path, or that has fewer agreeing samples than `ramp_samples`, or no scorecard baseline.** It goes to a person, and `would_auto_approve` says why. (Task 5: `test_auto_falls_back_to_a_person`.)

---

### Task 1: The `autonomy.speculate`, `autonomy.approvals` and `autonomy.ramp_samples` keys

**Depends on:** none

**Files:**
- Modify: `agentflow/defaults/config.yaml` (three keys at the end of `autonomy:`)
- Modify: `agentflow/config.py` (`AUTONOMY_KEYS`, constants, `_Validator.autonomy`)
- Modify: `agentflow/config_edit.py` (one Form field, two `_PROTECTED_KEYS`)
- Modify: `docs/agentflow-playbook.md` (§14, three lines), `CHANGELOG.md`
- Test: `agentflow/tests/test_config.py`, `agentflow/tests/test_config_edit.py`

**Interfaces:**
- Produces, in `config.py`:
  - `APPROVAL_MODES = ("off", "observe", "auto")`, `NEVER_AUTO = ("goal",)`, `RAMP_SAMPLES_MIN, RAMP_SAMPLES_MAX = 3, 50`;
  - `speculation_shape(phase: Phase) -> str | None`: why a phase can't be speculated, or None.
- `cfg.autonomy` stays a dict. Readers use `cfg.autonomy.get("speculate", [])`, `.get("approvals", {})` and `.get("ramp_samples", 5)`.
- Engine defaults:
  ```yaml
    speculate: []      # phases that run while the approval of the phase right before them waits (e.g. [plan]); [] = off
    approvals: {}      # phase -> off | observe | auto: the approval ramp; auto also needs ramp_samples agreeing runs and a green scorecard
    ramp_samples: 5    # human decisions in a row that agreed with the runner before `auto` approves by itself; 3 to 50
  ```

- [ ] **Step 1: Write the failing tests** in `test_config.py`. Use the existing `tmp_cfg`/validate helpers, and add `with_doubt` when a plan review is needed:
  ```python
  def test_the_autonomy_ramp_keys_default_off(shipped): ...  # speculate == [], approvals == {}, ramp_samples == 5; validate 0 warnings
  def test_speculate_names_a_phase_after_an_approval(tmp_cfg):
      # [define] -> error "first phase"; [nope] -> "not a phase"; [plan] valid when define needs_approval;
      # [plan] with define needs_approval: false -> error naming define
  def test_a_speculated_phase_holds_only_artifact_steps(tmp_cfg):
      # [build] -> error naming the first offending step id; a plan step without output -> error; a review step with commit: true -> error
  def test_approvals_map_an_approval_phase_to_a_mode(tmp_cfg):
      # {build: observe} (needs_approval false) -> error; {define: sometimes} -> error listing APPROVAL_MODES; {define: auto} valid
  def test_goal_is_never_auto(tmp_cfg):   # a phase named goal with needs_approval: true; {goal: auto} -> error; {goal: observe} valid
  @pytest.mark.parametrize("n", [2, 51, True, "5"])
  def test_ramp_samples_is_a_whole_number_from_3_to_50(tmp_cfg, n): ...
  ```
- [ ] **Step 2: Run them and see them fail** (unknown autonomy key, or no error raised).
- [ ] **Step 3: Implement it.** Add the three keys to `AUTONOMY_KEYS` and the defaults. In `_Validator.autonomy`, check them as the rules in Global Constraints say, with one error per key path. `speculation_shape` walks `phase.steps` for every lane. A step with nested `steps`, a `foreach`, a non-agent `type`, an agent step with no `output` that isn't `role: review`, or a review step with `commit` gives the reason `step <id> is not an artifact step`.
- [ ] **Step 4: Settings.** In `config_edit.py`, add `(("autonomy", "speculate"), "Phases run while the approval before them waits", {"kind": "list"})` next to the other autonomy fields, and add `"autonomy.approvals"` and `"autonomy.ramp_samples"` to `_PROTECTED_KEYS`. In `test_config_edit.py`, following `test_route_profiles_are_protected`, add `test_the_approval_ramp_is_protected` (both keys in `config_edit.PROTECTED`, and a Settings save that changes either one is refused) and `test_speculate_is_a_form_field` (it saves `[plan]` and reloads it).
- [ ] **Step 5: Run `test_config.py` and `test_config_edit.py` and see them pass,** then run `bin/agentflow validate`. Expected: `0 warnings`.
- [ ] **Step 6: Docs.** Add three playbook §14 lines in the `autonomy` group, each with its default and rule, and one `Added` line in `CHANGELOG.md`.
- [ ] **Step 7: Commit:** `git commit -m "Add the autonomy keys for speculative phases and the approval ramp."`

---

### Task 2: A pending approval survives another phase's rounds and stops

**Depends on:** Task 1

**Files:**
- Modify: `agentflow/state.py`:
  - `_on_phase_rejected`, `_on_phase_approved`, `_on_hitl_raised`;
  - two new reducers;
  - `REQUIRED`.
- Modify: `agentflow/hitl.py` (`raise_hitl`)
- Modify: `agentflow/autonomy.py` (`finding_rounds`)
- Test: `agentflow/tests/test_state.py`, `agentflow/tests/test_hitl.py`, `agentflow/tests/test_autonomy.py`

**Interfaces:**
- Produces:
  - the `speculation_started` and `speculation_ended` reducers in the event table;
  - `state.speculating(state: dict) -> dict | None`: `state["speculation"]` while `state["waiting"]` is the phase approval of its `after`, else None.
- Behaviour:
  - `_on_phase_rejected` for phase P while the current wait is the approval of another phase Q keeps `waiting`, the queue and `status == "awaiting_approval"`. It still sets `refine` and the step rows.
  - `_on_phase_rejected` for the waiting phase itself drops the queued waits that carry `speculative`, before it promotes the queue.
  - `_on_phase_approved` leaves `status == "refining"`, not `"running"`, when `state["refine"]` is still open for another phase.
  - `raise_hitl` adds `speculative=True` and doesn't notify when `state.speculating(run.state)` is set and the key's phase is its `phase`.
  - `autonomy.finding_rounds` leaves out `phase_rejected` events with `kind == "replan"`.

- [ ] **Step 1: Write the failing tests.**
  ```python
  # test_state.py: emit the events by hand on a `run`
  def test_a_reconcile_round_of_another_phase_keeps_the_pending_approval(run):
      # phase_approval_raised(define); speculation_started(plan, after=define, basis={...});
      # phase_rejected(plan, by="runner", kind="reconcile") ->
      assert run.state["waiting"]["key"] == "define_approved" and run.state["status"] == "awaiting_approval"
      assert run.state["refine"]["phase"] == "plan"
  def test_a_rejection_of_the_waiting_phase_drops_speculative_stops(run):
      # define approval pending; hitl_raised(plan/plan, speculative=True) is queued; phase_rejected(define, by="me") ->
      assert run.state["waiting"] is None and run.state["waiting_queue"] == []
  def test_approving_while_another_phase_refines_leaves_it_refining(run): ...  # status == "refining"
  def test_speculation_events_set_and_clear_the_record(run): ...              # and speculating() is None once the approval clears
  # test_hitl.py
  def test_a_stop_under_the_speculated_phase_is_raised_quietly(run, cfg, monkeypatch):
      # notify_run patched to record; raise_hitl(cfg, run, "needs_human", "plan/plan", "q") -> event speculative True, notified False, no notice
  def test_a_stop_of_another_phase_is_not_speculative(run, cfg): ...
  # test_autonomy.py
  def test_a_replan_round_is_not_a_finding_round(run): ...                    # finding_rounds(run, "plan") == 0
  ```
- [ ] **Step 2: Run them and see them fail.**
- [ ] **Step 3: Implement the reducers, `raise_hitl` and `finding_rounds`** as in Interfaces. Use `_approval_pending(state)`, and compare `waiting["phase"]` with `ev["phase"]`.
- [ ] **Step 4: Run the three files and see them pass,** together with `test_doubt_flow.py` and `test_pipeline.py`, which pin today's reject and approve paths.
- [ ] **Step 5: Commit:** `git commit -m "Keep a pending phase approval through another phase's rounds and queue that phase's stops quietly."`

---

### Task 3: Plan while the approval waits

**Depends on:** Task 2

**Files:**
- Create: `agentflow/speculation.py`
- Modify: `agentflow/pipeline.py`:
  - `_Loop.phase`, `settle`, `executor`, `go`;
  - new `speculate`, `catch_up_handoffs` and `tell_speculative`;
  - `approval_text`, `_artifacts`.
- Modify: `agentflow/flow_control.py` (`Executor.yield_if`, `Executor.speculating`, `run_step`)
- Modify: `agentflow/lanes.py` (the output fence)
- Modify: `agentflow/gitops.py` (`changed_between`)
- Modify: `agentflow/dashboard.py` (`_rests`)
- Modify: `CHANGELOG.md`
- Test: `agentflow/tests/test_speculation.py` (new; the `flow` fixture of `test_doubt_flow.py` with `autonomy: {speculate: [plan]}` in the patch, and its `begin`/`go` helpers copied, not imported)

**Interfaces:**
- Consumes: `state.speculating` (Task 2).
- Produces, in `speculation.py`:
  - `OUTCOMES = ("finished", "stopped", "approved", "rejected", "halted")`;
  - `next_phase(cfg: Config, names: list[str], index: int, lane: str) -> str | None`: the first later phase with steps for `lane`, if it's in `autonomy.speculate`;
  - `due(cfg: Config, run: Run, names: list[str], index: int, to_phase: str | None) -> str | None`: `next_phase`, or None when `to_phase == names[index]`, the run is `marked_done`, or `blocking_of(cfg, phase_findings(cfg, run, names[index]))` isn't empty;
  - `basis(phase: Phase, lane: str, ctx: Ctx, worktree: Path) -> dict[str, str]`: `doubt.artifact_digest` of each formatted `output` of the phase's top-level steps that have `commit: true`, for `lane`;
  - `last_record(events: list[dict], phase: str) -> tuple[dict | None, bool]`: the last `speculation_started` for `phase`, and whether a `speculation_ended` came after it;
  - `cut(state: dict, after: str) -> str | None`: `"rejected"` when `state["refine"]["phase"] == after`, `"ready"` when `state["ready"]["phase"] == after`, else None;
  - `page_line(after: str, phase: str) -> str`.
- Produces, in `pipeline.py`:
  - `_Loop.speculate(names, index, executor) -> str | None`;
  - `_Loop.catch_up_handoffs(names, index, executor) -> None`: `hand_off` for each earlier approved phase with no handoff;
  - `_Loop.tell_speculative() -> None`: at the start of `go`, `executor._tell_once(wait)` for a current wait that carries `speculative`.
- Produces: `gitops.changed_between(worktree: Path, a: str, b: str) -> list[str]`.
- `_Loop.speculate` in order:
  1. `nxt = due(...)`. If it's None or this is a dry run, return `"awaiting_approval"`.
  2. `basis(...)`. If `last_record` has the same basis and has ended, return `"awaiting_approval"`, because a revision is speculated only once.
  3. If there's no record with this basis: `write_handoff(cfg, run, after)` (provisional, for `{handoff}`; no `handoff_written`), then `speculation_started{phase: nxt, after, basis, sha: head}`. Task 4 inserts its re-plan before this event.
  4. Set `self._speculating = nxt` and `self._yield_if = lambda st: cut(st, after)`. Then `outcome = self.phase(names, names.index(nxt))`, and clear both in a `finally`.
  5. Refresh, then emit `speculation_ended` with the outcome:
     - `finished`: still pending, and the phase settled;
     - `stopped`: still pending, with a queued wait;
     - `halted`: paused or cancelled;
     - `rejected`: `cut` is `"rejected"`. Then `return self.phase(names, index)`, which runs the refine round now;
     - `approved`: otherwise. Then return `outcome`.

     In the pending outcomes, return `"awaiting_approval"`. In `halted`, return the status.
- `_Loop.phase`: when the wait is a phase approval of `names[index]`, return `self.speculate(names, index, executor)` instead of `"awaiting_approval"`, so a resumed runner picks a speculation up.
- `settle`:
  - first `catch_up_handoffs`;
  - every exit that would emit `phase_approved` or call `raise_phase_approval` returns `"awaiting_approval"` while `state.speculating(run.state)` names this phase. Reconcile and refine rounds before those exits run unchanged;
  - after `raise_phase_approval`, it returns `self.speculate(...)`.
- `Executor.run_step`: right after the `halted` check, `if self.yield_if is not None and self.yield_if(state): return "stopped", False`.
- The output fence: every `step.reconciles` test in `lanes.py` that guards the output (around lines 862, 873, 992, 1220, 1387 and 1403) tests `self._fenced(sr.step, key)` instead. That is `step.reconciles or (bool(step.output) and self.speculating == key.split("/")[0])`. Other uses of `reconciles` (dispositions, doubt) stay as they are.
- `approval_text`: after `_approval_note`, add `page_line(phase, nxt)` when `due(cfg, run, list(cfg.phases), list(cfg.phases).index(phase), run.state["options"].get("to_phase"))` gives `nxt`. The text is: ``While you review this, the runner drafts `<nxt>` from it. Approving keeps that work; rejecting re-plans it on your revision.``
- `_artifacts(run, phase)` uses `changed_between(worktree, start_sha, approval_sha)` when the phase row is `approved` and has an `approval_sha`, so define's handoff never lists the plan.
- `dashboard._rests` returns False while `state.speculating(run.state)` is the following phase: that work is already running.

- [ ] **Step 1: Write the failing tests** in `test_speculation.py`. In each, `define` reaches its approval clean (`REVIEW: [found(LOW)]`), and `PLAN_TEXT` is written by `plan/plan`.
  ```python
  def test_the_plan_is_written_while_the_spec_waits(flow):
      run = begin(flow, {...}, to=None); assert go(flow, run) == "awaiting_approval"
      assert run.state["waiting"]["key"] == "define_approved"
      assert [c for c in called(flow, "plan/")] == ["plan/plan", "plan/plan_review"]
      assert events(run, "speculation_ended")[-1]["outcome"] == "finished"
      assert not called(flow, "build/")                                    # build never starts before the approval
  def test_approving_after_speculation_keeps_the_plan(flow):
      # approve(run, "define", by="me"); go -> plan is not called again; plan settles (its approval, as the fixture says)
      assert called(flow, "plan/").count("plan/plan") == 1
  def test_approving_mid_speculation_goes_on_into_build(flow, monkeypatch):   # Review Focus 1
      # wrap Executor.run_step: right after "plan/plan" returns, approve(run, "define", by="me")
      # plan settles in the same advance; handoff_written(define) comes before plan's phase_approval_raised or approval
  def test_a_speculative_stop_waits_quietly_behind_the_spec(flow, monkeypatch):  # Review Focus 3
      # plan/plan answers needs_human with a spec_gap question of kind boundary; notices recorded
      assert run.state["waiting"]["key"] == "define_approved" and notices == []
      # approve define, go -> the question is the wait, one notice, hitl_notified once; go again -> no second notice
  def test_a_runner_that_died_mid_speculation_resumes_it(flow, monkeypatch):    # Review Focus 4
      # make plan/plan_review raise KeyboardInterrupt once; go again ->
      assert len(events(run, "speculation_started")) == 1 and "plan/plan_review" in called(flow, "plan/")
  def test_no_speculation_when_the_review_left_a_blocking_finding(flow): ...    # rounds exhausted -> no speculation_started
  def test_no_speculation_with_the_default_config(flow_repo): ...               # autonomy.speculate [] -> no plan/ call before approval
  def test_a_speculative_step_that_edits_code_stops(flow):
      # plan/plan also writes src/one.py -> guard_violation outside_output; nothing committed outside the plan
  def test_the_spec_page_says_the_plan_is_being_drafted(flow): ...             # page_line text in approval_text(define)
  def test_the_spec_handoff_lists_only_the_spec(flow): ...                      # after approval, handoffs/define.md has no plan path
  ```
  In `test_dashboard.py`, add `test_an_approval_does_not_rest_a_speculating_run`.
- [ ] **Step 2: Run them and see them fail.**
- [ ] **Step 3: Implement `speculation.py`, `gitops.changed_between` and the `Executor` hook.**
- [ ] **Step 4: Wire `_Loop`, the fence, `approval_text`, `_artifacts` and `_rests`** as in Interfaces.
- [ ] **Step 5: Run `test_speculation.py` and see it pass,** together with `test_pipeline.py`, `test_doubt_flow.py`, `test_feature_lane.py`, `test_dashboard.py` and `test_portability.py`, and check that the `explain` goldens are unchanged (`git status agentflow/tests/fixtures/explain`).
- [ ] **Step 6: Commit:** `git commit -m "Run the phase after an approval while the approval waits, and keep its work when the approval comes."` Add a CHANGELOG `Added` line.

---

### Task 4: Re-plan on a changed spec

**Depends on:** Task 3

**Files:**
- Modify: `agentflow/speculation.py` (`planned_on`, `replan_due`, `replan_note`)
- Modify: `agentflow/pipeline.py` (`_Loop.speculate` step 3, `_Loop.phase`)
- Test: `agentflow/tests/test_speculation.py`

**Interfaces:**
- Produces:
  - `REPLAN = "replan"`;
  - `planned_on(events: list[dict], phase: str) -> tuple[dict[str, str], str] | None`: the `(basis, sha)` of the last `speculation_started` or `phase_rejected{kind: replan}` for `phase`;
  - `replan_due(state: dict, events: list[dict], phase: str, basis: dict[str, str]) -> bool`: the phase row has started (it isn't missing, `pending` or `skipped`), `planned_on` isn't None, and its basis differs from `basis`;
  - `replan_note(paths: list[str], sha: str) -> str`. The exact copy is: ``The {paths} changed after this phase's work was written (at {sha[:12]}). Read the change with `git diff {sha[:12]} HEAD -- {paths}`, then update what this phase wrote to match it; keep what still holds.`` Paths are backticked and comma-joined.
- `_Loop.replan(names, index, basis)` runs `open_refine(run, phase, AUTO_BY, replan_note(changed, old_sha), extra={"kind": REPLAN, "basis": basis, "from_sha": old_sha})`. `changed` is the paths whose digest differs. Then it refreshes.
  - In `speculate` step 3: when `replan_due(...)` holds, `replan` runs before `speculation_started`.
  - In `phase()`: before the walk, `replan` runs when the phase isn't being speculated and `replan_due` holds against the basis of the approved phase named by the last `speculation_started`'s `after`.

- [ ] **Step 1: Write the failing tests.**
  ```python
  def test_rejecting_mid_speculation_refines_the_spec_then_replans(flow, monkeypatch):   # Review Focus 2
      # after "plan/plan" returns, reject(run, "define", "me", "Thêm rollback"); define's refine act edits the spec
      assert go(flow, run) == "awaiting_approval"            # the same runner ran define's round and raised it again
      (replan,) = [e for e in events(run, "phase_rejected") if e.get("kind") == "replan"]
      assert "git diff" in replan["note"] and SPEC in replan["note"]
      assert len(events(run, "speculation_started")) == 2 and run.state["waiting_queue"] == []
  def test_a_rejection_that_leaves_the_spec_as_it_was_keeps_the_plan(flow): ...  # same digest -> no replan, plan/plan called once
  def test_a_spec_changed_without_a_new_speculation_is_replanned_after_approval(flow): ...
      # second raise has a blocking finding left -> no speculation; approve -> one replan round, then plan settles
  def test_a_replan_is_not_counted_as_a_finding_round(flow): ...                 # plan with needs_approval: false keeps its finding_rounds
  ```
- [ ] **Step 2: Run them and see them fail.**
- [ ] **Step 3: Implement it** as in Interfaces.
- [ ] **Step 4: Run `test_speculation.py`, `test_doubt_flow.py` and `test_pipeline.py` and see them pass.**
- [ ] **Step 5: Commit:** `git commit -m "Re-plan a speculated phase once when the approved artifact changed under it."`

---

### Task 5: The approval ramp

**Depends on:** Tasks 1 and 3

**Files:**
- Create: `agentflow/approval_ramp.py`
- Modify: `agentflow/pipeline.py` (`settle`, the `needs_approval` branch)
- Modify: `agentflow/conformance.py` (`_approval`)
- Modify: `CHANGELOG.md`
- Test: `agentflow/tests/test_approval_ramp.py` (new), `agentflow/tests/test_conformance.py`

**Interfaces:**
- Produces, in `approval_ramp.py`:
  - `CHECKS`, `RAMP_PROFILES` (Global Constraints);
  - `@dataclass(frozen=True) class Sample: run_id: str; ended: datetime; auto: bool; agree: bool`;
  - `verdict(cfg: Config, run: Run, phase: str, ctx: Ctx) -> tuple[list[str], list[str]]`: `(held, missing)` in `CHECKS` order;
  - `samples(runs: list[Run], lane: str, profile: str, phase: str) -> list[Sample]`, oldest first. A run gives a sample when its first `would_auto_approve` for `phase` with `verdict: true` has the same `profile`.
    - Human sample (`auto=False`): `agree` is True when the first decision on the phase after that event is a `phase_approved` whose `by` isn't `AUTO_BY`, and False when it's a `phase_rejected` whose `by` isn't `AUTO_BY`. A run with neither gives no sample.
    - Auto sample (`auto=True`, a `phase_approved` with `ramp`): `agree` is False when the run's last `conformance_checked` has a `missing` obligation.
  - `eligible(repo: Path, cfg: Config, lane: str, profile: str, phase: str, now: datetime) -> tuple[bool, str, int]`: `(ok, why, agreeing)`. It reads `retro.finished_runs(repo, None)`. The checks, in order:
    1. the last `ramp_samples` human samples all agree, and there are at least that many. Otherwise `why` is `"<k> of <n> agreeing human decisions"`;
    2. no auto sample after the oldest of them disagrees. Otherwise `"an auto-approved run since missed an obligation"`;
    3. `scorecard.load_baseline(repo)` isn't None. Otherwise `"no scorecard baseline"`;
    4. the lane's `conformance_missing` row in `scorecard_view` doesn't have the verdict `worse`. Otherwise `"conformance is worse than the baseline"`.
  - `decide(cfg: Config, run: Run, phase: str, ctx: Ctx, now: datetime) -> dict | None`:
    - None for mode `off`;
    - otherwise it emits `would_auto_approve` and returns `{"auto": bool, "samples": int, "profile": str}`;
    - `auto` is True only under mode `auto`, with `verdict` True, a phase not in `NEVER_AUTO`, and `eligible` ok. `eligible` and `why` are on the event only under `auto`.
  - `ramp_view(repo: Path, cfg: Config, now: datetime) -> list[dict]`: one row per (lane, phase in `autonomy.approvals`, profile in `RAMP_PROFILES`) that has samples: `{lane, phase, profile, mode, human, agreeing, auto, regressions, eligible, why}`.
- `settle`: in the `needs_approval` branch, after doubt settles and before `raise_phase_approval`, it calls `decision = approval_ramp.decide(...)`. When `decision["auto"]`, it emits `phase_approved(by=AUTO_BY, phase=name, sha=head(worktree), ramp={"samples": n, "profile": p})`, calls `hand_off` and returns None. Task 3's speculating exit comes first.
- `conformance._approval` takes the last `phase_approved` with a `ramp` field and `by == AUTO_BY` as `_Item(True, [seq], RAMP_APPROVAL)`, where `RAMP_APPROVAL = "approval ramp"`. The blocking-findings override in `_step` stays human-only.

- [ ] **Step 1: Write the failing tests** in `test_approval_ramp.py`. Build finished runs with `Run.create` in the tmp state dir and emit their events by hand. Freeze a baseline with `scorecard.freeze` where it's needed.
  ```python
  def test_observe_records_and_never_approves(flow): ...  # would_auto_approve{mode: observe}; the run still awaits approval
  def test_verdict_lists_what_held_and_what_did_not(flow): ...  # route M, open low finding -> missing == ["small_route"]
  def test_samples_count_only_a_persons_decision(tmp_path): ...  # rejected -> agree False; never decided -> no sample
  def test_auto_approves_after_n_agreeing_runs_and_a_green_scorecard(flow, ...):
      # 5 agreeing human samples, a baseline, route S -> phase_approved{by: "runner", ramp: {samples: 5, profile: "S"}}; plan starts
  @pytest.mark.parametrize("case", ["security_path", "four_samples", "one_rejection", "no_baseline", "auto_regressed"])
  def test_auto_falls_back_to_a_person(flow, case): ...   # Review Focus 5: awaiting_approval; would_auto_approve eligible False, why set
  def test_the_ramps_own_approvals_never_promote_it(tmp_path): ...  # 5 auto samples + 0 human -> not eligible
  def test_a_phase_rejected_once_is_never_auto(flow): ...  # first_raise missing on the second raise
  def test_ramp_view_rows(tmp_path): ...
  ```
  In `test_conformance.py`, add `test_a_ramp_approval_is_evidence_for_approval` and `test_a_plain_runner_approval_is_not`.
- [ ] **Step 2: Run them and see them fail.**
- [ ] **Step 3: Implement `approval_ramp.py`, the `settle` call and `_approval`.** Build `verdict`'s `no_open_findings` from `blocking_of(cfg, phase_findings(...))`, `small_route` from `run.state.get("route")`, `no_security_overlay` from `overlays.security_path(cfg, ctx)`, and `first_raise` from `phase_rejected` events whose `by` isn't `AUTO_BY`.
- [ ] **Step 4: Run `test_approval_ramp.py`, `test_conformance.py`, `test_pipeline.py` and `test_speculation.py` and see them pass.**
- [ ] **Step 5: Commit:** `git commit -m "Record whether the runner would approve a phase, and approve it by itself once enough people agreed."` Add a CHANGELOG `Added` line.

---

### Task 6: Signals and the scorecard

**Depends on:** Tasks 4 and 5

**Files:**
- Modify: `agentflow/signals.py` (`FAMILIES`, `run_signals`, the docstring)
- Modify: `agentflow/scorecard.py` (`scorecard_view`, `scorecard_table`)
- Test: `agentflow/tests/test_signals.py`, `agentflow/tests/test_scorecard.py`

**Interfaces:**
- New families:
  - `"replans": ("lower", False)`: the `phase_rejected` events of kind `replan`. Present only for a run with a `speculation_started`;
  - `"ramp_agreement": ("higher", True)`, keyed by phase: 1.0 or 0.0 per human sample, the same rule as `approval_ramp.samples`. Present only for a phase with a sample;
  - `"auto_approvals": ("higher", False)`: the `phase_approved` events with `ramp`. Present only for a run with a `would_auto_approve`.
- `refine_rounds:<p>` leaves out kind `replan`, as it leaves out `reconcile`.
- `scorecard_view(...)` gains `"ramp": approval_ramp.ramp_view(repo, cfg, now)` when a `cfg` keyword is passed, which `flow.py`'s `scorecard` command does. `scorecard_table` prints an `Approval ramp` table after the lanes when the list isn't empty. The columns are `lane phase profile mode human agreeing auto regressions eligible`, and `why` goes under a row that isn't eligible.

- [ ] **Step 1: Write the failing tests.**
  ```python
  def test_replans_and_auto_approvals_are_counted(): ...           # from hand-built events
  def test_ramp_agreement_is_one_per_agreeing_decision(): ...
  def test_a_replan_is_no_refine_round(): ...
  def test_signals_of_a_run_without_t7_events_are_unchanged(): ...  # no replans / ramp_agreement / auto_approvals keys
  def test_the_scorecard_prints_the_approval_ramp(tmp_path): ...    # table text holds "Approval ramp" and the row
  ```
- [ ] **Step 2: Run them and see them fail.**
- [ ] **Step 3: Implement it,** with one docstring line per new family.
- [ ] **Step 4: Run `test_signals.py` and `test_scorecard.py` and see them pass.**
- [ ] **Step 5: Commit:** `git commit -m "Count re-plans and ramp approvals, and show the approval ramp in the scorecard."`

---

### Task 7: Docs and the release

**Depends on:** Task 6

**Files:**
- Modify: `docs/agentflow-playbook.md` (§8 "Operating a run": a "Speculative phases" and an "Approval ramp" paragraph, next to the waves paragraph)
- Modify: `docs/specs/agentflow-throughput-proposal.md` (§8 T7 row's Status cell only)
- Modify: `pyproject.toml`, `agentflow/pack.py` (`ENGINE_VERSION`), `CHANGELOG.md`

- [ ] **Step 1: Playbook.** Write the two §8 paragraphs:
  - Speculative phases: what runs while the spec waits; what approve, reject, pause and hold do then; the re-plan note.
  - Approval ramp: off, observe and auto; the four checks; how promotion and demotion work; `agentflow scorecard`'s table; the two kinds of approval that never go auto.
- [ ] **Step 2: Proposal.** Set the T7 Status cell to `shipped 2026-11-22; autonomy.speculate and autonomy.approvals default off`.
- [ ] **Step 3: Release.**
  - Raise the version to the next minor past `main` (3.3 → 3.4) in both `pyproject.toml` and `pack.ENGINE_VERSION`.
  - Rename `## [Unreleased]` to `## [3.4] - <today>` and open an empty `## [Unreleased]` above it.
  - Run `test_packaging.py`. Expected: PASS.
- [ ] **Step 4: Full verification.** Run:
  - the full suite: all pass;
  - `uvx ruff@0.17.4 check .`: `All checks passed!`;
  - `bin/agentflow validate`: `0 warnings`;
  - `git status agentflow/tests/fixtures/explain`: clean.
- [ ] **Step 5: Commit:** `git commit -m "Document speculative phases and the approval ramp, and release engine 3.4."`
