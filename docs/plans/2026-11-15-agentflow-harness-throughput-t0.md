# Throughput slice T0 (Measure) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the throughput proposal's KPIs come from a command: three new signals (`lead_time_h`, `human_blocked_share`, `full_suite_runs`) in `signals.py`, and so in the scorecard and lessons, plus the §1.1 wall-time breakdown in `agentflow metrics`. Then freeze the baseline that slices T1–T7 are measured against.

**Architecture:** `signals.run_signals` gains the three keys. It stays pure: the one fact it can't read from events, the pack's `gate_sets.verify`, comes in as a keyword argument, `full_suite`. Every caller that has a `Config` passes it: `views.metrics_view`, `views.lessons_view` → `lessons.effects`, `flow.cmd_scorecard` and `dashboard._scorecard` → `scorecard.freeze` / `scorecard_view` → `scorecard.run_row`. `views.metrics_table` reads the buckets `metrics.run_metrics` already computes and prints them as time and share, per run and in total. No event, config key or prompt changes, so a run behaves exactly as before.

**Tech Stack:** Python 3.12 stdlib, pytest. No new dependency.

**Spec:** `docs/specs/agentflow-throughput-proposal.md`, slice **T0 Measure** (§8), KPIs in §2 and §6, the breakdown in §1.1, and verification "T0". The other slices (T1–T7) each get their own plan once the slice before them has been measured on 5 runs (§6, §8). They are not part of this plan.

## Global Constraints

- Engine dependencies stay stdlib + `pyyaml>=6`.
- No project strings in the engine (`test_portability.py` `DENYLIST`). Don't name the repository whose runs §1 measured in code, tests, docs or commit messages; call it "the measured repository".
- The golden files `agentflow/tests/fixtures/explain/*.txt` stay byte-identical.
- `signals.py` stays pure: it reads no file and no clock, and it reads event fields with `.get`, so a log written before this slice gives the same signals.
- This slice adds **no config key**, **no event type** and **no prompt text**. If one becomes necessary, it needs the four companions from CLAUDE.md (a Settings field or `_PROTECTED_KEYS`, `validate`, `test_config_edit`, playbook §14).
- The KPI definitions are the spec's (§6):
  - `lead_time_h`: `run_started` → `pr_marked_ready`.
  - `human_blocked_share`: time with no running agent, no gate and an open item, divided by wall time.
  - `full_suite_runs`: local runs of the full suite (`gate_sets.verify`).
- Each user-visible change adds a line under `## [Unreleased]` in `CHANGELOG.md`.
- Tests run as `uv run --no-project --with pyyaml --with pytest pytest <path> --import-mode=prepend -q`. Never create or edit `uv.lock`.

## Review Focus

1. **Old logs.** The baseline is frozen over runs written before this slice. All three signals must come from events and fields those runs already have. In particular, a `gate_result` with no `tree`, `cmd` or `duration_s` still counts toward `full_suite_runs` (Task 1, `test_full_suite_runs_reads_old_gate_results`).
2. **A run that never reached a ready PR** (cancelled, failed, live, or a bug run with no PR) has **no** `lead_time_h` key, never `0`. A 0 would drag the mean lead time toward the target (Task 1, `test_no_lead_time_without_a_ready_pr`).
3. **Reused and skipped gates.**
   - A batch where some verify gates were reused (they write a `gates_reused` event and no `gate_result`) is not a full run. Neither is the conformance audit running only the missing gates.
   - A verify gate reported `skipped` (`when_exists` target missing) still counts as part of a full run. Otherwise a pack with an optional code root would never count one (Task 1, `test_full_suite_runs_counts_whole_batches_only`).
4. **The fix loop running verify again on the same step.** Two batches on the same key, with agent events between them, are two full runs, not one (Task 1, `test_full_suite_runs_counts_each_batch_on_the_same_step`).
5. **A run with no wall time** (only `run_started`, or every event at one instant). There must be no `human_blocked_share` key and no division by zero, and `metrics_table` must leave out the per-run wall line and the totals table (Tasks 1 and 3, `test_a_run_with_no_wall_time_has_no_blocked_share` and `test_metrics_table_skips_the_breakdown_with_no_wall_time`).

---

### Task 1: The three throughput signals

**Files:**
- Modify: `agentflow/signals.py` (module docstring, `FAMILIES`, `COUNT_FAMILIES`, `run_signals`, two new private helpers)
- Modify: `docs/agentflow-playbook.md` §15: the count-signal list (the line beginning "A run counts only when it has the target's key") and the `target` vocabulary line (the line beginning "- **`target`**")
- Modify: `CHANGELOG.md` (`### Added`)
- Test: `agentflow/tests/test_signals.py`

**Interfaces:**
- Produces:
  - `run_signals(events: list[dict], metrics: dict, *, full_suite: Collection[str] = ()) -> dict[str, float]`. The signature is unchanged for existing callers.
  - New keys:
    - `lead_time_h: float`, in hours, rounded to 3 places. Present only when the run has both a `run_started` and a `pr_marked_ready`.
    - `human_blocked_share: float`, in 0..1, rounded to 4 places. It is `metrics["buckets"]["human"] / metrics["wall_s"]`. Present only when `wall_s > 0`.
    - `full_suite_runs: int`. Present exactly when `full_suite` is non-empty.
  - `FAMILIES["lead_time_h"] == FAMILIES["human_blocked_share"] == FAMILIES["full_suite_runs"] == ("lower", False)`.
  - `"full_suite_runs" in COUNT_FAMILIES`, which now has 16 members.
- Consumes: nothing new.

Notes on the definitions:
- `human_blocked_share` is the metrics `human` bucket's share. That bucket is wall time while a stop, a phase approval or a ready phase waits, and nothing else has started. Today that is exactly "no running agent, no gate and an open item". T4 (parking) will let agents run while an item is open, and it must keep this signal true then. Write that in the docstring.
- A **batch** is a maximal run of consecutive `gate_result` events with the same `key`. A batch is a **full run** when its `gate` names include every name in `full_suite`. Passed, failed and skipped all count, because `gates.run_gates` runs every gate even after a failure.

- [ ] **Step 1: Write the failing tests** in `agentflow/tests/test_signals.py`. Use the file's `_ev`, `_gate` and `_metrics` helpers, and `VERIFY = ("lint", "unit")`.

```python
def test_lead_time_runs_from_start_to_the_ready_pr():
    evs = [_ev("run_started") | {"ts": "2026-11-03T09:00:00+00:00"},
           _ev("pr_marked_ready") | {"ts": "2026-11-03T11:30:00+00:00"}]
    assert run_signals(evs, EMPTY_METRICS)["lead_time_h"] == 2.5

def test_no_lead_time_without_a_ready_pr():
    assert "lead_time_h" not in run_signals([_ev("run_started"), _ev("run_cancelled")], EMPTY_METRICS)

def test_human_blocked_share_is_the_human_bucket_over_wall_time():
    m = {**EMPTY_METRICS, "wall_s": 400.0, "buckets": {"human": 100.0, "agent": 300.0}}
    assert run_signals([], m)["human_blocked_share"] == 0.25

def test_a_run_with_no_wall_time_has_no_blocked_share():
    assert "human_blocked_share" not in run_signals([], {**EMPTY_METRICS, "wall_s": 0.0, "buckets": {}})

def test_full_suite_runs_counts_whole_batches_only():
    whole = [_gate("verify/verify", "lint", True), _gate("verify/verify", "unit", False, skipped=True)]
    partial = [_ev("gates_reused", key="ship/finish", gates=["lint"]), _gate("ship/finish", "unit", True)]
    assert run_signals(whole + partial, EMPTY_METRICS, full_suite=VERIFY)["full_suite_runs"] == 1

def test_full_suite_runs_counts_each_batch_on_the_same_step():
    batch = [_gate("verify/verify", "lint", True), _gate("verify/verify", "unit", False)]
    evs = batch + [_ev("agent_started", key="verify/verify"), _ev("agent_exited", key="verify/verify")] + batch
    assert run_signals(evs, EMPTY_METRICS, full_suite=VERIFY)["full_suite_runs"] == 2

def test_full_suite_runs_reads_old_gate_results():
    old = [{"seq": 1, "type": "gate_result", "key": "verify/verify", "gate": g, "passed": True} for g in VERIFY]
    assert run_signals(old, EMPTY_METRICS, full_suite=VERIFY)["full_suite_runs"] == 1

def test_full_suite_runs_is_absent_without_a_verify_set_and_zero_when_none_ran():
    assert "full_suite_runs" not in run_signals([], EMPTY_METRICS)
    assert run_signals([_gate("build/b", "lint", True)], EMPTY_METRICS, full_suite=VERIFY)["full_suite_runs"] == 0

def test_the_throughput_families_are_plain_lower_signals():
    for family in ("lead_time_h", "human_blocked_share", "full_suite_runs"):
        assert FAMILIES[family] == ("lower", False) and parse_target(family) == (family, None)
    assert "full_suite_runs" in COUNT_FAMILIES and "lead_time_h" not in COUNT_FAMILIES
```

Also change `len(COUNT_FAMILIES) == 15` to `== 16` in `test_open_parameter_counts_stay_absent_and_count_families_are_lower_is_better`. The two tests that compare `run_signals([], EMPTY_METRICS)` with an exact dict must still pass unchanged: `EMPTY_METRICS` has no `wall_s`, and no `full_suite` is passed.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_signals.py --import-mode=prepend -q`
Expected: the new tests FAIL with `KeyError` or `TypeError: unexpected keyword argument 'full_suite'`, and the length check fails on 15.

- [ ] **Step 3: Implement them in `agentflow/signals.py`**
  - Add `_lead_time(events: list[dict]) -> dict[str, float]`, using `events.parse_iso` on the first `run_started` and the first `pr_marked_ready`.
  - Add `_full_suite_runs(events: list[dict], full_suite: Collection[str]) -> int`, which walks the batches as defined above.
  - In `run_signals`, set `human_blocked_share` from `metrics.get("buckets")` and `metrics.get("wall_s")`. Use `.get` throughout: test metrics dicts carry only some keys.
  - Add the three `FAMILIES` entries with comments, and add `full_suite_runs` to `COUNT_FAMILIES`.
  - Extend the module docstring with one sentence per key.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_signals.py agentflow/tests/test_lessons.py agentflow/tests/test_scorecard.py --import-mode=prepend -q`
Expected: PASS.

- [ ] **Step 5: Docs.**
  - Add `full_suite_runs` to the playbook §15 count-signal list.
  - Add the three keys, each with its definition in a parenthesis, to the `target` vocabulary line.
  - Add to `CHANGELOG.md` `### Added`: "Three throughput signals, `lead_time_h`, `human_blocked_share` and `full_suite_runs`, for the scorecard and lesson targets."

- [ ] **Step 6: Commit**

```bash
git add agentflow/signals.py agentflow/tests/test_signals.py docs/agentflow-playbook.md CHANGELOG.md
git commit -m "Add lead time, human-blocked share and full-suite runs to a run's signals."
```

---

### Task 2: Pass the pack's verify set to every signal reader

**Files:**
- Modify: `agentflow/scorecard.py` (`run_row`, `freeze`, `scorecard_view`, `EFFICIENCY` docstring sentence)
- Modify: `agentflow/lessons.py` (`effects`, `_all_runs`, `_run_facts`)
- Modify: `agentflow/views.py` (`metrics_view`, `lessons_view`)
- Modify: `agentflow/flow.py` (`cmd_scorecard`)
- Modify: `agentflow/dashboard.py` (`_scorecard`)
- Test: `agentflow/tests/test_scorecard.py`, `agentflow/tests/test_lessons.py`, `agentflow/tests/test_metrics.py`

**Interfaces:**
- Consumes: `run_signals(..., *, full_suite=...)` from Task 1.
- Produces:
  - `scorecard.run_row(run: Run, now: datetime, *, full_suite: Collection[str] = ()) -> dict[str, float]`
  - `scorecard.freeze(repo, lane, date_from, date_to, replace, now, *, full_suite: Collection[str] = ()) -> dict`
  - `scorecard.scorecard_view(repo, lane, date_from, date_to, now, *, full_suite: Collection[str] = ()) -> dict`
  - `lessons.effects(repo: Path, lessons: list[Lesson], now: datetime, *, full_suite: Collection[str] = ()) -> list[LessonEffect]`. `_all_runs` and `_run_facts` take `full_suite` as a third positional parameter.
  - Every caller that holds a `Config` passes `full_suite=tuple(cfg.gate_sets.get("verify") or ())`. In the dashboard that is `self.server.cfg`.

- [ ] **Step 1: Write the failing tests**
  - In `test_scorecard.py`, change the `fake_runs` fixture's stub to `lambda run, now, **_kw: dict(table[run.id][2])`, then add:

```python
def test_run_row_carries_the_throughput_signals(flow_repo):
    verify = tuple(flow_repo.cfg.gate_sets["verify"])
    run = flow_repo.new_run()
    for gate in verify:
        run.emit("gate_result", key="verify/verify", gate=gate, passed=True, skipped=False, duration_s=1.0)
    run.emit("pr_marked_ready", url="u", key="ship/ci")
    run.emit("run_finished")
    row = scorecard.run_row(run, datetime.now(UTC), full_suite=verify)
    assert row["full_suite_runs"] == 1 and row["lead_time_h"] >= 0 and "human_blocked_share" in row
```

  - In `test_lessons.py`:

```python
FULL_LESSON = Lesson("L-0002", "Once", "Run the suite once.", ("build",), (), None, "full_suite_runs", None)

def test_a_full_suite_target_is_measured_with_the_verify_set(tmp_git):
    for n in range(4):
        _write_run(tmp_git, n, gates=[("unit", True)])
    for n in range(4, 7):
        _write_run(tmp_git, n, injected=["L-0002"])
    (eff,) = effects(tmp_git, [FULL_LESSON], NOW, full_suite=("unit",))
    assert (eff.baseline, eff.after, eff.verdict) == (1.0, 0.0, "improved")
```

  - In `test_metrics.py`:

```python
def test_metrics_view_counts_full_suite_runs_with_the_pack_verify_set(flow_repo):
    from agentflow.views import metrics_view
    run = flow_repo.new_run()
    for gate in flow_repo.cfg.gate_sets["verify"]:
        run.emit("gate_result", key="verify/verify", gate=gate, passed=True, skipped=False, duration_s=0.0)
    run.emit("run_finished")
    assert metrics_view(flow_repo.root, flow_repo.cfg)["runs"][0]["signals"]["full_suite_runs"] == 1
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_scorecard.py agentflow/tests/test_lessons.py agentflow/tests/test_metrics.py --import-mode=prepend -q`
Expected: the three new tests FAIL, with `TypeError` on `full_suite` or `KeyError: 'full_suite_runs'`.

- [ ] **Step 3: Implement it.**
  - Thread `full_suite` through the signatures above, and pass it from `views.metrics_view`, `views.lessons_view`, `flow.cmd_scorecard` (to both `freeze` and `scorecard_view`) and `dashboard._scorecard`.
  - `freeze` and `scorecard_view` pass it to every `run_row` call.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_scorecard.py agentflow/tests/test_lessons.py agentflow/tests/test_metrics.py agentflow/tests/test_dashboard.py --import-mode=prepend -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agentflow/scorecard.py agentflow/lessons.py agentflow/views.py agentflow/flow.py agentflow/dashboard.py agentflow/tests/test_scorecard.py agentflow/tests/test_lessons.py agentflow/tests/test_metrics.py
git commit -m "Count full-suite runs against the pack's verify set in metrics, the scorecard and lesson effects."
```

---

### Task 3: The wall-time breakdown in `agentflow metrics`

**Files:**
- Modify: `agentflow/views.py` (`metrics_table`, plus two private helpers)
- Modify: `docs/agentflow-playbook.md`: one sentence after the scorecard paragraph in §15 (the paragraph beginning "The conformance audit at `finish`") saying that `agentflow metrics` prints the wall-time breakdown and the three KPIs
- Modify: `CHANGELOG.md` (`### Added`)
- Test: `agentflow/tests/test_metrics.py`

**Interfaces:**
- Consumes:
  - `row["signals"]` (Task 1's keys);
  - `metrics["buckets"]` and `metrics["wall_s"]`;
  - `view["totals"]["buckets"]` and `view["totals"]["bucket_pct"]` (already in `metrics.aggregate`);
  - `view["buckets"]`, the bucket order.
- Produces: text output only. `--json` and `--csv` are unchanged.

The output, below each run's header line. Only buckets above 0 appear, in `view["buckets"]` order, each share rounded to a whole percent. A KPI part appears only when its signal exists, and the line is left out when none does. Time uses the existing `_short`:
```
  wall time: agent 1h00m 50% · human 1h00m 50%
  lead time 2h00m · blocked on a human 50% · full suite ×0
```
After the existing `total …` line, when the totals' wall time is above 0:
```
Wall time: 2h00m over 1 run
  BUCKET  TIME   SHARE
  agent   1h00m  50.0%
  human   1h00m  50.0%
Lead time: median 2h00m, mean 2h00m (1 run reached a ready PR) · blocked on a human 50.0% of wall time · full suite runs: mean 0 per run
```
- The totals' "blocked on a human" is `totals["bucket_pct"]["human"]`, which is weighted by wall time like §1.1. It is not the mean of the per-run shares.
- The median and mean lead times come from the runs whose `signals` have `lead_time_h` (`statistics.median`). The "Lead time:" part is left out when no run has it.
- "full suite runs" is `view["signals"]["full_suite_runs"]["mean"]`, left out when absent.

- [ ] **Step 1: Write the failing tests** in `agentflow/tests/test_metrics.py`, with the file's `Log`, `aggregate`, `BUCKETS`, and `run_signals` / `aggregate_signals` from `agentflow.signals`:

```python
def _view(log: Log) -> dict:
    m = log.metrics()
    row = {"run_id": "r1", "lane": "feature", "module": "m", "status": "done", "metrics": m,
           "signals": run_signals(log.events, m, full_suite=("unit",))}
    return {"lane": None, "all_time": True, "from": None, "to": None, "buckets": list(BUCKETS), "runs": [row],
            "totals": aggregate([row]), "signals": aggregate_signals([row["signals"]])}

def test_metrics_table_prints_the_wall_time_breakdown_and_the_kpis():
    key = "build/build/task-1/implement"
    log = Log().add("run_started").add("phase_started", phase="build").call(0, 60, key, 1.0, session="s1")
    log.at(60).add("hitl_raised", key=key, kind="blocked").at(120).add("hitl_answered", key=key)
    log.add("pr_marked_ready").add("run_finished")
    lines = metrics_table(_view(log)).splitlines()
    assert "  wall time: agent 1h00m 50% · human 1h00m 50%" in lines
    assert "  lead time 2h00m · blocked on a human 50% · full suite ×0" in lines
    assert ["agent", "1h00m", "50.0%"] in [line.split() for line in lines]
    assert any(line.startswith("Lead time: median 2h00m, mean 2h00m (1 run reached a ready PR)") for line in lines)

def test_metrics_table_skips_the_breakdown_with_no_wall_time():
    text = metrics_table(_view(Log().add("run_started")))
    assert "wall time:" not in text and "Wall time:" not in text and "Lead time:" not in text
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_metrics.py --import-mode=prepend -q`
Expected: the two new tests FAIL with `AssertionError`.

- [ ] **Step 3: Implement it in `agentflow/views.py`.**
  - Add `_wall_line(buckets: dict[str, float], wall_s: float, order: list[str]) -> str | None` and `_kpi_line(signals: dict[str, float]) -> str | None` for the two per-run lines.
  - Build the totals block inline in `metrics_table`, with the existing `_table` helper for the BUCKET/TIME/SHARE rows.
  - The share in `_wall_line` is `round(100 * seconds / wall_s)`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_metrics.py agentflow/tests/test_portability.py --import-mode=prepend -q`
Expected: PASS.

- [ ] **Step 5: Docs.**
  - Add the playbook sentence described in Files.
  - Add to `CHANGELOG.md` `### Added`: "`agentflow metrics` prints where each run's wall time went, as time and share per bucket, with its lead time, the share blocked on a human and its full-suite runs, and the same in total."

- [ ] **Step 6: Commit**

```bash
git add agentflow/views.py agentflow/tests/test_metrics.py docs/agentflow-playbook.md CHANGELOG.md
git commit -m "Print each run's wall-time breakdown and throughput KPIs in agentflow metrics."
```

---

### Finish

- [ ] Run the full suite: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests --import-mode=prepend -q`. Expected: all pass (it takes 25–60 min). `git diff --stat main -- agentflow/tests/fixtures/explain` must be empty.
- [ ] `bin/agentflow validate` prints `0 warnings`.

### After merge (a human, in the measured repository)

This plan's executor doesn't do these steps. They need the real runs, which live in another repository's state dir.

1. `uv tool install --reinstall --from <this checkout> agentflow`.
2. **Check the figures (spec "Verification T0").** `agentflow metrics --all` in the measured repository. The totals table's `human`, `agent` and `gates` rows must be within 5 % of §1.1: 32.1 h, 24.2 h and 17.2 h, out of 99.3 h of wall time over 16 runs. If they differ by more, find out why before freezing. The proposal's script and `metrics.run_metrics` may disagree on a wait kind (`phase_ready` is a human wait here) or on the `down` bucket (silent gaps over `stall_s`).
3. **Freeze the baseline.** `agentflow scorecard --freeze --to 2026-11-15`. Add `--replace` if a baseline already exists: an older baseline lacks the three new keys, and its rows would read `-`. Slices T1–T7 are measured against this file.

### Not in this slice

- `human_touches`, `agent_parallelism`, `gate_cache_hit_rate` and `park_resume_rework` (§6). They need events that T1–T5 add, and each slice adds its own.
- Splitting lead time by plan size (the ≥ 9-task and ≤ 3-task KPIs of §2). Compare by hand from `metrics --json` until T3 records `route_profile`.
- A renamed verify gate. A run whose batches used a gate's old name never counts as a full run under today's verify set. Step 2 above catches a gap this causes in the baseline.
