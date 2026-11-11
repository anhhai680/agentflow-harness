"""What a run does on the GitHub issue it started from: mark it when the run starts, link its pull request into the
issue's Development section once the PR is open, and release it when the run ends (the issue status proposal is
the module's spec).

A GitHub problem never fails, stops or blocks a run, the rule `hitl.notify` follows: a `GhError` or `OSError` is
recorded as an `issue_status_failed` event and the run goes on. Every `gh` call goes through `gitops._gh` (an argv
list, no shell). The comment bodies are built from the run id, lane, branch, label and PR URL only, never from
issue text. A node id from gh's JSON goes into an argv only after it matches `NODE_ID`, as its own `-f` element; the
GraphQL text is the constant `LINK_MUTATION` and nothing is spliced into it.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING

from agentflow import safeio
from agentflow.gitops import GhError, _gh
from agentflow.state import TERMINAL

if TYPE_CHECKING:
    from agentflow.config import Config
    from agentflow.state import Run

COMMENT_FILE = "issue-comment.md"
LINK_FILE = "issue-link.md"
LABEL_DESCRIPTION = "agentflow is working on this issue"
LINK_MUTATION = ("mutation($issue: ID!, $pr: ID!) { addCloseIssueReferences(input: {issueId: $issue, "
                 "pullRequestIds: [$pr]}) { clientMutationId } }")
NODE_ID = re.compile(r"^[A-Za-z0-9_=-]{1,100}$")
NOT_LISTED = "GitHub does not list the link"


def _gh_out(repo: Path, args: list[str]) -> str:
    """`gh <args>`'s stdout; a non-zero exit is a `GhError` carrying gh's own message."""
    proc = _gh(args, cwd=repo)
    if proc.returncode != 0:
        raise GhError(proc.stderr.strip() or f"gh exited {proc.returncode}")
    return proc.stdout


def _edit(repo: Path, number: str, *flags: str) -> None:
    """`gh issue edit <number> <flags>`."""
    _gh_out(repo, ["issue", "edit", number, *flags])


def _failed(run: Run, action: str, exc: BaseException) -> None:
    run.emit("issue_status_failed", action=action, error=(str(exc) or type(exc).__name__)[:300])  # a retry per CI round


def _add_label(run: Run, number: str, label: str) -> bool:
    """Add `label` to the issue, creating it first when GitHub does not have it. False after a failure was recorded."""
    try:
        _edit(run.repo, number, "--add-label", label)
        return True
    except (GhError, OSError) as exc:
        if "not found" not in str(exc):
            _failed(run, "mark", exc)
            return False
    try:
        _gh_out(run.repo, ["label", "create", label, "--description", LABEL_DESCRIPTION])
    except (GhError, OSError) as exc:
        _failed(run, "label_create", exc)
        return False
    try:
        _edit(run.repo, number, "--add-label", label)
    except (GhError, OSError) as exc:
        _failed(run, "mark", exc)
        return False
    return True


def mark(cfg: Config, run: Run) -> None:
    """Label the run's issue and say on it that the run has begun. Nothing happens without an issue or a label."""
    number = (run.state.get("params") or {}).get("issue")
    label = cfg.issues.in_progress_label
    if not number or not label:
        return
    number = str(number)
    if not _add_label(run, number, label):
        return
    commented = False
    if cfg.issues.comment:
        body = (f"agentflow run `{run.id}` ({run.state.get('pipeline')}) is working on this issue, "
                f"on branch `{run.state.get('branch')}`.\n\n"
                f"The `{label}` label comes off when the run ends.\n")
        try:
            path = run.dir / COMMENT_FILE
            safeio.write_text(path, body)
            _gh_out(run.repo, ["issue", "comment", number, "--body-file", str(path)])
            commented = True
        except (GhError, OSError, ValueError) as exc:
            _failed(run, "comment", exc)
    run.emit("issue_marked", issue=number, label=label, commented=commented)


def release(run: Run) -> None:
    """Remove the label `mark` added, once the run has ended. A GitHub failure is logged, never raised."""
    run.refresh()
    if run.state["status"] not in TERMINAL:
        return
    events = run.events.read()
    marked = next((e for e in events if e["type"] == "issue_marked"), None)
    if marked is None or any(e["type"] == "issue_released" for e in events):
        return
    number, label = str(marked["issue"]), str(marked["label"])
    try:
        _edit(run.repo, number, "--remove-label", label)
    except (GhError, OSError) as exc:
        _failed(run, "release", exc)
        return
    run.emit("issue_released", issue=number, label=label)


# --------------------------------------------------------------------------- link


def _json(repo: Path, args: list[str]) -> dict:
    """`gh <args>`'s JSON object; anything else is a `ValueError`."""
    reply = json.loads(_gh_out(repo, args))
    if not isinstance(reply, dict):
        raise ValueError(f"gh {' '.join(args[:2])} printed no JSON object")
    return reply


def _node_id(reply: dict, what: str) -> str:
    """The reply's `id`, once it is a plain node id; otherwise a `ValueError`, before it can reach an argv."""
    node = reply.get("id")
    if not isinstance(node, str) or not NODE_ID.fullmatch(node):
        raise ValueError(f"the {what} reply has no valid node id")
    return node


def _closes(reply: dict, number: str) -> bool:
    """True when the PR's `closingIssuesReferences` lists issue `number`."""
    refs = reply.get("closingIssuesReferences")
    return isinstance(refs, list) and any(isinstance(ref, dict) and str(ref.get("number")) == number for ref in refs)


def _api_link(repo: Path, number: str, url: str) -> str:
    """Link the PR to the issue when its body keyword did not: `keyword` or `api`, or a raise when it can't."""
    pr = _json(repo, ["pr", "view", url, "--json", "id,closingIssuesReferences"])
    if _closes(pr, number):
        return "keyword"
    pr_id = _node_id(pr, "pull request")
    issue_id = _node_id(_json(repo, ["issue", "view", number, "--json", "id"]), "issue")
    _json(repo, ["api", "graphql", "-f", f"query={LINK_MUTATION}", "-f", f"issue={issue_id}", "-f", f"pr={pr_id}"])
    if not _closes(_json(repo, ["pr", "view", url, "--json", "closingIssuesReferences"]), number):
        raise ValueError(NOT_LISTED)
    return "api"


def _comment_link(run: Run, number: str, url: str) -> str:
    """An epic gets a comment with the PR's URL instead of a link, which would close it on merge."""
    body = (f"agentflow run `{run.id}` opened {url} for this issue.\n\n"
            "This issue has an epic label, so the pull request is not linked as closing it: "
            "merging it won't close this issue.\n")
    path = run.dir / LINK_FILE
    safeio.write_text(path, body)
    _gh_out(run.repo, ["issue", "comment", number, "--body-file", str(path)])
    return "comment"


def link(run: Run) -> None:
    """Put the run's PR in its issue's Development section, or comment on an epic. A GitHub failure is logged,
    never raised, and no config key turns it off."""
    run.refresh()
    params = run.state.get("params") or {}
    if not params.get("issue"):
        return
    events = run.events.read()
    opened = [e for e in events if e["type"] == "pr_opened"]
    if not opened or any(e["type"] == "issue_linked" for e in events):
        return
    number, url = str(params["issue"]), str(opened[-1]["url"])
    try:
        how = _comment_link(run, number, url) if params.get("issue_epic") else _api_link(run.repo, number, url)
    except (GhError, OSError, ValueError, RecursionError) as exc:  # RecursionError: absurdly nested JSON
        _failed(run, "link", exc)
        return
    run.emit("issue_linked", issue=number, pr=url, how=how)
