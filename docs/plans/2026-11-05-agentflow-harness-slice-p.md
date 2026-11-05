# agentflow harness, slice P: engine/pack separation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make this repository's `agentflow/` a project-agnostic engine that reads everything project-specific from a pack at `<repo>/.agentflow/`, proven by two fixture packs, a denylist test, and this repository's own pack.

**Architecture:** This repository is agentflow, cut out of the project it was built in. The engine is the `agentflow/` package at the repo root, and `bin/agentflow` runs it. A new leaf module `pack.py` finds the pack (`<main checkout>/.agentflow/config.yaml`), checks its format version against the engine, resolves prompts (pack first, then engine default) and the state dir. Every project-specific value moves out of engine code and default prompts into pack keys (`project.skills_dir`, `project.docs.spec_file`, `paths.state_dir`, `work_source`, `skills.map`). The config that shipped inside the engine leaves it: its shape becomes the `python` fixture pack that the engine's test suite runs on.

**Tech Stack:** Python 3.12, stdlib + `pyyaml>=6` (the `flow.py` script header), pytest.

**Spec:** `docs/agentflow-harness-proposal.md`, sections 1b ("Portability") and 5 ("Slice P"). Read 1b's "Hard-coding to remove" and "Portability is enforced" lists before Task 1. Where 1b says "this repo's pack" it meant the project agentflow was cut from; that pack is not written here (see Out of scope).

## Global Constraints

- **Before Task 1:** `main` has no commits. The human makes the initial commit of the copied tree, and the work goes on a branch from it.
- Runtime dependencies stay stdlib + `pyyaml>=6`. No `packaging`, no new third-party import.
- Engine files (`agentflow/*.py`, `agentflow/dashboard.html`, `agentflow/prompts/*.md`) contain none of: `tivacare` (any case), `CONSTRAINTS.md`, `src/`, `docs/specs`, `docs/ideas`, `CAPABILITY-MAP`, `SPEC-`, `floor_guard`, `webadmin`, `next-step`. `agentflow/tests/` is exempt.
- A pack is config, never code: values, prompts and plain gate commands only.
- The engine floor in this slice: `.agentflow/**` is always a forbidden path, a pack without `gate_sets.verify` is refused, and user-only skills stay refused (already true).
- From Task 3 on, the golden files `agentflow/tests/fixtures/explain/*.txt` never change. They pin `agentflow explain` for every lane of the `python` pack, and they replace the old "byte-identical for the source project" check.
- Every config key this slice adds is either a Settings Form field or in `_PROTECTED_KEYS` (`config_edit.py`), with an `agentflow/tests/test_config_edit.py` case and a line in `docs/agentflow-playbook.md` §14 "Pack reference" (created in Task 3), in the same task. All keys in this slice are protected.
- Test command: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests --import-mode=prepend -q`. `--no-project` is required: `pyproject.toml` describes the project agentflow was copied from, not this one.
- Commit messages are an imperative sentence, ending with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

**Out of scope:**
- Later slices: the console script and `uv tool install` packaging, `agentflow hook install`, layered/sparse config, `init`/`doctor`, bundled generic skills, `agentflow migrate`, schema-generated Settings, `lessons.md`.
- Files copied from the source project that are not the engine stay as they are: `token-issuer/`, `devtools/e2e_stack.py`, `pyproject.toml`, `uv.lock`, `CLAUDE.md`, `CONSTRAINTS.md`, `AGENTS.md`, `docs/agent-orchestration-guide-v3.md`, `.claude/settings*.json`. The copied runs in `agentflow/runs/` and `agentflow/workspaces/` are git-ignored, never read or committed.
- The source project's own pack: it writes its `.agentflow/` when it adopts this engine.

## Review Focus

1. **Running from a linked worktree.** `agentflow` invoked inside a run's worktree must load the main checkout's `.agentflow/config.yaml`, never the worktree's copy, which is on another branch. Test in Task 3.
2. **Resuming a run whose recorded config path is gone.** Its `run_options` names a config file that no longer exists (e.g. the engine-internal `config.yaml` this slice removes). The refusal must name `.agentflow/config.yaml` as the `--config` to pass. Test in Task 3.
3. **A broken pack prompt override.** An unknown placeholder or missing `{brief}` in `.agentflow/prompts/x.md` must be reported against the pack file, and the engine default of the same name must not mask it. Test in Task 2.
4. **A state dir inside the repo that git tracks,** or a relative `paths.state_dir` resolved from a worktree's cwd. The first is a validate warning, and the second must resolve against the main checkout. Tests in Task 5.
5. **A pack with no work source** (the node fixture and this repo's pack). `next`, the Backlog and `queue` must not crash: `next` falls through to its other rules, the Backlog shows only added cards, and `queue` refuses with a message naming `work_source`. Tests in Task 6.

---

### Task 1: Run the engine from this repository's layout

The copy flattened `devtools/agentflow/` to `agentflow/`, `devtools/bin/` to `bin/` and `devtools/hooks/` to `hooks/`, but the test suite and some comments still assume the old tree (`conftest.py:18` sets `REPO` to this repo's parent). This task also removes the floor guard and its push hook: this repo doesn't keep them.

**Files:**
- Modify: `agentflow/tests/conftest.py:16-21` (`ENGINE = Path(__file__).resolve().parents[1]`, `ROOT = parents[2]` on `sys.path`, `REPO = ROOT`; rename uses of `DEVTOOLS`/`AGENTFLOW` to match), `agentflow/flow.py:8,24-27` (`_DEVTOOLS` → `_ROOT`, its comment and the docstring name `bin/agentflow`), `bin/agentflow:3` (the `ln -s` line names `$PWD/bin/agentflow`), `agentflow/workspace.py:3` (docstring), `agentflow/tests/test_state.py:550` (`parents[2]` is the repo root), `.gitignore` (add `agentflow/runs/`, `agentflow/workspaces/`)
- Delete: `devtools/floor_guard.py`, `hooks/floor_before_push.py`, `agentflow/tests/test_floor_guard.py`, `agentflow/tests/test_push_hook.py`, `agentflow/tests/floor_guard_skipped_test.txt`, `agentflow/tests/test_e2e_stack.py` (it tests the source project's `devtools/e2e_stack.py`, not the engine)
- Docs: `docs/agentflow-playbook.md` (each `devtools/bin/agentflow` → `bin/agentflow`, each `devtools/agentflow/` → `agentflow/`; 13 lines)
- Test: `agentflow/tests/test_layout.py`

- [ ] **Step 1: Write the failing tests in `agentflow/tests/test_layout.py`**

```python
def test_repo_fixture_is_this_checkout(repo_root):
    assert (repo_root / "agentflow" / "flow.py").is_file()
    assert (repo_root / "bin" / "agentflow").is_file()

def test_flow_runs_as_a_script_from_any_cwd(tmp_path):
    proc = subprocess.run([sys.executable, str(ROOT / "agentflow" / "flow.py"), "--help"],
                          cwd=tmp_path, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
```

- [ ] **Step 2: Run them to see them fail**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_layout.py --import-mode=prepend -q`
Expected: FAIL on `test_repo_fixture_is_this_checkout` (`repo_root` is this repo's parent).

- [ ] **Step 3: Implement** the path changes and deletions listed under Files. Change no engine behaviour: `state.py` keeps `devtools/agentflow/runs` until Task 5.

- [ ] **Step 4: Run the whole suite**

Run: the test command.
Expected: all pass. A failure that is not about a path or a deleted file is pre-existing: stop and report it rather than fix it in this task.

- [ ] **Step 5: Commit**

```bash
git add -A agentflow bin devtools hooks .gitignore docs/agentflow-playbook.md
git commit -m "Run agentflow's engine and tests from this repository's layout"
```

---

### Task 2: Prompts: pack overrides, engine defaults

**Files:**
- Create: `agentflow/pack.py`, `agentflow/tests/test_pack.py`
- Modify: `agentflow/lanes.py:84-85` (`_template`), `agentflow/config.py:704-815` (the validator's prompt checks, which read `self.prompts`), `agentflow/config_edit.py:356` (`_check` validates through the same resolution), `agentflow/tests/conftest.py` (`tmp_cfg`: `prompts/` becomes a real, empty directory instead of a symlink to the engine's, so a test can add an override)

**Interfaces:**
- Produces (`pack.py`, imports nothing from `agentflow`):
  - `ENGINE_PROMPTS: Path = Path(__file__).parent / "prompts"`
  - `prompt_path(config_path: Path, name: str) -> Path | None`: `config_path.parent / "prompts" / name` if it is a file, else `ENGINE_PROMPTS / name` if a file, else None
  - `prompt_files(config_path: Path) -> dict[str, Path]`: every resolvable prompt name mapped to the file that wins
  - `prompt_label(config_path: Path, file: Path) -> str`: `prompts/<name>` for a pack file, `engine:prompts/<name>` for an engine default. Validator messages use it.

- [ ] **Step 1: Write the failing tests in `agentflow/tests/test_pack.py`**

```python
def test_pack_prompt_wins_over_engine_default(tmp_path):
    (tmp_path / "prompts").mkdir(); (tmp_path / "prompts/step.md").write_text("pack")
    assert pack.prompt_path(tmp_path / "config.yaml", "step.md") == tmp_path / "prompts/step.md"
    assert pack.prompt_path(tmp_path / "config.yaml", "debug.md") == pack.ENGINE_PROMPTS / "debug.md"
    assert pack.prompt_path(tmp_path / "config.yaml", "nope.md") is None

def test_bad_placeholder_in_a_pack_override_is_reported_against_the_pack_file(tmp_cfg, repo_root):
    path = tmp_cfg()
    (path.parent / "prompts" / "verify.md").write_text("{not_a_placeholder}\n{brief}\n")
    issues = validate(load_config(path), repo_root)
    assert any(i.path == "prompts/verify.md" and "not_a_placeholder" in i.message for i in issues)

def test_override_without_brief_is_reported_even_though_the_engine_default_has_one(tmp_cfg, repo_root):
    path = tmp_cfg()
    (path.parent / "prompts" / "verify.md").write_text("no brief here\n")
    assert any("prompts/verify.md has no {brief}" in i.message for i in validate(load_config(path), repo_root))

def test_step_naming_an_unknown_prompt_is_an_error(tmp_cfg, repo_root):
    cfg = load_config(tmp_cfg(lambda d: d["phases"]["verify"]["steps"][0].__setitem__("prompt", "nope.md")))
    assert any("is not in the pack's prompts/ or the engine's" in i.message for i in validate(cfg, repo_root))
```
Adapt the `phases`/`steps` path in the last test to the shipped config's real shape. Read it first.

- [ ] **Step 2: Run them to see them fail**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_pack.py --import-mode=prepend -q`
Expected: FAIL, `ModuleNotFoundError: agentflow.pack`.

- [ ] **Step 3: Implement**

`lanes._template`, the validator and `config_edit._check` read through `pack.prompt_path` / `pack.prompt_files`. The validator checks placeholders and the `{brief}` contract on the file that wins, and names it with `prompt_label`. An engine default shadowed by a pack file is not checked against that step.

- [ ] **Step 4: Run the whole suite**

Run: the test command.
Expected: all pass. `tests/test_workflow.py` passes with no edit, because the shipped config still resolves every prompt to the same engine file.

- [ ] **Step 5: Commit**

```bash
git add -A agentflow
git commit -m "Resolve agentflow prompts from the pack first, then the engine's defaults"
```

---

### Task 3: The pack: location, format version, engine floor; the suite's `python` pack

`agentflow/config.yaml` leaves the engine. Its shape, with the source project's values swapped for a toy repo's, becomes the `python` fixture pack, and the test suite runs on that from now on. The CLI finds a pack only at `<main checkout>/.agentflow/config.yaml`. Until Task 7 writes one, `bin/agentflow` in this repo correctly reports that there's no pack.

**Files:**
- Modify: `agentflow/pack.py`, `agentflow/flow.py:30-56` (`default_config_path`, `repo_root`, `_find_repo`, `_human_errors`), `agentflow/run_control.py:42,60,246`, `agentflow/pipeline.py:220-235` (`SHIPPED_CONFIG`), `agentflow/config_trust.py` (docstring at :7 and the meaning of `default`), `agentflow/config.py:584` (`load_config`) and the validator, `agentflow/config_edit.py:46-49`, `agentflow/tests/conftest.py` (`cfg`, `tmp_cfg`, `FlowRepo`), `agentflow/tests/test_config_edit.py`
- Move: `agentflow/config.yaml` → `agentflow/tests/fixtures/packs/python/.agentflow/config.yaml` (`git mv`, then edit)
- Create: the rest of `agentflow/tests/fixtures/packs/python/` (`pyproject.toml`, `toy/__init__.py`, `tests/test_toy.py`, `docs/idea.md` with an `## Open Questions` section, `docs/specs/SPEC-greet.md`, `docs/specs/CAPABILITY-MAP.md` with a `greet` row, and `.claude/skills/<skill>/SKILL.md` stubs for every skill the pack names), `agentflow/tests/test_explain_golden.py`, `agentflow/tests/fixtures/explain/{feature,bug,eval,design}.txt`
- Docs: `docs/agentflow-playbook.md`: add `## 14. Pack reference` (one line per key: name, meaning, protected), starting with `version` and `engine`. Every `agentflow/config.yaml` in the playbook becomes `.agentflow/config.yaml`.

**Interfaces:**
- Consumes: `pack.prompt_path` (Task 2).
- Produces (`pack.py`):
  - `PACK_DIR = ".agentflow"`, `PACK_VERSION = 2`, `ENGINE_VERSION = "2.0"`, `ENGINE_FORBIDDEN = (".agentflow/**",)`
  - `class PackError(ValueError)`
  - `main_checkout(start: Path) -> Path`: today's `flow.repo_root`, moved here. `flow.repo_root` stays as an alias for its callers.
  - `pack_config(repo: Path) -> Path`: `main_checkout(repo) / ".agentflow" / "config.yaml"`. Raises `PackError("no .agentflow/config.yaml in <root>: this repository has no agentflow pack")` when it's absent.
  - `engine_satisfies(spec: str, version: str = ENGINE_VERSION) -> bool`: comma-separated clauses of `>=`, `>`, `<=`, `<`, `==` on dotted integers. Raises `PackError` on a clause it can't parse.
- `Config.guardrails.forbidden_paths` holds the pack's list plus `ENGINE_FORBIDDEN`, deduplicated, pack order first. `guards.py` and `agents.py` keep reading it unchanged.
- `conftest.py`: `PACKS = ENGINE / "tests/fixtures/packs"`. `cfg` loads `PACKS / "python/.agentflow/config.yaml"`. `tmp_cfg` writes `<tmp>/.agentflow/config.yaml` from it, with an empty real `prompts/` and `<tmp>/.claude/skills` symlinked to the fixture's stubs. `FlowRepo` symlinks the same stubs.

**The `python` pack's values.** Keep every key, gate name, gate-set membership, condition, lane and phase. Change only these:

| Source value | `python` pack value |
|---|---|
| `version: 1` | `version: 2`, then `engine: ">=2,<3"` with a line comment |
| `project.worktrees_dir: ../tivacare-worktrees` | `../toy-worktrees` |
| `project.docs.idea: docs/ideas/agentic-rag-customer-support.md` | `docs/idea.md` |
| any path under `src/tivacare/` (gates, overlays) | the same path under `toy/` |
| `webadmin` (gates' `cwd` and `when_exists`) | `web`; the toy has no `web/`, so these gates skip |
| gate commands naming source-project tools (`floor_guard.py`, `tivacare.eval.cli`, `docker compose -p tivacare-test`, `e2e_stack.py`, `pyright`, `ruff`, `diff-cover`, `pnpm`) | the toy's equivalent (`python3 -m unittest -q` for unit, `python3 -m toy.eval` for `eval`), or `true` where the toy has no such tool |
| `guardrails.forbidden_paths` entries `devtools/floor_guard.py`, `devtools/agentflow/config.yaml`, `devtools/agentflow/tests/**` | dropped (the engine adds `.agentflow/**`) |
| `overlays.security.when_keywords` naming the source product (`IS4`, `Entra`, `customer_code`, `visibility = public`) | `token`, `password`, `secret` |
| comments that explain the source project | dropped |

Rewrite a test assertion only where it names a value from this table or the moved config path. List each rewritten assertion's file and test name in the commit body.

- [ ] **Step 1: Write the failing tests (in `agentflow/tests/test_pack.py` unless noted)**

```python
def test_pack_config_from_a_linked_worktree_is_the_main_checkouts(tmp_git, git):
    write_pack(tmp_git, {})                                            # helper: minimal format-2 pack, committed
    wt = make_worktree(tmp_git, git)                                   # helper: linked worktree on another branch
    assert pack.pack_config(wt) == (tmp_git / ".agentflow/config.yaml").resolve()

def test_missing_pack_names_the_path(tmp_git):
    with pytest.raises(pack.PackError, match=r"no \.agentflow/config\.yaml in "):
        pack.pack_config(tmp_git)

@pytest.mark.parametrize("spec,ok", [(">=2,<3", True), (">=2.1", False), ("==2.0", True), ("<2", False), (">1", True)])
def test_engine_satisfies(spec, ok):
    assert pack.engine_satisfies(spec, "2.0") is ok

def test_engine_spec_garbage_is_refused():
    with pytest.raises(pack.PackError): pack.engine_satisfies("~2")

def test_version_1_pack_is_refused(tmp_cfg):
    with pytest.raises(ConfigError, match="pack format 1; this engine reads format 2"):
        load_config(tmp_cfg({"version": 1}))

def test_engine_range_that_excludes_this_engine_is_refused(tmp_cfg):
    with pytest.raises(ConfigError, match=r"engine: '>=3' .* this engine is 2\.0"):
        load_config(tmp_cfg({"engine": ">=3"}))

def test_pack_dir_is_forbidden_even_when_the_pack_omits_it(tmp_cfg):
    cfg = load_config(tmp_cfg(lambda d: d["guardrails"].__setitem__("forbidden_paths", ["X.md"])))
    assert cfg.guardrails.forbidden_paths == ["X.md", ".agentflow/**"]

def test_pack_without_a_verify_gate_set_is_refused(tmp_cfg, repo_root):
    cfg = load_config(tmp_cfg(lambda d: d["gate_sets"].pop("verify")))
    assert any(i.level == "error" and i.path == "gate_sets.verify" for i in validate(cfg, repo_root))

def test_resuming_a_run_whose_config_is_gone_names_the_pack(...):
    # a Run whose first run_options records <tmp>/agentflow/config.yaml, now deleted
    with pytest.raises(ConfigUntrusted, match=r"--config \.agentflow/config\.yaml"):
        trusted_config_path(run, None, pack.pack_config(run.repo))

def test_python_pack_validates_clean(pack_repo_python):               # fixture: the python pack copied into a git repo
    assert validate(load_config(pack.pack_config(pack_repo_python)), pack_repo_python) == []
```
In `agentflow/tests/test_explain_golden.py`: for each lane of the `python` pack, run `flow.py explain <lane> --module greet --topic x --issue 1` in a fresh git copy of the fixture (with `XDG_STATE_HOME` under `tmp_path`), and compare stdout with `fixtures/explain/<lane>.txt`. When `AGENTFLOW_WRITE_GOLDEN=1`, write the file and skip. In `test_config_edit.py`, a proposed change to `version` or `engine` is refused with the existing "protected" message.

- [ ] **Step 2: Run them to see them fail**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_pack.py agentflow/tests/test_config_edit.py --import-mode=prepend -q`
Expected: FAIL, `AttributeError: module 'agentflow.pack' has no attribute 'pack_config'`, plus the protected-key cases.

- [ ] **Step 3: Implement**

- `pack.py` as in Interfaces.
- `load_config` requires `version == PACK_VERSION` (`ConfigError("version", "pack format <n>; this engine reads format 2")`) and a string `engine` that `engine_satisfies` (`ConfigError("engine", "engine: '<spec>' does not include this engine; this engine is 2.0")`). It appends `ENGINE_FORBIDDEN` to `forbidden_paths`.
- Validator: an error at `gate_sets.verify` when the pack has no non-empty `verify` set ("every pack declares the gates a diff must pass: gate_sets.verify").
- Replace `flow.default_config_path()`, `run_control.DEFAULT_CONFIG` and `pipeline.SHIPPED_CONFIG` with `pack.pack_config(repo)`. Each caller already has the repo, or a `Run` (`run.repo`). `trusted_config_path(run, explicit, default)` keeps its signature, and `default` is now the pack config. Its "cannot be read" message gains `Pass --config .agentflow/config.yaml to use the repository's pack.` Add `PackError` to `flow._human_errors()`.
- `git mv` the config and apply the table. Write the fixture repo files and skill stubs (frontmatter `name`/`description` plus one line). A test that asserted on a skill's real text now asserts on the stub's text.
- `config_edit._PROTECTED_KEYS`: add `"version"`, `"engine"`.
- Run `AGENTFLOW_WRITE_GOLDEN=1` on `test_explain_golden.py` once, then read the four files. Each should list the lane's steps with no `error:` line.

- [ ] **Step 4: Run the whole suite**

Run: the test command.
Expected: all pass, with the golden test comparing (not writing).

- [ ] **Step 5: Commit**

```bash
git add -A agentflow docs/agentflow-playbook.md
git commit -m "Move agentflow's config out of the engine into a versioned .agentflow pack"
```

---

### Task 4: Generic engine default prompts

**Files:**
- Modify: the 28 prompts in `agentflow/prompts/` that name the source project (every file except `_result.md`, `debug.md`, `regression_test.md`, `condition_question.md`), rewritten in place
- Test: `agentflow/tests/test_pack.py`

- [ ] **Step 1: Write the failing tests**

```python
PROMPT_DENYLIST = re.compile(r"(?i:tivacare)|CONSTRAINTS\.md|src/|docs/specs|docs/ideas|CAPABILITY-MAP|SPEC-|floor_guard|webadmin|next-step")

def test_engine_prompts_name_no_project():
    hits = [f"{f.name}:{n}" for f in pack.ENGINE_PROMPTS.glob("*.md")
            for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1) if PROMPT_DENYLIST.search(line)]
    assert hits == []

def test_engine_default_prompts_use_only_known_placeholders():
    for file in pack.ENGINE_PROMPTS.glob("*.md"):
        names = set(re.findall(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", file.read_text()))
        assert names <= PROMPT_PLACEHOLDERS, file.name
```

- [ ] **Step 2: Run them to see them fail**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_pack.py --import-mode=prepend -q`
Expected: FAIL, listing hits in about 28 files.

- [ ] **Step 3: Rewrite each prompt**

Keep its placeholders, structure and `{brief}` contract (a prompt has `{brief}` after the rewrite exactly when it did before). Rules:
- A repo path becomes an existing placeholder: `{spec}`, `{plan}`, `{idea}`, `{capability_map}`.
- `CONSTRAINTS.md` becomes "the repository's quality rules", and `CLAUDE.md`/`AGENTS.md` become "the repository's agent instructions".
- Repo commands (`uv run …`, `pnpm --dir webadmin …`, `devtools/e2e_stack.py`, `floor_guard.py`) become "run only the tests you wrote or changed".
- Product rules are dropped: widget routes, the platform-spec conventions, the source product's design specs, and the 40-130 KB/330 KB sizes.
- Keep every rule that holds for any repo: no commits, never weaken a gate, ask instead of guessing, grep big docs by section.

- [ ] **Step 4: Run the whole suite**

Run: the test command.
Expected: all pass, with the golden files unchanged. A test that asserted on source-project prompt wording (e.g. `test_headless.py:317`) asserts on the generic rule instead. Name each one in the commit body.

- [ ] **Step 5: Commit**

```bash
git add -A agentflow
git commit -m "Make agentflow's default prompts project-agnostic"
```

---

### Task 5: Paths from the pack: skills dir, spec file name, state dir

**Files:**
- Modify: `agentflow/pack.py`, `agentflow/config.py` (`Project`, the validator's `skill()` at :761-764), `agentflow/guards.py:120`, `agentflow/lanes.py:133-139` (`_read_file_skills`) and the doc checks near :725, `agentflow/conditions.py:54-77` (`make_ctx`), `agentflow/state.py:808-815`, `agentflow/agents.py:512-513` (deny list), `agentflow/config_edit.py:46-49`, `agentflow/gates.py:170` (`EDITING_RULES` text), `agentflow/tests/conftest.py` (an autouse fixture sets `XDG_STATE_HOME` to `tmp_path / "xdg"`), the `python` pack (`project.docs.spec_file: "SPEC-{module}.md"`)
- Test: `agentflow/tests/test_pack.py`, `test_guards.py`, `test_agents.py`, `test_state.py`, `test_config_edit.py`. Tests that build `root / "devtools/agentflow/runs"` (`test_backlog.py:511`, `test_agents.py:45`, `test_control_cmds.py:349`, `test_dashboard.py:893`, `test_hardening.py:328`, `test_next_queue.py:382,576`, `test_state.py:370,383,415`, `test_guards.py:335`) use `runs_root(root)` / `workspaces_root(root)` instead.
- Docs: playbook §14 gains `project.skills_dir`, `project.docs.spec_file`, `paths.state_dir` (protected).

**Interfaces:**
- Consumes: `pack.main_checkout`, `pack.pack_config` (Task 3).
- Produces:
  - `config.Project.skills_dir: str`, from `project.skills_dir`, default `DEFAULT_SKILLS_DIR = ".claude/skills"`
  - `config.Project.spec_file: str`, from `project.docs.spec_file`, default `"{module}.md"` (the spec is `<docs.specs>/<spec_file>`)
  - `pack.state_dir(repo: Path) -> Path`: the pack's raw `paths.state_dir` (`~` expanded; a relative value is relative to `main_checkout(repo)`), else `${XDG_STATE_HOME:-~/.local/state}/agentflow/<repo-id>`, where `repo-id = f"{root.name}-{sha256(str(root)).hexdigest()[:8]}"`. It reads the pack YAML directly (no `load_config`), cached by `(path, st_mtime_ns)`. With no pack, it uses the default.
  - `state.runs_root(repo) = pack.state_dir(repo) / "runs"`, `state.workspaces_root(repo) = pack.state_dir(repo) / "workspaces"` (same signatures as today)
- `project.docs` keys: `specs` and `plans` are required, and `idea`, `decisions`, `glossary` and `spec_file` are optional. `make_ctx` fills `{idea}` with `""` when `idea` is unset.

- [ ] **Step 1: Write the failing tests**

```python
def test_state_dir_relative_value_is_relative_to_the_main_checkout(tmp_git, git):
    write_pack(tmp_git, {"paths": {"state_dir": "var/af"}})
    wt = make_worktree(tmp_git, git)
    assert pack.state_dir(wt) == (tmp_git / "var/af").resolve()

def test_state_dir_default_is_outside_the_repo(tmp_git, monkeypatch, tmp_path):
    write_pack(tmp_git, {}); monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    got = pack.state_dir(tmp_git)
    assert got.parent == tmp_path / "xdg/agentflow" and got.name.startswith(tmp_git.name + "-")

def test_state_dir_inside_the_repo_and_not_ignored_warns(tmp_git):  # pack at tmp_git with paths.state_dir: state
    assert any(i.level == "warning" and i.path == "paths.state_dir" for i in validate(cfg_with_state_dir, tmp_git))

def test_skills_dir_is_read_from_the_pack(tmp_cfg, tmp_path):
    repo = copy_skills(tmp_path, into="skills")                     # the python pack's stubs copied to skills/
    cfg = load_config(tmp_cfg({"project": {"skills_dir": "skills"}}))
    assert not [i for i in validate(cfg, repo) if "SKILL.md" in i.message]

def test_user_only_skill_read_from_a_custom_skills_dir_trips(tmp_cfg):
    cfg = load_config(tmp_cfg({"project": {"skills_dir": "skills"}}))
    calls = [{"name": "Read", "input": {"file_path": "skills/doubt-driven-development/SKILL.md"}}]
    assert [v.kind for v in guards._user_only_violations(cfg, calls)] == ["user_only_skill"]

def test_spec_placeholder_uses_spec_file(tmp_cfg, repo_root):
    cfg = load_config(tmp_cfg({"project": {"docs": {"spec_file": "{module}-spec.md"}}}))
    assert make_ctx(cfg, repo_root, "feature", {"module": "crm"}).values["spec"] == "docs/specs/crm-spec.md"

def test_runner_denies_edits_to_the_runs_root(repo_root):        # in test_agents.py, beside the existing argv tests
    assert f"Edit(/{runs_root(repo_root)}/**)" in deny_rules(claude_argv_for(repo_root))
```
`write_pack`, `make_worktree` (Task 3) and `copy_skills(root, into)` are helpers in `test_pack.py`. `deny_rules`/`claude_argv_for` follow the existing argv tests in `test_agents.py`. Also: `project.skills_dir`, `project.docs.spec_file` and `paths.state_dir` are each refused from the Settings page (`test_config_edit.py`), `_read_file_skills` names `<skills_dir>/<skill>/SKILL.md`, and a step with `checks:` in a pack without `docs.idea`/`docs.decisions` is a validate error at that step.

- [ ] **Step 2: Run them to see them fail**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_pack.py agentflow/tests/test_guards.py agentflow/tests/test_state.py agentflow/tests/test_agents.py agentflow/tests/test_config_edit.py --import-mode=prepend -q`
Expected: FAIL on the new cases only.

- [ ] **Step 3: Implement**

- Replace the three `.claude/skills` literals (`config.py:764`, `guards.py:120`, `lanes.py:138`) with `cfg.project.skills_dir`. `_read_file_skills` gains a `skills_dir: str` parameter.
- `conditions.make_ctx`: `spec = f"{docs['specs']}/{cfg.project.spec_file.format(module=module)}"`.
- `state.runs_root`/`workspaces_root` delegate to `pack.state_dir`. The validator warns at `paths.state_dir` when the dir is inside the main checkout and `git check-ignore -q` says it isn't ignored.
- `agents.py:513`: add `Edit(/<absolute runs_root>/**)` to the runner's deny list. That's the runner's own state, wherever the pack puts it. Workspaces stay writable. The existing comment says `claude` ignores the `Edit(/path)` project-root form in `--settings`, so check the absolute-path form (`//` + absolute path) in the Claude Code permissions docs via context7 before writing it, and write the test to the syntax the docs give.
- `_PROTECTED_KEYS`: add `"paths"`. `project` is already protected, which covers the rest.
- `gates.EDITING_RULES` becomes `"the design doc's editing rules"`.

- [ ] **Step 4: Run the whole suite**

Run: the test command.
Expected: all pass, with the golden files unchanged and nothing written under `~/.local/state` (the autouse fixture).

- [ ] **Step 5: Commit**

```bash
git add -A agentflow docs/agentflow-playbook.md
git commit -m "Read agentflow's skills dir, spec file name and state dir from the pack"
```

---

### Task 6: Work source and `next` from the pack

**Files:**
- Create: `agentflow/work_source.py`, `agentflow/tests/test_work_source.py`
- Modify: `agentflow/config.py` (`WorkSource`, `skills_map`, validator), `agentflow/conditions.py:54-77,248-252`, `agentflow/backlog.py:163-166,364`, `agentflow/module_queue.py` (header docstring, `DESIGN_DOC_PATHS`, `SECURITY_LINE`, rules 2-7 at :170-280, `_module_ids`, `assess_modules`, `run_queue`), `agentflow/pipeline.py:163-175`, `agentflow/feature_lane.py:37,646`, `agentflow/sources.py` (spec listing), `agentflow/config_edit.py:46-49`, the `python` pack
- Docs: playbook §14 gains `work_source` and `skills` (protected) and drops `project.docs.capability_map`.
- Test: `test_next_queue.py`, `test_backlog.py`, `test_conditions.py`, `test_config_edit.py`

**Interfaces:**
- Consumes: `cfg.project.spec_file` (Task 5).
- Produces:
  - `config.WorkSource(kind: Literal["capability_map"], path: str)`, and `Config.work_source: WorkSource | None` from the optional `work_source:` block. Validate refuses any other `kind` ("the only work source this engine reads is capability_map").
  - `config.Config.skills_map: dict[str, str]` from the optional `skills.map`, with keys `decide`, `define`, `design_doc`, `plan`, `build`, `build_small`, `verify`, `ship`. Validate: an unknown key is an error, and every value must be a skill in `skills_dir`.
  - `work_source.items(cfg: Config, repo: Path) -> list[sources.ModuleNode]`: `[]` when `work_source` is None or the file is missing
  - `work_source.add_item(cfg: Config, repo: Path, template: dict[str, str], values: dict) -> None`: `ValueError("this pack has no work_source")` when None
  - `sources.spec_modules(specs_dir: Path, spec_file: str) -> dict[str, Path]`: module id → spec file, matching `spec_file` with `{module}` as the wildcard
  - `make_ctx` fills `{capability_map}` from `work_source.path` (`""` when unset)

- [ ] **Step 1: Write the failing tests**

```python
def test_security_line_comes_from_the_overlay_and_approval_note(cfg):
    assert module_queue.security_line(cfg) == ("a security path is involved: add `security-and-hardening` at Plan "
                                               "and Review and offer `/doubt-driven-development` before Build")

def test_skills_come_from_skills_map(tmp_cfg, repo_with_unchecked_plan):
    cfg = load_config(tmp_cfg({"skills": {"map": {"build": "my-build", "build_small": "my-small"}}}))
    assert next_step(cfg, repo).next in ("my-build", "my-small")

def test_design_doc_rule_uses_docs_idea_decisions_glossary(cfg, repo_dirty_with("docs/decisions/ADR-099.md")):
    assert next_step(cfg, repo).lane == "design-doc"

def test_no_work_source_next_falls_through(cfg_without_work_source, tmp_git):
    assert next_step(cfg, tmp_git).lane == "none"

def test_no_work_source_queue_refuses(cfg_without_work_source, tmp_git):
    with pytest.raises(ValueError, match="work_source"): run_queue(cfg, tmp_git, ...)

def test_no_work_source_backlog_has_only_added_cards(...): ...

def test_spec_modules_reads_the_template(tmp_path):
    (tmp_path / "SPEC-crm.md").touch(); (tmp_path / "notes.md").touch()
    assert sources.spec_modules(tmp_path, "SPEC-{module}.md") == {"crm": tmp_path / "SPEC-crm.md"}

def test_modules_done_reads_the_work_source(...): ...   # deps_merged reason text unchanged
```
In `test_config_edit.py`, `work_source` and `skills` are refused from the page. The existing `test_next_queue.py` assertions on skill names must pass unchanged: they read the `python` pack, whose `skills.map` holds the same names.

- [ ] **Step 2: Run them to see them fail**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_work_source.py agentflow/tests/test_next_queue.py agentflow/tests/test_config_edit.py --import-mode=prepend -q`
Expected: FAIL on the new cases.

- [ ] **Step 3: Implement**

- `work_source.py` dispatches on `kind` to the existing `sources.capability_map` / `sources.add_module_row`. That table format ("Module id", "Depends on" columns) is the engine's definition of the `capability_map` kind. Route the five callers through it: `backlog.py:163`, `module_queue.py:153,467`, `conditions.py:252` and `pipeline.py:173`. `modules_done` drops its `from:` parameter (validate refuses `from` with "modules_done reads work_source").
- `module_queue`:
  - `DESIGN_DOC_PATHS` becomes the set of `project.docs` `idea`, `decisions`, `glossary` that are set. A directory matches by prefix.
  - `SECURITY_LINE` becomes `security_line(cfg) -> str`, built from `overlays.security.add_skills` (the distinct skills, then their stages title-cased, joined with " and ") and `phases.plan.approval_note.human_skill`. It is `""` when either is missing.
  - Each rule's skill is `cfg.skills_map.get(<key>)`. With no entry, `NextAnswer.next` is the stage name (`"Build stage"`).
  - Rule texts lose the source project's wording:
    - eval: "run the eval gate; a regression blocks the change"
    - checked plan: `f"then {ship}"`
    - design doc: "finish the design-doc edits, then commit when the user asks"
    - idle: next `f"{decide} or {define}"`, reason `f"nothing is in flight: ask what to do; {decide} for a decision, {define} for something to build"`
  - The docstring calls it "a deterministic next-step check". Rule 5 lists specs with `sources.spec_modules`.
  - `run_queue`: `ValueError("this pack has no work_source: the queue has no modules to start")` when unset.
- `feature_lane.py:646`: the default message becomes `"Decide {topic} in the {module} spec"`, and its docstring follows.
- `python` pack:
  - `work_source: {kind: capability_map, path: docs/specs/CAPABILITY-MAP.md}` replaces `project.docs.capability_map`.
  - Drop `from: capability_map` from `deps_merged`.
  - Add `skills.map: {decide: grilling, define: spec-driven-development, design_doc: domain-modeling, plan: writing-plans, build: subagent-driven-development, build_small: executing-plans, verify: verification-before-completion, ship: finishing-a-development-branch}` (and a stub for any of these the fixture lacks).
- `_PROTECTED_KEYS`: add `"work_source"`, `"skills"`.

- [ ] **Step 4: Run the whole suite**

Run: the test command.
Expected: all pass, with the golden files unchanged.

- [ ] **Step 5: Commit**

```bash
git add -A agentflow docs/agentflow-playbook.md
git commit -m "Read agentflow's work source and next-step skills from the pack"
```

---

### Task 7: The node pack, this repository's pack, and the denylist test

**Files:**
- Create: `agentflow/tests/fixtures/packs/node/` (`package.json`, `index.js`, `index.test.js`, `skills/<skill>/SKILL.md` for each skill its workflow names, `.agentflow/config.yaml`), `agentflow/tests/test_portability.py`, `.agentflow/config.yaml` (this repository's pack)
- Modify: `agentflow/module_queue.py:3`, `agentflow/dashboard.html:177`, and any engine file the denylist test flags; `docs/agentflow-playbook.md` §13 ("Using agentflow in another repository")
- Move: `PROMPT_DENYLIST` and its test from `test_pack.py` into `test_portability.py`, widened to all engine files

**Interfaces:**
- Consumes: everything above.
- The `node` pack is the minimum: no `prompts/` dir, no `work_source`, no `idea`, `skills_dir: skills`, the default `spec_file`, lanes `feature` only, `gate_sets.verify: [unit]` with `unit: {run: "node --test"}`, and no `paths.state_dir`.
- This repository's pack (`.agentflow/config.yaml`) starts from the `python` pack:
  - `project`: `worktrees_dir: ../agentflow-harness-worktrees`, `docs: {specs: docs, spec_file: "agentflow-{module}-proposal.md", plans: docs/plans}`, no `idea`/`decisions`, no `work_source`. The `harness` module is `docs/agentflow-harness-proposal.md`.
  - `gates`: `engine_tests: {run: "uv run --no-project --with pyyaml --with pytest pytest agentflow/tests --import-mode=prepend -q"}`, `portability: {run: "<the same> agentflow/tests/test_portability.py"}`. `gate_sets`: `verify: [engine_tests]`, `build: [portability]`, and any other set the validator requires is `[portability]`.
  - `lanes: [feature, bug]`. Drop every step, condition and overlay used only by `eval` or `design`.
  - `overlays.security.when_paths`: `[agentflow/guards.py, agentflow/agents.py, agentflow/config_trust.py, agentflow/safeio.py, agentflow/workspace.py, agentflow/pack.py]`, and `when_keywords`: `[permission, deny rule, forbidden_paths, bypassPermissions]`.
  - `guardrails.forbidden_paths`: `[.claude/**, agentflow/tests/fixtures/explain/**]`. `user_only_skills` unchanged.
  - `skills.map` as the `python` pack (this repo's `.claude/skills/` has those skills). No `paths.state_dir`: state lives outside the repo.

- [ ] **Step 1: Write the failing tests in `agentflow/tests/test_portability.py`**

```python
PACKS = ["python", "node"]
DENYLIST = re.compile(r"(?i:tivacare)|CONSTRAINTS\.md|src/|docs/specs|docs/ideas|CAPABILITY-MAP|SPEC-|floor_guard|webadmin|next-step")

@pytest.fixture(params=PACKS)
def pack_repo(request, tmp_path, git, monkeypatch):   # copy the fixture into a fresh git repo; XDG_STATE_HOME under tmp
    ...

def test_fixture_pack_validates_clean(pack_repo):
    assert validate(load_config(pack.pack_config(pack_repo)), pack_repo) == []

def test_fixture_pack_explains_every_lane(pack_repo):
    cfg = load_config(pack.pack_config(pack_repo))
    for lane in cfg.lanes:
        assert explain(cfg, lane, make_ctx(cfg, pack_repo, lane, {"module": "greet", "issue": "1"}), {})

def test_fixture_pack_dry_run_writes_nothing(pack_repo):
    status_before = git_status(pack_repo)
    proc = subprocess.run([sys.executable, str(FLOW), "run", "feature", "--module", "greet", "--dry-run"],
                          cwd=pack_repo, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert git_status(pack_repo) == status_before

def test_fixture_pack_state_lives_outside_the_repo(pack_repo):
    assert not pack.state_dir(pack_repo).is_relative_to(pack_repo)

def test_this_repos_pack_validates_clean(repo_root):
    assert validate(load_config(pack.pack_config(repo_root)), repo_root) == []

def test_engine_names_no_pack_strings():
    engine = Path(agentflow.__file__).parent
    files = [*engine.glob("*.py"), engine / "dashboard.html", *engine.glob("prompts/*.md")]
    hits = [f"{f.name}:{n}: {line.strip()}" for f in files
            for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1) if DENYLIST.search(line)]
    assert hits == []
```

- [ ] **Step 2: Run them to see them fail**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_portability.py --import-mode=prepend -q`
Expected: FAIL. The denylist test lists the remaining hits (at least `module_queue.py:3` and `dashboard.html:177`), and the node and this-repo tests fail until those packs exist.

- [ ] **Step 3: Write the packs and clear the remaining hits**

- Write the `node` pack and this repo's pack as in Interfaces. Their phases name only engine default prompts and skills the pack's `skills_dir` provides.
- `dashboard.html:177` names the user-only skills from the config the page already loads, not a literal.
- Every other hit gets the treatment from Tasks 4-6: a pack value or generic wording. Never add an exemption to the test.
- Playbook §13 becomes: write `.agentflow/config.yaml` (start from `agentflow/tests/fixtures/packs/python` or this repo's `.agentflow/`), put prompt overrides in `.agentflow/prompts/`, run `agentflow validate`. That replaces the list of files to rewrite.

- [ ] **Step 4: Run everything**

```bash
uv run --no-project --with pyyaml --with pytest pytest agentflow/tests --import-mode=prepend -q
bin/agentflow validate
bin/agentflow explain feature --module harness
```
Expected: all tests pass with the golden files unchanged, `validate` prints `0 warnings`, and `explain` prints `explain feature {"module": "harness"}` and the lane's steps with no `error:` line.

- [ ] **Step 5: Commit**

```bash
git add -A .agentflow agentflow docs/agentflow-playbook.md
git commit -m "Prove agentflow's engine is project-agnostic with fixture packs, a denylist test and its own pack"
```
