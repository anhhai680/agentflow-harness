"""Waves of plan tasks in the runner (`docs/agentflow-throughput-proposal.md`, T5): what `plan_review` is told of
tasks that could share a `**Parallel group:**`, and how often the runner counts them. Built on the helpers of
`test_feature_lane.py` and `test_doubt_flow.py`; the later wave tests of this file reuse `WAVES` and `candidate_plan`."""

from __future__ import annotations

from agentflow.conditions import make_ctx
from agentflow.hitl import answer, approve
from agentflow.pipeline import RunOptions, advance, start
from conftest import with_doubt
from test_feature_lane import BIG, IMPL, begin_build, build, needs_human, plan_text

WAVES = {"parallel": {"max_subagents": 2}}
SHARED = ["src/core.py"]
# Tasks 1 to 5 are done; tasks 6 and 7 are open, ungrouped, independent and each have a file of their own.
CANDIDATE_FILES = {n: SHARED for n in range(1, 6)} | {6: ["src/six.py"], 7: ["src/seven.py"]}


def candidate_plan() -> str:
    none = {n: "none" for n in range(1, 8)}  # a task with no `Depends on` line follows the one before it
    return plan_text(none, count=7, ticked=(1, 2, 3, 4, 5), files=CANDIDATE_FILES)


def candidates_of(flow_repo, run) -> list[dict]:
    return flow_repo.events(run, "parallel_candidates")


def plan_review_prompt(flow_repo, patch: dict, text: str) -> str:
    """The prompt of the `plan/plan_review` session of a run that stops at the plan's approval."""
    flow_repo.configure(patch, autopilot=True, edit=with_doubt)
    flow_repo.script({"define/draft": [{"write": {"docs/specs/SPEC-demo.md": SPEC}, "result": DONE}]})
    run = start(flow_repo.cfg, flow_repo.root, "feature", {"module": "demo"}, RunOptions(to_phase="plan"))
    assert advance(run, flow_repo.cfg, RunOptions()) == "awaiting_approval"
    approve(run, "define", by="me")
    plan = make_ctx(flow_repo.cfg, flow_repo.root, "feature", run.state["params"], run).values["plan_target"]
    flow_repo.script({"plan/plan": [{"write": {plan: text}, "result": DONE}],
                      "plan/plan_review": [{"result": DONE}]})
    assert advance(run, flow_repo.cfg, RunOptions()) == "awaiting_approval"
    return next(c for c in flow_repo.calls() if c["step"] == "plan/plan_review")["argv"][-1]


SPEC = "# Spec: `demo`\n\n**Status: proposed** (pending approval)\n\n## Objective\n\nA demo.\n\n## Open Questions\n\nNone.\n"
DONE = {"status": "done", "summary": "ok"}


def test_plan_review_hears_of_ungrouped_candidates(flow_repo):
    prompt = plan_review_prompt(flow_repo, {"gate_sets": {"build": ["ok"], "verify": ["ok"]}, **WAVES},
                                candidate_plan())
    assert "Tasks 6 and 7 could share" in prompt
    assert "{parallel_candidates}" not in prompt


def test_plan_review_hears_nothing_with_waves_off(flow_repo):
    prompt = plan_review_prompt(flow_repo, {"gate_sets": {"build": ["ok"], "verify": ["ok"]}}, candidate_plan())
    assert "could share" not in prompt and "{parallel_candidates}" not in prompt


def test_candidates_are_counted_once(flow_repo):
    flow_repo.configure({**BIG, **WAVES})
    run = begin_build(flow_repo, candidate_plan())
    key = IMPL.format(n=6)
    flow_repo.script({key: [needs_human("other")]})
    assert build(flow_repo, run) == ("stopped", True)
    [event] = candidates_of(flow_repo, run)
    assert event["key"] == "build/build" and event["pairs"] == [[6, 7]]
    assert answer(run, key, {"q1": "A"}, by="me")
    assert build(flow_repo, run) == ("done", True)  # a new executor, as `agentflow resume` makes
    assert len(candidates_of(flow_repo, run)) == 1


def test_candidates_are_not_counted_with_waves_off(flow_repo):
    flow_repo.configure(BIG)
    run = begin_build(flow_repo, candidate_plan())
    assert build(flow_repo, run) == ("done", True)
    assert candidates_of(flow_repo, run) == []
