"""graph_syntax: TypeScript, TSX and JavaScript facts from tree-sitter, in the locked worker and never in the engine."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

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


def test_a_broken_file_keeps_what_was_found(exact):
    f = exact.parse("web/broken.ts", "typescript", b'import { a } from "./a";\nexport const a = 1,\n')
    assert "syntax_error" in f.notes
    assert [(i.module, i.name) for i in f.imports] == [("./a", "a")]


def test_the_locked_worker_offers_the_pinned_grammars(exact):
    assert exact.mode in ("offline", "locked")
    assert exact.languages["typescript"] == "1+0.23.2"
    assert exact.languages["tsx"] == "1+0.23.2" and exact.languages["javascript"] == "1+0.25.0"
    assert {"python", "markdown", "manifest"} <= set(exact.languages)


def test_the_script_lock_is_used_inside_a_project_of_its_own(exact, tmp_path):
    # uv reads the script's header and `graph_worker.py.lock`, never a project file or `uv.lock` around the cwd
    project = tmp_path / "other"
    project.mkdir()
    (project / "pyproject.toml").write_text('[project]\nname = "other"\nversion = "0"\ndependencies = ["requests"]\n')
    out = subprocess.run(graph_parse.worker_argv(exact.mode), input=b"", capture_output=True, cwd=project,
                         env=graph_parse.worker_env(), timeout=120)
    assert json.loads(out.stdout.splitlines()[0])["languages"]["typescript"] == "1+0.23.2"
    assert sorted(p.name for p in project.iterdir()) == ["pyproject.toml"]


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
