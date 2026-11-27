# Pack size bound Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use executing-plans (two tasks) to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A config file whose YAML aliases expand to more than 100,000 values is refused with a load error naming it, instead of making the loader's unmemoized passes run for minutes.

**Architecture:** One pure function, `layers.expanded_size`, counts the values a parsed document would hold if every alias were copied out, computed once per shared node. Both places that read a pack, `layers._read` and `config._read_yaml`, call it right after their existing `_no_cycle` check and refuse a document above `layers.MAX_EXPANDED_NODES`. No config key, no new interface beyond that constant and function.

**Tech Stack:** Python 3.12, stdlib + `pyyaml`, pytest.

**Spec:** none of its own: a one-constant loader bound. The decision and its reason are in `docs/plans/2026-11-26-agentflow-harness-workspace-w2.md` (Plan decisions, "A pack's size is bounded by a separate PR that ships first"), settled with the user on 2026-11-26. W2 depends on this PR and is released as 3.15.

**Order and execution (decided with the user, 2026-11-26):** branch from an up-to-date `main` after #60 (W1, 3.13) merges, so this releases 3.14 and the W2 plan releases 3.15; if #60 stalls it may be stacked on `feat/workspace-w1` as 3.14. Two tasks: execute natively with `executing-plans`, then one whole-branch review by the `security-auditor` agent.

## Global Constraints

- Engine dependencies stay stdlib + `pyyaml>=6`. No project strings in the engine (`test_portability.DENYLIST`).
- `layers.py` stays a leaf module: it imports nothing from `config.py` or the engine's other modules. `config.py` may call `layers.expanded_size` (it already imports `layers`).
- Every config the engine ships (`agentflow/defaults/config.yaml`, the starters, `.agentflow/config.yaml`, both fixture packs) expands to at most 845 values today; the budget is `100_000`, about 100 times that.
- `uvx ruff@0.17.4 check .` must print `All checks passed!` before every commit. Tests run with `uv run --no-project --with pyyaml --with pytest pytest <file> --import-mode=prepend -q`.
- The golden `explain` files stay byte-identical.
- This PR bumps the version to 3.14 (both `pyproject.toml` and `agentflow/pack.py`) and adds one `Fixed` line to `CHANGELOG.md`. Commit messages are one imperative sentence ending with the trailer the executing session is given (`project.commit_trailer` inside an agentflow run; interactively, `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`).
- `layers.py` triggers the pack's security overlay: plan and review with `security-and-hardening` and the `security-auditor` agent (CLAUDE.md, Invariants).

## Review Focus

1. **The bomb itself:** nine anchored lists, each holding nine references to the one before (9^9 values once expanded), is refused in well under a second, by both `layers._read` and `config._read_yaml` (Task 1).
2. **Legitimate anchors:** a pack that reuses one anchored `gates` block four times, and every shipped config, still loads (Task 1).
3. **The existing errors come first:** an alias that contains itself, and nesting past `MAX_DEPTH`, keep their current messages, because `expanded_size` is only called on a document `_no_cycle` accepted (and recurses no deeper than `MAX_DEPTH`) (Task 1).
4. **The boundary:** a document of exactly `MAX_EXPANDED_NODES` values loads, one more is refused; mapping keys are not counted (Task 1).
5. **A bound that only one loader enforces:** a pack read through `load_config` and a layer read through `layers.load_user_settings` or `layers.merge`'s callers are both refused (Task 1).

---

### Task 1: Refuse a config that expands past 100,000 values

**Files:**
- Modify: `agentflow/layers.py` (`MAX_EXPANDED_NODES`, `expanded_size`, `_read`)
- Modify: `agentflow/config.py` (`_read_yaml`)
- Test: `agentflow/tests/test_layers.py`, `agentflow/tests/test_config.py`

**Interfaces:**
- Consumes: the existing `layers._no_cycle` and `config._no_cycle` (each walks every shared node once), `LayerError`, `ConfigError`.
- Produces:
  ```python
  MAX_EXPANDED_NODES = 100_000                # layers.py
  def expanded_size(node: object) -> int      # layers.py
  ```
  `expanded_size` is `1` for a scalar and `1 + sum(child sizes)` for a mapping (its values; keys are not counted) or a list, memoized by `id(node)` so a shared node is computed once and counted once per use. `layers._read` raises `LayerError(str(path), "expands to more than 100000 values (nested aliases)")` and `config._read_yaml` raises `ConfigError("", f"{path} expands to more than 100000 values (nested aliases)")`.

- [ ] **Step 1: Write the failing tests**

`test_layers.py`:
- `test_a_pack_of_nested_aliases_is_refused_quickly`: write a file of nine anchored lists, `a0: &a0 [x, x, x, x, x, x, x, x, x]`, then `a<i>: &a<i> [*a<i-1>, … nine times]`, to `tmp_path`; `layers._read(path)` raises `LayerError` whose text names the file and contains `nested aliases`, and the call takes under 2 seconds (`time.monotonic()`).
- `test_expanded_size_counts_each_use_of_a_shared_node`: `expanded_size("x") == 1`; `expanded_size({"a": [1, 2], "b": [1, 2]}) == 7` (the mapping, two lists, four scalars); a document that holds one anchored list three times is `1 + 3 * (1 + n)`, not `1 + (1 + n)`.
- `test_the_budget_boundary`: a list of exactly `MAX_EXPANDED_NODES - 1` scalars (size `MAX_EXPANDED_NODES`) is read; one more scalar is refused; a mapping whose keys are long strings counts only its values.
- `test_an_alias_that_contains_itself_and_deep_nesting_keep_their_messages`: the two documents the existing tests use still raise the existing `LayerError` text (run the existing tests; add none if they already cover both).
- `test_an_ordinary_anchored_pack_still_loads`: a pack that reuses an anchored `gates` block four times is read; `layers.base_layers(None)` and `layers.starter_names()`'s files are read (the shipped engine defaults and starters are all under 100 values per anchor, so this fails only if the budget is wrong).

`test_config.py`:
- `test_load_config_refuses_nested_aliases`: the same nine-level document as a pack's `config.yaml` (use `tmp_cfg`'s file helper) gives `ConfigError` naming `nested aliases`, in under 2 seconds; a user file (`layers.user_config_path()`) holding it is refused by `load_config` and by `layers.load_user_settings` with the file's name.

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_layers.py agentflow/tests/test_config.py --import-mode=prepend -q -k "nested_aliases or expanded_size or budget_boundary or anchored_pack"`
Expected: the nested-alias, `expanded_size` and boundary tests FAIL (`expanded_size` is not defined, the loader does not refuse); the ordinary-pack test passes (it pins behavior the change must keep). If the nested-alias test hangs, the failing run is the evidence: stop it after 60 seconds.

- [ ] **Step 3: Implement**

Implement `expanded_size` in `layers.py`: recurse over a mapping's values and a list's items, memoize in a dict keyed by `id(node)` for the duration of one call (the depth is already capped at `MAX_DEPTH` by `_no_cycle`, which both readers call first, so recursion is safe). Call it after `_no_cycle` in `layers._read` and in `config._read_yaml`; raise when it exceeds `MAX_EXPANDED_NODES`. Then check the other readers of a YAML document and list what you find in the commit message: `pack._raw_state_dir`, `config_edit` (`safe_load` at its lines 281, 544, 596, 679, 715), `detect.py:546` and `backlog_file.py`: each parses and then indexes named keys, or walks the document once without the unmemoized recursion; any reader that walks every alias copy gets the same call (add a test for it) or a one-line note why it can't be reached with a hostile file.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_layers.py agentflow/tests/test_config.py agentflow/tests/test_plug_and_play.py agentflow/tests/test_config_edit.py --import-mode=prepend -q`
Expected: PASS.

- [ ] **Step 5: Lint and commit**

```bash
uvx ruff@0.17.4 check .
git add agentflow/layers.py agentflow/config.py agentflow/tests/test_layers.py agentflow/tests/test_config.py
git commit -m "Refuse a config whose nested aliases expand past 100000 values.

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Release 3.14

**Files:**
- Modify: `CHANGELOG.md`, `pyproject.toml`, `agentflow/pack.py`
- Test: `agentflow/tests/test_packaging.py`, `agentflow/tests/test_portability.py`, the whole suite

- [ ] **Step 1: Release**

`CHANGELOG.md`: under `## [Unreleased]` add `Fixed`: "A config file whose nested YAML aliases expand to more than 100,000 values is refused with a load error instead of hanging the loader (the CLI and the dashboard read the same loader)." Rename `## [Unreleased]` to `## [3.14] - 2026-11-26` and open a fresh empty `## [Unreleased]` above it. Set `version = "3.14"` in `pyproject.toml` and `ENGINE_VERSION = "3.14"` in `agentflow/pack.py`. (The workspace plan W2 then bumps to 3.15.)

- [ ] **Step 2: Run the checks**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_packaging.py agentflow/tests/test_portability.py agentflow/tests/test_explain_golden.py --import-mode=prepend -q`, then `bin/agentflow validate` (it must print `0 warnings`), then `uvx ruff@0.17.4 check .` (`All checks passed!`).
Expected: PASS.

- [ ] **Step 3: Run the full suite once**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests --import-mode=prepend -q` in the background (25 to 60 minutes).
Expected: PASS. A failure is a config in a fixture or test that now exceeds the budget or trips the new check: fix the fixture only if it is really over 100,000 values (none should be), never raise the constant to make a test pass.

- [ ] **Step 4: Commit**

```bash
git add CHANGELOG.md pyproject.toml agentflow/pack.py
git commit -m "Release 3.14.

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```
