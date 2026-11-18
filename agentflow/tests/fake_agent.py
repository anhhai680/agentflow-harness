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
(the result event says `is_error`), delete [relpath, ...] (files removed after the writes), hold_tool s (one
`Bash` tool call that stays open, with nothing on the stream, for s seconds before its result), linger s
(after the last line, stdout is closed and the process stays alive s more seconds, as a CLI's shutdown does),
stderr str (written to stderr before anything else, as a CLI that refuses a flag does: pair it with `no_init` and
`exit`), model_id str (the model the stream names, default `fake-<alias>`), windows {"five_hour": u, "seven_day": u}
(a `rate_limit_event` whose `unifiedWindows` carries those utilizations, status `allowed`, sent before `rate_limit`).

Every result event carries `model_usage_since_last_result`, this call's usage (`inputTokens` 10, and the act keys
cache_read, cache_write_5m, cache_write_1h and output_tokens, default 0, 0, 0 and 20), and `modelUsage`, the
session's total over every `--resume` (as Claude does). Act key results [{...}] sends one more result event per
payload after the first, each with `num_turns` 0 and an empty `model_usage_since_last_result` unless the payload
sets them (one invocation can send several results).
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


def call_usage(act: dict) -> dict:
    """This call's tokens, per model field as `model_usage_since_last_result` carries them."""
    write_5m, write_1h = int(act.get("cache_write_5m", 0)), int(act.get("cache_write_1h", 0))
    return {
        "inputTokens": 10,
        "outputTokens": int(act.get("output_tokens", 20)),
        "cacheReadInputTokens": int(act.get("cache_read", 0)),
        "cacheCreationInputTokens": write_5m + write_1h,
        "cacheCreationEphemeral5mInputTokens": write_5m,
        "cacheCreationEphemeral1hInputTokens": write_1h,
    }


def session_usage(session: str, model: str, usage: dict) -> dict:
    """`modelUsage` as Claude reports it: the session's tokens so far, over every `--resume`."""
    calls_path = os.environ.get("AGENTFLOW_FAKE_CALLS")
    if not calls_path:
        return {model: dict(usage)}
    totals_path = Path(calls_path + ".usage")
    totals = json.loads(totals_path.read_text(encoding="utf-8")) if totals_path.exists() else {}
    models = totals.setdefault(session, {})
    row = models.setdefault(model, {})
    for key, tokens in usage.items():
        row[key] = row.get(key, 0) + tokens
    totals_path.write_text(json.dumps(totals), encoding="utf-8")
    return models


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
                or k in os.environ.get("AGENTFLOW_FAKE_ENV", "").split(",")  # names a test asks to see
            },
            "stdin_devnull": stdin_is_devnull(),
            "step": step,
        }
        with open(calls_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")

    act = next_act(step)
    session = flag_value(argv, "--resume") or str(uuid.uuid4())
    model = act.get("model_id") or "fake-" + (flag_value(argv, "--model") or "model")
    exit_code = int(act.get("exit", 0))
    base = {"session_id": session}

    if act.get("stderr"):
        sys.stderr.write(act["stderr"])
        sys.stderr.flush()
    emit({"type": "system", "subtype": "hook_started", "hook_name": "SessionStart:startup", **base})
    if not act.get("no_init"):
        emit({"type": "system", "subtype": "init", "model": model, "per_turn_effort_active": True, **base})
    if act.get("windows"):
        unified = {name: {"utilization": u, "resetsAt": 1794910800, "status": "allowed"}
                   for name, u in act["windows"].items()}
        emit({"type": "rate_limit_event", "rate_limit_info": {
            "status": "allowed", "resetsAt": 1794910800, "rateLimitType": "five_hour", "unifiedWindows": unified}, **base})
    if act.get("rate_limit"):
        emit({"type": "rate_limit_event", "rate_limit_info": act["rate_limit"], **base})

    for relpath, text in act.get("write", {}).items():
        target = Path(relpath)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    for relpath in act.get("delete", []):
        Path(relpath).unlink()
    if act.get("git_commit"):
        subprocess.run(["git", "add", "-A"], check=True, capture_output=True)
        subprocess.run(["git", "commit", "-q", "-m", act["git_commit"]], check=True, capture_output=True)

    serial = 0

    def tool_turn(name: str, tool_input: dict, hold: float = 0) -> None:
        nonlocal serial
        serial += 1
        tool_id = f"toolu_fake_{serial}"
        content = [{"type": "tool_use", "id": tool_id, "name": name, "input": tool_input}]
        emit({"type": "assistant", "message": {"role": "assistant", "model": model, "content": content,
                                               "usage": usage(2000)}, "parent_tool_use_id": None, **base})
        if hold:
            time.sleep(hold)  # the tool runs: the stream says nothing until it returns
        emit({"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tool_id, "content": "ok"}]}, "parent_tool_use_id": None, **base})

    for call in act.get("tool_calls", []):
        tool_turn(call["name"], call.get("input", {}))
    if act.get("hold_tool"):
        tool_turn("Bash", {"command": "make test"}, hold=float(act["hold_tool"]))
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
    this_call = call_usage(act)
    cumulative = {name: {**tokens, "contextWindow": WINDOW, "costUSD": cost}
                  for name, tokens in session_usage(session, model, this_call).items()}
    result = {"type": "result", "subtype": "success", "is_error": bool(act.get("result_error")), "result": final_text,
              "total_cost_usd": cost, "num_turns": 1, "terminal_reason": "completed",
              "permission_denials": act.get("permission_denials", []), "modelUsage": cumulative,
              "model_usage_since_last_result": {model: {**this_call, "contextWindow": WINDOW}}, **base}
    emit(result)
    for extra in act.get("results", []):
        emit({**result, "num_turns": 0, "model_usage_since_last_result": {}, **extra, **base})
    if act.get("linger"):
        sys.stdout.flush()
        sys.stdout.close()  # a std stream does not own its fd: close the fd too, so the reader sees EOF
        os.close(1)
        time.sleep(float(act["linger"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
