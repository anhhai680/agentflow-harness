"""The pack's work source: where the modules `agentflow queue`, the Backlog and `modules_done` walk come from.

`work_source:` in the pack names a `kind` and a `path`. The one kind this engine reads is `capability_map`: a
Markdown table with `Module id` and `Depends on` columns, one row per module in build order (`sources.capability_map`,
`sources.add_module_row`). A pack without a work source has no modules: `items` is empty and `add_item` refuses.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from agentflow import sources

if TYPE_CHECKING:
    from agentflow.config import Config


def file(cfg: Config, repo: Path) -> Path | None:
    """The work source's file inside `repo`, or None when the pack has no work source."""
    return Path(repo) / cfg.work_source.path if cfg.work_source is not None else None


def items(cfg: Config, repo: Path) -> list[sources.ModuleNode]:
    """The modules in the work source, in order; `[]` when the pack has none or its file is missing."""
    path = file(cfg, repo)
    if cfg.work_source is None or path is None or not path.is_file():
        return []
    if cfg.work_source.kind == "capability_map":
        return sources.capability_map(path)
    raise ValueError(f"work_source: unknown kind {cfg.work_source.kind!r}")


def add_item(cfg: Config, repo: Path, template: dict[str, str], values: dict) -> None:
    """Add one module to the work source (`values`: `module`, `responsibility`, `deps`), filled from `template`."""
    path = file(cfg, repo)
    if cfg.work_source is None or path is None:
        raise ValueError("this pack has no work_source")
    if cfg.work_source.kind == "capability_map":
        sources.add_module_row(path, template, values)
        return
    raise ValueError(f"work_source: unknown kind {cfg.work_source.kind!r}")
