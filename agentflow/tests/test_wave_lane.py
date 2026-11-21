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
                               commit_of, files_of, needs_human, pairs, plan_text, review, sessions_of, shelves, steps,
                               subjects)
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
RWAVE = "build/build/wave-1/review"
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


def review_wave_act(findings: dict[int, list[dict]] | None = None) -> dict:
    """The review wave's dispatch: one reviewer per member it reports on, and each member's findings in `tasks`."""
    findings = {2: [], 3: []} if findings is None else findings
    return {"parallel_subagents": [{"description": f"Review Task {n}: {TITLES[n]}"} for n in findings],
            "result": {"status": "done", "summary": "reviewed the wave", "findings": [],
                       "tasks": [{"n": n, "findings": found} for n, found in findings.items()]}}


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
    waves.script({WAVE: [wave_act()], RWAVE: [review_wave_act()]})
    assert build(waves, run) == ("done", True)
    calls = waves.calls()
    assert steps(calls) == [IMPL.format(n=1), REVIEW.format(n=1), WAVE, RWAVE]  # one review wave, no per-task review
    events = run.events.read()
    builders = [e for e in events if e["type"] in ("subagent_started", "subagent_finished")
                and e["description"].startswith("Task ")]  # the review wave's reviewers are `Review Task <n>: …`
    started = [e["seq"] for e in builders if e["type"] == "subagent_started"]
    finished = [e["seq"] for e in builders if e["type"] == "subagent_finished"]
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


@pytest.mark.parametrize("held", [False, True])
def test_a_member_editing_a_parked_tasks_file_is_reverted(waves, git, monkeypatch, held):
    # Task 1 parks with its work shelved; tasks 2 and 3 do not wait on it and form the wave, whose third sub-agent
    # also writes the parked task's file (its report does not name it). `held`: the park records its paths as held
    # (`step_parked.paths`, as an older engine's park did), so they are the parked task's to commit.
    run = begin_build(waves, plan_text({1: "none", 2: "none", 3: "none"}, count=3, files=FILES, groups=GROUPS))
    if held:
        emit = run.emit

        def recording_paths(etype, **data):
            if etype == "step_parked":
                data["paths"] = list(data.get("shelved") or [])
            return emit(etype, **data)

        monkeypatch.setattr(run, "emit", recording_paths)
    intruder = sub(3, write={"src/three.py": "n = 3\n", "src/one.py": "not yours\n"})
    waves.script({IMPL.format(n=1): [{"write": {"src/one.py": "one = 1\n"}, **BLOCKED}],
                  WAVE: [wave_act(subs=[sub(2), intruder])]})
    assert build(waves, run) == ("stopped", True)  # the parked task is the run's wait once nothing else can run
    events = run.events.read()
    (parked,) = [e for e in events if e["type"] == "step_parked"]
    assert parked["key"] == IMPL.format(n=1) and parked["shelved"] == ["src/one.py"]
    assert scope_violations(events) == [(None, "src/one.py")]
    assert not (waves.root / "src/one.py").exists()  # back as the wave's start had it: the parked task's state
    assert "src/one.py" not in branch_files(git, waves.root)  # in no member's commit
    assert files_of(git, waves.root, "Task 2: Second thing") == [PLAN_PATH, "src/two.py"]
    assert files_of(git, waves.root, "Task 3: Third thing") == [PLAN_PATH, "src/three.py"]
    assert git(waves.root, "show", f"{parked['shelf']}:src/one.py") == "one = 1\n"  # its own work, untouched


# --------------------------------------------------------------------------- the review wave (Task 8)


F1 = {"severity": "blocking", "file": "src/three.py", "line": 1, "text": "F1: three forgets the empty case"}


@pytest.fixture
def notified(monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr("agentflow.hitl.notify", lambda cfg, title, message, run=None: seen.append(message))
    return seen


def wave_reviews(events: list[dict]) -> list[dict]:
    """The round-1 `review_result`s the review wave recorded for its members."""
    return [e for e in events if e["type"] == "review_result" and e.get("wave") == 1]


def prompt_of(calls: list[dict], key: str) -> str:
    """The prompt of the last call of step `key`."""
    return [c for c in calls if c["step"] == key][-1]["argv"][-1]


def start_head(events: list[dict]) -> str:
    return next(e["start_head"] for e in events if e["type"] == "wave_started")


def test_one_review_wave_reviews_both_members(waves, git):
    run = begin_build(waves, wave_plan())
    waves.script({WAVE: [wave_act()], RWAVE: [review_wave_act()]})
    assert build(waves, run) == ("done", True)
    calls, events = waves.calls(), run.events.read()
    assert RWAVE in steps(calls) and REVIEW.format(n=2) not in steps(calls) and REVIEW.format(n=3) not in steps(calls)
    assert [(e["key"], e["round"], e["blocking"]) for e in wave_reviews(events)] == [
        (REVIEW.format(n=2), 1, 0), (REVIEW.format(n=3), 1, 0)]
    started = [e["description"] for e in events if e["type"] == "subagent_started" and e["description"].startswith("Review")]
    assert started == ["Review Task 2: Second thing", "Review Task 3: Third thing"]
    [effort] = [e for e in events if e["type"] == "effort_decided" and e["key"] == RWAVE]
    assert effort["source"] == "wave"
    prompt = prompt_of(calls, RWAVE)
    cards = run.agent_dir / "wave" / "wave-1"
    assert f"- Review Task 2: Second thing: `{cards / 'review-2.md'}`" in prompt and "{wave_cards}" not in prompt
    card = (cards / "review-2.md").read_text(encoding="utf-8")
    assert "Task 2 test" in card and "Task 3 test" not in card  # the member's own task text
    assert f"git diff {start_head(events)} -- src/two.py" in card and "{task_n}" not in card
    assert "## When you finish" not in card and '{"n": 2, "findings"' in card  # it reports in its final message
    assert subjects(git, waves.root)[:2] == ["Task 3: Third thing", "Task 2: Second thing"]


def test_a_blocking_finding_reopens_only_its_member(waves, git):
    run = begin_build(waves, wave_plan())
    fix = {"write": {"src/three.py": "n = 3  # fixed\n"}, "result": {"status": "done", "summary": "fixed F1"}}
    waves.script({WAVE: [wave_act()], RWAVE: [review_wave_act({2: [], 3: [F1]})], IMPL.format(n=3): [fix]})
    assert build(waves, run) == ("done", True)
    calls = waves.calls()
    # the fix in a fresh single-task session, then that member's own re-review
    assert steps(calls) == [IMPL.format(n=1), REVIEW.format(n=1), WAVE, RWAVE, IMPL.format(n=3), REVIEW.format(n=3)]
    fix_argv = [c for c in calls if c["step"] == IMPL.format(n=3)][-1]["argv"]
    assert "--resume" not in fix_argv and "F1" in fix_argv[-1]
    assert "F1" in prompt_of(calls, REVIEW.format(n=3))  # the re-review checks what blocked
    results = [(e["key"], e["round"], e["blocking"]) for e in run.events.read() if e["type"] == "review_result"]
    assert results[1:] == [(REVIEW.format(n=2), 1, 0), (REVIEW.format(n=3), 1, 1), (REVIEW.format(n=3), 2, 0)]
    assert subjects(git, waves.root)[:2] == ["Task 3: Third thing", "Task 2: Second thing"]
    assert git(waves.root, "show", "HEAD:src/three.py") == "n = 3  # fixed\n"


def test_a_members_review_range_holds_only_its_files(waves, git):
    run = begin_build(waves, wave_plan())
    waves.script({WAVE: [wave_act()], RWAVE: [review_wave_act({2: [], 3: [F1]})]})
    assert build(waves, run) == ("done", True)
    sha = start_head(run.events.read())
    prompt = prompt_of(waves.calls(), REVIEW.format(n=3))  # the re-review, after Task 2 committed on the wave's start
    assert f"git diff {sha} -- src/three.py" in prompt
    assert f"`{sha}..worktree, limited to src/three.py`" in prompt
    card = (run.agent_dir / "wave" / "wave-1" / "review-3.md").read_text(encoding="utf-8")
    assert f"git diff {sha} -- src/three.py" in card and "src/two.py" not in card


@pytest.mark.parametrize(("fixer", "other"), [(3, 2), (2, 3)])
def test_a_member_fix_that_strays_is_reverted(waves, git, fixer, other):
    # `fixer`'s single-task fix also edits `other`'s file: 2's is committed by then, 3's is not yet
    run = begin_build(waves, wave_plan())
    finding = {**F1, "file": FILES[fixer][0]}
    fix = {"write": {FILES[fixer][0]: f"n = {fixer}  # fixed\n", FILES[other][0]: "not yours\n"},
           "result": {"status": "done", "summary": "fixed it"}}
    waves.script({WAVE: [wave_act()], RWAVE: [review_wave_act({2: [], 3: []} | {fixer: [finding]})],
                  IMPL.format(n=fixer): [fix]})
    assert build(waves, run) == ("done", True)
    events = run.events.read()
    assert scope_violations(events)[-1] == (fixer, FILES[other][0])
    [violation] = [e for e in events if e["type"] == "scope_violation"]
    assert violation["key"] == f"build/build/task-{fixer}" and violation["wave"] == 1
    assert files_of(git, waves.root, "Task 2: Second thing") == [PLAN_PATH, "src/two.py"]
    assert files_of(git, waves.root, "Task 3: Third thing") == [PLAN_PATH, "src/three.py"]
    assert git(waves.root, "show", f"HEAD:{FILES[other][0]}") == f"n = {other}\n"  # the wave's version
    assert git(waves.root, "show", f"HEAD:{FILES[fixer][0]}") == f"n = {fixer}  # fixed\n"
    assert git(waves.root, "status", "--porcelain") == ""


def test_a_member_fix_that_strays_and_stops_is_reverted_before_it_parks(waves, git):
    run = begin_build(waves, wave_plan())
    fix = {"write": {"src/two.py": "n = 2  # half\n", "src/three.py": "not yours\n"}, **BLOCKED}
    waves.script({WAVE: [wave_act()], RWAVE: [review_wave_act({2: [{**F1, "file": "src/two.py"}], 3: []})],
                  IMPL.format(n=2): [fix]})
    assert build(waves, run) == ("stopped", True)  # task 2 parked, then its stop is the run's wait
    events = run.events.read()
    assert scope_violations(events) == [(2, "src/three.py")]
    assert any(e["type"] == "step_parked" and e["key"] == IMPL.format(n=2) for e in events)
    assert files_of(git, waves.root, "Task 3: Third thing") == [PLAN_PATH, "src/three.py"]
    assert git(waves.root, "show", "HEAD:src/three.py") == "n = 3\n"  # the wave's version, not the stray


def test_an_unparked_member_fix_keeps_a_later_members_commit(waves, git):
    # Task 2's fix stops and parks; Task 3 fixes and commits; Task 2's fix then resumes on the moved HEAD and finishes.
    run = begin_build(waves, wave_plan())
    half = {"write": {"src/two.py": "n = 2  # half\n"}, **BLOCKED}
    fixed2 = {"write": {"src/two.py": "n = 2  # fixed\n"}, "result": {"status": "done", "summary": "fixed 2"}}
    fixed3 = {"write": {"src/three.py": "n = 3  # fixed\n"}, "result": {"status": "done", "summary": "fixed 3"}}
    waves.script({WAVE: [wave_act()],
                  RWAVE: [review_wave_act({2: [{**F1, "file": "src/two.py"}], 3: [F1]})],
                  IMPL.format(n=2): [half, fixed2], IMPL.format(n=3): [fixed3]})
    assert build(waves, run) == ("stopped", True)
    assert "Task 3: Third thing" in subjects(git, waves.root)
    approve(run, run.refresh()["waiting"]["key"], by="me")
    assert build(waves, run) == ("done", True)
    assert scope_violations(run.events.read()) == []  # the runner's own commit is no stray of task 2's
    assert git(waves.root, "show", "HEAD:src/three.py") == "n = 3  # fixed\n"
    assert git(waves.root, "show", "HEAD:src/two.py") == "n = 2  # fixed\n"
    plan = git(waves.root, "show", f"HEAD:{PLAN_PATH}")
    assert "- [x] **Step 1: Task 3 test**" in plan and "- [x] **Step 1: Task 2 test**" in plan
    assert files_of(git, waves.root, "Task 2: Second thing") == [PLAN_PATH, "src/two.py"]
    assert git(waves.root, "status", "--porcelain") == ""


def test_a_persons_edit_at_a_member_fix_stop_is_kept(waves, git):
    waves.configure({**BIG, **WAVES, "hitl": {"park_independent": False}})
    run = begin_build(waves, wave_plan())
    half = {"write": {"src/two.py": "n = 2  # half\n"}, **BLOCKED}
    fixed = {"write": {"src/two.py": "n = 2  # fixed\n"}, "result": {"status": "done", "summary": "fixed 2"}}
    waves.script({WAVE: [wave_act()], RWAVE: [review_wave_act({2: [{**F1, "file": "src/two.py"}], 3: []})],
                  IMPL.format(n=2): [half, fixed]})
    assert build(waves, run) == ("stopped", True)
    waiting = run.refresh()["waiting"]
    assert waiting["key"] == IMPL.format(n=2)
    (waves.root / "docs" / "note.md").write_text("a person's note\n", encoding="utf-8")  # edited at the stop
    approve(run, waiting["key"], by="me")
    assert build(waves, run) == ("done", True)
    assert scope_violations(run.events.read()) == []
    assert git(waves.root, "show", "HEAD:docs/note.md") == "a person's note\n"
    rebased = waves.events(run, "fix_rebased")
    assert [e["key"] for e in rebased] == [IMPL.format(n=2)]


def test_a_missing_reviewer_report_falls_back_to_its_own_review(waves):
    run = begin_build(waves, wave_plan())
    waves.script({WAVE: [wave_act()], RWAVE: [review_wave_act({2: []})]})
    assert build(waves, run) == ("done", True)
    calls = steps(waves.calls())
    assert calls[-2:] == [RWAVE, REVIEW.format(n=3)] and REVIEW.format(n=2) not in calls
    assert [e["key"] for e in wave_reviews(run.events.read())] == [REVIEW.format(n=2)]
    assert "-- src/three.py" in prompt_of(waves.calls(), REVIEW.format(n=3))


def test_a_review_wave_that_stops_is_dropped(waves, notified):
    run = begin_build(waves, wave_plan())
    waves.script({WAVE: [wave_act()], RWAVE: [{"result": {"status": "blocked", "summary": "no reviewers"}}]})
    assert build(waves, run) == ("done", True)
    [dropped] = waves.events(run, "review_wave_dropped")
    assert (dropped["key"], dropped["wave"], dropped["reason"]) == ("build/build/wave-1", 1, "blocked")
    assert run.state["steps"][RWAVE]["status"] == "skipped" and run.state["waiting"] is None
    assert {REVIEW.format(n=2), REVIEW.format(n=3)} <= set(steps(waves.calls()))  # each member is reviewed alone
    assert wave_reviews(run.events.read()) == [] and notified == []


def test_a_review_wave_guard_stop_stays_the_runs_wait(waves, notified):
    run = begin_build(waves, wave_plan())
    waves.script({WAVE: [wave_act()], RWAVE: [{**review_wave_act(), "write": {"src/two.py": "edited\n"}}]})
    assert build(waves, run) == ("stopped", True)
    waiting = run.refresh()["waiting"]
    assert waiting["kind"] == "guard_violation" and waiting["key"] == RWAVE
    assert waves.events(run, "review_wave_dropped") == [] and len(notified) == 1  # a person is told, once
    assert [e["key"] for e in waves.events(run, "hitl_notified")] == [RWAVE]


def test_a_runner_killed_in_the_review_wave_drops_it_on_resume(waves, monkeypatch):
    run = begin_build(waves, wave_plan())
    waves.script({WAVE: [wave_act()]})
    from agentflow.lanes import Executor

    real = Executor.run_agent_step

    def killed(self, phase, step, key, *args, **kwargs):  # the runner dies once the review wave's session started
        if key == RWAVE:
            self.run.emit("step_started", key=key, phase=phase.name)
            raise KeyboardInterrupt
        return real(self, phase, step, key, *args, **kwargs)

    monkeypatch.setattr(Executor, "run_agent_step", killed)
    with pytest.raises(KeyboardInterrupt):
        build(waves, run)
    monkeypatch.setattr(Executor, "run_agent_step", real)
    assert build(waves, run) == ("done", True)
    [dropped] = waves.events(run, "review_wave_dropped")
    assert dropped["reason"] == "interrupted"
    assert {REVIEW.format(n=2), REVIEW.format(n=3)} <= set(steps(waves.calls()))


def test_profile_L_keeps_a_review_session_per_task(waves):
    waves.configure({**BIG, **WAVES, "route": {"mode": "on", "lanes": [], "profiles": {"L": {"wave_review": False}}}})
    run = begin_build(waves, wave_plan())
    run.emit("route_profile", profile="L", applied=True, proposed_by="rule", upgraded_from=None, facts={}, reason="")
    waves.script({WAVE: [wave_act()]})
    assert build(waves, run) == ("done", True)
    calls = steps(waves.calls())
    assert RWAVE not in calls and {REVIEW.format(n=2), REVIEW.format(n=3)} <= set(calls)
    assert waves.events(run, "review_wave_dropped") == []  # never formed, so nothing to drop


# --------------------------------------------------------------------------- when a wave fails (Task 9)


W = "build/build/wave-1"


def dissolved(events: list[dict]) -> list[tuple[str, str]]:
    return [(e["key"], e["reason"]) for e in events if e["type"] == "wave_dissolved"]


def alone(n: int, text: str | None = None) -> dict:
    """Task `n` built on its own after its wave dissolved: it writes its file again, its own way."""
    return {"write": {FILES[n][0]: text or f"alone = {n}\n"}, "result": DONE_RESULT}


def once_each(git, root) -> bool:
    """Every commit subject of the branch appears once: nothing was committed twice."""
    found = subjects(git, root)
    return len(found) == len(set(found))


def assert_members_ran_alone(waves, git, run, reason: str) -> None:
    """The wave dissolved for `reason`, its work is kept in its shelf and in no commit, and each member was built alone."""
    events = run.events.read()
    assert dissolved(events) == [(W, reason)]
    [event] = [e for e in events if e["type"] == "wave_dissolved"]
    assert event["wave"] == 1 and event["tasks"] == [2, 3]
    assert [(n, r) for n, r in demoted(events) if r == "dissolved"] == [(2, "dissolved"), (3, "dissolved")]
    assert run.state["steps"][WAVE]["status"] == "skipped"
    waiting = run.state["waiting"]
    assert waiting is None or not waiting["key"].startswith(f"{W}/")
    assert {IMPL.format(n=2), IMPL.format(n=3)} <= set(steps(waves.calls()))
    assert files_of(git, waves.root, "Task 2: Second thing") == [PLAN_PATH, "src/two.py"]
    assert files_of(git, waves.root, "Task 3: Third thing") == [PLAN_PATH, "src/three.py"]
    assert git(waves.root, "show", "HEAD:src/two.py") == "alone = 2\n"  # the member's own build, not the wave's
    assert git(waves.root, "show", "HEAD:src/three.py") == "alone = 3\n"
    assert shelves(git, waves.root) == [f"{SHELVES}/{run.id}/build.build.wave-1"]
    assert event["shelf"] == git(waves.root, "rev-parse", f"{SHELVES}/{run.id}/build.build.wave-1").strip()
    assert once_each(git, waves.root) and git(waves.root, "status", "--porcelain") == ""


def test_a_wave_session_stop_dissolves_it_and_members_run_alone(waves, git, notified):
    # the wave's tree fails the build gate, and its own fix loop runs out: fix_attempts_exhausted
    waves.configure({**BIG, **WAVES, "gates": {"three": {"run": "test ! -f src/three.py || ! grep -q wave src/three.py"}},
                     "gate_sets": {"build": ["three"]}})
    run = begin_build(waves, wave_plan())
    waves.script({WAVE: [wave_act(subs=[sub(2), sub(3, write={"src/three.py": "wave = 3\n"})])],
                  IMPL.format(n=2): [alone(2)], IMPL.format(n=3): [alone(3)]})
    assert build(waves, run) == ("done", True)
    assert_members_ran_alone(waves, git, run, "fix_attempts_exhausted")
    shelf = next(e["shelf"] for e in run.events.read() if e["type"] == "wave_dissolved")
    assert git(waves.root, "show", f"{shelf}:src/three.py") == "wave = 3\n"  # the wave's work, kept
    assert notified == []  # settled by the runner: no person is needed


@pytest.mark.parametrize("wrong", [
    {"description": "Task 7: x"},  # a task that is not in the wave
    {"description": "Task 3: Third thing", "model": "opus"},  # another model than the wave's
    {"description": "Task 3: Third thing", "subagent_type": "Explore"},  # a custom type runs its own model
    {"description": "Task 3: Third thing", "error": True},  # a refused dispatch looks like a good one otherwise
])
def test_a_wrong_dispatch_dissolves_the_wave(waves, git, notified, wrong):  # Review Focus 4
    run = begin_build(waves, wave_plan())
    waves.script({WAVE: [wave_act(subs=[sub(2), {**wrong, "write": {"src/three.py": "n = 3\n"}}])],
                  IMPL.format(n=2): [alone(2)], IMPL.format(n=3): [alone(3)]})
    assert build(waves, run) == ("done", True)
    events = run.events.read()
    [tripped] = [e for e in events if e["type"] == "guard_tripped" and e["key"] == WAVE]
    assert [v["kind"] for v in tripped["violations"]] == ["wave_dispatch"]
    assert_members_ran_alone(waves, git, run, "wave_dispatch")
    assert not [e for e in events if e["type"] == "wave_attributed"]  # no member's work was taken from the wave
    assert notified == []


def test_a_moved_head_keeps_the_wave_stop(waves, git, notified):
    run = begin_build(waves, wave_plan())
    waves.script({WAVE: [{**wave_act(), "write": {"src/two.py": "n = 2\n"}, "git_commit": "the wave committed"}],
                  IMPL.format(n=2): [alone(2)], IMPL.format(n=3): [alone(3)]})
    assert build(waves, run) == ("stopped", True)
    waiting = run.refresh()["waiting"]
    assert waiting["key"] == WAVE and waiting["kind"] == "guard_violation" and not dissolved(run.events.read())
    assert len(notified) == 1  # a wave's stop is raised quietly: the runner tells a person once
    assert build(waves, run) == ("stopped", True) and len(notified) == 1  # a later walk does not tell them again
    assert IMPL.format(n=2) not in steps(waves.calls())
    # A person undoes the commit (the work stays in the tree) and approves: the wave dissolves and its members run alone.
    git(waves.root, "reset", "-q", "HEAD~1")
    approve(run, WAVE, by="me")
    assert build(waves, run) == ("done", True)
    assert_members_ran_alone(waves, git, run, "guard_violation")
    assert "the wave committed" not in subjects(git, waves.root)
    assert len([c for c in waves.calls() if c["step"] == WAVE]) == 1  # never resumed


def test_a_moved_head_found_on_resume_keeps_the_run_waiting(waves, git, notified, monkeypatch):
    # the runner died in the wave session, whose agent had committed: no stop was raised, so the dissolve raises one
    run = begin_build(waves, wave_plan())
    from agentflow.lanes import Executor

    real = Executor.run_agent_step

    def killed(self, phase, step, key, *args, **kwargs):
        if key == WAVE:
            self.run.emit("step_started", key=key, phase=phase.name,
                          start_head=git(waves.root, "rev-parse", "HEAD").strip())
            (waves.root / "src").mkdir(exist_ok=True)
            (waves.root / "src/two.py").write_text("n = 2\n", encoding="utf-8")
            git(waves.root, "add", "-A")
            git(waves.root, "commit", "-q", "-m", "the dead session committed")
            raise KeyboardInterrupt
        return real(self, phase, step, key, *args, **kwargs)

    monkeypatch.setattr(Executor, "run_agent_step", killed)
    with pytest.raises(KeyboardInterrupt):
        build(waves, run)
    monkeypatch.setattr(Executor, "run_agent_step", real)
    assert build(waves, run) == ("stopped", True)
    waiting = run.refresh()["waiting"]
    assert waiting["key"] == WAVE and not dissolved(run.events.read()) and len(notified) == 1
    assert "HEAD" in waiting["summary"]


def test_a_runner_killed_mid_wave_dissolves_it_on_resume(waves, git, monkeypatch):  # Review Focus 1
    run = begin_build(waves, wave_plan())
    waves.script({IMPL.format(n=2): [alone(2)], IMPL.format(n=3): [alone(3)]})
    from agentflow import wave_lane
    from agentflow.lanes import Executor

    real = Executor.run_agent_step

    def killed(self, phase, step, key, *args, **kwargs):  # the runner dies while the wave's sub-agents write
        if key == WAVE:
            self.run.emit("step_started", key=key, phase=phase.name)
            (waves.root / "src").mkdir(exist_ok=True)
            (waves.root / "src/two.py").write_text("half = 2\n", encoding="utf-8")
            raise KeyboardInterrupt
        return real(self, phase, step, key, *args, **kwargs)

    monkeypatch.setattr(Executor, "run_agent_step", killed)
    with pytest.raises(KeyboardInterrupt):
        build(waves, run)
    monkeypatch.setattr(Executor, "run_agent_step", real)
    waited: list[tuple[str, int]] = []
    real_wait = wave_lane.wait_for_agents
    monkeypatch.setattr(wave_lane, "wait_for_agents",
                        lambda run, cfg, key, timeout_s: waited.append((key, timeout_s)) or real_wait(run, cfg, key, timeout_s))
    assert build(waves, run) == ("done", True)
    executor = waves.executor(run)
    assert waited == [(WAVE, executor._timeout_s(wave=True))]  # its agents are waited for before anything moves
    assert dissolved(run.events.read())[-1][1] == "interrupted"
    assert WAVE not in steps(waves.calls())  # the wave's session is never resumed
    assert_members_ran_alone(waves, git, run, "interrupted")
    shelf = next(e["shelf"] for e in run.events.read() if e["type"] == "wave_dissolved")
    assert git(waves.root, "show", f"{shelf}:src/two.py") == "half = 2\n"


def kill_once(monkeypatch, cls, name: str, before=None) -> None:
    """`cls.name` raises KeyboardInterrupt on its first call (after `before(self)`), then works as it did."""
    real = getattr(cls, name)
    state = {"killed": False}

    def dying(self, *args, **kwargs):
        if not state["killed"]:
            state["killed"] = True
            if before is not None:
                before(self)
            raise KeyboardInterrupt
        return real(self, *args, **kwargs)

    monkeypatch.setattr(cls, name, dying)


def test_a_runner_killed_before_attribution_attributes_on_resume(waves, git, monkeypatch):  # Review Focus 1
    from agentflow.wave_lane import WaveLane

    run = begin_build(waves, wave_plan())
    waves.script({WAVE: [wave_act()], RWAVE: [review_wave_act()]})
    kill_once(monkeypatch, WaveLane, "_attribute")  # the session is done, its attribution not yet made
    with pytest.raises(KeyboardInterrupt):
        build(waves, run)
    assert run.refresh()["steps"][WAVE]["status"] == "done"
    assert build(waves, run) == ("done", True)
    events = run.events.read()
    assert len([e for e in events if e["type"] == "wave_attributed"]) == 1 and not dissolved(events)
    assert len([c for c in waves.calls() if c["step"] == WAVE]) == 1
    assert subjects(git, waves.root)[:2] == ["Task 3: Third thing", "Task 2: Second thing"] and once_each(git, waves.root)


def test_a_runner_killed_part_way_through_attribution_reads_the_shelf_on_resume(waves, git, monkeypatch):
    # the shelf was written and a stray already restored, then the runner died before `wave_attributed`
    from agentflow.gitops import shelf_ref, shelve
    from agentflow.guards import snapshot
    from agentflow.wave_lane import WaveLane

    run = begin_build(waves, wave_plan())
    strays = sub(3, write={"src/three.py": "n = 3\n", "src/zz.py": "zz = 1\n"})
    waves.script({WAVE: [wave_act(subs=[sub(2), strays], tasks=[report(2), report(3, files=["src/three.py",
                                                                                         "src/zz.py"])])],
                  IMPL.format(n=3): [alone(3)]})

    def shelved_then_died(self):
        shelve(self.worktree, snapshot(self.worktree), shelf_ref(self.run.id, W), f"Shelve {W}")
        (self.worktree / "src/zz.py").unlink()  # as the restore would

    kill_once(monkeypatch, WaveLane, "_attribute", before=shelved_then_died)
    with pytest.raises(KeyboardInterrupt):
        build(waves, run)
    assert not [e for e in run.events.read() if e["type"] == "wave_attributed"]
    assert build(waves, run) == ("done", True)
    events = run.events.read()
    [attributed] = [e for e in events if e["type"] == "wave_attributed"]
    assert attributed["strays"] == ["src/zz.py"] and attributed["out"] == {"3": "scope_violation"}
    assert len([c for c in waves.calls() if c["step"] == WAVE]) == 1 and not dissolved(events)
    assert len([e for e in events if e["type"] == "wave_finished"]) == 1
    assert "src/zz.py" not in branch_files(git, waves.root) and not (waves.root / "src/zz.py").exists()
    assert files_of(git, waves.root, "Task 2: Second thing") == [PLAN_PATH, "src/two.py"]
    assert git(waves.root, "show", "HEAD:src/three.py") == "alone = 3\n" and once_each(git, waves.root)


def test_a_runner_killed_after_a_member_committed_finishes_the_wave_once(waves, git, monkeypatch):
    run = begin_build(waves, wave_plan())
    waves.script({WAVE: [wave_act()], RWAVE: [review_wave_act()]})
    emit = run.emit

    def dies_after_task_2(etype, **data):
        out = emit(etype, **data)
        if etype == "task_committed" and data.get("key") == "build/build/task-2":
            raise KeyboardInterrupt
        return out

    monkeypatch.setattr(run, "emit", dies_after_task_2)
    with pytest.raises(KeyboardInterrupt):
        build(waves, run)
    monkeypatch.setattr(run, "emit", emit)
    assert subjects(git, waves.root)[0] == "Task 2: Second thing"
    assert build(waves, run) == ("done", True)  # a fresh executor, as `agentflow resume` makes
    events = run.events.read()
    assert len([c for c in waves.calls() if c["step"] in (WAVE, RWAVE)]) == 2  # each wave session ran once
    assert len([e for e in events if e["type"] == "wave_attributed"]) == 1
    [finished] = [e for e in events if e["type"] == "wave_finished"]
    assert finished["committed"] == [2, 3] and not dissolved(events)
    assert subjects(git, waves.root)[:2] == ["Task 3: Third thing", "Task 2: Second thing"] and once_each(git, waves.root)
    assert files_of(git, waves.root, "Task 3: Third thing") == [PLAN_PATH, "src/three.py"]


def test_a_member_parked_at_review_shelves_only_its_own_files(waves, git):  # Review Focus 3
    run = begin_build(waves, wave_plan())
    two = {**F1, "file": "src/two.py", "text": "F2: two is wrong"}
    waves.script({WAVE: [wave_act()], RWAVE: [review_wave_act({2: [two], 3: []})],
                  REVIEW.format(n=2): [review(two), review(two)]})  # 2's review stays unresolved
    assert build(waves, run) == ("stopped", True)
    events = run.events.read()
    parked = next(e for e in events if e["type"] == "step_parked")
    assert parked["key"] == REVIEW.format(n=2) and parked["shelved"] == ["src/two.py"]
    assert git(waves.root, "show", f"{parked['shelf']}:src/two.py") == "n = 2\n"
    assert files_of(git, waves.root, "Task 3: Third thing") == [PLAN_PATH, "src/three.py"]
    assert git(waves.root, "show", "HEAD:src/three.py") == "n = 3\n"
    assert "Task 2: Second thing" not in subjects(git, waves.root)


def test_a_dissolved_wave_never_forms_again(waves, git):
    run = begin_build(waves, wave_plan())
    waves.script({WAVE: [wave_act(subs=[sub(2), {"description": "Task 7: x"}])],
                  IMPL.format(n=2): [{**needs_human("other")}, alone(2)], IMPL.format(n=3): [alone(3)]})
    assert build(waves, run) == ("stopped", True)  # 2, built alone, asks; 3 was built meanwhile
    assert answer(run, IMPL.format(n=2), {"q1": "A"}, by="me")
    assert build(waves, run) == ("done", True)  # a fresh executor walks the plan again
    events = run.events.read()
    assert len([e for e in events if e["type"] == "wave_started"]) == 1
    assert dissolved(events) == [(W, "wave_dispatch")]
    assert len([c for c in waves.calls() if c["step"] == WAVE]) == 1


def test_a_stray_left_by_the_wave_fix_dissolves_the_wave(waves, git):
    waves.configure({**BIG, **WAVES, "gates": {"three": {"run": "test ! -f src/three.py || grep -q fixed src/three.py"}},
                     "gate_sets": {"build": ["three"]}})
    run = begin_build(waves, wave_plan())
    fix = {"write": {"src/three.py": "fixed = 3\n", "src/zz.py": "zz = 1\n"},
           "result": {"status": "done", "summary": "fixed three", "tasks": [report(2), report(3)]}}
    waves.script({WAVE: [wave_act(), fix],
                  IMPL.format(n=2): [alone(2)], IMPL.format(n=3): [alone(3, "fixed = 3  # alone = 3\n")]})
    assert build(waves, run) == ("done", True)
    events = run.events.read()
    assert dissolved(events) == [(W, "scope_violation")]
    assert "src/zz.py" in [p for _, p in scope_violations(events)]
    assert not (waves.root / "src/zz.py").exists() and "src/zz.py" not in branch_files(git, waves.root)
    shelf = next(e["shelf"] for e in events if e["type"] == "wave_dissolved")
    assert git(waves.root, "show", f"{shelf}:src/zz.py") == "zz = 1\n"
    assert files_of(git, waves.root, "Task 2: Second thing") == [PLAN_PATH, "src/two.py"]
    assert git(waves.root, "show", "HEAD:src/two.py") == "alone = 2\n" and once_each(git, waves.root)


@pytest.mark.parametrize("wrong", [{"description": "Review Task 9: x"},
                                   {"description": "Review Task 3: Third thing", "subagent_type": "Explore"}])
def test_a_wrong_review_dispatch_drops_the_review_wave(waves, git, wrong):
    run = begin_build(waves, wave_plan())
    act = review_wave_act()
    act["parallel_subagents"] = [act["parallel_subagents"][0], wrong]
    waves.script({WAVE: [wave_act()], RWAVE: [act]})
    assert build(waves, run) == ("done", True)
    events = run.events.read()
    [tripped] = [e for e in events if e["type"] == "guard_tripped" and e["key"] == RWAVE]
    assert [v["kind"] for v in tripped["violations"]] == ["wave_dispatch"]
    [dropped] = waves.events(run, "review_wave_dropped")
    assert dropped["reason"] == "wave_dispatch" and wave_reviews(events) == []  # none of its findings count
    assert {REVIEW.format(n=2), REVIEW.format(n=3)} <= set(steps(waves.calls()))  # each member reviewed alone
    assert not dissolved(events) and subjects(git, waves.root)[:2] == ["Task 3: Third thing", "Task 2: Second thing"]


def test_the_review_wave_alone_may_start_reviewers(waves):
    # read-only review sessions get `--tools` without `Agent`; the review wave dispatches, so it gets `Agent`/`Task`
    run = begin_build(waves, wave_plan())
    waves.script({WAVE: [wave_act()], RWAVE: [review_wave_act({2: []})]})
    assert build(waves, run) == ("done", True)
    calls = waves.calls()
    wave_argv = [c for c in calls if c["step"] == RWAVE][-1]["argv"]
    member_argv = [c for c in calls if c["step"] == REVIEW.format(n=3)][-1]["argv"]
    assert ("--tools", "Read,Grep,Glob,Bash,Write,Agent,Task") in pairs(wave_argv)
    assert ("--tools", "Read,Grep,Glob,Bash,Write") in pairs(member_argv)
    assert ("--permission-mode", "dontAsk") in pairs(wave_argv)
    assert "--tools" not in [c for c in calls if c["step"] == WAVE][-1]["argv"]  # the build wave is not read-only
