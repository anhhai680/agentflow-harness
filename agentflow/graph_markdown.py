"""Markdown facts: a doc's sections, decisions, questions, requirements, relative links and Status line, as `graph_model.Facts`.

`extract` reads the bytes as strict UTF-8 and splits them on `\\n` only, the way `graph_model.check_facts` counts lines, so a
file of lone CRs is one line. A line that opens a code fence hides everything up to its closing fence from the scan.
It makes one linear pass with no recursion and no backtracking pattern, and it never raises: a file that does not decode gives
`empty("markdown", "UnicodeDecodeError")`.

A `Section` is one ATX heading, running to the line before the next heading of the same or a higher level, with its own
body (up to the next heading of any level) as `text`. A list item becomes a `decision`, `question` or `requirement`
`Section` under the headings and the `**Resolved (...)**` line the proposal format uses. A record that `check_facts` would
refuse (a link target outside the specifier characters) is dropped and counted in a note, and each list is cut to its cap
with a note, so the facts are always accepted.

The worker process imports this module, so it uses the standard library and `graph_model` only.
"""

from __future__ import annotations

import re

from agentflow.graph_model import (
    DOC_MAX,
    MAX_LINKS,
    MAX_SECTIONS,
    NAME_MAX,
    SECTION_LEVEL_MAX,
    SECTION_TEXT_MAX,
    Facts,
    Link,
    Section,
    empty,
    specifier,
    text,
)

_HEADING = re.compile(r" {0,3}(#{1,6})(?=[ \t]|$)")
_FENCE = re.compile(r" {0,3}(`{3,}|~{3,})")
_ITEM = re.compile(r"( {0,3})(?:[-+*]|\d{1,9}[.)])(?:[ \t]+|$)")
_LINK = re.compile(r"\[[^\[\]]{0,1000}\]\(([^()\s<>]{1,400})(?:\s+\"[^\"]{0,200}\")?\)")
_CODE_SPAN = re.compile(r"`[^`]*`")
_SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*:")
_MODES = (("decisions", "decision"), ("open questions", "question"), ("success criteria", "requirement"))
_RESOLVED_MIN = len("**Resolved**")


def extract(path: str, data: bytes) -> Facts:
    """The sections, links and Status line of the Markdown `data`. Pure; never raises.

    `path` is only the file's name for the caller: it is not read.
    """
    del path
    try:
        lines = [line[:-1] if line.endswith("\r") else line for line in data.decode("utf-8-sig").split("\n")]
        scan = _Scan(len(lines))
        for number, line in enumerate(lines, 1):
            scan.feed(number, line)
        return scan.facts()
    except Exception as exc:  # the contract is "never raises": whatever a decode or scan bug throws is one note
        return empty("markdown", type(exc).__name__)


class _Body:
    """The words of some lines, whitespace collapsed, kept only up to the limit."""

    __slots__ = ("parts", "size")

    def __init__(self) -> None:
        self.parts: list[str] = []
        self.size = 0

    def add(self, line: str) -> None:
        if self.size > SECTION_TEXT_MAX:
            return
        words = " ".join(text(line, len(line)).split())  # controls become spaces first, so none edge the words
        if words:
            self.parts.append(words)
            self.size += len(words) + 1

    def value(self) -> str:
        return text(" ".join(self.parts), SECTION_TEXT_MAX)


class _Rec:
    """One section being built: a heading or a list item, with its end still moving."""

    __slots__ = ("title", "level", "line", "end", "kind", "body")

    def __init__(self, title: str, level: int, line: int, end: int, kind: str) -> None:
        self.title, self.level, self.line, self.end, self.kind = text(title, NAME_MAX), level, line, end, kind
        self.body = _Body()

    def section(self) -> Section:
        return Section(self.title, self.level, self.line, self.end, self.kind, self.body.value())


class _Item:
    """The list item that later lines may still continue."""

    __slots__ = ("rec", "indent", "fenced")

    def __init__(self, rec: _Rec, indent: int) -> None:
        self.rec, self.indent, self.fenced = rec, indent, False


class _Scan:
    def __init__(self, lines: int) -> None:
        self.lines = lines
        self.fence: tuple[str, int] | None = None
        self.recs: list[_Rec] = []
        self.stack: list[_Rec] = []  # the headings still open: levels strictly rise, so at most six
        self.cur: _Rec | None = None  # the latest heading, whose body the plain lines feed
        self.level = 0  # the latest heading's level
        self.mode: str | None = None
        self.mode_level = 0
        self.item: _Item | None = None
        self.gap = False  # a blank line since the open item's last line
        self.doc = ""
        self.status = ""
        self.status_open = True
        self.links: list[Link] = []
        self.dropped = 0
        self.sections_cut = False
        self.links_cut = False

    def feed(self, number: int, line: str) -> None:
        if self.fence is not None:
            self._in_fence(number, line)
            return
        opening = _FENCE.match(line)
        if opening is not None and not (opening.group(1)[0] == "`" and "`" in line[opening.end() :]):
            self._open_fence(number, line, (opening.group(1)[0], len(opening.group(1))))
            return
        heading = _HEADING.match(line)
        if heading is not None:
            self._heading(number, len(heading.group(1)), line[heading.end() :])
            return
        if self.cur is not None:
            self.cur.body.add(line)
        self._scan_links(number, line)
        if self.status_open and line.startswith("Status:"):
            self.status, self.status_open = line[len("Status:") :].strip(), False
        self._item_line(number, line)

    # --- code fences ---

    def _open_fence(self, number: int, line: str, fence: tuple[str, int]) -> None:
        if self.cur is not None:
            self.cur.body.add(line)
        item = self.item
        if item is not None and _indent(line) > item.indent:
            item.fenced, item.rec.end = True, number
        else:
            self.item = None
        self.fence = fence

    def _in_fence(self, number: int, line: str) -> None:
        if self.cur is not None:
            self.cur.body.add(line)
        item = self.item
        if item is not None and item.fenced:
            item.rec.end = number
        closing = _FENCE.match(line)
        char, size = self.fence or ("", 0)
        if closing is not None and closing.group(1)[0] == char and len(closing.group(1)) >= size and not line[closing.end() :].strip():
            self.fence = None
            self.gap = False
            if item is not None:
                item.fenced = False

    # --- headings ---

    def _heading(self, number: int, level: int, rest: str) -> None:
        title = _atx_title(rest)
        self.item, self.gap = None, False
        while self.stack and self.stack[-1].level >= level:
            self.stack.pop().end = number - 1
        rec = _Rec(title, level, number, self.lines, "section")
        self.stack.append(rec)
        self._emit(rec)
        self.cur, self.level = rec, level
        if level == 1 and not self.doc:
            self.doc = title
        if level == 2:
            self.status_open = False
        mode = _mode_of(title)
        if mode is not None:
            self.mode, self.mode_level = mode, level
        elif self.mode is None or level <= self.mode_level:
            self.mode = None

    def _emit(self, rec: _Rec) -> None:
        if len(self.recs) >= MAX_SECTIONS:
            self.sections_cut = True
        else:
            self.recs.append(rec)

    # --- list items ---

    def _item_line(self, number: int, line: str) -> None:
        stripped = line.strip()
        if not stripped:
            self.gap = self.item is not None
            return
        if _resolved(stripped):
            self.item, self.mode, self.mode_level = None, "decision", max(self.level, 1)
            return
        if _thematic(stripped):
            self.item = None
            return
        found = _ITEM.match(line)
        item = self.item
        if found is not None and (item is None or len(found.group(1)) <= item.indent):
            self._start_item(number, len(found.group(1)), line[found.end() :])
        elif item is not None and (_indent(line) > item.indent or not self.gap):
            item.rec.end, self.gap = number, False
            item.rec.body.add(line)
        else:
            self.item = None

    def _start_item(self, number: int, indent: int, rest: str) -> None:
        self.gap = False
        title = " ".join(text(rest, len(rest)).replace("**", "").split())
        if self.mode is None or not title:
            self.item = None
            return
        rec = _Rec(title, min(self.level + 1, SECTION_LEVEL_MAX), number, number, self.mode)
        rec.body.add(title)
        self._emit(rec)
        self.item = _Item(rec, indent)

    # --- links ---

    def _scan_links(self, number: int, line: str) -> None:
        if self.links_cut or "](" not in line:
            return
        if "`" in line:
            line = _CODE_SPAN.sub("", line)
        for found in _LINK.finditer(line):
            raw = _relative(found.group(1))
            if raw is None:
                continue
            target = specifier(raw)
            if target is None:
                self.dropped += 1
            elif len(self.links) >= MAX_LINKS:
                self.links_cut = True
                return
            else:
                self.links.append(Link(target, number))

    def facts(self) -> Facts:
        notes = []
        if self.sections_cut:
            notes.append(f"cut sections to {MAX_SECTIONS}")
        if self.links_cut:
            notes.append(f"cut links to {MAX_LINKS}")
        if self.dropped:
            notes.append(f"dropped {self.dropped} links with targets outside [A-Za-z0-9_@./-] or longer than 200 characters")
        return Facts(
            lang="markdown",
            doc=text(self.doc, DOC_MAX),
            sections=tuple(rec.section() for rec in self.recs),
            links=tuple(self.links),
            status=text(self.status, DOC_MAX),
            notes=tuple(notes),
        )


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def _atx_title(rest: str) -> str:
    """A heading's title: the text after the hashes, without a closing run of hashes."""
    title = rest.strip()
    body = title.rstrip("#")
    if body != title and (not body or body[-1] in " \t"):
        return body.rstrip()
    return title


def _mode_of(title: str) -> str | None:
    low = title.lstrip("*_ ").lower()
    for prefix, mode in _MODES:
        if low.startswith(prefix):
            return mode
    return None


def _resolved(stripped: str) -> bool:
    return len(stripped) >= _RESOLVED_MIN and stripped.startswith("**Resolved") and stripped.endswith("**")


def _thematic(stripped: str) -> bool:
    marks = stripped.replace(" ", "").replace("\t", "")
    return len(marks) >= 3 and marks[0] in "-*_" and marks.count(marks[0]) == len(marks)


def _relative(raw: str) -> str | None:
    """The path of a link target with its fragment and query removed, or None when it is not relative."""
    if raw[0] in "/#?" or _SCHEME.match(raw):
        return None
    return raw.split("#", 1)[0].split("?", 1)[0] or None
