"""Keep the machine from sleeping while a runner works, best effort.

A run left alone for hours is the point of the runner, and a laptop that sleeps mid-gate stops it until someone
wakes the machine: the gate's clock stops with it, so the lost hours show in no gate's `duration_s`. While
`pipeline.advance` holds the run's lock, `holding()` keeps an inhibitor alive:

- macOS: `caffeinate -i -w <runner pid>` (idle sleep only; the lid and the power button still win);
- Linux with systemd: `systemd-inhibit --what=idle:sleep --mode=block`, around a `tail --pid=<runner pid>` that
  ends with the runner;
- anything else: nothing.

Either way the inhibitor ends with the runner's process, even one killed with SIGKILL, and `holding()` ends it as
soon as the runner stops (for a human, or at the end of the run), so a run that waits for a person does not keep the
machine awake. A missing program, or one that cannot start, never fails a run. `AGENTFLOW_NO_KEEPAWAKE=1` turns it
off (the test suite sets it).
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import sys
from collections.abc import Iterator

OFF_ENV = "AGENTFLOW_NO_KEEPAWAKE"
WHY = "an agentflow run is working"


def inhibitor_argv(pid: int, platform: str | None = None) -> list[str] | None:
    """The command that keeps the machine awake until process `pid` ends, or None where there is none."""
    platform = sys.platform if platform is None else platform
    if platform == "darwin":
        caffeinate = shutil.which("caffeinate")
        return [caffeinate, "-i", "-w", str(pid)] if caffeinate else None
    if platform.startswith("linux"):
        inhibit, tail = shutil.which("systemd-inhibit"), shutil.which("tail")
        if inhibit and tail:
            return [inhibit, "--what=idle:sleep", "--who=agentflow", f"--why={WHY}", "--mode=block",
                    tail, f"--pid={pid}", "-f", "/dev/null"]
    return None


def describe(platform: str | None = None) -> str | None:
    """What `holding()` would use here (for `agentflow doctor`), or None."""
    argv = inhibitor_argv(os.getpid(), platform)
    return os.path.basename(argv[0]) if argv else None


@contextlib.contextmanager
def holding() -> Iterator[None]:
    """Keep the machine awake for the `with` block; never raises for the inhibitor's sake."""
    proc: subprocess.Popen | None = None
    if os.environ.get(OFF_ENV, "") not in ("1", "true", "yes"):
        argv = inhibitor_argv(os.getpid())
        if argv:
            with contextlib.suppress(OSError, ValueError):
                proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        yield
    finally:
        if proc is not None:
            with contextlib.suppress(OSError):
                proc.terminate()
            with contextlib.suppress(OSError, subprocess.TimeoutExpired):
                proc.wait(timeout=5)
