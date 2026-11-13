"""The bug lane (`expect: red|green`), the eval lane (`on_fail: block`), the security overlay and the
`--issue/--log/--task` intake (Task 10)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from agentflow.conditions import make_ctx
from agentflow.hitl import approve, reject
from agentflow.pipeline import RunOptions, raise_phase_approval, start
from agentflow.state import Run

AGENTFLOW = Path(__file__).resolve().parents[1]
BIN = AGENTFLOW / "tests" / "bin"
RED = "build/regression_test"
FIX = "build/fix"
EVAL = "verify/eval_gate"
SKILL_LINE = "Also use the `security-and-hardening` skill."
GREEN_GATES = {"gate_sets": {"build": ["ok"], "verify": ["ok"]}}
REGRESSION = "eval: retrieval recall@5 0.71 below the baseline 0.80"
EVAL_GATES = {"gates": {"floor": {"run": "true"}, "eval": {"run": f"echo '{REGRESSION}'; exit 1", "when_exists": None}}}
ISSUE = {"title": "Lỗi đăng nhập", "body": "Không đăng nhập được bằng Entra sau khi đổi mật khẩu.",
         "url": "https://github.com/example/repo/issues/42"}


def done(summary: str = "ok", **signals: str) -> dict:
    result: dict = {"status": "done", "summary": summary}
    if signals:
        result["signals"] = dict(signals)
    return {"result": result}


def bug_run(flow_repo, params: dict | None = None) -> Run:
    flow_repo.run = Run.create(flow_repo.cfg, flow_repo.root, "bug", params or {"task": "đăng nhập lỗi"})
    return flow_repo.run


def eval_run(flow_repo) -> Run:
    flow_repo.run = Run.create(flow_repo.cfg, flow_repo.root, "eval", {"task": "đổi prompt"})
    return flow_repo.run


def send_back(flow_repo, run: Run, phase: str, note: str) -> None:
    """Raise the phase's approval and reject it, so the next pass of the phase is a refine round."""
    run.emit("run_updated", worktree=str(flow_repo.root))
    raise_phase_approval(flow_repo.cfg, run, phase)
    reject(run, phase, by="me", note=note)


def run_phase(flow_repo, run: Run, name: str):
    phase = flow_repo.cfg.phases[name]
    return flow_repo.executor(run).run_steps(phase, phase.steps, name)


def run_step(flow_repo, run: Run, name: str, step_id: str):
    """One step of a phase, as a run reaches it."""
    phase = flow_repo.cfg.phases[name]
    return flow_repo.executor(run).run_steps(phase, [s for s in phase.steps if s.id == step_id], name)


def calls_for(flow_repo, key: str) -> list[dict]:
    return [c for c in flow_repo.calls() if c["step"] == key]


def raised(flow_repo, run: Run) -> dict:
    return flow_repo.events(run, "hitl_raised")[-1]


def pairs(argv: list[str]) -> set[tuple[str, str]]:
    return set(zip(argv, argv[1:]))


def commit_file(flow_repo, rel: str, text: str, message: str) -> None:
    from conftest import run_git

    target = flow_repo.root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    run_git(flow_repo.root, "add", "-A")
    run_git(flow_repo.root, "commit", "-q", "-m", message)


def plan_text(module: str, task_line: str) -> str:
    return (f"# Plan: {module}\n\n## Global Constraints\n\n- none\n\n### Task 1: {task_line}\n\n"
            "**Depends on:** none\n\n- [ ] **Step 1: test**\n- [ ] **Step 2: code**\n")


# --------------------------------------------------------------------------- expect: red | green


def test_red_test_that_passes_is_rejected(flow_repo):
    run = bug_run(flow_repo)
    flow_repo.script({RED: [
        {"write": {"tests/test_login.py": "def test_login(): assert True\n"}, **done("wrote it", test_command="true")},
        {"write": {"tests/test_login.py": "def test_login(): assert False\n"}, **done("now red", test_command="false")},
    ]})
    assert run_step(flow_repo, run, "build", "regression_test") == ("stopped", True)
    stop = raised(flow_repo, run)
    assert stop["kind"] == "red_test_passes" and stop["key"] == RED
    assert "does not reproduce" in stop["summary"]
    assert len(calls_for(flow_repo, RED)) == 1  # no fix call: a passing red test goes to the human
    assert not flow_repo.events(run, "fix_attempt")
    assert run.state["steps"][RED]["status"] == "waiting_human"
    checked = flow_repo.events(run, "expectation_checked")[-1]
    assert checked["expect"] == "red" and checked["ok"] is False and checked["exit_code"] == 0
    assert Path(checked["log"]).read_text(encoding="utf-8").startswith("$ true\n")

    approve(run, RED, by="me")  # resumes the same session: it must make the test fail
    assert run_step(flow_repo, run, "build", "regression_test") == ("done", True)
    resumed = calls_for(flow_repo, RED)[1]
    session = flow_repo.events(run, "agent_session")[0]["session_id"]
    assert ("--resume", session) in pairs(resumed["argv"])
    assert "red_test_passes" in resumed["argv"][-1]
    assert run.state["steps"][RED]["status"] == "done"
    last = flow_repo.events(run, "expectation_checked")[-1]
    assert last["ok"] is True and last["exit_code"] == 1 and last["command"] == "false"


def test_red_without_a_test_command_is_blocked(flow_repo):
    run = bug_run(flow_repo)
    flow_repo.script({RED: [{"write": {"tests/test_login.py": "x\n"}, **done("wrote a test")}]})
    assert run_step(flow_repo, run, "build", "regression_test") == ("stopped", True)
    stop = raised(flow_repo, run)
    assert stop["kind"] == "blocked" and "signals.test_command" in stop["summary"]
    assert len(calls_for(flow_repo, RED)) == 1


def test_red_then_green(flow_repo, git):
    flow_repo.configure(GREEN_GATES)
    run = bug_run(flow_repo)
    flow_repo.script({
        RED: [{"write": {"tests/test_login.py": "def test(): assert fixed\n"}, **done("red", test_command="test -f fixed")}],
        FIX: [
            {"write": {"src/login.py": "half = 1\n"}, **done("first try")},
            {"write": {"fixed": "yes\n"}, **done("fixed the root cause")},
        ],
    })
    assert run_step(flow_repo, run, "build", "regression_test") == ("done", True)
    red = flow_repo.events(run, "expectation_checked")[-1]
    assert red["expect"] == "red" and red["ok"] is True and red["exit_code"] == 1
    assert not flow_repo.events(run, "step_committed")  # the test lands with the fix

    assert run_step(flow_repo, run, "build", "fix") == ("done", True)
    fix_calls = calls_for(flow_repo, FIX)
    assert len(fix_calls) == 2
    attempt = flow_repo.events(run, "fix_attempt")[-1]
    assert attempt["key"] == FIX and attempt["gates"] == ["test_command"]  # the step's gates passed; green did not
    assert "test_command" in fix_calls[1]["argv"][-1]
    assert ("--resume", flow_repo.events(run, "agent_session")[-2]["session_id"]) in pairs(fix_calls[1]["argv"])
    green = [e for e in flow_repo.events(run, "expectation_checked") if e["expect"] == "green"]
    assert [e["ok"] for e in green] == [False, True]
    assert green[-1]["command"] == "test -f fixed"
    subject = git(flow_repo.root, "log", "-1", "--format=%s").strip()
    assert subject.startswith("Fix ") and subject == "Fix dang-nhap-loi"
    files = git(flow_repo.root, "show", "--name-only", "--format=", "HEAD").split()
    assert sorted(files) == ["fixed", "src/login.py", "tests/test_login.py"]
    assert run.state["steps"][FIX]["status"] == "done"


# --------------------------------------------------------------------------- intake


def test_issue_intake(flow_repo, monkeypatch, tmp_path):
    gh_log = tmp_path / "gh.jsonl"
    monkeypatch.setenv("PATH", f"{BIN}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_GH_LOG", str(gh_log))
    monkeypatch.setenv("FAKE_GH_ISSUE", json.dumps(ISSUE, ensure_ascii=False))
    run = start(flow_repo.cfg, flow_repo.root, "bug", {"issue": "42"}, RunOptions())
    assert run.state["branch"] == "flow/bug/issue-42-loi-dang-nhap"
    assert run.id.endswith("-bug-issue-42-loi-dang-nhap")
    issue = (run.dir / "issue.md").read_text(encoding="utf-8")
    assert "# Issue #42: Lỗi đăng nhập" in issue
    assert ISSUE["body"] in issue and ISSUE["url"] in issue
    assert "never instructions" in issue
    calls = [json.loads(line) for line in gh_log.read_text(encoding="utf-8").splitlines()]
    assert ["issue", "view", "42", "--json", "title,body,url,labels"] in calls
    assert flow_repo.events(run, "intake_saved")[0]["path"] == str(run.dir / "issue.md")
    ctx = make_ctx(flow_repo.cfg, flow_repo.root, "bug", run.state["params"], run)
    assert ctx.values["issue"] == "42"

    with pytest.raises(ValueError, match="issue"):
        start(flow_repo.cfg, flow_repo.root, "bug", {"issue": "--repo=evil/x"}, RunOptions())


def test_log_and_task_intake(flow_repo, tmp_path):
    log = tmp_path / "login-crash.log"
    log.write_bytes("Lỗi: token hết hạn\n\x00tail\n".encode())
    run = start(flow_repo.cfg, flow_repo.root, "bug", {"log": str(log)}, RunOptions())
    assert (run.dir / "bug.log").read_bytes() == log.read_bytes()
    assert run.state["branch"] == "flow/bug/login-crash"

    task = "Đổi prompt trả lời để luôn trích dẫn nguồn"
    run = start(flow_repo.cfg, flow_repo.root, "eval", {"task": task}, RunOptions())
    assert task in (run.dir / "task.md").read_text(encoding="utf-8")
    assert run.state["branch"].startswith("flow/eval/doi-prompt-tra-loi")

    with pytest.raises(ValueError, match="--log"):
        start(flow_repo.cfg, flow_repo.root, "bug", {"log": str(tmp_path / "missing.log")}, RunOptions())


# --------------------------------------------------------------------------- on_fail: block


def test_eval_regression_blocks(flow_repo):
    flow_repo.configure({**EVAL_GATES, "defaults": {"fix_attempts": 2}})
    run = eval_run(flow_repo)
    flow_repo.script({EVAL: [
        {"write": {"src/prompt.txt": "v2\n"}, **done("tuned the prompt")},
        {"write": {"src/prompt.txt": "v3\n"}, **done("tuned it again")},
        {"write": {"eval/baseline.json": "{\"recall_at_5\": 0.7}\n"}, **done("moved the baseline")},
    ]})
    assert run_phase(flow_repo, run, "verify") == ("stopped", True)
    calls = calls_for(flow_repo, EVAL)
    assert len(calls) == 2
    assert [e["attempt"] for e in flow_repo.events(run, "fix_attempt")] == [1, 2]
    first = calls[0]["argv"][-1]
    assert REGRESSION in first and "Never edit the eval baseline" in first  # the generic rule, not a path
    assert "verification-before-completion" in first  # the fix session verifies with the skill
    session = flow_repo.events(run, "agent_session")[0]["session_id"]
    assert "--resume" not in calls[0]["argv"] and ("--resume", session) in pairs(calls[1]["argv"])
    stop = raised(flow_repo, run)
    assert stop["kind"] == "eval_regression" and stop["key"] == EVAL
    assert REGRESSION in run.inbox_path(EVAL).read_text(encoding="utf-8")
    assert run.state["steps"][EVAL]["status"] == "waiting_human"

    approve(run, EVAL, by="me")  # resumes the fix session; the agent may still never touch the baseline
    assert run_phase(flow_repo, run, "verify") == ("stopped", True)
    assert ("--resume", session) in pairs(calls_for(flow_repo, EVAL)[2]["argv"])
    stop = raised(flow_repo, run)
    assert stop["kind"] == "guard_violation" and "eval/baseline.json" in stop["summary"]
    assert run.state["steps"][EVAL]["status"] == "failed"
    assert not flow_repo.events(run, "step_committed")


def test_eval_gate_that_passes_calls_no_agent(flow_repo):
    flow_repo.configure({"gates": {"floor": {"run": "true"}, "eval": {"run": "true", "when_exists": None}}})
    run = eval_run(flow_repo)
    assert run_phase(flow_repo, run, "verify") == ("done", True)
    assert flow_repo.calls() == []
    assert run.state["steps"][EVAL]["status"] == "done"


def test_eval_gate_refine_round_fixes_with_the_note(flow_repo):
    gate = {"run": "grep -q good src/state.txt || { echo 'recall dropped'; exit 1; }", "when_exists": None}
    flow_repo.configure({"gates": {"floor": {"run": "true"}, "eval": gate}})
    commit_file(flow_repo, "src/state.txt", "good\n", "Add state")
    run = eval_run(flow_repo)
    assert run_phase(flow_repo, run, "verify") == ("done", True)
    assert flow_repo.calls() == []
    send_back(flow_repo, run, "verify", "Chạy lại với dữ liệu mới")
    (flow_repo.root / "src" / "state.txt").write_text("bad\n", encoding="utf-8")
    flow_repo.script({EVAL: [{"write": {"src/state.txt": "good again\n"}, **done("restored recall")}]})

    assert run_phase(flow_repo, run, "verify") == ("done", True)
    calls = calls_for(flow_repo, EVAL)
    assert len(calls) == 1 and "--resume" not in calls[0]["argv"]
    assert "recall dropped" in calls[0]["argv"][-1] and "Chạy lại với dữ liệu mới" in calls[0]["argv"][-1]
    assert run.state["steps"][EVAL]["status"] == "done"


def test_gate_fix_after_a_refused_effort_change_keeps_the_gate_output(flow_repo):
    gate = {"run": "grep -q good src/state.txt || { echo 'recall dropped'; exit 1; }", "when_exists": None}
    flow_repo.configure({"gates": {"floor": {"run": "true"}, "eval": gate}})
    run = eval_run(flow_repo)
    flow_repo.script({EVAL: [
        {"result": {"status": "effort_change", "effort": "high", "reason": "retrieval is subtle"}},
        {"write": {"src/state.txt": "good\n"}, **done("restored recall")},
    ]})
    assert run_phase(flow_repo, run, "verify") == ("done", True)
    calls = calls_for(flow_repo, EVAL)
    assert len(calls) == 2 and ("--effort", "medium") in pairs(calls[1]["argv"])  # the change was refused
    assert "recall dropped" in calls[0]["argv"][-1]  # the gate output reached the first call ...
    assert "--resume" in calls[1]["argv"] and "refused" in calls[1]["argv"][-1]  # ... and a refusal resumes that session


def test_red_checked_again_in_a_refine_round(flow_repo):
    run = bug_run(flow_repo)
    flow_repo.script({RED: [
        {"write": {"tests/test_login.py": "red\n"}, **done("red", test_command="false")},
        {"write": {"tests/test_login.py": "now green\n"}, "result": {
            "status": "done", "summary": "rewrote it", "signals": {"test_command": "true"}}},
    ]})
    assert run_step(flow_repo, run, "build", "regression_test") == ("done", True)
    send_back(flow_repo, run, "build", "Kiểm tra cả token hết hạn")
    assert run_phase(flow_repo, run, "build")[0] == "stopped"
    assert raised(flow_repo, run)["kind"] == "red_test_passes"
    assert len(calls_for(flow_repo, RED)) == 2


# --------------------------------------------------------------------------- security overlay


def test_security_overlay(flow_repo):
    flow_repo.configure(GREEN_GATES)
    commit_file(flow_repo, "toy/staff_auth/__init__.py", "", "Start staff_auth")
    run = flow_repo.new_run({"module": "staff-auth"})
    target = make_ctx(flow_repo.cfg, flow_repo.root, "feature", run.state["params"], run).values["plan_target"]
    plan = plan_text("staff-auth", "Validate Entra tokens in toy/staff_auth/tokens.py")
    flow_repo.script({"plan/plan": [{"write": {target: plan}, **done("planned")}]})

    assert run_phase(flow_repo, run, "plan") == ("done", True)
    plan_prompt = calls_for(flow_repo, "plan/plan")[0]["argv"][-1]
    assert SKILL_LINE in plan_prompt
    assert run_phase(flow_repo, run, "build") == ("done", True)  # the offer is on the plan's approval page instead
    assert not [e for e in flow_repo.events(run, "hitl_raised") if e["kind"] == "checkpoint"]
    build_calls = [c for c in flow_repo.calls() if c["step"].startswith("build/")]
    assert build_calls and not any("doubt-driven" in c["argv"][-1] for c in build_calls)  # never run by the runner
    assert SKILL_LINE not in build_calls[0]["argv"][-1]  # the overlay adds to plan and review only

    assert run_phase(flow_repo, run, "review") == ("done", True)
    branch = calls_for(flow_repo, "review/branch_review")[0]["argv"]
    security = calls_for(flow_repo, "review/security_review")[0]["argv"]
    assert ("--agent", "security-auditor") in pairs(security)
    assert "--agent" not in branch
    assert SKILL_LINE in branch[-1]
    assert security[-1].count("`security-and-hardening` skill") == 1  # its own skill line is not repeated


def test_no_overlay_off_security_paths(flow_repo):
    flow_repo.configure(GREEN_GATES)
    run = flow_repo.new_run({"module": "crm"})
    target = make_ctx(flow_repo.cfg, flow_repo.root, "feature", run.state["params"], run).values["plan_target"]
    plan = plan_text("crm", "Cache the product list in toy/crm/client.py")
    flow_repo.script({"plan/plan": [{"write": {target: plan}, **done("planned")}]})

    assert run_phase(flow_repo, run, "plan") == ("done", True)
    assert SKILL_LINE not in calls_for(flow_repo, "plan/plan")[0]["argv"][-1]
    assert run_phase(flow_repo, run, "build") == ("done", True)
    assert run_phase(flow_repo, run, "review") == ("done", True)
    assert "review/security_review" in {e["key"] for e in flow_repo.events(run, "step_skipped")}
    branch = calls_for(flow_repo, "review/branch_review")[0]["argv"]
    assert "--agent" not in branch and SKILL_LINE not in branch[-1]


# --------------------------------------------------------------------------- review fix round 1: the test command


def red_with(flow_repo, command: str) -> Run:
    run = bug_run(flow_repo)
    flow_repo.script({RED: [{"write": {"tests/test_login.py": "x\n"}, **done("wrote it", test_command=command)}]})
    assert run_step(flow_repo, run, "build", "regression_test") == ("stopped", True)
    return run


@pytest.mark.parametrize("command", [
    "touch marker; false", "false && touch marker", "false || touch marker", "touch marker | false",
    "test -f $(touch marker)", "test -f `touch marker`", "touch marker > out", "false\ntouch marker",
    "touch marker &",
])
def test_test_command_with_shell_operators_is_blocked(flow_repo, command):
    run = red_with(flow_repo, command)
    stop = raised(flow_repo, run)
    assert stop["kind"] == "blocked" and "shell" in stop["summary"]
    assert not (flow_repo.root / "marker").exists()  # never executed
    assert not flow_repo.events(run, "expectation_checked")


def test_test_command_not_allowed_by_settings_is_blocked(flow_repo):
    for command in ("mkdir marker", "touch .claude/marker"):  # no allow rule; an allow rule overridden by a deny
        run = red_with(flow_repo, command)
        stop = raised(flow_repo, run)
        assert stop["kind"] == "blocked" and "Bash(...) allow rule" in stop["summary"]
        assert not (flow_repo.root / "marker").exists() and not (flow_repo.root / ".claude" / "marker").exists()
        assert not flow_repo.events(run, "expectation_checked")


def test_test_command_that_touches_a_forbidden_path_is_a_guard_violation(flow_repo):
    run = red_with(flow_repo, "touch CONSTRAINTS.md README.md")  # no content change: not a violation
    assert raised(flow_repo, run)["kind"] == "red_test_passes"

    (flow_repo.root / "eval").mkdir()
    run = red_with(flow_repo, "touch eval/baseline.json")
    stop = raised(flow_repo, run)
    assert stop["kind"] == "guard_violation" and "eval/baseline.json" in stop["summary"]
    assert run.state["steps"][RED]["status"] == "failed"


def test_test_command_runs_without_a_shell_and_is_killed_at_the_timeout(tmp_path):
    from agentflow.bug_eval import run_test_command

    (tmp_path / "a b").write_text("x\n", encoding="utf-8")
    ran = run_test_command(["test", "-f", "a b"], tmp_path, tmp_path / "log" / "t.log", timeout_s=5, tail_lines=5)
    assert ran.exit_code == 0 and (tmp_path / "log" / "t.log").read_text(encoding="utf-8").startswith("$ test -f 'a b'")
    ran = run_test_command(["sleep", "30"], tmp_path, tmp_path / "log" / "s.log", timeout_s=1, tail_lines=5)
    assert ran.timed_out and "killed after 1s" in ran.log_path.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- review fix round 1: lanes end clean


def test_eval_gate_fix_is_committed(flow_repo, git):
    gate = {"run": "grep -q good src/state.txt || { echo 'recall dropped'; exit 1; }", "when_exists": None}
    flow_repo.configure({"gates": {"floor": {"run": "true"}, "eval": gate}})
    run = eval_run(flow_repo)
    flow_repo.script({EVAL: [{"write": {"src/state.txt": "good\n"}, **done("restored recall")}]})
    assert run_phase(flow_repo, run, "verify") == ("done", True)
    assert git(flow_repo.root, "status", "--porcelain") == ""
    assert git(flow_repo.root, "log", "-1", "--format=%s").strip() == "Fix eval_gate gate failures"
    assert git(flow_repo.root, "show", "--name-only", "--format=", "HEAD").split() == ["src/state.txt"]
    assert flow_repo.events(run, "step_committed")[-1]["key"] == EVAL


def test_eval_gate_fix_that_changes_nothing_commits_nothing(flow_repo, git, tmp_path):
    once = tmp_path / "failed-once"
    gate = {"run": f"test -f {once} || {{ touch {once}; echo flaky; exit 1; }}", "when_exists": None}
    flow_repo.configure({"gates": {"floor": {"run": "true"}, "eval": gate}})
    run = eval_run(flow_repo)
    before = flow_repo.head()
    assert run_phase(flow_repo, run, "verify") == ("done", True)
    assert len(calls_for(flow_repo, EVAL)) == 1
    assert flow_repo.head() == before and not flow_repo.events(run, "step_committed")
    assert run.state["steps"][EVAL]["status"] == "done"


def test_feature_test_phase_ends_clean(flow_repo, git):
    gate = {"run": "grep -q good src/state.txt || { echo 'recall dropped'; exit 1; }", "when_exists": None}
    flow_repo.configure({**GREEN_GATES, "gates": {"floor": {"run": "true"}, "eval": gate}})
    commit_file(flow_repo, "toy/agent/policy.py", "THRESHOLD = 0.5\n", "Tune policy")
    run = flow_repo.new_run({"module": "agent"})
    flow_repo.script({
        "verify/verify": [{"write": {"toy/agent/fixup.py": "x = 1\n"}, **done("fixed a lint finding")}],
        "verify/eval_gate": [{"write": {"src/state.txt": "good\n"}, **done("restored recall")}],
    })
    assert run_phase(flow_repo, run, "verify") == ("done", True)
    assert git(flow_repo.root, "status", "--porcelain") == ""
    subjects = git(flow_repo.root, "log", "-2", "--format=%s").splitlines()
    assert subjects == ["Fix eval_gate gate failures", "Fix issues found in verification"]


def test_eval_lane_ends_clean_through_finish(flow_repo, git, tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", f"{BIN}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_GH_LOG", str(tmp_path / "gh.jsonl"))
    gate = {"run": "grep -q good src/state.txt || { echo 'recall dropped'; exit 1; }", "when_exists": None}
    flow_repo.configure({"gates": {"floor": {"run": "true"}, "eval": gate}})
    flow_repo.script({
        "build/change": [{"write": {"toy/agent/policy.py": "THRESHOLD = 0.6\n"}, **done("raised it")}],
        EVAL: [{"write": {"src/state.txt": "good\n"}, **done("restored recall")}],
    })
    from agentflow.pipeline import advance

    run = start(flow_repo.cfg, flow_repo.root, "eval", {"task": "Nâng ngưỡng"}, RunOptions())
    worktree = Path(run.state["worktree"])
    for phase in ("build", "verify", "review"):
        assert advance(run, flow_repo.cfg, RunOptions()) == "awaiting_approval"
        assert run.state["waiting"]["phase"] == phase
        assert git(worktree, "status", "--porcelain") == ""
        approve(run, phase, by="me")
    assert advance(run, flow_repo.cfg, RunOptions()) == "done"
    assert not [e for e in flow_repo.events(run, "hitl_raised") if e["kind"] == "dirty_tree"]
    assert flow_repo.events(run, "pr_opened")
    assert git(worktree, "log", "-2", "--format=%s").splitlines() == ["Fix eval_gate gate failures", "Change nang-nguong"]


# --------------------------------------------------------------------------- review fix round 2: whose rules


def ignore_local_settings(flow_repo) -> None:
    """Ignore `.claude/settings.local.json`, as real repos do, so the guards' snapshots cannot see it."""
    gitignore = (flow_repo.root / ".gitignore").read_text(encoding="utf-8") + ".claude/settings.local.json\n"
    commit_file(flow_repo, ".gitignore", gitignore, "Ignore local settings")


def red_in_worktree(flow_repo, act: dict) -> Run:
    from agentflow.pipeline import advance

    flow_repo.script({RED: [act]})
    run = start(flow_repo.cfg, flow_repo.root, "bug", {"task": "đăng nhập lỗi"}, RunOptions(from_phase="build"))
    assert Path(run.state["worktree"]) != flow_repo.root
    assert advance(run, flow_repo.cfg, RunOptions()) == "waiting_human"
    return run


def test_rules_written_in_the_worktree_do_not_widen_the_test_command(flow_repo):
    ignore_local_settings(flow_repo)
    widen = json.dumps({"permissions": {"allow": ["Bash"]}})
    run = red_in_worktree(flow_repo, {"write": {".claude/settings.local.json": widen, "tests/test_login.py": "x\n"},
                                      **done("wrote it", test_command="mkdir marker")})
    worktree = Path(run.state["worktree"])
    assert (worktree / ".claude" / "settings.local.json").exists()  # written, and invisible to the guards
    stop = run.state["waiting"]
    assert stop["kind"] == "blocked" and "Bash(...) allow rule" in stop["summary"]
    assert not (worktree / "marker").exists() and not (flow_repo.root / "marker").exists()
    assert not flow_repo.events(run, "expectation_checked")


def test_rules_of_the_main_checkout_allow_the_test_command(flow_repo):
    ignore_local_settings(flow_repo)
    allow = json.dumps({"permissions": {"allow": ["Bash(mkdir *)"]}})
    (flow_repo.root / ".claude" / "settings.local.json").write_text(allow, encoding="utf-8")  # the human's, not in git
    run = red_in_worktree(flow_repo, {"write": {"tests/test_login.py": "x\n"}, **done("wrote it", test_command="mkdir marker")})
    worktree = Path(run.state["worktree"])
    assert not (worktree / ".claude" / "settings.local.json").exists()
    assert run.state["waiting"]["kind"] == "red_test_passes"  # it ran: `mkdir` exits 0
    assert (worktree / "marker").is_dir()


def test_tracked_settings_edit_in_the_main_checkout_is_a_guard_violation(flow_repo):
    widen = json.dumps({"permissions": {"allow": ["Bash"]}})
    run = bug_run(flow_repo)  # works in the main checkout itself, as a design run does
    flow_repo.script({RED: [{"write": {".claude/settings.json": widen}, **done("wrote it", test_command="mkdir marker")}]})
    assert run_step(flow_repo, run, "build", "regression_test") == ("stopped", True)
    stop = raised(flow_repo, run)
    assert stop["kind"] == "guard_violation" and ".claude/settings.json" in stop["summary"]
    assert not (flow_repo.root / "marker").exists() and not flow_repo.events(run, "expectation_checked")


EVIDENCE_TYPES = ("gate_result", "review_result", "step_findings", "expectation_checked")


def test_every_evidence_event_of_a_finished_run_has_a_tree(flow_repo, git, tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", f"{BIN}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_GH_LOG", str(tmp_path / "gh.jsonl"))
    gate = {"run": "grep -q good src/state.txt || { echo 'recall dropped'; exit 1; }", "when_exists": None}
    flow_repo.configure({"gates": {"floor": {"run": "true"}, "eval": gate}})
    flow_repo.script({
        "build/change": [{"write": {"toy/agent/policy.py": "THRESHOLD = 0.6\n"}, **done("raised it")}],
        EVAL: [{"write": {"src/state.txt": "good\n"}, **done("restored recall")}],
    })
    from agentflow.pipeline import advance

    run = start(flow_repo.cfg, flow_repo.root, "eval", {"task": "Nâng ngưỡng"}, RunOptions())
    for phase in ("build", "verify", "review"):
        assert advance(run, flow_repo.cfg, RunOptions()) == "awaiting_approval"
        approve(run, phase, by="me")
    assert advance(run, flow_repo.cfg, RunOptions()) == "done"

    evidence = [e for e in run.events.read() if e["type"] in EVIDENCE_TYPES]
    assert {e["type"] for e in evidence} >= {"gate_result"}
    assert all(e.get("tree") for e in evidence), [e for e in evidence if not e.get("tree")]
