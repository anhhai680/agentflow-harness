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


def planned(flow) -> dict:
    return {"write": {plan_path(flow): PLAN_TEXT}, **done("planned")}


def acts(flow, *plan_acts: dict) -> dict:
    """`define` reaches its approval clean, and `plan/plan` writes the plan (or does `plan_acts`)."""
    return {REVIEW: [found(LOW)], PLAN: list(plan_acts) or [planned(flow)]}


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


def test_approving_mid_speculation_goes_on_into_build_when_plan_needs_no_approval(flow, monkeypatch):
    flow.configure({**SPECULATE, "phases": {"plan": {"needs_approval": False}}}, autopilot=True, edit=with_doubt)
    run = begin(flow, acts(flow), to="build")
    real = Executor.run_step

    def run_step(self, phase, step, prefix):
        outcome = real(self, phase, step, prefix)
        if f"{prefix}/{step.id}" == PLAN and run.refresh()["status"] == "awaiting_approval":
            approve(run, "define", by="me")
        return outcome

    monkeypatch.setattr(Executor, "run_step", run_step)
    go(flow, run)
    assert called(flow, "plan/") == [PLAN, PLAN_REVIEW]
    assert events(run, "speculation_ended")[-1]["outcome"] == "approved"
    handoff = seq(run, "handoff_written", phase="define")
    assert handoff < seq(run, "phase_approved", phase="plan", by="runner") < seq(run, "handoff_written", phase="plan")
    built = [e["seq"] for e in events(run, "step_started") if e["key"].startswith("build/")]
    assert built and min(built) > seq(run, "handoff_written", phase="plan")  # build started in the same advance
    assert [e["phase"] for e in events(run, "phase_approval_raised")] == ["define"]


def test_a_speculative_stop_the_runner_settles_is_not_told(flow, notices):
    asks = {"result": {"status": "needs_human", "summary": "cần quyết định", "questions": [{**BOUNDARY, "kind": "spec_gap"}]}}
    run = begin(flow, acts(flow, asks, planned(flow)), to=None)
    assert go(flow, run) == "awaiting_approval"
    assert [w["key"] for w in run.state["waiting_queue"]] == [PLAN]
    assert not events(run, "hitl_answered")  # nothing is answered behind the approval
    approve(run, "define", by="me")
    assert go(flow, run) == "awaiting_approval"
    assert run.state["waiting"]["key"] == "plan_approved"
    assert [(e["key"], e["by"]) for e in events(run, "hitl_answered")] == [(PLAN, "runner")]
    assert [n for n in notices if "cần quyết định" in n] == [] and not events(run, "hitl_notified")


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


def test_a_handoff_with_speculation_off_still_lists_uncommitted_files(flow_repo):
    flow_repo.configure(PIPE, autopilot=True, edit=with_doubt)
    run = begin(flow_repo, acts(flow_repo), to="define")
    assert go(flow_repo, run) == "awaiting_approval"
    (Path(run.state["worktree"]) / "notes.txt").write_text("chưa commit\n", encoding="utf-8")
    approve(run, "define", by="me")
    go(flow_repo, run)
    text = (run.dir / "handoffs" / "define.md").read_text(encoding="utf-8")
    assert f"- `{SPEC}`" in text and "- `notes.txt`" in text  # `changed_since`, as before


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


# --------------------------------------------------------------------------- re-plan on a changed spec


def replans(run: Run) -> list[dict]:
    return [e for e in events(run, "phase_rejected") if e.get("kind") == speculation.REPLAN]


def reject_after(run: Run, monkeypatch, key: str, times: int = 1, skip: int = 0) -> list[bool]:
    """Reject `define` right after the walking runner's `key` returns, the first `times` times it runs (after the first
    `skip`): the person acts while the runner is between that step and the next."""
    real = Executor.run_step
    done_: list[bool] = []
    seen: list[bool] = []

    def run_step(self, phase, step, prefix):
        outcome = real(self, phase, step, prefix)
        if f"{prefix}/{step.id}" == key:
            seen.append(True)
            if len(seen) > skip and len(done_) < times:
                done_.append(True)
                reject(run, "define", "me", "Thêm rollback")
        return outcome

    monkeypatch.setattr(Executor, "run_step", run_step)
    return done_


def test_planned_on_and_replan_due_read_the_last_basis():
    log = [{"type": "speculation_started", "phase": "plan", "after": "define", "basis": {SPEC: "a"}, "sha": "1" * 40},
           {"type": "phase_rejected", "phase": "plan", "kind": "reconcile", "basis": {SPEC: "x"}},
           {"type": "phase_rejected", "phase": "plan", "kind": "replan", "basis": {SPEC: "b"}, "from_sha": "1" * 40},
           {"type": "speculation_started", "phase": "build", "after": "plan", "basis": {"p": "c"}, "sha": "3" * 40}]
    assert speculation.planned_on([], "plan") is None
    assert speculation.planned_on(log[:1], "plan") == ({SPEC: "a"}, "1" * 40)
    assert speculation.planned_on(log, "plan") == ({SPEC: "b"}, "1" * 40)  # a replan keeps the sha its diff starts at
    state = {"phases": {"plan": {"status": "running"}}}
    assert speculation.replan_due(state, log, "plan", {SPEC: "c"})
    assert not speculation.replan_due(state, log, "plan", {SPEC: "b"})
    assert not speculation.replan_due(state, log[:0], "plan", {SPEC: "c"})
    for status in (None, "pending", "skipped"):
        assert not speculation.replan_due({"phases": {"plan": {"status": status}}}, log, "plan", {SPEC: "c"})
    assert not speculation.replan_due({"phases": {}}, log, "plan", {SPEC: "c"})


def test_the_replan_note_names_the_paths_and_the_diff():
    note = speculation.replan_note(["a.md", "docs/b c.md"], "0123456789abcdef")
    assert note == ("The `a.md`, `docs/b c.md` changed after this phase's work was written (at 0123456789ab). Read the "
                    "change with `git diff 0123456789ab HEAD -- a.md 'docs/b c.md'`, then update what this phase wrote "
                    "to match it; keep what still holds.")  # the command runs as it is: quoted, space-joined paths


def test_rejecting_mid_speculation_refines_the_spec_then_replans(flow, monkeypatch):  # Review Focus 2
    run = begin(flow, {**acts(flow), DRAFT: [DRAFTED, revised(1)]}, to=None)
    rejected = reject_after(run, monkeypatch, PLAN)
    assert go(flow, run) == "awaiting_approval"  # the same runner ran define's round and raised it again
    assert rejected and called(flow, DRAFT) == [DRAFT, DRAFT]
    assert events(run, "speculation_ended")[0]["outcome"] == "rejected"
    (replan,) = replans(run)
    assert "git diff" in replan["note"] and SPEC in replan["note"]
    started = events(run, "speculation_started")
    assert len(started) == 2 and run.state["waiting_queue"] == []
    assert (replan["by"], replan["basis"], replan["from_sha"]) == ("runner", started[1]["basis"], started[0]["sha"])
    assert started[0]["basis"] != started[1]["basis"]
    raised = [e["seq"] for e in events(run, "phase_approval_raised") if e["phase"] == "define"]
    assert len(raised) == 2 and raised[1] < replan["seq"] < started[1]["seq"]
    assert called(flow, "plan/") == [PLAN, PLAN, PLAN_REVIEW]  # the yield came between plan's steps
    second = [c for c in flow.calls() if c["step"] == PLAN][1]
    assert replan["note"] in second["argv"][-1]  # the re-plan reads its note
    assert run.state["waiting"]["key"] == "define_approved" and not called(flow, "build/")
    assert (run.state["steps"][PLAN]["status"], run.state["steps"][PLAN_REVIEW]["status"]) == ("done", "done")


def test_a_rejection_that_leaves_the_spec_as_it_was_keeps_the_plan(flow):
    run = begin(flow, {**acts(flow), DRAFT: [DRAFTED, done("giữ nguyên")]}, to=None)
    assert go(flow, run) == "awaiting_approval"
    reject(run, "define", "me", "Xem lại")
    assert go(flow, run) == "awaiting_approval"
    assert called(flow, DRAFT) == [DRAFT, DRAFT] and not replans(run)
    assert len(events(run, "speculation_started")) == 1
    approve(run, "define", by="me")
    assert go(flow, run) == "awaiting_approval"
    assert run.state["waiting"]["key"] == "plan_approved" and not replans(run)
    assert called(flow, "plan/") == [PLAN, PLAN_REVIEW]


def test_a_spec_changed_without_a_new_speculation_is_replanned_after_approval(flow):
    flow.configure({**SPECULATE, "autonomy": {"speculate": ["plan"], "finding_rounds": 2}}, autopilot=True,
                   edit=with_doubt)
    run = begin(flow, {REVIEW: [found(LOW), found(HIGH), found(HIGH), found(HIGH)], PLAN: [planned(flow)],
                       DRAFT: [DRAFTED, revised(1)],
                       RECONCILE: [revised(2, disp("R1-F1", "actionable")), revised(3, disp("R2-F1", "actionable"))]},
                to=None)
    assert go(flow, run) == "awaiting_approval"
    assert len(events(run, "speculation_started")) == 1
    reject(run, "define", "me", "Thêm rollback")
    assert go(flow, run) == "awaiting_approval"
    assert events(run, "reconcile_stopped")[-1]["reason"] == "bounded"  # a blocking finding is left: no speculation
    assert len(events(run, "speculation_started")) == 1 and not replans(run)
    assert called(flow, "plan/") == [PLAN, PLAN_REVIEW]
    approve(run, "define", by="me")
    assert go(flow, run) == "awaiting_approval"
    (replan,) = replans(run)
    assert seq(run, "phase_approved", phase="define") < replan["seq"]
    assert replan["from_sha"] == events(run, "speculation_started")[0]["sha"] and SPEC in replan["note"]
    assert called(flow, "plan/") == [PLAN, PLAN_REVIEW, PLAN, PLAN_REVIEW]
    assert run.state["waiting"]["key"] == "plan_approved"
    assert seq(run, "handoff_written", phase="define") < replan["seq"]
    go(flow, run)
    assert len(replans(run)) == 1  # once per revision


def test_a_replan_is_not_counted_as_a_finding_round(flow, monkeypatch):
    from agentflow.autonomy import finding_rounds

    flow.configure({**SPECULATE, "autonomy": {"speculate": ["plan"], "finding_rounds": 1},
                    "phases": {"plan": {"needs_approval": False}}}, autopilot=True, edit=with_doubt)
    gap = {"severity": "high", "file": plan_path(flow), "line": 1, "text": "Thiếu bước rollback"}
    run = begin(flow, {**acts(flow), DRAFT: [DRAFTED, revised(1)], PLAN_REVIEW: [found(gap)]}, to="build")
    reject_after(run, monkeypatch, PLAN)
    assert go(flow, run) == "awaiting_approval"
    assert len(replans(run)) == 1
    rounds = [e for e in events(run, "phase_rejected") if e["phase"] == "plan" and e.get("kind") is None]
    assert len(rounds) == 1 and rounds[0]["seq"] > replans(run)[0]["seq"]  # the finding still got its round
    assert finding_rounds(run, "plan") == 1
    assert called(flow, "plan/") == [PLAN, PLAN, PLAN_REVIEW, PLAN, PLAN_REVIEW]


def test_a_spec_rejected_again_during_its_replan_is_replanned_once_more(flow, monkeypatch):  # duty (b)
    run = begin(flow, {**acts(flow), DRAFT: [DRAFTED, revised(1), revised(2)]}, to=None)
    reject_after(run, monkeypatch, PLAN, times=2)  # the second lands inside the first re-plan's round
    assert go(flow, run) == "awaiting_approval"
    started = events(run, "speculation_started")
    first, second = replans(run)
    assert len(started) == 3 and len({str(s["basis"]) for s in started}) == 3
    assert (first["basis"], second["basis"]) == (started[1]["basis"], started[2]["basis"])  # one per revision
    assert second["from_sha"] == started[1]["sha"]
    assert called(flow, "plan/") == [PLAN, PLAN, PLAN, PLAN_REVIEW]  # the cut round is planned again, whole
    assert (run.state["steps"][PLAN]["status"], run.state["steps"][PLAN_REVIEW]["status"]) == ("done", "done")
    assert (run.state["refine"]["phase"], run.state["refine"]["kind"]) == ("plan", speculation.REPLAN)
    assert run.state["waiting"]["key"] == "define_approved" and run.state["waiting_queue"] == []
    approve(run, "define", by="me")
    assert go(flow, run) == "awaiting_approval"
    assert run.state["waiting"]["key"] == "plan_approved" and len(replans(run)) == 2


def test_the_re_raised_spec_page_leaves_out_the_speculated_plan(flow):  # duty (e)
    run = begin(flow, {**acts(flow), DRAFT: [DRAFTED, revised(1)]}, to=None)
    assert go(flow, run) == "awaiting_approval"
    assert [e["key"] for e in events(run, "step_committed")].count(PLAN) == 1
    reject(run, "define", "me", "Thêm rollback")
    assert go(flow, run) == "awaiting_approval"
    page = run.inbox_path("define_approved").read_text(encoding="utf-8")
    assert "## Since the last approval request" in page and f"- `{SPEC}`" in page
    assert plan_path(flow) not in page and "Plan demo" not in page
    assert "SPEC-demo.md" in page.split("## Since the last approval request")[1].split("## ")[0]
    approve(run, "define", by="me")
    go(flow, run)
    text = (run.dir / "handoffs" / "define.md").read_text(encoding="utf-8")
    assert f"- `{SPEC}`" in text and plan_path(flow) not in text


def test_a_spec_left_as_it_was_during_its_replan_finishes_that_round_after_approval(flow, monkeypatch):  # duty (b)
    run = begin(flow, {**acts(flow), DRAFT: [DRAFTED, revised(1), done("giữ nguyên")]}, to=None)
    reject_after(run, monkeypatch, PLAN, times=2)  # the second lands inside the re-plan, and leaves the spec as it was
    assert go(flow, run) == "awaiting_approval"
    assert len(replans(run)) == 1 and len(events(run, "speculation_started")) == 2  # no new revision: no new re-plan
    assert called(flow, "plan/") == [PLAN, PLAN]
    approve(run, "define", by="me")
    assert go(flow, run) == "awaiting_approval"
    assert run.state["waiting"]["key"] == "plan_approved" and len(replans(run)) == 1
    assert called(flow, "plan/") == [PLAN, PLAN, PLAN_REVIEW]  # the round's step left goes on from its row


# --------------------------------------------------------------------------- fix round 1 (Rulings L, N, O, P)

PLAN_RECONCILE = "plan/plan_reconcile"


def test_a_replan_cut_before_its_step_ran_keeps_its_note_when_the_spec_stays(flow, monkeypatch):  # Ruling L (i)
    run = begin(flow, {**acts(flow), DRAFT: [DRAFTED, revised(1), done("giữ nguyên")]}, to=None)
    real = Executor.run_step
    seen: list[str] = []

    def run_step(self, phase, step, prefix):
        key = f"{prefix}/{step.id}"
        if key == PLAN:
            seen.append(key)
            if len(seen) == 2:  # the re-plan opened, its `plan/plan` has not run: the person rejects again
                reject(run, "define", "me", "Xem lại")
        outcome = real(self, phase, step, prefix)
        if key == PLAN and len(seen) == 1:
            reject(run, "define", "me", "Thêm rollback")
        return outcome

    monkeypatch.setattr(Executor, "run_step", run_step)
    assert go(flow, run) == "awaiting_approval"
    (replan,) = replans(run)
    assert called(flow, "plan/") == [PLAN]  # the re-plan's step was cut before it ran
    assert (run.state["refine"]["phase"], run.state["refine"]["kind"]) == ("plan", speculation.REPLAN)
    assert run.state["waiting"]["key"] == "define_approved"
    monkeypatch.setattr(Executor, "run_step", real)
    approve(run, "define", by="me")
    assert go(flow, run) == "awaiting_approval"
    assert run.state["waiting"]["key"] == "plan_approved" and len(replans(run)) == 1
    assert called(flow, "plan/") == [PLAN, PLAN, PLAN_REVIEW]
    second = [c for c in flow.calls() if c["step"] == PLAN][1]
    assert replan["note"] in second["argv"][-1]  # it ran as the re-plan, in refine mode with its note


def test_a_reconcile_round_cut_by_a_rejection_goes_on_as_one_when_the_spec_stays(flow, monkeypatch):  # Ruling L (ii)
    gap = {"severity": "high", "file": plan_path(flow), "line": 5, "text": "Task 1 không có lệnh kiểm thử"}
    run = begin(flow, {**acts(flow), DRAFT: [DRAFTED, done("giữ nguyên")], PLAN_REVIEW: [found(gap), done("sạch")],
                       PLAN_RECONCILE: [revised(1, disp("R1-F1", "actionable"), path=plan_path(flow),
                                                text=PLAN_TEXT + "\nRun: `true`\n")]}, to=None)
    reject_after(run, monkeypatch, PLAN_RECONCILE, skip=1)  # it is skipped in the first walk: reject once it ran
    assert go(flow, run) == "awaiting_approval"
    assert called(flow, "plan/") == [PLAN, PLAN_REVIEW, PLAN_RECONCILE]
    assert (run.state["refine"]["phase"], run.state["refine"]["kind"]) == ("plan", "reconcile")
    assert not replans(run) and run.state["waiting"]["key"] == "define_approved"
    approve(run, "define", by="me")
    assert go(flow, run) == "awaiting_approval"
    assert called(flow, "plan/") == [PLAN, PLAN_REVIEW, PLAN_RECONCILE, PLAN_REVIEW]
    second = [c for c in flow.calls() if c["step"] == PLAN_REVIEW][1]
    assert "Earlier reviews of this artifact found these" in second["argv"][-1]  # the round's reviewer, not a fresh one
    assert run.state["waiting"]["key"] == "plan_approved"


def test_the_spec_refine_prompt_leaves_out_the_speculated_plan(flow):  # Ruling N
    run = begin(flow, {**acts(flow), DRAFT: [DRAFTED, revised(1)]}, to=None)
    assert go(flow, run) == "awaiting_approval"
    reject(run, "define", "me", "Thêm rollback")
    assert go(flow, run) == "awaiting_approval"
    prompt = [c for c in flow.calls() if c["step"] == DRAFT][1]["argv"][-1]
    assert "Thêm rollback" in prompt and f"- `{SPEC}`" in prompt
    assert plan_path(flow) not in prompt


def test_a_rejection_drops_the_walked_phase_s_uncommitted_output(flow):  # Ruling O
    stops = {"write": {plan_path(flow): PLAN_TEXT, "notes.txt": "giữ\n"},
             "result": {"status": "needs_human", "summary": "cần quyết định", "questions": [BOUNDARY]}}
    run = begin(flow, {**acts(flow, stops, planned(flow)), DRAFT: [DRAFTED, revised(1)]}, to=None)
    assert go(flow, run) == "awaiting_approval"
    worktree = Path(run.state["worktree"])
    assert (worktree / plan_path(flow)).exists() and not [e for e in events(run, "step_committed") if e["key"] == PLAN]
    reject(run, "define", "me", "Thêm rollback")
    go(flow, run)
    sha = [e["sha"] for e in events(run, "step_committed") if e["key"] == DRAFT][-1]
    files = run_git(worktree, "show", "--name-only", "--format=", sha).split()
    assert SPEC in files and plan_path(flow) not in files
    assert (worktree / "notes.txt").exists()  # only the walked phase's `output` is put back


def test_a_new_basis_ends_the_open_record_it_replaces(flow, monkeypatch):  # Ruling P
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
    spec = Path(run.state["worktree"]) / SPEC
    spec.write_text(spec.read_text(encoding="utf-8") + "\nSửa tay.\n", encoding="utf-8")
    go(flow, run)
    log = [e for e in run.events.read() if e["type"] in ("speculation_started", "speculation_ended")]
    assert [(e["type"], e.get("outcome")) for e in log[:3]] == [
        ("speculation_started", None), ("speculation_ended", "stopped"), ("speculation_started", None)]
    assert len(replans(run)) == 1
