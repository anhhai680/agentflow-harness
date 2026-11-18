from __future__ import annotations

import pytest

from agentflow import route
from agentflow.config import ROUTE_PROFILES, Route, RouteProfile, Step

NO = {q: "no" for q in ("interface", "risk", "ambiguity", "breadth", "novelty", "mechanical")}

GRID = {
    "XS": RouteProfile(skip=("define/spec_review",), per_task_review=False),
    "S": RouteProfile(doubt_rounds={"define": 1, "plan": 1}, per_task_review=False),
    "M": RouteProfile(doubt_rounds={"define": 2, "plan": 1}),
    "L": RouteProfile(),
}


def facts(answers=NO, *, tasks=None, files=None, security=False, **overrides) -> route.Facts:
    if answers is not None:
        answers = {**answers, **overrides}
    return route.Facts(answers=answers, tasks=tasks, files=files, security=security)


def with_route(cfg, mode: str):
    cfg.route = Route(mode=mode, lanes=["feature"], profiles=dict(GRID))
    return cfg


@pytest.mark.parametrize(("kw", "expected"), [
    (dict(mechanical="yes", files=2, tasks=1), "XS"),
    (dict(mechanical="yes", files=3, tasks=1), "S"),
    (dict(tasks=3), "S"),
    (dict(interface="yes", tasks=2), "M"),
    (dict(tasks=4), "M"),
    (dict(tasks=12), "L"),
    (dict(risk="unsure", tasks=1), "L"),
    (dict(mechanical="yes", files=1, security=True), "L"),
    (dict(mechanical="yes", novelty="yes", files=1, tasks=1), "S"),
])
def test_pick_follows_the_grid(kw, expected):
    assert route.pick(facts(**kw)) == expected


def test_bad_estimates_count_as_unknown():
    raw = {"answers": NO, "tasks_expected": "two", "files_expected": -1}
    f = route.facts_from(raw, False)
    assert f.tasks is None and f.files is None
    for bad in (True, 10_001, 2.0, None):
        f = route.facts_from({"answers": NO, "tasks_expected": bad, "files_expected": bad}, False)
        assert f.tasks is None and f.files is None
    f = route.facts_from({"answers": NO, "tasks_expected": 0, "files_expected": route.ESTIMATE_MAX}, False)
    assert (f.tasks, f.files) == (0, route.ESTIMATE_MAX)
    mech = route.facts_from({"answers": {**NO, "mechanical": "yes"}, "tasks_expected": "x", "files_expected": "y"}, False)
    assert route.pick(mech) == "M"


def test_no_answers_fall_back_to_l():
    assert route.decide(route.facts_from({}, False), None) == ("L", "fallback")
    assert route.decide(route.facts_from("junk", False), "XS") == ("L", "fallback")
    assert route.decide(route.facts_from({"answers": {"risk": "no"}}, False), None) == ("L", "fallback")


def test_facts_from_normalizes_answers_and_as_event_has_the_plan_keys():
    raw = {"answers": {k: v.upper() for k, v in NO.items()}, "tasks_expected": 2, "files_expected": 1}
    f = route.facts_from(raw, True)
    assert f == route.Facts(answers=NO, tasks=2, files=1, security=True)
    assert f.as_event() == {"answers": NO, "tasks": 2, "files": 1, "security": True}
    assert route.facts_from({}, False).as_event() == {"answers": None, "tasks": None, "files": None, "security": False}


def test_unsure_mechanical_is_not_xs():
    assert route.pick(facts(mechanical="unsure", files=1, tasks=1)) == "S"


def test_unknown_files_with_known_tasks_gives_s_not_xs():
    assert route.pick(facts(mechanical="yes", files=None, tasks=2)) == "S"


def test_zero_tasks_is_a_small_change():
    assert route.pick(facts(tasks=0)) == "S"
    assert route.pick(facts(mechanical="yes", files=0, tasks=0)) == "XS"


def test_a_proposal_only_raises():
    f = facts(tasks=4)
    assert route.pick(f) == "M"
    assert route.decide(f, "L") == ("L", "sizer")
    assert route.decide(f, "M") == ("M", "rule")
    assert route.decide(f, "XS") == ("M", "rule")
    assert route.decide(f, "XXS") == ("M", "rule")
    assert route.decide(f, 3) == ("M", "rule")
    assert route.decide(f, None) == ("M", "rule")


def test_higher_follows_profile_order():
    assert route.higher("XS", "L") == "L"
    assert route.higher("M", "S") == "M"
    assert [route.higher(a, "XS") for a in ROUTE_PROFILES] == list(ROUTE_PROFILES)


def test_upgrade_never_goes_down():
    assert route.upgrade("S", tasks=9, risk=False, security=False) == "M"
    assert route.upgrade("M", tasks=12, risk=False, security=False) == "L"
    assert route.upgrade("S", tasks=2, risk=True, security=False) == "L"
    assert route.upgrade("XS", tasks=1, risk=False, security=True) == "L"
    assert route.upgrade("L", tasks=1, risk=False, security=False) is None
    assert route.upgrade("M", tasks=2, risk=False, security=False) is None
    assert route.upgrade("S", tasks=None, risk=False, security=False) is None


@pytest.mark.parametrize("record", [None, "S", 3, {"profile": 3, "applied": True}, {"profile": "S"}, {"applied": True}])
def test_applied_ignores_unusable_records(cfg, record):
    with_route(cfg, "on")
    assert route.applied(cfg, record) is None
    assert route.skips(cfg, record, "define/spec_review") is None
    assert route.round_cap(cfg, record, "define") is None


def test_applied_only_when_on(cfg):
    record = {"profile": "S", "applied": True}
    for mode in ("off", "observe"):
        assert route.applied(with_route(cfg, mode), record) is None
    with_route(cfg, "on")
    assert route.applied(cfg, record) == GRID["S"]
    assert route.applied(cfg, None) is None
    assert route.applied(cfg, {"profile": "S", "applied": False}) is None
    assert route.applied(cfg, {"profile": "XXL", "applied": True}) is None


def test_round_cap_and_skips_read_the_applied_profile(cfg):
    with_route(cfg, "on")
    xs = {"profile": "XS", "applied": True}
    s = {"profile": "S", "applied": True}
    assert route.skips(cfg, xs, "define/spec_review") == "XS"
    assert route.skips(cfg, xs, "plan/plan_review") is None
    assert route.skips(cfg, s, "define/spec_review") is None
    assert route.round_cap(cfg, s, "define") == 1
    assert route.round_cap(cfg, s, "build") is None
    assert route.round_cap(cfg, xs, "define") is None
    with_route(cfg, "observe")
    assert route.skips(cfg, xs, "define/spec_review") is None
    assert route.round_cap(cfg, s, "define") is None


def test_last_record_is_the_last_route_profile_event():
    events = [
        {"type": "run_started"},
        {"type": "route_profile", "profile": "S", "applied": False},
        {"type": "gate_result"},
        {"type": "route_profile", "profile": "L", "applied": True},
        {"type": "phase_started"},
    ]
    assert route.last_record(events) == events[3]
    assert route.last_record([{"type": "run_started"}]) is None
    assert route.last_record([]) is None


def test_reviews_task_keeps_risky_and_unknown_tasks():
    s = GRID["S"]
    assert route.reviews_task(s, None, False) is True
    assert route.reviews_task(s, {**NO, "risk": "unsure"}, False) is True
    assert route.reviews_task(s, NO, True) is True
    assert route.reviews_task(s, NO, False) is False
    assert route.reviews_task(None, NO, False) is True
    assert route.reviews_task(GRID["M"], NO, False) is True


@pytest.mark.parametrize("step", [
    Step(id="finish", type="finish"),
    Step(id="ci", type="ci"),
    Step(id="build", type="agent", foreach="plan_task"),
    Step(id="wrapped", type="group", steps=[Step(id="finish", type="finish")]),
    Step(id="wrapped", type="group", steps=[Step(id="build", type="agent", foreach="plan_task")]),
])
def test_a_hand_written_profile_never_skips_a_finish_ci_or_foreach_step(cfg, step):
    """`validate` refuses such a profile; a pack nobody validated must still not skip the step at run time."""
    key = f"ship/{step.id}"
    cfg.route = Route(mode="on", lanes=["feature"], profiles={"XS": RouteProfile(skip=(key,))})
    record = {"profile": "XS", "applied": True}
    assert route.skips(cfg, record, key, step) is None
    assert route.skips(cfg, record, key, Step(id=step.id, type="agent")) == "XS"
