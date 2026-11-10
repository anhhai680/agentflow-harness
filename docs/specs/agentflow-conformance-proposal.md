# Proposal: obligations, conformance audit and scorecard (harness slice 1)

Status: proposed (2026-11-10)

Source: `docs/agentflow-harness-proposal.md` §3.2 (policy), §3.5 (conformance audit), §4 (measuring) and §5 steps 0 and 1. §6 item 7 merges those two steps into this one slice. This proposal is the spec for the slice. It checks the harness design against the code as it stands, and it reuses what slices LR and PnP already built rather than building a second copy.

## Objective

Today the quality bar is implicit: it is whatever steps a lane happens to walk. Nothing proves that the tree a run pushes is the tree its gates and reviews passed on. Before a conductor may choose a route (slices 2 to 4), the bar has to be explicit and checked mechanically. This slice does that on the **current fixed workflow**, and it changes no route:

1. **Obligations.** The pack declares what must be true of a run before it ships: "when condition C holds for lane L, evidence E must exist". The engine evaluates these rules. A model never does.
2. **Evidence bound to a tree.** Every gate, review and expectation event records the tree it ran on.
3. **Conformance audit.** Before `finish` pushes, the runner recomputes the obligations from the real diff and looks for passing evidence on the final tree. It runs missing gates itself. It records the outcome, and in `block` mode it stops when evidence is still missing. The PR body gets a Conformance table.
4. **Scorecard.** The KPIs the harness will be judged by are compared against a frozen baseline of reference-route runs. This is built on the existing `signals` vocabulary and `metrics.aggregate`, not as a new KPI engine.

Users: whoever owns a pack, and reviews its PRs, needs proof that the bar held. The later conductor slices need a floor they cannot go below and a baseline to be compared against.

### What already exists and is reused, not rebuilt

| Need from the harness proposal | Already in the engine | This slice adds |
|---|---|---|
| `tree_sha` on evidence (§3.5) | `step_finished.tree` for steps with gates (`lanes.py:885`), `gates_prerun.tree` (`flow_control.py:399`), `guards.snapshot` | `tree` on the 4 `gate_result` emit sites, on `review_result`, `step_findings` and `expectation_checked`, through one helper. The name stays `tree`, matching the existing events, rather than `tree_sha`. |
| Quality and compliance KPIs (§4.2) | `signals.run_signals`: gate first pass, fix attempts, guard trips, permission denials, stops, human rejections, blocking review findings, cost per task, turns, CI rounds, rollovers, rework | One new family, `conformance_missing`. Nothing else is recomputed. |
| Efficiency KPIs (§4.2) | `metrics.run_metrics` / `metrics.aggregate`: cost, wall time, human wait, by phase, model and effort | Nothing |
| Before/after comparison | `lessons.effects` (per lesson, over the runs that carried it) | A comparison per lane against a frozen baseline, reusing the same mean/direction logic |
| Dashboard | the Metrics tab (`views.metrics_view`) and the Lessons tab | A Scorecard section in the Metrics tab. There is no new tab. |
| Conditions | `conditions.evaluate`, `changed_paths` against `origin/<base_branch>` | Obligations name existing conditions. There is no new condition language. |
| Engine floor | `pack.py` refuses a pack without `gate_sets.verify` | The floor obligation `gates:verify`, added in code, which no layer can remove |
| PR audit trail | `feature_lane.render_pr_body` (Gates, Skipped steps, Effort changes, …) | One section, Conformance |
| Re-running a step | `agentflow retry <run> <key>` | Nothing. A stop names the `retry` command. |

Out of scope: the catalog and reference routes (slice 2), the conductor, `stats.json`, route-eval (slice 3), cost governance (3a), autonomy levels, and demotion SLOs (slice 4). `ci_green` is not an audit obligation, because CI runs after the push. The `ci` step already refuses to mark a PR ready without green checks, and the Conformance table links that result once it exists.

### Acceptance criteria

1. Every `gate_result`, `review_result`, `step_findings` and `expectation_checked` event written by a run carries `tree`, the `guards.snapshot` of the worktree that the gate or review ran on.
2. `policy.obligations` loads from every config layer. The engine defaults ship obligations equal to what today's default workflow already guarantees. A pack can add, replace or switch off an entry by its id, but it can never switch off the engine floor.
3. `agentflow validate` checks every obligation. These keys print `0 warnings` for this repository's pack and for both fixture packs.
4. At `finish`, after the sync merge and the runner-commits check and before the push, the audit:
   - writes one `conformance_checked` event: the final tree, then every obligation with its status (`satisfied`, `waived`, `missing`, `not_applicable`) and the seq of its evidence
   - runs missing gate evidence itself and records it
   - in `block` mode, stops with kind `conformance_failed` when anything is still missing
5. The full engine suite passes, and so do real-shaped fake-agent runs of the `feature` and `bug` lanes, with the audit at **100% satisfied** and no test edits beyond new tests. This proves that the default obligations match today's bar.
6. A test config that requires `step:branch_review` on a lane that doesn't walk it records `missing`. In `report` mode it ships with the gap shown. In `block` mode it stops `conformance_failed`.
7. The PR body has a Conformance section that lists each obligation, its status, its evidence as `<run>#<seq>` and the tree, abbreviated.
8. `agentflow scorecard` prints, per lane, each signal's baseline mean and current mean (with run counts), the delta, and whether it is better or worse. `agentflow scorecard --freeze` records the baseline. The Metrics tab shows the same table.
9. The golden `agentflow explain` files are byte-identical, and `test_portability` passes.

## Tech Stack

Python 3.12+, stdlib plus `pyyaml>=6`, the same as the engine. No new dependency.

## Commands

```bash
uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_conformance.py --import-mode=prepend -q
uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_scorecard.py --import-mode=prepend -q
uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_config_edit.py agentflow/tests/test_portability.py --import-mode=prepend -q
uv run --no-project --with pyyaml --with pytest pytest agentflow/tests --import-mode=prepend -q   # full suite before finishing
bin/agentflow validate                                   # must print "0 warnings"
bin/agentflow config show --effective policy             # the merged obligations and the layer each came from
bin/agentflow scorecard [--lane L] [--from D] [--to D] [--json]
bin/agentflow scorecard --freeze [--lane L] [--from D] [--to D] [--replace]
```

## Project Structure

- New in `agentflow/`:
  - `policy.py`: obligation parsing and evaluation, and the floor. It is pure apart from `conditions.evaluate`.
  - `conformance.py`: finds evidence in events, runs the audit at `finish`, and renders the PR section.
  - `scorecard.py`: compares against the baseline, and freezes and loads the baseline.
- Changed:
  - `config.py`: the `Policy` dataclass and validate
  - `layers.py`: `policy.obligations` entries are replaced whole, like `conditions`
  - `defaults/config.yaml`: the default `policy`
  - the gate and review emit sites in `flow_control.py`, `lanes.py`, `feature_lane.py`, `sync_lane.py` and `bug_eval.py`
  - `feature_lane.run_finish` and `render_pr_body`
  - `signals.py`: one family
  - `flow.py`: `scorecard`
  - `views.py`, `dashboard.py` and `dashboard.html`: the Scorecard section
  - `config_edit.py`: the protected keys
- Pack: `.agentflow/config.yaml` adds the `security_review` obligation. The `python` fixture pack adds `eval_gate`.
- Docs:
  - playbook §14, one line per key
  - playbook §10 and §15, the `scorecard` command
  - the CLAUDE.md engine architecture list
  - `CHANGELOG.md` under `[Unreleased]`
- Tests: `agentflow/tests/test_conformance.py`, `test_scorecard.py`, plus cases in `test_config_edit.py`, `test_layers.py` and `test_signals.py`.

## Design

### Obligations (`policy.obligations`)

An obligation is a mapping entry keyed by a stable id, so that the layers merge it by id:

```yaml
policy:
  enforce: report                  # report | block
  obligations:
    verified:      {lanes: [feature, bug], require: [step:verify]}
    reviewed:      {lanes: [feature, bug], require: [step:branch_review]}
    plan_approved: {lanes: [feature], require: [approval:plan]}
    red_then_green: {lanes: [bug], require: ["expect:red_then_green"]}
    # this repository's pack adds:
    security_reviewed: {when: security_path, require: [step:security_review]}
```

- `when` names a condition from `conditions:`, as a step's `when` does. If it is absent, the obligation always applies. An **unknown** result (git can't diff, for example) counts as holding: the obligation is required. That fails safe, as the security overlay does today.
- `lanes` filters the obligation the same way a step's `lanes` does. If it is absent, every lane applies.
- `require` lists evidence. Every item must be satisfied:

| Evidence | Satisfied when |
|---|---|
| `gates:<set>` | Every gate in `gate_sets.<set>` has a passing (or `skipped`, with its reason shown) `gate_result` whose `tree` equals the final tree |
| `step:<id>` | The step's last `step_finished{status: done}` comes after the last code change, and its last `step_findings` holds no blocking finding. A human's approval of the holding phase after those findings counts too, and is shown as `human override`. |
| `approval:<phase>` | A `phase_approved{phase}` whose `by` is not the runner, or the phase skipped by its `skip_if` because its artifact was already on the base branch. That second case is shown as `on base`. |
| `expect:red_then_green` | `expectation_checked` events show red, then green, with the green one after the last code change |

- **The last code change** is the last `step_committed` or `task_committed` event. A `base_synced` whose `conflicts` is non-empty counts too, because hand-resolved conflict code is a change no review has seen. A clean merge of the base branch is not the run's own code change. Gates still see it, because gate evidence is bound to the exact final tree, not to "after". Today's workflow doesn't review again after a conflicted merge, so those runs show `missing` until a later slice queues the review again by itself. That is a real gap in today's bar, made visible.
  - *Amendment (2026-11-10).* Two commits never make `step:` or `expect:` evidence stale, so the audit holds a run to today's bar and no stricter. (1) A step's own commit: a change whose key is the evidence's own key, as `build/fix` commits after its own green check, and a step that commits before it finishes. (2) A commit whose step ran every gate of `gate_sets.verify` under that same key, each one passed or skipped: a refine-round fix such as `review/fix_findings`. Today's workflow re-checks such a fix with the verify gates, not with a fresh verify step or red/green check, and the floor still needs those gates on the final tree. A conflicted `base_synced` gets neither exception.
- **Waived by a human.** When a human marked the run done (`run_marked_done`), an obligation whose evidence is missing only because a phase it needed was skipped by that mark-done gets the status `waived`. It is shown as `waived by <who>`, with the mark-done event's seq as its evidence. A waived obligation doesn't block in `block` mode, and it is counted in the scorecard. The floor is never waived.
- **The floor** is `engine_floor: {require: [gates:verify]}`. Code adds it after the layers merge, so no layer can remove or replace it. It is the obligation form of the existing rule that a pack must have `gate_sets.verify`.
- An entry set to `null` in a later layer switches that default off, except the floor. `validate` lists every switched-off default as a note, never silently.
- No new condition is needed. `plan_approved` has no `when`: the `approval:plan` evidence already accepts a plan phase that `skip_if` skipped, as `on base`. Conditions keep reading only files and git, never run events.

### Evidence bound to a tree

One helper, `emit_gate_results(run, key, results, tree)`, replaces the four copies of the `gate_result` emit loop. The tree is snapshotted once per gate batch, before the gates run. A gate never edits the tree, and if one did, the audit would see the next snapshot differ. `review_result`, `step_findings` and `expectation_checked` take the tree that the reviewer or command saw. Every reader of these events uses `.get`, so older logs still load.

### The audit (`conformance.audit`) at `finish`

Its place in `feature_lane.run_finish` is after `run_sync` and `runner_commits_only`, and before `write_pr_body` and `push`. At that point the worktree is clean, and `HEAD^{tree}` is the tree that will be pushed.

1. Compute the obligations that apply: lane plus `when`, evaluated against the final diff (`origin/<base_branch>...HEAD`, the context `changed_paths` uses today).
2. Look for each obligation's evidence in `events.jsonl`.
3. If gate evidence is missing, the runner runs those gates now, on the final tree, and records them. This includes a run marked done, so the floor's verify gates always run on the tree that gets pushed. A failure is the existing `gate_failed` stop on the `finish` key. Approving it runs the audit again.
4. Write `conformance_checked{tree, obligations: [{id, require, status, evidence: [seq…], note}], passed}`.
5. `passed` is true when nothing is `missing`, because `waived` doesn't count against it. If `passed` is false and `enforce: block`, stop with `conformance_failed`. The stop names each missing obligation, and the `agentflow retry <run> <step>` that would produce the evidence. Nothing is pushed. In `report` mode the run goes on, and the PR section starts with **"Conformance: N missing (report mode)"**.

A `--dry-run` prints the obligations that would apply and runs nothing. The audit never asks a model anything.

### Scorecard (`agentflow scorecard`)

- **Signals:** `signals.run_signals`, plus `cost_usd`, `wall_h` and `human_wait_h` from `metrics.run_metrics`, plus the new `conformance_missing` and `conformance_waived`, the number of missing and waived obligations in the run's last `conformance_checked`. `FAMILIES` gives the direction (better is higher or lower).
- **Baseline:** `--freeze` writes `<state_dir>/runs/scorecard_baseline.json`. It lives per machine, next to the runs it summarises, because the runs aren't shared. It holds, per lane, the run ids it used, the date range, the engine version and `aggregate_signals` over those runs. Only ended runs count, as `retro.finished_runs` already defines them: done, failed or cancelled, and never a retro run. The command refuses to overwrite an existing baseline without `--replace`.
- **Current:** the same aggregation over the runs in `--from`/`--to`, by default the runs after the baseline's range.
- **Output:** a row per signal: baseline mean (n), current mean (n), delta, and better/worse/same. `conformance_missing` above 0 in any current run is printed first and marked **SLO**. It is the only hard SLO in this slice, because demotion SLOs belong to slice 4.

### Config (each key ships with all four companions)

| Key | Settings | `validate` | `test_config_edit` | Playbook §14 |
|---|---|---|---|---|
| `policy.enforce` | `_PROTECTED_KEYS` | one of `report`, `block` | refused as protected | one line |
| `policy.obligations` (and each `<id>`) | `_PROTECTED_KEYS` | `when` is a known condition; each `lanes` entry of an obligation the pack writes is a lane (an engine default that names a lane the pack lacks simply never applies); each `require` item parses; `gates:<set>` names a gate set; `step:<id>` names a step that some applicable lane walks (when an engine default names a step the pack's workflow lacks, the error says to set it to `null`); `approval:<phase>` names a phase with `needs_approval: true`; a `null` entry is reported as a note | refused as protected | one line, plus a short evidence table |

`policy` is protected and is never a user key. A Settings page that could relax the bar would defeat the bar.

### Events and signals

- New events: `conformance_checked` and the stop kind `conformance_failed`.
- New signal families: `conformance_missing` and `conformance_waived`. Both are `lower`, both are counts, and both are always present on a run that reached `finish`.
- The retro digest builds its signals table from `signals.run_signals` and `aggregate_signals`, so both reach the retro with no change to `retro.py`.

## Code Style

The engine's existing style: module docstring first, pure functions over event lists, `.get` for every event field, dataclasses for config, and no third-party imports. For example:

```python
def step_evidence(events: list[dict], step_id: str, after_seq: int) -> Evidence | None:
    """The last clean finish of `step_id` after `after_seq`, else None. A blocking finding the phase was not
    approved past since makes it stale."""
```

Event and key names are `snake_case`. User-facing text is one plain sentence, as the existing stop summaries are.

## Testing Strategy

TDD. Each test is written failing first. No test calls a real model or GitHub (`fake_agent.py`, `tests/bin/gh`).

- **Unit (`test_conformance.py`):**
  - evidence matching for each kind, over hand-built event lists
  - the last-code-change rule
  - stale `tree`
  - a human override
  - an approval `on base`
  - a conflicted `base_synced` makes an earlier review stale, and a clean one doesn't
  - a run marked done gets `waived by <who>` for the steps it skipped, but never for the floor
  - the floor cannot be removed (a pack setting `engine_floor: null` still gets it, and `validate` says so)
  - unknown `when` → required
- **Integration:** a fake-agent `feature` run and `bug` run on the `python` fixture pack reach `finish` with `conformance_checked.passed` true. The existing suite, unchanged, passes. A run with a gate result on an older tree gets the gates run again by the runner at `finish`. A config that requires a step the lane doesn't walk: `report` mode ships with the PR section showing the gap, and `block` mode stops `conformance_failed` with nothing pushed.
- **Events:** every `gate_result` written by the suite's runs has `tree`. The test asserts this over the events of the integration runs, not per site.
- **Scorecard (`test_scorecard.py`):** freeze, refusal to overwrite, `--replace`, deltas and direction from synthetic per-run signals, and `conformance_missing` flagged as SLO. Plus a `views.metrics_view` shape test.
- **Config:** layer merge by id, `null` switch-off, `validate` cases for each rule above, and `test_config_edit` protected-key refusals.
- **Guards:** the golden `explain` files, `test_portability` and `test_packaging` (CHANGELOG entry).

## Boundaries

- **Always:**
  - keep `agentflow explain` output byte-identical
  - read every new event field with `.get`
  - bind gate evidence to the exact final tree
  - let the runner, not a model, decide conformance
  - ship each key with its four companions
  - add a CHANGELOG line
- **Ask first:**
  - changing what the default obligations require beyond today's bar, because that changes this repo's and every pack's behaviour
  - flipping the engine default to `block`
  - any new top-level dashboard tab
  - adding a condition check kind
- **Never:**
  - let a model decide or skip an obligation
  - allow a pack, the user file or Settings to remove the floor
  - push in `block` mode with evidence missing
  - add a third-party dependency
  - edit the golden files
  - duplicate KPI logic that `signals` or `metrics` already compute
- **Security overlay:** this slice changes `layers.py`, which is on `overlays.security.when_paths`, so the plan and review use `security-and-hardening` and the `security-auditor` agent.

## Success Criteria

- All 9 acceptance criteria above hold.
- On this repository, the next 3 real feature or bug runs after merge each show a Conformance section with 0 missing in `report` mode, apart from misses explained by a conflicted merge. That is the evidence for switching this pack to `block`. The engine default stays `report` until slice 2.
- `agentflow scorecard --freeze` on this machine's past runs produces a baseline per lane, and `agentflow scorecard` prints it with no errors.

## Open Questions

None.

**Resolved (2026-11-10, grilling):**
- **Conflicted merge.** A `base_synced` with conflicts counts as the run's last code change, so reviews before it are stale. A clean merge doesn't count.
- **`block` mode.** The engine default stays `report` in this slice. This repository's pack switches to `block` after 3 real runs show 0 missing, apart from conflicted-merge misses. The engine default changes in slice 2.
- **Baseline location.** `<state_dir>/runs/scorecard_baseline.json`, per machine. It is written only by `--freeze`, and overwritten only with `--replace`.
- **No `plan_written` condition.** `plan_approved` has no `when`. The `approval:plan` evidence covers a plan already on the base branch, as `on base`.
- **Mark done.** Obligations left unmet because a human marked the run done are `waived by <who>`. They don't block, and they are counted in `conformance_waived`. The floor is never waived, and the runner runs its gates itself.
