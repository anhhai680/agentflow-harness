"""A run's own documents, listed and read by a fixed allow-list of names.

Every name comes from an untrusted client, so a name must `fullmatch` RUN_FILE, and it is only ever joined to the
run's folder after that. A file is read under `views._tail`'s rules (no symlink on the file or its folder, resolved
inside its root, opened `O_NOFOLLOW`, a regular file), but from its head."""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path

from .artifacts import NotFound
from .intake import ISSUE_FILE, LOG_FILE, TASK_FILE
from .state import Run

RUN_FILE = re.compile(r"^(?:issue\.md|task\.md|bug\.log|questions\.md|pr\.md|handoff\.md|progress\.md|"
                      r"handoffs/[a-z0-9_-]{1,64}\.md|refine/[a-z0-9_-]{1,64}-\d{1,4}\.md)$")
GROUPS = ("request", "handoffs", "refine", "pull request", "notes")
_REQUEST = (ISSUE_FILE, TASK_FILE, LOG_FILE)
_NOTES = ("progress.md", "questions.md", "handoff.md")
_FOLDER_FILE = re.compile(r"^[a-z0-9_-]{1,64}\.md$")
_ROUND = re.compile(r"^([a-z0-9_-]{1,64})-(\d{1,4})\.md$")


def _root(run: Run, name: str) -> Path:
    return run.agent_dir if name == "progress.md" else run.dir


def _head(root: Path, path: Path, limit: int) -> tuple[bytes, int] | None:
    """The first `limit` bytes of `path` and its size, or None unless it is a regular file inside `root`
    reached without a symlink (on the file or on its folder)."""
    try:
        if path.is_symlink() or path.parent.is_symlink():
            return None
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(root.resolve()) or not resolved.is_file():
            return None
        fd = os.open(resolved, os.O_RDONLY | os.O_NOFOLLOW)
    except (OSError, RuntimeError):
        return None
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode):
            return None
        return handle.read(max(0, limit)), info.st_size


def _phase_order(run: Run) -> list[str]:
    order: list[str] = []
    for ev in run.events.read():
        phase = ev.get("phase")
        if ev.get("type") in ("phase_started", "phase_approved") and isinstance(phase, str) and phase not in order:
            order.append(phase)
    return order


def _approved_order(run: Run) -> list[str]:
    order: list[str] = []
    for ev in run.events.read():
        phase = ev.get("phase")
        if ev.get("type") == "phase_approved" and isinstance(phase, str) and phase not in order:
            order.append(phase)
    return order


def _names(run: Run, group: str) -> list[str]:
    if group == "request":
        return list(_REQUEST)
    if group == "pull request":
        return ["pr.md"]
    if group == "notes":
        return list(_NOTES)
    folder = run.dir / ("handoffs" if group == "handoffs" else "refine")
    if folder.is_symlink() or not folder.is_dir():
        return []
    try:
        found = sorted(p.name for p in folder.iterdir() if _FOLDER_FILE.match(p.name) or _ROUND.match(p.name))
    except OSError:
        return []
    if group == "handoffs":
        approved = _approved_order(run)
        rank = lambda n: (approved.index(n[:-3]) if n[:-3] in approved else len(approved), n)  # noqa: E731
        return [f"handoffs/{n}" for n in sorted((n for n in found if _FOLDER_FILE.match(n)), key=rank)]
    order = _phase_order(run)

    def key(n: str) -> tuple[int, str, int]:
        m = _ROUND.match(n)
        phase, rnd = (m.group(1), int(m.group(2))) if m else (n, 0)
        return (order.index(phase) if phase in order else len(order), phase, rnd)
    return [f"refine/{n}" for n in sorted((n for n in found if _ROUND.match(n)), key=key)]


def list_files(run: Run) -> list[dict]:
    """`[{name, group, size}]` for each allowed file that exists and passes the read rules, in GROUPS order."""
    out: list[dict] = []
    for group in GROUPS:
        for name in _names(run, group):
            if not RUN_FILE.fullmatch(name):
                continue
            got = _head(_root(run, name), _root(run, name) / name, 0)
            if got is not None:
                out.append({"name": name, "group": group, "size": got[1]})
    return out


def read_file(run: Run, name: str, max_bytes: int) -> dict:
    """`{name, text, size, truncated}`, read from the head and cut after the last newline when over `max_bytes`."""
    missing = NotFound(f"no file {name} in this run")
    if not isinstance(name, str) or not RUN_FILE.fullmatch(name):
        raise missing
    root = _root(run, name)
    got = _head(root, root / name, max_bytes)
    if got is None:
        raise missing
    data, size = got
    truncated = size > max_bytes
    if truncated:
        cut = data.rfind(b"\n")
        data = data[:cut + 1] if cut >= 0 else data
    return {"name": name, "text": data.decode("utf-8", errors="replace"), "size": size, "truncated": truncated}
