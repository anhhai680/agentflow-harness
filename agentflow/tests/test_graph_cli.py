"""`agentflow graph build|status|query|show`: the lines, the refusals, an untrusted store and an untrusted repository."""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest
import yaml
from conftest import SPARSE_PACK, run_git

from agentflow import flow, graph_build, graph_report, graph_store, pack
from agentflow.config import load_config
from agentflow.graph_build import Result
from agentflow.graph_model import Edge, Node

ESC_NAME = "a\x1b[31m.py"
BIDI_NAME = "b\u202e.py"
FILES = {
    "pkg/slots.py": b"def lease():\n    return 1\n",
    "pkg/cap.py": b'from pkg.slots import lease\n\n\nclass RunCapHold:\n    """Hold a run cap."""\n\n    def hold(self):\n        return lease()\n',
    "tests/test_cap.py": b"from pkg.cap import RunCapHold\n\n\ndef test_hold():\n    assert RunCapHold().hold()\n",
    "README.md": b"# Toy\n\nSee [the guide](docs/guide.md).\n",
    "docs/guide.md": b"# Guide\n\nCall `lease` first.\n",
    ".gitignore": b"generated/\n",
    "generated/api.py": b"def api():\n    return 1\n",
    ESC_NAME: b"def escaped():\n    return 1\n",
    BIDI_NAME: b"def flipped():\n    return 1\n",
}
STATE = "../graph-state"  # beside the repository: a store inside its checkout is refused (`copy_of` copies both)


def make_repo(root: Path, *, enabled: bool = True, base_branch: str = "main") -> Path:
    root.mkdir()
    run_git(root, "init", "-q", "-b", "main")
    for key, value in (("user.name", "Test"), ("user.email", "test@example.com"), ("commit.gpgsign", "false")):
        run_git(root, "config", key, value)
    config = {**SPARSE_PACK, "graph": {"enabled": enabled}, "paths": {"state_dir": STATE},
              "project": {"base_branch": base_branch}}
    (root / ".agentflow").mkdir()
    (root / ".agentflow" / "config.yaml").write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    for path, data in FILES.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_bytes(data)
    run_git(root, "add", "-A")
    run_git(root, "add", "-f", "generated/api.py")
    run_git(root, "commit", "-q", "-m", "initial")
    return root


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    """`graph build` may download the parser's packages; a test never does."""
    real = graph_build.build
    monkeypatch.setattr(graph_build, "build", lambda repo, cfg, **kw: real(repo, cfg, **{**kw, "download": False}))


@pytest.fixture
def repo(tmp_path, monkeypatch) -> Path:
    root = make_repo(tmp_path / "repo")
    monkeypatch.chdir(root)
    return root


@pytest.fixture(scope="module")
def built(tmp_path_factory) -> Path:
    """One repository, indexed once; tests that only read copy nothing and change nothing."""
    root = make_repo(tmp_path_factory.mktemp("built") / "repo")
    graph_build.build(root, load_config(root / ".agentflow" / "config.yaml"), download=False)  # no monkeypatch here
    return root


@pytest.fixture
def indexed(built, monkeypatch) -> Path:
    monkeypatch.chdir(built)
    return built


def graph(capsys, *argv: str) -> tuple[int, str]:
    code = flow.main(list(argv))
    return code, capsys.readouterr().out


def store_of(root: Path) -> Path:
    return graph_store.path_for(pack.state_dir(root))


def copy_of(built: Path, where: Path) -> Path:
    """`built` and its state dir copied to `where/copy` and `where/graph-state`, so the copy carries the index."""
    copy = where / "copy"
    shutil.copytree(built, copy)
    shutil.copytree(built.parent / "graph-state", where / "graph-state")
    assert store_of(copy) == (where / "graph-state" / "graph" / "graph.sqlite").resolve()
    return copy


# --- build ---


def test_build_prints_its_lines_and_a_second_build_changes_nothing(repo, capsys):
    code, out = graph(capsys, "graph", "build")
    lines = out.splitlines()
    sha = run_git(repo, "rev-parse", "HEAD").strip()[:12]
    assert code == 0
    assert lines[0].startswith(f"graph: {sha}: parsed ") and lines[0].endswith(" ms)")
    assert any(line.startswith("languages: ") and "python exact" in line for line in lines)
    assert any(line.startswith("left out: .gitignore 1 · secret 0 · vendored 0 · excluded 0 · other 0") for line in lines)
    code, out = graph(capsys, "graph", "build")
    assert code == 0 and "parsed 0, from cache 0, re-resolved 0, removed 0" in out.splitlines()[0]


def test_build_refuses_when_graph_is_off_and_creates_nothing(tmp_path, monkeypatch, capsys):
    root = make_repo(tmp_path / "off", enabled=False)
    monkeypatch.chdir(root)
    code, out = graph(capsys, "graph", "build")
    assert code == 1
    assert out.strip() == ("error: graph.enabled is false: set it to true in .agentflow/config.yaml to build the index")
    assert not (root.parent / "graph-state" / "graph").exists()


def test_build_error_text_is_printable(tmp_path, monkeypatch, capsys):
    root = make_repo(tmp_path / "odd", base_branch="nope\x1b[31m")
    monkeypatch.chdir(root)
    code, out = graph(capsys, "graph", "build")
    assert code == 1 and out.startswith("error: ") and "\x1b" not in out and "nope" in out


def test_build_rebuilds_over_a_store_of_random_bytes(repo, capsys):
    store = store_of(repo)
    store.parent.mkdir(parents=True)
    store.write_bytes(bytes(range(256)) * 64)
    code, out = graph(capsys, "graph", "status")
    assert (code, out.strip()) == (1, graph_report.NO_INDEX)
    code, out = graph(capsys, "graph", "build")
    assert code == 0 and out.startswith("graph: ")
    assert graph(capsys, "graph", "status")[0] == 0


# --- status ---


def test_status_before_a_build_says_there_is_no_index(repo, capsys):
    code, out = graph(capsys, "graph", "status")
    assert (code, out.strip()) == (1, "graph: no index yet: run `agentflow graph build`")
    assert not (repo.parent / "graph-state" / "graph").exists()  # reading creates nothing


def test_status_after_a_build_has_the_commit_counts_and_languages(indexed, capsys):
    sha = run_git(indexed, "rev-parse", "HEAD").strip()
    code, out = graph(capsys, "graph", "status")
    lines = out.splitlines()
    assert code == 0 and lines[0].startswith(f"graph: {sha[:12]}, built ")
    assert lines[1].startswith("files ") and "nodes " in lines[1] and "edges " in lines[1]
    assert "python exact" in lines[2]
    code, out = graph(capsys, "graph", "status", "--json")
    data = json.loads(out)
    assert code == 0 and data["commit"] == sha
    assert {"commit", "files", "nodes", "edges", "left_out", "languages"} <= set(data)
    assert data["left_out"] == {"ignored": 1} and data["languages"]["python"] == "exact"
    assert data["files"] >= 8 and data["nodes"] > data["files"] and data["edges"] > 0


def test_status_works_after_graph_is_turned_off(built, tmp_path, monkeypatch, capsys):
    copy = copy_of(built, tmp_path)
    path = copy / ".agentflow" / "config.yaml"
    path.write_text(path.read_text().replace("enabled: true", "enabled: false"), encoding="utf-8")
    monkeypatch.chdir(copy)
    assert graph(capsys, "graph", "status")[0] == 0


# --- query ---


def test_query_lists_the_node_for_a_path(indexed, capsys):
    sha = run_git(indexed, "rev-parse", "HEAD").strip()[:12]
    code, out = graph(capsys, "graph", "query", "run cap")
    lines = out.splitlines()
    assert code == 0 and lines[0] == f"# index at {sha}"
    assert any("pkg/cap.py:" in line for line in lines[1:])
    assert all(len(line.split("  ")) >= 3 for line in lines[1:])


def test_query_kind_keeps_only_that_kind(indexed, capsys):
    code, out = graph(capsys, "graph", "query", "run cap", "--kind", "symbol")
    hits = out.splitlines()[1:]
    assert code == 0 and hits and all(line.startswith("symbol  ") for line in hits)
    assert any("RunCapHold" in line for line in hits)
    code, out = graph(capsys, "graph", "query", "run cap", "--kind", "file", "--limit", "1")
    assert code == 0 and len(out.splitlines()) == 2


def test_query_text_is_cut_to_80_characters(indexed, capsys):
    out = graph(capsys, "graph", "query", "hold run cap", "--kind", "symbol")[1]
    text = [line.partition(" — ")[2] for line in out.splitlines() if " — " in line]
    assert text and all(len(item) <= 80 for item in text)


@pytest.mark.parametrize("limit", ["0", "51", "x"])
def test_query_limit_outside_1_to_50_is_an_argument_error(indexed, capsys, limit):
    with pytest.raises(SystemExit) as stopped:
        flow.main(["graph", "query", "cap", "--limit", limit])
    assert stopped.value.code == 2 and "--limit" in capsys.readouterr().err


def test_query_depth_and_kind_are_checked_by_argparse(indexed, capsys):
    for argv in (["graph", "show", "x", "--depth", "0"], ["graph", "show", "x", "--depth", "4"],
                 ["graph", "query", "x", "--kind", "banana"]):
        with pytest.raises(SystemExit) as stopped:
            flow.main(argv)
        assert stopped.value.code == 2
    capsys.readouterr()


def test_query_with_no_word_prints_no_match(indexed, capsys):
    assert graph(capsys, "graph", "query", '"* OR') == (0, "no match\n")
    assert graph(capsys, "graph", "query", "zzzqqqxxx") == (0, "no match\n")


def test_query_json(indexed, capsys):
    code, out = graph(capsys, "graph", "query", "run cap", "--json", "--limit", "3")
    data = json.loads(out)
    assert code == 0 and len(data["hits"]) <= 3 and data["commit"]
    assert {"id", "kind", "name", "path", "line", "score"} <= set(data["hits"][0])


# --- show ---


def test_show_a_path_lists_its_symbols_and_imports_with_confidence(indexed, capsys):
    code, out = graph(capsys, "graph", "show", "pkg/cap.py")
    lines = out.splitlines()
    assert code == 0 and lines[1].startswith("file  pkg/cap.py:")
    assert any("contains" in line and "symbol" in line and "RunCapHold" in line for line in lines)
    imports = [line for line in lines if " imports [" in line]
    assert imports and all(re.fullmatch(r"  (->|<-) imports \[\w+\]  \S.*", line) for line in imports)
    assert any("pkg/slots.py" in line for line in imports)


def test_show_by_node_id_and_depth(indexed, capsys):
    code, out = graph(capsys, "graph", "query", "RunCapHold", "--kind", "symbol", "--json")
    node_id = json.loads(out)["hits"][0]["id"]
    assert ":" in node_id
    code, out = graph(capsys, "graph", "show", node_id)
    assert code == 0 and "RunCapHold" in out.splitlines()[1]
    shallow = graph(capsys, "graph", "show", "pkg/cap.py")[1].splitlines()
    deep = graph(capsys, "graph", "show", "pkg/cap.py", "--depth", "3")[1].splitlines()
    assert len(deep) > len(shallow)


def test_show_json(indexed, capsys):
    code, out = graph(capsys, "graph", "show", "pkg/cap.py", "--json")
    data = json.loads(out)
    assert code == 0 and data["node"]["path"] == "pkg/cap.py"
    assert {"imports", "contains"} <= {edge["kind"] for edge in data["edges"]}
    assert all(edge["confidence"] and edge["direction"] in ("to", "from") for edge in data["edges"])


def test_show_a_left_out_path_names_its_state(indexed, capsys):
    code, out = graph(capsys, "graph", "show", "generated/api.py")
    assert (code, out.strip()) == (1, "no node: generated/api.py is left out (ignored)")


def test_show_an_unknown_target_has_no_node(indexed, capsys):
    assert graph(capsys, "graph", "show", "nope/none.py") == (1, "no node: nope/none.py\n")
    assert graph(capsys, "graph", "show", "x:file:nope#") == (1, "no node: x:file:nope#\n")


# --- an untrusted repository, an untrusted store ---


@pytest.mark.parametrize("argv", [
    ["graph", "status"], ["graph", "query", "flipped"], ["graph", "show", BIDI_NAME], ["graph", "show", "pkg/cap.py"],
    ["graph", "query", "flipped", "--json"], ["graph", "show", BIDI_NAME, "--json"]])
def test_no_view_prints_an_escape_or_a_bidi_character(indexed, capsys, argv):
    code, out = graph(capsys, *argv)
    assert code == 0 and out
    assert "\x1b" not in out and "\u202e" not in out


def test_a_bidi_name_is_shown_escaped(indexed, capsys):
    assert "b\\u202e.py" in graph(capsys, "graph", "query", "flipped")[1]
    assert "b\\u202e.py" in graph(capsys, "graph", "show", BIDI_NAME, "--json")[1]  # the JSON escape, not the text one


def test_a_path_with_an_escape_byte_is_left_out_and_never_printed_raw(repo, capsys):
    code, out = graph(capsys, "graph", "build")
    assert code == 0 and "\x1b" not in out and "control character" in out
    code, out = graph(capsys, "graph", "show", ESC_NAME)  # the target is echoed back
    assert (code, out) == (1, "no node: a\\x1b[31m.py\n")
    assert graph(capsys, "graph", "show", ESC_NAME, "--json")[1] == "no node: a\\x1b[31m.py\n"
    code, out = graph(capsys, "graph", "query", "escaped")
    assert "\x1b" not in out


def test_a_planted_store_with_escapes_in_it_prints_them_escaped(built, tmp_path, monkeypatch, capsys):
    """A name and a text that carry an escape sequence and a bidi override, as a store from elsewhere could."""
    copy = copy_of(built, tmp_path)
    conn = sqlite3.connect(store_of(copy))
    conn.execute("UPDATE nodes SET name = name || char(27) || '[31m' || char(8238), text = 'x' || char(27) || '[2J' || char(8238)")
    conn.execute("UPDATE meta SET value = value || char(27) WHERE key = 'commit'")
    conn.commit()
    conn.close()
    monkeypatch.chdir(copy)
    for argv in (["graph", "status"], ["graph", "query", "cap"], ["graph", "show", "pkg/cap.py"],
                 ["graph", "query", "cap", "--json"], ["graph", "show", "pkg/cap.py", "--json"]):
        code, out = graph(capsys, *argv)
        assert code == 0 and out and "\x1b" not in out and "\u202e" not in out, argv
    assert "RunCapHold\\x1b[31m\\u202e" in graph(capsys, "graph", "query", "cap")[1]


def test_build_notes_are_printable(repo, capsys, monkeypatch):
    real = graph_build.build
    monkeypatch.setattr(graph_build, "build", lambda r, c, **kw: replace(real(r, c, **kw), notes=("x\x1b[31m\u202e",)))
    code, out = graph(capsys, "graph", "build")
    assert code == 0 and "note: x\\x1b[31m\\u202e" in out and "\x1b" not in out and "\u202e" not in out


def _damaged_copy(built: Path, tmp_path: Path, damage) -> Path:
    copy = copy_of(built, tmp_path)
    damage(store_of(copy))
    return copy


def _zero_the_nodes_pages(store: Path) -> None:
    """Whole pages from the middle of the file, the ones the `nodes` table lives in; the first pages stay intact."""
    conn = sqlite3.connect(store)
    root = conn.execute("SELECT rootpage FROM sqlite_master WHERE name = 'nodes'").fetchone()[0]
    size = conn.execute("PRAGMA page_size").fetchone()[0]
    conn.close()
    data = bytearray(store.read_bytes())
    assert root > 2 and len(data) > (root + 3) * size
    data[(root - 1) * size:(root + 2) * size] = bytes(3 * size)
    store.write_bytes(bytes(data))


def _recreate_nodes_with_other_columns(store: Path) -> None:
    conn = sqlite3.connect(store)
    conn.execute("DROP TABLE nodes")
    conn.execute("CREATE TABLE nodes (id TEXT, other TEXT)")
    conn.commit()
    conn.close()


_READS = (["graph", "query", "cap"], ["graph", "query", "cap", "--json"], ["graph", "show", "pkg/cap.py"],
          ["graph", "show", "pkg/cap.py", "--json"])


@pytest.mark.parametrize("damage, argvs", [(_zero_the_nodes_pages, [["graph", "status"], ["graph", "status", "--json"], *_READS]),
                                           (_recreate_nodes_with_other_columns, _READS)],
                         ids=["middle pages zeroed", "nodes table with other columns"])
def test_a_store_that_opens_but_fails_at_a_query_is_no_index_and_no_traceback(built, tmp_path, monkeypatch, capsys, damage, argvs):
    monkeypatch.chdir(_damaged_copy(built, tmp_path, damage))
    for argv in argvs:
        code, out = graph(capsys, *argv)
        assert (code, out.strip()) == (1, graph_report.NO_INDEX), argv
    assert capsys.readouterr().err == ""


def test_build_rebuilds_a_store_damaged_in_the_middle_with_no_traceback(built, tmp_path, monkeypatch, capsys):
    copy = _damaged_copy(built, tmp_path, _zero_the_nodes_pages)
    monkeypatch.chdir(copy)
    code, out = graph(capsys, "graph", "build")
    assert code == 0 and out.startswith("graph: ") and "parsed 0," not in out.splitlines()[0]
    assert capsys.readouterr().err == ""
    assert graph(capsys, "graph", "status")[0] == 0
    code, out = graph(capsys, "graph", "build")
    assert code == 0 and "parsed 0, from cache 0, re-resolved 0, removed 0" in out.splitlines()[0]


def test_a_state_dir_inside_the_checkout_is_refused(built, tmp_path, monkeypatch, capsys):
    """A repository could commit a crafted store under a `paths.state_dir` of its own: it is never read or built."""
    copy = copy_of(built, tmp_path)
    inside = copy / ".graph-state" / "graph"
    inside.mkdir(parents=True)
    shutil.copy(store_of(copy), inside / "graph.sqlite")  # a sound store, as the repository could have committed it
    path = copy / ".agentflow" / "config.yaml"
    path.write_text(path.read_text().replace("state_dir: ../graph-state", "state_dir: .graph-state"), encoding="utf-8")
    monkeypatch.chdir(copy)
    assert store_of(copy) == (inside / "graph.sqlite").resolve()
    for argv in (["graph", "status"], ["graph", "status", "--json"], ["graph", "query", "cap"],
                 ["graph", "show", "pkg/cap.py"]):
        code, out = graph(capsys, *argv)
        assert (code, out.strip()) == (1, graph_report.NO_INDEX), argv
    before = sorted(p.name for p in inside.iterdir())
    code, out = graph(capsys, "graph", "build")
    assert code == 1 and out.startswith("error: ") and "paths.state_dir" in out
    assert sorted(p.name for p in inside.iterdir()) == before  # no lock, no temp file
    with pytest.raises(graph_build.BuildError, match=r"paths\.state_dir"):
        graph_build.build(copy, load_config(path), download=False)


def _planted(built: Path, tmp_path: Path, *statements: str) -> Path:
    copy = copy_of(built, tmp_path)
    conn = sqlite3.connect(store_of(copy))
    for statement in statements:
        conn.execute(statement)
    conn.commit()
    conn.close()
    return copy


def test_a_blob_where_a_language_belongs_never_crashes_status(built, tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(_planted(built, tmp_path, "UPDATE files SET lang = x'4142'"))
    code, out = graph(capsys, "graph", "status")
    assert code == 0 and out.startswith("graph: ") and "Traceback" not in out and "unexpected" not in out
    code, out = graph(capsys, "graph", "status", "--json")
    assert code == 0 and json.loads(out)["languages"] == {}
    assert capsys.readouterr().err == ""


def test_wrong_types_in_the_node_columns_never_crash_query_or_show(built, tmp_path, monkeypatch, capsys):
    copy = _planted(built, tmp_path, "UPDATE nodes SET name = x'4142', line = 'abc', end_line = x'00', trust = x'41', "
                    "signature = x'42', text = x'43'")
    monkeypatch.chdir(copy)
    for argv in (["graph", "query", "cap"], ["graph", "query", "cap", "--json"], ["graph", "show", "pkg/cap.py"],
                 ["graph", "show", "pkg/cap.py", "--json"]):
        code, out = graph(capsys, *argv)
        assert code in (0, 1) and "unexpected" not in out and "Traceback" not in out, argv
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("value", ["nan", "inf", "-inf", "1e999"])
def test_a_planted_built_at_never_makes_invalid_json(built, tmp_path, monkeypatch, capsys, value):
    monkeypatch.chdir(_planted(built, tmp_path, f"UPDATE meta SET value = '{value}' WHERE key = 'built_at'"))
    code, out = graph(capsys, "graph", "status", "--json")
    data = json.loads(out, parse_constant=lambda name: pytest.fail(f"bare {name} in the JSON"))
    assert code == 0 and data["built_at"] is None and data["age_s"] is None
    code, out = graph(capsys, "graph", "status")
    assert code == 0 and ", built " not in out.splitlines()[0]


def test_no_json_view_can_hold_a_bare_nan(built, tmp_path, monkeypatch, capsys):
    """`allow_nan=False` turns any stray non-finite number into the no-index line, never invalid JSON."""
    monkeypatch.chdir(built)
    monkeypatch.setattr(graph_report, "_score", lambda value: float("nan"))
    code, out = graph(capsys, "graph", "query", "cap", "--json")
    assert (code, out.strip()) == (1, graph_report.NO_INDEX)


def test_the_damaged_store_does_open(built, tmp_path):
    """Pins the case the test above is about: `open_read` accepts both stores, so the failure is at a query."""
    for damage in (_zero_the_nodes_pages, _recreate_nodes_with_other_columns):
        copy = _damaged_copy(built, tmp_path / damage.__name__, damage)
        graph_store.open_read(store_of(copy)).close()


def test_dash_c_equals_running_inside(indexed, tmp_path, monkeypatch, capsys):
    for argv in (["graph", "status", "--json"], ["graph", "query", "run cap"], ["graph", "show", "pkg/cap.py"]):
        inside = graph(capsys, *argv)
        monkeypatch.chdir(tmp_path)
        outside = graph(capsys, "-C", str(indexed), *argv)
        monkeypatch.chdir(indexed)
        assert inside[0] == outside[0] == 0
        if argv[1] != "status":
            assert inside == outside
        else:
            a, b = json.loads(inside[1]), json.loads(outside[1])
            assert {k: v for k, v in a.items() if k != "age_s"} == {k: v for k, v in b.items() if k != "age_s"}


# --- the pure views ---


def _result(**over) -> Result:
    base = dict(commit="a" * 40, previous=None, full=True, parsed=3, cached=2, resolved=5, removed=1,
                left_out={"ignored": 2, "secret": 1, "symlink": 1, "submodule": 2},
                languages={"python": "exact", "typescript": "exact", "javascript": "not parsed"}, notes=(), ms=42)
    return Result(**{**base, **over})


def test_build_lines_have_the_exact_shape():
    assert graph_report.build_lines(_result()) == [
        f"graph: {'a' * 12}: parsed 3, from cache 2, re-resolved 5, removed 1 (42 ms)",
        "languages: python exact · typescript exact · javascript not parsed (no grammar)",
        "left out: .gitignore 2 · secret 1 · vendored 0 · excluded 0 · other 3"]
    quiet = graph_report.build_lines(_result(left_out={}, languages={}))
    assert quiet == [quiet[0], "languages: none"]


def test_build_lines_show_at_most_20_notes():
    lines = graph_report.build_lines(_result(notes=tuple(f"n{i}" for i in range(30))))
    assert [line for line in lines if line.startswith("note: ")] == [f"note: n{i}" for i in range(20)]


def test_status_lines_say_how_old_the_index_is():
    meta = {"commit": "b" * 40, "built_at": "1000", "languages": json.dumps({"python": "1"})}
    counts = {"files": 4, "nodes": 9, "edges": 3, "files:indexed": 3, "files:vendored": 1}
    lines = graph_report.status_lines(meta, counts, 1000 + 2 * 3600)
    assert lines == [f"graph: {'b' * 12}, built 2 hours ago", "files 4 · nodes 9 · edges 3",
                     "languages: python exact", "left out: .gitignore 0 · secret 0 · vendored 1 · excluded 0 · other 0"]
    assert "just now" in graph_report.status_lines(meta, counts, 1000)[0]
    assert graph_report.status_lines({"commit": "c" * 40, "built_at": "x"}, {}, 5)[0] == f"graph: {'c' * 12}"


def test_show_lines_point_each_edge_away_from_the_target():
    target = Node("r:file:a.py#", "file", "a.py", "a.py", 1, 9, "parsed", "", "")
    far = Node("r:file:b.py#", "file", "b.py", "b.py", 1, 5, "parsed", "", "")
    out = Edge(target.id, far.id, "imports", "exact", "a.py")
    back = Edge(far.id, target.id, "tests", "inferred", "b.py")
    assert graph_report.show_lines(target, [(out, far), (back, far)], "d" * 40)[2:] == [
        "  -> imports [exact]  file  b.py:1  b.py", "  <- tests [inferred]  file  b.py:1  b.py"]
