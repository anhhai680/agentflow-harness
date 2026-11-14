"""Where a run's time and money went: one pass over its `events.jsonl`, nothing stored.

`run_metrics(events, now)` walks the events in order and charges each gap between two events to the run's
phase and step at that moment, and to one **bucket**, the thing the run was doing:

- `agent`: an agent call is open (`agent_started` .. `agent_exited`).
- `gates`: shell gates were running. A `gate_result` is written just after its gates finish, so the gap
  before it is re-filed from `runner` to `gates`: as much of it as the results' `duration_s` add up to, or
  the whole gap for a run older than `duration_s` (that step is then flagged `approx`).
- `human`: a stop, a phase approval or a ready phase waits on a person (`hitl_raised`,
  `phase_approval_raised`, `phase_ready`) until they act.
- `ci`: the PR's checks are being watched (`ci_watching`).
- `hold`: a usage limit or an outage holds the run (`run_held` .. `run_hold_ended`).
- `down`: no runner is going: paused, stopped on an error, or a silent gap longer than `stall_s` (a
  killed runner). A run not finished whose last event is older than `stall_s` is `stalled`.
- `runner`: anything else (git, commits, prompts, bookkeeping).

Cost is charged on each `agent_exited` through `state.cost_added`, so a resumed session is counted once;
an open call adds its latest `agent_turn` cost. Each call keeps the agent, model and effort it ran on, so
time and cost can be split by `model · effort`. A call that starts after a review reopened its step, a
retry, a fix attempt, a rejection or in a refine round counts as **rework**.
A call whose events carry `sizer` sizes a step's effort: its cost lands in its phase's `cost_usd` and
`sizing_cost_usd`, and it is never a step, an attempt or a `model · effort` row of a step.

The run ends at `run_finished`, `run_failed` or `run_cancelled`; nothing after that is charged but the cost
of a call that exits after it. A run archived without one ends at its last event before `run_archived`: the
idle gap up to the archive is not charged. `aggregate(rows)` sums several runs' metrics for the Metrics tab.
"""

from __future__ import annotations

import math
import statistics
from datetime import datetime
from typing import Any

from agentflow.events import parse_iso
from agentflow.state import RECONCILE_KIND as RECONCILE, cost_added

BUCKETS = ("agent", "gates", "human", "ci", "hold", "down", "runner")
WAITS = {
    "hitl_raised": "human", "phase_approval_raised": "human", "phase_ready": "human",
    "ci_watching": "ci", "run_held": "hold", "run_paused": "down", "runner_error": "down",
}
# Events that end a wait: a person acted, the runner came back, or work started again.
WAIT_ENDS = {
    "hitl_answered", "hitl_approved", "phase_approved", "phase_rejected", "run_resumed", "run_hold_ended",
    "pr_marked_ready", "resolve_started", "ci_round", "phase_started", "step_started", "agent_started",
    "checkpoint_cleared",
}
ENDS = {"run_finished", "run_failed", "run_cancelled", "run_archived"}
REWORK_MARKS = {"step_reopened", "step_retry", "fix_attempt"}
DEFAULT_STALL_S = 3600.0
TOP_STEPS = 10


def _phase_of(key: object) -> str | None:
    return str(key).split("/", 1)[0] if key else None


def _buckets() -> dict[str, float]:
    return dict.fromkeys(BUCKETS, 0.0)


class _Fold:
    def __init__(self, stall_s: float) -> None:
        self.stall_s = stall_s
        self.phase: str | None = None
        self.step: str | None = None
        self.wait: str | None = None
        self.wait_target: tuple[str | None, str | None] | None = None
        self.open: list[dict] = []          # agent calls not exited yet
        self.calls: list[dict] = []
        self.phases: dict[str, dict] = {}
        self.steps: dict[str, dict] = {}
        self.counted: dict[str, float] = {}
        self.last_turn_cost: dict[str, float] = {}
        self.step_mode: dict[str, tuple[Any, Any]] = {}
        self.rework_pending: set[str] = set()
        self.runner_gap: tuple[float, str | None, str | None] | None = None  # the last gap charged to runner
        self.gate_pending = 0.0
        self.approx: set[str] = set()
        self.started_at: str | None = None
        self.ended_at: str | None = None
        self.phase_status: dict[str, str] = {}
        self.estimated = False
        self.last_ts: str | None = None

    # ------------------------------------------------------------------ rows

    def _phase_row(self, phase: str) -> dict:
        if phase not in self.phases:
            self.phases[phase] = {"phase": phase, "buckets": _buckets(), "cost_usd": 0.0, "rework_cost_usd": 0.0,
                                  "sizing_cost_usd": 0.0, "attempts": 0, "rework_attempts": 0, "first_at": None, "last_at": None}
        return self.phases[phase]

    def _step_row(self, key: str) -> dict:
        if key not in self.steps:
            self.steps[key] = {"key": key, "phase": _phase_of(key), "buckets": _buckets(), "cost_usd": 0.0,
                               "rework_cost_usd": 0.0, "attempts": 0, "rework_attempts": 0, "fixes": 0,
                               "retries": 0, "reopened": 0, "review_rounds": 0, "role": None, "calls": []}
        return self.steps[key]

    def _charge(self, bucket: str, seconds: float, phase: str | None, step: str | None) -> None:
        if seconds == 0 or phase is None:
            return
        self._phase_row(phase)["buckets"][bucket] += seconds
        if step:
            self._step_row(step)["buckets"][bucket] += seconds

    # ------------------------------------------------------------------ the walk

    def _bucket(self) -> str:
        if self.wait:
            return self.wait
        return "agent" if self.open else "runner"

    def _target(self) -> tuple[str | None, str | None]:
        if self.wait and self.wait_target:
            return self.wait_target
        if self.open:
            key = self.open[-1].get("key")
            return _phase_of(key) or self.phase, None if self.open[-1].get("sizer") else key
        return self.phase, self.step

    def gap(self, seconds: float, nxt: dict) -> None:
        bucket = self._bucket()
        phase, step = self._target()
        if bucket in ("agent", "runner") and seconds > self.stall_s and nxt.get("type") != "gate_result":
            bucket = "down"  # nothing was written for longer than any call may run: no runner was going
        self._charge(bucket, seconds, phase, step)
        if bucket != "runner":
            self.runner_gap = None
        elif self.runner_gap is not None and self.runner_gap[1:] == (phase, step):  # the next result of one batch
            self.runner_gap = (self.runner_gap[0] + seconds, phase, step)
        else:
            self.runner_gap = (seconds, phase, step)

    def event(self, ev: dict) -> None:
        etype = ev.get("type")
        key = ev.get("key") or ev.get("step")
        if etype != "gate_result":
            self.runner_gap, self.gate_pending = None, 0.0
        if etype in WAIT_ENDS:
            self.wait, self.wait_target = None, None
        if etype in WAITS:
            self.wait = WAITS[etype]
            self.wait_target = (_phase_of(key) or ev.get("phase") or self.phase, key if key and "/" in str(key) else None)
        handler = getattr(self, f"_on_{etype}", None)
        if handler is not None:
            handler(ev, key)

    def _on_run_started(self, ev: dict, key: Any) -> None:
        self.started_at = self.started_at or ev.get("ts")

    def _on_phase_started(self, ev: dict, key: Any) -> None:
        self.phase, self.step = ev.get("phase"), None
        row = self._phase_row(str(self.phase))
        row["first_at"] = row["first_at"] or ev.get("ts")
        self.phase_status[str(self.phase)] = "running"

    def _on_phase_skipped(self, ev: dict, key: Any) -> None:
        self.phase_status[str(ev.get("phase"))] = "skipped"

    def _on_phase_approved(self, ev: dict, key: Any) -> None:
        self.phase_status[str(ev.get("phase"))] = "approved"

    def _on_phase_rejected(self, ev: dict, key: Any) -> None:
        if ev.get("kind") == RECONCILE:  # the runner's own round on a doubt review's findings is not rework
            return
        phase = str(ev.get("phase"))
        self.rework_pending.update(k for k in self.steps if _phase_of(k) == phase)

    def _on_step_started(self, ev: dict, key: Any) -> None:
        self.step = key
        if key:
            self.step_mode[key] = (ev.get("mode"), ev.get("round"))
            if ev.get("mode") == "refine" or ev.get("round"):
                self.rework_pending.add(key)

    def _on_step_finished(self, ev: dict, key: Any) -> None:
        if key:
            self.rework_pending.discard(key)

    def _on_step_reopened(self, ev: dict, key: Any) -> None:
        if key:
            self._step_row(key)["reopened"] += 1
            self.rework_pending.add(key)

    def _on_step_retry(self, ev: dict, key: Any) -> None:
        if key:
            self._step_row(key)["retries"] += 1
            self.rework_pending.add(key)

    def _on_fix_attempt(self, ev: dict, key: Any) -> None:
        if key:
            self._step_row(key)["fixes"] += 1
            self.rework_pending.add(key)

    def _on_review_result(self, ev: dict, key: Any) -> None:
        if key:
            row = self._step_row(key)
            row["review_rounds"] = max(row["review_rounds"], int(ev.get("round") or 0))

    def _on_agent_started(self, ev: dict, key: Any) -> None:
        key = key or self.step
        sizer = bool(ev.get("sizer"))  # sizes a step's effort: its cost is the phase's, it is no step's attempt
        mode, round_ = self.step_mode.get(key or "", (None, None))
        call = {
            "key": key, "session_id": ev.get("session_id"), "pid": ev.get("pid"), "agent": ev.get("agent"),
            "model": ev.get("model"), "model_alias": ev.get("model"), "model_source": None, "effort": ev.get("effort"),
            "role": ev.get("role"), "sizing": bool(ev.get("sizing")), "sizer": sizer, "mode": mode,
            "round": round_, "rework": bool(key and not sizer and key in self.rework_pending), "started_at": ev.get("ts"), "ended_at": None,
            "agent_s": 0.0, "cost_usd": 0.0, "cost_estimated": False, "live": True, "exit_code": None,
        }
        self.open.append(call)
        self.calls.append(call)
        if key and not sizer:
            row = self._step_row(key)
            row["attempts"] += 1
            row["rework_attempts"] += int(call["rework"])
            row["role"] = row["role"] or ev.get("role")
            phase = self._phase_row(_phase_of(key) or str(self.phase))
            phase["attempts"] += 1
            phase["rework_attempts"] += int(call["rework"])

    def _find_open(self, ev: dict) -> dict | None:
        for match in ("session_id", "pid", "key"):
            value = ev.get(match)
            if value is None:
                continue
            for call in reversed(self.open):
                if call.get(match) == value:
                    return call
        return self.open[-1] if len(self.open) == 1 else None

    def _on_agent_session(self, ev: dict, key: Any) -> None:
        call = self._find_open({"pid": ev.get("pid"), "key": ev.get("key")})
        if call is not None:
            call["session_id"] = ev.get("session_id") or call["session_id"]
            call["model"] = ev.get("model") or call["model"]

    def _on_agent_turn(self, ev: dict, key: Any) -> None:
        if ev.get("session_id"):
            self.last_turn_cost[str(ev["session_id"])] = float(ev.get("cost") or 0.0)

    def _on_agent_exited(self, ev: dict, key: Any) -> None:
        call = self._find_open(ev)
        added = cost_added(self.counted, ev)
        if call is None:  # an exit with no start on record: still money spent
            call = {"key": key, "session_id": ev.get("session_id"), "agent": None, "model": None, "effort": None,
                    "role": None, "rework": False, "started_at": ev.get("ts"), "agent_s": 0.0, "mode": None,
                    "round": None, "model_source": None, "sizing": False, "model_alias": None,
                    "sizer": bool(ev.get("sizer"))}
            self.calls.append(call)
        else:
            self.open.remove(call)
        start, end = parse_iso(call.get("started_at")), parse_iso(ev.get("ts"))
        call.update({"ended_at": ev.get("ts"), "cost_usd": added, "cost_estimated": bool(ev.get("cost_estimated")),
                     "live": False, "exit_code": ev.get("exit_code"),
                     "agent_s": max(0.0, (end - start).total_seconds()) if start and end else 0.0})
        self.estimated = self.estimated or bool(ev.get("cost_estimated"))
        self._spend(call)

    def _spend(self, call: dict) -> None:
        key = call.get("key")
        phase = _phase_of(key) or self.phase
        cost = float(call.get("cost_usd") or 0.0)
        if phase is None:
            return
        row = self._phase_row(phase)
        row["cost_usd"] += cost
        if call.get("sizer"):
            row["sizing_cost_usd"] += cost
            return
        if call.get("rework"):
            row["rework_cost_usd"] += cost
        if key:
            step = self._step_row(key)
            step["cost_usd"] += cost
            if call.get("rework"):
                step["rework_cost_usd"] += cost

    def _on_gate_result(self, ev: dict, key: Any) -> None:
        if self.runner_gap is None:
            return
        seconds, phase, step = self.runner_gap
        duration = ev.get("duration_s")
        if isinstance(duration, (int, float)) and not isinstance(duration, bool):
            self.gate_pending += float(duration)
            moved = min(self.gate_pending, seconds)
        else:
            moved = seconds
            if key:
                self.approx.add(str(key))
        if moved <= 0:
            return
        self._charge("runner", -moved, phase, step)
        self._charge("gates", moved, _phase_of(key) or phase, key or step)
        self.gate_pending -= moved
        rest = seconds - moved
        self.runner_gap = (rest, phase, step) if rest > 0 else None

    # ------------------------------------------------------------------ the end

    def finish(self, now: datetime) -> None:
        for call in self.open:  # still running: its last reported turn cost, and time up to now
            start = parse_iso(call.get("started_at"))
            call["agent_s"] = max(0.0, (now - start).total_seconds()) if start else 0.0
            call["cost_usd"] = self.last_turn_cost.get(str(call.get("session_id")), 0.0)
            call["cost_estimated"] = True
            self._spend(call)


def _round_buckets(buckets: dict[str, float]) -> dict[str, float]:
    return {name: round(value, 1) for name, value in buckets.items()}


def _models(calls: list[dict]) -> list[dict]:
    groups: dict[tuple, dict] = {}
    for call in calls:
        group = (call.get("agent"), call.get("model"), call.get("effort"))
        row = groups.setdefault(group, {"agent": group[0], "model": group[1], "effort": group[2], "sessions": 0,
                                        "agent_s": 0.0, "cost_usd": 0.0})
        row["sessions"] += 1
        row["agent_s"] += float(call.get("agent_s") or 0.0)
        row["cost_usd"] += float(call.get("cost_usd") or 0.0)
    out = sorted(groups.values(), key=lambda r: -r["cost_usd"])
    return [{**r, "agent_s": round(r["agent_s"], 1), "cost_usd": round(r["cost_usd"], 6)} for r in out]


def _call_view(call: dict) -> dict:
    keep = ("session_id", "agent", "model", "model_alias", "effort", "role", "sizing", "mode", "round", "rework",
            "started_at", "ended_at", "exit_code", "live", "cost_estimated")
    return {**{k: call.get(k) for k in keep}, "agent_s": round(float(call.get("agent_s") or 0.0), 1),
            "cost_usd": round(float(call.get("cost_usd") or 0.0), 6)}


def run_metrics(events: list[dict], now: datetime, stall_s: float = DEFAULT_STALL_S) -> dict:
    """Time and cost by phase, step, bucket and `model · effort` for one run's events (see the module docstring)."""
    fold = _Fold(stall_s)
    prev: datetime | None = None
    effort_changes: dict[str, list[dict]] = {}
    for ev in events:
        ts = parse_iso(ev.get("ts"))
        if fold.ended_at is not None:
            if ev.get("type") == "agent_exited":  # a call killed by the cancel still cost money
                fold.event(ev)
            continue
        if prev is not None and ts is not None and ev.get("type") != "run_archived":
            fold.gap(max(0.0, (ts - prev).total_seconds()), ev)
        if ev.get("type") == "effort_changed" and ev.get("key"):
            effort_changes.setdefault(str(ev["key"]), []).append(
                {"from": ev.get("from"), "to": ev.get("to"), "reason": ev.get("reason"), "at": ev.get("ts")})
        fold.event(ev)
        if ts is not None:
            prev = ts
        if ev.get("type") in ENDS:
            fold.ended_at = ev.get("ts") if ev.get("type") != "run_archived" else (fold.last_ts or ev.get("ts"))
        elif ts is not None:
            fold.last_ts = ev.get("ts")
    stalled = False
    if fold.ended_at is None and prev is not None:
        tail = max(0.0, (now - prev).total_seconds())
        bucket, (phase, step) = fold._bucket(), fold._target()
        if bucket in ("agent", "runner") and tail > stall_s:
            bucket, stalled = "down", True
        fold._charge(bucket, tail, phase, step)
        fold.finish(now)
    return _shape(fold, now, stalled, effort_changes)


def _shape(fold: _Fold, now: datetime, stalled: bool, effort_changes: dict[str, list[dict]]) -> dict:
    calls_by_key: dict[str, list[dict]] = {}
    for call in fold.calls:
        if call.get("sizer"):
            continue
        calls_by_key.setdefault(str(call.get("key")), []).append(call)
    phases = []
    for name, row in fold.phases.items():
        phase_calls = [c for c in fold.calls if (_phase_of(c.get("key")) or name) == name and c.get("key")]
        steps = []
        for key, step in fold.steps.items():
            if step["phase"] != name:
                continue
            calls = calls_by_key.get(key, [])
            steps.append({
                **{k: step[k] for k in ("key", "role", "attempts", "rework_attempts", "fixes", "retries", "reopened",
                                        "review_rounds")},
                "label": key.split("/", 1)[1] if "/" in key else key,
                "wall_s": round(sum(step["buckets"].values()), 1),
                "buckets": _round_buckets(step["buckets"]),
                "cost_usd": round(step["cost_usd"], 6),
                "rework_cost_usd": round(step["rework_cost_usd"], 6),
                "approx": key in fold.approx,
                "models": _models(calls),
                "effort_changes": effort_changes.get(key, []),
                "sessions": [_call_view(c) for c in calls],
            })
        phases.append({
            "phase": name,
            "status": fold.phase_status.get(name),
            "first_at": row["first_at"],
            "wall_s": round(sum(row["buckets"].values()), 1),
            "buckets": _round_buckets(row["buckets"]),
            "cost_usd": round(row["cost_usd"], 6),
            "sizing_cost_usd": round(row["sizing_cost_usd"], 6),
            "rework_cost_usd": round(row["rework_cost_usd"], 6),
            "attempts": row["attempts"],
            "rework_attempts": row["rework_attempts"],
            "approx": any(s["approx"] for s in steps),
            "models": _models(phase_calls),
            "steps": steps,
        })
    total = _buckets()
    for row in fold.phases.values():
        for bucket, value in row["buckets"].items():
            total[bucket] += value
    by_model: dict[str, dict] = {}
    for model in _models(fold.calls):
        by_model[f"{model['model'] or '?'}·{model['effort'] or '?'}"] = model
    by_role: dict[str, dict] = {}
    for call in fold.calls:
        role = by_role.setdefault(str(call.get("role")), {"sessions": 0, "agent_s": 0.0, "cost_usd": 0.0})
        role["sessions"] += 1
        role["agent_s"] = round(role["agent_s"] + float(call.get("agent_s") or 0.0), 1)
        role["cost_usd"] = round(role["cost_usd"] + float(call.get("cost_usd") or 0.0), 6)
    cost = sum(row["cost_usd"] for row in fold.phases.values())
    return {
        "started_at": fold.started_at,
        "ended_at": fold.ended_at,
        "live": fold.ended_at is None,
        "stalled": stalled,
        "wall_s": round(sum(total.values()), 1),
        "buckets": _round_buckets(total),
        "cost_usd": round(cost, 6),
        "rework_cost_usd": round(sum(row["rework_cost_usd"] for row in fold.phases.values()), 6),
        "cost_estimated": fold.estimated or any(c.get("live") for c in fold.calls),
        "approx": bool(fold.approx),
        "phases": phases,
        "by_model": by_model,
        "by_role": by_role,
        "generated_at": now.isoformat(timespec="seconds"),
    }


# --------------------------------------------------------------------------- many runs


def _p90(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(0.9 * len(ordered)) - 1)] if ordered else 0.0


def _spread(values: list[float]) -> dict[str, float]:
    if not values:
        return {"total": 0.0, "median": 0.0, "p90": 0.0}
    return {"total": round(sum(values), 6), "median": round(statistics.median(values), 6),
            "p90": round(_p90(values), 6)}


def aggregate(rows: list[dict]) -> dict:
    """Sum the `run_metrics` of several runs (each row also carries `run_id`, `lane`, `module`): totals, the
    median and p90 per lane and phase, the share of each bucket, cost by `model · effort` per phase, and the
    costliest and slowest steps."""
    buckets = _buckets()
    cost = rework = 0.0
    lanes: dict[str, dict] = {}
    model_cells: dict[tuple[str, str], dict] = {}
    steps: list[dict] = []
    for row in rows:
        metrics = row["metrics"]
        cost += metrics["cost_usd"]
        rework += metrics["rework_cost_usd"]
        for bucket, value in metrics["buckets"].items():
            buckets[bucket] += value
        lane = lanes.setdefault(str(row.get("lane")), {"runs": 0, "wall": [], "cost": [], "phases": {}})
        lane["runs"] += 1
        lane["wall"].append(metrics["wall_s"])
        lane["cost"].append(metrics["cost_usd"])
        for phase in metrics["phases"]:
            cell = lane["phases"].setdefault(phase["phase"], {"wall": [], "cost": [], "buckets": _buckets(),
                                                             "rework_cost_usd": 0.0, "attempts": 0})
            cell["wall"].append(phase["wall_s"])
            cell["cost"].append(phase["cost_usd"])
            cell["rework_cost_usd"] += phase["rework_cost_usd"]
            cell["attempts"] += phase["attempts"]
            for bucket, value in phase["buckets"].items():
                cell["buckets"][bucket] += value
            for model in phase["models"]:
                name = f"{model['model'] or '?'}·{model['effort'] or '?'}"
                m = model_cells.setdefault((phase["phase"], name), {"phase": phase["phase"], "model": name,
                                                                    "sessions": 0, "agent_s": 0.0, "cost_usd": 0.0})
                m["sessions"] += model["sessions"]
                m["agent_s"] = round(m["agent_s"] + model["agent_s"], 1)
                m["cost_usd"] = round(m["cost_usd"] + model["cost_usd"], 6)
            for step in phase["steps"]:
                steps.append({"run_id": row.get("run_id"), "module": row.get("module"), "lane": row.get("lane"),
                              "key": step["key"], "wall_s": step["wall_s"], "cost_usd": step["cost_usd"],
                              "attempts": step["attempts"], "rework_attempts": step["rework_attempts"],
                              "review_rounds": step["review_rounds"], "fixes": step["fixes"],
                              "models": [f"{m['model'] or '?'}·{m['effort'] or '?'}" for m in step["models"]]})
    wall = sum(buckets.values())
    lane_out = {}
    for name, lane in lanes.items():
        lane_out[name] = {
            "runs": lane["runs"], "wall_s": _spread(lane["wall"]), "cost_usd": _spread(lane["cost"]),
            "phases": {p: {"runs": len(c["wall"]), "wall_s": _spread(c["wall"]), "cost_usd": _spread(c["cost"]),
                           "buckets": _round_buckets(c["buckets"]), "rework_cost_usd": round(c["rework_cost_usd"], 6),
                           "attempts": c["attempts"]}
                       for p, c in lane["phases"].items()},
        }
    return {
        "runs": len(rows),
        "cost_usd": round(cost, 6),
        "rework_cost_usd": round(rework, 6),
        "rework_pct": round(rework / cost * 100, 1) if cost else 0.0,
        "wall_s": _spread([r["metrics"]["wall_s"] for r in rows]),
        "buckets": _round_buckets(buckets),
        "bucket_pct": {b: round(v / wall * 100, 1) if wall else 0.0 for b, v in buckets.items()},
        "lanes": lane_out,
        "models": sorted(model_cells.values(), key=lambda m: (m["phase"], -m["cost_usd"])),
        "costliest_steps": sorted(steps, key=lambda s: -s["cost_usd"])[:TOP_STEPS],
        "slowest_steps": sorted(steps, key=lambda s: -s["wall_s"])[:TOP_STEPS],
    }


__all__ = ["BUCKETS", "DEFAULT_STALL_S", "aggregate", "run_metrics"]
