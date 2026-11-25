"""The runner's own writes to the backlog file: one commit per change, on top of the runner's own commits.

`write` reads the file at the run's `runner_head` (never the work tree), applies a pure text edit
(`backlog_file.set_fields_text`, `append_item_text`), and commits exactly that file through `gitops.commit_backlog`.
The work tree's copy must be the committed one before anything is written, so a change some other process made to it
is never committed and never lost: the write is refused instead. Each commit is recorded as `backlog_updated`, a runner
commit (`commit_checks.RUNNER_COMMITS`), and `state["backlog_sha"]` keeps the newest one's sha.

`applies` says whether a run writes at all: a `backlog` pack, and a run with a `module` param in the item's lane.

`plan_start` decides, before a run exists, what the run's first commit writes: a new item (`--new-module`, or an item
only the main checkout's work tree has), its `ready` status, or nothing; it refuses a start the file says no to.
`first_commit` makes that commit once the run's worktree exists.
"""

from __future__ import annotations

import dataclasses
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from agentflow import backlog_file, gitops, module_queue, pack, safeio, work_source
from agentflow.backlog_file import BacklogItem
from agentflow.commit_checks import runner_head
from agentflow.config import Config, backlog_path
from agentflow.gitops import CommitRejected
from agentflow.intake import issue_number
from agentflow.state import Run

CHANGES = ("added", "ready", "spec", "plan", "done")
_ATTR = {"depends_on": "deps"}  # a backlog key -> its `BacklogItem` attribute, where they differ

__all__ = ["CHANGES", "StartWrite", "applies", "first_commit", "plan_start", "write"]


@dataclass(frozen=True)
class StartWrite:
    """What a backlog run's first commit writes: `item` appended (`added`; `local` when it came from the main
    checkout's work tree rather than `--new-module`), or its status set to `ready` (`ready`)."""

    change: Literal["added", "ready"]
    item: BacklogItem
    local: bool


def applies(cfg: Config, run: Run, item: BacklogItem | None) -> bool:
    """Whether `run` writes to the backlog file: a `backlog` pack, and a run with a `module` param whose lane is the
    item's lane, or `module_queue.module_lane` when the item names none (or is None)."""
    if backlog_path(cfg.work_source) is None or not run.state["params"].get("module"):
        return False
    lane = (item.lane if item is not None else "") or module_queue.module_lane(cfg)
    return bool(lane) and run.state["pipeline"] == lane


def _fields(before: BacklogItem | None, after: BacklogItem) -> dict[str, object]:
    """The keys of `after` that differ from `before` (every key that is set, for a new item), with their new values."""
    out: dict[str, object] = {}
    for key in backlog_file.KEYS[1:]:
        value = getattr(after, _ATTR.get(key, key))
        if before is None:
            if value not in ("", [], None):
                out[key] = value
        elif value != getattr(before, _ATTR.get(key, key)):
            out[key] = value
    return out


def _put_back(worktree: Path, path: str, head: str, written: str, committed: bytes | None) -> None:
    """After a refused commit, put the committed file back where the runner wrote: only when no commit was made and the
    file still holds exactly what the runner wrote, so a change another process made is never overwritten."""
    target = worktree / path
    try:
        if gitops.head(worktree) != head:
            return  # a commit was made and left for a human: the file is what it records
        if safeio.read_bytes(target, backlog_file.FILE_MAX + 1) != written.encode("utf-8"):
            return
        if committed is None:
            target.unlink()
        else:
            safeio.write_text(target, committed.decode("utf-8"))
    except (OSError, ValueError, subprocess.CalledProcessError):
        return  # the refusal the caller gets says what went wrong; the file is left for a human


def write(cfg: Config, run: Run, worktree: Path, change: str, edit: Callable[[str], str], message: str,
          local: bool = False) -> str | None:
    """Apply `edit` to the backlog file as committed at `runner_head(run)` and commit the result; return its sha, or
    None when `edit` changes nothing.

    `HEAD` must be `runner_head(run)` (`agent_commit`), and the work tree's file must hold the bytes committed there, or
    be absent where the commit has none (`outside_output`), else `CommitRejected` is raised with nothing written. The
    item is the run's `module` param, and `edit` may change only that item. `edit`'s `backlog_file.EditRefused`, an edit
    that changes another item, a file that is not UTF-8, and `commit_backlog`'s `CommitRejected` all reach the caller;
    after a refused commit the file is put back as committed, unless something other than the runner changed it since.
    A commit is recorded as `backlog_updated`: `item`, `change`, `fields` (the keys set and their values), `sha`,
    `path`, `before_tree` and `tree` (the trees of the commit before and of this one), and `local` for `change:
    "added"`."""
    if change not in CHANGES:
        raise ValueError(f"unknown backlog change {change!r}: one of {', '.join(CHANGES)}")
    path = backlog_path(cfg.work_source)
    item_id = str(run.state["params"].get("module") or "")
    if path is None or not item_id:
        raise ValueError("only a module run in a backlog pack writes to the backlog file")
    worktree = Path(worktree)
    head = runner_head(run)
    if head is None:
        raise CommitRejected("the runner recorded no HEAD for this run, so it has nothing to commit on top of",
                             kind="agent_commit")
    now = gitops.head(worktree)
    if now != head:
        raise CommitRejected(f"HEAD moved from {head[:12]} to {now[:12]} since the runner last recorded it; the runner "
                             "commits only on top of its own commits", kind="agent_commit")
    committed = gitops.read_blob(worktree, head, path, backlog_file.FILE_MAX)
    try:
        on_disk = safeio.read_bytes(worktree / path, backlog_file.FILE_MAX + 1)
    except (OSError, ValueError) as exc:  # `SymlinkRefused` is an `OSError`; not a regular file, a `ValueError`
        raise CommitRejected(f"the backlog commit was refused: {gitops.shown(path)} cannot be read as a regular "
                             f"file: {exc}", kind="outside_output") from exc
    if on_disk != committed:
        raise CommitRejected(f"the backlog commit was refused: {gitops.shown(path)} in the work tree is not the file "
                             f"committed at {head[:12]}, and the runner never commits a change it did not make",
                             kind="outside_output")
    try:
        text = (committed or b"").decode("utf-8")
    except UnicodeDecodeError:
        raise backlog_file.EditRefused(f"{path} is not valid UTF-8") from None
    new = edit(text)
    if new == text:
        return None
    if len(new.encode("utf-8")) > backlog_file.FILE_MAX:
        raise backlog_file.EditRefused(f"the edit would make {path} larger than {backlog_file.FILE_MAX} bytes")
    before_items, _ = backlog_file.parse(text)
    after_items, _ = backlog_file.parse(new)
    others_before = [i for i in before_items if i.id != item_id]
    others_after = [i for i in after_items if i.id != item_id]
    after = next((i for i in after_items if i.id == item_id), None)
    if after is None or others_after != others_before:
        raise backlog_file.EditRefused(f"the {change} edit must change item {item_id} of {path} and no other item")
    before = next((i for i in before_items if i.id == item_id), None)

    before_tree = gitops.head_tree(worktree)
    safeio.write_text(worktree / path, new)
    try:
        sha = gitops.commit_backlog(worktree, message, cfg, path, new, head)
    except (CommitRejected, gitops.NothingToCommit):
        _put_back(worktree, path, head, new, committed)
        raise
    data: dict[str, object] = {
        "item": item_id, "change": change, "fields": _fields(before, after), "sha": sha, "path": path,
        "before_tree": before_tree, "tree": gitops.head_tree(worktree),
    }
    if change == "added":
        data["local"] = bool(local)
    run.emit("backlog_updated", **data)
    return sha


def _one_line(text: str, limit: int = backlog_file.TITLE_MAX) -> str:
    """`text` as one line of at most `limit` characters, cut with `…`."""
    line = " ".join(text.split())
    return line if len(line) <= limit else line[: limit - 1] + "…"


def _append(text: str, item: BacklogItem) -> str:
    """`append_item_text`, or a new file holding `item` alone when the commit has no backlog file."""
    return backlog_file.append_item_text(text, item) if text.strip() else backlog_file.render([item])


def _ready(item_id: str) -> Callable[[str], str]:
    return lambda text: backlog_file.set_fields_text(text, item_id, {"status": "ready"})


def plan_start(cfg: Config, repo: Path, pipeline: str, params: dict, new_module: dict | None) -> StartWrite | None:
    """What the first commit of a run of `pipeline` with `params` writes to the backlog file, or None when it writes
    nothing. Called before the run exists, so a `ValueError` refuses the start with nothing created.

    Only a `backlog` pack and a run with a `module` param are concerned. The item is looked up in the file at the tip
    of `project.fork_from` (the commit the run forks from, read through `gitops.read_blob`) and then in the main
    checkout's work tree (read only, never written). A `--new-module` is appended as `ready` (with `lane: <pipeline>`
    when `pipeline` is not `module_lane`); an item only the work
    tree has is appended as it is there (`ready` when it said `backlog`); an item the fork has as `backlog` is set to
    `ready`; `done` and `dropped` items are refused, and so is a module-lane run of an item neither has. An item in
    another lane than `pipeline` is left alone. An appended item's `depends_on` must all be in the fork's file, and
    the edit is tried on the fork's text here, so one the file would refuse stops the start too."""
    path = backlog_path(cfg.work_source)
    item_id = str(params.get("module") or "")
    if path is None or not item_id:
        return None
    repo = Path(repo)
    fork = cfg.project.fork_from
    sha = gitops.commit_at(repo, fork)
    data = gitops.read_blob(repo, sha, path, backlog_file.FILE_MAX) if sha else None
    try:
        text: str | None = (data or b"").decode("utf-8")
    except UnicodeDecodeError:
        text = None
    at_fork = {i.id: i for i in backlog_file.parse(text)[0]} if text else {}
    local = work_source.item(cfg, pack.checkout_root(repo), item_id)
    module_lane = module_queue.module_lane(cfg) or ""

    def edited(edit: Callable[[str], str]) -> None:
        if text is None:
            raise ValueError(f"{path} on {fork} is not valid UTF-8: fix it before a run writes to it")
        edit(text)  # `EditRefused` is a `ValueError`: the start is refused with the file's own reason

    def added(item: BacklogItem, is_local: bool) -> StartWrite:
        for dep in item.deps:
            if dep not in at_fork:
                raise ValueError(f"{item.id} depends on {dep}, which is not in {path} on {fork}: commit it first")
        edited(lambda t: _append(t, item))
        return StartWrite("added", item, is_local)

    def open_(item: BacklogItem) -> None:
        if item.status in ("done", "dropped"):
            raise ValueError(f"module {item.id} is {item.status} in {path}: set it to ready first")

    if new_module is not None:
        if item_id in at_fork or local is not None:
            raise ValueError(f"{item_id} is already an item in {path}: start module:{item_id}")
        issue = params.get("issue")
        # outside the module lane the item names the run's lane, so the run's own later writes (`applies`) reach it
        item = BacklogItem(item_id, [str(d) for d in new_module.get("deps") or []],
                           title=_one_line(str(new_module.get("responsibility") or "")),
                           lane=pipeline if pipeline != module_lane else "", status="ready",
                           issue=int(issue_number(issue)) if issue not in (None, "") else None)
        return added(item, False)
    found = at_fork.get(item_id)
    if found is not None:
        if (found.lane or module_lane) != pipeline:
            return None
        open_(found)
        if found.status != "backlog":
            return None
        edited(_ready(item_id))
        return StartWrite("ready", found, False)
    if local is not None:
        if (local.lane or module_lane) != pipeline:
            return None
        open_(local)
        return added(dataclasses.replace(local, status="ready") if local.status == "backlog" else local, True)
    if module_lane and pipeline == module_lane:
        raise ValueError(f"module {item_id} is not in {path}: add it (agentflow backlog add {item_id} --title …) or "
                         "start it from the dashboard")
    return None


def first_commit(cfg: Config, run: Run, worktree: Path, plan: StartWrite) -> str:
    """Make the run's first commit as `plan` says, on top of its fork commit; return its sha ('' when the edit changed
    nothing, which only a `fork_from` that moved since `plan_start` can bring about). `write`'s refusals reach the
    caller."""
    item = plan.item
    if plan.change == "added":
        sha = write(cfg, run, worktree, "added", lambda text: _append(text, item), f"Add {item.id} to the backlog",
                    local=plan.local)
    else:
        sha = write(cfg, run, worktree, "ready", _ready(item.id), f"Mark {item.id} ready in the backlog")
    return sha or ""
