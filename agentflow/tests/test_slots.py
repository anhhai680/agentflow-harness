"""Slots: each live run of a repository holds its own number, and `{slot}` renders into its config."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agentflow import gates, pack, pipeline, run_control, slots
from agentflow.flow import main
from agentflow.hitl import approve
from agentflow.pipeline import RunOptions, advance, start
from agentflow.state import Run, runs_root

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


# --------------------------------------------------------------------------- the pipeline: start, advance, render

PIPE = {"gate_sets": {"build": ["ok"], "verify": ["ok"]}, "defaults": {"env": {"AF_PORT": "5543{slot}"}}}


def piped(flow_repo, monkeypatch, count: int, **extra):
    monkeypatch.setenv("AGENTFLOW_FAKE_ENV", "AF_PORT")
    return flow_repo.configure({"slots": {"count": count}, **PIPE, **extra})


def begin(flow_repo, params: dict | None = None):
    return start(flow_repo.cfg, flow_repo.root, "feature", params or {"module": "demo"}, RunOptions())


def go(flow_repo, run: Run) -> str:
    return advance(run, flow_repo.cfg, RunOptions())


def ports(flow_repo) -> list[str]:
    return [c["env"]["AF_PORT"] for c in flow_repo.calls()]


def run_dirs(flow_repo) -> set[str]:
    root = runs_root(flow_repo.root)
    return {p.name for p in root.iterdir() if p.is_dir()} if root.exists() else set()


def worktrees(flow_repo) -> str:
    return subprocess.run(["git", "worktree", "list"], cwd=flow_repo.root, capture_output=True, text=True,
                          check=True).stdout


def acquired(flow_repo, run: Run) -> list[dict]:
    return flow_repo.events(run, "slot_acquired")


def test_two_live_runs_get_different_slots_in_their_agents_env(flow_repo, monkeypatch):
    piped(flow_repo, monkeypatch, 2)
    a, b = begin(flow_repo), begin(flow_repo, {"module": "other"})
    assert go(flow_repo, a) == "awaiting_approval" and go(flow_repo, b) == "awaiting_approval"
    assert {a.state["slot"], b.state["slot"]} == {0, 1}
    assert set(ports(flow_repo)) == {"55430", "55431"}
    assert len(acquired(flow_repo, a)) == 1  # advance kept the number start took


def test_a_third_run_is_refused_before_it_exists(flow_repo, monkeypatch, capsys):
    piped(flow_repo, monkeypatch, 2)
    flow_repo.install_pack()
    begin(flow_repo)
    begin(flow_repo, {"module": "other"})
    dirs, trees = run_dirs(flow_repo), worktrees(flow_repo)
    with pytest.raises(slots.SlotsFull):
        begin(flow_repo, {"module": "third"})
    assert run_dirs(flow_repo) == dirs and worktrees(flow_repo) == trees
    capsys.readouterr()
    assert main(["run", "feature", "--module", "x"]) != 0
    assert "error: all 2 slots are held" in capsys.readouterr().out
    assert run_dirs(flow_repo) == dirs and worktrees(flow_repo) == trees


def test_a_cancelled_run_frees_its_slot_for_the_next_start(flow_repo, monkeypatch):
    piped(flow_repo, monkeypatch, 2)
    a, b = begin(flow_repo), begin(flow_repo, {"module": "other"})
    run_control.cancel(a, "me", flow_repo.cfg)
    c = begin(flow_repo, {"module": "third"})
    assert c.state["slot"] == a.state["slot"] == 0 and b.state["slot"] == 1


def three_runs_with_a_on_slot_two(flow_repo, monkeypatch):
    """Count 3: x holds 0, y holds 1 and `a` holds 2, stopped at a human wait."""
    piped(flow_repo, monkeypatch, 3)
    x, y = begin(flow_repo), begin(flow_repo, {"module": "x"})
    a = begin(flow_repo, {"module": "a"})
    assert (x.state["slot"], y.state["slot"], a.state["slot"]) == (0, 1, 2)
    assert go(flow_repo, a) == "awaiting_approval"
    assert ports(flow_repo) and set(ports(flow_repo)) == {"55432"}
    return x, y, a


def test_a_retried_run_takes_a_free_slot_again(flow_repo, monkeypatch):
    x, y, a = three_runs_with_a_on_slot_two(flow_repo, monkeypatch)
    run_control.cancel(x, "me", flow_repo.cfg)
    run_control.cancel(y, "me", flow_repo.cfg)
    b = begin(flow_repo, {"module": "b"})
    assert b.state["slot"] == 0
    lowered = piped(flow_repo, monkeypatch, 2)  # a's number is gone with the count
    approve(a, "define", by="me")
    calls = len(flow_repo.calls())
    advance(a, lowered, RunOptions())
    (event,) = [e for e in acquired(flow_repo, a) if e.get("previous") is not None]
    assert event["slot"] == 1 and event["previous"] == 2 and event["count"] == 2
    assert a.state["slot"] == 1
    assert [c["env"]["AF_PORT"] for c in flow_repo.calls()[calls:]] == ["55431"] * (len(flow_repo.calls()) - calls) != []


def test_resume_with_no_free_slot_changes_nothing(flow_repo, monkeypatch):
    x, y, a = three_runs_with_a_on_slot_two(flow_repo, monkeypatch)
    run_control.cancel(x, "me", flow_repo.cfg)
    b = begin(flow_repo, {"module": "b"})
    assert b.state["slot"] == 0 and y.state["slot"] == 1
    lowered = piped(flow_repo, monkeypatch, 2)
    approve(a, "define", by="me")
    status, seen, calls = a.refresh()["status"], len(a.events.read()), len(flow_repo.calls())
    with pytest.raises(slots.SlotsFull):
        advance(a, lowered, RunOptions())
    assert a.refresh()["status"] == status
    assert not flow_repo.events(a, "runner_error")
    assert len(acquired(flow_repo, a)) == 1 and len(flow_repo.calls()) == calls
    assert [e["type"] for e in a.events.read()[seen:]] == []
    assert a.lock().acquire()  # the lock was let go


def test_a_reloaded_config_is_rendered_again(flow_repo, monkeypatch):
    cfg = piped(flow_repo, monkeypatch, 2)
    monkeypatch.setattr(pack, "pack_config", lambda repo: Path(cfg.path))
    other = begin(flow_repo, {"module": "other"})
    run = begin(flow_repo)
    assert (other.state["slot"], run.state["slot"]) == (0, 1)
    loop = pipeline._Loop(run, cfg, RunOptions(), slots.acquire(cfg, run))
    assert loop.slot == 1 and loop.cfg.defaults.env["AF_PORT"] == "55431"
    assert loop.go() == "awaiting_approval"
    assert set(ports(flow_repo)) == {"55431"}
    path = Path(cfg.path)
    path.write_text(path.read_text(encoding="utf-8").replace("5543{slot}", "6543{slot}"), encoding="utf-8")
    approve(run, "define", by="me")
    assert loop.go() == "awaiting_approval"
    assert flow_repo.events(run, "config_reloaded")
    assert ports(flow_repo)[-1] == "65431" and "{slot}" not in "".join(ports(flow_repo))
    assert loop.cfg.defaults.env["AF_PORT"] == "65431"


def test_slots_off_leaves_a_run_as_it_was(flow_repo, monkeypatch):
    piped(flow_repo, monkeypatch, 1)
    a, b = begin(flow_repo), begin(flow_repo, {"module": "other"})
    assert go(flow_repo, a) == "awaiting_approval" and go(flow_repo, b) == "awaiting_approval"
    assert set(ports(flow_repo)) == {"55430"}
    for run in (a, b):
        assert not acquired(flow_repo, run)
        assert "slot" not in run.state


def test_status_prints_the_slot(flow_repo, monkeypatch, capsys):
    piped(flow_repo, monkeypatch, 2)
    flow_repo.install_pack()
    begin(flow_repo, {"module": "other"})
    run = begin(flow_repo)
    capsys.readouterr()
    assert main(["status", run.id]) == 0
    assert "slot:     1" in capsys.readouterr().out
    piped(flow_repo, monkeypatch, 1)
    off = begin(flow_repo, {"module": "off"})
    assert main(["status", off.id]) == 0
    assert "slot:" not in capsys.readouterr().out


def test_a_failure_taking_the_slot_does_not_leak_the_run_lock(flow_repo, monkeypatch):
    piped(flow_repo, monkeypatch, 2)
    run = begin(flow_repo)

    def boom(cfg, run):
        raise OSError("disk gone")

    monkeypatch.setattr(slots, "acquire", boom)
    with pytest.raises(OSError):
        go(flow_repo, run)
    assert not flow_repo.events(run, "runner_error")
    lock = run.lock()
    assert lock.acquire()
    lock.release()


def test_a_start_that_loses_the_slot_race_ends_failed_with_no_worktree(flow_repo, monkeypatch):
    piped(flow_repo, monkeypatch, 2)
    begin(flow_repo)
    begin(flow_repo, {"module": "other"})
    trees, dirs = worktrees(flow_repo), run_dirs(flow_repo)
    monkeypatch.setattr(pipeline.slots, "check_free", lambda cfg, repo: None)  # the quick look passes: acquire loses
    with pytest.raises(slots.SlotsFull):
        begin(flow_repo, {"module": "third"})
    (new,) = run_dirs(flow_repo) - dirs
    lost = Run.load(flow_repo.root, new)
    assert lost.state["status"] == "failed" and "slot" not in lost.state
    assert [e["reason"] for e in flow_repo.events(lost, "run_failed")][0].startswith("SlotsFull: all 2 slots are held")
    assert worktrees(flow_repo) == trees
