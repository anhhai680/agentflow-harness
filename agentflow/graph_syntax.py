"""TypeScript, TSX and JavaScript facts from tree-sitter: definitions, imports and references, as `graph_model.Facts`.

Only the parser worker imports this module, and `tree_sitter` is imported lazily, the first time `available` or
`extract` asks for a grammar: the engine never loads it. Under `uv run --locked --script` the worker has the exact
`PINS` its header names and its hash lock holds; under `python -I -S` (the stdlib mode) the import fails, `available`
is `{}` and `extract` gives the note `no_grammar`. A grammar is offered only when the installed distribution is the
pinned version and its language loads.

`extract` decodes the bytes as strict UTF-8 (a byte order mark is allowed) and hands them to tree-sitter. It never
compiles, executes or evaluates anything, and it never raises: a file that does not decode gives
`empty(lang, <the error's class name>)`, and a tree with errors still gives what was found, with the note
`syntax_error`. Every walk keeps an explicit stack, so a deep tree costs time and not a crash.

The rules. Top-level `function`, `class`, `interface`, `type` and `enum` declarations are definitions (`function`, or
`class` for the other four), as is a `const` or `let` bound to an arrow function or function expression (`function`)
and any other top-level `const NAME` but a `require` (`constant`, with no signature, so a literal's value never reaches the store). A
class's methods are `Class.method`. `exported` means declared under `export` (`export default` included, named
`default` when anonymous), named in a local `export { ... }`, or named by `module.exports` / `exports.x`; a method is
exported with its class unless it is `private` or `#private`. `signature` is the first line of the declaration up to
its body; `doc` is the first line of a `/** ... */` comment directly above. Imports are one `Import` per binding of
`import`, `export ... from`, `import x = require(...)`, `require(...)` and `import(...)`, with `module` the specifier
as written and `level` 0; a binding-less import has name and alias "". A `Ref` is a call or member chain whose first
part is an import binding or a top-level definition, once per `(name, scope)`, scoped to the enclosing definition.

A record that `graph_model.check_facts` would refuse is dropped and counted in one note per kind, and each list is cut
to its cap with a note, so the facts are always accepted.

The worker process imports this module, so it uses the standard library and `graph_model` only.
"""

from __future__ import annotations

import codecs
import dataclasses

from agentflow.graph_model import (
    DOC_MAX,
    EXTRACTOR_VERSIONS,
    MAX_DEFS,
    MAX_IMPORTS,
    MAX_REFS,
    SIGNATURE_MAX,
    Def,
    Facts,
    Import,
    Ref,
    empty,
    ident,
    specifier,
    text,
)

PINS = {"tree-sitter": "0.27.0", "tree-sitter-javascript": "0.25.0", "tree-sitter-typescript": "0.23.2"}
# language -> (distribution, module, the function that returns its grammar)
_GRAMMARS = {
    "javascript": ("tree-sitter-javascript", "tree_sitter_javascript", "language"),
    "typescript": ("tree-sitter-typescript", "tree_sitter_typescript", "language_typescript"),
    "tsx": ("tree-sitter-typescript", "tree_sitter_typescript", "language_tsx"),
}
_NAME_SET = "[A-Za-z0-9_$.]"
_FUNCTION_VALUES = frozenset(("arrow_function", "function_expression", "function", "generator_function"))
_FUNCTIONS = frozenset(("function_declaration", "generator_function_declaration"))
_CLASSES = frozenset(("class_declaration", "abstract_class_declaration"))
_TYPES = frozenset(("interface_declaration", "type_alias_declaration", "enum_declaration"))
_ASSIGNMENTS = frozenset(("assignment_expression", "augmented_assignment_expression"))
_SIGNATURE_TAILS = ("=>", "=", "{")

_parsers: dict[str, object] | None = None


def _load() -> dict[str, object]:
    """One tree-sitter parser per language whose pinned grammar loads; `{}` without tree-sitter. Never raises."""
    global _parsers
    if _parsers is not None:
        return _parsers
    _parsers = {}
    try:
        import importlib
        import importlib.metadata

        import tree_sitter

        if importlib.metadata.version("tree-sitter") != PINS["tree-sitter"]:
            return _parsers
    except Exception:  # not installed (the stdlib worker), or not loadable: no grammar at all
        return _parsers
    for lang, (dist, mod, func) in _GRAMMARS.items():
        try:
            if importlib.metadata.version(dist) != PINS[dist]:
                continue
            grammar = getattr(importlib.import_module(mod), func)()
            _parsers[lang] = tree_sitter.Parser(tree_sitter.Language(grammar))
        except Exception:  # a missing package or an ABI the core refuses: that language is not offered
            continue
    return _parsers


def available() -> dict[str, str]:
    """`{lang: "<extractor version>+<grammar version>"}` for each grammar that loads, or `{}` without tree-sitter."""
    return {lang: f"{EXTRACTOR_VERSIONS[lang]}+{PINS[_GRAMMARS[lang][0]]}" for lang in _load()}


def extract(path: str, data: bytes, lang: str) -> Facts:
    """The definitions, imports and references in the `lang` source `data`. Pure; never raises.

    `path` is only the file's name for the caller: it is not read and does not reach the parser.
    """
    del path
    try:
        parser = _load().get(lang) if lang in _GRAMMARS else None
        if parser is None:
            return empty(lang, "no_grammar")
        data.decode("utf-8-sig")  # strict: a file that is not UTF-8 is one note, as in the other extractors
        body = data[len(codecs.BOM_UTF8):] if data.startswith(codecs.BOM_UTF8) else data
        root = parser.parse(body).root_node
        return _Walk(body, lang, data.count(b"\n") + 1).facts(root)
    except Exception as exc:  # the contract is "never raises": whatever a parser bug throws is one note
        return empty(lang, type(exc).__name__)


def _cut(rows: list, cap: int, kind: str, notes: list[str]) -> list:
    if len(rows) <= cap:
        return rows
    notes.append(f"cut {kind} from {len(rows)} to {cap}")
    return rows[:cap]


def _field(node, name: str):
    return node.child_by_field_name(name)


class _Walk:
    """One file's walk: the source bytes, the line count, and the records dropped on the way."""

    def __init__(self, body: bytes, lang: str, lines: int) -> None:
        self.body = body
        self.lang = lang
        self.lines = lines
        self.drops = {"definitions": 0, "imports": 0, "references": 0}
        self.clamped = 0
        self.defs: list[Def] = []
        self.private: set[str] = set()  # methods that stay unexported with their class
        self.named: set[str] = set()  # local names an `export { }`, `export default x` or `module.exports` exports
        self.imports: list[Import] = []
        self.roots: list[tuple[object, str]] = []  # (node, scope) where the reference walk starts
        self.chains: dict[tuple[str, str], int] = {}

    # --- small helpers ---

    def text(self, node) -> str:
        return self.body[node.start_byte : node.end_byte].decode("utf-8", "replace")

    def line(self, row: int) -> int:
        """A tree-sitter row as a line inside the file, clamped as `graph_python` does."""
        line = row + 1
        if line > self.lines:
            self.clamped += 1
            return self.lines
        return max(line, 1)

    def string(self, node) -> str | None:
        """The text between a string literal's quotes, as written (an escape stays an escape), or None."""
        if node is None or node.type != "string":
            return None
        raw = self.text(node)
        return raw[1:-1] if len(raw) >= 2 else None

    def notes(self, root) -> list[str]:
        out = ["syntax_error"] if root.has_error else []
        out += [f"dropped {n} {kind} with names outside {_NAME_SET}" for kind, n in self.drops.items() if n]
        if self.clamped:
            out.append(f"clamped {self.clamped} line numbers to the file's {self.lines} lines")
        return out

    # --- the whole file ---

    def facts(self, root) -> Facts:
        for stmt in root.named_children:
            self.statement(stmt)
        self.walk_references()
        imports = sorted(self.imports, key=lambda i: i.line)
        known = {d.name.split(".", 1)[0] for d in self.defs} | {i.alias for i in imports if i.alias}
        refs = [Ref(name, line, scope) for (name, scope), line in self.chains.items() if name.split(".", 1)[0] in known]
        refs.sort(key=lambda r: r.line)
        defs = [dataclasses.replace(d, exported=True) if self._named(d) else d for d in self.defs]
        notes = self.notes(root)
        defs = _cut(defs, MAX_DEFS, "definitions", notes)
        imports = _cut(imports, MAX_IMPORTS, "imports", notes)
        refs = _cut(refs, MAX_REFS, "references", notes)
        return Facts(lang=self.lang, defs=tuple(defs), imports=tuple(imports), refs=tuple(refs), notes=tuple(notes))

    def _named(self, d: Def) -> bool:
        return not d.exported and d.name.split(".", 1)[0] in self.named and d.name not in self.private

    def statement(self, stmt) -> None:
        kind = stmt.type
        if kind == "import_statement":
            self.import_statement(stmt)
        elif kind == "export_statement":
            self.export_statement(stmt)
        elif kind == "expression_statement":
            self.module_exports(stmt)
            self.roots.append((stmt, ""))
        else:
            self.declaration(stmt, stmt, exported=False)

    # --- definitions ---

    def declaration(self, node, outer, *, exported: bool) -> None:
        """The definitions `node` makes, `outer` being the statement it stands in (an `export` around it, say)."""
        kind = node.type
        if kind in _FUNCTIONS:
            scope = self.add(_field(node, "name"), "function", node, outer, exported, _field(node, "body"))
            self.roots.append((node, scope))
        elif kind in _CLASSES:
            self.klass(node, outer, exported, self.text(_field(node, "name")) if _field(node, "name") else None)
        elif kind in _TYPES:
            body = _field(node, "value") if kind == "type_alias_declaration" else _field(node, "body")
            self.add(_field(node, "name"), "class", node, outer, exported, body)
        elif kind == "lexical_declaration":
            self.lexical(node, outer, exported)
        else:
            self.roots.append((node, ""))

    def add(self, name_node, kind: str, node, outer, exported: bool, body, *, name: str | None = None,
            doc: bool = True) -> str:
        """Record one definition; its name as a scope, or "" when it has none or was dropped."""
        if name is None:
            if name_node is None:
                return ""
            name = self.text(name_node)
        if ident(name) is None:
            self.drops["definitions"] += 1
            return ""
        line = self.line(outer.start_point[0])
        end = max(self.line(node.end_point[0]), line)
        signature = "" if kind == "constant" else self.signature(outer, body)
        self.defs.append(Def(name, kind, line, end, signature, self.doc(outer) if doc else "", exported))
        return name

    def klass(self, node, outer, exported: bool, name: str | None) -> None:
        body = _field(node, "body")
        scope = self.add(None, "class", node, outer, exported, body, name=name if name is not None else "")
        for child in node.named_children:
            if child is not None and child != body:
                self.roots.append((child, scope))
        if body is None:
            return
        for member in body.named_children:
            if member.type != "method_definition" or not scope:
                self.roots.append((member, scope))
                continue
            name_node = _field(member, "name")
            method = f"{scope}.{self.text(name_node)}" if name_node is not None else ""
            private = any(c.type == "accessibility_modifier" and self.text(c) == "private" for c in member.children)
            private = private or (name_node is not None and name_node.type == "private_property_identifier")
            qualified = self.add(None, "method", member, member, exported and not private, _field(member, "body"),
                                 name=method)
            if qualified and private:
                self.private.add(qualified)
            self.roots.append((member, qualified or scope))

    def lexical(self, node, outer, exported: bool) -> None:
        const = any(c.type == "const" for c in node.children)
        first = True
        for decl in node.named_children:
            if decl.type != "variable_declarator":
                continue
            name, value = _field(decl, "name"), _field(decl, "value")
            start = outer if first else decl
            scope = ""
            if name is not None and name.type == "identifier":
                if value is not None and value.type in _FUNCTION_VALUES:
                    scope = self.add(name, "function", decl, start, exported, _field(value, "body"), doc=first)
                elif const and not self.required(value):
                    self.add(name, "constant", decl, start, exported, None, doc=first)
            self.roots.append((decl, scope))
            first = False

    def required(self, value) -> bool:
        """Whether `value` is a `require(...)` call: what it binds is an import, not a constant."""
        fn = _field(value, "function") if value is not None and value.type == "call_expression" else None
        return fn is not None and fn.type == "identifier" and self.text(fn) == "require"

    def signature(self, outer, body) -> str:
        """The first line of `outer` up to `body`, without the `=>`, `=` or `{` before it."""
        stop = body.start_byte if body is not None and body.start_byte > outer.start_byte else outer.end_byte
        head = self.body[outer.start_byte : stop].decode("utf-8", "replace").split("\n", 1)[0].rstrip()
        for tail in _SIGNATURE_TAILS:
            if head.endswith(tail):
                head = head[: -len(tail)].rstrip()
        return text(head, SIGNATURE_MAX)

    def doc(self, outer) -> str:
        """The first line of a `/** ... */` comment that ends on the line above `outer` (or on its own line)."""
        comment = outer.prev_named_sibling
        if comment is None or comment.type != "comment" or comment.end_point[0] < outer.start_point[0] - 1:
            return ""
        raw = self.text(comment)
        if not raw.startswith("/**") or not raw.endswith("*/") or len(raw) < 5:
            return ""
        for row in raw[3:-2].split("\n"):
            row = row.strip().lstrip("*").strip()
            if row:
                return text(row, DOC_MAX)
        return ""

    # --- exports ---

    def export_statement(self, stmt) -> None:
        source = _field(stmt, "source")
        if source is not None:
            self.reexport(stmt, source)
            return
        decl, value = _field(stmt, "declaration"), _field(stmt, "value")
        if decl is not None:
            self.declaration(decl, stmt, exported=True)
            return
        if value is not None:
            self.default(stmt, value)
            return
        for clause in stmt.named_children:
            if clause.type == "export_clause":
                for spec in clause.named_children:
                    name = _field(spec, "name")
                    if spec.type == "export_specifier" and name is not None:
                        self.named.add(self.text(name))

    def default(self, stmt, value) -> None:
        kind = value.type
        if kind in _FUNCTION_VALUES:
            scope = self.add(None, "function", value, stmt, True, _field(value, "body"), name="default")
            self.roots.append((value, scope))
        elif kind == "class":
            self.klass(value, stmt, True, "default")
        else:
            if kind == "identifier":
                self.named.add(self.text(value))
            self.roots.append((value, ""))

    def module_exports(self, stmt) -> None:
        """Local names `module.exports = {a, b: c}`, `module.exports = a` or `exports.x = a` export."""
        assign = stmt.named_children[0] if stmt.named_child_count else None
        if assign is None or assign.type != "assignment_expression":
            return
        left, right = _field(assign, "left"), _field(assign, "right")
        if left is None or right is None or left.type != "member_expression":
            return
        target = self.text(left)
        if target != "module.exports" and not target.startswith(("module.exports.", "exports.")):
            return
        if right.type == "identifier":
            self.named.add(self.text(right))
        elif right.type == "object" and target == "module.exports":
            for prop in right.named_children:
                if prop.type == "shorthand_property_identifier":
                    self.named.add(self.text(prop))
                elif prop.type == "pair" and _field(prop, "value") is not None and _field(prop, "value").type == "identifier":
                    self.named.add(self.text(_field(prop, "value")))

    # --- imports ---

    def bind(self, mod: str | None, name: str, alias: str, node) -> None:
        if mod is None or specifier(mod) is None or (name not in ("", "*") and ident(name) is None):
            self.drops["imports"] += 1
            return
        if alias != "" and ident(alias) is None:
            self.drops["imports"] += 1
            return
        self.imports.append(Import(mod, name, alias, 0, self.line(node.start_point[0])))

    def import_statement(self, stmt) -> None:
        mod = self.string(_field(stmt, "source"))
        clauses = [c for c in stmt.named_children if c.type in ("import_clause", "import_require_clause")]
        if not clauses:
            self.bind(mod, "", "", stmt)
            return
        for clause in clauses:
            if clause.type == "import_require_clause":
                names = [c for c in clause.named_children if c.type == "identifier"]
                self.bind(self.string(_field(clause, "source")), "default", self.text(names[0]) if names else "", stmt)
                continue
            for part in clause.named_children:
                if part.type == "identifier":
                    self.bind(mod, "default", self.text(part), stmt)
                elif part.type == "namespace_import":
                    names = [c for c in part.named_children if c.type == "identifier"]
                    self.bind(mod, "*", self.text(names[0]) if names else "", stmt)
                elif part.type == "named_imports":
                    for spec in part.named_children:
                        if spec.type == "import_specifier":
                            self.specifiers(mod, spec, stmt)

    def specifiers(self, mod: str | None, spec, stmt) -> None:
        name, alias = _field(spec, "name"), _field(spec, "alias")
        if name is None:
            return
        bound = self.text(name)
        self.bind(mod, bound, self.text(alias) if alias is not None else bound, stmt)

    def reexport(self, stmt, source) -> None:
        mod = self.string(source)
        found = False
        for part in stmt.named_children:
            if part.type == "export_clause":
                found = True
                for spec in part.named_children:
                    if spec.type == "export_specifier":
                        self.specifiers(mod, spec, stmt)
            elif part.type == "namespace_export":
                found = True
                names = [c for c in part.named_children if c.type == "identifier"]
                self.bind(mod, "*", self.text(names[0]) if names else "", stmt)
        if not found:  # `export * from "m"`
            self.bind(mod, "*", "", stmt)

    def require(self, call, kind: str) -> None:
        """`require("m")` or `import("m")` with one string argument, bound by the declarator around it if any."""
        args = _field(call, "arguments")
        strings = [] if args is None else list(args.named_children)
        if len(strings) != 1 or strings[0].type != "string":
            return
        mod = self.string(strings[0])
        parent = call.parent
        target = _field(parent, "name") if kind == "require" and parent is not None and parent.type == "variable_declarator" else None
        if target is not None and _field(parent, "value") != call:
            target = None
        if target is None:
            self.bind(mod, "", "", call)
        elif target.type == "identifier":
            self.bind(mod, "default", self.text(target), call)
        elif target.type == "object_pattern":
            for prop in target.named_children:
                if prop.type == "shorthand_property_identifier_pattern":
                    self.bind(mod, self.text(prop), self.text(prop), call)
                elif prop.type == "pair_pattern" and _field(prop, "key") is not None and _field(prop, "value") is not None:
                    self.bind(mod, self.text(_field(prop, "key")), self.text(_field(prop, "value")), call)
        else:
            self.bind(mod, "", "", call)

    # --- references ---

    def chain(self, node) -> list[str] | None:
        """The parts of `a.b.c` when `node` is a member chain of plain names, else None."""
        parts: list[str] = []
        cur = node
        while cur is not None and cur.type == "member_expression":
            prop = _field(cur, "property")
            if prop is None or prop.type != "property_identifier":
                return None
            parts.append(self.text(prop))
            cur = _field(cur, "object")
        if cur is None or cur.type != "identifier":
            return None
        parts.append(self.text(cur))
        parts.reverse()
        return parts

    def note(self, parts: list[str], node, scope: str) -> None:
        name = ".".join(parts)
        if ident(name) is None:
            self.drops["references"] += 1
            return
        key = (name, scope)
        line = self.line(node.start_point[0])
        if key not in self.chains or line < self.chains[key]:
            self.chains[key] = line

    def walk_references(self) -> None:
        """Every call and member chain under the roots, with their scopes; requires and `import()` on the way."""
        stack = list(reversed(self.roots))
        while stack:
            node, scope = stack.pop()
            kind = node.type
            skip = None
            if kind == "member_expression":
                parts = self.chain(node)
                if parts is not None:
                    self.note(parts, node, scope)
                    continue
            elif kind == "call_expression":
                fn = _field(node, "function")
                if fn is not None and fn.type == "identifier":
                    if self.text(fn) == "require":
                        self.require(node, "require")
                    else:
                        self.note([self.text(fn)], fn, scope)
                    skip = fn
                elif fn is not None and fn.type == "import":
                    self.require(node, "import")
                    skip = fn
            elif kind == "new_expression":
                ctor = _field(node, "constructor")
                if ctor is not None and ctor.type == "identifier":
                    self.note([self.text(ctor)], ctor, scope)
                    skip = ctor
            elif kind in _ASSIGNMENTS:
                left = _field(node, "left")
                if left is not None and left.type == "member_expression":  # a store: only what it hangs from is read
                    skip = left
                    base = _field(left, "object")
                    if base is not None:
                        stack.append((base, scope))
            stack.extend((child, scope) for child in reversed(node.named_children) if child != skip)
