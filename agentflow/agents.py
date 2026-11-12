"""Agent adapters: build the argv/env for `claude -p` and `codex exec`, run them, parse their streams.

`run` starts the process with stdin closed, tees stdout to the transcript, turns stream lines
into runner events, and enforces two limits: the step timeout, and the context rollover (D9).
Parsing is pure (`parse_claude_line`, `parse_codex_line`) so tests can feed recorded streams.
"""

from __future__ import annotations

import abc
import json
import os
import shlex
import signal
import subprocess
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from . import layers, safeio
from .config import AgentSpec, Config, ContextPolicy

KILL_GRACE_S = 10
SUBAGENT_TOOLS = ("Agent", "Task")
AUTOCOMPACT_WINDOW_ENV = "CLAUDE_CODE_AUTO_COMPACT_WINDOW"  # Claude Code's own cap on the window `autocompact_pct` is read against
SHELL_ENV_FILE_ENV = "CLAUDE_ENV_FILE"  # a script Claude Code sources in its Bash tool's shell before each command
SYNTHETIC_MODEL = "<synthetic>"  # Claude Code's own error messages, shown as assistant turns: no model replied

READ_ONLY_TOOLS = ("Read", "Grep", "Glob")  # the only Claude tools a read-only call gets

EventCallback = Callable[[str, dict], None]


@dataclass
class AgentCall:
    prompt: str
    model: str
    effort: str
    cwd: Path
    run_id: str
    step_key: str
    result_path: Path
    transcript_path: Path
    add_dirs: list[Path]
    timeout_s: int
    resume_session: str | None = None
    agent_profile: str | None = None
    context_window: int | None = None  # the model's window when the runner knows it (else the default)
    env: dict[str, str] = field(default_factory=dict)  # `defaults.env`
    forbidden_paths: list[str] = field(default_factory=list)  # `guardrails.forbidden_paths`
    runs_root: Path | None = None  # the runner's own state (`state.runs_root`): no agent edits it
    env_base: Mapping[str, str] | None = None  # the inherited environment (`run_env.base`); None: this process's
    max_turns: int | None = None  # this call's turn cap; None: the agent's own `max_turns`
    read_only: bool = False  # Claude gets only `READ_ONLY_TOOLS` and no MCP server; Codex a read-only sandbox (fresh session)


@dataclass
class Interruption:
    """Why a call ended without finishing. `usage_limit` carries the reset time (`until`, epoch seconds);
    `transient` is everything else, unknown failures included. `reached_model` tells an outage, where a
    retry is free, from a call that spent money and still died. `limit` is the stream's last limit info."""

    kind: str
    reached_model: bool
    until: float | None = None
    limit: dict | None = None


@dataclass
class AgentOutcome:
    session_id: str | None
    exit_code: int
    cost_usd: float
    context_pct: float
    context_window: int | None
    final_text: str
    tool_calls: list[dict]
    permission_denials: list[dict]
    subagents: list[dict]
    compactions: int
    rolled_over: bool
    timed_out: bool
    resume_failed: bool
    interruption: Interruption | None = None


class Agent(Protocol):
    name: str

    def argv(self, call: AgentCall) -> list[str]: ...

    def env(self, call: AgentCall) -> dict[str, str]: ...

    def run(self, call: AgentCall, on_event: EventCallback) -> AgentOutcome: ...


# --------------------------------------------------------------------------- stream parsing


@dataclass
class StreamAccumulator:
    """What the stream has said so far. `line_type` and `over_threshold` describe the latest line."""

    default_window: int
    window_override: int | None = None
    rollover_pct: float | None = None
    max_compactions: int | None = None  # a session that has compacted this often is replaced by a fresh one
    window: int | None = None
    session_id: str | None = None
    model: str | None = None
    cost_usd: float = 0.0
    cost_reported: bool = False  # True once Claude's own `result` gave the cost; before that it is an estimate
    prices: dict[str, dict[str, float]] = field(default_factory=dict)
    usage_by_message: dict[str, tuple[str, dict]] = field(default_factory=dict)
    context_pct: float = 0.0
    final_text: str = ""
    tool_calls: list[dict] = field(default_factory=list)
    permission_denials: list[dict] = field(default_factory=list)
    subagents: list[dict] = field(default_factory=list)
    compactions: int = 0
    line_type: str = ""  # "assistant" | "user" | "result" for a main-thread line, else ""
    over_threshold: bool = False
    open_subagents: dict[str, dict] = field(default_factory=dict)
    limit: dict | None = None  # the latest `rate_limit_event`'s `rate_limit_info`
    reached_model: bool = False
    result_error: bool = False

    @property
    def compactions_spent(self) -> bool:
        return self.max_compactions is not None and self.compactions >= self.max_compactions

    @property
    def effective_window(self) -> int:
        return self.window_override or self.window or self.default_window

    def context_pct_of(self, tokens: int) -> float:
        return tokens / self.effective_window * 100

    def interruption(self, exit_code: int) -> Interruption | None:
        """Why the call ended early, or None for a clean end. Only the top-level `status` says requests
        are refused: a window can show `rejected` while extra usage still covers the call."""
        limit = self.limit or {}
        resets_at = limit.get("resetsAt")
        if limit.get("status") == "rejected" and isinstance(resets_at, (int, float)):
            return Interruption("usage_limit", self.reached_model, float(resets_at), self.limit)
        if exit_code == 0 and not self.result_error:
            return None
        return Interruption("transient", self.reached_model, None, self.limit)

    def to_outcome(
        self,
        exit_code: int,
        rolled_over: bool = False,
        timed_out: bool = False,
        resume_failed: bool = False,
    ) -> AgentOutcome:
        return AgentOutcome(
            session_id=self.session_id,
            exit_code=exit_code,
            cost_usd=self.cost_usd,
            context_pct=self.context_pct,
            context_window=self.window_override or self.window,
            final_text=self.final_text,
            tool_calls=list(self.tool_calls),
            permission_denials=list(self.permission_denials),
            subagents=list(self.subagents),
            compactions=self.compactions,
            rolled_over=rolled_over,
            timed_out=timed_out,
            resume_failed=resume_failed,
            interruption=self.interruption(exit_code),
        )


def _decode(line: str) -> dict | None:
    text = line.strip()
    if not text:
        return None
    try:
        event = json.loads(text)
    except ValueError:
        return None
    return event if isinstance(event, dict) else None


def _blocks(message: Any) -> list[dict]:
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return []
    return [block for block in content if isinstance(block, dict)]


def _price_row(prices: dict[str, dict[str, float]], model: str | None) -> dict[str, float] | None:
    if not model:
        return None
    if model in prices:
        return prices[model]
    return next((row for name, row in prices.items() if model.startswith(name)), None)


def estimate_cost(prices: dict[str, dict[str, float]], usage_by_message: dict[str, tuple[str, dict]]) -> float:
    """USD for the messages seen so far, from each message's `usage` and the config's price table.

    Claude reports a cost only in its final `result` event, which a session stopped at the context
    threshold or the timeout never sends, so those sessions are priced from their usage instead. A
    model with no price row adds 0."""
    total = 0.0
    for model, usage in usage_by_message.values():
        row = _price_row(prices, model)
        if row is None or not isinstance(usage, dict):
            continue
        created = usage.get("cache_creation") if isinstance(usage.get("cache_creation"), dict) else {}
        write_5m = int(created.get("ephemeral_5m_input_tokens") or 0)
        write_1h = int(created.get("ephemeral_1h_input_tokens") or 0)
        write_5m += max(0, int(usage.get("cache_creation_input_tokens") or 0) - write_5m - write_1h)
        total += (
            int(usage.get("input_tokens") or 0) * row.get("input", 0.0)
            + int(usage.get("output_tokens") or 0) * row.get("output", 0.0)
            + int(usage.get("cache_read_input_tokens") or 0) * row.get("cache_read", 0.0)
            + write_5m * row.get("cache_write_5m", 0.0)
            + write_1h * row.get("cache_write_1h", 0.0)
        ) / 1_000_000
    return total


def _usage_tokens(usage: Any) -> int | None:
    if not isinstance(usage, dict):
        return None
    keys = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
    return sum(int(usage.get(key) or 0) for key in keys)


def parse_claude_line(line: str, acc: StreamAccumulator) -> list[tuple[str, dict]]:
    """Fold one stream-json line into `acc` and return the runner events it caused."""
    acc.line_type = ""
    event = _decode(line)
    if event is None:
        return []
    kind = event.get("type")
    main_thread = event.get("parent_tool_use_id") is None
    out: list[tuple[str, dict]] = []

    if kind == "system":
        subtype = event.get("subtype")
        if subtype == "init":
            acc.session_id = event.get("session_id") or acc.session_id
            acc.model = event.get("model") or acc.model
            out.append(("agent_session", {"session_id": acc.session_id, "model": acc.model}))
        elif subtype == "compact_boundary":
            acc.compactions += 1
            acc.over_threshold = acc.compactions_spent
            meta = event.get("compact_metadata") or {}
            out.append(("compacted", {
                "session_id": acc.session_id,
                "trigger": meta.get("trigger"),
                "pre_tokens": meta.get("pre_tokens"),
            }))
        return out

    if kind == "rate_limit_event":
        info = event.get("rate_limit_info")
        acc.limit = info if isinstance(info, dict) else acc.limit
        return out

    if kind == "assistant":
        message = event.get("message") or {}
        acc.reached_model = acc.reached_model or message.get("model") != SYNTHETIC_MODEL
        last_tool = None
        started: list[tuple[str, dict]] = []
        for block in _blocks(message):
            if block.get("type") != "tool_use":
                continue
            name = block.get("name")
            tool_input = block.get("input") if isinstance(block.get("input"), dict) else {}
            last_tool = name
            acc.tool_calls.append({"name": name, "input": tool_input, "id": block.get("id")})
            if name in SUBAGENT_TOOLS:
                sub = {
                    "subagent_type": tool_input.get("subagent_type") or "general-purpose",
                    "model": tool_input.get("model") or "inherit",
                    "tool_use_id": block.get("id"),
                    "description": tool_input.get("description"),
                }
                acc.subagents.append(sub)
                acc.open_subagents[str(block.get("id"))] = sub
                started.append(("subagent_started", {
                    "parent_session": acc.session_id,
                    "subagent_type": sub["subagent_type"],
                    "model": sub["model"],
                    "tool_use_id": sub["tool_use_id"],
                }))
        if isinstance(message.get("usage"), dict) and not acc.cost_reported:
            key = str(message.get("id") or f"line-{len(acc.usage_by_message)}")  # one entry per API call
            acc.usage_by_message[key] = (str(message.get("model") or acc.model or ""), message["usage"])
            acc.cost_usd = estimate_cost(acc.prices, acc.usage_by_message)
        tokens = _usage_tokens(message.get("usage"))
        if main_thread:
            acc.line_type = "assistant"
            if tokens is not None:
                acc.context_pct = acc.context_pct_of(tokens)
                acc.over_threshold = acc.compactions_spent or (
                    acc.rollover_pct is not None and acc.context_pct >= acc.rollover_pct)
                out.append(("agent_turn", {
                    "session_id": acc.session_id,
                    "tokens": tokens,
                    "context_pct": acc.context_pct,
                    "cost": acc.cost_usd,
                    "last_tool": last_tool,
                }))
        return out + started

    if kind == "user":
        if main_thread:
            acc.line_type = "user"
        for block in _blocks(event.get("message")):
            sub = acc.open_subagents.pop(str(block.get("tool_use_id")), None) if block.get("type") == "tool_result" else None
            if sub is not None:
                out.append(("subagent_finished", {
                    "parent_session": acc.session_id,
                    "subagent_type": sub["subagent_type"],
                    "model": sub["model"],
                    "tool_use_id": sub["tool_use_id"],
                }))
        return out

    if kind == "result":
        acc.line_type = "result"
        acc.session_id = event.get("session_id") or acc.session_id
        acc.result_error = bool(event.get("is_error"))
        reported = float(event.get("total_cost_usd") or 0.0)
        if reported > 0:  # an interrupted session's result says 0: keep the estimate then
            acc.cost_usd = reported
            acc.cost_reported = True
        acc.final_text = str(event.get("result") or "")
        denials = event.get("permission_denials")
        acc.permission_denials = [d for d in denials if isinstance(d, dict)] if isinstance(denials, list) else []
        usage_by_model = event.get("modelUsage")
        if isinstance(usage_by_model, dict) and usage_by_model:
            entry = usage_by_model.get(acc.model) or next(iter(usage_by_model.values()))
            window = entry.get("contextWindow") if isinstance(entry, dict) else None
            if isinstance(window, int) and window > 0:
                acc.window = window
        return out

    return out


def parse_codex_line(line: str, acc: StreamAccumulator) -> list[tuple[str, dict]]:
    """Fold one `codex exec --json` line into `acc`. Codex reports no cost, so it stays 0."""
    acc.line_type = ""
    event = _decode(line)
    if event is None:
        return []
    kind = event.get("type")
    if kind == "thread.started":
        acc.session_id = event.get("thread_id") or acc.session_id
        return [("agent_session", {"session_id": acc.session_id, "model": acc.model})]
    if kind == "item.completed":
        item = event.get("item") if isinstance(event.get("item"), dict) else {}
        item_type = item.get("type")
        if item_type == "agent_message":
            acc.final_text = str(item.get("text") or "")
        elif item_type not in (None, "reasoning", "error"):
            name = f"{item.get('server')}.{item.get('tool')}" if item_type == "mcp_tool_call" else item_type
            acc.tool_calls.append({"name": name, "input": {k: v for k, v in item.items() if k != "id"},
                                   "id": item.get("id")})
        return []
    if kind == "turn.completed":
        acc.line_type = "assistant"
        usage = event.get("usage") if isinstance(event.get("usage"), dict) else {}
        tokens = int(usage.get("input_tokens") or 0)
        acc.context_pct = acc.context_pct_of(tokens)
        last_tool = acc.tool_calls[-1]["name"] if acc.tool_calls else None
        return [("agent_turn", {
            "session_id": acc.session_id,
            "tokens": tokens,
            "context_pct": acc.context_pct,
            "cost": 0.0,
            "last_tool": last_tool,
        })]
    return []


# --------------------------------------------------------------------------- process control


def _signal_group(proc: subprocess.Popen, sig: int) -> None:
    try:
        os.killpg(proc.pid, sig)
    except ProcessLookupError:
        return


def _terminate(proc: subprocess.Popen) -> None:
    """SIGTERM the agent's process group, SIGKILL it after the grace period."""
    if proc.poll() is not None:
        return
    _signal_group(proc, signal.SIGTERM)
    try:
        proc.wait(timeout=KILL_GRACE_S)
    except subprocess.TimeoutExpired:
        _signal_group(proc, signal.SIGKILL)


# --------------------------------------------------------------------------- adapters


class _CliAgent(abc.ABC):
    name = ""
    parse_line: Callable[[str, StreamAccumulator], list[tuple[str, dict]]]
    rollover_enabled = False

    def __init__(self, spec: AgentSpec, context: ContextPolicy) -> None:
        self.spec = spec
        self.context = context

    @abc.abstractmethod
    def argv(self, call: AgentCall) -> list[str]:
        """The full command line for `call`."""

    def env(self, call: AgentCall) -> dict[str, str]:
        window = self.context.context_window_tokens
        # Without this cap Claude reads `autocompact_pct` against the model's whole window (1M tokens for the
        # current models), so "60%" would mean 600k tokens. A number in config says which window the percentages mean.
        capped = {AUTOCOMPACT_WINDOW_ENV: str(window)} if isinstance(window, int) and not isinstance(window, bool) else {}
        return {
            **call.env,
            "AGENTFLOW_RUN": call.run_id,
            "AGENTFLOW_STEP": call.step_key,
            "AGENTFLOW_RESULT": str(call.result_path),
            **capped,
            self.context.autocompact_env: str(self.context.autocompact_pct),
        }

    def _effort_args(self, call: AgentCall) -> list[str]:
        return [part.replace("{effort}", call.effort) for part in self.spec.effort_arg]

    def _shell_env(self, call: AgentCall, env: Mapping[str, str]) -> dict[str, str]:
        """Variables added once the process environment `env` is known, for the agent's own shell tool."""
        return {}

    def _accumulator(self, call: AgentCall) -> StreamAccumulator:
        """Window precedence: a number in config, then the stream's result event, then the window the
        runner already knows for the model (`call.context_window`), then `default_window_tokens`."""
        window = self.context.context_window_tokens
        return StreamAccumulator(
            prices=self.spec.prices,
            default_window=call.context_window or self.context.default_window_tokens,
            window_override=window if isinstance(window, int) and not isinstance(window, bool) else None,
            rollover_pct=self.context.rollover_pct if self.rollover_enabled else None,
            max_compactions=self.context.max_compactions if self.rollover_enabled else None,
        )

    def run(self, call: AgentCall, on_event: EventCallback) -> AgentOutcome:
        argv = self.argv(call)
        acc = self._accumulator(call)
        call.transcript_path.parent.mkdir(parents=True, exist_ok=True)
        stderr_path = call.transcript_path.with_name(call.transcript_path.name + ".stderr")
        state = {"timed_out": False}
        rolled_over = False
        with open(call.transcript_path, "a", encoding="utf-8") as transcript, open(stderr_path, "a", encoding="utf-8") as stderr:
            env = {**(os.environ if call.env_base is None else call.env_base), **self.env(call)}
            env.update(self._shell_env(call, env))
            proc = subprocess.Popen(
                argv,
                cwd=call.cwd,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=stderr,
                text=True,
                encoding="utf-8",
                start_new_session=True,
            )

            def on_timeout() -> None:
                state["timed_out"] = True
                _terminate(proc)

            timer = threading.Timer(call.timeout_s, on_timeout)
            timer.daemon = True
            timer.start()
            on_event("agent_started", {
                "agent": self.name,
                "pid": proc.pid,
                "model": call.model,
                "effort": call.effort,
                "step": call.step_key,
                "session_id": call.resume_session,
            })
            try:
                assert proc.stdout is not None
                for line in proc.stdout:
                    transcript.write(line if line.endswith("\n") else line + "\n")
                    transcript.flush()
                    for name, payload in self.parse_line(line, acc):
                        on_event(name, payload)
                    if acc.over_threshold and acc.line_type in ("user", "result"):
                        rolled_over = True
                        _terminate(proc)
                        break
                exit_code = proc.wait()
            finally:
                timer.cancel()
                _terminate(proc)
                if proc.stdout is not None:
                    proc.stdout.close()
        timed_out = state["timed_out"]
        resume_failed = bool(call.resume_session) and exit_code != 0 and acc.session_id is None and not timed_out
        outcome = acc.to_outcome(exit_code, rolled_over=rolled_over, timed_out=timed_out, resume_failed=resume_failed)
        on_event("agent_exited", {"session_id": outcome.session_id, "exit_code": exit_code, "cost": outcome.cost_usd,
                                  "cost_estimated": not acc.cost_reported and outcome.cost_usd > 0})
        return outcome


# Commands only the runner runs (`gitops.py` does every push and `gh` call). Deny rules hold in every
# permission mode, `bypassPermissions` included, so these stay out of an agent's reach in a run. The
# `git push*` / `gh pr *` ask rules in .claude/settings.json are denied headless anyway.
RUNNER_ONLY_DENY = ("Bash(gh *)",)


class ClaudeAgent(_CliAgent):
    name = "claude"
    rollover_enabled = True
    parse_line = staticmethod(parse_claude_line)

    def argv(self, call: AgentCall) -> list[str]:
        spec = self.spec
        argv = [*spec.command, *spec.base_args]
        # `--add-dir` takes a list of values, so it goes before other options: right before the
        # prompt it would swallow the prompt as a directory.
        for directory in call.add_dirs:
            argv += ["--add-dir", str(directory)]
        if call.read_only:
            # `--tools` is an allowlist that holds in every permission mode, `bypassPermissions` included.
            # It is variadic like `--add-dir`, so it goes before the next option and never reaches the prompt.
            argv += ["--tools", ",".join(READ_ONLY_TOOLS), "--strict-mcp-config"]
        argv += ["--model", call.model, *self._effort_args(call)]
        if spec.permission_mode:
            argv += ["--permission-mode", spec.permission_mode]
        argv += ["--permission-prompts", "none"]
        # `./`: `claude` ignores the project-root form `Edit(/path)` in --settings. The guards stay the backstop.
        deny = [*(f"Edit(./{path})" for path in call.forbidden_paths), *RUNNER_ONLY_DENY]
        if call.runs_root is not None:  # `//` + an absolute path is an absolute path in a permission rule
            deny.append(f"Edit(/{call.runs_root}/**)")
        try:  # the user's own config file: a live run reloads it (`config.layer_files`), so no agent edits it
            deny.append(f"Edit(/{layers.user_config_path().parent}/**)")
        except RuntimeError:  # `Path.home()` with no home directory: there is no user file to protect
            pass
        argv += ["--settings", json.dumps({"permissions": {"deny": deny}})]
        max_turns = call.max_turns if call.max_turns is not None else spec.max_turns
        if max_turns is not None:
            argv += ["--max-turns", str(max_turns)]
        if call.agent_profile:
            argv += ["--agent", call.agent_profile]
        if call.resume_session:
            argv += ["--resume", call.resume_session]
        argv.append(call.prompt)
        return argv

    def _shell_env(self, call: AgentCall, env: Mapping[str, str]) -> dict[str, str]:
        """Pin the Bash tool's PATH to the one the gates get. Claude Code's Bash tool runs a login shell, and on
        macOS a login shell's `path_helper` moves the system folders (`/usr/local/bin`, ...) ahead of the rest, so
        the agent could run another `node` or `python` than the gates. The script is written next to the
        transcript, in the runner's state, where no agent edits. A `CLAUDE_ENV_FILE` the runner inherited is
        sourced after it, so the user's own script still runs and may still change PATH."""
        path = env.get("PATH")
        if not path:
            return {}
        lines = ["# written by agentflow: the runner's PATH, which a login shell may have reordered",
                 f"export PATH={shlex.quote(path)}"]
        own = env.get(SHELL_ENV_FILE_ENV)
        if own:
            quoted = shlex.quote(own)
            lines.append(f"if [ -f {quoted} ]; then . {quoted}; fi")
        script = call.transcript_path.with_name(call.transcript_path.name + ".env.sh")
        safeio.write_text(script, "\n".join(lines) + "\n")
        return {SHELL_ENV_FILE_ENV: str(script)}


class CodexAgent(_CliAgent):
    name = "codex"
    rollover_enabled = False
    parse_line = staticmethod(parse_codex_line)

    def argv(self, call: AgentCall) -> list[str]:
        spec = self.spec
        base = list(spec.base_args)
        if call.resume_session:
            at = base.index("exec") + 1 if "exec" in base else len(base)
            base[at:at] = ["resume", call.resume_session]
        argv = [*spec.command, *base, "-m", call.model, *self._effort_args(call)]
        if call.resume_session:
            # `codex exec resume` takes neither --profile, --cd nor --add-dir: the profile and the
            # extra writable directories (the runs dir holding $AGENTFLOW_RESULT) go through
            # config, and the working directory is the process cwd.
            if spec.profile:
                argv += ["-c", f'profile="{spec.profile}"']
            if call.add_dirs:
                roots = json.dumps([str(directory) for directory in call.add_dirs])
                argv += ["-c", f"sandbox_workspace_write.writable_roots={roots}"]
        else:
            if spec.profile:
                argv += ["-p", spec.profile]
            if call.read_only:  # a resumed session keeps its own sandbox: `codex exec resume` takes no `-s`
                argv += ["-s", "read-only"]
            argv += ["--cd", str(call.cwd)]
            for directory in call.add_dirs:
                argv += ["--add-dir", str(directory)]
        argv.append(call.prompt)
        return argv


ADAPTERS: dict[str, type[_CliAgent]] = {"claude": ClaudeAgent, "codex": CodexAgent}


def make_agents(cfg: Config) -> dict[str, Agent]:
    """One adapter per enabled agent in the config."""
    agents: dict[str, Agent] = {}
    for name, spec in cfg.agents.items():
        if not spec.enabled:
            continue
        if name not in ADAPTERS:
            raise ValueError(f"no adapter for agent {name!r}")
        agents[name] = ADAPTERS[name](spec, cfg.context)
    return agents
