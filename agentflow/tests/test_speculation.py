"""Speculative planning (`docs/specs/agentflow-throughput-proposal.md` §4.1.5, slice T7): while `define` waits for its
approval, the runner walks `plan` from the proposed spec, keeps the approval as the run's wait, and keeps the plan when
the person approves. The `flow` fixture and the `begin`/`go` helpers are copies of `test_doubt_flow.py`'s, with
`autonomy.speculate: [plan]` in the patch."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from agentflow import speculation
from agentflow.conditions import make_ctx
from agentflow.hitl import approve, reject
from agentflow.lanes import Executor
from agentflow.pipeline import RunOptions, advance, approval_text, start
from agentflow.run_control import pause
from agentflow.state import Run
from conftest import run_git, with_doubt

PIPE = {"gate_sets": {"build": ["ok"], "verify": ["ok"], "setup": []}}
SPECULATE = {**PIPE, "autonomy": {"speculate": ["plan"]}}
SPEC = "docs/specs/SPEC-demo.md"
STATUS = "**Status: proposed** (pending approval)"  # `spec_awaits_approval` holds: the review runs
SPEC_TEXT = f"# Spec: `demo`\n\n{STATUS}\n\n## Objective\n\nMột mô-đun mẫu.\n\n## Open Questions\n\nNone for this module.\n"
PLAN_TEXT = "# Plan: demo\n\n### Task 1: First thing\n\n**Depends on:** none\n\n- [ ] **Step 1: Do it**\n"
HIGH = {"severity": "high", "file": SPEC, "line": 3, "text": "Thiếu trường hợp rollback"}
LOW = {"severity": "low", "file": SPEC, "line": 9, "text": "lỗi chính tả"}
DRAFT, REVIEW, RECONCILE = "define/draft", "define/spec_review", "define/spec_reconcile"
PLAN, PLAN_REVIEW = "plan/plan", "plan/plan_review"
BOUNDARY = {"id": "q1", "question": "Giữ ranh giới mô-đun?", "evidence": [], "kind": "boundary",
            "options": [{"key": "A", "label": "Giữ", "recommended": True}, {"key": "B", "label": "Đổi"}]}


def done(summary: str = "ok", **extra) -> dict:
    return {"result": {"status": "done", "summary": summary, **extra}}


def found(*findings: dict) -> dict:
    return done("tìm thấy", findings=list(findings))


def disp(finding: str, cls: str, note: str = "ok") -> dict:
    return {"finding": finding, "class": cls, "note": note}


def revised(n: int, *dispositions: dict, path: str = SPEC, text: str | None = None) -> dict:
    """A reconcile act that edits the artifact (a new line each time) and answers each finding."""
    body = text if text is not None else SPEC_TEXT.replace("Một mô-đun mẫu.", f"Một mô-đun mẫu.\n\nRollback {n}.")
    return {"write": {path: body}, **done(f"sửa lần {n}", dispositions=list(dispositions))}


DRAFTED = {"write": {SPEC: SPEC_TEXT}, **done("drafted")}


@pytest.fixture
def flow(flow_repo):
    flow_repo.configure(SPECULATE, autopilot=True, edit=with_doubt)
    return flow_repo


def begin(flow, acts: dict, to: str = "define", cfg=None) -> Run:
    flow.script({DRAFT: [DRAFTED], **acts})
    return start(cfg or flow.cfg, flow.root, "feature", {"module": "demo"}, RunOptions(to_phase=to))


def go(flow, run: Run, cfg=None) -> str:
    return advance(run, cfg or flow.cfg, RunOptions())


def called(flow, prefix: str = "define/") -> list[str]:
    return [c["step"] for c in flow.calls() if c["step"].startswith(prefix) and "/idea/" not in c["step"]]


def events(run: Run, etype: str) -> list[dict]:
    return [e for e in run.events.read() if e["type"] == etype]


def plan_path(flow) -> str:
    """The plan `plan/plan` writes: `plan_target` on the day the run starts (the run's `{date}`)."""
    params = {"module": "demo", "date": date.today().isoformat()}
    return make_ctx(flow.cfg, flow.root, "feature", params).values["plan_target"]


def acts(flow, *plan_acts: dict) -> dict:
    """`define` reaches its approval clean, and `plan/plan` writes the plan (or does `plan_acts`)."""
    planned = {"write": {plan_path(flow): PLAN_TEXT}, **done("planned")}
    return {REVIEW: [found(LOW)], PLAN: list(plan_acts) or [planned]}


def seq(run: Run, etype: str, **match) -> int:
    return next(e["seq"] for e in run.events.read()
                if e["type"] == etype and all(e.get(k) == v for k, v in match.items()))


@pytest.fixture
def notices(monkeypatch):
    sent: list[str] = []
    monkeypatch.setattr("agentflow.hitl.notify", lambda cfg, title, message, run=None: sent.append(message))
    return sent


# --------------------------------------------------------------------------- the plan while the spec waits


def test_the_plan_is_written_while_the_spec_waits(flow):
    run = begin(flow, acts(flow), to=None)
    assert go(flow, run) == "awaiting_approval"
    assert run.state["waiting"]["key"] == "define_approved"
    assert called(flow, "plan/") == [PLAN, PLAN_REVIEW]
    assert events(run, "speculation_ended")[-1]["outcome"] == "finished"
    assert not called(flow, "build/")  # build never starts before the approval
    started = events(run, "speculation_started")
    assert len(started) == 1 and (started[0]["phase"], started[0]["after"]) == ("plan", "define")
    assert list(started[0]["basis"]) == [SPEC] and len(started[0]["basis"][SPEC]) == 64
    assert (run.state["steps"][PLAN]["status"], run.state["steps"][PLAN_REVIEW]["status"]) == ("done", "done")
    assert (run.state["speculation"], run.state["status"]) == (None, "awaiting_approval")
    # the speculated phase never raises its approval or approves itself: either would overwrite the spec's wait
    assert not [e for e in run.events.read()
                if e["type"] in ("phase_approval_raised", "phase_approved") and e["phase"] == "plan"]
    assert (Path(run.state["worktree"]) / plan_path(flow)).read_text(encoding="utf-8") == PLAN_TEXT


def test_approving_after_speculation_keeps_the_plan(flow):
    run = begin(flow, acts(flow), to=None)
    assert go(flow, run) == "awaiting_approval"
    approve(run, "define", by="me")
    assert go(flow, run) == "awaiting_approval"
    assert run.state["waiting"]["key"] == "plan_approved"  # plan settles: its approval, as the fixture says
    assert called(flow, "plan/").count(PLAN) == 1 and called(flow, "plan/").count(PLAN_REVIEW) == 1
    assert len(events(run, "speculation_started")) == 1
    assert seq(run, "handoff_written", phase="define") < seq(run, "phase_approval_raised", phase="plan")
    assert not called(flow, "build/")


def test_approving_mid_speculation_goes_on_into_build(flow, monkeypatch):
    run = begin(flow, acts(flow), to=None)
    real = Executor.run_step
    approved: list[bool] = []

    def run_step(self, phase, step, prefix):
        outcome = real(self, phase, step, prefix)
        if f"{prefix}/{step.id}" == PLAN and not approved:
            approved.append(True)
            approve(run, "define", by="me")
        return outcome

    monkeypatch.setattr(Executor, "run_step", run_step)
    assert go(flow, run) == "awaiting_approval"
    assert approved
    assert run.state["waiting"]["key"] == "plan_approved"  # plan settled in the same advance
    assert called(flow, "plan/") == [PLAN, PLAN_REVIEW]
    assert events(run, "speculation_ended")[-1]["outcome"] == "approved"
    assert seq(run, "handoff_written", phase="define") < seq(run, "phase_approval_raised", phase="plan")
    assert not events(run, "hitl_raised")  # no second stop
    assert [e["phase"] for e in events(run, "phase_approval_raised")] == ["define", "plan"]
    assert not called(flow, "build/")  # plan's own approval comes first in this pack


def test_approving_with_hold_mid_speculation_rests_the_run(flow, monkeypatch):
    run = begin(flow, acts(flow), to=None)
    real = Executor.run_step

    def run_step(self, phase, step, prefix):
        outcome = real(self, phase, step, prefix)
        if f"{prefix}/{step.id}" == PLAN and run.refresh()["status"] == "awaiting_approval":
            approve(run, "define", by="me", hold=True)
        return outcome

    monkeypatch.setattr(Executor, "run_step", run_step)
    assert go(flow, run) == "ready"  # it yields at the next step boundary and rests, as a held approval does
    assert called(flow, "plan/") == [PLAN]
    assert events(run, "speculation_ended")[-1]["outcome"] == "approved"
    monkeypatch.setattr(Executor, "run_step", real)
    assert go(flow, run) == "awaiting_approval"  # a person goes on: plan resumes from its rows
    assert called(flow, "plan/") == [PLAN, PLAN_REVIEW] and run.state["waiting"]["key"] == "plan_approved"
    assert seq(run, "handoff_written", phase="define") < seq(run, "phase_approval_raised", phase="plan")


def test_a_speculative_stop_waits_quietly_behind_the_spec(flow, notices):
    run = begin(flow, acts(flow, {"result": {"status": "needs_human", "summary": "cần quyết định",
                                             "questions": [BOUNDARY]}}), to=None)
    assert go(flow, run) == "awaiting_approval"
    assert run.state["waiting"]["key"] == "define_approved"
    assert [n for n in notices if "cần quyết định" in n] == []
    queued = run.state["waiting_queue"]
    assert [(w["key"], w.get("speculative"), w.get("notified")) for w in queued] == [(PLAN, True, False)]
    assert events(run, "speculation_ended")[-1]["outcome"] == "stopped"
    approve(run, "define", by="me")
    assert go(flow, run) == "waiting_human"
    assert run.state["waiting"]["key"] == PLAN
    assert len([n for n in notices if "cần quyết định" in n]) == 1
    assert [e["key"] for e in events(run, "hitl_notified")] == [PLAN]
    assert go(flow, run) == "waiting_human"
    assert len([n for n in notices if "cần quyết định" in n]) == 1  # told once, whoever looks again
    assert len(events(run, "hitl_notified")) == 1


def test_a_runner_that_died_mid_speculation_resumes_it(flow, monkeypatch):
    run = begin(flow, acts(flow), to=None)
    real = Executor.run_step
    died: list[bool] = []

    def run_step(self, phase, step, prefix):
        if f"{prefix}/{step.id}" == PLAN_REVIEW and not died:
            died.append(True)
            raise KeyboardInterrupt
        return real(self, phase, step, prefix)

    monkeypatch.setattr(Executor, "run_step", run_step)
    with pytest.raises(KeyboardInterrupt):
        go(flow, run)
    assert run.refresh()["speculation"] is not None
    assert go(flow, run) == "awaiting_approval"
    assert len(events(run, "speculation_started")) == 1 and PLAN_REVIEW in called(flow, "plan/")
    assert called(flow, "plan/").count(PLAN) == 1
    assert events(run, "speculation_ended")[-1]["outcome"] == "finished"
    assert run.state["waiting"]["key"] == "define_approved" and not called(flow, "build/")


def test_a_paused_speculation_goes_on_where_it_stopped(flow, monkeypatch):
    run = begin(flow, acts(flow), to=None)
    real = Executor.run_step

    def run_step(self, phase, step, prefix):
        outcome = real(self, phase, step, prefix)
        if f"{prefix}/{step.id}" == PLAN and run.refresh()["status"] != "paused" and PLAN_REVIEW not in called(flow, "plan/"):
            pause(run, by="me")
        return outcome

    monkeypatch.setattr(Executor, "run_step", run_step)
    assert go(flow, run) == "paused"
    assert called(flow, "plan/") == [PLAN]
    assert run.state["speculation"] is not None and not events(run, "speculation_ended")
    monkeypatch.setattr(Executor, "run_step", real)
    assert go(flow, run) == "awaiting_approval"
    assert len(events(run, "speculation_started")) == 1 and called(flow, "plan/") == [PLAN, PLAN_REVIEW]
    assert run.state["waiting"]["key"] == "define_approved" and not called(flow, "build/")


def test_a_rejection_while_no_runner_walks_ends_the_speculation(flow, monkeypatch):
    run = begin(flow, acts(flow), to=None)
    real = Executor.run_step

    def run_step(self, phase, step, prefix):
        if f"{prefix}/{step.id}" == PLAN_REVIEW:
            raise KeyboardInterrupt
        return real(self, phase, step, prefix)

    monkeypatch.setattr(Executor, "run_step", run_step)
    with pytest.raises(KeyboardInterrupt):
        go(flow, run)
    monkeypatch.setattr(Executor, "run_step", real)
    reject(run, "define", by="me", note="Thêm phần rollback")
    flow.script({DRAFT: [revised(1)], REVIEW: [found(LOW)]})
    assert go(flow, run) == "awaiting_approval"
    log = run.events.read()
    ended = [e for e in log if e["type"] == "speculation_ended"]
    assert ended and ended[0]["outcome"] == "rejected"
    raised = [e["seq"] for e in log if e["type"] == "phase_approval_raised" and e["phase"] == "define"]
    assert ended[0]["seq"] < raised[-1]  # before the re-raised approval, so its stops are not queued on a stale record


def test_no_speculation_when_the_review_left_a_blocking_finding(flow):
    flow.configure({**SPECULATE, "autonomy": {"speculate": ["plan"], "finding_rounds": 2}}, autopilot=True,
                   edit=with_doubt)
    run = begin(flow, {REVIEW: [found(HIGH), found(HIGH), found(HIGH)],
                       RECONCILE: [revised(1, disp("R1-F1", "actionable")), revised(2, disp("R2-F1", "actionable"))]},
                to=None)
    assert go(flow, run) == "awaiting_approval"
    assert events(run, "reconcile_stopped")[-1]["reason"] == "bounded"
    assert not events(run, "speculation_started") and not called(flow, "plan/")
    assert "the runner drafts" not in approval_text(flow.cfg, run, "define")


def test_no_speculation_with_the_default_config(flow_repo):
    flow_repo.configure(PIPE, autopilot=True, edit=with_doubt)
    run = begin(flow_repo, acts(flow_repo), to=None)
    assert go(flow_repo, run) == "awaiting_approval"
    assert run.state["waiting"]["key"] == "define_approved"
    assert not called(flow_repo, "plan/")
    assert not [e for e in run.events.read() if e["type"].startswith("speculation_")]
    assert "speculation" not in run.state
    assert "the runner drafts" not in approval_text(flow_repo.cfg, run, "define")


def test_no_speculation_when_the_run_ends_at_the_spec(flow):
    run = begin(flow, acts(flow), to="define")
    assert go(flow, run) == "awaiting_approval"
    assert not events(run, "speculation_started") and not called(flow, "plan/")


def test_a_speculative_step_that_edits_code_stops(flow):
    edits = {"write": {plan_path(flow): PLAN_TEXT, "src/one.py": "x = 1\n"}, **done("planned")}
    run = begin(flow, acts(flow, edits), to=None)
    assert go(flow, run) == "awaiting_approval"
    assert run.state["waiting"]["key"] == "define_approved"
    assert [(w["kind"], w["key"]) for w in run.state["waiting_queue"]] == [("guard_violation", PLAN)]
    tripped = events(run, "guard_tripped")[-1]
    assert tripped["key"] == PLAN and "outside_output" in [v["kind"] for v in tripped["violations"]]
    assert not [e for e in events(run, "step_committed") if e["key"] == PLAN]
    log = run_git(Path(run.state["worktree"]), "log", "--name-only", "--format=", f"{run.state['base_sha']}..HEAD")
    assert "src/one.py" not in log and plan_path(flow) not in log
    assert PLAN_REVIEW not in called(flow, "plan/")


def test_the_spec_page_says_the_plan_is_being_drafted(flow):
    run = begin(flow, acts(flow), to=None)
    assert go(flow, run) == "awaiting_approval"
    line = ("While you review this, the runner drafts `plan` from it. Approving keeps that work; rejecting re-plans "
            "it on your revision.")
    assert line == speculation.page_line("define", "plan")
    assert line in approval_text(flow.cfg, run, "define")
    assert line in run.inbox_path("define_approved").read_text(encoding="utf-8")


def test_the_spec_handoff_lists_only_the_spec(flow):
    run = begin(flow, acts(flow), to=None)
    assert go(flow, run) == "awaiting_approval"
    approve(run, "define", by="me")
    assert go(flow, run) == "awaiting_approval"
    text = (run.dir / "handoffs" / "define.md").read_text(encoding="utf-8")
    assert f"- `{SPEC}`" in text
    assert plan_path(flow) not in text
