"""Final-review fixes: config trust and symlinked runner files (Critical 1), runner commits (Important 3),
gate timeouts (Important 4), crashes that must not leave a run `running` (Important 5), and the cheap
validation items."""

from __future__ import annotations

import dataclasses
import fcntl
import json
import math
import os
import pty
import subprocess
import sys
import termios
import time
from pathlib import Path

import pytest
import yaml

from agentflow import gitops
from agentflow.config import Gate, Phase, Step, load_config, validate
from agentflow.config_trust import ConfigUntrusted, trusted_config_path
from agentflow.flow import main
from conftest import PYTHON_CONFIG
from agentflow.gates import run_gates
from agentflow.hitl import approve
from agentflow.pipeline import RunOptions, advance, start
from agentflow.run_control import ControlError, retry, spawn_resume
from agentflow.state import Run, display_status, runs_root
from test_gates import cfg_with

DONE = {"status": "done", "summary": "did it"}
PIPE = {"gate_sets": {"build": ["ok"], "verify": ["ok"]}}
KEY = "build/impl"


@pytest.fixture
def pipe(flow_repo):
    flow_repo.configure(PIPE)
    return flow_repo


def begin(flow_repo, pipeline: str = "feature", params: dict | None = None, **opts) -> Run:
    return start(flow_repo.cfg, flow_repo.root, pipeline, params or {"module": "demo"}, RunOptions(**opts))


def make_step(**kw) -> Step:
    fields = {"id": "impl", "type": "agent", "skill": "test-driven-development", "role": "build", "prompt": "step.md"}
    return Step(**(fields | kw))


def run_step(flow_repo, step: Step | None = None, run=None) -> str:
    step = step or make_step()
    phase = Phase(name="build", agent=None, model="sonnet", skip_if=None, steps=[step])
    return flow_repo.executor(run).run_agent_step(phase, step, KEY)


def hitl(flow_repo, run) -> list[dict]:
    return flow_repo.events(run, "hitl_raised")


# --------------------------------------------------------------------------- config trust (Critical 1)


def test_start_records_the_config_path_and_hash(pipe):
    run = begin(pipe)
    options = pipe.events(run, "run_options")[0]
    assert options["config"] == str(Path(pipe.cfg.path).resolve())
    assert len(options["config_sha256"]) == 64
    assert trusted_config_path(run, None, PYTHON_CONFIG) == Path(pipe.cfg.path).resolve()


def test_a_later_run_options_naming_another_config_is_refused(pipe, tmp_path, capsys):
    run = begin(pipe)
    evil = tmp_path / "evil.yaml"
    evil.write_text(Path(pipe.cfg.path).read_text(encoding="utf-8"), encoding="utf-8")
    run.emit("run_options", config=str(evil), config_sha256="0" * 64)
    with pytest.raises(ConfigUntrusted):
        trusted_config_path(run, None, PYTHON_CONFIG)
    assert main(["resume", run.id]) == 1
    assert "not started with" in capsys.readouterr().out
    assert pipe.calls() == []  # nothing ran


def test_a_config_changed_since_start_is_refused_unless_named(pipe, capsys):
    run = begin(pipe)
    path = Path(pipe.cfg.path)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["gates"]["ok"] = {"run": "touch pwned"}
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(ConfigUntrusted, match="changed since"):
        trusted_config_path(run, None, PYTHON_CONFIG)
    assert main(["resume", run.id]) == 1
    assert "changed since" in capsys.readouterr().out
    assert trusted_config_path(run, str(path), PYTHON_CONFIG) == path  # an explicit --config wins


def test_the_shipped_config_is_trusted_whatever_its_hash(repo, cfg):
    run = Run.create(cfg, repo, "feature", {"module": "crm"})
    run.emit("run_options", config=str(PYTHON_CONFIG), config_sha256="0" * 64)
    assert trusted_config_path(run, None, PYTHON_CONFIG) == PYTHON_CONFIG.resolve()


# --------------------------------------------------------------------------- symlinked runner files (Critical 1)


def test_symlinked_runner_log_is_refused(flow_repo, tmp_path):
    run = flow_repo.new_run()
    target = tmp_path / "target.txt"
    target.write_text("keep\n", encoding="utf-8")
    (run.dir / "runner.log").symlink_to(target)
    with pytest.raises(ControlError, match="symlink"):
        spawn_resume(run)
    assert target.read_text(encoding="utf-8") == "keep\n"


def test_a_closed_terminal_leaves_the_runner_working(pipe):
    pipe.script({"define/debug": [{"sleep": 2, "result": DONE}]})
    run = begin(pipe, "bug", {"task": "lỗi"}, to_phase="define")
    flow_py = Path(main.__code__.co_filename)
    terminal, tty = pty.openpty()
    runner = subprocess.Popen([sys.executable, str(flow_py), "resume", run.id], cwd=pipe.root,
                              stdin=tty, stdout=tty, stderr=tty, start_new_session=True,
                              preexec_fn=lambda: fcntl.ioctl(0, termios.TIOCSCTTY, 0))
    os.close(tty)
    try:
        deadline = time.monotonic() + 20
        while not pipe.events(run, "agent_started") and time.monotonic() < deadline:
            time.sleep(0.05)
        assert pipe.events(run, "agent_started")
        os.close(terminal)  # the window closes: the kernel hangs up the runner and its writes to the tty fail
        assert runner.wait(timeout=30) == 0
    finally:
        runner.kill()
    assert pipe.events(run, "agent_exited")[0]["exit_code"] == 0  # the runner saw its agent out
    assert run.id in (run.dir / "runner.log").read_text(encoding="utf-8")  # what it said after the hangup


def test_symlinked_gate_log_is_refused(tmp_git, tmp_path):
    target = tmp_path / "target.txt"
    target.write_text("keep\n", encoding="utf-8")
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "ok.log").symlink_to(target)
    (result,) = run_gates(cfg_with({"ok": "echo hi"}), ["ok"], tmp_git, logs)
    assert not result.passed and "symlink" in result.reason
    assert target.read_text(encoding="utf-8") == "keep\n"


def test_symlinked_test_command_log_is_refused(tmp_git, tmp_path):
    from agentflow.bug_eval import run_test_command

    target = tmp_path / "target.txt"
    target.write_text("keep\n", encoding="utf-8")
    log = tmp_path / "test_command.log"
    log.symlink_to(target)
    marker = tmp_path / "ran"
    with pytest.raises(OSError, match="symlink"):
        run_test_command(["touch", str(marker)], tmp_git, log, 10, 20)
    assert target.read_text(encoding="utf-8") == "keep\n" and not marker.exists()


# --------------------------------------------------------------------------- runner commits (Important 3)


def test_commit_refuses_when_head_moved(cfg, tmp_git, git):
    expected = git(tmp_git, "rev-parse", "HEAD").strip()
    git(tmp_git, "commit", "-q", "--allow-empty", "-m", "sneaky")
    (tmp_git / "README.md").write_text("changed\n", encoding="utf-8")
    with pytest.raises(gitops.CommitRejected) as caught:
        gitops.commit(tmp_git, "Work", cfg, expected_head=expected)
    assert caught.value.kind == "agent_commit"
    assert git(tmp_git, "diff", "--cached", "--name-only") == ""


def test_commit_runs_no_hooks(cfg, tmp_git, git, tmp_path):
    marker = tmp_path / "hook-ran"
    hooks = tmp_git / ".git" / "hooks"
    for name in ("pre-commit", "commit-msg", "prepare-commit-msg", "post-commit"):
        hook = hooks / name
        hook.write_text(f'#!/bin/sh\necho {name} >> "{marker}"\necho "Constraints-Approved-By: x" >> "$1"\n',
                        encoding="utf-8")
        hook.chmod(0o755)
    (tmp_git / "README.md").write_text("changed\n", encoding="utf-8")
    sha = gitops.commit(tmp_git, "Work", cfg, expected_head=git(tmp_git, "rev-parse", "HEAD").strip())
    assert not marker.exists()
    assert "Constraints-Approved-By" not in git(tmp_git, "log", "-1", "--format=%B", sha)


def test_commit_checks_the_message_it_created(cfg, tmp_git, git, monkeypatch):
    real = gitops.check_message
    seen = []

    def first_call_passes(cfg_, text):
        seen.append(text)
        return [] if len(seen) == 1 else real(cfg_, text)

    monkeypatch.setattr(gitops, "check_message", first_call_passes)
    (tmp_git / "README.md").write_text("changed\n", encoding="utf-8")
    with pytest.raises(gitops.CommitRejected, match="left in place") as caught:
        gitops.commit(tmp_git, "Work\n\nConstraints-Approved-By: me", cfg)
    assert caught.value.kind == "rejected_trailer"
    assert "Constraints-Approved-By" in git(tmp_git, "log", "-1", "--format=%B")  # visible, not rolled back


def test_a_gate_that_commits_makes_the_step_commit_refuse(flow_repo):
    flow_repo.configure({"gates": {"sneaky": {"run": "git commit -q --allow-empty -m sneaky"}}})
    run = flow_repo.new_run()
    flow_repo.script({KEY: [{"write": {"src/a.py": "x = 1\n"}, "result": DONE}]})
    head = flow_repo.head()
    assert run_step(flow_repo, make_step(gates=["sneaky"], commit=True, message="Work")) == "stopped"
    (raised,) = hitl(flow_repo, run)
    assert raised["kind"] == "guard_violation" and "agent_commit" in raised["summary"]
    assert flow_repo.events(run, "step_committed") == []
    from conftest import run_git

    assert run_git(flow_repo.root, "rev-list", f"{head}..HEAD").split() != []  # only the gate's own commit
    assert run_git(flow_repo.root, "log", "-1", "--format=%s").strip() == "sneaky"


def test_retrying_an_agent_commit_step_does_not_adopt_its_commit(flow_repo):
    run = flow_repo.new_run()
    flow_repo.script({KEY: [{"write": {"src/a.py": "x = 1\n"}, "git_commit": "agent commit", "result": DONE}]})
    assert run_step(flow_repo) == "stopped"
    assert run.refresh()["steps"][KEY]["status"] == "failed"
    retry(run, KEY, "me")
    calls = len(flow_repo.calls())
    assert run_step(flow_repo, run=run) == "stopped"
    assert len(flow_repo.calls()) == calls  # refused before any agent call
    last = hitl(flow_repo, run)[-1]
    assert last["kind"] == "guard_violation" and "agent_commit" in last["summary"]


def test_finish_refuses_commits_the_runner_did_not_make(pipe, git):
    run = begin(pipe, from_phase="ship")
    worktree = Path(run.state["worktree"])
    (worktree / "extra.txt").write_text("x\n", encoding="utf-8")
    git(worktree, "add", "-A")
    git(worktree, "commit", "-q", "-m", "not by the runner")
    assert advance(run, pipe.cfg, RunOptions()) == "waiting_human"
    (raised,) = hitl(pipe, run)
    assert raised["kind"] == "guard_violation" and raised["key"] == "ship/finish"
    assert "not made by the runner" in (run.inbox_path("ship/finish").read_text(encoding="utf-8"))
    assert "flow/feature/demo" not in git(pipe.origin, "branch", "--list")


# --------------------------------------------------------------------------- gates (Important 4)


def test_gate_times_out_and_its_group_is_killed(tmp_git, tmp_path):
    pid_file = tmp_path / "child.pid"
    cfg = cfg_with({"slow": f"sleep 30 & echo $! > {pid_file}; wait"})
    cfg = dataclasses.replace(cfg, defaults=dataclasses.replace(cfg.defaults, gate_timeout_min=0.01))
    began = time.monotonic()
    (result,) = run_gates(cfg, ["slow"], tmp_git, tmp_path / "logs")
    assert time.monotonic() - began < 10
    assert not result.passed and not result.skipped and result.reason == "timed out after 0.01 min"
    child = int(pid_file.read_text().strip())
    for _ in range(50):
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        pytest.fail("the gate's background child outlived the timeout")


def test_gate_with_a_missing_cwd_fails_with_the_path(tmp_git, tmp_path):
    cfg = cfg_with({"web": Gate(name="web", run="true", cwd="webadmin")})
    (result,) = run_gates(cfg, ["web"], tmp_git, tmp_path / "logs")
    assert not result.passed and not result.skipped and "webadmin" in result.reason


def test_gate_timeout_is_configured_and_validated(tmp_cfg):
    assert load_config(PYTHON_CONFIG).defaults.gate_timeout_min == 30
    path = tmp_cfg({"defaults": {"gate_timeout_min": 0}})
    assert any(i.path == "defaults.gate_timeout_min" for i in validate(load_config(path), path.parent.parent))


# --------------------------------------------------------------------------- crashes (Important 5)


def test_unexpected_error_in_advance_is_recorded_and_resumable(pipe, monkeypatch):
    from agentflow.lanes import Executor

    run = begin(pipe)
    real = Executor.run_steps

    def boom(self, *args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(Executor, "run_steps", boom)
    with pytest.raises(OSError):
        advance(run, pipe.cfg, RunOptions())
    run.refresh()
    assert run.state["status"] == "error" and "disk full" in run.state["failure"]
    (event,) = pipe.events(run, "runner_error")
    assert event["type"] == "runner_error" and event["error"] == "disk full"
    from agentflow.events import lock_state

    assert lock_state(run.dir) == "idle"
    assert display_status(run) == "error"
    monkeypatch.setattr(Executor, "run_steps", real)
    assert advance(run, pipe.cfg, RunOptions()) == "awaiting_approval"


def test_cli_turns_an_unexpected_error_into_a_message(pipe, monkeypatch, capsys):
    from agentflow.lanes import Executor

    run = begin(pipe)
    monkeypatch.setattr(Executor, "run_steps", lambda self, *a, **k: (_ for _ in ()).throw(OSError("no such binary")))
    assert main(["resume", run.id]) == 1
    out = capsys.readouterr().out
    assert "unexpected OSError: no such binary" in out and "agentflow resume" in out


def test_start_records_a_failure_after_the_run_exists(pipe, monkeypatch):
    import agentflow.pipeline as pipeline

    def broken(*_a, **_k):
        raise RuntimeError("worktree folder is read-only")

    monkeypatch.setattr(pipeline, "add_worktree", broken)
    with pytest.raises(RuntimeError):
        begin(pipe)
    (run,) = [Run.load(pipe.root, p.name) for p in runs_root(pipe.root).iterdir()]
    assert run.state["status"] == "failed" and "read-only" in run.state["failure"]


def test_running_run_without_a_runner_shows_stalled(repo, cfg):
    run = Run.create(cfg, repo, "feature", {"module": "crm"})
    assert display_status(run) == "stalled"


def test_queue_reports_a_crashed_run_and_frees_its_slot(flow_repo, monkeypatch):
    from agentflow import module_queue
    from agentflow.sources import ModuleNode

    run = flow_repo.new_run({"module": "platform"})
    run.emit("runner_error", error="disk full", exception="OSError")
    entry, holds_slot, problem = module_queue._assess(
        flow_repo.cfg, flow_repo.root, ModuleNode("platform", []), run, {}, [])
    assert not holds_slot and problem and "crashed" in problem and "disk full" in problem and "resume" in problem
    other = flow_repo.new_run({"module": "crm"})
    stale = "2026-01-01T00:00:00+00:00"
    monkeypatch.setitem(other.state, "updated_at", stale)
    _entry, holds, why = module_queue._assess(flow_repo.cfg, flow_repo.root, ModuleNode("crm", []), other, {}, [])
    assert not holds and why and "crashed" in why
    fresh = flow_repo.new_run({"module": "settings"})
    _entry, holds, why = module_queue._assess(flow_repo.cfg, flow_repo.root, ModuleNode("settings", []), fresh, {}, [])
    assert holds and why is None  # just started: its background runner has not taken the lock yet


# --------------------------------------------------------------------------- cheap items


def test_validate_rejects_permission_escapes_and_bad_values(tmp_cfg):
    def patch(data: dict) -> None:
        claude = data["agents"]["claude"]
        claude["effort_arg"] = ["--effort", "high"]
        claude["base_args"] = [*claude["base_args"], "--dangerously-skip-permissions", "--allowedTools=Bash"]
        claude["permission_mode"] = "yolo"
        fix = next(s for s in data["phases"]["build"]["steps"] if s["id"] == "fix")
        fix["expect"] = "maybe"
        next(s for s in data["phases"]["verify"]["steps"] if s["id"] == "eval_gate")["on_fail"] = "warn"

    path = tmp_cfg(patch)
    found = {(i.path, i.message.split(" ")[0]) for i in validate(load_config(path), path.parent.parent)
             if i.level == "error"}
    paths = {p for p, _ in found}
    assert "agents.claude.effort_arg" in paths and "agents.claude.base_args" in paths
    assert "agents.claude.permission_mode" in paths
    assert any(p.endswith(".expect") for p in paths) and any(p.endswith(".on_fail") for p in paths)


def test_validate_accepts_bypass_permissions(tmp_cfg):
    path = tmp_cfg(lambda data: data["agents"]["claude"].update(permission_mode="bypassPermissions"))
    errors = [i for i in validate(load_config(path), path.parent.parent) if i.level == "error"]
    assert not any(i.path == "agents.claude.permission_mode" for i in errors)


@pytest.mark.parametrize("budget", [math.nan, math.inf, -math.inf])
def test_budget_must_be_finite(flow_repo, budget):
    run = flow_repo.new_run()
    run.emit("hitl_raised", kind="budget_exceeded", key=KEY, summary="over", questions=[])
    with pytest.raises(ValueError, match="finite"):
        approve(run, KEY, "me", budget=budget)


def test_ids_are_matched_whole():
    from agentflow import hitl, module_queue

    with pytest.raises(hitl.ResultError):  # `$` alone would let a trailing newline through
        hitl._parse_question({"id": "q1\n", "question": "x"}, 0)
    with pytest.raises(module_queue.StartFailed):
        module_queue.spawn_feature_run(Path("."), "crm\n", lane="feature", command=["false"])


def test_dashboard_ignores_a_run_id_with_a_trailing_newline(flow_repo):
    from types import SimpleNamespace

    from agentflow import dashboard

    run = flow_repo.new_run()
    handler = object.__new__(dashboard._Handler)
    handler.server = SimpleNamespace(repo=flow_repo.root)
    assert handler._run(run.id) is not None
    assert handler._run(run.id + "\n") is None


def test_dashboard_budget_rejects_non_finite_json():
    from agentflow import dashboard

    assert json.loads("NaN") != json.loads("NaN")  # JSON parsing lets NaN through; the handler must not
    with pytest.raises(dashboard.BadRequest):
        dashboard._approve(None, {"budget": math.inf}, None)


def test_dashboard_approves_a_budget_stop_with_no_budget(flow_repo):
    from agentflow import dashboard

    run = flow_repo.new_run()
    run.emit("hitl_raised", kind="budget_exceeded", key=KEY, summary="over", questions=[])
    assert dashboard._approve(run, {"target": KEY}, flow_repo.cfg) == ({}, True)  # True: the run goes on
    assert run.state["waiting"] is None
