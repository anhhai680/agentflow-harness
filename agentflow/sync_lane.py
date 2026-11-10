"""Bringing the target branch into the run's branch before its pull request is opened (`finish`).

A pull request goes to `project.fork_from`, the branch the run was cut from. A run can last hours, so by the
time `finish` runs that branch may hold newer commits, and some may conflict. `run_sync` makes the branch hold
the latest `origin/<fork_from>` first, and never lets the merge break the system:

1. `git pull --no-commit` starts the merge and leaves it uncommitted in the work tree: up to date (nothing to do),
   clean, or conflicted.
2. Conflicts are the `sync` session's to resolve (a human's when a path is a security path, a `human_paths` match,
   a forbidden path, or has no conflict markers to work with). The session edits files only; it never commits.
3. Nothing is committed until the merged tree has passed the `sync` session's gates (and the eval gate when
   the pack defines `eval_required` and it holds). A clean merge is checked too: a textually clean merge can
   still break behaviour. A failing gate goes back to the `sync` session as a fix attempt; none left, or no
   session, and a human gets it.
4. The runner commits the merge (`gitops.commit_sync`) and records `base_synced`, so `foreign_commits` knows
   the merge is its own and the target's commits are not.

A stop leaves the merge in progress in the work tree and pushes nothing, so a human can finish it and resume.
This module only brings the target branch into the run's branch: nothing is pushed to the target, and a human
merges the pull request.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from agentflow.config import Config, Phase, Step
from agentflow.gates import GateResult, gates_ok, record_results, run_gates
from agentflow.gitops import (
    CommitRejected,
    GhError,
    GitError,
    PullResult,
    _git_or_raise,
    commit_sync,
    conflict_markers,
    merge_in_progress,
    merge_target,
    pr_mergeable,
    pull_base,
    push,
    unmerged_paths,
)
from agentflow.guards import path_matches, snapshot
from agentflow.hitl import ResultError, raise_hitl, read_result

if TYPE_CHECKING:
    from agentflow.conditions import Ctx
    from agentflow.state import Run

Outcome = Literal["done", "skipped", "stopped"]
DEFAULT_ROUNDS = 2
MERGEABLE_TRIES = 4


class SyncLane:
    """`run_sync` and `confirm_mergeable` for `lanes.Executor`; relies on its `cfg`, `run`, `ctx`, `worktree`,
    `run_agent_step`, `decide`, `_gate_report` and `runner_commits_only`."""

    cfg: Config
    run: Run
    ctx: Ctx
    worktree: Path
    # provided by the other mixins of `lanes.Executor`; annotations only, so they never shadow the real methods
    run_agent_step: Callable[..., Outcome]
    decide: Callable[[Phase, Any, str], tuple[bool | None, str]]
    runner_commits_only: Callable[[str], bool]
    _gate_report: Callable[[list[GateResult]], str]

    # ------------------------------------------------------------------ the sync

    def run_sync(self, phase: Phase, step: Step, key: str) -> Outcome:
        """Bring `origin/<fork_from>` into the branch: "done" (up to date, or merged and committed) or "stopped"."""
        target = self.cfg.project.fork_from
        wt = self.worktree
        try:
            if merge_in_progress(wt):  # a stop, or a human, left a merge half done: pick it up where it is
                pulled = PullResult("conflicted" if unmerged_paths(wt) else "merged", merge_target(wt), unmerged_paths(wt))
            else:
                pulled = pull_base(wt, target)
        except GitError as exc:
            raise_hitl(self.cfg, self.run, "blocked", key, f"Could not bring origin/{target} into the branch: {exc}")
            return "stopped"
        if pulled.status == "up_to_date":
            return "done"
        self.run.emit("sync_started", key=key, target=target, target_sha=pulled.target_sha, conflicts=pulled.conflicts)
        notes: list[str] = []  # what the resolver said, for the pull request
        if pulled.conflicts and self._resolve_conflicts(phase, step, key, notes) == "stopped":
            return "stopped"
        gated = self._verify_merge(phase, step, key, notes)
        if gated != "done":
            return gated
        message = f"Merge origin/{target} into {self.run.state.get('branch') or 'the branch'}"
        if notes:
            message += "\n\n" + "\n".join(notes)
        try:
            sha = commit_sync(wt, message, self.cfg, pulled.target_sha)
        except CommitRejected as exc:
            raise_hitl(self.cfg, self.run, "guard_violation", key, f"Guard violation in {key}: {exc.kind}: {exc}")
            return "stopped"
        except GitError as exc:
            raise_hitl(self.cfg, self.run, "blocked", key, f"Could not commit the merge of origin/{target}: {exc}")
            return "stopped"
        self.run.emit("base_synced", key=key, sha=sha, target=pulled.target_sha, branch=target,
                      conflicts=pulled.conflicts, notes=notes, gates=self._merge_gates_line(key))
        return "done"

    # ------------------------------------------------------------------ conflicts

    def _resolve_conflicts(self, phase: Phase, step: Step, key: str, notes: list[str]) -> Outcome:
        sync = step.sync
        rounds = int(self.cfg.ci.get("sync_rounds", DEFAULT_ROUNDS))
        left_over = ""  # what the last round left unresolved, told to the next session
        for _ in range(max(1, rounds)):
            unmerged = unmerged_paths(self.worktree)
            if not unmerged:
                return "done"
            marked = conflict_markers(self.worktree, unmerged)
            manual = [p for p in unmerged if p not in marked or self._human_only(p, sync)]
            if manual or sync is None:
                shown = manual or unmerged
                why = ("the conflict is not one the runner resolves on its own (a security path, a protected path, "
                       "a file without conflict markers, or no `sync` session is configured)")
                raise_hitl(self.cfg, self.run, "blocked", key,
                           f"origin/{self.cfg.project.fork_from} conflicts with the branch in: {', '.join(shown)}.",
                           extra=f"{why}.\n\nIn the run's work tree, resolve the conflicts, `git add` each resolved "
                                 "file, and resume. Nothing was committed or pushed.")
                return "stopped"
            report = self._sync_report(marked, left_over)
            session = self._next_sync_session(key, sync)
            outcome = self.run_agent_step(self._plain(phase), sync, f"{key}/{sync.id}-{session}",
                                          extra={"sync_report": report}, role=sync.role)
            if outcome != "done":
                return "stopped"
            self._note(f"{key}/{sync.id}-{session}", notes)
            still = conflict_markers(self.worktree, marked)
            if not still:
                _git_or_raise(self.worktree, "add", "--", *marked)  # what a person does once a file is resolved
                return "done"
            left_over = f"Conflict markers are still in: {', '.join(still)}."
        raise_hitl(self.cfg, self.run, "blocked", key,
                   f"The conflicts with origin/{self.cfg.project.fork_from} were not resolved after {rounds} session(s).",
                   extra=f"Still unresolved: {', '.join(unmerged_paths(self.worktree))}. Nothing was committed or pushed.")
        return "stopped"

    def _human_only(self, path: str, sync: Step | None) -> bool:
        security = self.cfg.overlays.get("security", {}).get("when_paths", [])
        patterns = [*security, *(sync.human_paths if sync else []), *self.cfg.guardrails.forbidden_paths]
        return any(path_matches(path, pattern) for pattern in patterns)

    def _sync_report(self, paths: list[str], left_over: str) -> str:
        target = self.cfg.project.fork_from
        listing = "\n".join(f"- `{p}`" for p in paths)
        more = f"\n\n{left_over}" if left_over else ""
        return (f"Branch: `{self.run.state.get('branch')}`. Target: `origin/{target}` "
                f"(`{merge_target(self.worktree)[:12]}`).\n\nFiles with conflict markers:\n\n{listing}{more}")

    # ------------------------------------------------------------------ the merged tree must pass

    def _verify_merge(self, phase: Phase, step: Step, key: str, notes: list[str]) -> Outcome:
        """Run the gates on the merged, uncommitted tree; a failure is the `sync` session's to fix, else a human's."""
        sync = step.sync
        if sync is None or not sync.gates:
            return "done"
        if step.gates:  # the environment, before a gate needs it
            setup = run_gates(self.cfg, step.gates, self.worktree, self.run.gate_log_dir(key))
            if not gates_ok(setup):
                names = ", ".join(r.name for r in setup if not (r.passed or r.skipped))
                raise_hitl(self.cfg, self.run, "blocked", key, f"The environment for checking the merge did not come up: {names}.")
                return "stopped"
        names = sync.gates if isinstance(sync.gates, list) else [sync.gates]
        eval_needed = False  # a pack that defines no `eval_required` has no eval gate to run
        if "eval_required" in self.cfg.conditions:
            eval_needed, _ = self.decide(phase, "eval_required", f"{key}.eval_required")
            if eval_needed is None:
                return "stopped"
        results = self._run_merge_gates(key, names, with_eval=bool(eval_needed))
        if gates_ok(results):
            return "done"
        failed = [r for r in results if not (r.passed or r.skipped)]
        if not sync.fix_loop:
            raise_hitl(self.cfg, self.run, "gate_failed", key,
                       f"The merge of origin/{self.cfg.project.fork_from} fails: {', '.join(r.name for r in failed)}. "
                       "Nothing was committed or pushed.", extra=self._gate_report(failed))
            return "stopped"
        session = self._next_sync_session(key, sync)
        outcome = self.run_agent_step(self._plain(phase), sync, f"{key}/{sync.id}-{session}",
                                      extra={"sync_report": self._sync_report([], "")}, role=sync.role,
                                      fix_first=failed)
        if outcome != "done":
            return "stopped"
        self._note(f"{key}/{sync.id}-{session}", notes)
        if eval_needed:  # the session ran its own gates; the eval gate is the runner's, and a fix never skips it
            again = self._run_merge_gates(key, [], with_eval=True)
            if not gates_ok(again):
                bad = [r for r in again if not (r.passed or r.skipped)]
                raise_hitl(self.cfg, self.run, "gate_failed", key,
                           f"The merge of origin/{self.cfg.project.fork_from} fails the eval gate. "
                           "Nothing was committed or pushed.", extra=self._gate_report(bad))
                return "stopped"
        return "done"

    def _run_merge_gates(self, key: str, names: list[str], with_eval: bool) -> list[GateResult]:
        results: list[GateResult] = []
        tree = snapshot(self.worktree)
        if names:
            results += run_gates(self.cfg, names if len(names) > 1 else names[0], self.worktree,
                                 self.run.gate_log_dir(key))
        if with_eval:
            results += run_gates(self.cfg, ["eval"], self.worktree, self.run.gate_log_dir(key))
        record_results(self.run, key, results, tree)
        return results

    def _merge_gates_line(self, key: str) -> str:
        last: dict[str, dict] = {}
        for e in self.run.events.read():
            if e["type"] == "gate_result" and str(e.get("key")).startswith(key):  # the sessions' own runs count too
                last[str(e.get("gate"))] = e
        return ", ".join(f"{gate} {'– skipped' if e.get('skipped') else '✓' if e.get('passed') else '✗'}"
                         for gate, e in last.items())

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _plain(phase: Phase) -> Phase:
        return Phase(name=phase.name, agent=phase.agent, model=None, skip_if=None, steps=[])

    def _next_sync_session(self, key: str, sync: Step) -> int:
        """The next `sync` session's number; a session a stop interrupted is resumed rather than counted past."""
        prefix = f"{key}/{sync.id}-"
        rows = {k: r for k, r in self.run.state["steps"].items() if k.startswith(prefix)}
        unfinished = [k for k, r in rows.items() if r.get("status") not in ("done", "unchanged")]
        return int(unfinished[0].rsplit("-", 1)[1]) if unfinished else len(rows) + 1

    def _note(self, step_key: str, notes: list[str]) -> None:
        try:
            summary = read_result(self.run.result_path(step_key)).summary.strip()
        except (ResultError, OSError):
            return
        if summary:
            notes.append(summary)

    # ------------------------------------------------------------------ after the pull request exists

    def confirm_mergeable(self, phase: Phase, step: Step, key: str, url: str) -> Outcome:
        """GitHub's verdict on the new pull request. Main may have moved since the fetch: on `CONFLICTING`, bring the
        target in again and push, up to `ci.sync_rounds` times, then a human. "UNKNOWN" and a failed read are not a
        verdict: the `ci` step is the backstop."""
        rounds = int(self.cfg.ci.get("sync_rounds", DEFAULT_ROUNDS))
        branch = str(self.run.state.get("branch") or "")
        for attempt in range(rounds + 1):
            if self._mergeable(url) != "CONFLICTING":
                return "done"
            if attempt == rounds:
                break
            if self.run_sync(phase, step, key) != "done":
                return "stopped"
            if not self.runner_commits_only(key):
                return "stopped"
            try:
                push(self.worktree, branch)
            except GitError as exc:
                raise_hitl(self.cfg, self.run, "blocked", key, f"Could not push {branch} after syncing it: {exc}")
                return "stopped"
        raise_hitl(self.cfg, self.run, "blocked", key,
                   f"{url} still conflicts with origin/{self.cfg.project.fork_from} after {rounds} sync(s).")
        return "stopped"

    def _mergeable(self, url: str) -> str:
        pause = min(float(self.cfg.ci.get("poll_s", 0) or 0), 3.0)
        verdict = "UNKNOWN"
        for _ in range(MERGEABLE_TRIES):
            try:
                verdict = pr_mergeable(self.worktree, url)
            except GhError:
                return "UNKNOWN"
            if verdict != "UNKNOWN":
                return verdict
            time.sleep(pause)
        return verdict

