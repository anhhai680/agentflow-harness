"""plan_view: a run's plan tasks joined with its state (dashboard slice U2)."""

import pytest

from agentflow.plan_view import _under, dependents, run_plan, waves
from agentflow.sources import parse_plan_text
from agentflow.tests.conftest import run_git

PLAN5 = """# P
### Task 1: One
- [x] done
### Task 2: Two
**Depends on:** Task 1
### Task 3: Three
**Depends on:** Task 2
### Task 4: Four
**Depends on:** Task 2
### Task 5: Five
**Depends on:** Task 1
"""


@pytest.fixture
def building(spec_run):
    """spec_run (U1), plus the plan docs/plans/2026-11-16-crm.md, task 1 committed and task 2 blocked."""
    spec_run.commit("plan/plan", write={"docs/plans/2026-11-16-crm.md": PLAN5})
    run, p = spec_run.run, "build/build"
    run.emit("phase_started", phase="build", start_sha=spec_run.head())
    run.emit("step_started", key=f"{p}/task-1/implement")
    run.emit("agent_started", key=f"{p}/task-1/implement", session_id="s1", pid=1, model="sonnet", effort="high")
    run.emit("agent_exited", session_id="s1", exit_code=0, cost=1.25)
    sha = spec_run.commit(f"{p}/task-1", write={"one.py": "x = 1\n"})
    run.emit("task_committed", key=f"{p}/task-1", n=1, title="One", sha=sha, round=0)
    run.emit("step_started", key=f"{p}/task-2/implement")
    run.emit("hitl_raised", kind="blocked", key=f"{p}/task-2/implement/resolve-2", summary="floor_guard fails")
    return spec_run


def test_a_blocked_task_names_what_it_holds_up(building, flow_repo):  # spec Verification U2
    plan = run_plan(building.run, building.repo, flow_repo.cfg)
    assert plan["path"] == "docs/plans/2026-11-16-crm.md" and plan["prefix"] == "build/build" and plan["started"]
    by = {t["n"]: t for t in plan["tasks"]}
    assert [by[n]["status"] for n in range(1, 6)] == ["committed", "blocked", "waiting", "waiting", "independent"]
    assert by[2]["blocks"] == [3, 4] and by[2]["stop"]["summary"] == "floor_guard fails"
    assert by[3]["blocked_by"] == [2] and by[5]["blocked_by"] == []
    assert by[1]["commit"] and by[1]["cost"] == 1.25 and by[1]["attempts"] == 1
    assert {n: t["wave"] for n, t in by.items()} == {1: 0, 2: 1, 3: 2, 4: 2, 5: 1}
    assert plan["summary"] == {"total": 5, "committed": 1, "blocked": 1, "parked": 0, "running": 0, "failed": 0,
                               "waiting": 2, "independent": 1, "deferred": 0}


def test_a_stop_on_task_10_never_marks_task_1():  # Review Focus 1
    assert _under("build/build/task-1", "build/build/task-1")
    assert _under("build/build/task-1/implement/resolve-2", "build/build/task-1")
    assert not _under("build/build/task-10/implement", "build/build/task-1")
    assert not _under("build/build/task-1x", "build/build/task-1")


def test_a_plan_without_depends_lines_is_a_chain():  # Review Focus 2
    tasks = parse_plan_text("".join(f"### Task {n}: T{n}\n" for n in range(1, 7)))
    assert dependents(tasks, 3) == [4, 5, 6] and waves(tasks) == {n: n - 1 for n in range(1, 7)}


def test_unknown_dependencies_and_cycles_terminate():  # Review Focus 3
    tasks = parse_plan_text("### Task 1: A\n**Depends on:** none\n### Task 2: B\n**Depends on:** Task 3\n"
                            "### Task 3: C\n**Depends on:** Task 2\n### Task 4: D\n**Depends on:** Task 9, Task 1\n"
                            "### Task 5: E\n**Depends on:** Task 3\n")
    assert waves(tasks) == {1: 0, 2: None, 3: None, 4: 1, 5: None}
    assert dependents(tasks, 2) == [3, 5] and dependents(tasks, 9) == [4]


def test_a_run_that_started_at_build_finds_its_plan_at_the_branch_head(flow_repo, tmp_path):
    plan = flow_repo.root / "docs" / "plans" / "2026-11-16-crm.md"
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan.write_text(PLAN5, encoding="utf-8")
    run_git(flow_repo.root, "add", "-A")
    run_git(flow_repo.root, "commit", "-q", "-m", "plan")
    run_git(flow_repo.root, "worktree", "add", "-q", "-b", "flow/feature/crm", str(tmp_path / "wt"))
    run = flow_repo.new_run()
    run.emit("run_updated", worktree=str(tmp_path / "wt"), base_sha=flow_repo.head(), branch="flow/feature/crm")
    run.emit("phase_started", phase="build", start_sha=flow_repo.head())
    assert run_plan(run, flow_repo.root, flow_repo.cfg)["path"] == "docs/plans/2026-11-16-crm.md"


def test_no_plan_is_an_empty_list_not_an_error(flow_repo):
    run = flow_repo.new_run()
    assert run_plan(run, flow_repo.root, flow_repo.cfg)["tasks"] == []


LADDER = """# P
### Task 1: Running live
**Depends on:** none
- [ ] a
### Task 2: Failed
**Depends on:** none
- [ ] a
### Task 3: Deferred
**Depends on:** none
- [ ] a
> **Deferred:** later
### Task 4: Done before
**Depends on:** none
- [x] a
### Task 5: Pending
**Depends on:** none
- [ ] a
### Task 6: Redone after its commit
**Depends on:** none
- [ ] a
### Task 7: Committed
**Depends on:** none
- [ ] a
### Task 8: Parked
**Depends on:** none
- [ ] a
"""


def _ladder(spec_run, flow_repo, park):
    spec_run.commit("plan/plan", write={"docs/plans/2026-11-16-crm.md": LADDER})
    run, p = spec_run.run, "build/build"
    run.emit("phase_started", phase="build", start_sha=spec_run.head())
    run.emit("step_started", key=f"{p}/task-1/implement")
    run.emit("agent_started", key=f"{p}/task-1/implement", session_id="s1", pid=2, model="sonnet", effort="high")
    run.emit("step_started", key=f"{p}/task-2/implement")
    run.emit("step_finished", key=f"{p}/task-2/implement", status="failed")
    for n in (6, 7):
        run.emit("step_started", key=f"{p}/task-{n}/implement")
        run.emit("step_finished", key=f"{p}/task-{n}/implement", status="done")
        run.emit("task_committed", key=f"{p}/task-{n}", n=n, title="T", sha="a" * 40, round=0)
    run.emit("step_started", key=f"{p}/task-6/implement")  # a refine round runs it again
    if park:
        run.emit("step_started", key=f"{p}/task-8/implement")
        run.emit("step_parked", key=f"{p}/task-8/implement/resolve-1", kind="needs_human", summary="which db?")
    plan = run_plan(run, spec_run.repo, flow_repo.cfg)
    return {t["n"]: t for t in plan["tasks"]}


def test_the_status_ladder(spec_run, flow_repo):
    by = _ladder(spec_run, flow_repo, park=False)
    assert [by[n]["status"] for n in range(1, 8)] == [
        "running", "failed", "deferred", "done_before", "pending", "running", "committed"]


def test_a_parked_task_carries_its_stop(spec_run, flow_repo):
    by = _ladder(spec_run, flow_repo, park=True)
    assert by[8]["status"] == "parked" and by[8]["stop"]["summary"] == "which db?"
