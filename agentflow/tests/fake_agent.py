#!/usr/bin/env python3
"""A scripted stand-in for `claude -p --output-format stream-json`, used by every agentflow test.

`AGENTFLOW_FAKE_SCRIPT` names a JSON file `{step_key: [act, ...]}`; each run consumes the next
act for its `AGENTFLOW_STEP` (the cursor is kept in `<script>.cursor`). A step key with no acts
left writes `{"status": "done", "summary": "ok"}`. Every run appends `{argv, env, stdin_devnull,
step}` to the file named by `AGENTFLOW_FAKE_CALLS`.

Act keys: write {relpath: text}, result {...}, result_raw str, usage_pct [..], tool_calls
[{name, input}], subagents [{subagent_type, model}], compact bool, git_commit msg, sleep s,
exit n, no_init bool, final_text str, cost float (this call's, default 0.01; the result event reports
the session's total, as Claude does), permission_denials [{tool_name, tool_input}] (copied into the
result event), rate_limit {...} (sent as a `rate_limit_event`'s `rate_limit_info`), result_error bool
(the result event says `is_error`).
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import time
import uuid
from pathlib import Path

WINDOW = 200000
DEFAULT_ACT = {"result": {"status": "done", "summary": "ok"}}


def flag_value(argv: list[str], flag: str) -> str | None:
    if flag in argv:
        index = argv.index(flag)
        if index + 1 < len(argv):
            return argv[index + 1]
    return None


def stdin_is_devnull() -> bool:
    info = os.fstat(0)
    return stat.S_ISCHR(info.st_mode) and info.st_rdev == os.stat(os.devnull).st_rdev


def next_act(step: str) -> dict:
    script_path = os.environ.get("AGENTFLOW_FAKE_SCRIPT")
    if not script_path:
        return DEFAULT_ACT
    script = json.loads(Path(script_path).read_text(encoding="utf-8"))
    cursor_path = Path(script_path + ".cursor")
    cursor = json.loads(cursor_path.read_text(encoding="utf-8")) if cursor_path.exists() else {}
    position = cursor.get(step, 0)
    acts = script.get(step, [])
    cursor[step] = position + 1
    cursor_path.write_text(json.dumps(cursor), encoding="utf-8")
    return acts[position] if position < len(acts) else DEFAULT_ACT


def emit(event: dict) -> None:
    sys.stdout.write(json.dumps(event) + "\n")
    sys.stdout.flush()


def usage(total_tokens: int) -> dict:
    return {
        "input_tokens": 10,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": total_tokens - 10,
        "output_tokens": 20,
    }


def session_total(session: str, cost: float) -> float:
    """`total_cost_usd` as Claude reports it: everything the session has cost so far, over every `--resume`."""
    calls_path = os.environ.get("AGENTFLOW_FAKE_CALLS")
    if not calls_path:
        return cost
    totals_path = Path(calls_path + ".costs")
    totals = json.loads(totals_path.read_text(encoding="utf-8")) if totals_path.exists() else {}
    totals[session] = totals.get(session, 0.0) + cost
    totals_path.write_text(json.dumps(totals), encoding="utf-8")
    return totals[session]


def main() -> int:
    argv = sys.argv[1:]
    step = os.environ.get("AGENTFLOW_STEP", "")
    calls_path = os.environ.get("AGENTFLOW_FAKE_CALLS")
    if calls_path:
        record = {
            "argv": argv,
            "env": {
                k: v
                for k, v in os.environ.items()
                if k.startswith("AGENTFLOW_") and not k.startswith("AGENTFLOW_FAKE_")
                or k == "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"
            },
            "stdin_devnull": stdin_is_devnull(),
            "step": step,
        }
        with open(calls_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")

    act = next_act(step)
    session = flag_value(argv, "--resume") or str(uuid.uuid4())
    model = "fake-" + (flag_value(argv, "--model") or "model")
    exit_code = int(act.get("exit", 0))
    base = {"session_id": session}

    emit({"type": "system", "subtype": "hook_started", "hook_name": "SessionStart:startup", **base})
    if not act.get("no_init"):
        emit({"type": "system", "subtype": "init", "model": model, "per_turn_effort_active": True, **base})
    if act.get("rate_limit"):
        emit({"type": "rate_limit_event", "rate_limit_info": act["rate_limit"], **base})

    for relpath, text in act.get("write", {}).items():
        target = Path(relpath)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    if act.get("git_commit"):
        subprocess.run(["git", "add", "-A"], check=True, capture_output=True)
        subprocess.run(["git", "commit", "-q", "-m", act["git_commit"]], check=True, capture_output=True)

    serial = 0

    def tool_turn(name: str, tool_input: dict) -> None:
        nonlocal serial
        serial += 1
        tool_id = f"toolu_fake_{serial}"
        content = [{"type": "tool_use", "id": tool_id, "name": name, "input": tool_input}]
        emit({"type": "assistant", "message": {"role": "assistant", "model": model, "content": content,
                                               "usage": usage(2000)}, "parent_tool_use_id": None, **base})
        emit({"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tool_id, "content": "ok"}]}, "parent_tool_use_id": None, **base})

    for call in act.get("tool_calls", []):
        tool_turn(call["name"], call.get("input", {}))
    for sub in act.get("subagents", []):
        tool_turn("Agent", {"description": "fake subagent", "prompt": "fake",
                            "subagent_type": sub["subagent_type"], "model": sub.get("model")})
    if act.get("compact"):
        emit({"type": "system", "subtype": "compact_boundary",
              "compact_metadata": {"trigger": "auto", "pre_tokens": 120000}, **base})
    for pct in act.get("usage_pct", []):
        serial += 1
        emit({"type": "assistant", "message": {"role": "assistant", "model": model,
                                               "content": [{"type": "text", "text": "working"}],
                                               "usage": usage(int(pct * WINDOW / 100))},
              "parent_tool_use_id": None, **base})
        emit({"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": f"toolu_fake_turn_{serial}", "content": "ok"}]},
              "parent_tool_use_id": None, **base})

    if act.get("sleep"):
        time.sleep(float(act["sleep"]))

    result_path = os.environ.get("AGENTFLOW_RESULT")
    if result_path and ("result_raw" in act or "result" in act):
        target = Path(result_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        text = act["result_raw"] if "result_raw" in act else json.dumps(act["result"])
        target.write_text(text, encoding="utf-8")

    if exit_code != 0:
        return exit_code
    cost = session_total(session, float(act.get("cost", 0.01)))
    final_text = act.get("final_text", "done")
    emit({"type": "result", "subtype": "success", "is_error": bool(act.get("result_error")), "result": final_text,
          "total_cost_usd": cost, "num_turns": 1, "permission_denials": act.get("permission_denials", []),
          "modelUsage": {model: {"contextWindow": WINDOW, "costUSD": cost}}, **base})
    return 0


if __name__ == "__main__":
    sys.exit(main())
