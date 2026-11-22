"""The approval ramp (`docs/specs/agentflow-throughput-proposal.md` §4.1.5, slice T7): before a phase approval is raised,
the runner records whether it would approve the phase itself (`would_auto_approve`), and under `auto` it approves it
once `ramp_samples` human decisions in a row agreed with it and the scorecard is green. The `flow` fixture and the
`begin`/`go` helpers are copies of `test_doubt_flow.py`'s, with `autonomy.approvals` in the patch."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from agentflow import approval_ramp, scorecard
from agentflow.approval_ramp import CHECKS, RAMP_PROFILES, Sample, eligible, ramp_view, samples
from agentflow.autonomy import AUTO_BY
from agentflow.config import load_config
from agentflow.hitl import reject
from agentflow.pipeline import RunOptions, advance, start
from agentflow.retro import finished_runs
from agentflow.state import Run
from conftest import with_doubt

PIPE = {"gate_sets": {"build": ["ok"], "verify": ["ok"], "setup": []}}
SPEC = "docs/specs/SPEC-demo.md"
STATUS = "**Status: proposed** (pending approval)"  # `spec_awaits_approval` holds: the review runs
SPEC_TEXT = f"# Spec: `demo`\n\n{STATUS}\n\n## Objective\n\nMột mô-đun mẫu.\n\n## Open Questions\n\nNone for this module.\n"
LOW = {"severity": "low", "file": SPEC, "line": 9, "text": "lỗi chính tả"}
DRAFT, REVIEW = "define/draft", "define/spec_review"
PLAN = "plan/plan"
SECURITY = {"conditions": {"security_path": {"any": None, "check": "file_exists", "path": "README.md"}}}


def done(summary: str = "ok", **extra) -> dict:
    return {"result": {"status": "done", "summary": summary, **extra}}


def found(*findings: dict) -> dict:
    return done("tìm thấy", findings=list(findings))


DRAFTED = {"write": {SPEC: SPEC_TEXT}, **done("drafted")}


def ramp(mode: str, **extra) -> dict:
    return {**PIPE, **extra, "autonomy": {"approvals": {"define": mode}}}


@pytest.fixture
def flow(flow_repo):
    flow_repo.configure(ramp("observe"), autopilot=True, edit=with_doubt)
    return flow_repo


def begin(flow, acts: dict, to: str | None = "define", cfg=None, profile: str | None = "S") -> Run:
    """Start the run and, before anything walks, record its route profile (`route.mode` stays `off` in the fixture)."""
    flow.script({DRAFT: [DRAFTED], **acts})
    run = start(cfg or flow.cfg, flow.root, "feature", {"module": "demo"}, RunOptions(to_phase=to))
    if profile is not None:
        run.emit("route_profile", profile=profile, applied=False, proposed_by="rule")
    return run


def go(flow, run: Run, cfg=None) -> str:
    return advance(run, cfg or flow.cfg, RunOptions())


def events(run: Run, etype: str) -> list[dict]:
    return [e for e in run.events.read() if e["type"] == etype]


def called(flow, prefix: str) -> list[str]:
    return [c["step"] for c in flow.calls() if c["step"].startswith(prefix)]


# --------------------------------------------------------------------------- finished runs, by hand


def past(cfg, repo, decision: str = "approve", *, profile: str = "S", phase: str = "define", lane: str = "feature",
         verdict: bool = True, missing: int | None = None) -> Run:
    """A finished run of `lane` whose first `would_auto_approve` on `phase` had `verdict` for `profile`, then
    `decision`: `approve` or `reject` (a person's), `ramp` (the runner's, by the ramp) or `none`. With `missing`, its
    last audit found that many obligations missing."""
    run = Run.create(cfg, repo, lane, {"module": "old"})
    run.emit("would_auto_approve", phase=phase, mode="observe", verdict=verdict, held=[], missing=[], profile=profile)
    if decision == "approve":
        run.emit("phase_approved", by="me", phase=phase)
    elif decision == "reject":
        run.emit("phase_rejected", by="me", phase=phase, round=1, note="chưa được")
    elif decision == "ramp":
        run.emit("phase_approved", by=AUTO_BY, phase=phase, ramp={"samples": 5, "profile": profile})
    if missing is not None:
        statuses = ["missing"] * missing + ["satisfied"]
        run.emit("conformance_checked", key="ship/finish", tree="t", enforce="block", passed=not missing,
                 obligations=[{"id": f"o{i}", "require": [], "status": s, "evidence": [], "note": ""}
                              for i, s in enumerate(statuses)])
    run.emit("run_finished")
    return run


def agreeing(cfg, repo, n: int = 5, **kw) -> list[Run]:
    return [past(cfg, repo, missing=0, **kw) for _ in range(n)]


def freeze(repo) -> None:
    scorecard.freeze(repo, None, None, None, False, datetime.now(UTC))


# --------------------------------------------------------------------------- observe and the checks


def test_observe_records_and_never_approves(flow):
    agreeing(flow.cfg, flow.root)
    freeze(flow.root)
    run = begin(flow, {REVIEW: [found(LOW)]})
    assert go(flow, run) == "awaiting_approval"
    assert run.state["waiting"]["key"] == "define_approved"
    [event] = events(run, "would_auto_approve")
    assert (event["phase"], event["mode"], event["verdict"]) == ("define", "observe", True)
    assert (event["held"], event["missing"], event["profile"]) == (list(CHECKS), [], "S")
    assert "eligible" not in event and "why" not in event
    assert not [e for e in events(run, "phase_approved") if e["phase"] == "define"]


def test_off_records_nothing(flow):
    flow.configure(PIPE, autopilot=True, edit=with_doubt)
    run = begin(flow, {REVIEW: [found(LOW)]})
    assert go(flow, run) == "awaiting_approval"
    assert not events(run, "would_auto_approve")


def test_a_bare_yaml_off_records_nothing(flow):
    flow.configure(ramp(False), autopilot=True, edit=with_doubt)  # YAML reads a bare `off` as False
    run = begin(flow, {REVIEW: [found(LOW)]})
    assert go(flow, run) == "awaiting_approval"
    assert not events(run, "would_auto_approve")


def test_verdict_lists_what_held_and_what_did_not(flow):
    run = begin(flow, {REVIEW: [found(LOW)]}, profile="M")
    assert go(flow, run) == "awaiting_approval"
    [event] = events(run, "would_auto_approve")
    assert event["verdict"] is False and event["missing"] == ["small_route"]
    assert event["held"] == ["no_open_findings", "no_security_overlay", "first_raise"]
    assert event["profile"] == "M"


def test_a_run_without_a_route_profile_is_not_small(flow):
    run = begin(flow, {REVIEW: [found(LOW)]}, profile=None)
    assert go(flow, run) == "awaiting_approval"
    [event] = events(run, "would_auto_approve")
    assert event["missing"] == ["small_route"] and "profile" not in event


def test_ramp_profiles_are_the_two_smallest():
    assert RAMP_PROFILES == ("XS", "S")
    assert CHECKS == ("no_open_findings", "small_route", "no_security_overlay", "first_raise")


# --------------------------------------------------------------------------- samples


def test_samples_count_only_a_persons_decision(tmp_git, cfg):
    rejected = past(cfg, tmp_git, "reject")
    past(cfg, tmp_git, "none")  # never decided: no sample
    past(cfg, tmp_git, profile="M")  # another profile
    past(cfg, tmp_git, verdict=False)  # the runner would not have approved: nothing to compare
    past(cfg, tmp_git, phase="plan")  # another phase
    past(cfg, tmp_git, lane="bug")  # another lane
    approved = past(cfg, tmp_git, "approve")
    auto_ok = past(cfg, tmp_git, "ramp", missing=0)
    auto_bad = past(cfg, tmp_git, "ramp", missing=1)
    got = samples(finished_runs(tmp_git, None), "feature", "S", "define")
    assert [(s.run_id, s.auto, s.agree) for s in got] == [
        (rejected.id, False, False), (approved.id, False, True), (auto_ok.id, True, True), (auto_bad.id, True, False)]
    assert all(isinstance(s, Sample) and s.ended.tzinfo is not None for s in got)
    assert [s.ended for s in got] == sorted(s.ended for s in got)


def test_a_runner_refine_round_is_not_a_decision(tmp_git, cfg):
    run = Run.create(cfg, tmp_git, "feature", {"module": "old"})
    run.emit("would_auto_approve", phase="define", mode="observe", verdict=True, held=[], missing=[], profile="S")
    run.emit("phase_rejected", by=AUTO_BY, phase="define", round=1, note="findings")
    run.emit("phase_approved", by="me", phase="define")
    run.emit("run_finished")
    [sample] = samples(finished_runs(tmp_git, None), "feature", "S", "define")
    assert (sample.auto, sample.agree) == (False, True)


def test_only_the_first_true_verdict_of_a_run_counts(tmp_git, cfg):
    run = Run.create(cfg, tmp_git, "feature", {"module": "old"})
    run.emit("would_auto_approve", phase="define", mode="observe", verdict=True, held=[], missing=[], profile="S")
    run.emit("phase_rejected", by="me", phase="define", round=1, note="chưa được")
    run.emit("would_auto_approve", phase="define", mode="observe", verdict=True, held=[], missing=[], profile="S")
    run.emit("phase_approved", by="me", phase="define")
    run.emit("run_finished")
    [sample] = samples(finished_runs(tmp_git, None), "feature", "S", "define")
    assert sample.agree is False


def test_the_ramps_own_approvals_never_promote_it(tmp_git, cfg):
    for _ in range(5):
        past(cfg, tmp_git, "ramp", missing=0)
    freeze(tmp_git)
    ok, why, n = eligible(tmp_git, cfg, "feature", "S", "define", datetime.now(UTC))
    assert (ok, why, n) == (False, "0 of 5 agreeing human decisions", 0)


def test_eligible_after_n_agreeing_human_decisions_and_a_baseline(tmp_git, cfg):
    past(cfg, tmp_git, "reject", missing=0)  # older than the last five: it no longer counts
    agreeing(cfg, tmp_git)
    freeze(tmp_git)
    assert eligible(tmp_git, cfg, "feature", "S", "define", datetime.now(UTC)) == (
        True, "5 of 5 agreeing human decisions", 5)
    assert eligible(tmp_git, cfg, "feature", "XS", "define", datetime.now(UTC))[0] is False  # per profile


def test_ramp_samples_sets_how_many(tmp_git, cfg, tmp_cfg):
    three = load_config(tmp_cfg({"autonomy": {"ramp_samples": 3}}))
    agreeing(cfg, tmp_git, 3)
    freeze(tmp_git)
    assert eligible(tmp_git, three, "feature", "S", "define", datetime.now(UTC))[0] is True
    assert eligible(tmp_git, cfg, "feature", "S", "define", datetime.now(UTC))[1] == "3 of 5 agreeing human decisions"


def test_a_worse_conformance_holds_the_ramp_back(tmp_git, cfg):
    agreeing(cfg, tmp_git)
    freeze(tmp_git)
    past(cfg, tmp_git, "none", missing=2)  # a run of the lane after the baseline missed obligations
    assert eligible(tmp_git, cfg, "feature", "S", "define", datetime.now(UTC))[:2] == (
        False, "conformance is worse than the baseline")


# --------------------------------------------------------------------------- auto


def test_auto_approves_after_n_agreeing_runs_and_a_green_scorecard(flow):
    flow.configure(ramp("auto"), autopilot=True, edit=with_doubt)
    agreeing(flow.cfg, flow.root)
    freeze(flow.root)
    run = begin(flow, {REVIEW: [found(LOW)]}, to=None)
    assert go(flow, run) == "awaiting_approval"
    [event] = events(run, "would_auto_approve")
    assert (event["mode"], event["verdict"], event["eligible"]) == ("auto", True, True)
    assert event["why"] == "5 of 5 agreeing human decisions"
    [approved] = [e for e in events(run, "phase_approved") if e["phase"] == "define"]
    assert approved["by"] == AUTO_BY and approved["ramp"] == {"samples": 5, "profile": "S"}
    assert approved["sha"]
    assert not [e for e in events(run, "phase_approval_raised") if e["phase"] == "define"]
    assert [e["phase"] for e in events(run, "handoff_written")][:1] == ["define"]
    assert PLAN in called(flow, "plan/")  # plan starts
    assert run.state["waiting"]["key"] == "plan_approved"


@pytest.mark.parametrize("case", ["security_path", "four_samples", "one_rejection", "no_baseline", "auto_regressed"])
def test_auto_falls_back_to_a_person(flow, case):
    flow.configure(ramp("auto", **(SECURITY if case == "security_path" else {})), autopilot=True, edit=with_doubt)
    cfg, root = flow.cfg, flow.root
    agreeing(cfg, root, 4 if case == "four_samples" else 5)
    if case == "one_rejection":
        past(cfg, root, "reject", missing=0)
    if case != "no_baseline":
        freeze(root)
    if case == "auto_regressed":
        past(cfg, root, "ramp", missing=1)
    run = begin(flow, {REVIEW: [found(LOW)]}, to=None)
    assert go(flow, run) == "awaiting_approval"
    assert run.state["waiting"]["key"] == "define_approved"
    assert not [e for e in events(run, "phase_approved") if e["phase"] == "define"]
    [event] = events(run, "would_auto_approve")
    assert event["mode"] == "auto" and event["eligible"] is False and event["why"]
    expected = {"security_path": "no_security_overlay", "four_samples": "4 of 5 agreeing human decisions",
                "one_rejection": "4 of 5 agreeing human decisions", "no_baseline": "no scorecard baseline",
                "auto_regressed": "an auto-approved run since missed an obligation"}[case]
    assert expected in event["why"]
    if case == "security_path":
        assert event["verdict"] is False and event["missing"] == ["no_security_overlay"]


def test_a_phase_rejected_once_is_never_auto(flow):
    flow.configure(ramp("auto"), autopilot=True, edit=with_doubt)
    run = begin(flow, {REVIEW: [found(LOW), found(LOW)], DRAFT: [DRAFTED, DRAFTED]}, to=None)
    assert go(flow, run) == "awaiting_approval"  # no samples yet: a person decides
    reject(run, "define", by="me", note="Thêm phần rollback")
    agreeing(flow.cfg, flow.root)  # the ramp is promoted meanwhile
    freeze(flow.root)
    assert go(flow, run) == "awaiting_approval"
    assert run.state["waiting"]["key"] == "define_approved"
    first, second = events(run, "would_auto_approve")
    assert first["verdict"] is True and first["why"] == "0 of 5 agreeing human decisions"
    assert second["verdict"] is False and second["missing"] == ["first_raise"] and second["eligible"] is False
    assert not [e for e in events(run, "phase_approved") if e["phase"] == "define"]


def test_goal_is_never_auto(flow, monkeypatch):
    monkeypatch.setattr(approval_ramp, "NEVER_AUTO", ("define",))  # `validate` refuses `goal: auto`; the runner too
    flow.configure(ramp("auto"), autopilot=True, edit=with_doubt)
    agreeing(flow.cfg, flow.root)
    freeze(flow.root)
    run = begin(flow, {REVIEW: [found(LOW)]}, to=None)
    assert go(flow, run) == "awaiting_approval"
    [event] = events(run, "would_auto_approve")
    assert event["verdict"] is True and event["eligible"] is False and event["why"]
    assert run.state["waiting"]["key"] == "define_approved"


# --------------------------------------------------------------------------- the view


def test_ramp_view_rows(tmp_git, tmp_cfg):
    cfg = load_config(tmp_cfg({"autonomy": {"approvals": {"define": "auto", "plan": "observe"}}}))
    agreeing(cfg, tmp_git)
    past(cfg, tmp_git, "ramp", missing=1)
    past(cfg, tmp_git, "reject", profile="XS", missing=0)
    past(cfg, tmp_git, "approve", profile="M", missing=0)  # not a ramp profile: no row
    freeze(tmp_git)
    rows = ramp_view(tmp_git, cfg, datetime.now(UTC))
    assert rows == [
        {"lane": "feature", "phase": "define", "profile": "XS", "mode": "auto", "human": 1, "agreeing": 0, "auto": 0,
         "regressions": 0, "eligible": False, "why": "0 of 5 agreeing human decisions"},
        {"lane": "feature", "phase": "define", "profile": "S", "mode": "auto", "human": 5, "agreeing": 5, "auto": 1,
         "regressions": 1, "eligible": False, "why": "an auto-approved run since missed an obligation"},
    ]
