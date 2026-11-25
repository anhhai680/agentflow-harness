"""A run's claim on its backlog item: the claim at start, made before the run, its worktree and its branch exist, and a
second start of a claimed item refused first. Against a real bare `origin` and real clones."""
from __future__ import annotations

import shutil
import subprocess
import threading
from pathlib import Path

import pytest
import test_conformance
from conftest import run_git
from test_backlog_runs import DONE_ACT, PLAN_TEXT, SPEC, SPEC_TEXT, BacklogFlow
from test_claims import OTHER, remote_refs, run_git_input, second_clone

from agentflow import claim_sync, claims, conditions, hitl, pipeline, run_control, slots, work_source
from agentflow.hitl import approve
from agentflow.pipeline import RunOptions, advance, start
from agentflow.state import Run, list_runs, runs_root

# The fake `gh` fixture of whole runs, bound by assignment so pytest finds it here.
ci_gh = test_conformance.ci_gh

class ClaimFlow(BacklogFlow):
    """`BacklogFlow` with claims on, and its `next` (the fork, with `backlog.yml`) pushed to `origin`, so a second
    clone can start the same item."""

    POLICY = {"claims": True}

    def __init__(self, flow_repo) -> None:
        super().__init__(flow_repo)
        run_git(flow_repo.root, "push", "-q", "origin", "next")

    def clone(self, tmp_path: Path, name: str = "Other") -> Path:
        """A second machine: a clone of `origin` on `next`, with its own `user.name` (and so its own state dir)."""
        other = second_clone(self.repo, tmp_path, name)
        run_git(other, "checkout", "-q", "next")
        return other


@pytest.fixture
def cflow(flow_repo) -> ClaimFlow:
    return ClaimFlow(flow_repo)


def _types(run: Run) -> list[str]:
    return [e["type"] for e in run.events.read()]


def _claim_refs(flow_repo) -> dict[str, str]:
    return {name: sha for name, sha in remote_refs(flow_repo).items() if "agentflow/claims/" in name}


def _flow_branches(repo: Path) -> list[str]:
    return run_git(repo, "branch", "--list", "flow/*", "--format=%(refname:short)").split()


def _worktrees(repo: Path) -> int:
    """How many worktrees `repo` has besides its own checkout."""
    return run_git(repo, "worktree", "list", "--porcelain").count("worktree ") - 1


def _nothing_started(repo: Path) -> None:
    assert list_runs(repo) == []
    root = runs_root(repo)
    assert not root.is_dir() or not [p for p in root.iterdir() if p.is_dir()]
    assert _flow_branches(repo) == [] and _worktrees(repo) == 0


# --------------------------------------------------------------------------- the claim at start


def test_a_start_claims_before_any_worktree(cflow):
    cfg, root = cflow.cfg, cflow.repo.root
    run = start(cfg, root, "feature", {"module": "b"}, RunOptions())
    found = claims.read(cfg, root, "b")
    assert found is not None and not found.unreadable
    assert (found.run, found.phase, found.status, found.by, found.lane) == (run.id, "define", "running", "Test",
                                                                            "feature")
    claim = run.refresh()["claim"]
    assert claim["sha"] == found.sha == _claim_refs(cflow.repo)[claims.ref(cfg, "b")]
    assert (claim["item"], claim["ref"], claim["released"], claim["lost"]) == ("b", claims.ref(cfg, "b"), False, False)
    types = _types(run)
    worktree_at = next(i for i, e in enumerate(run.events.read()) if e["type"] == "run_updated" and e.get("worktree"))
    assert types[:2] == ["run_started", "item_claimed"] and worktree_at > 1


def test_two_starts_from_two_clones_make_one_run(cflow, tmp_path):
    cfg, clones = cflow.cfg, [cflow.repo.root, cflow.clone(tmp_path)]
    barrier = threading.Barrier(2)
    results: list[object] = [None, None]

    def go(i: int) -> None:
        barrier.wait()
        try:
            results[i] = start(cfg, clones[i], "feature", {"module": "b"}, RunOptions())
        except Exception as exc:  # the loser's refusal is the result
            results[i] = exc

    threads = [threading.Thread(target=go, args=(i,)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    won = [r for r in results if isinstance(r, Run)]
    lost = [r for r in results if isinstance(r, ValueError)]
    assert len(won) == 1 and len(lost) == 1, results
    assert "claimed by" in str(lost[0]) and won[0].id in str(lost[0])
    assert sum(len(list_runs(c)) for c in clones) == 1
    assert sum(_worktrees(c) for c in clones) == 1
    assert sum(len(_flow_branches(c)) for c in clones) == 1
    assert claims.read(cfg, clones[0], "b").run == won[0].id


def _forged(cflow, tmp_path: Path) -> str:
    """Push a claim commit holding no `claim.json` to `b`'s claim ref from another clone; its sha."""
    other = cflow.clone(tmp_path, "Mallory")
    blob = run_git_input(other, "{}\n", "hash-object", "-w", "--stdin").strip()
    tree = run_git_input(other, f"100644 blob {blob}\tother.json\n", "mktree").strip()
    sha = run_git(other, "commit-tree", tree, "-m", "forged").strip()
    run_git(other, "push", "-q", "origin", f"{sha}:refs/agentflow/claims/b")
    return sha


@pytest.mark.parametrize("case", ["foreign", "unreadable", "hidden", "unreachable"])
def test_refusals_leave_no_run_and_no_claim(cflow, tmp_path, case):
    cfg, root, origin = cflow.cfg, cflow.repo.root, cflow.repo.origin
    name = claims.ref(cfg, "b")
    before: dict[str, str] = {}
    if case == "foreign":
        theirs = claims.create(cfg, cflow.clone(tmp_path), "b", run=OTHER, lane="feature", phase="plan")
        before = {name: theirs.sha}
        expected = (f"module b is claimed by Other: run {OTHER}, phase plan, running, updated {theirs.updated} "
                    "(agentflow backlog release b frees it)")
    elif case == "unreadable":
        before = {name: _forged(cflow, tmp_path)}
        expected = ("module b has a claim on origin that agentflow can't read (no claim.json of at most 4096 bytes): "
                    "agentflow backlog release b frees it")
    elif case == "hidden":
        run_git(origin, "config", "receive.hideRefs", "refs/agentflow/")
        expected = (f"origin refused the claim ref {name}: set backlog.claim_ref: branch in config.yaml, or "
                    "backlog.claims: false to start without claims")
    else:
        shutil.move(origin, tmp_path / "moved.git")
        expected = "could not reach origin to claim b: "
    with pytest.raises(ValueError) as caught:
        start(cfg, root, "feature", {"module": "b"}, RunOptions())
    text = str(caught.value)
    if case == "unreachable":
        assert text.startswith(expected), text
        assert text.endswith("; start again when it is reachable, or set backlog.claims: false to start without claims")
        assert len(text) < 500
        shutil.move(tmp_path / "moved.git", origin)
    else:
        assert text == expected
    _nothing_started(root)
    if case == "hidden":
        run_git(origin, "config", "--unset", "receive.hideRefs")
    assert _claim_refs(cflow.repo) == before


# --------------------------------------------------------------------------- a failure after the claim


def test_a_failure_after_the_claim_releases_it_when_the_slots_are_full(cflow, monkeypatch):
    cfg = cflow.configure({"slots": {"count": 2}})
    root = cflow.repo.root
    for task in ("one", "two"):  # two live runs hold both slots
        other = Run.create(cfg, root, "bug", {"task": task})
        slots.acquire(cfg, other)
    # the slots were free at the quick look and taken by the time the run is created
    monkeypatch.setattr(slots, "check_free", lambda cfg, repo: None)
    made: list[claims.Claim] = []
    create = claims.create

    def spy(*args, **kwargs):
        made.append(create(*args, **kwargs))
        return made[-1]

    monkeypatch.setattr(claims, "create", spy)
    with pytest.raises(slots.SlotsFull):
        start(cfg, root, "feature", {"module": "b"}, RunOptions())
    assert [held.item for held in made] == ["b"]  # it was claimed, and then released
    assert claims.read(cfg, root, "b") is None and _claim_refs(cflow.repo) == {}
    assert [r.state["pipeline"] for r in list_runs(root)] == ["bug", "bug"]
    assert _flow_branches(root) == [] and _worktrees(root) == 0


def test_a_failure_after_the_claim_releases_it_when_set_up_fails(cflow, monkeypatch):
    cfg, root = cflow.cfg, cflow.repo.root

    def boom(*args, **kwargs):
        raise RuntimeError("no worktree today")

    monkeypatch.setattr(pipeline, "_set_up", boom)
    with pytest.raises(RuntimeError):
        start(cfg, root, "feature", {"module": "b"}, RunOptions())
    assert claims.read(cfg, root, "b") is None and _claim_refs(cflow.repo) == {}
    [run] = list_runs(root)
    state = run.refresh()
    assert state["status"] == "failed" and state["claim"]["released"]
    types = _types(run)
    assert types.index("item_claimed") < types.index("run_failed") < types.index("claim_released")
    assert [e["reason"] for e in run.events.read() if e["type"] == "claim_released"] == ["failed"]


def test_a_release_that_fails_after_a_failed_start_is_recorded(cflow, monkeypatch):
    cfg, root = cflow.cfg, cflow.repo.root

    def boom(*args, **kwargs):
        raise RuntimeError("no worktree today")

    def unreachable(*args, **kwargs):
        raise claims.Unreachable("could not reach origin: " + "x" * 5000)

    monkeypatch.setattr(pipeline, "_set_up", boom)
    monkeypatch.setattr(claims, "release", unreachable)
    with pytest.raises(RuntimeError, match="no worktree today"):
        start(cfg, root, "feature", {"module": "b"}, RunOptions())
    [run] = list_runs(root)
    [failed] = [e for e in run.events.read() if e["type"] == "claim_release_failed"]
    assert failed["item"] == "b" and failed["error"].startswith("could not reach origin") and len(failed["error"]) <= 300
    assert not run.refresh()["claim"]["released"]


# --------------------------------------------------------------------------- starts that claim nothing


def _no_claim(cflow, run: Run) -> None:
    assert run.refresh()["claim"] is None and "item_claimed" not in _types(run)


def test_starts_that_claim_nothing(cflow, flow_repo):
    root = flow_repo.root
    cfg = cflow.configure({"backlog": {"claims": False}})
    _no_claim(cflow, start(cfg, root, "feature", {"module": "b"}, RunOptions()))
    cfg = cflow.configure()
    _no_claim(cflow, start(cfg, root, "bug", {"task": "fix the thing"}, RunOptions()))  # no module, so no item
    cfg = flow_repo.configure({"backlog": {"claims": True}})  # the python pack: a capability map
    _no_claim(cflow, start(cfg, root, "feature", {"module": "crm"}, RunOptions()))
    assert _claim_refs(flow_repo) == {}
    run_git(root, "remote", "remove", "origin")
    cfg = cflow.configure()
    _no_claim(cflow, start(cfg, root, "feature", {"module": "a2"}, RunOptions(
        new_module={"module": "a2", "responsibility": "Another", "deps": []})))
    assert _claim_refs(flow_repo) == {}


def test_a_new_item_is_claimed_by_its_id(cflow):
    cfg, root = cflow.cfg, cflow.repo.root
    new = {"module": "n", "responsibility": "Do the new thing", "deps": ["b"]}
    run = start(cfg, root, "feature", {"module": "n"}, RunOptions(new_module=new))
    found = claims.read(cfg, root, "n")
    assert found is not None and (found.run, found.item, found.phase) == (run.id, "n", "define")
    state = run.refresh()
    assert "n" not in [i.id for i in work_source.items_at(cfg, root, state["base_sha"])]
    worktree = Path(state["worktree"])
    assert "n" in [i.id for i in work_source.items_at(cfg, worktree, run_git(worktree, "rev-parse", "HEAD").strip())]


# --------------------------------------------------------------------------- the claim follows the run (Task 6)


def _plan_script(cflow, run) -> None:
    """Script the define and plan steps of `run`: the draft writes the spec, the plan step the plan."""
    values = conditions.make_ctx(cflow.cfg, cflow.repo.root, "feature", run.state["params"], run).values
    cflow.repo.script({"define/draft": [{"write": {SPEC: SPEC_TEXT}, **DONE_ACT}],
                       "plan/plan": [{"write": {values["plan_target"]: PLAN_TEXT}, **DONE_ACT}]})


def _defined(cflow) -> Run:
    """A `feature` run of `b`, advanced to the define phase's approval."""
    run = start(cflow.cfg, cflow.repo.root, "feature", {"module": "b"}, RunOptions(to_phase="plan"))
    _plan_script(cflow, run)
    assert advance(run, cflow.cfg, RunOptions(to_phase="plan")) == "awaiting_approval"
    return run


def _moves(run: Run) -> list[tuple[str, str]]:
    return [(e["phase"], e["status"]) for e in run.events.read() if e["type"] == "claim_advanced"]


def _on_origin(cflow, item: str = "b") -> tuple[str, str, str] | None:
    found = claims.read(cflow.cfg, cflow.repo.root, item)
    return None if found is None else (found.run, found.phase, found.status)


def _count_pushes(monkeypatch) -> list[tuple[str, ...]]:
    """Every git call `claims` makes from here on (pushes among them)."""
    calls: list[tuple[str, ...]] = []
    real = claims._run

    def spy(repo, *args, **kwargs):
        calls.append(args)
        return real(repo, *args, **kwargs)

    monkeypatch.setattr(claims, "_run", spy)
    return calls


def test_the_claim_follows_the_run(cflow, monkeypatch):
    run = _defined(cflow)
    assert _on_origin(cflow) == (run.id, "define", "waiting")
    approve(run, "define", by="me")
    assert advance(run, cflow.cfg, RunOptions(to_phase="plan")) == "awaiting_approval"
    assert _on_origin(cflow) == (run.id, "plan", "waiting")
    assert _moves(run) == [("define", "waiting"), ("define", "running"), ("plan", "running"), ("plan", "waiting")]
    state = run.refresh()
    assert (state["claim"]["phase"], state["claim"]["status"]) == ("plan", "waiting")
    assert state["claim"]["sha"] == _claim_refs(cflow.repo)[claims.ref(cflow.cfg, "b")]
    calls = _count_pushes(monkeypatch)
    assert claim_sync.sync(cflow.cfg, run) is True
    assert calls == [] and len(_moves(run)) == 4  # no change: nothing pushed, nothing recorded


def test_pause_and_resume_move_the_claim(cflow, monkeypatch):
    cfg, root = cflow.cfg, cflow.repo.root
    run = start(cfg, root, "feature", {"module": "b"}, RunOptions(to_phase="plan"))
    _plan_script(cflow, run)
    run_control.pause(run, "me", cfg=cfg)
    assert _on_origin(cflow) == (run.id, "define", "paused")
    assert run_control.resume(run, "me") is True  # no config given: the run's own is loaded
    assert _on_origin(cflow) == (run.id, "define", "running")
    assert advance(run, cfg, RunOptions(to_phase="plan")) == "awaiting_approval"
    assert _moves(run) == [("define", "paused"), ("define", "running"), ("define", "waiting")]
    approve(run, "define", by="me")

    def crash(*args, **kwargs):
        raise RuntimeError("the runner died here")

    monkeypatch.setattr(pipeline, "write_handoff", crash)
    with pytest.raises(RuntimeError, match="died"):
        advance(run, cfg, RunOptions(to_phase="plan"))
    assert run.refresh()["status"] == "error"
    assert _on_origin(cflow) == (run.id, "define", "stopped")


def test_cancel_fail_and_archive_release(cflow):
    cfg, root = cflow.cfg, cflow.repo.root
    cancelled = start(cfg, root, "feature", {"module": "b"}, RunOptions())
    run_control.cancel(cancelled, "me", cfg=cfg)
    assert claims.read(cfg, root, "b") is None

    failed = start(cfg, root, "feature", {"module": "b"}, RunOptions())
    failed.emit("run_failed", reason="a step failed")
    assert advance(failed, cfg, RunOptions()) == "failed"
    assert claims.read(cfg, root, "b") is None

    archived = start(cfg, root, "feature", {"module": "b"}, RunOptions())
    archived.emit("run_finished")
    assert claim_sync.sync(cfg, archived) is True  # a done run keeps its claim
    assert _on_origin(cflow) == (archived.id, "define", "waiting")
    run_control.archive(archived, "me")
    assert claims.read(cfg, root, "b") is None and _claim_refs(cflow.repo) == {}
    for run, reason in ((cancelled, "cancelled"), (failed, "failed"), (archived, "archived")):
        assert [e["reason"] for e in run.events.read() if e["type"] == "claim_released"] == [reason]
        assert run.refresh()["claim"]["released"] is True
        assert claim_sync.sync(cfg, run) is True and _types(run).count("claim_released") == 1


def test_a_done_run_keeps_its_claim_at_the_last_phase(cflow, ci_gh):
    from test_backlog_runs import _whole, _walk

    run, _ = _whole(cflow, claims=True)
    assert _walk(cflow, run) == "done", run.state["waiting"]
    last = list(cflow.cfg.phases)[-1]
    assert run.refresh()["phase"] == last
    assert _on_origin(cflow) == (run.id, last, "waiting")
    assert claims.ref(cflow.cfg, "b") in _claim_refs(cflow.repo)
    assert not run.state["claim"]["released"] and "claim_released" not in _types(run)


def _delete_on_origin(cflow, tmp_path: Path) -> None:
    other = cflow.clone(tmp_path, "Eve")
    run_git(other, "push", "-q", "origin", f":{claims.ref(cflow.cfg, 'b')}")


def test_a_deleted_claim_stops_the_run(cflow, tmp_path, monkeypatch):
    cfg = cflow.cfg
    run = _defined(cflow)
    _delete_on_origin(cflow, tmp_path)
    approve(run, "define", by="me")
    calls = _count_pushes(monkeypatch)
    assert advance(run, cfg, RunOptions(to_phase="plan")) == "waiting_human"
    state = run.refresh()
    waiting = state["waiting"]
    assert waiting["kind"] == "claim_lost" and "gone" in waiting["summary"] and "b" in waiting["summary"]
    assert "claim_lost" in _types(run) and state["claim"]["lost"] is True
    assert claims.read(cfg, cflow.repo.root, "b") is None  # nothing was pushed over it
    assert [c for c in calls if c[0] == "push"] and all("--force-with-lease" in " ".join(c) for c in calls
                                                        if c[0] == "push")
    assert "phase_started" not in [e["type"] for e in run.events.read() if e.get("phase") == "plan"]
    assert advance(run, cfg, RunOptions(to_phase="plan")) == "waiting_human"  # still stopped: nothing re-claimed
    assert _types(run).count("item_claimed") == 1

    approve(run, waiting["key"], by="me")
    assert advance(run, cfg, RunOptions(to_phase="plan")) == "awaiting_approval"
    state = run.refresh()
    assert _types(run).count("item_claimed") == 2 and state["claim"]["lost"] is False
    assert state["steps"][waiting["key"]]["status"] == "done"
    assert state["waiting"]["kind"] == "phase_approval" and state["waiting"]["phase"] == "plan"
    assert _on_origin(cflow) == (run.id, "plan", "waiting")


def test_a_claim_taken_by_another_run_stops_again(cflow, tmp_path):
    cfg = cflow.cfg
    run = _defined(cflow)
    other = cflow.clone(tmp_path)
    run_git(other, "push", "-q", "origin", f":{claims.ref(cfg, 'b')}")
    theirs = claims.create(cfg, other, "b", run=OTHER, lane="feature", phase="build")  # it is overwritten
    approve(run, "define", by="me")
    assert advance(run, cfg, RunOptions(to_phase="plan")) == "waiting_human"
    waiting = run.refresh()["waiting"]
    assert waiting["kind"] == "claim_lost" and "Other" in waiting["summary"] and OTHER in waiting["summary"]
    approve(run, waiting["key"], by="me")
    assert advance(run, cfg, RunOptions(to_phase="plan")) == "waiting_human"
    again = run.refresh()["waiting"]
    assert again["kind"] == "claim_lost" and "Other" in again["summary"] and OTHER in again["summary"]
    assert _types(run).count("claim_lost") == 2 and _types(run).count("item_claimed") == 1
    assert _claim_refs(cflow.repo)[claims.ref(cfg, "b")] == theirs.sha  # theirs stands


def test_origin_going_away_mid_run_does_not_stop_it(cflow, tmp_path):
    cfg, origin = cflow.cfg, cflow.repo.origin
    run = start(cfg, cflow.repo.root, "feature", {"module": "b"}, RunOptions(to_phase="plan"))
    _plan_script(cflow, run)
    shutil.move(origin, tmp_path / "moved.git")
    assert advance(run, cfg, RunOptions(to_phase="plan")) == "awaiting_approval"
    [failed] = [e for e in run.events.read() if e["type"] == "claim_sync_failed"]
    assert failed["item"] == "b" and failed["action"] == "advance"
    assert len(failed["error"]) <= 300 and "\n" not in failed["error"]
    state = run.refresh()
    assert state["status"] == "awaiting_approval" and not state["claim"]["lost"]
    assert (state["claim"]["phase"], state["claim"]["status"]) == ("define", "running")
    shutil.move(tmp_path / "moved.git", origin)
    assert claim_sync.sync(cfg, run) is True
    assert _on_origin(cflow) == (run.id, "define", "waiting")


def test_an_interrupted_start_stops_on_its_next_advance(cflow, monkeypatch):
    cfg, root = cflow.cfg, cflow.repo.root
    real = pipeline._set_up

    def interrupted(*args, **kwargs):
        real(*args, **kwargs)
        raise KeyboardInterrupt

    monkeypatch.setattr(pipeline, "_set_up", interrupted)
    with pytest.raises(KeyboardInterrupt):
        start(cfg, root, "feature", {"module": "b"}, RunOptions(to_phase="plan"))
    monkeypatch.setattr(pipeline, "_set_up", real)
    [run] = list_runs(root)
    assert [e["reason"] for e in run.events.read() if e["type"] == "claim_released"] == ["interrupted"]
    assert claims.read(cfg, root, "b") is None
    _plan_script(cflow, run)
    assert advance(run, cfg, RunOptions(to_phase="plan")) == "waiting_human"  # never a silent claim again
    waiting = run.refresh()["waiting"]
    assert waiting["kind"] == "claim_lost" and "interrupted" in waiting["summary"]
    assert _types(run).count("item_claimed") == 1 and claims.read(cfg, root, "b") is None
    approve(run, waiting["key"], by="me")
    assert advance(run, cfg, RunOptions(to_phase="plan")) == "awaiting_approval"
    assert _on_origin(cflow) == (run.id, "define", "waiting")


def test_a_claim_lost_at_a_step_stop_waits_behind_it(cflow, tmp_path):
    cfg = cflow.cfg
    run = start(cfg, cflow.repo.root, "feature", {"module": "b"}, RunOptions(to_phase="plan"))
    cflow.repo.script({"define/draft": [{"result": {"status": "blocked", "summary": "which database?"}}]})
    _delete_on_origin(cflow, tmp_path)
    assert advance(run, cfg, RunOptions(to_phase="plan")) == "waiting_human"
    state = run.refresh()
    assert state["waiting"]["key"] == "define/draft" and state["waiting"]["kind"] != "claim_lost"  # kept
    assert [w["kind"] for w in state["waiting_queue"]] == ["claim_lost"] and state["claim"]["lost"]
    approve(run, "define/draft", by="me")
    assert run.refresh()["waiting"]["kind"] == "claim_lost"
    assert advance(run, cfg, RunOptions(to_phase="plan")) == "waiting_human"  # nothing claimed until a person says
    assert _types(run).count("item_claimed") == 1 and claims.read(cfg, cflow.repo.root, "b") is None


# --------------------------------------------------------------------------- only a person's approval claims again


def _claimed_again(run: Run) -> int:
    return _types(run).count("item_claimed") - 1


def test_a_stop_raised_after_pause_found_the_loss_does_not_settle_it(cflow, tmp_path):
    cfg = cflow.cfg
    run = start(cfg, cflow.repo.root, "feature", {"module": "b"}, RunOptions(to_phase="plan"))
    _plan_script(cflow, run)
    _delete_on_origin(cflow, tmp_path)
    run_control.pause(run, "me", cfg=cfg)  # the runner is still on its step
    assert run.refresh()["waiting"]["kind"] == "claim_lost"
    hitl.raise_hitl(cfg, run, "blocked", "define/draft", "which database?")  # the live step then stops
    state = run.refresh()
    assert state["waiting"]["kind"] == "claim_lost" and [w["key"] for w in state["waiting_queue"]] == ["define/draft"]
    assert advance(run, cfg, RunOptions(to_phase="plan")) == "waiting_human"
    assert _claimed_again(run) == 0 and claims.read(cfg, cflow.repo.root, "b") is None
    approve(run, "define/claim", by="me")
    assert run.refresh()["waiting"]["key"] == "define/draft"
    advance(run, cfg, RunOptions(to_phase="plan"))
    assert _claimed_again(run) == 1 and _on_origin(cflow)[0] == run.id


def test_a_claim_stop_dropped_with_a_speculated_phase_is_raised_again(cflow, tmp_path):
    cfg = cflow.cfg
    run = _defined(cflow)
    _delete_on_origin(cflow, tmp_path)
    run.emit("claim_lost", item="b", why="the claim on b is gone from origin", key="plan/claim")
    run.emit("hitl_raised", kind="claim_lost", key="plan/claim", phase="plan", summary="gone", speculative=True)
    assert [w["key"] for w in run.refresh()["waiting_queue"]] == ["plan/claim"]
    hitl.reject(run, "define", by="me", note="say more about the rollback")  # drops the speculated phase's stops
    assert run.refresh()["waiting"] is None
    assert advance(run, cfg, RunOptions(to_phase="plan")) == "waiting_human"
    waiting = run.refresh()["waiting"]
    assert (waiting["kind"], waiting["key"]) == ("claim_lost", "plan/claim") and "gone" in waiting["summary"]
    assert _claimed_again(run) == 0 and claims.read(cfg, cflow.repo.root, "b") is None
    approve(run, "plan/claim", by="me")
    advance(run, cfg, RunOptions(to_phase="plan"))
    assert _claimed_again(run) == 1 and _on_origin(cflow)[0] == run.id


def test_a_claim_stop_that_failed_to_be_raised_is_raised_again(cflow, tmp_path, monkeypatch):
    cfg = cflow.cfg
    run = _defined(cflow)
    _delete_on_origin(cflow, tmp_path)
    approve(run, "define", by="me")

    def no_inbox(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(claim_sync, "raise_hitl", no_inbox)
    with pytest.raises(OSError, match="disk full"):
        advance(run, cfg, RunOptions(to_phase="plan"))
    state = run.refresh()
    assert state["status"] == "error" and state["claim"]["lost"] and state["waiting"] is None
    monkeypatch.setattr(claim_sync, "raise_hitl", hitl.raise_hitl)
    assert advance(run, cfg, RunOptions(to_phase="plan")) == "waiting_human"
    assert run.refresh()["waiting"]["kind"] == "claim_lost"
    assert _claimed_again(run) == 0 and claims.read(cfg, cflow.repo.root, "b") is None
    approve(run, "define/claim", by="me")
    assert advance(run, cfg, RunOptions(to_phase="plan")) == "awaiting_approval"
    assert _claimed_again(run) == 1 and _on_origin(cflow) == (run.id, "plan", "waiting")


def test_claiming_again_settles_only_the_stops_own_row(cflow, tmp_path):
    cfg = cflow.cfg
    run = _defined(cflow)
    _delete_on_origin(cflow, tmp_path)
    approve(run, "define", by="me")
    assert advance(run, cfg, RunOptions(to_phase="plan")) == "waiting_human"
    for other in ("build/claim", "build/task-1/claim"):  # rows the stop is not
        run.emit("step_started", key=other)
    approve(run, "define/claim", by="me")
    assert advance(run, cfg, RunOptions(to_phase="plan")) == "awaiting_approval"
    steps = run.refresh()["steps"]
    assert steps["define/claim"]["status"] == "done"
    assert steps["build/claim"]["status"] == steps["build/task-1/claim"]["status"] == "running"


# --------------------------------------------------------------------------- the run's own claim commit is gone here


def _forget_own_commit(cflow, sha: str) -> None:
    """Drop this clone's refs to its claim commit `sha` and the object itself, as a `gc` would once a fetch moved the
    local copy away."""
    root = cflow.repo.root
    for name in ("refs/agentflow/remote-claims/b", "refs/agentflow/held-claims/b"):
        run_git(root, "update-ref", "-d", name)
    (root / ".git" / "objects" / sha[:2] / sha[2:]).unlink()
    assert subprocess.run(["git", "cat-file", "-e", sha], cwd=root).returncode != 0


def test_a_lost_claim_is_found_when_the_runs_own_commit_is_gone(cflow, tmp_path):
    cfg = cflow.cfg
    run = start(cfg, cflow.repo.root, "feature", {"module": "b"}, RunOptions())
    other = cflow.clone(tmp_path)
    run_git(other, "push", "-q", "origin", f":{claims.ref(cfg, 'b')}")
    theirs = claims.create(cfg, other, "b", run=OTHER, lane="feature", phase="build")
    _forget_own_commit(cflow, run.refresh()["claim"]["sha"])
    run_control.pause(run, "me", cfg=cfg)
    waiting = run.refresh()["waiting"]
    assert waiting["kind"] == "claim_lost" and "Other" in waiting["summary"] and OTHER in waiting["summary"]
    assert _claim_refs(cflow.repo)[claims.ref(cfg, "b")] == theirs.sha


def test_the_runs_own_claim_is_adopted_from_origin_when_its_commit_is_gone(cflow):
    cfg = cflow.cfg
    run = start(cfg, cflow.repo.root, "feature", {"module": "b"}, RunOptions())
    _forget_own_commit(cflow, run.refresh()["claim"]["sha"])
    run_control.pause(run, "me", cfg=cfg)
    assert _on_origin(cflow) == (run.id, "define", "paused") and "claim_lost" not in _types(run)
    assert run.refresh()["status"] == "paused"


def test_a_gone_commit_and_no_origin_is_only_a_failed_sync(cflow, tmp_path):
    cfg, origin = cflow.cfg, cflow.repo.origin
    run = start(cfg, cflow.repo.root, "feature", {"module": "b"}, RunOptions())
    _forget_own_commit(cflow, run.refresh()["claim"]["sha"])
    shutil.move(origin, tmp_path / "moved.git")
    try:
        run_control.pause(run, "me", cfg=cfg)
    finally:
        shutil.move(tmp_path / "moved.git", origin)
    [failed] = [e for e in run.events.read() if e["type"] == "claim_sync_failed"]
    assert failed["action"] == "advance" and len(failed["error"]) <= 300 and "\n" not in failed["error"]
    state = run.refresh()
    assert "claim_lost" not in _types(run) and state["status"] == "paused" and not state["claim"]["lost"]
