"""`validate`'s drift check on pack prompts, and `validate --fix`: retired engine text and redundant overrides."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from agentflow import flow, pack, pack_fix
from agentflow.config import load_config, validate


def engine(name: str) -> str:
    return (pack.ENGINE_PROMPTS / name).read_text(encoding="utf-8")


BLOCK = next(e for e in pack_fix.RETIRED if e.id == "effort-check-block")
STATUS = next(e for e in pack_fix.RETIRED if e.id == "effort-change-status")
OLD_PREAMBLE = engine("_preamble.md").replace("## Rules for this step", BLOCK.old + "## Rules for this step", 1)
OLD_RESULT = engine("_result.md").replace(STATUS.new, STATUS.old)
EDITED_RESULT = engine("_result.md") + "\n- also `effort_change` when unsure\n"


def prompts(repo: Path) -> Path:
    return repo / ".agentflow" / "prompts"


def put(repo: Path, name: str, text: str | bytes) -> Path:
    path = prompts(repo) / name
    if isinstance(text, bytes):
        path.write_bytes(text)
    else:
        path.write_bytes(text.encode("utf-8"))
    return path


def found(repo: Path) -> pack_fix.Findings:
    return pack_fix.findings(pack.pack_config(repo))


def drift_issues(repo: Path) -> list:
    return [i for i in validate(load_config(pack.pack_config(repo)), repo) if i.path.startswith("prompts/")]


# ---- the register


def test_register_matches_the_engine_prompts():
    texts = {f.name: f.read_text(encoding="utf-8") for f in pack.ENGINE_PROMPTS.glob("*.md")}
    for entry in pack_fix.RETIRED:
        for name, text in texts.items():
            assert entry.old not in text, (entry.id, name)
            assert entry.detect not in text, (entry.id, name)
        if entry.new:
            assert entry.new in texts[entry.file], entry.id


# ---- findings


def test_old_result_line_is_fixable(pack_repo_python):
    put(pack_repo_python, "_result.md", OLD_RESULT + "\nPack wording.\n")
    got = found(pack_repo_python)
    assert len(got.issues) == 1
    issue = got.issues[0]
    assert (issue.level, issue.path, issue.fixable) == ("warning", "prompts/_result.md", True)
    assert "2.5" in issue.message
    assert len(got.fixes) == 1
    assert got.fixes[0].after.decode("utf-8") == engine("_result.md") + "\nPack wording.\n"
    assert drift_issues(pack_repo_python) == got.issues


def test_old_preamble_block_is_one_fixable_issue(pack_repo_python):
    put(pack_repo_python, "_preamble.md", OLD_PREAMBLE + "\nPack rule.\n")
    got = found(pack_repo_python)
    assert [i.fixable for i in got.issues] == [True]
    assert b"effort" not in got.fixes[0].after
    assert got.fixes[0].after.decode("utf-8") == engine("_preamble.md") + "\nPack rule.\n"


def test_fix_that_lands_on_the_engine_text_deletes(pack_repo_python):
    put(pack_repo_python, "_result.md", OLD_RESULT)
    got = found(pack_repo_python)
    assert [i.fixable for i in got.issues] == [True]
    assert got.fixes[0].after is None


def test_edited_retired_text_is_not_fixable(pack_repo_python):
    put(pack_repo_python, "_result.md", EDITED_RESULT)
    got = found(pack_repo_python)
    assert [i.fixable for i in got.issues] == [False]
    assert "effort_change" in got.issues[0].message
    assert got.fixes == []


def test_identical_override_is_fixable(pack_repo_python):
    put(pack_repo_python, "task.md", engine("task.md"))
    got = found(pack_repo_python)
    assert [(i.path, i.fixable) for i in got.issues] == [("prompts/task.md", True)]
    assert "identical" in got.issues[0].message
    assert got.fixes[0].after is None


def test_crlf_and_repeated_text(pack_repo_python):
    put(pack_repo_python, "_result.md", (OLD_RESULT + "\n" + STATUS.old + "\n").replace("\n", "\r\n"))
    after = found(pack_repo_python).fixes[0].after
    assert b"effort_change" not in after
    assert b"\r\n" in after
    assert b"\n" not in after.replace(b"\r\n", b"")


def test_unreadable_prompts_are_skipped(pack_repo_python):
    os.symlink(pack.ENGINE_PROMPTS / "task.md", prompts(pack_repo_python) / "x.md")
    put(pack_repo_python, "big.md", b"a" * 2_000_000)
    put(pack_repo_python, "bad.md", b"\xff\xfe")
    got = found(pack_repo_python)
    assert sorted(s.path.name for s in got.skipped) == ["bad.md", "big.md", "x.md"]
    assert got.issues == [] and got.fixes == []
    drift_issues(pack_repo_python)  # does not raise


def test_python_pack_has_no_drift(pack_repo_python):
    got = found(pack_repo_python)
    assert (got.issues, got.fixes, got.skipped) == ([], [], [])


def test_cli_marks_fixable_and_hints(pack_repo_python, monkeypatch, capsys):
    put(pack_repo_python, "_result.md", OLD_RESULT + "\nPack wording.\n")
    monkeypatch.chdir(pack_repo_python)
    assert flow.main(["validate"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert any(line.startswith("warning prompts/_result.md: ") and line.endswith(" (fixable)") for line in lines)
    assert lines[-1] == "1 fixable: run agentflow validate --fix"
