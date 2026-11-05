"""The one main workflow: what each lane walks, the review fix step, the build skills, the workflow views."""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentflow.config import for_lane, load_config, resolve_model
from agentflow.flow import default_config_path, main
from agentflow.hitl import answer, approve, reject
from agentflow.lanes import build_prompt
from agentflow.module_queue import feature_runs
from agentflow.pipeline import RunOptions, advance, start
from agentflow.state import Run
from agentflow.views import _run_step, run_workflow, workflow_view

PIPE = {"gate_sets": {"build": ["ok"], "verify": ["ok"]}}
EVAL_OK = {**PIPE, "gates": {"floor": {"run": "true"}, "eval": {"run": "true", "when_exists": None}}}
SPEC = "docs/specs/SPEC-demo.md"
SPEC_TEXT = "# Spec: `demo`\n\n**Status: approved** 2026-10-01\n\n## Objective\n\nMẫu.\n\n## Open Questions\n\nNone for this module.\n"
PLAN = "docs/plans/2026-10-29-demo.md"
PLAN_TEXT = "# Plan: demo\n\n### Task 1: First thing\n\n**Depends on:** none\n\n- [ ] **Step 1: Do it**\n"
QUESTION = {"id": "q1", "question": "Lưu trong bao lâu?", "options": [{"key": "A", "label": "1 giờ", "recommended": True}],
            "evidence": [], "kind": "other"}
AGENTFLOW_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = AGENTFLOW_DIR.parents[1]
NOTE = "Sửa lỗi kiểm tra quyền"
BLOCKING = {"severity": "blocking", "file": "src/one.py", "line": 3, "text": "Thiếu kiểm tra quyền"}
MINOR_FINDING = {"severity": "minor", "file": "src/one.py", "line": 9, "text": "đổi tên biến"}


@pytest.fixture
def pipe(flow_repo):
    flow_repo.configure(PIPE)
    return flow_repo


def begin(flow_repo, lane: str, params: dict, **opts) -> Run:
    return start(flow_repo.cfg, flow_repo.root, lane, params, RunOptions(**opts))


def go(flow_repo, run: Run) -> str:
    return advance(run, flow_repo.cfg, RunOptions())


def steps_called(flow_repo) -> list[str]:
    return [c["step"] for c in flow_repo.calls()]


def approvals_walked(flow_repo, run: Run) -> list[str]:
    """Advance the run, approving every phase, and return the phases that asked."""
    seen: list[str] = []
    status = go(flow_repo, run)
    while status == "awaiting_approval":
        phase = run.state["waiting"]["phase"]
        seen.append(phase)
        approve(run, phase, by="me")
        status = go(flow_repo, run)
    assert status == "done", run.state["waiting"] or run.state["failure"]
    return seen


# --------------------------------------------------------------------------- what each lane walks


def test_feature_walks_every_phase_up_to_review(pipe):
    pipe.script({
        "define/draft": [{"write": {SPEC: SPEC_TEXT}, "result": {"status": "done", "summary": "drafted"}}],
        "plan/plan": [{"write": {PLAN: PLAN_TEXT}, "result": {"status": "done", "summary": "planned"}}],
    })
    run = begin(pipe, "feature", {"module": "demo"}, to_phase="review")
    assert approvals_walked(pipe, run) == ["define", "plan", "build", "verify", "review"]


def test_a_phase_switch_starts_a_new_session(pipe):
    pipe.script({
        "define/draft": [{"write": {SPEC: SPEC_TEXT}, "result": {"status": "done", "summary": "drafted"}}],
        "plan/plan": [{"write": {PLAN: PLAN_TEXT}, "result": {"status": "done", "summary": "planned"}}],
    })
    run = begin(pipe, "feature", {"module": "demo"}, to_phase="review")
    phases = approvals_walked(pipe, run)
    assert phases == ["define", "plan", "build", "verify", "review"]
    calls = pipe.calls()
    earlier: set[str] = set()
    for phase in phases:
        first = next(c for c in calls if c["step"].startswith(f"{phase}/"))
        assert "--resume" not in first["argv"], f"{phase} resumed a session"
        sessions = {e["session_id"] for e in pipe.events(run, "agent_session") if e["key"].startswith(f"{phase}/")}
        assert sessions, f"{phase} ran no agent"
        assert not sessions & earlier, f"{phase} reused a session from an earlier phase"
        earlier |= sessions


def test_bug_walks_without_plan(pipe):
    pipe.script({
        "build/regression_test": [{"result": {"status": "done", "summary": "red",
                                              "signals": {"test_command": "test -f fixed.txt"}}}],
        "build/fix": [{"write": {"fixed.txt": "ok\n"}, "result": {"status": "done", "summary": "fixed"}}],
    })
    run = begin(pipe, "bug", {"task": "đăng nhập lỗi"}, to_phase="verify")
    assert approvals_walked(pipe, run) == ["define", "build", "verify"]
    assert steps_called(pipe)[:3] == ["define/debug", "build/regression_test", "build/fix"]
    assert [e["expect"] for e in pipe.events(run, "expectation_checked")] == ["red", "green"]
    assert "plan" in {e["phase"] for e in pipe.events(run, "phase_skipped")}


def test_eval_walks_build_verify_review(flow_repo):
    flow_repo.configure(EVAL_OK)
    run = begin(flow_repo, "eval", {"task": "đổi prompt"}, to_phase="review")
    assert approvals_walked(flow_repo, run) == ["build", "verify", "review"]
    assert steps_called(flow_repo) == ["build/change", "review/branch_review"]  # a passing eval gate calls no agent
    assert run.state["gates"]["verify/eval_gate"]


def test_design_walks_define_and_commits_in_ship(pipe, monkeypatch):
    monkeypatch.setenv("PATH", f"{Path(__file__).parent / 'bin'}{os.pathsep}{os.environ['PATH']}")
    pipe.script({
        "define/decide/grill_topic": [{"result": {"status": "done", "summary": "one question", "questions": [QUESTION]}}],
        "define/decide/apply_topic": [{"write": {"note.md": "quyết định\n"},
                                       "result": {"status": "done", "summary": "applied"}}],
    })
    pipe.configure({**PIPE, "phases": {"define": {"steps": _without_checks(pipe)}}})
    run = begin(pipe, "design", {"topic": "Ngưỡng"})
    assert go(pipe, run) == "waiting_human"
    answer(run, "define/decide/topic_answers", {"q1": "1 giờ"}, by="me")
    assert approvals_walked(pipe, run) == ["define"]
    assert "ship/commit" in run.state["steps"]
    assert run.state["steps"]["ship/finish"]["status"] == "done"  # commit, then the draft pull request


def _without_checks(flow_repo) -> list[dict]:
    """`define`'s raw steps with the doc checks off: this test is about the phases, not the idea doc."""
    import copy

    steps = copy.deepcopy(flow_repo.cfg.raw["phases"]["define"]["steps"])
    for step in steps:
        for child in step.get("steps", []):
            child.pop("checks", None)
    return steps


def test_a_step_not_in_the_lane_leaves_no_record(pipe):
    run = begin(pipe, "bug", {"task": "lỗi"}, to_phase="define")
    go(pipe, run)
    assert [k for k in run.state["steps"] if k.startswith("define/")] == ["define/debug"]


@pytest.mark.parametrize("lane, phases", [
    ("feature", ["define", "plan", "build", "verify", "review", "ship"]),
    ("bug", ["define", "build", "verify", "review", "ship"]),
    ("eval", ["build", "verify", "review", "ship"]),
    ("design", ["define", "ship"]),
])
def test_each_lane_has_steps_in_exactly_its_phases(lane, phases):
    cfg = load_config(default_config_path())
    assert [name for name, phase in cfg.phases.items() if for_lane(phase.steps, lane)] == phases


def test_spec_is_not_a_lane(flow_repo, capsys):
    assert main(["run", "spec", "--module", "demo", "--dry-run"]) == 1
    assert "agentflow run feature --module <module> --to define" in capsys.readouterr().out


# --------------------------------------------------------------------------- spec only


def test_to_define_ends_after_its_approval_and_leaves_the_queue_alone(pipe):
    pipe.script({"define/draft": [{"write": {SPEC: SPEC_TEXT}, "result": {"status": "done", "summary": "drafted"}}]})
    run = begin(pipe, "feature", {"module": "demo"}, to_phase="define")
    assert approvals_walked(pipe, run) == ["define"]
    assert run.state["status"] == "done"
    assert feature_runs(pipe.cfg, pipe.root) == {}  # a spec-only run does not start the module

    whole = begin(pipe, "feature", {"module": "demo"}, to_phase="plan")
    assert feature_runs(pipe.cfg, pipe.root)["demo"].id == whole.id


# --------------------------------------------------------------------------- answers inside one phase


def test_idea_answers_reach_the_draft(pipe):
    pipe.script({"define/idea/idea_questions": [{"result": {"status": "done", "summary": "q", "questions": [QUESTION]}}]})
    run = begin(pipe, "feature", {"module": "demo"})
    assert go(pipe, run) == "waiting_human"
    answer(run, "define/idea/idea_answers", {"q1": "1 giờ"}, by="me")
    go(pipe, run)
    draft = next(c for c in pipe.calls() if c["step"] == "define/draft")
    prompt = " ".join(draft["argv"])
    assert "treat them as settled" in prompt
    questions = next(arg for arg in prompt.split() if arg.strip("`").endswith("questions.md"))
    assert "1 giờ" in Path(questions.strip("`")).read_text(encoding="utf-8")


# --------------------------------------------------------------------------- the review fix step


def test_fix_findings_is_skipped_on_a_first_pass(pipe):
    run = begin(pipe, "feature", {"module": "demo"}, from_phase="review", to_phase="review")
    assert go(pipe, run) == "awaiting_approval"
    assert steps_called(pipe) == ["review/branch_review"]
    assert run.state["steps"]["review/fix_findings"]["status"] == "skipped"


def test_rejected_review_fixes_then_reviews_again(pipe):
    pipe.script({"review/fix_findings": [{"write": {"fixed.py": "x = 1\n"},
                                          "result": {"status": "done", "summary": "fixed"}}]})
    run = begin(pipe, "feature", {"module": "demo"}, from_phase="review", to_phase="review")
    assert go(pipe, run) == "awaiting_approval"
    reject(run, "review", by="me", note=NOTE)
    assert go(pipe, run) == "awaiting_approval"
    assert run.state["waiting"]["phase"] == "review"

    assert steps_called(pipe) == ["review/branch_review", "review/plan_fixes", "review/fix_findings", "review/branch_review"]
    fix = next(c for c in pipe.calls() if c["step"] == "review/fix_findings")
    prompt = " ".join(fix["argv"])
    assert NOTE in prompt and "{refine_note}" not in prompt
    assert any(g["gate"] == "ok" for g in run.state["gates"]["review/fix_findings"])
    assert (Path(run.state["worktree"]) / "fixed.py").is_file()
    assert any(e.get("key") == "review/fix_findings" for e in pipe.events(run, "step_committed"))


def test_rejected_review_runs_in_a_fresh_session(pipe):
    pipe.script({"review/branch_review": [
        {"result": {"status": "done", "summary": "reviewed", "findings": [BLOCKING, MINOR_FINDING]}},
        {"result": {"status": "done", "summary": "reviewed again", "findings": []}}]})
    run = begin(pipe, "feature", {"module": "demo"}, from_phase="review", to_phase="review")
    assert go(pipe, run) == "awaiting_approval"
    reject(run, "review", by="me", note=NOTE)
    assert go(pipe, run) == "awaiting_approval"

    first, second = [c["argv"] for c in pipe.calls() if c["step"] == "review/branch_review"]
    assert "--resume" not in first and "{earlier_findings}" not in first[-1]
    assert "--resume" not in second and "This is refine round" not in second[-1]
    assert BLOCKING["text"] in second[-1] and "src/one.py:3" in second[-1] and NOTE in second[-1]
    assert MINOR_FINDING["text"] not in second[-1]
    sessions = {e["session_id"] for e in pipe.events(run, "agent_session") if e["key"] == "review/branch_review"}
    assert len(sessions) == 2


def test_rejected_review_with_no_readable_result_still_gets_the_note(pipe):
    pipe.script({"review/branch_review": [{"result": {"status": "done", "summary": "reviewed"}},
                                          {"result": {"status": "done", "summary": "reviewed again"}}]})
    run = begin(pipe, "feature", {"module": "demo"}, from_phase="review", to_phase="review")
    assert go(pipe, run) == "awaiting_approval"
    run.result_path("review/branch_review").unlink()
    reject(run, "review", by="me", note=NOTE)
    assert go(pipe, run) == "awaiting_approval"
    assert NOTE in [c for c in pipe.calls() if c["step"] == "review/branch_review"][-1]["argv"][-1]


def test_a_review_fix_in_an_eval_run_goes_through_the_eval_gate(flow_repo):
    flow_repo.configure(EVAL_OK)
    run = begin(flow_repo, "eval", {"task": "đổi prompt"}, from_phase="review", to_phase="review")
    assert go(flow_repo, run) == "awaiting_approval"
    assert run.state["steps"]["review/eval_gate"]["status"] == "skipped"
    reject(run, "review", by="me", note=NOTE)
    assert go(flow_repo, run) == "awaiting_approval"
    assert run.state["gates"]["review/eval_gate"]


# --------------------------------------------------------------------------- the build skills


def _build_step(flow_repo):
    return next(s for s in flow_repo.cfg.phases["build"].steps if s.id == "build")


def _prompt(flow_repo, step) -> str:
    executor = flow_repo.executor(flow_repo.new_run({"module": "demo"}))
    extra = {"task_title": "Task 1: Thing", "task_text": "Do it."}
    return build_prompt(flow_repo.cfg, step, executor.ctx, {}, extra, "first")


def test_a_larger_plan_gives_each_task_the_implementer_brief(pipe):
    step = _build_step(pipe)
    assert step.skill == "subagent-driven-development"
    text = _prompt(pipe, step)
    assert "subagent-driven-development/implementer-prompt.md" in text
    assert "Use the `subagent-driven-development` skill" not in text  # that would load the controller process
    assert "**do not commit**" in text and "Use the `test-driven-development` skill" in text


def _review_prompts(flow_repo) -> list[str]:
    build = _build_step(flow_repo)
    branch = next(s for s in flow_repo.cfg.phases["review"].steps if s.id == "branch_review")
    return [_prompt(flow_repo, build.review), _prompt(flow_repo, branch)]


def test_each_reviewer_gets_the_code_reviewer_brief_in_its_prompt(pipe):
    brief = (REPO_ROOT / ".claude/skills/requesting-code-review/code-reviewer.md").read_text(encoding="utf-8")
    for text in _review_prompts(pipe):
        assert brief.strip() in text  # in the prompt itself: a Sonnet session never opened it on its own
        assert "{brief}" not in text


def _agent_steps(steps):
    for step in steps:
        yield step
        if step.review is not None:
            yield step.review
        yield from _agent_steps(step.steps)


def test_every_sonnet_step_with_a_skill_gets_it_in_its_prompt():
    """Sonnet sessions never loaded a skill by name (0 of 96 in the real runs), so its text must be in the prompt."""
    cfg = load_config(default_config_path())
    bare = []
    for phase in cfg.phases.values():
        steps = list(_agent_steps(phase.steps))
        if phase.name in ("define", "plan") and cfg.record_step is not None:
            steps.append(cfg.record_step)
        for step in steps:
            model, _source = resolve_model(cfg, phase, step, step.role or "build", {})
            if step.skill and model == "sonnet" and not step.brief:
                bare.append(f"{phase.name}/{step.id}")
    assert bare == []


@pytest.mark.parametrize(("phase", "step_id", "needle"), [
    ("define", "apply", "domain-modeling/SKILL.md"),
    ("build", "regression_test", "test-driven-development/SKILL.md"),
    ("build", "fix", "test-driven-development/SKILL.md"),
    ("verify", "verify", "verification-before-completion/SKILL.md"),
    ("verify", "eval_gate", "verification-before-completion/SKILL.md"),
    ("review", "fix_findings", "receiving-code-review/SKILL.md"),
    ("review", "security_review", "security-and-hardening/SKILL.md"),
])
def test_a_skill_step_gets_that_skills_text(pipe, phase, step_id, needle):
    step = next(s for s in _agent_steps(pipe.cfg.phases[phase].steps) if s.id == step_id)
    skill = (REPO_ROOT / ".claude/skills" / needle).read_text(encoding="utf-8")
    assert skill.strip() in _prompt(pipe, step)


def test_record_step_gets_the_skills_text(pipe):
    skill = (REPO_ROOT / ".claude/skills/domain-modeling/SKILL.md").read_text(encoding="utf-8")
    assert skill.strip() in _prompt(pipe, pipe.cfg.record_step)


def test_an_implementer_gets_its_brief_and_the_tdd_skill_in_its_prompt(pipe):
    text = _prompt(pipe, _build_step(pipe))
    for name in ("subagent-driven-development/implementer-prompt.md", "test-driven-development/SKILL.md",
                 "test-driven-development/writing-good-tests.md"):
        assert (REPO_ROOT / ".claude/skills" / name).read_text(encoding="utf-8").strip() in text


def test_a_console_task_also_gets_the_frontend_skill(pipe):
    step = _build_step(pipe)
    executor = pipe.executor(pipe.new_run({"module": "demo"}))
    skill = (REPO_ROOT / ".claude/skills/frontend-ui-engineering/SKILL.md").read_text(encoding="utf-8").strip()
    assert skill in _prompt(pipe, executor._impl_step(step, {}, "Add `webadmin/src/pages/inbox.tsx`"))
    assert skill in _prompt(pipe, executor._impl_step(step, {}, "Add `web/src/thread.ts`"))
    assert skill not in _prompt(pipe, executor._impl_step(step, {}, "Add `src/tivacare/crm/client.py`"))


def test_a_reviewer_is_told_not_to_dispatch_another_reviewer(pipe):
    for text in _review_prompts(pipe):
        assert "Use the `requesting-code-review` skill" not in text  # that would load the dispatch process
        assert "do not spawn subagents" in text


def test_review_severities_follow_the_brief(pipe):
    for text in _review_prompts(pipe):
        assert "`blocking` for anything the brief calls Critical or Important" in text
        assert 'the brief\'s "Declined to judge" section in the result\'s `risks` list' in text


def test_every_review_prompt_is_fresh_ready():
    for name in ("task_review.md", "branch_review.md", "security_review.md"):
        text = (AGENTFLOW_DIR / "prompts" / name).read_text(encoding="utf-8")
        assert "{earlier_findings}" in text and "Do not open or append to `{progress}`" in text


def test_a_small_plan_uses_executing_plans(pipe):
    step = _build_step(pipe)
    executor = pipe.executor(pipe.new_run({"module": "demo"}))
    small = executor._impl_step(step, {"active": True, **step.small_plan})
    assert small.skill == "executing-plans" and small.prompt == "task_inline.md"
    text = _prompt(pipe, small)
    assert "Use the `executing-plans` skill" in text and "implementer-prompt.md" not in text
    tdd = (REPO_ROOT / ".claude/skills/test-driven-development/SKILL.md").read_text(encoding="utf-8")
    assert tdd.strip() in text


# --------------------------------------------------------------------------- the workflow views


def test_workflow_view_comes_from_the_config():
    cfg = load_config(default_config_path())
    view = workflow_view(cfg)
    assert [p["name"] for p in view["phases"]] == ["define", "plan", "build", "verify", "review", "ship"]
    assert [p["approval"] for p in view["phases"]] == [True, True, False, False, False, False]
    assert next(p for p in view["phases"] if p["name"] == "plan")["human_skill"] == "doubt-driven-development"
    assert "constraint-driven-development" in view["human_skills"]
    build = {s["id"]: s for s in next(p for p in view["phases"] if p["name"] == "build")["steps"]}
    assert build["build"]["skill"] == "subagent-driven-development"
    assert build["build"]["small_plan_skill"] == "executing-plans"
    assert build["build"]["review"]["model"] == "sonnet" and build["build"]["model"] == "sonnet"
    assert build["fix"]["lanes"] == ["bug"]
    assert build["build"]["review"]["id"] == "per-task review" and build["build"]["review"]["lanes"] == ["feature"]
    define = {s["id"]: s for s in view["phases"][0]["steps"]}
    assert [c["lanes"] for c in define["decide"]["steps"]] == [["design"]] * 3  # a group's steps run in its lanes
    assert build["change"]["tdd"] is False and build["setup"]["tdd"] is True  # only `change` opts out of TDD
    assert build["setup"]["type"] == "gate"


def _step(cfg, phase: str, step_id: str):
    return next(s for s in cfg.phases[phase].steps if s.id == step_id)


def test_a_per_task_step_rolls_up_with_a_count():
    cfg = load_config(default_config_path())
    state = {"task": {"n": 2, "of": 5}, "steps": {
        "build/build/task-1/implement": {"status": "done"}, "build/build/task-1/review": {"status": "done"},
        "build/build/task-2/implement": {"status": "running"},
    }}
    row = _run_step(state, "feature", _step(cfg, "build", "build"), "build/build")
    assert row["status"] == "running" and row["count"] == "1/5 tasks"


def test_the_task_count_total_does_not_grow_with_the_started_tasks():
    cfg = load_config(default_config_path())
    step = _step(cfg, "build", "build")
    steps = {"build/build/task-1/implement": {"status": "done"}}
    state = {"task": None, "task_total": 5, "steps": steps}  # between tasks: no live task, the total stays 5
    assert _run_step(state, "feature", step, "build/build")["count"] == "1/5 tasks"
    steps["build/build/task-2/implement"] = {"status": "running"}
    assert _run_step(state, "feature", step, "build/build")["count"] == "1/5 tasks"


def test_a_repeat_shows_its_latest_round():
    cfg = load_config(default_config_path())
    key = "define/grill_open_questions"
    state = {"waiting": {"key": f"{key}/r2/answers"}, "steps": {
        f"{key}/r1/grill": {"status": "done"}, f"{key}/r1/answers": {"status": "done"},
        f"{key}/r2/grill": {"status": "done"},
    }}
    row = _run_step(state, "feature", _step(cfg, "define", "grill_open_questions"), key)
    assert row["status"] == "waiting" and row["count"] == "2 rounds"
    assert {c["id"]: c["status"] for c in row["steps"]} == {"grill": "done", "answers": "waiting", "apply": "not reached"}


def test_a_skipped_step_gives_its_reason():
    cfg = load_config(default_config_path())
    state = {"steps": {"define/contracts": {"status": "skipped", "skip_reason": "the package exists"}}}
    row = _run_step(state, "feature", _step(cfg, "define", "contracts"), "define/contracts")
    assert row["status"] == "skipped" and row["reason"] == "the package exists"


def test_run_workflow_shows_only_the_runs_lane():
    cfg = load_config(default_config_path())
    run = SimpleNamespace(state={"pipeline": "eval", "status": "running", "steps": {}, "phases": {}})
    assert [p["name"] for p in run_workflow(run, cfg)] == ["build", "verify", "review", "ship"]
    build = run_workflow(run, cfg)[0]
    assert [s["id"] for s in build["steps"]] == ["change"] and build["status"] == "not reached"


def test_a_run_from_before_the_one_workflow_shows_its_own_records():
    cfg = load_config(default_config_path())
    run = SimpleNamespace(state={"pipeline": "spec", "status": "done", "phases": {"spec": {"status": "approved"}}, "steps": {
        "spec/draft": {"phase": "spec", "status": "skipped", "skip_reason": "exists"},
        "spec/contracts": {"phase": "spec", "status": "done"},
    }})
    [phase] = run_workflow(run, cfg)
    assert phase["name"] == "spec" and phase["status"] == "approved"
    assert [(s["id"], s["status"], s["reason"]) for s in phase["steps"]] == [
        ("draft", "skipped", "exists"), ("contracts", "done", None)]


def test_for_lane_keeps_untagged_steps():
    cfg = load_config(default_config_path())
    step = replace(_step(cfg, "plan", "plan"), lanes=[])
    assert for_lane([step], "bug") == [step]
