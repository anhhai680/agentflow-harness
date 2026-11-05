"""Ready and Mark done: an approved phase can rest until a human continues it or finishes the run."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from agentflow.flow import main
from agentflow.hitl import approve
from agentflow.module_queue import feature_runs
from agentflow.pipeline import RunOptions, advance, start
from agentflow.run_control import ControlError, archive, mark_done, pause, retry
from agentflow.state import Run

DONE = {"status": "done", "summary": "did it"}
PIPE = {"gate_sets": {"build": ["ok"], "verify": ["ok"]}}
BIN = Path(__file__).parent / "bin"
URL = "https://github.com/acme/repo/pull/42"
SPEC = "docs/specs/SPEC-demo.md"
SPEC_TEXT = "# Spec: `demo`\n\n**Status: approved** 2026-10-01\n\n## Objective\n\nA fixture.\n\n## Open Questions\n\nNone for this module.\n"
COMMITS_A_SPEC = {"define/draft": [{"write": {SPEC: SPEC_TEXT}, "result": DONE}]}


@pytest.fixture
def pipe(flow_repo, tmp_path, monkeypatch):
    flow_repo.configure(PIPE)
    flow_repo.gh_log = tmp_path / "gh.jsonl"
    monkeypatch.setenv("PATH", f"{BIN}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_GH_LOG", str(flow_repo.gh_log))
    monkeypatch.setenv("FAKE_GH_URL", URL)
    return flow_repo


def begin(flow_repo, **opts) -> Run:
    return start(flow_repo.cfg, flow_repo.root, "feature", {"module": "demo"}, RunOptions(**opts))


def go(flow_repo, run: Run) -> str:
    return advance(run, flow_repo.cfg, RunOptions())


def steps_called(flow_repo) -> list[str]:
    return [c["step"] for c in flow_repo.calls()]


def pr_creates(flow_repo) -> list[list[str]]:
    if not flow_repo.gh_log.exists():
        return []
    calls = [json.loads(line) for line in flow_repo.gh_log.read_text(encoding="utf-8").splitlines() if line]
    return [c for c in calls if c[:2] == ["pr", "create"]]


def ready_after_define(flow_repo, **opts) -> Run:
    """A run whose define phase committed something, was approved, and now rests as ready."""
    flow_repo.script(COMMITS_A_SPEC)
    run = begin(flow_repo, **opts)
    assert go(flow_repo, run) == "awaiting_approval"
    approve(run, "define", by="me", hold=True)
    return run


# --------------------------------------------------------------------------- ready


def test_a_held_approval_rests_the_run_on_ready(pipe):
    run = ready_after_define(pipe)
    assert run.state["status"] == "ready"
    assert run.state["ready"]["phase"] == "define"
    assert run.state["waiting"] is None
    assert run.state["phases"]["define"]["status"] == "approved"
    assert [e["by"] for e in pipe.events(run, "phase_ready")] == ["me"]


def test_resuming_a_ready_run_starts_the_next_phase(pipe):
    run = ready_after_define(pipe)
    assert not [s for s in steps_called(pipe) if s.startswith("plan/")]
    assert go(pipe, run) == "awaiting_approval"
    assert run.state["waiting"]["phase"] == "plan"
    assert run.state["ready"] is None
    assert "plan/plan" in steps_called(pipe)


def test_continuing_goes_past_the_runs_to_phase(pipe):
    run = ready_after_define(pipe, to_phase="define")
    assert go(pipe, run) == "awaiting_approval"
    assert run.state["waiting"]["phase"] == "plan"
    assert run.state["status"] != "done"


def test_a_plain_approval_still_ends_at_the_runs_to_phase(pipe):
    run = begin(pipe, to_phase="define")
    go(pipe, run)
    approve(run, "define", by="me")
    assert go(pipe, run) == "done"
    assert pr_creates(pipe) == []


def test_hold_does_not_rest_a_stop_inside_a_phase(pipe):
    run = begin(pipe)
    run.emit("hitl_raised", kind="blocked", key="define/spec", summary="stuck")
    approve(run, "define/spec", by="me", hold=True)
    assert run.state["status"] == "running"
    assert run.state["ready"] is None


def test_a_held_approval_rests_a_paused_run_too(pipe):
    run = begin(pipe, to_phase="define")
    go(pipe, run)
    pause(run, "me")
    assert approve(run, "define", by="me", hold=True) is True  # the dashboard starts no runner on this
    assert run.state["status"] == "ready"
    assert run.state["ready"]["phase"] == "define"


def test_cli_no_continue_lands_on_ready_and_resume_continues(pipe):
    run = begin(pipe)
    go(pipe, run)
    assert main(["approve", run.id, "define", "--no-continue"]) == 0
    assert Run.load(pipe.root, run.id).state["status"] == "ready"
    assert main(["resume", run.id]) == 0
    assert Run.load(pipe.root, run.id).state["waiting"]["phase"] == "plan"


def test_cli_approve_still_continues_on_its_own(pipe):
    run = begin(pipe)
    go(pipe, run)
    assert main(["approve", run.id, "define"]) == 0
    assert Run.load(pipe.root, run.id).state["waiting"]["phase"] == "plan"


@pytest.mark.parametrize("action", [pause, lambda run, by: retry(run, None, by)])
def test_a_ready_run_refuses_pause_and_retry(pipe, action):
    run = ready_after_define(pipe)
    with pytest.raises(ControlError, match="ready"):
        action(run, "me")
    assert run.state["status"] == "ready"


# --------------------------------------------------------------------------- mark done


def test_mark_done_skips_to_ship_and_opens_one_draft_pr(pipe, git):
    run = ready_after_define(pipe)
    mark_done(run, by="me")
    assert go(pipe, run) == "done"
    assert run.state["pr_url"] == URL
    assert {e["phase"]: e["reason"] for e in pipe.events(run, "phase_skipped")} == {
        phase: "marked done after define" for phase in ("plan", "build", "verify", "review")
    }
    assert all(step.startswith("define/") for step in steps_called(pipe))
    (create,) = pr_creates(pipe)
    assert "--draft" in create
    assert create[create.index("--head") + 1] == "flow/feature/demo"
    assert "flow/feature/demo" in git(pipe.origin, "branch", "--list")


def test_the_pr_body_says_where_the_run_was_marked_done(pipe):
    run = ready_after_define(pipe)
    mark_done(run, by="me")
    go(pipe, run)
    body = (run.dir / "pr.md").read_text(encoding="utf-8")
    assert "Marked done after `define`" in body
    assert "phase `verify`: marked done after define" in body


def test_mark_done_ships_a_run_started_with_to(pipe):
    run = ready_after_define(pipe, to_phase="define")
    mark_done(run, by="me")
    assert go(pipe, run) == "done"
    assert run.state["pr_url"] == URL


def test_mark_done_with_a_dirty_tree_stops_and_ships_after_it_is_cleaned(pipe):
    run = ready_after_define(pipe)
    mark_done(run, by="me")
    stray = Path(run.state["worktree"]) / "stray.txt"
    stray.write_text("wip\n", encoding="utf-8")
    assert go(pipe, run) == "waiting_human"
    assert run.state["waiting"]["kind"] == "dirty_tree"
    assert pr_creates(pipe) == []
    stray.unlink()
    approve(run, run.state["waiting"]["key"], by="me")
    assert go(pipe, run) == "done"
    assert len(pr_creates(pipe)) == 1


def test_mark_done_with_a_failing_gh_stops_and_opens_one_pr_on_retry(pipe, monkeypatch):
    run = ready_after_define(pipe)
    mark_done(run, by="me")
    monkeypatch.setenv("FAKE_GH_FAIL", "1")
    assert go(pipe, run) == "waiting_human"
    assert run.state["waiting"]["kind"] == "blocked"
    assert run.state["status"] != "done" and run.state["pr_url"] is None
    monkeypatch.delenv("FAKE_GH_FAIL")
    approve(run, run.state["waiting"]["key"], by="me")
    assert go(pipe, run) == "done"
    assert run.state["pr_url"] == URL
    assert len(pipe.events(run, "pr_opened")) == 1


def test_the_cli_marks_a_ready_run_done(pipe):
    run = ready_after_define(pipe)
    assert main(["resume", run.id, "--mark-done", "--by", "tester"]) == 0
    run = Run.load(pipe.root, run.id)
    assert run.state["status"] == "done" and run.state["pr_url"] == URL
    assert [e["by"] for e in pipe.events(run, "run_marked_done")] == ["tester"]


def test_mark_done_refuses_a_run_that_is_not_ready(pipe):
    run = begin(pipe)
    go(pipe, run)
    with pytest.raises(ControlError, match="not ready"):
        mark_done(run, by="me")
    assert run.state["status"] == "awaiting_approval"


def test_mark_done_refuses_a_missing_worktree(pipe):
    run = ready_after_define(pipe)
    run.emit("run_updated", worktree=str(pipe.root / "gone"))
    with pytest.raises(ControlError, match="worktree"):
        mark_done(run, by="me")
    assert run.state["status"] == "ready"


def test_mark_done_refuses_a_branch_with_nothing_to_ship(pipe):
    run = begin(pipe)  # the fake agent writes nothing, so define commits nothing
    go(pipe, run)
    approve(run, "define", by="me", hold=True)
    with pytest.raises(ControlError, match="nothing to"):
        mark_done(run, by="me")
    assert run.state["status"] == "ready"


def test_a_marked_run_survives_a_lost_runner(pipe):
    """The decision is in the log, so a plain resume finishes it when the spawned runner never ran."""
    run = ready_after_define(pipe)
    mark_done(run, by="me")
    assert main(["resume", run.id]) == 0
    assert Run.load(pipe.root, run.id).state["pr_url"] == URL


def test_a_module_marked_done_at_define_has_not_started(pipe):
    run = ready_after_define(pipe)
    mark_done(run, by="me")
    go(pipe, run)
    assert "demo" not in feature_runs(pipe.cfg, pipe.root)


def test_a_module_marked_done_after_define_has_started(pipe):
    run = ready_after_define(pipe)
    assert go(pipe, run) == "awaiting_approval"
    approve(run, "plan", by="me", hold=True)
    mark_done(run, by="me")
    go(pipe, run)
    assert feature_runs(pipe.cfg, pipe.root)["demo"].id == run.id


def stopped_after_define(flow_repo) -> Run:
    """A run that ended at `define` through `--to`: a spec-only run, as the old cards in Define are."""
    flow_repo.script(COMMITS_A_SPEC)
    run = begin(flow_repo, to_phase="define")
    assert go(flow_repo, run) == "awaiting_approval"
    approve(run, "define", by="me")
    assert go(flow_repo, run) == "done"
    return run


def test_archiving_a_spec_only_run_leaves_its_module_not_started(pipe):
    run = stopped_after_define(pipe)
    archive(run, by="me")
    assert run.state["status"] == "done" and run.state["archived"]["by"] == "me"
    assert "demo" not in feature_runs(pipe.cfg, pipe.root)


def test_a_run_still_going_cannot_be_archived(pipe):
    run = begin(pipe)
    go(pipe, run)
    with pytest.raises(ControlError, match="only a finished run"):
        archive(run, by="me")
    assert pipe.events(run, "run_archived") == []


def test_a_run_is_archived_once(pipe):
    run = stopped_after_define(pipe)
    archive(run, by="me")
    with pytest.raises(ControlError, match="already archived"):
        archive(run, by="me")
    assert len(pipe.events(run, "run_archived")) == 1


def test_the_cli_archives_a_finished_run(pipe):
    run = stopped_after_define(pipe)
    assert main(["archive", run.id, "--by", "tester"]) == 0
    assert [e["by"] for e in pipe.events(Run.load(pipe.root, run.id), "run_archived")] == ["tester"]
    assert main(["archive", run.id]) == 1  # refused, and said so, rather than a traceback


def test_continuing_past_to_define_starts_the_module(pipe):
    run = ready_after_define(pipe, to_phase="define")
    assert "demo" not in feature_runs(pipe.cfg, pipe.root)
    go(pipe, run)
    assert feature_runs(pipe.cfg, pipe.root)["demo"].id == run.id


# --------------------------------------------------------------------------- finish never pushes the target


def test_finish_refuses_to_push_the_base_branch(pipe, git):
    run = begin(pipe, from_phase="ship")
    run.emit("run_updated", branch=pipe.cfg.project.base_branch)
    before = git(pipe.origin, "rev-parse", "main")
    assert go(pipe, run) == "waiting_human"
    assert run.state["waiting"]["kind"] == "blocked"
    assert "base branch" in run.state["waiting"]["summary"]
    assert git(pipe.origin, "rev-parse", "main") == before
    assert pr_creates(pipe) == []
