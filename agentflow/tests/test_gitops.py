"""`gitops.blobs_at`: a bounded, streamed read of the files of one directory at a git ref."""
from __future__ import annotations

import os
from pathlib import Path

from agentflow import gitops
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
