"""The reconcile round in the runner (`docs/agentflow-doubt-review-proposal.md`, sections 3 to 6): a doubt review's
blocking findings in `define` or `plan` open a round the runner runs by itself, before the human's approval.
Every test adds the four steps with `with_doubt`, so the `python` pack's own file (which pins the goldens) stays."""

from __future__ import annotations

import dataclasses
import io
import os
import re
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from agentflow import conditions, doubt, lanes, metrics
from agentflow.conditions import explain, make_ctx
from agentflow.feature_lane import _open_findings
from agentflow.hitl import answer, approve, reject
from agentflow.lanes import HANDOFF_LINE, Executor, build_prompt
from agentflow.pipeline import DEFAULT_SHOW, RunOptions, advance, approval_text, dry_run, start
from agentflow.state import Run
from conftest import run_git, with_doubt

PIPE = {"gate_sets": {"build": ["ok"], "verify": ["ok"], "setup": []}}
SPEC = "docs/specs/SPEC-demo.md"
STATUS = "**Status: proposed** (pending approval)"  # `spec_awaits_approval` holds: the review runs
SPEC_TEXT = f"# Spec: `demo`\n\n{STATUS}\n\n## Objective\n\nMột mô-đun mẫu.\n\n## Open Questions\n\nNone for this module.\n"
PLAN_TEXT = "# Plan: demo\n\n### Task 1: First thing\n\n**Depends on:** none\n\n- [ ] **Step 1: Do it**\n"
HIGH = {"severity": "high", "file": SPEC, "line": 3, "text": "Thiếu trường hợp rollback"}
LOW = {"severity": "low", "file": SPEC, "line": 9, "text": "lỗi chính tả"}
BLOCKING = {"severity": "blocking", "file": "src/one.py", "line": 3, "text": "Thiếu kiểm tra quyền"}
DRAFT, REVIEW, RECONCILE = "define/draft", "define/spec_review", "define/spec_reconcile"


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
    flow_repo.configure(PIPE, autopilot=True, edit=with_doubt)
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


def page(flow, run: Run, phase: str = "define") -> str:
    return approval_text(flow.cfg, run, phase)


# --------------------------------------------------------------------------- the round


def test_a_low_finding_goes_straight_to_approval(flow):
    run = begin(flow, {REVIEW: [found(LOW)]})
    assert go(flow, run) == "awaiting_approval"
    assert not events(run, "phase_rejected")
    assert RECONCILE not in called(flow)
    skipped = [e for e in events(run, "step_skipped") if e["key"] == RECONCILE]
    assert skipped and skipped[0]["reason"] == doubt.RECONCILE_ONLY
    assert "lỗi chính tả" in page(flow, run)
    assert events(run, "doubt_settled")[-1]["rounds"] == 0


def test_a_high_finding_gets_one_reconcile_round(flow):
    run = begin(flow, {REVIEW: [found(HIGH), done("sạch")], RECONCILE: [revised(1, disp("R1-F1", "actionable"))]})
    assert go(flow, run) == "awaiting_approval"
    assert called(flow) == [DRAFT, REVIEW, RECONCILE, REVIEW]
    rejected = events(run, "phase_rejected")
    assert len(rejected) == 1 and rejected[0]["kind"] == doubt.RECONCILE
    record = events(run, "findings_reconciled")[-1]
    assert record["before"] != record["after"]
    text = page(flow, run)
    assert re.search(r"R1-F1 .*actionable.*\(fixed\)", text)
    assert "(refine round" not in text
    log = run.events.read()
    approval = next(e["seq"] for e in log if e["type"] == "phase_approval_raised")
    assert not [e for e in log if e["type"] == "hitl_raised" and e["seq"] < approval]


@pytest.mark.parametrize("severity", ["critical", "high", "medium", None])
def test_each_of_critical_high_medium_and_no_severity_opens_a_round(flow, severity):
    finding = {**HIGH, "severity": severity} if severity else {k: v for k, v in HIGH.items() if k != "severity"}
    run = begin(flow, {REVIEW: [found(finding)]})
    assert go(flow, run) == "awaiting_approval"
    assert [e["kind"] for e in events(run, "phase_rejected")] == [doubt.RECONCILE]


def test_an_unchanged_artifact_is_not_reviewed_again(flow):
    run = begin(flow, {REVIEW: [found(HIGH)], RECONCILE: [done("không sửa", dispositions=[disp("R1-F1", "noise", "đã có ở mục 2")])]})
    assert go(flow, run) == "awaiting_approval"
    assert called(flow).count(REVIEW) == 1
    assert run.state["steps"][REVIEW]["status"] == "unchanged"
    assert events(run, "reconcile_stopped")[-1]["reason"] == "unchanged"
    assert re.search(r"R1-F1 .*noise: đã có ở mục 2 \(still open\)", page(flow, run))


def test_unchanged_uses_the_round_start_digest(flow):
    boundary = {"id": "R1-F1", "question": "Giữ thiết kế?", "evidence": [], "kind": "boundary",
                "options": [{"key": "A", "label": "Giữ"}, {"key": "B", "label": "Đổi"}]}
    edited = SPEC_TEXT.replace("Một mô-đun mẫu.", "Một mô-đun mẫu.\n\nRollback.")
    asks = {"write": {SPEC: edited}, "result": {"status": "needs_human", "summary": "cần", "questions": [boundary]}}
    run = begin(flow, {REVIEW: [found(HIGH), done("sạch")],
                       RECONCILE: [asks, done("xong", dispositions=[disp("R1-F1", "actionable")])]})
    assert go(flow, run) == "waiting_human"
    answer(run, RECONCILE, {"R1-F1": "A"}, by="me")
    assert go(flow, run) == "awaiting_approval"
    opened = events(run, "phase_rejected")[-1]
    record = events(run, "findings_reconciled")[-1]
    assert record["before"] == opened["digests"][RECONCILE]
    assert record["before"] != record["after"]
    assert called(flow).count(REVIEW) == 2


def test_a_lost_findings_reconciled_is_recovered(flow, monkeypatch):
    real = Executor._reconciled
    crashed: list[bool] = []

    def once(self, phase, key):
        if not crashed:
            crashed.append(True)
            raise RuntimeError("the runner died")
        return real(self, phase, key)

    monkeypatch.setattr(Executor, "_reconciled", once)
    run = begin(flow, {REVIEW: [found(HIGH)], RECONCILE: [done("không sửa", dispositions=[disp("R1-F1", "noise")])]})
    with pytest.raises(RuntimeError):
        go(flow, run)
    assert not events(run, "findings_reconciled")
    assert go(flow, run) == "awaiting_approval"
    log = run.events.read()
    record = next(e for e in log if e["type"] == "findings_reconciled")
    carried = next(e for e in log if e["type"] == "step_unchanged" and e["key"] == REVIEW)
    assert record["seq"] < carried["seq"]
    assert called(flow).count(REVIEW) == 1


def test_a_lost_findings_reconciled_is_recovered_when_the_review_is_skipped(flow, monkeypatch):
    """The runner dies after the reconcile step, and its edit dropped the status line, so the review is skipped and
    never recovers the record: settling the phase writes it, once, before the stop."""
    real = Executor._reconciled
    crashed: list[bool] = []

    def once(self, phase, key):
        if not crashed:
            crashed.append(True)
            raise RuntimeError("the runner died")
        return real(self, phase, key)

    monkeypatch.setattr(Executor, "_reconciled", once)
    no_status = SPEC_TEXT.replace(STATUS + "\n\n", "")
    run = begin(flow, {REVIEW: [found(HIGH)], RECONCILE: [revised(1, disp("R1-F1", "actionable"), text=no_status)]})
    with pytest.raises(RuntimeError):
        go(flow, run)
    assert not events(run, "findings_reconciled")
    assert go(flow, run) == "waiting_human"
    log = run.events.read()
    records = [e for e in log if e["type"] == "findings_reconciled"]
    stop = next(e for e in log if e["type"] == "reconcile_changed_conditions")
    assert len(records) == 1 and records[0]["seq"] < stop["seq"]
    assert records[0]["unresolved"] == [] and records[0]["before"] != records[0]["after"]
    assert REVIEW in stop["changed"] and called(flow).count(REVIEW) == 1


def test_a_reconcile_edit_that_skips_the_review_stops(flow):
    no_status = SPEC_TEXT.replace(STATUS + "\n\n", "")
    run = begin(flow, {REVIEW: [found(HIGH), done("sạch")],
                       RECONCILE: [revised(1, disp("R1-F1", "actionable"), text=no_status)]})
    assert go(flow, run) == "waiting_human"
    stop = events(run, "reconcile_changed_conditions")[-1]
    assert REVIEW in stop["changed"]
    assert run.state["waiting"]["key"] == RECONCILE
    assert not events(run, "phase_approval_raised")


def test_a_reconcile_edit_that_makes_grilling_due_stops(flow):
    grill = SPEC_TEXT.replace("None for this module.", "- Lưu ở đâu?")
    run = begin(flow, {REVIEW: [found(HIGH), done("sạch")],
                       RECONCILE: [revised(1, disp("R1-F1", "actionable"), text=grill)]})
    assert go(flow, run) == "waiting_human"
    stop = events(run, "reconcile_changed_conditions")[-1]
    assert "define/grill_open_questions" in stop["changed"]
    assert called(flow).count(REVIEW) == 1
    assert not events(run, "phase_approval_raised")


def test_a_nested_review_runs_again(flow_repo):
    def nest(data: dict) -> None:
        with_doubt(data)
        steps = data["phases"]["define"]["steps"]
        at = next(i for i, step in enumerate(steps) if step["id"] == "spec_review")
        steps[at] = {"id": "grp", "lanes": ["feature"], "steps": [steps[at]]}

    flow_repo.configure(PIPE, autopilot=True, edit=nest)
    nested = "define/grp/spec_review"
    run = begin(flow_repo, {nested: [found(HIGH), done("sạch")], RECONCILE: [revised(1, disp("R1-F1", "actionable"))]})
    assert go(flow_repo, run) == "awaiting_approval"
    assert called(flow_repo).count(nested) == 2
    assert events(run, "phase_rejected")[-1]["keys"] == [RECONCILE, "define/grp", nested]


def test_a_fixed_finding_is_not_left_open_in_the_pr_body(flow):
    run = begin(flow, {REVIEW: [found(HIGH), done("sạch")], RECONCILE: [revised(1, disp("R1-F1", "actionable"))]})
    assert go(flow, run) == "awaiting_approval"
    recorded = [e["findings"] for e in events(run, "step_findings") if e["key"] == REVIEW]
    assert len(recorded) == 2 and recorded[0][0]["text"] == HIGH["text"] and recorded[-1] == []
    assert not [line for line in _open_findings(run.state, run.events.read()) if REVIEW in line]


def test_a_human_round_after_reconcile_rounds_is_round_1(flow):
    run = begin(flow, {REVIEW: [found(HIGH), found(HIGH), found(HIGH), done("sạch")],
                       RECONCILE: [revised(1, disp("R1-F1", "actionable")), revised(2, disp("R2-F1", "actionable"))]})
    assert go(flow, run) == "awaiting_approval"
    assert len(events(run, "phase_rejected")) == 2
    assert events(run, "reconcile_stopped")[-1]["reason"] == "bounded"
    reject(run, "define", by="me", note="Viết lại phần rollback")
    human = events(run, "phase_rejected")[-1]
    assert human["round"] == 1 and "kind" not in human
    assert go(flow, run) == "awaiting_approval"
    assert "(refine round 1)" in page(flow, run)  # the same check that finds no "(refine round" after reconcile rounds


def test_rounds_are_bounded(flow):
    flow.configure({**PIPE, "autonomy": {"finding_rounds": 2}}, autopilot=True, edit=with_doubt)
    run = begin(flow, {REVIEW: [found(HIGH), found(HIGH), found(HIGH)],
                       RECONCILE: [revised(1, disp("R1-F1", "actionable")), revised(2, disp("R2-F1", "actionable"))]})
    assert go(flow, run) == "awaiting_approval"
    assert [e.get("kind") for e in events(run, "phase_rejected")] == [doubt.RECONCILE] * 2
    assert events(run, "reconcile_stopped")[-1]["reason"] == "bounded"
    settled = events(run, "doubt_settled")[-1]
    assert settled["rounds"] == 2 and settled["reviews"] == 3 and settled["stop"] == "bounded"


def test_a_disputed_finding_goes_to_the_human(flow):
    dispute = {**HIGH, "disputes": "R1-F1", "text": "Vẫn thiếu rollback"}
    run = begin(flow, {REVIEW: [found(HIGH), found(dispute)], RECONCILE: [revised(1, disp("R1-F1", "noise", "không cần"))]})
    assert go(flow, run) == "awaiting_approval"
    assert len(events(run, "phase_rejected")) == 1
    assert events(run, "reconcile_stopped")[-1]["reason"] == "disputed"
    assert "(disputed)" in page(flow, run)


def test_a_human_decision_waits_for_a_human(flow):
    boundary = {"id": "R1-F1", "question": "Đổi quyết định đã chốt?", "evidence": [], "kind": "boundary",
                "options": [{"key": "A", "label": "Giữ", "recommended": True}, {"key": "B", "label": "Đổi"}]}
    asks = {"result": {"status": "needs_human", "summary": "cần người", "questions": [boundary]}}
    run = begin(flow, {REVIEW: [found(HIGH)],
                       RECONCILE: [asks, done("ghi lại", dispositions=[disp("R1-F1", "human_decision", "người đã chốt")])]})
    assert flow.cfg.autonomy.get("answer_recommended") is True
    assert go(flow, run) == "waiting_human"
    assert not [e for e in events(run, "hitl_answered") if e.get("by") == "runner"]
    answer(run, RECONCILE, {"R1-F1": "A"}, by="me")
    assert go(flow, run) == "awaiting_approval"
    record = events(run, "findings_reconciled")[-1]
    assert record["unresolved"] == [] and record["dispositions"][0]["class"] == "human_decision"


def test_a_human_rejection_runs_every_step_and_skips_the_reconcile_step(flow):
    run = begin(flow, {REVIEW: [found(HIGH), done("sạch"), found(HIGH), done("sạch")],
                       RECONCILE: [revised(1, disp("R1-F1", "actionable")), revised(2, disp("R1-F1", "actionable"))]})
    assert go(flow, run) == "awaiting_approval"
    reject(run, "define", by="me", note="Thêm phần giới hạn")
    human = events(run, "phase_rejected")[-1]["seq"]
    assert go(flow, run) == "awaiting_approval"
    after = [e for e in run.events.read() if e["seq"] > human]
    started = [e["key"] for e in after if e["type"] == "step_started"]
    assert DRAFT in started and REVIEW in started
    skipped = [e for e in after if e["type"] == "step_skipped" and e["key"] == RECONCILE]
    assert skipped and skipped[0]["reason"] == doubt.RECONCILE_ONLY
    assert started.index(DRAFT) < started.index(REVIEW) < started.index(RECONCILE)  # then fresh rounds
    rounds = [e for e in after if e["type"] == "phase_rejected"]
    assert [(e["kind"], e["round"], e["findings"][0]["id"]) for e in rounds] == [(doubt.RECONCILE, 1, "R1-F1")]
    assert run.state["phases"]["define"]["round"] == 1


def test_the_plan_phase_reconciles_the_same_way(flow):
    run = begin(flow, {}, to="plan")
    assert go(flow, run) == "awaiting_approval"
    approve(run, "define", by="me")
    plan = make_ctx(flow.cfg, flow.root, "feature", run.state["params"], run).values["plan_target"]
    finding = {"severity": "high", "file": plan, "line": 5, "text": "Task 1 không có lệnh kiểm thử"}
    flow.script({"plan/plan": [{"write": {plan: PLAN_TEXT}, **done("planned")}],
                 "plan/plan_review": [found(finding), done("sạch")],
                 "plan/plan_reconcile": [revised(1, disp("R1-F1", "actionable"), path=plan,
                                                 text=PLAN_TEXT + "\nRun: `true`\n")]})
    assert go(flow, run) == "awaiting_approval"
    assert called(flow, "plan/") == ["plan/plan", "plan/plan_review", "plan/plan_reconcile", "plan/plan_review"]
    rejected = [e for e in events(run, "phase_rejected") if e["phase"] == "plan"]
    assert [e["kind"] for e in rejected] == [doubt.RECONCILE]
    assert re.search(r"R1-F1 .*\(fixed\)", page(flow, run, "plan"))


def test_explain_and_dry_run_skip_the_reconcile_steps(flow):
    (flow.root / "docs" / "specs").mkdir(parents=True)
    (flow.root / SPEC).write_text(SPEC_TEXT, encoding="utf-8")
    ctx = make_ctx(flow.cfg, flow.root, "feature", {"module": "demo"})
    lines = {line.key: line for line in explain(flow.cfg, "feature", ctx) if line.verdict != "approval"}
    for key in (RECONCILE, "plan/plan_reconcile"):
        assert (lines[key].verdict, lines[key].reason) == ("skip", doubt.RECONCILE_ONLY)
    assert lines[REVIEW].verdict == "run"
    out = io.StringIO()
    dry_run(flow.cfg, flow.root, "feature", {"module": "demo"}, RunOptions(), out=out)
    text = out.getvalue()
    assert f"– skip {RECONCILE} ({doubt.RECONCILE_ONLY})" in text
    assert f"– skip plan/plan_reconcile ({doubt.RECONCILE_ONLY})" in text


def test_reconcile_rounds_are_not_rework(flow):
    run = begin(flow, {REVIEW: [found(HIGH), done("sạch")], RECONCILE: [revised(1, disp("R1-F1", "actionable"))]})
    assert go(flow, run) == "awaiting_approval"
    assert events(run, "phase_rejected")
    assert metrics.run_metrics(run.events.read(), datetime.now(UTC))["rework_cost_usd"] == 0


# --------------------------------------------------------------------------- what stays as it was


TITLES = {"handoff": "Handoff (written when you approve)", "diff": "Diff", "step_results": "Step results",
          "skipped_steps": "Skipped steps", "gates": "Gates", "review_findings": "Review findings"}


def test_a_phase_without_a_reconcile_step_is_as_before(flow_repo):
    flow_repo.configure(PIPE, autopilot=False)
    plan = "docs/plans/2026-10-29-demo.md"
    approved = SPEC_TEXT.replace(STATUS, "**Status: approved** 2026-10-01")
    flow_repo.script({DRAFT: [{"write": {SPEC: approved}, **done("drafted")}],
                      "plan/plan": [{"write": {plan: PLAN_TEXT}, **done("planned")}],
                      "build/build/task-1/implement": [{"write": {"src/one.py": "x = 1\n"}, **done("xong")}],
                      "review/branch_review": [found(BLOCKING)]})
    run = start(flow_repo.cfg, flow_repo.root, "feature", {"module": "demo"}, RunOptions(to_phase="review"))
    for _ in range(6):
        assert go(flow_repo, run) == "awaiting_approval"
        if run.state["waiting"]["phase"] == "review":
            break
        approve(run, run.state["waiting"]["phase"], by="me")
    assert run.state["waiting"]["phase"] == "review"
    for etype in ("phase_rejected", "reconcile_stopped", "doubt_settled", "findings_reconciled"):
        assert not events(run, etype), etype
    approval_phases = {name for name, phase in flow_repo.cfg.phases.items() if phase.needs_approval}
    assert not [e for e in events(run, "step_findings") if e["key"].split("/")[0] in approval_phases]
    text = page(flow_repo, run, "review")
    show = flow_repo.cfg.phase_approval.get("show", DEFAULT_SHOW)
    assert re.findall(r"^## (.+)$", text, re.M) == [TITLES[item] for item in show if item in TITLES]
    assert "Doubt review" not in text
    assert "blocking finding(s) are still open" in text


# --------------------------------------------------------------------------- the reconcile step edits only its artifact


STRAY = "src/stray_code.py"
EDITED = SPEC_TEXT.replace("Một mô-đun mẫu.", "Một mô-đun mẫu.\n\nRollback.")
EDITS_CODE = {"write": {SPEC: EDITED, STRAY: "x = 1\n"}, **done("sửa cả mã", dispositions=[disp("R1-F1", "actionable")])}


def worktree(run: Run) -> Path:
    return Path(run.state["worktree"])


def tripped(run: Run, key: str = RECONCILE) -> list[list[dict]]:
    return [e["violations"] for e in events(run, "guard_tripped") if e["key"] == key]


def committed(run: Run, key: str = RECONCILE) -> list[str]:
    return [e["sha"] for e in events(run, "step_committed") if e["key"] == key]


def files_of(run: Run, sha: str) -> list[str]:
    return [p for p in run_git(worktree(run), "show", "--name-only", "--format=", sha).split("\n") if p]


def test_a_reconcile_step_that_edits_code_is_refused(flow):
    run = begin(flow, {REVIEW: [found(HIGH)], RECONCILE: [EDITS_CODE]})
    assert go(flow, run) == "waiting_human"
    assert run.state["waiting"]["kind"] == "guard_violation" and run.state["waiting"]["key"] == RECONCILE
    assert tripped(run) == [[{"kind": "outside_output", "detail": repr(STRAY)}]]
    assert committed(run) == []
    assert not events(run, "forbidden_reverted")
    assert (worktree(run) / STRAY).is_file()  # left for the human, never reverted by the runner


def test_a_stray_file_is_never_committed_on_retry(flow):
    run = begin(flow, {REVIEW: [found(HIGH), done("sạch")],
                       RECONCILE: [EDITS_CODE, revised(2, disp("R1-F1", "actionable"))]})
    assert go(flow, run) == "waiting_human"
    approve(run, RECONCILE, by="me")
    assert go(flow, run) == "waiting_human"  # the retry stops at its start: the stray file is still there
    assert tripped(run) == [[{"kind": "outside_output", "detail": repr(STRAY)}]] * 2
    assert called(flow).count(RECONCILE) == 1 and committed(run) == []
    (worktree(run) / STRAY).unlink()
    approve(run, RECONCILE, by="me")
    assert go(flow, run) == "awaiting_approval"
    assert called(flow).count(RECONCILE) == 2
    shas = committed(run)
    assert len(shas) == 1 and files_of(run, shas[0]) == [SPEC]
    assert "Rollback 2." in run_git(worktree(run), "show", f"{shas[0]}:{SPEC}")


def test_a_reconcile_step_may_not_delete_its_artifact(flow):
    deletes = {"delete": [SPEC], **done("xoá", dispositions=[disp("R1-F1", "actionable")])}
    run = begin(flow, {REVIEW: [found(HIGH)], RECONCILE: [deletes]})
    assert go(flow, run) == "waiting_human"
    assert tripped(run) == [[{"kind": "outside_output", "detail": f"{SPEC!r} was deleted or is not a regular file"}]]
    assert committed(run) == []
    assert SPEC in run_git(worktree(run), "ls-tree", "-r", "--name-only", "HEAD").split("\n")


def test_the_allowed_path_survives_a_resume_on_another_day(flow, monkeypatch):
    run = begin(flow, {}, to="plan")
    assert go(flow, run) == "awaiting_approval"
    approve(run, "define", by="me")
    today = date.today()
    plan = make_ctx(flow.cfg, flow.root, "feature", run.state["params"], run).values["plan_target"]
    assert today.isoformat() in plan

    class Tomorrow(date):
        @classmethod
        def today(cls):
            return date.fromordinal(today.toordinal() + 1)

    monkeypatch.setattr(conditions, "date", Tomorrow)
    finding = {"severity": "high", "file": plan, "line": 5, "text": "Task 1 không có lệnh kiểm thử"}
    flow.script({"plan/plan": [{"write": {plan: PLAN_TEXT}, **done("planned")}],
                 "plan/plan_review": [found(finding), done("sạch")],
                 "plan/plan_reconcile": [revised(1, disp("R1-F1", "actionable"), path=plan,
                                                 text=PLAN_TEXT + "\nRun: `true`\n")]})
    assert go(flow, run) == "awaiting_approval"
    wrote = committed(run, "plan/plan")
    assert len(wrote) == 1 and files_of(run, wrote[0]) == [plan]
    prompt = next(c for c in flow.calls() if c["step"] == "plan/plan_reconcile")["argv"][-1]
    assert f"The artifact is `{plan}`" in prompt
    revised_sha = committed(run, "plan/plan_reconcile")
    assert len(revised_sha) == 1 and files_of(run, revised_sha[0]) == [plan]
    assert run.state["params"]["date"] == today.isoformat()  # pinned when the run started


# --------------------------------------------------------------------------- the doubt reviewer sees no author notes


CODEMAP = "`app/demo.py:1` `CODEMAP-X()`: the demo entry"
HANDOFF_LEAD = HANDOFF_LINE.split("`")[0]
FOUR_TASKS = "# Plan: demo\n\n" + "\n".join(
    f"### Task {n}: Thing {n}\n\n**Depends on:** none\n\n- [ ] **Step 1: Do thing {n}**\n" for n in range(1, 5))


def prompts_of(flow, step: str) -> list[str]:
    return [c["argv"][-1] for c in flow.calls() if c["step"] == step]


@pytest.mark.parametrize("autopilot", [True, False])
def test_the_doubt_reviewer_prompt_has_no_author_notes(flow, autopilot):
    flow.configure(PIPE, autopilot=autopilot, edit=with_doubt)
    run = begin(flow, {}, to="build")
    assert go(flow, run) == "awaiting_approval"
    approve(run, "define", by="me")
    plan = make_ctx(flow.cfg, flow.root, "feature", run.state["params"], run).values["plan_target"]
    tasks = {f"build/build/task-{n}/implement": [{"write": {f"src/t{n}.py": f"x = {n}\n"}, **done("xong")}]
             for n in range(1, 5)}
    flow.script({"plan/plan": [{"write": {plan: FOUR_TASKS}, **done("planned", code_map=[CODEMAP])}], **tasks})
    assert go(flow, run) == "awaiting_approval" and run.state["waiting"]["phase"] == "plan"
    assert run.handoff_path().is_file()
    [author] = prompts_of(flow, "plan/plan")
    assert HANDOFF_LEAD in author  # the plan's author was pointed at the handoff, so the review's prompt could be too
    [review] = prompts_of(flow, "plan/plan_review")
    assert "CODEMAP-X" not in review and "Code this run has mapped" not in review
    assert HANDOFF_LEAD not in review and "{code_map}" not in review
    preamble, marker, _body = review.partition("## The step")
    assert marker and "\n\n\n" not in preamble  # the empty code-map slot went whole
    assert "Do not open or append to" in review
    approve(run, "plan", by="me")
    go(flow, run)
    implement = prompts_of(flow, "build/build/task-1/implement")
    task_review = prompts_of(flow, "build/build/task-1/review")
    assert implement and task_review
    assert "CODEMAP-X" in implement[0] and "CODEMAP-X" in task_review[0]
    assert HANDOFF_LEAD in implement[0] and HANDOFF_LEAD in task_review[0]


def test_build_prompt_without_a_phase_is_unchanged(flow, monkeypatch):
    run = Run.create(flow.cfg, flow.root, "feature", {"module": "demo"})
    run.emit("code_noted", text=CODEMAP, key="plan/plan")
    run.handoff_path().parent.mkdir(parents=True, exist_ok=True)
    run.handoff_path().write_text("# Handoff\n", encoding="utf-8")
    ctx = make_ctx(flow.cfg, flow.root, "feature", run.state["params"], run)
    review = next(step for step in flow.cfg.phases["plan"].steps if step.id == "plan_review")
    with_phase = build_prompt(flow.cfg, review, ctx, {"role": "review"}, {"phase": "plan"}, "first")
    assert "CODEMAP-X" not in with_phase  # the same step, told its phase, is a doubt reviewer
    plain = build_prompt(flow.cfg, review, ctx, {"role": "review"}, {}, "first")
    assert "CODEMAP-X" in plain and HANDOFF_LEAD in plain
    unflagged = dataclasses.replace(review, blocks_on_findings=False)  # a review that can never be a doubt reviewer
    assert plain == build_prompt(flow.cfg, unflagged, ctx, {"role": "review"}, {}, "first")
    assert with_phase != build_prompt(flow.cfg, unflagged, ctx, {"role": "review"}, {"phase": "plan"}, "first")
    monkeypatch.setattr(doubt, "is_doubt_reviewer", lambda *_a, **_k: False)
    assert build_prompt(flow.cfg, review, ctx, {"role": "review"}, {"phase": "plan"}, "first") != with_phase


def test_a_reconcile_step_is_never_reverted_by_the_resolver(flow_repo):
    """`define` resolves its stops here, and a forbidden edit of the draft step is put back (the resolver is live);
    the reconcile step's same edit, which is also outside its artifact, stops for a human with nothing reverted."""
    flow_repo.configure({**PIPE, "phases": {"define": {"resolve": True}}}, autopilot=True, edit=with_doubt)
    forbidden = "CONSTRAINTS.md"
    flow_repo.script({DRAFT: [{"write": {SPEC: SPEC_TEXT, forbidden: "weaker\n"}, **done("drafted")}, DRAFTED],
                      REVIEW: [found(HIGH)],
                      RECONCILE: [{"write": {SPEC: EDITED, forbidden: "weaker\n"},
                                   **done("sửa", dispositions=[disp("R1-F1", "actionable")])}]})
    run = start(flow_repo.cfg, flow_repo.root, "feature", {"module": "demo"}, RunOptions(to_phase="define"))
    assert go(flow_repo, run) == "waiting_human"
    reverted = events(run, "forbidden_reverted")
    assert [e["key"] for e in reverted] == [DRAFT]  # the resolver put the draft's edit back
    assert run.state["waiting"]["kind"] == "guard_violation" and run.state["waiting"]["key"] == RECONCILE
    kinds = {v["kind"] for v in tripped(run)[-1]}
    assert kinds == {"forbidden_path", "outside_output"}
    assert (worktree(run) / forbidden).read_text(encoding="utf-8") == "weaker\n"  # left for the human
    assert committed(run) == []


def test_the_reconcile_commit_holds_only_the_artifact_even_unchecked(flow, monkeypatch):
    monkeypatch.setattr(lanes, "outside_only", lambda *_a, **_k: [])  # every tree check blinded
    monkeypatch.setattr(lanes, "git_state_changes", lambda *_a, **_k: [])
    run = begin(flow, {REVIEW: [found(HIGH), done("sạch")], RECONCILE: [EDITS_CODE]})
    assert go(flow, run) == "awaiting_approval"
    [sha] = committed(run)
    assert files_of(run, sha) == [SPEC]
    assert (worktree(run) / STRAY).is_file()
    assert run_git(worktree(run), "status", "--porcelain", "--untracked-files=all").strip() == f"?? {STRAY}"


def test_a_planted_attribute_filter_is_refused_before_it_runs(flow, tmp_path):
    marker = tmp_path / "filter-ran"
    script = tmp_path / "filter.sh"
    script.write_text(f'#!/bin/sh\necho ran >> "{marker}"\ncat\n', encoding="utf-8")
    script.chmod(0o755)
    run_git(flow.root, "config", "filter.x.clean", str(script))  # a driver no path uses yet
    common = Path(run_git(flow.root, "rev-parse", "--path-format=absolute", "--git-common-dir").strip())
    attributes = common / "info" / "attributes"
    plants = {"write": {SPEC: EDITED, str(attributes): "* filter=x\n"},
              **done("sửa", dispositions=[disp("R1-F1", "actionable")])}
    run = begin(flow, {REVIEW: [found(HIGH)], RECONCILE: [plants]})
    assert go(flow, run) == "waiting_human"
    [[violation]] = tripped(run)
    assert violation["kind"] == "outside_output" and "info/attributes" in violation["detail"]
    assert committed(run) == []
    assert not marker.exists()
    tracked = next(p for p in run_git(worktree(run), "ls-files").split("\n") if p and p != SPEC)
    os.utime(worktree(run) / tracked, (1_000_000_000, 1_000_000_000))  # stat-dirty: status must hash it
    run_git(worktree(run), "status", "--porcelain")  # the plant is live
    assert marker.exists()


def test_a_resumed_reconcile_step_keeps_its_baseline_until_a_human_approves(flow):
    boundary = {"id": "R1-F1", "question": "Giữ thiết kế?", "evidence": [], "kind": "boundary",
                "options": [{"key": "A", "label": "Giữ"}, {"key": "B", "label": "Đổi"}]}
    asks = {"write": {SPEC: EDITED}, "result": {"status": "needs_human", "summary": "cần", "questions": [boundary]}}
    run = begin(flow, {REVIEW: [found(HIGH), done("sạch")],
                       RECONCILE: [asks, done("xong", dispositions=[disp("R1-F1", "actionable")])]})
    assert go(flow, run) == "waiting_human"
    common = Path(run_git(flow.root, "rev-parse", "--path-format=absolute", "--git-common-dir").strip())
    (common / "info").mkdir(exist_ok=True)
    (common / "info" / "attributes").write_text("*.md diff\n", encoding="utf-8")  # changed while it waited
    answer(run, RECONCILE, {"R1-F1": "A"}, by="me")
    assert go(flow, run) == "waiting_human"  # an answer resumes the step on the baseline it recorded
    [[violation]] = tripped(run)
    assert "info/attributes" in violation["detail"]
    assert called(flow).count(RECONCILE) == 1
    approve(run, RECONCILE, by="me")  # a human looked at the tree: a new baseline
    assert go(flow, run) == "awaiting_approval"
    [sha] = committed(run)
    assert files_of(run, sha) == [SPEC]
