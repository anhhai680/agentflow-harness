"""Run metrics: time by phase, step and bucket, cost by phase and `model · effort`, from the event log alone."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from agentflow.metrics import aggregate, run_metrics

T0 = datetime(2026, 11, 3, 9, 0, tzinfo=UTC)


class Log:
    """Events with a clock: `at(minutes)` moves it, `add(type, **fields)` writes one event at the current time."""

    def __init__(self) -> None:
        self.events: list[dict] = []
        self.minute = 0.0

    def at(self, minute: float) -> Log:
        self.minute = minute
        return self

    def add(self, etype: str, **fields) -> Log:
        ts = (T0 + timedelta(minutes=self.minute)).isoformat(timespec="milliseconds")
        self.events.append({"seq": len(self.events) + 1, "ts": ts, "type": etype, "by": "runner", **fields})
        return self

    def call(self, start: float, end: float, key: str, cost: float, *, session: str, model: str = "claude-opus-5-5",
             alias: str = "opus", effort: str = "medium", role: str = "build", estimated: bool = False) -> Log:
        self.at(start).add("agent_started", agent="claude", pid=hash(session) % 10000, model=alias, effort=effort,
                           step=key, session_id=None, role=role, phase=key.split("/")[0])
        self.add("agent_session", session_id=session, model=model, key=key, pid=hash(session) % 10000)
        self.at(end).add("agent_exited", session_id=session, exit_code=0, cost=cost, cost_estimated=estimated, key=key)
        return self

    def metrics(self, now_minute: float | None = None, **kw) -> dict:
        now = T0 + timedelta(minutes=self.minute if now_minute is None else now_minute)
        return run_metrics(self.events, now, **kw)


def phase(m: dict, name: str) -> dict:
    return next(p for p in m["phases"] if p["phase"] == name)


def step(m: dict, key: str) -> dict:
    return next(s for p in m["phases"] for s in p["steps"] if s["key"] == key)


def minutes(buckets: dict) -> dict:
    return {k: round(v / 60, 2) for k, v in buckets.items() if v}


def test_a_finished_run_splits_its_time_into_agent_gates_human_and_runner():
    log = Log().add("run_started", run_id="r", pipeline="feature", params={"module": "crm"})
    log.at(1).add("phase_started", phase="plan")
    log.add("step_started", key="plan/plan", phase="plan", mode="first", round=None)
    log.call(1, 11, "plan/plan", 2.5, session="s1", role="plan")
    log.at(12).add("phase_approval_raised", phase="plan", round=0, summary="done")
    log.at(42).add("phase_approved", phase="plan", by="dashboard")
    log.at(43).add("phase_started", phase="build")
    log.add("step_started", key="build/build/task-1/implement", phase="build", mode="first", round=None)
    log.call(43, 63, "build/build/task-1/implement", 4.0, session="s2", model="claude-sonnet-5-5", alias="sonnet")
    log.at(68).add("gate_result", key="build/build/task-1/implement", gate="py_unit", passed=True, skipped=False,
                   duration_s=180.0)
    log.add("gate_result", key="build/build/task-1/implement", gate="py_lint", passed=True, skipped=False, duration_s=60.0)
    log.at(70).add("run_finished")
    m = log.metrics(now_minute=500)

    assert m["live"] is False and m["ended_at"] == log.events[-1]["ts"]
    assert m["wall_s"] == 70 * 60 - 60  # from run_started's first phase at minute 1 to the finish at 70
    assert minutes(phase(m, "plan")["buckets"]) == {"agent": 10, "human": 30, "runner": 2}
    assert minutes(phase(m, "build")["buckets"]) == {"agent": 20, "gates": 4, "runner": 3}
    assert phase(m, "plan")["cost_usd"] == 2.5 and phase(m, "build")["cost_usd"] == 4.0 and m["cost_usd"] == 6.5
    assert step(m, "build/build/task-1/implement")["approx"] is False


def test_each_phase_lists_the_model_and_effort_its_calls_ran_on():
    log = Log().add("run_started", run_id="r", pipeline="feature")
    log.add("phase_started", phase="build")
    log.call(0, 10, "build/build/task-1/implement", 3.0, session="a", model="claude-opus-5-5", effort="high")
    log.call(10, 15, "build/build/task-1/review", 1.0, session="b", model="claude-sonnet-5-5", alias="sonnet",
             effort="low", role="review")
    log.call(15, 20, "build/build/task-2/implement", 2.0, session="c", model="claude-opus-5-5", effort="high")
    log.at(21).add("run_finished")
    m = log.metrics()

    models = {(r["model"], r["effort"]): r for r in phase(m, "build")["models"]}
    assert set(models) == {("claude-opus-5-5", "high"), ("claude-sonnet-5-5", "low")}
    assert models[("claude-opus-5-5", "high")]["sessions"] == 2 and models[("claude-opus-5-5", "high")]["cost_usd"] == 5.0
    assert models[("claude-sonnet-5-5", "low")]["agent_s"] == 300
    assert m["by_model"]["claude-opus-5-5·high"]["cost_usd"] == 5.0
    (session,) = step(m, "build/build/task-1/review")["sessions"]
    assert (session["model"], session["model_alias"], session["effort"], session["role"]) == \
        ("claude-sonnet-5-5", "sonnet", "low", "review")


def test_a_call_with_no_session_event_keeps_the_alias():
    log = Log().add("phase_started", phase="plan")
    log.at(0).add("agent_started", agent="claude", pid=7, model="opus", effort="medium", step="plan/plan")
    log.at(5).add("agent_exited", session_id=None, exit_code=1, cost=0.0, key="plan/plan")
    log.add("run_finished")
    assert phase(log.metrics(), "plan")["models"][0]["model"] == "opus"


def test_an_effort_change_is_a_new_call_at_the_new_effort_and_keeps_its_reason():
    log = Log().add("phase_started", phase="build")
    log.call(0, 2, "build/build/task-1/implement", 0.2, session="a", effort="medium")
    log.add("effort_changed", key="build/build/task-1/implement", session_id="a", **{"from": "medium", "to": "high"},
            reason="touches auth")
    log.call(2, 12, "build/build/task-1/implement", 3.0, session="b", effort="high")
    log.add("run_finished")
    s = step(log.metrics(), "build/build/task-1/implement")
    assert {(r["effort"], r["cost_usd"]) for r in s["models"]} == {("medium", 0.2), ("high", 3.0)}
    assert s["effort_changes"] == [{"from": "medium", "to": "high", "reason": "touches auth", "at": s["effort_changes"][0]["at"]}]


def test_a_resumed_session_is_charged_once_and_the_fix_round_counts_as_rework():
    key = "verify/verify"
    log = Log().add("phase_started", phase="verify")
    log.add("step_started", key=key, phase="verify", mode="first")
    log.call(0, 5, key, 1.0, session="s")
    log.at(6).add("fix_attempt", key=key, attempt=1, gates=["py_unit"])
    log.call(6, 9, key, 1.6, session="s")  # Claude's cost spans every resume of the session: 0.6 is new
    log.at(9).add("step_finished", key=key, status="done")
    log.add("run_finished")
    m = log.metrics()
    s = step(m, key)
    assert phase(m, "verify")["cost_usd"] == pytest.approx(1.6)
    assert s["rework_cost_usd"] == pytest.approx(0.6) and s["rework_attempts"] == 1 and s["fixes"] == 1
    assert m["rework_cost_usd"] == pytest.approx(0.6)


def test_an_estimated_cost_prices_its_own_call():
    log = Log().add("phase_started", phase="plan")
    log.call(0, 5, "plan/plan", 1.0, session="s", estimated=True)
    log.call(5, 7, "plan/plan", 0.5, session="s", estimated=True)
    log.add("run_finished")
    m = log.metrics()
    assert m["cost_usd"] == 1.5 and m["cost_estimated"] is True


def test_holds_ci_and_a_pause_are_their_own_buckets():
    log = Log().add("phase_started", phase="build")
    log.call(0, 10, "build/build/task-1/implement", 1.0, session="a")
    log.at(10).add("run_held", key="build/build/task-1/implement", reason="usage limit", until="x")
    log.at(70).add("run_hold_ended", key="build/build/task-1/implement")
    log.at(71).add("run_paused", by="dashboard")
    log.at(100).add("run_resumed")
    log.at(101).add("phase_started", phase="ship")
    log.add("ci_watching", url="u", round=0)
    log.at(111).add("pr_marked_ready", url="u", key="ship/ci")
    log.add("run_finished")
    m = log.metrics()
    assert minutes(phase(m, "build")["buckets"]) == {"agent": 10, "hold": 60, "runner": 2, "down": 29}
    assert minutes(phase(m, "ship")["buckets"]) == {"ci": 10}


def test_a_silent_gap_longer_than_any_call_is_a_runner_that_was_down():
    log = Log().add("phase_started", phase="define")
    log.at(0).add("agent_started", agent="claude", pid=1, model="opus", effort="medium", step="define/idea")
    log.at(5).add("agent_turn", session_id=None, tokens=1, context_pct=1, cost=0.1)
    log.at(95).add("agent_started", agent="claude", pid=2, model="opus", effort="medium", step="define/idea")
    log.at(100).add("run_finished")
    buckets = minutes(log.metrics(stall_s=3600)["buckets"])
    assert buckets["down"] == 90 and buckets["agent"] == 10


def test_a_live_run_is_charged_up_to_now_and_its_open_call_adds_its_last_turn_cost():
    log = Log().add("phase_started", phase="build")
    log.at(0).add("agent_started", agent="claude", pid=3, model="sonnet", effort="low", step="build/build/task-1/implement")
    log.add("agent_session", session_id="live", model="claude-sonnet-5-5", key="build/build/task-1/implement", pid=3)
    log.at(4).add("agent_turn", session_id="live", tokens=9, context_pct=2, cost=0.75)
    m = log.metrics(now_minute=10)
    assert m["live"] is True and m["stalled"] is False and m["wall_s"] == 600
    assert m["cost_usd"] == 0.75 and m["cost_estimated"] is True
    assert step(m, "build/build/task-1/implement")["sessions"][0]["live"] is True


def test_a_run_quiet_for_longer_than_the_stall_limit_is_stalled():
    log = Log().add("phase_started", phase="build").at(1).add("step_started", key="build/setup", phase="build")
    m = log.metrics(now_minute=200, stall_s=3600)
    assert m["stalled"] is True and minutes(m["buckets"]) == {"runner": 1, "down": 199}


def test_a_waiting_run_keeps_counting_human_time_until_now():
    log = Log().add("phase_started", phase="plan")
    log.at(2).add("hitl_raised", kind="needs_human", key="plan/plan", phase="plan")
    m = log.metrics(now_minute=300)
    assert m["stalled"] is False and minutes(step(m, "plan/plan")["buckets"]) == {"human": 298}


def test_an_old_run_without_gate_durations_files_the_gap_as_gates_and_says_so():
    key = "verify/verify"
    log = Log().add("phase_started", phase="verify")
    log.call(0, 2, key, 0.1, session="s")
    log.at(2).add("skill_footer", key=key, phase="verify", next="x")
    log.at(7).add("gate_result", key=key, gate="py_unit", passed=True, skipped=False)
    log.add("gate_result", key=key, gate="py_lint", passed=True, skipped=False)
    log.add("run_finished")
    m = log.metrics()
    assert minutes(phase(m, "verify")["buckets"]) == {"agent": 2, "gates": 5}
    assert step(m, key)["approx"] is True and m["approx"] is True


def test_nothing_after_the_end_is_charged_but_a_late_exit_still_costs():
    log = Log().add("phase_started", phase="spec")
    log.at(0).add("agent_started", agent="claude", pid=4, model="opus", effort="medium", step="spec/draft")
    log.add("agent_session", session_id="x", model="claude-opus-5-5", key="spec/draft", pid=4)
    log.at(10).add("run_cancelled", by="me")
    log.at(11).add("agent_exited", session_id="x", exit_code=-9, cost=0.9, key="spec/draft")
    log.at(5000).add("run_archived", by="me")
    m = log.metrics(now_minute=9000)
    assert m["wall_s"] == 600 and m["cost_usd"] == 0.9 and m["live"] is False


def test_a_run_archived_without_finishing_ends_at_its_last_event():
    log = Log().add("phase_started", phase="spec").at(10).add("step_started", key="spec/draft", phase="spec")
    log.at(4000).add("run_archived", by="me")
    m = log.metrics(now_minute=9000)
    assert m["wall_s"] == 600 and m["live"] is False and m["ended_at"] == log.events[1]["ts"]


def test_aggregate_sums_runs_and_ranks_steps():
    def one(cost: float, minutes_: float) -> dict:
        log = Log().add("phase_started", phase="build")
        log.call(0, minutes_, "build/build/task-1/implement", cost, session=f"s{cost}", effort="high")
        log.add("run_finished")
        return log.metrics()

    rows = [{"run_id": f"r{i}", "lane": "feature", "module": "crm", "metrics": one(c, m)}
            for i, (c, m) in enumerate([(1.0, 10), (3.0, 30), (2.0, 20)])]
    out = aggregate(rows)
    assert out["runs"] == 3 and out["cost_usd"] == 6.0
    assert out["lanes"]["feature"]["cost_usd"] == {"total": 6.0, "median": 2.0, "p90": 3.0}
    assert out["lanes"]["feature"]["phases"]["build"]["wall_s"]["median"] == 1200
    assert out["costliest_steps"][0]["run_id"] == "r1" and out["slowest_steps"][-1]["run_id"] == "r0"
    assert out["models"] == [{"phase": "build", "model": "claude-opus-5-5·high", "sessions": 3, "agent_s": 3600.0,
                              "cost_usd": 6.0}]
    assert out["bucket_pct"]["agent"] == 100.0
