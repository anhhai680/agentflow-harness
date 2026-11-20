"""inbox_view: each inbox item as a titled decision (dashboard slice U4)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from conftest import awaiting

from agentflow import hitl
from agentflow.config_trust import start_options
from agentflow.inbox_view import approve_note, inbox_view, item_title, split_cli


@pytest.mark.parametrize("item, title", [
    ({"kind": "phase_approval", "phase": "define", "key": "define_approved", "questions": []}, "Approve the spec"),
    ({"kind": "phase_approval", "phase": "verify", "key": "verify_approved", "questions": []}, "Approve verify"),
    ({"kind": "blocked", "key": "build/build/task-9/implement", "questions": []}, "Task 9 · implement is blocked"),
    ({"kind": "parked", "key": "build/build/task-4/implement", "questions": [{"id": "q1"}, {"id": "q2"}]},
     "Answer 2 questions (Task 4 · implement)"),
    ({"kind": "dirty_tree", "key": "ship/finish", "questions": []}, "ship · finish stopped (dirty_tree)"),
])
def test_items_have_titles_in_words(item, title):
    assert item_title(item) == title


@pytest.mark.parametrize("kind, questions, expected", [
    ("phase_approval", [], "none"), ("checkpoint", [], "none"), ("preflight_failed", [], "none"),
    ("blocked_repeat", [], "required"), ("blocked", [], "optional"), (hitl.ENVIRONMENT, [], "none"), ("needs_human", [{"id": "q"}], "none")])
def test_the_note_follows_what_hitl_approve_accepts(kind, questions, expected):  # Review Focus 1
    assert approve_note({"kind": kind, "questions": questions}) == expected


def test_cli_lines_are_split_off():
    body, cli = split_cli("# r: k\n\nfloor_guard fails.\n\nRun `agentflow approve r k --note \"…\"` to go on.\n\nMore.")
    assert body == "# r: k\n\nfloor_guard fails.\n\nMore." and cli == ["Run `agentflow approve r k --note \"…\"` to go on."]


def test_a_paragraph_with_guidance_stays_in_the_text_and_is_listed_as_cli():
    how = ("Revert or fix the files yourself, then `agentflow approve r build/x` restarts the step in a fresh session "
           "(the runner checks the tree again first); `agentflow reject r build/x --note \"...\"` sends the phase back.")
    body, cli = split_cli(f"# r: x\n\nfloor_guard fails.\n\n{how}\n\nRun `agentflow retry r x` now.")
    assert how in body and "Run `agentflow retry r x` now." not in body
    assert cli == [how, "Run `agentflow retry r x` now."]


def test_a_parked_question_and_a_wait_are_two_cards(flow_repo):  # Review Focus 2
    run = flow_repo.new_run()
    run.emit("phase_started", phase="build")
    run.emit("step_parked", key="build/build/task-4/implement", summary="which db?", questions=[
        {"id": "q1", "question": "Which db?", "options": [{"key": "A", "label": "pg", "recommended": True}],
         "evidence": [], "kind": "choice"}])
    run.emit("hitl_raised", kind="blocked", key="build/build/task-9/implement", summary="floor_guard")
    items = inbox_view(flow_repo.root, flow_repo.cfg, datetime.now(UTC))["items"]
    assert sorted((i["kind"], i["actions"]) for i in items) == [("blocked", ["approve", "reject"]),
                                                               ("parked", ["answer"])]


def test_a_decision_made_elsewhere_is_listed_as_recent(flow_repo):  # Review Focus 3
    run = awaiting(flow_repo, phase="define")
    hitl.approve(run, "define", by="alex")
    (recent,) = inbox_view(flow_repo.root, flow_repo.cfg, datetime.now(UTC))["recent"]
    assert (recent["phase"], recent["action"], recent["by"]) == ("define", "approved", "alex")


def test_a_missing_inbox_file_keeps_the_summary_and_a_huge_one_is_cut(flow_repo):  # Review Focus 5
    run = flow_repo.new_run()
    run.emit("phase_started", phase="build")
    run.emit("hitl_raised", kind="blocked", key="build/x", summary="why")
    (item,) = inbox_view(flow_repo.root, flow_repo.cfg, datetime.now(UTC))["items"]
    assert item["summary"] == "why" and item["text"] == "" and item["cli"] == []
    run.inbox_path("build/x").parent.mkdir(parents=True, exist_ok=True)
    run.inbox_path("build/x").write_text("x\n" * 400_000, encoding="utf-8")
    flow_repo.cfg.dashboard["artifact_max_kb"] = 16
    (item,) = inbox_view(flow_repo.root, flow_repo.cfg, datetime.now(UTC))["items"]
    assert item["text_truncated"] is True and len(item["text"].encode()) <= 16 * 1024


def test_a_stop_in_a_plan_task_carries_its_blast_radius(building, flow_repo):  # U2's `building` fixture
    (item,) = inbox_view(building.repo, flow_repo.cfg, datetime.now(UTC))["items"]
    assert item["blast_radius"]["task"] == 2 and item["blast_radius"]["blocks"] == [3, 4]
    assert item["blast_radius"]["independent"] == [5]


def test_a_phase_approval_carries_its_artifacts(spec_run, flow_repo):  # U1's `spec_run`
    spec_run.run.emit("phase_approval_raised", phase="plan", round=0, summary="plan is done")
    (item,) = inbox_view(spec_run.repo, flow_repo.cfg, datetime.now(UTC))["items"]
    assert [(a["path"], a["kind"]) for a in item["artifacts"]] == [("docs/plans/x.md", "plan")]


def test_held_and_stalled_runs_are_watched_without_buttons(flow_repo):
    run = flow_repo.new_run()
    run.emit("phase_started", phase="build")
    run.emit("run_held", key="build/x", reason="usage limit", until="2026-11-16T18:00:00+00:00")
    (row,) = inbox_view(flow_repo.root, flow_repo.cfg, datetime.now(UTC))["watch"]
    assert row["reason"] == "held" and "usage limit" in row["text"]


@pytest.fixture(autouse=True)
def _a_persons_terminal(monkeypatch):
    """The cards are drawn for a person, even when an agent session runs the suite."""
    monkeypatch.delenv("AGENTFLOW_STEP", raising=False)
    monkeypatch.delenv("AGENTFLOW_RUN", raising=False)


OVERRIDE = {"guardrails": {"constraint_override": {"trailer": "Constraints-Approved-By", "gates": ["floor"]}}}
TASK = "build/build/task-2/commit"


def parked_card(flow_repo, kind, questions=(), key="build/build/task-4/implement"):
    run = flow_repo.new_run()
    run.emit("phase_started", phase="build")
    run.emit("step_parked", key=key, kind=kind, summary="stopped", questions=list(questions))
    (item,) = [i for i in inbox_view(flow_repo.root, flow_repo.cfg, datetime.now(UTC))["items"] if i["kind"] == "parked"]
    return item


def test_a_parked_stop_card_offers_approve(flow_repo):
    item = parked_card(flow_repo, "blocked")
    assert item["actions"] == ["approve"] and item["approve_note"] == "optional" and item["stop_kind"] == "blocked"


def test_a_parked_stop_note_follows_its_stop_kind(flow_repo):
    assert parked_card(flow_repo, "blocked_repeat")["approve_note"] == "required"


def test_a_parked_stop_with_questions_offers_answer_only(flow_repo):
    question = {"id": "q1", "question": "Which db?", "options": [{"key": "A", "label": "pg", "recommended": True}],
                "evidence": [], "kind": "choice"}
    item = parked_card(flow_repo, "needs_human", [question])
    assert item["actions"] == ["answer"] and item["approve_note"] == "none"


def test_approve_note_reads_the_stop_kind_of_a_parked_item():
    assert approve_note({"kind": "parked", "stop_kind": "blocked_repeat", "questions": []}) == "required"
    assert approve_note({"kind": "parked", "stop_kind": "preflight_failed", "questions": []}) == "none"
    assert approve_note({"kind": "parked", "stop_kind": "blocked", "questions": []}) == "optional"
    assert approve_note({"kind": "parked", "questions": []}) == "optional"


def constraint_run(flow_repo, parked: bool, passed: bool = False):
    """A run started on a config whose `floor` gate is a constraint gate, with `floor`'s result on a task and the stop."""
    cfg = flow_repo.configure(OVERRIDE)
    run = flow_repo.new_run()
    run.emit("run_options", **start_options(cfg.path))
    run.emit("phase_started", phase="build")
    run.emit("gate_result", key=TASK, gate="floor", passed=passed, skipped=False, reason="", log=None, duration_s=0.1,
             tree="t")
    if parked:
        run.emit("step_parked", key=TASK, kind="gate_failed", summary="Gates failed: floor.", questions=[])
    else:
        hitl.raise_hitl(cfg, run, "gate_failed", TASK, "Gates failed: floor.", notify=False)
    return run


@pytest.mark.parametrize("parked", [False, True])
def test_a_failed_constraint_gate_offers_the_constraint_button(flow_repo, parked):
    server_cfg = flow_repo.cfg  # the dashboard's config: it names no constraint gate, the run's own config does
    constraint_run(flow_repo, parked)
    (item,) = inbox_view(flow_repo.root, server_cfg, datetime.now(UTC))["items"]
    assert "approve_constraint" in item["actions"] and "approve" in item["actions"]


def test_a_passing_constraint_gate_offers_no_constraint_button(flow_repo):
    constraint_run(flow_repo, parked=False, passed=True)
    (item,) = inbox_view(flow_repo.root, flow_repo.cfg, datetime.now(UTC))["items"]
    assert "approve_constraint" not in item["actions"]


def test_a_run_whose_config_cannot_be_loaded_offers_no_constraint_button(flow_repo):
    run = constraint_run(flow_repo, parked=False)
    run.emit("run_options", config="/nowhere/else.yaml")  # a later config the run never started with: untrusted
    (item,) = inbox_view(flow_repo.root, flow_repo.cfg, datetime.now(UTC))["items"]
    assert "approve_constraint" not in item["actions"]
