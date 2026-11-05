"""The feature lane (Task 8): plan tasks with per-task review and commits, small plans, parking,
decision recording, resuming at the right task, and `finish` (push + draft pull request)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

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
TITLES = {1: "First thing", 2: "Second thing", 3: "Third thing"}
BIG = {"gate_sets": {"build": ["ok"], "verify": ["ok"]}, "conditions": {"plan_is_small": {"max": 0}}}
SMALL = {"gate_sets": {"build": ["ok"], "verify": ["ok"]}}
FINDING = {"severity": "blocking", "file": "src/one.py", "line": 3, "text": "Thiếu kiểm tra lỗi khi token hết hạn"}
DONE_RESULT = {"status": "done", "summary": "did it"}
MINOR = {"severity": "minor", "file": "src/one.py", "line": 9, "text": "rename x"}


def question(kind: str, qid: str = "q1", text: str = "Lưu bộ nhớ đệm token MISA?") -> dict:
    return {"id": qid, "question": text, "options": [{"key": "A", "label": "Theo tiến trình", "recommended": True}],
            "evidence": ["docs/specs/SPEC-demo.md#M5"], "kind": kind}


def needs_human(kind: str, can_continue: bool = False, qid: str = "q1") -> dict:
    return {"result": {"status": "needs_human", "summary": "a decision is needed",
                       "questions": [question(kind, qid)], "can_continue_without": can_continue}}


def review(*findings: dict) -> dict:
    return {"result": {"status": "done", "summary": "reviewed", "findings": list(findings)}}


def plan_text(depends: dict[int, str] | None = None, ticked: tuple[int, ...] = (), count: int = 2) -> str:
    depends = depends or {}
    parts = ["# Plan: demo\n\n## Global Constraints\n\n- [ ] not a task box\n"]
    for n in range(1, count + 1):
        box = "x" if n in ticked else " "
        dep = f"**Depends on:** {depends[n]}\n\n" if n in depends else ""
        parts.append(f"### Task {n}: {TITLES[n]}\n\n{dep}- [{box}] **Step 1: Task {n} test**\n"
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
    impl_session, review_session = session_of(lane, run, impl_key), session_of(lane, run, review_key)
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
    assert len([e for e in lane.events(run, "gate_result") if e["key"] == impl_key]) == 2  # gates ran again
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
    assert (lane.root / "src" / "one.py").exists()
    assert "Task 1: First thing" not in subjects(git, lane.root)
    (parked,) = lane.events(run, "step_parked")
    assert parked["key"] == impl1 and parked["questions"][0]["id"] == "q1"
    assert parked["seq"] < lane.events(run, "agent_started")[1]["seq"]  # parked before task 2 started
    assert run.state["status"] == "waiting_human"
    assert run.state["waiting"]["key"] == impl1 and run.state["waiting"]["kind"] == "needs_human"
    assert run.state["waiting"]["questions"][0]["question"] == "Lưu bộ nhớ đệm token MISA?"
    assert run.inbox_path(impl1).exists()
    assert build(lane, run) == ("stopped", True)  # still waiting: nothing runs, nothing is raised again
    assert len(lane.calls()) == 3 and len(lane.events(run, "hitl_raised")) == 1

    assert answer(run, impl1, {"q1": "A: giữ theo tiến trình"}, by="me")
    assert build(lane, run) == ("done", True)
    after = lane.calls()[3:]
    assert steps(after) == [impl1, REVIEW.format(n=1), IMPL.format(n=3), REVIEW.format(n=3)]
    assert ("--resume", session_of(lane, run, impl1)) in pairs(after[0]["argv"])
    assert "giữ theo tiến trình" in after[0]["argv"][-1]
    body = git(lane.root, "log", "-1", "--format=%B", commit_of(git, lane.root, "Task 1: First thing"))
    assert "q1 (ambiguity)" in body and "giữ theo tiến trình" in body  # ambiguity answers go in the body
    task1 = git(lane.root, "show", commit_of(git, lane.root, "Task 1: First thing"), "--name-only", "--format=").split()
    assert "src/one.py" in task1
    assert [e["key"] for e in lane.events(run, "step_rebased")] == [impl1]  # task 2's commit moved HEAD
    assert not lane.events(run, "hitl_raised")[1:]  # no guard violation on the resumed task
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

    # a parked item is answerable while another one waits, here through the CLI
    assert main(["answer", run.id, "--key", impl2, "--set", "q2=B", "--no-continue"]) == 0
    run.refresh()
    assert run.state["waiting"]["key"] == impl1 and run.state["parked"] == {}
    assert run.state["steps"][impl2]["status"] == "running"
    assert answer(run, impl1, {"q1": "A"}, by="me")
    assert run.state["waiting"] is None and run.state["status"] == "running"
    assert build(lane, run) == ("done", True)
    assert {"Task 1: First thing", "Task 2: Second thing"} <= set(subjects(git, lane.root)[:2])


def test_parking_off_stops_at_once(lane, git):
    lane.configure({**BIG, "hitl": {"park_independent": False}})
    run = begin_build(lane, plan_text({1: "none", 2: "none"}))
    lane.script({IMPL.format(n=1): [needs_human("other", True)]})
    assert build(lane, run) == ("stopped", True)
    assert steps(lane.calls()) == [IMPL.format(n=1)]
    assert lane.events(run, "step_parked") == []


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
    assert "Lưu bộ nhớ đệm token MISA?" in rec_call["argv"][-1] and "Answer: A" in rec_call["argv"][-1]
    assert ("--resume", session_of(lane, run, impl)) in pairs(calls[2]["argv"])
    decide = "Decide Lưu bộ nhớ đệm token MISA in SPEC-demo"
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
    assert "Decide Lưu bộ nhớ đệm token MISA in SPEC-demo" in subjects(git, lane.root)


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
    assert "Lưu bộ nhớ đệm token MISA?" in run.questions_path().read_text(encoding="utf-8")
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
    assert f"Spend: $0.00 of ${flow_repo.cfg.defaults.run_budget_usd:.2f}" in body
    assert run.state["pr_url"] == "https://github.com/acme/repo/pull/42"
    assert flow_repo.events(run, "pr_opened")[0]["url"] == "https://github.com/acme/repo/pull/42"


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
    flow_repo.configure({**BIG, "defaults": {"task_budget_usd": 1.5, "run_budget_usd": 2}})
    run = begin_build(flow_repo, plan_text())
    assert build(flow_repo, run) == ("done", True)
    [event] = scaled(flow_repo, run)
    assert event["raised"] and event["tasks"] == 2 and event["previous"] == 2
    assert run.state["budget_usd"] == event["budget_usd"] >= 1.5 * 2 + 0.25 * 2
    build(flow_repo, run)
    assert len(scaled(flow_repo, run)) == 1


def test_ticked_tasks_do_not_count_toward_the_budget(flow_repo):
    flow_repo.configure({**BIG, "defaults": {"task_budget_usd": 1.5, "run_budget_usd": 2}})
    run = begin_build(flow_repo, plan_text(ticked=(1,)))
    build(flow_repo, run)
    assert scaled(flow_repo, run)[0]["tasks"] == 1


def test_the_budget_is_never_lowered(flow_repo):
    flow_repo.configure({**BIG, "defaults": {"task_budget_usd": 1.5, "run_budget_usd": 100}})
    run = begin_build(flow_repo, plan_text())
    build(flow_repo, run)
    [event] = scaled(flow_repo, run)
    assert not event["raised"]
    assert run.state["budget_usd"] == 100
    assert not [e for e in flow_repo.events(run, "run_updated") if e.get("budget_usd") is not None]


def test_a_budget_a_human_set_is_kept(flow_repo):
    flow_repo.configure({**BIG, "defaults": {"task_budget_usd": 1.5, "run_budget_usd": 2}})
    run = begin_build(flow_repo, plan_text())
    run.emit("run_updated", by="human", budget_usd=2.5)
    build(flow_repo, run)
    assert scaled(flow_repo, run) == []
    assert run.state["budget_usd"] == 2.5


def test_no_per_task_budget_leaves_the_budget_alone(flow_repo):
    flow_repo.configure({**BIG, "defaults": {"task_budget_usd": 0, "run_budget_usd": 2}})
    run = begin_build(flow_repo, plan_text())
    build(flow_repo, run)
    assert scaled(flow_repo, run) == []
    assert run.state["budget_usd"] == 2


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


def test_a_batch_member_stops_instead_of_parking(batched):
    run = begin_build(batched, plan_text({1: "none", 2: "none"}, count=2))
    batched.script({IMPL.format(n=1): [needs_human("ambiguity", can_continue=True)]})
    assert build(batched, run) == ("stopped", True)
    assert batched.events(run, "step_parked") == []
    assert steps(batched.calls()) == [IMPL.format(n=1)]  # task 2 waits: the batch stays whole
    assert run.state["waiting"]["key"] == IMPL.format(n=1)


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
