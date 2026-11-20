# agentflow cost, slice C6 (the usage-window governor) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The runner reads the newest usage-window reading that any session on the machine has reported, and schedules against it.
- Above `usage.five_hour_soft` (or `seven_day_soft`), no new run, pre-run sizer or new phase starts.
- Above `usage.five_hour_hard`, no new session starts.
- Held runs go again closest to `finish` first.
- `observe` only records.

**Architecture:**
- **One new module, `agentflow/governor.py`.** It holds two things:
  - a pure `verdict(usage, windows, now)` that answers `clear`, `soft` or `hard`;
  - `WindowFile`, the machine-wide `usage_window.json`. It carries a schema version, the readings, the live sessions, the runs waiting on the governor, and the interactive-use note. It is written under a short, non-blocking `flock`, through `safeio`, and replaced by rename, so reads need no lock. Every entry is checked when it is read, and a bad one is dropped.
- **Readings go in.** `agents.py` keeps each invocation's first and last reading per window (`AgentOutcome.first_readings` / `last_readings`), together with the top-level refusal.
  - The two places that run an agent, `lanes._call` and `sizing_lane._sizer_call`, wrap the call in `WindowFile.enter_session` / `exit_session`.
  - They also record every raw reading as it streams (`AgentCall.on_reading`), with one non-blocking try, so a file problem never touches a session that is already running.
- **Holds come out.** A new mixin, `agentflow/governor_lane.py`, gives `Executor` one method, `govern(key, mark, phase)`. It holds the run (`run_held`, reason `usage window`), heart-beats a waiter entry, polls the file, and applies finish-first among waiters whose own hold has cleared. It returns False only when a human paused or cancelled the run during the hold. It has three call sites:
  - `lanes._loop` calls it with `hard` before every fresh Claude session;
  - `sizing_lane._sizer_call` calls it with `hard` before every Claude sizer;
  - `pipeline._Loop` calls it with `soft` before a not-started run's route triage, and before every `phase_started` except a phase that holds `finish` or `ci`.
- **Starts are refused.** `governor.start_gate(usage)` gives the reason a new run must wait, or None. The queue uses it for its starts and its retro, and its bookkeeping goes on. The Backlog answers 409, `next` prints the reason, and `run` prints a note.
- **The header.** `views.board` adds a `usage` block, and a pure `usageLabel` in `board.js` renders it as a header pill, plus the interactive note or a "window file unreadable" warning.

**Tech Stack:** Python 3.12 stdlib (`fcntl`, `json`, `math`, `os`) and PyYAML. Tests use pytest with `tests/fake_agent.py`. Its `windows` act already emits a `rate_limit_event` with `unifiedWindows`, and this plan gives it a `resets_at` act key. Node runs `test_dashboard_js.py`.

**Spec:** `docs/specs/agentflow-cost-proposal.md`:

| Section | What this plan implements from it |
|---|---|
| §3.7 L7 | all of it: the four keys, `observe`, the shared file, harness-only decisions, the interactive warning (Q5), headroom in the user layer |
| §4 | the rows `usage.governor`, `usage.five_hour_soft` / `five_hour_hard` / `seven_day_soft`, `usage.finish_first`, and `usage.*` in `layers.USER_KEYS` |
| §7 | the "C6 Governor" row |
| §8 | "the governor idles a machine…": `observe`, and per-user thresholds in the user layer |
| Verification | "no run held mid-phase by the usage window" is an outcome goal, not a rule: L7's own hard mark holds the first session of a mid-phase step, and the soft mark exists so the hard one is rarely reached (Decided 2). The Finish step's real-run check reports mid-phase holds; a day of parallel runs is the post-merge check, as for every C slice |

## Decided in this plan

The spec leaves these choices open, and the doubt review (2026-11-20) reshaped several of them. Confirm them at review. Task 1 records each one in the spec as an "Amended for the C6 plan" note under L7.

1. **"Above" means strictly greater.** A window governs when `utilization > mark`.
2. **What the hard mark holds.** It holds the first call of every Claude step, which includes the resolver, the record step and the retro. That first call is held even when it continues a shared session (`small_plan.shared_session`, a batch, a C5 carry), because a new task is new work. It also holds a fresh session after a rollover or a replaced brief, and every Claude sizer and route triage.
   - **The test in `_loop`:** `turn.resume is None`, or the step has no recorded call yet (`attempts == 0` in its state row).
   - **A resume within a step is not held.** That covers a fix attempt, a reminder, a wrap-up and a refine resume. It continues the step's warm session, and a refusal there is the CLI's own `usage limit` hold, as before.
   - **Why:** the spec says "no new session". A multi-hour hold in the middle of a step would expire the cache and force the full rewrite that §1.4 describes.
   - **Codex isn't held.** A Codex step or sizer is never held by the hard mark, because the window is Claude's.
3. **What the soft mark holds.**
   - **Inside a run:**
     - the route triage of a run that has started no phase;
     - every `phase_started`, checked after `skip_if`.
   - **Never held:** a phase that holds a `finish` or `ci` step (a new `pipeline._holds_ship(steps)`, recursive like `config._holds_finish`, which stays unchanged because the `ci.early_pr_after` check uses it), so a run that is done with review still opens its PR. A phase already started also keeps going: its steps, its refine and reconcile rounds, and a phase resumed mid-way.
   - **A `skip_if` that asks an agent** (`on_unknown: agent`) runs before the soft gate. Its session is fresh, so the hard mark still holds it.
   - **Every agent.** The soft mark holds runs of any agent, because a phase may use Claude. `usage.governor: off` is the escape hatch.
   - **Outside a run:**
     - the queue starts no module and no retro, but still runs its assessment and bookkeeping;
     - the Backlog answers 409;
     - `next` adds one line;
     - `run` starts the run and prints one line, but only when a window governs, and the run waits at its first phase. `resume` and `retry` go through the same in-run gates.
4. **Refusal.** A reading counts as `hard` when its utilization is above the hard mark, or when it is `refused`. A reading is `refused` when the `rate_limit_event`'s top-level `status` is `rejected` and its `rateLimitType` names that window.
   - A per-window `status: rejected` alone does not count. The engine already treats only the top-level status as a refusal (`StreamAccumulator.interruption`), because extra usage can still cover a call.
   - **An unverified shape.** No real rejection has been seen yet (`test_agents.py`: "a real rejection has not been seen yet").
     - **A missing entry:** when a top-level `rejected` names a window in `SPANS` that has no `unifiedWindows` entry, the reading is built from the top-level fields, `{utilization: 1.0, resets_at: <top-level resetsAt>, refused: true}`.
     - **An unknown type:** a `rateLimitType` outside `SPANS` (for example `seven_day_opus`) is not recorded. The CLI's own hold still covers that run.
5. **The shared file.**
   - **Where it lives:** `${XDG_STATE_HOME:-~/.local/state}/agentflow/usage_window.json`, with a sibling `usage_window.lock`. The file is machine-wide, never under a pack's `paths.state_dir`, and holds `"version": 1`.
   - **Valid readings.** A reading `{utilization, resets_at, refused, ts}` is kept only when all of these hold. `record` sets `ts` itself, so a reading passed in needs no `ts`.
     - `utilization` is a finite number of at least 0, and a value above 1.5 is clamped to 1.5, so an overage reading still governs;
     - `resets_at` is a whole number with `now - SLACK_S < resets_at ≤ now + span + SLACK_S`, where `span` is 5 h for `five_hour` and 7 d for `seven_day`;
     - `refused` is a bool.

     Anything else is dropped, both when it is recorded and when it is read, so a planted or buggy far-future reading governs nothing.
   - **Accounts.** Readings are kept per account: `windows: {account: {window: reading}}`.
     - **The key:** `governor.account_of(cfg)` gives the first 12 hex characters of the sha256 of the effective `CLAUDE_CONFIG_DIR` (from `run_env.base(cfg)`, then `cfg.defaults.env`, then `cfg.agents["claude"].env`), or of `"default"` when it is unset.
     - **Who reads what:** a run, the queue and the Backlog read only their own account's readings. So a second account on the machine neither holds this one nor hides from it.
   - **Merging.** A reading for a later period replaces the stored one, and a reading for an earlier period is ignored. In the same period, the higher utilization wins, and `refused` sticks.
     - **Why the highest wins, not the newest.** This reads the spec's "the newest utilization" as "the highest in the period". Parallel runs report out of order, and utilization only rises within a period.
     - **The trade-off:** a lower reading after a plan upgrade waits for the reset.
     - **Not yet verified:** `seven_day`'s `resetsAt` stability. If it jitters, "a later period replaces" lets the newest jittered value win, which is benign.
     - *Evidence:* `resetsAt` is identical within a period. Across the 5 runs' transcripts, 1,217 `five_hour` readings fall on 11 distinct values, all of them on hour or half-hour boundaries.
   - **Staleness.** A reading governs until `resets_at + SLACK_S` (60 s), so a machine where every run is held still wakes after the reset.
   - **Failing open.** The governor fails open, and the CLI's own `usage limit` hold stays the backstop. That happens when:
     - the file is missing, broken, oversized, a symlink, or of another version;
     - the lock can't be taken within `LOCK_WAIT_S` (2 s).

     A file that parses with an integer `version` other than 1 is never written over. A newer engine owns that file, and this one fails open until the machine runs one engine version. A file that doesn't parse, or has no `version`, is replaced only by a write that brings readings (`record`, `exit_session`). A heartbeat (`wait`, `unwait`, `enter_session`) skips the write on a file that `read()` reports as an error, so it never wipes the readings that keep a hold in place. `read()` reports `error` when the file exists but can't be used, and the header shows it.
   - **The lock file.** After opening it, `_lock` checks that it is a regular file with `st_nlink == 1`. A lock file that is hardlinked (for example to `runs/queue.lock`) fails open, so it never contends with another lock.
   - **The state root.** `pack.user_state_root()` keeps `state_dir`'s rule: `XDG_STATE_HOME` counts only when it is absolute, else `~/.local/state`. It returns the path unresolved, so `safeio.ensure_dir` still sees, and refuses, a symlinked folder. `state_dir` resolves it as before.
   - **Writing.** The file is written to a temp file and renamed under the lock, so a killed writer never leaves a torn file. The temp file is created exclusively (`safeio.write_new` at `usage_window.<uuid>.tmp`), so a planted hardlink is never written through. Each public writing method takes the lock once and calls unlocked `_load` / `_store`.
   - **Reading takes no lock.** Writes are atomic renames, so `read()` never waits on a held lock.
   - **Denied to agents.** Every run session's `--settings` denies `Edit(/<user_state_root>/usage_window.*)` next to its existing user-config deny. That covers the file, the lock and the temp files. Bash isn't bound by it (Decided 12).
6. **Where readings come from.**
   - Every raw reading during a call, not only the `usage_window` events, which `WINDOW_STEP` deduplicates against the run's own last record. A new `AgentCall.on_reading: Callable[[dict], None] | None` is called from the stream loop whenever `acc.last_readings` changes. It is wrapped in `except Exception`, makes one non-blocking lock try, and drops the reading if the lock is busy.
   - The invocation's last reading per window when the call ends, with the normal lock wait, from run steps and sizers alike.
   - Recording happens whatever `usage.governor` says, under the run's account (Decided 5). So a pack set to `off` still shows its spend to every other run on the same account. `off` means "never hold", nothing more.
   - **Interactive-use false positives.** An engine from before C6 on the same machine records nothing, so its spend can show up as interactive use in the note. The note is a warning only.
   - *Evidence:* two 220-turn implement transcripts carry 5 and 8 `rate_limit_event`s spread through the session, the last a line before the result.
7. **The shape of a hold.**
   - **Events:** `run_held {key, reason: "usage window", until, level, window, utilization}`, then `run_hold_ended {key}`.
     - `key` is the step key under the hard mark, the sizer's key for a sizer, and `<phase>/governor` at a phase boundary or before triage.
     - `until` is the governing reading's `resets_at + SLACK_S`, as ISO text.
   - **Polling.** The run polls every `governor.POLL_S` (30 s). A pause or a cancel ends the wait (`run_hold_ended`), and the run then stops without calling anything.
   - **What each poll re-reads.** Each poll re-reads the usage config through `governor.current_usage(cfg) -> Usage`. It reloads the layers `cfg` came from and runs `validate`, and it keeps the last good `Usage` when the load fails or `validate` reports an error at any `usage.*` path.
     - **Who uses it:** `govern`, every tick of `run_queue`, and every Backlog `begin_start`.
     - **What that gives:** setting `usage.governor: off` in the pack or the user layer releases held runs, and the queue's and the Backlog's refusals, with no restart.
     - **What it doesn't reload:** only `usage` is taken. The rest of the config keeps the pipeline's own reload rules.
   - **A read that fails during a hold** keeps the last verdict while `now < v.until`, so a broken file never ends a hold early, and never holds past the reset.
   - **Failing open:** `govern` wraps its body so that any unexpected exception fails open (it ends the hold and returns True). It never raises a `runner_error`.
   - **Notification.** There is one notification per hold.
   - **Signals and metrics.** The `usage_holds` signal still counts only `usage limit`, and metrics charge the time to `hold`.
   - **A known gap.** A run that moves from "governed" to "waiting on finish-first" keeps its first `until`.
   - **Kept awake.** `keepawake` keeps the machine awake through a hold, as it does through a `usage limit` hold today.
8. **Finish first.**
   - **Waiter entries.** A held run keeps a waiter entry: `{pid, run_dir, rank, since, seen, ready, ready_since}`.
     - `seen` is refreshed at every poll.
     - `ready` is True once the run's own verdict has cleared, so it waits only on finish-first.
     - `ready_since` is when it last became ready. It is cleared whenever `ready` goes back to False.
   - **When an entry counts.** An entry counts only when all of these hold:
     - it is `ready`, as a bool;
     - `seen`, `since` and `ready_since` are finite numbers, none of them above `now + SLACK_S`;
     - `now - seen ≤ FRESH_S`, which is 90 s, its own constant, not derived from `POLL_S`;
     - `now - ready_since ≤ READY_MAX_S`, which is 600 s, so even a fresh ready entry stops counting after 10 minutes;
     - `rank` is a list of exactly two finite numbers in `[0, 1] × [0, 10**6]`;
     - `pid` is an int (not a bool) with `1 < pid < 2**22`, and that pid is alive.

     An entry that fails any of these is ignored, and the next write prunes it. `ahead` and `start_gate` never raise, so a bad entry fails open.
   - **Who goes first.** A run waits only behind counting entries that rank ahead of it. The rank is `(phase fraction, open tasks)`, compared in this order:
     - the higher `index / len(phases)` goes first;
     - then the fewer open tasks;
     - then the earlier `since`.

     A phase that isn't in `cfg.phases` (the retro) counts as fraction 0. Open tasks that can't be read count as `10**6`.
   - **Why a still-governed waiter blocks nobody.** A run held by another window, or by another pack's marks, never blocks anyone. Ready entries leave within one poll, so the wait is bounded.
   - **Where it applies:**
     - **The soft gate:** a run at a phase boundary, or before triage, waits behind counting entries. So does `start_gate`, for a new run.
     - **The hard gate:** waiters are considered only once the run itself was held.
   - **`finish_first: false`:** a run goes as soon as it clears, and nobody waits on waiters.
   - **What finish-first buys, and what it doesn't.** It orders who takes the first sessions after a clear. A run goes about one poll after the one ahead of it, so the one closest to `finish` reports the new period's first reading before the others start. It doesn't meter the window after that: the soft and hard marks do. This narrows the spec's "goes first" to an ordering of the release.
9. **`observe`.**
   - It holds nothing and keeps no waiter entry.
   - It records `usage_governed {key, level, window, utilization, until}` once per key, level and `until`. `state.usage_governed` keeps the last one.
   - `start_gate` returns None in `observe` and `off`.
10. **The interactive note (Q5).**
    - **When a session starts:** `enter_session` registers it (`pid`, run, `since`) and remembers two things:
      - whether any other live agentflow session was open (`alone`), where a session entry counts while its pid is alive and `since` is within 6 h;
      - the stored readings (`baseline`).
    - **When it ends:** `exit_session` sets `interactive {window, rise, from, to, since, at, resets_at}` when all of these hold:
      - the session was `alone`;
      - its first reading is in the same period as the baseline;
      - that reading is at least `INTERACTIVE_RISE` (0.05) above the baseline.
    - **Display.** The header shows the note until its `resets_at`.
    - **Trade-off.** This is narrower than the spec's "rose faster than the runs' own spend explains". It only fires across a gap with no agentflow session open, so it stays quiet while parallel runs overlap. Measuring the runs' spend against utilization needs a calibration the engine doesn't have, and this rule has no false positive from the runs' own spend.
11. **The keys.**
    - **`usage.governor`** is `on`, `observe` or `off`.
      - YAML reads a bare `off` as `false`, so the loader maps a bool to `"off"` or `"on"`, and `validate` warns "quote it".
      - Any other value is a `validate` error, and runs as `"on"`.
    - **The marks** load with `_float`, so a bool is a load error, as for every number key. `validate` checks that each is finite, that `0 < five_hour_soft < five_hour_hard ≤ 1`, and that `0 < seven_day_soft ≤ 1`.
    - **`finish_first`** loads as a bool, and anything else is a load error.
    - **Unknown keys.** Any other key under `usage` is a `validate` error.
    - **A reserved step id.** `validate` reserves `governor` as a step id, next to the sizers' and the triage's reserved ids (`config.py` ~1700), because `<phase>/governor` is the hold key.
13. **The spec's Verification bullet** ("a day of parallel runs ends with no run held mid-phase by the usage window") is an outcome goal, not a rule. L7's own hard mark holds the first call of a mid-phase step (Decided 2). The soft mark exists so the hard one is rarely reached. The Finish step's real-run check reports any mid-phase hold. Task 1 copies this decision into the spec too, next to that bullet.
    - **Where the keys live:** the engine defaults carry the block, and `layers.USER_KEYS` gains `usage.*`. The form edits all five keys, and none of them is protected.
12. **Trust.** The file is a scheduling aid, not a security boundary.
    - **Who can write it:** any process under the developer's account, a run's `bypassPermissions` build agent included.
    - **What Decided 5 and 8 bound:** a one-shot planted reading governs for at most one window span. For a `seven_day` reading that is up to 7 days, which a developer ends by deleting the file, or with `usage.governor: off`, which every poll re-reads. A planted waiter counts for at most `READY_MAX_S`, unless something keeps rewriting it. Neither can crash a run.
    - **Accounts:** readings are kept per account (Decided 5). A second account is told apart only by its `CLAUDE_CONFIG_DIR`. Two accounts that share one config dir (a login switched in place) look like one.
    - **The queue's retro.** It runs inside the queue's own loop, so a retro session that the hard mark holds also holds the queue's ticks until the window clears. The queue starts a retro only when its verdict is `clear`, which leaves a narrow race. That race is accepted.
    - **Packs:** a pack's `usage.*` can loosen the marks for its own runs. That adds nothing to what a pack can already do, since a pack runs its own gate commands.

## Global Constraints

- Engine dependencies stay stdlib + `pyyaml>=6`.
- No project strings in the engine (`test_portability.py`'s `DENYLIST`).
- The golden files `agentflow/tests/fixtures/explain/*.txt` stay byte-identical; `explain` shows no new step.
- Every new config key ships with: a Settings form field (`config_edit.py`), `validate` checks, a `tests/test_config_edit.py` case, a playbook §14 line.
- `CHANGELOG.md` gets one `Added` line under `## [Unreleased]`.
- **Defaults (§4):**
  - `usage.governor: "on"`
  - `five_hour_soft: 0.75`
  - `five_hour_hard: 0.92`
  - `seven_day_soft: 0.85`
  - `finish_first: true`
- A hold is a harness decision over a stream signal. No model decides one.
- **Nothing the governor does may raise into a session that is running, or block one for more than one lock try.**
- **Security overlay.** This slice changes `agents.py`, `pack.py` (`user_state_root`) and `layers.py` (`USER_KEYS`). Plan review and branch review therefore use `security-and-hardening` and the `security-auditor` agent.
- `uvx ruff@0.17.4 check .` prints `All checks passed!` and `bin/agentflow validate` prints `0 warnings`.

## Review Focus

1. **A reset with nobody calling.** Every run is held, so no session writes a new reading. The stored reading must read as stale once `resets_at + SLACK_S` has passed, and the runs must go. Task 2 tests `verdict` with a past `resets_at`. Task 4 tests a hold that ends at the reset with no new reading.
2. **A dead, stale or planted waiter.**
   - **The cases:**
     - a runner killed during a hold;
     - an entry whose pid belongs to another process (pid 1, a reused pid);
     - an entry whose heartbeat is old;
     - an entry with a non-int pid.
   - **The expected behaviour:** none of these blocks a run or the queue. Task 2 tests `ahead` and `start_gate` with each.
3. **A pause or cancel during a governor hold.** `govern` must return False within one poll, write `run_hold_ended`, make no agent call, and leave no waiter entry. Task 4 tests it.
4. **A malformed file while a session runs.** A file holding NaN, `Infinity`, a string pid, a list where a dict belongs, a far-future `resets_at`, or a held lock must not stop, stall or crash the running session, `start_gate`, `board()` or `describe`. Tasks 2 and 3 test it.
5. **Two writers, and a killed writer.** Two runners record at once: the file must never tear, and a lower reading of the same period must never overwrite a higher one. A file left half-written must read as empty, not raise. A symlink at the file or at the lock must never be followed. Task 2 tests all of these.

---

### Task 1: The `usage.*` keys, their checks and their docs

**Files:**
- Modify: `agentflow/config.py`:
  - a `USAGE_KEYS` tuple beside `WATCHDOG_KEYS` (~214);
  - a `Usage` dataclass beside `Watchdog` (~333);
  - the `Config.usage` field (~660);
  - loading beside `watchdog` (~1469/1558);
  - a `usage()` validator beside `watchdog()` (~2539), called from the list (~3033).
- Modify: `agentflow/defaults/config.yaml` (a `usage:` block after `watchdog:`, with one comment per key)
- Modify: `agentflow/layers.py` (`USER_KEYS` gains `"usage.*"`)
- Modify: `agentflow/config_edit.py` (a "Usage window" form section after "Environment", ~142)
- Modify: `docs/agentflow-playbook.md` (§14: five lines after `watchdog.read_only_idle_min`, ~494)
- Modify: `docs/specs/agentflow-cost-proposal.md` (L7: an "Amended for the C6 plan" note recording Decided 1–13, with Decided 13 also next to the Verification bullet)
- Modify: `CHANGELOG.md`
- Test: `agentflow/tests/test_config.py`, `agentflow/tests/test_config_edit.py`, `agentflow/tests/test_layers.py`

**Interfaces:**
- Produces: `config.Usage(governor: str = "on", five_hour_soft: float = 0.75, five_hour_hard: float = 0.92, seven_day_soft: float = 0.85, finish_first: bool = True)`, and `Config.usage: Usage = field(default_factory=Usage)`.

- [ ] **Step 1: Write the failing tests**

The tests use `test_config.py`'s own helper `issues_for(tmp_cfg, patch)`, which returns the `validate` issues of the fixture pack with `patch` merged in. `tmp_cfg(patch)` writes that pack and returns its path.

```python
def test_usage_defaults_load(tmp_cfg):
    assert load_config(tmp_cfg()).usage == Usage("on", 0.75, 0.92, 0.85, True)

def test_a_bare_off_loads_as_off_and_is_a_warning(tmp_cfg):
    assert load_config(tmp_cfg({"usage": {"governor": False}})).usage.governor == "off"
    issues = issues_for(tmp_cfg, {"usage": {"governor": False}})
    assert [(i.level, i.path) for i in issues if "governor" in i.path] == [("warning", "usage.governor")]

def test_governor_is_a_reserved_step_id(tmp_cfg): ...  # a build step `id: governor` → error "step id 'governor' is reserved"

@pytest.mark.parametrize("block, path, message", [
    ({"governor": "sometimes"}, "usage.governor", 'governor must be "on", "observe" or "off"'),
    ({"five_hour_soft": 0.95}, "usage.five_hour_soft", "five_hour_soft must be above 0 and below five_hour_hard"),
    ({"five_hour_soft": 0}, "usage.five_hour_soft", "five_hour_soft must be above 0 and below five_hour_hard"),
    ({"five_hour_soft": float("nan")}, "usage.five_hour_soft", "five_hour_soft must be above 0 and below five_hour_hard"),
    ({"five_hour_hard": 1.2}, "usage.five_hour_hard", "five_hour_hard must be at most 1"),
    ({"five_hour_hard": float("inf")}, "usage.five_hour_hard", "five_hour_hard must be at most 1"),
    ({"seven_day_soft": 0}, "usage.seven_day_soft", "seven_day_soft must be above 0 and at most 1"),
    ({"soft": 0.7}, "usage.soft", "unknown usage key"),
])
def test_validate_refuses_bad_usage(tmp_cfg, block, path, message): ...  # an issue in issues_for(tmp_cfg, {"usage": block}) has level "error", that path, and a message holding `message`

@pytest.mark.parametrize("block", [{"five_hour_soft": True}, {"finish_first": "yes"}])
def test_a_wrongly_typed_usage_value_is_a_load_error(block): ...  # ConfigError at usage.<key>, like every number/bool key

def test_validate_takes_the_shipped_usage_block(): ...  # 0 errors, 0 warnings
```

In `test_layers.py`, add a test: a user file holding `usage: {five_hour_soft: 0.6, governor: observe}` overrides the pack, and `config show --effective usage` names the `user` layer for both keys.

In `test_config_edit.py`, next to `test_the_form_sets_the_watchdog`:

```python
def test_the_form_sets_the_usage_window(shipped):
    proposal = form(shipped, (["usage", "governor"], "observe"), (["usage", "five_hour_soft"], 0.6),
                    (["usage", "five_hour_hard"], 0.9), (["usage", "seven_day_soft"], 0.8),
                    (["usage", "finish_first"], False))
    ...  # save it
    assert load_config(shipped).usage == Usage("observe", 0.6, 0.9, 0.8, False)

def test_the_form_reports_a_soft_mark_above_the_hard_one(shipped):
    proposal = form(shipped, (["usage", "five_hour_soft"], 0.95))  # in range for the field, wrong across fields
    assert any(i.path == "usage.five_hour_soft" and i.level == "error" for i in proposal.issues)

def test_the_form_refuses_a_mark_out_of_its_range(shipped):
    with pytest.raises(EditError):
        form(shipped, (["usage", "five_hour_hard"], 1.5))
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_config.py agentflow/tests/test_config_edit.py agentflow/tests/test_layers.py --import-mode=prepend -q -k usage`

Expected: FAIL, because `Usage` isn't defined.

- [ ] **Step 3: Implement**
  - **Loading.** The block loads with `_optional(raw, "usage", "", dict, {})`.
    - `governor`: a bool maps to `"off"` or `"on"`, and any other value loads as given.
    - The marks load with `_float`, and `finish_first` loads with `_optional(..., bool, True)`.
  - **Validation.** `usage()` follows `watchdog()`: `_finite` checks and the error texts above. A `governor` that `validate` refuses runs as `"on"`. The reserved-id check gains `governor`, next to the triage's.
  - **Form fields.**
    - `governor` is `{"kind": "enum", "options": ["on", "observe", "off"]}`.
    - The marks are `{"kind": "number", "min": 0, "max": 1, "exclusive_min": True}`.
    - `finish_first` is `{"kind": "bool"}`.
    - The section text is: "When runs stop starting new work because the plan's usage window is nearly spent; the soft mark is your headroom."
  - **Playbook §14.** Add one line per key, in the `watchdog` lines' style. Each line gives the default and the range, and what the key holds (Decided 2, 3, 4, 8, 9 and 12). It says the key is a personal key, and ends "Not protected."
  - **Changelog.** `Added`: "The usage-window governor (`usage.*`): above the soft mark no new run or phase starts, above the hard mark no new session; held runs go again closest to `finish` first; the dashboard header shows the window."

- [ ] **Step 4: Run them to verify they pass.** Then run `bin/agentflow validate`: it must print `0 warnings`.

- [ ] **Step 5: Commit**

```bash
git add agentflow/config.py agentflow/defaults/config.yaml agentflow/layers.py agentflow/config_edit.py docs/agentflow-playbook.md docs/specs/agentflow-cost-proposal.md CHANGELOG.md agentflow/tests/test_config.py agentflow/tests/test_config_edit.py agentflow/tests/test_layers.py
git commit -m "Add the usage.* governor keys with their checks, form fields and docs."
```

---

### Task 2: `governor.py`: the verdict and the shared window file

**Files:**
- Create: `agentflow/governor.py`
- Modify: `agentflow/pack.py` (`user_state_root()`; `state_dir` uses it for its default)
- Test: `agentflow/tests/test_governor.py`

**Interfaces:**
- Consumes:
  - `config.Usage` (Task 1);
  - `safeio.ensure_dir`, `safeio.read_bytes`, `safeio.write_new` and `safeio.open_append`;
  - `run_env.base`, `config.load_config` and `config.validate` (for `account_of` and `current_usage`);
  - every tunable constant below is read as `governor.<NAME>` at call time, never copied into a default or imported by name, so tests can patch it;
  - `events._pid_alive`, looked up at call time, never bound as a default.
- Produces, as module constants:
  - `VERSION = 1`
  - `USAGE_HOLD = "usage window"`
  - `GOVERNOR_STEP = "governor"`
  - `POLL_S = 30.0`
  - `SLACK_S = 60.0`
  - `LOCK_WAIT_S = 2.0`
  - `FRESH_S = 90.0` (a waiter's heartbeat must be this recent)
  - `READY_MAX_S = 600.0` (a ready waiter stops counting after this long)
  - `INTERACTIVE_RISE = 0.05`
  - `SESSION_MAX_S = 6 * 3600`
  - `SPANS = {"five_hour": 5 * 3600, "seven_day": 7 * 86400}`
  - `MAX_BYTES = 256 * 1024`
- Produces, as functions and classes:
  - `pack.user_state_root() -> Path`: `${XDG_STATE_HOME:-~/.local/state}/agentflow`, resolved.
  - `window_path() -> Path`: `user_state_root() / "usage_window.json"`.
  - `@dataclass(frozen=True) Verdict(level: str, window: str | None = None, utilization: float | None = None, until: float | None = None)`.
    - `level` is `"clear" | "soft" | "hard"`.
    - `until` is the governing reading's `resets_at + SLACK_S`.
  - `valid_reading(window: str, reading: Any, now: float) -> dict | None`: Decided 5's checks. It returns the normalised `{"utilization", "resets_at", "refused", "ts"}`, or None.
  - `account_of(cfg: Config) -> str` and `current_usage(cfg: Config, last: Usage | None = None) -> Usage`: Decided 5 and 7. `current_usage` never raises.
  - `verdict(usage: Usage, windows: Mapping[str, Mapping], now: float) -> Verdict`. `windows` is one account's map: callers pass `read()["windows"].get(account, {})`.
    - It is pure, and `"off"` always gives `clear`. Only readings that are valid and not stale count.
    - `hard` beats `soft`. Between two windows at the same level, the one with the larger utilization over its mark is named.
  - `merge_reading(old: Mapping | None, new: Mapping) -> dict`: Decided 5's merge.
  - `describe(v: Verdict, usage: Usage, now: float) -> str`, which never raises.
    - The text is exactly `"the 5h usage window is at 82% (above usage.five_hour_soft 0.75) until 15:01"`.
    - A refused reading gives `"the 5h usage window refuses requests until 15:01"`.
    - The label is `5h` or `7d`.
    - The time is local `%H:%M`, or `%a %H:%M` when it is more than 24 h away.
  - `rank_of(phases: list[str], phase: str, open_tasks: int | None) -> list[float]`: Decided 8.
  - `ahead(waiting: Mapping[str, Any], me: str, my_rank: list[float], my_since: float, now: float, alive: Callable[[int], bool] | None = None) -> list[str]`: the keys of the counting entries that rank ahead of `me` (Decided 8).
  - `@dataclass(frozen=True) SessionTicket(token: str, account: str, alone: bool, baseline: dict)`.
  - `class WindowFile(path: Path | None = None)`. No method raises.
    - `read() -> dict`. It takes no lock. The result is always `{"windows": {}, "sessions": {}, "waiting": {}, "interactive": None, "error": str | None}`, holding only valid entries. `interactive` is kept only when its `window` is a key of `SPANS` and its numbers are finite.
    - `record(account: str, readings: Mapping[str, Mapping], now: float, wait_s: float = LOCK_WAIT_S) -> None`. It stamps `ts = now` on each reading.
    - `enter_session(run_key: str, account: str, now: float) -> SessionTicket`. `alone` and `baseline` are taken within that account.
    - `exit_session(ticket: SessionTicket, first: Mapping, last: Mapping, now: float) -> None`. In one lock it records `last` (as `record` does), sets the interactive note, and drops the session.
    - `wait(run_key: str, run_dir: str, rank: list[float], since: float, ready: bool, now: float) -> None`. It is also the heartbeat: it sets `seen = now`, sets `ready_since = now` when `ready` turns True, and clears `ready_since` when `ready` turns False. On a file that `read()` reports as an error it writes nothing.
    - `unwait(run_key: str) -> None`.

    Every write prunes the `sessions` and `waiting` entries that no longer count. A file whose `version` is another integer is left as it was.
  - `start_gate(usage: Usage, account: str, now: float | None = None, file: WindowFile | None = None) -> str | None`, which never raises. In `on` mode it returns:
    - `describe(...)` when the verdict is `soft` or `hard`;
    - with `finish_first`, `"N run(s) wait on the usage window and go first"` while counting entries exist;
    - otherwise None.

- [ ] **Step 1: Write the failing tests** (in `test_governor.py`; `conftest` already points `XDG_STATE_HOME` at a tmp dir)

```python
NOW = 1_800_000_000.0
U = Usage()
def w(u, refused=False, resets=NOW + 3600): return {"utilization": u, "resets_at": int(resets), "refused": refused, "ts": NOW}

def test_verdict_marks():
    assert verdict(U, {"five_hour": w(0.75)}, NOW).level == "clear"          # Decided 1: strictly above
    assert verdict(U, {"five_hour": w(0.8)}, NOW) == Verdict("soft", "five_hour", 0.8, NOW + 3600 + SLACK_S)
    assert verdict(U, {"five_hour": w(0.95)}, NOW).level == "hard"
    assert verdict(U, {"seven_day": w(0.9)}, NOW).level == "soft"
    assert verdict(U, {"five_hour": w(0.1, refused=True)}, NOW).level == "hard"  # Decided 4
    assert verdict(Usage(governor="off"), {"five_hour": w(0.99)}, NOW).level == "clear"

def test_a_reading_past_its_reset_governs_nothing():                         # Review Focus 1
    assert verdict(U, {"five_hour": w(0.99, resets=NOW - SLACK_S - 1)}, NOW).level == "clear"
    assert verdict(U, {"five_hour": w(0.99, resets=NOW - 1)}, NOW).level == "hard"

def test_an_overage_reading_is_clamped_not_dropped():
    assert valid_reading("five_hour", w(2.0), NOW)["utilization"] == 1.5

@pytest.mark.parametrize("bad", [
    w(1.0, resets=NOW + 5 * 3600 + SLACK_S + 1), w(float("nan")), w(float("inf")), w(-0.1),
    {"utilization": "0.9", "resets_at": NOW + 60}, {"utilization": 0.9, "resets_at": True}, [], None,
])
def test_an_invalid_reading_is_dropped(bad):                                   # Decided 5, Review Focus 4
    assert valid_reading("five_hour", bad, NOW) is None

def test_merge_keeps_the_newest_period_and_the_highest_reading():
    assert merge_reading(w(0.6), w(0.5))["utilization"] == 0.6
    assert merge_reading(w(0.9), w(0.1, resets=NOW + 7200))["utilization"] == 0.1
    assert merge_reading(w(0.6, refused=True), w(0.7))["refused"] is True

def test_record_then_read_round_trips(tmp_path): ...     # WindowFile(tmp_path / "f.json"); file holds "version": 1
def test_a_file_of_another_version_is_read_empty_and_never_overwritten(tmp_path): ...  # {"version": 2}: read() error set; record() leaves bytes as they were
def test_a_garbage_or_unversioned_file_is_replaced_by_the_next_write(tmp_path): ...  # b"{oops", {"windows": {}}: record() writes a version 1 file
def test_a_heartbeat_never_rewrites_an_unusable_file(tmp_path): ...  # garbage file: wait(), unwait(), enter_session() leave its bytes as they were
def test_a_hardlinked_lock_fails_open(tmp_path): ...  # lock is a hardlink to another file held LOCK_EX: record() returns at once, changes nothing
def test_a_relative_xdg_state_home_is_ignored(monkeypatch): ...  # XDG_STATE_HOME=rel → user_state_root() is under ~/.local/state, as state_dir does
def test_ahead_counts_only_ready_fresh_live_entries(): ...
    # ranks: [0.8, 2] ahead of [0.6, 0]; [0.6, 1] ahead of [0.6, 3]; equal → earlier since.
    # Not counted (Review Focus 2): ready False or "yes"; seen older than FRESH_S; seen, since or ready_since
    # above now + SLACK_S (1e999 included); ready_since older than READY_MAX_S; rank [2, 0], [0.5], ["a", 0], [inf, 0];
    # pid 0, 1, -1, True, "12", 2**22; dead pid (alive → False). A non-dict entry or a non-dict `waiting` → [] (never raises)
def test_rank_of_handles_an_unknown_phase_and_no_plan(): ...   # rank_of(["a","b"], "retro", None) == [0.0, 10**6]
def test_start_gate_reasons(tmp_path): ...    # describe() text at 0.8; waiter text with a counting entry; None for a stale entry, observe, off
def test_the_interactive_note(tmp_path): ...  # alone + first 0.50 vs baseline 0.41 → note rise ≈ 0.09 (pytest.approx);
                                              # not alone → none; rise 0.04 → none; other period → none
def test_a_held_lock_never_blocks_a_read_and_fails_a_write_open(tmp_path): ...
    # another fd holds LOCK_EX: read() returns the stored readings at once; record() returns within LOCK_WAIT_S + 0.5 s
    # and changes nothing; record(wait_s=0) returns at once
def test_a_planted_hardlink_at_a_temp_name_is_never_written_through(tmp_path): ...  # temp names are unique and exclusive
def test_two_writers_never_tear_the_file(tmp_path): ...  # 2 threads × 50 record() calls, each with its own open: read() parses and holds the max
def test_a_half_written_file_reads_empty(tmp_path): ...  # truncated JSON → empty, error set, no raise
def test_a_symlink_at_the_file_or_lock_is_never_followed(tmp_path): ...  # read empty; record() leaves each link target untouched; no raise
def test_describe_never_raises(): ...         # until NaN / 1e12 / None → a text without a time; a 7d until > 24 h away uses "%a %H:%M"
def test_state_dir_default_is_under_user_state_root(monkeypatch, tmp_path): ...  # pack.state_dir(repo).parent == pack.user_state_root()
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_governor.py --import-mode=prepend -q`

Expected: FAIL, because `agentflow.governor` doesn't exist.

- [ ] **Step 3: Implement `agentflow/governor.py` and `pack.user_state_root()`.**
  - **The lock.** Open the lock with `safeio.open_append(lock_path)`, and check `os.fstat`: `S_ISREG` and `st_nlink == 1`, else fail open. Then call `fcntl.flock(fd, LOCK_EX | LOCK_NB)`. Retry every 50 ms until `wait_s` is up, then fail open.
  - **Writing.** `_store` creates `usage_window.<uuid4 hex>.tmp` with `safeio.write_new(path, text, 0o600)`, then calls `os.replace` onto the file (the `finished.py` pattern), and removes the temp file on any failure.
  - **Reading.** `_load` reads with `safeio.read_bytes(path, MAX_BYTES)` and parses with `json.loads(..., parse_constant=reject)`. It then keeps only the entries that pass the checks. Numbers go through `math.isfinite`, because `1e999` parses as `inf`.
  - **Session tokens** are `f"{os.getpid()}:{uuid4().hex}"`.
  - **The module docstring** states Decided 1, 4, 5, 8, 10 and 12.

- [ ] **Step 4: Run the tests to verify they pass.** Also run `agentflow/tests/test_pack.py`.

- [ ] **Step 5: Commit**

```bash
git add agentflow/governor.py agentflow/pack.py agentflow/tests/test_governor.py
git commit -m "Add the governor's verdict and the machine-wide usage window file."
```

---

### Task 3: Every session reports its window readings to the shared file

**Files:**
- Modify: `agentflow/agents.py`:
  - `StreamAccumulator` (~217): add `last_readings` and `first_readings`, filled in the `rate_limit_event` branch (~509);
  - `AgentOutcome` (~161): add the same two fields, filled in `to_outcome`.

  Don't touch `StreamAccumulator.windows`, which is the run-seeded dedupe map.

  Also the `--settings` deny list (~974): add `Edit(/{pack.user_state_root()}/usage_window.*)` inside the existing `try/except RuntimeError` next to the user-config deny, so a machine with no home directory still runs (Decided 5). Add `on_reading` to `AgentCall` and call it from the stream loop (~852).
- Modify: `agentflow/tests/test_headless.py` (~156, ~168): the two exact `--settings` deny-list assertions gain the new rule. `test_agents.py:291` `test_other_calls_keep_their_argv` does too. These are the only existing tests this task changes.
- Create: `agentflow/governor_lane.py` (the `GovernorLane` mixin's metering helpers)
- Modify: `agentflow/lanes.py` (`Executor`'s bases, ~669, gain `GovernorLane`; `_call`, ~1148/1166)
- Modify: `agentflow/sizing_lane.py` (`_sizer_call`, ~478/492)
- Modify: `agentflow/tests/fake_agent.py`:
  - act key `resets_at`, the number the `windows` act sends as `resetsAt` (the default `1794910800` stays);
  - act key `refused`, which makes the `windows` act's top-level `status` `rejected`;
  - both added to the docstring.
- Test: `agentflow/tests/test_agents.py`, `agentflow/tests/test_agent_step.py`

**Interfaces:**
- Consumes: `governor.WindowFile`, `SessionTicket` and `valid_reading` (Task 2).
- Produces:
  - `AgentOutcome.last_readings: dict = field(default_factory=dict)` and `AgentOutcome.first_readings: dict = field(default_factory=dict)`. Both have the shape `{window: {"utilization", "resets_at", "refused"}}`.
    - They hold only windows whose reading has a number and a `resetsAt`.
    - `refused` is True when the event's top-level `status` is `rejected` and its `rateLimitType` is that window.
  - `GovernorLane._metered(self, run_call: Callable[[], AgentOutcome]) -> AgentOutcome`:
    - it calls `enter_session` before the call;
    - it calls `exit_session` in a `finally`, with empty readings when the call raised;
    - each of the two is wrapped in `except Exception`, so the call runs whatever the file does.
  - `AgentCall.on_reading: Callable[[dict], None] | None = None` (agents.py). The stream loop calls it with a copy of `acc.last_readings` whenever a `rate_limit_event` changed them, inside `try/except Exception`.
  - `GovernorLane._on_reading(self, readings: dict) -> None`: `record(account_of(cfg), readings, now, wait_s=0)`, wrapped in `except Exception`. `lanes._agent_call` and `sizing_lane._sizer_call` set `on_reading=self._on_reading`.

- [ ] **Step 1: Write the failing tests**

```python
# test_agents.py
def test_the_outcome_keeps_the_first_and_last_reading_per_window(): ...
    # parse three rate_limit_event lines (five_hour 0.40, 0.43, then one with no resetsAt): first 0.40, last 0.43
def test_only_the_top_level_status_marks_a_reading_refused(): ...
    # `_limit("allowed", window="rejected")` → refused False; `_limit("rejected")` → five_hour refused True

# test_agent_step.py
def test_a_session_writes_its_readings_to_the_shared_file(flow_repo):
    reset = int(time.time()) + 3600
    flow_repo.new_run()
    flow_repo.script({KEY: [{"windows": {"five_hour": 0.42, "seven_day": 0.30}, "resets_at": reset, "result": DONE}]})
    assert run_step(flow_repo) == "done"
    data = governor.WindowFile().read()
    mine = data["windows"][governor.account_of(flow_repo.cfg)]
    assert mine["five_hour"]["utilization"] == 0.42 and mine["five_hour"]["resets_at"] == reset
    assert data["sessions"] == {}

def test_a_sizer_writes_its_readings_too(flow_repo, tmp_cfg): ...  # a pack with effort.sizing: true (the fixture's is false), as test_sizing_lane.py does
def test_off_still_records_and_never_holds(flow_repo): ...       # usage.governor: "off": the windows act's reading is in the file
def test_readings_are_kept_per_account(flow_repo, tmp_cfg): ...  # defaults.env CLAUDE_CONFIG_DIR=/x: the reading lands under account_of(that cfg), and a run without it sees none
def test_a_raw_reading_mid_session_reaches_the_file(flow_repo): ... # 0.88 then 0.92 in one call (a 0.04 move, no usage_window event): the file holds 0.92 before the call ends (on_reading)
def test_a_rejection_with_no_window_entry_is_recorded_refused(): ... # test_agents.py: top-level rejected five_hour with no unifiedWindows → last_readings five_hour {1.0, resetsAt, refused True}
def test_a_run_session_may_not_edit_the_window_file(): ...        # test_agents.py: the --settings deny list of a run call holds Edit(/<user_state_root>/usage_window.*)
def test_a_broken_or_locked_window_file_never_touches_a_running_session(flow_repo): ...   # Review Focus 4
    # (a) the file holds {"version": 1, "waiting": {"x": {"pid": "1", "rank": "a"}}, "windows": {"five_hour": {"utilization": NaN}}}
    # (b) another fd holds the lock for the whole call
    # each: run_step == "done", no watchdog_fired, no runner_error, the call's argv unchanged
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_agents.py agentflow/tests/test_agent_step.py --import-mode=prepend -q -k "reading or readings or window_file"`

Expected: FAIL, because `AgentOutcome` has no `last_readings`.

- [ ] **Step 3: Implement**
  - **The stream branch.** In the `rate_limit_event` branch, `parse_claude_line` updates `acc.last_readings`, and also `acc.first_readings` the first time it sees each window. Its returned events stay as they are.
  - **`lanes._call`.** It becomes `outcome = self._metered(lambda: sr.agent.run(call, on_event))`. `on_event` doesn't change.
  - **`sizing_lane._sizer_call`.** It gets the same wrapping around `self._run_sizer(...)`, and `on_reading=self._on_reading` on its `AgentCall`.

- [ ] **Step 4: Run them to verify they pass.** Then run all of `test_agents.py`, `test_agent_step.py` and `test_headless.py`. The existing `usage_window` and `usage limit` tests must pass unchanged, and the only changed assertions are the three deny lists above.

- [ ] **Step 5: Commit**

```bash
git add agentflow/agents.py agentflow/governor_lane.py agentflow/lanes.py agentflow/sizing_lane.py agentflow/tests/fake_agent.py agentflow/tests/test_agents.py agentflow/tests/test_agent_step.py
git commit -m "Record every session's usage window readings in the machine-wide window file."
```

---

### Task 4: The holds: hard before each fresh session, soft before each phase, finish first, observe

**Files:**
- Modify: `agentflow/governor_lane.py` (`govern`, `_observed`)
- Modify: `agentflow/lanes.py` (`_loop`, ~940: before `self._call`, the hard gate applies when `sr.agent_name == "claude"` and either `turn.resume is None` or the step's state row has `attempts == 0` (Decided 2))
- Modify: `agentflow/sizing_lane.py` (`_sizer_call`: before `sizer_started`, the hard gate applies to a Claude agent; a halted hold returns `None, []`)
- Modify: `agentflow/pipeline.py`:
  - `_Loop.go`, ~325: the soft gate goes before the route triage of a run with no `phase_started`;
  - `_Loop.phase`, ~404: the soft gate goes after `skip_if` and before the reroute and `phase_started`, and is skipped when `_holds_ship(phase.steps)` is true;
  - a new module-level `_holds_ship(steps: list[Step]) -> bool`: True when a `type` of `finish` or `ci` is among `steps` or inside their groups. `config._holds_finish` stays as it is.
- Modify: `agentflow/state.py`:
  - the initial state gets `"usage_governed": None` (~75);
  - the reducer `_on_usage_governed` and its table entry (~840);
  - the required fields `"usage_governed": ("key", "level")` (~912).
- Modify: `agentflow/tests/conftest.py`: the helpers `prime(u, resets_in=3600, refused=False)` and `release()`, and the fixture `quick`.
  - `prime` records a `five_hour` reading with `resets_at = int(time.time()) + resets_in`.
  - `release` records a `five_hour` reading of 0.0 with `resets_at = int(time.time()) + 5 * 3600`. That is a later period than any `prime` with `resets_in < 5 h`, and still inside the span, so it clears the window at once.
  - `quick` patches `governor.POLL_S` to 0.01 and nothing else, so `FRESH_S` and `READY_MAX_S` keep their values. Tasks 5 and 6 use these helpers too.
- Test: `agentflow/tests/test_governor_flow.py`, a new file. It imports `DONE, KEY, make_phase, make_step, run_step` from `test_agent_step`, as `test_sizing_lane.py` does, and `begin` from `test_pipeline`.

**Interfaces:**
- Consumes (Task 2):
  - `governor.verdict`, `ahead`, `rank_of`, `describe` and `WindowFile`;
  - `USAGE_HOLD`, `GOVERNOR_STEP` and `POLL_S`.

  Also `self.plan_open_tasks()` (`RouteLane`), `notify_run` and `halted`.
- Produces: `GovernorLane.govern(self, key: str, mark: str, phase: str) -> bool`.
  - `mark` is `"soft"` or `"hard"`.
  - It returns True to go on.
  - It returns False only when the run was paused or cancelled while held. The caller then returns `"stopped"` (lanes), `None, []` (a sizer) or `self.status()` (pipeline).

`govern`'s loop, which the tests pin:

```
try:
  me, rank, since, account = str(run.dir), rank_of(phases, phase, plan_open_tasks()), time.time(), account_of(cfg)
  loop:
    now = time.time()                                                # every iteration
    usage = current_usage(cfg, usage)                                # every poll: `off` releases a held run
    off → return True
    data = file.read(); windows = data["windows"].get(account, {})
    v = verdict(usage, windows, now), but while held and data["error"] is set, the last v if now < last v.until
    governs = v.level == "hard" or (mark == "soft" and v.level == "soft")
    observe → (governs → _observed(key, v)); return True              # always returns: observe never holds
    queued = finish_first and (held or mark == "soft") and ahead(data["waiting"], me, rank, since, now)
    governs or queued:
        not held → emit run_held(key, reason=USAGE_HOLD, until=iso(v.until or now), level=v.level, window, utilization), notify once
        file.wait(me, run_dir, rank, since, ready=not governs, now)  # heartbeat every poll
        sleep POLL_S in steps, re-reading the run's status; halted → return False
    else → return True
  # (POLL_S, SLACK_S, FRESH_S read as governor.<NAME> each time)
except Exception → return True                                       # fails open; never a runner_error
finally: held → try: file.unwait(me); emit run_hold_ended(key) except Exception: pass
```

- [ ] **Step 1: Write the failing tests**

```python
KEY2 = "build/second"

def second_step(flow_repo):  # a second agent step in the same phase, with its own key
    step = make_step(); return flow_repo.executor().run_agent_step(make_phase(step), step, KEY2)

def test_a_reading_at_095_holds_the_next_fresh_session_until_the_window_clears(flow_repo, quick):   # spec §7
    flow_repo.new_run()
    flow_repo.script({KEY: [{"windows": {"five_hour": 0.95}, "resets_at": int(time.time()) + 3600, "result": DONE}],
                      KEY2: [{"result": DONE}]})
    assert run_step(flow_repo) == "done"                      # the session already running is never cut
    threading.Timer(0.3, release).start()
    assert second_step(flow_repo) == "done"
    (held,) = flow_repo.events(flow_repo.run, "run_held")
    assert held["reason"] == "usage window" and held["level"] == "hard" and held["key"] == KEY2
    assert held["utilization"] == 0.95 and flow_repo.run.state["hold"] is None
    assert governor.WindowFile().read()["waiting"] == {}

def test_a_resume_is_not_held(flow_repo, quick): ...
    # nothing primed; the step's first call streams {"windows": {"five_hour": 0.95}, "resets_at": now + 3600} and
    # ends without a result → the reminder resumes that session: no run_held, two calls, the second with --resume
def test_a_reading_at_08_holds_a_new_phase_but_not_a_step(flow_repo, quick):                     # spec §7
    flow_repo.new_run()
    flow_repo.script({KEY: [{"windows": {"five_hour": 0.8}, "resets_at": int(time.time()) + 3600, "result": DONE}],
                      KEY2: [{"result": DONE}]})
    assert run_step(flow_repo) == "done" and second_step(flow_repo) == "done"    # steps in a started phase go on
    assert flow_repo.events(flow_repo.run, "run_held") == []
    threading.Timer(0.3, release).start()
    assert flow_repo.executor().govern("plan/governor", "soft", "plan") is True
    (held,) = flow_repo.events(flow_repo.run, "run_held")
    assert held["key"] == "plan/governor" and held["level"] == "soft" and held["utilization"] == 0.8
def test_a_new_run_waits_before_its_first_phase(flow_repo, quick, monkeypatch): ...
    # prime(0.8), Timer(0.3, release); a run from test_pipeline's `begin(flow_repo)` (it has a worktree), with
    # `pipeline._Loop.walk` patched to return ("stopped", False), advanced once:
    # run_held(define/governor) < run_hold_ended < phase_started(define)
def test_setting_off_releases_a_held_run(flow_repo, quick): ...
    # prime(0.95); govern(...) holding in a thread; rewrite the run's config with usage.governor "off": govern returns True
def test_a_broken_file_during_a_hold_does_not_end_it(flow_repo, quick): ...
    # held at 0.95; the file becomes garbage: still held after several polls (heartbeats don't rewrite it);
    # release() writes readings, which replaces it: returns True. And with SLACK_S 0, prime(0.95, resets_in=1), then a
    # planted {"version": 2} file: returns after ≈ 1 s, at the kept verdict's `until` (never held past the reset)
def test_a_skipped_phase_and_the_ship_phase_are_never_held(flow_repo, quick): ...
    # prime(0.8): a phase skipped by skip_if → phase_skipped, no run_held; the phase holding `finish` starts with no run_held
def test_observe_only_records(flow_repo, quick): ...                                                # spec §7
    # usage.governor: observe; the first step's call streams five_hour 0.95: second_step is done, no run_held;
    # one usage_governed {key: KEY2, level: "hard", utilization: 0.95}; an observe run with a ready waiter ahead
    # in the file is not held either; a third step `build/third` streaming nothing new adds no second event for KEY2
def test_a_pause_during_a_hold_stops_without_a_call(flow_repo, quick): ...                          # Review Focus 3
    # prime(0.95); Timer(0.05, pause): run_step == "stopped", fake calls == [], run_hold_ended, waiting == {}
def test_the_hold_ends_at_the_reset_with_no_new_reading(flow_repo, quick, monkeypatch): ...         # Review Focus 1
    # SLACK_S 0, prime(0.95, resets_in=1), no later reading: govern returns True after ≈ 1 s
def test_finish_first_waits_only_for_a_ready_run_closer_to_finish(flow_repo, quick): ...
    # the file holds another entry (pid os.getpid(), rank [0.8, 0], since/seen/ready_since now, ready True);
    # `quick` leaves FRESH_S at 90 s, so it stays fresh:
    #   govern("build/governor", "soft", "build") holds with the window clear, and returns once a Timer(0.3) unwaits it;
    # the same entry with ready False: returns at once; with finish_first false: returns at once
def test_a_bad_waiter_entry_never_holds_or_crashes_a_gate(flow_repo, quick): ...           # Review Focus 2, 4
    # waiting {"x": {"pid": 1, "rank": [1e999, 0], "since": 0, "seen": 1e999, "ready": true}} and
    # {"x": {"pid": os.getpid(), "rank": "a", ...}}: govern returns True at once, no run_held, no runner_error
def test_a_codex_step_and_a_codex_sizer_are_not_held(flow_repo, quick): ...  # the fixture pack defines codex; prime(0.95): no run_held
def test_a_shared_session_s_next_task_is_held(lane, quick): ...  # Decided 2: a small plan's second task (takes_shared) under 0.95 → run_held at its implement key
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_governor_flow.py --import-mode=prepend -q`

Expected: FAIL, because `Executor` has no `govern`.

- [ ] **Step 3: Implement `govern` and the three call sites.**
  - **Sleeping.** `govern` sleeps in steps of at most `POLL_S`, against the wall clock, like `lanes._wait_until`.
  - **`_observed`.** It emits `usage_governed` only when `(key, level, until)` differs from `state.usage_governed`.
  - **The `observe` acceptance** also gets a stream-driven case: in `test_observe_only_records`, the 0.95 reading comes from a first call's `windows` act, not from `prime`.

- [ ] **Step 4: Run them to verify they pass.** Then run `test_pipeline.py`, `test_agent_step.py`, `test_feature_lane.py`, `test_state.py` and `test_retro.py`. The golden `explain` tests must stay unchanged.

- [ ] **Step 5: Commit**

```bash
git add agentflow/governor_lane.py agentflow/lanes.py agentflow/sizing_lane.py agentflow/pipeline.py agentflow/state.py agentflow/tests/conftest.py agentflow/tests/test_governor_flow.py
git commit -m "Hold new sessions above the hard mark and new phases above the soft mark, closest to finish first."
```

---

### Task 5: New runs wait: the queue, the Backlog, `next` and `run`

**Files:**
- Modify: `agentflow/module_queue.py`:
  - `queue_tick` (~697) gains `hold: str | None = None`. With a reason, its start loop starts nothing, while the assessment, the state writes and the reports still run.
  - `run_queue` (~1062) calls `start_gate` each tick. It passes the reason as `hold` and skips `_retro_if_due` while held. It logs the reason once, until the reason changes.
- Modify: `agentflow/backlog.py`: `begin_start` (~967) runs `raise Refused(f"the usage window holds new runs: {reason}")` before it takes the log lock.
- Modify: `agentflow/flow.py`:
  - `cmd_next` (~709) prints `usage: {reason}; new runs wait` after the answer.
  - `cmd_run` (~623) prints `note: {reason}; the run waits at its first phase` before `start`, but only when `verdict` governs at `soft` or above. A reason that comes only from waiters prints nothing, because the run's own soft gate waits for them.
- Test: `agentflow/tests/test_next_queue.py`, `agentflow/tests/test_backlog.py`

**Interfaces:**
- Consumes: `governor.start_gate(current_usage(cfg), account_of(cfg))`, `current_usage`, `account_of` and `verdict` (Task 2), and `prime` (Task 4's conftest helper, which records under `account_of` of the test's config).

- [ ] **Step 1: Write the failing tests**

```python
def test_the_queue_starts_nothing_above_the_soft_mark(qrepo, capsys): ...
    # prime(0.8); run_queue(..., once=True) with a recording `go`: go never called, no retro started, queue.json still
    # written, one log line holding "the 5h usage window is at 80% (above usage.five_hour_soft 0.75)"
def test_the_queue_starts_nothing_while_a_ready_run_waits(qrepo): ...  # a counting waiter, window clear → no start
def test_a_stale_waiter_does_not_stop_the_queue(qrepo): ...            # Review Focus 2: seen 10 min ago → started
def test_the_queue_starts_again_in_observe(qrepo): ...                 # usage.governor: observe, prime(0.95) → started
def test_the_backlog_refuses_a_start_above_the_soft_mark(bk): ...       # Refused naming the window; no item_starting
def test_next_says_new_runs_wait(qrepo, capsys): ...                    # cmd_next ends with "usage: the 5h usage window …; new runs wait"
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_next_queue.py agentflow/tests/test_backlog.py --import-mode=prepend -q -k "usage or soft_mark or waits or waiter or observe"`

Expected: FAIL, because the queue starts the module.

- [ ] **Step 3: Implement the four call sites** with the copy above.

- [ ] **Step 4: Run them to verify they pass.** Then run all of `test_next_queue.py`, `test_backlog.py` and `test_dashboard.py`. The Backlog start must answer 409 on `Refused`.

- [ ] **Step 5: Commit**

```bash
git add agentflow/module_queue.py agentflow/backlog.py agentflow/flow.py agentflow/tests/test_next_queue.py agentflow/tests/test_backlog.py
git commit -m "Start no new run from the queue, the Backlog or next while the usage window holds them."
```

---

### Task 6: The dashboard header shows the window, the interactive note and a broken file

**Files:**
- Modify: `agentflow/governor.py` (`header(usage, now)`)
- Modify: `agentflow/views.py` (`board`, ~420, gains `"usage": governor.header(current_usage(cfg), account_of(cfg), now.timestamp())`)
- Modify: `agentflow/dashboard.html`:
  - after `#version` (~312), add `<span id="usage" class="pill" hidden></span><span id="usage-note" class="pill usage-note" hidden></span>`;
  - add `.pill.soft`, `.pill.hard` and `.pill.usage-note` rules, coloured from the existing tokens. The page has no `.warn` class, so none is used.
- Modify: `agentflow/dashboard_ui/board.js` (`usageLabel(usage)`; `renderBoard`, ~396, fills both spans with `textContent` and `title` only, never `innerHTML`)
- Test: `agentflow/tests/test_dashboard.py`, `agentflow/tests/test_dashboard_js.py`

**Interfaces:**
- Produces:
  - `governor.header(usage: Usage, account: str, now: float, file: WindowFile | None = None) -> dict`, which never raises. It returns `{"mode": str, "level": "clear"|"soft"|"hard", "windows": {name: {"utilization": float, "resets_at": int, "stale": bool}}, "waiting": int, "note": str | None, "error": str | None}`.
    - `waiting` counts the entries that count.
    - `note` is set only while the note's `resets_at` is in the future, and is exactly `"the 5h window rose 9 points (41% → 50%) between 14:02 and 14:40 while no run had a session open: interactive use draws on the same window"`.
    - `error` is `read()`'s error.
  - JS `usageLabel(usage) -> {text, level, title, note}`:
    - `text` is `"5h 82% · 7d 41%"`, leaving out stale or missing windows. An empty `text` hides the pill. `observe` and `off` add `" (observe)"` or `" (off)"`.
    - `title` is `"resets 15:00"`, plus `" · 2 runs wait"` when some run waits.
    - `level` is the verdict's level.
    - `note` is the interactive note, else `"usage window file unreadable: " + error` when `error` is set, else `""`.

- [ ] **Step 1: Write the failing tests**

```python
# test_dashboard.py
def test_the_board_carries_the_usage_window(flow_repo): ...
    # prime(0.82) → board(...)["usage"] has mode "on", level "soft", five_hour utilization 0.82 stale False, waiting 0, note None, error None
def test_the_board_carries_the_interactive_note_and_a_broken_file(flow_repo): ...
    # a stored `interactive` → the note text; past its resets_at → None; garbage file → error set, board() does not raise

# test_dashboard_js.py
def test_the_usage_pill_reads_short():
    usage = {"mode": "on", "level": "soft", "waiting": 2, "note": None, "error": None,
             "windows": {"five_hour": {"utilization": 0.82, "resets_at": 1800000000, "stale": False},
                         "seven_day": {"utilization": 0.41, "resets_at": 1800300000, "stale": False}}}
    label = js(["core.js", "board.js"], "usageLabel(args)", usage)
    assert label["text"] == "5h 82% · 7d 41%" and label["level"] == "soft" and label["title"].endswith(" · 2 runs wait")
    assert js(["core.js", "board.js"], "usageLabel(args).text", {**usage, "windows": {}}) == ""
    assert js(["core.js", "board.js"], "usageLabel(args).note", {**usage, "error": "bad json"}) == \
        "usage window file unreadable: bad json"
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_dashboard.py agentflow/tests/test_dashboard_js.py --import-mode=prepend -q -k "usage"`

Expected: FAIL, because `board()` has no `usage` key.

- [ ] **Step 3: Implement** `header`, the `board` key, the two spans and `usageLabel`. `board.js` must stay loadable with no DOM, through the `js([...])` helper that the existing `test_dashboard_js.py` cases use.

- [ ] **Step 4: Run them to verify they pass.** Then run `test_portability.py`.

- [ ] **Step 5: Commit**

```bash
git add agentflow/governor.py agentflow/views.py agentflow/dashboard.html agentflow/dashboard_ui/board.js agentflow/tests/test_dashboard.py agentflow/tests/test_dashboard_js.py
git commit -m "Show the usage window, the interactive-use note and a broken window file in the dashboard header."
```

---

### Finish

- [ ] Run `uvx ruff@0.17.4 check .` and see `All checks passed!`. Run `bin/agentflow validate` and see `0 warnings`.
- [ ] Run the full suite: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests --import-mode=prepend -q`. Every test must pass, and `git status` must show no change to `agentflow/tests/fixtures/explain/*.txt`.
- [ ] Do the real-run check (spec §9). Run one feature on agentflow itself with `usage.governor: on`. The PR description gives:
  - the run's `run_held` events, with their `reason`, `level` and key, and whether any of them came mid-phase (a step key, not `<phase>/governor`);
  - the window the header shows;
  - the run's scorecard against the C0 baseline.
