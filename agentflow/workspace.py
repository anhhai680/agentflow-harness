"""The per-run agent workspace: the one directory an agent may write outside its working tree.

Agents get `<state dir>/workspaces/<run>/` (`Run.agent_dir`) as their only `--add-dir`. The run
directory (`runs/<run>/`: `events.jsonl`, `state.json`, `inbox/`, `runner.lock`, the runner's result
copies, gate logs), the other runs, the queue files and the config are outside every directory an agent
may write, so nothing an agent writes can change a run's state.

Before every agent call, `prepare` refreshes the workspace's read-only-by-convention copies of the run's
inputs from the run directory, which stays the source of truth: `issue.md`, `bug.log`, `task.md`,
`questions.md`, `handoff.md`, `handoffs/*.md` and `results/*.json` (the checked results of earlier
steps). Whatever the agent left at those names (an edit, a symlink, a folder) is replaced, never written
through. The agent writes its result to `out/<step>.json` (`AGENTFLOW_RESULT`) and its notes to
`progress.md`; the runner reads only those two, and `take_result` copies a result into the run
directory (`Run.result_path`) before anything parses it, so later readers see the checked copy only.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

from agentflow import safeio
from agentflow.state import Run, workspaces_root

INPUT_FILES = ("issue.md", "bug.log", "task.md", "questions.md", "handoff.md")
INPUT_DIRS = {"handoffs": "*.md", "results": "*.json"}
OUT_DIR = "out"
RESULT_LIMIT = 1_000_000  # bytes; a result file is a few lines of JSON

__all__ = ["INPUT_DIRS", "INPUT_FILES", "OUT_DIR", "clear_result", "prepare", "take_result"]


def _real_dir(path: Path) -> Path:
    """`path` as a real directory: anything else there (a symlink, a file) is removed first."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        path.mkdir()
        return path
    if not stat.S_ISDIR(info.st_mode):
        safeio.remove(path)
        path.mkdir()
    return path


def _root(run: Run) -> Path:
    _real_dir(workspaces_root(run.repo))
    return _real_dir(run.agent_dir)


def _mirror(source: Path, target: Path) -> None:
    """Make `target` a fresh copy of the runner's `source`, or remove it when `source` is gone."""
    if source.is_file() and not source.is_symlink():
        safeio.replace_file(target, source.read_bytes())
    else:
        safeio.remove(target)


def prepare(run: Run) -> Path:
    """Refresh the workspace's copies of the run's inputs; returns the workspace directory."""
    root = _root(run)
    for name in INPUT_FILES:
        _mirror(run.dir / name, root / name)
    for folder, pattern in INPUT_DIRS.items():
        target = _real_dir(root / folder)
        wanted = {p.name for p in (run.dir / folder).glob(pattern)} if (run.dir / folder).is_dir() else set()
        for entry in target.iterdir():
            if entry.name not in wanted:
                safeio.remove(entry)
        for name in sorted(wanted):
            _mirror(run.dir / folder / name, target / name)
    _real_dir(root / OUT_DIR)
    return root


def clear_result(run: Run, key: str) -> None:
    """Remove the step's result, the agent's and the runner's copy: a stale one must never count."""
    safeio.remove(run.agent_result_path(key))
    run.result_path(key).unlink(missing_ok=True)


def take_result(run: Run, key: str) -> str:
    """Copy the agent's result for `key` into the run directory, unparsed; "" when it did, else why not
    (the runner's copy is then absent, so reading it reports a missing result)."""
    target = run.result_path(key)
    target.unlink(missing_ok=True)
    try:
        data = safeio.read_bytes(run.agent_result_path(key), RESULT_LIMIT)
    except (OSError, ValueError) as exc:
        return f"the result file cannot be read: {exc}"
    if data is None:
        return ""
    safeio.ensure_dir(target.parent)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, target)
    return ""
