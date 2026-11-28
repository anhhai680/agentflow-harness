"""graph_git: trees, diffs and blobs read through hardened git, with odd names left out and nothing ever raised."""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

from agentflow import graph_git
from agentflow.graph_git import Change, Entry
from conftest import run_git


def _commit(repo: Path, message: str = "c") -> str:
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-q", "--allow-empty", "-m", message)
    return run_git(repo, "rev-parse", "HEAD").strip()


def _blob(repo: Path, data: bytes) -> str:
    return subprocess.run(["git", "hash-object", "-w", "--stdin"], cwd=repo, input=data, capture_output=True,
                          check=True).stdout.decode().strip()


def _commit_tree(repo: Path, entries: list[tuple[bytes, str, str, bytes]]) -> str:
    """A commit whose root tree holds exactly `entries` (mode, type, id, name), names as raw bytes."""
    feed = b"".join(mode + b" " + kind.encode() + b" " + oid.encode() + b"\t" + name + b"\0"
                    for mode, kind, oid, name in entries)
    tree = subprocess.run(["git", "mktree", "-z"], cwd=repo, input=feed, capture_output=True, check=True).stdout
    return run_git(repo, "commit-tree", "-m", "odd", tree.decode().strip()).strip()


@pytest.fixture
def repo(tmp_git: Path) -> Path:
    return tmp_git


def test_base_commit_prefers_origin(repo):
    first = run_git(repo, "rev-parse", "HEAD").strip()
    (repo / "x.py").write_text("x = 2\n")
    second = _commit(repo)
    assert graph_git.base_commit(repo, "main") == second
    run_git(repo, "update-ref", "refs/remotes/origin/main", first)
    found = graph_git.base_commit(repo, "main")
    assert found == first and len(found) == 40
    assert graph_git.base_commit(repo, "nope") is None
    assert graph_git.is_commit(repo, first) and not graph_git.is_commit(repo, "0" * 40)
    tree = run_git(repo, "rev-parse", "HEAD^{tree}").strip()
    assert not graph_git.is_commit(repo, tree) and not graph_git.is_commit(repo, "HEAD")


def test_an_option_shaped_name_runs_nothing(repo, monkeypatch):
    ran: list = []
    real = subprocess.Popen
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: ran.append(a) or real(*a, **k))
    for name in ("--upload-pack=x", "-x", "a..b", "", "a b"):
        assert graph_git.base_commit(repo, name) is None
    assert graph_git.listing(repo, "HEAD") is None
    assert graph_git.changes(repo, "--output=/tmp/x", "a" * 40) is None
    assert list(graph_git.read_blobs(repo, ["--batch-all-objects"], 100)) == [("--batch-all-objects", None)]
    assert ran == []


def test_listing_modes_symlink_and_submodule(repo):
    os.symlink("README.md", repo / "link")
    (repo / "run.sh").write_text("#!/bin/sh\n")
    (repo / "run.sh").chmod(0o755)
    head = run_git(repo, "rev-parse", "HEAD").strip()
    run_git(repo, "add", "-A")
    run_git(repo, "update-index", "--add", "--cacheinfo", f"160000,{head},sub")  # a gitlink, no checkout behind it
    run_git(repo, "commit", "-q", "-m", "sub")
    commit = run_git(repo, "rev-parse", "HEAD").strip()
    entries, notes = graph_git.listing(repo, commit)
    by_path = {e.path: e for e in entries}
    assert by_path["link"].mode == "120000"
    assert by_path["sub"] == Entry("sub", "160000", head)
    assert by_path["run.sh"].mode == "100755" and by_path["src/app.py"].mode == "100644"
    assert notes == []


def test_odd_names_are_left_out_with_one_note_per_kind(repo):
    blob = _blob(repo, b"x = 1\n")
    commit = _commit_tree(repo, [
        (b"100644", "blob", blob, b"good.py"),
        (b"100644", "blob", blob, b"a\nb.py"),
        (b"100644", "blob", blob, b"esc\x1b[31m.py"),
        (b"100644", "blob", blob, b"bad\xff.py"),
        (b"100644", "blob", blob, b"bad\xfe.py"),
        (b"100644", "blob", blob, b"x" * 1001),
        (b"100644", "blob", blob, "café.py".encode()),
    ])
    entries, notes = graph_git.listing(repo, commit)
    assert sorted(e.path for e in entries) == ["café.py", "good.py"]
    assert notes == ["1 path(s) left out: longer than 1000 bytes", "2 path(s) left out: not UTF-8",
                     "2 path(s) left out: holding a control character"]
    empty = _commit_tree(repo, [(b"100644", "blob", blob, b"good.py")])
    found, notes = graph_git.changes(repo, empty, commit)
    assert [c.path for c in found] == ["café.py"] and len(notes) == 3


def test_changes_lists_add_change_delete_and_mode(repo):
    (repo / "keep.sh").write_text("echo\n")
    (repo / "gone.py").write_text("g = 1\n")
    old = _commit(repo)
    (repo / "keep.sh").chmod(0o755)
    (repo / "gone.py").unlink()
    (repo / "src" / "app.py").write_text("x = 2\n")
    (repo / "new.py").write_text("n = 1\n")
    new = _commit(repo)
    found, notes = graph_git.changes(repo, old, new)
    by_path = {c.path: c for c in found}
    assert set(by_path) == {"keep.sh", "gone.py", "src/app.py", "new.py"} and notes == []

    def blob(commit: str, path: str) -> str:
        return run_git(repo, "rev-parse", f"{commit}:{path}").strip()

    assert by_path["new.py"] == Change("new.py", "100644", None, blob(new, "new.py"))
    assert by_path["gone.py"] == Change("gone.py", "100644", blob(old, "gone.py"), None)
    assert by_path["src/app.py"] == Change("src/app.py", "100644", blob(old, "src/app.py"), blob(new, "src/app.py"))
    assert by_path["keep.sh"].mode == "100755" and by_path["keep.sh"].old == by_path["keep.sh"].new
    assert graph_git.changes(repo, old, old) == ([], [])
    assert graph_git.changes(repo, old, "1" * 40) is None


def test_read_blobs_skips_what_is_over_the_limit_or_not_a_blob(repo, monkeypatch):
    small, big = _blob(repo, b"tiny\n"), _blob(repo, b"x" * 5000)
    read: list[list[str]] = []
    real = graph_git._batch
    monkeypatch.setattr(graph_git, "_batch", lambda r, ids, limit: read.append(list(ids)) or real(r, ids, limit))
    got = list(graph_git.read_blobs(repo, [big, small, "not-an-id", small], 1000))
    assert got == [(big, None), (small, b"tiny\n"), ("not-an-id", None)]
    assert read == [[small]]
    tree = run_git(repo, "rev-parse", "HEAD^{tree}").strip()
    assert list(graph_git.read_blobs(repo, [tree], 10**6)) == [(tree, None)]
    assert list(graph_git.read_blobs(repo, [], 10)) == []


def test_a_failed_read_raises_and_is_never_a_none(repo, tmp_path, monkeypatch):
    small = _blob(repo, b"tiny\n")
    with pytest.raises(graph_git.ReadFailed, match="not in the object store"):
        list(graph_git.read_blobs(repo, [small, "e" * 40], 1000))
    with pytest.raises(graph_git.ReadFailed):
        list(graph_git.read_blobs(tmp_path / "not-a-repo", [small], 1000))
    monkeypatch.setattr(graph_git, "_git", lambda *a, **k: None)  # git failed or timed out
    with pytest.raises(graph_git.ReadFailed):
        list(graph_git.read_blobs(repo, [small], 1000))
    sizes_ok = {small: ("blob", 5)}
    monkeypatch.setattr(graph_git, "_sizes", lambda r, ids: sizes_ok)
    with pytest.raises(graph_git.ReadFailed, match="--batch failed"):
        list(graph_git.read_blobs(repo, [small], 1000))


def test_read_blobs_reads_in_runs(repo, monkeypatch):
    monkeypatch.setattr(graph_git, "BATCH_MAX_BYTES", 10)
    ids = [_blob(repo, f"blob {n}\n".encode()) for n in range(4)]
    assert [data for _, data in graph_git.read_blobs(repo, ids, 100)] == [f"blob {n}\n".encode() for n in range(4)]


def test_a_gitlink_change_is_listed_whatever_gitmodules_says(repo):
    first = run_git(repo, "rev-parse", "HEAD").strip()
    (repo / ".gitmodules").write_text('[submodule "sub"]\n\tpath = sub\n\turl = ./x\n\tignore = all\n')
    run_git(repo, "add", "-A")
    run_git(repo, "update-index", "--add", "--cacheinfo", f"160000,{first},sub")
    run_git(repo, "commit", "-q", "-m", "sub")
    old = run_git(repo, "rev-parse", "HEAD").strip()
    run_git(repo, "config", "diff.ignoreSubmodules", "all")
    run_git(repo, "update-index", "--cacheinfo", f"160000,{old},sub")
    run_git(repo, "commit", "-q", "-m", "move sub")
    new = run_git(repo, "rev-parse", "HEAD").strip()
    assert graph_git.changes(repo, old, new) == ([Change("sub", "160000", first, old)], [])


def test_hooks_and_fsmonitor_never_run(repo, tmp_path):
    marker = tmp_path / "ran"
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    script = f"#!/bin/sh\ntouch '{marker}'\n"
    for name in ("pre-commit", "post-checkout", "post-index-change", "reference-transaction", "fsmonitor"):
        (hooks / name).write_text(script)
        (hooks / name).chmod(stat.S_IRWXU)
    run_git(repo, "config", "core.hooksPath", str(hooks))
    run_git(repo, "config", "core.fsmonitor", str(hooks / "fsmonitor"))
    head = run_git(repo, "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "rev-parse", "HEAD").strip()
    entries, _ = graph_git.listing(repo, head)
    assert graph_git.base_commit(repo, "main") == head and graph_git.is_commit(repo, head)
    assert graph_git.changes(repo, head, head) == ([], [])
    assert all(data is not None for _, data in graph_git.read_blobs(repo, [e.blob for e in entries], 10**6))
    assert not marker.exists()


def test_every_git_call_refuses_every_transport(repo, monkeypatch):
    """`protocol.allow=never`: a git older than 2.44 ignores `GIT_NO_LAZY_FETCH`, and still cannot fetch."""
    argvs: list[list[str]] = []
    real = subprocess.Popen
    monkeypatch.setattr(subprocess, "Popen", lambda argv, **k: argvs.append(list(argv)) or real(argv, **k))
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()
    argvs.clear()
    entries, _ = graph_git.listing(repo, head)
    graph_git.changes(repo, head, head)
    list(graph_git.read_blobs(repo, [e.blob for e in entries], 10**6))
    assert graph_git.base_commit(repo, "main") == head
    assert len(argvs) >= 5
    for argv in argvs:
        at = argv.index("-C")
        assert argv[:2] == ["git", "--no-pager"] and ["-c", "protocol.allow=never"] in _pairs(argv[:at])


def _pairs(argv: list[str]) -> list[list[str]]:
    return [argv[n : n + 2] for n in range(len(argv) - 1)]

