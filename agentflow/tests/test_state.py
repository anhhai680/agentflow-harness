from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from agentflow.events import EventError, EventLog, RunLock, lock_state
from agentflow.flow import _human_errors, main
from agentflow.state import (
    Run,
    RunNotFound,
    display_status,
    find_run,
    initial_state,
    list_runs,
    reduce,
    replay,
    runs_root,
    slugify,
)


def state_json(run: Run) -> dict:
    return json.loads((run.dir / "state.json").read_text(encoding="utf-8"))


def script(run: Run) -> None:
    """A run that goes through a phase: steps, an agent call, a gate, a result, the approval ask."""
    run.emit("phase_started", phase="plan", start_sha="abc123")
    run.emit("step_started", key="plan/writing-plans", phase="plan", agent="claude", model="opus", model_source="phase")
    run.emit("agent_started", agent="claude", pid=4242, model="opus", effort="medium", step="plan/writing-plans",
             session_id=None, sizing=True)
    run.emit("agent_session", key="plan/writing-plans", pid=4242, session_id="s-1", model="opus")
    run.emit("agent_turn", session_id="s-1", tokens=1000, context_pct=12.5, cost=0.0, last_tool="Read")
    run.emit("gate_result", key="plan/writing-plans", gate="floor", passed=True, skipped=False, log="g.log")
    run.emit("agent_exited", session_id="s-1", exit_code=0, cost=0.75)
    run.emit("step_finished", key="plan/writing-plans", status="done", summary="plan written")
    run.emit("some_future_event", payload={"x": 1})
    run.emit("phase_approval_raised", phase="plan", round=0)


def test_state_matches_replay(run):
    script(run)
    on_disk = state_json(run)
    assert on_disk == replay(run.events.read())
    assert on_disk == run.state
    assert on_disk["status"] == "awaiting_approval"
    assert on_disk["waiting"]["kind"] == "phase_approval"
    assert on_disk["waiting"]["key"] == "plan_approved"
    assert on_disk["spend_usd"] == 0.75
    assert on_disk["phases"]["plan"]["status"] == "awaiting_approval"
    assert on_disk["steps"]["plan/writing-plans"]["status"] == "done"
    assert on_disk["steps"]["plan/writing-plans"]["result"] == "plan written"
    assert on_disk["steps"]["plan/writing-plans"]["session_id"] == "s-1"
    assert on_disk["gates"]["plan/writing-plans"][0]["gate"] == "floor"
    assert on_disk["last_seq"] == len(run.events.read())


def test_unknown_events_are_kept_and_ignored(run):
    before = copy.deepcopy(run.state)
    run.emit("some_future_event", payload={"x": 1})
    assert run.events.read()[-1]["type"] == "some_future_event"
    after = dict(run.state)
    assert after.pop("last_seq") == before["last_seq"] + 1
    after.pop("updated_at")
    before.pop("last_seq")
    before.pop("updated_at")
    assert after == before


def test_reduce_is_pure():
    state = initial_state()
    event = {"seq": 1, "ts": "2026-10-29T10:00:00.000+00:00", "type": "run_started", "pipeline": "feature",
             "params": {"module": "crm"}, "run_id": "r1", "budget_usd": 25}
    snapshot = copy.deepcopy(state)
    out = reduce(state, event)
    assert state == snapshot
    assert out is not state
    assert out["pipeline"] == "feature" and out["params"] == {"module": "crm"} and out["status"] == "running"
    assert reduce(state, event) == out


def test_agent_rows(run):
    run.emit("step_started", key="build/task", phase="build")
    run.emit("agent_started", agent="claude", pid=77, model="sonnet", effort="medium", step="build/task",
             session_id=None, sizing=True)
    (key, row), = run.state["agents"].items()
    assert key == "77"
    assert row["status"] == "sizing" and row["effort"] == "medium" and row["pid"] == 77
    run.emit("agent_session", key="build/task", pid=77, session_id="s-9", model="sonnet")
    assert list(run.state["agents"]) == ["s-9"]
    run.emit("effort_changed", **{"from": "medium", "to": "high", "reason": "large task"})
    row = run.state["agents"]["s-9"]
    assert row["effort"] == "high" and row["effort_changed"] == "up"
    assert run.state["steps"]["build/task"]["effort"] == "high"
    assert run.state["steps"]["build/task"]["effort_changed"] == "up"
    assert run.state["effort_changes"][0]["to"] == "high"
    run.emit("agent_turn", session_id="s-9", tokens=5, context_pct=40.0, cost=0.1, last_tool="Edit")
    assert run.state["agents"]["s-9"]["context_pct"] == 40.0
    assert run.state["agents"]["s-9"]["last_tool"] == "Edit"
    run.emit("agent_exited", session_id="s-9", exit_code=0, cost=1.25)
    assert run.state["agents"] == {}
    (gone,) = run.state["exited"]
    assert gone["exit_code"] == 0 and gone["session_id"] == "s-9" and gone["effort"] == "high"
    assert run.state["spend_usd"] == 1.25


def test_effort_down_and_refused(run):
    run.emit("step_started", key="plan/x", phase="plan")
    run.emit("agent_started", agent="claude", pid=1, model="opus", effort="medium", step="plan/x", sizing=True)
    run.emit("effort_changed", key="plan/x", **{"from": "medium", "to": "low", "reason": "tiny"})
    assert run.state["steps"]["plan/x"]["effort_changed"] == "down"
    run.emit("effort_refused", key="plan/x", requested="max", reason="not allowed")
    assert run.state["steps"]["plan/x"]["effort_refused"] == {"requested": "max", "reason": "not allowed"}


def test_agent_exit_without_session_uses_pid_row(run):
    run.emit("agent_started", agent="claude", pid=5, model="opus", effort="medium", step="plan/x", sizing=False)
    run.emit("agent_exited", session_id=None, exit_code=1, cost=0.0)
    assert run.state["agents"] == {}
    assert run.state["exited"][0]["exit_code"] == 1


def test_compaction_and_subagents_on_the_row(run):
    run.emit("agent_started", agent="claude", pid=5, model="opus", effort="medium", step="plan/x", sizing=False,
             session_id="s-2")
    run.emit("compacted", session_id="s-2", trigger="auto", pre_tokens=900)
    assert run.state["agents"]["s-2"]["status"] == "compacting"
    run.emit("agent_turn", session_id="s-2", tokens=1, context_pct=3.0, cost=0.0, last_tool=None)
    assert run.state["agents"]["s-2"]["status"] == "running"
    run.emit("subagent_started", parent_session="s-2", subagent_type="Explore", model="haiku", tool_use_id="t1")
    run.emit("subagent_finished", parent_session="s-2", subagent_type="Explore", model="haiku", tool_use_id="t1")
    (sub,) = run.state["agents"]["s-2"]["subagents"]
    assert sub["subagent_type"] == "Explore" and sub["done"] is True
    run.emit("rolled_over", key="plan/x", session_id="s-2")
    assert run.state["steps"]["plan/x"]["rollovers"] == 1


def test_context_window_is_cached_per_model(run):
    run.emit("context_window", model="opus", tokens=1000000)
    assert run.state["context_windows"] == {"opus": 1000000}


def test_the_task_total_is_fixed_by_the_first_task_step(run):
    run.emit("phase_started", phase="build", start_sha="aaa")
    run.emit("step_started", key="build/build/task-1/implement", phase="build", task={"n": 1, "of": 4})
    run.emit("step_started", key="build/build/task-2/implement", phase="build", task={"n": 2, "of": 5})
    assert run.state["task"] == {"n": 2, "of": 5}  # the live task follows the event ...
    assert run.state["task_total"] == 4  # ... the total does not move once set
    run.emit("step_started", key="build/gates", phase="build")
    assert run.state["task"] is None and run.state["task_total"] == 4


def test_step_lifecycle(run):
    run.emit("phase_started", phase="spec", start_sha="aaa")
    run.emit("step_started", key="define/draft", phase="spec", task={"n": 2, "of": 7})
    assert run.state["phase"] == "spec" and run.state["step"] == "define/draft"
    assert run.state["task"] == {"n": 2, "of": 7}
    assert run.state["steps"]["define/draft"]["status"] == "running"
    run.emit("step_skipped", key="define/contracts", phase="spec", condition="spec_needs_api_design", reason="no routes")
    assert run.state["steps"]["define/contracts"]["status"] == "skipped"
    assert run.state["steps"]["define/contracts"]["skip_reason"] == "no routes"
    run.emit("step_retry", key="define/draft")
    assert run.state["steps"]["define/draft"]["status"] == "pending"
    run.emit("step_unchanged", step="define/draft", reason="not my concern")
    assert run.state["steps"]["define/draft"]["status"] == "unchanged"
    run.emit("permission_denied", key="define/draft", tool="Bash", input={"command": "rm"})
    run.emit("progress_not_updated", key="define/draft")
    run.emit("checkpoint_reused", key="define/draft")
    assert run.state["steps"]["define/draft"]["permission_denials"] == 1
    assert run.state["steps"]["define/draft"]["progress_not_updated"] is True
    assert run.state["steps"]["define/draft"]["checkpoint_reused"] is True
    run.emit("phase_skipped", phase="design", reason="no ui")
    assert run.state["phases"]["design"] == {"status": "skipped", "round": 0, "start_sha": None,
                                             "approval_sha": None, "skip_reason": "no ui"}
    run.emit("footer_mismatch", expected="plan", got="design")
    assert run.state["footer_mismatches"] == [{"phase": "spec", "expected": "plan", "got": "design"}]
    run.emit("review_result", key="build/task-1/review", verdict="approve", findings=[])
    assert run.state["reviews"]["build/task-1/review"]["verdict"] == "approve"
    run.emit("pr_opened", url="https://example.test/pr/1")
    assert run.state["pr_url"] == "https://example.test/pr/1"
    run.emit("run_updated", branch="flow/feature/crm", worktree="/w/x", base_sha="abc")
    assert run.state["branch"] == "flow/feature/crm" and run.state["worktree"] == "/w/x"
    assert run.state["base_sha"] == "abc"


def test_task_is_cleared_when_a_non_task_step_starts(run):
    run.emit("step_started", key="build/implement/task-1/implement", phase="build", task={"n": 1, "of": 3})
    run.emit("step_started", key="build/finish", phase="build")
    assert run.state["task"] is None


def test_run_control_events(run):
    run.emit("run_paused")
    assert run.state["status"] == "paused"
    run.emit("run_resumed")
    assert run.state["status"] == "running"
    run.emit("phase_approval_raised", phase="plan", round=0)
    run.emit("run_paused")
    run.emit("run_resumed")
    assert run.state["status"] == "awaiting_approval"
    run.emit("hitl_reminded", key="plan_approved")
    assert run.state["waiting"]["reminded"] == 1
    run.emit("run_failed", reason="budget")
    assert run.state["status"] == "failed" and run.state["failure"] == "budget"
    run.emit("run_cancelled")
    assert run.state["status"] == "cancelled"
    run.emit("run_finished")
    assert run.state["status"] == "done"


def test_hitl_events(run):
    run.emit("step_started", key="plan/grill", phase="plan")
    run.emit("hitl_raised", kind="needs_human", key="plan/grill", phase="plan", summary="need a call",
             questions=[{"id": "q1", "question": "?"}])
    assert run.state["status"] == "waiting_human"
    assert run.state["waiting"]["questions"] == [{"id": "q1", "question": "?"}]
    assert run.state["steps"]["plan/grill"]["status"] == "waiting_human"
    run.emit("hitl_answered", key="plan/grill", by="me", answers={"q1": "A"})
    assert run.state["status"] == "running" and run.state["waiting"] is None
    assert run.state["steps"]["plan/grill"]["status"] == "running"
    run.emit("hitl_raised", kind="checkpoint", key="plan/doubt_offer", phase="plan", summary="", questions=[])
    run.emit("checkpoint_cleared", key="plan/doubt_offer", by="me")
    assert run.state["waiting"] is None and run.state["status"] == "running"


def test_phase_approval_and_rejection_events(run):
    run.emit("phase_started", phase="plan", start_sha="s0")
    run.emit("step_started", key="plan/a", phase="plan")
    run.emit("step_started", key="spec/b", phase="spec")
    run.emit("phase_approval_raised", phase="plan", round=0, sha="s1")
    assert run.state["phases"]["plan"]["approval_sha"] == "s1"
    run.emit("phase_rejected", phase="plan", round=1, note="split task 3", by="me")
    assert run.state["status"] == "refining"
    assert run.state["refine"] == {"phase": "plan", "round": 1, "note": "split task 3"}
    assert run.state["steps"]["plan/a"]["status"] == "refine_pending"
    assert run.state["steps"]["plan/a"]["round"] == 1
    assert run.state["steps"]["spec/b"]["status"] == "running"
    assert run.state["phases"]["plan"]["status"] == "refining" and run.state["phases"]["plan"]["round"] == 1
    run.emit("phase_approval_raised", phase="plan", round=1, sha="s2")
    assert run.state["refine"] is None and run.state["status"] == "awaiting_approval"
    run.emit("phase_approved", phase="plan", by="me")
    assert run.state["status"] == "running" and run.state["waiting"] is None
    assert run.state["phases"]["plan"]["status"] == "approved"
    assert run.state["phases"]["plan"]["approval_sha"] == "s2"


def test_stalled_lock(run):
    assert lock_state(run.dir) == "idle"
    RunLock(run.dir).path.write_text("999999", encoding="utf-8")
    assert lock_state(run.dir) == "stalled"
    assert display_status(run) == "stalled"
    run.emit("phase_approval_raised", phase="plan", round=0)
    assert display_status(run) == "awaiting_approval"


def test_live_lock_shows_the_state_status(run):
    lock = RunLock(run.dir)
    assert lock.acquire()
    assert lock_state(run.dir) == "live"
    assert display_status(run) == "running"
    lock.release()
    assert lock_state(run.dir) == "idle"


def test_second_runner_refused(run):
    first = RunLock(run.dir)
    assert first.acquire() and not RunLock(run.dir).acquire()
    first.release()
    assert RunLock(run.dir).acquire()


def test_dead_pid_lock_is_reclaimed(run):
    RunLock(run.dir).path.write_text("999999", encoding="utf-8")
    lock = RunLock(run.dir)
    assert lock.acquire()
    assert lock.path.read_text(encoding="utf-8") == str(os.getpid())


def test_release_leaves_a_lock_it_does_not_own(run):
    RunLock(run.dir).path.write_text("999999", encoding="utf-8")
    RunLock(run.dir).release()
    assert RunLock(run.dir).path.exists()


def test_event_log_numbers_events_and_keeps_nfc(tmp_path):
    log = EventLog(tmp_path / "e" / "events.jsonl")
    nfd = "Có luu token"  # decomposed accent
    first = log.append("a", note=nfd)
    second = log.append("b", by="me", n=2)
    assert (first["seq"], second["seq"]) == (1, 2)
    assert first["ts"].endswith("+00:00") and first["by"] == "runner" and second["by"] == "me"
    assert log.read()[0]["note"] == "Có luu token"
    assert [e["type"] for e in log.read(after_seq=1)] == ["b"]
    assert log.last_seq() == 2
    with pytest.raises(ValueError):
        log.append("c", seq=9)


def test_event_log_survives_a_torn_last_line(tmp_path):
    path = tmp_path / "events.jsonl"
    log = EventLog(path)
    log.append("a")
    with path.open("a", encoding="utf-8") as out:
        out.write('{"seq": 2, "ty')
    assert [e["seq"] for e in log.read()] == [1]
    third = log.append("b")
    assert third["seq"] == 2
    assert [e["type"] for e in log.read()] == ["a", "b"]


def test_follow_yields_new_events_until_stop(tmp_path):
    log = EventLog(tmp_path / "events.jsonl")
    log.append("a")
    log.append("b")
    seen: list[str] = []
    state = {"calls": 0}

    def stop() -> bool:
        state["calls"] += 1
        if state["calls"] == 2:
            log.append("c")
        return state["calls"] >= 3

    for event in log.follow(after_seq=1, stop=stop, poll_s=0.01):
        seen.append(event["type"])
    assert seen == ["b", "c"]


def test_two_writers_never_reuse_a_seq(run, repo):
    other = Run.load(repo, run.id)
    run.emit("a_event")
    other.emit("b_event")
    run.emit("c_event")
    seqs = [e["seq"] for e in run.events.read()]
    assert seqs == sorted(set(seqs))
    assert state_json(run) == replay(run.events.read())
    assert run.state["last_seq"] == seqs[-1]


def test_load_catches_up_when_state_json_is_behind(run, repo):
    run.emit("phase_started", phase="plan", start_sha="a")
    (run.dir / "state.json").write_text(json.dumps(replay(run.events.read()[:1])), encoding="utf-8")
    loaded = Run.load(repo, run.id)
    assert loaded.state["phase"] == "plan"
    assert state_json(loaded) == replay(loaded.events.read())


def test_load_survives_a_deleted_state_json(run, repo):
    run.emit("phase_started", phase="plan", start_sha="a")
    (run.dir / "state.json").unlink()
    assert Run.load(repo, run.id).state == run.state


def test_load_unknown_run(repo):
    with pytest.raises(RunNotFound):
        Run.load(repo, "nope")


def test_run_create_and_paths(run, repo, cfg):
    assert run.id.endswith("-feature-crm")
    assert run.dir == runs_root(repo) / run.id
    assert runs_root(repo) == repo.resolve() / "devtools" / "agentflow" / "runs"
    started = run.events.read()[0]
    assert started["type"] == "run_started" and started["params"] == {"module": "crm"}
    assert run.state["run_id"] == run.id and run.state["budget_usd"] == cfg.defaults.run_budget_usd
    assert run.state["pipeline"] == "feature"
    key = "build/implement/task-3/implement"
    assert run.result_path(key) == run.dir / "results" / "build__implement__task-3__implement.json"
    assert run.inbox_path(key) == run.dir / "inbox" / "build__implement__task-3__implement.md"
    assert run.transcript_path(key, 2) == run.dir / "transcripts" / "build__implement__task-3__implement.2.jsonl"
    assert run.gate_log_dir(key) == run.dir / "gates" / "build__implement__task-3__implement"
    assert run.refine_note_path("plan", 1) == run.dir / "refine" / "plan-1.md"
    assert run.questions_path() == run.dir / "questions.md"
    assert run.progress_path() == run.agent_dir / "progress.md"  # the agent's notes live in its workspace
    assert run.agent_dir == repo.resolve() / "devtools" / "agentflow" / "workspaces" / run.id
    assert run.agent_result_path(key) == run.agent_dir / "out" / "build__implement__task-3__implement.json"
    assert run.handoff_path() == run.dir / "handoff.md"
    assert run.dir.is_absolute()


def test_run_create_rejects_unknown_pipeline_and_avoids_id_clashes(repo, cfg):
    with pytest.raises(ValueError):
        Run.create(cfg, repo, "nonsense", {})
    one = Run.create(cfg, repo, "feature", {"module": "crm"})
    two = Run.create(cfg, repo, "feature", {"module": "crm"})
    assert one.id != two.id and two.id.endswith("-2")


def test_slugify():
    assert slugify("Hỏi Mayai: đăng nhập!") == "hoi-mayai-dang-nhap"
    assert slugify("Đơn hàng") == "don-hang"
    assert len(slugify("x" * 100)) == 40
    assert slugify("???") == ""


def test_run_id_slug_comes_from_params(repo, cfg):
    topic = Run.create(cfg, repo, "design", {"topic": "Tách nhóm đơn hàng"})
    bug = Run.create(cfg, repo, "bug", {"issue": 42})
    assert topic.id.endswith("-design-tach-nhom-don-hang")
    assert bug.id.endswith("-bug-42")


def test_list_runs_and_find_run(repo, cfg):
    assert list_runs(repo) == []
    first = Run.create(cfg, repo, "feature", {"module": "crm"})
    second = Run.create(cfg, repo, "bug", {"issue": 7})
    (repo / "devtools" / "agentflow" / "runs" / "stray").mkdir()
    assert {r.id for r in list_runs(repo)} == {first.id, second.id}
    assert find_run(repo, first.id).id == first.id
    assert find_run(repo, "-bug-7").id == second.id
    with pytest.raises(RunNotFound):
        find_run(repo, "missing")
    with pytest.raises(RunNotFound):
        find_run(repo, "-")


def test_status_cli(repo, cfg, capsys):
    assert main(["status"]) == 0
    assert "No runs" in capsys.readouterr().out
    one = Run.create(cfg, repo, "feature", {"module": "crm"})
    one.emit("phase_started", phase="build", start_sha="a")
    one.emit("step_started", key="build/tdd", phase="build")
    one.emit("agent_exited", session_id=None, exit_code=0, cost=3.1)
    two = Run.create(cfg, repo, "bug", {"issue": 9})
    two.emit("phase_approval_raised", phase="debug", round=0)
    assert main(["status"]) == 0
    out = capsys.readouterr().out
    rows = [line for line in out.splitlines() if one.id in line or two.id in line]
    assert len(rows) == 2
    row_one = next(r for r in rows if one.id in r)
    # `running` with no runner holding the lock is shown as `stalled`
    assert "build" in row_one and "build/tdd" in row_one and "stalled" in row_one and "$3.10" in row_one
    row_two = next(r for r in rows if two.id in r)
    assert "debug" in row_two or "awaiting_approval" in row_two
    assert "awaiting_approval" in row_two


def test_status_cli_single_run_and_missing(repo, cfg, capsys):
    one = Run.create(cfg, repo, "feature", {"module": "crm"})
    one.emit("phase_approval_raised", phase="plan", round=0)
    assert main(["status", "feature-crm"]) == 0
    out = capsys.readouterr().out
    assert one.id in out and "plan_approved" in out
    assert main(["status", "no-such-run"]) == 1
    assert "no run matches" in capsys.readouterr().out


def test_state_module_does_not_write_outside_the_run_dir(run, repo):
    run.emit("step_started", key="plan/a", phase="plan")
    names = sorted(p.name for p in run.dir.iterdir())
    assert "state.json" in names and "events.jsonl" in names
    assert not any(Path(p).suffix == ".tmp" for p in names)


# ------------------------------------------------------------------ fix round 1


def test_stray_events_do_not_clear_a_phase_approval(run):
    run.emit("phase_approval_raised", phase="plan", round=0)
    before = copy.deepcopy(run.state["waiting"])
    run.emit("hitl_answered", key="build/x", by="me", answers={})
    run.emit("hitl_answered", key="plan_approved", by="me", answers={})
    run.emit("hitl_answered", by="me", answers={})
    run.emit("checkpoint_cleared", key="plan_approved", by="me")
    run.emit("checkpoint_cleared", key="build/x", by="me")
    assert run.state["waiting"] == before
    assert run.state["status"] == "awaiting_approval"
    run.emit("phase_approved", phase="plan", by="me")
    assert run.state["waiting"] is None and run.state["status"] == "running"


def test_hitl_raised_does_not_replace_a_pending_approval(run):
    run.emit("phase_approval_raised", phase="plan", round=0)
    run.emit("hitl_raised", kind="needs_human", key="build/x", phase="build", summary="late question",
             questions=[{"id": "q1", "question": "?"}])
    assert run.state["waiting"]["kind"] == "phase_approval"
    assert run.state["status"] == "awaiting_approval"
    assert [w["key"] for w in run.state["waiting_queue"]] == ["build/x"]
    assert state_json(run) == replay(run.events.read())
    run.emit("phase_approved", phase="plan", by="me")
    assert run.state["waiting"]["key"] == "build/x" and run.state["waiting"]["questions"][0]["id"] == "q1"
    assert run.state["status"] == "waiting_human" and run.state["waiting_queue"] == []


def test_queued_wait_is_promoted_after_a_rejection_too(run):
    run.emit("phase_approval_raised", phase="plan", round=0)
    run.emit("hitl_raised", kind="checkpoint", key="build/x", phase="build", summary="", questions=[])
    run.emit("phase_rejected", phase="plan", round=1, note="redo", by="me")
    assert run.state["refine"]["round"] == 1
    assert run.state["waiting"]["key"] == "build/x" and run.state["waiting_queue"] == []


def test_empty_lock_with_a_fresh_mtime_is_live(run):
    lock = RunLock(run.dir)
    lock.path.write_text("", encoding="utf-8")
    assert lock_state(run.dir) == "live"
    assert not RunLock(run.dir).acquire()
    assert lock.path.read_text(encoding="utf-8") == ""


def test_empty_lock_that_is_old_is_stalled_and_reclaimed(run):
    lock = RunLock(run.dir)
    lock.path.write_text("not a pid", encoding="utf-8")
    old = time.time() - 60
    os.utime(lock.path, (old, old))
    assert lock_state(run.dir) == "stalled"
    assert RunLock(run.dir).acquire()
    assert lock.path.read_text(encoding="utf-8") == str(os.getpid())


def test_a_new_lock_never_exists_without_its_pid(run, monkeypatch):
    seen: list[str] = []
    real_link = os.link

    def spying_link(src, dst, *args, **kwargs):
        seen.append(Path(src).read_text(encoding="utf-8"))
        return real_link(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "link", spying_link)
    assert RunLock(run.dir).acquire()
    assert seen == [str(os.getpid())]
    assert sorted(p.name for p in run.dir.glob("runner.lock*") if p.suffix == ".tmp") == []


def test_threads_racing_for_the_lock_have_one_winner(run):
    results: list[bool] = []
    barrier = threading.Barrier(8)

    def contend() -> None:
        barrier.wait()
        results.append(RunLock(run.dir).acquire())

    threads = [threading.Thread(target=contend) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count(True) == 1 and len(results) == 8


def test_processes_racing_for_the_lock_have_one_winner(run):
    root = Path(__file__).resolve().parents[2]
    code = (
        "import sys, time\n"
        f"sys.path.insert(0, {str(root)!r})\n"
        "from pathlib import Path\n"
        "from agentflow.events import RunLock\n"
        "while time.time() < float(sys.argv[2]):\n"
        "    pass\n"
        "print(RunLock(Path(sys.argv[1])).acquire(), flush=True)\n"
        "time.sleep(1.5)\n"
    )
    start = time.time() + 1.0
    procs = [subprocess.Popen([sys.executable, "-c", code, str(run.dir), str(start)], stdout=subprocess.PIPE, text=True)
             for _ in range(6)]
    outs = [p.communicate()[0].strip() for p in procs]
    assert outs.count("True") == 1 and outs.count("False") == 5


def test_emit_rejects_a_malformed_event_and_writes_nothing(run):
    before_events = run.events.read()
    before_state = copy.deepcopy(run.state)
    before_file = (run.dir / "state.json").read_text(encoding="utf-8")
    with pytest.raises(EventError) as err:
        run.emit("step_finished", status="done")
    assert "step_finished" in str(err.value) and "key" in str(err.value)
    assert run.events.read() == before_events
    assert run.state == before_state
    assert (run.dir / "state.json").read_text(encoding="utf-8") == before_file
    with pytest.raises(EventError, match="key.*step|step.*key"):
        run.emit("step_skipped", reason="x")
    with pytest.raises(EventError, match="context_window"):
        run.emit("context_window", model="opus", tokens="lots")
    with pytest.raises(EventError, match="phase"):
        run.emit("phase_started", phase=["plan"])
    assert run.events.read() == before_events
    run.emit("step_started", key="plan/a", phase="plan")  # the run still works
    assert run.state["step"] == "plan/a"


def test_reduce_skips_a_malformed_event_and_lists_it():
    good = {"seq": 1, "ts": "t1", "type": "run_started", "pipeline": "feature", "params": {}, "run_id": "r"}
    bad = {"seq": 2, "ts": "t2", "type": "step_finished", "status": "done"}
    after = {"seq": 3, "ts": "t3", "type": "phase_started", "phase": "plan"}
    state = replay([good, bad, after])
    assert state["phase"] == "plan" and state["last_seq"] == 3 and state["steps"] == {}
    (entry,) = state["malformed_events"]
    assert entry["seq"] == 2 and entry["type"] == "step_finished" and "key" in entry["error"]
    one = reduce(initial_state(), bad)
    assert one["malformed_events"][0]["seq"] == 2 and one["last_seq"] == 2


def test_a_hand_edited_bad_line_does_not_brick_loading(run, repo):
    with run.events.path.open("a", encoding="utf-8") as out:
        out.write(json.dumps({"seq": 2, "ts": "t", "type": "step_finished", "by": "hand", "status": "done"}) + "\n")
    (run.dir / "state.json").unlink()
    loaded = Run.load(repo, run.id)
    assert [m["seq"] for m in loaded.state["malformed_events"]] == [2]
    assert [r.id for r in list_runs(repo)] == [run.id]
    loaded.emit("phase_started", phase="plan", start_sha="a")
    assert loaded.state["phase"] == "plan" and loaded.state["last_seq"] == 3
    assert state_json(loaded) == replay(loaded.events.read())


def test_event_error_is_a_human_readable_cli_error():
    assert EventError in _human_errors()


# ------------------------------------------------------------------ parking (Task 8)


def test_parked_questions_never_take_the_wait_and_stay_answerable(run):
    q = [{"id": "q1", "question": "Bao lâu?"}]
    run.emit("step_parked", kind="needs_human", key="build/b/task-1/implement", summary="one", questions=q, paths=[])
    run.emit("step_parked", kind="needs_human", key="build/b/task-2/implement", summary="two", questions=q, paths=[])
    assert run.state["waiting"] is None and run.state["status"] == "running"
    assert list(run.state["parked"]) == ["build/b/task-1/implement", "build/b/task-2/implement"]
    assert run.state["steps"]["build/b/task-1/implement"]["status"] == "parked"

    run.emit("hitl_raised", kind="needs_human", key="build/b/task-1/implement", phase="build", summary="one",
             questions=q)  # no runnable task left: the first parked question becomes the wait
    assert run.state["waiting"]["key"] == "build/b/task-1/implement"
    assert list(run.state["parked"]) == ["build/b/task-2/implement"]

    run.emit("hitl_answered", key="build/b/task-2/implement", by="me", answers={"q1": "1 giờ"})
    assert run.state["waiting"]["key"] == "build/b/task-1/implement"  # answering a parked item keeps the wait
    assert run.state["parked"] == {} and run.state["steps"]["build/b/task-2/implement"]["status"] == "running"
    run.emit("hitl_answered", key="build/b/task-1/implement", by="me", answers={"q1": "2 giờ"})
    assert run.state["waiting"] is None and run.state["status"] == "running"
    assert state_json(run) == replay(run.events.read())


def test_reopened_step_is_resumable_and_rejection_drops_parked(run):
    run.emit("step_finished", key="build/b/task-1/review", status="done", summary="ok")
    run.emit("step_reopened", key="build/b/task-1/review", note="re-review")
    assert run.state["steps"]["build/b/task-1/review"]["status"] == "running"
    run.emit("step_parked", kind="needs_human", key="build/b/task-2/implement", summary="", questions=[])
    run.emit("phase_rejected", phase="build", round=1, note="redo")
    assert run.state["parked"] == {}
    assert run.state["steps"]["build/b/task-2/implement"]["status"] == "refine_pending"


def test_status_spend_includes_a_session_still_running():
    from agentflow.flow import _spend

    state = {"budget_usd": 25, "spend_usd": 1.0, "agents": {"a": {"spend": 0.5}, "b": {"spend": None}}}
    assert _spend(state) == "$1.50 / $25.00"
    assert _spend({"budget_usd": None, "spend_usd": 0.0, "agents": {}}) == "$0.00"


def test_a_hold_shows_while_its_runner_lives(run):
    held = {"key": "build/impl", "reason": "usage limit", "until": "2026-10-31T09:30:00+00:00"}
    run.emit("run_held", **held)
    assert run.state["hold"] == held
    assert display_status(run) == "stalled"  # no runner: a dead runner outranks its hold
    lock = run.lock()
    assert lock.acquire()
    try:
        assert display_status(run) == "held"
        assert run.state["status"] == "running"  # so the queue keeps the run's slot
    finally:
        lock.release()


@pytest.mark.parametrize("event, data", [
    ("run_hold_ended", {"key": "build/impl"}),
    ("agent_started", {"pid": 4242, "step": "build/impl", "agent": "claude"}),  # the runner died in the hold
])
def test_a_hold_ends(run, event, data):
    run.emit("run_held", key="build/impl", reason="outage", until="2026-10-31T09:30:00+00:00")
    run.emit(event, **data)
    assert run.state["hold"] is None


def test_a_resumed_session_adds_only_its_new_cost(run):
    run.emit("agent_exited", session_id="s-1", exit_code=0, cost=2.2046758)
    run.emit("agent_exited", session_id="s-1", exit_code=0, cost=2.3711376)  # total_cost_usd spans the resumes
    assert run.state["spend_usd"] == 2.371138


def test_a_resume_covers_the_call_the_runner_never_saw_end(run):
    run.emit("agent_exited", session_id="s-1", exit_code=0, cost=2.9689066)  # the dead runner sent no exit before it
    assert run.state["spend_usd"] == 2.968907


def test_an_estimate_prices_one_call_and_a_later_report_replaces_it(run):
    run.emit("agent_exited", session_id="s-1", exit_code=1, cost=0.5, cost_estimated=True)
    run.emit("agent_exited", session_id="s-1", exit_code=1, cost=0.25, cost_estimated=True)
    assert run.state["spend_usd"] == 0.75
    run.emit("agent_exited", session_id="s-1", exit_code=0, cost=1.0)
    assert run.state["spend_usd"] == 1.0


def test_sessions_add_up_separately(run):
    run.emit("agent_exited", session_id="s-1", exit_code=0, cost=0.5)
    run.emit("agent_exited", session_id="s-2", exit_code=0, cost=0.25)
    run.emit("agent_exited", session_id=None, exit_code=1, cost=0.25, cost_estimated=True)
    assert run.state["spend_usd"] == 1.0
