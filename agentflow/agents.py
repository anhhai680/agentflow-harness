"""Agent adapters: build the argv/env for `claude -p` and `codex exec`, run them, parse their streams.

`run` starts the process with stdin closed, tees stdout to the transcript, turns stream lines
into runner events, and enforces three limits: the step timeout, the no-progress watchdog (`idle_limit`),
and the context rollover (D9).
Parsing is pure (`parse_claude_line`, `parse_codex_line`) so tests can feed recorded streams.
"""

from __future__ import annotations

import abc
import json
import math
import os
import re
import shlex
import signal
import subprocess
import threading
import time
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from . import layers, pack, safeio
from .config import AgentSpec, Config, ContextPolicy

KILL_GRACE_S = 10
WATCH_MAX_POLL_S = 5.0  # the watchdog wakes at least this often, and four times per limit when that is shorter
WATCH_MIN_POLL_S = 0.05  # ... but never more often than this, however small the limit
WATCHDOG_THREAD = "agentflow-watchdog"
SUBAGENT_TOOLS = ("Agent", "Task")
# A dispatching read-only call (`AgentCall.dispatches`) may start `general-purpose` sub-agents only: under `dontAsk` a
# sub-agent of another type would run in the permission mode its own definition sets, so `dontAsk` refuses it instead.
DISPATCH_ALLOW = tuple(f"{tool}(general-purpose)" for tool in SUBAGENT_TOOLS)
AUTOCOMPACT_WINDOW_ENV = "CLAUDE_CODE_AUTO_COMPACT_WINDOW"  # Claude Code's own cap on the window `autocompact_pct` is read against
SHELL_ENV_FILE_ENV = "CLAUDE_ENV_FILE"  # a script Claude Code sources in its Bash tool's shell before each command
SYNTHETIC_MODEL = "<synthetic>"  # Claude Code's own error messages, shown as assistant turns: no model replied

READ_ONLY_TOOLS = ("Read", "Grep", "Glob")  # the only Claude tools a read-only call gets
# A `read_only_roles` session (`AgentCall.read_only_role`): these tools exist (`Edit` and the rest don't), and under
# `dontAsk` only what `READ_ONLY_ALLOW` and the workspace rule allow runs. `Bash` is for read-only `git` and the shell's reads
# the prompt asks for, `Write` for the result file in the workspace's `out/`. A control on cost and independence, not a sandbox: the
# project's own settings and hooks still apply (plan C3, Decided 2).
REVIEW_TOOLS = ("Read", "Grep", "Glob", "Bash", "Write")
READ_ONLY_GIT = ("diff", "log", "show", "status", "blame")
READ_ONLY_ALLOW = ("Read", "Grep", "Glob",
                   *(rule for sub in READ_ONLY_GIT for rule in (f"Bash(git {sub})", f"Bash(git {sub} *)")))
READ_ONLY_DENY = ("Bash(git *--output*)",)  # `git diff --output=<file>` writes a file: no allow rule may reach it
# In a path, these could make a permission rule name more than that one directory, or end or split the rule; so could
# any control or whitespace character other than a plain space (`_rule_unsafe`).
RULE_UNSAFE = frozenset("*?[]{}\\()!#|@+,")
# The one folder of each add-dir a read-only call may write: the workspace's `out/` (`workspace.OUT_DIR`), where its
# result file goes. Never the workspace itself: `progress.md` there is read by the later steps that edit the code.
READ_ONLY_WRITES = "out"

# `agents.claude.cache_ttl`: the variable each lifetime sets. The inherited environment's other TTL variables are dropped
# (`env_drop`), so it can't override the pack; `cli` sets and drops nothing, leaving the CLI's own choice.
TTL_ENV: dict[str, dict[str, str]] = {"5m": {"FORCE_PROMPT_CACHING_5M": "1"}, "1h": {"ENABLE_PROMPT_CACHING_1H": "1"}, "cli": {}}
TTL_VARS = ("FORCE_PROMPT_CACHING_5M", "ENABLE_PROMPT_CACHING_1H", "CLAUDE_CODE_PROMPT_CACHE_TTL")
# `agents.claude.lean`: a system prefix that is the same in every worktree, only the MCP servers `base_args` names, and
# no auto-memory (a run's memory is the lessons file).
LEAN_ARGS = ("--exclude-dynamic-system-prompt-sections", "--strict-mcp-config")
LEAN_ENV = {"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"}
NO_USER_SETTINGS = ("--setting-sources", "project,local")  # `agents.claude.user_settings: false`
SELF_CONTAINED_ARGS = ("--disable-slash-commands",)  # a call whose brief is self-contained gets no skills listing
BRIEF_ARG = "--append-system-prompt-file"  # a run session's brief (`lanes.build_brief`), its cached system prompt
# Claude Code loads no `CLAUDE.md` with this set: only a call whose brief carries the repository's rules gets it, and every
# other call has it dropped from the inherited environment and the pack's `defaults.env`, so its rules are never lost.
NO_CLAUDE_MDS_ENV = "CLAUDE_CODE_DISABLE_CLAUDE_MDS"
# A result that says the CLI stopped itself at a cap (`--max-turns`, `--max-budget-usd`): its `subtype`, or else its own
# `terminal_reason`, mapped to what the runner calls the cap. Not a dead agent: the work it did stands (plan C4).
CAP_SUBTYPES = {"error_max_turns": "turns", "error_max_budget_usd": "budget"}
CAP_REASONS = {"max_turns": "turns", "budget_exhausted": "budget"}
# `agents.claude.bash_output_chars`: the CLI's cap on a shell command's output. Runner-owned like `NO_CLAUDE_MDS_ENV`:
# when the spec sets it, the pack's `defaults.env` and the inherited environment can't raise it.
BASH_OUTPUT_ENV = "BASH_MAX_OUTPUT_LENGTH"
MIN_BUDGET_USD = 0.01  # the smallest `--max-budget-usd` the CLI takes ("0.00" is refused)
UNKNOWN_OPTION = re.compile(r"unknown option '(--[\w-]+)'")  # how the claude CLI refuses a flag it does not know
CLAUDE_MIN_VERSION = "2.1.327"  # the oldest Claude Code that takes every flag above
STDERR_TAIL_BYTES = 4096  # how much of a failed call's own stderr is read for a refused flag

# An invocation's usage (`StreamAccumulator.usage_payload`): the per-model fields of a result's
# `model_usage_since_last_result`, the token classes they split into, and the classes their price splits into.
USAGE_KEYS = ("inputTokens", "outputTokens", "cacheReadInputTokens", "cacheCreationInputTokens",
              "cacheCreationEphemeral5mInputTokens", "cacheCreationEphemeral1hInputTokens")
TOKEN_CLASSES = ("input", "output", "cache_read", "cache_write_5m", "cache_write_1h")
USD_CLASSES = ("input", "output", "cache_read", "cache_write")
USAGE_WINDOWS = ("five_hour", "seven_day")  # the `rate_limit_event` windows a `usage_window` event reports
WINDOW_STEP = 0.05  # a window's utilization must move this much (or its status change) to be written again

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
    # this call's turn cap: 0 passes no `--max-turns`; None: the agent's own `max_turns`
    max_turns: int | None = None
    budget_usd: float | None = None  # this call's spend cap in list-price USD (`--max-budget-usd`); None: none
    read_only: bool = False  # Claude gets only `READ_ONLY_TOOLS` and no MCP server; Codex a read-only sandbox (fresh session)
    # a `read_only_roles` session: `dontAsk`, `REVIEW_TOOLS`, read-only git, writes only in `add_dirs`, no MCP server
    read_only_role: bool = False
    # a wave session (`Step.wave_size`), which starts one sub-agent per member: a `read_only_role` call also gets
    # `SUBAGENT_TOOLS`, allowed for `general-purpose` sub-agents only (`DISPATCH_ALLOW`). Nothing else changes.
    dispatches: bool = False
    idle_s: float | None = None  # end the session after this long with no stream line (`watchdog`); None: no watchdog
    tool_idle_s: float | None = None  # the same limit while a tool call is open, or for an agent that `tracks_tools` not
    self_contained: bool = False  # the prompt is the whole brief (a sizer): Claude gets no skills listing
    system_prompt_file: Path | None = None  # the session's pinned run brief, appended to Claude's system prompt
    carries_rules: bool = False  # that brief holds the repository's rules: Claude loads no `CLAUDE.md` of its own
    # The run's last recorded usage windows (`state.usage_window`): only a window that moved since is written again.
    usage_windows: dict = field(default_factory=dict)
    # Called with a copy of the stream's last window readings (`StreamAccumulator.last_readings`) whenever a
    # `rate_limit_event` changed them. A failure in it never reaches the call (the stream loop catches it).
    on_reading: Callable[[dict], None] | None = None


@dataclass
class Interruption:
    """Why a call ended without finishing. `usage_limit` carries the reset time (`until`, epoch seconds);
    `cli_flags` is a CLI that refused a flag before it started (`detail` names the flag), which no retry fixes;
    `transient` is everything else, unknown failures included. `reached_model` tells an outage, where a
    retry is free, from a call that spent money and still died. `limit` is the stream's last limit info."""

    kind: str
    reached_model: bool
    until: float | None = None
    limit: dict | None = None
    detail: str | None = None


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
    stalled: bool = False  # the watchdog ended the session: no stream line for `idle_limit_s` (never with `timed_out`)
    idle_limit_s: float | None = None  # the limit that was in force when the watchdog fired
    usage: dict = field(default_factory=dict)  # this invocation's split (`StreamAccumulator.usage_payload`)
    capped: str | None = None  # `turns` or `budget`: the CLI stopped itself at that cap (`CAP_SUBTYPES`); None: it didn't
    # This invocation's first and last window reading, `{window: {utilization, resets_at, refused}}` (`window_readings`)
    first_readings: dict = field(default_factory=dict)
    last_readings: dict = field(default_factory=dict)


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
    # Per open sub-agent call id, the usage of the `assistant` lines that carry it as `parent_tool_use_id`, in the shape of
    # `usage_by_message`. Only ids of calls that started and haven't finished are kept (`track_subagent_usage`).
    subagent_usage: dict[str, dict[str, tuple[str, dict]]] = field(default_factory=dict)
    open_tools: set[str] = field(default_factory=set)  # main-thread `tool_use` ids with no `tool_result` yet
    limit: dict | None = None  # the latest `rate_limit_event`'s `rate_limit_info`
    reached_model: bool = False
    result_error: bool = False
    # This invocation's usage (`usage_payload`). A `result` event's `modelUsage` is the session's total over every
    # `--resume`, so only its `model_usage_since_last_result` is summed, over every result the invocation sends.
    result_usage: dict[str, dict] = field(default_factory=dict)  # per model, camel case as Claude writes it
    results_seen: int = 0
    result_turns: int = 0
    main_calls: int = 0  # main-thread assistant messages with usage, once per message id
    main_ids: set[str] = field(default_factory=set)
    prefix_write: int | None = None  # the first main-thread message's `cache_creation_input_tokens`
    tool_output_chars: int = 0  # the text of main-thread `tool_result`s
    capped: str | None = None  # `turns` | `budget`: the first result of the invocation that says a cap ended it
    terminal_reason: str | None = None  # the first result's that has one: a trailing `api_error` hides nothing
    auto_memory: bool | None = None  # the `init` event named memory paths; None: no `init` seen
    windows: dict[str, tuple[float, str]] = field(default_factory=dict)  # the last emitted utilization and status
    # Every window reading the stream carried (`window_readings`), apart from `windows`: that one only dedupes events
    first_readings: dict[str, dict] = field(default_factory=dict)
    last_readings: dict[str, dict] = field(default_factory=dict)

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
        if self.capped:  # a cap is an end the runner asked for: the exit code and `is_error` say nothing more
            return None
        if exit_code == 0 and not self.result_error:
            return None
        return Interruption("transient", self.reached_model, None, self.limit)

    def usage_payload(self) -> dict:
        """This invocation's tokens, their price, its turns and its prefix write. The results' own usage when they
        reported any; else the messages' usage (a killed session, an old CLI, Codex), marked `estimated`."""
        measured = any(usage_split(self.result_usage).values())
        model_usage = self.result_usage if measured else _message_usage(self.usage_by_message)
        return {
            "tokens": usage_split(model_usage),
            "usd": price_split(self.prices, model_usage),
            "turns": self.result_turns if measured else self.main_calls,
            "terminal_reason": self.terminal_reason,
            "prefix_write": self.prefix_write,
            "tool_output_chars": self.tool_output_chars,
            "auto_memory": self.auto_memory,
            "estimated": not measured,
        }

    def to_outcome(
        self,
        exit_code: int,
        rolled_over: bool = False,
        timed_out: bool = False,
        resume_failed: bool = False,
        stalled: bool = False,
        idle_limit_s: float | None = None,
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
            stalled=stalled,
            idle_limit_s=idle_limit_s if stalled else None,
            usage=self.usage_payload(),
            capped=self.capped,
            first_readings={name: dict(reading) for name, reading in self.first_readings.items()},
            last_readings={name: dict(reading) for name, reading in self.last_readings.items()},
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


def _number(value: Any) -> float | None:
    """A finite number from the stream (never a bool), else None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)  # an int past the float range raises OverflowError
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def _count(value: Any) -> int:
    """A token count from the stream: a non-negative whole number, else 0."""
    number = _number(value)
    return max(0, int(number)) if number is not None else 0


def _camel(usage: Mapping[str, Any]) -> dict[str, int]:
    """One message's snake-case `usage` in the camel-case shape of a result's per-model usage."""
    created = usage.get("cache_creation") if isinstance(usage.get("cache_creation"), dict) else {}
    return {
        "inputTokens": _count(usage.get("input_tokens")),
        "outputTokens": _count(usage.get("output_tokens")),
        "cacheReadInputTokens": _count(usage.get("cache_read_input_tokens")),
        "cacheCreationInputTokens": _count(usage.get("cache_creation_input_tokens")),
        "cacheCreationEphemeral5mInputTokens": _count(created.get("ephemeral_5m_input_tokens")),
        "cacheCreationEphemeral1hInputTokens": _count(created.get("ephemeral_1h_input_tokens")),
    }


def _add_usage(into: dict[str, dict], model: str, entry: Mapping[str, Any]) -> None:
    row = into.setdefault(model, dict.fromkeys(USAGE_KEYS, 0))
    for key in USAGE_KEYS:
        row[key] += _count(entry.get(key))


def _message_usage(usage_by_message: dict[str, tuple[str, dict]]) -> dict[str, dict]:
    """The messages' usage summed per model, in the camel-case shape: the estimate when no result reported any."""
    out: dict[str, dict] = {}
    for model, usage in usage_by_message.values():
        if isinstance(usage, dict):
            _add_usage(out, model, _camel(usage))
    return out


def _entry_split(entry: Mapping[str, Any]) -> dict[str, int]:
    """One model's tokens by class. Creation the two TTL fields don't cover is a 5-minute write."""
    write_5m = _count(entry.get("cacheCreationEphemeral5mInputTokens"))
    write_1h = _count(entry.get("cacheCreationEphemeral1hInputTokens"))
    write_5m += max(0, _count(entry.get("cacheCreationInputTokens")) - write_5m - write_1h)
    return {
        "input": _count(entry.get("inputTokens")),
        "output": _count(entry.get("outputTokens")),
        "cache_read": _count(entry.get("cacheReadInputTokens")),
        "cache_write_5m": write_5m,
        "cache_write_1h": write_1h,
    }


def _usd(row: Mapping[str, float], split: Mapping[str, int]) -> dict[str, float]:
    return {
        "input": split["input"] * row.get("input", 0.0) / 1_000_000,
        "output": split["output"] * row.get("output", 0.0) / 1_000_000,
        "cache_read": split["cache_read"] * row.get("cache_read", 0.0) / 1_000_000,
        "cache_write": (split["cache_write_5m"] * row.get("cache_write_5m", 0.0)
                        + split["cache_write_1h"] * row.get("cache_write_1h", 0.0)) / 1_000_000,
    }


def usage_split(model_usage: Mapping[str, Any]) -> dict[str, int]:
    """Tokens by class (`TOKEN_CLASSES`), summed over the models of a camel-case per-model usage dict."""
    total = dict.fromkeys(TOKEN_CLASSES, 0)
    for entry in model_usage.values():
        if isinstance(entry, dict):
            for key, tokens in _entry_split(entry).items():
                total[key] += tokens
    return total


def price_split(prices: dict[str, dict[str, float]], model_usage: Mapping[str, Any]) -> dict[str, float]:
    """USD by class (`USD_CLASSES`) for a camel-case per-model usage dict, rounded to 6 places. An unpriced model adds 0."""
    total = dict.fromkeys(USD_CLASSES, 0.0)
    for model, entry in model_usage.items():
        row = _price_row(prices, str(model))
        if row is None or not isinstance(entry, dict):
            continue
        for key, usd in _usd(row, _entry_split(entry)).items():
            total[key] += usd
    return {key: round(usd, 6) for key, usd in total.items()}


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
        total += sum(_usd(row, _entry_split(_camel(usage))).values())
    return total


def ignored_settings(spec: AgentSpec, usage: dict) -> list[str]:
    """The settings of `spec` an invocation's `usage` shows the CLI did not honour: `cache_ttl` when the writes landed
    in the other lifetime, `auto_memory` when a lean session still loaded the user's memory. Reads `usage` only through
    `.get`: an empty one (a Codex call, a dry run) gives `[]`."""
    tokens = usage.get("tokens") or {}
    write_5m, write_1h = tokens.get("cache_write_5m") or 0, tokens.get("cache_write_1h") or 0
    out = []
    if (spec.cache_ttl == "5m" and write_1h > 0) or (spec.cache_ttl == "1h" and write_5m > 0 and not write_1h):
        out.append("cache_ttl")
    if spec.lean and usage.get("auto_memory"):
        out.append("auto_memory")
    return out


def _seed_windows(usage_windows: Any) -> dict[str, tuple[float, str]]:
    """The run's recorded windows (`state.usage_window`: `{window: {"utilization", "status", ...}}`) as
    `StreamAccumulator.windows`. An entry with no number is left out, so its next reading is news."""
    out: dict[str, tuple[float, str]] = {}
    if not isinstance(usage_windows, Mapping):
        return out
    for name, entry in usage_windows.items():
        utilization = _number(entry.get("utilization")) if isinstance(entry, Mapping) else None
        if utilization is not None:
            out[str(name)] = (utilization, str(entry.get("status") or ""))
    return out


def _window_news(acc: StreamAccumulator, unified: Mapping[str, Any]) -> list[tuple[str, dict]]:
    """A `usage_window` event for each window that is new to `acc.windows`, moved `WINDOW_STEP` or more since the
    last one written, or changed status. Readings in between are not news, so the last emitted value stays."""
    out: list[tuple[str, dict]] = []
    for name in USAGE_WINDOWS:
        entry = unified.get(name)
        utilization = _number(entry.get("utilization")) if isinstance(entry, dict) else None
        if utilization is None:
            continue
        status = str(entry.get("status") or "")
        last = acc.windows.get(name)
        if last is not None and round(abs(utilization - last[0]), 6) < WINDOW_STEP and status == last[1]:
            continue
        acc.windows[name] = (utilization, status)
        resets_at = _number(entry.get("resetsAt"))
        out.append(("usage_window", {"window": name, "utilization": utilization,
                                     "resets_at": int(resets_at) if resets_at is not None else None, "status": status}))
    return out


def window_readings(info: Mapping[str, Any]) -> dict[str, dict]:
    """The readings a `rate_limit_event`'s `rate_limit_info` carries, `{window: {utilization, resets_at, refused}}`.
    A window counts only with a number and a `resetsAt`. `refused` is True when the top-level `status` is `rejected`
    and `rateLimitType` names the window: a window's own `status` says nothing, because extra usage can still cover
    a call. A top-level rejection of a window that has no usable entry is read from the top-level fields (utilization
    1.0, its `resetsAt`): no real rejection has been seen yet, so this shape is a guess. A `rateLimitType` outside
    `USAGE_WINDOWS` is not a reading."""
    out: dict[str, dict] = {}
    unified = info.get("unifiedWindows")
    rejected = info.get("status") == "rejected"
    for name in USAGE_WINDOWS:
        entry = unified.get(name) if isinstance(unified, dict) else None
        utilization = _number(entry.get("utilization")) if isinstance(entry, dict) else None
        resets_at = _number(entry.get("resetsAt")) if isinstance(entry, dict) else None
        refused = rejected and info.get("rateLimitType") == name
        if utilization is None or resets_at is None:
            top = _number(info.get("resetsAt"))
            if not (refused and top is not None and not isinstance(entry, dict)):
                continue
            utilization, resets_at = 1.0, top
        out[name] = {"utilization": utilization, "resets_at": int(resets_at), "refused": refused}
    return out


def _text_length(content: Any) -> int:
    """The text in a `tool_result`'s content: a string, or a list of blocks whose `text` counts."""
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        return sum(len(block["text"]) for block in content if isinstance(block, dict) and isinstance(block.get("text"), str))
    return 0


def _usage_tokens(usage: Any) -> int | None:
    if not isinstance(usage, dict):
        return None
    keys = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
    return sum(int(usage.get(key) or 0) for key in keys)


SUBAGENT_DESCRIPTION_MAX = 200


def subagent_description(tool_input: dict) -> str:
    """A sub-agent call's `description`, cut to `SUBAGENT_DESCRIPTION_MAX` characters; `""` when it is missing or not a
    string. Model text, stored as it came: whoever shows it to a person cleans it first."""
    description = tool_input.get("description")
    return description[:SUBAGENT_DESCRIPTION_MAX] if isinstance(description, str) else ""


# The cost of one sub-agent: a separable unit, so it can be dropped if the CLI's stream doesn't tag a sub-agent's own lines
# with `parent_tool_use_id`. `start_subagent_usage` and `track_subagent_usage` fold the lines in, `subagent_cost` reads it.


def start_subagent_usage(acc: StreamAccumulator, call_id: str) -> None:
    """Begin tracking the lines of the sub-agent call `call_id`. Only a call that started is ever tracked."""
    acc.subagent_usage.setdefault(call_id, {})


def track_subagent_usage(acc: StreamAccumulator, parent: Any, key: str, model: str, usage: dict) -> None:
    """Keep one `assistant` line's usage for the sub-agent call `parent` names. A line naming a call that isn't open
    (unknown, finished, or not a string) is ignored, so no other call's cost grows from it."""
    if isinstance(parent, str) and parent in acc.subagent_usage:
        acc.subagent_usage[parent][key] = (model, usage)


def subagent_cost(acc: StreamAccumulator, call_id: str) -> float:
    """USD for the call `call_id`'s own lines, priced as the session is; stops tracking the call."""
    return round(estimate_cost(acc.prices, acc.subagent_usage.pop(call_id, {})), 6)


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
            acc.auto_memory = bool(event.get("memory_paths"))  # null when auto-memory is off
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
        unified = info.get("unifiedWindows") if isinstance(info, dict) else None
        if isinstance(unified, dict):
            out.extend(_window_news(acc, unified))
        if isinstance(info, dict):
            for name, reading in window_readings(info).items():
                acc.first_readings.setdefault(name, reading)
                acc.last_readings[name] = reading
        return out

    if kind == "assistant":
        message = event.get("message")
        message = message if isinstance(message, dict) else {}
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
            if main_thread:  # a subagent's own calls run inside the main thread's open `Agent` call
                acc.open_tools.add(str(block.get("id")))
            if name in SUBAGENT_TOOLS:
                sub = {
                    "subagent_type": tool_input.get("subagent_type") or "general-purpose",
                    "model": tool_input.get("model") or "inherit",
                    "tool_use_id": block.get("id"),
                    "description": subagent_description(tool_input),
                    "background": bool(tool_input.get("run_in_background")),  # fail closed: any truthy value counts
                }
                acc.subagents.append(sub)
                acc.open_subagents[str(block.get("id"))] = sub
                start_subagent_usage(acc, str(block.get("id")))
                started.append(("subagent_started", {
                    "parent_session": acc.session_id,
                    "subagent_type": sub["subagent_type"],
                    "model": sub["model"],
                    "tool_use_id": sub["tool_use_id"],
                    "description": sub["description"],
                    "background": sub["background"],
                }))
        usage = message.get("usage")
        if isinstance(usage, dict):
            key = str(message.get("id") or f"line-{len(acc.usage_by_message)}")  # one entry per API call
            acc.usage_by_message[key] = (str(message.get("model") or acc.model or ""), usage)
            track_subagent_usage(acc, event.get("parent_tool_use_id"), key, acc.usage_by_message[key][0], usage)
            if not acc.cost_reported:  # a reported cost is never replaced by the estimate
                acc.cost_usd = estimate_cost(acc.prices, acc.usage_by_message)
            if main_thread and message.get("model") != SYNTHETIC_MODEL and key not in acc.main_ids:
                acc.main_ids.add(key)
                acc.main_calls += 1
                if acc.prefix_write is None:
                    acc.prefix_write = _count(usage.get("cache_creation_input_tokens"))
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
            if main_thread and block.get("type") == "tool_result":
                acc.open_tools.discard(str(block.get("tool_use_id")))
                acc.tool_output_chars += _text_length(block.get("content"))
            sub = acc.open_subagents.pop(str(block.get("tool_use_id")), None) if block.get("type") == "tool_result" else None
            if sub is not None:
                out.append(("subagent_finished", {
                    "parent_session": acc.session_id,
                    "subagent_type": sub["subagent_type"],
                    "model": sub["model"],
                    "tool_use_id": sub["tool_use_id"],
                    "description": sub["description"],
                    "background": sub["background"],
                    "cost_usd": subagent_cost(acc, str(sub["tool_use_id"])),
                    "ok": not bool(block.get("is_error")),  # a call the CLI refused comes back as an error result
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
        acc.results_seen += 1
        acc.result_turns += _count(event.get("num_turns"))
        since = event.get("model_usage_since_last_result")  # never `modelUsage`: that is the session's total
        if isinstance(since, dict):
            for name, entry in since.items():
                if isinstance(entry, dict):
                    _add_usage(acc.result_usage, str(name), entry)
        reason = event.get("terminal_reason")
        if acc.capped is None:  # set once: a trailing result doesn't undo it
            subtype = event.get("subtype")
            acc.capped = (CAP_SUBTYPES.get(subtype) if isinstance(subtype, str) else None) or \
                (CAP_REASONS.get(reason) if isinstance(reason, str) else None)
        if acc.terminal_reason is None and isinstance(reason, str) and reason:
            acc.terminal_reason = reason
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


def idle_limit(call: AgentCall, acc: StreamAccumulator, tracks_tools: bool) -> float | None:
    """How long the stream may stay quiet before the watchdog ends the session; None: no watchdog. While a
    main-thread tool call is open (a long test run writes nothing until it returns), or for an agent whose
    stream does not show open tool calls, the limit is the longer `tool_idle_s`."""
    if call.idle_s is None:
        return None
    if acc.open_tools or not tracks_tools:
        return call.tool_idle_s if call.tool_idle_s is not None else call.idle_s
    return call.idle_s


def _stderr_tail(handle: Any, start: int) -> str:
    """What this call wrote to its stderr file: at most the last `STDERR_TAIL_BYTES` past `start`, read from the
    runner's own open handle, so an earlier call's output in the same file is never this call's."""
    end = handle.seek(0, os.SEEK_END)
    handle.seek(max(start, end - STDERR_TAIL_BYTES))
    return handle.read(STDERR_TAIL_BYTES).decode("utf-8", errors="replace")


def refused_flag(stderr_tail: str) -> str | None:
    """The flag the CLI refused as unknown, or None."""
    found = UNKNOWN_OPTION.search(stderr_tail)
    return found.group(1) if found else None


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
    tracks_tools = False  # the stream shows open tool calls (`StreamAccumulator.open_tools`); else `idle_limit` is the long one
    takes_brief = False  # passes `AgentCall.system_prompt_file` on (`lanes.build_brief`); else a call gets no brief

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

    def env_drop(self, call: AgentCall) -> tuple[str, ...]:
        """Names `run` removes from the inherited environment before it adds `env(call)`."""
        return ()

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
            windows=_seed_windows(call.usage_windows),
        )

    def run(self, call: AgentCall, on_event: EventCallback) -> AgentOutcome:
        argv = self.argv(call)
        acc = self._accumulator(call)
        call.transcript_path.parent.mkdir(parents=True, exist_ok=True)
        stderr_path = call.transcript_path.with_name(call.transcript_path.name + ".stderr")
        # The step timeout (`timer`) and the watchdog (`watcher`) each end the session for their own reason, and only
        # the first to fire does: `claim` sets its flag under `lock`, so `timed_out` and `stalled` are never both true.
        lock = threading.Lock()
        state: dict[str, Any] = {"timed_out": False, "stalled": False, "limit": None, "last_line": time.monotonic()}
        # Set under `lock` (`end_stream`) once the stream has ended (its `result` line, EOF, or a rollover), and again by
        # the `finally` below: the watcher returns, and `claim("stalled")` refuses from then on. After the stream ends
        # the runner waits in `proc.wait()`, where `poll()` from another thread says None even for an exited process,
        # so a CLI that lingers on shutdown is the step timeout's to end, never a stall.
        done = threading.Event()

        def claim(flag: str, limit: float | None = None) -> bool:
            with lock:
                if state["timed_out"] or state["stalled"] or (flag == "stalled" and done.is_set()):
                    return False
                state[flag] = True
                state["limit"] = limit
                return True

        def end_stream() -> None:
            with lock:  # so a watcher that has not claimed yet never claims after the stream ended
                done.set()

        rolled_over = False
        # The stderr file appends across a step's calls: `stderr_start` is where this call's output begins.
        with open(call.transcript_path, "a", encoding="utf-8") as transcript, open(stderr_path, "a+b") as stderr:
            stderr_start = stderr.seek(0, os.SEEK_END)
            inherited = dict(os.environ if call.env_base is None else call.env_base)
            for name in self.env_drop(call):
                inherited.pop(name, None)
            env = {**inherited, **self.env(call)}
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
                if claim("timed_out"):
                    _terminate(proc)

            def watch() -> None:
                """End the session once its stream has been quiet for longer than `idle_limit`. The limit is read
                again on every wake, since it changes as tool calls open and close."""
                while True:
                    limit = idle_limit(call, acc, self.tracks_tools)
                    if limit is None or done.wait(max(WATCH_MIN_POLL_S, min(WATCH_MAX_POLL_S, limit / 4))):
                        return
                    if proc.poll() is not None:  # it has exited: nothing to end, and its pid may be reused
                        return
                    if time.monotonic() - state["last_line"] > limit and claim("stalled", limit):
                        _terminate(proc)
                        return

            timer = threading.Timer(call.timeout_s, on_timeout)
            timer.daemon = True
            timer.start()
            watcher: threading.Thread | None = None
            on_event("agent_started", {
                "agent": self.name,
                "pid": proc.pid,
                "model": call.model,
                "effort": call.effort,
                "step": call.step_key,
                "session_id": call.resume_session,
            })
            try:
                if idle_limit(call, acc, self.tracks_tools) is not None:
                    thread = threading.Thread(target=watch, name=WATCHDOG_THREAD, daemon=True)
                    thread.start()
                    watcher = thread  # only a started thread is joined
                assert proc.stdout is not None
                reported: dict[str, dict] = {}  # the readings `call.on_reading` has been given
                for line in proc.stdout:
                    state["last_line"] = time.monotonic()
                    transcript.write(line if line.endswith("\n") else line + "\n")
                    transcript.flush()
                    for name, payload in self.parse_line(line, acc):
                        on_event(name, payload)
                    if call.on_reading is not None and acc.last_readings != reported:
                        reported = {window: dict(reading) for window, reading in acc.last_readings.items()}
                        try:  # a failure here never reaches the session: the readings are a scheduling aid
                            call.on_reading({window: dict(reading) for window, reading in reported.items()})
                        except Exception:  # noqa: BLE001
                            pass
                    if acc.line_type == "result":
                        end_stream()
                    if acc.over_threshold and acc.line_type in ("user", "result"):
                        rolled_over = True
                        end_stream()
                        _terminate(proc)
                        break
                end_stream()  # EOF
                exit_code = proc.wait()
            finally:
                timer.cancel()
                end_stream()
                try:
                    if watcher is not None:
                        watcher.join()  # bounded: a watcher that fired is at most inside `_terminate`'s grace period
                finally:  # the child is always ended, whatever failed above
                    _terminate(proc)
                    if proc.stdout is not None:
                        proc.stdout.close()
            # A CLI that refuses a flag exits before its `init` line, with the reason on stderr.
            tail = _stderr_tail(stderr, stderr_start) if exit_code != 0 and acc.session_id is None else ""
        # A timer callback that started but has not claimed yet may still claim after this read. That is harmless:
        # the process has exited by now, so its `_terminate` returns at once (`poll()` is set).
        with lock:
            timed_out, stalled, limit = state["timed_out"], state["stalled"], state["limit"]
        refused = None if timed_out or stalled else refused_flag(tail)
        resume_failed = (bool(call.resume_session) and exit_code != 0 and acc.session_id is None
                         and not timed_out and not stalled and refused is None)
        outcome = acc.to_outcome(exit_code, rolled_over=rolled_over, timed_out=timed_out, resume_failed=resume_failed,
                                 stalled=stalled, idle_limit_s=limit)
        if refused is not None:  # no retry fixes it, and no session was there to resume
            outcome.interruption = Interruption("cli_flags", reached_model=False, detail=refused)
        on_event("agent_exited", {"session_id": outcome.session_id, "exit_code": exit_code, "cost": outcome.cost_usd,
                                  "cost_estimated": not acc.cost_reported and outcome.cost_usd > 0,
                                  "usage": outcome.usage})
        return outcome


# Commands only the runner runs (`gitops.py` does every push and `gh` call). Deny rules hold in every
# permission mode, `bypassPermissions` included, so these stay out of an agent's reach in a run. The
# `git push*` / `gh pr *` ask rules in .claude/settings.json are denied headless anyway.
RUNNER_ONLY_DENY = ("Bash(gh *)",)


def _without_mcp_config(args: list[str]) -> list[str]:
    """`args` with every `--mcp-config` and its values dropped (`--mcp-config=<v>` too): `--strict-mcp-config` still
    loads the servers it names. It takes a list, so the values run up to the next option."""
    out: list[str] = []
    dropping = False
    for arg in args:
        if arg == "--mcp-config" or arg.startswith("--mcp-config="):
            dropping = True
        elif dropping and not arg.startswith("-"):
            continue
        else:
            dropping = False
            out.append(arg)
    return out


def _rule_unsafe(path: str) -> bool:
    """`path` holds a `RULE_UNSAFE` character, or a control, format or whitespace character other than a plain space."""
    return bool(RULE_UNSAFE & set(path)) or any(
        char != " " and (char.isspace() or unicodedata.category(char).startswith("C")) for char in path)


def read_only_allow(add_dirs: list[Path]) -> list[str]:
    """The allow rules of a `read_only_role` call: `READ_ONLY_ALLOW`, and writes in the `READ_ONLY_WRITES` folder of each
    of `add_dirs` (and nowhere else). `//` + an absolute path is an absolute path in a permission rule. A path a rule
    can't name exactly (`_rule_unsafe`) raises `ValueError`: the caller runs the step as it would otherwise."""
    for directory in add_dirs:
        if _rule_unsafe(str(directory)):
            raise ValueError(f"a read-only role can't allow writes in {str(directory)!r}: the path holds a permission-rule character")
    return [*READ_ONLY_ALLOW, *(f"Edit(/{directory}/{READ_ONLY_WRITES}/**)" for directory in add_dirs)]


def _role_tools(call: AgentCall) -> tuple[str, ...]:
    """The tools of a `read_only_role` call: `REVIEW_TOOLS`, then `SUBAGENT_TOOLS` for a wave session that dispatches."""
    return (*REVIEW_TOOLS, *SUBAGENT_TOOLS) if call.dispatches else REVIEW_TOOLS


def _carries_rules(call: AgentCall) -> bool:
    """The call passes a brief that holds the repository's rules."""
    return call.carries_rules and call.system_prompt_file is not None


class ClaudeAgent(_CliAgent):
    name = "claude"
    rollover_enabled = True
    tracks_tools = True
    takes_brief = True
    takes_read_only_role = True  # `AgentCall.read_only_role` changes this agent's argv (`CodexAgent` has no such flag)
    parse_line = staticmethod(parse_claude_line)

    def argv(self, call: AgentCall) -> list[str]:
        spec = self.spec
        locked = call.read_only or call.read_only_role  # no MCP server in either
        argv = [*spec.command, *(_without_mcp_config(spec.base_args) if locked else spec.base_args)]
        # `--add-dir` takes a list of values, so it goes before other options: right before the
        # prompt it would swallow the prompt as a directory.
        for directory in call.add_dirs:
            argv += ["--add-dir", str(directory)]
        if locked:
            # `--tools` is an allowlist that holds in every permission mode, `bypassPermissions` included.
            # It is variadic like `--add-dir`, so it goes before the next option and never reaches the prompt.
            argv += ["--tools", ",".join(_role_tools(call) if call.read_only_role else READ_ONLY_TOOLS)]
        # `--strict-mcp-config` once: lean keeps only the servers `base_args` names, a read-only call none at all
        if spec.lean:
            argv += LEAN_ARGS
        elif locked:
            argv.append("--strict-mcp-config")
        if not spec.user_settings:
            argv += NO_USER_SETTINGS
        if call.self_contained:
            argv += SELF_CONTAINED_ARGS
        argv += ["--model", call.model, *self._effort_args(call)]
        if call.read_only_role:  # whatever the pack sets: a role call runs only what the allow list below names
            argv += ["--permission-mode", "dontAsk"]
        elif spec.permission_mode:
            argv += ["--permission-mode", spec.permission_mode]
        argv += ["--permission-prompts", "none"]
        # `./`: `claude` ignores the project-root form `Edit(/path)` in --settings. The guards stay the backstop.
        deny = [*(f"Edit(./{path})" for path in call.forbidden_paths), *RUNNER_ONLY_DENY]
        if call.runs_root is not None:  # `//` + an absolute path is an absolute path in a permission rule
            deny.append(f"Edit(/{call.runs_root}/**)")
        try:  # the user's own config file: a live run reloads it (`config.layer_files`), so no agent edits it
            deny.append(f"Edit(/{layers.user_config_path().parent}/**)")
            # the machine-wide usage window file, its lock and its temp files: a scheduling aid no agent writes
            deny.append(f"Edit(/{pack.user_state_root()}/usage_window.*)")
        except RuntimeError:  # `Path.home()` with no home directory: there is no user file to protect
            pass
        permissions: dict = {"deny": deny}
        if call.read_only_role:  # deny rules win over the allow rules, so `git diff --output=` stays out of reach
            permissions = {"allow": [*read_only_allow(call.add_dirs), *(DISPATCH_ALLOW if call.dispatches else ())],
                           "deny": [*deny, *READ_ONLY_DENY]}
        argv += ["--settings", json.dumps({"permissions": permissions})]
        if call.system_prompt_file is not None:
            argv += [BRIEF_ARG, str(call.system_prompt_file)]
        max_turns = call.max_turns if call.max_turns is not None else spec.max_turns
        if max_turns:  # 0: no cap
            argv += ["--max-turns", str(max_turns)]
        budget = call.budget_usd
        if isinstance(budget, (int, float)) and not isinstance(budget, bool) and math.isfinite(budget) \
                and budget >= MIN_BUDGET_USD:
            argv += ["--max-budget-usd", f"{budget:.2f}"]
        if call.agent_profile:
            argv += ["--agent", call.agent_profile]
        if call.resume_session:
            argv += ["--resume", call.resume_session]
        argv.append(call.prompt)
        return argv

    def env(self, call: AgentCall) -> dict[str, str]:
        """`cache_ttl` other than `cli` owns the TTL variables: the pack's `defaults.env` can't set another one. The
        runner owns `NO_CLAUDE_MDS_ENV`: `1` when the call's brief carries the rules, else never set. It owns
        `BASH_OUTPUT_ENV` too when `bash_output_chars` is above 0, set last so `defaults.env` can't override it."""
        spec = self.spec
        env = super().env(call)
        if spec.cache_ttl in TTL_ENV and spec.cache_ttl != "cli":
            env = {name: value for name, value in env.items() if name not in TTL_VARS}
        env.pop(NO_CLAUDE_MDS_ENV, None)
        rules = {NO_CLAUDE_MDS_ENV: "1"} if _carries_rules(call) else {}
        bash = {BASH_OUTPUT_ENV: str(spec.bash_output_chars)} if spec.bash_output_chars > 0 else {}
        return {**env, **TTL_ENV.get(spec.cache_ttl, {}), **(LEAN_ENV if spec.lean else {}), **rules, **bash}

    def env_drop(self, call: AgentCall) -> tuple[str, ...]:
        """The TTL variables `cache_ttl` doesn't set, so the inherited environment can't override it (none for `cli`),
        `NO_CLAUDE_MDS_ENV` unless the call's brief carries the rules (Claude then loads its own memory files), and
        `BASH_OUTPUT_ENV` when the spec sets it, so the inherited copy can't stand."""
        rules = (*(() if _carries_rules(call) else (NO_CLAUDE_MDS_ENV,)),
                 *((BASH_OUTPUT_ENV,) if self.spec.bash_output_chars > 0 else ()))
        if self.spec.cache_ttl not in TTL_ENV or self.spec.cache_ttl == "cli":
            return rules
        return (*(name for name in TTL_VARS if name not in TTL_ENV[self.spec.cache_ttl]), *rules)

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
    tracks_tools = False
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
