"""When a plan task's implementer resumes the previous task's session (a step's `carry`, cost proposal §3.3 L3a).

`decide` answers for one implement key, from the run's events and step rows: it reads no file and no clock. The caller
has already checked that the previous task ran in the same walk of the plan, was committed in round 0, and that no task
was held between the two; `decide` checks the rest, in `REASONS` order, and the first that fails is the answer:

- `no_session`: the previous implementer has no session to resume;
- `not_clean`: a `hitl_raised` or `step_parked` names the previous task or a key under it (a stop, a park, a resolver);
- `max_tasks`: the session already holds `max_tasks` tasks (its first, plus one per `session_carried` naming it);
- `rolled_over`: the previous implementer rolled over to a fresh session;
- `compacted`: the session's context was compacted;
- `context`: the session's last `agent_turn` was at `max_context_pct` of its context or above (no turn counts as 0);
- `risk`: the plan sizer flagged the next task `risk` (or `unsure`); a task with no valid answers is not `risk`;
- `effort`: the plan sizer put the next task above the session's effort, which a carried task would run at.

`recorded` gives the first decision already recorded for a key (`session_carried` or `carry_declined`), so a
restarted runner makes the same one.
"""

from __future__ import annotations

from agentflow import sizing

REASONS = ("no_session", "not_clean", "max_tasks", "rolled_over", "compacted", "context", "risk", "effort")
CARRIED = "carried"
DECISIONS = ("session_carried", "carry_declined")


def recorded(events: list[dict], key: str) -> tuple[str | None, str] | None:
    """The first `session_carried` / `carry_declined` for implement key `key`: `(from_key, "carried")`, or
    `(None, reason)`; None when there is none."""
    for event in events:
        if event["type"] in DECISIONS and event.get("key") == key:
            if event["type"] == "session_carried":
                return str(event.get("from")), CARRIED
            return None, str(event.get("reason"))
    return None


def decide(events: list[dict], steps: dict[str, dict], prev_tkey: str, key: str,
           max_tasks: int, max_context_pct: int | float) -> tuple[str | None, str]:
    """`(prev implement key, "carried")` or `(None, reason)` for implement key `key` carrying `prev_tkey`'s session;
    the first failing check in `REASONS` order wins."""
    prev = f"{prev_tkey}/implement"
    row = steps.get(prev) or {}
    session = row.get("session_id")
    if not session:
        return None, "no_session"
    if any(e["type"] in ("hitl_raised", "step_parked") and _under(str(e.get("key") or ""), prev_tkey)
           for e in events):
        return None, "not_clean"
    if tasks_in(events, session) >= max_tasks:
        return None, "max_tasks"
    if any(e["type"] == "rolled_over" and e.get("key") == prev for e in events):
        return None, "rolled_over"
    if any(e["type"] == "compacted" and e.get("session_id") == session for e in events):
        return None, "compacted"
    turns = [e for e in events if e["type"] == "agent_turn" and e.get("session_id") == session]
    if turns and float(turns[-1].get("context_pct") or 0.0) >= max_context_pct:
        return None, "context"
    sized = [e for e in events if e["type"] == "effort_decided" and e.get("key") == key]
    answers = sizing.parse_answers(sized[-1].get("answers")) if sized else None
    if answers is not None and sizing.flags(answers)["risk"]:
        return None, "risk"
    level, current = (sized[-1].get("level") if sized else None), row.get("effort")
    known = sizing.KNOWN_EFFORT_LEVELS
    if level in known and current in known and sizing.highest([level, current]) != current:
        return None, "effort"
    return prev, CARRIED


def tasks_in(events: list[dict], session: str) -> int:
    """How many plan tasks session `session` holds: its first, plus one per `session_carried` that names it."""
    return 1 + sum(1 for e in events if e["type"] == "session_carried" and e.get("session_id") == session)


def _under(key: str, tkey: str) -> bool:
    return key == tkey or key.startswith(tkey + "/")
