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
from .config import KNOWN_EFFORT_LEVELS, Config, check_lane
from .events import EventError, EventLog, RunLock, lock_state, nfc, now_iso

TERMINAL = ("done", "failed", "cancelled")
SLUG_PARAMS = ("slug", "module", "issue", "topic", "task", "log")  # an explicit slug (an issue's) wins
SLUG_MAX = 40
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
        "budget_usd": None,
        "context_windows": {},
        "env_dropped": [],
        "facts": [],
        "gates": {},
        "reviews": {},
        "effort_changes": [],
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
    """Drop the current wait and promote the next queued one, if any."""
    state["waiting"] = None
    if state["waiting_queue"]:
        state["waiting"] = state["waiting_queue"].pop(0)
        if state["status"] not in TERMINAL:
            state["status"] = "waiting_human"
    elif state["status"] in ("waiting_human", "awaiting_approval"):
        state["status"] = "running"


def _approval_pending(state: dict) -> bool:
    """A phase approval is the current wait: only `phase_approved` and `phase_rejected` may end it."""
    return state["waiting"] is not None and state["waiting"].get("kind") == "phase_approval"


def _on_run_started(state: dict, ev: dict) -> None:
    state["run_id"] = ev.get("run_id", state["run_id"])
    state["pipeline"] = ev.get("pipeline", state["pipeline"])
    state["params"] = copy.deepcopy(ev.get("params") or {})
    for field in ("branch", "worktree", "base_sha"):
        if ev.get(field) is not None:
            state[field] = ev[field]
    if ev.get("budget_usd") is not None:
        state["budget_usd"] = ev["budget_usd"]
    state["status"] = "running"


def _on_run_options(state: dict, ev: dict) -> None:
    state["options"] = {k: copy.deepcopy(v) for k, v in ev.items() if k not in ("seq", "ts", "type", "by")}


def _on_run_updated(state: dict, ev: dict) -> None:
    for field in ("branch", "worktree", "base_sha", "pr_url", "budget_usd"):
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


def _on_agent_started(state: dict, ev: dict) -> None:
    key = ev.get("step") or ev.get("key") or state["step"]
    session, pid = ev.get("session_id"), ev.get("pid")
    name = str(session) if session else str(pid)
    sizing = bool(ev.get("sizing"))
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
        "status": "sizing" if sizing else "running",
        "context_pct": 0.0,
        "spend": 0.0,
        "started_at": ev.get("ts"),
        "last_activity": ev.get("ts"),
        "last_tool": None,
        "subagents": [],
    }
    if key:
        row = _step_row(state, key)
        row["attempts"] += 1
        row["agent"] = ev.get("agent") or row["agent"]
        row["model"] = ev.get("model") or row["model"]
        row["effort"] = ev.get("effort") or row["effort"]
        if session:
            row["session_id"] = session
        if not sizing:
            row["sized"] = True


def _on_run_held(state: dict, ev: dict) -> None:
    state["hold"] = {"key": ev["key"], "reason": ev["reason"], "until": ev["until"]}


def _on_run_hold_ended(state: dict, ev: dict) -> None:
    state["hold"] = None


def _on_agent_session(state: dict, ev: dict) -> None:
    session = ev.get("session_id")
    name, row = _find_agent(state, {"pid": ev.get("pid"), "key": ev.get("key")})
    if row is not None and session:
        row["session_id"] = session
        row["model"] = ev.get("model") or row["model"]
        state["agents"][str(session)] = state["agents"].pop(name)
    key = ev.get("key") or (row["step"] if row else None)
    if key and session:
        step = _step_row(state, key)
        step["session_id"] = session
        step["model"] = ev.get("model") or step["model"]


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
    if key:
        step = _step_row(state, key)
        step["effort"] = new
        step["effort_changed"] = direction
    state["effort_changes"].append({"key": key, "from": ev.get("from"), "to": new, "reason": ev.get("reason")})


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
            "model": ev.get("model"), "done": False,
        })


def _on_subagent_finished(state: dict, ev: dict) -> None:
    _, row = _find_agent(state, ev)
    if row is not None:
        for sub in row["subagents"]:
            if sub["tool_use_id"] == ev.get("tool_use_id"):
                sub["done"] = True


def _on_context_window(state: dict, ev: dict) -> None:
    state["context_windows"][ev["model"]] = int(ev["tokens"])


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
    if state["status"] in TERMINAL:  # a stop raised after a cancel is logged, never a wait to act on
        return
    state.setdefault("parked", {}).pop(key, None)  # a parked question that stops the run is now the wait
    if _approval_pending(state):
        state["waiting_queue"].append(wait)  # the approval stays; this one is shown once it ends
    else:
        state["waiting"] = wait
        state["status"] = "waiting_human"
    _step_row(state, key, ev.get("phase"))["status"] = "waiting_human"


def _on_step_parked(state: dict, ev: dict) -> None:
    """A question that later tasks need not wait for: kept answerable in `parked`, the run goes on."""
    key = ev["key"]
    state.setdefault("parked", {})[key] = {
        "kind": ev.get("kind") or "needs_human",
        "key": key,
        "phase": ev.get("phase") or _phase_of(key),
        "summary": ev.get("summary", ""),
        "questions": copy.deepcopy(ev.get("questions") or []),
        "since": ev.get("ts"),
    }
    _step_row(state, key, ev.get("phase"))["status"] = "parked"


def _on_step_reopened(state: dict, ev: dict) -> None:
    """A finished step goes back to its session (an implementer given review findings): resumable, not done."""
    _step_row(state, ev["key"])["status"] = "running"


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

    A failed step (a guard violation) stays failed, so its restart checks the tree again.
    """
    waiting = state["waiting"]
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
    state["phase"] = phase
    state["status"] = "awaiting_approval"


def _on_phase_approved(state: dict, ev: dict) -> None:
    phase = ev["phase"]
    row = _phase_row(state, phase)
    row["status"] = "approved"
    if ev.get("sha"):
        row["approval_sha"] = ev["sha"]
    waiting = state["waiting"]
    if waiting is not None and waiting.get("kind") == "phase_approval" and waiting.get("phase") == phase:
        _clear_wait(state)


def _on_phase_ready(state: dict, ev: dict) -> None:
    """An approved phase rests until a human continues the run or marks it done. A queued wait comes first."""
    if state["status"] in ("running", "paused") and state["waiting"] is None:
        state["ready"] = {"phase": ev["phase"], "since": ev.get("ts")}
        state["status"] = "ready"


def _on_run_marked_done(state: dict, ev: dict) -> None:
    state["ready"] = None
    state["marked_done"] = {"phase": ev["phase"], "by": ev.get("by"), "at": ev.get("ts")}
    if state["status"] == "ready":
        state["status"] = "running"


def _on_run_archived(state: dict, ev: dict) -> None:
    state["archived"] = {"by": ev.get("by"), "at": ev.get("ts")}


def _in_scope(key: str, scope: str | None) -> bool:
    """Whether a rejection scoped to one plan task (`<phase>/<step>/task-<n>`) sends `key` back: the task's
    own steps, and the steps that hold it (the `foreach` step), or the phase would never reach the task."""
    return not scope or key == scope or key.startswith(scope + "/") or scope.startswith(key + "/")


def _on_phase_rejected(state: dict, ev: dict) -> None:
    phase, round_ = ev["phase"], ev.get("round", 1)
    scope = ev.get("scope") or None
    row = _phase_row(state, phase)
    row["status"] = "refining"
    row["round"] = round_
    for key, step in state["steps"].items():
        if step["phase"] == phase and _in_scope(key, scope):  # a step outside the scope keeps its status
            step["status"] = "refine_pending"
            step["round"] = round_
    state["refine"] = {"phase": phase, "round": round_, "note": ev.get("note", "")}
    if scope:
        state["refine"]["scope"] = scope
    state["resolving"] = None
    parked = state.setdefault("parked", {})
    for key in [k for k, item in parked.items() if item.get("phase") == phase and _in_scope(k, scope)]:
        parked.pop(key)  # its step runs again in the refine round
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
    "phase_started": _on_phase_started,
    "phase_skipped": _on_phase_skipped,
    "phase_approval_raised": _on_phase_approval_raised,
    "phase_approved": _on_phase_approved,
    "phase_ready": _on_phase_ready,
    "phase_rejected": _on_phase_rejected,
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
    "compacted": _on_compacted,
    "rolled_over": _on_rolled_over,
    "subagent_started": _on_subagent_started,
    "subagent_finished": _on_subagent_finished,
    "context_window": _on_context_window,
    "env_dropped": _on_env_dropped,
    "gate_result": _on_gate_result,
    "review_result": _on_review_result,
    "review_skipped": _on_review_skipped,
    "hitl_raised": _on_hitl_raised,
    "run_held": _on_run_held,
    "run_hold_ended": _on_run_hold_ended,
    "step_parked": _on_step_parked,
    "step_reopened": _on_step_reopened,
    "resolve_started": _on_resolve_started,
    "resolve_finished": _on_resolve_finished,
    "run_fact": _on_run_fact,
    "ci_watching": _on_ci_watching,
    "pr_marked_ready": _on_pr_marked_ready,
    "hitl_answered": _on_hitl_answered,
    "hitl_reminded": _on_hitl_reminded,
    "checkpoint_cleared": _on_checkpoint_cleared,
    "hitl_approved": _on_hitl_approved,
    "handoff_written": _on_handoff_written,
    "footer_mismatch": _on_footer_mismatch,
    "permission_denied": _on_permission_denied,
    "progress_not_updated": _on_progress_not_updated,
    "checkpoint_reused": _on_checkpoint_reused,
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
    "step_started": ("key",),
    "step_finished": ("key",),
    "step_skipped": (("key", "step"),),
    "step_unchanged": (("key", "step"),),
    "step_retry": ("key",),
    "effort_refused": ("key",),
    "permission_denied": ("key",),
    "progress_not_updated": ("key",),
    "checkpoint_reused": ("key",),
    "checkpoint_cleared": ("key",),
    "hitl_approved": ("key",),
    "handoff_written": ("phase",),
    "hitl_raised": ("kind", "key"),
    "module_added": ("module", "sha"),
    "run_held": ("key", "reason", "until"),
    "run_hold_ended": ("key",),
    "step_parked": ("key",),
    "step_reopened": ("key",),
    "resolve_started": ("key",),
    "resolve_finished": ("key",),
    "run_fact": ("text", "key"),
    "agent_started": (("session_id", "pid"),),
    "agent_session": ("session_id",),
    "context_window": ("model", "tokens"),
    "runner_error": ("error",),
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
    def create(cls, cfg: Config, repo: Path, pipeline: str, params: dict) -> Run:
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
        run.emit("run_started", run_id=run_id, pipeline=pipeline, params=params, budget_usd=cfg.defaults.run_budget_usd)
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

    def refine_note_path(self, phase: str, round: int) -> Path:
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
