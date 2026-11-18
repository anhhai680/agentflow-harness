"""`brief.py`: the run brief, read from the run's fork commit and rendered byte-identically."""
from __future__ import annotations

from pathlib import Path

import pytest

from agentflow import brief, pack
from agentflow.brief import Doc
from conftest import run_git


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    run_git(repo, "init", "-q", "-b", "main")
    run_git(repo, "config", "user.email", "t@example.com")
    run_git(repo, "config", "user.name", "T")
    return repo


def _write(repo: Path, rel: str, text: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode())


def _commit(repo: Path, files: dict[str, str] | None = None) -> str:
    for rel, text in (files or {}).items():
        _write(repo, rel, text)
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-q", "--allow-empty", "-m", "c")
    return run_git(repo, "rev-parse", "HEAD").strip()


SHA = "0123456789abcdef0123456789abcdef01234567"
RULES = [Doc("## CLAUDE.md", "Rule one.\n")]
SKILL = [Doc("## The `x` skill", "Skill body.\n")]


def _render(**parts) -> str:
    base = dict(rules=RULES, base_sha=SHA, preamble="PREAMBLE\n", skill=SKILL, result="RESULT\n")
    return brief.render(**{**base, **parts})


def test_render_is_byte_identical_for_the_same_parts():
    first, second = _render(), _render()
    assert first == second
    assert brief.file_name("build", "build", first) == brief.file_name("build", "build", second)
    other = _render(skill=[Doc("## The `y` skill", "Other body.\n")])
    assert other != first
    assert brief.file_name("build", "build", other) != brief.file_name("build", "build", first)
    assert first.endswith("RESULT\n") and not first.endswith("\n\n")
    assert brief.digest(first) == __import__("hashlib").sha256(first.encode()).hexdigest()


def test_file_name_shape():
    text = "x"
    name = brief.file_name("re view", "b/../x", text)
    assert name == f"re_view-b____x-{brief.digest(text)[:12]}.md"
    assert brief.file_name("build", "build", text) == f"build-build-{brief.digest(text)[:12]}.md"


def test_render_orders_the_sections():
    text = _render()
    rules = text.index("# The repository's agent rules")
    lead = text.index("These are the repository's agent rules as of the commit this run started from (`0123456789ab`). "
                      "Claude Code does not load `CLAUDE.md` files itself in this session: follow these.")
    rule = text.index("## CLAUDE.md\n\nRule one.")
    preamble = text.index("PREAMBLE")
    skill_lead = text.index("The step's skill and brief files follow in full, so you already have them: do not load them.")
    skill = text.index("## The `x` skill\n\nSkill body.")
    result = text.index("RESULT")
    assert rules == 0
    assert rules < lead < rule < preamble < skill_lead < skill < result
    bare = _render(rules=[], skill=[])
    assert "# The repository's agent rules" not in bare
    assert "do not load them" not in bare
    assert bare == "PREAMBLE\n\nRESULT\n"


def test_rules_are_read_at_base_sha(tmp_path):
    repo = _repo(tmp_path)
    sha = _commit(repo, {"CLAUDE.md": "A\r\n"})
    _commit(repo, {"CLAUDE.md": "B\n"})
    _write(repo, "CLAUDE.md", "C\n")
    docs, carries = brief.read_rules(repo, sha, ["CLAUDE.md"])
    assert carries is True
    assert docs == [Doc("## CLAUDE.md", "A\n")]


def test_rules_read_every_listed_file_in_order(tmp_path):
    repo = _repo(tmp_path)
    sha = _commit(repo, {"CLAUDE.md": "claude\n", "docs/rules.md": "more\n"})
    docs, carries = brief.read_rules(repo, sha, ["docs/rules.md", "CLAUDE.md", "absent.md", "./CLAUDE.md"])
    assert carries is True
    assert docs == [Doc("## docs/rules.md", "more\n"), Doc("## CLAUDE.md", "claude\n")]


@pytest.mark.parametrize("files, rules", [
    ({"CLAUDE.md": "see @AGENTS.md\n"}, ["CLAUDE.md"]),
    ({"CLAUDE.md": "x\n", ".claude/CLAUDE.md": "y\n"}, ["CLAUDE.md"]),
    ({"CLAUDE.md": "x\n", ".claude/rules/a.md": "y\n"}, ["CLAUDE.md"]),
    ({"CLAUDE.md": "x\n", "pkg/CLAUDE.md": "y\n"}, ["CLAUDE.md"]),
    ({"CLAUDE.md": "x\n", "AGENTS.md": "y\n"}, ["AGENTS.md"]),
    ({"CLAUDE.md": "x" * (brief.BRIEF_FILE_MAX_BYTES + 1)}, ["CLAUDE.md"]),
    ({"CLAUDE.md": "x\n"}, ["CLAUDE.md", "../outside.md"]),
    ({"CLAUDE.md": "x\n", ".agentflow/rules.md": "y\n"}, ["CLAUDE.md", ".agentflow/rules.md"]),
])
def test_rules_do_not_carry_when_memory_blocks(tmp_path, files, rules):
    repo = _repo(tmp_path)
    sha = _commit(repo, files)
    assert brief.read_rules(repo, sha, rules) == ([], False)


def test_rules_do_not_carry_through_a_symlink_out_or_without_a_base(tmp_path):
    repo = _repo(tmp_path)
    _write(repo, "CLAUDE.md", "x\n")
    (repo / "AGENTS.md").symlink_to("../outside.md")
    sha = _commit(repo)
    assert brief.read_rules(repo, sha, ["CLAUDE.md", "AGENTS.md"]) == ([], False)
    assert brief.read_rules(repo, None, ["CLAUDE.md"]) == ([], False)
    assert brief.read_rules(repo, sha, []) == ([], False)
    assert brief.read_rules(repo, "HEAD", ["CLAUDE.md"]) == ([], False)


def test_rules_follow_an_inner_symlink_and_check_its_target_for_imports(tmp_path):
    repo = _repo(tmp_path)
    _write(repo, "docs/agents.md", "plain\n")
    (repo / "CLAUDE.md").symlink_to("docs/agents.md")
    sha = _commit(repo)
    assert brief.read_rules(repo, sha, ["CLAUDE.md"]) == ([Doc("## CLAUDE.md", "plain\n")], True)
    later = _commit(repo, {"docs/agents.md": "see @README\n"})
    assert brief.read_rules(repo, later, ["CLAUDE.md"]) == ([], False)


def test_a_tree_without_rules_carries_nothing(tmp_path):
    repo = _repo(tmp_path)
    sha = _commit(repo, {"README.md": "x\n"})
    assert brief.read_rules(repo, sha, ["CLAUDE.md"]) == ([], True)


def test_repo_skill_is_read_at_base_sha_and_normalised(tmp_path):
    repo = _repo(tmp_path)
    sha = _commit(repo, {".claude/skills/mine/SKILL.md": "﻿---\r\nname: mine\r\n---\r\n\r\nBody\r\n"})
    _commit(repo, {".claude/skills/mine/SKILL.md": "changed\n"})
    assert brief.read_skill(repo, sha, ".claude/skills", "mine", True) == Doc("## The `mine` skill", "Body\n")
    assert brief.read_skill(repo, sha, "./.claude/skills/", "mine", True) == Doc("## The `mine` skill", "Body\n")


def test_repo_skill_unreadable_stays_in_the_prompt(tmp_path):
    repo = _repo(tmp_path)
    sha = _commit(repo, {"README.md": "x\n"})
    _commit(repo, {".claude/skills/mine/SKILL.md": "later\n"})
    assert brief.read_skill(repo, sha, ".claude/skills", "mine", True) is None
    assert brief.read_skill(repo, None, ".claude/skills", "mine", True) is None
    for name in ("..", "../x", "-x", "a/b", ""):
        assert brief.read_skill(repo, sha, ".claude/skills", name, True) is None, name
    for skills_dir in ("../x", "/abs", "", "a\\b"):
        assert brief.read_skill(repo, sha, skills_dir, "mine", True) is None, skills_dir


def test_bundled_skill_is_read_from_the_engine(tmp_path):
    repo = _repo(tmp_path)
    found = brief.read_skill(repo, None, ".claude/skills", "agentflow-build", False)
    expected = (pack.ENGINE_SKILLS / "agentflow-build" / "SKILL.md").read_text(encoding="utf-8")
    assert found is not None and found.heading == "## The `agentflow-build` skill"
    assert found.text and found.text in expected and not found.text.startswith("---")
    assert brief.read_skill(repo, None, ".claude/skills", "no-such-skill", False) is None
    assert brief.read_skill(repo, None, ".claude/skills", "../skills", False) is None


def test_brief_files_are_read_at_base_sha(tmp_path):
    repo = _repo(tmp_path)
    sha = _commit(repo, {"docs/a.md": "A\r\n", "docs/b.md": "B\n"})
    _commit(repo, {"docs/a.md": "changed\n"})
    assert brief.read_brief_files(repo, sha, ["docs/a.md", "docs/b.md"]) == [Doc("## docs/a.md", "A\n"),
                                                                            Doc("## docs/b.md", "B\n")]
    assert brief.read_brief_files(repo, sha, []) == []
    assert brief.read_brief_files(repo, sha, ["docs/a.md", "docs/missing.md"]) is None
    assert brief.read_brief_files(repo, None, ["docs/a.md"]) is None
