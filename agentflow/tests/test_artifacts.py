"""`artifacts`: a run's files listed, read and diffed from its own commits in git, capped and allow-listed."""

from __future__ import annotations

import pytest
from conftest import run_git

from agentflow.artifacts import (
    NotFound,
    artifact_diff,
    artifact_max_bytes,
    artifact_tabs,
    read_artifact,
    run_artifacts,
    run_findings,
)


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


# -- fix round 1


def test_a_filename_holding_the_record_mark_cannot_rewrite_another_commits_files(spec_run):
    draft = spec_run.revisions("docs/specs/x.md")[0]["sha"]
    before = {a["path"]: [r["sha"] for r in a["revisions"]] for a in run_artifacts(spec_run.run, spec_run.repo)}
    odd = f"a\x01{draft}"
    newest = spec_run.commit("define/spec_reconcile", write={odd: "x\n", "b.md": "b\n", "c.md": "c\n",
                                                              f"\x01{draft}": "y\n"})
    after = {a["path"]: [r["sha"] for r in a["revisions"]] for a in run_artifacts(spec_run.run, spec_run.repo)}
    for path, shas in before.items():
        assert after[path] == shas  # every earlier revision still lists its own files
    for path in (odd, f"\x01{draft}", "b.md", "c.md"):
        assert after[path] == [newest]


def test_a_listing_git_could_not_finish_is_not_cached_and_not_retried_per_commit(spec_run, monkeypatch):
    from agentflow import artifacts

    real, calls = artifacts._git, []

    def stalled(repo, *args, limit=None):
        if "log" in args:
            calls.append(args)
            raise artifacts._GitTimeout("git timed out")
        return real(repo, *args, limit=limit)

    spec_run.run.emit("note", text="a fresh event log, so no cached listing")
    monkeypatch.setattr(artifacts, "_git", stalled)
    assert run_artifacts(spec_run.run, spec_run.repo) == []
    assert len(calls) == 1  # one stalled batch call, no per-sha fallback behind it
    monkeypatch.setattr(artifacts, "_git", real)
    assert {a["path"] for a in run_artifacts(spec_run.run, spec_run.repo)} == {
        "docs/specs/x.md", "notes.md", "docs/plans/x.md"}


def _fake_git(tmp_path, monkeypatch, body: str):
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    script = bin_dir / "git"
    script.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")


def test_a_git_that_floods_stderr_does_not_stall_a_read(tmp_path, monkeypatch):
    import time

    from agentflow import artifacts

    _fake_git(tmp_path, monkeypatch, "head -c 2000000 /dev/zero >&2; printf ok")
    monkeypatch.setattr(artifacts, "GIT_READ_TIMEOUT_S", 5)
    started = time.monotonic()
    assert artifacts._git(tmp_path, "cat-file", "blob", "f" * 40, limit=100) == (b"ok", False)
    assert artifacts._git(tmp_path, "ls-tree") == (b"ok", False)
    assert time.monotonic() - started < 4


def test_a_git_that_hangs_times_out_and_is_reaped(tmp_path, monkeypatch):
    from agentflow import artifacts

    _fake_git(tmp_path, monkeypatch, "printf partial; exec sleep 30")
    monkeypatch.setattr(artifacts, "GIT_READ_TIMEOUT_S", 0.3)
    seen = []
    real_popen = artifacts.subprocess.Popen

    def popen(*args, **kwargs):
        seen.append(real_popen(*args, **kwargs))
        return seen[-1]

    monkeypatch.setattr(artifacts.subprocess, "Popen", popen)
    for limit in (None, 1 << 20):
        with pytest.raises(artifacts._GitTimeout):
            artifacts._git(tmp_path, "cat-file", "blob", "f" * 40, limit=limit)
    assert all(p.returncode is not None and p.stdout.closed for p in seen)


def test_a_cut_read_reaps_git(tmp_path, monkeypatch):
    from agentflow import artifacts

    _fake_git(tmp_path, monkeypatch, "head -c 5000000 /dev/zero")
    seen = []
    real_popen = artifacts.subprocess.Popen
    monkeypatch.setattr(artifacts.subprocess, "Popen",
                        lambda *a, **k: seen.append(real_popen(*a, **k)) or seen[-1])
    out, cut = artifacts._git(tmp_path, "cat-file", "blob", "f" * 40, limit=1000)
    assert cut is True and len(out) == 1000
    assert seen[0].returncode is not None and seen[0].stdout.closed


def test_the_cache_survives_an_entry_evicted_by_another_thread(spec_run, monkeypatch):
    from agentflow import artifacts

    class Racy(dict):  # another thread empties it between any check and any read
        def __contains__(self, key):
            return True

        def __getitem__(self, key):
            raise KeyError(key)

        def __len__(self):
            return 10 ** 6

        def __iter__(self):
            return iter(())

    monkeypatch.setattr(artifacts, "_CACHE", Racy())
    assert {a["path"] for a in run_artifacts(spec_run.run, spec_run.repo)} == {
        "docs/specs/x.md", "notes.md", "docs/plans/x.md"}


def test_an_event_log_that_cannot_be_read_lists_nothing_and_reads_not_found(spec_run, monkeypatch):
    newest = spec_run.revisions("docs/specs/x.md")[-1]["sha"]
    spec_run.run.emit("note", text="a fresh event log, so no cached listing")

    def gone(*args, **kwargs):
        raise PermissionError("events.jsonl")

    monkeypatch.setattr(spec_run.run.events, "read", gone)
    assert run_artifacts(spec_run.run, spec_run.repo) == []
    with pytest.raises(NotFound):
        read_artifact(spec_run.run, spec_run.repo, "docs/specs/x.md", newest, 512 * 1024)
    with pytest.raises(NotFound):
        artifact_diff(spec_run.run, spec_run.repo, "docs/specs/x.md", newest, "head", 512 * 1024)
    monkeypatch.undo()
    assert run_artifacts(spec_run.run, spec_run.repo) != []  # the failed read was not cached


def test_findings_come_by_step_and_round_with_the_newest_open(flow_repo):
    run = flow_repo.new_run()
    run.emit("step_findings", key="define/spec_review", tree="", findings=[
        {"severity": "HIGH", "file": "docs/specs/x.md", "line": "34-40", "text": "Scope 3 contradicts the code"}])
    run.emit("step_findings", key="define/spec_review", tree="", findings=[
        {"severity": "medium", "file": "docs/specs/x.md", "line": 65, "title": "Locks", "detail": "after row locks"},
        {"severity": "low", "file": "docs/specs/x.md", "line": "L0"}, {"text": "no file"}])
    rows = run_findings(run)
    assert [(r["round"], r["open"]) for r in rows] == [(1, False), (2, True)]
    assert rows[0]["findings"][0] == {"severity": "high", "file": "docs/specs/x.md", "line": 34,
                                      "text": "Scope 3 contradicts the code"}
    assert [f["line"] for f in rows[1]["findings"]] == [65, None, None]
    assert rows[1]["findings"][0]["text"] == "Locks after row locks"
    assert rows[1]["label"] and rows[1]["key"] == "define/spec_review" and rows[1]["ts"]


def test_findings_line_and_severity_are_cleaned(flow_repo):
    run = flow_repo.new_run()
    run.emit("step_findings", key="define/spec_review", tree="", findings=[
        {"line": "L34", "severity": 3}, {"line": 0}, {"line": -2}, {"line": True}, {"line": "x"}, {"line": 7.5},
        "a bare string"])
    (row,) = run_findings(run)
    assert [f["line"] for f in row["findings"]] == [34, None, None, None, None, None, None]
    assert [f["severity"] for f in row["findings"]] == [None] * 7
    assert row["findings"][-1]["text"] == "a bare string"


def test_a_negative_or_overlong_line_is_none_and_never_raises(flow_repo):
    run = flow_repo.new_run()
    run.emit("step_findings", key="define/spec_review", tree="", findings=[
        {"line": "-5"}, {"line": "L-5"}, {"line": "9" * 5000}, {"line": "L" + "1" * 10}, {"line": 10 ** 12},
        {"line": "34"}, {"line": "34-40"}, {"line": "L34"}, {"line": "see 12"}, {"line": 999_999_999}])
    (row,) = run_findings(run)
    assert [f["line"] for f in row["findings"]] == [None, None, None, None, None, 34, 34, 34, 12, 999_999_999]


def test_findings_keep_review_results_apart_by_key(flow_repo):
    run = flow_repo.new_run()
    run.emit("step_findings", key="define/spec_review", tree="", findings=[{"text": "a"}])
    run.emit("review_result", key="build/task_1/review", round=2, findings=[{"text": "b"}], blocking=0)
    run.emit("review_result", key="build/task_1/review", round=3, findings=[], blocking=0)
    rows = run_findings(run)
    assert [(r["key"], r["round"], r["open"]) for r in rows] == [
        ("define/spec_review", 1, True), ("build/task_1/review", 2, False), ("build/task_1/review", 3, True)]
