"""The agent step lifecycle (Task 6): effort, guards, result, gates and fix loop, HITL, rollover, commit."""

from __future__ import annotations

import io
import os
import subprocess
import sys
import threading
import time

import pytest

from agentflow import gitops, lanes
from agentflow.config import Phase, Step
from agentflow.conditions import make_ctx
from agentflow.events import parse_iso
from agentflow.hitl import answer
from agentflow.lanes import build_prompt
from agentflow.state import Run

KEY = "build/impl"
DONE = {"status": "done", "summary": "did it"}
TRAILER = "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"


def pairs(argv: list[str]) -> set[tuple[str, str]]:
    return set(zip(argv, argv[1:]))


def make_step(**kw) -> Step:
    fields = {"id": "impl", "type": "agent", "skill": "test-driven-development", "role": "build", "prompt": "step.md"}
    return Step(**(fields | kw))


def make_phase(step: Step, **kw) -> Phase:
    fields = {"name": "build", "agent": None, "model": "sonnet", "skip_if": None, "steps": [step]}
    return Phase(**(fields | kw))


def run_step(flow_repo, step: Step | None = None, executor=None, **kw) -> str:
    step = step or make_step()
    executor = executor or flow_repo.executor()
    return executor.run_agent_step(make_phase(step), step, KEY, **kw)


def session_of(flow_repo, run, n: int = 0) -> str:
    return flow_repo.events(run, "agent_session")[n]["session_id"]


def hitl_kinds(flow_repo, run) -> list[str]:
    return [e["kind"] for e in flow_repo.events(run, "hitl_raised")]


# --------------------------------------------------------------------------- effort


def test_first_call_medium_with_sizing(flow_repo):
    run = flow_repo.new_run()
    assert run_step(flow_repo) == "done"
    c = flow_repo.calls()[0]
    assert ("--effort", "medium") in pairs(c["argv"])
    assert ("--model", "sonnet") in pairs(c["argv"])
    assert "Effort check" in c["argv"][-1]
    assert c["env"]["AGENTFLOW_STEP"] == KEY
    assert c["env"]["AGENTFLOW_RESULT"] == str(run.agent_result_path(KEY))  # in the workspace, not runs/
    assert f"`{run.agent_result_path(KEY)}`" in c["argv"][-1]  # the prompt names it: agents cannot read their env
    assert run.state["steps"][KEY]["status"] == "done"
    assert run.state["steps"][KEY]["effort"] == "medium"
    started = flow_repo.events(run, "agent_started")[0]
    assert started["sizing"] is True


def test_effort_change_restarts_fresh(flow_repo):
    run = flow_repo.new_run()
    flow_repo.script({KEY: [{"result": {"status": "effort_change", "effort": "high", "reason": "6 files"}},
                            {"result": DONE}]})
    assert run_step(flow_repo) == "done"
    c = flow_repo.calls()
    assert len(c) == 2
    assert "--resume" not in c[1]["argv"]
    assert ("--effort", "high") in pairs(c[1]["argv"])
    assert "Effort check" not in c[1]["argv"][-1]
    changed = flow_repo.events(run, "effort_changed")[0]
    assert {"from": "medium", "to": "high", "reason": "6 files", "key": KEY}.items() <= changed.items()
    assert run.state["steps"][KEY]["effort"] == "high"
    assert run.state["steps"][KEY]["effort_changed"] == "up"


def test_effort_change_after_work_refused(flow_repo):
    run = flow_repo.new_run()
    flow_repo.script({KEY: [{"write": {"src/a.py": "x = 1\n"},
                             "result": {"status": "effort_change", "effort": "high", "reason": "big"}},
                            {"result": DONE}]})
    assert run_step(flow_repo) == "done"
    refused = flow_repo.events(run, "effort_refused")
    assert len(refused) == 1 and refused[0]["requested"] == "high"
    assert flow_repo.events(run, "effort_changed") == []
    c = flow_repo.calls()
    assert len(c) == 2
    assert ("--resume", session_of(flow_repo, run)) in pairs(c[1]["argv"])
    assert ("--effort", "medium") in pairs(c[1]["argv"])
    assert "refused" in c[1]["argv"][-1]


def test_effort_outside_range_refused(flow_repo):
    run = flow_repo.new_run()
    flow_repo.script({KEY: [{"result": {"status": "effort_change", "effort": "xhigh", "reason": "huge"}},
                            {"result": DONE}]})
    assert run_step(flow_repo) == "done"
    refused = flow_repo.events(run, "effort_refused")
    assert len(refused) == 1 and refused[0]["requested"] == "xhigh"
    c = flow_repo.calls()
    assert ("--resume", session_of(flow_repo, run)) in pairs(c[1]["argv"])
    assert ("--effort", "medium") in pairs(c[1]["argv"])
    assert run.state["steps"][KEY]["effort"] == "medium"


# --------------------------------------------------------------------------- gates and the fix loop


def test_fix_loop_resumes_and_stops(flow_repo):
    run = flow_repo.new_run()
    step = make_step(gates=["fail"], fix_loop=True)
    assert run_step(flow_repo, step) == "stopped"
    c = flow_repo.calls()
    assert len(c) == 4  # the first call plus defaults.fix_attempts (3)
    sid = session_of(flow_repo, run)
    for call in c[1:]:
        assert ("--resume", sid) in pairs(call["argv"])
        assert "boom {module} line" in call["argv"][-1]  # gate output goes in verbatim
    assert hitl_kinds(flow_repo, run) == ["fix_attempts_exhausted"]
    assert run.state["waiting"]["kind"] == "fix_attempts_exhausted"
    assert not [e for e in flow_repo.events(run, "step_finished") if e.get("status") == "done"]


def test_fix_loop_keeps_high_effort(flow_repo):
    run = flow_repo.new_run()
    flow_repo.script({KEY: [{"result": {"status": "effort_change", "effort": "high", "reason": "hard"}}]})
    step = make_step(gates=["fail"], fix_loop=True)
    assert run_step(flow_repo, step) == "stopped"
    c = flow_repo.calls()
    assert len(c) == 5  # sizing call, fresh call at high, three fix attempts
    assert ("--effort", "medium") in pairs(c[0]["argv"])
    for call in c[1:]:
        assert ("--effort", "high") in pairs(call["argv"])
    assert hitl_kinds(flow_repo, run) == ["fix_attempts_exhausted"]


def test_fix_loop_passes_after_a_fix(flow_repo):
    flow_repo.configure({"gates": {"flaky": {"run": "test -f fixed.txt"}}})
    run = flow_repo.new_run()
    flow_repo.script({KEY: [{"result": DONE}, {"write": {"fixed.txt": "ok\n"}, "result": DONE}]})
    assert run_step(flow_repo, make_step(gates=["flaky"], fix_loop=True)) == "done"
    assert len(flow_repo.calls()) == 2
    results = [(e["gate"], e["passed"]) for e in flow_repo.events(run, "gate_result")]
    assert results == [("flaky", False), ("flaky", True)]


def run_gates_first(flow_repo, gates: list[str]) -> str:
    step = make_step(gates=gates, fix_loop=True, gates_first=True)
    return flow_repo.executor().run_steps(make_phase(step), [step], "build")[0]


def gate_runs(flow_repo, run) -> list[tuple[str, bool]]:
    return [(e["gate"], e["passed"]) for e in flow_repo.events(run, "gate_result")]


def test_gates_first_hands_the_session_the_gate_output_and_does_not_run_them_again(flow_repo):
    run = flow_repo.new_run()
    assert run_gates_first(flow_repo, ["ok"]) == "done"
    prompt = flow_repo.calls()[0]["argv"][-1]
    assert "The runner ran these gates on tree" in prompt
    assert "- `ok`: passed" in prompt and "fine" in prompt  # the result line and the log tail
    assert gate_runs(flow_repo, run) == [("ok", True)]  # once, before the session
    assert [e["passed"] for e in flow_repo.events(run, "gates_prerun")] == [True]
    assert len(flow_repo.events(run, "gates_reused")) == 1


def test_gates_first_runs_the_gates_again_when_the_session_changed_the_tree(flow_repo):
    run = flow_repo.new_run()
    flow_repo.script({KEY: [{"write": {"fix.txt": "x\n"}, "result": DONE}]})
    assert run_gates_first(flow_repo, ["ok"]) == "done"
    assert gate_runs(flow_repo, run) == [("ok", True), ("ok", True)]
    assert flow_repo.events(run, "gates_reused") == []


def test_a_failed_pre_run_starts_the_session_in_its_fix_loop(flow_repo):
    flow_repo.configure({"gates": {"flaky": {"run": "test -f fixed.txt || { echo 'not fixed'; exit 1; }"}}})
    run = flow_repo.new_run()
    flow_repo.script({KEY: [{"write": {"fixed.txt": "ok\n"}, "result": DONE}]})
    assert run_gates_first(flow_repo, ["flaky"]) == "done"
    calls = flow_repo.calls()
    assert len(calls) == 1  # the first call already is fix attempt 1
    assert "not fixed" in calls[0]["argv"][-1]
    assert [e["attempt"] for e in flow_repo.events(run, "fix_attempt")] == [1]
    assert gate_runs(flow_repo, run) == [("flaky", False), ("flaky", True)]


def test_a_step_without_gates_first_gets_no_evidence_and_runs_its_gates_after(flow_repo):
    run = flow_repo.new_run()
    step = make_step(gates=["ok"], fix_loop=True)
    assert flow_repo.executor().run_steps(make_phase(step), [step], "build")[0] == "done"
    assert "The runner ran these gates" not in flow_repo.calls()[0]["argv"][-1]
    assert flow_repo.events(run, "gates_prerun") == []
    assert gate_runs(flow_repo, run) == [("ok", True)]


def test_gate_failure_without_fix_loop_escalates(flow_repo):
    run = flow_repo.new_run()
    assert run_step(flow_repo, make_step(gates=["fail"])) == "stopped"
    assert len(flow_repo.calls()) == 1
    assert hitl_kinds(flow_repo, run) == ["fix_attempts_exhausted"]


def test_skipped_gate_reported(flow_repo):
    run = flow_repo.new_run()
    assert run_step(flow_repo, make_step(gates=["ok", "absent"])) == "done"
    by_gate = {e["gate"]: e for e in flow_repo.events(run, "gate_result")}
    assert by_gate["absent"]["skipped"] is True and by_gate["absent"]["passed"] is False
    assert by_gate["ok"]["passed"] is True and by_gate["ok"]["skipped"] is False
    assert run.state["steps"][KEY]["status"] == "done"


# --------------------------------------------------------------------------- guards


def test_forbidden_path_fails_no_retry(flow_repo):
    run = flow_repo.new_run()
    flow_repo.script({KEY: [{"write": {"CONSTRAINTS.md": "weaker\n"}, "result": DONE}]})
    assert run_step(flow_repo, make_step(gates=["fail"], fix_loop=True)) == "stopped"
    assert len(flow_repo.calls()) == 1
    assert run.state["steps"][KEY]["status"] == "failed"
    raised = flow_repo.events(run, "hitl_raised")
    assert [e["kind"] for e in raised] == ["guard_violation"]
    assert "CONSTRAINTS.md" in raised[0]["summary"]
    assert flow_repo.events(run, "gate_result") == []


def test_agent_commit_fails(flow_repo):
    run = flow_repo.new_run()
    before = flow_repo.head()
    flow_repo.script({KEY: [{"write": {"notes.txt": "n\n"}, "git_commit": "x", "result": DONE}]})
    assert run_step(flow_repo) == "stopped"
    assert flow_repo.head() != before  # the agent's commit is left for the human to inspect
    assert run.state["steps"][KEY]["status"] == "failed"
    raised = flow_repo.events(run, "hitl_raised")
    assert [e["kind"] for e in raised] == ["guard_violation"]
    assert "agent_commit" in raised[0]["summary"]


def test_permission_denials_logged(flow_repo):
    run = flow_repo.new_run()
    denial = {"tool_name": "Bash", "tool_use_id": "toolu_1", "tool_input": {"command": "git push"}}
    flow_repo.script({KEY: [{"permission_denials": [denial, denial], "result": DONE}]})
    assert run_step(flow_repo) == "done"
    denied = flow_repo.events(run, "permission_denied")
    assert len(denied) == 2
    assert denied[0]["tool"] == "Bash" and denied[0]["input"] == {"command": "git push"} and denied[0]["key"] == KEY
    assert run.state["steps"][KEY]["permission_denials"] == 2


# --------------------------------------------------------------------------- result.json


def test_missing_result_reminded_then_hitl(flow_repo):
    run = flow_repo.new_run()
    flow_repo.script({KEY: [{}, {}]})
    assert run_step(flow_repo) == "stopped"
    c = flow_repo.calls()
    assert len(c) == 2
    assert ("--resume", session_of(flow_repo, run)) in pairs(c[1]["argv"])
    assert ("--effort", "medium") in pairs(c[1]["argv"])
    assert "result.json" in c[1]["argv"][-1]
    assert f"`{run.agent_result_path(KEY)}`" in c[1]["argv"][-1]
    assert hitl_kinds(flow_repo, run) == ["blocked"]
    assert run.state["steps"][KEY]["status"] == "waiting_human"
    assert not [e for e in flow_repo.events(run, "step_finished") if e.get("status") == "done"]


@pytest.mark.parametrize("raw", ["{not json", '{"status": "finished", "summary": "x"}', '["done"]'])
def test_invalid_result_json_is_not_done(flow_repo, raw):
    run = flow_repo.new_run()
    flow_repo.script({KEY: [{"result_raw": raw}, {"result_raw": raw}]})
    assert run_step(flow_repo, make_step(commit=True, message="Never")) == "stopped"
    assert len(flow_repo.calls()) == 2
    assert not [e for e in flow_repo.events(run, "step_finished") if e.get("status") == "done"]
    assert run.state["steps"][KEY]["status"] != "done"
    assert hitl_kinds(flow_repo, run) == ["blocked"]


def test_stale_result_from_an_earlier_call_is_not_reused(flow_repo):
    run = flow_repo.new_run()
    run.result_path(KEY).parent.mkdir(parents=True, exist_ok=True)
    run.result_path(KEY).write_text('{"status": "done", "summary": "old"}', encoding="utf-8")
    flow_repo.script({KEY: [{}, {}]})
    assert run_step(flow_repo) == "stopped"
    assert hitl_kinds(flow_repo, run) == ["blocked"]


def test_blocked_result_raises_hitl(flow_repo):
    run = flow_repo.new_run()
    flow_repo.script({KEY: [{"result": {"status": "blocked", "summary": "the spec is missing"}}]})
    assert run_step(flow_repo) == "stopped"
    raised = flow_repo.events(run, "hitl_raised")
    assert [e["kind"] for e in raised] == ["blocked"]
    assert raised[0]["summary"] == "the spec is missing"


def test_needs_human_then_answer_resumes_same_session(flow_repo):
    run = flow_repo.new_run()
    question = {"id": "q1", "question": "Cache the token across conversations?",
                "options": [{"key": "A", "label": "Per process"}, {"key": "B", "label": "Per conversation"}]}
    flow_repo.script({KEY: [{"result": {"status": "needs_human", "summary": "need a call", "questions": [question]}},
                            {"result": DONE}]})
    assert run_step(flow_repo) == "stopped"
    assert hitl_kinds(flow_repo, run) == ["needs_human"]
    assert run.inbox_path(KEY).is_file()
    assert run.state["status"] == "waiting_human"

    assert run_step(flow_repo) == "stopped"  # still waiting: no call made
    assert len(flow_repo.calls()) == 1

    assert answer(run, KEY, {"q1": "A"}, by="test") is True
    assert run_step(flow_repo, executor=flow_repo.executor(run)) == "done"
    c = flow_repo.calls()
    assert len(c) == 2
    sid = session_of(flow_repo, run)
    assert ("--resume", sid) in pairs(c[1]["argv"])
    assert ("--effort", run.state["steps"][KEY]["effort"]) in pairs(c[1]["argv"])
    assert ("--effort", "medium") in pairs(c[1]["argv"])
    assert "q1: A" in c[1]["argv"][-1]
    assert "Effort check" not in c[1]["argv"][-1]
    assert run.state["steps"][KEY]["status"] == "done"


# --------------------------------------------------------------------------- rollover, budget, timeout


def test_rollover_fresh_session_with_progress(flow_repo):
    run = flow_repo.new_run()
    flow_repo.script({KEY: [{"usage_pct": [81]}, {"result": DONE}]})
    assert run_step(flow_repo) == "done"
    c = flow_repo.calls()
    assert len(c) == 2
    assert "--resume" not in c[1]["argv"]
    prompt = c[1]["argv"][-1]
    assert str(run.progress_path()) in prompt and "progress.md" in prompt
    assert str(run.agent_dir / "handoff.md") in prompt and "handoff.md" in prompt  # the workspace copy
    assert "Effort check" not in prompt
    assert ("--effort", "medium") in pairs(c[1]["argv"])
    assert len(flow_repo.events(run, "rolled_over")) == 1
    assert run.state["steps"][KEY]["rollovers"] == 1


def test_rollover_past_max_raises_hitl(flow_repo):
    run = flow_repo.new_run()
    flow_repo.script({KEY: [{"usage_pct": [81]}, {"usage_pct": [85]}, {"usage_pct": [90]}]})
    assert run_step(flow_repo) == "stopped"
    assert len(flow_repo.calls()) == 3  # max_rollovers is 2: the third rollover stops the run
    assert len(flow_repo.events(run, "rolled_over")) == 3
    assert hitl_kinds(flow_repo, run) == ["max_rollovers"]


def test_rollover_with_a_done_result_is_done(flow_repo):
    run = flow_repo.new_run()
    # The fake writes `result` after its usage turns (too late), so write the file first to model
    # the over-threshold turn being followed directly by the result event.
    early = str(run.agent_result_path(KEY))  # where the agent writes it: its workspace, in the state dir
    flow_repo.script({KEY: [{"write": {early: '{"status": "done", "summary": "early"}'}, "usage_pct": [81]}]})
    assert run_step(flow_repo) == "done"
    assert len(flow_repo.calls()) == 1
    assert flow_repo.events(run, "agent_exited")[0]["exit_code"] != 0  # the adapter did end the session
    assert flow_repo.events(run, "rolled_over") == []


def test_budget_exceeded_stops(flow_repo):
    flow_repo.configure({"defaults": {"run_budget_usd": 0.015}})
    run = flow_repo.new_run()
    assert run_step(flow_repo, make_step(gates=["fail"], fix_loop=True)) == "stopped"
    assert len(flow_repo.calls()) == 2
    assert hitl_kinds(flow_repo, run) == ["budget_exceeded"]
    assert run.state["spend_usd"] == pytest.approx(0.02)


def test_timeout_escalates(flow_repo, monkeypatch):
    run = flow_repo.new_run()
    monkeypatch.setattr(flow_repo.cfg.defaults, "step_timeout_min", 1 / 60)
    flow_repo.script({KEY: [{"sleep": 3, "result": DONE}]})
    started = time.monotonic()
    assert run_step(flow_repo) == "stopped"
    assert time.monotonic() - started < 3
    assert hitl_kinds(flow_repo, run) == ["step_timeout"]
    assert len(flow_repo.calls()) == 1


# --------------------------------------------------------------------------- limits and outages


OUTAGE = {"exit": 1}  # the process dies with no model reply
DIED = {"usage_pct": [10], "exit": 1}  # the model replied, then the process died


def rejected(resets_at: float, **act) -> dict:
    return {"rate_limit": {"status": "rejected", "resetsAt": resets_at, "rateLimitType": "five_hour"},
            "exit": 1, **act}


@pytest.fixture
def quick_holds(monkeypatch):
    monkeypatch.setattr(lanes, "HOLD_POLL_S", 0.01)
    monkeypatch.setattr(lanes, "HOLD_SLACK_S", 0.0)
    monkeypatch.setattr(lanes, "BACKOFF_START_S", 0.01)
    monkeypatch.setattr(lanes, "BACKOFF_MAX_S", 0.02)


def test_usage_limit_waits_for_the_reset_then_carries_on(flow_repo, quick_holds):
    run = flow_repo.new_run()
    reset = time.time() + 1
    flow_repo.script({KEY: [rejected(reset, usage_pct=[10]), {"result": DONE}]})
    assert run_step(flow_repo) == "done"
    assert time.time() >= reset
    first, second = flow_repo.calls()
    assert ("--resume", session_of(flow_repo, run)) in pairs(second["argv"])
    assert "Carry on where you left off" in second["argv"][-1]
    assert hitl_kinds(flow_repo, run) == [] and flow_repo.events(run, "result_reminded") == []
    (held,) = flow_repo.events(run, "run_held")
    assert held["reason"] == "usage limit" and held["limit"]["rateLimitType"] == "five_hour"
    assert run.state["hold"] is None


def test_a_call_refused_before_any_reply_is_sent_again_as_it_was(flow_repo, quick_holds):
    flow_repo.new_run()
    flow_repo.script({KEY: [rejected(time.time()), {"result": DONE}]})
    assert run_step(flow_repo) == "done"
    first, second = flow_repo.calls()
    assert "--resume" not in second["argv"] and second["argv"][-1] == first["argv"][-1]


def test_a_limit_that_outlasts_its_reset_holds_again(flow_repo, quick_holds):
    run = flow_repo.new_run()
    flow_repo.script({KEY: [rejected(time.time()), rejected(time.time()), {"result": DONE}]})
    assert run_step(flow_repo) == "done"
    assert len(flow_repo.events(run, "run_held")) == 2 and hitl_kinds(flow_repo, run) == []


def test_an_outage_is_retried_until_the_agent_is_back(flow_repo, quick_holds):
    run = flow_repo.new_run()
    flow_repo.script({KEY: [OUTAGE, OUTAGE, OUTAGE, OUTAGE, {"result": DONE}]})
    assert run_step(flow_repo) == "done"  # more calls than `failed_attempts`: a failed probe is free
    assert len(flow_repo.calls()) == 5 and hitl_kinds(flow_repo, run) == []
    assert [e["reason"] for e in flow_repo.events(run, "run_held")] == ["outage"] * 4


def test_an_outage_past_give_up_h_stops_for_a_human(flow_repo, quick_holds):
    flow_repo.configure({"recovery": {"give_up_h": 0.1 / 3600}})
    run = flow_repo.new_run()
    flow_repo.script({KEY: [OUTAGE] * 100})
    assert run_step(flow_repo) == "stopped"
    assert hitl_kinds(flow_repo, run) == ["agent_unavailable"]
    assert run.state["steps"][KEY]["status"] == "waiting_human" and run.state["hold"] is None


def test_calls_that_reach_the_model_and_die_stop_at_failed_attempts(flow_repo, quick_holds):
    run = flow_repo.new_run()
    flow_repo.script({KEY: [DIED] * 10})
    assert run_step(flow_repo) == "stopped"
    assert len(flow_repo.calls()) == 3
    assert hitl_kinds(flow_repo, run) == ["agent_unavailable"]


def test_a_step_finished_before_the_agent_died_is_not_repeated(flow_repo, quick_holds):
    run = flow_repo.new_run()
    flow_repo.script({KEY: [{"result": DONE, "exit": 1}]})
    assert run_step(flow_repo) == "done"
    assert len(flow_repo.calls()) == 1 and flow_repo.events(run, "run_held") == []


def test_an_agent_error_result_is_retried_not_reminded(flow_repo, quick_holds):
    run = flow_repo.new_run()
    flow_repo.script({KEY: [{"result_error": True}, {"result": DONE}]})
    assert run_step(flow_repo) == "done"
    assert flow_repo.events(run, "result_reminded") == []


def test_a_hold_notifies_once(flow_repo, quick_holds, monkeypatch):
    sent = []
    monkeypatch.setattr(lanes, "notify_run", lambda cfg, run, message: sent.append(message))
    flow_repo.new_run()
    flow_repo.script({KEY: [OUTAGE, OUTAGE, OUTAGE, {"result": DONE}]})
    assert run_step(flow_repo) == "done"
    assert len(sent) == 1


@pytest.mark.parametrize("event, status", [("run_paused", "paused"), ("run_cancelled", "cancelled")])
def test_a_human_can_end_a_hold(flow_repo, quick_holds, event, status):
    run = flow_repo.new_run()
    flow_repo.script({KEY: [rejected(time.time() + 3600)]})
    timer = threading.Timer(0.3, lambda: Run.load(flow_repo.root, run.id).emit(event, by="me"))
    timer.start()
    started = time.monotonic()
    assert run_step(flow_repo) == "stopped"
    timer.join()
    assert time.monotonic() - started < 10
    assert len(flow_repo.calls()) == 1
    assert run.refresh()["status"] == status and run.state["hold"] is None


# --------------------------------------------------------------------------- resume


def _interrupted(flow_repo, run, effort: str = "high", pid: int = 999999) -> None:
    run.emit("step_started", key=KEY, phase="build", agent="claude", model="sonnet", effort=effort,
             start_head=flow_repo.head())
    run.emit("agent_started", agent="claude", pid=pid, model="sonnet", effort=effort, step=KEY, session_id="s1")


def _orphan(seconds: float) -> subprocess.Popen:
    """An agent that outlived its runner: its own session, and a command the runner recognises."""
    agent = subprocess.Popen([sys.executable, "-c", f"import time; time.sleep({seconds})", "fake_agent.py"],
                             start_new_session=True)
    threading.Thread(target=agent.wait, daemon=True).start()  # reaped as launchd would: no zombie to mistake
    return agent


def test_a_resume_waits_for_an_agent_that_outlived_its_runner(flow_repo):
    run = flow_repo.new_run()
    agent = _orphan(1.0)
    exited_at = {}
    threading.Thread(target=lambda: (agent.wait(), exited_at.update(t=time.time())), daemon=True).start()
    _interrupted(flow_repo, run, pid=agent.pid)
    assert run_step(flow_repo, executor=flow_repo.executor(run)) == "done"
    (awaited,) = flow_repo.events(run, "agent_awaited")
    assert awaited["pids"] == [agent.pid]
    resumed = flow_repo.events(run, "agent_started")[-1]
    assert resumed["seq"] > awaited["seq"] and len(flow_repo.calls()) == 1
    assert exited_at["t"] < parse_iso(resumed["ts"]).timestamp()


def test_a_resume_stops_an_agent_that_outlasts_the_step_timeout(flow_repo, monkeypatch):
    run = flow_repo.new_run()
    monkeypatch.setattr(flow_repo.cfg.defaults, "step_timeout_min", 0.5 / 60)
    agent = _orphan(60)
    _interrupted(flow_repo, run, pid=agent.pid)
    started = time.monotonic()
    assert run_step(flow_repo, executor=flow_repo.executor(run)) == "done"
    assert time.monotonic() - started < 30
    agent.wait(timeout=5)
    assert len(flow_repo.calls()) == 1


def test_a_resume_with_no_live_agent_does_not_wait(flow_repo):
    run = flow_repo.new_run()
    _interrupted(flow_repo, run)
    assert run_step(flow_repo, executor=flow_repo.executor(run)) == "done"
    assert flow_repo.events(run, "agent_awaited") == []


def test_resume_after_interrupt_uses_recorded_session(flow_repo):
    run = flow_repo.new_run()
    _interrupted(flow_repo, run)
    before = flow_repo.head()
    step = make_step(commit=True, message="Step {module}")
    assert run_step(flow_repo, step, executor=flow_repo.executor(run)) == "done"
    c = flow_repo.calls()
    assert len(c) == 1
    assert ("--resume", "s1") in pairs(c[0]["argv"])
    assert ("--effort", "high") in pairs(c[0]["argv"])
    assert "Effort check" not in c[0]["argv"][-1]
    assert f"`{run.agent_result_path(KEY)}`" in c[0]["argv"][-1]
    assert flow_repo.head() == before  # nothing new to commit, and no commit was made twice
    assert run.state["steps"][KEY]["status"] == "done"


def test_resume_after_commit_finishes_without_a_call(flow_repo):
    run = flow_repo.new_run()
    _interrupted(flow_repo, run)
    run.emit("step_committed", key=KEY, sha=flow_repo.head())
    before = flow_repo.head()
    (flow_repo.root / "later.txt").write_text("later\n", encoding="utf-8")
    assert run_step(flow_repo, make_step(commit=True, message="m"), executor=flow_repo.executor(run)) == "done"
    assert flow_repo.calls() == []
    assert flow_repo.head() == before
    assert run.state["steps"][KEY]["status"] == "done"


def test_done_step_is_not_run_again(flow_repo):
    run = flow_repo.new_run()
    assert run_step(flow_repo) == "done"
    assert run_step(flow_repo, executor=flow_repo.executor(run)) == "done"
    assert len(flow_repo.calls()) == 1


def test_refine_round_resumes_and_accepts_unchanged(flow_repo):
    run = flow_repo.new_run()
    assert run_step(flow_repo) == "done"
    sid = session_of(flow_repo, run)
    run.emit("phase_approval_raised", phase="build", summary="ok?")
    run.emit("phase_rejected", by="human", phase="build", round=1, note="Đổi tên biến cho rõ")
    flow_repo.script({KEY: [{"result": {"status": "unchanged", "summary": "note is about another step"}}]})
    assert run_step(flow_repo, executor=flow_repo.executor(run), mode="refine") == "done"
    c = flow_repo.calls()
    assert len(c) == 2
    assert ("--resume", sid) in pairs(c[1]["argv"])
    assert ("--effort", "medium") in pairs(c[1]["argv"])
    assert "Đổi tên biến cho rõ" in c[1]["argv"][-1]
    assert "refine round 1" in c[1]["argv"][-1]
    assert run.state["steps"][KEY]["status"] == "unchanged"


# --------------------------------------------------------------------------- prompts


def test_prompt_with_braces_renders(flow_repo):
    flow_repo.new_run()
    text = "def f(): return {'a': 1}\nand {module} stays literal"
    step = make_step(prompt="task.md")
    assert run_step(flow_repo, step, extra={"task_text": text, "task_title": "Task 3: F"}) == "done"
    prompt = flow_repo.calls()[0]["argv"][-1]
    assert text in prompt
    assert "implement Task 3: F from the plan" in prompt


def test_build_prompt_parts(flow_repo):
    run = flow_repo.new_run()
    cfg = flow_repo.cfg
    ctx = make_ctx(cfg, flow_repo.root, "feature", {"module": "crm"}, run)
    step = make_step(prompt="spec.md")
    extra = {"result_file": "/workspace/out/step.json"}
    first = build_prompt(cfg, step, ctx, {"sized": False, "agent": "claude"}, extra, "first")
    assert "Effort check" in first
    assert "Use the `spec-driven-development` skill." in first
    assert "`/workspace/out/step.json`" in first
    assert str(run.progress_path()) in first
    assert "stop and ask" in first
    assert "commit" in first and "push" in first
    assert "docs/specs/SPEC-crm.md" in first
    assert first.index("Effort check") < first.index("spec-driven-development") < first.index("/workspace/out/step.json")
    sized = build_prompt(cfg, step, ctx, {"sized": True, "agent": "claude"}, extra, "first")
    assert "Effort check" not in sized and "/workspace/out/step.json" in sized


def test_build_prompt_names_a_skill_the_template_leaves_out(flow_repo):
    run = flow_repo.new_run()
    ctx = make_ctx(flow_repo.cfg, flow_repo.root, "feature", {"module": "crm"}, run)
    prompt = build_prompt(flow_repo.cfg, make_step(skill="writing-plans"), ctx, {"sized": True}, {}, "first")
    assert "Use the `writing-plans` skill." in prompt
    assert "do the step `impl`" in prompt


def test_codex_prompt_reads_skill_file(flow_repo):
    run = flow_repo.new_run()
    ctx = make_ctx(flow_repo.cfg, flow_repo.root, "feature", {"module": "crm"}, run)
    prompt = build_prompt(flow_repo.cfg, make_step(prompt="plan.md"), ctx, {"sized": True, "agent": "codex"}, {}, "first")
    assert "Read `.claude/skills/writing-plans/SKILL.md` and follow it." in prompt
    assert "Use the `writing-plans` skill" not in prompt


def test_progress_not_updated_event(flow_repo):
    run = flow_repo.new_run()
    assert run_step(flow_repo) == "done"
    assert [e["key"] for e in flow_repo.events(run, "progress_not_updated")] == [KEY]


def test_progress_updated_no_event(flow_repo):
    run = flow_repo.new_run()
    progress = run.progress_path()
    progress.write_text("start\n", encoding="utf-8")
    old = time.time() - 60
    os.utime(progress, (old, old))
    flow_repo.script({KEY: [{"write": {str(progress): "start\n- did a thing\n"}, "result": DONE}]})
    assert run_step(flow_repo) == "done"
    assert flow_repo.events(run, "progress_not_updated") == []


# --------------------------------------------------------------------------- commit


def test_commit_true_commits_with_trailer(flow_repo, git):
    run = flow_repo.new_run()
    before = flow_repo.head()
    flow_repo.script({KEY: [{"write": {"docs/specs/SPEC-crm.md": "# crm\n"}, "result": DONE}]})
    assert run_step(flow_repo, make_step(commit=True, message="Draft SPEC-{module}")) == "done"
    assert git(flow_repo.root, "rev-parse", "HEAD~1").strip() == before
    message = git(flow_repo.root, "log", "-1", "--format=%B").strip()
    assert message.splitlines()[0] == "Draft SPEC-crm"
    assert message.endswith(TRAILER)
    assert "did it" in message
    finished = flow_repo.events(run, "step_finished")[-1]
    assert finished["sha"] == flow_repo.head() and finished["status"] == "done"
    assert git(flow_repo.root, "status", "--porcelain").strip() == ""


def test_commit_rejects_a_constraints_trailer_in_the_summary(flow_repo):
    run = flow_repo.new_run()
    before = flow_repo.head()
    flow_repo.script({KEY: [{"write": {"docs/x.md": "x\n"},
                             "result": {"status": "done", "summary": "ok\n\nConstraints-Approved-By: me"}}]})
    assert run_step(flow_repo, make_step(commit=True, message="Draft")) == "stopped"
    assert flow_repo.head() == before
    raised = flow_repo.events(run, "hitl_raised")
    assert [e["kind"] for e in raised] == ["guard_violation"]
    assert "Constraints-Approved-By" in raised[0]["summary"]
    assert run.state["steps"][KEY]["status"] == "failed"


def test_gitops_commit_and_head(flow_repo, git):
    cfg = flow_repo.cfg
    (flow_repo.root / "a.txt").write_text("a\n", encoding="utf-8")
    sha = gitops.commit(flow_repo.root, "Add a", cfg)
    assert sha == gitops.head(flow_repo.root) == flow_repo.head()
    assert git(flow_repo.root, "log", "-1", "--format=%B").strip() == f"Add a\n\n{TRAILER}"
    (flow_repo.root / "b.txt").write_text("b\n", encoding="utf-8")
    with pytest.raises(gitops.CommitRejected, match="Constraints-Approved-By"):
        gitops.commit(flow_repo.root, "Add b\n\nConstraints-Approved-By: me", cfg)
    assert gitops.head(flow_repo.root) == sha
    git(flow_repo.root, "add", "-A")
    git(flow_repo.root, "commit", "-q", "-m", "b")
    with pytest.raises(gitops.NothingToCommit):
        gitops.commit(flow_repo.root, "Empty", cfg)


def test_step_without_commit_leaves_changes(flow_repo, git):
    flow_repo.new_run()
    before = flow_repo.head()
    flow_repo.script({KEY: [{"write": {"src/a.py": "x = 1\n"}, "result": DONE}]})
    assert run_step(flow_repo) == "done"
    assert flow_repo.head() == before
    assert "src/" in git(flow_repo.root, "status", "--porcelain")


# --------------------------------------------------------------------------- dry run


def test_dry_run_prints_argv_only(flow_repo):
    run = flow_repo.new_run()
    seq = run.state["last_seq"]
    out = io.StringIO()
    executor = flow_repo.executor(run, dry_run=True, out=out)
    assert run_step(flow_repo, executor=executor) == "skipped"
    printed = out.getvalue()
    assert "fake_agent.py" in printed
    assert "--effort medium" in printed and "--model sonnet" in printed and "--permission-prompts none" in printed
    assert flow_repo.calls() == []
    assert run.events.last_seq() == seq
    assert not (run.dir / "results").exists() and not (run.dir / "transcripts").exists()
    assert not (run.dir / "gates").exists() and not (run.dir / "inbox").exists()


# --------------------------------------------------------------------------- forbidden paths at commit time


def test_guard_violation_cannot_be_committed_after_approve(flow_repo, git):
    from agentflow.hitl import approve

    run = flow_repo.new_run()
    before = flow_repo.head()
    step = make_step(commit=True, message="Step {module}")
    flow_repo.script({KEY: [{"write": {"CONSTRAINTS.md": "weaker\n"}, "result": DONE},
                            {"write": {"docs/ok.md": "ok\n"}, "result": DONE}]})
    assert run_step(flow_repo, step) == "stopped"
    approve(run, KEY, by="test")  # what the inbox text invites
    assert run.state["waiting"] is None and run.state["steps"][KEY]["status"] == "failed"

    assert run_step(flow_repo, step, executor=flow_repo.executor(run)) == "stopped"
    assert len(flow_repo.calls()) == 1  # the restart sees the uncommitted violation before calling an agent
    assert flow_repo.head() == before
    assert git(flow_repo.root, "log", "--format=%H", f"{before}..HEAD").strip() == ""
    assert git(flow_repo.root, "diff", "--cached", "--name-only").strip() == ""
    assert (flow_repo.root / "CONSTRAINTS.md").read_text(encoding="utf-8") == "weaker\n"  # left for the human
    assert hitl_kinds(flow_repo, run) == ["guard_violation", "guard_violation"]
    assert "CONSTRAINTS.md" in flow_repo.events(run, "hitl_raised")[-1]["summary"]
    assert run.state["steps"][KEY]["status"] == "failed"
    assert not [e for e in flow_repo.events(run, "step_finished") if e.get("status") == "done"]


def test_dirty_forbidden_path_is_never_committed(flow_repo, git):
    run = flow_repo.new_run()
    before = flow_repo.head()
    (flow_repo.root / "CONSTRAINTS.md").write_text("edited outside the step\n", encoding="utf-8")
    flow_repo.script({KEY: [{"write": {"docs/ok.md": "ok\n"}, "result": DONE}]})
    assert run_step(flow_repo, make_step(commit=True, message="Step")) == "stopped"
    assert flow_repo.head() == before
    assert git(flow_repo.root, "diff", "--cached", "--name-only").strip() == ""
    raised = flow_repo.events(run, "hitl_raised")
    assert [e["kind"] for e in raised] == ["guard_violation"]
    assert "forbidden_path" in raised[0]["summary"] and "CONSTRAINTS.md" in raised[0]["summary"]
    assert run.state["steps"][KEY]["status"] == "failed"


def test_gitops_commit_rejects_a_staged_forbidden_path(flow_repo, git):
    before = flow_repo.head()
    (flow_repo.root / "CONSTRAINTS.md").write_text("weaker\n", encoding="utf-8")
    (flow_repo.root / "fine.txt").write_text("fine\n", encoding="utf-8")
    with pytest.raises(gitops.CommitRejected, match="CONSTRAINTS.md") as caught:
        gitops.commit(flow_repo.root, "Sneak", flow_repo.cfg)
    assert caught.value.kind == "forbidden_path"
    assert gitops.head(flow_repo.root) == before
    assert git(flow_repo.root, "diff", "--cached", "--name-only").strip() == ""  # index clean
    assert (flow_repo.root / "CONSTRAINTS.md").read_text(encoding="utf-8") == "weaker\n"  # tree untouched
    assert (flow_repo.root / "fine.txt").is_file()
