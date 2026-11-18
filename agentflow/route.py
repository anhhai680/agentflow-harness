"""The rule that picks a route profile (`XS`, `S`, `M` or `L`) for a run, from the sizer's checklist.

Pure: reads no file and no clock. The sizer answers the six yes/no/unsure questions of `sizing` and
estimates the plan's tasks and the files it touches; this module turns those facts into a profile. The
rules are checked in order and the first match wins:

1. No usable answers: `L`, the full route (`FALLBACK`). Anything unknown falls back to the biggest route.
2. The security overlay applies, or a risk question is yes or unsure, or `L_MIN_TASKS` tasks or more: `L`.
3. `mechanical`, no risk question flagged, and at most `XS_MAX_FILES` files: `XS`.
4. At most `S_MAX_TASKS` tasks, and neither `interface` nor `risk` flagged: `S`.
5. Otherwise `M`.

An estimate that is missing, not an int or out of range is unknown, and unknown rules out `XS` and `S`.

The route only goes up. A sizer's proposal counts only when it names a higher profile than the rule's
pick (`decide`), and once the plan's real size is known `upgrade` can only raise the profile, never
lower it. A profile drops ceremony, never an obligation.
"""

from __future__ import annotations

from dataclasses import dataclass

from agentflow import sizing
from agentflow.config import ROUTE_PROFILES, Config, RouteProfile, Step

XS_MAX_FILES = 2
S_MAX_TASKS = 3
L_MIN_TASKS = 12
FALLBACK = "L"
ESTIMATE_MAX = 1000

RECORD_KIND = "route_profile"


@dataclass(frozen=True)
class Facts:
    """What the rule reads: the six answers (`None` unless all are valid), the estimates (`None` = unknown)
    and whether the security overlay applies."""
    answers: dict[str, str] | None
    tasks: int | None
    files: int | None
    security: bool

    def as_event(self) -> dict:
        return {"answers": dict(self.answers) if self.answers is not None else None,
                "tasks": self.tasks, "files": self.files, "security": self.security}


def _estimate(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= ESTIMATE_MAX:
        return None
    return value


def facts_from(raw: object, security: bool) -> Facts:
    """The facts of a sizer's result object (`answers`, `tasks_expected`, `files_expected`); any other `raw` is no facts."""
    if not isinstance(raw, dict):
        return Facts(answers=None, tasks=None, files=None, security=security)
    return Facts(answers=sizing.parse_answers(raw.get("answers")), tasks=_estimate(raw.get("tasks_expected")),
                 files=_estimate(raw.get("files_expected")), security=security)


def pick(facts: Facts) -> str:
    if facts.answers is None:
        return FALLBACK
    flags = sizing.flags(facts.answers)
    tasks, files = facts.tasks, facts.files
    if facts.security or flags["risk"] or (tasks is not None and tasks >= L_MIN_TASKS):
        return "L"
    if flags["mechanical"] and not any(flags[q] for q in sizing.RISK_QUESTIONS) \
            and files is not None and files <= XS_MAX_FILES:
        return "XS"
    if tasks is not None and tasks <= S_MAX_TASKS and not flags["interface"] and not flags["risk"]:
        return "S"
    return "M"


def higher(a: str, b: str) -> str:
    return max(a, b, key=ROUTE_PROFILES.index)


def decide(facts: Facts, proposed: object) -> tuple[str, str]:
    """`(profile, proposed_by)`: `fallback` with no answers, `sizer` when its proposal is higher than the rule's pick, else `rule`."""
    if facts.answers is None:
        return FALLBACK, "fallback"
    chosen = pick(facts)
    if isinstance(proposed, str) and proposed in ROUTE_PROFILES and higher(proposed, chosen) != chosen:
        return proposed, "sizer"
    return chosen, "rule"


def upgrade(current: str, *, tasks: int | None, risk: bool, security: bool) -> str | None:
    """The profile the plan's real size calls for, when it is higher than `current` (a name in ROUTE_PROFILES); never a lower one."""
    if security or risk or (tasks is not None and tasks >= L_MIN_TASKS):
        target = "L"
    elif tasks is not None and tasks > S_MAX_TASKS:
        target = "M"
    else:
        target = current
    return target if higher(target, current) != current else None


def last_record(events: list[dict]) -> dict | None:
    """The last `route_profile` event as stored. This module reads its `profile` (a profile name) and `applied` (a bool)."""
    for ev in reversed(events):
        if isinstance(ev, dict) and ev.get("type") == RECORD_KIND:
            return ev
    return None


def applied(cfg: Config, record: dict | None) -> RouteProfile | None:
    """The profile to apply. Only under `route.mode: on`, and only when the run was triaged as applied, so a run
    triaged under `observe` stays on the full route even if the pack switches to `on` mid-run."""
    if cfg.route.mode != "on" or not isinstance(record, dict) or record.get("applied") is not True:
        return None
    name = record.get("profile")
    return cfg.route.profiles.get(name) if isinstance(name, str) else None


def skips(cfg: Config, record: dict | None, key: str, step: Step | None = None) -> str | None:
    """The applied profile's name when its `skip` holds the step `key`. Never for a `step` that is, or holds, a
    `finish`, `ci` or `foreach` step: `validate` refuses such a profile, and a pack run without it still keeps them."""
    profile = applied(cfg, record)
    if profile is None or key not in profile.skip or not isinstance(record, dict):
        return None
    if step is not None and not skippable(step):
        return None
    return record["profile"]


def skippable(step: Step) -> bool:
    """Whether a profile may skip `step`: not when it, a step it holds or its `review` is a `finish`, `ci` or `foreach` step."""
    if step.type in ("finish", "ci") or step.foreach:
        return False
    return all(skippable(inner) for inner in ([step.review] if step.review is not None else []) + list(step.steps))


def round_cap(cfg: Config, record: dict | None, phase: str) -> int | None:
    profile = applied(cfg, record)
    return profile.doubt_rounds.get(phase) if profile is not None else None


def reviews_task(profile: RouteProfile | None, answers: dict[str, str] | None, security: bool) -> bool:
    """Whether a plan task still gets its own review: always when unsure, risky, under security, or the profile keeps it."""
    return (profile is None or profile.per_task_review or answers is None or security
            or sizing.flags(answers)["risk"])
