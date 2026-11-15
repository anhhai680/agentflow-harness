"""Preflight: the machine checks before a run's first phase (`preflight.py`, `_Loop.go`)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from agentflow import doctor, preflight, run_control
from agentflow.events import LOCK_NAME, lock_state
from agentflow.hitl import NotWaiting, approve, reject
from agentflow.pipeline import RunOptions, advance, start
from agentflow.state import Run

MISSING = "no-such-tool-xyz"


@pytest.fixture
def flow(flow_repo):
    flow_repo.configure({"preflight": {"enabled": True}})
    return flow_repo


def probe_with(flow, gates: dict, **extra):
    """`preflight.probe` over `gates` (name -> gate) added to the pack."""
    cfg = flow.configure({"gates": gates, **extra})
    return preflight.probe(cfg, list(gates), flow.root)


def script_on_path(tmp_path: Path, monkeypatch, name: str, body: str) -> Path:
    directory = tmp_path / "pathbin"
    directory.mkdir(exist_ok=True)
    script = directory / name
    script.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{directory}{os.pathsep}{os.environ['PATH']}")
    return script


def begin(flow, lane: str = "feature", **params):
    return start(flow.cfg, flow.root, lane, params or {"module": "demo"}, RunOptions())


def go(flow, run) -> str:
    return advance(run, flow.cfg, RunOptions())


def of(flow, run, etype: str) -> list[dict]:
    return flow.events(run, etype)


# --------------------------------------------------------------------------- route_gates


def test_route_gates_lists_every_set_the_lane_names(flow_repo):
    cfg = flow_repo.cfg
    names = preflight.route_gates(cfg, "bug")
    assert len(names) == len(set(names))
    for gate_set in ("setup", "build", "verify"):
        assert set(cfg.gate_sets[gate_set]) <= set(names)


# --------------------------------------------------------------------------- probe


def test_a_missing_program_fails_the_probe(flow_repo):
    result = probe_with(flow_repo, {"lint": {"run": f"{MISSING} --check"}})
    assert result.ok is False
    assert result.lines == [f"gate:lint: `{MISSING}` is not on PATH"]


def test_a_program_without_a_version_flag_passes(flow_repo, tmp_path, monkeypatch):
    script_on_path(tmp_path, monkeypatch, "fake-tool", "exit 2")
    assert probe_with(flow_repo, {"lint": {"run": "fake-tool --check"}}).ok


def test_a_program_that_cannot_start_fails(flow_repo, monkeypatch):
    monkeypatch.setattr(doctor, "run_bounded", lambda argv, cwd, env=None: (126, "permission denied"))
    result = probe_with(flow_repo, {"lint": {"run": "python3 -c 1"}})
    assert not result.ok and "could not run" in result.lines[0] and "permission denied" in result.lines[0]


def test_a_program_killed_by_a_signal_fails(flow_repo, monkeypatch):
    monkeypatch.setattr(doctor, "run_bounded", lambda argv, cwd, env=None: (-11, ""))
    assert not probe_with(flow_repo, {"lint": {"run": "python3 -c 1"}}).ok


def test_the_version_probe_gets_the_gates_environment(flow_repo, tmp_path, monkeypatch):
    script_on_path(tmp_path, monkeypatch, "marked-tool", '[ -n "$PROBE_MARK" ] || exit 127')
    gates = {"lint": {"run": "marked-tool"}}
    assert not probe_with(flow_repo, gates).ok
    assert probe_with(flow_repo, gates, defaults={"env": {"PROBE_MARK": "1"}}).ok


def test_preflight_skips_a_gate_whose_root_is_missing(flow_repo):
    gates = {"a": {"run": MISSING, "when_exists": "web/package.json"}, "b": {"run": MISSING, "cwd": "web"}}
    assert probe_with(flow_repo, gates).ok


def test_the_digest_is_the_same_for_the_same_problem(flow_repo):
    first = probe_with(flow_repo, {"lint": {"run": MISSING}})
    again = probe_with(flow_repo, {"lint": {"run": MISSING}})
    other = probe_with(flow_repo, {"lint": {"run": "other-missing-tool-xyz"}})
    assert first.digest == again.digest != other.digest


# --------------------------------------------------------------------------- the run


def test_a_missing_gate_program_stops_the_run_before_define(flow):
    flow.configure({"preflight": {"enabled": True}, "gates": {"bad": {"run": MISSING}}, "gate_sets": {"build": ["bad"]}})
    run = begin(flow)
    assert go(flow, run) == "waiting_human"
    raised = of(flow, run, "hitl_raised")
    assert [(e["kind"], e["key"]) for e in raised] == [("preflight_failed", "define/preflight")]
    assert not of(flow, run, "agent_started") and not of(flow, run, "gate_result")
    assert run.state["waiting"]["kind"] == "preflight_failed"
    assert go(flow, run) == "waiting_human" and len(of(flow, run, "hitl_raised")) == 1  # a second advance waits


def fail_setup(flow, command: str = "false"):
    flow.configure({"preflight": {"enabled": True}, "gates": {"s": {"run": command}}, "gate_sets": {"setup": ["s"]}})


def test_a_failing_setup_gate_stops_the_run_before_define(flow):
    fail_setup(flow)
    run = begin(flow)
    assert go(flow, run) == "waiting_human"
    assert [e["kind"] for e in of(flow, run, "hitl_raised")] == ["preflight_failed"]
    assert not of(flow, run, "agent_started") and not of(flow, run, "gate_result")
    assert "setup:s" in run.inbox_path("define/preflight").read_text(encoding="utf-8")


def test_approving_after_the_fix_runs_preflight_again(flow):
    fail_setup(flow)
    run = begin(flow)
    assert go(flow, run) == "waiting_human"
    fail_setup(flow, "true")
    approve(run, "define/preflight", by="me")
    assert go(flow, run) == "awaiting_approval"
    checks = of(flow, run, "preflight_checked")
    assert [e["ok"] for e in checks] == [False, True]
    assert run.state["steps"]["define/preflight"]["status"] == "done"
    finished = of(flow, run, "step_finished")[0]
    started = of(flow, run, "agent_started")[0]
    assert finished["key"] == "define/preflight" and finished["seq"] < started["seq"]
    assert checks[-1]["seq"] < started["seq"]


def test_a_preflight_stop_cannot_be_rejected(flow):
    fail_setup(flow)
    run = begin(flow)
    go(flow, run)
    with pytest.raises(NotWaiting, match="nothing to refine"):
        reject(run, "define/preflight", "me", "try again")


def test_a_preflight_stop_refuses_a_note(flow):
    fail_setup(flow)
    run = begin(flow)
    assert go(flow, run) == "waiting_human"
    with pytest.raises(ValueError, match="preflight runs again"):
        approve(run, "define/preflight", by="me", note="installed it")
    assert run.state["waiting"]["kind"] == "preflight_failed" and not of(flow, run, "hitl_approved")
    fail_setup(flow, "true")
    approve(run, "define/preflight", by="me")  # a plain approval still goes on
    assert go(flow, run) == "awaiting_approval"


def test_a_passing_preflight_is_cached_for_the_day(flow, tmp_path):
    counter = tmp_path / "counter.txt"
    flow.configure({"preflight": {"enabled": True}, "gates": {"s": {"run": f"echo ran >> {counter}"}},
                    "gate_sets": {"setup": ["s"]}})
    first, second = begin(flow, module="one"), begin(flow, module="two")
    go(flow, first)
    go(flow, second)
    assert [e["cached"] for e in of(flow, first, "preflight_checked")] == [False]
    assert [e["cached"] for e in of(flow, second, "preflight_checked")] == [True]
    assert counter.read_text(encoding="utf-8").splitlines() == ["ran"]


def test_a_run_that_already_started_skips_preflight(flow):
    run = begin(flow)
    run.emit("phase_started", phase="define", start_sha="0" * 40)
    fail_setup(flow)
    go(flow, run)
    assert not of(flow, run, "preflight_checked") and not of(flow, run, "hitl_raised")


def test_preflight_off_changes_nothing(flow_repo):
    run = begin(flow_repo)
    assert go(flow_repo, run) == "awaiting_approval"
    assert not of(flow_repo, run, "preflight_checked")


# --------------------------------------------------------------------------- re-checking an environment stop

RED = preflight.Probe(False, ["gate:lint: `pnpm` is not on PATH"], "e" * 64, "f" * 64)
GREEN = preflight.Probe(True, [], "0" * 64, "f" * 64)
KEY = "build/fix"


def probes(monkeypatch, *answers):
    """`preflight.probe` gives the next of `answers` (the last one repeats, an exception is raised); returns its calls."""
    calls: list[tuple[list[str], Path]] = []

    def probe(cfg, gates, cwd):
        calls.append((list(gates), Path(cwd)))
        answer = answers[min(len(calls), len(answers)) - 1]
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(preflight, "probe", probe)
    return calls


def environment_wait(flow):
    """A run waiting on an `environment` stop on `KEY`, with no runner holding its lock."""
    run = flow.new_run()
    run.emit("run_updated", worktree=str(flow.root))
    run.emit("environment_waiting", key=KEY, digest=RED.digest, lines=RED.lines, gates=["lint"], kind="gate_failed")
    run.emit("hitl_raised", kind="environment", key=KEY, phase="build", summary=f"{KEY} waits on this machine: x")
    assert run.state["waiting"]["kind"] == "environment"
    return run


def test_recheck_waiting_approves_a_fixed_machine_and_resumes(flow_repo, monkeypatch):
    calls = probes(monkeypatch, GREEN)
    run = environment_wait(flow_repo)
    spawned: list = []
    assert preflight.recheck_waiting(flow_repo.cfg, flow_repo.root, spawn=spawned.append) == [run.id]
    assert calls == [(["lint"], flow_repo.root)]
    run.refresh()
    assert run.state["waiting"] is None
    approved = of(flow_repo, run, "hitl_approved")[-1]
    assert approved["by"] == "runner" and approved["key"] == KEY and approved["note"]
    assert [(e["ok"], e["by"]) for e in of(flow_repo, run, "environment_rechecked")] == [(True, "runner")]
    assert [e["action"] for e in of(flow_repo, run, "stop_classified")] == ["environment_fixed"]
    assert [r.id for r in spawned] == [run.id]


def test_recheck_waiting_leaves_a_broken_machine_and_waits_between_checks(flow_repo, monkeypatch):
    calls = probes(monkeypatch, RED)
    run = environment_wait(flow_repo)
    spawned: list = []
    assert preflight.recheck_waiting(flow_repo.cfg, flow_repo.root, spawn=spawned.append) == []
    assert [e["ok"] for e in of(flow_repo, run, "environment_rechecked")] == [False]
    assert preflight.recheck_waiting(flow_repo.cfg, flow_repo.root, spawn=spawned.append) == []
    assert len(calls) == 1  # too soon to check again
    run.refresh()
    assert run.state["waiting"]["kind"] == "environment" and not spawned


def test_recheck_waiting_leaves_a_run_paused_during_the_probe(flow_repo, monkeypatch):
    run = environment_wait(flow_repo)
    wait = dict(run.state["waiting"])

    def probe(cfg, gates, cwd):  # a human pauses the run while the machine is checked
        run_control.pause(Run.load(flow_repo.root, run.id), by="me")
        return GREEN

    monkeypatch.setattr(preflight, "probe", probe)
    spawned: list = []
    assert preflight.recheck_waiting(flow_repo.cfg, flow_repo.root, spawn=spawned.append) == []
    run.refresh()
    assert run.state["status"] == "paused" and run.state["waiting"] == wait
    assert not of(flow_repo, run, "hitl_approved") and not of(flow_repo, run, "environment_rechecked")
    assert not spawned


def test_recheck_waiting_leaves_a_run_whose_runner_is_live(flow_repo, monkeypatch):
    calls = probes(monkeypatch, GREEN)
    run = environment_wait(flow_repo)
    lock = run.dir / LOCK_NAME
    lock.write_text(str(os.getpid()), encoding="utf-8")  # the runner that raised the stop is still unwinding
    assert lock_state(run.dir) == "live"
    spawned: list = []
    assert preflight.recheck_waiting(flow_repo.cfg, flow_repo.root, spawn=spawned.append) == []
    assert not calls and not spawned and not of(flow_repo, run, "environment_rechecked")
    assert not of(flow_repo, run, "hitl_approved")
    lock.unlink()
    assert preflight.recheck_waiting(flow_repo.cfg, flow_repo.root, spawn=spawned.append) == [run.id]
    assert [r.id for r in spawned] == [run.id]


@pytest.mark.parametrize("gone", ["missing", "a_file"])
def test_recheck_waiting_leaves_a_run_whose_worktree_is_gone(flow_repo, monkeypatch, tmp_path, gone):
    calls = probes(monkeypatch, GREEN)  # every gate's cwd would be missing, so a real probe would pass too
    run = environment_wait(flow_repo)
    worktree = tmp_path / "worktree"
    if gone == "a_file":
        worktree.write_text("", encoding="utf-8")
    run.emit("run_updated", worktree=str(worktree))
    wait = dict(run.state["waiting"])
    spawned: list = []
    assert preflight.recheck_waiting(flow_repo.cfg, flow_repo.root, spawn=spawned.append) == []
    run.refresh()
    assert not calls and not spawned and run.state["waiting"] == wait
    assert not of(flow_repo, run, "environment_rechecked") and not of(flow_repo, run, "hitl_approved")


def test_recheck_waiting_marks_a_run_it_cannot_start(flow_repo, monkeypatch):
    probes(monkeypatch, GREEN)
    run = environment_wait(flow_repo)
    lines: list[str] = []

    def spawn(run):
        raise run_control.ControlError("runner.log is a symlink")

    assert preflight.recheck_waiting(flow_repo.cfg, flow_repo.root, spawn=spawn, log=lines.append) == [run.id]
    run.refresh()
    assert run.state["waiting"] is None and run.state["status"] == "error"  # approved, and shown as needing a resume
    assert "runner.log is a symlink" in run.state["failure"]
    assert len(lines) == 1 and run.id in lines[0] and f"agentflow resume {run.id}" in lines[0]


def test_recheck_waiting_never_raises(flow_repo, monkeypatch):
    probes(monkeypatch, RuntimeError("probe broke"))
    environment_wait(flow_repo)
    lines: list[str] = []
    assert preflight.recheck_waiting(flow_repo.cfg, flow_repo.root, spawn=lambda run: None, log=lines.append) == []
    assert len(lines) == 1 and "probe broke" in lines[0]
    assert preflight.recheck_waiting(flow_repo.cfg, flow_repo.root, spawn=lambda run: None) == []  # stderr by default
