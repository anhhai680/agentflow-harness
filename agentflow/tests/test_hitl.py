from __future__ import annotations

import dataclasses
import json
import subprocess
from pathlib import Path

import pytest

from agentflow import hitl
from agentflow.flow import main
from agentflow.hitl import (
    FACT_MAX,
    FACTS_PER_RESULT,
    NotWaiting,
    Option,
    Question,
    ResultError,
    answer,
    approve,
    inbox_items,
    notify,
    parse_answers,
    raise_hitl,
    read_result,
    reject,
    render_inbox,
)
from agentflow.state import Run

QUESTION = "Có lưu token CRM giữa các hội thoại không?"
ANSWER = "Không, mỗi hội thoại xác thực lại"


def write_result(path: Path, body) -> Path:
    path.write_text(body if isinstance(body, str) else json.dumps(body, ensure_ascii=False), encoding="utf-8")
    return path


def fill(path: Path, answers: dict[str, str]) -> None:
    """What a human does: type after the `Answer:` of each question."""
    text = path.read_text(encoding="utf-8")
    for qid, value in answers.items():
        head = text.index(f"### {qid} — ")
        at = text.index("Answer:", head)
        end = text.index("\n", at) if "\n" in text[at:] else len(text)
        text = text[:at] + f"Answer: {value}" + text[end:]
    path.write_text(text, encoding="utf-8")


def one_question(qid: str = "q1") -> Question:
    return Question(qid, QUESTION, [Option("A", "Theo tiến trình", True), Option("B", "Mỗi hội thoại")],
                    ["docs/specs/SPEC-crm.md#M5"], "spec_gap")


REAL_RUN = subprocess.run


def patch_osascript(monkeypatch, behave):
    """Replace `subprocess.run` for `osascript` only; git and other commands still run for real."""

    def fake_run(argv, **kwargs):
        if argv[0] == "osascript":
            return behave(argv)
        return REAL_RUN(argv, **kwargs)

    monkeypatch.setattr(hitl.subprocess, "run", fake_run)


@pytest.fixture
def macos_calls(monkeypatch):
    calls: list[list[str]] = []

    def behave(argv):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, "", "")

    patch_osascript(monkeypatch, behave)
    return calls


# ------------------------------------------------------------------ result schema


def test_result_facts_are_cleaned_cut_and_capped(tmp_path):
    p = tmp_path / "r.json"
    assert read_result(write_result(p, {"status": "done", "summary": "x"})).facts == []
    raw = ["  the shell sets a URL\n\tto a dead host\x00\x1b[31m  ", "e\u0301 is one letter", "x" * 500, "", "   "]
    facts = read_result(write_result(p, {"status": "done", "summary": "x", "facts": raw})).facts
    assert facts[0] == "the shell sets a URL to a dead host[31m"  # whitespace collapsed, control characters gone
    assert facts[1] == "\u00e9 is one letter"  # NFC
    assert facts[2] == "x" * FACT_MAX
    many = [f"fact {n}" for n in range(9)]
    assert read_result(write_result(p, {"status": "done", "summary": "x", "facts": many})).facts == many[:FACTS_PER_RESULT]


@pytest.mark.parametrize("facts", ["one fact", {"a": "b"}, [1], [["nested"]], [None]])
def test_result_facts_must_be_a_list_of_strings(tmp_path, facts):
    with pytest.raises(ResultError):
        read_result(write_result(tmp_path / "r.json", {"status": "done", "summary": "x", "facts": facts}))


def test_result_schema(tmp_path):
    p = tmp_path / "r.json"
    ok = read_result(write_result(p, {"status": "done", "summary": "wrote the spec", "next": "plan"}))
    assert (ok.status, ok.summary, ok.next) == ("done", "wrote the spec", "plan")
    assert ok.questions == [] and ok.signals == {} and ok.findings == [] and ok.risks == []
    assert ok.can_continue_without is False

    with pytest.raises(ResultError):
        read_result(write_result(p, {"status": "needs_human", "summary": "x"}))
    with pytest.raises(ResultError):
        read_result(write_result(p, {"status": "needs_human", "summary": "x", "questions": []}))
    with pytest.raises(ResultError):
        read_result(write_result(p, {"status": "maybe", "summary": "x"}))
    with pytest.raises(ResultError):
        read_result(write_result(p, {"status": "unchanged", "summary": "not mine"}))
    assert read_result(write_result(p, {"status": "unchanged", "summary": "not mine"}), refine=True).status == "unchanged"
    with pytest.raises(ResultError):
        read_result(write_result(p, {"status": "effort_change", "summary": "x"}))
    with pytest.raises(ResultError):
        read_result(write_result(p, {"status": "effort_change", "summary": "x", "effort": ""}))
    change = read_result(write_result(p, {"status": "effort_change", "summary": "x", "effort": "high", "reason": "big"}))
    assert (change.effort, change.reason) == ("high", "big")


def test_result_errors_for_missing_and_malformed_files(tmp_path):
    with pytest.raises(ResultError, match="not found"):
        read_result(tmp_path / "nope.json")
    with pytest.raises(ResultError, match="JSON"):
        read_result(write_result(tmp_path / "r.json", "{not json"))
    with pytest.raises(ResultError):
        read_result(write_result(tmp_path / "r.json", "[1, 2]"))
    with pytest.raises(ResultError):
        read_result(write_result(tmp_path / "r.json", {"status": "done", "summary": 5}))
    with pytest.raises(ResultError):
        read_result(write_result(tmp_path / "r.json", {"summary": "no status"}))


def test_result_reads_questions_and_extras(tmp_path):
    body = {
        "status": "needs_human", "summary": "need a call", "can_continue_without": True,
        "signals": {"answer": "yes"}, "findings": [{"severity": "high"}], "risks": ["r1"],
        "questions": [{"id": "q1", "question": QUESTION, "kind": "spec_gap", "evidence": ["a.md#1"],
                       "options": [{"key": "A", "label": "Theo tiến trình", "recommended": True}, {"key": "B", "label": "x"}]}],
    }
    res = read_result(write_result(tmp_path / "r.json", body))
    (q,) = res.questions
    assert q.id == "q1" and q.question == QUESTION and q.kind == "spec_gap" and q.evidence == ["a.md#1"]
    assert q.options == [Option("A", "Theo tiến trình", True), Option("B", "x", False)]
    assert res.can_continue_without is True and res.signals == {"answer": "yes"} and res.risks == ["r1"]


@pytest.mark.parametrize("bad", [
    {"question": "no id"},
    {"id": "q 1", "question": "space in id"},
    {"id": "q1", "question": ""},
    {"id": "q1", "question": "x", "options": "A"},
    {"id": "q1", "question": "x", "options": [{"label": "no key"}]},
    {"id": "q1", "question": "x", "evidence": "a"},
])
def test_result_rejects_malformed_questions(tmp_path, bad):
    with pytest.raises(ResultError):
        read_result(write_result(tmp_path / "r.json", {"status": "needs_human", "summary": "x", "questions": [bad]}))


def test_result_rejects_duplicate_question_ids(tmp_path):
    qs = [{"id": "q1", "question": "a"}, {"id": "q1", "question": "b"}]
    with pytest.raises(ResultError, match="duplicate"):
        read_result(write_result(tmp_path / "r.json", {"status": "needs_human", "summary": "x", "questions": qs}))


# ------------------------------------------------------------------ inbox


def test_inbox_roundtrip_vietnamese(tmp_path):
    p = tmp_path / "inbox" / "q.md"
    q = Question("q1", QUESTION, [Option("A", "Theo tiến trình", True)], [], "spec_gap")
    render_inbox(p, "t", "s", [q])
    assert parse_answers(p) == {"q1": ""}
    fill(p, {"q1": ANSWER})
    assert parse_answers(p) == {"q1": ANSWER}


def test_inbox_format(tmp_path):
    p = tmp_path / "q.md"
    render_inbox(p, "Run needs you", "Tóm tắt: cần quyết định", [one_question("q1"), one_question("q2")],
                 extra="Run `agentflow answer r1`.")
    text = p.read_text(encoding="utf-8")
    lines = text.splitlines()
    assert lines[0] == "# Run needs you"
    assert "Tóm tắt: cần quyết định" in text and "Run `agentflow answer r1`." in text
    assert f"### q1 — {QUESTION}" in lines and f"### q2 — {QUESTION}" in lines
    assert "- A: Theo tiến trình (recommended)" in lines and "- B: Mỗi hội thoại" in lines
    assert "- docs/specs/SPEC-crm.md#M5" in lines
    assert "Kind: spec_gap" in lines
    assert lines.count("Answer: ") == 2 or lines.count("Answer:") == 2
    assert list(parse_answers(p)) == ["q1", "q2"]


def test_inbox_multiline_and_nfd_answers(tmp_path):
    p = tmp_path / "q.md"
    render_inbox(p, "t", "s", [one_question("q1"), one_question("q2")])
    fill(p, {"q1": "dòng một\ndòng hai", "q2": "Có"})
    got = parse_answers(p)
    assert got == {"q1": "dòng một\ndòng hai", "q2": "Có"}


def test_inbox_title_and_question_stay_on_one_line(tmp_path):
    p = tmp_path / "q.md"
    render_inbox(p, "t", "s", [Question("q1", "line one\nline two", [], [], "other")])
    assert "### q1 — line one line two" in p.read_text(encoding="utf-8")


# ------------------------------------------------------------------ raise_hitl, notify


def test_raise_hitl_notifies(run, cfg, macos_calls, capsys):
    path = raise_hitl(cfg, run, "needs_human", "plan/task-1/implement", "Task 1 needs a TTL", [one_question()])
    assert path == run.inbox_path("plan/task-1/implement") and path.exists()
    assert "\a" in capsys.readouterr().err
    (argv,) = macos_calls
    assert argv[:2] == ["osascript", "-e"]
    assert f"agentflow: {run.id} needs you" in argv[2]
    assert argv[2].startswith("display notification")
    assert run.state["waiting"]["kind"] == "needs_human"
    assert run.state["waiting"]["key"] == "plan/task-1/implement"
    assert run.state["waiting"]["phase"] == "plan"
    assert run.state["waiting"]["summary"] == "Task 1 needs a TTL"
    assert run.state["waiting"]["questions"][0]["id"] == "q1"
    assert run.state["status"] == "waiting_human"
    assert parse_answers(path) == {"q1": ""}
    assert f"agentflow answer {run.id}" in path.read_text(encoding="utf-8")


def test_notify_failures_are_events_not_exceptions(run, cfg, monkeypatch, capsys):
    def boom(argv):
        raise FileNotFoundError("osascript")

    patch_osascript(monkeypatch, boom)
    notify(cfg, "t", "m", run=run)
    failures = [e for e in run.events.read() if e["type"] == "notify_failed"]
    assert [f["notifier"] for f in failures] == ["macos"]
    assert "osascript" in failures[0]["error"]
    assert "\a" in capsys.readouterr().err


def test_notify_nonzero_exit_is_an_event(run, cfg, monkeypatch):
    patch_osascript(monkeypatch, lambda argv: subprocess.CompletedProcess(argv, 1, "", "denied"))
    notify(cfg, "t", "m", run=run)
    (failure,) = [e for e in run.events.read() if e["type"] == "notify_failed"]
    assert "denied" in failure["error"]


def test_notify_escapes_applescript_quotes(cfg, macos_calls):
    notify(cfg, 'say "hi"', 'back\\slash "quoted"\nnewline')
    script = macos_calls[0][2]
    assert '\\"hi\\"' in script and "\\\\slash" in script and "\n" not in script


def test_notify_webhook_only_when_enabled(run, cfg, macos_calls, monkeypatch):
    posted: list[tuple[str, dict]] = []

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b""

    def fake_urlopen(req, timeout=0):
        posted.append((req.full_url, json.loads(req.data.decode("utf-8"))))
        return Resp()

    monkeypatch.setattr(hitl.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("AGENTFLOW_WEBHOOK_URL", "https://hooks.example.test/x")
    notify(cfg, "tiêu đề", "m", run=run)
    assert posted == []  # shipped config has the webhook disabled

    entries = [dict(e, enabled=True) if e["type"] == "webhook" else e for e in cfg.hitl["notify"]]
    enabled = dataclasses.replace(cfg, hitl={**cfg.hitl, "notify": entries})
    notify(enabled, "tiêu đề", "m", run=run)
    assert posted == [("https://hooks.example.test/x", {"title": "tiêu đề", "message": "m"})]

    monkeypatch.delenv("AGENTFLOW_WEBHOOK_URL")
    notify(enabled, "t", "m", run=run)
    (failure,) = [e for e in run.events.read() if e["type"] == "notify_failed"]
    assert failure["notifier"] == "webhook" and "AGENTFLOW_WEBHOOK_URL" in failure["error"]


def test_notify_unknown_type_is_an_event(run, cfg):
    odd = dataclasses.replace(cfg, hitl={"notify": [{"type": "pigeon"}]})
    notify(odd, "t", "m", run=run)
    (failure,) = [e for e in run.events.read() if e["type"] == "notify_failed"]
    assert failure["notifier"] == "pigeon"


def test_raise_hitl_for_a_runner_escalation_without_questions(run, cfg, macos_calls):
    path = raise_hitl(cfg, run, "fix_attempts_exhausted", "build/task-2/implement", "gates keep failing")
    assert path.exists() and run.state["waiting"]["questions"] == []
    assert run.state["waiting"]["kind"] == "fix_attempts_exhausted"


# ------------------------------------------------------------------ answer


def raise_with_question(cfg, run, key="plan/grill", qids=("q1",)):
    return raise_hitl(cfg, run, "needs_human", key, "need a call", [one_question(q) for q in qids])


def test_answer_records_and_clears_the_wait(run, cfg, macos_calls):
    path = raise_with_question(cfg, run)
    assert answer(run, "plan/grill", {"q1": ANSWER}, "me") is True
    assert run.state["waiting"] is None and run.state["status"] == "running"
    ev = [e for e in run.events.read() if e["type"] == "hitl_answered"][-1]
    assert ev["by"] == "me" and ev["key"] == "plan/grill" and ev["answers"] == {"q1": ANSWER}
    assert parse_answers(path) == {"q1": ANSWER}


def test_answer_partial_keeps_waiting(run, cfg, macos_calls):
    path = raise_with_question(cfg, run, qids=("q1", "q2"))
    assert answer(run, "plan/grill", {"q1": "A"}, "me") is False
    assert run.state["waiting"] is not None
    assert parse_answers(path) == {"q1": "A", "q2": ""}
    assert answer(run, "plan/grill", {"q2": "B"}, "me") is True
    assert run.state["waiting"] is None


def test_answer_picks_up_a_hand_edited_inbox_file(run, cfg, macos_calls):
    path = raise_with_question(cfg, run)
    fill(path, {"q1": ANSWER})
    assert answer(run, "plan/grill", {}, "me") is True
    ev = [e for e in run.events.read() if e["type"] == "hitl_answered"][-1]
    assert ev["answers"] == {"q1": ANSWER}


def test_answer_rejects_unknown_ids_and_wrong_waits(run, cfg, macos_calls):
    raise_with_question(cfg, run)
    with pytest.raises(ValueError, match="q9"):
        answer(run, "plan/grill", {"q9": "x"}, "me")
    with pytest.raises(NotWaiting):
        answer(run, "plan/other", {"q1": "x"}, "me")
    run.emit("phase_approval_raised", phase="plan", round=0)
    with pytest.raises(NotWaiting):
        answer(run, "plan_approved", {}, "me")


def test_answer_when_nothing_waits(run):
    with pytest.raises(NotWaiting):
        answer(run, "plan/grill", {"q1": "x"}, "me")


def test_answer_recreates_a_deleted_inbox_file(run, cfg, macos_calls):
    path = raise_with_question(cfg, run)
    path.unlink()
    assert answer(run, "plan/grill", {"q1": ANSWER}, "me") is True
    assert parse_answers(path) == {"q1": ANSWER}


# ------------------------------------------------------------------ approve / reject


def test_approve_a_phase(run):
    run.emit("phase_started", phase="plan", start_sha="a")
    run.emit("phase_approval_raised", phase="plan", round=0)
    approve(run, "plan_approved", "me")
    assert run.state["phases"]["plan"]["status"] == "approved" and run.state["waiting"] is None
    ev = [e for e in run.events.read() if e["type"] == "phase_approved"][-1]
    assert ev["phase"] == "plan" and ev["by"] == "me"


def test_double_approve_rejected(run):
    run.emit("phase_approval_raised", phase="plan", round=0)
    approve(run, "plan", "me")
    with pytest.raises(NotWaiting, match="not awaiting approval"):
        approve(run, "plan", "me")
    assert len([e for e in run.events.read() if e["type"] == "phase_approved"]) == 1


def test_approve_the_wrong_phase_is_refused(run):
    run.emit("phase_approval_raised", phase="plan", round=0)
    with pytest.raises(NotWaiting):
        approve(run, "build", "me")
    assert run.state["status"] == "awaiting_approval"


def test_approve_an_inner_checkpoint(run, cfg, macos_calls):
    raise_hitl(cfg, run, "checkpoint", "plan/doubt_offer", "run the doubt skill?")
    approve(run, "plan/doubt_offer", "me")
    assert run.state["waiting"] is None
    ev = [e for e in run.events.read() if e["type"] == "checkpoint_cleared"][-1]
    assert ev["key"] == "plan/doubt_offer" and ev["by"] == "me"
    with pytest.raises(NotWaiting):
        approve(run, "plan/doubt_offer", "me")


def test_approve_refuses_a_wait_that_needs_answers(run, cfg, macos_calls):
    raise_with_question(cfg, run)
    with pytest.raises(NotWaiting, match="answer"):
        approve(run, "plan/grill", "me")


def test_reject_records_note(run):
    run.emit("step_started", key="plan/writing-plans", phase="plan")
    run.emit("step_started", key="build/tdd", phase="build")
    run.emit("phase_approval_raised", phase="plan", round=0)
    reject(run, "plan", "me", "Tách task 3")
    note = run.dir / "refine" / "plan-1.md"
    assert "Tách task 3" in note.read_text(encoding="utf-8")
    assert run.state["refine"] == {"phase": "plan", "round": 1, "note": "Tách task 3"}
    assert run.state["status"] == "refining" and run.state["waiting"] is None
    assert run.state["steps"]["plan/writing-plans"]["status"] == "refine_pending"
    assert run.state["steps"]["build/tdd"]["status"] == "running"
    ev = [e for e in run.events.read() if e["type"] == "phase_rejected"][-1]
    assert ev["note"] == "Tách task 3" and ev["round"] == 1 and ev["by"] == "me"


def test_second_rejection_is_round_two(run):
    run.emit("phase_approval_raised", phase="plan", round=0)
    reject(run, "plan", "me", "one")
    run.emit("phase_approval_raised", phase="plan", round=1)
    reject(run, "plan_approved", "me", "two")
    assert run.state["refine"]["round"] == 2
    assert (run.dir / "refine" / "plan-2.md").exists()


def test_reject_needs_a_note_and_an_approval_wait(run):
    with pytest.raises(NotWaiting):
        reject(run, "plan", "me", "note")
    run.emit("phase_approval_raised", phase="plan", round=0)
    with pytest.raises(ValueError):
        reject(run, "plan", "me", "   ")
    with pytest.raises(NotWaiting):
        reject(run, "build", "me", "note")
    assert not (run.dir / "refine").exists()


def test_reject_an_inner_checkpoint_refines_its_phase(run, cfg, macos_calls):
    raise_hitl(cfg, run, "checkpoint", "plan/doubt_offer", "run the doubt skill?")
    reject(run, "plan/doubt_offer", "me", "not yet")
    assert run.state["refine"] == {"phase": "plan", "round": 1, "note": "not yet"}
    assert run.state["waiting"] is None


# ------------------------------------------------------------------ inbox listing and CLI


def test_inbox_lists_waiting_runs(repo, cfg, macos_calls):
    waiting = Run.create(cfg, repo, "feature", {"module": "crm"})
    other = Run.create(cfg, repo, "bug", {"issue": 3})
    other.emit("step_started", key="debug/x", phase="debug")
    raise_hitl(cfg, waiting, "needs_human", "plan/grill", "need a call", [one_question()])
    (item,) = inbox_items(repo)
    assert item["run_id"] == waiting.id and item["kind"] == "needs_human" and item["key"] == "plan/grill"
    assert item["summary"] == "need a call" and item["questions"][0]["id"] == "q1"
    assert item["inbox_path"] == str(waiting.inbox_path("plan/grill"))
    other.emit("phase_approval_raised", phase="debug", round=0)
    assert {i["run_id"] for i in inbox_items(repo)} == {waiting.id, other.id}
    waiting.emit("run_cancelled")
    assert [i["run_id"] for i in inbox_items(repo)] == [other.id]


def test_inbox_cli(repo, cfg, macos_calls, capsys):
    assert main(["inbox"]) == 0
    assert "Nothing is waiting" in capsys.readouterr().out
    waiting = Run.create(cfg, repo, "feature", {"module": "crm"})
    raise_hitl(cfg, waiting, "needs_human", "plan/grill", "need a call", [one_question()])
    capsys.readouterr()
    assert main(["inbox"]) == 0
    out = capsys.readouterr().out
    assert waiting.id in out and "plan/grill" in out and QUESTION in out and f"agentflow answer {waiting.id}" in out


def test_answer_cli_with_set(repo, cfg, macos_calls, capsys):
    run = Run.create(cfg, repo, "feature", {"module": "crm"})
    raise_with_question(cfg, run, qids=("q1", "q2"))
    assert main(["answer", run.id, "--set", "q1=A", "--set", f"q2={ANSWER}=x", "--no-continue"]) == 0
    out = capsys.readouterr().out
    assert "recorded" in out.lower()
    run = Run.load(repo, run.id)
    assert run.state["waiting"] is None
    ev = [e for e in run.events.read() if e["type"] == "hitl_answered"][-1]
    assert ev["answers"] == {"q1": "A", "q2": f"{ANSWER}=x"}


def test_answer_cli_partial_and_errors(repo, cfg, macos_calls, capsys):
    run = Run.create(cfg, repo, "feature", {"module": "crm"})
    raise_with_question(cfg, run, qids=("q1", "q2"))
    assert main(["answer", run.id, "--set", "q1=A"]) == 0
    assert "still waiting" in capsys.readouterr().out.lower()
    assert main(["answer", run.id, "--set", "q9=A"]) == 1
    assert "q9" in capsys.readouterr().out
    assert main(["answer", run.id, "--set", "nonsense"]) == 1
    capsys.readouterr()
    assert main(["answer", "no-such-run", "--set", "q1=A"]) == 1


def test_answer_cli_interactive(repo, cfg, macos_calls, monkeypatch, capsys):
    run = Run.create(cfg, repo, "feature", {"module": "crm"})
    raise_with_question(cfg, run, qids=("q1", "q2"))
    replies = iter(["B", ANSWER])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(replies))
    assert main(["answer", run.id, "--no-continue"]) == 0
    out = capsys.readouterr().out
    assert QUESTION in out and "A: Theo tiến trình (recommended)" in out
    ev = [e for e in Run.load(repo, run.id).events.read() if e["type"] == "hitl_answered"][-1]
    assert ev["answers"] == {"q1": "B", "q2": ANSWER}


def test_answer_cli_when_nothing_waits(repo, cfg, capsys):
    run = Run.create(cfg, repo, "feature", {"module": "crm"})
    assert main(["answer", run.id, "--set", "q1=A"]) == 1
    assert "not waiting" in capsys.readouterr().out.lower()


def test_approve_and_reject_cli(repo, cfg, capsys):
    run = Run.create(cfg, repo, "feature", {"module": "crm"})
    run.emit("phase_approval_raised", phase="plan", round=0)
    assert main(["reject", run.id, "plan", "--note", "Tách task 3", "--by", "tester", "--no-continue"]) == 0
    assert "refine" in capsys.readouterr().out.lower()
    run = Run.load(repo, run.id)
    assert run.state["refine"] == {"phase": "plan", "round": 1, "note": "Tách task 3"}
    assert main(["approve", run.id, "plan"]) == 1  # a refine round is running, nothing awaits approval
    assert "not awaiting approval" in capsys.readouterr().out
    run.emit("phase_approval_raised", phase="plan", round=1)
    assert main(["approve", run.id, "plan_approved", "--by", "tester", "--no-continue"]) == 0
    assert "approved" in capsys.readouterr().out
    done = Run.load(repo, run.id)
    assert done.state["phases"]["plan"]["status"] == "approved"
    assert [e["by"] for e in done.events.read() if e["type"] in ("phase_rejected", "phase_approved")] == ["tester", "tester"]
    assert main(["reject", run.id, "plan", "--note", "x"]) == 1


def test_stray_events_cannot_clear_a_phase_approval_but_approve_can(run):
    run.emit("phase_approval_raised", phase="plan", round=0)
    run.emit("hitl_answered", key="build/x", by="me", answers={})
    run.emit("checkpoint_cleared", key="plan_approved", by="me")
    assert run.state["waiting"]["key"] == "plan_approved" and run.state["status"] == "awaiting_approval"
    approve(run, "plan", "me")
    assert run.state["waiting"] is None and run.state["phases"]["plan"]["status"] == "approved"
