# Throughput slice T2 (Gate economics) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cut gate wall time without weakening any evidence. The changes:
- independent gates run at the same time;
- a gate's pass is reused when nothing it reads has changed;
- the steps that fix things run only the tests their change touches, and the full verify set runs once, on the tree that ships;
- the draft PR opens as soon as `verify` is green, so CI runs while review runs.

**Architecture:**
- **Groups.** `gates.run_gates` partitions a set's gates by `group` and runs each group in its own thread, in set order within the group. A `resource` is a lock held while a gate runs. Results keep set order.
- **Inputs.** A gate with `inputs` gets an `input_hash`: the command digest plus the `(path, mode, blob)` of every file in the gated tree that matches the globs. It is computed from `git ls-tree` on the `guards.snapshot` tree. `reusable_passes` accepts a pass with the same input hash on another tree. `conformance.evaluate` accepts it as evidence on the final tree.
- **Tiers.** A step's `gate_tier: affected` runs each gate's `affected` command, filled with the changed paths, in place of its `run`. Such a result carries `tier: "affected"`, and the floor, reuse and `full_suite_runs` all ignore it.
  - A run that deferred a full run has a final full check in `finish`, before the audit. On a failure, the `sync` session fixes the tree and the runner commits.
  - The audit is unchanged in what it requires.
- **Early PR.** `ci.early_pr_after: <phase>` makes the runner push and open the draft PR when that phase is approved. `finish` then audits, pushes and updates the body instead of creating a PR.
- Everything is off until a pack declares `group`, `inputs`, `affected` or `early_pr_after`.

**Tech Stack:** Python 3.12 stdlib (`concurrent.futures`, `threading`, `shlex`, `hashlib`), git plumbing, pytest. No new dependency.

**Spec:** `docs/specs/agentflow-throughput-proposal.md`. This plan covers slice **T2** (§8):
- §4.5.1: parallel groups and resource locks, within a run;
- §4.5.2: gate results keyed on their inputs;
- §4.5.3: tiers, with the full suite once;
- §4.5.4: the early draft PR;
- §6: the `gate_cache_hit` event and the `gate_cache_hit_rate` KPI;
- verification "T2".

Out of scope:
- resource locks *across* runs (§4.6, T6);
- the CI watcher (§4.5.5, shipped in T1);
- CI as evidence (decision 3: never).

## Global Constraints

- Engine dependencies stay stdlib + `pyyaml>=6`.
- No project strings in the engine (`test_portability.py` `DENYLIST`). Don't name the repository §1 measured anywhere: code, tests, docs or commit messages. Call it "the measured repository". Example gate names in docs and tests are generic (`api_tests`, `web_types`).
- The golden files `agentflow/tests/fixtures/explain/*.txt` stay byte-identical. `explain` prints no gate key, no `gate_tier` and no `ci` key.
- Every new config key ships in its own task with all four companions:
  - a Settings form field (`config_edit._SECTIONS`), or for a key under `gates` or `phases` (already in `_PROTECTED_KEYS`/`PROTECTED`), a `test_config_edit.py` case proving the page refuses it;
  - its `validate` checks;
  - a `tests/test_config_edit.py` case;
  - a line in playbook §14.
- **Every lever is opt-in** (spec §5). With no gate declaring `group`, `resource`, `inputs` or `affected`, and `ci.early_pr_after: ""`, a run's commands, events and pushes are exactly today's. The engine defaults' `gate_tier: affected` lines (Task 3) are inert, because a gate without `affected` runs its `run` at either tier.
- **The floor never weakens.** `gates:verify` is satisfied only by **full-tier** results (`tier` absent or `"full"`) whose `tree` equals the final tree, or whose `input_hash` equals that gate's input hash on the final tree. An affected-tier result, passed or skipped, is never floor evidence and is never reused.
- **No task edits `.agentflow/**`.** Turning the levers on in this pack is one human step in Task 6.
- Engine prompts only gain text. If a task drops or rewords text in `agentflow/prompts/*.md`, it must also add a `Retired` entry to `agentflow/pack_fix.py`.
- Task 3 puts repository file names into a shell command. Review it with the `security-and-hardening` skill: every path is `shlex.quote`d, and a path starting with `-` gets a `./` prefix.
- New events, each with a `state.REQUIRED` entry:

  | Event | Fields | `state.REQUIRED` |
  |---|---|---|
  | `gate_cache_hit` | `{key, gate, input_hash, from}` | `("key", "gate", "input_hash")` |
  | `early_pr_failed` | `{phase, reason}` | `("phase",)` |
  | `pr_updated` | `{url, key, sha}` | `("url", "key")` |

  New fields on existing events:
  - `gate_result.input_hash`, only for a gate with `inputs`;
  - `gate_result.tier`, written only as `"affected"`;
  - `pr_opened.early: true`, on the early PR only.
- Model and tool text shown to a human goes through `hitl.clean_fact`.
- Each user-visible change adds a line under `## [Unreleased]` in `CHANGELOG.md`.
- Tests run as `uv run --no-project --with pyyaml --with pytest pytest <path> --import-mode=prepend -q`. Never create or edit `uv.lock`.

## Review Focus

1. **Repository file names in a shell command.** A changed file named `$(touch pwned).py`, `a b.py` or `-rf.py` reaches an `affected` command as one inert argument each: quoted, and `./-rf.py` for a leading dash (Task 3, `test_changed_paths_are_quoted_and_dash_safe`).
2. **A skipped or passed affected result on the final tree.** It must never satisfy `gates:verify`. A fix step whose affected run skipped every gate ("no affected paths") still leaves the audit to run the full set (Task 3, `test_an_affected_result_is_never_floor_evidence`).
3. **The runner interrupted while two groups run.** Ctrl-C, a crash in a thread, or the runner killed: every gate's process group is killed, not only the main thread's (Task 1, `test_leaving_early_kills_every_groups_process`).
4. **A merge that changes thousands of paths.** `{paths}` must never exceed the argument limit. Above `MAX_AFFECTED_PATHS` (200) matched paths, the gate runs its full `run` instead, and that result is ordinary full-tier evidence (Task 3, `test_too_many_paths_run_the_full_command`).
5. **GitHub down when the early PR opens.** A failed push or `gh pr create` never stops the run. It records `early_pr_failed`, and `finish` opens the PR the normal way later (Task 5, `test_a_failed_early_pr_never_stops_the_run`).

---

### Task 0: The baseline exists

- [ ] **Step 1: Check the baseline.** Run `bin/agentflow scorecard`. Its header names a frozen baseline dated before T1's first commit. If there is none, stop and ask the human to run `agentflow scorecard --freeze`. T2 must not freeze one itself (spec §6).

---

### Task 1: Parallel gate groups and resource locks

**Files:**
- Modify:
  - `agentflow/config.py`: `Gate.group`, `Gate.resource`, `_build_gates`, `_Validator.gate_definitions`;
  - `agentflow/gates.py`: `_shell`, `run_gates`, the module docstring;
  - `docs/agentflow-playbook.md` §14;
  - `CHANGELOG.md`.
- Test: `agentflow/tests/test_gates.py`, `agentflow/tests/test_config.py`, `agentflow/tests/test_config_edit.py`

**Interfaces:**
- Produces:
  - `Gate.group: str | None = None` and `Gate.resource: str | None = None`;
  - `run_gates(cfg, names, cwd, log_dir, *, env_base=None) -> list[GateResult]`, with the same signature and results in set order;
  - `gates._collect(futures: list[Future]) -> None`: waits for every future and re-raises the first exception. It exists as a seam for the interrupt test.
- Rules:
  - The gates with no `group` form one group of their own. A set with one group runs exactly as today: no thread, in order.
  - Each group runs in its own thread (`ThreadPoolExecutor(max_workers=<number of groups>)`), in set order within the group, every gate even after a failure.
  - A gate with a `resource` holds `locks[resource]` (one `threading.Lock` per name, per `run_gates` call) while it runs.
  - `_shell` gains `live: _Live | None = None`, a small class holding a set of `Popen` objects and a lock. It adds the process after `Popen` and removes it in its `finally`. `run_gates` wraps the wait in `try/finally`, and on an exception kills every process still in `live` with `_kill_group`.
  - `validate`:
    - `group` and `resource` must match `^[A-Za-z0-9_.-]{1,40}$`;
    - a gate of `gate_sets.setup` with a `group` is a warning: "setup gates start things in order; drop `group`".

- [ ] **Step 1: Write the failing tests** in `test_gates.py`, using `cfg_with` and gate scripts that leave marker files in `tmp_git`:
  - `test_two_groups_run_at_the_same_time`: gate `a` (group `x`) touches `a.on`, then waits up to 5 s for `b.on`; gate `b` (group `y`) does the reverse. Both pass.
  - `test_ungrouped_gates_still_run_one_after_another`: the same two scripts with no `group`, and a 1 s wait. `a` fails because `b` hasn't started; `b` passes.
  - `test_a_group_runs_in_set_order_and_results_keep_set_order`: set `[c, a, b]`, where `a` and `b` are in group `x` and `c` is ungrouped. Each gate appends its name to `order.txt` after `sleep 0.2`. `a` comes before `b` in `order.txt`, and `[r.name for r in results] == ["c", "a", "b"]`.
  - `test_a_shared_resource_never_overlaps`: `a` (group `x`) and `b` (group `y`) share `resource: db`. Each runs `mkdir db.lock || exit 1; sleep 0.3; rmdir db.lock`. Both pass.
  - `test_leaving_early_kills_every_groups_process`: two groups, each `echo $$ > <name>.pid; exec sleep 30`. Monkeypatch `gates._collect` to wait until both pid files exist, then raise `KeyboardInterrupt`. `pytest.raises(KeyboardInterrupt)`. Within `KILL_WAIT_S`, `os.kill(pid, 0)` raises `ProcessLookupError` for both pids.

  In `test_config.py`:
  - `test_gate_group_and_resource_are_names`: `group: "a b"` is an error at `gates.<n>.group`, and `resource: ""` is an error at `gates.<n>.resource`.
  - `test_a_grouped_setup_gate_is_a_warning`.

  In `test_config_edit.py`:
  - `test_gate_groups_are_protected`: proposing YAML text that adds `group: api` to a shipped gate raises `Refused` matching `"protected"`.
- [ ] **Step 2: Run them and see them fail.** `test_two_groups_run_at_the_same_time` fails on `b`, and the config tests fail on the unknown key.
- [ ] **Step 3: Implement** the dataclass fields, loading (`_optional(raw, "group", path, str, None)`), `validate`, `_Live`, `_collect` and the grouped `run_gates`.
- [ ] **Step 4: Run** `test_gates.py`, `test_config.py` and `test_config_edit.py`. All pass.
- [ ] **Step 5: Docs.**
  - Playbook §14, after `gates.<name>.agent_check`: one line for `gates.<name>.group` and one for `gates.<name>.resource`. Say that gates with no group run in order as before, and that a resource lock holds within one run; locks across runs come later.
  - Add a `CHANGELOG.md` `Added` line.
- [ ] **Step 6: Commit** "Run gates of different groups at the same time, and never two gates that share a resource."

---

### Task 2: Gate results keyed on their inputs

**Files:**
- Modify:
  - `agentflow/config.py`: `Gate.inputs`, loading, `validate`;
  - `agentflow/gates.py`: `GateResult.input_hash`, `hash_inputs`, `input_hashes`, `reusable_passes`, `run_gates_reusing`, `record_results`;
  - `agentflow/conformance.py`: `evaluate`, `_gates`, `audit`;
  - `agentflow/signals.py`: the `gate_cache_hit_rate` family;
  - `agentflow/state.py`: `REQUIRED`;
  - `docs/agentflow-playbook.md` §14: the new line, and the `gates:<set>` row of the evidence table;
  - `CHANGELOG.md`.
- Test: `agentflow/tests/test_gates.py`, `test_conformance.py`, `test_signals.py`, `test_config.py`, `test_config_edit.py`

**Interfaces:**
- Produces:
  - `Gate.inputs: list[str]` (default `[]`): globs with `guards.path_matches` semantics.
  - `GateResult.input_hash: str | None = None`.
  - `gates.hash_inputs(entries: list[tuple[str, str, str]], patterns: list[str], cmd: str) -> str`. It is pure. `entries` are `(path, mode, blob)`. The result is the sha256 hex, cut to 16 characters, of `cmd`, then one `"<mode> <blob> <path>"` line for each entry matching any pattern, sorted by path.
  - `gates.input_hashes(cfg: Config, gates: list[Gate], cwd: Path, tree: str) -> dict[str, str]`: one `git ls-tree -r -z --full-tree <tree>` (with `guards.HARDENED`), then `hash_inputs` for each gate that has `inputs`, with `cmd = command_digest(gate, cfg.defaults.env)`. Gates without inputs are absent.
  - `reusable_passes(events, gates, tree, digests, never=frozenset(), hashes: Mapping[str, str] | None = None)`. A pass also counts when its `input_hash` equals `hashes[name]`. The full-tier rule of Task 3 is added there.
  - `run_gates_reusing` computes `hashes` once, stamps `input_hash` on each fresh result (`dataclasses.replace`), and emits one `gate_cache_hit{key, gate, input_hash, from}` for each pass reused from a *different* tree. `gates_reused` stays as it is and still lists every reused gate.
  - `record_results` writes `input_hash` when it is not None.
  - `conformance.evaluate(cfg, events, obligations, final_tree, not_applicable=(), *, input_hashes: Mapping[str, str] | None = None)`. `_gates` takes the latest result whose `tree == final_tree`, or whose `input_hash` is not None and equals `input_hashes[name]`. `audit` passes `gates.input_hashes(cfg, list(cfg.gates.values()), worktree, tree)`, both times it calls `evaluate`.
  - Signal `gate_cache_hit_rate` (`"higher"`, no parameter) = reused ÷ (reused + not-skipped `gate_result`s). Reused is the gates named by `gates_reused` events. It is present only when the denominator is above 0, and it isn't in `COUNT_FAMILIES`.
  - `validate`:
    - `inputs` is a non-empty list of non-empty strings, each relative, with no `..` part;
    - once any gate declares `inputs`, a `gate_sets.verify` gate without them is a warning: "`<gate>` has no `inputs`, so it runs again after every change". Spec §4.5.2's warning, limited to packs that opted in, so packs without `inputs` stay at 0 warnings.

- [ ] **Step 1: Write the failing tests.**

  `test_gates.py`:
  - `test_hash_inputs_ignores_paths_outside_the_globs`: entries for `api/a.py` and `web/b.ts`, patterns `["api/**"]`. Changing `web/b.ts`'s blob leaves the hash equal; changing `api/a.py`'s blob, or removing that entry, changes it.
  - `test_hash_inputs_follows_the_command`: the same entries with a different `cmd` give a different hash.
  - `test_input_hashes_count_untracked_files`: in `tmp_git`, an untracked `api/new.py` changes the hash of a gate with `inputs: ["api/**"]` between two `snapshot`s.
  - `test_a_change_outside_a_gates_inputs_reuses_its_pass`: a `Run` on the python fixture. Gate `api` has `inputs: ["api/**"]`. Run `run_gates_reusing` on tree A, write `web/x.ts`, then run it again on tree B. The second result has `reused_from` set, there is exactly one `gate_result` for `api`, and one `gate_cache_hit` with `gate == "api"` and `from` equal to the first key.
  - `test_a_gate_without_inputs_is_not_reused_on_another_tree`.

  `test_conformance.py`:
  - `test_a_pass_with_the_final_trees_input_hash_is_evidence`: a `gate_result` on tree `t1` with `input_hash: "h"`; `evaluate(..., "t2", input_hashes={"<verify gate>": "h"})`. The floor is satisfied, and its evidence is that seq.
  - `test_a_pass_whose_input_hash_differs_is_not_evidence`: the same with `{"<gate>": "other"}` gives `missing`, with the note "no result on the final tree".
  - `test_audit_reuses_a_pass_by_input_hash`: a `flow_repo` finish where the last code change touched only a path outside the verify gate's `inputs`. `conformance_checked.passed` is true, and no `gate_result` carries the finish key.

  `test_signals.py`:
  - `test_gate_cache_hit_rate`: 3 gate results that ran, and one `gates_reused` naming 1 gate: `0.25`. It is absent with no gate events.

  `test_config.py`:
  - `test_gate_inputs_must_be_relative_globs`;
  - `test_a_verify_gate_without_inputs_warns_only_once_a_pack_uses_inputs`.

  `test_config_edit.py`:
  - `test_gate_inputs_are_protected`.
- [ ] **Step 2: Run them and see them fail** (`AttributeError` on `hash_inputs`, the unknown key `inputs`).
- [ ] **Step 3: Implement** the interfaces above.
- [ ] **Step 4: Run** the five test files, then `test_finished.py` and `test_sync.py`. All pass.
- [ ] **Step 5: Docs.**
  - Playbook §14:
    - a `gates.<name>.inputs` line. Say what the hash covers, and that ignored files (`.env`, `node_modules`) are not in it: a gate that reads one must not declare `inputs`;
    - in the evidence table's `gates:<set>` row: "…on the final tree, or a pass whose input hash equals the gate's input hash on the final tree".
  - Add a `CHANGELOG.md` line.
- [ ] **Step 6: Commit** "Reuse a gate's pass when none of its declared inputs changed, and accept it as evidence on the final tree."

---

### Task 3: Tiers: an `affected` command for the steps that fix things

**Files:**
- Modify:
  - `agentflow/config.py`:
    - an `Affected` dataclass and `Gate.affected`;
    - `"gate_tier"` in `STEP_KEYS`, `Step.gate_tier`, loading;
    - `validate`;
  - `agentflow/gates.py`:
    - `GateResult.tier`;
    - `MAX_AFFECTED_PATHS = 200`;
    - `affected_gate`, `deferred_full`;
    - `run_gates` and `run_gates_reusing` gain `tier` and `changed`;
    - the full-tier filter in `reusable_passes`;
    - `record_results`;
  - `agentflow/lanes.py`: `_failed_gates` passes the step's tier, and the paths changed since `sr.start_head`;
  - `agentflow/sync_lane.py`: `_run_merge_gates` passes `step.sync.gate_tier` and the paths changed since `HEAD`. The `eval` gate is always full;
  - `agentflow/conformance.py`: `_gates` skips results with `tier == "affected"`;
  - `agentflow/signals.py`: `_full_suite_runs` skips them;
  - `agentflow/defaults/config.yaml`: `gate_tier: affected` on `review/fix_findings`, `ship/finish.sync` and `ship/ci.fix`;
  - `docs/agentflow-playbook.md` §14;
  - `CHANGELOG.md`.
- Test: `agentflow/tests/test_gates.py`, `test_conformance.py`, `test_signals.py`, `test_config.py`, `test_config_edit.py`, `test_feature_lane.py`

**Interfaces:**
- Consumes: `Gate.inputs` (Task 2), and `guards.changed_paths(worktree, tree_a, tree_b) -> list[str]`, which takes any tree-ish, a commit included.
- Produces:
  - `@dataclass(frozen=True) class Affected: run: str; paths: list[str]`. It is loaded from `gates.<n>.affected: {run, paths}`. `paths` defaults to the gate's `inputs`, else `["**"]`.
  - `Step.gate_tier: str = "full"`, one of `"full"` or `"affected"`.
  - `GateResult.tier: str = "full"`.
  - `gates.affected_gate(gate: Gate, changed: list[str], cwd: Path) -> Gate | None`.
    - Takes the paths in `changed` that match `gate.affected.paths` and exist under `cwd`.
    - When `gate.cwd` is set, makes each path relative to it and drops the ones outside it.
    - Returns None when no path is left.
    - Otherwise returns `dataclasses.replace(gate, run=<rendered>)`. The rendering replaces `{paths}` with the paths, each `shlex.quote`d and prefixed `./` when it starts with `-`, joined by spaces. `{{` and `}}` are literal braces.
  - `run_gates(..., tier: str = "full", changed: Sequence[str] = ())` and `run_gates_reusing(..., tier="full", changed=())`. At `tier == "affected"`, for each gate with `affected`:
    - more than `MAX_AFFECTED_PATHS` matched paths: run `gate.run`, tier full;
    - `affected_gate` is None: a skipped result with reason `"no affected paths"` and tier `"affected"`;
    - otherwise: run the rendered gate, tier `"affected"`.

    A gate with no `affected` runs `gate.run` at tier full. Reuse is tried first, at either tier.
  - `record_results` writes `tier: "affected"` only for affected results.
  - `reusable_passes` ignores events with `tier == "affected"`.
  - `gates.deferred_full(events: list[dict]) -> bool`: some `gate_result` has `tier == "affected"`.
  - `validate`:
    - `affected.run` is a non-empty string whose only placeholder is `{paths}`;
    - `affected.paths` is a list of relative globs with no `..` part;
    - `gate_tier` is `full` or `affected`;
    - `gate_tier: affected` on a step with no `gates` is an error.

**Decided here:** `conformance._verify_covered` stays tier-blind. A fix commit whose step passed every verify gate at the affected tier doesn't make earlier `step:` evidence stale. That is safe because the floor still needs full-tier results on the final tree, which Task 4 and the audit provide (see Notes).

- [ ] **Step 1: Write the failing tests.**

  `test_gates.py` (gate `t`, `affected: {run: "printf '%s\\n' {paths} > out.txt", paths: ["**/*.py"]}`):
  - `test_affected_runs_the_template_with_the_changed_python_paths`: `changed = ["a.py", "src/x.py", "docs/y.md"]` (all files exist). `out.txt` reads `a.py\nsrc/x.py\n`, and `results[0].tier == "affected"`.
  - `test_changed_paths_are_quoted_and_dash_safe`: the files `$(touch pwned).py`, `a b.py` and `-rf.py`. `out.txt` holds exactly those three names, with `./-rf.py` for the last, and no `pwned` file exists.
  - `test_a_deleted_path_is_not_passed`.
  - `test_no_matching_path_skips_at_the_affected_tier`: `skipped`, reason `"no affected paths"`, tier `"affected"`.
  - `test_a_gate_without_affected_runs_its_full_command_at_the_affected_tier`: tier `"full"`.
  - `test_too_many_paths_run_the_full_command`: 201 matching files make `gate.run` run, at tier `"full"`.
  - `test_paths_are_relative_to_the_gates_cwd`: `cwd: api`, changed `api/a.py` and `web/b.py`. `out.txt` reads `a.py`.
  - `test_an_affected_pass_is_never_reused`: an affected pass on tree T, then a full-tier `run_gates_reusing` on T runs the gate.

  `test_conformance.py`:
  - `test_an_affected_result_is_never_floor_evidence`: a passed result and a skipped result, each with `tier: "affected"`, on the final tree give `missing`.

  `test_signals.py`:
  - `test_an_affected_batch_is_not_a_full_suite_run`.

  `test_config.py`:
  - `test_affected_allows_only_the_paths_placeholder`;
  - `test_gate_tier_needs_gates`;
  - `test_the_engine_defaults_mark_the_fix_steps_affected`: the loaded defaults' `fix_findings`, `finish.sync` and `ci.fix` have `gate_tier == "affected"`, and `verify` has `"full"`.

  `test_config_edit.py`:
  - `test_gate_affected_and_gate_tier_are_protected`.

  `test_feature_lane.py`:
  - `test_a_fix_step_runs_the_affected_tier`. Use the existing refine-round fixture pattern of `fix_findings`, with the verify gate given `affected: {run: "true {paths}", paths: ["**/*.py"]}` and the step given `gate_tier: affected`. The fake agent changes `src/x.py`. The step's `gate_result` has `tier == "affected"`.
- [ ] **Step 2: Run them and see them fail.**
- [ ] **Step 3: Implement** the interfaces above, and the three `gate_tier: affected` lines in `defaults/config.yaml`.
- [ ] **Step 4: Run** the touched test files, `test_explain_golden.py` (byte-identical), `test_sync.py` and `test_ci.py`. All pass.
- [ ] **Step 5: Docs.**
  - Playbook §14:
    - `gates.<name>.affected`, with an example: `{run: "pytest {paths}", paths: ["tests/**/test_*.py"]}`;
    - `phases.<name>.steps[].gate_tier`.

    Name the three tiers: the `build` set is the fast tier, `affected` is for fix steps, and the `verify` set runs in full once, on the final tree.
  - Add a `CHANGELOG.md` line.
- [ ] **Step 6: Commit** "Let the steps that fix findings, merges and CI run only the tests their change touches, never as evidence for the final tree."

---

### Task 4: The full verify set once on the final tree, with a fix loop

**Files:**
- Modify: `agentflow/feature_lane.py` (`run_finish`, a new `final_check`), `CHANGELOG.md`
- Test: `agentflow/tests/test_conformance.py`

**Interfaces:**
- Consumes:
  - `gates.deferred_full`, and `run_gates_reusing(..., tier="full")` (Task 3);
  - `input_hashes` (Task 2);
  - `SyncLane._sync_report`, `SyncLane._plain` and `lanes.run_agent_step(..., fix_first=...)`.
- Produces:
  - `FeatureLane.final_check(phase: Phase, step: Step, key: str) -> Outcome`. `run_finish` calls it after `runner_commits_only`, before reading `sha` for the audit, only when `deferred_full(self.run.events.read())`.
  - What it does:
    1. Run `run_gates_reusing(cfg, run, key, "verify", worktree, run.gate_log_dir(key), snapshot(worktree))` and record it. Everything passes or is skipped: `"done"`.
    2. Failures, and `step.sync` has `fix_loop`: run `run_agent_step(self._plain(phase), replace(step.sync, gate_tier="full", commit=True, message="Fix what the full verify run found"), f"{key}/final_fix", extra={"sync_report": self._sync_report([], "")}, role=step.sync.role, fix_first=failed)`. Its gates (`verify`, full) and its fix loop decide, and the runner commits. Return its outcome.
    3. Failures, and no fix session, or the `final_fix` step is already `done`: the same `gate_failed` stop the audit raises today (`failure_report`), and `"stopped"`.

- [ ] **Step 1: Write the failing tests** in `test_conformance.py`, on `flow_repo`. In the copied pack, the verify gate has `inputs` and `affected`, and `fix_findings` has `gate_tier: affected`:
  - `test_a_tiered_run_runs_the_full_suite_once`: branch review raises one blocking finding, and `fix_findings` fixes it in a file outside the gate's `inputs`. `signals.run_signals(events, metrics, full_suite=[...])["full_suite_runs"] == 1`, and `conformance_checked.passed` is true.
  - `test_a_fix_inside_the_inputs_runs_the_full_set_again_at_finish`: the fix touches a file inside the inputs. Exactly one full-tier `gate_result` for that gate carries the finish key, and the audit runs no gate.
  - `test_a_final_check_failure_goes_to_the_sync_session_and_is_committed`: the gate's full command fails until the fake `final_fix` session writes `fixed.txt`. One `step_committed` with key `ship/finish/final_fix`, then a PR. No `hitl_raised`.
  - `test_without_a_deferred_run_finish_is_unchanged`: no affected result means no `final_fix` key, and the event types at finish are the same sequence as today's `test_finish_audits_and_reports_in_report_mode`.
- [ ] **Step 2: Run them and see them fail.**
- [ ] **Step 3: Implement** `final_check` and the call in `run_finish`.
- [ ] **Step 4: Run** `test_conformance.py`, `test_sync.py`, `test_ci.py` and `test_finished.py`. All pass.
- [ ] **Step 5: Commit** "Run the full verify set once on the tree to be pushed when a fix step deferred it, and let the sync session fix what it finds."

---

### Task 5: The early draft PR

**Files:**
- Modify:
  - `agentflow/defaults/config.yaml`: `ci.early_pr_after: ""`;
  - `agentflow/config.py`: `validate`;
  - `agentflow/config_edit.py`: a CI form field;
  - `agentflow/feature_lane.py`: `open_early_pr`, `run_finish`, `render_pr_body(..., early=False)`;
  - `agentflow/pipeline.py`: `_Loop.hand_off`, and `dry_run`'s per-phase output;
  - `agentflow/state.py`: `REQUIRED`;
  - `docs/agentflow-playbook.md` §14 (`ci`), and the §8 line on what `finish` does;
  - `CHANGELOG.md`.
- Test: `agentflow/tests/test_ci.py`, `test_conformance.py`, `test_config.py`, `test_config_edit.py`

**Interfaces:**
- Consumes: `gitops.push`, `create_draft_pr`, `update_pr_body`; `issue_status.link`; `FeatureLane.runner_commits_only`, `write_pr_body`; `conformance.clean_tree`.
- Produces:
  - `FeatureLane.open_early_pr(phase: str) -> None`. `_Loop.hand_off` calls it last, when `cfg.ci.get("early_pr_after") == name`. It does nothing when:
    - a `pr_opened` exists;
    - the run's lane walks no `type: finish` step;
    - the branch is `base_branch` or `fork_from`.

    Otherwise it checks a clean tree and `runner_commits_only` without raising. Then it pushes `head` with `sha=`, writes the body with `early=True`, calls `create_draft_pr`, and emits `pr_opened{url, key: f"{phase}/early_pr", early: True}`, then `link(run)`. Any refusal, `GitError` or `GhError` emits `early_pr_failed{phase, reason}` (reason through `clean_fact`) and returns. **It never raises a stop.**
  - `render_pr_body(run, plan=None, *, early=False)`. With `early`, the body's first line is: "Early draft: review is still running. The runner audits conformance and updates this description before it marks the pull request ready."
  - `run_finish`:
    - on resume, it returns `"done"` when a `pr_opened` **or** `pr_updated` carries its own key;
    - when a `pr_opened` with `early` exists, after the audit it pushes `sha`, calls `update_pr_body` (a `GhError` is the usual `blocked` stop), emits `pr_updated{url, key, sha}`, then calls `confirm_mergeable` and finishes;
    - without an early PR, it runs as today.
  - `dry_run`: when `ci.early_pr_after` names a phase, the line `  ⇪ early draft PR: git push origin <branch>; gh pr create --draft` comes after that phase's steps.
  - `validate`:
    - `ci.early_pr_after` is `""` or a phase name in `phases`;
    - that phase must come before the phase holding the `finish` step, otherwise it is an error at `ci.early_pr_after`.
  - Form field: `(("ci", "early_pr_after"), "Open the draft PR after phase (empty = at finish)", {"kind": "text", "allow_empty": True})`.

- [ ] **Step 1: Write the failing tests.**

  In `test_ci.py` or `test_conformance.py`, with `flow_repo` + `gh` and `ci.early_pr_after: verify` in the copied pack:
  - `test_the_draft_pr_opens_when_verify_is_approved`: the fake `gh` records `pr create` before the first `branch_review` agent call. `pr_opened.early` is true.
  - `test_finish_updates_the_early_pr_instead_of_opening_one`: exactly one `gh pr create` and one `gh pr edit --body-file`. `conformance_checked` comes before the second push, and `pr_updated` carries the finish key.
  - `test_a_failed_early_pr_never_stops_the_run`: the fake `gh` fails `pr create` once. `early_pr_failed`, then the run reaches finish, which opens the PR. No `hitl_raised` for it.
  - `test_ci_watches_the_early_pr`.
  - `test_dry_run_shows_the_early_pr`. The existing dry-run tests stay unchanged with the key empty.

  `test_config.py`:
  - `test_early_pr_after_must_name_a_phase_before_finish`: `ship` and an unknown phase are errors, and `verify` is fine.

  `test_config_edit.py`:
  - `test_the_form_sets_the_early_pr_phase`: setting `verify` works, and `nope` raises `EditError`.
- [ ] **Step 2: Run them and see them fail.**
- [ ] **Step 3: Implement** the interfaces above.
- [ ] **Step 4: Run** `test_ci.py`, `test_conformance.py`, `test_pipeline.py`, `test_config.py`, `test_config_edit.py` and `test_finished.py`. All pass.
- [ ] **Step 5: Docs.**
  - Playbook §14: the `ci.early_pr_after` line. Say that a push before the audit is a draft only, and that marking it ready still needs the audit and CI.
  - Playbook §8: the same in one sentence.
  - Add a `CHANGELOG.md` line.
- [ ] **Step 6: Commit** "Open the draft pull request as soon as a chosen phase is approved, so CI runs while review does."

---

### Task 6: The whole slice

- [ ] **Step 1: Run the full suite.** `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests --import-mode=prepend -q`. All pass. The golden files are unchanged: `git diff --stat agentflow/tests/fixtures/explain` is empty.
- [ ] **Step 2: Validate.** `bin/agentflow validate` prints "0 warnings".
- [ ] **Step 3: Human step: turn the levers on in this pack.** `.agentflow/**` is forbidden to a run. The human edits `.agentflow/config.yaml`:
  - `engine_tests`:
    - `inputs: ["agentflow/**", "pyproject.toml"]`;
    - `affected: {run: "uv run --no-project --with pyyaml --with pytest pytest {paths} --import-mode=prepend -q", paths: ["agentflow/tests/test_*.py"]}`;
  - `ci.early_pr_after: verify`.

  The pack overrides `phases`, so the human also adds `gate_tier: affected` to its `review/fix_findings`, `ship/finish.sync` and `ship/ci.fix` steps. After that, `bin/agentflow validate` prints "0 warnings".
- [ ] **Step 4: Check against the spec's "T2" verification.**
  - A fix touching only files outside a gate's inputs reuses the gate: `gate_cache_hit` (Task 2).
  - The audit refuses a reused result whose input hash differs (Task 2).
  - `full_suite_runs == 1` on a clean run (Task 4).

  The gate wall-time saving (−50–60 %) is measured on 5 real feature runs with `agentflow scorecard` before T3 starts (spec §6).

---

## Notes: accepted trade-offs

- **Resource locks hold within one run.** Two runs of `agentflow queue --max-parallel 2` can still overlap on a resource. Slice T6 adds slots and locks across runs.
- **The fast tier is pack data.** It is the `build` gate set each task already runs, so there is no engine code for it.
- **`_verify_covered` counts affected-tier batches** (Task 3). Without that, every fix-findings commit would make `step:verify` stale, and block-mode runs would fail `verified`. The full-tier floor on the final tree is what replaces the full run each fix step used to make.
- **The final fix reuses the `sync` session and its prompt**, with an empty conflict list. That is the same call `_verify_merge` already makes when a merged tree fails its gates. A dedicated prompt can come later if its fixes turn out worse.
- **The `inputs` warning fires only once a pack uses `inputs`.** The spec asks `validate` to warn about a gate with no `inputs`. Warning every existing pack would break "0 warnings" on this pack and on the fixtures, and would push packs to declare inputs that are too narrow.
- **Ignored files are not in the input hash** (the snapshot leaves them out). Playbook §14 says a gate that reads one must not declare `inputs`.
- **A crash between `gh pr create` and `pr_opened`** leaves a PR the run doesn't know about. Today's `finish` has the same window. The next `create_draft_pr` fails and stops as `blocked`.
- **CI is never evidence** (spec decision 3). The early PR saves time only by running CI alongside review.
