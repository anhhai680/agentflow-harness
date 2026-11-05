from __future__ import annotations

from agentflow import pack
from agentflow.config import load_config, validate


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
