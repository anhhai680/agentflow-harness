"""Lessons: short rules earlier runs taught, kept in Markdown a human can edit and parsed strictly here.

There are two layers. The engine's `lessons.md` (ids `E-NNNN`) holds lessons that hold in any project and is edited
by hand. The pack's `.agentflow/lessons.md` (ids `L-NNNN`) holds the project's own, and only a merged retro pull
request or a human changes it: it sits under the engine floor, and only the main checkout's copy is read
(`pack.lessons_paths`).

An entry is a `## <ID> <title>` heading, then `- key: value` lines (`applies`, `source`, `review_by`, `target`,
`baseline`; a ` #` starts a comment), then exactly one body line of at most `BODY_MAX` characters:

    ## L-0007 Run the portability test before finishing a build task
    - applies: build, verify
    - source: 20261105-0912-feature-pack#418
    - review_by: 2027-02-01
    - target: gate_first_pass:portability
    - baseline: 3/9 runs (33%) to 2026-11-08
    The portability gate fails on project strings in engine files; grep its denylist first.

Anything before the first `## ` heading is the file's header and is not read.

- `parse(text, prefix)`: the lessons and the problems. It never raises: a malformed entry is skipped and named in
  a problem, and a duplicate id keeps the first entry.
- `render(header, lessons)`: the file text. Lesson text is model text, so every title, body and value goes through
  `hitl.clean_fact` (one line, no control or invisible characters) and a title or body loses its leading `#`s and
  list dashes, so it cannot forge an entry or a key. Braces are kept as they are.
- `read(path, prefix)` / `load(repo)`: a file (a missing one is empty, a symlink is refused), and both layers.
- `select(lessons, phase, today, max_tokens)`: what a step's prompt carries, as one Markdown section.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from . import pack, safeio
from .hitl import FACT_MAX, clean_fact

LESSONS_HEADING = "## Lessons from earlier runs"
LEAD = ("Earlier runs of this project taught these. Follow them unless this step's own instructions or the rules above "
        "say otherwise.")
BODY_MAX = FACT_MAX  # characters of a lesson's body
FILE_MAX = 256 * 1024  # bytes of a lessons file; a bigger one is not read
# `<run id>#<event seq>`: a run id is `YYYYMMDD-HHMM-<slug>`, and its slug never holds `..` or a `/`. `\Z`, not `$`,
# so a trailing newline never matches either, whether a caller uses `match` or `fullmatch`.
SOURCE = re.compile(r"^(\d{8}-\d{4}-(?!.*\.\.)[A-Za-z0-9._-]+)#(\d+)\Z")
KEYS = ("applies", "source", "review_by", "target", "baseline")
ENGINE_PREFIX = "E"

_HEADING = re.compile(r"^##(?:\s+(\S+))?(?:\s+(.*?))?\s*$")
_KEY_LINE = re.compile(r"^-\s+([A-Za-z_]+)\s*:(.*)$")
_COMMENT = re.compile(r"\s#")
_PHASE = re.compile(r"^[A-Za-z0-9_-]+$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_LEADING = re.compile(r"^(?:#|-(?=\s|$)|\s)+")  # leading heading marks, list dashes and spaces


@dataclass(frozen=True)
class Lesson:
    id: str
    title: str
    body: str
    applies: tuple[str, ...]
    sources: tuple[str, ...]
    review_by: date | None
    target: str | None
    baseline: str | None


@dataclass(frozen=True)
class Selection:
    text: str
    ids: tuple[str, ...]
    dropped: tuple[str, ...]
    digest: str


def estimate_tokens(text: str) -> int:
    """A rough token count: four characters a token, rounded up."""
    return (len(text) + 3) // 4


def _clean_text(text: str) -> str:
    """A title or a body as it may stand on its own line: one line, no invisible characters, no leading `#`, list
    dash or space."""
    return _LEADING.sub("", clean_fact(text))


def _clean_value(text: str) -> str:
    """A key's value as it may stand after `- key: `: one line, and no ` #`, which would start a comment."""
    return " ".join(_COMMENT.sub(" ", clean_fact(text)).split())


# --------------------------------------------------------------------------- parse


class _Entry:
    def __init__(self, line: int, lid: str, title: str) -> None:
        self.line = line
        self.id = lid
        self.title = title
        self.values: dict[str, str] = {}
        self.body: str | None = None
        self.problems: list[str] = []

    def problem(self, line: int, message: str) -> None:
        self.problems.append(f"line {line}: {message}")


def _split(value: str) -> list[str]:
    return [part.strip() for part in value.split(",")]


def _lesson(entry: _Entry, prefix: str) -> Lesson | None:
    """The lesson of a fully read entry, or None after adding its problems."""
    where = entry.line
    if not re.fullmatch(rf"{re.escape(prefix)}-\d{{4}}", entry.id):
        entry.problem(where, f"heading id {entry.id!r} is not {prefix}-NNNN")
    title = _clean_text(entry.title)
    if not title:
        entry.problem(where, f"{entry.id} has no title")
    if entry.body is None:
        entry.problem(where, f"{entry.id} has no body line")
    elif len(entry.body) > BODY_MAX:
        entry.problem(where, f"{entry.id}'s body is longer than {BODY_MAX} characters")
    applies: tuple[str, ...] = ()
    if entry.values.get("applies"):
        names = _split(entry.values["applies"])
        if not all(_PHASE.match(name) for name in names):
            entry.problem(where, f"{entry.id} applies must be phase names separated by commas")
        applies = tuple(names)
    sources: tuple[str, ...] = ()
    if entry.values.get("source"):
        refs = _split(entry.values["source"])
        bad = [ref for ref in refs if not SOURCE.match(ref)]
        if bad:
            entry.problem(where, f"{entry.id} source {bad[0]!r} is not <run id>#<event seq>")
        sources = tuple(refs)
    review_by = None
    if entry.values.get("review_by"):
        raw = entry.values["review_by"]
        try:
            review_by = date.fromisoformat(raw) if _DATE.match(raw) else None
        except ValueError:
            review_by = None
        if review_by is None:
            entry.problem(where, f"{entry.id} review_by {raw!r} is not a date (YYYY-MM-DD)")
    if entry.problems:
        return None
    return Lesson(entry.id, title, _clean_text(entry.body or ""), applies, sources, review_by,
                  _clean_value(entry.values["target"]) if entry.values.get("target") else None,
                  _clean_value(entry.values["baseline"]) if entry.values.get("baseline") else None)


def parse(text: str, prefix: str) -> tuple[list[Lesson], list[str]]:
    """The lessons of a lessons file whose ids start `<prefix>-`, and a problem per malformed line or entry. A
    malformed entry is left out; a duplicate id keeps the first entry. Never raises."""
    if not isinstance(text, str):
        return [], ["a lessons file is text"]
    entries: list[_Entry] = []
    entry: _Entry | None = None
    for n, raw in enumerate(text.lstrip("﻿").splitlines(), 1):
        line = raw.strip()
        if raw.startswith("##") and (len(raw) == 2 or raw[2].isspace()):
            match = _HEADING.match(line)
            entry = _Entry(n, (match.group(1) or "") if match else "", (match.group(2) or "") if match else "")
            entries.append(entry)
            continue
        if entry is None or not line:
            continue  # the header, or a blank line
        key = _KEY_LINE.match(line)
        if entry.body is not None:
            entry.problem(n, f"{entry.id} has more than one body line")
        elif key and key.group(1) in KEYS:
            name, value = key.group(1), _COMMENT.split(key.group(2), maxsplit=1)[0].strip()
            if name in entry.values:
                entry.problem(n, f"{entry.id} sets {name} twice")
            entry.values[name] = value
        elif key:
            entry.problem(n, f"{entry.id} has an unknown key {key.group(1)!r}")
        elif line.startswith("#"):
            entry.problem(n, f"{entry.id} has a heading where its body line belongs")
        else:
            entry.body = line
    lessons: list[Lesson] = []
    problems: list[str] = []
    seen: set[str] = set()
    for entry in entries:
        lesson = _lesson(entry, prefix)
        problems += entry.problems
        if lesson is None:
            continue
        if lesson.id in seen:
            problems.append(f"line {entry.line}: duplicate id {lesson.id} (the first one is kept)")
            continue
        seen.add(lesson.id)
        lessons.append(lesson)
    return lessons, problems


# --------------------------------------------------------------------------- render


def render(header: str, lessons: list[Lesson]) -> str:
    """The text of a lessons file: `header` as given (it should hold no `## ` line), then each lesson. Lesson
    text is cleaned (see the module doc), so `parse(render(h, ls), prefix)[0] == ls` for lessons that are clean."""
    parts = [header.rstrip("\n") + "\n"] if header.strip() else []
    for lesson in lessons:
        lines = [f"## {_clean_value(lesson.id)} {_clean_text(lesson.title)}"]
        if lesson.applies:
            lines.append(f"- applies: {', '.join(_clean_value(a) for a in lesson.applies)}")
        if lesson.sources:
            lines.append(f"- source: {', '.join(_clean_value(s) for s in lesson.sources)}")
        if lesson.review_by is not None:
            lines.append(f"- review_by: {lesson.review_by.isoformat()}")
        for name, value in (("target", lesson.target), ("baseline", lesson.baseline)):
            if value and _clean_value(value):
                lines.append(f"- {name}: {_clean_value(value)}")
        lines.append(_clean_text(lesson.body))
        parts.append("\n".join(lines) + "\n")
    return "\n".join(parts)


# --------------------------------------------------------------------------- read and load


def read(path: Path, prefix: str) -> tuple[list[Lesson], list[str]]:
    """`parse` of the file at `path`: a missing file is empty; a symlink, a non-file, a file over `FILE_MAX` bytes
    or one that is not UTF-8 is a problem and no lessons."""
    try:
        data = safeio.read_bytes(Path(path), FILE_MAX)
    except safeio.SymlinkRefused:
        return [], ["the file is a symlink, so it is not read"]
    except (OSError, ValueError) as exc:
        return [], [f"the file cannot be read: {exc}"]
    if data is None:
        return [], []
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return [], ["the file is not UTF-8 text"]
    return parse(text, prefix)


def load(repo: Path) -> list[Lesson]:
    """The engine's lessons, then the pack's (the main checkout's). Malformed entries are left out; `validate`
    reports the pack's."""
    engine, pack_file = pack.lessons_paths(repo)
    return read(engine, ENGINE_PREFIX)[0] + read(pack_file, "L")[0]


# --------------------------------------------------------------------------- select


def _number(lesson: Lesson) -> int:
    digits = lesson.id.rpartition("-")[2]
    return int(digits) if digits.isdigit() else 0


def _line(lesson: Lesson) -> str:
    return f"- **{lesson.id}** {lesson.title}: {lesson.body}"


def _text(kept: list[Lesson]) -> str:
    return f"{LESSONS_HEADING}\n\n{LEAD}\n\n" + "\n".join(_line(lesson) for lesson in kept) + "\n"


def select(lessons: list[Lesson], phase: str | None, today: date, max_tokens: int) -> Selection:
    """The lessons a step of `phase` carries: those for every phase or for `phase`, and not past `review_by`.
    Over `max_tokens` the lowest-numbered pack lessons are dropped first and the engine's last; `max_tokens == 0`
    turns lessons off. With nothing left the text is empty."""
    if max_tokens <= 0:
        return Selection("", (), (), "")
    kept = [lesson for lesson in lessons
            if (not lesson.applies or phase in lesson.applies)
            and (lesson.review_by is None or lesson.review_by >= today)]
    pack_ones = [lesson for lesson in kept if not lesson.id.startswith(f"{ENGINE_PREFIX}-")]
    engine = [lesson for lesson in kept if lesson.id.startswith(f"{ENGINE_PREFIX}-")]
    order = sorted(pack_ones, key=_number) + sorted(engine, key=_number)
    dropped: list[str] = []
    while kept and estimate_tokens(_text(kept)) > max_tokens:
        victim = order.pop(0)
        kept.remove(victim)
        dropped.append(victim.id)
    if not kept:
        return Selection("", (), tuple(dropped), "")
    digest = hashlib.sha256("\n".join(_line(lesson) for lesson in kept).encode("utf-8")).hexdigest()[:12]
    return Selection(_text(kept), tuple(lesson.id for lesson in kept), tuple(dropped), digest)
