"""`devtools/floor_guard.py` run for real on a throwaway repository: what it blocks, and its one scope limit."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from agentflow.config import load_config

AGENTFLOW = Path(__file__).resolve().parents[1]
GUARD = AGENTFLOW.parent / "floor_guard.py"
RUNNER_TEST = "devtools/agentflow/tests/test_runner.py"
PRODUCT_TEST = "tests/unit/test_crm.py"
TWO_ASSERTS = "def test_it():\n    assert 1 == 1\n    assert 2 == 2\n"
ONE_ASSERT = "def test_it():\n    assert 1 == 1\n"
SKIPPED = Path(__file__).with_name("floor_guard_skipped_test.txt")  # a data file: this line would be a real finding here


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
                   check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    for name in (RUNNER_TEST, PRODUCT_TEST):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(TWO_ASSERTS, encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "Start")
    git(root, "checkout", "-q", "-b", "work")
    return root


def guard(repo: Path) -> subprocess.CompletedProcess[str]:
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "Change")
    return subprocess.run([sys.executable, str(GUARD), "--base", "main"], cwd=repo, capture_output=True, text=True)


def test_a_removed_assertion_in_a_product_test_still_blocks(repo):
    (repo / PRODUCT_TEST).write_text(ONE_ASSERT, encoding="utf-8")
    done = guard(repo)
    assert done.returncode == 1 and "assertion-removed" in done.stderr


def test_a_removed_assertion_in_the_runners_own_tests_does_not_block(repo):
    (repo / RUNNER_TEST).write_text(ONE_ASSERT, encoding="utf-8")
    done = guard(repo)
    assert done.returncode == 0, done.stderr


def test_the_runners_own_tests_keep_every_other_floor_check(repo):
    (repo / RUNNER_TEST).write_text(SKIPPED.read_text(encoding="utf-8"), encoding="utf-8")
    done = guard(repo)
    assert done.returncode == 1 and "test-made-easier" in done.stderr


def test_deleting_one_of_the_runners_own_tests_still_blocks(repo):
    (repo / RUNNER_TEST).unlink()
    done = guard(repo)
    assert done.returncode == 1 and "test-deleted" in done.stderr


def test_an_agent_in_a_run_cannot_edit_the_runners_own_tests():
    forbidden = load_config(AGENTFLOW / "config.yaml").guardrails.forbidden_paths
    assert "devtools/agentflow/tests/**" in forbidden
