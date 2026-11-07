"""The pack's work source: where the modules `agentflow queue`, the Backlog and `modules_done` walk come from.

`work_source:` in the pack names a `kind`. This engine reads two:

- `capability_map`, at `path`: a Markdown table with `Module id` and `Depends on` columns, one row per module in
  build order (`sources.capability_map`, `sources.add_module_row`).
- `specs`, which takes no path: one module per file in `project.docs.specs` whose name fits
  `project.docs.spec_file`, its dependencies read from the spec's header (`sources.spec_deps`). A new module is a
  new spec file, which the run's define step writes, so `add_item` writes nothing.

A pack without a work source has no modules: `items` is empty and `add_item` refuses.
"""

from __future__ import annotations

import os
import stat
import threading
from pathlib import Path
from typing import TYPE_CHECKING

from agentflow import sources

if TYPE_CHECKING:
    from agentflow.config import Config

_NOUNS = {"capability_map": "capability map", "specs": "spec files"}

# Per specs directory: `{name: ((st_mtime_ns, st_size), deps)}` from the last listing, so a header is read again
# only when its file changes (the Backlog lists the work source on every render).
_HEADERS: dict[str, dict[str, tuple[tuple[int, int], list[str]]]] = {}
_HEADERS_LOCK = threading.Lock()


def _kind(cfg: Config) -> str | None:
    return cfg.work_source.kind if cfg.work_source is not None else None


def kind(cfg: Config) -> str | None:
    """The work source's kind (`capability_map` or `specs`), or None when the pack has no work source."""
    return _kind(cfg)


def noun(cfg: Config) -> str:
    """The words messages use for where the modules are listed: `capability map` (also with no work source) or
    `spec files`."""
    return _NOUNS.get(_kind(cfg) or "capability_map", "capability map")


def _unsafe(cfg: Config, repo: Path) -> str | None:
    """The work source's `path` when it is absolute or resolves (`..`, symlinks) outside `repo`, else None."""
    if cfg.work_source is None:
        return None
    raw = cfg.work_source.path
    if Path(raw).is_absolute():
        return raw
    root = Path(repo).resolve()
    if not (root / raw).resolve().is_relative_to(root):
        return raw
    return None


def file(cfg: Config, repo: Path) -> Path | None:
    """The work source's file inside `repo`, or None when the pack has no work source, its kind has no file
    (`specs`) or its path leaves `repo`."""
    if cfg.work_source is None or _kind(cfg) == "specs" or _unsafe(cfg, repo) is not None:
        return None
    return Path(repo) / cfg.work_source.path


def directory(cfg: Config, repo: Path) -> Path | None:
    """For `specs`, the directory listed (`repo / project.docs.specs`); None when that path is absolute, resolves
    outside `repo`, is a symlink or is not a directory, and always None for other kinds."""
    if _kind(cfg) != "specs":
        return None
    raw = cfg.project.docs.get("specs")
    if not isinstance(raw, str) or not raw.strip() or Path(raw).is_absolute():
        return None
    root = Path(repo).resolve()
    path = Path(repo) / raw
    if path.is_symlink() or not path.resolve().is_relative_to(root) or not path.is_dir():
        return None
    return path


def _list_specs(cfg: Config, repo: Path) -> tuple[list[sources.ModuleNode], list[str]]:
    """The modules in the specs directory, sorted by id, and the names that fit `spec_file` but are not modules."""
    folder = directory(cfg, repo)
    if folder is None:
        return [], []
    spec_file = cfg.project.spec_file
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return [], []
    fitting = list(sources.spec_ids(names, spec_file).items())
    limit = sources.SPECS_MAX
    skipped: list[str] = []
    if len(fitting) > limit:
        skipped.append(f"… and {len(fitting) - limit} more (over {limit} files)")
        fitting = fitting[:limit]
    key = str(folder.resolve())
    with _HEADERS_LOCK:
        cached = dict(_HEADERS.get(key, {}))
    fresh: dict[str, tuple[tuple[int, int], list[str]]] = {}
    nodes: list[sources.ModuleNode] = []
    for module, name in fitting:
        if not sources.is_module_id(module, spec_file):
            skipped.append(name)
            continue
        try:
            info = os.lstat(folder / name)
        except OSError:
            skipped.append(f"{name} (unreadable)")
            continue
        if not stat.S_ISREG(info.st_mode):  # a symlink, a directory, a pipe...
            skipped.append(name)
            continue
        stamp = (info.st_mtime_ns, info.st_size)
        known = cached.get(name)
        if known is not None and known[0] == stamp:
            deps = known[1]
        else:
            try:
                text = sources.read_header(folder / name)
            except OSError:
                skipped.append(f"{name} (unreadable)")
                continue
            deps = sources.spec_deps(text)
            if info.st_size > sources.HEADER_MAX and not sources.header_ends(text):
                # the header may go on past the bytes read (a dependency line there, or one cut mid-id): block
                deps = list(dict.fromkeys([*deps, sources.CUT_DEP]))
        fresh[name] = (stamp, deps)
        nodes.append(sources.ModuleNode(module, list(deps)))
    with _HEADERS_LOCK:
        _HEADERS[key] = fresh
    return sorted(nodes, key=lambda n: n.id), sorted(skipped)


def items(cfg: Config, repo: Path) -> list[sources.ModuleNode]:
    """The modules in the work source: in row order for `capability_map`, sorted by id for `specs`; `[]` when the
    pack has none, its file or directory is missing or its path leaves `repo` (not an error here: the Backlog and
    the queue read this on every render; `add_item` refuses)."""
    if cfg.work_source is None:
        return []
    if cfg.work_source.kind == "specs":
        return _list_specs(cfg, repo)[0]
    path = file(cfg, repo)
    if path is None or not path.is_file():
        return []
    if cfg.work_source.kind == "capability_map":
        return sources.capability_map(path)
    raise ValueError(f"work_source: unknown kind {cfg.work_source.kind!r}")


def skipped(cfg: Config, repo: Path) -> list[str]:
    """For `specs`, the sorted names that fit `spec_file` but are not modules: an id that is not a module id, a
    symlink or anything but a regular file, `<name> (unreadable)`, and one line for the entries past
    `sources.SPECS_MAX`. `[]` for other kinds."""
    if _kind(cfg) != "specs":
        return []
    return _list_specs(cfg, repo)[1]


def add_item(cfg: Config, repo: Path, template: dict[str, str], values: dict) -> None:
    """Add one module to the work source (`values`: `module`, `responsibility`, `deps`), filled from `template`.
    For `specs` it writes nothing: the new module's spec file is the run's to write."""
    if cfg.work_source is None:
        raise ValueError("this pack has no work_source")
    if cfg.work_source.kind == "specs":
        return
    bad = _unsafe(cfg, repo)
    if bad is not None:
        raise ValueError(f"work_source.path must stay inside the repository: {bad}")
    path = file(cfg, repo)
    if path is None:
        raise ValueError("this pack has no work_source")
    if cfg.work_source.kind == "capability_map":
        sources.add_module_row(path, template, values)
        return
    raise ValueError(f"work_source: unknown kind {cfg.work_source.kind!r}")
