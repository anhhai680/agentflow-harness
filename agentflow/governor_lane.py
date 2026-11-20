"""Reporting a session's usage window readings to the machine-wide file (slice C6).

`GovernorLane` is a mixin of `Executor`, like `SizingLane` and `RouteLane`. `_metered` wraps every agent call of a run
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
from typing import TYPE_CHECKING

from agentflow import governor

if TYPE_CHECKING:
    from agentflow.agents import AgentOutcome


class GovernorLane:
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
