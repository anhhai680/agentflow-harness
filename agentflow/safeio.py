"""File writes that never follow a symlink, for files the runner writes where someone else may have planted one.

`write_text` and `open_append` open the final path component with `O_NOFOLLOW`, so a symlink there raises
`SymlinkRefused` (an `OSError`) instead of writing through it; `ensure_dir` refuses a symlinked directory.
`replace_file` (used for the agent workspace, where the agent may plant anything) removes whatever is at
the path first and creates the file new and exclusive. `read_bytes` reads a regular file without
following a symlink, up to a size limit.
"""

from __future__ import annotations

import errno
import os
import stat
from pathlib import Path
from typing import TextIO

FILE_MODE = 0o600


class SymlinkRefused(OSError):
    """The runner will not write through (or read via) a symlink at this path."""


def _refused(path: Path, exc: OSError | None = None) -> SymlinkRefused:
    err = SymlinkRefused(errno.ELOOP, f"refusing to follow a symlink at {path}")
    if exc is not None:
        err.__cause__ = exc
    return err


def ensure_dir(path: Path) -> Path:
    """Create `path` (and its parents) as real directories; `SymlinkRefused` if `path` is a symlink."""
    path = Path(path)
    if path.is_symlink():
        raise _refused(path)
    path.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise _refused(path)
    return path


def _open(path: Path, flags: int) -> int:
    try:
        return os.open(path, flags | os.O_NOFOLLOW, FILE_MODE)
    except OSError as exc:
        if exc.errno == errno.ELOOP or path.is_symlink():
            raise _refused(path, exc) from exc
        raise


def write_text(path: Path, text: str) -> Path:
    """Write `text` to `path` from scratch (truncating), refusing a symlink at `path` or its folder."""
    path = Path(path)
    ensure_dir(path.parent)
    fd = _open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    return path


def open_append(path: Path) -> TextIO:
    """`path` opened for appending text (its `name` is the path), refusing a symlink at `path` or its folder."""
    path = Path(path)
    ensure_dir(path.parent)
    return open(path, "a", encoding="utf-8", opener=lambda name, flags: _open(Path(name), flags))


def replace_file(path: Path, data: bytes) -> None:
    """Put `data` at `path` as a new regular file, whatever was there (a symlink, a file, a folder)."""
    path = Path(path)
    remove(path)
    fd = _open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)


def remove(path: Path) -> None:
    """Remove whatever is at `path` without following it (a folder goes with its contents)."""
    path = Path(path)
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return
    if stat.S_ISDIR(info.st_mode):
        for child in path.iterdir():
            remove(child)
        path.rmdir()
    else:
        path.unlink()


def read_bytes(path: Path, limit: int) -> bytes | None:
    """The bytes of the regular file at `path`, or None when it is missing. `SymlinkRefused` for a symlink;
    `ValueError` when it is not a regular file or is larger than `limit`."""
    path = Path(path)
    try:
        fd = _open(path, os.O_RDONLY)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f"{path} is not a regular file")
        if info.st_size > limit:
            raise ValueError(f"{path} is larger than {limit} bytes")
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f"{path} is larger than {limit} bytes")
    return data
