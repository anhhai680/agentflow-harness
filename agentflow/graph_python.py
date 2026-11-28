"""Python facts from `ast`: definitions, imports and references of one file, as `graph_model.Facts`.

`extract` parses the bytes as strict UTF-8 under the placeholder name `<blob>`. It never compiles,
executes or evaluates anything, and it never raises: a file that does not decode or parse gives `empty("python", <the
error's class name>)`. Nothing here recurses on the tree's depth (the walk keeps an explicit stack), so a hostile file costs
a note and not a crash.

A record that `graph_model.check_facts` would refuse (a name outside ASCII, a path too long, an odd shape) is dropped
and counted in one note per kind, and each list is cut to its cap with a note, so the facts are always accepted.

The worker process imports this module, so it uses the standard library and `graph_model` only.
"""

from __future__ import annotations

import ast
import re
import warnings

from agentflow.graph_model import (
    DOC_MAX,
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
    module,
    text,
)

_UPPER = re.compile(r"_*[A-Z][A-Z0-9_]*")
_FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)
_NAME_SET = "[A-Za-z0-9_$.]"


def extract(path: str, data: bytes) -> Facts:
    """The definitions, imports and references in the Python source `data`. Pure; never raises.

    `path` is only the file's name for the caller: it is not read and does not reach the parser.
    """
    del path
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            tree = ast.parse(data.decode("utf-8-sig"), filename="<blob>")  # strict UTF-8, whatever a coding line says
        return _facts(tree, data.count(b"\n") + 1)
    except (SyntaxError, ValueError, RecursionError, MemoryError) as exc:
        return empty("python", type(exc).__name__)
    except Exception as exc:  # the contract is "never raises": whatever a parser bug throws is one note
        return empty("python", type(exc).__name__)


class _Drops:
    """The records an extractor leaves out, counted per kind so each kind gets one note."""

    def __init__(self, lines: int) -> None:
        self.lines = lines
        self.counts = {"definitions": 0, "imports": 0, "references": 0}
        self.clamped = 0

    def line(self, value: int | None) -> int:
        """`value` as a line inside the file: the parser counts a lone CR as a line break, `check_facts` does not."""
        line = value if isinstance(value, int) else 1
        if line > self.lines:
            self.clamped += 1
            return self.lines
        return max(line, 1)

    def notes(self) -> list[str]:
        out = [f"dropped {n} {kind} with names outside {_NAME_SET}" for kind, n in self.counts.items() if n]
        if self.clamped:
            out.append(f"clamped {self.clamped} line numbers to the file's {self.lines} lines")
        return out


def _facts(tree: ast.Module, lines: int) -> Facts:
    drops = _Drops(lines)
    defs = _definitions(tree, drops)
    imports, aliases = _imports(tree, drops)
    known = aliases | {d.name.split(".", 1)[0] for d in defs}
    refs = _references(tree, known, drops)
    notes = drops.notes()
    defs = _cut(defs, MAX_DEFS, "definitions", notes)
    imports = _cut(imports, MAX_IMPORTS, "imports", notes)
    refs = _cut(refs, MAX_REFS, "references", notes)
    return Facts(
        lang="python",
        doc=text(_first_line(tree), DOC_MAX),
        defs=tuple(defs),
        imports=tuple(imports),
        refs=tuple(refs),
        notes=tuple(notes),
    )


def _cut(rows: list, cap: int, kind: str, notes: list[str]) -> list:
    if len(rows) <= cap:
        return rows
    notes.append(f"cut {kind} from {len(rows)} to {cap}")
    return rows[:cap]


def _first_line(node: ast.AST) -> str:
    try:
        doc = ast.get_docstring(node)
    except TypeError:
        return ""
    return doc.split("\n", 1)[0].strip() if doc else ""


def _unparse(node: ast.AST | None) -> str | None:
    """`ast.unparse(node)`, or None when it cannot be rebuilt (a tree too deep for it, say)."""
    if node is None:
        return None
    try:
        return ast.unparse(node)
    except Exception:  # RecursionError, MemoryError, ValueError: the signature is then cut short
        return None


# --- definitions ---


def _definitions(tree: ast.Module, drops: _Drops) -> list[Def]:
    out: list[Def] = []
    for stmt in tree.body:
        if isinstance(stmt, ast.ClassDef):
            _add(out, drops, _def(stmt, stmt.name, "class", drops))
            for member in stmt.body:
                if isinstance(member, _FUNCTIONS):
                    _add(out, drops, _def(member, f"{stmt.name}.{member.name}", "method", drops))
        elif isinstance(stmt, _FUNCTIONS):
            _add(out, drops, _def(stmt, stmt.name, "function", drops))
        else:
            for name in _constant_names(stmt):
                _add(out, drops, _constant(stmt, name, drops))
    return out


def _add(out: list[Def], drops: _Drops, made: Def | None) -> None:
    if made is None:
        drops.counts["definitions"] += 1
    else:
        out.append(made)


def _def(node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef, name: str, kind: str, drops: _Drops) -> Def | None:
    if ident(name) is None:
        return None
    line = drops.line(node.lineno)
    end = max(drops.line(getattr(node, "end_lineno", None) or node.lineno), line)
    return Def(
        name,
        kind,
        line,
        end,
        text(_signature(node), SIGNATURE_MAX),
        text(_first_line(node), DOC_MAX),
        not name.rsplit(".", 1)[-1].startswith("_"),
    )


def _elided() -> ast.Constant:
    return ast.Constant(value=...)


def _without_defaults(args: ast.arguments) -> ast.arguments:
    """`args` with every default and keyword-only default written `...`: a default can hold a secret, as a constant's
    value can, so none reaches the store. The tree itself is not changed."""
    return ast.arguments(
        posonlyargs=args.posonlyargs, args=args.args, vararg=args.vararg, kwonlyargs=args.kwonlyargs,
        kw_defaults=[None if d is None else _elided() for d in args.kw_defaults], kwarg=args.kwarg,
        defaults=[_elided() for _ in args.defaults],
    )


def _signature(node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    if isinstance(node, ast.ClassDef):
        keywords = [f"{kw.arg}=..." if kw.arg is not None  # a keyword's value can hold a secret too
                    else f"**{_unparse(kw.value)}" if isinstance(kw.value, (ast.Name, ast.Attribute)) else "**..."
                    for kw in node.keywords]
        parts = [_unparse(base) or "..." for base in node.bases] + keywords
        return f"class {node.name}({', '.join(parts)})" if parts else f"class {node.name}"
    head = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    args = _unparse(_without_defaults(node.args))
    returns = _unparse(node.returns)
    sig = f"{head} {node.name}({'...' if args is None else args})"
    return f"{sig} -> {returns}" if returns is not None else sig


def _constant_names(stmt: ast.stmt) -> list[str]:
    """The `UPPER_CASE` names a module-level assignment binds with a plain name target."""
    if isinstance(stmt, ast.Assign):
        targets = stmt.targets
    elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
        targets = [stmt.target]
    else:
        return []
    return [t.id for t in targets if isinstance(t, ast.Name) and _UPPER.fullmatch(t.id)]


def _constant(stmt: ast.stmt, name: str, drops: _Drops) -> Def | None:
    if ident(name) is None:
        return None
    line = drops.line(stmt.lineno)
    end = max(drops.line(getattr(stmt, "end_lineno", None) or stmt.lineno), line)
    return Def(name, "constant", line, end, "", "", not name.startswith("_"))


# --- imports ---


def _imports(tree: ast.Module, drops: _Drops) -> tuple[list[Import], set[str]]:
    out: list[Import] = []
    aliases: set[str] = set()
    for node in _preorder(tree):
        if isinstance(node, ast.Import):
            rows = [(a.name, "", a.asname or a.name.split(".", 1)[0], 0) for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            level = node.level or 0
            rows = [(node.module or "", "*" if a.name == "*" else a.name, "" if a.name == "*" else a.asname or a.name, level) for a in node.names]
        else:
            continue
        for mod, name, alias, level in rows:
            if not _import_ok(mod, name, alias, level):
                drops.counts["imports"] += 1
                continue
            out.append(Import(mod, name, alias, level, drops.line(node.lineno)))
            if alias:
                aliases.add(alias)
    return out, aliases


def _import_ok(mod: str, name: str, alias: str, level: int) -> bool:
    if module(mod) is None and not (mod == "" and level > 0):
        return False
    if name not in ("", "*") and ident(name) is None:
        return False
    return alias == "" or ident(alias) is not None


def _preorder(tree: ast.AST):
    """Every node of `tree`, parents first and siblings in source order, with an explicit stack."""
    stack = [tree]
    while stack:
        node = stack.pop()
        yield node
        stack.extend(reversed(list(ast.iter_child_nodes(node))))


# --- references ---


def _references(tree: ast.Module, known: set[str], drops: _Drops) -> list[Ref]:
    """One `Ref` per `(name, scope)` for the outermost dotted chain in load context that starts at a known name."""
    first: dict[tuple[str, str], int] = {}
    stack: list[tuple[ast.AST, str, bool]] = [(stmt, *_scope_of(stmt)) for stmt in reversed(tree.body)]
    while stack:
        node, scope, in_class = stack.pop()
        chain = _chain(node)
        if chain is not None:
            _note_ref(first, chain, node, scope, known, drops)
            continue
        if isinstance(node, ast.Attribute):  # a store, or a chain on a call: only what it hangs from can be a reference
            stack.append((_chain_base(node), scope, False))
            continue
        if in_class and isinstance(node, ast.ClassDef):
            stack.extend(_class_children(node, scope))
        else:
            stack.extend((child, scope, False) for child in reversed(list(ast.iter_child_nodes(node))))
    rows = [Ref(name, line, scope) for (name, scope), line in first.items()]
    rows.sort(key=lambda r: r.line)
    return rows


def _scope_of(stmt: ast.stmt) -> tuple[str, bool]:
    """The scope a module-level statement's code belongs to, and whether its children need class handling."""
    if isinstance(stmt, ast.ClassDef):
        return (stmt.name if ident(stmt.name) else ""), True
    if isinstance(stmt, _FUNCTIONS):
        return (stmt.name if ident(stmt.name) else ""), False
    return "", False


def _class_children(node: ast.ClassDef, scope: str) -> list[tuple[ast.AST, str, bool]]:
    """A top-level class's children, reversed for the stack; a method's code is scoped to `Class.method`."""
    out: list[tuple[ast.AST, str, bool]] = []
    for child in ast.iter_child_nodes(node):
        child_scope = scope
        if isinstance(child, _FUNCTIONS) and scope:
            qualified = f"{scope}.{child.name}"
            child_scope = qualified if ident(qualified) else ""
        out.append((child, child_scope, False))
    out.reverse()
    return out


def _chain_base(node: ast.Attribute) -> ast.AST:
    base: ast.AST = node
    while isinstance(base, ast.Attribute):
        base = base.value
    return base


def _chain(node: ast.AST) -> list[str] | None:
    """The parts of `a.b.c` when `node` is a load-context dotted name that ends in a plain name, else None."""
    parts: list[str] = []
    cur = node
    if isinstance(node, ast.Attribute):
        if not isinstance(node.ctx, ast.Load):
            return None
        while isinstance(cur, ast.Attribute):
            parts.append(cur.attr)
            cur = cur.value
    if not isinstance(cur, ast.Name) or not isinstance(cur.ctx, ast.Load):
        return None
    parts.append(cur.id)
    parts.reverse()
    return parts


def _note_ref(first: dict[tuple[str, str], int], parts: list[str], node: ast.AST, scope: str, known: set[str], drops: _Drops) -> None:
    if parts[0] not in known:
        return
    name = ".".join(parts)
    if ident(name) is None:
        drops.counts["references"] += 1
        return
    key = (name, scope)
    line = drops.line(getattr(node, "lineno", 1))
    if key not in first or line < first[key]:
        first[key] = line
