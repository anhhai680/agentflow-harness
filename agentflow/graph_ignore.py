"""A pure `.gitignore` matcher for the knowledge graph: which tracked paths a repository's own ignore files name.

`Ignore` is built from the text of each `.gitignore` (the caller reads them from git objects, so this module reads no
file) and answers `ignored(path)` the way `git check-ignore` does. The same class holds the pack's `graph.exclude`
patterns, passed as one root file.

The patterns are untrusted repository text, so nothing here uses a regular expression. A pattern is split on `/` into
segments and each segment is compiled to a short token list. A segment is matched by equality, by `str.find` over the
chunks between its stars, by comparing the tokens either side of its one star, or, when it has several stars beside a
`?` or a class, by the two-pointer wildcard walk, which is `O(len(text) * len(pattern))` however many `*` it holds (a
backtracking regular expression is not). `**` segments are matched by a set walk over the path's segments, so
repetition is never nested.

The work of one `ignored(path)` call is counted and bounded. A step is a rule looked at, a token compared or a
character walked, counted over every rule of every file the call consults and including the directories above the
path (a directory already judged by an earlier call is charged what it cost then, so the count is the same whatever
order the paths are asked in). A call that spends `MAX_STEPS` stops, judges the path ignored, which is the safe side
for the graph, adds one note and caches nothing. Honest files stay far below it (the tests assert a quarter at most:
1,000 `*.ext` rules at depth 20 take 21,000 steps, a 5,000-line mixed file at depth 20 takes 73,000, this
repository's own file about 300). An exhausted call measured about 15 to 100 ms on a 2026 laptop (hostile shapes,
fresh directories; the figure moves with the shape, the Python version and the load), so a hostile file can slow a
build by that much per path but cannot make it judge a path not ignored that git ignores. The exceptions are the
three caps below, which leave a file or a pattern out with a note: `MAX_FILE_BYTES`, `MAX_LINES` and `MAX_PATTERN`.
Parsing is linear in each pattern's length, so a file within the caps parses in well under a second however its
brackets are placed.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

MAX_FILE_BYTES, MAX_LINES, MAX_PATTERN = 256 * 1024, 5000, 512
MAX_STEPS = 400_000

_STAR = ("*",)
_ANY = ("?",)
_POSIX = {
    "alnum": str.isalnum, "alpha": str.isalpha, "digit": str.isdigit, "lower": str.islower, "upper": str.isupper,
    "space": str.isspace, "blank": lambda c: c in " \t", "xdigit": lambda c: c in "0123456789abcdefABCDEF",
    "punct": lambda c: c.isascii() and c.isprintable() and not c.isalnum() and c != " ",
}
_LONGEST = max(map(len, _POSIX))


class _Classes:
    """Reads the bracket classes of one segment so that parsing it is linear in its length, however many `[` it holds.

    Once past its first member, a class scan's path depends only on where it stands, so every position a scan that
    never closed walked through is remembered as dead and a later scan that reaches one stops there. A scan that closes
    is not walked again, because the tokens resume after it. The `:]` that ends a `[:name:]` is looked up once per
    stretch of the segment, not once per `[`.
    """

    __slots__ = ("text", "dead", "seek", "found")

    def __init__(self, text: str) -> None:
        self.text = text
        self.dead: set[int] = set()
        self.seek, self.found = len(text) + 1, -1  # the first `:]` at or after `seek` is at `found` (-1: none)

    def _colon(self, start: int) -> int:
        """Where the first `:]` at or after `start` begins, or -1."""
        if not (self.seek <= start and (self.found < 0 or self.found >= start)):
            self.seek, self.found = start, self.text.find(":]", start)
        return self.found

    def read(self, at: int):
        """The class opening at `text[at] == "["`: (token, index after it), or None when it never closes."""
        text, dead = self.text, self.dead
        i = at + 1
        negate = i < len(text) and text[i] in "!^"
        i += negate
        ranges: list[tuple[str, str]] = []
        named: list[str] = []
        first = True
        walked: list[int] = []
        while i < len(text):
            if not first:
                if i in dead:
                    break
                walked.append(i)
            c = text[i]
            if c == "]" and not first:
                return ("[", negate, tuple(ranges), tuple(named)), i + 1
            first = False
            if c == "[" and text.startswith(":", i + 1) and (end := self._colon(i + 2)) > 0:
                if end - i - 2 <= _LONGEST and (name := text[i + 2 : end]) in _POSIX:
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
        dead.update(walked)
        return None


def _tokens(segment: str) -> tuple:
    """One path segment's glob as tokens: ("c", char), `?`, `*` (runs of `*` collapse) and classes."""
    out: list[tuple] = []
    classes = _Classes(segment)
    i = 0
    while i < len(segment):
        c = segment[i]
        if c == "*":
            if not out or out[-1] != _STAR:
                out.append(_STAR)
        elif c == "?":
            out.append(_ANY)
        elif c == "[" and (made := classes.read(i)) is not None:
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


class _Meter:
    """The steps one call has left."""

    __slots__ = ("left",)

    def __init__(self, left: int) -> None:
        self.left = left

    def spend(self, steps: int = 1) -> None:
        self.left -= steps
        if self.left < 0:
            raise _Exhausted


def _weight(token: tuple) -> int:
    """What comparing one character against the token costs: a class costs by its members."""
    return 1 + len(token[2]) + len(token[3]) if token[0] == "[" else 1


def _walk(tokens: tuple, weights: tuple, text: str, meter: _Meter) -> bool:
    """Whether `text` (no `/`) matches the tokens: the wildcard walk that backtracks to the last `*` only."""
    ti = si = 0
    star = mark = -1
    left = meter.left
    try:
        while si < len(text):
            left -= weights[ti] if ti < len(tokens) else 1
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
        while ti < len(tokens) and tokens[ti] == _STAR:
            ti += 1
        return ti == len(tokens)
    finally:
        meter.left = left


class _Glob:
    """One path segment's glob. `match` charges the meter for the work beyond the one step its caller paid."""

    __slots__ = ("tokens", "weights", "literal", "head", "mid", "tail", "stars", "need", "first", "last", "before", "after", "span")

    def __init__(self, tokens: tuple) -> None:
        self.tokens = tokens
        self.weights = tuple(_weight(t) for t in tokens)
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
        self.span = sum(self.weights) - (1 if self.stars else 0)
        self.first = tokens[0][1] if tokens and tokens[0][0] == "c" else None
        self.last = tokens[-1][1] if tokens and tokens[-1][0] == "c" else None

    def match(self, text: str, meter: _Meter) -> bool:
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
            meter.spend(len(self.mid))
            for chunk in self.mid:
                at = text.find(chunk, pos, end)
                if at < 0:
                    return False
                pos = at + len(chunk)
            return True
        if (self.first is not None and text[0] != self.first) or (self.last is not None and text[-1] != self.last):
            return False
        if self.stars <= 1:
            meter.spend(self.span)
            tail = text[len(text) - len(self.after) :] if self.after else ""
            return all(map(_one, self.before, text)) and all(map(_one, self.after, tail))
        return _walk(self.tokens, self.weights, text, meter)


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


def _match(rule: _Rule, parts: list[str], meter: _Meter) -> bool:
    """Whether the rule matches the path made of `parts` (relative to the rule's directory)."""
    segments = rule.segments
    if not rule.anchored:
        return segments[0].match(parts[-1], meter)
    count = len(parts)
    if count < rule.need or (rule.fixed and count != len(segments)):
        return False
    first, last = segments[0], segments[-1]
    meter.spend(2)
    if (last != "**" and not last.match(parts[-1], meter)) or (first != "**" and not first.match(parts[0], meter)):
        return False
    here = {0}
    final = len(segments) - 1
    for n, segment in enumerate(segments):
        if segment == "**":
            lowest = min(here) + (1 if n == final else 0)
            meter.spend(count - lowest + 1)
            here = set(range(lowest, count + 1))
        else:
            meter.spend(len(here))
            here = {j + 1 for j in here if j < count and segment.match(parts[j], meter)}
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
    skipped = 0
    for line in lines:
        line = _clean(line.removesuffix("\r"))
        if len(line) > MAX_PATTERN:
            skipped += 1
        elif (rule := _rule(line)) is not None:
            rules.append(rule)
    if skipped:
        notes.append(f"{name}: {skipped} pattern(s) over {MAX_PATTERN} characters left out")
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

    def verdict(self, below: list[str], is_dir: bool, meter: _Meter) -> bool | None:
        """True or False by the last rule that matches `below`, or None when none does."""
        best, answer = -1, None
        for n, rule in (*self.wild, *self.named.get(below[-1], ())):
            meter.spend()
            if n > best and (is_dir or not rule.dir_only) and _match(rule, below, meter):
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
        self._dirs: dict[str, tuple[bool, int]] = {}  # directory -> (ignored, steps it cost with its ancestors)

    def _verdict(self, path: str, is_dir: bool, meter: _Meter) -> bool:
        """The last matching rule's answer for `path`, over the files above it, shallowest first."""
        parts = path.split("/")
        meter.spend(len(parts))
        verdict = False
        for depth in range(len(parts)):
            index = self._index.get("/".join(parts[:depth]))
            if index is not None and (found := index.verdict(parts[depth:], is_dir, meter)) is not None:
                verdict = found
        return verdict

    def _dir(self, directory: str) -> tuple[bool, int]:
        """Whether `directory` or one above it is ignored, and the steps that took, remembered per directory."""
        parts = directory.split("/") if directory else []
        above, spent = False, 0
        for depth in range(1, len(parts) + 1):
            current = "/".join(parts[:depth])
            known = self._dirs.get(current)
            if known is None:
                if above:
                    known = (True, spent)
                else:
                    meter = _Meter(MAX_STEPS - spent)
                    known = (self._verdict(current, True, meter), MAX_STEPS - meter.left)
                self._dirs[current] = known
            above, spent = known
        return above, spent

    def judge(self, path: str) -> tuple[bool, int]:
        """Whether the file `path` is ignored, itself or through an ignored directory above it, and the steps it took.

        The steps are the same whatever was asked before. A path that needs `MAX_STEPS` is judged ignored.
        """
        path = path.strip("/")
        if not path:
            return False, 0
        try:
            above, spent = self._dir(path.rpartition("/")[0])
            if above:
                return True, spent
            meter = _Meter(MAX_STEPS - spent)
            verdict = self._verdict(path, False, meter)
            return verdict, MAX_STEPS - meter.left
        except _Exhausted:
            note = f"a path that took {MAX_STEPS} steps to judge is judged ignored"
            if note not in self.notes:
                self.notes.append(note)
            return True, MAX_STEPS

    def ignored(self, path: str) -> bool:
        """Whether the file `path` is ignored."""
        return self.judge(path)[0]

    def folder_ignored(self, directory: str) -> bool:
        """Whether the folder `directory`, or one above it, is ignored: git loads no ignore file inside such a folder.
        A folder that needs `MAX_STEPS` is judged ignored, as `judge` does for a path."""
        directory = directory.strip("/")
        if not directory:
            return False
        try:
            return self._dir(directory)[0]
        except _Exhausted:
            note = f"a path that took {MAX_STEPS} steps to judge is judged ignored"
            if note not in self.notes:
                self.notes.append(note)
            return True
