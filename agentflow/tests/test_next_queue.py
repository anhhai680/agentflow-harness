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
from agentflow.state import runs_root

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


def test_next_marks_a_bundled_skill(fr):
    write(fr.root, "docs/plans/2026-10-28-crm.md", PLAN_OPEN)
    fr.cfg.skills_map["build_small"] = "agentflow-build"  # the fixture repo has no such skill: only the engine does
    fr.cfg.skills_map["verify"] = "no-such-skill"  # found nowhere: named plainly
    answer = next_step(fr.cfg, fr.root)
    assert answer.next == ("`agentflow-build` (bundled with agentflow: a run uses it; "
                           "it is not installed for interactive sessions)")
    fr.cfg.skills_map["build_small"] = "no-such-skill"
    assert next_step(fr.cfg, fr.root).next == "no-such-skill"
    fr.cfg.project.skills_dir = "own-skills"  # not `.claude/skills`: the fixture repo links that to the shared fixture
    write(fr.root, "own-skills/agentflow-build/SKILL.md", "# the repository's own\n")
    fr.cfg.skills_map["build_small"] = "agentflow-build"  # a repo skill that shadows the bundled name is plain
    assert next_step(fr.cfg, fr.root).next == "agentflow-build"


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
    write(fr.root, "docs/idea.md", "# idea\n")
    write(fr.root, "GLOSSARY.md", "# terms\n")
    answer = next_step(fr.cfg, fr.root)
    assert answer.lane == "design-doc"
    assert any("docs/idea.md" in line for line in answer.evidence)


def test_next_mixed_changes_are_not_doc_only(fr):
    write(fr.root, "docs/idea.md", "# idea\n")
    write(fr.root, "src/other.py", "x = 1\n")
    assert next_step(fr.cfg, fr.root).lane == "none"


def test_next_eval_paths(fr):
    write(fr.root, "toy/knowledge/retrieval/x.py", "x = 1\n")
    answer = next_step(fr.cfg, fr.root)
    assert answer.lane == "eval"
    assert answer.stage == "Verify"
    assert any("toy/knowledge/retrieval/x.py" in line for line in answer.evidence)


def test_next_eval_beats_open_plan(fr):
    write(fr.root, "docs/plans/2026-10-28-crm.md", PLAN_OPEN)
    write(fr.root, "toy/agent/policy.py", "x = 1\n")
    assert next_step(fr.cfg, fr.root).lane == "eval"


def test_next_names_the_bundled_note_once_in_its_answer(fr):
    for key in ("decide", "define"):  # both bundled: the idle answer names them in `next` and again in `reason`
        fr.cfg.skills_map[key] = "agentflow-define"
    text = module_queue.format_answer(next_step(fr.cfg, fr.root))
    assert text.count("bundled with agentflow") == 1 and "`agentflow-define`" in text
    fr.cfg.skills_map["define"] = "agentflow-plan"  # two bundled skills: still one note
    answer = next_step(fr.cfg, fr.root)
    assert answer.next == f"`agentflow-define` or `agentflow-plan` {module_queue.BUNDLED_NOTE}"
    assert module_queue.format_answer(answer).count("bundled with agentflow") == 1


def test_next_nothing_in_flight(fr):
    answer = next_step(fr.cfg, fr.root)
    assert answer.lane == "none"
    assert answer.evidence


def test_next_security_line_when_overlay_matches(fr):
    write(fr.root, "docs/plans/2026-10-28-crm.md", PLAN_OPEN)
    write(fr.root, "toy/staff_auth/guard.py", "x = 1\n")
    answer = next_step(fr.cfg, fr.root)
    assert answer.security
    text = module_queue.format_answer(answer)
    assert text.splitlines()[3].startswith("Security:")
    assert "security-and-hardening" in text and "/doubt-driven-development" in text


def test_next_security_line_from_plan_keywords(fr):
    write(fr.root, "docs/plans/2026-10-28-crm.md", PLAN_OPEN + "\nThe session token is checked.\n")
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
    fr.install_pack()
    write(fr.root, "docs/idea.md", "# idea\n")
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
    path = runs_root(qrepo.root) / "queue.json"
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
    qrepo.install_pack()
    spawned: list[tuple] = []
    monkeypatch.setattr("agentflow.flow._preflight", lambda cfg: None)
    monkeypatch.setattr(module_queue, "spawn_feature_run",
                        lambda repo, module, lane, config=None: spawned.append((module, config)) or f"run-{module}")
    assert main(["queue", "--once", "--max-parallel", "3"]) == 0
    assert spawned == [("platform", None)]
    assert "started platform" in capsys.readouterr().out


def test_cmd_queue_refused_while_another_queue_runs(qrepo, monkeypatch, capsys):
    qrepo.install_pack()
    monkeypatch.setattr("agentflow.flow._preflight", lambda cfg: None)
    with module_queue.queue_lock(qrepo.root):
        assert main(["queue", "--once"]) == 1
    assert "another `agentflow queue` is running" in capsys.readouterr().out


def test_cmd_queue_rejects_bad_max_parallel(qrepo, capsys):
    assert main(["queue", "--max-parallel", "0", "--once"]) == 1
    assert "max-parallel" in capsys.readouterr().out


def test_no_module_is_named_like_a_stdlib_module():
    """A file named like a stdlib module shadows it for any process with this folder on `sys.path`
    (`cd agentflow && python3 -m pytest` died on `from queue import Queue`)."""
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
    assert "Started 20261030-1000-feature-crm" in (runs_root(qrepo.root) / "queue.log").read_text()


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


@pytest.fixture
def sqrepo(fr):
    """`qrepo` for a pack whose work source is its spec files: two of them, `crm` after `platform`."""
    fr.configure({"work_source": {"kind": "specs", "path": None}})
    write(fr.root, "docs/specs/SPEC-platform.md", APPROVED_SPEC.format(m="platform"))
    write(fr.root, "docs/specs/SPEC-crm.md", APPROVED_SPEC.format(m="crm") + "\n**Depends on:** platform\n")
    commit_all(fr.root)
    return fr


def test_cmd_queue_on_a_specs_pack_needs_include_specs(sqrepo, monkeypatch, capsys):
    sqrepo.install_pack()
    spawned: list[tuple] = []
    monkeypatch.setattr("agentflow.flow._preflight", lambda cfg: None)
    monkeypatch.setattr(module_queue, "spawn_feature_run",
                        lambda repo, module, lane, config=None: spawned.append((module, config)) or f"run-{module}")
    assert main(["queue", "--once"]) == 1
    out = capsys.readouterr().out
    assert ("this pack's work source is its spec files: 2 modules are listed; pass --include-specs to queue them"
            in out)
    assert spawned == []
    assert main(["queue", "--once", "--include-specs"]) == 0
    assert spawned == [("platform", None)]
    assert "started platform" in capsys.readouterr().out


# --------------------------------------------------------------------------- finished modules and their reasons


class Merges:
    """`branch_is_merged` that records the branches it was asked about and answers `answer`."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.answer: tuple[bool | None, str] = (False, "")

    def __call__(self, cfg, repo, branch: str) -> tuple[bool | None, str]:
        self.calls.append(branch)
        return self.answer


@pytest.fixture
def dq(qrepo, monkeypatch):
    """`qrepo` (the capability map, `fork_from: next`) with `branch_is_merged` counted (`dq.merges`)."""
    qrepo.merges = Merges()
    monkeypatch.setattr(module_queue, "branch_is_merged", qrepo.merges)
    return qrepo


def spec_text(status: str) -> str:
    return f"# Spec\n\n{status}\n\n## Scope\n\nWhat it does.\n"


def naming(module: str, plan: str = PLAN_DONE) -> str:
    """`plan` with the header line naming `module`'s spec file."""
    return plan.replace("# Plan\n\n", f"# Plan\n\n**Spec:** `docs/specs/SPEC-{module}.md`\n\n", 1)


def assess(fr) -> dict[str, dict]:
    return {e["id"]: e for e in module_queue.assess_modules(fr.cfg, fr.root).entries}


def recorded_on_disk(fr) -> dict[str, dict]:
    return {m["id"]: m for m in QueueState.load(fr.root).modules}


def test_a_module_done_by_hand_is_done_without_asking_gh(dq):
    from agentflow import finished

    write(dq.root, "docs/plans/2026-10-28-crm.md", PLAN_DONE)  # would ask gh, were the mark not first
    finished.mark(dq.root, "crm", "cli")
    crm = assess(dq)["crm"]
    assert (crm["status"], crm["done_reason"]) == ("done", finished.BY_HAND)
    assert recorded_on_disk(dq)["crm"]["done_reason"] == finished.BY_HAND
    assert "done_reason" not in recorded_on_disk(dq)["platform"]  # only done entries carry a reason
    assert dq.merges.calls == []


def test_a_veto_stops_origin_evidence_but_not_a_merged_branch(dq, tmp_path):
    from agentflow import finished
    from conftest import land_on_origin

    land_on_origin(dq, tmp_path, {"docs/specs/SPEC-crm.md": spec_text("Status: done"),
                                  "docs/plans/2026-10-01-crm.md": naming("crm")})
    assert assess(dq)["crm"]["done_reason"] == finished.SPEC_SAYS
    finished.veto(dq.root, "crm", "pat")
    dq.merges.answer = (False, "")
    crm = assess(dq)["crm"]
    assert crm["status"] == "backlog" and "done_reason" not in crm
    dq.merges.answer = (True, "")
    assert assess(dq)["crm"]["done_reason"] == finished.MERGED


def test_a_spec_on_origin_whose_header_says_done_is_done(dq, tmp_path):
    from agentflow import finished
    from conftest import land_on_origin

    land_on_origin(dq, tmp_path, {"docs/specs/SPEC-crm.md": spec_text("**Status:** done")})
    entries = assess(dq)
    assert (entries["crm"]["status"], entries["crm"]["done_reason"]) == ("done", finished.SPEC_SAYS)
    assert entries["platform"]["status"] == "backlog"
    assert dq.merges.calls == []


@pytest.mark.parametrize("line", ["Status: shipped", "**Status: implemented** 2026-01-02", "- **Status:** done",
                                  "> Status: done"])
def test_each_status_form_counts(line, dq, tmp_path):
    from agentflow import finished
    from conftest import land_on_origin

    land_on_origin(dq, tmp_path, {"docs/specs/SPEC-crm.md": spec_text(line)})
    assert assess(dq)["crm"].get("done_reason") == finished.SPEC_SAYS


@pytest.mark.parametrize("line", ["Status: done-ish", "Status: not done", "Statusdone"])
def test_lookalike_status_lines_do_not_count(line, dq, tmp_path):
    from conftest import land_on_origin

    land_on_origin(dq, tmp_path, {"docs/specs/SPEC-crm.md": spec_text(line)})
    assert assess(dq)["crm"]["status"] == "backlog"


def test_a_done_status_in_a_fence_or_below_the_first_section_does_not_count(dq, tmp_path):
    from conftest import land_on_origin

    text = "# Spec\n\n```\nStatus: done\n```\n\n**Status:** draft\n\n## History\n\nStatus: done\n"
    land_on_origin(dq, tmp_path, {"docs/specs/SPEC-crm.md": text})
    assert assess(dq)["crm"]["status"] == "backlog"


def test_a_bad_done_status_pattern_is_a_problem_not_a_crash(dq, tmp_path):
    from conftest import land_on_origin

    land_on_origin(dq, tmp_path, {"docs/specs/SPEC-crm.md": spec_text("Status: done")})
    dq.cfg.project.done_status = "("  # set after load: validate would have refused it
    found = module_queue.assess_modules(dq.cfg, dq.root)
    crm = {e["id"]: e for e in found.entries}["crm"]
    assert crm["status"] == "backlog"
    assert "project.docs.done_status" in (crm.get("blocked") or "")


def test_a_spec_or_plan_finished_only_in_the_work_tree_does_not_count(dq):
    run_git(dq.root, "checkout", "-q", "-b", "unmerged-work")
    write(dq.root, "docs/specs/SPEC-crm.md", spec_text("**Status:** done"))
    write(dq.root, "docs/plans/2026-10-28-crm.md", naming("crm"))
    commit_all(dq.root)  # committed on a branch nobody merged: origin has neither file
    dq.merges.answer = (False, "")
    crm = assess(dq)["crm"]
    assert crm["status"] == "backlog" and "done_reason" not in crm


def test_the_newest_dated_plan_ticked_on_origin_and_naming_its_spec_is_done_without_gh(dq, tmp_path):
    from agentflow import finished
    from conftest import land_on_origin

    land_on_origin(dq, tmp_path, {"docs/plans/2026-10-28-crm.md": naming("crm"),
                                  "docs/plans/2026-10-28-platform.md": naming("platform", PLAN_DEFERRED)})
    entries = assess(dq)
    assert entries["crm"]["done_reason"] == finished.plan_complete("next") == "plan complete on next"
    assert entries["platform"]["done_reason"] == finished.plan_complete("next")  # a deferred task waits on others
    assert dq.merges.calls == []


def test_a_ticked_plan_that_does_not_name_the_modules_spec_needs_a_merged_branch(dq, tmp_path):
    from agentflow import finished
    from conftest import land_on_origin

    land_on_origin(dq, tmp_path, {"docs/plans/2026-10-28-crm.md": PLAN_DONE,
                                  "docs/plans/2026-10-28-platform.md": naming("settings")})
    dq.merges.answer = (False, "")
    entries = assess(dq)
    assert entries["crm"]["status"] == entries["platform"]["status"] == "backlog"
    assert sorted(dq.merges.calls) == ["flow/feature/crm", "flow/feature/platform"]
    dq.merges.answer = (True, "")
    assert assess(dq)["crm"]["done_reason"] == finished.MERGED


def test_an_older_ticked_plan_does_not_finish_a_module_whose_newer_plan_is_open(dq, tmp_path):
    from conftest import land_on_origin

    land_on_origin(dq, tmp_path, {"docs/plans/2026-10-01-crm.md": naming("crm"),
                                  "docs/plans/2026-10-28-crm-rework.md": naming("crm", PLAN_OPEN)})
    dq.merges.answer = (True, "")
    assert assess(dq)["crm"]["status"] == "backlog"
    assert dq.merges.calls == []  # the newest plan is open: nothing to ask gh about


def test_an_undated_plan_file_is_never_a_plan(dq, tmp_path):
    from conftest import land_on_origin

    land_on_origin(dq, tmp_path, {"docs/plans/crm.md": naming("crm")})
    write(dq.root, "docs/plans/crm.md", naming("crm"))
    dq.merges.answer = (True, "")
    assert assess(dq)["crm"]["status"] == "backlog"
    assert dq.merges.calls == []
    assert module_queue.own_plans(["crm.md", "2026-10-28-crm.md"], "crm", {"crm"}) == ["2026-10-28-crm.md"]


def test_a_truncated_plan_on_origin_is_not_finished(dq, tmp_path):
    from conftest import land_on_origin

    padding = "\n<!-- " + "x" * (1 << 20) + " -->\n"  # past the 1 MB a plan is read to, under the size skipped
    land_on_origin(dq, tmp_path, {"docs/plans/2026-10-28-crm.md": naming("crm") + padding})
    dq.merges.answer = (True, "")
    assert assess(dq)["crm"]["status"] == "backlog"
    assert dq.merges.calls == []


def test_a_long_heading_line_in_an_origin_plan_is_cut_before_parsing(dq, tmp_path, monkeypatch):
    from agentflow import finished, sources
    from conftest import land_on_origin

    plan = naming("crm", "# Plan\n\n### Task 1: " + "y" * ((1 << 20) - 4096) + "\n\n- [x] done\n")
    land_on_origin(dq, tmp_path, {"docs/plans/2026-10-28-crm.md": plan})
    seen: list[int] = []
    real = sources.parse_plan_text

    def spy(text: str):
        seen.append(max((len(line) for line in text.splitlines()), default=0))
        return real(text)

    monkeypatch.setattr(sources, "parse_plan_text", spy)
    assert assess(dq)["crm"]["done_reason"] == finished.plan_complete("next")
    assert seen and max(seen) <= sources.LINE_MAX


def test_no_other_modules_plan_finishes_a_prefix_module(dq, tmp_path):
    from conftest import land_on_origin

    write(dq.root, "docs/specs/CAPABILITY-MAP.md",
          "| Module id | Responsibility | Depends on |\n|---|---|---|\n| `user` | users | — |\n")
    commit_all(dq.root)
    land_on_origin(dq, tmp_path, {
        "docs/specs/SPEC-user-auth.md": spec_text("**Status:** draft"),  # `user-auth` is known from origin
        "docs/plans/2026-01-01-user-auth.md": naming("user-auth"),
        "docs/plans/2026-01-02-user-profile.md": naming("user-profile"),  # no source knows `user-profile`
    })
    dq.merges.answer = (False, "")
    assert assess(dq)["user"]["status"] == "backlog"
    names = ["2026-01-01-user-auth.md", "2026-01-02-user-profile.md", "2026-01-03-user.md"]
    assert module_queue.own_plans(names, "user", {"user", "user-auth"}) == ["2026-01-02-user-profile.md",
                                                                            "2026-01-03-user.md"]
    assert module_queue.own_plans(names, "user-auth", {"user"}) == ["2026-01-01-user-auth.md"]


def test_an_old_done_entry_without_a_reason_stays_done_while_gh_fails(dq, monkeypatch):
    from agentflow import finished

    write(dq.root, "docs/plans/2026-10-28-platform.md", PLAN_DONE)
    QueueState([{"id": "platform", "deps": [], "status": "done", "run_id": None}]).save(dq.root)  # before reasons
    dq.merges.answer = (None, "fake gh: failing as asked")
    platform = assess(dq)["platform"]
    assert (platform["status"], platform["done_reason"]) == ("done", finished.MERGED)
    assert dq.merges.calls == []
    assert finished.recorded(dq.root)["platform"] == finished.MERGED


def test_origin_unreadable_or_incomplete_keeps_earlier_conclusions(dq, tmp_path, monkeypatch):
    from agentflow import finished, gitops
    from conftest import land_on_origin

    land_on_origin(dq, tmp_path, {"docs/specs/SPEC-crm.md": spec_text("Status: done"),
                                  "docs/plans/2026-10-28-platform.md": naming("platform")})
    entries = assess(dq)
    assert entries["crm"]["done_reason"] == finished.SPEC_SAYS
    assert entries["platform"]["done_reason"] == finished.plan_complete("next")
    real = gitops.blobs_at

    monkeypatch.setattr(gitops, "blobs_at", lambda *a, **k: None)  # git failed or timed out
    entries = assess(dq)
    assert entries["crm"]["done_reason"] == finished.SPEC_SAYS
    assert entries["platform"]["done_reason"] == finished.plan_complete("next")

    def partial(*args, **kwargs):
        found = real(*args, **kwargs)
        return None if found is None else gitops.Listing({}, False)

    monkeypatch.setattr(gitops, "blobs_at", partial)  # too many files to list in full
    entries = assess(dq)
    assert entries["crm"]["done_reason"] == finished.SPEC_SAYS
    assert entries["platform"]["done_reason"] == finished.plan_complete("next")

    monkeypatch.setattr(gitops, "blobs_at", real)
    run_git(dq.root, "update-ref", "-d", "refs/remotes/origin/next")  # no origin ref at all
    assert assess(dq)["crm"]["done_reason"] == finished.SPEC_SAYS
    assert dq.merges.calls == []


def test_a_ticked_plan_in_an_incomplete_listing_is_not_plan_complete(dq, tmp_path, monkeypatch):
    from agentflow import gitops
    from conftest import land_on_origin

    land_on_origin(dq, tmp_path, {"docs/plans/2026-10-28-crm.md": naming("crm")})
    real = gitops.blobs_at

    def incomplete(*args, **kwargs):
        found = real(*args, **kwargs)
        return None if found is None else gitops.Listing(found.blobs, False)

    monkeypatch.setattr(gitops, "blobs_at", incomplete)
    dq.merges.answer = (False, "")
    crm = assess(dq)["crm"]
    assert crm["status"] == "backlog" and "done_reason" not in crm


def test_a_merged_branch_with_a_finished_plan_is_still_done_as_merged(dq):
    from agentflow import finished

    write(dq.root, "docs/plans/2026-10-28-crm.md", PLAN_DONE)
    dq.merges.answer = (True, "")
    assert assess(dq)["crm"]["done_reason"] == finished.MERGED
    assert dq.merges.calls == ["flow/feature/crm"]
    dq.merges.answer = (None, "gh is down")
    assert assess(dq)["crm"]["done_reason"] == finished.MERGED  # recorded as merged: gh is not asked again
    assert dq.merges.calls == ["flow/feature/crm"]


def test_a_hand_marked_done_does_not_stick_as_merged_after_unmark(dq):
    from agentflow import finished

    write(dq.root, "docs/plans/2026-10-28-crm.md", PLAN_DONE)
    finished.mark(dq.root, "crm", "cli")
    assert assess(dq)["crm"]["done_reason"] == finished.BY_HAND
    assert finished.unmark(dq.root, "crm") is True
    dq.merges.answer = (False, "")
    crm = assess(dq)["crm"]
    assert crm["status"] == "backlog" and "done_reason" not in crm
    assert dq.merges.calls == ["flow/feature/crm"]  # a hand mark is no merge: gh is asked once it is gone


def test_cached_entries_apply_live_marks_on_top_of_the_last_assessment(dq, tmp_path):
    from agentflow import finished, sources
    from conftest import land_on_origin

    write(dq.root, "docs/plans/2026-10-28-platform.md", PLAN_DONE)
    land_on_origin(dq, tmp_path, {"docs/specs/SPEC-staff-auth.md": spec_text("Status: done")})
    dq.merges.answer = (True, "")
    assess(dq)
    nodes = [sources.ModuleNode("platform", []), sources.ModuleNode("staff-auth", ["platform"]),
             sources.ModuleNode("crm", ["platform"])]
    cached = {e["id"]: e for e in module_queue.cached_entries(dq.root, nodes, {})}
    assert (cached["platform"]["status"], cached["platform"]["done_reason"]) == ("done", finished.MERGED)
    assert cached["staff-auth"]["done_reason"] == finished.SPEC_SAYS
    assert cached["crm"]["status"] == "backlog" and "done_reason" not in cached["crm"]
    finished.mark(dq.root, "crm", "cli")
    finished.veto(dq.root, "platform", "cli")
    finished.veto(dq.root, "staff-auth", "cli")
    cached = {e["id"]: e for e in module_queue.cached_entries(dq.root, nodes, {})}
    assert (cached["crm"]["status"], cached["crm"]["done_reason"]) == ("done", finished.BY_HAND)
    assert cached["staff-auth"]["status"] == "backlog"  # a veto ignores origin evidence
    assert (cached["platform"]["status"], cached["platform"]["done_reason"]) == ("done", finished.MERGED)  # not a merge
    entries = assess(dq)  # the assessment agrees with the cached view
    assert entries["staff-auth"]["status"] == "backlog"
    assert entries["platform"]["done_reason"] == finished.MERGED
    assert finished.recorded(dq.root) == {"platform": finished.MERGED, "crm": finished.BY_HAND}


def test_next_skips_done_modules_in_both_spec_and_checked_plan_rules(dq):
    from agentflow import finished

    write(dq.root, "docs/specs/SPEC-crm.md", APPROVED_SPEC.format(m="crm"))
    commit_all(dq.root)
    assert next_step(dq.cfg, dq.root).stage == "Plan"
    finished.mark(dq.root, "crm", "cli")
    assert next_step(dq.cfg, dq.root).lane == "none"  # crm is done: no plan is asked for
    finished.unmark(dq.root, "crm")
    write(dq.root, "docs/plans/2026-10-28-crm.md", PLAN_DONE)
    commit_all(dq.root)
    assert next_step(dq.cfg, dq.root).stage == "Verify"
    assert dq.merges.calls == ["flow/feature/crm"]
    finished.mark(dq.root, "crm", "cli")
    assert next_step(dq.cfg, dq.root).lane == "none"
    assert dq.merges.calls == ["flow/feature/crm"]  # a done module's branch is not asked about


def test_a_corrupt_marks_file_lets_origin_evidence_make_no_new_done(dq, tmp_path):
    from agentflow import finished
    from conftest import land_on_origin

    land_on_origin(dq, tmp_path, {"docs/specs/SPEC-platform.md": spec_text("Status: done")})
    assert assess(dq)["platform"]["done_reason"] == finished.SPEC_SAYS
    finished.veto(dq.root, "crm", "pat")
    land_on_origin(dq, tmp_path, {"docs/specs/SPEC-crm.md": spec_text("Status: done"),
                                  "docs/plans/2026-10-28-crm.md": naming("crm")})
    assert assess(dq)["crm"]["status"] == "backlog"  # vetoed
    (runs_root(dq.root) / finished.MARKS).write_text('{"crm": {"state": "not_d', encoding="utf-8")  # torn
    messages: list[str] = []
    entries = {e["id"]: e for e in module_queue.assess_modules(dq.cfg, dq.root, log=messages.append).entries}
    assert entries["crm"]["status"] == "backlog"  # the veto can't be read: origin evidence makes nothing done
    assert entries["platform"]["done_reason"] == finished.SPEC_SAYS  # an earlier Done stays
    assert [m for m in messages if finished.MARKS in m and "unreadable" in m]


# --------------------------------------------------------------------------- retro due


def _old_run(fr) -> str:
    """A run that ended ten days ago, so a retro is due under the default `memory.retro_every_days: 7`."""
    from datetime import UTC, datetime, timedelta

    from test_retro import _finished

    return _finished(fr.root, start=datetime.now(UTC) - timedelta(days=10))


def test_next_names_a_due_retro(fr, capsys):
    fr.install_pack()
    write(fr.root, "docs/idea.md", "# idea\n")
    assert main(["next"]) == 0
    assert "Retro due" not in capsys.readouterr().out
    _old_run(fr)
    assert main(["next"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "Lane: design-doc · Stage: Define"
    assert out[-1] == "Retro due: 1 finished runs since the start → agentflow retro"


def test_status_names_a_due_retro(fr, capsys):
    fr.install_pack()
    _old_run(fr)
    assert main(["status"]) == 0
    assert capsys.readouterr().out.splitlines()[-1] == "Retro due: 1 finished runs since the start → agentflow retro"


def test_status_of_one_run_does_not_name_a_retro(fr, capsys):
    fr.install_pack()
    run = fr.new_run()
    _old_run(fr)
    assert main(["status", run.id]) == 0
    assert "Retro due" not in capsys.readouterr().out


def _retro_queue(qrepo, monkeypatch, outcome=None, error=None):
    """The queue with `run_retro` replaced by a recorder; returns the list of its calls."""
    from agentflow import retro

    calls: list[tuple] = []

    def fake(repo, cfg, since, dry_run, now, out=None):
        calls.append((repo, since, dry_run))
        if error is not None:
            raise error
        return outcome or retro.RetroOutcome(None, None, "nothing to learn")

    monkeypatch.setattr(retro, "run_retro", fake)
    return calls


def test_queue_runs_a_due_retro_between_modules(qrepo, monkeypatch):
    from agentflow.retro import RetroOutcome

    _old_run(qrepo)
    calls = _retro_queue(qrepo, monkeypatch, RetroOutcome("r", "https://example.test/pr/1", "opened"))
    messages: list[str] = []
    go = Starter(qrepo)
    assert module_queue.run_queue(qrepo.cfg, qrepo.root, 2, go, once=True, log=messages.append) == 0
    assert len(calls) == 1 and calls[0][0] == qrepo.root and calls[0][2] is False
    assert go.started == ["platform"]
    assert any("retro" in m and "https://example.test/pr/1" in m for m in messages)


def test_queue_does_not_retro_when_none_is_due(qrepo, monkeypatch):
    calls = _retro_queue(qrepo, monkeypatch)
    assert module_queue.run_queue(qrepo.cfg, qrepo.root, 2, Starter(qrepo), once=True, log=lambda m: None) == 0
    assert calls == []


@pytest.mark.parametrize("failure", ["stopped", "raised"])
def test_a_failed_retro_never_stops_the_queue_and_is_tried_once(qrepo, monkeypatch, failure):
    from agentflow.retro import RetroOutcome

    _old_run(qrepo)
    stopped = RetroOutcome("r-1", None, "the retro session stopped: see `agentflow inbox` (r-1)")
    calls = _retro_queue(qrepo, monkeypatch, outcome=stopped if failure == "stopped" else None,
                         error=RuntimeError("boom") if failure == "raised" else None)
    messages: list[str] = []
    ticks: list[float] = []

    def sleep(seconds: float) -> None:
        ticks.append(seconds)
        if len(ticks) == 3:
            raise KeyboardInterrupt

    go = Starter(qrepo)
    assert module_queue.run_queue(qrepo.cfg, qrepo.root, 3, go, interval=1.0, sleep=sleep, log=messages.append) == 0
    assert len(calls) == 1  # three ticks, one attempt
    assert go.started[0] == "platform"  # the next module still started
    retro_lines = [m for m in messages if "retro" in m.lower()]
    assert len(retro_lines) == 1 and "\n" not in retro_lines[0]


def test_queue_opens_the_retro_pr_then_starts_the_module(qrepo, monkeypatch, tmp_path):
    from test_retro import KEY, PR_URL, SEQ, _add, _created, _retro_result, _retro_runs, _two_runs_failing

    monkeypatch.setenv("FAKE_GH_URL", PR_URL)
    a, _b = _two_runs_failing(qrepo, "unit")
    qrepo.fake.script({KEY: [_retro_result(add=[_add(sources=[f"{a}#{SEQ}"])])]})
    messages: list[str] = []
    go = Starter(qrepo)
    assert module_queue.run_queue(qrepo.cfg, qrepo.root, 2, go, once=True, log=messages.append) == 0
    assert len(_retro_runs(qrepo)) == 1 and go.started == ["platform"]
    assert len(_created(tmp_path / "gh.log")) == 1
    assert any(PR_URL in m for m in messages)
