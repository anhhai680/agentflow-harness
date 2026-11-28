"""The text and JSON views of `agentflow graph`: what a build, the index's status, a search and a node look like.

Pure: every function takes values and returns lines or a dict, and reads no file, no clock and no store. The store's
contents are untrusted (a path or a name comes from a repository, a planted store can hold anything), so each line
is built from `doctor.printable` pieces: an escape sequence or a bidirectional-format character in a path is written
as a backslash escape. The JSON views are plain data: `json.dumps` with its default `ensure_ascii` writes every
control and every non-ASCII character as a `\\uXXXX` escape, which is why they need no `printable` pass (it would
double the escapes).
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Mapping, Sequence
from typing import TYPE_CHECKING

from agentflow.doctor import printable
from agentflow.graph_store import HAS_NODES

if TYPE_CHECKING:
    from agentflow.graph_build import Result
    from agentflow.graph_model import Edge, Node
    from agentflow.graph_store import FileRow, Hit

NO_INDEX = "graph: no index yet: run `agentflow graph build`"
DISABLED = "graph.enabled is false: set it to true in .agentflow/config.yaml to build the index"
OFF = f"error: {DISABLED}"
SHA_CHARS = 12
NOTES_SHOWN = 20
TEXT_CHARS = 80  # a hit's text, and a node's in `show`
_LEFT_OUT_NAMES = (("ignored", ".gitignore"), ("secret", "secret"), ("vendored", "vendored"), ("excluded", "excluded"))
_OTHER_STATES = ("symlink", "submodule")
_EXACT, _NOT_PARSED = "exact", "not parsed"


def sha12(commit: object) -> str:
    return printable(str(commit)[:SHA_CHARS])


def _int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _text(value: object, limit: int | None = None) -> str:
    """One line of `value`: white space collapsed, cut to `limit` characters."""
    line = " ".join(str(value).split())
    return line[:limit] if limit is not None else line


# --- shared pieces ---


def language_states(rows: Iterable[FileRow], offered: Mapping[str, str]) -> dict[str, str]:
    """Each language a file with nodes has, `exact` when the last build's parser offered it, else `not parsed`."""
    langs = {row.lang for row in rows if row.state in HAS_NODES and isinstance(row.lang, str) and row.lang}
    return {lang: _EXACT if lang in offered else _NOT_PARSED for lang in sorted(langs)}


def languages_line(languages: Mapping[str, str]) -> str:
    """`languages: python exact · javascript not parsed (no grammar)`: the parsed languages first, then the others."""
    shown = [f"{printable(lang)} exact" for lang, how in sorted(languages.items()) if how == _EXACT]
    shown += [f"{printable(lang)} not parsed (no grammar)" for lang, how in sorted(languages.items()) if how != _EXACT]
    return "languages: " + (" · ".join(shown) if shown else "none")


def left_out_counts(states: Mapping[str, int]) -> dict[str, int]:
    """The `files` rows by state, for every state that has no nodes."""
    return {state: _int(n) for state, n in sorted(states.items()) if state not in HAS_NODES and _int(n) > 0}


def left_out_line(left_out: Mapping[str, int]) -> str | None:
    """`left out: .gitignore 2 · secret 1 · vendored 0 · excluded 0 · other 0`, or None when nothing is left out."""
    if not any(left_out.values()):
        return None
    parts = [f"{label} {_int(left_out.get(state, 0))}" for state, label in _LEFT_OUT_NAMES]
    parts.append(f"other {sum(_int(left_out.get(state, 0)) for state in _OTHER_STATES)}")
    return "left out: " + " · ".join(parts)


def _age(seconds: float) -> str:
    if seconds < 60:
        return "just now"
    for size, unit in ((86400, "day"), (3600, "hour"), (60, "minute")):
        if seconds >= size:
            number = int(seconds // size)
            return f"{number} {unit}{'s' if number != 1 else ''} ago"
    return "just now"


def _node_line(node: Node) -> str:
    return f"{printable(node.kind)}  {printable(node.path)}:{_int(node.line)}  {printable(node.name)}"


def node_json(node: Node) -> dict:
    return {"id": str(node.id), "kind": str(node.kind), "name": str(node.name), "path": str(node.path),
            "line": _int(node.line), "end": _int(node.end), "trust": str(node.trust),
            "signature": str(node.signature), "text": str(node.text)}


# --- build ---


def build_lines(result: Result) -> list[str]:
    lines = [f"graph: {sha12(result.commit)}: parsed {result.parsed}, from cache {result.cached}, "
             f"re-resolved {result.resolved}, removed {result.removed} ({result.ms} ms)",
             languages_line(result.languages)]
    if (left := left_out_line(result.left_out)) is not None:
        lines.append(left)
    lines += [f"note: {printable(note)}" for note in result.notes[:NOTES_SHOWN]]
    return lines


# --- status ---


def _built_at(meta: Mapping[str, str]) -> float | None:
    try:
        built = float(meta["built_at"])
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    return built if math.isfinite(built) else None  # a `nan` or `inf` would make `json.dumps` write invalid JSON


def offered(meta: Mapping[str, str]) -> dict[str, str]:
    try:
        raw = json.loads(meta.get("languages", "{}"))
    except (TypeError, ValueError, RecursionError):
        return {}
    return {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}


def status_lines(meta: Mapping[str, str], counts: Mapping[str, int], now: float,
                 languages: Mapping[str, str] | None = None) -> list[str]:
    """The commit and its age, the counts, the languages and what is left out. `languages` is `language_states` of
    the stored files; without it the languages are the ones the last build's parser offered."""
    built = _built_at(meta)
    age = f", built {_age(max(now - built, 0.0))}" if built is not None else ""
    states = {key.partition(":")[2]: n for key, n in counts.items() if key.startswith("files:")}
    shown = languages if languages is not None else {lang: _EXACT for lang in offered(meta)}
    lines = [f"graph: {sha12(meta.get('commit', ''))}{age}",
             f"files {_int(counts.get('files'))} · nodes {_int(counts.get('nodes'))} · edges {_int(counts.get('edges'))}",
             languages_line(shown)]
    if (left := left_out_line(left_out_counts(states))) is not None:
        lines.append(left)
    return lines


def status_json(meta: Mapping[str, str], counts: Mapping[str, int], now: float,
                languages: Mapping[str, str] | None = None) -> dict:
    built = _built_at(meta)
    states = {key.partition(":")[2]: n for key, n in counts.items() if key.startswith("files:")}
    return {"commit": str(meta.get("commit", "")), "built_at": built,
            "age_s": None if built is None else max(now - built, 0.0), "engine": str(meta.get("engine", "")),
            "files": _int(counts.get("files")), "nodes": _int(counts.get("nodes")), "edges": _int(counts.get("edges")),
            "left_out": left_out_counts(states),
            "languages": dict(languages) if languages is not None else {lang: _EXACT for lang in offered(meta)}}


# --- query ---


def query_lines(hits: Sequence[Hit], commit: str) -> list[str]:
    if not hits:
        return ["no match"]
    lines = [f"# index at {sha12(commit)}"]
    for hit in hits:
        line = _node_line(hit.node)
        text = _text(hit.node.text, TEXT_CHARS)
        lines.append(f"{line} — {printable(text)}" if text else line)
    return lines


def _score(value: object) -> float:
    try:
        score = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return score if math.isfinite(score) else 0.0


def query_json(hits: Sequence[Hit], commit: str) -> dict:
    return {"commit": str(commit), "hits": [{**node_json(hit.node), "score": _score(hit.score)} for hit in hits]}


# --- show ---


def _edge_line(edge: Edge, far: Node) -> str:
    arrow = "->" if edge.dst == far.id else "<-"  # `->`: the edge runs from the node nearer the target to this one
    return f"  {arrow} {printable(edge.kind)} [{printable(edge.confidence)}]  {_node_line(far)}"


def show_lines(target: Node, around: Sequence[tuple[Edge, Node]], commit: str) -> list[str]:
    lines = [f"# index at {sha12(commit)}", _node_line(target)]
    if signature := _text(target.signature):
        lines.append(f"  {printable(signature)}")
    if text := _text(target.text, TEXT_CHARS):
        lines.append(f"  {printable(text)}")
    lines += [_edge_line(edge, far) for edge, far in around]
    return lines


def show_json(target: Node, around: Sequence[tuple[Edge, Node]], commit: str) -> dict:
    return {"commit": str(commit), "node": node_json(target),
            "edges": [{"direction": "to" if edge.dst == far.id else "from", "kind": str(edge.kind),
                       "confidence": str(edge.confidence), "origin": str(edge.origin), "node": node_json(far)}
                      for edge, far in around]}


def no_node_line(target: str, state: str | None = None) -> str:
    """`no node: <path> is left out (<state>)` for a path the index holds without nodes, else `no node: <target>`."""
    if state is not None and state not in HAS_NODES:
        return f"no node: {printable(target)} is left out ({printable(state)})"
    return f"no node: {printable(target)}"
