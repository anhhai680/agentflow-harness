# agentflow cost, slice C0+C1 (measure and cache) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every Claude run session records how its tokens split between cache writes, cache reads and output, and the usage window it saw. It also starts on a 5-minute cache with a lean, stable prefix that no longer loads the developer's personal Claude config.

**Architecture:**
- **Capture:** the Claude adapter (`agents.py`) folds the stream's `result` usage and `rate_limit_event` windows into a `usage` payload on `agent_exited` and a new `usage_window` event.
- **Read-only passes:** `state.py`, `metrics.py` and `signals.py` read those events and store nothing of their own. The Metrics tab gets a "Where the tokens go" table.
- **Session start:** three new `agents.claude` keys (`cache_ttl`, `lean`, `user_settings`) change only the argv and env the adapter builds.
- **Checking the CLI:** the runner verifies the undocumented settings took effect, from the stream. It stops, with a named cause, when the CLI refuses a flag.

**Tech Stack:** Python 3.12 stdlib + PyYAML; pytest with `tests/fake_agent.py`; plain JS in `dashboard_ui/` tested with node.

**Spec:** `docs/specs/agentflow-cost-proposal.md`: §3.1 (L1), §3.2 L2a and L2b, §4, §7 row "C0 + C1", the Decided table (Q1, Q2, Q9) and its "Amended for the C0+C1 plan" notes.

## Global Constraints

- Engine dependencies stay stdlib + `pyyaml>=6`. There are no project strings in engine files (`test_portability.py`).
- The golden files `agentflow/tests/fixtures/explain/*.txt` stay byte-identical.
- **Config keys:** every new key ships with a Settings form field or a `_PROTECTED_KEYS` entry, the `validate` checks, a `test_config_edit.py` case and a playbook §14 line, all in the same task.
- **Changelog:** every task with a user-visible change adds one line under `## [Unreleased]` in `CHANGELOG.md`.
- **Defaults** (Claude only; Codex ignores them):
  - `agents.claude.cache_ttl: 5m` (`5m | 1h | cli`);
  - `agents.claude.lean: true`;
  - `agents.claude.user_settings: false`.
- **The env and flags these keys set:**
  - `5m` → `FORCE_PROMPT_CACHING_5M=1`;
  - `1h` → `ENABLE_PROMPT_CACHING_1H=1`;
  - `lean` → `--exclude-dynamic-system-prompt-sections`, `--strict-mcp-config` and `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`;
  - `user_settings: false` → `--setting-sources project,local`.
- **The minimum Claude Code version** for these flags is **2.1.327** (the version probed).
- **CLI facts this plan relies on** (probed on 2.1.327, recorded in spec §1.6 and the doubt review of this plan):
  - Under `--setting-sources project,local` with `bypassPermissions`, both the runner's `--settings` deny rules and the project's `.claude/settings.json` deny rules still deny.
  - `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` makes the `init` event's `memory_paths` null.
  - An unknown flag exits 1 with `error: unknown option '--<flag>'` on stderr, before any `init` event.
  - **One invocation can emit several `result` events.** Each has its own `model_usage_since_last_result` and `num_turns`; later ones can have an empty usage dict. The first `result` of a resumed invocation can have an empty dict while `modelUsage` holds the session's cumulative total.
- **`doctor`:** it runs no new command. It reads at most one file, the user's Claude settings.
- **Lint and tests:** `uvx ruff@0.17.4 check .` must print `All checks passed!` before each commit. Tests run with `uv run --no-project --with pyyaml --with pytest pytest <file> --import-mode=prepend -q`.
- **Security overlay:** `agents.py` is on the security overlay. The final review uses `security-and-hardening` and the `security-auditor` agent.

## Review Focus

1. **An older CLI that doesn't know a new flag.** The step stops as `blocked`, naming the flag and the fixes (upgrade, or switch `lean`/`user_settings`). It is never an outage hold, and never "Could not resume". *(Task 2.)*
2. **A user whose own Claude settings carry `env`, `apiKeyHelper` or `permissions`.** `doctor` warns that runs won't load them, and how to keep them. A missing, broken, symlinked or oversized file is still `ok`. *(Task 8.)*
3. **Several `result` events in one invocation, or a resumed invocation.** The usage is this invocation's alone, summed over its results. It is never the session's cumulative total. *(Task 3.)*
4. **Old logs and sessions that never reached the model.** There is no `usage` on old `agent_exited` events, and zero-turn sessions occur. Metrics, signals and the panel render them without errors, and zero-turn sessions never lower a turn signal. *(Tasks 5 and 6.)*
5. **An inherited environment that sets the other cache TTL.** It can't override `cache_ttl`. *(Task 2.)*

---

### Task 1: The three `agents.claude` keys and their `validate` checks

**Files:**
- Modify: `agentflow/config.py`:
  - `AgentSpec`;
  - `_build_agent`;
  - `_Validator.agents`, plus a new `_Validator.claude_session` called from it;
  - a new constant `CACHE_TTLS`.
- Modify: `agentflow/defaults/config.yaml` (`agents.claude`: `cache_ttl: 5m`, `lean: true`, `user_settings: false`, one comment each)
- Modify: `agentflow/config_edit.py`:
  - `AGENT_FREE_KEYS`;
  - the `Agent: <name>` section in `_catalog`, which gets the three fields **only when `name == "claude"`**;
  - the `PROTECTED` text.
- Modify: `agentflow/layers.py` (`USER_KEYS` gains `"agents.claude.user_settings"`)
- Modify: `docs/agentflow-playbook.md`:
  - §13, the user-file paragraph at line 455, names `agents.claude.user_settings`;
  - §14 gets three key lines, and the `agents.<name>` line lists the new unprotected keys;
  - a "Claude Code 2.1.327 or later" line in §14's `agents` entry.
- Modify: `CHANGELOG.md`
- Test: `agentflow/tests/test_config.py`, `agentflow/tests/test_config_edit.py`, `agentflow/tests/test_layers.py`

**Interfaces:**
- Produces: `AgentSpec.cache_ttl: str = "5m"`, `AgentSpec.lean: bool = True`, `AgentSpec.user_settings: bool = False`; `config.CACHE_TTLS = ("5m", "1h", "cli")`.

**The `validate` checks (`_Validator.claude_session`):**
- **Error:** `agents.claude.cache_ttl` is not in `CACHE_TTLS`.
- **Warning:** the raw block of an agent other than `claude` sets `cache_ttl`, `lean` or `user_settings`. The message is "only the claude agent reads `<key>`".
- **Warning, path `agents.claude.lean`:** `lean` is true and the repository root has a `.mcp.json`. The message says lean runs don't load it, and to name its servers with `--mcp-config` in `agents.claude.base_args` or set `lean: false`.
- **Warning, path `overlays.security.review_agent`:** `user_settings` is false and `overlays.security.review_agent` names an agent with no `.claude/agents/<name>.md` in the repository. The message says that with `user_settings: false` a run loads only the project's agents.

- [ ] **Step 1: Write the failing tests.** Load configs with the `tmp_cfg` fixture factory, the way the existing `test_config.py` tests do.

```python
def test_claude_starts_on_the_five_minute_cache_lean_and_without_user_settings(tmp_cfg):
    spec = load_config(tmp_cfg()).agents["claude"]
    assert (spec.cache_ttl, spec.lean, spec.user_settings) == ("5m", True, False)

def test_an_unknown_cache_ttl_is_a_validate_error(...):          # agents.claude.cache_ttl: 10m
    assert issue("agents.claude.cache_ttl").level == "error"

def test_the_cache_keys_on_codex_are_a_warning(...):              # agents.codex.lean: false
    assert issue("agents.codex.lean").level == "warning" and "only the claude agent" in issue(...).message

def test_a_project_mcp_file_under_lean_is_a_warning(...):          # write <repo>/.mcp.json
    assert issue("agents.claude.lean").level == "warning" and "--mcp-config" in issue(...).message

def test_a_review_agent_outside_the_project_is_a_warning_without_user_settings(...):
    # overlays.security.review_agent: auditor, no .claude/agents/auditor.md -> warning; add the file -> no issue

# test_config_edit.py
def test_the_form_sets_the_claude_cache_keys(shipped):
    for key, value in ((["agents", "claude", "cache_ttl"], "1h"), (["agents", "claude", "lean"], False),
                       (["agents", "claude", "user_settings"], True)):
        config_edit.save(shipped, form(shipped, (key, value)))
    spec = load_config(shipped).agents["claude"]
    assert (spec.cache_ttl, spec.lean, spec.user_settings) == ("1h", False, True)
    with pytest.raises(EditError):
        form(shipped, (["agents", "claude", "cache_ttl"], "10m"))

def test_the_codex_section_has_no_claude_cache_fields(shipped):
    keys = {tuple(f["key"]) for s in config_edit.form_fields(shipped)["sections"] for f in s["fields"]}
    assert ("agents", "claude", "lean") in keys and ("agents", "codex", "lean") not in keys

# test_layers.py
def test_the_user_file_may_set_claude_user_settings_only(...):
    # a user file with agents.claude.user_settings: true loads and wins; agents.claude.cache_ttl and
    # agents.codex.user_settings in the user file are refused with the file and key named
```

- [ ] **Step 2: Run them to verify they fail.** Expected: FAIL (`AgentSpec` has no attribute `cache_ttl`).
- [ ] **Step 3: Implement.**
  - **Parsing:** `_optional(data, key, path, type, default)`.
  - **Form fields:** `cache_ttl` is `enum` (`options: list(CACHE_TTLS)`); `lean` and `user_settings` are `bool`.
  - **`AGENT_FREE_KEYS`:** `("enabled", "models", "max_turns", "prices", "cache_ttl", "lean", "user_settings")`.
- [ ] **Step 4: Run the three test files, `bin/agentflow validate` (0 warnings in this repository) and ruff.** Expected: PASS.
- [ ] **Step 5: Commit**: "Add the cache_ttl, lean and user_settings keys for Claude run sessions."

### Task 2: Session flags and env, and a refused flag as a named stop

**Files:**
- Modify: `agentflow/agents.py`:
  - `AgentCall`;
  - `Interruption` gains `detail: str | None = None`;
  - `ClaudeAgent.argv`;
  - a new `ClaudeAgent.env`;
  - a new `_CliAgent.env_drop`;
  - `_CliAgent.run` (env assembly, refused-flag detection);
  - constants.
- Modify: `agentflow/lanes.py` (the interruption handling near `lanes.py:640-662`: a `cli_flags` interruption stops before `resume_failed` and `_hold`)
- Modify: `agentflow/sizing_lane.py:466` (`self_contained=True`)
- Modify: `CHANGELOG.md`
- Test: `agentflow/tests/test_agents.py`, `agentflow/tests/test_sizing_lane.py`, `agentflow/tests/test_agent_step.py`

**Interfaces:**
- Consumes: the Task 1 `AgentSpec` fields.
- Produces:
  - **`AgentCall.self_contained: bool = False`.**
  - **Constants:**
    - `TTL_ENV = {"5m": {"FORCE_PROMPT_CACHING_5M": "1"}, "1h": {"ENABLE_PROMPT_CACHING_1H": "1"}, "cli": {}}`;
    - `TTL_VARS = ("FORCE_PROMPT_CACHING_5M", "ENABLE_PROMPT_CACHING_1H", "CLAUDE_CODE_PROMPT_CACHE_TTL")`;
    - `LEAN_ARGS = ("--exclude-dynamic-system-prompt-sections", "--strict-mcp-config")`;
    - `LEAN_ENV = {"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"}`;
    - `NO_USER_SETTINGS = ("--setting-sources", "project,local")`;
    - `UNKNOWN_OPTION = re.compile(r"unknown option '(--[\w-]+)'")`.
  - **`_CliAgent.env_drop(call) -> tuple[str, ...]`:** `()` by default. For Claude with `cache_ttl` other than `cli`, it is the names in `TTL_VARS` that `TTL_ENV[cache_ttl]` doesn't set. `run` pops them from the inherited environment before it adds `self.env(call)`.
  - **`Interruption(kind="cli_flags", reached_model=False, detail="<flag>")`:** when the process exits non-zero, the stream had no `init` event, and the stderr file matches `UNKNOWN_OPTION`. In that case `resume_failed` is False.
  - **A test-local helper** `claude_with(cfg, **spec_changes) -> ClaudeAgent` = `ClaudeAgent(dataclasses.replace(cfg.agents["claude"], **spec_changes), cfg.context)`.

- [ ] **Step 1: Write the failing tests.** Also change the existing `test_agents.py:210` assertion to `"--tools" not in claude.argv(call)` plus `claude_with(cfg, lean=False).argv(call)` having no `--strict-mcp-config`. And change `test_env`: Codex's env is still exactly the run vars plus the auto-compact override, and Claude's is that set plus `FORCE_PROMPT_CACHING_5M` and `CLAUDE_CODE_DISABLE_AUTO_MEMORY`.

```python
def test_a_lean_claude_call_has_a_stable_prefix_and_no_user_settings(claude, call):
    a = claude.argv(call)
    assert "--exclude-dynamic-system-prompt-sections" in a and a.count("--strict-mcp-config") == 1
    assert ("--setting-sources", "project,local") in pairs(a) and a[-1] == call.prompt
    assert "--disable-slash-commands" not in a
    assert claude.argv(dataclasses.replace(call, read_only=True)).count("--strict-mcp-config") == 1

def test_lean_keeps_the_packs_own_mcp_servers(cfg, call):
    spec = dataclasses.replace(cfg.agents["claude"], base_args=[*cfg.agents["claude"].base_args, "--mcp-config", "servers.json"])
    assert ("--mcp-config", "servers.json") in pairs(ClaudeAgent(spec, cfg.context).argv(call))

def test_the_keys_switch_off(cfg, call):
    agent = claude_with(cfg, lean=False, user_settings=True, cache_ttl="cli")
    assert not {"--exclude-dynamic-system-prompt-sections", "--setting-sources", "--strict-mcp-config"} & set(agent.argv(call))
    assert not set(TTL_VARS) & set(agent.env(call)) and "CLAUDE_CODE_DISABLE_AUTO_MEMORY" not in agent.env(call)

def test_the_cache_ttl_wins_over_the_inherited_environment(fake, cfg, call):   # Review Focus 5
    call = dataclasses.replace(call, env_base={"PATH": os.environ["PATH"], "ENABLE_PROMPT_CACHING_1H": "1",
                                               "CLAUDE_CODE_PROMPT_CACHE_TTL": "1h"})
    claude_with(cfg, command=fake.command).run(call, lambda *_: None)
    env = recorded_env(fake)      # from AGENTFLOW_FAKE_CALLS
    assert env["FORCE_PROMPT_CACHING_5M"] == "1" and "ENABLE_PROMPT_CACHING_1H" not in env and "CLAUDE_CODE_PROMPT_CACHE_TTL" not in env

def test_a_self_contained_call_drops_the_skills_listing(claude, call):
    assert "--disable-slash-commands" in claude.argv(dataclasses.replace(call, self_contained=True))

def test_a_refused_flag_is_a_cli_flags_interruption_not_an_outage(fake, fake_claude, call):  # Review Focus 1
    # fake act: no_init, exit 1, stderr "error: unknown option '--setting-sources'"  (add act key `stderr` to fake_agent)
    assert outcome.interruption.kind == "cli_flags" and outcome.interruption.detail == "--setting-sources"
    assert outcome.resume_failed is False   # also with call.resume_session set

# test_agent_step.py
def test_a_refused_flag_stops_the_step_naming_the_flag_and_the_fix(flow_repo):
    # the step's state is a `blocked` stop whose text names --setting-sources, "2.1.327", "user_settings" and "lean";
    # no `run_held` event
```

Add one `test_sizing_lane.py` assertion: the sizer's recorded argv contains `--disable-slash-commands`.

- [ ] **Step 2: Run them to verify they fail.** Expected: FAIL.
- [ ] **Step 3: Implement.**
  - **Placement:** the new flags go after the `--add-dir`/`--tools` block and before `--model`.
  - **`--strict-mcp-config`:** added once.
  - **`ClaudeAgent.env`:** `{**super().env(call), **TTL_ENV[spec.cache_ttl], **(LEAN_ENV if spec.lean else {})}`.
  - **Reading stderr:** `run` reads only the last 4 KB of its own stderr file.
  - **Lanes:** the `cli_flags` branch calls `self._stop(sr, "blocked", …)`.
- [ ] **Step 4: Run `test_agents.py`, `test_sizing_lane.py`, `test_agent_step.py`, `test_explain_golden.py` and ruff.** Expected: PASS; the golden files unchanged.
- [ ] **Step 5: Commit**: "Start Claude run sessions on the 5-minute cache with a lean prefix and without the user's settings, and stop on a refused flag."

### Task 3: Capture each invocation's usage and the usage window from the stream

**Files:**
- Modify: `agentflow/agents.py`:
  - `StreamAccumulator`;
  - `parse_claude_line`;
  - `AgentCall` (`usage_windows`);
  - `AgentOutcome.usage`;
  - the `agent_exited` payload in `_CliAgent.run`;
  - new pure helpers.
- Modify: `agentflow/lanes.py:739` and `agentflow/sizing_lane.py:466` (pass `usage_windows=dict(self.run.state.get("usage_window") or {})`)
- Modify: `agentflow/tests/fake_agent.py`:
  - every `result` event carries `modelUsage` (session cumulative) and `model_usage_since_last_result` (this result's share), with cache fields;
  - new act keys:
    - `model_id` (the stream's model, default `fake-<alias>`);
    - `cache_read`, `cache_write_5m`, `cache_write_1h`, `output_tokens`;
    - `results` (a list of extra result payloads, for multi-result tests);
    - `windows` (`{"five_hour": u, "seven_day": u}` → a `rate_limit_event` with `unifiedWindows`);
    - `stderr` (text written to stderr);
  - the docstring.
- Modify: `CHANGELOG.md`
- Test: `agentflow/tests/test_agents.py`

**Interfaces:**
- **New `StreamAccumulator` fields:**
  - `result_usage: dict[str, dict]`: per-model sums over every result's `model_usage_since_last_result`;
  - `results_seen: int`;
  - `result_turns: int`;
  - `main_calls: int`: main-thread assistant messages with usage, counted once per message id;
  - `prefix_write: int | None`: the first main-thread message's `cache_creation_input_tokens`;
  - `tool_output_chars: int`: the text length of main-thread `tool_result` contents, string or list;
  - `terminal_reason: str | None`: the first non-null `terminal_reason`, so a trailing `api_error` result doesn't hide a completed turn;
  - `auto_memory: bool | None`: from `init`, `memory_paths` truthy;
  - `windows: dict[str, tuple[float, str]]`: the last *emitted* utilization and status, seeded from `AgentCall.usage_windows`.
- **`usage_split(model_usage) -> dict[str, int]`:** keys `input`, `output`, `cache_read`, `cache_write_5m`, `cache_write_1h`, summed over models. Creation not covered by the two ephemeral fields goes to `cache_write_5m`, as in `estimate_cost`.
- **`price_split(prices, model_usage) -> dict[str, float]`:** keys `input`, `output`, `cache_read`, `cache_write`, in USD rounded to 6 places. An unpriced model adds 0.
- **`StreamAccumulator.usage_payload() -> dict`:**
  - **Shape:** `{"tokens", "usd", "turns", "terminal_reason", "prefix_write", "tool_output_chars", "auto_memory", "estimated"}`.
  - **The source rule:**
    - when `result_usage` has any tokens, use it, with `turns = result_turns`;
    - otherwise use the per-message usage (`usage_by_message`, converted to the camel-case shape), with `turns = main_calls` and `estimated: True`.
  - **Never** `modelUsage`.
- **`AgentCall.usage_windows: dict = {}`** and **`AgentOutcome.usage: dict = {}`**. `agent_exited` gains `"usage"`.
- **Event `usage_window`:**
  - **Shape:** `{"window": "five_hour" | "seven_day", "utilization": float, "resets_at": int | None, "status": str}`.
  - **When it is emitted:** when the window is unknown to `acc.windows`, when `round(abs(u - last), 6) >= 0.05`, or when the status differs.

- [ ] **Step 1: Write the failing tests.** Priced tests use the model id `claude-sonnet-5-5`.

```python
def test_usage_sums_every_result_of_the_invocation():                       # Review Focus 3
    # results: since-last {write 130480, turns 64}, {write 300, turns 1} x8, then 10 results with {} and api_error
    assert u["tokens"]["cache_write_5m"] + u["tokens"]["cache_write_1h"] == 130480 + 8 * 300
    assert u["turns"] == 72 and u["terminal_reason"] == "completed" and u["estimated"] is False

def test_a_resumed_invocation_never_takes_the_sessions_cumulative_usage():
    # result 0: since-last {} with modelUsage cumulative 900 read; result 1: since-last 100 read
    assert u["tokens"]["cache_read"] == 100

def test_a_result_with_no_usage_keeps_the_per_message_estimate():          # killed/interrupted session
    assert u["estimated"] is True and u["tokens"]["cache_read"] == 300 and u["turns"] == 3

def test_usage_splits_writes_by_ttl_and_prices_them(cfg):
    # claude-sonnet-5-5: 1h 1_000_000 -> 4.0; 5m 1_000_000 -> 2.5; read 1_000_000 -> 0.1
    assert u["usd"] == {"input": 0.0, "output": 0.0, "cache_read": 0.1, "cache_write": 6.5}

def test_prefix_write_and_tool_output_count_the_main_thread_only():
    assert (u["prefix_write"], u["tool_output_chars"]) == (25404, 30)

def test_auto_memory_reads_the_init_events_memory_paths():
    assert usage_of(init_with(memory_paths=None))["auto_memory"] is False

def test_a_usage_window_event_only_on_news():
    # seeded {"five_hour": (0.40, "allowed")}; stream 0.40, 0.44, 0.45 (allowed), 0.45 (allowed_warning); seven_day 0.41
    assert [(e["window"], e["utilization"], e["status"]) for n, e in events if n == "usage_window"] == \
        [("five_hour", 0.45, "allowed"), ("five_hour", 0.45, "allowed_warning"), ("seven_day", 0.41, "allowed")]

def test_run_puts_usage_on_agent_exited(fake, fake_claude, call):
    assert set(exited["usage"]) >= {"tokens", "usd", "turns", "prefix_write", "estimated"}
```

- [ ] **Step 2: Run them to verify they fail.** Expected: FAIL.
- [ ] **Step 3: Implement.** `parse_codex_line` is unchanged. A Codex payload takes the per-message path, so it comes out estimated with 0 turns.
- [ ] **Step 4: Run the full `test_agents.py` and ruff.** Expected: PASS.
- [ ] **Step 5: Commit**: "Record each agent invocation's token split, turns and prefix write, and the usage windows its stream reports."

### Task 4: The state reducer, and a note when the CLI ignores a setting

**Files:**
- Modify: `agentflow/state.py`:
  - `initial_state` gains `"usage_window": {}` and `"cli_settings_ignored": []`;
  - handlers `_on_usage_window` and `_on_cli_setting_ignored`.
- Modify: `agentflow/agents.py` (a new pure `ignored_settings(spec: AgentSpec, usage: dict) -> list[str]`)
- Modify: `agentflow/lanes.py` (right after `outcome = sr.agent.run(call, on_event)`)
- Modify: `docs/agentflow-playbook.md` (§14 `agents.claude.cache_ttl` and `lean` lines: what the note means), `CHANGELOG.md`
- Test: `agentflow/tests/test_state.py`, `agentflow/tests/test_agents.py`, `agentflow/tests/test_agent_step.py`

**Interfaces:**
- Consumes: `AgentOutcome.usage` and the `usage_window` event (Task 3).
- **`ignored_settings`** reads `usage` only through `.get`, so `{}` gives `[]`. It returns any of these:
  - `"cache_ttl"`, when `cache_ttl == "5m"` and the 1h writes are > 0, or when `cache_ttl == "1h"` and the 5m writes are > 0 with no 1h writes;
  - `"auto_memory"`, when `lean` is set and `usage["auto_memory"]` is True.
- **State:**
  - `state["usage_window"][window] = {"utilization", "resets_at", "status", "at"}`;
  - `state["cli_settings_ignored"]`: the settings noted so far.
- **Event `cli_setting_ignored`:** `{"key", "setting", "agent": "claude"}`, at most once per setting per run. Sizer calls aren't checked.

- [ ] **Step 1: Write the failing tests**

```python
def test_ignored_settings_reads_the_usage_split(cfg):
    assert ignored_settings(spec(cache_ttl="5m"), {"tokens": {"cache_write_1h": 10}}) == ["cache_ttl"]
    assert ignored_settings(spec(lean=True), {"auto_memory": True}) == ["auto_memory"]
    assert ignored_settings(spec(), {}) == []

def test_the_state_keeps_the_latest_usage_window_per_window():
    assert state["usage_window"]["five_hour"]["utilization"] == 0.46 and "seven_day" in state["usage_window"]

def test_a_cli_that_ignores_the_five_minute_ttl_is_noted_once_and_the_run_goes_on(flow_repo):
    # two steps whose fake results report 1h writes
    assert [e["setting"] for e in events if e["type"] == "cli_setting_ignored"] == ["cache_ttl"]
    assert run_status == "finished"
```

- [ ] **Step 2: Run them to verify they fail.** Expected: FAIL.
- [ ] **Step 3: Implement.** The lane emits for each setting in `ignored_settings(...)` that isn't in `self.run.state["cli_settings_ignored"]`.
- [ ] **Step 4: Run the three test files and ruff.** Expected: PASS.
- [ ] **Step 5: Commit**: "Keep the latest usage window in the run state and note once when the CLI ignores a cache or memory setting."

### Task 5: Metrics by token class and by step kind

**Files:**
- Modify: `agentflow/metrics.py`:
  - `step_kind`;
  - `_on_agent_started` records `fresh = not ev.get("session_id")`;
  - `_on_agent_exited` keeps `usage`;
  - `_shape`;
  - `aggregate`;
  - the module docstring.
- Modify: `CHANGELOG.md`
- Test: `agentflow/tests/test_metrics.py`

**Interfaces:**
- Consumes: `agent_exited.usage` (Task 3).
- **`step_kind(key: str) -> str`:** `"<phase>.<base>"`. `base` is the key's last `/` segment cut at its first `-`. So:
  - `"build/build/task-3/implement"` → `"build.implement"`;
  - `"ship/ci/fix-1"` → `"ship.fix"`;
  - `"define/grill_open_questions/r2/apply"` → `"define.apply"`;
  - `"plan/plan/record-q3"` → `"plan.record"`;
  - `"build/build/size-2"` → `"build.size"`.
- **`run_metrics(...)` gains:**
  - `"usd_split": {"input", "output", "cache_read", "cache_write"}`;
  - `"tokens": {"input", "output", "cache_read", "cache_write_5m", "cache_write_1h"}`;
  - `"by_step_kind": {kind: {"sessions", "cost_usd", "usd", "turns", "prefix_writes", "tool_output_chars"}}`.
  - **What goes into the lists:**
    - `turns` holds only calls whose `usage.turns > 0`;
    - `prefix_writes` holds only fresh, non-sizer calls with a `prefix_write`.
- **`aggregate(rows)` gains:**
  - `"usd_split"` (summed);
  - `"step_kinds"`: a list of `{"kind", "sessions", "cost_usd", "usd", "turns_p50", "turns_p90", "prefix_write_p50", "tool_output_chars"}`, sorted by `cost_usd` descending. An empty list gives 0.

- [ ] **Step 1: Write the failing tests**

```python
def test_step_kind_is_the_phase_and_a_bounded_base():
    assert [step_kind(k) for k in ("build/build/task-3/implement", "ship/ci/fix-1",
            "define/grill_open_questions/r2/apply", "plan/plan/record-q3", "build/build/size-2")] == \
        ["build.implement", "ship.fix", "define.apply", "plan.record", "build.size"]

def test_a_run_splits_its_cost_by_token_class_and_step_kind():
    # implement: fresh (prefix 25000, 16 turns), resumed (prefix 3000, 4 turns), one that never reached the model (0 turns);
    # a sizer with prefix 1500; a review
    k = run_metrics(events, NOW)["by_step_kind"]
    assert k["build.implement"]["prefix_writes"] == [25000] and k["build.implement"]["turns"] == [16, 4]
    assert k["build.implement"]["sessions"] == 3 and k["build.size"]["prefix_writes"] == []

def test_runs_from_before_usage_have_empty_splits():                        # Review Focus 4
    m = run_metrics(old_events_without_usage, NOW)
    assert set(m["usd_split"].values()) == {0.0} and m["by_step_kind"]["build.implement"]["turns"] == []
    assert aggregate([{"metrics": m, "lane": "feature"}])["step_kinds"][0]["turns_p90"] == 0

def test_aggregate_takes_the_percentiles_over_every_runs_sessions():
    assert (row["turns_p50"], row["turns_p90"]) == (16, 37)
```

- [ ] **Step 2: Run them to verify they fail.** Expected: FAIL.
- [ ] **Step 3: Implement**, reusing `_p90` and `statistics.median`.
- [ ] **Step 4: Run `test_metrics.py`, `test_dashboard.py -k metrics` and ruff.** Expected: PASS.
- [ ] **Step 5: Commit**: "Split run metrics by token class and step kind."

### Task 6: The cost signals, and one fake-agent run end to end

**Files:**
- Modify: `agentflow/signals.py` (`FAMILIES`, `COUNT_FAMILIES`, `run_signals`, the module docstring)
- Modify: `docs/agentflow-playbook.md` §15 (the signal vocabulary), `CHANGELOG.md`
- Test: `agentflow/tests/test_signals.py`, `agentflow/tests/test_scorecard.py`, `agentflow/tests/test_metrics.py` (the end-to-end case)

**Interfaces:**
- Consumes: the `run_metrics` keys of Task 5.
- Produces six families, all with `"lower"` as the better direction:
  - **`cache_write_share`:** `usd_split.cache_write / sum(usd_split)`, rounded to 4 places. Present when the sum is > 0.
  - **`prefix_write_tokens`:** the median of all `prefix_writes`. Present when there is one.
  - **`session_turns_p90:<kind>`:** present for a kind with turns.
  - **`tool_output_kchars:<kind>`:** `tool_output_chars / sessions / 1000`, rounded to 1 place. Present for a kind with sessions.
  - **`compactions`:** counts `compacted` events. Always present.
  - **`usage_holds`:** counts `run_held` events with `reason == "usage limit"`. Always present.
- **Baseline (spec Q9, a documented trade-off):** the scorecard's baseline for these keys starts at the first `agentflow scorecard --freeze` after this slice ships. The comparison with spec §1's replay numbers is made in the PR description, not wired into `scorecard`.

- [ ] **Step 1: Write the failing tests**

```python
def test_cost_signals_read_the_metrics_splits():
    s = run_signals(events, metrics)
    assert s["cache_write_share"] == 0.58 and s["prefix_write_tokens"] == 25000
    assert s["session_turns_p90:build.implement"] == 37 and s["tool_output_kchars:build.implement"] == 12.5

def test_compactions_and_usage_holds_are_always_counted():
    empty = run_signals([], {"phases": []})
    assert (empty["compactions"], empty["usage_holds"]) == (0, 0)
    s = run_signals(events_with_one_compaction_one_usage_hold_one_ci_hold, metrics)
    assert (s["compactions"], s["usage_holds"]) == (1, 1)

def test_a_run_with_no_usage_has_no_cost_share():                            # Review Focus 4
    assert "cache_write_share" not in run_signals(events, metrics_without_usage)

def test_the_new_families_are_targets_and_lower_is_better():
    assert parse_target("session_turns_p90:build.review") == ("session_turns_p90", "build.review")
    assert FAMILIES["cache_write_share"] == ("lower", False)

# test_metrics.py: the spec §3.1 acceptance
def test_a_fake_agent_run_shows_its_exact_split_in_metrics_and_signals(flow_repo):
    # every act: model_id "claude-sonnet-5-5", cache_write_1h 1_000_000, cache_read 1_000_000, output_tokens 0
    m = run_metrics(events, NOW)
    assert m["usd_split"]["cache_write"] == pytest.approx(4.0 * n_calls)
    assert run_signals(events, m)["cache_write_share"] == pytest.approx(round(4.0 / 4.1, 4))
```

- [ ] **Step 2: Run them to verify they fail.** Expected: FAIL.
- [ ] **Step 3: Implement** in `run_signals`, keeping it pure.
- [ ] **Step 4: Run `test_signals.py`, `test_scorecard.py`, `test_lessons.py`, `test_metrics.py` and ruff.** Expected: PASS.
- [ ] **Step 5: Commit**: "Add the cache, prefix, turn, tool-output, compaction and usage-hold signals."

### Task 7: "Where the tokens go" on the Metrics tab

**Files:**
- Create: `agentflow/dashboard_ui/tokens.js`, pure, with no DOM and no other file needed: `compact(n)` and `tokenRows(kinds)`.
- Modify: `agentflow/dashboard.py:109` (`UI_SCRIPTS`: `"tokens.js"` right before `"metrics.js"`)
- Modify: `agentflow/dashboard_ui/metrics.js` (`renderMetrics`)
- Modify: `CHANGELOG.md`
- Test: `agentflow/tests/test_dashboard_js.py`, `agentflow/tests/test_dashboard.py` (the page serves `tokens.js`)

**Interfaces:**
- Consumes: `totals.usd_split` and `totals.step_kinds` (Task 5), through the existing `/api/metrics` route.
- Produces:
  - **`compact(n: number) -> string`:**
    - below 1000, `String(n)`;
    - below 1e6, `(n/1e3).toFixed(1) + "K"`;
    - otherwise `(n/1e6).toFixed(1) + "M"`.
  - **`tokenRows(kinds) -> string[][]`:** one row per kind, `[kind, sessions, "$"+cost.toFixed(2), "$"+usd.cache_write.toFixed(2), "$"+usd.cache_read.toFixed(2), "$"+usd.output.toFixed(2), turns_p50+" · "+turns_p90, compact(prefix_write_p50), compact(tool_output_chars)+" chars"]`.

- [ ] **Step 1: Write the failing tests**

```python
def test_the_token_table_reads_one_row_per_step_kind():
    rows = js(["tokens.js"], "tokenRows(args)", [{"kind": "build.implement", "sessions": 101, "cost_usd": 48.2,
              "usd": {"cache_write": 23.73, "cache_read": 12.4, "output": 11.98, "input": 0},
              "turns_p50": 16, "turns_p90": 37, "prefix_write_p50": 25404, "tool_output_chars": 5950000}])
    assert rows == [["build.implement", "101", "$48.20", "$23.73", "$12.40", "$11.98", "16 · 37", "25.4K", "6.0M chars"]]

def test_compact_and_an_empty_table():
    assert js(["tokens.js"], "[compact(999), compact(1000), compact(2500000)]", None) == ["999", "1.0K", "2.5M"]
    assert js(["tokens.js"], "tokenRows(args)", []) == []
```

- [ ] **Step 2: Run them to verify they fail.** Expected: FAIL (`tokens.js` missing).
- [ ] **Step 3: Implement `renderMetrics`.**
  - **The KPI:** "of cost on cache writes" (`pct(t.usd_split.cache_write, sum)`), only when the sum is > 0.
  - **The table:** after "Model and effort by phase", an `h2` "Where the tokens go". It is `table([...9 headers], tokenRows(t.step_kinds).map((cells) => el("tr", {}, ...cells.map((text, i) => el("td", { class: i ? "num" : "", text })))), [1, 2, 3, 4, 5, 6, 7, 8])`, shown only when `t.step_kinds.length`.
- [ ] **Step 4: Run `test_dashboard_js.py`, `test_dashboard.py` and ruff.** Expected: PASS.
- [ ] **Step 5: Commit**: "Show where the tokens go, by step kind, on the Metrics tab."

### Task 8: `doctor` names what a run no longer loads from the user's Claude settings

**Files:**
- Modify: `agentflow/doctor.py`:
  - a new `claude_config_dir() -> Path`: `$CLAUDE_CONFIG_DIR`, else `Path.home() / ".claude"`;
  - a new `_user_settings_check(cfg: Config) -> Check | None`, appended after the agent checks.
- Modify: `agentflow/tests/conftest.py`: the autouse fixture that sets `XDG_STATE_HOME` also sets `CLAUDE_CONFIG_DIR` to an empty tmp dir, so no test reads the developer's real file.
- Modify: `docs/agentflow-playbook.md` (the `doctor` checks list), `CHANGELOG.md`
- Test: `agentflow/tests/test_doctor.py`; also update `test_doctor_is_all_ok_with_every_tool_present_and_logged_in` (`test_doctor.py:70-75`), whose expected dict gains `"claude user settings": "ok"`.

**Interfaces:**
- Consumes: `AgentSpec.user_settings` (Task 1).
- **When the check runs:** only when `agents.claude` is enabled and `user_settings` is false.
- **What it reads:** `claude_config_dir() / "settings.json"` with `safeio.read_bytes`. A missing file, `SymlinkRefused`, `ValueError` (over 256 KB), invalid JSON or a non-mapping all count as "no settings". It runs no command.
- **Status `warn`:** when the file has any of `env`, `apiKeyHelper` or `permissions`.
  - The detail: "runs skip your user Claude settings, including <keys>: an API key helper, provider or proxy env, or deny rules set there don't apply in runs (`doctor`'s login check still reads them)".
  - The fix: "move them to the project's .claude/settings.json or the pack's defaults.env, or set `agents.claude.user_settings: true` in your user file".
- **Status `ok`:** otherwise.
  - The detail: "runs skip your user Claude settings: N plugins, M hook events", counting only `enabledPlugins` entries whose value is `true`, or "runs skip your user Claude settings" when there are none.

- [ ] **Step 1: Write the failing tests**

```python
def test_doctor_names_the_user_plugins_and_hooks_a_run_skips(tmp_cfg, repo, monkeypatch, tmp_path):
    # CLAUDE_CONFIG_DIR/settings.json: {"enabledPlugins": {"a@x": true, "b@x": true, "c@x": false}, "hooks": {"PreToolUse": [], "Stop": []}}
    assert check.status == "ok" and "2 plugins, 2 hook events" in check.detail

def test_doctor_warns_when_runs_would_drop_user_env_auth_or_deny_rules(...):   # Review Focus 2
    # {"env": {"ANTHROPIC_BASE_URL": "x"}, "permissions": {"deny": ["Bash(rm *)"]}}
    assert check.status == "warn" and "env" in check.detail and "permissions" in check.detail

@pytest.mark.parametrize("content", [None, "{not json", "[]", "SYMLINK", "x" * (300 * 1024)])
def test_an_absent_or_unreadable_user_settings_file_is_ok(..., content):       # Review Focus 2
    assert check.status == "ok"

def test_no_check_when_runs_keep_the_user_settings(...):                         # user_settings: true
    assert "claude user settings" not in checks
```

- [ ] **Step 2: Run them to verify they fail.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run `test_doctor.py`, `test_plug_and_play.py`, `test_init.py` and ruff.** Expected: PASS.
- [ ] **Step 5: Commit**: "Have doctor name the user settings that run sessions no longer load, and warn about env, auth and deny rules."

---

## Finishing the slice

- [ ] Run the full suite: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests --import-mode=prepend -q`. Then run `uvx ruff@0.17.4 check .` and `bin/agentflow validate` (0 warnings).
- [ ] Run a whole-branch review with `security-and-hardening` and the `security-auditor` agent (overlay: `agents.py`). Focus areas:
  - the argv order, so no flag swallows the prompt;
  - `env_drop`;
  - the stderr read;
  - `--setting-sources` against the probed deny facts;
  - the doctor's read of the user's settings file.
- [ ] Do one real feature run on agentflow itself, on Claude Code 2.1.327 or later. The PR description gives:
  - its `cache_write_share`, `prefix_write_tokens` and `session_turns_p90:*` against spec §1's baseline (58%, 25K, implement p90 37);
  - that the transcripts show `ephemeral_5m` writes and `memory_paths: null`;
  - that the run has no `cli_setting_ignored` event.
