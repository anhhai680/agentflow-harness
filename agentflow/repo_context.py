"""One repository as the dashboard serves it, and the pure rescan that turns a workspace into a map of them.

A `RepoContext` holds a repository's checkout, its loaded pack and its state together, so a request uses one
repository's `repo`, `cfg` and `config` and never a mix. A scanned repository's files are untrusted input: loading
its pack, counting its runs and looking for a live runner catch every `Exception` and turn it into an `error`
context, zero counts or a kept `missing` context, so one broken repository never costs the others their place.

Nothing here starts a server or a thread; the dashboard calls `reconcile` and swaps in what it returns."""

from __future__ import annotations

import copy
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

from . import hitl, layers, pack, state
from .config import Config, load_config
from .events import lock_state
from .repo_set import RepoRef, RepoSet, Skipped

State = Literal["live", "not_set_up", "missing", "error"]

ERROR_MAX = 300  # an error text is cut to one line of at most this many characters
ACTIVE = ("running", "refining", "held")  # the `display_status` values of a run that is working


@dataclass
class RepoContext:
    id: str  # `pack.repo_id` of the main checkout
    name: str
    repo: Path
    cfg: Config | None = None
    config: Path | None = None  # the `--config` file a single-repository dashboard was started with
    state: State = "live"
    error: str | None = None
    # the runs directory resolved when a workspace context was built; None for a single-repository context, which
    # follows its pack's `paths.state_dir` as it changes
    runs: Path | None = None


def _one_line(text: object) -> str:
    return " ".join(str(text).split())[:ERROR_MAX]


def _failed(ctx: RepoContext, text: object) -> RepoContext:
    return replace(ctx, cfg=None, state="error", error=_one_line(text))


def runs_dir(ctx: RepoContext) -> Path | None:
    """The runs directory `ctx.repo`'s pack names now, resolved; None when it can't be resolved."""
    try:
        return state.runs_root(ctx.repo).resolve()
    except Exception:  # a hostile `paths.state_dir`: `~nosuchuser`, a NUL byte, an unreadable folder
        return None


def load_context(ref: RepoRef) -> RepoContext:
    """`ref` as a context: its pack loaded when it has one, else `not_set_up`; any failure is an `error` context
    with the reason on one line. A `.agentflow` that is a symlink is an error, and the pack it leads to is not read.
    Never raises."""
    ctx = RepoContext(ref.id, ref.name, ref.path)
    if ref.state != "live":
        return replace(ctx, state="not_set_up")
    try:
        folder = ref.path / pack.PACK_DIR
        if folder.is_symlink():
            return _failed(ctx, f"{folder} is a symlink; a workspace repository's pack must be its own")
        cfg = load_config(pack.pack_config(ref.path))
        runs = runs_dir(ctx)
        if runs is None:
            return _failed(ctx, "its state directory (paths.state_dir) cannot be resolved")
        return replace(ctx, cfg=cfg, runs=runs)
    except Exception as exc:  # a pack that is not UTF-8 raises what `config` does not catch
        return _failed(ctx, exc)


def single_context(repo: Path, cfg: Config, config: Path | None = None) -> RepoContext:
    """The one live context of a single-repository dashboard (`make_server`, `--config`): `repo` as given, and no
    frozen runs directory, so it follows its pack as today."""
    root = pack.checkout_root(repo)
    return RepoContext(pack.repo_id(root), root.name, Path(repo), cfg=cfg, config=config)


def usable(ctx: RepoContext) -> bool:
    """Whether a request or a background pass may use `ctx`: it is live, and a workspace context's pack still names
    the runs directory it was built with."""
    return ctx.state == "live" and (ctx.runs is None or runs_dir(ctx) == ctx.runs)


def _runs(ctx: RepoContext) -> list[state.Run]:
    """Every run of `ctx.repo` that loads, oldest first; one that can't be loaded is skipped. An unreadable runs
    root raises."""
    root = state.runs_root(ctx.repo)
    if not root.is_dir():
        return []
    runs = []
    for path in sorted(root.iterdir()):
        try:
            if (path / "events.jsonl").is_file():
                runs.append(state.Run.load(ctx.repo, path.name))
        except Exception:
            continue
    return runs


def counts(ctx: RepoContext) -> dict[str, int]:
    """What `ctx`'s runs need: `waiting` (every inbox item, parked stops included), `active` (a runner is working)
    and `failed` (failed, or stalled with no runner). Zeros when the context is not `usable` (a pack that now names
    another runs directory would count another repository's runs) or its runs can't be read; a run that fails on
    its own costs only its own share."""
    out = {"waiting": 0, "active": 0, "failed": 0}
    if not usable(ctx):
        return out
    try:
        runs = _runs(ctx)
    except Exception:
        return out
    for run in runs:
        try:
            out["waiting"] += len(hitl.inbox_items(ctx.repo, [run]))
        except Exception:
            pass
        try:
            shown = state.display_status(run)
            if shown in ACTIVE:
                out["active"] += 1
            elif run.state["status"] == "failed" or shown == "stalled":
                out["failed"] += 1
        except Exception:
            continue
    return out


def in_flight(ctx: RepoContext) -> bool:
    """Whether a runner holds a live lock on any run of `ctx`: in the runs directory it was built with (`ctx.runs`),
    never the one its pack names now, and in `state.runs_root` only for a context with none. True when the runs
    can't be read at all, so a repository is never dropped on a transient error. The lock is checked without
    loading the run, so a run whose log is corrupt still counts when its runner is alive."""
    try:
        root = ctx.runs if ctx.runs is not None else state.runs_root(ctx.repo)
        paths = sorted(root.iterdir()) if root.is_dir() else []
    except Exception:
        return True
    for path in paths:
        try:
            if (path / "events.jsonl").is_file() and lock_state(path) == "live":
                return True
        except Exception:
            continue
    return False


def _resolved(path: Path) -> Path | None:
    try:
        return path.resolve()
    except Exception:
        return None


def _own_default(ctx: RepoContext) -> Path | None:
    """The runs directory `ctx` has when its pack sets no `paths.state_dir`."""
    try:
        return _resolved(pack.user_state_root() / ctx.id / "runs")
    except Exception:
        return None


def _inside(path: Path, folder: Path | None) -> bool:
    return folder is not None and path.is_relative_to(folder)


def _refusal(ctx: RepoContext, holders: dict[Path, list[RepoContext]], state_root: Path | None) -> str | None:
    """Why the live workspace context `ctx` may not use its runs directory, or None when it may."""
    if ctx.runs is None:
        return "its state directory (paths.state_dir) cannot be resolved"
    sharing = holders.get(ctx.runs, [])
    if len(sharing) > 1:
        owner = next((c for c in sharing if _own_default(c) == ctx.runs), None)
        if owner is None:
            others = sorted(c.name for c in sharing if c is not ctx)
            return f"its state directory is shared with {', '.join(others)}"
        if owner is not ctx:
            return f"its state directory belongs to {owner.name}"
    if _inside(ctx.runs, _resolved(ctx.repo)):
        return "its state directory is inside the repository"
    if state_root is None:
        return "the agentflow state folder cannot be resolved"
    if _inside(ctx.runs, state_root) and ctx.runs != _own_default(ctx):
        return "its state directory is in the agentflow state folder but is not this repository's own"
    return None


def settle_dirs(contexts: list[RepoContext]) -> list[RepoContext]:
    """`contexts` in the same order, each live one that may not use its runs directory replaced by an `error` copy
    (a context is never changed in place). A directory several contexts hold stays with the one whose default it
    is, and with none when it is nobody's default; `missing` contexts hold theirs but are never refused here. A
    live context's directory may not lie inside its own checkout, nor in the agentflow state folder unless it is
    its own default there. Order-independent."""
    holders: dict[Path, list[RepoContext]] = {}
    for ctx in contexts:
        if ctx.state in ("live", "missing") and ctx.runs is not None:
            holders.setdefault(ctx.runs, []).append(ctx)
    try:
        state_root = _resolved(pack.user_state_root())
    except Exception:
        state_root = None
    out = []
    for ctx in contexts:
        reason = _refusal(ctx, holders, state_root) if ctx.state == "live" else None
        out.append(ctx if reason is None else _failed(ctx, reason))
    return out


def reconcile(current: dict[str, RepoContext], found: RepoSet,
              busy: Callable[[RepoContext], bool] = in_flight) -> dict[str, RepoContext]:
    """The next map, in `found`'s order: a live repository whose live context is still usable keeps that object;
    every other found repository is loaded again. A live or missing context of `current` that `found` no longer
    holds follows as a `missing` copy while `busy` says a runner is in it, and is dropped otherwise (an `error` or
    `not_set_up` one is dropped without asking); nothing else is read for it. The
    runs-directory rule (`settle_dirs`) is applied over the found and the missing contexts together."""
    built: list[RepoContext] = []
    for ref in found.repos:
        old = current.get(ref.id)
        if ref.state == "live" and old is not None and old.state == "live" and usable(old):
            built.append(old if old.name == ref.name else replace(old, name=ref.name))
        else:
            built.append(load_context(ref))
    ids = {ref.id for ref in found.repos}
    # only a context that was served (live, or already missing) can be kept: a refused or unset-up one never had a
    # runs directory of its own, so a lock found there (a clone can ship one) keeps nothing
    missing = [replace(ctx, state="missing") for key, ctx in current.items()
               if key not in ids and ctx.state in ("live", "missing") and busy(ctx)]
    return {ctx.id: ctx for ctx in settle_dirs(built + missing)}


def repos_view(contexts: Iterable[RepoContext], skipped: Iterable[Skipped], default: str | None,
               user_error: str | None) -> dict:
    """The `/api/repos` body. No checkout path is in it: a `skipped` entry is the setting as written, and an
    `error` text may name a file, both for the token holder."""
    rows = []
    for ctx in contexts:
        live = ctx.state == "live" and ctx.cfg is not None
        rows.append({
            "id": ctx.id, "name": ctx.name, "state": ctx.state,
            "pack_version": ctx.cfg.version if live else None,
            **counts(ctx),
            "error": ctx.error if ctx.state == "error" else None,
        })
    return {
        "repos": rows,
        "skipped": [{"entry": s.entry, "reason": s.reason} for s in skipped],
        "default": default,
        "user_config_error": user_error,
    }


def engine_user_settings() -> layers.UserSettings:
    """The user settings of the engine defaults alone, for when the user file can't be read: no workspace, the
    defaults' `dashboard` block, and no file."""
    merged, _ = layers.merge(layers.base_layers(None))
    dashboard = merged.get("dashboard")
    return layers.UserSettings(layers.WorkspaceSettings((), (), ()),
                               copy.deepcopy(dashboard) if isinstance(dashboard, dict) else {}, None)
