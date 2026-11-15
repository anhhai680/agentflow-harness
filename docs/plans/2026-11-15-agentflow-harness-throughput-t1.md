# Throughput slice T1 (Never block on the environment) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop runs from waiting on a person for things no person needs to decide.
- A broken machine is found before `define` (preflight).
- A resolver names a stop's class. A flaky gate is then confirmed and recorded. A broken tool becomes one inbox item, which the runner approves by itself once the machine is fixed.
- A session that has stopped making progress is ended early (watchdog).
- A `blocked_repeat` stop that comes back after a human's approval says so.
- CI that is still pending becomes a watch, not a human item.

**Architecture:**
- **Preflight.** A new module, `agentflow/preflight.py`, holds the machine checks: `probe` (program checks and `--version` probes for a list of gates) and `run_preflight` (the `setup` gate set plus `probe`, cached for the day). `pipeline._Loop.go` calls `run_preflight` once, before the first phase.
- **Stop classes.** The resolver's `result.json` gains an advisory, typed `stop_class`. `autonomy.Resolver` routes `flaky` and `environment` while its resolve is still open (`state["resolving"]`; `resolve_started` has already cleared the wait).
- **Waits.**
  - An `environment` stop is one ordinary inbox item. `approve` re-checks the machine first, and the queue and the dashboard approve it by themselves once the checks pass.
  - The CI watch reuses the runner's existing hold (`run_held` … `run_hold_ended`, shown as `held`) and polls past `ci.timeout_min`.
- **Watchdog.** It lives in `agents._CliAgent.run`, beside the step timeout.
- **Out of scope.** Parking is T4. Where the spec says "park the unit", T1 holds the run or gives the stop to a human, as today.

**Tech Stack:** Python 3.12 stdlib, pytest. No new dependency.

**Spec:** `docs/specs/agentflow-throughput-proposal.md`. This plan covers slice **T1** (§8):
- §4.1.2: only the `environment` and `flaky` rows, plus recording `repeated_failure`;
- §4.1.3: preflight;
- §4.1.4 and §4.5.5: the CI watcher;
- §4.2: watchdogs;
- verification "T1".

Parking (§4.1.1, T4) and slots (§4.6, T6) are separate slices.

## Global Constraints

- Engine dependencies stay stdlib + `pyyaml>=6`.
- No project strings in the engine (`test_portability.py` `DENYLIST`). Don't name the repository §1 measured anywhere: code, tests, docs or commit messages. Call it "the measured repository".
- The golden files `agentflow/tests/fixtures/explain/*.txt` stay byte-identical. Nothing in this slice is printed by `explain`.
- Every new config key ships in its own task with all four companions:
  - a Settings form field (`config_edit._SECTIONS`);
  - its `validate` checks;
  - a `tests/test_config_edit.py` case;
  - a line in playbook §14.
- **Every lever is opt-in** (spec §5: "Every lever is opt-in for a pack until its slice proves itself on the scorecard, then defaults on in the engine"). The engine defaults are exactly today's behaviour:
  - `preflight.enabled: false`;
  - `watchdog.no_progress_min: 0`;
  - `ci.watch_max_h: 0`.

  The spec's table values (`no_progress_min` 10) become the engine defaults only after T1 is green on 5 runs. The `stop_class` routing acts only when a resolver returns the field.
- **No task edits `.agentflow/**`.** Every run forbids it (`pack.ENGINE_FORBIDDEN`), and this repository's pack is agentflow working on itself. Turning the levers on in this pack, and the matching `.agentflow/prompts/resolve.md` addition, are one human step in Task 7.
- Engine floor unchanged. A `preflight_failed` stop is always a human's: add it to `config.NEVER_RESOLVED`.
- Engine prompts only gain text. If a task drops or rewords text in `agentflow/prompts/*.md`, it must also add a `Retired` entry to `agentflow/pack_fix.py`.
- `agents.py` is a security-overlay path (CLAUDE.md). Plan and review Task 2 with the `security-and-hardening` skill and the `security-auditor` agent.
- New events, each with a `state.REQUIRED` entry:

  | Event | Fields | `state.REQUIRED` |
  |---|---|---|
  | `preflight_checked` | `{ok, lines, digest, cached, duration_s}` | `("ok",)` |
  | `watchdog_fired` | `{key, idle_min}` | `("key",)` |
  | `stop_classified` | `{key, stop_class, action}` | `("key", "stop_class")` |
  | `environment_waiting` | `{key, digest, lines, gates, kind}` | `("key", "digest")` |
  | `environment_rechecked` | `{key, ok, digest}` | `("key", "ok")` |
- **New stop kind and hold reason:**
  - An `environment` stop is an ordinary wait, so `metrics` charges it to `human` as it does any stop, and T0's `human_blocked_share` stays honest.
  - The one new hold reason, `ci_pending`, goes to the `ci` bucket (Task 6).
- Model and tool text shown to a human (a notification line, an inbox line) goes through `hitl.clean_fact`.
- Each user-visible change adds a line under `## [Unreleased]` in `CHANGELOG.md`.
- Tests run as `uv run --no-project --with pyyaml --with pytest pytest <path> --import-mode=prepend -q`. Never create or edit `uv.lock`.

## Review Focus

1. **A long command inside a session.** A Bash call running a 13-minute integration suite writes nothing to the stream until it returns. While a main-thread tool call is open, the watchdog's limit is `max(no_progress_min, defaults.gate_timeout_min)`. Codex's stream isn't tracked for open tools, so a Codex session always gets the longer limit (Task 2: `test_an_open_tool_call_is_not_a_stall`, through a real session; `test_an_agent_that_does_not_track_tools_always_gets_the_gate_timeout`).
2. **A stop that leaves the runner's hands must reach a human, with a way out.** An `environment` stop closes the resolve and becomes one inbox item. A human can approve it (it re-checks first), approve it with `--note` (no check) or cancel. `reject` is refused with a reason. A repeat of the same problem is not notified again, and the run is never left `running` with no wait (Task 4, `test_an_environment_stop_is_one_inbox_item`, `test_environment_twice_gives_one_notification`, `test_approve_with_a_note_skips_the_check`).
3. **A transient `gh` error during a 24 h CI watch.** It keeps the watch going and never ends it as `ci_failed`. Every exit from the watch, an exception included, emits `run_hold_ended` (Task 6, `test_a_gh_error_during_the_watch_keeps_watching`, `test_an_exception_during_the_watch_ends_the_hold`).
4. **A resolver that fixed the code and still says `flaky`.** Reruns on a tree the resolver changed prove nothing. No `flaky:` fact is recorded then (Task 3, `test_a_flaky_claim_after_a_change_records_no_fact`).
5. **Gates a run would never run.** A gate whose `when_exists` target or `cwd` is missing is skipped by preflight too, so a missing `pnpm` under an absent `web/` never fails preflight. A program killed by a signal on `--version` (negative return code) fails it (Task 1, `test_preflight_skips_a_gate_whose_root_is_missing`, `test_a_program_killed_by_a_signal_fails`).

---

### Task 0: The baseline exists

- [ ] **Step 1: Check the baseline.** Run `bin/agentflow scorecard`. Its header names a frozen baseline dated before this slice's first commit. If there is none, stop and ask the human to run `agentflow scorecard --freeze`. Spec §6 measures T1 against a baseline frozen before it, so T1 must not freeze one itself.

---

### Task 1: Preflight before the first phase

**Files:**
- Create: `agentflow/preflight.py`
- Modify:
  - `agentflow/doctor.py`:
    - rename `_run` → `run_bounded(argv, cwd, env=None)`. `env=None` keeps today's behaviour, the process environment;
    - rename `_docker_check` → `docker_check`;
    - update their callers in the file;
  - `agentflow/config.py`: a `Preflight` dataclass, `Config.preflight`, loading, `validate`, and `"preflight_failed"` added to `NEVER_RESOLVED`;
  - `agentflow/defaults/config.yaml`: `preflight: {enabled: false}`;
  - `agentflow/pipeline.py`: `_Loop.go`;
  - `agentflow/hitl.py`:
    - a `preflight_failed` branch in `_how_to_go_on`;
    - `reject` refuses a `preflight_failed` wait;
  - `agentflow/state.py`: `REQUIRED`;
  - `agentflow/config_edit.py`: a new section;
  - `agentflow/tests/fixtures/packs/python/.agentflow/config.yaml`: add `preflight: {enabled: false}` so the form shows the field. Being a test fixture, it isn't a forbidden path;
  - `docs/agentflow-playbook.md`: §14, plus a `preflight_failed` row in the stop table next to `ci_failed`;
  - `CHANGELOG.md`.
- Test: `agentflow/tests/test_preflight.py` (new), `agentflow/tests/test_config_edit.py`, `agentflow/tests/test_config.py`

**Interfaces:**
- Produces:
  - `preflight.Probe`, a frozen dataclass:
    - `ok: bool`;
    - `lines: list[str]`: one per problem, sorted, each cut by `clean_fact`;
    - `digest: str`: the sha256 hex of `"\n".join(lines)`, which names *this* problem;
    - `fingerprint: str`: the sha256 hex of every `(program, resolved path, first line of version output)`, which names the machine.
  - `preflight.route_gates(cfg: Config, lane: str) -> list[str]`: the gate names, in order and without repeats, of every gate set named by a step whose `lanes` include `lane` (or that has no `lanes`). It walks `steps`, `review`, `fix` and `sync` recursively. `gate_sets.setup` and `gate_sets.verify` are always included. It doesn't evaluate `when` or `skip_if`: before `define`, the files they read don't exist yet (a trade-off; see Notes).
  - `preflight.probe(cfg: Config, gates: list[str], cwd: Path) -> Probe`.
  - `preflight.run_preflight(cfg: Config, run: Run, worktree: Path, key: str) -> Probe`: emits one `preflight_checked`.
  - `preflight.PREFLIGHT_STEP = "preflight"`. The stop's key is `f"{first}/preflight"`, where `first` is the phase `_Loop.go` starts at (`opts.from_phase or names[0]`).
  - `Config.preflight.enabled: bool`.

The rules `probe` applies to each gate, using `cfg.gates[name]`:
- **Skipped gates.** Skip a gate whose `when_exists` target is missing under `cwd`, whose `cwd` directory is missing, or whose command `doctor.gate_programs` can't tokenise.
- **Programs.** For each program, skip a relative path (`./x`). If it isn't `doctor.found` on `doctor.gate_path(cfg)`, add `` gate:<name>: `<p>` is not on PATH ``.
- **Version probes.** Otherwise run `doctor.run_bounded([<resolved path>, "--version"], cwd, env=<run_env.base(cfg) with defaults.env on top>)`, once per program. This is the environment the gates get. If the code is `None`, negative (a signal) or ≥ 126, add `` gate:<name>: `<p> --version` could not run: <last line> ``. Any other code passes.
- **Docker.** When any probed program is `docker` and `doctor.docker_check(cwd).status == "fail"`, add `docker: <detail>`.

`run_preflight`:
1. `before = probe(cfg, route_gates(cfg, run.state["pipeline"]), worktree)`.
2. Check the cache, `runs_root(run.repo) / "preflight.json"`, written through `safeio`, with the shape `{"date": "<local ISO date>", "fingerprints": [...]}`. If `before.ok` and today's entry holds `before.fingerprint`, emit `cached=True` and return.
3. Run `gates.run_gates(cfg, "setup", worktree, run.gate_log_dir(key))` when `gate_sets.setup` is non-empty. Each gate that isn't passed or skipped adds `setup:<gate>: <reason>`. Record no `gate_result`: preflight is not evidence, and T0's `full_suite_runs` must not move.
4. `after = probe(...)` again, since setup may install a program. `ok` is `setup passed and after.ok`.
5. On ok with `before.ok`, add `before.fingerprint` to today's entry. An entry for another date is replaced. A machine whose programs only setup installs never hits the cache, so its setup always runs (a trade-off).

`_Loop.go`, after `record_env()` and before the phase loop. Run preflight when all three hold:
- `cfg.preflight.enabled`;
- the run has no `phase_started` event;
- it has no `preflight_checked` event with `ok: true`.

What happens next:
- **Fail.** Call `raise_hitl(cfg, run, "preflight_failed", key, f"Preflight found {n} problem(s) before the run started: {lines[0]}", extra="\n".join(lines))` and return `"waiting_human"`.
- **Pass, after an earlier failure.** Emit `step_finished(key=key, status="done", summary="preflight passed")`.
- **Approve.** The next `advance` runs preflight again.

The `_how_to_go_on` text for this kind is: ``Fix what is listed below, then `agentflow approve {run.id} {key}` runs preflight again; nothing has been spent yet. If a listed setup gate fails on the base branch itself, fix it there, or set `preflight.enabled: false`; `agentflow cancel {run.id}` ends the run.``

`reject` on a `preflight_failed` wait raises `NotWaiting("preflight has nothing to refine: approve it once the machine is fixed, or cancel the run")`.

- [ ] **Step 1: Write the failing tests** in `agentflow/tests/test_preflight.py`, using `flow_repo` and `flow_repo.configure`:
  - `test_route_gates_lists_every_set_the_lane_names`: for the python fixture pack, `route_gates(cfg, "bug")` contains every gate of `gate_sets.setup`, `build` and `verify`, each once.
  - `test_a_missing_program_fails_the_probe`: a gate `{run: "no-such-tool-xyz --check"}` → `ok is False`, and one line equals ``gate:<name>: `no-such-tool-xyz` is not on PATH``.
  - `test_a_program_without_a_version_flag_passes`: an executable script `fake-tool` on `PATH` that exits 2 on `--version` → `ok`.
  - `test_a_program_that_cannot_start_fails`: monkeypatch `doctor.run_bounded` to return `(126, "permission denied")` → the line contains `could not run`.
  - `test_a_program_killed_by_a_signal_fails`: `run_bounded` returning `(-11, "")` → not `ok`.
  - `test_the_version_probe_gets_the_gates_environment`: `defaults.env: {PROBE_MARK: "1"}` and a script that exits 0 only when `PROBE_MARK` is set → `ok`.
  - `test_preflight_skips_a_gate_whose_root_is_missing`: `{run: "no-such-tool-xyz", when_exists: web/package.json}` and `{run: "no-such-tool-xyz", cwd: web}` with no `web/` → `ok`.
  - `test_the_digest_is_the_same_for_the_same_problem`: the same missing tool twice → the same `digest`. Another missing tool → a different one.
  - `test_a_missing_gate_program_stops_the_run_before_define` (the spec's T1 check): with `preflight.enabled: true` and a build gate `{run: "no-such-tool-xyz"}`, `advance` returns `"waiting_human"`. Check that:
    - there is exactly one `hitl_raised`, with kind `preflight_failed` and key `define/preflight`;
    - there is no `agent_started` and no `gate_result`.
  - `test_a_failing_setup_gate_stops_the_run_before_define`: a `setup` gate `{run: "false"}` → the same, and the inbox text contains `setup:`.
  - `test_approving_after_the_fix_runs_preflight_again`: continue the last test. Make the setup gate `"true"`, approve and advance. Check that:
    - there are two `preflight_checked` events, the last with `ok: true`;
    - `define/preflight` is `done`;
    - the first `agent_started` comes after it.
  - `test_a_preflight_stop_cannot_be_rejected`: `reject(run, "define/preflight", ...)` raises `NotWaiting`.
  - `test_a_passing_preflight_is_cached_for_the_day`: two runs back to back. The second's `preflight_checked` has `cached: true`, and the setup gate (a script that appends to a counter file) ran once in total.
  - `test_a_run_that_already_started_skips_preflight`: emit `phase_started` for `define` on a fresh run, then enable preflight with a failing setup → no `preflight_failed` is raised.
  - `test_preflight_off_changes_nothing`: by default, there is no `preflight_checked`.
- [ ] **Step 2: Write the config tests.**
  - In `test_config.py`, `test_preflight_enabled_must_be_a_bool`: `preflight: {enabled: "yes"}` is a `ConfigError` naming `preflight.enabled`, and `preflight: {nope: 1}` is the `validate` error "unknown preflight key".
  - In `test_config_edit.py`, `test_the_form_switches_preflight_on`: `form(shipped, (["preflight", "enabled"], True))` has no errors, changes one line, and loads back `True`.
- [ ] **Step 3: Run them to make sure they fail.**
  Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_preflight.py agentflow/tests/test_config.py agentflow/tests/test_config_edit.py --import-mode=prepend -q`
  Expected: FAIL (`ModuleNotFoundError: agentflow.preflight`, and `Config` has no `preflight`).
- [ ] **Step 4: Implement it** as specified:
  - `config.Preflight(enabled: bool = False)`, loaded like `Recovery`, with `validate` refusing unknown keys;
  - a Settings section `("Environment", "Checks that keep a run from waiting on the machine.", [(("preflight", "enabled"), "Check the machine before a run starts", {"kind": "bool"})])`. Task 2 adds its field here.
- [ ] **Step 5: Run the tests,** plus `test_doctor.py`, `test_hitl.py` and `test_explain_golden.py`.
  Expected: PASS. Then `bin/agentflow validate` prints `0 warnings`.
- [ ] **Step 6: Write the docs.**
  - Playbook §14: `preflight.enabled`: `false` by default. `true` makes a run check, once before its first phase:
    - the programs of every gate its lane names: each found on `PATH`, and able to start `--version` in the gates' environment;
    - Docker, when a gate runs it;
    - the `setup` gate set on the base tree.

    A failure is one `preflight_failed` stop before any agent runs. A pass is cached for the day by the programs' paths and versions. Gates are skipped as a run would skip them (`when_exists`, a missing `cwd`); `when` and `skip_if` conditions aren't evaluated. Not protected.
  - The stop-table row for `preflight_failed`.
  - `CHANGELOG.md` `### Added`.
- [ ] **Step 7: Commit:** `git commit -m "Check the machine before a run's first phase and stop once, before any spend, when it cannot run the gates."`

---

### Task 2: The no-progress watchdog

**Files:**
- Modify:
  - `agentflow/agents.py`:
    - `AgentCall`: add `idle_s` and `tool_idle_s`;
    - `StreamAccumulator`: add `open_tools`;
    - `AgentOutcome` and `to_outcome`: add `stalled`;
    - `_CliAgent`: add `tracks_tools`, and a watcher thread in `run`;
    - `parse_claude_line`: maintain `open_tools`;
  - `agentflow/lanes.py`: the `AgentCall` built at `lanes.py:723`, and the outcome checks at `lanes.py:640-656`;
  - `agentflow/config.py`: a `Watchdog` dataclass, loading, `validate`;
  - `agentflow/defaults/config.yaml`: `watchdog: {no_progress_min: 0}`;
  - `agentflow/state.py`: `REQUIRED`;
  - `agentflow/config_edit.py`: the field in the "Environment" section;
  - `agentflow/tests/fake_agent.py`: a `hold_tool` act;
  - the fixture python pack: `watchdog: {no_progress_min: 0}`;
  - `docs/agentflow-playbook.md`: §14, and the `step_timeout` row;
  - `CHANGELOG.md`.
- Test: `agentflow/tests/test_agents.py`, `agentflow/tests/test_agent_step.py`, `agentflow/tests/test_config_edit.py`, `agentflow/tests/test_config.py`

**Interfaces:**
- Produces:
  - `AgentCall.idle_s: float | None = None` and `AgentCall.tool_idle_s: float | None = None`. `None` means no watchdog. The `Executor` sets them for every session it runs: step sessions, resolvers, and retro, since `retro.py:1064` uses an `Executor`. `sizing_lane`'s own `AgentCall` leaves them `None`.
  - `StreamAccumulator.open_tools: set[str]`: the ids of main-thread `tool_use` blocks with no `tool_result` yet.
  - `_CliAgent.tracks_tools: bool`: `True` on `ClaudeAgent`, `False` on `CodexAgent`.
  - `agents.idle_limit(call: AgentCall, acc: StreamAccumulator, tracks_tools: bool) -> float | None`, a pure function:
    - `None` when `call.idle_s` is `None`;
    - `call.tool_idle_s` when `acc.open_tools` is non-empty, or when `not tracks_tools`;
    - otherwise `call.idle_s`.
  - `AgentOutcome.stalled: bool = False`. `resume_failed` also requires `not stalled`.
  - `Config.watchdog.no_progress_min: float` (default 0, off).
  - The `Executor` sets:
    - `idle_s = no_progress_min * 60`, or `None` when it is 0;
    - `tool_idle_s = max(idle_s, defaults.gate_timeout_min * 60)`.
  - The `hold_tool` act in `fake_agent`: `hold_tool: s` emits one `tool_use`, sleeps `s`, then emits its `tool_result`.

How `run` watches:
- Record `time.monotonic()` on every stdout line.
- A daemon thread waits on a `threading.Event` with a timeout of `min(5.0, limit / 4)`. When `proc.poll() is None`, `not state["timed_out"]` and `monotonic() - last_line > idle_limit(...)`, it sets `state["stalled"] = True` and calls `_terminate(proc)`.
- The `finally` block that cancels the timer also sets the event and joins the thread.
- The timeout timer and the watcher are exclusive: only the first to fire sets its flag, under one `threading.Lock`. So `timed_out` and `stalled` are never both true.

In `lanes`, check `outcome.stalled` right after `outcome.timed_out`, before reading `result.json` and before `outcome.interruption`. It emits `watchdog_fired(key=sr.key, idle_min=<limit in minutes, rounded to 1>)`, then `self._stop(sr, "step_timeout", f"The agent made no progress for {m} min, so the runner ended the session.")`. Its kind is `step_timeout`, so `autonomy.resolve_kinds.step_timeout` settles it in a `resolve: true` phase.

- [ ] **Step 1: Write the failing unit tests** in `test_agents.py`:
  - `test_open_tools_follow_tool_use_and_tool_result`: an assistant line with a main-thread `tool_use` `t1` → `acc.open_tools == {"t1"}`; its `tool_result` → `set()`. A line with `parent_tool_use_id` set never changes it.
  - `test_an_open_tool_call_gets_the_gate_timeout`: with `idle_s=600, tool_idle_s=1800`, `idle_limit` returns 600 with no open tools and 1800 with one.
  - `test_an_agent_that_does_not_track_tools_always_gets_the_gate_timeout`: `tracks_tools=False` → 1800.
  - `test_no_idle_limit_when_the_watchdog_is_off`: `idle_s=None` → `None`.
- [ ] **Step 2: Write the failing step tests** in `test_agent_step.py`, in a phase without `resolve`:
  - `test_a_session_that_goes_quiet_is_ended_as_a_timeout`: `watchdog: {no_progress_min: 0.02}` (1.2 s), and the step's act is `{"sleep": 6, "result": {"status": "done", "summary": "ok"}}`. Expect a `watchdog_fired` with the key, the waiting kind `step_timeout`, a summary containing `no progress`, and `agent_exited` less than 6 s after `agent_started`.
  - `test_an_open_tool_call_is_not_a_stall`: `no_progress_min: 0.02`, `defaults.gate_timeout_min: 0.5`, and the act `{"hold_tool": 3, "result": {"status": "done", "summary": "ok"}}` → the step finishes `done`, with no `watchdog_fired`.
  - `test_the_watchdog_off_changes_nothing`: with the default, the `{"sleep": 2, ...}` act finishes `done`.
- [ ] **Step 3: Write the config tests.**
  - `test_watchdog_no_progress_min_must_be_zero_or_more`: `-1` is a `validate` error naming `watchdog.no_progress_min`; `true` fails to load.
  - `test_the_form_sets_the_watchdog`: `(["watchdog", "no_progress_min"], 15)` saves, and loads back 15.
- [ ] **Step 4: Run them to make sure they fail.**
  Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_agents.py agentflow/tests/test_agent_step.py agentflow/tests/test_config.py agentflow/tests/test_config_edit.py --import-mode=prepend -q`
  Expected: FAIL (`idle_limit` isn't defined).
- [ ] **Step 5: Implement it** as specified, with the form field `(("watchdog", "no_progress_min"), "End a session with no progress after (min, 0 = off)", _NUM)`.
- [ ] **Step 6: Run the tests,** then `test_ci.py`, `test_headless.py`, `test_retro.py` and `test_sizing_lane.py`.
  Expected: PASS. Then `bin/agentflow validate` prints `0 warnings`.
- [ ] **Step 7: Write the docs.**
  - Playbook §14: `watchdog.no_progress_min`: `0` (off) by default. A session that writes nothing to its stream for this many minutes is ended and stopped as `step_timeout`, which a resolver may settle. While the agent waits on a tool call, the limit is `defaults.gate_timeout_min` instead; for Codex, whose stream doesn't show open tool calls, it is always that. One long generation also writes nothing until it ends, so keep the value well above the longest thinking turn (15 is a safe start). Not protected.
  - `CHANGELOG.md` `### Added`.
- [ ] **Step 8: Commit:** `git commit -m "End a step's agent session that has made no progress for watchdog.no_progress_min minutes."`

---

### Task 3: The resolver names a stop's class; a flaky gate is confirmed and recorded

**Files:**
- Modify:
  - `agentflow/hitl.py`: `STOP_CLASSES`, `StepResult.stop_class`, `read_result`;
  - `agentflow/autonomy.py`: `Resolver._run_session`, `Resolver._resolve`, and new private helpers;
  - `agentflow/prompts/resolve.md` and `agentflow/prompts/_result.md`: added text only;
  - `agentflow/state.py`: `REQUIRED`;
  - `docs/agentflow-playbook.md`: §7;
  - `CHANGELOG.md`.
- Test: `agentflow/tests/test_hitl.py`, `agentflow/tests/test_autonomy.py`

**Interfaces:**
- Produces:
  - `hitl.STOP_CLASSES = ("environment", "flaky", "guard_false_positive", "spec_gap", "impact", "repeated_failure")`.
  - `StepResult.stop_class: str | None = None`. It is advisory, so `read_result` never refuses it: a value that is in `STOP_CLASSES` is kept, and anything else (missing, `null`, unknown, not a string) becomes `None`.
  - `Resolver._stop_class(session_key: str) -> str | None`.
  - `Resolver._confirm_flaky(key: str, session_key: str, changed: bool) -> str`, which returns an action, one of:
    - `"confirmed"`;
    - `"not_confirmed"`;
    - `"no_failed_gate"`;
    - `"tree_changed"`;
    - `"halted"`.
  - Every resolver session whose result names a class emits `stop_classified(key=key, session_key=session_key, stop_class=cls, action=action)`. A class with no routing gets `action="none"`.
  - **Once per session.** When a `stop_classified` already exists for the `session_key`, a resume that runs `_run_session` again (`finish_open`) emits no second one and doesn't rerun `_confirm_flaky`. Only the environment wait (Task 4) is taken up again. Test: `test_a_resume_does_not_classify_again`, where a resolve left open after a pause, then resumed, yields one `stop_classified`.
  - When `_resolve` refuses because the attempts are used, it emits `stop_classified(key=key, stop_class="repeated_failure", action="human")`, once per key since a human last acted (`_since_human`). T4 parks on this; T1 only records it.

How `_run_session` works now:
- Take `before = gitops.tree_state(executor.worktree)` before the session, and compare it with the state after. `changed` means they differ.
- Then route by class:
  - `flaky` → `_confirm_flaky`;
  - `environment` → Task 4;
  - any other class → `stop_classified` with `action="none"`.
- `_finish` follows, as today.

How `_confirm_flaky` works:
- If `changed`, return `"tree_changed"`: reruns on a tree the resolver changed prove nothing.
- **The failing gates** are the failures of the **most recent** batch of `gate_result` events on `key` (a maximal run of consecutive `gate_result` events with that key, as in T0): `passed: false` and not `skipped`. An empty list returns `"no_failed_gate"`.
- **The reruns.** Rerun each failing gate alone, twice: `run_gates(cfg, [name], executor.worktree, run.gate_log_dir(f"{key}/flaky"))`. Check `halted` between reruns (return `"halted"`). Record no `gate_result`. The reruns aren't evidence: the step's own gates still decide when it resumes.
- **Confirmed** when every failing gate passed both reruns. Emit **one** `run_fact(text=clean_fact(f"flaky: {', '.join(names)} failed in {key}, then passed twice when run alone"), key=session_key)`. One fact per confirmation keeps the fact list (`RUN_FACTS_MAX` 8) from filling up. The fact names gates, not tests; the resolver's summary names the test (a trade-off).

The prompt additions are text only:
- **`resolve.md`:** insert a paragraph immediately before the line that begins `Say in your summary what the cause was`:

  > Name the cause's class in `stop_class` when one fits: `environment` (a tool is missing or broken, a port is in use, Docker is down: nothing in this work tree can fix it), `flaky` (a gate fails and then passes when run again, or depends on the clock or on test order), `guard_false_positive`, `spec_gap`, `impact` (a public contract breaks, or a module's responsibility changes) or `repeated_failure`. `environment` is the one case where you finish as `done` without fixing the cause, and not as `blocked`: say exactly what is broken. The runner then checks the machine's tools, waits while those checks fail, and resumes the step once they pass. For `flaky`, change nothing in the tree. Leave `stop_class` out when none fits.

- **`_result.md`:** add a line after the `facts` line: ``- `stop_class`: only in a resolver session: the cause's class, one of `environment`, `flaky`, `guard_false_positive`, `spec_gap`, `impact`, `repeated_failure`.``

- [ ] **Step 1: Write the failing tests.**
  - In `test_hitl.py`, `test_stop_class_is_advisory`: `"flaky"` → `"flaky"`; a missing value, `"weather"` and `3` → `None`, with no `ResultError`.
  - In `test_autonomy.py`, using `auto` and `RESOLVE`, with `defaults.fix_attempts: 0` so the first gate failure stops the step, and a gate script that fails on its first run and passes afterwards (a counter file):
    - `test_a_flaky_gate_is_confirmed_and_recorded`: the resolver's act is `{"result": {"status": "done", "summary": "flaky", "stop_class": "flaky"}}`, and it writes nothing. Check that:
      - there is one `stop_classified` with `action == "confirmed"`;
      - there is one `run_fact` starting with `flaky: `;
      - the step finishes `done`;
      - no `gate_result` has a key under `<key>/flaky`.
    - `test_a_gate_that_fails_again_is_not_flaky`: the gate always fails → `"not_confirmed"`, and no `flaky:` fact.
    - `test_a_flaky_claim_after_a_change_records_no_fact`: the resolver's act also writes a file → `"tree_changed"`, and no `flaky:` fact.
    - `test_spent_attempts_are_recorded_as_a_repeated_failure_once`: the resolver keeps reporting `done` and the step keeps blocking. When the stop goes to a human, there is exactly one `stop_classified` with `stop_class == "repeated_failure"`.
- [ ] **Step 2: Run them to make sure they fail.**
  Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_hitl.py agentflow/tests/test_autonomy.py --import-mode=prepend -q`
  Expected: FAIL (`StepResult` has no `stop_class`).
- [ ] **Step 3: Implement it** as specified.
- [ ] **Step 4: Run the tests,** then `test_agent_step.py`, `test_portability.py`, `test_pack_fix.py` and `test_explain_golden.py`.
  Expected: PASS. Then `bin/agentflow validate` prints `0 warnings`.
- [ ] **Step 5: Write the docs.**
  - Playbook §7: a resolver may name a `stop_class`, and the runner acts on two of them:
    - `flaky`: it reruns the failing gates alone twice, only when the resolver changed nothing, and records one `flaky:` fact;
    - `environment`: Task 4.

    Every other class is recorded only.
  - `CHANGELOG.md` `### Added`.
- [ ] **Step 6: Commit:** `git commit -m "Let a resolver name a stop's class, and confirm and record a flaky gate by running it alone twice."`

---

### Task 4: An environment stop is one inbox item, approved by the runner once the machine is fixed

The design was chosen after review: an `environment` stop is an ordinary human wait, so the inbox, reminders, `approve`, `cancel`, the dashboard and `metrics` (its `human` bucket) all work as they do today. The runner never holds the run for it. What's new:
- one notification per distinct problem;
- `approve` checks the machine again first;
- the queue and the dashboard re-check waiting environment stops, and approve them as the runner once the checks pass.

**Files:**
- Modify:
  - `agentflow/autonomy.py`:
    - `stop_gates`;
    - the `environment` branch in `_run_session`, and `_hand_over`;
    - `take_over` skips its own closing notification after a hand-over;
    - `recheck_environment`, called first in `finish_open`;
  - `agentflow/preflight.py`: `recheck_waiting`;
  - `agentflow/hitl.py`:
    - `raise_hitl(…, notify: bool | None = None)`;
    - an `environment` branch in `_how_to_go_on`;
    - `reject` refuses an `environment` wait;
  - `agentflow/config.py`: `"environment"` added to `NEVER_RESOLVED`;
  - `agentflow/module_queue.py` (`run_queue`) and `agentflow/dashboard.py` (the loop that calls `hitl.remind_due`, `dashboard.py:254`): call `preflight.recheck_waiting` next to `remind_due`;
  - `agentflow/state.py`: `REQUIRED`;
  - `docs/agentflow-playbook.md`: §7, and a stop-table row for `environment`;
  - `CHANGELOG.md`.
- Test: `agentflow/tests/test_autonomy.py`, `agentflow/tests/test_preflight.py`, `agentflow/tests/test_hitl.py`

**Interfaces:**
- Consumes:
  - `preflight.probe`, `preflight.route_gates`, `Probe.ok`, `Probe.digest` and `Probe.lines` (Task 1). Callers do `from agentflow import preflight` and call `preflight.probe(...)`, so tests can patch `agentflow.preflight.probe`.
  - `Resolver._stop_class`, `stop_classified` and the once-per-session rule (Task 3).
  - `run_control.spawn_resume(run, config) -> Path` (existing).
- Produces:
  - `hitl.ENVIRONMENT = "environment"`: the new stop kind.
  - `autonomy.ENV_POLL_S = 120`: the least number of seconds between two re-checks of one stop by `recheck_waiting`.
  - `autonomy.stop_gates(cfg: Config, run: Run, key: str) -> list[str]`:
    - the names of the `gate_result` events whose key is `key` or starts with `f"{key}/"`, in order, without repeats, plus `gate_sets.setup`;
    - when no gate ran there, `preflight.route_gates(cfg, run.state["pipeline"])`.
  - `Resolver._hand_over(key: str, kind: str, summary: str, probe: Probe, gates: list[str]) -> None`, in this order:
    1. emit `environment_waiting(key=key, digest=probe.digest, lines=probe.lines, gates=gates, kind=kind)`;
    2. emit `resolve_finished(key=key, stop_kind=kind, summary=summary)`, which closes the resolve;
    3. call `raise_hitl(cfg, run, ENVIRONMENT, key, clean_fact(f"{key} waits on this machine: {probe.lines[0]}"), extra="\n".join([*probe.lines, "", "The resolver reported:", summary]), notify=<see below>)`;
    4. set `self._handed_over = True`.

    `notify` is `True` unless an earlier `environment_waiting` with the same `digest` came after the run's last `stop_classified` with `action == "environment_fixed"`; then it is `False`.
  - `raise_hitl(…, notify: bool | None = None)`:
    - `None`: today's rule (quiet when `runner_settles`);
    - `True`: always notify;
    - `False`: never notify.
  - `take_over` skips its closing `notify_run` when `self._handed_over`, so a hand-over notifies at most once.
  - `Resolver.recheck_environment() -> bool`, called first in `finish_open`. It finds the newest `hitl_approved` with `kind == "environment"` and no `note`, and no later `environment_rechecked` on its key. Then it:
    1. probes the gates of that key's last `environment_waiting`;
    2. emits `environment_rechecked(key=key, ok=p.ok, digest=p.digest)`;
    3. **red:** calls `raise_hitl(cfg, run, ENVIRONMENT, key, <the same summary form>, notify=<digest rule>)` and returns `False` (`walk` returns `"stopped"`);
    4. **green:** emits `stop_classified(key=key, stop_class="environment", action="environment_fixed")` and returns `True`.

    An approval with `--note` is the human's override, and it isn't checked.
  - `preflight.recheck_waiting(cfg: Config, repo: Path, spawn: Callable[[Run], object] | None = None) -> list[str]`. For each non-terminal run whose `state["waiting"]["kind"] == "environment"`, and whose last `environment_rechecked` for that key is older than `ENV_POLL_S` (or absent):
    1. probe that key's `environment_waiting` gates in `run.state["worktree"]`;
    2. emit `environment_rechecked(key, ok, digest, by=AUTO_BY)`;
    3. on green:
       - `hitl.approve(run, key, by=AUTO_BY, note="The machine checks pass now.")`, ignoring `NotWaiting`, since a human may have acted first;
       - emit `stop_classified(action="environment_fixed")`;
       - call `spawn(run)` when `lock_state(run.dir) != "live"`. The default `spawn` is `lambda run: run_control.spawn_resume(run, None)`.

    It returns the ids it approved and never raises: an error becomes one line in the queue's log.
  - The `_how_to_go_on` text for `environment` is: ``The runner checks the machine again whenever `agentflow queue` or the dashboard is running, and approves this itself once the checks pass. To go on now, fix it and `agentflow approve {run.id} {key}` (it checks again first), or add `--note "..."` to go on without the check; `agentflow cancel {run.id}` ends the run.``
  - `reject` on an `environment` wait raises `NotWaiting("an environment stop has nothing to refine: approve it once the machine is fixed, or cancel the run")`.

How `_run_session` routes `environment`. It does this after the resolver session finishes `done`, and once per session (Task 3):
1. `gates = stop_gates(cfg, run, key)`; `p = preflight.probe(cfg, gates, executor.worktree)`.
2. **Green:** `stop_classified(action="probes_green")`, then `_finish`. The step reopens at once, as today. The probes can't see every fault (a port in use), and the resolver's attempts still bound the loop.
3. **Red:** `stop_classified(action="handed_over")`, then `_hand_over(...)`, and return `False`.

- [ ] **Step 1: Write the failing tests** in `test_autonomy.py`. In each one, monkeypatch:
  - `agentflow.preflight.probe` to a stub returning the next `Probe` from a list (the last one repeats). `RED` and `GREEN` are module constants, and every `RED` has the same digest;
  - `agentflow.autonomy.notify_run` and `agentflow.hitl.notify` to collect their messages.

  The resolver's act is `{"result": {"status": "done", "summary": "pnpm is broken", "stop_class": "environment"}}`, and the stopped step ran a gate.
  - `test_an_environment_stop_is_one_inbox_item`: `[RED]`. Check that:
    - `advance` returns `"waiting_human"`, with waiting kind `environment` and a summary containing `waits on this machine`;
    - `state["resolving"] is None`;
    - `hitl.inbox_items` has exactly one item for the run;
    - exactly one message was sent.
  - `test_environment_twice_gives_one_notification` (the spec's T1 check): `[RED]`. `approve` with no note, then `advance` → the re-check is red and the stop is raised again with the same digest. Check that:
    - there are two `hitl_raised` events of kind `environment`;
    - exactly one message was sent;
    - there is one `environment_rechecked` with `ok: false`;
    - no agent session started after the approval.
  - `test_approve_checks_again_and_goes_on_once_green`: `[RED, GREEN]`, then approve and advance → `environment_rechecked` has `ok: true`, `stop_classified` has `action == "environment_fixed"`, and the step finishes.
  - `test_approve_with_a_note_skips_the_check`: `[RED]`, then approve with `note="fixed by hand"` → there is no `environment_rechecked`, and the step's session resumes.
  - `test_the_problem_back_after_a_fix_notifies_again`: red, approve, green, then the step blocks again and the resolver says `environment` with the same digest → two messages.
  - `test_green_probes_reopen_the_step_at_once`: `[GREEN]` → `action == "probes_green"`, and there is no `environment` wait.
  - `test_stop_gates_prefers_the_gates_that_ran`: after a `gate_result` on `build/fix` for `lint`, `stop_gates(cfg, run, "build/fix") == ["lint", *setup]`. With none, it equals `route_gates`.
- [ ] **Step 2: Write the re-check and refusal tests.**
  - `test_preflight.py`, `test_recheck_waiting_approves_a_fixed_machine_and_resumes`: a run waiting on `environment`, with no live lock, and the probes `[GREEN]` → `recheck_waiting(cfg, repo, spawn=calls.append)` returns `[run.id]`. The wait was approved `by: runner`, and `calls == [run]`.
  - `test_preflight.py`, `test_recheck_waiting_leaves_a_broken_machine_and_waits_between_checks`: `[RED]` → it returns `[]` and logs one `environment_rechecked`. A second call at once probes nothing (the stub's call count is unchanged).
  - `test_preflight.py`, `test_recheck_waiting_never_raises`: a stub that raises `RuntimeError` → `[]`.
  - `test_hitl.py`, `test_an_environment_stop_cannot_be_rejected`: `reject` raises `NotWaiting`.
  - The existing `test_autonomy.py::test_a_human_only_stop_cannot_be_listed`, parametrized over `NEVER_RESOLVED`, now covers `environment` too: `autonomy.resolve_kinds.environment` is a `validate` error.
- [ ] **Step 3: Run them to make sure they fail.**
  Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_autonomy.py agentflow/tests/test_preflight.py agentflow/tests/test_hitl.py --import-mode=prepend -q`
  Expected: FAIL.
- [ ] **Step 4: Implement it** as specified.
- [ ] **Step 5: Run the tests,** then `test_next_queue.py`, `test_dashboard.py`, `test_control_cmds.py` and `test_metrics.py`.
  Expected: PASS.
- [ ] **Step 6: Write the docs.**
  - Playbook §7 and the stop table: an `environment` stop is one inbox item. It notifies once per distinct problem, `approve` re-checks the machine first (`--note` skips the check), and the queue and the dashboard approve it by themselves once the checks pass.
  - `CHANGELOG.md` `### Added`.
- [ ] **Step 7: Commit:** `git commit -m "Make an environment stop one inbox item that the runner re-checks and approves once the machine is fixed."`

---

### Task 5: A `blocked_repeat` stop a human already approved

**Files:**
- Modify:
  - `agentflow/hitl.py`: `raise_hitl`, `_how_to_go_on(…, times: int = 1)`, `notice_text`;
  - `agentflow/autonomy.py`: `take_over`'s closing notification uses `notice_text`;
  - `agentflow/state.py`: `_on_hitl_raised` copies `times` into the wait;
  - `docs/agentflow-playbook.md`: the `blocked_repeat` row;
  - `CHANGELOG.md`.
- Test: `agentflow/tests/test_hitl.py`, `agentflow/tests/test_autonomy.py`

The scope is the spec's: dedupe of `blocked_repeat`.
- Only that kind is counted.
- Its refusal of a plain approval stays as it is today.
- `blocked` and every other kind are unchanged.
- No notification is ever suppressed.

What counts is the measured repository's case: a human approved the same stop, and it came back. Raises the runner settled quietly are not counted.

**Interfaces:**
- Produces:
  - For kind `blocked_repeat`, `raise_hitl` sets `times` to 1 plus the number of `hitl_approved` events on `key` whose `by` is not `AUTO_BY` and whose `kind` is `blocked` or `blocked_repeat`. The summary is not compared: it is model prose, and a resumed session rarely repeats it word for word. `blocked_repeat` already means "blocked again with the tree unchanged", so the stop is the same one. The count restarts after the last `step_retry` on `key`. It is recorded as `times` on the event and in `state["waiting"]["times"]`.
  - `hitl.notice_text(wait: dict) -> str`:
    - when `times > 1`: `f"{key} is blocked again for the same reason after {times - 1} approval(s): {summary}"`;
    - otherwise the summary.

    Both notification paths use it: `raise_hitl` and `take_over`'s closing `notify_run`.
  - When `times > 1`, `_how_to_go_on` puts first the line ``This exact stop came back after {times - 1} approval(s), so a plain approval would repeat it: fix the cause, then `agentflow retry {run.id} {key}` starts a fresh session, or approve with --note to say what changed.``

- [ ] **Step 1: Write the failing tests.**
  - `test_hitl.py`, `test_a_reworded_summary_still_counts`: the second `blocked_repeat` has a different summary → `times == 2`.
  - `test_hitl.py`, `test_a_repeat_after_a_human_approval_says_so`: monkeypatch `hitl.notify`. `raise_hitl("blocked_repeat")` → `approve(by="me", note="x")` → `raise_hitl("blocked_repeat")` with the same key and summary. Check that:
    - there are two notifications, the second containing `after 1 approval(s)`;
    - `state["waiting"]["times"] == 2`;
    - the inbox contains `came back after 1 approval(s)`.
  - `test_hitl.py`, `test_a_retry_starts_the_count_afresh`: the same, with a `step_retry` on the key in between → `times == 1`.
  - `test_hitl.py`, `test_runner_approvals_and_other_kinds_are_not_counted`: an approval with `by=AUTO_BY` → `times == 1`. `blocked` and `gate_failed` events carry no `times`.
  - `test_autonomy.py`, `test_a_repeat_through_the_resolver_path_names_it`: in a `resolve: true` phase (`auto`), a step blocks with nothing changed, its resolver attempts are spent, a human approves with a note, and it blocks the same way again. The human's notification contains `blocked again for the same reason`.
- [ ] **Step 2: Run them to make sure they fail.**
  Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_hitl.py agentflow/tests/test_autonomy.py --import-mode=prepend -q`
  Expected: FAIL.
- [ ] **Step 3: Implement it** as specified.
- [ ] **Step 4: Run the tests,** then `test_control_cmds.py` and `test_dashboard.py`.
  Expected: PASS.
- [ ] **Step 5: Write the docs.**
  - The playbook's `blocked_repeat` row: a stop that comes back after a human's approval says how many approvals it has outlived, in the notification and the inbox.
  - `CHANGELOG.md` `### Changed`.
- [ ] **Step 6: Commit:** `git commit -m "Say when a blocked_repeat stop comes back after a human's approval, and how often."`

---

### Task 6: CI that is still pending is a watch, not a human item

**Files:**
- Modify:
  - `agentflow/ci_lane.py`: `_wait`, the module docstring step 1, and an import of `notify_run` from `agentflow.hitl`;
  - `agentflow/config.py`:
    - `"watch_max_h"` added to `CI_KEYS`;
    - the `CI_KEYS` validate loop refuses a value that isn't finite (`math.isfinite`), and so does `watchdog.no_progress_min`;
  - `agentflow/defaults/config.yaml`: `ci.watch_max_h: 0`;
  - `agentflow/pipeline.py`: `_Loop.go` ends a stale hold;
  - `agentflow/module_queue.py`: `_assess`;
  - `agentflow/metrics.py`: the bucket of a `run_held` event, by reason;
  - `agentflow/config_edit.py`: the CI section;
  - `docs/agentflow-playbook.md`: the `ci_failed` row, and the §14 `ci` line;
  - `CHANGELOG.md`.
- Test: `agentflow/tests/test_ci.py`, `agentflow/tests/test_pipeline.py`, `agentflow/tests/test_next_queue.py`, `agentflow/tests/test_config.py`, `agentflow/tests/test_config_edit.py`, `agentflow/tests/test_metrics.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces:
  - `ci.watch_max_h: float`, default 0, which keeps today's `ci_failed` stop at `ci.timeout_min`. It must be a finite number, 0 or more. The engine layer always supplies it.
  - `ci_lane.HOLD_REASON = "ci_pending"`.
  - `metrics`: the event→bucket map gains a reason lookup for `run_held`, `HELD_BUCKETS = {"ci_pending": "ci"}`. Any other reason stays `hold`. Tests (`test_metrics.py`): `test_a_ci_pending_hold_is_ci_time`, where `run_held{reason: ci_pending}` → `run_hold_ended` 100 s later gives `buckets["ci"] == 100`; and `test_other_holds_stay_hold_time`.
  - `module_queue.FREE_HOLDS = ("ci_pending",)`: a run whose `state["hold"]["reason"]` is listed occupies no queue slot.
  - `_Loop.go` emits `run_hold_ended(key=hold["key"])` first, when `state["hold"]` is set. A runner starting holds nothing yet, so any recorded hold is one a dead runner left behind. The reducers are unchanged.

How `_wait` changes:
- **The watch window** uses the wall clock (`time.time()`), as `lanes` holds do: `monotonic` stops while a Mac sleeps. The existing `appear_min` and `timeout_min` checks keep `monotonic`.
- **The start.** The watch starts at the first poll where `waited >= timeout_min` with checks still pending, when `watch_max_h > 0`:
  - emit `run_held(key=key, reason="ci_pending", until=<deadline ISO from time.time()>)`;
  - then `notify_run(cfg, run, f"{key}: CI is still running after {timeout_min} min; the run watches it for up to {watch_max_h} h and needs nothing from you.")`, once.
- **During the watch:** a `GhError` is no verdict, and "no checks" isn't either (GitHub reports none for a moment after a re-run). Both keep the watch going. The `appear_min` stop applies only before the watch starts. The deadline is checked on every poll, **before** the no-checks `continue` (`ci_lane.py:144`), so a run whose checks vanish still gives up. Test: `test_no_checks_forever_during_the_watch_gives_up`, where `PENDING` then `NONE` forever → `"waiting_human"`.
- **The exits.**
  - The checks settle: end the hold, then return `pass` or `fail`.
  - A halt: end the hold, then return `"stopped"`.
  - Past the deadline: end the hold **first**, then raise today's `ci_failed` stop with the summary `CI did not settle within {timeout_min} minutes and {watch_max_h} h of watching: {why}.` `run_hold_ended` is a wait end in `metrics`, so it must come before the new `hitl_raised`.
  - Any exception: a `finally` ends the hold if no exit ended it yet, guarded by a local flag.

A `gh` error at the timeout with the watch off (`watch_max_h: 0`) stops as today.

- [ ] **Step 1: Write the failing tests** in `test_ci.py`.
  - `WATCH = {**PATIENT, "ci": {**PATIENT["ci"], "watch_max_h": 1}}`.
  - The fake clock: monkeypatch `agentflow.ci_lane.time` to `SimpleNamespace(monotonic=<+60 s per call>, time=<+60 s per call>, sleep=lambda s: None)`, so the global `time` module is untouched.

  The tests:
  - `test_pending_checks_past_the_timeout_are_watched`: `gh.play(PENDING, PENDING, PENDING, PASS)` → `go == "done"`. Check that:
    - the PR is marked ready;
    - there is one `run_held` with `reason == "ci_pending"` and one `run_hold_ended`;
    - no `hitl_raised` was logged;
    - one message contains `needs nothing from you`.
  - `test_a_gh_error_or_no_checks_during_the_watch_keeps_watching`: play `PENDING`, `PENDING`, `{"stderr": "HTTP 502", "exit": 1}`, `NONE`, then `PASS` → `"done"`.
  - `test_the_watch_gives_up_after_watch_max_h`: `timeout_min: 1`, `watch_max_h: 0.05` (3 min), and `PENDING` forever → `"waiting_human"`. Check that:
    - the summary contains `of watching`;
    - there is one `run_held` and one `run_hold_ended`;
    - `run_hold_ended` comes before the `hitl_raised`;  - `test_an_exception_during_the_watch_ends_the_hold`: `ci_lane.pr_checks` raises `RuntimeError` on its 3rd call → the error propagates, `run_hold_ended` is logged, and `state["hold"] is None`.
  - `test_a_pause_ends_the_ci_watch`: like `test_a_pause_ends_the_wait_and_a_resume_picks_it_up`, under `WATCH`, pausing on the 3rd poll → `"paused"` and `run_hold_ended`. A resume finishes `done`.
  - `test_the_watch_off_stops_as_today`: the existing `test_checks_that_never_settle_stop` passes unchanged under `PIPE`.
- [ ] **Step 2: Write the pipeline, queue and config tests.**
  - `test_pipeline.py`, `test_a_new_runner_ends_a_stale_hold`: a run with `run_held` and no `run_hold_ended` (its runner died) → after `advance` starts, one `run_hold_ended` for that key comes before any new step event.
  - `test_next_queue.py`, `test_a_run_watching_ci_frees_its_slot`: a running run with `state["hold"]["reason"] == "ci_pending"` and a live lock doesn't count against `max_parallel=1`, so the next ready module starts. A run whose hold is `usage limit` still counts.
  - `test_config.py`, `test_ci_watch_max_h_must_be_finite_and_zero_or_more`: `-1` and `.inf` are `validate` errors naming `ci.watch_max_h`. `.nan` in `watchdog.no_progress_min` is an error too.
  - `test_config_edit.py`, `test_the_form_sets_the_ci_watch`: `(["ci", "watch_max_h"], 6)` saves; `-1` is an `EditError`.
- [ ] **Step 3: Run them to make sure they fail.**
  Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_ci.py agentflow/tests/test_pipeline.py agentflow/tests/test_next_queue.py agentflow/tests/test_config.py agentflow/tests/test_config_edit.py agentflow/tests/test_metrics.py --import-mode=prepend -q`
  Expected: the new tests FAIL; the existing ones PASS.
- [ ] **Step 4: Implement it** as specified, with the form field `(("ci", "watch_max_h"), "Watch pending checks after the timeout for (h, 0 = stop)", _NUM)`.
- [ ] **Step 5: Run the tests.**
  Expected: PASS. Then `bin/agentflow validate` prints `0 warnings`.
- [ ] **Step 6: Write the docs.**
  - The `ci_failed` row: with `ci.watch_max_h` above 0, checks still pending after `ci.timeout_min` are watched (`held`) for up to that many hours before they stop.
  - The §14 `ci` line names `watch_max_h`. It says a run watching CI holds no queue slot, so a pack whose runs share one database or port should leave it at 0 until slots exist (T6). It also says a resume starts a fresh watch.
  - `CHANGELOG.md` `### Added`.
- [ ] **Step 7: Commit:** `git commit -m "Watch CI checks still pending after ci.timeout_min instead of stopping for a human, and free the run's queue slot meanwhile."`

---

### Task 7: The whole slice

- [ ] **Step 1: Run the full suite.**
  Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests --import-mode=prepend -q`
  Expected: PASS (25–60 min).
- [ ] **Step 2: Validate and run doctor.** `bin/agentflow validate` prints `0 warnings`. `bin/agentflow doctor`'s output is unchanged by the renames.
- [ ] **Step 3: Check the goldens.** Run `git diff --stat main...HEAD -- agentflow/tests/fixtures/explain/`. Expected: no output.
- [ ] **Step 4: Check the measurement.** `bin/agentflow scorecard` still names the baseline from Task 0, and its rows still carry `lead_time_h`, `human_blocked_share` and `full_suite_runs`.
- [ ] **Step 5: A human turns T1 on in this repository's pack, outside any run.** `.agentflow/**` is forbidden to agents.
  - In `.agentflow/config.yaml`, set:
    - `preflight: {enabled: true}`;
    - `watchdog: {no_progress_min: 15}`;
    - `ci: {watch_max_h: 24}`.

    Its `setup` set is the `portability` test, so preflight runs it on the base tree.
  - In `.agentflow/prompts/resolve.md`, add Task 3's paragraph before the line `Say in your summary what the cause was and what you changed.`
  - Run `bin/agentflow validate` (0 warnings), and commit with the human's own message.

## Notes: accepted trade-offs

- **Preflight probes every gate of the lane.** It doesn't evaluate `when` or `skip_if`, since their files don't exist before `define`. A pack whose conditional step needs a tool the machine lacks gets a preflight stop. `preflight.enabled: false` is the way out.
- **The watchdog's "progress" is a stream line,** not a file change. Without partial messages, one long generation is silent. That is why the lever is off by default, with 15 suggested for this pack.
- **Probes can't see every environment fault,** such as a port in use or a remote service down. Such a stop reopens at once (`probes_green`), as today, and the resolver's attempts bound it.
- **When no gate ran on the stopped step,** the environment probe falls back to every gate of the lane. A missing program of an unrelated gate then makes the stop an inbox item, which a human clears with `approve --note`.
- **Auto-approval needs `agentflow queue` or the dashboard running.** Without either, a human approves.
- **A CI watch restarts its window on resume,** and it frees the queue slot while it waits.
- **A flaky fact names gates, not tests.**
- **A machine whose programs only setup installs never hits the preflight cache.**
