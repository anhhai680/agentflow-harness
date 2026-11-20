"""`carry.decide` and `carry.recorded`: when a plan task resumes the previous task's implementer session."""

from __future__ import annotations

from agentflow.carry import REASONS, decide, recorded

PREV, KEY = "b/task-1", "b/task-2/implement"
PREV_IMPL = f"{PREV}/implement"
STEPS = {PREV_IMPL: {"session_id": "S", "effort": "medium"}}
NO = dict.fromkeys(("interface", "risk", "ambiguity", "breadth", "novelty", "mechanical"), "no")


def ev(type_: str, **fields) -> dict:
    return {"type": type_, **fields}


def turn(session: str, pct: float) -> dict:
    return ev("agent_turn", session_id=session, context_pct=pct)


def sized(level: str = "medium", **answers: str) -> dict:
    return ev("effort_decided", key=KEY, level=level, answers={**NO, **answers})


def run(events: list[dict], steps: dict | None = None, max_tasks: int = 4, pct: int = 40) -> tuple[str | None, str]:
    return decide(events, STEPS if steps is None else steps, PREV, KEY, max_tasks, pct)


def test_a_clean_small_young_session_carries():
    assert run([turn("S", 12.0)]) == (PREV_IMPL, "carried")


def test_no_session_declines():
    assert run([], steps={PREV_IMPL: {"effort": "medium"}}) == (None, "no_session")
    assert run([], steps={}) == (None, "no_session")


def test_a_stop_on_the_previous_task_declines():
    assert run([ev("hitl_raised", key=f"{PREV}/review")]) == (None, "not_clean")
    assert run([ev("step_parked", key=PREV_IMPL)]) == (None, "not_clean")
    assert run([ev("hitl_raised", key=f"{PREV}/implement/resolve-1")]) == (None, "not_clean")
    assert run([ev("hitl_raised", key=PREV)]) == (None, "not_clean")


def test_a_stop_on_another_task_does_not_count():
    assert run([ev("hitl_raised", key="b/task-10/implement"), ev("hitl_raised", key="b/task-2/review")]) == \
        (PREV_IMPL, "carried")


def test_max_tasks_counts_the_session_carries():
    carried = [ev("session_carried", key=f"b/task-{n}/implement", session_id="S") for n in (0, 1)]
    other = [ev("session_carried", key="b/task-9/implement", session_id="T")]
    assert run(carried + other, max_tasks=3) == (None, "max_tasks")  # the session holds 3 tasks already
    assert run(carried + other, max_tasks=4) == (PREV_IMPL, "carried")


def test_max_tasks_one_never_carries():
    assert run([], max_tasks=1) == (None, "max_tasks")


def test_a_rollover_declines():
    assert run([ev("rolled_over", key=PREV_IMPL, session_id="R")]) == (None, "rolled_over")
    assert run([ev("rolled_over", key="b/task-3/implement", session_id="R")]) == (PREV_IMPL, "carried")


def test_a_compaction_declines():
    assert run([ev("compacted", session_id="S")]) == (None, "compacted")
    assert run([ev("compacted", session_id="T")]) == (PREV_IMPL, "carried")


def test_context_at_the_limit_declines():
    assert run([turn("S", 40.0)]) == (None, "context")
    assert run([turn("S", 39.9)]) == (PREV_IMPL, "carried")
    assert run([turn("S", 55.0), turn("S", 20.0)]) == (PREV_IMPL, "carried")  # only the session's last turn counts
    assert run([turn("S", 20.0), turn("S", 55.0), turn("T", 1.0)]) == (None, "context")


def test_no_turn_counts_as_empty():
    assert run([turn("T", 90.0)]) == (PREV_IMPL, "carried")


def test_a_risk_task_declines():
    assert run([sized(risk="yes")]) == (None, "risk")
    assert run([sized(risk="unsure")]) == (None, "risk")
    assert run([sized(risk="no")]) == (PREV_IMPL, "carried")
    assert run([sized(risk="yes"), sized(risk="no")]) == (PREV_IMPL, "carried")  # the latest answers count
    assert run([ev("effort_decided", key=KEY, level="medium", answers={"risk": "yes"})]) == (PREV_IMPL, "carried")


def test_a_task_sized_above_the_session_declines():
    assert run([sized("high")]) == (None, "effort")
    assert run([sized("low")]) == (PREV_IMPL, "carried")
    assert run([sized("medium")]) == (PREV_IMPL, "carried")
    assert run([sized("xhigh-unknown")]) == (PREV_IMPL, "carried")  # a level the engine doesn't rank says nothing


def test_reasons_are_checked_in_order():
    assert run([ev("compacted", session_id="S"), sized("high", risk="yes")]) == (None, "compacted")
    assert run([turn("S", 60.0), sized("high", risk="yes")]) == (None, "context")
    assert run([sized("high", risk="yes")]) == (None, "risk")
    assert REASONS == ("no_session", "not_clean", "max_tasks", "rolled_over", "compacted", "context", "risk", "effort")


def test_recorded_returns_the_first_decision():
    assert recorded([], KEY) is None
    first = [ev("session_carried", key=KEY, **{"from": PREV_IMPL}, session_id="S", tasks=2),
             ev("carry_declined", key=KEY, **{"from": PREV_IMPL}, reason="context")]
    assert recorded(first, KEY) == (PREV_IMPL, "carried")
    assert recorded(first[::-1], KEY) == (None, "context")
    assert recorded(first, "b/task-3/implement") is None
