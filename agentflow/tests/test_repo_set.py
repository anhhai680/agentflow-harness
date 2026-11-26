"""`repo_set`: the repositories a workspace's settings hold, found by directory names and `.git` alone."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

from agentflow import layers, pack, repo_set
from agentflow.layers import WorkspaceSettings
from agentflow.repo_set import NoWorkspace, RepoRef, Skipped, resolve
from agentflow.tests.conftest import run_git


def make_repo(parent: Path, name: str, pack: bool = True) -> Path:
    path = parent / name
    path.mkdir(parents=True)
    run_git(path, "init", "-q", "-b", "main")
    if pack:
        (path / ".agentflow").mkdir()
        (path / ".agentflow" / "config.yaml").write_text("version: 1\n", encoding="utf-8")
    return path


def commit(path: Path) -> None:
    run_git(path, "-c", "user.name=T", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false",
            "commit", "-q", "--allow-empty", "-m", "init")


def settings(roots=(), repos=(), exclude=()) -> WorkspaceSettings:
    return WorkspaceSettings(tuple(str(r) for r in roots), tuple(str(r) for r in repos), tuple(str(e) for e in exclude))


def names(result: repo_set.RepoSet) -> list[str]:
    return [r.name for r in result.repos]


def test_a_root_lists_its_child_repositories_one_level_deep_in_name_order(tmp_path):
    root = tmp_path / "root"
    make_repo(root, "b")
    make_repo(root, "a")
    make_repo(root / "c", "nested")
    (root / "docs").mkdir()
    (root / "f.txt").write_text("x\n", encoding="utf-8")
    result = resolve(settings([root]))
    assert names(result) == ["a", "b"]
    for ref in result.repos:
        assert isinstance(ref, RepoRef)
        assert ref.state == "live"
        assert ref.path == (root / ref.name).resolve()
        assert ref.id == pack.repo_id(ref.path)
    assert result.skipped == ()
    assert result.live == result.repos


def test_a_repository_with_no_pack_is_not_set_up(tmp_path):
    root = tmp_path / "root"
    make_repo(root, "x", pack=False)
    make_repo(root, "y")
    result = resolve(settings([root]))
    assert {r.name: r.state for r in result.repos} == {"x": "not_set_up", "y": "live"}
    assert [r.name for r in result.live] == ["y"]


def test_hidden_folders_and_symlinks_are_never_entered(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    make_repo(root, ".hidden")
    outside = make_repo(tmp_path / "elsewhere", "real")
    (root / "link").symlink_to(outside, target_is_directory=True)
    result = resolve(settings([root]))
    assert result.repos == ()
    assert result.skipped == ()


def test_a_linked_worktree_collapses_into_its_main_checkout(tmp_path):
    root = tmp_path / "root"
    main = make_repo(root, "main-in")
    commit(main)
    run_git(main, "worktree", "add", "-q", str(root / "wt-in"), "-b", "wt-in")
    outer = make_repo(tmp_path / "outer", "main-out")
    commit(outer)
    run_git(outer, "worktree", "add", "-q", str(root / "wt-out"), "-b", "wt-out")
    result = resolve(settings([root]))
    assert [r.path for r in result.repos] == [main.resolve(), outer.resolve()]
    assert names(result) == ["main-in", "main-out"]


def test_repos_entries_join_and_are_deduplicated(tmp_path):
    root = tmp_path / "root"
    inside = make_repo(root, "in")
    outside = make_repo(tmp_path / "other", "out")
    result = resolve(settings([root], repos=[outside, inside]))
    assert names(result) == ["in", "out"]


def test_exclude_matches_a_name_or_a_path(tmp_path):
    root = tmp_path / "root"
    make_repo(root, "a")
    path_b = make_repo(root, "b")
    make_repo(root, "c")
    assert names(resolve(settings([root], exclude=["a"]))) == ["b", "c"]
    assert names(resolve(settings([root], exclude=[path_b]))) == ["a", "c"]
    assert names(resolve(settings([root], exclude=["b", "c"]))) == ["a"]


def test_a_missing_root_and_a_non_checkout_repos_entry_are_skipped_with_a_reason(tmp_path):
    root = tmp_path / "root"
    make_repo(root, "a")
    plain = tmp_path / "plain"
    plain.mkdir()
    missing = tmp_path / "missing"
    result = resolve(settings([missing, root], repos=[plain]))
    assert names(result) == ["a"]
    assert [s.entry for s in result.skipped] == [str(missing), str(plain)]
    assert all(isinstance(s, Skipped) and s.reason for s in result.skipped)
    assert result.skipped[0].reason == "is not a folder"
    assert result.skipped[1].reason == "is not a git checkout"


def test_a_repos_entry_may_be_a_symlink_because_the_developer_named_it(tmp_path):
    target = make_repo(tmp_path / "elsewhere", "real")
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    result = resolve(settings(repos=[link]))
    assert [r.path for r in result.repos] == [target.resolve()]


def test_two_repositories_with_one_folder_name_are_told_apart(tmp_path):
    root = tmp_path / "root"
    make_repo(root / "p1", "app")
    make_repo(root / "p2", "app")
    make_repo(root, "solo")
    result = resolve(settings(repos=[root / "p1" / "app", root / "p2" / "app", root / "solo"]))
    assert names(result) == ["app (p1)", "app (p2)", "solo"]


def test_the_scan_stops_at_max_children(tmp_path, monkeypatch):
    monkeypatch.setattr(repo_set, "MAX_CHILDREN", 3)
    root = tmp_path / "root"
    for name in "abcde":
        make_repo(root, name)
    result = resolve(settings([root]))
    assert names(result) == ["a", "b", "c"]
    assert len(result.skipped) == 1
    assert result.skipped[0].entry == str(root)
    assert "more than 3 entries" in result.skipped[0].reason
    assert "ignored" in result.skipped[0].reason


def test_resolve_starts_only_git_rev_parse(tmp_path, monkeypatch):
    root = tmp_path / "root"
    make_repo(root, "a")
    make_repo(root, "b", pack=False)
    make_repo(tmp_path / "other", "c")
    calls: list[list[str]] = []
    real_run = subprocess.run

    def recording_run(argv, *args, **kwargs):
        calls.append(list(argv))
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", recording_run)
    result = resolve(settings([root], repos=[tmp_path / "other" / "c"]))
    assert len(result.repos) == 3
    assert calls
    assert all(argv == ["git", "rev-parse", "--git-common-dir"] for argv in calls)


def test_from_user_config_reads_the_user_file(tmp_path):
    root = tmp_path / "root"
    make_repo(root, "a")
    path = layers.user_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"workspace": {"roots": [str(root)]}}), encoding="utf-8")
    assert names(repo_set.from_user_config()) == ["a"]


def test_no_workspace_names_the_user_file():
    with pytest.raises(NoWorkspace) as info:
        repo_set.from_user_config()
    assert str(info.value) == f"no workspace: set workspace.roots in {layers.user_config_path()}"
