from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from agentflow import pack
from agentflow.config import ConfigError, Issue, load_config, validate
from agentflow.flow import main, repo_root
from conftest import PYTHON_CONFIG

CFG = PYTHON_CONFIG


def issues_for(tmp_cfg, patch) -> list[Issue]:
    path = tmp_cfg(patch)
    return validate(load_config(path), path.parent.parent)


def errors(issues: list[Issue]) -> list[tuple[str, str]]:
    return [(i.level, i.path) for i in issues if i.level == "error"]


def phase_step(data: dict, phase: str, step_id: str) -> dict:
    return next(s for s in data["phases"][phase]["steps"] if s["id"] == step_id)


def test_shipped_config_validates(repo_root):
    issues = validate(load_config(CFG), repo_root)
    assert [i for i in issues if i.level == "error"] == []


def test_package_design_belongs_to_the_plan_not_the_spec():
    def skills(steps):
        for step in steps:
            yield step.skill
            yield from skills(step.steps)

    assert "codebase-design" not in set(skills(load_config(CFG).phases["define"].steps))
    assert "`codebase-design` skill" in (CFG.parent / "prompts" / "plan.md").read_text(encoding="utf-8")


def test_eval_steps_map_to_a_verify_skill_and_lanes_hold_no_repo_paths():
    cfg = load_config(CFG)
    gates = [next(s for s in cfg.phases[name].steps if s.id == "eval_gate") for name in ("verify", "review")]
    for step in gates:
        assert step.type == "gate" and step.skill == "verification-before-completion"
        assert step.prompt == "eval_verify.md" and step.on_fail == "block" and step.gates == ["floor", "eval"]
    assert cfg.raw["overlays"]["eval"]["when_paths"]  # the repo's eval inputs live with the overlays
    assert cfg.raw["conditions"]["eval_paths_touched"]["globs"] == cfg.raw["overlays"]["eval"]["when_paths"]


def test_agent_prices_are_validated(tmp_cfg):
    cfg = load_config(CFG)
    assert set(cfg.agents["claude"].prices) == {"claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"}
    assert cfg.agents["codex"].prices == {}
    bad_key = {"agents": {"claude": {"prices": {"claude-opus-5-5": {"inptu": 4}}}}}
    bad_value = {"agents": {"claude": {"prices": {"claude-opus-5-5": {"input": -1}}}}}
    for patch in (bad_key, bad_value):
        with pytest.raises(ConfigError):
            load_config(tmp_cfg(patch))


def test_reference_resolution():
    cfg = load_config(CFG)
    assert next(s for s in cfg.phases["build"].steps if s.id == "build").foreach == "plan_task"
    assert cfg.phases["plan"].steps[0].skill == "writing-plans"
    assert cfg.phases["plan"].steps[0].path == "phases.plan.steps[0]"
    assert cfg.raw["conditions"]["eval_paths_touched"]["globs"] == cfg.raw["overlays"]["eval"]["when_paths"]


def test_chained_references_resolve(tmp_cfg):
    shared = [{"id": "plan", "skill": "writing-plans", "role": "plan"}]
    path = tmp_cfg({"shared": {"real": shared, "alias": "@shared.real"}, "phases": {"plan": {"steps": "@shared.alias"}}})
    cfg = load_config(path)
    assert cfg.phases["plan"].steps[0].skill == "writing-plans"
    # a referenced step keeps the YAML path of where it is defined
    assert cfg.phases["plan"].steps[0].path == "shared.real[0]"


def test_step_types_follow_d4():
    cfg = load_config(CFG)
    build = cfg.phases["build"].steps
    assert [s.type for s in build] == ["gate", "agent", "agent", "agent", "agent"]
    assert build[1].foreach == "plan_task" and build[1].commit == "per_task"
    assert build[1].review is not None and build[1].review.role == "review"
    assert [s.type for s in cfg.phases["verify"].steps] == ["gate", "agent", "agent", "agent", "gate"]
    assert [s.type for s in cfg.phases["ship"].steps] == ["commit", "finish", "ci"]  # design commits before it pushes
    group = next(s for s in cfg.phases["define"].steps if s.id == "grill_open_questions")
    assert group.type == "group" and group.repeat["max_rounds"] == 2
    assert [s.type for s in group.steps] == ["agent", "checkpoint", "agent"]


def test_step_id_defaults_to_skill_then_type(tmp_cfg):
    def patch(data):
        data["phases"]["ship"]["steps"] = [{"skill": "writing-plans", "role": "plan"}, {"type": "finish"}]

    cfg = load_config(tmp_cfg(patch))
    steps = cfg.phases["ship"].steps
    assert [s.id for s in steps] == ["writing-plans", "finish"]


def test_unknown_step_key_is_a_config_error(tmp_cfg):
    def patch(data):
        phase_step(data, "define", "draft")["colour"] = "red"

    with pytest.raises(ConfigError) as e:
        load_config(tmp_cfg(patch))
    assert e.value.path == "phases.define.steps[3].colour"


def test_step_without_a_type_is_a_config_error(tmp_cfg):
    def patch(data):
        data["phases"]["ship"]["steps"] = [{"id": "mystery"}]

    with pytest.raises(ConfigError) as e:
        load_config(tmp_cfg(patch))
    assert e.value.path == "phases.ship.steps[0]"


def test_missing_section_is_a_config_error(tmp_cfg):
    with pytest.raises(ConfigError) as e:  # `gates: null` in the file: it deletes the engine defaults' `gates: {}` too
        load_config(tmp_cfg(lambda data: data.__setitem__("gates", None)))
    assert e.value.path == "gates"


def test_bad_reference_names_path(tmp_cfg):
    with pytest.raises(ConfigError) as e:
        load_config(tmp_cfg({"phases": {"plan": {"steps": "@nowhere.nope"}}}))
    assert e.value.path == "phases.plan.steps"


def test_reference_cycle_is_an_error(tmp_cfg):
    patch = {"loop": {"a": "@loop.b", "b": "@loop.a"}}
    with pytest.raises(ConfigError) as e:
        load_config(tmp_cfg(patch))
    assert "cycle" in str(e.value)
    assert e.value.path.startswith("loop.")


def test_reference_to_own_ancestor_is_a_cycle(tmp_cfg):
    def patch(data):
        data["phases"]["ship"]["steps"] = [{"id": "again", "steps": "@phases.ship.steps"}]

    with pytest.raises(ConfigError) as e:
        load_config(tmp_cfg(patch))
    assert "cycle" in str(e.value)


def test_role_map_without_default(tmp_cfg):
    issues = issues_for(tmp_cfg, {"phases": {"build": {"model": {"default": None, "review": "opus"}}}})
    assert ("error", "phases.build.model") in errors(issues)


def test_model_for_disabled_agent(tmp_cfg):
    issues = issues_for(tmp_cfg, {"phases": {"review": {"agent": "codex", "model": "gpt-5-codex"}}})
    assert ("error", "phases.review.model") in errors(issues)


def test_phase_agent_must_be_defined(tmp_cfg):
    issues = issues_for(tmp_cfg, {"phases": {"review": {"agent": "ghost"}}})
    assert ("error", "phases.review.agent") in errors(issues)


@pytest.mark.parametrize(
    "patch",
    [
        {"phase_approval": {"skip": ["plan"]}},
        {"phase_approval": {"enabled": False}},
        {"checkpoints": {"plan_approved": "auto"}},
        {"phases": {"plan": {"approval": False}}},
        {"phases": {"plan": {"auto_if": "x"}}},
    ],
)
def test_cannot_skip_phase_approval(tmp_cfg, patch):
    issues = issues_for(tmp_cfg, patch)
    assert any(i.level == "error" and "approval" in i.message for i in issues)


def test_approval_step_id_is_rejected(tmp_cfg):
    def patch(data):
        data["phases"]["ship"]["steps"] = [{"id": "build_approved", "type": "checkpoint"}]

    issues = issues_for(tmp_cfg, patch)
    assert any(i.level == "error" and "approval" in i.message for i in issues)


def test_undefined_condition_named(tmp_cfg):
    def patch(data):
        phase_step(data, "define", "contracts")["when"] = "nope"

    issues = issues_for(tmp_cfg, patch)
    assert ("error", "phases.define.steps[4].when") in errors(issues)
    assert any("nope" in i.message for i in issues if i.path == "phases.define.steps[4].when")


def test_inline_conditions_are_accepted(tmp_cfg):
    def patch(data):
        step = phase_step(data, "define", "contracts")
        step["when"] = {"all": [{"check": "file_exists", "path": "x"}, {"not": "spec_exists"}]}

    assert errors(issues_for(tmp_cfg, patch)) == []


def test_bad_inline_condition_is_an_error(tmp_cfg):
    def patch(data):
        phase_step(data, "define", "contracts")["when"] = {"check": "vibes"}

    assert ("error", "phases.define.steps[4].when.check") in errors(issues_for(tmp_cfg, patch))


def test_undefined_skip_if_and_until(tmp_cfg):
    def patch(data):
        data["phases"]["plan"]["skip_if"] = "ghost"
        group = phase_step(data, "define", "grill_open_questions")
        group["repeat"]["until"] = {"not": "ghost2"}

    found = errors(issues_for(tmp_cfg, patch))
    assert ("error", "phases.plan.skip_if") in found
    assert ("error", "phases.define.steps[5].repeat.until.not") in found


def test_condition_cycle_is_an_error(tmp_cfg):
    issues = issues_for(tmp_cfg, {"conditions": {"ping": {"not": "pong"}, "pong": {"not": "ping"}}})
    assert any(i.level == "error" and i.path.startswith("conditions.p") and "cycle" in i.message for i in issues)


def test_repeat_without_max_rounds(tmp_cfg):
    def patch(data):
        del phase_step(data, "define", "grill_open_questions")["repeat"]["max_rounds"]

    assert ("error", "phases.define.steps[5].repeat.max_rounds") in errors(issues_for(tmp_cfg, patch))


def test_goto_missing_step(tmp_cfg):
    def patch(data):
        phase_step(data, "define", "draft")["next"] = [{"when": "spec_exists", "goto": "nope"}]

    assert ("error", "phases.define.steps[3].next[0].goto") in errors(issues_for(tmp_cfg, patch))


def test_goto_existing_step_is_fine(tmp_cfg):
    def patch(data):
        phase_step(data, "define", "draft")["next"] = [{"when": "spec_exists", "goto": "contracts"}]

    assert errors(issues_for(tmp_cfg, patch)) == []


def test_duplicate_step_id(tmp_cfg):
    def patch(data):
        phase_step(data, "define", "contracts")["id"] = "draft"

    assert ("error", "phases.define.steps[4].id") in errors(issues_for(tmp_cfg, patch))


def test_user_only_skill_as_step(tmp_cfg):
    def patch(data):
        phase_step(data, "define", "contracts")["skill"] = "writing-for-agents"

    issues = issues_for(tmp_cfg, patch)
    assert ("error", "phases.define.steps[4].skill") in errors(issues)
    assert any("user-only" in i.message for i in issues)


def test_missing_skill_dir(tmp_cfg):
    def patch(data):
        phase_step(data, "define", "contracts")["skill"] = "no-such-skill"

    assert ("error", "phases.define.steps[4].skill") in errors(issues_for(tmp_cfg, patch))


def test_missing_prompt_template(tmp_cfg):
    def patch(data):
        phase_step(data, "define", "contracts")["prompt"] = "nope.md"

    assert ("error", "phases.define.steps[4].prompt") in errors(issues_for(tmp_cfg, patch))


def test_nested_review_step_is_checked(tmp_cfg):
    def patch(data):
        phase_step(data, "build", "build")["review"]["prompt"] = "nope.md"

    assert ("error", "phases.build.steps[1].review.prompt") in errors(issues_for(tmp_cfg, patch))


def test_small_plan_skill_and_condition_are_checked(tmp_cfg):
    def patch(data):
        small = phase_step(data, "build", "build")["small_plan"]
        small["skill"] = "no-such-skill"
        small["when"] = "ghost"

    found = errors(issues_for(tmp_cfg, patch))
    assert ("error", "phases.build.steps[1].small_plan.skill") in found
    assert ("error", "phases.build.steps[1].small_plan.when") in found


@pytest.mark.parametrize("key, value", [("commit", "per_task"), ("push", True), ("pr", "draft")])
def test_removed_defaults_are_refused(tmp_cfg, key, value):
    # read but never acted on: `finish` always pushes and opens a draft PR, and steps say when to commit
    assert ("error", f"defaults.{key}") in errors(issues_for(tmp_cfg, {"defaults": {key: value}}))


def test_unknown_gate_name(tmp_cfg):
    def patch(data):
        phase_step(data, "verify", "verify")["gates"] = "ghost_set"

    assert ("error", "phases.verify.steps[3].gates") in errors(issues_for(tmp_cfg, patch))


def test_step_lanes_must_be_listed(tmp_cfg):
    def patch(data):
        phase_step(data, "define", "contracts")["lanes"] = ["feature", "ghost"]

    assert ("error", "phases.define.steps[4].lanes") in errors(issues_for(tmp_cfg, patch))


def test_an_agent_step_needs_a_skill(tmp_cfg):
    def patch(data):
        del phase_step(data, "define", "contracts")["skill"]

    assert ("error", "phases.define.steps[4]") in errors(issues_for(tmp_cfg, patch))


def test_only_a_no_tdd_step_may_go_without_a_skill():
    cfg = load_config(CFG)

    def agents(steps):
        for step in steps:
            yield from agents(step.steps)
            if step.type == "agent":
                yield step

    bare = [s.id for phase in cfg.phases.values() for s in agents(phase.steps) if s.skill is None]
    assert bare == ["change"]


def test_small_plan_prompt_is_checked(tmp_cfg):
    def patch(data):
        phase_step(data, "build", "build")["small_plan"]["prompt"] = "nope.md"

    assert ("error", "phases.build.steps[1].small_plan.prompt") in errors(issues_for(tmp_cfg, patch))


def test_the_workflow_is_six_phases_and_four_lanes():
    cfg = load_config(CFG)
    assert list(cfg.phases) == ["define", "plan", "build", "verify", "review", "ship"]
    assert cfg.lanes == ["feature", "bug", "eval", "design"]


@pytest.mark.parametrize(
    "patch, bad_path",
    [
        ({"effort": {"allowed": ["low", "turbo"]}}, "effort.allowed"),
        ({"effort": {"allowed": ["low", "high"], "default": "medium"}}, "effort.default"),
    ],
)
def test_effort_outside_known_levels(tmp_cfg, patch, bad_path):
    assert ("error", bad_path) in errors(issues_for(tmp_cfg, patch))


def test_dashboard_must_bind_loopback(tmp_cfg):
    issues = issues_for(tmp_cfg, {"dashboard": {"host": "0.0.0.0"}})
    assert ("error", "dashboard.host") in errors(issues)


@pytest.mark.parametrize(
    "backlog, bad_path",
    [
        ("nope", "dashboard.backlog"),
        ({"bogus": 1}, "dashboard.backlog.bogus"),
        ({"sync_base": "yes"}, "dashboard.backlog.sync_base"),
        ({"module_lane": "nowhere"}, "dashboard.backlog.module_lane"),
        ({"new_task": ["bug"]}, "dashboard.backlog.new_task"),
        ({"new_task": {"nowhere": ["task"]}}, "dashboard.backlog.new_task.nowhere"),
        ({"new_task": {"bug": "task"}}, "dashboard.backlog.new_task.bug"),
        ({"new_task": {"bug": ["telepathy"]}}, "dashboard.backlog.new_task.bug"),
        ({"new_task": {"bug": ["after"]}}, "dashboard.backlog.new_task.bug"),  # no input field to fill in
        ({"module_row": {"Module id": "{module}"}}, "dashboard.backlog.module_row"),  # no Depends on
        ({"module_row": {"Depends on": "{deps}"}}, "dashboard.backlog.module_row"),  # no Module id
        ({"module_row": {"Module id": 1, "Depends on": "{deps}"}}, "dashboard.backlog.module_row"),
        ({"new_task": {"bug": ["new_module"]}}, "dashboard.backlog.new_task.bug"),  # only the module lane adds modules
        ({"lane_labels": ["Bug fix"]}, "dashboard.backlog.lane_labels"),
        ({"lane_labels": {"nowhere": "Bug fix"}}, "dashboard.backlog.lane_labels.nowhere"),
        ({"lane_labels": {"bug": ""}}, "dashboard.backlog.lane_labels.bug"),
        ({"lane_labels": {"bug": 1}}, "dashboard.backlog.lane_labels.bug"),
    ],
)
def test_dashboard_backlog_block_is_checked(tmp_cfg, backlog, bad_path):
    def patch(data):
        data["dashboard"]["backlog"] = (
            backlog if not isinstance(backlog, dict) else {**data["dashboard"]["backlog"], **backlog})

    assert ("error", bad_path) in errors(issues_for(tmp_cfg, patch))


def test_new_module_form_needs_a_module_row(tmp_cfg):
    def patch(data):
        del data["dashboard"]["backlog"]["module_row"]

    assert ("error", "dashboard.backlog.new_task.feature") in errors(issues_for(tmp_cfg, patch))


def test_a_repository_may_have_no_backlog_block(tmp_cfg):
    assert errors(issues_for(tmp_cfg, lambda data: data["dashboard"].pop("backlog"))) == []


def test_validate_cli_exit_codes(tmp_cfg, capsys, pack_repo_python, monkeypatch):
    bad = tmp_cfg({"effort": {"allowed": ["low", "turbo"]}})
    assert main(["validate", "--config", str(bad)]) == 1
    assert "error effort.allowed:" in capsys.readouterr().out
    monkeypatch.chdir(pack_repo_python)
    assert main(["validate"]) == 0


def test_validate_cli_reports_load_errors(tmp_cfg, capsys):
    bad = tmp_cfg({"phases": {"plan": {"steps": "@nowhere.nope"}}})
    assert main(["validate", "--config", str(bad)]) == 1
    assert "error phases.plan.steps:" in capsys.readouterr().out


def test_validate_cli_outside_a_git_checkout_says_so(tmp_cfg, tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)  # no checkout, so no pack: the engine's own checkout is not one
    assert main(["validate"]) == 1
    assert "not inside a git checkout" in capsys.readouterr().out


def test_repo_root_from_a_subdirectory():
    # In a linked worktree repo_root names the main checkout, so compare with the checkout's own root.
    checkout = Path(__file__).resolve().parents[2]
    assert repo_root(CFG.parent) == repo_root(checkout)
    assert (repo_root(CFG.parent) / ".git").is_dir()


def test_shipped_yaml_has_one_workflow():
    data = yaml.safe_load(CFG.read_text(encoding="utf-8"))
    assert "pipelines" not in data and data["lanes"] == ["feature", "bug", "eval", "design"]
    assert data["agents"]["claude"]["command"] == ["claude"]


def test_nested_duplicate_of_a_top_level_id(tmp_cfg):
    def patch(data):
        group = phase_step(data, "define", "grill_open_questions")
        group["steps"][2]["id"] = "draft"

    assert ("error", "phases.define.steps[5].steps[2].id") in errors(issues_for(tmp_cfg, patch))


def test_review_step_id_duplicates_a_sibling(tmp_cfg):
    def patch(data):
        phase_step(data, "build", "build")["review"]["id"] = "setup"

    assert ("error", "phases.build.steps[1].review.id") in errors(issues_for(tmp_cfg, patch))


def test_same_id_in_different_phases_is_fine():
    cfg = load_config(CFG)
    ids = [s.id for p in ("verify", "review") for s in cfg.phases[p].steps]
    assert ids.count("eval_gate") == 2


def test_unknown_placeholder_in_a_template(tmp_cfg):
    path = tmp_cfg()
    prompts = path.parent / "prompts"
    (prompts / "bogus.md").write_text("Use {module} and {bogus}.\n", encoding="utf-8")
    issues = validate(load_config(path), path.parent.parent)
    assert ("error", "prompts/bogus.md") in errors(issues)
    assert any("{bogus}" in i.message for i in issues)
    assert not any(i.path.startswith(("prompts/", "engine:prompts/")) and i.path != "prompts/bogus.md" for i in issues)


def test_a_brief_file_must_exist(tmp_cfg):
    path = tmp_cfg(lambda data: phase_step(data, "review", "branch_review").update(brief=".claude/skills/none.md"))
    assert ("error", "phases.review.steps[4].brief") in errors(validate(load_config(path), path.parent.parent))


def test_a_template_with_a_brief_needs_a_step_that_names_one(tmp_cfg):
    def patch(data):
        step = phase_step(data, "review", "branch_review")
        step.pop("brief")
        step["skill"] = "no-such-skill-anywhere"  # a skill that resolves would be the brief

    path = tmp_cfg(patch)
    assert ("error", "phases.review.steps[4].brief") in errors(validate(load_config(path), path.parent.parent))


def test_a_brief_needs_a_template_that_uses_it(tmp_cfg):
    path = tmp_cfg(lambda data: phase_step(data, "review", "branch_review").update(prompt="gate_fix.md"))
    assert ("error", "phases.review.steps[4].brief") in errors(validate(load_config(path), path.parent.parent))


def test_a_task_brief_file_must_exist(tmp_cfg):
    path = tmp_cfg(lambda data: phase_step(data, "build", "build").update(task_briefs={"console/": "none.md"}))
    assert ("error", "phases.build.steps[1].task_briefs") in errors(validate(load_config(path), path.parent.parent))


def test_a_small_plan_brief_file_must_exist(tmp_cfg):
    path = tmp_cfg(lambda data: phase_step(data, "build", "build")["small_plan"].update(brief="none.md"))
    assert ("error", "phases.build.steps[1].small_plan.brief") in errors(validate(load_config(path), path.parent.parent))


def test_shipped_templates_use_only_known_placeholders(repo_root):
    issues = validate(load_config(CFG), repo_root)
    assert [i for i in issues if i.path.startswith("prompts/")] == []


def test_gates_first_needs_an_agent_step_with_gates(tmp_cfg):
    def patch(data):
        phase_step(data, "verify", "verify").pop("gates")

    assert ("error", "phases.verify.steps[3].gates_first") in errors(issues_for(tmp_cfg, patch))


def test_a_negative_task_budget_is_refused(tmp_cfg):
    assert ("error", "defaults.task_budget_usd") in errors(issues_for(tmp_cfg, {"defaults": {"task_budget_usd": -1}}))


def test_env_inherit_defaults_to_dropping_the_dotenv_twins(tmp_cfg):
    inherit = load_config(tmp_cfg()).defaults.env_inherit
    assert inherit.drop == [] and inherit.drop_dotenv == [".env"]


@pytest.mark.parametrize("patch", [
    {"drop": ["has space"]}, {"drop": ["a;b"]}, {"drop": [""]}, {"drop": ["X" * 129]},
    {"drop_dotenv": ["../.env"]}, {"drop_dotenv": ["a/../../.env"]}, {"drop_dotenv": ["/etc/env"]}, {"drop_dotenv": [""]},
])
def test_a_bad_env_inherit_is_refused(tmp_cfg, patch):
    got = errors(issues_for(tmp_cfg, {"defaults": {"env_inherit": patch}}))
    assert any(path.startswith("defaults.env_inherit.") for _, path in got)


def test_a_good_env_inherit_validates(tmp_cfg):
    patch = {"drop": ["*_URL", "A_B", "X[12]"], "drop_dotenv": [".env", "web/.env.local"]}
    assert [p for _, p in errors(issues_for(tmp_cfg, {"defaults": {"env_inherit": patch}}))
            if p.startswith("defaults.env_inherit")] == []


def test_batch_is_checked(tmp_cfg):
    def patch(data):
        phase_step(data, "build", "build")["batch"] = {"max_tasks": 1, "max_files": -1, "prompt": "nope.md"}

    found = errors(issues_for(tmp_cfg, patch))
    for key in ("max_tasks", "max_files", "prompt"):
        assert ("error", f"phases.build.steps[1].batch.{key}") in found


def test_validate_accepts_a_bundled_skill(tmp_cfg):
    def patch(data):
        phase_step(data, "define", "contracts")["skill"] = "agentflow-plan"  # in no skills dir of the repo

    assert not [i for i in issues_for(tmp_cfg, patch) if i.path == "phases.define.steps[4].skill"]


def test_validate_names_both_places_for_a_missing_skill(tmp_cfg):
    def patch(data):
        phase_step(data, "define", "contracts")["skill"] = "no-such-skill"

    [issue] = [i for i in issues_for(tmp_cfg, patch) if i.path == "phases.define.steps[4].skill"]
    assert issue.message.startswith("skill ")
    assert ".claude/skills/no-such-skill/SKILL.md" in issue.message
    assert "and no bundled skill of that name" in issue.message


@pytest.mark.parametrize("name", ["../x", "a/b", "-x", "", "a b", "x\\y"])
def test_validate_refuses_a_path_as_a_skill_name(tmp_cfg, name):
    def patch(data):
        phase_step(data, "define", "contracts")["skill"] = name

    [issue] = [i for i in issues_for(tmp_cfg, patch) if i.path == "phases.define.steps[4].skill"]
    assert issue.level == "error" and "not a skill name" in issue.message


def test_skills_map_accepts_review_debug_and_review_feedback(tmp_cfg):
    patch = {"skills": {"map": {"review": "agentflow-review", "debug": "agentflow-debug",
                                "review_feedback": "agentflow-respond"}}}
    assert not [i for i in issues_for(tmp_cfg, patch) if i.path.startswith("skills")]
    bad = issues_for(tmp_cfg, {"skills": {"map": {"review_feedback": "../x", "reviews": "agentflow-review"}}})
    assert ("error", "skills.map.review_feedback") in errors(bad) and ("error", "skills.map.reviews") in errors(bad)


def test_a_brief_template_with_no_brief_is_valid_when_the_skill_resolves(tmp_cfg):
    path = tmp_cfg(lambda data: phase_step(data, "review", "branch_review").pop("brief"))
    # branch_review's skill (requesting-code-review) has a SKILL.md in the repository: it is the step's brief
    assert ("error", "phases.review.steps[4].brief") not in errors(validate(load_config(path), path.parent.parent))


def test_a_brief_template_with_no_brief_and_no_skill_is_still_an_error(tmp_cfg):
    def patch(data):
        step = phase_step(data, "review", "branch_review")
        step.pop("brief")
        step["skill"] = "no-such-skill-anywhere"

    path = tmp_cfg(patch)
    issues = validate(load_config(path), path.parent.parent)
    assert ("error", "phases.review.steps[4].brief") in errors(issues)
    assert any("must name the file" in i.message for i in issues if i.path == "phases.review.steps[4].brief")


# --------------------------------------------------------------------------- the layers: engine defaults, pack, user

import dataclasses  # noqa: E402

from agentflow import layers  # noqa: E402
from conftest import PACKS, ROOT, SPARSE_PACK  # noqa: E402

DEFAULTS = yaml.safe_load(layers.ENGINE_DEFAULTS.read_text(encoding="utf-8"))
FULL_PACKS = {"python": PYTHON_CONFIG, "node": PACKS / "node" / ".agentflow" / "config.yaml",
              "this repo": ROOT / ".agentflow" / "config.yaml"}
# Where a full pack's loaded config differs from the same file loaded alone (`layered=False`), and why. A full pack
# writes its whole workflow, so the engine's workflow never applies to it; only keys outside the workflow it left
# unset can come from the engine defaults.
INHERITED = {
    "python": {},
    "node": {
        "agents.codex": "the engine's codex agent, disabled: it runs no step unless the pack enables it",
        "agents.claude.prices": "the engine's price table: it prices only a session Claude reported no cost for",
    },
    "this repo": {},
}
# The keys of `defaults`, `context`, `effort` and `recovery` that load_config requires (no code fallback). The engine
# defaults hold the values the engine's minimum fixture pack (`node`) writes for them.
REQUIRED = {("defaults", "agent"), ("effort", "default"), ("effort", "allowed"), ("context", "autocompact_pct"),
            ("context", "rollover_pct"), ("context", "max_rollovers"), ("context", "progress_file"),
            ("context", "gate_output_lines")}
STEP_SKILLS = {  # each default step, by its id path, and the `skills.map` key its skill comes from
    "define/debug": "debug", "define/draft": "define", "define/grill_open_questions/grill": "decide",
    "define/grill_open_questions/apply": "design_doc", "plan/plan": "plan", "build/build": "build",
    "build/build/review": "review", "build/build/small_plan": "build_small", "build/regression_test": "build",
    "build/fix": "build", "verify/plan_fixes": "review_feedback", "verify/fix_findings": "review_feedback",
    "verify/verify": "verify", "review/plan_fixes": "review_feedback", "review/fix_findings": "review_feedback",
    "review/branch_review": "review", "ship/finish/sync": "debug", "ship/ci/fix": "debug",
    "record_step": "design_doc", "resolve_step": "debug",
}
_WHOLE = (("record_step",), ("resolve_step",))


def _leaves(data, prefix=()):
    """Every leaf a pack file states, as merge sees it: a scalar, a list, an empty mapping or a whole entry."""
    for key, value in data.items():
        here = (*prefix, key)
        if value is None:
            continue  # a null deletes; it states no value
        if isinstance(value, dict) and value and here not in _WHOLE and not (
                len(here) == 2 and here[0] in ("conditions", "gates")):
            yield from _leaves(value, here)
        else:
            yield here, value


def _get(data, key):
    for part in key:
        data = data[part]
    return data


def _flat(value, prefix=""):
    if isinstance(value, dict) and value:
        out = {}
        for key, inner in value.items():
            out.update(_flat(inner, f"{prefix}.{key}" if prefix else str(key)))
        return out
    return {prefix: value}


def _fields(cfg) -> dict:
    data = dataclasses.asdict(cfg)
    for name in ("path", "raw", "merged", "layer_origins"):
        data.pop(name)
    return _flat(data)


def _default_steps():
    """`(id path, step mapping)` for every step of the engine defaults, nested ones and `small_plan` included."""
    def walk(steps, prefix):
        for step in steps:
            where = f"{prefix}/{step.get('id')}"
            yield where, step
            for key in ("review", "fix", "sync", "small_plan"):
                if isinstance(step.get(key), dict):
                    yield f"{where}/{key}" if key == "small_plan" else f"{where}/{step[key].get('id')}", step[key]
            yield from walk(step.get("steps") or [], where)

    for name, phase in DEFAULTS["phases"].items():
        yield from walk(phase["steps"], name)
    yield "record_step", DEFAULTS["record_step"]
    yield "resolve_step", DEFAULTS["resolve_step"]


def _sparse_config(sparse_repo: Path):
    return load_config(pack.pack_config(sparse_repo))


def _user_file(text: str) -> Path:
    target = layers.user_config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target


def test_a_sparse_pack_loads_on_the_engine_defaults(sparse_repo):
    cfg = _sparse_config(sparse_repo)
    assert list(cfg.phases) == ["define", "plan", "build", "verify", "review", "ship"]
    assert cfg.lanes == ["feature", "bug"]
    assert cfg.agents["claude"].permission_mode == "bypassPermissions" and not cfg.agents["codex"].enabled
    assert cfg.project.worktrees_dir is None
    assert [i for i in validate(cfg, sparse_repo) if i.level == "error"] == []
    assert cfg.layer_origins[("gates", "unit")] == "project" and cfg.layer_origins[("lanes",)] == "engine"
    assert cfg.merged["phases"]["plan"]["steps"][0]["skill"] == "@skills.map.plan"  # before `@` resolution
    assert cfg.raw["phases"]["plan"]["steps"][0]["skill"] == "agentflow-plan"


@pytest.mark.parametrize("lane", ["feature", "bug"])
def test_explain_runs_on_a_sparse_pack(sparse_repo, monkeypatch, capsys, lane):
    monkeypatch.chdir(sparse_repo)
    assert main(["explain", lane, "--module", "greet"]) == 0, capsys.readouterr().out


def test_a_sparse_pack_dry_run_passes_the_effort(sparse_repo, monkeypatch, capsys):
    monkeypatch.chdir(sparse_repo)
    assert main(["run", "feature", "--module", "greet", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "--effort medium" in out and "--permission-mode bypassPermissions" in out and "--max-turns" not in out


def test_every_default_step_has_an_explicit_id_and_a_mapped_skill():
    seen = {}
    for where, step in _default_steps():
        assert "brief" not in step and "task_briefs" not in step and "human_paths" not in step, where
        if not where.endswith("/small_plan"):
            assert isinstance(step.get("id"), str), where  # an id never falls back to a `@skills.map.*` reference
        if "skill" in step:
            seen[where] = step["skill"]
    assert seen == {where: f"@skills.map.{key}" for where, key in STEP_SKILLS.items()}
    assert DEFAULTS["checkpoints"] == {} and "approval_note" not in DEFAULTS["phases"]["plan"]
    for gone in ("spec_defines_contracts", "spec_api_reviewed", "spec_needs_api_design", "security_path"):
        assert gone not in DEFAULTS["conditions"]


def test_a_sparse_pack_maps_a_default_step_to_its_own_skill(sparse_repo):
    target = pack.pack_config(sparse_repo)
    skill = sparse_repo / ".claude" / "skills" / "grilling" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: grilling\n---\nAsk hard questions.\n", encoding="utf-8")
    target.write_text(yaml.safe_dump({**SPARSE_PACK, "skills": {"map": {"decide": "grilling"}}}), encoding="utf-8")
    cfg = load_config(target)
    [group] = [s for s in cfg.phases["define"].steps if s.id == "grill_open_questions"]
    assert {s.id: s.skill for s in group.steps if s.skill} == {"grill": "grilling", "apply": "agentflow-define"}
    assert cfg.skills_map["define"] == "agentflow-define"  # the rest of the defaults' map stays
    assert [i for i in validate(cfg, sparse_repo) if i.level == "error"] == []


def test_every_skills_map_reference_in_the_defaults_resolves(tmp_path):
    skills = DEFAULTS["skills"]["map"]
    assert skills == {"decide": "agentflow-define", "define": "agentflow-define", "design_doc": "agentflow-define",
                      "plan": "agentflow-plan", "build": "agentflow-build", "build_small": "agentflow-build",
                      "debug": "agentflow-debug", "verify": "agentflow-verify", "review": "agentflow-review",
                      "review_feedback": "agentflow-respond"}
    for name in skills.values():
        found = pack.skill_file(tmp_path, ".claude/skills", name)
        assert found is not None and pack.is_bundled(found), name
    refs = [v for v in _flat(DEFAULTS).values() if isinstance(v, str) and v.startswith("@")]
    refs += [v for step in (s for _w, s in _default_steps()) for v in step.values()
             if isinstance(v, str) and v.startswith("@")]
    assert refs and all(r.startswith("@skills.map.") and r.split(".")[-1] in skills for r in refs)


def test_engine_defaults_match_the_code_fallbacks(tmp_path):
    node = yaml.safe_load(FULL_PACKS["node"].read_text(encoding="utf-8"))
    minimal = {"version": 2, "engine": ">=2,<3",
               "project": {"base_branch": "main", "fork_from": "main", "branch": "b", "commit_trailer": ""},
               "agents": {"claude": {"command": ["claude"]}}, "guardrails": {}, "gates": {}, "gate_sets": {},
               "lanes": [], "phases": {}}
    for section, key in REQUIRED:
        minimal.setdefault(section, {})[key] = node[section][key]
    target = tmp_path / "minimal.yaml"
    target.write_text(yaml.safe_dump(minimal), encoding="utf-8")
    fallback = load_config(target, layered=False)
    for section in ("defaults", "context", "effort", "recovery"):
        built = dataclasses.asdict(getattr(fallback, section))
        built.pop("env", None)  # no `defaults.env`: the defaults set no environment
        assert set(DEFAULTS[section]) == set(built), section  # the defaults state every key load_config reads
        for key, value in DEFAULTS[section].items():
            assert value == built[key], f"{section}.{key}"
            if (section, key) in REQUIRED:
                assert value == node[section][key], f"{section}.{key}"
    assert "env" not in DEFAULTS["defaults"]


def test_the_defaults_set_no_permission_mode(sparse_repo):
    assert "permission_mode" not in layers.ENGINE_DEFAULTS.read_text(encoding="utf-8")
    target = pack.pack_config(sparse_repo)
    target.write_text(target.read_text(encoding="utf-8").replace("permission_mode: bypassPermissions",
                                                                 "max_turns: 80"), encoding="utf-8")
    assert load_config(target).agents["claude"].permission_mode is None


def test_a_pack_without_gates_is_refused_with_a_starter_hint(sparse_repo):
    target = pack.pack_config(sparse_repo)
    data = {k: v for k, v in SPARSE_PACK.items() if k not in ("gates", "gate_sets")}
    target.write_text(yaml.safe_dump(data), encoding="utf-8")
    [issue] = [i for i in validate(load_config(target), sparse_repo) if i.path == "gate_sets.verify"]
    assert issue.level == "error" and ("starter:" in issue.message or "gates:" in issue.message)


@pytest.mark.parametrize("name", list(FULL_PACKS))
def test_full_packs_keep_every_value_they_state(name):
    p = FULL_PACKS[name]
    cfg = load_config(p)
    stated = list(_leaves(yaml.safe_load(p.read_text(encoding="utf-8"))))
    assert stated
    for key, value in stated:
        assert _get(cfg.merged, key) == value and cfg.layer_origins[key] == "project", key


@pytest.mark.parametrize("name", list(FULL_PACKS))
def test_full_packs_differ_from_unlayered_only_where_listed(name):
    p = FULL_PACKS[name]
    layered, alone = _fields(load_config(p)), _fields(load_config(p, layered=False))
    differ = {k for k in layered.keys() | alone.keys() if layered.get(k, "<unset>") != alone.get(k, "<unset>")}
    listed = INHERITED[name]
    assert all(reason for reason in listed.values())
    assert {k for k in differ if not any(k == i or k.startswith(i + ".") for i in listed)} == set()
    assert {i for i in listed if not any(k == i or k.startswith(i + ".") for k in differ)} == set()  # no stale row


def test_a_user_value_overrides_the_pack_and_says_so():
    _user_file("agents:\n  claude:\n    models: {build: haiku}\ndefaults:\n  run_budget_usd: 7\n")
    cfg = load_config(PYTHON_CONFIG)
    assert cfg.agents["claude"].models["build"] == "haiku" and cfg.agents["claude"].models["plan"] == "opus"
    assert cfg.defaults.run_budget_usd == 7
    assert cfg.layer_origins[("agents", "claude", "models", "build")] == "user"
    assert load_config(PYTHON_CONFIG, user=False).defaults.run_budget_usd == 25


def test_a_user_value_of_the_wrong_type_names_the_user_file():
    user = _user_file("defaults:\n  run_budget_usd: lots\n")
    with pytest.raises(ConfigError) as err:
        load_config(PYTHON_CONFIG)
    assert err.value.path == f"{user}: defaults.run_budget_usd" and "expected" in err.value.message


@pytest.mark.parametrize("text, words", [
    ("agents: [\n", "cannot read"),                                  # not YAML
    ("- a list\n", "mapping"),                                       # not a mapping
    ("agents: &a {claude: {models: *a}}\n", "refers to itself"),     # a recursive anchor
    ("agents:\n  claude:\n    command: [evil]\n", "not a personal setting"),
    ("gates:\n  unit: {run: evil}\n", "not a personal setting"),
    ("agents:\n  nobody:\n    models: {build: x}\n", "no agent `nobody`"),
])
def test_a_broken_user_file_is_a_config_error_naming_it(text, words):
    user = _user_file(text)
    with pytest.raises(ConfigError) as err:
        load_config(PYTHON_CONFIG)
    assert str(user) in str(err.value) and words in str(err.value)


def test_an_empty_user_file_changes_nothing():
    _user_file("")
    assert _fields(load_config(PYTHON_CONFIG)) == _fields(load_config(PYTHON_CONFIG, user=False))


def test_load_config_without_the_user_layer_ignores_a_broken_user_file():
    _user_file("gates: [\n")
    assert load_config(PYTHON_CONFIG, user=False).phases
    assert load_config(PYTHON_CONFIG, layered=False).phases


def test_a_user_file_cannot_revive_an_agent_the_pack_deleted(tmp_cfg):
    user = _user_file("agents:\n  codex:\n    models: {build: x}\n")
    assert load_config(PYTHON_CONFIG).agents["codex"].models["build"] == "x"  # the pack defines codex: personal

    def drop_codex(data):
        data["agents"]["codex"] = None  # written as `null`: it deletes the engine's codex too

    with pytest.raises(ConfigError) as err:
        load_config(tmp_cfg(drop_codex))
    assert str(user) in str(err.value) and "no agent `codex`" in str(err.value)


def test_a_recursive_anchor_in_the_pack_is_a_config_error_naming_it(tmp_path):
    target = tmp_path / "config.yaml"
    target.write_text('version: 2\nengine: ">=2,<3"\nloop: &a {x: *a}\n', encoding="utf-8")
    with pytest.raises(ConfigError) as err:
        load_config(target)
    assert str(target) in str(err.value)


def test_version_and_engine_are_checked_on_the_pack_file_alone(tmp_path):
    target = tmp_path / "config.yaml"
    target.write_text("gates: {}\n", encoding="utf-8")
    with pytest.raises(ConfigError) as err:
        load_config(target)
    assert err.value.path == "version"
    _user_file("defaults:\n  run_budget_usd: 3\n")  # a user file can't supply them either
    with pytest.raises(ConfigError):
        load_config(target)


# --------------------------------------------------------------------------- worktrees, and the files a run's layers come from


def test_worktrees_default_into_the_state_dir(sparse_repo):
    from agentflow.config import worktrees_root

    cfg = _sparse_config(sparse_repo)
    assert cfg.project.worktrees_dir is None
    assert worktrees_root(cfg, sparse_repo) == pack.state_dir(sparse_repo) / "worktrees"


def test_a_set_worktrees_dir_still_wins(tmp_cfg, sparse_repo):
    from agentflow.config import worktrees_root

    cfg = load_config(tmp_cfg({"project": {"worktrees_dir": "wt/runs"}}))
    assert worktrees_root(cfg, sparse_repo) == sparse_repo / "wt" / "runs"
    assert [i for i in validate(cfg, sparse_repo) if i.path == "project.worktrees_dir"] == []


@pytest.mark.parametrize("value", ["/abs", "../up", "a/../../up", ""])
def test_a_worktrees_dir_outside_the_repo_is_an_error(tmp_cfg, sparse_repo, value):
    cfg = load_config(tmp_cfg({"project": {"worktrees_dir": value}}))
    errors = [i for i in validate(cfg, sparse_repo) if i.level == "error" and i.path == "project.worktrees_dir"]
    assert len(errors) == 1 and "relative" in errors[0].message


def test_layer_files_lists_the_defaults_the_pack_and_the_user_file_that_exists(sparse_repo):
    from agentflow import layers
    from agentflow.config import layer_files

    cfg = _sparse_config(sparse_repo)
    assert layer_files(cfg) == [layers.ENGINE_DEFAULTS, Path(cfg.path).resolve()]  # no user file yet
    user = _user_file("defaults:\n  run_budget_usd: 7\n")
    assert layer_files(cfg) == [layers.ENGINE_DEFAULTS, Path(cfg.path).resolve(), user]


def test_an_unreadable_user_file_location_is_a_config_error_naming_it(monkeypatch):
    user = layers.user_config_path()
    real = Path.exists

    def exists(self):
        if self == user:
            raise PermissionError(13, "Permission denied")
        return real(self)

    monkeypatch.setattr(Path, "exists", exists)
    with pytest.raises(ConfigError) as err:
        load_config(PYTHON_CONFIG)
    assert str(user) in str(err.value) and "cannot read" in str(err.value)
    assert load_config(PYTHON_CONFIG, user=False).phases  # not asked for: not touched


# --------------------------------------------------------------------------- starters


def _starter_pack(sparse_repo: Path, **extra) -> Path:
    path = pack.pack_config(sparse_repo)
    data = {"version": 2, "engine": ">=2,<3", "agents": {"claude": {"permission_mode": "bypassPermissions"}}, **extra}
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


@pytest.mark.parametrize("name", ["docs-only", "go", "node-pnpm", "python-uv"])
def test_every_starter_validates_with_no_errors(name, sparse_repo):
    path = _starter_pack(sparse_repo, starter=name)
    cfg = load_config(path)
    assert [i for i in validate(cfg, sparse_repo) if i.level == "error"] == []
    assert cfg.gate_sets["verify"] and set(cfg.gate_sets) >= {"setup", "build", "verify"}
    assert cfg.layer_origins[("gates", next(iter(cfg.gates)))] == f"starter:{name}"
    assert cfg.layer_origins[("lanes",)] == "engine"


def test_no_starter_owns_the_workflow_or_sets_lanes():
    from agentflow import layers

    for name in layers.starter_names():
        data = yaml.safe_load((layers.STARTERS / name / "config.yaml").read_text(encoding="utf-8"))
        assert not layers.owns_workflow(data), name
        assert "lanes" not in data and "phases" not in data, name
        assert set(data["gate_sets"]) == {"setup", "build", "verify"} and data["gates"], name
        assert all(g in data["gates"] for names in data["gate_sets"].values() for g in names), name


def test_no_engine_layer_confirms_the_gates():
    # Only a human confirms a pack's gates: neither the engine defaults nor a starter may set `gates_confirmed`.
    from agentflow import layers

    def keys(node):
        if isinstance(node, dict):
            for key, value in node.items():
                yield key
                yield from keys(value)
        elif isinstance(node, list):
            for item in node:
                yield from keys(item)

    files = [p for root in (layers.ENGINE_DEFAULTS.parent, layers.STARTERS) for p in root.rglob("*") if p.is_file()]
    assert layers.ENGINE_DEFAULTS in files and len(files) > len(layers.starter_names())
    for path in files:
        assert "gates_confirmed" not in set(keys(yaml.safe_load(path.read_text(encoding="utf-8")))), path


def test_no_starter_is_the_same_as_an_absent_or_null_starter(sparse_repo):
    absent = load_config(_starter_pack(sparse_repo, gates={"unit": {"run": "true"}},
                                       gate_sets={"setup": [], "build": ["unit"], "verify": ["unit"]}))
    null = load_config(_starter_pack(sparse_repo, starter=None, gates={"unit": {"run": "true"}},
                                     gate_sets={"setup": [], "build": ["unit"], "verify": ["unit"]}))
    assert "starter" not in null.merged  # a null deletes the key
    assert absent.merged == null.merged and absent.layer_origins == null.layer_origins
    assert set(absent.layer_origins.values()) == {"engine", "project"}


@pytest.mark.parametrize("value", ["nope", "", "../defaults", "/etc", 3, ["go"]])
def test_a_starter_that_is_not_a_known_name_is_refused(value, sparse_repo):
    path = _starter_pack(sparse_repo, starter=value)
    with pytest.raises(ConfigError) as err:
        load_config(path)
    assert err.value.path == "starter"
    assert all(name in err.value.message for name in ("docs-only", "go", "node-pnpm", "python-uv"))


def test_validate_reports_an_unknown_starter_instead_of_crashing(sparse_repo, monkeypatch, capsys):
    _starter_pack(sparse_repo, starter="nope")
    monkeypatch.chdir(sparse_repo)
    assert main(["validate"]) == 1
    out = capsys.readouterr().out
    assert out.startswith("error starter: ") and "python-uv" in out


def test_the_pack_overrides_its_starter(sparse_repo):
    path = _starter_pack(sparse_repo, starter="python-uv", gates={"tests": {"run": "make check"}})
    cfg = load_config(path)
    assert cfg.gates["tests"].run == "make check" and cfg.layer_origins[("gates", "tests")] == "project"
    assert cfg.gates["sync"].run == "uv sync" and cfg.layer_origins[("gates", "sync")] == "starter:python-uv"
    assert cfg.gate_sets["verify"] == ["tests"]
    again = _starter_pack(sparse_repo, starter="python-uv", gate_sets={"setup": [], "build": ["sync"], "verify": ["sync"]})
    assert load_config(again).gate_sets["verify"] == ["sync"]


def test_layer_files_names_the_starter_between_the_defaults_and_the_pack(sparse_repo):
    from agentflow import layers
    from agentflow.config import layer_files

    cfg = load_config(_starter_pack(sparse_repo, starter="go"))
    starter = layers.STARTERS / "go" / "config.yaml"
    assert layer_files(cfg) == [layers.ENGINE_DEFAULTS, starter, Path(cfg.path).resolve()]
    user = _user_file("defaults:\n  run_budget_usd: 7\n")
    assert layer_files(cfg) == [layers.ENGINE_DEFAULTS, starter, Path(cfg.path).resolve(), user]


def test_editing_a_starter_file_changes_the_layers_digest(sparse_repo, monkeypatch, tmp_path):
    import shutil

    from agentflow import layers
    from agentflow.config import layer_files
    from agentflow.config_trust import layers_sha256

    copy = tmp_path / "starters"
    shutil.copytree(layers.STARTERS, copy)
    monkeypatch.setattr(layers, "STARTERS", copy)
    cfg = load_config(_starter_pack(sparse_repo, starter="go"))
    before = layers_sha256(layer_files(cfg))
    target = copy / "go" / "config.yaml"
    target.write_text(target.read_text(encoding="utf-8") + "# edited\n", encoding="utf-8")
    assert layers_sha256(layer_files(cfg)) != before


# --------------------------------------------------------------------------- gates_confirmed, baseline, cwd, the bug lane


def _warnings(issues: list[Issue]) -> list[tuple[str, str]]:
    return [(i.path, i.message) for i in issues if i.level == "warning"]


def test_unconfirmed_gates_warn(tmp_cfg):
    issues = issues_for(tmp_cfg, {"gates_confirmed": None})
    assert [w for w in _warnings(issues) if w[0] == "gates_confirmed"] == [
        ("gates_confirmed", "detected or inherited gates are a proposal: check `gates` and `gate_sets`, "
                            "then set `gates_confirmed: true`")]
    assert [w for w in _warnings(issues_for(tmp_cfg, {"gates_confirmed": False})) if w[0] == "gates_confirmed"]
    assert not [w for w in _warnings(issues_for(tmp_cfg, {"gates_confirmed": True})) if w[0] == "gates_confirmed"]


@pytest.mark.parametrize("value", ["true", 1, [True]])
def test_gates_confirmed_must_be_a_bool(tmp_cfg, value):
    assert ("error", "gates_confirmed") in errors(issues_for(tmp_cfg, {"gates_confirmed": value}))


def test_a_red_baseline_warns_naming_the_base_branch(tmp_cfg):
    issues = issues_for(tmp_cfg, {"gates": {"floor": {"baseline": "red"}}})
    base = load_config(tmp_cfg()).project.base_branch
    assert ("gates.floor.baseline",
            f"`floor` failed on `{base}` when the pack was set up: fix it and remove `baseline`, "
            "or delete the gate") in _warnings(issues)
    assert errors(issues) == []
    green = issues_for(tmp_cfg, {"gates": {"floor": {"baseline": "green"}}})
    assert errors(green) == [] and not [w for w in _warnings(green) if w[0] == "gates.floor.baseline"]


@pytest.mark.parametrize("value", ["amber", True, 3])
def test_a_baseline_other_than_red_or_green_is_an_error(tmp_cfg, value):
    assert ("error", "gates.floor.baseline") in errors(issues_for(tmp_cfg, {"gates": {"floor": {"baseline": value}}}))


@pytest.mark.parametrize("cwd", ["/abs", "../up", "a/../../b"])
def test_a_gate_cwd_outside_the_repo_is_an_error(tmp_cfg, cwd):
    assert ("error", "gates.floor.cwd") in errors(issues_for(tmp_cfg, {"gates": {"floor": {"cwd": cwd}}}))


def test_a_relative_gate_cwd_is_fine(tmp_cfg):
    assert ("error", "gates.floor.cwd") not in errors(issues_for(tmp_cfg, {"gates": {"floor": {"cwd": "web/app"}}}))


def _bug_lane_warnings(issues: list[Issue]) -> list[str]:
    return [i.message for i in issues if i.level == "warning" and i.path == "lanes"]


def test_the_bug_lane_without_any_bash_allow_rule_warns(sparse_repo):
    cfg = load_config(pack.pack_config(sparse_repo))
    assert "bug" in cfg.lanes
    messages = _bug_lane_warnings(validate(cfg, sparse_repo))
    assert len(messages) == 1
    assert messages[0].startswith("the bug lane's red/green check runs the regression test only when a Bash allow rule "
                                  "in .claude/settings.json covers it: add a narrow rule such as `Bash(")


def test_the_bug_lane_with_a_bash_allow_rule_does_not_warn(sparse_repo):
    (sparse_repo / ".claude").mkdir()
    (sparse_repo / ".claude" / "settings.local.json").write_text(
        json.dumps({"permissions": {"allow": ["Bash(true)"]}}), encoding="utf-8")
    cfg = load_config(pack.pack_config(sparse_repo))
    assert _bug_lane_warnings(validate(cfg, sparse_repo)) == []


def test_the_bug_lane_warning_reads_the_main_checkout_not_a_worktree(sparse_repo, git, tmp_path):
    (sparse_repo / ".claude").mkdir()
    (sparse_repo / ".claude" / "settings.json").write_text(
        json.dumps({"permissions": {"allow": ["Bash(true)"]}}), encoding="utf-8")
    git(sparse_repo, "add", "-A")
    git(sparse_repo, "commit", "-q", "-m", "settings")
    linked = tmp_path / "linked"
    git(sparse_repo, "worktree", "add", "-q", str(linked), "-b", "side")
    (linked / ".claude" / "settings.json").write_text("{}\n", encoding="utf-8")  # a worktree's own copy is not read
    cfg = load_config(pack.pack_config(sparse_repo))
    assert _bug_lane_warnings(validate(cfg, linked)) == []


def test_a_pack_without_the_bug_lane_needs_no_allow_rule(sparse_repo):
    path = pack.pack_config(sparse_repo)
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw["lanes"] = ["feature"]
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    assert _bug_lane_warnings(validate(load_config(path), sparse_repo)) == []
