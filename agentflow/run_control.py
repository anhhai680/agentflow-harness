"""Run control: `pause`, `resume`, `mark_done`, `archive`, `cancel`, `retry` and `attach`, as the CLI and the dashboard call them.

Each action records one event with who took it, through `Run.emit`, and the runner reads the result:

- `pause` records `run_paused`. The runner checks before every step (`flow_control.halted`), so a live
  agent finishes its step and nothing after it starts. `resume` (or `agentflow resume`) records `run_resumed`,
  which on a ready run means: go on to the next phase.
- `mark_done` records `run_marked_done` on a ready run: the runner skips to the last phase, which ends it.
- `archive` records `run_archived` on a finished run, and the board moves its card to Done. Only the card changes:
  the run keeps its status, so a spec-only run still does not count as starting its module.
- `cancel` records `run_cancelled` (terminal), then SIGTERMs the process group of each live agent the
  state lists, SIGKILL after a grace period. A PID is signalled only once it is verified to still be that
  agent (PIDs get reused): alive, a session leader the way the adapter starts agents, not in this
  process's or the runner's group, running the agent's configured command, and started no later than the
  `agent_started` event. The runner sees the cancel as soon as the agent's call returns and stops.
- `retry` puts a step back to `pending` (`step_retry`), by default the run's failed step, clearing the stop
  the runner raised on it; a retried failed step still gets the failed step's check for forbidden paths. A step
  of an approved phase runs again, and the phase settles again (`pipeline.retried`). On a `conformance_failed`
  stop, retrying the step that would bring the missing evidence clears that stop too, so `finish` audits again.
- `attach` opens the waiting step's Claude Code session interactively in the run's worktree.

`spawn_resume` starts `flow.py resume <run>` in the background (new session, stdin closed, output in the
run's `runner.log`), the way `--detach` and the dashboard continue a run. A dashboard phase approval starts
nothing: the phase rests as ready until a human continues it or marks it done (D13, amended by D16).
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from agentflow.config import CONFORMANCE_FAILED, RETRO_PIPELINE, Config, ConfigError, load_config
from agentflow.config_trust import run_config_path
from agentflow import pack, safeio, sizing
from agentflow.events import lock_state, parse_iso
from agentflow.gitops import GitError, commits_between, dirty_paths
from agentflow.issue_status import link, release
from agentflow.state import TERMINAL, Run

FLOW = Path(__file__).resolve().parent / "flow.py"
KILL_GRACE_S = 5.0
SETTLE_S = 1.0  # a just-forked agent may not have exec'd its command yet
POLL_S = 0.05
AWAIT_POLL_S = 0.5  # each look runs `ps`, and the wait can last a whole step
START_SLACK_S = 2.0  # `ps` reports start times in whole seconds

__all__ = ["ControlError", "attach", "cancel", "mark_done", "pause", "resume", "retry", "run_config", "spawn_resume",
           "wait_for_agents"]


class ControlError(Exception):
    """The run is not in a state this action applies to (the message says why and what to do)."""


def run_config(run: Run) -> Config:
    """The config the run was started with when it is still trusted (`config_trust`), else the repository's
    pack when none is recorded; `ConfigUntrusted` otherwise."""
    return load_config(run_config_path(run))


def _refuse_terminal(run: Run, action: str) -> None:
    status = run.state["status"]
    if status in TERMINAL:
        raise ControlError(f"{run.id} is {status}; there is nothing to {action}")


def _refuse_retro(run: Run) -> None:
    """A retro run is never resumed (`pipeline.advance` refuses it): a retry or a resume would record a step or a run
    put back that no runner then takes. Refused before any event."""
    if run.state.get("pipeline") == RETRO_PIPELINE:
        raise ControlError(f"{run.id} is a retro run, which cannot be resumed; cancel it and run `agentflow retro` "
                           "again")


# --------------------------------------------------------------------------- pause and resume


def pause(run: Run, by: str) -> None:
    """Stop the run before its next step; a live agent finishes the step it is on."""
    with run.locked():
        _refuse_terminal(run, "pause")
        if run.state["status"] == "paused":
            raise ControlError(f"{run.id} is already paused; `agentflow resume {run.id}` continues it")
        _refuse_ready(run, "pause")
        run.emit("run_paused", by=by)


def resume(run: Run, by: str) -> bool:
    """Record who resumed a paused or ready run (`run_resumed`); True if it was. The caller starts the runner."""
    with run.locked():
        _refuse_terminal(run, "resume")
        _refuse_retro(run)
        if run.state["status"] not in ("paused", "ready"):
            return False
        run.emit("run_resumed", by=by)
        return True


def _refuse_ready(run: Run, action: str) -> None:
    """A ready run has no runner to stop or step to redo, and pausing it would turn a later resume into a
    silent start of the next phase."""
    if run.state["status"] == "ready":
        raise ControlError(f"{run.id} is ready, so there is nothing to {action}: continue it or mark it done")


def _has_work(worktree: Path, base: str) -> bool:
    """Whether a pull request from `worktree` has anything to hold: commits since `base`, or changes the
    last phase will commit (a design run's) or stop on."""
    try:
        return bool(commits_between(worktree, base) or dirty_paths(worktree))
    except GitError as exc:
        raise ControlError(f"could not read {worktree}: {exc}") from exc


def mark_done(run: Run, by: str) -> None:
    """Finish a ready run early (`run_marked_done`): the next runner skips to the last phase, which pushes
    and opens the draft pull request. The caller starts the runner; the decision survives if it never runs."""
    with run.locked():
        _refuse_terminal(run, "mark done")
        ready = run.state["ready"]
        if run.state["status"] != "ready" or ready is None:
            raise ControlError(f"{run.id} is not ready: approve its phase first")
        worktree = Path(run.state["worktree"] or "")
        if not run.state["worktree"] or not worktree.is_dir():
            raise ControlError(f"{run.id} has no worktree at {worktree}, so there is no branch to ship")
        if not _has_work(worktree, str(run.state["base_sha"] or "")):
            raise ControlError(f"{run.id} has nothing to ship: no commits since it started and no changes")
        run.emit("run_marked_done", by=by, phase=ready["phase"])


def archive(run: Run, by: str) -> None:
    """File a finished run away as old data (`run_archived`), so its card leaves the phase it stopped in."""
    with run.locked():
        status = run.state["status"]
        if status not in TERMINAL:
            raise ControlError(f"{run.id} is {status}: only a finished run can be archived")
        if run.state.get("archived"):
            raise ControlError(f"{run.id} is already archived")
        run.emit("run_archived", by=by)
    # outside the lock, so a gh call never holds it: the second try for a link or a release that failed earlier
    link(run)
    release(run)


# --------------------------------------------------------------------------- cancel


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _ps(pid: int) -> tuple[float, str] | None:
    """`(start time as epoch seconds, command line)` of `pid`, or None when `ps` cannot say."""
    try:
        done = subprocess.run(["ps", "-p", str(pid), "-o", "lstart=", "-o", "command="], capture_output=True,
                              text=True, env={**os.environ, "LC_ALL": "C"}, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    parts = done.stdout.strip().split(None, 5)
    if done.returncode != 0 or len(parts) < 6:
        return None
    try:
        started = time.mktime(time.strptime(" ".join(parts[:5]), "%a %b %d %H:%M:%S %Y"))
    except (ValueError, OverflowError):
        return None
    return started, parts[5]


def _runner_group(run: Run) -> int | None:
    try:
        return os.getpgid(int((run.dir / "runner.lock").read_text(encoding="utf-8").strip()))
    except (OSError, ValueError):
        return None


def _why_not(run: Run, row: dict, commands: dict[str, list[str]]) -> str | None:
    """Why `row`'s PID must not be signalled, or None when it is verified to be that agent."""
    pid = row.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1:
        return f"pid {pid!r} is never signalled"
    if pid == os.getpid():
        return "it is this process"
    if not _alive(pid):
        return "it is not running"
    try:
        sid, group = os.getsid(pid), os.getpgid(pid)
    except (ProcessLookupError, PermissionError):
        return "it is not running (or not ours)"
    if group in (os.getpgrp(), _runner_group(run)):
        return "it is in this process's or the runner's process group"
    if sid != pid or group != pid:
        return "it is not the session leader the runner starts each agent as"
    command = commands.get(str(row.get("agent")))
    if not command:
        return f"agent {row.get('agent')!r} has no configured command"
    info = _ps(pid)
    if info is None:
        return "ps could not describe it"
    marker = Path(command[-1]).name
    if marker not in info[1]:
        return f"its command does not run {marker}"
    recorded = parse_iso(row.get("started_at"))
    if recorded is None:
        return "the state has no start time for it"
    if info[0] > recorded.timestamp() + START_SLACK_S:
        return "it started after the agent was recorded (the PID was reused)"
    return None


def _verify(run: Run, commands: dict[str, list[str]], skip: set[int], settle_s: float) -> tuple[list[int], list[dict]]:
    """The live agents' PIDs that are verified, and `{pid, reason}` for the others. A command that does not
    match yet is looked at again for `settle_s` (the fork has not exec'd)."""
    rows = [r for r in run.state["agents"].values() if r.get("pid") not in skip]
    verified: list[int] = []
    refused: dict[int, str] = {}
    deadline = time.monotonic() + settle_s
    while True:
        pending = []
        for row in rows:
            why = _why_not(run, row, commands)
            if why is None:
                verified.append(row["pid"])
            elif why.startswith("its command") and time.monotonic() < deadline:
                pending.append(row)
            else:
                refused[row.get("pid")] = why
        if not pending:
            break
        rows = pending
        time.sleep(POLL_S * 2)
    return verified, [{"pid": pid, "reason": why} for pid, why in refused.items()]


def _agent_commands(run: Run, cfg: Config | None) -> dict[str, list[str]]:
    """The agent commands `cancel` matches live processes against. They only name processes, never run, so an
    untrusted or unloadable recorded config gives way to the repository's pack, and with neither no process
    matches."""
    if cfg is None:
        try:
            cfg = run_config(run)
        except (ConfigError, pack.PackError):  # ConfigUntrusted is a ConfigError
            try:
                cfg = load_config(pack.pack_config(run.repo))
            except (ConfigError, pack.PackError):
                return {}
    return {name: list(spec.command) for name, spec in cfg.agents.items()}


def _signal(pid: int, sig: int) -> bool:
    try:
        os.killpg(pid, sig)
    except ProcessLookupError:
        return False
    return True


def cancel(run: Run, by: str, cfg: Config | None = None, grace_s: float = KILL_GRACE_S) -> list[int]:
    """End the run (`run_cancelled`) and stop its live agents; returns the PIDs signalled."""
    run.refresh()
    _refuse_terminal(run, "cancel")
    commands = _agent_commands(run, cfg)
    verified, skipped = _verify(run, commands, set(), SETTLE_S)
    with run.locked():
        _refuse_terminal(run, "cancel")
        run.emit("run_cancelled", by=by, killed=verified, skipped=skipped)
    try:
        signalled = [pid for pid in verified if _signal(pid, signal.SIGTERM)]
        run.refresh()  # an agent started between the check and the event is stopped too
        late, _ = _verify(run, commands, set(verified), 0.0)
        signalled += [pid for pid in late if _signal(pid, signal.SIGTERM)]
        _settle(run, signalled, commands, grace_s)
    finally:
        release(run)  # last, outside the lock: a slow gh call never delays stopping the agents
    return verified + late


def _settle(run: Run, signalled: list[int], commands: dict[str, list[str]], grace_s: float) -> None:
    """Give the agents just sent SIGTERM `grace_s` to exit, then SIGKILL the ones still verified alive."""
    deadline = time.monotonic() + grace_s
    rows = {r.get("pid"): r for r in run.state["agents"].values()}
    while time.monotonic() < deadline:
        if not [p for p in signalled if p in rows and _why_not(run, rows[p], commands) is None]:
            return
        time.sleep(POLL_S)
    for pid in signalled:
        if pid in rows and _why_not(run, rows[pid], commands) is None:
            _signal(pid, signal.SIGKILL)


def wait_for_agents(run: Run, cfg: Config, key: str, timeout_s: float, grace_s: float = KILL_GRACE_S) -> list[int]:
    """Wait for agents that outlived their runner, so a resumed session never shares their files; returns
    their PIDs. One still running after `timeout_s` is stopped the way `cancel` stops it."""
    commands = {name: list(spec.command) for name, spec in cfg.agents.items()}
    alive, _ = _verify(run, commands, set(), 0.0)
    if not alive:
        return []
    run.emit("agent_awaited", key=key, pids=alive)
    rows = {r.get("pid"): r for r in run.state["agents"].values()}
    deadline = time.monotonic() + timeout_s
    remaining = alive
    while remaining and time.monotonic() < deadline:
        time.sleep(AWAIT_POLL_S)
        remaining = [p for p in remaining if _why_not(run, rows[p], commands) is None]
    _settle(run, [p for p in remaining if _signal(p, signal.SIGTERM)], commands, grace_s)
    return alive


# --------------------------------------------------------------------------- retry


def retry(run: Run, key: str | None, by: str, reason: str | None = None) -> str:
    """Put `key` (default: the run's failed step) back to pending; returns the key. The caller resumes."""
    with run.locked():
        _refuse_terminal(run, "retry")
        _refuse_retro(run)
        _refuse_ready(run, "retry")
        steps = run.state["steps"]
        if key is None:
            failed = [k for k, row in steps.items() if row.get("status") == "failed"]
            if not failed:
                raise ControlError(f"{run.id} has no failed step; name the one to retry: `agentflow retry {run.id} <step>`")
            key = failed[-1]
        elif key not in steps:
            raise ControlError(f"{run.id} has no step {key!r}")
        if lock_state(run.dir) == "live":
            raise ControlError(f"a runner is working on {run.id}; pause it (`agentflow pause {run.id}`) and retry once it stops")
        waiting = run.state["waiting"]
        if waiting is not None and waiting.get("key") == key:
            if waiting.get("kind") == "phase_approval" or waiting.get("questions"):
                raise ControlError(f"{key!r} waits for an answer or an approval, not a retry")
            run.emit("hitl_approved", by=by, key=key, kind=waiting["kind"], summary=waiting.get("summary", ""))
        elif waiting is not None and waiting.get("kind") == CONFORMANCE_FAILED:
            # retrying a step that would bring missing evidence is the way on this stop names: `finish` audits again
            run.emit("hitl_approved", by=by, key=waiting["key"], kind=waiting["kind"], summary=waiting.get("summary", ""))
        run.emit("step_retry", by=by, key=key, reason=reason)
    return key


# --------------------------------------------------------------------------- attach and spawn


def attach(run: Run, cfg: Config) -> int:
    """Run `claude --resume <session> --effort <level>` interactively in the run's worktree; its exit code."""
    if lock_state(run.dir) == "live":
        raise ControlError(f"a runner is working on {run.id}; `agentflow pause {run.id}` and wait for it to stop first")
    state = run.state
    waiting = state["waiting"] or {}
    key = waiting.get("key") if waiting.get("key") in state["steps"] else state["step"]
    row = state["steps"].get(key) or {}
    if not row.get("session_id"):
        raise ControlError(f"{run.id} has no agent session to attach to" + (f" at {key}" if key else ""))
    agent = row.get("agent") or cfg.defaults.agent
    if agent != "claude" or "claude" not in cfg.agents:
        raise ControlError(f"attach opens Claude Code sessions; {key} ran on {agent!r}")
    effort = row.get("effort") or cfg.effort.default
    role = row.get("role") or next((a.get("role") for a in state["agents"].values()
                                    if a.get("step") == key and a.get("role")), None) or "build"
    effort = sizing.floor(effort, sizing.is_judgement(row.get("phase") or "", role), cfg.effort.allowed)
    argv = [*cfg.agents["claude"].command, "--resume", row["session_id"], "--effort", effort]
    worktree = state.get("worktree") or str(run.repo)
    return subprocess.run(argv, cwd=str(worktree), check=False).returncode


def spawn_resume(run: Run, config: str | Path | None = None) -> Path:
    """Start `flow.py resume <run>` in the background; returns the log it writes to."""
    command = [sys.executable, str(FLOW), "resume", run.id]
    if config:
        command += ["--config", str(config)]
    log_path = run.dir / "runner.log"
    try:
        log = safeio.open_append(log_path)
    except safeio.SymlinkRefused as exc:
        raise ControlError(f"{exc}: remove it, then resume {run.id}") from exc
    with log:
        subprocess.Popen(command, cwd=run.repo, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                         start_new_session=True)
    return log_path
