"""graph_resolve: stored facts into nodes and edges, and the keys a file provides and wants."""

from __future__ import annotations

import pytest

from agentflow import graph_markdown, graph_python
from agentflow.graph_manifest import extract as extract_manifest
from agentflow.graph_model import Def, Facts, Import, Link, Ref, Section
from agentflow.graph_resolve import file_node, provided, resolve, wanted

FILES = {
    "pkg/__init__.py": b"",
    "pkg/cap.py": b'"""Run cap."""\nfrom .slots import lease\nfrom pkg import util\ndef hold():\n    return lease() and util.now()\n',
    "pkg/slots.py": b"def lease(): ...\n",
    "pkg/util.py": b"def now(): ...\n",
    "tests/test_cap.py": b"from pkg.cap import hold\ndef test_hold(): assert hold()\n",
    "other/util.py": b"def now(): ...\n",
    "docs/a.md": b"# A\nSee [cap](../pkg/cap.py) and [gone](missing.md).\n",
}


def _extract(path: str, data: bytes) -> Facts:
    if path.endswith(".py"):
        return graph_python.extract(path, data)
    return graph_markdown.extract(path, data)


class Idx:
    def __init__(self, facts: dict[str, Facts], unparsed: tuple[str, ...] = ()) -> None:
        self._facts = facts
        self._keys: dict[str, set[str]] = {}
        for path, f in facts.items():
            for key in provided(path, f):
                self._keys.setdefault(key, set()).add(path)
        for path in unparsed:
            self._keys.setdefault(f"path:{path}", set()).add(path)

    def providers(self, key: str) -> list[str]:
        return sorted(self._keys.get(key, ()))

    def facts(self, path: str) -> Facts | None:
        return self._facts.get(path)


F = {p: _extract(p, b) for p, b in FILES.items()}
IDX = Idx(F)


def _tail(node_id: str) -> str:
    return node_id.split(":", 2)[2]


def _edges(path: str, facts: Facts, index: Idx = IDX) -> set[tuple[str, str, str, str]]:
    _, edges = resolve("r", path, facts, index)
    return {(e.kind, _tail(e.src), _tail(e.dst), e.confidence) for e in edges}


def _py(imports=(), defs=(), refs=()) -> Facts:
    return Facts(lang="python", imports=tuple(imports), defs=tuple(defs), refs=tuple(refs))


def _ts(imports=(), defs=(), refs=()) -> Facts:
    return Facts(lang="typescript", imports=tuple(imports), defs=tuple(defs), refs=tuple(refs))


def _def(name: str, line: int = 1) -> Def:
    return Def(name, "function", line, line, f"def {name}()", "", True)


def test_keys():
    assert provided("pkg/cap.py", F["pkg/cap.py"]) == {"path:pkg/cap.py", "py:cap", "py:pkg.cap"}
    assert {"path:pkg/slots.py", "path:pkg/slots/__init__.py", "py:pkg.util", "py:pkg"} <= wanted("pkg/cap.py", F["pkg/cap.py"])


def test_a_package_init_provides_the_package_names():
    assert provided("a/b/__init__.py", F["pkg/__init__.py"]) == {"path:a/b/__init__.py", "py:b", "py:a.b"}
    assert provided("__init__.py", F["pkg/__init__.py"]) == {"path:__init__.py"}


def test_a_directory_that_is_not_an_identifier_ends_the_module_names():
    assert provided("my-dir/pkg/mod.py", _py()) == {"path:my-dir/pkg/mod.py", "py:mod", "py:pkg.mod"}


def test_a_manifest_provides_its_package():
    facts = extract_manifest("pyproject.toml", b'[project]\nname = "demo"\ndependencies = ["requests>=2"]\n')
    assert provided("pyproject.toml", facts) == {"path:pyproject.toml", "pkg:pypi/demo"}


def test_a_listed_unparsed_file_provides_its_path():
    assert provided("x.bin", None) == {"path:x.bin"}


def test_edges():
    nodes, edges = resolve("r", "pkg/cap.py", F["pkg/cap.py"], IDX)
    e = {(x.kind, _tail(x.src), _tail(x.dst), x.confidence) for x in edges}
    assert ("imports", "pkg/cap.py#", "pkg/slots.py#", "exact") in e
    assert ("imports", "pkg/cap.py#", "pkg/util.py#", "exact") in e
    assert ("imports", "pkg/cap.py#", "pkg/__init__.py#", "exact") not in e
    assert ("references", "pkg/cap.py#hold", "pkg/slots.py#lease", "heuristic") in e
    assert ("references", "pkg/cap.py#hold", "pkg/util.py#now", "heuristic") in e
    assert ("contains", "pkg/cap.py#", "pkg/cap.py#hold", "exact") in e
    assert next(n for n in nodes if n.kind == "file").text == "Run cap."
    assert {n.kind for n in nodes} == {"file", "symbol"}


def test_tests_and_mentions():
    _, edges = resolve("r", "tests/test_cap.py", F["tests/test_cap.py"], IDX)
    assert ("tests", "heuristic") in {(x.kind, x.confidence) for x in edges if x.dst.endswith("pkg/cap.py#")}
    _, edges = resolve("r", "docs/a.md", F["docs/a.md"], IDX)
    assert [_tail(x.dst) for x in edges if x.kind == "mentions"] == ["pkg/cap.py#"]


def test_a_test_does_not_test_a_test():
    files = {"tests/test_a.py": _py([Import("helpers", "", "helpers", 0, 1)]), "tests/helpers.py": _py()}
    assert not {k for k, *_ in _edges("tests/test_a.py", files["tests/test_a.py"], Idx(files))} & {"tests"}


def test_two_providers_at_equal_distance_give_no_edge():
    files = {"a/util.py": _py(), "b/util.py": _py(), "c/x.py": _py([Import("util", "", "util", 0, 1)])}
    assert not [e for e in _edges("c/x.py", files["c/x.py"], Idx(files)) if e[0] == "imports"]


def test_the_nearer_provider_wins():
    files = {"a/b/util.py": _py(), "a/c/util.py": _py(), "z/util.py": _py(), "a/b/x.py": _py([Import("util", "", "util", 0, 1)])}
    imports = [e for e in _edges("a/b/x.py", files["a/b/x.py"], Idx(files)) if e[0] == "imports"]
    assert imports == [("imports", "a/b/x.py#", "a/b/util.py#", "exact")]


def test_a_file_does_not_import_itself():
    files = {"a/util.py": _py([Import("util", "", "util", 0, 1)]), "b/util.py": _py()}
    assert [e for e in _edges("a/util.py", files["a/util.py"], Idx(files)) if e[0] == "imports"] == [
        ("imports", "a/util.py#", "b/util.py#", "exact")
    ]


def test_from_import_of_a_name_goes_to_the_package_when_there_is_no_submodule():
    files = {"pkg/__init__.py": _py(defs=[_def("thing")]), "x.py": _py([Import("pkg", "thing", "thing", 0, 1)], refs=[Ref("thing", 2, "")])}
    e = _edges("x.py", files["x.py"], Idx(files))
    assert ("imports", "x.py#", "pkg/__init__.py#", "exact") in e
    assert ("references", "x.py#", "pkg/__init__.py#thing", "heuristic") in e
    assert len([x for x in e if x[0] == "imports"]) == 1


def test_from_import_of_a_submodule_goes_to_the_submodule_only():
    files = {"pkg/__init__.py": _py(), "pkg/sub.py": _py(defs=[_def("go")]), "x.py": _py([Import("pkg", "sub", "sub", 0, 1)], refs=[Ref("sub.go", 2, "")])}
    e = _edges("x.py", files["x.py"], Idx(files))
    assert {x[2] for x in e if x[0] == "imports"} == {"pkg/sub.py#"}
    assert ("references", "x.py#", "pkg/sub.py#go", "heuristic") in e


def test_import_a_dotted_module_takes_the_most_specific_provider():
    files = {"os/__init__.py": _py(), "os/path.py": _py(defs=[_def("join")]), "x.py": _py([Import("os.path", "", "os", 0, 1)], refs=[Ref("os.path.join", 2, "")])}
    facts = files["x.py"]
    assert {"py:os.path", "py:os"} <= wanted("x.py", facts)
    e = _edges("x.py", facts, Idx(files))
    assert {x[2] for x in e if x[0] == "imports"} == {"os/path.py#"}
    assert ("references", "x.py#", "os/path.py#join", "heuristic") in e
    only_top = {"os/__init__.py": _py(), "x.py": facts}
    assert {x[2] for x in _edges("x.py", facts, Idx(only_top)) if x[0] == "imports"} == {"os/__init__.py#"}


def test_longest_definition_name_first():
    files = {
        "m.py": _py(defs=[_def("Cap"), _def("Cap.hold", 2)]),
        "x.py": _py([Import("m", "Cap", "Cap", 0, 1)], defs=[_def("run")], refs=[Ref("Cap.hold", 3, "run"), Ref("Cap", 4, "run")]),
    }
    e = _edges("x.py", files["x.py"], Idx(files))
    assert ("references", "x.py#run", "m.py#Cap.hold", "heuristic") in e
    assert ("references", "x.py#run", "m.py#Cap", "heuristic") in e


def test_a_ref_to_a_definition_of_the_same_file():
    facts = _py(defs=[_def("a"), _def("b", 5)], refs=[Ref("a", 6, "b")])
    assert ("references", "m.py#b", "m.py#a", "heuristic") in _edges("m.py", facts, Idx({"m.py": facts}))


def test_a_recursive_ref_is_no_edge():
    facts = _py(defs=[_def("a")], refs=[Ref("a", 2, "a")])
    assert not [e for e in _edges("m.py", facts, Idx({"m.py": facts})) if e[0] == "references"]


def test_relative_imports_climb_the_directories():
    files = {"a/b/c.py": _py([Import("x", "", "x", 2, 1)]), "a/x.py": _py()}
    facts = files["a/b/c.py"]
    assert "path:a/x.py" in wanted("a/b/c.py", facts)
    assert ("imports", "a/b/c.py#", "a/x.py#", "exact") in _edges("a/b/c.py", facts, Idx(files))


def test_a_relative_import_from_the_package_itself():
    files = {"a/__init__.py": _py(), "a/c.py": _py([Import("", "", "", 1, 1)]), "a/d.py": _py([Import("", "d", "d", 1, 1)])}
    assert wanted("a/c.py", files["a/c.py"]) == {"path:a/__init__.py"}
    assert ("imports", "a/c.py#", "a/__init__.py#", "exact") in _edges("a/c.py", files["a/c.py"], Idx(files))
    assert "path:a/d.py" in wanted("a/d.py", files["a/d.py"])


@pytest.mark.parametrize("level", [3, 4, 10**30])
def test_a_level_past_the_root_wants_nothing(level):
    facts = _py([Import("x", "y", "y", level, 1)])
    assert wanted("a/c.py", facts) == frozenset()
    nodes, edges = resolve("r", "a/c.py", facts, Idx({"a/c.py": facts}))
    assert [e for e in edges if e.kind == "imports"] == []
    assert len(nodes) == 1


def test_a_relative_module_that_is_not_names_wants_nothing():
    assert wanted("a/c.py", _py([Import("x..y", "", "", 1, 1)])) == frozenset()
    assert wanted("a/c.py", _py([Import("x/y", "", "", 1, 1)])) == frozenset()


@pytest.mark.parametrize("module", ["/x", "a//b", "a/./b", "-", ".", "a..b"])
def test_odd_module_names_are_opaque_strings(module):
    facts = _py([Import(module, "n", "n", 0, 1), Import(module, "", "m", 0, 2)])
    assert all(key.startswith("py:") for key in wanted("a/c.py", facts))
    resolve("r", "a/c.py", facts, Idx({"a/c.py": facts}))


def test_script_imports():
    files = {
        "api/client.ts": _ts(defs=[_def("get")]),
        "helper.ts": _ts(defs=[_def("help")]),
        "lib/index.ts": _ts(defs=[_def("lib")]),
        "src/main.ts": _ts(
            [Import("../api/client", "get", "fetch", 0, 1), Import("../helper.js", "", "", 0, 2), Import("../lib", "*", "lib", 0, 3), Import("react", "x", "x", 0, 4)],
            refs=[Ref("fetch", 5, ""), Ref("lib.lib", 6, "")],
        ),
    }
    facts = files["src/main.ts"]
    w = wanted("src/main.ts", facts)
    assert {"path:api/client.ts", "path:helper.ts", "path:helper.tsx", "path:lib/index.js"} <= w
    assert not [k for k in w if "react" in k]
    e = _edges("src/main.ts", facts, Idx(files))
    assert {x[2] for x in e if x[0] == "imports"} == {"api/client.ts#", "helper.ts#", "lib/index.ts#"}
    assert ("references", "src/main.ts#", "api/client.ts#get", "heuristic") in e
    assert ("references", "src/main.ts#", "lib/index.ts#lib", "heuristic") in e


def test_the_spec_example_paths():
    files = {"api/client.ts": _ts(), "helper.ts": _ts(), "x.ts": _ts([Import("./api/client", "", "", 0, 1), Import("./helper.js", "", "", 0, 2)])}
    e = _edges("x.ts", files["x.ts"], Idx(files))
    assert {x[2] for x in e if x[0] == "imports"} == {"api/client.ts#", "helper.ts#"}


def test_binding_less_and_default_imports_still_import_and_bind_nothing():
    files = {"side.ts": _ts(defs=[_def("x")]), "m.ts": _ts(defs=[_def("x")]), "a.ts": _ts([Import("./side", "", "", 0, 1), Import("./m", "default", "x", 0, 2)], refs=[Ref("x", 3, "")])}
    e = _edges("a.ts", files["a.ts"], Idx(files))
    assert {x[2] for x in e if x[0] == "imports"} == {"side.ts#", "m.ts#"}
    assert not [x for x in e if x[0] == "references"]


def test_a_specifier_climbing_past_the_root_wants_nothing():
    assert wanted("a.ts", _ts([Import("../x", "", "", 0, 1)])) == frozenset()
    assert wanted("a/b.ts", _ts([Import("../../x", "", "", 0, 1)])) == frozenset()
    assert wanted("a/b.ts", _ts([Import("./..", "", "", 0, 1)])) == {"path:index.ts", "path:index.tsx", "path:index.js"}


def test_a_root_specifier_dot_wants_the_index_files():
    assert wanted("a.ts", _ts([Import(".", "", "", 0, 1)])) == {"path:index.ts", "path:index.tsx", "path:index.js"}


def test_a_link_out_of_the_repository_wants_nothing():
    facts = Facts(lang="markdown", links=(Link("../../outside.md", 1),))
    assert wanted("docs/a.md", facts) == frozenset()
    assert wanted("docs/a.md", Facts(lang="markdown", links=(Link("../b.md", 1),))) == {"path:b.md"}


def test_markdown_nodes_and_edges():
    facts = Facts(
        lang="markdown",
        doc="A",
        sections=(
            Section("A", 1, 1, 5, "section", "intro"),
            Section("Use x", 2, 2, 3, "decision", "because"),
            Section("A", 2, 4, 5, "section", "again"),
        ),
        links=(Link("b.md", 2),),
    )
    other = Facts(lang="markdown", doc="B")
    nodes, edges = resolve("r", "docs/a.md", facts, Idx({"docs/a.md": facts, "docs/b.md": other}))
    by_id = {n.id: n for n in nodes}
    assert by_id["r:doc:docs/a.md#"].trust == "human" and by_id["r:doc:docs/a.md#"].text == "A"
    assert by_id["r:decision:docs/a.md#Use x"].trust == "human"
    assert by_id["r:section:docs/a.md#A"].text == "intro"
    assert by_id["r:section:docs/a.md#A:4"].text == "again"
    assert {(e.kind, e.dst) for e in edges if e.kind == "mentions"} == {("mentions", "r:doc:docs/b.md#")}
    assert len([e for e in edges if e.kind == "contains"]) == 3


def test_a_link_to_an_unparsed_file_is_a_file_node():
    facts = Facts(lang="markdown", links=(Link("x.bin", 1),))
    _, edges = resolve("r", "a.md", facts, Idx({"a.md": facts}, unparsed=("x.bin",)))
    assert [e.dst for e in edges] == ["r:file:x.bin#"]


def test_manifest_nodes_and_edges():
    facts = extract_manifest("pyproject.toml", b'[project]\nname = "demo"\ndependencies = ["requests>=2", "rich"]\n')
    nodes, edges = resolve("r", "pyproject.toml", facts, Idx({"pyproject.toml": facts}))
    assert {(n.kind, n.name) for n in nodes} == {
        ("file", "pyproject.toml"), ("package", "demo"), ("provides", "pkg:pypi/demo"), ("needs", "pkg:pypi/requests"), ("needs", "pkg:pypi/rich"),
    }
    assert {(e.kind, e.src, e.confidence) for e in edges} == {("depends_on", "r:package:pyproject.toml#demo", "exact")}
    assert len(edges) == 2


def test_an_unnamed_manifest_depends_from_its_file():
    facts = extract_manifest("requirements.txt", b"requests\n")
    _, edges = resolve("r", "requirements.txt", facts, Idx({"requirements.txt": facts}))
    assert [(e.src, e.dst) for e in edges] == [("r:file:requirements.txt#", "r:needs:requirements.txt#pkg:pypi/requests")]


def test_duplicate_definitions_give_one_node_the_first():
    facts = _py(defs=[Def("f", "function", 9, 9, "def f(b)", "", True), Def("f", "function", 3, 3, "def f(a)", "", True)])
    nodes, edges = resolve("r", "m.py", facts, Idx({"m.py": facts}))
    symbols = [n for n in nodes if n.kind == "symbol"]
    assert [(n.line, n.signature) for n in symbols] == [(3, "def f(a)")]
    assert len([e for e in edges if e.kind == "contains"]) == 1


def test_edges_are_unique_per_src_dst_kind():
    files = {"m.py": _py(defs=[_def("f")]), "x.py": _py([Import("m", "f", "f", 0, 1), Import("m", "f", "g", 0, 2)], refs=[Ref("f", 3, ""), Ref("g", 4, "")])}
    _, edges = resolve("r", "x.py", files["x.py"], Idx(files))
    keys = [(e.src, e.dst, e.kind) for e in edges]
    assert len(keys) == len(set(keys))
    assert len([e for e in edges if e.kind == "imports"]) == 1


def test_every_edge_has_the_path_as_origin_and_the_result_is_deterministic():
    for path in ("pkg/cap.py", "tests/test_cap.py", "docs/a.md"):
        first = resolve("r", path, F[path], IDX)
        assert all(e.origin == path for e in first[1])
        assert first == resolve("r", path, F[path], IDX)
        assert first[0] == sorted(first[0], key=lambda n: n.id)


def test_a_binding_less_ref_scope_that_is_unknown_hangs_from_the_file():
    files = {"m.py": _py(defs=[_def("f")]), "x.py": _py([Import("m", "f", "f", 0, 1)], refs=[Ref("f", 2, "ghost")])}
    assert ("references", "x.py#", "m.py#f", "heuristic") in _edges("x.py", files["x.py"], Idx(files))


def test_file_node():
    node = file_node("r", "src/a.bin", None)
    assert (node.id, node.kind, node.name, node.path, node.line, node.end, node.trust, node.text) == ("r:file:src/a.bin#", "file", "a.bin", "src/a.bin", 1, 1, "code", "")
    doc = file_node("r", "a.md", "markdown")
    assert (doc.id, doc.kind, doc.trust) == ("r:doc:a.md#", "doc", "human")


def test_a_parsed_file_node_matches_file_node_but_for_its_text():
    nodes, _ = resolve("r", "pkg/slots.py", F["pkg/slots.py"], IDX)
    assert next(n for n in nodes if n.kind == "file") == file_node("r", "pkg/slots.py", "python")


def test_resolve_only_asks_for_keys_it_wants():
    class Spy(Idx):
        asked: set[str] = set()

        def providers(self, key: str) -> list[str]:
            self.asked.add(key)
            return super().providers(key)

    spy = Spy(F)
    for path in ("pkg/cap.py", "tests/test_cap.py", "docs/a.md"):
        spy.asked = set()
        resolve("r", path, F[path], spy)
        assert spy.asked <= wanted(path, F[path])
