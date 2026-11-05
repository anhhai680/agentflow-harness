"""`finish` brings the latest `origin/<fork_from>` into the branch before it opens the pull request.

The fixture repo forks from `next` and its `project.base_branch` is `main`, so a test that sees a commit from
`next` and none from `main` also shows the target is `fork_from`.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from agentflow.config import load_config, validate
from agentflow.gitops import merge_in_progress
from agentflow.hitl import approve
from agentflow.pipeline import RunOptions, advance, start
from conftest import PYTHON_CONFIG, run_git

AGENTFLOW = Path(__file__).resolve().parents[1]
BIN = AGENTFLOW / "tests" / "bin"
REPO = AGENTFLOW.parent
URL = "https://github.com/acme/repo/pull/42"
SYNC = "ship/finish/sync-{n}"
BASE = {"gate_sets": {"build": ["ok"], "verify": ["ok"]}}


@pytest.fixture
def gh(tmp_path, monkeypatch):
    log = tmp_path / "gh.jsonl"
    monkeypatch.setenv("PATH", f"{BIN}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_GH_LOG", str(log))
    monkeypatch.setenv("FAKE_GH_URL", URL)
    monkeypatch.delenv("FAKE_GH_MERGEABLE", raising=False)
    return log


def gh_calls(log: Path) -> list[list[str]]:
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line] if log.exists() else []


def verbs(log: Path, verb: str) -> list[list[str]]:
    return [call for call in gh_calls(log) if call[:2] == ["pr", verb]]


def begin(flow_repo, patch=None):
    flow_repo.configure({**BASE, **(patch or {})})
    return start(flow_repo.cfg, flow_repo.root, "feature", {"module": "demo"}, RunOptions(from_phase="ship"))


def write(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")


def target_moves(flow_repo, git, files: dict[str, str], branch: str = "next") -> None:
    """A teammate's commit lands on `origin/<branch>` (the fixture's main checkout is on `next`)."""
    on = git(flow_repo.root, "rev-parse", "--abbrev-ref", "HEAD").strip()
    git(flow_repo.root, "checkout", "-q", branch)
    write(flow_repo.root, files)
    git(flow_repo.root, "add", "-A")
    git(flow_repo.root, "commit", "-q", "-m", "upstream change")
    git(flow_repo.root, "push", "-q", "origin", branch)
    git(flow_repo.root, "checkout", "-q", on)


def own_work(flow_repo, run, files: dict[str, str]) -> None:
    """A commit the runner made on the run's branch (made with git directly: the runner's own `commit` refuses
    the forbidden paths some tests change on purpose, as an earlier, human-approved commit could have)."""
    worktree = Path(run.state["worktree"])
    write(worktree, files)
    run_git(worktree, "add", "-A")
    run_git(worktree, "-c", "user.name=Test", "-c", "user.email=t@example.com", "commit", "-q", "-m", "Own work")
    sha = run_git(worktree, "rev-parse", "HEAD").strip()
    run.emit("step_committed", key="build/own", sha=sha, summary="own work")


def on_origin(git, flow_repo, run, path: str) -> str:
    return git(flow_repo.origin, "show", f"{run.state['branch']}:{path}")


def go(flow_repo, run) -> str:
    return advance(run, flow_repo.cfg, RunOptions())


def pushed(git, flow_repo, run) -> bool:
    return run.state["branch"] in git(flow_repo.origin, "branch", "--list")


def resolving(path: str, text: str) -> dict:
    return {"write": {path: text}, "result": {"status": "done", "summary": f"resolved {path}"}}


# --------------------------------------------------------------------------- a clean merge


def test_the_target_is_brought_in_before_the_pull_request(flow_repo, gh, git):
    run = begin(flow_repo)
    own_work(flow_repo, run, {"mine.txt": "mine\n"})
    target_moves(flow_repo, git, {"upstream.txt": "theirs\n"})
    target_moves(flow_repo, git, {"main-only.txt": "not for this PR\n"}, branch="main")
    assert go(flow_repo, run) == "done"
    assert on_origin(git, flow_repo, run, "upstream.txt") == "theirs\n"
    assert on_origin(git, flow_repo, run, "mine.txt") == "mine\n"
    assert "main-only.txt" not in git(flow_repo.origin, "ls-tree", "--name-only", run.state["branch"])
    (synced,) = flow_repo.events(run, "base_synced")
    assert synced["branch"] == "next" and synced["conflicts"] == [] and synced["gates"] == "ok ✓"
    parents = git(Path(run.state["worktree"]), "rev-list", "--parents", "-n", "1", "HEAD").split()[1:]
    assert len(parents) == 2 and parents[1] == synced["target"]
    assert [c["step"] for c in flow_repo.calls()] == []  # a clean merge that passes calls no agent
    (create,) = verbs(gh, "create")
    assert create[create.index("--base") + 1] == "next"
    assert flow_repo.events(run, "pr_opened") and not merge_in_progress(Path(run.state["worktree"]))
    body = (run.dir / "pr.md").read_text(encoding="utf-8")
    assert "Merge from the target branch" in body and "`origin/next`" in body


def test_a_branch_that_is_up_to_date_makes_no_merge_commit(flow_repo, gh, git):
    run = begin(flow_repo)
    own_work(flow_repo, run, {"mine.txt": "mine\n"})
    before = git(Path(run.state["worktree"]), "rev-parse", "HEAD").strip()
    assert go(flow_repo, run) == "done"
    assert git(Path(run.state["worktree"]), "rev-parse", "HEAD").strip() == before
    assert not flow_repo.events(run, "base_synced") and len(verbs(gh, "create")) == 1


def test_a_target_change_to_a_forbidden_path_is_not_the_branchs(flow_repo, gh, git):
    run = begin(flow_repo)
    own_work(flow_repo, run, {"mine.txt": "mine\n"})
    target_moves(flow_repo, git, {"CONSTRAINTS.md": "rules\nand a new one\n"})
    assert go(flow_repo, run) == "done"  # the guard looks at what the merge decided, not what came in
    assert on_origin(git, flow_repo, run, "CONSTRAINTS.md") == "rules\nand a new one\n"


def test_a_forbidden_file_changed_on_both_sides_is_a_human_stop(flow_repo, gh, git):
    run = begin(flow_repo)
    own_work(flow_repo, run, {"CONSTRAINTS.md": "top\nrules\n"})
    target_moves(flow_repo, git, {"CONSTRAINTS.md": "rules\nbottom\n"})  # merges cleanly into a third version
    assert go(flow_repo, run) == "waiting_human"
    assert run.state["waiting"]["kind"] == "guard_violation" and "CONSTRAINTS.md" in run.state["waiting"]["summary"]
    assert not pushed(git, flow_repo, run) and not verbs(gh, "create")


# --------------------------------------------------------------------------- conflicts


def conflict(flow_repo, run, git) -> None:
    own_work(flow_repo, run, {"README.md": "hello from the branch\n"})
    target_moves(flow_repo, git, {"README.md": "hello from the target\n"})


def test_a_conflict_is_resolved_by_the_session_and_the_runner_commits_it(flow_repo, gh, git):
    run = begin(flow_repo)
    conflict(flow_repo, run, git)
    flow_repo.script({SYNC.format(n=1): [resolving("README.md", "hello from both\n")]})
    assert go(flow_repo, run) == "done"
    assert on_origin(git, flow_repo, run, "README.md") == "hello from both\n"
    assert [c["step"] for c in flow_repo.calls()] == [SYNC.format(n=1)]
    (synced,) = flow_repo.events(run, "base_synced")
    assert synced["conflicts"] == ["README.md"] and synced["notes"] == ["resolved README.md"]
    assert flow_repo.events(run, "pr_opened") and len(verbs(gh, "create")) == 1
    assert "resolved README.md" in (run.dir / "pr.md").read_text(encoding="utf-8")


def test_markers_left_behind_are_never_committed(flow_repo, gh, git):
    run = begin(flow_repo)
    conflict(flow_repo, run, git)
    marked = resolving("README.md", "<<<<<<< HEAD\nmine\n=======\ntheirs\n>>>>>>> next\n")
    flow_repo.script({SYNC.format(n=1): [marked], SYNC.format(n=2): [marked]})  # ci.sync_rounds = 2
    assert go(flow_repo, run) == "waiting_human"
    assert run.state["waiting"]["kind"] == "blocked"
    assert merge_in_progress(Path(run.state["worktree"]))  # left for a human to finish
    assert not pushed(git, flow_repo, run) and not verbs(gh, "create") and not flow_repo.events(run, "base_synced")


def test_a_human_finishes_the_merge_and_the_run_goes_on(flow_repo, gh, git):
    run = begin(flow_repo)
    conflict(flow_repo, run, git)
    marked = resolving("README.md", "<<<<<<< HEAD\nmine\n=======\ntheirs\n>>>>>>> next\n")
    flow_repo.script({SYNC.format(n=1): [marked], SYNC.format(n=2): [marked]})
    assert go(flow_repo, run) == "waiting_human"
    worktree = Path(run.state["worktree"])
    write(worktree, {"README.md": "settled by a person\n"})
    git(worktree, "add", "README.md")
    approve(run, "ship/finish", by="me")
    assert go(flow_repo, run) == "done"  # the half-done merge is picked up, not refused as a dirty tree
    assert on_origin(git, flow_repo, run, "README.md") == "settled by a person\n"


@pytest.mark.parametrize("path", ["CONSTRAINTS.md", "uv.lock", "toy/staff_auth/tokens.py"])
def test_conflicts_in_protected_paths_are_a_humans_without_calling_the_agent(flow_repo, gh, git, path):
    run = begin(flow_repo)
    own_work(flow_repo, run, {path: "branch\n"})
    target_moves(flow_repo, git, {path: "target\n"})
    assert go(flow_repo, run) == "waiting_human"
    assert path in run.state["waiting"]["summary"]
    assert flow_repo.calls() == [] and not pushed(git, flow_repo, run) and not verbs(gh, "create")


def test_without_a_sync_session_a_conflict_is_a_humans(flow_repo, gh, git):
    run = begin(flow_repo)
    conflict(flow_repo, run, git)
    flow_repo.cfg.phases["ship"].steps[1].sync = None
    assert go(flow_repo, run) == "waiting_human"
    assert flow_repo.calls() == [] and not pushed(git, flow_repo, run)


# --------------------------------------------------------------------------- the merged tree must pass


NEEDS_FIX = {"gates": {"needs_fix": {"run": "test -f fixed.txt"}}, "gate_sets": {"verify": ["needs_fix"]}}


def test_a_clean_merge_that_breaks_a_gate_is_fixed_before_it_is_committed(flow_repo, gh, git):
    run = begin(flow_repo, NEEDS_FIX)
    own_work(flow_repo, run, {"mine.txt": "mine\n"})
    target_moves(flow_repo, git, {"upstream.txt": "theirs\n"})
    flow_repo.script({SYNC.format(n=1): [{"write": {"fixed.txt": "ok\n"},
                                          "result": {"status": "done", "summary": "adapted to the new code"}}]})
    assert go(flow_repo, run) == "done"
    assert on_origin(git, flow_repo, run, "fixed.txt") == "ok\n" and on_origin(git, flow_repo, run, "upstream.txt")
    (synced,) = flow_repo.events(run, "base_synced")
    assert synced["notes"] == ["adapted to the new code"] and synced["gates"] == "needs_fix ✓"


def test_a_merge_the_gates_never_accept_is_not_committed_or_pushed(flow_repo, gh, git):
    run = begin(flow_repo, {"gates": {"never": {"run": "exit 1"}}, "gate_sets": {"verify": ["never"]}})
    own_work(flow_repo, run, {"mine.txt": "mine\n"})
    target_moves(flow_repo, git, {"upstream.txt": "theirs\n"})
    assert go(flow_repo, run) == "waiting_human"
    assert run.state["waiting"]["kind"] != "ci_failed"
    assert merge_in_progress(Path(run.state["worktree"]))
    assert not pushed(git, flow_repo, run) and not verbs(gh, "create") and not flow_repo.events(run, "base_synced")


def test_a_pack_without_an_eval_condition_checks_the_merge_without_the_eval_gate(flow_repo, gh, git):
    """`eval_required` is a pack's condition, not the engine's: a pack that defines none never runs the eval gate."""
    run = begin(flow_repo, {"conditions": {"eval_required": None}})
    own_work(flow_repo, run, {"mine.txt": "mine\n"})
    target_moves(flow_repo, git, {"upstream.txt": "theirs\n"})
    assert go(flow_repo, run) == "done"
    (synced,) = flow_repo.events(run, "base_synced")
    assert "eval" not in synced["gates"] and on_origin(git, flow_repo, run, "upstream.txt") == "theirs\n"


# --------------------------------------------------------------------------- after the pull request exists


def test_a_pull_request_github_calls_conflicting_is_synced_again(flow_repo, gh, git, monkeypatch):
    monkeypatch.setenv("FAKE_GH_MERGEABLE", "CONFLICTING,MERGEABLE")
    run = begin(flow_repo)
    own_work(flow_repo, run, {"mine.txt": "mine\n"})
    assert go(flow_repo, run) == "done"
    assert len(verbs(gh, "view")) == 2 and len(verbs(gh, "create")) == 1


def test_a_pull_request_that_stays_conflicting_is_a_humans(flow_repo, gh, git, monkeypatch):
    monkeypatch.setenv("FAKE_GH_MERGEABLE", "CONFLICTING")
    run = begin(flow_repo)
    own_work(flow_repo, run, {"mine.txt": "mine\n"})
    assert go(flow_repo, run) == "waiting_human"
    assert run.state["waiting"]["kind"] == "blocked" and "still conflicts" in run.state["waiting"]["summary"]
    assert len(verbs(gh, "view")) == 3  # the first look and one per `ci.sync_rounds`


def test_an_unclear_verdict_is_left_to_ci(flow_repo, gh, git, monkeypatch):
    monkeypatch.setenv("FAKE_GH_MERGEABLE", "UNKNOWN")
    run = begin(flow_repo)
    own_work(flow_repo, run, {"mine.txt": "mine\n"})
    assert go(flow_repo, run) == "done"


# --------------------------------------------------------------------------- config


def issues(tmp_cfg, patch):
    return validate(load_config(tmp_cfg(patch)), REPO)


def test_the_shipped_config_has_a_sync_session():
    cfg = load_config(PYTHON_CONFIG)
    finish = next(step for step in cfg.phases["ship"].steps if step.type == "finish")
    assert finish.sync is not None and finish.sync.gates == "verify" and finish.sync.commit is False
    assert not [i for i in validate(cfg, REPO) if i.level == "error"]


def test_only_a_finish_step_has_a_sync_session(tmp_cfg):
    def patch(data):
        ship = data["phases"]["ship"]["steps"]
        next(step for step in ship if step.get("id") == "ci")["sync"] = {"id": "sync", "skill": "systematic-debugging",
                                                                         "prompt": "sync_conflicts.md"}

    assert any("only a finish step has `sync`" in i.message for i in issues(tmp_cfg, patch))


def test_sync_rounds_is_a_ci_setting(tmp_cfg):
    assert any(i.path == "ci.sync_rounds" for i in issues(tmp_cfg, lambda data: data["ci"].pop("sync_rounds")))
    assert any(i.path == "ci.sync_rounds" for i in issues(tmp_cfg, {"ci": {"sync_rounds": -1}}))
