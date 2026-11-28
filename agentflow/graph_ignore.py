"""A pure `.gitignore` matcher for the knowledge graph: which tracked paths a repository's own ignore files name.

`Ignore` is built from the text of each `.gitignore` (the caller reads them from git objects, so this module reads no
file) and answers `ignored(path)` the way `git check-ignore` does. The same class holds the pack's `graph.exclude`
patterns, passed as one root file.

The patterns are untrusted repository text, so nothing here uses a regular expression. A pattern is split on `/` into
segments and each segment is compiled to a short token list. A segment is matched with the two-pointer wildcard walk,
which is `O(len(text) * len(pattern))` however many `*` it holds (a backtracking regular expression is not: `a*a*a*b`
on a long run of `a` goes polynomial). `**` segments are matched by a set walk over the path's segments, so repetition
is never nested. Caps bound the rest: a file over `MAX_FILE_BYTES` or `MAX_LINES` contributes nothing, and a pattern
over `MAX_PATTERN` characters, `MAX_SEGMENTS` segments or `MAX_STARS` stars in a segment is skipped, as is every
pattern past the file's `MAX_TOKENS` budget (and past `MAX_HEAVY` tokens of the one kind of segment that can still
backtrack: two or more `*` beside a `?` or a class), each with one note. A path of more than `MAX_DEPTH` segments, or
with a segment over `MAX_NAME` characters, is judged ignored (and noted once), which is the safe side for a graph.
Per path, a rule is dismissed cheaply first (its last segment must match before the earlier ones are walked, and a
rule whose last segment is a plain name is looked up by that name). Whatever the shape of the files, one `ignored`
call spends at most `MAX_STEPS` steps (a step is a rule looked at, a segment compared in a `**` walk, or a step of the
wildcard walk), counted over every rule of every file it consults; a call that runs out judges the path ignored (and
the directory it was inside, which keeps the answer the same for what lies below), with one note.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

MAX_FILE_BYTES, MAX_LINES, MAX_PATTERN = 256 * 1024, 5000, 512
MAX_SEGMENTS, MAX_STARS, MAX_TOKENS, MAX_HEAVY = 16, 8, 10000, 100
MAX_DEPTH, MAX_NAME, MAX_STEPS = 64, 255, 4000

_STAR = ("*",)
_ANY = ("?",)
_POSIX = {
    "alnum": str.isalnum, "alpha": str.isalpha, "digit": str.isdigit, "lower": str.islower, "upper": str.isupper,
    "space": str.isspace, "blank": lambda c: c in " \t", "xdigit": lambda c: c in "0123456789abcdefABCDEF",
    "punct": lambda c: c.isascii() and c.isprintable() and not c.isalnum() and c != " ",
}


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


class _Exhausted(Exception):
    """One `ignored` call spent its steps."""


def _walk(tokens: tuple, text: str, budget: list[int]) -> bool:
    """Whether `text` (no `/`) matches the tokens: the wildcard walk that backtracks to the last `*` only."""
    ti = si = 0
    star = mark = -1
    left = budget[0]
    while si < len(text):
        left -= 1
        if left < 0:
            raise _Exhausted

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
    budget[0] = left
    while ti < len(tokens) and tokens[ti] == _STAR:
        ti += 1
    return ti == len(tokens)


class _Glob:
    """One path segment's glob, with the cheap tests that dismiss most texts before any walk.

    A glob is matched one of four ways, none of which backtracks more than the wildcard walk of the last kind:
    equality (no wildcard), `str.find` over the chunks between stars (literals and stars only), a comparison of
    the tokens before the one star with the text's start and of those after it with the text's end, and the walk.
    """

    __slots__ = ("tokens", "literal", "head", "mid", "tail", "stars", "need", "first", "last", "before", "after", "heavy")

    def __init__(self, tokens: tuple) -> None:
        self.tokens = tokens
        plain = all(t[0] == "c" or t == _STAR for t in tokens)
        self.stars = sum(1 for t in tokens if t == _STAR)
        self.need = len(tokens) - self.stars
        self.literal = "".join(t[1] for t in tokens) if plain and not self.stars else None
        self.head = self.tail = ""
        self.mid: tuple[str, ...] | None = None
        self.before = self.after = ()
        if plain and self.stars:
            pieces = [""]
            for t in tokens:
                if t == _STAR:
                    pieces.append("")
                else:
                    pieces[-1] += t[1]
            self.head, self.tail, self.mid = pieces[0], pieces[-1], tuple(p for p in pieces[1:-1] if p)
        elif not plain and self.stars <= 1:
            at = tokens.index(_STAR) if self.stars else len(tokens)
            self.before, self.after = tokens[:at], tokens[at + 1 :]
        self.heavy = len(tokens) if not plain and self.stars > 1 else 0
        self.first = tokens[0][1] if tokens and tokens[0][0] == "c" else None
        self.last = tokens[-1][1] if tokens and tokens[-1][0] == "c" else None

    def match(self, text: str, budget: list[int]) -> bool:
        if self.literal is not None:
            return text == self.literal
        if len(text) < self.need or (not self.stars and len(text) != self.need):
            return False
        if self.mid is not None:
            if not (text.startswith(self.head) and text.endswith(self.tail)):
                return False
            pos, end = len(self.head), len(text) - len(self.tail)
            if pos > end:
                return False
            budget[0] -= len(self.mid)
            for chunk in self.mid:
                at = text.find(chunk, pos, end)
                if at < 0:
                    return False
                pos = at + len(chunk)
            return True
        if (self.first is not None and text[0] != self.first) or (self.last is not None and text[-1] != self.last):
            return False
        if not self.heavy:
            tail = text[len(text) - len(self.after) :] if self.after else ""
            return all(map(_one, self.before, text)) and all(map(_one, self.after, tail))
        return _walk(self.tokens, text, budget)


@dataclass(frozen=True)
class _Rule:
    segments: tuple  # per segment: "**" (any directories) or a _Glob; an unanchored pattern has one _Glob
    anchored: bool
    dir_only: bool
    negate: bool
    need: int  # fewest path segments it can match
    fixed: bool  # no `**`: it matches paths of exactly len(segments) segments

    @property
    def name(self) -> str | None:
        """The one name its last segment accepts, when that is a plain name."""
        last = self.segments[-1]
        return None if last == "**" else last.literal


def _match(rule: _Rule, parts: list[str], budget: list[int]) -> bool:
    """Whether the rule matches the path made of `parts` (relative to the rule's directory)."""
    segments = rule.segments
    if not rule.anchored:
        return segments[0].match(parts[-1], budget)
    count = len(parts)
    last = segments[-1]
    first = segments[0]
    if (
        count < rule.need
        or (rule.fixed and count != len(segments))
        or (last != "**" and not last.match(parts[-1], budget))
        or (first != "**" and not first.match(parts[0], budget))
    ):
        return False
    here = {0}
    final = len(segments) - 1
    for n, segment in enumerate(segments):
        budget[0] -= len(here)
        if budget[0] < 0:
            raise _Exhausted
        if segment == "**":
            here = set(range(min(here) + (1 if n == final else 0), count + 1))
        else:
            here = {j + 1 for j in here if j < count and segment.match(parts[j], budget)}
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
    """The rule one cleaned line stands for, or None for a line that holds none."""
    if not line or line.startswith("#"):
        return None
    negate = line.startswith("!")
    if negate:
        line = line[1:]
    dir_only = line.endswith("/")
    line = line[:-1] if dir_only else line
    anchored = "/" in line
    line = line.removeprefix("/")
    if not line or "" in line.split("/"):  # an empty segment (`a//b`, `c//`) names nothing, as in git
        return None
    if not anchored:
        return _Rule((_Glob(_tokens(line)),), False, dir_only, negate, 1, True)
    segments: list = []
    for seg in line.split("/"):
        if seg == "**":
            if not segments or segments[-1] != "**":  # a run of `**` is one
                segments.append("**")
        else:
            segments.append(_Glob(_tokens(seg)))
    stars = segments.count("**")
    need = len(segments) - stars + (1 if segments[-1] == "**" else 0)
    return _Rule(tuple(segments), True, dir_only, negate, need, not stars)


def _cost(rule: _Rule) -> int:
    """What the rule adds to a file's budget: its tokens, and more when `**` makes it walk the path's segments."""
    tokens = sum(1 if seg == "**" else 1 + len(seg.tokens) for seg in rule.segments)
    return tokens if rule.fixed else tokens + 4 * len(rule.segments)


def _heavy(rule: _Rule) -> int:
    return sum(seg.heavy for seg in rule.segments if seg != "**")


def _parse(directory: str, text: str, notes: list[str]) -> list[_Rule]:
    name = f"{directory}/.gitignore" if directory else ".gitignore"
    if len(text.encode("utf-8", "replace")) > MAX_FILE_BYTES:
        notes.append(f"{name}: over {MAX_FILE_BYTES} bytes, left out")
        return []
    lines = text.removeprefix("\ufeff").split("\n")
    if len(lines) > MAX_LINES:
        notes.append(f"{name}: over {MAX_LINES} lines, left out")
        return []
    rules: list[_Rule] = []
    skipped = budget = heavy = 0
    for line in lines:
        line = _clean(line.removesuffix("\r"))
        if len(line) > MAX_PATTERN:
            skipped += 1
            continue
        rule = _rule(line)
        if rule is None:
            continue
        cost = _cost(rule)
        if (
            len(rule.segments) > MAX_SEGMENTS
            or any(seg != "**" and seg.stars > MAX_STARS for seg in rule.segments)
            or budget + cost > MAX_TOKENS
            or heavy + _heavy(rule) > MAX_HEAVY
        ):
            skipped += 1
            continue
        budget += cost
        heavy += _heavy(rule)
        rules.append(rule)
    if skipped:
        notes.append(
            f"{name}: {skipped} pattern(s) left out (over {MAX_PATTERN} characters, {MAX_SEGMENTS} segments,"
            f" {MAX_STARS} stars in a segment or the file's budget of {MAX_TOKENS} tokens)"
        )
    return rules


class _Index:
    """One directory's rules in file order: those that accept one plain name by that name, the rest apart."""

    __slots__ = ("named", "wild")

    def __init__(self, rules: list[_Rule]) -> None:
        self.named: dict[str, list[tuple[int, _Rule]]] = {}
        self.wild: list[tuple[int, _Rule]] = []
        for n, rule in enumerate(rules):
            name = rule.name
            if name is None:
                self.wild.append((n, rule))
            else:
                self.named.setdefault(name, []).append((n, rule))

    def verdict(self, below: list[str], is_dir: bool, budget: list[int]) -> bool | None:
        """True or False by the last rule that matches `below`, or None when none does."""
        best, answer = -1, None
        for n, rule in (*self.wild, *self.named.get(below[-1], ())):
            budget[0] -= 1
            if budget[0] < 0:
                raise _Exhausted
            if n > best and (is_dir or not rule.dir_only) and _match(rule, below, budget):
                best, answer = n, not rule.negate
        return answer


class Ignore:
    """The ignore files of one tree. `files` maps a `.gitignore`'s directory ("" is the root) to its text."""

    def __init__(self, files: Mapping[str, str]) -> None:
        self.notes: list[str] = []
        rules: dict[str, list[_Rule]] = {}
        for directory, text in files.items():
            directory = directory.strip("/")
            if parsed := _parse(directory, text, self.notes):
                rules.setdefault(directory, []).extend(parsed)
        self._index = {directory: _Index(found) for directory, found in rules.items()}
        self._dirs: dict[str, bool] = {}

    def _verdict(self, path: str, is_dir: bool, budget: list[int]) -> bool:
        """The last matching rule's answer for `path`, over the files above it, shallowest first."""
        parts = path.split("/")
        verdict = False
        for depth in range(len(parts)):
            index = self._index.get("/".join(parts[:depth]))
            if index is not None and (found := index.verdict(parts[depth:], is_dir, budget)) is not None:
                verdict = found
        return verdict

    def _dir_ignored(self, directory: str, budget: list[int]) -> bool:
        """Whether `directory` or one above it is ignored, remembered per directory."""
        parts = directory.split("/") if directory else []
        above = False
        for depth in range(1, len(parts) + 1):
            current = "/".join(parts[:depth])
            if current not in self._dirs:
                try:
                    self._dirs[current] = above or self._verdict(current, True, budget)
                except _Exhausted:
                    self._dirs[current] = True
                    self._note(f"a path that took over {MAX_STEPS} steps to judge is judged ignored")
            above = self._dirs[current]
        return above

    def _note(self, text: str) -> None:
        if text not in self.notes:
            self.notes.append(text)

    def ignored(self, path: str) -> bool:
        """Whether the file `path` is ignored, itself or through an ignored directory above it."""
        path = path.strip("/")
        if not path:
            return False
        parts = path.split("/")
        if len(parts) > MAX_DEPTH or any(len(part) > MAX_NAME for part in parts):
            self._note(f"a path over {MAX_DEPTH} segments or with a name over {MAX_NAME} characters is judged ignored")
            return True
        budget = [MAX_STEPS]
        parent, _, _ = path.rpartition("/")
        try:
            return self._dir_ignored(parent, budget) or self._verdict(path, False, budget)
        except _Exhausted:
            self._note(f"a path that took over {MAX_STEPS} steps to judge is judged ignored")
            return True
