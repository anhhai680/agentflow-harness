"""Backlog file B2: the runner's own writes to `backlog.yml`, and what a run may and may not do to it."""

from __future__ import annotations

from pathlib import Path

import dataclasses

import pytest
from conftest import run_git

from agentflow import activity, backlog_file, backlog_writes, conditions, gitops, work_source
from agentflow.commit_checks import foreign_commits, runner_head
from agentflow.config import load_config
from agentflow.gitops import CommitRejected, NothingToCommit
from agentflow.pipeline import RunOptions, advance, start
from agentflow.state import list_runs

BACKLOG_YML = """version: 1
items:
  - id: a
    title: Alpha
    status: done
  - id: b
    title: Bee
    status: ready
  - id: i
    title: An idea
    status: backlog
"""
DONE_YML = BACKLOG_YML.replace("status: ready", "status: done")


class BacklogFlow:
    """`flow_repo` as a `backlog` pack with a committed `backlog.yml` (item `b` is ready, item `a` is done, item `i` is
    an idea, `status: backlog`)."""

    def __init__(self, flow_repo) -> None:
        self.repo = flow_repo
        flow_repo.configure({"work_source": {"kind": "backlog", "path": None}})
        (flow_repo.root / "backlog.yml").write_text(BACKLOG_YML, encoding="utf-8")
        run_git(flow_repo.root, "add", "-A")
        run_git(flow_repo.root, "commit", "-q", "-m", "backlog")

    @property
    def cfg(self):
        return self.repo.cfg

    def script(self, **acts: list[dict]) -> None:
        """Script the fake agent, one list of acts per step; `define` stands for the `define/draft` step."""
        self.repo.script({"define/draft" if key == "define" else key: value for key, value in acts.items()})

    def start(self, pipeline: str, module: str | None = None, **options):
        """Start a run and advance it until a human is needed or it ends; returns the run."""
        params = {"module": module} if module else {}
        run = start(self.cfg, self.repo.root, pipeline, params, RunOptions(**options))
        advance(run, self.cfg, RunOptions(**options))
        return run

    def worktree(self, run):
        return Path(run.refresh()["worktree"])

    def events(self, run, etype: str) -> list[dict]:
        return self.repo.events(run, etype)


@pytest.fixture
def bflow(flow_repo) -> BacklogFlow:
    return BacklogFlow(flow_repo)


def test_a_backlog_pack_forbids_its_file_in_a_run(tmp_cfg):
    cfg = load_config(tmp_cfg({"work_source": {"kind": "backlog", "path": "./backlog.yml"}}))
    assert cfg.guardrails.forbidden_paths[-1] == "backlog.yml" and ".agentflow/**" in cfg.guardrails.forbidden_paths


def test_other_kinds_do_not(tmp_cfg):
    assert "backlog.yml" not in load_config(tmp_cfg({})).guardrails.forbidden_paths


def test_an_agent_step_that_edits_the_file_is_a_guard_violation(bflow):
    # `write` is the fake's own hand on the tree, with no Edit tool call: what a shell write looks like to the guard
    bflow.script(define=[{"write": {"backlog.yml": BACKLOG_YML + "# x\n"}}])
    run = bflow.start("feature", module="b")
    waiting = run.refresh()["waiting"]
    assert waiting["kind"] == "guard_violation" and waiting["key"] == "define/draft"
    assert "forbidden_path: backlog.yml" in waiting["summary"]
    assert run_git(bflow.worktree(run), "log", "--format=%s", f"{run.state['base_sha']}..HEAD") == ""



def _started(bflow, module: str = "b"):
    """A `feature` run for `module`, started (its worktree exists) and not advanced."""
    run = start(bflow.cfg, bflow.repo.root, "feature", {"module": module}, RunOptions())
    return run, bflow.worktree(run)


def _done(text: str) -> str:
    return backlog_file.set_fields_text(text, "b", {"status": "done"})


def test_commit_backlog_commits_exactly_the_file(bflow):
    root, before = bflow.repo.root, bflow.repo.head()
    (root / "backlog.yml").write_text(DONE_YML, encoding="utf-8")
    sha = gitops.commit_backlog(root, "Mark b done in the backlog", bflow.cfg, "backlog.yml", DONE_YML, before)
    assert sha == bflow.repo.head() and run_git(root, "rev-parse", f"{sha}^").strip() == before
    assert run_git(root, "diff-tree", "--no-commit-id", "--name-only", "-r", sha).split() == ["backlog.yml"]
    assert run_git(root, "log", "-1", "--format=%B", sha).strip().endswith(bflow.cfg.project.commit_trailer)
    with pytest.raises(NothingToCommit):
        gitops.commit_backlog(root, "Mark b done in the backlog", bflow.cfg, "backlog.yml", DONE_YML, sha)


def test_commit_backlog_refuses_another_path_a_rewritten_file_and_a_moved_head(bflow):
    root, before = bflow.repo.root, bflow.repo.head()
    (root / "docs").mkdir(exist_ok=True)
    (root / "docs" / "x.md").write_text("x\n", encoding="utf-8")
    rewritten = DONE_YML.replace("title: Bee", "title: Wasp")
    cases = [
        ({"path": "docs/x.md", "expected_text": "x\n", "expected_head": before}, DONE_YML, "outside_output"),
        ({"path": "backlog.yml", "expected_text": DONE_YML, "expected_head": before}, rewritten, "outside_output"),
        ({"path": "backlog.yml", "expected_text": DONE_YML, "expected_head": "0" * 40}, DONE_YML, "agent_commit"),
    ]
    for kwargs, on_disk, kind in cases:
        (root / "backlog.yml").write_text(on_disk, encoding="utf-8")
        with pytest.raises(CommitRejected) as caught:
            gitops.commit_backlog(root, "Mark b done in the backlog", bflow.cfg, **kwargs)
        assert caught.value.kind == kind, kwargs
        assert run_git(root, "diff", "--cached", "--name-only") == "" and bflow.repo.head() == before


def test_write_reads_the_committed_file_not_the_work_tree(bflow):
    run, worktree = _started(bflow)
    dirty = BACKLOG_YML + "  - id: c\n    title: Sea\n"
    (worktree / "backlog.yml").write_text(dirty, encoding="utf-8")
    with pytest.raises(CommitRejected) as caught:
        backlog_writes.write(bflow.cfg, run, worktree, "done", _done, "Mark b done in the backlog")
    assert caught.value.kind == "outside_output"
    assert (worktree / "backlog.yml").read_text(encoding="utf-8") == dirty
    assert gitops.head(worktree) == run.state["base_sha"] and bflow.events(run, "backlog_updated") == []


def test_a_backlog_commit_is_a_runner_commit(bflow):
    run, worktree = _started(bflow)
    base = run.state["base_sha"]
    before_tree = gitops.head_tree(worktree)
    sha = backlog_writes.write(bflow.cfg, run, worktree, "done", _done, "Mark b done in the backlog")
    assert sha == gitops.head(worktree) == runner_head(run)
    assert foreign_commits(run, bflow.cfg, worktree, base) == []
    assert run.state["backlog_sha"] == sha and run.refresh()["backlog_sha"] == sha
    [event] = bflow.events(run, "backlog_updated")
    assert {k: event.get(k) for k in ("item", "change", "fields", "sha", "path", "before_tree", "tree")} == {
        "item": "b", "change": "done", "fields": {"status": "done"}, "sha": sha, "path": "backlog.yml",
        "before_tree": before_tree, "tree": gitops.head_tree(worktree),
    }
    assert "local" not in event
    assert activity.sentence(event) == (f"Marked b done in the backlog ({sha[:7]})", "status: done")
    assert activity.in_kind(event, "commits")
    assert (worktree / "backlog.yml").read_text(encoding="utf-8") == DONE_YML
    assert backlog_writes.write(bflow.cfg, run, worktree, "done", _done, "Mark b done in the backlog") is None
    assert len(bflow.events(run, "backlog_updated")) == 1


def test_applies_only_to_a_module_run_of_the_items_lane(bflow):
    run, _ = _started(bflow)
    cfg = bflow.cfg
    item = backlog_file.BacklogItem("b", [], title="Bee", status="ready")
    assert backlog_writes.applies(cfg, run, item) and backlog_writes.applies(cfg, run, None)
    assert not backlog_writes.applies(cfg, run, dataclasses.replace(item, lane="bug"))
    assert not backlog_writes.applies(dataclasses.replace(cfg, work_source=None), run, item)


def test_a_refused_backlog_commit_puts_the_committed_file_back(bflow):
    run, worktree = _started(bflow)
    with pytest.raises(CommitRejected) as caught:
        backlog_writes.write(bflow.cfg, run, worktree, "done", _done, "Mark b done\n\nConstraints-Approved-By: me")
    assert caught.value.kind == "rejected_trailer"
    assert (worktree / "backlog.yml").read_text(encoding="utf-8") == BACKLOG_YML
    assert run_git(worktree, "status", "--porcelain") == "" and bflow.events(run, "backlog_updated") == []


# --------------------------------------------------------------------------- the first commit


def _first(bflow, module: str, new_module: dict | None = None, **params):
    """A `feature` run for `module`, started and not advanced: `(run, worktree)`."""
    run = start(bflow.cfg, bflow.repo.root, "feature", {"module": module, **params}, RunOptions(new_module=new_module))
    return run, bflow.worktree(run)


def _changed(worktree: Path, sha: str) -> list[str]:
    return run_git(worktree, "diff-tree", "--no-commit-id", "--name-only", "-r", sha).split()


def _at(bflow, worktree: Path, sha: str) -> list[backlog_file.BacklogItem]:
    return backlog_file.parse(run_git(worktree, "show", f"{sha}:backlog.yml"))[0]


LOCAL = "  - id: l\n    title: Local\n    status: backlog\n"


def test_a_new_module_run_appends_the_item_as_its_first_commit(bflow):
    new = {"module": "n", "responsibility": "Do the new thing", "deps": ["b"]}
    run, worktree = _first(bflow, "n", new)
    head = gitops.head(worktree)
    assert run_git(worktree, "rev-parse", f"{head}^").strip() == run.state["base_sha"]
    assert _changed(worktree, head) == ["backlog.yml"]
    assert run_git(worktree, "log", "-1", "--format=%s", head).strip() == "Add n to the backlog"
    items = _at(bflow, worktree, head)
    assert [i.id for i in items] == ["a", "b", "i", "n"]
    assert (items[-1].status, items[-1].title, items[-1].deps, items[-1].issue) == ("ready", "Do the new thing", ["b"], None)
    [event] = bflow.events(run, "backlog_updated")
    assert (event["change"], event["local"], event["sha"]) == ("added", False, head)
    assert run.refresh()["backlog_sha"] == head and runner_head(run) == head
    default = conditions.make_ctx(bflow.cfg, bflow.repo.root, "feature", {"module": "zz"}).values["spec"]
    ctx = conditions.make_ctx(bflow.cfg, bflow.repo.root, "feature", run.state["params"], run)
    assert ctx.values["spec"] == default.replace("zz", "n")
    assert conditions._fork(run) == (bflow.repo.root.resolve(), head)
    assert "n" in [i.id for i in work_source.items_at(bflow.cfg, *conditions._fork(run))]


def test_a_new_module_with_an_issue_records_it_and_a_long_responsibility_is_cut(bflow):
    new = {"module": "issue-7", "responsibility": "GitHub issue #7\n" + "x" * 300, "deps": []}
    plan = backlog_writes.plan_start(bflow.cfg, bflow.repo.root, "feature", {"module": "issue-7", "issue": "#7"}, new)
    assert (plan.change, plan.local, plan.item.id, plan.item.issue, plan.item.status) == (
        "added", False, "issue-7", 7, "ready")
    assert len(plan.item.title) == 200 and plan.item.title.startswith("GitHub issue #7 xxx")
    assert plan.item.title.endswith("…")
    run, worktree = _first(bflow, "issue-7", new)
    item = _at(bflow, worktree, gitops.head(worktree))[-1]
    assert (item.id, item.title, item.issue) == ("issue-7", plan.item.title, None)


def test_a_local_only_item_goes_into_the_first_commit_and_the_work_tree_is_untouched(bflow):
    main = bflow.repo.root / "backlog.yml"
    main.write_text(BACKLOG_YML + LOCAL, encoding="utf-8")
    before = main.read_bytes()
    run, worktree = _first(bflow, "l")
    head = gitops.head(worktree)
    assert _changed(worktree, head) == ["backlog.yml"]
    item = _at(bflow, worktree, head)[-1]
    assert (item.id, item.title, item.status) == ("l", "Local", "ready")
    [event] = bflow.events(run, "backlog_updated")
    assert (event["change"], event["local"]) == ("added", True)
    assert main.read_bytes() == before


def test_starting_a_backlog_status_item_sets_ready_in_the_first_commit(bflow):
    run, worktree = _first(bflow, "i")
    head = gitops.head(worktree)
    assert run_git(worktree, "rev-list", "--count", f"{run.state['base_sha']}..{head}").strip() == "1"
    assert run_git(worktree, "log", "-1", "--format=%s", head).strip() == "Mark i ready in the backlog"
    diff = run_git(worktree, "diff", "-U0", run.state["base_sha"], head).splitlines()
    assert [ln for ln in diff if ln[:1] in "+-" and ln[:3] not in ("+++", "---")] == [
        "-    status: backlog", "+    status: ready"]
    [event] = bflow.events(run, "backlog_updated")
    assert (event["change"], event["fields"]) == ("ready", {"status": "ready"}) and "local" not in event


def test_a_ready_item_gets_no_first_commit(bflow):
    run, worktree = _first(bflow, "b")
    assert gitops.head(worktree) == run.state["base_sha"] and bflow.events(run, "backlog_updated") == []
    assert conditions._fork(run) == (bflow.repo.root.resolve(), run.state["base_sha"])


def test_another_lane_or_kind_writes_nothing(bflow):
    run = start(bflow.cfg, bflow.repo.root, "bug", {"module": "a", "task": "fix it"}, RunOptions())
    assert gitops.head(bflow.worktree(run)) == run.state["base_sha"]
    run = start(bflow.cfg, bflow.repo.root, "bug", {"module": "nope", "task": "fix it"}, RunOptions())
    assert gitops.head(bflow.worktree(run)) == run.state["base_sha"]


REFUSALS = {
    "unknown": ("nope", None, None,
                "module nope is not in backlog.yml: add it (agentflow backlog add nope --title …) or start it from the "
                "dashboard"),
    "done": ("a", None, None, "module a is done in backlog.yml: set it to ready first"),
    "local_dep": ("l2", None, LOCAL + "  - id: l2\n    title: Two\n    status: ready\n    depends_on: [l]\n",
                  "l2 depends on l, which is not in backlog.yml on {fork}: commit it first"),
    "new_dup": ("b", {"module": "b", "responsibility": "again", "deps": []}, None,
                "b is already an item in backlog.yml: start module:b"),
    "new_dup_local": ("l", {"module": "l", "responsibility": "again", "deps": []}, LOCAL,
                      "l is already an item in backlog.yml: start module:l"),
    "new_dep": ("n", {"module": "n", "responsibility": "r", "deps": ["l"]}, LOCAL,
                "n depends on l, which is not in backlog.yml on {fork}: commit it first"),
}


@pytest.mark.parametrize("case", sorted(REFUSALS))
def test_refusals_leave_no_run(bflow, case):
    module, new, extra, message = REFUSALS[case]
    if extra:
        (bflow.repo.root / "backlog.yml").write_text(BACKLOG_YML + extra, encoding="utf-8")
    branches = run_git(bflow.repo.root, "branch", "--list")
    with pytest.raises(ValueError) as caught:
        start(bflow.cfg, bflow.repo.root, "feature", {"module": module}, RunOptions(new_module=new))
    assert str(caught.value) == message.format(fork=bflow.cfg.project.fork_from)
    assert list_runs(bflow.repo.root) == [] and run_git(bflow.repo.root, "branch", "--list") == branches
