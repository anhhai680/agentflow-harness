"""graph_python: definitions, imports and references from `ast`, and facts `check_facts` always accepts."""

from __future__ import annotations

from agentflow import graph_model as gm
from agentflow.graph_python import extract

SRC = b'''"""Run cap: hold a run until a slot frees.

More text."""
import os.path
import a.b as ab
from . import sibling
from ..pkg.mod import helper as h, Other
LIMIT = 64
_private = 1

class Cap(Base):
    """Holds one slot."""
    def hold(self, run: "Run", *, wait: float = 1.0) -> bool:
        return h(run) and ab.go(os.path.join("a"))
    def _free(self): ...

async def main(argv: list[str] | None = None) -> int:
    def inner(): return Other()
    return Cap().hold(None)
'''


def _roundtrip(data: bytes) -> gm.Facts:
    """The facts for `data`, proven to pass `check_facts` unchanged."""
    facts = extract("pkg/mod.py", data)
    checked = gm.check_facts(gm.to_json(facts), "python", data.count(b"\n") + 1)
    assert checked == facts
    return facts


def test_definitions():
    f = extract("pkg/run_cap.py", SRC)
    assert f.doc == "Run cap: hold a run until a slot frees."
    assert [(d.name, d.kind, d.exported) for d in f.defs] == [
        ("LIMIT", "constant", True),
        ("Cap", "class", True),
        ("Cap.hold", "method", True),
        ("Cap._free", "method", False),
        ("main", "function", True),
    ]
    hold = f.defs[2]
    assert hold.signature == "def hold(self, run: 'Run', *, wait: float=...) -> bool"  # R22: no default's value
    assert (hold.line, hold.end) == (13, 14) and f.defs[1].doc == "Holds one slot."
    assert f.defs[1].signature == "class Cap(Base)"
    assert f.defs[4].signature == "async def main(argv: list[str] | None=...) -> int"
    assert f.defs[4].doc == ""


CANARY = "zqxcanary7731"


def test_no_default_or_class_keyword_value_reaches_a_signature():
    """A default can hold a secret, as a constant's value can (R13): every one is written `...`."""
    src = f'''class Conf(Base, metaclass=Meta, token="{CANARY}", **extra):
    def connect(self, url="https://u:{CANARY}@h", /, port=5432, *args, key=b"{CANARY}", retries, **kw) -> None:
        ...
def call(fn=lambda: "{CANARY}", data={{"k": "{CANARY}"}}): ...
async def later(*, wait: float = len("{CANARY}")): ...
class Inline(**{{"token": "{CANARY}"}}): ...
'''.encode()
    defs = {d.name: d.signature for d in _roundtrip(src).defs}
    assert CANARY not in repr(defs)
    assert defs == {
        "Conf": "class Conf(Base, metaclass=..., token=..., **extra)",
        "Conf.connect": "def connect(self, url=..., /, port=..., *args, key=..., retries, **kw) -> None",
        "call": "def call(fn=..., data=...)",
        "later": "async def later(*, wait: float=...)",
        "Inline": "class Inline(**...)",
    }


def test_imports():
    assert [(i.module, i.name, i.alias, i.level) for i in extract("pkg/run_cap.py", SRC).imports] == [
        ("os.path", "", "os", 0),
        ("a.b", "", "ab", 0),
        ("", "sibling", "sibling", 1),
        ("pkg.mod", "helper", "h", 2),
        ("pkg.mod", "Other", "Other", 2),
    ]


def test_references():
    refs = {(r.name, r.scope) for r in extract("pkg/run_cap.py", SRC).refs}
    assert refs == {
        ("h", "Cap.hold"),
        ("ab.go", "Cap.hold"),
        ("os.path.join", "Cap.hold"),
        ("Other", "main"),
        ("Cap", "main"),
    }  # `Base` is neither imported nor defined here: no Ref


def test_ref_keeps_the_first_line_once_per_name_and_scope():
    src = b"import os\n\ndef f():\n    pass\n\ndef g():\n    x = 1\n    os.getcwd()\n    return os.getcwd()\n"
    refs = extract("m.py", src).refs
    assert [(r.name, r.scope, r.line) for r in refs] == [("os.getcwd", "g", 8)]


def test_a_store_or_a_call_base_still_reaches_the_import():
    src = b"import ab\nimport cd\nab.x = 1\ncd.make().go()\n"
    assert {r.name for r in extract("m.py", src).refs} == {"ab", "cd.make"}


def test_star_import_and_plain_dotted_import():
    f = extract("m.py", b"from x.y import *\nimport p.q\n")
    assert [(i.module, i.name, i.alias, i.level) for i in f.imports] == [("x.y", "*", "", 0), ("p.q", "", "p", 0)]


def test_nothing_nested_deeper_is_a_definition():
    src = b"class A:\n    class B:\n        def deep(self): ...\n    x = 1\n    X = 2\n\nif True:\n    def cond(): ...\n"
    assert [d.name for d in extract("m.py", src).defs] == ["A"]


def test_a_decorated_function_starts_at_its_def_line():
    f = extract("m.py", b"@dec\n@other\ndef f(x):\n    return x\n")
    assert (f.defs[0].line, f.defs[0].end) == (3, 4)


def test_syntax_error_gives_an_empty_facts_with_a_note():
    assert extract("m.py", b"def f(:\n") == gm.empty("python", "SyntaxError")


def test_deep_nesting_gives_a_note_and_no_exception():
    f = _roundtrip(b"x = " + b"(" * 3000 + b")" * 3000 + b"\n")
    assert f.notes and not f.defs


def test_a_deep_operator_chain_does_not_crash():
    f = _roundtrip(b"X = " + b"-" * 4000 + b"1\n")
    assert f.lang == "python"


def test_bytes_that_are_not_utf8_give_a_note():
    f = _roundtrip(b"# \xff\xfe\ndef f(): ...\n")
    assert f.notes and not f.defs


def test_a_null_byte_gives_a_note():
    assert _roundtrip(b"x = 1\x00\n").notes


def test_a_name_outside_ascii_is_dropped_and_counted_once():
    f = _roundtrip("def café(): ...\ndef ok(): ...\ndef naïve(): ...\n".encode())
    assert [d.name for d in f.defs] == ["ok"]
    assert len(f.notes) == 1 and "2" in f.notes[0]


def test_non_ascii_imports_and_references_are_dropped():
    src = "import café\nimport os\nfrom os import é\nos.pâth\nx = os.sep\n".encode()
    f = _roundtrip(src)
    assert [(i.module, i.alias) for i in f.imports] == [("os", "os")]
    assert {r.name for r in f.refs} == {"os.sep"}
    assert len(f.notes) >= 2


def test_6000_functions_are_cut_to_max_defs_with_a_note():
    src = b"".join(b"def f%d(): ...\n" % i for i in range(6000))
    f = _roundtrip(src)
    assert len(f.defs) == gm.MAX_DEFS and f.defs[0].name == "f0"
    assert any("definitions" in n for n in f.notes)


def test_imports_and_refs_are_cut_to_their_caps_with_a_note():
    imports = b"".join(b"import m%d\n" % i for i in range(gm.MAX_IMPORTS + 5))
    f = _roundtrip(imports)
    assert len(f.imports) == gm.MAX_IMPORTS and any("imports" in n for n in f.notes)
    refs = b"import os\ndef f():\n" + b"".join(b"    os.a%d\n" % i for i in range(gm.MAX_REFS + 5))
    g = _roundtrip(refs)
    assert len(g.refs) == gm.MAX_REFS and any("references" in n for n in g.notes)


def test_a_very_long_name_is_dropped():
    long = b"x" * 300
    f = _roundtrip(b"def " + long + b"(): ...\ndef ok(): ...\n")
    assert [d.name for d in f.defs] == ["ok"] and f.notes


def test_cr_only_line_endings_stay_inside_the_files_line_count():
    f = _roundtrip(b"def f():\r    return 1\rdef g():\r    return 2\r")
    assert all(1 <= d.line <= d.end for d in f.defs)


def test_the_source_is_never_executed():
    f = _roundtrip(b"import sys\nraise SystemExit(3)\n1 / 0\ndef f(): ...\n")
    assert [d.name for d in f.defs] == ["f"]


def test_free_text_is_clean_and_short():
    src = b'def f(a="x\\ny"):\n    """first\\x0bline\n    second"""\n'
    f = _roundtrip(src)
    assert "\n" not in f.defs[0].signature and len(f.defs[0].signature) <= gm.SIGNATURE_MAX
    assert f.defs[0].doc == "first line"


def test_every_edge_case_and_the_brief_source_round_trip():
    _roundtrip(SRC)
    _roundtrip(b"")
    _roundtrip(b"def f(:\n")
    _roundtrip(b"\n\n\n")
    _roundtrip(b'"""only a docstring"""')
    _roundtrip(b"X: int = 3\nclass C(metaclass=M, k=1): ...\nasync def a(): ...\n")


def test_the_module_imports_only_the_standard_library_and_graph_model():
    import agentflow.graph_python as gp

    with open(gp.__file__, encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    assert not any("yaml" in line for line in lines)
    engine = [line for line in lines if line.startswith(("from agentflow", "import agentflow"))]
    assert engine == ["from agentflow.graph_model import ("]
