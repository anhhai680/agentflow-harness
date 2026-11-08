"""Per-run failure and efficiency signals, read from a run's events and its metrics.

A signal is a number with a key: a family (`gate_first_pass`) and, for most families, one parameter (the gate).
The families are a fixed vocabulary (`FAMILIES`): a lesson names the signal it wants to move as a target in this
vocabulary (`parse_target`), and the same keys are averaged over many runs (`aggregate_signals`).

- `run_signals(events, metrics)`: the signals of one run. A key appears only when the run gives evidence for it
  (a run that never ran gate `lint` has no `gate_first_pass:lint`), so a mean over runs is a mean over the runs it
  applies to. The counts in `COUNT_FAMILIES` are the exception: `ci_rounds`, `rollovers`, `progress_not_updated`
  and `rework_pct` are always present (0 when nothing happened), `fix_attempts:<p>` and `human_rejections:<p>`
  are present for every phase of the run, and a key of the open-parameter count families that is absent is 0.
- `parse_target(text)`: `(family, parameter)` for a key in the vocabulary, else None.
- `aggregate_signals(per_run)`: `{key: {"runs": n, "mean": m}}` over the runs that have each key.

Pure functions over data: nothing here reads a file or the clock. Fields added to events later are read with
`.get`, so an older log still gives its signals.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

# family -> (the direction that is better, whether the key takes a parameter after `:`)
FAMILIES: dict[str, tuple[str, bool]] = {
    "gate_first_pass": ("higher", True),   # parameter: a gate
    "fix_attempts": ("lower", True),       # a phase
    "guard_trips": ("lower", True),        # a violation kind
    "permission_denied": ("lower", True),  # a tool
    "stop": ("lower", True),               # a stop kind
    "human_rejections": ("lower", True),   # a phase
    "review_blocking": ("lower", True),    # a phase
    "cost_per_task": ("lower", True),      # a phase
    "turns_per_step": ("lower", True),     # a phase
    "ci_rounds": ("lower", False),
    "rollovers": ("lower", False),
    "progress_not_updated": ("lower", False),
    "rework_pct": ("lower", False),
}
# Families that count events: a run with none has a true 0, so a key absent from a run's signals reads as 0 here.
# The plain ones (`ci_rounds`, `rollovers`, `progress_not_updated`) are always present, and `fix_attempts` and
# `human_rejections` are present for every phase of the run; the open-parameter ones (`guard_trips`,
# `permission_denied`, `stop`) are present only for a parameter that occurred.
COUNT_FAMILIES = frozenset({"fix_attempts", "guard_trips", "permission_denied", "stop", "human_rejections",
                            "ci_rounds", "rollovers", "progress_not_updated"})
RUNNER = "runner"  # `phase_rejected.by` for what the runner settles itself; a person's name is never this


def parse_target(text: str) -> tuple[str, str | None] | None:
    """`(family, parameter)` for `text` when it is in the vocabulary, else None. A family that takes a parameter
    needs a non-empty one, and one that takes none must have none."""
    if not isinstance(text, str):
        return None
    family, sep, param = text.partition(":")
    spec = FAMILIES.get(family)
    if spec is None:
        return None
    if spec[1]:
        return (family, param) if sep and param.strip() else None
    return (family, None) if not sep else None


def _phase_of(key: object) -> str | None:
    return str(key).split("/", 1)[0] if key else None


def _first_pass(events: list[dict]) -> dict[str, float]:
    first: dict[tuple[str, str], bool] = {}
    for ev in events:
        if ev.get("type") != "gate_result" or ev.get("skipped") or not ev.get("gate"):
            continue
        first.setdefault((str(ev.get("key")), str(ev["gate"])), bool(ev.get("passed")))
    totals: dict[str, list[int]] = {}
    for (_key, gate), passed in first.items():
        row = totals.setdefault(gate, [0, 0])
        row[0] += int(passed)
        row[1] += 1
    return {f"gate_first_pass:{gate}": passed / n for gate, (passed, n) in totals.items()}


def _turns(events: list[dict]) -> dict[str, float]:
    """Agent turns per step, by phase. A turn names its session, not its step, so the step comes from the session
    event that opened the session (or, before one, from the call that just started)."""
    step_of: dict[str, str] = {}
    current: str | None = None
    turns: Counter[str] = Counter()
    for ev in events:
        etype = ev.get("type")
        if etype == "agent_started":
            current = ev.get("step") or ev.get("key") or current
            if ev.get("session_id") and ev.get("step"):
                step_of[str(ev["session_id"])] = str(ev["step"])
        elif etype == "agent_session" and ev.get("session_id") and ev.get("key"):
            step_of[str(ev["session_id"])] = str(ev["key"])
            current = str(ev["key"])
        elif etype == "agent_turn":
            key = ev.get("key") or step_of.get(str(ev.get("session_id"))) or current
            if key:
                turns[str(key)] += 1
    per_phase: dict[str, list[int]] = {}
    for key, n in turns.items():
        phase = _phase_of(key)
        if phase:
            row = per_phase.setdefault(phase, [0, 0])
            row[0] += n
            row[1] += 1
    return {f"turns_per_step:{phase}": total / steps for phase, (total, steps) in per_phase.items()}


def run_signals(events: list[dict], metrics: dict) -> dict[str, float]:
    """The signals of one run, only for what the run did. `metrics` is `metrics.run_metrics` for the same run."""
    out: dict[str, float] = _first_pass(events)
    counts: dict[str, Counter[str]] = {name: Counter() for name in (
        "fix_attempts", "guard_trips", "permission_denied", "stop", "human_rejections", "review_blocking")}
    plain: Counter[str] = Counter()
    for ev in events:
        etype = ev.get("type")
        if etype == "fix_attempt":
            if phase := _phase_of(ev.get("key")):
                counts["fix_attempts"][phase] += 1
        elif etype == "guard_tripped":
            for violation in ev.get("violations") or []:
                kind = violation.get("kind") if isinstance(violation, dict) else None
                if kind:
                    counts["guard_trips"][str(kind)] += 1
        elif etype == "permission_denied":
            if ev.get("tool"):
                counts["permission_denied"][str(ev["tool"])] += 1
        elif etype == "hitl_raised":
            if ev.get("kind"):
                counts["stop"][str(ev["kind"])] += 1
        elif etype == "phase_rejected":
            if ev.get("phase") and ev.get("by") != RUNNER:
                counts["human_rejections"][str(ev["phase"])] += 1
        elif etype == "review_result":
            if phase := _phase_of(ev.get("key")):
                counts["review_blocking"][phase] += int(ev.get("blocking") or 0)
        elif etype in ("ci_round", "rolled_over", "progress_not_updated"):
            plain[{"ci_round": "ci_rounds", "rolled_over": "rollovers"}.get(etype, etype)] += 1
    for family, found in counts.items():
        out.update({f"{family}:{param}": n for param, n in found.items()})
    out.update({name: plain[name] for name in ("ci_rounds", "rollovers", "progress_not_updated")})
    for phase in metrics.get("phases") or []:
        for family in ("fix_attempts", "human_rejections"):
            out.setdefault(f"{family}:{phase['phase']}", 0)
    out.update(_turns(events))
    for phase in metrics.get("phases") or []:
        called = sum(1 for s in phase.get("steps") or [] if s.get("attempts"))
        if called:
            out[f"cost_per_task:{phase['phase']}"] = round(float(phase.get("cost_usd") or 0.0) / called, 6)
    cost = float(metrics.get("cost_usd") or 0.0)
    out["rework_pct"] = round(100 * float(metrics.get("rework_cost_usd") or 0.0) / cost, 2) if cost else 0
    return out


def aggregate_signals(per_run: list[dict[str, float]]) -> dict[str, dict[str, Any]]:
    """`{key: {"runs": n, "mean": m}}`: each key averaged over the runs that have it."""
    values: dict[str, list[float]] = {}
    for signals in per_run:
        for key, value in signals.items():
            values.setdefault(key, []).append(float(value))
    return {key: {"runs": len(v), "mean": sum(v) / len(v)} for key, v in sorted(values.items())}
