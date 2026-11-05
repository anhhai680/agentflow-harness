"""Refine rounds after a rejected phase (Task 9): every step of the phase again with the note, in its own
session at its recorded effort; new `Refine …` commits on top; `unchanged` steps listed at the next
approval; every plan task refined in `build`; conditions decided again; answers reused."""

from __future__ import annotations

from pathlib import Path

import pytest

from agentflow.flow import main
from agentflow.hitl import answer, reject
from agentflow.pipeline import RunOptions, advance, start
from agentflow.state import Run

SPEC = "docs/specs/SPEC-demo.md"
SPEC_PLAIN = "# Spec: `demo`\n\n## Objective\n\nMột mô-đun mẫu.\n\n## Open Questions\n\nNone for this module.\n"
SPEC_ROUTES = SPEC_PLAIN.replace("Một mô-đun mẫu.", "Một mô-đun mẫu với GET /items.")
SPEC_DELETE = SPEC_ROUTES.replace("GET /items.", "GET /items và DELETE /items/{id}.")
NOTE = "Thêm route xóa"
EFFORT_CHECK = "## Effort check"
PIPE = {"gate_sets": {"build": ["ok"], "verify": ["ok"]}}
BIG = {**PIPE, "conditions": {"plan_is_small": {"max": 0}}}
PLAN_PATH = "docs/plans/2026-10-29-demo.md"
TITLES = {1: "First thing", 2: "Second thing"}
QUESTION = {"id": "q1", "question": "Bộ nhớ đệm bao lâu?", "options": [{"key": "A", "label": "1 giờ", "recommended": True}],
            "evidence": [], "kind": "spec_gap"}


def done(summary: str = "did it", **extra) -> dict:
    return {"result": {"status": "done", "summary": summary, **extra}}


def unchanged(reason: str) -> dict:
    return {"result": {"status": "unchanged", "summary": reason}}


GO_HIGH = {"result": {"status": "effort_change", "effort": "high", "reason": "the spec has many parts"}}


@pytest.fixture
def pipe(flow_repo):
    flow_repo.configure(PIPE)
    return flow_repo


def begin(flow_repo, lane: str | None = None, **opts) -> Run:
    """A run of `lane`, or with no lane the spec-only run: `feature` stopped after `define`."""
    if lane is None:
        lane, opts = "feature", {"to_phase": "define", **opts}
    return start(flow_repo.cfg, flow_repo.root, lane, {"module": "demo"}, RunOptions(**opts))


def go(flow_repo, run: Run) -> str:
    return advance(run, flow_repo.cfg, RunOptions())


def pairs(argv: list[str]) -> set[tuple[str, str]]:
    return set(zip(argv, argv[1:]))


def session_of(flow_repo, run, key: str) -> str:
    """The step's latest session id."""
    return [e["session_id"] for e in flow_repo.events(run, "agent_session") if e["key"] == key][-1]


def calls_for(calls: list[dict], key: str) -> list[dict]:
    return [c for c in calls if c["step"] == key]


def git_in(git, run: Run, *args: str) -> str:
    return git(Path(run.state["worktree"]), *args)


def sha_of(git, run: Run, subject: str) -> str:
    for line in git_in(git, run, "log", "--format=%H %s").splitlines():
        sha, text = line.split(" ", 1)
        if text == subject:
            return sha
    raise AssertionError(f"no commit {subject!r}")


def approvals(flow_repo, run) -> list[tuple[str, int]]:
    return [(e["phase"], e["round"]) for e in flow_repo.events(run, "phase_approval_raised")]


# --------------------------------------------------------------------------- every step, its own session


def test_reject_reruns_every_step_with_note(pipe):
    pipe.script({
        "define/draft": [GO_HIGH, {"write": {SPEC: SPEC_ROUTES}, **done("drafted")},
                       {"write": {SPEC: SPEC_DELETE}, **done("added the delete route")}],
        "define/contracts": [done("contracts for GET"), done("contracts for DELETE")],
    })
    run = begin(pipe)
    assert go(pipe, run) == "awaiting_approval"
    draft_sid = session_of(pipe, run, "define/draft")
    contracts_sid = session_of(pipe, run, "define/contracts")
    before = len(pipe.calls())

    reject(run, "define", "me", NOTE)
    assert go(pipe, run) == "awaiting_approval"
    refined = pipe.calls()[before:]
    assert [c["step"] for c in refined] == ["define/draft", "define/contracts"]
    draft, contracts = refined
    assert ("--resume", draft_sid) in pairs(draft["argv"])
    assert ("--effort", "high") in pairs(draft["argv"])  # the effort the draft step settled on
    assert ("--resume", contracts_sid) in pairs(contracts["argv"])
    assert ("--effort", "medium") in pairs(contracts["argv"])
    for call in refined:
        prompt = call["argv"][-1]
        assert NOTE in prompt and "refine round 1" in prompt
        assert SPEC in prompt  # the phase's artifacts, by path
        assert EFFORT_CHECK not in prompt
    started = [e for e in pipe.events(run, "step_started") if e.get("mode") == "refine"]
    assert [(e["key"], e["round"]) for e in started] == [("define/draft", 1), ("define/contracts", 1)]
    # `unless: spec_exists` reads the file the draft step wrote itself, so it does not skip the draft
    kept = pipe.events(run, "condition_kept")
    assert [e["key"] for e in kept] == ["define/draft"] and SPEC in kept[0]["reason"]
    assert approvals(pipe, run) == [("define", 0), ("define", 1)]


def test_refine_commits_on_top(pipe, git):
    pipe.script({
        "define/draft": [{"write": {SPEC: SPEC_ROUTES}, **done("drafted")},
                       {"write": {SPEC: SPEC_DELETE}, **done("added the delete route")}],
        "define/contracts": [done("contracts"), unchanged("the contracts already cover it")],
    })
    run = begin(pipe)
    assert go(pipe, run) == "awaiting_approval"
    first = sha_of(git, run, "Draft SPEC-demo")

    reject(run, "define", "me", NOTE)
    assert go(pipe, run) == "awaiting_approval"
    subjects = git_in(git, run, "log", "--format=%s").splitlines()
    assert subjects[0] == "Refine draft: Draft SPEC-demo"
    assert "Draft SPEC-demo" in subjects[1:]
    assert sha_of(git, run, "Draft SPEC-demo") == first  # the earlier commit is untouched
    git_in(git, run, "merge-base", "--is-ancestor", first, "HEAD")  # raises if it is not an ancestor
    body = git_in(git, run, "log", "-1", "--format=%b")
    assert NOTE in body and "added the delete route" in body
    assert "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>" in body
    committed = [e for e in pipe.events(run, "step_committed") if e["key"] == "define/draft"]
    assert len(committed) == 2 and committed[-1]["sha"] == git_in(git, run, "rev-parse", "HEAD").strip()
    assert (Path(run.state["worktree"]) / SPEC).read_text(encoding="utf-8") == SPEC_DELETE


def test_unchanged_listed_at_next_approval(pipe, git):
    reason = "Ghi chú không liên quan đến hợp đồng"
    pipe.script({
        "define/draft": [{"write": {SPEC: SPEC_ROUTES}, **done("drafted")},
                       {"write": {SPEC: SPEC_DELETE}, **done("added the delete route")}],
        "define/contracts": [done("contracts"), unchanged(reason)],
    })
    run = begin(pipe)
    assert go(pipe, run) == "awaiting_approval"
    previous = run.state["phases"]["define"]["approval_sha"]

    reject(run, "define", "me", NOTE)
    assert go(pipe, run) == "awaiting_approval"
    assert approvals(pipe, run) == [("define", 0), ("define", 1)]  # the second request, after refine round 1
    assert [e["key"] for e in pipe.events(run, "step_unchanged")] == ["define/contracts"]
    inbox = run.inbox_path("define_approved").read_text(encoding="utf-8")
    assert "refine round 1" in inbox
    assert f"define/contracts: unchanged — {reason}" in inbox
    assert "define/draft: changed — added the delete route" in inbox
    since = inbox.split("## Since the last approval request", 1)[1]
    assert previous[:12] in since and "Refine draft: Draft SPEC-demo" in since and SPEC in since
    assert run.state["phases"]["define"]["approval_sha"] == git_in(git, run, "rev-parse", "HEAD").strip()


# --------------------------------------------------------------------------- build: every plan task


def plan_text() -> str:
    parts = ["# Plan: demo\n"]
    for n, title in TITLES.items():
        parts.append(f"### Task {n}: {title}\n\n**Depends on:** none\n\n- [ ] **Step 1: Task {n} code**\n")
    return "\n".join(parts)


def commit_spec_and_plan(flow_repo, git) -> None:
    for rel, text in ((SPEC, SPEC_PLAIN), (PLAN_PATH, plan_text())):
        target = flow_repo.root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    git(flow_repo.root, "add", "-A")
    git(flow_repo.root, "commit", "-q", "-m", "Spec and plan demo")


def test_build_refines_every_task(flow_repo, git):
    flow_repo.configure(BIG)
    commit_spec_and_plan(flow_repo, git)
    impl = "build/build/task-{n}/implement"
    review = "build/build/task-{n}/review"
    flow_repo.script({
        impl.format(n=1): [{"write": {"src/one.py": "x = 1\n"}, **done("task one")},
                           {"write": {"src/one.py": "x = 10\n"}, **done("task one refined")}],
        impl.format(n=2): [{"write": {"src/two.py": "y = 2\n"}, **done("task two")},
                           {"write": {"src/two.py": "y = 20\n"}, **done("task two refined")}],
    })
    run = begin(flow_repo, "feature", from_phase="build")
    assert go(flow_repo, run) == "awaiting_approval" and run.state["waiting"]["key"] == "build_approved"
    sids = {n: session_of(flow_repo, run, impl.format(n=n)) for n in TITLES}
    before = len(flow_repo.calls())

    reject(run, "build", "me", NOTE)
    assert go(flow_repo, run) == "awaiting_approval"
    refined = flow_repo.calls()[before:]
    assert [c["step"] for c in refined] == [impl.format(n=1), review.format(n=1), impl.format(n=2), review.format(n=2)]
    for n in TITLES:
        call = calls_for(refined, impl.format(n=n))[0]
        assert ("--resume", sids[n]) in pairs(call["argv"])
        assert NOTE in call["argv"][-1] and f"Task {n}: {TITLES[n]}" in call["argv"][-1]
    assert len(flow_repo.events(run, "review_result")) == 4  # every reviewer ran again
    subjects = git_in(git, run, "log", "--format=%s").splitlines()
    assert subjects[:4] == ["Refine Task 2: Second thing", "Refine Task 1: First thing",
                            "Task 2: Second thing", "Task 1: First thing"]
    body = git_in(git, run, "log", "-1", "--format=%b")
    assert NOTE in body and "task two refined" in body
    assert [(e["key"], e["round"]) for e in flow_repo.events(run, "task_committed")] == [
        ("build/build/task-1", 0), ("build/build/task-2", 0), ("build/build/task-1", 1), ("build/build/task-2", 1)]
    assert approvals(flow_repo, run) == [("build", 0), ("build", 1)]


# --------------------------------------------------------------------------- sessions and conditions


def test_refine_resume_failed_fresh_session(pipe):
    pipe.script({
        "define/draft": [GO_HIGH, {"write": {SPEC: SPEC_PLAIN}, **done("drafted")},
                       {"no_init": True, "exit": 1},
                       {"write": {SPEC: SPEC_PLAIN.replace("mẫu.", "mẫu, đã sửa.")}, **done("revised in a new session")}],
    })
    run = begin(pipe)
    assert go(pipe, run) == "awaiting_approval"
    sid = session_of(pipe, run, "define/draft")
    before = len(pipe.calls())

    reject(run, "define", "me", NOTE)
    assert go(pipe, run) == "awaiting_approval"
    failed, fresh = calls_for(pipe.calls()[before:], "define/draft")
    assert ("--resume", sid) in pairs(failed["argv"])
    assert "--resume" not in fresh["argv"]
    assert ("--effort", "high") in pairs(fresh["argv"])
    prompt = fresh["argv"][-1]
    assert str(run.progress_path()) in prompt and str(run.agent_dir / "handoff.md") in prompt  # workspace copies
    assert NOTE in prompt and "refine round 1" in prompt
    assert EFFORT_CHECK not in prompt
    started = [e for e in pipe.events(run, "agent_started") if e["step"] == "define/draft"]
    assert started[-1]["sizing"] is False and started[-1]["effort"] == "high"
    assert [e["key"] for e in pipe.events(run, "resume_failed")] == ["define/draft"]
    assert not [e for e in pipe.events(run, "hitl_raised") if e["key"] == "define/draft"]
    assert run.state["steps"]["define/draft"]["status"] == "done"


def test_condition_flips_on_refine(pipe):
    pipe.script({
        "define/draft": [{"write": {SPEC: SPEC_PLAIN}, **done("drafted")},
                       {"write": {SPEC: SPEC_ROUTES}, **done("added routes")}],
        "define/contracts": [done("contracts for GET")],
    })
    run = begin(pipe)
    assert go(pipe, run) == "awaiting_approval"
    assert not calls_for(pipe.calls(), "define/contracts")
    assert run.state["steps"]["define/contracts"]["status"] == "skipped"

    reject(run, "define", "me", NOTE)
    assert go(pipe, run) == "awaiting_approval"
    contracts = calls_for(pipe.calls(), "define/contracts")
    assert len(contracts) == 1
    assert "--resume" not in contracts[0]["argv"]  # a fresh session: the step never ran before
    assert ("--effort", "medium") in pairs(contracts[0]["argv"])
    assert EFFORT_CHECK in contracts[0]["argv"][-1]
    assert run.state["steps"]["define/contracts"]["status"] == "done"
    inbox = run.inbox_path("define_approved").read_text(encoding="utf-8")
    assert "define/contracts: changed — contracts for GET" in inbox


def test_condition_turning_false_skips_and_keeps_output(pipe, git):
    pipe.script({
        "define/draft": [{"write": {SPEC: SPEC_ROUTES}, **done("drafted")},
                       {"write": {SPEC: SPEC_PLAIN}, **done("dropped the routes")}],
        "define/contracts": [{"write": {"docs/specs/contracts-demo.md": "GET /items\n"}, **done("contracts")}],
    })
    run = begin(pipe)
    assert go(pipe, run) == "awaiting_approval"
    reject(run, "define", "me", "Bỏ route")
    assert go(pipe, run) == "awaiting_approval"
    assert len(calls_for(pipe.calls(), "define/contracts")) == 1
    skipped = [e for e in pipe.events(run, "step_skipped") if e["key"] == "define/contracts"]
    assert len(skipped) == 1 and skipped[0]["condition"] == "spec_needs_api_design"
    assert (Path(run.state["worktree"]) / "docs/specs/contracts-demo.md").read_text(encoding="utf-8") == "GET /items\n"
    assert "define/contracts: skipped" in run.inbox_path("define_approved").read_text(encoding="utf-8")


# --------------------------------------------------------------------------- inner checkpoints and moving on


def test_answers_reused_without_new_questions(pipe):
    key = "define/idea/idea_answers"
    pipe.script({"define/idea/idea_questions": [done("asked", questions=[QUESTION]), done("refined, no new questions"),
                                           done("asked again", questions=[QUESTION])]})
    run = begin(pipe, "feature")
    assert go(pipe, run) == "waiting_human" and run.state["waiting"]["key"] == key
    assert answer(run, key, {"q1": "A"}, "me")
    assert go(pipe, run) == "awaiting_approval"

    reject(run, "define", "me", "Hỏi thêm về TTL")
    assert go(pipe, run) == "awaiting_approval"
    assert [e["key"] for e in pipe.events(run, "hitl_raised")] == [key]  # not raised again
    assert [e["key"] for e in pipe.events(run, "checkpoint_reused")] == [key]

    # a refined step that writes new questions raises the checkpoint again; the old answer does not count
    reject(run, "define", "me", "Hỏi lại")
    assert go(pipe, run) == "waiting_human" and run.state["waiting"]["key"] == key
    assert [e["key"] for e in pipe.events(run, "hitl_raised")] == [key, key]
    assert len(pipe.events(run, "checkpoint_reused")) == 1


def test_reject_then_approve_moves_on(pipe, capsys):
    run = begin(pipe, "feature")
    assert go(pipe, run) == "awaiting_approval"
    assert main(["reject", run.id, "define", "--note", "Hỏi thêm về TTL", "--by", "tester"]) == 0
    run = Run.load(pipe.root, run.id)
    assert run.state["status"] == "awaiting_approval" and run.state["waiting"]["key"] == "define_approved"
    assert approvals(pipe, run) == [("define", 0), ("define", 1)]
    assert "plan" not in [e["phase"] for e in pipe.events(run, "phase_started")]

    assert main(["approve", run.id, "define", "--by", "tester"]) == 0
    run = Run.load(pipe.root, run.id)
    assert run.state["waiting"]["key"] == "plan_approved"
    assert [e["phase"] for e in pipe.events(run, "phase_started")] == ["define", "plan"]
    acts = [(e["type"], e["phase"], e["by"]) for e in run.events.read() if e["type"] in ("phase_rejected", "phase_approved")]
    assert acts == [("phase_rejected", "define", "tester"), ("phase_approved", "define", "tester")]
    assert "plan_approved" in capsys.readouterr().out


# --------------------------------------------------------------------------- review fixes (round 1)


def build_run(flow_repo, git, acts: dict) -> Run:
    """A feature run from `build` with two independent tasks, stopped at `build_approved`."""
    flow_repo.configure(BIG)
    commit_spec_and_plan(flow_repo, git)
    flow_repo.script(acts)
    run = begin(flow_repo, "feature", from_phase="build")
    assert go(flow_repo, run) == "awaiting_approval" and run.state["waiting"]["key"] == "build_approved"
    return run


IMPL1, IMPL2 = "build/build/task-1/implement", "build/build/task-2/implement"
REVIEW1, REVIEW2 = "build/build/task-1/review", "build/build/task-2/review"
AMBIGUITY = {**QUESTION, "kind": "ambiguity"}


def test_parked_task_in_refine_lets_independent_tasks_refine(flow_repo, git):
    run = build_run(flow_repo, git, {
        IMPL1: [{"write": {"src/one.py": "x = 1\n"}, **done("task one")},
                {"write": {"src/one.py": "x = 5\n"},
                 "result": {"status": "needs_human", "summary": "which TTL?", "questions": [AMBIGUITY],
                            "can_continue_without": True}},
                {"write": {"src/one.py": "x = 10\n"}, **done("task one refined")}],
        IMPL2: [{"write": {"src/two.py": "y = 2\n"}, **done("task two")},
                {"write": {"src/two.py": "y = 20\n"}, **done("task two refined")}],
    })
    reject(run, "build", "me", NOTE)
    assert go(flow_repo, run) == "waiting_human"
    assert run.state["waiting"]["key"] == IMPL1 and run.state["waiting"]["questions"][0]["id"] == "q1"
    assert git_in(git, run, "log", "-1", "--format=%s").strip() == "Refine Task 2: Second thing"
    files = git_in(git, run, "show", "--name-only", "--format=", "HEAD").split()
    assert "src/two.py" in files and "src/one.py" not in files  # the parked task's work stays out

    assert answer(run, IMPL1, {"q1": "A"}, "me")
    assert go(flow_repo, run) == "awaiting_approval" and run.state["waiting"]["key"] == "build_approved"
    assert git_in(git, run, "log", "-1", "--format=%s").strip() == "Refine Task 1: First thing"
    assert approvals(flow_repo, run) == [("build", 0), ("build", 1)]


def test_refine_reviewer_cannot_answer_unchanged(flow_repo, git):
    run = build_run(flow_repo, git, {
        IMPL1: [{"write": {"src/one.py": "x = 1\n"}, **done("task one")},
                {"write": {"src/one.py": "x = 10\n"}, **done("task one refined")}],
        REVIEW1: [done("reviewed", findings=[]), unchanged("the note is not about review"),
                  unchanged("still not about review")],
    })
    before_head = git_in(git, run, "rev-parse", "HEAD").strip()
    before = len(flow_repo.calls())
    reject(run, "build", "me", NOTE)
    assert go(flow_repo, run) == "waiting_human"
    waiting = run.state["waiting"]
    assert waiting["key"] == REVIEW1 and waiting["kind"] == "blocked"
    assert git_in(git, run, "rev-parse", "HEAD").strip() == before_head  # nothing committed unreviewed
    assert not [e for e in flow_repo.events(run, "step_unchanged") if e["key"] == REVIEW1]
    review_call = calls_for(flow_repo.calls()[before:], REVIEW1)[0]
    prompt = review_call["argv"][-1]
    assert "--resume" not in review_call["argv"]  # a fresh reviewer session on the review prompt
    assert "This is refine round" not in prompt and NOTE not in prompt
    start = [e for e in flow_repo.events(run, "step_started") if e["key"] == IMPL1][-1]["start_head"]
    assert f"{start}..worktree" in prompt  # the refine diff: what changed since the task's previous commit
    assert start == before_head


def test_unchanged_with_edits_is_refused(pipe, git):
    pipe.script({
        "define/draft": [{"write": {SPEC: SPEC_PLAIN}, **done("drafted")},
                       {"write": {SPEC: SPEC_PLAIN.replace("mẫu.", "mẫu, đã sửa.")}, **unchanged("nothing to do")},
                       done("kept the edit")],
    })
    run = begin(pipe)
    assert go(pipe, run) == "awaiting_approval"
    approved_head = git_in(git, run, "rev-parse", "HEAD").strip()

    reject(run, "define", "me", NOTE)
    assert go(pipe, run) == "waiting_human"
    waiting = run.state["waiting"]
    assert waiting["key"] == "define/draft" and waiting["kind"] == "blocked" and SPEC in waiting["summary"]
    assert not pipe.events(run, "step_unchanged")
    assert git_in(git, run, "rev-parse", "HEAD").strip() == approved_head  # not swept into another commit
    assert not [e for e in pipe.events(run, "step_started") if e["key"] == "define/contracts"]

    from agentflow.hitl import approve
    approve(run, "define/draft", "me")
    assert go(pipe, run) == "awaiting_approval"
    assert git_in(git, run, "log", "-1", "--format=%s").strip() == "Refine draft: Draft SPEC-demo"
    assert SPEC in git_in(git, run, "show", "--name-only", "--format=", "HEAD").split()
