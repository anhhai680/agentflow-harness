"""Run state: `state.json` is the reduction of `events.jsonl`, and `Run` is the one writer of both.

`reduce` is pure and tolerant: it copies the state, applies one event and returns the copy; an
event type it does not know is kept in the log and changes nothing but `last_seq`/`updated_at`.
`Run.emit` appends an event, applies it, and writes `state.json` atomically (tmp + rename) while
holding the log's lock, so two actors on one run cannot interleave. Nothing else writes `state.json`,
and nothing reads it back: `Run.refresh` rebuilds the state from the events (`state.json` is a view).
"""

from __future__ import annotations

import copy
import json
import os
import re
import tempfile
import unicodedata
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from . import pack
from .config import KNOWN_EFFORT_LEVELS, RETRO_PIPELINE, Config, check_lane
from .events import EventError, EventLog, RunLock, lock_state, nfc, now_iso

TERMINAL = ("done", "failed", "cancelled")
MARKED_DONE_SKIP = "marked done after"  # the `reason` prefix of a `phase_skipped` that a mark-done caused
SLUG_PARAMS = ("slug", "module", "issue", "topic", "task", "log")  # an explicit slug (an issue's) wins
SLUG_MAX = 40
ENGINE_PIPELINES = (RETRO_PIPELINE,)  # pipelines the engine runs itself (`Run.create(..., engine_pipeline=True)`)
CODE_MAP_MAX = 40  # existing code a step reported that the run keeps for every later session (the newest)
RUN_FACTS_MAX = 8  # what a resolver found that the run keeps for every later session (the newest)


class RunNotFound(Exception):
    """No run (or more than one) matches the id the caller gave."""


# --------------------------------------------------------------------------- reducer


def initial_state() -> dict:
    return {
        "run_id": None,
        "pipeline": None,
        "params": {},
        "status": "running",
        "branch": None,
        "worktree": None,
        "base_sha": None,
        "pr_url": None,
        "backlog_sha": None,  # the newest `backlog_updated` commit: the run reads its own item at this sha
        "claim": None,  # the run's claim on its backlog item on `origin` (`_on_item_claimed`); None when it holds none
        "phase": None,
        "step": None,
        "task": None,
        "task_total": None,
        "phases": {},
        "steps": {},
        "agents": {},
        "exited": [],
        "waiting": None,
        "refine": None,
        "resolving": None,
        "ci": None,
        "ready": None,
        "marked_done": None,
        "archived": None,
        "spend_usd": 0.0,
        "session_costs": {},
        "hold": None,
        "budget_usd": None,  # the hard limit: the run stops when its spend crosses it
        "soft_budget_usd": None,  # the soft limit: crossing it only notifies; None for a run started before it
        "context_windows": {},
        "usage_window": {},  # the latest `usage_window` per window (`five_hour`, `seven_day`): the plan's usage limits
        "usage_governed": None,  # the last `usage_governed`: what `usage.governor: observe` would have held
        "cli_settings_ignored": [],  # the settings a `cli_setting_ignored` event noted: each once per run
        "env_dropped": [],
        "facts": [],
        "code_map": [],
        "gates": {},
        "reviews": {},
        "effort_changes": [],
        "route": None,  # the last `route_profile`: the run's route profile (`route.py` reads it)
        "footer_mismatches": [],
        "failure": None,
        "options": {},
        "waiting_queue": [],
        "parked": {},
        "malformed_events": [],
        "last_seq": 0,
        "updated_at": None,
    }


def _phase_of(key: str | None) -> str | None:
    return key.split("/")[0] if key else None


def _phase_row(state: dict, phase: str) -> dict:
    return state["phases"].setdefault(
        phase, {"status": "pending", "round": 0, "start_sha": None, "approval_sha": None, "skip_reason": None}
    )


def _step_row(state: dict, key: str, phase: str | None = None) -> dict:
    row = state["steps"].setdefault(
        key,
        {
            "status": "pending",
            "phase": phase or _phase_of(key),
            "agent": None,
            "session_id": None,
            "model": None,
            "model_source": None,
            "effort": None,
            "effort_changed": None,
            "sized": False,
            "effort_source": None,
            "attempts": 0,
            "rollovers": 0,
            "result": None,
            "skip_reason": None,
            "round": 0,
        },
    )
    if phase and not row["phase"]:
        row["phase"] = phase
    return row


def _direction(old: str | None, new: str | None) -> str | None:
    if old in KNOWN_EFFORT_LEVELS and new in KNOWN_EFFORT_LEVELS:
        a, b = KNOWN_EFFORT_LEVELS.index(old), KNOWN_EFFORT_LEVELS.index(new)
        return "up" if b > a else "down" if b < a else None
    return None


def _find_agent(state: dict, event: dict) -> tuple[str | None, dict | None]:
    """The live agent row an event is about: by session, pid or step key, else the newest live row."""
    agents = state["agents"]
    session = event.get("session_id") or event.get("parent_session")
    if session is not None:
        if str(session) in agents:
            return str(session), agents[str(session)]
        for name, row in agents.items():
            if row["session_id"] == session:
                return name, row
    if event.get("pid") is not None:
        for name, row in agents.items():
            if row["pid"] == event["pid"]:
                return name, row
    key = event.get("key") or event.get("step")
    if key:
        for name in reversed(list(agents)):
            if agents[name]["step"] == key:
                return name, agents[name]
    if agents and session is None:
        name = next(reversed(agents))
        return name, agents[name]
    return None, None


def _clear_wait(state: dict) -> None:
    """Drop the current wait and promote the next queued one, if any. The last one cleared leaves the run `ready` when
    a person held an approval while it waited (`_on_phase_ready`, speculation only), else `running`."""
    state["waiting"] = None
    if state["waiting_queue"]:
        state["waiting"] = state["waiting_queue"].pop(0)
        if state["status"] not in TERMINAL:
            state["status"] = "waiting_human"
    elif state["status"] in ("waiting_human", "awaiting_approval"):
        state["status"] = "ready" if (state.get("ready") or {}).get("speculative") else "running"


def _approval_pending(state: dict) -> bool:
    """A phase approval is the current wait: only `phase_approved` and `phase_rejected` may end it."""
    return state["waiting"] is not None and state["waiting"].get("kind") == "phase_approval"


def speculating(state: dict) -> dict | None:
    """The speculation record while the approval of the phase it follows (`after`) is still the run's wait, else None:
    the runner is looking ahead at `phase` while a human decides on `after`."""
    record = state.get("speculation")
    if record and _approval_pending(state) and state["waiting"].get("phase") == record.get("after"):
        return record
    return None


def _on_speculation_started(state: dict, ev: dict) -> None:
    state["speculation"] = {"phase": ev["phase"], "after": ev["after"], "basis": copy.deepcopy(ev["basis"]),
                            "sha": ev.get("sha"), "since": ev.get("ts")}


def _on_speculation_ended(state: dict, ev: dict) -> None:
    state["speculation"] = None


def _on_run_started(state: dict, ev: dict) -> None:
    state["run_id"] = ev.get("run_id", state["run_id"])
    state["pipeline"] = ev.get("pipeline", state["pipeline"])
    state["params"] = copy.deepcopy(ev.get("params") or {})
    for field in ("branch", "worktree", "base_sha"):
        if ev.get(field) is not None:
            state[field] = ev[field]
    for field in ("budget_usd", "soft_budget_usd"):
        if ev.get(field) is not None:
            state[field] = ev[field]
    state["status"] = "running"


def _on_run_options(state: dict, ev: dict) -> None:
    state["options"] = {k: copy.deepcopy(v) for k, v in ev.items() if k not in ("seq", "ts", "type", "by")}


def _on_run_updated(state: dict, ev: dict) -> None:
    for field in ("branch", "worktree", "base_sha", "pr_url", "budget_usd", "soft_budget_usd"):
        if ev.get(field) is not None:
            state[field] = ev[field]


def _on_phase_started(state: dict, ev: dict) -> None:
    phase = ev["phase"]
    state["phases"][phase] = {
        "status": "running", "round": 0, "start_sha": ev.get("start_sha"), "approval_sha": None, "skip_reason": None,
    }
    state["phase"] = phase


def _on_phase_skipped(state: dict, ev: dict) -> None:
    row = _phase_row(state, ev["phase"])
    row["status"] = "skipped"
    row["skip_reason"] = ev.get("reason")


def _on_step_started(state: dict, ev: dict) -> None:
    key = ev["key"]
    row = _step_row(state, key, ev.get("phase"))
    row["status"] = "running"
    for field in ("agent", "model", "model_source", "effort"):
        if ev.get(field) is not None:
            row[field] = ev[field]
    if ev.get("round") is not None:
        row["round"] = ev["round"]
    state["step"] = key
    state["phase"] = row["phase"] or state["phase"]
    if isinstance(ev.get("task"), dict):
        state["task"] = {"n": ev["task"].get("n"), "of": ev["task"].get("of")}
        if state.get("task_total") is None and ev["task"].get("of"):
            state["task_total"] = ev["task"]["of"]  # fixed by the first task step: never moves again
    elif "/task-" not in key:
        state["task"] = None


def _on_step_finished(state: dict, ev: dict) -> None:
    row = _step_row(state, ev["key"])
    row["status"] = ev.get("status", "done")
    row["result"] = ev.get("summary")
    row["sized"] = True


def _on_step_skipped(state: dict, ev: dict) -> None:
    row = _step_row(state, ev.get("key") or ev["step"], ev.get("phase"))
    row["status"] = "skipped"
    row["skip_reason"] = ev.get("reason")
    row["condition"] = ev.get("condition")


def _on_step_unchanged(state: dict, ev: dict) -> None:
    row = _step_row(state, ev.get("key") or ev["step"])
    row["status"] = "unchanged"
    row["result"] = ev.get("reason")


def _on_step_retry(state: dict, ev: dict) -> None:
    row = _step_row(state, ev["key"])
    failed = "failed" in (row["status"], row.get("retry_of"))  # sticky: a second retry keeps the tree check
    row["retry_of"] = "failed" if failed else row["status"]
    row["status"] = "pending"
    state.setdefault("parked", {}).pop(ev["key"], None)  # a retried parked stop starts afresh


def _on_agent_started(state: dict, ev: dict) -> None:
    key = ev.get("step") or ev.get("key") or state["step"]
    session, pid = ev.get("session_id"), ev.get("pid")
    name = str(session) if session else str(pid)
    sizing = bool(ev.get("sizing"))
    sizer = bool(ev.get("sizer"))  # a call that sizes a step's effort: it is never a step of its own
    state["hold"] = None  # a runner that died in a hold never recorded its end
    state["agents"][name] = {
        "session_id": session,
        "pid": pid,
        "agent": ev.get("agent"),
        "phase": ev.get("phase") or _phase_of(key) or state["phase"],
        "step": key,
        "role": ev.get("role"),
        "model": ev.get("model"),
        "effort": ev.get("effort"),
        "effort_changed": None,
        "sizing": sizing,
        "sizer": sizer,
        "status": "sizing" if sizing else "running",
        "context_pct": 0.0,
        "spend": 0.0,
        "started_at": ev.get("ts"),
        "last_activity": ev.get("ts"),
        "last_tool": None,
        "subagents": [],
    }
    if key and not sizer:
        row = _step_row(state, key)
        row["attempts"] += 1
        row["agent"] = ev.get("agent") or row["agent"]
        row["model"] = ev.get("model") or row["model"]
        row["effort"] = ev.get("effort") or row["effort"]
        if session:
            row["session_id"] = session
        if not sizing:
            row["sized"] = True
            # The session's pinned brief (`lanes.build_brief`), on every call: a shared session's later task carries it
            # on. A call with none (a session from before the brief, Codex, `run_brief` off) has None. The pin is bound
            # to its session (`brief_session`), and the row's `brief` always belongs to its `session_id`: a resumed
            # call's pin is the resumed session's, and a fresh call's waits (`brief_pending`) for that call's
            # `agent_session`, so a fresh call that dies before it never pairs the old session with the new pin.
            pin = ev["brief"] if isinstance(ev.get("brief"), dict) else None
            # The session's read-only setting (`read_only_roles`), kept beside it the same way: a resume takes it,
            # whatever the config says now. A call from before the setting has none, which reads as False.
            read_only = ev.get("read_only") is True
            if session:
                row["brief"], row["brief_session"] = pin, session
                row["read_only"], row["read_only_session"] = read_only, session
                row.pop("brief_pending", None)
                row.pop("read_only_pending", None)
            else:
                row["brief_pending"] = pin
                row["read_only_pending"] = read_only


def _on_slot_acquired(state: dict, ev: dict) -> None:
    state["slot"] = ev["slot"]


def _on_run_held(state: dict, ev: dict) -> None:
    state["hold"] = {"key": ev["key"], "reason": ev["reason"], "until": ev["until"]}


def _on_run_hold_ended(state: dict, ev: dict) -> None:
    state["hold"] = None


def _on_usage_governed(state: dict, ev: dict) -> None:
    state["usage_governed"] = {name: ev.get(name) for name in ("key", "level", "window", "utilization", "until")}


def _on_agent_session(state: dict, ev: dict) -> None:
    session = ev.get("session_id")
    name, row = _find_agent(state, {"pid": ev.get("pid"), "key": ev.get("key")})
    if row is not None and session:
        row["session_id"] = session
        row["model"] = ev.get("model") or row["model"]
        state["agents"][str(session)] = state["agents"].pop(name)
    key = ev.get("key") or (row["step"] if row else None)
    if key and session and not ev.get("sizer") and not (row and row.get("sizer")):
        step = _step_row(state, key)
        step["session_id"] = session
        step["model"] = ev.get("model") or step["model"]
        if "brief_pending" in step:  # the fresh call's session is known: its pin is the row's now
            step["brief"] = step.pop("brief_pending")
        if "brief" in step:  # the call in flight passed the row's pin (`_on_agent_started`): bound to its session
            step["brief_session"] = session
        if "read_only_pending" in step:
            step["read_only"] = step.pop("read_only_pending")
        if "read_only" in step:
            step["read_only_session"] = session


def _on_agent_turn(state: dict, ev: dict) -> None:
    _, row = _find_agent(state, ev)
    if row is None:
        return
    row["context_pct"] = ev.get("context_pct", row["context_pct"])
    row["spend"] = ev.get("cost", row["spend"])
    row["last_tool"] = ev.get("last_tool") or row["last_tool"]
    row["last_activity"] = ev.get("ts") or row["last_activity"]
    row["status"] = "sizing" if row["sizing"] else "running"


def cost_added(counted: dict[str, float], ev: dict) -> float:
    """What one `agent_exited` adds to the spend; `counted` keeps each session's USD so far.

    A reported cost is Claude's `total_cost_usd`, which spans every `--resume` of the session, so only
    the part not counted yet is new. That also picks up a call whose runner died before it could record
    the exit. An estimate (`cost_estimated`) prices its own call only."""
    cost = float(ev.get("cost") or 0.0)
    session = ev.get("session_id")
    if not session:
        return cost
    so_far = counted.get(session, 0.0)
    added = cost if ev.get("cost_estimated") else max(0.0, cost - so_far)
    counted[session] = so_far + added
    return added


def _on_agent_exited(state: dict, ev: dict) -> None:
    name, row = _find_agent(state, ev)
    gone = state["agents"].pop(name) if name is not None else {"session_id": ev.get("session_id"), "pid": None}
    cost = cost_added(state.setdefault("session_costs", {}), ev)
    gone.setdefault("sizer", bool(ev.get("sizer")))
    gone.update({"status": "exited", "exit_code": ev.get("exit_code"), "spend": cost, "exited_at": ev.get("ts")})
    state["exited"].append(gone)
    state["spend_usd"] = round(state["spend_usd"] + cost, 6)
    window = ev.get("context_window")
    if isinstance(window, int) and gone.get("model"):
        state["context_windows"][gone["model"]] = window


def _on_effort_changed(state: dict, ev: dict) -> None:
    _, row = _find_agent(state, ev)
    new = ev.get("to")
    direction = _direction(ev.get("from"), new)
    key = ev.get("key") or (row["step"] if row else None) or state["step"]
    if row is not None:
        row["effort"] = new
        row["effort_changed"] = direction
    if key and not ev.get("sizer") and not (row and row.get("sizer")):
        step = _step_row(state, key)
        step["effort"] = new
        step["effort_changed"] = direction
    state["effort_changes"].append({"key": key, "from": ev.get("from"), "to": new, "reason": ev.get("reason")})


def _on_effort_decided(state: dict, ev: dict) -> None:
    row = _step_row(state, ev["key"])
    row["effort"] = ev["level"]
    row["sized"] = True
    row["effort_source"] = ev.get("source")


def _on_effort_escalated(state: dict, ev: dict) -> None:
    row = _step_row(state, ev["key"])
    row["effort"] = ev["to"]
    row["effort_changed"] = _direction(ev.get("from"), ev["to"])
    state["effort_changes"].append(
        {"key": ev["key"], "from": ev.get("from"), "to": ev["to"], "reason": ev.get("trigger")})


def _on_sizer_started(state: dict, ev: dict) -> None:
    """Read from the log, not from the state."""


def _on_plan_sized(state: dict, ev: dict) -> None:
    """Read from the log, not from the state."""


def _on_sizer_finished(state: dict, ev: dict) -> None:
    """Read from the log, not from the state."""


ROUTE_FIELDS = ("profile", "applied", "proposed_by", "upgraded_from", "facts", "reason")


def _on_route_profile(state: dict, ev: dict) -> None:
    """The last one wins: a mid-flight upgrade replaces the triage's profile."""
    state["route"] = {name: copy.deepcopy(ev.get(name)) for name in ROUTE_FIELDS}


def _on_effort_refused(state: dict, ev: dict) -> None:
    _step_row(state, ev["key"])["effort_refused"] = {"requested": ev.get("requested"), "reason": ev.get("reason")}


def _on_compacted(state: dict, ev: dict) -> None:
    _, row = _find_agent(state, ev)
    if row is not None:
        row["status"] = "compacting"


def _on_rolled_over(state: dict, ev: dict) -> None:
    _, row = _find_agent(state, ev)
    key = ev.get("key") or (row["step"] if row else None) or state["step"]
    if key:
        _step_row(state, key)["rollovers"] += 1


def _on_subagent_started(state: dict, ev: dict) -> None:
    _, row = _find_agent(state, ev)
    if row is not None:
        row["subagents"].append({
            "tool_use_id": ev.get("tool_use_id"), "subagent_type": ev.get("subagent_type"),
            "model": ev.get("model"), "description": ev.get("description"), "done": False, "ok": None,
            "background": ev.get("background"),  # True: started with `run_in_background` (None: an older engine's event)
        })


def _on_subagent_finished(state: dict, ev: dict) -> None:
    _, row = _find_agent(state, ev)
    if row is not None:
        for sub in row["subagents"]:
            if sub["tool_use_id"] == ev.get("tool_use_id"):
                sub["done"] = True
                sub["ok"] = ev.get("ok")  # False: the call came back as an error (None: an older engine's event)


def _on_context_window(state: dict, ev: dict) -> None:
    state["context_windows"][ev["model"]] = int(ev["tokens"])


def _on_usage_window(state: dict, ev: dict) -> None:
    """A usage window as the CLI last reported it (`utilization` 0..1). Other keys of the event (`sizer`) are ignored."""
    state["usage_window"][str(ev["window"])] = {
        "utilization": ev.get("utilization"), "resets_at": ev.get("resets_at"), "status": ev.get("status"),
        "at": ev.get("ts"),
    }


def _on_cli_setting_ignored(state: dict, ev: dict) -> None:
    if ev["setting"] not in state["cli_settings_ignored"]:
        state["cli_settings_ignored"].append(ev["setting"])


def _on_env_dropped(state: dict, ev: dict) -> None:
    """The inherited variables the run does not pass on: names only, never a value."""
    state["env_dropped"] = sorted({str(name) for name in ev.get("names") or []})


def _on_gate_result(state: dict, ev: dict) -> None:
    key = ev.get("key") or state["step"] or "run"
    state["gates"].setdefault(key, []).append({
        "gate": ev.get("gate"), "passed": ev.get("passed"), "skipped": ev.get("skipped", False), "log": ev.get("log"),
    })


def _on_review_result(state: dict, ev: dict) -> None:
    key = ev.get("key") or state["step"] or "run"
    state["reviews"][key] = {k: v for k, v in ev.items() if k not in ("seq", "ts", "type", "by", "key")}


def _on_review_skipped(state: dict, ev: dict) -> None:
    """A refine round's implementer changed nothing, so its task was not reviewed again; a later review replaces it."""
    state["reviews"][ev["key"]] = {"skipped": True, "reason": ev.get("reason"), "round": ev.get("round")}
    row = _step_row(state, ev["key"])
    row["status"] = "skipped"  # settled: the round leaves nothing pending
    row["skip_reason"] = ev.get("reason")


def _on_hitl_raised(state: dict, ev: dict) -> None:
    key = ev["key"]
    wait = {
        "kind": ev["kind"],
        "key": key,
        "phase": ev.get("phase") or _phase_of(key),
        "summary": ev.get("summary", ""),
        "questions": copy.deepcopy(ev.get("questions") or []),
        "since": ev.get("ts"),
    }
    if "times" in ev:  # a `blocked_repeat` stop that came back after a human's approval
        wait["times"] = ev["times"]
    for mark in ("promoted", "notified", "speculative"):  # a parked item made the wait once no task could run; a person was told; a stop of a speculated phase
        if mark in ev:
            wait[mark] = ev[mark]
    if state["status"] in TERMINAL:  # a stop raised after a cancel is logged, never a wait to act on
        return
    state.setdefault("parked", {}).pop(key, None)  # a parked stop that stops the run is now the wait
    if _approval_pending(state):
        state["waiting_queue"].append(wait)  # the approval stays; this one is shown once it ends
    else:
        state["waiting"] = wait
        state["status"] = "waiting_human"
    row = _step_row(state, key, ev.get("phase"))
    if row["status"] != "failed":  # a failed step (a guard violation) stays failed, so its restart checks the tree
        row["status"] = "waiting_human"


def _on_step_parked(state: dict, ev: dict) -> None:
    """A stop that later tasks need not wait for: kept in `parked` to approve, answer or retry, and the run goes on.

    The wait on the same key, which the stop raised first (or a promoted wait put back, `demoted`), is cleared. A
    failed step (a guard violation) stays failed, so its restart checks the tree."""
    key = ev["key"]
    item = {
        "kind": ev.get("kind") or "needs_human",
        "key": key,
        "phase": ev.get("phase") or _phase_of(key),
        "summary": ev.get("summary", ""),
        "questions": copy.deepcopy(ev.get("questions") or []),
        "since": ev.get("ts"),
    }
    for field in ("stop_class", "blast_radius", "shelf", "shelved"):
        if field in ev:
            item[field] = copy.deepcopy(ev[field])
    state.setdefault("parked", {})[key] = item
    waiting = state["waiting"]
    if waiting is not None and not _approval_pending(state) and waiting.get("key") == key:
        _clear_wait(state)
    row = _step_row(state, key, ev.get("phase"))
    if row["status"] != "failed":
        row["status"] = "parked"


def _on_step_reopened(state: dict, ev: dict) -> None:
    """A finished step goes back to its session (an implementer given review findings): resumable, not done."""
    _step_row(state, ev["key"])["status"] = "running"


def _clear_waits_under(state: dict, key: str) -> None:
    """Drop the waits on `key` or under it (current and queued), unless a phase approval is the current wait."""
    def under(wait: dict) -> bool:
        return wait.get("key") == key or str(wait.get("key") or "").startswith(f"{key}/")

    state["waiting_queue"] = [w for w in state["waiting_queue"] if not under(w)]
    waiting = state["waiting"]
    if waiting is not None and not _approval_pending(state) and under(waiting):
        _clear_wait(state)


def _on_wave_dissolved(state: dict, ev: dict) -> None:
    """A wave of parallel tasks is given up: its tasks run alone. A wait under the wave's key (its stopped
    implementer) is cleared, and the wave's implement row is skipped, not failed."""
    _clear_waits_under(state, ev["key"])
    row = _step_row(state, f"{ev['key']}/implement")
    row["status"] = "skipped"
    row["skip_reason"] = f"wave dissolved: {ev.get('reason')}"


def _on_review_wave_dropped(state: dict, ev: dict) -> None:
    """A wave's review is given up (its tasks are reviewed alone): the same as `_on_wave_dissolved`, for the
    wave's review row."""
    key = f"{ev['key']}/review"
    _clear_waits_under(state, key)
    row = _step_row(state, key)
    row["status"] = "skipped"
    row["skip_reason"] = f"wave review dropped: {ev.get('reason')}"


def _on_resolve_started(state: dict, ev: dict) -> None:
    """The runner takes a stop over: no human is needed while its session runs. The step stays as it stopped."""
    state["resolving"] = {"key": ev["key"], "kind": ev.get("stop_kind"), "session_key": ev.get("session_key"),
                          "attempt": ev.get("attempt"), "since": ev.get("ts")}
    if state["waiting"] is not None and state["waiting"].get("key") == ev["key"]:
        _clear_wait(state)


def _on_run_fact(state: dict, ev: dict) -> None:
    """A fact a resolver session found, kept for every later session. A repeat (any case) moves to the newest place."""
    text = str(ev["text"])
    kept = [f for f in state.get("facts") or [] if str(f.get("text")).casefold() != text.casefold()]
    state["facts"] = [*kept, {"text": text, "key": ev["key"]}][-RUN_FACTS_MAX:]


def _on_code_noted(state: dict, ev: dict) -> None:
    """A line of code map a step reported, kept for every later session. A repeat (any case) moves to the newest place."""
    text = str(ev["text"])
    kept = [c for c in state.get("code_map") or [] if str(c.get("text")).casefold() != text.casefold()]
    state["code_map"] = [*kept, {"text": text, "key": ev["key"]}][-CODE_MAP_MAX:]


def _on_resolve_finished(state: dict, ev: dict) -> None:
    state["resolving"] = None


def _on_ci_watching(state: dict, ev: dict) -> None:
    state["ci"] = {"url": ev.get("url"), "round": ev.get("round", 0), "since": ev.get("ts")}


def _on_pr_marked_ready(state: dict, ev: dict) -> None:
    state["ci"] = None


def _on_hitl_answered(state: dict, ev: dict) -> None:
    waiting = state["waiting"]
    parked = state.setdefault("parked", {})
    if ev.get("key") in parked and (waiting is None or waiting.get("key") != ev.get("key")):
        parked.pop(ev["key"])
        if ev["key"] in state["steps"]:
            state["steps"][ev["key"]]["status"] = "running"
        return
    if waiting is None or _approval_pending(state) or ev.get("key") not in (None, waiting.get("key")):
        return
    key = waiting.get("key")
    _clear_wait(state)
    if key in state["steps"]:
        state["steps"][key]["status"] = "running"


def _on_checkpoint_cleared(state: dict, ev: dict) -> None:
    """An inner checkpoint (or a repeat group past `max_rounds`) is settled: its row is done."""
    waiting = state["waiting"]
    if waiting is not None and not _approval_pending(state) and waiting.get("key") == ev["key"]:
        _clear_wait(state)
    row = state["steps"].get(ev["key"])
    if row is not None and row["status"] in ("waiting_human", "running"):
        row["status"] = "done"


def _on_hitl_approved(state: dict, ev: dict) -> None:
    """A human approved a stop the runner raised: the step goes back to resumable, never to done.

    A parked stop approved by its key leaves the parked list, and the run's own wait stays. A failed step (a guard
    violation) stays failed, so its restart checks the tree again.
    """
    waiting = state["waiting"]
    parked = state.setdefault("parked", {})
    if ev["key"] in parked and (waiting is None or waiting.get("key") != ev["key"]):
        parked.pop(ev["key"])  # a parked stop settled by its key: the run's own wait, if any, stays
        row = state["steps"].get(ev["key"])
        if row is not None and row["status"] in ("parked", "waiting_human"):
            row["status"] = "running"
        return
    if waiting is None or _approval_pending(state) or waiting.get("key") != ev["key"]:
        return
    _clear_wait(state)
    row = state["steps"].get(ev["key"])
    if row is not None and row["status"] == "waiting_human":
        row["status"] = "running"


def _on_handoff_written(state: dict, ev: dict) -> None:
    _phase_row(state, ev["phase"])["handoff"] = ev.get("path")


def _on_hitl_reminded(state: dict, ev: dict) -> None:
    waiting = state["waiting"]
    if waiting is not None:
        waiting["reminded"] = waiting.get("reminded", 0) + 1
        waiting["reminded_at"] = ev.get("at") or ev.get("ts")


def _on_hitl_notified(state: dict, ev: dict) -> None:
    """A person was told of the run's wait after it was raised quietly (a task stop that could not park): kept on the
    wait, so no later walk or runner tells them again."""
    waiting = state["waiting"]
    if waiting is not None and waiting.get("key") == ev["key"]:
        waiting["notified"] = True


def _on_phase_approval_raised(state: dict, ev: dict) -> None:
    if state["status"] in TERMINAL:  # an ended run raises nothing a human could approve
        return
    phase = ev["phase"]
    row = _phase_row(state, phase)
    row["status"] = "awaiting_approval"
    row["round"] = ev.get("round", row["round"])
    if ev.get("sha"):
        row["approval_sha"] = ev["sha"]
    state["waiting"] = {
        "kind": "phase_approval",
        "key": f"{phase}_approved",
        "phase": phase,
        "summary": ev.get("summary", ""),
        "questions": [],
        "since": ev.get("ts"),
    }
    state["refine"] = None
    stash = state.get("refine_stash")  # only ever set under speculation (`_on_phase_rejected`)
    if stash and stash.get("after") == phase:
        state.pop("refine_stash")
        if _refining_row(state, stash["refine"].get("phase")):
            state["refine"] = stash["refine"]  # the round a rejection of this phase cut in the phase walked behind it
    state["phase"] = phase
    state["status"] = "awaiting_approval"


def _on_phase_approved(state: dict, ev: dict) -> None:
    phase = ev["phase"]
    row = _phase_row(state, phase)
    row["status"] = "approved"
    if (state.get("refine") or {}).get("phase") == phase:  # the round is over: the next phase must not inherit it
        state["refine"] = None
        if state["status"] == "refining":
            state["status"] = "running"
    if ev.get("sha"):
        row["approval_sha"] = ev["sha"]
    waiting = state["waiting"]
    if waiting is not None and waiting.get("kind") == "phase_approval" and waiting.get("phase") == phase:
        _clear_wait(state)
        if state["status"] == "running" and state.get("refine"):  # another phase's round, run while this one waited
            state["status"] = "refining"


def _behind_speculation(state: dict, phase: str) -> bool:
    """Whether a phase was walked behind `phase`'s approval and is not done with it: the speculation record names
    `phase`, or a stop the walk queued quietly is still the wait or queued."""
    if (state.get("speculation") or {}).get("after") == phase:
        return True
    return any(w.get("speculative") and w.get("phase") != phase for w in [state["waiting"] or {}, *state["waiting_queue"]])


def _on_phase_ready(state: dict, ev: dict) -> None:
    """An approved phase rests until a human continues the run or marks it done. A queued wait comes first.

    Under speculation, a hold on the approval a phase was walked behind is never dropped: it is recorded whatever the
    status (the walked phase's round made it `refining`, or the walk's queued stop became the wait), so the walk
    yields to it (`speculation.cut`) and the run never goes on past the held phase. The status is `ready` at once when
    nothing waits, else once the last wait clears (`_clear_wait`)."""
    if state["status"] in ("running", "paused") and state["waiting"] is None:
        state["ready"] = {"phase": ev["phase"], "since": ev.get("ts")}
        state["status"] = "ready"
    elif state["status"] not in TERMINAL and _behind_speculation(state, ev["phase"]):
        state["ready"] = {"phase": ev["phase"], "since": ev.get("ts"), "speculative": True}
        if state["waiting"] is None:
            state["status"] = "ready"


def _on_run_marked_done(state: dict, ev: dict) -> None:
    state["ready"] = None
    state["marked_done"] = {"phase": ev["phase"], "by": ev.get("by"), "at": ev.get("ts")}
    if state["status"] == "ready":
        state["status"] = "running"


def _on_run_archived(state: dict, ev: dict) -> None:
    state["archived"] = {"by": ev.get("by"), "at": ev.get("ts")}


def _in_scope(key: str, scope: str | None, keys: list[str] | None = None) -> bool:
    """Whether a rejection scoped to one plan task (`<phase>/<step>/task-<n>`) sends `key` back: the task's
    own steps, and the steps that hold it (the `foreach` step), or the phase would never reach the task.
    With `keys` (a reconcile round's), only those steps and the steps under them go back."""
    if keys is not None and not any(key == k or key.startswith(k + "/") for k in keys):
        return False
    return not scope or key == scope or key.startswith(scope + "/") or scope.startswith(key + "/")


RECONCILE_KIND = "reconcile"  # `doubt.RECONCILE`: a round the runner opens on a doubt review's findings, not a human's


def _refining_row(state: dict, phase: object) -> bool:
    return isinstance(phase, str) and (state["phases"].get(phase) or {}).get("status") == "refining"


def _on_phase_rejected(state: dict, ev: dict) -> None:
    phase, round_ = ev["phase"], ev.get("round", 1)
    prior = state.get("refine") or {}
    if (_approval_pending(state) and state["waiting"].get("phase") == phase and prior.get("phase") != phase
            and _refining_row(state, prior.get("phase"))):
        # The waiting phase is sent back while the phase walked behind it (speculation) is in a round of its own: keep
        # that round, so the waiting phase's next raise gives it back (`_on_phase_approval_raised`) and it goes on as one.
        state["refine_stash"] = {"after": phase, "refine": copy.deepcopy(prior)}
    scope = ev.get("scope") or None
    keys = ev.get("keys") if isinstance(ev.get("keys"), list) else None
    row = _phase_row(state, phase)
    row["status"] = "refining"
    if ev.get("kind") == RECONCILE_KIND:  # the human's round number stays: the page's "refine round N" is theirs
        row["reconcile_round"] = ev.get("reconcile_round")
    else:
        row["round"] = round_
    for key, step in state["steps"].items():
        if step["phase"] == phase and _in_scope(key, scope, keys):  # a step outside the scope keeps its status
            step["status"] = "refine_pending"
            step["round"] = round_
    state["refine"] = {"phase": phase, "round": round_, "note": ev.get("note", "")}
    if scope:
        state["refine"]["scope"] = scope
    for name in ("kind", "keys", "digests"):  # a human's round keeps exactly {phase, round, note}
        if name in ev:
            state["refine"][name] = ev[name]
    state["resolving"] = None
    parked = state.setdefault("parked", {})
    for key in [k for k, item in parked.items() if item.get("phase") == phase and _in_scope(k, scope, keys)]:
        parked.pop(key)  # its step runs again in the refine round
    if _approval_pending(state) and state["waiting"].get("phase") != phase:
        return  # another phase's round (speculation): the pending approval, its queue and its status stay
    # the waiting phase itself is sent back: the stops queued from its speculated phase no longer apply
    for wait in [w for w in state["waiting_queue"] if w.get("speculative")]:
        step = state["steps"].get(wait.get("key"))
        if step is not None and step["status"] == "waiting_human":  # it starts afresh, not as a session resumed on a question nobody answered
            step["status"] = "pending"
            step["session_id"] = None
    state["waiting_queue"] = [w for w in state["waiting_queue"] if not w.get("speculative")]
    state["waiting"] = None
    if state["status"] not in TERMINAL:
        state["status"] = "refining"
    if state["waiting_queue"]:
        _clear_wait(state)


def _on_run_paused(state: dict, ev: dict) -> None:
    if state["status"] not in TERMINAL:
        state["status"] = "paused"


def _on_run_resumed(state: dict, ev: dict) -> None:
    if state["status"] in TERMINAL:
        return
    if state["ready"] is not None:  # a human chose to go on, so a `--to` set at the start no longer ends the run
        state["ready"] = None
        state["options"]["to_phase"] = None
    waiting = state["waiting"]
    if waiting is not None:
        state["status"] = "awaiting_approval" if waiting.get("kind") == "phase_approval" else "waiting_human"
    else:
        state["status"] = "refining" if state["refine"] else "running"


def _on_run_finished(state: dict, ev: dict) -> None:
    state["status"] = "done"


def _on_run_failed(state: dict, ev: dict) -> None:
    state["status"] = "failed"
    state["failure"] = ev.get("reason")


def _on_run_cancelled(state: dict, ev: dict) -> None:
    state["status"] = "cancelled"


def _on_runner_error(state: dict, ev: dict) -> None:
    """The runner stopped on an unexpected exception: `error` until `run_resumed` (a resume) goes on."""
    if state["status"] not in TERMINAL:
        state["status"] = "error"
        state["failure"] = f"{ev.get('exception') or 'Error'}: {ev['error']}"


def _on_footer_mismatch(state: dict, ev: dict) -> None:
    phase = ev.get("phase") or state["phase"]
    state["footer_mismatches"].append({"phase": phase, "expected": ev.get("expected"), "got": ev.get("got")})


def _on_pr_opened(state: dict, ev: dict) -> None:
    state["pr_url"] = ev.get("url") or state["pr_url"]


def _on_backlog_updated(state: dict, ev: dict) -> None:
    state["backlog_sha"] = ev["sha"]


def _on_item_claimed(state: dict, ev: dict) -> None:
    """A claim made (or made again after a `claim_lost` was approved) replaces the whole row and clears `lost`."""
    state["claim"] = {"item": ev["item"], "ref": ev.get("ref"), "sha": ev["sha"], "phase": ev.get("phase"),
                      "status": ev.get("status"), "released": False, "lost": False}


def _claim_row(state: dict) -> dict | None:
    """The run's claim, None for a run that holds none (or that was written before claims: it has no `claim` key)."""
    row = state.get("claim")
    return row if isinstance(row, dict) else None


def _on_claim_advanced(state: dict, ev: dict) -> None:
    row = _claim_row(state)
    if row is not None:
        row.update(sha=ev["sha"], phase=ev.get("phase"), status=ev.get("status"))


def _on_claim_released(state: dict, ev: dict) -> None:
    row = _claim_row(state)
    if row is not None:
        row["released"] = True


def _on_claim_lost(state: dict, ev: dict) -> None:
    row = _claim_row(state)
    if row is not None:
        row["lost"] = True


def _on_permission_denied(state: dict, ev: dict) -> None:
    row = _step_row(state, ev["key"])
    row["permission_denials"] = row.get("permission_denials", 0) + 1


def _on_progress_not_updated(state: dict, ev: dict) -> None:
    _step_row(state, ev["key"])["progress_not_updated"] = True


def _on_checkpoint_reused(state: dict, ev: dict) -> None:
    _step_row(state, ev["key"])["checkpoint_reused"] = True


HANDLERS: dict[str, Callable[[dict, dict], None]] = {
    "run_started": _on_run_started,
    "run_updated": _on_run_updated,
    "run_options": _on_run_options,
    "run_paused": _on_run_paused,
    "run_resumed": _on_run_resumed,
    "run_marked_done": _on_run_marked_done,
    "run_archived": _on_run_archived,
    "run_finished": _on_run_finished,
    "run_failed": _on_run_failed,
    "run_cancelled": _on_run_cancelled,
    "runner_error": _on_runner_error,
    "pr_opened": _on_pr_opened,
    "backlog_updated": _on_backlog_updated,
    "item_claimed": _on_item_claimed,
    "claim_advanced": _on_claim_advanced,
    "claim_released": _on_claim_released,
    "claim_lost": _on_claim_lost,
    "phase_started": _on_phase_started,
    "phase_skipped": _on_phase_skipped,
    "phase_approval_raised": _on_phase_approval_raised,
    "phase_approved": _on_phase_approved,
    "phase_ready": _on_phase_ready,
    "phase_rejected": _on_phase_rejected,
    "speculation_started": _on_speculation_started,
    "speculation_ended": _on_speculation_ended,
    "step_started": _on_step_started,
    "step_finished": _on_step_finished,
    "step_skipped": _on_step_skipped,
    "step_unchanged": _on_step_unchanged,
    "step_retry": _on_step_retry,
    "agent_started": _on_agent_started,
    "agent_session": _on_agent_session,
    "agent_turn": _on_agent_turn,
    "agent_exited": _on_agent_exited,
    "effort_changed": _on_effort_changed,
    "effort_refused": _on_effort_refused,
    "effort_decided": _on_effort_decided,
    "effort_escalated": _on_effort_escalated,
    "sizer_started": _on_sizer_started,
    "plan_sized": _on_plan_sized,
    "sizer_finished": _on_sizer_finished,
    "route_profile": _on_route_profile,
    "compacted": _on_compacted,
    "rolled_over": _on_rolled_over,
    "subagent_started": _on_subagent_started,
    "subagent_finished": _on_subagent_finished,
    "context_window": _on_context_window,
    "usage_window": _on_usage_window,
    "cli_setting_ignored": _on_cli_setting_ignored,
    "env_dropped": _on_env_dropped,
    "gate_result": _on_gate_result,
    "review_result": _on_review_result,
    "review_skipped": _on_review_skipped,
    "hitl_raised": _on_hitl_raised,
    "run_held": _on_run_held,
    "run_hold_ended": _on_run_hold_ended,
    "usage_governed": _on_usage_governed,
    "step_parked": _on_step_parked,
    "step_reopened": _on_step_reopened,
    "wave_dissolved": _on_wave_dissolved,
    "review_wave_dropped": _on_review_wave_dropped,
    "resolve_started": _on_resolve_started,
    "resolve_finished": _on_resolve_finished,
    "run_fact": _on_run_fact,
    "code_noted": _on_code_noted,
    "ci_watching": _on_ci_watching,
    "pr_marked_ready": _on_pr_marked_ready,
    "hitl_answered": _on_hitl_answered,
    "hitl_reminded": _on_hitl_reminded,
    "hitl_notified": _on_hitl_notified,
    "checkpoint_cleared": _on_checkpoint_cleared,
    "hitl_approved": _on_hitl_approved,
    "handoff_written": _on_handoff_written,
    "footer_mismatch": _on_footer_mismatch,
    "permission_denied": _on_permission_denied,
    "progress_not_updated": _on_progress_not_updated,
    "checkpoint_reused": _on_checkpoint_reused,
    "slot_acquired": _on_slot_acquired,
}


# Fields an event type cannot do without; an inner tuple means "any one of these".
REQUIRED: dict[str, tuple[Any, ...]] = {
    "phase_started": ("phase",),
    "phase_skipped": ("phase",),
    "phase_approval_raised": ("phase",),
    "phase_approved": ("phase",),
    "phase_ready": ("phase",),
    "run_marked_done": ("phase",),
    "phase_rejected": ("phase",),
    "speculation_started": ("phase", "after", "basis"),
    "speculation_ended": ("phase", "outcome"),
    "would_auto_approve": ("phase", "verdict"),
    "step_started": ("key",),
    "step_finished": ("key",),
    "step_skipped": (("key", "step"),),
    "step_unchanged": (("key", "step"),),
    "step_retry": ("key",),
    "effort_refused": ("key",),
    "effort_decided": ("key", "level"),
    "effort_escalated": ("key", "to"),
    "sizer_started": ("key",),
    "plan_sized": ("key",),
    "sizer_finished": ("key",),
    "route_profile": ("profile",),
    "permission_denied": ("key",),
    "progress_not_updated": ("key",),
    "checkpoint_reused": ("key",),
    "checkpoint_cleared": ("key",),
    "hitl_approved": ("key",),
    "handoff_written": ("phase",),
    "hitl_raised": ("kind", "key"),
    "module_added": ("module", "sha"),
    "backlog_updated": ("item", "change", "sha"),
    "item_claimed": ("item", "ref", "sha", "phase", "status"),
    "claim_advanced": ("item", "sha", "phase", "status"),
    "claim_released": ("item", "reason"),
    "claim_lost": ("item", "why"),
    "claim_sync_failed": ("item", "action", "error"),
    "claim_release_failed": ("item", "error"),
    "run_held": ("key", "reason", "until"),
    "run_hold_ended": ("key",),
    "usage_governed": ("key", "level"),
    "step_parked": ("key",),
    "unit_unparked": ("key",),
    "hitl_notified": ("key",),
    "constraint_approved": ("key", "sha"),
    "step_reopened": ("key",),
    "wave_started": ("key", "wave"),
    "parallel_demoted": ("key",),
    "wave_attributed": ("key",),
    "scope_violation": ("key", "path"),
    "wave_dissolved": ("key", "wave"),
    "review_wave_dropped": ("key",),
    "fix_rebased": ("key", "tree"),
    "wave_finished": ("key",),
    "parallel_candidates": ("key",),
    "resolve_started": ("key",),
    "resolve_finished": ("key",),
    "run_fact": ("text", "key"),
    "code_noted": ("text", "key"),
    "agent_started": (("session_id", "pid"),),
    "agent_session": ("session_id",),
    "brief_replaced": ("key", "reason"),
    "session_capped": ("key", "cap"),
    "read_only_skipped": ("key", "reason"),
    "context_window": ("model", "tokens"),
    "usage_window": ("window",),
    "cli_setting_ignored": ("setting",),
    "runner_error": ("error",),
    "preflight_checked": ("ok",),
    "watchdog_fired": ("key",),
    "stop_classified": ("key", "stop_class"),
    "environment_waiting": ("key", "digest"),
    "environment_rechecked": ("key", "ok"),
    "gate_cache_hit": ("key", "gate", "input_hash"),
    "early_pr_failed": ("phase",),
    "pr_updated": ("url", "key"),
    "slot_acquired": ("slot",),
}
TEXT_FIELDS = ("key", "step", "phase", "model", "kind")
HANDLER_ERRORS = (KeyError, TypeError, AttributeError, ValueError, IndexError)


def _validate(event: dict) -> None:
    """Raise `EventError` when a required field is missing or has the wrong type."""
    etype = event.get("type")
    for need in REQUIRED.get(etype, ()):
        names = need if isinstance(need, tuple) else (need,)
        present = [n for n in names if event.get(n) not in (None, "")]
        if not present:
            wanted = " or ".join(repr(n) for n in names)
            raise EventError(str(etype), f"missing required field {wanted}")
        for name in present:
            if name in TEXT_FIELDS and not isinstance(event[name], str):
                raise EventError(str(etype), f"field {name!r} must be text, got {type(event[name]).__name__}")


def _apply_strict(state: dict, event: dict) -> None:
    """Apply `event` in place; a bad event raises `EventError` (the state may be half-changed, so use a copy)."""
    _validate(event)
    handler = HANDLERS.get(event.get("type"))
    try:
        if handler is not None:
            handler(state, event)
    except HANDLER_ERRORS as exc:
        raise EventError(str(event.get("type")), f"cannot apply it ({type(exc).__name__}: {exc})") from exc
    state["last_seq"] = event.get("seq", state["last_seq"])
    state["updated_at"] = event.get("ts", state["updated_at"])


def _apply(state: dict, event: dict) -> None:
    """Apply `event` in place, tolerating a malformed one: it is skipped and listed in `malformed_events`.

    Required fields are checked before anything changes, so a skipped event leaves no half-applied state.
    """
    try:
        _apply_strict(state, event)
    except EventError as exc:
        state["malformed_events"].append({"seq": event.get("seq"), "type": event.get("type"), "error": exc.message})
        state["last_seq"] = event.get("seq", state["last_seq"])
        state["updated_at"] = event.get("ts", state["updated_at"])


def reduce(state: dict, event: dict) -> dict:
    """The state after `event`. Pure: `state` is not modified. Unknown event types change nothing."""
    out = copy.deepcopy(state)
    _apply(out, event)
    return out


def replay(events: Iterable[dict]) -> dict:
    """The state reached by applying `events` from the start."""
    state = initial_state()
    for event in events:
        _apply(state, event)
    return state


# --------------------------------------------------------------------------- runs


def runs_root(repo: Path) -> Path:
    """Where runs live: `runs/` in the state dir of the main checkout holding `repo` (`pack.state_dir`)."""
    return pack.state_dir(repo) / "runs"


def workspaces_root(repo: Path) -> Path:
    """Where the agent workspaces live: `workspaces/` in the state dir, beside the runs root, never in it."""
    return pack.state_dir(repo) / "workspaces"


def slugify(text: str, limit: int = SLUG_MAX) -> str:
    """ASCII slug (D14): NFKD, `đ` to `d`, non-alphanumerics to `-`, at most `limit` characters."""
    text = text.replace("đ", "d").replace("Đ", "D")
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")[:limit].strip("-")


def safe_name(key: str) -> str:
    """A step key as a file name: `/` becomes `__`."""
    return key.replace("/", "__")


def _slug_for(pipeline: str, params: dict) -> str:
    for name in SLUG_PARAMS:
        value = params.get(name)
        if value not in (None, ""):
            text = Path(str(value)).stem if name == "log" else str(value)
            return slugify(text) or slugify(pipeline)
    return slugify(pipeline)


class Run:
    """One run on disk. `emit` is the only way its state changes."""

    def __init__(self, repo: Path, run_id: str) -> None:
        self.repo = Path(repo).resolve()
        self.id = run_id
        self.dir = runs_root(self.repo) / run_id
        self.events = EventLog(self.dir / "events.jsonl")
        self.state_path = self.dir / "state.json"
        self.state: dict = initial_state()

    # -- construction

    @classmethod
    def create(cls, cfg: Config, repo: Path, pipeline: str, params: dict, *, engine_pipeline: bool = False) -> Run:
        """A new run of `pipeline`: a lane of the pack's, or with `engine_pipeline` one of `ENGINE_PIPELINES` (the
        retro), which no `lanes:` lists, so `agentflow run <it>` still says it is no lane."""
        if engine_pipeline:
            if pipeline not in ENGINE_PIPELINES:
                raise ValueError(f"{pipeline!r} is not an engine pipeline; known: {', '.join(ENGINE_PIPELINES)}")
        else:
            check_lane(cfg, pipeline)
        root = runs_root(repo)
        root.mkdir(parents=True, exist_ok=True)
        base = f"{datetime.now():%Y%m%d-%H%M}-{pipeline}-{_slug_for(pipeline, params)}"
        run_id, n = base, 1
        while True:
            try:
                (root / run_id).mkdir()
                break
            except FileExistsError:
                n += 1
                run_id = f"{base}-{n}"
        run = cls(repo, run_id)
        run.agent_dir.mkdir(parents=True, exist_ok=True)
        run.emit("run_started", run_id=run_id, pipeline=pipeline, params=params,
                 budget_usd=cfg.defaults.run_budget_hard_usd, soft_budget_usd=cfg.defaults.run_budget_usd)
        return run

    @classmethod
    def load(cls, repo: Path, run_id: str) -> Run:
        run = cls(repo, run_id)
        if not run.events.path.exists():
            raise RunNotFound(f"no run {run_id!r} in {runs_root(repo)}")
        run.refresh()
        return run

    # -- state

    def refresh(self) -> dict:
        """Bring `state` up to the log, from the events alone: `state.json` is an output and is never read
        back (an agent or anyone else could have written it). A first refresh replays the whole log (5000
        events: about 30 ms); later ones apply only the events this object has not seen. A `state.json` that
        differs from the replay is rewritten."""
        with self.events.locked():
            last = self.events.last_seq()
            if self.state["last_seq"] == 0 or last < self.state["last_seq"]:
                self.state = replay(self.events.read())
                behind = True
            else:
                missed = self.events.read(after_seq=self.state["last_seq"]) if last > self.state["last_seq"] else []
                for event in missed:
                    _apply(self.state, event)
                behind = bool(missed)
            if behind or not self._state_file_matches():
                self._write_state()
            # a new object each time, as when it was read from disk: callers hold rows from before a refresh
            self.state = copy.deepcopy(self.state)
        return self.state

    def _state_file_matches(self) -> bool:
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8")) == self.state
        except (OSError, ValueError):
            return False

    def _write_state(self) -> None:
        fd, tmp = tempfile.mkstemp(dir=self.dir, prefix="state.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(self.state, handle, ensure_ascii=False, indent=2)
            os.replace(tmp, self.state_path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    @contextmanager
    def locked(self) -> Iterator[None]:
        """Hold the run's log lock with `state` freshly read, for check-then-emit actions."""
        with self.events.locked():
            self.refresh()
            yield

    def _catch_up(self) -> None:
        """Apply events another writer appended since this object last looked."""
        if self.events.last_seq() != self.state["last_seq"]:
            for missed in self.events.read(after_seq=self.state["last_seq"]):
                _apply(self.state, missed)

    def emit(self, type: str, by: str = "runner", **data: Any) -> dict:
        """Append an event, apply it, and write `state.json`. Returns the stored event.

        The event is tried on a copy of the state first; if the reducer rejects it, `EventError`
        is raised and nothing is written, so a bad event can never reach the log.
        """
        self.events.check_data(data)
        with self.events.locked():
            self._catch_up()
            trial = nfc({"seq": self.state["last_seq"] + 1, "ts": now_iso(), "type": type, "by": by, **data})
            _apply_strict(copy.deepcopy(self.state), json.loads(json.dumps(trial, ensure_ascii=False, default=str)))
            event = self.events.append(type, by, **data)
            _apply(self.state, event)
            self._write_state()
        return event

    def lock(self) -> RunLock:
        return RunLock(self.dir)

    # -- paths

    def result_path(self, key: str) -> Path:
        return self.dir / "results" / f"{safe_name(key)}.json"

    def inbox_path(self, key: str) -> Path:
        return self.dir / "inbox" / f"{safe_name(key)}.md"

    def questions_path(self) -> Path:
        return self.dir / "questions.md"

    @property
    def agent_dir(self) -> Path:
        """The run's agent workspace (`workspace.py`): the only directory its agents get with `--add-dir`."""
        return workspaces_root(self.repo) / self.id

    def agent_result_path(self, key: str) -> Path:
        """Where the agent writes the step's result (`AGENTFLOW_RESULT`); the runner keeps a checked copy
        at `result_path`."""
        return self.agent_dir / "out" / f"{safe_name(key)}.json"

    def progress_path(self) -> Path:
        return self.agent_dir / "progress.md"

    def handoff_path(self) -> Path:
        return self.dir / "handoff.md"

    def transcript_path(self, key: str, n: int) -> Path:
        return self.dir / "transcripts" / f"{safe_name(key)}.{n}.jsonl"

    def gate_log_dir(self, key: str) -> Path:
        return self.dir / "gates" / safe_name(key)

    def refine_note_path(self, phase: str, round: int | str) -> Path:
        return self.dir / "refine" / f"{phase}-{round}.md"


def list_runs(repo: Path) -> list[Run]:
    """Every run under the runs root, oldest first (ids start with the date)."""
    root = runs_root(repo)
    if not root.is_dir():
        return []
    return [Run.load(repo, p.name) for p in sorted(root.iterdir()) if (p / "events.jsonl").is_file()]


def find_run(repo: Path, text: str) -> Run:
    """The run whose id is `text`, or the only run whose id contains it."""
    root = runs_root(repo)
    if (root / text / "events.jsonl").is_file():
        return Run.load(repo, text)
    names = [p.name for p in root.iterdir() if text in p.name and (p / "events.jsonl").is_file()] if root.is_dir() else []
    if len(names) == 1:
        return Run.load(repo, names[0])
    if names:
        raise RunNotFound(f"{text!r} matches {len(names)} runs: {', '.join(sorted(names))}")
    raise RunNotFound(f"no run matches {text!r}")


def display_status(run: Run) -> str:
    """The run's status for people: `stalled` when a runner should be working but none holds the lock
    (its PID is gone, or it exited without recording why), `held` when a live one is waiting out a limit
    or an outage."""
    status = run.state["status"]
    if status in ("running", "refining"):
        if lock_state(run.dir) != "live":
            return "stalled"
        if run.state.get("hold"):
            return "held"
    return status


__all__ = [
    "Run", "RunNotFound", "cost_added", "display_status", "find_run", "initial_state", "list_runs",
    "reduce", "replay", "runs_root", "safe_name", "slugify",
]
