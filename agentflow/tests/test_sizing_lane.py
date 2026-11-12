"""Sizing an agent step before its work session (slice ES, Task 5), and a plan's tasks when build starts (Task 7)."""

from __future__ import annotations

import dataclasses
import io
import json
import re
import threading

import pytest

from agentflow import sizing, sizing_lane
from agentflow.gitops import GitError
from agentflow.hitl import answer as answer_questions
from agentflow.hitl import approve
from agentflow.sizing_lane import SizingLane
from agentflow.state import Run
from conftest import ENGINE
from test_retro import gh_log  # noqa: F401  (the fake `gh` fixture the retro test needs)
from test_agent_step import DONE, KEY, _interrupted, make_phase, make_step, pairs, run_step
from test_feature_lane import BIG, IMPL, PLAN_PATH, REVIEW, begin_build, build, build_with, needs_human

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


# --------------------------------------------------------------------------- internal sessions and the floor (Task 6)


def _no_sizing_traces(flow_repo, run, key: str) -> None:
    assert not [c for c in flow_repo.calls() if c["step"].endswith("/size")]
    assert flow_repo.events(run, "effort_decided") == [] and flow_repo.events(run, "sizer_started") == []
    assert [c["step"] for c in flow_repo.calls()] == [key]


def test_internal_sessions_are_not_sized(flow_repo):
    cond = "build/cond-q"  # a condition question
    flow_repo.script({cond: [{"result": {"status": "done", "summary": "yes", "signals": {"answer": "yes"}}}]})
    run = flow_repo.new_run()
    value, _why = flow_repo.executor(run)._ask_agent(make_phase(make_step()), cond, "some_condition", "no file says")
    assert value is True
    _no_sizing_traces(flow_repo, run, cond)


def test_a_resolver_session_is_not_sized(flow_repo):
    from agentflow.autonomy import Resolver

    session = f"{KEY}/resolve-1"
    flow_repo.script({session: [{"result": {"status": "done", "summary": "settled"}}]})
    run = flow_repo.new_run()
    assert flow_repo.cfg.resolve_step is not None
    assert Resolver(flow_repo.cfg, run, flow_repo.executor(run))._run_session(KEY, "blocked", session, {}) is True
    _no_sizing_traces(flow_repo, run, session)


def test_a_record_step_session_is_not_sized(flow_repo):
    rkey = f"{KEY}/record-q1"
    flow_repo.script({rkey: [{"result": {"status": "done", "summary": "filed"}}]})
    run = flow_repo.new_run()
    executor = flow_repo.executor(run)
    record = flow_repo.cfg.record_step
    assert record is not None
    assert executor._record_one(make_phase(make_step()), record, rkey, {"question": "q?"}, "A") == "done"
    _no_sizing_traces(flow_repo, run, rkey)


def test_the_retro_session_is_not_sized(flow_repo, gh_log):
    from agentflow.retro import run_retro
    from test_retro import KEY as RETRO_KEY, NOW, SEQ, _add, _retro_result, _two_runs_failing

    a, b = _two_runs_failing(flow_repo, "unit")
    flow_repo.fake.script({RETRO_KEY: [_retro_result(add=[_add(sources=[f"{a}#{SEQ}", f"{b}#{SEQ}"])])]})
    out = run_retro(flow_repo.root, flow_repo.cfg, None, False, NOW)
    assert out.run_id
    calls = [c["step"] for c in flow_repo.calls()]
    assert calls == [RETRO_KEY]
    retro = Run.load(flow_repo.root, out.run_id)
    assert flow_repo.events(retro, "effort_decided") == [] and flow_repo.events(retro, "sizer_started") == []
    argv = flow_repo.calls()[0]["argv"]
    assert ("--effort", "medium") in pairs(argv)  # effort.default, and the retro step's review role holds the floor


def test_the_floor_holds_with_sizing_off(flow_repo):
    flow_repo.configure({"effort": {"sizing": False, "default": "low"}})
    run = flow_repo.new_run()
    step = make_step(id="rev", role="review")
    phase = make_phase(step, name="review")
    assert flow_repo.executor(run).run_agent_step(phase, step, "review/rev") == "done"
    assert ("--effort", "medium") in pairs(flow_repo.calls()[0]["argv"])
    assert flow_repo.events(run, "step_started")[0]["effort"] == "medium"
    # a build step is not floored
    build = make_step()
    run2 = flow_repo.new_run()
    assert flow_repo.executor(run2).run_agent_step(make_phase(build), build, KEY) == "done"
    assert ("--effort", "low") in pairs(flow_repo.calls()[-1]["argv"])


def test_the_dry_run_prints_the_floored_level(flow_repo):
    flow_repo.configure({"effort": {"sizing": True, "default": "low"}})
    run = flow_repo.new_run()

    def dry(step, phase_name) -> str:
        out = io.StringIO()
        executor = flow_repo.executor(run, dry_run=True, out=out)
        assert executor.run_steps(make_phase(step, name=phase_name), [step], phase_name) == ("skipped", True)
        return out.getvalue()

    assert "--effort medium" in dry(make_step(id="idea", role="build"), "plan")  # judgement by phase
    assert "--effort medium" in dry(make_step(id="rev", role="review"), "build")  # judgement by role
    text = dry(make_step(id="impl", role="build"), "build")
    assert "--effort low" in text and "--effort medium" not in text  # a work step keeps effort.default
    assert flow_repo.calls() == [] and flow_repo.events(run, "sizer_started") == []


@pytest.mark.parametrize("key,phase,role,level,exited", [
    ("review/rev", "review", "build", "medium", True),          # judgement by phase
    ("build/dbg", "build", "debug", "medium", True),            # judgement by role only
    ("build/task-1/review", "build", "review", "medium", True),  # a build-phase review step
    ("build/task-1/review", "build", "review", "medium", False),
    ("build/impl", "build", "build", "low", True),
    ("build/impl", "build", None, "low", True)])                # no role recorded counts as build
def test_attach_applies_the_floor(flow_repo, monkeypatch, key, phase, role, level, exited):
    import subprocess

    from agentflow import run_control

    run = flow_repo.new_run()
    run.emit("step_started", key=key, phase=phase, agent="claude", model="sonnet", effort="low",
             start_head=flow_repo.head())
    run.emit("agent_started", agent="claude", pid=999999, model="sonnet", effort="low", step=key,
             session_id="s1", phase=phase, role=role)
    if exited:  # the shape of a run `attach` accepts: its runner is gone, so the agent has left `agents`
        run.emit("agent_exited", agent="claude", pid=999999, session_id="s1", step=key, exit_code=0)
        assert run.refresh()["agents"] == {} and run.state["exited"]
    seen = []
    monkeypatch.setattr(run_control.subprocess, "run",
                        lambda argv, *a, **k: (seen.append(argv), subprocess.CompletedProcess(argv, 0))[1])
    assert run_control.attach(run, flow_repo.cfg) == 0
    assert seen[0][-4:] == ["--resume", "s1", "--effort", level]


# --------------------------------------------------------------------------- plan tasks sized when build starts (Task 7)

PREFIX = "build/build"
PLAN_SIZE = PREFIX + sizing.SIZE_MARK + "-{i}"


def plan_doc(count: int, files: dict[int, list[str]] | None = None, notes: dict[int, str] | None = None) -> str:
    parts = ["# Plan: demo\n"]
    for n in range(1, count + 1):
        named = "".join(f"- Modify: `{path}`\n" for path in (files or {}).get(n, []))
        note = f"{(notes or {})[n]}\n\n" if n in (notes or {}) else ""
        parts.append(f"### Task {n}: Thing {n}\n\n{note}{named}\n- [ ] **Step 1: Task {n} test**\n"
                     f"- [ ] **Step 2: Task {n} code**\n")
    return "\n".join(parts)


def plan_answer(by_task: dict[int, dict]) -> dict:
    tasks = {str(n): {"answers": ANSWERS | kw, "reason": f"task {n}"} for n, kw in by_task.items()}
    return {"final_text": json.dumps({"tasks": tasks})}


def levels_of(flow_repo, run) -> dict[str, str]:
    return {e["key"]: e["level"] for e in flow_repo.events(run, "effort_decided")}


def cards_in(call: dict) -> list[int]:
    """The task numbers whose cards a plan-sizer prompt holds, in order."""
    return [int(n) for n in re.findall(r"^step: build/build/task-(\d+)/implement ", call["argv"][-1], re.MULTILINE)]


def size_directly(flow_repo, run, executor=None) -> str:
    """`size_plan` as `run_foreach` calls it, without running the tasks after it."""
    executor = executor or flow_repo.executor(run)
    phase = flow_repo.cfg.phases["build"]
    step = next(s for s in phase.steps if s.id == "build")
    plan = executor.worktree / PLAN_PATH
    executor._batch_of = executor._batches(phase, step, plan, PREFIX, {})
    return executor.size_plan(phase, step, plan, PREFIX, {})


@pytest.fixture
def planned(flow_repo):
    flow_repo.configure({**BIG, "effort": {"sizing": True}})
    return flow_repo


def test_a_plan_is_sized_by_the_plan_model(planned):
    run = begin_build(planned, plan_doc(2))
    planned.script({PLAN_SIZE.format(i=1): [plan_answer({1: {"mechanical": "yes"}, 2: {"risk": "yes"}})]})
    assert build(planned, run) == ("done", True)
    calls = planned.calls()
    assert sum(1 for c in calls if c["env"]["AGENTFLOW_STEP"].endswith("/size-1")) == 1
    assert ("--model", "opus") in pairs(calls[0]["argv"]) and ("--effort", "medium") in pairs(calls[0]["argv"])
    assert ("--max-turns", "16") in pairs(calls[0]["argv"]) and ("--tools", "Read,Grep,Glob") in pairs(calls[0]["argv"])
    levels = levels_of(planned, run)
    assert levels["build/build/task-1/implement"] == "low" and levels["build/build/task-1/review"] == "medium"
    assert levels["build/build/task-2/implement"] == "high" and levels["build/build/task-2/review"] == "high"
    # no step sizer after it: the plan sizer decided every task step
    assert steps_called(planned) == [PLAN_SIZE.format(i=1), IMPL.format(n=1), REVIEW.format(n=1),
                                     IMPL.format(n=2), REVIEW.format(n=2)]
    effort = {c["step"]: dict(pairs(c["argv"]))["--effort"] for c in calls[1:]}
    assert effort == {IMPL.format(n=1): "low", REVIEW.format(n=1): "medium",
                      IMPL.format(n=2): "high", REVIEW.format(n=2): "high"}
    decided = planned.events(run, "effort_decided")
    assert {e["source"] for e in decided} == {"plan_sizer"} and {e["sizer_model"] for e in decided} == {"opus"}
    assert decided[0]["reason"] == "task 1" and decided[0]["answers"] == ANSWERS | {"mechanical": "yes"}
    sized = planned.events(run, "plan_sized")
    assert [(e["key"], e["tasks"]) for e in sized] == [(PREFIX, [1, 2])] and len(sized[0]["digest"]) == 64
    assert cards_in(calls[0]) == [1, 2] and calls[0]["argv"][-1].rstrip().endswith("Task 2 code**")


def test_the_plan_sizer_model_ignores_the_steps_own_model_but_not_the_cli(planned):
    run = begin_build(planned, plan_doc(1))
    executor = planned.executor(run)
    phase = planned.cfg.phases["build"]
    step = next(s for s in phase.steps if s.id == "build")
    plan = executor.worktree / PLAN_PATH
    assert executor.size_plan(phase, dataclasses.replace(step, model="haiku"), plan, PREFIX, {}) == "done"
    assert ("--model", "opus") in pairs(planned.calls()[0]["argv"])
    run2 = begin_build(planned, plan_doc(1) + "\n")
    executor2 = planned.executor(run2)
    executor2.overrides = {"build": "sonnet"}  # `--model build=sonnet`
    assert executor2.size_plan(phase, step, executor2.worktree / PLAN_PATH, PREFIX, {}) == "done"
    assert ("--model", "sonnet") in pairs(planned.calls()[1]["argv"])


def test_the_plan_sizer_prompt_ends_with_the_task_cards():
    text = (ENGINE / "prompts" / sizing_lane.PLAN_SIZE_PROMPT).read_text(encoding="utf-8")
    assert text.rstrip().endswith("{task_cards}")
    for question in sizing.QUESTIONS:
        assert f"**{question}**" in text
    assert "Read, Grep and Glob" in text and '"tasks"' in text and '"answers"' in text


def test_a_long_plan_is_sized_in_chunks(planned):
    run = begin_build(planned, plan_doc(8))
    planned.script({PLAN_SIZE.format(i=1): [plan_answer({n: {"mechanical": "yes"} for n in range(1, 7)})],
                    PLAN_SIZE.format(i=2): [plan_answer({7: {"risk": "yes"}, 8: {}})]})
    assert size_directly(planned, run) == "done"
    calls = planned.calls()
    assert steps_called(planned) == [PLAN_SIZE.format(i=1), PLAN_SIZE.format(i=2)]
    assert cards_in(calls[0]) == [1, 2, 3, 4, 5, 6] and cards_in(calls[1]) == [7, 8]
    levels = levels_of(planned, run)
    assert [levels[f"build/build/task-{n}/implement"] for n in range(1, 9)] == ["low"] * 6 + ["high", "medium"]
    assert {e.get("size_key") for e in planned.events(run, "effort_decided")} == {PLAN_SIZE.format(i=1),
                                                                                   PLAN_SIZE.format(i=2)}
    assert [e["tasks"] for e in planned.events(run, "plan_sized")] == [list(range(1, 9))]


def test_a_batch_runs_at_its_highest_members_level(flow_repo, git):
    flow_repo.configure({**BIG, "effort": {"sizing": True}}, batch=True)
    run = begin_build(flow_repo, plan_doc(2))
    flow_repo.script({PLAN_SIZE.format(i=1): [plan_answer({1: {"risk": "yes"}, 2: {"mechanical": "yes"}})]})
    assert build(flow_repo, run) == ("done", True)
    assert [e["batches"] for e in flow_repo.events(run, "plan_batched")] == [[[1, 2]]]
    levels = levels_of(flow_repo, run)
    assert levels["build/build/task-1/implement"] == "high" and levels["build/build/task-2/implement"] == "high"
    assert "build/build/task-1/review" not in levels  # the batch's one review is its last member's
    assert levels["build/build/task-2/review"] == "high"  # task 2's own review would be `medium`
    calls = {c["step"]: c for c in flow_repo.calls()}
    assert ("--effort", "high") in pairs(calls[IMPL.format(n=1)]["argv"])
    assert ("--effort", "high") in pairs(calls[REVIEW.format(n=2)]["argv"])


def test_only_a_task_naming_a_security_path_is_high(planned, git):
    planned.configure({**BIG, "effort": {"sizing": True}, "overlays": {"security": {"when_paths": ["toy/auth/**"]}}})
    login = planned.root / "toy" / "auth" / "login.py"
    login.parent.mkdir(parents=True)
    login.write_text("a = 1\nb = 2\nc = 3\n", encoding="utf-8")
    text = plan_doc(2, files={1: ["toy/auth/login.py"], 2: ["toy/other.py"]}, notes={2: "Mask the password."})
    run = begin_build(planned, text)  # commits login.py with the plan
    planned.script({PLAN_SIZE.format(i=1): [plan_answer({1: {}, 2: {}})]})
    assert size_directly(planned, run) == "done"
    executor = planned.executor(run)
    from agentflow import overlays

    assert overlays.security_path(planned.cfg, executor.ctx) is True  # the run-wide grep holds ("password")...
    levels = levels_of(planned, run)
    assert levels["build/build/task-1/implement"] == "high"
    assert levels["build/build/task-2/implement"] in ("low", "medium")  # ...but only a task's own paths count
    security = {e["key"]: e["security"] for e in planned.events(run, "effort_decided")}
    assert security["build/build/task-1/implement"] is True and security["build/build/task-2/implement"] is False
    prompt = planned.calls()[0]["argv"][-1]
    assert "toy/auth/login.py (3 lines)" in prompt and "toy/other.py (new or unreadable)" in prompt
    assert prompt.count("security overlay: applies") == 1


def test_touches_security_matches_when_paths():
    from agentflow import overlays
    from agentflow.config import Config

    cfg = Config.__new__(Config)
    object.__setattr__(cfg, "overlays", {"security": {"when_paths": ["toy/auth/**", "**/secrets.py"]}})
    assert overlays.touches_security(cfg, ["docs/x.md", "toy/auth/a/b.py"]) is True
    assert overlays.touches_security(cfg, ["lib/secrets.py"]) is True
    assert overlays.touches_security(cfg, ["toy/other.py"]) is False and overlays.touches_security(cfg, []) is False
    object.__setattr__(cfg, "overlays", {})
    assert overlays.touches_security(cfg, ["toy/auth/a.py"]) is False


def test_line_count_reads_only_regular_files_inside_the_worktree(flow_repo, tmp_path):
    run = flow_repo.new_run()
    executor = flow_repo.executor(run)
    root = executor.worktree
    (root / "pkg").mkdir()
    (root / "pkg" / "a.py").write_text("1\n2\n", encoding="utf-8")
    (root / "pkg" / "big.txt").write_bytes(b"\n" * (2 * 1024 * 1024 + 1))
    outside = tmp_path / "outside.txt"
    outside.write_text("x\n", encoding="utf-8")
    (root / "pkg" / "link.py").symlink_to(root / "pkg" / "a.py")
    (root / "pkg" / "out").symlink_to(tmp_path)
    assert executor._line_count("pkg/a.py") == 2
    assert executor._line_count("pkg/missing.py") is None
    assert executor._line_count("pkg/link.py") is None  # a symlink, even one inside the worktree
    assert executor._line_count("pkg/out/outside.txt") is None  # resolves outside the worktree
    assert executor._line_count("../outside.txt") is None and executor._line_count(str(outside)) is None
    assert executor._line_count("pkg/big.txt") is None and executor._line_count("pkg") is None


def test_a_changed_plan_resizes_only_unstarted_tasks(planned, git):
    run = begin_build(planned, plan_doc(2))
    first, second = PLAN_SIZE.format(i=1), PLAN_SIZE.format(i=1)
    planned.script({first: [plan_answer({1: {"mechanical": "yes"}, 2: {"mechanical": "yes"}}),
                            plan_answer({2: {"risk": "yes"}})]})
    executor = planned.executor(run)
    real = executor._commit_task

    def commit_then_pause(*args, **kwargs):
        out = real(*args, **kwargs)
        run.emit("run_paused", by="me")  # the run stops after task 1 commits, before task 2 starts
        return out

    executor._commit_task = commit_then_pause
    assert build_with(executor, planned) == ("stopped", True)
    assert steps_called(planned) == [first, IMPL.format(n=1), REVIEW.format(n=1)]
    assert "[x] **Step 1: Task 1 test**" in (planned.root / PLAN_PATH).read_text(encoding="utf-8")
    run.emit("run_resumed", by="me")

    assert size_directly(planned, run) == "done"  # ticked boxes keep the digest: no new call
    assert len(planned.calls()) == 3 and len(planned.events(run, "plan_sized")) == 1

    plan = planned.root / PLAN_PATH
    plan.write_text(plan.read_text(encoding="utf-8").replace("Task 2 code**", "Task 2 code, with a cache**"),
                    encoding="utf-8")
    assert build(planned, run) == ("done", True)
    assert steps_called(planned)[3:] == [second, IMPL.format(n=2), REVIEW.format(n=2)]
    assert cards_in(planned.calls()[3]) == [2]  # task 1 is done: only task 2 is sized again
    sized = planned.events(run, "plan_sized")
    assert len(sized) == 2 and sized[0]["digest"] != sized[1]["digest"] and sized[1]["tasks"] == [2]
    decided = planned.events(run, "effort_decided")
    assert [e["level"] for e in decided if e["key"] == IMPL.format(n=1)] == ["low"]  # task 1 keeps its level
    assert [e["level"] for e in decided if e["key"] == IMPL.format(n=2)] == ["low", "high"]
    assert ("--effort", "high") in pairs(planned.calls()[4]["argv"])


@pytest.mark.parametrize("bad", [{"final_text": "not json at all"}, plan_answer({1: {"mechanical": "yes"}})])
def test_a_failed_plan_sizer_is_retried_on_the_next_entry(planned, bad):
    run = begin_build(planned, plan_doc(2))
    size1 = PLAN_SIZE.format(i=1)
    planned.script({size1: [bad, plan_answer({2: {"mechanical": "yes"}})],
                    IMPL.format(n=1): [{"write": {"pkg/one.py": "x = 1\n"}, **needs_human("other")}]})
    assert build(planned, run) == ("stopped", True)
    decided = {e["key"]: e for e in planned.events(run, "effort_decided")}
    task2 = (decided[IMPL.format(n=2)]["level"], decided[IMPL.format(n=2)]["source"])
    assert task2 == ("medium", "fallback")  # effort.default
    assert (decided[REVIEW.format(n=2)]["level"], decided[REVIEW.format(n=2)]["source"]) == ("high", "fallback")
    if "tasks" in bad["final_text"]:  # task 1's own answer was good: only task 2 fell back
        assert (decided[IMPL.format(n=1)]["level"], decided[IMPL.format(n=1)]["source"]) == ("low", "plan_sizer")
    else:
        assert decided[IMPL.format(n=1)]["source"] == "fallback" and decided[REVIEW.format(n=1)]["level"] == "high"
    assert planned.events(run, "plan_sized") == []
    assert planned.events(run, "hitl_raised")[0]["key"] == IMPL.format(n=1)  # the work's question, no sizing stop

    assert answer_questions(run, IMPL.format(n=1), {"q1": "A"}, by="me") is True
    assert build(planned, run) == ("done", True)  # task 1's edit is its own, not a sizer's
    assert steps_called(planned)[:3] == [size1, IMPL.format(n=1), size1]
    assert cards_in(planned.calls()[2]) == [2]  # task 1 had started: only task 2 is sized again
    assert planned.events(run, "guard_tripped") == []
    after = [e for e in planned.events(run, "effort_decided") if e["key"] == IMPL.format(n=2)][-1]
    assert (after["level"], after["source"]) == ("low", "plan_sizer")
    assert [e["tasks"] for e in planned.events(run, "plan_sized")] == [[2]]


def _raises(*a, **k):
    raise RuntimeError("boom")


def test_a_failure_while_building_task_cards_falls_back_per_task(planned, monkeypatch):
    monkeypatch.setattr(sizing, "task_card", _raises)
    run = begin_build(planned, plan_doc(2))
    assert build(planned, run) == ("done", True)
    # no sizer at all: the plan's tasks are decided by the fallback, so no step sizer runs for them either
    assert steps_called(planned) == [IMPL.format(n=1), REVIEW.format(n=1), IMPL.format(n=2), REVIEW.format(n=2)]
    assert {c["step"]: effort_of(c) for c in planned.calls()} == {
        IMPL.format(n=1): "medium", REVIEW.format(n=1): "high", IMPL.format(n=2): "medium", REVIEW.format(n=2): "high"}
    decided = planned.events(run, "effort_decided")
    assert sorted(e["key"] for e in decided) == sorted([IMPL.format(n=1), REVIEW.format(n=1), IMPL.format(n=2),
                                                         REVIEW.format(n=2)])
    assert {(e["source"], e["size_key"]) for e in decided} == {("fallback", PLAN_SIZE.format(i=1))}
    assert planned.events(run, "plan_sized") == [] and planned.events(run, "hitl_raised") == []
    assert planned.events(run, "guard_tripped") == [] and planned.events(run, "run_held") == []


def test_a_failure_while_building_task_cards_is_retried_on_the_next_entry(planned, monkeypatch):
    run = begin_build(planned, plan_doc(2))
    with monkeypatch.context() as patch:
        patch.setattr(sizing, "task_card", _raises)
        assert size_directly(planned, run) == "done"
    assert planned.calls() == [] and planned.events(run, "plan_sized") == []
    assert {e["source"] for e in planned.events(run, "effort_decided")} == {"fallback"}
    planned.script({PLAN_SIZE.format(i=1): [plan_answer({1: {"mechanical": "yes"}, 2: {}})]})
    assert size_directly(planned, run) == "done"  # the plan sizer is tried again
    assert steps_called(planned) == [PLAN_SIZE.format(i=1)]
    assert levels_of(planned, run)[IMPL.format(n=1)] == "low"
    assert [e["tasks"] for e in planned.events(run, "plan_sized")] == [[1, 2]]


@pytest.mark.parametrize("act", ["call_raises", "timed_out", "wait_raises", "state_raises"])
def test_a_failed_plan_sizer_falls_back_without_a_stop(planned, monkeypatch, act):
    monkeypatch.setattr(sizing, "PLAN_SIZE_TIMEOUT_S", 2)  # the code reads it at call time
    run = begin_build(planned, plan_doc(2))
    if act == "call_raises":
        monkeypatch.setattr(SizingLane, "_run_sizer", _raises)
    elif act == "timed_out":
        planned.script({PLAN_SIZE.format(i=1): [{"sleep": 999} | plan_answer({1: {}, 2: {}})]})
    elif act == "wait_raises":  # before the orphan check: the except path runs it
        monkeypatch.setattr(sizing_lane, "wait_for_agents", _raises)
    else:  # after the orphan check: the repository state cannot be read, so `_sizer_call` raises
        monkeypatch.setattr(SizingLane, "_repo_state", _raises)
    assert size_directly(planned, run) == "done"
    decided = planned.events(run, "effort_decided")
    assert {(e["key"], e["level"], e["source"]) for e in decided} == {
        (IMPL.format(n=1), "medium", "fallback"), (REVIEW.format(n=1), "high", "fallback"),
        (IMPL.format(n=2), "medium", "fallback"), (REVIEW.format(n=2), "high", "fallback")}
    assert {e["size_key"] for e in decided} == {PLAN_SIZE.format(i=1)}
    assert planned.events(run, "hitl_raised") == [] and planned.events(run, "guard_tripped") == []
    assert planned.events(run, "run_held") == [] and planned.events(run, "plan_sized") == []
    assert steps_called(planned) == ([PLAN_SIZE.format(i=1)] if act == "timed_out" else [])


def test_a_raising_wait_still_checks_an_orphan_plan_sizer(planned, monkeypatch):
    run = begin_build(planned, plan_doc(2))
    size1 = PLAN_SIZE.format(i=1)
    run.emit("sizer_started", key=size1, **planned.executor(run)._repo_state())  # its runner died in the call
    (planned.root / "pkg").mkdir(exist_ok=True)
    (planned.root / "pkg" / "x.py").write_text("y = 1\n", encoding="utf-8")
    monkeypatch.setattr(sizing_lane, "wait_for_agents", _raises)
    assert size_directly(planned, run) == "stopped"
    assert planned.calls() == [] and planned.events(run, "effort_decided") == []
    tripped = planned.events(run, "guard_tripped")
    assert [t["key"] for t in tripped] == [PREFIX] and "pkg/x.py" in tripped[0]["violations"][0]["detail"]


@pytest.mark.parametrize("broken", ["raise_hitl", "sizer_stop"])
def test_an_orphan_plan_sizer_stop_that_raises_still_stops(planned, monkeypatch, broken):
    run = begin_build(planned, plan_doc(2))
    size1 = PLAN_SIZE.format(i=1)
    run.emit("sizer_started", key=size1, **planned.executor(run)._repo_state())  # its runner died in the call
    (planned.root / "pkg").mkdir(exist_ok=True)
    (planned.root / "pkg" / "x.py").write_text("y = 1\n", encoding="utf-8")
    with monkeypatch.context() as patch:
        patch.setattr(sizing_lane, "wait_for_agents", _raises)
        if broken == "raise_hitl":  # the trip is recorded, the inbox write fails
            patch.setattr(sizing_lane, "raise_hitl", _raises)
        else:  # nothing is recorded at all
            patch.setattr(SizingLane, "_sizer_stop", _raises)
        assert size_directly(planned, run) == "stopped"
    assert planned.events(run, "effort_decided") == [] and planned.events(run, "plan_sized") == []
    assert planned.calls() == []
    executor = planned.executor(run)
    if broken == "raise_hitl":  # the trip stays pending: nothing cleared it
        assert executor._sizer_trip(PREFIX) is not None and "pkg/x.py" in executor.sizer_reset_needed(PREFIX)
    assert size_directly(planned, run) == "stopped"  # the next entry stops too, and now asks a human
    assert run.refresh()["waiting"]["kind"] == "guard_violation" and planned.calls() == []
    assert planned.events(run, "effort_decided") == []


def test_a_cleared_plan_stop_whose_plan_cannot_be_read_is_settled_and_sized_again(planned, monkeypatch, git):
    from agentflow import sources

    run = begin_build(planned, plan_doc(2))
    size1 = PLAN_SIZE.format(i=1)
    planned.script({size1: [{"write": {"pkg/x.py": "y = 1\n"}} | plan_answer({1: {}, 2: {}}),
                            plan_answer({1: {"mechanical": "yes"}, 2: {}})]})
    assert size_directly(planned, run) == "stopped"
    (planned.root / "pkg" / "x.py").unlink()
    approve(run, PREFIX, by="me")
    executor = planned.executor(run)
    phase = planned.cfg.phases["build"]
    step = next(s for s in phase.steps if s.id == "build")
    plan = executor.worktree / PLAN_PATH
    executor._batch_of = executor._batches(phase, step, plan, PREFIX, {})
    with monkeypatch.context() as patch:
        patch.setattr(sources, "parse_plan", _raises)
        assert executor.size_plan(phase, step, plan, PREFIX, {}) == "done"
    assert [(e["digest"], e["tasks"]) for e in planned.events(run, "plan_sized")] == [(None, [])]
    assert planned.events(run, "effort_decided") == [] and executor._sizer_trip(PREFIX) is None
    assert size_directly(planned, run) == "done"  # a later entry sizes the plan again
    assert steps_called(planned) == [size1, size1] and levels_of(planned, run)[IMPL.format(n=1)] == "low"
    assert [e["tasks"] for e in planned.events(run, "plan_sized")] == [[], [1, 2]]


def test_a_plan_sizer_edit_blocks_build_until_reset(planned, git):
    run = begin_build(planned, plan_doc(2))
    size1 = PLAN_SIZE.format(i=1)
    planned.script({size1: [{"write": {"pkg/x.py": "y = 1\n"}} | plan_answer({1: {}, 2: {}}),
                            plan_answer({1: {"mechanical": "yes"}, 2: {}})]})
    assert build(planned, run) == ("stopped", True)
    assert steps_called(planned) == [size1] and planned.events(run, "effort_decided") == []
    waiting = run.refresh()["waiting"]
    assert (waiting["kind"], waiting["key"]) == ("guard_violation", PREFIX)
    tripped = planned.events(run, "guard_tripped")
    assert tripped[0]["key"] == PREFIX and tripped[0]["violations"][0]["kind"] == "sizer_edit"
    assert "pkg/x.py" in tripped[0]["violations"][0]["detail"]
    assert tripped[0]["before"] == {k: planned.events(run, "sizer_started")[0][k] for k in ("tree", "head", "state")}
    assert build(planned, run) == ("stopped", True) and len(planned.events(run, "guard_tripped")) == 1  # waiting

    approve(run, PREFIX, by="me")
    assert build(planned, run) == ("stopped", True)  # refused: the file is still there
    assert steps_called(planned) == [size1] and len(planned.events(run, "guard_tripped")) == 2
    assert "pkg/x.py" in run.refresh()["waiting"]["summary"]

    (planned.root / "pkg" / "x.py").unlink()
    approve(run, PREFIX, by="me")
    assert build(planned, run) == ("done", True)
    assert steps_called(planned) == [size1, size1, IMPL.format(n=1), REVIEW.format(n=1), IMPL.format(n=2),
                                     REVIEW.format(n=2)]
    assert levels_of(planned, run)[IMPL.format(n=1)] == "low"
    assert [e["tasks"] for e in planned.events(run, "plan_sized")] == [[1, 2]]


def test_approving_a_plan_sizer_stop_keeps_the_forbidden_file_check(flow_repo, git):
    flow_repo.configure({**BIG, "effort": {"sizing": True}, "guardrails": {"forbidden_paths": ["eval/baseline.json"]}})
    run = begin_build(flow_repo, plan_doc(2))
    flow_repo.script({PLAN_SIZE.format(i=1): [{"write": {"pkg/x.py": "y = 1\n"}} | plan_answer({1: {}, 2: {}})]})
    assert build(flow_repo, run) == ("stopped", True)
    (flow_repo.root / "pkg" / "x.py").unlink()
    approve(run, PREFIX, by="me")
    plan = flow_repo.root / PLAN_PATH
    plan.write_text(plan_doc(2, files={2: ["eval/baseline.json"]}), encoding="utf-8")
    assert build(flow_repo, run) == ("stopped", True)
    waiting = run.refresh()["waiting"]
    assert waiting["kind"] == "blocked" and "Task 2: eval/baseline.json" in waiting["summary"]


def test_a_dry_run_or_sizing_off_does_not_size_the_plan(planned):
    run = begin_build(planned, plan_doc(2))
    executor = planned.executor(run, dry_run=True, out=io.StringIO())
    assert size_directly(planned, run, executor) == "done"
    planned.configure({**BIG, "effort": {"sizing": False}})
    assert size_directly(planned, run) == "done"
    assert planned.calls() == [] and planned.events(run, "sizer_started") == []
    assert planned.events(run, "effort_decided") == [] and planned.events(run, "plan_sized") == []


@pytest.mark.parametrize("after", ["fallback", "sizing_off"])
def test_a_cleared_plan_sizer_stop_does_not_hold_back_later_work(planned, after):
    run = begin_build(planned, plan_doc(2))
    size1 = PLAN_SIZE.format(i=1)
    planned.script({size1: [{"write": {"pkg/x.py": "y = 1\n"}} | plan_answer({1: {}, 2: {}}), {"final_text": "?"}],
                    IMPL.format(n=1): [{"write": {"pkg/one.py": "x = 1\n"}, **needs_human("other")}]})
    assert build(planned, run) == ("stopped", True)
    (planned.root / "pkg" / "x.py").unlink()
    if after == "sizing_off":
        planned.configure({**BIG, "effort": {"sizing": False}})
    approve(run, PREFIX, by="me")
    assert build(planned, run) == ("stopped", True)
    assert run.refresh()["waiting"]["key"] == IMPL.format(n=1)  # task 1's own question, after it wrote pkg/one.py
    assert answer_questions(run, IMPL.format(n=1), {"q1": "A"}, by="me") is True
    assert build(planned, run) == ("done", True)  # its edit is not read as the plan sizer's
    assert len(planned.events(run, "guard_tripped")) == 1
    if after == "sizing_off":
        assert [(e["digest"], e["tasks"]) for e in planned.events(run, "plan_sized")] == [(None, [])]


def test_a_run_paused_while_sizing_the_plan_starts_no_task(planned):
    run = begin_build(planned, plan_doc(2))
    size1 = PLAN_SIZE.format(i=1)
    planned.script({size1: [{"sleep": 2} | plan_answer({1: {}, 2: {}}), plan_answer({1: {}, 2: {}})]})
    timer = threading.Timer(0.5, lambda: Run.load(planned.root, run.id).emit("run_paused", by="me"))
    timer.start()
    assert build(planned, run) == ("stopped", True)
    timer.join()
    assert steps_called(planned) == [size1] and run.refresh()["status"] == "paused"
    assert planned.events(run, "effort_decided") == [] and planned.events(run, "plan_sized") == []
    run.emit("run_resumed", by="me")
    assert build(planned, run) == ("done", True)  # the open sizer is checked, then the plan is sized again
    assert steps_called(planned)[:3] == [size1, size1, IMPL.format(n=1)]
    assert planned.events(run, "guard_tripped") == [] and len(planned.events(run, "plan_sized")) == 1


# --------------------------------------------------------------------------- escalating once (Task 8)

FAILING = make_step(gates=["fail"], fix_loop=True)


def effort_of(call: dict) -> str:
    return dict(pairs(call["argv"]))["--effort"]


def test_the_first_gate_failure_resumes_one_level_higher(flow_repo):
    flow_repo.script({SIZE_KEY: [answer()], KEY: [{"result": DONE}, {"result": DONE}]})
    run = flow_repo.new_run()
    assert run_step(flow_repo, FAILING) == "stopped"
    work, fix = flow_repo.calls()[1:3]
    assert ("--effort", "medium") in pairs(work["argv"])
    assert ("--effort", "high") in pairs(fix["argv"]) and "--resume" in fix["argv"]
    escalated = flow_repo.events(run, "effort_escalated")
    assert [(e["key"], e["from"], e["to"], e["trigger"]) for e in escalated] == [(KEY, "medium", "high", "gate")]
    assert run.state["steps"][KEY]["effort"] == "high" and run.state["steps"][KEY]["effort_changed"] == "up"


def test_a_step_escalates_once(flow_repo):
    flow_repo.script({SIZE_KEY: [answer()]})
    run = flow_repo.new_run()
    assert run_step(flow_repo, FAILING) == "stopped"
    calls = flow_repo.calls()
    assert [effort_of(c) for c in calls[1:]] == ["medium", "high", "high", "high"]  # three fix attempts
    assert len(flow_repo.events(run, "effort_escalated")) == 1
    assert [e["kind"] for e in flow_repo.events(run, "hitl_raised")] == ["fix_attempts_exhausted"]
    approve(run, KEY, by="me")  # another round of fix attempts: still no second escalation
    assert run_step(flow_repo, FAILING, executor=flow_repo.executor(run)) == "stopped"
    assert {effort_of(c) for c in flow_repo.calls()[4:]} == {"high"}
    assert len(flow_repo.events(run, "effort_escalated")) == 1


def test_a_rollover_keeps_the_level(flow_repo):
    flow_repo.script({SIZE_KEY: [answer()], KEY: [{"usage_pct": [81]}, {"result": DONE}]})
    run = flow_repo.new_run()
    assert run_step(flow_repo) == "done"
    sizer, work, fresh = flow_repo.calls()
    assert effort_of(work) == "medium" and effort_of(fresh) == "medium" and "--resume" not in fresh["argv"]
    assert len(flow_repo.events(run, "rolled_over")) == 1 and flow_repo.events(run, "effort_escalated") == []


def test_a_reopening_review_resumes_the_implementer_one_level_higher(planned, git):
    from test_feature_lane import FINDING, review

    run = begin_build(planned, plan_doc(1))
    impl, rev = IMPL.format(n=1), REVIEW.format(n=1)
    planned.script({PLAN_SIZE.format(i=1): [plan_answer({1: {}})], rev: [review(FINDING), review()]})
    assert build(planned, run) == ("done", True)
    calls = planned.calls()
    assert steps_called(planned) == [PLAN_SIZE.format(i=1), impl, rev, impl, rev]
    assert [effort_of(c) for c in calls[1:]] == ["medium", "medium", "high", "medium"]
    assert "--resume" in calls[3]["argv"]  # the implementer's own session, one level higher
    escalated = planned.events(run, "effort_escalated")
    assert [(e["key"], e["from"], e["to"], e["trigger"]) for e in escalated] == [(impl, "medium", "high", "review")]


def test_a_refine_round_resumes_one_level_higher(flow_repo):
    flow_repo.script({SIZE_KEY: [answer()], KEY: [{"result": DONE}, {"result": DONE}]})
    run = flow_repo.new_run()
    assert run_step(flow_repo) == "done"
    run.emit("phase_rejected", phase="build", round=1, note="tighten it")
    assert run_step(flow_repo, executor=flow_repo.executor(run), mode="refine") == "done"
    sizer, work, refine = flow_repo.calls()
    assert effort_of(work) == "medium" and effort_of(refine) == "high" and "--resume" in refine["argv"]
    assert [e["trigger"] for e in flow_repo.events(run, "effort_escalated")] == ["review"]


def test_a_step_started_in_its_fix_loop_escalates_on_its_next_fix(flow_repo):
    from agentflow.gates import GateResult

    flow_repo.script({SIZE_KEY: [answer()]})
    run = flow_repo.new_run()
    failed = [GateResult(name="fail", passed=False, skipped=False, reason="", log_path=None, tail="boom")]
    assert run_step(flow_repo, FAILING, fix_first=failed) == "stopped"
    assert [effort_of(c) for c in flow_repo.calls()[1:]] == ["medium", "high", "high"]
    assert [e["trigger"] for e in flow_repo.events(run, "effort_escalated")] == ["gate"]


def test_no_escalation_at_the_top_level(flow_repo):
    flow_repo.script({SIZE_KEY: [answer(risk="yes")]})
    run = flow_repo.new_run()
    assert run_step(flow_repo, FAILING) == "stopped"
    assert {effort_of(c) for c in flow_repo.calls()[1:]} == {"high"} and len(flow_repo.calls()) == 5
    assert flow_repo.events(run, "effort_escalated") == []


def test_an_escalation_that_raises_keeps_the_level_and_the_step_runs_on(flow_repo, monkeypatch):
    monkeypatch.setattr(sizing, "escalate", lambda *a, **k: (_ for _ in ()).throw(ValueError("unknown level")))
    flow_repo.configure({"effort": {"sizing": True}, "gates": {"flaky": {"run": "test -f fixed.txt"}}})
    flow_repo.script({SIZE_KEY: [answer()], KEY: [{"result": DONE}, {"write": {"fixed.txt": "ok\n"}, "result": DONE}]})
    run = flow_repo.new_run()
    assert run_step(flow_repo, make_step(gates=["flaky"], fix_loop=True)) == "done"
    sizer, work, fix = flow_repo.calls()
    assert effort_of(work) == "medium" and effort_of(fix) == "medium" and "--resume" in fix["argv"]
    assert flow_repo.events(run, "effort_escalated") == [] and run.state["steps"][KEY]["effort"] == "medium"
    assert flow_repo.events(run, "hitl_raised") == [] and run.state["steps"][KEY]["status"] == "done"


def test_no_escalation_with_sizing_off(flow_repo):
    flow_repo.configure({"effort": {"sizing": False}})
    run = flow_repo.new_run()
    assert run_step(flow_repo, FAILING) == "stopped"
    assert {effort_of(c) for c in flow_repo.calls()} == {"medium"} and len(flow_repo.calls()) == 4
    assert flow_repo.events(run, "effort_escalated") == []


# --------------------------------------------------------------------------- a restart after a sizer stop, sizer keys


def test_a_retried_step_stopped_by_its_sizer_is_sized_again_and_runs_fresh(flow_repo):
    from agentflow.run_control import retry

    hook = flow_repo.root / ".git" / "hooks" / "post-commit"
    flow_repo.script({SIZE_KEY: [{"write": {".git/hooks/post-commit": "#!/bin/sh\n"}} | answer(), answer()]})
    run = flow_repo.new_run()
    # interrupted in its first turn, a self-sizing one (e.g. started while `effort.sizing` was off): never sized
    run.emit("step_started", key=KEY, phase="build", agent="claude", model="sonnet", effort="high",
             start_head=flow_repo.head())
    run.emit("agent_started", agent="claude", pid=999999, model="sonnet", effort="high", step=KEY, session_id="s1",
             sizing=True)
    assert not run.refresh()["steps"][KEY].get("sized")
    assert retry(Run.load(flow_repo.root, run.id), KEY, by="me") == KEY
    assert run_step(flow_repo, executor=flow_repo.executor(run)) == "stopped"  # the sizer edited the repository
    hook.unlink()
    approve(run, KEY, by="me")
    assert run_step(flow_repo, executor=flow_repo.executor(run)) == "done"
    assert steps_called(flow_repo) == [SIZE_KEY, SIZE_KEY, KEY]
    work = flow_repo.calls()[-1]
    assert "--resume" not in work["argv"] and effort_of(work) == "medium"
    assert [e["source"] for e in flow_repo.events(run, "effort_decided")] == ["sizer"]


@pytest.mark.parametrize("key", ["build/sizes", "build/size-up", "build/size-1a"])
def test_a_step_whose_key_merely_contains_size_is_sized(flow_repo, key):
    step = make_step(id=key.split("/", 1)[1])
    flow_repo.script({f"{key}/size": [answer(mechanical="yes")]})
    run = flow_repo.new_run()
    assert flow_repo.executor(run).run_agent_step(make_phase(step), step, key) == "done"
    assert steps_called(flow_repo) == [f"{key}/size", key]
    assert flow_repo.events(run, "effort_decided")[0]["level"] == "low"


@pytest.mark.parametrize("key,expected", [("build/impl/size", True), ("build/build/size-1", True),
                                          ("build/build/size-12", True), ("build/sizes", False),
                                          ("build/size-up", False), ("build/size-1a", False),
                                          ("build/impl", False)])
def test_is_sizer_key_matches_only_the_sizers_own_keys(key, expected):
    assert sizing.is_sizer_key(key) is expected
