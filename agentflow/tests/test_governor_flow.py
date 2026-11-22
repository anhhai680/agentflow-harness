"""The usage window governor's holds (slice C6, Task 4): the hard mark before each fresh Claude session, the soft mark
before each phase, finish first, and `observe`. Every hold polls at 0.01 s (`quick`) and is released by a later
reading (`release`), a reset, a pause or `off`: none waits on a real poll or a real reset."""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import pytest

from agentflow import governor, lanes, pipeline
from agentflow.pipeline import RunOptions
from agentflow.sizing_lane import SizingLane
from agentflow.state import Run
from conftest import prime, release
from test_agent_step import DONE, KEY, make_phase, make_step, run_step
from test_pipeline import begin, go

KEY2 = "build/second"
KEY3 = "build/third"
SOON = 10.0  # seconds: far above any hold these tests mean to end, far below a real poll or reset


def second_step(flow_repo, key: str = KEY2) -> str:  # another agent step in the same phase, with its own key
    step = make_step()
    return flow_repo.executor().run_agent_step(make_phase(step), step, key)


def wait_for(predicate) -> bool:
    deadline = time.monotonic() + SOON
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def in_thread(fn) -> tuple[threading.Thread, dict]:
    box: dict = {}

    def target() -> None:
        box["out"] = fn()
        box["at"] = time.time()

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread, box


def held_events(flow_repo) -> list[dict]:
    return flow_repo.events(flow_repo.run, "run_held")


def after_hold(flow_repo, action, run=None) -> tuple[threading.Thread, dict]:
    """Run `action` once a new `run_held` event exists, never before: a fixed delay races the code reaching its hold.
    When none appears within SOON, `action` is not run (`box["fired"]` stays False) and `held_then` fails the test."""
    run = run or flow_repo.run
    seen = len(flow_repo.events(run, "run_held"))
    box: dict = {"fired": False}

    def target() -> None:
        if wait_for(lambda: len(flow_repo.events(run, "run_held")) > seen):
            action()
            box["fired"] = True

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread, box


def held_then(waiter: tuple[threading.Thread, dict]) -> None:
    thread, box = waiter
    thread.join(SOON)
    assert box["fired"], "no run_held event appeared, so the action that ends the hold never ran"


def write_file(text: str) -> None:
    path = governor.window_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def prime_one_second() -> None:
    """`prime(0.95, resets_in=1)` just after a whole second, so the reset (a whole second) is a full second away."""
    time.sleep(1.0 - time.time() % 1.0)
    prime(0.95, resets_in=1)


def test_prime_records_under_the_run_s_account(flow_repo):
    assert governor.account_key(os.environ.get("CLAUDE_CONFIG_DIR")) == governor.account_of(flow_repo.cfg)


def test_a_reading_at_095_holds_the_next_fresh_session_until_the_window_clears(flow_repo, quick):
    flow_repo.new_run()
    flow_repo.script({KEY: [{"windows": {"five_hour": 0.95}, "resets_at": int(time.time()) + 3600, "result": DONE}],
                      KEY2: [{"result": DONE}]})
    assert run_step(flow_repo) == "done"  # the session already running is never cut
    waiter = after_hold(flow_repo, release)
    assert second_step(flow_repo) == "done"
    held_then(waiter)
    (held,) = held_events(flow_repo)
    assert held["reason"] == "usage window" and held["level"] == "hard" and held["key"] == KEY2
    assert held["utilization"] == 0.95 and held["window"] == "five_hour" and flow_repo.run.state["hold"] is None
    assert [e["key"] for e in flow_repo.events(flow_repo.run, "run_hold_ended")] == [KEY2]
    assert governor.WindowFile().read()["waiting"] == {}
    assert [c["step"] for c in flow_repo.calls()] == [KEY, KEY2]


def test_a_resume_is_not_held(flow_repo, quick):
    flow_repo.new_run()
    flow_repo.script({KEY: [{"windows": {"five_hour": 0.95}, "resets_at": int(time.time()) + 3600},
                            {"result": DONE}]})
    assert run_step(flow_repo) == "done"
    assert held_events(flow_repo) == []
    first, second = flow_repo.calls()
    assert "--resume" not in first["argv"] and "--resume" in second["argv"]


def test_a_reading_at_08_holds_a_new_phase_but_not_a_step(flow_repo, quick):
    flow_repo.new_run()
    flow_repo.script({KEY: [{"windows": {"five_hour": 0.8}, "resets_at": int(time.time()) + 3600, "result": DONE}],
                      KEY2: [{"result": DONE}]})
    assert run_step(flow_repo) == "done" and second_step(flow_repo) == "done"  # steps in a started phase go on
    assert held_events(flow_repo) == []
    waiter = after_hold(flow_repo, release)
    assert flow_repo.executor().govern("plan/governor", "soft", "plan") is True
    held_then(waiter)
    (held,) = held_events(flow_repo)
    assert held["key"] == "plan/governor" and held["level"] == "soft" and held["utilization"] == 0.8
    assert flow_repo.run.refresh()["hold"] is None


def test_a_new_run_waits_before_its_first_phase(flow_repo, quick, monkeypatch):
    prime(0.8)
    monkeypatch.setattr(pipeline._Loop, "walk", lambda self, phase, executor: ("stopped", False))
    run = begin(flow_repo)
    waiter = after_hold(flow_repo, release, run)
    go(flow_repo, run)
    held_then(waiter)
    log = run.events.read()
    seq = {e["type"]: e["seq"] for e in log if e["type"] in ("run_held", "run_hold_ended", "phase_started")}
    (held,) = [e for e in log if e["type"] == "run_held"]
    assert held["key"] == "define/governor" and held["level"] == "soft"
    assert seq["run_held"] < seq["run_hold_ended"] < seq["phase_started"]
    assert [e["phase"] for e in log if e["type"] == "phase_started"] == ["define"]


def test_a_new_run_s_triage_waits_on_the_soft_mark(flow_repo, quick, monkeypatch):
    flow_repo.configure({"route": {"mode": "observe"}})
    prime(0.8)
    triaged = []
    monkeypatch.setattr(lanes.Executor, "size_route",
                        lambda self, first: triaged.append(len(self.run.events.read())) or "stopped")
    run = begin(flow_repo)

    waiter = after_hold(flow_repo, release, run)
    go(flow_repo, run)
    held_then(waiter)
    log = run.events.read()
    (held,) = [e for e in log if e["type"] == "run_held"]
    (ended,) = [e for e in log if e["type"] == "run_hold_ended"]
    assert held["key"] == "define/governor" and held["level"] == "soft"
    assert len(triaged) == 1 and triaged[0] >= ended["seq"]  # the triage ran only once the hold ended


def test_setting_off_releases_a_held_run(flow_repo, quick):
    prime(0.95)
    flow_repo.new_run()
    executor = flow_repo.executor()
    thread, box = in_thread(lambda: executor.govern(KEY, "hard", "build"))
    assert wait_for(lambda: held_events(flow_repo))
    flow_repo.configure({"usage": {"governor": "off"}})
    thread.join(SOON)
    assert box.get("out") is True
    assert flow_repo.run.refresh()["hold"] is None and governor.WindowFile().read()["waiting"] == {}


def test_a_broken_file_during_a_hold_does_not_end_it(flow_repo, quick, monkeypatch):
    prime(0.95)
    flow_repo.new_run()
    executor = flow_repo.executor()
    thread, box = in_thread(lambda: executor.govern(KEY, "hard", "build"))
    assert wait_for(lambda: held_events(flow_repo))
    write_file("{not json")
    time.sleep(0.2)  # many polls: each heartbeat skips a file it can't read, so the garbage stays
    assert thread.is_alive() and governor.window_path().read_text(encoding="utf-8") == "{not json"
    release()  # a write that brings readings replaces the broken file
    thread.join(SOON)
    assert box.get("out") is True

    # The kept verdict holds only until its own `until`: never past the reset.
    monkeypatch.setattr(governor, "SLACK_S", 0.0)
    governor.window_path().unlink()
    prime_one_second()
    resets_at = governor.WindowFile().read()["windows"][governor.account_of(flow_repo.cfg)]["five_hour"]["resets_at"]
    thread, box = in_thread(lambda: executor.govern(KEY, "hard", "build"))
    assert wait_for(lambda: len(held_events(flow_repo)) == 2)
    write_file(json.dumps({"version": 2}))
    thread.join(SOON)
    assert box.get("out") is True and resets_at <= box["at"] < resets_at + 1


def test_a_skipped_phase_and_the_ship_phase_are_never_held(flow_repo, quick, monkeypatch):
    def skip_define(data: dict) -> None:
        data["phases"]["define"]["skip_if"] = {"check": "file_exists", "path": "README.md"}

    flow_repo.configure(edit=skip_define)
    prime(0.8)
    run = begin(flow_repo)
    loop = pipeline._Loop(run, flow_repo.cfg, RunOptions(), 0)
    loop.worktree = Path(run.state["worktree"])
    names = list(flow_repo.cfg.phases)
    assert loop.phase(names, 0) is None
    assert [e["phase"] for e in flow_repo.events(run, "phase_skipped")] == ["define"]
    monkeypatch.setattr(pipeline._Loop, "walk", lambda self, phase, executor: ("stopped", False))
    loop.phase(names, names.index("ship"))
    assert [e["phase"] for e in flow_repo.events(run, "phase_started")] == ["ship"]
    assert flow_repo.events(run, "run_held") == []


def test_holds_ship_finds_finish_and_ci_in_groups():
    from agentflow.config import Step

    def step(kind: str, *inner: Step) -> Step:
        return Step(id=kind, type=kind, steps=list(inner))

    assert pipeline._holds_ship([step("agent"), step("group", step("ci"))])
    assert pipeline._holds_ship([step("finish")])
    assert not pipeline._holds_ship([step("agent"), step("group", step("gate"))])


def test_observe_only_records(flow_repo, quick):
    flow_repo.configure({"usage": {"governor": "observe"}})
    flow_repo.new_run()
    flow_repo.script({KEY: [{"windows": {"five_hour": 0.95}, "resets_at": int(time.time()) + 3600, "result": DONE}],
                      KEY2: [{"result": DONE}], KEY3: [{"result": DONE}]})
    assert run_step(flow_repo) == "done" and second_step(flow_repo) == "done"
    assert held_events(flow_repo) == []
    (seen,) = flow_repo.events(flow_repo.run, "usage_governed")
    assert seen["key"] == KEY2 and seen["level"] == "hard" and seen["utilization"] == 0.95
    assert seen["window"] == "five_hour" and seen["until"]
    assert flow_repo.run.refresh()["usage_governed"]["key"] == KEY2
    assert flow_repo.executor().govern(KEY2, "hard", "build") is True  # the same key, level and until: no new event
    assert len(flow_repo.events(flow_repo.run, "usage_governed")) == 1

    now = time.time()
    governor.WindowFile().wait("other", "/elsewhere", [0.9, 0.0], now, True, now)  # a ready run ahead
    assert flow_repo.executor().govern("build/governor", "soft", "build") is True
    assert second_step(flow_repo, KEY3) == "done"
    assert held_events(flow_repo) == []
    assert [e["key"] for e in flow_repo.events(flow_repo.run, "usage_governed")].count(KEY2) == 1
    assert set(governor.WindowFile().read()["waiting"]) == {"other"}  # observe keeps no waiter entry


def test_a_pause_during_a_hold_stops_without_a_call(flow_repo, quick):
    prime(0.95)
    run = flow_repo.new_run()
    flow_repo.script({KEY: [{"result": DONE}]})
    waiter = after_hold(flow_repo, lambda: Run.load(flow_repo.root, run.id).emit("run_paused", by="me"), run)
    started = time.monotonic()
    assert run_step(flow_repo) == "stopped"
    held_then(waiter)
    assert time.monotonic() - started < SOON
    assert flow_repo.calls() == []
    assert [e["key"] for e in flow_repo.events(run, "run_hold_ended")] == [KEY]
    assert run.refresh()["hold"] is None and governor.WindowFile().read()["waiting"] == {}


def test_a_cancel_during_a_sizer_hold_returns_no_answer(flow_repo, quick, monkeypatch):
    prime(0.95)
    run = flow_repo.new_run()
    executor = flow_repo.executor()
    reached = []
    monkeypatch.setattr(SizingLane, "_run_sizer", lambda self, agent, call, on_event: reached.append(call.step_key))
    waiter = after_hold(flow_repo, lambda: Run.load(flow_repo.root, run.id).emit("run_cancelled", by="me"), run)
    assert executor._sizer_call(f"{KEY}/size", "p", executor.agents["claude"], "sonnet", "low", "build", "build",
                                3, 30) == (None, [])
    held_then(waiter)
    assert reached == [] and flow_repo.events(run, "sizer_started") == []
    assert [e["key"] for e in flow_repo.events(run, "run_hold_ended")] == [f"{KEY}/size"]


def test_the_hold_ends_at_the_reset_with_no_new_reading(flow_repo, quick, monkeypatch):
    monkeypatch.setattr(governor, "SLACK_S", 0.0)
    flow_repo.new_run()
    executor = flow_repo.executor()
    prime_one_second()
    started = time.monotonic()
    assert executor.govern(KEY, "hard", "build") is True
    assert 0.5 < time.monotonic() - started < 2
    (held,) = held_events(flow_repo)
    assert held["level"] == "hard" and flow_repo.run.refresh()["hold"] is None


def test_finish_first_waits_only_for_a_ready_run_closer_to_finish(flow_repo, quick):
    flow_repo.new_run()

    def plant(ready: bool) -> None:
        now = time.time()
        governor.WindowFile().wait("x", "/elsewhere", [0.8, 0.0], now, ready, now)

    plant(True)
    executor = flow_repo.executor()
    waiter = after_hold(flow_repo, lambda: governor.WindowFile().unwait("x"))
    assert executor.govern("build/governor", "soft", "build") is True
    held_then(waiter)
    (held,) = held_events(flow_repo)
    assert held["key"] == "build/governor" and held["level"] == "clear"
    assert governor.WindowFile().read()["waiting"] == {}

    plant(False)
    assert executor.govern("build/governor", "soft", "build") is True
    plant(True)
    flow_repo.configure({"usage": {"finish_first": False}})
    assert flow_repo.executor().govern("build/governor", "soft", "build") is True
    assert len(held_events(flow_repo)) == 1


def test_the_hard_gate_does_not_wait_on_a_queue_when_the_window_is_clear(flow_repo, quick):
    flow_repo.new_run()
    now = time.time()
    governor.WindowFile().wait("x", "/elsewhere", [0.9, 0.0], now, True, now)
    assert flow_repo.executor().govern(KEY, "hard", "build") is True
    assert held_events(flow_repo) == []


@pytest.mark.parametrize("entry", [
    '{"pid": 1, "rank": [1e999, 0], "since": 0, "seen": 1e999, "ready": true}',
    '{"pid": PID, "rank": "a", "since": NOW, "seen": NOW, "ready": true, "ready_since": NOW, "run_dir": "/x"}',
])
def test_a_bad_waiter_entry_never_holds_or_crashes_a_gate(flow_repo, quick, entry):
    run = flow_repo.new_run()
    entry = entry.replace("PID", str(os.getpid())).replace("NOW", str(time.time()))
    write_file('{"version": 1, "waiting": {"x": ' + entry + "}}")
    started = time.monotonic()
    assert flow_repo.executor().govern("build/governor", "soft", "build") is True
    assert time.monotonic() - started < 2
    assert flow_repo.events(run, "run_held") == [] and flow_repo.events(run, "runner_error") == []


def test_govern_fails_open(flow_repo, quick, monkeypatch):
    prime(0.95)
    run = flow_repo.new_run()
    account_of = governor.account_of
    monkeypatch.setattr(governor, "account_of", lambda cfg: (_ for _ in ()).throw(RuntimeError("no account")))
    assert flow_repo.executor().govern(KEY, "hard", "build") is True
    assert flow_repo.events(run, "run_held") == []
    monkeypatch.setattr(governor, "account_of", account_of)
    monkeypatch.setattr(governor.WindowFile, "wait", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("broken")))
    assert flow_repo.executor().govern(KEY, "hard", "build") is True  # it fails open mid-hold, and ends the hold
    assert [e["key"] for e in flow_repo.events(run, "run_hold_ended")] == [KEY]
    assert run.refresh()["hold"] is None and flow_repo.events(run, "runner_error") == []


def test_a_codex_step_and_a_codex_sizer_are_not_held(flow_repo, quick, monkeypatch):
    class Reached(Exception):
        pass

    flow_repo.configure({"agents": {"codex": {"enabled": True}}})
    prime(0.95)
    run = flow_repo.new_run()
    reached = []

    def call(self, sr, turn):
        reached.append(sr.key)
        raise Reached

    monkeypatch.setattr(lanes.Executor, "_call", call)
    step = make_step()
    with pytest.raises(Reached):
        flow_repo.executor().run_agent_step(make_phase(step, agent="codex"), step, KEY)
    monkeypatch.setattr(SizingLane, "_run_sizer", lambda self, agent, call, on_event: reached.append(call.step_key))
    executor = flow_repo.executor()
    size = f"{KEY}/size"
    assert executor._sizer_call(size, "p", executor.agents["codex"], "gpt-5-codex", "low", "build", "build",
                                3, 30) == (None, [])
    assert reached == [KEY, size] and flow_repo.events(run, "run_held") == []

    waiter = after_hold(flow_repo, release, run)  # a Claude sizer is held
    executor._sizer_call(size, "p", executor.agents["claude"], "sonnet", "low", "build", "build", 3, 30)
    held_then(waiter)
    assert reached == [KEY, size, size]
    assert [e["key"] for e in flow_repo.events(run, "run_held")] == [size]
