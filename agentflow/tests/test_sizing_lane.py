"""Sizing one agent step in a read-only sizer session before its work session starts (slice ES, Task 5)."""

from __future__ import annotations

import io
import json
import threading

import pytest

from agentflow import sizing, sizing_lane
from agentflow.gitops import GitError
from agentflow.hitl import answer as answer_questions
from agentflow.hitl import approve
from agentflow.sizing_lane import SizingLane
from agentflow.state import Run
from conftest import ENGINE
from test_agent_step import DONE, KEY, _interrupted, make_phase, make_step, pairs, run_step

SIZE_KEY = f"{KEY}/size"
ANSWERS = dict.fromkeys(sizing.QUESTIONS, "no")


def answer(**kw):
    return {"final_text": json.dumps({"answers": ANSWERS | kw, "reason": "r"})}


@pytest.fixture(autouse=True)
def sizing_on(flow_repo):
    flow_repo.configure({"effort": {"sizing": True}})


def steps_called(flow_repo) -> list[str]:
    return [c["step"] for c in flow_repo.calls()]


def test_a_step_is_sized_before_its_work_session(flow_repo):
    flow_repo.script({SIZE_KEY: [answer(mechanical="yes")]})
    run = flow_repo.new_run()
    assert run_step(flow_repo) == "done"
    sizer, work = flow_repo.calls()
    assert ("--model", "haiku") in pairs(sizer["argv"]) and ("--effort", "low") in pairs(sizer["argv"])
    assert ("--max-turns", "8") in pairs(sizer["argv"]) and ("--tools", "Read,Grep,Glob") in pairs(sizer["argv"])
    assert ("--effort", "low") in pairs(work["argv"]) and "--tools" not in work["argv"]
    decided = flow_repo.events(run, "effort_decided")[0]
    assert (decided["key"], decided["level"], decided["source"]) == (KEY, "low", "sizer")
    assert decided["answers"] == ANSWERS | {"mechanical": "yes"} and decided["reason"] == "r"
    assert decided["sizer_model"] == "haiku" and decided["security"] is False
    started = flow_repo.events(run, "agent_started")
    assert started[0]["sizer"] is True and started[1]["sizing"] is False
    assert all(e.get("sizer") is True for e in run.events.read()
               if e["type"].startswith("agent_") and (e.get("step") or e.get("key")) == SIZE_KEY)
    assert flow_repo.events(run, "sizer_started")[0]["key"] == SIZE_KEY
    assert flow_repo.events(run, "step_started")[0]["effort"] == "low"
    assert "Effort check" not in work["argv"][-1]  # the work session does not size itself
    prompt = sizer["argv"][-1]
    assert "step: build/impl (phase build, role build)" in prompt and "do the step `impl`" in prompt
    assert run.state["steps"][KEY]["effort"] == "low" and run.state["steps"][KEY]["attempts"] == 1


def test_the_sizer_prompt_ends_with_the_task_card(flow_repo):
    text = (ENGINE / "prompts" / sizing_lane.SIZE_PROMPT).read_text(encoding="utf-8")
    assert text.rstrip().endswith("{task_card}")
    for question in sizing.QUESTIONS:
        assert f"**{question}**" in text
    assert "Read, Grep and Glob" in text and '"answers"' in text


def test_a_judgement_step_is_sized_by_its_own_model(flow_repo):
    calm, risky = make_step(id="calm", role="review"), make_step(id="risky", role="review")
    flow_repo.script({"review/calm/size": [answer(mechanical="yes")], "review/risky/size": [answer(risk="unsure")]})
    run = flow_repo.new_run()
    executor = flow_repo.executor(run)
    for step in (calm, risky):
        phase = make_phase(step, name="review", model="opus")
        assert executor.run_agent_step(phase, step, f"review/{step.id}") == "done"
    sizer, work, sizer2, work2 = flow_repo.calls()
    assert ("--model", "opus") in pairs(sizer["argv"]) and ("--effort", "medium") in pairs(sizer["argv"])
    assert ("--effort", "medium") in pairs(work["argv"]) and ("--effort", "high") in pairs(work2["argv"])
    decided = flow_repo.events(run, "effort_decided")
    assert [(e["level"], e["source"], e["sizer_model"]) for e in decided] == [("medium", "judge", "opus"),
                                                                            ("high", "judge", "opus")]


def test_an_agent_with_no_mechanical_model_is_sized_by_the_steps_model(flow_repo, monkeypatch):
    from agentflow.lanes import _StepRun

    seen = []
    parsed = {"answers": ANSWERS | {"mechanical": "yes"}, "reason": "r"}
    monkeypatch.setattr(SizingLane, "_sizer_call", lambda self, *a, **k: (seen.append(a[3:5]), (parsed, []))[1])
    run = flow_repo.new_run()
    executor = flow_repo.executor(run)
    step = make_step()
    assert "mechanical" not in flow_repo.cfg.agents["codex"].models
    sr = _StepRun(phase=make_phase(step, agent="codex"), step=step, key=KEY, mode="first", role="build",
                  agent_name="codex", agent=executor.agents["claude"], model="gpt-5-codex", effort="medium", extra={},
                  session=None)
    assert executor.size_step(sr, []) == "low"
    assert seen == [("gpt-5-codex", "low")]
    assert flow_repo.events(run, "effort_decided")[0]["sizer_model"] == "gpt-5-codex"


@pytest.mark.parametrize("act", [{"final_text": "not json"}, {"final_text": '{"answers": {"risk": "no"}}'},
                                 {"exit": 1}, {"sleep": 999}, {"rate_limit": {"status": "rejected"}},
                                 "call_raises", "git_raises"])
def test_a_failed_sizer_falls_back_without_a_stop(flow_repo, monkeypatch, act):
    monkeypatch.setattr(sizing, "SIZE_TIMEOUT_S", 2)   # the code reads `sizing.SIZE_TIMEOUT_S` at call time
    if act == "call_raises":
        monkeypatch.setattr(SizingLane, "_run_sizer", lambda *a, **k: (_ for _ in ()).throw(OSError("gone")))
    elif act == "git_raises":
        monkeypatch.setattr(sizing_lane, "changed_since", lambda *a, **k: (_ for _ in ()).throw(GitError("x")))
    else:
        flow_repo.script({SIZE_KEY: [act]})
    run = flow_repo.new_run()
    run.emit("run_updated", base_sha=flow_repo.head())  # so the facts read the paths changed since the run started
    assert run_step(flow_repo) == "done"
    decided = flow_repo.events(run, "effort_decided")[0]
    assert (decided["level"], decided["source"]) == ("medium", "fallback")
    assert flow_repo.events(run, "hitl_raised") == [] and flow_repo.events(run, "run_held") == []
    assert flow_repo.events(run, "result_reminded") == [] and flow_repo.events(run, "guard_tripped") == []
    assert steps_called(flow_repo)[-1] == KEY and ("--effort", "medium") in pairs(flow_repo.calls()[-1]["argv"])
    if act in ("call_raises", "git_raises"):
        assert steps_called(flow_repo) == [KEY]


@pytest.mark.parametrize("act", [{"write": {"toy/x.py": "y = 1\n"}},
                                 {"write": {"toy/x.py": "y = 1\n"}, "git_commit": "sneaky"},
                                 {"write": {".git/hooks/post-commit": "#!/bin/sh\n"}}])
def test_a_sizer_that_changes_the_repository_stops_the_run(flow_repo, act):
    flow_repo.script({SIZE_KEY: [act | answer()]})
    run = flow_repo.new_run()
    assert run_step(flow_repo) == "stopped"
    assert flow_repo.events(run, "effort_decided") == [] and len(flow_repo.calls()) == 1
    tripped = flow_repo.events(run, "guard_tripped")[0]
    assert tripped["key"] == KEY and tripped["violations"][0]["kind"] == "sizer_edit"
    assert tripped["before"] == {k: flow_repo.events(run, "sizer_started")[0][k] for k in ("tree", "head", "state")}
    assert [e["kind"] for e in flow_repo.events(run, "hitl_raised")] == ["guard_violation"]
    assert flow_repo.events(run, "step_started") == [] and flow_repo.events(run, "step_finished") == []
    detail = tripped["violations"][0]["detail"]
    expected = {"toy/x.py"} if "toy/x.py" in act.get("write", {}) else {"git state"}
    assert all(name in detail for name in expected | ({"HEAD"} if "git_commit" in act else set()))


def test_a_restart_after_a_sizer_edit_needs_the_state_back(flow_repo):
    hook = flow_repo.root / ".git" / "hooks" / "post-commit"
    flow_repo.script({SIZE_KEY: [{"write": {".git/hooks/post-commit": "#!/bin/sh\n"}} | answer(), answer()]})
    run = flow_repo.new_run()
    assert run_step(flow_repo) == "stopped"
    approve(run, KEY, by="test")
    assert run_step(flow_repo, executor=flow_repo.executor(run)) == "stopped"  # refused: the hook is still there
    tripped = flow_repo.events(run, "guard_tripped")
    assert len(tripped) == 2 and tripped[-1]["violations"][0]["kind"] == "sizer_edit"
    assert "git state" in tripped[-1]["violations"][0]["detail"] and len(flow_repo.calls()) == 1
    approve(run, KEY, by="test")
    assert run_step(flow_repo, executor=flow_repo.executor(run)) == "stopped"  # approving alone changes nothing
    assert len(flow_repo.events(run, "guard_tripped")) == 3 and len(flow_repo.calls()) == 1

    hook.unlink()
    approve(run, KEY, by="test")
    assert run_step(flow_repo, executor=flow_repo.executor(run)) == "done"
    assert steps_called(flow_repo) == [SIZE_KEY, SIZE_KEY, KEY]  # sized again, then the work session
    decided = flow_repo.events(run, "effort_decided")
    assert len(decided) == 1 and decided[0]["source"] == "sizer"
    assert run.state["steps"][KEY]["status"] == "done"


def test_a_reset_checked_once_work_starts_does_not_hold_back_the_steps_own_edits(flow_repo):
    question = {"id": "q1", "question": "Which?", "options": [{"key": "A", "label": "a"}]}
    flow_repo.script({SIZE_KEY: [{"write": {".git/hooks/post-commit": "#!/bin/sh\n"}} | answer()],
                      KEY: [{"write": {"toy/y.py": "z = 1\n"},
                             "result": {"status": "needs_human", "summary": "q", "questions": [question]}},
                            {"result": DONE}]})
    run = flow_repo.new_run()
    assert run_step(flow_repo) == "stopped"
    (flow_repo.root / ".git" / "hooks" / "post-commit").unlink()
    approve(run, KEY, by="test")
    assert run_step(flow_repo, executor=flow_repo.executor(run), size=False) == "stopped"  # its work asks a question
    assert answer_questions(run, KEY, {"q1": "A"}, by="test") is True
    assert run_step(flow_repo, executor=flow_repo.executor(run), size=False) == "done"  # toy/y.py is the step's own
    assert len(flow_repo.events(run, "guard_tripped")) == 1


def test_a_run_paused_while_sizing_starts_no_work(flow_repo):
    flow_repo.script({SIZE_KEY: [{"sleep": 2} | answer()]})
    run = flow_repo.new_run()
    timer = threading.Timer(0.5, lambda: Run.load(flow_repo.root, run.id).emit("run_paused", by="me"))
    timer.start()
    assert run_step(flow_repo) == "stopped"
    timer.join()
    assert flow_repo.events(run, "step_started") == [] and len(flow_repo.calls()) == 1
    assert flow_repo.events(run, "effort_decided") == [] and run.refresh()["status"] == "paused"


@pytest.mark.parametrize("edited", [True, False])
def test_an_orphan_sizer_is_checked_on_resume(flow_repo, edited):
    run = flow_repo.new_run()
    run.emit("sizer_started", key=SIZE_KEY, **flow_repo.executor(run)._repo_state())  # its runner died in the call
    if edited:
        (flow_repo.root / "toy").mkdir()
        (flow_repo.root / "toy" / "x.py").write_text("y = 1\n", encoding="utf-8")
    flow_repo.script({SIZE_KEY: [answer()]})
    outcome = run_step(flow_repo, executor=flow_repo.executor(run))
    if edited:
        assert outcome == "stopped" and flow_repo.calls() == []
        tripped = flow_repo.events(run, "guard_tripped")[0]
        assert tripped["violations"][0]["kind"] == "sizer_edit" and "toy/x.py" in tripped["violations"][0]["detail"]
    else:
        assert outcome == "done" and steps_called(flow_repo) == [SIZE_KEY, KEY]
        assert len(flow_repo.events(run, "sizer_started")) == 2 and len(flow_repo.events(run, "effort_decided")) == 1


def test_an_over_budget_run_is_not_sized(flow_repo):
    flow_repo.configure({"effort": {"sizing": True}, "defaults": {"run_budget_usd": 0.01, "run_budget_hard_usd": 0.01}})
    run = flow_repo.new_run()
    first = make_step(id="first")
    assert flow_repo.executor(run).run_agent_step(make_phase(first), first, "build/first", size=False) == "done"
    assert run_step(flow_repo, executor=flow_repo.executor(run)) == "stopped"
    assert steps_called(flow_repo) == ["build/first"]
    assert [e["kind"] for e in flow_repo.events(run, "hitl_raised")] == ["budget_exceeded"]
    assert flow_repo.events(run, "sizer_started") == []


def test_a_resumed_step_is_not_sized(flow_repo):
    run = flow_repo.new_run()
    _interrupted(flow_repo, run, effort="high")
    assert run_step(flow_repo, executor=flow_repo.executor(run)) == "done"
    assert steps_called(flow_repo) == [KEY]
    work = flow_repo.calls()[0]
    assert ("--effort", "high") in pairs(work["argv"]) and ("--resume", "s1") in pairs(work["argv"])
    assert flow_repo.events(run, "effort_decided") == []


def test_size_false_and_a_shared_session_are_not_sized(flow_repo):
    run = flow_repo.new_run()
    assert run_step(flow_repo, size=False) == "done"
    other = make_step(id="next")
    flow_repo.executor(run).run_agent_step(make_phase(other), other, "build/next", share_session_of=KEY)
    assert steps_called(flow_repo) == [KEY, "build/next"] and flow_repo.events(run, "effort_decided") == []


def test_a_dry_run_is_not_sized(flow_repo):
    run = flow_repo.new_run()
    out = io.StringIO()
    assert run_step(flow_repo, executor=flow_repo.executor(run, dry_run=True, out=out)) == "skipped"
    assert flow_repo.calls() == [] and flow_repo.events(run, "sizer_started") == []
    assert "--effort medium" in out.getvalue() and "--tools" not in out.getvalue()


def test_sizing_off_runs_at_the_default(flow_repo):
    flow_repo.configure({"effort": {"sizing": False}})
    run = flow_repo.new_run()
    assert run_step(flow_repo) == "done"
    assert steps_called(flow_repo) == [KEY] and ("--effort", "medium") in pairs(flow_repo.calls()[0]["argv"])
    assert flow_repo.events(run, "effort_decided") == []


def test_sizer_spend_counts_toward_the_run_budget(flow_repo):
    flow_repo.script({SIZE_KEY: [answer() | {"cost": 0.05}]})
    run = flow_repo.new_run()
    run_step(flow_repo)
    assert run.refresh()["spend_usd"] >= 0.05
