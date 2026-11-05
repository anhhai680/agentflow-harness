"""Where a pack is and what it is: the repository's `.agentflow/` directory, its format version and the engine
range it runs on, and its prompts, which resolve from the pack first, then from the engine's defaults.

A leaf module: it imports nothing from `agentflow`. The pack is the directory holding the config file; its
`prompts/` directory overrides the engine's `prompts/` file by file. The CLI reads the pack of the main checkout
only, never a linked worktree's copy, which is on another branch. The state dir (`state_dir`) holds the runs and
the agent workspaces, outside the repository unless the pack says otherwise."""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from pathlib import Path
from typing import Any

import yaml

ENGINE_PROMPTS: Path = Path(__file__).parent / "prompts"
PACK_DIR = ".agentflow"
PACK_VERSION = 2  # the pack format this engine reads (`version:`)
ENGINE_VERSION = "2.0"  # what a pack's `engine:` range is checked against
ENGINE_FORBIDDEN = (".agentflow/**",)  # forbidden paths every pack gets, whatever it lists itself
STATE_HOME = "~/.local/state"  # where the default state dir goes when `XDG_STATE_HOME` is unset

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


_CHECKOUTS: dict[str, Path] = {}  # a directory's main checkout, once git has named one: it does not move


def checkout_root(repo: Path) -> Path:
    """The main checkout holding `repo`, or `repo` itself when it is in no git repository (or doesn't exist).

    Every run path goes through here (`state.runs_root`), so a found checkout is cached by the resolved path;
    a miss is not, so a directory that becomes a repository later is looked up again."""
    key = str(Path(repo).resolve())
    if key not in _CHECKOUTS:
        try:
            _CHECKOUTS[key] = main_checkout(Path(key))
        except (subprocess.CalledProcessError, OSError):
            return Path(key)
    return _CHECKOUTS[key]


_STATE_DIR_CACHE: dict[tuple[str, int, int], Any] = {}


def _raw_state_dir(config: Path) -> Any:
    """The pack's `paths.state_dir` as written (None when unset, unreadable or no pack), cached by the file's
    path, modification time and size, so a rewritten pack is read again."""
    try:
        st = config.stat()
    except OSError:
        return None
    key = (str(config), st.st_mtime_ns, st.st_size)
    if key not in _STATE_DIR_CACHE:
        try:
            data = yaml.safe_load(config.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            data = None
        paths = data.get("paths") if isinstance(data, dict) else None
        _STATE_DIR_CACHE[key] = paths.get("state_dir") if isinstance(paths, dict) else None
    return _STATE_DIR_CACHE[key]


def repo_id(root: Path) -> str:
    """The default state dir's name for the main checkout `root`: its name and a hash of its path."""
    root = Path(root).resolve()
    return f"{root.name}-{hashlib.sha256(str(root).encode('utf-8')).hexdigest()[:8]}"


def resolve_state_dir(value: str, root: Path) -> Path:
    """A `paths.state_dir` value as a directory: `~` expanded, a relative value relative to the main checkout."""
    path = Path(value).expanduser()
    return (path if path.is_absolute() else Path(root) / path).resolve()


def state_dir(repo: Path) -> Path:
    """Where the runs and the agent workspaces of the repository holding `repo` live: the pack's `paths.state_dir`,
    else `${XDG_STATE_HOME:-~/.local/state}/agentflow/<repo-id>`. It reads the pack's YAML itself (no config load)
    and uses the default when the repository has no pack or the value is not a non-empty string."""
    root = checkout_root(repo)
    value = _raw_state_dir(root / PACK_DIR / "config.yaml")
    if isinstance(value, str) and value.strip():
        return resolve_state_dir(value, root)
    home = os.environ.get("XDG_STATE_HOME", "")
    base = Path(home) if home and Path(home).is_absolute() else Path(STATE_HOME).expanduser()
    return base / "agentflow" / repo_id(root)


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
