"""graph_model: the kinds, the facts, and the check that distrusts facts from a parser child process."""

from __future__ import annotations

import copy
import json

import pytest

from agentflow import graph_model as gm


def _full() -> gm.Facts:
    return gm.Facts(
        lang="python",
        doc="A module.",
        defs=(
            gm.Def("hold", "function", 3, 9, "def hold(x)", "Hold it.", True),
            gm.Def("Cap.hold", "method", 5, 8, "def hold(self)", "", False),
        ),
        imports=(
            gm.Import("os.path", "join", "j", 0, 1),
            gm.Import("", "x", "", 1, 2),
            gm.Import("a.b", "", "a", 0, 2),
            gm.Import("m", "*", "", 0, 2),
        ),
        refs=(gm.Ref("os.path.join", 4, "hold"), gm.Ref("print", 5, "")),
        sections=(gm.Section("Intro", 1, 1, 10, "section", "Some text."),),
        links=(gm.Link("../plans/p.md", 6),),
        deps=(gm.Dep("pyyaml", ">=6", "runtime"),),
        package="agentflow",
        ecosystem="pypi",
        status="approved",
        notes=("one note",),
    )


def _raw(**over) -> dict:
    raw = gm.to_json(_full())
    raw.update(over)
    return raw


def test_round_trip_through_json():
    facts = _full()
    wire = json.loads(json.dumps(gm.to_json(facts)))
    assert gm.check_facts(wire, "python", 10) == facts


def test_empty_round_trips_and_keeps_notes():
    facts = gm.empty("markdown", "a note", "b\nnote")
    assert facts.notes == ("a note", "b note")
    assert gm.check_facts(gm.to_json(facts), "markdown", 0) == facts


def test_empty_caps_notes():
    assert len(gm.empty("python", *[str(i) for i in range(50)]).notes) == gm.MAX_NOTES


def test_to_json_is_plain_json_data():
    wire = gm.to_json(_full())
    assert json.loads(json.dumps(wire)) == wire
    assert isinstance(wire["defs"], list) and isinstance(wire["defs"][0], dict)


@pytest.mark.parametrize(
    "raw",
    [
        None,
        [],
        "x",
        {},
        _raw(defs="not a list"),
        {**_raw(), "extra": 1},
        _raw(defs=[gm.to_json(_full())["defs"][0]] * (gm.MAX_DEFS + 1)),
        _raw(imports=[{"module": "a", "name": "", "alias": "", "level": 0, "line": 1}] * (gm.MAX_IMPORTS + 1)),
        _raw(refs=[{"name": "a", "line": 1, "scope": ""}] * (gm.MAX_REFS + 1)),
        _raw(sections=[{"title": "t", "level": 1, "line": 1, "end": 1, "kind": "section", "text": ""}] * (gm.MAX_SECTIONS + 1)),
        _raw(links=[{"target": "a.md", "line": 1}] * (gm.MAX_LINKS + 1)),
        _raw(deps=[{"name": "a", "spec": "", "group": "dev"}] * (gm.MAX_DEPS + 1)),
        _raw(lang="markdown"),
        _raw(ecosystem="cargo"),
        _raw(package="a b"),
        _raw(package="../x"),
        _raw(notes="a note"),
    ],
)
def test_refuses_a_malformed_whole(raw):
    assert gm.check_facts(raw, "python", 10) is None


def test_refuses_a_missing_top_level_key():
    raw = _raw()
    del raw["notes"]
    assert gm.check_facts(raw, "python", 10) is None


def test_lang_asked_for_must_be_known_and_equal():
    assert gm.check_facts(_raw(), "markdown", 10) is None
    assert gm.check_facts(_raw(lang="cobol"), "cobol", 10) is None


def _with_def(**over) -> dict:
    d = {"name": "f", "kind": "function", "line": 1, "end": 2, "signature": "", "doc": "", "exported": True}
    d.update(over)
    return _raw(defs=[d])


@pytest.mark.parametrize("name", ["a b", "a\nb", "é", "a" * (gm.NAME_MAX + 1), "", "1a", "a-b", 5, None])
def test_refuses_a_def_name(name):
    assert gm.check_facts(_with_def(name=name), "python", 10) is None


def test_accepts_a_def_name_of_the_limit_and_dotted_and_dollar():
    for name in ("a" * gm.NAME_MAX, "Cap.hold", "$el", "_x1"):
        assert gm.check_facts(_with_def(name=name), "python", 10) is not None


@pytest.mark.parametrize(
    "over",
    [
        {"line": 0},
        {"line": 11, "end": 11},
        {"end": 11},
        {"line": 3, "end": 2},
        {"line": True},
        {"end": True},
        {"line": 1.0},
        {"line": "1"},
        {"exported": 1},
        {"exported": "yes"},
        {"kind": "module"},
        {"kind": ["function"]},
        {"extra": 1},
    ],
)
def test_refuses_a_bad_def_field(over):
    assert gm.check_facts(_with_def(**over), "python", 10) is None


def test_def_may_end_on_the_last_line():
    assert gm.check_facts(_with_def(line=10, end=10), "python", 10) is not None


def test_a_missing_record_key_refuses():
    d = {"name": "f", "kind": "function", "line": 1, "end": 2, "signature": "", "doc": ""}
    assert gm.check_facts(_raw(defs=[d]), "python", 10) is None


def test_def_free_text_is_cleaned_and_cut_not_refused():
    facts = gm.check_facts(_with_def(signature="def f(\x1b[0m\n)" + "x" * 500, doc="d\r\ne" + "y" * 500), "python", 10)
    assert facts is not None
    d = facts.defs[0]
    assert d.signature.startswith("def f( [0m )") and len(d.signature) == gm.SIGNATURE_MAX
    assert d.doc.startswith("d  e") and len(d.doc) == gm.DOC_MAX
    assert not any(ord(c) < 32 for c in d.signature + d.doc)


def test_non_text_free_text_becomes_empty():
    facts = gm.check_facts(_with_def(signature=7, doc=None), "python", 10)
    assert facts is not None and facts.defs[0].signature == "" and facts.defs[0].doc == ""


def _with_import(**over) -> dict:
    i = {"module": "a.b", "name": "c", "alias": "", "level": 0, "line": 1}
    i.update(over)
    return {"imports": [i]}


def test_python_import_module_with_dotdot_segment_is_refused():
    assert gm.check_facts(_raw(**_with_import(module="../x")), "python", 10) is None
    assert gm.check_facts(_raw(**_with_import(module="a/../b")), "python", 10) is None


def test_python_relative_import_may_have_an_empty_module_only_with_level():
    assert gm.check_facts(_raw(**_with_import(module="", level=1)), "python", 10) is not None
    assert gm.check_facts(_raw(**_with_import(module="", level=0)), "python", 10) is None


def _js(lang: str, **over) -> gm.Facts | None:
    raw = gm.to_json(gm.empty(lang))
    raw.update(_with_import(**over))
    return gm.check_facts(raw, lang, 10)


@pytest.mark.parametrize("lang", ["typescript", "tsx", "javascript"])
@pytest.mark.parametrize("spec", ["../x", "../types", "./api/client", "react", "@acme/web", "../../a/b", ".", ".."])
def test_js_specifier_is_accepted(lang, spec):
    assert _js(lang, module=spec) is not None


@pytest.mark.parametrize("spec", ["a/../b", "./a/../b", "", "/etc/passwd", "a b", "é", "../" * 100, "a/./b", "x" * 201, "a\nb"])
def test_js_specifier_is_refused(spec):
    assert _js("typescript", module=spec) is None


def test_markdown_import_uses_module_rule():
    raw = gm.to_json(gm.empty("markdown"))
    raw.update(_with_import(module="../x"))
    assert gm.check_facts(raw, "markdown", 10) is None


@pytest.mark.parametrize(
    "over",
    [
        {"name": "a b"},
        {"name": "é"},
        {"alias": "a b"},
        {"level": -1},
        {"level": True},
        {"level": 1.5},
        {"level": "1"},
        {"line": 0},
        {"line": 11},
        {"line": False},
    ],
)
def test_refuses_a_bad_import_field(over):
    assert gm.check_facts(_raw(**_with_import(**over)), "python", 10) is None


def test_import_name_may_be_empty_or_star():
    assert gm.check_facts(_raw(**_with_import(name="")), "python", 10) is not None
    assert gm.check_facts(_raw(**_with_import(name="*")), "python", 10) is not None
    assert gm.check_facts(_raw(**_with_import(name="**")), "python", 10) is None


@pytest.mark.parametrize(
    "ref",
    [
        {"name": "a b", "line": 1, "scope": ""},
        {"name": "a", "line": 0, "scope": ""},
        {"name": "a", "line": 11, "scope": ""},
        {"name": "a", "line": 1, "scope": "a b"},
        {"name": "a", "line": 1, "scope": None},
        {"name": "", "line": 1, "scope": ""},
        {"name": "a", "line": True, "scope": ""},
    ],
)
def test_refuses_a_bad_ref(ref):
    assert gm.check_facts(_raw(refs=[ref]), "python", 10) is None


@pytest.mark.parametrize(
    "over",
    [
        {"kind": "chapter"},
        {"level": 7},
        {"level": -1},
        {"level": True},
        {"line": 0},
        {"end": 11},
        {"line": 5, "end": 4},
    ],
)
def test_refuses_a_bad_section(over):
    s = {"title": "t", "level": 1, "line": 1, "end": 2, "kind": "section", "text": ""}
    s.update(over)
    assert gm.check_facts(_raw(sections=[s]), "python", 10) is None


def test_section_text_is_cleaned_and_cut_not_refused():
    s = {"title": "T\n" + "t" * 500, "level": 0, "line": 1, "end": 2, "kind": "decision", "text": "a\nb" * 5000}
    facts = gm.check_facts(_raw(sections=[s]), "python", 10)
    assert facts is not None
    got = facts.sections[0]
    assert len(got.title) == gm.NAME_MAX and "\n" not in got.title
    assert len(got.text) == gm.SECTION_TEXT_MAX and "\n" not in got.text


@pytest.mark.parametrize("target", ["../plans/p.md", "./a.md", "docs/a.md", "a.md"])
def test_link_target_relative_paths_pass(target):
    assert gm.check_facts(_raw(links=[{"target": target, "line": 1}]), "python", 10) is not None


@pytest.mark.parametrize("target", ["a/../b.md", "/abs.md", "a b.md", "", "é.md", None, 3])
def test_link_target_bad_values_fail(target):
    assert gm.check_facts(_raw(links=[{"target": target, "line": 1}]), "python", 10) is None


@pytest.mark.parametrize(
    "dep",
    [
        {"name": "a b", "spec": "", "group": "dev"},
        {"name": "../x", "spec": "", "group": "dev"},
        {"name": "a", "spec": "", "group": "test"},
        {"name": "", "spec": "", "group": "dev"},
    ],
)
def test_refuses_a_bad_dep(dep):
    assert gm.check_facts(_raw(deps=[dep]), "python", 10) is None


def test_dep_spec_is_free_text():
    facts = gm.check_facts(_raw(deps=[{"name": "@acme/web", "spec": ">=1\n<2", "group": "optional"}]), "python", 10)
    assert facts is not None and facts.deps[0].spec == ">=1 <2"


def test_notes_are_cut_not_refused_and_cleaned():
    notes = ["n\n%d" % i for i in range(gm.MAX_NOTES + 10)] + ["", 5]
    facts = gm.check_facts(_raw(notes=notes), "python", 10)
    assert facts is not None and len(facts.notes) == gm.MAX_NOTES and facts.notes[0] == "n 0"


def test_status_and_doc_are_cleaned():
    facts = gm.check_facts(_raw(status="ap\x00proved", doc="d\x85" + "x" * 300), "python", 10)
    assert facts is not None and facts.status == "ap proved" and len(facts.doc) == gm.DOC_MAX


def test_lines_must_be_an_int():
    assert gm.check_facts(_raw(), "python", True) is None
    assert gm.check_facts(_raw(), "python", "10") is None


def test_check_does_not_mutate_its_input():
    raw = _raw()
    before = copy.deepcopy(raw)
    gm.check_facts(raw, "python", 10)
    assert raw == before


def test_text_replaces_controls_and_breaks_and_cuts():
    assert gm.text("a\x1b[31mb\nc", 10) == "a [31mb c"
    assert gm.text("abcdef", 3) == "abc"
    assert gm.text("a\x7fb\x85c d\re", 20) == "a b c d e"
    assert gm.text(None, 5) == "" and gm.text(5, 5) == ""
    assert gm.text("é", 5) == "é"


def test_ident():
    assert gm.ident("a.b_c$") == "a.b_c$"
    assert gm.ident("$x") == "$x"
    for bad in ("", "1a", "a b", "a\n", "é", "a" * 201, None, 3, ".a", "a-b"):
        assert gm.ident(bad) is None
    assert gm.ident("a" * 200) is not None


def test_module():
    for good in ("os.path", "@acme/web", "a-b/c_d", "./x", "x" * 200, "...", "a..b"):
        assert gm.module(good) == good
    for bad in ("", "../x", "a/../b", "..", "a b", "é", "x" * 201, "a\n", None, 4):
        assert gm.module(bad) is None


def test_specifier():
    for good in ("../types", "./api/client", "react", "@acme/web", "..", ".", "../../a"):
        assert gm.specifier(good) == good
    for bad in ("a/../b", "/etc/x", "", "a b", "x" * 201, None, "./a/./b", "a/.."):
        assert gm.specifier(bad) is None


def test_node_id():
    assert gm.node_id("r", "file", "pkg/cap.py") == "r:file:pkg/cap.py#"
    assert gm.node_id("r", "symbol", "pkg/cap.py", "hold") == "r:symbol:pkg/cap.py#hold"


def test_node_and_edge_are_frozen_records():
    node = gm.Node("r:file:a#", "file", "a", "a", 1, 1, "extracted", "", "")
    edge = gm.Edge("a", "b", "imports", "high", "a.py")
    with pytest.raises(AttributeError):
        node.id = "x"  # type: ignore[misc]
    assert edge.origin == "a.py"


def test_constants():
    assert gm.SCHEMA_VERSION == 1
    assert set(gm.EXTRACTOR_VERSIONS) == set(gm.LANGUAGES)
    assert "file" in gm.NODE_KINDS and "imports" in gm.EDGE_KINDS
