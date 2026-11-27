from __future__ import annotations

import dataclasses
import json
import subprocess
from pathlib import Path

import pytest

from agentflow import hitl, pack
from agentflow.autonomy import AUTO_BY
from agentflow.flow import main
from agentflow.hitl import (
    CODE_MAP_PER_RESULT,
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


def test_clean_fact_drops_format_characters():
    # a bidi override, a zero-width space and a byte-order mark are invisible, and can reorder or hide what a
    # reviewer reads in a prompt, a notification or a pull request body
    assert hitl.clean_fact("﻿use ‮evil‬ the​ cache⁦ here⁩") == "use evil the cache here"
    assert hitl.clean_fact("a​b") == "ab"


def test_stop_class_is_advisory(tmp_path):
    p = tmp_path / "r.json"
    assert read_result(write_result(p, {"status": "done", "summary": "x", "stop_class": "flaky"})).stop_class == "flaky"
    assert read_result(write_result(p, {"status": "done", "summary": "x"})).stop_class is None
    for value in ("weather", 3, None, ["flaky"]):  # never a `ResultError`: the field only advises
        assert read_result(write_result(p, {"status": "done", "summary": "x", "stop_class": value})).stop_class is None
    assert hitl.STOP_CLASSES == ("environment", "flaky", "guard_false_positive", "spec_gap", "impact",
                                 "repeated_failure")


@pytest.mark.parametrize("facts", ["one fact", {"a": "b"}, [1], [["nested"]], [None]])
def test_result_facts_must_be_a_list_of_strings(tmp_path, facts):
    with pytest.raises(ResultError):
        read_result(write_result(tmp_path / "r.json", {"status": "done", "summary": "x", "facts": facts}))


def test_result_code_map_is_cleaned_cut_and_capped(tmp_path):
    p = tmp_path / "r.json"
    raw = [f"`app/m{n}.py:{n}` `f{n}()`: does {n}" for n in range(12)]
    raw[1] = "`app/a.py:1` evil \u202e name"
    raw[2] = "x" * 2000
    code_map = read_result(write_result(p, {"status": "done", "summary": "x", "code_map": raw})).code_map
    assert len(code_map) == CODE_MAP_PER_RESULT == 8
    assert code_map[0] == raw[0] and code_map[3:] == raw[3:CODE_MAP_PER_RESULT]
    assert all("\u202e" not in c and len(c) <= FACT_MAX for c in code_map)
    assert code_map[1] == "`app/a.py:1` evil name"
    assert code_map[2] == "x" * FACT_MAX


@pytest.mark.parametrize("code_map", ["x", {"a": "b"}, [1], [["nested"]], [None]])
def test_result_code_map_must_be_a_list_of_strings(tmp_path, code_map):
    with pytest.raises(ResultError, match="code_map must be a list of strings"):
        read_result(write_result(tmp_path / "r.json", {"status": "done", "summary": "x", "code_map": code_map}))


def test_a_result_with_no_code_map_has_an_empty_one(tmp_path):
    assert read_result(write_result(tmp_path / "r.json", {"status": "done", "summary": "x"})).code_map == []


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


def test_result_accepts_a_boundary_question_and_refuses_an_unknown_kind(tmp_path):
    asked = {"id": "q1", "question": "Đổi lược đồ có cần hỏi trước không?", "kind": "boundary",
             "options": [{"key": "A", "label": "Hỏi trước", "recommended": True}]}
    res = read_result(write_result(tmp_path / "r.json", {"status": "needs_human", "summary": "x", "questions": [asked]}))
    assert [q.kind for q in res.questions] == ["boundary"]
    with pytest.raises(ResultError, match="boundary"):  # the error lists the kinds there are
        read_result(write_result(tmp_path / "r.json", {"status": "needs_human", "summary": "x",
                                                       "questions": [{**asked, "kind": "taste"}]}))


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
    asked = dataclasses.replace(one_question(), kind="boundary")  # one only a human answers, in any phase
    path = raise_hitl(cfg, run, "needs_human", "plan/plan", "Task 1 needs a TTL", [asked])
    assert path == run.inbox_path("plan/plan") and path.exists()
    assert "\a" in capsys.readouterr().err
    (argv,) = macos_calls
    assert argv[:2] == ["osascript", "-e"]
    assert f"agentflow: {run.id} needs you" in argv[2]
    assert argv[2].startswith("display notification")
    assert run.state["waiting"]["kind"] == "needs_human"
    assert run.state["waiting"]["key"] == "plan/plan"
    assert run.state["waiting"]["phase"] == "plan"
    assert run.state["waiting"]["summary"] == "Task 1 needs a TTL"
    assert run.state["waiting"]["questions"][0]["id"] == "q1"
    assert run.state["status"] == "waiting_human"
    assert parse_answers(path) == {"q1": ""}
    assert f"agentflow answer {run.id}" in path.read_text(encoding="utf-8")


def test_a_task_stop_that_may_park_is_raised_without_a_notification(run, cfg, macos_calls, capsys):
    asked = dataclasses.replace(one_question(), kind="boundary")  # the runner never answers it, but it may park
    raise_hitl(cfg, run, "needs_human", "build/build/task-1/implement", "Task 1 needs a TTL", [asked])
    assert macos_calls == [] and "\a" not in capsys.readouterr().err  # the plan walk tells a person if it can't park
    assert run.state["waiting"]["notified"] is False


def test_a_wave_stop_raises_quietly(run, cfg, macos_calls, capsys):
    asked = dataclasses.replace(one_question(), kind="boundary")  # the runner never answers it, and a wave stop never parks
    raise_hitl(cfg, run, "needs_human", "build/build/wave-1/implement", "a member needs a TTL", [asked])
    raise_hitl(cfg, run, "needs_human", "build/build/wave-1/review", "a reviewer needs a TTL", [asked])
    assert macos_calls == [] and "\a" not in capsys.readouterr().err
    assert [e["notified"] for e in hitl_raised_of(run)] == [False, False]
    raise_hitl(cfg, run, "needs_human", "build/build/wave-1/gate", "a gate needs a TTL", [asked])  # not a wave's session
    assert len(macos_calls) == 1


def test_hitl_raised_keeps_the_extra_text_capped(run, cfg, macos_calls):
    raise_hitl(cfg, run, "gate_failed", "build/x", "s", extra="x" * 5000 + "END")
    ev = [e for e in run.events.read() if e["type"] == "hitl_raised"][-1]
    assert len(ev["extra"]) == hitl.EVENT_EXTRA_MAX == 4000 and ev["extra"].endswith("END")


def test_hitl_raised_without_extra_records_an_empty_one(run, cfg, macos_calls):
    raise_hitl(cfg, run, "gate_failed", "build/x", "s")
    assert [e for e in run.events.read() if e["type"] == "hitl_raised"][-1]["extra"] == ""


def test_a_question_the_runner_takes_is_raised_without_a_notification(run, cfg, macos_calls, capsys):
    raise_hitl(cfg, run, "needs_human", "plan/task-1/implement", "Task 1 needs a TTL", [one_question()])
    assert run.state["waiting"]["key"] == "plan/task-1/implement"  # the resolver answers it next
    assert macos_calls == [] and "\a" not in capsys.readouterr().err


def speculate_plan(run):
    run.emit("phase_approval_raised", phase="define", round=0)
    run.emit("speculation_started", phase="plan", after="define", basis={"docs/specs/S.md": "a" * 64}, sha="s1")


def test_a_stop_under_the_speculated_phase_is_raised_quietly(run, cfg, macos_calls, capsys):
    speculate_plan(run)
    asked = dataclasses.replace(one_question(), kind="boundary")
    raise_hitl(cfg, run, "needs_human", "plan/plan", "needs a TTL", [asked])
    (event,) = hitl_raised_of(run)
    assert event["speculative"] is True and event["notified"] is False
    assert macos_calls == [] and "\a" not in capsys.readouterr().err
    assert run.state["waiting"]["key"] == "define_approved"
    assert run.state["waiting_queue"][0]["speculative"] is True


def test_a_stop_under_the_speculated_phase_stays_quiet_even_when_asked_to_notify(run, cfg, macos_calls):
    speculate_plan(run)
    raise_hitl(cfg, run, "gate_failed", "plan/plan", "s", notify=True)
    assert macos_calls == [] and hitl_raised_of(run)[0]["notified"] is False


def test_a_stop_of_another_phase_is_not_speculative(run, cfg, macos_calls):
    speculate_plan(run)
    asked = dataclasses.replace(one_question(), kind="boundary")  # one only a human answers
    raise_hitl(cfg, run, "needs_human", "build/x", "s", [asked])
    (event,) = hitl_raised_of(run)
    assert "speculative" not in event and event["notified"] is True and len(macos_calls) == 1


def test_a_stop_without_a_speculation_is_not_speculative(run, cfg, macos_calls):
    raise_hitl(cfg, run, "gate_failed", "plan/plan", "s")
    (event,) = hitl_raised_of(run)
    assert "speculative" not in event and event["notified"] is True


REPEAT_KEY = "plan/write_plan"


def raise_repeat(cfg, run, summary="blocked again"):
    raise_hitl(cfg, run, "blocked_repeat", REPEAT_KEY, summary)
    return run.state["waiting"]


def hitl_raised_of(run) -> list[dict]:
    return [e for e in run.events.read() if e["type"] == "hitl_raised"]


def heard(monkeypatch) -> list[str]:
    seen: list[str] = []
    monkeypatch.setattr(hitl, "notify", lambda cfg, title, message, run=None: seen.append(message))
    return seen


def test_a_reworded_summary_still_counts(run, cfg, monkeypatch):
    heard(monkeypatch)
    raise_repeat(cfg, run, "cannot go on")
    approve(run, REPEAT_KEY, by="me", note="x")
    assert raise_repeat(cfg, run, "still cannot, in other words")["times"] == 2


def test_a_repeat_after_a_human_approval_says_so(run, cfg, monkeypatch):
    seen = heard(monkeypatch)
    raise_repeat(cfg, run)
    assert run.state["waiting"]["times"] == 1
    approve(run, REPEAT_KEY, by="me", note="x")
    raise_repeat(cfg, run)
    assert len(seen) == 2
    assert "after 1 approval(s)" in seen[1] and "after" not in seen[0]
    assert seen[1] == f"{REPEAT_KEY} is blocked again for the same reason after 1 approval(s): blocked again"
    assert run.state["waiting"]["times"] == 2
    assert hitl_raised_of(run)[-1]["times"] == 2
    assert "came back after 1 approval(s)" in run.inbox_path(REPEAT_KEY).read_text(encoding="utf-8")


def test_a_retry_starts_the_count_afresh(run, cfg, monkeypatch):
    heard(monkeypatch)
    raise_repeat(cfg, run)
    approve(run, REPEAT_KEY, by="me", note="x")
    run.emit("step_retry", by="me", key=REPEAT_KEY, reason="fresh")
    assert raise_repeat(cfg, run)["times"] == 1


def test_a_blocked_stop_a_human_approved_counts_for_the_repeat(run, cfg, monkeypatch):
    heard(monkeypatch)
    raise_hitl(cfg, run, "blocked", REPEAT_KEY, "cannot go on")
    approve(run, REPEAT_KEY, by="me")
    assert raise_repeat(cfg, run)["times"] == 2


def test_runner_approvals_and_other_kinds_are_not_counted(run, cfg, monkeypatch):
    seen = heard(monkeypatch)
    raise_repeat(cfg, run)
    approve(run, REPEAT_KEY, by=AUTO_BY, note="x")
    assert raise_repeat(cfg, run)["times"] == 1
    assert "after" not in seen[-1]
    for kind in ("blocked", "gate_failed"):
        raise_hitl(cfg, run, kind, "build/other", "s")
        approve(run, "build/other", by="me")
        raise_hitl(cfg, run, kind, "build/other", "s")
    assert all("times" not in event for event in hitl_raised_of(run) if event["kind"] in ("blocked", "gate_failed"))
    assert "times" not in run.state["waiting"]


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


def test_the_cli_approval_carries_no_viewed(run):
    run.emit("phase_started", phase="define")
    run.emit("phase_approval_raised", phase="define", round=0, summary="s")
    approve(run, "define", by="me")
    assert "viewed" not in [e for e in run.events.read() if e["type"] == "phase_approved"][-1]


def test_a_phase_approval_records_viewed_only_when_there_is_some(run):
    viewed = [{"path": "docs/specs/x.md", "sha": "a" * 40}]
    run.emit("phase_started", phase="define")
    run.emit("phase_approval_raised", phase="define", round=0, summary="s")
    approve(run, "define", by="me", viewed=[])
    assert "viewed" not in [e for e in run.events.read() if e["type"] == "phase_approved"][-1]
    run.emit("phase_rejected", phase="define", by="me", note="again")
    run.emit("phase_started", phase="define")
    run.emit("phase_approval_raised", phase="define", round=1, summary="s")
    approve(run, "define", by="me", viewed=viewed)
    assert [e for e in run.events.read() if e["type"] == "phase_approved"][-1]["viewed"] == viewed


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


@pytest.mark.parametrize("key", ["build/build/task-2/implement", "build/build/task-2/commit"])
def test_rejecting_a_task_stop_refines_only_that_task(run, cfg, macos_calls, key):
    path = raise_hitl(cfg, run, "blocked", key, "cannot go on")
    assert "sends task 2 back for a refine round" in path.read_text(encoding="utf-8")
    reject(run, key, "me", "redo task two")
    assert run.state["refine"] == {"phase": "build", "round": 1, "note": "redo task two",
                                   "scope": "build/build/task-2"}
    ev = [e for e in run.events.read() if e["type"] == "phase_rejected"][-1]
    assert ev["scope"] == "build/build/task-2" and ev["key"] == key


@pytest.mark.parametrize("key", ["build/build", "build/setup", "build/build/task-x/implement"])
def test_rejecting_a_stop_outside_a_task_refines_the_phase(run, cfg, macos_calls, key):
    path = raise_hitl(cfg, run, "blocked", key, "cannot go on")
    assert "sends the phase back for a refine round" in path.read_text(encoding="utf-8")
    reject(run, key, "me", "redo")
    assert "scope" not in run.state["refine"]
    assert "scope" not in [e for e in run.events.read() if e["type"] == "phase_rejected"][-1]


def test_an_environment_stop_cannot_be_rejected(run, cfg, macos_calls):
    path = raise_hitl(cfg, run, hitl.ENVIRONMENT, "build/fix", "build/fix waits on this machine: `pnpm` is missing")
    text = path.read_text(encoding="utf-8")
    assert "approves this itself once the checks pass" in text and f"agentflow cancel {run.id}" in text
    with pytest.raises(NotWaiting, match="an environment stop has nothing to refine"):
        reject(run, "build/fix", "me", "try again")
    assert run.state["waiting"]["kind"] == "environment" and not run.state.get("refine")


def test_raise_hitl_notify_overrides_who_is_told(run, cfg, macos_calls):
    raise_hitl(cfg, run, "blocked", "build/a", "the runner settles it")  # `None`: today's rule, quiet for the runner
    assert not macos_calls
    raise_hitl(cfg, run, "blocked", "build/b", "told anyway", notify=True)
    assert len(macos_calls) == 1
    raise_hitl(cfg, run, "checkpoint", "plan/doubt_offer", "never told", notify=False)
    assert len(macos_calls) == 1


def test_raise_hitl_records_whether_a_person_was_told(run, cfg, macos_calls):
    raise_hitl(cfg, run, "blocked", "build/a", "quiet", notify=False)
    assert run.events.read()[-1]["notified"] is False and run.state["waiting"]["notified"] is False
    raise_hitl(cfg, run, "blocked", "build/b", "told", notify=True)
    assert run.events.read()[-1]["notified"] is True and run.state["waiting"]["notified"] is True


# ------------------------------------------------------------------ parked stops (T4)

PA = "build/build/task-1/implement"
PB = "build/build/task-2/implement"
RADIUS = {"blocks": [2], "continue": [3]}


def park(cfg, run, key, kind="blocked", questions=(), radius=None, extra=""):
    """What the plan walk does with a task stop it parks: raise the wait, then park it away."""
    raise_hitl(cfg, run, kind, key, f"{key} is stuck", questions, notify=False)
    return hitl.park_wait(cfg, run, dict(run.state["waiting"]), blast_radius=radius or RADIUS, shelf=None,
                          shelved=[], stop_class=None, extra=extra)


def promote(run, key):
    """What the walk does once no runnable task is left: the parked item becomes the run's wait."""
    item = run.state["parked"][key]
    run.emit("hitl_raised", kind=item["kind"], key=key, phase="build", summary=item["summary"],
             questions=item["questions"], promoted=True, notified=True)


def test_park_wait_records_the_park_and_clears_the_wait(run, cfg, macos_calls):
    path = park(cfg, run, PA, extra="the gate log")
    assert path == run.inbox_path(PA)
    ev = run.events.read()[-1]
    assert ev["type"] == "step_parked" and ev["kind"] == "blocked" and ev["paths"] == []
    assert ev["blast_radius"] == RADIUS and ev["shelf"] is None and ev["shelved"] == [] and ev["stop_class"] is None
    assert "demoted" not in ev
    assert run.state["waiting"] is None and run.state["status"] == "running" and list(run.state["parked"]) == [PA]
    assert run.state["steps"][PA]["status"] == "parked"


def test_the_parked_inbox_file_names_the_blast_radius(run, cfg, macos_calls):
    text = park(cfg, run, PA, extra="the gate log").read_text(encoding="utf-8")
    assert "Blast radius: blocks task 2; task 3 continues" in text and "the gate log" in text
    assert f"agentflow approve {run.id} {PA}" in text and f"agentflow retry {run.id} {PA}" in text
    assert f"agentflow pause {run.id}" in text and "agentflow answer" not in text
    assert [c for c in macos_calls if f"parked {PA}: blocks task 2; task 3 continues" in c[-1]]
    asked = park(cfg, run, PB, kind="needs_human", questions=[one_question()]).read_text(encoding="utf-8")
    assert f"agentflow answer {run.id} --key {PB}" in asked and QUESTION in asked


def test_a_parked_stop_is_approved_by_its_key(run, cfg, macos_calls):
    park(cfg, run, PA)
    assert approve(run, PA, "me") is False
    ev = run.events.read()[-1]
    assert ev["type"] == "hitl_approved" and ev["key"] == PA and ev["kind"] == "blocked" and ev["by"] == "me"
    assert run.state["waiting"] is None and run.state["parked"] == {}
    assert run.state["steps"][PA]["status"] == "running"


def test_a_parked_blocked_repeat_needs_a_note(run, cfg, macos_calls):
    park(cfg, run, PA, kind="blocked_repeat")
    with pytest.raises(NotWaiting, match="blocked again with nothing changed"):
        approve(run, PA, "me")
    assert list(run.state["parked"]) == [PA]
    approve(run, PA, "me", note="the import is fixed")
    assert run.events.read()[-1]["note"] == "the import is fixed" and run.state["parked"] == {}


def test_a_parked_question_is_answered_not_approved(run, cfg, macos_calls):
    park(cfg, run, PA, kind="needs_human", questions=[one_question()])
    with pytest.raises(NotWaiting, match="waiting for answers"):
        approve(run, PA, "me")
    assert answer(run, PA, {"q1": ANSWER}, "me") is True
    assert run.state["parked"] == {} and run.state["waiting"] is None


def test_a_parked_item_cannot_be_rejected(run, cfg, macos_calls):
    park(cfg, run, PA)
    with pytest.raises(NotWaiting) as caught:
        reject(run, PA, "me", "redo it")
    assert str(caught.value) == f"{PA} is parked: approve, answer or retry it; reject it once it is the run's wait"
    assert list(run.state["parked"]) == [PA] and not run.state.get("refine")


def test_settling_a_parked_item_demotes_the_promoted_wait(run, cfg, macos_calls):  # Review Focus 2
    park(cfg, run, PA)
    park(cfg, run, PB)
    promote(run, PA)
    assert run.state["waiting"]["key"] == PA and run.state["waiting"]["promoted"] is True
    approve(run, PB, "me")
    assert run.state["waiting"] is None and set(run.state["parked"]) == {PA}
    assert run.state["status"] == "running"
    last = run.events.read()[-1]
    assert last["type"] == "step_parked" and last["demoted"] is True and last["key"] == PA
    assert last["blast_radius"] == RADIUS
    promote(run, PA)
    approve(run, PA, "me")  # settling the promoted wait itself demotes nothing
    assert run.state["waiting"] is None and run.state["parked"] == {}
    assert run.events.read()[-1]["type"] == "hitl_approved"


def test_answering_a_parked_item_demotes_the_promoted_wait(run, cfg, macos_calls):
    park(cfg, run, PA)
    park(cfg, run, PB, kind="needs_human", questions=[one_question()])
    promote(run, PA)
    assert answer(run, PB, {"q1": ANSWER}, "me") is True
    assert run.state["waiting"] is None and set(run.state["parked"]) == {PA}
    assert run.events.read()[-1]["demoted"] is True


def test_a_parked_stop_is_retried_by_its_key(run, cfg, macos_calls):
    from agentflow.run_control import ControlError, retry

    park(cfg, run, PA)
    park(cfg, run, PB)
    park(cfg, run, "build/build/task-3/implement", kind="needs_human", questions=[one_question()])
    with pytest.raises(ControlError, match="parked waiting for answers"):
        retry(run, "build/build/task-3/implement", "me")
    promote(run, PA)
    assert retry(run, PB, "me") == PB
    assert run.state["steps"][PB]["status"] == "pending" and PB not in run.state["parked"]
    assert run.state["waiting"] is None and PA in run.state["parked"]
    assert run.events.read()[-1]["demoted"] is True


def test_inbox_items_carry_the_stop_kind_of_a_parked_item(repo, run, cfg, macos_calls):
    park(cfg, run, PA, kind="guard_violation")
    (item,) = inbox_items(repo)
    assert item["kind"] == "parked" and item["stop_kind"] == "guard_violation" and item["key"] == PA
    assert item["blast_radius"] == RADIUS and item["inbox_path"] == str(run.inbox_path(PA))


def test_inbox_cli_names_a_parked_stops_way_out(repo, run, cfg, macos_calls, capsys):
    park(cfg, run, PA)
    capsys.readouterr()
    assert main(["inbox"]) == 0
    out = capsys.readouterr().out
    assert "parked (blocked)" in out and "Blast radius: blocks task 2; task 3 continues" in out
    assert f"agentflow approve {run.id} {PA}" in out and f"agentflow retry {run.id} {PA}" in out


def test_inbox_cli_asks_for_a_note_on_a_parked_repeated_block(repo, run, cfg, macos_calls, capsys):
    park(cfg, run, PA, kind="blocked_repeat")
    capsys.readouterr()
    assert main(["inbox"]) == 0
    out = capsys.readouterr().out
    assert f'agentflow approve {run.id} {PA} --note "..."' in out  # a plain approve of it is refused


def test_approving_while_a_runner_is_live_only_records(repo, run, cfg, macos_calls, capsys):
    from agentflow.events import RunLock

    park(cfg, run, PA)
    lock = RunLock(run.dir)
    assert lock.acquire()
    try:
        assert main(["approve", run.id, PA]) == 0
    finally:
        lock.release()
    assert f"recorded; the runner working on {run.id} picks it up" in capsys.readouterr().out
    ev = Run.load(repo, run.id).events.read()[-1]
    assert ev["type"] == "hitl_approved" and ev["key"] == PA


def test_resuming_while_a_runner_is_live_records_nothing_and_is_refused(repo, run, cfg, capsys, monkeypatch):
    from agentflow import flow
    from agentflow.events import RunLock

    monkeypatch.setattr(flow, "_preflight", lambda cfg: None)  # no agent CLI is needed to be refused
    lock = RunLock(run.dir)
    assert lock.acquire()
    try:
        assert main(["resume", run.id, "--config", str(cfg.path)]) == 1
    finally:
        lock.release()
    out = capsys.readouterr().out
    assert "recorded" not in out and f"another runner is working on {run.id}" in out


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


def test_lessons_cited_must_be_lesson_ids(tmp_path):
    for bad in (["drop table"], ["L-1"], ["L-00001"], ["X-0001"], ["L-0001\n"], "L-0001", [1], [["L-0001"]], {"a": "L-0001"}):
        with pytest.raises(ResultError, match="lessons_cited"):
            read_result(write_result(tmp_path / "r.json", {"status": "done", "summary": "x", "lessons_cited": bad}))


def test_lessons_cited_keeps_the_first_ten(tmp_path):
    ids = [f"L-{n:04d}" for n in range(1, 13)] + ["E-0001"]
    got = read_result(write_result(tmp_path / "r.json", {"status": "done", "summary": "x", "lessons_cited": ids}))
    assert got.lessons_cited == ids[:10]
    assert read_result(write_result(tmp_path / "r.json", {"status": "done", "summary": "x"})).lessons_cited == []
    assert read_result(write_result(tmp_path / "r.json", {"status": "done", "summary": "x",
                                                          "lessons_cited": ["E-0003"]})).lessons_cited == ["E-0003"]


def test_read_result_takes_dispositions(tmp_path):
    disposed = [{"finding": "R1-F1", "class": "noise", "note": "Task 3 covers it"}]
    res = read_result(write_result(tmp_path / "r.json", {"status": "done", "summary": "x", "dispositions": disposed}))
    assert res.dispositions == disposed
    assert read_result(write_result(tmp_path / "r.json", {"status": "done", "summary": "x"})).dispositions == []


@pytest.mark.parametrize("bad", ["R1-F1", {"finding": "R1-F1"}, 3])
def test_read_result_refuses_dispositions_that_are_not_a_list(tmp_path, bad):
    with pytest.raises(ResultError, match="dispositions must be a list"):
        read_result(write_result(tmp_path / "r.json", {"status": "done", "summary": "x", "dispositions": bad}))


# ------------------------------------------------------------------ which stops park a task (T4)

TASK_IMPLEMENT = "build/build/task-2/implement"


def _park_cfg(cfg, **hitl_keys):
    return dataclasses.replace(cfg, hitl={**cfg.hitl, **hitl_keys})


def test_parks_first_only_for_a_task_steps_parkable_stop(cfg):
    assert hitl.parks_first(cfg, "blocked", TASK_IMPLEMENT)
    assert hitl.parks_first(cfg, "gate_failed", "build/build/task-2/commit")
    assert hitl.parks_first(cfg, "needs_human", "build/build/task-2/review")
    assert not hitl.parks_first(cfg, "blocked", TASK_IMPLEMENT + "/resolve-1")  # a resolver's own stop
    assert not hitl.parks_first(cfg, "blocked", "build/build/task-2/gate")
    assert not hitl.parks_first(cfg, "blocked", "build/build/implement")  # not a task's step
    assert not hitl.parks_first(cfg, "budget_exceeded", TASK_IMPLEMENT)
    assert not hitl.parks_first(cfg, "environment", TASK_IMPLEMENT)
    assert not hitl.parks_first(_park_cfg(cfg, park_independent=False), "blocked", TASK_IMPLEMENT)


def test_park_kinds_default_to_every_parkable_stop_and_never_name_a_run_wide_one(cfg):
    engine = _park_cfg(cfg)
    assert hitl.park_kinds(dataclasses.replace(cfg, hitl={})) == hitl.PARKABLE
    assert hitl.park_kinds(engine) == tuple(engine.hitl.get("park_kinds", hitl.PARKABLE))
    assert "budget_exceeded" not in hitl.PARKABLE and "environment" not in hitl.PARKABLE
    assert hitl.park_kinds(_park_cfg(cfg, park_kinds=["blocked"])) == ("blocked",)
    assert hitl.park_kinds(_park_cfg(cfg, park_kinds=[])) == ()


def test_parks_first_follows_park_kinds(cfg):
    only_blocked = _park_cfg(cfg, park_independent=True, park_kinds=["blocked"])
    assert hitl.parks_first(only_blocked, "blocked", TASK_IMPLEMENT)
    assert not hitl.parks_first(only_blocked, "gate_failed", TASK_IMPLEMENT)
    assert not hitl.parks_first(_park_cfg(cfg, park_independent=True, park_kinds=[]), "blocked", TASK_IMPLEMENT)
    assert not hitl.parks_first(_park_cfg(cfg, park_independent="yes"), "blocked", TASK_IMPLEMENT)  # only `true` parks


def test_the_engine_default_parks_a_task_stop():
    import yaml

    raw = yaml.safe_load((Path(hitl.__file__).parent / "defaults" / "config.yaml").read_text(encoding="utf-8"))
    assert raw["hitl"]["park_independent"] is True
    assert tuple(raw["hitl"]["park_kinds"]) == hitl.PARKABLE


# ------------------------------------------------------------------ the notification digest (T4)


def _digest_cfg(cfg):
    return _park_cfg(cfg, digest=True)


def test_the_digest_counts_items_and_runs(repo, run, cfg, macos_calls):
    from agentflow.state import Run

    assert hitl.digest_text(repo) == "Nothing needs you; 1 run progressing"
    park(cfg, run, PA)  # a parked item needs a person, though its run is still progressing
    assert hitl.digest_text(repo) == "1 item needs you; 1 run progressing"
    other = Run.create(cfg, repo, "feature", {"module": "billing"})
    third = Run.create(cfg, repo, "feature", {"module": "search"})
    asked = dataclasses.replace(one_question(), kind="boundary")
    raise_hitl(cfg, other, "needs_human", "plan/plan", "needs a TTL", [asked])  # a run's wait: it holds the run
    assert hitl.digest_text(repo) == "2 items need you (1 holds a run); 2 runs progressing"
    assert third.state["status"] == "running"


def test_an_unchanged_digest_is_not_sent_again(repo, run, cfg, macos_calls):
    on = _digest_cfg(cfg)
    hitl.notify_run(on, run, "blocked: something specific")
    assert len(macos_calls) == 1
    assert "Nothing needs you; 1 run progressing" in macos_calls[0][2]
    assert "blocked: something specific" not in macos_calls[0][2] and 'with title "agentflow"' in macos_calls[0][2]
    hitl.notify_run(on, run, "blocked: something else")
    assert len(macos_calls) == 1  # nothing changed
    park(on, run, PA)  # a stop that parks is raised quietly, so only the digest can tell of it
    hitl.notify_run(on, run, "parked")
    assert len(macos_calls) == 2 and "1 item needs you; 1 run progressing" in macos_calls[1][2]
    assert (pack.state_dir(repo) / "notify_digest.txt").read_text(encoding="utf-8") == (
        "1 item needs you; 1 run progressing")


def write_user_file(text: str) -> None:
    from agentflow import layers

    path = layers.user_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def name_a_workspace(tmp_path) -> None:
    import yaml

    write_user_file(yaml.safe_dump({"workspace": {"roots": [str(tmp_path)]}}))


def test_the_default_title_names_the_repository_when_a_workspace_is_set(tmp_path, repo, run, cfg, macos_calls):
    name_a_workspace(tmp_path)
    untitled = dataclasses.replace(cfg, hitl={**cfg.hitl, "notify": [{"type": "macos"}]})  # the fixture pack spells the title out
    hitl.notify_run(untitled, run, "m")
    assert f'with title "agentflow \u00b7 {repo.name}: {run.id} needs you"' in macos_calls[0][2]


def test_the_digest_title_names_the_repository_too(tmp_path, repo, run, cfg, macos_calls):
    name_a_workspace(tmp_path)
    hitl.notify_run(_digest_cfg(cfg), run, "m")
    assert f'with title "agentflow \u00b7 {repo.name}"' in macos_calls[0][2]


def test_a_configured_title_is_kept_as_written_in_a_workspace(tmp_path, run, cfg, macos_calls):
    name_a_workspace(tmp_path)
    mine = dataclasses.replace(cfg, hitl={**cfg.hitl, "notify": [{"type": "macos", "title": "mine {run}"}]})
    hitl.notify_run(mine, run, "m")
    assert f'with title "mine {run.id}"' in macos_calls[0][2]


def test_an_unreadable_user_file_keeps_the_plain_title(run, cfg, macos_calls):
    write_user_file(": : :")
    untitled = dataclasses.replace(cfg, hitl={**cfg.hitl, "notify": [{"type": "macos"}]})
    hitl.notify_run(untitled, run, "m")
    assert f'with title "agentflow: {run.id} needs you"' in macos_calls[0][2]
    assert hitl._repo_label(run.repo) is None


def test_a_folder_name_with_quotes_cannot_break_the_notification_command(tmp_path, cfg, macos_calls):
    name_a_workspace(tmp_path)
    label = hitl._repo_label(Path('/w/my "x"\nrepo'))
    assert label == 'my "x" repo'
    hitl.notify(cfg, f"agentflow \u00b7 {label}", "m")
    assert 'with title "agentflow \u00b7 my \\"x\\" repo"' in macos_calls[0][2]


def test_a_runner_note_never_reaches_the_digest(repo, run, cfg, macos_calls):
    on = _digest_cfg(cfg)
    hitl.notify_run(on, run, "needs you")
    macos_calls.clear()
    hitl.notify_run(on, run, "took the recommended answer(s) q1=A in plan/grill; the decision is recorded")
    assert macos_calls == []


def test_an_unreadable_last_digest_is_sent_again(repo, run, cfg, macos_calls):
    on = _digest_cfg(cfg)
    hitl.notify_run(on, run, "x")
    last = pack.state_dir(repo) / "notify_digest.txt"
    last.unlink()
    last.mkdir()  # not a regular file: it can't be read
    hitl.notify_run(on, run, "x")
    assert len(macos_calls) == 2


def test_a_digest_that_cannot_be_built_sends_the_plain_message(repo, run, cfg, macos_calls, monkeypatch):
    def broken(repo):
        raise RuntimeError("another run's state is unreadable")

    monkeypatch.setattr(hitl, "digest_text", broken)
    hitl.notify_run(_digest_cfg(cfg), run, "blocked: something specific")  # never raises where it did not before
    assert len(macos_calls) == 1 and "blocked: something specific" in macos_calls[0][2]
    (failed,) = [e for e in run.events.read() if e["type"] == "notify_failed"]
    assert failed["notifier"] == "digest" and "unreadable" in failed["error"]
    assert not (pack.state_dir(repo) / "notify_digest.txt").exists()


def test_with_the_digest_off_every_notification_is_sent(repo, run, cfg, macos_calls):
    hitl.notify_run(cfg, run, "one")
    hitl.notify_run(cfg, run, "one")
    assert len(macos_calls) == 2 and "one" in macos_calls[0][2]
    assert not (pack.state_dir(repo) / "notify_digest.txt").exists()


def test_the_engine_default_has_the_digest_off():
    import yaml

    raw = yaml.safe_load((Path(hitl.__file__).parent / "defaults" / "config.yaml").read_text(encoding="utf-8"))
    assert raw["hitl"]["digest"] is False


# ------------------------------------------------------------------ the merged digest (W5)


def _merged_workspace(tmp_path, names=("alpha", "beta"), digest=True) -> dict[str, Path]:
    """`ws/<name>` repositories, each with a sparse pack, and a user file naming `ws` with `workspace.digest`."""
    import yaml

    from agentflow.tests.test_workspace_cli import make_repo

    ws = tmp_path / "ws"
    ws.mkdir()
    made = {name: make_repo(ws, name) for name in names}
    write_user_file(yaml.safe_dump({"workspace": {"roots": [str(ws)], "digest": digest}}))
    return made


def _held(cfg, repo, module) -> Run:
    """A run of `repo` stopped on a needs_human wait, raised quietly: an item that holds its run."""
    held = Run.create(cfg, repo, "feature", {"module": module})
    raise_hitl(cfg, held, "needs_human", "plan/plan", "needs a TTL", [dataclasses.replace(one_question(), kind="boundary")],
               notify=False)
    return held


def _kept_line() -> Path:
    return pack.user_state_root() / "workspace_digest.txt"


def test_counts_of_states_matches_the_inbox(repo, run, cfg, macos_calls):
    from agentflow.state import list_runs

    park(cfg, run, PA)  # a parked item: its run still progresses
    _held(cfg, repo, "billing")
    Run.create(cfg, repo, "feature", {"module": "search"}).emit("run_finished")
    runs = list_runs(repo)
    items = inbox_items(repo, runs)
    counts = hitl.counts_of_states(r.state for r in runs)
    assert counts == hitl.DigestCounts(len(items), sum(1 for i in items if i["kind"] != "parked"),
                                       sum(1 for r in runs if r.state["status"] in hitl.PROGRESSING))
    assert counts == hitl.DigestCounts(2, 1, 1)
    assert hitl.digest_counts(repo) == counts
    assert hitl.digest_text(repo) == hitl.digest_line(counts) == "2 items need you (1 holds a run); 1 run progressing"


def test_the_workspace_digest_counts_every_repository(tmp_path, cfg, macos_calls):
    made = _merged_workspace(tmp_path)
    alpha, beta = made["alpha"], made["beta"]
    mine = Run.create(cfg, alpha, "feature", {"module": "crm"})
    assert hitl.workspace_digest_text(alpha) == "Nothing needs you; 1 run progressing in 2 repositories"
    park(cfg, mine, PA)
    _held(cfg, beta, "billing")
    _held(cfg, beta, "search")
    assert hitl.workspace_digest_text(alpha) == (
        "3 items need you (2 hold a run): beta 2, alpha 1; 1 run progressing in 2 repositories")


def test_no_workspace_counts_the_run_s_own_repository_only(repo, run, cfg, macos_calls):
    write_user_file("workspace:\n  digest: true\n")
    assert hitl.workspace_digest_text(repo) == "Nothing needs you; 1 run progressing in 1 repository"


def test_another_repository_is_read_without_load_lock_or_write(tmp_path, cfg, macos_calls, monkeypatch):
    made = _merged_workspace(tmp_path)
    alpha, beta = made["alpha"], made["beta"]
    Run.create(cfg, alpha, "feature", {"module": "crm"})
    _held(cfg, beta, "billing")
    beta_state = pack.state_dir(beta)
    seen = sorted(beta_state.rglob("*"))
    before = {p: (p.stat().st_mtime_ns, p.read_bytes()) for p in seen if p.is_file()}
    beta_root = pack.checkout_root(beta)
    real_load = Run.load.__func__

    def load(cls, repo, run_id):
        if pack.checkout_root(repo) == beta_root:
            raise AssertionError("another repository's run was loaded")
        return real_load(cls, repo, run_id)

    monkeypatch.setattr(Run, "load", classmethod(load))
    assert hitl.workspace_digest_text(alpha) == (
        "1 item needs you (1 holds a run): beta 1; 1 run progressing in 2 repositories")
    after = sorted(beta_state.rglob("*"))
    assert after == seen  # no lock file, no new file
    assert {p: (p.stat().st_mtime_ns, p.read_bytes()) for p in after if p.is_file()} == before


def test_more_than_three_repositories_with_items_are_summed_up(tmp_path, cfg, macos_calls):
    made = _merged_workspace(tmp_path, names=("a", "b", "c", "d", "e"))
    for name, path in made.items():
        _held(cfg, path, name)
    assert hitl.workspace_digest_text(made["a"]) == (
        "5 items need you (5 hold a run): a 1, b 1, c 1, +2 more; 0 runs progressing in 5 repositories")


def test_the_run_s_own_repository_counts_when_it_is_outside_the_workspace(tmp_path, repo, run, cfg, macos_calls):
    made = _merged_workspace(tmp_path)
    park(cfg, run, PA)
    _held(cfg, made["beta"], "billing")
    assert hitl.workspace_digest_text(repo) == (
        f"2 items need you (1 holds a run): beta 1, {repo.name} 1; 1 run progressing in 3 repositories")


def _plant_state_dir(tmp_path, beta, held):
    """Beta's pack points `paths.state_dir` at a folder holding a copy of its runs: none of it may be read."""
    import shutil

    import yaml

    from agentflow.tests.conftest import SPARSE_PACK

    elsewhere = tmp_path / "elsewhere"
    shutil.copytree(held.dir, elsewhere / "runs" / held.id)
    (beta / ".agentflow" / "config.yaml").write_text(
        yaml.safe_dump({**SPARSE_PACK, "paths": {"state_dir": str(elsewhere)}}), encoding="utf-8")
    return elsewhere


def _spoil_state(text):
    def spoil(tmp_path, beta, held):
        held.state_path.write_bytes(text() if callable(text) else text)
    return spoil


def _no_waiting(tmp_path, beta, held):
    state = json.loads(held.state_path.read_text(encoding="utf-8"))
    del state["waiting"]
    held.state_path.write_text(json.dumps(state), encoding="utf-8")


def _symlinked_pack(tmp_path, beta, held):
    import shutil

    real = tmp_path / "real-pack"
    shutil.move(beta / ".agentflow", real)
    (beta / ".agentflow").symlink_to(real, target_is_directory=True)


def _fifo_pack(tmp_path, beta, held):
    import os

    config = beta / ".agentflow" / "config.yaml"
    config.unlink()
    os.mkfifo(config)


@pytest.mark.parametrize("spoil", [
    _plant_state_dir,
    _spoil_state(b"{not json"),
    _spoil_state(lambda: b"{}" + b" " * hitl.STATE_MAX_BYTES),  # valid JSON, but too large to be read
    _spoil_state(b"[]"),
    _no_waiting,
    _symlinked_pack,
    _fifo_pack,
], ids=["own_state_dir", "not_json", "too_large", "a_list", "no_waiting", "symlinked_pack", "fifo_pack"])
def test_a_repository_with_its_own_state_dir_or_a_bad_state_file_is_not_counted(tmp_path, cfg, macos_calls,
                                                                                 monkeypatch, spoil):
    import signal

    from agentflow import repo_set, safeio

    made = _merged_workspace(tmp_path)
    alpha, beta = made["alpha"], made["beta"]
    Run.create(cfg, alpha, "feature", {"module": "crm"})
    held = _held(cfg, beta, "billing")
    planted = spoil(tmp_path, beta, held)
    real_set = repo_set.from_user_config

    def all_live():  # a pack swapped for a FIFO after the scan found a regular file there
        found = real_set()
        return dataclasses.replace(found, repos=tuple(dataclasses.replace(r, state="live") for r in found.repos))

    monkeypatch.setattr(repo_set, "from_user_config", all_live)
    read: list[Path] = []
    real_read = safeio.read_bytes

    def recording(path, limit):
        read.append(Path(path))
        return real_read(path, limit)

    monkeypatch.setattr(safeio, "read_bytes", recording)

    def hung(signum, frame):
        raise AssertionError("the digest blocked on a file")

    previous = signal.signal(signal.SIGALRM, hung)
    signal.alarm(10)
    try:
        line = hitl.workspace_digest_text(alpha)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)
    assert line == "Nothing needs you; 1 run progressing in 1 repository; 1 not counted"
    if planted is not None:
        assert not [p for p in read if p.is_relative_to(planted)]


def test_a_symlinked_run_of_another_repository_is_skipped(tmp_path, cfg, macos_calls):
    import shutil

    made = _merged_workspace(tmp_path)
    alpha, beta = made["alpha"], made["beta"]
    Run.create(cfg, alpha, "feature", {"module": "crm"})
    held = _held(cfg, beta, "billing")
    away = tmp_path / "away" / held.id
    shutil.move(held.dir, away)
    held.dir.symlink_to(away, target_is_directory=True)
    assert hitl.workspace_digest_text(alpha) == "Nothing needs you; 1 run progressing in 2 repositories"


def test_a_long_line_is_cut_and_kept_whole(tmp_path, cfg, macos_calls):
    made = _merged_workspace(tmp_path, names=tuple(letter * 90 for letter in "abcd"))
    for path in made.values():
        _held(cfg, path, "billing")
    own = made["a" * 90]
    run = Run.create(cfg, own, "feature", {"module": "crm"})
    line = hitl.workspace_digest_text(own)
    assert 0 < len(line) <= hitl.MESSAGE_MAX and line.startswith("4 items need you (4 hold a run): " + "a" * 90 + " 1, ")
    hitl.notify_run(cfg, run, "x")
    hitl.notify_run(cfg, run, "y")
    assert len(macos_calls) == 1
    assert _kept_line().read_text(encoding="utf-8") == line


def test_workspace_digest_on_sends_the_merged_line_whatever_the_pack_says(tmp_path, repo, run, cfg, macos_calls):
    _merged_workspace(tmp_path)
    assert cfg.hitl.get("digest") is not True
    hitl.notify_run(cfg, run, "blocked: something specific")
    (call,) = macos_calls
    assert call[2].endswith('with title "agentflow"')
    assert "Nothing needs you; 1 run progressing in 3 repositories" in call[2] and "something specific" not in call[2]
    assert _kept_line().read_text(encoding="utf-8") == "Nothing needs you; 1 run progressing in 3 repositories"
    hitl.notify_run(cfg, run, "blocked: something else")
    assert len(macos_calls) == 1  # the same line again sends nothing
    assert not (pack.state_dir(repo) / "notify_digest.txt").exists()


def test_notify_counts_the_entries_that_delivered(cfg, macos_calls, monkeypatch):
    monkeypatch.delenv("AGENTFLOW_TEST_NO_HOOK", raising=False)
    two = dataclasses.replace(cfg, hitl={**cfg.hitl, "notify": [
        {"type": "macos"}, {"type": "webhook", "url_env": "AGENTFLOW_TEST_NO_HOOK"}, {"type": "macos", "enabled": False}]})
    assert notify(two, "t", "m") == 1
    assert notify(dataclasses.replace(cfg, hitl={**cfg.hitl, "notify": []}), "t", "m") == 0


def test_a_line_no_notifier_delivered_is_not_kept(tmp_path, repo, run, cfg, macos_calls):
    _merged_workspace(tmp_path)
    silent = dataclasses.replace(cfg, hitl={**cfg.hitl, "notify": [{"type": "macos", "enabled": False}]})
    hitl.notify_run(silent, run, "x")
    assert macos_calls == [] and not _kept_line().exists()
    hitl.notify_run(cfg, run, "x")
    assert len(macos_calls) == 1 and "in 3 repositories" in macos_calls[0][2]


def test_a_merged_digest_that_cannot_be_built_sends_the_plain_message(tmp_path, repo, run, cfg, macos_calls, monkeypatch):
    _merged_workspace(tmp_path)

    def broken(repo):
        raise RuntimeError("this repository's runs are unreadable")

    monkeypatch.setattr(hitl, "workspace_digest_text", broken)
    hitl.notify_run(cfg, run, "blocked: something specific")  # never raises
    assert len(macos_calls) == 1 and "blocked: something specific" in macos_calls[0][2]
    (failed,) = [e for e in run.events.read() if e["type"] == "notify_failed"]
    assert failed["notifier"] == "digest" and "unreadable" in failed["error"]
    assert not _kept_line().exists()


def test_the_run_s_own_unreadable_runs_raise_out_of_the_merged_digest(tmp_path, repo, run, cfg, monkeypatch):
    _merged_workspace(tmp_path)

    def broken(repo):
        raise RuntimeError("unreadable")

    monkeypatch.setattr(hitl, "list_runs", broken)
    with pytest.raises(RuntimeError, match="unreadable"):
        hitl.workspace_digest_text(repo)


@pytest.mark.parametrize("user_file", ["digest_off", "unreadable"])
def test_digest_off_or_an_unreadable_user_file_keeps_the_per_repository_digest(tmp_path, repo, run, cfg, macos_calls,
                                                                              user_file):
    if user_file == "digest_off":
        _merged_workspace(tmp_path, digest=False)
    else:
        write_user_file(": : :")
    hitl.notify_run(_digest_cfg(cfg), run, "x")
    assert len(macos_calls) == 1 and "Nothing needs you; 1 run progressing" in macos_calls[0][2]
    assert "repositor" not in macos_calls[0][2]
    assert (pack.state_dir(repo) / "notify_digest.txt").read_text(encoding="utf-8") == "Nothing needs you; 1 run progressing"
    assert not _kept_line().exists()


def test_claim_lost_names_the_three_ways_on(run, cfg):
    text = hitl._how_to_go_on(run, "claim_lost", "build/build", False, cfg)
    assert f"agentflow approve {run.id} build/build" in text and "claims" in text
    assert "agentflow backlog release <id>" in text
    assert f"agentflow cancel {run.id}" in text


def test_claim_lost_names_the_item_to_release(run, cfg):
    run.emit("item_claimed", item="b", ref="refs/agentflow/claims/b", sha="a" * 40, phase="build", status="running")
    text = hitl._how_to_go_on(run, "claim_lost", "build/claim", False, cfg)
    assert "`agentflow backlog release b` frees" in text and "<id>" not in text
