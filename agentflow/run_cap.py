"""The machine-wide run cap (`workspace.max_active_runs`): at most that many runs work at once on this machine.

A run is active only while its runner runs a piece of work for it: an agent session (a step's, fresh or resumed, a
sizer's or a triage's) or a gate set (a step's gates, preflight, conformance, sync, the CI fix loop). Each piece takes
one slot, through a `working` block at the three take sites (`lanes` before each agent call, `sizing_lane._sizer_call`,
`gates.run_gates`), and gives it back when it ends. A wait on a person, an approval or CI, a pause, the end of the run
and the time between two pieces of work hold nothing. A gate set that resolves to no gate takes nothing.

The slot comes from the turn: `pipeline.advance` runs its loop inside `turn`, and `current()` is that turn. With no
turn (`agentflow init`'s baseline, the retro pipeline, a test calling an executor directly) nothing is capped. A turn
holds at most one slot: a take nested inside a held one just goes on.

A slot is an exclusive, non-blocking `flock` on `<user_state_root()>/run_cap/<n>.lock`, taken under the mutex
`run_cap/cap.lock`. Under the mutex `take` counts every held lock, whatever its number, and takes the lowest free
`n` below the cap only when fewer than the cap are held, so a cap lowered under busy runs admits no new work until
the excess has drained, and two takers never overshoot. The process dying drops its lock: a crash leaves nothing to
clean up.

Past the cap a run is held, never refused: `run_held` (reason `run cap`, key `<phase>/run_cap`, `until` the time the
hold began, since the hold has no end time: `state.OPEN_HOLDS`), one notification, and a check every `POLL_S`
seconds, which re-reads the cap from the user file, until a slot frees or the cap is removed or raised; then
`run_hold_ended`. A pause or cancel during the wait stops an agent session (`stop_on_halt`), while a gate set runs once
without a slot. Waiters are not ranked: the first check after a slot frees wins.

The cap is a scheduling aid, not a security boundary, and it fails open: a lock folder or file that can't be used (a
symlink, a hardlink, not a regular file, any `OSError`, a mutex not taken within `MUTEX_WAIT_S`) lets the piece of
work go on uncapped, said once per process on stderr. The cap is read only from the user file
(`layers.load_user_settings`, never a pack), and any failure reading it means no cap. Holding never raises into a run
and never leaks a lock or a `run_held` without its `run_hold_ended`.

Only `state`, `layers`, `pack` and `safeio` are imported at module level: `gates` imports this module, and
`flow_control` imports `gates`, so the halt rule is written out here (`_halted`) and `hitl` is imported inside the
hold.
"""

from __future__ import annotations

import contextlib
import contextvars
import errno
import fcntl
import os
import stat
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from agentflow import layers, pack, safeio, state

if TYPE_CHECKING:
    from agentflow.config import Config
    from agentflow.state import Run

CAP_DIR, MUTEX = "run_cap", "cap.lock"
CAP_STEP = "run_cap"  # the hold key's step: `<phase>/run_cap`
HOLD_REASON = state.OPEN_HOLDS[0]
POLL_S, MUTEX_WAIT_S = 10.0, 2.0
_FLAGS = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
_MODE = 0o600
_warned = False  # the first fail-open of the process says so on stderr; later ones stay silent


class _Unusable(Exception):
    """A lock folder or file the cap can't use: the piece of work goes on uncapped."""


def _halted(status: str | None) -> bool:
    """`flow_control.halted`'s rule, written out here because `flow_control` imports `gates`, which imports this."""
    return status in state.TERMINAL or status == "paused"


@dataclass
class Slot:
    number: int | None  # None: nothing is held (no cap, or the cap could not be enforced)
    fd: int | None

    def release(self) -> None:
        """Close the slot's descriptor, which drops its lock; once only, and never raises."""
        fd, self.fd = self.fd, None
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)


def limit() -> int | None:
    """The user file's `workspace.max_active_runs`; None with no key or on any error reading it."""
    try:
        cap = layers.load_user_settings().workspace.max_active_runs
    except Exception:  # noqa: BLE001 - a user file that can't be read caps nothing
        return None
    if isinstance(cap, bool) or not isinstance(cap, int) or not 1 <= cap <= layers.MAX_ACTIVE_RUNS:
        return None
    return cap


def _open(path: Path, create: bool) -> int | None:
    """A descriptor on the lock file `path`, never through a symlink and never blocking on a FIFO; None when it does not
    exist and `create` is False. `_Unusable` for a symlink, a hardlink or anything but a regular file."""
    try:
        fd = os.open(path, _FLAGS | (os.O_CREAT if create else 0), _MODE)
    except FileNotFoundError:
        if create:
            raise
        return None
    except OSError as exc:
        if exc.errno == errno.ELOOP or path.is_symlink():
            raise _Unusable(f"{path} is a symlink") from exc
        raise
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise _Unusable(f"{path} is not a regular file")
        if info.st_nlink != 1:
            raise _Unusable(f"{path} has more than one link")
    except BaseException:
        os.close(fd)
        raise
    return fd


def _try_lock(fd: int) -> bool:
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


def _lock_within(fd: int, wait_s: float) -> bool:
    deadline = time.monotonic() + wait_s
    while not _try_lock(fd):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.01)
    return True


def _fail_open(reason: str) -> Slot:
    global _warned
    if not _warned:
        _warned = True
        with contextlib.suppress(Exception):
            print(f"agentflow: the run cap can't be used ({reason}); runs are not capped", file=sys.stderr)
    return Slot(None, None)


def take(cap: int, root: Path | None = None) -> Slot | None:
    """The lowest free slot below `cap`, locked; None when `cap` or more locks are held, whatever their numbers. An
    unheld `Slot(None, None)` when the lock files can't be used (fail open). `root` defaults to
    `pack.user_state_root()`."""
    mutex: int | None = None
    try:
        folder = Path(root if root is not None else pack.user_state_root()) / CAP_DIR
        if folder.is_symlink():
            raise _Unusable(f"{folder} is a symlink")
        safeio.ensure_dir(folder)
        mutex = _open(folder / MUTEX, create=True)
        if not _lock_within(mutex, MUTEX_WAIT_S):
            raise _Unusable(f"{folder / MUTEX} was not free within {MUTEX_WAIT_S:g} s")
        busy: set[int] = set()
        for n in range(layers.MAX_ACTIVE_RUNS):  # every held lock counts, a number past a lowered cap too
            fd = _open(folder / f"{n}.lock", create=False)
            if fd is None:
                continue
            try:
                if not _try_lock(fd):
                    busy.add(n)
            finally:
                os.close(fd)  # drops the probe's lock: nobody else probes while the mutex is held
        if len(busy) >= cap:
            return None
        for n in range(cap):
            if n in busy:
                continue
            fd = _open(folder / f"{n}.lock", create=True)
            try:
                if _try_lock(fd):
                    slot, fd = Slot(n, fd), None
                    return slot
            finally:
                if fd is not None:
                    os.close(fd)  # locked by a holder that takes no mutex: not ours
        return None
    except Exception as exc:  # noqa: BLE001 - the cap fails open; every descriptor it took is closed
        return _fail_open(str(exc) or type(exc).__name__)
    finally:
        if mutex is not None:
            with contextlib.suppress(OSError):
                os.close(mutex)


class Turn:
    """One `advance` turn of a run: the slot it holds (at most one) and how deep the `working` blocks are."""

    def __init__(self, run: Run, cfg: Config) -> None:
        self.run = run
        self.cfg = cfg
        self.slot: Slot | None = None
        self.depth = 0

    def close(self) -> None:
        """Give back the slot the turn holds, if any; idempotent."""
        slot, self.slot = self.slot, None
        if slot is not None:
            slot.release()


_CURRENT: contextvars.ContextVar[Turn | None] = contextvars.ContextVar("agentflow_run_cap_turn", default=None)


@contextlib.contextmanager
def turn(run: Run, cfg: Config) -> Iterator[Turn]:
    """Make a new `Turn` of `run` the `current()` one for the block, and close it on the way out, however it ends."""
    t = Turn(run, cfg)
    token = _CURRENT.set(t)
    try:
        yield t
    finally:
        _CURRENT.reset(token)
        t.close()


def current() -> Turn | None:
    return _CURRENT.get()


def _iso_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _notify(t: Turn, key: str, cap: int) -> None:
    try:
        from agentflow.hitl import notify_run  # inside: `hitl` reaches `flow_control`, which imports `gates`

        runs = f"{cap} run{'s' if cap != 1 else ''}"
        notify_run(t.cfg, t.run, f"{key} is held: {runs} already working on this machine "
                                 "(workspace.max_active_runs); it carries on by itself.")
    except Exception:  # noqa: BLE001 - telling a person never stops the run
        pass


def _hold(t: Turn, key: str, stop_on_halt: bool) -> bool:
    """Take a slot for `t`, holding the run past the cap until one frees. True to go on (holding, or uncapped); False
    only with `stop_on_halt`, when the run was paused or cancelled while held. Never raises."""
    run = t.run
    held = False
    try:
        cap = limit()
        if cap is None:
            return True
        slot = take(cap)
        if slot is not None:
            t.slot = slot
            return True
        run.emit("run_held", key=key, reason=HOLD_REASON, until=_iso_now(), cap=cap)
        held = True
        _notify(t, key, cap)
        while True:
            time.sleep(POLL_S)
            if _halted(run.refresh()["status"]):
                return not stop_on_halt  # a gate set runs once without a slot
            cap = limit()
            if cap is None:
                return True
            slot = take(cap)
            if slot is not None:
                t.slot = slot
                return True
    except Exception:  # noqa: BLE001 - the cap fails open: never a runner_error
        return True
    finally:
        if held:
            try:
                run.emit("run_hold_ended", key=key)
            except Exception:  # noqa: BLE001
                pass


@contextlib.contextmanager
def working(key: str, stop_on_halt: bool) -> Iterator[bool]:
    """Hold a slot of the current turn for the block. Yields True to go on (holding, or uncapped), False only with
    `stop_on_halt`, when the run was paused or cancelled while held. No turn: True, and nothing is taken. A block inside
    one that already works just goes on. The slot is given back when the outermost block ends, however it ends."""
    t = current()
    if t is None:
        yield True
        return
    go_on = True if t.depth > 0 else _hold(t, key, stop_on_halt)
    t.depth += 1
    try:
        yield go_on
    finally:
        t.depth -= 1
        if t.depth == 0:
            t.close()
