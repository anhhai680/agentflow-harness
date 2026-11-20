"""The usage window governor inside a run (slice C6): its holds, and the readings each session reports.

`GovernorLane` is a mixin of `Executor`, like `SizingLane` and `RouteLane`.

`govern` is the hold. The hard mark is checked before each fresh Claude session (`lanes._loop`, a sizer), and the soft
mark before each phase starts and before a new run's triage (`pipeline`). Above its mark the run emits `run_held`
(reason `usage window`), keeps a waiter entry in the file and polls every `governor.POLL_S`, until the window clears
and no ready run closer to finish waits ahead of it (finish first), a pause or cancel ends the wait, or `usage.governor`
is set to `off`; then `run_hold_ended`. `observe` never holds: it records `usage_governed` once per key, level and
`until`. `govern` fails open: any unexpected exception ends the hold and lets the run go on.

`_metered` wraps every agent call of a run
(a step's, a sizer's, a triage's): it registers the session in the usage window file before the call and, in a
`finally`, leaves it with the call's first and last reading per window. `_on_reading` is the `AgentCall.on_reading`
hook: it records the readings as they arrive, with one non-blocking lock try.

The file is a scheduling aid, and a call that is running is never worth more than it: every use of the file here is
wrapped in `except Exception`, including the account's lookup (`governor.account_of` may raise), so a broken file, a
held lock or a bad config skips the file and the call still runs. Readings are recorded whatever `usage.governor`
says: `off` means never hold, nothing more.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from agentflow import governor
from agentflow.flow_control import halted
from agentflow.hitl import notify_run

if TYPE_CHECKING:
    from agentflow.agents import AgentOutcome


def _iso(at: float) -> str:
    return datetime.fromtimestamp(at, UTC).isoformat(timespec="seconds")


class GovernorLane:
    def govern(self, key: str, mark: str, phase: str) -> bool:
        """Hold the run while the usage window governs at `mark` (`"soft"` or `"hard"`) or, with `finish_first`, a
        ready run closer to finish waits ahead of it. True to go on; False only when the run was paused or cancelled
        while held. Never raises, and never leaves a waiter entry or a `run_held` without its `run_hold_ended`."""
        run = self.run  # type: ignore[attr-defined]
        held = False
        window_file = None
        me = ""
        try:
            window_file = governor.WindowFile()
            me = str(run.dir)
            rank = governor.rank_of(list(self.cfg.phases), phase, self.plan_open_tasks())  # type: ignore[attr-defined]
            since = time.time()
            account = governor.account_of(self.cfg)  # type: ignore[attr-defined]
            usage = None
            last: governor.Verdict | None = None
            while True:
                now = time.time()
                data = window_file.read()
                windows = data["windows"].get(account, {})
                if not held and not windows and not data["waiting"]:
                    return True  # nothing in the file can govern or queue this run, whatever `usage` says
                usage = governor.current_usage(self.cfg, usage)  # type: ignore[attr-defined]
                mode = usage.governor
                if mode == "off":
                    return True
                if held and data["error"] and last is not None and last.until is not None and now < last.until:
                    v = last  # a file that can't be read never ends a hold early, nor holds past the reset
                else:
                    v = governor.verdict(usage, windows, now)
                last = v
                governs = v.level == "hard" or (mark == "soft" and v.level == "soft")
                if mode == "observe":
                    if governs:
                        self._observed(key, v)
                    return True
                queued = (bool(usage.finish_first) and (held or mark == "soft")
                          and bool(governor.ahead(data["waiting"], me, rank, since, now)))
                if not (governs or queued):
                    return True
                if not held:
                    run.emit("run_held", key=key, reason=governor.USAGE_HOLD, until=_iso(v.until or now),
                             level=v.level, window=v.window, utilization=v.utilization)
                    held = True
                    why = governor.describe(v, usage, now) if governs else "runs closer to finish go first"
                    cfg = self.cfg  # type: ignore[attr-defined]
                    notify_run(cfg, run, f"{key} is held: {why}; it carries on by itself.")
                window_file.wait(me, me, rank, since, not governs, now)
                if not self._governor_sleep():
                    return False
        except Exception:  # the governor fails open: never a runner_error
            return True
        finally:
            if held:
                try:
                    if window_file is not None:
                        window_file.unwait(me)
                    run.emit("run_hold_ended", key=key)
                except Exception:
                    pass

    def _governor_sleep(self) -> bool:
        """Sleep one `governor.POLL_S` against the wall clock; False as soon as a human paused or ended the run."""
        run = self.run  # type: ignore[attr-defined]
        until = time.time() + governor.POLL_S
        while time.time() < until:
            if halted(run.refresh()["status"]):
                return False
            time.sleep(min(governor.POLL_S, max(0.0, until - time.time())))
        return not halted(run.refresh()["status"])

    def _observed(self, key: str, v: governor.Verdict) -> None:
        """`observe`: record `usage_governed` when its key, level and `until` differ from the run's last one."""
        run = self.run  # type: ignore[attr-defined]
        until = _iso(v.until) if v.until is not None else None
        last = run.refresh().get("usage_governed") or {}
        if (last.get("key"), last.get("level"), last.get("until")) == (key, v.level, until):
            return
        run.emit("usage_governed", key=key, level=v.level, window=v.window, utilization=v.utilization, until=until)

    def _metered(self, run_call: Callable[[], AgentOutcome]) -> AgentOutcome:
        """`run_call()` between `enter_session` and `exit_session`. The file's failures are swallowed; `run_call`'s own
        exception passes through, after the session has left the file with no readings."""
        window_file = None
        ticket = None
        try:
            account = governor.account_of(self.cfg)  # type: ignore[attr-defined]
            window_file = governor.WindowFile()
            ticket = window_file.enter_session(str(self.run.id), account, time.time())  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            ticket = None
        outcome = None
        try:
            outcome = run_call()
            return outcome
        finally:
            if ticket is not None and window_file is not None:
                try:
                    first = getattr(outcome, "first_readings", None)
                    last = getattr(outcome, "last_readings", None)
                    window_file.exit_session(ticket, first if isinstance(first, dict) else {},
                                             last if isinstance(last, dict) else {}, time.time())
                except Exception:  # noqa: BLE001
                    pass

    def _on_reading(self, readings: dict) -> None:
        """`AgentCall.on_reading`: record `readings` under the run's account with one non-blocking lock try."""
        try:
            account = governor.account_of(self.cfg)  # type: ignore[attr-defined]
            governor.WindowFile().record(account, readings, time.time(), wait_s=0)
        except Exception:  # noqa: BLE001
            pass
