"""Where a pack's files come from: prompts resolve from the pack first, then from the engine's defaults.

A leaf module: it imports nothing from `agentflow`. The pack is the directory holding the config file; its
`prompts/` directory overrides the engine's `prompts/` file by file."""

from __future__ import annotations

from pathlib import Path

ENGINE_PROMPTS: Path = Path(__file__).parent / "prompts"


def prompt_path(config_path: Path, name: str) -> Path | None:
    """The file a prompt name resolves to: the pack's own, else the engine's default, else None."""
    own = config_path.parent / "prompts" / name
    if own.is_file():
        return own
    default = ENGINE_PROMPTS / name
    return default if default.is_file() else None


def prompt_files(config_path: Path) -> dict[str, Path]:
    """Every resolvable prompt name mapped to the file that wins."""
    found: dict[str, Path] = {}
    for root in (ENGINE_PROMPTS, config_path.parent / "prompts"):  # the pack comes last, so it replaces
        if root.is_dir():
            for file in sorted(root.glob("*.md")):
                if file.is_file():
                    found[file.name] = file
    return found


def prompt_label(config_path: Path, file: Path) -> str:
    """How a message names a prompt file: `prompts/<name>` for the pack's, `engine:prompts/<name>` for a default."""
    if file.parent == ENGINE_PROMPTS and file.parent != config_path.parent / "prompts":
        return f"engine:prompts/{file.name}"
    return f"prompts/{file.name}"
