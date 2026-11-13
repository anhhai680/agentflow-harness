"""The rules of a reconcile round (`agentflow/doubt.py`): numbering, dispositions, stop rules and page lines.
Pure tests on fixed event lists, in `test_signals._ev` style; the page tests use a real run for its event log."""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

from agentflow import doubt
from agentflow.config import load_config
from conftest import ENGINE, with_doubt

HIGH = {"severity": "high", "file": "docs/x.md", "line": 3, "text": "no rollback case"}
LOW = {"severity": "low", "file": "docs/x.md", "line": 9, "text": "typo"}
SPEC_REVIEW = "define/spec_review"
SPEC_RECONCILE = "define/spec_reconcile"


def log(*events: dict) -> list[dict]:
    """The events, numbered in the order given (the log's `seq`)."""
    return [{"seq": n, "ts": "2026-11-13T09:00:00.000+00:00", "by": "runner", **event} for n, event in enumerate(events, 1)]


def started(phase: str = "define") -> dict:
    return {"type": "phase_started", "phase": phase}


def human_reject(phase: str = "define") -> dict:
    return {"type": "phase_rejected", "phase": phase, "round": 1, "by": "ana", "note": "no"}


def round_opened(findings: list[dict], rnd: int, phase: str = "define") -> dict:
    return {"type": "phase_rejected", "phase": phase, "round": 1, "by": "runner", "kind": doubt.RECONCILE,
            "keys": [SPEC_RECONCILE, SPEC_REVIEW], "findings": doubt.numbered(findings, rnd), "digests": {SPEC_RECONCILE: "a"}}


def reviewed(findings: list[dict], key: str = SPEC_REVIEW) -> list[dict]:
    """A review that finished and the findings the settle step recorded for it."""
    return [{"type": "step_finished", "key": key, "status": "done", "summary": ""},
            {"type": "step_findings", "key": key, "findings": [{**f, "key": key} for f in findings], "tree": "t"}]


def reconciled(dispositions: list[dict], rnd: int, before: str = "a", after: str = "b", unresolved: list[str] | None = None,
               phase: str = "define") -> dict:
    return {"type": "findings_reconciled", "key": SPEC_RECONCILE, "phase": phase, "round": rnd, "dispositions": dispositions,
            "unresolved": unresolved or [], "before": before, "after": after}


def disp(finding: str, cls: str, note: str = "ok") -> dict:
    return {"finding": finding, "class": cls, "note": note}


@pytest.fixture
def dcfg(tmp_cfg):
    return load_config(tmp_cfg(with_doubt))


# --------------------------------------------------------------------------- numbering and keys


def test_numbering_takes_strings_and_any_case():
    got = doubt.numbered(["a plain string", {"severity": "HIGH", "text": "y", "id": "evil"}, {"text": "z"}], 2)
    assert [f["id"] for f in got] == ["R2-F1", "R2-F2", "R2-F3"]
    assert got[0] == {"text": "a plain string", "id": "R2-F1"}
    assert got[1]["severity"] == "HIGH" and got[1]["text"] == "y"


def test_flagged_keys_recurse_into_groups(tmp_cfg):
    def nest(data: dict) -> None:
        with_doubt(data)
        steps = data["phases"]["define"]["steps"]
        at = next(i for i, step in enumerate(steps) if step["id"] == "spec_review")
        steps[at] = {"id": "grp", "lanes": ["feature"], "steps": [steps[at]]}

    cfg = load_config(tmp_cfg(nest))
    assert doubt.flagged_keys(cfg.phases["define"], "feature") == ["define/grp/spec_review"]
    assert doubt.round_keys(cfg.phases["define"], "feature") == ["define/spec_reconcile", "define/grp", "define/grp/spec_review"]
    assert doubt.flagged_keys(cfg.phases["define"], "bug") == []  # the pair is tagged `lanes: [feature]`


def test_with_ancestors_adds_parent_keys():
    assert doubt.with_ancestors(["define/a", "plan/g/r", "plan/g/h/r"]) == [
        "define/a", "plan/g", "plan/g/r", "plan/g/h", "plan/g/h/r"]
    assert doubt.with_ancestors([]) == []


def test_reconcile_steps_are_the_top_level_ones_of_the_lane(dcfg):
    assert [s.id for s in doubt.reconcile_steps(dcfg.phases["define"], "feature")] == ["spec_reconcile"]
    assert [s.id for s in doubt.reconcile_steps(dcfg.phases["plan"], "feature")] == ["plan_reconcile"]
    assert doubt.reconcile_steps(dcfg.phases["define"], "bug") == []
    assert doubt.reconcile_steps(dcfg.phases["review"], "feature") == []
    assert doubt.round_keys(dcfg.phases["define"], "feature") == ["define/spec_reconcile", "define/spec_review"]


def test_is_doubt_reviewer_only_with_a_reconcile_step(tmp_cfg):
    def approve_review(data: dict) -> None:
        with_doubt(data)
        data["phases"]["review"]["needs_approval"] = True

    cfg = load_config(tmp_cfg(approve_review))
    spec_review = next(s for s in cfg.phases["define"].steps if s.id == "spec_review")
    assert doubt.is_doubt_reviewer(cfg, "define", spec_review, "feature") is True
    assert doubt.is_doubt_reviewer(cfg, "define", spec_review, "bug") is False
    branch = next(s for s in cfg.phases["review"].steps if s.id == "branch_review")
    assert branch.blocks_on_findings and branch.role == "review" and cfg.phases["review"].needs_approval
    assert doubt.is_doubt_reviewer(cfg, "review", branch, "feature") is False  # no reconcile step in that phase
    verify = next(s for s in cfg.phases["verify"].steps if s.id == "verify")
    assert doubt.is_doubt_reviewer(cfg, "verify", verify, "feature") is False  # a build role, not a review


def test_in_reconcile_round():
    assert doubt.in_reconcile_round({"refine": {"phase": "define", "round": 1, "note": "", "kind": doubt.RECONCILE}}, "define")
    assert not doubt.in_reconcile_round({"refine": {"phase": "define", "round": 1, "note": ""}}, "define")
    assert not doubt.in_reconcile_round({"refine": {"phase": "plan", "kind": doubt.RECONCILE}}, "define")
    assert not doubt.in_reconcile_round({"refine": None}, "define") and not doubt.in_reconcile_round({}, "define")


# --------------------------------------------------------------------------- dispositions


IDS = {"R1-F1", "R1-F2", "R1-F3"}


def test_well_formed_dispositions_are_kept_with_a_one_line_note():
    got = doubt.parse_dispositions([disp("R1-F1", "noise", "line one\n\nline  two‮"), disp("R1-F2", "actionable", "x" * 900)], IDS)
    assert got[0] == doubt.Disposition("R1-F1", "noise", "line one line two")
    assert got[1].cls == "actionable" and len(got[1].note) <= doubt.NOTE_MAX


@pytest.mark.parametrize("raw", [
    None, "R1-F1", {"finding": "R1-F1"}, [None, 3, "R1-F1"],
    [{"finding": ["R1-F1"], "class": "noise", "note": "n"}],  # a list `finding`
    [disp("R1-F9", "noise")],  # an id not in the round
    [disp("R1-F1", "wishful")],  # an unknown class
    [disp("R1-F1", ["noise"])],
    [{"finding": "R1-F1", "class": "noise", "note": 7}],  # a non-string note
    [{"finding": "R1-F1", "class": "noise"}],
    [disp("R1-F1", "noise"), disp("R1-F1", "actionable")],  # a duplicated id drops both
    [disp("R1-F1", "noise"), disp("R1-F1", "wishful")],  # ... even when the second is malformed
])
def test_malformed_dispositions_are_unresolved(raw):
    kept = doubt.parse_dispositions(raw, {"R1-F1"})
    assert kept == []
    assert doubt.unresolved(["R1-F1"], kept, set()) == ["R1-F1"]


def test_a_duplicate_drops_only_its_own_id():
    kept = doubt.parse_dispositions([disp("R1-F1", "noise"), disp("R1-F1", "noise"), disp("R1-F2", "trade_off")], IDS)
    assert [d.finding for d in kept] == ["R1-F2"]


def test_a_human_decision_is_unresolved_until_a_human_answered_it():
    kept = doubt.parse_dispositions([disp("R1-F1", "human_decision"), disp("R1-F2", "actionable")], IDS)
    assert doubt.unresolved(["R1-F1", "R1-F2", "R1-F3"], kept, set()) == ["R1-F1", "R1-F3"]
    assert doubt.unresolved(["R1-F1", "R1-F2", "R1-F3"], kept, {"R1-F1"}) == ["R1-F3"]
    assert doubt.unresolved(["R1-F1"], kept, {"R1-F2"}) == ["R1-F1"]  # the answer must be to that finding's question


def question(qid: str, kind: str = "boundary") -> dict:
    return {"id": qid, "question": "q", "options": [], "evidence": [], "kind": kind}


def test_answered_boundary_is_per_round_kind_and_finding():
    key = SPEC_RECONCILE
    events = log(
        {"type": "hitl_raised", "kind": "needs_human", "key": key, "phase": "define", "questions": [question("R0-F1")]},  # 1: an earlier round
        {"type": "hitl_answered", "by": "ana", "key": key, "answers": {"R0-F1": "A"}},  # 2
        {"type": "phase_rejected", "phase": "define", "round": 1, "by": "runner", "kind": doubt.RECONCILE},  # 3: the round
        {"type": "hitl_raised", "kind": "needs_human", "key": key, "phase": "define",
         "questions": [question("R1-F1"), question("R1-F2", "spec_gap"), question("R1-F3"), question("R1-F4"), question("R1-F5")]},  # 4
        {"type": "hitl_answered", "by": "runner", "key": key, "answers": {"R1-F1": "A", "R1-F2": "A", "R1-F3": "A", "R1-F4": "A", "R1-F5": "A"}},
        {"type": "hitl_answered", "by": "ana", "key": key, "answers": {"R1-F2": "A", "R1-F3": "A"}},  # a human, to the spec_gap one and R1-F3
        {"type": "hitl_answered", "by": "ana", "key": "define/other", "answers": {"R1-F4": "A"}},  # another step's
        {"type": "hitl_answered", "by": "ana", "key": key, "answers": {"R1-F5": ""}},  # an empty answer is none
    )
    assert doubt.answered_boundary(events, key, since_seq=3) == {"R1-F3"}
    assert doubt.answered_boundary(events, key, since_seq=0) == {"R0-F1", "R1-F3"}
    assert doubt.answered_boundary(events[:5], key, since_seq=3) == set()  # only the runner has answered so far


def test_answered_boundary_needs_the_answer_after_the_question():
    key = SPEC_RECONCILE
    events = log(
        {"type": "hitl_answered", "by": "ana", "key": key, "answers": {"R1-F1": "A"}},
        {"type": "hitl_raised", "kind": "needs_human", "key": key, "phase": "define", "questions": [question("R1-F1")]},
    )
    assert doubt.answered_boundary(events, key, since_seq=0) == set()


# --------------------------------------------------------------------------- since the last human round


def test_rules_see_only_the_phase_since_its_last_human_round():
    events = log(
        started(), {"type": "step_started", "key": "define/draft", "phase": "define"},
        round_opened([HIGH], 1), human_reject(), {"type": "step_finished", "key": "define/draft"},
        started("plan"), {"type": "step_finished", "key": "plan/plan"},
        round_opened([HIGH], 1), {"type": "step_finished", "key": "define/spec_review"},
    )
    got = doubt.since_human_round(events, "define")
    assert [e["type"] for e in got] == ["step_finished", "phase_rejected", "step_finished"]
    assert all(e["seq"] > 4 for e in got)
    assert [e["seq"] for e in doubt.since_human_round(log(started(), {"type": "x", "phase": "define"}, started("plan")), "define")] == [2]
    assert [e["seq"] for e in doubt.since_human_round(log({"type": "x", "phase": "define"}), "define")] == [1]


# --------------------------------------------------------------------------- the stop rules


def first_review(findings: list[dict]) -> list[dict]:
    return [started(), *reviewed(findings)]


def test_stop_reason_order(dcfg):
    define = dcfg.phases["define"]

    def reason(*events: dict, phase=define, lane="feature"):
        return doubt.stop_reason(dcfg, log(*events), phase, lane)

    # None: a first review with a blocking finding and no rounds used
    assert reason(*first_review([HIGH])) is None
    # no_reconcile_step: the lane has none (and it beats every later rule)
    assert reason(*first_review([HIGH]), lane="bug") == "no_reconcile_step"
    assert doubt.stop_reason(dcfg, log(*first_review([HIGH])), dcfg.phases["review"], "feature") == "no_reconcile_step"
    # unchanged: the last findings_reconciled has before == after
    unchanged = [*first_review([HIGH]), round_opened([HIGH], 1), reconciled([disp("R1-F1", "noise")], 1, "a", "a")]
    assert reason(*unchanged) == "unchanged"
    assert reason(*unchanged[:-1], reconciled([disp("R1-F1", "actionable")], 1, "a", "b")) is None
    # disputed: the review after round 1 disputes a noise disposition of round 1 (beats unchanged only when changed)
    changed = [*first_review([HIGH]), round_opened([HIGH], 1), reconciled([disp("R1-F1", "noise")], 1)]
    disputing = {**HIGH, "disputes": "R1-F1", "text": "still wrong"}
    assert reason(*changed, *reviewed([disputing])) == "disputed"
    assert reason(*unchanged, *reviewed([disputing])) == "unchanged"
    # theater: two rounds, every blocking finding a noise, trade_off or contract_misread
    two = [*first_review([HIGH]), round_opened([HIGH], 1), reconciled([disp("R1-F1", "noise")], 1),
           *reviewed([HIGH]), round_opened([HIGH], 2), reconciled([disp("R2-F1", "trade_off")], 2), *reviewed([HIGH])]
    assert reason(*two) == "theater"
    # bounded: the reconcile rounds are at the limit (2 in the python pack), with an actionable fix in each
    bounded = [*first_review([HIGH]), round_opened([HIGH], 1), reconciled([disp("R1-F1", "actionable")], 1),
               *reviewed([HIGH]), round_opened([HIGH], 2), reconciled([disp("R2-F1", "actionable")], 2), *reviewed([HIGH])]
    assert reason(*bounded) == "bounded"
    assert reason(*bounded[:7]) is None  # one round of two used


def test_a_dispute_of_an_unknown_id_does_not_stop(dcfg):
    define = dcfg.phases["define"]
    events = [*first_review([HIGH]), round_opened([HIGH], 1), reconciled([disp("R1-F1", "noise")], 1)]
    for value in ("R9-F9", "F1", ["R1-F1"], 7, None, ""):
        stray = {**HIGH, "disputes": value}
        assert doubt.stop_reason(dcfg, log(*events, *reviewed([stray])), define, "feature") is None, value


def test_a_dispute_of_an_actionable_or_misread_disposition_does_not_stop(dcfg):
    define = dcfg.phases["define"]
    for cls in ("actionable", "contract_misread", "human_decision"):
        events = [*first_review([HIGH]), round_opened([HIGH], 1), reconciled([disp("R1-F1", cls)], 1),
                  *reviewed([{**HIGH, "disputes": "R1-F1"}])]
        assert doubt.stop_reason(dcfg, log(*events), define, "feature") is None, cls


def test_a_first_review_cannot_dispute(dcfg):
    events = log(*first_review([{**HIGH, "disputes": "R1-F1"}]))
    assert doubt.stop_reason(dcfg, events, dcfg.phases["define"], "feature") is None


def test_rules_restart_after_a_human_round(dcfg):
    events = log(
        *first_review([HIGH]), round_opened([HIGH], 1), reconciled([disp("R1-F1", "actionable")], 1), *reviewed([HIGH]),
        round_opened([HIGH], 2), reconciled([disp("R2-F1", "actionable")], 2), *reviewed([HIGH]),
        human_reject(), *reviewed([HIGH]),
    )
    assert doubt.stop_reason(dcfg, events[: len(events) - 2], dcfg.phases["define"], "feature") is None  # the human's reject came last
    assert doubt.stop_reason(dcfg, events, dcfg.phases["define"], "feature") is None  # not `bounded`


def test_theater_shows_at_the_default_limit(tmp_cfg):
    cfg = load_config(tmp_cfg(lambda data: (with_doubt(data), data["autonomy"].update(finding_rounds=2))))
    events = log(*first_review([HIGH]), round_opened([HIGH], 1), reconciled([disp("R1-F1", "trade_off")], 1), *reviewed([HIGH]),
                 round_opened([HIGH], 2), reconciled([disp("R2-F1", "trade_off")], 2), *reviewed([HIGH]))
    assert doubt.stop_reason(cfg, events, cfg.phases["define"], "feature") == "theater"


def test_a_round_with_any_fix_or_question_does_not_count_toward_theater(dcfg):
    define = dcfg.phases["define"]
    two = {**HIGH, "text": "other"}
    for second in ([disp("R2-F1", "noise"), disp("R2-F2", "actionable")], [disp("R2-F1", "noise")],  # fixed one; the other unresolved
                   [disp("R2-F1", "noise"), disp("R2-F2", "human_decision")]):
        events = log(*first_review([HIGH]), round_opened([HIGH], 1), reconciled([disp("R1-F1", "noise")], 1), *reviewed([HIGH, two]),
                     round_opened([HIGH, two], 2), reconciled(second, 2), *reviewed([HIGH, two]))
        assert doubt.stop_reason(dcfg, events, define, "feature") == "bounded", second


def test_ids_are_per_round():
    assert [f["id"] for f in doubt.numbered([HIGH, LOW], 1)] == ["R1-F1", "R1-F2"]
    assert [f["id"] for f in doubt.numbered([HIGH, LOW], 2)] == ["R2-F1", "R2-F2"]
    # a disposition of round 2 for an id round 1 also had cannot be taken for round 1's
    assert doubt.parse_dispositions([disp("R1-F1", "noise")], {"R2-F1"}) == []


# --------------------------------------------------------------------------- the note and the reviewer's history


def test_the_reconcile_note_numbers_the_findings_and_carries_the_history():
    first = doubt.numbered([HIGH], 1)
    note = doubt.reconcile_note(first, [])
    assert "R1-F1" in note and "[high] docs/x.md:3: no rollback case" in note
    history = [round_opened([HIGH], 1), reconciled([disp("R1-F1", "noise", "Task 3 covers it")], 1)]
    second = doubt.reconcile_note(doubt.numbered([{**HIGH, "text": "again"}], 2), history)
    assert "R2-F1" in second and "again" in second
    assert "R1-F1" in second and "noise" in second and "Task 3 covers it" in second
    assert "Task 3" not in note


def test_earlier_findings_for_the_reviewer():
    assert doubt.earlier_for_review([], "define") == ""
    assert doubt.earlier_for_review(log(*first_review([HIGH])), "define") == ""
    events = log(*first_review([HIGH]), round_opened([HIGH], 1), reconciled([disp("R1-F1", "trade_off", "kept; see Risks")], 1))
    got = doubt.earlier_for_review(events, "define")
    assert got.startswith("Earlier reviews of this artifact found these, and the reconcile step answered as shown. "
                          "Check each against the artifact as it is now; dispute an answer you think wrong with `disputes`.")
    assert "- R1-F1 [high] no rollback case → trade_off: kept; see Risks" in got.splitlines()
    after_human = log(*events, human_reject())
    assert doubt.earlier_for_review(after_human, "define") == ""


# --------------------------------------------------------------------------- artifacts


def test_artifact_digest(tmp_path):
    import hashlib

    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "a.md").write_bytes(b"hello")
    assert doubt.artifact_digest(tmp_path, "docs/a.md") == hashlib.sha256(b"hello").hexdigest()
    assert doubt.artifact_digest(tmp_path, "docs/missing.md") == ""
    assert doubt.artifact_digest(tmp_path, "docs") == ""  # not a regular file
    (tmp_path / "docs" / "link.md").symlink_to(tmp_path / "docs" / "a.md")
    assert doubt.artifact_digest(tmp_path, "docs/link.md") == ""
    (tmp_path / "docs" / "big.md").write_bytes(b"x" * (doubt.ARTIFACT_MAX + 1))
    assert doubt.artifact_digest(tmp_path, "docs/big.md") == ""
    assert doubt.artifact_digest(tmp_path, "../a.md") == "" and doubt.artifact_digest(tmp_path, "/etc/hosts") == ""


# --------------------------------------------------------------------------- the approval page


def feed(run, events: list[dict]) -> None:
    for event in events:
        fields = {k: v for k, v in event.items() if k not in ("type", "seq", "ts", "by")}
        run.emit(event["type"], by=event.get("by", "runner"), **fields)


def page(flow_repo, edit, events: list[dict], phase: str = "define") -> list[str]:
    cfg = flow_repo.configure(autopilot=True, edit=edit)
    run = flow_repo.new_run()
    feed(run, events)
    return doubt.page_lines(cfg, run, phase, "feature")


def no_note(data: dict) -> None:
    with_doubt(data)
    data["phases"]["plan"].pop("approval_note", None)


def note_when(when) -> object:
    def edit(data: dict) -> None:
        with_doubt(data)
        data["phases"]["define"]["approval_note"] = {"when": when, "human_skill": "doubt-driven-development"}

    return edit


def test_page_lines_are_empty_when_the_review_never_ran(flow_repo):
    assert page(flow_repo, with_doubt, [started()]) == []
    assert page(flow_repo, with_doubt, [started(), {"type": "step_skipped", "key": SPEC_REVIEW, "phase": "define", "reason": "x"}]) == []
    plain = flow_repo.configure(autopilot=True)  # no doubt steps in the phase
    run = flow_repo.new_run()
    run.emit("phase_started", phase="define")
    run.emit("step_finished", key=SPEC_REVIEW, status="done", summary="")
    assert doubt.page_lines(plain, run, "define", "feature") == []


def test_a_low_finding_alone_is_listed_with_the_summary(flow_repo):
    lines = page(flow_repo, no_note, [started(), *reviewed([LOW])])
    assert lines[0] == ("Doubt review: 1 review(s) and 0 reconcile round(s), single-model; 0 fixed, 0 trade-off(s) recorded, "
                        "0 still open. A cross-model opinion is yours to run.")
    assert len(lines) == 2 and "[low]" in lines[1] and "typo" in lines[1]


def test_fixed_needs_a_changed_artifact_and_an_undisputed_review(flow_repo):
    fixed = [started(), *reviewed([HIGH]), round_opened([HIGH], 1), reconciled([disp("R1-F1", "actionable", "Added the case")], 1),
             *reviewed([LOW])]
    lines = page(flow_repo, no_note, fixed)
    assert "R1-F1 [high] no rollback case — actionable: Added the case (fixed)" in lines
    assert lines[0].startswith("Doubt review: 2 review(s) and 1 reconcile round(s), single-model; 1 fixed, 0 trade-off(s) recorded, 0 still open.")
    # the same claim on an artifact that did not change is never "fixed"
    same = [started(), *reviewed([HIGH]), round_opened([HIGH], 1), reconciled([disp("R1-F1", "actionable")], 1, "a", "a"),
            {"type": "step_unchanged", "key": SPEC_REVIEW, "reason": "the artifact did not change"}]
    lines = page(flow_repo, no_note, same)
    assert any(line.endswith("(claimed, artifact unchanged)") for line in lines)
    assert not any("(fixed)" in line for line in lines)
    assert "The reconcile step left the artifact as it was, so it was not reviewed again." in lines
    assert "1 still open." in lines[0]
    # a changed artifact with no review after it is only claimed
    pending = [started(), *reviewed([HIGH]), round_opened([HIGH], 1), reconciled([disp("R1-F1", "actionable")], 1)]
    assert page(flow_repo, no_note, pending)[-1].endswith("(claimed)")
    # a review after it that raises the finding again with `disputes` is not a confirmation
    disputed = [*pending, *reviewed([{**HIGH, "disputes": "R1-F1"}])]
    assert not any("(fixed)" in line for line in page(flow_repo, no_note, disputed))


def test_page_lines_show_every_status(flow_repo):
    findings = [{**HIGH, "text": f"f{n}"} for n in range(1, 7)]
    dispositions = [disp("R1-F1", "trade_off", "kept"), disp("R1-F2", "noise"), disp("R1-F3", "human_decision"),
                    disp("R1-F4", "contract_misread"), disp("R1-F5", "actionable")]  # R1-F6 has none
    events = [started(), *reviewed(findings), round_opened(findings, 1),
              reconciled(dispositions, 1, unresolved=["R1-F3", "R1-F6"]),
              *reviewed([{**HIGH, "text": "f2 again", "disputes": "R1-F2"}])]
    lines = page(flow_repo, no_note, events)
    by_id = {line.split(" ", 1)[0]: line for line in lines if line.startswith("R")}
    assert by_id["R1-F1"].endswith("— trade_off: kept (recorded)")
    assert by_id["R1-F2"].endswith("(disputed)")
    assert by_id["R1-F3"].endswith("(asked a human)")
    assert by_id["R1-F4"].endswith("(recorded)")
    assert by_id["R1-F5"].endswith("(fixed)")  # actionable, the artifact changed, and the review did not dispute it
    assert by_id["R1-F6"].endswith("(unresolved)") and "—" not in by_id["R1-F6"]
    assert "The reviewer disputes a disposition: you decide." in lines
    assert "R2-F1" not in by_id  # the dispute is the same issue as R1-F2: listed once, as disputed
    assert "3 still open." in lines[0]  # R1-F2 (disputed), R1-F3 (asked a human), R1-F6 (unresolved)


def test_a_disputed_finding_is_counted_once(flow_repo):
    pending = [started(), *reviewed([HIGH]), round_opened([HIGH], 1), reconciled([disp("R1-F1", "noise", "x")], 1)]
    lines = page(flow_repo, no_note, [*pending, *reviewed([{**HIGH, "text": "still no rollback", "disputes": "R1-F1"}])])
    assert [line.split(" ", 1)[0] for line in lines[1:] if line.startswith("R")] == ["R1-F1"]
    assert lines[-1].endswith("(disputed)")
    assert "1 still open." in lines[0]
    # a blocking finding that disputes nothing valid is still listed as open
    lines = page(flow_repo, no_note, [*pending, *reviewed([{**HIGH, "text": "new problem", "disputes": "R9-F9"}])])
    assert lines[-1].startswith("R2-F1") and lines[-1].endswith("(still open)")
    assert "1 still open." in lines[0]  # only the new problem: R1-F1 (noise) was recorded, and a review ran after it


def test_page_lines_say_a_noise_finding_is_still_open_while_the_artifact_is_unchanged(flow_repo):
    events = [started(), *reviewed([HIGH]), round_opened([HIGH], 1), reconciled([disp("R1-F1", "noise", "Task 3 covers it")], 1, "a", "a"),
              {"type": "step_unchanged", "key": SPEC_REVIEW, "reason": "x"}]
    lines = page(flow_repo, no_note, events)
    assert "R1-F1 [high] no rollback case — noise: Task 3 covers it (still open)" in lines


def test_page_lines_name_the_stop_reasons(flow_repo):
    bounded = [started(), *reviewed([HIGH]), round_opened([HIGH], 1), reconciled([disp("R1-F1", "actionable")], 1), *reviewed([HIGH]),
               round_opened([HIGH], 2), reconciled([disp("R2-F1", "actionable")], 2), *reviewed([HIGH])]
    lines = page(flow_repo, no_note, bounded)
    assert "The reconcile rounds are used up." in lines
    clean = [*bounded[:-2], *reviewed([LOW])]
    assert "The reconcile rounds are used up." not in page(flow_repo, no_note, clean)  # nothing is left open
    theater = [started(), *reviewed([HIGH]), round_opened([HIGH], 1), reconciled([disp("R1-F1", "trade_off")], 1), *reviewed([HIGH]),
               round_opened([HIGH], 2), reconciled([disp("R2-F1", "noise")], 2), *reviewed([HIGH])]
    assert ("Two rounds found blocking problems and none was fixed: the review may be validating, not doubting."
            in page(flow_repo, no_note, theater))


def test_page_offers_cross_model_always_and_names_the_skill_only_when_configured(flow_repo):
    events = [started(), *reviewed([LOW])]
    assert page(flow_repo, no_note, events)[0].endswith("A cross-model opinion is yours to run.")
    holds = {"check": "file_exists", "path": "CONSTRAINTS.md"}
    named = page(flow_repo, note_when(holds), events)[0]
    assert named.endswith("A cross-model opinion is yours to run. For example: /doubt-driven-development.")
    never = {"check": "file_exists", "path": "no-such-file.md"}
    assert page(flow_repo, note_when(never), events)[0].endswith("A cross-model opinion is yours to run.")
    unknown = {"check": "status_line", "file": "CONSTRAINTS.md", "approved": "approved", "pending": "pending",
               "on_unknown": "ask_human"}
    assert page(flow_repo, note_when(unknown), events)[0].endswith("For example: /doubt-driven-development.")


NOISE_UNCHANGED = [started(), *reviewed([HIGH, LOW]), round_opened([HIGH], 1),
                   reconciled([disp("R1-F1", "noise", "Task 3 covers it")], 1, "a", "a"),
                   {"type": "step_unchanged", "key": SPEC_REVIEW, "reason": "x"}]
FIXED_THEN_CLEAN = [started(), *reviewed([HIGH, LOW]), round_opened([HIGH], 1), reconciled([disp("R1-F1", "actionable")], 1),
                    *reviewed([])]


@pytest.mark.parametrize("events", [NOISE_UNCHANGED, FIXED_THEN_CLEAN], ids=["noise-unchanged", "fixed-then-clean"])
def test_a_low_finding_of_an_earlier_review_stays_on_the_page(flow_repo, events):
    lines = page(flow_repo, no_note, events)
    assert sum("typo" in line for line in lines) == 1
    assert any(line.startswith("[low]") and line.endswith("(not blocking)") for line in lines)
    assert any(line.startswith("R1-F1") for line in lines)  # the blocking row behaves as before


@pytest.mark.parametrize("events", [NOISE_UNCHANGED, FIXED_THEN_CLEAN], ids=["noise-unchanged", "fixed-then-clean"])
def test_a_low_finding_of_an_earlier_review_reaches_the_handoff(flow_repo, events):
    cfg = flow_repo.configure(autopilot=True, edit=no_note)
    run = flow_repo.new_run()
    feed(run, events)
    risks = doubt.handoff_risks(cfg, run, "define", "feature")
    assert sum("typo" in risk for risk in risks) == 1


def test_a_low_finding_raised_again_is_listed_once(flow_repo):
    events = [started(), *reviewed([HIGH, LOW]), round_opened([HIGH], 1), reconciled([disp("R1-F1", "actionable")], 1),
              *reviewed([LOW])]
    assert sum("typo" in line for line in page(flow_repo, no_note, events)) == 1


def test_page_and_handoff_are_per_human_round(flow_repo):
    events = [started(), *reviewed([HIGH]), round_opened([HIGH], 1), reconciled([disp("R1-F1", "actionable")], 1), *reviewed([LOW]),
              human_reject(), *reviewed([LOW])]
    lines = page(flow_repo, no_note, events)
    assert lines[0].startswith("Doubt review: 1 review(s) and 0 reconcile round(s)")
    assert not any(line.startswith("R1-F1") for line in lines)


def test_handoff_risks_list_what_is_open_what_is_low_and_the_trade_offs(flow_repo):
    cfg = flow_repo.configure(autopilot=True, edit=no_note)
    run = flow_repo.new_run()
    two = {**HIGH, "text": "second"}
    events = [started(), *reviewed([HIGH, two]), round_opened([HIGH, two], 1),
              reconciled([disp("R1-F1", "trade_off", "kept the single writer"), disp("R1-F2", "noise")], 1, "a", "a"),
              {"type": "step_unchanged", "key": SPEC_REVIEW, "reason": "x"}]
    feed(run, events)
    risks = doubt.handoff_risks(cfg, run, "define", "feature")
    text = "\n".join(risks)
    assert "R1-F2" in text and "second" in text  # still open
    assert "kept the single writer" in text  # the trade-off note
    assert all("\n" not in risk for risk in risks)
    plain = flow_repo.configure(autopilot=True)
    assert doubt.handoff_risks(plain, run, "define", "feature") == []


# --------------------------------------------------------------------------- imports


def test_doubt_does_not_import_autonomy_at_module_level():
    proc = subprocess.run([sys.executable, "-c", "import agentflow.flow_control, agentflow.doubt, agentflow.conditions"],
                          cwd=ENGINE.parent, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    tree = ast.parse(Path(doubt.__file__).read_text(encoding="utf-8"))
    imported = set()
    for node in tree.body:  # module level only
        if isinstance(node, ast.Import):
            imported |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add(("." * node.level) + (node.module or ""))
            imported |= {("." * node.level) + (node.module or "") + "." + alias.name for alias in node.names}
    assert not any("autonomy" in name for name in imported), imported
    assert not any(name.split(".")[-1] in ("pipeline", "flow_control", "lanes", "conditions") for name in imported), imported
