"""Per-run failure and efficiency signals, and the fixed vocabulary a lesson target is written in."""

from __future__ import annotations


from agentflow.signals import COUNT_FAMILIES, FAMILIES, aggregate_signals, parse_target, run_signals

EMPTY_METRICS = {"cost_usd": 0.0, "rework_cost_usd": 0.0, "phases": []}


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
                                              "gate_reruns": 0}


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
    assert COUNT_FAMILIES <= set(FAMILIES) and all(FAMILIES[f][0] == "lower" for f in COUNT_FAMILIES)
    assert isinstance(COUNT_FAMILIES, frozenset) and len(COUNT_FAMILIES) == 15


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
                                           "review_blocking:build": 0, "gate_reruns": 0}


def test_parse_target_accepts_only_the_vocabulary():
    assert parse_target("gate_first_pass:lint") == ("gate_first_pass", "lint")
    assert parse_target("rework_pct") == ("rework_pct", None)
    assert parse_target("rework_pct:x") is None and parse_target("vibes") is None and parse_target("stop") is None
    assert parse_target("stop:") is None and parse_target("vibes:x") is None


def test_every_family_has_a_direction_and_a_parameter_flag():
    assert all(d in ("lower", "higher") and isinstance(p, bool) for d, p in FAMILIES.values())
    assert FAMILIES["gate_first_pass"] == ("higher", True) and FAMILIES["rework_pct"] == ("lower", False)
    assert len(FAMILIES) == 21


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
