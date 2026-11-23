"""Per-run failure and efficiency signals, and the fixed vocabulary a lesson target is written in."""

from __future__ import annotations


from datetime import UTC, datetime

from agentflow.metrics import run_metrics
from agentflow.signals import COUNT_FAMILIES, FAMILIES, RUNNER, aggregate_signals, parse_target, run_signals

EMPTY_METRICS = {"cost_usd": 0.0, "rework_cost_usd": 0.0, "phases": []}
VERIFY = ("lint", "unit")
NO_WAVES = {"waves": 0, "parallel_demoted": 0, "scope_violations": 0, "waves_dissolved": 0, "parallel_candidates": 0}


def _ev(etype: str, **fields) -> dict:
    return {"seq": 1, "ts": "2026-11-03T09:00:00.000+00:00", "type": etype, "by": "runner", **fields}


def _gate(key: str, gate: str, passed: bool, skipped: bool = False) -> dict:
    return _ev("gate_result", key=key, gate=gate, passed=passed, skipped=skipped)


def _metrics(phases: list[dict], cost: float = 0.0, rework: float = 0.0) -> dict:
    return {"cost_usd": cost, "rework_cost_usd": rework, "phases": phases}


def test_gate_first_pass_counts_only_the_first_result_per_key():
    evs = [_gate("build/t1", "lint", False), _gate("build/t1", "lint", True), _gate("build/t2", "lint", True)]
    assert run_signals(evs, EMPTY_METRICS)["gate_first_pass:lint"] == 0.5


def test_gate_first_pass_ignores_skipped_results_and_gates_that_never_ran():
    evs = [_gate("build/t1", "lint", True, skipped=True), _gate("build/t1", "unit", True)]
    out = run_signals(evs, EMPTY_METRICS)
    assert "gate_first_pass:lint" not in out and out["gate_first_pass:unit"] == 1.0


def test_human_rejections_ignore_the_runner():
    evs = [_ev("phase_rejected", phase="plan", by="human"), _ev("phase_rejected", phase="plan", by="runner")]
    assert run_signals(evs, EMPTY_METRICS)["human_rejections:plan"] == 1


def test_fix_attempts_and_review_blocking_are_counted_by_phase():
    evs = [_ev("fix_attempt", key="build/build/task-1/implement", attempt=1),
           _ev("fix_attempt", key="build/build/task-2/implement", attempt=1),
           _ev("fix_attempt", key="verify/verify", attempt=1),
           _ev("review_result", key="build/build/task-1/review", round=1, blocking=2),
           _ev("review_result", key="build/build/task-1/review", round=2, blocking=1)]
    out = run_signals(evs, EMPTY_METRICS)
    assert out["fix_attempts:build"] == 2 and out["fix_attempts:verify"] == 1
    assert out["review_blocking:build"] == 3


def test_guard_trips_count_each_violation_by_kind_and_denials_by_tool():
    evs = [_ev("guard_tripped", key="build/x", violations=[{"kind": "forbidden_path", "detail": "a"},
                                                          {"kind": "head_moved", "detail": "b"}]),
           _ev("guard_tripped", key="build/y", violations=[{"kind": "forbidden_path", "detail": "c"}]),
           _ev("permission_denied", key="build/x", tool="Bash"), _ev("permission_denied", key="build/x", tool="Bash"),
           _ev("permission_denied", key="build/x", tool=None)]
    out = run_signals(evs, EMPTY_METRICS)
    assert out["guard_trips:forbidden_path"] == 2 and out["guard_trips:head_moved"] == 1
    assert out["permission_denied:Bash"] == 2 and not [k for k in out if k.startswith("permission_denied:None")]


def test_read_only_denials_are_not_counted():
    evs = [_ev("permission_denied", key="build/t/review", tool="Bash", read_only=True),
           _ev("permission_denied", key="build/t/review", tool="Write", read_only=True),
           _ev("permission_denied", key="build/t/implement", tool="Bash"),
           _ev("permission_denied", key="build/t/implement", tool="Bash", read_only=False)]
    out = run_signals(evs, EMPTY_METRICS)
    assert out["permission_denied:Bash"] == 2  # by design in a read-only session: no rule to add for those
    assert "permission_denied:Write" not in out


def test_stops_and_plain_counters():
    evs = [_ev("hitl_raised", kind="blocked", key="build/x"), _ev("hitl_raised", kind="blocked", key="build/y"),
           _ev("hitl_raised", kind="boundary", key="plan/plan"),
           _ev("ci_round", key="ship/ci", round=1), _ev("ci_round", key="ship/ci", round=2),
           _ev("rolled_over", key="build/x"), _ev("progress_not_updated", key="build/x")]
    out = run_signals(evs, EMPTY_METRICS)
    assert out["stop:blocked"] == 2 and out["stop:boundary"] == 1
    assert (out["ci_rounds"], out["rollovers"], out["progress_not_updated"]) == (2, 1, 1)


def test_a_run_with_nothing_to_report_has_only_the_always_present_counts():
    assert run_signals([], EMPTY_METRICS) == {"ci_rounds": 0, "rollovers": 0, "progress_not_updated": 0, "rework_pct": 0,
                                              "gate_reruns": 0, "compactions": 0, "usage_holds": 0,
                                              "parked_units": 0, "park_resume_rework": 0, **NO_WAVES}


def test_a_run_with_a_gate_failure_but_no_ci_rounds_reports_zero_ci_rounds():
    out = run_signals([_gate("build/t1", "unit", False)], EMPTY_METRICS)
    assert out["ci_rounds"] == 0 and out["gate_first_pass:unit"] == 0.0


def test_a_phase_that_ran_reports_zero_fixes_and_rejections():
    phases = [{"phase": "build", "cost_usd": 0.0, "steps": []}, {"phase": "verify", "cost_usd": 0.0, "steps": []}]
    evs = [_ev("fix_attempt", key="build/x", attempt=1)]
    out = run_signals(evs, _metrics(phases))
    assert (out["fix_attempts:build"], out["fix_attempts:verify"]) == (1, 0)
    assert (out["human_rejections:build"], out["human_rejections:verify"]) == (0, 0)


def test_open_parameter_counts_stay_absent_and_count_families_are_lower_is_better():
    out = run_signals([], EMPTY_METRICS)
    assert not [k for k in out if k.split(":")[0] in ("guard_trips", "permission_denied", "stop")]
    assert COUNT_FAMILIES <= set(FAMILIES) and all(FAMILIES[f][0] == "lower" for f in COUNT_FAMILIES - {"waves"})
    assert isinstance(COUNT_FAMILIES, frozenset) and len(COUNT_FAMILIES) == 26


def test_cost_per_task_divides_by_the_steps_that_called_an_agent():
    phases = [{"phase": "build", "cost_usd": 6.0, "steps": [{"key": "build/a", "attempts": 1}, {"key": "build/b", "attempts": 2},
                                                           {"key": "build/gate", "attempts": 0}]},
              {"phase": "ship", "cost_usd": 0.0, "steps": [{"key": "ship/finish", "attempts": 0}]}]
    out = run_signals([], _metrics(phases, cost=6.0, rework=1.5))
    assert out["cost_per_task:build"] == 3.0 and "cost_per_task:ship" not in out
    assert out["rework_pct"] == 25.0


def test_turns_per_step_follow_a_turn_to_its_step_through_the_session():
    evs = [_ev("agent_started", step="build/a", session_id=None),
           _ev("agent_session", session_id="s1", key="build/a", pid=1),
           _ev("agent_turn", session_id="s1"), _ev("agent_turn", session_id="s1"), _ev("agent_turn", session_id="s1"),
           _ev("agent_started", step="build/b", session_id=None),
           _ev("agent_session", session_id="s2", key="build/b", pid=2),
           _ev("agent_turn", session_id="s2"),
           _ev("agent_started", step="plan/plan", session_id=None),
           _ev("agent_session", session_id="s3", key="plan/plan", pid=3),
           _ev("agent_turn", session_id="s3", key="plan/plan")]
    out = run_signals(evs, EMPTY_METRICS)
    assert out["turns_per_step:build"] == 2.0 and out["turns_per_step:plan"] == 1.0


def test_signals_survive_events_without_the_new_fields():
    evs = [_ev("guard_tripped", key="build/x"), _ev("review_result", key="build/x"), _ev("hitl_raised", key="build/x"),
           _ev("phase_rejected", by="human")]
    assert run_signals(evs, EMPTY_METRICS) == {"ci_rounds": 0, "rollovers": 0, "progress_not_updated": 0, "rework_pct": 0,
                                           "review_blocking:build": 0, "gate_reruns": 0, "compactions": 0,
                                           "usage_holds": 0, "parked_units": 0, "park_resume_rework": 0, **NO_WAVES}


def test_parse_target_accepts_only_the_vocabulary():
    assert parse_target("gate_first_pass:lint") == ("gate_first_pass", "lint")
    assert parse_target("rework_pct") == ("rework_pct", None)
    assert parse_target("rework_pct:x") is None and parse_target("vibes") is None and parse_target("stop") is None
    assert parse_target("stop:") is None and parse_target("vibes:x") is None


def test_every_family_has_a_direction_and_a_parameter_flag():
    assert all(d in ("lower", "higher") and isinstance(p, bool) for d, p in FAMILIES.values())
    assert FAMILIES["gate_first_pass"] == ("higher", True) and FAMILIES["rework_pct"] == ("lower", False)
    assert len(FAMILIES) == 53


def test_aggregate_means_over_runs_that_have_the_key():
    assert aggregate_signals([{"ci_rounds": 2}, {"ci_rounds": 0}, {}])["ci_rounds"] == {"runs": 2, "mean": 1.0}


def test_conformance_counts_come_from_the_last_check():
    first = _ev("conformance_checked", obligations=[{"id": "a", "status": "missing"}, {"id": "b", "status": "missing"}])
    last = _ev("conformance_checked", obligations=[{"id": "a", "status": "missing"}, {"id": "b", "status": "waived"},
                                                   {"id": "c", "status": "waived"}, {"id": "d", "status": "satisfied"}])
    out = run_signals([first, last], EMPTY_METRICS)
    assert (out["conformance_missing"], out["conformance_waived"]) == (1, 2)
    bare = run_signals([_ev("conformance_checked")], EMPTY_METRICS)
    assert (bare["conformance_missing"], bare["conformance_waived"]) == (0, 0)
    none = run_signals([], EMPTY_METRICS)
    assert "conformance_missing" not in none and "conformance_waived" not in none


def _timed(key: str, gate: str, passed: bool, tree: str | None, secs: float, cmd: str | None = "c1") -> dict:
    e = _ev("gate_result", key=key, gate=gate, passed=passed, skipped=False, duration_s=secs)
    if tree is not None:
        e["tree"] = tree
    if cmd is not None:
        e["cmd"] = cmd
    return e


def test_gate_minutes_add_up_by_phase():
    evs = [_timed("verify/verify", "it", True, "T1", 600), _timed("verify/verify", "lint", True, "T1", 30),
           _timed("build/b/task-1/implement", "lint", True, "T0", 60)]
    out = run_signals(evs, EMPTY_METRICS)
    assert out["gate_minutes:verify"] == 10.5 and out["gate_minutes:build"] == 1.0


def test_a_gate_run_again_on_a_tree_it_passed_on_is_a_rerun():
    evs = [_timed("verify/fix_findings", "it", True, "T1", 600),
           _timed("verify/verify", "it", True, "T1", 600),          # same tree, same command: a rerun
           _timed("review/fix_findings", "it", True, "T1", 600, cmd="c2"),  # the command changed: not one
           _timed("verify/verify", "it", True, "T2", 600),          # another tree: not one
           _timed("verify/verify", "lint", False, "T3", 5), _timed("verify/verify", "lint", True, "T3", 5),  # a retry
           _timed("verify/verify", "x", True, None, 5), _timed("verify/verify", "x", True, None, 5)]  # no tree
    assert run_signals(evs, EMPTY_METRICS)["gate_reruns"] == 1


def test_refine_rounds_count_only_the_runner_own_rounds():
    phases = [{"phase": "verify", "cost_usd": 0.0, "steps": []}, {"phase": "review", "cost_usd": 0.0, "steps": []}]
    evs = [_ev("phase_rejected", phase="verify", by="runner"), _ev("phase_rejected", phase="verify", by="runner"),
           _ev("phase_rejected", phase="review", by="me")]
    out = run_signals(evs, _metrics(phases))
    assert out["refine_rounds:verify"] == 2 and out["refine_rounds:review"] == 0
    assert out["human_rejections:review"] == 1


def test_escalations_undersized_and_fallbacks_are_counted_per_phase():
    events = [_ev("effort_decided", key="build/build/task-1/implement", level="medium", source="plan_sizer"),
              _ev("effort_escalated", key="build/build/task-1/implement", to="high", trigger="gate"),
              _ev("effort_decided", key="build/build/task-2/implement", level="medium", source="fallback"),
              _ev("effort_escalated", key="build/build/task-2/implement", to="high", trigger="gate"),
              _ev("effort_decided", key="verify/verify", level="high", source="sizer"),
              _ev("effort_escalated", key="verify/verify", to="xhigh", trigger="gate")]
    out = run_signals(events, {"phases": []})
    assert out["effort_escalations:build"] == 2 and out["undersized:build"] == 1   # a fallback is not undersized
    assert out["sizer_fallbacks:build"] == 1 and "undersized:verify" not in out
    assert parse_target("undersized:build") == ("undersized", "build")


def test_sizer_turns_and_cost_stay_out_of_step_signals():
    evs = [_ev("agent_started", step="build/a", session_id=None),
           _ev("agent_session", session_id="s1", key="build/a", pid=1),
           _ev("agent_turn", session_id="s1"), _ev("agent_turn", session_id="s1"),
           _ev("agent_started", step="build/a/size", session_id=None, sizer=True),
           _ev("agent_session", session_id="sz", key="build/a/size", pid=2, sizer=True),
           _ev("agent_turn", session_id="sz", sizer=True), _ev("agent_turn", session_id="sz", key="build/a/size", sizer=True)]
    assert run_signals(evs, EMPTY_METRICS)["turns_per_step:build"] == 2.0
    phases = [{"phase": "build", "cost_usd": 0.12, "sizing_cost_usd": 0.02, "steps": [{"key": "build/a", "attempts": 1}]}]
    out = run_signals([], _metrics(phases, cost=0.12))
    assert out["cost_per_task:build"] == 0.10


def test_the_sizing_families_are_lower_counts():
    for family in ("effort_escalations", "undersized", "sizer_fallbacks"):
        assert FAMILIES[family] == ("lower", True) and family in COUNT_FAMILIES


def test_a_fallback_counts_once_per_step_by_its_last_decision():
    key, other = "build/build/task-1/implement", "build/build/task-2/implement"
    events = [_ev("effort_decided", key=key, level="medium", source="fallback"),
              _ev("effort_decided", key=key, level="medium", source="fallback"),   # the plan sizer, tried again
              _ev("effort_decided", key=other, level="medium", source="fallback"),
              _ev("effort_decided", key=other, level="low", source="plan_sizer")]  # a later answer replaces it
    out = run_signals(events, {"phases": []})
    assert out["sizer_fallbacks:build"] == 1
    assert "sizer_fallbacks:build" not in run_signals(events[2:], {"phases": []})


DOUBT_FAMILIES = ("doubt_blocking", "doubt_rounds", "doubt_noise", "doubt_misread", "doubt_open", "doubt_actionable")


def _settled(phase: str, **fields) -> dict:
    base = {"reviews": 1, "rounds": 0, "first_blocking": 0, "open": 0, "stop": "clean", "actionable": 0, "noise": 0,
            "misread": 0}
    return _ev("doubt_settled", phase=phase, **{**base, **fields})


def test_doubt_signals_come_from_doubt_settled():
    first = _settled("plan", rounds=1, first_blocking=7, open=5, actionable=6, noise=4, misread=3)
    last = _settled("plan", rounds=2, first_blocking=3, open=1, actionable=1, noise=2, misread=0)
    other = _settled("define", rounds=1, first_blocking=1, open=0, actionable=1, noise=0, misread=0)
    out = run_signals([first, last, other], EMPTY_METRICS)
    assert {k: v for k, v in out.items() if k.endswith(":plan") and k.startswith("doubt_")} == {
        "doubt_blocking:plan": 3, "doubt_rounds:plan": 2, "doubt_open:plan": 1, "doubt_actionable:plan": 1,
        "doubt_noise:plan": 2, "doubt_misread:plan": 0}
    assert out["doubt_blocking:define"] == 1 and out["doubt_actionable:define"] == 1   # one window per phase
    reversed_ = run_signals([last, first], EMPTY_METRICS)   # the last event wins, not the largest value
    assert reversed_["doubt_blocking:plan"] == 7 and reversed_["doubt_misread:plan"] == 3


def test_no_doubt_key_without_doubt_settled():
    phases = [{"phase": "plan", "cost_usd": 0.0, "steps": []}]
    evs = [_ev("phase_rejected", phase="plan", by="runner", kind="reconcile"),
           _ev("findings_reconciled", phase="plan", key="plan/plan_reconcile", round=1, dispositions=[]),
           _ev("reconcile_stopped", phase="plan", round=1, reason="max_rounds", open=2)]
    out = run_signals(evs, _metrics(phases))
    assert not [k for k in out if k.startswith("doubt_")]
    assert not [k for k in run_signals([_settled("plan")], EMPTY_METRICS) if k.endswith(":define")]
    assert out["refine_rounds:plan"] == 0


def test_reconcile_rounds_are_not_refine_rounds():
    phases = [{"phase": "plan", "cost_usd": 0.0, "steps": []}]
    evs = [_ev("phase_rejected", phase="plan", by="runner", kind="reconcile"),
           _ev("phase_rejected", phase="plan", by="runner", kind="reconcile"),
           _ev("phase_rejected", phase="plan", by="runner"),
           _ev("phase_rejected", phase="plan", by="runner", kind="refine")]
    out = run_signals(evs, _metrics(phases))
    assert out["refine_rounds:plan"] == 2 and out["human_rejections:plan"] == 0
    only = run_signals(evs[:2], _metrics(phases))
    assert only["refine_rounds:plan"] == 0 and only["human_rejections:plan"] == 0


def test_doubt_targets_parse():
    for family in DOUBT_FAMILIES:
        assert parse_target(f"{family}:plan") == (family, "plan")
        assert parse_target(family) is None and parse_target(f"{family}:") is None
        assert family in FAMILIES and FAMILIES[family][1] is True and family not in COUNT_FAMILIES
    assert FAMILIES["doubt_actionable"] == ("higher", True)
    assert all(FAMILIES[f] == ("lower", True) for f in DOUBT_FAMILIES if f != "doubt_actionable")


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


def test_an_affected_batch_is_not_a_full_suite_run():
    affected = [{**_gate("review/fix_findings", g, True), "tier": "affected"} for g in VERIFY]
    assert run_signals(affected, EMPTY_METRICS, full_suite=VERIFY)["full_suite_runs"] == 0
    mixed = [_gate("ship/ci/fix-1", "lint", True), {**_gate("ship/ci/fix-1", "unit", True), "tier": "affected"}]
    assert run_signals(mixed, EMPTY_METRICS, full_suite=VERIFY)["full_suite_runs"] == 0
    full = [_gate("verify/verify", g, True) for g in VERIFY]
    assert run_signals(affected + full, EMPTY_METRICS, full_suite=VERIFY)["full_suite_runs"] == 1


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


def test_vuln_signals():
    alert = _ev("vuln_alert_read", alerts=[42], ids=["GHSA-cfgh-2345-vwxj"], package="requests", ecosystem="pip",
                manifest="requirements.txt", severity="high", dependabot_prs=[])
    start = _ev("run_started") | {"ts": "2026-11-03T09:00:00+00:00"}
    present = _ev("expectation_checked", key="build/advisory_red", expect="advisory_present", ok=True)
    early = _ev("pr_opened", key="review/early_pr", early=True) | {"ts": "2026-11-03T10:30:00+00:00"}
    final = _ev("pr_opened", key="ship/finish") | {"ts": "2026-11-03T12:00:00+00:00"}
    scope = _ev("vuln_scope", key="ship/finish", code_paths=["app.py"])
    out = run_signals([start, alert, present, scope, early, final], EMPTY_METRICS)
    assert out["advisory_reproduced"] == 1.0 and out["vuln_code_change"] == 1.0
    assert out["hours_to_pr:high"] == 1.5 and not [k for k in out if k.startswith("hours_to_pr:") and k != "hours_to_pr:high"]
    later = _ev("vuln_scope", key="ship/finish", code_paths=[])  # the last scope counts
    not_reproduced = {**present, "ok": False}
    out = run_signals([start, alert, not_reproduced, scope, later], EMPTY_METRICS)
    assert out["advisory_reproduced"] == 0.0 and out["vuln_code_change"] == 0.0
    assert not [k for k in out if k.startswith("hours_to_pr")]  # no pull request yet
    assert "vuln_code_change" not in run_signals([start, alert], EMPTY_METRICS)  # no scope recorded
    plain = run_signals([start, final], EMPTY_METRICS)  # a run of another lane carries none of them
    assert not [k for k in plain if k.split(":")[0] in ("advisory_reproduced", "vuln_code_change", "hours_to_pr")]
    odd = run_signals([start, {**alert, "severity": "huge"}, final], EMPTY_METRICS)
    assert not [k for k in odd if k.startswith("hours_to_pr")]  # only a known severity is a parameter
    assert FAMILIES["advisory_reproduced"] == ("higher", False)  # 1.0: the probe saw the advisory, the healthy case
    assert FAMILIES["vuln_code_change"] == ("lower", False)
    assert FAMILIES["hours_to_pr"] == ("lower", True)
    assert parse_target("hours_to_pr:critical") == ("hours_to_pr", "critical") and parse_target("hours_to_pr") is None
    assert parse_target("advisory_reproduced") == ("advisory_reproduced", None)
    assert not {"advisory_reproduced", "vuln_code_change", "hours_to_pr"} & COUNT_FAMILIES


def test_gate_cache_hit_rate():
    ran = [_gate("verify/verify", g, True) for g in ("a", "b", "c")] + [_gate("verify/verify", "d", False, skipped=True)]
    reused = [_ev("gates_reused", key="ship/finish", gates=["a"])]
    assert run_signals(ran + reused, EMPTY_METRICS)["gate_cache_hit_rate"] == 0.25  # 1 reused of 1 + 3 that ran
    assert "gate_cache_hit_rate" not in run_signals([], EMPTY_METRICS)
    assert "gate_cache_hit_rate" not in run_signals([_gate("verify/verify", "d", False, skipped=True)], EMPTY_METRICS)
    assert run_signals(ran, EMPTY_METRICS)["gate_cache_hit_rate"] == 0.0
    assert parse_target("gate_cache_hit_rate") == ("gate_cache_hit_rate", None)
    assert parse_target("gate_cache_hit_rate:x") is None


def _approved(phase, by="dashboard", **kw):
    return _ev("phase_approved", phase=phase, **{**kw, "by": by})


def test_informed_approval_is_the_share_of_human_approvals_that_viewed_every_newest_revision():
    spec = {"path": "s.md", "sha": "a" * 40}
    events = [_approved("define", produced=[spec], viewed=[spec, {"path": "x", "sha": "b" * 40}]),
              _approved("define", produced=[spec], viewed=[]),
              _approved("plan", by="alex"),                       # the CLI: no produced -> counted, not informed
              _approved("plan", produced=[]),                     # nothing produced -> not counted
              _approved("build", by="runner", produced=[spec])]   # the runner's -> never counted
    got = run_signals(events, EMPTY_METRICS)
    assert got["informed_approval:define"] == 0.5 and got["informed_approval:plan"] == 0.0
    assert "informed_approval:build" not in got
    assert parse_target("informed_approval:define") == ("informed_approval", "define")
    assert "informed_approval" not in COUNT_FAMILIES and FAMILIES["informed_approval"] == ("higher", True)


def test_route_upgrades_counts_upgrades():
    picked = _ev("route_profile", profile="S", applied=True, proposed_by="rule", upgraded_from=None, facts={}, reason="")
    upgraded = _ev("route_profile", profile="M", applied=True, proposed_by="runner", upgraded_from="S",
                   facts={"tasks": 9, "risk": False, "security": False}, reason="the plan has 9 tasks")
    assert run_signals([picked, upgraded], EMPTY_METRICS)["route_upgrades"] == 1
    assert run_signals([picked], EMPTY_METRICS)["route_upgrades"] == 0
    assert "route_upgrades" not in run_signals([], EMPTY_METRICS)
    assert FAMILIES["route_upgrades"] == ("lower", False) and "route_upgrades" in COUNT_FAMILIES
    assert parse_target("route_upgrades") == ("route_upgrades", None)


COST_METRICS = {
    **EMPTY_METRICS,
    "usd_split": {"input": 0.05, "output": 0.15, "cache_read": 0.22, "cache_write": 0.58},
    "by_step_kind": {
        "build.implement": {"sessions": 2, "turns": [1] * 3 + [37] * 7, "prefix_writes": [25000, 20000, 30000],
                            "tool_output_chars": 50000},
        "build.review": {"sessions": 1, "turns": [], "prefix_writes": [], "tool_output_chars": 0},
        "build.size": {"sessions": 0, "turns": [], "prefix_writes": [], "tool_output_chars": 0},
    },
}


def test_cost_signals_read_the_metrics_splits():
    s = run_signals([], COST_METRICS)
    assert s["cache_write_share"] == 0.58 and s["prefix_write_tokens"] == 25000
    assert s["session_turns_p90:build.implement"] == 37 and s["tool_output_kchars:build.implement"] == 5.0
    assert "session_turns_p90:build.review" not in s and "session_turns_p90:build.size" not in s
    assert "tool_output_kchars:build.review" not in s  # a session with no usage has no measured tool output
    assert "tool_output_kchars:build.size" not in s


def test_a_kind_with_turns_but_no_tool_output_reads_zero():
    metrics = {**EMPTY_METRICS, "by_step_kind": {
        "build.review": {"sessions": 2, "turns": [5, 9], "prefix_writes": [], "tool_output_chars": 0}}}
    s = run_signals([], metrics)
    assert s["tool_output_kchars:build.review"] == 0.0 and s["session_turns_p90:build.review"] == 9


def test_tool_output_is_per_session_that_reached_the_model():
    metrics = {**EMPTY_METRICS, "by_step_kind": {
        "build.implement": {"sessions": 3, "turns": [4, 6], "prefix_writes": [], "tool_output_chars": 6000}}}
    s = run_signals([], metrics)
    assert s["tool_output_kchars:build.implement"] == 3.0  # the zero-turn session doesn't dilute it to 2.0


def test_cache_write_share_is_rounded_to_four_places_and_tool_output_to_one():
    metrics = {**EMPTY_METRICS, "usd_split": {"input": 1.0, "output": 0.0, "cache_read": 1.0, "cache_write": 1.0},
               "by_step_kind": {"ship.fix": {"sessions": 3, "turns": [1, 1, 1], "prefix_writes": [], "tool_output_chars": 12345}}}
    s = run_signals([], metrics)
    assert s["cache_write_share"] == 0.3333 and s["tool_output_kchars:ship.fix"] == 4.1
    assert "prefix_write_tokens" not in s


def test_compactions_and_usage_holds_are_always_counted():
    empty = run_signals([], {"phases": []})
    assert (empty["compactions"], empty["usage_holds"]) == (0, 0)
    evs = [_ev("compacted", key="build/x"), _ev("run_held", key="build/x", reason="usage limit"),
           _ev("run_held", key="ship/ci", reason="ci_pending"), _ev("run_held", key="build/x", reason="outage")]
    s = run_signals(evs, EMPTY_METRICS)
    assert (s["compactions"], s["usage_holds"]) == (1, 1)


def test_a_run_with_no_usage_has_no_cost_share():
    metrics = {**EMPTY_METRICS, "usd_split": dict.fromkeys(("input", "output", "cache_read", "cache_write"), 0.0),
               "by_step_kind": {"build.implement": {"sessions": 1, "turns": [], "prefix_writes": [], "tool_output_chars": 0}}}
    for m in (metrics, EMPTY_METRICS):
        s = run_signals([], m)
        assert "cache_write_share" not in s and "prefix_write_tokens" not in s
        assert not [k for k in s if k.startswith(("session_turns_p90", "tool_output_kchars"))]


def test_the_new_families_are_targets_and_lower_is_better():
    assert parse_target("session_turns_p90:build.review") == ("session_turns_p90", "build.review")
    assert parse_target("tool_output_kchars:build.implement") == ("tool_output_kchars", "build.implement")
    assert parse_target("session_turns_p90") is None and parse_target("compactions:x") is None
    assert parse_target("cache_write_share") == ("cache_write_share", None)
    assert parse_target("usage_holds") == ("usage_holds", None) and parse_target("prefix_write_tokens") is not None
    for family in ("cache_write_share", "prefix_write_tokens", "compactions", "usage_holds"):
        assert FAMILIES[family] == ("lower", False)
    assert FAMILIES["session_turns_p90"] == ("lower", True) and FAMILIES["tool_output_kchars"] == ("lower", True)
    assert {"compactions", "usage_holds"} <= COUNT_FAMILIES
    assert not {"cache_write_share", "prefix_write_tokens", "session_turns_p90"} & COUNT_FAMILIES


T2 = "build/build/task-2"


def _park(key: str, **fields) -> dict:
    return _ev("step_parked", key=key, kind="blocked", shelf=None, shelved=[], **fields)


def test_parked_units_skip_demoted_parks():
    evs = [_park(f"{T2}/implement"), _park(f"{T2}/implement", demoted=True), _park("build/build/task-3/review")]
    assert run_signals(evs, EMPTY_METRICS)["parked_units"] == 2


def test_rework_counts_a_conflict_and_a_retry_once_per_task():
    evs = [_park(f"{T2}/implement"), _park("build/build/task-3/review"),
           _ev("unit_unparked", key=f"{T2}/implement", shelf="r", paths=[], conflict=True, by="human"),
           _ev("step_retry", by="human", key=f"{T2}/review"),
           _ev("step_retry", by="human", key=f"{T2}/commit"),
           _ev("unit_unparked", key="build/build/task-3/review", shelf="r", paths=[], conflict=False)]
    out = run_signals(evs, EMPTY_METRICS)
    assert (out["parked_units"], out["park_resume_rework"]) == (2, 1)
    evs.append(_ev("step_retry", by="human", key="build/build/task-3/implement"))
    assert run_signals(evs, EMPTY_METRICS)["park_resume_rework"] == 2


def test_rework_ignores_a_retry_before_the_park_and_one_on_another_task():
    evs = [_ev("step_retry", by="human", key=f"{T2}/review"), _ev("unit_unparked", key=f"{T2}/implement", conflict=True),
           _park(f"{T2}/implement"), _ev("step_retry", by="human", key="build/build/task-3/review"),
           _ev("step_retry", by="human", key="build/build/task-22/review")]
    out = run_signals(evs, EMPTY_METRICS)
    assert (out["parked_units"], out["park_resume_rework"]) == (1, 0)


def test_a_run_with_no_park_has_zero_of_both():
    out = run_signals([_ev("step_retry", by="human", key=f"{T2}/review")], EMPTY_METRICS)
    assert (out["parked_units"], out["park_resume_rework"]) == (0, 0)
    assert FAMILIES["parked_units"] == FAMILIES["park_resume_rework"] == ("lower", False)
    assert {"parked_units", "park_resume_rework"} <= COUNT_FAMILIES
    assert parse_target("park_resume_rework") == ("park_resume_rework", None)


def test_rework_ignores_the_runners_own_review_round_retries():
    runner_retry = {"seq": 1, "ts": "2026-11-03T09:00:00.000+00:00", "type": "step_retry", "by": "runner",
                    "key": f"{T2}/review", "reason": "re-review", "round": 1}
    evs = [_park(f"{T2}/implement"), runner_retry, {k: v for k, v in runner_retry.items() if k != "by"}]
    assert run_signals(evs, EMPTY_METRICS)["park_resume_rework"] == 0
    assert run_signals([*evs, _ev("step_retry", by="alice", key=f"{T2}/review")], EMPTY_METRICS)["park_resume_rework"] == 1


def test_a_park_on_a_non_task_key_is_its_own_unit():
    evs = [_park("build/verify-fix"), _ev("step_retry", by="human", key="build/verify-fix/gate"),
           _ev("step_retry", by="human", key="build/verify-fix-2")]
    out = run_signals(evs, EMPTY_METRICS)
    assert (out["parked_units"], out["park_resume_rework"]) == (1, 1)


def test_the_same_task_parked_twice_counts_two_parks_and_one_task_of_rework():
    evs = [_park(f"{T2}/implement"), _park(f"{T2}/review"), _ev("step_retry", by="human", key=f"{T2}/review")]
    out = run_signals(evs, EMPTY_METRICS)
    assert (out["parked_units"], out["park_resume_rework"]) == (2, 1)


def _at(sec: int, etype: str, **fields) -> dict:
    """An event `sec` seconds after 09:00:00 (so a build of 100 s spans 09:00:00 to 09:01:40)."""
    ts = f"2026-11-03T09:{sec // 60:02d}:{sec % 60:02d}.000+00:00"
    return {"seq": 1, "ts": ts, "type": etype, "by": "runner", **fields}


def _build_with_main(*extra: dict, end: int = 100) -> list[dict]:
    return [_at(0, "run_started"), _at(0, "phase_started", phase="build"),
            _at(0, "agent_started", key="build/build/task-1/implement", session_id="s1", pid=1, role="implement"),
            *extra,
            _at(end, "agent_exited", key="build/build/task-1/implement", session_id="s1", pid=1, exit_code=0),
            _at(end, "phase_started", phase="verify")]


def _signals(events: list[dict]) -> dict:
    return run_signals(events, run_metrics(events, datetime(2026, 11, 3, 10, 0, tzinfo=UTC)))


def test_two_overlapping_subagents_give_parallelism_above_one():
    evs = _build_with_main(
        _at(10, "subagent_started", tool_use_id="a", description="one"), _at(90, "subagent_finished", tool_use_id="a"),
        _at(10, "subagent_started", tool_use_id="b", description="two"), _at(90, "subagent_finished", tool_use_id="b"))
    assert _signals(evs)["agent_parallelism"] == 2.6


def test_parallelism_is_clipped_to_the_build_window_and_leaves_out_an_unfinished_subagent():
    evs = [_at(0, "phase_started", phase="define"),
           _at(0, "agent_started", key="define/spec", session_id="d", pid=1, role="spec"),
           _at(50, "agent_exited", key="define/spec", session_id="d", pid=1, exit_code=0),
           _at(50, "phase_started", phase="build"),
           _at(50, "agent_started", key="build/build/task-1/implement", session_id="s1", pid=2, role="implement"),
           _at(60, "subagent_started", tool_use_id="a"), _at(70, "subagent_started", tool_use_id="never"),
           _at(200, "subagent_finished", tool_use_id="a"),
           _at(150, "agent_exited", key="build/build/task-1/implement", session_id="s1", pid=2, exit_code=0),
           _at(150, "phase_started", phase="verify")]
    # window 50..150: the main session 100 s, the sub-agent 60..150 = 90 s
    assert _signals(evs)["agent_parallelism"] == 1.9


def test_a_serial_build_has_parallelism_one_and_a_run_with_no_build_agent_time_has_none():
    assert _signals(_build_with_main())["agent_parallelism"] == 1.0
    idle = [_at(0, "phase_started", phase="build"), _at(100, "phase_started", phase="verify")]
    assert "agent_parallelism" not in _signals(idle)
    assert "agent_parallelism" not in run_signals([], EMPTY_METRICS)


def test_wave_counts():
    evs = [_ev("wave_started", key="build/build/wave-1", wave=1, tasks=[1, 2]),
           _ev("wave_started", key="build/build/wave-2", wave=2, tasks=[3, 4]),
           _ev("parallel_demoted", key="build/build/task-5", task=5, reason="shared_files", wave=1),
           _ev("parallel_demoted", key="build/build/task-5", task=5, reason="shared_files", wave=1),
           _ev("parallel_demoted", key="build/build/task-5", task=5, reason="blocked", wave=1),
           _ev("parallel_demoted", key="build/build/task-6", task=6, reason="dissolved", wave=2),
           _ev("scope_violation", key="build/build/task-1", task=1, path="a.py", wave=1),
           _ev("scope_violation", key="build/build/task-2", task=2, path="b.py", wave=1),
           _ev("wave_dissolved", key="build/build/wave-2", wave=2, tasks=[3, 4], reason="stopped", shelf="r"),
           _ev("parallel_candidates", key="build/build", pairs=[[1, 2], [1, 3], [2, 3]]),
           _ev("parallel_candidates", key="build/build", pairs=[[1, 2]])]
    out = run_signals(evs, EMPTY_METRICS)
    assert (out["waves"], out["parallel_demoted"], out["scope_violations"], out["waves_dissolved"],
            out["parallel_candidates"]) == (2, 2, 2, 1, 1)


def test_a_run_with_no_wave_has_zero_counts_and_no_parallelism_key_without_build():
    out = run_signals([_ev("run_started")], EMPTY_METRICS)
    names = ("waves", "parallel_demoted", "scope_violations", "waves_dissolved", "parallel_candidates")
    assert [out[n] for n in names] == [0] * 5 and "agent_parallelism" not in out
    assert set(names) <= COUNT_FAMILIES and FAMILIES["waves"] == FAMILIES["agent_parallelism"] == ("higher", False)
    assert all(FAMILIES[n] == ("lower", False) for n in names[1:])
    assert parse_target("agent_parallelism") == ("agent_parallelism", None) and "agent_parallelism" not in COUNT_FAMILIES


# --------------------------------------------------------------------------- slice T7: replans and the ramp


def _would(phase: str, verdict: bool = True, **extra) -> dict:
    return _ev("would_auto_approve", phase=phase, mode="observe", verdict=verdict, held=[], missing=[], **extra)


def test_replans_and_auto_approvals_are_counted():
    evs = [_ev("speculation_started", phase="plan", after="define", basis={}, sha="a"),
           _ev("phase_rejected", phase="plan", by="runner", kind="replan"),
           _ev("phase_rejected", phase="plan", by="runner", kind="replan"),
           _ev("phase_rejected", phase="plan", by="runner", kind="refine"),
           _would("define"),
           _ev("phase_approved", phase="define", by="runner", ramp={"samples": 5, "profile": "S"}),
           _ev("phase_approved", phase="plan", by="me")]
    out = run_signals(evs, EMPTY_METRICS)
    assert out["replans"] == 2 and out["auto_approvals"] == 1
    assert FAMILIES["replans"] == ("lower", False) and FAMILIES["auto_approvals"] == ("higher", False)
    assert parse_target("replans") == ("replans", None) and parse_target("auto_approvals") == ("auto_approvals", None)
    assert "replans" not in run_signals([_would("define")], EMPTY_METRICS)
    assert run_signals([_would("define")], EMPTY_METRICS)["auto_approvals"] == 0
    assert run_signals([_ev("speculation_started", phase="plan", after="define", basis={}, sha="a")],
                       EMPTY_METRICS)["replans"] == 0


def test_ramp_agreement_is_one_per_agreeing_decision():
    evs = [_would("define"), _ev("phase_approved", phase="define", by="me"),
           _would("plan"), _ev("phase_rejected", phase="plan", by="me"),
           _would("review"), _ev("phase_rejected", phase="review", by="runner", kind="refine"),
           _ev("phase_approved", phase="review", by="me"),
           _would("verify"), _ev("phase_approved", phase="verify", by="runner", ramp={"samples": 5, "profile": "S"}),
           _would("ship", verdict=False), _ev("phase_approved", phase="ship", by="me"),
           _would("goal")]
    out = run_signals(evs, EMPTY_METRICS)
    assert out["ramp_agreement:define"] == 1.0 and out["ramp_agreement:plan"] == 0.0
    assert out["ramp_agreement:review"] == 1.0
    assert not [k for k in out if k in ("ramp_agreement:verify", "ramp_agreement:ship", "ramp_agreement:goal")]
    assert FAMILIES["ramp_agreement"] == ("higher", True) and parse_target("ramp_agreement:define")


def test_a_replan_is_no_refine_round():
    phases = [{"phase": "plan", "cost_usd": 0.0, "steps": []}]
    evs = [_ev("phase_rejected", phase="plan", by="runner", kind="replan"),
           _ev("phase_rejected", phase="plan", by="runner")]
    out = run_signals(evs, _metrics(phases))
    assert out["refine_rounds:plan"] == 1 and out["human_rejections:plan"] == 0
    assert run_signals(evs[:1], _metrics(phases))["refine_rounds:plan"] == 0


def test_signals_of_a_run_without_t7_events_are_unchanged():
    evs = [_ev("phase_rejected", phase="plan", by="runner"), _ev("phase_approved", phase="plan", by="me"),
           _ev("phase_rejected", phase="plan", by="me")]
    out = run_signals(evs, EMPTY_METRICS)
    assert not [k for k in out if k.startswith(("replans", "ramp_agreement", "auto_approvals"))]


def test_the_human_decision_rule_is_shared_with_the_ramp():
    from agentflow import autonomy
    from agentflow.signals import human_agreement

    assert RUNNER == autonomy.AUTO_BY
    assert human_agreement([_would("define"), _ev("phase_approved", phase="define", by="me")], "define") is True
    assert human_agreement([_would("define"), _ev("phase_rejected", phase="define", by="me")], "define") is False
    assert human_agreement([_would("define")], "define") is None
    assert human_agreement([_ev("phase_approved", phase="define", by="me")], "define") is None
