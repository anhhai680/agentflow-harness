"""Backlog file B2: the runner's own writes to `backlog.yml`, and what a run may and may not do to it."""

from __future__ import annotations

from pathlib import Path

import dataclasses

import pytest
import test_conformance
from conftest import land_on_origin, run_git
from test_conformance import fully_met

from agentflow import activity, backlog_file, backlog_writes, conditions, gitops, work_source
from agentflow.commit_checks import foreign_commits, runner_head
from agentflow.config import load_config
from agentflow.gitops import CommitRejected, NothingToCommit
from agentflow.hitl import approve
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

# The fake `gh` fixture of whole runs, bound by assignment so pytest finds it here.
ci_gh = test_conformance.ci_gh


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


def test_a_new_module_outside_the_module_lane_names_the_runs_lane(bflow):
    new = {"module": "n", "responsibility": "Fix the new thing", "deps": []}
    run = start(bflow.cfg, bflow.repo.root, "bug", {"module": "n", "task": "fix it"}, RunOptions(new_module=new))
    worktree = bflow.worktree(run)
    item = _at(bflow, worktree, gitops.head(worktree))[-1]
    assert (item.id, item.lane, item.status) == ("n", "bug", "ready")
    assert "    lane: bug\n" in run_git(worktree, "show", "HEAD:backlog.yml")
    assert backlog_writes.applies(bflow.cfg, run, item)
    run, worktree = _first(bflow, "m", {"module": "m", "responsibility": "M", "deps": []})  # the module lane: no lane
    assert _at(bflow, worktree, gitops.head(worktree))[-1].lane == ""


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


# --------------------------------------------------------------------------- spec and plan after their step

SPEC = "docs/specs/SPEC-b.md"
SPEC_TEXT = "# Spec: `b`\n\n**Status: approved**\n\n## Open Questions\n\nNone for this module.\n"
DONE_ACT = {"result": {"status": "done", "summary": "ok"}}


def _drafted(bflow, text: str = SPEC_TEXT):
    """`feature` run of item `b` up to the end of the define phase: the draft step wrote `SPEC`."""
    bflow.script(define=[{"write": {SPEC: text}, **DONE_ACT}])
    return bflow.start("feature", module="b", to_phase="define")


def _recorded(bflow, run, change: str) -> dict:
    [event] = [e for e in bflow.events(run, "backlog_updated") if e["change"] == change]
    return event


def _commits_after(run, worktree: Path, key: str) -> tuple[str, str]:
    """`(step sha, the commit right after it)` for the step `key`."""
    [step] = [e for e in run.events.read() if e["type"] == "step_committed" and e["key"] == key]
    after = run_git(worktree, "rev-list", "--reverse", f"{step['sha']}..HEAD").split()
    return step["sha"], after[0] if after else ""


def test_define_records_the_spec_path_it_wrote(bflow):
    run = _drafted(bflow)
    worktree = bflow.worktree(run)
    step, after = _commits_after(run, worktree, "define/draft")
    assert after and run_git(worktree, "log", "-1", "--format=%s", after).strip() == "Record the b spec in the backlog"
    assert _changed(worktree, after) == ["backlog.yml"]
    assert run_git(worktree, "rev-parse", f"{after}^").strip() == step
    [item] = [i for i in _at(bflow, worktree, "HEAD") if i.id == "b"]
    assert item.spec == SPEC
    assert _recorded(bflow, run, "spec")["fields"] == {"spec": SPEC}
    assert run.refresh()["waiting"]["kind"] != "guard_violation"
    assert not [e for e in bflow.events(run, "backlog_updated") if e["change"] == "plan"]


def test_plan_records_the_plan_path(bflow):
    run = _drafted(bflow)
    values = conditions.make_ctx(bflow.cfg, bflow.repo.root, "feature", run.state["params"], run).values
    bflow.repo.script({"define/draft": [{"write": {SPEC: SPEC_TEXT}, **DONE_ACT}],
                       "plan/plan": [{"write": {values["plan_target"]: "# Plan: b\n\n### Task 1: A\n\n- [ ] **Step 1: x**\n"},
                                      **DONE_ACT}]})
    approve(run, "define", by="me")
    advance(run, bflow.cfg, RunOptions(to_phase="plan"))
    worktree = bflow.worktree(run)
    step, after = _commits_after(run, worktree, "plan/plan")
    assert after and run_git(worktree, "log", "-1", "--format=%s", after).strip() == "Record the b plan in the backlog"
    assert _changed(worktree, after) == ["backlog.yml"]
    [item] = [i for i in _at(bflow, worktree, "HEAD") if i.id == "b"]
    assert (item.spec, item.plan) == (SPEC, values["plan_target"])
    assert _recorded(bflow, run, "plan")["fields"] == {"plan": values["plan_target"]}
    assert run.refresh()["waiting"]["kind"] != "guard_violation"


def test_a_spec_the_item_already_names_writes_nothing(bflow):
    (bflow.repo.root / "backlog.yml").write_text(BACKLOG_YML.replace(
        "    title: Bee\n", f"    title: Bee\n    spec: {SPEC}\n"), encoding="utf-8")
    run_git(bflow.repo.root, "commit", "-q", "-am", "b names its spec")
    run = _drafted(bflow)
    assert [e for e in bflow.events(run, "step_committed") if e["key"] == "define/draft"]
    assert [e for e in bflow.events(run, "backlog_updated") if e["change"] == "spec"] == []
    worktree = bflow.worktree(run)
    assert run_git(worktree, "diff", "--name-only", run.state["base_sha"], "HEAD").split() == [SPEC]


def test_a_reconcile_step_that_rewrites_the_same_spec_writes_nothing(bflow):
    from conftest import with_doubt

    bflow.repo.configure({"work_source": {"kind": "backlog", "path": None}}, autopilot=True, edit=with_doubt)
    high = {"severity": "high", "file": SPEC, "line": 3, "text": "Thiếu trường hợp rollback"}
    pending = SPEC_TEXT.replace("**Status: approved**", "**Status: proposed** (pending approval)")
    bflow.script(**{
        "define": [{"write": {SPEC: pending}, **DONE_ACT}],
        "define/spec_review": [{"result": {"status": "done", "summary": "found", "findings": [high]}},
                               {"result": {"status": "done", "summary": "clean"}}],
        "define/spec_reconcile": [{"write": {SPEC: pending + "\nRollback.\n"},
                                   "result": {"status": "done", "summary": "revised", "dispositions": [
                                       {"finding": "R1-F1", "class": "fixed", "note": "ok"}]}}],
    })
    run = bflow.start("feature", module="b", to_phase="define")
    worktree = bflow.worktree(run)
    assert [e["key"] for e in bflow.events(run, "step_committed")][:2] == ["define/draft", "define/spec_reconcile"]
    assert [e["change"] for e in bflow.events(run, "backlog_updated")] == ["spec"]
    waiting = run.refresh().get("waiting") or {}
    assert waiting.get("kind") != "guard_violation", waiting
    assert (worktree / SPEC).read_text(encoding="utf-8").endswith("Rollback.\n")


def test_a_crash_after_the_step_commit_is_recorded_on_resume(bflow, monkeypatch):
    real = backlog_writes.after_output

    def crash(*args, **kwargs):
        raise RuntimeError("the runner died here")

    bflow.script(define=[{"write": {SPEC: SPEC_TEXT}, **DONE_ACT}])
    run = start(bflow.cfg, bflow.repo.root, "feature", {"module": "b"}, RunOptions(to_phase="define"))
    monkeypatch.setattr(backlog_writes, "after_output", crash)
    with pytest.raises(RuntimeError, match="died"):
        advance(run, bflow.cfg, RunOptions(to_phase="define"))
    assert bflow.events(run, "backlog_updated") == []
    monkeypatch.setattr(backlog_writes, "after_output", real)
    advance(run, bflow.cfg, RunOptions(to_phase="define"))
    assert _recorded(bflow, run, "spec")["fields"] == {"spec": SPEC}
    worktree = bflow.worktree(run)
    assert [i.spec for i in _at(bflow, worktree, "HEAD") if i.id == "b"] == [SPEC]
    assert run.refresh()["waiting"]["kind"] != "guard_violation"
    assert len([e for e in bflow.events(run, "backlog_updated") if e["change"] == "spec"]) == 1


# --------------------------------------------------------------------------- done in `finish`

FINISH = "ship/finish"
PLAN_TEXT = "# Plan: b\n\n### Task 1: One\n\n**Depends on:** none\n\n- [ ] **Step 1: Do it**\n"
NOTED = BACKLOG_YML.replace("    status: ready\n", "    status: ready\n    notes: Seen by a teammate\n")


def _whole(bflow, **acts: list[dict]):
    """A `feature` run of item `b` with the workflow as shipped and the `ok` gate, started and not advanced; its define
    and plan steps write the spec and the plan."""
    from test_ci import PIPE

    bflow.repo.configure({**PIPE, "work_source": {"kind": "backlog", "path": None}}, autopilot=True)
    run = start(bflow.cfg, bflow.repo.root, "feature", {"module": "b"}, RunOptions())
    plan = conditions.make_ctx(bflow.cfg, bflow.repo.root, "feature", run.state["params"], run).values["plan_target"]
    bflow.repo.script({"define/draft": [{"write": {SPEC: SPEC_TEXT}, **DONE_ACT}],
                       "plan/plan": [{"write": {plan: PLAN_TEXT}, **DONE_ACT}], **acts})
    return run, plan


def _walk(bflow, run, at_plan=None) -> str:
    """Advance `run`, approving each phase that asks; `at_plan()` runs once, when the plan waits for approval."""
    status = advance(run, bflow.cfg, RunOptions())
    while status == "awaiting_approval":
        phase = run.state["waiting"]["phase"]
        if phase == "plan" and at_plan is not None:
            at_plan()
        approve(run, phase, by="me")
        status = advance(run, bflow.cfg, RunOptions())
    return status


def _done_events(bflow, run) -> list[dict]:
    return [e for e in bflow.events(run, "backlog_updated") if e["change"] == "done"]


def _pushed_item(bflow, run, item_id: str = "b") -> backlog_file.BacklogItem:
    text = run_git(bflow.repo.origin, "show", f"{run.state['branch']}:backlog.yml")
    [item] = [i for i in backlog_file.parse(text)[0] if i.id == item_id]
    return item


def _diff_lines(worktree: Path, sha: str) -> list[str]:
    diff = run_git(worktree, "diff", "-U0", f"{sha}^", sha).splitlines()
    return [ln for ln in diff if ln[:1] in "+-" and ln[:3] not in ("+++", "---")]


def test_finish_marks_the_item_done_before_the_push_and_reruns_no_gate(bflow, ci_gh):
    run, plan = _whole(bflow)
    assert _walk(bflow, run) == "done", run.state["waiting"]
    [done] = _done_events(bflow, run)
    [opened] = [e for e in bflow.events(run, "pr_opened") if e["key"] == FINISH]
    worktree = bflow.worktree(run)
    assert opened["sha"] == done["sha"] and done["seq"] < opened["seq"]
    assert run_git(worktree, "log", "-1", "--format=%s", done["sha"]).strip() == "Mark b done in the backlog"
    assert _changed(worktree, done["sha"]) == ["backlog.yml"]
    assert _diff_lines(worktree, done["sha"]) == ["-    status: ready", "+    status: done"]
    item = _pushed_item(bflow, run)
    assert (item.status, item.spec, item.plan) == ("done", SPEC, plan)
    assert not [e for e in run.events.read() if e["type"] == "gate_result" and e["seq"] > done["seq"]]
    fully_met(bflow.repo, run)
    (checked,) = bflow.events(run, "conformance_checked")
    assert checked["tree"] == done["tree"]


def test_finish_keeps_a_teammates_edit_merged_by_the_sync(bflow, ci_gh, tmp_path):
    run_git(bflow.repo.root, "push", "-q", "origin", "next")
    run, plan = _whole(bflow)
    status = _walk(bflow, run, at_plan=lambda: land_on_origin(bflow.repo, tmp_path, {"backlog.yml": NOTED}))
    assert status == "done", run.state["waiting"]
    [synced] = bflow.events(run, "base_synced")
    [done] = _done_events(bflow, run)
    worktree = bflow.worktree(run)
    assert synced["conflicts"] == [] and run_git(worktree, "rev-parse", f"{done['sha']}^").strip() == synced["sha"]
    assert _diff_lines(worktree, done["sha"]) == ["-    status: ready", "+    status: done"]
    item = _pushed_item(bflow, run)
    assert (item.status, item.notes, item.spec, item.plan) == ("done", "Seen by a teammate", SPEC, plan)
    fully_met(bflow.repo, run)


def test_finish_leaves_an_item_a_teammate_dropped(bflow, ci_gh, tmp_path):
    run_git(bflow.repo.root, "push", "-q", "origin", "next")
    run, _ = _whole(bflow)
    dropped = BACKLOG_YML.replace("    title: Bee\n    status: ready\n", "    title: Bee\n    status: dropped\n")
    status = _walk(bflow, run, at_plan=lambda: land_on_origin(bflow.repo, tmp_path, {"backlog.yml": dropped}))
    assert status == "done", run.state["waiting"]
    assert bflow.events(run, "base_synced") and _done_events(bflow, run) == []
    [skipped] = bflow.events(run, "backlog_skipped")
    assert (skipped["change"], skipped["reason"]) == ("done", "b is dropped in backlog.yml at HEAD")
    assert _pushed_item(bflow, run).status == "dropped"
    assert backlog_writes.mark_done(bflow.cfg, run, bflow.worktree(run)) is None  # said once
    assert len(bflow.events(run, "backlog_skipped")) == 1


def test_a_resumed_finish_does_not_mark_done_twice(bflow, ci_gh, monkeypatch):
    monkeypatch.setenv("FAKE_GH_FAIL_ONCE", "pr create")
    run, _ = _whole(bflow)
    assert _walk(bflow, run) == "waiting_human"
    waiting = run.refresh()["waiting"]
    assert (waiting["kind"], waiting["key"]) == ("blocked", FINISH) and "pull request" in waiting["summary"]
    assert len(_done_events(bflow, run)) == 1
    approve(run, FINISH, by="me")
    assert _walk(bflow, run) == "done", run.state["waiting"]
    [done] = _done_events(bflow, run)
    [opened] = bflow.events(run, "pr_opened")
    assert opened["sha"] == done["sha"] and _pushed_item(bflow, run).status == "done"


def test_a_resolved_merge_may_not_decide_the_backlog_file(bflow, ci_gh, tmp_path):
    """Git's own clean merge of `backlog.yml` is let through only when nothing else needed resolving: a merge that a
    resolver session finished, and the same merge resumed, are refused as the forbidden path."""
    run_git(bflow.repo.root, "push", "-q", "origin", "next")
    run, _ = _whole(bflow, **{"ship/finish/sync-1": [
        {"write": {"README.md": "hello from both\n"}, "result": {"status": "done", "summary": "resolved README.md"}}]})

    def diverge() -> None:
        worktree = bflow.worktree(run)
        (worktree / "README.md").write_text("hello from the branch\n", encoding="utf-8")
        run_git(worktree, "commit", "-q", "-am", "Own work")
        run.emit("step_committed", key="build/own", sha=gitops.head(worktree), summary="own work")
        land_on_origin(bflow.repo, tmp_path, {"backlog.yml": NOTED, "README.md": "hello from the target\n"})

    assert _walk(bflow, run, at_plan=diverge) == "waiting_human"
    waiting = run.refresh()["waiting"]
    assert waiting["kind"] == "guard_violation" and "forbidden_path" in waiting["summary"]
    assert "backlog.yml" in waiting["summary"]
    assert gitops.merge_in_progress(bflow.worktree(run)) and _done_events(bflow, run) == []
    approve(run, FINISH, by="me")
    assert advance(run, bflow.cfg, RunOptions()) == "waiting_human"  # the half-done merge, resumed: still refused
    waiting = run.refresh()["waiting"]
    assert waiting["kind"] == "guard_violation" and "backlog.yml" in waiting["summary"]
    assert _done_events(bflow, run) == [] and not bflow.events(run, "pr_opened")


def test_mark_done_skips_an_item_the_sync_removed(bflow):
    run, worktree = _started(bflow)
    gone = BACKLOG_YML.replace("  - id: b\n    title: Bee\n    status: ready\n", "")
    (worktree / "backlog.yml").write_text(gone, encoding="utf-8")
    run_git(worktree, "commit", "-q", "-am", "A teammate removed b")  # as the base sync brings it in
    run.emit("step_committed", key="ship/finish", sha=gitops.head(worktree), summary="merged")
    head = gitops.head(worktree)
    assert backlog_writes.mark_done(bflow.cfg, run, worktree) is None
    assert backlog_writes.mark_done(bflow.cfg, run, worktree) is None  # a resumed `finish` says it once
    [skipped] = bflow.events(run, "backlog_skipped")
    assert (skipped["item"], skipped["change"], skipped["reason"]) == ("b", "done", "b is not in backlog.yml at HEAD")
    assert gitops.head(worktree) == head and bflow.events(run, "backlog_updated") == []


def test_mark_done_leaves_a_run_outside_the_items_lane_alone(bflow):
    run = start(bflow.cfg, bflow.repo.root, "bug", {"module": "nope", "task": "fix it"}, RunOptions())
    worktree = bflow.worktree(run)
    assert backlog_writes.mark_done(bflow.cfg, run, worktree) is None
    assert bflow.events(run, "backlog_skipped") == [] and gitops.head(worktree) == run.state["base_sha"]
