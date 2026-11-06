from __future__ import annotations

from pathlib import Path

import pytest
import yaml

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
    with pytest.raises(ConfigError) as e:
        load_config(tmp_cfg({"gates": None}))
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
    path = tmp_cfg(lambda data: phase_step(data, "review", "branch_review").pop("brief"))
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


def test_batch_is_checked(tmp_cfg):
    def patch(data):
        phase_step(data, "build", "build")["batch"] = {"max_tasks": 1, "max_files": -1, "prompt": "nope.md"}

    found = errors(issues_for(tmp_cfg, patch))
    for key in ("max_tasks", "max_files", "prompt"):
        assert ("error", f"phases.build.steps[1].batch.{key}") in found
