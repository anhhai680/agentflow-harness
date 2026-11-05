#!/usr/bin/env python3
"""Claude Code PreToolUse hook: run the floor guard before `git push` or `gh pr create`.

CI runs `devtools/floor_guard.py` on the whole PR diff, but nothing ran it before a hand-made push, so a floor
violation (a silenced checker, a skipped test, ...) only showed up as a red CI run on a new PR. This hook runs the
same guard against the same base first, and blocks the command (exit 2, reason on stderr) while it fails. A guard
that cannot run blocks too, as in CONSTRAINTS.md: "could not run" never counts as clean.

Input is the hook event as JSON on stdin. Any other tool or command, and input it cannot read, passes untouched.
`CLAUDE_PROJECT_DIR` names the repository (default: the current directory); `FLOOR_BASE` names the base ref
(default `origin/main`, fetched first).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

# A `git push` or `gh pr create` that starts a command: at the start, or after `;`, `&`, `|` or `(`. Text inside a
# quoted message or after `echo` does not start one. `git -C dir push` and `git -c k=v push` count.
GUARDED = re.compile(
    r"(?:^|[;&|(]\s*)(?:git\s+(?:-C\s+\S+\s+|-c\s+\S+\s+|--\S+\s+)*push|gh\s+pr\s+create)\b"
)
FETCH_TIMEOUT_S = 30
BLOCKED = "Blocked: the floor guard found problems, and CI would fail on them. Fix them, commit, then push again.\n"


def guarded(event: object) -> bool:
    if not isinstance(event, dict) or event.get("tool_name") != "Bash":
        return False
    tool_input = event.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    return isinstance(command, str) and bool(GUARDED.search(command))


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except ValueError:
        return 0
    if not guarded(event):
        return 0
    root = Path(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())
    guard = root / "devtools" / "floor_guard.py"
    base = os.environ.get("FLOOR_BASE")
    if not guard.is_file():
        sys.stderr.write(f"Blocked: the floor guard could not run ({guard} is missing).\n")
        return 2
    if base is None:
        base = "origin/main"
        try:  # best effort: a stale origin/main only makes the check differ from CI's, never pass less
            subprocess.run(["git", "fetch", "-q", "origin", "main"], cwd=root, capture_output=True,
                           timeout=FETCH_TIMEOUT_S, check=False)
        except (OSError, subprocess.TimeoutExpired):
            sys.stderr.write("floor hook: could not fetch origin/main; checking against the local copy\n")
    done = subprocess.run([sys.executable, str(guard), "--base", base], cwd=root, capture_output=True, text=True)
    if done.returncode == 0:
        return 0
    detail = (done.stderr or done.stdout).strip()
    if done.returncode == 1:
        sys.stderr.write(f"{detail}\n\n{BLOCKED}")
    else:
        sys.stderr.write(f"Blocked: the floor guard could not run (exit {done.returncode}).\n{detail}\n")
    return 2


if __name__ == "__main__":
    sys.exit(main())
