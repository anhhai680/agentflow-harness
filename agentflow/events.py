"""The append-only event log and the run lock.

`events.jsonl` is the one record of what happened in a run: one JSON object per line, each with
a `seq` (1, 2, 3 ...), an ISO `ts`, a `type` and a `by`. Writers take an advisory file lock, so the
CLI, the dashboard and the runner can append at the same time without interleaving or reusing a
`seq`. Strings are stored in Unicode NFC, so Vietnamese text compares equal wherever it came from.
"""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
import threading
import time
import unicodedata
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

LOCK_NAME = "runner.lock"
RESERVED_KEYS = ("seq", "ts", "type", "by")
EMPTY_LOCK_GRACE_S = 5


class EventError(Exception):
    """An event the reducer cannot apply (a missing or mistyped field). Nothing was written."""

    def __init__(self, event_type: str, message: str) -> None:
        super().__init__(f"event {event_type!r}: {message}")
        self.event_type = event_type
        self.message = message


def now_iso() -> str:
    """The current UTC time as an ISO 8601 string with millisecond precision."""
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def parse_iso(text: Any) -> datetime | None:
    """An event timestamp as an aware datetime (naive ones are taken as UTC); None when it is not one."""
    if not isinstance(text, str) or not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def nfc(value: Any) -> Any:
    """`value` with every string (and dict key) inside it in Unicode NFC."""
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, dict):
        return {nfc(k): nfc(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [nfc(v) for v in value]
    return value


def _parse(raw: bytes) -> dict | None:
    """One log line as a dict, or None for a blank or torn line."""
    raw = raw.strip()
    if not raw:
        return None
    try:
        event = json.loads(raw)
    except ValueError:
        return None
    return event if isinstance(event, dict) else None


class EventLog:
    """An append-only JSON-lines file. `append` is safe across threads and processes."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock_path = self.path.with_name(self.path.name + ".lock")
        self._thread_lock = threading.RLock()
        self._depth = 0
        self._fd: int | None = None

    @contextmanager
    def locked(self) -> Iterator[None]:
        """Hold the log's exclusive lock; re-entrant within one `EventLog`."""
        with self._thread_lock:
            if self._depth == 0:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self._fd = os.open(self._lock_path, os.O_CREAT | os.O_RDWR, 0o644)
                fcntl.flock(self._fd, fcntl.LOCK_EX)
            self._depth += 1
            try:
                yield
            finally:
                self._depth -= 1
                if self._depth == 0 and self._fd is not None:
                    fcntl.flock(self._fd, fcntl.LOCK_UN)
                    os.close(self._fd)
                    self._fd = None

    def last_seq(self) -> int:
        """The highest `seq` in the file, read from its tail (0 for an empty or missing log)."""
        if not self.path.exists():
            return 0
        size = self.path.stat().st_size
        chunk = 4096
        with self.path.open("rb") as handle:
            while True:
                start = max(0, size - chunk)
                handle.seek(start)
                data = handle.read(size - start)
                lines = data.split(b"\n")
                for raw in reversed(lines[1:] if start > 0 else lines):
                    event = _parse(raw)
                    if event is not None and isinstance(event.get("seq"), int):
                        return event["seq"]
                if start == 0:
                    return 0
                chunk *= 4

    @staticmethod
    def check_data(data: dict) -> None:
        """Refuse event data that uses a key the log owns (`seq`, `ts`, `type`, `by`)."""
        clash = [key for key in data if key in RESERVED_KEYS]
        if clash:
            raise ValueError(f"event data may not use the reserved keys {clash}")

    def append(self, type: str, by: str = "runner", **data: Any) -> dict:
        """Write one event and return it as stored (`seq` and `ts` added, strings in NFC)."""
        self.check_data(data)
        with self.locked():
            seq = self.last_seq() + 1
            event = {"seq": seq, "ts": now_iso(), "type": type, "by": by, **data}
            line = json.dumps(nfc(event), ensure_ascii=False, default=str)
            prefix = ""
            if self.path.exists() and self.path.stat().st_size:
                with self.path.open("rb") as handle:
                    handle.seek(-1, os.SEEK_END)
                    if handle.read(1) != b"\n":
                        prefix = "\n"  # a torn last line must not swallow the new event
            with self.path.open("a", encoding="utf-8") as out:
                out.write(prefix + line + "\n")
                out.flush()
                os.fsync(out.fileno())
        return json.loads(line)

    def read(self, after_seq: int = 0) -> list[dict]:
        """Every event with `seq > after_seq`, in order. Torn or blank lines are skipped."""
        if not self.path.exists():
            return []
        events = []
        with self.path.open("rb") as handle:
            for raw in handle:
                event = _parse(raw)
                if event is not None and isinstance(event.get("seq"), int) and event["seq"] > after_seq:
                    events.append(event)
        return events

    def follow(self, after_seq: int, stop: Callable[[], bool], poll_s: float = 0.5) -> Iterator[dict]:
        """Yield events with `seq > after_seq` as they are written, until `stop()` is true.

        `stop` is checked before each drain, so everything written before it turned true is
        still delivered.
        """
        offset = 0
        pending = b""
        while True:
            finished = stop()
            if self.path.exists():
                with self.path.open("rb") as handle:
                    handle.seek(offset)
                    chunk = handle.read()
                offset += len(chunk)
                *lines, pending = (pending + chunk).split(b"\n")
                for raw in lines:
                    event = _parse(raw)
                    if event is not None and isinstance(event.get("seq"), int) and event["seq"] > after_seq:
                        after_seq = event["seq"]
                        yield event
                if chunk and not finished:
                    continue
            if finished:
                return
            time.sleep(poll_s)


# --------------------------------------------------------------------------- run lock


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _read_pid(path: Path) -> int | None:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _holder_alive(path: Path) -> bool:
    """Whether the lock at `path` is held. A lock without a readable PID counts as held while it is fresh."""
    pid = _read_pid(path)
    if pid is not None:
        return _pid_alive(pid)
    try:
        return time.time() - path.stat().st_mtime < EMPTY_LOCK_GRACE_S
    except OSError:
        return False


class RunLock:
    """One runner per run: a file holding the runner's PID. A dead PID's lock is reclaimed."""

    def __init__(self, run_dir: Path) -> None:
        self.run_dir = Path(run_dir)
        self.path = self.run_dir / LOCK_NAME
        self._held = False

    def _create(self) -> bool:
        """Make the lock with its PID already inside: write a temp file, then `link` it into place."""
        fd, tmp = tempfile.mkstemp(dir=self.run_dir, prefix=LOCK_NAME + ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(str(os.getpid()))
            try:
                os.link(tmp, self.path)
            except FileExistsError:
                return False
            return True
        finally:
            Path(tmp).unlink(missing_ok=True)

    def acquire(self) -> bool:
        """Take the lock; False when a live process holds it."""
        self.run_dir.mkdir(parents=True, exist_ok=True)
        if self._create():
            self._held = True
            return True
        guard = os.open(self.run_dir / (LOCK_NAME + ".reclaim"), os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(guard, fcntl.LOCK_EX)  # two reclaimers must not delete each other's new lock
            if _holder_alive(self.path):
                return False
            self.path.unlink(missing_ok=True)
            self._held = self._create()
            return self._held
        finally:
            fcntl.flock(guard, fcntl.LOCK_UN)
            os.close(guard)

    def release(self) -> None:
        """Drop the lock if this object holds it; safe to call twice."""
        if self._held and _read_pid(self.path) == os.getpid():
            self.path.unlink(missing_ok=True)
        self._held = False


def lock_state(run_dir: Path) -> Literal["live", "stalled", "idle"]:
    """`live` when a running process holds the lock, `stalled` when its PID is dead, `idle` with no lock."""
    path = Path(run_dir) / LOCK_NAME
    if not path.exists():
        return "idle"
    return "live" if _holder_alive(path) else "stalled"
