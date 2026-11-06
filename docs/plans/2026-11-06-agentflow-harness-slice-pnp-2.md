# agentflow harness, slice PnP-2: `doctor`, `detect` and `init` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `cd any-repo && agentflow init` writes a sparse pack that `validate`s, `explain`s and dry-runs, with nothing else written to the repository. Gates are only *proposed* until a human confirms them. They run once on the base branch only when the human asks for that with `--baseline`.

**Prerequisite:** slice PnP-1 (`docs/plans/2026-11-05-agentflow-harness-slice-pnp-1.md`) has shipped: layers, engine defaults, starters, bundled skills, `gates_confirmed`, `baseline`, and gate `cwd` validation.

**Architecture:**
- `doctor.py` checks the machine.
- `detect.py` reads a repository **from its base branch** through git, never from the working tree.
- `init.py` renders the pack as YAML data. With `--baseline`, it first runs the gates in a throwaway worktree, then writes the pack once.

**Tech Stack:** Python 3.12, stdlib + `pyyaml>=6`, pytest.

**Spec:** `docs/agentflow-harness-proposal.md`, §1c "Plug and play" ("Install and run", `doctor`, "Leaves no footprint") and the "Plug and play" bullet of "## Verification".

## Global Constraints

- All of PnP-1's Global Constraints still hold. These matter most here:
  - stdlib + `pyyaml`
  - the denylist over every engine file (no `docs/specs`, `src/` or `next-step` in any regex, hint or message)
  - unchanged explain goldens
  - the four-part rule
  - the engine floor
- **No repository-controlled command runs before a human asks for it.** `init` runs gates only with `--baseline`. `doctor` never executes a pack's commands; it only looks programs up on `PATH`. The one exception is the agent CLI's login check, which runs only when the agent's command is a bare `claude` or `codex` on `PATH`.
- **Security overlay.** Tasks 2 and 3 parse untrusted repository files and write the pack, and Task 3 changes `gates.py` and `safeio.py`. Review them with `security-and-hardening`.
- **Execution.** Execute this plan interactively (subagent-driven-development). Task 4 edits `.agentflow/config.yaml`.
- Line numbers cited are approximate; find the function by name.
- Test command: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests --import-mode=prepend -q`.
- Commit messages are one imperative sentence, ending with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

**Out of scope:**
- a Rust starter (`Cargo.toml` gets a note)
- the multi-project registry
- `agentflow baseline` on an existing pack
- measuring the proposal's "time to first dry run under 10 minutes": record it as not measured

**Deviations from the proposal** (Task 4 records them in proposal §1c, together with PnP-1's):
1. **`init` runs no gate without `--baseline`.** Decided 2026-11-06: detected gates are a proposal.
2. **`init` writes the permission line itself:** `agents.claude.permission_mode: bypassPermissions`, with a comment. Decided 2026-11-06.
3. **The bug lane needs a Bash allow rule; `init` prints one.** Decided 2026-11-06.
4. **The Portability check.** The proposal's "Portability" check (`init --starter python-uv` in an empty repo, then `validate` reports 0 warnings) is read together with §1c's `gates_confirmed` rule: `validate` reports exactly the `gates_confirmed` warning, and 0 once it is set.

## Review Focus

1. **Real CI workflows.** Detection must produce only gates that run the same way they do in CI, or skip the step with a note. It must never crash on a malformed file. Task 2.
2. **`init --baseline`.** An environment failure (a missing tool, a timeout, a failed setup) must never be recorded as `baseline: red`. Cleanup must touch only `init`'s own worktrees. The pack is written once, exclusively. Task 3.
3. **No footprint.** After `init`, `git status` shows only `.agentflow/config.yaml`, and any change to `.git/config` or `.git/hooks` made by a setup gate is reported. Tasks 3 and 4.

---

### Task 1: `agentflow doctor`

**Depends on:** none

**Files:**
- Create: `agentflow/doctor.py`
- Modify: `agentflow/flow.py` (the `doctor` command), `docs/agentflow-playbook.md` §10 and §12
- Test: `agentflow/tests/test_doctor.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) class Check: name: str; status: Literal["ok", "warn", "fail"]; detail: str; fix: str = ""`
  - `doctor.run_checks(cfg: Config, repo: Path) -> list[Check]`
  - `doctor.render(checks) -> str`
  - `doctor.TIMEOUT_S = 20`
  - `doctor.gate_programs(command: str) -> list[str]` (Task 3 uses it)

**How the command loads config.** `agentflow doctor [--config PATH]` loads the pack layered. If loading fails at the user file, it adds a `user config` `fail` check, naming the file and the error, then reloads with `load_config(..., user=False)` and goes on.

The checks, in order. Each external command times out after `TIMEOUT_S`, and a timeout is a `fail` with the detail "timed out".
1. **`agent:<name>`, for each enabled agent.**
   - The agent's `command[0]` must be a bare name (no `/`) that `shutil.which` finds. Otherwise fail with "install `<cmd>`, or set `agents.<name>.enabled: false`".
   - Only when its basename is `claude` or `codex`, check the login: `[*command, "auth", "status"]` or `[*command, "login", "status"]`. A non-zero exit fails with "run `claude auth login`" or "`codex login`".
   - Any other agent gets a `warn`: "login not checked".
2. **`gh`.** It must be present, and `gh auth status` must exit 0. Otherwise fail with "install gh" or "run `gh auth login`". The detail notes that this check needs the network.
3. **`remote`.** `git remote get-url origin` must succeed. Otherwise fail with "add a remote named origin: `finish` pushes the run's branch there".
4. **`docker`**, only when `docker` is among some gate's `gate_programs`. `docker info` must exit 0. Otherwise fail with "start Docker".
5. **`state dir`.** `pack.state_dir(repo)` can be created, and a probe file there can be written and removed. Otherwise fail with the `OSError`.
6. **`gate:<name>`, for each gate in any gate set.** Check each of the gate's `gate_programs` against `PATH`, using `defaults.env.PATH` when it is set.
   - A program on `PATH` is fine.
   - A relative program is a `warn` ("may be created by setup").
   - A missing program is a `warn` ("may be installed by `gate_sets.setup`") when the pack has setup gates, and a `fail` ("install `<prog>`") otherwise.
   - A command that can't be tokenised is a `warn`.

**What `gate_programs` counts as a program.**
- Tokenise with `shlex.shlex(command, posix=True, punctuation_chars=True)`, with `commenters="#"` and `whitespace_split=True`.
- Split the token stream at the operator tokens (`&&`, `||`, `;`, `|`, `&`, newline, `(`, `)`).
- In each segment, skip leading `NAME=value` tokens. The next token is the program unless it is one of these:
  - a shell builtin or keyword: `true false test [ [[ cd export set source . exec command env time if then elif else fi for do done while until case esac { } ! exit trap`;
  - a word starting with `$`.
- A builtin such as `env` or `command` is skipped, and the token after it is not looked at.

- [ ] **Step 1: Write the failing tests.** Fake executables are `sh` scripts in a tmp dir, put first on `PATH`. The fake `gh` is `tests/bin/gh`, with `FAKE_GH_FAIL`.

```python
def test_doctor_is_all_ok_with_every_tool_present_and_logged_in()
def test_a_missing_agent_cli_fails_with_an_install_hint()
def test_a_logged_out_agent_fails_with_a_login_hint()
def test_an_agent_command_that_is_a_path_is_never_executed(tmp_path)   # command: ["./evil"]; a marker file stays absent
def test_a_disabled_agent_is_not_checked()
def test_gh_logged_out_fails()
def test_no_origin_remote_fails()
def test_docker_is_checked_only_when_a_gate_runs_it()          # "echo docker" alone does not count
def test_an_unwritable_state_dir_fails()
@pytest.mark.parametrize("command,programs", [
    ("FOO=1 nosuchtool --x", ["nosuchtool"]), ("cd web && npm test", ["npm"]),
    ("python -c \"import x; y\"", ["python"]), ("set -e\n# note\npytest -q", ["pytest"]),
    ("${PYTHON:-python} -m pytest", []), ("pytest | tee out", ["pytest", "tee"])])
def test_gate_programs(command, programs)
def test_a_missing_gate_program_fails_without_setup_and_warns_with_setup()
def test_a_broken_user_file_is_a_failed_check_not_a_crash()
def test_a_check_that_hangs_times_out(monkeypatch)
def test_cli_doctor_exits_1_on_any_failure_and_0_otherwise()
```

- [ ] **Step 2: Run them to see them fail.** Expected: FAIL, `No module named 'agentflow.doctor'`.
- [ ] **Step 3: Implement `doctor.py` and the command.**
- [ ] **Step 4: Run `test_doctor.py` and `test_portability.py`, then the full suite.**
- [ ] **Step 5: Commit**

```bash
git add agentflow/doctor.py agentflow/flow.py agentflow/tests/test_doctor.py docs/agentflow-playbook.md
git commit -m "Add agentflow doctor: agent CLIs and logins, gh, the remote, Docker, the state dir and each gate's programs"
```

---

### Task 2: Detect a repository's branch, starter, gates and docs from its base branch

**Depends on:** none

**Files:**
- Create: `agentflow/detect.py`
- Test: `agentflow/tests/test_detect.py`

**Interfaces:**
- Consumes: `layers.starter_names()`, `layers.base_layers` (the starter's gates).
- Produces:
  - `@dataclass class Detected` with these fields:
    - `base_branch: str`
    - `base_ref: str | None`: the commit-ish files were read from, or None for an unborn repository, which is read from its working tree
    - `starter: str`
    - `gates: dict[str, dict]` and `gate_sets: dict[str, list[str]]`: empty when the starter's gates stand
    - `gate_source: str`: `"ci"`, `"scripts"` or `"starter"`
    - `docs: dict[str, str]`
    - `skills: list[str]` and `agent_docs: list[str]` (`CLAUDE.md` and `AGENTS.md` if present, printed only)
    - `notes: list[str]`
  - `detect.detect(repo: Path, starter: str | None = None) -> Detected`
  - `detect.default_branch(repo: Path) -> tuple[str, list[str]]`

**Reading files.** Every file is read from `base_ref` with these commands, all run with `git -C <repo>` and root-relative paths:
- `git show <ref>:<path>` to read a file
- `git cat-file -e <ref>:<path>` to test that a path exists
- `git ls-tree --name-only <ref> -- <dir>/` to list a directory

A file over 1 MB isn't read: add a note for it. An unborn repository (`git rev-parse --verify HEAD` fails) is read from its working tree, with a note.

**Rules:**
- **Branch.** The first that holds:
  1. `git symbolic-ref --short refs/remotes/origin/HEAD` without its `origin/` prefix;
  2. a local `main`, then a local `master`;
  3. `git config init.defaultBranch` if that branch exists locally;
  4. `git symbolic-ref --short HEAD`, which also works on an unborn branch, with the note "guessed the base branch from the current branch: check `project.base_branch`";
  5. on a detached HEAD, `main`, with the same note.

  If the branch exists only as `origin/<b>`, add "create it locally: `git branch <b> origin/<b>`".
- **Starter.** The `starter` argument wins. Otherwise the first match wins:
  - `pyproject.toml` → `python-uv`
  - `package.json` → `node-pnpm`
  - `go.mod` → `go`
  - otherwise `docs-only`

  When more than one manifest exists, add "found <a> and <b>; picked <starter>: pass `--starter` to choose". `Cargo.toml` adds "no Rust starter yet: using docs-only".
- **Setup overrides.** These are written as gates, whatever the source of the other gates, so the starter's setup matches the project:
  - `node-pnpm` without `pnpm-lock.yaml` gets `gates.install`:
    - `npm ci` with a `package-lock.json`
    - `yarn install --frozen-lockfile` with a `yarn.lock`
    - `npm install` otherwise
  - `python-uv` with a `poetry.lock` and no `uv.lock` gets `gates.sync: poetry install`.
  - `python-uv` with neither lockfile gets the note "the python-uv starter runs `uv sync`: override `gates.sync` if this project doesn't use uv".
- **CI first.**
  - **Which files.** Workflows are `.github/workflows/*.yml` and `*.yaml`, sorted by name.
  - **Which triggers.** PyYAML parses `on:` as the key `True`, so read `wf.get("on", wf.get(True))`. A workflow counts when that value is a str, a list or a mapping naming `pull_request`, or naming `push` without being limited to tags only. A `push` whose mapping has `tags` or `tags-ignore` and no `branches` or `paths` is tags only.
  - **Shape check, before anything else.** These must hold, or that file, job or step is skipped with a note:
    - the top level is a mapping
    - `jobs` is a mapping, and each job is a mapping
    - `steps` is a list, and each step is a mapping
    - `defaults` and `defaults.run` are mappings
    - `env` values are scalars
    - `run`, `name`, `shell` and `working-directory` are strings (or absent)

    An empty file or invalid YAML skips the file with a note.
  - **Skip with a note** naming `<file>/<job>/<step>`:
    - a step with `uses:`, `if:` or `continue-on-error`, or with a `shell` other than `bash` or `sh`
    - a job with `if:`, `services:`, `container:` or `strategy.matrix`. A matrix job runs its steps per matrix value, and a gate can run them only once.
    - a step whose `run`, effective `working-directory` or effective `env` holds `${{`, `$GITHUB_` or `${GITHUB_`
    - a `run` holding `--write` or `--fix`, which rewrites files
  - **Removing installs.** The install pattern is `^\s*(sudo\s+)?((python[0-9.]*\s+-m\s+)?(pip|pip3)|uv|npm|pnpm|yarn|npx|corepack|poetry|pipenv|bundle|apt-get|apt|brew|go)(\s+(install|i|sync|ci|add|update|enable|get|mod\s+download)\b|\s*$)`, or any segment that pipes into `sh` or `bash`.
    - For a **one-line** `run`, split it into segments with `shlex` punctuation at `&&` and `;`. Drop the install segments and rejoin the rest with ` && `.
    - For a **multi-line** `run` in which any line matches the install pattern, skip the step with the note "mixes install and test commands: add it by hand". Never edit lines inside a script.
    - When nothing remains, skip the step.
  - **What becomes a gate.** The step becomes a gate when what remains of its `run` matches `\b(test|tests|pytest|unittest|jest|vitest|tox|lint|ruff|eslint|flake8|mypy|pyright|tsc|typecheck|check|vet|clippy)\b` (case-insensitive). The step's `name` is used only to name the gate, never to select it.
  - **The command, with GitHub's semantics:**
    - **The shell.** A `run` with no `shell` becomes `bash -e -c <quoted script>`. One with `shell: bash` becomes `bash --noprofile --norc -eo pipefail -c <quoted script>`. One with `shell: sh` becomes `sh -e -c <quoted script>`.
    - **The script.** It is `export CI=true`, one `export K=<quoted V>` line per effective `env` entry (workflow, then job, then step), then the remaining `run`. Quote with `shlex.quote`.
    - **The simple case.** When the `run` is one line, has no effective `env` and no shell syntax (no `|`, `$`, `>`, `<`, backquote, `(`, `;` or `&`), the gate's command is the bare `run`, for readability.
  - **`cwd`** is the step's `working-directory`, else the job's `defaults.run.working-directory`, else the workflow's. It must be relative with no `..`; otherwise skip the step with a note.
  - **The name** is the step's `name` slugged (lower case, runs of non-alphanumerics become `_`, at most 30 characters, stripped of `_`). Else it is the first two words of the `run`, slugged. Else it is `gate`.
  - **Duplicates and collisions.** A step whose `(command, cwd)` equals an earlier gate's is dropped. A repeated name gets `_2`, `_3` and so on. A gate named like a starter gate **replaces** it: it is written whole, so it shows in the pack.
- **Then scripts, only when CI yields no gate:**
  - **`package.json`:** scripts named `test`, `lint`, `typecheck` or `check` become a gate named after the script.
    - The command is `pnpm run <s>` with a `pnpm-lock.yaml`, `yarn <s>` with a `yarn.lock`, and `npm run <s>` otherwise.
    - The `npm init` placeholder (a script containing `no test specified`) is skipped with a note.
  - **`pyproject.toml`:**
    - `[tool.pytest.ini_options]` or a `tests/` dir gives gate `pytest`: `uv run pytest -q`
    - `[tool.ruff]` gives gate `ruff`: `uv run ruff check .`
    - `[tool.mypy]` gives gate `mypy`: `uv run mypy .`
  - **`Makefile`:** a line matching `^(test|lint|check)\s*:` gives a gate named after the target: `make <t>`.
  - **No duplicates of the starter.** A detected command equal to one of the starter's gate commands isn't added, because the starter's gate stands.
- **Gate sets.**
  - `verify` gets every detected gate.
  - `build` gets the gates whose name or command matches `\b(lint|ruff|eslint|flake8|mypy|pyright|tsc|typecheck|vet|clippy)\b`. When none do, it gets `verify`'s list, with the note "no fast gate found: build runs the full verify set".
  - `setup` comes from the starter, so it isn't written, except through the setup overrides above.
- **Docs.**
  - `specs`: the first existing of `specs`, `docs/rfcs` and `docs/proposals`.
  - `plans`: `docs/plans` or `plans`.
  - `decisions`: `docs/adr` or `docs/decisions`.
  - The denylist forbids the string `docs/specs` in engine code. When `ls-tree docs/` lists an entry equal to `"spec" + "s"`, add the note "set `project.docs.specs` to that directory".
- **Skills.** The directories of `.claude/skills` that hold a `SKILL.md`.

- [ ] **Step 1: Write the failing tests**, in tmp git repos. Each case commits its files on `main`, unless the case is about the working tree.

```python
def test_ci_test_and_lint_steps_become_gates_and_install_steps_do_not()
@pytest.mark.parametrize("run,gate", [("npm ci && npm test", "npm test"), ("npm ci; npm test", "npm test"),
                                      ("uv sync && uv run pytest", "uv run pytest")])
def test_an_install_then_test_one_liner_keeps_the_test(run, gate)
def test_a_multiline_run_that_installs_is_a_note()
def test_a_multiline_run_becomes_bash_e_with_its_env_and_ci_true()
def test_explicit_shell_bash_gets_pipefail()
@pytest.mark.parametrize("case", ["if", "continue-on-error", "shell: pwsh", "uses", "matrix job", "services job",
                                  "expr in run", "expr in working-directory", "expr in job env", "$GITHUB_SHA",
                                  "prettier --write", "ruff --fix"])
def test_a_step_that_cannot_run_as_a_gate_is_a_note(case)
def test_a_step_selected_only_by_its_name_is_not_a_gate()   # name "Check python version", run "python --version"
def test_job_and_workflow_default_working_directory_become_cwd()
def test_a_working_directory_outside_the_repo_is_a_note()
@pytest.mark.parametrize("on,counts", [("push", True), ("[push, pull_request]", True), ("{pull_request: {}}", True),
                                       ("{push: {tags: ['v*']}}", False), ("{workflow_dispatch: {}}", False),
                                       ("{schedule: [{cron: '0 0 * * *'}]}", False)])
def test_only_ci_triggers_count(on, counts)
@pytest.mark.parametrize("content", ["", "- a list", "jobs: 3", "jobs: {a: 3}", "jobs: {a: {steps: null}}",
                                     "jobs: {a: {steps: [3]}}", "jobs: {a: {defaults: 3, steps: []}}",
                                     "jobs: {a: {steps: [{name: yes, run: pytest}]}}", "on: 3", "{{{"])
def test_a_malformed_workflow_is_a_note_not_a_crash(content)
def test_duplicates_collapse_and_names_get_suffixes()
def test_a_gate_named_like_a_starter_gate_replaces_it()
def test_ci_wins_over_package_scripts()
@pytest.mark.parametrize("lock,cmd,install", [("pnpm-lock.yaml", "pnpm run test", None),
                                              ("yarn.lock", "yarn test", "yarn install --frozen-lockfile"),
                                              ("package-lock.json", "npm run test", "npm ci"),
                                              (None, "npm run test", "npm install")])
def test_package_scripts_use_the_lockfiles_manager_and_setup(lock, cmd, install)
def test_the_npm_placeholder_test_script_is_skipped()
def test_pyproject_tool_sections_become_named_gates()
def test_a_poetry_project_gets_a_poetry_sync()
def test_makefile_targets_become_gates()
def test_a_detected_command_equal_to_the_starters_is_not_added()
def test_no_gate_found_keeps_the_starters()
def test_build_set_takes_the_lint_gates()
@pytest.mark.parametrize("files,starter", [(["pyproject.toml"], "python-uv"), (["package.json"], "node-pnpm"),
                                           (["go.mod"], "go"), ([], "docs-only"), (["Cargo.toml"], "docs-only")])
def test_starter_is_picked_from_the_manifest(files, starter)
def test_two_manifests_are_a_note()
def test_default_branch_from_origin_head()
def test_default_branch_prefers_a_local_main_over_the_current_branch()
def test_default_branch_on_a_lone_feature_branch_or_detached_head_is_a_note()
def test_detection_reads_the_base_branch_not_the_working_tree()      # a lint step only on dirty `wip` → not detected
def test_files_in_subdirectories_are_read_from_the_ref()             # .github/workflows/ci.yml via git show
def test_an_unborn_repo_is_read_from_its_working_tree_with_a_note()
def test_docs_skills_and_agent_docs_are_found()
```

- [ ] **Step 2: Run them to see them fail.** Expected: FAIL, `No module named 'agentflow.detect'`.
- [ ] **Step 3: Implement `detect.py`.** Use `tomllib`, `yaml.safe_load` and `json`, and catch each file's parse error into `notes`.
- [ ] **Step 4: Run `test_detect.py` and `test_portability.py`.**
- [ ] **Step 5: Commit**

```bash
git add agentflow/detect.py agentflow/tests/test_detect.py
git commit -m "Detect a repository's base branch, starter, gates, docs and skills from its base branch for agentflow init"
```

---

### Task 3: `agentflow init`, with an opt-in gate baseline

**Depends on:** Task 1, Task 2

**Files:**
- Create: `agentflow/init.py`
- Modify:
  - `agentflow/flow.py` (the `init` command, which runs with no pack present)
  - `agentflow/gates.py` (`run_gates` gains `env_base`)
  - `agentflow/safeio.py` (`write_new`)
- Test: `agentflow/tests/test_init.py`, `agentflow/tests/test_gates.py`, `agentflow/tests/test_safeio.py`

**Interfaces:**
- Consumes:
  - `detect.detect`
  - `layers.base_layers` and `layers.merge` (what "differs from the defaults" is compared against)
  - `doctor.run_checks`, `doctor.render` and `doctor.gate_programs`
  - `gates.run_gates`
  - `config.load_config`
- Produces:
  - `gates.run_gates(cfg, names, cwd, log_dir, *, env_base: Mapping[str, str] | None = None)`. Each gate's environment is `(env_base if env_base is not None else os.environ) | defaults.env`, and the default keeps today's behaviour.
  - `safeio.write_new(path: Path, text: str, mode: int = 0o644) -> Path`: `O_WRONLY|O_CREAT|O_EXCL|O_NOFOLLOW`, refusing a symlinked parent like `write_text`, and `FileExistsError` when anything is already at `path`.
  - `init.render_pack(d: Detected, *, base: dict, red: frozenset[str] = frozenset()) -> str`
  - `@dataclass class BaselineRow: gate: str; command: str; status: Literal["pass", "fail", "error", "skipped", "not run"]; seconds: float; note: str = ""`
  - `init.baseline(cfg: Config, repo: Path, base_ref: str, missing: set[str], out: TextIO) -> list[BaselineRow]`
  - `init.init(repo: Path, *, starter: str | None, run_baseline: bool, out: TextIO) -> int`

**Behaviour:**
- **When `init` refuses:**
  - when anything is at `.agentflow/config.yaml`, including a broken symlink (`os.path.lexists`): "this repository already has a pack: edit it, or remove .agentflow to start over"
  - when `.agentflow` is a symlink
  - when `--starter` names no starter

  `init` finds the repository with `_find_repo()` and never calls `_pack_config()`. In a linked worktree it prints which checkout it writes into.
- **`render_pack`.** The YAML body comes from `yaml.safe_dump(sort_keys=False)`, with a representer that writes multi-line strings in literal block style (`|`). Comments are whole lines placed between separately dumped top-level blocks. A test checks that the text loads back to exactly the data. The blocks, in order:
  1. A header comment.
  2. `version: 2`, `engine: ">=2,<3"`, `starter` and `gates_confirmed: false`.
  3. `agents: {claude: {permission_mode: bypassPermissions}}`, preceded by the comment "runs are headless: this lets the agent use its tools; deny rules, hooks and agentflow's guards still bind. Without it, each gate and the bug lane's test command need a Bash allow rule in .claude/settings.json (validate lists them)."
  4. `project`, holding only what differs from `base`: `base_branch`, `fork_from` and the found `docs.*`.
  5. `gates`: the detected gates and the setup overrides, each written whole.
  6. `gate_sets`, when detected.
- **What `render_pack` drops.** Any string value starting with `@` (in `run`, `cwd` or `docs`) would be read as a config reference, so that gate or doc is dropped with a note.
- **Red gates.** A red gate is written whole (`run`, `cwd`, `baseline: red`), even when it comes from the starter. When any gate is red, all three `gate_sets` are written whole, without the red gates. If that would empty `verify`, the red gates stay in it, and `init` says "every verify gate fails on `<base>`: runs cannot pass verify until you fix them".
- **`baseline`**, only with `--baseline`:
  - **When it doesn't run.** With no commits, or a `base_ref` that doesn't exist locally, it doesn't run, and `init` says why.
  - **Before:** clean up `init`'s own leftovers only. For each `git worktree list --porcelain` entry under `<state_dir>/baseline-*`, run `git worktree remove --force` and remove the directory.
    - When git refuses because the directory is already gone, run `git worktree prune` and say so: it prunes every missing worktree.
    - Never run a global prune otherwise.
  - **The worktree:** `tempfile.mkdtemp(prefix="baseline-", dir=<state_dir>)`, then `git worktree add --detach <dir> <base_ref>`.
  - **The environment:** `env_base` is `os.environ` minus `VIRTUAL_ENV`, `CONDA_PREFIX` and `PYTHONPATH`.
  - **Before running gates,** print every command that will run, once.
  - **Setup:** run each `gate_sets.setup` gate on its own call, in order. The first failure stops the baseline: that row is `error` ("setup failed"), and every remaining row is `not run`.
  - **Then** each gate of `build ∪ verify`, once, with logs in `<state_dir>/baseline-logs/`.
  - **What each result becomes:**
    - `skipped` from `when_exists` stays `skipped`.
    - Exit 126 or 127, a timeout, or a missing `cwd` is `error`.
    - A gate with a program in `missing` (doctor's failed `gate:` checks) is `not run`.
    - Only another non-zero exit is `fail`.
    - **Only `fail` is red.**
  - **The footprint:** before and after, record `git config --local --list` and the file list of `$(git rev-parse --git-common-dir)/hooks`, and print any change.
  - **Signals:** while the baseline runs, SIGTERM and SIGHUP raise `KeyboardInterrupt`, so the `finally` runs.
  - **In the `finally`:** `git worktree remove --force <dir>`, then remove `<dir>` if it is still there.
- **Order.**
  1. Detect.
  2. Run doctor. Doctor before the baseline gives the baseline its `missing` set.
  3. With `--baseline`, write a provisional pack to `<state_dir>/init/config.yaml`, load it with `load_config`, and run the baseline. Then render the final pack with the red gates, and write `.agentflow/config.yaml` **once** with `safeio.write_new`. An interrupted baseline leaves no pack behind, and a pack that appeared meanwhile is never overwritten.
  4. Print the detection summary with every note, the doctor table, and the baseline table (gate, command, status, seconds, note) or "baseline not run: pass --baseline to run the detected gates once on `<base>`".
  5. When the bug lane is on, print a narrow `Bash(...)` allow rule to add, built from the first verify gate's program.
  6. Print the next steps: review the gates, set `gates_confirmed: true`, then `agentflow validate`, `agentflow explain feature --module <id>` and `agentflow run … --dry-run`.
- **Exit codes.** `init` exits 0 when it wrote the pack, whatever doctor or the baseline found. Its own errors exit 1 with a message that names `init`, never the run-error message from `main()`'s catch-all.

- [ ] **Step 1: Write the failing tests**

```python
# conftest-style fixtures in test_init.py: `toy_repo` = a tmp git repo on `main` with one commit pushed to a bare
# `origin` (no `git remote set-head`), with pyproject.toml and a CI workflow;
# git_status(repo) = `git status --porcelain --untracked-files=all`
def test_init_writes_only_the_pack(toy_repo)                      # "?? .agentflow/config.yaml"; rm -rf → clean
def test_init_runs_no_gate_without_the_baseline_flag(toy_repo)    # a gate that would create a marker: no marker
def test_the_written_pack_loads_back_to_the_rendered_data(toy_repo)
def test_the_pack_has_the_permission_line_with_its_comment(toy_repo)
def test_a_value_that_looks_like_a_reference_is_dropped(toy_repo) # run "@x", working-directory "@scope/web"
def test_init_refuses_an_existing_pack_a_broken_symlink_and_a_symlinked_dir(toy_repo)
def test_the_starter_flag_overrides_detection(toy_repo)
def test_an_unknown_starter_flag_is_refused(toy_repo)
def test_baseline_marks_a_failing_gate_red_and_drops_it_from_every_set(toy_repo)
def test_baseline_marks_a_failing_starter_gate_red_with_its_run(toy_repo)
def test_baseline_keeps_red_gates_in_verify_when_nothing_else_is_left(toy_repo)
def test_a_failing_setup_is_an_error_and_reddens_nothing(toy_repo)
@pytest.mark.parametrize("run", ["exit 127", "sleep 5"])           # with a tiny timeout for the second
def test_an_environment_failure_is_an_error_not_red(toy_repo, run)
def test_a_gate_with_a_missing_program_is_not_run(toy_repo)
def test_a_skipped_gate_is_not_red(toy_repo)
def test_baseline_runs_on_the_base_branch_not_the_working_tree(toy_repo)
def test_baseline_runs_without_the_callers_virtualenv(toy_repo, monkeypatch)
def test_baseline_leaves_no_worktree_even_when_a_gate_times_out(toy_repo)
def test_baseline_cleans_up_its_own_leftover_and_keeps_other_worktrees(toy_repo)
def test_an_interrupted_baseline_writes_no_pack(toy_repo, monkeypatch)
def test_a_pack_created_during_the_baseline_is_not_overwritten(toy_repo, monkeypatch)
def test_init_reports_changed_git_config_and_hooks(toy_repo)       # a setup gate runs `git config core.hooksPath x`
def test_init_on_an_unborn_repo_writes_a_pack_and_skips_the_baseline(tmp_path)
def test_init_prints_a_bash_allow_rule_for_the_bug_lane(toy_repo)
# test_gates.py
def test_run_gates_env_base_replaces_the_inherited_environment(...)
# test_safeio.py
def test_write_new_refuses_an_existing_file_and_a_symlink(...)
```

- [ ] **Step 2: Run them to see them fail.** Expected: FAIL, `No module named 'agentflow.init'`.
- [ ] **Step 3: Implement `init.py`, `agentflow init [--starter NAME] [--baseline]`, `env_base` and `write_new`.**
- [ ] **Step 4: Run the three test files and `test_portability.py`, then the full suite.**
- [ ] **Step 5: Commit**

```bash
git add agentflow/init.py agentflow/flow.py agentflow/gates.py agentflow/safeio.py agentflow/tests
git commit -m "Add agentflow init: detect, write a sparse pack, and run the gates once on the base branch only when asked"
```

---

### Task 4: The plug-and-play end-to-end test, the security overlay and the docs

**Depends on:** Task 3

**Files:**
- Create: `agentflow/tests/test_plug_and_play.py`, `agentflow/tests/fixtures/repos/<kind>/`. There are three toy repositories: `python` (with a GitHub Actions workflow), `node` and `docs`, where `docs` has no skills dir.
- Modify:
  - `.agentflow/config.yaml` (`overlays.security.when_paths` += `agentflow/layers.py`, `agentflow/detect.py`, `agentflow/init.py`)
  - `docs/agentflow-playbook.md` (§13 rewritten around `init`; §10)
  - `CLAUDE.md` (Commands and Engine architecture: `layers.py`, `detect.py`, `init.py`, `doctor.py`, `defaults/`, `starters/`, `skills/`, and "a step's skill is its brief")
  - `docs/agentflow-harness-proposal.md` (§1c: both plans' deviations; §5: mark slice PnP done with both plans' paths)

**What the test sets up itself:**
- A tmp `bin/` first on `PATH` with:
  - `claude`: an `sh` wrapper that answers `auth status` with exit 0 and otherwise runs `sys.executable agentflow/tests/fake_agent.py "$@"`
  - `gh`: a copy of `tests/bin/gh`
  - `pnpm` and `uv`: fakes that exit 0
- No real `pnpm`, `uv` or network is needed.
- Each fixture is copied into a tmp git repo, committed on `main` and pushed to a bare `origin`, without `set-head`.

- [ ] **Step 1: Write the failing tests**

```python
@pytest.mark.parametrize("kind", ["python", "node", "docs"])
def test_init_to_dry_run(kind, tmp_path):
    # init → doctor → set gates_confirmed: true → validate → explain feature --module greet → run feature --module greet --dry-run
    assert detected gate commands == EXPECTED[kind]   # python: "python3 -m unittest -q" (CI); node: "pnpm run test" (scripts); docs: the starter's
    assert "0 warnings" in validate_out and dry_run.returncode == 0
    assert git_status(repo) == "?? .agentflow/config.yaml"
    shutil.rmtree(repo / ".agentflow"); assert git_status(repo) == ""
def test_init_then_validate_for_a_starter_in_an_empty_repo(tmp_path)
    # `init --starter python-uv`: validate shows exactly the gates_confirmed warning, then 0 warnings once it is true
def test_security_overlay_covers_the_new_modules()
```

- [ ] **Step 2: Run them to see them fail.**
- [ ] **Step 3: Make them pass.** These tests are where both plans meet. A failure here is a defect in the task that owns that behaviour, so fix it there, not in a fixture.
- [ ] **Step 4: Rewrite the docs.**
  - Playbook §13 becomes "Using agentflow in another repository": `uv tool install`, `agentflow init [--starter …] [--baseline]`, read the notes and the gate table, add the printed Bash rule if you use the bug lane, set `gates_confirmed: true`, then `doctor`, `validate`, `explain` and `--dry-run`.
  - Add `init`, `doctor` and `config show --effective` to §10.
  - Update CLAUDE.md.
  - In the proposal, record the deviations of both plans, and "time to first dry run: not measured".
  - In proposal §5, record "Slice PnP (done, <date>; `docs/plans/2026-11-05-agentflow-harness-slice-pnp-1.md`, `docs/plans/2026-11-06-agentflow-harness-slice-pnp-2.md`)".
- [ ] **Step 5: Run `test_plug_and_play.py` and `bin/agentflow validate`, then the full suite.** Expected: all pass, with "0 warnings".
- [ ] **Step 6: Commit**

```bash
git add agentflow/tests .agentflow/config.yaml docs/agentflow-playbook.md docs/agentflow-harness-proposal.md CLAUDE.md
git commit -m "Prove plug and play end to end, cover the new modules with the security overlay, and document init"
```
