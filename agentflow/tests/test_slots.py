"""Slots: each live run of a repository holds its own number, and `{slot}` renders into its config."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agentflow import gates, slots
from agentflow.state import Run

CHILD = """
import sys, time
from pathlib import Path
from agentflow import slots
from agentflow.config import load_config
from agentflow.state import Run

cfg = load_config(Path(sys.argv[1]))
repo, go, ready = Path(sys.argv[2]), Path(sys.argv[3]), Path(sys.argv[4])
run = Run.create(cfg, repo, "feature", {"module": "crm"})
ready.write_text("ready")
deadline = time.monotonic() + 30
while not go.exists() and time.monotonic() < deadline:
    time.sleep(0.005)
try:
    print(slots.acquire(cfg, run))
except slots.SlotsFull:
    print("SlotsFull")
"""


def make(flow_repo, count: int, **extra):
    patch = {"slots": {"count": count}, **extra}
    return flow_repo.configure(patch)


def new_run(flow_repo, params: dict | None = None) -> Run:
    return Run.create(flow_repo.cfg, flow_repo.root, "feature", params or {"module": "crm"})


def take(cfg, run: Run) -> int:
    return slots.acquire(cfg, run)


def test_off_takes_slot_zero_and_writes_nothing(flow_repo):
    cfg = make(flow_repo, 1)
    runs = [new_run(flow_repo) for _ in range(5)]
    for run in runs:
        assert slots.acquire(cfg, run) == 0
        assert not [e for e in run.events.read() if e["type"] == "slot_acquired"]
        assert "slot" not in run.state
        slots.check_free(cfg, flow_repo.root)
    assert not slots.enabled(cfg)
    assert slots.slot_of(runs[0]) == 0
    assert slots.queue_hold(cfg, flow_repo.root) is None


def test_runs_take_the_lowest_free_number(flow_repo):
    cfg = make(flow_repo, 3)
    runs = [new_run(flow_repo) for _ in range(3)]
    assert [take(cfg, run) for run in runs] == [0, 1, 2]
    assert slots.held(cfg, flow_repo.root) == {0: runs[0].id, 1: runs[1].id, 2: runs[2].id}
    assert slots.free(cfg, flow_repo.root) == []
    assert [slots.slot_of(run) for run in runs] == [0, 1, 2]


def test_a_full_house_raises_slots_full_naming_the_holders(flow_repo):
    cfg = make(flow_repo, 2)
    a, b, c = (new_run(flow_repo) for _ in range(3))
    take(cfg, a)
    take(cfg, b)
    with pytest.raises(slots.SlotsFull) as info:
        take(cfg, c)
    message = str(info.value)
    assert a.id in message and b.id in message and "raise slots.count" in message
    assert message.startswith("all 2 slots are held: 0 by ")
    with pytest.raises(slots.SlotsFull) as info:
        slots.check_free(cfg, flow_repo.root)
    assert str(info.value) == message
    assert "slot" not in c.state


def test_a_waiting_paused_or_held_run_keeps_its_slot(flow_repo):
    cfg = make(flow_repo, 5)
    waiting, paused, holding, errored = (new_run(flow_repo) for _ in range(4))
    for run in (waiting, paused, holding, errored):
        take(cfg, run)
    waiting.emit("hitl_raised", key="define.ask", kind="spec_gap", summary="q")
    paused.emit("run_paused")
    holding.emit("run_held", key="ci", reason="ci_pending", until="2099-01-01T00:00:00+00:00")
    errored.emit("runner_error", error="boom")
    assert waiting.state["status"] == "waiting_human"
    assert paused.state["status"] == "paused"
    assert holding.state["hold"] is not None
    assert errored.state["status"] == "error"
    taken = slots.held(cfg, flow_repo.root)
    assert set(taken.values()) == {waiting.id, paused.id, holding.id, errored.id}
    assert slots.free(cfg, flow_repo.root) == [4]


@pytest.mark.parametrize("ending", [("run_finished", {}), ("run_failed", {"reason": "x"}), ("run_cancelled", {})])
def test_a_terminal_run_gives_its_slot_back(flow_repo, ending):
    cfg = make(flow_repo, 2)
    a, b = new_run(flow_repo), new_run(flow_repo)
    take(cfg, a)
    take(cfg, b)
    a.emit(ending[0], **ending[1])
    assert slots.free(cfg, flow_repo.root) == [0]
    assert slots.held(cfg, flow_repo.root) == {1: b.id}
    slots.check_free(cfg, flow_repo.root)


def test_acquire_keeps_a_runs_own_slot(flow_repo):
    cfg = make(flow_repo, 3)
    other, run = new_run(flow_repo), new_run(flow_repo)
    take(cfg, other)
    assert take(cfg, run) == 1
    assert take(cfg, run) == 1
    assert len([e for e in run.events.read() if e["type"] == "slot_acquired"]) == 1


def test_a_retried_run_takes_another_slot_when_its_own_is_taken(flow_repo):
    cfg = make(flow_repo, 2)
    a, b = new_run(flow_repo), new_run(flow_repo)
    assert take(cfg, a) == 0
    a.emit("run_failed", reason="x")
    assert take(cfg, b) == 0
    assert a.state["status"] == "failed"  # its number was free, so b took it; a comes back (a retry) and finds it gone
    assert take(cfg, a) == 1
    event = [e for e in a.events.read() if e["type"] == "slot_acquired"][-1]
    assert event["slot"] == 1 and event["previous"] == 0 and event["count"] == 2
    assert slots.slot_of(a) == 1


def test_a_slot_above_a_lowered_count_is_given_up(flow_repo):
    cfg = make(flow_repo, 3)
    a, b, c = (new_run(flow_repo) for _ in range(3))
    for run in (a, b, c):
        take(cfg, run)
    a.emit("run_cancelled")
    lowered = make(flow_repo, 2)
    assert slots.held(lowered, flow_repo.root) == {1: b.id}  # c holds 2, which is gone
    assert c.id not in slots.held(lowered, flow_repo.root).values()
    assert take(lowered, c) == 0
    event = [e for e in c.events.read() if e["type"] == "slot_acquired"][-1]
    assert event["slot"] == 0 and event["previous"] == 2


def test_two_processes_never_take_the_same_slot(flow_repo, tmp_path: Path):
    cfg = make(flow_repo, 2)
    take(cfg, new_run(flow_repo))  # one other live run holds slot 0
    go = tmp_path / "go"
    root = Path(__file__).resolve().parents[2]
    env = {"PYTHONPATH": str(root), "PATH": "/usr/bin:/bin"}
    env.update({k: v for k, v in os.environ.items() if k.startswith(("XDG_", "HOME", "TMP"))})
    procs, readies = [], []
    for n in range(2):
        ready = tmp_path / f"ready{n}"
        readies.append(ready)
        procs.append(subprocess.Popen(
            [sys.executable, "-c", CHILD, str(cfg.path), str(flow_repo.root), str(go), str(ready)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env))
    deadline = time.monotonic() + 30
    while not all(r.exists() for r in readies) and time.monotonic() < deadline:
        time.sleep(0.01)
    go.write_text("go")
    outs = []
    for proc in procs:
        out, err = proc.communicate(timeout=60)
        assert proc.returncode == 0, err
        outs.append(out.strip())
    assert sorted(outs) == ["1", "SlotsFull"]


def test_render_replaces_the_token_in_env_and_gate_commands(flow_repo):
    def edit(data: dict) -> None:
        data["defaults"]["env"] = {"PORT": "5543{slot}"}
        data["gates"]["slotted"] = {"run": "docker compose -p app-test-{slot} up",
                                    "affected": {"run": "pytest -p app-{slot} {paths}", "paths": ["*.py"]}}

    cfg = flow_repo.configure(edit=edit)
    out = slots.render(cfg, 2)
    assert out.defaults.env["PORT"] == "55432"
    assert out.gates["slotted"].run == "docker compose -p app-test-2 up"
    assert out.gates["slotted"].affected.run == "pytest -p app-2 {paths}"
    assert out.gates["slotted"].affected.paths == ["*.py"]
    assert cfg.defaults.env["PORT"] == "5543{slot}"
    assert cfg.gates["slotted"].run == "docker compose -p app-test-{slot} up"
    assert cfg.gates["slotted"].affected.run == "pytest -p app-{slot} {paths}"
    assert out.path == cfg.path and out.raw is cfg.raw
    assert out.gates["ok"] == cfg.gates["ok"]  # a gate without the token is unchanged


def test_render_leaves_a_shell_dollar_brace_and_a_doubled_brace_alone(flow_repo):
    def edit(data: dict) -> None:
        data["gates"]["shell"] = {"run": "echo ${slot} {slot}"}
        data["gates"]["doubled"] = {"run": "echo {{slot}}"}

    cfg = flow_repo.configure(edit=edit)
    out = slots.render(cfg, 3)
    assert out.gates["shell"].run == "echo ${slot} 3"
    assert out.gates["doubled"].run == "echo {{slot}}"


def test_render_changes_the_gate_digest_with_the_slot(flow_repo):
    def edit(data: dict) -> None:
        data["defaults"]["env"] = {"PORT": "5543{slot}"}
        data["gates"]["slotted"] = {"run": "echo app-{slot}"}

    cfg = flow_repo.configure(edit=edit)

    def digest(slot: int) -> str:
        out = slots.render(cfg, slot)
        return gates.command_digest(out.gates["slotted"], out.defaults.env)

    assert digest(0) != digest(1)
    assert digest(1) == digest(1)


def test_queue_hold_keeps_the_human_slot(flow_repo):
    cfg = make(flow_repo, 3, queue={"human_slot": 1})
    a, b = new_run(flow_repo), new_run(flow_repo)
    assert slots.queue_hold(cfg, flow_repo.root) is None
    take(cfg, a)
    assert slots.queue_hold(cfg, flow_repo.root) is None
    take(cfg, b)
    assert slots.queue_hold(cfg, flow_repo.root) == (
        "waiting for a free slot: 2 of 3 held, queue.human_slot keeps 1 free")
    off = make(flow_repo, 1, queue={"human_slot": 1})
    assert slots.queue_hold(off, flow_repo.root) is None
