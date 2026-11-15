"""The `ci` step: wait for GitHub CI, fix what fails, mark the pull request ready, and never merge."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentflow import ci_lane
from agentflow.gitops import GhError
from agentflow.hitl import approve
from agentflow.pipeline import RunOptions, advance, start
from agentflow.run_control import pause

BIN = Path(__file__).resolve().parent / "bin"
URL = "https://github.com/acme/repo/pull/42"
LINK = "https://github.com/acme/repo/actions/runs/111/job/222"
PIPE = {"gate_sets": {"build": ["ok"], "verify": ["ok"], "setup": []},
        "ci": {"poll_s": 0, "appear_min": 0, "timeout_min": 0}}
PATIENT = {**PIPE, "ci": {"poll_s": 0, "appear_min": 1, "timeout_min": 1}}
WATCH = {**PATIENT, "ci": {**PATIENT["ci"], "watch_max_h": 1}}
RED = "build/regression_test"
GOOD_TEST = {"result": {"status": "done", "summary": "red", "signals": {"test_command": "test -f fixed.txt"}}}
FIXED = {"write": {"fixed.txt": "ok\n"}, "result": {"status": "done", "summary": "fixed"}}
FIX = "ship/ci/fix-1"
PASS = {"checks": [{"name": "backend", "state": "SUCCESS", "bucket": "pass", "link": LINK},
                   {"name": "eval", "state": "SKIPPED", "bucket": "skipping", "link": LINK}]}
FAIL = {"checks": [{"name": "backend", "state": "FAILURE", "bucket": "fail", "link": LINK}]}  # `--json` exits 0 here
PENDING = {"checks": [{"name": "backend", "state": "IN_PROGRESS", "bucket": "pending", "link": LINK}], "exit": 8}
NONE = {"stderr": "no checks reported on the 'x' branch", "exit": 1}


class Gh:
    def __init__(self, path: Path, checks: Path) -> None:
        self.path = path
        self.checks = checks

    def play(self, *entries: dict) -> None:
        self.checks.write_text(json.dumps(list(entries)), encoding="utf-8")

    def calls(self) -> list[list[str]]:
        return [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines()]

    def did(self, *argv: str) -> bool:
        return any(call[: len(argv)] == list(argv) for call in self.calls())


@pytest.fixture
def gh(monkeypatch, tmp_path):
    log, checks = tmp_path / "gh.jsonl", tmp_path / "checks.json"
    monkeypatch.setenv("PATH", f"{BIN}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_GH_LOG", str(log))
    monkeypatch.setenv("FAKE_GH_URL", URL)
    monkeypatch.setenv("FAKE_GH_CHECKS", str(checks))
    log.touch()
    return Gh(log, checks)


def go(flow, run) -> str:
    return advance(run, flow.cfg, RunOptions())


def to_ship(flow, patch=PIPE, acts=None):
    """A bug run past `define`, whose next stop or end is the `ci` step."""
    flow.configure(patch, autopilot=True)
    flow.script({RED: [GOOD_TEST], "build/fix": [FIXED], **(acts or {})})
    run = start(flow.cfg, flow.root, "bug", {"task": "đăng nhập lỗi"}, RunOptions())
    assert go(flow, run) == "awaiting_approval"
    approve(run, "define", by="me")
    return run


def change(path: str = "src/ci.py") -> dict:
    return {"write": {path: "x = 1\n"}, "result": {"status": "done", "summary": "sửa lỗi CI"}}


def test_passing_checks_mark_the_pull_request_ready(flow_repo, gh):
    gh.play(PASS)
    run = to_ship(flow_repo)
    assert go(flow_repo, run) == "done"
    assert gh.did("pr", "ready", URL) and gh.did("pr", "edit", URL)
    assert flow_repo.events(run, "pr_marked_ready")[0]["url"] == URL
    assert run.state["ci"] is None


def test_nothing_is_ever_merged(flow_repo, gh):
    gh.play(FAIL, PASS)
    run = to_ship(flow_repo, acts={FIX: [change()]})
    assert go(flow_repo, run) == "done"
    assert not gh.did("pr", "merge") and not any("--auto" in call for call in gh.calls())


def test_only_a_settled_set_of_checks_decides(flow_repo, gh):
    gh.play(NONE, PENDING, PASS)
    run = to_ship(flow_repo, PATIENT)
    assert go(flow_repo, run) == "done"
    assert len([c for c in gh.calls() if c[:2] == ["pr", "checks"]]) == 3


def test_a_failure_still_pending_elsewhere_waits_for_the_rest(flow_repo, gh):
    mixed = {"checks": [*FAIL["checks"], {**PENDING["checks"][0], "name": "e2e"}]}
    gh.play(mixed, PASS)
    run = to_ship(flow_repo, PATIENT)
    assert go(flow_repo, run) == "done"
    assert not flow_repo.events(run, "ci_round")


def test_a_failure_is_fixed_committed_pushed_and_checked_again(flow_repo, gh, git):
    gh.play(FAIL, PASS)
    run = to_ship(flow_repo, acts={FIX: [change()]})
    assert go(flow_repo, run) == "done"
    worktree = Path(run.state["worktree"])
    assert git(worktree, "log", "-1", "--format=%s").strip() == "Fix CI failures"
    assert git(worktree, "rev-parse", "HEAD") == git(flow_repo.origin, "rev-parse", run.state["branch"])
    assert gh.did("run", "view", "--job", "222", "--log-failed")
    assert flow_repo.events(run, "ci_round")[0]["action"] == "fix"
    assert gh.did("pr", "ready", URL)


def test_ci_fix_round_names_the_failed_checks(flow_repo, gh):
    two = {"checks": [{"name": "unit", "state": "FAILURE", "bucket": "fail", "link": LINK},
                      {"name": "lint", "state": "FAILURE", "bucket": "fail", "link": LINK}]}
    gh.play(two, PASS)
    run = to_ship(flow_repo, acts={FIX: [change()]})
    assert go(flow_repo, run) == "done"
    ev = flow_repo.events(run, "ci_round")[0]
    assert ev["action"] == "fix" and ev["failed"] == ["lint", "unit"] and "### lint" in ev["tail"]


def test_ci_rerun_round_names_the_failed_checks(flow_repo, gh):
    gh.play(FAIL, PASS)
    run = to_ship(flow_repo)
    assert go(flow_repo, run) == "done"
    ev = flow_repo.events(run, "ci_round")[0]
    assert ev["action"] == "rerun" and ev["failed"] == ["backend"] and "tail" not in ev


def test_the_fix_session_is_told_the_failed_job_on_opus(flow_repo, gh):
    gh.play(FAIL, PASS)
    run = to_ship(flow_repo, acts={FIX: [change()]})
    assert go(flow_repo, run) == "done"
    call = next(c for c in flow_repo.calls() if c["step"] == FIX)
    assert "opus" in call["argv"]


def test_a_session_that_changes_nothing_runs_the_failed_jobs_again(flow_repo, gh):
    gh.play(FAIL, PASS)
    run = to_ship(flow_repo)
    assert go(flow_repo, run) == "done"
    assert gh.did("run", "rerun", "111", "--failed")
    assert flow_repo.events(run, "ci_round")[0]["action"] == "rerun"


def test_a_second_failure_with_no_change_stops_for_a_human(flow_repo, gh):
    gh.play(FAIL)
    run = to_ship(flow_repo)
    assert go(flow_repo, run) == "waiting_human"
    assert run.state["waiting"]["kind"] == "ci_failed"
    assert not gh.did("pr", "ready", URL)


def test_rounds_run_out_then_a_human_earns_fresh_ones(flow_repo, gh):
    gh.play(FAIL, FAIL, FAIL, FAIL, FAIL, PASS)
    run = to_ship(flow_repo, acts={f"ship/ci/fix-{n}": [change(f"src/ci{n}.py")] for n in (1, 2, 3, 4)})
    assert go(flow_repo, run) == "waiting_human"
    assert run.state["waiting"]["kind"] == "ci_failed"
    assert len(flow_repo.events(run, "ci_round")) == 3  # `ci.fix_rounds`
    assert not gh.did("pr", "ready", URL)
    approve(run, "ship/ci", by="me")
    assert go(flow_repo, run) == "done"
    assert "ship/ci/fix-4" in [c["step"] for c in flow_repo.calls()]  # a fresh round is a new session
    assert gh.did("pr", "ready", URL)


def test_no_checks_at_all_stops(flow_repo, gh):
    gh.play(NONE)
    run = to_ship(flow_repo)
    assert go(flow_repo, run) == "waiting_human"
    assert "no checks" in run.state["waiting"]["summary"]


def test_checks_that_never_settle_stop(flow_repo, gh):
    gh.play(PENDING)
    run = to_ship(flow_repo)
    assert go(flow_repo, run) == "waiting_human"
    assert "did not settle" in run.state["waiting"]["summary"]


def test_a_pause_ends_the_wait_and_a_resume_picks_it_up(flow_repo, gh, monkeypatch):
    gh.play(PENDING, PASS)
    run = to_ship(flow_repo, PATIENT)
    real = ci_lane.pr_checks
    state = {"n": 0}

    def once(worktree, url):
        state["n"] += 1
        if state["n"] == 1:
            pause(run, "me")
        return real(worktree, url)

    monkeypatch.setattr("agentflow.ci_lane.pr_checks", once)
    assert go(flow_repo, run) == "paused"
    assert not gh.did("pr", "ready", URL)
    assert advance(run, flow_repo.cfg, RunOptions()) == "done"
    assert gh.did("pr", "ready", URL)


def test_ci_cannot_be_fixed_by_editing_ci(flow_repo, gh):
    gh.play(FAIL)
    run = to_ship(flow_repo, acts={FIX: [change(".github/workflows/ci.yml")]})
    assert go(flow_repo, run) == "waiting_human"
    assert run.state["waiting"]["kind"] == "guard_violation"
    assert ".github/workflows/ci.yml" in run.state["waiting"]["summary"]
    assert not gh.did("pr", "ready", URL)


def test_a_failed_ready_call_stops_rather_than_finishing(flow_repo, gh, monkeypatch):
    gh.play(PASS)
    run = to_ship(flow_repo)

    def refuse(worktree, url):
        raise GhError("not allowed")

    monkeypatch.setattr("agentflow.ci_lane.mark_pr_ready", refuse)
    assert go(flow_repo, run) == "waiting_human"
    assert "could not be marked ready" in run.state["waiting"]["summary"]


def test_only_define_and_plan_ask_and_the_run_ends_with_a_ready_pr(flow_repo, gh):
    spec, plan = "docs/specs/SPEC-demo.md", "docs/plans/2026-10-29-demo.md"
    gh.play(FAIL, PASS)
    flow_repo.configure(PIPE, autopilot=True)
    flow_repo.script({
        "define/draft": [{"write": {spec: "# Spec: `demo`\n\n**Status: approved** 2026-10-01\n\n## Open Questions\n\nNone for this module.\n"},
                          "result": {"status": "done", "summary": "drafted"}}],
        "plan/plan": [{"write": {plan: "# Plan: demo\n\n### Task 1: One\n\n**Depends on:** none\n\n- [ ] **Step 1: Do it**\n"},
                       "result": {"status": "done", "summary": "planned"}}],
        FIX: [change()],
    })
    run = start(flow_repo.cfg, flow_repo.root, "feature", {"module": "demo"}, RunOptions())
    asked = []
    status = go(flow_repo, run)
    while status == "awaiting_approval":
        asked.append(run.state["waiting"]["phase"])
        approve(run, asked[-1], by="me")
        status = go(flow_repo, run)
    assert status == "done", run.state["waiting"]
    assert asked == ["define", "plan"]
    assert [e["kind"] for e in flow_repo.events(run, "hitl_raised")] == []
    assert gh.did("pr", "ready", URL) and not gh.did("pr", "merge")


def test_the_pull_request_body_is_the_audit_trail(flow_repo, gh):
    question = {"id": "q1", "question": "Lưu bao lâu?", "evidence": [], "kind": "other",
                "options": [{"key": "A", "label": "1 giờ", "recommended": True}]}
    minor = {"severity": "minor", "file": "src/one.py", "line": 9, "text": "đổi tên biến"}
    blocking = {"severity": "blocking", "file": "src/one.py", "line": 3, "text": "Thiếu kiểm tra quyền"}
    gh.play(FAIL, PASS)
    run = to_ship(flow_repo, acts={
        RED: [{"result": {"status": "blocked", "summary": "kẹt"}}, GOOD_TEST],
        f"{RED}/resolve-1": [{"result": {"status": "done", "summary": "đã tìm ra nguyên nhân"}}],
        "build/fix": [{"result": {"status": "needs_human", "summary": "hỏi", "questions": [question]}}, FIXED],
        "review/branch_review": [{"result": {"status": "done", "summary": "x", "findings": [blocking]}},
                                 {"result": {"status": "done", "summary": "y", "findings": [minor]}}],
        FIX: [change()],
    })
    assert go(flow_repo, run) == "done"
    body = (run.dir / "pr.md").read_text(encoding="utf-8")
    assert "Lưu bao lâu? → 1 giờ" in body
    assert "đã tìm ra nguyên nhân" in body
    assert "round 1 (the runner)" in body and "Thiếu kiểm tra quyền" in body
    assert "fixed and pushed" in body
    assert "src/one.py:9: đổi tên biến" in body
    edits = [c for c in gh.calls() if c[:2] == ["pr", "edit"]]
    assert edits and edits[-1][edits[-1].index("--body-file") + 1] == str(run.dir / "pr.md")  # the final body, not the draft's


def test_the_refreshed_body_keeps_the_issue_line(flow_repo, gh, monkeypatch):
    monkeypatch.setenv("FAKE_GH_ISSUE", json.dumps({"title": "Login breaks", "body": "b", "url": "u", "labels": []}))
    gh.play(PASS)
    flow_repo.configure(PIPE, autopilot=True)
    flow_repo.script({RED: [GOOD_TEST], "build/fix": [FIXED]})
    run = start(flow_repo.cfg, flow_repo.root, "bug", {"issue": "7"}, RunOptions())
    assert go(flow_repo, run) == "awaiting_approval"
    approve(run, "define", by="me")
    assert go(flow_repo, run) == "done"
    assert (run.dir / "pr.md").read_text(encoding="utf-8").split("\n\n")[1] == "Closes #7"
    assert gh.did("pr", "edit", URL) and gh.did("pr", "ready", URL)


def test_a_link_that_failed_at_finish_is_tried_again_each_ci_round_until_it_holds(flow_repo, gh, monkeypatch):
    """The PR must land in the issue's Development section: a failed link is retried while the run waits on CI."""
    from agentflow import issue_status

    monkeypatch.setenv("FAKE_GH_ISSUE", json.dumps({"title": "Login breaks", "body": "b", "url": "u", "labels": [],
                                                    "id": "I_fake7"}))
    monkeypatch.setenv("FAKE_GH_FAIL_ON", "api graphql")  # GitHub refuses the link at `finish` and on round 1
    tries: list[int] = []

    def link(run):
        tries.append(len(tries))
        if len(tries) == 2:
            monkeypatch.delenv("FAKE_GH_FAIL_ON")
        issue_status.link(run)

    monkeypatch.setattr(ci_lane, "link", link)
    gh.play(FAIL, PASS)
    flow_repo.configure(PIPE, autopilot=True)
    flow_repo.script({RED: [GOOD_TEST], "build/fix": [FIXED]})
    run = start(flow_repo.cfg, flow_repo.root, "bug", {"issue": "7"}, RunOptions())
    assert go(flow_repo, run) == "awaiting_approval"
    approve(run, "define", by="me")
    assert go(flow_repo, run) == "done"
    assert len(tries) == 2  # the start of the ci step, then the second round
    assert [e["action"] for e in flow_repo.events(run, "issue_status_failed")] == ["link", "link"]
    assert [(e["issue"], e["how"]) for e in flow_repo.events(run, "issue_linked")] == [("7", "api")]
    ready = next(i for i, c in enumerate(gh.calls()) if c[:2] == ["pr", "ready"])
    mutations = [i for i, c in enumerate(gh.calls()) if c[:2] == ["api", "graphql"]]
    assert mutations[-1] < ready


# --------------------------------------------------------------------------- CI still pending is watched, not a stop


@pytest.fixture
def clock(monkeypatch):
    """`ci_lane`'s clock, 60 s on at every reading (`monotonic` and `time` apart) and no sleeping; the global
    `time` module is untouched."""
    marks = {"monotonic": 0.0, "time": time.time()}

    def reader(name: str):
        def read() -> float:
            marks[name] += 60
            return marks[name]
        return read

    monkeypatch.setattr(ci_lane, "time", SimpleNamespace(monotonic=reader("monotonic"), time=reader("time"),
                                                         sleep=lambda seconds: None))
    return marks


@pytest.fixture
def notified(monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr("agentflow.hitl.notify", lambda cfg, title, message, run=None: seen.append(message))
    return seen


def watching(timeout_min: float = 1, watch_max_h: float = 1) -> dict:
    return {**WATCH, "ci": {**WATCH["ci"], "timeout_min": timeout_min, "watch_max_h": watch_max_h}}


def test_pending_checks_past_the_timeout_are_watched(flow_repo, gh, clock, notified):
    gh.play(PENDING, PENDING, PENDING, PASS)
    run = to_ship(flow_repo, WATCH)
    assert go(flow_repo, run) == "done"
    assert gh.did("pr", "ready", URL)
    held = flow_repo.events(run, "run_held")
    assert [e["reason"] for e in held] == ["ci_pending"] and held[0]["key"] == "ship/ci"
    assert len(flow_repo.events(run, "run_hold_ended")) == 1
    assert not flow_repo.events(run, "hitl_raised")
    assert len([m for m in notified if "needs nothing from you" in m]) == 1
    assert run.state["hold"] is None


def test_a_gh_error_or_no_checks_during_the_watch_keeps_watching(flow_repo, gh, clock):
    gh.play(PENDING, PENDING, {"stderr": "HTTP 502", "exit": 1}, NONE, PASS)
    run = to_ship(flow_repo, WATCH)
    assert go(flow_repo, run) == "done"
    assert not flow_repo.events(run, "hitl_raised")
    assert len(flow_repo.events(run, "run_held")) == 1 and len(flow_repo.events(run, "run_hold_ended")) == 1


def test_the_watch_gives_up_after_watch_max_h(flow_repo, gh, clock):
    gh.play(PENDING)
    run = to_ship(flow_repo, watching(timeout_min=1, watch_max_h=0.05))  # 3 minutes of watching
    assert go(flow_repo, run) == "waiting_human"
    assert run.state["waiting"]["kind"] == "ci_failed"
    assert "of watching" in run.state["waiting"]["summary"]
    assert len(flow_repo.events(run, "run_held")) == 1 and len(flow_repo.events(run, "run_hold_ended")) == 1
    types = [e["type"] for e in run.events.read()]
    assert types.index("run_hold_ended") < types.index("hitl_raised")
    assert run.state["hold"] is None


def test_no_checks_forever_during_the_watch_gives_up(flow_repo, gh, clock):
    gh.play(PENDING, NONE)
    run = to_ship(flow_repo, watching(timeout_min=1, watch_max_h=0.05))
    assert go(flow_repo, run) == "waiting_human"
    assert "of watching" in run.state["waiting"]["summary"]
    assert len(flow_repo.events(run, "run_hold_ended")) == 1


def test_an_exception_during_the_watch_ends_the_hold(flow_repo, gh, clock, monkeypatch):
    gh.play(PENDING)
    run = to_ship(flow_repo, WATCH)
    real = ci_lane.pr_checks
    calls = {"n": 0}

    def third_fails(worktree, url):
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("boom")
        return real(worktree, url)

    monkeypatch.setattr("agentflow.ci_lane.pr_checks", third_fails)
    with pytest.raises(RuntimeError, match="boom"):
        go(flow_repo, run)
    assert len(flow_repo.events(run, "run_hold_ended")) == 1
    run.refresh()
    assert run.state["hold"] is None


def test_a_pause_ends_the_ci_watch(flow_repo, gh, clock, monkeypatch):
    gh.play(PENDING, PENDING, PENDING, PASS)
    run = to_ship(flow_repo, WATCH)
    real = ci_lane.pr_checks
    state = {"n": 0}

    def third(worktree, url):
        state["n"] += 1
        if state["n"] == 3:
            pause(run, "me")
        return real(worktree, url)

    monkeypatch.setattr("agentflow.ci_lane.pr_checks", third)
    assert go(flow_repo, run) == "paused"
    assert len(flow_repo.events(run, "run_held")) == 1 and len(flow_repo.events(run, "run_hold_ended")) == 1
    assert not gh.did("pr", "ready", URL)
    assert advance(run, flow_repo.cfg, RunOptions()) == "done"
    assert gh.did("pr", "ready", URL)


def test_the_watch_off_stops_as_today(flow_repo, gh):
    gh.play(PENDING)
    run = to_ship(flow_repo)
    assert "watch_max_h" not in flow_repo.cfg.ci  # the fixture pack writes its own `ci` without it: the watch is off
    assert go(flow_repo, run) == "waiting_human"
    summary = run.state["waiting"]["summary"]
    assert "did not settle" in summary and "of watching" not in summary
    assert not flow_repo.events(run, "run_held")


def test_a_gh_error_at_the_timeout_with_the_watch_off_stops_as_today(flow_repo, gh):
    gh.play({"stderr": "HTTP 502", "exit": 1})
    run = to_ship(flow_repo)
    assert go(flow_repo, run) == "waiting_human"
    assert "HTTP 502" in run.state["waiting"]["summary"] and not flow_repo.events(run, "run_held")
