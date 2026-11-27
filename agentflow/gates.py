"""Run the configured shell gates and summarise the results.

A gate is a named shell command from `config.yaml`. It passes when it exits 0. A gate whose
`when_exists` target is missing is reported **skipped**, never passed: the check it stands
for did not run. Each gate runs in its own process group with stdin closed; past
`defaults.gate_timeout_min` the whole group is killed and the gate fails ("timed out after N min"),
and the group is killed too whenever the runner leaves early. A missing `cwd` fails the gate (the
reason names the path). Gate logs are written fresh and never through a symlink (`safeio`). `gates_ok` treats skipped gates as not failing, so a missing optional code
root does not block a step, but the summary always shows them.

Groups (`run_gates`): gates with the same `group` run in set order in one thread, and the groups run at the same time; the
gates with no `group` form one group of their own, so a set with no `group` runs exactly as it always did, in order and
with no thread. A gate with a `resource` holds that name's lock while its command runs, on every path, so two gates
sharing one never overlap, in a run or across the runs of the repository (a file lock in the state dir's `locks/`);
the seconds it waited are `GateResult.waited_s`, kept out of `duration_s`. The results keep the set's order. However
`run_gates` is left, every gate's process group is killed. Inside an `advance` turn a set with at least one gate holds a
machine-wide `run_cap` slot (`workspace.max_active_runs`) while it runs; a pause or cancel during its wait for one
lets the set run once without it.

Doc checks (`run_doc_checks`) are the design lane's built-in gates, named in a step's `checks:`. They
read `git diff` from a base commit to the working tree as it is now (committed, staged, unstaged and
untracked changes, through a `guards.snapshot` tree, so the real index is untouched). Lines are placed
in a section by their line numbers in the hunk headers against the section headings, which come from
`git grep '^## '` on each side, so the idea doc is never parsed whole. A section runs from its `## `
heading to the next `## ` heading (`###` subsections stay inside it).

Reuse (`run_gates_reusing`): a gate that already passed in this run on exactly the tree about to be gated, with the
same command (`run`, `cwd`, `when_exists` and `defaults.env`, as the `cmd` digest on its `gate_result`), is not run
again: the earlier pass is the evidence, the rule `conformance` already holds the final tree to. A failure, a skip,
an event with no `tree` or no `cmd`, and the `setup` set's gates (they start things) are never reused.

Inputs: a gate with `inputs` globs is also reused across trees. Its `input_hash` (`hash_inputs`) covers its command
digest and the mode and blob of every file of the tree its globs match, untracked files included and ignored files
never (so a gate that reads an ignored file must not declare `inputs`). A fresh result carries it; a pass whose
`input_hash` equals the gate's hash on the tree about to be gated stands for it, and `conformance` accepts the same
pass as evidence on the final tree only when the hash there is equal too.

Tiers: a step with `gate_tier: affected` (the steps that fix things) runs each gate that has an `affected` command on
the files the step changed (`affected_gate`): its `paths` globs pick them, each is shell-quoted, and a gate with none
of them is skipped ("no affected paths"). Such a result is written with `tier: "affected"`, and it is never reused
and never floor evidence: the full set still runs on the final tree. A gate without `affected`, and one that would
take more than `MAX_AFFECTED_PATHS` paths or render a command over `MAX_AFFECTED_COMMAND_BYTES`, runs its whole `run`,
as an ordinary full-tier result; so does one whose `{paths}` is not its own unquoted word (`validate` refuses that).
"""

from __future__ import annotations

import contextlib
import fcntl
import fnmatch
import hashlib
import json
import math
import os
import posixpath
import re
import shlex
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from agentflow import pack, run_cap, run_env, safeio
from agentflow.config import GATE_LABEL, Config, Gate, unquoted_paths_word
from agentflow.guards import HARDENED, path_matches, snapshot

if TYPE_CHECKING:
    from agentflow.state import Run

KILL_WAIT_S = 5
# How long a gate that found its `resource` taken sleeps between two tries at the lock.
RESOURCE_POLL_S = 0.2
# Above this many matched paths an `affected` command would risk the argument limit: the gate runs its whole `run`.
MAX_AFFECTED_PATHS = 200
# A rendered `affected` command longer than this (bytes) runs the whole `run` instead: one argv string has a limit
# (about 128 KiB on Linux), and `Popen` would raise E2BIG rather than fail the gate.
MAX_AFFECTED_COMMAND_BYTES = 64 * 1024
_AFFECTED_FIELD = re.compile(r"\{\{|\}\}|\{paths\}")


@dataclass
class GateResult:
    name: str
    passed: bool
    skipped: bool
    reason: str
    log_path: Path | None
    tail: str
    duration_s: float = 0.0  # wall time the gate ran, for the run metrics
    cmd: str = ""  # `command_digest` of what ran; "" for a doc check
    reused_from: str | None = None  # the step key of the earlier pass this result stands for (`run_gates_reusing`)
    input_hash: str | None = None  # `hash_inputs` of the gate's `inputs` on the tree it ran on; None for a gate with none
    tier: str = "full"  # "affected": its `affected` command ran (or it skipped for no affected path); never evidence
    waited_s: float = 0.0  # seconds the gate waited for its `resource` lock (not in `duration_s`); 0 when it was free
    exit_code: int | None = None  # the command's exit code; None for a skip, a gate that didn't start, or a timeout


def _resolve(cfg: Config, names: str | list[str]) -> list[Gate]:
    """Turn a gate-set name or a list of gate (or gate-set) names into gates, in order."""
    wanted = cfg.gate_sets[names] if isinstance(names, str) else names
    if isinstance(names, str) and names not in cfg.gate_sets:
        raise KeyError(names)
    gates: list[Gate] = []
    for name in wanted:
        if name in cfg.gates:
            gates.append(cfg.gates[name])
        elif name in cfg.gate_sets:
            gates.extend(_resolve(cfg, name))
        else:
            raise KeyError(name)
    return gates


def resolve(cfg: Config, names: str | list[str]) -> list[Gate]:
    """The gates a step's `gates:` names, in order, without running them (for dry runs)."""
    return _resolve(cfg, names)


def agent_checks(cfg: Config, names: str | list[str] | None, worktree: Path) -> list[Gate]:
    """The gates a step's `gates:` names that the implementer may run itself: flagged `agent_check`, and not
    ones the runner would skip for a missing `when_exists` path in `worktree`. In order, each name once."""
    if not names:
        return []
    try:
        gates = _resolve(cfg, names)
    except KeyError:
        return []
    seen: set[str] = set()
    out: list[Gate] = []
    for gate in gates:
        if not gate.agent_check or gate.name in seen:
            continue
        if gate.when_exists and not (worktree / gate.when_exists).exists():
            continue
        seen.add(gate.name)
        out.append(gate)
    return out


def _kill_group(proc: subprocess.Popen) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):  # it may have ended on its own meanwhile
        os.killpg(proc.pid, signal.SIGKILL)


class _Live:
    """The gate processes running now, so that leaving `run_gates` early can kill every group's, not one thread's."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._procs: set[subprocess.Popen] = set()
        self._closed = False

    def add(self, proc: subprocess.Popen) -> None:
        with self._lock:
            if self._closed:  # the runner is leaving: nothing new may start
                _kill_group(proc)
            else:
                self._procs.add(proc)

    def remove(self, proc: subprocess.Popen) -> None:
        with self._lock:
            self._procs.discard(proc)

    def kill_all(self) -> None:
        """Kill every process still running, and refuse to start more."""
        with self._lock:
            self._closed = True
            for proc in self._procs:
                _kill_group(proc)

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed


def _shell(command: str, cwd: Path, timeout_s: float, env: dict[str, str] | None = None,
           env_base: Mapping[str, str] | None = None, live: _Live | None = None) -> tuple[int | None, str]:
    """`(exit code, output)` of `command` run by the shell in its own process group; `(None, output)` when
    it was killed at `timeout_s`. The group is killed whenever this is left with it still running. The
    environment is `env_base` (default: this process's) with `env` on top. A `live` registry holds the process
    while it runs."""
    base = os.environ if env_base is None else env_base
    proc = subprocess.Popen(command, shell=True, cwd=cwd, env={**base, **(env or {})}, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                            errors="replace", start_new_session=True)
    if live is not None:
        live.add(proc)
    try:
        try:
            output, _ = proc.communicate(timeout=timeout_s)
            return proc.returncode, output or ""
        except subprocess.TimeoutExpired:
            _kill_group(proc)
            try:
                output, _ = proc.communicate(timeout=KILL_WAIT_S)
            except subprocess.TimeoutExpired:
                output = ""
            return None, output or ""
    finally:
        if proc.returncode is None:  # left early (timeout, an exception, Ctrl-C): take its children too
            _kill_group(proc)
        if live is not None:
            live.remove(proc)


def _minutes(value: float) -> str:
    return f"{value:g} min"


def command_digest(gate: Gate, env: Mapping[str, str] | None) -> str:
    """What makes two runs of `gate` the same check: its command, cwd, `when_exists` and the run's `defaults.env`."""
    raw = json.dumps([gate.run, gate.cwd or "", gate.when_exists or "", sorted((env or {}).items())])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def hash_inputs(entries: list[tuple[str, str, str]], patterns: list[str], cmd: str) -> str:
    """What a gate's result depends on: its command digest and the files its `inputs` globs match. Pure.

    `entries` are `(path, mode, blob)` of a tree. The hash is the sha256 (16 hex characters) of `cmd`, then one
    `<mode> <blob> <path>` record (ended by NUL, which no path holds) for each entry that matches any pattern, sorted by path."""
    digest = hashlib.sha256(f"{cmd}\n".encode())
    for path, mode, blob in sorted(entries):
        if any(path_matches(path, pattern) for pattern in patterns):
            digest.update(f"{mode} {blob} {path}\0".encode())  # NUL ends a record: a path never holds one
    return digest.hexdigest()[:16]


def input_hashes(cfg: Config, gates: list[Gate], cwd: Path, tree: str) -> dict[str, str]:
    """Gate name -> `hash_inputs` on `tree`, for each of `gates` that declares `inputs` (the others are absent).

    One `git ls-tree` reads the tree, and nothing else is read: the files are those of the tree, untracked ones
    included (a `guards.snapshot` holds them), ignored ones never. When git cannot read the tree no gate has a
    hash, so nothing is reused or accepted by input."""
    declared = [g for g in gates if g.inputs]
    if not declared or not tree:
        return {}
    proc = subprocess.run(["git", *HARDENED, "ls-tree", "-r", "-z", "--full-tree", tree], cwd=cwd,
                          capture_output=True, text=True, encoding="utf-8", errors="surrogateescape", check=False)
    if proc.returncode != 0:
        return {}
    entries: list[tuple[str, str, str]] = []
    for line in proc.stdout.split("\0"):
        meta, tab, path = line.partition("\t")
        fields = meta.split()
        if tab and len(fields) == 3:
            entries.append((path, fields[0], fields[2]))
    env = cfg.defaults.env
    return {g.name: hash_inputs(entries, g.inputs, command_digest(g, env)) for g in declared}


def _affected_paths(gate: Gate, changed: Sequence[str], cwd: Path) -> list[str]:
    """The paths of `changed` that `gate.affected.paths` match and that exist inside the worktree `cwd`, made relative
    to `gate.cwd` when it has one (the ones outside it are dropped). In order, each once.

    A path is repository file names, so it is untrusted: an empty one, an absolute one, one with a `..` part or a
    control character (NUL, a newline, any other below 0x20, and DEL), and one that resolves outside the worktree
    (through a symlink) are never passed on."""
    if gate.affected is None:
        return []
    root = Path(cwd).resolve()
    prefix = posixpath.normpath(gate.cwd) if gate.cwd else "."
    out: list[str] = []
    for raw in dict.fromkeys(changed):
        if (not isinstance(raw, str) or not raw or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in raw)
                or posixpath.isabs(raw) or ".." in raw.split("/")):
            continue
        path = posixpath.normpath(raw)
        if not any(path_matches(path, pattern) for pattern in gate.affected.paths):
            continue
        try:
            target = root / path
            if not target.exists() or not target.resolve().is_relative_to(root):
                continue  # deleted, or a symlink out of the worktree
        except (OSError, ValueError):
            continue
        if prefix != ".":
            if not path.startswith(f"{prefix}/"):
                continue
            path = path[len(prefix) + 1:]
        out.append(path)
    return out


def _render_affected(template: str, paths: list[str]) -> str:
    """`template` with `{paths}` replaced by `paths`, each one shell-quoted (and `./`-prefixed when it starts with
    `-`, so no file name reads as an option), joined by spaces. `{{` and `}}` are literal braces; nothing else is a
    placeholder, and the replaced text is never read again."""
    args = " ".join(shlex.quote(f"./{p}" if p.startswith("-") else p) for p in paths)
    return _AFFECTED_FIELD.sub(lambda m: {"{{": "{", "}}": "}"}.get(m.group(0), args), template)


def affected_gate(gate: Gate, changed: Sequence[str], cwd: Path) -> Gate | None:
    """`gate` with its `affected` command, rendered with the paths of `changed` it takes (`_affected_paths`), as its
    `run`; None when the gate has no `affected`, its `{paths}` is not its own unquoted word, or no path is left."""
    if gate.affected is None or not unquoted_paths_word(gate.affected.run):
        return None
    paths = _affected_paths(gate, changed, cwd)
    if not paths:
        return None
    return replace(gate, run=_render_affected(gate.affected.run, paths))


def _tiered(gate: Gate, tier: str, changed: Sequence[str], cwd: Path) -> tuple[Gate | GateResult, str]:
    """What runs for `gate` at `tier`, and the result's tier: the gate itself at the full tier (no `affected`, the full
    tier asked, a template whose `{paths}` is not its own unquoted word (`validate` refuses it too), more than
    `MAX_AFFECTED_PATHS` paths, or a rendered command over `MAX_AFFECTED_COMMAND_BYTES`), a skip when no changed path
    is the gate's, or its rendered `affected` command."""
    if tier != "affected" or gate.affected is None or not unquoted_paths_word(gate.affected.run):
        return gate, "full"
    paths = _affected_paths(gate, changed, cwd)
    if len(paths) > MAX_AFFECTED_PATHS:
        return gate, "full"
    if not paths:
        return GateResult(gate.name, False, True, "no affected paths", None, "", tier="affected"), "affected"
    run = _render_affected(gate.affected.run, paths)
    if len(run.encode("utf-8", "surrogateescape")) > MAX_AFFECTED_COMMAND_BYTES:
        return gate, "full"
    return replace(gate, run=run), "affected"


def deferred_full(events: list[dict]) -> bool:
    """True when some `gate_result` ran at the affected tier: the full set is still owed on the final tree."""
    return any(e.get("type") == "gate_result" and e.get("tier") == "affected" for e in events)


def _run_one(gate: Gate, cwd: Path, log_dir: Path, tail_lines: int, timeout_min: float,
             env: dict[str, str] | None = None, env_base: Mapping[str, str] | None = None,
             live: _Live | None = None) -> GateResult:
    started = time.monotonic()
    result = _run_gate(gate, cwd, log_dir, tail_lines, timeout_min, env, env_base, live)
    return replace(result, duration_s=round(time.monotonic() - started, 3), cmd=command_digest(gate, env))


def _run_gate(gate: Gate, cwd: Path, log_dir: Path, tail_lines: int, timeout_min: float,
              env: dict[str, str] | None, env_base: Mapping[str, str] | None = None,
              live: _Live | None = None) -> GateResult:
    if gate.when_exists and not (cwd / gate.when_exists).exists():
        return GateResult(gate.name, False, True, f"{gate.when_exists} missing", None, "")
    run_dir = cwd / gate.cwd if gate.cwd else cwd
    if not run_dir.is_dir():
        reason = f"cwd {gate.cwd} does not exist in {cwd}"
        return GateResult(gate.name, False, False, reason, None, f"The gate did not run: {reason}.")
    code, output = _shell(gate.run, run_dir, max(1.0, timeout_min * 60), env, env_base, live)
    if code is None:
        output += f"\n[agentflow: killed after {_minutes(timeout_min)}]\n"
    log_path = log_dir / f"{gate.name}.log"
    try:
        safeio.write_text(log_path, f"$ {gate.run}\n{output}")
    except OSError as exc:
        return GateResult(gate.name, False, False, f"cannot write its log: {exc}", None, output[-2000:])
    tail = "\n".join(output.splitlines()[-tail_lines:]) if tail_lines > 0 else ""
    if code is None:
        return GateResult(gate.name, False, False, f"timed out after {_minutes(timeout_min)}", log_path, tail)
    passed = code == 0
    return GateResult(gate.name, passed, False, "" if passed else f"exit {code}", log_path, tail, exit_code=code)


class _Left(Exception):
    """The run left while a gate waited for its resource: the group stops (`run_group`), and nothing ran."""


@contextlib.contextmanager
def resource_lock(cwd: Path, name: str, live: _Live | None = None) -> Iterator[float]:
    """Hold the repository's lock for the resource `name` (a file lock in the state dir's `locks/`, so it holds across
    runs and processes, and a crashed holder frees it) and yield the seconds spent waiting for it: `0.0` when the first
    try got it. Raises `_Left` once `live` is closed, whether it was still waiting or just got the lock, so a run that
    leaves never starts the gate's command. A `name` that is not a label (`GATE_LABEL`, as `validate` checks) raises
    `ValueError` before anything is made or opened, so it never names a path outside `locks/`."""
    if not isinstance(name, str) or GATE_LABEL.fullmatch(name) is None:
        raise ValueError(f"resource name {name!r} is not a label: letters, digits, '_', '.' or '-', 1 to 40 of them")
    root = safeio.ensure_dir(pack.state_dir(cwd))
    locks = root / "locks"
    locks.mkdir(mode=0o700, exist_ok=True)  # the mode, for a new one; `ensure_dir` refuses a symlink there
    safeio.ensure_dir(locks)
    # O_NONBLOCK: a FIFO planted at the path must not hang the open (it means nothing for a regular file, and the
    # flock below still polls)
    fd = os.open(locks / f"{name}.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        waited_from: float | None = None
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if live is not None and live.closed:
                    raise _Left from None
                if waited_from is None:
                    waited_from = time.monotonic()
                time.sleep(RESOURCE_POLL_S)
        if live is not None and live.closed:  # the run left while this waited, and the lock came free first
            raise _Left
        yield 0.0 if waited_from is None else time.monotonic() - waited_from
    finally:
        os.close(fd)  # closing the descriptor drops the lock


def _collect(futures: list[Future]) -> None:
    """Wait for every future, then re-raise the first exception (in order). `run_gates` leaves through here."""
    wait(futures)
    for future in futures:
        future.result()


def run_gates(cfg: Config, names: str | list[str], cwd: Path, log_dir: Path, *,
              env_base: Mapping[str, str] | None = None, tier: str = "full",
              changed: Sequence[str] = ()) -> list[GateResult]:
    """Run gates (all of them, even after a failure) with `cwd` as the worktree root.

    A `str` names a `gate_sets` entry. Each gate's full output goes to `<log_dir>/<gate>.log`
    after a `$ <command>` line; `tail` keeps the last `context.gate_output_lines` lines. Each gate
    gets `defaults.gate_timeout_min`. Its environment is `env_base` (default: `run_env.base`, this process's
    environment without the variables a run does not inherit) with `defaults.env` on top.

    The gates with no `group` form one group of their own. One group runs in set order, in this thread. With
    several, each group runs in its own thread (in set order inside it). A gate with a `resource` holds that name's
    lock (`resource_lock`, across runs) while its command runs, on either path. The results are in set order. Leaving
    early (an exception, Ctrl-C) kills every gate's process group, whichever thread started it.

    At `tier="affected"` a gate with `affected` runs that command on the paths of `changed` it takes (`_tiered`), and
    its result has `tier="affected"`; a gate without one runs its `run`, at the full tier.

    Inside an `advance` turn a set that resolves to at least one gate holds a `run_cap` slot while it runs; past the
    cap it waits, and a pause or cancel during the wait lets this one set run without a slot.
    """
    cwd, log_dir = Path(cwd), Path(log_dir)
    if env_base is None:
        env_base = run_env.base(cfg)
    gates = _resolve(cfg, names)
    if not gates:  # a set with no gate is no work: it takes no slot
        return []
    turn = run_cap.current()
    phase = (turn.run.state.get("phase") if turn is not None else None) or "run"
    with run_cap.working(f"{phase}/{run_cap.CAP_STEP}", stop_on_halt=False):  # a pause ends the wait: the set runs
        return _run_resolved(cfg, gates, cwd, log_dir, env_base, tier, changed)


def _run_resolved(cfg: Config, gates: list[Gate], cwd: Path, log_dir: Path, env_base: Mapping[str, str], tier: str,
                  changed: Sequence[str]) -> list[GateResult]:
    """`run_gates`' work on the gates its names resolved to."""
    timeout = cfg.defaults.gate_timeout_min
    jobs = [_tiered(g, tier, changed, cwd) for g in gates]
    groups: dict[str | None, list[int]] = {}
    for i, gate in enumerate(gates):
        groups.setdefault(gate.group, []).append(i)

    def run(i: int, live: _Live | None = None) -> GateResult:
        job, job_tier = jobs[i]
        if isinstance(job, GateResult):  # nothing to run at this tier
            return job
        args = (job, cwd, log_dir, cfg.context.gate_output_lines, timeout, cfg.defaults.env, env_base, live)
        if not job.resource:
            return replace(_run_one(*args), tier=job_tier)
        with resource_lock(cwd, job.resource, live) as waited:  # only the command is held, never `_tiered`'s choice
            result = _run_one(*args)
        return replace(result, tier=job_tier, waited_s=round(waited, 3))

    if len(groups) <= 1:  # no `group` anywhere, or all in one: today's plain loop
        return [run(i) for i in range(len(gates))]
    live = _Live()
    results: dict[int, GateResult] = {}

    def run_group(indexes: list[int]) -> None:
        for i in indexes:
            if live.closed:
                return
            try:
                results[i] = run(i, live)
            except _Left:  # the run is leaving, so `run_gates` raises and no result is read
                return

    pool = ThreadPoolExecutor(max_workers=len(groups), thread_name_prefix="gates")
    try:
        _collect([pool.submit(run_group, indexes) for indexes in groups.values()])
    finally:
        live.kill_all()  # whatever still runs (an interrupt, a crashed thread), every group's
        pool.shutdown(wait=True)
    return [results[i] for i in range(len(gates))]


def setup_gate_names(cfg: Config) -> set[str]:
    """The gates of the `setup` set: they bring the environment up, so a pass of theirs is never reused."""
    try:
        return {g.name for g in _resolve(cfg, "setup")} if "setup" in cfg.gate_sets else set()
    except KeyError:
        return set()


def carried_trees(events: list[dict], tree: str | None) -> frozenset[str]:
    """The trees whose gate results count for `tree`: `tree` itself, plus the `before_tree` of every `backlog_updated`
    event with `change: "done"` whose `tree` is already in the set, followed to a fixpoint. Empty for no `tree`.

    Pure. The runner's `status: done` commit (`backlog_writes.mark_done`, in `finish`) changes one line of the backlog
    file and nothing a gate checks, so gate evidence carries across it. No other change carries: every other runner
    write to the backlog file happens before `verify`."""
    if not tree:
        return frozenset()
    edges = [(str(e["before_tree"]), str(e["tree"])) for e in events
             if e.get("type") == "backlog_updated" and e.get("change") == "done" and e.get("before_tree") and e.get("tree")]
    found = {tree}
    grew = True
    while grew:
        grew = False
        for before, after in edges:
            if after in found and before not in found:
                found.add(before)
                grew = True
    return frozenset(found)


def reusable_passes(events: list[dict], gates: list[Gate], tree: str | None, digests: Mapping[str, str],
                    never: set[str] | frozenset[str] = frozenset(),
                    hashes: Mapping[str, str] | None = None) -> dict[str, dict]:
    """Gate name -> the latest `gate_result` that passed (not skipped) on `tree` (or on a tree whose evidence carries
    to it, `carried_trees`) with the digest in `digests`, or, for a gate in `hashes` (its input hash on `tree`), whose
    `input_hash` equals that one, on whatever tree.

    Pure. A gate in `never`, an event with no `tree` or no `cmd` (older runs), a failure, a skip and an affected-tier
    result (`tier: "affected"`: it checked only the paths a step changed) never count."""
    if not tree:
        return {}
    wanted = {g.name for g in gates if g.name not in never and g.name in digests}
    hashes = hashes or {}
    trees = carried_trees(events, tree)
    found: dict[str, dict] = {}
    for e in events:
        if e.get("type") != "gate_result" or not e.get("passed") or e.get("skipped") or e.get("tier") == "affected":
            continue
        name = e.get("gate")
        if name not in wanted:
            continue
        same_tree = e.get("tree") in trees and e.get("cmd") and e.get("cmd") == digests[name]
        same_inputs = bool(e.get("input_hash")) and e.get("input_hash") == hashes.get(name)
        if same_tree or same_inputs:
            found[name] = e
    return found


def run_gates_reusing(cfg: Config, run: Run, key: str, names: str | list[str], cwd: Path, log_dir: Path,
                      tree: str | None, *, tier: str = "full", changed: Sequence[str] = ()) -> list[GateResult]:
    """`run_gates`, except that a gate with a reusable pass on `tree` (`reusable_passes`) is not run again.

    The results keep the gates' order; a reused one is a pass with `reused_from` set, its old log and no tail.
    One `gates_reused` event names them (`record_results` writes no `gate_result` for a reused result, so the
    run's evidence stays one event per gate actually run).

    A gate with `inputs` also reuses a pass from another tree when the files its `inputs` match are the same
    (`input_hashes`): its fresh result carries the `input_hash`, and a pass taken from another tree is told in a
    `gate_cache_hit` event.

    `tier` and `changed` go to `run_gates` for the gates not reused. Reuse comes first at either tier, and only a
    full-tier pass is ever reused; an affected-tier result carries no `input_hash`, so nothing takes it for the
    gate's whole check."""
    gates = _resolve(cfg, names)
    env = cfg.defaults.env
    digests = {g.name: command_digest(g, env) for g in gates}
    hashes = input_hashes(cfg, gates, cwd, tree) if tree else {}
    events = run.events.read()
    reused = reusable_passes(events, gates, tree, digests, setup_gate_names(cfg), hashes)
    trees = carried_trees(events, tree)
    fresh = iter(run_gates(cfg, [g.name for g in gates if g.name not in reused], cwd, log_dir, tier=tier,
                           changed=changed) if len(reused) < len(gates) else [])
    results: list[GateResult] = []
    for g in gates:
        e = reused.get(g.name)
        if e is None:
            result = next(fresh)
            full = result.tier != "affected"
            results.append(replace(result, input_hash=hashes[g.name]) if full and g.name in hashes else result)
            continue
        log = Path(e["log"]) if e.get("log") else None
        results.append(GateResult(g.name, True, False, f"reused: passed on this tree in {e.get('key')}", log, "",
                                  cmd=digests[g.name], reused_from=str(e.get("key")), input_hash=hashes.get(g.name)))
    for name, e in sorted(reused.items()):
        if e.get("tree") not in trees:  # a pass from another tree: the gate's inputs are what made it the same check
            run.emit("gate_cache_hit", key=key, gate=name, input_hash=hashes[name], **{"from": str(e.get("key"))})
    if reused:
        run.emit("gates_reused", key=key, tree=tree, gates=sorted(reused),
                 **{"from": sorted({str(e.get("key")) for e in reused.values()})})
    return results


GATE_TABLE_MAX_ROWS = 20  # rows of `results_table`; the older gates are counted in one line


def _cell(text: object) -> str:
    """One Markdown table cell: a newline becomes a space and `|` is escaped."""
    return " ".join(str(text).split("\n")).replace("\r", " ").replace("|", "\\|")


def results_table(events: list[dict]) -> str:
    """The gates the runner already ran, as a Markdown table for a read-only session's prompt; `""` with no
    `gate_result` event. Pure: one row per gate, its newest result (by event order), newest first, at most
    `GATE_TABLE_MAX_ROWS` rows, then `(<k> older gates not shown)`. An `affected`-tier result is marked: it checked
    only the files a step changed."""
    newest: dict[str, dict] = {}
    for e in events:
        if e.get("type") == "gate_result" and e.get("gate") is not None:
            newest.pop(str(e["gate"]), None)  # re-inserted last: the dict keeps event order of the newest results
            newest[str(e["gate"])] = e
    if not newest:
        return ""
    latest = list(reversed(newest.values()))
    rows = []
    for e in latest[:GATE_TABLE_MAX_ROWS]:
        if e.get("skipped"):
            result = f"skipped: {e.get('reason') or 'no reason given'}"
        else:
            result = "pass" if e.get("passed") else "FAIL"
            if e.get("tier") == "affected":
                result += " (affected files)"
        seconds = e.get("duration_s")
        finite = isinstance(seconds, (int, float)) and not isinstance(seconds, bool) and math.isfinite(seconds)
        took = f"{round(seconds)}s" if finite else "—"  # `round` raises on an infinity and a NaN
        cells = (e["gate"], result, took, e.get("key") or "—", e.get("log") or "—")
        rows.append("| " + " | ".join(_cell(c) for c in cells) + " |")
    lines = ["The runner already ran these gates on this run's work; each row is a gate's newest result.", "",
             "| Gate | Result | Time | Step | Log |", "|---|---|---|---|---|", *rows]
    if len(latest) > GATE_TABLE_MAX_ROWS:
        lines.append(f"({len(latest) - GATE_TABLE_MAX_ROWS} older gates not shown)")
    return "\n".join(lines)


def gates_ok(results: list[GateResult]) -> bool:
    """True when no gate that actually ran failed (skipped gates do not count as failures)."""
    return all(r.passed or r.skipped for r in results)


def record_results(run: Run, key: str, results: list[GateResult], tree: str, *, with_tail: bool = False) -> None:
    """Write one `gate_result` per result that ran, each stamped with the `tree` (a `guards.snapshot`) the gates ran
    on, the gate's `cmd` digest and, for a gate with `inputs`, its `input_hash` (a reused result writes none). An
    affected-tier result also writes `tier: "affected"`; a full one writes no `tier`.

    The caller snapshots the worktree before the gates run. `with_tail` adds a `tail` to every event: a failed
    gate's output tail (the fix loop's and the retro's evidence), `""` for a gate that passed or was skipped."""
    for r in results:
        if r.reused_from is not None:  # `run_gates_reusing` already said so in its `gates_reused` event
            continue
        data = {"key": key, "gate": r.name, "passed": r.passed, "skipped": r.skipped, "reason": r.reason,
                "log": str(r.log_path) if r.log_path else None, "duration_s": r.duration_s, "tree": tree}
        if r.cmd:
            data["cmd"] = r.cmd
        if r.input_hash is not None:
            data["input_hash"] = r.input_hash
        if r.waited_s > 0:  # only a gate that found its resource taken
            data["resource_wait_s"] = round(r.waited_s, 3)
        if r.tier == "affected":  # written only for these: a result with no `tier` is a full one, as it always was
            data["tier"] = "affected"
        if with_tail:  # "" for a gate that passed or was skipped
            data["tail"] = "" if (r.passed or r.skipped) else r.tail
        run.emit("gate_result", **data)


def failure_report(failed: list[GateResult]) -> str:
    """The stop report for failed gates: one `### <gate> (<reason>)` block each, with its log path and output tail."""
    blocks = []
    for r in failed:
        log = f"Full log: {r.log_path}\n" if r.log_path else ""
        blocks.append(f"### {r.name} ({r.reason})\n{log}```\n{r.tail}\n```")
    return "\n\n".join(blocks)


# A line that tells what failed, in the output of the usual test runners, linters and compilers.
FAILURE_LINE = re.compile(
    r"\b(?:FAILED|FAIL|ERROR|Error|error)\b|^E\s|AssertionError|Traceback|[\u2717\u2718]|^\s*-->\s|^[^\s:]+:\d+(?::\d+)?:\s")
EXCERPT_LINE_MAX = 300  # a kept line is cut to this many characters, then `\u2026`
EXCERPT_GATE_LINES = 10  # a gate that gets an excerpt gets at least this many lines, `max_lines` allowing


def _excerpt(tail: str, budget: int) -> tuple[list[str], int, int]:
    """`(lines, kept, total)`: at most `budget` lines of `tail` (`kept` of its `total`), plus the cut markers. A tail
    that fits is kept whole. A longer one keeps up to half the budget from its first failure line (none when no line
    matches), then its last lines for the rest, in the original order with no line twice, and `\u2026 <k> lines cut \u2026`
    where lines are skipped. Every kept line is cut to `EXCERPT_LINE_MAX` characters."""
    lines = tail.splitlines()
    total = len(lines)
    first = next((i for i, line in enumerate(lines) if FAILURE_LINE.search(line)), None)
    if total <= budget:
        keep = set(range(total))
    else:
        keep = set(range(first, min(total, first + budget // 2))) if first is not None else set()
        keep.update(range(total - (budget - len(keep)), total))
    shown: list[str] = []
    expected = 0
    for i in sorted(keep):
        if i > expected:
            shown.append(f"\u2026 {i - expected} lines cut \u2026")
        line = lines[i]
        shown.append(line[:EXCERPT_LINE_MAX] + "\u2026" if len(line) > EXCERPT_LINE_MAX else line)
        expected = i + 1
    return shown, len(keep), total


def resume_report(failed: list[GateResult], max_lines: int) -> str:
    """The gate output a fix turn carries: `failure_report` cut to `max_lines` kept lines in all.

    The first `max(1, max_lines // 10)` failed gates each get an excerpt of their tail (`_excerpt`): the budget split
    between them, at least `EXCERPT_GATE_LINES` each. Every other gate gets its header, its log path and a note to
    read the log. A block is `### <gate> (<reason>)`, `Full log: <path>` (when there is one), the kept lines in a
    fence and, when lines were cut, `(<k> of <m> lines shown; the full log has the rest)`."""
    shown = max(1, max_lines // 10)
    each = max(1, min(max_lines, max(EXCERPT_GATE_LINES, max_lines // min(len(failed) or 1, shown))))
    blocks = []
    for number, r in enumerate(failed):
        head = f"### {r.name} ({r.reason})\n" + (f"Full log: {r.log_path}\n" if r.log_path else "")
        if number >= shown:
            blocks.append(f"{head}(output not shown: read the log)")
            continue
        lines, kept, total = _excerpt(r.tail, each)
        note = f"\n({kept} of {total} lines shown; the full log has the rest)" if kept < total else ""
        blocks.append(f"{head}```\n" + "\n".join(lines) + f"\n```{note}")
    return "\n\n".join(blocks)


def summarize(results: list[GateResult]) -> str:
    """One line per gate: `✓ floor`, `✗ py_lint`, `– web_types (skipped: <reason>)`."""
    lines = []
    for r in results:
        if r.skipped:
            lines.append(f"– {r.name} (skipped: {r.reason})")
        elif r.passed:
            lines.append(f"✓ {r.name}")
        else:
            lines.append(f"✗ {r.name}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- doc checks

DECISIONS_LOG = "## Decisions Log"
OPEN_QUESTIONS = "## Open Questions"
HUNK = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")
TABLE_SEPARATOR = re.compile(r"^\|[\s|:-]*$")
ADR_FILE = "ADR-*.md"
EDITING_RULES = "the design doc's editing rules"
WORK_TREE = "the work tree"


@dataclass
class _DocDiff:
    """One run of the doc checks: the repo, the base commit and the snapshot tree of the work tree."""

    worktree: Path
    base: str
    tree: str
    idea: str
    decisions_dir: str

    def git(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["git", *args], cwd=self.worktree, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", check=False)

    def idea_lines(self) -> tuple[list[tuple[int, str]], list[tuple[int, str]]]:
        """`(removed, added)` lines of the idea doc as `(line number, text)`: old numbers, new numbers."""
        proc = self.git("diff-tree", "-p", "-U0", "--no-renames", "--no-color", "--no-ext-diff",
                        self.base, self.tree, "--", self.idea)
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip() or f"git diff-tree exited {proc.returncode}")
        removed: list[tuple[int, str]] = []
        added: list[tuple[int, str]] = []
        old = new = 0
        in_hunk = False
        for line in proc.stdout.splitlines():
            found = HUNK.match(line)
            if found:
                old, new, in_hunk = int(found.group(1)), int(found.group(2)), True
            elif in_hunk and line.startswith("-"):
                removed.append((old, line[1:]))
                old += 1
            elif in_hunk and line.startswith("+"):
                added.append((new, line[1:]))
                new += 1
        return removed, added

    def section(self, rev: str, heading: str) -> tuple[int, float] | None:
        """`(heading line, end line)` of the `heading` section of the idea doc at `rev` (end exclusive),
        or None when the doc or the heading is missing there."""
        if self.git("cat-file", "-e", f"{rev}:{self.idea}").returncode != 0:
            return None
        proc = self.git("grep", "-h", "-n", "-E", "^## ", rev, "--", self.idea)
        if proc.returncode not in (0, 1):
            raise RuntimeError(proc.stderr.strip() or f"git grep exited {proc.returncode}")
        headings = []
        for line in proc.stdout.splitlines():
            number, _, text = line.partition(":")
            headings.append((int(number), text.rstrip()))
        wanted = re.compile(rf"^{re.escape(heading)}\b")
        starts = [n for n, text in headings if wanted.match(text)]
        if not starts:
            return None
        start = starts[0]
        later = [n for n, _ in headings if n > start]
        return start, (later[0] if later else float("inf"))

    def changed_decisions(self) -> list[tuple[str, str]]:
        """`(status letter, path)` for each file under `decisions_dir` that differs from the base."""
        proc = self.git("diff-tree", "-r", "-z", "--name-status", "--no-renames", self.base, self.tree,
                        "--", self.decisions_dir)
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip() or f"git diff-tree exited {proc.returncode}")
        fields = [f for f in proc.stdout.split("\0") if f]
        return list(zip(fields[0::2], fields[1::2]))


def _inside(lines: list[tuple[int, str]], span: tuple[int, float]) -> list[str]:
    start, end = span
    return [text for number, text in lines if start < number < end]


def _result(name: str, passed: bool, reason: str, detail: str = "") -> GateResult:
    return GateResult(name, passed, False, reason, None, detail)


def _missing(name: str, d: _DocDiff, heading: str, where: str) -> GateResult:
    return _result(name, False, f"no `{heading}` section in {d.idea} in {where}",
                   f"{d.idea} has no `{heading}` heading in {where} (or the file is missing there), "
                   "so the runner cannot check it. Keep the idea doc and its sections in place.")


def _decisions_log_row(d: _DocDiff) -> GateResult:
    name = "decisions_log_row"
    span = d.section(d.tree, DECISIONS_LOG)
    if span is None:
        return _missing(name, d, DECISIONS_LOG, WORK_TREE)
    rows = [t for t in _inside(d.idea_lines()[1], span)
            if t.strip().startswith("|") and not TABLE_SEPARATOR.match(t.strip())]
    if rows:
        return _result(name, True, f"{len(rows)} row(s) added")
    return _result(name, False, f"no table row added under `{DECISIONS_LOG}`",
                   f"The diff from {d.base[:12]} adds no `| ... |` table row inside the `{DECISIONS_LOG}` "
                   f"section of {d.idea}. Add the decision's row there ({EDITING_RULES}).")


def _open_question_removed(d: _DocDiff) -> GateResult:
    name = "open_question_removed"
    span = d.section(d.base, OPEN_QUESTIONS)
    if span is None:
        return _missing(name, d, OPEN_QUESTIONS, f"base {d.base[:12]}")
    if d.section(d.tree, OPEN_QUESTIONS) is None:  # dropping the doc or the whole section resolves nothing
        return _missing(name, d, OPEN_QUESTIONS, WORK_TREE)
    gone = [t for t in _inside(d.idea_lines()[0], span) if t.strip()]
    if gone:
        return _result(name, True, f"{len(gone)} line(s) removed")
    return _result(name, False, f"nothing removed from `{OPEN_QUESTIONS}`",
                   f"The diff from {d.base[:12]} removes no line inside the `{OPEN_QUESTIONS}` section of "
                   f"{d.idea}. Remove the question the decision resolves ({EDITING_RULES}).")


def _adr_index_updated(d: _DocDiff) -> GateResult:
    name = "adr_index_updated"
    changed = d.changed_decisions()
    index = str(PurePosixPath(d.decisions_dir) / "README.md")
    new = [p for status, p in changed if status == "A" and str(PurePosixPath(p).parent) == d.decisions_dir
           and fnmatch.fnmatchcase(PurePosixPath(p).name, ADR_FILE)]
    if not new:
        return _result(name, True, "no new ADR")
    if any(p == index for _, p in changed):
        return _result(name, True, f"{len(new)} new ADR(s), index updated")
    return _result(name, False, f"new ADR without an index row: {', '.join(new)}",
                   f"The diff from {d.base[:12]} adds {', '.join(new)} but does not change {index}. "
                   f"Add the new ADR's row to the index (and the superseded ADR's status) ({EDITING_RULES}).")


# The names here are `config.DOC_CHECK_NAMES` (config cannot import this module); a test keeps them equal.
_DOC_CHECKS: dict[str, Callable[[_DocDiff], GateResult]] = {
    "decisions_log_row": _decisions_log_row,
    "open_question_removed": _open_question_removed,
    "adr_index_updated": _adr_index_updated,
}


def run_doc_checks(names: list[str], worktree: Path, base: str, idea: str, decisions_dir: str) -> list[GateResult]:
    """Run the named doc checks, in order, on the diff from `base` to the work tree as it is now.

    - `decisions_log_row`: the diff adds a `|` table row (not a `|---|` separator) inside `## Decisions Log`;
    - `open_question_removed`: the diff removes a non-blank line inside `## Open Questions`;
    - `adr_index_updated`: a new `<decisions_dir>/ADR-*.md` implies a changed `<decisions_dir>/README.md`
      (passes, as "no new ADR", when there is none).

    A missing doc or section, or a git error, fails the check with the reason. Each result is a
    `GateResult` (never skipped, no log file; `tail` tells the agent what to fix), so failures feed a
    step's fix loop like gate failures. Raises `ValueError` for an unknown check name.
    """
    unknown = [n for n in names if n not in _DOC_CHECKS]
    if unknown:
        raise ValueError(f"unknown doc check(s): {', '.join(unknown)}")
    worktree = Path(worktree)
    d = _DocDiff(worktree, base, snapshot(worktree), idea, decisions_dir.rstrip("/"))
    results = []
    for name in names:
        try:
            results.append(_DOC_CHECKS[name](d))
        except RuntimeError as exc:
            results.append(_result(name, False, f"git failed: {exc}", f"The runner could not read the diff: {exc}"))
    return results
