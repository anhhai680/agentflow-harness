"""Conformance: which obligations apply, what evidence in a run's events satisfies them, and the audit at `finish`
(the gates it runs, the event it writes, the pull request section and the `block` stop)."""

from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path

from test_feature_lane import SMALL, begin_finish, created, gh, gh_calls, review  # noqa: F401  (`gh` is a fixture)

from agentflow.conditions import Ctx
from agentflow.config import FLOOR_ID, Evidence, Obligation
from agentflow.conformance import evaluate, last_code_change, missing_gates
from agentflow.flow import main
from agentflow.hitl import approve
from agentflow.pipeline import RunOptions, advance, start
from agentflow.policy import applicable, step_phase
from agentflow.run_control import retry

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
