# Throughput slice T1 (Never block on the environment) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop runs from waiting on a person for things no person needs to decide. A broken machine is found before `define` (preflight). A resolver names the class of a stop, so a flaky gate is confirmed and recorded and a broken tool makes the run wait for the machine rather than for a human. A session that has stopped making progress is ended early (watchdog). A stop raised again in the same words notifies nobody a second time. CI that is still pending becomes a watch, not a human item.

**Architecture:** One new module, `agentflow/preflight.py`, holds the machine checks: `probe` (program checks and `--version` probes for a list of gates) and `run_preflight` (the `setup` gate set plus `probe`, cached for the day). `pipeline._Loop.go` calls `run_preflight` once, before the first phase. The resolver's `result.json` gains a typed `stop_class`, and `autonomy.Resolver` routes two classes, `flaky` and `environment`. A wait that needs no human decision reuses the hold the runner already has (`run_held` … `run_hold_ended`, shown as `held`): the environment wait re-probes until it is green, and the CI watch polls past `ci.timeout_min`. The watchdog lives in `agents._CliAgent.run`, beside the step timeout, and ends a session whose stream has gone quiet. Parking (T4) is not in this slice. Where the spec says "park the unit", T1 holds the run or leaves the stop with a human, as today.

**Tech Stack:** Python 3.12 stdlib, pytest. No new dependency.

**Spec:** `docs/specs/agentflow-throughput-proposal.md`. This plan covers slice **T1** (§8): §4.1.2 (only the `environment` and `flaky` rows, plus recording `repeated_failure`), §4.1.3 preflight, §4.1.4 and §4.5.5 the CI watcher, §4.2 watchdogs, and verification "T1". Parking (§4.1.1, T4) and slots (§4.6, T6) are separate slices.

## Global Constraints

- Engine dependencies stay stdlib + `pyyaml>=6`.
- No project strings in the engine (`test_portability.py` `DENYLIST`). Don't name the repository §1 measured anywhere: code, tests, docs or commit messages. Call it "the measured repository".
- The golden files `agentflow/tests/fixtures/explain/*.txt` stay byte-identical. Nothing in this slice is printed by `explain`.
- Every new config key ships in its own task with all four companions:
  - a Settings form field (`config_edit._SECTIONS`);
  - its `validate` checks;
  - a `tests/test_config_edit.py` case;
  - a line in playbook §14.

  The keys are `preflight.enabled` (default `false`), `watchdog.no_progress_min` (default `10`) and `ci.watch_max_h` (default `24`).
- Preflight is opt-in (spec §5: "Every lever is opt-in for a pack until its slice proves itself on the scorecard"). This repository's pack turns it on. Each lever has an off switch that restores today's behaviour:
  - `preflight.enabled: false`;
  - `watchdog.no_progress_min: 0`;
  - `ci.watch_max_h: 0`.

  The `stop_class` routing acts only when a resolver returns the field.
- Engine floor unchanged. A `preflight_failed` stop is always a human's: add it to `config.NEVER_RESOLVED`.
- Engine prompts only gain text. If a task drops or rewords text in `agentflow/prompts/*.md`, it must also add a `Retired` entry to `agentflow/pack_fix.py`. This repository's pack overrides `resolve.md` (`.agentflow/prompts/resolve.md`), so that file gets the same addition.
- `agents.py` is a security-overlay path (CLAUDE.md). Plan and review Task 2 with the `security-and-hardening` skill and the `security-auditor` agent.
- New events: `preflight_checked{ok, lines, digest, cached, duration_s}`, `watchdog_fired{key, idle_min}`, `stop_classified{key, stop_class, action}`, `environment_waiting{key, digest, lines}`. Each one gets a `state.REQUIRED` entry: `preflight_checked: ("ok",)`, `watchdog_fired: ("key",)`, `stop_classified: ("key", "stop_class")`, `environment_waiting: ("key", "digest")`.
- New hold reasons: `environment` and `ci_pending`. `metrics` charges the first to the `human` bucket and the second to `ci` (Task 4 and Task 6), so the T0 KPI `human_blocked_share` stays honest.
- Each user-visible change adds a line under `## [Unreleased]` in `CHANGELOG.md`.
- Tests run as `uv run --no-project --with pyyaml --with pytest pytest <path> --import-mode=prepend -q`. Never create or edit `uv.lock`.

## Review Focus

1. **A long command inside a session.** A Bash call running a 13-minute integration suite writes nothing to the stream until it returns. While a main-thread tool call is open, the watchdog's limit is `max(no_progress_min, defaults.gate_timeout_min)`, not `no_progress_min`. Codex's stream isn't tracked for open tools, so a Codex session always gets the longer limit (Task 2, `test_an_open_tool_call_gets_the_gate_timeout`, `test_an_agent_that_does_not_track_tools_always_gets_the_gate_timeout`).
2. **A pack with an optional code root.** A gate whose `when_exists` target is missing would be skipped in a run. Preflight must skip it too, so a missing `pnpm` under an absent `web/` never fails preflight (Task 1, `test_preflight_skips_a_gate_whose_root_is_missing`).
3. **A program with no `--version` flag.** BSD `find` and some `make`s exit 1 or 2 on `--version`. Only a program that can't start fails the probe: exit code `None` (it timed out or didn't start), or 126 and above (not executable, or killed by a signal) (Task 1, `test_a_program_without_a_version_flag_passes`).
4. **A pause or a cancel during a hold.** An environment wait or a CI watch ends within one poll. It emits `run_hold_ended`, and notifies nobody (Tasks 4 and 6, `test_a_pause_ends_the_environment_wait_quietly`, `test_a_pause_ends_the_ci_watch`).
5. **A run started before this slice.** A run that already has a `phase_started` event never runs preflight when it resumes. Neither does a run whose preflight already passed (Task 1, `test_a_run_that_already_started_skips_preflight`).

---

### Task 1: Preflight before define

**Files:**
- Create: `agentflow/preflight.py`
- Modify:
  - `agentflow/doctor.py`: rename `_run` → `run_bounded` and `_docker_check` → `docker_check`, and update their callers in the file;
  - `agentflow/config.py`: a `Preflight` dataclass, `Config.preflight`, loading, `validate`, and `"preflight_failed"` added to `NEVER_RESOLVED`;
  - `agentflow/defaults/config.yaml`: `preflight: {enabled: false}`;
  - `agentflow/pipeline.py`: `_Loop.go`;
  - `agentflow/hitl.py`: a `preflight_failed` branch in `_how_to_go_on`;
  - `agentflow/state.py`: `REQUIRED`;
  - `agentflow/config_edit.py`: a new section;
  - `.agentflow/config.yaml`: `preflight: {enabled: true}`;
  - `agentflow/tests/fixtures/packs/python/.agentflow/config.yaml`: `preflight: {enabled: false}`, so the form shows the field;
  - `docs/agentflow-playbook.md`: §14, plus a `preflight_failed` row in the stop table next to `ci_failed`;
  - `CHANGELOG.md`.
- Test: `agentflow/tests/test_preflight.py` (new), `agentflow/tests/test_config_edit.py`, `agentflow/tests/test_config.py`

**Interfaces:**
- Produces:
  - `preflight.Probe`, a frozen dataclass:
    - `ok: bool`;
    - `lines: list[str]`: one per problem, sorted;
    - `digest: str`: the sha256 hex of `"\n".join(lines)`, which names *this* machine problem;
    - `fingerprint: str`: the sha256 hex of every `(program, resolved path, version output)`, which names the machine.
  - `preflight.route_gates(cfg: Config, lane: str) -> list[str]`: the gate names, in order and without repeats, of every gate set named by a step of `lane`. It walks `steps`, `review`, `fix` and `sync` recursively. `gate_sets.setup` and `gate_sets.verify` are always included.
  - `preflight.probe(cfg: Config, gates: list[str], cwd: Path) -> Probe`.
  - `preflight.run_preflight(cfg: Config, run: Run, worktree: Path) -> Probe`: emits one `preflight_checked`.
  - `preflight.PREFLIGHT_STEP = "preflight"`. The stop's key is `f"{first phase}/preflight"`.
  - `doctor.run_bounded(argv: list[str], cwd: Path) -> tuple[int | None, str]` and `doctor.docker_check(repo: Path) -> Check`: renamed, behaviour unchanged.
  - `Config.preflight.enabled: bool`.

The rules `probe` applies to each gate in `gates`, using `cfg.gates[name]`:
- **Skipped gates.** Skip a gate whose `when_exists` target is missing under `cwd`, or whose command `doctor.gate_programs` can't tokenise.
- **Programs.** For each program, skip a relative path (`./x`). If the program isn't `doctor.found` on `doctor.gate_path(cfg)`, add the line `` gate:<name>: `<p>` is not on PATH ``.
- **Version probes.** Otherwise run `doctor.run_bounded([<resolved path>, "--version"], cwd)`, once per program. If the code is `None` or ≥ 126, add the line `` gate:<name>: `<p> --version` could not run: <last line> ``. Any other code passes. Its output (its first line) goes into the fingerprint.
- **Docker.** When any probed program is `docker` and `doctor.docker_check(cwd).status == "fail"`, add `docker: <detail>`.

`run_preflight`:
1. `before = probe(cfg, route_gates(cfg, run.state["pipeline"]), worktree)`.
2. Check the cache, `runs_root(run.repo) / "preflight.json"`, written through `safeio`, with the shape `{"date": "<local ISO date>", "fingerprints": [...]}`. If it holds `before.fingerprint` for today's local date and `before.ok`, emit `cached=True` and return.
3. Otherwise run `gates.run_gates(cfg, "setup", worktree, run.gate_log_dir(key))` when `gate_sets.setup` is non-empty. Each gate that isn't passed or skipped adds the line `setup:<gate>: <reason>`. Record no `gate_result`: preflight is not evidence, and T0's `full_suite_runs` must not move.
4. `after = probe(...)` again, since setup may install a program. `ok` holds when setup passed and `after.ok`. On ok, add `before.fingerprint` to today's cache entry. An entry for another date is replaced.

`_Loop.go`, after `record_env()` and before the phase loop. Run preflight when all three hold:
- `cfg.preflight.enabled`;
- the run has no `phase_started` event;
- it has no `preflight_checked` event with `ok: true`.

What happens next:
- **Fail.** Call `raise_hitl(cfg, run, "preflight_failed", key, f"Preflight found {n} problem(s) with this machine: {lines[0]}", extra="\n".join(lines))` and return `"waiting_human"`.
- **Pass, after an earlier failure.** Emit `step_finished(key=key, status="done", summary="preflight passed")`, so no row stays open.
- **Approve.** `agentflow approve <run> <key>` clears the wait. The next `advance` runs preflight again.

The `_how_to_go_on` text for this kind is: ``Fix the machine (the checks are listed below), then `agentflow approve {run.id} {key}` runs preflight again; nothing has been spent yet.``

- [ ] **Step 1: Write the failing tests** in `agentflow/tests/test_preflight.py`, using the `flow_repo` fixture and `flow_repo.configure`:
  - `test_route_gates_lists_every_set_the_lane_names`: for the python fixture pack, `route_gates(cfg, "bug")` contains every gate of `gate_sets.setup`, `build` and `verify`, each once.
  - `test_a_missing_program_fails_the_probe`: a gate `{run: "no-such-tool-xyz --check"}` in `route_gates` → `probe(...).ok is False`, and one line equals ``gate:<name>: `no-such-tool-xyz` is not on PATH``.
  - `test_a_program_without_a_version_flag_passes`: write an executable script `fake-tool` on `PATH` that exits 2 on `--version`. A gate running it probes `ok`.
  - `test_a_program_that_cannot_start_fails`: a script that isn't executable but is found by a monkeypatched `doctor.found`, or a monkeypatched `doctor.run_bounded` returning `(126, "permission denied")` → the line contains `could not run`.
  - `test_preflight_skips_a_gate_whose_root_is_missing`: a gate `{run: "no-such-tool-xyz", when_exists: web/package.json}` with no `web/` → `ok`.
  - `test_the_digest_is_the_same_for_the_same_problem`: two probes of the same missing tool → the same `digest`. A second missing tool → a different one.
  - `test_a_failing_setup_gate_stops_the_run_before_define`: with `preflight.enabled: true` and a `setup` gate `{run: "false"}`, start a feature run and `advance`. Check that:
    - the result is `"waiting_human"`;
    - `run.state["waiting"]["kind"] == "preflight_failed"` and `["key"] == "define/preflight"`;
    - there is no `agent_started` event and no `gate_result` event;
    - the inbox text names `setup:`.
  - `test_approving_after_the_fix_runs_preflight_again`: continue the last test. Change the setup gate to `"true"` through `flow.configure`, approve, and advance. Check that there are two `preflight_checked` events, the last with `ok: true`, that `define/preflight` is `done`, and that the first `agent_started` comes after it.
  - `test_a_passing_preflight_is_cached_for_the_day`: two runs back to back. The second run's `preflight_checked` has `cached: true`, and its setup gate (a script that appends to a counter file) ran once in total.
  - `test_a_run_that_already_started_skips_preflight`: emit `phase_started` for `define` on a fresh run, then enable preflight with a failing setup. `advance` raises no `preflight_failed`.
  - `test_preflight_off_changes_nothing`: with the default `enabled: false`, there is no `preflight_checked` event.
- [ ] **Step 2: Write the config tests.**
  - In `test_config.py`, `test_preflight_enabled_must_be_a_bool`: `preflight: {enabled: "yes"}` fails to load with a `ConfigError` naming `preflight.enabled`, and `preflight: {nope: 1}` is a `validate` error, "unknown preflight key".
  - In `test_config_edit.py`, `test_the_form_switches_preflight_on`: `form(shipped, (["preflight", "enabled"], True))` has no errors, changes one line, and `load_config(shipped).preflight.enabled is True`.
- [ ] **Step 3: Run them to make sure they fail.**
  Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_preflight.py agentflow/tests/test_config.py agentflow/tests/test_config_edit.py --import-mode=prepend -q`
  Expected: FAIL (`ModuleNotFoundError: agentflow.preflight`, and `Config` has no `preflight`).
- [ ] **Step 4: Implement it.**
  - `config.Preflight(enabled: bool = False)`, loaded like `Recovery`.
  - `validate` refuses unknown keys in the block.
  - The doctor renames.
  - `preflight.py` as specified above.
  - The `_Loop.go` hook, the `_how_to_go_on` branch and the `REQUIRED` entry.
  - A Settings section `("Environment", "Checks that keep a run from waiting on the machine.", [(("preflight", "enabled"), "Check the machine before a run starts", {"kind": "bool"})])`. Task 2 adds its field here.
  - Set `preflight: {enabled: true}` in `.agentflow/config.yaml`.
- [ ] **Step 5: Run the tests, plus `test_doctor.py` and `test_explain_golden.py`.**
  Expected: PASS. Then `bin/agentflow validate` prints `0 warnings`.
- [ ] **Step 6: Write the docs.**
  - Playbook §14 line: `preflight.enabled`: `false` by default. `true` makes a run check the machine once before its first phase: the programs of every gate its lane uses (each found on `PATH` and able to run `--version`), Docker when a gate runs it, and the `setup` gate set on the base tree. A failure is one `preflight_failed` stop before any agent runs. A pass is cached for the day by the programs' paths and versions. Not protected.
  - Stop-table row: `preflight_failed`.
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
  - `agentflow/lanes.py`: the `AgentCall` built at `lanes.py:723`, and the outcome checks next to `outcome.timed_out` (`lanes.py:646`);
  - `agentflow/config.py`: a `Watchdog` dataclass, loading, `validate`;
  - `agentflow/defaults/config.yaml`: `watchdog: {no_progress_min: 10}`;
  - `agentflow/state.py`: `REQUIRED`;
  - `agentflow/config_edit.py`: the field in the "Environment" section;
  - the fixture python pack: `watchdog: {no_progress_min: 10}`;
  - `docs/agentflow-playbook.md`: §14, and the `step_timeout` row of the stop table;
  - `CHANGELOG.md`.
- Test: `agentflow/tests/test_agents.py`, `agentflow/tests/test_agent_step.py`, `agentflow/tests/test_config_edit.py`, `agentflow/tests/test_config.py`

**Interfaces:**
- Consumes: nothing from Task 1, apart from the "Environment" form section.
- Produces:
  - `AgentCall.idle_s: float | None = None` and `AgentCall.tool_idle_s: float | None = None`. `None` means no watchdog. Only `lanes.Executor` sets them; `sizing_lane` and `retro` calls leave them `None`.
  - `StreamAccumulator.open_tools: set[str]`: the ids of main-thread `tool_use` blocks that have no `tool_result` yet.
  - `_CliAgent.tracks_tools: bool`: `True` on `ClaudeAgent`, `False` on `CodexAgent`.
  - `agents.idle_limit(call: AgentCall, acc: StreamAccumulator, tracks_tools: bool) -> float | None`, a pure function:
    - `None` when `call.idle_s` is `None`;
    - `call.tool_idle_s` when `acc.open_tools` is non-empty, or when `not tracks_tools`;
    - otherwise `call.idle_s`.
  - `AgentOutcome.stalled: bool = False`. `resume_failed` also requires `not stalled`.
  - `Config.watchdog.no_progress_min: float` (default 10; `0` turns the watchdog off).
  - The `Executor` sets:
    - `idle_s = no_progress_min * 60`, or `None` when it is 0;
    - `tool_idle_s = max(idle_s, defaults.gate_timeout_min * 60)`.
  - A stalled outcome emits `watchdog_fired(key=sr.key, idle_min=<limit in min, rounded to 1>)`, then `self._stop(sr, "step_timeout", f"The agent made no progress for {m} min, so the runner ended the session.")`. It is the same kind as the step timeout, so `autonomy.resolve_kinds.step_timeout` settles it in a `resolve: true` phase.

How `run` watches: record `time.monotonic()` on every stdout line. A daemon thread wakes every `min(5.0, limit / 4)` seconds. When `monotonic() - last_line > idle_limit(...)`, it sets `stalled` and calls `_terminate(proc)`. It exits when the process does. The step timeout's `threading.Timer` stays as it is.

- [ ] **Step 1: Write the failing unit tests** in `test_agents.py`:
  - `test_open_tools_follow_tool_use_and_tool_result`: feed `parse_claude_line` an assistant line holding a `tool_use` with id `t1` → `acc.open_tools == {"t1"}`. Then a user line with a `tool_result` for `t1` → `set()`. A sub-agent's line (`parent_tool_use_id` set) never changes it.
  - `test_an_open_tool_call_gets_the_gate_timeout`: with `AgentCall(idle_s=600, tool_idle_s=1800, ...)`, `idle_limit` returns 600 with no open tools and 1800 with one.
  - `test_an_agent_that_does_not_track_tools_always_gets_the_gate_timeout`: `idle_limit(call, acc, tracks_tools=False) == 1800`.
  - `test_no_idle_limit_when_the_watchdog_is_off`: `idle_s=None` → `None`.
- [ ] **Step 2: Write the failing step test** in `test_agent_step.py`. `test_a_session_that_goes_quiet_is_ended_as_a_timeout`:
  - configure `watchdog: {no_progress_min: 0.02}` (1.2 s) in a phase without `resolve`;
  - script the step `{"sleep": 6, "result": {"status": "done", "summary": "ok"}}`;
  - after `advance`: a `watchdog_fired` event with that key; the waiting kind is `step_timeout`; the summary contains `no progress`; and `agent_exited` came less than 6 s after `agent_started`.
- [ ] **Step 3: Write the config tests.**
  - `test_watchdog_no_progress_min_must_be_zero_or_more`: `-1` is a `validate` error naming `watchdog.no_progress_min`; `true` fails to load.
  - `test_the_form_turns_the_watchdog_off`: `(["watchdog", "no_progress_min"], 0)` saves, and loads back as 0.
- [ ] **Step 4: Run them to make sure they fail.**
  Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_agents.py agentflow/tests/test_agent_step.py agentflow/tests/test_config.py agentflow/tests/test_config_edit.py --import-mode=prepend -q`
  Expected: FAIL (`idle_limit` isn't defined, and there's no `watchdog_fired`).
- [ ] **Step 5: Implement it** as specified, with the form field `(("watchdog", "no_progress_min"), "End a session with no progress after (min, 0 = off)", _NUM)`.
- [ ] **Step 6: Run the tests,** then `test_ci.py`, `test_headless.py` and `test_sizing_lane.py`. They make agent calls, and none of them may change.
  Expected: PASS. Then `bin/agentflow validate` prints `0 warnings`.
- [ ] **Step 7: Write the docs.**
  - Playbook §14: `watchdog.no_progress_min`: `10` by default. A step's session that writes nothing to its stream for this many minutes is ended and stopped as `step_timeout`, which a resolver may settle. While the agent waits on a tool call (a long test run), the limit is `defaults.gate_timeout_min` instead. For Codex, whose stream doesn't show open tool calls, it is always that. `0` turns it off. Not protected.
  - `CHANGELOG.md` `### Added`.
- [ ] **Step 8: Commit:** `git commit -m "End a step's agent session that has made no progress for watchdog.no_progress_min minutes."`

---

### Task 3: The resolver names a stop's class; a flaky gate is confirmed and recorded

**Files:**
- Modify:
  - `agentflow/hitl.py`: `STOP_CLASSES`, `StepResult.stop_class`, `read_result`;
  - `agentflow/autonomy.py`: `Resolver._run_session`, `Resolver._resolve`, and new private helpers;
  - `agentflow/prompts/resolve.md` and `.agentflow/prompts/resolve.md`: one added paragraph, identical in both;
  - `agentflow/state.py`: `REQUIRED`;
  - `docs/agentflow-playbook.md`: §7, where the resolver is described;
  - `CHANGELOG.md`.
- Test: `agentflow/tests/test_hitl.py`, `agentflow/tests/test_autonomy.py`

**Interfaces:**
- Produces:
  - `hitl.STOP_CLASSES = ("environment", "flaky", "guard_false_positive", "spec_gap", "impact", "repeated_failure")`.
  - `StepResult.stop_class: str | None = None`. `read_result` maps a missing value or `null` to `None`. Any other value must be one of `STOP_CLASSES`, or it raises `ResultError("stop_class must be one of …")`.
  - `Resolver._stop_class(session_key: str) -> str | None`: the class from the resolver session's result, or `None` when the result can't be read.
  - `Resolver._confirm_flaky(key: str, session_key: str) -> str`, which returns an action, one of:
    - `"confirmed"`;
    - `"not_confirmed"`;
    - `"no_failed_gate"`.
  - Every routed class emits `stop_classified(key=key, session_key=session_key, stop_class=cls, action=action)`.
  - When `_resolve` refuses because the attempts are used, it emits `stop_classified(key=key, stop_class="repeated_failure", action="human")`. T4 parks on this; T1 only records it.

How `_confirm_flaky` works:
- **The failing gates** are the last batch of `gate_result` events on `key` with `passed: false` and not `skipped`. A batch is a maximal run of consecutive `gate_result` events with the same key, as in T0.
- **The reruns.** Rerun each failing gate alone, twice: `run_gates(cfg, [name], executor.worktree, run.gate_log_dir(f"{key}/flaky"))`. Record no `gate_result`. The reruns aren't evidence; the step's own gates still decide when it resumes.
- **Confirmed** when every failing gate passed both reruns. Then, for each gate, emit `run_fact(text=f"flaky:{name}: it failed in {key}, then passed twice when run alone", key=session_key)`. It is a fact, so every later session of the run sees it.

After `_remember_facts`:
- `flaky` → `_confirm_flaky`;
- `environment` → Task 4;
- any other class → `stop_classified` with `action="none"`;
- in every case `_finish` follows, as today.

The paragraph added to `resolve.md`, after the `facts` paragraph:

> Name the cause's class in `stop_class` when one fits: `environment` (a tool is missing or broken, a port is in use, Docker is down: nothing in this work tree can fix it), `flaky` (a gate fails and then passes when run again, or depends on the clock or on test order), `guard_false_positive`, `spec_gap`, `impact` (a public contract breaks, or a module's responsibility changes) or `repeated_failure`. For `environment`, finish as `done` and say exactly what is broken: the runner waits until its own checks of the machine pass, then resumes the step. Leave `stop_class` out when none fits.

- [ ] **Step 1: Write the failing tests.**
  - In `test_hitl.py`:
    - `test_stop_class_is_read_and_checked`: `"flaky"` → `"flaky"`; a missing value → `None`; `"weather"` → `ResultError` naming `stop_class`.
  - In `test_autonomy.py`, using the file's `auto` fixture and `RESOLVE`, with the step's gate set to a script that fails on its first run and passes afterwards (a counter file):
    - `test_a_flaky_gate_is_confirmed_and_recorded`: the resolver's result is `{"status": "done", "summary": "flaky", "stop_class": "flaky"}`. After `advance`:
      - one `stop_classified` with `action == "confirmed"`;
      - a `run_fact` whose text starts with `flaky:<gate>:`;
      - the step finishes `done`;
      - no `gate_result` is written under `<key>/flaky`.
    - `test_a_gate_that_fails_again_is_not_flaky`: the same, but the gate always fails → `action == "not_confirmed"`, and no `flaky:` fact.
    - `test_spent_attempts_are_recorded_as_a_repeated_failure`: the resolver keeps reporting `done` and the step keeps blocking. When the stop goes to a human, the last `stop_classified` has `stop_class == "repeated_failure"`.
- [ ] **Step 2: Run them to make sure they fail.**
  Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_hitl.py agentflow/tests/test_autonomy.py --import-mode=prepend -q`
  Expected: FAIL (`StepResult` has no `stop_class`).
- [ ] **Step 3: Implement it** as specified. Add the paragraph to both `resolve.md` files.
- [ ] **Step 4: Run the tests,** then `test_portability.py`, `test_pack_fix.py` and `test_explain_golden.py`.
  Expected: PASS. Then `bin/agentflow validate` prints `0 warnings`.
- [ ] **Step 5: Write the docs.**
  - Playbook §7: a resolver may name a `stop_class`, and the runner acts on `flaky` (it reruns the failing gates alone twice and records a `flaky:` fact) and on `environment` (Task 4). Every other class is recorded only.
  - `CHANGELOG.md` `### Added`.
- [ ] **Step 6: Commit:** `git commit -m "Let a resolver name a stop's class, and confirm and record a flaky gate by running it alone twice."`

---

### Task 4: An environment stop waits for the machine, not for a human

**Files:**
- Modify:
  - `agentflow/autonomy.py`: `Resolver._wait_for_environment`, its call in `_run_session`, and `take_over`, which sends no notification when the run is halted;
  - `agentflow/state.py`: `display_status`, `REQUIRED`;
  - `agentflow/metrics.py`: the bucket of a `run_held` event, by reason;
  - `docs/agentflow-playbook.md`: §7 and the hold row of the stop-handling table;
  - `CHANGELOG.md`.
- Test: `agentflow/tests/test_autonomy.py`, `agentflow/tests/test_metrics.py`, `agentflow/tests/test_state.py`

**Interfaces:**
- Consumes:
  - `preflight.probe`, `preflight.route_gates` and `Probe.digest` / `Probe.lines` (Task 1);
  - `Resolver._stop_class` and `stop_classified` (Task 3);
  - `Executor._wait_until(until: float) -> bool` (existing).
- Produces:
  - `autonomy.ENV_POLL_S = 120`: seconds between probes. Tests monkeypatch it to 0.
  - `Resolver._wait_for_environment(key: str) -> bool`: `True` once the probes are green; `False` when the run was halted, or when `recovery.give_up_h` passed.
  - `environment_waiting(key, digest, lines)`, emitted once per wait.
  - `display_status(run)` returns `held` whenever `state["hold"]` is set and the lock is live, for any status that isn't terminal. Today it does so only for `running` and `refining`.
  - `metrics` charges a `run_held` with `reason == "environment"` to `human`, one with `reason == "ci_pending"` to `ci` (used by Task 6), and any other reason to `hold`, as today.

How `_wait_for_environment` works:
1. Run `p = probe(cfg, route_gates(cfg, run.state["pipeline"]), executor.worktree)`.
2. If `p.ok`, emit `stop_classified(action="probes_green")` and return `True`. The step reopens at once, as today. The probes can't see every environment fault, and the resolver's attempts still bound the loop.
3. Otherwise, emit `environment_waiting`. If no earlier `environment_waiting` in this run had the same `digest`, call `notify_run(cfg, run, f"{key} waits on this machine: {p.lines[0]}. The run goes on by itself once it is fixed.")`. A repeat of the same problem notifies nobody.
4. Emit `run_held(key=key, reason="environment", until=<give-up deadline, ISO>)`.
5. Loop. Wait with `executor._wait_until(now + ENV_POLL_S)` and probe again.
   - **Green:** emit `run_hold_ended` and `stop_classified(action="environment_fixed")`, then return `True`.
   - **Halted:** emit `run_hold_ended` and return `False`, with no notification.
   - **Past `recovery.give_up_h`:** emit `run_hold_ended` and return `False`. `take_over`'s existing path then notifies a human with the stop's summary.

- [ ] **Step 1: Write the failing tests** in `test_autonomy.py`. In each one, monkeypatch:
  - `agentflow.autonomy.ENV_POLL_S` to 0;
  - `agentflow.preflight.probe` to a stub that returns red `Probe` objects (same digest) for the first N calls and green ones afterwards;
  - `agentflow.autonomy.notify_run` to collect its messages.

  The tests:
  - `test_an_environment_stop_holds_until_the_probes_pass`: the resolver returns `{"status": "done", "summary": "pnpm is broken", "stop_class": "environment"}`, and the probes are red twice. Then check that:
    - the step finishes;
    - there is one `run_held` with `reason == "environment"`, and one `run_hold_ended`;
    - no `hitl_raised` event was added after the original stop;
    - exactly one message names `waits on this machine`.
  - `test_the_same_environment_problem_twice_notifies_once`: the step blocks twice, and the resolver says `environment` each time with the same digest. Expect two `environment_waiting` events and one notification. This is spec T1's "one inbox item, not two": the only human-facing message is the one notification, and there is no inbox item while the probes can still go green.
  - `test_green_probes_reopen_the_step_at_once`: the stub is always green → `stop_classified` with `action == "probes_green"`, and no `run_held`.
  - `test_a_pause_ends_the_environment_wait_quietly`: the stub calls `run_control.pause(run, "me")` on its second call → `advance` returns `"paused"`, `run_hold_ended` is logged, and no message was collected after the first.
  - `test_the_wait_gives_up_after_give_up_h`: set `recovery.give_up_h` to 0 → the stop goes to a human (`notify_run` is called with the stop's summary), with `run_hold_ended` logged.
- [ ] **Step 2: Write the KPI and status tests.**
  - `test_metrics.py`, `test_an_environment_hold_is_human_time`: events with `run_held{reason: environment}` → `run_hold_ended` 100 s later → `buckets["human"] == 100` and `buckets["hold"] == 0`.
  - `test_metrics.py`, `test_a_ci_pending_hold_is_ci_time`: the same, but the reason `ci_pending` goes to `ci`.
  - `test_state.py`, `test_a_hold_shows_as_held_while_a_stop_waits`: a run whose status is `waiting_human`, with `hold` set and a live lock (monkeypatch `state.lock_state` to `"live"`) → `display_status(run) == "held"`.
- [ ] **Step 3: Run them to make sure they fail.**
  Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_autonomy.py agentflow/tests/test_metrics.py agentflow/tests/test_state.py --import-mode=prepend -q`
  Expected: FAIL.
- [ ] **Step 4: Implement it** as specified.
- [ ] **Step 5: Run the tests,** then `test_signals.py`, `test_scorecard.py` and `test_dashboard.py`.
  Expected: PASS.
- [ ] **Step 6: Write the docs.**
  - Playbook §7: an `environment` stop holds the run (`held`), re-checks the machine every 2 minutes, notifies once per distinct problem, and resumes the step by itself once the checks pass. After `recovery.give_up_h` it is a human's.
  - `CHANGELOG.md` `### Added`.
- [ ] **Step 7: Commit:** `git commit -m "Hold a run on an environment stop until the machine checks pass, notifying once per distinct problem."`

---

### Task 5: A stop raised again in the same words notifies nobody again

**Files:**
- Modify:
  - `agentflow/hitl.py`: `stop_fingerprint`, `raise_hitl`, `_how_to_go_on`;
  - `docs/agentflow-playbook.md`: the `blocked_repeat` row;
  - `CHANGELOG.md`.
- Test: `agentflow/tests/test_hitl.py`

**Interfaces:**
- Produces:
  - `hitl.stop_fingerprint(kind: str, key: str, summary: str) -> str`: the sha256 hex of `f"{family}\0{key}\0{text}"`, where:
    - `family` is `"blocked"` for both `blocked` and `blocked_repeat`, and `kind` for anything else;
    - `text` is `" ".join(summary[:2000].split()).casefold()`.
  - `raise_hitl` counts the earlier `hitl_raised` events of the run whose `stop_fingerprint(kind, key, summary)` (recomputed, so logs written before this slice count too) equals this one's. It records `times=<that count + 1>` on the new `hitl_raised`.
  - When `times > 1`:
    - no notification is sent. `hitl.remind_due` still reminds after `hitl.remind_after_h`;
    - the inbox's "how to go on" gains the first line: ``This exact stop was raised {times} times in this run, so approving it again will not fix it: fix the cause, then `agentflow retry {run.id} {key}` starts a fresh session.``

- [ ] **Step 1: Write the failing tests** in `test_hitl.py`:
  - `test_blocked_and_blocked_repeat_share_a_fingerprint`: the same key and the same summary up to spaces and case → equal. Another key → different.
  - `test_the_same_stop_twice_notifies_once`: monkeypatch `hitl.notify` to collect its messages. `raise_hitl` → `approve` → `raise_hitl` with the same kind, key and summary → one notification; the second event has `times == 2`; the inbox text contains `raised 2 times`.
  - `test_a_different_summary_notifies_again`: the second summary differs → two notifications.
- [ ] **Step 2: Run them to make sure they fail.**
  Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_hitl.py --import-mode=prepend -q`
  Expected: FAIL (`stop_fingerprint` isn't defined).
- [ ] **Step 3: Implement it** as specified.
- [ ] **Step 4: Run the tests,** then `test_autonomy.py`, `test_control_cmds.py` and `test_dashboard.py`.
  Expected: PASS.
- [ ] **Step 5: Write the docs.** Update the playbook's `blocked_repeat` row: a stop raised again in the same words is notified once, and its inbox item says how many times it came back. Add a `CHANGELOG.md` `### Changed` line.
- [ ] **Step 6: Commit:** `git commit -m "Notify a stop raised again in the same words only once, and say in its inbox item how often it came back."`

---

### Task 6: CI that is still pending is a watch, not a human item

**Files:**
- Modify:
  - `agentflow/ci_lane.py`: `_wait`, and the module docstring step 1;
  - `agentflow/config.py`: `"watch_max_h"` added to `CI_KEYS`;
  - `agentflow/defaults/config.yaml`: `ci.watch_max_h: 24`;
  - `agentflow/module_queue.py`: `_assess`;
  - `agentflow/config_edit.py`: the CI section;
  - `docs/agentflow-playbook.md`: the `ci_failed` row, and the §14 `ci` line;
  - `CHANGELOG.md`.
- Test: `agentflow/tests/test_ci.py`, `agentflow/tests/test_next_queue.py`, `agentflow/tests/test_config_edit.py`

**Interfaces:**
- Consumes: the `ci_pending` hold reason, which `metrics` charges to `ci` (Task 4).
- Produces:
  - `ci.watch_max_h: float` (default 24; `0` restores today's `ci_failed` stop at `ci.timeout_min`). `validate` checks it through the existing `CI_KEYS` loop: a number, 0 or more.
  - `ci_lane.HOLD_REASON = "ci_pending"`.
  - `module_queue.FREE_HOLDS = ("ci_pending",)`: a run whose `state["hold"]["reason"]` is listed doesn't occupy a queue slot.

How `_wait` changes: when `waited >= timeout_min` with checks still pending, and `error` is empty, the wait becomes a watch if `watch_max_h > 0` and `waited < timeout_min + watch_max_h * 60`:
- On the first such poll of this wait, emit `run_held(key=key, reason="ci_pending", until=<deadline ISO>)`, and notify nobody.
- Keep polling every `poll_s`. When the checks settle, emit `run_hold_ended` before returning `pass` or `fail`.
- A halt emits `run_hold_ended` and returns `"stopped"`.
- Past the deadline, emit `run_hold_ended` and raise today's `ci_failed` stop, with the summary `CI did not settle within {timeout_min} minutes and {watch_max_h} h of watching: {n} check(s) still pending.`

`gh` being unreachable at the timeout, and no checks after `appear_min`, stop as today.

- [ ] **Step 1: Update the existing tests.** Add `"watch_max_h": 0` to `PIPE["ci"]` and `PATIENT["ci"]` in `test_ci.py`, so `test_checks_that_never_settle_stop` keeps pinning today's stop.
- [ ] **Step 2: Write the failing tests** in `test_ci.py`:
  - `test_pending_checks_past_the_timeout_are_watched`: `WATCH = {**PIPE, "ci": {**PIPE["ci"], "watch_max_h": 1}}`, and `gh.play(PENDING, PENDING, PASS)` → `go == "done"`. Check that the PR is marked ready, that there is one `run_held` with `reason == "ci_pending"` and one `run_hold_ended`, and that no `hitl_raised` was logged.
  - `test_the_watch_gives_up_after_watch_max_h`: `watch_max_h` is `0.0001` and `ci_lane.time.monotonic` is monkeypatched to advance 60 s per call → `waiting_human`; the summary contains `of watching`; `run_hold_ended` is logged.
  - `test_a_pause_ends_the_ci_watch`: like `test_a_pause_ends_the_wait_and_a_resume_picks_it_up`, under `WATCH`, pausing on the second poll → `"paused"`, with `run_hold_ended` logged.
- [ ] **Step 3: Write the queue and form tests.**
  - `test_next_queue.py`, `test_a_run_watching_ci_frees_its_slot`: a running run with `state["hold"] == {"reason": "ci_pending", ...}` and a live lock doesn't count against `max_parallel=1`, so the next ready module starts.
  - `test_config_edit.py`, `test_the_form_sets_the_ci_watch`: `(["ci", "watch_max_h"], 6)` saves; `-1` is refused (`EditError`).
- [ ] **Step 4: Run them to make sure they fail.**
  Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_ci.py agentflow/tests/test_next_queue.py agentflow/tests/test_config_edit.py --import-mode=prepend -q`
  Expected: the new tests FAIL; the updated old ones PASS.
- [ ] **Step 5: Implement it** as specified, with the form field `(("ci", "watch_max_h"), "Watch pending checks after the timeout for (h, 0 = stop)", _NUM)`.
- [ ] **Step 6: Run the tests.**
  Expected: PASS. Then `bin/agentflow validate` prints `0 warnings`.
- [ ] **Step 7: Write the docs.**
  - The playbook's `ci_failed` row: checks still pending after `ci.timeout_min` are watched (`held`) for up to `ci.watch_max_h` hours before they stop.
  - The §14 `ci` line names `watch_max_h`, and says a run watching CI holds no queue slot.
  - `CHANGELOG.md` `### Changed`.
- [ ] **Step 8: Commit:** `git commit -m "Watch CI checks still pending after ci.timeout_min instead of stopping for a human, and free the run's queue slot meanwhile."`

---

### Task 7: The whole slice

- [ ] **Step 1: Run the full suite.**
  Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests --import-mode=prepend -q`
  Expected: PASS (25–60 min).
- [ ] **Step 2: Validate the packs.** Run `bin/agentflow validate`; it prints `0 warnings`. Run `bin/agentflow doctor`; preflight's renames leave its output unchanged.
- [ ] **Step 3: Check the goldens.** Run `git diff --stat agentflow/tests/fixtures/explain/`. Expected: no output.
- [ ] **Step 4: Check the measurement** (spec §6). `bin/agentflow scorecard` still compares against the T0 baseline, unfrozen, and none of `lead_time_h`, `human_blocked_share` and `full_suite_runs` has disappeared from its rows.
