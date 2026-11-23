"""The bug lane's test expectations and the eval lane's blocking gate step.

`BugEvalLane` is mixed into `lanes.Executor`, ahead of `flow_control.StepControl`.

- `expect: red` (the bug lane's regression test): once the step is done and its gates pass, the runner
  runs `signals.test_command` from the step's result in the worktree. The test must fail: exit 0 stops
  the run (`red_test_passes`) with no fix call, since a passing test does not reproduce the bug, and a
  result with no command stops it (`blocked`).
- `expect: green` (the fix): the same command (the last one a red check accepted) must pass, in
  addition to the step's gates; a failure is one more failing "gate" (`test_command`) for the fix loop.
- `expect: advisory_present` (the vuln lane's `advisory_red` gate step) and `expect: advisory_cleared` (its
  `patch`): the runner runs the pack's probe gate (`vuln.run_probe`) and judges its output against the run's
  advisory ids. Before the patch an id must be named, or the run stops (`advisory_not_reproduced`); after it none
  may be, or that is one more failing "gate" (`advisory_cleared`) for the fix loop. A probe that did not run
  cleanly stops the run either way (`probe_failed`) and is never a clean scan. The probe's effect on the tree is
  guard-checked like the test command's, and in a vuln run the scanner ignore files are forbidden
  (`vuln.forbidden`).
- A gate step with `fix_loop` (the eval gate) runs its gates first; when they fail, an agent session is
  started with the gate output and resumed with it up to `defaults.fix_attempts` times (the agent step
  lifecycle's own fix loop). With `on_fail: block` the stop after the last attempt is `eval_regression`.
  The guards still apply to that session, so an edit to `eval/baseline.json` (a forbidden path) fails
  it with `guard_violation`: a regression is never passed by moving the baseline. When the fix session
  changed files, the step commits them (`Fix <step> gate failures`) once the gates pass, so the lane
  ends with a clean tree.

Trust model for the test command. It is untrusted text from the agent's result, and the runner runs it
with the runner's own privileges and environment: outside Claude Code, so the `.claude/settings*.json`
rules the agent is held to do not apply by themselves. The runner therefore narrows it before running:

- one simple command: any shell metacharacter or operator (`; | & $ ( ) < >`, a backtick, a newline)
  stops the step (`blocked`) and nothing runs; the command is split with `shlex` and run with no shell
  (an argv list), so nothing is expanded, chained or redirected;
- the human's merged `.claude/settings.json` + `settings.local.json`, read from the MAIN checkout (never
  the run's worktree, whose ignored `settings.local.json` the agent could write unseen by the guards),
  must allow it as a `Bash(...)` command with no `ask` or `deny` match (`guards.command_allowed`): the
  rules the human set for the agent's own Bash calls, so it cannot run through the runner what it may
  not run itself; otherwise `blocked`;
- stdin closed, a timeout (`defaults.step_timeout_min`), its own process group (killed whole at the
  timeout or when the runner itself fails), and its output in the step's gate log directory;
- its effect on the tree is guard-checked: a change it makes to a `guardrails.forbidden_paths` path
  fails the step (`guard_violation`), as an agent's edit would.

An allowed program can still do whatever it does (a test runner runs test code the agent wrote); that is
the trust already given to the agent and to the gates. The command's output is logged like gate output.
Inputs from outside the run (issue, log, task text) never reach this command.
"""

from __future__ import annotations

import contextlib
import os
import re
import shlex
import signal
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Literal

from agentflow import safeio, vuln
from agentflow.config import ADVISORY_CLEARED, ADVISORY_PRESENT
from agentflow.gates import GateResult, gates_ok, record_results, run_gates, summarize
from agentflow.guards import Violation, changed_paths, command_allowed, path_matches, snapshot
from agentflow.hitl import StepResult, raise_hitl

if TYPE_CHECKING:
    from agentflow.config import Config, Phase, Step
    from agentflow.state import Run

TEST_GATE = "test_command"
GATE_FIX_TEMPLATE = "gate_fix.md"
BLOCK = "block"
RED, GREEN = "red", "green"
RESUMABLE = ("running", "waiting_human")
SHELL_SYNTAX = re.compile(r"[;|&$()<>`\n\r]")
KILL_WAIT_S = 5


def exhausted_kind(step: Step) -> str:
    """The stop raised when a step's gates still fail after its fix attempts."""
    return "eval_regression" if step.on_fail == BLOCK else "fix_attempts_exhausted"


@dataclass
class CommandRun:
    command: str
    exit_code: int | None  # None: killed at the timeout
    log_path: Path
    tail: str

    @property
    def timed_out(self) -> bool:
        return self.exit_code is None


def command_argv(command: str) -> tuple[list[str], str]:
    """`(argv, "")` for one simple command, or `([], why)` when it is not one (shell syntax, bad quoting)."""
    found = SHELL_SYNTAX.search(command)
    if found:
        return [], f"it uses shell syntax ({found.group(0)!r}); give one plain command, no operators or substitutions"
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        return [], f"it cannot be split into arguments ({exc})"
    return (argv, "") if argv else ([], "it is empty")


def _kill_group(proc: subprocess.Popen) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):  # it may have ended on its own meanwhile
        os.killpg(proc.pid, signal.SIGKILL)


def run_test_command(argv: list[str], cwd: Path, log_path: Path, timeout_s: int, tail_lines: int) -> CommandRun:
    """Run `argv` with no shell in `cwd`, stdin closed, its process group killed after `timeout_s` (and
    whenever this function is left with it still running). Raises `OSError` when it cannot start, or when
    `log_path` is a symlink (checked before anything runs; the log is never written through one)."""
    shown = shlex.join(argv)
    safeio.write_text(log_path, f"$ {shown}\n")
    proc = subprocess.Popen(
        argv, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", start_new_session=True,
    )
    code: int | None = None
    try:
        try:
            output, _ = proc.communicate(timeout=timeout_s)
            code = proc.returncode
        except subprocess.TimeoutExpired:
            _kill_group(proc)
            try:
                output, _ = proc.communicate(timeout=KILL_WAIT_S)
            except subprocess.TimeoutExpired:
                output = ""
            output = (output or "") + f"\n[agentflow: killed after {timeout_s}s]\n"
    finally:
        if proc.returncode is None:  # left early (timeout, an exception, Ctrl-C): take its children too
            _kill_group(proc)
    safeio.write_text(log_path, f"$ {shown}\n{output}")
    tail = "\n".join(output.splitlines()[-tail_lines:]) if tail_lines > 0 else ""
    return CommandRun(shown, code, log_path, tail)


class BugEvalLane:
    """`check_expect` (for `lanes.Executor._loop`) and the fix-looping `_run_gate_step`; relies on the
    executor's `cfg`, `run`, `worktree`, `run_agent_step`, `current_wait`, `_stop`, `_fail`, and `_gate_report`."""

    cfg: Config
    run: Run

    # ------------------------------------------------------------------ expect: red | green

    def check_expect(self, sr: Any, result: StepResult, gates_failed: bool) -> list[GateResult] | Literal["stopped"]:
        """After a `done` result: `[]` when the step's expectation holds (or it has none), the failed
        test as a gate result for the fix loop (`green`), or "stopped" when a human is needed."""
        expect = sr.step.expect
        if expect == ADVISORY_CLEARED:
            return self._check_cleared(sr)
        if expect not in (RED, GREEN) or (expect == RED and gates_failed):
            return []
        command = self._test_command(expect, result)
        if not command:
            return self._stop(sr, "blocked", f"The step's result gives no `signals.test_command`, so the runner "
                              f"cannot check that the test is {expect}. Approve to resume the step: it must name the "
                              "exact command that runs the regression test.")
        argv, why = command_argv(command)
        if not why and not command_allowed(self.worktree, command):
            why = ("no Bash allow rule in .claude/settings*.json covers it (or an ask/deny rule matches): "
                   "add a narrow Bash(...) allow rule or change the test command")
        if why:
            return self._stop(sr, "blocked", f"The runner will not run the test command `{command}`: {why}. "
                              "Approve to resume the step.")
        timeout = max(1, round(self.cfg.defaults.step_timeout_min * 60))
        before = snapshot(self.worktree)
        try:
            ran = run_test_command(argv, self.worktree, self.run.gate_log_dir(sr.key) / f"{TEST_GATE}.log",
                                   timeout, self.cfg.context.gate_output_lines)
        except OSError as exc:
            return self._stop(sr, "blocked", f"The test command `{command}` could not start: {exc}. "
                              "Approve to resume the step.")
        touched = self._forbidden_changes(before)
        if touched:
            return self._fail(sr, [Violation("forbidden_path", f"{path} (changed by the test command `{command}`)")
                                   for path in touched])
        ok = not ran.timed_out and (ran.exit_code != 0 if expect == RED else ran.exit_code == 0)
        self.run.emit("expectation_checked", key=sr.key, expect=expect, command=command, exit_code=ran.exit_code,
                      ok=ok, log=str(ran.log_path), tree=before)
        report = self._command_report(ran)
        if expect == GREEN:
            return [] if ok else [GateResult(TEST_GATE, False, False, self._reason(ran), ran.log_path, ran.tail)]
        if ok:
            return []
        if ran.timed_out:
            return self._stop(sr, "blocked", f"The regression test `{command}` ran past {timeout}s: a hang does not "
                              "show that it reproduces the bug. Approve to resume the step.", extra=report)
        return self._stop(sr, "red_test_passes", f"The regression test passes before any fix (`{command}` exited 0), "
                          "so it does not reproduce the bug. Approve to resume the step: the test must fail for the "
                          "bug's reason.", extra=report)

    def _forbidden_changes(self, before: str) -> list[str]:
        """Forbidden paths the tree changed in since the `before` snapshot: `guardrails.forbidden_paths`, plus the
        scanner ignore files in a vuln run (`vuln.forbidden`)."""
        lane = str(self.run.state.get("pipeline") or "")
        patterns = [*self.cfg.guardrails.forbidden_paths, *vuln.forbidden(self.cfg, lane)]
        paths = changed_paths(self.worktree, before, snapshot(self.worktree))
        return [p for p in paths if any(path_matches(p, g) for g in patterns)]

    def _test_command(self, expect: str, result: StepResult) -> str:
        own = result.signals.get("test_command")
        own = own.strip() if isinstance(own, str) else ""
        if expect == RED:
            return own
        for event in reversed(self.run.events.read()):  # green runs the command the red check accepted
            if event["type"] == "expectation_checked" and event.get("expect") == RED and event.get("ok"):
                return str(event.get("command") or "") or own
        return own

    @staticmethod
    def _reason(ran: CommandRun) -> str:
        return "timed out" if ran.timed_out else f"exit {ran.exit_code}"

    def _command_report(self, ran: CommandRun) -> str:
        return self._gate_report([GateResult(TEST_GATE, False, False, self._reason(ran), ran.log_path, ran.tail)])

    # ------------------------------------------------------------------ expect: advisory_present | advisory_cleared

    def _probe(self, key: str, expect: str) -> tuple[str, list[str], GateResult, list[Violation]]:
        """Run the vuln probe for `key` (`vuln.run_probe`), and the guard check of what it did to the tree: a probe that
        writes a forbidden path (an ignore file) is a `forbidden_path` violation, as an agent's edit would be."""
        before = snapshot(self.worktree)
        verdict, matched, result = vuln.run_probe(self.cfg, self.run, self.worktree, key, expect)
        name = self.cfg.vuln.probe
        touched = [Violation("forbidden_path", f"{path} (changed by the probe gate `{name}`)")
                   for path in self._forbidden_changes(before)]
        return verdict, matched, result, touched

    def _probe_failed(self, sr: Any, result: GateResult) -> Literal["stopped"]:
        report = self._gate_report([replace(result, reason=result.reason or "no advisory id in its output")])
        return self._stop(sr, "probe_failed", f"The probe gate `{result.name}` did not run cleanly (a missing tool, a "
                          "crash, a timeout, empty output, or a 'found' exit that names no advisory), so it shows "
                          "nothing about the advisory and is never read as a clean scan.", extra=report)

    def _check_cleared(self, sr: Any) -> list[GateResult] | Literal["stopped"]:
        """`expect: advisory_cleared` (the patch): the probe must run cleanly and name none of the run's advisory ids.
        Still named, it is a failing "gate" for the fix loop; a probe that did not run cleanly stops the run."""
        verdict, matched, result, touched = self._probe(sr.key, ADVISORY_CLEARED)
        if touched:
            return self._fail(sr, touched)
        if verdict == "failed":
            return self._probe_failed(sr, result)
        if verdict == "absent":
            return []
        return [GateResult(ADVISORY_CLEARED, False, False, f"advisory still reported: {', '.join(matched)}",
                           result.log_path, result.tail)]

    def _run_probe_step(self, step: Step, key: str) -> str:
        """A gate step with `expect: advisory_present` (`advisory_red`): the probe must run cleanly and name at least
        one of the run's advisory ids before any agent patches anything."""
        if self.current_wait() is not None:
            return "stopped"
        sr = SimpleNamespace(key=key, step=step)  # what `_fail` and `_stop` read of a step run
        verdict, matched, result, touched = self._probe(key, ADVISORY_PRESENT)
        if touched:
            return self._fail(sr, touched)
        if verdict == "failed":
            return self._probe_failed(sr, result)
        if verdict == "absent":
            report = self._gate_report([replace(result, reason="no advisory id of the alert in its output")])
            return self._stop(sr, "advisory_not_reproduced", f"The probe gate `{result.name}` ran cleanly but does "
                              "not name the alert's advisory, so the run cannot show the patch fixes it: the alert may "
                              "be stale, or the scanner may not see it.", extra=report)
        self.run.emit("step_finished", key=key, status="done", summary=f"advisory reported: {', '.join(matched)}")
        return "done"

    # ------------------------------------------------------------------ gate steps with a fix loop

    def _run_gate_step(self, phase: Phase, step: Step, key: str) -> str:
        """A gate step: its gates, then (with `fix_loop`) an agent fix session, and the `on_fail` stop. A step with
        `expect: advisory_present` runs the vuln probe instead (`_run_probe_step`)."""
        if step.expect == ADVISORY_PRESENT:
            return self._run_probe_step(step, key)
        if not step.fix_loop and step.on_fail != BLOCK:
            return super()._run_gate_step(phase, step, key)  # the plain gate step of `flow_control`
        if self.run.state["waiting"] is not None:
            return "stopped"
        rec = self.run.state["steps"].get(key) or {}
        fixer = replace(step, prompt=step.prompt or GATE_FIX_TEMPLATE, commit=True,
                        message=step.message or f"Fix {step.id} gate failures")  # commits only what the fix changed
        if rec.get("status") in RESUMABLE and rec.get("session_id"):
            return self.run_agent_step(phase, fixer, key)  # its fix session was going: resume it (it runs the gates)
        tree = snapshot(self.worktree)
        results = run_gates(self.cfg, step.gates or [], self.worktree, self.run.gate_log_dir(key))
        record_results(self.run, key, results, tree)
        if gates_ok(results):
            self.run.emit("step_finished", key=key, status="done", summary=summarize(results))
            return "done"
        failed = [r for r in results if not (r.passed or r.skipped)]
        if step.fix_loop and self.cfg.defaults.fix_attempts > 0:
            return self.run_agent_step(phase, fixer, key, fix_first=failed)
        names = ", ".join(r.name for r in failed)
        raise_hitl(self.cfg, self.run, exhausted_kind(step), key, f"Gates failed in {key}: {names}.",
                   extra=self._gate_report(failed))
        return "stopped"
