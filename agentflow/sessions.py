"""A run's agent sessions, read from its `events.jsonl`. Read-only: nothing here writes a file or an event."""

from __future__ import annotations

import re
from pathlib import Path

from agentflow import views
from agentflow.artifacts import NotFound
from agentflow.autonomy import RESOLVE_MARK
from agentflow.events import parse_iso
from agentflow.run_files import _head
from agentflow.state import Run, cost_added

SESSION_ID = re.compile(r"^s\d{1,9}$")
SUMMARY_MAX = 200


def _key(ev: dict) -> str:
    return str(ev.get("step") or ev.get("key") or "")


def _label(key: str, attempt: int, sizer: bool) -> str:
    title = views.step_title(key)
    if sizer:
        return f"{title} · sizer"
    if RESOLVE_MARK in key and attempt == 1:
        return title
    return f"{title} · attempt {attempt}"


def _duration(start: object, end: object) -> float | None:
    try:
        return round((parse_iso(str(end)) - parse_iso(str(start))).total_seconds(), 3)
    except (TypeError, ValueError):
        return None


def _first_line(text: object) -> str:
    lines = str(text or "").strip().splitlines()
    return lines[0].strip()[:SUMMARY_MAX] if lines else ""


def _has_transcript(run: Run, key: str, attempt: int) -> bool:
    return _head(run.dir, run.transcript_path(key, attempt), 0) is not None


def run_sessions(run: Run, events: list[dict] | None = None) -> list[dict]:
    """One row per `agent_started` event, newest first."""
    if events is None:
        events = run.events.read()
    rows: list[dict] = []
    open_rows: list[dict] = []  # started and not exited, oldest first
    attempts: dict[str, int] = {}
    latest: dict[str, dict] = {}  # the newest row of each key
    counted: dict[str, float] = {}
    subs: dict[int, dict[str, dict]] = {}

    def close(row: dict, ev: dict) -> None:
        row["ended_at"] = ev.get("ts")
        row["exit_code"] = ev.get("exit_code")
        row["cost"] = round(cost_added(counted, ev), 4)
        row["duration_s"] = _duration(row["started_at"], ev.get("ts"))
        open_rows.remove(row)

    for ev in events:
        kind = ev.get("type")
        if kind == "agent_started":
            key = _key(ev)
            sizer = bool(ev.get("sizer"))
            if not sizer:
                attempts[key] = attempts.get(key, 0) + 1
            attempt = attempts.get(key, 0) if not sizer else 1
            row = {
                "id": f"s{ev.get('seq')}", "key": key, "label": _label(key, attempt, sizer),
                "phase": ev.get("phase"), "role": ev.get("role"), "agent": ev.get("agent"),
                "model": ev.get("model"), "effort": ev.get("effort"),
                "effort_reason": views._effort_reason(run.state, key), "sizer": sizer, "attempt": attempt,
                "session_id": ev.get("session_id"), "pid": ev.get("pid"), "started_at": ev.get("ts"),
                "ended_at": None, "duration_s": None, "cost": 0.0, "exit_code": None, "live": False,
                "context_pct": None, "last_tool": None, "summary": "", "transcript": False, "subagents": [],
            }
            rows.append(row)
            open_rows.append(row)
            latest[key] = row
            subs[id(row)] = {}
        elif kind == "agent_session":
            pid, key = ev.get("pid"), ev.get("key")
            for row in reversed(open_rows):
                if row["session_id"] is not None:
                    continue
                if (pid is not None and row["pid"] == pid) or (row["pid"] is None and key and row["key"] == key):
                    row["session_id"] = ev.get("session_id")
                    break
        elif kind == "agent_turn":
            sid = ev.get("session_id")
            for row in reversed(open_rows):
                if sid is not None and row["session_id"] == sid:
                    row["context_pct"] = ev.get("context_pct", row["context_pct"])
                    row["last_tool"] = ev.get("last_tool") or row["last_tool"]
                    break
        elif kind == "agent_exited":
            sid, pid, key = ev.get("session_id"), ev.get("pid"), ev.get("key")
            match = None
            if sid is not None:
                match = next((r for r in open_rows if r["session_id"] == sid), None)
            if match is None and pid is not None:
                match = next((r for r in open_rows if r["pid"] == pid), None)
            if match is None and key:
                match = next((r for r in reversed(open_rows) if r["key"] == key), None)
            if match is not None:
                close(match, ev)
            else:
                cost_added(counted, ev)  # an exit with no row still counts toward the session's total
        elif kind in ("subagent_started", "subagent_finished"):
            parent = ev.get("parent_session")
            row = next((r for r in reversed(open_rows) if parent is not None and r["session_id"] == parent), None)
            if row is None:
                continue
            seen = subs[id(row)]
            tool = ev.get("tool_use_id")
            if kind == "subagent_started":
                entry = {"subagent_type": ev.get("subagent_type"), "model": ev.get("model"), "done": False}
                row["subagents"].append(entry)
                if tool is not None:
                    seen[str(tool)] = entry
            else:
                entry = seen.get(str(tool)) if tool is not None else None
                if entry is None:
                    entry = next((e for e in row["subagents"] if not e["done"]
                                  and e["subagent_type"] == ev.get("subagent_type")), None)
                if entry is not None:
                    entry["done"] = True
        elif kind in ("step_finished", "hitl_raised"):
            row = latest.get(_key(ev))
            if row is not None and not row["summary"] and ev.get("summary"):
                row["summary"] = _first_line(ev.get("summary"))
    for row in rows:
        row["live"] = row["ended_at"] is None and views._alive(row["pid"])
        row["transcript"] = _has_transcript(run, row["key"], row["attempt"])
    rows.reverse()
    return rows


def find_session(run: Run, sid: str) -> dict:
    if isinstance(sid, str) and SESSION_ID.fullmatch(sid):
        for row in run_sessions(run):
            if row["id"] == sid:
                return row
    raise NotFound(f"no session {sid} in this run")


def transcript_file(run: Run, session: dict) -> Path:
    return run.transcript_path(session["key"], session["attempt"])
