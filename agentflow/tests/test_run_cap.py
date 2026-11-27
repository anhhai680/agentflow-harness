"""The machine-wide run cap (workspace slice W5, Task 2): `workspace.max_active_runs` slots, taken by each agent session,
sizer call and gate set inside an `advance` turn, held rather than refused past the cap, and failing open when the lock
files can't be used. Every hold polls at 0.01 s and is ended by the test (`after_hold`), never by a fixed delay."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from agentflow import flow, flow_control, gates, inbox_view, layers, lanes, pack, pipeline, run_cap, views
from agentflow.sizing_lane import SizingLane
from agentflow.state import Run
from test_agent_step import DONE, KEY, run_step
from test_governor_flow import SOON, after_hold, held_then
from test_pipeline import begin, go

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(run_cap, "POLL_S", 0.01)
    monkeypatch.setattr(run_cap, "_warned", False)


def cap_of(n: int | None) -> None:
    """Write the user file with `workspace.max_active_runs: n` (no key for None)."""
    path = layers.user_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"workspace": {"max_active_runs": n}} if n is not None else {}), encoding="utf-8")


def in_turn(flow_repo, fn):
    """Run `fn()` in a thread inside a turn of its own (a thread starts with an empty context)."""
    box: dict = {}

    def target() -> None:
        with run_cap.turn(flow_repo.run, flow_repo.cfg):
            try:
                box["out"] = fn()
            except BaseException as exc:  # noqa: BLE001 - handed back to the test
                box["error"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread, box


def finished(waiter) -> dict:
    thread, box = waiter
    thread.join(SOON)
    assert not thread.is_alive(), "the work never finished"
    if "error" in box:
        raise box["error"]
    return box


def types(run: Run) -> list[str]:
    return [e["type"] for e in run.events.read()]


def a_fixture_gate(flow_repo) -> str:
    assert flow_repo.cfg.gates["floor"].run == "true"
    return "floor"


# --------------------------------------------------------------------------- wiring


def test_the_engine_imports_with_run_cap_wired_in():
    # `-I` leaves the working directory off `sys.path`, so the repository is put first by hand
    code = "import sys; sys.path.insert(0, sys.argv[1]); import agentflow.gates, agentflow.flow_control, agentflow.pipeline"
    done = subprocess.run([sys.executable, "-I", "-c", code, str(REPO)], cwd=REPO, capture_output=True, text=True,
                          timeout=60)
    assert done.returncode == 0, done.stderr
    for status in ("running", "paused", "waiting_human", "held", "error", "done", "failed", "cancelled"):
        assert run_cap._halted(status) == flow_control.halted(status), status


# --------------------------------------------------------------------------- take


def test_slots_are_taken_lowest_first_and_freed_on_release(tmp_path):
    a, b = run_cap.take(2, tmp_path), run_cap.take(2, tmp_path)
    assert (a.number, b.number) == (0, 1) and run_cap.take(2, tmp_path) is None
    a.release()
    a.release()  # idempotent
    again = run_cap.take(2, tmp_path)
    assert again.number == 0
    again.release()
    b.release()


def test_a_lowered_cap_drains_before_anyone_new_starts(tmp_path):
    held = [run_cap.take(3, tmp_path) for _ in range(3)]
    assert [s.number for s in held] == [0, 1, 2]
    held[0].release()
    assert run_cap.take(1, tmp_path) is None  # locks 1 and 2 still count against a cap of 1
    held[1].release()
    assert run_cap.take(1, tmp_path) is None
    held[2].release()
    slot = run_cap.take(1, tmp_path)
    assert slot.number == 0
    slot.release()


def test_takers_racing_through_the_mutex_never_overshoot(tmp_path, monkeypatch):
    monkeypatch.setattr(run_cap, "MUTEX_WAIT_S", 5.0)  # no fail-open under load
    barrier = threading.Barrier(8)
    got: list = []
    lock = threading.Lock()

    def taker() -> None:
        barrier.wait()
        slot = run_cap.take(2, tmp_path)
        with lock:
            got.append(slot)

    threads = [threading.Thread(target=taker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(SOON)
    numbers = sorted(s.number for s in got if s is not None)
    assert numbers == [0, 1] and sum(s is None for s in got) == 6
    for s in got:
        if s is not None:
            s.release()


def test_a_killed_holder_frees_its_slot(tmp_path):
    folder = tmp_path / run_cap.CAP_DIR
    folder.mkdir()
    code = ("import fcntl, os, sys, time\n"
            "fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)\n"
            "fcntl.flock(fd, fcntl.LOCK_EX)\n"
            "print('held', flush=True)\n"
            "time.sleep(60)\n")
    child = subprocess.Popen([sys.executable, "-I", "-c", code, str(folder / "0.lock")], stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "held"
        assert run_cap.take(1, tmp_path) is None
    finally:
        child.kill()
        child.wait(SOON)
    slot = run_cap.take(1, tmp_path)
    assert slot.number == 0
    slot.release()


@pytest.mark.parametrize("plant", ["symlinked_dir", "symlinked_lock", "hardlinked_lock", "fifo_lock", "held_mutex"])
def test_an_unusable_lock_fails_open_and_says_so_once(tmp_path, plant, capsys, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    folder = root / run_cap.CAP_DIR
    other = tmp_path / "elsewhere"
    holder = None
    if plant == "symlinked_dir":
        other.mkdir()
        folder.symlink_to(other, target_is_directory=True)
    else:
        folder.mkdir()
        if plant == "symlinked_lock":
            other.write_text("", encoding="utf-8")
            (folder / "0.lock").symlink_to(other)
        elif plant == "hardlinked_lock":
            (folder / "0.lock").write_text("", encoding="utf-8")
            os.link(folder / "0.lock", other)
        elif plant == "fifo_lock":
            os.mkfifo(folder / "0.lock")
        else:
            import fcntl

            monkeypatch.setattr(run_cap, "MUTEX_WAIT_S", 0.05)
            holder = os.open(folder / run_cap.MUTEX, os.O_RDWR | os.O_CREAT, 0o600)
            fcntl.flock(holder, fcntl.LOCK_EX)
    try:
        assert run_cap.take(1, root) == run_cap.Slot(None, None)
        assert run_cap.take(1, root) == run_cap.Slot(None, None)
    finally:
        if holder is not None:
            os.close(holder)
    assert capsys.readouterr().err.count("the run cap can't be used") == 1


# --------------------------------------------------------------------------- the cap


def test_limit_reads_the_user_file_and_a_bad_file_is_no_cap():
    assert run_cap.limit() is None
    cap_of(2)
    assert run_cap.limit() == 2
    layers.user_config_path().write_text(": : :", encoding="utf-8")
    assert run_cap.limit() is None


def test_a_pack_s_workspace_key_caps_nothing(flow_repo):
    flow_repo.cfg = flow_repo.configure({"workspace": {"max_active_runs": 1}})
    assert run_cap.limit() is None
    flow_repo.new_run()
    mine = run_cap.take(1)
    try:
        out = finished(in_turn(flow_repo, lambda: run_step(flow_repo)))["out"]
    finally:
        mine.release()
    assert out == "done" and flow_repo.events(flow_repo.run, "run_held") == []


def test_without_a_turn_nothing_is_capped(flow_repo, tmp_path):
    cap_of(1)
    flow_repo.new_run()
    mine = run_cap.take(1)
    try:
        assert run_cap.current() is None
        (result,) = gates.run_gates(flow_repo.cfg, [a_fixture_gate(flow_repo)], flow_repo.root, tmp_path / "logs")
    finally:
        mine.release()
    assert result.passed and flow_repo.events(flow_repo.run, "run_held") == []


# --------------------------------------------------------------------------- holds


def test_an_agent_session_past_the_cap_waits_until_a_slot_frees(flow_repo):
    cap_of(1)
    run = flow_repo.new_run()
    mine = run_cap.take(1)
    waiter = after_hold(flow_repo, mine.release, run)
    out = finished(in_turn(flow_repo, lambda: run_step(flow_repo)))["out"]
    held_then(waiter)
    (held,) = flow_repo.events(run, "run_held")
    assert held["reason"] == "run cap" and held["key"].endswith("/run_cap") and held["until"]
    assert out == "done" and len(flow_repo.calls()) == 1
    order = types(run)
    assert order.index("run_hold_ended") < order.index("agent_started")


def test_a_pause_while_a_session_waits_stops_the_step(flow_repo):
    cap_of(1)
    run = flow_repo.new_run()
    mine = run_cap.take(1)
    try:
        waiter = after_hold(flow_repo, lambda: Run.load(flow_repo.root, run.id).emit("run_paused", by="me"), run)
        out = finished(in_turn(flow_repo, lambda: run_step(flow_repo)))["out"]
        held_then(waiter)
    finally:
        mine.release()
    assert out == "stopped" and flow_repo.calls() == []
    assert len(flow_repo.events(run, "run_hold_ended")) == 1
    slot = run_cap.take(1)
    assert slot is not None and slot.number == 0
    slot.release()


def test_an_empty_gate_set_never_waits(flow_repo, tmp_path):
    cap_of(1)
    flow_repo.new_run()
    mine = run_cap.take(1)
    try:
        out = finished(in_turn(flow_repo, lambda: gates.run_gates(flow_repo.cfg, [], flow_repo.root,
                                                                 tmp_path / "logs")))["out"]
    finally:
        mine.release()
    assert out == [] and flow_repo.events(flow_repo.run, "run_held") == []


def test_a_pause_while_a_gate_set_waits_runs_that_set_once(flow_repo, tmp_path):
    cap_of(1)
    run = flow_repo.new_run()
    mine = run_cap.take(1)
    try:
        waiter = after_hold(flow_repo, lambda: Run.load(flow_repo.root, run.id).emit("run_paused", by="me"), run)
        out = finished(in_turn(flow_repo, lambda: gates.run_gates(flow_repo.cfg, [a_fixture_gate(flow_repo)],
                                                                 flow_repo.root, tmp_path / "logs")))["out"]
        held_then(waiter)
    finally:
        mine.release()
    assert [(r.name, r.passed) for r in out] == [("floor", True)]
    (held,) = flow_repo.events(run, "run_held")
    assert held["key"] == f"run/{run_cap.CAP_STEP}"  # a run with no phase yet
    assert len(flow_repo.events(run, "run_hold_ended")) == 1


# --------------------------------------------------------------------------- what takes a slot, and for how long


def test_the_slot_is_given_back_after_each_piece_of_work(flow_repo, monkeypatch):
    cap_of(1)
    flow_repo.new_run()

    def after() -> bool:
        slot = run_cap.take(1)
        if slot is None:
            return False
        slot.release()
        return True

    assert finished(in_turn(flow_repo, lambda: (run_step(flow_repo), after())))["out"] == ("done", True)

    def boom(self, sr, turn):
        raise RuntimeError("the agent broke")

    monkeypatch.setattr(lanes.Executor, "_call", boom)
    flow_repo.new_run()  # the first run's step is done: a new one calls the agent again

    def raising() -> bool:
        with pytest.raises(RuntimeError, match="the agent broke"):
            run_step(flow_repo)
        return after()

    assert finished(in_turn(flow_repo, raising))["out"] is True
    assert flow_repo.events(flow_repo.run, "run_held") == []


def test_every_kind_of_work_takes_a_slot(flow_repo, monkeypatch, tmp_path):
    cap_of(4)
    flow_repo.new_run()
    taken: list[int] = []
    take = run_cap.take

    def spy(cap, root=None):
        taken.append(cap)
        return take(cap, root)

    monkeypatch.setattr(run_cap, "take", spy)

    def count(fn) -> int:
        taken.clear()
        finished(in_turn(flow_repo, fn))
        return len(taken)

    assert count(lambda: run_step(flow_repo)) == 1  # a fresh session
    flow_repo.script({KEY: [{}, {"result": DONE}]})
    flow_repo.new_run()
    assert count(lambda: run_step(flow_repo)) == 2  # a fresh session and its resume
    first, second = flow_repo.calls()[-2:]
    assert "--resume" not in first["argv"] and "--resume" in second["argv"]

    monkeypatch.setattr(SizingLane, "_run_sizer", lambda self, agent, call, on_event: None)

    def sizer():
        executor = flow_repo.executor()
        return executor._sizer_call(f"{KEY}/size", "p", executor.agents["claude"], "sonnet", "low", "build", "build",
                                    3, 30)

    assert count(sizer) == 1
    assert count(lambda: gates.run_gates(flow_repo.cfg, [a_fixture_gate(flow_repo)], flow_repo.root,
                                         tmp_path / "logs")) == 1


def test_a_turn_that_runs_no_work_takes_no_slot(flow_repo, monkeypatch):
    cap_of(1)
    monkeypatch.setattr(pipeline._Loop, "walk", lambda self, phase, executor: ("stopped", False))
    assert not flow_repo.cfg.preflight.enabled
    run = begin(flow_repo)
    mine = run_cap.take(1)
    box: dict = {}
    thread = threading.Thread(target=lambda: box.update(out=go(flow_repo, run)), daemon=True)
    try:
        thread.start()
        thread.join(SOON)
        assert not thread.is_alive(), "the turn waited for a slot"
    finally:
        mine.release()
        thread.join(SOON)
    assert "out" in box and flow_repo.events(run, "run_held") == []


def test_the_turn_releases_its_slot_when_advance_raises(flow_repo, monkeypatch):
    cap_of(1)

    def walk(self, phase, executor):
        with run_cap.working(f"{phase.name}/{run_cap.CAP_STEP}", stop_on_halt=True) as go_on:
            assert go_on and run_cap.current().slot.number == 0
            raise RuntimeError("the walk broke")

    monkeypatch.setattr(pipeline._Loop, "walk", walk)
    run = begin(flow_repo)
    with pytest.raises(RuntimeError, match="the walk broke"):
        go(flow_repo, run)
    assert run_cap.current() is None
    slot = run_cap.take(1)
    assert slot is not None
    slot.release()


def test_a_nested_take_inside_a_held_slot_just_goes_on(flow_repo):
    cap_of(1)
    flow_repo.new_run()

    def nested() -> tuple:
        with run_cap.working("build/run_cap", stop_on_halt=True) as outer:
            with run_cap.working("build/run_cap", stop_on_halt=True) as inner:
                held = run_cap.current().slot.number
            still = run_cap.current().slot.number
        return outer, inner, held, still, run_cap.current().slot

    assert finished(in_turn(flow_repo, nested))["out"] == (True, True, 0, 0, None)
    assert flow_repo.events(flow_repo.run, "run_held") == []


# --------------------------------------------------------------------------- readers


def test_an_open_hold_shows_no_until(flow_repo, capsys):
    run = flow_repo.new_run()
    run.emit("phase_started", phase="define")
    run.emit("run_held", key=f"define/{run_cap.CAP_STEP}", reason=run_cap.HOLD_REASON,
             until=datetime.now(UTC).isoformat(timespec="seconds"), cap=1)
    lock = run.lock()
    assert lock.acquire()
    try:
        card = next(c for c in views.board(flow_repo.root, flow_repo.cfg)["cards"] if c["run_id"] == run.id)
        assert flow.main(["status", run.id]) == 0
        out = capsys.readouterr().out
        (row,) = inbox_view.inbox_view(flow_repo.root, flow_repo.cfg, datetime.now(UTC))["watch"]
    finally:
        lock.release()
    assert (card["status"], card["badge"]) == ("held", "held (run cap)")
    assert "held:     run cap, step define/run_cap\n" in out
    assert row["text"] == "run cap"


def test_the_cap_lives_in_the_user_state_root(flow_repo):
    cap_of(1)
    flow_repo.new_run()
    seen: list = []

    def work() -> None:
        with run_cap.working("build/run_cap", stop_on_halt=True):
            seen.append(sorted(p.name for p in (pack.user_state_root() / run_cap.CAP_DIR).iterdir()))

    finished(in_turn(flow_repo, work))
    assert seen == [sorted(["0.lock", run_cap.MUTEX])]
