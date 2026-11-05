from __future__ import annotations

import dataclasses
import json

import pytest

from agentflow import flow, guards
from agentflow.config import Gate, load_config
from agentflow.state import runs_root
from conftest import PYTHON_CONFIG
from agentflow.guards import (
    Violation,
    _covered,
    changed_paths,
    check_message,
    check_step,
    path_matches,
    settings_coverage,
    snapshot,
)
from test_gates import cfg_with


@pytest.fixture
def cfg():
    return load_config(PYTHON_CONFIG)


def head(git, wt):
    return git(wt, "rev-parse", "HEAD").strip()


def run_step(cfg, git, wt, mutate, tool_calls=None):
    start_head, start_tree = head(git, wt), snapshot(wt)
    mutate()
    return check_step(cfg, wt, start_head, start_tree, snapshot(wt), tool_calls or [])


def test_snapshot_leaves_index_untouched(tmp_git, git):
    (tmp_git / "src" / "app.py").write_text("x = 2\n", encoding="utf-8")
    (tmp_git / "new.txt").write_text("new\n", encoding="utf-8")
    (tmp_git / "README.md").unlink()
    before = git(tmp_git, "status", "--porcelain=v2")
    index_before = (tmp_git / ".git" / "index").read_bytes()
    tree = snapshot(tmp_git)
    assert git(tmp_git, "status", "--porcelain=v2") == before
    assert (tmp_git / ".git" / "index").read_bytes() == index_before
    assert "?? new.txt" in git(tmp_git, "status", "--porcelain")
    assert len(tree) == 40


def test_snapshot_is_stable_and_detects_changes(tmp_git):
    a = snapshot(tmp_git)
    assert snapshot(tmp_git) == a
    (tmp_git / "new.txt").write_text("new\n", encoding="utf-8")
    b = snapshot(tmp_git)
    assert b != a
    (tmp_git / "src" / "app.py").unlink()
    c = snapshot(tmp_git)
    assert changed_paths(tmp_git, a, b) == ["new.txt"]
    assert changed_paths(tmp_git, b, c) == ["src/app.py"]
    assert changed_paths(tmp_git, a, a) == []


def test_snapshot_ignores_gitignored_files(tmp_git):
    (tmp_git / ".gitignore").write_text("*.log\n", encoding="utf-8")
    a = snapshot(tmp_git)
    (tmp_git / "x.log").write_text("noise", encoding="utf-8")
    assert snapshot(tmp_git) == a


@pytest.mark.parametrize(
    ("path", "pattern", "expected"),
    [
        ("CONSTRAINTS.md", "CONSTRAINTS.md", True),
        ("docs/CONSTRAINTS.md", "CONSTRAINTS.md", False),
        (".claude/skills/x/SKILL.md", ".claude/**", True),
        (".claude/settings.json", ".claude/**", True),
        ("src/a/b.py", "src/*.py", False),
        ("src/b.py", "src/*.py", True),
        ("src/a/b.py", "src/**/*.py", True),
        ("src/b.py", "src/**/*.py", True),
        ("src/toy/x/identity/y.py", "**/identity/**", True),
        ("identity/y.py", "**/identity/**", True),
        ("src/toy/staff_auth/a/b.py", "src/toy/staff_auth/**", True),
        ("src/toy/staff_authx/a.py", "src/toy/staff_auth/**", False),
        ("a.b", "a.b", True),
        ("axb", "a.b", False),
    ],
)
def test_path_matches(path, pattern, expected):
    assert path_matches(path, pattern) is expected


def test_forbidden_path_edit(cfg, tmp_git, git):
    v = run_step(cfg, git, tmp_git, lambda: (tmp_git / "CONSTRAINTS.md").write_text("weaker\n", encoding="utf-8"))
    assert v == [Violation("forbidden_path", "CONSTRAINTS.md")]


def test_forbidden_untracked_under_claude(cfg, tmp_git, git):
    def mutate():
        skill = tmp_git / ".claude" / "skills" / "x"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("---\nname: x\n---\n", encoding="utf-8")

    v = run_step(cfg, git, tmp_git, mutate)
    assert v == [Violation("forbidden_path", ".claude/skills/x/SKILL.md")]


def test_forbidden_path_deleted(cfg, tmp_git, git):
    v = run_step(cfg, git, tmp_git, lambda: (tmp_git / "CONSTRAINTS.md").unlink())
    assert v == [Violation("forbidden_path", "CONSTRAINTS.md")]


def test_allowed_edit_is_clean(cfg, tmp_git, git):
    v = run_step(cfg, git, tmp_git, lambda: (tmp_git / "src" / "app.py").write_text("x = 2\n", encoding="utf-8"))
    assert v == []


def test_agent_commit(cfg, tmp_git, git):
    def mutate():
        (tmp_git / "src" / "app.py").write_text("x = 3\n", encoding="utf-8")
        git(tmp_git, "commit", "-q", "-am", "agent was here")

    v = run_step(cfg, git, tmp_git, mutate)
    assert [x.kind for x in v] == ["agent_commit"]


def test_agent_commit_of_forbidden_file_reports_both(cfg, tmp_git, git):
    def mutate():
        (tmp_git / "CONSTRAINTS.md").write_text("weaker\n", encoding="utf-8")
        git(tmp_git, "commit", "-q", "-am", "sneaky\n\nConstraints-Approved-By: me")

    kinds = {x.kind for x in run_step(cfg, git, tmp_git, mutate)}
    assert kinds == {"agent_commit", "forbidden_path", "rejected_trailer"}


def test_rejected_trailer(cfg):
    v = check_message(cfg, "x\n\nConstraints-Approved-By: me")
    assert v == [Violation("rejected_trailer", "Constraints-Approved-By")]
    assert check_message(cfg, "x\n\nCo-Authored-By: Claude <a@b.c>") == []
    assert check_message(cfg, "mentions Constraints-Approved-By in prose") == []


def test_user_only_skill_tool_call(cfg, tmp_git, git):
    calls = [{"name": "Skill", "input": {"skill": "writing-for-agents"}}]
    v = run_step(cfg, git, tmp_git, lambda: None, calls)
    assert [x.kind for x in v] == ["user_only_skill"]
    assert "writing-for-agents" in v[0].detail


def test_user_only_skill_with_plugin_prefix(cfg, tmp_git, git):
    calls = [{"name": "Skill", "input": {"skill": "agent-skills:using-git-worktrees"}}]
    assert [x.kind for x in run_step(cfg, git, tmp_git, lambda: None, calls)] == ["user_only_skill"]


def test_allowed_skill_is_clean(cfg, tmp_git, git):
    calls = [{"name": "Skill", "input": {"skill": "test-driven-development"}}]
    assert run_step(cfg, git, tmp_git, lambda: None, calls) == []


def test_user_only_skill_read_by_path(cfg, tmp_git, git):
    path = str(tmp_git / ".claude" / "skills" / "using-git-worktrees" / "SKILL.md")
    calls = [{"name": "Read", "input": {"file_path": path}}]
    v = run_step(cfg, git, tmp_git, lambda: None, calls)
    assert [x.kind for x in v] == ["user_only_skill"]


def test_user_only_skill_read_from_a_custom_skills_dir_trips(tmp_cfg):
    cfg = load_config(tmp_cfg({"project": {"skills_dir": "skills"}}))
    calls = [{"name": "Read", "input": {"file_path": "skills/doubt-driven-development/SKILL.md"}}]
    assert [v.kind for v in guards._user_only_violations(cfg, calls)] == ["user_only_skill"]


@pytest.mark.parametrize("skills_dir", ["skills", "./skills", "skills/", "./skills/"])
@pytest.mark.parametrize("read", ["skills/{s}/SKILL.md", "./skills/{s}/SKILL.md", "{root}/skills/{s}/SKILL.md"])
def test_user_only_skill_trips_however_the_skills_dir_and_the_read_are_spelled(tmp_cfg, tmp_path, skills_dir, read):
    cfg = load_config(tmp_cfg({"project": {"skills_dir": skills_dir}}))
    path = read.format(s="doubt-driven-development", root=tmp_path)
    calls = [{"name": "Read", "input": {"file_path": path}}]
    assert [v.kind for v in guards._user_only_violations(cfg, calls)] == ["user_only_skill"]


def test_user_only_skill_read_via_bash(cfg, tmp_git, git):
    calls = [{"name": "Bash", "input": {"command": "cat .claude/skills/doubt-driven-development/SKILL.md"}}]
    assert [x.kind for x in run_step(cfg, git, tmp_git, lambda: None, calls)] == ["user_only_skill"]


def test_similar_skill_dir_is_not_flagged(cfg, tmp_git, git):
    calls = [{"name": "Read", "input": {"file_path": ".claude/skills/using-git-worktrees-lite/SKILL.md"}}]
    assert run_step(cfg, git, tmp_git, lambda: None, calls) == []


# --- settings coverage ---------------------------------------------------------------


def with_mode(cfg, mode):
    """`cfg` with the default agent's permission_mode set to `mode`."""
    name = cfg.defaults.agent
    agents = {**cfg.agents, name: dataclasses.replace(cfg.agents[name], permission_mode=mode)}
    return dataclasses.replace(cfg, agents=agents)


def test_settings_cover_shipped_gates(repo_root):
    assert settings_coverage(load_config(PYTHON_CONFIG), repo_root) == []


def test_shipped_settings_file_alone_covers_shipped_gates(repo_root, tmp_path):
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text(
        (repo_root / ".claude" / "settings.json").read_text(encoding="utf-8"), encoding="utf-8"
    )
    assert settings_coverage(load_config(PYTHON_CONFIG), tmp_path) == []


def test_settings_coverage_warns_on_uncovered_gate(repo_root):
    cfg = with_mode(cfg_with({"make": "make lint"}), "acceptEdits")
    issues = settings_coverage(cfg, repo_root)
    assert [(i.level, i.path) for i in issues] == [("warning", "gates.make.run")]
    assert "make lint" in issues[0].message


def test_settings_coverage_checks_each_part(repo_root):
    cfg = with_mode(cfg_with({"mixed": "uv run --no-project --with pyyaml --with pytest pytest agentflow/tests -q && make lint; git status --short"}), "acceptEdits")
    issues = settings_coverage(cfg, repo_root)
    assert len(issues) == 1 and "make lint" in issues[0].message


def test_settings_coverage_without_settings_files_warns_for_every_gate(tmp_path):
    cfg = with_mode(load_config(PYTHON_CONFIG), "acceptEdits")
    issues = settings_coverage(cfg, tmp_path)
    assert {i.path for i in issues} == {f"gates.{n}.run" for n in cfg.gates}
    assert all(i.level == "warning" for i in issues)


def test_settings_coverage_local_file_adds_allow_and_deny_wins(tmp_path):
    claude = tmp_path / ".claude"
    claude.mkdir()
    (claude / "settings.json").write_text(
        json.dumps({"permissions": {"allow": ["Bash(uv run *)"], "deny": ["Bash(uv run rm*)"]}}), encoding="utf-8"
    )
    (claude / "settings.local.json").write_text(
        json.dumps({"permissions": {"allow": ["Bash(make *)", "Bash(uv run rm*)"]}}), encoding="utf-8"
    )
    cfg = with_mode(cfg_with({"lint": "make lint", "ok": "uv run ruff check .", "blocked": "uv run rm -rf x"}),
                    "acceptEdits")
    assert {i.path for i in settings_coverage(cfg, tmp_path)} == {"gates.blocked.run"}


def test_settings_coverage_in_bypass_mode_warns_only_on_ask_or_deny(tmp_path):
    claude = tmp_path / ".claude"
    claude.mkdir()
    (claude / "settings.json").write_text(
        json.dumps({"permissions": {"ask": ["Bash(git push*)"], "deny": ["Bash(uv run rm*)"]}}), encoding="utf-8"
    )
    cfg = with_mode(cfg_with({"lint": "make lint", "push": "git push origin x", "blocked": "uv run rm -rf x"}),
                    "bypassPermissions")
    issues = settings_coverage(cfg, tmp_path)
    assert {i.path for i in issues} == {"gates.push.run", "gates.blocked.run"}
    assert all("ask or deny" in i.message for i in issues)


def test_shipped_config_runs_in_bypass_mode_with_no_coverage_warnings(tmp_path):
    cfg = load_config(PYTHON_CONFIG)
    assert cfg.agents[cfg.defaults.agent].permission_mode == "bypassPermissions"
    assert settings_coverage(cfg, tmp_path) == []  # no settings files: allow rules are moot


def test_settings_coverage_ask_rule_is_not_coverage(tmp_path):
    claude = tmp_path / ".claude"
    claude.mkdir()
    (claude / "settings.json").write_text(
        json.dumps({"permissions": {"allow": ["Bash(git *)"], "ask": ["Bash(git push*)"]}}), encoding="utf-8"
    )
    cfg = cfg_with({"push": "git push", "log": "git log"})
    assert {i.path for i in settings_coverage(cfg, tmp_path)} == {"gates.push.run"}


def test_settings_coverage_malformed_file_is_a_warning(tmp_path):
    claude = tmp_path / ".claude"
    claude.mkdir()
    (claude / "settings.json").write_text("{not json", encoding="utf-8")
    cfg = cfg_with({"ok": "true"})
    paths = [i.path for i in settings_coverage(cfg, tmp_path)]
    assert ".claude/settings.json" in paths


def test_validate_command_reports_coverage_warnings(tmp_cfg, monkeypatch, repo_root, capsys):
    def patch(d: dict) -> None:
        d["gates"].update({"make": {"run": "make lint"}})
        d["agents"]["claude"]["permission_mode"] = "acceptEdits"  # bypass mode never needs an allow rule

    cfg_path = tmp_cfg(patch)
    monkeypatch.setattr(flow, "_find_repo", lambda: repo_root)
    assert flow.main(["validate", "--config", str(cfg_path)]) == 0
    out = capsys.readouterr().out
    assert "warning gates.make.run" in out


def test_validate_command_shipped_config_has_no_coverage_warnings(capsys, pack_repo_python, monkeypatch):
    monkeypatch.chdir(pack_repo_python)
    assert flow.main(["validate"]) == 0
    out = capsys.readouterr().out
    assert "gates." not in out


# --- the shipped allow list is narrow (final review, Important 2) ----------------------


def shipped_allows(repo_root, command: str) -> bool:
    """Whether the shipped `.claude/settings.json` alone lets a headless agent run `command`."""
    rules = json.loads((repo_root / ".claude" / "settings.json").read_text(encoding="utf-8"))["permissions"]
    return _covered({kind: list(rules.get(kind, [])) for kind in ("allow", "ask", "deny")}, command)


@pytest.mark.parametrize("command", [
    "uv run git push --force origin main",
    "uv run python -c 'import os'",
    "uv run sh -c 'git push'",
    "uvx anything",
    "uvx --from evil tool",
    "pnpm dlx some-package",
    "pnpm exec git push origin main",
    "pnpm install",
    "pnpm run anything",
    "git difftool -x evil",
    "git log --output=runs/x/events.jsonl",
    "git diff --output=/tmp/x",
    "git push origin main",
    "git push --force origin main",
])
def test_shipped_settings_do_not_cover_escapes(repo_root, command):
    assert not shipped_allows(repo_root, command)


@pytest.mark.parametrize("command", [
    "uv run --no-project --with pyyaml --with pytest pytest agentflow/tests --import-mode=prepend -q",
    "uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_x.py::test_y --import-mode=prepend -q",
    "bin/agentflow validate",
    "bin/agentflow explain feature --module demo",
    "git status",
    "git diff HEAD~1",
])
def test_shipped_settings_cover_the_gate_commands(repo_root, command):
    assert shipped_allows(repo_root, command)


def test_the_runner_denies_agent_edits_to_its_runs_root(flow_repo):
    # The runs root is in the state dir, outside the repo by default, so no project settings file can name it:
    # the runner's own `--settings` deny rule does, as an absolute (`//`) path.
    from test_agent_step import run_step

    flow_repo.new_run()
    assert run_step(flow_repo) == "done"
    argv = flow_repo.calls()[0]["argv"]
    deny = json.loads(argv[argv.index("--settings") + 1])["permissions"]["deny"]
    assert f"Edit(/{runs_root(flow_repo.root)}/**)" in deny
    assert runs_root(flow_repo.root).is_absolute()
