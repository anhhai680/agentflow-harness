"""A run's log told as sentences, filtered by kind, and cut into pages by `seq`.

Pure, like `signals.py`: every function takes the run's event list (the dashboard's route reads `events.jsonl`)
and reads no file and no clock.

- `KINDS`, `CATEGORY` and `in_kind(event, kind)`: the five kinds the Activity filter offers. A type that is in no
  kind (and is not a `hitl_*` or `phase_*` type) shows only under "all".
- `sentence(event)`: `(headline, detail)` for one event. Any event gets one, a missing field reads as "?", and it
  never raises.
- `activity_page(events, before, kinds, limit, turns)`: newest first, `limit` entries, with `before` (the cursor)
  being a `seq`. A cursor of `seq` is stable while the log grows: the entries older than it never change. A run of
  `agent_turn` events is folded from the whole log before paging (unless `turns`), so a page never splits one.
"""

from __future__ import annotations

import json
from typing import Any

from agentflow.events import parse_iso

KINDS = ("decisions", "agents", "gates", "commits", "stops")

_DECISIONS = (
    "checkpoint_cleared", "condition_decided", "condition_kept", "stop_classified", "effort_decided", "effort_escalated",
    "resolve_finished", "doubt_settled", "findings_reconciled", "plan_batched", "budget_scaled", "run_marked_done",
    "decision_recorded",
)
_BY_KIND = {
    "agents": (
        "agent_started", "agent_session", "agent_exited", "agent_turn", "subagent_started", "subagent_finished",
        "sizer_started", "sizer_finished", "plan_sized", "effort_changed", "rolled_over", "compacted", "context_window",
        "resolve_started",
    ),
    "gates": (
        "gate_result", "gate_cache_hit", "gates_reused", "gates_prerun", "gates_deferred", "fix_attempt",
        "preflight_checked", "conformance_checked", "expectation_checked", "review_result", "review_skipped",
        "step_findings",
    ),
    "commits": (
        "step_committed", "task_committed", "module_added", "pr_opened", "pr_updated", "pr_marked_ready",
        "early_pr_failed", "ci_watching", "ci_round", "base_synced", "sync_started", "lessons_committed",
    ),
    "stops": (
        "hitl_raised", "step_parked", "run_held", "run_hold_ended", "guard_tripped", "watchdog_fired", "runner_error",
        "environment_waiting", "permission_denied", "run_failed", "run_paused", "run_cancelled",
    ),
}
# An event type -> its kind. The decisions that are not by prefix are listed here, and `hitl_raised` (which the
# prefix makes a decision too) is a stop.
CATEGORY: dict[str, str] = {
    **{t: "decisions" for t in _DECISIONS},
    **{t: kind for kind, types in _BY_KIND.items() for t in types},
}

RAW_MAX = 4096


def in_kind(event: dict, kind: str) -> bool:
    """Whether `event` belongs to `kind`. `decisions` is by prefix (`hitl_*`, `phase_*`) first, then the list."""
    etype = event.get("type")
    etype = etype if isinstance(etype, str) else ""
    if kind == "decisions":
        return etype.startswith(("hitl_", "phase_")) or CATEGORY.get(etype) == "decisions"
    return CATEGORY.get(etype) == kind


def _text(value: Any) -> str:
    return "?" if value is None or value == "" else str(value)


def _detail(value: Any) -> str:
    return "" if value is None else str(value)


def _step(event: dict) -> str:
    """The step title of the event's `key` (or `step`), "?" when it has neither."""
    from agentflow.views import step_title  # views may read activity later, so it is imported here

    key = event.get("key") or event.get("step")
    return step_title(str(key)) if key else "?"


def _answers(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    return "; ".join(f"{q}: {a}" for q, a in value.items())


def _bool_word(value: Any) -> str:
    return "true" if value is True else "false" if value is False else "?"


def sentence(event: dict) -> tuple[str, str]:
    """`(headline, detail)` of one event. Never raises: a field that is missing reads as "?"."""
    try:
        return _sentence(event)
    except Exception:  # the log is data from disk: one odd event must not take the page down
        return (_text(event.get("type") if isinstance(event, dict) else None).replace("_", " "), "")


def _sentence(event: dict) -> tuple[str, str]:
    etype = event.get("type")
    who = "The runner" if event.get("by") == "runner" else "You"  # a missing `by` is not the runner
    step = _step(event)
    phase = _text(event.get("phase"))
    if etype == "step_finished":
        failed = event.get("status") == "failed"
        return f"{step} {'failed' if failed else 'finished'}", _detail(event.get("summary"))
    if etype == "step_started":
        return f"{step} started", ""
    if etype == "hitl_raised":
        return f"Stopped for you: {step} ({_text(event.get('kind'))})", _detail(event.get("summary"))
    if etype == "phase_approved":
        return f"{who} approved {phase}", ""
    if etype == "phase_rejected":
        return f"{who} sent {phase} back (round {_text(event.get('round'))})", _detail(event.get("note"))
    if etype == "phase_started":
        return f"{phase} started", ""
    if etype == "phase_approval_raised":
        return f"{phase} is done and waits for approval", _detail(event.get("summary"))
    if etype == "hitl_approved":
        return f"{who} approved {step}", _detail(event.get("note"))
    if etype == "hitl_answered":
        return f"{who} answered {step}", _answers(event.get("answers"))
    if etype == "checkpoint_cleared":
        return f"{who} cleared {step}", ""
    if etype == "gate_result":
        passed = event.get("passed")
        word = "skipped" if event.get("skipped") or passed not in (True, False) else "passed" if passed else "failed"
        return f"Gate {_text(event.get('gate'))} {word} on {step}", ""
    if etype == "step_committed":
        return f"Committed {step} ({_text(str(event.get('sha') or '')[:7])})", _detail(event.get("summary"))
    if etype == "task_committed":
        return f"Task {_text(event.get('n'))} committed ({_text(str(event.get('sha') or '')[:7])})", _detail(event.get("title"))
    if etype == "agent_started":
        return f"{step} agent started ({_text(event.get('model'))} · {_text(event.get('effort'))})", ""
    if etype == "agent_exited":
        return f"{step} agent exited {_text(event.get('exit_code'))}", ""
    if etype == "condition_decided":
        return f"The runner took {_text(event.get('condition'))} as {_bool_word(event.get('value'))} for {step}", ""
    if etype == "stop_classified":
        return f"The runner classified {step}'s stop as {_text(event.get('stop_class'))}", ""
    if etype == "resolve_finished":
        return f"Resolver for {step} finished", _detail(event.get("summary"))
    if etype == "run_started":
        return "Run started", ""
    if etype == "run_finished":
        return "Run finished", ""
    if etype == "pr_opened":
        return "Draft pull request opened", _detail(event.get("url"))
    if etype == "pr_marked_ready":
        return "Pull request marked ready", _detail(event.get("url"))
    words = _text(etype).replace("_", " ")
    headline = words[:1].upper() + words[1:]
    return (f"{headline}: {step}" if event.get("key") or event.get("step") else headline), ""


def _raw(event: dict) -> str:
    text = json.dumps(event, ensure_ascii=False, default=str)
    return text if len(text) <= RAW_MAX else text[:RAW_MAX] + " …"


def _entry(event: dict) -> dict:
    headline, detail = sentence(event)
    return {
        "seq": event["seq"], "ts": event.get("ts"), "type": event.get("type"), "by": event.get("by", ""),
        "key": event.get("key") or event.get("step") or "", "headline": headline, "detail": detail, "raw": _raw(event),
    }


def _minutes(first: Any, last: Any) -> int:
    start, end = parse_iso(first), parse_iso(last)
    if start is None or end is None:
        return 1
    return max(1, int((end - start).total_seconds() // 60))


def _fold(run: list[dict]) -> dict:
    tools = sum(1 for e in run if e.get("last_tool"))
    return {
        "seq": run[-1]["seq"], "first_seq": run[0]["seq"], "ts": run[-1].get("ts"), "type": "agent_activity",
        "headline": f"Agent active {_minutes(run[0].get('ts'), run[-1].get('ts'))} min, {tools} tool calls",
        "detail": "", "raw": "",
    }


def _allowed(event: dict, kinds: tuple[str, ...]) -> bool:
    return not kinds or any(in_kind(event, kind) for kind in kinds)


def activity_page(events: list[dict], before: int | None, kinds: tuple[str, ...], limit: int, turns: bool) -> dict:
    """`{entries, before, total}`: up to `limit` entries newest first from the ones with `seq < before`.

    `kinds` is the filter (`()` is all). Unless `turns`, a run of consecutive `agent_turn` events is one
    `agent_activity` entry, found over the whole log so that no page splits it, and shown under "all" and "agents".
    `before` in the result is the cursor for the next page: the smallest `seq` on this one (a fold's `first_seq`),
    `None` when nothing older is left. `total` is the number of entries the filter allows over the whole log.
    """
    log = sorted((e for e in events if isinstance(e, dict) and isinstance(e.get("seq"), int)), key=lambda e: e["seq"])
    entries: list[dict] = []
    run: list[dict] = []

    def close() -> None:
        if run and (not kinds or "agents" in kinds):
            entries.append(_fold(run))
        run.clear()

    for event in log:
        if not turns and event.get("type") == "agent_turn":
            run.append(event)
            continue
        close()
        if _allowed(event, kinds):
            entries.append(_entry(event))
    close()

    older = [e for e in entries if before is None or e["seq"] < before]
    older.sort(key=lambda e: e["seq"], reverse=True)
    page = older[: max(limit, 0)]
    cursor = min((e.get("first_seq", e["seq"]) for e in page), default=None) if len(older) > len(page) else None
    return {"entries": page, "before": cursor, "total": len(entries)}
