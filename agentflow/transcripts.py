"""An agent session's transcript (`claude -p` stream-json or `codex exec --json`), read as conversation turns in
pages of whole lines. Read-only: a page is cut at a newline, so a record is never split."""

from __future__ import annotations

import copy
import json
import threading
from collections import OrderedDict

from .run_files import read_range
from .sessions import find_session, transcript_file
from .state import Run

TEXT_MAX = 8 * 1024
IO_MAX = 4 * 1024
SUMMARY_MAX = 200
WAIT_TYPES = ("tool_progress", "rate_limit_event", "stream_event")
CODEX_WAITS = ("turn.started", "turn.completed", "item.started", "item.updated")
SCAN_STEP = 64 * 1024
CACHE_MAX = 64

_cache: OrderedDict[tuple, tuple[list[dict], int]] = OrderedDict()
_cache_lock = threading.Lock()


def _cut(text: object, limit: int) -> str:
    text = text if isinstance(text, str) else ("" if text is None else str(text))
    if len(text) <= limit:
        return text
    return f"{text[:limit]} … ({len(text) - limit} more characters)"


def _json(value: object) -> str:
    try:
        return json.dumps(value, indent=2, ensure_ascii=False, default=str)
    except (TypeError, ValueError, RecursionError):
        return "(too deeply nested to show)"


def _tool_line(name: str, data: object) -> str:
    data = data if isinstance(data, dict) else {}
    key = {"Bash": "command", "Read": "file_path", "Edit": "file_path", "Write": "file_path",
           "Grep": "pattern", "Glob": "pattern", "Agent": "description", "Task": "description"}.get(name)
    value = data.get(key) if key else None
    if not isinstance(value, str):
        value = next((v for v in data.values() if isinstance(v, str)), "")
    return _cut(f"{name}: {value}" if value else name, SUMMARY_MAX)


def _tool_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [b.get("text", "") if isinstance(b, dict) else (b if isinstance(b, str) else "") for b in content]
        return "\n".join(p for p in parts if isinstance(p, str))
    return "" if content is None else str(content)


def _codex_turns(event: dict) -> list[dict]:
    item = event.get("item") if isinstance(event.get("item"), dict) else {}
    kind = item.get("type")
    if kind == "agent_message":
        return [{"kind": "text", "text": _cut(item.get("text"), TEXT_MAX), "parent": None}]
    if kind == "reasoning":
        return [{"kind": "thinking", "text": _cut(item.get("text"), TEXT_MAX), "parent": None}]
    shown = {k: v for k, v in item.items() if k != "id"}
    if kind == "command_execution":
        turn = {"kind": "tool", "id": item.get("id"), "name": "command",
                "line": _cut("command: " + str(item.get("command") or ""), SUMMARY_MAX),
                "input": _cut(_json({k: v for k, v in shown.items() if k != "aggregated_output"}), IO_MAX),
                "parent": None, "result": _cut(item.get("aggregated_output"), IO_MAX)}
        return [turn]
    name = f"{item.get('server')}.{item.get('tool')}" if kind == "mcp_tool_call" else str(kind or "item")
    return [{"kind": "tool", "id": item.get("id"), "name": name, "line": _tool_line(name, shown),
             "input": _cut(_json(shown), IO_MAX), "parent": None}]


def _turns_of(event: dict) -> list[dict] | None:
    """The turns of one record, or None for a wait record."""
    kind = event.get("type")
    parent = event.get("parent_tool_use_id")
    if kind in WAIT_TYPES or kind in CODEX_WAITS or (kind == "system" and event.get("subtype") != "init"):
        return None
    if kind == "system":
        return [{"kind": "session", "model": event.get("model")}]
    if kind == "thread.started":
        return [{"kind": "session", "model": event.get("model")}]
    if kind == "item.completed":
        return _codex_turns(event)
    if kind == "result":
        return [{"kind": "result", "text": _cut(event.get("result"), TEXT_MAX), "cost": event.get("total_cost_usd"),
                 "is_error": bool(event.get("is_error"))}]
    if kind in ("assistant", "user"):
        message = event.get("message") if isinstance(event.get("message"), dict) else {}
        content = message.get("content")
        blocks = [{"type": "text", "text": content}] if isinstance(content, str) else content
        out: list[dict] = []
        for block in blocks if isinstance(blocks, list) else []:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if kind == "assistant" and btype == "text":
                out.append({"kind": "text", "text": _cut(block.get("text"), TEXT_MAX), "parent": parent})
            elif kind == "assistant" and btype == "thinking":
                out.append({"kind": "thinking", "text": _cut(block.get("thinking"), TEXT_MAX), "parent": parent})
            elif kind == "assistant" and btype == "tool_use":
                name = str(block.get("name") or "tool")
                out.append({"kind": "tool", "id": block.get("id"), "name": name,
                            "line": _tool_line(name, block.get("input")),
                            "input": _cut(_json(block.get("input")), IO_MAX), "parent": parent})
            elif kind == "user" and btype == "tool_result":
                out.append({"kind": "tool_result", "id": block.get("tool_use_id"),
                            "text": _cut(_tool_text(block.get("content")), IO_MAX),
                            "is_error": bool(block.get("is_error")), "parent": parent})
            else:  # a user text block (the compaction notice) or a block type this parser does not know
                shown = block.get("text") if btype == "text" and isinstance(block.get("text"), str) else json.dumps(block, ensure_ascii=False)
                out.append({"kind": "raw", "text": _cut(shown, IO_MAX), "parent": parent})
        return out
    return [{"kind": "raw", "text": _cut(json.dumps(event, ensure_ascii=False), IO_MAX)}]


def parse_lines(lines: list[bytes]) -> list[dict]:
    turns: list[dict] = []
    wait: dict | None = None
    for line in lines:
        text = line.decode("utf-8", errors="replace").strip()
        if not text:
            continue
        try:
            event = json.loads(text)
        except (ValueError, RecursionError):
            event = None
        if not isinstance(event, dict):
            wait = None
            turns.append({"kind": "raw", "text": _cut(text, IO_MAX)})
            continue
        try:
            found = _turns_of(event)
        except RecursionError:
            wait = None
            turns.append({"kind": "raw", "text": _cut(text, IO_MAX)})
            continue
        if found is None:
            seconds = event.get("elapsed_time_seconds")
            seconds = seconds if isinstance(seconds, (int, float)) and not isinstance(seconds, bool) else None
            if wait is None:
                wait = {"kind": "wait", "records": 0, "seconds": None}
                turns.append(wait)
            wait["records"] += 1
            if seconds is not None and (wait["seconds"] is None or seconds > wait["seconds"]):
                wait["seconds"] = seconds
            continue
        wait = None
        turns.extend(found)
    return turns


def _line_end(run: Run, path, pos: int) -> int | None:
    """The offset just after the next newline at or after `pos`, found in steps without keeping the bytes."""
    while True:
        got = read_range(run.dir, path, pos, SCAN_STEP)
        if got is None or not got[0]:
            return None
        at = got[0].find(b"\n")
        if at >= 0:
            return pos + at + 1
        pos += len(got[0])


def _parsed(run: Run, path, after: int, max_bytes: int, data: bytes, size: int) -> tuple[list[dict], int]:
    end = data.rfind(b"\n") + 1
    if end:
        return parse_lines(data[:end].split(b"\n")), after + end
    if len(data) >= max_bytes:  # one line longer than a page: step over it once its newline is found
        stop = _line_end(run, path, after + len(data))
        if stop is not None:
            return [{"kind": "skipped", "bytes": stop - after}], stop
    return [], after


def transcript_page(run: Run, sid: str, after: int, max_bytes: int) -> dict:
    session = find_session(run, sid)
    path = transcript_file(run, session)
    if isinstance(after, bool) or not isinstance(after, int) or after < 0:
        raise ValueError("after must be a page boundary")
    got = read_range(run.dir, path, after, max_bytes)
    if got is None:
        return {"session": session, "turns": [], "after": after, "next": 0, "size": 0, "done": True, "missing": True}
    data, size = got
    if after > size:
        raise ValueError("after must be a page boundary")
    if after:
        before = read_range(run.dir, path, after - 1, 1)
        if before is None or before[0] != b"\n":
            raise ValueError("after must be a page boundary")
    try:
        mtime = path.stat().st_mtime_ns
    except OSError:
        mtime = 0
    key = (str(path), after, max_bytes, size, mtime)
    with _cache_lock:
        hit = _cache.get(key)
        if hit is not None:
            _cache.move_to_end(key)
    if hit is None:
        hit = _parsed(run, path, after, max_bytes, data, size)
        with _cache_lock:
            _cache[key] = hit
            while len(_cache) > CACHE_MAX:
                _cache.popitem(last=False)
    turns, nxt = copy.deepcopy(hit[0]), hit[1]
    return {"session": session, "turns": turns, "after": after, "next": nxt, "size": size,
            "done": nxt == size and not session["live"]}
