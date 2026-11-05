"""`agentflow next` (the next-step rules, in order) and `agentflow queue` (the capability-map build order)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from agentflow import hitl, module_queue
from agentflow.flow import main
from agentflow.module_queue import NextAnswer, QueueBusy, QueueState, StartFailed, next_step, queue_tick

from conftest import ENGINE, run_git

GH_BIN = ENGINE / "tests" / "bin"
APPROVED_SPEC = "# Spec: {m}\n\n**Status: approved** 2026-10-28\n"
MAP = """# Capability map

| Module id | Responsibility | Depends on | Spec |
|---|---|---|---|
| `platform` | base | — | SPEC-platform |
| `staff-auth` | auth | platform | SPEC-staff-auth |
| `crm` | crm | platform | SPEC-crm |
| `settings` | settings | staff-auth | SPEC-settings |
"""
PLAN_OPEN = "# Plan\n\n### Task 1: One\n\n- [x] done\n\n### Task 2: Two\n\n- [ ] open\n"
PLAN_DONE = "# Plan\n\n### Task 1: One\n\n- [x] done\n"
PLAN_DEFERRED = (PLAN_DONE + "\n### Task 2: Later\n\n> **Deferred (SPEC-x C1, decided 2026-11-03).** "
                 "Needs a module not merged yet.\n\n**Depends on:** Task 1\n")


def write(root: Path, rel: str, text: str) -> None:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


@pytest.fixture
def fr(flow_repo, monkeypatch, tmp_path):
    """`flow_repo` with the fake `gh` first on PATH (log in `gh.log`, nothing merged)."""
    monkeypatch.setenv("PATH", f"{GH_BIN}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_GH_LOG", str(tmp_path / "gh.log"))
    monkeypatch.delenv("FAKE_GH_FAIL", raising=False)
    monkeypatch.delenv("FAKE_GH_JSON", raising=False)
    return flow_repo


def commit_all(root: Path) -> None:
    run_git(root, "add", "-A")
    run_git(root, "commit", "-q", "-m", "fixtures")


# --------------------------------------------------------------------------- next


def test_next_unchecked_plan(fr):
    write(fr.root, "docs/plans/2026-10-28-crm.md", PLAN_OPEN)
    answer = next_step(fr.cfg, fr.root)
    assert (answer.lane, answer.stage) == ("feature", "Build")
    assert answer.next == "executing-plans"  # two tasks: the small-plan route
    assert any("1 of 2 tasks unchecked" in line for line in answer.evidence)


def test_next_does_not_send_anyone_to_build_a_deferred_task(fr):
    write(fr.root, "docs/plans/2026-10-28-crm.md", PLAN_DEFERRED)
    answer = next_step(fr.cfg, fr.root)
    assert (answer.lane, answer.stage) == ("feature", "Verify")  # the plan is finished; its branch is not merged


def test_next_big_plan_uses_subagents(fr):
    tasks = "".join(f"### Task {n}: T{n}\n\n- [ ] s\n\n" for n in range(1, 5))
    write(fr.root, "docs/plans/2026-10-28-crm.md", "# Plan\n\n" + tasks)
    assert next_step(fr.cfg, fr.root).next == "subagent-driven-development"


def test_next_newest_plan_wins(fr):
    write(fr.root, "docs/plans/2026-10-01-old.md", PLAN_OPEN)
    write(fr.root, "docs/plans/2026-10-28-new.md", PLAN_DONE)
    answer = next_step(fr.cfg, fr.root)
    assert (answer.lane, answer.stage) == ("feature", "Verify")  # the old plan's open task is not "in flight"


def test_next_checked_plan_branch_not_merged(fr):
    write(fr.root, "docs/plans/2026-10-28-crm.md", PLAN_DONE)
    answer = next_step(fr.cfg, fr.root)
    assert (answer.lane, answer.stage) == ("feature", "Verify")
    assert answer.next == "verification-before-completion"
    assert "finishing-a-development-branch" in answer.reason
    assert any("flow/feature/crm" in line for line in answer.evidence)


def test_next_checked_plan_branch_merged_falls_through(fr, monkeypatch):
    write(fr.root, "docs/plans/2026-10-28-crm.md", PLAN_DONE)
    monkeypatch.setenv("FAKE_GH_JSON", json.dumps([{"number": 1, "mergedAt": "2026-10-29"}]))
    commit_all(fr.root)
    answer = next_step(fr.cfg, fr.root)
    assert answer.lane == "none"
    assert "grilling" in answer.next


def test_next_checked_plan_gh_failure_still_verify(fr, monkeypatch):
    write(fr.root, "docs/plans/2026-10-28-crm.md", PLAN_DONE)
    monkeypatch.setenv("FAKE_GH_FAIL", "1")
    answer = next_step(fr.cfg, fr.root)
    assert (answer.lane, answer.stage) == ("feature", "Verify")
    assert any("could not check" in line for line in answer.evidence)


def test_next_approved_spec_without_plan(fr):
    write(fr.root, "docs/specs/SPEC-crm.md", APPROVED_SPEC.format(m="crm"))
    commit_all(fr.root)
    answer = next_step(fr.cfg, fr.root)
    assert (answer.lane, answer.stage) == ("feature", "Plan")
    assert answer.next == "writing-plans"
    assert any("SPEC-crm.md" in line for line in answer.evidence)


def test_next_spec_with_plan_or_pending_is_skipped(fr, monkeypatch):
    write(fr.root, "docs/specs/SPEC-crm.md", APPROVED_SPEC.format(m="crm"))
    write(fr.root, "docs/specs/SPEC-settings.md", "# Spec\n\n**Status: draft**, pending approval\n")
    write(fr.root, "docs/plans/2026-10-20-crm.md", PLAN_DONE)
    commit_all(fr.root)
    monkeypatch.setenv("FAKE_GH_JSON", json.dumps([{"number": 1}]))  # crm's plan is done and merged
    assert next_step(fr.cfg, fr.root).lane == "none"


def test_next_doc_only_changes(fr):
    write(fr.root, "docs/ideas/x.md", "# idea\n")
    write(fr.root, "GLOSSARY.md", "# terms\n")
    answer = next_step(fr.cfg, fr.root)
    assert answer.lane == "design-doc"
    assert any("docs/ideas/x.md" in line for line in answer.evidence)


def test_next_mixed_changes_are_not_doc_only(fr):
    write(fr.root, "docs/ideas/x.md", "# idea\n")
    write(fr.root, "src/other.py", "x = 1\n")
    assert next_step(fr.cfg, fr.root).lane == "none"


def test_next_eval_paths(fr):
    write(fr.root, "src/tivacare/knowledge/retrieval/x.py", "x = 1\n")
    answer = next_step(fr.cfg, fr.root)
    assert answer.lane == "eval"
    assert answer.stage == "Verify"
    assert any("src/tivacare/knowledge/retrieval/x.py" in line for line in answer.evidence)


def test_next_eval_beats_open_plan(fr):
    write(fr.root, "docs/plans/2026-10-28-crm.md", PLAN_OPEN)
    write(fr.root, "src/tivacare/agent/policy.py", "x = 1\n")
    assert next_step(fr.cfg, fr.root).lane == "eval"


def test_next_nothing_in_flight(fr):
    answer = next_step(fr.cfg, fr.root)
    assert answer.lane == "none"
    assert answer.evidence


def test_next_security_line_when_overlay_matches(fr):
    write(fr.root, "docs/plans/2026-10-28-crm.md", PLAN_OPEN)
    write(fr.root, "src/tivacare/staff_auth/guard.py", "x = 1\n")
    answer = next_step(fr.cfg, fr.root)
    assert answer.security
    text = module_queue.format_answer(answer)
    assert text.splitlines()[3].startswith("Security:")
    assert "security-and-hardening" in text and "/doubt-driven-development" in text


def test_next_security_line_from_plan_keywords(fr):
    write(fr.root, "docs/plans/2026-10-28-crm.md", PLAN_OPEN + "\nThe customer_code claim is injected.\n")
    assert next_step(fr.cfg, fr.root).security


def test_next_no_security_line_otherwise(fr):
    write(fr.root, "docs/plans/2026-10-28-crm.md", PLAN_OPEN)
    answer = next_step(fr.cfg, fr.root)
    assert answer.security == ""
    assert len(module_queue.format_answer(answer).splitlines()) == 3


def test_format_answer_is_three_lines():
    answer = NextAnswer("feature", "Build", ["a fact", "another"], "executing-plans", "continue the plan")
    assert module_queue.format_answer(answer).splitlines() == [
        "Lane: feature · Stage: Build",
        "Evidence: a fact; another",
        "Next: executing-plans — continue the plan",
    ]


def test_cmd_next_prints_the_answer(fr, capsys):
    write(fr.root, "docs/ideas/x.md", "# idea\n")
    assert main(["next"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "Lane: design-doc · Stage: Define"
    assert out[1].startswith("Evidence: ") and out[2].startswith("Next: ")


# --------------------------------------------------------------------------- queue


class Starter:
    """A `start` callable that records the modules it was asked for and creates their (running) runs."""

    def __init__(self, fr) -> None:
        self.fr = fr
        self.started: list[str] = []
        self.run_ids: dict[str, str] = {}

    def __call__(self, module: str) -> str:
        from agentflow.state import Run

        self.started.append(module)
        run = Run.create(self.fr.cfg, self.fr.root, "feature", {"module": module})
        self.run_ids[module] = run.id
        return run.id


@pytest.fixture
def qrepo(fr):
    write(fr.root, "docs/specs/CAPABILITY-MAP.md", MAP)
    commit_all(fr.root)
    return fr


def merged(monkeypatch) -> None:
    """Every `gh pr list --state merged` answers with one merged pull request."""
    monkeypatch.setenv("FAKE_GH_STATE", "MERGED")
    monkeypatch.setenv("FAKE_GH_JSON", json.dumps([{"number": 1, "mergedAt": "2026-10-29"}]))


def test_queue_starts_roots_then_dependents(qrepo, monkeypatch):
    go = Starter(qrepo)
    assert queue_tick(qrepo.cfg, qrepo.root, 2, go) == ["platform"]
    # platform has a recorded run and is not merged: nothing else is ready
    assert queue_tick(qrepo.cfg, qrepo.root, 2, go) == []
    write(qrepo.root, "docs/plans/2026-10-28-platform.md", PLAN_DONE)
    merged(monkeypatch)
    assert queue_tick(qrepo.cfg, qrepo.root, 2, go) == ["staff-auth", "crm"]
    assert go.started == ["platform", "staff-auth", "crm"]
    # settings waits for staff-auth, which is running
    assert queue_tick(qrepo.cfg, qrepo.root, 3, go) == []
    state = {m["id"]: m for m in QueueState.load(qrepo.root).modules}
    assert state["platform"]["status"] == "done"
    assert state["staff-auth"] == {
        "id": "staff-auth", "deps": ["platform"], "status": "running", "run_id": go.run_ids["staff-auth"],
    }
    assert state["settings"]["status"] == "backlog" and state["settings"]["run_id"] is None


def test_queue_respects_max_parallel(qrepo, monkeypatch):
    go = Starter(qrepo)
    write(qrepo.root, "docs/plans/2026-10-28-platform.md", PLAN_DONE)
    merged(monkeypatch)
    assert queue_tick(qrepo.cfg, qrepo.root, 1, go) == ["staff-auth"]
    assert queue_tick(qrepo.cfg, qrepo.root, 1, go) == []  # the one slot is taken
    assert queue_tick(qrepo.cfg, qrepo.root, 2, go) == ["crm"]


def test_queue_never_starts_module_with_unfinished_deps(qrepo, monkeypatch):
    go = Starter(qrepo)
    # a checked plan alone is not done: the branch is not merged
    write(qrepo.root, "docs/plans/2026-10-28-platform.md", PLAN_DONE)
    assert queue_tick(qrepo.cfg, qrepo.root, 5, go) == ["platform"]
    assert go.started == ["platform"]
    # merged but the plan still has an open task: not done either
    write(qrepo.root, "docs/plans/2026-10-28-platform.md", PLAN_OPEN)
    merged(monkeypatch)
    assert queue_tick(qrepo.cfg, qrepo.root, 5, go) == []


def test_queue_a_deferred_task_does_not_hold_its_module_open(qrepo, monkeypatch):
    write(qrepo.root, "docs/plans/2026-10-28-platform.md", PLAN_DEFERRED)
    merged(monkeypatch)
    go = Starter(qrepo)
    assert queue_tick(qrepo.cfg, qrepo.root, 5, go) == ["staff-auth", "crm"]
    assert {m["id"]: m["status"] for m in QueueState.load(qrepo.root).modules}["platform"] == "done"


def _feature_run(qrepo, module: str, events: list[tuple[str, dict]]):
    from agentflow.state import Run

    run = Run.create(qrepo.cfg, qrepo.root, "feature", {"module": module})
    for etype, fields in events:
        run.emit(etype, **fields)
    return run


def test_queue_waiting_run_does_not_block(qrepo, monkeypatch):
    write(qrepo.root, "docs/plans/2026-10-28-platform.md", PLAN_DONE)
    merged(monkeypatch)
    waiting = _feature_run(qrepo, "staff-auth", [("hitl_raised", {"key": "plan/grill", "kind": "questions",
                                                                 "phase": "plan", "summary": "q", "questions": []})])
    assert waiting.state["status"] == "waiting_human"
    go = Starter(qrepo)
    assert queue_tick(qrepo.cfg, qrepo.root, 3, go) == ["crm"]
    state = {m["id"]: m for m in QueueState.load(qrepo.root).modules}
    assert state["staff-auth"]["status"] == "waiting" and state["staff-auth"]["run_id"] == waiting.id


def test_queue_waiting_run_holds_a_slot(qrepo, monkeypatch):
    write(qrepo.root, "docs/plans/2026-10-28-platform.md", PLAN_DONE)
    merged(monkeypatch)
    _feature_run(qrepo, "staff-auth", [("hitl_raised", {"key": "plan/grill", "kind": "questions",
                                                       "phase": "plan", "summary": "q", "questions": []})])
    go = Starter(qrepo)
    assert queue_tick(qrepo.cfg, qrepo.root, 1, go) == []  # one slot, held by the waiting run
    assert queue_tick(qrepo.cfg, qrepo.root, 2, go) == ["crm"]


def test_queue_ready_run_holds_a_slot_and_is_not_a_crash(qrepo, monkeypatch):
    write(qrepo.root, "docs/plans/2026-10-28-platform.md", PLAN_DONE)
    merged(monkeypatch)
    run = _feature_run(qrepo, "staff-auth", [("phase_started", {"phase": "plan"}), ("phase_ready", {"phase": "plan"})])
    assert run.state["status"] == "ready"
    go = Starter(qrepo)
    messages: list[str] = []
    assert queue_tick(qrepo.cfg, qrepo.root, 1, go, log=messages.append) == []  # one slot, held by the ready run
    assert not [m for m in messages if "crashed" in m]
    assert queue_tick(qrepo.cfg, qrepo.root, 2, go) == ["crm"]


def marked_done_after(phase: str) -> list[tuple[str, dict]]:
    return [("phase_started", {"phase": phase}), ("phase_ready", {"phase": phase}),
            ("run_marked_done", {"phase": phase, "by": "me"}), ("run_finished", {})]


def test_queue_says_why_a_module_marked_done_early_does_not_move(qrepo):
    write(qrepo.root, "docs/plans/2026-10-28-platform.md", PLAN_OPEN)
    _feature_run(qrepo, "platform", marked_done_after("plan"))
    started, messages = ticks(qrepo)()
    assert started == []
    assert [m for m in messages if m.startswith("queue: platform blocked") and "marked done after plan" in m]


def test_queue_waits_quietly_for_the_merge_when_a_marked_module_finished_its_plan(qrepo):
    write(qrepo.root, "docs/plans/2026-10-28-platform.md", PLAN_DONE)
    _feature_run(qrepo, "platform", marked_done_after("verify"))
    _, messages = ticks(qrepo)()
    assert not [m for m in messages if "marked done" in m]


def test_queue_finished_unmerged_run_does_not_restart_or_hold_a_slot(qrepo, monkeypatch):
    write(qrepo.root, "docs/plans/2026-10-28-platform.md", PLAN_DONE)
    merged(monkeypatch)
    run = _feature_run(qrepo, "staff-auth", [("run_finished", {})])
    assert run.state["status"] == "done"
    go = Starter(qrepo)
    assert queue_tick(qrepo.cfg, qrepo.root, 1, go) == ["crm"]  # staff-auth awaits its merge; no second run for it


def test_queue_start_failure_is_logged_and_retried(qrepo):
    calls: list[str] = []

    real = Starter(qrepo)

    def flaky(module: str) -> str:
        calls.append(module)
        if len(calls) == 1:
            raise StartFailed("worktree could not be created")
        return real(module)

    messages: list[str] = []
    assert queue_tick(qrepo.cfg, qrepo.root, 2, flaky, log=messages.append) == []
    assert any("platform" in m and "worktree could not be created" in m for m in messages)
    assert QueueState.load(qrepo.root).modules[0]["status"] == "backlog"
    assert queue_tick(qrepo.cfg, qrepo.root, 2, flaky) == ["platform"]


def test_queue_rejects_bad_max_parallel(qrepo):
    with pytest.raises(ValueError, match="max_parallel"):
        queue_tick(qrepo.cfg, qrepo.root, 0, Starter(qrepo))


def test_queue_state_roundtrip_is_atomic_json(qrepo):
    queue_tick(qrepo.cfg, qrepo.root, 1, Starter(qrepo))
    path = qrepo.root / "devtools" / "agentflow" / "runs" / "queue.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert [m["id"] for m in data["modules"]] == ["platform", "staff-auth", "crm", "settings"]
    assert not list(path.parent.glob("queue.json.*"))


def test_run_queue_once_and_interrupt(qrepo, capsys):
    go = Starter(qrepo)
    assert module_queue.run_queue(qrepo.cfg, qrepo.root, 2, go, once=True) == 0
    assert go.started == ["platform"]
    assert "started platform" in capsys.readouterr().out

    def interrupt(_seconds: float) -> None:
        raise KeyboardInterrupt

    assert module_queue.run_queue(qrepo.cfg, qrepo.root, 2, go, sleep=interrupt) == 0  # Ctrl-C is a clean exit
    assert "stopped" in capsys.readouterr().out


def test_run_queue_loops_every_interval(qrepo):
    ticks: list[float] = []

    def sleep(seconds: float) -> None:
        ticks.append(seconds)
        if len(ticks) == 3:
            raise KeyboardInterrupt

    assert module_queue.run_queue(qrepo.cfg, qrepo.root, 2, Starter(qrepo), interval=30.0, sleep=sleep) == 0
    assert ticks == [30.0, 30.0, 30.0]


def test_run_queue_calls_remind_due_every_tick(qrepo, monkeypatch):
    seen: list[tuple] = []
    monkeypatch.setattr(hitl, "remind_due", lambda cfg, repo, now: seen.append((cfg, repo, now)) or [])
    assert module_queue.run_queue(qrepo.cfg, qrepo.root, 2, Starter(qrepo), once=True) == 0
    assert len(seen) == 1 and seen[0][1] == qrepo.root and seen[0][2].tzinfo is not None


def test_second_queue_is_refused(qrepo):
    with module_queue.queue_lock(qrepo.root):
        with pytest.raises(QueueBusy):
            module_queue.run_queue(qrepo.cfg, qrepo.root, 2, Starter(qrepo), once=True)
    assert module_queue.run_queue(qrepo.cfg, qrepo.root, 2, Starter(qrepo), once=True) == 0  # released


def test_cmd_queue_once_wires_the_spawner(qrepo, monkeypatch, capsys):
    spawned: list[tuple] = []
    monkeypatch.setattr("agentflow.flow._preflight", lambda cfg: None)
    monkeypatch.setattr(module_queue, "spawn_feature_run",
                        lambda repo, module, lane, config=None: spawned.append((module, config)) or f"run-{module}")
    assert main(["queue", "--once", "--max-parallel", "3"]) == 0
    assert spawned == [("platform", None)]
    assert "started platform" in capsys.readouterr().out


def test_cmd_queue_refused_while_another_queue_runs(qrepo, monkeypatch, capsys):
    monkeypatch.setattr("agentflow.flow._preflight", lambda cfg: None)
    with module_queue.queue_lock(qrepo.root):
        assert main(["queue", "--once"]) == 1
    assert "another `agentflow queue` is running" in capsys.readouterr().out


def test_cmd_queue_rejects_bad_max_parallel(qrepo, capsys):
    assert main(["queue", "--max-parallel", "0", "--once"]) == 1
    assert "max-parallel" in capsys.readouterr().out


def test_no_module_is_named_like_a_stdlib_module():
    """A file named like a stdlib module shadows it for any process with this folder on `sys.path`
    (`cd devtools/agentflow && python3 -m pytest` died on `from queue import Queue`)."""
    names = {p.stem for p in ENGINE.glob("*.py")} | {p.name for p in ENGINE.iterdir() if p.is_dir()}
    assert sorted(names & set(sys.stdlib_module_names)) == []


def test_stdlib_queue_resolves_from_the_agentflow_folder():
    out = subprocess.run(
        [sys.executable, "-c", "import queue; print(queue.__file__)"], cwd=ENGINE,
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert Path(out).parent != ENGINE


# --------------------------------------------------------------------------- blocked modules are reported


def ticks(qrepo, max_parallel: int = 3):
    """`tick()` runs one `queue_tick` and returns `(started, messages)`; every call shares one `Starter`."""
    go = Starter(qrepo)

    def tick() -> tuple[list[str], list[str]]:
        messages: list[str] = []
        return queue_tick(qrepo.cfg, qrepo.root, max_parallel, go, log=messages.append), messages

    return tick


def test_queue_reports_gh_failure_once_and_again_on_change(qrepo, monkeypatch):
    write(qrepo.root, "docs/plans/2026-10-28-platform.md", PLAN_DONE)
    _feature_run(qrepo, "platform", [("run_finished", {})])  # a finished run awaiting its merge
    monkeypatch.setenv("FAKE_GH_FAIL", "1")
    tick = ticks(qrepo)
    started, messages = tick()
    assert started == []
    blocked = [m for m in messages if m.startswith("queue: platform blocked")]
    assert len(blocked) == 1 and "fake gh: failing as asked" in blocked[0] and "flow/feature/platform" in blocked[0]
    dependents = {m.split()[1] for m in messages if "waiting on platform" in m}
    assert dependents == {"staff-auth", "crm"}  # settings waits on staff-auth, which already says why
    assert tick() == ([], [])  # the same reasons are not repeated
    monkeypatch.delenv("FAKE_GH_FAIL")  # gh works again: nothing merged yet, so nothing is wrong
    assert tick() == ([], [])
    monkeypatch.setenv("FAKE_GH_FAIL", "1")  # failing again is a change from the last tick and is reported again
    assert any(m.startswith("queue: platform blocked") for m in tick()[1])


def test_queue_reports_a_failed_run_once_and_does_not_restart_it(qrepo):
    _feature_run(qrepo, "platform", [("run_failed", {"reason": "gate verify failed"})])
    tick = ticks(qrepo)
    started, messages = tick()
    assert started == []
    blocked = [m for m in messages if m.startswith("queue: platform blocked")]
    assert len(blocked) == 1 and "failed" in blocked[0] and "gate verify failed" in blocked[0]
    assert "human" in blocked[0]
    assert {m.split()[1] for m in messages if "waiting on platform" in m} == {"staff-auth", "crm"}
    assert tick() == ([], [])
    _feature_run(qrepo, "platform", [("run_cancelled", {})])  # a newer run, cancelled: a different reason
    started, messages = tick()
    assert started == []
    assert any(m.startswith("queue: platform blocked") and "cancelled" in m for m in messages)
    assert tick() == ([], [])


def test_queue_missing_run_directory_is_treated_as_no_run(qrepo):
    QueueState([{"id": "platform", "deps": [], "status": "running", "run_id": "ghost-run"}]).save(qrepo.root)
    tick = ticks(qrepo, max_parallel=1)
    started, messages = tick()
    assert started == ["platform"]  # it holds no slot and is started again
    assert [m for m in messages if "ghost-run" in m] == [
        "queue: platform: run ghost-run no longer exists; treating platform as not started"
    ]
    assert tick() == ([], [])


def test_queue_reports_unknown_dependency_and_cycle(qrepo):
    table = MAP + "| `lonely` | x | nowhere | SPEC-lonely |\n| `a` | x | b | SPEC-a |\n| `b` | x | a | SPEC-b |\n"
    write(qrepo.root, "docs/specs/CAPABILITY-MAP.md", table)
    tick = ticks(qrepo, max_parallel=1)
    started, messages = tick()
    assert started == ["platform"]
    lonely = [m for m in messages if m.startswith("queue: lonely blocked")]
    assert len(lonely) == 1 and "nowhere" in lonely[0] and "not in the capability map" in lonely[0]
    for module in ("a", "b"):
        found = [m for m in messages if m.startswith(f"queue: {module} blocked")]
        assert len(found) == 1 and "dependency cycle: a -> b -> a" in found[0]
    assert tick() == ([], [])
    write(qrepo.root, "docs/specs/CAPABILITY-MAP.md", table.replace("| nowhere |", "| elsewhere |"))
    started, messages = tick()
    assert [m for m in messages if m.startswith("queue: lonely blocked")] == [
        "queue: lonely blocked \u2014 depends on elsewhere, which is not in the capability map"
    ]


def test_queue_start_failure_is_reported_once_across_ticks(qrepo):
    def broken(module: str) -> str:
        raise StartFailed("fork_from is missing")

    first, second = [], []
    queue_tick(qrepo.cfg, qrepo.root, 2, broken, log=first.append)
    queue_tick(qrepo.cfg, qrepo.root, 2, broken, log=second.append)  # a second process: the state file remembers
    assert first == ["queue: platform blocked \u2014 could not start: fork_from is missing"]
    assert second == []


# --------------------------------------------------------------------------- the real spawn


def fake_flow(tmp_path: Path, body: str) -> list[str]:
    script = tmp_path / "fake_flow.py"
    script.write_text(textwrap.dedent(body), encoding="utf-8")
    return [sys.executable, str(script)]


def test_spawn_feature_run_returns_run_id(qrepo, tmp_path):
    argv_log = tmp_path / "argv.json"
    command = fake_flow(tmp_path, f"""
        import json, os, sys
        json.dump([sys.argv[1:], os.getsid(0) != os.getppid(), sys.stdin.read()], open({str(argv_log)!r}, "w"))
        print("Started 20261030-1000-feature-crm in /w on flow/feature/crm.")
        print("20261030-1000-feature-crm goes on in the background; log: /w/runner.log")
    """)
    run_id = module_queue.spawn_feature_run(qrepo.root, "crm", lane="feature", command=command)
    assert run_id == "20261030-1000-feature-crm"
    argv, _, stdin_text = json.loads(argv_log.read_text(encoding="utf-8"))
    assert argv == ["run", "feature", "--module=crm", "--detach"]
    assert stdin_text == ""  # stdin is closed
    assert "Started 20261030-1000-feature-crm" in (qrepo.root / "devtools/agentflow/runs/queue.log").read_text()


def test_spawn_feature_run_passes_config_as_argv_items(qrepo, tmp_path):
    argv_log = tmp_path / "argv.json"
    command = fake_flow(tmp_path, f"""
        import json, sys
        json.dump(sys.argv[1:], open({str(argv_log)!r}, "w"))
        print("Started r1 in /w on b")
    """)
    module_queue.spawn_feature_run(qrepo.root, "staff-auth", lane="feature", command=command, config=Path("/c/config.yaml"))
    assert json.loads(argv_log.read_text(encoding="utf-8")) == [
        "run", "feature", "--module=staff-auth", "--detach", "--config", "/c/config.yaml",
    ]


def test_spawn_feature_run_failure_raises(qrepo, tmp_path):
    command = fake_flow(tmp_path, """
        import sys
        print("error: no such module")
        sys.exit(1)
    """)
    with pytest.raises(StartFailed, match="no such module"):
        module_queue.spawn_feature_run(qrepo.root, "crm", lane="feature", command=command)
    silent = fake_flow(tmp_path, 'print("hello")\n')
    with pytest.raises(StartFailed, match="run id"):
        module_queue.spawn_feature_run(qrepo.root, "crm", lane="feature", command=silent)


@pytest.mark.parametrize("bad", ["--config=evil", "x; touch pwned", "a b", "", "$(id)"])
def test_module_ids_are_validated_before_spawn(qrepo, bad):
    with pytest.raises(StartFailed, match="module id"):
        module_queue.spawn_feature_run(qrepo.root, bad, lane="feature", command=["true"])
    assert not (qrepo.root / "pwned").exists()


def test_uncommitted_paths_lists_files_renames_and_odd_names(tmp_git):
    from agentflow.gitops import uncommitted_paths

    assert uncommitted_paths(tmp_git) == []
    write(tmp_git, "docs/ideas/ghi chú.md", "x\n")  # untracked, non-ASCII and a space
    write(tmp_git, "docs/decisions/a.md", "x\n")
    run_git(tmp_git, "mv", "README.md", "READ.md")
    write(tmp_git, "src/app.py", "x = 2\n")
    assert uncommitted_paths(tmp_git) == sorted(
        ["README.md", "READ.md", "docs/decisions/a.md", "docs/ideas/ghi chú.md", "src/app.py"]
    )


# --------------------------------------------------------------------------- sync_branch


def _push_from_clone(fr, tmp_path: Path, branch: str, name: str = "other") -> str:
    """Commit to `branch` through a second clone of origin and push it; return the new sha."""
    clone = tmp_path / name
    run_git(tmp_path, "clone", "-q", str(fr.origin), str(clone))
    run_git(clone, "config", "user.name", "Other")
    run_git(clone, "config", "user.email", "other@example.com")
    run_git(clone, "config", "commit.gpgsign", "false")
    run_git(clone, "checkout", "-q", branch)
    (clone / f"{name}.txt").write_text("x\n", encoding="utf-8")
    run_git(clone, "add", "-A")
    run_git(clone, "commit", "-q", "-m", f"{name} change")
    run_git(clone, "push", "-q", "origin", branch)
    return run_git(clone, "rev-parse", "HEAD").strip()


def test_sync_branch_fast_forwards_a_branch_that_is_not_checked_out(fr, tmp_path):
    from agentflow.gitops import sync_branch

    run_git(fr.root, "checkout", "-q", "main")
    sha = _push_from_clone(fr, tmp_path, "next")
    assert sync_branch(fr.root, "next") == ""
    assert run_git(fr.root, "rev-parse", "next").strip() == sha


def test_sync_branch_fast_forwards_the_checked_out_branch(fr, tmp_path):
    from agentflow.gitops import sync_branch

    sha = _push_from_clone(fr, tmp_path, "next")  # the fixture has `next` checked out
    assert sync_branch(fr.root, "next") == ""
    assert fr.head() == sha


def test_sync_branch_never_rewrites_a_diverged_branch(fr, tmp_path):
    from agentflow.gitops import sync_branch

    write(fr.root, "local.txt", "mine\n")
    commit_all(fr.root)
    mine = fr.head()
    _push_from_clone(fr, tmp_path, "next")
    note = sync_branch(fr.root, "next")
    assert note and "next" in note
    assert fr.head() == mine


def test_sync_branch_says_why_when_it_cannot_reach_origin(fr):
    from agentflow.gitops import sync_branch

    run_git(fr.root, "remote", "set-url", "origin", str(fr.root / "no-such-remote"))
    before = fr.head()
    assert sync_branch(fr.root, "next")  # a note, not an exception
    assert fr.head() == before


# --------------------------------------------------------------------------- assess_modules


def test_assess_modules_records_the_map_without_starting_anything(qrepo, monkeypatch):
    write(qrepo.root, "docs/plans/2026-10-28-platform.md", PLAN_DONE)
    merged(monkeypatch)
    notes: list[str] = []
    assessment = module_queue.assess_modules(qrepo.cfg, qrepo.root, log=notes.append)
    by_id = {e["id"]: e for e in assessment.entries}
    assert by_id["platform"]["status"] == "done"
    assert by_id["crm"]["status"] == "backlog" and by_id["crm"]["run_id"] is None
    assert {m["id"]: m["status"] for m in QueueState.load(qrepo.root).modules} == {
        "platform": "done", "staff-auth": "backlog", "crm": "backlog", "settings": "backlog",
    }
    assert assessment.occupied == 0 and assessment.blocked == {}


# --------------------------------------------------------------------------- spawn_run


def _argv_of(tmp_path: Path, repo: Path, lane: str, params: dict, **kwargs) -> list[str]:
    argv_log = tmp_path / "argv.json"
    command = fake_flow(tmp_path, f"""
        import json, sys
        json.dump(sys.argv[1:], open({str(argv_log)!r}, "w"))
        print("Started r1 in /w on b")
    """)
    assert module_queue.spawn_run(repo, lane, params, command=command, **kwargs) == "r1"
    return json.loads(argv_log.read_text(encoding="utf-8"))


def test_spawn_run_passes_every_value_as_one_flag_item(qrepo, tmp_path):
    argv = _argv_of(tmp_path, qrepo.root, "bug", {"task": "--config=/evil; rm -rf x"})
    assert argv == ["run", "bug", "--task=--config=/evil; rm -rf x", "--detach"]


def test_spawn_run_passes_the_stop_phase_and_the_new_module_row(qrepo, tmp_path):
    argv = _argv_of(tmp_path, qrepo.root, "feature", {"module": "billing"}, to_phase="define",
                    new_module={"responsibility": "bills", "deps": ["platform"]})
    assert argv[:4] == ["run", "feature", "--module=billing", "--to=define"]
    assert json.loads(argv[4].removeprefix("--new-module=")) == {"responsibility": "bills", "deps": ["platform"]}
    assert argv[-1] == "--detach"


@pytest.mark.parametrize("lane,params", [
    ("bad lane", {"task": "x"}), ("--x", {"task": "x"}), ("", {"task": "x"}),
    ("bug", {"log": "/etc/passwd"}),       # --log reads a local path, so it is never offered
    ("bug", {"nope": "x"}), ("bug", {}), ("bug", {"task": "a\0b"}), ("feature", {"module": "A b"}),
])
def test_spawn_run_refuses_what_could_become_a_flag_or_a_path(qrepo, lane, params):
    with pytest.raises(StartFailed):
        module_queue.spawn_run(qrepo.root, lane, params, command=["true"])
