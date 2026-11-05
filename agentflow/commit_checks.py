"""What the runner knows about its own commits, so it commits and pushes only on top of them.

`runner_head` is the last `HEAD` the runner recorded for a run: the worktree's base, a step's start (taken
after the guards checked the step before it), a rebase it made, or a commit it made. `gitops.commit` gets
it as `expected_head`, so code a gate or a test command ran cannot slip a commit under the runner's.
`foreign_commits` lists the commits between the base and `HEAD` the runner did not make, or whose message
carries a rejected trailer; `finish` pushes only when there are none. The target branch brought in by
`finish` (a `base_synced` event: the merge commit is the runner's, `target` is the tip it brought in) is not
the runner's to vouch for: commits already on it are left out.
"""

from __future__ import annotations

from pathlib import Path

from agentflow.config import Config
from agentflow.gitops import GitError, _git_or_raise, commits_between
from agentflow.guards import check_message
from agentflow.state import Run

RUNNER_COMMITS = ("step_committed", "task_committed", "decision_recorded", "module_added", "base_synced")
HEAD_FIELDS = {"run_updated": "base_sha", "step_started": "start_head", "step_rebased": "start_head",
               **{etype: "sha" for etype in RUNNER_COMMITS}}

__all__ = ["foreign_commits", "runner_commits", "runner_head"]


def runner_head(run: Run) -> str | None:
    """The last `HEAD` the runner recorded for `run`, or None when it recorded none."""
    found = None
    for event in run.events.read():
        field = HEAD_FIELDS.get(event["type"])
        if field and event.get(field):
            found = str(event[field])
    return found


def runner_commits(run: Run) -> set[str]:
    return {str(e["sha"]) for e in run.events.read() if e["type"] in RUNNER_COMMITS and e.get("sha")}


def foreign_commits(run: Run, cfg: Config, worktree: Path, base: str) -> list[str]:
    """One line per commit in `base..HEAD` the runner did not record, or whose message carries a rejected
    trailer. Raises `GitError` when git cannot list them."""
    events = run.events.read()
    ours = {str(e["sha"]) for e in events if e["type"] in RUNNER_COMMITS and e.get("sha")}
    targets = [str(e["target"]) for e in events if e["type"] == "base_synced" and e.get("target")]
    lines = []
    for sha in commits_between(Path(worktree), base, exclude=targets):
        why = []
        if sha not in ours:
            why.append("not made by the runner")
        try:
            message = _git_or_raise(Path(worktree), "log", "-1", "--format=%B", sha)
        except GitError as exc:
            why.append(f"its message cannot be read: {exc}")
            message = ""
        rejected = check_message(cfg, message)
        if rejected:
            why.append("rejected trailer " + ", ".join(v.detail for v in rejected))
        if why:
            lines.append(f"{sha[:12]}: {'; '.join(why)}")
    return lines
