# agentflow cost, slice C4 (turn caps, spend caps and context hygiene) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every Claude run session has a turn cap and a spend cap set by its role.
- **An editing step** that hits either cap gets one short wrap-up turn to write its result.
- **A review that hits a cap** is never taken as complete.
- **Either way,** a cap that isn't settled stops the step as `turn_cap` or `budget_cap`, with the ways out listed. The runner no longer holds a capped session and retries it as a dead agent.
- **Context hygiene:** shell output is capped, and the prompts stop telling sessions to run the gates the runner runs after them.

**Architecture:**
- **Caps per call.**
  - `config.resolve_turn_cap(cfg, agent_name, step, role)` resolves the turn cap in this order: the step's `max_turns`, then `agents.<name>.max_turns_by_role[role]`, then `agents.<name>.max_turns`. It lives in `config.py`, because `flow_control` can't import `lanes`.
  - `config.session_budget(cfg, agent_name, role)` reads `agents.<name>.session_budget_usd[role]`.
  - The executor puts both on every `AgentCall`. `ClaudeAgent.argv` passes `--max-turns` (as today) and the new `--max-budget-usd`.
  - A doubt round 2+ halves the resolved turn cap (C3's Decided 4 swap).
- **Seeing a cap.** `parse_claude_line` records the first result whose `subtype` (or `terminal_reason`) names a cap. `AgentOutcome.capped` is `"turns"` or `"budget"`, and a capped call is never an `Interruption` unless the usage window refused it.
- **After a cap.** In `Executor._loop`:
  - **A review step** (`role: review`) that was capped stops at once as the cap's kind, whatever it wrote.
  - **Any other step** that was capped with no valid result gets one wrap-up turn per step since the last human approval. The wrap-up resumes the session under `WRAP_UP_TURNS` and `WRAP_UP_BUDGET_USD`, and is recorded as `session_capped`, so a restart counts it.
  - **A wrap-up** that doesn't end `done` stops the step as the cap's kind. The stop's report lists the ways out, and `autonomy.resolve_kinds` lets a resolver take it once.
- **Context hygiene (L6).**
  - `agents.claude.bash_output_chars` sets `BASH_MAX_OUTPUT_LENGTH`.
  - The preamble gains lines on quiet test flags, `Glob`/`Grep` with limits, and reading a file once.
  - Five prompts that tell a session to run the build or verify gates say the runner runs them after it. The fix lead says to re-run only what failed.
  - The reworded sentences get `pack_fix.Retired` entries, each with an `old` unique to its file.

**Tech Stack:** Python 3.12 stdlib + PyYAML; pytest with `tests/fake_agent.py`.

**Spec:** `docs/specs/agentflow-cost-proposal.md`:

| Section | What this plan implements from it |
|---|---|
| §3.5 L5a | all of it, with the default values, the "off" form and review wrap-ups changed (Decided 1, 2, 5) |
| §3.5 L5b | all of it |
| §3.5 L5c, L5d | nothing new (L5c shipped in C3; L5d changes no default) |
| §3.3 L3c | the C3 amendment's last line: the round 2+ cap's base becomes the resolved turn cap |
| §3.6 L6a–c | all of it (L6d stays deferred, C2 amendment) |
| §4 | the rows `max_turns_by_role`, `session_budget_usd`, `bash_output_chars` and `phases.<p>.steps[].max_turns` |
| §5, §8, §9 | what stays as it is, the cap risks, and the testing strategy |
| §7 | the "C4 Caps" row |

## Decided in this plan

These are choices the spec leaves open, or measurements that change one of its values. Two doubt review cycles (2026-11-19: a security auditor and correctness reviewers) shaped 1, 2, 5, 6 and 9–12. Confirm them at review. Task 1 records each one in the spec as an "Amended for the C4 plan" note (the repository's convention).

1. **The turn caps are in the CLI's `num_turns` unit, set at about 1.5× the largest observed session.**
   - **What the unit is:** the post-C0 runs in the state dir (98 invocations with `agent_exited.usage`, 2026-11-18 → 19) show `num_turns` is about tool calls + 1, not API calls. Example: a build session with 32 API calls and 61 tool calls reports 62. §1.2's turns were API calls.
   - **The numbers, per invocation:**
     - build: p90 74, max 88;
     - plan: p90 65, max 70;
     - review: p90 42, max 64.
   - **Why the max and not the p90:** the sample is small. A cap at 1.5× p90 would sit at the observed review max, so it would cut ordinary reviews, and a halved round 2+ cap would sit below the review p90.
   - **The defaults:** `max_turns_by_role: {review: 100, plan: 110, build: 130, debug: 130, mechanical: 30}`.
     - `debug` has no data and follows `build`.
     - `mechanical` is the spec's 15 API calls in this unit.
     - A round 2+ doubt review gets 50.
   - **The probe decides:** Task 1's probe checks which counter `--max-turns` uses. If it counts API calls (assistant messages) and not `num_turns`, ship the spec's values `{review: 30, plan: 60, build: 60, debug: 60, mechanical: 15}`, raised to `{review: 45}` (1.5× the §1.2 review p90 of 31), and say so in the amendment.
2. **"No cap" is `0`; "remove the entry" is `null`; `{}` does nothing.**
   - **Why not `{}`:** `layers.merge` skips an empty mapping ("merging nothing into a mapping changes nothing"), so a pack's `session_budget_usd: {}` keeps the engine's caps.
   - **The two maps are independent:** each acts only on its own cap.
   - **`max_turns_by_role.<role>: 0`** means no turn cap for that role: no `--max-turns`, and no fallback to `agents.<name>.max_turns`.
     - `config.resolve_turn_cap` returns `0` for it, and `AgentCall.max_turns == 0` means "pass no flag".
     - `doubt.delta_turn_cap(0)` returns `0`.
   - **`session_budget_usd.<role>: 0`** means no `--max-budget-usd` for that role.
   - **`null` on a role** deletes that entry, so the role falls back to `agents.<name>.max_turns` (turns) or no cap (spend).
   - **`null` on the whole key** deletes every entry.
3. **The spend caps keep the spec's values: `{review: 4, plan: 10, build: 10, debug: 10}`.**
   - **The data:** the post-C0 per-invocation p99 is build $1.47, plan $0.94 and review $0.78. Every cap is more than 3× p99, Opus prices included.
   - **No role is lowered:** the data is one pack, and a cap is a backstop.
4. **The role map supersedes `agents.<name>.max_turns` for the roles it names.**
   - **The change:** a pack that set `max_turns` (this repository's `80`, both fixture packs' `80`) now gets the role caps for `review`, `plan`, `build`, `debug` and `mechanical`. `max_turns` stays the cap for every other role.
   - **Where it is written:** the changelog says so under `Changed`, and the playbook line for `max_turns` and the Settings agent note are reworded.
5. **The wrap-up is for editing steps only.**
   - **For a review step** (the step's configured `role` is `review`):
     - a capped call stops the step at once as the cap's kind, even when it wrote a result;
     - there is no wrap-up;
     - a capped review is never a complete review. This keeps the refine floor and review independence (§5).
     - **With a usage-window refusal** (`outcome.interruption`), the call is held as today, any result it wrote is ignored, and the next call decides.
   - **`verify` is an editing step** (`role: build`, spec §3.4), although it has `blocks_on_findings`. It gets the wrap-up, and the runner runs its gates after the wrap-up's result anyway.
   - **For any other step:**
     - a capped call that wrote a valid result first is a normal result, and gets no wrap-up;
     - a capped call with no valid result gets one wrap-up turn, at most once per step since the last human approval or reopen (`hitl_approved` or `step_reopened`).
   - **How the count survives:** it comes from `session_capped` events with `wrap_up: true`, so a runner restart doesn't give a second one.
   - **The wrap-up's caps:** `WRAP_UP_TURNS = 3`, and `WRAP_UP_BUDGET_USD = 2.5`, never more than the role's own spend cap. With no spend cap for the role, the wrap-up has none either: its 3 turns bound it.
     - **Why 2.5:** after a long hold the cache is cold, and the first resumed call rewrites the whole context. That is about $1.00 on Opus at a 200K context with 5-minute writes, and $1.60 with 1-hour writes.
   - **What keeps the wrap-up's small caps and its prompt:**
     - **A hold's retry:** `_hold` returns the same wrap-up turn, prompt included, whether or not the call reached the model.
     - **A runner restart:** the resume is a wrap-up turn, with the wrap-up prompt, when the step's last `session_capped`-or-`fix_attempt` event since the approval is a `session_capped` with `wrap_up: true`.
   - **What ends it instead:** a rollover or a replaced brief during a wrap-up turn stops the step as the cap's kind. Either one would start a fresh session, and 3 turns can't do a step in a fresh session.
   - **What follows the wrap-up:**
     - **`done`** goes through the gates, the fix loop and the review as any result does.
     - **Every other status** (`blocked`, `needs_human`, `effort_change`, `unchanged`), no result at all, or the wrap-up hitting its own cap, stops the step as the cap's kind. The report carries the agent's cleaned summary and `signals.todo`.
     - **A second cap after the wrap-up was used,** for example in a fix attempt, stops at once.
   - **Precedence:** a usage-window refusal (`rate_limit_event` status `rejected`) is a hold, as today, even when the result is also capped.
   - **No new per-step bound:** each call is capped, but a step can still make more calls than `fix_attempts` suggests: a reminder, holds, an approval or a reopen each start a call with a fresh cap. `run_budget_hard_usd` bounds the run, as today (spec Q6).
6. **Ways out, the resolver and the retro.**
   - **Approving the stop** resumes the session. Each call has its own cap (probe B2, and Task 1's probe for turns), so approving *is* "raise the cap once".
   - **Split and descope** are `approve --note`, which the resumed session reads. `retry` starts fresh.
   - **Where the ways out are written:** `hitl._how_to_go_on`, the inbox line that already says what each way out of a stop does, gets a branch for `turn_cap` and `budget_cap`. It replaces the default text there, rather than adding a second list in `extra`.
   - **What the report leaves out:** the config keys that raise a cap for good are in the playbook, not in the report. The report is the resolver's `{stop_report}`, and a resolver must not be pointed at the pack.
   - **The resolver:** `autonomy.resolve_kinds` gains `turn_cap: 1` and `budget_cap: 1`:
     - in the engine defaults;
     - in this repository's `.agentflow/config.yaml`;
     - in the `python` and `node` fixture packs.

     A pack that writes its own workflow replaces the engine's whole `autonomy` block (`layers.WORKFLOW_KEYS`). A resolver's own capped session goes to a human, as every resolver stop does today.
   - **A retro's cap stop** gives `hitl.RETRO_WAY_OUT` as its only way out, because a retro run is never resumed.
7. **What the caps cover.**
   - **Covered:** every call made through `run_agent_step`. That includes the resolver (`role: plan`), the record step (`role: build`) and the retro (`role: review`, so it stops at its cap with no wrap-up).
   - **Not covered:** sizers and the route triage keep their own turn caps and get no spend cap.
   - **A role with no entry** (a pack's own role) falls back to `agents.<name>.max_turns`, and gets no spend cap.
   - **Codex:** these keys are a `validate` warning ("has no effect") on a non-Claude agent, and Codex's argv is unchanged.
8. **`bash_output_chars`.** The engine default is `15000`.
   - **Above 0:** the runner sets `BASH_MAX_OUTPUT_LENGTH`, and drops any inherited or `defaults.env` copy, as `cache_ttl` does with its variables.
   - **What it can't stop:** a project `.claude/settings*.json` `env` block can still set the variable. The playbook says so, and the effect is cost only.
   - **`0`:** the runner leaves the variable to the environment.
   - **`validate`:** the value must be `0`, or a whole number from 1000 to 1000000.
9. **Flags.**
   - **The budget value:** `--max-budget-usd` gets the value in fixed notation, `f"{usd:.2f}"`.
   - **Its range:** `validate` takes `0` or a number from 0.01 to 1000. `_build_agent` parses the values to floats and raises `ConfigError` on anything else.
   - **Runner-owned flags:** `--max-turns` and `--max-budget-usd` join `RUNNER_FLAGS`, so a copy in `base_args` or `command` is an error (the runner now always sets both). The check also covers `effort_arg`.
   - **A refused flag:** `_refused_flag_summary` names the key a refused `--max-budget-usd` or `--max-turns` comes from.
10. **Prompt wording (L6b, L6c).**
    - **Run-the-gates lines:** these prompts tell a session to run the build or verify gates, although a gate set runs after each of their steps:
      - `task_inline.md`;
      - `fix.md`;
      - `ci_fix.md`;
      - `fix_findings.md`;
      - `sync_conflicts.md`.

      Their line now says the runner runs those gates after the session. This holds for every engine default step that uses them. A pack that puts one of these prompts on a step with no gates gets a session that skips the gates; that is a trade-off, written in the playbook's prompt list.
    - **`gate_fix.md`** stays as it is: its job is a failing gate.
    - **`_fix_lead`** (Python text) says to re-run only what failed.
    - **The preamble** gains three new lines and rewords none. None of them mentions `{progress}`, which read-only sessions may not write.
    - **`agentflow-build/SKILL.md`** gets "quiet flags".
11. **Each `Retired` entry has an `old` unique to its file.**
    - **The constraint:** `pack_fix._scan` applies every entry to every pack prompt, whatever its name. That is deliberate: it catches a copy saved under another name, and `test_unusual_name_is_not_marked_fixable` relies on it.
    - **The problem:** three files retire the same sentence ("Run the verify gates before you finish.") with different replacements.
    - **The fix:** each entry's `old` carries enough of its line to match only its own file, and the entries are ordered longest `old` first.
    - **`detect` tokens** are fragments of `old` that appear in no `new` text and in no engine prompt, as `test_register_matches_the_engine_prompts` already demands.
    - **The preamble's three new lines** are a reword too. Its entry's `old` is the junction between two existing lines, and its `new` puts the three lines between them. So a pack override of `_preamble.md` that holds the junction gains the lines with `--fix`, and the C2 fixture still fixes to the engine's file.
12. **This slice is executed interactively, never as an agentflow run.**
    - **How:** on a branch of the main checkout, with subagent-driven-development or executing-plans.
    - **Why:**
      - Tasks 1 and 4 edit `.agentflow/`, which a run refuses.
      - In a run, the verify gate would fail on `test_every_pack_validates_clean`, which validates the worktree's `.agentflow/` and its prompt overrides.
      - `bin/agentflow validate` and `validate --fix` read the main checkout's pack, so they must run there, on this branch.
    - **What still runs:** the real-run check under "Finishing the slice" is a separate feature run, after the PR's branch is done.

**Out of scope, older than this plan.** The security review raised these; each needs its own change:
- **Unpriced nested sessions:** a `bypassPermissions` session can run `claude`, `codex` or `agentflow` through Bash. That spend is outside every cap and outside `run_budget_hard_usd`, and `agentflow answer` records under the user's name.
- **The main checkout's pack:** a run's agent can edit the main checkout's `.agentflow/config.yaml`, which a live run reloads before a phase (`pipeline.reload`).

The PR description lists both.

## Global Constraints

- **Dependencies:** engine dependencies stay stdlib + `pyyaml>=6`.
- **No project strings** in engine files (`test_portability.py`).
- **Golden files:** `agentflow/tests/fixtures/explain/*.txt` stay byte-identical. `explain` shows no session flags.
- **Config keys:** every new key ships, in the same task, with all four of these:
  - a Settings form field, or protection (`phases.<name>` steps and the `record_step`/`resolve_step`/`retro_step` blocks are already protected);
  - its `validate` checks;
  - a `test_config_edit.py` case;
  - a playbook §14 line.
- **Defaults:**
  - `agents.claude.max_turns_by_role: {review: 100, plan: 110, build: 130, debug: 130, mechanical: 30}` (Decided 1);
  - `agents.claude.session_budget_usd: {review: 4, plan: 10, build: 10, debug: 10}`;
  - `agents.claude.bash_output_chars: 15000`;
  - `autonomy.resolve_kinds.turn_cap: 1`, `autonomy.resolve_kinds.budget_cap: 1`;
  - `WRAP_UP_TURNS = 3`, `WRAP_UP_BUDGET_USD = 2.5`.
- **Stop kinds:** `turn_cap` and `budget_cap`. A cap's own name is `"turns"` or `"budget"`.
- **The stream:** `CAP_SUBTYPES = {"error_max_turns": "turns", "error_max_budget_usd": "budget"}`. As a fallback, `CAP_REASONS = {"max_turns": "turns", "budget_exhausted": "budget"}` read from the same result's `terminal_reason`.
- **Model text in a stop:** model text in a stop's summary or report goes through `hitl.clean_fact`, one line per item.
- **Engine prompts:** every reworded sentence gets a `pack_fix.Retired` entry with `retired_in="2.10"`, `old` holding the exact shipped text, and a `detect` token that `test_register_matches_the_engine_prompts` accepts.
- **Changelog:** every task with a user-visible change adds one line under `## [Unreleased]` in `CHANGELOG.md`.
- **Unchanged:**
  - the engine floor;
  - review independence;
  - the doubt stop rules and numbering;
  - `run_budget_hard_usd` and its `budget_exceeded` stop;
  - `fix_attempts`;
  - the sizers' own turn caps;
  - the Codex argv;
  - per-task commits.
- **Lint and tests:**
  - `uvx ruff@0.17.4 check .` must print `All checks passed!` before each commit.
  - Tests run with `uv run --no-project --with pyyaml --with pytest pytest <file> --import-mode=prepend -q`.
- **Security overlay:** `agents.py` and `pack_fix.py` change. Plan and review with `security-and-hardening` and the `security-auditor` agent, with attention to:
  - the new argv flag and its value;
  - the runner-owned environment variable;
  - a capped call no longer counting as a dead agent;
  - model text in the stop report;
  - the `Retired` entries' `old` texts, each matching only its own file.

## Review Focus

1. **A capped review.**
   - A per-task review, a `branch_review`, a doubt `spec_review` in round 2 (cap 50) and the retro, each capped after writing `{"status": "done", "findings": []}`. Each stops as the cap's kind: no phase goes on, no task commits and no refine round is skipped on a truncated review.
   - The same capped review whose stream also says the usage window refused it (`rejected`): it is held, the result is ignored, and the next call decides.
   - An editing step capped after writing a valid result is used as it is, and so is `verify` (`role: build`). *(Task 3.)*
2. **Every way out of a wrap-up turn:**
   - `effort_change`, `needs_human` and `blocked` results stop as the cap's kind;
   - a hold during the wrap-up (one that reached the model, and one that didn't) retries it with `--max-turns 3` and the wrap-up prompt;
   - a rollover or a replaced brief during the wrap-up stops as the cap's kind;
   - a runner restart during the wrap-up resumes with `--max-turns 3` and the wrap-up prompt;
   - a fix attempt capped after the wrap-up was used stops at once;
   - after `approve`, a new cap gets a new wrap-up. *(Task 3.)*
3. **A cap and the usage window at once.** The result subtype says `error_max_budget_usd`, and the last `rate_limit_event` says `rejected`. The call is held (`usage limit`), not wrapped up. Also, a capped call never adds to `agent_failures`, so caps never become `agent_unavailable`. *(Tasks 2 and 3.)*
4. **Agent text in the stop.** A `summary` or a `signals.todo` item holding newlines, `### Ways out`, a fake command, a bidi character or 5,000 characters shows as one cleaned, cut line under `### Left to do`. The runner's own ways out stay in the inbox's "how to go on" line, ahead of it. *(Task 3.)*
5. **Layered config:**
   - `max_turns_by_role: {build: 0}` with `agents.claude.max_turns: 80`: no `--max-turns` for build, and a round 2+ review of a `{review: 0}` pack has none either;
   - `session_budget_usd: null`: no `--max-budget-usd` anywhere;
   - `{review: null}`: none for review only;
   - a pack that writes its own workflow: its own `resolve_kinds` decides;
   - `bash_output_chars: 15000` with `defaults.env.BASH_MAX_OUTPUT_LENGTH: "999999"`: 15000 wins.

   *(Tasks 1, 2 and 3.)*

---

### Task 1: Probe, the config keys, their checks and the spec amendments

**Files:**
- Modify: `agentflow/config.py`:
  - `AgentSpec` gains `max_turns_by_role: dict[str, int]`, `session_budget_usd: dict[str, float]` (both `field(default_factory=dict)`) and `bash_output_chars: int = 0`, parsed in `_build_agent` with `ConfigError` on a wrong type;
  - `Step` gains `max_turns: int | None = None`, and `"max_turns"` joins `STEP_KEYS`;
  - `RUNNER_FLAGS` becomes a dict from flag to the key the runner sets it from;
  - the module functions `resolve_turn_cap` and `session_budget`;
  - `_Validator` gets the checks below.
- Modify: `agentflow/doubt.py`: `delta_turn_cap(0)` returns `0`.
- Modify: `agentflow/defaults/config.yaml`: the three `agents.claude` keys (one comment each), and `turn_cap: 1` and `budget_cap: 1` under `autonomy.resolve_kinds`.
- Modify: `agentflow/tests/fixtures/packs/python/.agentflow/config.yaml` and `.../node/.agentflow/config.yaml`: the same two `resolve_kinds` lines, when the pack has a `resolve_kinds`.
- Modify: `.agentflow/config.yaml`: the two `resolve_kinds` lines (Decided 12).
- Modify: `agentflow/config_edit.py`:
  - `AGENT_FREE_KEYS` gains the three keys, and the `PROTECTED` note follows it;
  - the Claude section gets the fields below;
  - the agent section's note no longer says `max_turns` is "the turn limit for every session".
- Modify: `docs/agentflow-playbook.md` §14 (four new key lines, and the `max_turns` line reworded), `docs/specs/agentflow-cost-proposal.md` (the "Amended for the C4 plan" notes), `CHANGELOG.md` (`Added`, and `Changed` for Decided 4)
- Test: `agentflow/tests/test_config.py`, `agentflow/tests/test_config_edit.py`, `agentflow/tests/test_doubt.py`

**Interfaces:**
- Produces:
  - `cfg.agents["claude"].max_turns_by_role` (the Decided 1 map);
  - `cfg.agents["claude"].session_budget_usd` (`{"review": 4.0, "plan": 10.0, "build": 10.0, "debug": 10.0}`);
  - `cfg.agents["claude"].bash_output_chars` (`15000`);
  - `Step.max_turns`.

  An agent that sets none of them gets `{}`, `{}` and `0`.
- **Parsing:** `None` values (a role set to `null` that no lower layer filled) are dropped at parse time.
- Produces: `config.resolve_turn_cap(cfg: Config, agent_name: str, step: Step, role: str) -> int | None`. It returns:
  1. `step.max_turns` when set;
  2. else `spec.max_turns_by_role[role]` when the role has an entry, `0` included;
  3. else `spec.max_turns`.

  `0` means "no cap" and never falls back.
- Produces: `config.session_budget(cfg: Config, agent_name: str, role: str) -> float | None`. It is `spec.session_budget_usd.get(role)`, and `0` or a missing entry gives None.
- Produces: `config.RUNNER_FLAGS: dict[str, str]`, which is:
  - `{"--setting-sources": "agents.claude.user_settings",`
  - `"--max-turns": "agents.claude.max_turns_by_role",`
  - `"--max-budget-usd": "agents.claude.session_budget_usd"}`

**The `validate` checks:**
- **Error at `agents.<name>.max_turns_by_role`:**
  - a value is not a whole number ≥ 0 (a bool counts as wrong);
  - a role is not a key of that agent's `models`.
- **Error at `agents.<name>.session_budget_usd`:**
  - a value is not `0`, or a finite number from 0.01 to 1000 (a bool counts as wrong);
  - a role is not a key of `models`.
- **Error at `agents.<name>.bash_output_chars`:** not `0`, and not a whole number from 1000 to 1000000.
- **Warning at each of the three keys** on an agent other than `claude`, when set: "only the claude agent's calls are capped: this has no effect on `<name>`". Extend `claude_session`'s per-key loop.
- **Error at a step's `max_turns`** (phase steps and their sub-steps, `record_step`, `resolve_step`, `retro_step`): not a whole number ≥ 1.
- **Error at `agents.<name>.command`, `base_args` and now `effort_arg`:** a `RUNNER_FLAGS` flag (the bare form or `--flag=`). The text names the key from the dict: "`<arg>` is not allowed: the runner sets it from `<key>`". `PERMISSION_FLAGS` is checked in `effort_arg` too.

**The form fields** (Claude section, after `read_only_roles`):
- `{"key": ["agents", name, "max_turns_by_role"], "label": "Max turns by role (0 = no cap)", "kind": "map", "value_kind": "int", "min": 0}`
- `{"key": ["agents", name, "session_budget_usd"], "label": "Spend cap per session by role (USD, 0 = no cap)", "kind": "map", "value_kind": "number", "min": 0}`
- `{"key": ["agents", name, "bash_output_chars"], "label": "Shell output cap (chars, 0 = the CLI's own)", "kind": "int", "min": 0, "max": 1000000}`

- [ ] **Step 0: Probe the real CLI**

  This step is interactive, needs a logged-in `claude`, and costs under a cent on Haiku. Run each probe in a scratch git repo with `--output-format stream-json --verbose`, and record the results:

  | # | Probe | Record |
  |---|---|---|
  | a | `--max-turns 2`, a prompt that needs 4 sequential `Read` calls | the exit code, `subtype`, `is_error`, `terminal_reason`, `num_turns`, and how many assistant messages ran |
  | b | `--max-turns 2`, a prompt that makes 4 `Read` calls in parallel in one message | the same: does one message with 4 tool calls count as 1 turn or 4? |
  | c | `--resume <a's session> --max-turns 3`, "write `ok` to ./r.txt" | succeeds: the cap counts per invocation |
  | d | `BASH_MAX_OUTPUT_LENGTH=2000`, "run `seq 1 5000`" | the `tool_result` text is ≤ ~2.2K chars |
  | e | `--max-budget-usd 0.01` on a 4-call task | ends `error_max_budget_usd`, and the value in fixed notation is accepted |

  **What the results mean:**
  - **(b) says one message counts as 1 turn** (the cap counts API calls): use Decided 1's fallback values.
  - **(a)'s `subtype` or `terminal_reason` differ** from `CAP_SUBTYPES`/`CAP_REASONS`: Task 2 uses the observed strings.
  - **(c) fails:** stop and bring it to the user, because Decided 6's "approve raises the cap once" depends on it.
  - **(d) is not capped:** keep the key, which fails toward today's behaviour. Note it in the PR.

  The results go in the PR description.
- [ ] **Step 1: Write the failing tests**
  - `test_config.py::test_cost_c4_keys_default`: the engine defaults (load `layers.ENGINE_DEFAULTS` alone, not a fixture pack) give the three Decided values and `resolve_kinds` `turn_cap: 1`, `budget_cap: 1`. The `python` fixture pack's `resolve_kinds` has both too.
  - `test_config.py::test_cap_maps_merge_and_switch_off`. A pack layer with:
    - `max_turns_by_role: {build: 0}` keeps `review: 100` and gives `build: 0`;
    - `session_budget_usd: null` gives `{}`;
    - `session_budget_usd: {review: null}` drops only `review`;
    - `session_budget_usd: {}` keeps all four (the merge rule Decided 2 names).
  - `test_config.py::test_resolve_turn_cap_precedence`:
    - a step's `max_turns: 20` beats the role's 130;
    - the role's 130 beats `agents.claude.max_turns: 80`;
    - a role `design` with no entry gets `80`;
    - `{build: 0}` gives `0` even with `max_turns: 80`.

    And `session_budget` gives `10.0` for `build`, and None for `design` and for `{build: 0}`.
  - `test_doubt.py::test_delta_turn_cap`: add `0 → 0`, keeping `None → None`, `1 → 1` and `60 → 30`.
  - `test_config.py::test_cap_map_checks`. Each of these gives one error:
    - `max_turns_by_role: {reviewer: 10}`;
    - `{build: -1}`;
    - `{build: true}`;
    - `session_budget_usd: {build: .inf}`;
    - `{build: 0.001}`;
    - `{build: 5000}`;
    - `{nobody: 1}`.

    A string value is a `ConfigError` at load.
  - `test_config.py::test_bash_output_chars_checks`: `500` and `2000000` are errors, and `0` and `15000` are valid.
  - `test_config.py::test_cap_keys_on_codex_warn`.
  - `test_config.py::test_step_max_turns_checks`:
    - `max_turns: 0` and `max_turns: "x"` on a phase step are errors;
    - `max_turns: 20` on `retro_step` is valid and parses to `Step.max_turns == 20`.
  - `test_config.py::test_runner_flags_name_their_key`: `--max-budget-usd 5` in `base_args`, `--max-turns=9` in `command` and `--max-turns 9` in `effort_arg` each give one error naming their key. The existing `--setting-sources` test still passes, its text naming `user_settings`.
  - `test_config_edit.py::test_cap_fields_are_editable`:
    - all three fields round-trip with comments kept;
    - `{build: -1}` and `bash_output_chars: 2000001` are refused;
    - a change to a phase step's `max_turns` is refused as protected.
  - Update `test_config_edit.py::test_the_codex_section_has_no_claude_cache_fields`, which pins `AGENT_FREE_KEYS[-4:]` and the protected note, to the new keys.
  - Update `test_config.py::INHERITED`: rows for `agents.claude.max_turns_by_role`, `agents.claude.session_budget_usd` and `agents.claude.bash_output_chars` for `python`, `node` and `this repo`, with one shared reason ("the engine's per-role caps: a pack states them only to change them"). This keeps `test_full_packs_differ_from_unlayered_only_where_listed` passing.
- [ ] **Step 2: Run them and see them fail**

  Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_config.py agentflow/tests/test_config_edit.py agentflow/tests/test_doubt.py --import-mode=prepend -q -k "c4_keys or cap_ or bash_output or step_max_turns or runner_flags or turn_cap or codex_section or unlayered"`
  Expected: FAIL.
- [ ] **Step 3: Implement the keys, the functions, the checks, the form fields, the pack lines, the playbook lines, the spec notes and the changelog lines**

  **The playbook lines:**
  - `max_turns_by_role`: Decided 1, 2, 4 and 7.
  - `session_budget_usd`: Decided 2 and 3, and Decided 5's note that `run_budget_hard_usd`, not the per-call cap, bounds a step's total. It counts list-price dollars, per call, on a subscription too, and overshoots by one turn.
  - `bash_output_chars`: Decided 8.
  - `phases.<p>.steps[].max_turns`: overrides the role cap for that step, and a doubt round 2+ gets half of it.
  - `max_turns`: the cap for roles the role map doesn't name.
  - The layers paragraph that says the engine defaults' `claude` agent has "no `max_turns`": it now names the role map.
  - The protected-key list: the three new keys among the agent keys a page may change.

  **The spec** gets "Amended for the C4 plan" notes:
  - under §3.5 L5a (Decided 1, 2, 4, 5, 6, 7);
  - under L5b (Decided 2, 3, and 5's "no new per-step bound", which corrects §3.5's "(1 + `fix_attempts` + wrap-up) × the cap");
  - under §3.6 (Decided 8, 10, 11);
  - in the §4 table rows.
- [ ] **Step 4: Run the tests again**

  Run: the Step 2 command, then `bin/agentflow validate`.
  Expected: PASS, and `0 warnings`.
- [ ] **Step 5: Lint and commit**

  ```bash
  uvx ruff@0.17.4 check .
  git add agentflow/config.py agentflow/doubt.py agentflow/defaults/config.yaml agentflow/config_edit.py agentflow/tests/fixtures/packs .agentflow/config.yaml docs/agentflow-playbook.md docs/specs/agentflow-cost-proposal.md CHANGELOG.md agentflow/tests/test_config.py agentflow/tests/test_config_edit.py agentflow/tests/test_doubt.py
  git commit -m "Add per-role turn and spend caps, a shell output cap and a step turn cap, with their validate checks."
  ```

---

### Task 2: The capped argv, environment and stream, and a fake agent that hits caps

**Files:**
- Modify: `agentflow/agents.py`:
  - `CAP_SUBTYPES`, `CAP_REASONS`, `BASH_OUTPUT_ENV = "BASH_MAX_OUTPUT_LENGTH"`;
  - `AgentCall.budget_usd: float | None = None`;
  - `StreamAccumulator.capped: str | None = None`;
  - `AgentOutcome.capped: str | None = None`;
  - `interruption()`, `to_outcome()`, `parse_claude_line`'s result branch;
  - `ClaudeAgent.argv`, `env` and `env_drop`.
- Modify: `agentflow/tests/fake_agent.py`: a new act key, `capped`.
- Test: `agentflow/tests/test_agents.py`, `agentflow/tests/test_fake_agent.py`

**Interfaces:**
- Consumes: nothing from Task 1 but the `AgentSpec` fields.
- Produces: `AgentCall.budget_usd`, commented "this call's spend cap in list-price USD (`--max-budget-usd`); None: none".
- **`AgentCall.max_turns` semantics:** `0` means pass no `--max-turns`. None keeps today's fallback to `spec.max_turns`, for callers that don't resolve a cap.
- Produces: `AgentOutcome.capped`: `"turns"`, `"budget"` or None.
  - **Set from:** `acc.capped`, set once, by the first `result` event of the invocation whose `subtype` is in `CAP_SUBTYPES`, or else whose own `terminal_reason` is in `CAP_REASONS`.
  - **Interruption:** `interruption()` checks the usage-limit refusal first, as today. Then a capped accumulator returns None, whatever the exit code or `is_error`.
- **The argv:** `ClaudeAgent.argv` adds `["--max-budget-usd", f"{call.budget_usd:.2f}"]` right after the `--max-turns` place, when `call.budget_usd` is a finite number ≥ 0.01. No other argv changes, and `CodexAgent.argv` is untouched.
- **The environment:**
  - `ClaudeAgent.env` sets `BASH_OUTPUT_ENV` to `str(spec.bash_output_chars)` when it is above 0, after `call.env`, so the pack's `defaults.env` can't override it.
  - `env_drop` adds `BASH_OUTPUT_ENV` then, so the inherited copy is dropped.
  - With `0`, neither changes.
- **The fake's `capped` act key** is `"turns"` or `"budget"`. The fake:
  1. runs the act's other keys (writes, attempts, `result`/`result_raw`) as usual;
  2. emits a result event with:
     - `subtype` `error_max_turns` / `error_max_budget_usd`;
     - `is_error: true`;
     - `terminal_reason` `max_turns` / `budget_exhausted` (or Task 1 probe (a)'s strings);
     - no `result` text;
     - the usual usage fields;
  3. exits 1.

  The docstring's act-key list gets the line.

- [ ] **Step 1: Write the failing tests**
  - `test_agents.py::test_caps_on_the_argv`:
    - a call with `max_turns=60, budget_usd=10.0` has `--max-turns 60 --max-budget-usd 10.00` in that order;
    - `budget_usd=None` and `budget_usd=0.0` have no `--max-budget-usd`;
    - `max_turns=0` has no `--max-turns`, even with `spec.max_turns=80`;
    - `max_turns=None` with `spec.max_turns=80` has `--max-turns 80` (today);
    - a Codex call with both set equals today's Codex argv.
  - `test_agents.py::test_bash_output_cap_is_runner_owned`:
    - with `bash_output_chars=15000`, `env(call)` has `BASH_MAX_OUTPUT_LENGTH == "15000"` even with `call.env={"BASH_MAX_OUTPUT_LENGTH": "999999"}`, and `env_drop` lists it;
    - with `0`, `env` doesn't set it and `env_drop` doesn't list it. *(Review Focus 5.)*
  - `test_agents.py::test_a_capped_result_is_not_an_interruption`. Feed `parse_claude_line` a result line with `subtype: error_max_turns`, `is_error: true`, then `to_outcome(1)`:
    - `capped == "turns"` and `interruption is None`;
    - the same with `subtype: error_max_budget_usd` gives `"budget"`;
    - `terminal_reason: budget_exhausted` and `subtype: error_during_execution` gives `"budget"`;
    - a capped result then a trailing `success` result stays `"turns"`;
    - a `success` result alone gives None.
  - `test_agents.py::test_a_refused_window_wins_over_a_cap`: a `rate_limit_event` with status `rejected` and `resetsAt`, then a capped result, gives `interruption.kind == "usage_limit"`. *(Review Focus 3.)*
  - `test_fake_agent.py::test_capped_act_ends_like_the_cli`: through `ClaudeAgent.run` on the fake, an act `{"capped": "turns"}` gives exit code 1, `capped == "turns"`, no `final_text`, and `usage.terminal_reason == "max_turns"`.
- [ ] **Step 2: Run them and see them fail**

  Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_agents.py agentflow/tests/test_fake_agent.py --import-mode=prepend -q -k "caps_on_the_argv or bash_output or capped or window_wins"`
  Expected: FAIL.
- [ ] **Step 3: Implement the constants, the fields, the argv and environment changes, the capped reading and the fake's act key**
- [ ] **Step 4: Run the tests again**

  Run: `test_agents.py` and `test_fake_agent.py` in full.
  Expected: PASS. `test_other_calls_keep_their_argv` is unchanged, since its calls set no cap.
  - **Two tests pin `env_drop` exactly** on a pack that now inherits `bash_output_chars: 15000`:
    - `test_the_keys_switch_off` gains `bash_output_chars=0` in its `claude_with(...)` and asserts the variable is neither set nor dropped;
    - `test_a_one_hour_cache_ttl_sets_its_variable_and_drops_the_others` adds `BASH_MAX_OUTPUT_LENGTH` to its expected set.
- [ ] **Step 5: Lint and commit**

  ```bash
  uvx ruff@0.17.4 check .
  git add agentflow/agents.py agentflow/tests/fake_agent.py agentflow/tests/test_agents.py agentflow/tests/test_fake_agent.py
  git commit -m "Pass a per-call spend cap and a runner-owned shell output cap, and read a capped result as a cap, not a dead agent."
  ```

---

### Task 3: Caps on every run call, the wrap-up turn and the `turn_cap` / `budget_cap` stops

**Files:**
- Modify: `agentflow/lanes.py`:
  - the constants `WRAP_UP_TURNS = 3`, `WRAP_UP_BUDGET_USD = 2.5`, `WRAP_UP_PROMPT`, `CAP_KINDS = {"turns": "turn_cap", "budget": "budget_cap"}`, `TODO_SHOWN = 20`;
  - `_Turn.wrap_up: str | None = None`;
  - `_StepRun.budget_usd: float | None = None` and `_StepRun.wrap_ups: int = 0`;
  - `run_agent_step` and `dry_agent_argv`, which set both caps;
  - `_agent_call`;
  - `_loop`;
  - `_hold`, which returns the same wrap-up turn (prompt and `wrap_up`) for a wrap-up turn, whether or not the call reached the model;
  - `_rollover` and `_replace_brief`, which stop as the cap's kind instead of starting a fresh session when the turn is a wrap-up turn;
  - the new `_capped(sr, outcome, turn) -> _Turn | None` and `_cap_stop(sr, cap, result, wrapped: bool) -> None`;
  - `_refused_flag_summary`.
- Modify: `agentflow/hitl.py`: `_how_to_go_on` gets a branch for `turn_cap` and `budget_cap` (after the retro branch, which stays first), holding the ways out below.
- Modify: `agentflow/flow_control.py`: the delta round's base is `config.resolve_turn_cap(...)` (it already imports from `agentflow.config`).
- Modify: `agentflow/state.py`: `REQUIRED["session_capped"] = ("key", "cap")`.
- Modify: `docs/agentflow-playbook.md` (a row in "Stops you will meet", and §7's doubt round line on the halved cap), `CHANGELOG.md` (`Added`)
- Test: `agentflow/tests/test_agent_step.py`, `agentflow/tests/test_doubt_flow.py`, `agentflow/tests/test_retro.py`, `agentflow/tests/test_feature_lane.py`, `agentflow/tests/test_config.py`

**Interfaces:**
- Consumes:
  - Task 1's `config.resolve_turn_cap`, `config.session_budget` and `RUNNER_FLAGS`;
  - Task 2's `AgentCall.budget_usd`, `AgentCall.max_turns == 0` and `AgentOutcome.capped`.
- **`run_agent_step`:**
  - `sr.max_turns = turn_cap if turn_cap is not None else config.resolve_turn_cap(cfg, agent_name, step, role)`, where the parameter `turn_cap` is the delta round's;
  - `sr.budget_usd = config.session_budget(cfg, agent_name, role)`.

  `dry_agent_argv` sets the same two, so `run --dry-run` prints the flags.
- **On resume:**
  - `sr.wrap_ups` is the count of `session_capped` events with `wrap_up: true` in `_after_approval(since, key)`, next to `sr.fixes`;
  - the resumed turn is a wrap-up turn (`wrap_up=<that event's cap>`, with `WRAP_UP_PROMPT`) when the last `session_capped`-or-`fix_attempt` event there is a `session_capped` with `wrap_up: true`.
- **`_agent_call(sr, turn, attempt)`:**
  - for a `turn.wrap_up` turn: `max_turns=WRAP_UP_TURNS`, and `budget_usd=min(WRAP_UP_BUDGET_USD, sr.budget_usd)` when `sr.budget_usd` is set, else None;
  - otherwise: `max_turns=sr.max_turns` and `budget_usd=sr.budget_usd`.
- **`flow_control._run_agent`:** the delta branch's `turn_cap = doubt.delta_turn_cap(resolve_turn_cap(self.cfg, phase.agent or self.cfg.defaults.agent, step, step.role))`. The branch runs only for a doubt reviewer, whose role is `review` (`doubt.is_doubt_reviewer`), so `step.role` is set; `DEFAULT_ROLE` lives in `lanes` and is not imported.
- **A review step** here is `sr.step.role == "review"`, the step's configured role. `blocks_on_findings` alone doesn't make one: `verify` is `role: build` (Decided 5).

**`WRAP_UP_PROMPT`** (exact copy; `{cap}` is `turn` or `spend`), followed by a blank line and `_result_partial(sr)`:

> You have used this session's {cap} budget, so the runner stopped it. Write your result now with what you have, and do not start new work: `done` if the step is finished, else `blocked`, with what is left in `signals.todo` as a list of one-line strings.

**`_loop`, after the guard checks, in this order:**
1. **`outcome.capped` and a review step,** before the result is read, so even a valid result is not used:
   - with `outcome.interruption` (the usage window refused it): `turn = self._hold(sr, turn, outcome.interruption)`, and `return "stopped"` when that is None, else `continue`. The result file is cleared before the next call (`workspace.clear_result`), so the review's result is never read;
   - otherwise: emit `session_capped` with `wrap_up: false`, call `self._cap_stop(sr, outcome.capped, None, wrapped=False)` and return `"stopped"`.
2. **A valid result** is handled as today. With `outcome.capped`, first emit `session_capped` with `wrap_up: false`. *(Review Focus 1.)*
   - **From a `turn.wrap_up` turn,** any status but `done` goes to `_cap_stop(sr, turn.wrap_up, result, wrapped=True)` and returns `"stopped"`, ahead of today's status handling. *(Review Focus 2.)*
3. **The no-result branches:**
   1. `rolled_over`: unchanged, except that on a wrap-up turn it is `self._cap_stop(sr, turn.wrap_up, None, wrapped=True)` and `next_turn = None` (no rollover event, no fresh session);
   2. `interruption`: a capped call has none unless the window refused it (Task 2). On a wrap-up turn, `_hold` returns the same turn, whether or not the call reached the model;
   3. **new:** `outcome.capped`, which goes to `next_turn = self._capped(sr, outcome, turn)`;
   4. **new:** `turn.wrap_up` with no result: `self._cap_stop(sr, turn.wrap_up, None, wrapped=True)` and `next_turn = None`;
   5. the reminder, and then the `blocked` stop (unchanged).

**`_capped(sr, outcome, turn) -> _Turn | None`:**
- **The wrap-up is used:** that is `turn.wrap_up` is set, or `sr.wrap_ups >= 1`. Emit `session_capped` with `wrap_up: false`, call `self._cap_stop(sr, outcome.capped, None, wrapped=bool(turn.wrap_up))`, and return None.
- **Otherwise:**
  1. `sr.wrap_ups += 1`;
  2. emit `session_capped` with `wrap_up: true`;
  3. return `_Turn(WRAP_UP_PROMPT.format(cap=...) + "\n\n" + self._result_partial(sr), sr.session, wrap_up=outcome.capped)`.
- **The event:** `session_capped` carries `key`, `cap`, `session_id`, `limit` (the cap value in force: turns or USD), `wrap_up` (bool).

**`_cap_stop(sr, cap, result, wrapped) -> None`** calls `self._stop(sr, CAP_KINDS[cap], summary, extra=...)`.
- **The limit:**
  - `"turns"`: `<n> turns`;
  - `"budget"`: `$<x:.2f>`.

  When `wrapped` is true, the limit is the wrap-up's own (`WRAP_UP_TURNS`, or the wrap-up budget).
- **Summary (exact copy):**
  - a review step: "The review reached its {what} ({limit}), so the runner did not take it as complete.";
  - `wrapped`: "The wrap-up turn reached its {what} ({limit}) or did not finish: {how}.";
  - otherwise: "The session reached its {what} ({limit}) after its wrap-up turn was used.".
- **The placeholders:**
  - `{what}` is "turn cap" or "spend cap";
  - `{how}` is "no result", or `clean_fact(result.summary)` (cut to `FACT_MAX`).
- **Extra:** `### Left to do`, when `result.signals.todo` is a list: each string item through `clean_fact`, as `- ` bullets, at most `TODO_SHOWN`, then `(N more)`. Nothing else: the ways out are `_how_to_go_on`'s.
- **`hitl._how_to_go_on` for `turn_cap` and `budget_cap`** (exact copy, with the run id and the key filled in):
  - Raise the cap once: `agentflow approve <run> <key>` resumes the session, and each call has its own cap.
  - Split the step: `agentflow approve <run> <key> --note "Finish <part> as done now and stop"`, then add the rest as a new task.
  - Descope: `agentflow approve <run> <key> --note "Drop <part>: <why>"`.
  - Start over: `agentflow retry <run> <key>` starts a fresh session.

  A retro run keeps `_how_to_go_on`'s existing retro text, whose branch comes first.
- **`_refused_flag_summary`:** a refused `--max-turns` or `--max-budget-usd` names its `RUNNER_FLAGS` key as the way to turn it off (set the role's value to `0`), next to the version note.

- [ ] **Step 1: Write the failing tests**
  - `test_agent_step.py::test_every_run_call_carries_both_caps`:
    - a build step's first call and its fix-attempt resume both have `--max-turns 130 --max-budget-usd 10.00`;
    - a review step's call has `--max-turns 100 --max-budget-usd 4.00`;
    - a sizer's call has its own `--max-turns` and no `--max-budget-usd`;
    - with `max_turns_by_role: {build: 0}`, the build call has no `--max-turns`. *(Review Focus 5.)*
  - `test_agent_step.py::test_a_capped_session_wraps_up_once_then_stops`. Script: act 1 `{"capped": "turns"}`, act 2 (the wrap-up) `{"capped": "turns"}`. Then:
    - the second call resumes the first's session with `--max-turns 3 --max-budget-usd 2.50` and a prompt starting "You have used this session's turn budget";
    - the step stops as `turn_cap`, and the inbox page holds "Raise the cap once" with the step key, and not the default "Fix what the stop needs" text;
    - there is no `run_held` event, and two `session_capped` events (`wrap_up` true, then false).
  - `test_agent_step.py::test_a_wrap_up_result_finishes_the_step`: act 1 `{"capped": "budget"}`, act 2 a `done` result. The step runs its gates and finishes `done`.
  - `test_agent_step.py::test_a_wrap_up_that_does_not_finish_is_a_cap_stop`. Parametrize act 2 over:
    - `blocked` with `signals.todo: ["wire the CLI"]`;
    - `needs_human` with one question;
    - `effort_change`.

    Each stops as `budget_cap` with no further call, and the `blocked` case's report holds `### Left to do` and `wire the CLI`. *(Review Focus 2.)*
  - `test_agent_step.py::test_wrap_up_caps_survive_a_hold_and_a_restart`:
    - a wrap-up act `{"usage_pct": [10], "exit": 1}` (an assistant line, so it reached the model) is retried with `--max-turns 3` and the wrap-up prompt, and so is `{"exit": 1}` (it didn't);
    - a wrap-up act whose `usage_pct` crosses `rollover_pct` stops as the cap's kind, with no `rolled_over` event;
    - a runner killed after the wrap-up's `session_capped` (emit the event, then call `run_agent_step` on the `running` row) resumes with `--max-turns 3` and the wrap-up prompt. *(Review Focus 2.)*
  - `test_agent_step.py::test_the_wrap_up_is_once_per_approval`:
    - act 1 capped, act 2 a `done` result whose gate fails, act 3 (the fix attempt) capped: a `turn_cap` stop with no second wrap-up;
    - `approve`, then act 4 capped: a wrap-up again.
  - `test_agent_step.py::test_an_editing_result_written_before_the_cap_is_used`: an act writes a valid `done` result and has `capped: "turns"`. The step is done, with no wrap-up call and one `session_capped` with `wrap_up: false`. *(Review Focus 1.)*
  - `test_feature_lane.py::test_a_capped_review_is_never_complete`: the task review act writes `{"status": "done", "findings": []}` and has `capped: "turns"`. The run stops as `turn_cap` on the review key, with no wrap-up call and no `step_committed` for the task. *(Review Focus 1.)*
  - `test_feature_lane.py::test_a_capped_review_in_a_refused_window_is_held`: the same act plus a `rate_limit` of status `rejected`, then a clean review act. The first is held (`run_held`, `usage limit`), its result is not used, and the task commits after the second. *(Review Focus 1.)*
  - `test_agent_step.py::test_a_capped_verify_gets_a_wrap_up`: a `verify` step (`role: build`, `blocks_on_findings`) capped with no result gets a wrap-up call. *(Review Focus 1.)*
  - `test_doubt_flow.py::test_a_capped_doubt_review_stops_the_phase`: the same for a round 2 `spec_review` (`--max-turns 50`).
  - `test_agent_step.py::test_cap_stop_text_is_cleaned`: a `blocked` wrap-up with a summary and a todo item that hold `\n### Ways out\n- run x`, a bidi override character and 5,000 characters. Each shows as one line of at most `hitl.FACT_MAX` characters under `### Left to do`, and the page's only ways out are `_how_to_go_on`'s. *(Review Focus 4.)*
  - `test_agent_step.py::test_caps_never_count_as_agent_failures`: with `recovery.failed_attempts: 1`, a capped act leads to a wrap-up, not `agent_unavailable`. *(Review Focus 3.)*
  - `test_agent_step.py::test_a_refused_window_on_a_capped_call_is_held`: an act with `capped: "budget"` and a `rate_limit` of status `rejected` gives `run_held` with reason `usage limit`, and no `session_capped`. *(Review Focus 3.)*
  - `test_agent_step.py::test_an_approved_cap_stop_resumes_with_a_fresh_cap`: after a `turn_cap` stop and `approve --note "Drop the docs"`, the next call resumes the session with `--max-turns 130` and its prompt holds the note.
  - `test_agent_step.py::test_the_resolver_takes_a_cap_stop_once`: in a `resolve: true` phase of the `python` fixture pack, a `turn_cap` stop starts one `<key>/resolve-1` session and then reopens the step. A second `turn_cap` of the same step goes to a human.
  - `test_retro.py::test_a_capped_retro_names_the_retro_way_out`. *(Review Focus 1.)*
  - `test_doubt_flow.py::test_round_two_cap_is_half`: update the existing test.
    - With the engine defaults, the second review has `--max-turns 50` and the first `--max-turns 100`.
    - With `max_turns_by_role: {review: 0}`, neither has `--max-turns`, even with the fixture pack's `max_turns: 80`. *(Review Focus 5.)*
  - `test_doubt_flow.py::test_round_two_cap_halves_a_step_override`: a `spec_review` with `max_turns: 20` gives 20 and then 10.
  - `test_config.py::test_a_sparse_pack_dry_run_passes_the_effort`: its assertion `"--max-turns" not in out` pinned "no cap set". It becomes `"--max-turns 110" in out and "--max-budget-usd 10.00" in out`, for the first step's role (`plan`). Check the role, and use that role's values.
- [ ] **Step 2: Run them and see them fail**

  Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_agent_step.py agentflow/tests/test_doubt_flow.py agentflow/tests/test_retro.py agentflow/tests/test_feature_lane.py --import-mode=prepend -q -k "cap or wrap_up or agent_failures or round_two or refused_window"`
  Expected: FAIL.
- [ ] **Step 3: Implement the per-call caps, the review rule, the wrap-up and its carriers, the stops, the delta base, the event's schema and the playbook row**

  **The playbook row** for "Stops you will meet":
  - **Stop:** `turn_cap`, `budget_cap`.
  - **Meaning:**
    - the session reached its role's turn or spend cap;
    - a review reached it and so isn't taken as complete;
    - or one wrap-up turn didn't finish an editing step.

    In a `resolve: true` phase a resolver looks once first.
  - **Do:** the four ways out, and the keys that raise a cap for good (`max_turns_by_role.<role>`, a step's `max_turns`, `session_budget_usd.<role>`).
- [ ] **Step 4: Run the tests again**

  Run these in full:
  - `test_agent_step.py`, `test_doubt_flow.py`, `test_doubt.py`, `test_retro.py`;
  - `test_feature_lane.py`, `test_headless.py`, `test_workflow.py`, `test_autonomy.py`;
  - `test_bug_eval_security.py`, `test_state.py`, `test_config.py`.

  Expected: PASS.
  - **A test that pins a run call's argv:** add the two cap flags to it. Never drop an assertion about another flag.
  - **An `exit: 1` act without `capped`** is held as today.
- [ ] **Step 5: Lint and commit**

  ```bash
  uvx ruff@0.17.4 check .
  git add agentflow/lanes.py agentflow/flow_control.py agentflow/state.py docs/agentflow-playbook.md CHANGELOG.md agentflow/tests/
  git commit -m "Cap every run session's turns and spend by role, stop a capped review, give a capped editing session one wrap-up turn, then stop it as turn_cap or budget_cap with its ways out."
  ```

---

### Task 4: Context hygiene in the prompts (L6b, L6c), with their `Retired` entries

**Files:**
- Modify: `agentflow/prompts/_preamble.md` (`## Shell`: three new lines between two existing ones)
- Modify: `agentflow/prompts/task_inline.md`, `fix.md`, `ci_fix.md`, `fix_findings.md`, `sync_conflicts.md`: one sentence each.
- Modify: `agentflow/skills/agentflow-build/SKILL.md`: step 3.
- Modify: `agentflow/lanes.py`: `_fix_lead`'s last sentence.
- Modify: `agentflow/pack_fix.py`: six `RETIRED` entries. `_scan` is unchanged.
- Modify: the overrides of the five prompts in `agentflow/tests/fixtures/packs/python/.agentflow/prompts/` and `.../node/.agentflow/prompts/`.
- Modify: `.agentflow/prompts/*.md` (Decided 12)
- Modify: `CHANGELOG.md` (`Changed`)
- Test: `agentflow/tests/test_pack_fix.py`, `agentflow/tests/test_agent_step.py`

**The text** (exact copy; `old` is the current text, quoted from the file):

| File | Old sentence | New sentence | `detect` |
|---|---|---|---|
| `_preamble.md` | `the runner's gates run it.\n- A denied command stays denied` (the junction of lines 18 and 19) | `the runner's gates run it.\n` + the three lines below, each ending `\n`, + `- A denied command stays denied`:<br>`- Use quiet flags for the tests you run (such as \`-q\`, no coverage report), and show only the failures.`<br>`- Find files with Glob and search with Grep, with a result limit, not \`grep -r\`, \`find\` or \`ls -R\` through Bash.`<br>`- Read a file once, and read it again only after it changed.` | the whole `old` |
| `task_inline.md` | `Run the build gates before you finish.` | `Run only the tests you wrote or changed: the runner runs the build gates after you.` | `Run the build gates before you finish` |
| `fix.md` | `Run the test and see it go from red to green, then run the build gates.` | `Run the test and see it go from red to green: the runner runs the gates after you.` | `then run the build gates` |
| `ci_fix.md` | `Run the verify gates before you finish. Do not commit: the runner commits and pushes.` | `Run only the tests that cover your fix: the runner runs the verify gates after you. Do not commit: the runner commits and pushes.` | `Run the verify gates before you finish. Do not commit` |
| `fix_findings.md` | `so do not review your own work. Run the verify gates before you finish.` | `so do not review your own work. Run only the tests that cover your fixes: the runner runs the verify gates after you.` | `do not review your own work. Run the verify gates` |
| `sync_conflicts.md` | `\nRun the verify gates before you finish.\n` (the line on its own) | `\nRun only the tests that cover the files you merged: the runner runs the verify gates after you.\n` | `Run the verify gates before you finish` |
| `SKILL.md` step 3 | `Run only the tests you wrote or changed, and their own directory once at the end. Never run the whole suite.` | `Run only the tests you wrote or changed, with quiet flags, and their own directory once at the end. Never run the whole suite.` | — |
| `lanes._fix_lead` | `Run the failing gates yourself, then write the result file again.` | `Re-run only what failed (the failing tests, or the one lint or type check), with quiet flags, not the whole gate, then write the result file again: the runner runs every gate after you.` | — |

- **The `RETIRED` entries:**
  - `id`s of the form `<file stem>-runner-runs-gates`, and `preamble-quiet-search-read-once`;
  - `retired_in="2.10"`;
  - appended in this order: `preamble`, `task_inline`, `fix`, `ci_fix`, `fix_findings`, `sync_conflicts`.
- **Why that order:** `_scan` applies every entry to every pack prompt (Decided 11). `ci_fix`'s and `fix_findings`' longer `old`s replace their sentence before `sync_conflicts`' line-only `old` is tried, and a line-only `old` can't match the mid-line sentence of `fix_findings.md`.
- **The `sync_conflicts` `detect` is the bare sentence.** After the fix, any copy of it left in any pack prompt is retired text, so the warning is correct.
- **The register test:** `test_register_matches_the_engine_prompts` requires every `detect` to appear in no engine prompt. Before relying on the table's tokens, `grep -rn` each one over `agentflow/prompts/`; if one is still found there after the edits, pick a longer fragment of its `old`.

- [ ] **Step 1: Write the failing tests**
  - `test_pack_fix.py::test_shared_sentence_gets_each_files_own_fix`. Overrides of `ci_fix.md`, `fix_findings.md` and `sync_conflicts.md`, each the engine's old file text, are each fixed to their own new engine text, and each warning names its own file's entry.
  - `test_pack_fix.py::test_c4_retired_entries_fix_old_overrides`: for each of the six files, an override holding the engine's old file text gets one warning naming its entry, and after `--fix` it is equal to the new engine file (so the fix deletes it, as an override identical to the engine).
  - `test_pack_fix.py::test_no_engine_prompt_tells_a_session_to_run_its_gates`: no engine prompt except `gate_fix.md` matches `Run the (build|verify) gates before you finish` or `then run the build gates`.
  - `test_agent_step.py::test_fix_lead_reruns_only_what_failed`: a fix-attempt prompt holds "Re-run only what failed" and not "Run the failing gates yourself".
- [ ] **Step 2: Run them and see them fail**

  Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_pack_fix.py agentflow/tests/test_agent_step.py --import-mode=prepend -q -k "own_entries or c4_retired or run_its_gates or reruns_only"`
  Expected: FAIL.
- [ ] **Step 3: Edit the prompts, the skill and `_fix_lead`, add the entries, then repair the fixture packs and this repository's pack**
  - **The fixture packs:** edit the same sentence in each of the five overrides of both fixture packs by hand, keeping the rest of each file. The fixtures are test data, and `validate --fix` would delete an override that becomes identical to the engine's file.
  - **This repository's pack** (on this branch of the main checkout, Decided 12):
    1. run `bin/agentflow validate`, whose warnings should name the new entries for the five `.agentflow/prompts/` overrides;
    2. run `bin/agentflow validate --fix --dry-run` and check the diff;
    3. run `bin/agentflow validate --fix`.

    An override that edited the old sentence (so `--fix` skips it) is edited by hand to the new wording.
- [ ] **Step 4: Run the tests again**

  Run these in full, then `bin/agentflow validate`:
  - `test_pack_fix.py`, `test_agent_step.py`, `test_brief.py`;
  - `test_portability.py`, `test_feature_lane.py`, `test_headless.py`;
  - `test_plug_and_play.py`, `test_explain.py` (or whichever file pins the golden `explain` output).

  Expected: PASS, `0 warnings`, and `git diff --stat agentflow/tests/fixtures/explain` empty.
  - **Existing `test_pack_fix.py` tests that count the warnings** on an old `_preamble.md` (`test_preamble_and_result_c2_rewords_are_fixable` expects 2) gain one for `preamble-quiet-search-read-once`. Update the count. Every "fixes to the engine's file" assertion must still hold unchanged.
  - **`test_unusual_name_is_not_marked_fixable`** is unchanged.
- [ ] **Step 5: Lint and commit**

  ```bash
  uvx ruff@0.17.4 check .
  git add agentflow/prompts agentflow/skills/agentflow-build/SKILL.md agentflow/lanes.py agentflow/pack_fix.py agentflow/tests/ .agentflow/prompts CHANGELOG.md
  git commit -m "Tell sessions the runner runs the gates after them, to re-run only what failed with quiet flags, and to search with Grep and Glob and read a file once."
  ```

---

## Finishing the slice

- [ ] **Full suite, lint and validate.** Run the full suite, `uvx ruff@0.17.4 check .` and `bin/agentflow validate`. Expect green, `All checks passed!` and `0 warnings`. The golden `explain` files must be unchanged: `git diff --stat agentflow/tests/fixtures/explain` is empty.
- [ ] **Final review.** Review the branch with `security-and-hardening` and the `security-auditor` agent. Focus on:
  - the `--max-budget-usd` value and `RUNNER_FLAGS`;
  - `BASH_MAX_OUTPUT_LENGTH`'s ownership;
  - a capped call skipping `_hold`'s failure count;
  - a capped review never passing;
  - every carrier of the wrap-up's caps;
  - `clean_fact` on the stop's model text;
  - each `Retired` entry's `old` matching only its own file;
  - the `Retired` entries' `old` text matching the shipped files byte for byte.
- [ ] **Real-run check (spec §9).** Run one feature run on agentflow itself. The PR description gives:
  - **The Task 1 probe results,** (a)–(e), and which `max_turns_by_role` values shipped.
  - **The scorecard against the C0 baseline:**
    - `session_turns_p90:*`, with no kind's p90 above half its cap;
    - `tool_output_kchars:*` for build and fix step kinds, expected down (no in-session full suite);
    - `gate_first_pass:*` and the review blocking-finding rate, no worse than C3's (spec §5 Goal 5).
  - **Caps:** every `session_capped` event, with its step and what followed. Expect none in a normal run, because the caps are a backstop. One firing on an ordinary session means Decided 1's values need raising before release.
  - **The two out-of-scope items** from "Decided in this plan".
