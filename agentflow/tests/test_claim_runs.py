"""A run's claim on its backlog item: the claim at start, made before the run, its worktree and its branch exist, and a
second start of a claimed item refused first. Against a real bare `origin` and real clones."""
from __future__ import annotations

import shutil
import threading
from pathlib import Path

import pytest
from conftest import run_git
from test_backlog_runs import BacklogFlow
from test_claims import OTHER, remote_refs, run_git_input, second_clone

from agentflow import claims, pipeline, slots, work_source
from agentflow.pipeline import RunOptions, start
from agentflow.state import Run, list_runs, runs_root


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
