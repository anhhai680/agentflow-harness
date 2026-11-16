"""`artifacts`: a run's files listed, read and diffed from its own commits in git, capped and allow-listed."""

from __future__ import annotations

import pytest
from conftest import run_git

from agentflow.artifacts import NotFound, artifact_diff, artifact_max_bytes, artifact_tabs, read_artifact, run_artifacts


def test_artifacts_list_each_revision_by_phase(spec_run):
    run, repo = spec_run.run, spec_run.repo
    found = {a["path"]: a for a in run_artifacts(run, repo)}
    spec = found["docs/specs/x.md"]
    assert (spec["phase"], spec["kind"]) == ("define", "spec")
    assert [r["label"] for r in spec["revisions"]] == ["draft", "spec_reconcile r1", "spec_reconcile r2"]
    assert [r["round"] for r in spec["revisions"]] == [0, 1, 2]
    assert [r["step"] for r in spec["revisions"]] == ["define/draft", "define/spec_reconcile", "define/spec_reconcile"]
    assert all(r["ts"] for r in spec["revisions"])
    assert found["docs/plans/x.md"]["kind"] == "plan" and found["notes.md"]["kind"] == "spec"
    assert list(found) == ["docs/specs/x.md", "notes.md", "docs/plans/x.md"]


def test_a_diff_between_two_revisions_shows_the_change(spec_run):
    r1, r2 = (r["sha"] for r in spec_run.revisions("docs/specs/x.md")[:2])
    diff = artifact_diff(spec_run.run, spec_run.repo, "docs/specs/x.md", r1, r2, 512 * 1024)
    assert "-Scope 3 says one thing" in diff["diff"] and "+Scope 3 says another" in diff["diff"]
    assert diff["truncated"] is False
    assert (diff["path"], diff["from"], diff["to"]) == ("docs/specs/x.md", r1, r2)


def test_an_artifact_still_reads_after_its_worktree_is_gone(spec_run):
    run_git(spec_run.repo, "worktree", "remove", "--force", str(spec_run.worktree))
    newest = spec_run.revisions("docs/specs/x.md")[-1]["sha"]
    got = read_artifact(spec_run.run, spec_run.repo, "docs/specs/x.md", newest, 512 * 1024)
    assert got["sha"] == newest and "Scope 3 says another" in got["text"] and got["truncated"] is False
    assert got["lines"] == 4 and got["path"] == "docs/specs/x.md"


def test_head_reads_the_branch_tip(spec_run):
    got = read_artifact(spec_run.run, spec_run.repo, "docs/plans/x.md", "head", 512 * 1024)
    assert got["sha"] == run_git(spec_run.repo, "rev-parse", "flow/feature/crm").strip()


@pytest.mark.parametrize("path", ["../../state.json", "/etc/passwd", "docs/specs/../plans/x.md", "README.md",
                                  "-p", "docs/specs/x.md\0"])
def test_a_path_outside_the_list_is_not_found(spec_run, path):
    with pytest.raises(NotFound):
        read_artifact(spec_run.run, spec_run.repo, path, "head", 512 * 1024)


@pytest.mark.parametrize("sha", ["0" * 40, "HEAD", "main", "--output=/tmp/x", "abc", "<plan sha>"])
def test_a_sha_that_is_not_a_revision_of_the_path_is_not_found(spec_run, sha):
    sha = spec_run.revisions("docs/plans/x.md")[0]["sha"] if sha == "<plan sha>" else sha
    with pytest.raises(NotFound):
        read_artifact(spec_run.run, spec_run.repo, "docs/specs/x.md", sha, 512 * 1024)
    with pytest.raises(NotFound):
        artifact_diff(spec_run.run, spec_run.repo, "docs/specs/x.md", sha, "head", 512 * 1024)


def test_a_big_revision_is_cut_at_a_line(spec_run):
    big = spec_run.commit("define/spec_reconcile", write={"docs/specs/x.md": ("x" * 39 + "\n") * 3000})
    got = read_artifact(spec_run.run, spec_run.repo, "docs/specs/x.md", big, 16 * 1024)
    assert got["truncated"] is True and len(got["text"].encode()) <= 16 * 1024 and got["text"].endswith("\n")
    assert got["lines"] == got["text"].count("\n")
    first = spec_run.revisions("docs/specs/x.md")[0]["sha"]
    diff = artifact_diff(spec_run.run, spec_run.repo, "docs/specs/x.md", first, big, 16 * 1024)
    assert diff["truncated"] is True and len(diff["diff"].encode()) <= 16 * 1024 and diff["diff"].endswith("\n")


def test_a_symlink_or_a_deleted_path_is_not_read(spec_run):
    linked = spec_run.commit("define/spec_reconcile", link={"notes.md": "/etc/passwd"})
    gone = spec_run.commit("define/spec_reconcile", delete=["notes.md"])
    with pytest.raises(NotFound, match="not a regular file"):
        read_artifact(spec_run.run, spec_run.repo, "notes.md", linked, 512 * 1024)
    with pytest.raises(NotFound, match="does not exist"):
        read_artifact(spec_run.run, spec_run.repo, "notes.md", gone, 512 * 1024)


def test_a_run_with_no_commit_has_no_artifacts(flow_repo):
    run = flow_repo.new_run()
    run.emit("phase_started", phase="define", start_sha=flow_repo.head())
    assert run_artifacts(run, flow_repo.root) == []


def test_a_revision_git_lost_is_not_found_not_an_error(spec_run):
    newest = spec_run.revisions("docs/specs/x.md")[-1]["sha"]  # listed (and cached) while git still has it
    run_git(spec_run.repo, "worktree", "remove", "--force", str(spec_run.worktree))
    run_git(spec_run.repo, "branch", "-D", "flow/feature/crm")
    run_git(spec_run.repo, "reflog", "expire", "--expire=now", "--all")
    run_git(spec_run.repo, "gc", "-q", "--prune=now")
    with pytest.raises(NotFound, match="no longer in git"):
        read_artifact(spec_run.run, spec_run.repo, "docs/specs/x.md", newest, 512 * 1024)
    first = spec_run.revisions("docs/specs/x.md")[0]["sha"]
    with pytest.raises(NotFound, match="no longer in git"):
        artifact_diff(spec_run.run, spec_run.repo, "docs/specs/x.md", first, newest, 512 * 1024)


def test_a_commit_git_cannot_read_is_dropped_from_the_list(spec_run):
    spec_run.run.emit("step_committed", key="define/spec_reconcile", sha="ab" * 20, summary="lost")
    spec = next(a for a in run_artifacts(spec_run.run, spec_run.repo) if a["path"] == "docs/specs/x.md")
    assert len(spec["revisions"]) == 3 and "ab" * 20 not in {r["sha"] for r in spec["revisions"]}


def test_a_commit_sha_that_is_not_hex_is_never_passed_to_git(spec_run):
    spec_run.run.emit("step_committed", key="define/spec_reconcile", sha="--output=/tmp/x", summary="bad")
    spec = next(a for a in run_artifacts(spec_run.run, spec_run.repo) if a["path"] == "docs/specs/x.md")
    assert len(spec["revisions"]) == 3


def test_the_cap_comes_from_the_config_and_survives_a_bad_value(flow_repo):
    for value, expect in ((64, 64 * 1024), (None, 512 * 1024), (0, 512 * 1024), (True, 512 * 1024)):
        flow_repo.cfg.dashboard.pop("artifact_max_kb", None)
        if value is not None:
            flow_repo.cfg.dashboard["artifact_max_kb"] = value
        assert artifact_max_bytes(flow_repo.cfg) == expect


def test_artifact_tabs_follow_the_lane_phases():
    assert artifact_tabs(["define", "plan", "build"]) == ["spec", "plan"]
    assert artifact_tabs(["define", "build"]) == ["spec"] and artifact_tabs(["build"]) == []
