"""The `ci` step: wait for GitHub CI on the run's pull request, fix what fails, mark it ready. Never merge.

The runner does every `gh` call and every push; an agent only edits the tree, under the same guards as any
step, plus the step's `forbid`. Each round is:

1. Poll `gh pr checks` every `ci.poll_s` seconds (the first wait comes before the first poll, so a check that
   has not restarted yet is not read as this round's). A pause or a cancel ends the wait. GitHub reporting no
   checks for `ci.appear_min` minutes, or any still pending after `ci.timeout_min`, is a `ci_failed` stop. With
   `ci.watch_max_h` above 0, checks still pending at `ci.timeout_min` are watched instead: the run is held
   (`run_held`, reason `ci_pending`, which frees its queue slot) for up to that many hours of wall-clock time, and
   a human is told once that nothing is needed. During the watch a `gh` error or no checks is no verdict; past its
   end the hold ends, then the `ci_failed` stop is raised. Every way out of the watch ends the hold, once.
2. All passing or skipped: refresh the pull request's body, mark it ready, and finish.

A draft opened early (`ci.early_pr_after`, `pr_opened` with `early`) is watched only once `finish` has audited and
updated it (`pr_updated`); before that the step stops as if no pull request were open. Its checks are read only while
GitHub's head for it (`headRefOid`) is the sha the runner pushed last (`pr_updated`, the merge `finish` pushes again
for a conflicting PR, or a fix round's `ci_round`): until then they count as none yet, under the same `appear_min` and
watch limits, so the early push's checks never pass it.
3. A failure: run the step's `gates` (the environment), then its `fix` session with the failed jobs' logs. The
   session's own gates hold before the runner commits. A commit is pushed (in a vuln run only once the probe on it
   passed, `conformance.probe_before_push`, so a fix that pins the package back stops there); no commit means the
   session found nothing in the code to change, so the failed jobs run again, at most `ci.rerun_rounds` times.
4. After `ci.fix_rounds` fixes, a `ci_failed` stop. A human's approval earns fresh rounds.
"""

from __future__ import annotations

import re
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from agentflow.autonomy import AUTO_BY
from agentflow.config import Config, Phase, Step
from agentflow.conformance import probe_before_push
from agentflow.flow_control import halted
from agentflow.gates import gates_ok, run_gates
from agentflow.gitops import (
    GhError,
    GitError,
    head,
    job_log,
    mark_pr_ready,
    pr_checks,
    pr_head,
    push,
    rerun_failed,
    update_pr_body,
)
from agentflow.hitl import EVENT_EXTRA_MAX, notify_run, raise_hitl
from agentflow.issue_status import link

if TYPE_CHECKING:
    from agentflow.state import Run

Outcome = Literal["done", "skipped", "stopped"]
Verdict = Literal["pass", "fail", "stopped"]
GOOD = ("pass", "skipping")
PENDING = ("pending",)
JOB_LINK = re.compile(r"/actions/runs/(\d+)/job/(\d+)")
STOP = "ci_failed"
HOLD_REASON = "ci_pending"  # checks still pending after `ci.timeout_min`, watched for up to `ci.watch_max_h`


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
        if not opened or (opened[-1].get("early") and not self._finish_took(events)):
            return self._ci_stop(key, "There is no pull request to watch: `finish` has not opened one.")
        url = str(opened[-1]["url"])
        self._early = bool(opened[-1].get("early"))
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

    def _finish_took(self, events: list[dict]) -> bool:
        """`finish` audited the tree and opened or updated the pull request: a `pr_updated`, or a `pr_opened` that is
        not early, carrying the key of a `finish` step."""
        keys = _finish_keys(self.cfg)
        return any(e.get("key") in keys and (e["type"] == "pr_updated"
                                              or (e["type"] == "pr_opened" and not e.get("early")))
                   for e in events)

    def _pushed_sha(self) -> str:
        """The sha the runner pushed last to the pull request: of the `pr_updated`, `pr_opened`, `ci_round` and
        `base_synced` events (the merge `confirm_mergeable` pushes when GitHub calls the PR conflicting) that carry
        one, the latest by seq; "" when none does. `finish`'s own merge comes before its `pr_updated`, so it never
        shadows it. A merge committed but never pushed names a sha GitHub does not show: that is stale, never a pass."""
        shas = [str(e["sha"]) for e in self.run.events.read()
                if e["type"] in ("pr_updated", "pr_opened", "ci_round", "base_synced") and e.get("sha")]
        return shas[-1] if shas else ""

    def _stale_head(self, url: str) -> str:
        """For an early draft only: why its checks cannot be read yet ("" when they can). GitHub's head for it must
        be the sha the runner pushed last, or its checks may still be the early push's."""
        if not self._early:
            return ""
        want = self._pushed_sha()
        if not want:  # `_finish_took` guarantees one for a real early PR: none is never read as a match
            return f"no pushed commit is recorded for {url}"
        try:
            got = pr_head(self.worktree, url)
        except GhError as exc:
            return f"GitHub's head for {url} could not be read: {exc}"
        if got == want:
            return ""
        return f"GitHub still shows {url} at {got[:12] or 'no commit'}, not the pushed {want[:12]}"

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
        """Poll until the checks settle: `(verdict, what is wrong, checks)`.

        `appear_min` and `timeout_min` are measured on `monotonic`; the watch past `timeout_min` on the wall clock,
        as the runner's other holds are, since `monotonic` stops while the machine sleeps."""
        settings = self.cfg.ci
        poll = float(settings["poll_s"])
        timeout_min = settings["timeout_min"]
        watch_h = settings.get("watch_max_h") or 0
        started = time.monotonic()
        error = ""
        deadline: float | None = None  # epoch seconds: the end of the watch, once it started
        held = False  # a `run_held` not ended yet: the `finally` ends it on any way out not already ended
        try:
            while True:
                time.sleep(poll)
                self.run.refresh()
                if halted(self.run.state["status"]):
                    held = self._end_hold(key, held)
                    return "stopped", "", []
                waited = (time.monotonic() - started) / 60
                stale = self._stale_head(url)  # an early draft whose head is not the pushed sha: none yet
                try:
                    checks = [] if stale else pr_checks(self.worktree, url)
                    error = ""
                except GhError as exc:  # GitHub being unreachable for a moment is no verdict on the code
                    checks, error = [], str(exc)
                if not checks and not error and deadline is None:  # none yet: GitHub has not seen the push, or no CI
                    if waited >= float(settings["appear_min"]):
                        what = stale or f"GitHub reported no checks for {url}"
                        self._ci_stop(key, f"{what} after {waited:.0f} minutes.")
                        return "stopped", "", []
                    continue
                failed = failed_checks(checks or [])
                pending = [c for c in checks or [] if c.get("bucket") in PENDING]
                if checks and not failed and not pending:
                    held = self._end_hold(key, held)
                    return "pass", "", checks
                if checks and failed and not pending:
                    held = self._end_hold(key, held)
                    return "fail", ", ".join(_failed_names(checks)), checks
                if deadline is not None:  # watching: a `gh` error or no checks (just after a re-run) is no verdict
                    if time.time() >= deadline:
                        why = error or stale or (f"{len(pending)} check(s) still pending" if checks
                                                 else "GitHub reported no checks")
                        held = self._end_hold(key, held)  # before the stop: `metrics` reads the hold's end as a wait's
                        self._ci_stop(key, f"CI did not settle within {timeout_min} minutes and {watch_h} h of "
                                           f"watching: {why}.")
                        return "stopped", "", []
                    continue
                if waited >= float(timeout_min):
                    if float(watch_h) > 0:
                        now = time.time()
                        deadline = now + float(watch_h) * 3600
                        self.run.emit("run_held", key=key, reason=HOLD_REASON,
                                      until=datetime.fromtimestamp(deadline, UTC).isoformat(timespec="seconds"))
                        held = True
                        notify_run(self.cfg, self.run, f"{key}: CI is still running after {timeout_min} min; the run "
                                                       f"watches it for up to {watch_h} h and needs nothing from you.")
                        continue
                    why = error or f"{len(pending)} check(s) still pending"
                    self._ci_stop(key, f"CI did not settle within {timeout_min} minutes: {why}.")
                    return "stopped", "", []
        finally:
            if held:
                self.run.emit("run_hold_ended", key=key)

    _early: bool = False  # the pull request watched is a draft opened early: its head is checked before its checks

    def _end_hold(self, key: str, held: bool) -> bool:
        """End the CI watch's hold if one is open; returns the new `held` (False)."""
        if held:
            self.run.emit("run_hold_ended", key=key)
        return False

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
        """Push the fix commit; in a vuln run only once the probe on it passed (`probe_before_push`): the commit
        probed is the commit pushed."""
        if not self.runner_commits_only(key):
            return False
        branch = str(self.run.state.get("branch") or "")
        try:
            sha = head(self.worktree)
        except (GitError, subprocess.CalledProcessError) as exc:
            self._ci_raise("blocked", key, f"Could not read HEAD of {self.worktree}: {exc}")
            return False
        if not probe_before_push(self.cfg, self.run, self.worktree, key):
            return False
        try:
            push(self.worktree, branch, sha=sha)
        except GitError as exc:
            self._ci_raise("blocked", key, f"Could not push the CI fix to {branch}: {exc}")
            return False
        self.run.emit("ci_round", key=key, action="fix", round=round_, sha=sha, note="fixed and pushed",
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


def _finish_keys(cfg: Config) -> set[str]:
    """The step keys of every `type: finish` step, inside groups too."""
    keys: set[str] = set()

    def walk(steps: list[Step], prefix: str) -> None:
        for step in steps:
            key = f"{prefix}/{step.id}"
            if step.type == "finish":
                keys.add(key)
            walk(step.steps, key)

    for name, phase in cfg.phases.items():
        walk(phase.steps, name)
    return keys
