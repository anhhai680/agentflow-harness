"""The runner on its own: phases that move on, findings that hold a phase back, stops it settles itself,
and the stops that stay a human's. `configure(autopilot=True)` is the workflow as shipped."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from agentflow.autonomy import blocking_of, eligible, phase_findings, recommended_answers
from agentflow.config import load_config, validate
from agentflow.hitl import approve, reject
from agentflow.pipeline import RunOptions, advance, approval_text, start
from agentflow.state import Run
from conftest import PYTHON_CONFIG

AGENTFLOW = Path(__file__).resolve().parents[1]
REPO = AGENTFLOW.parent
PIPE = {"gate_sets": {"build": ["ok"], "verify": ["ok"], "setup": []}}
SPEC = "docs/specs/SPEC-demo.md"
SPEC_TEXT = "# Spec: `demo`\n\n**Status: approved** 2026-10-01\n\n## Objective\n\nMẫu.\n\n## Open Questions\n\nNone for this module.\n"
PLAN = "docs/plans/2026-10-29-demo.md"
PLAN_TEXT = "# Plan: demo\n\n### Task 1: First thing\n\n**Depends on:** none\n\n- [ ] **Step 1: Do it**\n"
BLOCKING = {"severity": "blocking", "file": "src/one.py", "line": 3, "text": "Thiếu kiểm tra quyền"}
MEDIUM = {"severity": "medium", "file": "compose.yaml", "line": 19, "text": "Mật khẩu mặc định"}
MINOR = {"severity": "minor", "file": "src/one.py", "line": 9, "text": "đổi tên biến"}
RED = "build/regression_test"
RESOLVE = f"{RED}/resolve-1"
IMPL = "build/build/task-1/implement"
REVIEW = "build/build/task-1/review"
GOOD_TEST = {"result": {"status": "done", "summary": "red", "signals": {"test_command": "test -f fixed.txt"}}}
FIXED = {"write": {"fixed.txt": "ok\n"}, "result": {"status": "done", "summary": "fixed"}}
BLOCKED = {"result": {"status": "blocked", "summary": "cannot go on"}}


def done(summary: str = "ok", **extra) -> dict:
    return {"result": {"status": "done", "summary": summary, **extra}}


def question(kind: str = "spec_gap", recommended: bool = True) -> dict:
    return {"id": "q1", "question": "Lưu trong bao lâu?", "evidence": [], "kind": kind,
            "options": [{"key": "A", "label": "1 giờ", "recommended": recommended}, {"key": "B", "label": "1 ngày"}]}


@pytest.fixture
def auto(flow_repo):
    flow_repo.configure(PIPE, autopilot=True)
    return flow_repo


@pytest.fixture
def notified(monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr("agentflow.hitl.notify", lambda cfg, title, message, run=None: seen.append(message))
    return seen


def begin_bug(flow, to: str = "review") -> Run:
    """A bug run, with `define` already approved: it walks the phases that need no human."""
    run = start(flow.cfg, flow.root, "bug", {"task": "đăng nhập lỗi"}, RunOptions(to_phase=to))
    assert advance(run, flow.cfg, RunOptions()) == "awaiting_approval"
    assert run.state["waiting"]["phase"] == "define"
    approve(run, "define", by="me")
    return run


def go(flow, run: Run) -> str:
    return advance(run, flow.cfg, RunOptions())


def called(flow) -> list[str]:
    return [c["step"] for c in flow.calls()]


# --------------------------------------------------------------------------- the shipped workflow


def test_shipped_config_says_who_approves_on_every_phase():
    raw = load_config(PYTHON_CONFIG)
    assert {name: phase.needs_approval for name, phase in raw.phases.items()} == {
        "define": True, "plan": True, "build": False, "verify": False, "review": False, "ship": False}
    assert {name for name, phase in raw.phases.items() if phase.resolve} == {"build", "verify", "review"}
    assert all("needs_approval" in raw.raw["phases"][name] for name in raw.phases)


@pytest.mark.parametrize("path", ["autonomy.py", "ci_lane.py"])
def test_the_runner_never_names_a_phase(path):
    text = (AGENTFLOW / path).read_text(encoding="utf-8")
    assert not re.search(r"""["'](define|plan|build|verify|review|ship)["']""", text)


def test_nothing_in_agentflow_can_merge():
    merge = re.compile(r"""["']merge["']|pr\s+merge|--auto\b|--admin\b""")
    for path in [*AGENTFLOW.glob("*.py"), *AGENTFLOW.glob("prompts/*.md"), PYTHON_CONFIG]:
        assert not merge.search(path.read_text(encoding="utf-8")), path


def test_phase_missing_needs_approval_fails_validate(tmp_cfg):
    def strip(data):
        del data["phases"]["build"]["needs_approval"]

    cfg = load_config(tmp_cfg(strip))
    assert cfg.phases["build"].needs_approval is True  # a config that skipped validate keeps its human
    assert any(i.path == "phases.build.needs_approval" for i in validate(cfg, REPO))


@pytest.mark.parametrize("value", ["false", 0, None])
def test_only_a_real_false_drops_the_human(tmp_cfg, value):
    cfg = load_config(tmp_cfg(lambda data: data["phases"]["build"].update(needs_approval=value)))
    assert cfg.phases["build"].needs_approval is True
    assert any(i.path == "phases.build.needs_approval" for i in validate(cfg, REPO))


@pytest.mark.parametrize("kind", ["budget_exceeded", "agent_unavailable", "phase_approval"])
def test_a_human_only_stop_cannot_be_listed(tmp_cfg, kind):
    cfg = load_config(tmp_cfg(lambda data: data["autonomy"]["resolve_kinds"].update({kind: 1})))
    assert any(kind in i.path for i in validate(cfg, REPO))


# --------------------------------------------------------------------------- findings


def test_findings_fail_closed(cfg):
    keeps = [{"severity": s} for s in ("minor", "LOW", "info", "nit")]
    blocks = [{"severity": s} for s in ("blocking", "critical", "medium", "whatever", "")] + [{}]
    assert blocking_of(cfg, keeps) == []
    assert blocking_of(cfg, blocks) == blocks
    assert blocking_of(cfg, ["a bare string"]) == [{"text": "a bare string"}]


def test_a_review_with_no_findings_moves_on_by_itself(auto):
    auto.script({RED: [GOOD_TEST], "build/fix": [FIXED]})
    run = begin_bug(auto)
    assert go(auto, run) == "done"
    assert not auto.events(run, "phase_approval_raised")[1:]  # only `define` asked
    approved = auto.events(run, "phase_approved")
    assert [(e["phase"], e["by"]) for e in approved[1:]] == [("build", "runner"), ("verify", "runner"), ("review", "runner")]
    assert all(run.state["phases"][p]["handoff"] for p in ("build", "verify", "review"))


def test_a_blocking_branch_review_is_planned_fixed_and_reviewed_again(auto):
    auto.script({RED: [GOOD_TEST], "build/fix": [FIXED],
                 "review/branch_review": [done("một lỗi", findings=[BLOCKING]), done("sạch")],
                 "review/fix_findings": [{"write": {"src/one.py": "x = 2\n"}, **done("đã sửa")}]})
    run = begin_bug(auto)
    assert go(auto, run) == "done"
    steps = called(auto)
    plan, fix = steps.index("review/plan_fixes"), steps.index("review/fix_findings")
    reviews = [i for i, s in enumerate(steps) if s == "review/branch_review"]
    assert reviews[0] < plan < fix < reviews[1] and len(reviews) == 2
    rejected = auto.events(run, "phase_rejected")
    assert [(e["phase"], e["by"]) for e in rejected] == [("review", "runner")] and BLOCKING["text"] in rejected[0]["note"]
    assert not auto.events(run, "hitl_raised")
    assert [e["phase"] for e in auto.events(run, "phase_approval_raised")] == ["define"]
    assert any(e["type"] == "step_committed" and e["key"] == "review/fix_findings" for e in run.events.read())


def test_a_medium_finding_blocks_too(auto):
    auto.script({RED: [GOOD_TEST], "build/fix": [FIXED],
                 "review/branch_review": [done("ổn", findings=[MEDIUM]), done("sạch")]})
    run = begin_bug(auto)
    assert go(auto, run) == "done"
    assert len(auto.events(run, "phase_rejected")) == 1


def test_a_minor_finding_does_not_hold_the_phase(auto):
    auto.script({RED: [GOOD_TEST], "build/fix": [FIXED], "review/branch_review": [done("ổn", findings=[MINOR])]})
    run = begin_bug(auto)
    assert go(auto, run) == "done"
    assert not auto.events(run, "phase_rejected")
    assert auto.events(run, "step_findings")[-1]["findings"][0]["text"] == MINOR["text"]


def test_a_verify_finding_gets_a_fix_round(auto):
    auto.script({RED: [GOOD_TEST], "build/fix": [FIXED], "verify/verify": [done("thiếu", findings=[BLOCKING]), done("sạch")]})
    run = begin_bug(auto)
    assert go(auto, run) == "done"
    assert [e["phase"] for e in auto.events(run, "phase_rejected")] == ["verify"]
    assert "verify/plan_fixes" in called(auto) and "verify/fix_findings" in called(auto)


def test_verify_cannot_clear_its_own_finding_by_answering_unchanged(auto):
    unchanged = {"result": {"status": "unchanged", "summary": "không có gì đổi"}}
    auto.script({RED: [GOOD_TEST], "build/fix": [FIXED],
                 "verify/verify": [done("thiếu", findings=[BLOCKING]), unchanged, done("sạch")]})
    run = begin_bug(auto)
    assert go(auto, run) == "done"
    second = [c for c in auto.calls() if c["step"] == "verify/verify"][1]
    assert "--resume" not in second["argv"]  # a fresh session, told the last findings
    assert BLOCKING["text"] in second["argv"][-1]
    assert auto.events(run, "result_reminded")  # `unchanged` is no verify


def test_a_paused_run_starts_no_resolver(auto):
    auto.script({RED: [BLOCKED]})
    run = begin_bug(auto, to="build")
    real = run.emit

    def pausing(type, by="runner", **data):
        event = real(type, by, **data)
        if type == "hitl_raised":
            real("run_paused", "me")
        return event

    run.emit = pausing
    assert go(auto, run) == "paused"
    assert not auto.events(run, "resolve_started")


def test_findings_that_never_clear_reach_a_human_with_the_findings_shown(auto, notified):
    auto.script({RED: [GOOD_TEST], "build/fix": [FIXED],
                 "review/branch_review": [done("lỗi", findings=[BLOCKING])] * 3})
    run = begin_bug(auto)
    assert go(auto, run) == "awaiting_approval"
    assert run.state["waiting"]["phase"] == "review"
    assert len(auto.events(run, "phase_rejected")) == 2  # `autonomy.finding_rounds`
    page = approval_text(auto.cfg, run, "review")
    assert "1 blocking finding(s) are still open" in page and BLOCKING["text"] in page and "src/one.py:3" in page
    assert any("Phase review is done" in text for text in notified)
    approve(run, "review", by="me")
    assert go(auto, run) == "done"


def test_an_unreadable_result_is_no_clean_review(auto):
    auto.script({RED: [GOOD_TEST], "build/fix": [FIXED]})
    run = begin_bug(auto)
    assert go(auto, run) == "done"
    run.result_path("review/branch_review").write_text("not json", encoding="utf-8")
    found = phase_findings(auto.cfg, run, "review")
    assert blocking_of(auto.cfg, found) and "cannot be read" in found[0]["text"]


def test_a_refine_round_on_a_human_phase_still_goes_to_a_human(auto):
    auto.script({"define/debug": [done("đã tìm"), done("đã sửa")]})
    run = start(auto.cfg, auto.root, "bug", {"task": "đăng nhập lỗi"}, RunOptions(to_phase="define"))
    assert go(auto, run) == "awaiting_approval"
    reject(run, "define", by="me", note="xem lại")
    assert go(auto, run) == "awaiting_approval"
    assert run.state["waiting"]["phase"] == "define"


# --------------------------------------------------------------------------- stops the runner settles


def test_a_blocked_step_goes_to_a_resolver_then_back_to_its_session(auto, notified):
    auto.script({RED: [BLOCKED, GOOD_TEST], RESOLVE: [done("nguyên nhân: thiếu tệp")], "build/fix": [FIXED]})
    run = begin_bug(auto, to="build")
    assert go(auto, run) == "done"
    assert called(auto)[-4:-1] == [RED, RESOLVE, RED] or RESOLVE in called(auto)
    assert not any("cannot go on" in text for text in notified)
    finished = auto.events(run, "resolve_finished")
    assert finished[0]["stop_kind"] == "blocked" and "nguyên nhân" in finished[0]["summary"]
    reopened = auto.events(run, "step_reopened")[0]
    assert reopened["reason"] == "resolve" and "nguyên nhân" in reopened["note"]
    assert not auto.events(run, "hitl_approved")  # nobody approved anything
    assert run.state["resolving"] is None


def test_the_resolver_gets_the_stop_on_opus_and_the_step_keeps_its_session(auto):
    auto.script({RED: [BLOCKED, GOOD_TEST], RESOLVE: [done("đã rõ")], "build/fix": [FIXED]})
    run = begin_bug(auto, to="build")
    assert go(auto, run) == "done"
    resolver = next(c for c in auto.calls() if c["step"] == RESOLVE)
    assert "opus" in resolver["argv"] and "--resume" not in resolver["argv"]
    assert "--resume" in [c for c in auto.calls() if c["step"] == RED][-1]["argv"]


def test_a_step_that_stays_blocked_stops_for_a_human_after_its_attempts(auto, notified):
    auto.script({RED: [BLOCKED] * 3, f"{RED}/resolve-2": [done("vẫn chưa")]})
    run = begin_bug(auto, to="build")
    assert go(auto, run) == "waiting_human"
    assert run.state["waiting"]["key"] == RED
    assert len(auto.events(run, "resolve_started")) == 2  # `autonomy.resolve_kinds.blocked`
    assert sum("cannot go on" in text for text in notified) == 1


def test_a_resolver_that_is_blocked_is_a_human_stop_with_its_diagnosis(auto, notified):
    auto.script({RED: [BLOCKED], RESOLVE: [{"result": {"status": "blocked", "summary": "cần khóa API"}}]})
    run = begin_bug(auto, to="build")
    assert go(auto, run) == "waiting_human"
    assert run.state["waiting"]["key"] == RESOLVE
    assert any("cần khóa API" in text for text in notified)


def test_a_runner_killed_mid_resolve_finishes_it_first(auto):
    auto.script({RED: [BLOCKED, GOOD_TEST], RESOLVE: [BLOCKED, done("xong")], "build/fix": [FIXED]})
    run = begin_bug(auto, to="build")
    assert go(auto, run) == "waiting_human"
    approve(run, RESOLVE, by="me")
    assert go(auto, run) == "done"
    assert auto.events(run, "resolve_finished")
    assert run.state["resolving"] is None


def test_a_human_stop_gives_fresh_attempts(auto):
    auto.script({RED: [BLOCKED] * 4 + [GOOD_TEST], "build/fix": [FIXED]})
    run = begin_bug(auto, to="build")
    assert go(auto, run) == "waiting_human"
    approve(run, RED, by="me")
    assert go(auto, run) == "done"
    assert len(auto.events(run, "resolve_started")) == 3
    assert called(auto).count(f"{RED}/resolve-3") == 1  # a fresh attempt is a new session, never a finished one


def test_stops_outside_a_resolve_phase_are_a_humans(auto, notified):
    auto.script({"define/debug": [BLOCKED]})
    run = start(auto.cfg, auto.root, "bug", {"task": "đăng nhập lỗi"}, RunOptions())
    assert go(auto, run) == "waiting_human"
    assert not auto.events(run, "resolve_started")
    assert any("cannot go on" in text for text in notified)


def test_a_budget_stop_is_never_settled(flow_repo, notified):
    flow_repo.configure({**PIPE, "defaults": {"run_budget_usd": 0.0001}}, autopilot=True)
    flow_repo.script({"define/debug": [{**done("ok"), "cost": 1.0}]})
    run = begin_bug(flow_repo)
    assert go(flow_repo, run) == "waiting_human"
    assert run.state["waiting"]["kind"] == "budget_exceeded"
    assert not flow_repo.events(run, "resolve_started")


# --------------------------------------------------------------------------- questions


def begin_feature(flow) -> Run:
    """A feature run past `plan`, with the spec and the one-task plan written."""
    flow.script({"define/draft": [{"write": {SPEC: SPEC_TEXT}, **done("drafted")}],
                 "plan/plan": [{"write": {PLAN: PLAN_TEXT}, **done("planned")}]})
    run = start(flow.cfg, flow.root, "feature", {"module": "demo"}, RunOptions(to_phase="build"))
    for phase in ("define", "plan"):
        assert go(flow, run) == "awaiting_approval"
        approve(run, phase, by="me")
    return run


def script_more(flow, acts: dict) -> None:
    current = json.loads(flow.fake.script_path.read_text(encoding="utf-8"))
    flow.fake.script_path.write_text(json.dumps({**current, **acts}), encoding="utf-8")


def test_a_recommended_answer_is_taken_recorded_and_said_to_be_the_runners(auto, notified):
    run = begin_feature(auto)
    needs = {"result": {"status": "needs_human", "summary": "cần quyết định", "questions": [question()]}}
    script_more(auto, {IMPL: [needs, {"write": {"src/one.py": "x = 1\n"}, **done("xong")}]})
    assert go(auto, run) == "done"
    answered = auto.events(run, "hitl_answered")[-1]
    assert answered["by"] == "runner" and answered["answers"] == {"q1": "A"}
    assert f"{IMPL}/record-q1" in called(auto)  # a spec gap is filed in the spec, as a human's answer is
    assert not any("cần quyết định" in text for text in notified)


def test_no_recommended_option_stops_for_a_human(auto, notified):
    run = begin_feature(auto)
    needs = {"result": {"status": "needs_human", "summary": "cần quyết định", "questions": [question(recommended=False)]}}
    script_more(auto, {IMPL: [needs]})
    assert go(auto, run) == "waiting_human"
    assert any("cần quyết định" in text for text in notified)


def test_a_parkable_question_with_a_recommendation_is_answered_not_parked(auto):
    run = begin_feature(auto)
    needs = {"result": {"status": "needs_human", "summary": "cần", "questions": [question()], "can_continue_without": True}}
    script_more(auto, {IMPL: [needs, {"write": {"src/one.py": "x = 1\n"}, **done("xong")}]})
    assert go(auto, run) == "done"
    assert not auto.events(run, "step_parked")


@pytest.mark.parametrize(("questions", "want"), [
    ([question()], {"q1": "A"}),
    ([question(recommended=False)], None),
    ([], None),
    ([{"id": "q1", "options": [{"key": "A", "recommended": True}, {"key": "B", "recommended": True}]}], None),
])
def test_recommended_answers(questions, want):
    assert recommended_answers(questions) == want


# --------------------------------------------------------------------------- guards


def test_a_forbidden_edit_is_put_back_once_and_the_step_told(auto, git):
    edit = {"write": {"CONSTRAINTS.md": "weaker\n"}, **GOOD_TEST}
    auto.script({RED: [edit, GOOD_TEST], "build/fix": [FIXED]})
    run = begin_bug(auto, to="build")
    assert go(auto, run) == "done"
    worktree = Path(run.state["worktree"])
    assert (worktree / "CONSTRAINTS.md").read_text(encoding="utf-8") == "rules\n"
    assert auto.events(run, "forbidden_reverted")[0]["paths"] == ["CONSTRAINTS.md"]
    assert "CONSTRAINTS.md" not in git(worktree, "log", "--name-only", "--format=", auto.head() + "..HEAD")
    assert "no agent may touch" in auto.events(run, "step_reopened")[0]["note"]


def test_a_second_forbidden_edit_stops_for_a_human(auto, notified):
    edit = {"write": {"CONSTRAINTS.md": "weaker\n"}, **GOOD_TEST}
    auto.script({RED: [edit, {"write": {"CONSTRAINTS.md": "weaker still\n"}, **GOOD_TEST}]})
    run = begin_bug(auto, to="build")
    assert go(auto, run) == "waiting_human"
    assert run.state["waiting"]["kind"] == "guard_violation"
    assert len(auto.events(run, "forbidden_reverted")) == 1
    assert any("forbidden_path" in text for text in notified)


def test_only_the_flagged_paths_move(auto):
    edit = {"write": {"CONSTRAINTS.md": "weaker\n", "src/keep.py": "x = 1\n"}, **GOOD_TEST}
    auto.script({RED: [edit, GOOD_TEST], "build/fix": [FIXED]})
    run = begin_bug(auto, to="build")
    assert go(auto, run) == "done"
    assert (Path(run.state["worktree"]) / "src/keep.py").is_file()


def test_a_new_forbidden_file_is_removed(auto):
    edit = {"write": {".claude/new.json": "{}\n"}, **GOOD_TEST}
    auto.script({RED: [edit, GOOD_TEST], "build/fix": [FIXED]})
    run = begin_bug(auto, to="build")
    assert go(auto, run) == "done"
    assert not (Path(run.state["worktree"]) / ".claude/new.json").exists()


def test_an_agent_commit_is_never_put_right_by_the_runner(auto, notified):
    auto.script({RED: [{"write": {"src/x.py": "x = 1\n"}, "git_commit": "tự commit", **GOOD_TEST}]})
    run = begin_bug(auto, to="build")
    assert go(auto, run) == "waiting_human"
    assert run.state["waiting"]["kind"] == "guard_violation"
    assert not auto.events(run, "forbidden_reverted")
    assert notified


# --------------------------------------------------------------------------- review_unresolved


@pytest.fixture
def big(flow_repo):
    flow_repo.configure({**PIPE, "conditions": {"plan_is_small": {"max": 0}}}, autopilot=True)
    return flow_repo


def test_a_runner_approval_never_commits_open_findings(big, git, notified):
    run = begin_feature(big)
    script_more(big, {IMPL: [{"write": {"src/one.py": "x = 1\n"}, **done("xong")}],
                      REVIEW: [done("lỗi", findings=[BLOCKING])] * 3})
    assert go(big, run) == "waiting_human"
    assert run.state["waiting"]["kind"] == "review_unresolved"
    assert len(big.events(run, "resolve_started")) == 1
    assert not big.events(run, "task_committed")
    assert any("blocking review finding" in text for text in notified)


def test_a_resolver_that_clears_the_findings_lets_the_task_commit(big, git):
    run = begin_feature(big)
    script_more(big, {IMPL: [{"write": {"src/one.py": "x = 1\n"}, **done("xong")}],
                      REVIEW: [done("lỗi", findings=[BLOCKING]), done("lỗi", findings=[BLOCKING]), done("sạch")]})
    assert go(big, run) == "done"
    message = git(Path(run.state["worktree"]), "log", "-1", "--format=%B")
    assert "accepted by a human" not in message


def test_a_human_can_still_accept_open_findings(big, git):
    run = begin_feature(big)
    script_more(big, {IMPL: [{"write": {"src/one.py": "x = 1\n"}, **done("xong")}],
                      REVIEW: [done("lỗi", findings=[BLOCKING])] * 3})
    assert go(big, run) == "waiting_human"
    approve(run, REVIEW, by="me")
    assert go(big, run) == "done"
    assert "accepted by a human" in git(Path(run.state["worktree"]), "log", "-1", "--format=%B")


# --------------------------------------------------------------------------- the offer, and who hears of what


def test_eligible_reads_the_phase_never_a_name(flow_repo):
    cfg = flow_repo.configure({"phases": {"define": {"resolve": True}}, "autonomy": {"resolve_kinds": {"blocked": 1}}}, autopilot=True)
    assert eligible(cfg, "blocked", "define/debug", [])
    assert not eligible(cfg, "blocked", "plan/plan", [])
    assert not eligible(cfg, "blocked", "define/debug/resolve-1", [])
    assert not eligible(cfg, "budget_exceeded", "define/debug", [])


def test_the_doubt_offer_is_on_the_plan_page_and_build_does_not_stop(flow_repo):
    flow_repo.configure({**PIPE, "conditions": {"security_path": {"any": None, "check": "file_exists", "path": "README.md"}}},
                        autopilot=True)
    flow_repo.script({"define/draft": [{"write": {SPEC: SPEC_TEXT}, **done("drafted")}],
                      "plan/plan": [{"write": {PLAN: PLAN_TEXT}, **done("planned")}]})
    run = start(flow_repo.cfg, flow_repo.root, "feature", {"module": "demo"}, RunOptions(to_phase="build"))
    assert go(flow_repo, run) == "awaiting_approval"
    approve(run, "define", by="me")
    assert go(flow_repo, run) == "awaiting_approval"
    page = approval_text(flow_repo.cfg, run, "plan")
    assert "/doubt-driven-development" in page and "The runner never runs it" in page
    assert "doubt" not in approval_text(flow_repo.cfg, run, "define")
    approve(run, "plan", by="me")
    assert go(flow_repo, run) == "done"
    assert not [e for e in flow_repo.events(run, "hitl_raised") if e["kind"] == "checkpoint"]
