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
- `split_header(text)`: the file's header, the lines before its first entry heading, as `parse` splits them: what
  `render` may be given back, since it writes its header verbatim.
- `render(header, lessons)`: the file text. Lesson text is model text, so every title, body and value goes through
  `hitl.clean_fact` (one line, no control or invisible characters) and a title or body loses its leading `#`s and
  list dashes, so it cannot forge an entry or a key. Braces are kept as they are.
- `read(path, prefix)` / `load(repo)`: a file (a missing one is empty, a symlink is refused), and both layers.
- `select(lessons, phase, today, max_tokens)`: what a step's prompt carries, as one Markdown section.
- `effects(repo, lessons, now)`: each lesson's target signal before and after the runs that carried it, with a
  verdict. It compares runs, so it shows correlation, not cause (`CORRELATION_NOTE`).
- `run_logs(repo)`: every run's events, read without the lock or a write (the retro digest reads runs this way too).
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from . import pack, safeio
from .config import RETRO_PIPELINE
from .events import EventError, EventLog, parse_iso
from .hitl import FACT_MAX, clean_fact
from .metrics import run_metrics
from .signals import COUNT_FAMILIES, FAMILIES, parse_target, run_signals
from .state import TERMINAL, replay, runs_root

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

_KEY_LINE = re.compile(r"^-\s+([A-Za-z_]+)\s*:(.*)$")
_COMMENT = re.compile(r"\s#")  # where a comment starts on a key line
_HASHES = re.compile(r"\s+#+")  # a run of spaces then of `#`s: what render collapses so no comment can start
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
    """A rough token count: four characters a token, the estimate used everywhere in the engine."""
    return len(text) // 4


def _clean_text(text: str) -> str:
    """A title or a body as it may stand on its own line: one line, no invisible characters, no leading `#`, list
    dash or space."""
    return _LEADING.sub("", clean_fact(text))


def _clean_value(text: str) -> str:
    """A key's value as it may stand after `- key: `: one line, and no ` #`, which would start a comment."""
    text = clean_fact(text)
    while _COMMENT.search(text):  # one pass leaves none; the loop makes sure, whatever the input
        text = _HASHES.sub(" ", text)
    return " ".join(text.split())


# --------------------------------------------------------------------------- parse


def _heading(line: str) -> tuple[str, str]:
    """`(id, title)` of a stripped heading line `## <id> <title>`, either "" when absent. Split on whitespace, not a
    regex, so a long run of spaces costs linear time."""
    parts = line[2:].split(None, 1)
    return (parts[0] if parts else "", parts[1].strip() if len(parts) > 1 else "")


def _is_heading(raw: str) -> bool:
    """Whether a line starts an entry: `##` then a space, a tab or nothing (`### x` is header or body text)."""
    return raw.startswith("##") and (len(raw) == 2 or raw[2].isspace())


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
        if _is_heading(raw):
            entry = _Entry(n, *_heading(line))
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


def split_header(text: str) -> str:
    """The header of a lessons file: its lines before the first entry heading, split as `parse` splits them (so a
    form feed or a line separator ends a line here too), trailing blank lines dropped; "" when there is none. It
    holds no entry heading, so `render(split_header(text), ...)` writes no entry the lessons did not give it."""
    lines: list[str] = []
    for raw in text.lstrip("\ufeff").splitlines():
        if _is_heading(raw):
            break
        lines.append(raw)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines) + "\n" if lines else ""


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


# --------------------------------------------------------------------------- effects

MIN_AFTER_RUNS = 3  # runs that carried a lesson before its effect gets a verdict
NO_CHANGE_BAND = 0.10  # a relative change smaller than this either way is `no change`
VERDICTS = ("improved", "no change", "worse", "too few runs", "not measured")
CORRELATION_NOTE = "Before/after compares runs; it shows correlation, not cause."
# What reading one run's log may raise on a corrupt or hand-edited file: that run is left out, never fatal.
RUN_ERRORS = (OSError, ValueError, TypeError, KeyError, AttributeError, IndexError, EventError)


@dataclass(frozen=True)
class LessonEffect:
    id: str
    title: str
    target: str | None
    review_by: date | None
    baseline_runs: int
    baseline: float | None
    after_runs: int
    after: float | None
    verdict: str
    cited: int


@dataclass(frozen=True)
class _RunFacts:
    started: datetime | None
    counted: bool  # ended (done, failed or cancelled): only those are evidence
    injected: frozenset[str]
    cited: Counter
    signals: dict[str, float]


def run_logs(repo: Path) -> Iterator[tuple[str, list[dict]]]:
    """`(folder name, events)` for every run under the runs root, in name order (oldest first: ids start with the
    date). The logs are read without the lock and nothing is written, so a reader never disturbs a live run (a torn
    last line is skipped). A symlinked folder or log, a folder with no log, an empty log and a log that cannot be
    read are left out."""
    root = runs_root(repo)
    if not root.is_dir():
        return
    for folder in sorted(root.iterdir()):
        log = folder / "events.jsonl"
        if folder.is_symlink() or log.is_symlink() or not log.is_file():
            continue
        try:
            events = EventLog(log).read()
        except RUN_ERRORS:
            continue
        if events:
            yield folder.name, events


def _run_facts(events: list[dict], now: datetime) -> _RunFacts | None:
    """What `effects` needs from one run's events, or None for a retro run."""
    state = replay(events)
    if state.get("pipeline") == RETRO_PIPELINE:
        return None
    started = next((parse_iso(ev.get("ts")) for ev in events if ev.get("type") == "run_started"), None)
    injected: set[str] = set()
    cited: Counter = Counter()
    for ev in events:
        if ev.get("type") == "lessons_injected" and isinstance(ev.get("ids"), list):
            injected.update(i for i in ev["ids"] if isinstance(i, str))
        elif ev.get("type") == "lesson_cited" and isinstance(ev.get("id"), str):
            cited[ev["id"]] += 1
    counted = state.get("status") in TERMINAL
    signals = run_signals(events, run_metrics(events, now)) if counted else {}
    return _RunFacts(started, counted, frozenset(injected), cited, signals)


def _all_runs(repo: Path, now: datetime) -> list[_RunFacts]:
    """Every non-retro run's facts; a run whose log cannot be read is left out."""
    runs = []
    for _name, events in run_logs(repo):
        try:
            facts = _run_facts(events, now)
        except RUN_ERRORS:
            continue
        if facts is not None:
            runs.append(facts)
    return runs


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 6) if values else None


def _verdict(direction: str, baseline: float, after: float) -> str:
    # Rounded before the band test, so a change of exactly NO_CHANGE_BAND lands on the band's edge whatever the float
    # error (1.0 -> 0.9 is -0.09999999999999998 unrounded).
    rel = round((after - baseline) / max(abs(baseline), 1.0), 9)
    if direction == "higher":
        rel = -rel
    if rel <= -NO_CHANGE_BAND:
        return "improved"
    return "worse" if rel >= NO_CHANGE_BAND else "no change"


def _effect(lesson: Lesson, runs: list[_RunFacts]) -> LessonEffect:
    counted = [r for r in runs if r.counted]
    cited = sum(r.cited[lesson.id] for r in counted)
    parsed = parse_target(lesson.target) if lesson.target else None
    if parsed is None:
        return LessonEffect(lesson.id, lesson.title, lesson.target, lesson.review_by, 0, None, 0, None,
                            "not measured", cited)
    family, param = parsed
    key = family if param is None else f"{family}:{param.strip()}"
    absent = 0.0 if family in COUNT_FAMILIES else None  # a count family's absent key is a true 0

    def value(run: _RunFacts) -> float | None:
        found = run.signals.get(key, absent)
        return None if found is None else float(found)

    # The lesson came in with the first run that carried it, live ones included: a run started later without it
    # ran beside the lesson, not before it, so it is no baseline.
    starts = [r.started for r in runs if lesson.id in r.injected and r.started is not None]
    first = min(starts) if starts else None
    after = [v for r in counted if lesson.id in r.injected and (v := value(r)) is not None]
    before = [v for r in counted
              if lesson.id not in r.injected and r.started is not None and (first is None or r.started < first)
              and (v := value(r)) is not None]
    baseline, after_mean = _mean(before), _mean(after)
    if len(after) < MIN_AFTER_RUNS or baseline is None or after_mean is None:
        verdict = "too few runs"
    else:
        verdict = _verdict(FAMILIES[family][0], baseline, after_mean)
    return LessonEffect(lesson.id, lesson.title, lesson.target, lesson.review_by, len(before), baseline, len(after),
                        after_mean, verdict, cited)


def effects(repo: Path, lessons: list[Lesson], now: datetime) -> list[LessonEffect]:
    """Each lesson's target signal over the ended, non-retro runs: the mean over the runs that carried it (after)
    against the mean over the runs without it that started before the first run that carried it (baseline). That
    first run is the earliest start among every non-retro run that carried the lesson, live ones included: a live
    run moves the cutoff (a run started beside it ran with the lesson in play, so it is no baseline) but is never an
    after-run itself. Only runs that have the target's key count, except a count family's, where a missing key is 0.
    Fewer than `MIN_AFTER_RUNS` after-runs or no baseline is `too few runs`; a missing or unknown target is
    `not measured`."""
    runs = _all_runs(repo, now)
    return [_effect(lesson, runs) for lesson in lessons]
