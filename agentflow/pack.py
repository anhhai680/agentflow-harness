"""Where a pack is and what it is: the repository's `.agentflow/` directory, its format version and the engine
range it runs on, and its prompts, which resolve from the pack first, then from the engine's defaults.

A leaf module: it imports nothing from `agentflow`. The pack is the directory holding the config file; its
`prompts/` directory overrides the engine's `prompts/` file by file. The CLI reads the pack of the main checkout
only, never a linked worktree's copy, which is on another branch."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ENGINE_PROMPTS: Path = Path(__file__).parent / "prompts"
PACK_DIR = ".agentflow"
PACK_VERSION = 2  # the pack format this engine reads (`version:`)
ENGINE_VERSION = "2.0"  # what a pack's `engine:` range is checked against
ENGINE_FORBIDDEN = (".agentflow/**",)  # forbidden paths every pack gets, whatever it lists itself

_CLAUSE = re.compile(r"(>=|<=|==|>|<)\s*(\d+(?:\.\d+)*)")


class PackError(ValueError):
    """The repository has no usable pack, or a pack value this module checks cannot be read."""


def main_checkout(start: Path) -> Path:
    """The main checkout containing `start`, even when `start` is inside a linked worktree."""
    start = Path(start).resolve()
    out = subprocess.run(
        ["git", "rev-parse", "--git-common-dir"],
        cwd=start,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    return (start / out).resolve().parent


def pack_config_path(repo: Path) -> Path:
    """Where the pack's config of the checkout holding `repo` is, whether or not it exists."""
    return main_checkout(repo) / PACK_DIR / "config.yaml"


def pack_config(repo: Path) -> Path:
    """The pack's config of the main checkout holding `repo`; `PackError` when the repository has none."""
    path = pack_config_path(repo)
    if not path.is_file():
        raise PackError(f"no {PACK_DIR}/config.yaml in {path.parent.parent}: this repository has no agentflow pack")
    return path


def _version(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in text.split("."))


def engine_satisfies(spec: str, version: str = ENGINE_VERSION) -> bool:
    """Whether `version` is in `spec`: comma-separated clauses of `>=`, `>`, `<=`, `<`, `==` on dotted integers,
    all of which must hold. Raises `PackError` on a clause it can't parse."""
    if not isinstance(spec, str) or not spec.strip():
        raise PackError(f"engine range {spec!r} is empty: write clauses such as '>=2,<3'")
    have = _version(version)
    ok = True
    for clause in spec.split(","):
        match = _CLAUSE.fullmatch(clause.strip())
        if match is None:
            raise PackError(f"engine range {spec!r}: cannot read {clause.strip()!r}; "
                            "use >=, >, <=, < or == and a dotted number, comma-separated")
        op, number = match.groups()
        want = _version(number)
        width = max(len(have), len(want))
        a, b = have + (0,) * (width - len(have)), want + (0,) * (width - len(want))
        ok = ok and {">=": a >= b, ">": a > b, "<=": a <= b, "<": a < b, "==": a == b}[op]
    return ok


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
