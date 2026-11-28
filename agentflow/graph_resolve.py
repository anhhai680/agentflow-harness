"""Turn one file's stored facts into the knowledge graph's nodes and edges, and name the keys that tie files together.

Everything here is pure string logic: no file is opened, no clock is read, and no module name is ever treated as a
filesystem path. A file *provides* keys (`path:<path>`, `py:<dotted module>`, `pkg:<ecosystem>/<name>`) and *wants* the
keys its imports and links could resolve to. A later delta build re-resolves exactly the files whose wanted keys a
change touched, so `wanted` must list every key `resolve` can look up: both are built from the same plan.

`resolve` asks an `Index` (a lookup over the facts of the other files) who provides a key. One provider is taken. Several
are narrowed to the one that shares the longest leading directory with the importing file, and a tie gives no edge. A
target that is absolute, empty, or climbs past the repository root wants nothing.

Ids and values the caller can rely on:

- A parsed code or manifest file is a `file` node and a Markdown file is a `doc` node. Both have `line` 1 and `end` 1, so
  that `file_node` (a listed file that was not parsed) gives the same node as a parsed one with no text.
- An edge's target is a `doc` node for a Markdown path and a `file` node for any other path, decided by the path alone, so
  an edge never depends on whether the target was parsed.
- Two definitions with one name give one `symbol` node (the one with the lowest line). Two sections with the same kind
  and title each keep their own node: the later one's id carries `:<line>` after the title.
- A `needs` node is named by the `pkg:<ecosystem>/<name>` key of the dependency, as a `provides` node is, and keeps the
  dependency's version spec as its signature.

It imports only `graph_model` and `graph_select`, and the parser worker does not import it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from agentflow.graph_model import JS_LANGS, Def, Edge, Facts, Import, Node, node_id
from agentflow.graph_select import is_test, language

_JS_SUFFIXES = ("", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", "/index.ts", "/index.tsx", "/index.js")


class Index(Protocol):
    """What `resolve` knows about the other files of the repository; `graph_build` backs it with the store."""

    def providers(self, key: str) -> Sequence[str]:
        """The paths that provide `key`, sorted."""
        ...

    def facts(self, path: str) -> Facts | None:
        """The stored facts of `path`, or None when it is listed and not parsed."""
        ...


@dataclass(frozen=True)
class _Cand:
    """One file an import may mean: the keys that name it, what is looked up in it and what a ref's head consumes."""

    keys: tuple[str, ...]  # in priority order; the first key with a provider decides
    name: str  # "" = the module itself; else a name looked up in the file
    consumed: tuple[str, ...]  # the leading parts of a ref that this binding takes; () binds nothing


@dataclass(frozen=True)
class _Plan:
    cands: tuple[_Cand, ...]  # most specific first
    first_only: bool  # only the first candidate with a provider counts (a from-import's submodule or its package)


# --- keys ---


def _normalise(parts: Sequence[str]) -> list[str] | None:
    """`parts` joined as a relative path with `.` and empty segments dropped and `..` applied, or None past the root."""
    out: list[str] = []
    for part in parts:
        if part in ("", "."):
            continue
        if part == "..":
            if not out:
                return None
            out.pop()
        else:
            out.append(part)
    return out


def _directories(path: str) -> list[str]:
    return path.split("/")[:-1]


def _plain(part: str) -> bool:
    return part.isascii() and part.isidentifier()


def _python_keys(path: str) -> set[str]:
    """`py:` keys: every dotted suffix of the file's module path that names importable segments only."""
    *directories, name = path.split("/")
    if not name.endswith(".py"):
        return set()
    stem = name[:-3]
    parts = directories if stem == "__init__" else [*directories, stem]
    keys: set[str] = set()
    for start in range(len(parts) - 1, -1, -1):
        if not _plain(parts[start]):
            break
        keys.add("py:" + ".".join(parts[start:]))
    return keys


def provided(path: str, facts: Facts | None) -> frozenset[str]:
    """The keys other files may resolve to: its path, its Python module names, the package a manifest declares."""
    keys = {f"path:{path}"}
    if facts is None:
        return frozenset(keys)
    if facts.lang == "python":
        keys |= _python_keys(path)
    if facts.lang == "manifest" and facts.package:
        keys.add(_package_key(facts.ecosystem, facts.package))
    return frozenset(keys)


def _package_key(ecosystem: str, name: str) -> str:
    return f"pkg:{ecosystem}/{name}"


def _path_keys(parts: Sequence[str]) -> tuple[str, ...]:
    """The keys of the Python module or package at the directory path `parts`: its `__init__.py`, then its `.py`."""
    joined = "/".join(parts)
    if not parts:
        return ("path:__init__.py",)
    return (f"path:{joined}/__init__.py", f"path:{joined}.py")


def _binding(alias: str) -> tuple[str, ...]:
    return (alias,) if alias else ()


def _python_plan(path: str, imp: Import) -> _Plan | None:
    named = imp.name not in ("", "*")
    if imp.level == 0:
        if named:
            return _Plan(
                (
                    _Cand((f"py:{imp.module}.{imp.name}",), "", _binding(imp.alias)),
                    _Cand((f"py:{imp.module}",), imp.name, _binding(imp.alias)),
                ),
                True,
            )
        parts = imp.module.split(".")
        if imp.alias and imp.name == "" and len(parts) > 1 and imp.alias == parts[0]:  # `import a.b.c` binds `a`
            ladder = tuple(_Cand((f"py:{'.'.join(parts[:k])}",), "", tuple(parts[:k])) for k in range(len(parts), 0, -1))
            return _Plan(ladder, False)
        return _Plan((_Cand((f"py:{imp.module}",), "", _binding(imp.alias)),), True)
    directories = _directories(path)
    up = imp.level - 1
    if up > len(directories):
        return None
    base = directories[: len(directories) - up]
    parts = imp.module.split(".") if imp.module else []
    if not all(_plain(part) for part in parts):
        return None
    module_keys = _path_keys([*base, *parts]) if parts else ("path:" + "/".join([*base, "__init__.py"]),)
    if named and _plain(imp.name):
        return _Plan(
            (
                _Cand(_path_keys([*base, *parts, imp.name]), "", _binding(imp.alias)),
                _Cand(module_keys, imp.name, _binding(imp.alias)),
            ),
            True,
        )
    return _Plan((_Cand(module_keys, imp.name if named else "", _binding(imp.alias)),), True)


def _script_plan(path: str, imp: Import) -> _Plan | None:
    segments = imp.module.split("/")
    if segments[0] not in (".", ".."):
        return None  # a bare specifier is a package, not a file of this repository
    target = _normalise([*_directories(path), *segments])
    if target is None:
        return None
    joined = "/".join(target)
    forms = [joined + suffix for suffix in _JS_SUFFIXES] if joined else [suffix.lstrip("/") for suffix in _JS_SUFFIXES[7:]]
    if joined.endswith(".js") and len(joined) > 3:
        forms += [joined[:-3] + ".ts", joined[:-3] + ".tsx"]
    keys = tuple(dict.fromkeys(f"path:{form}" for form in forms))
    if not imp.alias or imp.name == "default":
        return _Plan((_Cand(keys, "", ()),), True)
    name = "" if imp.name in ("", "*") else imp.name
    return _Plan((_Cand(keys, name, (imp.alias,)),), True)


def _plan(path: str, lang: str, imp: Import) -> _Plan | None:
    if lang == "python":
        return _python_plan(path, imp)
    if lang in JS_LANGS:
        return _script_plan(path, imp)
    return None


def _link_key(path: str, target: str) -> str | None:
    parts = _normalise([*_directories(path), *target.split("/")])
    return f"path:{'/'.join(parts)}" if parts else None


def wanted(path: str, facts: Facts) -> frozenset[str]:
    """Every key this file's imports and links could resolve to; empty for a file with neither."""
    keys: set[str] = set()
    for imp in facts.imports:
        plan = _plan(path, facts.lang, imp)
        if plan is not None:
            for cand in plan.cands:
                keys.update(cand.keys)
    if facts.lang == "markdown":
        for link in facts.links:
            key = _link_key(path, link.target)
            if key is not None:
                keys.add(key)
    return frozenset(keys)


# --- choosing a provider ---


def _shared(a: Sequence[str], b: Sequence[str]) -> int:
    count = 0
    for left, right in zip(a, b):
        if left != right:
            break
        count += 1
    return count


def _choose(path: str, found: Sequence[str]) -> str | None:
    """The one provider nearest `path`, or None when two are equally near."""
    if len(found) == 1:
        return found[0]
    here = _directories(path)
    scored = sorted(((_shared(here, _directories(p)), p) for p in found), reverse=True)
    if scored[0][0] == scored[1][0]:
        return None
    return scored[0][1]


def _provider(path: str, keys: Sequence[str], index: Index) -> tuple[bool, str | None]:
    """`(matched, file)`: whether any key had a provider other than `path`, and the one chosen (None when ambiguous)."""
    for key in keys:
        found = sorted({p for p in index.providers(key) if p != path})
        if found:
            return True, _choose(path, found)
    return False, None


# --- nodes ---


def _kind_of(path: str, lang: str | None = None) -> str:
    return "doc" if (lang or language(path)) == "markdown" else "file"


def _basename(path: str) -> str:
    return path.rpartition("/")[2]


def file_node(repo_id: str, path: str, lang: str | None) -> Node:
    """The node of a path that is listed and not parsed: a `doc` for Markdown, else a `file`; line 1, end 1, no text."""
    kind = _kind_of(path, lang)
    return Node(node_id(repo_id, kind, path), kind, _basename(path), path, 1, 1, "human" if kind == "doc" else "code", "", "")


def _defs_by_name(defs: Sequence[Def]) -> dict[str, Def]:
    out: dict[str, Def] = {}
    for d in sorted(defs, key=lambda d: d.line):
        out.setdefault(d.name, d)
    return out


def _section_names(facts: Facts) -> list[str]:
    seen: set[tuple[str, str]] = set()
    names: list[str] = []
    for section in facts.sections:
        key = (section.kind, section.title)
        names.append(section.title if key not in seen else f"{section.title}:{section.line}")
        seen.add(key)
    return names


def _own_nodes(repo_id: str, path: str, facts: Facts, defs: dict[str, Def]) -> list[Node]:
    kind = _kind_of(path, facts.lang)
    trust = "human" if kind == "doc" else "code"
    nodes = [Node(node_id(repo_id, kind, path), kind, _basename(path), path, 1, 1, trust, "", facts.doc)]
    for d in defs.values():
        nodes.append(Node(node_id(repo_id, "symbol", path, d.name), "symbol", d.name, path, d.line, d.end, "code", d.signature, d.doc))
    for section, name in zip(facts.sections, _section_names(facts)):
        nodes.append(Node(node_id(repo_id, section.kind, path, name), section.kind, section.title, path, section.line, section.end, "human", "", section.text))
    if facts.lang == "manifest":
        if facts.package:
            nodes.append(Node(node_id(repo_id, "package", path, facts.package), "package", facts.package, path, 1, 1, "code", "", ""))
            key = _package_key(facts.ecosystem, facts.package)
            nodes.append(Node(node_id(repo_id, "provides", path, key), "provides", key, path, 1, 1, "code", "", ""))
        for dep in facts.deps:
            key = _package_key(facts.ecosystem, dep.name)
            nodes.append(Node(node_id(repo_id, "needs", path, key), "needs", key, path, 1, 1, "code", dep.spec, ""))
    return nodes


# --- edges ---


def _lookup(index: Index, cache: dict[str, dict[str, Def]], file: str) -> dict[str, Def]:
    if file not in cache:
        found = index.facts(file)
        cache[file] = _defs_by_name(found.defs) if found is not None else {}
    return cache[file]


def _hit(defs: dict[str, Def], name: str, rest: Sequence[str], least: int) -> str | None:
    """The longest `name.rest[:j]` (`j` from `len(rest)` down to `least`) that `defs` has."""
    for j in range(len(rest), least - 1, -1):
        wanted_name = ".".join([name, *rest[:j]]) if name else ".".join(rest[:j])
        if wanted_name and wanted_name in defs:
            return wanted_name
    return None


def _reference_target(
    ref_name: str, path: str, own: dict[str, Def], bound: dict[str, list[tuple[_Cand, str]]], index: Index, cache: dict[str, dict[str, Def]]
) -> tuple[str, str] | None:
    parts = ref_name.split(".")
    options = bound.get(parts[0])
    if options is None:
        for k in range(len(parts), 0, -1):
            name = ".".join(parts[:k])
            if name in own:
                return path, name
        return None
    for cand, file in options:
        if parts[: len(cand.consumed)] != list(cand.consumed):
            continue
        rest = parts[len(cand.consumed) :]
        found = _hit(_lookup(index, cache, file), cand.name, rest, 0 if cand.name else 1)
        if found is not None:
            return file, found
    return None


def resolve(repo_id: str, path: str, facts: Facts, index: Index) -> tuple[list[Node], list[Edge]]:
    """The nodes and edges `facts` (the facts of `path`) make, looking other files up in `index`; both sorted.

    Every edge has `origin == path`. A node id appears once and an edge is kept once per `(src, dst, kind)`.
    """
    own = _defs_by_name(facts.defs)
    nodes = {n.id: n for n in reversed(_own_nodes(repo_id, path, facts, own))}
    me = node_id(repo_id, _kind_of(path, facts.lang), path)
    edges: dict[tuple[str, str, str], Edge] = {}

    def add(src: str, dst: str, kind: str, confidence: str) -> None:
        if src != dst:
            edges.setdefault((src, dst, kind), Edge(src, dst, kind, confidence, path))

    def target(file: str) -> str:
        return node_id(repo_id, _kind_of(file), file)

    for d in own.values():
        add(me, node_id(repo_id, "symbol", path, d.name), "contains", "exact")
    for section, name in zip(facts.sections, _section_names(facts)):
        add(me, node_id(repo_id, section.kind, path, name), "contains", "exact")
    if facts.lang == "manifest":
        owner = node_id(repo_id, "package", path, facts.package) if facts.package else me
        for dep in facts.deps:
            add(owner, node_id(repo_id, "needs", path, _package_key(facts.ecosystem, dep.name)), "depends_on", "exact")
    for link in facts.links if facts.lang == "markdown" else ():
        key = _link_key(path, link.target)
        matched, file = _provider(path, (key,), index) if key else (False, None)
        if matched and file is not None:
            add(me, target(file), "mentions", "exact")

    cache: dict[str, dict[str, Def]] = {}
    bound: dict[str, list[tuple[_Cand, str]]] = {}
    imported: list[str] = []
    for imp in facts.imports:
        plan = _plan(path, facts.lang, imp)
        if plan is None:
            continue
        chosen: list[tuple[_Cand, str]] = []
        for cand in plan.cands:
            matched, file = _provider(path, cand.keys, index)
            if matched and file is None:
                break  # an ambiguous provider is no edge, and no weaker reading either
            if file is not None:
                chosen.append((cand, file))
                if plan.first_only:
                    break
        if not chosen:
            continue
        imported.append(chosen[0][1])
        add(me, target(chosen[0][1]), "imports", "exact")
        for cand, file in chosen:
            if cand.consumed:
                bound.setdefault(cand.consumed[0], []).append((cand, file))
    if is_test(path):
        for file in imported:
            if not is_test(file):
                add(me, target(file), "tests", "heuristic")

    for ref in facts.refs:
        found = _reference_target(ref.name, path, own, bound, index, cache)
        if found is None:
            continue
        file, name = found
        src = node_id(repo_id, "symbol", path, ref.scope) if ref.scope in own else me
        add(src, node_id(repo_id, "symbol", file, name), "references", "heuristic")

    return sorted(nodes.values(), key=lambda n: n.id), sorted(edges.values(), key=lambda e: (e.src, e.dst, e.kind))
