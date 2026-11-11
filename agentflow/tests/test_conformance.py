"""Conformance: which obligations apply, what evidence in a run's events satisfies them, and the audit at `finish`
(the gates it runs, the event it writes, the pull request section and the `block` stop)."""

from __future__ import annotations

import os
import re
from dataclasses import replace
from pathlib import Path

import pytest
import test_feature_lane
from test_feature_lane import SMALL, begin_finish, created, gh_calls, review

from agentflow.conditions import Ctx
from agentflow.config import FLOOR_ID, Evidence, Obligation
from agentflow.conformance import evaluate, last_code_change, missing_gates
from agentflow.flow import main
from agentflow.hitl import approve
from agentflow.pipeline import RunOptions, advance, start
from agentflow.policy import applicable, step_phase
from agentflow.run_control import retry

# The fake `gh` fixture, bound by assignment so pytest finds it here and the tests' `gh` parameters don't redefine an import.
gh = test_feature_lane.gh

FINISH = "ship/finish"
BRANCH_REVIEW = "review/branch_review"


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
    skipped = evaluate(cfg, [ev(2, "phase_skipped", phase="plan", reason="plan exists on origin/main", via="skip_if")], [planned], "t")[0]
    assert skipped.status == "satisfied" and "on base" in skipped.note
    marked = [ev(2, "phase_skipped", phase="plan", reason="marked done after build")]
    assert evaluate(cfg, marked, [planned], "t")[0].status == "missing"


def test_all_steps_skipped_is_not_approval(cfg):
    planned = ob("plan_approved", "approval:plan")
    events = [ev(2, "phase_skipped", phase="plan", reason="all steps skipped")]
    assert evaluate(cfg, events, [planned], "t")[0].status == "missing"


def test_a_skip_if_an_agent_decided_is_not_on_base(cfg):
    planned = ob("plan_approved", "approval:plan")
    agent = [ev(2, "phase_skipped", phase="plan", reason="r", via="skip_if", decided_by="agent")]
    assert evaluate(cfg, agent, [planned], "t")[0].status == "missing"
    for by in ("runner", "human"):
        decided = [ev(2, "phase_skipped", phase="plan", reason="r", via="skip_if", decided_by=by)]
        assert evaluate(cfg, decided, [planned], "t")[0].status == "satisfied"


def test_skip_if_skip_without_via_is_not_on_base(cfg):
    planned = ob("plan_approved", "approval:plan")  # an older log: the skip has a reason but no `via`
    events = [ev(2, "phase_skipped", phase="plan", reason="plan exists on origin/main")]
    assert evaluate(cfg, events, [planned], "t")[0].status == "missing"


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
    late_commit = ev(9, "step_committed", key="build/other", sha="y")
    assert evaluate(cfg, [red, fix, green, late_commit], [rtg], "t")[0].status == "missing"
    own_commit = ev(9, "step_committed", key="build/fix", sha="y")  # `build/fix` commits after its own green check
    assert evaluate(cfg, [red, fix, green, own_commit], [rtg], "t")[0].status == "satisfied"
    failed_green = {**green, "ok": False}
    assert evaluate(cfg, [red, fix, failed_green], [rtg], "t")[0].status == "missing"
    green_before_red = {**green, "seq": 2}
    assert evaluate(cfg, [green_before_red, red], [rtg], "t")[0].status == "missing"


def test_a_steps_own_commit_never_makes_its_evidence_stale(cfg):
    verified = ob("verified", "step:verify")
    own = [ev(5, "step_committed", key="verify/verify", sha="a"), finished(6, "verify/verify"),
           ev(7, "step_committed", key="verify/verify", sha="b")]
    assert evaluate(cfg, own, [verified], "t")[0].status == "satisfied"
    other = own + [ev(8, "step_committed", key="build/other", sha="c")]
    assert evaluate(cfg, other, [verified], "t")[0].status == "missing"


def verify_gates(seq: int, cfg, key: str, **over) -> list[dict]:
    fields = {"passed": True, "skipped": False, "reason": "", "tree": "x", **over}
    return [ev(seq + i, "gate_result", key=key, gate=g, **fields) for i, g in enumerate(cfg.gate_sets["verify"])]


def test_a_fix_that_ran_the_verify_gates_keeps_earlier_evidence(cfg):
    """A refine-round fix (`review/fix_findings`) that ran the whole verify gate set under its own key: the earlier
    verify step and red/green check still count, as they do in today's bar."""
    obligations = [ob("verified", "step:verify"), ob("red_then_green", "expect:red_then_green")]
    key = "review/fix_findings"
    base = [ev(2, "expectation_checked", key="build/regression_test", expect="red", ok=True),
            ev(3, "expectation_checked", key="build/fix", expect="green", ok=True), finished(4, "verify/verify")]
    covered = base + verify_gates(10, cfg, key) + [ev(20, "step_committed", key=key, sha="f")]
    assert [f.status for f in evaluate(cfg, covered, obligations, "t")] == ["satisfied", "satisfied"]
    skipped = base + verify_gates(10, cfg, key, skipped=True, passed=False) + [ev(20, "step_committed", key=key, sha="f")]
    assert [f.status for f in evaluate(cfg, skipped, obligations, "t")] == ["satisfied", "satisfied"]


def test_a_commit_without_the_verify_gates_still_makes_evidence_stale(cfg):
    obligations = [ob("verified", "step:verify"), ob("red_then_green", "expect:red_then_green")]
    key = "review/fix_findings"
    base = [ev(2, "expectation_checked", key="build/regression_test", expect="red", ok=True),
            ev(3, "expectation_checked", key="build/fix", expect="green", ok=True), finished(4, "verify/verify")]
    commit = [ev(20, "step_committed", key=key, sha="f")]
    assert [f.status for f in evaluate(cfg, base + commit, obligations, "t")] == ["missing", "missing"]
    other_key = verify_gates(10, cfg, "verify/verify")  # the gates ran, but not on this commit's step
    assert [f.status for f in evaluate(cfg, base + other_key + commit, obligations, "t")] == ["missing", "missing"]
    gates = verify_gates(10, cfg, key)
    failed_last = gates + [{**gates[0], "seq": 15, "passed": False}]  # the last result of one gate failed
    assert [f.status for f in evaluate(cfg, base + failed_last + commit, obligations, "t")] == ["missing", "missing"]
    if len(cfg.gate_sets["verify"]) > 1:
        partial = gates[:1]
        assert evaluate(cfg, base + partial + commit, obligations, "t")[0].status == "missing"
    conflicted = base + verify_gates(10, cfg, "ship/finish") + [ev(20, "base_synced", key="ship/finish", conflicts=["a"])]
    assert [f.status for f in evaluate(cfg, conflicted, obligations, "t")] == ["missing", "missing"]


def test_an_unrecognised_item_and_an_empty_require_are_missing(cfg):
    from agentflow.config import UNRECOGNISED

    typo = Obligation("typo", (UNRECOGNISED,))
    empty = Obligation("empty", ())
    events = [finished(2, "verify/verify")]
    found = evaluate(cfg, events, [typo, empty], "t")
    assert [(f.status, f.note) for f in found] == [("missing", "unrecognised evidence item"), ("missing", "no evidence items")]


def test_an_unparseable_item_loads_as_unrecognised(tmp_cfg):
    from agentflow.config import UNRECOGNISED, load_config

    cfg = load_config(tmp_cfg({"policy": {"obligations": {"typo": {"require": ["stepp:verify", "step:verify"]}}}}))
    assert cfg.policy.obligations["typo"].require == (UNRECOGNISED, Evidence("step", "verify"))


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
    marked = [ev(4, "run_marked_done", by="me", phase="build"), ev(5, "phase_skipped", phase="verify", reason="marked done after build")]
    assert evaluate(cfg, marked, [gated], "t")[0].status == "missing"
    expect = ob("red_then_green", "expect:red_then_green")
    skipped_build = [ev(4, "run_marked_done", by="me", phase="plan"), ev(5, "phase_skipped", phase="build", reason="marked done after build")]
    assert evaluate(cfg, skipped_build, [expect], "t")[0].status == "waived"


def test_a_phase_skipped_for_another_reason_after_the_mark_does_not_waive(cfg):
    verified = ob("verified", "step:verify")
    events = [ev(4, "run_marked_done", by="me", phase="build"), ev(5, "phase_skipped", phase="verify", reason="all steps skipped")]
    assert evaluate(cfg, events, [verified], "t")[0].status == "missing"
    via = [ev(4, "run_marked_done", by="me", phase="build"),
           ev(5, "phase_skipped", phase="verify", reason="checks done on base", via="skip_if")]
    assert evaluate(cfg, via, [verified], "t")[0].status == "missing"


def test_a_satisfied_obligation_is_not_waived(cfg):
    verified = ob("verified", "step:verify")
    events = [finished(2, "verify/verify"), ev(4, "run_marked_done", by="me", phase="build"),
              ev(5, "phase_skipped", phase="verify", reason="marked done after build")]
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


def test_a_when_the_config_no_longer_defines_applies(cfg, tmp_path):
    lost = ob("lost", "step:verify", when="no_such_condition")  # a pack dropped the condition a default names
    cfg.policy = replace(cfg.policy, obligations={"lost": lost, FLOOR_ID: cfg.policy.obligations[FLOOR_ID]})
    got = applicable(cfg, Ctx(repo=tmp_path, cfg=cfg, values={}, base_ref="origin/main"), "feature")
    assert [o.id for o, _ in got] == ["lost", FLOOR_ID] and "unknown" in got[0][1]


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


# --------------------------------------------------------------------------- the audit at finish


def last(flow_repo, run, etype: str) -> dict:
    return flow_repo.events(run, etype)[-1]


def final_tree(git, run) -> str:
    return git(Path(run.state["worktree"]), "rev-parse", "HEAD^{tree}").strip()


def pushed(git, flow_repo, run) -> bool:
    return run.state["branch"] in git(flow_repo.origin, "branch", "--list")


def conformance_section(body: str) -> list[str]:
    return body.split("## Conformance\n\n", 1)[1].split("\n\n## ", 1)[0].splitlines()


def other_evidence(run) -> None:
    """What a walked run would show for every default obligation but `reviewed`: a verify step, a human's plan."""
    run.emit("step_finished", key="verify/verify", status="done", summary="")
    run.emit("phase_approved", phase="plan", by="me")


def test_finish_audits_and_reports_in_report_mode(flow_repo, gh, git):
    run = begin_finish(flow_repo)  # no verify or review ran
    assert advance(run, flow_repo.cfg, RunOptions()) == "done"
    checked = last(flow_repo, run, "conformance_checked")
    tree = final_tree(git, run)
    assert checked["tree"] == tree and checked["enforce"] == "report" and checked["passed"] is False
    ran = [e for e in flow_repo.events(run, "gate_result") if e.get("key") == FINISH]
    assert [e["gate"] for e in ran] == flow_repo.cfg.gate_sets["verify"]
    assert all(e["tree"] == tree and e["passed"] for e in ran)
    statuses = {o["id"]: o["status"] for o in checked["obligations"]}
    assert statuses["reviewed"] == "missing" and statuses[FLOOR_ID] == "satisfied"
    body = (run.dir / "pr.md").read_text(encoding="utf-8")
    assert "Conformance: " in body and "`reviewed` missing" in body
    assert body.index("## Gates") < body.index("## Conformance") < body.index("## Skipped steps")
    assert pushed(git, flow_repo, run) and len(created(gh)) == 1


def test_block_mode_stops_before_the_push(flow_repo, gh, git):
    run = begin_finish(flow_repo)
    flow_repo.configure({**SMALL, "policy": {"enforce": "block"}})
    assert advance(run, flow_repo.cfg, RunOptions()) == "waiting_human"
    raised = last(flow_repo, run, "hitl_raised")
    assert raised["kind"] == "conformance_failed" and raised["key"] == FINISH
    assert "have no evidence on the tree to be pushed" in raised["summary"] and "reviewed" in raised["summary"]
    assert last(flow_repo, run, "conformance_checked")["passed"] is False
    assert not pushed(git, flow_repo, run) and gh_calls(gh) == []
    inbox = " ".join(Path(raised["inbox"]).read_text(encoding="utf-8").split())
    assert f"agentflow retry {run.id} {BRANCH_REVIEW}" not in inbox  # this run never walked it: retry would refuse it
    assert "never ran `branch_review`, so no retry can bring its evidence" in inbox
    assert f"`agentflow retry {run.id} {FINISH}` audits again" in inbox


def test_retry_after_conformance_stop_audits_again_and_pushes(flow_repo, gh, git):
    run = begin_finish(flow_repo)
    flow_repo.configure({**SMALL, "policy": {"enforce": "block"}})
    other_evidence(run)
    assert advance(run, flow_repo.cfg, RunOptions()) == "waiting_human"
    assert [o["id"] for o in last(flow_repo, run, "conformance_checked")["obligations"]
            if o["status"] == "missing"] == ["reviewed"]
    run.emit("step_finished", key=BRANCH_REVIEW, status="done", summary="")
    retry(run, FINISH, "me")
    assert advance(run, flow_repo.cfg, RunOptions()) == "done"
    checks = flow_repo.events(run, "conformance_checked")
    assert len(checks) == 2 and checks[-1]["passed"] is True
    assert flow_repo.events(run, "pr_opened") and pushed(git, flow_repo, run)


def test_retrying_the_missing_step_runs_it_again_then_finish_audits_and_pushes(flow_repo, gh, git):
    """The way on the inbox names: `agentflow retry <run> review/branch_review` reruns the review in its approved
    phase, and clears the `conformance_failed` stop, so `finish` audits again and pushes."""
    flow_repo.configure({**SMALL, "policy": {"enforce": "block"}})
    flow_repo.script({BRANCH_REVIEW: [review(), review()]})
    run = start(flow_repo.cfg, flow_repo.root, "feature", {"module": "demo"}, RunOptions(from_phase="review"))
    assert advance(run, flow_repo.cfg, RunOptions()) == "awaiting_approval"
    run.emit("step_committed", key="review/fix_findings", sha="0" * 40, summary="a change no review has seen")
    other_evidence(run)
    approve(run, "review", by="me")
    assert advance(run, flow_repo.cfg, RunOptions()) == "waiting_human"
    raised = last(flow_repo, run, "hitl_raised")
    assert raised["kind"] == "conformance_failed" and not pushed(git, flow_repo, run)
    inbox = " ".join(Path(raised["inbox"]).read_text(encoding="utf-8").split())
    assert f"`agentflow retry {run.id} {BRANCH_REVIEW}` runs `{BRANCH_REVIEW}` again" in inbox and "never ran" not in inbox

    assert retry(run, BRANCH_REVIEW, "me") == BRANCH_REVIEW
    assert run.state["waiting"] is None
    assert advance(run, flow_repo.cfg, RunOptions()) == "awaiting_approval"  # the review phase ran it again
    assert [c["step"] for c in flow_repo.calls()].count(BRANCH_REVIEW) == 2
    approve(run, "review", by="me")
    assert advance(run, flow_repo.cfg, RunOptions()) == "done"
    checks = flow_repo.events(run, "conformance_checked")
    assert len(checks) == 2 and checks[-1]["passed"] is True
    assert pushed(git, flow_repo, run) and len(created(gh)) == 1


def test_retrying_a_task_the_walk_does_not_visit_settles_the_phase_once(flow_repo, gh, git):
    """A retried key under a step that is already done (a committed task) is never walked again: the approved phase
    settles once more, then stays approved. It does not reopen on every advance."""
    flow_repo.configure(SMALL)
    flow_repo.script({BRANCH_REVIEW: [review()]})
    run = start(flow_repo.cfg, flow_repo.root, "feature", {"module": "demo"}, RunOptions(from_phase="review"))
    assert advance(run, flow_repo.cfg, RunOptions()) == "awaiting_approval"
    approve(run, "review", by="me")
    task = "review/branch_review/task-1/implement"  # a key nested under a done step: `run_step` never reaches it
    run.emit("step_started", key=task, phase="review")
    run.emit("step_finished", key=task, status="done", summary="")
    run.emit("pr_opened", url="https://github.com/acme/repo/pull/42", key=FINISH)  # finish already pushed
    retry(run, task, "me")
    assert advance(run, flow_repo.cfg, RunOptions()) == "awaiting_approval"  # walked and settled again, once
    approve(run, "review", by="me")
    assert advance(run, flow_repo.cfg, RunOptions()) == "done"  # no fresh approval asked: the retry is settled
    review_approvals = [e for e in flow_repo.events(run, "phase_approved") if e["phase"] == "review"]
    assert [e["by"] for e in review_approvals] == ["me", "me"]  # the first approval, and the one the retry asked
    assert len([e for e in flow_repo.events(run, "handoff_written") if e["phase"] == "review"]) == 1
    assert len([e for e in flow_repo.events(run, "phase_approval_raised") if e.get("phase") == "review"]) == 2
    assert [c["step"] for c in flow_repo.calls()].count(BRANCH_REVIEW) == 1


def test_a_file_left_after_the_first_dirty_check_stops_the_audit(flow_repo, gh, git, monkeypatch):
    """Something between `finish`'s first check and the audit (the sync's merge gates) leaves a file: the audit
    refuses to snapshot a tree the push would not send."""
    from agentflow.sync_lane import SyncLane

    def sync_leaving_a_file(self, phase, step, key):
        (self.worktree / "leftover.txt").write_text("from a gate\n", encoding="utf-8")
        return "done"

    monkeypatch.setattr(SyncLane, "run_sync", sync_leaving_a_file)
    run = begin_finish(flow_repo)
    assert advance(run, flow_repo.cfg, RunOptions()) == "waiting_human"
    raised = last(flow_repo, run, "hitl_raised")
    assert raised["kind"] == "dirty_tree" and raised["key"] == FINISH and "leftover.txt" in raised["summary"]
    assert "finish pushes committed work only" in raised["summary"]
    assert flow_repo.events(run, "conformance_checked") == []
    assert not pushed(git, flow_repo, run) and gh_calls(gh) == []


def test_resumed_finish_after_pr_opened_does_not_audit_again(flow_repo, gh, git):
    run = begin_finish(flow_repo)
    run.emit("pr_opened", url="https://github.com/acme/repo/pull/42", key=FINISH)
    assert advance(run, flow_repo.cfg, RunOptions()) == "done"
    assert flow_repo.events(run, "conformance_checked") == []
    assert not [e for e in flow_repo.events(run, "gate_result") if e.get("key") == FINISH]
    assert created(gh) == [] and not pushed(git, flow_repo, run)


def test_stale_gate_tree_makes_the_runner_run_the_gates(flow_repo, gh, git):
    run = begin_finish(flow_repo)
    for name in flow_repo.cfg.gate_sets["verify"]:
        run.emit("gate_result", key="verify/verify", gate=name, passed=True, skipped=False, reason="", tree="old")
        run.emit("gate_result", key="verify/verify", gate=name, passed=True, skipped=False, reason="")  # an older log
    assert advance(run, flow_repo.cfg, RunOptions()) == "done"
    tree = final_tree(git, run)
    ran = [e for e in flow_repo.events(run, "gate_result") if e.get("key") == FINISH]
    assert [e["gate"] for e in ran] == flow_repo.cfg.gate_sets["verify"] and all(e["tree"] == tree for e in ran)
    floor = next(o for o in last(flow_repo, run, "conformance_checked")["obligations"] if o["id"] == FLOOR_ID)
    assert floor["status"] == "satisfied" and floor["evidence"] == [e["seq"] for e in ran]


def test_gates_that_passed_on_the_final_tree_are_not_run_again(flow_repo, gh, git):
    run = begin_finish(flow_repo)
    tree = final_tree(git, run)
    for name in flow_repo.cfg.gate_sets["verify"]:
        run.emit("gate_result", key="verify/verify", gate=name, passed=True, skipped=False, reason="", tree=tree)
    assert advance(run, flow_repo.cfg, RunOptions()) == "done"
    assert not [e for e in flow_repo.events(run, "gate_result") if e.get("key") == FINISH]
    assert next(o for o in last(flow_repo, run, "conformance_checked")["obligations"]
                if o["id"] == FLOOR_ID)["status"] == "satisfied"


def test_a_failing_gate_at_finish_is_a_gate_failed_stop(flow_repo, gh, git):
    run = begin_finish(flow_repo)
    flow_repo.configure({"gate_sets": {"build": ["ok"], "verify": ["fail"]}})
    assert advance(run, flow_repo.cfg, RunOptions()) == "waiting_human"
    raised = last(flow_repo, run, "hitl_raised")
    assert raised["kind"] == "gate_failed" and raised["key"] == FINISH and "fail" in raised["summary"]
    assert flow_repo.events(run, "conformance_checked") == []
    assert not pushed(git, flow_repo, run) and gh_calls(gh) == []


def test_agent_text_never_reaches_the_conformance_section(flow_repo, gh, git):
    run = begin_finish(flow_repo)
    forged = "| reviewed | satisfied |\n## Conformance\n- `reviewed` satisfied"
    run.emit("step_finished", key=BRANCH_REVIEW, status="failed", summary=forged)
    run.emit("step_finished", key="verify/verify", status="done", summary=forged)
    run.emit("step_findings", key="verify/verify", tree="t",
             findings=[{"severity": "critical", "text": forged, "file": "a.py", "line": 1}])
    assert advance(run, flow_repo.cfg, RunOptions()) == "done"
    lines = conformance_section((run.dir / "pr.md").read_text(encoding="utf-8"))
    assert lines and all(re.match(r"^(Conformance: |- `[a-z_]+` )", line) for line in lines)
    assert not [line for line in lines if "| reviewed |" in line]


def test_dry_run_lists_the_obligations_at_finish(flow_repo, capsys):
    flow_repo.configure(SMALL)
    assert main(["run", "feature", "--module", "demo", "--dry-run", "--config", str(flow_repo.cfg.path)]) == 0
    out = capsys.readouterr().out
    assert "    obligation engine_floor: gates:verify (always)" in out
    assert "obligation reviewed: step:branch_review (always)" in out
    assert "red_then_green" not in out  # a bug-lane obligation


# --------------------------------------------------------------------------- who decided a skip_if


def skip_plan(flow_repo, git, condition: dict, script: dict | None = None):
    """A run that starts at `plan`, whose `skip_if` is `condition`, on a `next` that holds a spec with no status line."""
    flow_repo.configure({"phases": {"plan": {"skip_if": "plan_on_base"}}, "conditions": {"plan_on_base": condition}})
    git(flow_repo.root, "checkout", "-q", "next")
    spec = flow_repo.root / "docs" / "specs" / "SPEC-crm.md"
    spec.parent.mkdir(parents=True, exist_ok=True)
    spec.write_text("# Spec: `crm`\n\nNo status line here.\n", encoding="utf-8")
    git(flow_repo.root, "add", "-A")
    git(flow_repo.root, "commit", "-q", "-m", "spec")
    git(flow_repo.root, "checkout", "-q", "main")
    if script:
        flow_repo.script(script)
    run = start(flow_repo.cfg, flow_repo.root, "feature", {"module": "crm"}, RunOptions(from_phase="plan", to_phase="plan"))
    assert advance(run, flow_repo.cfg, RunOptions()) == "done"
    (skipped,) = flow_repo.events(run, "phase_skipped")
    assert skipped["phase"] == "plan" and skipped["via"] == "skip_if"
    return run, skipped


def test_a_skip_if_the_runner_decided_records_it_and_counts_as_on_base(flow_repo, git):
    run, skipped = skip_plan(flow_repo, git, {"check": "file_exists", "path": "README.md"})
    assert skipped["decided_by"] == "runner"
    finding = evaluate(flow_repo.cfg, run.events.read(), [ob("plan_approved", "approval:plan")], "t")[0]
    assert finding.status == "satisfied" and finding.note == "on base"


def test_a_skip_if_an_agent_decided_records_it_and_is_not_approval(flow_repo, git):
    condition = {"check": "status_line", "file": "docs/specs/SPEC-{module}.md", "approved": r"\*\*Status: approved",
                 "pending": "pending approval", "on_unknown": "agent"}
    answer = {"result": {"status": "done", "summary": "looks approved", "signals": {"answer": "yes"}}}
    run, skipped = skip_plan(flow_repo, git, condition, {"plan/skip_if": [answer]})
    assert skipped["decided_by"] == "agent"
    assert evaluate(flow_repo.cfg, run.events.read(), [ob("plan_approved", "approval:plan")], "t")[0].status == "missing"


# --------------------------------------------------------------------------- finish keeps what the audit checked


def test_a_commit_made_during_the_audit_stops_before_the_push(flow_repo, gh, git):
    run = begin_finish(flow_repo)
    flow_repo.configure({"gates": {"sneaky": {"run": "git commit -q --allow-empty -m sneaky"}},
                         "gate_sets": {"build": ["ok"], "verify": ["sneaky"]}})
    assert advance(run, flow_repo.cfg, RunOptions()) == "waiting_human"
    raised = last(flow_repo, run, "hitl_raised")
    assert raised["kind"] == "blocked" and raised["key"] == FINISH and "HEAD moved" in raised["summary"]
    assert not pushed(git, flow_repo, run) and gh_calls(gh) == []


def test_approving_a_gate_failed_stop_at_finish_runs_the_audit_again(flow_repo, gh, git):
    run = begin_finish(flow_repo)
    flow_repo.configure({"gate_sets": {"build": ["ok"], "verify": ["fail"]}})
    assert advance(run, flow_repo.cfg, RunOptions()) == "waiting_human"
    assert last(flow_repo, run, "hitl_raised")["kind"] == "gate_failed"
    flow_repo.configure(SMALL)  # the human fixed the cause: the verify set passes now
    approve(run, FINISH, by="me")
    assert advance(run, flow_repo.cfg, RunOptions()) == "done"
    (checked,) = flow_repo.events(run, "conformance_checked")
    ran = [e for e in flow_repo.events(run, "gate_result") if e.get("key") == FINISH]
    assert [(e["gate"], e["passed"]) for e in ran] == [("fail", False), ("ok", True)]
    floor = next(o for o in checked["obligations"] if o["id"] == FLOOR_ID)
    assert floor["status"] == "satisfied" and floor["evidence"] == [ran[-1]["seq"]]
    assert pushed(git, flow_repo, run)


# --------------------------------------------------------------------------- whole runs: today's bar is 100% met


@pytest.fixture
def ci_gh(monkeypatch, tmp_path):
    from test_ci import BIN, URL, Gh

    log, checks = tmp_path / "ci-gh.jsonl", tmp_path / "checks.json"
    monkeypatch.setenv("PATH", f"{BIN}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_GH_LOG", str(log))
    monkeypatch.setenv("FAKE_GH_URL", URL)
    monkeypatch.setenv("FAKE_GH_CHECKS", str(checks))
    log.touch()
    gh = Gh(log, checks)
    gh.play({"checks": [{"name": "backend", "state": "SUCCESS", "bucket": "pass", "link": URL}]})
    return gh


def fully_met(flow_repo, run) -> dict:
    (checked,) = flow_repo.events(run, "conformance_checked")
    statuses = {o["id"]: o["status"] for o in checked["obligations"]}
    assert checked["passed"] is True, checked["obligations"]
    assert set(statuses.values()) <= {"satisfied", "not_applicable"}, statuses
    return statuses


def walk(flow_repo, run) -> list[str]:
    asked = []
    status = advance(run, flow_repo.cfg, RunOptions())
    while status == "awaiting_approval":
        asked.append(run.state["waiting"]["phase"])
        approve(run, asked[-1], by="me")
        status = advance(run, flow_repo.cfg, RunOptions())
    assert status == "done", run.state["waiting"]
    return asked


def test_a_whole_feature_run_meets_every_obligation(flow_repo, ci_gh):
    from test_ci import PIPE

    spec, plan = "docs/specs/SPEC-demo.md", "docs/plans/2026-10-29-demo.md"
    flow_repo.configure(PIPE, autopilot=True)
    flow_repo.script({
        "define/draft": [{"write": {spec: "# Spec: `demo`\n\n**Status: approved** 2026-10-01\n\n## Open Questions\n\n"
                                          "None for this module.\n"}, "result": {"status": "done", "summary": "drafted"}}],
        "plan/plan": [{"write": {plan: "# Plan: demo\n\n### Task 1: One\n\n**Depends on:** none\n\n- [ ] **Step 1: Do it**\n"},
                       "result": {"status": "done", "summary": "planned"}}],
    })
    run = start(flow_repo.cfg, flow_repo.root, "feature", {"module": "demo"}, RunOptions())
    assert walk(flow_repo, run) == ["define", "plan"]
    statuses = fully_met(flow_repo, run)
    assert {k: v for k, v in statuses.items() if v == "satisfied"}.keys() >= {"verified", "reviewed", "plan_approved", FLOOR_ID}


def begin_bug_to_finish(flow_repo, acts: dict):
    from test_ci import FIXED, GOOD_TEST, PIPE, RED

    flow_repo.configure(PIPE, autopilot=True)
    flow_repo.script({RED: [GOOD_TEST], "build/fix": [FIXED], **acts})
    return start(flow_repo.cfg, flow_repo.root, "bug", {"task": "đăng nhập lỗi"}, RunOptions())


def test_a_plain_bug_run_meets_every_obligation(flow_repo, ci_gh):
    run = begin_bug_to_finish(flow_repo, {})
    assert walk(flow_repo, run) == ["define"]
    statuses = fully_met(flow_repo, run)
    assert {k for k, v in statuses.items() if v == "satisfied"} >= {"verified", "reviewed", "red_then_green", FLOOR_ID}


def test_a_bug_run_with_a_review_refine_round_meets_every_obligation(flow_repo, ci_gh):
    blocking = {"severity": "blocking", "file": "src/one.py", "line": 3, "text": "Thiếu kiểm tra quyền"}
    run = begin_bug_to_finish(flow_repo, {
        BRANCH_REVIEW: [{"result": {"status": "done", "summary": "x", "findings": [blocking]}},
                        {"result": {"status": "done", "summary": "clean"}}],
        "review/fix_findings": [{"write": {"src/one.py": "x = 2\n"}, "result": {"status": "done", "summary": "fixed"}}],
    })
    assert walk(flow_repo, run) == ["define"]
    events = run.events.read()
    assert [e["phase"] for e in events if e["type"] == "phase_rejected"] == ["review"]
    fix = [e for e in events if e["type"] == "step_committed" and e["key"] == "review/fix_findings"]
    verify = [e for e in events if e["type"] == "step_finished" and e["key"] == "verify/verify" and e["status"] == "done"]
    assert fix and verify and verify[-1]["seq"] < fix[-1]["seq"]  # the verify step ran before the fix: still counts
    statuses = fully_met(flow_repo, run)
    assert {k for k, v in statuses.items() if v == "satisfied"} >= {"verified", "reviewed", "red_then_green", FLOOR_ID}


def test_an_unknown_enforce_mode_fails_closed_as_block(flow_repo, gh, git):
    run = begin_finish(flow_repo)
    flow_repo.configure({**SMALL, "policy": {"enforce": "blok"}})
    assert advance(run, flow_repo.cfg, RunOptions()) == "waiting_human"
    assert last(flow_repo, run, "hitl_raised")["kind"] == "conformance_failed"
    assert last(flow_repo, run, "conformance_checked")["enforce"] == "block"
    assert not pushed(git, flow_repo, run)


def test_an_unparseable_obligation_is_missing_at_finish(flow_repo, gh, git):
    run = begin_finish(flow_repo)
    flow_repo.configure({**SMALL, "policy": {"obligations": {"typo": {"require": ["stepp:verify"]}}}})
    assert advance(run, flow_repo.cfg, RunOptions()) == "done"  # report mode: the gap ships, shown
    typo = next(o for o in last(flow_repo, run, "conformance_checked")["obligations"] if o["id"] == "typo")
    assert typo["status"] == "missing" and typo["note"] == "unrecognised evidence item"
