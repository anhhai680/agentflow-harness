"""The environment a run's agents and gates inherit (`defaults.env_inherit`).

An IDE or a shell often loads a repository's `.env` into the process that starts agentflow, and a variable
leaked that way can reach a gate or an agent session that was never meant to see it. `base` is this process's
environment without those variables; `defaults.env` is laid on top by the caller, so it always wins.

A name is dropped when it matches a `drop` glob, or when its inherited value equals that name's value in a
`drop_dotenv` file of the main checkout (the user set a different value on purpose, so that one stays). A
few names a process cannot do without are never dropped. The dotenv files are untrusted input: they are read
without following a symlink, cut at `MAX_DOTENV_BYTES`, and parsed in one linear pass. `dropped` returns
names and reasons only, never a value.
"""

from __future__ import annotations

import fnmatch
import os
import re
import stat
import subprocess
from collections.abc import Mapping
from pathlib import Path

from agentflow import pack
from agentflow.config import Config

__all__ = ["MAX_DOTENV_BYTES", "NEVER_DROPPED", "base", "dropped"]

MAX_DOTENV_BYTES = 256 * 1024
MAX_LINE_CHARS = 8192  # a longer line is no variable a shell would have loaded
NEVER_DROPPED = frozenset({"PATH", "HOME", "USER", "LOGNAME", "SHELL", "TMPDIR", "LANG", "TERM"})
_NEVER_PREFIXES = ("LC_",)
_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")
_EXPORT = re.compile(r"export[ \t]+")
_COMMENT = re.compile(r"[ \t]#")
_CACHE_SIZE = 32
_PARSED: dict[tuple[str, int, int], dict[str, str]] = {}


def _protected(name: str) -> bool:
    return name in NEVER_DROPPED or name.startswith(_NEVER_PREFIXES)


def _quoted(text: str) -> str | None:
    """The value inside the quote `text` opens (the quote is `text[0]`); None when it is never closed."""
    quote = text[0]
    out: list[str] = []
    i, n = 1, len(text)
    while i < n:
        ch = text[i]
        if ch == quote:
            return "".join(out)
        if ch == "\\" and quote == '"' and i + 1 < n and text[i + 1] in '"\\n':
            out.append("\n" if text[i + 1] == "n" else text[i + 1])
            i += 2
            continue
        out.append(ch)
        i += 1
    return None


def _line(raw: str) -> tuple[str, str] | None:
    if len(raw) > MAX_LINE_CHARS:
        return None
    text = raw.strip()
    if not text or text.startswith("#"):
        return None
    export = _EXPORT.match(text)
    if export:
        text = text[export.end():]
    key, sep, value = text.partition("=")
    key = key.strip()
    if not sep or not _KEY.fullmatch(key):
        return None
    value = value.lstrip()
    if value[:1] in ("'", '"'):
        inner = _quoted(value)
        return None if inner is None else (key, inner)
    comment = _COMMENT.search(value)
    return key, (value[:comment.start()] if comment else value).rstrip()


def _regular_file(path: Path, root: Path) -> bool:
    """True when `path` (below `root`) is a regular file and no part of it, the folders included, is a symlink."""
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        return False
    current = root
    for index, part in enumerate(parts):
        current = current / part
        try:
            mode = os.lstat(current).st_mode
        except OSError:
            return False
        if stat.S_ISLNK(mode):
            return False
        if index == len(parts) - 1:
            return stat.S_ISREG(mode)
    return False


def _read(path: Path) -> bytes | None:
    """At most `MAX_DOTENV_BYTES` of a regular file, opened without following a symlink; None when it is bigger."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        data = os.read(fd, MAX_DOTENV_BYTES + 1)
        return data if len(data) <= MAX_DOTENV_BYTES else None
    except OSError:
        return None
    finally:
        os.close(fd)


def _dotenv(path: Path) -> dict[str, str]:
    """`KEY=VALUE` lines of a dotenv file (an optional `export `; single or double quotes; an unquoted value ends at
    ` #`). Empty when the file is missing, is not a regular file, is a symlink or is over `MAX_DOTENV_BYTES`."""
    data = _read(path)
    if data is None:
        return {}
    found: dict[str, str] = {}
    for raw in data.decode("utf-8", errors="replace").splitlines():
        row = _line(raw)
        if row is not None:
            found[row[0]] = row[1]
    return found


def _main_checkout(cfg: Config) -> Path:
    config_dir = Path(cfg.path).resolve().parent
    try:
        return pack.main_checkout(config_dir)
    except (subprocess.CalledProcessError, OSError):
        return config_dir.parent  # not a git checkout: the pack sits in `<repo>/.agentflow/`


def _cached(path: Path, root: Path) -> dict[str, str]:
    """The parsed dotenv file at `path`, cached on its mtime and size."""
    if not _regular_file(path, root):
        return {}
    try:
        info = os.lstat(path)
    except OSError:
        return {}
    key = (str(path), info.st_mtime_ns, info.st_size)
    if key not in _PARSED:
        if len(_PARSED) >= _CACHE_SIZE:
            _PARSED.pop(next(iter(_PARSED)))
        _PARSED[key] = _dotenv(path)
    return _PARSED[key]


def _safe_relative(rel: str) -> bool:
    return bool(rel) and not rel.startswith("/") and ".." not in Path(rel).parts and "\0" not in rel


def _scan(cfg: Config, environ: Mapping[str, str]) -> list[tuple[str, str]]:
    inherit = cfg.defaults.env_inherit
    root = _main_checkout(cfg)
    files = [(rel, _cached(root / rel, root)) for rel in inherit.drop_dotenv if _safe_relative(rel)]
    files = [(rel, values) for rel, values in files if values]
    out: list[tuple[str, str]] = []
    for name in sorted(environ):
        if _protected(name):
            continue
        pattern = next((p for p in inherit.drop if fnmatch.fnmatchcase(name, p)), None)
        if pattern is not None:
            out.append((name, f"matches drop pattern {pattern}"))
            continue
        same = next((rel for rel, values in files if name in values and values[name] == environ[name]), None)
        if same is not None:
            out.append((name, f"same value as {same}"))
    return out


def dropped(cfg: Config, environ: Mapping[str, str] | None = None) -> list[tuple[str, str]]:
    """`(name, reason)` for each variable of `environ` (default: this process's) that a run does not inherit,
    sorted by name. A name `defaults.env` sets is not listed: the pack's value replaces it anyway."""
    environ = os.environ if environ is None else environ
    return [row for row in _scan(cfg, environ) if row[0] not in cfg.defaults.env]


def base(cfg: Config, environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """`environ` (default: this process's) without the variables `dropped` names: what an agent session or a
    gate starts from, with `defaults.env` laid on top."""
    environ = os.environ if environ is None else environ
    gone = {name for name, _ in _scan(cfg, environ)}
    return {k: v for k, v in environ.items() if k not in gone}
