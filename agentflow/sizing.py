"""The rule that sizes a step's effort from a checklist the sizer answered.

Pure: reads no file and no clock. The sizer (a short agent session) answers six yes/no/unsure
questions; this module turns the answers into `low`, `medium` or `high`, keeps the level inside
`effort.allowed`, and raises judgement steps (define, plan, review, debug) to at least `medium`.
Only escalation, after a step has failed, reaches a level above `high`.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Sequence

from agentflow.config import KNOWN_EFFORT_LEVELS, SIZER_STEP_ID, EffortPolicy
from agentflow.sources import BOX

QUESTIONS = ("interface", "risk", "ambiguity", "breadth", "novelty", "mechanical")
RISK_QUESTIONS = ("interface", "risk", "ambiguity", "breadth", "novelty")
OTHER_RISKS = ("interface", "ambiguity", "breadth", "novelty")
ANSWERS = ("yes", "no", "unsure")

JUDGEMENT_PHASES = frozenset({"define", "plan", "review"})
JUDGEMENT_ROLES = frozenset({"plan", "review", "debug"})

LOW_MAX_FILES = 2
SIZE_MARK = "/size"
REASON_MAX = 300
ASK_MAX = 6000
CHANGED_SHOWN = 20

SIZE_MAX_TURNS = 8
SIZE_TIMEOUT_S = 180
PLAN_SIZE_MAX_TURNS = 16
PLAN_SIZE_TIMEOUT_S = 420
PLAN_SIZE_CHUNK = 6

_JSON_BLOCK = re.compile(r"```json\s*\n(.*?)\n\s*```", re.DOTALL | re.IGNORECASE)


def is_sizer_key(key: str) -> bool:
    """A sizer's own key: `<step key>/size` or `<plan prefix>/size-<n>`. `validate` refuses a step id of that shape,
    so a step whose key only contains `/size` (`build/sizes`) is still a step."""
    _, sep, last = key.rpartition("/")
    return bool(sep) and SIZER_STEP_ID.fullmatch(last) is not None


def is_judgement(phase: str, role: str) -> bool:
    return phase in JUDGEMENT_PHASES or role in JUDGEMENT_ROLES


def parse_answers(raw: object) -> dict[str, str] | None:
    """The six ids mapped to `yes`/`no`/`unsure`; extra keys are dropped; `None` unless all six are valid."""
    if not isinstance(raw, dict):
        return None
    out: dict[str, str] = {}
    for question in QUESTIONS:
        value = raw.get(question)
        if not isinstance(value, str) or value.strip().lower() not in ANSWERS:
            return None
        out[question] = value.strip().lower()
    return out


def flags(answers: dict[str, str]) -> dict[str, bool]:
    """`unsure` counts toward the cautious side: true for a risk question, false for `mechanical`."""
    out = {q: answers[q] in ("yes", "unsure") for q in RISK_QUESTIONS}
    out["mechanical"] = answers["mechanical"] == "yes"
    return out


def level_for(flags: dict[str, bool], files: int, security: bool, judgement: bool) -> str:
    risky = any(flags[q] for q in RISK_QUESTIONS)
    if judgement:
        return "high" if security or risky else "medium"
    if flags["risk"] or security or sum(flags[q] for q in OTHER_RISKS) >= 2:
        return "high"
    if flags["mechanical"] and not risky and files <= LOW_MAX_FILES:
        return "low"
    return "medium"


def _index(level: str) -> int:
    return KNOWN_EFFORT_LEVELS.index(level)


def clamp(level: str, allowed: Sequence[str]) -> str:
    """The nearest allowed level at or above `level`, else the highest allowed one."""
    ranked = sorted((a for a in allowed if a in KNOWN_EFFORT_LEVELS), key=_index)
    if not ranked:
        return level
    for candidate in ranked:
        if _index(candidate) >= _index(level):
            return candidate
    return ranked[-1]


def floor(level: str, judgement: bool, allowed: Sequence[str]) -> str:
    if not judgement:
        return level
    return highest([level, clamp("medium", allowed)])


def decide(answers: dict[str, str] | None, files: int, security: bool, judgement: bool,
           policy: EffortPolicy) -> tuple[str, bool]:
    """`(level, fell_back)`, always floored. No usable answers falls back to the default (`high` for judgement)."""
    if answers is None:
        base = "high" if judgement else policy.default
        return floor(clamp(base, policy.allowed), judgement, policy.allowed), True
    level = level_for(flags(answers), files, security, judgement)
    return floor(clamp(level, policy.allowed), judgement, policy.allowed), False


def sizer_effort(judgement: bool, allowed: Sequence[str]) -> str:
    return floor(clamp("low", allowed), judgement, allowed)


def escalate(level: str, allowed: Sequence[str]) -> str | None:
    """The first allowed level above `level`, in `KNOWN_EFFORT_LEVELS` order, or `None`."""
    for candidate in KNOWN_EFFORT_LEVELS:
        if candidate in allowed and _index(candidate) > _index(level):
            return candidate
    return None


def highest(levels: Iterable[str]) -> str:
    return max(levels, key=_index)


def parse_result(text: str) -> dict | None:
    """The whole message as a JSON object, else its last fenced json block; `None` when neither is an object."""
    candidates = [text]
    blocks = _JSON_BLOCK.findall(text)
    if blocks:
        candidates.append(blocks[-1])
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        if isinstance(value, dict):
            return value
    return None


def plan_digest(tasks: Iterable[tuple[int, str, str]]) -> str:
    """A hash of each task's number, title and text with every box read as unchecked, so ticking a box keeps it."""
    digest = hashlib.sha256()
    for n, title, text in tasks:
        lines = [(f"{m.group(1)}[ ]" + line[m.end():]) if (m := BOX.match(line)) else line
                 for line in text.splitlines()]
        digest.update(json.dumps([n, title, lines]).encode())
    return digest.hexdigest()


def task_card(*, key: str, phase: str, role: str, ask: str, files: list[tuple[str, int | None]],
              changed: list[str], security: bool, failing_gates: list[str]) -> str:
    named = [f"{path} ({lines} lines)" if lines is not None else f"{path} (new or unreadable)"
             for path, lines in files]
    shown = changed[:CHANGED_SHOWN]
    more = f" (and {len(changed) - len(shown)} more)" if len(changed) > len(shown) else ""
    return "\n".join([
        f"step: {key} (phase {phase}, role {role})",
        f"files the task names: {', '.join(named) if named else 'none'}",
        f"paths changed since the run started: {', '.join(shown) if shown else 'none'}{more}",
        f"security overlay: {'applies' if security else 'does not apply'}",
        f"failing gates: {', '.join(failing_gates) if failing_gates else 'none'}",
        "",
        "## What the step asks",
        ask[:ASK_MAX],
    ])


def reason_text(raw: object) -> str:
    return raw[:REASON_MAX] if isinstance(raw, str) else ""
