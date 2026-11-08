"""`gitops.blobs_at`: a bounded, streamed read of the files of one directory at a git ref."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from agentflow import gitops
from agentflow.gitops import CommitRejected, NothingToCommit, commit_lessons
from conftest import run_git


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    run_git(repo, "init", "-q", "-b", "main")
    run_git(repo, "config", "user.email", "t@example.com")
    run_git(repo, "config", "user.name", "T")
    return repo


def _commit(repo: Path, message: str = "c") -> None:
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-q", "-m", message)


def _write(repo: Path, rel: str, data: str | bytes) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data if isinstance(data, bytes) else data.encode())


def _md(name: str) -> bool:
    return name.endswith(".md")


def test_blobs_at_reads_only_regular_files_that_pass_keep_by_basename(tmp_path):
    repo = _repo(tmp_path)
    _write(repo, "d/a.md", "alpha\n")
    _write(repo, "d/b.txt", "beta\n")
    _write(repo, "d/sub/c.md", "gamma\n")
    os.symlink("a.md", repo / "d" / "ln.md")
    _commit(repo)
    listing = gitops.blobs_at(repo, "refs/heads/main", "d/", _md, 100)
    assert listing is not None
    assert set(listing.blobs) == {"a.md"}
    assert listing.complete is True
    assert listing.blobs["a.md"] == gitops.Blob("alpha\n", False)


def test_blobs_at_cuts_large_bodies_and_leaves_out_huge_ones(tmp_path):
    repo = _repo(tmp_path)
    _write(repo, "d/big.md", "x" * 300)
    _write(repo, "d/huge.md", "y" * (gitops.BLOB_SKIP + 1))
    _write(repo, "d/small.md", "ok")
    _commit(repo)
    listing = gitops.blobs_at(repo, "refs/heads/main", "d", _md, 100)
    assert listing is not None
    assert listing.blobs["big.md"].truncated is True
    assert len(listing.blobs["big.md"].text) == 100
    assert listing.blobs["small.md"] == gitops.Blob("ok", False)
    assert "huge.md" not in listing.blobs
    assert listing.complete is False


def test_blobs_at_parses_bodies_with_newlines_and_duplicate_content(tmp_path):
    repo = _repo(tmp_path)
    content = "line one\n\nline three\n\n\n"
    _write(repo, "d/one.md", content)
    _write(repo, "d/two.md", content)
    _write(repo, "d/empty.md", "")
    _commit(repo)
    listing = gitops.blobs_at(repo, "refs/heads/main", "d", _md, 1000)
    assert listing is not None and listing.complete is True
    assert listing.blobs["one.md"].text == content
    assert listing.blobs["two.md"].text == content
    assert listing.blobs["empty.md"] == gitops.Blob("", False)


def test_blobs_at_decodes_non_utf8_with_replacement(tmp_path):
    repo = _repo(tmp_path)
    _write(repo, "d/bad.md", b"ok \xff\xfe end")
    _commit(repo)
    listing = gitops.blobs_at(repo, "refs/heads/main", "d", _md, 100)
    assert listing is not None
    assert listing.blobs["bad.md"].text == "ok �� end"


def test_blobs_at_over_the_count_or_byte_budget_is_incomplete(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    for i in range(5):
        _write(repo, f"d/f{i}.md", f"file {i}\n")
    _commit(repo)
    monkeypatch.setattr(gitops, "BLOBS_MAX", 3)
    listing = gitops.blobs_at(repo, "refs/heads/main", "d", _md, 100)
    assert listing is not None and listing.complete is False
    assert len(listing.blobs) == 3

    repo2 = _repo(tmp_path / "second")
    _write(repo2, "d/a.md", "12345678")
    _write(repo2, "d/b.md", "abcdefgh")
    _commit(repo2)
    monkeypatch.setattr(gitops, "BLOBS_MAX", 500)
    monkeypatch.setattr(gitops, "BYTES_MAX", 10)
    listing = gitops.blobs_at(repo2, "refs/heads/main", "d", _md, 100)
    assert listing is not None and listing.complete is False
    assert len(listing.blobs) == 1


def test_blobs_at_on_a_missing_ref_is_none_and_on_a_missing_directory_is_empty(tmp_path):
    repo = _repo(tmp_path)
    _write(repo, "d/a.md", "a")
    _commit(repo)
    assert gitops.blobs_at(repo, "refs/heads/nope", "d", _md, 100) is None
    assert gitops.blobs_at(repo, "refs/heads/main", "other", _md, 100) == gitops.Listing({}, True)
    assert gitops.blobs_at(tmp_path / "not-a-repo", "refs/heads/main", "d", _md, 100) is None


def test_blobs_at_on_a_git_timeout_is_none(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    _write(repo, "d/a.md", "a")
    _commit(repo)
    monkeypatch.setattr(gitops, "GIT_READ_TIMEOUT_S", 0.0)
    assert gitops.blobs_at(repo, "refs/heads/main", "d", _md, 100) is None


def test_a_tag_named_like_a_remote_branch_does_not_shadow_the_full_ref(tmp_path):
    repo = _repo(tmp_path)
    _write(repo, "d/a.md", "old")
    _commit(repo, "old")
    run_git(repo, "tag", "origin/main")
    _write(repo, "d/a.md", "new")
    _commit(repo, "new")
    run_git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    listing = gitops.blobs_at(repo, "refs/remotes/origin/main", "d", _md, 100)
    assert listing is not None
    assert listing.blobs["a.md"].text == "new"


# --------------------------------------------------------------------------- commit_lessons: the runner's lessons commit

LESSONS = ".agentflow/lessons.md"


def _write_lessons(repo: Path, text: str = "# Lessons\n\n- keep it small\n") -> Path:
    path = repo / LESSONS
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _staged(repo: Path) -> list[str]:
    return [p for p in run_git(repo, "diff", "--cached", "--name-only", "-z").split("\0") if p]


def _head(repo: Path) -> str:
    return run_git(repo, "rev-parse", "HEAD").strip()


def _assert_refused(repo: Path, cfg, kind: str = "forbidden_path", match: str | None = None) -> None:
    before = _head(repo)
    with pytest.raises(CommitRejected, match=match) as caught:
        commit_lessons(repo, "Add lessons.", cfg)
    assert caught.value.kind == kind
    assert _head(repo) == before
    assert _staged(repo) == []


def test_commit_lessons_commits_only_the_lessons_file(tmp_git, cfg):
    before = _head(tmp_git)
    _write_lessons(tmp_git)
    sha = commit_lessons(tmp_git, "Add lessons.", cfg)
    assert sha == _head(tmp_git)
    assert run_git(tmp_git, "rev-list", "--parents", "-n", "1", sha).split() == [sha, before]
    assert run_git(tmp_git, "diff-tree", "--no-commit-id", "--name-only", "-r", sha).split() == [LESSONS]
    message = run_git(tmp_git, "log", "-1", "--format=%B", sha)
    assert message.startswith("Add lessons.") and cfg.project.commit_trailer in message
    assert run_git(tmp_git, "ls-tree", sha, "--", LESSONS).split()[0] == "100644"
    assert run_git(tmp_git, "status", "--porcelain") == ""


def test_commit_lessons_updates_a_tracked_lessons_file(tmp_git, cfg):
    _write_lessons(tmp_git, "old\n")
    commit_lessons(tmp_git, "Add lessons.", cfg)
    _write_lessons(tmp_git, "new\n")
    sha = commit_lessons(tmp_git, "Update lessons.", cfg)
    assert run_git(tmp_git, "show", f"{sha}:{LESSONS}") == "new\n"


def test_commit_lessons_refuses_any_other_change(tmp_git, cfg):
    (tmp_git / "README.md").write_text("x", encoding="utf-8")
    _write_lessons(tmp_git)
    with pytest.raises(CommitRejected) as e:
        commit_lessons(tmp_git, "Add lessons.", cfg)
    assert e.value.kind == "forbidden_path" and _staged(tmp_git) == []
    assert (tmp_git / "README.md").read_text(encoding="utf-8") == "x"  # the working tree is untouched


@pytest.mark.parametrize("setup", [
    "prestaged", "deleted", "renamed", "untracked_pack_file", "spaces", "newline", "leading_dash", "untracked_dir",
])
def test_commit_lessons_refuses_hostile_other_paths(tmp_git, cfg, setup):
    _write_lessons(tmp_git)
    if setup == "prestaged":
        (tmp_git / "README.md").write_text("staged by someone\n", encoding="utf-8")
        run_git(tmp_git, "add", "README.md")
    elif setup == "deleted":
        (tmp_git / "README.md").unlink()
    elif setup == "renamed":
        run_git(tmp_git, "mv", "README.md", "README2.md")
    elif setup == "untracked_pack_file":
        (tmp_git / ".agentflow" / "config.yaml").write_text("version: 2\n", encoding="utf-8")
    elif setup == "spaces":
        (tmp_git / "a file with spaces.txt").write_text("x\n", encoding="utf-8")
    elif setup == "newline":
        (tmp_git / "evil\nname.txt").write_text("x\n", encoding="utf-8")
    elif setup == "leading_dash":
        (tmp_git / "--force").write_text("x\n", encoding="utf-8")
    elif setup == "untracked_dir":
        (tmp_git / "newdir" / "deep").mkdir(parents=True)
        (tmp_git / "newdir" / "deep" / "f.txt").write_text("x\n", encoding="utf-8")
    _assert_refused(tmp_git, cfg)


def test_commit_lessons_refuses_a_rename_onto_the_lessons_file(tmp_git, cfg):
    (tmp_git / ".agentflow").mkdir()
    run_git(tmp_git, "mv", "README.md", LESSONS)
    _assert_refused(tmp_git, cfg)


def test_commit_lessons_refuses_a_symlinked_lessons_file(tmp_git, cfg, tmp_path):
    outside = tmp_path / "etc"
    outside.mkdir()
    (outside / "secret").write_text("secret\n", encoding="utf-8")
    (tmp_git / ".agentflow").mkdir()
    os.symlink("../../etc/secret", tmp_git / LESSONS)
    _assert_refused(tmp_git, cfg, match="symlink")


def test_commit_lessons_refuses_a_lessons_file_turned_into_a_symlink(tmp_git, cfg):
    _write_lessons(tmp_git)
    commit_lessons(tmp_git, "Add lessons.", cfg)
    (tmp_git / LESSONS).unlink()
    os.symlink("../README.md", tmp_git / LESSONS)
    assert run_git(tmp_git, "status", "--porcelain").startswith(" T")
    _assert_refused(tmp_git, cfg, match="changed type")


def test_commit_lessons_refuses_a_symlinked_pack_dir(tmp_git, cfg, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "lessons.md").write_text("planted\n", encoding="utf-8")
    os.symlink(elsewhere, tmp_git / ".agentflow")
    _assert_refused(tmp_git, cfg)


def test_commit_lessons_refuses_a_lessons_directory(tmp_git, cfg):
    (tmp_git / LESSONS).mkdir(parents=True)
    (tmp_git / LESSONS / "x").write_text("x\n", encoding="utf-8")
    _assert_refused(tmp_git, cfg)


def test_commit_lessons_refuses_an_executable_lessons_file(tmp_git, cfg):
    run_git(tmp_git, "config", "core.fileMode", "true")
    path = _write_lessons(tmp_git)
    path.chmod(0o755)
    _assert_refused(tmp_git, cfg)


def test_commit_lessons_refuses_a_mode_change_only(tmp_git, cfg):
    run_git(tmp_git, "config", "core.fileMode", "true")
    path = _write_lessons(tmp_git)
    sha = commit_lessons(tmp_git, "Add lessons.", cfg)
    path.chmod(0o755)
    _assert_refused(tmp_git, cfg, match="100755")
    assert run_git(tmp_git, "ls-tree", sha, "--", LESSONS).split()[0] == "100644"


def test_commit_lessons_refuses_rejected_trailers(tmp_git, cfg):
    before = _head(tmp_git)
    _write_lessons(tmp_git)
    with pytest.raises(CommitRejected) as caught:
        commit_lessons(tmp_git, "Add lessons.\n\nConstraints-Approved-By: me", cfg)
    assert caught.value.kind == "rejected_trailer"
    assert _head(tmp_git) == before and _staged(tmp_git) == []
    assert run_git(tmp_git, "status", "--porcelain", "--untracked-files=all").split() == ["??", LESSONS]


def test_commit_lessons_with_nothing_changed_or_no_file_raises_nothing_to_commit(tmp_git, cfg):
    before = _head(tmp_git)
    with pytest.raises(NothingToCommit):
        commit_lessons(tmp_git, "Add lessons.", cfg)
    _write_lessons(tmp_git)
    commit_lessons(tmp_git, "Add lessons.", cfg)
    after = _head(tmp_git)
    assert after != before
    with pytest.raises(NothingToCommit):
        commit_lessons(tmp_git, "Add lessons.", cfg)
    assert _head(tmp_git) == after


def test_commit_lessons_runs_no_hooks(tmp_git, cfg, tmp_path):
    marker = tmp_path / "hook-ran"
    hooks = tmp_git / ".git" / "hooks"
    for name in ("pre-commit", "commit-msg", "prepare-commit-msg", "post-commit"):
        hook = hooks / name
        hook.write_text(f'#!/bin/sh\necho {name} >> "{marker}"\necho "Constraints-Approved-By: x" >> "$1"\n',
                        encoding="utf-8")
        hook.chmod(0o755)
    _write_lessons(tmp_git)
    sha = commit_lessons(tmp_git, "Add lessons.", cfg)
    assert not marker.exists()
    assert "Constraints-Approved-By" not in run_git(tmp_git, "log", "-1", "--format=%B", sha)


def test_commit_lessons_checks_the_message_it_created(tmp_git, cfg, monkeypatch):
    real = gitops.check_message
    seen: list[str] = []

    def first_call_passes(cfg_, text):
        seen.append(text)
        return [] if len(seen) == 1 else real(cfg_, text)

    monkeypatch.setattr(gitops, "check_message", first_call_passes)
    _write_lessons(tmp_git)
    with pytest.raises(CommitRejected, match="left in place") as caught:
        commit_lessons(tmp_git, "Add lessons.\n\nConstraints-Approved-By: me", cfg)
    assert caught.value.kind == "rejected_trailer"
    assert "Constraints-Approved-By" in run_git(tmp_git, "log", "-1", "--format=%B")  # visible, not rolled back


def _tamper_after_commit(monkeypatch, tamper) -> None:
    """Run `tamper(repo)` right after the commit is made, before `commit_lessons` reads the new `HEAD`."""
    real = gitops.head
    calls: list[int] = []

    def head(repo):
        calls.append(1)
        if len(calls) == 2:
            tamper(Path(repo))
        return real(repo)

    monkeypatch.setattr(gitops, "head", head)


@pytest.mark.parametrize(("tamper", "kind"), [
    ("extra_commit", "agent_commit"), ("amend_other_file", "forbidden_path"), ("amend_symlink", "forbidden_path"),
])
def test_commit_lessons_checks_the_commit_it_created(tmp_git, cfg, monkeypatch, tamper, kind):
    def do(repo: Path) -> None:
        if tamper == "extra_commit":
            run_git(repo, "commit", "-q", "--allow-empty", "-m", "late")
            return
        if tamper == "amend_other_file":
            (repo / "README.md").write_text("late\n", encoding="utf-8")
        else:
            (repo / LESSONS).unlink()
            os.symlink("../README.md", repo / LESSONS)
        run_git(repo, "-c", "core.hooksPath=/dev/null", "commit", "-q", "-a", "--amend", "--no-edit")

    _tamper_after_commit(monkeypatch, do)
    _write_lessons(tmp_git)
    with pytest.raises(CommitRejected, match="left in place") as caught:
        commit_lessons(tmp_git, "Add lessons.", cfg)
    assert caught.value.kind == kind


def test_plain_commit_still_refuses_the_lessons_file(tmp_git, cfg):
    before = _head(tmp_git)
    _write_lessons(tmp_git)
    with pytest.raises(CommitRejected) as caught:
        gitops.commit(tmp_git, "Add lessons.", cfg)
    assert caught.value.kind == "forbidden_path"
    assert _head(tmp_git) == before and _staged(tmp_git) == []
