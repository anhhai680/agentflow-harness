"""The phase loop and phase approvals (Task 7): start, advance, handoff, footers, --from/--to, the CLI."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from agentflow.flow import main
from agentflow.hitl import approve, reject
from agentflow.pipeline import RunOptions, advance, footer_next, start
from agentflow.state import Run, runs_root

DONE = {"status": "done", "summary": "did it"}
PIPE = {"gate_sets": {"build": ["ok"], "verify": ["ok"]}}
APPROVED_SPEC = (
    "# Spec: `demo`\n\n**Status: approved** 2026-10-01\n\n## Objective\n\nA fixture module.\n\n"
    "## Open Questions\n\nNone for this module.\n"
)
PLAN = "# Plan: demo\n\n### Task 1: First thing\n\n**Depends on:** none\n\n- [ ] **Step 1: Do it**\n"
PLAN_PATH = "docs/plans/2026-10-29-demo.md"
IDEA = "docs/idea.md"
ENDS_AT_ANSWERS = {"phases": {"define": {"steps": [
    {"id": "ask", "skill": "grilling", "role": "plan", "prompt": "idea.md", "output": "runs/{run}/questions.md"},
    {"id": "asked", "type": "checkpoint", "wait_for": "answered_file"},
]}}}
QUESTION = {"id": "q1", "question": "Bộ nhớ đệm bao lâu?", "options": [{"key": "A", "label": "1 giờ", "recommended": True}],
            "evidence": [], "kind": "spec_gap"}


@pytest.fixture
def pipe(flow_repo):
    flow_repo.configure(PIPE)
    return flow_repo


def commit_files(flow_repo, files: dict[str, str]) -> None:
    from conftest import run_git

    for rel, text in files.items():
        target = flow_repo.root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    run_git(flow_repo.root, "add", "-A")
    run_git(flow_repo.root, "commit", "-q", "-m", "fixture")


def begin(flow_repo, pipeline: str = "feature", params: dict | None = None, **opts) -> Run:
    return start(flow_repo.cfg, flow_repo.root, pipeline, params or {"module": "demo"}, RunOptions(**opts))


def go(flow_repo, run: Run) -> str:
    return advance(run, flow_repo.cfg, RunOptions())


def steps_called(flow_repo) -> list[str]:
    return [c["step"] for c in flow_repo.calls()]


def types(flow_repo, run, etype: str, field: str = "phase") -> list:
    return [e.get(field) for e in flow_repo.events(run, etype)]


def pairs(argv: list[str]) -> set[tuple[str, str]]:
    return set(zip(argv, argv[1:]))


# --------------------------------------------------------------------------- the phase loop


def test_a_pack_with_no_worktrees_dir_puts_the_worktree_in_the_state_dir(pipe):
    from agentflow import pack

    pipe.configure({**PIPE, "project": {"worktrees_dir": None}})  # the engine defaults have no worktrees_dir
    assert pipe.cfg.project.worktrees_dir is None
    run = begin(pipe)
    worktree = Path(run.state["worktree"])
    assert worktree.is_dir() and worktree == (pack.state_dir(pipe.root) / "worktrees" / run.id).resolve()


# --------------------------------------------------------------------------- the config layers during and across a run


def _user_file(text: str) -> Path:
    from agentflow import layers

    target = layers.user_config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target


def _live_loop(pipe, monkeypatch):
    from agentflow import pack, pipeline

    monkeypatch.setattr(pack, "pack_config", lambda repo: Path(pipe.cfg.path))  # the run's repo's own pack
    run = pipe.new_run()
    return pipeline._Loop(run, pipe.cfg, RunOptions()), run


def test_a_changed_user_file_between_phases_is_reloaded(pipe, monkeypatch):
    loop, run = _live_loop(pipe, monkeypatch)
    loop.reload()
    assert not [e for e in run.events.read() if e["type"].startswith("config_reload")]  # nothing changed
    user = _user_file("defaults:\n  run_budget_usd: 7\n")  # a file that did not exist when the loop began
    loop.reload()
    assert loop.cfg.defaults.run_budget_usd == 7 and run.events.read()[-1]["type"] == "config_reloaded"
    user.write_text("defaults:\n  run_budget_usd: 9\n", encoding="utf-8")
    loop.reload()
    assert loop.cfg.defaults.run_budget_usd == 9


def test_a_broken_user_file_between_phases_is_skipped_with_its_reason(pipe, monkeypatch):
    loop, run = _live_loop(pipe, monkeypatch)
    before = loop.cfg
    user = _user_file("gates:\n  unit: {run: evil}\n")  # a gate is not a personal setting
    loop.reload()
    assert loop.cfg is before
    last = run.events.read()[-1]
    assert last["type"] == "config_reload_skipped" and str(user) in last["reason"] and "not a personal setting" in last["reason"]
    loop.reload()  # the same broken file is reported once
    assert run.events.read()[-1]["seq"] == last["seq"]
    user.write_text("defaults:\n  run_budget_usd: 3\n", encoding="utf-8")  # mended: taken
    loop.reload()
    assert loop.cfg.defaults.run_budget_usd == 3 and run.events.read()[-1]["type"] == "config_reloaded"


def test_the_layers_digest_is_over_each_files_path_and_bytes_in_order(tmp_path):
    from agentflow.config_trust import layers_sha256

    a, b = tmp_path / "a.yaml", tmp_path / "b.yaml"
    a.write_text("x: 1\n", encoding="utf-8")
    b.write_text("y: 2\n", encoding="utf-8")
    digest = layers_sha256([a, b])
    assert len(digest) == 64 and digest == layers_sha256([a, b])
    assert digest != layers_sha256([b, a])
    b.write_text("y: 3\n", encoding="utf-8")
    assert digest != layers_sha256([a, b])


def test_start_records_the_layers_digest_and_each_files_digest(pipe):
    from agentflow.config import layer_files
    from agentflow.config_trust import layers_sha256

    run = begin(pipe)
    options = pipe.events(run, "run_options")[0]
    files = layer_files(pipe.cfg)
    assert options["layers_sha256"] == layers_sha256(files)
    assert sorted(options["layer_digests"]) == sorted(str(p) for p in files)


def test_resume_reports_changed_layer_files(pipe, capsys):
    run = begin(pipe)
    assert go(pipe, run) == "awaiting_approval"
    approve(run, "define", by="me")
    user = _user_file("defaults:\n  run_budget_usd: 7\n")  # appears after the run started
    assert main(["resume", run.id]) == 0
    out = capsys.readouterr().out
    lines = [line for line in out.splitlines() if "layer" in line and str(user) in line]
    assert len(lines) == 1 and "changed since" in lines[0] and run.id in lines[0]
    assert str(pipe.cfg.path) not in lines[0]  # only the files that changed are named


def test_resume_says_nothing_when_no_layer_changed(pipe, capsys):
    run = begin(pipe)
    assert go(pipe, run) == "awaiting_approval"
    approve(run, "define", by="me")
    assert main(["resume", run.id]) == 0
    assert "changed since" not in capsys.readouterr().out


def test_a_run_started_before_the_digests_were_recorded_reports_nothing(pipe):
    from agentflow.config_trust import changed_layers

    run = pipe.new_run()
    run.emit("run_options", config=str(Path(pipe.cfg.path).resolve()), config_sha256="0" * 64)  # the old record
    _user_file("defaults:\n  run_budget_usd: 7\n")
    assert changed_layers(run, pipe.cfg) == []


def test_feature_stops_after_every_phase(pipe):
    run = begin(pipe)
    worktree = Path(run.state["worktree"])
    assert worktree.is_dir() and worktree != pipe.root
    assert run.state["branch"] == "flow/feature/demo"

    assert go(pipe, run) == "awaiting_approval"
    assert run.state["waiting"]["key"] == "define_approved"
    assert steps_called(pipe) and all(s.startswith("define/") for s in steps_called(pipe))
    inbox = run.inbox_path("define_approved").read_text(encoding="utf-8")
    assert f"agentflow approve {run.id} define" in inbox and "## Step results" in inbox and "## Diff" in inbox

    calls = len(pipe.calls())
    assert go(pipe, run) == "awaiting_approval"  # nothing starts until a human approves
    assert len(pipe.calls()) == calls

    approve(run, "define", by="me")
    assert go(pipe, run) == "awaiting_approval"
    assert run.state["waiting"]["key"] == "plan_approved"
    assert "plan/plan" in steps_called(pipe)
    assert types(pipe, run, "phase_approval_raised") == ["define", "plan"]


def test_skipped_phase_raises_no_approval(pipe):
    commit_files(pipe, {"docs/specs/SPEC-demo.md": APPROVED_SPEC, "src/toy/demo/__init__.py": "", PLAN_PATH: PLAN})
    run = begin(pipe)
    assert go(pipe, run) == "awaiting_approval"
    skipped = {e["phase"]: e["reason"] for e in pipe.events(run, "phase_skipped")}
    assert set(skipped) == {"define", "plan"}
    assert "plan_exists_unchecked = true" in skipped["plan"]
    assert types(pipe, run, "phase_approval_raised") == ["build"]
    assert not [s for s in steps_called(pipe) if s.startswith(("define/", "plan/"))]


def test_phase_with_all_steps_skipped_is_skipped(pipe):
    commit_files(pipe, {"docs/specs/SPEC-demo.md": APPROVED_SPEC, "src/toy/demo/__init__.py": ""})
    run = begin(pipe)
    assert go(pipe, run) == "awaiting_approval"
    reasons = {e["phase"]: e["reason"] for e in pipe.events(run, "phase_skipped")}
    assert reasons["define"] == "all steps skipped"
    assert "define" in types(pipe, run, "phase_started")
    assert types(pipe, run, "phase_approval_raised") == ["plan"]


def test_the_first_phase_with_work_is_the_phase_a_real_run_stops_in(pipe):
    """The dashboard offers this phase as a card's drop target, so it must never disagree with the runner."""
    from agentflow.conditions import first_phase_with_work, make_ctx

    commit_files(pipe, {"docs/specs/SPEC-demo.md": APPROVED_SPEC, "src/toy/demo/__init__.py": ""})
    ctx = make_ctx(pipe.cfg, pipe.root, "feature", {"module": "demo"})
    predicted = first_phase_with_work(pipe.cfg, "feature", ctx)
    run = begin(pipe)
    assert go(pipe, run) == "awaiting_approval"
    assert predicted == "plan"
    assert types(pipe, run, "phase_approval_raised") == [predicted]


def test_a_spec_pending_approval_needs_one_human_approval_before_plan(pipe):
    spec = APPROVED_SPEC.replace("2026-10-01", "2026-10-01. **Amended 2026-10-25, pending approval.**")
    commit_files(pipe, {"docs/specs/SPEC-demo.md": spec, "src/toy/demo/__init__.py": ""})
    run = begin(pipe)
    assert go(pipe, run) == "awaiting_approval"
    assert run.state["waiting"]["phase"] == "define"
    assert steps_called(pipe) == []  # the wait costs no agent session
    approve(run, "define", by="me")
    assert go(pipe, run) == "awaiting_approval"
    assert run.state["waiting"]["phase"] == "plan"
    assert types(pipe, run, "phase_approval_raised") == ["define", "plan"]


def test_a_spec_with_no_status_line_goes_straight_to_plan(pipe):
    """Only a spec that says it is waiting stops: the older specs never had a status line to keep up to date."""
    spec = APPROVED_SPEC.replace("**Status: approved** 2026-10-01\n\n", "")
    commit_files(pipe, {"docs/specs/SPEC-demo.md": spec, "src/toy/demo/__init__.py": ""})
    run = begin(pipe)
    assert go(pipe, run) == "awaiting_approval"
    assert types(pipe, run, "phase_approval_raised") == ["plan"]


def test_new_session_per_phase_and_handoff(pipe):
    pipe.script({"define/idea/idea_questions": [{"write": {"notes/idea.md": "câu hỏi\n"}, "result": DONE}],
                 "plan/plan": [{"write": {PLAN_PATH: PLAN}, "result": DONE}]})
    run = begin(pipe)
    for phase in ("define", "plan"):
        assert go(pipe, run) == "awaiting_approval" and run.state["waiting"]["phase"] == phase
        approve(run, phase, by="me")
    assert go(pipe, run) == "awaiting_approval" and run.state["waiting"]["phase"] == "build"

    first_by_phase: dict[str, dict] = {}
    for c in pipe.calls():
        first_by_phase.setdefault(c["step"].split("/")[0], c)
    assert set(first_by_phase) == {"define", "plan", "build"}
    for c in first_by_phase.values():
        assert "--resume" not in c["argv"]
    build_call = first_by_phase["build"]
    assert f"`{run.agent_dir / 'handoff.md'}`" in build_call["argv"][-1]  # the workspace copy the agent may read
    assert (run.agent_dir / "handoff.md").read_bytes() == run.handoff_path().read_bytes()
    handoff = run.handoff_path().read_text(encoding="utf-8")
    assert "notes/idea.md" in handoff and "## define" in handoff and "## plan" in handoff
    assert handoff.index("## plan") < handoff.index("## define")  # newest phase first
    assert run.state["phases"]["define"]["handoff"]


def test_unchecked_plan_goes_straight_to_build(pipe):
    commit_files(pipe, {"docs/specs/SPEC-demo.md": APPROVED_SPEC, "src/toy/demo/__init__.py": "",
                        PLAN_PATH: PLAN})
    run = begin(pipe)
    assert go(pipe, run) == "awaiting_approval"
    assert types(pipe, run, "phase_started") == ["define", "build"]
    assert types(pipe, run, "phase_skipped") == ["define", "plan"]
    assert steps_called(pipe) == ["build/build/task-1/implement"]
    assert "First thing" in pipe.calls()[0]["argv"][-1]
    assert run.state["waiting"]["key"] == "build_approved"


def test_from_to(pipe):
    pipe.script({"plan/plan": [{"write": {PLAN_PATH: PLAN}, "result": DONE}]})
    run = begin(pipe, from_phase="plan", to_phase="verify")
    for phase in ("plan", "build", "verify"):
        assert go(pipe, run) == "awaiting_approval" and run.state["waiting"]["phase"] == phase
        approve(run, phase, by="me")
    assert go(pipe, run) == "done"
    assert types(pipe, run, "phase_started") == ["plan", "build", "verify"]
    assert types(pipe, run, "phase_approval_raised") == ["plan", "build", "verify"]
    assert len(pipe.events(run, "run_finished")) == 1 and run.state["status"] == "done"
    assert not [s for s in steps_called(pipe) if s.startswith(("review/", "define/"))]
    assert run.state["options"]["to_phase"] == "verify"


def test_footer_next():
    assert footer_next("Plan written.\n\nNext: writing-plans — the spec is approved (lane: feature)") == "writing-plans"
    assert footer_next("Next: `executing-plans` — small plan") == "executing-plans"
    assert footer_next("Next: a — one\nmore\nNext: b — two") == "b"
    assert footer_next("no footer here") is None
    assert footer_next("") is None


def test_footer_mismatch_logged(pipe):
    pipe.script({"plan/plan": [{"write": {PLAN_PATH: PLAN}, "result": DONE, "final_text": "Done.\n\nNext: grilling — x"}]})
    run = begin(pipe, from_phase="plan")
    assert go(pipe, run) == "awaiting_approval"
    approve(run, "plan", by="me")
    assert go(pipe, run) == "awaiting_approval"
    (mismatch,) = pipe.events(run, "footer_mismatch")
    assert mismatch["expected"] == "subagent-driven-development" and mismatch["got"] == "grilling"
    assert mismatch["phase"] == "plan"
    assert "build" in types(pipe, run, "phase_started")  # the config route wins
    assert run.state["waiting"]["key"] == "build_approved"


def test_matching_footer_logs_nothing(pipe):
    final = "Done.\n\nNext: subagent-driven-development — build it"
    pipe.script({"plan/plan": [{"write": {PLAN_PATH: PLAN}, "result": DONE, "final_text": final}]})
    run = begin(pipe, from_phase="plan")
    go(pipe, run)
    approve(run, "plan", by="me")
    go(pipe, run)
    assert pipe.events(run, "footer_mismatch") == []


def test_last_phase_no_approval(pipe, git, monkeypatch):
    monkeypatch.setenv("PATH", f"{Path(__file__).parent / 'bin'}{os.pathsep}{os.environ['PATH']}")
    idea = Path(__file__).parent / "fixtures" / "idea.md"
    text = idea.read_text(encoding="utf-8")
    commit_files(pipe, {IDEA: text})
    decided = text.replace("| CRM sandbox / rate limit | Sandbox API key available; defensive caching |\n",
                           "| CRM sandbox / rate limit | Sandbox API key available; defensive caching |\n| Cache TTL | 1 giờ |\n")
    decided = decided.replace("- Is zero data retention available with each LLM provider we shortlist?\n", "")
    assert decided.count("\n") == text.count("\n")  # one row in, one open question out (the doc checks pass)
    pipe.script({"define/decide/apply_topic": [{"write": {IDEA: decided}, "result": DONE}]})
    run = begin(pipe, "design", {"topic": "Cache TTL"})
    worktree = Path(run.state["worktree"])
    assert worktree != pipe.root and run.state["branch"] == "flow/design/cache-ttl"
    assert go(pipe, run) == "awaiting_approval" and run.state["waiting"]["phase"] == "define"
    approve(run, "define", by="me")
    assert go(pipe, run) == "done"
    assert types(pipe, run, "phase_approval_raised") == ["define"]
    assert len(pipe.events(run, "run_finished")) == 1
    assert git(worktree, "log", "-1", "--format=%s").strip() == "Decide Cache TTL in agentic RAG idea doc"
    assert git(worktree, "rev-parse", "--abbrev-ref", "HEAD").strip() == "flow/design/cache-ttl"
    assert run.state["pr_url"]


def test_reject_runs_refine_round(pipe):
    run = begin(pipe)
    assert go(pipe, run) == "awaiting_approval"
    session = pipe.events(run, "agent_session")[0]["session_id"]
    reject(run, "define", "me", "Hỏi thêm về TTL")
    assert go(pipe, run) == "awaiting_approval"
    refined = [c for c in pipe.calls() if c["step"] == "define/idea/idea_questions"][-1]
    assert ("--resume", session) in pairs(refined["argv"])
    assert "Hỏi thêm về TTL" in refined["argv"][-1] and "refine round 1" in refined["argv"][-1]
    assert types(pipe, run, "checkpoint_reused", "key") == ["define/idea/idea_answers"]
    raised = pipe.events(run, "phase_approval_raised")
    assert [(e["phase"], e["round"]) for e in raised] == [("define", 0), ("define", 1)]
    assert run.state["refine"] is None


def test_second_runner_is_refused(pipe):
    from agentflow.pipeline import RunLocked

    run = begin(pipe)
    lock = run.lock()
    assert lock.acquire()
    try:
        with pytest.raises(RunLocked):
            go(pipe, run)
    finally:
        lock.release()


def test_finish_pushes_and_opens_a_draft_pr(pipe, tmp_path, monkeypatch, git):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "gh.log"
    gh = bin_dir / "gh"
    gh.write_text(f'#!/bin/sh\necho "$@" >> "{log}"\necho https://github.com/acme/repo/pull/7\n', encoding="utf-8")
    gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    run = begin(pipe, from_phase="ship")
    assert go(pipe, run) == "done"
    assert run.state["pr_url"] == "https://github.com/acme/repo/pull/7"
    args = log.read_text(encoding="utf-8")
    assert "pr create --draft --base next --head flow/feature/demo" in args  # `fork_from`, the fixture's `next`
    assert "flow/feature/demo" in git(pipe.origin, "branch", "--list")
    assert types(pipe, run, "phase_approval_raised") == []


# --------------------------------------------------------------------------- where runs happen


def test_design_works_on_its_own_branch_whatever_the_main_checkout_holds(pipe, git):
    (pipe.root / "scratch.txt").write_text("wip\n", encoding="utf-8")
    git(pipe.root, "checkout", "-q", "main")
    run = begin(pipe, "design", {"topic": "x"})
    worktree = Path(run.state["worktree"])
    assert worktree != pipe.root and run.state["branch"] == "flow/design/x"
    assert git(worktree, "rev-parse", "--abbrev-ref", "HEAD").strip() == "flow/design/x"
    assert not (worktree / "scratch.txt").exists()
    assert git(pipe.root, "rev-parse", "--abbrev-ref", "HEAD").strip() == "main"


def test_start_rejects_unknown_from_and_to(pipe):
    with pytest.raises(ValueError, match="not a phase"):
        begin(pipe, from_phase="grill")
    with pytest.raises(ValueError, match="after"):
        begin(pipe, from_phase="verify", to_phase="plan")


# --------------------------------------------------------------------------- the CLI


def test_answer_on_last_checkpoint_offers_approval(pipe, monkeypatch, capsys):
    pipe.configure({**PIPE, **ENDS_AT_ANSWERS})
    pipe.script({"define/ask": [{"result": {"status": "done", "summary": "asked", "questions": [QUESTION]}}]})
    run = begin(pipe)
    assert go(pipe, run) == "waiting_human"
    assert run.state["waiting"]["key"] == "define/asked"
    prompts: list[str] = []
    monkeypatch.setattr("builtins.input", lambda prompt="": prompts.append(prompt) or "y")
    assert main(["answer", run.id, "--set", "q1=A", "--by", "tester", "--config", str(pipe.cfg.path)]) == 0
    assert prompts == ["Approve define now? [y/N] "]
    run = Run.load(pipe.root, run.id)
    approved = pipe.events(run, "phase_approved")
    assert [(e["phase"], e["by"]) for e in approved] == [("define", "tester")]
    assert run.state["waiting"]["key"] == "plan_approved"  # the run went on to the next stop
    assert "plan_approved" in capsys.readouterr().out


def test_answer_declined_leaves_the_approval_waiting(pipe, monkeypatch):
    pipe.configure({**PIPE, **ENDS_AT_ANSWERS})
    pipe.script({"define/ask": [{"result": {"status": "done", "summary": "asked", "questions": [QUESTION]}}]})
    run = begin(pipe)
    go(pipe, run)
    monkeypatch.setattr("builtins.input", lambda prompt="": "")
    assert main(["answer", run.id, "--set", "q1=A", "--config", str(pipe.cfg.path)]) == 0
    run = Run.load(pipe.root, run.id)
    assert run.state["waiting"]["key"] == "define_approved" and pipe.events(run, "phase_approved") == []


def test_resume_cli_continues(pipe, capsys):
    run = begin(pipe)
    assert go(pipe, run) == "awaiting_approval"
    approve(run, "define", by="me")
    assert main(["resume", run.id]) == 0
    run = Run.load(pipe.root, run.id)
    assert run.state["waiting"]["key"] == "plan_approved"
    assert "plan/plan" in steps_called(pipe)
    out = capsys.readouterr().out
    assert f"agentflow approve {run.id} plan" in out


def test_approve_cli_continues_unless_told_not_to(pipe):
    pipe.script({"plan/plan": [{"write": {PLAN_PATH: PLAN}, "result": DONE}]})
    run = begin(pipe)
    go(pipe, run)
    assert main(["approve", run.id, "define", "--no-continue"]) == 0
    assert not [s for s in steps_called(pipe) if s.startswith("plan/")]
    run = Run.load(pipe.root, run.id)
    assert run.state["phases"]["define"]["status"] == "approved"
    assert main(["resume", run.id]) == 0
    assert main(["approve", run.id, "plan"]) == 0  # records, then runs on to build's approval
    run = Run.load(pipe.root, run.id)
    assert run.state["waiting"]["key"] == "build_approved"


def test_run_cli_starts_and_stops(pipe, capsys):
    assert main(["run", "feature", "--module", "demo", "--config", str(pipe.cfg.path)]) == 0
    out = capsys.readouterr().out
    (run_dir,) = list(runs_root(pipe.root).iterdir())
    run = Run.load(pipe.root, run_dir.name)
    assert run.state["waiting"]["key"] == "define_approved"
    assert run.id in out and f"agentflow approve {run.id} define" in out
    assert run.state["options"]["config"] == str(pipe.cfg.path)


def test_run_cli_model_override(pipe):
    assert main(["run", "feature", "--module", "demo", "--model", "define=fable",
                 "--config", str(pipe.cfg.path)]) == 0
    assert ("--model", "fable") in pairs(pipe.calls()[0]["argv"])


def test_run_cli_preflight_missing_binary(pipe, capsys):
    pipe.configure({**PIPE, "agents": {"claude": {"command": ["agentflow-no-such-binary"]}}})
    assert main(["run", "feature", "--module", "demo", "--config", str(pipe.cfg.path)]) == 1
    out = capsys.readouterr().out
    assert "agentflow-no-such-binary" in out and "not found" in out and "Traceback" not in out
    assert not runs_root(pipe.root).exists()
    assert main(["run", "feature", "--module", "demo", "--dry-run", "--config", str(pipe.cfg.path)]) == 0
    assert "agentflow-no-such-binary" in capsys.readouterr().out


def test_dry_run_feature_for_a_new_module_with_a_task(pipe):
    assert main(["run", "feature", "--module", "e", "--task", "t", "--dry-run", "--config", str(pipe.cfg.path)]) == 0


def test_dry_run_feature(pipe, capsys):
    from agentflow import pack

    assert main(["run", "feature", "--module", "demo", "--dry-run", "--config", str(pipe.cfg.path)]) == 0
    out = capsys.readouterr().out
    for phase in ("define", "plan", "build", "verify", "review", "ship"):
        assert f"\n{phase}:" in out or out.startswith(f"{phase}:") or f"{phase}: " in out
    assert "fake_agent.py" in out and "--model opus" in out and "--effort medium" in out
    assert out.index("plan/plan") < out.index("⏸ plan_approved") < out.index("build/build")
    assert "build/build/task-N/implement" in out and "Task N" in out
    assert "echo fine" in out  # the build gate set's command
    assert "⏸ ship_approved" not in out and "⏸ review_approved" in out
    assert "gh pr create --draft" in out
    assert pipe.calls() == []
    assert not runs_root(pipe.root).exists()
    assert not (pack.state_dir(pipe.root) / "worktrees").exists()


def test_run_cli_detach_starts_a_background_resume(pipe, monkeypatch):
    import subprocess

    spawned: list[dict] = []
    real_popen = subprocess.Popen

    def popen(command, *args, **kwargs):
        if isinstance(command, list) and "resume" in command:
            spawned.append({"command": command, **kwargs})
            return None
        return real_popen(command, *args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", popen)
    assert main(["run", "feature", "--module", "demo", "--detach", "--config", str(pipe.cfg.path)]) == 0
    (call,) = spawned
    (run_dir,) = list(runs_root(pipe.root).iterdir())
    assert call["command"][-4:] == ["resume", run_dir.name, "--config", str(pipe.cfg.path)]
    assert call["start_new_session"] is True and call["stdin"] is not None
    assert pipe.calls() == []  # the foreground process started the run and nothing else
    assert Run.load(pipe.root, run_dir.name).state["worktree"]


# --------------------------------------------------------------------------- the inherited environment


def _env_events(run) -> list[dict]:
    return [e for e in run.events.read() if e["type"] == "env_dropped"]


def test_a_run_records_the_variables_it_does_not_inherit_by_name_only(pipe, monkeypatch):
    (Path(pipe.cfg.path).parent.parent / ".env").write_text("X_URL=leak-sentinel-7f3a\n", encoding="utf-8")
    monkeypatch.setenv("X_URL", "leak-sentinel-7f3a")
    run = begin(pipe)
    assert go(pipe, run) == "awaiting_approval"
    (event,) = _env_events(run)
    assert event["names"] == ["X_URL"] and run.state["env_dropped"] == ["X_URL"]
    for name in ("events.jsonl", "state.json"):
        assert "leak-sentinel-7f3a" not in (run.dir / name).read_text(encoding="utf-8")
    approve(run, "define", by="me")
    assert main(["resume", run.id]) == 0  # the same set again: nothing new
    assert len(_env_events(run)) == 1


def test_a_changed_set_is_recorded_again(pipe, monkeypatch):
    dotenv = Path(pipe.cfg.path).parent.parent / ".env"
    dotenv.write_text("X_URL=a1\n", encoding="utf-8")
    monkeypatch.setenv("X_URL", "a1")
    run = begin(pipe)
    assert go(pipe, run) == "awaiting_approval"
    monkeypatch.delenv("X_URL")
    approve(run, "define", by="me")
    assert main(["resume", run.id]) == 0
    assert [e["names"] for e in _env_events(run)] == [["X_URL"], []]
    run.refresh()
    assert run.state["env_dropped"] == []


def test_a_run_with_nothing_to_drop_records_nothing(pipe, monkeypatch):
    run = begin(pipe)
    assert go(pipe, run) == "awaiting_approval"
    assert _env_events(run) == [] and run.state["env_dropped"] == []
