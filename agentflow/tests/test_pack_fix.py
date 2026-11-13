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


# ---- validate --fix


def cli(repo: Path, monkeypatch, capsys, *args: str) -> tuple[int, str]:
    monkeypatch.chdir(repo)
    code = flow.main(["validate", *args])
    return code, capsys.readouterr().out


def test_dry_run_prints_the_diff_and_writes_nothing(pack_repo_python, monkeypatch, capsys):
    path = put(pack_repo_python, "_result.md", OLD_RESULT + "\nPack wording.\n")
    before = path.read_bytes()
    code, out = cli(pack_repo_python, monkeypatch, capsys, "--fix", "--dry-run")
    assert code == 0
    assert "--- a/.agentflow/prompts/_result.md" in out
    assert any(line.startswith("-") and "effort_change" in line for line in out.splitlines())
    assert "nothing written" in out
    assert path.read_bytes() == before


def test_fix_rewrites_then_validates_clean(pack_repo_python, monkeypatch, capsys, git):
    path = put(pack_repo_python, "_result.md", OLD_RESULT + "\nPack wording.\n")
    git(pack_repo_python, "add", "-A")
    git(pack_repo_python, "commit", "-q", "-m", "old override")
    code, out = cli(pack_repo_python, monkeypatch, capsys, "--fix")
    assert code == 0, out
    assert path.read_text(encoding="utf-8") == engine("_result.md") + "\nPack wording.\n"
    assert out.splitlines()[-1] == "ok: config.yaml is valid (0 warnings)"
    assert git(pack_repo_python, "status", "--porcelain").splitlines() == [" M .agentflow/prompts/_result.md"]


def test_fix_deletes_identical_and_landed_overrides(pack_repo_python, monkeypatch, capsys):
    task = put(pack_repo_python, "task.md", engine("task.md"))
    result = put(pack_repo_python, "_result.md", OLD_RESULT)
    code, out = cli(pack_repo_python, monkeypatch, capsys, "--fix")
    assert code == 0, out
    assert not task.exists() and not result.exists()
    assert "delete .agentflow/prompts/task.md" in out


def test_fix_leaves_edited_text(pack_repo_python, monkeypatch, capsys):
    path = put(pack_repo_python, "_result.md", EDITED_RESULT)
    code, out = cli(pack_repo_python, monkeypatch, capsys, "--fix")
    assert code == 0, out
    assert path.read_text(encoding="utf-8") == EDITED_RESULT
    assert "which the engine retired" in out


def test_changed_since_read_is_skipped(pack_repo_python):
    path = put(pack_repo_python, "_result.md", OLD_RESULT + "\nPack wording.\n")
    fixes = found(pack_repo_python).fixes
    path.write_text("edited meanwhile\n", encoding="utf-8")
    applied, skipped = pack_fix.apply(fixes)
    assert applied == []
    assert [(s.path, s.reason) for s in skipped] == [(path, "changed since it was read")]
    assert path.read_text(encoding="utf-8") == "edited meanwhile\n"


def test_apply_refuses_a_path_outside_the_prompts(pack_repo_python, tmp_path):
    outside = tmp_path / "elsewhere.md"
    outside.write_text("x", encoding="utf-8")
    applied, skipped = pack_fix.apply([pack_fix.Fix(outside, b"x", None)])
    assert applied == [] and [s.reason for s in skipped] == ["outside .agentflow/prompts"]
    assert outside.exists()


def test_symlinked_prompts_dir_is_refused(pack_repo_python, monkeypatch, capsys, tmp_path):
    target = tmp_path / "shared-prompts"
    target.mkdir()
    (target / "_result.md").write_text(OLD_RESULT, encoding="utf-8")
    folder = prompts(pack_repo_python)
    for file in folder.iterdir():
        file.unlink()
    folder.rmdir()
    os.symlink(target, folder)
    code, out = cli(pack_repo_python, monkeypatch, capsys, "--fix")
    assert code == 1
    assert "is a symlink" in out
    assert (target / "_result.md").read_text(encoding="utf-8") == OLD_RESULT


def test_refused_inside_a_run(pack_repo_python, monkeypatch, capsys):
    path = put(pack_repo_python, "_result.md", OLD_RESULT)
    monkeypatch.setenv("AGENTFLOW_RUN", "r1")
    code, out = cli(pack_repo_python, monkeypatch, capsys, "--fix")
    assert code == 1
    assert "never runs inside an agentflow run" in out
    assert path.read_text(encoding="utf-8") == OLD_RESULT


def test_fix_with_config_is_refused(pack_repo_python, monkeypatch, capsys):
    code, out = cli(pack_repo_python, monkeypatch, capsys, "--fix", "--config", str(pack.pack_config(pack_repo_python)))
    assert code == 1
    assert "works only on the repository's own pack" in out


def test_dry_run_without_fix_is_a_usage_error(pack_repo_python, monkeypatch, capsys):
    with pytest.raises(SystemExit) as exc:
        cli(pack_repo_python, monkeypatch, capsys, "--dry-run")
    assert exc.value.code == 2


def test_fix_names_skipped_files(pack_repo_python, monkeypatch, capsys):
    os.symlink(pack.ENGINE_PROMPTS / "task.md", prompts(pack_repo_python) / "x.md")
    code, out = cli(pack_repo_python, monkeypatch, capsys, "--fix")
    assert "skipped .agentflow/prompts/x.md: a symlink" in out
