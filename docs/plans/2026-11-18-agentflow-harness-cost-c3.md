# agentflow cost, slice C3 (read-only reviews and delta doubt rounds) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A `role: review` Claude session reads the code and runs read-only `git`, but it can't edit the tree and has no MCP servers. The runner's own rules don't let it run tests or builds. Its prompt carries the gate results instead, and its watchdog fires after 5 quiet minutes when no tool call is open. A doubt review after the first round gets only the artifact's diff since the last review, and the dispositions to check.

**Architecture:**
- **Read-only sessions (L4).**
  - A per-session flag, `AgentCall.read_only_role`, makes `ClaudeAgent.argv` pass:
    - `--permission-mode dontAsk`;
    - `--tools Read,Grep,Glob,Bash,Write`;
    - an allow list in the runner's `--settings` JSON;
    - no MCP servers;
    - `--disable-slash-commands`.
  - `lanes.Executor` decides the flag when a session starts, records it on `agent_started`, and every resume of that session uses the recorded value.
  - `guards.check_step` gains a `read_only` mode: any change to the tree is a `read_only_edit` violation. A retry is refused while those edits are still in the tree.
- **The prompt.** A new `{read_only_note}` slot holds two things:
  - a fixed note: denials are expected here, carry on, and leave `progress.md` alone;
  - a table of the run's gate results (`gates.results_table`).

  It is appended to a read-only session's prompt when the template has no slot of its own.
- **Self-contained prompts.** Read-only, reconcile and record steps get `--disable-slash-commands`. The skill lines in their template text point at the skill's file instead. This finishes C1's deferred item (spec §3.2 L2b, "Amended for the C0+C1 plan").
- **Delta rounds (L3c).**
  - `doubt.delta_for_review` builds the round 2+ lead from `git diff <the last review's start tree> -- <artifact>`.
  - `flow_control._run_agent` puts it in front of the existing `earlier_findings`, and halves the session's turn cap.

**Tech Stack:** Python 3.12 stdlib + PyYAML; pytest with `tests/fake_agent.py`.

**Spec:** `docs/specs/agentflow-cost-proposal.md`:

| Section | What this plan implements from it |
|---|---|
| §3.2 L2b | the C1 amendment's deferred item: `--disable-slash-commands` for review, reconcile and record steps |
| §3.3 L3c | all of it; the halved cap uses today's `agents.<name>.max_turns` as its base until C4 |
| §3.4 L4 | all of it, except the Codex line, with the idle limit changed (see "Decided in this plan") |
| §3.5 L5c | `watchdog.read_only_idle_min` |
| §4 | the rows `agents.claude.read_only_roles` and `watchdog.read_only_idle_min` |
| §5, §8, §9 | what stays as it is, the read-only reviewer risk, and the testing strategy |
| §7 | the "C3 Reviews" row |

## Decided in this plan

These are choices the spec leaves open. Confirm them at review. Task 1 records each one in the spec as an "Amended for the C3 plan" note (the repository's convention).

1. **The allow list goes in the `--settings` JSON, not `--allowedTools`.**
   - **Why:** the runner already passes a `--settings` JSON with its deny rules. A list there has no comma-splitting hazard.
   - **Fallback:** if the Task 2 probe shows `--settings` allow rules aren't honoured under `dontAsk`, use one comma-joined `--allowedTools` argument right after `--tools`. A workspace path with `,`, `(` or `)` then joins the refused characters of Decided 6.
2. **L4 is a cost and independence control, not a sandbox.**
   - **What can widen the rules:** under `--setting-sources project,local`, these still apply to a read-only session:
     - the project's `.claude/settings.json` and `settings.local.json`, including a worktree copy an earlier step of the run wrote;
     - their hooks (a `PreToolUse` hook can allow a call);
     - with `user_settings: true`, the user's own rules.
   - **Example:** this repository's file allows its `pytest` command.
   - **What the runner adds:** its own allow list holds no test, build or write command. The prompt asks the reviewer not to run them. The guard catches tracked and untracked edits to the tree.
   - **The playbook line** names all of this.
   - **Added in the fix wave:** the agent definition passed with `--agent` (the overlay's review agent, read from the work tree) and a project settings file's `sandbox.enabled` (`autoAllowBashIfSandboxed`) can widen a session too; a 2.1.328 probe saw neither do so (other versions unverified). The guard covers only the work tree: not writes or deletes outside it by a project-allowed command (`--basetemp`, `--junitxml` into the state dir), nor files hidden by an untracked `.gitignore` or `.git/info/exclude`. The write rule is the workspace's `out/` folder only, so a reviewer can't rewrite `progress.md`.
3. **Codex is unchanged.**
   - A Codex review must write its result file, and `-s read-only` would refuse that.
   - `read_only_roles` on a non-Claude agent is a `validate` warning ("has no effect").
   - Spec §3.4's Codex line moves to a later slice, which needs a result taken from the final message.
4. **The round 2+ turn cap** is `max(1, base // 2)`, where `base` is the agent's `max_turns` (unset by default, which means no cap). C4 swaps the base for `max_turns_by_role`.
5. **The idle limit** (this changes spec §3.4's "no reason to wait 15–30 minutes"):
   - A read-only session's limit with **no tool call open** is `min(no_progress_min, read_only_idle_min)` minutes.
   - **While a tool call is open,** it keeps today's `max(idle, gate_timeout_min)`. Decided 2 lets a reviewer run a long command, and killing it at 5 minutes would stop the step.
   - `watchdog.no_progress_min: 0` switches every watchdog off.
   - `read_only_idle_min: 0` gives read-only sessions today's limits.
6. **A workspace path the allow rule can't name exactly** makes the step run as today, not read-only, with a `read_only_skipped` note recorded once per run. Such a path holds one of `*?[]{}\` (and `,()` under Decided 1's fallback). The run dir comes from the repository's directory name (`pack.repo_id`). This is a fallback toward today's behaviour, never a failed review.
7. **The `read_only_edit` guard compares the tree snapshot** (tracked and untracked, not ignored files), as `forbidden_path` does.
   - **Not covered:** ignored paths, `.git/config` and hooks. The sizer's strict `git_state` + `tree_state` check would fail every test run Decided 2 allows (`__pycache__`, `.pytest_cache`).
   - **Trade-off, written down:** this is the same coverage every build step has today.
8. **Denials in read-only sessions** are recorded as `permission_denied` with `read_only: true`.
   - `signals.py` leaves them out of `permission_denied:<tool>`, so the scorecard and the retro aren't told to re-allow tools.
   - A `blocked` stop's denied report says the denial is by design, and doesn't suggest a `settings.local.json` rule.

## Global Constraints

- **Dependencies:** engine dependencies stay stdlib + `pyyaml>=6`.
- **No project strings** in engine files (`test_portability.py`).
- **Golden files:** `agentflow/tests/fixtures/explain/*.txt` stay byte-identical. `explain` shows no session flags.
- **Config keys:** every new key ships, in the same task, with all four of these:
  - a Settings form field;
  - its `validate` checks;
  - a `test_config_edit.py` case;
  - a playbook §14 line.
- **Prompt placeholders:** `read_only_note` joins `config.PROMPT_PLACEHOLDERS`.
- **Engine prompts:** no `agentflow/prompts/*.md` file is reworded. New text lives in Python constants and is appended, so no `pack_fix.Retired` entry is needed. If an implementer does reword a prompt, a `Retired` entry with `retired_in="2.10"` is required.
- **Changelog:** every task with a user-visible change adds one line under `## [Unreleased]` in `CHANGELOG.md`.
- **Defaults:**
  - `agents.claude.read_only_roles: [review]`
  - `watchdog.read_only_idle_min: 5`
  - `DELTA_MAX_LINES = 400`
  - `GATE_TABLE_MAX_ROWS = 20`
- **Read-only tools:** `REVIEW_TOOLS = ("Read", "Grep", "Glob", "Bash", "Write")`.
- **The allow list,** in this order:
  - `Read`, `Grep`, `Glob`;
  - for each of `diff`, `log`, `show`, `status`, `blame`: `Bash(git <c>)` and `Bash(git <c> *)`;
  - `Edit(/<dir>/**)` for each `add_dir`. `pack.state_dir` already resolves the path, so no realpath variant is needed.
- **Extra deny for read-only calls:** `Bash(git *--output*)`. It goes on top of today's deny list, which stays as it is.
- **Unchanged:** the engine floor, review independence (every reviewer is a fresh session), the doubt stop rules and numbering, `verify` (`role: build`), and per-task commits.
- **Lint and tests:**
  - `uvx ruff@0.17.4 check .` must print `All checks passed!` before each commit.
  - Tests run with `uv run --no-project --with pyyaml --with pytest pytest <file> --import-mode=prepend -q`.
- **Security overlay:** `agents.py` and `guards.py` change. Plan and review with `security-and-hardening` and the `security-auditor` agent, with particular attention to the allow list, the workspace write rule, the extra deny rule and the `base_args` checks.

## Review Focus

1. **A reviewer denied a command.** Its prompt tells it to carry on, not to stop `blocked`. The preamble says otherwise, but the note is the last word on it. The step finishes with its result. If it does stop `blocked`, the inbox report says the denial is by design. *(Tasks 3 and 4.)*
2. **A read-only session that resumes** (a reminder, a continue after `needs_human`, an effort change), **a config edit in between, and the retro** (`retro_step` is `role: review`):
   - every call of a session keeps the read-only setting it started with, even after `read_only_roles` changes;
   - the retro still reads its digest and writes its result;
   - a `role: review` step on Codex gets today's argv.

   *(Task 3.)*
3. **A reviewer that changes the tree anyway** (a hook, a project allow rule, a Bash built-in that writes):
   - the step fails with `read_only_edit`, and the stop names the paths and says to revert them;
   - `retry` is refused while they are still changed against that session's `start_tree`;
   - writes to the workspace (the result) are not violations.

   *(Task 3.)*
4. **A delta round with no usable base:**
   - no earlier review in this human round;
   - a restarted round-2 review, whose own `step_started` is the newest one;
   - a base that is no longer readable;
   - the artifact deleted, new since the base, or given as an absolute path, a `..` path or a pathspec-magic path;
   - a diff over `DELTA_MAX_LINES`;
   - an empty diff.

   Each gives the full round-1 prompt plus the earlier findings, as today, and never an error or a stop. *(Task 5.)*
5. **Gate results that are awkward:**
   - none yet (define and plan reviews): the note shows, with no table;
   - a skipped gate;
   - an `affected`-tier result after a full one;
   - more than 20 gates;
   - a gate name with `|` in it.

   The table shows the newest result per gate, escapes `|`, and stays within 20 rows plus an "older gates not shown" line. *(Task 4.)*

---

### Task 1: The two config keys, the `base_args` checks and the spec amendments

**Files:**
- Modify: `agentflow/config.py`:
  - `AgentSpec` gains `read_only_roles: tuple[str, ...] = ()`, read with `_optional` as a list of strings.
  - `Watchdog` gains `read_only_idle_min: float = 5`. Add `"read_only_idle_min"` to `WATCHDOG_KEYS`, and read it with `_float` as `no_progress_min` is.
  - `READ_ONLY_FLAGS = ("--tools", "--settings", "--agents", "--plugin-dir")`.
  - `_Validator`: the checks below.
- Modify: `agentflow/defaults/config.yaml`: `agents.claude.read_only_roles: [review]` and `watchdog.read_only_idle_min: 5`, one comment each.
- Modify: `agentflow/config_edit.py`:
  - `AGENT_FREE_KEYS` gains `read_only_roles`, and the protected-key note follows it;
  - the Claude section gets `{"key": ["agents", name, "read_only_roles"], "label": "Read-only roles (no edits, no tests)", "kind": "list"}`;
  - the watchdog field `read_only_idle_min` is `{"kind": "number", "min": 0, "max": 1440}`, labelled "End a read-only session that is quiet with no tool running after (min, 0 = as above)".
- Modify: `docs/agentflow-playbook.md` §14 (two key lines), `docs/specs/agentflow-cost-proposal.md` (the "Amended for the C3 plan" notes), `CHANGELOG.md` (`Added`)
- Test: `agentflow/tests/test_config.py`, `agentflow/tests/test_config_edit.py`

**Interfaces:**
- Produces:
  - `cfg.agents["claude"].read_only_roles` (default `("review",)` from the engine defaults; `()` for an agent that doesn't set it);
  - `cfg.watchdog.read_only_idle_min` (default 5.0).

**The `validate` checks.** A step's agent is `phase.agent or defaults.agent`. `record_step`, `resolve_step` and `retro_step` use `defaults.agent`. Only the configured `role` is checked; a role passed at run time is the caller's.
- **Error at `agents.<name>.read_only_roles`:**
  - the value is not a list of strings;
  - an entry is not a key of that agent's `models` map.
- **Error at the step's path:** a step whose agent lists its role in `read_only_roles` and that edits the tree. That is a step with `commit`, `reconciles`, `fix_loop`, `output`, `expect` or `gates` set. Walk the phase steps, their `review`/`fix`/`sync` sub-steps, `record_step`, `resolve_step` and `retro_step`.
- **Error at `agents.<name>.base_args`:** a `READ_ONLY_FLAGS` flag (the bare form or `--flag=`) while that agent's `read_only_roles` isn't empty. The runner sets these flags for read-only calls, and a pack copy could widen them.
- **Warning at `agents.<name>.read_only_roles`:** a non-empty list on an agent other than `claude` (Decided 3).
- **Error at `watchdog.read_only_idle_min`:** the same rule as `no_progress_min`, that is 0 or a finite number up to `WATCHDOG_MAX_MIN`.
- **Warning at `watchdog.read_only_idle_min`:** greater than a non-zero `no_progress_min`, where it has no effect.

- [ ] **Step 1: Write the failing tests**
  - `test_config.py::test_cost_c3_keys_default`: the engine defaults give `("review",)` and `5.0`.
  - `test_config.py::test_read_only_roles_must_be_known_roles`: `[reviewer]` is one error, and `review` is not.
  - `test_config.py::test_a_read_only_role_on_a_step_that_edits_is_an_error`. Use a pack that keeps the engine's default phases:
    - `read_only_roles: [review, build]` errors on the build task step;
    - `[plan]` errors on `plan_reconcile` (`reconciles`) and on `plan` (`output`).
  - `test_config.py::test_read_only_flags_in_base_args_are_errors`:
    - `--tools Edit` and `--settings={}` each give one error with the default `read_only_roles`;
    - with `read_only_roles: []`, neither does.
  - `test_config.py::test_read_only_roles_on_codex_warns`.
  - `test_config.py::test_read_only_idle_min_checks`:
    - `-1` and `.inf` are errors;
    - 20 with `no_progress_min: 15` warns;
    - 0 is valid.
  - `test_config_edit.py::test_read_only_roles_and_idle_are_editable`: both fields round-trip with comments kept, and `-1` and `1441` are refused.
  - Update `test_config_edit.py::test_the_codex_section_has_no_claude_cache_fields`. It pins `AGENT_FREE_KEYS[-3:]` and the protected note's text, so extend both to the new key.
- [ ] **Step 2: Run them and see them fail**

  Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_config.py agentflow/tests/test_config_edit.py --import-mode=prepend -q -k "c3_keys or read_only or codex_section"`
  Expected: FAIL.
- [ ] **Step 3: Implement the keys, the checks, the form fields, the playbook lines, the spec notes and the changelog line**

  The playbook line for `agents.claude.read_only_roles` says:
  - what such a session gets: `dontAsk`, the five tools, read-only `git`, writes only in the run's workspace, no MCP servers and no skills listing;
  - that its prompt carries a note and the gate results;
  - that `guards` fail a step whose session changed the tree, and refuse a retry until it is reverted;
  - Decided 2 in full, and Decided 6;
  - that it has no effect on Codex.

  The line for `watchdog.read_only_idle_min` gives Decided 5. The spec gets a short "Amended for the C3 plan" note under §3.3 L3c (Decided 4) and §3.4 L4 (Decided 1, 2, 3, 5, 6 and 7).
- [ ] **Step 4: Run the tests again**

  Run: the Step 2 command, then `bin/agentflow validate`.
  Expected: PASS, and `0 warnings`. The fixture packs still validate: their `role: review` steps have no `commit`, `gates` or `output`, and their `base_args` carry no read-only flag. If one does, the fixture is wrong, not the check.
- [ ] **Step 5: Lint and commit**

  ```bash
  uvx ruff@0.17.4 check .
  git add agentflow/config.py agentflow/defaults/config.yaml agentflow/config_edit.py docs/agentflow-playbook.md docs/specs/agentflow-cost-proposal.md CHANGELOG.md agentflow/tests/test_config.py agentflow/tests/test_config_edit.py
  git commit -m "Add agents.claude.read_only_roles and watchdog.read_only_idle_min with their validate checks."
  ```

---

### Task 2: The read-only argv, and a fake agent that enforces permissions

**Files:**
- Modify: `agentflow/agents.py`:
  - the constants `REVIEW_TOOLS`, `READ_ONLY_ALLOW`, `READ_ONLY_DENY` and `RULE_UNSAFE = frozenset("*?[]{}\\")`;
  - `AgentCall.read_only_role: bool = False`;
  - `ClaudeAgent.argv`;
  - `ClaudeAgent.takes_read_only_role = True`.
- Modify: `agentflow/tests/fake_agent.py`: a new act key, `attempts`, and a small permission model.
- Test: `agentflow/tests/test_agents.py`, `agentflow/tests/test_fake_agent.py` (create it if no test file covers the fake yet)

**Interfaces:**
- Produces: `AgentCall.read_only_role`, commented "a `read_only_roles` session: `dontAsk`, `REVIEW_TOOLS`, read-only git, writes only in `add_dirs`, no MCP server".
- Produces: `agents.read_only_allow(add_dirs: list[Path]) -> list[str]`. It is `READ_ONLY_ALLOW` plus `Edit(/{d}/**)` per directory, and it raises `ValueError` when a path holds a `RULE_UNSAFE` character.
- Produces: `ClaudeAgent.takes_read_only_role = True`. `CodexAgent` doesn't define it, so read it with `getattr(agent, "takes_read_only_role", False)`.

**The argv for `read_only_role`, against today's:**
- **Base args:** `_without_mcp_config(spec.base_args)`, as for `read_only`.
- **Tools:** `--tools Read,Grep,Glob,Bash,Write`, at the place `--tools` already goes.
- **MCP:** `--strict-mcp-config` exactly once, whether `lean` is set or not.
- **Permission mode:** `--permission-mode dontAsk` in place of `spec.permission_mode`.
- **Settings:** the `--settings` JSON becomes `{"permissions": {"allow": read_only_allow(call.add_dirs), "deny": [...today's..., *READ_ONLY_DENY]}}`.
- **Skills listing:** `SELF_CONTAINED_ARGS` comes through `self_contained`, which the executor sets (Task 3).
- **Unchanged for every other call:** its argv and its settings JSON stay byte for byte as they are.

**The fake agent's permission model.** It is close to the real CLI, so the tests built on it encode the right model. The `attempts` act key takes a list of `{tool_name, tool_input}`, and each entry is decided in this order:
1. **Not in `--tools`:** the tool doesn't exist. The fake records an `unknown_tool` entry in a new `unavailable` list of the result event. It is not a permission denial, and the call does nothing.
2. **Bash:** split `input.command` on `&&`, `||`, `;`, `|` and on `>`/`>>` redirections. A redirection is a write to its target path.
3. **Deny rules:** the deny rules in `--settings` win, for any part.
4. **`dontAsk`:** every part needs a matching allow rule:
   - a bare tool name;
   - `Bash(<pattern>)`, matched with `fnmatch` on the part;
   - `Edit(/<abs>/**)`, for a `Write`/`Edit` `file_path` or a redirection target under `<abs>`.
5. **Any other mode:** the call is allowed.

A permitted `Write` writes `input.content`. A denied call goes into the result event's `permission_denials`.

- [ ] **Step 0: Probe the real CLI**

  This step is interactive, needs a logged-in `claude`, and costs under a cent on Haiku. Run each probe in a scratch git repo with `--max-turns 3`, using:
  - `--permission-mode dontAsk --tools Read,Grep,Glob,Bash,Write`;
  - `--add-dir <tmp ws>`;
  - `--settings '{"permissions":{"allow":[…the list…],"deny":["Bash(git *--output*)"]}}'`;
  - `--strict-mcp-config --disable-slash-commands --setting-sources project,local`.

  Record yes or no for each:

  | # | Probe | Expected |
  |---|---|---|
  | a | `git status` runs | yes |
  | b | `touch x` and `python -m pytest` | denied |
  | c | `Write` to `<ws>/out/r.json` | succeeds |
  | d | `Write` to `./x` | denied |
  | e | `Edit` is an available tool | no |
  | f | `grep -n '^## ' <ws>/d.md` | runs |
  | g | `git diff --output=y` | denied |
  | h | `git diff > y` and `git log >> <ws>/../z` | denied |
  | i | `GIT_PAGER=x git diff` | denied |
  | j | `Read` of a file outside the cwd and `<ws>` (a bundled skill's absolute path) | allowed |
  | k | with `--agent <a profile whose frontmatter lists Edit and sets permissionMode>`: (b), (d) and `Edit` | still denied |

  **What a failed probe means:**
  - (c) fails: switch to Decided 1's fallback, then probe again.
  - (f) or (j) fails: note it for the retro's prompt and the skill pointers, and go on.
  - (b), (d), (g), (h), (i) or (k) fails: **stop.** Bring the result to the user before Step 1, because the allow list isn't the boundary the plan assumes.

  The results go in the PR description.
- [ ] **Step 1: Write the failing tests**
  - `test_agents.py::test_a_read_only_role_call_cannot_edit_or_test`:
    - argv has `--permission-mode dontAsk` once, and no `bypassPermissions`;
    - `--tools Read,Grep,Glob,Bash,Write` and `--strict-mcp-config` once;
    - no `--mcp-config`, even when `base_args` carries one;
    - the settings `allow` is exactly `read_only_allow([ws])`, and the `deny` ends with `Bash(git *--output*)`.
  - `test_agents.py::test_read_only_allow_refuses_pattern_characters`: `ws/app[1]` and `ws/a*b` raise `ValueError`.
  - `test_agents.py::test_other_calls_keep_their_argv`: a build call and a sizer call (`read_only=True`) give argv equal to a literal copy of today's lists.
  - `test_agents.py::test_codex_ignores_read_only_role`.
  - `test_fake_agent.py::test_dont_ask_denies_what_is_not_allowed`. Through `ClaudeAgent.run` on the fake, with the read-only argv:
    - `Edit src/a.py` is `unavailable` and the file is unchanged;
    - `Bash "pytest -q"`, `Bash "git diff > src/a.py"` and `Write src/a.py` are denied;
    - `Bash "git diff HEAD~1"` is allowed;
    - `Write <ws>/out/r.json` is allowed and writes the file.

    `permission_denials` lists exactly the three denied calls.
- [ ] **Step 2: Run them and see them fail**

  Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_agents.py agentflow/tests/test_fake_agent.py --import-mode=prepend -q -k "read_only or keep_their_argv or codex_ignores or dont_ask"`
  Expected: FAIL.
- [ ] **Step 3: Implement the flag, the argv branch, `read_only_allow` and the fake's model**
- [ ] **Step 4: Run the tests again**

  Run: `test_agents.py` and `test_fake_agent.py` in full.
  Expected: PASS.
- [ ] **Step 5: Lint and commit**

  ```bash
  uvx ruff@0.17.4 check .
  git add agentflow/agents.py agentflow/tests/fake_agent.py agentflow/tests/test_agents.py agentflow/tests/test_fake_agent.py
  git commit -m "Give read-only role calls dontAsk, a read-only tool set and an allow list that writes only to the workspace."
  ```

---

### Task 3: Run review steps read-only: the per-session decision, the guard, self-contained prompts and the watchdog

**Files:**
- Modify: `agentflow/lanes.py`:
  - `_StepRun.read_only: bool = False`;
  - `Executor._decide_read_only(sr)` and `_self_contained(sr)`;
  - `_idle_s(read_only: bool)`;
  - `_agent_call`;
  - `_call` (`agent_started.read_only`; `permission_denied` gets `read_only: true` when set);
  - `_loop`'s `check_step` call;
  - `run_agent_step`'s retry check;
  - `_denied_report`;
  - `_prompt`, `build_prompt(..., self_contained: bool = False)` and `_read_file_skills(..., resolve=None)`.
- Modify: `agentflow/state.py`: the step row keeps `read_only` from `agent_started`, beside the session id (the brief pin's pattern).
- Modify: `agentflow/guards.py`: `check_step(..., read_only: bool = False)`, and the violation kind `read_only_edit` added to `ViolationKind`.
- Modify: `agentflow/signals.py`: skip a `permission_denied` event with `read_only: true`.
- Modify: `CHANGELOG.md` (`Changed`)
- Test: `agentflow/tests/test_agent_step.py`, `agentflow/tests/test_guards.py`, `agentflow/tests/test_feature_lane.py`, `agentflow/tests/test_retro.py`, `agentflow/tests/test_signals.py`

**Interfaces:**
- Consumes: Task 1's `read_only_roles` and `read_only_idle_min`; Task 2's `AgentCall.read_only_role`, `takes_read_only_role` and `read_only_allow`.
- Produces: `guards.check_step(cfg, worktree, start_head, start_tree, end_tree, tool_calls, forbid=(), read_only: bool = False) -> list[Violation]`. With `read_only`, every path in `changed_paths(start_tree, end_tree)` that isn't already a `forbidden_path` gives `Violation("read_only_edit", path)`.
- Produces: `Executor._decide_read_only(sr) -> None`, which sets `sr.read_only`.
  - **A fresh session** is read-only when all of these hold:
    - `sr.role` is in `self.cfg.agents[sr.agent_name].read_only_roles`;
    - `getattr(sr.agent, "takes_read_only_role", False)` is true;
    - `read_only_allow([self.run.agent_dir])` doesn't raise. If it does, record `read_only_skipped` once per run, with the reason (Decided 6).
  - **A resumed session** (resume, refine resume, shared session) takes the `read_only` recorded for that session, whatever the config says now. A session recorded before this slice counts as `False`.
- Produces: `Executor._self_contained(sr) -> bool`, which is `sr.read_only`, or `sr.step.reconciles`, or `sr.step.id == self.cfg.record_step.id` when there is a record step.

**Behaviour:**
- **`_agent_call`:**
  - it sets `read_only_role=sr.read_only` and `self_contained=self._self_contained(sr)` on every call of the session;
  - `dry_agent_argv` decides the fresh-session value, so `run --dry-run` prints the read-only argv.
- **`_idle_s(read_only)`** (Decided 5):
  - with `no_progress_min <= 0`, it returns `(None, None)`;
  - with `read_only` and `read_only_idle_min > 0`, it returns `(min(no_progress_min, read_only_idle_min) * 60, today's tool limit)`;
  - otherwise it returns today's pair.
- **Self-contained prompts.** `build_prompt(..., self_contained=True)` applies the `read_file` rewrite to the template text, as `skill_loading: read_file` does today. That covers the step template and the overlay's `Also use …` lines, before placeholders are filled. The step's own carried skill keeps its line.
  - **Path a line names (`resolve`):**
    - a repository skill: `<skills_dir>/<name>/SKILL.md`;
    - a bundled skill: the engine's absolute path, from `pack.skill_file`;
    - an unknown name: `<skills_dir>/<name>/SKILL.md`.
  - **Not rewritten (trade-off):** text filled in later or carried in the run brief (the skill's own text, `CLAUDE.md`, task text). The C1 amendment accepted this.
- **The guard and the retry:**
  - `_loop` passes `read_only=sr.read_only` to `check_step`, so a violation goes through `_fail`.
  - The `read_only_edit` stop's text names the paths and says: "Revert these before you approve or retry: a later step commits the whole work tree."
  - `run_agent_step`'s `"failed"` restart branch also refuses the restart with the same violation while any path named by the step's last `read_only_edit` violation still differs from the `start_tree` of that failed session's `step_started` (`changed_paths`).
- **The denied report:** for a step whose session was read-only, `_denied_report` says "This step runs read-only (`agents.claude.read_only_roles`): these denials are by design." It doesn't suggest a rule.

- [ ] **Step 1: Write the failing tests**
  - `test_guards.py::test_read_only_check_flags_any_tree_change`:
    - a new file and an edited file each give one `read_only_edit`;
    - a forbidden path gives only its `forbidden_path`;
    - with `read_only=False`, the result is as today.
  - `test_feature_lane.py::test_task_review_runs_read_only`. In a feature run on the fake, the `build/task-1/review` act `attempts` an `Edit toy/x.py` and a `Bash "pytest -q"`. Then:
    - one `permission_denied` event with `read_only: true` names the pytest call, and no `toy/x.py` change happens;
    - the review argv has `dontAsk` and `--disable-slash-commands`, and the implement argv has neither;
    - the task is committed.
  - `test_feature_lane.py::test_a_forced_review_edit_fails_the_step`:
    - the review act's `write: {"toy/x.py": "…"}` (which bypasses the fake's permissions) stops the run with `read_only_edit` on `toy/x.py`, the revert text, and no `step_committed` for the task;
    - `retry` is refused while `toy/x.py` is still changed;
    - after `git checkout -- toy/x.py` (or deleting a new file), `retry` runs the review again.
  - `test_agent_step.py::test_a_resumed_review_keeps_read_only`:
    - a review whose first act writes no result gets a reminder call, still with `dontAsk` and the allow list;
    - with `read_only_roles: []` written to the config between the two calls, it still has them;
    - a build session started before the change keeps `bypassPermissions` after `read_only_roles` gains `build`, a config `validate` would refuse that the test loads anyway.
  - `test_agent_step.py::test_an_unsafe_workspace_path_runs_as_today`: with the state dir under a folder named `ws[1]`, the review argv is today's, and one `read_only_skipped` event is written.
  - `test_agent_step.py::test_read_only_idle_limits`:
    - a review call gets `idle_s == 300` and `tool_idle_s == max(900, gate_timeout*60)`;
    - a build call keeps `(900, max(900, gate_timeout*60))`;
    - `read_only_idle_min: 0` gives the review the build pair;
    - `no_progress_min: 0` gives `(None, None)` for both.
  - `test_agent_step.py::test_self_contained_prompts_point_at_skill_files`:
    - a review step with an overlay line `Also use the \`security-and-hardening\` skill.` gets `Also read \`.claude/skills/security-and-hardening/SKILL.md\` and follow it`;
    - a reconcile step and the record step get `--disable-slash-commands`;
    - a build step's prompt and argv are unchanged.
  - `test_agent_step.py::test_a_blocked_read_only_step_reports_denials_as_by_design`.
  - `test_retro.py::test_retro_session_is_read_only_and_writes_its_result`.
  - `test_signals.py::test_read_only_denials_are_not_counted`.
- [ ] **Step 2: Run them and see them fail**

  Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_guards.py agentflow/tests/test_feature_lane.py agentflow/tests/test_agent_step.py agentflow/tests/test_retro.py agentflow/tests/test_signals.py --import-mode=prepend -q -k "read_only or forced_review or self_contained or unsafe_workspace or by_design"`
  Expected: FAIL.
- [ ] **Step 3: Implement the decision and its record, the guard and the retry check, the prompt rewrite, the idle limits, the report and the signal filter**
- [ ] **Step 4: Run the tests again**

  Run: `test_guards.py`, `test_feature_lane.py`, `test_agent_step.py`, `test_retro.py`, `test_signals.py`, `test_doubt_flow.py`, `test_headless.py`, `test_bug_eval_security.py`, `test_workflow.py` and `test_state.py`.
  Expected: PASS. If an existing test pinned a review call's argv, update it to the read-only form; never weaken an assertion about a build call.
- [ ] **Step 5: Lint and commit**

  ```bash
  uvx ruff@0.17.4 check .
  git add agentflow/lanes.py agentflow/state.py agentflow/guards.py agentflow/signals.py CHANGELOG.md agentflow/tests/
  git commit -m "Run read-only roles with no edits and no skills listing, pinned per session, and fail a step whose reviewer changed the tree until it is reverted."
  ```

---

### Task 4: The read-only note and the gate results table

**Files:**
- Modify: `agentflow/gates.py`: `results_table(events: list[dict]) -> str` and `GATE_TABLE_MAX_ROWS = 20`.
- Modify: `agentflow/lanes.py`:
  - `READ_ONLY_NOTE`;
  - `run_agent_step` sets `sr.extra["read_only_note"]` when `sr.read_only`;
  - `build_prompt` defaults `values["read_only_note"] = ""`, and appends `\n\n{read_only_note}` to the body when the value is non-empty and the template has no slot (the `gate_evidence` pattern).
- Modify: `agentflow/config.py`: `"read_only_note"` joins `PROMPT_PLACEHOLDERS`.
- Modify: `docs/agentflow-playbook.md` (the placeholder list), `CHANGELOG.md` (`Added`)
- Test: `agentflow/tests/test_gates.py`, `agentflow/tests/test_feature_lane.py`, `agentflow/tests/test_doubt_flow.py`

**Interfaces:**
- Consumes: Task 3's `sr.read_only`.
- Produces: `gates.results_table(events) -> str`, which is `""` with no `gate_result` events.

**The note.** The value is `READ_ONLY_NOTE`, followed by a blank line and the table when the table isn't empty. `READ_ONLY_NOTE` is exact copy:

> ## This session is read-only
>
> You can read files, search, run `git diff`, `log`, `show`, `status` and `blame`, and write your result file. Editing the code, running tests, linters or builds, and other commands are denied here by design. A denial is expected: do not stop as `blocked` for it and do not rephrase the command; carry on with what you can read, and write your result. Do not write `{progress}`: a read-only step leaves it to the steps that change the code. Judge whether a test is real by reading it and the code it covers.

**The table.**
- **Rows:** take the newest `gate_result` per `gate` (by event order). Rows are sorted newest first, and at most `GATE_TABLE_MAX_ROWS` are shown. After them comes `(<k> older gates not shown)` when more exist.
- **Shape:**
  1. the line `The runner already ran these gates on this run's work; each row is a gate's newest result.`;
  2. a blank line;
  3. `| Gate | Result | Time | Step | Log |`, then `|---|---|---|---|---|`;
  4. one row per gate.
- **Cells:**
  - **Result:** `pass`, `FAIL` or `skipped: <reason>`, with ` (affected files)` added for an `affected`-tier result.
  - **Time:** `<round(duration_s)>s`, or `—` with no duration.
  - **Step:** the event's `key`.
  - **Log:** the `log` path, or `—`.
  - **Escaping:** `|` in any cell becomes `\|`, and newlines become spaces.

- [ ] **Step 1: Write the failing tests**
  - `test_gates.py::test_results_table_keeps_the_newest_result_per_gate`:
    - `lint` fails, then passes: one `pass` row;
    - a skipped gate shows `skipped: no tests`;
    - an affected-tier row has `(affected files)`;
    - a gate named `a|b` comes out as `a\|b`.
  - `test_gates.py::test_results_table_caps_its_rows`: 25 gates give 20 rows and `(5 older gates not shown)`.
  - `test_gates.py::test_results_table_is_empty_without_results`.
  - `test_feature_lane.py::test_task_review_prompt_carries_the_note_and_gate_table`:
    - the review prompt (`argv[-1]`) holds `## This session is read-only`, the progress path filled in, and the task's gate names;
    - the implement prompt holds none of them.
  - `test_doubt_flow.py::test_spec_review_has_the_note_without_a_table`: a define-phase review holds the note, no table line, and no stray `{read_only_note}`. *(Review Focus 5.)*
- [ ] **Step 2: Run them and see them fail**

  Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_gates.py agentflow/tests/test_feature_lane.py agentflow/tests/test_doubt_flow.py --import-mode=prepend -q -k "results_table or gate_table or without_a_table"`
  Expected: FAIL.
- [ ] **Step 3: Implement `results_table`, the note, the placeholder and the append**
- [ ] **Step 4: Run the tests again**

  Run: `test_gates.py`, `test_feature_lane.py`, `test_doubt_flow.py`, `test_config.py` (the placeholder check in `validate`) and `test_pack_fix.py`.
  Expected: PASS.
- [ ] **Step 5: Lint and commit**

  ```bash
  uvx ruff@0.17.4 check .
  git add agentflow/gates.py agentflow/config.py agentflow/lanes.py docs/agentflow-playbook.md CHANGELOG.md agentflow/tests/
  git commit -m "Tell read-only review sessions what they may do, and give them a table of the gates the runner already ran."
  ```

---

### Task 5: Delta-scoped doubt rounds (L3c)

**Files:**
- Modify: `agentflow/gitops.py`: `artifact_diff(worktree: Path, base: str, path: str, max_lines: int) -> str | None`.
- Modify: `agentflow/doubt.py`:
  - `DELTA_MAX_LINES = 400`;
  - `DELTA_LEAD`;
  - `delta_for_review(...)`;
  - `delta_turn_cap(...)`.
- Modify: `agentflow/flow_control.py`: the doubt-reviewer branch of `_run_agent`.
- Modify: `agentflow/lanes.py`: `run_agent_step(..., turn_cap: int | None = None)`, stored on `_StepRun.max_turns` and passed as `AgentCall.max_turns` on every call of the step.
- Modify: `docs/agentflow-playbook.md` (§7, the doubt review's rounds), `CHANGELOG.md` (`Changed`)
- Test: `agentflow/tests/test_doubt.py`, `agentflow/tests/test_doubt_flow.py`

**Interfaces:**
- Produces: `gitops.artifact_diff(worktree, base, path, max_lines)`.
  - **None** when:
    - `path` is empty, absolute, has a `..` part, or starts with `:` or `-`;
    - `base:path` doesn't exist (`_read_git(worktree, "cat-file", "-e", f"{base}:{path}")` fails), so the artifact was new since the base;
    - the artifact is missing from the worktree;
    - the diff command fails, is empty, or is over `max_lines` lines.
  - **The command:** `_read_git(worktree, "--literal-pathspecs", "diff", "--no-ext-diff", "--no-textconv", "--no-color", "-U3", base, "--", path)`. `base` is a commit or a tree id, compared with the working tree. The output is UTF-8 decoded with `errors="replace"`.
- Produces: `doubt.delta_for_review(worktree: Path, events: list[dict], phase: str, review_key: str, artifact: str) -> str`.
  1. Find `base`: take the `step_started` events for `review_key` in `since_human_round(events, phase)` that come before the latest `phase_rejected` with `kind: reconcile`. That leaves out this round's own start after a restart. From the last of them, `base` is its `start_tree`, else its `start_head`.
  2. Return `""` when there is no `base`, or `artifact_diff` is None.
  3. Otherwise return `DELTA_LEAD.format(base=base[:12], artifact=artifact)`, then a blank line, then the diff in a fence. The fence is a run of backticks one longer than the longest backtick run in the diff, and at least 3, followed by `diff`.
- Produces: `doubt.delta_turn_cap(base: int | None) -> int | None`, which is `None` for `None`, else `max(1, base // 2)`.
- **`DELTA_LEAD`** (exact copy): "This is a later round of the review. Since the last review (at `{base}`), `{artifact}` changed only as the diff below shows. Check that each disposition listed below holds in the artifact as it is now, and look for new problems in the changed sections. Read the rest of the artifact only as far as you need to judge those sections; do not review it again in full."

**The wiring:** only the branch `review and rerun and reconciling and doubt.is_doubt_reviewer(...)` in `flow_control._run_agent` changes. It does this:
1. `artifact` is `fmt(str(s.output), self.ctx)` of the phase's first `doubt.reconcile_steps(phase, lane)` step.
2. `delta = doubt.delta_for_review(self.worktree, events, phase.name, key, artifact)`.
3. `earlier_findings` becomes `"\n\n".join(p for p in (delta, doubt.earlier_for_review(events, phase.name)) if p)`.
4. It passes `turn_cap=doubt.delta_turn_cap(self.cfg.agents[phase.agent or self.cfg.defaults.agent].max_turns) if delta else None`.

Round 1, `_carried_over`, the stop rules, the numbering and every other review are untouched.

- [ ] **Step 1: Write the failing tests**
  - `test_doubt.py::test_delta_for_review_shows_the_artifact_diff`. In a temp repo, commit `spec.md`, record a `step_started` for `define/spec_review` with that `start_tree`, then a reconcile `phase_rejected`, then change one section. The result:
    - starts with "This is a later round";
    - holds the base's first 12 characters and the changed line with its `+`;
    - holds no line more than 3 lines away from the change.
  - `test_doubt.py::test_delta_fence_outlasts_backticks_in_the_diff`: a changed line with a run of 5 backticks gets a 6-backtick fence.
  - `test_doubt.py::test_delta_for_review_falls_back_to_the_full_review`. Each of these gives `""`. *(Review Focus 4.)*
    - no `step_started`;
    - only a `step_started` after the latest reconcile `phase_rejected` (a restarted round-2 review);
    - a base of 40 zeros;
    - the artifact deleted;
    - an artifact created after the base;
    - `artifact` set to `/etc/passwd`, to `../x` and to `:(glob)*`;
    - a 401-line diff;
    - no change.
  - `test_doubt.py::test_delta_turn_cap`: `None → None`, `1 → 1`, `60 → 30`, `61 → 30`.
  - `test_doubt_flow.py::test_round_two_review_gets_the_delta`. A define run on the `python` fixture with the engine's default `define` phase (`spec_reconcile`, `spec_review`): the first review raises one `high` finding, the reconcile edits the spec, and the second review is clean. Then:
    - the second review's prompt holds `DELTA_LEAD`'s first sentence, the diff and the `R1-F1` disposition line;
    - the first review's prompt holds none of them.
  - `test_doubt_flow.py::test_round_two_cap_is_half`:
    - with `agents.claude.max_turns: 40`, the second review's argv has `--max-turns 20` and the first has `--max-turns 40`;
    - with `max_turns` unset, neither has `--max-turns`.
  - Every existing test in `test_doubt.py` and `test_doubt_flow.py` (stop rules, numbering, carried-over reviews) passes without edits.
- [ ] **Step 2: Run them and see them fail**

  Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_doubt.py agentflow/tests/test_doubt_flow.py --import-mode=prepend -q -k "delta or round_two"`
  Expected: FAIL.
- [ ] **Step 3: Implement `artifact_diff`, `delta_for_review`, `delta_turn_cap` and the wiring**
- [ ] **Step 4: Run the tests again**

  Run: `test_doubt.py`, `test_doubt_flow.py`, `test_agent_step.py` and `test_gitops.py` (if it exists) in full.
  Expected: PASS, with no edits to the existing doubt tests.
- [ ] **Step 5: Lint and commit**

  ```bash
  uvx ruff@0.17.4 check .
  git add agentflow/gitops.py agentflow/doubt.py agentflow/flow_control.py agentflow/lanes.py docs/agentflow-playbook.md CHANGELOG.md agentflow/tests/
  git commit -m "Scope doubt review rounds after the first to the artifact's diff and the dispositions, at half the turn cap."
  ```

---

## Finishing the slice

- [ ] **Full suite, lint and validate.** Run the full suite, `uvx ruff@0.17.4 check .` and `bin/agentflow validate`. Expect green, `All checks passed!` and `0 warnings`. The golden `explain` files must be unchanged: `git diff --stat agentflow/tests/fixtures/explain` is empty.
- [ ] **Final review.** Review the branch with `security-and-hardening` and the `security-auditor` agent. Focus on:
  - the allow list, `Edit(/<workspace>/**)` and `RULE_UNSAFE`;
  - the `Bash(git *--output*)` deny;
  - the `READ_ONLY_FLAGS` checks;
  - `--strict-mcp-config` with `base_args`;
  - the `read_only_edit` guard and the retry check;
  - `artifact_diff`'s flags and path checks.
- [ ] **Real-run check (spec §9).** Run one feature run on agentflow itself. The PR description gives:
  - **The Task 2 probe results,** (a)–(k).
  - **The scorecard against the C0 baseline:** `session_turns_p90:*` and `tool_output_kchars:*` for the review step kinds, and `gate_first_pass:*` and the reviews' blocking-finding rate no worse than C2's (spec §8).
  - **Reviewer calls:** `Write` calls, against §1.3's 80. Also `pytest` calls, against its 35, and whether they were allowed. This repository's `.claude/settings.json` allows its `pytest` command (Decided 2).
  - **Doubt rounds:** this repository's pack has no doubt steps, so L3c's real-run evidence comes from the first run of a pack with `spec_review`/`plan_review` after release. The PR says so; Task 5's flow test is the evidence for this PR.
