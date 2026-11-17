from __future__ import annotations

import pytest

from agentflow.artifacts import NotFound
from agentflow.sessions import find_session, run_sessions, transcript_file


def started(run, key, pid, **kw):
    return run.emit("agent_started", step=key, pid=pid, agent="claude", model=kw.get("model", "sonnet"),
                    effort="high", role="build", session_id=kw.get("session_id"), sizer=kw.get("sizer", False))


def test_three_sessions_on_one_step_read_as_three_labels(flow_repo):
    run, k = flow_repo.new_run(), "build/build/task-9/implement"
    for n, pid in ((1, 11), (2, 12)):
        started(run, k, pid)
        run.emit("agent_session", session_id=f"a{n}", pid=pid, key=k, model="sonnet")
        run.emit("agent_exited", session_id=f"a{n}", exit_code=0 if n == 2 else 1, cost=0.5 * n, key=k)
        run.emit("step_finished", key=k, status="done" if n == 2 else "failed", summary=f"try {n}\nmore")
    started(run, f"{k}/resolve-1", 13, model="opus")
    run.emit("agent_session", session_id="r1", pid=13, key=f"{k}/resolve-1", model="opus")
    labels = [s["label"] for s in run_sessions(run)]
    assert labels == ["Task 9 · implement · resolver 1", "Task 9 · implement · attempt 2",
                      "Task 9 · implement · attempt 1"]
    rows = {s["label"]: s for s in run_sessions(run)}
    assert rows["Task 9 · implement · attempt 1"]["exit_code"] == 1
    assert rows["Task 9 · implement · attempt 1"]["summary"] == "try 1"
    assert rows["Task 9 · implement · attempt 2"]["cost"] == 1.0


def test_a_sizer_shares_attempt_ones_file_with_its_own_label(flow_repo):
    run, k = flow_repo.new_run(), "plan/plan"
    started(run, k, 21, sizer=True)
    started(run, k, 22)
    sizer, first = sorted(run_sessions(run), key=lambda s: s["id"])
    assert (sizer["label"], first["label"]) == ("plan · sizer", "plan · attempt 1")
    assert transcript_file(run, sizer) == transcript_file(run, first) == run.transcript_path(k, 1)


def test_a_session_that_died_before_init_is_matched_by_pid(flow_repo):
    run, k = flow_repo.new_run(), "define/draft"
    started(run, k, 31)
    run.emit("agent_exited", pid=31, exit_code=137, key=k)
    (row,) = run_sessions(run)
    assert row["session_id"] is None and row["exit_code"] == 137 and row["transcript"] is False and not row["live"]


def test_an_exit_with_neither_session_nor_pid_closes_the_open_session_of_its_key(flow_repo):
    run, k = flow_repo.new_run(), "define/draft"
    started(run, k, 61)
    started(run, "plan/plan", 62)
    run.emit("agent_exited", exit_code=2, key=k)
    rows = {r["key"]: r for r in run_sessions(run)}
    assert rows[k]["exit_code"] == 2 and rows["plan/plan"]["exit_code"] is None


def test_find_session_refuses_what_is_not_a_session(flow_repo):
    run = flow_repo.new_run()
    started(run, "plan/plan", 41)
    for sid in ("s999", "../x", "1", "s1/..", ""):
        with pytest.raises(NotFound):
            find_session(run, sid)


def test_a_resumed_session_counts_its_cost_once(flow_repo):
    run, k = flow_repo.new_run(), "build/build/task-1/implement"
    for cost in (0.4, 1.0):
        started(run, k, 51, session_id="same")
        run.emit("agent_exited", session_id="same", exit_code=0, cost=cost, key=k)
    assert sorted(s["cost"] for s in run_sessions(run)) == [0.4, 0.6]
