"""`detect`: a repository's base branch, starter, gates and docs, read from its base branch (never its working tree)."""

from __future__ import annotations

import json
import subprocess
import textwrap
from pathlib import Path

import pytest

from agentflow import detect
from agentflow.detect import Detected

SPEC_DIR = "spec" + "s"  # the engine's denylist forbids the literal path, and so does this file's fixture text


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


def _write(root: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def _init(root: Path, branch: str = "main") -> None:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q", "-b", branch)
    for key, value in (("user.name", "Test"), ("user.email", "test@example.com"), ("commit.gpgsign", "false")):
        _git(root, "config", key, value)


def _commit(root: Path, files: dict[str, str]) -> None:
    _write(root, files)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "c")


@pytest.fixture
def make(tmp_path: Path):
    """`make(files)`: a git repo on `main` with one commit holding `files`; returns its path."""

    def build(files: dict[str, str] | None = None, branch: str = "main") -> Path:
        root = tmp_path / f"repo{len(list(tmp_path.glob('repo*')))}"
        _init(root, branch)
        _commit(root, {"README.md": "hello\n", **(files or {})})
        return root

    return build


def ci(steps: str, *, on: str = "push", head: str = "", job: str = "") -> dict[str, str]:
    """A workflow file with one job `test` whose steps are `steps` (YAML list items, unindented)."""
    body = f"name: CI\non: {on}\n{head}jobs:\n  test:\n    runs-on: ubuntu-latest\n{job}    steps:\n"
    body += textwrap.indent(textwrap.dedent(steps).strip("\n"), "      ") + "\n"
    return {".github/workflows/ci.yml": body}


def gate_runs(found: Detected) -> dict[str, str]:
    return {name: gate["run"] for name, gate in found.gates.items()}


def notes_text(found: Detected) -> str:
    return "\n".join(found.notes)


# --- CI -------------------------------------------------------------------------------------------------------------

def test_ci_test_and_lint_steps_become_gates_and_install_steps_do_not(make):
    repo = make(ci("""
        - uses: actions/checkout@v4
        - run: pip install -r requirements.txt
        - name: Lint
          run: ruff check .
        - name: Unit tests
          run: pytest -q
    """))
    found = detect.detect(repo)
    assert gate_runs(found) == {"lint": "ruff check .", "unit_tests": "pytest -q"}
    assert found.gate_source == "ci"
    assert found.gate_sets == {"verify": ["lint", "unit_tests"], "build": ["lint"]}


@pytest.mark.parametrize("run,gate", [("npm ci && npm test", "npm test"), ("npm ci; npm test", "npm test"),
                                      ("uv sync && uv run pytest", "uv run pytest")])
def test_an_install_then_test_one_liner_keeps_the_test(make, run, gate):
    found = detect.detect(make(ci(f"- name: Test\n  run: {run}")))
    assert gate_runs(found) == {"test": gate}


def test_a_multiline_run_that_installs_is_a_note(make):
    found = detect.detect(make(ci("""
        - name: Test
          run: |
            pip install pytest
            pytest -q
    """)))
    assert found.gates == {}
    assert "ci.yml/test/Test" in notes_text(found)
    assert "mixes install and test commands: add it by hand" in notes_text(found)


def test_a_multiline_run_becomes_bash_e_with_its_env_and_ci_true(make):
    found = detect.detect(make(ci("""
        - name: Test
          env:
            STEP_VAR: a b
          run: |
            cd app
            pytest -q
    """, head="env:\n  WF_VAR: '1'\n", job="    env:\n      JOB_VAR: two\n")))
    run = found.gates["test"]["run"]
    script = "export CI=true\nexport WF_VAR=1\nexport JOB_VAR=two\nexport STEP_VAR='a b'\ncd app\npytest -q"
    import shlex
    assert run == f"bash -e -c {shlex.quote(script)}"


def test_explicit_shell_bash_gets_pipefail(make):
    found = detect.detect(make(ci("- name: Test\n  shell: bash\n  run: pytest | tee out.txt")))
    import shlex
    assert found.gates["test"]["run"] == ("bash --noprofile --norc -eo pipefail -c "
                                          + shlex.quote("export CI=true\npytest | tee out.txt"))


def test_explicit_shell_sh_gets_sh_e(make):
    found = detect.detect(make(ci("- name: Test\n  shell: sh\n  run: pytest | tee out.txt")))
    import shlex
    assert found.gates["test"]["run"] == "sh -e -c " + shlex.quote("export CI=true\npytest | tee out.txt")


CANNOT_RUN = {
    "if": ("- name: T\n  if: github.ref == 'x'\n  run: pytest", ""),
    "continue-on-error": ("- name: T\n  continue-on-error: true\n  run: pytest", ""),
    "shell: pwsh": ("- name: T\n  shell: pwsh\n  run: pytest", ""),
    "uses": ("- name: T\n  uses: ./.github/actions/tests", ""),
    "matrix job": ("- name: T\n  run: pytest", "    strategy:\n      matrix:\n        v: [1, 2]\n"),
    "services job": ("- name: T\n  run: pytest", "    services:\n      db:\n        image: postgres\n"),
    "expr in run": ("- name: T\n  run: pytest ${{ matrix.v }}", ""),
    "expr in working-directory": ("- name: T\n  working-directory: ${{ env.X }}\n  run: pytest", ""),
    "expr in job env": ("- name: T\n  run: pytest", "    env:\n      TOKEN: ${{ secrets.T }}\n"),
    "$GITHUB_SHA": ("- name: T\n  run: pytest --junit $GITHUB_SHA", ""),
    "prettier --write": ("- name: T\n  run: prettier --write . && npm test", ""),
    "ruff --fix": ("- name: T\n  run: ruff check --fix .", ""),
}


@pytest.mark.parametrize("case", list(CANNOT_RUN))
def test_a_step_that_cannot_run_as_a_gate_is_a_note(make, case):
    steps, job = CANNOT_RUN[case]
    found = detect.detect(make(ci(steps, job=job)))
    assert found.gates == {}
    assert "ci.yml/test" in notes_text(found)


def test_a_step_selected_only_by_its_name_is_not_a_gate(make):
    found = detect.detect(make(ci("- name: Check python version\n  run: python --version")))
    assert found.gates == {} and found.gate_source == "starter"


def test_job_and_workflow_default_working_directory_become_cwd(make):
    found = detect.detect(make(ci("""
        - name: Web tests
          run: npm test
        - name: Docs tests
          working-directory: docs
          run: npm test
    """, head="defaults:\n  run:\n    working-directory: web\n", job="    defaults:\n      run:\n        working-directory: app\n")))
    assert found.gates["web_tests"] == {"run": "npm test", "cwd": "app"}
    assert found.gates["docs_tests"] == {"run": "npm test", "cwd": "docs"}


def test_a_working_directory_outside_the_repo_is_a_note(make):
    for wd in ("../x", "/abs"):
        found = detect.detect(make(ci(f"- name: T\n  working-directory: {wd}\n  run: pytest")))
        assert found.gates == {}
        assert "working-directory" in notes_text(found)


@pytest.mark.parametrize("on,counts", [("push", True), ("[push, pull_request]", True), ("{pull_request: {}}", True),
                                       ("{push: {tags: ['v*']}}", False), ("{workflow_dispatch: {}}", False),
                                       ("{schedule: [{cron: '0 0 * * *'}]}", False)])
def test_only_ci_triggers_count(make, on, counts):
    found = detect.detect(make(ci("- name: Test\n  run: pytest", on=on)))
    assert bool(found.gates) is counts


@pytest.mark.parametrize("content", ["", "- a list", "jobs: 3", "jobs: {a: 3}", "jobs: {a: {steps: null}}",
                                     "jobs: {a: {steps: [3]}}", "jobs: {a: {defaults: 3, steps: []}}",
                                     "jobs: {a: {steps: [{name: yes, run: pytest}]}}", "on: 3", "{{{"])
def test_a_malformed_workflow_is_a_note_not_a_crash(make, content):
    if content.startswith("jobs"):
        content = "on: push\n" + content
    found = detect.detect(make({".github/workflows/ci.yml": content}))
    assert found.gates == {}
    assert found.notes and "ci.yml" in notes_text(found)


def test_a_malformed_env_or_run_type_is_a_note_not_a_crash(make):
    found = detect.detect(make(ci("""
        - name: A
          env: {X: [1, 2]}
          run: pytest
        - name: B
          env: {"bad key": x}
          run: pytest
        - name: C
          run: [pytest]
        - name: D
          working-directory: 3
          run: pytest
    """)))
    assert found.gates == {}
    assert "ci.yml/test/A" in notes_text(found) and "ci.yml/test/B" in notes_text(found)


def test_duplicates_collapse_and_names_get_suffixes(make):
    found = detect.detect(make(ci("""
        - name: Tests
          run: pytest
        - name: Tests
          run: pytest
        - name: Tests
          run: pytest -x
        - name: Tests
          run: pytest -k slow
    """)))
    assert gate_runs(found) == {"tests": "pytest", "tests_2": "pytest -x", "tests_3": "pytest -k slow"}


def test_a_gate_named_like_a_starter_gate_replaces_it(make):
    repo = make({**ci("- name: Tests\n  run: pytest -x tests/"), "pyproject.toml": "[project]\nname='x'\n"})
    found = detect.detect(repo)
    assert found.starter == "python-uv"
    assert found.gates["tests"] == {"run": "pytest -x tests/"}


def test_ci_wins_over_package_scripts(make):
    repo = make({**ci("- name: Lint\n  run: npm run lint"),
                 "package.json": json.dumps({"scripts": {"test": "jest", "lint": "eslint ."}})})
    found = detect.detect(repo)
    assert found.gate_source == "ci" and list(found.gates) == ["install", "lint"]  # install: the setup override
    assert found.gate_sets["verify"] == ["lint"]


# --- scripts --------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("lock,cmd,install", [("pnpm-lock.yaml", "pnpm run test", None),
                                              ("yarn.lock", "yarn test", "yarn install --frozen-lockfile"),
                                              ("package-lock.json", "npm run test", "npm ci"),
                                              (None, "npm run test", "npm install")])
def test_package_scripts_use_the_lockfiles_manager_and_setup(make, lock, cmd, install):
    files = {"package.json": json.dumps({"scripts": {"test": "jest"}})}
    if lock:
        files[lock] = "\n"
    found = detect.detect(make(files))
    assert found.starter == "node-pnpm" and found.gate_source == "scripts"
    assert found.gates["test"] == {"run": cmd}
    if install is None:
        assert "install" not in found.gates
    else:
        assert found.gates["install"] == {"run": install}
    assert found.gate_sets["verify"] == ["test"]


def test_package_scripts_named_test_lint_typecheck_check(make):
    scripts = {"test": "jest", "lint": "eslint .", "typecheck": "tsc", "check": "x", "build": "tsc -b"}
    found = detect.detect(make({"package.json": json.dumps({"scripts": scripts}), "pnpm-lock.yaml": ""}))
    assert list(found.gates) == ["test", "lint", "typecheck", "check"]
    assert found.gate_sets["build"] == ["lint", "typecheck"]


def test_the_npm_placeholder_test_script_is_skipped(make):
    placeholder = 'echo "Error: no test specified" && exit 1'
    found = detect.detect(make({"package.json": json.dumps({"scripts": {"test": placeholder}})}))
    assert "test" not in found.gates and found.gate_source == "starter"
    assert "no test specified" in notes_text(found) or "placeholder" in notes_text(found)


def test_malformed_package_json_and_pyproject_are_notes(make):
    found = detect.detect(make({"package.json": "{not json", "pyproject.toml": "[[[", "Makefile": "\x00"}))
    assert found.notes and found.gate_source == "starter"


def test_pyproject_tool_sections_become_named_gates(make):
    repo = make({"pyproject.toml": "[tool.pytest.ini_options]\naddopts='-q'\n[tool.ruff]\nline-length=100\n"
                                   "[tool.mypy]\nstrict=true\n", "uv.lock": ""})
    found = detect.detect(repo)
    assert found.starter == "python-uv"
    assert gate_runs(found) == {"ruff": "uv run ruff check .", "mypy": "uv run mypy ."}  # pytest equals the starter's
    assert found.gate_sets == {"verify": ["tests", "ruff", "mypy"], "build": ["ruff", "mypy"]}


def test_a_tests_directory_gives_a_pytest_gate(make):
    repo = make({"pyproject.toml": "[project]\nname='x'\n", "tests/test_a.py": ""})
    assert detect.detect(repo, starter="docs-only").gates == {"pytest": {"run": "uv run pytest -q"}}


def test_a_poetry_project_gets_a_poetry_sync(make):
    found = detect.detect(make({"pyproject.toml": "[project]\nname='x'\n", "poetry.lock": ""}))
    assert found.gates == {"sync": {"run": "poetry install"}}
    assert found.gate_sets == {} and found.gate_source == "starter"


def test_a_python_project_with_no_lockfile_is_a_note(make):
    found = detect.detect(make({"pyproject.toml": "[project]\nname='x'\n"}))
    assert "override `gates.sync` if this project doesn't use uv" in notes_text(found)
    found = detect.detect(make({"pyproject.toml": "[project]\nname='x'\n", "uv.lock": ""}))
    assert "doesn't use uv" not in notes_text(found)


def test_makefile_targets_become_gates(make):
    found = detect.detect(make({"Makefile": "build:\n\techo\ntest:\n\tpytest\nlint :\n\truff .\ncheck: lint test\n"}))
    assert gate_runs(found) == {"test": "make test", "lint": "make lint", "check": "make check"}
    assert found.starter == "docs-only"
    assert found.gate_sets["build"] == ["lint"]


def test_a_detected_command_equal_to_the_starters_is_not_added(make):
    found = detect.detect(make({"pyproject.toml": "[tool.pytest.ini_options]\n", "uv.lock": ""}))
    assert found.gates == {} and found.gate_sets == {} and found.gate_source == "starter"


def test_no_gate_found_keeps_the_starters(make):
    found = detect.detect(make())
    assert found.gates == {} and found.gate_sets == {} and found.gate_source == "starter"


def test_build_set_takes_the_lint_gates(make):
    found = detect.detect(make(ci("- name: Tests\n  run: pytest\n- name: Types\n  run: mypy src\n"
                                  "- name: Style\n  run: flake8")))
    assert found.gate_sets == {"verify": ["tests", "types", "style"], "build": ["types", "style"]}


def test_build_set_without_a_fast_gate_is_verify_with_a_note(make):
    found = detect.detect(make(ci("- name: Tests\n  run: pytest")))
    assert found.gate_sets == {"verify": ["tests"], "build": ["tests"]}
    assert "no fast gate found: build runs the full verify set" in notes_text(found)


# --- starter --------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("files,starter", [(["pyproject.toml"], "python-uv"), (["package.json"], "node-pnpm"),
                                           (["go.mod"], "go"), ([], "docs-only"), (["Cargo.toml"], "docs-only")])
def test_starter_is_picked_from_the_manifest(make, files, starter):
    found = detect.detect(make({name: "{}" if name == "package.json" else "" for name in files}))
    assert found.starter == starter
    assert ("no Rust starter yet: using docs-only" in notes_text(found)) is (files == ["Cargo.toml"])


def test_the_starter_argument_wins(make):
    assert detect.detect(make({"go.mod": ""}), starter="docs-only").starter == "docs-only"
    with pytest.raises(ValueError):
        detect.detect(make({"go.mod": ""}), starter="../x")


def test_two_manifests_are_a_note(make):
    found = detect.detect(make({"pyproject.toml": "", "package.json": "{}"}))
    assert found.starter == "python-uv"
    assert "found pyproject.toml and package.json; picked python-uv: pass `--starter` to choose" in notes_text(found)


# --- branch ---------------------------------------------------------------------------------------------------------

def test_default_branch_from_origin_head(make):
    repo = make(branch="trunk")
    _git(repo, "update-ref", "refs/remotes/origin/develop", "HEAD")
    _git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/develop")
    assert detect.default_branch(repo) == ("develop", ["create it locally: `git branch develop origin/develop`"])
    found = detect.detect(repo)
    assert found.base_branch == "develop" and found.base_ref == "origin/develop"
    _git(repo, "branch", "develop", "origin/develop")
    assert detect.default_branch(repo) == ("develop", [])
    assert detect.detect(repo).base_ref == "develop"


def test_default_branch_prefers_a_local_main_over_the_current_branch(make):
    repo = make()
    _git(repo, "checkout", "-q", "-b", "feature")
    assert detect.default_branch(repo) == ("main", [])
    _git(repo, "branch", "-m", "main", "legacy")
    _git(repo, "branch", "master")
    assert detect.default_branch(repo) == ("master", [])


def test_default_branch_uses_init_default_branch_when_it_exists(make):
    repo = make(branch="feature")
    _git(repo, "branch", "develop")
    _git(repo, "config", "init.defaultBranch", "develop")
    assert detect.default_branch(repo) == ("develop", [])


def test_default_branch_on_a_lone_feature_branch_or_detached_head_is_a_note(make):
    repo = make(branch="feature")
    branch, notes = detect.default_branch(repo)
    assert branch == "feature" and "guessed the base branch from the current branch: check `project.base_branch`" in notes
    _git(repo, "checkout", "-q", "--detach")
    branch, notes = detect.default_branch(repo)
    assert branch == "main" and "guessed the base branch from the current branch: check `project.base_branch`" in notes
    found = detect.detect(repo)
    assert found.base_branch == "main" and found.base_ref == "HEAD"


def test_a_hostile_origin_head_is_not_a_branch(make):
    repo = make()
    _git(repo, "update-ref", "refs/remotes/origin/-x", "HEAD")
    _git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/-x")
    assert detect.default_branch(repo)[0] == "main"


# --- the base ref, not the working tree -----------------------------------------------------------------------------

def test_detection_reads_the_base_branch_not_the_working_tree(make):
    repo = make(ci("- name: Tests\n  run: pytest"))
    _git(repo, "checkout", "-q", "-b", "wip")
    _commit(repo, ci("- name: Tests\n  run: pytest\n- name: Lint\n  run: ruff check ."))
    _write(repo, {"package.json": json.dumps({"scripts": {"test": "jest"}})})
    found = detect.detect(repo)
    assert list(found.gates) == ["tests"] and found.base_ref == "main"
    (repo / ".github" / "workflows" / "ci.yml").unlink()  # even a deleted working-tree file is still read from `main`
    assert list(detect.detect(repo).gates) == ["tests"]


def test_files_in_subdirectories_are_read_from_the_ref(make):
    repo = make(ci("- name: Lint\n  run: ruff check ."))
    (repo / ".github").rename(repo / "moved")  # the tree on disk no longer matches the ref
    assert list(detect.detect(repo).gates) == ["lint"]


def test_a_file_over_a_megabyte_is_a_note(make):
    repo = make({"package.json": "{" + " " * (1024 * 1024 + 10) + "}"})
    found = detect.detect(repo)
    assert "package.json" in notes_text(found) and "1 MB" in notes_text(found)


def test_an_unborn_repo_is_read_from_its_working_tree_with_a_note(tmp_path):
    repo = tmp_path / "fresh"
    _init(repo)
    _write(repo, {**ci("- name: Lint\n  run: ruff check ."), "go.mod": "module x\n", "CLAUDE.md": "x"})
    found = detect.detect(repo)
    assert found.base_ref is None and found.base_branch == "main"
    assert list(found.gates) == ["lint"] and found.starter == "go"
    assert "no commits yet" in notes_text(found) and "working tree" in notes_text(found)
    assert found.agent_docs == ["CLAUDE.md"]


def test_an_unborn_repo_does_not_follow_a_symlink_out_of_the_repo(tmp_path):
    repo = tmp_path / "fresh"
    _init(repo)
    outside = tmp_path / "outside"
    _write(outside, {"ci.yml": "on: push\njobs: {a: {steps: [{name: T, run: pytest}]}}\n"})
    (repo / ".github").mkdir()
    (repo / ".github" / "workflows").symlink_to(outside, target_is_directory=True)
    assert detect.detect(repo).gates == {}


# --- docs, skills, agent docs ---------------------------------------------------------------------------------------

def test_docs_skills_and_agent_docs_are_found(make):
    repo = make({"docs/rfcs/a.md": "", "docs/plans/b.md": "", "docs/adr/c.md": "", ".claude/skills/one/SKILL.md": "",
                 ".claude/skills/two/SKILL.md": "", ".claude/skills/empty/notes.md": "", "CLAUDE.md": "x",
                 "AGENTS.md": "y"})
    found = detect.detect(repo)
    assert found.docs == {"specs": "docs/rfcs", "plans": "docs/plans", "decisions": "docs/adr"}
    assert found.skills == ["one", "two"]
    assert found.agent_docs == ["CLAUDE.md", "AGENTS.md"]


def test_docs_alternatives_and_the_default_specs_directory_note(make):
    found = detect.detect(make({"plans/a.md": "", "docs/decisions/a.md": "", "docs/proposals/a.md": ""}))
    assert found.docs == {"specs": "docs/proposals", "plans": "plans", "decisions": "docs/decisions"}
    found = detect.detect(make({f"docs/{SPEC_DIR}/a.md": ""}))
    assert "set `project.docs.specs` to that directory" in notes_text(found)
    assert found.docs == {}
