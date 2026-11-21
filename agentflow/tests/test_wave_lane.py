"""Waves of plan tasks in the runner (`docs/agentflow-throughput-proposal.md`, T5): what `plan_review` is told of
tasks that could share a `**Parallel group:**`, and how often the runner counts them. Built on the helpers of
`test_feature_lane.py` and `test_doubt_flow.py`; the later wave tests of this file reuse `WAVES` and `candidate_plan`."""

from __future__ import annotations

import dataclasses

import pytest

from agentflow import sources
from agentflow.conditions import make_ctx
from agentflow.hitl import answer, approve
from agentflow.pipeline import RunOptions, advance, start
from conftest import with_doubt
from test_feature_lane import (BIG, DONE_RESULT, IMPL, PLAN_PATH, REVIEW, SHELVES, TITLES, begin_build, build,
                               commit_of, files_of, needs_human, plan_text, sessions_of, shelves, steps, subjects)
from test_sizing_lane import PLAN_SIZE, plan_answer

WAVES = {"parallel": {"max_subagents": 2}}
SHARED = ["src/core.py"]
# Tasks 1 to 5 are done; tasks 6 and 7 are open, ungrouped, independent and each have a file of their own.
CANDIDATE_FILES = {n: SHARED for n in range(1, 6)} | {6: ["src/six.py"], 7: ["src/seven.py"]}


def candidate_plan() -> str:
    none = {n: "none" for n in range(1, 8)}  # a task with no `Depends on` line follows the one before it
    return plan_text(none, count=7, ticked=(1, 2, 3, 4, 5), files=CANDIDATE_FILES)


def candidates_of(flow_repo, run) -> list[dict]:
    return flow_repo.events(run, "parallel_candidates")


def plan_review_prompt(flow_repo, patch: dict, text: str) -> str:
    """The prompt of the `plan/plan_review` session of a run that stops at the plan's approval."""
    flow_repo.configure(patch, autopilot=True, edit=with_doubt)
    flow_repo.script({"define/draft": [{"write": {"docs/specs/SPEC-demo.md": SPEC}, "result": DONE}]})
    run = start(flow_repo.cfg, flow_repo.root, "feature", {"module": "demo"}, RunOptions(to_phase="plan"))
    assert advance(run, flow_repo.cfg, RunOptions()) == "awaiting_approval"
    approve(run, "define", by="me")
    plan = make_ctx(flow_repo.cfg, flow_repo.root, "feature", run.state["params"], run).values["plan_target"]
    flow_repo.script({"plan/plan": [{"write": {plan: text}, "result": DONE}],
                      "plan/plan_review": [{"result": DONE}]})
    assert advance(run, flow_repo.cfg, RunOptions()) == "awaiting_approval"
    return next(c for c in flow_repo.calls() if c["step"] == "plan/plan_review")["argv"][-1]


SPEC = "# Spec: `demo`\n\n**Status: proposed** (pending approval)\n\n## Objective\n\nA demo.\n\n## Open Questions\n\nNone.\n"
DONE = {"status": "done", "summary": "ok"}


def test_plan_review_hears_of_ungrouped_candidates(flow_repo):
    prompt = plan_review_prompt(flow_repo, {"gate_sets": {"build": ["ok"], "verify": ["ok"]}, **WAVES},
                                candidate_plan())
    assert "Tasks 6 and 7 could share" in prompt
    assert "{parallel_candidates}" not in prompt


def test_plan_review_hears_nothing_with_waves_off(flow_repo):
    prompt = plan_review_prompt(flow_repo, {"gate_sets": {"build": ["ok"], "verify": ["ok"]}}, candidate_plan())
    assert "could share" not in prompt and "{parallel_candidates}" not in prompt


def test_candidates_are_counted_once(flow_repo):
    flow_repo.configure({**BIG, **WAVES})
    run = begin_build(flow_repo, candidate_plan())
    key = IMPL.format(n=6)
    flow_repo.script({key: [needs_human("other")]})
    assert build(flow_repo, run) == ("stopped", True)
    [event] = candidates_of(flow_repo, run)
    assert event["key"] == "build/build" and event["pairs"] == [[6, 7]]
    assert answer(run, key, {"q1": "A"}, by="me")
    assert build(flow_repo, run) == ("done", True)  # a new executor, as `agentflow resume` makes
    assert len(candidates_of(flow_repo, run)) == 1


def test_candidates_are_not_counted_with_waves_off(flow_repo):
    flow_repo.configure(BIG)
    run = begin_build(flow_repo, candidate_plan())
    assert build(flow_repo, run) == ("done", True)
    assert candidates_of(flow_repo, run) == []


# --------------------------------------------------------------------------- the wave in the plan walk (Task 7)


WAVE = "build/build/wave-1/implement"
FILES = {1: ["src/one.py"], 2: ["src/two.py"], 3: ["src/three.py"], 4: ["src/four.py"]}
DEPENDS = {1: "none", 2: "Task 1", 3: "Task 1", 4: "Task 1"}
GROUPS = {2: "A", 3: "A"}
BLOCKED = {"result": {"status": "blocked", "summary": "stuck"}}


def wave_plan(files: dict[int, list[str]] | None = None, count: int = 3) -> str:
    return plan_text(DEPENDS, count=count, files={**FILES, **(files or {})}, groups=GROUPS)


def sub(n: int, write: dict[str, str] | None = None) -> dict:
    """One sub-agent of the wave's dispatch: its description names its task, and it writes the task's file."""
    return {"description": f"Task {n}: {TITLES[n]}", "write": write or {FILES[n][0]: f"n = {n}\n"}}


def report(n: int, status: str = "done", files: list[str] | None = None) -> dict:
    return {"n": n, "status": status, "summary": f"built task {n}", "files": files or FILES[n], "tests_run": []}


def wave_act(subs: list[dict] | None = None, tasks: list[dict] | None = None) -> dict:
    return {"parallel_subagents": subs or [sub(2), sub(3)],
            "result": {"status": "done", "summary": "dispatched both", "tasks": tasks or [report(2), report(3)]}}


def demoted(events: list[dict]) -> list[tuple[int, str]]:
    return [(e["task"], e["reason"]) for e in events if e["type"] == "parallel_demoted"]


def scope_violations(events: list[dict]) -> list[tuple[int | None, str]]:
    return [(e["task"], e["path"]) for e in events if e["type"] == "scope_violation"]


def branch_files(git, root) -> set[str]:
    """Every path a commit of the run's branch touched."""
    return set(git(root, "log", "--format=", "--name-only", "HEAD").split())


@pytest.fixture
def waves(flow_repo):
    flow_repo.configure({**BIG, **WAVES})
    return flow_repo


def test_two_grouped_tasks_build_in_one_wave_session(waves, git):  # the spec's first T5 check
    run = begin_build(waves, wave_plan())
    waves.script({WAVE: [wave_act()]})
    assert build(waves, run) == ("done", True)
    calls = waves.calls()
    # each member still gets its own review session until the review wave (Task 8)
    assert steps(calls) == [IMPL.format(n=1), REVIEW.format(n=1), WAVE, REVIEW.format(n=2), REVIEW.format(n=3)]
    events = run.events.read()
    started = [e["seq"] for e in events if e["type"] == "subagent_started"]
    finished = [e["seq"] for e in events if e["type"] == "subagent_finished"]
    assert len(started) == 2 and max(started) < min(finished)  # both were dispatched before either came back
    assert subjects(git, waves.root)[:2] == ["Task 3: Third thing", "Task 2: Second thing"]
    assert files_of(git, waves.root, "Task 2: Second thing") == [PLAN_PATH, "src/two.py"]  # its own files only
    assert files_of(git, waves.root, "Task 3: Third thing") == [PLAN_PATH, "src/three.py"]
    body = git(waves.root, "log", "-1", "--format=%B", commit_of(git, waves.root, "Task 2: Second thing"))
    assert "built task 2" in body  # the member's report is its implement summary
    [wave_started] = [e for e in events if e["type"] == "wave_started"]
    assert wave_started["key"] == "build/build/wave-1" and wave_started["tasks"] == [2, 3]
    [effort] = [e for e in events if e["type"] == "effort_decided" and e["key"] == WAVE]
    assert effort["source"] == "wave"
    [finished_event] = [e for e in events if e["type"] == "wave_finished"]
    assert finished_event["committed"] == [2, 3] and finished_event["parked"] == []
    projected = [e for e in events if e["type"] == "step_finished" and e.get("wave") == 1]
    assert [e["key"] for e in projected] == [IMPL.format(n=2), IMPL.format(n=3)]


def test_the_wave_prompt_names_one_card_per_member(waves):
    run = begin_build(waves, wave_plan())
    waves.script({WAVE: [wave_act()]})
    assert build(waves, run) == ("done", True)
    prompt = next(c for c in waves.calls() if c["step"] == WAVE)["argv"][-1]
    cards = run.agent_dir / "wave" / "wave-1"
    assert "Tasks 2, 3" in prompt and "{wave_cards}" not in prompt and "{wave_title}" not in prompt
    assert f"- Task 2: Second thing: `{cards / 'task-2.md'}`" in prompt
    assert f"- Task 3: Third thing: `{cards / 'task-3.md'}`" in prompt
    card = (cards / "task-2.md").read_text(encoding="utf-8")
    assert "Task 2 test" in card and "Task 3 test" not in card  # the member's own task text
    assert "`src/two.py`" in card and "{allowed_files}" not in card and "{task_n}" not in card
    assert "## When you finish" not in card  # no result contract: the wave session reports for it


def test_the_build_gates_run_once_for_the_wave(waves):
    run = begin_build(waves, wave_plan())
    waves.script({WAVE: [wave_act()]})
    assert build(waves, run) == ("done", True)
    # task 1 alone, then the wave once: no gate run on task-2/commit or task-3/commit
    assert [e["key"] for e in waves.events(run, "gate_result")] == [IMPL.format(n=1), WAVE]


def test_a_red_wave_gate_reopens_the_wave_session(waves, git):
    waves.configure({**BIG, **WAVES, "gates": {"three": {"run": "test ! -f src/three.py || grep -q fixed src/three.py"}},
                     "gate_sets": {"build": ["three"]}})
    run = begin_build(waves, wave_plan())
    fix = {"write": {"src/three.py": "fixed = 3\n"},
           "result": {"status": "done", "summary": "fixed three", "tasks": [report(2), report(3)]}}
    waves.script({WAVE: [wave_act(), fix]})
    assert build(waves, run) == ("done", True)
    sessions = sessions_of(waves, run, WAVE)
    assert len(sessions) == 2 and sessions[0] == sessions[1]  # resumed, not fresh
    [reopened] = [e for e in waves.events(run, "step_reopened") if e["key"] == WAVE]
    assert reopened["reason"] == "gates" and "dispatch one fix `Task` per member" in reopened["note"]
    second = [c for c in waves.calls() if c["step"] == WAVE][1]["argv"]
    assert "--resume" in second and "dispatch one fix `Task` per member" in second[-1]
    assert [(e["key"], e["passed"]) for e in waves.events(run, "gate_result") if e["key"] == WAVE] == [
        (WAVE, False), (WAVE, True)]
    assert git(waves.root, "show", "HEAD:src/three.py") == "fixed = 3\n"
    assert subjects(git, waves.root)[:2] == ["Task 3: Third thing", "Task 2: Second thing"]


def test_a_grouped_task_sharing_a_file_runs_alone(waves, git):
    run = begin_build(waves, wave_plan(files={3: ["src/three.py", "src/two.py"]}))
    assert build(waves, run) == ("done", True)
    events = run.events.read()
    assert demoted(events) == [(3, "shared_files")]
    assert WAVE not in steps(waves.calls()) and not [e for e in events if e["type"] == "wave_started"]
    assert {IMPL.format(n=2), IMPL.format(n=3)} <= set(steps(waves.calls()))


def test_a_member_writing_outside_its_files_is_reverted_and_runs_alone(waves, git):
    run = begin_build(waves, wave_plan())
    strays = sub(3, write={"src/three.py": "n = 3\n", "src/zz.py": "zz = 1\n"})
    waves.script({WAVE: [wave_act(subs=[sub(2), strays], tasks=[report(2), report(3, files=["src/three.py",
                                                                                            "src/zz.py"])])],
                  IMPL.format(n=3): [{"write": {"src/three.py": "alone = 3\n"}, "result": DONE_RESULT}]})
    assert build(waves, run) == ("done", True)
    events = run.events.read()
    assert scope_violations(events) == [(3, "src/zz.py")]
    assert demoted(events) == [(3, "scope_violation")]
    assert not (waves.root / "src/zz.py").exists()  # reverted to the wave's start
    assert "src/zz.py" not in branch_files(git, waves.root)  # and in no commit
    assert IMPL.format(n=3) in steps(waves.calls())  # built alone
    assert files_of(git, waves.root, "Task 2: Second thing") == [PLAN_PATH, "src/two.py"]
    assert git(waves.root, "show", "HEAD:src/three.py") == "alone = 3\n"
    [attributed] = [e for e in events if e["type"] == "wave_attributed"]
    assert attributed["strays"] == ["src/zz.py"] and attributed["out"] == {"3": "scope_violation"}
    assert git(waves.root, "show", f"{attributed['shelf']}:src/zz.py") == "zz = 1\n"  # kept in the wave's shelf
    assert shelves(git, waves.root) == [f"{SHELVES}/{run.id}/build.build.wave-1"]


def test_a_blocked_member_is_set_aside_and_the_other_commits(waves, git):
    run = begin_build(waves, plan_text(DEPENDS, count=4, files=FILES, groups=GROUPS))
    waves.script({WAVE: [wave_act(tasks=[report(2), report(3, status="blocked")])],
                  IMPL.format(n=3): [BLOCKED]})
    assert build(waves, run) == ("stopped", True)
    events = run.events.read()
    assert demoted(events) == [(3, "blocked")]
    assert "Task 2: Second thing" in subjects(git, waves.root)
    assert files_of(git, waves.root, "Task 2: Second thing") == [PLAN_PATH, "src/two.py"]
    assert "src/three.py" not in branch_files(git, waves.root)  # its wave work went back to the wave's start
    assert any(e["type"] == "step_parked" and e["key"].startswith("build/build/task-3/") for e in events)
    assert "Task 4: Fourth thing" in subjects(git, waves.root)  # the independent task still ran


def test_a_risky_task_never_joins_and_keeps_its_own_review(waves):
    waves.configure({**BIG, **WAVES, "effort": {"sizing": True}})
    run = begin_build(waves, wave_plan())
    waves.script({PLAN_SIZE.format(i=1): [plan_answer({1: {}, 2: {}, 3: {"risk": "yes"}})]})
    assert build(waves, run) == ("done", True)
    events = run.events.read()
    assert demoted(events) == [(3, "risk")]
    assert WAVE not in steps(waves.calls())
    assert {IMPL.format(n=3), REVIEW.format(n=3)} <= set(steps(waves.calls()))


def test_max_subagents_1_changes_nothing(flow_repo):
    flow_repo.configure(BIG)
    run = begin_build(flow_repo, wave_plan())
    assert build(flow_repo, run) == ("done", True)
    events = run.events.read()
    assert not [e for e in events if e["type"] in ("wave_started", "parallel_demoted", "parallel_candidates")]
    assert steps(flow_repo.calls()) == [IMPL.format(n=n) if part == "i" else REVIEW.format(n=n)
                                        for n in (1, 2, 3) for part in "ir"]


def test_codex_builds_every_task_alone(waves):
    run = waves.new_run({"module": "demo"})
    executor = waves.executor(run)
    phase = waves.cfg.phases["build"]
    step = next(s for s in phase.steps if s.id == "build")
    assert executor.waves_on(phase, step, {}) is True
    assert executor.waves_on(dataclasses.replace(phase, agent="codex"), step, {}) is False
    assert executor.waves_on(phase, step, {"active": True}) is False  # nor a small plan's shared session


def test_grouped_tasks_are_never_batched(waves):
    tasks = sources.parse_plan_text(wave_plan(count=4))
    assert sources.plan_batches(tasks, 4, 3) == [[1, 2, 3, 4]]
    assert sources.plan_batches(tasks, 4, 3, skip={2, 3}) == []  # 1 and 4 are no neighbours once 2 and 3 are out
    run = begin_build(waves, wave_plan(count=4))
    executor = waves.executor(run)
    phase = waves.cfg.phases["build"]
    step = dataclasses.replace(next(s for s in phase.steps if s.id == "build"), batch={"max_tasks": 4, "max_files": 3})
    assert executor._batches(phase, step, executor.worktree / PLAN_PATH, "build/build", {}) == {}
