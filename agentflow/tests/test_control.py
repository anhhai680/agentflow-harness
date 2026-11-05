"""Step control flow (Task 7): when/unless, on_unknown, repeat, next, sub-lanes, checkpoints, gate and
commit steps, and what `approve` does to a stop the runner raised."""

from __future__ import annotations

import pytest

from agentflow.config import Phase, Step
from agentflow.hitl import NotWaiting, answer, approve, parse_answers, write_answers

DONE = {"status": "done", "summary": "did it"}
NOSTATUS_SPEC = "# Spec: `crm`\n\nNo status line here.\n"
CONDS = {
    "conditions": {
        "yes": {"check": "command", "run": "true"},
        "no": {"check": "command", "run": "false"},
    }
}
QUESTION = {
    "id": "q1",
    "question": "Lưu bộ nhớ đệm token MISA?",
    "options": [{"key": "A", "label": "Theo tiến trình", "recommended": True}, {"key": "B", "label": "Mỗi lượt"}],
    "evidence": ["docs/specs/SPEC-crm.md#M5"],
    "kind": "spec_gap",
}


def agent(step_id: str, **kw) -> Step:
    fields = {"id": step_id, "type": "agent", "skill": "test-driven-development", "role": "build", "prompt": "step.md"}
    return Step(**(fields | kw))


def phase_of(steps: list[Step], **kw) -> Phase:
    fields = {"name": "build", "agent": None, "model": "sonnet", "skip_if": None, "steps": steps}
    return Phase(**(fields | kw))


def pairs(argv: list[str]) -> set[tuple[str, str]]:
    return set(zip(argv, argv[1:]))


def called_steps(flow_repo) -> list[str]:
    return [c["step"] for c in flow_repo.calls()]


def run_steps(flow_repo, steps: list[Step], run=None):
    executor = flow_repo.executor(run)
    return executor.run_steps(phase_of(steps), steps, "build")


# --------------------------------------------------------------------------- when / unless


def test_when_skip_logged(flow_repo):
    flow_repo.configure(CONDS)
    run = flow_repo.new_run()
    steps = [agent("a", when="no"), agent("b", unless="no")]
    assert run_steps(flow_repo, steps) == ("done", True)
    (skipped,) = flow_repo.events(run, "step_skipped")
    assert skipped["key"] == "build/a" and skipped["condition"] == "no"
    assert "no = false" in skipped["reason"] and "exit 1" in skipped["reason"]
    assert run.state["steps"]["build/a"]["status"] == "skipped"
    assert run.state["steps"]["build/a"]["skip_reason"] == skipped["reason"]
    assert called_steps(flow_repo) == ["build/b"]


def test_all_steps_skipped_reports_nothing_ran(flow_repo):
    flow_repo.configure(CONDS)
    run = flow_repo.new_run()
    assert run_steps(flow_repo, [agent("a", when="no"), agent("b", unless="yes")]) == ("done", False)
    assert [e["key"] for e in flow_repo.events(run, "step_skipped")] == ["build/a", "build/b"]
    assert flow_repo.calls() == []


def test_unknown_condition_asks_human(flow_repo):
    run = flow_repo.new_run()
    (flow_repo.root / "docs" / "specs").mkdir(parents=True)
    (flow_repo.root / "docs" / "specs" / "SPEC-crm.md").write_text(NOSTATUS_SPEC, encoding="utf-8")
    steps = [agent("x", when="spec_is_approved")]
    assert run_steps(flow_repo, steps) == ("stopped", False)
    (raised,) = flow_repo.events(run, "hitl_raised")
    assert raised["kind"] == "condition" and raised["key"] == "build/x.when"
    assert "Treat as approved?" in raised["questions"][0]["question"]
    assert "no status line" in raised["summary"]
    assert run.state["status"] == "waiting_human" and flow_repo.calls() == []

    assert run_steps(flow_repo, steps) == ("stopped", False)  # still waiting: nothing is raised twice
    assert len(flow_repo.events(run, "hitl_raised")) == 1

    assert answer(run, "build/x.when", {raised["questions"][0]["id"]: "yes"}, by="me") is True
    assert run_steps(flow_repo, steps) == ("done", True)
    assert called_steps(flow_repo) == ["build/x"]
    (decided,) = flow_repo.events(run, "condition_decided")
    assert decided["value"] is True and decided["decided_by"] == "human" and decided["condition"] == "spec_is_approved"


def test_unknown_condition_answered_no_skips(flow_repo):
    run = flow_repo.new_run()
    (flow_repo.root / "docs" / "specs").mkdir(parents=True)
    (flow_repo.root / "docs" / "specs" / "SPEC-crm.md").write_text(NOSTATUS_SPEC, encoding="utf-8")
    steps = [agent("x", when="spec_is_approved")]
    assert run_steps(flow_repo, steps)[0] == "stopped"
    qid = run.state["waiting"]["questions"][0]["id"]
    answer(run, "build/x.when", {qid: "no"}, by="me")
    assert run_steps(flow_repo, steps) == ("done", False)
    assert run.state["steps"]["build/x"]["status"] == "skipped" and flow_repo.calls() == []


def test_unknown_condition_agent(flow_repo):
    flow_repo.configure({"conditions": {"spec_is_approved": {"on_unknown": "agent"}}})
    run = flow_repo.new_run()
    (flow_repo.root / "docs" / "specs").mkdir(parents=True)
    (flow_repo.root / "docs" / "specs" / "SPEC-crm.md").write_text(NOSTATUS_SPEC, encoding="utf-8")
    flow_repo.script({"build/x.when": [{"result": {"status": "done", "summary": "the spec says approved in prose",
                                                   "signals": {"answer": "yes"}}}]})
    steps = [agent("x", when="spec_is_approved")]
    assert run_steps(flow_repo, steps) == ("done", True)
    question, work = flow_repo.calls()
    assert question["step"] == "build/x.when"
    assert ("--model", "haiku") in pairs(question["argv"])
    assert "Answer one yes/no question" in question["argv"][-1]
    assert "spec_is_approved" in question["argv"][-1] and "no status line" in question["argv"][-1]
    assert work["step"] == "build/x" and ("--model", "sonnet") in pairs(work["argv"])
    (decided,) = flow_repo.events(run, "condition_decided")
    assert decided["value"] is True and decided["decided_by"] == "agent"
    assert flow_repo.events(run, "hitl_raised") == []


def test_unknown_condition_agent_unsure_falls_back_to_human(flow_repo):
    flow_repo.configure({"conditions": {"spec_is_approved": {"on_unknown": "agent"}}})
    run = flow_repo.new_run()
    (flow_repo.root / "docs" / "specs").mkdir(parents=True)
    (flow_repo.root / "docs" / "specs" / "SPEC-crm.md").write_text(NOSTATUS_SPEC, encoding="utf-8")
    flow_repo.script({"build/x.when": [{"result": {"status": "done", "summary": "cannot tell",
                                                   "signals": {"answer": "unknown"}}}]})
    assert run_steps(flow_repo, [agent("x", when="spec_is_approved")]) == ("stopped", False)
    assert [e["kind"] for e in flow_repo.events(run, "hitl_raised")] == ["condition"]
    assert called_steps(flow_repo) == ["build/x.when"]


# --------------------------------------------------------------------------- repeat / next / sub-lanes


def test_repeat_until_then_hitl(flow_repo):
    flow_repo.configure(CONDS)
    run = flow_repo.new_run()
    group = Step(id="g", type="group", repeat={"until": "no", "max_rounds": 2}, steps=[agent("a"), agent("b")])
    assert run_steps(flow_repo, [group]) == ("stopped", True)
    assert called_steps(flow_repo) == ["build/g/r1/a", "build/g/r1/b", "build/g/r2/a", "build/g/r2/b"]
    (raised,) = flow_repo.events(run, "hitl_raised")
    assert raised["kind"] == "max_rounds" and raised["key"] == "build/g"
    assert "2 rounds" in raised["summary"]

    assert run_steps(flow_repo, [group]) == ("stopped", True)  # waiting: no third round, no second stop
    assert len(flow_repo.calls()) == 4 and len(flow_repo.events(run, "hitl_raised")) == 1

    approve(run, "build/g", by="me")
    assert flow_repo.events(run, "checkpoint_cleared")[-1]["key"] == "build/g"
    assert run_steps(flow_repo, [group, agent("after")]) == ("done", True)
    assert called_steps(flow_repo)[4:] == ["build/after"]


def test_repeat_stops_when_until_holds(flow_repo):
    flow_repo.configure(CONDS)
    run = flow_repo.new_run()
    group = Step(id="g", type="group", repeat={"until": "yes", "max_rounds": 3}, steps=[agent("a")])
    assert run_steps(flow_repo, [group]) == ("done", True)
    assert called_steps(flow_repo) == ["build/g/r1/a"]
    assert flow_repo.events(run, "hitl_raised") == []


def test_next_goto_skips(flow_repo):
    flow_repo.configure(CONDS)
    run = flow_repo.new_run()
    steps = [agent("a", next=[{"when": "no", "goto": "b"}, {"when": "yes", "goto": "c"}]), agent("b"), agent("c")]
    assert run_steps(flow_repo, steps) == ("done", True)
    assert called_steps(flow_repo) == ["build/a", "build/c"]
    assert not [e for e in flow_repo.events(run, "step_started") if e["key"] == "build/b"]
    (skipped,) = flow_repo.events(run, "step_skipped")
    assert skipped["key"] == "build/b" and "goto c" in skipped["reason"]
    assert flow_repo.events(run, "step_branched")[0]["goto"] == "c"


def test_next_goto_backwards_is_refused(flow_repo):
    flow_repo.configure(CONDS)
    flow_repo.new_run()
    steps = [agent("a"), agent("b", next=[{"goto": "a"}])]
    with pytest.raises(ValueError, match="later step"):
        run_steps(flow_repo, steps)


def test_group_with_every_step_skipped_did_not_run(flow_repo):
    flow_repo.configure(CONDS)
    run = flow_repo.new_run()
    group = Step(id="g", type="group", steps=[agent("a", when="no"), agent("b", unless="yes")])
    rounds = Step(id="r", type="group", steps=[agent("c", when="no")], repeat={"until": "yes", "max_rounds": 2})
    assert run_steps(flow_repo, [group, rounds]) == ("done", False)
    for key in ("build/g", "build/r"):
        assert run.state["steps"][key]["status"] == "skipped"
        assert run.state["steps"][key]["skip_reason"] == "all steps skipped"
    assert run_steps(flow_repo, [group, rounds]) == ("done", False)  # a resumed run agrees
    assert flow_repo.calls() == []
    assert run_steps(flow_repo, [group, agent("after")]) == ("done", True)


# --------------------------------------------------------------------------- checkpoints


def grill_steps() -> list[Step]:
    return [
        agent("grill", skill="grilling", role="plan", output="runs/{run}/questions.md"),
        Step(id="answers", type="checkpoint", wait_for="answered_file"),
        agent("apply", skill="domain-modeling", role="plan"),
    ]


def test_checkpoint_answered_file(flow_repo):
    run = flow_repo.new_run()
    flow_repo.script({"build/grill": [{"result": {"status": "done", "summary": "asked", "questions": [QUESTION]}}]})
    steps = grill_steps()
    assert run_steps(flow_repo, steps) == ("stopped", True)
    assert called_steps(flow_repo) == ["build/grill"]
    questions = run.questions_path()
    assert parse_answers(questions) == {"q1": ""}
    assert QUESTION["question"] in questions.read_text(encoding="utf-8")
    (raised,) = flow_repo.events(run, "hitl_raised")
    assert raised["kind"] == "checkpoint" and raised["key"] == "build/answers"
    assert raised["questions"][0]["id"] == "q1"

    assert run_steps(flow_repo, steps) == ("stopped", True)  # unanswered: still stopped, no new call
    assert len(flow_repo.calls()) == 1

    write_answers(questions, {"q1": "A — theo tiến trình"})  # the human edits questions.md
    assert run_steps(flow_repo, steps) == ("done", True)
    (cleared,) = flow_repo.events(run, "checkpoint_cleared")
    assert cleared["key"] == "build/answers" and cleared["answers"] == {"q1": "A — theo tiến trình"}
    assert called_steps(flow_repo) == ["build/grill", "build/apply"]
    assert run.state["waiting"] is None and run.state["steps"]["build/answers"]["status"] == "done"


def test_checkpoint_answered_with_flow_answer_fills_questions_file(flow_repo):
    run = flow_repo.new_run()
    flow_repo.script({"build/grill": [{"result": {"status": "done", "summary": "asked", "questions": [QUESTION]}}]})
    steps = grill_steps()
    assert run_steps(flow_repo, steps)[0] == "stopped"
    assert answer(run, "build/answers", {"q1": "B"}, by="me") is True
    assert run_steps(flow_repo, steps) == ("done", True)
    assert parse_answers(run.questions_path()) == {"q1": "B"}  # the apply step reads questions.md
    assert called_steps(flow_repo) == ["build/grill", "build/apply"]


def test_checkpoint_without_questions_clears_itself(flow_repo):
    run = flow_repo.new_run()
    assert run_steps(flow_repo, grill_steps()) == ("done", True)
    (cleared,) = flow_repo.events(run, "checkpoint_cleared")
    assert cleared["key"] == "build/answers" and "no questions" in cleared["reason"]
    assert flow_repo.events(run, "hitl_raised") == []


def test_plain_checkpoint_waits_for_approve(flow_repo):
    run = flow_repo.new_run()
    steps = [Step(id="doubt_offer", type="checkpoint", name="doubt_offer"), agent("after")]
    assert run_steps(flow_repo, steps) == ("stopped", True)
    assert flow_repo.events(run, "hitl_raised")[0]["kind"] == "checkpoint"
    inbox = run.inbox_path("build/doubt_offer").read_text(encoding="utf-8")
    assert f"agentflow approve {run.id} build/doubt_offer" in inbox
    approve(run, "build/doubt_offer", by="me")
    assert run_steps(flow_repo, steps) == ("done", True)
    assert called_steps(flow_repo) == ["build/after"]


# --------------------------------------------------------------------------- gate and commit steps


def test_gate_step_passes_and_fails(flow_repo):
    run = flow_repo.new_run()
    assert run_steps(flow_repo, [Step(id="g", type="gate", gates=["ok", "absent"])]) == ("done", True)
    gates = {e["gate"]: e for e in flow_repo.events(run, "gate_result")}
    assert gates["ok"]["passed"] is True and gates["absent"]["skipped"] is True
    assert run.state["steps"]["build/g"]["status"] == "done"

    bad = Step(id="h", type="gate", gates=["fail"])
    assert run_steps(flow_repo, [bad]) == ("stopped", True)
    raised = flow_repo.events(run, "hitl_raised")[-1]
    assert raised["kind"] == "gate_failed" and raised["key"] == "build/h"
    assert "boom {module} line" in run.inbox_path("build/h").read_text(encoding="utf-8")
    approve(run, "build/h", by="me")  # runs the gates again
    assert run_steps(flow_repo, [bad]) == ("stopped", True)
    assert len([e for e in flow_repo.events(run, "gate_result") if e["gate"] == "fail"]) == 2


def test_commit_step(flow_repo, git):
    run = flow_repo.new_run()
    (flow_repo.root / "docs").mkdir()
    (flow_repo.root / "docs" / "idea.md").write_text("decided\n", encoding="utf-8")
    step = Step(id="commit", type="commit", message="Decide {module} in agentic RAG idea doc")
    assert run_steps(flow_repo, [step]) == ("done", True)
    subject = git(flow_repo.root, "log", "-1", "--format=%s").strip()
    assert subject == "Decide crm in agentic RAG idea doc"
    assert flow_repo.events(run, "step_committed")[0]["sha"] == flow_repo.head()
    assert run_steps(flow_repo, [step]) == ("done", True)  # done steps never commit twice
    assert len(flow_repo.events(run, "step_committed")) == 1


# --------------------------------------------------------------------------- approving a runner-raised stop


def test_approve_resumes_a_runner_stop(flow_repo):
    run = flow_repo.new_run()
    step = agent("impl", gates=["fail"], fix_loop=True)
    flow_repo.configure({"defaults": {"fix_attempts": 1}})
    assert run_steps(flow_repo, [step]) == ("stopped", True)
    assert flow_repo.events(run, "hitl_raised")[-1]["kind"] == "fix_attempts_exhausted"
    inbox = run.inbox_path("build/impl").read_text(encoding="utf-8")
    assert "resumes the step" in inbox

    approve(run, "build/impl", by="me")
    assert run.state["steps"]["build/impl"]["status"] == "running"  # not done: nothing verified or committed
    assert flow_repo.events(run, "hitl_approved")[-1]["kind"] == "fix_attempts_exhausted"
    assert not flow_repo.events(run, "checkpoint_cleared")
    session = flow_repo.events(run, "agent_session")[0]["session_id"]
    assert run_steps(flow_repo, [step]) == ("stopped", True)
    resumed = flow_repo.calls()[2]
    assert ("--resume", session) in pairs(resumed["argv"])
    assert "human" in resumed["argv"][-1] and "fix_attempts_exhausted" in resumed["argv"][-1]
    assert len(flow_repo.events(run, "fix_attempt")) == 2  # the approval gave it a fresh fix attempt
    assert not [e for e in flow_repo.events(run, "step_finished") if e.get("status") == "done"]


def test_plain_approve_goes_past_a_budget_stop(flow_repo):
    flow_repo.configure({"defaults": {"run_budget_usd": 0.015}})
    run = flow_repo.new_run()
    flow_repo.script({"build/impl": [{"result_raw": "not json", "cost": 0.02}]})
    assert run_steps(flow_repo, [agent("impl")]) == ("stopped", True)
    assert flow_repo.events(run, "hitl_raised")[-1]["kind"] == "budget_exceeded"
    assert f"`agentflow approve {run.id} build/impl`" in run.inbox_path("build/impl").read_text(encoding="utf-8")
    approve(run, "build/impl", by="me")
    assert run.state["waiting"] is None
    assert run.state["budget_usd"] == pytest.approx(0.035)  # spent 0.02, plus one more budget of 0.015
    assert run_steps(flow_repo, [agent("impl")]) == ("done", True)


def test_budget_stop_asks_again_after_another_budget_is_spent(flow_repo):
    flow_repo.configure({"defaults": {"run_budget_usd": 0.015}})
    run = flow_repo.new_run()
    flow_repo.script({"build/impl": [{"result_raw": "not json", "cost": 0.02}, {"result_raw": "not json", "cost": 0.02}]})
    assert run_steps(flow_repo, [agent("impl")]) == ("stopped", True)
    approve(run, "build/impl", by="me")
    assert run_steps(flow_repo, [agent("impl")]) == ("stopped", True)
    assert [e["kind"] for e in flow_repo.events(run, "hitl_raised")] == ["budget_exceeded", "budget_exceeded"]


def test_explicit_budget_on_a_budget_stop_sets_the_limit(flow_repo):
    flow_repo.configure({"defaults": {"run_budget_usd": 0.015}})
    run = flow_repo.new_run()
    flow_repo.script({"build/impl": [{"result_raw": "not json", "cost": 0.02}]})
    assert run_steps(flow_repo, [agent("impl")]) == ("stopped", True)
    with pytest.raises(ValueError, match="budget"):
        approve(run, "build/impl", by="me", budget=0.01)
    assert run.state["waiting"] is not None
    approve(run, "build/impl", by="me", budget=5)
    assert run.state["budget_usd"] == 5 and run.state["waiting"] is None
    assert run_steps(flow_repo, [agent("impl")]) == ("done", True)


def test_approve_still_refuses_questions(flow_repo):
    run = flow_repo.new_run()
    flow_repo.script({"build/impl": [{"result": {"status": "needs_human", "summary": "?", "questions": [QUESTION]}}]})
    assert run_steps(flow_repo, [agent("impl")]) == ("stopped", True)
    with pytest.raises(NotWaiting, match="answer"):
        approve(run, "build/impl", by="me")


# --------------------------------------------------------------------------- context window from state


def test_context_window_from_state_drives_rollover(flow_repo):
    run = flow_repo.new_run()
    run.emit("context_window", model="sonnet", tokens=100000)  # cached under the alias
    flow_repo.script({"build/impl": [{"usage_pct": [45]}, {"result": DONE}]})
    assert run_steps(flow_repo, [agent("impl")]) == ("done", True)
    assert len(flow_repo.events(run, "rolled_over")) == 1  # 90000 of 100000 tokens crosses 80 %


def test_context_window_cached_by_stream_id_and_alias(flow_repo):
    run = flow_repo.new_run()
    assert run_steps(flow_repo, [agent("impl")]) == ("done", True)
    assert run.state["context_windows"]["fake-sonnet"] == 200000
    assert run.state["context_windows"]["sonnet"] == 200000
    run.emit("context_window", model="fake-sonnet", tokens=100000)
    executor = flow_repo.executor(run)
    assert executor.context_window_for("build/impl", "sonnet") == 100000  # its record names the stream model
    assert executor.context_window_for("build/other", "sonnet") == 200000  # a new step: by alias
    assert executor.context_window_for("build/other", "opus") is None
