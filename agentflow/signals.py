"""Per-run failure and efficiency signals, read from a run's events and its metrics.

A signal is a number with a key: a family (`gate_first_pass`) and, for most families, one parameter (the gate).
The families are a fixed vocabulary (`FAMILIES`): a lesson names the signal it wants to move as a target in this
vocabulary (`parse_target`), and the same keys are averaged over many runs (`aggregate_signals`).

- `run_signals(events, metrics, *, full_suite=())`: the signals of one run (`full_suite` is the pack's verify gate
  names, which `full_suite_runs` counts). A key appears only when the run gives evidence for it
  (a run that never ran gate `lint` has no `gate_first_pass:lint`), so a mean over runs is a mean over the runs it
  applies to. The counts in `COUNT_FAMILIES` are the exception: `ci_rounds`, `rollovers`, `progress_not_updated`
  and `rework_pct` are always present (0 when nothing happened), `fix_attempts:<p>` and `human_rejections:<p>`
  are present for every phase of the run, and a key of the open-parameter count families that is absent is 0.
  `conformance_missing` and `conformance_waived` count the statuses of the run's last `conformance_checked` and
  are present only for a run that has one. `gate_minutes:<p>` is the time a phase spent in gates (present for a
  phase that ran one), `gate_reruns` counts gates run again on a tree they had already passed on with the same
  command (always present: reuse should keep it at 0), and `refine_rounds:<p>` counts the refine rounds the runner
  opened itself (present for every phase of the run). `effort_escalations:<p>` counts `effort_escalated` events,
  `undersized:<p>` those whose step was decided `low` or `medium` by a source other than `fallback`, and
  `sizer_fallbacks:<p>` the steps whose last decision is a `fallback`; all three are present only for a parameter
  that occurred. `doubt_blocking:<p>`, `doubt_rounds:<p>`, `doubt_open:<p>`, `doubt_actionable:<p>`, `doubt_noise:<p>`
  and `doubt_misread:<p>` read the last `doubt_settled` of a phase (the findings its doubt review first raised, the
  reconcile rounds, the findings still open, and the dispositions given since the last human round). They are
  present only for a phase that has one, and reconcile rounds are not counted in `refine_rounds:<p>`.
  `informed_approval:<p>` is the share of a person's approvals of the phase whose `viewed` (the revisions the
  dashboard showed) covers every newest revision the phase produced. A CLI approval counts as uninformed, and an
  approval of a phase that produced nothing isn't counted. Present only for a phase with a counted approval.
  Sizer calls (events with `sizer`) are no step's turns, and their cost is not a step's cost.
  `lead_time_h` is the hours from `run_started` to `pr_marked_ready` (present only for a run that has both).
  `human_blocked_share` is the metrics `human` bucket over the run's wall time, 0..1 (present only when the run has
  wall time): the time a stop, a phase approval or a ready phase waited with nothing else started. Today that is
  "no running agent, no gate and an open item"; when parking lets agents run while an item is open, the signal must
  stay true to it, so it keeps meaning time spent waiting on a person with no work going. `full_suite_runs` counts
  the batches of gate results (consecutive `gate_result` events of one step) that ran every gate of the
  `full_suite` passed in, skipped and failed results included; it is present only when `full_suite` is non-empty.
  `route_upgrades` counts the `route_profile` events the runner recorded as an upgrade (`upgraded_from` set); it is
  present only for a run with a `route_profile`.
  `gate_cache_hit_rate` is the gates named by `gates_reused` events over those plus the `gate_result`s that were not
  skipped (0..1); it is present only when that total is above 0, and a rate has no true 0 to read as when absent.
  `cache_write_share` is the cache-write USD over all the USD of the run (0..1, 4 places; present only when the run
  has USD split by class), `prefix_write_tokens` the median tokens a fresh session wrote for its prefix (present when
  one did), `session_turns_p90:<kind>` the p90 of the turns of a step kind's sessions (present for a kind with turns)
  and `tool_output_kchars:<kind>` its tool output per session that reached the model (one with turns) in thousands of
  characters (present for the same kinds: those with turns). The kind is `metrics.step_kind`, such as
  `build.implement`. `compactions` counts `compacted` events and `usage_holds` the `run_held` events whose reason is
  `usage limit` (a CI or outage hold is not one); both are always present. A run from before usage was recorded has
  none of the other four.
  `parked_units` counts the `step_parked` events of the run that are not a re-emitted park (`demoted`), and
  `park_resume_rework` the tasks that were parked and afterwards had a `unit_unparked` with `conflict` or a
  `step_retry` a person or a command asked for (its `by` is not the runner's) on one of their keys, once per task
  (a retry before the task's first park is no rework, and neither is the runner's own `re-review`). Both are
  always present (0 when nothing parked), and both are lower-is-better.
  `waves` counts the `wave_started` events (higher is better), `parallel_demoted` the tasks a wave turned away or set
  aside, once per task and reason and leaving out `dissolved` (a dissolve is counted by `waves_dissolved`),
  `scope_violations` the `scope_violation` events, `waves_dissolved` the `wave_dissolved` events, and
  `parallel_candidates` the pairs in the run's last `parallel_candidates` event (the independent tasks the plan left
  ungrouped). All five are counts, always present (0 for a run with no wave), and lower is better but for `waves`.
  `agent_parallelism` (higher is better) is (main-session seconds + sub-agent seconds) over wall seconds of the build
  window, which runs from each `phase_started` of `build` to the next `phase_started` (the log's last event ends one
  still open). Main sessions are the ones `metrics` pairs for agent time (its steps' `sessions`), sub-agents are
  paired from `subagent_started` to `subagent_finished` by `tool_use_id` (one never finished is left out), and both
  are clipped to the window. 1.0 is one agent at a time, above it sub-agents ran together. It is present only when
  build had agent time (wall time and a main session in the window), and it counts seconds, never cost: a session's
  cost already holds its sub-agents'.
- `parse_target(text)`: `(family, parameter)` for a key in the vocabulary, else None.
- `aggregate_signals(per_run)`: `{key: {"runs": n, "mean": m}}` over the runs that have each key.

Pure functions over data: nothing here reads a file or the clock. Fields added to events later are read with
`.get`, so an older log still gives its signals.
"""

from __future__ import annotations

import re
import statistics
from collections import Counter
from collections.abc import Collection
from datetime import datetime
from typing import Any

from agentflow.events import parse_iso
from agentflow.metrics import _p90

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
    "conformance_missing": ("lower", False),  # obligations with no evidence at the last audit
    "conformance_waived": ("lower", False),   # obligations a person waived at the last audit
    "gate_minutes": ("lower", True),          # a phase: minutes its gates ran
    "gate_reruns": ("lower", False),          # gates run again on a tree they had passed on, same command
    "refine_rounds": ("lower", True),         # a phase: refine rounds the runner opened on its own findings
    "effort_escalations": ("lower", True),    # a phase: steps whose effort was raised after a gate or review failed
    "undersized": ("lower", True),            # a phase: escalations of a step sized low or medium by a real sizer
    "sizer_fallbacks": ("lower", True),       # a phase: steps sized by the fallback because the sizer failed
    "doubt_blocking": ("lower", True),        # a phase: findings its first doubt review raised that block
    "doubt_rounds": ("lower", True),          # a phase: reconcile rounds before the approval
    "doubt_noise": ("lower", True),           # a phase: findings its reconcile step dispositioned as noise
    "doubt_misread": ("lower", True),         # a phase: findings dispositioned as a misread contract
    "doubt_open": ("lower", True),            # a phase: findings still open when it raised its approval
    "doubt_actionable": ("higher", True),     # a phase: findings dispositioned as actionable
    "lead_time_h": ("lower", False),          # hours from the run's start to its pull request marked ready
    "human_blocked_share": ("lower", False),  # share of wall time spent waiting on a person with no work started
    "full_suite_runs": ("lower", False),      # times the whole verify gate set ran, in one batch of gate results
    "informed_approval": ("higher", True),    # a phase: share of a person's approvals that viewed what it produced
    "gate_cache_hit_rate": ("higher", False),  # gate results reused, over those reused plus those that ran
    "route_upgrades": ("lower", False),       # times the runner raised the run's route profile mid-flight
    "cache_write_share": ("lower", False),    # share of the run's cost that was cache writes, 0..1
    "prefix_write_tokens": ("lower", False),  # median tokens a fresh session wrote to the cache for its prefix
    "session_turns_p90": ("lower", True),     # a step kind: the 90th percentile of its sessions' turns
    "tool_output_kchars": ("lower", True),    # a step kind: thousands of characters of tool output per session
    "compactions": ("lower", False),          # times a session's context was compacted
    "usage_holds": ("lower", False),          # times the run waited out a usage limit
    "parked_units": ("lower", False),         # stops parked so the run could go on with other tasks
    "park_resume_rework": ("lower", False),   # parked tasks that then needed a conflict settled or a step retried
    "waves": ("higher", False),               # waves of plan tasks built together by one session's sub-agents
    "parallel_demoted": ("lower", False),     # tasks a wave turned away or set aside (a dissolve counts apart)
    "scope_violations": ("lower", False),     # paths a wave member changed that were not its own
    "waves_dissolved": ("lower", False),      # waves undone because they stopped, dispatched wrongly or were cut off
    "parallel_candidates": ("lower", False),  # independent ungrouped task pairs the plan left on the table
    "agent_parallelism": ("higher", False),   # agent seconds of the build window over its wall seconds
}
# Families that count events: a run with none has a true 0, so a key absent from a run's signals reads as 0 here.
# The plain ones (`ci_rounds`, `rollovers`, `progress_not_updated`) are always present, and `fix_attempts` and
# `human_rejections` are present for every phase of the run; the open-parameter ones (`guard_trips`,
# `permission_denied`, `stop`) are present only for a parameter that occurred.
COUNT_FAMILIES = frozenset({"fix_attempts", "guard_trips", "permission_denied", "stop", "human_rejections",
                            "ci_rounds", "rollovers", "progress_not_updated", "conformance_missing", "conformance_waived",
                            "gate_reruns", "refine_rounds", "effort_escalations", "undersized",
                            "sizer_fallbacks", "full_suite_runs", "route_upgrades", "compactions", "usage_holds",
                            "parked_units", "park_resume_rework", "waves", "parallel_demoted", "scope_violations",
                            "waves_dissolved", "parallel_candidates"})
DOUBT_FIELDS = (("doubt_blocking", "first_blocking"), ("doubt_rounds", "rounds"), ("doubt_noise", "noise"),
                ("doubt_misread", "misread"), ("doubt_open", "open"), ("doubt_actionable", "actionable"))
RECONCILE = "reconcile"  # `phase_rejected.kind` of a round the runner opens on a doubt review's findings
USAGE_HOLD = "usage limit"  # `run_held.reason` of a run waiting for its plan's usage limit to reset
TASK_PREFIX = re.compile(r"^(.*?/task-\d+)(?:/|$)")  # a plan task's key: `<phase>/<step>/task-<n>`
DISSOLVED = "dissolved"  # `parallel_demoted.reason` of a task a dissolved wave handed back: counted by the dissolve
BUILD = "build"  # the phase whose window `agent_parallelism` measures
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


def _park_signals(events: list[dict]) -> dict[str, int]:
    """`parked_units`: the `step_parked` events that are a new park (a re-emitted one carries `demoted`).
    `park_resume_rework`: the parked units (a plan task, else the parked key itself) that, after their first park, had
    a `unit_unparked` that was a conflict or a `step_retry` on a key at or under theirs, once each. Only a retry
    someone asked for counts: the runner emits `step_retry` itself for a re-review round, with `by` the runner's."""
    parked = 0
    units: set[str] = set()
    redone: set[str] = set()

    def unit_of(key: str) -> str:
        match = TASK_PREFIX.match(key)
        return match.group(1) if match else key

    for ev in events:
        etype, key = ev.get("type"), str(ev.get("key") or "")
        if etype == "step_parked" and key:
            if not ev.get("demoted"):
                parked += 1
            units.add(unit_of(key))
        elif etype == "unit_unparked" and ev.get("conflict") is True:
            if (unit := unit_of(key)) in units:
                redone.add(unit)
        elif etype == "step_retry" and ev.get("by") and ev["by"] != RUNNER:
            redone.update(unit for unit in units if key == unit or key.startswith(f"{unit}/"))
    return {"parked_units": parked, "park_resume_rework": len(redone)}


def _wave_signals(events: list[dict]) -> dict[str, int]:
    """The wave counts. `parallel_demoted` is one per `(task, reason)` (a resumed runner may say it again) and leaves
    out the `dissolved` reason; `parallel_candidates` is the pair count of the run's last such event."""
    waves = violations = dissolved = candidates = 0
    demoted: set[tuple[str, str]] = set()
    for ev in events:
        etype = ev.get("type")
        if etype == "wave_started":
            waves += 1
        elif etype == "parallel_demoted":
            if ev.get("reason") != DISSOLVED:
                demoted.add((str(ev.get("task") or ev.get("key")), str(ev.get("reason"))))
        elif etype == "scope_violation":
            violations += 1
        elif etype == "wave_dissolved":
            dissolved += 1
        elif etype == "parallel_candidates":
            pairs = ev.get("pairs")
            candidates = len(pairs) if isinstance(pairs, list) else 0
    return {"waves": waves, "parallel_demoted": len(demoted), "scope_violations": violations,
            "waves_dissolved": dissolved, "parallel_candidates": candidates}


def _build_windows(events: list[dict]) -> list[tuple[datetime, datetime]]:
    """Each `phase_started` of `build` to the next `phase_started`, or to the log's last timestamp."""
    starts = [(parse_iso(ev.get("ts")), ev.get("phase")) for ev in events if ev.get("type") == "phase_started"]
    last = max((t for ev in events if (t := parse_iso(ev.get("ts"))) is not None), default=None)
    windows = []
    for i, (start, phase) in enumerate(starts):
        end = starts[i + 1][0] if i + 1 < len(starts) else last
        if phase == BUILD and start is not None and end is not None and end > start:
            windows.append((start, end))
    return windows


def _overlap(start: datetime | None, end: datetime | None, window: tuple[datetime, datetime]) -> float:
    if start is None or end is None:
        return 0.0
    return max(0.0, (min(end, window[1]) - max(start, window[0])).total_seconds())


def _agent_parallelism(events: list[dict], metrics: dict) -> dict[str, float]:
    """`agent_parallelism`: see the module docstring. The main sessions are the ones `metrics` pairs; a session with
    no end yet runs to the end of its window."""
    windows = _build_windows(events)
    wall = sum((end - start).total_seconds() for start, end in windows)
    if wall <= 0:
        return {}
    main = [(parse_iso(s.get("started_at")), parse_iso(s.get("ended_at")), s.get("ended_at") is None)
            for phase in metrics.get("phases") or [] for step in phase.get("steps") or []
            for s in step.get("sessions") or []]
    opened: dict[str, datetime] = {}
    subs: list[tuple[datetime, datetime]] = []
    for ev in events:
        etype, ident = ev.get("type"), str(ev.get("tool_use_id") or "")
        if not ident or (at := parse_iso(ev.get("ts"))) is None:
            continue
        if etype == "subagent_started":
            opened.setdefault(ident, at)
        elif etype == "subagent_finished" and ident in opened:
            subs.append((opened.pop(ident), at))
    main_s = sub_s = 0.0
    for window in windows:
        main_s += sum(_overlap(start, window[1] if live else end, window) for start, end, live in main)
        sub_s += sum(_overlap(start, end, window) for start, end in subs)
    return {"agent_parallelism": round((main_s + sub_s) / wall, 4)} if main_s > 0 else {}


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


def _gate_time(events: list[dict]) -> dict[str, float]:
    """`gate_minutes:<phase>` for each phase that ran a gate, and `gate_reruns`: a gate that ran again on a tree it
    had already passed on (same command, when both events carry one). Events with no `tree` never count as reruns."""
    minutes: dict[str, float] = {}
    passed: dict[tuple[str, str], set[str]] = {}
    reruns = 0
    for ev in events:
        if ev.get("type") != "gate_result" or not ev.get("gate"):
            continue
        if phase := _phase_of(ev.get("key")):
            minutes[phase] = minutes.get(phase, 0.0) + float(ev.get("duration_s") or 0.0) / 60
        tree = ev.get("tree")
        if not tree:
            continue
        slot = (str(ev["gate"]), str(tree))
        cmd = str(ev.get("cmd") or "")
        seen = passed.get(slot)
        if seen is not None and (not cmd or "" in seen or cmd in seen):
            reruns += 1
        if ev.get("passed") and not ev.get("skipped"):
            passed.setdefault(slot, set()).add(cmd)
    out = {f"gate_minutes:{phase}": round(m, 3) for phase, m in minutes.items()}
    out["gate_reruns"] = reruns
    return out


def _turns(events: list[dict]) -> dict[str, float]:
    """Agent turns per step, by phase. A turn names its session, not its step, so the step comes from the session
    event that opened the session (or, before one, from the call that just started)."""
    step_of: dict[str, str] = {}
    current: str | None = None
    turns: Counter[str] = Counter()
    for ev in events:
        etype = ev.get("type")
        if ev.get("sizer"):
            continue
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


def _lead_time(events: list[dict]) -> dict[str, float]:
    """`lead_time_h`: hours from the first `run_started` to the first `pr_marked_ready`, when the run has both."""
    started = ready = None
    for ev in events:
        etype = ev.get("type")
        if etype == "run_started" and started is None:
            started = parse_iso(ev.get("ts"))
        elif etype == "pr_marked_ready" and ready is None:
            ready = parse_iso(ev.get("ts"))
    if started is None or ready is None:
        return {}
    return {"lead_time_h": round((ready - started).total_seconds() / 3600, 3)}


def _full_suite_runs(events: list[dict], full_suite: Collection[str]) -> int:
    """How many batches ran every gate of `full_suite`. A batch is a maximal run of consecutive `gate_result` events
    with the same `key`; passed, failed and skipped results all count, since every gate runs even after a failure.
    An affected-tier result (`tier: "affected"`) is no part of a batch."""
    need = set(full_suite)
    runs = 0
    batch: set[str] = set()
    batch_key: object = None
    in_batch = False

    def close() -> int:
        return int(in_batch and need <= batch)

    for ev in events:
        if ev.get("type") == "gate_result" and ev.get("tier") == "affected":
            continue  # it ran only the files a fix changed: never part of a full run
        if ev.get("type") == "gate_result" and ev.get("gate"):
            if in_batch and ev.get("key") != batch_key:
                runs += close()
                batch = set()
            in_batch, batch_key = True, ev.get("key")
            batch.add(str(ev["gate"]))
        else:
            runs += close()
            in_batch, batch = False, set()
    return runs + close()


def _gate_cache_hit_rate(events: list[dict]) -> dict[str, float]:
    reused = sum(len(ev.get("gates") or []) for ev in events if ev.get("type") == "gates_reused")
    ran = sum(1 for ev in events if ev.get("type") == "gate_result" and ev.get("gate") and not ev.get("skipped"))
    return {"gate_cache_hit_rate": reused / (reused + ran)} if reused + ran else {}


def _informed(ev: dict) -> bool:
    """Whether a `phase_approved` viewed every newest revision its phase produced: a non-empty `produced` that is a
    subset of `viewed`, by `(path, sha)`. An approval that recorded neither (the CLI's) is not informed."""
    def pairs(value: object) -> set[tuple[Any, Any]]:
        return {(i.get("path"), i.get("sha")) for i in value if isinstance(i, dict)} if isinstance(value, list) else set()
    produced = pairs(ev.get("produced"))
    return bool(produced) and produced <= pairs(ev.get("viewed"))


def _cost_signals(metrics: dict) -> dict[str, float]:
    """The cache write share of the run's USD, the median prefix write, and per step kind the p90 of its sessions'
    turns and its tool output per session that reached the model, all read from the metrics' splits. A key is absent
    for a run (or a kind) with nothing to measure: a log from before usage was recorded, or a session that never reached a model."""
    out: dict[str, float] = {}
    split = metrics.get("usd_split") or {}
    total = sum(float(v or 0.0) for v in split.values())
    if total > 0:
        out["cache_write_share"] = round(float(split.get("cache_write") or 0.0) / total, 4)
    kinds = metrics.get("by_step_kind") or {}
    prefixes = [p for row in kinds.values() for p in row.get("prefix_writes") or []]
    if prefixes:
        out["prefix_write_tokens"] = statistics.median(prefixes)
    for kind, row in kinds.items():
        if row.get("turns"):  # a kind with usage: a pre-usage row has sessions and no turns
            out[f"session_turns_p90:{kind}"] = _p90(row["turns"])
            # per session that reached the model (one `turns` entry each): a zero-turn session doesn't dilute it
            out[f"tool_output_kchars:{kind}"] = round(float(row.get("tool_output_chars") or 0) / len(row["turns"]) / 1000, 1)
    return out


def run_signals(events: list[dict], metrics: dict, *, full_suite: Collection[str] = ()) -> dict[str, float]:
    """The signals of one run, only for what the run did. `metrics` is `metrics.run_metrics` for the same run.
    `full_suite` names the gates of the verify set; with it, `full_suite_runs` is present."""
    out: dict[str, float] = _first_pass(events)
    counts: dict[str, Counter[str]] = {name: Counter() for name in (
        "fix_attempts", "guard_trips", "permission_denied", "stop", "human_rejections", "review_blocking",
        "refine_rounds", "effort_escalations", "undersized", "sizer_fallbacks")}
    decided: dict[str, tuple[Any, Any]] = {}
    plain: Counter[str] = Counter()
    audit: dict | None = None
    routed = False
    settled: dict[str, dict] = {}
    approvals: dict[str, list[bool]] = {}
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
            # a denial in a read-only session (`read_only_roles`) is by design: no rule should be added for it
            if ev.get("tool") and ev.get("read_only") is not True:
                counts["permission_denied"][str(ev["tool"])] += 1
        elif etype == "hitl_raised":
            if ev.get("kind"):
                counts["stop"][str(ev["kind"])] += 1
        elif etype == "phase_rejected":
            if ev.get("phase") and ev.get("by") != RUNNER:
                counts["human_rejections"][str(ev["phase"])] += 1
            elif ev.get("phase") and ev.get("kind") != RECONCILE:
                counts["refine_rounds"][str(ev["phase"])] += 1
        elif etype == "doubt_settled":
            if ev.get("phase"):
                settled[str(ev["phase"])] = ev   # the last one per phase: a crash can settle a phase twice
        elif etype == "review_result":
            if phase := _phase_of(ev.get("key")):
                counts["review_blocking"][phase] += int(ev.get("blocking") or 0)
        elif etype == "effort_decided":
            if ev.get("key"):
                decided[str(ev["key"])] = (ev.get("level"), ev.get("source"))
        elif etype == "effort_escalated":
            if phase := _phase_of(ev.get("key")):
                counts["effort_escalations"][phase] += 1
                level, source = decided.get(str(ev["key"]), (None, None))
                if level in ("low", "medium") and source != "fallback":
                    counts["undersized"][phase] += 1
        elif etype == "phase_approved":
            if (phase := ev.get("phase")) and ev.get("by") != RUNNER and ev.get("produced") != []:
                approvals.setdefault(str(phase), []).append(_informed(ev))
        elif etype == "conformance_checked":
            audit = ev
        elif etype == "route_profile":
            routed = True
            plain["route_upgrades"] += int(ev.get("upgraded_from") is not None)
        elif etype in ("ci_round", "rolled_over", "progress_not_updated", "compacted"):
            plain[{"ci_round": "ci_rounds", "rolled_over": "rollovers", "compacted": "compactions"}.get(etype, etype)] += 1
        elif etype == "run_held" and ev.get("reason") == USAGE_HOLD:
            plain["usage_holds"] += 1
    for key, (_level, source) in decided.items():  # a step's last decision: a retried plan sizer counts once
        if source == "fallback" and (phase := _phase_of(key)):
            counts["sizer_fallbacks"][phase] += 1
    for family, found in counts.items():
        out.update({f"{family}:{param}": n for param, n in found.items()})
    out.update({name: plain[name] for name in ("ci_rounds", "rollovers", "progress_not_updated", "compactions",
                                               "usage_holds")})
    if routed:  # only a run that was triaged has a route to upgrade
        out["route_upgrades"] = plain["route_upgrades"]
    for phase in metrics.get("phases") or []:
        for family in ("fix_attempts", "human_rejections", "refine_rounds"):
            out.setdefault(f"{family}:{phase['phase']}", 0)
    for phase, ev in settled.items():
        for family, field in DOUBT_FIELDS:
            if isinstance(ev.get(field), (int, float)) and not isinstance(ev.get(field), bool):
                out[f"{family}:{phase}"] = ev[field]
    out.update({f"informed_approval:{phase}": round(sum(flags) / len(flags), 4) for phase, flags in approvals.items()})
    out.update(_turns(events))
    out.update(_gate_time(events))
    if audit is not None:  # the last audit only, and only for a run that was audited
        statuses = Counter(f.get("status") for f in audit.get("obligations") or [] if isinstance(f, dict))
        out["conformance_missing"] = statuses["missing"]
        out["conformance_waived"] = statuses["waived"]
    for phase in metrics.get("phases") or []:
        called = sum(1 for s in phase.get("steps") or [] if s.get("attempts"))
        if called:
            out[f"cost_per_task:{phase['phase']}"] = round((float(phase.get("cost_usd") or 0.0) - float(phase.get("sizing_cost_usd") or 0.0)) / called, 6)
    cost = float(metrics.get("cost_usd") or 0.0)
    out["rework_pct"] = round(100 * float(metrics.get("rework_cost_usd") or 0.0) / cost, 2) if cost else 0
    out.update(_lead_time(events))
    wall = float(metrics.get("wall_s") or 0.0)
    if wall > 0:
        out["human_blocked_share"] = round(float((metrics.get("buckets") or {}).get("human") or 0.0) / wall, 4)
    if full_suite:
        out["full_suite_runs"] = _full_suite_runs(events, full_suite)
    out.update(_gate_cache_hit_rate(events))
    out.update(_cost_signals(metrics))
    out.update(_park_signals(events))
    out.update(_wave_signals(events))
    out.update(_agent_parallelism(events, metrics))
    return out


def aggregate_signals(per_run: list[dict[str, float]]) -> dict[str, dict[str, Any]]:
    """`{key: {"runs": n, "mean": m}}`: each key averaged over the runs that have it."""
    values: dict[str, list[float]] = {}
    for signals in per_run:
        for key, value in signals.items():
            values.setdefault(key, []).append(float(value))
    return {key: {"runs": len(v), "mean": sum(v) / len(v)} for key, v in sorted(values.items())}
