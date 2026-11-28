"""graph_syntax: TypeScript, TSX and JavaScript facts from tree-sitter, in the locked worker and never in the engine."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

import pytest

from agentflow import graph_model as gm
from agentflow import graph_parse, graph_syntax
from agentflow.graph_parse import Parser


@pytest.fixture(scope="module")
def exact():
    p = Parser(download=bool(os.environ.get("CI")))  # CI may download; a developer machine uses its cache
    if "typescript" not in p.languages:
        p.close()
        if os.environ.get("CI"):
            pytest.fail("the locked grammars did not start in CI")
        pytest.skip("the tree-sitter grammars are not in uv's cache: run `agentflow graph build` once with network access")
    yield p
    p.close()


TS = b'''import React, { useState as useS } from "react";
import * as api from "./api/client";
import type { User } from "../types";
export { helper } from "./helper.js";
const limit = 5;
/** Loads one user. */
export async function loadUser(id: string): Promise<User> { return api.get(id); }
export const format = (u: User): string => String(u);
export default class Store<T> implements Base { private items: T[] = []; add(item: T): void { useS(item); } }
export interface Base { add(item: unknown): void }
'''

TSX = b'''import { useState } from "react";
export function Counter({ start }: { start: number }) {
  const [n, setN] = useState(start);
  return <button onClick={() => setN(n + 1)}>{n}</button>;
}
'''

JS = b'''const x = require("./x");
function run() { return x.go(); }
module.exports = { run };
'''


def test_typescript(exact):
    f = exact.parse("web/store.ts", "typescript", TS)
    assert [(d.name, d.kind, d.exported) for d in f.defs] == [
        ("limit", "constant", False), ("loadUser", "function", True), ("format", "function", True),
        ("Store", "class", True), ("Store.add", "method", True), ("Base", "class", True)]
    assert f.defs[1].doc == "Loads one user." and f.defs[1].signature == "export async function loadUser(id: string): Promise<User>"
    assert [(i.module, i.name, i.alias) for i in f.imports] == [
        ("react", "default", "React"), ("react", "useState", "useS"), ("./api/client", "*", "api"),
        ("../types", "User", "User"), ("./helper.js", "helper", "helper")]
    assert {(r.name, r.scope) for r in f.refs} >= {("api.get", "loadUser"), ("useS", "Store.add")}
    assert all(i.level == 0 for i in f.imports) and "syntax_error" not in f.notes


def test_tsx_component_parses_cleanly(exact):
    f = exact.parse("web/counter.tsx", "tsx", TSX)
    assert "syntax_error" not in f.notes
    assert [(d.name, d.kind, d.exported) for d in f.defs] == [("Counter", "function", True)]
    assert ("useState", "Counter") in {(r.name, r.scope) for r in f.refs}


def test_javascript_require_and_module_exports(exact):
    f = exact.parse("lib/run.js", "javascript", JS)
    assert [(i.module, i.name, i.alias) for i in f.imports] == [("./x", "default", "x")]
    assert [(d.name, d.kind, d.exported) for d in f.defs] == [("run", "function", True)]
    assert ("x.go", "run") in {(r.name, r.scope) for r in f.refs}


EXPORTS = b'''export default function () { return helper(); }
function helper() {}
export { helper };
export * from "./m";
export * as ns from "./n";
import x = require("y");
import "side";
class K { #p() {} private q() {} r() { x.s.t(); new K(); } }
export default K;
import { "a-b" as q } from "z";
import w from "a/../b";
'''


def test_export_forms_private_methods_and_dropped_records(exact):
    f = exact.parse("web/exports.ts", "typescript", EXPORTS)
    assert [(d.name, d.kind, d.exported) for d in f.defs] == [
        ("default", "function", True), ("helper", "function", True), ("K", "class", True),
        ("K.q", "method", False), ("K.r", "method", True)]
    assert [(i.module, i.name, i.alias) for i in f.imports] == [
        ("./m", "*", ""), ("./n", "*", "ns"), ("y", "default", "x"), ("side", "", "")]
    assert {(r.name, r.scope) for r in f.refs} == {("helper", "default"), ("x.s.t", "K.r"), ("K", "K.r")}
    assert f.notes == ("dropped 1 definitions with names outside [A-Za-z0-9_$.]",
                       "dropped 2 imports with names outside [A-Za-z0-9_$.]")


CANARY = "zqxcanary7731"
DEFAULTS = f'''export function connect(url: string = "https://u:{CANARY}@h", port = 5432, {{ key = "{CANARY}" }} = {{}}, ...rest: string[]): void {{ }}
export const call = (fn = () => "{CANARY}", n?: number) => fn();
export class Conf {{ open(token: string = `{CANARY}`): void {{ }} }}
'''.encode()


@pytest.mark.parametrize("lang", ["typescript", "tsx"])
def test_no_parameter_initialiser_reaches_a_signature(exact, lang):
    """A default can hold a secret, as a constant's value can (R13): each `= initialiser` is written `= ...`."""
    defs = {d.name: d.signature for d in exact.parse("web/conf.ts", lang, DEFAULTS).defs}
    assert CANARY not in repr(defs)
    assert defs == {
        "connect": "export function connect(url: string = ..., port = ..., { key = ... } = ..., ...rest: string[]): void",
        "call": "export const call = (fn = ..., n?: number)",
        "Conf": "export class Conf",
        "Conf.open": "open(token: string = ...): void",
    }


def test_no_parameter_initialiser_reaches_a_javascript_signature(exact):
    src = f'''function connect(url = "{CANARY}", {{ key = "{CANARY}" }} = {{}}, [a = 1] = []) {{ }}
const call = function (fn = () => "{CANARY}") {{ return fn(); }};
'''.encode()
    defs = {d.name: d.signature for d in exact.parse("web/conf.js", "javascript", src).defs}
    assert CANARY not in repr(defs)
    assert defs == {"connect": "function connect(url = ..., { key = ... } = ..., [a = ...] = ...)",
                    "call": "const call = function (fn = ...)"}


def test_a_broken_file_keeps_what_was_found(exact):
    f = exact.parse("web/broken.ts", "typescript", b'import { a } from "./a";\nexport const a = 1,\n')
    assert "syntax_error" in f.notes
    assert [(i.module, i.name) for i in f.imports] == [("./a", "a")]


def test_the_locked_worker_offers_the_pinned_grammars(exact):
    assert exact.mode in ("offline", "locked")
    assert exact.languages["typescript"] == "2+0.23.2"
    assert exact.languages["tsx"] == "2+0.23.2" and exact.languages["javascript"] == "2+0.25.0"
    assert {"python", "markdown", "manifest"} <= set(exact.languages)


def test_the_script_lock_is_used_inside_a_project_of_its_own(exact, tmp_path):
    # uv reads the script's header and `graph_worker.py.lock`, never a project file or `uv.lock` around the cwd
    project = tmp_path / "other"
    project.mkdir()
    (project / "pyproject.toml").write_text('[project]\nname = "other"\nversion = "0"\ndependencies = ["requests"]\n')
    out = subprocess.run(graph_parse.worker_argv(exact.mode), input=b"", capture_output=True, cwd=project,
                         env=graph_parse.worker_env(), timeout=120)
    assert json.loads(out.stdout.splitlines()[0])["languages"]["typescript"] == "2+0.23.2"
    assert sorted(p.name for p in project.iterdir()) == ["pyproject.toml"]


# --- hostile files: every node is read a constant number of times, and nothing raises ---

N = 10_000
LINEAR_S = 2.0  # a quadratic walk takes minutes at N; a linear one, a few hundredths of a second


def _timed(parser, lang: str, data: bytes) -> tuple[gm.Facts, float]:
    t0 = time.monotonic()
    facts = parser.parse("web/hostile.js", lang, data)
    return facts, time.monotonic() - t0


def test_a_long_member_chain_on_a_call_is_linear(exact):
    f, took = _timed(exact, "javascript", b'import g from "g";\ng()' + b".a" * N + b";\nimport api from \"api\";\napi" + b".b" * N + b";\n")
    assert took < LINEAR_S, took
    assert ("g", "") in {(r.name, r.scope) for r in f.refs}
    assert "dropped 1 references with names outside [A-Za-z0-9_$.]" in f.notes  # `api.b.b...` is past NAME_MAX


def test_a_long_member_chain_on_this_is_linear(exact):
    f, took = _timed(exact, "typescript", b'import api from "api";\nclass K { m() { this' + b".a" * N + b"; api.go(); } }\n")
    assert took < LINEAR_S, took
    assert {(r.name, r.scope) for r in f.refs} == {("api.go", "K.m")}


def test_require_in_deeply_nested_parentheses_is_linear(exact):
    data = b"(require('x'), " * N + b"0" + b")" * N + b";\nconst y = require('y');\n"
    f, took = _timed(exact, "javascript", data)
    assert took < LINEAR_S, took
    assert f.imports[0] == gm.Import("x", "", "", 0, 1) and "cut imports from 10001 to 2000" in f.notes
    assert [d.name for d in f.defs] == []  # `y` binds an import, not a constant


@pytest.mark.parametrize("data", [b"[" * (10 * N), b"x = " + b"[" * (5 * N) + b"]" * (5 * N) + b";\n",
                                  b"function f() {" * (2 * N) + b"}" * (2 * N), b"const f = " + b"() => " * (2 * N) + b"g();\n"])
def test_a_deeply_nested_file_is_facts_or_a_note(exact, data):
    f, took = _timed(exact, "javascript", data)
    assert took < LINEAR_S, took
    assert not any(note.startswith("parser_") for note in f.notes)
    assert set(f.notes) <= {"syntax_error"}


def test_each_list_is_cut_to_its_cap_with_a_note(exact):
    imports = b"".join(b'import a%d from "m";\n' % i for i in range(gm.MAX_IMPORTS + 1))
    consts = b"".join(b"const c%d = 1;\n" % i for i in range(gm.MAX_DEFS + 1))
    calls = b"function f() {" + b"".join(b"a0.p%d();" % i for i in range(gm.MAX_REFS + 1)) + b"}\n"
    f, took = _timed(exact, "javascript", imports + consts + calls)
    assert took < 5 * LINEAR_S, took
    assert (len(f.imports), len(f.defs), len(f.refs)) == (gm.MAX_IMPORTS, gm.MAX_DEFS, gm.MAX_REFS)
    assert f.notes == (f"cut definitions from {gm.MAX_DEFS + 2} to {gm.MAX_DEFS}",
                       f"cut imports from {gm.MAX_IMPORTS + 1} to {gm.MAX_IMPORTS}",
                       f"cut references from {gm.MAX_REFS + 1} to {gm.MAX_REFS}")


def test_bytes_that_are_not_utf8_are_a_note_and_a_bom_is_allowed(exact):
    assert exact.parse("web/a.ts", "typescript", b"export const a = '\xff';\n").notes == ("UnicodeDecodeError",)
    f = exact.parse("web/b.ts", "typescript", b"\xef\xbb\xbfexport function f() {}\n")
    assert [(d.name, d.line, d.signature, d.exported) for d in f.defs] == [("f", 1, "export function f()", True)]
    assert f.notes == ()


# --- the engine never loads tree-sitter ---


def test_without_tree_sitter_nothing_is_available():
    # the test process has no tree_sitter, like the stdlib worker
    assert "tree_sitter" not in sys.modules
    assert graph_syntax.available() == {}
    assert graph_syntax.extract("a.ts", b"export const a = 1\n", "typescript").notes == ("no_grammar",)


def test_importing_graph_syntax_does_not_import_tree_sitter():
    code = "import sys, agentflow.graph_syntax; print(any(m.startswith('tree_sitter') for m in sys.modules))"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60,
                         cwd=os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    assert out.stdout.strip() == "False", out.stderr


def test_pins_match_the_worker_header():
    head = graph_parse.WORKER.read_text(encoding="utf-8")
    pins = ", ".join(f'"{name}=={version}"' for name, version in graph_syntax.PINS.items())
    assert f"# dependencies = [{pins}]\n" in head
    assert graph_parse.WORKER.with_name("graph_worker.py.lock").is_file()
