"""Applying a run's route profile (slice T3, Task 4): the steps it skips, each phase's doubt-round cap, and the per-task
review it drops for a task with no risk. Every run is seeded with a `route_profile` before it advances, and
`route.lanes: []` keeps the triage from running, so each test pins one profile."""

from __future__ import annotations

import pytest

from agentflow import doubt, sizing
from agentflow.conditions import make_ctx
from agentflow.hitl import approve
from agentflow.pipeline import RunOptions, advance, approval_text, start
from agentflow.state import Run
from conftest import with_doubt
from test_feature_lane import BIG, IMPL, REVIEW, begin_build, build, plan_text

PIPE = {"gate_sets": {"build": ["ok"], "verify": ["ok"], "setup": []}}
SPEC = "docs/specs/SPEC-demo.md"
STATUS = "**Status: proposed** (pending approval)"
OPEN = "## Open Questions\n\n- Có lưu bộ nhớ đệm token không?\n"
CLOSED = "## Open Questions\n\nNone for this module.\n"
PLAN_TEXT = "# Plan: demo\n\n### Task 1: First thing\n\n**Depends on:** none\n\n- [ ] **Step 1: Do it**\n"
HIGH = {"severity": "high", "file": SPEC, "line": 3, "text": "Thiếu trường hợp rollback"}
DRAFT, REVIEW_KEY, RECONCILE = "define/draft", "define/spec_review", "define/spec_reconcile"
GRILL = "define/grill_open_questions"
PLAN_REVIEW = "plan/plan_review"
XS = {"skip": [GRILL, REVIEW_KEY, PLAN_REVIEW]}
NO = dict.fromkeys(sizing.QUESTIONS, "no")
PLAN_SIZE = "build/build" + sizing.SIZE_MARK + "-1"


def spec_text(questions: str, n: int = 0) -> str:
    extra = f"\n\nRollback {n}." if n else ""
    return f"# Spec: `demo`\n\n{STATUS}\n\n## Objective\n\nMột mô-đun mẫu.{extra}\n\n{questions}"


def done(summary: str = "ok", **extra) -> dict:
    return {"result": {"status": "done", "summary": summary, **extra}}


def found(*findings: dict) -> dict:
    return done("tìm thấy", findings=list(findings))


def revised(n: int) -> dict:
    """A reconcile act that edits the spec (a new line each round) and fixes the round's one finding."""
    return {"write": {SPEC: spec_text(CLOSED, n)},
            **done(f"sửa lần {n}", dispositions=[{"finding": f"R{n}-F1", "class": "actionable", "note": "ok"}])}


def configure(flow_repo, profiles: dict, mode: str = "on", finding_rounds: int = 2, **extra) -> None:
    patch = {**PIPE, "autonomy": {"finding_rounds": finding_rounds},
             "route": {"mode": mode, "lanes": [], "profiles": profiles}, **extra}
    flow_repo.configure(patch, autopilot=True, edit=with_doubt)


def seed(run: Run, profile: str, applied: bool = True) -> None:
    run.emit("route_profile", profile=profile, applied=applied, proposed_by="rule", upgraded_from=None, facts={},
             reason="")


def begin(flow_repo, acts: dict, profile: str | None, applied: bool = True, to: str = "define") -> Run:
    flow_repo.script(acts)
    run = start(flow_repo.cfg, flow_repo.root, "feature", {"module": "demo"}, RunOptions(to_phase=to))
    if profile is not None:
        seed(run, profile, applied)
    return run


def go(flow_repo, run: Run) -> str:
    return advance(run, flow_repo.cfg, RunOptions())


def called(flow_repo, prefix: str = "") -> list[str]:
    return [c["step"] for c in flow_repo.calls() if c["step"].startswith(prefix) and "/idea/" not in c["step"]]


def events(run: Run, etype: str) -> list[dict]:
    return [e for e in run.events.read() if e["type"] == etype]


def route_skips(run: Run) -> list[dict]:
    return [e for e in events(run, "step_skipped") if str(e.get("condition") or "").startswith("route:")]


def reconcile_rounds(run: Run, phase: str = "define") -> list[dict]:
    return [e for e in events(run, "phase_rejected") if e.get("kind") == doubt.RECONCILE and e["phase"] == phase]


# --------------------------------------------------------------------------- skipped steps


def test_an_xs_run_has_no_grilling_and_no_doubt_round(flow_repo):
    configure(flow_repo, {"XS": XS})
    run = begin(flow_repo, {DRAFT: [{"write": {SPEC: spec_text(OPEN)}, **done("drafted")}]}, "XS", to="plan")
    assert go(flow_repo, run) == "awaiting_approval"
    approve(run, "define", by="me")
    plan = make_ctx(flow_repo.cfg, flow_repo.root, "feature", run.state["params"], run).values["plan_target"]
    flow_repo.script({"plan/plan": [{"write": {plan: PLAN_TEXT}, **done("planned")}]})
    assert go(flow_repo, run) == "awaiting_approval"
    skipped = {e["key"]: e for e in route_skips(run)}
    assert set(skipped) == {GRILL, REVIEW_KEY, PLAN_REVIEW}
    for event in skipped.values():
        assert event["condition"] == "route:XS" and event["reason"].startswith("route profile XS")
    steps = called(flow_repo)
    assert DRAFT in steps and "plan/plan" in steps
    assert not [s for s in steps if s.startswith(GRILL) or s in (REVIEW_KEY, PLAN_REVIEW)]
    assert not [e for e in events(run, "phase_rejected") if e.get("kind") == doubt.RECONCILE]


def test_a_reconcile_step_keeps_its_own_check_first(flow_repo):
    configure(flow_repo, {"XS": {"skip": [RECONCILE]}})
    run = begin(flow_repo, {DRAFT: [{"write": {SPEC: spec_text(CLOSED)}, **done("drafted")}]}, "XS")
    assert go(flow_repo, run) == "awaiting_approval"
    (skipped,) = [e for e in events(run, "step_skipped") if e["key"] == RECONCILE]
    assert skipped["reason"] == doubt.RECONCILE_ONLY


# --------------------------------------------------------------------------- doubt-round caps


@pytest.mark.parametrize("seeded", [True, False])
def test_s_caps_the_spec_review_at_one_round(flow_repo, seeded):
    configure(flow_repo, {"S": {"doubt_rounds": {"define": 1}}}, finding_rounds=2)
    acts = {DRAFT: [{"write": {SPEC: spec_text(CLOSED)}, **done("drafted")}],
            REVIEW_KEY: [found(HIGH), found(HIGH), found(HIGH)], RECONCILE: [revised(1), revised(2)]}
    run = begin(flow_repo, acts, "S" if seeded else None)
    assert go(flow_repo, run) == "awaiting_approval"
    assert len(reconcile_rounds(run)) == (1 if seeded else 2)
    assert events(run, "reconcile_stopped")[-1]["reason"] == "bounded"
    assert doubt.STOP_LINES["bounded"] in approval_text(flow_repo.cfg, run, "define")


def test_a_cap_above_finding_rounds_does_not_widen(flow_repo):
    configure(flow_repo, {"S": {"doubt_rounds": {"define": 5}}}, finding_rounds=1)
    acts = {DRAFT: [{"write": {SPEC: spec_text(CLOSED)}, **done("drafted")}],
            REVIEW_KEY: [found(HIGH), found(HIGH), found(HIGH)], RECONCILE: [revised(1), revised(2)]}
    run = begin(flow_repo, acts, "S")
    assert go(flow_repo, run) == "awaiting_approval"
    assert len(reconcile_rounds(run)) == 1
    assert events(run, "reconcile_stopped")[-1]["reason"] == "bounded"


# --------------------------------------------------------------------------- per-task review


def build_run(flow_repo, sizing_on: bool, profile: str = "S"):
    flow_repo.configure({**BIG, "effort": {"sizing": sizing_on},
                         "route": {"mode": "on", "lanes": [], "profiles": {"S": {"per_task_review": False}}}})
    run = begin_build(flow_repo, plan_text(count=2))
    seed(run, profile)
    return run


def test_s_drops_the_review_of_a_task_with_no_risk(flow_repo):
    import json

    run = build_run(flow_repo, sizing_on=True)
    tasks = {"1": {"answers": NO, "reason": "task 1"}, "2": {"answers": NO | {"risk": "yes"}, "reason": "task 2"}}
    flow_repo.script({PLAN_SIZE: [{"final_text": json.dumps({"tasks": tasks})}]})
    assert build(flow_repo, run) == ("done", True)
    steps = [c["step"] for c in flow_repo.calls()]
    assert REVIEW.format(n=1) not in steps and REVIEW.format(n=2) in steps
    assert IMPL.format(n=1) in steps and IMPL.format(n=2) in steps
    skipped = [e for e in flow_repo.events(run, "review_skipped") if e["key"] == REVIEW.format(n=1)]
    assert len(skipped) == 1 and skipped[0]["reason"] == "route profile S"
    assert not [e for e in flow_repo.events(run, "review_skipped") if e["key"] == REVIEW.format(n=2)]
    assert [e["key"] for e in flow_repo.events(run, "task_committed")] == ["build/build/task-1", "build/build/task-2"]


def test_a_task_with_no_sizer_answers_keeps_its_review(flow_repo):
    run = build_run(flow_repo, sizing_on=False)
    assert build(flow_repo, run) == ("done", True)
    steps = [c["step"] for c in flow_repo.calls()]
    assert REVIEW.format(n=1) in steps and REVIEW.format(n=2) in steps
    assert flow_repo.events(run, "review_skipped") == []
    assert len(flow_repo.events(run, "task_committed")) == 2


# --------------------------------------------------------------------------- nothing applied


def full_route_acts() -> dict:
    return {DRAFT: [{"write": {SPEC: spec_text(CLOSED)}, **done("drafted")}],
            REVIEW_KEY: [found(HIGH), found(HIGH), found(HIGH)], RECONCILE: [revised(1), revised(2)]}


def test_observe_applies_nothing(flow_repo):
    configure(flow_repo, {"XS": {**XS, "doubt_rounds": {"define": 1}}}, mode="observe")
    run = begin(flow_repo, full_route_acts(), "XS", applied=False)
    assert go(flow_repo, run) == "awaiting_approval"
    assert route_skips(run) == []
    assert called(flow_repo, "define/").count(REVIEW_KEY) == 3
    assert len(reconcile_rounds(run)) == 2


def test_an_unknown_profile_runs_the_full_route(flow_repo):
    configure(flow_repo, {"XS": {**XS, "doubt_rounds": {"define": 1}}})
    run = begin(flow_repo, full_route_acts(), "M")
    assert go(flow_repo, run) == "awaiting_approval"
    assert route_skips(run) == []
    assert called(flow_repo, "define/").count(REVIEW_KEY) == 3
    assert len(reconcile_rounds(run)) == 2
