"""Which modules are finished, and why: hand marks and vetoes, and the reasons the last assessment recorded.

A person marks a module done by hand (`mark`), or vetoes a Done the evidence gave (`veto`); both live in
`runs/done_marks.json` until `unmark` removes them. `module_queue.assess_modules` decides the rest from origin and
`gh`, and records each done module's reason in `runs/queue.json`. `recorded` reads both files and no git or `gh`,
so the Backlog, `agentflow next` and `modules_done` can ask what is done at no cost.

A marks file that cannot be read never undoes a module marked done by hand: `recorded` keeps the last assessment's
hand-marked entries, and a change is refused until the file is repaired or deleted.
"""

from __future__ import annotations

import fcntl
import json
import os
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agentflow.state import runs_root

QUEUE_FILE = "queue.json"  # written by `module_queue.QueueState`
MARKS = "done_marks.json"
LOCK = "done_marks.lock"

BY_HAND = "done by hand"
SPEC_SAYS = "spec says done"
MERGED = "merged"

_STATES = ("done", "not_done")


def plan_complete(branch: str) -> str:
    """The reason for a module whose newest plan is fully ticked on `origin/<branch>` and names its spec."""
    return f"plan complete on {branch}"


def _unreadable(path: Path, why: str) -> str:
    return f"{path} is unreadable ({why}); fix it or delete it, then mark again"


def read_marks(repo: Path) -> tuple[dict[str, dict], str | None]:
    """`({module: {"state": "done" | "not_done", "by", "at"}}, error)`. A missing file is `({}, None)`; a file that
    cannot be read, is not JSON, or has the wrong shape is `({}, <what is wrong and how to repair it>)`."""
    path = runs_root(repo) / MARKS
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}, None
    except (OSError, ValueError) as exc:  # ValueError: bytes that are not UTF-8
        return {}, _unreadable(path, getattr(exc, "strerror", None) or type(exc).__name__)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return {}, _unreadable(path, f"invalid JSON at line {exc.lineno}")
    if not isinstance(data, dict):
        return {}, _unreadable(path, "not a JSON object of module marks")
    marks: dict[str, dict] = {}
    for module, mark in data.items():
        if not isinstance(mark, dict) or mark.get("state") not in _STATES:
            return {}, _unreadable(path, f"the mark for {str(module)[:80]!r} is not done or not_done")
        marks[str(module)] = {"state": mark["state"], "by": str(mark.get("by") or ""), "at": str(mark.get("at") or "")}
    return marks, None


def _change(repo: Path, edit: Callable[[dict[str, dict]], Any]) -> Any:
    """Run `edit` on the marks under `LOCK` and replace `MARKS` atomically when it changed them. Each call opens the
    lock file afresh, so two threads of one process exclude each other as two processes do."""
    root = runs_root(repo)
    root.mkdir(parents=True, exist_ok=True)
    fd = os.open(root / LOCK, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        marks, error = read_marks(repo)
        if error:
            raise ValueError(error)
        before = json.dumps(marks, sort_keys=True)
        result = edit(marks)
        if json.dumps(marks, sort_keys=True) != before:
            path = root / MARKS
            tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
            try:
                tmp.write_text(json.dumps(marks, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
                tmp.replace(path)
            finally:
                tmp.unlink(missing_ok=True)
        return result
    finally:
        os.close(fd)  # closing the only descriptor of this open releases the lock


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _set(repo: Path, module: str, state: str, by: str) -> None:
    def edit(marks: dict[str, dict]) -> None:
        marks[str(module)] = {"state": state, "by": str(by), "at": _now()}

    _change(repo, edit)


def mark(repo: Path, module: str, by: str) -> None:
    """Record that `module` is done by hand. Raises `ValueError` when the marks file is unreadable."""
    _set(repo, module, "done", by)


def veto(repo: Path, module: str, by: str) -> None:
    """Record that `module` is not done, whatever origin says. Raises `ValueError` when the marks file is unreadable."""
    _set(repo, module, "not_done", by)


def unmark(repo: Path, module: str) -> bool:
    """Remove `module`'s mark or veto; whether there was one. Raises `ValueError` when the marks file is unreadable."""
    return _change(repo, lambda marks: marks.pop(str(module), None) is not None)


def recorded(repo: Path) -> dict[str, str]:
    """`{module: reason}` for every module that is done now, from files only (no git, no `gh`).

    The last assessment's done entries in `QUEUE_FILE` (a missing reason, from before reasons were kept, is
    `MERGED`; a broken file counts as none), less an entry done by hand whose mark is gone and any entry with a live
    veto, plus every live done mark as `BY_HAND`. When the marks file cannot be read, the entries stay as recorded,
    so a module marked done by hand stays done."""
    try:
        data = json.loads((runs_root(repo) / QUEUE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    modules = data.get("modules") if isinstance(data, dict) else None
    done: dict[str, str] = {}
    for entry in modules if isinstance(modules, list) else []:
        if isinstance(entry, dict) and entry.get("status") == "done" and isinstance(entry.get("id"), str):
            reason = entry.get("done_reason")
            done[entry["id"]] = reason if isinstance(reason, str) and reason else MERGED
    marks, error = read_marks(repo)
    if error:
        return done
    for module, reason in list(done.items()):
        state = (marks.get(module) or {}).get("state")
        if state == "not_done" or (reason == BY_HAND and state != "done"):
            del done[module]
    for module, mark_ in marks.items():
        if mark_["state"] == "done":
            done[module] = BY_HAND
    return done


__all__ = ["BY_HAND", "LOCK", "MARKS", "MERGED", "QUEUE_FILE", "SPEC_SAYS", "mark", "plan_complete", "read_marks",
           "recorded", "unmark", "veto"]
