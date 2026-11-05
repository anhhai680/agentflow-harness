"""Run control, reminders and the terminal views (Task 13): pause, cancel, retry, attach, remind_due,
`board`/`agents_view` and their terminal tables."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agentflow import hitl
from agentflow.events import RunLock
from agentflow.flow import main
from agentflow.lanes import Executor
from agentflow.pipeline import RunOptions, advance, start
from agentflow.run_control import ControlError, cancel, pause, retry
from agentflow.state import Run
from agentflow.views import agents_table, agents_view, board, board_table, watch

DONE = {"status": "done", "summary": "did it"}
PIPE = {"gate_sets": {"build": ["ok"], "verify": ["ok"]}}
FEATURE_PHASES = ["define", "plan", "build", "verify", "review", "ship"]


@pytest.fixture
def pipe(flow_repo):
    flow_repo.configure(PIPE)
    return flow_repo


def begin(flow_repo, params: dict | None = None) -> Run:
    return start(flow_repo.cfg, flow_repo.root, "feature", params or {"module": "demo"}, RunOptions())


def go(flow_repo, run: Run) -> str:
    return advance(run, flow_repo.cfg, RunOptions())


def steps_called(flow_repo) -> list[str]:
    return [c["step"] for c in flow_repo.calls()]


def wait_until(check, timeout: float = 15.0, every: float = 0.02):
    """Poll `check()` until it returns something truthy; fail the test at the deadline."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(every)
    raise AssertionError(f"condition not met within {timeout} s")


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def raw_event(run: Run, **event) -> None:
    """Append an event line as another writer would, with a timestamp of our choosing."""
    seq = run.events.last_seq() + 1
    with run.events.path.open("a", encoding="utf-8") as out:
        out.write(json.dumps({"seq": seq, "by": "runner", **event}) + "\n")
    run.refresh()


# --------------------------------------------------------------------------- pause


def test_pause_stops_before_next_step(pipe, monkeypatch):
    run = begin(pipe)
    real_call = Executor._call
    paused: list[str] = []

    def call_then_pause(self, sr, turn):
        if not paused:  # a second actor pauses the run while this step's agent works
            paused.append(sr.key)
            pause(Run.load(pipe.root, run.id), by="me")
        return real_call(self, sr, turn)

    monkeypatch.setattr(Executor, "_call", call_then_pause)
    assert go(pipe, run) == "paused"
    run.refresh()
    assert paused == ["define/idea/idea_questions"]
    assert run.state["status"] == "paused"
    assert run.state["steps"]["define/idea/idea_questions"]["status"] == "done"  # the live agent finished its step
    assert (run.state["steps"].get("define/idea/idea_answers") or {}).get("status") in (None, "pending")
    assert pipe.events(run, "phase_approval_raised") == []
    assert [e["by"] for e in pipe.events(run, "run_paused")] == ["me"]
    assert steps_called(pipe) == ["define/idea/idea_questions"]

    assert go(pipe, run) == "awaiting_approval"  # resume goes on with the next step, not the finished one
    assert [e["by"] for e in pipe.events(run, "run_resumed")] == ["runner"]
    assert steps_called(pipe).count("define/idea/idea_questions") == 1
    assert run.state["waiting"]["key"] == "define_approved"


def test_pause_refuses_paused_and_finished_runs(pipe):
    run = pipe.new_run()
    pause(run, by="me")
    with pytest.raises(ControlError, match="already paused"):
        pause(run, by="me")
    run.emit("run_cancelled", by="me")
    with pytest.raises(ControlError, match="cancelled"):
        pause(run, by="me")


# --------------------------------------------------------------------------- cancel


def _live_pid(run: Run) -> int | None:
    run.refresh()
    for row in run.state["agents"].values():
        if row.get("pid") and row.get("session_id"):  # the agent has exec'd and reported its session
            return int(row["pid"])
    return None


def test_cancel_kills_agent_pid(pipe):
    pipe.script({"define/idea/idea_questions": [{"sleep": 60, "result": DONE}]})
    run = begin(pipe)
    outcome: dict = {}
    worker = threading.Thread(target=lambda: outcome.update(status=go(pipe, run)))
    worker.start()
    watcher = Run.load(pipe.root, run.id)
    pid = wait_until(lambda: _live_pid(watcher))
    assert alive(pid)

    killed = cancel(watcher, by="me", cfg=pipe.cfg)
    worker.join(timeout=20)
    assert not worker.is_alive()
    assert killed == [pid]
    assert outcome["status"] == "cancelled"
    assert not alive(pid)
    (event,) = pipe.events(watcher, "run_cancelled")
    assert event["by"] == "me" and event["killed"] == [pid]
    assert steps_called(pipe) == ["define/idea/idea_questions"]  # no reminder call after the kill
    assert pipe.events(watcher, "hitl_raised") == []
    assert Run.load(pipe.root, run.id).state["status"] == "cancelled"
    with pytest.raises(ControlError, match="cancelled"):
        cancel(watcher, by="me", cfg=pipe.cfg)


def test_cancel_never_signals_a_process_it_cannot_verify(pipe):
    run = pipe.new_run()
    stranger = subprocess.Popen(["sleep", "30"], start_new_session=True)  # not an agent command
    reused = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", "fake_agent.py"],
                              start_new_session=True)  # looks like the agent, but started after the record
    try:
        run.emit("agent_started", pid=stranger.pid, step="define/idea/idea_questions", agent="claude")
        run.emit("agent_started", pid=1, step="define/idea/idea_questions", agent="claude")
        run.emit("agent_started", pid=os.getpid(), step="define/idea/idea_questions", agent="claude")
        raw_event(run, ts="2020-01-01T00:00:00.000+00:00", type="agent_started", pid=reused.pid,
                  step="define/idea/idea_questions", agent="claude")
        killed = cancel(run, by="me", cfg=pipe.cfg)
        assert killed == []
        assert stranger.poll() is None and reused.poll() is None
        (event,) = pipe.events(run, "run_cancelled")
        reasons = {item["pid"]: item["reason"] for item in event["skipped"]}
        assert set(reasons) == {stranger.pid, 1, os.getpid(), reused.pid}
        assert "command" in reasons[stranger.pid]
        assert "started after" in reasons[reused.pid]
        assert run.state["status"] == "cancelled"
    finally:
        for proc in (stranger, reused):
            proc.send_signal(signal.SIGKILL)
            proc.wait()


def test_cancelled_run_stays_cancelled(pipe):
    run = pipe.new_run()
    cancel(run, by="me", cfg=pipe.cfg)
    run.emit("hitl_raised", kind="blocked", key="build/x", summary="late stop")
    run.emit("phase_approval_raised", phase="build", round=0)
    assert run.state["status"] == "cancelled"
    assert run.state["waiting"] is None and run.state["waiting_queue"] == []  # nothing to act on any more
    with pytest.raises(hitl.NotWaiting, match="cancelled"):
        hitl.approve(run, "build", by="me")
    with pytest.raises(hitl.NotWaiting, match="cancelled"):
        hitl.reject(run, "build", "me", "too late")
    assert go(pipe, run) == "cancelled"


def test_a_wait_raised_before_the_cancel_cannot_be_acted_on(pipe):
    run = pipe.new_run()
    run.emit("phase_started", phase="plan")
    run.emit("phase_approval_raised", phase="plan", round=0)
    cancel(run, by="me", cfg=pipe.cfg)
    before = run.events.last_seq()
    for act in (lambda: hitl.approve(run, "plan", by="me"), lambda: hitl.reject(run, "plan", "me", "no")):
        with pytest.raises(hitl.NotWaiting, match="cancelled"):
            act()
    assert run.events.last_seq() == before  # nothing recorded


# --------------------------------------------------------------------------- retry


def test_retry_failed_step_resumes(pipe, git):
    pipe.script({"define/idea/idea_questions": [{"write": {"CONSTRAINTS.md": "weaker\n"}, "result": DONE}, {"result": DONE}]})
    run = begin(pipe)
    assert go(pipe, run) == "waiting_human"
    assert run.state["steps"]["define/idea/idea_questions"]["status"] == "failed"
    first = pipe.events(run, "agent_session")[0]["session_id"]
    git(Path(run.state["worktree"]), "checkout", "--", "CONSTRAINTS.md")  # the human reverts the violation

    assert retry(run, None, by="me") == "define/idea/idea_questions"  # default: the failed step
    (event,) = pipe.events(run, "step_retry")
    assert event["key"] == "define/idea/idea_questions" and event["by"] == "me"
    assert run.state["waiting"] is None and run.state["steps"]["define/idea/idea_questions"]["status"] == "pending"

    assert go(pipe, run) == "awaiting_approval"
    calls = [c for c in pipe.calls() if c["step"] == "define/idea/idea_questions"]
    assert len(calls) == 2 and "--resume" not in calls[1]["argv"]  # a fresh session
    assert pipe.events(run, "agent_session")[-1]["session_id"] != first
    assert run.state["steps"]["define/idea/idea_questions"]["status"] == "done"


def test_retry_keeps_the_guard_on_an_unreverted_violation(pipe):
    pipe.script({"define/idea/idea_questions": [{"write": {"CONSTRAINTS.md": "weaker\n"}, "result": DONE}]})
    run = begin(pipe)
    assert go(pipe, run) == "waiting_human"
    retry(run, None, by="me")
    retry(run, "define/idea/idea_questions", by="me")  # a second retry does not drop the failed step's check
    assert go(pipe, run) == "waiting_human"
    assert len(pipe.calls()) == 1  # the restart saw the uncommitted violation before calling an agent
    kinds = [e["kind"] for e in pipe.events(run, "hitl_raised")]
    assert kinds == ["guard_violation", "guard_violation"]


def test_retry_refusals(pipe):
    run = pipe.new_run()
    with pytest.raises(ControlError, match="no failed step"):
        retry(run, None, by="me")
    with pytest.raises(ControlError, match="no step"):
        retry(run, "build/nope", by="me")
    run.emit("step_started", key="build/x", phase="build")
    lock = run.lock()
    assert lock.acquire()
    try:
        with pytest.raises(ControlError, match="runner is working"):
            retry(run, "build/x", by="me")
    finally:
        lock.release()


# --------------------------------------------------------------------------- CLI wiring


def test_cli_pause_cancel_and_retry(pipe, capsys):
    run = pipe.new_run()
    assert main(["pause", run.id, "--by", "tester"]) == 0
    assert "paused" in capsys.readouterr().out
    assert main(["pause", run.id]) == 1
    assert "already paused" in capsys.readouterr().out
    assert main(["retry", run.id, "--no-continue"]) == 1
    assert "no failed step" in capsys.readouterr().out
    assert main(["cancel", run.id, "--by", "tester"]) == 0
    assert "cancelled" in capsys.readouterr().out
    run.refresh()
    assert [e["by"] for e in pipe.events(run, "run_cancelled")] == ["tester"]


def test_cli_attach_resumes_the_session_interactively(pipe, monkeypatch, capsys):
    from agentflow import run_control

    run = begin(pipe)
    pipe.script({"define/idea/idea_questions": [{"result": {"status": "blocked", "summary": "stuck"}}]})
    assert go(pipe, run) == "waiting_human"
    session = run.state["steps"]["define/idea/idea_questions"]["session_id"]
    seen: list[dict] = []
    real_run = subprocess.run

    def fake_run(argv, *args, **kw):
        if "--resume" not in argv:
            return real_run(argv, *args, **kw)
        seen.append({"argv": argv, **kw})
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(run_control.subprocess, "run", fake_run)
    assert main(["attach", run.id]) == 0
    (call,) = seen
    assert call["argv"][-4:] == ["--resume", session, "--effort", "medium"]
    assert call["cwd"] == run.state["worktree"]
    assert "-p" not in call["argv"]
    assert f"agentflow resume {run.id}" in capsys.readouterr().out


# --------------------------------------------------------------------------- reminders


def test_reminder_after_hours(pipe, monkeypatch):
    pipe.configure({**PIPE, "hitl": {"remind_after_h": 4}})
    run = pipe.new_run()
    hitl.raise_hitl(pipe.cfg, run, "blocked", "build/x", "Bị chặn")
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(hitl, "notify_run", lambda cfg, r, message: sent.append((r.id, message)))
    later = datetime.now(UTC) + timedelta(hours=5)

    assert hitl.remind_due(pipe.cfg, pipe.root, later - timedelta(hours=2)) == []  # 3 h: not yet
    assert hitl.remind_due(pipe.cfg, pipe.root, later) == [run.id]
    assert len(sent) == 1 and "build/x" in sent[0][1]
    assert hitl.remind_due(pipe.cfg, pipe.root, later) == []  # called again at once: no second reminder
    assert len(sent) == 1
    run.refresh()
    assert [e["key"] for e in pipe.events(run, "hitl_reminded")] == ["build/x"]
    assert run.state["status"] == "waiting_human" and run.state["waiting"]["key"] == "build/x"
    assert hitl.remind_due(pipe.cfg, pipe.root, later + timedelta(hours=4, minutes=1)) == [run.id]  # since the last one
    assert len(sent) == 2


# --------------------------------------------------------------------------- board and agents data


def _agent(run: Run, key: str, session: str, model: str = "sonnet", role: str = "build", effort: str = "medium") -> None:
    run.emit("step_started", key=key, phase=key.split("/")[0], agent="claude", model=model, model_source="phase",
             effort=effort)
    run.emit("agent_started", pid=os.getpid(), step=key, agent="claude", model=model, effort=effort,
             role=role, phase=key.split("/")[0])
    run.emit("agent_session", session_id=session, pid=os.getpid(), key=key, model=model)
    run.emit("agent_turn", session_id=session, context_pct=42.0, cost=0.0, last_tool="Edit src/app.py")


def fixture_runs(flow_repo) -> tuple[Run, Run]:
    planning = flow_repo.new_run({"module": "staff-auth"})
    planning.emit("phase_started", phase="plan")
    planning.emit("step_started", key="plan/plan", phase="plan")
    planning.emit("phase_approval_raised", phase="plan", round=0, summary="Phase plan is done")
    building = flow_repo.new_run({"module": "crm"})
    building.emit("phase_started", phase="build")
    _agent(building, "build/build/task-3/implement", "s-crm")
    building.emit("step_started", key="build/build/task-3/implement", phase="build", task={"n": 3, "of": 7})
    building.emit("gate_result", key="build/build/task-3/implement", gate="floor", passed=True)
    return planning, building


def test_board_cards_for_fixture_runs(pipe):
    planning, building = fixture_runs(pipe)
    queue = pipe.root / "devtools" / "agentflow" / "runs" / "queue.json"
    queue.write_text(json.dumps({"modules": [
        {"id": "settings", "deps": ["platform"], "status": "backlog", "run_id": None, "blocked": "waiting on platform"},
        {"id": "crm", "deps": [], "status": "running", "run_id": building.id},
    ]}), encoding="utf-8")

    lock = RunLock(building.dir)  # its runner is live: a `running` run with no runner shows as `stalled`
    assert lock.acquire()
    try:
        data = board(pipe.root, pipe.cfg)
    finally:
        lock.release()
    assert data["columns"] == ["Backlog", *FEATURE_PHASES, "Done"]
    cards = {c["run_id"]: c for c in data["cards"]}
    assert cards[planning.id]["column"] == "plan" and cards[planning.id]["status"] == "awaiting_approval"
    assert cards[planning.id]["module"] == "staff-auth" and cards[planning.id]["agent"] is None
    crm = cards[building.id]
    assert crm["column"] == "build" and crm["task"] == "3/7" and crm["status"] == "running"
    assert crm["agent"]["model"] == "sonnet" and crm["agent"]["effort"] == "medium"
    assert crm["context_pct"] == 42.0
    assert crm["gates"] == [{"gate": "floor", "passed": True, "skipped": False}]
    assert crm["time_in_phase_s"] >= 0
    assert [(i["run_id"], i["key"]) for i in data["needs_human"]] == [(planning.id, "plan_approved")]
    assert [b["id"] for b in data["backlog"]] == ["settings"] and data["backlog"][0]["deps"] == ["platform"]
    assert data["counts"] == {"awaiting_approval": 1, "running": 1}
    assert data["agents_running"]["total"] == 1


def test_board_done_and_other_lanes(pipe):
    done = pipe.new_run({"module": "old"})
    done.emit("phase_started", phase="ship")
    done.emit("run_finished")
    bug = Run.create(pipe.cfg, pipe.root, "bug", {"issue": "42"})
    data = board(pipe.root, pipe.cfg)
    cards = {c["run_id"]: c for c in data["cards"]}
    assert cards[done.id]["column"] == "Done"
    assert cards[bug.id]["lane"] == "bug"  # one board for every lane
    assert [c["run_id"] for c in board(pipe.root, pipe.cfg, "bug")["cards"]] == [bug.id]
    with pytest.raises(ValueError, match="unknown lane"):
        board(pipe.root, pipe.cfg, "nope")


def test_agents_rows(pipe):
    planning, building = fixture_runs(pipe)
    _agent(planning, "plan/plan", "s-plan", model="opus", role="plan")
    view = agents_view(pipe.root, pipe.cfg)
    rows = {r["run_id"]: r for r in view["live"]}
    assert len(view["live"]) == 2
    crm = rows[building.id]
    assert (crm["phase"], crm["step"], crm["role"], crm["model"], crm["effort"]) == (
        "build", "build/task-3/implement", "build", "sonnet", "medium")
    assert crm["where"] == "feature · build › task 3/7 implement"
    assert crm["place"] == "build › task 3/7 implement"  # the dashboard's column: no lane
    assert crm["model_source"] == "phase" and crm["agent"] == "claude"
    assert crm["last_activity"] and crm["idle"] is False
    assert rows[planning.id]["model"] == "opus" and rows[planning.id]["role"] == "plan"

    building.emit("effort_changed", session_id="s-crm", key="build/build/task-3/implement",
                  reason="tricky concurrency", **{"from": "medium", "to": "high"})
    crm = {r["run_id"]: r for r in agents_view(pipe.root, pipe.cfg)["live"]}[building.id]
    assert crm["effort"] == "high" and crm["effort_arrow"] == "up" and crm["effort_reason"] == "tricky concurrency"

    building.emit("agent_exited", session_id="s-crm", exit_code=0, cost=0.5)  # a fix-loop resume of the session
    building.emit("fix_attempt", key="build/build/task-3/implement", attempt=1, gates=["build"])
    building.emit("agent_started", pid=os.getpid(), session_id="s-crm", step="build/build/task-3/implement",
                  agent="claude", model="sonnet", effort="high", role="build", phase="build")
    view = agents_view(pipe.root, pipe.cfg)
    crm_rows = [r for r in view["live"] + view["exited"] if r["run_id"] == building.id]
    assert len(crm_rows) == 1 and crm_rows[0] in view["live"]
    assert crm_rows[0]["fix"] == "1/3" and "fix 1/3" in crm_rows[0]["where"]

    planning.emit("agent_exited", session_id="s-plan", exit_code=0, cost=0.2)
    view = agents_view(pipe.root, pipe.cfg)
    assert [r["run_id"] for r in view["live"]] == [building.id]
    assert [(r["run_id"], r["status"]) for r in view["exited"]] == [(planning.id, "exit 0")]
    later = datetime.now(UTC) + timedelta(minutes=11)
    assert agents_view(pipe.root, pipe.cfg, now=later)["exited"] == []  # past show_exited_min
    assert agents_view(pipe.root, pipe.cfg, now=later)["live"][0]["idle"] is True  # past idle_warn_min


def test_agents_view_drops_dead_pids(pipe):
    run = pipe.new_run()
    gone = subprocess.Popen(["true"])
    gone.wait()
    run.emit("agent_started", pid=gone.pid, step="build/x", agent="claude", model="sonnet", effort="medium")
    view = agents_view(pipe.root, pipe.cfg)
    assert view["live"] == []
    assert [r["status"] for r in view["exited"]] == ["gone"]


def test_subagents_nest_under_their_session(pipe):
    run = pipe.new_run()
    _agent(run, "build/x", "s-1")
    run.emit("subagent_started", session_id="s-1", tool_use_id="t1", subagent_type="Explore", model=None)
    (row,) = agents_view(pipe.root, pipe.cfg)["live"]
    assert row["subagents"] == [{"subagent_type": "Explore", "model": "inherit", "done": False}]


# --------------------------------------------------------------------------- terminal views


def test_terminal_tables(pipe):
    planning, building = fixture_runs(pipe)
    text = board_table(board(pipe.root, pipe.cfg))
    assert "plan" in text.splitlines()[0] and "build" in text.splitlines()[0]
    assert planning.id in text and building.id in text and "task 3/7" in text
    assert "Needs human" in text and "plan_approved" in text
    agents = agents_table(agents_view(pipe.root, pipe.cfg))
    assert "Agents running: 1" in agents and "build › task 3/7 implement" in agents and "sonnet" in agents


def test_watch_rerenders_on_change_and_reminds(pipe):
    frames: list[str] = []
    marks = iter([1, 1, 2, 2, 3])
    reminded: list[int] = []
    clock = iter(range(0, 1000, 30))
    watch(lambda: f"frame {len(frames)}", lambda: next(marks), out=frames.append, sleep=lambda s: None,
          stop=lambda: len(frames) >= 3, remind=lambda: reminded.append(1), clock=lambda: next(clock))
    assert frames == ["frame 0", "frame 1", "frame 2"]
    assert reminded  # once a minute of the fake clock


def test_cli_agents_once(pipe, capsys):
    fixture_runs(pipe)
    pipe.install_pack()
    assert main(["agents"]) == 0
    assert "Agents running: 1" in capsys.readouterr().out


def test_late_approval_on_an_ended_run_leaves_the_phase_row_alone(pipe):
    run = pipe.new_run()
    run.emit("phase_started", phase="build")
    cancel(run, by="me", cfg=pipe.cfg)
    run.emit("phase_approval_raised", phase="build", round=0)
    assert run.state["phases"]["build"]["status"] == "running"
    assert run.state["waiting"] is None and run.state["status"] == "cancelled"
