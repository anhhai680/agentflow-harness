# Proposal: give `verify` and `review` one purpose each

Status: approved (2026-11-14)

Source: the lifecycle the workflow is built on, `addyosmani/agent-skills` (`DEFINE → PLAN → BUILD → VERIFY → REVIEW →
SHIP`, vendored skills listed in `.claude/skills/VENDORED.md`), and the 14 finished runs that reached `verify` or
`review` in the local state dir (aggregated below, no run named). It changes the engine's default workflow
(`agentflow/defaults/config.yaml`), two prompts, two bundled skills and this repository's pack. It adds no config key.

## Objective

**Problem.** On the dashboard the two phases look like copies of each other, and in the config they mostly are:

| `verify` (today) | `review` (today) |
|---|---|
| `setup` (gates: setup) | `setup` (gates: setup, when: refine_round) |
| `plan_fixes` (`agentflow-respond`, opus, when: refine_round) | `plan_fixes`: the same anchor `*plan_fixes` |
| `fix_findings` (`agentflow-respond`, gates: verify, when: refine_round) | `fix_findings`: the same anchor `*fix_findings` |
| `verify` (`agentflow-verify`, gates_first, fix_loop, commits) | `branch_review` (`agentflow-review`, read-only) |

Three of the four steps are the same YAML anchor. Only the last step differs, and even there the briefs overlap:

- `agentflow-verify` step 4 says "compare the result against the spec's acceptance criteria and the plan's tasks, one
  by one". `branch_review.md` says "check that every task landed … tests cover the changed lines", and
  `agentflow-review` asks "does it do what the plan task and the spec say … did every task land?". Two sessions judge
  completeness against the same spec and plan, and neither is told the other did.
- `verify` fixes what it finds through `plan_fixes` / `fix_findings`, whose skill is `agentflow-respond`
  ("receiving code review"). That is review-feedback machinery inside the phase that is meant to prove the code runs.
- `branch_review` is asked to judge coverage ("tests cover the changed lines"), which is a measurement, so it belongs
  to a gate.

**The intended split** (`addyosmani/agent-skills` README, the lifecycle diagram and the stage tables):

| | VERIFY: "Prove it works" (Test / Debug) | REVIEW: "Quality gates before merge" (QA / Gate) |
|---|---|---|
| Question | Does this tree run and pass, and what is the evidence? | Is this change right and healthy enough to merge? |
| How | Run things: tests, the build, runtime checks (`browser-testing-with-devtools`) | Read things: the five-axis review (`code-review-and-quality`) |
| On failure | Stop the line and debug: reproduce, localize, fix the cause, guard (`debugging-and-error-recovery`) | Findings labelled by severity; the author addresses the feedback |
| Output | A verification story: what ran, with what result | Findings on correctness, readability, architecture, security, performance |
| Relation | — | Step 5, "Verify the verification": it *reads* the verification story, never redoes it |

agentflow has drifted from that split in both directions: verify reads the spec like a reviewer and fixes like one,
and review measures like a verifier.

**What the runs show** (14 finished runs, 13 `feature` and 1 `bug`, every run in the state dir that reached either
phase):

- **The two checkers do not find the same defects.** The verify session raised 1 blocking finding in 14 runs (an eval
  gate passing with 0 cases, an environment problem). `branch_review` raised 6 blocking findings in 3 runs and opened
  4 refine rounds. The overlap is in the briefs and the fix machinery, not in the results.
- **Verify's completeness check is the weaker copy.** In one run verify reported only a minor note, and `branch_review`
  then found that an acceptance criterion was not met (a seeded value missing from one of two places). Verify claimed
  the criteria were met by reading, which is review's job, and got it wrong.
- **Verify's real work is debugging.** The verify session committed a change in 8 of 14 runs. All 8 were repairs of a
  failing gate or test environment: clock skew between host and database, `xdist` parametrize ids, a missing eval
  database, a test timeout, a test isolated from the host's env. None changed the feature. That is the Debug half of
  VERIFY, done well, but under a brief that does not say so.
- **The review-feedback loop inside verify cost the most and caused a defect.** `verify/plan_fixes` and
  `verify/fix_findings` ran in 3 runs for $15.35, against $7.64 for all 14 verify sessions together. In one of those
  runs `verify/fix_findings` committed a generated eval output at the repository root, and `branch_review` then had
  to block on it. In another, the round was opened by a finding with severity `note` (this repository's
  `.agentflow/prompts/verify.md` override dropped the engine's line that allows only `blocking` and `minor`) about a
  flaky test the branch did not touch. That override was fixed on its own on 2026-11-14 (Decided behaviour 6).
- **The shared steps already leaked once.** Before `e0baafb` (2026-11-11) a verify refine round stayed open into
  `review`, so `review/plan_fixes` and `review/fix_findings` ran again on verify's findings in 2 runs, with no review
  finding to act on. That bug is fixed, but it came from both phases holding the same `when: refine_round` steps.

### Acceptance criteria

1. In the engine defaults, `plan_fixes` and `fix_findings` appear in `review` only. `verify` holds `setup` and
   `verify`. No YAML anchor is shared between the two phases except `*setup`.
2. A failing verify gate is fixed inside the `verify` step's fix loop under a debugging brief (reproduce, localize,
   fix the cause, guard with a test). It is never routed through `agentflow-respond`.
3. A blocking verify finding that the fix loop could not clear opens a refine round of `verify` that re-runs only
   `setup` and `verify` (a fresh session given `{earlier_findings}`), up to `autonomy.finding_rounds`, then the
   resolver, then a human, as today.
4. The verify step's result carries a verification story in `signals.verification`: each gate with its command,
   result line and counts; each acceptance criterion with the test or command that ran to prove it; each check that
   could not run, with the reason. The PR body gets a `Verification` section from it. A missing or malformed story
   blocks nothing: the section and `{verification}` say "verify reported no evidence".
5. Verify never judges by reading. A criterion with no evidence that ran is listed as "not verified" (`minor`, so it
   lands in the PR), not marked "met".
6. `branch_review` judges on the five axes (correctness, readability and simplicity, architecture, security,
   performance). It receives the verification story through a new `{verification}` placeholder, flags evidence that
   does not prove its criterion (for example a test that passes without the change), and never re-runs a gate or
   re-checks coverage.
7. The python fixture pack keeps its own `phases:`, so `agentflow/tests/fixtures/explain/*.txt` stay byte-identical.
   A pack that still puts `plan_fixes` / `fix_findings` in `verify` keeps working unchanged.
8. `bin/agentflow validate` prints `0 warnings` for this repository's pack and both fixture packs.
9. The PR's `Verification` section marks each criterion whose evidence came before the last `review/fix_findings`
   commit as `(before review fix <sha>)`. No step re-runs verify after a review fix.

## Decided behaviour

(The user's choices, confirmed 2026-11-14 in a grilling round on this proposal's open questions and its unstated
assumptions.)

1. **No re-verify after a review fix.** A review fix is re-gated by `fix_findings` and re-reviewed by
   `branch_review`. Criteria proven only by a command no gate runs are not re-run. The PR marks them
   `(before review fix <sha>)` instead. Rejected: re-running `verify` at the end of a review round (it puts a verify
   step back inside `review`), and re-running the story's commands as runner code (agent-written commands would need
   the bug lane's allow-list checks).
2. **No `validate` warning for review feedback outside review.** A warning narrow enough to spare the python fixture
   pack (whose `verify` phase holds `plan_fixes` / `fix_findings` and whose golden files cannot change) would also
   spare the old-style `verify` phase it was meant to catch. The playbook and the engine defaults show the intended
   shape.
3. **All five review axes on every change.** The reviewer writes "None" for an axis that does not apply (performance
   on a docs-only diff, say). No condition per axis and no config key.
4. **Environment failures take the normal path.** A failure verify judges to be environmental (a missing database,
   clock skew, an unset key) is a blocking finding with that reason. It goes through the verify refine round, then the
   resolver, then a human, like any other. There is no `environment` marker and no new `autonomy.resolve_kinds` entry:
   it happened once in 14 runs. Revisit if `refine_rounds:verify` shows it often.
5. **A missing verification story blocks nothing.** Gates and the conformance audit already hold the hard evidence;
   the story is for the reviewer and the human. When `signals.verification` is missing or malformed, the PR's
   `Verification` section and `{verification}` say "verify reported no evidence". Make it blocking later only if runs
   show it going missing.
6. **The `note` severity bug was fixed separately.** `.agentflow/prompts/verify.md` got back the engine's line that
   allows only `blocking` and `minor`, on 2026-11-14, outside this proposal. Section 4 no longer carries it.

## Design

### 1. Verify: prove it works

The engine default becomes:

```yaml
  verify:
    needs_approval: false
    resolve: true
    model: sonnet
    steps:
      - *setup
      - {id: verify, lanes: [feature, bug], skill: "@skills.map.verify", role: build, prompt: verify.md,
         blocks_on_findings: true, gates_first: true, gates: verify, fix_loop: true, commit: true,
         message: "Fix failures found in verification"}
```

The `&plan_fixes` and `&fix_findings` anchors move into `review`, the only phase whose findings are review
feedback.

Nothing new is needed in the engine for the refine round: a `blocks_on_findings` step that is re-run in a refine
round already gets a fresh session with `{earlier_findings}` (`flow_control.py`, the `review = step.role == "review"
or step.blocks_on_findings` branch), and `when: refine_round` steps are simply absent. A test pins this (Testing).

`verify.md` and `agentflow-verify` say what VERIFY means upstream:

- **When gates fail** (the session starts in its fix loop, `fix_first`): stop the line. Reproduce, localize, fix the
  cause, guard with a test, then let the runner re-run every gate. Say whether the failure comes from the branch or
  from the environment (clock, database, ports, a flaky test the branch does not touch). An environment failure that
  cannot be fixed in the worktree is a blocking finding with that reason. It takes the normal path: the verify refine
  round, then the resolver, then a human (Decided behaviour 4).
- **When gates pass**: write the verification story. Report each gate from `{gate_evidence}` without re-running it.
  For each acceptance criterion in the spec, and each verification command in the plan, name the test or command that
  ran and its result, and run the ones no gate covers. A criterion with nothing that ran is listed under
  `not_verified` with the reason (needs a browser, needs the full stack, needs a key that is not set).
- **Dropped from verify:** comparing the code against the spec and the plan by reading. That moves to review.

The result shape (in `signals`, which the result schema already allows):

```json
{"verification": {
  "gates": [{"gate": "py_unit", "cmd": "…", "result": "2427 passed", "passed": true}],
  "criteria": [{"criterion": "Success Criterion 3", "evidence": "tests/unit/test_x.py::test_y", "ran": true, "passed": true}],
  "not_verified": [{"what": "Success Criterion 7 (browser)", "why": "needs the compose stack"}]
}}
```

`feature_lane.render_pr_body` adds a `Verification` section next to `Gates`: the criteria table and the not-verified
list. That way a human reviewer of the PR sees what was proven and what was not, which is the "verification story"
upstream asks the author for. When the result has no `verification` signal, or one that does not parse, the section
says "verify reported no evidence" and nothing blocks (Decided behaviour 5).

### 2. Review: the quality gate

The engine default keeps its steps and becomes the only owner of the fix loop:

```yaml
  review:
    needs_approval: false
    resolve: true
    model: {default: sonnet, build: sonnet}
    steps:
      - {<<: *setup, lanes: [feature, bug], when: refine_round}
      - &plan_fixes
        {id: plan_fixes, lanes: [feature, bug], skill: "@skills.map.review_feedback", role: plan, model: opus,
         prompt: plan_fixes.md, when: refine_round}
      - &fix_findings
        {id: fix_findings, lanes: [feature, bug], skill: "@skills.map.review_feedback", role: build,
         prompt: fix_findings.md, when: refine_round, gates: verify, fix_loop: true, commit: true,
         message: "Fix findings"}
      - {id: branch_review, lanes: [feature, bug], skill: "@skills.map.review", role: review,
         prompt: branch_review.md, blocks_on_findings: true}
```

`branch_review.md` and `agentflow-review` change:

- **Removed:** "check that every task landed" (the runner already knows: `plan_done` ticks every box, and verify's
  criteria table shows what ran) and "tests cover the changed lines" (a measurement: a coverage gate, or verify's
  story).
- **Added:** the five axes, in upstream's order of leverage: correctness (does the code do what the spec says, edge
  cases and error paths, are the tests the *right* tests), readability and simplicity, architecture (fits existing
  patterns, no logic leaking into shared modules, no near-duplicate helpers), security, performance. Every axis runs
  on every change, and the reviewer writes "None" for one that does not apply (Decided behaviour 3).
- **Added:** "Verify the verification". The prompt carries `{verification}` (the runner fills it from the
  `verify/verify` result). The reviewer flags a criterion whose evidence does not prove it, and a `not_verified`
  item that a test could have covered. It never re-runs gates and never repeats the criteria table.
- **Kept:** scope ("nothing outside the plan changed"), the docs the change touches, the severity mapping, and
  "Declined to judge".

The per-task review in `build` (`task_review.md`) is not changed. `branch_review` already differs from it by range
(the whole branch against one task), and the five axes give it the cross-task view a per-task review cannot have:
duplication between tasks, and architecture across them.

### 3. Final tree and conformance

The order stays `verify → review`, as upstream has it. Review fixes land after verification, and that is already
covered: `fix_findings` runs `gates: verify` on its own commit, `conformance._staling` already treats a commit whose
step ran the whole verify gate set as not staling `step:verify` (the bar chosen in the conformance slice), and the
re-run `branch_review` reviews the fix. No obligation changes.

What a review fix does make stale is the non-gate part of the story (a criterion proven by a command no gate runs).
The PR's `Verification` section marks each criterion whose evidence came before the last `review/fix_findings`
commit as `(before review fix <sha>)`, so the staleness is visible instead of implied. Nothing re-runs verify after a
review fix (Decided behaviour 1).

### 4. This repository's pack

`.agentflow/config.yaml` gets the same shape: `verify` drops `plan_fixes` / `fix_findings`, and its `verify` step's
brief adds `.claude/skills/systematic-debugging/SKILL.md` next to `verification-before-completion`. The anchors
move into `review`. `.agentflow/prompts/verify.md` takes the new engine text, with its skill wording kept (its
severity line was already restored separately, Decided behaviour 6). `.agentflow/prompts/branch_review.md` takes the
new engine text, with its `origin/main` and skill wording kept.

### 5. Retired prompt text

The engine prompts drop text, so `agentflow/pack_fix.py` gets `Retired` entries for:

- `verify.md`: "If something fails, say which gate and why, and fix only the cause." (replaced by the debugging
  paragraph);
- `branch_review.md`: "Check that every task landed, nothing outside the plan changed, tests cover the changed lines,
  and the docs the change touches (spec, idea doc, decision record index) are consistent." (replaced by the axes and
  the scope line).

`validate` then finds packs whose overrides still carry the old text, and `validate --fix` repairs them.

## Boundaries

- **Always:** verify runs things and reports what ran; review reads things and judges; one fix loop for review
  feedback, in `review`; every blocking finding still gets at least one automatic round (engine floor).
- **Never:** remove the ability to put `plan_fixes` / `fix_findings` in any phase (the python fixture pack does it
  and its golden files pin it); change the phase order; change an obligation or the conformance bar; add a config
  key or a `skills.map` key.
- **Ask first:** re-running `verify` after a review fix; a stop kind of its own for environment failures; making a
  missing verification story blocking; turning a review axis on or off by condition. Each was decided against on
  2026-11-14 (Decided behaviour 1, 3, 4, 5).

## Testing

- `test_refine.py`: a verify refine round in the engine-default workflow
  re-runs only `setup` and `verify`, gives the session `{earlier_findings}`, and goes to the resolver after
  `autonomy.finding_rounds`. A `review` round after an approved `verify` still starts with no round of its own (the
  `e0baafb` regression).
- A run on a pack whose `verify` still holds `plan_fixes` / `fix_findings` behaves exactly as today.
- `test_feature_lane.py`: `render_pr_body` with a `verification` signal renders the criteria and not-verified lists,
  marks criteria older than the last `review/fix_findings` commit, and says "verify reported no evidence" for a
  result with no signal or a malformed one (and the phase is not held back).
- `test_agent_step.py`: `{verification}` is filled from the `verify/verify` result, says "verify reported no
  evidence" when the signal is missing or malformed, is empty when `verify` was skipped, and is never filled from a
  file in the worktree.
- `test_pack_fix.py`: the two `Retired` entries are found in an override and replaced by `--fix`.
- `test_portability.py`, and the golden explain files unchanged.
- `test_config.py`: the engine defaults validate with `0 warnings`; `verify` holds no `when: refine_round` step.

## Files

| File | Change |
|---|---|
| `agentflow/defaults/config.yaml` | `verify` without `plan_fixes` / `fix_findings`; the anchors defined in `review` |
| `agentflow/prompts/verify.md` | debugging on failure, the verification story on success, no reading-based judgement |
| `agentflow/prompts/branch_review.md` | the five axes, `{verification}`, the removed completeness and coverage checks |
| `agentflow/skills/agentflow-verify/SKILL.md` | Test/Debug: the stop-the-line triage, the story, not-verified items |
| `agentflow/skills/agentflow-review/SKILL.md` | the five axes and "verify the verification" |
| `agentflow/config.py`, `agentflow/lanes.py` | the `{verification}` placeholder |
| `agentflow/feature_lane.py` | the PR body's `Verification` section |
| `agentflow/pack_fix.py` | two `Retired` entries |
| `.agentflow/config.yaml`, `.agentflow/prompts/verify.md`, `.agentflow/prompts/branch_review.md` | section 4 |
| `docs/agentflow-playbook.md` | §1 and §8: what each phase is for and where its fixes go; §15 is unchanged |
| `CHANGELOG.md` | `Changed`: verify proves and debugs, review judges and is the only review-feedback loop; `Added`: the PR's `Verification` section |

`agentflow/pack_fix.py` is on the security overlay's paths, so the plan and its review use `security-and-hardening`
and the `security-auditor` agent for that file.

## Commands

```bash
uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_refine.py agentflow/tests/test_feature_lane.py agentflow/tests/test_agent_step.py agentflow/tests/test_pack_fix.py agentflow/tests/test_config.py --import-mode=prepend -q
uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_portability.py --import-mode=prepend -q
uv run --no-project --with pyyaml --with pytest pytest agentflow/tests --import-mode=prepend -q   # before finishing
bin/agentflow validate                  # 0 warnings
bin/agentflow explain feature --module <id>   # verify: setup, verify; review: setup?, plan_fixes?, fix_findings?, branch_review
```

## Success criteria

1. The dashboard's `verify` column shows `setup` and `verify` only, and the `review` column shows the fix loop and
   `branch_review`.
2. Across the next 10 finished runs (`agentflow scorecard`): `refine_rounds:verify` and the verify phase's cost do not
   rise above the baseline, and no `review/plan_fixes` runs without a blocking review finding.
3. Every PR opened by a run has a `Verification` section. Every criterion in it names something that ran or is
   listed as not verified, or the section says "verify reported no evidence".
4. The golden explain files are byte-identical; the full suite passes.

## Alternatives considered

- **Merge `verify` and `review` into one phase.** It removes the duplicated rows, but it also removes the line between
  running and judging, and between a build-role session that may edit and a read-only reviewer. Upstream keeps them
  apart for that reason. Rejected.
- **Swap the order to `review → verify`,** so verification is the last word on the final tree. Gate repairs (8 of 14
  runs) would then ship without a review, and upstream orders VERIFY before REVIEW. Rejected; section 3 covers the
  staleness instead.
- **Drop the verify session and keep only the gates.** That is cheaper, but it loses the debugging of red gates (the
  phase's most useful output in the data) and the verification story that review's step 5 reads. Rejected.
- **Keep the shared anchors and give verify's copy the debug skill.** The dashboard rows and the two-loop design would
  stay, and so would the leak hazard. Rejected.

## Open Questions

None. The three open questions and three unstated assumptions were settled on 2026-11-14 (see Decided behaviour).
