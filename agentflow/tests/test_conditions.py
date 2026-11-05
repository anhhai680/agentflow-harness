"""Conditions, `fmt`, model resolution and `explain`: the `python` pack's specs read-only, fixtures for the rest."""

from __future__ import annotations

import dataclasses
import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
from agentflow.conditions import CondResult, Ctx, evaluate, explain, fmt, make_ctx, plan_warnings
from agentflow.config import resolve_model
from agentflow.gitops import GhError, gh_json
from conftest import PYTHON_PACK

FIXTURES = Path(__file__).resolve().parent / "fixtures"
PACK = PYTHON_PACK  # the toy repository: `greet` has an approved, API-reviewed spec with nothing open
# An approved spec with contracts but no review of its own: it only mentions greet's.
FAREWELL = """# Spec: `farewell`

**Status: approved** (2026-11-02)

Builds on greet, whose **Resolved (2026-11-01, API review)** settled the route; this spec has had no review of its own.

## Contracts this module provides

- `toy.farewell(name: str) -> str`, called by greet.

## Open Questions

None.
"""

T = {"check": "command", "run": "true"}
F = {"check": "command", "run": "false"}
U = {"check": "status_line", "file": "missing-status.md", "approved": "zzz", "pending": "yyy"}


@pytest.fixture
def unknown_repo(tmp_path):
    (tmp_path / "missing-status.md").write_text("no status here\n", encoding="utf-8")
    return tmp_path


def real_ctx(cfg, module, **extra):
    ctx = make_ctx(cfg, PACK, "feature", {"module": module})
    ctx.values.update(extra)
    return ctx


def fixture_repo(tmp_path, *names):
    specs = tmp_path / "docs" / "specs"
    specs.mkdir(parents=True)
    for name in names:
        shutil.copy(FIXTURES / "specs" / name, specs / name)
    return tmp_path


def fixture_ctx(cfg, tmp_path, module, *names):
    repo = fixture_repo(tmp_path, *names)
    return make_ctx(cfg, repo, "spec", {"module": module})


def farewell_repo(root):
    """`root` with the farewell spec: approved, with contracts, never API-reviewed."""
    specs = root / "docs" / "specs"
    specs.mkdir(parents=True, exist_ok=True)
    (specs / "SPEC-farewell.md").write_text(FAREWELL, encoding="utf-8")
    return root


# ------------------------------------------------------------------ spec conditions (the pack's specs)


def test_no_open_questions_in_the_packs_spec(cfg):
    r = evaluate(cfg, "spec_has_open_questions", real_ctx(cfg, "greet"))
    assert r.value is False
    assert "0 open" in r.reason


def test_resolved_items_are_counted_not_open(cfg):
    r = evaluate(cfg, "spec_has_open_questions", real_ctx(cfg, "greet"))
    assert "resolved" in r.reason
    assert not r.reason.startswith("spec_has_open_questions = true")


def test_open_questions_fixture(cfg, tmp_path):
    r = evaluate(cfg, "spec_has_open_questions", fixture_ctx(cfg, tmp_path, "demo", "SPEC-demo.md"))
    assert r.value is True
    assert "2 open items, 2 resolved" in r.reason


def test_spec_approved_in_the_pack(cfg):
    assert evaluate(cfg, "spec_is_approved", real_ctx(cfg, "greet")).value is True


def test_pending_amendment_wins(cfg, tmp_path):
    ctx = fixture_ctx(cfg, tmp_path, "amended", "SPEC-amended.md")
    r = evaluate(cfg, "spec_is_approved", ctx)
    assert r.value is False
    assert "pending" in r.reason


def test_missing_status_line_unknown(cfg, tmp_path):
    r = evaluate(cfg, "spec_is_approved", fixture_ctx(cfg, tmp_path, "nostatus", "SPEC-nostatus.md"))
    assert r.value is None
    assert r.on_unknown == "ask_human"
    assert "spec_is_approved = unknown" in r.reason


def test_spec_missing_means_not_approved_and_not_exists(cfg, tmp_path):
    ctx = make_ctx(cfg, tmp_path, "spec", {"module": "ghost"})
    assert evaluate(cfg, "spec_exists", ctx).value is False
    assert evaluate(cfg, "spec_is_approved", ctx).value is False
    assert evaluate(cfg, "spec_has_open_questions", ctx).value is False
    assert evaluate(cfg, "spec_defines_contracts", ctx).value is False


def test_routes_detected(cfg):
    assert evaluate(cfg, "spec_defines_contracts", real_ctx(cfg, "greet")).value is True


def test_api_review_marker_is_the_modules_own(cfg, tmp_path):
    assert evaluate(cfg, "spec_api_reviewed", real_ctx(cfg, "greet")).value is True
    assert evaluate(cfg, "spec_needs_api_design", real_ctx(cfg, "greet")).value is False
    # a spec that only mentions another module's API review is not reviewed itself
    ctx = make_ctx(cfg, farewell_repo(tmp_path), "feature", {"module": "farewell"})
    assert evaluate(cfg, "spec_api_reviewed", ctx).value is False


def test_contracts_without_routes_still_need_an_api_review(cfg, tmp_path):
    # farewell has no HTTP routes but provides a function other modules call, so it still needs an API review
    ctx = make_ctx(cfg, farewell_repo(tmp_path), "feature", {"module": "farewell"})
    assert evaluate(cfg, "spec_defines_contracts", ctx).value is True
    assert evaluate(cfg, "spec_needs_api_design", ctx).value is True


def test_explain_unreviewed_approved_spec_runs_api_design_in_define(cfg, tmp_path):
    ctx = make_ctx(cfg, farewell_repo(tmp_path), "feature", {"module": "farewell"})
    by_key = lines_by_key(explain(cfg, "feature", ctx))
    assert by_key["define"].verdict == "run"
    assert by_key["define/contracts"].verdict == "run"
    assert by_key["define/draft"].verdict == "skip"
    assert "define/grill_open_questions" in by_key  # its questions are grilled before plan
    ids = [k for k in by_key if k.startswith("define/")]
    assert ids.index("define/contracts") < ids.index("define/grill_open_questions")


def test_explain_reviewed_spec_skips_api_design(cfg):
    ctx = make_ctx(cfg, PACK, "feature", {"module": "greet"})
    by_key = lines_by_key(explain(cfg, "feature", ctx))
    assert by_key["define/contracts"].verdict == "skip"
    assert "spec_api_reviewed = true" in by_key["define/contracts"].reason


def test_routes_absent_in_fixture(cfg, tmp_path):
    ctx = fixture_ctx(cfg, tmp_path, "nostatus", "SPEC-nostatus.md")
    assert evaluate(cfg, "spec_defines_contracts", ctx).value is False


def test_section_without_the_heading_is_unknown(cfg, tmp_path):
    repo = fixture_repo(tmp_path)
    (repo / "docs" / "specs" / "SPEC-bare.md").write_text("# bare\n\ntext\n", encoding="utf-8")
    r = evaluate(cfg, "spec_has_open_questions", make_ctx(cfg, repo, "spec", {"module": "bare"}))
    assert r.value is None


def test_section_items_match_filter(cfg, tmp_path):
    idea = tmp_path / "docs" / "ideas" / "idea.md"
    idea.parent.mkdir(parents=True)
    idea.write_text(
        "# Idea\n\n## Open Questions\n**MVP**\n- Can MISA issue a read-only AppID?\n- Is zero retention on?\n\n"
        "**Phase 2**\n- Which origins host the widget?\n\n## Next\n- not counted\n",
        encoding="utf-8",
    )
    ctx = make_ctx(cfg, tmp_path, "design", {"topic": "MISA"})
    ctx.values["idea"] = "docs/ideas/idea.md"
    assert evaluate(cfg, "idea_doc_question_open", ctx).value is True
    ctx.values["topic"] = "nothing like it"
    r = evaluate(cfg, "idea_doc_question_open", ctx)
    assert r.value is False
    assert "0 open" in r.reason


# ------------------------------------------------------------------ composition


def test_composition_with_unknown(cfg, unknown_repo):
    ctx = make_ctx(cfg, unknown_repo, "spec", {"module": "x"})
    assert evaluate(cfg, {"not": U}, ctx).value is None
    assert evaluate(cfg, {"any": [T, U]}, ctx).value is True
    assert evaluate(cfg, {"any": [F, U]}, ctx).value is None
    assert evaluate(cfg, {"any": [F, F]}, ctx).value is False
    assert evaluate(cfg, {"all": [F, U]}, ctx).value is False
    assert evaluate(cfg, {"all": [T, U]}, ctx).value is None
    assert evaluate(cfg, {"all": [T, T]}, ctx).value is True
    assert evaluate(cfg, {"not": T}, ctx).value is False
    assert evaluate(cfg, {"not": F}, ctx).value is True


def test_unknown_composite_keeps_on_unknown_of_the_unknown_member(cfg, unknown_repo):
    ctx = make_ctx(cfg, unknown_repo, "spec", {"module": "x"})
    r = evaluate(cfg, {"all": [T, dict(U, on_unknown="agent")]}, ctx)
    assert r.value is None
    assert r.on_unknown == "agent"


def test_on_unknown_true_and_false_decide(cfg, unknown_repo):
    ctx = make_ctx(cfg, unknown_repo, "spec", {"module": "x"})
    assert evaluate(cfg, dict(U, on_unknown=True), ctx).value is True
    r = evaluate(cfg, dict(U, on_unknown=False), ctx)
    assert r.value is False
    assert "unknown" in r.reason


def test_named_condition_inside_composition(cfg):
    ctx = real_ctx(cfg, "greet")
    r = evaluate(cfg, {"all": ["spec_exists", "spec_defines_contracts"]}, ctx)
    assert r.value is True
    assert evaluate(cfg, "plan_done", real_ctx(cfg, "no-such-module")).value is False  # no plan, so not done


def test_undefined_condition_raises(cfg):
    with pytest.raises(ValueError, match="undefined condition 'nope'"):
        evaluate(cfg, "nope", real_ctx(cfg, "settings"))


def test_unknown_check_raises(cfg):
    with pytest.raises(ValueError, match="unknown check 'bogus'"):
        evaluate(cfg, {"check": "bogus"}, real_ctx(cfg, "settings"))


# ------------------------------------------------------------------ built-in checks


def test_command_check(cfg):
    ctx = real_ctx(cfg, "settings")
    r = evaluate(cfg, {"check": "command", "run": "test -d toy"}, ctx)
    assert r.value is True
    assert evaluate(cfg, {"check": "command", "run": "test -d no-such-dir"}, ctx).value is False
    assert evaluate(cfg, {"check": "command", "run": "exit 3"}, ctx).reason.endswith("exit 3")


def test_command_check_runs_in_the_repo_and_formats_placeholders(cfg, tmp_path):
    (tmp_path / "marker-crm").write_text("x", encoding="utf-8")
    ctx = make_ctx(cfg, tmp_path, "feature", {"module": "crm"})
    assert evaluate(cfg, {"check": "command", "run": "test -f marker-{module}"}, ctx).value is True


def test_file_exists_and_grep_and_empty_path(cfg, tmp_path):
    (tmp_path / "a.txt").write_text("alpha\nbeta GET /x\n", encoding="utf-8")
    ctx = make_ctx(cfg, tmp_path, "feature", {"module": "crm"})
    assert evaluate(cfg, {"check": "file_exists", "path": "a.txt"}, ctx).value is True
    assert evaluate(cfg, {"check": "file_exists", "path": "b.txt"}, ctx).value is False
    assert evaluate(cfg, {"check": "file_exists", "path": ""}, ctx).value is False
    hit = evaluate(cfg, {"check": "grep", "file": "a.txt", "pattern": r"GET /"}, ctx)
    assert hit.value is True and "line 2" in hit.reason
    assert evaluate(cfg, {"check": "grep", "file": "a.txt", "pattern": "zzz"}, ctx).value is False
    assert evaluate(cfg, {"check": "grep", "file": "a.txt", "pattern": ["zzz", "alp"]}, ctx).value is True
    # a list is literal keywords, not regexes
    assert evaluate(cfg, {"check": "grep", "file": "a.txt", "pattern": ["a.p"]}, ctx).value is False
    assert evaluate(cfg, {"check": "grep", "file": "", "pattern": "x"}, ctx).value is False


def git_repo(path: Path) -> Path:
    def run(*args):
        subprocess.run(["git", *args], cwd=path, check=True, capture_output=True)

    path.mkdir(parents=True, exist_ok=True)
    run("init", "-q", "-b", "main")
    run("config", "user.name", "T")
    run("config", "user.email", "t@example.com")
    run("config", "commit.gpgsign", "false")
    (path / "README.md").write_text("x\n", encoding="utf-8")
    run("add", "-A")
    run("commit", "-q", "-m", "init")
    run("branch", "base")
    run("checkout", "-q", "-b", "work")
    return path


def test_changed_paths_committed_uncommitted_and_untracked(cfg, tmp_path):
    repo = git_repo(tmp_path / "r")
    ctx = make_ctx(cfg, repo, "feature", {"module": "crm"})
    ctx.base_ref = "base"
    cond = {"check": "changed_paths", "globs": ["src/tivacare/crm/**", "**/identity/**"]}
    assert evaluate(cfg, cond, ctx).value is False
    (repo / "src" / "tivacare" / "crm").mkdir(parents=True)
    (repo / "src" / "tivacare" / "crm" / "x.py").write_text("x\n", encoding="utf-8")
    assert evaluate(cfg, cond, ctx).value is True  # untracked counts
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "add"], cwd=repo, check=True)
    assert evaluate(cfg, cond, ctx).value is True  # committed counts
    assert evaluate(cfg, {"check": "changed_paths", "globs": "src/other/**"}, ctx).value is False
    ctx.base_ref = "no-such-ref"
    assert evaluate(cfg, cond, ctx).value is None


def test_security_path_named_condition(cfg, tmp_path):
    repo = git_repo(tmp_path / "r")
    ctx = make_ctx(cfg, repo, "feature", {"module": "staff-auth"})
    ctx.base_ref = "base"
    assert evaluate(cfg, "security_path", ctx).value is False
    plan = repo / "docs" / "plans" / "2026-10-29-staff-auth.md"
    plan.parent.mkdir(parents=True)
    plan.write_text("### Task 1: Validate the Entra token\n\n- [ ] x\n", encoding="utf-8")
    ctx2 = make_ctx(cfg, repo, "feature", {"module": "staff-auth"})
    ctx2.base_ref = "base"
    r = evaluate(cfg, "security_path", ctx2)
    assert r.value is True


def test_plan_checks(cfg, tmp_path):
    plans = tmp_path / "docs" / "plans"
    plans.mkdir(parents=True)
    plan = plans / "2026-10-29-crm.md"
    plan.write_text("### Task 1: A\n\n- [x] a\n\n### Task 2: B\n\n- [ ] b\n\n### Task 3: C\n\n- [ ] c\n", encoding="utf-8")
    ctx = make_ctx(cfg, tmp_path, "feature", {"module": "crm"})
    assert ctx.values["plan"] == "docs/plans/2026-10-29-crm.md"
    small = evaluate(cfg, "plan_is_small", ctx)
    assert small.value is True and "3 tasks" in small.reason
    assert evaluate(cfg, {"check": "plan_tasks", "file": "{plan}", "max": 2}, ctx).value is False
    unchecked = evaluate(cfg, "plan_exists_unchecked", ctx)
    assert unchecked.value is True and "2 of 3" in unchecked.reason
    assert evaluate(cfg, "plan_done", ctx).value is False
    from agentflow.sources import tick_task

    tick_task(plan, 2)
    tick_task(plan, 3)
    assert evaluate(cfg, "plan_exists_unchecked", ctx).value is False
    assert evaluate(cfg, "plan_done", ctx).value is True


def test_plan_checks_without_a_plan(cfg, tmp_path):
    ctx = make_ctx(cfg, tmp_path, "feature", {"module": "crm"})
    assert ctx.values["plan"] == ""
    assert evaluate(cfg, "plan_exists_unchecked", ctx).value is False
    assert evaluate(cfg, "plan_done", ctx).value is False
    assert evaluate(cfg, "plan_is_small", ctx).value is None


def test_a_deferred_task_leaves_no_plan_work(cfg, tmp_path):
    plans = tmp_path / "docs" / "plans"
    plans.mkdir(parents=True)
    (plans / "2026-10-29-crm.md").write_text(
        "### Task 1: A\n\n- [x] a\n\n### Task 2: B\n\n> **Deferred (SPEC-crm C1).** Needs `knowledge`.\n",
        encoding="utf-8")
    ctx = make_ctx(cfg, tmp_path, "feature", {"module": "crm"})
    assert evaluate(cfg, "plan_exists_unchecked", ctx).value is False


def test_gate_passed_reads_the_runs_gate_results(cfg, run):
    cond = {"check": "gate_passed", "gate": "floor"}
    ctx = make_ctx(cfg, run.repo, "feature", {"module": "crm"}, run=run)
    assert evaluate(cfg, cond, ctx).value is None  # never ran
    run.emit("gate_result", key="build/build", gate="floor", passed=False, skipped=False, log=None)
    assert evaluate(cfg, cond, ctx).value is False
    run.emit("gate_result", key="build/build", gate="floor", passed=True, skipped=False, log=None)
    assert evaluate(cfg, cond, ctx).value is True
    run.emit("gate_result", key="build/build", gate="floor", passed=False, skipped=True, log=None)
    assert evaluate(cfg, cond, ctx).value is None  # a skipped gate proves nothing
    no_run = make_ctx(cfg, run.repo, "feature", {"module": "crm"})
    assert evaluate(cfg, cond, no_run).value is None


def test_result_signal_reads_the_previous_steps_result(cfg, run):
    cond = {"check": "result_signal", "signal": "signals.needs_contracts"}
    ctx = make_ctx(cfg, run.repo, "feature", {"module": "crm"}, run=run)
    assert evaluate(cfg, cond, ctx).value is None  # no step has finished
    run.emit("step_started", key="define/draft", phase="define", step="draft")
    run.result_path("define/draft").parent.mkdir(parents=True, exist_ok=True)
    run.result_path("define/draft").write_text(
        json.dumps({"status": "done", "signals": {"needs_contracts": True, "kind": "api"}}), encoding="utf-8"
    )
    run.emit("step_finished", key="define/draft", status="done", summary="ok")
    assert evaluate(cfg, cond, ctx).value is True
    assert evaluate(cfg, {"check": "result_signal", "signal": "signals.kind", "equals": "api"}, ctx).value is True
    assert evaluate(cfg, {"check": "result_signal", "signal": "signals.kind", "equals": "ui"}, ctx).value is False
    assert evaluate(cfg, {"check": "result_signal", "signal": "signals.absent"}, ctx).value is False
    named = {"check": "result_signal", "signal": "signals.kind", "step": "define/draft"}
    assert evaluate(cfg, named, ctx).value is True


@pytest.fixture
def fake_gh(tmp_path, monkeypatch):
    """A `gh` on PATH that prints the JSON in `$FAKE_GH_JSON` (or fails when `$FAKE_GH_FAIL` is set)."""
    bin_dir = tmp_path / "ghbin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(
        "#!/usr/bin/env python3\nimport os, sys\n"
        "open(os.environ['FAKE_GH_LOG'], 'a').write(' '.join(sys.argv[1:]) + '\\n')\n"
        "if os.environ.get('FAKE_GH_FAIL'):\n    sys.stderr.write('boom\\n')\n    sys.exit(1)\n"
        "print(os.environ.get('FAKE_GH_JSON', '[]'))\n",
        encoding="utf-8",
    )
    gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_GH_LOG", str(tmp_path / "gh.log"))
    monkeypatch.delenv("FAKE_GH_FAIL", raising=False)
    return tmp_path / "gh.log"


def test_gh_json_parses_and_raises(fake_gh, monkeypatch):
    monkeypatch.setenv("FAKE_GH_JSON", '[{"a": 1}]')
    assert gh_json(["pr", "list"]) == [{"a": 1}]
    assert fake_gh.read_text(encoding="utf-8") == "pr list\n"
    monkeypatch.setenv("FAKE_GH_FAIL", "1")
    with pytest.raises(GhError, match="boom"):
        gh_json(["pr", "list"])


def test_gh_json_without_gh_installed(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(GhError):
        gh_json(["pr", "list"])


def test_branch_merged(cfg, fake_gh, monkeypatch):
    ctx = real_ctx(cfg, "crm", branch="flow/feature/crm")
    cond = {"check": "branch_merged", "branch": "{branch}"}
    monkeypatch.setenv("FAKE_GH_JSON", '[{"number": 7, "mergedAt": "2026-10-20T10:00:00Z"}]')
    assert evaluate(cfg, cond, ctx).value is True
    assert "--head flow/feature/crm" in fake_gh.read_text(encoding="utf-8")
    monkeypatch.setenv("FAKE_GH_JSON", "[]")
    assert evaluate(cfg, cond, ctx).value is False
    monkeypatch.setenv("FAKE_GH_FAIL", "1")
    r = evaluate(cfg, cond, ctx)
    assert r.value is None and "boom" in r.reason


def test_modules_done_uses_merged_pull_requests(cfg, fake_gh, monkeypatch, tmp_path):
    (tmp_path / "docs" / "specs").mkdir(parents=True)
    shutil.copy(FIXTURES / "capability-sample.md", tmp_path / "docs" / "specs" / "CAPABILITY-MAP.md")
    ctx = make_ctx(cfg, tmp_path, "feature", {"module": "beta"})  # depends on alpha
    monkeypatch.setenv("FAKE_GH_JSON", json.dumps([{"headRefName": "flow/feature/alpha"}]))
    assert evaluate(cfg, "deps_merged", ctx).value is True
    monkeypatch.setenv("FAKE_GH_JSON", json.dumps([{"headRefName": "flow/feature/gamma"}]))
    r = evaluate(cfg, "deps_merged", ctx)
    assert r.value is False and "alpha" in r.reason
    monkeypatch.setenv("FAKE_GH_FAIL", "1")
    assert evaluate(cfg, "deps_merged", ctx).value is None


def test_modules_done_with_no_dependencies_needs_no_gh(cfg, monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path))  # no gh at all
    ctx = make_ctx(cfg, PACK, "feature", {"module": "greet"})
    r = evaluate(cfg, "deps_merged", ctx)
    assert r.value is True and "no dependencies" in r.reason


def test_modules_done_unknown_module_is_unknown(cfg):
    ctx = make_ctx(cfg, PACK, "feature", {"module": "ghost"})
    assert evaluate(cfg, "deps_merged", ctx).value is None


# ------------------------------------------------------------------ make_ctx and fmt


def test_make_ctx_values(cfg, tmp_path):
    plans = tmp_path / "docs" / "plans"
    plans.mkdir(parents=True)
    (plans / "2026-10-01-staff-auth.md").write_text("### Task 1: A\n- [ ] a\n", encoding="utf-8")
    ctx = make_ctx(cfg, tmp_path, "feature", {"module": "staff-auth", "topic": "Khách hàng", "issue": 12})
    v = ctx.values
    assert isinstance(ctx, Ctx) and ctx.base_ref == "origin/main" and ctx.run is None
    assert v["module"] == "staff-auth"
    assert v["module_pkg"] == "staff_auth"
    assert v["spec"] == "docs/specs/SPEC-staff-auth.md"
    assert v["plan"] == "docs/plans/2026-10-01-staff-auth.md"
    assert v["idea"] == "docs/idea.md"
    assert v["capability_map"] == "docs/specs/CAPABILITY-MAP.md"
    assert v["topic"] == "Khách hàng"
    assert v["issue"] == "12"
    assert v["run"] == ""
    assert v["slug"] == "staff-auth"
    assert v["plan_target"] == f"docs/plans/{v['date']}-staff-auth-staff-auth.md"
    assert len(v["date"]) == 10 and v["date"][4] == "-"
    assert all(isinstance(x, str) for x in v.values())


def test_make_ctx_without_a_module(cfg, tmp_path):
    v = make_ctx(cfg, tmp_path, "design", {"topic": "Khác biệt"}).values
    assert v["module"] == v["module_pkg"] == v["spec"] == v["plan"] == ""
    assert v["topic"] == "Khác biệt"
    assert v["slug"] == "khac-biet"


def test_make_ctx_carries_the_run_id(cfg, run):
    ctx = make_ctx(cfg, run.repo, "feature", {"module": "crm"}, run=run)
    assert ctx.run is run
    assert ctx.values["run"] == run.id


def test_make_ctx_supplies_every_context_placeholder(cfg, tmp_path):
    from agentflow.config import PROMPT_PLACEHOLDERS

    v = make_ctx(cfg, tmp_path, "feature", {"module": "crm"}).values
    context_names = set("module module_pkg spec plan plan_target idea capability_map topic issue run date slug".split())
    assert context_names <= PROMPT_PLACEHOLDERS
    assert context_names <= set(v)


def test_fmt_leaves_unknown_braces():
    assert fmt("x {module} {not_a_key} {{y}} {", {"module": "crm"}) == "x crm {not_a_key} {{y}} {"


def test_fmt_takes_a_ctx_and_never_raises(cfg, tmp_path):
    ctx = make_ctx(cfg, tmp_path, "feature", {"module": "crm"})
    assert fmt("{module}/{module_pkg}", ctx) == "crm/crm"
    code = "def f(): return {'a': {1, 2}}\n{module}"
    assert fmt(code, ctx) == "def f(): return {'a': {1, 2}}\ncrm"
    assert fmt("{}", ctx) == "{}"
    assert fmt("{0} {module!r}", ctx) == "{0} {module!r}"


def test_fmt_keeps_vietnamese_unchanged():
    assert fmt("{topic}: Khách hàng", {"topic": "Hội thoại"}) == "Hội thoại: Khách hàng"


# ------------------------------------------------------------------ resolve_model


def phase_without_model(cfg, name="plan"):
    return dataclasses.replace(cfg.phases[name], model=None)


def test_resolve_model_orders(cfg):
    build = cfg.phases["build"]
    step = cfg.phases["build"].steps[1]
    assert step.id == "build"
    assert resolve_model(cfg, build, step, "build", {}) == ("sonnet", "phase_role")
    assert resolve_model(cfg, build, step, "review", {}) == ("sonnet", "phase_role")
    assert resolve_model(cfg, cfg.phases["plan"], step, "review", {}) == ("opus", "phase_role")
    assert resolve_model(cfg, phase_without_model(cfg), step, "plan", {}) == ("opus", "agent_role")
    assert resolve_model(cfg, phase_without_model(cfg), step, "build", {}) == ("sonnet", "agent_role")


def test_resolve_model_step_beats_phase(cfg):
    step = dataclasses.replace(cfg.phases["build"].steps[1], model="haiku")
    assert resolve_model(cfg, cfg.phases["build"], step, "build", {}) == ("haiku", "step")


def test_resolve_model_cli_override_is_per_phase(cfg):
    step = dataclasses.replace(cfg.phases["build"].steps[2], model="haiku")
    overrides = {"plan": "fable"}
    assert resolve_model(cfg, cfg.phases["plan"], step, "plan", overrides) == ("fable", "cli")
    assert resolve_model(cfg, cfg.phases["build"], step, "build", overrides) == ("haiku", "step")
    assert resolve_model(cfg, cfg.phases["review"], step, "review", overrides)[1] == "step"


def test_resolve_model_uses_the_phases_agent(cfg):
    phase = dataclasses.replace(cfg.phases["plan"], model=None, agent="codex")
    step = cfg.phases["plan"].steps[0]
    assert resolve_model(cfg, phase, step, "plan", {}) == ("gpt-5-codex", "agent_role")


def test_resolve_model_role_map_without_a_matching_key_uses_default(cfg):
    phase = dataclasses.replace(cfg.phases["build"], model={"default": "sonnet"})
    step = cfg.phases["build"].steps[2]
    assert resolve_model(cfg, phase, step, "review", {}) == ("sonnet", "phase_role")


def test_resolve_model_missing_role_raises(cfg):
    step = cfg.phases["plan"].steps[0]
    with pytest.raises(ValueError, match="no model for role 'nobody'"):
        resolve_model(cfg, phase_without_model(cfg), step, "nobody", {})


# ------------------------------------------------------------------ explain


def lines_by_key(lines):
    return {line.key: line for line in lines if line.verdict != "approval"}


def test_explain_reviewed_spec_skips_its_review(cfg):
    ctx = make_ctx(cfg, PACK, "feature", {"module": "greet"})
    by_key = lines_by_key(explain(cfg, "feature", ctx))
    assert by_key["define/draft"].verdict == "skip" and "spec_exists = true" in by_key["define/draft"].reason
    assert by_key["define/contracts"].verdict == "skip" and "spec_api_reviewed = true" in by_key["define/contracts"].reason
    grill = by_key["define/grill_open_questions"]
    assert grill.verdict == "skip" and "spec_has_open_questions = false" in grill.reason and "0 open items" in grill.reason


def test_explain_unreviewed_spec_runs_api_design_then_asks_approval(cfg, tmp_path):
    ctx = make_ctx(cfg, farewell_repo(tmp_path), "feature", {"module": "farewell"})
    lines = explain(cfg, "feature", ctx)
    by_key = lines_by_key(lines)
    assert by_key["define/contracts"].verdict == "run" and by_key["define/contracts"].model == "opus"
    assert "spec_api_reviewed = false" in by_key["define/contracts"].reason
    assert [line.key for line in lines if line.verdict == "approval"][0] == "define_approved"


def test_explain_feature_with_define_work(cfg):
    ctx = make_ctx(cfg, PACK, "feature", {"module": "knowledge"})  # no spec yet: define drafts one
    lines = explain(cfg, "feature", ctx)
    phases = {line.key: line for line in lines if line.key == line.phase}
    assert list(phases) == list(cfg.phases)
    assert all(line.verdict != "skip" for line in phases.values())
    approvals = [line.key for line in lines if line.verdict == "approval"]
    assert approvals == [f"{name}_approved" for name in ("define", "plan")]  # each phase's `needs_approval` says


def test_explain_finished_spec_raises_no_define_approval(cfg):
    # greet's spec is reviewed with nothing open, so no define step runs and no human is stopped for it
    by_key = lines_by_key(explain(cfg, "feature", make_ctx(cfg, PACK, "feature", {"module": "greet"})))
    assert "define_approved" not in by_key


def test_explain_lists_build_implementer_and_reviewer_models(cfg, tmp_path):
    ctx = make_ctx(cfg, tmp_path, "feature", {"module": "crm"})
    by_key = lines_by_key(explain(cfg, "feature", ctx))
    assert by_key["build/build"].model == "sonnet"
    assert by_key["build/build/review"].model == "sonnet"
    assert by_key["ship/finish"].model is None
    assert by_key["build/build"].verdict == "run"  # no plan yet, so build is not "done"


def test_explain_applies_cli_model_overrides(cfg, tmp_path):
    ctx = make_ctx(cfg, tmp_path, "feature", {"module": "crm"})
    by_key = lines_by_key(explain(cfg, "feature", ctx, {"plan": "fable"}))
    assert by_key["plan/plan"].model == "fable"
    assert by_key["review/branch_review"].model == "sonnet"


def test_explain_skipped_phase_lists_no_steps(cfg):
    ctx = make_ctx(cfg, PACK, "feature", {"module": "greet"})
    keys = [line.key for line in explain(cfg, "feature", ctx)]
    assert not any(k.startswith("define/debug") for k in keys)  # another lane's step is not listed


def test_explain_runs_nested_group_steps_when_the_group_runs(cfg, tmp_path):
    repo = fixture_repo(tmp_path, "SPEC-demo.md")
    ctx = make_ctx(cfg, repo, "feature", {"module": "demo"})
    by_key = lines_by_key(explain(cfg, "feature", ctx))
    assert by_key["define/grill_open_questions"].verdict == "run"
    assert by_key["define/grill_open_questions/grill"].verdict == "run"
    assert by_key["define/grill_open_questions/grill"].model == "opus"
    assert by_key["define/grill_open_questions/apply"].model == "sonnet"
    assert by_key["define/grill_open_questions/answers"].model is None


def test_explain_unknown_pipeline_raises(cfg):
    ctx = make_ctx(cfg, PACK, "feature", {"module": "greet"})
    with pytest.raises(ValueError, match="unknown lane 'nope'"):
        explain(cfg, "nope", ctx)


def test_plan_warnings_for_big_tasks(cfg, tmp_path):
    plans = tmp_path / "docs" / "plans"
    plans.mkdir(parents=True)
    steps = "\n".join(f"- [ ] **Step {i}: s**" for i in range(1, 10))
    (plans / "2026-10-29-crm.md").write_text(f"### Task 1: Big\n\n{steps}\n", encoding="utf-8")
    ctx = make_ctx(cfg, tmp_path, "feature", {"module": "crm"})
    warnings = plan_warnings(ctx)
    assert len(warnings) == 1 and "Task 1" in warnings[0] and "9 checkbox steps" in warnings[0]
    assert plan_warnings(make_ctx(cfg, tmp_path, "feature", {"module": "other"})) == []


def test_cond_result_defaults():
    r = CondResult(True, "because")
    assert r.on_unknown == "ask_human"


# ------------------------------------------------------------------ CLI


def test_cli_explain_spec_greet(capsys, monkeypatch, pack_repo_python):
    from agentflow import flow

    monkeypatch.chdir(pack_repo_python)
    assert flow.main(["explain", "feature", "--module", "greet"]) == 0
    out = capsys.readouterr().out
    assert "– skip" in out and "contracts" in out and "spec_api_reviewed = true" in out
    assert "spec_has_open_questions = false" in out
    farewell_repo(pack_repo_python)
    assert flow.main(["explain", "feature", "--module", "farewell"]) == 0
    out = capsys.readouterr().out
    assert "✓ run" in out and "contracts" in out
    assert "⏸ define_approved" in out


def test_cli_explain_needs_module_or_topic(capsys, monkeypatch, pack_repo_python):
    from agentflow import flow

    monkeypatch.chdir(pack_repo_python)
    assert flow.main(["explain", "feature"]) == 1
    assert "--module" in capsys.readouterr().out


def test_cli_explain_design_by_topic(capsys, monkeypatch, pack_repo_python):
    from agentflow import flow

    monkeypatch.chdir(pack_repo_python)
    assert flow.main(["explain", "design", "--topic", "MISA"]) == 0
    out = capsys.readouterr().out
    assert "⏸ define_approved" in out
    assert "ship_approved" not in out


def test_cli_explain_model_override(capsys, monkeypatch, pack_repo_python):
    from agentflow import flow

    monkeypatch.chdir(pack_repo_python)
    assert flow.main(["explain", "feature", "--module", "greet", "--model", "plan=fable"]) == 0
    assert "fable" in capsys.readouterr().out
    assert flow.main(["explain", "feature", "--module", "greet", "--model", "nonsense"]) == 1
