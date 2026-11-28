"""A pure `.gitignore` matcher for the knowledge graph: which tracked paths a repository's own ignore files name.

`Ignore` is built from the text of each `.gitignore` (the caller reads them from git objects, so this module reads no
file) and answers `ignored(path)` the way `git check-ignore` does. The same class holds the pack's `graph.exclude`
patterns, passed as one root file.

The patterns are untrusted repository text, so nothing here uses a regular expression. A pattern is split on `/` into
segments and each segment is compiled to a short token list. A segment is matched with the two-pointer wildcard walk,
which is `O(len(text) * len(pattern))` however many `*` it holds (a backtracking regular expression is not: `a*a*a*b`
on a long run of `a` goes polynomial). `**` segments are matched by a set walk over the path's segments, so repetition
is never nested. Caps bound the rest: a file over `MAX_FILE_BYTES` or `MAX_LINES` contributes nothing, and a pattern
over `MAX_PATTERN` is skipped, each with one note.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

MAX_FILE_BYTES, MAX_LINES, MAX_PATTERN = 256 * 1024, 5000, 512

_STAR = ("*",)
_ANY = ("?",)
_POSIX = {
    "alnum": str.isalnum, "alpha": str.isalpha, "digit": str.isdigit, "lower": str.islower, "upper": str.isupper,
    "space": str.isspace, "blank": lambda c: c in " \t", "xdigit": lambda c: c in "0123456789abcdefABCDEF",
    "punct": lambda c: c.isascii() and c.isprintable() and not c.isalnum() and c != " ",
}


@dataclass(frozen=True)
class _Rule:
    segments: tuple  # per segment: "**" (any directories) or a token tuple; for an unanchored pattern, one entry
    anchored: bool
    dir_only: bool
    negate: bool


def _class(text: str, at: int):
    """The class opening at `text[at] == "["`: (token, index after it), or None when it never closes."""
    i = at + 1
    negate = i < len(text) and text[i] in "!^"
    i += negate
    ranges: list[tuple[str, str]] = []
    named: list[str] = []
    first = True
    while i < len(text):
        c = text[i]
        if c == "]" and not first:
            return ("[", negate, tuple(ranges), tuple(named)), i + 1
        first = False
        if c == "[" and text.startswith(":", i + 1) and (end := text.find(":]", i + 2)) > 0:
            name = text[i + 2 : end]
            if name in _POSIX:
                named.append(name)
                i = end + 2
                continue
        if c == "\\" and i + 1 < len(text):
            i += 1
            c = text[i]
        if i + 2 < len(text) and text[i + 1] == "-" and text[i + 2] != "]":
            hi = text[i + 2]
            if hi == "\\" and i + 3 < len(text):
                hi = text[i + 3]
                i += 1
            ranges.append((c, hi))
            i += 3
        else:
            ranges.append((c, c))
            i += 1
    return None


def _tokens(segment: str) -> tuple:
    """One path segment's glob as tokens: ("c", char), `?`, `*` (runs of `*` collapse) and classes."""
    out: list[tuple] = []
    i = 0
    while i < len(segment):
        c = segment[i]
        if c == "*":
            if not out or out[-1] != _STAR:
                out.append(_STAR)
        elif c == "?":
            out.append(_ANY)
        elif c == "[" and (made := _class(segment, i)) is not None:
            out.append(made[0])
            i = made[1]
            continue
        else:
            if c == "\\" and i + 1 < len(segment):
                i += 1
                c = segment[i]
            out.append(("c", c))
        i += 1
    return tuple(out)


def _one(token: tuple, char: str) -> bool:
    kind = token[0]
    if kind == "c":
        return token[1] == char
    if kind == "?":
        return True
    hit = any(lo <= char <= hi for lo, hi in token[2]) or any(_POSIX[name](char) for name in token[3])
    return hit != token[1]


def _glob(tokens: tuple, text: str) -> bool:
    """Whether `text` (no `/`) matches the tokens: the wildcard walk that backtracks to the last `*` only."""
    ti = si = 0
    star = mark = -1
    while si < len(text):
        if ti < len(tokens) and tokens[ti] == _STAR:
            star, mark = ti, si
            ti += 1
        elif ti < len(tokens) and _one(tokens[ti], text[si]):
            ti += 1
            si += 1
        elif star >= 0:
            mark += 1
            ti, si = star + 1, mark
        else:
            return False
    while ti < len(tokens) and tokens[ti] == _STAR:
        ti += 1
    return ti == len(tokens)


def _match(rule: _Rule, parts: list[str]) -> bool:
    """Whether the rule matches the path made of `parts` (relative to the rule's directory)."""
    if not rule.anchored:
        return _glob(rule.segments[0], parts[-1])
    count = len(parts)
    here = {0}
    last = len(rule.segments) - 1
    for n, segment in enumerate(rule.segments):
        if segment == "**":
            lowest = min(here) + (1 if n == last else 0)
            here = set(range(lowest, count + 1))
        else:
            here = {j + 1 for j in here if j < count and _glob(segment, parts[j])}
        if not here:
            return False
    return count in here


def _clean(line: str) -> str:
    """The line without its unescaped trailing spaces."""
    end = len(line)
    while end > 0 and line[end - 1] == " ":
        slashes = 0
        while end - 2 - slashes >= 0 and line[end - 2 - slashes] == "\\":
            slashes += 1
        if slashes % 2:
            break
        end -= 1
    return line[:end]


def _rule(line: str) -> _Rule | None:
    if not line or line.startswith("#"):
        return None
    negate = line.startswith("!")
    if negate:
        line = line[1:]
    dir_only = line.endswith("/")
    line = line.rstrip("/") if dir_only else line
    anchored = "/" in line
    line = line.lstrip("/")
    if not line:
        return None
    if not anchored:
        return _Rule((_tokens(line),), False, dir_only, negate)
    return _Rule(tuple("**" if seg == "**" else _tokens(seg) for seg in line.split("/")), True, dir_only, negate)


def _parse(directory: str, text: str, notes: list[str]) -> list[_Rule]:
    name = f"{directory}/.gitignore" if directory else ".gitignore"
    if len(text.encode("utf-8", "replace")) > MAX_FILE_BYTES:
        notes.append(f"{name}: over {MAX_FILE_BYTES} bytes, left out")
        return []
    lines = text.split("\n")
    if len(lines) > MAX_LINES:
        notes.append(f"{name}: over {MAX_LINES} lines, left out")
        return []
    rules: list[_Rule] = []
    skipped = 0
    for line in lines:
        line = _clean(line)
        if len(line) > MAX_PATTERN:
            skipped += 1
            continue
        if (rule := _rule(line)) is not None:
            rules.append(rule)
    if skipped:
        notes.append(f"{name}: {skipped} pattern(s) over {MAX_PATTERN} characters left out")
    return rules


class Ignore:
    """The ignore files of one tree. `files` maps a `.gitignore`'s directory ("" is the root) to its text."""

    def __init__(self, files: Mapping[str, str]) -> None:
        self.notes: list[str] = []
        self._rules: dict[str, list[_Rule]] = {}
        named = sorted(((d.strip("/"), text) for d, text in files.items()), key=lambda f: (f[0].count("/") + 1 if f[0] else 0, f[0]))
        for directory, text in named:
            if rules := _parse(directory, text, self.notes):
                self._rules.setdefault(directory, []).extend(rules)
        self._dirs: dict[str, bool] = {}

    def _verdict(self, path: str, is_dir: bool) -> bool:
        """The last matching rule's answer for `path`, over the files above it, shallowest first."""
        parts = path.split("/")
        verdict = False
        for depth in range(len(parts)):
            directory = "/".join(parts[:depth])
            rules = self._rules.get(directory)
            if not rules:
                continue
            below = parts[depth:]
            for rule in rules:
                if (is_dir or not rule.dir_only) and _match(rule, below):
                    verdict = not rule.negate
        return verdict

    def _dir_ignored(self, directory: str) -> bool:
        """Whether `directory` or one above it is ignored, remembered per directory."""
        parts = directory.split("/") if directory else []
        above = False
        for depth in range(1, len(parts) + 1):
            current = "/".join(parts[:depth])
            if current not in self._dirs:
                self._dirs[current] = above or self._verdict(current, True)
            above = self._dirs[current]
        return above

    def ignored(self, path: str) -> bool:
        """Whether the file `path` is ignored, itself or through an ignored directory above it."""
        path = path.strip("/")
        if not path:
            return False
        parent, _, _ = path.rpartition("/")
        return self._dir_ignored(parent) or self._verdict(path, False)
