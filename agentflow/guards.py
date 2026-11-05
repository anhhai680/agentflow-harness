"""Diff and permission guards: what an agent step may not do, and whether settings cover the gates.

Guards compare **tree snapshots**, not `HEAD`: `snapshot()` stages everything into a temporary
index and writes a tree, so the real index and working tree are never touched, and untracked
files count. A step's diff is `changed_paths(start_tree, end_tree)`.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from agentflow.config import Config, Issue

ViolationKind = Literal["forbidden_path", "agent_commit", "rejected_trailer", "user_only_skill"]


@dataclass(frozen=True)
class Violation:
    kind: ViolationKind
    detail: str


def _git(worktree: Path, *args: str, env: dict[str, str] | None = None) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=worktree,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
        env=env,
    ).stdout


def snapshot(worktree: Path) -> str:
    """Tree hash of the working tree (tracked and untracked, minus ignored), index untouched."""
    worktree = Path(worktree)
    real_index = Path(_git(worktree, "rev-parse", "--path-format=absolute", "--git-path", "index").strip())
    with tempfile.TemporaryDirectory(prefix="agentflow-index-") as tmp:
        index = Path(tmp) / "index"
        if real_index.exists():
            shutil.copyfile(real_index, index)  # seeds the stat cache so the add below is fast
        env = {**os.environ, "GIT_INDEX_FILE": str(index)}
        _git(worktree, "add", "-A", env=env)
        return _git(worktree, "write-tree", env=env).strip()


def changed_paths(worktree: Path, tree_a: str, tree_b: str) -> list[str]:
    """Paths that differ between two trees (added, modified, deleted), sorted."""
    out = _git(Path(worktree), "diff-tree", "-r", "--name-only", "--no-renames", "-z", tree_a, tree_b)
    return sorted(p for p in out.split("\0") if p)


def _glob_regex(pattern: str) -> re.Pattern[str]:
    out = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out) + r"\Z", re.DOTALL)


def path_matches(path: str, pattern: str) -> bool:
    """Glob match on a repo-relative path: `**` crosses `/`, `*` and `?` do not."""
    path = path.removeprefix("./")
    return _glob_regex(pattern.removeprefix("./")).match(path) is not None


def check_message(cfg: Config, message: str) -> list[Violation]:
    """A commit message must not carry a rejected trailer (a line `Trailer: value`)."""
    found = []
    for trailer in cfg.guardrails.reject_trailers:
        if re.search(rf"^\s*{re.escape(trailer)}\s*:", message, re.IGNORECASE | re.MULTILINE):
            found.append(Violation("rejected_trailer", trailer))
    return found


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _strings(v)]
    if isinstance(value, list):
        return [s for v in value for s in _strings(v)]
    return []


def _user_only_violations(cfg: Config, tool_calls: list[dict]) -> list[Violation]:
    user_only = cfg.guardrails.user_only_skills
    skills_dir = re.escape(cfg.project.skills_dir)
    found: list[Violation] = []
    for call in tool_calls:
        args = call.get("input") or {}
        if call.get("name") == "Skill" and isinstance(args, dict):
            invoked = str(args.get("skill", "")).rsplit(":", 1)[-1]
            if invoked in user_only:
                found.append(Violation("user_only_skill", f"{invoked}: loaded with the Skill tool"))
                continue
        for skill in user_only:
            hit = re.compile(rf"{skills_dir}/{re.escape(skill)}(?![\w.-])")
            for text in _strings(args):
                if hit.search(text):
                    found.append(Violation("user_only_skill", f"{skill}: {call.get('name')} {text}"))
                    break
    return found


def check_step(
    cfg: Config,
    worktree: Path,
    start_head: str,
    start_tree: str,
    end_tree: str,
    tool_calls: list[dict],
    forbid: list[str] | tuple[str, ...] = (),
) -> list[Violation]:
    """Everything a finished agent step did that the guardrails, or the step's own `forbid`, forbid."""
    worktree = Path(worktree)
    violations: list[Violation] = []
    patterns = [*cfg.guardrails.forbidden_paths, *forbid]
    for path in changed_paths(worktree, start_tree, end_tree):
        if any(path_matches(path, pattern) for pattern in patterns):
            violations.append(Violation("forbidden_path", path))
    head = _git(worktree, "rev-parse", "HEAD").strip()
    if head != start_head:
        violations.append(Violation("agent_commit", f"HEAD moved from {start_head[:12]} to {head[:12]}"))
        try:
            messages = _git(worktree, "log", "--format=%B%x00", f"{start_head}..{head}")
        except subprocess.CalledProcessError:
            messages = _git(worktree, "log", "-1", "--format=%B", head)
        for message in messages.split("\0"):
            violations.extend(v for v in check_message(cfg, message) if v not in violations)
    violations.extend(_user_only_violations(cfg, tool_calls))
    return violations


# --- permission coverage -------------------------------------------------------------

_SPLIT = re.compile(r"&&|\|\||;|\|")


def _load_rules(repo: Path) -> tuple[dict[str, list[str]], list[Issue]]:
    rules: dict[str, list[str]] = {"allow": [], "ask": [], "deny": []}
    issues: list[Issue] = []
    for name in ("settings.json", "settings.local.json"):
        path = repo / ".claude" / name
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            permissions = data.get("permissions", {})
            for kind in rules:
                rules[kind].extend(str(x) for x in permissions.get(kind, []))
        except (ValueError, AttributeError, TypeError) as exc:
            issues.append(Issue("warning", f".claude/{name}", f"cannot read permission rules: {exc}"))
    return rules, issues


def _bash_patterns(entries: list[str]) -> list[str]:
    patterns = []
    for entry in entries:
        if entry == "Bash":
            patterns.append("*")
        elif entry.startswith("Bash(") and entry.endswith(")"):
            patterns.append(entry[5:-1])
    return patterns


def _bash_matches(pattern: str, command: str) -> bool:
    if pattern.endswith(":*"):  # legacy prefix form: `Bash(npm run:*)`
        prefix = pattern[:-2]
        return command == prefix or command.startswith(prefix + " ")
    if pattern.endswith(" *") and pattern.count("*") == 1 and command == pattern[:-2]:
        return True  # Claude Code: a lone trailing ` *` also matches the bare command (`git status *`)
    regex = ".*".join(re.escape(piece) for piece in pattern.split("*"))
    return re.fullmatch(regex, command, re.DOTALL) is not None


def _covered(rules: dict[str, list[str]], command: str, *, need_allow: bool = True) -> bool:
    """No `ask` or `deny` pattern matches `command`, and (with `need_allow`) an `allow` pattern does."""
    allow = _bash_patterns(rules["allow"])
    blocked = _bash_patterns(rules["ask"]) + _bash_patterns(rules["deny"])
    if any(_bash_matches(p, command) for p in blocked):
        return False
    return not need_allow or any(_bash_matches(p, command) for p in allow)


def main_checkout(path: Path) -> Path:
    """The main checkout of the repository `path` is in, even from inside a linked worktree."""
    path = Path(path).resolve()
    common = _git(path, "rev-parse", "--path-format=absolute", "--git-common-dir").strip()
    return Path(common).resolve().parent


def command_allowed(repo: Path, command: str) -> bool:
    """Whether the human's permission rules would let a headless agent run `command` (one simple command,
    no `&&`/`;`/`|` parts): an allow match, and no ask or deny match.

    The rules are always read from the MAIN checkout of `repo` (`.claude/settings.json` and the gitignored
    `settings.local.json`), never from a run's worktree: an agent can write the worktree's
    `settings.local.json` in its session, and the guards cannot see it there (snapshots skip ignored
    files)."""
    rules, _issues = _load_rules(main_checkout(repo))
    return _covered(rules, command.strip())


def settings_coverage(cfg: Config, repo: Path) -> list[Issue]:
    """Warn for each gate command part a headless agent would be denied.

    Rules come from the merged `.claude/settings.json` and `settings.local.json`. A part is
    covered when no `ask` or `deny` pattern matches it: headless runs deny anything that would
    prompt, so an `ask` match is as bad as a deny. Unless the default agent runs in
    `bypassPermissions` (where allow rules have no effect), an `allow` pattern must match it too.
    """
    rules, issues = _load_rules(Path(repo))
    agent = cfg.agents.get(cfg.defaults.agent)
    need_allow = agent is None or agent.permission_mode != "bypassPermissions"
    for name, gate in cfg.gates.items():
        for part in (p.strip() for p in _SPLIT.split(gate.run)):
            if not part:
                continue
            if not _covered(rules, part, need_allow=need_allow):
                message = (
                    f"'{part}' is not covered by a Bash allow rule in .claude/settings*.json; "
                    "a headless run would be denied. Add a narrow Bash(...) rule."
                    if need_allow
                    else f"'{part}' matches an ask or deny rule in .claude/settings*.json; a headless run would be denied."
                )
                issues.append(Issue("warning", f"gates.{name}.run", message))
                break
    return issues
