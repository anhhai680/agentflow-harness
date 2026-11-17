"""activity.py: a run's log as sentences, by kind, folded and paged by seq."""

from __future__ import annotations

import json

import pytest

from agentflow.activity import KINDS, activity_page, in_kind, sentence


def ev(seq, type, **kw):
    return {"seq": seq, "ts": f"2026-11-16T10:{seq // 60 % 60:02d}:{seq % 60:02d}+00:00", "type": type, "by": "runner", **kw}


def log_of(n):  # a 5,000-event log: a step, then turns, in a cycle
    out = []
    for seq in range(1, n + 1):
        kind = ("step_started", "agent_turn", "agent_turn", "gate_result", "step_finished")[seq % 5]
        out.append(ev(seq, kind, key="build/build/task-1/implement", last_tool="Edit", gate="pytest", passed=True))
    return out


def test_paging_reaches_the_first_event_of_5000():
    events, before, seen = log_of(5000), None, []
    while True:
        page = activity_page(events, before, (), 200, turns=True)
        seen += [e["seq"] for e in page["entries"]]
        before = page["before"]
        if before is None:
            break
    assert seen == list(range(5000, 0, -1))


def test_decisions_are_only_hitl_phase_checkpoint_and_runner_decisions():
    events = [ev(1, "phase_started", phase="build"), ev(2, "gate_result", gate="x", passed=True),
              ev(3, "hitl_raised", kind="blocked", key="build/x"), ev(4, "condition_decided", condition="c", value=True),
              ev(5, "checkpoint_cleared", key="build/x"), ev(6, "agent_started", step="build/x"),
              ev(7, "phase_unknown_future"), ev(8, "step_committed", key="build/x", sha="a" * 40)]
    page = activity_page(events, None, ("decisions",), 200, turns=False)
    assert [e["type"] for e in page["entries"]] == ["phase_unknown_future", "checkpoint_cleared", "condition_decided",
                                                     "hitl_raised", "phase_started"]


def test_turns_fold_into_one_line_and_a_page_never_splits_a_fold():
    events = [ev(1, "step_started", key="k")] + [ev(s, "agent_turn", last_tool="Bash" if s % 2 else None) for s in range(2, 62)] \
             + [ev(62, "step_finished", key="k", status="done", summary="ok")]
    first = activity_page(events, None, (), 2, turns=False)
    assert [e["type"] for e in first["entries"]] == ["step_finished", "agent_activity"]
    assert first["entries"][1]["headline"] == "Agent active 1 min, 30 tool calls" and first["before"] == 2
    assert [e["type"] for e in activity_page(events, first["before"], (), 2, turns=False)["entries"]] == ["step_started"]


def test_a_cursor_is_stable_while_the_log_grows():
    events = log_of(600)
    older = activity_page(events, 400, (), 100, turns=True)
    grown = activity_page(events + [ev(s, "gate_result", gate="g", passed=False) for s in range(601, 700)], 400, (), 100, turns=True)
    assert older == {**grown, "total": older["total"]} and older["entries"][0]["seq"] == 399


@pytest.mark.parametrize("event, headline", [
    (ev(1, "step_finished", key="build/build/task-9/implement", status="done", summary="Task 9 is complete"),
     "Task 9 · implement finished"),
    ({**ev(2, "phase_approved", phase="define"), "by": "alex"}, "You approved define"),
    (ev(3, "phase_approved", phase="build"), "The runner approved build"),
    (ev(4, "hitl_raised", kind="blocked", key="build/build/task-9/implement", summary="floor_guard"),
     "Stopped for you: Task 9 · implement (blocked)"),
    (ev(5, "brand_new_thing", key="verify/verify"), "Brand new thing: verify"),
    ({"seq": 6, "type": "gate_result"}, "Gate ? skipped on ?"),
])
def test_each_event_reads_as_a_sentence(event, headline):
    assert sentence(event)[0] == headline


# ---- beyond the brief ----

def test_details_and_other_templates():
    assert sentence(ev(1, "step_finished", key="verify/verify", status="failed", summary="boom")) == ("verify failed", "boom")
    assert sentence(ev(2, "phase_rejected", phase="plan", round=2, note="redo", by="alex")) == ("You sent plan back (round 2)", "redo")
    assert sentence(ev(3, "hitl_answered", key="define/x", by="alex", answers={"q1": "a1", "q2": "a2"})) == (
        "You answered define · x", "q1: a1; q2: a2")
    assert sentence(ev(4, "step_committed", key="build/x", sha="a" * 40, summary="s")) == ("Committed build · x (aaaaaaa)", "s")
    assert sentence(ev(5, "task_committed", key="k", n=3, sha="b" * 40, title="T")) == ("Task 3 committed (bbbbbbb)", "T")
    assert sentence(ev(6, "agent_started", step="build/x", model="m", effort="high")) == ("build · x agent started (m · high)", "")
    assert sentence(ev(7, "condition_decided", key="build/x", condition="c", value=False)) == (
        "The runner took c as false for build · x", "")
    assert sentence(ev(8, "pr_opened", url="https://example.test/1")) == ("Draft pull request opened", "https://example.test/1")


def test_gate_result_reads_passed_failed_and_skipped():
    assert sentence(ev(1, "gate_result", gate="g", key="a/b", passed=True))[0] == "Gate g passed on a · b"
    assert sentence(ev(2, "gate_result", gate="g", key="a/b", passed=False))[0] == "Gate g failed on a · b"
    assert sentence(ev(3, "gate_result", gate="g", key="a/b", passed=True, skipped=True))[0] == "Gate g skipped on a · b"


def test_sentence_never_raises_on_odd_events():
    for event in ({}, {"type": None}, {"type": "step_finished", "key": 5}, {"type": "x", "key": ["a"]},
                  {"type": "hitl_answered", "answers": "text"}, {"type": "task_committed", "sha": None}):
        headline, detail = sentence(event)
        assert isinstance(headline, str) and isinstance(detail, str)


def test_kinds_and_in_kind():
    assert KINDS == ("decisions", "agents", "gates", "commits", "stops")
    assert in_kind(ev(1, "hitl_raised"), "decisions") and in_kind(ev(1, "hitl_raised"), "stops")
    assert in_kind(ev(1, "agent_turn"), "agents") and not in_kind(ev(1, "agent_turn"), "decisions")
    assert not in_kind(ev(1, "brand_new_thing"), "agents") and not in_kind({}, "gates")


def test_an_unknown_type_shows_only_under_all():
    events = [ev(1, "brand_new_thing"), ev(2, "gate_result", gate="g", passed=True)]
    assert [e["seq"] for e in activity_page(events, None, (), 10, turns=False)["entries"]] == [2, 1]
    for kind in KINDS:
        assert 1 not in [e["seq"] for e in activity_page(events, None, (kind,), 10, turns=False)["entries"]]


def test_turns_true_shows_each_turn_and_no_fold():
    events = [ev(s, "agent_turn", last_tool="Bash") for s in range(1, 6)]
    page = activity_page(events, None, (), 10, turns=True)
    assert [e["type"] for e in page["entries"]] == ["agent_turn"] * 5 and page["total"] == 5 and page["before"] is None


def test_a_fold_wholly_before_the_cursor_is_kept_and_one_after_it_is_not():
    events = [ev(1, "agent_turn"), ev(2, "agent_turn"), ev(3, "step_started", key="k"), ev(4, "agent_turn"), ev(5, "agent_turn")]
    def folds(page):
        return [(e["first_seq"], e["seq"]) for e in page["entries"] if e["type"] == "agent_activity"]

    assert folds(activity_page(events, 3, (), 10, turns=False)) == [(1, 2)]
    assert folds(activity_page(events, None, (), 10, turns=False)) == [(4, 5), (1, 2)]
    assert folds(activity_page(events, 4, (), 10, turns=False)) == [(1, 2)]
    assert activity_page(events, None, (), 10, turns=False)["total"] == 3


def test_a_run_of_turns_counts_every_turn_even_across_pages_of_other_events():
    events = [ev(s, "agent_turn", last_tool="Edit") for s in range(1, 101)] + [ev(101, "gate_result", gate="g", passed=True)]
    page = activity_page(events, None, (), 1, turns=False)
    assert [e["type"] for e in page["entries"]] == ["gate_result"] and page["before"] == 101
    fold = activity_page(events, page["before"], (), 5, turns=False)["entries"][0]
    assert fold["headline"].endswith("100 tool calls") and fold["first_seq"] == 1 and fold["seq"] == 100 and fold["raw"] == ""


def test_kinds_filter_with_a_fold():
    events = [ev(1, "agent_turn", last_tool="Edit"), ev(2, "agent_turn"), ev(3, "gate_result", gate="g", passed=True)]
    assert [e["type"] for e in activity_page(events, None, ("agents",), 10, turns=False)["entries"]] == ["agent_activity"]
    assert [e["type"] for e in activity_page(events, None, ("gates",), 10, turns=False)["entries"]] == ["gate_result"]
    assert [e["type"] for e in activity_page(events, None, ("gates", "agents"), 10, turns=False)["entries"]] == [
        "gate_result", "agent_activity"]
    assert activity_page(events, None, ("gates",), 10, turns=False)["total"] == 1


def test_fold_minutes_run_from_first_to_last_turn():
    events = [{"seq": 1, "ts": "2026-11-16T10:00:00+00:00", "type": "agent_turn", "last_tool": "A"},
              {"seq": 2, "ts": "2026-11-16T10:05:30+00:00", "type": "agent_turn"}]
    assert activity_page(events, None, (), 10, turns=False)["entries"][0]["headline"] == "Agent active 5 min, 1 tool calls"


def test_entries_carry_the_event_and_raw_is_cut_at_4096():
    small = ev(1, "gate_result", gate="g", passed=True, key="a/b")
    entry = activity_page([small], None, (), 10, turns=True)["entries"][0]
    assert entry == {"seq": 1, "ts": small["ts"], "type": "gate_result", "by": "runner", "key": "a/b",
                     "headline": "Gate g passed on a · b", "detail": "", "raw": json.dumps(small, ensure_ascii=False)}
    big = ev(2, "step_finished", key="k", summary="é" * 9000)
    raw = activity_page([big], None, (), 10, turns=True)["entries"][0]["raw"]
    assert len(raw) == 4096 + 2 and raw.endswith(" …") and raw.startswith('{"seq": 2')


def test_total_counts_the_whole_log_and_an_empty_page_has_no_cursor():
    events = log_of(50)
    page = activity_page(events, 10, (), 3, turns=True)
    assert page["total"] == 50 and page["before"] == 7
    assert activity_page(events, 1, (), 3, turns=True) == {"entries": [], "before": None, "total": 50}
    assert activity_page([], None, (), 3, turns=False) == {"entries": [], "before": None, "total": 0}
