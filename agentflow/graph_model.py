"""The knowledge graph's vocabulary: its kinds, the `Facts` an extractor returns, and the check that distrusts them.

An extractor (Python `ast`, Markdown, manifests, tree-sitter) reads one file's bytes and returns `Facts`. For
tree-sitter that happens in a child process that sends the facts back as JSON, so what arrives is untrusted:
`check_facts` accepts it only when it is a mapping of exactly the `Facts` fields, every list is within its cap,
every name is plain ASCII, every enumerated field is known and every line lies inside the file. Free text
(signatures, doc lines, section text) is cleaned of control characters and cut to its limit, never refused.
`Node` and `Edge` are what the resolver builds from the facts.

The worker process imports this module, so it is stdlib only and imports nothing from the engine.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

SCHEMA_VERSION = 1
NODE_KINDS = ("file", "symbol", "doc", "section", "decision", "question", "requirement", "package", "provides", "needs")
EDGE_KINDS = ("contains", "imports", "references", "tests", "depends_on", "mentions")
LANGUAGES = ("python", "typescript", "tsx", "javascript", "markdown", "manifest")
EXTRACTOR_VERSIONS = {  # bumped with any change to an extractor's output, so cached facts are extracted again
    "python": "2",  # 2: no default's or class keyword's value in a signature
    "typescript": "2",  # 2: no parameter initialiser in a signature
    "tsx": "2",
    "javascript": "2",
    "markdown": "1",
    "manifest": "1",
}

DEF_KINDS = ("function", "class", "method", "constant")
SECTION_KINDS = ("section", "decision", "question", "requirement")
DEP_GROUPS = ("runtime", "dev", "optional")
ECOSYSTEMS = ("", "pypi", "npm")

NAME_MAX, SIGNATURE_MAX, DOC_MAX, SECTION_TEXT_MAX = 200, 300, 200, 4000
MAX_DEFS, MAX_IMPORTS, MAX_REFS, MAX_SECTIONS, MAX_LINKS, MAX_DEPS, MAX_NOTES = 5000, 2000, 20000, 2000, 2000, 2000, 20
NOTE_MAX = SIGNATURE_MAX
SECTION_LEVEL_MAX = 6  # a Markdown heading is 1 to 6; 0 is for text that comes before the first heading

_IDENT = re.compile(r"[A-Za-z_$][A-Za-z0-9_$.]{0,199}")
_MODULE = re.compile(r"[A-Za-z0-9_@./-]{1,200}")
_BREAKS = "  "


@dataclass(frozen=True)
class Def:
    name: str
    kind: str  # function | class | method | constant
    line: int
    end: int
    signature: str
    doc: str
    exported: bool


@dataclass(frozen=True)
class Import:
    module: str
    name: str
    alias: str
    level: int
    line: int  # one record per bound name; `import a.b` binds alias "a"


@dataclass(frozen=True)
class Ref:
    name: str  # a dotted name as written
    line: int
    scope: str  # the enclosing Def.name, or ""


@dataclass(frozen=True)
class Section:
    title: str
    level: int
    line: int
    end: int
    kind: str  # section | decision | question | requirement
    text: str


@dataclass(frozen=True)
class Link:
    target: str  # a relative link target, fragment removed
    line: int


@dataclass(frozen=True)
class Dep:
    name: str
    spec: str
    group: str  # runtime | dev | optional


@dataclass(frozen=True)
class Facts:
    lang: str
    doc: str = ""
    defs: tuple[Def, ...] = ()
    imports: tuple[Import, ...] = ()
    refs: tuple[Ref, ...] = ()
    sections: tuple[Section, ...] = ()
    links: tuple[Link, ...] = ()
    deps: tuple[Dep, ...] = ()
    package: str = ""
    ecosystem: str = ""
    status: str = ""
    notes: tuple[str, ...] = field(default=())


@dataclass(frozen=True)
class Node:
    id: str
    kind: str
    name: str
    path: str
    line: int
    end: int
    trust: str
    signature: str
    text: str


@dataclass(frozen=True)
class Edge:
    src: str
    dst: str
    kind: str
    confidence: str
    origin: str  # the path whose facts made it


def node_id(repo_id: str, kind: str, path: str, name: str = "") -> str:
    """`<repo_id>:<kind>:<path>#<name>`; a file's or doc's id has an empty name."""
    return f"{repo_id}:{kind}:{path}#{name}"


# --- cleaning and the name checks ---


def text(value: object, limit: int) -> str:
    """`value` as a str with every C0/C1 control, DEL and line break replaced by one space, cut to `limit`.

    Anything that is not a str becomes "".
    """
    if not isinstance(value, str):
        return ""
    out = "".join(" " if (ord(ch) < 0x20 or 0x7F <= ord(ch) <= 0x9F or ch in _BREAKS) else ch for ch in value)
    return out[: max(limit, 0)]


def ident(text: object) -> str | None:
    """`text` when it is an ASCII identifier or dotted name (`[A-Za-z_$][A-Za-z0-9_$.]{0,199}`), else None."""
    if isinstance(text, str) and _IDENT.fullmatch(text):
        return text
    return None


def module(text: object) -> str | None:
    """`text` when it is `[A-Za-z0-9_@./-]{1,200}` with no `..` segment, else None."""
    if not isinstance(text, str) or not _MODULE.fullmatch(text):
        return None
    if ".." in text.split("/"):
        return None
    return text


def specifier(text: object) -> str | None:
    """`text` when it is a module specifier or relative path an extractor may keep, else None.

    Zero or more leading `./` or `../` segments, then `[A-Za-z0-9_@./-]` with no `.` or `..` segment after
    them, 1 to 200 characters, and never absolute. `"../types"`, `"./api/client"`, `"react"`, `"@acme/web"`
    and `".."` pass; `"a/../b"` and `"/etc/x"` do not. An extractor calls this to drop a record that would
    fail `check_facts`, with a counted note.
    """
    if not isinstance(text, str) or not _MODULE.fullmatch(text) or text.startswith("/"):
        return None
    parts = text.split("/")
    i = 0
    while i < len(parts) and parts[i] in (".", ".."):
        i += 1
    if any(part in (".", "..") for part in parts[i:]):
        return None
    return text


# --- Facts to and from JSON ---


def empty(lang: str, *notes: str) -> Facts:
    """Facts with nothing in them but the language and (cleaned, capped) notes."""
    return Facts(lang=lang, notes=_clean_notes(notes))


def _clean_notes(notes: object) -> tuple[str, ...]:
    out: list[str] = []
    for note in notes if isinstance(notes, (list, tuple)) else ():
        cleaned = text(note, NOTE_MAX)
        if cleaned:
            out.append(cleaned)
        if len(out) >= MAX_NOTES:
            break
    return tuple(out)


def to_json(facts: Facts) -> dict:
    """`facts` as plain JSON data: every record a dict, every sequence a list."""
    return {
        "lang": facts.lang,
        "doc": facts.doc,
        "defs": [vars(d).copy() for d in facts.defs],
        "imports": [vars(i).copy() for i in facts.imports],
        "refs": [vars(r).copy() for r in facts.refs],
        "sections": [vars(s).copy() for s in facts.sections],
        "links": [vars(link).copy() for link in facts.links],
        "deps": [vars(d).copy() for d in facts.deps],
        "package": facts.package,
        "ecosystem": facts.ecosystem,
        "status": facts.status,
        "notes": list(facts.notes),
    }


_FACT_KEYS = frozenset(to_json(Facts(lang="")))
_DEF_KEYS = frozenset(("name", "kind", "line", "end", "signature", "doc", "exported"))
_IMPORT_KEYS = frozenset(("module", "name", "alias", "level", "line"))
_REF_KEYS = frozenset(("name", "line", "scope"))
_SECTION_KEYS = frozenset(("title", "level", "line", "end", "kind", "text"))
_LINK_KEYS = frozenset(("target", "line"))
_DEP_KEYS = frozenset(("name", "spec", "group"))
JS_LANGS = ("typescript", "tsx", "javascript")


def _int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _record(raw: object, keys: frozenset[str]) -> dict | None:
    if isinstance(raw, dict) and set(raw) == keys:
        return raw
    return None


def _rows(raw: object, cap: int) -> list | None:
    if isinstance(raw, (list, tuple)) and len(raw) <= cap:
        return list(raw)
    return None


def _span(line: object, end: object, lines: int) -> bool:
    return _int(line) and _int(end) and 1 <= line <= end <= lines


def _import_module_ok(lang: str, mod: object, level: int) -> bool:
    if lang == "python":
        return module(mod) is not None or (mod == "" and level > 0)
    if lang in JS_LANGS:
        return specifier(mod) is not None
    return module(mod) is not None


def _def(raw: object, lines: int) -> Def | None:
    r = _record(raw, _DEF_KEYS)
    if r is None or ident(r["name"]) is None or r["kind"] not in DEF_KINDS:
        return None
    if not _span(r["line"], r["end"], lines) or not isinstance(r["exported"], bool):
        return None
    return Def(r["name"], r["kind"], r["line"], r["end"], text(r["signature"], SIGNATURE_MAX), text(r["doc"], DOC_MAX), r["exported"])


def _import(raw: object, lang: str, lines: int) -> Import | None:
    r = _record(raw, _IMPORT_KEYS)
    if r is None or not _int(r["level"]) or r["level"] < 0:
        return None
    if not _import_module_ok(lang, r["module"], r["level"]):
        return None
    if r["name"] not in ("", "*") and ident(r["name"]) is None:
        return None
    if r["alias"] != "" and ident(r["alias"]) is None:
        return None
    if not _span(r["line"], r["line"], lines):
        return None
    return Import(r["module"], r["name"], r["alias"], r["level"], r["line"])


def _ref(raw: object, lines: int) -> Ref | None:
    r = _record(raw, _REF_KEYS)
    if r is None or ident(r["name"]) is None or (r["scope"] != "" and ident(r["scope"]) is None):
        return None
    if not _span(r["line"], r["line"], lines):
        return None
    return Ref(r["name"], r["line"], r["scope"])


def _section(raw: object, lines: int) -> Section | None:
    r = _record(raw, _SECTION_KEYS)
    if r is None or r["kind"] not in SECTION_KINDS or not _span(r["line"], r["end"], lines):
        return None
    if not _int(r["level"]) or not 0 <= r["level"] <= SECTION_LEVEL_MAX:
        return None
    return Section(text(r["title"], NAME_MAX), r["level"], r["line"], r["end"], r["kind"], text(r["text"], SECTION_TEXT_MAX))


def _link(raw: object, lines: int) -> Link | None:
    r = _record(raw, _LINK_KEYS)
    if r is None or specifier(r["target"]) is None or not _span(r["line"], r["line"], lines):
        return None
    return Link(r["target"], r["line"])


def _dep(raw: object) -> Dep | None:
    r = _record(raw, _DEP_KEYS)
    if r is None or module(r["name"]) is None or r["group"] not in DEP_GROUPS:
        return None
    return Dep(r["name"], text(r["spec"], NAME_MAX), r["group"])


def _each(rows: list, make) -> tuple | None:
    out = []
    for row in rows:
        item = make(row)
        if item is None:
            return None
        out.append(item)
    return tuple(out)


def check_facts(raw: object, lang: str, lines: int) -> Facts | None:
    """The `Facts` in `raw` (JSON from an extractor), or None when anything in it is not as it must be.

    `raw` must be a mapping of exactly the `Facts` fields for `lang`, every list within its cap (`notes` are
    cut to `MAX_NOTES` instead, since a chatty extractor is not forging anything), every name an ASCII
    identifier or module path, every `kind`, `group` and `ecosystem` known, every `Import.module` and
    `Link.target` valid for the language, and every line an int inside `1..lines`, with `line <= end`.
    Free text is cleaned with `text()` and cut to its limit, never refused. One bad record refuses the lot:
    an extractor that wants to survive an odd record drops it first, with a note.
    """
    if lang not in LANGUAGES or not _int(lines) or not isinstance(raw, dict) or set(raw) != _FACT_KEYS:
        return None
    if raw["lang"] != lang or raw["ecosystem"] not in ECOSYSTEMS:
        return None
    if raw["package"] != "" and module(raw["package"]) is None:
        return None
    caps = (("defs", MAX_DEFS), ("imports", MAX_IMPORTS), ("refs", MAX_REFS), ("sections", MAX_SECTIONS), ("links", MAX_LINKS), ("deps", MAX_DEPS))
    rows = {name: _rows(raw[name], cap) for name, cap in caps}
    if any(v is None for v in rows.values()) or not isinstance(raw["notes"], (list, tuple)):
        return None
    defs = _each(rows["defs"], lambda r: _def(r, lines))
    imports = _each(rows["imports"], lambda r: _import(r, lang, lines))
    refs = _each(rows["refs"], lambda r: _ref(r, lines))
    sections = _each(rows["sections"], lambda r: _section(r, lines))
    links = _each(rows["links"], lambda r: _link(r, lines))
    deps = _each(rows["deps"], _dep)
    if any(v is None for v in (defs, imports, refs, sections, links, deps)):
        return None
    return Facts(
        lang=lang,
        doc=text(raw["doc"], DOC_MAX),
        defs=defs,
        imports=imports,
        refs=refs,
        sections=sections,
        links=links,
        deps=deps,
        package=raw["package"],
        ecosystem=raw["ecosystem"],
        status=text(raw["status"], DOC_MAX),
        notes=_clean_notes(raw["notes"]),
    )
