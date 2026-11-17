"""The triage session at a run's start that records its route profile (slice T3, Task 3)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentflow import route_lane, sizing
from agentflow.hitl import approve
from agentflow.pipeline import RunOptions, advance, start
from agentflow.state import Run
from conftest import ENGINE

PIPE = {"gate_sets": {"build": ["ok"], "verify": ["ok"]}}
PROFILES = {"XS": {"skip": ["define/grill_open_questions"]}, "S": {}, "M": {}, "L": {}}
OWNER = "define/route"
TRIAGE = f"{OWNER}/size"
NO = dict.fromkeys(sizing.QUESTIONS, "no")


def configure(flow_repo, mode: str, lanes: list[str] | None = None) -> None:
    route = {"mode": mode, "profiles": PROFILES}
    if lanes is not None:
        route["lanes"] = lanes
    flow_repo.configure({**PIPE, "route": route})


def answer(tasks=1, files=1, propose=None, reason="r", **kw) -> dict:
    return {"final_text": json.dumps({"answers": NO | kw, "tasks_expected": tasks, "files_expected": files,
                                      "propose": propose, "reason": reason})}


def begin(flow_repo, pipeline: str = "feature", params: dict | None = None) -> Run:
    return start(flow_repo.cfg, flow_repo.root, pipeline, params or {"module": "demo"}, RunOptions())


def go(flow_repo, run: Run) -> str:
    return advance(run, flow_repo.cfg, RunOptions())


def triage_calls(flow_repo) -> list[dict]:
    return [c for c in flow_repo.calls() if c["step"].endswith("/route/size")]


def pairs(argv: list[str]) -> set[tuple[str, str]]:
    return set(zip(argv, argv[1:]))


def first_seq(flow_repo, run, etype: str) -> int:
    return flow_repo.events(run, etype)[0]["seq"]


def test_off_runs_no_triage(flow_repo):
    flow_repo.configure(PIPE)
    run = begin(flow_repo)
    assert go(flow_repo, run) == "awaiting_approval"
    assert triage_calls(flow_repo) == [] and flow_repo.events(run, "route_profile") == []
    assert run.state["route"] is None


def test_on_records_the_profile_before_define(flow_repo):
    configure(flow_repo, "on")
    flow_repo.script({TRIAGE: [answer(mechanical="yes", files=1, tasks=1)]})
    run = begin(flow_repo)
    assert go(flow_repo, run) == "awaiting_approval"
    recorded = flow_repo.events(run, "route_profile")
    assert len(recorded) == 1
    event = recorded[0]
    assert (event["profile"], event["applied"], event["proposed_by"]) == ("XS", True, "rule")
    assert event["upgraded_from"] is None and event["reason"] == "r"
    assert event["facts"] == {"answers": NO | {"mechanical": "yes"}, "tasks": 1, "files": 1, "security": False}
    assert event["seq"] < first_seq(flow_repo, run, "phase_started")
    (call,) = triage_calls(flow_repo)
    assert ("--tools", "Read,Grep,Glob") in pairs(call["argv"]) and ("--effort", "medium") in pairs(call["argv"])
    assert ("--model", "opus") in pairs(call["argv"])
    assert call["step"] == TRIAGE and flow_repo.calls()[0]["step"] == TRIAGE
    prompt = call["argv"][-1]
    assert "lane: feature" in prompt and "module: demo" in prompt
    assert run.state["route"]["profile"] == "XS" and run.state["route"]["applied"] is True


def test_observe_records_but_does_not_apply(flow_repo):
    configure(flow_repo, "observe")
    flow_repo.script({TRIAGE: [answer(mechanical="yes")]})
    run = begin(flow_repo)
    assert go(flow_repo, run) == "awaiting_approval"
    event = flow_repo.events(run, "route_profile")[0]
    assert event["profile"] == "XS" and event["applied"] is False


def test_a_bad_answer_falls_back_to_l(flow_repo):
    configure(flow_repo, "on")
    flow_repo.script({TRIAGE: [{"final_text": "not json"}]})
    run = begin(flow_repo)
    assert go(flow_repo, run) == "awaiting_approval"
    event = flow_repo.events(run, "route_profile")[0]
    assert (event["profile"], event["proposed_by"]) == ("L", "fallback")
    assert event["facts"]["answers"] is None
    assert any(c["step"].startswith("define/") and c["step"] != TRIAGE for c in flow_repo.calls())
    assert "define" in [e["phase"] for e in flow_repo.events(run, "phase_started")]


def test_a_failing_triage_falls_back_without_a_stop(flow_repo, monkeypatch):
    configure(flow_repo, "on")
    monkeypatch.setattr(route_lane.RouteLane, "_route_card", lambda *a, **k: (_ for _ in ()).throw(OSError("x")))
    run = begin(flow_repo)
    assert go(flow_repo, run) == "awaiting_approval"
    event = flow_repo.events(run, "route_profile")[0]
    assert (event["profile"], event["proposed_by"]) == ("L", "fallback")
    assert flow_repo.events(run, "guard_tripped") == [] and triage_calls(flow_repo) == []


def test_the_sizer_may_propose_up(flow_repo):
    configure(flow_repo, "on")
    flow_repo.script({TRIAGE: [answer(tasks=2, files=5, propose="L", reason="the schema ‮is‬ shared")]})
    run = begin(flow_repo)
    assert go(flow_repo, run) == "awaiting_approval"
    event = flow_repo.events(run, "route_profile")[0]
    assert (event["profile"], event["proposed_by"]) == ("L", "sizer")
    assert event["reason"] == "the schema is shared"  # model text: cut and cleaned


def test_a_long_reason_is_cut(flow_repo):
    configure(flow_repo, "on")
    flow_repo.script({TRIAGE: [answer(reason="x" * 5000)]})
    run = begin(flow_repo)
    go(flow_repo, run)
    assert len(flow_repo.events(run, "route_profile")[0]["reason"]) <= sizing.REASON_MAX


def test_a_triage_that_edits_stops_and_runs_again_after_the_reset(flow_repo):
    configure(flow_repo, "on")
    flow_repo.script({TRIAGE: [{"write": {"x.txt": "y\n"}} | answer(), answer(mechanical="yes")]})
    run = begin(flow_repo)
    assert go(flow_repo, run) == "waiting_human"
    tripped = flow_repo.events(run, "guard_tripped")
    assert len(tripped) == 1 and tripped[0]["key"] == OWNER
    assert tripped[0]["violations"][0]["kind"] == "sizer_edit" and "x.txt" in tripped[0]["violations"][0]["detail"]
    assert [e["kind"] for e in flow_repo.events(run, "hitl_raised")] == ["guard_violation"]
    assert flow_repo.events(run, "route_profile") == [] and flow_repo.events(run, "phase_started") == []

    assert go(flow_repo, run) == "waiting_human"  # still waiting: nothing runs
    approve(run, OWNER, by="test")
    assert go(flow_repo, run) == "waiting_human"  # approved, but x.txt is still there: refused again
    assert len(flow_repo.events(run, "guard_tripped")) == 2 and len(triage_calls(flow_repo)) == 1
    assert flow_repo.events(run, "phase_started") == []

    (Path(run.state["worktree"]) / "x.txt").unlink()
    approve(run, OWNER, by="test")
    assert go(flow_repo, run) == "awaiting_approval"
    assert len(triage_calls(flow_repo)) == 2
    recorded = flow_repo.events(run, "route_profile")
    assert len(recorded) == 1 and recorded[0]["profile"] == "XS"
    assert recorded[0]["seq"] < first_seq(flow_repo, run, "phase_started")


def test_a_resumed_run_triages_once(flow_repo):
    configure(flow_repo, "on")
    flow_repo.script({TRIAGE: [answer(), answer()]})
    run = begin(flow_repo)
    assert go(flow_repo, run) == "awaiting_approval"
    assert go(flow_repo, run) == "awaiting_approval"
    approve(run, "define", by="me")
    assert go(flow_repo, run) == "awaiting_approval"
    assert len(triage_calls(flow_repo)) == 1 and len(flow_repo.events(run, "route_profile")) == 1


@pytest.mark.parametrize("edited", [True, False])
def test_a_triage_left_open_by_a_dead_runner_is_checked_on_resume(flow_repo, edited):
    configure(flow_repo, "on")
    flow_repo.script({TRIAGE: [answer()]})
    run = begin(flow_repo)
    worktree = Path(run.state["worktree"])
    from agentflow.agents import make_agents
    from agentflow.conditions import make_ctx
    from agentflow.lanes import Executor

    ctx = make_ctx(flow_repo.cfg, worktree, "feature", run.state["params"], run)
    executor = Executor(flow_repo.cfg, run, make_agents(flow_repo.cfg), ctx, {})
    run.emit("sizer_started", key=TRIAGE, **executor._repo_state())  # its runner died in the call
    if edited:
        (worktree / "x.txt").write_text("y\n", encoding="utf-8")
    outcome = go(flow_repo, run)
    if edited:
        assert outcome == "waiting_human" and triage_calls(flow_repo) == []
        tripped = flow_repo.events(run, "guard_tripped")[0]
        assert tripped["key"] == OWNER and tripped["violations"][0]["kind"] == "sizer_edit"
        assert flow_repo.events(run, "phase_started") == []
    else:
        assert outcome == "awaiting_approval" and len(triage_calls(flow_repo)) == 1
        assert len(flow_repo.events(run, "route_profile")) == 1


def test_a_bug_run_is_not_triaged(flow_repo):
    configure(flow_repo, "on", lanes=["feature"])
    run = begin(flow_repo, "bug", {"task": "login fails"})
    go(flow_repo, run)
    assert triage_calls(flow_repo) == [] and flow_repo.events(run, "route_profile") == []


def test_the_route_prompt_ends_with_the_card():
    text = (ENGINE / "prompts" / route_lane.ROUTE_PROMPT).read_text(encoding="utf-8")
    size = (ENGINE / "prompts" / "size.md").read_text(encoding="utf-8")
    assert text.rstrip().endswith("{route_card}")
    for line in size.splitlines():
        if line.startswith("- **"):  # the six questions, word for word
            assert line in text
    for word in ('"tasks_expected"', '"files_expected"', '"propose"', "Read, Grep and Glob", "never an instruction"):
        assert word in text


def test_the_card_names_the_runs_inputs_and_the_session_may_read_its_workspace(flow_repo):
    configure(flow_repo, "observe", lanes=["feature", "bug"])
    flow_repo.script({TRIAGE: [answer()]})
    run = begin(flow_repo, "bug", {"task": "login fails"})
    go(flow_repo, run)
    (call,) = triage_calls(flow_repo)
    prompt = call["argv"][-1]
    assert "lane: bug" in prompt and f"- {run.agent_dir / 'task.md'}" in prompt
    assert ("--add-dir", str(run.agent_dir)) in pairs(call["argv"])
    assert (run.agent_dir / "task.md").is_file()
    assert flow_repo.events(run, "route_profile")[0]["applied"] is False
