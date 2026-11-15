"""The verify step's evidence, as the runner reads it: the `signals.verification` of its result, parsed into a
`Story` and rendered for the pull request body and for a prompt's placeholder.

The result is model text, so nothing here trusts it. A malformed signal or item is dropped, never repaired, every text
is one clean line (`clean_fact`) cut to `MAX_TEXT`, each list is cut to `MAX_ITEMS`, and a backtick that could close
the code span a text sits in is replaced. In the pull request every model text is a code span. Everything is a pure
function except `read_story`."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from agentflow.hitl import ResultError, clean_fact, read_result

if TYPE_CHECKING:
    from agentflow.state import Run

VERIFY_KEY = "verify/verify"
REVIEW_FIX_KEY = "review/fix_findings"
NO_EVIDENCE = "verify reported no evidence"
NOT_RUN = "verify did not run"
MAX_ITEMS = 40  # items kept of each list
MAX_TEXT = 300  # characters of one text


@dataclass(frozen=True)
class GateLine:
    gate: str
    cmd: str
    result: str
    passed: bool


@dataclass(frozen=True)
class Criterion:
    criterion: str
    evidence: str
    ran: bool
    passed: bool


@dataclass(frozen=True)
class Gap:
    what: str
    why: str


@dataclass(frozen=True)
class Story:
    gates: tuple[GateLine, ...]
    criteria: tuple[Criterion, ...]
    not_verified: tuple[Gap, ...]
    more: int  # items cut by MAX_ITEMS, over all three lists


def _text(raw: str, code: bool = False) -> str:
    """One model text as one clean line, cut to MAX_TEXT with a trailing `…`. `code` text sits in a code span, so
    its backticks become apostrophes."""
    text = clean_fact(raw)
    if code:
        text = text.replace("`", "'")
    return text[: MAX_TEXT - 1] + "…" if len(raw) > MAX_TEXT else text[:MAX_TEXT]


def _items(raw: object, build) -> tuple[list, int]:
    """The valid items of one list and how many valid ones the cap cut. `build` returns an item or None."""
    if raw is None:
        return [], 0
    if not isinstance(raw, list):
        raise ValueError("not a list")
    items = [item for item in (build(entry) for entry in raw if isinstance(entry, dict)) if item is not None]
    return items[:MAX_ITEMS], max(0, len(items) - MAX_ITEMS)


def _strings(entry: dict, *names: str) -> list[str] | None:
    values = [entry.get(name) for name in names]
    return values if all(isinstance(value, str) for value in values) else None


def _gate(entry: dict) -> GateLine | None:
    texts, passed = _strings(entry, "gate", "cmd", "result"), entry.get("passed")
    if texts is None or not isinstance(passed, bool):
        return None
    gate, cmd, result = _text(texts[0], True), _text(texts[1], True), _text(texts[2])
    return GateLine(gate, cmd, result, passed) if gate else None


def _criterion(entry: dict) -> Criterion | None:
    texts, ran, passed = _strings(entry, "criterion", "evidence"), entry.get("ran"), entry.get("passed")
    if texts is None or not isinstance(ran, bool) or not isinstance(passed, bool):
        return None
    criterion = _text(texts[0])
    return Criterion(criterion, _text(texts[1], True), ran, passed) if criterion else None


def _gap(entry: dict) -> Gap | None:
    texts = _strings(entry, "what", "why")
    if texts is None:
        return None
    what = _text(texts[0])
    return Gap(what, _text(texts[1])) if what else None


def parse(signals: object) -> Story | None:
    """The story in a result's `signals`, or None when it holds none: not a mapping, `verification` not a mapping,
    a list that is not a list, or nothing valid left in any of the three."""
    if not isinstance(signals, dict) or not isinstance(signals.get("verification"), dict):
        return None
    raw = signals["verification"]
    try:
        gates, cut_gates = _items(raw.get("gates"), _gate)
        criteria, cut_criteria = _items(raw.get("criteria"), _criterion)
        gaps, cut_gaps = _items(raw.get("not_verified"), _gap)
    except ValueError:
        return None
    if not (gates or criteria or gaps):
        return None
    return Story(tuple(gates), tuple(criteria), tuple(gaps), cut_gates + cut_criteria + cut_gaps)


def stale_sha(events: list[dict]) -> str | None:
    """The short sha of the last review fix commit made after `verify` last finished, or None: that verification
    was of the tree before the fix."""
    finished = [i for i, e in enumerate(events)
                if e.get("type") == "step_finished" and e.get("key") == VERIFY_KEY and e.get("status") == "done"]
    if not finished:
        return None
    fixes = [e for e in events[finished[-1] + 1:]
             if e.get("type") == "step_committed" and e.get("key") == REVIEW_FIX_KEY and e.get("sha")]
    return str(fixes[-1]["sha"])[:12] if fixes else None


def _mark(ran: bool, passed: bool) -> str:
    return ("✓" if passed else "✗") if ran else "– not run"


def _span(text: str) -> str:
    """One parsed text as a code span, so markdown, HTML, mentions and issue keywords in it are shown, never acted on.
    Its backticks become apostrophes; an empty text is `(none)`, never an empty span."""
    return f"`{text.replace('`', "'")}`" if text else "(none)"


def _lines(story: Story, with_gates: bool, suffix: str, show=str) -> list[str]:
    """One line per gate (when `with_gates`), criterion and gap. `show` renders each prose text; the gate name and the
    evidence are always a code span."""
    lines = []
    if with_gates:
        lines += [f"- gate {_span(g.gate)}: {show(g.result)} {_mark(True, g.passed)}" for g in story.gates]
    lines += [f"- {show(c.criterion)}: {_span(c.evidence)} {_mark(c.ran, c.passed)}{suffix if c.ran else ''}"
              for c in story.criteria]
    lines += [f"- not verified: {show(g.what)}: {show(g.why)}" for g in story.not_verified]
    if story.more:
        lines.append(f"- … and {story.more} more")
    return lines


def render_prompt(found: bool, story: Story | None) -> str:
    """The story for a prompt's placeholder: gates, criteria and gaps, one line each. Empty when verify did not run."""
    if not found:
        return ""
    return "\n".join(_lines(story, True, "")) if story else NO_EVIDENCE


def render_pr(found: bool, story: Story | None, stale: str | None) -> list[str]:
    """The story for the pull request body, without the gates (its own section has them). Every model text is a code
    span, so nothing in it renders or links. `stale` is the sha of a review fix made after verify, which every
    criterion that ran is marked with."""
    if not found:
        return [NOT_RUN]
    if story is None:
        return [NO_EVIDENCE]
    return _lines(story, False, f" (before review fix {stale})" if stale else "", _span)


def read_story(run: Run) -> tuple[bool, Story | None]:
    """Whether verify left a result and the story in it. `found` is False only when there is no result file."""
    path = run.result_path(VERIFY_KEY)
    if not path.exists():  # `read_result` reports a missing file as a `ResultError`, so it is told apart here
        return False, None
    try:
        result = read_result(path)
    except FileNotFoundError:
        return False, None
    except (ResultError, OSError, ValueError):
        return True, None
    return True, parse(result.signals)
