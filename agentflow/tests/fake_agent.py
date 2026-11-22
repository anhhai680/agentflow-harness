#!/usr/bin/env python3
"""A scripted stand-in for `claude -p --output-format stream-json`, used by every agentflow test.

`AGENTFLOW_FAKE_SCRIPT` names a JSON file `{step_key: [act, ...]}`; each run consumes the next
act for its `AGENTFLOW_STEP` (the cursor is kept in `<script>.cursor`). A step key with no acts
left writes `{"status": "done", "summary": "ok"}`. Every run appends `{argv, env, stdin_devnull,
step, system_prompt}` to the file named by `AGENTFLOW_FAKE_CALLS`; `system_prompt` is the text of the file
`--append-system-prompt-file` names (the run brief), or None when there is no such flag (or no such file).

Act keys: write {relpath: text}, result {...}, result_raw str, usage_pct [..], tool_calls
[{name, input}], subagents [{subagent_type, model}], parallel_subagents [{description, model?, subagent_type?, write?:
{path: text}, output_tokens?, error?, unfinished?, background?}] (one `assistant` line holding every `Agent` call, then one `assistant` line per
sub-agent carrying its `parent_tool_use_id`, its usage (`output_tokens`, default 20, priced as `model_id`) and its
`write`s applied, then one `user` line holding every result, an `is_error` one for `error: true`, as the CLI answers a
call it refused, and none for an `unfinished: true` one; `background: true` puts `run_in_background: true` in the
call's input: the sub-agents run concurrently), compact bool, git_commit msg, sleep s,
exit n, no_init bool, final_text str, cost float (this call's, default 0.01; the result event reports
the session's total, as Claude does), permission_denials [{tool_name, tool_input}] (copied into the
result event), rate_limit {...} (sent as a `rate_limit_event`'s `rate_limit_info`), result_error bool
(the result event says `is_error`), delete [relpath, ...] (files removed after the writes), hold_tool s (one
`Bash` tool call that stays open, with nothing on the stream, for s seconds before its result), linger s
(after the last line, stdout is closed and the process stays alive s more seconds, as a CLI's shutdown does),
stderr str (written to stderr before anything else, as a CLI that refuses a flag does: pair it with `no_init` and
`exit`), model_id str (the model the stream names, default `fake-<alias>`), windows {"five_hour": u, "seven_day": u}
(a `rate_limit_event` whose `unifiedWindows` carries those utilizations, status `allowed`, sent before `rate_limit`;
`resets_at` n is the `resetsAt` it sends, default 1794910800, and `refused` true makes its top-level status `rejected`),
attempts [{tool_name, tool_input}] (tool calls the model tries, decided by the permission model below; a permitted
`Write` writes `tool_input.content` to `file_path`; a denied call is added to the result event's `permission_denials`, and
a call to a tool `--tools` leaves out to its `unavailable` list as `{tool_name, tool_input, kind: "unknown_tool"}`; a
`file_path` of `$AGENTFLOW_RESULT` is the step's result file), capped "turns"|"budget" (after the act's other keys, the
result event is the CLI's cap error: `subtype` `error_max_turns` / `error_max_budget_usd`, `is_error`, `terminal_reason`
`max_turns` / `budget_exhausted`, an empty `result`, and the process exits 1 whatever `exit` says).
`write` is not a tool call and bypasses the model: it is the fake's own hand on the tree.

The permission model of `attempts` is a model of the real CLI's, close enough for the runner's tests, not a copy.
It does NOT model: the CLI's built-in read-only command set (real `dontAsk` runs `grep`, `cat`, `ls` and the like with no
allow rule; here every `Bash` part needs one); chaining by `&`, a newline, `$(...)` or backticks (only the separators in
2. split a command); an environment-variable prefix (`FOO=1 git diff` is judged as the whole text); hooks, project or
user settings files, `--agent` profiles and sandbox settings. A denied attempt's `tool_result` is an error, as the CLI's.
Each attempt is decided in this order:
1. A tool `--tools` doesn't list doesn't exist (a `--tools`-less argv lists every tool): `unavailable`, and nothing happens.
2. A `Bash` command is split on `&&`, `||`, `;` and `|` into parts, and each `>` / `>>` redirection in a part is a write
   to its target path.
3. The deny rules of `--settings` win for any part: `Bash(<pattern>)` matches the part by `fnmatch`, and `Edit(./<rel>)` or
   `Edit(/<abs>/**)` matches a `Write` / `Edit` `file_path` or a redirection target.
4. Under `--permission-mode dontAsk` every part needs an allow rule: the bare tool name, a `Bash(<pattern>)` that matches the
   part, and for a write (a `Write` / `Edit` `file_path`, a redirection target) an `Edit(/<abs>/**)` that covers the path.
5. In any other mode the call is allowed.

Every result event carries `model_usage_since_last_result`, this call's usage (`inputTokens` 10, and the act keys
cache_read, cache_write_5m, cache_write_1h and output_tokens, default 0, 0, 0 and 20), and `modelUsage`, the
session's total over every `--resume` (as Claude does). Act key results [{...}] sends one more result event per
payload after the first, each with `num_turns` 0 and an empty `model_usage_since_last_result` unless the payload
sets them (one invocation can send several results).
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
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


def system_prompt(argv: list[str]) -> str | None:
    """The text of the file `--append-system-prompt-file` names, read as Claude would, or None."""
    path = flag_value(argv, "--append-system-prompt-file")
    if path is None:
        return None
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError:
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


BASH_SEPARATORS = re.compile(r"&&|\|\||;|\|")
REDIRECTION = re.compile(r"\d*>>?\s*(?P<target>[^\s;|&<>]+)")
WRITE_TOOLS = ("Write", "Edit")
RESULT_PLACEHOLDER = "$AGENTFLOW_RESULT"  # an attempt's `file_path` that stands for the step's result file


def tool_rules(settings: dict, kind: str) -> list[str]:
    return [rule for rule in settings.get("permissions", {}).get(kind, []) if isinstance(rule, str)]


def rule_arg(rule: str, tool: str) -> str | None:
    """`pattern` of a `Tool(pattern)` rule, or None for another tool's rule and for the bare name."""
    prefix = tool + "("
    return rule[len(prefix):-1] if rule.startswith(prefix) and rule.endswith(")") else None


def under_edit_rule(rule: str, path: Path, cwd: Path) -> bool:
    """`rule` is an `Edit(...)` rule that covers `path`: `./<rel>` is relative to the cwd, `/<abs>/**` is absolute."""
    pattern = rule_arg(rule, "Edit")
    if pattern is None:
        return False
    if pattern.startswith("./"):
        pattern = str(cwd / pattern[2:])
    elif pattern.startswith("/"):
        pattern = pattern[1:]  # `Edit(//abs/**)` is the absolute `/abs/**`
    else:
        pattern = str(cwd / pattern)
    return fnmatch.fnmatchcase(str(path), pattern)


def bash_parts(command: str) -> list[tuple[str, list[str]]]:
    """`[(command text, [redirection targets])]`: the command split on `&&`, `||`, `;`, `|`, redirections taken out."""
    parts = []
    for segment in BASH_SEPARATORS.split(command):
        targets = [match.group("target") for match in REDIRECTION.finditer(segment)]
        text = " ".join(REDIRECTION.sub(" ", segment).split())
        if text or targets:
            parts.append((text, targets))
    return parts


def decide(argv: list[str], cwd: Path, tool: str, tool_input: dict) -> str:
    """`unknown_tool`, `denied` or `allowed`, as the permission model in the module docstring decides."""
    tools = flag_value(argv, "--tools")
    if tools is not None and tool not in tools.split(","):
        return "unknown_tool"
    settings = json.loads(flag_value(argv, "--settings") or "{}")
    deny, allow = tool_rules(settings, "deny"), tool_rules(settings, "allow")
    commands: list[str] = []  # Bash parts, each needs its own allow
    writes: list[Path] = []  # every path the call writes
    if tool == "Bash":
        for text, targets in bash_parts(str(tool_input.get("command", ""))):
            if text:
                commands.append(text)
            writes += [Path(os.path.normpath(cwd / target)) for target in targets]
    elif tool in WRITE_TOOLS and tool_input.get("file_path"):
        writes.append(Path(os.path.normpath(cwd / str(tool_input["file_path"]))))
    if any(fnmatch.fnmatchcase(text, pattern) for text in commands for rule in deny
           if (pattern := rule_arg(rule, "Bash")) is not None):
        return "denied"
    if any(under_edit_rule(rule, path, cwd) for path in writes for rule in deny):
        return "denied"
    if tool in deny:  # a bare tool name in the deny list
        return "denied"
    if flag_value(argv, "--permission-mode") != "dontAsk":
        return "allowed"
    if any(not under_allow(allow, tool, path, cwd) for path in writes):
        return "denied"
    if tool == "Bash":
        if "Bash" not in allow and not all(
                any(fnmatch.fnmatchcase(text, pattern) for rule in allow if (pattern := rule_arg(rule, "Bash")) is not None)
                for text in commands):
            return "denied"
    elif tool in WRITE_TOOLS:
        if tool not in allow and not writes:
            return "denied"
    elif tool not in allow:
        return "denied"
    return "allowed"


def under_allow(allow: list[str], tool: str, path: Path, cwd: Path) -> bool:
    """An allow rule covers `tool`'s write to `path`: an `Edit(...)` rule, the bare `Edit`, or the bare name of a
    write tool itself."""
    return "Edit" in allow or (tool in WRITE_TOOLS and tool in allow) or any(under_edit_rule(rule, path, cwd) for rule in allow)


# act key `capped` -> the result event's `subtype` and `terminal_reason`, as the real CLI ends at a cap
CAPS = {"turns": ("error_max_turns", "max_turns"), "budget": ("error_max_budget_usd", "budget_exhausted")}


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
            "system_prompt": system_prompt(argv),
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
        resets = act.get("resets_at", 1794910800)
        unified = {name: {"utilization": u, "resetsAt": resets, "status": "allowed"}
                   for name, u in act["windows"].items()}
        emit({"type": "rate_limit_event", "rate_limit_info": {
            "status": "rejected" if act.get("refused") else "allowed", "resetsAt": resets,
            "rateLimitType": "five_hour", "unifiedWindows": unified}, **base})
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

    def tool_turn(name: str, tool_input: dict, hold: float = 0, denied: bool = False) -> None:
        nonlocal serial
        serial += 1
        tool_id = f"toolu_fake_{serial}"
        content = [{"type": "tool_use", "id": tool_id, "name": name, "input": tool_input}]
        emit({"type": "assistant", "message": {"role": "assistant", "model": model, "content": content,
                                               "usage": usage(2000)}, "parent_tool_use_id": None, **base})
        if hold:
            time.sleep(hold)  # the tool runs: the stream says nothing until it returns
        # a denied call never ran: the CLI answers it with an error result, as the real one does
        answer = ({"content": f"Permission to use {name} has been denied.", "is_error": True} if denied
                  else {"content": "ok"})
        emit({"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tool_id, **answer}]}, "parent_tool_use_id": None, **base})

    for call in act.get("tool_calls", []):
        tool_turn(call["name"], call.get("input", {}))
    denials: list[dict] = list(act.get("permission_denials", []))
    unavailable: list[dict] = []
    for attempt in act.get("attempts", []):
        tool, tool_input = attempt["tool_name"], dict(attempt.get("tool_input", {}))
        if tool_input.get("file_path") == RESULT_PLACEHOLDER:  # the step's result file, which a script can't name
            tool_input["file_path"] = os.environ.get("AGENTFLOW_RESULT", "")
        verdict = decide(argv, Path.cwd(), tool, tool_input)
        if verdict == "unknown_tool":  # the model can't call a tool it was not given: nothing reaches the stream
            unavailable.append({"tool_name": tool, "tool_input": tool_input, "kind": "unknown_tool"})
            continue
        tool_turn(tool, tool_input, denied=verdict == "denied")
        if verdict == "denied":
            denials.append({"tool_name": tool, "tool_input": tool_input})
        elif tool == "Write":
            target = Path(tool_input["file_path"])
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(str(tool_input.get("content", "")), encoding="utf-8")
    if act.get("hold_tool"):
        tool_turn("Bash", {"command": "make test"}, hold=float(act["hold_tool"]))
    for sub in act.get("subagents", []):
        tool_turn("Agent", {"description": "fake subagent", "prompt": "fake",
                            "subagent_type": sub["subagent_type"], "model": sub.get("model")})
    parallel = act.get("parallel_subagents", [])
    if parallel:
        ids = [f"toolu_fake_par_{i}" for i in range(len(parallel))]
        emit({"type": "assistant", "message": {"role": "assistant", "model": model, "usage": usage(2000), "content": [
            {"type": "tool_use", "id": call_id, "name": "Agent", "input": {
                "description": sub.get("description", "fake subagent"), "prompt": "fake",
                "subagent_type": sub.get("subagent_type", "general-purpose"), "model": sub.get("model"),
                **({"run_in_background": True} if sub.get("background") else {})}}
            for call_id, sub in zip(ids, parallel)]}, "parent_tool_use_id": None, **base})
        for call_id, sub in zip(ids, parallel):
            for relpath, text in sub.get("write", {}).items():
                target = Path(relpath)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text, encoding="utf-8")
            emit({"type": "assistant", "message": {
                "role": "assistant", "id": f"msg_{call_id}", "model": model,
                "content": [{"type": "text", "text": "working"}],
                "usage": {**usage(2000), "output_tokens": int(sub.get("output_tokens", 20))}},
                "parent_tool_use_id": call_id, **base})
        emit({"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": call_id, **({"content": "Agent type not found", "is_error": True}
                                                               if sub.get("error") else {"content": "ok"})}
            for call_id, sub in zip(ids, parallel) if not sub.get("unfinished")]},
              "parent_tool_use_id": None, **base})
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

    capped = act.get("capped")
    if capped is None and exit_code != 0:
        return exit_code
    cost = session_total(session, float(act.get("cost", 0.01)))
    final_text = act.get("final_text", "done")
    this_call = call_usage(act)
    cumulative = {name: {**tokens, "contextWindow": WINDOW, "costUSD": cost}
                  for name, tokens in session_usage(session, model, this_call).items()}
    result = {"type": "result", "subtype": "success", "is_error": bool(act.get("result_error")), "result": final_text,
              "total_cost_usd": cost, "num_turns": 1, "terminal_reason": "completed",
              "permission_denials": denials, "modelUsage": cumulative,
              "model_usage_since_last_result": {model: {**this_call, "contextWindow": WINDOW}}, **base}
    if capped:
        subtype, reason = CAPS[capped]
        result.update(subtype=subtype, is_error=True, result="", terminal_reason=reason)
    if unavailable:
        result["unavailable"] = unavailable
    emit(result)
    if capped:
        return 1
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
