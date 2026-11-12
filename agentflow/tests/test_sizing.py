from __future__ import annotations

import pytest

from agentflow import sizing
from agentflow.config import EffortPolicy

NO = dict.fromkeys(sizing.QUESTIONS, "no")
ALLOWED = ["low", "medium", "high"]


def f(**answers):
    return sizing.flags(NO | answers)


def test_answers_are_normalized_and_unsure_is_cautious():
    assert sizing.parse_answers(NO | {"risk": "Unsure", "extra": "x"}) == NO | {"risk": "unsure"}
    assert sizing.parse_answers({"risk": "no"}) is None
    assert sizing.parse_answers(dict.fromkeys(sizing.QUESTIONS, "maybe")) is None
    assert f(risk="unsure")["risk"] is True and f(mechanical="unsure")["mechanical"] is False


@pytest.mark.parametrize("flags, files, security, judgement, level", [
    (f(mechanical="yes"), 2, False, False, "low"),
    (f(mechanical="yes"), 3, False, False, "medium"),       # more than LOW_MAX_FILES
    (f(mechanical="unsure"), 1, False, False, "medium"),    # unsure is not mechanical
    (f(mechanical="yes"), 1, True, False, "high"),          # the security overlay
    (f(mechanical="yes"), 1, False, True, "medium"),        # judgement: never low
    (f(), 5, False, False, "medium"),
    (f(risk="yes"), 1, False, False, "high"),
    (f(interface="yes"), 1, False, False, "medium"),
    (f(interface="yes", novelty="yes"), 1, False, False, "high"),
    (f(ambiguity="yes"), 1, False, True, "high"),           # judgement: any risk answer is high
])
def test_level_for(flags, files, security, judgement, level):
    assert sizing.level_for(flags, files, security, judgement) == level


def test_clamp_goes_up_to_the_nearest_allowed_level_else_the_highest():
    assert sizing.clamp("low", ["medium", "high"]) == "medium"
    assert sizing.clamp("medium", ["high"]) == "high"
    assert sizing.clamp("high", ["low", "medium"]) == "medium"


def test_floor_raises_only_judgement_steps():
    assert sizing.floor("low", True, ALLOWED) == "medium" and sizing.floor("low", False, ALLOWED) == "low"
    assert sizing.floor("high", True, ALLOWED) == "high"


def test_decide_falls_back_to_default_or_high_for_judgement():
    policy = EffortPolicy(default="low", allowed=ALLOWED, sizing=True)
    assert sizing.decide(None, 0, False, False, policy) == ("low", True)
    assert sizing.decide(None, 0, False, True, policy) == ("high", True)
    assert sizing.decide(NO | {"mechanical": "yes"}, 0, False, False, policy) == ("low", False)


def test_judgement_steps_by_phase_or_role():
    assert sizing.is_judgement("define", "build") and sizing.is_judgement("build", "review")
    assert sizing.is_judgement("bug", "debug") and not sizing.is_judgement("verify", "build")


def test_sizer_effort_and_escalation():
    assert sizing.sizer_effort(False, ALLOWED) == "low" and sizing.sizer_effort(True, ALLOWED) == "medium"
    assert sizing.escalate("medium", ALLOWED) == "high" and sizing.escalate("high", ALLOWED) is None
    assert sizing.escalate("low", ["low", "high", "max"]) == "high"
    assert sizing.highest(["low", "high", "medium"]) == "high"


def test_parse_result_reads_the_whole_message_or_its_last_json_block():
    assert sizing.parse_result('{"answers": {}}') == {"answers": {}}
    assert sizing.parse_result('Done.\n```json\n{"a": 1}\n```\n```json\n{"a": 2}\n```') == {"a": 2}
    assert sizing.parse_result("no json here") is None and sizing.parse_result("[1, 2]") is None


@pytest.mark.parametrize("bullet", ["- ", "* ", "+ ", "1. ", "  - "])
def test_ticking_a_box_keeps_the_plan_digest(bullet):
    open_ = [(1, "A", f"{bullet}[ ] write it"), (2, "B", f"{bullet}[ ] test it")]
    ticked = [(1, "A", f"{bullet}[x] write it"), (2, "B", f"{bullet}[X] test it")]
    edited = [(1, "A", f"{bullet}[ ] write it"), (2, "B", f"{bullet}[ ] test it twice")]
    assert sizing.plan_digest(open_) == sizing.plan_digest(ticked) != sizing.plan_digest(edited)


def test_task_card_names_the_facts_and_caps_the_ask():
    card = sizing.task_card(key="build/build/task-2/implement", phase="build", role="build", ask="x" * 9000,
                            files=[("src/a.py", 120), ("src/new.py", None)], changed=["src/a.py"],
                            security=True, failing_gates=["unit"])
    assert "src/a.py (120 lines)" in card and "src/new.py (new or unreadable)" in card
    assert "security overlay: applies" in card and "failing gates: unit" in card
    assert card.count("x") <= sizing.ASK_MAX


def test_reason_text_is_bounded_and_strings_only():
    assert sizing.reason_text("y" * 1000) == "y" * sizing.REASON_MAX
    assert sizing.reason_text(None) == "" and sizing.reason_text(5) == ""
