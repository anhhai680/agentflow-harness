"""The design lane (Task 11): grill -> answers -> apply with doc checks -> commit on its own branch -> draft PR.

The idea doc here is `tests/fixtures/idea.md`, copied to the configured `project.docs.idea` path; the real
idea doc is never read.
"""

from __future__ import annotations

import json
import os
from itertools import pairwise
from pathlib import Path

import pytest
from agentflow.flow import main
from agentflow.gates import GateResult, run_doc_checks
from agentflow.hitl import approve, parse_answers
from agentflow.pipeline import RunOptions, advance, start
from agentflow.state import Run
from conftest import ENGINE, run_git

FIXTURE = ENGINE / "tests" / "fixtures" / "idea.md"
GH_BIN = ENGINE / "tests" / "bin"
IDEA = "docs/idea.md"
DECISIONS = "docs/decisions"
TOPIC = "Ngưỡng làm rõ"
CHECKS = ["decisions_log_row", "open_question_removed", "adr_index_updated"]
DONE = {"status": "done", "summary": "applied the decision"}
QUESTION = {"id": "q1", "question": "Bao nhiêu vòng làm rõ trước khi chuyển người?",
            "options": [{"key": "A", "label": "Tối đa 2 vòng", "recommended": True}, {"key": "B", "label": "1 vòng"}],
            "evidence": [], "kind": "spec_gap"}
QUESTION_LINE = "- Ngưỡng làm rõ: how many clarifying rounds before the agent escalates?\n"
LAST_ROW = "| CRM sandbox / rate limit | Sandbox API key available; defensive caching |\n"
NEW_ROW = "| Ngưỡng làm rõ: how many clarifying rounds? | Tối đa 2 vòng, rồi chuyển người |\n"
README = "# ADRs\n\n| ADR | Decision | Status |\n|---|---|---|\n| [ADR-001](ADR-001-langgraph.md) | LangGraph | Accepted |\n"


def idea_text() -> str:
    return FIXTURE.read_text(encoding="utf-8")


def replaced(text: str, old: str, new: str) -> str:
    assert old in text, f"fixture lost {old!r}"
    return text.replace(old, new, 1)


def with_row(text: str) -> str:
    return replaced(text, LAST_ROW, LAST_ROW + NEW_ROW)


def without_question(text: str) -> str:
    return replaced(text, QUESTION_LINE, "")


def decided() -> str:
    """The idea doc after a correct apply: a Decisions Log row added and the open question removed."""
    return without_question(with_row(idea_text()))


def write(root: Path, rel: str, text: str) -> None:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def head(root: Path) -> str:
    return run_git(root, "rev-parse", "HEAD").strip()


def refs(repo: Path) -> str:
    return run_git(repo, "for-each-ref", "--format=%(refname) %(objectname)")


def by_name(results: list[GateResult]) -> dict[str, GateResult]:
    return {r.name: r for r in results}


def pairs(argv: list[str]) -> set[tuple[str, str]]:
    return set(pairwise(argv))


@pytest.fixture
def design(flow_repo, tmp_path, monkeypatch):
    """`flow_repo` with the idea fixture and an ADR index committed on `next`, and a logging fake `gh` on PATH."""
    root = flow_repo.root
    write(root, IDEA, idea_text())
    write(root, f"{DECISIONS}/README.md", README)
    write(root, f"{DECISIONS}/ADR-001-langgraph.md", "# ADR-001: LangGraph\n\nStatus: Accepted\n")
    run_git(root, "add", "-A")
    run_git(root, "commit", "-q", "-m", "Add the design docs fixture")
    flow_repo.gh_log = tmp_path / "gh.log"
    monkeypatch.setenv("FAKE_GH_LOG", str(flow_repo.gh_log))
    monkeypatch.setenv("PATH", f"{GH_BIN}{os.pathsep}{os.environ['PATH']}")
    return flow_repo


def begin(design) -> Run:
    return start(design.cfg, design.root, "design", {"topic": TOPIC}, RunOptions())


def go(design, run: Run) -> str:
    return advance(run, design.cfg, RunOptions())


def calls_for(design, key: str) -> list[dict]:
    return [c for c in design.calls() if c["step"] == key]


def gate_rounds(design, run: Run, key: str) -> list[dict[str, bool]]:
    """The doc check results per round, in order (each round reports all three checks)."""
    results = [e for e in design.events(run, "gate_result") if e["key"] == key]
    rounds = [results[i:i + len(CHECKS)] for i in range(0, len(results), len(CHECKS))]
    return [{e["gate"]: e["passed"] for e in r} for r in rounds]


# --------------------------------------------------------------------------- the lane end to end


def test_design_flow(design, monkeypatch, git):
    origin_before = refs(design.origin)
    start_sha = head(design.root)
    design.script({
        "define/decide/grill_topic": [{"result": {"status": "done", "summary": "one decision", "questions": [QUESTION]}}],
        "define/decide/apply_topic": [{"write": {IDEA: decided()}, "result": DONE}],
    })
    run = begin(design)
    worktree = Path(run.state["worktree"])
    assert worktree != design.root and run.state["branch"].startswith("flow/design/")

    assert go(design, run) == "waiting_human"
    assert run.state["waiting"]["key"] == "define/decide/topic_answers"
    questions = run.questions_path()
    text = questions.read_text(encoding="utf-8")
    assert QUESTION["question"] in text and "Answer:" in text
    assert parse_answers(questions) == {"q1": ""}
    assert [c["step"] for c in design.calls()] == ["define/decide/grill_topic"]

    prompts: list[str] = []
    monkeypatch.setattr("builtins.input", lambda prompt="": prompts.append(prompt) or "y")
    assert main(["answer", run.id, "--set", "q1=A — tối đa 2 vòng", "--by", "tester"]) == 0
    assert prompts == []  # the answers feed the apply step of the same phase: nothing to approve yet
    run = Run.load(design.root, run.id)
    assert run.state["waiting"]["key"] == "define_approved"
    assert parse_answers(questions) == {"q1": "A — tối đa 2 vòng"}

    (apply_call,) = calls_for(design, "define/decide/apply_topic")
    prompt = apply_call["argv"][-1]
    assert "Use the `domain-modeling` skill." in prompt
    copy = run.agent_dir / "questions.md"  # the apply step is given the workspace copy of the answered file
    assert str(copy) in prompt and copy.read_bytes() == questions.read_bytes()
    assert TOPIC in prompt
    assert gate_rounds(design, run, "define/decide/apply_topic") == [dict.fromkeys(CHECKS, True)]
    assert head(worktree) == start_sha  # the apply step does not commit; `ship` does

    assert main(["approve", run.id, "define", "--by", "tester"]) == 0
    run = Run.load(design.root, run.id)
    assert run.state["status"] == "done"
    assert git(worktree, "log", "-1", "--format=%s").strip() == f"Decide {TOPIC} in agentic RAG idea doc"
    assert git(worktree, "log", "-1", "--format=%B").strip().endswith(design.cfg.project.commit_trailer)
    assert git(worktree, "rev-list", "--count", f"{start_sha}..HEAD").strip() == "1"
    assert git(worktree, "show", "--name-only", "--format=", "HEAD").split() == [IDEA]
    assert git(worktree, "status", "--porcelain").strip() == ""
    assert (worktree / IDEA).read_text(encoding="utf-8") == decided()
    assert head(design.root) == start_sha  # the main checkout never moves
    assert (design.root / IDEA).read_text(encoding="utf-8") == idea_text()
    assert set(origin_before.splitlines()) < set(refs(design.origin).splitlines())  # one new ref, nothing moved
    assert run.state["branch"] in refs(design.origin)
    calls = [json.loads(line) for line in design.gh_log.read_text(encoding="utf-8").splitlines()]
    (create,) = [call for call in calls if call[:2] == ["pr", "create"]]
    assert "--draft" in create
    assert create[create.index("--head") + 1] == run.state["branch"]
    assert create[create.index("--base") + 1] == design.cfg.project.fork_from
    assert [e["phase"] for e in design.events(run, "phase_approval_raised")] == ["define"]
    assert [e["url"] for e in design.events(run, "pr_opened")] == [run.state["pr_url"]]


def test_single_human_stop_for_answers(design, monkeypatch):
    design.script({
        "define/decide/grill_topic": [{"result": {"status": "done", "summary": "one decision", "questions": [QUESTION]}}],
        "define/decide/apply_topic": [{"write": {IDEA: decided()}, "result": DONE}],
    })
    run = begin(design)
    assert go(design, run) == "waiting_human"
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")
    assert main(["answer", run.id, "--set", "q1=A", "--by", "tester"]) == 0
    run = Run.load(design.root, run.id)

    stops = design.events(run, "hitl_raised")
    assert [(e["kind"], e["key"]) for e in stops] == [("checkpoint", "define/decide/topic_answers")]
    assert design.events(run, "phase_approved") == []
    assert run.state["waiting"]["key"] == "define_approved"  # the one approval, after the doc is edited


# --------------------------------------------------------------------------- doc checks in the fix loop


def test_doc_checks_fail_then_fix(design):
    design.script({"define/decide/apply_topic": [
        {"write": {IDEA: without_question(idea_text())}, "result": DONE},  # forgot the Decisions Log row
        {"write": {IDEA: decided()}, "result": DONE},
    ]})
    run = begin(design)
    assert go(design, run) == "awaiting_approval"  # grill asked nothing, so its checkpoint cleared itself
    assert run.state["waiting"]["key"] == "define_approved"

    _, second = calls_for(design, "define/decide/apply_topic")
    (session,) = {e["session_id"] for e in design.events(run, "agent_session") if e["key"] == "define/decide/apply_topic"}
    assert ("--resume", session) in pairs(second["argv"])  # the same session is resumed with the failure
    fix_prompt = second["argv"][-1]
    assert "decisions_log_row" in fix_prompt and "## Decisions Log" in fix_prompt
    assert "open_question_removed" not in fix_prompt and "adr_index_updated" not in fix_prompt
    assert [(e["attempt"], e["gates"]) for e in design.events(run, "fix_attempt")] == [(1, ["decisions_log_row"])]
    assert gate_rounds(design, run, "define/decide/apply_topic") == [
        {"decisions_log_row": False, "open_question_removed": True, "adr_index_updated": True},
        dict.fromkeys(CHECKS, True),
    ]
    assert run.state["steps"]["define/decide/apply_topic"]["status"] == "done"
    assert design.events(run, "hitl_raised") == []


def test_doc_checks_still_failing_stop_for_a_human(design):
    design.configure({"defaults": {"fix_attempts": 1}})
    design.script({"define/decide/apply_topic": [
        {"write": {IDEA: with_row(idea_text())}, "result": DONE},  # never removes the open question
        {"result": DONE},
    ]})
    run = begin(design)
    assert go(design, run) == "waiting_human"
    (stop,) = design.events(run, "hitl_raised")
    assert stop["kind"] == "fix_attempts_exhausted" and stop["key"] == "define/decide/apply_topic"
    assert "open_question_removed" in stop["summary"] and "decisions_log_row" not in stop["summary"]
    assert len(calls_for(design, "define/decide/apply_topic")) == 2


# --------------------------------------------------------------------------- run_doc_checks itself


def test_adr_without_index_fails(design):
    base = head(design.root)
    write(design.root, f"{DECISIONS}/ADR-040-clarify-threshold.md", "# ADR-040: Clarify threshold\n")
    result = by_name(run_doc_checks(["adr_index_updated"], design.root, base, IDEA, DECISIONS))["adr_index_updated"]
    assert not result.passed and not result.skipped
    assert "ADR-040-clarify-threshold.md" in result.reason + result.tail
    assert f"{DECISIONS}/README.md" in result.tail

    write(design.root, f"{DECISIONS}/README.md", README + "| [ADR-040](ADR-040-clarify-threshold.md) | Clarify | Accepted |\n")
    result = by_name(run_doc_checks(["adr_index_updated"], design.root, base, IDEA, DECISIONS))["adr_index_updated"]
    assert result.passed


def test_adr_check_passes_vacuously_without_a_new_adr(design):
    base = head(design.root)
    write(design.root, f"{DECISIONS}/ADR-001-langgraph.md", "# ADR-001: LangGraph\n\nStatus: Superseded by ADR-040\n")
    result = by_name(run_doc_checks(["adr_index_updated"], design.root, base, IDEA, DECISIONS))["adr_index_updated"]
    assert result.passed and not result.skipped and "no new ADR" in result.reason


def test_doc_checks_see_committed_and_uncommitted_changes(design):
    root = design.root
    base = head(root)
    write(root, IDEA, with_row(idea_text()))
    run_git(root, "commit", "-q", "-am", "row")
    write(root, IDEA, decided())  # uncommitted on top
    results = by_name(run_doc_checks(CHECKS, root, base, IDEA, DECISIONS))
    assert [r.name for r in results.values()] == CHECKS
    assert all(r.passed for r in results.values())
    assert run_git(root, "status", "--porcelain").strip() == f"M {IDEA}"  # the real index was not touched


def test_doc_checks_only_count_their_own_section(design):
    root = design.root
    base = head(root)
    text = idea_text()
    text = replaced(text, "| Console | Staff |\n", "| Console | Staff |\n| Widget | Customers |\n")  # a row elsewhere
    text = replaced(text, LAST_ROW, LAST_ROW + "|---|---|\n")  # a separator is not a decision row
    text = replaced(text, "- **Phone support:** out of scope", "- **Phone:** out")  # Not Doing, not Open Questions
    text = replaced(text, "## Open Questions\n\n**MVP**", "## Open Questions\n**MVP**")  # a blank line is no question
    write(root, IDEA, text)
    results = by_name(run_doc_checks(CHECKS[:2], root, base, IDEA, DECISIONS))
    assert not results["decisions_log_row"].passed
    assert "## Decisions Log" in results["decisions_log_row"].tail
    assert not results["open_question_removed"].passed
    assert "## Open Questions" in results["open_question_removed"].tail


def test_doc_checks_fail_on_a_missing_section_or_doc(design):
    root = design.root
    base = head(root)
    write(root, IDEA, replaced(with_row(idea_text()), "## Decisions Log\n", "## Decisions\n"))
    result = by_name(run_doc_checks(["decisions_log_row"], root, base, IDEA, DECISIONS))["decisions_log_row"]
    assert not result.passed and "no `## Decisions Log` section" in result.reason

    write(root, IDEA, replaced(without_question(idea_text()), "## Open Questions\n", "## Later\n"))
    result = by_name(run_doc_checks(["open_question_removed"], root, base, IDEA, DECISIONS))["open_question_removed"]
    assert not result.passed and "no `## Open Questions` section" in result.reason and "work tree" in result.reason

    (root / IDEA).unlink()
    results = by_name(run_doc_checks(CHECKS[:2], root, base, IDEA, DECISIONS))
    assert not results["decisions_log_row"].passed and not results["open_question_removed"].passed


def test_unknown_doc_check(design, tmp_cfg):
    from agentflow.config import DOC_CHECK_NAMES, load_config, validate

    results = run_doc_checks(list(DOC_CHECK_NAMES), design.root, head(design.root), IDEA, DECISIONS)
    assert [r.name for r in results] == list(DOC_CHECK_NAMES)  # every name config accepts is a check
    with pytest.raises(ValueError, match="ghost"):
        run_doc_checks(["ghost"], design.root, head(design.root), IDEA, DECISIONS)

    def patch(data):
        decide = next(s for s in data["phases"]["define"]["steps"] if s["id"] == "decide")
        next(s for s in decide["steps"] if s["id"] == "apply_topic")["checks"] = ["decisions_log_row", "ghost"]

    path = tmp_cfg(patch, name="ghost.yaml")
    issues = validate(load_config(path), path.parent.parent)
    assert ("error", "phases.define.steps[2].steps[2].checks") in [(i.level, i.path) for i in issues if i.level == "error"]


def test_dry_run_lists_the_doc_checks(design):
    import io

    from agentflow.pipeline import dry_run

    out = io.StringIO()
    dry_run(design.cfg, design.root, "design", {"topic": TOPIC}, RunOptions(), out=out)
    text = out.getvalue()
    assert "define/decide/apply_topic" in text
    for name in CHECKS:
        assert f"doc check {name}" in text
    assert f"Decide {TOPIC} in agentic RAG idea doc" in text
    assert design.calls() == []
