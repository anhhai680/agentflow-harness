"""Conformance, the pure part: which obligations apply, and what evidence in a run's events satisfies them."""

from __future__ import annotations

from dataclasses import replace

from agentflow.conditions import Ctx
from agentflow.config import FLOOR_ID, Evidence, Obligation
from agentflow.conformance import evaluate, last_code_change, missing_gates
from agentflow.policy import applicable, step_phase


def ev(seq: int, type_: str, **fields) -> dict:
    return {"seq": seq, "ts": "2026-11-10T09:00:00.000+00:00", "type": type_, **fields}


def ob(id_: str, *require: str, **kw) -> Obligation:
    kind_args = [item.partition(":")[::2] for item in require]
    return Obligation(id_, tuple(Evidence(k, a) for k, a in kind_args), **kw)


def finished(seq: int, key: str, status: str = "done") -> dict:
    return ev(seq, "step_finished", key=key, status=status, summary="")


def gates(seq: int, cfg, tree: str, set_name: str = "verify", **over) -> list[dict]:
    return [ev(seq + i, "gate_result", key="verify/verify", gate=g, passed=True, skipped=False, reason="", tree=tree,
               **over) for i, g in enumerate(cfg.gate_sets[set_name])]


def only(findings, id_):
    return next(f for f in findings if f.id == id_)


# --------------------------------------------------------------------------- gates


def test_gates_evidence_needs_the_final_tree(cfg):
    events = gates(1, cfg, "t1")
    floor = ob(FLOOR_ID, "gates:verify")
    assert evaluate(cfg, events, [floor], "t1")[0].status == "satisfied"
    assert evaluate(cfg, events, [floor], "t2")[0].status == "missing"


def test_gate_result_without_tree_is_not_evidence(cfg):
    events = [{k: v for k, v in e.items() if k != "tree"} for e in gates(1, cfg, "t1")]
    assert evaluate(cfg, events, [ob(FLOOR_ID, "gates:verify")], "t1")[0].status == "missing"
    assert evaluate(cfg, events, [ob(FLOOR_ID, "gates:verify")], "")[0].status == "missing"


def test_a_failed_gate_is_not_evidence_and_the_last_result_on_the_tree_counts(cfg):
    events = gates(1, cfg, "t1")
    first = events[0]["gate"]
    floor = [ob(FLOOR_ID, "gates:verify")]
    failed = [ev(100, "gate_result", key="verify/verify", gate=first, passed=False, skipped=False, reason="", tree="t1")]
    assert evaluate(cfg, events + failed, floor, "t1")[0].status == "missing"
    again = [ev(101, "gate_result", key="verify/verify", gate=first, passed=True, skipped=False, reason="", tree="t1")]
    assert evaluate(cfg, events + failed + again, floor, "t1")[0].status == "satisfied"


def test_a_skipped_gate_counts_with_its_reason(cfg):
    events = gates(1, cfg, "t1")
    events[0] = {**events[0], "passed": False, "skipped": True, "reason": "no web dir"}
    finding = evaluate(cfg, events, [ob(FLOOR_ID, "gates:verify")], "t1")[0]
    assert finding.status == "satisfied" and "no web dir" in finding.note
    assert finding.evidence == [e["seq"] for e in events]


# --------------------------------------------------------------------------- steps


def test_step_after_last_code_change(cfg):
    verified = ob("verified", "step:verify")
    late = [finished(10, "verify/verify"), ev(12, "step_committed", key="build/build", sha="a")]
    assert evaluate(cfg, late, [verified], "t")[0].status == "missing"
    early = [ev(8, "step_committed", key="build/build", sha="a"), finished(10, "verify/verify")]
    finding = evaluate(cfg, early, [verified], "t")[0]
    assert finding.status == "satisfied" and finding.evidence == [10]


def test_a_step_that_never_finished_done_is_missing(cfg):
    verified = ob("verified", "step:verify")
    assert evaluate(cfg, [], [verified], "t")[0].status == "missing"
    assert evaluate(cfg, [finished(3, "verify/verify", "failed")], [verified], "t")[0].status == "missing"


def test_blocking_findings_make_a_step_unclean_unless_a_human_approved(cfg):
    reviewed = ob("reviewed", "step:branch_review")
    key = "review/branch_review"
    blocking = [{"severity": "critical", "text": "x"}]
    base = [finished(5, key), ev(6, "step_findings", key=key, findings=blocking, tree="t")]
    assert evaluate(cfg, base, [reviewed], "t")[0].status == "missing"
    runner = base + [ev(7, "phase_approved", phase="review", by="runner")]
    assert evaluate(cfg, runner, [reviewed], "t")[0].status == "missing"
    human = base + [ev(7, "phase_approved", phase="review", by="me")]
    finding = evaluate(cfg, human, [reviewed], "t")[0]
    assert finding.status == "satisfied" and "human override" in finding.note
    before = [ev(4, "phase_approved", phase="review", by="me")] + base
    assert evaluate(cfg, before, [reviewed], "t")[0].status == "missing"
    other_phase = base + [ev(7, "phase_approved", phase="verify", by="me")]
    assert evaluate(cfg, other_phase, [reviewed], "t")[0].status == "missing"


def test_non_blocking_findings_and_findings_before_the_last_finish_do_not_count(cfg):
    reviewed = ob("reviewed", "step:branch_review")
    key = "review/branch_review"
    nit = [ev(5, "step_findings", key=key, findings=[{"severity": "nit", "text": "x"}], tree="t"), finished(6, key)]
    assert evaluate(cfg, nit, [reviewed], "t")[0].status == "satisfied"
    stale = [ev(5, "step_findings", key=key, findings=[{"severity": "critical", "text": "x"}], tree="t"), finished(6, key)]
    assert evaluate(cfg, stale, [reviewed], "t")[0].status == "satisfied"
    cleared = [finished(5, key), ev(6, "step_findings", key=key, findings=[{"severity": "critical", "text": "x"}]),
               ev(7, "step_findings", key=key, findings=[])]
    assert evaluate(cfg, cleared, [reviewed], "t")[0].status == "satisfied"


# --------------------------------------------------------------------------- approvals


def test_runner_approval_is_not_approval(cfg):
    planned = ob("plan_approved", "approval:plan")
    assert evaluate(cfg, [ev(2, "phase_approved", phase="plan", by="runner")], [planned], "t")[0].status == "missing"
    finding = evaluate(cfg, [ev(2, "phase_approved", phase="plan", by="me")], [planned], "t")[0]
    assert finding.status == "satisfied" and finding.evidence == [2]


def test_skip_if_skip_is_on_base(cfg):
    planned = ob("plan_approved", "approval:plan")
    skipped = evaluate(cfg, [ev(2, "phase_skipped", phase="plan", reason="plan exists on origin/main")], [planned], "t")[0]
    assert skipped.status == "satisfied" and "on base" in skipped.note
    marked = [ev(2, "phase_skipped", phase="plan", reason="marked done after build")]
    assert evaluate(cfg, marked, [planned], "t")[0].status == "missing"


# --------------------------------------------------------------------------- code changes


def test_conflicted_sync_is_a_code_change_and_clean_is_not(cfg):
    reviewed = ob("reviewed", "step:branch_review")
    key = "review/branch_review"
    conflicted = [finished(5, key), ev(6, "base_synced", conflicts=["a.py"])]
    assert evaluate(cfg, conflicted, [reviewed], "t")[0].status == "missing"
    clean = [finished(5, key), ev(6, "base_synced", conflicts=[])]
    assert evaluate(cfg, clean, [reviewed], "t")[0].status == "satisfied"
    assert last_code_change(conflicted) == 6 and last_code_change(clean) == 0


def test_last_code_change_is_the_last_commit_or_conflicted_sync(cfg):
    assert last_code_change([]) == 0
    events = [ev(2, "step_committed", key="k"), ev(5, "task_committed", key="k"), ev(7, "base_synced", conflicts=[]),
              ev(9, "gate_result", key="k")]
    assert last_code_change(events) == 5


# --------------------------------------------------------------------------- expectations


def test_red_then_green(cfg):
    rtg = ob("red_then_green", "expect:red_then_green")
    red = ev(3, "expectation_checked", key="build/regression_test", expect="red", ok=True, tree="a")
    green = ev(8, "expectation_checked", key="build/fix", expect="green", ok=True, tree="b")
    fix = ev(6, "step_committed", key="build/fix", sha="x")
    finding = evaluate(cfg, [red, fix, green], [rtg], "t")[0]
    assert finding.status == "satisfied" and finding.evidence == [3, 8]
    assert evaluate(cfg, [green], [rtg], "t")[0].status == "missing"
    late_commit = ev(9, "step_committed", key="build/fix", sha="y")
    assert evaluate(cfg, [red, fix, green, late_commit], [rtg], "t")[0].status == "missing"
    failed_green = {**green, "ok": False}
    assert evaluate(cfg, [red, fix, failed_green], [rtg], "t")[0].status == "missing"
    green_before_red = {**green, "seq": 2}
    assert evaluate(cfg, [green_before_red, red], [rtg], "t")[0].status == "missing"


# --------------------------------------------------------------------------- waived


def test_marked_done_waives_skipped_phases_but_never_the_floor(cfg):
    obligations = [ob("verified", "step:verify"), ob("reviewed", "step:branch_review"), ob(FLOOR_ID, "gates:verify")]
    events = [ev(4, "run_marked_done", by="me", phase="build"),
              ev(5, "phase_skipped", phase="verify", reason="marked done after build"),
              ev(6, "phase_skipped", phase="review", reason="marked done after build")]
    findings = evaluate(cfg, events, obligations, "t")
    for id_ in ("verified", "reviewed"):
        waived = only(findings, id_)
        assert waived.status == "waived" and waived.note == "waived by me" and waived.evidence == [4]
    assert only(findings, FLOOR_ID).status == "missing"


def test_waiver_needs_the_phase_skipped_after_the_mark_and_an_unmet_item_in_a_skipped_phase(cfg):
    verified = ob("verified", "step:verify")
    skipped_before = [ev(3, "phase_skipped", phase="verify", reason="marked done after build"),
                      ev(4, "run_marked_done", by="me", phase="build")]
    assert evaluate(cfg, skipped_before, [verified], "t")[0].status == "missing"
    no_skip = [ev(4, "run_marked_done", by="me", phase="build")]
    assert evaluate(cfg, no_skip, [verified], "t")[0].status == "missing"
    gated = ob("both", "step:verify", "gates:verify")
    marked = [ev(4, "run_marked_done", by="me", phase="build"), ev(5, "phase_skipped", phase="verify", reason="x")]
    assert evaluate(cfg, marked, [gated], "t")[0].status == "missing"
    expect = ob("red_then_green", "expect:red_then_green")
    skipped_build = [ev(4, "run_marked_done", by="me", phase="plan"), ev(5, "phase_skipped", phase="build", reason="x")]
    assert evaluate(cfg, skipped_build, [expect], "t")[0].status == "waived"


def test_a_satisfied_obligation_is_not_waived(cfg):
    verified = ob("verified", "step:verify")
    events = [finished(2, "verify/verify"), ev(4, "run_marked_done", by="me", phase="build"),
              ev(5, "phase_skipped", phase="verify", reason="x")]
    assert evaluate(cfg, events, [verified], "t")[0].status == "satisfied"


def test_every_item_must_hold_and_not_applicable_ones_are_reported(cfg):
    both = ob("both", "step:verify", "approval:plan")
    events = [finished(2, "verify/verify")]
    assert evaluate(cfg, events, [both], "t")[0].status == "missing"
    events.append(ev(3, "phase_approved", phase="plan", by="me"))
    finding = evaluate(cfg, events, [both], "t", [ob("red_then_green", "expect:red_then_green")])
    assert [f.status for f in finding] == ["satisfied", "not_applicable"]
    assert finding[1].id == "red_then_green" and finding[1].require == ["expect:red_then_green"]


# --------------------------------------------------------------------------- policy


def test_unknown_when_applies(cfg, tmp_path):
    cfg.conditions["diff_unknown"] = {"check": "changed_paths", "globs": ["x/**"]}
    guess = ob("guess", "step:verify", when="diff_unknown")
    cfg.policy = replace(cfg.policy, obligations={"guess": guess, FLOOR_ID: cfg.policy.obligations[FLOOR_ID]})
    ctx = Ctx(repo=tmp_path, cfg=cfg, values={}, base_ref="origin/does-not-exist")
    got = applicable(cfg, ctx, "feature")
    assert [o.id for o, _ in got] == ["guess", FLOOR_ID]
    assert got[1][1] == "always" and "diff_unknown" in got[0][1]


def test_lanes_filter_and_a_false_when_drops_the_obligation(cfg, tmp_path):
    ctx = Ctx(repo=tmp_path, cfg=cfg, values={}, base_ref="origin/main")
    ids = lambda lane: [o.id for o, _ in applicable(cfg, ctx, lane)]  # noqa: E731
    assert "red_then_green" in ids("bug") and "red_then_green" not in ids("feature")
    assert "plan_approved" in ids("feature") and "plan_approved" not in ids("bug")
    assert ids("feature")[-1] == FLOOR_ID and ids("bug")[-1] == FLOOR_ID


def test_step_phase_finds_a_step_at_any_nesting(cfg):
    assert step_phase(cfg, "verify") == "verify"
    assert step_phase(cfg, "branch_review") == "review"
    assert step_phase(cfg, "answers") == "define"  # nested in a group
    assert step_phase(cfg, "no_such_step") is None


# --------------------------------------------------------------------------- missing_gates


def test_missing_gates_lists_each_gate_once_in_set_order(cfg):
    findings = evaluate(cfg, [], [ob(FLOOR_ID, "gates:verify"), ob("built", "gates:build"), ob("v", "step:verify")], "t")
    names = missing_gates(cfg, findings)
    expected = list(dict.fromkeys([*cfg.gate_sets["build"], *cfg.gate_sets["verify"]]))
    assert names == expected and len(set(names)) == len(names)


def test_missing_gates_ignores_satisfied_and_non_gate_findings(cfg):
    events = gates(1, cfg, "t")
    findings = evaluate(cfg, events, [ob(FLOOR_ID, "gates:verify"), ob("v", "step:verify")], "t")
    assert missing_gates(cfg, findings) == []
