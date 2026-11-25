"""The runner's own writes to the backlog file: one commit per change, on top of the runner's own commits.

`write` reads the file at the run's `runner_head` (never the work tree), applies a pure text edit
(`backlog_file.set_fields_text`, `append_item_text`), and commits exactly that file through `gitops.commit_backlog`.
The work tree's copy must be the committed one before anything is written, so a change some other process made to it
is never committed and never lost: the write is refused instead. Each commit is recorded as `backlog_updated`, a runner
commit (`commit_checks.RUNNER_COMMITS`), and `state["backlog_sha"]` keeps the newest one's sha.

`applies` says whether a run writes at all: a `backlog` pack, and a run with a `module` param in the item's lane.

`plan_start` decides, before a run exists, what the run's first commit writes: a new item (`--new-module`, or an item
only the main checkout's work tree has), its `ready` status, or nothing; it refuses a start the file says no to.
`first_commit` makes that commit once the run's worktree exists. `after_output` records the spec and plan a step
wrote, and `mark_done` sets `status: done` in `finish`, after the base sync and before the audit and the push.

`bring_in` settles the backlog file of the base sync's merge itself: the target's file with the run's own recorded
changes made on it again (`replay`), staged as the runner's, so `gitops.commit_sync` lets exactly that blob through.
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
from agentflow.guards import HARDENED
from agentflow.intake import issue_number
from agentflow.state import Run

CHANGES = ("added", "ready", "spec", "plan", "done")
_ATTR = {"depends_on": "deps"}  # a backlog key -> its `BacklogItem` attribute, where they differ

__all__ = ["CHANGES", "StartWrite", "after_output", "applies", "bring_in", "first_commit", "mark_done", "plan_start",
           "replay", "write"]


@dataclass(frozen=True)
class StartWrite:
    """What a backlog run's first commit writes: `item` appended (`added`; `local` when it came from the main
    checkout's work tree rather than `--new-module`), or its status set to `ready` (`ready`). `source` is the text of
    the main checkout's file a local item came from, whose block the commit copies as it is written there."""

    change: Literal["added", "ready"]
    item: BacklogItem
    local: bool
    source: str = ""


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
    """After a refused or failed commit, put the index and the committed file back where the runner wrote: only when no
    commit was made, and the file only when it still holds exactly what the runner wrote, so a change another process
    made is never overwritten."""
    target = worktree / path
    try:
        if gitops.head(worktree) != head:
            return  # a commit was made and left for a human: the file is what it records
        subprocess.run(["git", *HARDENED, "--literal-pathspecs", "reset", "-q", "--", path], cwd=worktree,
                       capture_output=True, check=False)  # the index back at `HEAD` for the file, as a refusal leaves it
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
    that changes another item, a file that is not UTF-8, and `commit_backlog`'s `CommitRejected` and `GitError` all reach
    the caller; after a refused or failed commit the index and the file are put back as committed, the file unless
    something other than the runner changed it since.
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
    except gitops.GitError:  # a refusal (`CommitRejected`, `NothingToCommit`), or git failed (a signing program, say)
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


def _copied(item: BacklogItem, source: str) -> Callable[[str], str]:
    """The edit that appends `item`'s block as `source` writes it (`backlog_file.append_span_text`: its comments and
    unknown keys kept), with `item`'s status set on it when that differs (`ready` for a `backlog` item); `item` rendered
    (`_append`) when `source` is empty, the block cannot be copied cleanly, or it no longer reads as `item`."""

    def edit(text: str) -> str:
        if not source:
            return _append(text, item)
        try:
            new = backlog_file.append_span_text(text if text.strip() else backlog_file.render([]), source, item.id)
        except backlog_file.EditRefused:
            return _append(text, item)
        copied = next(i for i in backlog_file.parse(new)[0] if i.id == item.id)
        if dataclasses.replace(copied, status=item.status) != item:  # the file changed since `item` was read from it
            return _append(text, item)
        return new if copied.status == item.status else backlog_file.set_fields_text(new, item.id,
                                                                                     {"status": item.status})

    return edit


def _checkout_text(cfg: Config, root: Path) -> str:
    """The text of the backlog file in the main checkout's work tree (read only, no symlink followed), or ''."""
    target = work_source.file(cfg, root)
    try:
        data = safeio.read_bytes(target, backlog_file.FILE_MAX) if target is not None else None
        return data.decode("utf-8") if data is not None else ""
    except (OSError, ValueError):  # unreadable, a symlink, too large, or not UTF-8 (`UnicodeDecodeError`)
        return ""


def plan_start(cfg: Config, repo: Path, pipeline: str, params: dict, new_module: dict | None) -> StartWrite | None:
    """What the first commit of a run of `pipeline` with `params` writes to the backlog file, or None when it writes
    nothing. Called before the run exists, so a `ValueError` refuses the start with nothing created.

    Only a `backlog` pack and a run with a `module` param are concerned. The item is looked up in the file at the tip
    of `project.fork_from` (the commit the run forks from, read through `gitops.read_blob`) and then in the main
    checkout's work tree (read only, never written). A `--new-module` is appended as `ready` (with `lane: <pipeline>`
    when `pipeline` is not `module_lane`); an item only the work
    tree has is appended with its block as written there (`ready` when it said `backlog`); an item the fork has as
    `backlog` is set to `ready`; `done` and `dropped` items are refused (`commit the change first` when the work tree has
    already reopened it), and so is a module-lane run of an item neither has. An item in
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
        source = _checkout_text(cfg, pack.checkout_root(repo)) if is_local else ""
        edited(_copied(item, source))
        return StartWrite("added", item, is_local, source)

    def open_(item: BacklogItem) -> None:
        if item.status not in ("done", "dropped"):
            return
        if item is found and local is not None and local.status in backlog_file.OPEN:  # reopened, not yet committed
            raise ValueError(f"module {item.id} is {item.status} on {fork}: commit the change first")
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
        sha = write(cfg, run, worktree, "added", _copied(item, plan.source), f"Add {item.id} to the backlog",
                    local=plan.local)
    else:
        sha = write(cfg, run, worktree, "ready", _ready(item.id), f"Mark {item.id} ready in the backlog")
    return sha or ""


def _item_at(worktree: Path, sha: str | None, path: str, item_id: str) -> BacklogItem | None:
    """Item `item_id` of the backlog file as committed at `sha`, or None (no sha, no file, no such item)."""
    blob = gitops.read_blob(worktree, sha, path, backlog_file.FILE_MAX) if sha else None
    if blob is None:
        return None
    try:
        items, _ = backlog_file.parse(blob.decode("utf-8"))
    except UnicodeDecodeError:
        raise backlog_file.EditRefused(f"{path} is not valid UTF-8") from None
    return next((i for i in items if i.id == item_id), None)


def after_output(cfg: Config, run: Run, worktree: Path, values: dict[str, str], output: str) -> str | None:
    """Record in the run's item the path a committed step just wrote: `spec` when `output` is `values["spec"]`, `plan`
    when it is `values["plan_target"]`; return the commit's sha, or None when nothing is written. The item is read from
    the file at `runner_head(run)`; nothing is written when the run does not write to the backlog (`applies`), the item
    is not in the file, or it already names `output`, so a step that rewrites its own artifact (a reconcile round) and a
    step run again write nothing. `write`'s refusals reach the caller."""
    item_id = str(run.state["params"].get("module") or "")
    path = backlog_path(cfg.work_source)
    head = runner_head(run)
    if not output or not item_id or path is None or head is None:
        return None
    change = "spec" if output == values.get("spec") else "plan" if output == values.get("plan_target") else ""
    if not change:
        return None
    item = _item_at(Path(worktree), head, path, item_id)
    if item is None or not applies(cfg, run, item) or getattr(item, change) == output:
        return None
    return write(cfg, run, worktree, change, lambda text: backlog_file.set_fields_text(text, item_id, {change: output}),
                 f"Record the {item_id} {change} in the backlog")


def mark_done(cfg: Config, run: Run, worktree: Path) -> str | None:
    """Set `status: done` on the run's item in `finish`, as one commit on top of `runner_head(run)`; return its sha, or
    None when nothing is written: the run does not write to the backlog (`applies`), or the item is already `done` in
    the file at `HEAD`, so a resumed `finish` writes nothing twice.

    When the item is not in the file at `HEAD` (a teammate removed it on `fork_from` and the base sync merged that in),
    nothing is written and `backlog_skipped` says so, for a run that wrote to the backlog: `applies` to the item as the
    run's latest backlog commit, or its fork commit, had it. An item that is `dropped` at `HEAD` (a person dropped it
    while the run went on) is never set to `done`: `backlog_skipped` says that instead. Either is said once, and the
    run goes on. `write`'s refusals reach the caller."""
    item_id = str(run.state["params"].get("module") or "")
    path = backlog_path(cfg.work_source)
    head = runner_head(run)
    if not item_id or path is None or head is None:
        return None
    worktree = Path(worktree)

    def skip(reason: str) -> None:
        if not any(e["type"] == "backlog_skipped" and e.get("change") == "done" for e in run.events.read()):
            run.emit("backlog_skipped", item=item_id, change="done", reason=reason)  # a resumed `finish` says it once

    item = _item_at(worktree, head, path, item_id)
    if item is None:
        earlier = _item_at(worktree, run.state.get("backlog_sha") or run.state.get("base_sha"), path, item_id)
        if applies(cfg, run, earlier):
            skip(f"{item_id} is not in {path} at HEAD")
        return None
    if item.status == "done" or not applies(cfg, run, item):
        return None
    if item.status == "dropped":  # a person dropped it on `fork_from` while the run went on: that decision stands
        skip(f"{item_id} is dropped in {path} at HEAD")
        return None
    return write(cfg, run, worktree, "done", lambda text: backlog_file.set_fields_text(text, item_id, {"status": "done"}),
                 f"Mark {item_id} done in the backlog")


# --------------------------------------------------------------------------- the base sync's merge


def _decoded(data: bytes | None, what: str) -> str:
    try:
        return (data or b"").decode("utf-8")
    except UnicodeDecodeError:
        raise backlog_file.EditRefused(f"{what} is not valid UTF-8") from None


def _replay_added(text: str, worktree: Path, event: dict, path: str, fork: str) -> str:
    """`text` with the item the run's `added` commit appended: its block as that commit holds it (read through
    `gitops.read_blob`; a runner commit, so the comments and unknown keys it copied are the runner's own), or, when the
    target already has an item with that id, that item, which must be the same one but for its `status`."""
    item_id = str(event["item"])
    source = _decoded(gitops.read_blob(worktree, str(event.get("sha") or ""), path, backlog_file.FILE_MAX),
                      f"{path} at the run's commit adding {item_id}")
    added = next((i for i in backlog_file.parse(source)[0] if i.id == item_id), None)
    if added is None:
        raise backlog_file.EditRefused(f"item {item_id} cannot be read from the run's commit that added it to {path}")
    there = next((i for i in backlog_file.parse(text)[0] if i.id == item_id), None)
    if there is not None:
        if dataclasses.replace(there, status=added.status) != added:
            raise backlog_file.EditRefused(f"item {item_id} is already in {path} on {fork} as a different item")
        if there.status in (added.status, "dropped"):  # a person's drop stands, as `mark_done` keeps it
            return text
        return backlog_file.set_fields_text(text, item_id, {"status": added.status}, name=path)
    try:
        return backlog_file.append_span_text(text if text.strip() else backlog_file.render([]), source, item_id,
                                             name=path)
    except backlog_file.EditRefused:
        return _append(text, added)


def replay(cfg: Config, run: Run, worktree: Path, target_sha: str) -> str:
    """The backlog file for the merge of `target_sha` into the run's branch: the file as commit `target_sha` holds it
    (read through `gitops.read_blob`; an empty file when it has none) with this run's own recorded changes made on it
    again, in event order, by `backlog_file`'s text functions, so every other line is the target's.

    An `added` item is appended as the run's commit wrote it (`_replay_added`). For `ready`, `spec`, `plan` and `done`
    the event's `fields` are set; a `status` is not set on an item that is `dropped` on the target (a person's drop
    stands, as in `mark_done`). Line endings follow the target's file. `backlog_file.EditRefused` names the item when a
    change cannot be made (the target removed the item, say)."""
    path = backlog_path(cfg.work_source)
    if path is None:
        raise ValueError("only a backlog pack replays its backlog changes")
    fork = cfg.project.fork_from
    worktree = Path(worktree)
    text = _decoded(gitops.read_blob(worktree, target_sha, path, backlog_file.FILE_MAX), f"{path} on {fork}")
    for event in run.events.read():
        if event["type"] != "backlog_updated" or event.get("path", path) != path:
            continue
        if event.get("change") == "added":
            text = _replay_added(text, worktree, event, path, fork)
            continue
        item_id = str(event["item"])
        fields = dict(event.get("fields") or {})
        there = next((i for i in backlog_file.parse(text)[0] if i.id == item_id), None)
        if there is not None and there.status == "dropped":
            fields.pop("status", None)
        if fields:
            text = backlog_file.set_fields_text(text, item_id, fields, name=path)
    if len(text.encode("utf-8")) > backlog_file.FILE_MAX:
        raise backlog_file.EditRefused(f"the merge would make {path} larger than {backlog_file.FILE_MAX} bytes")
    return text


def bring_in(cfg: Config, run: Run, worktree: Path, target_sha: str, conflicts: list[str]) -> str:
    """Settle the backlog file of the merge of `target_sha` that `gitops.pull_base` just started, and return the blob
    the runner staged for it ("" when it had nothing to settle).

    Only a `backlog` pack, and only when git conflicted on the file (it is in `conflicts`) or merged it into something
    that is neither the branch's (`HEAD`) nor the target's file. The file is then `replay`'s text, written to the work
    tree (no symlink followed) and staged as exactly those bytes (`gitops.stage_resolved`), so no session is told about
    it and `gitops.commit_sync` lets that blob, and nothing else, through. `backlog_file.EditRefused` when the replay
    cannot be made, `OSError`/`ValueError` when the file cannot be written, `gitops.GitError` when it cannot be staged:
    the merge is left in progress for the caller to stop on."""
    path = backlog_path(cfg.work_source)
    if path is None:
        return ""
    worktree = Path(worktree)
    if path not in conflicts:
        merged = gitops.merged_blob(worktree, path)
        if merged in (gitops.blob_id(worktree, "HEAD", path), gitops.blob_id(worktree, target_sha, path)):
            return ""  # one side's file, as it is: the commit's own check lets it through
    text = replay(cfg, run, worktree, target_sha)
    safeio.write_text(worktree / path, text)
    return gitops.stage_resolved(worktree, path, text.encode("utf-8"))
