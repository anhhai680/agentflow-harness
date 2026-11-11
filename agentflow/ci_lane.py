"""The `ci` step: wait for GitHub CI on the run's pull request, fix what fails, mark it ready. Never merge.

The runner does every `gh` call and every push; an agent only edits the tree, under the same guards as any
step, plus the step's `forbid`. Each round is:

1. Poll `gh pr checks` every `ci.poll_s` seconds (the first wait comes before the first poll, so a check that
   has not restarted yet is not read as this round's). A pause or a cancel ends the wait. GitHub reporting no
   checks for `ci.appear_min` minutes, or any still pending after `ci.timeout_min`, is a `ci_failed` stop.
2. All passing or skipped: refresh the pull request's body, mark it ready, and finish.
3. A failure: run the step's `gates` (the environment), then its `fix` session with the failed jobs' logs. The
   session's own gates hold before the runner commits. A commit is pushed; no commit means the session found
   nothing in the code to change, so the failed jobs run again, at most `ci.rerun_rounds` times.
4. After `ci.fix_rounds` fixes, a `ci_failed` stop. A human's approval earns fresh rounds.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from agentflow.autonomy import AUTO_BY
from agentflow.config import Config, Phase, Step
from agentflow.flow_control import halted
from agentflow.gates import gates_ok, run_gates
from agentflow.gitops import (
    GhError,
    GitError,
    head,
    job_log,
    mark_pr_ready,
    pr_checks,
    push,
    rerun_failed,
    update_pr_body,
)
from agentflow.hitl import EVENT_EXTRA_MAX, raise_hitl
from agentflow.issue_status import link

if TYPE_CHECKING:
    from agentflow.state import Run

Outcome = Literal["done", "skipped", "stopped"]
Verdict = Literal["pass", "fail", "stopped"]
GOOD = ("pass", "skipping")
PENDING = ("pending",)
JOB_LINK = re.compile(r"/actions/runs/(\d+)/job/(\d+)")
STOP = "ci_failed"


def failed_checks(checks: list[dict]) -> list[dict]:
    return [c for c in checks if c.get("bucket") not in (*GOOD, *PENDING)]


def _failed_names(checks: list[dict]) -> list[str]:
    """The sorted names of the failed checks."""
    return sorted({str(c.get("name")) for c in failed_checks(checks)})


class CILane:
    """`run_ci` for `lanes.Executor`; relies on its `cfg`, `run`, `worktree`, `run_agent_step` and
    `runner_commits_only` and `write_pr_body`."""

    cfg: Config
    run: Run
    worktree: Path
    _ci_report: str = ""  # the last fix round's failed-job report, for its `ci_round` event

    def run_ci(self, phase: Phase, step: Step, key: str) -> Outcome:
        self.run.refresh()
        if self.run.state["waiting"] is not None:
            return "stopped"
        events = self.run.events.read()
        opened = [e for e in events if e["type"] == "pr_opened"]
        if not opened:
            return self._ci_stop(key, "There is no pull request to watch: `finish` has not opened one.")
        url = str(opened[-1]["url"])
        link(self.run)  # a link that failed at `finish` gets another try; a no-op once `issue_linked` is logged
        if any(e["type"] == "pr_marked_ready" for e in events):
            self.run.emit("step_finished", key=key, status="done", summary=f"ready for review: {url}")
            return "done"
        first = True
        while True:
            if not first:
                link(self.run)  # and again on every later round, until GitHub lists the link
            first = False
            fixes, reruns = self._rounds(key)
            self.run.emit("ci_watching", url=url, round=fixes + reruns)
            verdict, detail, checks = self._wait(key, url)
            if verdict == "stopped":
                return "stopped"
            if verdict == "pass":
                return self._ready(key, url)
            if fixes >= int(self.cfg.ci["fix_rounds"]):
                return self._ci_stop(key, f"CI is still failing after {fixes} fix round(s): {detail}.", self._report(key, checks))
            before = head(self.worktree)
            if self._fix(phase, step, key, self._next_session(key, step), checks) != "done":
                return "stopped"
            if head(self.worktree) != before:
                if not self._push(key, fixes + 1, checks):
                    return "stopped"
            elif reruns < int(self.cfg.ci["rerun_rounds"]):
                if not self._rerun(key, checks, reruns + 1):
                    return "stopped"
            else:
                return self._ci_stop(key, f"CI failed ({detail}), and the fix session found nothing in the code to change.",
                                  self._report(key, checks))

    def _next_session(self, key: str, step: Step) -> int:
        """The next fix session's number. Sessions are counted, not rounds: a round can reuse no finished session,
        whether its session changed nothing or a human's approval started the rounds again. A session a stop
        interrupted is resumed rather than counted past."""
        prefix = f"{key}/{step.fix.id if step.fix else 'fix'}-"
        rows = {k: r for k, r in self.run.state["steps"].items() if k.startswith(prefix)}
        unfinished = [k for k, r in rows.items() if r.get("status") not in ("done", "unchanged")]
        return int(unfinished[0].rsplit("-", 1)[1]) if unfinished else len(rows) + 1

    def _rounds(self, key: str) -> tuple[int, int]:
        """`(fixes, reruns)` since a human last approved a stop on this step."""
        events = self.run.events.read()
        for index in range(len(events) - 1, -1, -1):
            if events[index]["type"] == "hitl_approved" and events[index].get("key") == key:
                events = events[index + 1 :]
                break
        rounds = [e for e in events if e["type"] == "ci_round"]
        return (sum(1 for e in rounds if e.get("action") == "fix"), sum(1 for e in rounds if e.get("action") == "rerun"))

    def _wait(self, key: str, url: str) -> tuple[Verdict, str, list[dict]]:
        """Poll until the checks settle: `(verdict, what is wrong, checks)`."""
        settings = self.cfg.ci
        poll = float(settings["poll_s"])
        started = time.monotonic()
        error = ""
        while True:
            time.sleep(poll)
            self.run.refresh()
            if halted(self.run.state["status"]):
                return "stopped", "", []
            waited = (time.monotonic() - started) / 60
            try:
                checks = pr_checks(self.worktree, url)
                error = ""
            except GhError as exc:  # GitHub being unreachable for a moment is no verdict on the code
                checks, error = [], str(exc)
            if not checks and not error:  # none yet: GitHub has not seen the push, or there is no CI at all
                if waited >= float(settings["appear_min"]):
                    self._ci_stop(key, f"GitHub reported no checks for {url} after {waited:.0f} minutes.")
                    return "stopped", "", []
                continue
            failed = failed_checks(checks or [])
            pending = [c for c in checks or [] if c.get("bucket") in PENDING]
            if checks and not failed and not pending:
                return "pass", "", checks
            if checks and failed and not pending:
                return "fail", ", ".join(_failed_names(checks)), checks
            if waited >= float(settings["timeout_min"]):
                why = error or f"{len(pending)} check(s) still pending"
                self._ci_stop(key, f"CI did not settle within {settings['timeout_min']} minutes: {why}.")
                return "stopped", "", []

    def _report(self, key: str, checks: list[dict]) -> str:
        """The failed checks with the tail of each job's log, the log itself kept in the run."""
        keep = int(self.cfg.context.gate_output_lines)
        logs = self.run.gate_log_dir(key)
        logs.mkdir(parents=True, exist_ok=True)
        parts: list[str] = []
        seen: set[str] = set()
        for check in failed_checks(checks):
            link = str(check.get("link") or "")
            match = JOB_LINK.search(link)
            if match is None:
                parts.append(f"### {check.get('name')}\n\nNot a GitHub Actions job, so no log: {link or 'no link'}")
                continue
            job = match.group(2)
            name = str(check.get("name"))
            if name in seen:  # `pull_request` and `push` each run the workflow: one log per check is enough
                continue
            seen.add(name)
            text = job_log(self.worktree, job)
            (logs / f"ci-{job}.log").write_text(text, encoding="utf-8")
            tail = "\n".join(text.splitlines()[-keep:])
            parts.append(f"### {check.get('name')} (job {job})\n\n```\n{tail}\n```")
        return "\n\n".join(parts) or "(no failed job could be named)"

    def _fix(self, phase: Phase, step: Step, key: str, round_: int, checks: list[dict]) -> Outcome:
        if step.gates:
            results = run_gates(self.cfg, step.gates, self.worktree, self.run.gate_log_dir(key))
            if not gates_ok(results):
                failed = ", ".join(r.name for r in results if not (r.passed or r.skipped))
                return self._ci_stop(key, f"The environment for a CI fix did not come up: {failed}.")
        fix = step.fix
        if fix is None:
            raise ValueError(f"step {key}: a ci step needs `fix` (agentflow validate checks this)")
        plain = Phase(name=phase.name, agent=phase.agent, model=None, skip_if=None, steps=[])
        self._ci_report = self._report(key, checks)  # kept for this round's `ci_round` event
        return self.run_agent_step(plain, fix, f"{key}/{fix.id}-{round_}",
                                   extra={"ci_report": self._ci_report}, role=fix.role)

    def _push(self, key: str, round_: int, checks: list[dict]) -> bool:
        if not self.runner_commits_only(key):
            return False
        branch = str(self.run.state.get("branch") or "")
        try:
            push(self.worktree, branch)
        except GitError as exc:
            self._ci_raise("blocked", key, f"Could not push the CI fix to {branch}: {exc}")
            return False
        self.run.emit("ci_round", key=key, action="fix", round=round_, sha=head(self.worktree), note="fixed and pushed",
                      failed=_failed_names(checks), tail=self._ci_report[-EVENT_EXTRA_MAX:])
        return True

    def _rerun(self, key: str, checks: list[dict], round_: int) -> bool:
        """Nothing to fix in the code: a flake, or the infrastructure. Run the failed jobs again."""
        ids = sorted({m.group(1) for c in failed_checks(checks) if (m := JOB_LINK.search(str(c.get("link") or "")))})
        try:
            for run_id in ids:
                rerun_failed(self.worktree, run_id)
        except GhError as exc:
            self._ci_raise("blocked", key, f"Could not run the failed CI jobs again: {exc}")
            return False
        self.run.emit("ci_round", key=key, action="rerun", round=round_, note="no code change: failed jobs run again",
                      failed=_failed_names(checks))
        return True

    def _ready(self, key: str, url: str) -> Outcome:
        try:
            update_pr_body(self.worktree, url, self.write_pr_body())
            mark_pr_ready(self.worktree, url)
        except GhError as exc:
            self._ci_raise("blocked", key, f"CI passed, but {url} could not be marked ready: {exc}")
            return "stopped"
        self.run.emit("pr_marked_ready", by=AUTO_BY, url=url, key=key)
        self.run.emit("step_finished", key=key, status="done", summary=f"ready for review: {url}")
        return "done"

    def _ci_stop(self, key: str, summary: str, extra: str = "") -> Outcome:
        self._ci_raise(STOP, key, summary, extra)
        return "stopped"

    def _ci_raise(self, kind: str, key: str, summary: str, extra: str = "") -> None:
        raise_hitl(self.cfg, self.run, kind, key, summary, extra=extra)
