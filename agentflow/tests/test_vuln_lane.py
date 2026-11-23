"""The `vuln` lane in the engine defaults: its steps, its prompts and its bundled skill (the runner's behaviour for
its two expectation steps is in later tests)."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml
from conftest import SPARSE_PACK, deep_merge

from agentflow import conditions, layers, pack, pipeline
from agentflow.config import load_config, validate

PROMPTS = Path(__file__).resolve().parents[1] / "prompts"
SKILLS = Path(__file__).resolve().parents[1] / "skills"


def _defaults() -> dict:
    return yaml.safe_load(layers.ENGINE_DEFAULTS.read_text(encoding="utf-8"))


def vuln_steps() -> dict[str, list[dict]]:
    """Per phase, the engine defaults' steps whose `lanes` include `vuln`, in their order."""
    out: dict[str, list[dict]] = {}
    for name, phase in _defaults()["phases"].items():
        steps = [copy.deepcopy(s) for s in phase["steps"] if "vuln" in (s.get("lanes") or [])]
        if steps:
            out[name] = steps
    return out


def add_vuln_lane(data: dict) -> None:
    """A `FlowRepo.configure(edit=...)` callback: add `vuln` to a pack that writes its own `phases:`. A step the pack
    already has (`setup`, `verify`, `plan_fixes`, `fix_findings`) gains `vuln` in its `lanes`; a new one goes in
    right after the vuln step before it in its phase (the first one at the start), so `advisory_red` and `patch`
    follow `setup` and `vuln_review` follows `fix_findings`."""
    if "vuln" not in data["lanes"]:
        data["lanes"].append("vuln")
    skills = data.setdefault("skills", {}).setdefault("map", {})
    for key, name in (("patch", "agentflow-patch"), ("review", "agentflow-review")):
        skills.setdefault(key, name)
    for phase, wanted in vuln_steps().items():
        steps = data["phases"][phase]["steps"]
        anchor = -1
        for step in wanted:
            have = next((i for i, s in enumerate(steps) if s.get("id") == step["id"]), None)
            if have is None:
                steps.insert(anchor + 1, copy.deepcopy(step))
                have = anchor + 1
            elif steps[have].get("lanes") and "vuln" not in steps[have]["lanes"]:
                steps[have]["lanes"].append("vuln")
            anchor = have


def _sparse_with_probe(sparse_repo: Path, probe: str | None = "unit"):
    data = deep_merge(SPARSE_PACK, {"vuln": {"probe": probe}} if probe else {})
    target = pack.pack_config(sparse_repo)
    target.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return load_config(target)


def test_explain_vuln_on_engine_defaults(sparse_repo):
    cfg = _sparse_with_probe(sparse_repo)
    ctx = conditions.make_ctx(cfg, sparse_repo, "vuln", {"alert": "42"})
    lines = conditions.explain(cfg, "vuln", ctx)
    steps = [line.key for line in lines if line.key != line.phase and not line.key.endswith("_approved")
             and line.verdict == "run"]
    assert steps == ["define/triage", "build/setup", "build/advisory_red", "build/patch", "verify/setup",
                     "verify/verify", "review/vuln_review", "ship/finish", "ship/ci"]
    shown = {line.key for line in lines}  # a step of another lane is not listed at all
    assert not shown & {"review/branch_review", "define/debug", "build/regression_test", "build/fix"}


def test_sparse_pack_validates_with_vuln_lane_and_no_probe(sparse_repo):
    cfg = _sparse_with_probe(sparse_repo, probe=None)
    assert "vuln" in cfg.lanes and cfg.vuln.probe == ""
    assert [i for i in validate(cfg, sparse_repo) if i.level == "error"] == []
    assert [i for i in validate(cfg, sparse_repo) if "vuln" in i.path or "vuln" in i.message] == []


def test_run_vuln_refuses_without_probe(sparse_repo):
    cfg = _sparse_with_probe(sparse_repo, probe=None)
    with pytest.raises(ValueError, match=r"vuln\.probe"):
        pipeline.start(cfg, sparse_repo, "vuln", {"alert": "42"}, pipeline.RunOptions())
    runs = pack.state_dir(sparse_repo) / "runs"
    assert not runs.exists() or not list(runs.iterdir())


def test_validate_refuses_vuln_lane_without_its_steps(tmp_cfg):
    def lane_only(data):
        data["lanes"].append("vuln")

    base = tmp_cfg(None)
    before = validate(load_config(base), base.parent.parent)  # the fixture pack's own warnings (no agent file here)

    path = tmp_cfg(lane_only)
    issues = validate(load_config(path), path.parent.parent)
    [issue] = [i for i in issues if i.path == "lanes" and i.level == "error"]
    assert "advisory_red" in issue.message and "patch" in issue.message

    path = tmp_cfg(add_vuln_lane)
    assert validate(load_config(path), path.parent.parent) == before


def test_prompts_point_at_the_alert_file():
    for name in ("vuln_triage.md", "vuln_patch.md", "vuln_review.md"):
        assert "{run_dir}/alert.md" in (PROMPTS / name).read_text(encoding="utf-8")


def test_the_patch_skill_is_bundled_and_the_lane_steps_use_it():
    found = pack.skill_file(Path("/nonexistent"), ".claude/skills", "agentflow-patch")
    assert found is not None and pack.is_bundled(found)
    text = (SKILLS / "agentflow-patch" / "SKILL.md").read_text(encoding="utf-8")
    assert text.startswith("---\nname: agentflow-patch\n")
    steps = vuln_steps()
    assert [s["id"] for s in steps["define"]] == ["triage"]
    assert [s["id"] for s in steps["build"]] == ["setup", "advisory_red", "patch"]
    assert [s["id"] for s in steps["review"]][-1] == "vuln_review"
    assert {s["id"]: s["skill"] for s in steps["build"] if "skill" in s} == {"patch": "@skills.map.patch"}


def test_review_blocks_on_high_for_a_fresh_target_version():
    text = (PROMPTS / "vuln_review.md").read_text(encoding="utf-8")
    assert "7 days" in text and "release_date" in text and "release-age" in text
