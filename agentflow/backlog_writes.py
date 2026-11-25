"""The runner's own writes to the backlog file: one commit per change, on top of the runner's own commits.

`write` reads the file at the run's `runner_head` (never the work tree), applies a pure text edit
(`backlog_file.set_fields_text`, `append_item_text`), and commits exactly that file through `gitops.commit_backlog`.
The work tree's copy must be the committed one before anything is written, so a change some other process made to it
is never committed and never lost: the write is refused instead. Each commit is recorded as `backlog_updated`, a runner
commit (`commit_checks.RUNNER_COMMITS`), and `state["backlog_sha"]` keeps the newest one's sha.

`applies` says whether a run writes at all: a `backlog` pack, and a run with a `module` param in the item's lane.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path

from agentflow import backlog_file, gitops, module_queue, safeio
from agentflow.backlog_file import BacklogItem
from agentflow.commit_checks import runner_head
from agentflow.config import Config, backlog_path
from agentflow.gitops import CommitRejected
from agentflow.state import Run

CHANGES = ("added", "ready", "spec", "plan", "done")
_ATTR = {"depends_on": "deps"}  # a backlog key -> its `BacklogItem` attribute, where they differ

__all__ = ["CHANGES", "applies", "write"]


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
