"""The pack's work source: where the modules `agentflow queue`, the Backlog and `modules_done` walk come from.

`work_source:` in the pack names a `kind`. This engine reads three:

- `capability_map`, at `path`: a Markdown table with `Module id` and `Depends on` columns, one row per module in
  build order (`sources.capability_map`, `sources.add_module_row`).
- `specs`, which takes no path: one module per file in `project.docs.specs` whose name fits
  `project.docs.spec_file`, its dependencies read from the spec's header (`sources.spec_deps`). A new module is a
  new spec file, which the run's define step writes, so `add_item` writes nothing.
- `backlog`, at `path` (default `backlog.yml`): one item per entry of the YAML file (`backlog_file.read`), in file
  order, and `skipped` lists the spec files no item names. `add_item` writes nothing: items are added through
  `backlog_file.append_item`.

A pack without a work source has no modules: `items` is empty and `add_item` refuses.
"""

from __future__ import annotations

import fnmatch
import os
import stat
import threading
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from agentflow import backlog_file, sources

if TYPE_CHECKING:
    from agentflow.config import Config

_NOUNS = {"capability_map": "capability map", "specs": "spec files", "backlog": "backlog"}

# Per specs directory: `{name: ((st_mtime_ns, st_size), deps)}` from the last listing, so a header is read again
# only when its file changes (the Backlog lists the work source on every render).
_HEADERS: dict[str, dict[str, tuple[tuple[int, int], list[str]]]] = {}
_HEADERS_LOCK = threading.Lock()


def _kind(cfg: Config) -> str | None:
    return cfg.work_source.kind if cfg.work_source is not None else None


def kind(cfg: Config) -> str | None:
    """The work source's kind (`capability_map`, `specs` or `backlog`), or None when the pack has no work source."""
    return _kind(cfg)


def hand_marks(cfg: Config) -> bool:
    """Whether `runs/done_marks.json` counts for this pack: not for `backlog`, whose file is the only say."""
    return _kind(cfg) != "backlog"


def noun(cfg: Config) -> str:
    """The words messages use for where the modules are listed: `capability map` (also with no work source),
    `spec files` or `backlog`."""
    return _NOUNS.get(_kind(cfg) or "capability_map", "capability map")


def _unsafe(cfg: Config, repo: Path) -> str | None:
    """The work source's `path` when it is absolute or resolves (`..`, symlinks) outside `repo`, else None."""
    if cfg.work_source is None:
        return None
    raw = cfg.work_source.path
    if Path(raw).is_absolute():
        return raw
    try:
        root = Path(repo).resolve()
        if not (root / raw).resolve().is_relative_to(root):
            return raw
    except (OSError, RuntimeError):  # a symlink loop on the way (RuntimeError on Python 3.12)
        return raw
    return None


def file(cfg: Config, repo: Path) -> Path | None:
    """The work source's file inside `repo`, or None when the pack has no work source, its kind has no file
    (`specs`) or its path leaves `repo`. For `backlog` it is the backlog file, which may not exist yet."""
    if cfg.work_source is None or _kind(cfg) == "specs" or _unsafe(cfg, repo) is not None:
        return None
    return Path(repo) / cfg.work_source.path


def _specs_dir(cfg: Config, repo: Path) -> Path | None:
    """`repo / project.docs.specs`, or None when that path is absent, absolute, resolves outside `repo`, is a symlink
    or is not a directory."""
    raw = cfg.project.docs.get("specs")
    if not isinstance(raw, str) or not raw.strip() or Path(raw).is_absolute():
        return None
    path = Path(repo) / raw
    try:
        inside = Path(path).resolve().is_relative_to(Path(repo).resolve())
    except (OSError, RuntimeError):  # a symlink loop on the way (RuntimeError on Python 3.12)
        return None
    if path.is_symlink() or not inside or not path.is_dir():
        return None
    return path


def directory(cfg: Config, repo: Path) -> Path | None:
    """For `specs`, the directory listed (`repo / project.docs.specs`); None when that path is absolute, resolves
    outside `repo`, is a symlink or is not a directory, and always None for other kinds."""
    if _kind(cfg) != "specs":
        return None
    return _specs_dir(cfg, repo)


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
    try:
        key = str(folder.resolve())
    except (OSError, RuntimeError):
        return [], []
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
            if info.st_size > sources.HEADER_MAX and not sources.header_ends(text):
                # the header may go on past the bytes read: drop the last line, which the cut may have split (a
                # `Depends on: crm` of `crm-api`), keep what was read whole, and block on LONG_HEADER
                text = text[: text.rfind("\n") + 1]
                deps = list(dict.fromkeys([*sources.spec_deps(text), sources.LONG_HEADER]))
            else:
                deps = sources.spec_deps(text)
        fresh[name] = (stamp, deps)
        nodes.append(sources.ModuleNode(module, list(deps)))
    with _HEADERS_LOCK:
        _HEADERS[key] = fresh
    return sorted(nodes, key=lambda n: n.id), sorted(skipped)


def items(cfg: Config, repo: Path) -> list[sources.ModuleNode]:
    """The modules in the work source: in row order for `capability_map`, sorted by id for `specs`, in file order
    and without the `dropped` ones for `backlog`; `[]` when the pack has none, its file or directory is missing,
    unreadable or its path leaves `repo` (not an error here: the Backlog and the queue read this on every render;
    `add_item` refuses, and `problems` says what is wrong with a backlog file)."""
    if cfg.work_source is None:
        return []
    if cfg.work_source.kind == "backlog":
        return [i for i in all_items(cfg, repo) if i.status != "dropped"]
    if cfg.work_source.kind == "specs":
        return _list_specs(cfg, repo)[0]
    path = file(cfg, repo)
    if path is None or not path.is_file():
        return []
    if cfg.work_source.kind == "capability_map":
        return sources.capability_map(path)
    raise ValueError(f"work_source: unknown kind {cfg.work_source.kind!r}")


def _read_backlog(cfg: Config, repo: Path) -> tuple[list[tuple[int, backlog_file.BacklogItem]], list[backlog_file.Problem]]:
    """The backlog file's items and problems; one error and no items when the path leaves `repo`. Never raises."""
    if _kind(cfg) != "backlog":
        return [], []
    bad = _unsafe(cfg, repo)
    if bad is not None:
        return [], [backlog_file.Problem("error", "work_source.path", f"must stay inside the repository: {bad}")]
    path = file(cfg, repo)
    if path is None:
        return [], []
    return backlog_file.read_indexed(path)


def all_items(cfg: Config, repo: Path) -> list[backlog_file.BacklogItem]:
    """For `backlog`, every item of the file in file order, the `dropped` ones too (`[]` when the file can't be read);
    `[]` for other kinds."""
    return [item for _, item in _read_backlog(cfg, repo)[0]]


def indexed(cfg: Config, repo: Path) -> tuple[list[tuple[int, backlog_file.BacklogItem]], list[backlog_file.Problem]]:
    """For `backlog`, `all_items` with each item's position in the file's `items` list (an item the parser dropped
    leaves a gap), and `problems`, from one read of the file; `([], [])` for other kinds."""
    return _read_backlog(cfg, repo)


def item(cfg: Config, repo: Path, module: str) -> backlog_file.BacklogItem | None:
    """For `backlog`, the item with id `module` (a `dropped` one too), or None."""
    return next((i for i in all_items(cfg, repo) if i.id == module), None)


def problems(cfg: Config, repo: Path) -> list[backlog_file.Problem]:
    """For `backlog`, what is wrong with the file (a missing file is one error); `[]` for other kinds."""
    return _read_backlog(cfg, repo)[1]


def _unlisted_specs(cfg: Config, repo: Path, named: set[str]) -> list[str]:
    """The Markdown files under `project.docs.specs` that no item names and `backlog.ignore_specs` doesn't silence,
    as sorted repo-relative POSIX paths: symlinks and `project.docs.plans` are not entered, and at most
    `sources.SPECS_MAX` files are examined."""
    folder = _specs_dir(cfg, repo)
    if folder is None:
        return []
    root = Path(repo)
    plans = cfg.project.docs.get("plans")
    skip_plans = root / plans if isinstance(plans, str) and plans.strip() and not Path(plans).is_absolute() else None
    found: list[str] = []
    examined = 0
    for here, dirs, names in os.walk(folder, followlinks=False):
        here_path = Path(here)
        dirs[:] = sorted(d for d in dirs if skip_plans is None or here_path / d != skip_plans)
        for name in sorted(names):
            if examined >= sources.SPECS_MAX:
                break
            examined += 1
            full = here_path / name
            try:
                regular = stat.S_ISREG(os.lstat(full).st_mode)
            except OSError:
                continue
            if not regular or not name.endswith(".md"):
                continue
            within = PurePosixPath(*full.relative_to(folder).parts).as_posix()
            if any(fnmatch.fnmatchcase(within, glob) for glob in cfg.backlog.ignore_specs):
                continue
            rel = PurePosixPath(*full.relative_to(root).parts).as_posix()
            if rel not in named:
                found.append(rel)
        if examined >= sources.SPECS_MAX:
            break
    return sorted(found)


def unlisted_specs(cfg: Config, repo: Path, named: set[str]) -> list[str]:
    """`_unlisted_specs`, for `backlog import`: the Markdown files under `project.docs.specs` (any work source kind)
    that are not in `named` (repo-relative POSIX paths), `backlog.ignore_specs` and `project.docs.plans` left out."""
    return _unlisted_specs(cfg, repo, named)


def skipped(cfg: Config, repo: Path) -> list[str]:
    """For `specs`, the sorted names that fit `spec_file` but are not modules: an id that is not a module id, a
    symlink or anything but a regular file, `<name> (unreadable)`, and one line for the entries past
    `sources.SPECS_MAX`. For `backlog`, the sorted repo-relative paths of the spec files no item's `spec` names
    (`_unlisted_specs`). `[]` for other kinds."""
    kind = _kind(cfg)
    if kind == "backlog":
        return _unlisted_specs(cfg, repo, {i.spec for i in all_items(cfg, repo) if i.spec})
    if kind != "specs":
        return []
    return _list_specs(cfg, repo)[1]


def add_item(cfg: Config, repo: Path, template: dict[str, str], values: dict) -> None:
    """Add one module to the work source (`values`: `module`, `responsibility`, `deps`), filled from `template`.
    For `specs` it writes nothing: the new module's spec file is the run's to write. Nor does `backlog`: its items
    are added with `backlog_file.append_item`."""
    if cfg.work_source is None:
        raise ValueError("this pack has no work_source")
    if cfg.work_source.kind in ("specs", "backlog"):
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
