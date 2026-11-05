from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from agentflow import pack
from agentflow.config import ConfigError, load_config, validate
from agentflow.config_trust import ConfigUntrusted, trusted_config_path
from agentflow.state import Run
from conftest import PYTHON_CONFIG, deep_merge, run_git


def write_pack(root: Path, patch: dict) -> Path:
    """Commit `<root>/.agentflow/config.yaml`: the `python` pack's config with `patch` deep-merged."""
    data = deep_merge(yaml.safe_load(PYTHON_CONFIG.read_text(encoding="utf-8")), patch)
    target = root / ".agentflow" / "config.yaml"
    target.parent.mkdir(exist_ok=True)
    target.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
    run_git(root, "add", "-A")
    run_git(root, "commit", "-q", "-m", "Add the agentflow pack")
    return target


def make_worktree(root: Path, git) -> Path:
    """A linked worktree of `root` on a new branch `other`, beside it."""
    wt = root.parent / f"{root.name}-wt"
    git(root, "worktree", "add", "-q", "-b", "other", str(wt))
    return wt


def test_pack_prompt_wins_over_engine_default(tmp_path):
    (tmp_path / "prompts").mkdir(); (tmp_path / "prompts/step.md").write_text("pack")
    assert pack.prompt_path(tmp_path / "config.yaml", "step.md") == tmp_path / "prompts/step.md"
    assert pack.prompt_path(tmp_path / "config.yaml", "debug.md") == pack.ENGINE_PROMPTS / "debug.md"
    assert pack.prompt_path(tmp_path / "config.yaml", "nope.md") is None


def test_prompt_files_and_labels(tmp_path):
    config = tmp_path / "config.yaml"
    (tmp_path / "prompts").mkdir(); (tmp_path / "prompts/step.md").write_text("pack")
    files = pack.prompt_files(config)
    assert files["step.md"] == tmp_path / "prompts/step.md"
    assert files["debug.md"] == pack.ENGINE_PROMPTS / "debug.md"
    assert pack.prompt_label(config, files["step.md"]) == "prompts/step.md"
    assert pack.prompt_label(config, files["debug.md"]) == "engine:prompts/debug.md"


def test_bad_placeholder_in_a_pack_override_is_reported_against_the_pack_file(tmp_cfg, repo_root):
    path = tmp_cfg()
    (path.parent / "prompts" / "verify.md").write_text("{not_a_placeholder}\n{brief}\n")
    issues = validate(load_config(path), repo_root)
    assert any(i.path == "prompts/verify.md" and "not_a_placeholder" in i.message for i in issues)


def test_override_without_brief_is_reported_even_though_the_engine_default_has_one(tmp_cfg, repo_root):
    path = tmp_cfg()
    (path.parent / "prompts" / "verify.md").write_text("no brief here\n")
    assert any("prompts/verify.md has no {brief}" in i.message for i in validate(load_config(path), repo_root))


def test_step_naming_an_unknown_prompt_is_an_error(tmp_cfg, repo_root):
    def patch(data):
        next(s for s in data["phases"]["verify"]["steps"] if s["id"] == "verify")["prompt"] = "nope.md"

    issues = validate(load_config(tmp_cfg(patch)), repo_root)
    assert any("is not in the pack's prompts/ or the engine's" in i.message for i in issues)


# --------------------------------------------------------------------------- where the pack is, and which it is


def test_pack_config_from_a_linked_worktree_is_the_main_checkouts(tmp_git, git):
    write_pack(tmp_git, {})
    wt = make_worktree(tmp_git, git)
    (wt / ".agentflow" / "config.yaml").write_text("version: 2\n", encoding="utf-8")  # the other branch's copy
    assert pack.pack_config(wt) == (tmp_git / ".agentflow/config.yaml").resolve()
    assert pack.pack_config(wt / "src") == (tmp_git / ".agentflow/config.yaml").resolve()  # from a subdirectory too


def test_missing_pack_names_the_path(tmp_git):
    with pytest.raises(pack.PackError, match=r"no \.agentflow/config\.yaml in "):
        pack.pack_config(tmp_git)


@pytest.mark.parametrize("spec,ok", [(">=2,<3", True), (">=2.1", False), ("==2.0", True), ("<2", False), (">1", True),
                                     ("<=2", True), (">= 2 , < 3", True)])
def test_engine_satisfies(spec, ok):
    assert pack.engine_satisfies(spec, "2.0") is ok


@pytest.mark.parametrize("spec", ["~2", "", "2", ">=", ">=2,", ">=two", "=>2"])
def test_engine_spec_garbage_is_refused(spec):
    with pytest.raises(pack.PackError):
        pack.engine_satisfies(spec)


def test_version_1_pack_is_refused(tmp_cfg):
    with pytest.raises(ConfigError, match="pack format 1; this engine reads format 2"):
        load_config(tmp_cfg({"version": 1}))


def test_engine_range_that_excludes_this_engine_is_refused(tmp_cfg):
    with pytest.raises(ConfigError, match=r"engine: '>=3' .* this engine is 2\.0"):
        load_config(tmp_cfg({"engine": ">=3"}))


def test_engine_range_that_does_not_parse_is_refused(tmp_cfg):
    with pytest.raises(ConfigError) as exc:
        load_config(tmp_cfg({"engine": "~2"}))
    assert exc.value.path == "engine"


def test_pack_without_an_engine_range_is_refused(tmp_cfg):
    with pytest.raises(ConfigError) as exc:
        load_config(tmp_cfg({"engine": None}))
    assert exc.value.path == "engine"


def test_pack_dir_is_forbidden_even_when_the_pack_omits_it(tmp_cfg):
    cfg = load_config(tmp_cfg(lambda d: d["guardrails"].__setitem__("forbidden_paths", ["X.md"])))
    assert cfg.guardrails.forbidden_paths == ["X.md", ".agentflow/**"]


def test_pack_that_names_the_pack_dir_itself_keeps_one_entry(tmp_cfg):
    cfg = load_config(tmp_cfg(lambda d: d["guardrails"].__setitem__("forbidden_paths", [".agentflow/**", "X.md"])))
    assert cfg.guardrails.forbidden_paths == [".agentflow/**", "X.md"]


def test_pack_without_a_verify_gate_set_is_refused(tmp_cfg, repo_root):
    cfg = load_config(tmp_cfg(lambda d: d["gate_sets"].pop("verify")))
    assert any(i.level == "error" and i.path == "gate_sets.verify" for i in validate(cfg, repo_root))


def test_pack_with_an_empty_verify_gate_set_is_refused(tmp_cfg, repo_root):
    cfg = load_config(tmp_cfg(lambda d: d["gate_sets"].__setitem__("verify", [])))
    assert any(i.level == "error" and i.path == "gate_sets.verify" for i in validate(cfg, repo_root))


def test_resuming_a_run_whose_config_is_gone_names_the_pack(pack_repo_python, tmp_path, cfg):
    run = Run.create(cfg, pack_repo_python, "feature", {"module": "greet"})
    gone = tmp_path / "agentflow" / "config.yaml"  # where the engine kept its config before the pack
    run.emit("run_options", config=str(gone), config_sha256="0" * 64)
    with pytest.raises(ConfigUntrusted, match=r"--config \.agentflow/config\.yaml"):
        trusted_config_path(run, None, pack.pack_config(run.repo))


def test_python_pack_validates_clean(pack_repo_python):
    assert validate(load_config(pack.pack_config(pack_repo_python)), pack_repo_python) == []


def test_cli_inside_a_linked_worktree_reads_the_main_checkouts_pack(pack_repo_python, git, monkeypatch, capsys):
    from agentflow import flow

    wt = make_worktree(pack_repo_python, git)
    (wt / ".agentflow" / "config.yaml").write_text("version: 1\n", encoding="utf-8")  # the worktree's copy: unusable
    monkeypatch.chdir(wt)
    assert flow.main(["validate"]) == 0, capsys.readouterr().out
    assert "ok: config.yaml is valid" in capsys.readouterr().out


def test_cli_without_a_pack_says_so(tmp_git, monkeypatch, capsys):
    from agentflow import flow

    monkeypatch.chdir(tmp_git)
    assert flow.main(["validate"]) == 1
    assert "no .agentflow/config.yaml in " in capsys.readouterr().out
    assert flow.main(["explain", "feature", "--module", "greet"]) == 1
    assert "this repository has no agentflow pack" in capsys.readouterr().out
