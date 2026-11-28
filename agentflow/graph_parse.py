"""The engine's side of parsing: start `graph_worker.py` as a child, limit it, and distrust whatever it returns.

Repository bytes are hostile, so the engine never parses them itself. `Parser` starts the worker in its own session,
in a fresh empty temp directory, with `worker_env()` and stderr to /dev/null, and sends it one file at a time over
the protocol `graph_worker` describes. It tries a `uv` mode first (`locked` when `download`, else `offline`) and
falls back to `stdlib` when the handshake does not arrive in time or the child exits before it; an `argv` override is
tried once and never falls back.

Everything the worker says is untrusted. The handshake must be exactly `ready`, a `pid` above 1 and a `languages` map
of known language names to short version strings (anything else there is no languages). A reply must arrive within
`PARSE_TIMEOUT_S`, fit in `REPLY_MAX_BYTES`, carry the request's `id`, and hold facts `graph_model.check_facts`
accepts for the language asked and the file's line count. While it waits the parent wakes every `WAKE_S` to check the
clock and, at most that often, the worker's resident size (`ps`, read only for the handshake's pid and only while it
is in the worker's process group). Any failure kills the whole group and is one note for that file:
`parser_timeout`, `parser_memory`, `parser_crashed` or `parser_reply`. The worker is restarted for the next file, up to
`MAX_RESTARTS` times, after which every file is `parser_unavailable` at once.

`Parser` is a context manager, and `close()` (idempotent) kills and reaps the child and removes its directory. There
is no `__del__`: whoever starts a parser closes it.

This module imports `graph_model` and the standard library only: never an extractor and never tree-sitter.
"""

from __future__ import annotations

import contextlib
import json
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from agentflow.graph_model import LANGUAGES, Facts, check_facts, empty

PARSE_TIMEOUT_S, MAX_RESTARTS, RSS_MAX_MB, REPLY_MAX_BYTES = 10, 20, 1024, 4 * 1024 * 1024
WORKER = Path(__file__).with_name("graph_worker.py")
MODES = ("locked", "offline", "stdlib")
HANDSHAKE_S = {"locked": 120.0, "offline": 20.0, "stdlib": 20.0}
DATA_MAX_BYTES = 16 * 1024 * 1024  # graph_worker refuses a larger request and exits; a larger file is `too_large`
WAKE_S = 0.5
PS_TIMEOUT_S = 2.0
THREAD_JOIN_S = 2.0

_HELLO_KEYS = frozenset(("ready", "pid", "languages"))
_REPLY_KEYS = frozenset(("id", "facts"))
_VERSION = re.compile(r"[A-Za-z0-9.+_-]{1,40}")
_DIGITS = re.compile(r"[0-9]{1,15}")
_EOF, _LONG = "eof", "long"  # what the reader thread puts after the last line: end of output, or a line over the cap


def worker_argv(mode: str) -> list[str] | None:
    """The command that starts the worker in `mode`, or None for a `uv` mode when `uv` is not on PATH."""
    if mode == "stdlib":
        return [sys.executable, "-I", "-S", str(WORKER)]
    if mode not in ("locked", "offline"):
        raise ValueError(f"unknown parser mode {mode!r}")
    uv = shutil.which("uv")
    if uv is None:
        return None
    offline = ["--offline"] if mode == "offline" else []
    return [uv, "run", "--quiet", "--locked", *offline, "--python", sys.executable, "--script", str(WORKER)]


def worker_env() -> dict[str, str]:
    """PATH, HOME, LANG=C.UTF-8, and UV_CACHE_DIR / XDG_CACHE_HOME when they are set; nothing else."""
    env = {"PATH": os.environ.get("PATH", os.defpath), "LANG": "C.UTF-8"}
    for key in ("HOME", "UV_CACHE_DIR", "XDG_CACHE_HOME"):
        if os.environ.get(key):
            env[key] = os.environ[key]
    return env


def rss_mb(pid: int, group: int) -> float | None:
    """The resident size of `pid` in MiB, or None when `ps` gives no number or `pid` is not in process group `group`."""
    try:
        out = subprocess.run(["ps", "-o", "rss=,pgid=", "-p", str(pid)], stdin=subprocess.DEVNULL,
                             capture_output=True, text=True, errors="replace", timeout=PS_TIMEOUT_S, env=worker_env())
    except (OSError, subprocess.SubprocessError):
        return None
    fields = out.stdout.split()
    if len(fields) != 2 or not all(_DIGITS.fullmatch(f) for f in fields) or int(fields[1]) != group:
        return None
    return int(fields[0]) / 1024


def _int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _languages(raw: object) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    for lang, version in raw.items():
        if lang not in LANGUAGES or not isinstance(version, str) or not _VERSION.fullmatch(version):
            return {}
    return dict(raw)


def _ready(line: bytes) -> tuple[int, dict[str, str]] | None:
    """The worker's pid and languages from its handshake line, or None when it is not a handshake."""
    try:
        hello = json.loads(line)
    except (ValueError, RecursionError):
        return None
    if not isinstance(hello, dict) or set(hello) != _HELLO_KEYS:
        return None
    if not _int(hello["ready"]) or hello["ready"] != 1 or not _int(hello["pid"]) or hello["pid"] <= 1:
        return None
    return hello["pid"], _languages(hello["languages"])


def _reply(line: bytes, rid: int, lang: str, lines: int) -> Facts | None:
    try:
        reply = json.loads(line)
        if not isinstance(reply, dict) or set(reply) != _REPLY_KEYS or not _int(reply["id"]) or reply["id"] != rid:
            return None
        return check_facts(reply["facts"], lang, lines)
    except Exception:  # whatever odd shape gets past `json` is a refused reply, never a raise
        return None


def _read_lines(stream, out: queue.Queue) -> None:
    """Put each line of `stream` on `out`, then `_EOF` (end, or a partial last line) or `_LONG` (a line over the cap)."""
    limit = REPLY_MAX_BYTES
    try:
        while True:
            line = stream.readline(limit + 1)
            if len(line) > limit:
                out.put(_LONG)
                return
            if not line.endswith(b"\n"):
                out.put(_EOF)
                return
            out.put(line)
    except (OSError, ValueError):
        out.put(_EOF)


def _write(stream, payload: bytes) -> None:
    try:
        stream.write(payload)
        stream.flush()
    except (OSError, ValueError):  # the worker died or stopped reading: the reader sees it
        pass


class _Worker:
    """One running worker: its process group, its directory, and the threads that read and write its pipes."""

    def __init__(self, argv: list[str]) -> None:
        self.cwd = tempfile.mkdtemp(prefix="agentflow-graph-")
        self.pid = 0
        self.lines: queue.Queue = queue.Queue()
        self.writer: threading.Thread | None = None
        try:
            self.proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                         cwd=self.cwd, env=worker_env(), start_new_session=True)
        except BaseException:
            shutil.rmtree(self.cwd, ignore_errors=True)
            raise
        self.reader = threading.Thread(target=_read_lines, args=(self.proc.stdout, self.lines), daemon=True)
        self.reader.start()

    def send(self, payload: bytes) -> None:
        """Write `payload` from a thread of its own, so a worker that stops reading cannot block the parent."""
        self.writer = threading.Thread(target=_write, args=(self.proc.stdin, payload), daemon=True)
        self.writer.start()

    def kill(self) -> None:
        with contextlib.suppress(ProcessLookupError, PermissionError):  # it may have ended on its own meanwhile
            os.killpg(self.proc.pid, signal.SIGKILL)
        self.proc.wait()
        if self.writer is not None:
            self.writer.join(THREAD_JOIN_S)
        if self.writer is None or not self.writer.is_alive():
            with contextlib.suppress(OSError):
                self.proc.stdin.close()
        self.reader.join(THREAD_JOIN_S)
        if not self.reader.is_alive():  # a reader still blocked holds the stream's lock: leave it to the daemon thread
            self.proc.stdout.close()
        shutil.rmtree(self.cwd, ignore_errors=True)


class Parser:
    """A kept worker process: `parse` sends it one file and returns facts that `check_facts` accepted, or a note."""

    def __init__(self, *, download: bool, argv: list[str] | None = None) -> None:
        self.mode = ""
        self.languages: dict[str, str] = {}
        self._worker: _Worker | None = None
        self._argv: list[str] | None = None
        self._timeout = HANDSHAKE_S["stdlib"]
        self._restarts = 0
        self._next_id = 0
        self._sampled = float("-inf")
        self._closed = False
        if argv is not None:
            mode = next((m for m in MODES if worker_argv(m) == list(argv)), "custom")
            tries = [(mode, list(argv))]
        else:
            tries = [("locked" if download else "offline", None), ("stdlib", None)]
        try:
            for mode, cmd in tries:
                cmd = cmd if cmd is not None else worker_argv(mode)
                timeout = HANDSHAKE_S.get(mode, HANDSHAKE_S["stdlib"])
                languages = None if cmd is None else self._start(cmd, timeout)
                if languages is not None:
                    self.mode, self.languages, self._argv, self._timeout = mode, languages, cmd, timeout
                    break
        except BaseException:
            self.close()
            raise

    def __enter__(self) -> Parser:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _start(self, argv: list[str], timeout: float) -> dict[str, str] | None:
        """Start a worker and wait for its handshake; its languages, or None (and nothing left running)."""
        try:
            worker = _Worker(argv)
        except OSError:
            return None
        deadline = time.monotonic() + timeout
        hello = None
        while (remaining := deadline - time.monotonic()) > 0:
            try:
                item = worker.lines.get(timeout=min(WAKE_S, remaining))
            except queue.Empty:
                continue
            hello = _ready(item) if isinstance(item, bytes) else None
            break  # one line, or the end of output: either way the handshake is decided
        if hello is None:
            worker.kill()
            return None
        worker.pid = hello[0]
        self._worker, self._sampled = worker, float("-inf")
        return hello[1]

    def _fail(self, lang: str, note: str) -> Facts:
        if self._worker is not None:
            self._worker.kill()
            self._worker = None
        return empty(lang, note)

    def _too_big(self) -> bool:
        """Whether the worker's resident size is over `RSS_MAX_MB`, sampled at most every `WAKE_S`."""
        now = time.monotonic()
        if now - self._sampled < WAKE_S or self._worker is None:
            return False
        self._sampled = now
        size = rss_mb(self._worker.pid, self._worker.proc.pid)
        return size is not None and size > RSS_MAX_MB

    def parse(self, path: str, lang: str, data: bytes) -> Facts:
        """The facts of one file, or `empty(lang, <note>)`; never raises."""
        if lang not in self.languages:
            return empty(lang, "no_grammar")
        if len(data) > DATA_MAX_BYTES:
            return empty(lang, "too_large")
        if self._worker is None:
            if self._closed or self._argv is None or self._restarts >= MAX_RESTARTS:
                return empty(lang, "parser_unavailable")
            self._restarts += 1
            if self._start(self._argv, self._timeout) is None:
                return empty(lang, "parser_unavailable")
        worker = self._worker
        self._next_id += 1
        rid = self._next_id
        head = json.dumps({"id": rid, "path": path, "lang": lang, "size": len(data)}, ensure_ascii=True)
        worker.send(head.encode("ascii") + b"\n" + data)
        deadline = time.monotonic() + PARSE_TIMEOUT_S
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return self._fail(lang, "parser_timeout")
            try:
                item = worker.lines.get(timeout=min(WAKE_S, remaining))
                break
            except queue.Empty:
                if self._too_big():
                    return self._fail(lang, "parser_memory")
        if not isinstance(item, bytes):
            return self._fail(lang, "parser_crashed" if item is _EOF else "parser_reply")
        if self._too_big():
            return self._fail(lang, "parser_memory")
        worker.writer.join(max(deadline - time.monotonic(), 0))
        if worker.writer.is_alive():  # it answered before it read the whole request
            return self._fail(lang, "parser_reply")
        facts = _reply(item, rid, lang, data.count(b"\n") + 1)
        return facts if facts is not None else self._fail(lang, "parser_reply")

    def close(self) -> None:
        """Kill and reap the worker and remove its directory; later calls do nothing, and later parses are unavailable."""
        self._closed = True
        if self._worker is not None:
            self._worker.kill()
            self._worker = None
