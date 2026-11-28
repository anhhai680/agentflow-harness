"""The knowledge graph's reads from git: one commit's tree, the difference of two commits, and blobs by id.

Every byte the graph indexes comes from a git object at a named commit, never from a working tree. The repository is
untrusted, so every call is `git --no-pager <HARDENED> -C <repo> ...` (no hook, no fsmonitor, no replace object),
with the caller's `GIT_*` variables removed and `GIT_TERMINAL_PROMPT=0`, `GIT_NO_LAZY_FETCH=1` and
`GIT_OPTIONAL_LOCKS=0` set, so git never prompts, never asks a promisor remote for a missing object and never writes
the index. Stdin is closed or fed only object ids, each call has `TIMEOUT_S`, and its output is counted as it streams
and refused past a cap, so a hostile tree cannot make the engine hold gigabytes.

An object id is used only when it is a full hex id; a branch name only when it cannot be read as an option. A path is
left out, and counted in one note per kind, when it is not UTF-8, holds a control character or is longer than
`PATH_MAX_BYTES`, so what reaches the store and the terminal is always a plain string. Nothing here raises: a git that
fails, times out or writes something unexpected is `None`.
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
import unicodedata
from collections import Counter
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

from agentflow.guards import HARDENED

TIMEOUT_S = 60
PATH_MAX_BYTES = 1000
LIST_MAX_BYTES = 256 * 1024 * 1024  # the output of one `ls-tree` or `diff-tree`
BATCH_MAX_BYTES = 64 * 1024 * 1024  # the blobs one `cat-file --batch` reads
_HEADER_BYTES = 200  # room for one `cat-file` header line, per object
_ID = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_ZERO = re.compile(r"0{40}|0{64}")
_MODE = re.compile(r"[0-7]{6}")
_BRANCH = re.compile(r"[A-Za-z0-9._][A-Za-z0-9._/+@-]{0,254}")  # a name that can't be read as an option
_LEFT_OUT = {
    "long": f"longer than {PATH_MAX_BYTES} bytes",
    "utf8": "not UTF-8",
    "control": "holding a control character",
}


@dataclass(frozen=True)
class Entry:
    path: str
    mode: str
    blob: str  # the object id: a blob, or a commit for a submodule


@dataclass(frozen=True)
class Change:
    path: str
    mode: str  # the new mode, or the old one for a deleted path
    old: str | None  # None for an added path
    new: str | None  # None for a deleted path


def _env() -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_TERMINAL_PROMPT="0", GIT_NO_LAZY_FETCH="1", GIT_OPTIONAL_LOCKS="0")
    return env


def _feed(stream, data: bytes) -> None:
    try:
        stream.write(data)
    except (OSError, ValueError):  # git ended or was killed: the reader sees it
        pass
    finally:
        try:
            stream.close()
        except (OSError, ValueError):
            pass


def _git(repo: Path, args: Sequence[str], *, feed: bytes | None = None, limit: int = LIST_MAX_BYTES) -> bytes | None:
    """stdout of `git --no-pager <HARDENED> -C <repo> <args>`, or None when git fails, times out or writes more than
    `limit` bytes. `feed` (object ids) is written from a thread, so a git that stops reading cannot block us."""
    argv = ["git", "--no-pager", *HARDENED, "-C", str(repo), *args]
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.PIPE if feed is not None else subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=_env())
    except (OSError, ValueError, TypeError):
        return None
    timer = threading.Timer(TIMEOUT_S, proc.kill)
    timer.daemon = True
    timer.start()
    writer = None
    if feed is not None:
        writer = threading.Thread(target=_feed, args=(proc.stdin, feed), daemon=True)
        writer.start()
    chunks: list[bytes] = []
    size = 0
    try:
        stream = proc.stdout
        while stream is not None and (chunk := stream.read1(65536)):
            size += len(chunk)
            if size > limit:
                return None
            chunks.append(chunk)
        if proc.wait() != 0:  # a kill by the timer is a negative exit code
            return None
    except OSError:
        return None
    finally:
        timer.cancel()
        if proc.poll() is None:
            proc.kill()
        if proc.stdout is not None:
            proc.stdout.close()
        proc.wait()
        if writer is not None:
            writer.join(1)
    return b"".join(chunks)


def _is_id(value: object) -> bool:
    return isinstance(value, str) and _ID.fullmatch(value) is not None


def _rev(repo: Path, name: str) -> str | None:
    out = _git(repo, ["rev-parse", "--verify", "--quiet", "--end-of-options", f"{name}^{{commit}}"], limit=4096)
    try:
        line = out.decode("ascii").strip() if out is not None else ""
    except UnicodeDecodeError:
        return None
    return line if _ID.fullmatch(line) else None


def base_commit(repo: Path, base_branch: str) -> str | None:
    """The full id of the commit `refs/remotes/origin/<base_branch>` names, else `refs/heads/<base_branch>`, or None.

    A name that could be read as an option or climbs (`..`) runs nothing."""
    if not isinstance(base_branch, str) or _BRANCH.fullmatch(base_branch) is None or ".." in base_branch:
        return None
    for ref in (f"refs/remotes/origin/{base_branch}", f"refs/heads/{base_branch}"):
        if (sha := _rev(repo, ref)) is not None:
            return sha
    return None


def is_commit(repo: Path, sha: str) -> bool:
    """Whether `sha` is the full id of a commit object in `repo`."""
    return _is_id(sha) and _rev(repo, sha) == sha


def _path(raw: bytes, left: Counter) -> str | None:
    """`raw` as a path, or None (counted by kind in `left`) when it is too long, not UTF-8 or holds a control character."""
    if len(raw) > PATH_MAX_BYTES:
        left["long"] += 1
        return None
    try:
        path = raw.decode("utf-8")
    except UnicodeDecodeError:
        left["utf8"] += 1
        return None
    if any(unicodedata.category(ch) == "Cc" for ch in path):
        left["control"] += 1
        return None
    return path


def _notes(left: Counter) -> list[str]:
    return [f"{left[kind]} path(s) left out: {text}" for kind, text in _LEFT_OUT.items() if left[kind]]


def listing(repo: Path, commit: str) -> tuple[list[Entry], list[str]] | None:
    """Every entry of the tree of `commit` (`ls-tree -r`: files, symlinks, submodules), and the notes for the names
    left out; None when git cannot list it."""
    if not _is_id(commit):
        return None
    out = _git(repo, ["ls-tree", "-r", "-z", "--full-tree", commit])
    if out is None:
        return None
    entries: list[Entry] = []
    left: Counter = Counter()
    for record in out.split(b"\0"):
        if not record:
            continue
        head, tab, raw = record.partition(b"\t")
        fields = head.split(b" ")
        if not tab or len(fields) != 3:
            return None
        try:
            mode, oid = fields[0].decode("ascii"), fields[2].decode("ascii")
        except UnicodeDecodeError:
            return None
        if _MODE.fullmatch(mode) is None or _ID.fullmatch(oid) is None:
            return None
        if (path := _path(raw, left)) is not None:
            entries.append(Entry(path, mode, oid))
    return entries, _notes(left)


def changes(repo: Path, old: str, new: str) -> tuple[list[Change], list[str]] | None:
    """The paths that differ between the trees of commits `old` and `new` (no rename detection), and the notes for
    the names left out; None when git cannot diff them (an unknown id among them)."""
    if not _is_id(old) or not _is_id(new):
        return None
    out = _git(repo, ["diff-tree", "-r", "-z", "--raw", "--no-renames", "--no-ext-diff", "--no-textconv", old, new])
    if out is None:
        return None
    parts = out.split(b"\0")
    if parts and parts[-1] == b"":
        parts.pop()
    if len(parts) % 2:
        return None
    found: list[Change] = []
    left: Counter = Counter()
    for head, raw in zip(parts[0::2], parts[1::2]):
        try:
            fields = head.decode("ascii").split(" ")
        except UnicodeDecodeError:
            return None
        if len(fields) != 5 or not fields[0].startswith(":"):
            return None
        old_mode, new_mode, old_id, new_id = fields[0][1:], fields[1], fields[2], fields[3]
        if not all(_MODE.fullmatch(m) for m in (old_mode, new_mode)) or not all(_ID.fullmatch(i) for i in (old_id, new_id)):
            return None
        if (path := _path(raw, left)) is None:
            continue
        gone, born = _ZERO.fullmatch(new_id) is not None, _ZERO.fullmatch(old_id) is not None
        found.append(Change(path, old_mode if gone else new_mode, None if born else old_id, None if gone else new_id))
    return found, _notes(left)


def _sizes(repo: Path, ids: Sequence[str]) -> dict[str, int]:
    """The size of each id in `ids` that names a blob (`cat-file --batch-check`); {} when git fails."""
    if not ids:
        return {}
    out = _git(repo, ["cat-file", "--batch-check"], feed="".join(f"{i}\n" for i in ids).encode("ascii"),
               limit=len(ids) * _HEADER_BYTES)
    sizes: dict[str, int] = {}
    for line in (out or b"").split(b"\n"):
        fields = line.decode("ascii", "replace").split(" ")
        if len(fields) == 3 and fields[1] == "blob" and _ID.fullmatch(fields[0]) and fields[2].isdigit():
            sizes[fields[0]] = int(fields[2])
    return sizes


def _batch(repo: Path, ids: Sequence[str], limit: int) -> dict[str, bytes]:
    """The bytes of each blob in `ids` at or under `limit` (`cat-file --batch`); {} when git fails or writes nonsense."""
    out = _git(repo, ["cat-file", "--batch"], feed="".join(f"{i}\n" for i in ids).encode("ascii"),
               limit=len(ids) * (_HEADER_BYTES + 1) + BATCH_MAX_BYTES + limit)
    if out is None:
        return {}
    found: dict[str, bytes] = {}
    at = 0
    for oid in ids:
        end = out.find(b"\n", at)
        if end < 0:
            return {}
        fields = out[at:end].decode("ascii", "replace").split(" ")
        at = end + 1
        if fields[0] != oid:
            return {}
        if len(fields) == 2 and fields[1] == "missing":
            continue
        if len(fields) != 3 or not fields[2].isdigit():
            return {}
        size = int(fields[2])
        data, at = out[at : at + size], at + size + 1
        if len(data) != size or out[at - 1 : at] != b"\n":
            return {}
        if fields[1] == "blob" and size <= limit:
            found[oid] = data
    return found


def _flush(repo: Path, run: Sequence[str], sizes: dict[str, int], limit: int) -> Iterator[tuple[str, bytes | None]]:
    readable = [oid for oid in run if 0 <= sizes.get(oid, -1) <= limit]
    got = _batch(repo, readable, limit) if readable else {}
    for oid in run:
        yield oid, got.get(oid)


def read_blobs(repo: Path, blobs: Sequence[str], limit: int) -> Iterator[tuple[str, bytes | None]]:
    """Each distinct id of `blobs`, in order, with its bytes; None for an id that is not a full hex id, names no blob,
    or is over `limit` bytes. Sizes come first from `cat-file --batch-check`, so a blob over `limit` is never read;
    the rest are read by `cat-file --batch` in runs of at most `BATCH_MAX_BYTES`."""
    ids = list(dict.fromkeys(blobs))
    sizes = _sizes(repo, [oid for oid in ids if _is_id(oid)]) if limit >= 0 else {}
    run: list[str] = []
    budget = 0
    for oid in ids:
        size = sizes.get(oid, -1)
        readable = 0 <= size <= limit
        if readable and run and budget + size > BATCH_MAX_BYTES:
            yield from _flush(repo, run, sizes, limit)
            run, budget = [], 0
        run.append(oid)
        budget += size if readable else 0
    if run:
        yield from _flush(repo, run, sizes, limit)
