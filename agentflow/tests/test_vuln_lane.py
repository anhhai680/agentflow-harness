"""The `vuln` lane in the engine defaults: its steps, its prompts and its bundled skill (the runner's behaviour for
its two expectation steps is in later tests)."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml
from conftest import SPARSE_PACK, deep_merge

from agentflow import conditions, layers, pack, pipeline, workspace
from agentflow.config import load_config, validate
from agentflow.state import Run

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


TRIAGE_RESULT = {"status": "done", "summary": "requests is direct; 2.32.0 is the smallest fix.",
                 "signals": {"triage": {"relationship": "direct", "target_version": "2.32.0", "bump": "major",
                                        "method": "bump", "release_date": "2024-05-20",
                                        "notes": "drops Python 3.7\n# not a heading"}}}


def _triage_phase(flow_repo):
    flow_repo.configure(edit=add_vuln_lane)
    run = Run.create(flow_repo.cfg, flow_repo.root, "vuln", {"module": "crm"})
    phase = flow_repo.cfg.phases["define"]
    step = next(s for s in phase.steps if s.id == "triage")
    return run, phase, step


def test_a_triage_result_reaches_the_run_dir_and_the_approval_page(flow_repo):
    from agentflow import pipeline

    run, phase, step = _triage_phase(flow_repo)
    flow_repo.script({"define/triage": [{"result": TRIAGE_RESULT}]})
    assert flow_repo.executor(run).run_steps(phase, [step], "define")[0] == "done"
    text = (run.dir / "triage.md").read_text(encoding="utf-8")
    for line in ("Relationship: direct", "Target version: 2.32.0", "Bump: major", "Method: bump",
                 "Release date: 2024-05-20", "Notes: drops Python 3.7 # not a heading"):
        assert line in text
    assert "\n# not a heading" not in text  # one line per fact: the agent's text opens no heading
    page = pipeline.approval_text(flow_repo.cfg, run, "define")
    assert "## Triage" in page and "Bump: major" in page and page.index("## Triage") < page.index("## Step results")
    workspace.prepare(run)  # the later sessions read the mirrored copy
    assert (run.agent_dir / "triage.md").read_text(encoding="utf-8") == text


def test_a_triage_result_with_no_facts_still_shows_what_is_missing(flow_repo):
    run, phase, step = _triage_phase(flow_repo)
    flow_repo.script({"define/triage": [{"result": {"status": "done", "summary": "no facts"}}]})
    assert flow_repo.executor(run).run_steps(phase, [step], "define")[0] == "done"
    text = (run.dir / "triage.md").read_text(encoding="utf-8")
    assert "Target version: (not given)" in text and "Notes:" not in text


def test_the_triage_file_is_a_run_file_the_dashboard_lists(flow_repo):
    from agentflow import run_files

    run, phase, step = _triage_phase(flow_repo)
    flow_repo.script({"define/triage": [{"result": TRIAGE_RESULT}]})
    flow_repo.executor(run).run_steps(phase, [step], "define")
    assert "triage.md" in [f["name"] for f in run_files.list_files(run)]


def test_triage_and_patch_leave_a_major_bump_to_the_approval():
    triage = (PROMPTS / "vuln_triage.md").read_text(encoding="utf-8")
    skill = (SKILLS / "agentflow-patch" / "SKILL.md").read_text(encoding="utf-8")
    assert "A major bump is not a stop" in triage and "allow for" not in triage
    assert "approval of the bump" in skill
    assert "A major bump in it is approved" in (PROMPTS / "vuln_patch.md").read_text(encoding="utf-8")


def test_only_a_package_and_a_version_may_reach_a_command():
    for text in ((PROMPTS / "vuln_patch.md").read_text(encoding="utf-8"),
                 (SKILLS / "agentflow-patch" / "SKILL.md").read_text(encoding="utf-8")):
        assert "never put" in text.lower() or "never go into a command" in text
        assert "matched them against the manifest and the lockfile" in text
