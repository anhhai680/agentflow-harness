"""graph_build: the delta build parses only what changed, and a delta always equals a full build at the same commit."""

from __future__ import annotations

import dataclasses
import errno
import fcntl
import json
import os
import sqlite3
import subprocess
from pathlib import Path

import pytest
from conftest import PYTHON_CONFIG, run_git

from agentflow import graph_git, graph_parse, graph_store, pack
from agentflow.config import GraphPolicy, load_config
from agentflow.graph_build import BuildError, build, doc_roots

REPO_A = {
    "pkg/slots.py": b"def lease():\n    return 1\n",
    "pkg/cap.py": b"from pkg.slots import lease\n\n\nclass RunCapHold:\n    def hold(self):\n        return lease()\n",
    "tests/test_cap.py": b"from pkg.cap import RunCapHold\n\n\ndef test_hold():\n    assert RunCapHold().hold()\n",
    "README.md": b"# Toy\n\nSee [the guide](docs/guide.md).\n",
    "docs/guide.md": b"# Guide\n\nCall `lease` first.\n",
    "pyproject.toml": b'[project]\nname = "toy"\nversion = "1.0"\ndependencies = ["pyyaml>=6"]\n',
    "LICENSE": b"MIT\n",
}


def commit(root: Path, files: dict[str, bytes | None], message: str = "c", force: bool = False) -> str:
    """Write (or, for None, delete) `files` and commit all of it; the new commit's id."""
    for path, data in files.items():
        target = root / path
        if data is None:
            target.unlink()
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    run_git(root, "add", "-A", *(["-f", *files] if force else []))
    run_git(root, "commit", "-q", "--allow-empty", "-m", message)
    return run_git(root, "rev-parse", "HEAD").strip()


def make_repo(tmp_path: Path, files: dict[str, bytes], name: str = "repo", force: bool = False) -> Path:
    root = tmp_path / name
    root.mkdir()
    run_git(root, "init", "-q", "-b", "main")
    run_git(root, "config", "user.name", "Test")
    run_git(root, "config", "user.email", "test@example.com")
    run_git(root, "config", "commit.gpgsign", "false")
    commit(root, files, "initial", force=force)
    return root


@pytest.fixture(scope="module")
def base_cfg():
    return load_config(PYTHON_CONFIG)


@pytest.fixture
def cfg(base_cfg):
    return dataclasses.replace(base_cfg, graph=GraphPolicy(enabled=True))


@pytest.fixture
def repo_a(tmp_path):
    return make_repo(tmp_path, REPO_A)


def store_of(repo: Path) -> Path:
    return graph_store.path_for(pack.state_dir(repo))


def read(repo: Path, fn, *args):
    conn = graph_store.open_read(store_of(repo))
    try:
        return fn(conn, *args)
    finally:
        conn.close()


def graph_of(repo: Path) -> dict:
    """The store's dump without what depends on history: the `facts` cache, `meta.built_at` and `meta.languages`
    (ruling R3, amended)."""
    dumped = read(repo, graph_store.dump)
    dumped.pop("facts")
    dumped["meta"] = [row for row in dumped["meta"] if row[0] not in ("built_at", "languages")]
    return dumped


def spy_on_parser(monkeypatch) -> list[tuple[str, str]]:
    seen: list[tuple[str, str]] = []
    real = graph_parse.Parser.parse

    def parse(self, path, lang, data):
        seen.append((path, lang))
        return real(self, path, lang, data)

    monkeypatch.setattr(graph_parse.Parser, "parse", parse)
    return seen


def no_parser(monkeypatch) -> None:
    def refuse(*args, **kwargs):
        raise AssertionError("the parser was started")

    monkeypatch.setattr(graph_parse, "Parser", refuse)


def stdlib_only(monkeypatch) -> None:
    real = graph_parse.worker_argv
    monkeypatch.setattr(graph_parse, "worker_argv", lambda mode: real(mode) if mode == "stdlib" else None)


_GRAMMARS: list[bool] = []


def need_grammars() -> None:
    """Skip without the locked tree-sitter grammars in uv's cache; CI must have them."""
    if not _GRAMMARS:
        with graph_parse.Parser(download=bool(os.environ.get("CI"))) as p:
            _GRAMMARS.append("typescript" in p.languages)
    if not _GRAMMARS[0]:
        if os.environ.get("CI"):
            pytest.fail("the locked grammars did not start in CI")
        pytest.skip("the tree-sitter grammars are not in uv's cache: run `agentflow graph build` once with network access")


def rows(repo: Path) -> dict[str, graph_store.FileRow]:
    return read(repo, graph_store.files)


# --- the delta ---


def test_first_build_then_no_change(repo_a, cfg, monkeypatch):
    first = build(repo_a, cfg, download=False)
    assert first.full and first.previous is None and first.parsed == 6
    assert first.commit == run_git(repo_a, "rev-parse", "HEAD").strip() and first.left_out == {}
    assert first.languages == {"python": "exact", "markdown": "exact", "manifest": "exact"}
    no_parser(monkeypatch)
    again = build(repo_a, cfg, download=False)
    assert (again.full, again.parsed, again.cached, again.resolved, again.removed) == (False, 0, 0, 0, 0)
    assert again.previous == first.commit and again.languages == first.languages


def test_a_one_file_commit_parses_one_blob(repo_a, cfg, monkeypatch):
    build(repo_a, cfg, download=False)
    seen = spy_on_parser(monkeypatch)
    commit(repo_a, {"pkg/slots.py": b"def lease(): ...\ndef free(): ...\n"})
    result = build(repo_a, cfg, download=False)
    assert seen == [("pkg/slots.py", "python")] and result.parsed == 1
    assert result.resolved >= 1  # pkg/cap.py imports it and is re-resolved, not re-parsed


def _add(repo, cfg):
    commit(repo, {"pkg/extra.py": b"from pkg.cap import RunCapHold\n\nRunCapHold().hold()\n"})


def _change_referenced(repo, cfg):
    commit(repo, {"pkg/slots.py": b"def lend():\n    return 2\n"})


def _delete_imported(repo, cfg):
    commit(repo, {"pkg/slots.py": None})


def _rename_imported(repo, cfg):
    run_git(repo, "mv", "pkg/slots.py", "pkg/leases.py")
    commit(repo, {})


def _revert(repo, cfg):
    commit(repo, {"pkg/slots.py": b"def lend():\n    return 2\n", "pkg/new.py": b"X = 1\n"})
    build(repo, cfg, download=False)
    run_git(repo, "revert", "--no-edit", "HEAD")


def _amend(repo, cfg):
    commit(repo, {"pkg/slots.py": b"def lend():\n    return 2\n"})
    build(repo, cfg, download=False)
    (repo / "pkg" / "slots.py").write_bytes(b"def lease():\n    return 3\n\ndef free():\n    return 0\n")
    run_git(repo, "commit", "-q", "-a", "--amend", "-m", "amended")


def _amend_and_prune(repo, cfg):
    _amend(repo, cfg)
    previous = dict(read(repo, graph_store.meta))["commit"]
    run_git(repo, "reflog", "expire", "--expire=now", "--all")
    run_git(repo, "gc", "-q", "--prune=now")
    assert not graph_git.is_commit(repo, previous)
    assert graph_git.changes(repo, previous, run_git(repo, "rev-parse", "HEAD").strip()) is None


@pytest.mark.parametrize("edit", [_add, _change_referenced, _delete_imported, _rename_imported, _revert, _amend,
                                  _amend_and_prune])
def test_delta_equals_full(repo_a, cfg, tmp_path, monkeypatch, edit):
    build(repo_a, cfg, download=False)
    edit(repo_a, cfg)
    delta = build(repo_a, cfg, download=False)
    assert not delta.full
    delta_graph = graph_of(repo_a)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "elsewhere"))
    full = build(repo_a, cfg, download=False, full=True)
    assert full.full and full.commit == delta.commit
    assert graph_of(repo_a) == delta_graph


def test_a_rename_and_a_revert_parse_nothing(repo_a, cfg, monkeypatch):
    build(repo_a, cfg, download=False)
    seen = spy_on_parser(monkeypatch)
    _rename_imported(repo_a, cfg)
    renamed = build(repo_a, cfg, download=False)
    assert seen == [] and (renamed.parsed, renamed.cached, renamed.removed) == (0, 1, 1)
    commit(repo_a, {"pkg/leases.py": b"def lend():\n    return 2\n"})
    build(repo_a, cfg, download=False)
    seen.clear()
    run_git(repo_a, "revert", "--no-edit", "HEAD")
    reverted = build(repo_a, cfg, download=False)
    assert seen == [] and (reverted.parsed, reverted.cached) == (0, 1)


def test_cap_imports_slots_and_references_lease(repo_a, cfg):
    build(repo_a, cfg, download=False)
    rid = pack.repo_id(repo_a)
    edges = {(e[0], e[1], e[2]) for e in read(repo_a, graph_store.dump)["edges"]}
    assert (f"{rid}:file:pkg/cap.py#", f"{rid}:file:pkg/slots.py#", "imports") in edges
    assert (f"{rid}:symbol:pkg/cap.py#RunCapHold.hold", f"{rid}:symbol:pkg/slots.py#lease", "references") in edges
    _change_referenced(repo_a, cfg)
    build(repo_a, cfg, download=False)
    edges = {(e[0], e[1], e[2]) for e in read(repo_a, graph_store.dump)["edges"]}
    assert not any(kind == "references" and dst.endswith("#lease") for _, dst, kind in edges)


# --- what is read: .gitignore, secrets, the pack's exclude ---


IGNORED = {
    ".gitignore": b"generated/\n",
    "pkg/a.py": b"A = 1\n",
    "a.snap": b"snapshot\n",
    "generated/api.py": b"API = 1\n",
    ".env": b"TOKEN=secret\n",
}


def test_gitignore_and_secrets_are_never_read(tmp_path, cfg, monkeypatch):
    repo = make_repo(tmp_path, IGNORED, force=True)
    asked: list[str] = []
    real = graph_git.read_blobs
    monkeypatch.setattr(graph_git, "read_blobs", lambda r, blobs, limit: asked.extend(blobs) or real(r, blobs, limit))
    result = build(repo, cfg, download=False)
    blob = {path: run_git(repo, "rev-parse", f"HEAD:{path}").strip() for path in ("generated/api.py", ".env")}
    assert not set(blob.values()) & set(asked)
    assert result.left_out == {"ignored": 1, "secret": 1}
    assert read(repo, graph_store.nodes_at, "generated/api.py") == [] and read(repo, graph_store.nodes_at, ".env") == []
    states = {path: row.state for path, row in rows(repo).items()}
    assert states == {".gitignore": "unparsed", "pkg/a.py": "indexed", "a.snap": "unparsed",
                      "generated/api.py": "ignored", ".env": "secret"}


def test_a_gitignore_commit_judges_every_path_again(tmp_path, cfg, monkeypatch):
    repo = make_repo(tmp_path, IGNORED, force=True)
    build(repo, cfg, download=False)
    assert read(repo, graph_store.nodes_at, "a.snap")
    seen = spy_on_parser(monkeypatch)
    commit(repo, {".gitignore": b"generated/\n*.snap\n"})
    added = build(repo, cfg, download=False)
    assert read(repo, graph_store.nodes_at, "a.snap") == [] and rows(repo)["a.snap"].state == "ignored"
    assert seen == [] and added.removed == 1 and added.left_out == {"ignored": 2, "secret": 1}
    commit(repo, {".gitignore": b"generated/\n"})
    build(repo, cfg, download=False)
    assert read(repo, graph_store.nodes_at, "a.snap") and seen == []
    commit(repo, {".gitignore": b"generated/\n*_snap.py\n", "x_snap.py": b"S = 1\n"}, force=True)
    build(repo, cfg, download=False)
    assert seen == [] and rows(repo)["x_snap.py"].state == "ignored"
    commit(repo, {".gitignore": b"generated/\n"})
    removed = build(repo, cfg, download=False)
    assert seen == [("x_snap.py", "python")] and removed.parsed == 1


def test_uncommitted_edits_and_local_excludes_change_nothing(tmp_path, cfg, monkeypatch):
    repo = make_repo(tmp_path, IGNORED, force=True)
    build(repo, cfg, download=False)
    before = graph_of(repo)
    (repo / ".gitignore").write_bytes(b"pkg/\n")
    (repo / "pkg" / "a.py").write_bytes(b"def changed(): ...\n")
    (repo / ".git" / "info").mkdir(exist_ok=True)
    (repo / ".git" / "info" / "exclude").write_text("pkg/\n*.snap\n")
    excludes = tmp_path / "global-excludes"
    excludes.write_text("*.py\n")
    run_git(repo, "config", "core.excludesFile", str(excludes))
    again = build(repo, cfg, download=False)
    assert (again.parsed, again.cached, again.resolved, again.removed) == (0, 0, 0, 0) and graph_of(repo) == before
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "elsewhere"))
    build(repo, cfg, download=False, full=True)
    assert graph_of(repo) == before


def full_graph_of(repo: Path, cfg, monkeypatch, where: Path) -> dict:
    """The graph a full build in another state dir gives at the same commit; the state dir is put back after."""
    home = os.environ["XDG_STATE_HOME"]
    monkeypatch.setenv("XDG_STATE_HOME", str(where))
    try:
        build(repo, cfg, download=False, full=True)
        return graph_of(repo)
    finally:
        monkeypatch.setenv("XDG_STATE_HOME", home)


def test_an_ignore_file_inside_an_ignored_folder_is_never_read(tmp_path, cfg, monkeypatch):
    repo = make_repo(tmp_path, {".gitignore": b"generated/\n", "generated/.gitignore": b"!keep.py\n# inner\n",
                                "generated/keep.py": b"K = 1\n", "a.py": b"A = 1\n"}, force=True)
    inner = run_git(repo, "rev-parse", "HEAD:generated/.gitignore").strip()
    asked: list[str] = []
    real = graph_git.read_blobs
    monkeypatch.setattr(graph_git, "read_blobs", lambda r, blobs, limit: asked.extend(blobs) or real(r, blobs, limit))
    build(repo, cfg, download=False)
    assert git_ignores(repo, "generated/keep.py")
    assert inner not in asked and read(repo, graph_store.ignore_files) == {"": "generated/\n"}
    assert rows(repo)["generated/keep.py"].state == "ignored"
    commit(repo, {".gitignore": b"*.log\n"})  # the folder is no longer ignored: its own file is loaded now
    build(repo, cfg, download=False)
    assert inner in asked and read(repo, graph_store.ignore_files)["generated"] == "!keep.py\n# inner\n"
    assert rows(repo)["generated/keep.py"].state == "indexed"
    delta = graph_of(repo)
    assert full_graph_of(repo, cfg, monkeypatch, tmp_path / "elsewhere") == delta


def git_ignores(repo: Path, path: str) -> bool:
    """`git check-ignore --no-index` on `path`, with no excludes file of this machine."""
    out = subprocess.run(["git", "-c", "core.excludesFile=/dev/null", "check-ignore", "--no-index", "-q", path],
                         cwd=repo, capture_output=True)
    assert out.returncode in (0, 1), out.stderr
    return out.returncode == 0


@pytest.mark.parametrize("root", [b".*\n", b"*.gitignore\n", b".gitignore\n"])
def test_an_ignore_file_that_a_pattern_matches_still_applies(tmp_path, cfg, monkeypatch, root):
    repo = make_repo(tmp_path, {".gitignore": root, "pkg/.gitignore": b"secret.txt\n", "pkg/secret.txt": b"s\n",
                                "pkg/a.py": b"A = 1\n"}, force=True)
    assert git_ignores(repo, "pkg/.gitignore") and git_ignores(repo, "pkg/secret.txt")
    assert not git_ignores(repo, "pkg/a.py")
    secret = run_git(repo, "rev-parse", "HEAD:pkg/secret.txt").strip()
    asked: list[str] = []
    real = graph_git.read_blobs
    monkeypatch.setattr(graph_git, "read_blobs", lambda r, blobs, limit: asked.extend(blobs) or real(r, blobs, limit))
    build(repo, cfg, download=False)
    found = rows(repo)
    assert secret not in asked and found["pkg/secret.txt"].state == "ignored"
    assert found["pkg/.gitignore"].state == "ignored" and found["pkg/a.py"].state == "indexed"
    assert read(repo, graph_store.ignore_files)["pkg"] == "secret.txt\n"


def test_a_failed_ignore_read_is_a_build_error_and_keeps_the_snapshot(tmp_path, cfg, monkeypatch):
    repo = make_repo(tmp_path, IGNORED, force=True)
    first = build(repo, cfg, download=False)
    commit(repo, {".gitignore": b"generated/\n*.snap\n"})

    def failed(*args):
        raise graph_git.ReadFailed("git cat-file --batch-check failed")

    with pytest.MonkeyPatch.context() as patch, pytest.raises(BuildError, match="batch-check failed"):
        patch.setattr(graph_git, "_sizes", failed)
        build(repo, cfg, download=False)
    assert read(repo, graph_store.meta)["commit"] == first.commit
    assert read(repo, graph_store.ignore_files) == {"": "generated/\n"}
    assert sorted(p.name for p in store_of(repo).parent.iterdir()) == ["build.lock", "graph.sqlite"]
    build(repo, cfg, download=False)
    assert rows(repo)["a.snap"].state == "ignored" and rows(repo)["generated/api.py"].state == "ignored"
    delta = graph_of(repo)
    assert full_graph_of(repo, cfg, monkeypatch, tmp_path / "elsewhere") == delta


def test_a_failed_blob_read_is_a_build_error_never_a_verdict(repo_a, cfg, tmp_path, monkeypatch):
    first = build(repo_a, cfg, download=False)
    commit(repo_a, {"pkg/slots.py": b"def lend(): ...\n"})

    def failed(*args):
        raise graph_git.ReadFailed("git cat-file --batch failed")

    with pytest.MonkeyPatch.context() as patch, pytest.raises(BuildError, match="batch failed"):
        patch.setattr(graph_git, "_batch", failed)
        build(repo_a, cfg, download=False)
    assert read(repo_a, graph_store.meta)["commit"] == first.commit and rows(repo_a)["pkg/slots.py"].state == "indexed"
    seen = spy_on_parser(monkeypatch)
    again = build(repo_a, cfg, download=False)
    assert seen == [("pkg/slots.py", "python")] and again.previous == first.commit
    assert rows(repo_a)["pkg/slots.py"].state == "indexed"
    delta = graph_of(repo_a)
    assert full_graph_of(repo_a, cfg, monkeypatch, tmp_path / "elsewhere") == delta


def test_a_new_engine_judges_every_path_again(repo_a, cfg, monkeypatch):
    first = build(repo_a, cfg, download=False)
    monkeypatch.setattr(pack, "ENGINE_VERSION", "99.0")
    seen = spy_on_parser(monkeypatch)
    upgraded = build(repo_a, cfg, download=False)
    assert upgraded.full and upgraded.previous == first.commit and seen == [] and upgraded.cached == 6
    assert json.loads(read(repo_a, graph_store.meta)["settings"])["engine"] == "99.0"


def test_a_nested_gitignore_applies_below_its_folder_only(tmp_path, cfg):
    repo = make_repo(tmp_path, {"pkg/.gitignore": b"gen_*.py\n", "pkg/gen_a.py": b"A = 1\n", "gen_b.py": b"B = 1\n",
                                "pkg/sub/gen_c.py": b"C = 1\n"}, force=True)
    build(repo, cfg, download=False)
    states = {path: row.state for path, row in rows(repo).items()}
    assert (states["pkg/gen_a.py"], states["pkg/sub/gen_c.py"], states["gen_b.py"]) == ("ignored", "ignored", "indexed")


def test_graph_exclude_gives_excluded(tmp_path, cfg):
    repo = make_repo(tmp_path, {"docs/drafts/x.md": b"# X\n", "docs/y.md": b"# Y\n"})
    excluding = dataclasses.replace(cfg, graph=GraphPolicy(enabled=True, exclude=("docs/drafts/",)))
    result = build(repo, excluding, download=False)
    assert rows(repo)["docs/drafts/x.md"].state == "excluded" and rows(repo)["docs/y.md"].state == "indexed"
    assert result.left_out == {"excluded": 1}
    back = build(repo, cfg, download=False)  # the setting changed: every path is judged again
    assert back.full and back.previous == result.commit and rows(repo)["docs/drafts/x.md"].state == "indexed"


def test_nothing_in_the_repository_runs(tmp_path, cfg):
    markers = tmp_path / "markers"
    markers.mkdir()
    py = "open({!r}, 'w').write('ran')\n"
    repo = make_repo(tmp_path, {
        "conftest.py": py.format(str(markers / "conftest")).encode(),
        "setup.py": py.format(str(markers / "setup")).encode(),
        "sitecustomize.py": py.format(str(markers / "sitecustomize")).encode(),
        "package.json": json.dumps({"name": "x", "scripts": {"install": f"touch {markers / 'npm'}",
                                                             "preinstall": f"touch {markers / 'npm-pre'}"}}).encode(),
    })
    result = build(repo, cfg, download=False)
    assert result.parsed == 4 and list(markers.iterdir()) == []


def test_large_and_binary_files_are_unparsed_with_a_file_node(tmp_path, cfg, monkeypatch):
    repo = make_repo(tmp_path, {"big.py": b"x = 1\n" * 400, "nul.py": b"x = 1\n\0\n", "ok.py": b"Y = 2\n"})
    seen = spy_on_parser(monkeypatch)
    small = dataclasses.replace(cfg, graph=GraphPolicy(enabled=True, max_file_kb=1))
    build(repo, small, download=False)
    assert seen == [("ok.py", "python")]
    found = rows(repo)
    assert (found["big.py"].state, found["big.py"].note) == ("unparsed", "over_max_file_kb")
    assert (found["nul.py"].state, found["nul.py"].note) == ("unparsed", "binary")
    for path in ("big.py", "nul.py"):
        assert [n.kind for n in read(repo, graph_store.nodes_at, path)] == ["file"]


def test_notes_are_cut_to_fifty_with_a_count(tmp_path, cfg):
    repo = make_repo(tmp_path, {f"bin{n:02}.py": b"\0" for n in range(60)})
    notes = build(repo, cfg, download=False).notes
    assert len(notes) == 50 and notes[0] == "bin00.py: binary" and notes[-1] == "11 more note(s)"


def test_doc_roots_are_the_folders_project_docs_names(cfg):
    assert doc_roots(cfg.project.docs) == ("docs", "docs/decisions", "docs/plans", "docs/specs")
    assert doc_roots({"a": "./notes/", "b": "/abs/x.md", "c": "../up", "d": "README.md", "spec_file": "x/{module}.md"}) == (
        "abs", "notes")


# --- refusals and interruptions ---


def test_refusals(tmp_path, repo_a, base_cfg, cfg):
    with pytest.raises(BuildError, match=r"graph\.enabled"):
        build(repo_a, base_cfg, download=False)
    empty = tmp_path / "empty"
    empty.mkdir()
    run_git(empty, "init", "-q", "-b", "main")
    with pytest.raises(BuildError):
        build(empty, cfg, download=False)
    with pytest.raises(BuildError):
        build(repo_a, cfg, download=False, commit="HEAD")
    assert not store_of(repo_a).parent.exists()


def test_a_second_build_is_refused_while_one_holds_the_lock(repo_a, cfg):
    build(repo_a, cfg, download=False)
    stale = store_of(repo_a).with_name("graph.sqlite.tmp-1-live")
    stale.write_bytes(b"a live builder's temp file")
    fd = os.open(store_of(repo_a).parent / "build.lock", os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BuildError) as refused:
            build(repo_a, cfg, download=False)
        assert str(refused.value) == "another build is running" and stale.exists()
    finally:
        os.close(fd)
    build(repo_a, cfg, download=False)
    assert not stale.exists()


def test_an_interrupted_publish_keeps_the_old_snapshot(repo_a, cfg, monkeypatch):
    first = build(repo_a, cfg, download=False)
    commit(repo_a, {"pkg/slots.py": b"def lend(): ...\n"})

    def interrupt(*args):
        raise KeyboardInterrupt

    with pytest.MonkeyPatch.context() as patch, pytest.raises(KeyboardInterrupt):
        patch.setattr(graph_store, "publish", interrupt)
        build(repo_a, cfg, download=False)
    assert read(repo_a, graph_store.meta)["commit"] == first.commit
    killed = store_of(repo_a).with_name("graph.sqlite.tmp-99-killed")
    killed.write_bytes(b"left by a killed build")
    again = build(repo_a, cfg, download=False)
    assert again.previous == first.commit and again.parsed == 1
    assert sorted(p.name for p in store_of(repo_a).parent.iterdir()) == ["build.lock", "graph.sqlite"]


def test_a_full_disk_is_a_build_error_and_keeps_the_snapshot(repo_a, cfg):
    first = build(repo_a, cfg, download=False)
    commit(repo_a, {"pkg/slots.py": b"def lend(): ...\n"})

    def full_disk(*args):
        raise sqlite3.OperationalError("database or disk is full")

    for target in ("prune_facts", "publish"):  # during the build, and at the `COMMIT` of the publish
        with pytest.MonkeyPatch.context() as patch, pytest.raises(BuildError, match="disk is full"):
            patch.setattr(graph_store, target, full_disk)
            build(repo_a, cfg, download=False)
        assert read(repo_a, graph_store.meta)["commit"] == first.commit
        assert sorted(p.name for p in store_of(repo_a).parent.iterdir()) == ["build.lock", "graph.sqlite"]
    assert build(repo_a, cfg, download=False).previous == first.commit  # the lock was released


def test_a_filesystem_that_cannot_lock_is_a_build_error(repo_a, cfg):
    def no_locks(fd, operation):
        raise OSError(errno.ENOLCK, "No locks available")

    with pytest.MonkeyPatch.context() as patch, pytest.raises(BuildError, match="No locks available"):
        patch.setattr(fcntl, "flock", no_locks)
        build(repo_a, cfg, download=False)
    assert not store_of(repo_a).exists()
    assert build(repo_a, cfg, download=False).full


def test_a_directory_at_the_store_path_is_a_build_error(repo_a, cfg):
    store_of(repo_a).mkdir(parents=True)
    with pytest.raises(BuildError):
        build(repo_a, cfg, download=False)
    assert sorted(p.name for p in store_of(repo_a).parent.iterdir()) == ["build.lock", "graph.sqlite"]


# --- versions and grammars ---


def test_a_stored_version_that_differs_is_extracted_again(repo_a, cfg, monkeypatch):
    build(repo_a, cfg, download=False)
    conn = sqlite3.connect(store_of(repo_a))
    conn.execute("UPDATE files SET version = '0' WHERE lang = 'python'")  # as an older extractor would have left it
    conn.execute("UPDATE facts SET version = '0' WHERE lang = 'python'")
    conn.commit()
    conn.close()
    seen = spy_on_parser(monkeypatch)
    result = build(repo_a, cfg, download=False)
    assert sorted(seen) == [("pkg/cap.py", "python"), ("pkg/slots.py", "python"), ("tests/test_cap.py", "python")]
    assert result.parsed == 3 and {row.version for row in rows(repo_a).values() if row.lang == "python"} == {"1"}


TS = {"web/a.ts": b'import { b } from "./b";\nexport const a = b;\n', "web/b.ts": b"export const b = 1;\n",
      "app.py": b"A = 1\n"}


def test_a_missing_grammar_is_unparsed_and_parsed_once_present(tmp_path, cfg, monkeypatch):
    repo = make_repo(tmp_path, TS)
    with pytest.MonkeyPatch.context() as patch:
        stdlib_only(patch)
        first = build(repo, cfg, download=False)
    found = rows(repo)
    assert (found["web/a.ts"].state, found["web/a.ts"].note, found["web/a.ts"].version) == ("unparsed", "no_grammar", "")
    assert first.languages == {"python": "exact", "typescript": "not parsed"} and "web/a.ts: no_grammar" in first.notes
    assert [n.kind for n in read(repo, graph_store.nodes_at, "web/a.ts")] == ["file"]
    need_grammars()
    seen = spy_on_parser(monkeypatch)
    later = build(repo, cfg, download=False)  # no new commit: the stored version differed from the parser's
    assert sorted(seen) == [("web/a.ts", "typescript"), ("web/b.ts", "typescript")]
    assert rows(repo)["web/a.ts"].state == "indexed" and later.languages["typescript"] == "exact"
    kept = graph_of(repo)
    stdlib_only(monkeypatch)
    commit(repo, {"app.py": b"A = 2\n"})  # the parser starts and offers no TypeScript: the stored facts stay
    build(repo, cfg, download=False)
    assert rows(repo)["web/a.ts"].state == "indexed"
    assert [n for n in graph_of(repo)["nodes"] if n[3].startswith("web/")] == [n for n in kept["nodes"] if n[3].startswith("web/")]


def test_typescript_end_to_end(tmp_path, cfg):
    need_grammars()
    repo = make_repo(tmp_path, TS)
    build(repo, cfg, download=False)
    rid = pack.repo_id(repo)
    edges = {(e[0], e[1], e[2]) for e in read(repo, graph_store.dump)["edges"]}
    assert (f"{rid}:file:web/a.ts#", f"{rid}:file:web/b.ts#", "imports") in edges
