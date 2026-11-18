# Throughput slice T3 (Route profiles) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Size the route to the task. A one-module change stops paying an epic's ceremony:
- a short read-only **triage** session at run start answers the sizer's checklist plus two estimates;
- engine code picks a **route profile** (`XS`, `S`, `M` or `L`), and the session may only propose a higher one;
- the profile skips steps, caps the doubt rounds of each phase, and drops per-task review for tasks with no risk;
- the runner upgrades the profile mid-flight when the plan turns out bigger or riskier, and never downgrades;
- per-task reviews run at `medium` unless the plan sizer flagged `risk`.

**Architecture:**
- **Profiles are pack data.** A new workflow block, `route: {mode, lanes, profiles}`. Each profile is `{skip, doubt_rounds, per_task_review}`. The engine defaults carry the grid of spec §4.3.1. `route` joins `layers.WORKFLOW_KEYS`, because a profile names the workflow's step keys: a pack that writes its own `phases` writes its own `route`.
- **The rule is engine code.** `agentflow/route.py` is pure, like `sizing.py`. It turns facts (the six answers, `tasks_expected`, `files_expected`, the security overlay) into a profile. It accepts a proposal only when it is higher, and upgrades on plan facts.
- **The triage session.** `route_lane.RouteLane` is a mixin of `Executor`, like `SizingLane`, whose `_sizer_call` it reuses. So the triage is read-only, and any edit stops the run as `sizer_edit`. It runs once, before the first phase, and records `route_profile`.
- **Applying it.** These read the last `route_profile`, and only when `route.mode: on`:
  - `flow_control._should_run` skips a step;
  - `autonomy.round_limit` caps rounds per phase;
  - `feature_lane._run_task` drops a task's review;
  - `size_plan` tiers review effort.
- **Observe first.** `route.mode` is `off` (default: no session, no event, today's run byte for byte), `observe` (decide and record, `applied: false`, run the full route) or `on`.

**Tech Stack:** Python 3.12 stdlib, PyYAML, pytest. No new dependency.

**Spec:** `docs/specs/agentflow-throughput-proposal.md`. This plan covers slice **T3** (§8):
- §4.3.1: profiles as pack data, the rule, proposals only upward, the route validator, mid-flight upgrade, the doubt-round stop rule;
- §4.3.2: review tiering;
- §6: the `route_profile` event;
- verification "T3".

Out of scope:
- the M profile's **wave review** (§4.3.2's second half, and §4.4.4). It needs T5's waves, so in T3 profile M keeps per-task review;
- speculative planning and the `observe → auto` approval ramp (T7);
- showing the profile on the dashboard.

## Global Constraints

- Engine dependencies stay stdlib + `pyyaml>=6`.
- No project strings in the engine (`test_portability.py` `DENYLIST`). Don't name the repository §1 measured: call it "the measured repository".
- The golden files `agentflow/tests/fixtures/explain/*.txt` stay byte-identical. `explain` without `--profile` prints nothing new.
- **Off means today.** With `route.mode: off` (the engine default), no triage session runs, no `route_profile` is written, and every step, round cap, review and effort level is what it is today.
- **Profiles only narrow.** A profile's round cap is `min(doubt_rounds[phase], autonomy.finding_rounds)`, and never below 1. A profile never adds a step, a round or a review.
- **The rule only goes up after it picks.** The session's proposal counts only when it is higher than the rule's pick. A mid-flight change only goes up. A missing or unusable answer falls back to `L`, the full route: over-sizing is the safe direction (§6b).
- **The engine floor holds in every profile:**
  - `doubt_rounds` values are whole numbers ≥ 1;
  - a profile may not skip a `type: finish` or `type: ci` step, a `foreach` step, a step an obligation for a route lane names (`step:<id>`), or the last step of a phase whose approval an obligation requires (`approval:<phase>`);
  - the judgement-step effort floor (`sizing.floor`) applies to the triage and to tiered reviews.
- **Risky work always gets its own review** (decision 2). In every profile, a task keeps its per-task review when:
  - its plan-sizer answers flag `risk`;
  - the security overlay applies;
  - it has no plan-sizer answers (sizing off, or a fallback).
- Every new config key ships in its own task with all four companions: a Settings form field or a `_PROTECTED_KEYS` entry, `validate` checks, a `test_config_edit.py` case, and a playbook §14 line.
- The new prompt `agentflow/prompts/route.md` only adds text. No existing prompt text is dropped or reworded, so no `pack_fix.py` `Retired` entry is needed.
- Model text shown to a human (the triage's `reason`) is cut to `sizing.REASON_MAX` and goes through `hitl.clean_fact`. It never reaches a commit message.
- New event, with its `state.REQUIRED` entry:

  | Event | Fields | `state.REQUIRED` |
  |---|---|---|
  | `route_profile` | `{profile, applied, proposed_by, upgraded_from, facts, reason}` | `("profile",)` |

  - `proposed_by` is one of `rule`, `sizer`, `fallback` or `runner`. It is `runner` for an upgrade.
  - `facts` is `{answers, tasks, files, security}`.
- Each user-visible change adds a line under `## [Unreleased]` in `CHANGELOG.md`.
- Tests run as `uv run --no-project --with pyyaml --with pytest pytest <path> --import-mode=prepend -q`. Run `uvx ruff@0.17.4 check .` before each commit. Never create or edit `uv.lock`.

## Review Focus

1. **A triage session that edits the repository.** It must stop the run as `sizer_edit`, exactly like a step's sizer. After a human restores the tree and approves, the triage runs again, and the first phase never starts before it. (Task 3: `test_a_triage_that_edits_stops_and_runs_again_after_the_reset`.)
2. **A runner killed during triage.** On resume, the open sizer is waited for and compared before anything runs (`check_open_sizers`), and a run with a `route_profile` already recorded never triages again. (Task 3: `test_a_resumed_run_triages_once`.)
3. **A profile chosen by a session that lies.** For example, `files_expected: -1`, `tasks_expected: "two"`, `propose: "XXS"`, or `propose: "XS"` when the rule picked `M`. Bad numbers count as unknown, which rules out XS and S. A bad or downward proposal is ignored. (Task 2: `test_bad_estimates_count_as_unknown`, `test_a_proposal_only_raises`.)
4. **A config reload mid-run that drops the active profile,** or that names a step that is gone. An unknown profile name applies nothing, which means today's route. A skip entry that names no step skips nothing. (Task 4: `test_an_unknown_profile_runs_the_full_route`.)
5. **A plan that grows after the triage said S.** The build phase must upgrade before its first task, so per-task review is back for a 9-task plan. (Task 5: `test_a_plan_past_the_s_cap_upgrades_to_m_before_build`.)

---

### Task 0: The baseline exists

- [ ] **Step 1: Check the baseline.** Run `bin/agentflow scorecard`. Its header names a frozen baseline dated before T1's first commit. If there is none, stop and ask the human to run `agentflow scorecard --freeze`. T3 must not freeze one itself (spec §6).

---

### Task 1: The `route` block: config, defaults, validation and Settings

**Files:**
- Modify:
  - `agentflow/config.py`: the new dataclasses, `Config.route`, loading, `_Validator.route()` (called from `validate`);
  - `agentflow/layers.py`: `"route"` in `WORKFLOW_KEYS`;
  - `agentflow/defaults/config.yaml`: the `route` block, after `autonomy:`;
  - `agentflow/config_edit.py`: a form field and a protected key;
  - `docs/agentflow-playbook.md` §14;
  - `CHANGELOG.md`.
- Test: `agentflow/tests/test_config.py`, `agentflow/tests/test_layers.py`, `agentflow/tests/test_config_edit.py`

**Interfaces:**
- Produces, in `config.py`:
  - `ROUTE_PROFILES = ("XS", "S", "M", "L")`, smallest first;
  - `ROUTE_MODES = ("off", "observe", "on")`;
  - `@dataclass(frozen=True) class RouteProfile`, with fields:
    - `skip: tuple[str, ...] = ()`;
    - `doubt_rounds: Mapping[str, int] = field(default_factory=dict)`;
    - `per_task_review: bool = True`;
  - `@dataclass class Route`, with fields:
    - `mode: str = "off"`;
    - `lanes: list[str] = field(default_factory=lambda: ["feature"])`;
    - `profiles: dict[str, RouteProfile] = field(default_factory=dict)`;
  - `Config.route: Route = field(default_factory=Route)`.
- The engine default grid (spec §4.3.1, on the default workflow's step keys):
  ```yaml
  route:
    mode: "off"     # off | observe (decide and record a profile, run the full route) | on (apply it)
    lanes: [feature]
    profiles:
      XS: {skip: [define/grill_open_questions, define/spec_review, plan/plan_review], per_task_review: false}
      S:  {doubt_rounds: {define: 1, plan: 1}, per_task_review: false}
      M:  {doubt_rounds: {define: 2, plan: 1}}
      L:  {}
  ```
  Quote `"off"`: YAML 1.1 reads a bare `off` as `false`.
- `validate` rules. Each one is an error unless it says warning:
  - `route.mode` must be one of `ROUTE_MODES`;
  - each of `route.lanes` must be in `lanes`;
  - a profile name must be one of `ROUTE_PROFILES`, and a profile's key must be `skip`, `doubt_rounds` or `per_task_review`;
  - `skip` entry `<phase>/<id>` must name a top-level step of that phase;
  - a skip entry may not name:
    - a step with `type` `finish` or `ci`;
    - a `foreach` step;
    - a step whose id is `step:<id>` evidence of an obligation whose `lanes` is empty or meets `route.lanes`;
  - a profile may not skip every step a route lane walks in a phase whose `approval:<phase>` such an obligation requires;
  - the error for a dropped obligation says: `route.profiles.<P>.skip: skipping <key> drops the obligation <id>`;
  - `doubt_rounds` keys must be phases, and the values whole numbers ≥ 1. The message names the floor: "the engine floor keeps at least one round";
  - `per_task_review` must be a bool;
  - warning: `mode` is not `off` and `profiles` is empty. The text: "route.mode is on but route.profiles is empty: every profile runs the full route".
- Settings:
  - `(("route", "mode"), "Route profiles", {"kind": "enum", "options": list(ROUTE_MODES)})`, in a new section `("Route", "How much ceremony a run's route gets, sized at its start.", [...])`;
  - `"route.lanes"` and `"route.profiles"` in `_PROTECTED_KEYS`. Profiles carry the floor, and lanes decide which runs pay a triage session.

- [ ] **Step 1: Write the failing tests.**

  In `test_config.py`:
  - `test_route_defaults_are_off_with_the_grid`: on a sparse pack, `cfg.route.mode == "off"`, `set(cfg.route.profiles) == {"XS", "S", "M", "L"}`, `cfg.route.profiles["S"].doubt_rounds == {"define": 1, "plan": 1}` and `cfg.route.profiles["XS"].per_task_review is False`.
  - `test_the_python_pack_has_no_profiles`: the fixture pack writes its workflow, so `cfg.route.profiles == {}` and `cfg.route.mode == "off"`.
  - `test_route_rejects_bad_values` (parametrized), each an error at the dotted key:
    - `mode: sometimes`;
    - `lanes: [nope]`;
    - profile `XL`;
    - profile key `grill`;
    - `skip: [define/nope]`;
    - `doubt_rounds: {define: 0}`;
    - `doubt_rounds: {nope: 1}`;
    - `per_task_review: "no"`.
  - `test_a_profile_may_not_drop_an_obligation`: skipping `verify/verify` errors with `"drops the obligation verified"`, and skipping `ship/finish` is an error.
  - `test_skipping_every_plan_step_drops_plan_approved`: a profile skipping `plan/plan`, `plan/plan_reconcile` and `plan/plan_review` (every step the feature lane walks in `plan`) errors with `"plan_approved"`. Skipping only `plan/plan_review` is fine.
  - `test_route_on_with_no_profiles_warns`.

  In `test_layers.py`:
  - `test_a_pack_that_writes_phases_drops_the_default_route`.

  In `test_config_edit.py`:
  - `test_route_mode_is_a_form_field`;
  - `test_route_profiles_are_protected`: proposing a change to `route.profiles.S.per_task_review` raises `Refused` matching `"protected"`.
- [ ] **Step 2: Run them and see them fail** on the missing attribute and the unknown key.
- [ ] **Step 3: Implement** the dataclasses, loading, the defaults block, `WORKFLOW_KEYS`, `_Validator.route()` (read obligations from `cfg.policy.obligations`, and each one's `require` evidence) and the Settings entries.
- [ ] **Step 4: Run** `test_config.py`, `test_layers.py`, `test_config_edit.py` and `test_explain_golden.py`. All pass. `bin/agentflow validate` prints "0 warnings".
- [ ] **Step 5: Docs.**
  - Playbook §14 gets three lines: `route.mode`, `route.lanes` and `route.profiles`. The `route.profiles` line covers the three profile keys, the floor and the default grid, and says that a pack that writes `phases` writes its own `route`.
  - Add a `CHANGELOG.md` `Added` line.
- [ ] **Step 6: Commit** "Add the route block: profiles that skip steps, cap doubt rounds and drop per-task review, off by default."

---

### Task 2: The rule

**Files:**
- Create: `agentflow/route.py`
- Test: `agentflow/tests/test_route.py`

**Interfaces:**
- Consumes:
  - `sizing.parse_answers`, `sizing.flags` and `sizing.RISK_QUESTIONS`;
  - `config.ROUTE_PROFILES`, `RouteProfile` and `Config`.
- Produces (pure: no file, no clock):
  - `XS_MAX_FILES = 2`, `S_MAX_TASKS = 3`, `L_MIN_TASKS = 12`, `FALLBACK = "L"`, `ESTIMATE_MAX = 1000`;
  - `@dataclass(frozen=True) class Facts`, with fields:
    - `answers: dict[str, str] | None`;
    - `tasks: int | None`;
    - `files: int | None`;
    - `security: bool`;
    - and a method `as_event() -> dict`;
  - `facts_from(raw: object, security: bool) -> Facts`. It reads `answers`, `tasks_expected` and `files_expected`. An estimate that is not an `int` (a `bool` counts as not an int) in `0..ESTIMATE_MAX` becomes `None`;
  - `pick(facts: Facts) -> str`. Rules are checked in order, and the first match wins:
    1. `facts.answers is None` → `FALLBACK`;
    2. `security`, or `flags["risk"]`, or `tasks >= L_MIN_TASKS` → `L`;
    3. `flags["mechanical"]`, no `RISK_QUESTIONS` flag, and `files is not None and files <= XS_MAX_FILES` → `XS`;
    4. `tasks is not None and tasks <= S_MAX_TASKS`, and neither `interface` nor `risk` flagged → `S`;
    5. otherwise `M`.

    `sizing.flags` already counts `unsure` as a yes for a risk question and as a no for `mechanical`;
  - `higher(a: str, b: str) -> str`, by `ROUTE_PROFILES` order;
  - `decide(facts: Facts, proposed: object) -> tuple[str, str]` returns `(profile, proposed_by)`:
    - `("L", "fallback")` with no answers;
    - `(proposed, "sizer")` when `proposed` is a profile name higher than `pick`;
    - otherwise `(pick, "rule")`;
  - `upgrade(current: str, *, tasks: int | None, risk: bool, security: bool) -> str | None`:
    - the target is `L` for `security`, `risk` or `tasks >= L_MIN_TASKS`;
    - otherwise it is `M` for `tasks > S_MAX_TASKS`;
    - otherwise it is `current`;
    - return the target when it is higher than `current`, else `None`;
  - `last_record(events: list[dict]) -> dict | None`: the last `route_profile` event;
  - `applied(cfg: Config, record: dict | None) -> RouteProfile | None`: the profile to apply. It is `None` unless `cfg.route.mode == "on"`, `record["applied"]` is true, and `record["profile"]` is in `cfg.route.profiles`. So a run triaged under `observe` stays on the full route even if the pack switches to `on` mid-run;
  - `skips(cfg: Config, record: dict | None, key: str) -> str | None`: the profile name when the applied profile's `skip` holds `key`;
  - `round_cap(cfg: Config, record: dict | None, phase: str) -> int | None`: the applied profile's `doubt_rounds[phase]`, or `None`;
  - `reviews_task(profile: RouteProfile | None, answers: dict[str, str] | None, security: bool) -> bool`. True when:
    - `profile` is `None`; or
    - `profile.per_task_review`; or
    - `answers is None`; or
    - `security`; or
    - `sizing.flags(answers)["risk"]`.

- [ ] **Step 1: Write the failing tests** in `test_route.py`. `NO` is the six answers all `no`, and `facts(**kw)` builds a `Facts`.
  - `test_pick_follows_the_grid` (parametrized):

    | Facts | Profile |
    |---|---|
    | `NO \| {mechanical: yes}`, files 2, tasks 1 | `XS` |
    | `NO \| {mechanical: yes}`, files 3, tasks 1 | `S` |
    | `NO`, tasks 3 | `S` |
    | `NO \| {interface: yes}`, tasks 2 | `M` |
    | `NO`, tasks 4 | `M` |
    | `NO`, tasks 12 | `L` |
    | `NO \| {risk: unsure}`, tasks 1 | `L` |
    | `NO \| {mechanical: yes}`, files 1, security | `L` |
    | `NO \| {mechanical: yes, novelty: yes}`, files 1, tasks 1 | `S` |
  - `test_bad_estimates_count_as_unknown`: `facts_from({"answers": NO_RAW, "tasks_expected": "two", "files_expected": -1}, False)` has `tasks is None and files is None`. `True` and `10_001` are also `None`. With `mechanical: yes`, `pick` is `M`: unknown rules out both XS and S.
  - `test_no_answers_fall_back_to_l`: `decide(facts_from({}, False), None) == ("L", "fallback")`.
  - `test_a_proposal_only_raises`. For facts that pick `M`:
    - `decide(f, "L") == ("L", "sizer")`;
    - `decide(f, "XS") == ("M", "rule")`;
    - `decide(f, "XXS") == ("M", "rule")`;
    - `decide(f, 3) == ("M", "rule")`.
  - `test_upgrade_never_goes_down`:
    - `upgrade("S", tasks=9, risk=False, security=False) == "M"`;
    - `upgrade("M", tasks=12, ...) == "L"`;
    - `upgrade("S", tasks=2, risk=True, ...) == "L"`;
    - `upgrade("L", tasks=1, ...) is None`;
    - `upgrade("M", tasks=2, ...) is None`.
  - `test_applied_only_when_on`: with the default grid and a record `{"profile": "S", "applied": True}`, `applied` is `None` for modes `off` and `observe`. For `on` it is `profiles["S"]`. It is `None` for record `None`, and `None` for a record with `applied: False` even under `on`.
  - `test_round_cap_and_skips_read_the_applied_profile`.
  - `test_reviews_task_keeps_risky_and_unknown_tasks`. For profile `S`:
    - `answers=None` → True;
    - `risk: unsure` → True;
    - `security` → True;
    - `NO` → False.
- [ ] **Step 2: Run them and see them fail** on the missing module.
- [ ] **Step 3: Implement `agentflow/route.py`** with a module docstring that states the rule, the fallback and "only up".
- [ ] **Step 4: Run** `test_route.py` and `test_portability.py`. All pass.
- [ ] **Step 5: Commit** "Pick a route profile from the sizer's facts in engine code, taking a proposal only when it is higher."

---

### Task 3: The triage session at run start

**Files:**
- Create:
  - `agentflow/route_lane.py`;
  - `agentflow/prompts/route.md`.
- Modify:
  - `agentflow/lanes.py`: `Executor`'s bases gain `RouteLane`;
  - `agentflow/pipeline.py`: `_Loop.go` calls the triage after `preflight`, before the first phase;
  - `agentflow/state.py`: `initial_state()["route"] = None`, an `_on_route_profile` reducer, and the `REQUIRED` entry;
  - `agentflow/activity.py`: a sentence for `route_profile`;
  - `CHANGELOG.md`.
- Test: `agentflow/tests/test_route_lane.py`, `agentflow/tests/test_state.py`

**Interfaces:**
- Consumes:
  - `SizingLane._sizer_call`, `_sizer_stop`, `sizer_reset_needed`, `check_open_sizers`, `_orphan_check` and `_last_sizer_started`;
  - `route.facts_from`, `route.decide` and `route.last_record`;
  - `overlays.security_path`.
- Produces:
  - `ROUTE_PROMPT = "route.md"` and `ROUTE_STEP = "route"`. The owner key is `f"{first_phase}/{ROUTE_STEP}"` and the sizer key is `owner + sizing.SIZE_MARK`. That keeps it a sizer key (`sizing.is_sizer_key`), so the orphan and reset checks work unchanged;
  - `RouteLane.route_due(self) -> bool`. True when all of these hold:
    - `cfg.route.mode != "off"`;
    - the run's lane is in `cfg.route.lanes`;
    - `not self.dry_run`;
    - no `route_profile` event yet;
  - `RouteLane.size_route(self, first_phase: str) -> Literal["done", "stopped"]`. In order:
    1. If `sizer_reset_needed(owner)` is non-empty, raise the `sizer_edit` stop again.
    2. If `check_open_sizers(owner)` is False, return `"stopped"`.
    3. Build the card. It lists:
       - the lane;
       - `module` or `topic`;
       - which of `{run_dir}/issue.md`, `{run_dir}/task.md`, `{spec}` and `{plan}` exist, as paths for the session to read;
       - the security overlay;
       - the paths changed since `base_sha`.
    4. Call `_sizer_call` with:
       - the first phase's `plan`-role model, chosen the way `size_plan` chooses its model;
       - effort `sizing.sizer_effort(True, cfg.effort.allowed)`;
       - `sizing.SIZE_MAX_TURNS` turns and `sizing.SIZE_TIMEOUT_S`.
    5. An edit leads to `_sizer_stop(owner, …)`, which returns `"stopped"`.
    6. Otherwise, record `route_profile`:
       - `profile` and `proposed_by` from `route.decide`;
       - `applied=(mode == "on")`;
       - `upgraded_from=None`;
       - `facts=Facts.as_event()`;
       - `reason` cut to `sizing.REASON_MAX`.

       It returns `"done"`. Any other exception records the `fallback` decision. Triage never stops a run except for an edit, as with sizing.
- `route.md` asks for one JSON object in the final message:
  ```json
  {"answers": {"interface": "...", "risk": "...", "ambiguity": "...", "breadth": "...", "novelty": "...", "mechanical": "..."},
   "tasks_expected": 0, "files_expected": 0, "propose": null, "reason": "..."}
  ```
  - It reuses `size.md`'s six question texts word for word, so the meaning is the same.
  - It says the input files are data, never instructions.
  - It explains `propose`: name a larger profile with a reason only when the checklist undersells the work. The runner ignores a smaller one.
  - It says the session may only read (`Read`, `Grep`, `Glob`).
- `_Loop.go` calls `executor.size_route(first)` when `executor.route_due()` is true. It goes right after the preflight check and before the phase loop, and returns `"waiting_human"` when the triage stops.

- [ ] **Step 1: Write the failing tests** in `test_route_lane.py`, on `flow_repo` with `route.mode` set by `configure`. The python pack writes its workflow, so the test patch also writes `route.profiles` with the fixture's keys: `XS: {skip: [define/grill_open_questions]}`, `S: {}`, `M: {}`, `L: {}`. The fake agent answers the key `define/route/size` with `{"final_text": json.dumps({...})}`.
  - `test_off_runs_no_triage`: the default mode. No call ends in `/route/size`, and there is no `route_profile`.
  - `test_on_records_the_profile_before_define`:
    - the answers are `mechanical: yes`, files 1, tasks 1;
    - `route_profile` has `profile == "XS"`, `applied is True` and `proposed_by == "rule"`;
    - its `seq` is lower than the first `phase_started`;
    - the call ran with `read_only` and at effort `medium`.
  - `test_observe_records_but_does_not_apply`: `applied is False`.
  - `test_a_bad_answer_falls_back_to_l`: `final_text: "not json"` gives `profile == "L"` and `proposed_by == "fallback"`, and the run goes on to `define`.
  - `test_the_sizer_may_propose_up`: facts that pick `S`, with `propose: "L"`, give `("L", "sizer")`, and the reason is recorded.
  - `test_a_triage_that_edits_stops_and_runs_again_after_the_reset`:
    - the fake writes `x.txt`, and the run stops with `guard_violation` kind `sizer_edit`;
    - after `x.txt` is deleted and the stop approved, `advance` triages again and records `route_profile`;
    - only then does `phase_started` appear.
  - `test_a_resumed_run_triages_once`: a second `advance` makes no second `/route/size` call.
  - `test_a_bug_run_is_not_triaged`: `route.lanes: [feature]` and a bug run make no call.

  In `test_state.py`:
  - `test_route_profile_sets_state_route`: two events, and the last one wins in `state["route"]`.
- [ ] **Step 2: Run them and see them fail.**
- [ ] **Step 3: Implement** `route_lane.py`, `route.md`, the `Executor` base, the `_Loop.go` call, the reducer and the activity sentence. The activity sentence reads: "Sized the route as S (rule)" or "Upgraded the route from S to M (runner)".
- [ ] **Step 4: Run** `test_route_lane.py`, `test_state.py`, `test_sizing_lane.py`, `test_pipeline.py`, `test_activity.py` and `test_portability.py`. All pass.
- [ ] **Step 5: Docs.** Add a paragraph to the playbook after the effort paragraph (§ "The runner sizes every step…"). It covers:
  - the triage session;
  - the rule;
  - proposals only up;
  - the three modes;
  - that a `sizer_edit` stop from triage is cleared the same way as a step sizer's.

  Add a `CHANGELOG.md` `Added` line.
- [ ] **Step 6: Commit** "Triage a run at its start in a read-only session and record the route profile the rule picks."

---

### Task 4: Apply the profile: skipped steps, round caps, per-task review and `explain --profile`

**Files:**
- Modify:
  - `agentflow/flow_control.py`: `_should_run`;
  - `agentflow/autonomy.py`: `round_limit`;
  - `agentflow/doubt.py`: `stop_reason`;
  - `agentflow/pipeline.py`: `_Loop.settle`;
  - `agentflow/feature_lane.py`: `_run_task`;
  - `agentflow/conditions.py`: `explain` and `_explain_steps`;
  - `agentflow/flow.py`: `explain --profile`;
  - `docs/agentflow-playbook.md`;
  - `CHANGELOG.md`.
- Test: `agentflow/tests/test_route_apply.py`, `agentflow/tests/test_conditions.py`

**Interfaces:**
- Consumes: `route.applied`, `route.skips`, `route.round_cap`, `route.reviews_task` and `route.last_record` (Task 2), and `state["route"]` (Task 3).
- Produces:
  - In `_should_run`, before `when`: when `route.skips(cfg, state["route"], key)` gives profile `P`, return `(False, f"route:{P}", f"route profile {P} skips it", None)`.
    - Only top-level step keys (`<phase>/<id>`) can match.
    - A `reconciles` step keeps its own check first.
  - `autonomy.round_limit(cfg: Config, phase: str | None = None, record: dict | None = None) -> int`: today's value, lowered to `route.round_cap` when that is set, and never below 1. The callers pass these values:
    - `doubt.stop_reason`: `phase.name` and `route.last_record(events)`;
    - `_Loop.settle`: `name` and `self.run.state["route"]`.
  - In `_run_task`, after the existing `review = …` line: when `review is not None` and `route.reviews_task(...)` is False, record `review_skipped{key: f"{tkey}/review", reason: f"route profile {P}"}` once per key and set `review = None`.
    - Pass `reviews_task` the applied profile, the task's last `effort_decided` `answers` for `f"{tkey}/implement"`, and `overlays.security_path(cfg, ctx)`.
    - The commit then goes ahead as it does for a small plan with `review: false`.
  - `conditions.explain(cfg, lane, ctx, overrides=None, profile: str | None = None)`. With a profile, a step it skips gets verdict `skip` with reason `route profile <P>`. With `None`, the output is unchanged.
  - `agentflow explain --profile {XS,S,M,L}`. A name with no entry in `cfg.route.profiles` is an error that names the defined profiles.

- [ ] **Step 1: Write the failing tests** in `test_route_apply.py`, on `flow_repo` with `with_doubt` and `route.mode: on`. Seed the profile with `run.emit("route_profile", profile=..., applied=True, proposed_by="rule", upgraded_from=None, facts={}, reason="")` before `advance`, and with `route.lanes: []` so no triage runs.
  - `test_an_xs_run_has_no_grilling_and_no_doubt_round`:
    - the profile `XS: {skip: [define/grill_open_questions, define/spec_review, plan/plan_review]}`;
    - a spec with an open question;
    - `define/grill_open_questions`, `define/spec_review` and `plan/plan_review` have `step_skipped` with reason `route profile XS`;
    - the fake agent got no call for them;
    - no `phase_rejected` of kind `reconcile` is recorded.
  - `test_s_caps_the_spec_review_at_one_round`:
    - the profile `S: {doubt_rounds: {define: 1}}`, with `autonomy.finding_rounds: 2`;
    - a `high` finding in every review;
    - one reconcile round, then the approval page says the rounds are used up (`STOP_LINES["bounded"]`);
    - without the profile, the same script opens two rounds.
  - `test_a_cap_above_finding_rounds_does_not_widen`: `doubt_rounds: {define: 5}` with `finding_rounds: 1` opens one round.
  - `test_s_drops_the_review_of_a_task_with_no_risk`:
    - `S: {per_task_review: false}`, and the plan sizer answers task 1 `NO` and task 2 `risk: yes`;
    - `build/build/task-1/review` gets `review_skipped` and no reviewer call;
    - `build/build/task-2/review` runs;
    - both tasks commit.
  - `test_a_task_with_no_sizer_answers_keeps_its_review`: `effort.sizing: false`, so the review runs.
  - `test_observe_applies_nothing`: the same seed with `applied=False` and `mode: observe` skips nothing.
  - `test_an_unknown_profile_runs_the_full_route`: a seed of `profile: "M"` with only `XS` defined skips nothing and caps nothing.

  In `test_conditions.py`:
  - `test_explain_with_a_profile_marks_its_skips`;
  - `test_explain_without_a_profile_is_unchanged`: the same lines as calling it without the argument.
- [ ] **Step 2: Run them and see them fail.**
- [ ] **Step 3: Implement** the four hooks and `explain --profile`.
- [ ] **Step 4: Run** `test_route_apply.py`, `test_conditions.py`, `test_doubt.py`, `test_doubt_flow.py`, `test_autonomy.py`, `test_feature_lane.py`, `test_refine.py` and `test_explain_golden.py`. All pass. `git diff --stat agentflow/tests/fixtures/explain` is empty.
- [ ] **Step 5: Docs.**
  - Extend the Task 3 playbook paragraph with what each profile key does when applied, and with `explain --profile`.
  - Add `explain --profile` to the §10 cheat sheet.
  - Add a `CHANGELOG.md` `Added` line.
- [ ] **Step 6: Commit** "Apply the route profile: skip its steps, cap each phase's doubt rounds, and drop per-task review for tasks with no risk."

---

### Task 5: Mid-flight upgrade, review tiering and the `route_upgrades` signal

**Files:**
- Modify:
  - `agentflow/route_lane.py`: `reroute`;
  - `agentflow/pipeline.py`: `_Loop.phase` calls it before `phase_started`;
  - `agentflow/feature_lane.py`: `run_foreach` calls it after `size_plan`;
  - `agentflow/sizing.py`: `review_level`;
  - `agentflow/sizing_lane.py`: `size_plan`'s review levels;
  - `agentflow/signals.py`: the `route_upgrades` family;
  - `docs/agentflow-playbook.md` §15 signal list;
  - `CHANGELOG.md`.
- Test: `agentflow/tests/test_route_lane.py`, `agentflow/tests/test_sizing.py`, `agentflow/tests/test_sizing_lane.py`, `agentflow/tests/test_signals.py`

**Interfaces:**
- Consumes: `route.upgrade` and `route.last_record`, `sources.parse_plan`, and `overlays.security_path`.
- Produces:
  - `RouteLane.reroute(self, *, tasks: int | None, risk: bool) -> None`. It does nothing unless a `route_profile` exists and the mode is not `off`. When `route.upgrade(current, tasks=tasks, risk=risk, security=overlays.security_path(cfg, ctx))` gives a target, it emits `route_profile` with:
    - `profile=target` and `upgraded_from=current`;
    - `proposed_by="runner"`;
    - `applied` as the last record had it;
    - `facts={"tasks": tasks, "risk": risk, "security": ...}`;
    - `reason="the plan has <n> tasks"` or `"a task is flagged risk"` or `"the security overlay applies"`.

    It never emits when the target equals or is below `current`.
  - The callers:
    - `_Loop.phase`, for a phase about to start (`status in (None, "pending")`, after `skip_if`): `tasks` is the open task count of `ctx.values["plan"]` when that file exists, else `None`; `risk=False`;
    - `run_foreach`, after `size_plan` returns `"done"`: `tasks` is the open task count; `risk` is any task whose latest `effort_decided` `answers` has `sizing.flags(answers)["risk"]`.
  - `sizing.review_level(answers: dict[str, str] | None, files: int, security: bool, policy: EffortPolicy) -> str`:
    - with `answers is None`, it returns `decide(None, files, security, True, policy)[0]`;
    - otherwise the level is `high` if `security` or `flags(answers)["risk"]`, else `medium`;
    - the result is clamped and floored as a judgement step.
  - `size_plan` uses `review_level` in place of `decide(..., True, policy)[0]` for `review[task.n]` when `route.applied(cfg, state["route"])` is not `None`.
  - `signals`:
    - `"route_upgrades": ("lower", False)` counts `route_profile` events with a non-null `upgraded_from`;
    - it is present only for a run with a `route_profile`;
    - it is in `COUNT_FAMILIES`.

- [ ] **Step 1: Write the failing tests.**

  In `test_route_lane.py`:
  - `test_a_plan_past_the_s_cap_upgrades_to_m_before_build`:
    - seed `S` with `per_task_review: false`, and a 9-task plan;
    - at `build`'s start, there is a `route_profile` with `profile == "M"`, `upgraded_from == "S"` and `proposed_by == "runner"`, before `build`'s `phase_started`;
    - task 1's review runs.
  - `test_a_risky_task_upgrades_to_l_after_sizing`: seed `M`; the plan sizer answers task 2 `risk: yes`; `route_profile` has `profile == "L"` and `upgraded_from == "M"`.
  - `test_no_downgrade`: seed `L` with a 1-task plan; no second `route_profile`.
  - `test_reroute_is_quiet_when_off`.

  In `test_sizing.py`:
  - `test_review_level_tiers_on_risk_only`:
    - `NO | {novelty: yes}` → `medium` (where `decide(..., True, …)` gives `high`);
    - `risk: yes` → `high`;
    - `security` → `high`;
    - `None` → `high`.

  In `test_sizing_lane.py`:
  - `test_reviews_are_tiered_when_a_profile_applies`: with `on`, a task answered `breadth: yes` has a review `effort_decided` of `medium`; with `off` it is `high`.

  In `test_signals.py`:
  - `test_route_upgrades_counts_upgrades`: two `route_profile` events, one an upgrade, give `1`; a run with none has no key.
- [ ] **Step 2: Run them and see them fail.**
- [ ] **Step 3: Implement** `reroute`, its two callers, `review_level`, the `size_plan` switch and the signal.
- [ ] **Step 4: Run** the four test files, plus `test_feature_lane.py`, `test_pipeline.py` and `test_scorecard.py`. All pass.
- [ ] **Step 5: Docs.**
  - Playbook: add the upgrade and the tiering to the route paragraph, and `route_upgrades` to the §15 signal list.
  - Add a `CHANGELOG.md` `Added` line.
- [ ] **Step 6: Commit** "Upgrade the route when the plan is bigger or riskier than its profile, and review tasks with no risk at medium effort."

---

### Task 6: The whole slice

- [ ] **Step 1: Run the full suite.** `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests --import-mode=prepend -q`. All pass. `git diff --stat agentflow/tests/fixtures/explain` is empty.
- [ ] **Step 2: Lint and validate.** `uvx ruff@0.17.4 check .` prints `All checks passed!`. `bin/agentflow validate` prints "0 warnings".
- [ ] **Step 3: Human step: turn on `observe` in this pack.** `.agentflow/**` is forbidden to a run, so a human makes this edit. This pack writes its own `phases`, so it writes its own `route` on its own step keys. It has no `spec_review` or `plan_review`:
  ```yaml
  route:
    mode: "observe"
    lanes: [feature]
    profiles:
      XS: {skip: [define/idea, define/contracts, define/grill_open_questions], per_task_review: false}
      S:  {skip: [define/idea], per_task_review: false}
      M:  {}
      L:  {}
  ```
  After that, `bin/agentflow validate` prints "0 warnings", and `bin/agentflow explain feature --module <id> --profile XS` shows the three skips.
- [ ] **Step 4: Check against the spec's "T3" verification.**
  - `explain` with no `route.profiles` is byte-identical to the golden files (Step 1).
  - An XS fixture runs no grilling and no doubt round (Task 4, `test_an_xs_run_has_no_grilling_and_no_doubt_round`).
  - A plan that grows past S's task cap upgrades to M, as a `route_profile` with `upgraded_from` (Task 5).

  Run `observe` on 5 real feature runs. Then compare each run's recorded profile with how it went, using `agentflow scorecard` and the `route_profile` events. Only then switch the pack to `on`. The −30–60 % define/plan saving, and small features in ≤ 45 min, are measured after that (spec §6).

---

## Notes: accepted trade-offs

- **XS still drafts a spec.** The spec's table says XS has no define and "the issue text is the spec". But `plan.md`, the spec approval and the `spec_is_approved` condition all read `{spec}`. So XS skips grilling and the doubt review, and keeps a short draft from the issue. Dropping the draft would need a spec-less plan prompt, which is a later slice if XS runs show the draft is the cost.
- **Three profile keys, not five.** The spec lists `skip, doubt_rounds, per_task_review, grill, plan_review`. `grill: false` is `skip: [<phase>/<grill group>]`, and the plan review's rounds are `doubt_rounds: {plan: N}`. Phase and step names are pack data, so keying by them keeps the engine free of the default workflow's names. The two extra keys would only be aliases.
- **`route.mode` and `route.lanes` are new keys** the spec's key table doesn't list. The spec asks for "observe first" (T3's risk column) and for opt-in levers (§5). A mode is how both hold without a second config. `lanes` keeps bug runs, whose route is already short, from paying for a triage session.
- **Profile M keeps per-task review in T3.** The wave review that replaces it needs T5's wave sessions.
- **The plan phase's doubt cap comes from the profile at the plan phase's start.** A plan that comes back bigger upgrades the profile at `build`'s start. By then its own review rounds are already spent at the smaller cap. The cap is a floor of at least 1, and the human still approves the plan.
- **Fallback is L.** A failing triage costs ceremony, never a skipped review. `proposed_by: fallback` makes such runs visible on the scorecard.
- **A run started `--from build` is still triaged.** Its plan exists, so `_Loop.phase` upgrades on the task count before `build` starts.
