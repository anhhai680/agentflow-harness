"""The feature lane (Task 8): plan tasks with per-task review and commits, small plans, parking,
decision recording, resuming at the right task, and `finish` (push + draft pull request)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from agentflow import lanes
from agentflow.flow import main
from agentflow.gitops import gh_json
from agentflow.hitl import answer, approve, inbox_items
from agentflow.pipeline import RunOptions, advance, start

AGENTFLOW = Path(__file__).resolve().parents[1]
BIN = AGENTFLOW / "tests" / "bin"
PLAN_PATH = "docs/plans/2026-10-29-demo.md"
IMPL = "build/build/task-{n}/implement"
REVIEW = "build/build/task-{n}/review"
PR_FOOTER = "🤖 Generated with [Claude Code](https://claude.com/claude-code)"
TITLES = {1: "First thing", 2: "Second thing", 3: "Third thing", 4: "Fourth thing", 5: "Fifth thing"}
BIG = {"gate_sets": {"build": ["ok"], "verify": ["ok"]}, "conditions": {"plan_is_small": {"max": 0}}}
SMALL = {"gate_sets": {"build": ["ok"], "verify": ["ok"]}}
FINDING = {"severity": "blocking", "file": "src/one.py", "line": 3, "text": "Thiếu kiểm tra lỗi khi token hết hạn"}
DONE_RESULT = {"status": "done", "summary": "did it"}
MINOR = {"severity": "minor", "file": "src/one.py", "line": 9, "text": "rename x"}


def question(kind: str, qid: str = "q1", text: str = "Lưu bộ nhớ đệm token CRM?") -> dict:
    return {"id": qid, "question": text, "options": [{"key": "A", "label": "Theo tiến trình", "recommended": True}],
            "evidence": ["docs/specs/SPEC-demo.md#M5"], "kind": kind}


def needs_human(kind: str, can_continue: bool = False, qid: str = "q1") -> dict:
    return {"result": {"status": "needs_human", "summary": "a decision is needed",
                       "questions": [question(kind, qid)], "can_continue_without": can_continue}}


def review(*findings: dict) -> dict:
    return {"result": {"status": "done", "summary": "reviewed", "findings": list(findings)}}


def plan_text(depends: dict[int, str] | None = None, ticked: tuple[int, ...] = (), count: int = 2,
              files: dict[int, list[str]] | None = None) -> str:
    depends, files = depends or {}, files or {}
    parts = ["# Plan: demo\n\n## Global Constraints\n\n- [ ] not a task box\n"]
    for n in range(1, count + 1):
        box = "x" if n in ticked else " "
        dep = f"**Depends on:** {depends[n]}\n\n" if n in depends else ""
        named = "".join(f"- Modify: `{path}`\n" for path in files.get(n, []))
        named = f"{named}\n" if named else ""
        parts.append(f"### Task {n}: {TITLES[n]}\n\n{dep}{named}- [{box}] **Step 1: Task {n} test**\n"
                     f"- [{box}] **Step 2: Task {n} code**\n")
    return "\n".join(parts)


def commit_plan(flow_repo, text: str) -> None:
    from conftest import run_git

    target = flow_repo.root / PLAN_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    run_git(flow_repo.root, "add", "-A")
    run_git(flow_repo.root, "commit", "-q", "-m", "Plan demo")


@pytest.fixture
def notified(monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr("agentflow.hitl.notify", lambda cfg, title, message, run=None: seen.append(message))
    return seen


@pytest.fixture
def lane(flow_repo):
    flow_repo.configure(BIG)
    return flow_repo


def begin_build(flow_repo, text: str):
    spec = flow_repo.root / "docs" / "specs" / "SPEC-demo.md"
    spec.parent.mkdir(parents=True, exist_ok=True)
    spec.write_text("# Spec: `demo`\n\n## M5\n\nToken handling.\n", encoding="utf-8")
    commit_plan(flow_repo, text)
    return flow_repo.new_run({"module": "demo"})


def build(flow_repo, run):
    executor = flow_repo.executor(run)
    phase = flow_repo.cfg.phases["build"]
    step = next(s for s in phase.steps if s.id == "build")
    return executor.run_steps(phase, [step], "build")


def steps(calls: list[dict]) -> list[str]:
    return [c["step"] for c in calls]


def pairs(argv: list[str]) -> set[tuple[str, str]]:
    return set(zip(argv, argv[1:]))


def subjects(git, root: Path) -> list[str]:
    return git(root, "log", "--format=%s").splitlines()


def commit_of(git, root: Path, subject: str) -> str:
    for line in git(root, "log", "--format=%H %s").splitlines():
        sha, text = line.split(" ", 1)
        if text == subject:
            return sha
    raise AssertionError(f"no commit {subject!r}")


def session_of(flow_repo, run, key: str) -> str:
    return next(e["session_id"] for e in flow_repo.events(run, "agent_session") if e["key"] == key)


def sessions_of(flow_repo, run, key: str) -> list[str]:
    return [e["session_id"] for e in flow_repo.events(run, "agent_session") if e["key"] == key]


# --------------------------------------------------------------------------- tasks that need a forbidden file


FORBIDDEN_PLAN = (plan_text(count=2) + "\n- Create: `eval/baseline.json`\n").replace(
    "### Task 2: Second thing\n", "### Task 2: Second thing\n\n- Create: `eval/baseline.json`\n- Test: `tests/test_two.py`\n")


def test_plan_naming_a_forbidden_file_stops_before_any_task(flow_repo):
    flow_repo.configure({**BIG, "guardrails": {"forbidden_paths": ["eval/baseline.json"]}})
    run = begin_build(flow_repo, FORBIDDEN_PLAN)
    assert build(flow_repo, run) == ("stopped", True)
    assert flow_repo.calls() == []  # no session is spent on task 1 first
    waiting = run.state["waiting"]
    assert waiting["kind"] == "blocked" and waiting["key"] == "build/build"
    assert "Task 2: eval/baseline.json" in waiting["summary"] and "Task 1" not in waiting["summary"]


def test_approving_the_forbidden_file_stop_goes_on(flow_repo):
    flow_repo.configure({**BIG, "guardrails": {"forbidden_paths": ["eval/baseline.json"]}})
    run = begin_build(flow_repo, FORBIDDEN_PLAN)
    assert build(flow_repo, run) == ("stopped", True)
    approve(run, "build/build", by="me")  # the human made the change themselves
    assert build(flow_repo, run) == ("done", True)
    assert IMPL.format(n=2) in steps(flow_repo.calls())


# --------------------------------------------------------------------------- per-task sessions, review, commits


def test_every_task_step_carries_the_plan_total(lane):
    run = begin_build(lane, plan_text())
    assert build(lane, run) == ("done", True)
    started = [e for e in lane.events(run, "step_started") if "/task-" in e["key"]]
    assert started and {e["key"].split("/")[-2] for e in started} == {"task-1", "task-2"}
    for e in started:
        assert e["task"] == {"n": int(e["key"].split("/task-")[1][0]), "of": 2}
    assert run.state["task_total"] == 2


def test_tasks_fresh_sessions_and_models(lane, git):
    run = begin_build(lane, plan_text())
    start_sha = lane.head()
    assert build(lane, run) == ("done", True)
    calls = lane.calls()
    assert steps(calls) == [IMPL.format(n=1), REVIEW.format(n=1), IMPL.format(n=2), REVIEW.format(n=2)]
    impl = [c for c in calls if c["step"].endswith("/implement")]
    reviews = [c for c in calls if c["step"].endswith("/review")]
    for c in impl:
        assert ("--model", "sonnet") in pairs(c["argv"]) and "--resume" not in c["argv"]
    for c in reviews:
        assert ("--model", "sonnet") in pairs(c["argv"]) and "--resume" not in c["argv"]
    assert "Task 1: First thing" in impl[0]["argv"][-1] and "Task 1 test" in impl[0]["argv"][-1]
    assert "Task 2: Second thing" in impl[1]["argv"][-1]
    assert f"{start_sha}..worktree" in reviews[0]["argv"][-1]
    task1 = commit_of(git, lane.root, "Task 1: First thing")
    assert f"{task1}..worktree" in reviews[1]["argv"][-1]  # task 2 starts where task 1's commit left HEAD
    assert subjects(git, lane.root)[:2] == ["Task 2: Second thing", "Task 1: First thing"]
    assert [e["key"] for e in lane.events(run, "task_committed")] == ["build/build/task-1", "build/build/task-2"]
    assert len(lane.events(run, "review_result")) == 2
    assert build(lane, run) == ("done", True)  # done tasks never run or commit again
    assert len(lane.calls()) == 4 and len(lane.events(run, "task_committed")) == 2


def test_plan_boxes_ticked_in_task_commit(lane, git):
    run = begin_build(lane, plan_text())
    lane.script({IMPL.format(n=1): [{"write": {"src/one.py": "x = 1\n"}, "result": {"status": "done",
                                                                                     "summary": "added one"}}]})
    assert build(lane, run) == ("done", True)
    sha = commit_of(git, lane.root, "Task 1: First thing")
    diff = git(lane.root, "show", sha, "--format=", "--", PLAN_PATH)
    assert "-- [ ] **Step 1: Task 1 test**" in diff and "+- [x] **Step 1: Task 1 test**" in diff
    assert "+- [x] **Step 2: Task 1 code**" in diff
    changed = [line for line in diff.splitlines() if line[:1] in "+-" and line[:3] not in ("+++", "---")]
    assert len(changed) == 4  # task 1's two boxes, each one line out and one line in
    assert not [line for line in changed if "Task 2" in line or "not a task box" in line]
    files = git(lane.root, "show", sha, "--name-only", "--format=").split()
    assert sorted(files) == [PLAN_PATH, "src/one.py"]  # the work and the ticks in one commit
    body = git(lane.root, "log", "-1", "--format=%B", sha)
    assert body.startswith("Task 1: First thing\n\nadded one")
    assert body.rstrip().endswith(lane.cfg.project.commit_trailer)
    plan = (lane.root / PLAN_PATH).read_text(encoding="utf-8")
    assert "- [ ] not a task box" in plan and "- [x] **Step 1: Task 2 test**" in plan


def test_review_findings_back_once_then_hitl(lane, git):
    run = begin_build(lane, plan_text(count=1))
    impl_key, review_key = IMPL.format(n=1), REVIEW.format(n=1)
    lane.script({review_key: [review(FINDING, MINOR), review(FINDING), review(FINDING)]})
    assert build(lane, run) == ("stopped", True)
    calls = lane.calls()
    assert steps(calls) == [impl_key, review_key, impl_key, review_key]
    impl_session = session_of(lane, run, impl_key)
    fix = calls[2]["argv"]
    assert ("--resume", impl_session) in pairs(fix) and ("--effort", "medium") in pairs(fix)
    assert "receiving-code-review" in fix[-1] and FINDING["text"] in fix[-1] and "src/one.py:3" in fix[-1]
    assert MINOR["text"] not in fix[-1]  # only blocking findings go back
    assert "Stub: the receiving-code-review skill." in fix[-1]  # the skill's own text, since a resumed session will not load it
    assert "--resume" not in calls[3]["argv"]  # every round is a new session
    assert "--resume" not in calls[1]["argv"]
    assert FINDING["text"] in calls[3]["argv"][-1] and "src/one.py:3" in calls[3]["argv"][-1]
    assert MINOR["text"] not in calls[3]["argv"][-1]  # the re-review checks what blocked, not the whole last review
    assert len(set(sessions_of(lane, run, review_key))) == 2
    # gated again after the fix: the fix changed no file here, so the passes on that same tree are reused
    assert len([e for e in lane.events(run, "gate_result") if e["key"] == impl_key]) == 1
    assert [e["key"] for e in lane.events(run, "gates_reused")] == [impl_key]
    assert [e["blocking"] for e in lane.events(run, "review_result")] == [1, 1]
    waiting = run.state["waiting"]
    assert waiting["kind"] == "review_unresolved" and waiting["key"] == review_key
    assert FINDING["text"] in run.inbox_path(review_key).read_text(encoding="utf-8")
    assert "Task 1: First thing" not in subjects(git, lane.root)

    approve(run, review_key, by="me")  # a human looked: the reviewer gets one last look, then the task commits
    assert build(lane, run) == ("done", True)
    calls = lane.calls()
    assert steps(calls[4:]) == [review_key]
    assert "--resume" not in calls[4]["argv"] and FINDING["text"] in calls[4]["argv"][-1]
    assert len([c for c in calls if c["step"] == impl_key]) == 2  # still only one fix round
    sha = commit_of(git, lane.root, "Task 1: First thing")
    body = git(lane.root, "log", "-1", "--format=%B", sha)
    assert "Open review findings accepted by a human" in body and FINDING["text"] in body


def test_review_clean_after_fix_commits(lane, git):
    run = begin_build(lane, plan_text(count=1))
    lane.script({REVIEW.format(n=1): [review(FINDING), review(MINOR)]})
    assert build(lane, run) == ("done", True)
    assert steps(lane.calls()) == [IMPL.format(n=1), REVIEW.format(n=1), IMPL.format(n=1), REVIEW.format(n=1)]
    assert subjects(git, lane.root)[0] == "Task 1: First thing"
    assert run.state["waiting"] is None


TOY = "toy/x.py"
REVERT = "Revert these before the run continues: a later step commits the whole work tree."


def begin_toy_build(flow_repo):
    """`begin_build` with a tracked `toy/x.py` on the base, for a reviewer to try to change."""
    (flow_repo.root / "toy").mkdir(exist_ok=True)
    (flow_repo.root / TOY).write_text("x = 1\n", encoding="utf-8")
    return begin_build(flow_repo, plan_text(count=1))


def test_task_review_runs_read_only(lane, git):
    run = begin_toy_build(lane)
    review_key = REVIEW.format(n=1)
    lane.script({review_key: [{**review(), "attempts": [
        {"tool_name": "Edit", "tool_input": {"file_path": TOY, "old_string": "x = 1", "new_string": "x = 2"}},
        {"tool_name": "Bash", "tool_input": {"command": "pytest -q"}}]}]})
    assert build(lane, run) == ("done", True)
    denied = lane.events(run, "permission_denied")
    assert [(e["key"], e["tool"], e["input"], e.get("read_only")) for e in denied] == [
        (review_key, "Bash", {"command": "pytest -q"}, True)]  # `Edit` doesn't exist in the session: no denial
    assert (lane.root / TOY).read_text(encoding="utf-8") == "x = 1\n"
    calls = lane.calls()
    (impl,) = [c["argv"] for c in calls if c["step"] == IMPL.format(n=1)]
    (rev,) = [c["argv"] for c in calls if c["step"] == review_key]
    assert ("--permission-mode", "dontAsk") in pairs(rev) and "--disable-slash-commands" in rev
    assert ("--permission-mode", "dontAsk") not in pairs(impl) and "--disable-slash-commands" not in impl
    assert ("--permission-mode", "bypassPermissions") in pairs(impl)
    assert [e["read_only"] for e in lane.events(run, "agent_started")] == [False, True]
    assert run.state["steps"][review_key]["read_only"] is True
    commit_of(git, lane.root, "Task 1: First thing")  # the task is committed


def test_task_review_prompt_carries_the_note_and_gate_table(lane, git):
    run = begin_toy_build(lane)
    assert build(lane, run) == ("done", True)
    calls = {c["step"]: c["argv"][-1] for c in lane.calls()}
    review_prompt, impl_prompt = calls[REVIEW.format(n=1)], calls[IMPL.format(n=1)]
    progress = str(run.agent_dir / "progress.md")
    assert "## This session is read-only" in review_prompt
    assert f"Do not write `{progress}`" in review_prompt and "{progress}" not in review_prompt
    assert "| Gate | Result | Time | Step | Log |" in review_prompt
    assert "| ok | pass |" in review_prompt  # the task's gate, from the run's own results
    assert "{read_only_note}" not in review_prompt
    for text in ("## This session is read-only", "| Gate | Result |", "already ran these gates"):
        assert text not in impl_prompt


def test_a_forced_review_edit_fails_the_step(lane, git):
    from agentflow.run_control import retry

    run = begin_toy_build(lane)
    review_key = REVIEW.format(n=1)
    # `write` is the fake's own hand on the tree, past its permissions: a hook, a project allow rule, a shell built-in
    lane.script({review_key: [{**review(), "write": {TOY: "x = 2\n"}}, review()]})
    assert build(lane, run) == ("stopped", True)
    waiting = run.refresh()["waiting"]
    assert waiting["kind"] == "guard_violation" and waiting["key"] == review_key
    assert f'read_only_edit: "{TOY}"' in waiting["summary"] and REVERT in waiting["summary"]
    assert not lane.events(run, "step_committed") and not lane.events(run, "task_committed")
    assert "Task 1: First thing" not in subjects(git, lane.root)
    reviews = len([c for c in lane.calls() if c["step"] == review_key])

    retry(run, review_key, by="me")  # refused while `toy/x.py` still differs from the failed session's start
    assert build(lane, run) == ("stopped", True)
    assert len([c for c in lane.calls() if c["step"] == review_key]) == reviews
    waiting = run.refresh()["waiting"]
    assert waiting["kind"] == "guard_violation" and f'read_only_edit: "{TOY}"' in waiting["summary"]
    assert REVERT in waiting["summary"]

    git(lane.root, "checkout", "--", TOY)
    retry(run, review_key, by="me")
    assert build(lane, run) == ("done", True)
    assert len([c for c in lane.calls() if c["step"] == review_key]) == reviews + 1  # the review runs again
    commit_of(git, lane.root, "Task 1: First thing")
    assert (lane.root / TOY).read_text(encoding="utf-8") == "x = 1\n"


def test_review_stays_off_the_shared_progress_notes(lane, git):
    run = begin_build(lane, plan_text(count=1))
    assert build(lane, run) == ("done", True)
    prompt = lane.calls()[1]["argv"][-1]
    assert f"Do not open or append to `{run.agent_dir / 'progress.md'}`" in prompt


def test_human_note_reaches_the_fresh_last_look(lane, git):
    run = begin_build(lane, plan_text(count=1))
    review_key = REVIEW.format(n=1)
    lane.script({review_key: [review(FINDING), review(FINDING), review()]})
    assert build(lane, run) == ("stopped", True)
    approve(run, review_key, by="me", note="Dòng này cố ý giữ nguyên")
    assert build(lane, run) == ("done", True)
    last = lane.calls()[-1]
    assert last["step"] == review_key and "--resume" not in last["argv"]
    assert "Dòng này cố ý giữ nguyên" in last["argv"][-1] and FINDING["text"] in last["argv"][-1]


def test_stop_inside_a_re_review_resumes_that_round(lane, git):
    run = begin_build(lane, plan_text(count=1))
    review_key = REVIEW.format(n=1)
    blocked = {"result": {"status": "blocked", "summary": "a command was denied"}}
    lane.script({review_key: [review(FINDING), blocked, review()]})
    assert build(lane, run) == ("stopped", True)
    round_two = sessions_of(lane, run, review_key)[-1]
    assert run.state["waiting"]["key"] == review_key
    approve(run, review_key, by="me")
    assert build(lane, run) == ("done", True)
    last = lane.calls()[-1]
    assert last["step"] == review_key and ("--resume", round_two) in pairs(last["argv"])


def test_small_plan_shared_session(flow_repo, git):
    flow_repo.configure(SMALL)
    run = begin_build(flow_repo, plan_text())
    assert build(flow_repo, run) == ("done", True)
    calls = flow_repo.calls()
    assert steps(calls) == [IMPL.format(n=1), IMPL.format(n=2)]  # no review calls
    first = session_of(flow_repo, run, IMPL.format(n=1))
    assert "--resume" not in calls[0]["argv"]
    assert ("--resume", first) in pairs(calls[1]["argv"]) and ("--effort", "medium") in pairs(calls[1]["argv"])
    for c in calls:
        assert "Use the `executing-plans` skill" in c["argv"][-1]
    assert "Task 2: Second thing" in calls[1]["argv"][-1]
    assert subjects(git, flow_repo.root)[:2] == ["Task 2: Second thing", "Task 1: First thing"]
    assert flow_repo.events(run, "review_result") == []


STUB = "Stub: the executing-plans skill."


def _small_plan_without_brief(flow_repo, monkeypatch, shared: bool) -> list:
    """The `python` pack's small plan as the engine defaults write it (no `brief:`); returns the `(skill,
    continuation)` of every prompt built for it."""
    from agentflow import lanes

    flow_repo.configure(SMALL)
    step = next(s for s in flow_repo.cfg.phases["build"].steps if s.id == "build")
    step.small_plan = {**{k: v for k, v in step.small_plan.items() if k != "brief"}, "shared_session": shared}
    seen = []
    real = lanes.build_prompt
    monkeypatch.setattr(lanes, "build_prompt",
                        lambda cfg, s, *rest, **kw: seen.append((s.skill, s.continuation)) or real(cfg, s, *rest, **kw))
    return seen


@pytest.mark.parametrize("shared", [True, False])
def test_a_small_plan_task_that_resumes_the_session_does_not_resend_the_skill(flow_repo, monkeypatch, shared):
    seen = _small_plan_without_brief(flow_repo, monkeypatch, shared)
    run = begin_build(flow_repo, plan_text())
    assert build(flow_repo, run) == ("done", True)
    assert seen == [("executing-plans", False), ("executing-plans", shared)]
    first, second = (c["argv"][-1] for c in flow_repo.calls())
    assert STUB in first and "This is its full text" in first
    if shared:  # a resumed session already holds the text: no skill section is left empty in its prompt
        assert STUB not in second and "## The `executing-plans` skill" not in second
        assert "Use the `executing-plans` skill." in second
    else:
        assert STUB in second


@pytest.mark.parametrize("shared", [True, False])
def test_a_small_plan_task_that_rolls_over_gets_the_skill_in_its_fresh_session(flow_repo, monkeypatch, shared):
    seen = _small_plan_without_brief(flow_repo, monkeypatch, shared)
    flow_repo.script({IMPL.format(n=2): [{"usage_pct": [81]}, {"result": DONE_RESULT}]})
    run = begin_build(flow_repo, plan_text())
    assert build(flow_repo, run) == ("done", True)
    calls = flow_repo.calls()
    assert steps(calls) == [IMPL.format(n=1), IMPL.format(n=2), IMPL.format(n=2)]
    assert seen == [("executing-plans", False), ("executing-plans", shared), ("executing-plans", False)]
    fresh = calls[2]["argv"][-1]
    assert "--resume" not in calls[2]["argv"] and "a fresh session" in fresh
    assert STUB in fresh and "This is its full text" in fresh  # a new session holds nothing: the skill goes again


def test_foreach_with_every_task_ticked_reports_nothing_ran(lane, git):
    run = begin_build(lane, plan_text(ticked=(1, 2)))
    before = lane.head()
    assert build(lane, run) == ("done", False)
    assert lane.calls() == [] and lane.head() == before
    assert run.state["steps"]["build/build"]["status"] == "skipped"
    assert build(lane, run) == ("done", False)  # the step is recorded skipped


def test_a_deferred_task_starts_no_session(lane, git):
    deferred = "### Task 3: Third thing\n\n> **Deferred (SPEC-demo M9).** Needs `conversations`.\n\n**Depends on:** none\n"
    run = begin_build(lane, plan_text(ticked=(1, 2)) + "\n" + deferred)
    assert build(lane, run) == ("done", False)
    assert lane.calls() == []


# --------------------------------------------------------------------------- parking


def test_parking_runs_independent_task(lane, git):
    run = begin_build(lane, plan_text({1: "none", 2: "none", 3: "Task 1"}, count=3))
    impl1 = IMPL.format(n=1)
    lane.script({impl1: [{"write": {"src/one.py": "half\n"}, **needs_human("ambiguity", can_continue=True)}],
                 IMPL.format(n=2): [{"write": {"src/two.py": "two\n"}, "result": {"status": "done", "summary": "two"}}]})
    assert build(lane, run) == ("stopped", True)
    assert steps(lane.calls()) == [impl1, IMPL.format(n=2), REVIEW.format(n=2)]  # task 3 not started
    assert subjects(git, lane.root)[0] == "Task 2: Second thing"
    task2 = git(lane.root, "show", "HEAD", "--name-only", "--format=").split()
    assert sorted(task2) == [PLAN_PATH, "src/two.py"]  # the parked task's half-done file stays out
    assert not (lane.root / "src" / "one.py").exists()  # shelved: it comes back when task 1 runs again
    assert "Task 1: First thing" not in subjects(git, lane.root)
    (parked,) = lane.events(run, "step_parked")
    assert parked["key"] == impl1 and parked["questions"][0]["id"] == "q1"
    assert parked["paths"] == [] and parked["shelved"] == ["src/one.py"] and parked["shelf"]
    assert parked["seq"] < lane.events(run, "agent_started")[1]["seq"]  # parked before task 2 started
    assert run.state["status"] == "waiting_human"
    assert run.state["waiting"]["key"] == impl1 and run.state["waiting"]["kind"] == "needs_human"
    assert run.state["waiting"]["promoted"] is True  # no runnable task remains
    assert run.state["waiting"]["questions"][0]["question"] == "Lưu bộ nhớ đệm token CRM?"
    assert run.inbox_path(impl1).exists()
    raised = lane.events(run, "hitl_raised")  # the stop as it was raised, then promoted: no runnable task remains
    assert [(e["key"], e.get("promoted")) for e in raised] == [(impl1, None), (impl1, True)]
    assert build(lane, run) == ("stopped", True)  # still waiting: nothing runs, nothing is raised again
    assert len(lane.calls()) == 3 and len(lane.events(run, "hitl_raised")) == 2

    assert answer(run, impl1, {"q1": "A: giữ theo tiến trình"}, by="me")
    assert build(lane, run) == ("done", True)
    after = lane.calls()[3:]
    assert steps(after) == [impl1, REVIEW.format(n=1), IMPL.format(n=3), REVIEW.format(n=3)]
    assert ("--resume", session_of(lane, run, impl1)) in pairs(after[0]["argv"])
    assert "giữ theo tiến trình" in after[0]["argv"][-1]
    body = git(lane.root, "log", "-1", "--format=%B", commit_of(git, lane.root, "Task 1: First thing"))
    assert "q1 (ambiguity)" in body and "giữ theo tiến trình" in body  # ambiguity answers go in the body
    task1 = git(lane.root, "show", commit_of(git, lane.root, "Task 1: First thing"), "--name-only", "--format=").split()
    assert "src/one.py" in task1 and git(lane.root, "show", "HEAD:src/one.py") == "half\n"
    assert [e["conflict"] for e in lane.events(run, "unit_unparked")] == [False]
    assert [e["key"] for e in lane.events(run, "step_rebased")] == [impl1]  # task 2's commit moved HEAD
    assert len(lane.events(run, "hitl_raised")) == 2  # no guard violation on the resumed task
    assert subjects(git, lane.root)[0] == "Task 3: Third thing"


def test_parked_items_stay_answerable(lane, git):
    run = begin_build(lane, plan_text({1: "none", 2: "none", 3: "none"}, count=3))
    impl1, impl2 = IMPL.format(n=1), IMPL.format(n=2)
    lane.script({impl1: [needs_human("other", True)], impl2: [needs_human("other", True, qid="q2")]})
    assert build(lane, run) == ("stopped", True)
    assert subjects(git, lane.root)[0] == "Task 3: Third thing"
    assert run.state["waiting"]["key"] == impl1
    assert list(run.state["parked"]) == [impl2]
    assert {(i["key"], i["kind"]) for i in inbox_items(lane.root)} == {(impl1, "needs_human"), (impl2, "parked")}

    # a parked item is answerable while another one waits, here through the CLI; the promoted wait is parked again
    assert main(["answer", run.id, "--key", impl2, "--set", "q2=B", "--no-continue"]) == 0
    run.refresh()
    assert run.state["waiting"] is None and list(run.state["parked"]) == [impl1]
    assert run.state["steps"][impl2]["status"] == "running"
    assert answer(run, impl1, {"q1": "A"}, by="me")
    assert run.state["waiting"] is None and run.state["status"] == "running"
    assert build(lane, run) == ("done", True)
    assert {"Task 1: First thing", "Task 2: Second thing"} <= set(subjects(git, lane.root)[:2])


@pytest.mark.parametrize("act", [needs_human("other", True), {"result": {"status": "blocked", "summary": "stuck"}}])
def test_parking_off_stops_at_once(lane, git, act, notified):
    lane.configure({**BIG, "hitl": {"park_independent": False}})
    run = begin_build(lane, plan_text({1: "none", 2: "none"}))
    lane.script({IMPL.format(n=1): [act]})
    assert build(lane, run) == ("stopped", True)
    assert steps(lane.calls()) == [IMPL.format(n=1)]
    assert lane.events(run, "step_parked") == []
    assert run.state["waiting"]["key"] == IMPL.format(n=1) and run.state["waiting"]["notified"] is True
    assert len(notified) == 1  # told once, as the stop was raised: today's rule


# --------------------------------------------------------------------------- every task stop parks (T4)


FIVE = plan_text({n: "none" for n in range(1, 6)}, count=5,
                 files={1: ["src/one.py"], 2: ["src/two.py"], 3: ["src/three.py"], 4: ["src/four.py"],
                        5: ["src/five.py"]})
BLOCKED = {"result": {"status": "blocked", "summary": "stuck"}}
TWO = {"write": {"src/two.py": "two\n"}, "result": DONE_RESULT}
SHELVES = "refs/agentflow/shelf"


def shelves(git, root: Path) -> list[str]:
    return git(root, "for-each-ref", "--format=%(refname)", SHELVES).split()


def files_of(git, root: Path, subject: str) -> list[str]:
    return git(root, "show", commit_of(git, root, subject), "--name-only", "--format=").split()


def test_a_blocked_task_parks_and_the_independent_ones_finish(lane, git):
    lane.configure({**BIG, "phases": {"build": {"resolve": True}}, "autonomy": {"resolve_kinds": {"blocked": 1}}})
    run = begin_build(lane, FIVE)
    impl2 = IMPL.format(n=2)
    lane.script({impl2: [{"write": {"src/two.py": "half\n"}, **BLOCKED},
                         {"write": {"src/two.py": "half again\n"}, "result": {"status": "blocked", "summary": "still"}}]})
    assert build(lane, run) == ("stopped", True)
    assert {"Task 1: First thing", "Task 3: Third thing", "Task 4: Fourth thing",
            "Task 5: Fifth thing"} <= set(subjects(git, lane.root))
    assert "Task 2: Second thing" not in subjects(git, lane.root)
    assert not (lane.root / "src/two.py").exists()  # shelved, not in the tree
    assert git(lane.root, "status", "--porcelain") == ""
    (parked,) = lane.events(run, "step_parked")
    assert parked["key"] == impl2 and parked["kind"] == "blocked"
    assert parked["blast_radius"] == {"blocks": [2], "continue": [3, 4, 5]} and parked["shelf"]
    assert parked["paths"] == [] and parked["shelved"] == ["src/two.py"]
    assert shelves(git, lane.root) == [f"{SHELVES}/{run.id}/build.build.task-2"]
    assert lane.events(run, "resolve_started")[0]["seq"] < parked["seq"]  # the resolver went first
    assert run.state["waiting"]["key"] == impl2 and run.state["waiting"]["promoted"] is True
    assert "Blast radius: blocks task 2" in run.inbox_path(impl2).read_text(encoding="utf-8")
    n_before = len(lane.calls())
    assert build(lane, run) == ("stopped", True)  # still the person's: nothing runs, nothing is raised again
    assert len(lane.calls()) == n_before and len(lane.events(run, "hitl_raised")) == 3

    approve(run, impl2, by="me")
    assert build(lane, run) == ("done", True)
    assert steps(lane.calls()[n_before:]) == [impl2, REVIEW.format(n=2)]  # task 2 only
    (unparked,) = lane.events(run, "unit_unparked")
    assert unparked["conflict"] is False and unparked["paths"] == ["src/two.py"] and unparked["shelf"] == parked["shelf"]
    assert shelves(git, lane.root) == []
    assert "src/two.py" in files_of(git, lane.root, "Task 2: Second thing")
    assert git(lane.root, "show", "HEAD:src/two.py") == "half again\n"


def test_a_task_naming_the_parked_tasks_file_waits(lane, git):
    run = begin_build(lane, plan_text({1: "none", 2: "none", 3: "none"}, count=3,
                                      files={1: ["src/shared.py"], 2: ["src/two.py"], 3: ["src/shared.py"]}))
    impl1 = IMPL.format(n=1)
    lane.script({impl1: [{"write": {"src/shared.py": "half\n"}, **BLOCKED}]})
    assert build(lane, run) == ("stopped", True)
    assert steps(lane.calls()) == [impl1, IMPL.format(n=2), REVIEW.format(n=2)]  # task 3 shares task 1's file
    (parked,) = lane.events(run, "step_parked")
    assert parked["blast_radius"] == {"blocks": [1, 3], "continue": [2]}
    approve(run, impl1, by="me")
    assert build(lane, run) == ("done", True)
    assert steps(lane.calls())[3:] == [impl1, REVIEW.format(n=1), IMPL.format(n=3), REVIEW.format(n=3)]
    assert subjects(git, lane.root)[:2] == ["Task 3: Third thing", "Task 1: First thing"]


def test_a_gate_failure_before_the_commit_parks(lane, git):
    from conftest import TEST_GATES

    lane.configure({**BIG, "gates": {**TEST_GATES, "nobroken": {"run": "test ! -f broken.txt"}},
                    "gate_sets": {"build": ["nobroken"], "verify": ["ok"]}})
    run = begin_build(lane, plan_text({1: "none", 2: "none"}, files={1: ["src/one.py"], 2: ["src/two.py"]}))
    impl1, commit1 = IMPL.format(n=1), "build/build/task-1/commit"
    lane.script({impl1: [{"write": {"src/one.py": "one\n"}, "result": DONE_RESULT}], IMPL.format(n=2): [TWO]})
    edited: list[str] = []

    def human(key):  # a person's edit that breaks the gate, made after task 1's implementer passed its gates
        if key == REVIEW.format(n=1) and not edited:
            edited.append(key)
            (lane.root / "broken.txt").write_text("broken\n", encoding="utf-8")

    assert build_with(wrap_agent(lane, run, human), lane) == ("stopped", True)
    (parked,) = lane.events(run, "step_parked")
    assert parked["key"] == commit1 and parked["kind"] == "gate_failed"
    assert parked["shelved"] == ["broken.txt", "src/one.py"]
    assert subjects(git, lane.root)[0] == "Task 2: Second thing"  # task 2's gates passed: the breakage is shelved
    assert files_of(git, lane.root, "Task 2: Second thing") == [PLAN_PATH, "src/two.py"]
    assert run.state["waiting"]["key"] == commit1 and run.state["waiting"]["promoted"] is True

    approve(run, commit1, by="me")
    assert build(lane, run) == ("stopped", True)  # the shelf is back, and so is the failure: nothing else can run
    assert len(lane.events(run, "step_parked")) == 1
    assert run.state["waiting"]["key"] == commit1 and run.state["waiting"]["kind"] == "gate_failed"
    assert (lane.root / "broken.txt").exists() and (lane.root / "src/one.py").exists()
    (lane.root / "broken.txt").unlink()
    approve(run, commit1, by="me")
    assert build(lane, run) == ("done", True)
    assert files_of(git, lane.root, "Task 1: First thing") == [PLAN_PATH, "src/one.py"]


def test_no_park_when_every_other_task_waits_on_it(lane, git, notified):
    run = begin_build(lane, plan_text(count=2))  # task 2 depends on task 1 (no `Depends on` line)
    lane.script({IMPL.format(n=1): [{"write": {"src/one.py": "half\n"}, **BLOCKED}]})
    assert build(lane, run) == ("stopped", True)
    assert steps(lane.calls()) == [IMPL.format(n=1)]
    assert lane.events(run, "step_parked") == [] and shelves(git, lane.root) == []
    assert (lane.root / "src/one.py").read_text(encoding="utf-8") == "half\n"  # a stop leaves the tree as it is
    waiting = run.state["waiting"]
    assert waiting["key"] == IMPL.format(n=1) and waiting["kind"] == "blocked" and not waiting.get("promoted")
    assert waiting["notified"] is True and [e["key"] for e in lane.events(run, "hitl_notified")] == [IMPL.format(n=1)]
    assert build(lane, run) == ("stopped", True)  # each walk is a fresh executor: the person was told already
    assert build(lane, run) == ("stopped", True)
    assert notified == ["stuck"]  # one stop, one notification
    assert len(lane.calls()) == 1


def test_settling_a_parked_stop_while_a_later_task_stops_runs_its_task(lane, git, notified):
    run = begin_build(lane, plan_text({1: "none", 2: "none", 3: "none"}, count=3,
                                      files={1: ["src/one.py"], 2: ["src/two.py"], 3: ["src/three.py"]}))
    impl1, impl3 = IMPL.format(n=1), IMPL.format(n=3)
    lane.script({impl1: [{"write": {"src/one.py": "one\n"}, **BLOCKED}], IMPL.format(n=2): [TWO],
                 impl3: [{"write": {"src/three.py": "three\n"}, "result": {"status": "blocked", "summary": "stuck 3"}}]})
    assert build(lane, run) == ("stopped", True)
    # task 3 parks too, though nothing else can run, because task 1 is parked: the first parked stop is promoted
    assert [e["key"] for e in lane.events(run, "step_parked")] == [impl1, impl3]
    assert run.state["waiting"]["key"] == impl1 and run.state["waiting"]["promoted"] is True
    assert list(run.state["parked"]) == [impl3]
    assert len(notified) == 3  # two parks and the promotion
    notified.clear()

    approve(run, impl1, by="me")
    calls = len(lane.calls())
    assert build(lane, run) == ("stopped", True)
    assert steps(lane.calls()[calls:]) == [impl1, REVIEW.format(n=1)]  # the run goes on with task 1
    assert subjects(git, lane.root)[0] == "Task 1: First thing"
    assert run.state["waiting"]["key"] == impl3 and run.state["waiting"]["promoted"] is True
    assert notified == ["No runnable task remains: 1 parked stop(s) wait."]  # one notification for that walk


def test_a_guard_stop_with_a_foreign_head_never_parks(lane, git):
    run = begin_build(lane, plan_text({1: "none", 2: "none"}, files={1: ["src/one.py"], 2: ["src/two.py"]}))
    impl1 = IMPL.format(n=1)
    lane.script({impl1: [{"write": {"src/one.py": "x\n"}, "git_commit": "sneaky", **DONE_RESULT}]})
    assert build(lane, run) == ("stopped", True)  # the agent moved HEAD: every later commit would be refused
    assert steps(lane.calls()) == [impl1]
    assert run.state["waiting"]["kind"] == "guard_violation" and "agent_commit" in run.state["waiting"]["summary"]
    assert lane.events(run, "step_parked") == []


FORBIDDEN_ACT = {"write": {"src/one.py": "x\n", "CONSTRAINTS.md": "weakened\n"}, **DONE_RESULT}


def test_a_forbidden_path_stop_with_head_not_the_runners_never_parks(lane, git, monkeypatch):
    run = begin_build(lane, plan_text({1: "none", 2: "none"}, files={1: ["src/one.py"], 2: ["src/two.py"]}))
    lane.script({IMPL.format(n=1): [FORBIDDEN_ACT]})
    monkeypatch.setattr("agentflow.feature_lane.runner_head", lambda run: "0" * 40)
    assert build(lane, run) == ("stopped", True)
    assert lane.events(run, "step_parked") == [] and steps(lane.calls()) == [IMPL.format(n=1)]
    assert run.state["waiting"]["kind"] == "guard_violation"


def test_a_forbidden_path_stop_parks(lane, git):
    run = begin_build(lane, plan_text({1: "none", 2: "none"}, files={1: ["src/one.py"], 2: ["src/two.py"]}))
    impl1 = IMPL.format(n=1)
    lane.script({impl1: [FORBIDDEN_ACT], IMPL.format(n=2): [TWO]})
    assert build(lane, run) == ("stopped", True)
    (parked,) = lane.events(run, "step_parked")
    assert parked["kind"] == "guard_violation" and parked["shelved"] == ["src/one.py"]  # the forbidden edit is set aside
    assert [e["paths"] for e in lane.events(run, "forbidden_reverted")] == [["CONSTRAINTS.md"]]
    ref = f"{SHELVES}/{run.id}/build.build.task-1"
    assert git(lane.root, "show", f"{ref}:CONSTRAINTS.md") == "weakened\n"  # recoverable from the shelf
    inbox = run.inbox_path(impl1).read_text(encoding="utf-8")
    assert "was set aside" in inbox and ref in inbox
    assert files_of(git, lane.root, "Task 2: Second thing") == [PLAN_PATH, "src/two.py"]
    assert git(lane.root, "status", "--porcelain") == ""

    approve(run, impl1, by="me")
    assert build(lane, run) == ("done", True)  # the task restarts cleanly: the guard does not trip again
    assert len(lane.events(run, "guard_tripped")) == 1
    assert files_of(git, lane.root, "Task 1: First thing") == [PLAN_PATH, "src/one.py"]
    assert "weakened" not in (lane.root / "CONSTRAINTS.md").read_text(encoding="utf-8")
    assert shelves(git, lane.root) == [ref]  # kept: it holds the edit that was set aside


def test_a_shelf_ref_git_refuses_never_parks_and_tells_a_person(lane, git, monkeypatch, notified):
    run = begin_build(lane, plan_text({1: "none", 2: "none"}, files={1: ["src/one.py"], 2: ["src/two.py"]}))
    impl1 = IMPL.format(n=1)
    lane.script({impl1: [{"write": {"src/one.py": "half\n"}, **BLOCKED}]})

    def refused(run_id, tkey):
        raise ValueError(f"not a valid shelf ref: {tkey!r}")

    monkeypatch.setattr("agentflow.feature_lane.shelf_ref", refused)
    assert build(lane, run) == ("stopped", True)  # no crash: no shelf, no park
    assert lane.events(run, "step_parked") == [] and steps(lane.calls()) == [impl1]
    assert run.state["waiting"]["key"] == impl1 and run.state["waiting"]["kind"] == "blocked"
    assert (lane.root / "src/one.py").read_text(encoding="utf-8") == "half\n"  # the work stays in the tree
    assert notified == ["stuck"]


def test_a_runner_killed_mid_guard_park_sets_the_forbidden_edit_aside_on_resume(lane, git, monkeypatch):
    run = begin_build(lane, plan_text({1: "none", 2: "none"}, files={1: ["src/one.py"], 2: ["src/two.py"]}))
    lane.script({IMPL.format(n=1): [FORBIDDEN_ACT], IMPL.format(n=2): [TWO]})
    from agentflow import feature_lane

    real = feature_lane.restore_paths
    kills: list[list[str]] = []

    def killed(worktree, paths, sha):  # dies after `step_parked`, before any path is reset
        if not kills:
            kills.append(list(paths))
            raise KeyboardInterrupt
        real(worktree, paths, sha)

    monkeypatch.setattr(feature_lane, "restore_paths", killed)
    with pytest.raises(KeyboardInterrupt):
        build(lane, run)
    assert len(lane.events(run, "step_parked")) == 1 and lane.events(run, "forbidden_reverted") == []
    assert "weakened" in (lane.root / "CONSTRAINTS.md").read_text(encoding="utf-8")

    assert build(lane, run) == ("stopped", True)
    assert [e["paths"] for e in lane.events(run, "forbidden_reverted")] == [["CONSTRAINTS.md"]]
    assert "weakened" not in (lane.root / "CONSTRAINTS.md").read_text(encoding="utf-8")
    assert files_of(git, lane.root, "Task 2: Second thing") == [PLAN_PATH, "src/two.py"]
    assert git(lane.root, "status", "--porcelain") == ""


def test_an_unshelve_conflict_stops_and_overwrites_nothing(lane, git):
    run = begin_build(lane, plan_text({1: "none", 2: "none"}, files={1: ["src/one.py"], 2: ["src/two.py"]}))
    impl1, unshelve = IMPL.format(n=1), "build/build/task-1/unshelve"
    lane.script({impl1: [{"write": {"src/one.py": "half\n"}, **BLOCKED}],
                 IMPL.format(n=2): [{"write": {"src/two.py": "two\n", "src/one.py": "theirs\n"},  # names only two.py
                                     "result": DONE_RESULT}]})
    assert build(lane, run) == ("stopped", True)
    assert git(lane.root, "show", "HEAD:src/one.py") == "theirs\n"
    approve(run, impl1, by="me")
    calls = len(lane.calls())
    assert build(lane, run) == ("stopped", True)
    assert len(lane.calls()) == calls  # no session on a tree the shelf did not apply to
    waiting = run.state["waiting"]
    ref = f"{SHELVES}/{run.id}/build.build.task-1"
    assert waiting["key"] == unshelve and waiting["kind"] == "blocked"
    assert ref in waiting["summary"] and "src/one.py" in waiting["summary"]
    assert (lane.root / "src/one.py").read_text(encoding="utf-8") == "theirs\n"  # nothing overwritten
    assert git(lane.root, "status", "--porcelain") == "" and shelves(git, lane.root) == [ref]
    assert lane.events(run, "unit_unparked") == []

    (lane.root / "src/one.py").write_text("merged\n", encoding="utf-8")  # the person applies the shelf by hand
    approve(run, unshelve, by="me")
    assert build(lane, run) == ("done", True)
    (unparked,) = lane.events(run, "unit_unparked")
    assert unparked["conflict"] is True and unparked["by"] == "me" and unparked["paths"] == ["src/one.py"]
    assert steps(lane.calls()[calls:]) == [impl1, REVIEW.format(n=1)]
    assert git(lane.root, "show", "HEAD:src/one.py") == "merged\n"  # the tree as the person left it
    assert subjects(git, lane.root)[0] == "Task 1: First thing"
    assert run.state["steps"][unshelve]["status"] == "done"


def test_a_runner_killed_mid_park_finishes_the_shelving(lane, git, monkeypatch):
    run = begin_build(lane, plan_text({1: "none", 2: "none"}, files={1: ["src/one.py"], 2: ["src/two.py"]}))
    impl1 = IMPL.format(n=1)
    lane.script({impl1: [{"write": {"src/one.py": "half\n"}, **BLOCKED}], IMPL.format(n=2): [TWO]})
    from agentflow import feature_lane

    real = feature_lane.restore_paths
    kills: list[list[str]] = []

    def killed(worktree, paths, sha):  # the runner dies after `step_parked`, before the paths are reset
        if not kills:
            kills.append(list(paths))
            raise KeyboardInterrupt
        real(worktree, paths, sha)

    monkeypatch.setattr(feature_lane, "restore_paths", killed)
    with pytest.raises(KeyboardInterrupt):
        build(lane, run)
    assert kills == [["src/one.py"]] and len(lane.events(run, "step_parked")) == 1
    assert (lane.root / "src/one.py").exists()  # the parked work is still in the tree

    assert build(lane, run) == ("stopped", True)
    assert steps(lane.calls()) == [impl1, IMPL.format(n=2), REVIEW.format(n=2)]
    assert files_of(git, lane.root, "Task 2: Second thing") == [PLAN_PATH, "src/two.py"]  # task 1's work stays out
    assert not (lane.root / "src/one.py").exists()
    approve(run, impl1, by="me")
    assert build(lane, run) == ("done", True)
    assert git(lane.root, "show", "HEAD:src/one.py") == "half\n"


def test_approving_a_parked_stop_while_another_waits_runs_its_task(lane, git):
    run = begin_build(lane, plan_text({1: "none", 2: "none", 3: "none"}, count=3,
                                      files={1: ["src/one.py"], 2: ["src/two.py"], 3: ["src/three.py"]}))
    impl1, impl2 = IMPL.format(n=1), IMPL.format(n=2)
    lane.script({impl1: [{"write": {"src/one.py": "one\n"}, **BLOCKED}],
                 impl2: [{"write": {"src/two.py": "two\n"}, **BLOCKED}]})
    assert build(lane, run) == ("stopped", True)
    assert subjects(git, lane.root)[0] == "Task 3: Third thing"
    assert run.state["waiting"]["key"] == impl1 and run.state["waiting"]["promoted"] is True
    assert list(run.state["parked"]) == [impl2]

    approve(run, impl2, by="me")  # B, parked, while the run waits on the promoted A
    assert run.state["waiting"] is None and set(run.state["parked"]) == {impl1}  # A is parked again, not lost
    calls = len(lane.calls())
    assert build(lane, run) == ("stopped", True)
    assert steps(lane.calls()[calls:]) == [impl2, REVIEW.format(n=2)]  # B's task runs, A's does not
    assert subjects(git, lane.root)[0] == "Task 2: Second thing"
    assert git(lane.root, "show", "HEAD:src/two.py") == "two\n"
    assert run.state["waiting"]["key"] == impl1 and run.state["waiting"]["promoted"] is True
    assert not (lane.root / "src/one.py").exists()  # A's work is still on its shelf

    approve(run, impl1, by="me")
    assert build(lane, run) == ("done", True)
    assert subjects(git, lane.root)[0] == "Task 1: First thing"
    assert git(lane.root, "show", "HEAD:src/one.py") == "one\n"


def test_a_question_parks_without_can_continue_without(lane, git):
    run = begin_build(lane, plan_text({1: "none", 2: "none"}))
    impl1 = IMPL.format(n=1)
    lane.script({impl1: [needs_human("other", can_continue=False)]})
    assert build(lane, run) == ("stopped", True)
    assert steps(lane.calls()) == [impl1, IMPL.format(n=2), REVIEW.format(n=2)]
    (parked,) = lane.events(run, "step_parked")
    assert parked["key"] == impl1 and parked["questions"][0]["id"] == "q1" and parked["shelf"] is None
    assert parked["blast_radius"] == {"blocks": [1], "continue": [2]}
    assert answer(run, impl1, {"q1": "A"}, by="me")
    assert build(lane, run) == ("done", True)
    assert subjects(git, lane.root)[0] == "Task 1: First thing"


# --------------------------------------------------------------------------- decisions and resuming


def test_spec_gap_answer_records_decision_first(lane, git):
    run = begin_build(lane, plan_text(count=1))
    impl = IMPL.format(n=1)
    record = f"{impl}/record-q1"
    lane.script({
        impl: [{"write": {"src/partial.py": "y = 2\n"}, **needs_human("spec_gap")}],
        record: [{"write": {"docs/specs/SPEC-demo.md": "## M5\n\nCache per process.\n"},
                  "result": {"status": "done", "summary": "recorded M5"}}],
    })
    assert build(lane, run) == ("stopped", True)
    assert answer(run, impl, {"q1": "A"}, by="me")
    assert build(lane, run) == ("done", True)
    calls = lane.calls()
    assert steps(calls)[:3] == [impl, record, impl]
    rec_call = calls[1]
    assert ("--model", "sonnet") in pairs(rec_call["argv"]) and "domain-modeling" in rec_call["argv"][-1]
    assert "Lưu bộ nhớ đệm token CRM?" in rec_call["argv"][-1] and "Answer: A" in rec_call["argv"][-1]
    assert ("--resume", session_of(lane, run, impl)) in pairs(calls[2]["argv"])
    decide = "Decide Lưu bộ nhớ đệm token CRM in SPEC-demo"
    assert subjects(git, lane.root)[:2] == ["Task 1: First thing", decide]
    sha = commit_of(git, lane.root, decide)
    assert git(lane.root, "show", sha, "--name-only", "--format=").split() == ["docs/specs/SPEC-demo.md"]
    committed = next(e for e in lane.events(run, "step_committed") if e["key"] == record)
    resumed = [e for e in lane.events(run, "agent_started") if e["step"] == impl][1]
    assert committed["seq"] < resumed["seq"]
    task = commit_of(git, lane.root, "Task 1: First thing")
    assert "src/partial.py" in git(lane.root, "show", task, "--name-only", "--format=").split()


def test_record_flag_records_any_kind(lane, git):
    run = begin_build(lane, plan_text(count=1))
    impl = IMPL.format(n=1)
    lane.script({impl: [needs_human("other")],
                 f"{impl}/record-q1": [{"write": {"docs/specs/SPEC-demo.md": "x\n"}, "result": {"status": "done",
                                                                                              "summary": "ok"}}]})
    build(lane, run)
    assert answer(run, impl, {"q1": "A"}, by="me", record=True)
    assert build(lane, run) == ("done", True)
    assert "Decide Lưu bộ nhớ đệm token CRM in SPEC-demo" in subjects(git, lane.root)


def test_spec_gap_without_a_spec_records_nothing(lane, git):
    commit_plan(lane, plan_text(count=1))
    run = lane.new_run({"module": "demo"})
    impl = IMPL.format(n=1)
    lane.script({impl: [needs_human("spec_gap")]})
    build(lane, run)
    assert answer(run, impl, {"q1": "A"}, by="me")
    assert build(lane, run) == ("done", True)
    assert not [s for s in steps(lane.calls()) if "/record-" in s]
    assert not [s for s in subjects(git, lane.root) if s.startswith("Decide ")]


def test_grill_answers_all_reach_apply_with_no_record_sessions(lane, git):
    spec = lane.root / "docs" / "specs" / "SPEC-demo.md"
    spec.parent.mkdir(parents=True, exist_ok=True)
    spec.write_text("# Spec: `demo`\n\n## Open Questions\n\n- Q1 — Lưu token ở đâu?\n", encoding="utf-8")
    git(lane.root, "add", "-A")
    git(lane.root, "commit", "-q", "-m", "Draft SPEC-demo")
    run = lane.new_run({"module": "demo"})
    group = "define/grill_open_questions/r1"
    grill, apply = f"{group}/grill", f"{group}/apply"
    lane.script({
        grill: [needs_human("spec_gap")],  # a grill may say `needs_human`, but its questions are its output
        apply: [{"write": {"docs/specs/SPEC-demo.md": "# Spec: `demo`\n\n## Open Questions\n\nNone for this module.\n"},
                 "result": {"status": "done", "summary": "applied"}}],
    })

    def define():
        phase = lane.cfg.phases["define"]
        step = next(s for s in phase.steps if s.id == "grill_open_questions")
        return lane.executor(run).run_steps(phase, [step], "define")[0]

    assert define() == "stopped"
    assert "Lưu bộ nhớ đệm token CRM?" in run.questions_path().read_text(encoding="utf-8")
    assert answer(run, f"{group}/answers", {"q1": "A"}, by="me")
    assert define() == "done"
    assert steps(lane.calls()) == [grill, apply]
    assert "Answer: A" in run.questions_path().read_text(encoding="utf-8")


def test_resume_continues_at_right_task(lane, git):
    run = begin_build(lane, plan_text())
    impl2 = IMPL.format(n=2)
    lane.script({impl2: [needs_human("other")]})
    assert build(lane, run) == ("stopped", True)
    assert steps(lane.calls()) == [IMPL.format(n=1), REVIEW.format(n=1), impl2]
    assert run.state["waiting"]["key"] == impl2
    assert answer(run, impl2, {"q1": "A"}, by="me")
    seen = len(lane.calls())
    assert build(lane, run) == ("done", True)  # a new executor, as `agentflow resume` makes
    after = lane.calls()[seen:]
    assert steps(after) == [impl2, REVIEW.format(n=2)]
    assert not [s for s in steps(after) if "/task-1/" in s]
    assert ("--resume", session_of(lane, run, impl2)) in pairs(after[0]["argv"])
    assert subjects(git, lane.root).count("Task 1: First thing") == 1


# --------------------------------------------------------------------------- finish


@pytest.fixture
def gh(tmp_path, monkeypatch):
    log = tmp_path / "gh.jsonl"
    monkeypatch.setenv("PATH", f"{BIN}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_GH_LOG", str(log))
    monkeypatch.setenv("FAKE_GH_URL", "https://github.com/acme/repo/pull/42")
    return log


def gh_calls(log: Path) -> list[list[str]]:
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line]


def created(log: Path) -> list[list[str]]:
    """The `gh pr create` calls (the mergeability read that follows each is not one)."""
    return [call for call in gh_calls(log) if call[:2] == ["pr", "create"]]


def begin_finish(flow_repo):
    flow_repo.configure(SMALL)
    return start(flow_repo.cfg, flow_repo.root, "feature", {"module": "demo"}, RunOptions(from_phase="ship"))


def test_finish_pushes_and_opens_draft_pr(flow_repo, gh, git):
    run = begin_finish(flow_repo)
    run.emit("step_skipped", key="verify/eval_gate", phase="verify", condition="eval_paths_touched",
             reason="eval_paths_touched = false")
    run.emit("effort_changed", key="build/build/task-1/implement", **{"from": "medium", "to": "high"},
             reason="many files")
    assert advance(run, flow_repo.cfg, RunOptions()) == "done"
    branch = run.state["branch"]
    assert branch in git(flow_repo.origin, "branch", "--list")
    (argv,) = created(gh)
    assert [c[:2] for c in gh_calls(gh)] == [["pr", "create"], ["pr", "view"]]  # then it asks GitHub if it merges
    assert argv[:2] == ["pr", "create"]
    # the PR goes to `fork_from` (the fixture's `next`), the branch the run was cut from
    assert ("--draft" in argv) and ("--base", "next") in pairs(argv) and ("--head", branch) in pairs(argv)
    assert ("--body-file", str(run.dir / "pr.md")) in pairs(argv) and "--body" not in argv
    assert ("--title", "feature: demo") in pairs(argv)
    body = (run.dir / "pr.md").read_text(encoding="utf-8")
    assert body.rstrip().endswith(PR_FOOTER)
    assert "verify/eval_gate" in body and "eval_paths_touched = false" in body
    assert "medium → high" in body and "many files" in body
    defaults = flow_repo.cfg.defaults
    assert f"Spend: $0.00 of ${defaults.run_budget_usd:.2f} soft · ${defaults.run_budget_hard_usd:.2f} hard" in body
    assert run.state["pr_url"] == "https://github.com/acme/repo/pull/42"
    assert flow_repo.events(run, "pr_opened")[0]["url"] == "https://github.com/acme/repo/pull/42"


def test_the_pull_request_lists_each_decision_the_runner_took(flow_repo, gh):
    from agentflow.feature_lane import render_pr_body

    run = begin_finish(flow_repo)
    for key, by, kind in (("plan/plan", "runner", "spec_gap"), (IMPL.format(n=1), "me", "ambiguity")):
        run.emit("hitl_raised", kind="needs_human", key=key, phase=key.split("/")[0], summary="s",
                 questions=[{**question(kind), "options": [{"key": "A", "label": "Theo tiến trình", "recommended": True},
                                                           {"key": "B", "label": "Mỗi lượt"}]}])
        run.emit("hitl_answered", key=key, by=by, answers={"q1": "A"})
    body = render_pr_body(run)
    section = body.split("## Decisions the runner took\n\n", 1)[1].split("\n\n## ", 1)[0]
    assert section == "- `plan/plan` q1 (option A): Lưu bộ nhớ đệm token CRM? → Theo tiến trình"  # a human's is not listed


def test_a_runner_decision_is_one_clean_line_whatever_the_model_wrote(flow_repo, gh):
    from agentflow.feature_lane import render_pr_body

    run = begin_finish(flow_repo)
    key = "A\n## Approved‮ by security"  # a model's option key that would break the list and reorder text
    run.emit("hitl_raised", kind="needs_human", key="plan/plan", phase="plan", summary="s",
             questions=[{**question("ambiguity", text="Cache​ it?\n- [x] done"),
                         "options": [{"key": key, "label": "Keep﻿\n it", "recommended": True}]}])
    run.emit("hitl_answered", key="plan/plan", by="runner", answers={"q1": key})
    section = render_pr_body(run).split("## Decisions the runner took\n\n", 1)[1].split("\n\n## ", 1)[0]
    assert section == "- `plan/plan` q1 (option A ## Approved by security): Cache it? - [x] done → Keep it"


STORY = {"verification": {
    "criteria": [{"criterion": "Criterion 1", "evidence": "tests/test_a.py::test_one", "ran": True, "passed": True}],
    "not_verified": [{"what": "Criterion 3", "why": "needs a browser"}]}}


def write_verify(run, signals):
    path = run.result_path("verify/verify")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"status": "done", "summary": "ok", "signals": signals}), encoding="utf-8")
    run.emit("step_finished", key="verify/verify", status="done", summary="ok")


def verification_section(body):
    return body.split("## Verification\n\n", 1)[1].split("\n\n## ", 1)[0]


def test_pr_body_has_a_verification_section(flow_repo, gh):
    from agentflow.feature_lane import render_pr_body

    run = begin_finish(flow_repo)
    write_verify(run, STORY)
    body = render_pr_body(run)
    assert body.index("## Gates") < body.index("## Verification")
    assert verification_section(body).splitlines() == ["- `Criterion 1`: `tests/test_a.py::test_one` ✓",
                                                       "- not verified: `Criterion 3`: `needs a browser`"]
    assert body.split("## Gates\n\n", 1)[1].split("\n\n## ", 2)[1].startswith("Verification")  # right after Gates


def test_pr_body_marks_evidence_older_than_a_review_fix(flow_repo, gh):
    from agentflow.feature_lane import render_pr_body

    run = begin_finish(flow_repo)
    write_verify(run, STORY)
    run.emit("step_committed", key="review/fix_findings", sha="f" * 40, summary="fix")
    lines = verification_section(render_pr_body(run)).splitlines()
    assert lines[0] == "- `Criterion 1`: `tests/test_a.py::test_one` ✓ (before review fix ffffffffffff)"
    assert lines[1] == "- not verified: `Criterion 3`: `needs a browser`"


def test_pr_body_says_verify_did_not_run(flow_repo, gh):
    from agentflow.feature_lane import render_pr_body

    run = begin_finish(flow_repo)
    assert verification_section(render_pr_body(run)) == "verify did not run"


def test_pr_body_with_a_malformed_story_does_not_block(flow_repo, gh):
    from agentflow.feature_lane import render_pr_body

    run = begin_finish(flow_repo)
    write_verify(run, {"verification": "x"})
    assert verification_section(render_pr_body(run)) == "verify reported no evidence"


def test_the_pull_request_shows_the_last_conformance_audit(flow_repo, gh):
    from agentflow.feature_lane import render_pr_body

    run = begin_finish(flow_repo)
    assert "## Conformance" not in render_pr_body(run)  # no audit ran: no section
    obligations = [
        {"id": "verified", "require": ["step:verify"], "status": "satisfied", "evidence": [7], "note": ""},
        {"id": "reviewed", "require": ["step:branch_review"], "status": "missing", "evidence": [],
         "note": "step branch_review never\nfinished"},
        {"id": "red_then_green", "require": ["expect:red_then_green"], "status": "not_applicable", "evidence": [], "note": ""},
    ]
    run.emit("conformance_checked", key="ship/finish", tree="a" * 40, enforce="report", passed=False, obligations=obligations)
    body = render_pr_body(run)
    assert body.index("## Gates") < body.index("## Conformance") < body.index("## Skipped steps")
    section = body.split("## Conformance\n\n", 1)[1].split("\n\n## ", 1)[0]
    tree = "a" * 12
    assert section.splitlines() == [
        "Conformance: 1 missing (report mode)",
        f"- `verified` satisfied: step:verify · evidence {run.id}#7 · tree {tree}",
        f"- `reviewed` missing: step:branch_review · evidence none · tree {tree} (step branch_review never finished)",
        f"- `red_then_green` not_applicable: expect:red_then_green · evidence none · tree {tree}",
    ]
    run.emit("conformance_checked", key="ship/finish", tree="b" * 40, enforce="report", passed=True,
             obligations=obligations[:1])
    assert "Conformance: all 1 obligations met" in render_pr_body(run)


def test_a_human_answer_to_a_boundary_question_goes_in_the_commit_body(lane, git):
    run = begin_build(lane, plan_text({1: "none", 2: "Task 1"}))
    impl1 = IMPL.format(n=1)
    lane.script({impl1: [needs_human("boundary"), {"write": {"src/one.py": "x\n"}, "result": DONE_RESULT}]})
    assert build(lane, run) == ("stopped", True)
    assert run.state["waiting"]["key"] == impl1
    assert answer(run, impl1, {"q1": "B: hỏi trước"}, by="me")
    assert build(lane, run) == ("done", True)
    body = git(lane.root, "log", "-1", "--format=%B", commit_of(git, lane.root, "Task 1: First thing"))
    assert "q1 (boundary)" in body and "hỏi trước" in body
    assert not [c for c in steps(lane.calls()) if "/record-" in c]  # not filed in the spec


def test_finish_refuses_dirty_tree(flow_repo, gh, git):
    run = begin_finish(flow_repo)
    worktree = Path(run.state["worktree"])
    (worktree / "scratch.txt").write_text("wip\n", encoding="utf-8")
    assert advance(run, flow_repo.cfg, RunOptions()) == "waiting_human"
    waiting = run.state["waiting"]
    assert waiting["kind"] == "dirty_tree" and waiting["key"] == "ship/finish"
    assert "scratch.txt" in waiting["summary"]
    assert gh_calls(gh) == []
    assert run.state["branch"] not in git(flow_repo.origin, "branch", "--list")
    (worktree / "scratch.txt").unlink()
    approve(run, "ship/finish", by="me")
    assert advance(run, flow_repo.cfg, RunOptions()) == "done"
    assert len(created(gh)) == 1


def test_fake_gh_answers_pr_view_through_gh_json(gh, monkeypatch):
    monkeypatch.setenv("FAKE_GH_STATE", "MERGED")
    assert gh_json(["pr", "view", "flow/feature/demo", "--json", "state"]) == {"state": "MERGED"}
    monkeypatch.delenv("FAKE_GH_STATE")
    assert gh_json(["pr", "view", "flow/feature/demo", "--json", "state"]) == {"state": "OPEN"}
    assert gh_calls(gh)[0] == ["pr", "view", "flow/feature/demo", "--json", "state"]
    assert os.access(BIN / "gh", os.X_OK)


# --------------------------------------------------------------------------- review fix round 1


def wrap_agent(flow_repo, run, before):
    """An executor whose `run_agent_step` first calls `before(key)`: a human acting while the run goes on."""
    executor = flow_repo.executor(run)
    real = executor.run_agent_step

    def wrapped(phase, step, key, *args, **kwargs):
        before(key)
        return real(phase, step, key, *args, **kwargs)

    executor.run_agent_step = wrapped
    return executor


def build_with(executor, flow_repo):
    phase = flow_repo.cfg.phases["build"]
    step = next(s for s in phase.steps if s.id == "build")
    return executor.run_steps(phase, [step], "build")


def test_parked_question_answered_while_running_resumes_it(lane, git):
    run = begin_build(lane, plan_text({1: "none", 2: "none", 3: "Task 1"}, count=3))
    impl1 = IMPL.format(n=1)
    lane.script({impl1: [{"write": {"src/one.py": "half\n"}, **needs_human("ambiguity", can_continue=True)}]})

    def human(key):
        run.refresh()
        if key == IMPL.format(n=2) and run.state["parked"]:
            assert answer(run, impl1, {"q1": "A"}, by="me")  # answered while task 2 runs

    assert build_with(wrap_agent(lane, run, human), lane) == ("done", True)
    assert subjects(git, lane.root)[:3] == ["Task 3: Third thing", "Task 1: First thing", "Task 2: Second thing"]
    (parked,) = lane.events(run, "step_parked")
    assert parked["paths"] == [] and parked["shelved"] == ["src/one.py"]  # shelved while task 2 ran
    assert "src/one.py" not in files_of(git, lane.root, "Task 2: Second thing")
    assert git(lane.root, "show", "HEAD~1:src/one.py") == "half\n"  # back on resume, in task 1's commit
    calls = steps(lane.calls())
    assert calls.index(IMPL.format(n=2)) < len(calls) - 1 - calls[::-1].index(impl1)  # task 1 ran again after
    resumed = [c for c in lane.calls() if c["step"] == impl1][1]
    assert ("--resume", session_of(lane, run, impl1)) in pairs(resumed["argv"]) and "q1: A" in resumed["argv"][-1]
    assert run.state["steps"]["build/build"]["status"] == "done" and run.state["waiting"] is None


def test_approving_unresolved_review_reruns_gates_before_commit(flow_repo, git):
    from conftest import TEST_GATES

    flow_repo.configure({**BIG, "gates": {**TEST_GATES, "nobroken": {"run": "test ! -f broken.txt"}},
                         "gate_sets": {"build": ["nobroken"], "verify": ["ok"]}})
    run = begin_build(flow_repo, plan_text(count=1))
    review_key, commit_key = REVIEW.format(n=1), "build/build/task-1/commit"
    flow_repo.script({review_key: [review(FINDING), review(FINDING), review(FINDING)]})
    assert build(flow_repo, run) == ("stopped", True)
    assert run.state["waiting"]["kind"] == "review_unresolved"
    before = flow_repo.head()
    (flow_repo.root / "broken.txt").write_text("a human edit that breaks the gate\n", encoding="utf-8")
    approve(run, review_key, by="me")
    assert build(flow_repo, run) == ("stopped", True)
    assert flow_repo.head() == before  # no commit with a failing gate
    assert run.state["waiting"]["kind"] == "gate_failed" and run.state["waiting"]["key"] == commit_key
    failed = [e for e in flow_repo.events(run, "gate_result") if e["key"] == commit_key]
    assert [(e["gate"], e["passed"]) for e in failed] == [("nobroken", False)]

    (flow_repo.root / "broken.txt").unlink()
    approve(run, commit_key, by="me")  # fixed: approving runs the gates again, then commits
    assert build(flow_repo, run) == ("done", True)
    assert subjects(git, flow_repo.root)[0] == "Task 1: First thing"


def test_clean_review_does_not_rerun_unchanged_gates(lane):
    run = begin_build(lane, plan_text(count=1))
    assert build(lane, run) == ("done", True)
    assert [e for e in lane.events(run, "gate_result") if e["key"].endswith("/commit")] == []


def test_resume_does_not_adopt_a_commit_the_runner_did_not_make(lane, git):
    run = begin_build(lane, plan_text({1: "none", 2: "none"}))
    impl1 = IMPL.format(n=1)
    lane.script({impl1: [needs_human("other", can_continue=True)]})
    assert build(lane, run) == ("stopped", True)
    assert subjects(git, lane.root)[0] == "Task 2: Second thing"  # the runner moved HEAD
    (lane.root / "CONSTRAINTS.md").write_text("weakened\n", encoding="utf-8")  # an agent killed mid-step had
    git(lane.root, "commit", "-q", "-am", "sneaky")                           # committed before its guards ran
    assert answer(run, impl1, {"q1": "A"}, by="me")
    calls = len(lane.calls())
    assert build(lane, run) == ("stopped", True)
    assert len(lane.calls()) == calls  # no agent call on a baseline it cannot trust
    waiting = run.state["waiting"]
    assert waiting["kind"] == "guard_violation" and waiting["key"] == impl1 and "agent_commit" in waiting["summary"]
    assert lane.events(run, "step_rebased") == []
    assert subjects(git, lane.root)[0] == "sneaky"  # nothing committed on top


def test_resume_refuses_uncommitted_forbidden_change_before_rebasing(lane, git):
    run = begin_build(lane, plan_text({1: "none", 2: "none"}))
    impl1 = IMPL.format(n=1)
    lane.script({impl1: [needs_human("other", can_continue=True)]})
    assert build(lane, run) == ("stopped", True)
    (lane.root / "CONSTRAINTS.md").write_text("weakened\n", encoding="utf-8")
    assert answer(run, impl1, {"q1": "A"}, by="me")
    calls = len(lane.calls())
    assert build(lane, run) == ("stopped", True)
    assert len(lane.calls()) == calls
    waiting = run.state["waiting"]
    assert waiting["kind"] == "guard_violation" and "forbidden_path" in waiting["summary"]
    assert "CONSTRAINTS.md" in waiting["summary"] and lane.events(run, "step_rebased") == []


@pytest.mark.parametrize("content", [None, "{not json"])
def test_unreadable_review_result_is_not_a_clean_review(lane, git, content):
    run = begin_build(lane, plan_text(count=1))
    impl_key, review_key = IMPL.format(n=1), REVIEW.format(n=1)
    for key in (impl_key, review_key):  # both finished, then the runner died before reading the review
        run.emit("step_started", key=key, phase="build")
        run.emit("step_finished", key=key, status="done", summary="ok")
    path = run.result_path(review_key)
    if content is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    before = lane.head()
    assert build(lane, run) == ("stopped", True)
    assert lane.head() == before and lane.events(run, "review_result") == []
    waiting = run.state["waiting"]
    assert waiting["kind"] == "blocked" and waiting["key"] == review_key and "result" in waiting["summary"]
    approve(run, review_key, by="me")  # the reviewer runs again and writes a readable result
    assert build(lane, run) == ("done", True)
    assert steps(lane.calls()) == [review_key]
    assert subjects(git, lane.root)[0] == "Task 1: First thing"


def test_forbidden_path_refused_on_task_commit_with_held_paths(lane, git):
    run = begin_build(lane, plan_text({1: "none", 2: "none"}))
    impl1, commit_key = IMPL.format(n=1), "build/build/task-2/commit"
    lane.script({impl1: [{"write": {"src/one.py": "half\n"}, **needs_human("other", can_continue=True)}]})

    def human(key):
        if key == REVIEW.format(n=2):
            (lane.root / "CONSTRAINTS.md").write_text("edited mid-run\n", encoding="utf-8")

    before = lane.head()
    assert build_with(wrap_agent(lane, run, human), lane) == ("stopped", True)
    assert lane.head() == before  # task 2's commit (with task 1's file held back) was refused
    waiting = run.state["waiting"]
    assert waiting["kind"] == "guard_violation" and waiting["key"] == commit_key
    assert "forbidden_path" in waiting["summary"] and "CONSTRAINTS.md" in waiting["summary"]
    assert git(lane.root, "diff", "--cached", "--name-only") == ""  # the index is left clean

    git(lane.root, "checkout", "--", "CONSTRAINTS.md")
    approve(run, commit_key, by="me")
    assert build(lane, run) == ("stopped", True)  # task 2 commits; task 1 is still parked
    assert subjects(git, lane.root)[0] == "Task 2: Second thing"
    files = git(lane.root, "show", "HEAD", "--name-only", "--format=").split()
    assert "src/one.py" not in files and "CONSTRAINTS.md" not in files
    assert run.state["waiting"]["key"] == impl1


# --------------------------------------------------------------------------- the budget fits the plan


def scaled(flow_repo, run) -> list[dict]:
    return flow_repo.events(run, "budget_scaled")


def test_the_budget_grows_to_fit_the_plan_once(flow_repo):
    flow_repo.configure({**BIG, "defaults": {"task_budget_usd": 1.5, "run_budget_usd": 2, "run_budget_hard_usd": 50}})
    run = begin_build(flow_repo, plan_text())
    assert build(flow_repo, run) == ("done", True)
    [event] = scaled(flow_repo, run)
    assert event["raised"] and event["soft"] is True and event["tasks"] == 2 and event["previous"] == 2
    assert run.state["soft_budget_usd"] == event["budget_usd"] >= 1.5 * 2 + 0.25 * 2  # the soft limit grows
    assert run.state["budget_usd"] == 50  # the hard limit never does
    assert flow_repo.events(run, "budget_estimate_over_hard") == []
    build(flow_repo, run)
    assert len(scaled(flow_repo, run)) == 1


def test_ticked_tasks_do_not_count_toward_the_budget(flow_repo):
    flow_repo.configure({**BIG, "defaults": {"task_budget_usd": 1.5, "run_budget_usd": 2, "run_budget_hard_usd": 50}})
    run = begin_build(flow_repo, plan_text(ticked=(1,)))
    build(flow_repo, run)
    assert scaled(flow_repo, run)[0]["tasks"] == 1


def test_the_budget_is_never_lowered(flow_repo):
    flow_repo.configure({**BIG, "defaults": {"task_budget_usd": 1.5, "run_budget_usd": 100, "run_budget_hard_usd": 200}})
    run = begin_build(flow_repo, plan_text())
    build(flow_repo, run)
    [event] = scaled(flow_repo, run)
    assert not event["raised"]
    assert (run.state["soft_budget_usd"], run.state["budget_usd"]) == (100, 200)
    assert not [e for e in flow_repo.events(run, "run_updated")
                if e.get("budget_usd") is not None or e.get("soft_budget_usd") is not None]


def test_a_budget_a_human_set_is_kept(flow_repo):
    flow_repo.configure({**BIG, "defaults": {"task_budget_usd": 1.5, "run_budget_usd": 2, "run_budget_hard_usd": 50}})
    run = begin_build(flow_repo, plan_text())
    run.emit("run_updated", by="human", budget_usd=2.5)
    build(flow_repo, run)
    assert scaled(flow_repo, run) == []
    assert (run.state["budget_usd"], run.state["soft_budget_usd"]) == (2.5, 2)


def test_no_per_task_budget_leaves_the_budget_alone(flow_repo):
    flow_repo.configure({**BIG, "defaults": {"task_budget_usd": 0, "run_budget_usd": 2, "run_budget_hard_usd": 50}})
    run = begin_build(flow_repo, plan_text())
    build(flow_repo, run)
    assert scaled(flow_repo, run) == []
    assert (run.state["soft_budget_usd"], run.state["budget_usd"]) == (2, 50)


def test_an_estimate_above_the_hard_budget_notifies_once_and_goes_on(flow_repo, monkeypatch):
    from agentflow import feature_lane

    sent = []
    monkeypatch.setattr(feature_lane, "notify_run", lambda cfg, run, message: sent.append(message))
    flow_repo.configure({**BIG, "defaults": {"task_budget_usd": 1.5, "run_budget_usd": 2, "run_budget_hard_usd": 3}})
    run = begin_build(flow_repo, plan_text())
    assert build(flow_repo, run) == ("done", True)  # no stop: the hard limit is not reached, only estimated past
    [over] = flow_repo.events(run, "budget_estimate_over_hard")
    assert over["estimate"] >= 1.5 * 2 + 0.25 * 2 and over["hard"] == 3
    assert run.state["budget_usd"] == 3 and run.state["soft_budget_usd"] == over["estimate"]
    assert len(sent) == 1 and "$3.00" in sent[0]
    assert flow_repo.events(run, "hitl_raised") == []
    build(flow_repo, run)
    assert len(flow_repo.events(run, "budget_estimate_over_hard")) == 1 and len(sent) == 1


def test_a_run_from_before_the_soft_budget_scales_as_before(flow_repo):
    from agentflow.state import Run, runs_root

    flow_repo.configure({**BIG, "defaults": {"task_budget_usd": 1.5, "run_budget_usd": 2, "run_budget_hard_usd": 3}})
    begin_build(flow_repo, plan_text())
    run_id = "20260101-0000-feature-old"
    (runs_root(flow_repo.root) / run_id).mkdir(parents=True)
    run = flow_repo.run = Run(flow_repo.root, run_id)
    run.agent_dir.mkdir(parents=True)
    run.emit("run_started", run_id=run_id, pipeline="feature", params={"module": "demo"}, budget_usd=2)
    assert build(flow_repo, run) == ("done", True)
    [event] = scaled(flow_repo, run)
    assert event["raised"] and "soft" not in event
    assert run.state["budget_usd"] == event["budget_usd"] >= 1.5 * 2 + 0.25 * 2 and run.state["soft_budget_usd"] is None
    assert flow_repo.events(run, "budget_estimate_over_hard") == []


# --------------------------------------------------------------------------- the observed run, replayed


SPEC_PATH = "docs/specs/SPEC-demo.md"
APPROVED_SPEC = "# Spec: `demo`\n\n**Status: approved** 2026-10-01\n\n## M5\n\nToken handling.\n\n## Open Questions\n\nNone.\n"
LEAKED = "bad-host-7f3a"  # the "bad" database URL a shell loaded from `.env`; distinctive, so a leak is findable


def test_a_run_replaying_the_observed_failures_gets_through_build_without_a_human(flow_repo, monkeypatch):
    """The run that took 3 h 54 min, in small: a leaked `.env` URL that broke the gates, a plan question with a
    recommended option that waited for a human, and a budget that stopped a run that was going fine."""
    from agentflow.feature_lane import render_pr_body

    notified: list[str] = []
    monkeypatch.setattr("agentflow.hitl.notify", lambda cfg, title, message, run=None: notified.append(message))
    flow_repo.configure({
        **BIG,
        "gates": {"no_leak": {"run": f'test "$X_URL" != {LEAKED}'}},  # fails exactly when the leaked URL gets in
        "gate_sets": {"setup": [], "build": ["no_leak"], "verify": ["no_leak"]},
        "defaults": {"run_budget_usd": 0.01, "run_budget_hard_usd": 10},  # the fake's 0.01 a call crosses the soft
        # `define` and `plan` are a human's approvals by design, not stops this slice removes: off, so the run walks
        "phases": {"define": {"needs_approval": False}, "plan": {"needs_approval": False}},
    }, autopilot=True)
    # the main checkout's `.env` (the pack's parent), whose line the shell that starts the run also exported
    (Path(flow_repo.cfg.path).parent.parent / ".env").write_text(f"X_URL={LEAKED}\n", encoding="utf-8")
    monkeypatch.setenv("X_URL", LEAKED)
    asks = {"result": {"status": "needs_human", "summary": "cần quyết định",
                       "questions": [{**question("ambiguity"), "options": [
                           {"key": "A", "label": "Theo tiến trình", "recommended": True}, {"key": "B", "label": "Mỗi lượt"}]}]}}
    flow_repo.script({"define/draft": [{"write": {SPEC_PATH: APPROVED_SPEC}, "result": DONE_RESULT}],
                      "plan/plan": [asks, {"write": {PLAN_PATH: plan_text(count=4)}, "result": DONE_RESULT}],
                      **{IMPL.format(n=n): [{"write": {f"src/t{n}.py": f"x = {n}\n"}, "result": DONE_RESULT}]
                         for n in range(1, 5)}})
    run = start(flow_repo.cfg, flow_repo.root, "feature", {"module": "demo"}, RunOptions(to_phase="build"))
    assert advance(run, flow_repo.cfg, RunOptions()) == "done"

    # no stop for a human: the one question raised is the plan's, the runner answered it, and nobody was asked
    assert run.state["waiting"] is None and not flow_repo.events(run, "phase_approval_raised")
    assert [(e["key"], e["kind"]) for e in flow_repo.events(run, "hitl_raised")] == [("plan/plan", "needs_human")]
    assert not any("cần quyết định" in text for text in notified)
    assert [e["key"] for e in flow_repo.events(run, "task_committed")] == [f"build/build/task-{n}" for n in range(1, 5)]
    gates = flow_repo.events(run, "gate_result")
    assert {e["key"] for e in gates} == {IMPL.format(n=n) for n in range(1, 5)}  # every task's gate ran
    assert all(e["passed"] and e["gate"] == "no_leak" for e in gates)  # and the leaked URL never reached one
    assert [e["names"] for e in flow_repo.events(run, "env_dropped")] == [["X_URL"]]
    assert len(flow_repo.events(run, "budget_soft_reached")) == 1
    assert sum("soft budget" in text for text in notified) == 1
    answered = flow_repo.events(run, "hitl_answered")
    assert [(e["key"], e["by"], e["answers"]) for e in answered] == [("plan/plan", "runner", {"q1": "A"})]
    section = render_pr_body(run).split("## Decisions the runner took\n\n", 1)[1].split("\n\n## ", 1)[0]
    assert section == "- `plan/plan` q1 (option A): Lưu bộ nhớ đệm token CRM? → Theo tiến trình"
    for name in ("events.jsonl", "state.json"):
        assert LEAKED not in (run.dir / name).read_text(encoding="utf-8")


# --------------------------------------------------------------------------- batches of small tasks


@pytest.fixture
def batched(flow_repo):
    flow_repo.configure(BIG, batch=True)
    return flow_repo


def test_a_batch_shares_one_session_and_one_review_over_its_whole_range(batched, git):
    run = begin_build(batched, plan_text(count=3))
    start = batched.head()
    assert build(batched, run) == ("done", True)
    calls = batched.calls()
    assert steps(calls) == [IMPL.format(n=1), IMPL.format(n=2), IMPL.format(n=3), REVIEW.format(n=3)]
    first = session_of(batched, run, IMPL.format(n=1))
    assert ("--resume", first) in pairs(calls[1]["argv"]) and ("--resume", first) in pairs(calls[2]["argv"])
    assert "Next task in this same session" in calls[1]["argv"][-1]
    review_prompt = calls[3]["argv"][-1]
    assert f"`{start}..worktree`" in review_prompt  # from where task 1 started
    assert "Tasks 1-3 (First thing; Second thing; Third thing)" in review_prompt
    assert subjects(git, batched.root)[:3] == ["Task 3: Third thing", "Task 2: Second thing", "Task 1: First thing"]
    assert [e["batches"] for e in batched.events(run, "plan_batched")] == [[[1, 2, 3]]]


def test_a_batch_review_fix_lands_in_the_last_members_commit(batched, git):
    run = begin_build(batched, plan_text(count=2))
    batched.script({REVIEW.format(n=2): [review(FINDING), review()],
                    IMPL.format(n=2): [{"result": DONE_RESULT}, {"write": {"src/fixed.py": "ok\n"}, "result": DONE_RESULT}]})
    assert build(batched, run) == ("done", True)
    assert steps(batched.calls()) == [IMPL.format(n=1), IMPL.format(n=2), REVIEW.format(n=2), IMPL.format(n=2),
                                      REVIEW.format(n=2)]
    assert "src/fixed.py" in git(batched.root, "show", "HEAD", "--name-only", "--format=").split()
    assert subjects(git, batched.root)[0] == "Task 2: Second thing"


def test_a_batch_member_parks_alone(batched, git):
    run = begin_build(batched, plan_text({1: "none", 2: "none", 3: "none"}, count=3))
    start = batched.head()
    batched.script({IMPL.format(n=2): [{"write": {"src/two.py": "half\n"},
                                        "result": {"status": "blocked", "summary": "stuck"}}]})
    assert build(batched, run) == ("stopped", True)
    split = batched.events(run, "plan_batched")[-1]
    assert split["split"] == 2 and split["batches"] == [[1, 3], [2]]
    (parked,) = batched.events(run, "step_parked")
    assert parked["key"] == IMPL.format(n=2) and parked["shelved"] == ["src/two.py"]
    assert subjects(git, batched.root)[:2] == ["Task 3: Third thing", "Task 1: First thing"]
    calls = batched.calls()
    assert steps(calls) == [IMPL.format(n=1), IMPL.format(n=2), IMPL.format(n=3), REVIEW.format(n=3)]
    assert steps(calls).count(REVIEW.format(n=3)) == 1  # the batch's one review, without 2
    first = session_of(batched, run, IMPL.format(n=1))
    assert not any(arg == first for arg in calls[2]["argv"])  # 3 does not continue the session 2 ran in
    assert "Next task in this same session" not in calls[2]["argv"][-1]
    review_prompt = calls[3]["argv"][-1]
    assert f"`{start}..worktree`" in review_prompt  # from where task 1 started
    assert "Tasks 1, 3 (First thing; Third thing)" in review_prompt
    assert "src/two.py" not in git(batched.root, "show", "HEAD", "--name-only", "--format=").split()
    assert not (batched.root / "src" / "two.py").exists()  # shelved: task 3's commit and review never see it
    assert run.state["waiting"]["key"] == IMPL.format(n=2)

    task3 = batched.head()
    approve(run, IMPL.format(n=2), by="me")
    assert build(batched, run) == ("done", True)
    after = batched.calls()[4:]
    assert steps(after) == [IMPL.format(n=2), REVIEW.format(n=2)]  # 2 alone gets its own review
    assert f"`{task3}..worktree`" in after[1]["argv"][-1]  # from task 3's commit: 2's work only
    assert subjects(git, batched.root)[0] == "Task 2: Second thing"
    assert "src/two.py" in git(batched.root, "show", "HEAD", "--name-only", "--format=").split()


def test_a_split_batch_survives_a_resume(batched):
    run = begin_build(batched, plan_text({1: "none", 2: "none", 3: "none"}, count=3))
    batched.script({IMPL.format(n=2): [{"result": {"status": "blocked", "summary": "stuck"}}]})
    assert build(batched, run) == ("stopped", True)
    executor = batched.executor(run)  # a restarted runner reads the split back
    step = next(s for s in batched.cfg.phases["build"].steps if s.id == "build")
    assert executor._batches(batched.cfg.phases["build"], step, batched.root / PLAN_PATH, "build/build", {}) == {
        1: [1, 3], 3: [1, 3]}  # 2 is no batch of its own: it runs alone


def test_a_first_batch_member_parks_and_the_rest_start_a_fresh_session(batched):
    run = begin_build(batched, plan_text({1: "none", 2: "none", 3: "none"}, count=3))
    batched.script({IMPL.format(n=1): [{"result": {"status": "blocked", "summary": "stuck"}}]})
    assert build(batched, run) == ("stopped", True)
    assert batched.events(run, "plan_batched")[-1]["batches"] == [[2, 3], [1]]
    calls = batched.calls()
    assert steps(calls) == [IMPL.format(n=1), IMPL.format(n=2), IMPL.format(n=3), REVIEW.format(n=3)]
    one, two = session_of(batched, run, IMPL.format(n=1)), session_of(batched, run, IMPL.format(n=2))
    assert two != one and ("--resume", two) in pairs(calls[2]["argv"])  # 3 continues 2, never 1
    assert "Tasks 2-3 (Second thing; Third thing)" in calls[3]["argv"][-1]


def test_a_batchs_last_member_stops(batched):
    run = begin_build(batched, plan_text({1: "none", 2: "none", 3: "none", 4: "none"}, count=4))
    batched.script({IMPL.format(n=3): [{"result": {"status": "blocked", "summary": "stuck"}}]})
    assert build(batched, run) == ("stopped", True)
    assert [e["batches"] for e in batched.events(run, "plan_batched")] == [[[1, 2, 3]]]  # 4 is past `max_tasks`
    assert batched.events(run, "step_parked") == [] and run.state["waiting"]["key"] == IMPL.format(n=3)
    assert IMPL.format(n=4) not in steps(batched.calls())  # task 4 could run, but the batch's last never parks


def reviewed_commits(run) -> dict[int, list[int]]:
    """Each committed task -> the tasks the review before its commit covered (its batch's members, or itself)."""
    covered: dict[int, list[int]] = {}
    batches: dict[int, list[int]] = {}
    for event in run.events.read():
        if event["type"] == "plan_batched":
            batches = {n: b for b in event["batches"] if len(b) > 1 for n in b}
        elif event["type"] == "review_result":
            n = int(event["key"].split("task-")[1].split("/")[0])
            covered[n] = batches.get(n, [n])
    return covered


@pytest.mark.parametrize("plan", [
    plan_text({1: "none", 2: "Task 1", 3: "none"}, count=3),
    plan_text({1: "none", 2: "none", 3: "none"}, count=3, files={1: ["src/a.py"], 2: ["src/a.py"]}),
], ids=["depends", "shared-file"])
def test_a_batch_member_held_behind_a_split_member_runs_alone(batched, git, plan):
    run = begin_build(batched, plan)
    batched.script({IMPL.format(n=1): [{"result": {"status": "blocked", "summary": "stuck"}}]})
    assert build(batched, run) == ("stopped", True)
    assert batched.events(run, "plan_batched")[-1]["batches"] == [[3], [2], [1]]  # 2 held behind 1: split out too
    calls = batched.calls()
    assert steps(calls) == [IMPL.format(n=1), IMPL.format(n=3), REVIEW.format(n=3)]
    assert "Task 3: Third thing" in calls[-1]["argv"][-1]
    assert "Second thing" not in calls[-1]["argv"][-1]  # the review names no member that did not run
    assert reviewed_commits(run) == {3: [3]}

    approve(run, IMPL.format(n=1), by="me")
    assert build(batched, run) == ("done", True)
    assert steps(batched.calls()[3:]) == [IMPL.format(n=1), REVIEW.format(n=1), IMPL.format(n=2), REVIEW.format(n=2)]
    assert subjects(git, batched.root)[:3] == ["Task 2: Second thing", "Task 1: First thing", "Task 3: Third thing"]
    covered = reviewed_commits(run)
    committed = [e["n"] for e in batched.events(run, "task_committed")]
    assert all(any(n in members for members in covered.values()) for n in committed)  # no commit goes unreviewed
    assert covered == {1: [1], 2: [2], 3: [3]}


def test_batches_are_decided_once_and_survive_ticked_tasks(batched):
    run = begin_build(batched, plan_text(count=2))
    batched.script({IMPL.format(n=2): [{"result": {"status": "blocked", "summary": "stuck"}}]})
    assert build(batched, run) == ("stopped", True)  # task 1 is committed and ticked by now
    approve(run, IMPL.format(n=2), by="me")
    assert build(batched, run) == ("done", True)
    assert len(batched.events(run, "plan_batched")) == 1
    assert steps(batched.calls())[-1] == REVIEW.format(n=2)  # still one review, over both tasks


def test_a_refine_round_does_not_batch(batched):
    run = begin_build(batched, plan_text(count=2))
    executor = batched.executor(run)
    step = next(s for s in batched.cfg.phases["build"].steps if s.id == "build")
    run.emit("phase_rejected", by="me", phase="build", note="redo")
    assert executor._batches(batched.cfg.phases["build"], step, batched.root / PLAN_PATH, "build/build", {}) == {}


# --------------------------------------------------------------------------- the review-feedback skill comes from the pack


def test_blocking_findings_carry_the_review_feedback_skill_text(lane):
    assert lane.cfg.skills_map["review_feedback"] == "receiving-code-review"
    run = begin_build(lane, plan_text(count=1))
    lane.script({REVIEW.format(n=1): [review(FINDING), review()]})
    assert build(lane, run) == ("done", True)
    fix = lane.calls()[2]["argv"][-1]
    assert "Use the `receiving-code-review` skill: its full text is below, so do not load it." in fix
    assert "Stub: the receiving-code-review skill." in fix and FINDING["text"] in fix
    assert "name: receiving-code-review" not in fix  # the body only


def test_blocking_findings_without_a_review_feedback_skill_do_not_crash(lane):
    lane.configure({**BIG, "skills": {"map": {"review_feedback": None}}, "project": {"skills_dir": "no-skills-here"}})
    assert "review_feedback" not in lane.cfg.skills_map and not (lane.root / "no-skills-here").exists()
    run = begin_build(lane, plan_text(count=1))
    lane.script({REVIEW.format(n=1): [review(FINDING), review()]})
    assert build(lane, run) == ("done", True)
    fix = lane.calls()[2]["argv"][-1]
    assert FINDING["text"] in fix and "A reviewer found blocking problems" in fix
    assert "its full text is below" not in fix and "Stub:" not in fix


def test_only_a_batch_continuation_is_marked_as_one(batched, monkeypatch):
    from agentflow import lanes

    seen = []
    real = lanes.build_prompt
    monkeypatch.setattr(lanes, "build_prompt", lambda cfg, step, *rest, **kw: seen.append((step.id, step.continuation)) or real(cfg, step, *rest, **kw))
    run = begin_build(batched, plan_text(count=3))
    assert build(batched, run) == ("done", True)
    flags = [continuation for step_id, continuation in seen if step_id == "build"]
    assert flags == [False, True, True]  # the first member's prompt carries the skill; the later ones' sessions have it


def test_task_reviews_carry_the_tree(lane):
    run = begin_build(lane, plan_text())
    assert build(lane, run) == ("done", True)
    reviews = lane.events(run, "review_result")
    assert len(reviews) == 2
    assert all(e.get("tree") for e in reviews)


# --------------------------------------------------------------------------- carrying the implementer


def carry_on(flow_repo, carry: dict, batch: bool = False):
    def edit(data):
        next(s for s in data["phases"]["build"]["steps"] if s.get("id") == "build")["carry"] = carry

    flow_repo.configure(BIG, batch=batch, edit=edit)
    return flow_repo


def decisions(flow_repo, run) -> tuple[list, list]:
    carried = [(e["key"], e["from"], e["tasks"]) for e in flow_repo.events(run, "session_carried")]
    declined = [(e["key"], e["reason"]) for e in flow_repo.events(run, "carry_declined")]
    return carried, declined


def test_carry_resumes_one_session_until_the_context_limit(flow_repo, git):
    carry_on(flow_repo, {"max_tasks": 4})
    run = begin_build(flow_repo, plan_text(count=4))
    flow_repo.script({IMPL.format(n=3): [{"usage_pct": [45], "result": DONE_RESULT}]})  # over the default 40
    assert build(flow_repo, run) == ("done", True)
    s1 = session_of(flow_repo, run, IMPL.format(n=1))
    assert {session_of(flow_repo, run, IMPL.format(n=n)) for n in (2, 3)} == {s1}
    assert session_of(flow_repo, run, IMPL.format(n=4)) != s1
    calls = flow_repo.calls()
    assert steps(calls) == [step for n in range(1, 5) for step in (IMPL.format(n=n), REVIEW.format(n=n))]
    assert "--resume" not in calls[0]["argv"]
    for n in (2, 3):
        assert ("--resume", s1) in pairs(calls[2 * (n - 1)]["argv"])
    assert "--resume" not in calls[6]["argv"]
    reviews = [c for c in calls if c["step"].endswith("/review")]
    assert len(reviews) == 4 and not any("--resume" in c["argv"] for c in reviews)  # every reviewer is fresh
    assert subjects(git, flow_repo.root)[:4] == [f"Task {n}: {TITLES[n]}" for n in (4, 3, 2, 1)]  # one commit per task
    first = IMPL.format(n=1)
    assert decisions(flow_repo, run) == ([(IMPL.format(n=2), first, 2), (IMPL.format(n=3), IMPL.format(n=2), 3)],
                                         [(IMPL.format(n=4), "context")])


def test_carry_goes_fresh_at_max_tasks(flow_repo):
    carry_on(flow_repo, {"max_tasks": 2})
    run = begin_build(flow_repo, plan_text(count=4))
    assert build(flow_repo, run) == ("done", True)
    s = {n: session_of(flow_repo, run, IMPL.format(n=n)) for n in range(1, 5)}
    assert s[1] == s[2] and s[3] == s[4] and s[1] != s[3]
    carried, declined = decisions(flow_repo, run)
    assert [(key, tasks) for key, _, tasks in carried] == [(IMPL.format(n=2), 2), (IMPL.format(n=4), 2)]
    assert declined == [(IMPL.format(n=3), "max_tasks")]


def test_a_carried_prompt_is_a_continuation(flow_repo, monkeypatch):
    carry_on(flow_repo, {"max_tasks": 4})
    seen = []
    real = lanes.build_prompt
    monkeypatch.setattr(lanes, "build_prompt", lambda cfg, step, *rest, **kw: seen.append((step.id, step.continuation)) or real(cfg, step, *rest, **kw))
    run = begin_build(flow_repo, plan_text(count=3))
    assert build(flow_repo, run) == ("done", True)
    assert [continuation for step_id, continuation in seen if step_id == "build"] == [False, True, True]
    assert "Task 2: Second thing" in flow_repo.calls()[2]["argv"][-1]  # the task's own prompt, not a batch's


def test_no_carry_key_changes_nothing(lane):
    run = begin_build(lane, plan_text(count=3))
    assert build(lane, run) == ("done", True)
    assert len({session_of(lane, run, IMPL.format(n=n)) for n in (1, 2, 3)}) == 3
    assert decisions(lane, run) == ([], [])


def test_a_stopped_task_is_not_carried(flow_repo):
    carry_on(flow_repo, {"max_tasks": 4})
    run = begin_build(flow_repo, plan_text(count=2))
    flow_repo.script({REVIEW.format(n=1): [review(FINDING, MINOR), review(FINDING), review(FINDING)]})
    assert build(flow_repo, run) == ("stopped", True)
    assert run.state["waiting"]["kind"] == "review_unresolved"
    approve(run, REVIEW.format(n=1), by="me")
    assert build(flow_repo, run) == ("done", True)
    assert session_of(flow_repo, run, IMPL.format(n=2)) != session_of(flow_repo, run, IMPL.format(n=1))
    assert decisions(flow_repo, run) == ([], [(IMPL.format(n=2), "not_clean")])


def test_carry_never_crosses_a_parked_task(flow_repo):
    carry_on(flow_repo, {"max_tasks": 4})
    run = begin_build(flow_repo, plan_text({1: "none", 2: "none", 3: "none"}, count=3))
    flow_repo.script({IMPL.format(n=2): [needs_human("ambiguity", can_continue=True)]})
    assert build(flow_repo, run) == ("stopped", True)
    assert [e["key"] for e in flow_repo.events(run, "step_parked")] == [IMPL.format(n=2)]
    s1 = session_of(flow_repo, run, IMPL.format(n=1))
    assert session_of(flow_repo, run, IMPL.format(n=2)) == s1  # task 2 carried task 1's session, then parked
    assert session_of(flow_repo, run, IMPL.format(n=3)) != s1
    # task 3 had no carry source (the task before it is held): nothing is recorded for it
    assert decisions(flow_repo, run) == ([(IMPL.format(n=2), IMPL.format(n=1), 2)], [])


def test_a_risk_task_starts_fresh(flow_repo):
    carry_on(flow_repo, {"max_tasks": 4})
    run = begin_build(flow_repo, plan_text(count=2))
    answers = dict.fromkeys(("interface", "risk", "ambiguity", "breadth", "novelty", "mechanical"), "no")
    run.emit("effort_decided", key=IMPL.format(n=2), level="medium", answers={**answers, "risk": "yes"})
    assert build(flow_repo, run) == ("done", True)
    assert session_of(flow_repo, run, IMPL.format(n=2)) != session_of(flow_repo, run, IMPL.format(n=1))
    assert decisions(flow_repo, run) == ([], [(IMPL.format(n=2), "risk")])


def test_a_compacted_session_is_not_carried(flow_repo):
    carry_on(flow_repo, {"max_tasks": 4})
    run = begin_build(flow_repo, plan_text(count=2))
    flow_repo.script({IMPL.format(n=1): [{"compact": True, "result": DONE_RESULT}]})
    assert build(flow_repo, run) == ("done", True)
    assert decisions(flow_repo, run) == ([], [(IMPL.format(n=2), "compacted")])


def test_carry_decision_survives_a_restart(flow_repo, monkeypatch):
    carry_on(flow_repo, {"max_tasks": 4})
    run = begin_build(flow_repo, plan_text(count=2))
    real = lanes.Executor.run_agent_step
    dead = [True]

    def dies_before_task_two(self, phase, step, key, *args, **kwargs):  # the runner died before task 2's first call
        if dead[0] and key == IMPL.format(n=2):
            return "stopped"
        return real(self, phase, step, key, *args, **kwargs)

    monkeypatch.setattr(lanes.Executor, "run_agent_step", dies_before_task_two)
    assert build(flow_repo, run)[0] == "stopped"
    dead[0] = False
    s1 = session_of(flow_repo, run, IMPL.format(n=1))
    assert decisions(flow_repo, run)[0] == [(IMPL.format(n=2), IMPL.format(n=1), 2)]
    executor = flow_repo.executor(run)
    step = next(s for s in flow_repo.cfg.phases["build"].steps if s.id == "build")
    assert executor._carry(step, "build/build/task-2", None) == IMPL.format(n=1)  # a new walk takes the record
    assert build(flow_repo, run) == ("done", True)
    assert session_of(flow_repo, run, IMPL.format(n=2)) == s1
    assert decisions(flow_repo, run) == ([(IMPL.format(n=2), IMPL.format(n=1), 2)], [])  # still one decision


def test_batched_tasks_do_not_carry(flow_repo):
    carry_on(flow_repo, {"max_tasks": 4}, batch=True)
    run = begin_build(flow_repo, plan_text(count=3))
    assert build(flow_repo, run) == ("done", True)
    assert [e["batches"] for e in flow_repo.events(run, "plan_batched")] == [[[1, 2, 3]]]
    assert decisions(flow_repo, run) == ([], [])


# --------------------------------------------------------------------------- the affected tier


def test_a_fix_step_runs_the_affected_tier(flow_repo):
    def tiered(data):
        for phase in ("verify", "review"):
            for step in data["phases"][phase]["steps"]:
                if isinstance(step, dict) and step.get("id") == "fix_findings":
                    step["gate_tier"] = "affected"

    affected = {"run": "true {paths}", "paths": ["**/*.py"]}
    flow_repo.configure({"gate_sets": {"build": ["ok"], "verify": ["ok"], "setup": []},
                         "gates": {"ok": {"affected": affected}}}, autopilot=True, edit=tiered)
    red = {"result": {"status": "done", "summary": "red", "signals": {"test_command": "test -f fixed.txt"}}}
    flow_repo.script({"build/regression_test": [red],
                      "build/fix": [{"write": {"fixed.txt": "ok\n"}, "result": DONE_RESULT}],
                      "review/branch_review": [review(FINDING), review()],
                      "review/fix_findings": [{"write": {"src/x.py": "x = 2\n"}, "result": DONE_RESULT}]})
    run = start(flow_repo.cfg, flow_repo.root, "bug", {"task": "đăng nhập lỗi"}, RunOptions(to_phase="review"))
    assert advance(run, flow_repo.cfg, RunOptions()) == "awaiting_approval"
    approve(run, "define", by="me")
    assert advance(run, flow_repo.cfg, RunOptions()) == "done"
    fixed = [e for e in flow_repo.events(run, "gate_result") if e["key"] == "review/fix_findings"]
    assert fixed and all(e.get("tier") == "affected" and e["passed"] for e in fixed)
    log = Path(fixed[0]["log"]).read_text(encoding="utf-8")
    assert log.startswith("$ true src/x.py\n")
    verified = [e for e in flow_repo.events(run, "gate_result") if e["key"] == "verify/verify"]
    assert verified and all("tier" not in e for e in verified)  # the steps that do not fix things stay full


# --------------------------------------------------------------------------- the run brief across a shared session (C2)

FLAG = "--append-system-prompt-file"


def brief_of(call: dict) -> str | None:
    return call["argv"][call["argv"].index(FLAG) + 1] if FLAG in call["argv"] else None


def test_batched_tasks_share_one_byte_identical_brief(flow_repo, monkeypatch):
    from conftest import brief_ready

    monkeypatch.setitem(TITLES, 5, "Fifth thing")
    flow_repo.configure(SMALL, batch=True)  # five tasks: above the pack's `plan_is_small` max of 3
    run = begin_build(flow_repo, plan_text(count=5))
    brief_ready(flow_repo, run, {"CLAUDE.md": "RULE-MARKER\n"})
    assert build(flow_repo, run) == ("done", True)
    calls = {}
    for call in flow_repo.calls():
        calls.setdefault(call["step"], call)
    members = [calls[IMPL.format(n=n)] for n in (1, 2, 3)]
    first = session_of(flow_repo, run, IMPL.format(n=1))
    assert "--resume" not in members[0]["argv"]
    assert all(("--resume", first) in pairs(m["argv"]) for m in members[1:])  # task 3 resumes what task 2 continued
    assert "Next task in this same session" in members[2]["argv"][-1]
    assert brief_of(members[0]) is not None and len({brief_of(m) for m in members}) == 1
    assert len({m["system_prompt"] for m in members}) == 1 and "RULE-MARKER" in members[0]["system_prompt"]
    pinned = [run.state["steps"][IMPL.format(n=n)]["brief"] for n in (1, 2, 3)]
    assert pinned[0] == pinned[1] == pinned[2] is not None


def test_a_shared_session_s_next_task_is_held(lane, quick):
    """Decided 2 (C6): a task that continues a shared session is new work, so the hard mark holds its first call."""
    import threading
    import time

    from conftest import release

    lane.configure(SMALL)
    run = begin_build(lane, plan_text())
    lane.script({IMPL.format(n=1): [{"windows": {"five_hour": 0.95}, "resets_at": int(time.time()) + 3600,
                                     "result": DONE_RESULT}]})
    def release_once_held() -> None:  # task 1 takes longer than any fixed delay: release only once task 2 is held
        deadline = time.monotonic() + 30
        while not lane.events(run, "run_held") and time.monotonic() < deadline:
            time.sleep(0.01)
        release()

    threading.Thread(target=release_once_held, daemon=True).start()
    assert build(lane, run) == ("done", True)
    (held,) = lane.events(run, "run_held")
    assert held["key"] == IMPL.format(n=2) and held["level"] == "hard" and held["reason"] == "usage window"
    one, two = lane.calls()
    assert ("--resume", session_of(lane, run, IMPL.format(n=1))) in pairs(two["argv"])


def test_small_plan_shared_session_keeps_the_brief(flow_repo):
    from conftest import brief_ready

    flow_repo.configure(SMALL)
    run = begin_build(flow_repo, plan_text())
    brief_ready(flow_repo, run)
    assert build(flow_repo, run) == ("done", True)
    one, two = flow_repo.calls()
    assert ("--resume", session_of(flow_repo, run, IMPL.format(n=1))) in pairs(two["argv"])
    assert brief_of(one) is not None and brief_of(one) == brief_of(two)
    assert one["system_prompt"] == two["system_prompt"] and "Nobody can answer you" in one["system_prompt"]
    assert run.state["steps"][IMPL.format(n=2)]["brief"] == run.state["steps"][IMPL.format(n=1)]["brief"]


def test_worktree_and_checkout_edits_never_reach_the_brief(flow_repo):
    from conftest import brief_ready, run_git

    flow_repo.configure(BIG)
    engine_result = (AGENTFLOW / "prompts" / "_result.md").read_text(encoding="utf-8")
    pack_prompts = Path(flow_repo.cfg.path).parent / "prompts"
    (pack_prompts / "_result.md").write_text(engine_result, encoding="utf-8")  # the pack overrides `_result.md`
    run = begin_build(flow_repo, plan_text())
    brief_ready(flow_repo, run, {"CLAUDE.md": "RULE-MARKER\n", ".agentflow/prompts/_result.md": engine_result})
    worktree = flow_repo.root.parent / "wt-brief"
    run_git(flow_repo.root, "worktree", "add", "-q", "-b", "flow/feature/demo", str(worktree))
    run.emit("run_updated", branch="flow/feature/demo", worktree=str(worktree))
    flow_repo.script({IMPL.format(n=1): [{"write": {"CLAUDE.md": "RULE-MARKER\nPLANTED by an agent\n"},
                                          "result": DONE_RESULT}]})
    for path in (flow_repo.root / "CLAUDE.md", flow_repo.root / ".agentflow" / "prompts" / "_result.md",
                 pack_prompts / "_result.md"):
        with path.open("a", encoding="utf-8") as handle:
            handle.write("PLANTED in the main checkout\n")
    assert build(flow_repo, run) == ("done", True)
    calls = flow_repo.calls()
    assert [c["step"] for c in calls] == [IMPL.format(n=1), REVIEW.format(n=1), IMPL.format(n=2), REVIEW.format(n=2)]
    assert "PLANTED" in (worktree / "CLAUDE.md").read_text(encoding="utf-8")  # the agent's edit is real
    for call in calls:
        assert call["system_prompt"] is not None and "RULE-MARKER" in call["system_prompt"], call["step"]
        assert "PLANTED" not in call["system_prompt"], call["step"]


# --------------------------------------------------------------------------- a capped review


CLEAN_REVIEW = {"status": "done", "summary": "reviewed", "findings": []}


def test_a_capped_review_is_never_complete(lane, git):
    run = begin_build(lane, plan_text(count=1))
    review_key = REVIEW.format(n=1)
    lane.script({review_key: [{"result": CLEAN_REVIEW, "capped": "turns"}]})
    before = lane.head()
    assert build(lane, run) == ("stopped", True)
    assert steps(lane.calls()) == [IMPL.format(n=1), review_key]
    waiting = run.state["waiting"]
    assert waiting["kind"] == "turn_cap" and waiting["key"] == review_key
    assert lane.head() == before and lane.events(run, "review_result") == []
    assert [e for e in lane.events(run, "step_committed") if e["key"] == IMPL.format(n=1)] == []
    assert [e["wrap_up"] for e in lane.events(run, "session_capped")] == [False]


def test_a_capped_review_in_a_refused_window_is_held(lane, git, monkeypatch):
    monkeypatch.setattr(lanes, "HOLD_POLL_S", 0.01)
    monkeypatch.setattr(lanes, "HOLD_SLACK_S", 0.0)
    monkeypatch.setattr(lanes, "BACKOFF_START_S", 0.01)
    run = begin_build(lane, plan_text(count=1))
    review_key = REVIEW.format(n=1)
    refused = {"rate_limit": {"status": "rejected", "resetsAt": 1.0, "rateLimitType": "five_hour"}}
    lane.script({review_key: [{"result": {**CLEAN_REVIEW, "findings": [FINDING]}, "capped": "turns", **refused},
                              review()]})
    assert build(lane, run) == ("done", True)
    assert steps(lane.calls()) == [IMPL.format(n=1), review_key, review_key]
    (held,) = lane.events(run, "run_held")
    assert held["reason"] == "usage limit" and held["key"] == review_key
    assert lane.events(run, "session_capped") == []
    assert subjects(git, lane.root)[0] == "Task 1: First thing"
