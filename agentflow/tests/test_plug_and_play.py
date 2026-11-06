"""Plug and play, end to end: `agentflow init` in a repository that has never seen agentflow, then `doctor`,
`validate`, `explain` and a dry run, with nothing written to the repository but `.agentflow/config.yaml`.

Each toy repository under `fixtures/repos/` is copied into a tmp git repo on `main`, pushed to a bare `origin` (with
no `remote set-head`, so the base branch is found the way a fresh clone's would be). A tmp `bin/` first on PATH holds
a logged-in `claude` (the scripted fake agent otherwise), the tests' `gh`, and `pnpm` and `uv` fakes that exit 0, so
no real package manager, model or network is used.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from agentflow import pack
from agentflow.config import load_config
from conftest import ENGINE, ROOT, run_git

REPOS = ENGINE / "tests" / "fixtures" / "repos"
FLOW = ENGINE / "flow.py"
WHITESPACE = 'git -c core.whitespace=-blank-at-eol diff --check "$(git hash-object -t tree /dev/null)"'
EXPECTED = {  # the verify gates' commands each repository's pack ends up with, and where they came from
    "python": (["python3 -m unittest -q"], "the CI workflow"),
    "node": (["pnpm run test"], "package.json scripts"),
    "docs": ([WHITESPACE], "the docs-only starter"),
}
CONFIRM_WARNING = "warning gates_confirmed:"
PRINTED_RULE = re.compile(r'^  add to `permissions\.allow`: "(Bash\(.+\))"$', re.MULTILINE)


def _script(path: Path, body: str) -> None:
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture
def fakebin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    bins = tmp_path / "bin"
    bins.mkdir()
    agent = f"{shlex.quote(sys.executable)} {shlex.quote(str(ENGINE / 'tests' / 'fake_agent.py'))}"
    _script(bins / "claude", f'if [ "$1 $2" = "auth status" ]; then exit 0; fi\nexec {agent} "$@"\n')
    shutil.copy2(ENGINE / "tests" / "bin" / "gh", bins / "gh")
    for name in ("pnpm", "uv"):
        _script(bins / name, "exit 0\n")
    monkeypatch.setenv("PATH", f"{bins}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.delenv("FAKE_GH_FAIL", raising=False)
    return bins


def _new_repo(root: Path) -> None:
    run_git(root, "init", "-q", "-b", "main")
    for key, value in (("user.name", "Test"), ("user.email", "test@example.com"), ("commit.gpgsign", "false")):
        run_git(root, "config", key, value)


def toy(kind: str, tmp_path: Path) -> Path:
    """`fixtures/repos/<kind>` as a git repo on `main` with one commit, pushed to a bare `origin`."""
    origin = tmp_path / "origin.git"
    run_git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    repo = tmp_path / kind
    shutil.copytree(REPOS / kind, repo, ignore=shutil.ignore_patterns("__pycache__", ".DS_Store"))
    _new_repo(repo)
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-q", "-m", "initial")
    run_git(repo, "remote", "add", "origin", str(origin))
    run_git(repo, "push", "-q", "origin", "main")
    return repo.resolve()


def agentflow(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(FLOW), *args], cwd=repo, capture_output=True, text=True, timeout=300)


def git_status(repo: Path) -> str:
    return run_git(repo, "status", "--porcelain", "--untracked-files=all").strip()


def confirm_gates(repo: Path) -> None:
    """What the human does after reading init's gate table: flip `gates_confirmed` in the pack."""
    path = repo / ".agentflow" / "config.yaml"
    text = path.read_text(encoding="utf-8")
    assert text.count("gates_confirmed: false") == 1, text
    path.write_text(text.replace("gates_confirmed: false", "gates_confirmed: true"), encoding="utf-8")


def add_bug_lane_rule(repo: Path, init_out: str) -> str:
    """What the human does for the bug lane (playbook §13): add the `Bash(...)` rule init printed to their own
    `.claude/settings.local.json`, which git ignores (Claude Code keeps it out of git; here `.git/info/exclude`)."""
    found = PRINTED_RULE.findall(init_out)
    assert len(found) == 1, init_out
    settings = repo / ".claude" / "settings.local.json"
    assert not settings.exists()
    settings.parent.mkdir(exist_ok=True)
    settings.write_text(json.dumps({"permissions": {"allow": found}}), encoding="utf-8")
    exclude = Path(run_git(repo, "rev-parse", "--path-format=absolute", "--git-path", "info/exclude").strip())
    exclude.parent.mkdir(parents=True, exist_ok=True)
    with exclude.open("a", encoding="utf-8") as handle:
        handle.write("/.claude/settings.local.json\n")
    return found[0]


def verify_commands(repo: Path) -> list[str]:
    cfg = load_config(pack.pack_config(repo))
    return [cfg.gates[name].run for name in cfg.gate_sets["verify"]]


def _shown(proc: subprocess.CompletedProcess) -> str:
    return f"exit {proc.returncode}\n{proc.stdout}{proc.stderr}"


@pytest.mark.parametrize("kind", sorted(EXPECTED))
def test_init_to_dry_run(kind: str, tmp_path: Path, fakebin: Path) -> None:
    repo = toy(kind, tmp_path)
    if kind == "docs":
        assert not (repo / ".claude").exists()  # a repository with no skills of its own runs on the bundled ones

    init = agentflow(repo, "init")
    assert init.returncode == 0, _shown(init)
    commands, source = EXPECTED[kind]
    assert verify_commands(repo) == commands, f"verify gates should come from {source}\n{init.stdout}"

    doctor = agentflow(repo, "doctor")
    assert doctor.returncode == 0, _shown(doctor)

    add_bug_lane_rule(repo, init.stdout)
    confirm_gates(repo)
    validate = agentflow(repo, "validate")
    assert validate.returncode == 0 and "(0 warnings)" in validate.stdout, _shown(validate)

    explain = agentflow(repo, "explain", "feature", "--module", "greet")
    assert explain.returncode == 0, _shown(explain)
    dry_run = agentflow(repo, "run", "feature", "--module", "greet", "--dry-run")
    assert dry_run.returncode == 0, _shown(dry_run)
    assert all(command in dry_run.stdout for command in commands), _shown(dry_run)

    assert git_status(repo) == "?? .agentflow/config.yaml"
    shutil.rmtree(repo / ".agentflow")
    assert git_status(repo) == ""


def test_init_then_validate_for_a_starter_in_an_empty_repo(tmp_path: Path, fakebin: Path) -> None:
    repo = tmp_path / "empty"
    repo.mkdir()
    _new_repo(repo)

    init = agentflow(repo, "init", "--starter", "python-uv")
    assert init.returncode == 0, _shown(init)
    add_bug_lane_rule(repo, init.stdout)
    validate = agentflow(repo, "validate")
    assert validate.returncode == 0, _shown(validate)
    warnings = [line for line in validate.stdout.splitlines() if line.startswith("warning ")]
    assert len(warnings) == 1 and warnings[0].startswith(CONFIRM_WARNING), _shown(validate)
    assert "(1 warnings)" in validate.stdout

    confirm_gates(repo)
    validate = agentflow(repo, "validate")
    assert validate.returncode == 0 and "(0 warnings)" in validate.stdout, _shown(validate)
    assert git_status(repo) == "?? .agentflow/config.yaml"


def test_security_overlay_covers_the_new_modules() -> None:
    """The modules that parse an untrusted repository, write its pack or merge the config layers are reviewed with
    the security overlay, like the guards they feed."""
    when_paths = load_config(pack.pack_config(ROOT)).overlays["security"]["when_paths"]
    for module in ("agentflow/layers.py", "agentflow/detect.py", "agentflow/init.py"):
        assert module in when_paths
