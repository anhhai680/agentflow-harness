"""The engine is project-agnostic: every fixture pack validates, explains and dry-runs in a repository of its own,
this repository's pack validates, and no engine file names a pack's strings."""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import agentflow
from agentflow import pack
from agentflow.conditions import explain, make_ctx
from agentflow.config import load_config, validate
from conftest import ENGINE, PACKS as PACKS_DIR, run_git

PACKS = ["python", "node"]
DENYLIST = re.compile(r"(?i:tivacare)|CONSTRAINTS\.md|src/|docs/specs|docs/ideas|CAPABILITY-MAP|SPEC-|floor_guard|webadmin|next-step")
FLOW = ENGINE / "flow.py"


def git_status(root: Path) -> str:
    """Everything git sees in `root`: changes, untracked files and ignored ones."""
    return run_git(root, "status", "--porcelain", "--untracked-files=all", "--ignored")


@pytest.fixture(params=PACKS)
def pack_repo(request, tmp_path, git, monkeypatch) -> Path:
    """A fixture pack copied into a fresh git repo of its own (one commit on `main`), the state dir under tmp."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    root = tmp_path / request.param
    shutil.copytree(PACKS_DIR / request.param, root, ignore=shutil.ignore_patterns("__pycache__"))
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.name", "Test")
    git(root, "config", "user.email", "test@example.com")
    git(root, "config", "commit.gpgsign", "false")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "initial")
    return root


def test_fixture_pack_validates_clean(pack_repo):
    assert validate(load_config(pack.pack_config(pack_repo)), pack_repo) == []


def test_fixture_pack_explains_every_lane(pack_repo):
    cfg = load_config(pack.pack_config(pack_repo))
    for lane in cfg.lanes:
        assert explain(cfg, lane, make_ctx(cfg, pack_repo, lane, {"module": "greet", "issue": "1"}), {})


def test_fixture_pack_dry_run_writes_nothing(pack_repo):
    status_before = git_status(pack_repo)
    proc = subprocess.run([sys.executable, str(FLOW), "run", "feature", "--module", "greet", "--dry-run"],
                          cwd=pack_repo, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert git_status(pack_repo) == status_before


def test_fixture_pack_state_lives_outside_the_repo(pack_repo):
    assert not pack.state_dir(pack_repo).is_relative_to(pack_repo)


def test_this_repos_pack_validates_clean(repo_root):
    assert validate(load_config(pack.pack_config(repo_root)), repo_root) == []


def test_engine_names_no_pack_strings():
    engine = Path(agentflow.__file__).parent
    files = [*sorted(engine.glob("*.py")), engine / "dashboard.html", *sorted(engine.glob("prompts/*.md"))]
    hits = [f"{f.name}:{n}: {line.strip()}" for f in files
            for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1) if DENYLIST.search(line)]
    assert hits == []
