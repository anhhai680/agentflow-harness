"""A person's override for a constraint gate: the runner writes the commit that carries the human-only trailer.

A pack's `guardrails.constraint_override` names a trailer (one `guardrails.reject_trailers` lists, so no agent and no
other commit may carry it) and the gates a person may lift with it. `agentflow approve <run> <key> --constraint`
records `hitl_approved{constraint: True}` (`hitl.approve` refuses the runner, an agent session, and a key no
constraint gate failed on, before any event). Between steps, `write` turns each such approval into one commit on
`HEAD`, holding `HEAD`'s own tree, whose message ends with `<trailer>: <person>` (`gitops.commit_constraint`), and
records `constraint_approved{key, by, sha, gates}`. That event moves `runner_head`, so the next runner commit goes on
top of it, and `foreign_commits` accepts the trailer on that sha alone. The step the gate stopped then runs again and
its gates see the override.

`write` runs in `pipeline.walk` before each `run_steps`, and at the top of each plan walk (`_plan_pass`), so the
commit lands before any task runs: a parked task's work is still on its shelf then, and is applied back on top.
"""

from __future__ import annotations

from pathlib import Path

from agentflow.autonomy import AUTO_BY
from agentflow.commit_checks import runner_head
from agentflow.config import TRAILER_NAME, Config
from agentflow.gitops import GitError, _git_or_raise, commit_constraint
from agentflow.guards import HARDENED, check_message
from agentflow.hitl import clean_fact, constraint_gates_failed, raise_hitl
from agentflow.state import Run

NAME_MAX = 100  # characters of the person's name the trailer carries

__all__ = ["message", "pending", "write"]


def pending(events: list[dict]) -> list[dict]:
    """The `hitl_approved` events with `constraint: True` that no `constraint_approved` for the same key follows."""
    found = []
    for index, event in enumerate(events):
        if event.get("type") != "hitl_approved" or event.get("constraint") is not True:
            continue
        key = event.get("key")
        if not any(e.get("type") == "constraint_approved" and e.get("key") == key for e in events[index + 1:]):
            found.append(event)
    return found


def message(run_id: str, key: str, by: str, gates: list[str], trailer: str) -> str:
    """The override commit's message: a subject, one line of who approved what, and the trailer with the name."""
    shown = ", ".join(gates)
    return (f"Approve the {shown} constraint for {key}\n\n"
            f"{by} approved what `{shown}` refused in `{key}` of run {run_id}.\n\n"
            f"{trailer}: {by}\n")


def write(cfg: Config, run: Run, worktree: Path) -> bool:
    """Write one override commit for each pending constraint approval, then record `constraint_approved` for it. True
    once none is left, or while the run waits on a person (nothing is written then). False when one cannot be written:
    a `blocked` stop on its key says why, and approving that stop (once the cause is fixed) tries it again."""
    run.refresh()
    if run.state["waiting"] is not None:
        return True
    while True:
        todo = pending(run.events.read())
        if not todo:
            return True
        approval = todo[0]
        key = str(approval.get("key") or "")
        try:
            sha, by, gates = _commit(cfg, run, Path(worktree), approval, key)
        except GitError as exc:
            raise_hitl(cfg, run, "blocked", key,
                       f"The runner could not write the constraint override a person approved for {key}: "
                       f"{clean_fact(str(exc))}. Put HEAD back where the runner left it (or fix the pack's "
                       f"guardrails.constraint_override), then `agentflow approve {run.id} {key}` writes it again.",
                       notify=True)
            return False
        run.emit("constraint_approved", by=by, key=key, sha=sha, gates=gates)


def _commit(cfg: Config, run: Run, worktree: Path, approval: dict, key: str) -> tuple[str, str, list[str]]:
    """`(sha, name, gates)` of the override commit for `approval`; `GitError` when it cannot be written. A commit a
    runner killed before its `constraint_approved` already made (one parent, the runner's `HEAD`; the same tree; this
    exact message) is taken as written instead of being written twice."""
    trailer = cfg.guardrails.constraint_trailer
    if not trailer or not TRAILER_NAME.fullmatch(trailer) or trailer not in cfg.guardrails.reject_trailers:
        raise GitError("the pack sets no valid guardrails.constraint_override.trailer (one that "
                       "guardrails.reject_trailers lists)")
    by = clean_fact(str(approval.get("by") or ""))[:NAME_MAX]
    if not by or by.casefold() == AUTO_BY:  # `hitl.approve` refuses both; a log that holds one is not a person's word
        raise GitError("the approval names no person")
    gates = constraint_gates_failed(cfg, run, key)
    if not gates:
        raise GitError(f"no constraint gate failed on {key} any more")
    text = message(run.id, key, by, gates, trailer)
    if [v for v in check_message(cfg, text) if v.detail != trailer]:
        raise GitError("the override message would carry another rejected trailer")
    expected = runner_head(run)
    if not expected:
        raise GitError("the runner has recorded no HEAD for this run")
    adopted = _already_written(worktree, expected, text)
    if adopted:
        return adopted, by, gates
    return commit_constraint(worktree, text, expected), by, gates


def _already_written(worktree: Path, expected: str, text: str) -> str | None:
    """`HEAD`'s sha when it is the override commit `text` on top of `expected` with `expected`'s tree, else None."""
    try:
        now = _git_or_raise(worktree, *HARDENED, "rev-parse", "--verify", "HEAD^{commit}").strip()
        if now == expected:
            return None
        parents = _git_or_raise(worktree, *HARDENED, "rev-list", "--parents", "-n", "1", now).split()[1:]
        trees = [_git_or_raise(worktree, *HARDENED, "rev-parse", "--verify", f"{sha}^{{tree}}").strip()
                 for sha in (now, expected)]
        body = _git_or_raise(worktree, *HARDENED, "log", "-1", "--format=%B", now)
    except GitError:
        return None
    if parents == [expected] and trees[0] == trees[1] and body.strip() == text.strip():
        return now
    return None
