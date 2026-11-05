"""`devtools/hooks/floor_before_push.py`: the Claude Code hook that runs the floor guard before `git push` / `gh pr create`.

CI runs the guard on the whole PR diff, and nothing ran it before a hand-made push, so a violation only showed up as a
red CI run on a new PR. The hook closes that gap: it blocks the push (exit 2, reason on stderr) while the guard fails.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

DEVTOOLS = Path(__file__).resolve().parents[2]
HOOK = DEVTOOLS / "hooks" / "floor_before_push.py"
GUARD = DEVTOOLS / "floor_guard.py"
SETTINGS = DEVTOOLS.parent / ".claude" / "settings.json"
SILENCED = "x = 1  # " + "type: " + "ignore\n"  # split, so this file is not itself a finding


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
                   check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "devtools").mkdir(parents=True)
    shutil.copy(GUARD, root / "devtools" / "floor_guard.py")
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "Start")
    git(root, "checkout", "-q", "-b", "work")
    return root


def run_hook(repo: Path, command: str, tool: str = "Bash") -> subprocess.CompletedProcess[str]:
    event = {"tool_name": tool, "tool_input": {"command": command}}
    env = {**os.environ, "CLAUDE_PROJECT_DIR": str(repo), "FLOOR_BASE": "main"}
    return subprocess.run([sys.executable, str(HOOK)], input=json.dumps(event), cwd=repo, env=env,
                          capture_output=True, text=True)


def commit_change(repo: Path, text: str) -> None:
    (repo / "src.py").write_text(text, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "Change")


@pytest.mark.parametrize("command", [
    "git push -u origin feat/x", "git push", "cd repo && git push origin HEAD", "gh pr create --base main --title t",
    "git status && gh pr create --fill",
])
def test_a_push_or_pr_with_a_floor_violation_is_blocked(repo, command):
    commit_change(repo, SILENCED)
    done = run_hook(repo, command)
    assert done.returncode == 2
    assert "silenced-checker" in done.stderr and "src.py" in done.stderr


@pytest.mark.parametrize("command", ["git push origin HEAD", "gh pr create --fill"])
def test_a_clean_branch_may_push(repo, command):
    commit_change(repo, "x = 1\n")
    assert run_hook(repo, command).returncode == 0


@pytest.mark.parametrize("command", [
    "git status", "git commit -m 'git push later'", "gh pr view 17", "gh pr list", "echo gh pr create", "ls",
])
def test_other_commands_are_left_alone_even_on_a_violating_branch(repo, command):
    commit_change(repo, SILENCED)
    assert run_hook(repo, command).returncode == 0


def test_other_tools_are_left_alone(repo):
    commit_change(repo, SILENCED)
    assert run_hook(repo, "git push", tool="Edit").returncode == 0


def test_a_guard_that_cannot_run_blocks_too(repo):
    commit_change(repo, "x = 1\n")
    (repo / "devtools" / "floor_guard.py").unlink()
    done = run_hook(repo, "git push")
    assert done.returncode == 2 and "could not run" in done.stderr


def test_unreadable_input_never_blocks(repo):
    env = {**os.environ, "CLAUDE_PROJECT_DIR": str(repo), "FLOOR_BASE": "main"}
    done = subprocess.run([sys.executable, str(HOOK)], input="not json", cwd=repo, env=env, capture_output=True, text=True)
    assert done.returncode == 0


def test_the_project_settings_wire_the_hook_to_bash():
    hooks = json.loads(SETTINGS.read_text(encoding="utf-8"))["hooks"]["PreToolUse"]
    wired = [h for entry in hooks if entry["matcher"] == "Bash" for h in entry["hooks"]]
    assert any(h["type"] == "command" and "devtools/hooks/floor_before_push.py" in h["command"] for h in wired)
