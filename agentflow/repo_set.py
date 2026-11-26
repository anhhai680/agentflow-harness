"""The repositories a workspace holds, found from the user's `workspace` settings.

A scan reads directory names and the existence of `.git` and `.agentflow/config.yaml`, and nothing inside a
repository: nothing is opened or run there. The one subprocess is the `git rev-parse --git-common-dir` that
`pack.checkout_root` starts to find a repository's main checkout."""

from __future__ import annotations

import os
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from . import layers, pack

MAX_CHILDREN = 2000  # the children of one root that a scan examines; the rest are ignored


@dataclass(frozen=True)
class RepoRef:
    id: str  # `pack.repo_id` of the main checkout: the name of its state dir
    name: str  # the folder name, or `name (parent folder)` when another repository has the same name
    path: Path  # the main checkout, resolved
    state: Literal["live", "not_set_up"]  # `live` when the checkout has a `.agentflow/config.yaml`


@dataclass(frozen=True)
class Skipped:
    entry: str  # the settings entry as written
    reason: str


@dataclass(frozen=True)
class RepoSet:
    settings: layers.WorkspaceSettings
    repos: tuple[RepoRef, ...]
    skipped: tuple[Skipped, ...]

    @property
    def live(self) -> tuple[RepoRef, ...]:
        return tuple(r for r in self.repos if r.state == "live")


class NoWorkspace(Exception):
    """The user's settings name no root and no repository."""


def _is_checkout(path: Path) -> bool:
    return (path / ".git").exists()  # a folder in a main checkout, a file in a linked worktree


def _children(root: Path, skipped: list[Skipped], entry: str) -> list[Path]:
    """The child folders of `root` that look like repositories: not hidden, not a symlink, holding `.git`."""
    try:
        with os.scandir(root) as scan:
            found = sorted(scan, key=lambda e: e.name)
    except OSError as exc:
        skipped.append(Skipped(entry, f"cannot be read: {exc.strerror or exc}"))
        return []
    if len(found) > MAX_CHILDREN:
        skipped.append(Skipped(entry, f"more than {MAX_CHILDREN} entries; the rest are ignored"))
        found = found[:MAX_CHILDREN]
    out: list[Path] = []
    for child in found:
        try:
            if child.name.startswith(".") or child.is_symlink() or not child.is_dir(follow_symlinks=False):
                continue
        except OSError:
            continue
        if _is_checkout(Path(child.path)):
            out.append(Path(child.path))
    return out


def _excluded(checkout: Path, exclude: tuple[str, ...]) -> bool:
    for entry in exclude:
        if "/" in entry or entry.startswith("~"):
            if Path(entry).expanduser().resolve() == checkout:
                return True
        elif entry == checkout.name:
            return True
    return False


def resolve(settings: layers.WorkspaceSettings) -> RepoSet:
    """The repositories of `settings`: each root's child repositories in name order, then the `repos` entries,
    each as its main checkout and once, with `exclude` applied last."""
    skipped: list[Skipped] = []
    candidates: list[Path] = []
    for entry in settings.roots:
        root = Path(entry).expanduser()
        if not root.is_dir():
            skipped.append(Skipped(entry, "is not a folder"))
            continue
        candidates.extend(_children(root, skipped, entry))
    for entry in settings.repos:
        path = Path(entry).expanduser()
        if path.is_dir() and _is_checkout(path):
            candidates.append(path)  # named by the developer, so a symlink is followed
        else:
            skipped.append(Skipped(entry, "is not a git checkout"))
    seen: set[Path] = set()
    checkouts: list[Path] = []
    for candidate in candidates:
        checkout = pack.checkout_root(candidate)
        if checkout in seen or _excluded(checkout, settings.exclude):
            seen.add(checkout)
            continue
        seen.add(checkout)
        checkouts.append(checkout)
    shared = Counter(c.name for c in checkouts)
    repos = tuple(
        RepoRef(
            pack.repo_id(c),
            f"{c.name} ({c.parent.name})" if shared[c.name] > 1 else c.name,
            c,
            "live" if (c / pack.PACK_DIR / "config.yaml").is_file() else "not_set_up",
        )
        for c in checkouts
    )
    return RepoSet(settings, repos, tuple(skipped))


def from_user_config() -> RepoSet:
    """The workspace of the user's config file; `NoWorkspace` when it names none, `layers.LayerError` when the
    file is unusable."""
    settings = layers.load_user_settings().workspace
    if not settings.roots and not settings.repos:
        raise NoWorkspace(f"no workspace: set workspace.roots in {layers.user_config_path()}")
    return resolve(settings)
