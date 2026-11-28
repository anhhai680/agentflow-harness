from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from agentflow import pack
from agentflow.config import (
    CACHE_TTLS, ConfigError, Evidence, Issue, Usage, VulnPolicy, load_config, parse_evidence, validate, validate_usage,
)
from agentflow.flow import main, repo_root
from conftest import PYTHON_CONFIG, with_doubt

CFG = PYTHON_CONFIG


def issues_for(tmp_cfg, patch) -> list[Issue]:
    path = tmp_cfg(patch)
    return validate(load_config(path), path.parent.parent)


def errors(issues: list[Issue]) -> list[tuple[str, str]]:
    return [(i.level, i.path) for i in issues if i.level == "error"]


def _write_user_file(data: dict) -> Path:
    target = layers.user_config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(yaml.safe_dump(data), encoding="utf-8")
    return target


def test_a_pack_that_sets_workspace_gets_a_warning_and_a_user_value_does_not(tmp_cfg):
    found = issues_for(tmp_cfg, {"workspace": {"roots": ["/x"]}})
    assert [(i.level, i.path) for i in found if i.path.startswith("workspace")] == [("warning", "workspace.roots")]
    assert "user config" in next(i for i in found if i.path == "workspace.roots").message
    assert errors(found) == []
    _write_user_file({"workspace": {"roots": ["/x"]}})
    assert [i for i in issues_for(tmp_cfg, {}) if i.path.startswith("workspace")] == []


def test_a_pack_that_sets_the_cap_or_the_digest_gets_a_warning(tmp_cfg):
    found = issues_for(tmp_cfg, {"workspace": {"max_active_runs": 2, "digest": True}})
    assert sorted((i.level, i.path) for i in found if i.path.startswith("workspace")) == [
        ("warning", "workspace.digest"), ("warning", "workspace.max_active_runs")]


def test_the_workspace_warning_survives_a_missing_home_directory(tmp_cfg, monkeypatch):
    path = tmp_cfg({"workspace": {"roots": ["/x"]}})
    cfg = load_config(path)

    def nohome():
        raise RuntimeError("no home")

    monkeypatch.setattr(layers, "user_config_path", nohome)
    found = validate(cfg, path.parent.parent)
    warning = next(i for i in found if i.path == "workspace.roots")
    assert warning.level == "warning" and "your user config file" in warning.message


def test_a_malformed_workspace_block_is_an_error(tmp_cfg):
    found = issues_for(tmp_cfg, {"workspace": {"roots": ["rel"]}})
    assert errors(found) == [("error", "workspace.roots")]


def test_user_dashboard_keys_are_still_range_checked(tmp_cfg):
    _write_user_file({"dashboard": {"board_refresh_s": 0.1}})
    assert ("error", "dashboard.board_refresh_s") in errors(issues_for(tmp_cfg, {}))


def test_load_config_accepts_the_workspace_key_from_the_user_file(tmp_cfg):
    _write_user_file({"workspace": {"roots": ["/x"]}})
    path = tmp_cfg({})
    assert load_config(path).merged["workspace"]["roots"] == ["/x"]


def phase_step(data: dict, phase: str, step_id: str) -> dict:
    return next(s for s in data["phases"][phase]["steps"] if s["id"] == step_id)


def test_with_doubt_validates_clean(tmp_cfg):
    baseline = issues_for(tmp_cfg, None)  # the tmp repo has no settings.json, so the bug lane's rule warning is always there
    assert issues_for(tmp_cfg, with_doubt) == baseline


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


@pytest.mark.parametrize("step_id,refused", [("size", True), ("size-1", True), ("size-12", True),
                                              ("sizes", False), ("size-up", False), ("resize", False)])
def test_a_step_id_shaped_like_a_sizer_key_is_refused(tmp_cfg, step_id, refused):
    def patch(data):
        phase_step(data, "define", "contracts")["id"] = step_id
        phase_step(data, "build", "build")["review"]["id"] = "size-2" if refused else "review-2"

    found = errors(issues_for(tmp_cfg, patch))
    assert (("error", "phases.define.steps[4].id") in found) is refused
    assert (("error", "phases.build.steps[1].review.id") in found) is refused


@pytest.mark.parametrize("step_id,refused", [("route", True), ("routes", False), ("reroute", False)])
def test_the_triage_step_id_is_reserved(tmp_cfg, step_id, refused):
    def patch(data):
        phase_step(data, "define", "contracts")["id"] = step_id
        phase_step(data, "build", "build")["review"]["id"] = step_id if refused else "review-2"

    found = errors(issues_for(tmp_cfg, patch))
    assert (("error", "phases.define.steps[4].id") in found) is refused
    assert (("error", "phases.build.steps[1].review.id") in found) is refused


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


def test_effort_sizing_defaults_on(sparse_repo):
    assert load_config(pack.pack_config(sparse_repo)).effort.sizing is True


def test_effort_sizing_must_be_a_bool(tmp_cfg):
    with pytest.raises(ConfigError, match="effort.sizing"):
        load_config(tmp_cfg({"effort": {"sizing": "yes"}}))


def test_effort_allowed_needs_a_level_at_or_above_medium(tmp_cfg, capsys):
    bad = tmp_cfg({"effort": {"allowed": ["low"], "default": "low"}})
    assert main(["validate", "--config", str(bad)]) == 1
    assert "error effort.allowed:" in capsys.readouterr().out


@pytest.mark.parametrize(
    "patch, bad_path",
    [
        ({"context": {"max_compactions": 0}}, "context.max_compactions"),
        ({"context": {"context_window_tokens": 50000}}, "context.context_window_tokens"),
        ({"context": {"context_window_tokens": 2000000}}, "context.context_window_tokens"),
        ({"context": {"rollover_pct": 60}}, "context.rollover_pct"),  # not above autocompact_pct (60)
        ({"context": {"autocompact_pct": 0}}, "context.autocompact_pct"),
    ],
)
def test_context_limits(tmp_cfg, patch, bad_path):
    assert ("error", bad_path) in errors(issues_for(tmp_cfg, patch))


def test_context_defaults_are_valid(tmp_cfg):
    assert not [e for e in errors(issues_for(tmp_cfg, {"context": {"context_window_tokens": 200000, "max_compactions": 3}}))
                if e[1].startswith("context.")]


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


def _brief_placeholder_warnings(issues: list[Issue]) -> list[Issue]:
    return [i for i in issues if i.level == "warning" and i.path.startswith("prompts/") and "get no brief" in i.message]


@pytest.mark.parametrize("name", ["_result.md", "_preamble.md"])
def test_brief_placeholder_warning(tmp_cfg, name):
    path = tmp_cfg()
    (path.parent / "prompts" / name).write_text("Write to {result_file}. In run {run}.\n", encoding="utf-8")
    found = _brief_placeholder_warnings(validate(load_config(path), path.parent.parent))
    assert [(i.path, i.fixable) for i in found] == [(f"prompts/{name}", False)]
    assert "{result_file}" in found[0].message and "{run}" not in found[0].message


def test_an_old_preamble_copy_with_only_slot_lines_still_gets_a_brief(tmp_cfg):
    # The runner drops an old copy's slot lines and its effort-check block before it looks for placeholders: so must
    # validate, or it warns of a brief the run does give.
    path = tmp_cfg()
    engine = (pack.ENGINE_PROMPTS / "_preamble.md").read_text(encoding="utf-8")
    old = (engine + "\n{run_facts}\n{code_map}\n{lessons}\n\n<!-- effort-check -->\nYou run at `{effort}`.\n"
           "<!-- /effort-check -->\n")
    (path.parent / "prompts" / "_preamble.md").write_text(old.replace("\n", "\r\n"), encoding="utf-8")
    assert _brief_placeholder_warnings(validate(load_config(path), path.parent.parent)) == []
    (path.parent / "prompts" / "_preamble.md").write_text(old + "Step {step_id}.\n", encoding="utf-8")
    (found,) = _brief_placeholder_warnings(validate(load_config(path), path.parent.parent))
    assert "{step_id}" in found.message and "{run_facts}" not in found.message and "{effort}" not in found.message


def test_the_shared_brief_partial_helper_matches_the_runner():
    from agentflow.config import brief_partial

    text, outside = brief_partial("_preamble.md", "A {run}.\r\n{lessons}\r\n<!-- effort-check -->\r\n{effort}\r\n"
                                                  "<!-- /effort-check -->\r\nB {step_id}")
    assert text == "A {run}.\nB {step_id}" and outside == ["step_id"]
    assert brief_partial("_result.md", "{lessons} {result_file}\n") == ("{lessons} {result_file}\n",
                                                                       ["lessons", "result_file"])


def _override_warnings(issues: list[Issue]) -> list[Issue]:
    return [i for i in issues if i.level == "warning" and "until it is committed" in i.message]


def test_a_prompt_override_not_committed_at_head_warns(tmp_cfg, tmp_path):
    path = tmp_cfg()
    engine = (pack.ENGINE_PROMPTS / "_result.md").read_text(encoding="utf-8")
    git_repo(tmp_path, {"CLAUDE.md": "x"})
    (path.parent / "prompts" / "_result.md").write_text(engine + "\nOurs.\n", encoding="utf-8")
    found = _override_warnings(validate(load_config(path), path.parent.parent))
    assert [(i.path, i.message) for i in found] == [
        ("prompts/_result.md", "is not committed at HEAD (or can't be read there): run sessions get no brief until "
                               "it is committed (the brief reads it from the run's fork commit)")]
    off = tmp_cfg({"context": {"run_brief": False}})
    assert _override_warnings(validate(load_config(off), off.parent.parent)) == []
    import subprocess

    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "o"], cwd=tmp_path,
                   check=True, capture_output=True)
    path = tmp_cfg()
    assert _override_warnings(validate(load_config(path), path.parent.parent)) == []


def test_brief_placeholder_warning_off_with_run_brief_false(tmp_cfg):
    path = tmp_cfg({"context": {"run_brief": False}})
    (path.parent / "prompts" / "_result.md").write_text("Write to {result_file}.\n", encoding="utf-8")
    assert _brief_placeholder_warnings(validate(load_config(path), path.parent.parent)) == []


def test_brief_placeholders_are_prompt_placeholders():
    from agentflow import config

    assert config.BRIEF_PLACEHOLDERS <= config.PROMPT_PLACEHOLDERS and "in_brief" in config.PROMPT_PLACEHOLDERS
    assert "result_file" not in config.BRIEF_PLACEHOLDERS


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


def test_the_budgets_default_to_a_soft_and_a_hard_limit(tmp_cfg):
    defaults = load_config(tmp_cfg({"defaults": {"run_budget_usd": None, "run_budget_hard_usd": None,
                                                  "task_budget_usd": None}})).defaults
    assert (defaults.run_budget_usd, defaults.run_budget_hard_usd, defaults.task_budget_usd) == (25, 65, 2)


@pytest.mark.parametrize("patch, key", [
    ({"run_budget_usd": 30, "run_budget_hard_usd": 20}, "defaults.run_budget_hard_usd"),  # hard below soft
    ({"run_budget_usd": 0}, "defaults.run_budget_usd"),
    ({"run_budget_usd": -1}, "defaults.run_budget_usd"),
    ({"run_budget_usd": float("inf"), "run_budget_hard_usd": float("inf")}, "defaults.run_budget_usd"),
    ({"run_budget_hard_usd": 0}, "defaults.run_budget_hard_usd"),
    ({"run_budget_hard_usd": -5}, "defaults.run_budget_hard_usd"),
    ({"run_budget_hard_usd": float("nan")}, "defaults.run_budget_hard_usd"),
])
def test_the_budgets_must_be_positive_and_the_hard_one_not_below_the_soft(tmp_cfg, patch, key):
    assert (("error", key)) in errors(issues_for(tmp_cfg, {"defaults": patch}))


def test_a_hard_budget_equal_to_the_soft_one_is_allowed(tmp_cfg):
    found = errors(issues_for(tmp_cfg, {"defaults": {"run_budget_usd": 10, "run_budget_hard_usd": 10}}))
    assert not [path for _, path in found if "budget" in path]


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
_RETRO_STEP = "the engine's retro step: a retro is the engine's pipeline, so no pack needs to state it"
_WATCHDOG = "the engine's watchdog default (15 minutes): a pack states it only to change it"
_READ_ONLY = "the engine's read-only roles (review): a pack states them only to change them"
_CAPS = "the engine's per-role caps: a pack states them only to change them"
_PARK = "the engine's parkable stops (hitl.PARKABLE): a pack states them only to park fewer"
_DIGEST = "the engine's notification default (one line per stop): a pack states it only to send a digest"
_POLICY = "the engine's default obligations (verified, reviewed, ...) and the engine floor, which a pack states only to add to"
INHERITED = {
    "python": {"retro_step": _RETRO_STEP, "policy": _POLICY, "agents.claude.read_only_roles": _READ_ONLY,
               "agents.claude.max_turns_by_role": _CAPS, "agents.claude.session_budget_usd": _CAPS,
               "agents.claude.bash_output_chars": _CAPS, "hitl.park_kinds": _PARK, "hitl.digest": _DIGEST},
    "node": {
        "policy": _POLICY,
        "hitl": "the engine's park settings (parking on, every parkable stop): a pack states them only to change them",
        "agents.codex": "the engine's codex agent, disabled: it runs no step unless the pack enables it",
        "agents.claude.prices": "the engine's price table: it prices only a session Claude reported no cost for",
        "retro_step": _RETRO_STEP,
        "watchdog.no_progress_min": _WATCHDOG,
        "agents.claude.read_only_roles": _READ_ONLY,
        "agents.claude.max_turns_by_role": _CAPS,
        "agents.claude.session_budget_usd": _CAPS,
        "agents.claude.bash_output_chars": _CAPS,
    },
    "this repo": {"retro_step": _RETRO_STEP, "policy": _POLICY, "watchdog.no_progress_min": _WATCHDOG,
                  "agents.claude.read_only_roles": _READ_ONLY, "agents.claude.max_turns_by_role": _CAPS,
                  "agents.claude.session_budget_usd": _CAPS, "agents.claude.bash_output_chars": _CAPS,
                  "hitl.park_kinds": _PARK, "hitl.digest": _DIGEST},
}
# The keys of `defaults`, `context`, `effort` and `recovery` that load_config requires (no code fallback). The engine
# defaults hold the values the engine's minimum fixture pack (`node`) writes for them.
REQUIRED = {("defaults", "agent"), ("effort", "default"), ("effort", "allowed"), ("context", "autocompact_pct"),
            ("context", "rollover_pct"), ("context", "max_rollovers"), ("context", "progress_file"),
            ("context", "gate_output_lines")}
STEP_SKILLS = {  # each default step, by its id path, and the `skills.map` key its skill comes from
    "define/debug": "debug", "define/triage": "patch", "define/draft": "define", "define/grill_open_questions/grill": "decide",
    "define/grill_open_questions/apply": "design_doc", "define/spec_reconcile": "design_doc",
    "define/spec_review": "doubt", "plan/plan": "plan", "plan/plan_reconcile": "plan", "plan/plan_review": "doubt",
    "build/build": "build",
    "build/build/review": "review", "build/build/small_plan": "build_small", "build/regression_test": "build",
    "build/fix": "build", "build/patch": "patch", "verify/verify": "verify", "review/plan_fixes": "review_feedback", "review/fix_findings": "review_feedback",
    "review/branch_review": "review", "review/vuln_review": "review", "ship/finish/sync": "debug", "ship/ci/fix": "debug",
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
                len(here) == 2 and here[0] in ("conditions", "gates")) and not (
                len(here) == 3 and here[:2] == ("policy", "obligations")):
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
    assert cfg.lanes == ["feature", "bug", "vuln"]
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
    assert "--effort medium" in out and "--permission-mode bypassPermissions" in out
    first = next(line for line in out.splitlines() if "--model" in line)  # the first step's command line
    assert first.lstrip().startswith("define/draft:")  # `role: plan` in the engine defaults
    assert "--max-turns 60" in first and "--max-budget-usd 10.00" in first


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


def test_engine_default_verify_has_no_refine_round_step():
    ids = {name: [step["id"] for step in DEFAULTS["phases"][name]["steps"]] for name in ("verify", "review")}
    assert ids["verify"] == ["setup", "verify"]
    assert ids["review"] == ["setup", "plan_fixes", "fix_findings", "branch_review", "vuln_review"]


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
                      "build_wave": "agentflow-build", "debug": "agentflow-debug", "verify": "agentflow-verify", "review": "agentflow-review",
                      "review_feedback": "agentflow-respond", "doubt": "agentflow-doubt", "patch": "agentflow-patch"}
    for name in skills.values():
        found = pack.skill_file(tmp_path, ".claude/skills", name)
        assert found is not None and pack.is_bundled(found), name
    refs = [v for v in _flat(DEFAULTS).values() if isinstance(v, str) and v.startswith("@")]
    refs += [v for step in (s for _w, s in _default_steps()) for v in step.values()
             if isinstance(v, str) and v.startswith("@")]
    assert refs and all(r.startswith("@skills.map.") and r.split(".")[-1] in skills for r in refs)


def test_engine_defaults_match_the_code_fallbacks(tmp_path):
    node = yaml.safe_load(FULL_PACKS["node"].read_text(encoding="utf-8"))
    minimal = {"version": 2, "engine": ">=3,<4",
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
    target.write_text('version: 2\nengine: ">=3,<4"\nloop: &a {x: *a}\n', encoding="utf-8")
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
    data = {"version": 2, "engine": ">=3,<4", "agents": {"claude": {"permission_mode": "bypassPermissions"}}, **extra}
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


def test_human_question_kinds_default_to_boundary(tmp_cfg):
    from agentflow.autonomy import human_kinds

    engine = yaml.safe_load((Path(pack.__file__).parent / "defaults" / "config.yaml").read_text(encoding="utf-8"))
    assert engine["autonomy"]["human_question_kinds"] == ["boundary"]
    patch = {"autonomy": {"human_question_kinds": None}}  # a pack that writes the whole workflow and leaves it out
    cfg = load_config(tmp_cfg(patch))
    assert "human_question_kinds" not in cfg.autonomy and human_kinds(cfg) == ("boundary",)
    assert not [path for _, path in errors(issues_for(tmp_cfg, patch)) if path.startswith("autonomy")]


@pytest.mark.parametrize("value", [["boundary", "taste"], "boundary", [1], {"boundary": True}])
def test_human_question_kinds_must_be_a_list_of_known_kinds(tmp_cfg, value):
    found = errors(issues_for(tmp_cfg, {"autonomy": {"human_question_kinds": value}}))
    assert ("error", "autonomy.human_question_kinds") in found


@pytest.mark.parametrize("value", [[], ["boundary", "spec_gap"]])
def test_human_question_kinds_may_be_empty_or_name_any_question_kind(tmp_cfg, value):
    found = errors(issues_for(tmp_cfg, {"autonomy": {"human_question_kinds": value}}))
    assert not [path for _, path in found if path.startswith("autonomy")]


def test_answer_recommended_must_be_a_bool(tmp_cfg):
    found = errors(issues_for(tmp_cfg, {"autonomy": {"answer_recommended": "sometimes"}}))
    assert ("error", "autonomy.answer_recommended") in found


def test_a_gate_is_no_agent_check_unless_it_says_so(tmp_cfg):
    assert load_config(tmp_cfg()).gates["floor"].agent_check is False
    cfg = load_config(tmp_cfg({"gates": {"floor": {"run": "true", "agent_check": True}}}))
    assert cfg.gates["floor"].agent_check is True


def test_agent_check_must_be_a_bool(tmp_cfg):
    with pytest.raises(ConfigError) as e:
        load_config(tmp_cfg({"gates": {"floor": {"run": "true", "agent_check": "yes"}}}))
    assert "gates.floor.agent_check" in str(e.value)


@pytest.mark.parametrize("gate", [
    {"run": "true", "cwd": "web", "agent_check": True},
    {"run": "a\nb", "agent_check": True},
    {"run": "echo `x`", "agent_check": True},
])
def test_validate_refuses_an_agent_check_the_agent_cannot_run_as_written(tmp_cfg, gate):
    found = issues_for(tmp_cfg, {"gates": {"floor": gate}})
    assert [i.message for i in found if i.level == "error" and i.path == "gates.floor.agent_check"
            and "worktree root" in i.message]
    off = issues_for(tmp_cfg, {"gates": {"floor": {**gate, "agent_check": False}}})
    assert ("error", "gates.floor.agent_check") not in errors(off)


# --------------------------------------------------------------------------- the memory section


def test_memory_defaults(tmp_cfg):
    cfg = load_config(tmp_cfg({"memory": {"lessons_max_tokens": None}}))  # the suite's pack sets it to 0; deleted, the default shows
    assert (cfg.memory.lessons_max_tokens, cfg.memory.digest_tokens, cfg.memory.retro_every_days) == (1500, 15000, 7)


@pytest.mark.parametrize("key,value", [("lessons_max_tokens", 4001), ("lessons_max_tokens", -1),
                                       ("digest_tokens", 999), ("digest_tokens", 50001), ("retro_every_days", 91),
                                       ("retro_every_days", -1)])
def test_memory_bounds_are_validated(tmp_cfg, key, value):
    assert ("error", f"memory.{key}") in errors(issues_for(tmp_cfg, {"memory": {key: value}}))


@pytest.mark.parametrize("key,value", [("lessons_max_tokens", 0), ("lessons_max_tokens", 4000), ("digest_tokens", 1000),
                                       ("digest_tokens", 50000), ("retro_every_days", 0), ("retro_every_days", 90)])
def test_memory_bounds_are_inclusive(tmp_cfg, key, value):
    assert not [i for i in issues_for(tmp_cfg, {"memory": {key: value}}) if i.path.startswith("memory")]


# --------------------------------------------------------------------------- the graph section


def test_graph_defaults(cfg):
    assert (cfg.graph.enabled, cfg.graph.exclude, cfg.graph.max_file_kb) == (False, (), 512)


@pytest.mark.parametrize("patch, path", [
    ({"max_file_kb": 0}, "graph.max_file_kb"),
    ({"max_file_kb": 4097}, "graph.max_file_kb"),
    ({"max_file_kb": True}, "graph.max_file_kb"),
    ({"exclude": ["/abs"]}, "graph.exclude[0]"),
    ({"exclude": ["a/../b"]}, "graph.exclude[0]"),
    ({"exclude": [".."]}, "graph.exclude[0]"),
    ({"exclude": [""]}, "graph.exclude[0]"),
    ({"exclude": ["x" * 201]}, "graph.exclude[0]"),
    ({"exclude": ["p"] * 65}, "graph.exclude"),
])
def test_graph_keys_are_validated(tmp_cfg, patch, path):
    assert ("error", path) in errors(issues_for(tmp_cfg, {"graph": patch}))


@pytest.mark.parametrize("patch, path", [
    ({"enabled": "yes"}, "graph.enabled"),
    ({"exclude": "generated/"}, "graph.exclude"),
    ({"exclude": [1]}, "graph.exclude"),
    ({"max_file_kb": "big"}, "graph.max_file_kb"),
])
def test_graph_keys_of_the_wrong_type_are_refused_on_load(tmp_cfg, patch, path):
    with pytest.raises(ConfigError) as err:
        load_config(tmp_cfg({"graph": patch}))
    assert err.value.path == path


@pytest.mark.parametrize("patch", [
    {"enabled": True}, {"max_file_kb": 1}, {"max_file_kb": 4096},
    {"exclude": ["generated/", "*.snap", "docs/**/old", "x" * 200]}, {"exclude": ["p"] * 64},
    {"exclude": ["\\#notes", "\\!keep", "C:notes", "a\\..\\b"]},  # gitignore escapes and odd names are patterns too
])
def test_graph_bounds_are_inclusive(tmp_cfg, patch):
    assert not [i for i in issues_for(tmp_cfg, {"graph": patch}) if i.path.startswith("graph")]


# --------------------------------------------------------------------------- the retro step


def test_the_retro_step_comes_from_the_engine_defaults(cfg):
    step = cfg.retro_step
    assert step is not None
    assert (step.id, step.skill, step.role, step.prompt, step.commit) == ("retro", "agentflow-retro", "review",
                                                                          "retro.md", False)
    assert DEFAULTS["retro_step"] == {"id": "retro", "skill": "agentflow-retro", "role": "review", "prompt": "retro.md"}
    assert "retro_step" not in layers.WORKFLOW_KEYS  # a pack that writes its whole workflow keeps it


def test_a_pack_lane_named_retro_is_refused(tmp_cfg):
    def patch(data):
        data["lanes"] = [*data["lanes"], "retro"]

    issues = issues_for(tmp_cfg, patch)
    [issue] = [i for i in issues if i.level == "error" and i.path.startswith("lanes")]
    assert issue.path == "lanes[4]" and "retro" in issue.message


def test_a_broken_retro_step_is_an_error(tmp_cfg):
    issues = issues_for(tmp_cfg, {"retro_step": {"id": "retro", "skill": "no-such-skill", "role": "review",
                                                 "prompt": "retro.md"}})
    assert ("error", "retro_step.skill") in errors(issues)


def test_a_phase_named_retro_is_refused(tmp_cfg):
    def patch(data):
        data["phases"]["retro"] = {"needs_approval": False, "steps": [{"id": "x", "type": "gate", "gates": ["ok"]}]}
        data.setdefault("gates", {})["ok"] = {"run": "true"}

    assert ("error", "phases.retro") in errors(issues_for(tmp_cfg, patch))


# ---- policy: obligations merged by id, the engine floor

PACKS_NODE = Path(__file__).parent / "fixtures" / "packs" / "node" / ".agentflow" / "config.yaml"


def _policy(patch_policy: dict):
    def patch(data: dict) -> None:
        data["policy"] = patch_policy

    return patch


def _drop_step(phase: str, step_id: str):
    def patch(data: dict) -> None:
        steps = data["phases"][phase]["steps"]
        steps[:] = [s for s in steps if s["id"] != step_id]

    return patch


def test_policy_defaults_and_floor():
    cfg = load_config(CFG)
    assert cfg.policy.enforce == "report"
    assert list(cfg.policy.obligations)[-1] == "engine_floor"
    assert cfg.policy.obligations["engine_floor"].require == (Evidence("gates", "verify"),)
    assert set(cfg.policy.obligations) >= {"verified", "reviewed", "plan_approved", "red_then_green", "vuln_reviewed"}
    verified = cfg.policy.obligations["verified"]
    assert verified.lanes == ("feature", "bug", "vuln") and verified.require == (Evidence("step", "verify"),)
    assert str(Evidence("step", "verify")) == "step:verify"


def test_a_pack_cannot_remove_the_floor(tmp_cfg):
    path = tmp_cfg(_policy({"obligations": {"engine_floor": None}}))
    cfg = load_config(path)
    assert list(cfg.policy.obligations)[-1] == "engine_floor"
    found = [i for i in validate(cfg, path.parent.parent) if i.path == "policy.obligations.engine_floor"]
    assert [i.level for i in found] == ["warning"] and "cannot be removed" in found[0].message


def test_a_pack_cannot_replace_the_floor(tmp_cfg):
    path = tmp_cfg(_policy({"obligations": {"engine_floor": {"require": ["step:verify"]}}}))
    cfg = load_config(path)
    assert cfg.policy.obligations["engine_floor"].require == (Evidence("gates", "verify"),)
    assert [i for i in validate(cfg, path.parent.parent) if i.path == "policy.obligations.engine_floor"]


@pytest.mark.parametrize("text, expected", [
    ("gates:verify", Evidence("gates", "verify")),
    ("step:branch_review", Evidence("step", "branch_review")),
    ("approval:plan", Evidence("approval", "plan")),
    ("expect:red_then_green", Evidence("expect", "red_then_green")),
    ("step:", None),
    ("expect:green", None),
    ("merge:x", None),
    ("verify", None),
])
def test_parse_evidence(text, expected):
    assert parse_evidence(text) == expected


@pytest.mark.parametrize("patch, level, path, text", [
    (_policy({"enforce": "strict"}), "error", "policy.enforce", "report"),
    (_policy({"obligations": {"x": {"require": ["step:verify"], "when": "nope"}}}),
     "error", "policy.obligations.x.when", "nope"),
    (_policy({"obligations": {"x": {"require": ["step:verify"], "lanes": ["nope"]}}}),
     "error", "policy.obligations.x.lanes", "nope"),
    (_policy({"obligations": {"x": {"require": ["gates:nope"]}}}),
     "error", "policy.obligations.x.require[0]", "nope"),
    (_policy({"obligations": {"x": {"require": ["step:nope"]}}}),
     "error", "policy.obligations.x.require[0]", "nope"),
    (_policy({"obligations": {"x": {"require": ["approval:build"]}}}),
     "error", "policy.obligations.x.require[0]", "needs_approval"),
    (_policy({"obligations": {"x": {"require": ["merge:x"]}}}),
     "error", "policy.obligations.x.require[0]", "merge:x"),
    (_policy({"obligations": {"x": {"require": []}}}),
     "error", "policy.obligations.x.require", "at least one"),
    (_drop_step("review", "branch_review"), "error", "policy.obligations.reviewed.require[0]",
     "set policy.obligations.reviewed: null"),
    (_policy({"obligations": {"reviewed": None}}), "warning", "policy.obligations.reviewed", "reviewed"),
])
def test_validate_policy_rules(tmp_cfg, patch, level, path, text):
    found = [i for i in issues_for(tmp_cfg, patch) if i.path == path]
    assert found, path
    assert found[0].level == level and text in found[0].message


def _reconciling(step_patch=None, phase="plan", at=1, review=True):
    """The `plan` phase of the fixture pack with a reconcile step and a blocking review, built from steps and
    prompts that exist in the pack (`step_patch` changes the reconcile step)."""
    def patch(data: dict) -> None:
        steps = data["phases"][phase]["steps"]
        reconcile = {"id": "reconcile", "lanes": ["feature"], "skill": "writing-plans", "role": "plan",
                     "prompt": "spec.md", "output": "docs/plans/{date}-{module}-{slug}.md", "commit": True,
                     "message": "Revise the plan", "reconciles": True}
        reconcile.update(step_patch or {})
        steps.insert(at, reconcile)
        if review:
            steps.append({"id": "doubt_review", "lanes": ["feature"], "skill": "requesting-code-review",
                          "role": "review", "prompt": "branch_review.md", "blocks_on_findings": True})

    return patch


def _both(*patches):
    def patch(data: dict) -> None:
        for each in patches:
            each(data)

    return patch


def _set(*keys_and_value):
    *keys, value = keys_and_value

    def patch(data: dict) -> None:
        node = data
        for key in keys[:-1]:
            node = node.setdefault(key, {})
        node[keys[-1]] = value

    return patch


def _nest_reconcile(data: dict) -> None:
    _reconciling()(data)
    steps = data["phases"]["plan"]["steps"]
    steps[1] = {"id": "group", "lanes": ["feature"], "steps": [steps[1]]}


@pytest.mark.parametrize("patch, level, path, text", [
    (_set("autonomy", "non_blocking_severities", "minor"), "error", "autonomy.non_blocking_severities", "list"),
    (_set("autonomy", "non_blocking_severities", [1]), "error", "autonomy.non_blocking_severities", "list"),
    (_set("autonomy", "non_blocking_severities", ["minor", "Medium"]),
     "error", "autonomy.non_blocking_severities", "always blocks"),
    (_set("autonomy", "non_blocking_severities", ["HIGH"]), "error", "autonomy.non_blocking_severities", "HIGH"),
    (_set("autonomy", "non_blocking_severities", ["critical"]), "error", "autonomy.non_blocking_severities", "critical"),
    (_set("autonomy", "non_blocking_severities", [""]), "error", "autonomy.non_blocking_severities", "always blocks"),
    (_set("autonomy", "non_blocking_severities", ["none"]), "error", "autonomy.non_blocking_severities", "none"),
    (_set("autonomy", "non_blocking_severities", ["null"]), "error", "autonomy.non_blocking_severities", "null"),
    (_set("autonomy", "finding_rounds", 0), "error", "autonomy.finding_rounds", "1 or more"),
    (_set("autonomy", "finding_rounds", -1), "error", "autonomy.finding_rounds", "1 or more"),
    (_set("autonomy", "finding_rounds", True), "error", "autonomy.finding_rounds", "whole number"),
    (_reconciling({"type": "gate"}), "error", "phases.plan.steps[1].reconciles", "agent step"),
    (_reconciling({"role": "review"}), "error", "phases.plan.steps[1].reconciles", "role: review"),
    (_reconciling({"output": None}), "error", "phases.plan.steps[1].reconciles", "output"),
    (_reconciling({"commit": False}), "error", "phases.plan.steps[1].reconciles", "commit: true"),
    (_reconciling({"commit": "per_task"}), "error", "phases.plan.steps[1].reconciles", "commit: true"),
    (_reconciling({"gates": "build"}), "error", "phases.plan.steps[1].reconciles", "runs no `gates`"),
    (_reconciling({"gates": ["floor"]}), "error", "phases.plan.steps[1].reconciles", "runs no `gates`"),
    (_both(_reconciling(), lambda d: d["phases"]["plan"]["steps"].insert(
        2, {"id": "again", "lanes": ["feature"], "skill": "writing-plans", "role": "plan", "prompt": "spec.md",
            "output": "docs/plans/x.md", "commit": True, "message": "x", "reconciles": True})),
     "error", "phases.plan.steps[2].reconciles", "at most one"),
    (_both(_reconciling(), lambda d: d["phases"]["plan"]["steps"].reverse()),
     "error", "phases.plan.steps[1].reconciles", "after a `blocks_on_findings`"),
    (_nest_reconcile, "error", "phases.plan.steps[1].steps[0].reconciles", "top level"),
    (_both(_reconciling(), _set("retro_step", {"id": "retro", "skill": "writing-plans", "role": "review",
                                               "prompt": "refine.md", "reconciles": True})),
     "error", "retro_step.reconciles", "only a step in a phase"),
    (_both(_reconciling(phase="build", at=1), lambda d: d["phases"]["build"]["steps"].__setitem__(
        slice(None), [d["phases"]["build"]["steps"][0], d["phases"]["build"]["steps"][1]])),
     "warning", "phases.build.steps[1].reconciles", "needs_approval: false"),
    (_reconciling(review=False), "warning", "phases.plan.steps[1].reconciles", "no `blocks_on_findings` step"),
    (lambda d: d["phases"]["plan"]["steps"].append(
        {"id": "doubt_review", "lanes": ["feature"], "skill": "requesting-code-review", "role": "review",
         "prompt": "branch_review.md", "blocks_on_findings": True}),
     "warning", "phases.plan", "straight to the human"),
    (_set("skills", "map", {"doubt": "doubt-driven-development"}),
     "error", "skills.map.doubt", "user-only"),
])
def test_validate_doubt_rules(tmp_cfg, patch, level, path, text):
    found = [i for i in issues_for(tmp_cfg, patch) if i.path == path]
    assert found, path
    assert found[0].level == level and text in found[0].message


def test_the_doubt_skill_is_a_known_skills_map_key(tmp_cfg):
    issues = issues_for(tmp_cfg, _set("skills", "map", {"doubt": "requesting-code-review"}))
    assert not [i for i in issues if i.path.startswith("skills")]


def test_reconciles_must_be_a_bool(tmp_cfg):
    with pytest.raises(ConfigError, match="reconciles must be true or false"):
        load_config(tmp_cfg(_reconciling({"reconciles": "false"})))


def test_an_absent_finding_rounds_is_not_an_error(tmp_cfg):
    def patch(data: dict) -> None:
        del data["autonomy"]

    assert not [i for i in issues_for(tmp_cfg, patch) if i.path.startswith("autonomy")]


def test_a_default_naming_a_lane_the_pack_lacks_never_applies(tmp_cfg):
    def patch(data: dict) -> None:
        data["lanes"] = ["feature"]

    issues = issues_for(tmp_cfg, patch)
    assert not [i for i in issues if i.path.startswith("policy")]


def test_conformance_failed_is_never_resolved(tmp_cfg):
    found = errors(issues_for(tmp_cfg, {"autonomy": {"resolve_kinds": {"conformance_failed": 1}}}))
    assert ("error", "autonomy.resolve_kinds.conformance_failed") in found


def test_every_pack_validates_clean(repo_root):
    for path, root in ((repo_root / ".agentflow" / "config.yaml", repo_root), (PYTHON_CONFIG, repo_root),
                       (PACKS_NODE, PACKS_NODE.parent.parent)):
        assert validate(load_config(path), root) == [], path


def test_turning_off_plan_approval_only_warns_for_the_default_that_needs_it(tmp_cfg):
    found = [i for i in issues_for(tmp_cfg, {"phases": {"plan": {"needs_approval": False}}})
             if i.path.startswith("policy")]
    assert [(i.level, i.path) for i in found] == [("warning", "policy.obligations.plan_approved.require[0]")]
    assert "set policy.obligations.plan_approved: null" in found[0].message


@pytest.mark.parametrize("patch", [{"policy": None}, {"policy": {"obligations": None}}])
def test_a_null_policy_or_obligations_reports_every_default_it_switches_off(tmp_cfg, patch):
    def apply(data: dict) -> None:
        data.update(patch)

    path = tmp_cfg(apply)
    cfg = load_config(path)
    assert list(cfg.policy.obligations) == ["advisory_cleared", "engine_floor"]  # the two floors, whatever the layers say
    assert cfg.policy.disabled == ("verified", "reviewed", "plan_approved", "red_then_green", "vuln_reviewed")
    found = {i.path: i for i in validate(cfg, path.parent.parent) if i.level == "warning"}
    for key in cfg.policy.disabled:
        assert key in found[f"policy.obligations.{key}"].message


def test_a_policy_that_is_not_a_mapping_is_a_config_error_not_a_crash(tmp_cfg):
    with pytest.raises(ConfigError, match="policy"):
        load_config(tmp_cfg(lambda data: data.update({"policy": 5})))


def test_preflight_enabled_must_be_a_bool(tmp_cfg):
    with pytest.raises(ConfigError, match=r"preflight\.enabled"):
        load_config(tmp_cfg({"preflight": {"enabled": "yes"}}))
    found = issues_for(tmp_cfg, {"preflight": {"nope": 1}})
    assert [(i.path, i.message) for i in found if i.level == "error"] == [("preflight.nope", "unknown preflight key")]
    assert load_config(tmp_cfg({"preflight": {"enabled": True}})).preflight.enabled is True
    assert load_config(tmp_cfg({"preflight": None})).preflight.enabled is False


def test_usage_defaults_load(tmp_cfg):
    assert load_config(tmp_cfg()).usage == Usage("on", 0.75, 0.92, 0.85, True)


def test_a_bare_off_loads_as_off_and_is_a_warning(tmp_cfg):
    assert load_config(tmp_cfg({"usage": {"governor": False}})).usage.governor == "off"
    issues = issues_for(tmp_cfg, {"usage": {"governor": False}})
    assert [(i.level, i.path) for i in issues if "governor" in i.path] == [("warning", "usage.governor")]
    assert load_config(tmp_cfg({"usage": {"governor": True}})).usage.governor == "on"


def test_governor_is_a_reserved_step_id(tmp_cfg):
    def patch(data):
        phase_step(data, "build", "build")["id"] = "governor"
    found = issues_for(tmp_cfg, patch)
    assert any(i.level == "error" and "step id 'governor' is reserved" in i.message for i in found)


def test_claim_is_a_reserved_step_id(tmp_cfg):
    def patch(data):
        phase_step(data, "build", "build")["id"] = "claim"
    found = issues_for(tmp_cfg, patch)
    assert any(i.level == "error" and "step id 'claim' is reserved" in i.message for i in found)


@pytest.mark.parametrize("block, path, message", [
    ({"governor": "sometimes"}, "usage.governor", 'governor must be "on", "observe" or "off"'),
    ({"five_hour_soft": 0.95}, "usage.five_hour_soft", "five_hour_soft must be above 0 and below five_hour_hard"),
    ({"five_hour_soft": 0}, "usage.five_hour_soft", "five_hour_soft must be above 0 and below five_hour_hard"),
    ({"five_hour_soft": float("nan")}, "usage.five_hour_soft", "five_hour_soft must be above 0 and below five_hour_hard"),
    ({"five_hour_hard": 1.2}, "usage.five_hour_hard", "five_hour_hard must be at most 1"),
    ({"five_hour_hard": float("inf")}, "usage.five_hour_hard", "five_hour_hard must be at most 1"),
    ({"seven_day_soft": 0}, "usage.seven_day_soft", "seven_day_soft must be above 0 and at most 1"),
    ({"soft": 0.7}, "usage.soft", "unknown usage key"),
])
def test_validate_refuses_bad_usage(tmp_cfg, block, path, message):
    found = issues_for(tmp_cfg, {"usage": block})
    assert any(i.level == "error" and i.path == path and message in i.message for i in found), found


def test_validate_usage_is_the_usage_part_of_validate(tmp_cfg):
    path = tmp_cfg({"usage": {"five_hour_soft": 0.95, "soft": 0.7}})
    cfg = load_config(path)
    mine = [i for i in validate(cfg, path.parent.parent) if i.path == "usage" or i.path.startswith("usage.")]
    assert mine and validate_usage(cfg) == mine


@pytest.mark.parametrize("block", [{"five_hour_soft": True}, {"finish_first": "yes"}])
def test_a_wrongly_typed_usage_value_is_a_load_error(tmp_cfg, block):
    (key,) = block
    with pytest.raises(ConfigError, match=rf"usage\.{key}"):
        load_config(tmp_cfg({"usage": block}))


def test_validate_takes_the_shipped_usage_block():
    issues = validate(load_config(ROOT / ".agentflow" / "config.yaml"), ROOT)
    assert [(i.level, i.path) for i in issues if i.level in ("error", "warning")] == []
    assert yaml.safe_load(layers.ENGINE_DEFAULTS.read_text(encoding="utf-8"))["usage"]["governor"] == "on"


def test_watchdog_no_progress_min_must_be_zero_or_more(tmp_cfg):
    found = issues_for(tmp_cfg, {"watchdog": {"no_progress_min": -1}})
    assert [i.path for i in found if i.level == "error"] == ["watchdog.no_progress_min"]
    with pytest.raises(ConfigError, match=r"watchdog\.no_progress_min"):
        load_config(tmp_cfg({"watchdog": {"no_progress_min": True}}))
    found = issues_for(tmp_cfg, {"watchdog": {"nope": 1}})
    assert [(i.path, i.message) for i in found if i.level == "error"] == [("watchdog.nope", "unknown watchdog key")]
    assert load_config(tmp_cfg({"watchdog": {"no_progress_min": 12.5}})).watchdog.no_progress_min == 12.5
    assert load_config(tmp_cfg({"watchdog": None})).watchdog.no_progress_min == 15  # the engine default


def test_ci_watch_max_h_defaults_off_and_may_be_left_out(sparse_repo, tmp_cfg):
    assert load_config(pack.pack_config(sparse_repo)).ci["watch_max_h"] == 0  # the engine layer supplies it, off
    owned = load_config(tmp_cfg())  # a pack that writes its own workflow, `ci` included, without the key: off too
    assert "watch_max_h" not in owned.ci and not errors(validate(owned, tmp_cfg().parent.parent))


def test_early_pr_after_must_name_a_phase_before_finish(sparse_repo, tmp_cfg):
    assert load_config(pack.pack_config(sparse_repo)).ci["early_pr_after"] == ""  # the engine layer: off
    owned = load_config(tmp_cfg())  # a pack that writes its own `ci` without the key: off too
    assert "early_pr_after" not in owned.ci and not errors(validate(owned, tmp_cfg().parent.parent))
    for bad in ("ship", "nope", 3, True, ["verify"]):
        found = issues_for(tmp_cfg, {"ci": {"early_pr_after": bad}})
        assert [i.path for i in found if i.level == "error"] == ["ci.early_pr_after"], bad
    found = issues_for(tmp_cfg, {"ci": {"early_pr_after": "ship"}})
    assert "before the phase" in next(i.message for i in found if i.path == "ci.early_pr_after")
    for good in ("", "verify", "review", "define"):
        assert not errors(issues_for(tmp_cfg, {"ci": {"early_pr_after": good}})), good


def test_ci_watch_max_h_must_be_finite_and_zero_or_more(tmp_cfg):
    for bad in (-1, float("inf"), float("nan"), 721, 10**400, True):
        found = issues_for(tmp_cfg, {"ci": {"watch_max_h": bad}})
        assert [i.path for i in found if i.level == "error"] == ["ci.watch_max_h"], bad
    for good in (0, 0.05, 6, 720):
        assert not errors(issues_for(tmp_cfg, {"ci": {"watch_max_h": good}})), good
    found = issues_for(tmp_cfg, {"watchdog": {"no_progress_min": float("nan")}})
    assert [i.path for i in found if i.level == "error"] == ["watchdog.no_progress_min"]
    found = issues_for(tmp_cfg, {"watchdog": {"no_progress_min": float("inf")}})
    assert [i.path for i in found if i.level == "error"] == ["watchdog.no_progress_min"]


def test_dashboard_board_refresh_s_is_half_a_second_to_a_minute(tmp_cfg):
    for bad in (0, 0.4, 61, True, "2", float("inf"), float("nan")):
        found = issues_for(tmp_cfg, {"dashboard": {"board_refresh_s": bad}})
        assert [i.path for i in found if i.level == "error"] == ["dashboard.board_refresh_s"], bad
    for good in (0.5, 2, 60):
        assert not errors(issues_for(tmp_cfg, {"dashboard": {"board_refresh_s": good}})), good


def test_dashboard_artifact_max_kb_is_a_whole_number_from_16_to_8192(tmp_cfg):
    for bad in (0, 15, 8193, 512.5, True, "512"):
        found = issues_for(tmp_cfg, {"dashboard": {"artifact_max_kb": bad}})
        assert [i.path for i in found if i.level == "error"] == ["dashboard.artifact_max_kb"], bad
    for good in (16, 512, 8192):
        assert not errors(issues_for(tmp_cfg, {"dashboard": {"artifact_max_kb": good}})), good


def test_watchdog_no_progress_min_loads_as_a_float_and_an_oversized_int_as_inf(tmp_cfg):
    """A 400-digit integer never reaches the watcher thread's `limit / 4` as an int: it loads as `inf`, which
    `validate` refuses."""
    loaded = load_config(tmp_cfg({"watchdog": {"no_progress_min": 15}})).watchdog.no_progress_min
    assert loaded == 15 and isinstance(loaded, float)
    huge = load_config(tmp_cfg({"watchdog": {"no_progress_min": 10**400}})).watchdog.no_progress_min
    assert huge == float("inf")
    found = issues_for(tmp_cfg, {"watchdog": {"no_progress_min": 10**400}})
    assert [i.path for i in found if i.level == "error"] == ["watchdog.no_progress_min"]


def test_watchdog_no_progress_min_is_capped_at_a_day(tmp_cfg):
    found = issues_for(tmp_cfg, {"watchdog": {"no_progress_min": 1441}})
    assert [i.path for i in found if i.level == "error"] == ["watchdog.no_progress_min"]
    for good in (0, 0.02, 10, 1440):  # no floor for a small positive value: the watchdog's own tests use 0.02
        assert not errors(issues_for(tmp_cfg, {"watchdog": {"no_progress_min": good}})), good


# --------------------------------------------------------------------------- gate groups and resources


def test_gate_group_and_resource_are_names(tmp_cfg):
    cfg = load_config(tmp_cfg({"gates": {"floor": {"run": "true", "group": "api", "resource": "db.1"}}}))
    assert (cfg.gates["floor"].group, cfg.gates["floor"].resource) == ("api", "db.1")
    assert load_config(tmp_cfg()).gates["floor"].group is None
    found = errors(issues_for(tmp_cfg, {"gates": {"floor": {"run": "true", "group": "a b", "resource": ""}}}))
    assert ("error", "gates.floor.group") in found
    assert ("error", "gates.floor.resource") in found
    assert ("error", "gates.floor.group") not in errors(
        issues_for(tmp_cfg, {"gates": {"floor": {"run": "true", "group": "api", "resource": "db"}}}))


def test_a_grouped_setup_gate_is_a_warning(tmp_cfg):
    found = _warnings(issues_for(tmp_cfg, {"gates": {"setup_py": {"run": "uv sync", "group": "api"}}}))
    assert ("gates.setup_py.group", "setup gates start things in order; drop `group`") in found
    assert not [w for w in _warnings(issues_for(tmp_cfg, {"gates": {"floor": {"run": "true", "group": "api"}}}))
                if w[0] == "gates.floor.group"]


# --------------------------------------------------------------------------- gate inputs


def test_gate_inputs_must_be_relative_globs(tmp_cfg):
    cfg = load_config(tmp_cfg({"gates": {"floor": {"run": "true", "inputs": ["api/**", "pyproject.toml"]}}}))
    assert cfg.gates["floor"].inputs == ["api/**", "pyproject.toml"]
    assert load_config(tmp_cfg()).gates["floor"].inputs == []
    for bad in ([], [""], ["/abs/**"], ["../up/**"], ["a/../b"], ["ok", 3], [" "]):
        found = errors(issues_for(tmp_cfg, {"gates": {"floor": {"run": "true", "inputs": bad}}}))
        assert ("error", "gates.floor.inputs") in found, bad
    assert ("error", "gates.floor.inputs") not in errors(
        issues_for(tmp_cfg, {"gates": {"floor": {"run": "true", "inputs": ["./api/**"]}}}))
    with pytest.raises(ConfigError, match="gates.floor.inputs"):  # not a list at all: refused when the pack loads
        load_config(tmp_cfg({"gates": {"floor": {"run": "true", "inputs": "api/**"}}}))


def test_a_verify_gate_without_inputs_warns_only_once_a_pack_uses_inputs(tmp_cfg):
    def inputs_warnings(patch):
        return [w for w in _warnings(issues_for(tmp_cfg, patch)) if "has no `inputs`" in w[1]]

    assert inputs_warnings(None) == []  # no gate declares `inputs`: today's packs stay at 0 warnings
    cfg = load_config(tmp_cfg())
    verify = cfg.gate_sets["verify"]
    assert len(verify) >= 2
    patch = {"gates": {verify[0]: {"inputs": ["api/**"]}}}
    found = inputs_warnings(patch)
    assert [w[0] for w in found] == [f"gates.{name}" for name in verify[1:]]
    assert all(w[1] == f"`{w[0].split('.', 1)[1]}` has no `inputs`, so it runs again after every change" for w in found)
    both = {"gates": {name: {"inputs": ["api/**"]} for name in verify}}
    assert inputs_warnings(both) == []


# --------------------------------------------------------------------------- the affected tier


def test_affected_allows_only_the_paths_placeholder(tmp_cfg):
    def affected(value):
        return {"gates": {"floor": {"run": "true", "affected": value}}}

    cfg = load_config(tmp_cfg(affected({"run": "pytest {paths}", "paths": ["tests/**/test_*.py"]})))
    assert (cfg.gates["floor"].affected.run, cfg.gates["floor"].affected.paths) == ("pytest {paths}", ["tests/**/test_*.py"])
    assert load_config(tmp_cfg()).gates["floor"].affected is None
    assert load_config(tmp_cfg(affected({"run": "pytest {paths}"}))).gates["floor"].affected.paths == ["**"]
    with_inputs = {"gates": {"floor": {"run": "true", "inputs": ["api/**"], "affected": {"run": "pytest {paths}"}}}}
    assert load_config(tmp_cfg(with_inputs)).gates["floor"].affected.paths == ["api/**"]
    for good in ("pytest {paths}", "pytest", "echo {{x}} ${{HOME}} {paths} {paths}"):
        found = errors(issues_for(tmp_cfg, affected({"run": good, "paths": ["**/*.py"]})))
        assert ("error", "gates.floor.affected.run") not in found, good
    for bad in ("", "  ", "pytest {changed}", "pytest {}", "pytest {paths!r}", "pytest {paths:>3}", "pytest {0}",
                "pytest {paths.x}", "pytest { paths}", "echo ${HOME}", "pytest {paths", "pytest }"):
        found = errors(issues_for(tmp_cfg, affected({"run": bad, "paths": ["**/*.py"]})))
        assert ("error", "gates.floor.affected.run") in found, bad
    for bad in ([], [""], ["/abs/**"], ["../up/**"], ["a/../b"], ["ok", 3]):
        found = errors(issues_for(tmp_cfg, affected({"run": "pytest {paths}", "paths": bad})))
        assert ("error", "gates.floor.affected.paths") in found, bad
    for bad in ("pytest {paths}", {"paths": ["**"]}, {"run": 3}, {"run": "x", "paths": "**"}, {"run": "x", "other": 1}):
        with pytest.raises(ConfigError, match="gates.floor.affected"):
            load_config(tmp_cfg(affected(bad)))


@pytest.mark.parametrize("template", [
    "echo '{paths}'", 'echo "{paths}"', "echo `{paths}`", "echo $({paths})", "echo $(ls {paths})",
    'echo "a {paths} b"', "echo ${{x:-{paths}}}", "echo x # {paths}", "echo x{paths}", "echo {paths}x",
    "cat <<EOF\n{paths}\nEOF", "echo '{paths}' {paths}", "echo \"$(echo ')')\" '{paths}'",
])
def test_affected_refuses_a_paths_placeholder_that_is_not_its_own_unquoted_word(tmp_cfg, template):
    patch = {"gates": {"floor": {"run": "true", "affected": {"run": template, "paths": ["**/*.py"]}}}}
    assert ("error", "gates.floor.affected.run") in errors(issues_for(tmp_cfg, patch))


@pytest.mark.parametrize("template", [
    "echo {paths}", "pytest {paths} -q", "pytest -q {paths}; echo done", "pytest {paths}|tee log", "a && b {paths}",
    "echo '#' {paths}", "echo \"it's\" {paths}", "echo $(date) {paths}", "printf '%s\\n' {paths} > out.txt",
    "echo {{x}} {paths}", "echo ${{HOME}} {paths}", "x <<< y {paths}", "cd api &&\n  pytest {paths}",
])
def test_affected_accepts_a_paths_placeholder_that_is_its_own_unquoted_word(tmp_cfg, template):
    patch = {"gates": {"floor": {"run": "true", "affected": {"run": template, "paths": ["**/*.py"]}}}}
    assert ("error", "gates.floor.affected.run") not in errors(issues_for(tmp_cfg, patch))


# Each of these let a file name run as shell code (`x$(touch PWNED)y.py`) through a `{paths}` the scanner thought safe.
HOSTILE_TEMPLATES = [
    "echo \"$(echo '\"')') {paths} \"",  # `'` is ignored while a `"` is open inside `$(...)`
    "echo \\{paths} \\'",  # an escaped placeholder is still replaced by the renderer
    "echo $'\\' {paths} \\'",  # ANSI-C quoting
    "echo \"$(case x in (x) \" {paths} \";; esac)\"",  # a case-pattern `)` closes `$(` early
    "echo \"`echo`\" {paths}", "echo \"${{x}}\" {paths}", "echo {paths} '", "echo {paths} \"", "echo {paths} $(echo",
    "echo $(echo # ') x\n) ' {paths} 'y'", "echo {paths} `echo", "echo $(echo (a)) {paths}", "echo \\{{paths}}",
    "echo x\\{paths}", "echo {paths} $'a'",
    # A `#` right after an operator or a line continuation starts a comment that hides a quote from the scan.
    "echo a;#'\n' {paths} \\'", "echo a&&#'\n' {paths} \\'", "echo a|#'\n' {paths} \\'", "(#'\n' {paths} \\'\n)",
    "echo a \\\n#'\n' {paths} \\'",
    "echo `true '`'`'\"' {paths} \"'\"'\\'",  # the shell ignores `'` when it looks for the closing backtick
    "echo `(true)` {paths}",  # an unquoted `(` inside backticks, as inside `$(...)`
    "echo $[\n{paths} ]", "(( {paths} ))",  # arithmetic reads its words as if double-quoted (found by the fuzz)
    "echo \"${x:-'}\" {paths} \"'}\"",  # a lone brace: the renderer leaves `${`, whose `'` bash honours inside `"`
    # The shell deletes a line continuation before it reads tokens, so it can glue `$(`, `$'`, `${` or `<<` together.
    "echo \"$\\\n( echo \" {paths} \" )\"", "echo $\\\n'\\' {paths} \\'", "cat <\\\n<EOF\n'\nEOF\n' {paths} \\'",
    "echo \"$\\\n{{x:-\" {paths} \"}}\"",
]


@pytest.mark.parametrize("template", HOSTILE_TEMPLATES)
def test_affected_refuses_a_template_whose_quoting_could_let_a_name_run(tmp_cfg, template):
    patch = {"gates": {"floor": {"run": "true", "affected": {"run": template, "paths": ["**/*.py"]}}}}
    assert ("error", "gates.floor.affected.run") in errors(issues_for(tmp_cfg, patch)), template


@pytest.mark.parametrize("template", HOSTILE_TEMPLATES)
def test_the_scanner_refuses_every_hostile_template(template):
    from agentflow.config import unquoted_paths_word

    assert not unquoted_paths_word(template), template


@pytest.mark.parametrize("template", [
    "pytest {paths}", "ruff check {paths}", "cmd --flag {paths} | tail -n 5", "cmd {paths} && other",
    "echo $(date) {paths}", "echo \"$HOME\" '{{x}}' {paths}", "pytest -q {paths} 2>&1", "cd api &&\n  pytest {paths}",
    "echo a#b {paths}", "echo '#' \"#\" {paths}", "git log --format=%h#%s {paths}", "pytest \\\n  {paths}",
    "pytest -q \\\n  --tb=short \\\n  {paths}",
])
def test_the_scanner_still_accepts_the_ordinary_templates(template):
    from agentflow.config import unquoted_paths_word

    assert unquoted_paths_word(template), template


def test_gate_tier_needs_gates(tmp_cfg):
    def tier(step_id: str, value):
        def edit(data):
            phase_step(data, "verify", step_id)["gate_tier"] = value
        return edit

    cfg = load_config(tmp_cfg(tier("fix_findings", "affected")))
    assert phase_step_of(cfg, "verify", "fix_findings").gate_tier == "affected"
    assert phase_step_of(load_config(tmp_cfg()), "verify", "fix_findings").gate_tier == "full"
    assert not [e for e in errors(issues_for(tmp_cfg, tier("fix_findings", "affected"))) if e[1].endswith(".gate_tier")]
    assert not [e for e in errors(issues_for(tmp_cfg, tier("plan_fixes", "full"))) if e[1].endswith(".gate_tier")]
    found = [i for i in issues_for(tmp_cfg, tier("plan_fixes", "affected")) if i.path.endswith(".gate_tier")]
    assert found and all(i.level == "error" and "needs a step with `gates`" in i.message for i in found)  # each use of the anchor
    for bad in ("fast", "", 1, True):
        assert [e for e in errors(issues_for(tmp_cfg, tier("fix_findings", bad))) if e[1].endswith(".gate_tier")], bad


def phase_step_of(cfg, phase: str, step_id: str):
    return next(s for s in cfg.phases[phase].steps if s.id == step_id)


def test_the_engine_defaults_mark_the_fix_steps_affected(sparse_repo):
    cfg = _sparse_config(sparse_repo)
    assert phase_step_of(cfg, "review", "fix_findings").gate_tier == "affected"
    assert phase_step_of(cfg, "ship", "finish").sync.gate_tier == "affected"
    # `ci.fix` stays full: `ci_lane` pushes right after the fix and no conformance audit follows, so an affected-only
    # check would ship a tree no full verify has seen.
    assert phase_step_of(cfg, "ship", "ci").fix.gate_tier == "full"
    assert phase_step_of(cfg, "verify", "verify").gate_tier == "full"
    assert phase_step_of(cfg, "build", "fix").gate_tier == "full"


def test_dashboard_done_shown_is_a_whole_number_from_1_to_1000(tmp_cfg):
    for bad in (0, 1001, 2.5, True, "10"):
        found = issues_for(tmp_cfg, {"dashboard": {"done_shown": bad}})
        assert [i.path for i in found if i.level == "error"] == ["dashboard.done_shown"], bad
    for good in (1, 10, 1000):
        assert not errors(issues_for(tmp_cfg, {"dashboard": {"done_shown": good}})), good


def test_dashboard_inbox_legacy_is_true_or_false(tmp_cfg):
    for bad in ("yes", 1, 0):  # a null never reaches the check: the layer merge reads it as "delete the key"
        found = issues_for(tmp_cfg, {"dashboard": {"inbox_legacy": bad}})
        assert [i.path for i in found if i.level == "error"] == ["dashboard.inbox_legacy"], bad
    assert not errors(issues_for(tmp_cfg, {"dashboard": {"inbox_legacy": True}}))


# --------------------------------------------------------------------------- route profiles


def with_route(route: dict, doubt: bool = True):
    """A `tmp_cfg` patch: the python pack (with the doubt review, so `plan` has three feature steps) and `route`."""
    def patch(data: dict) -> None:
        if doubt:
            with_doubt(data)
        data["route"] = route
    return patch


def route_issues(tmp_cfg, route: dict, doubt: bool = True) -> list[Issue]:
    return issues_for(tmp_cfg, with_route(route, doubt))


def test_route_defaults_are_off_with_the_grid(sparse_repo):
    cfg = load_config(pack.pack_config(sparse_repo))
    assert cfg.route.mode == "off"
    assert cfg.route.lanes == ["feature"]
    assert set(cfg.route.profiles) == {"XS", "S", "M", "L"}
    assert cfg.route.profiles["S"].doubt_rounds == {"define": 1, "plan": 1}
    assert cfg.route.profiles["XS"].per_task_review is False
    assert cfg.route.profiles["XS"].skip == ("define/grill_open_questions", "define/spec_review", "plan/plan_review")
    assert cfg.route.profiles["L"].per_task_review is True


def test_the_default_grid_validates_clean(sparse_repo):
    cfg = load_config(pack.pack_config(sparse_repo))
    assert [i for i in validate(cfg, sparse_repo) if "route" in i.path] == []


def test_the_python_pack_has_no_profiles(tmp_cfg):
    cfg = load_config(tmp_cfg())
    assert cfg.route.profiles == {}
    assert cfg.route.mode == "off"


@pytest.mark.parametrize("route, path, text", [
    ({"mode": "sometimes"}, "route.mode", "off, observe, on"),
    ({"lanes": ["nope"]}, "route.lanes", "unknown lane"),
    ({"profiles": {"XL": {}}}, "route.profiles.XL", "XS, S, M, L"),
    ({"profiles": {"S": {"grill": 1}}}, "route.profiles.S.grill", "skip, doubt_rounds, per_task_review"),
    ({"profiles": {"S": {"skip": ["define/nope"]}}}, "route.profiles.S.skip", "no step 'nope'"),
    ({"profiles": {"S": {"skip": ["nope/draft"]}}}, "route.profiles.S.skip", "not a phase"),
    ({"profiles": {"S": {"skip": ["draft"]}}}, "route.profiles.S.skip", "<phase>/<id>"),
    ({"profiles": {"S": {"skip": ["build/build"]}}}, "route.profiles.S.skip", "foreach"),
    ({"profiles": {"S": {"doubt_rounds": {"define": 0}}}}, "route.profiles.S.doubt_rounds.define",
     "the engine floor keeps at least one round"),
    ({"profiles": {"S": {"doubt_rounds": {"nope": 1}}}}, "route.profiles.S.doubt_rounds.nope", "not a phase"),
    ({"profiles": {"S": {"per_task_review": "no"}}}, "route.profiles.S.per_task_review", "true or false"),
])
def test_route_rejects_bad_values(tmp_cfg, route, path, text):
    found = [i for i in route_issues(tmp_cfg, route) if i.level == "error" and i.path == path]
    assert found, route_issues(tmp_cfg, route)
    assert text in found[0].message


def test_a_profile_may_not_drop_an_obligation(tmp_cfg):
    found = route_issues(tmp_cfg, {"profiles": {"S": {"skip": ["verify/verify"]}}})
    messages = [i.message for i in found if i.level == "error" and i.path == "route.profiles.S.skip"]
    assert any("skipping verify/verify drops the obligation verified" in m for m in messages), found
    found = route_issues(tmp_cfg, {"profiles": {"S": {"skip": ["ship/finish"]}}})
    assert ("error", "route.profiles.S.skip") in errors(found)


def test_a_profile_may_skip_a_step_no_obligation_needs(tmp_cfg):
    found = route_issues(tmp_cfg, {"profiles": {"S": {"skip": ["define/contracts"]}}})
    assert errors(found) == []
    # an obligation for a lane the route does not cover does not protect the step: `red_then_green` is a bug lane's
    found = route_issues(tmp_cfg, {"lanes": ["bug"], "profiles": {"S": {"skip": ["verify/verify"]}}})
    assert ("error", "route.profiles.S.skip") in errors(found)  # `verified` covers bug too


def test_skipping_every_plan_step_drops_plan_approved(tmp_cfg):
    every = ["plan/plan", "plan/plan_reconcile", "plan/plan_review"]
    found = route_issues(tmp_cfg, {"profiles": {"S": {"skip": every}}})
    assert any(i.level == "error" and "plan_approved" in i.message for i in found), found
    found = route_issues(tmp_cfg, {"profiles": {"S": {"skip": ["plan/plan_review"]}}})
    assert errors(found) == []


def test_route_on_with_no_profiles_warns(tmp_cfg):
    found = route_issues(tmp_cfg, {"mode": "on"})
    assert [i.message for i in found if i.path == "route.profiles"] == [
        "route.mode is on but route.profiles is empty: every profile runs the full route"]
    assert errors(found) == []
    assert not [i for i in route_issues(tmp_cfg, {"mode": "off"}) if i.path == "route.profiles"]


def test_a_bare_yaml_off_keeps_the_route_off_and_still_fails_validate(tmp_cfg):
    path = tmp_cfg(with_route({"mode": "MODE", "profiles": {"S": {}}}))
    path.write_text(path.read_text(encoding="utf-8").replace("mode: MODE", "mode: off"), encoding="utf-8")
    cfg = load_config(path)
    assert cfg.raw["route"]["mode"] is False  # YAML 1.1 reads a bare `off` as false
    assert cfg.route.mode == "off"
    found = [i for i in validate(cfg, path.parent.parent) if i.path == "route.mode"]
    assert [i.level for i in found] == ["error"]
    assert "quote it" in found[0].message
    assert not [i for i in validate(cfg, path.parent.parent) if i.path == "route.profiles"]


@pytest.mark.parametrize("mode", ["sometimes", True, 1, None])
def test_a_mode_outside_the_list_runs_as_off(tmp_cfg, mode):
    cfg = load_config(tmp_cfg(with_route({"mode": mode})))
    assert cfg.route.mode == "off"


def test_a_profile_may_not_skip_a_group_that_holds_an_obligations_step(tmp_cfg):
    def patch(data: dict) -> None:
        with_route({"profiles": {"S": {"skip": ["define/grill_open_questions"]}}})(data)
        data["policy"]["obligations"]["grilled"] = {"lanes": ["feature"], "require": ["step:apply"]}
    found = issues_for(tmp_cfg, patch)
    messages = [i.message for i in found if i.level == "error" and i.path == "route.profiles.S.skip"]
    assert messages == ["skipping define/grill_open_questions drops the obligation grilled"], found


def test_a_profile_may_not_skip_a_step_whose_review_is_an_obligations_step(tmp_cfg):
    def patch(data: dict) -> None:
        with_route({"profiles": {"S": {"skip": ["define/contracts"]}}})(data)
        phase_step(data, "define", "contracts")["review"] = {"id": "contracts_check", "role": "review",
                                                             "prompt": "task_review.md"}
        data["policy"]["obligations"]["checked"] = {"lanes": ["feature"], "require": ["step:contracts_check"]}
    found = issues_for(tmp_cfg, patch)
    messages = [i.message for i in found if i.level == "error" and i.path == "route.profiles.S.skip"]
    assert messages == ["skipping define/contracts drops the obligation checked"], found


@pytest.mark.parametrize("held, text", [("finish", "it holds the `finish` step finish, which ends every run"),
                                        ("ci", "it holds the `ci` step ci, which ends every run"),
                                        ("build", "it holds the `foreach` step build, which is the build itself")])
def test_a_profile_may_not_skip_a_group_that_holds_a_finish_ci_or_foreach_step(tmp_cfg, held, text):
    def patch(data: dict) -> None:
        with_route({"profiles": {"S": {"skip": ["ship/wrapped"]}}})(data)
        source = "build" if held == "build" else "ship"
        inner = phase_step(data, source, held)
        data["phases"][source]["steps"].remove(inner)
        data["phases"]["ship"]["steps"].append({"id": "wrapped", "type": "group", "steps": [inner]})
    found = issues_for(tmp_cfg, patch)
    messages = [i.message for i in found if i.level == "error" and i.path == "route.profiles.S.skip"]
    assert any(m.startswith("skipping ship/wrapped is refused") and text in m for m in messages), found


def claude_issue(tmp_cfg, patch, path: str, prepare=None) -> Issue | None:
    cfg_path = tmp_cfg(patch)
    if prepare:
        prepare(cfg_path.parent.parent)
    return next((i for i in validate(load_config(cfg_path), cfg_path.parent.parent) if i.path == path), None)


def test_claude_starts_on_the_five_minute_cache_lean_and_without_user_settings(tmp_cfg):
    spec = load_config(tmp_cfg()).agents["claude"]
    assert (spec.cache_ttl, spec.lean, spec.user_settings) == ("5m", True, False)
    assert CACHE_TTLS == ("5m", "1h", "cli")


def test_the_cache_keys_load_from_the_pack(tmp_cfg):
    spec = load_config(tmp_cfg({"agents": {"claude": {"cache_ttl": "1h", "lean": False, "user_settings": True}}})).agents["claude"]
    assert (spec.cache_ttl, spec.lean, spec.user_settings) == ("1h", False, True)
    with pytest.raises(ConfigError, match=r"agents\.claude\.lean"):
        load_config(tmp_cfg({"agents": {"claude": {"lean": "yes"}}}))


def test_an_unknown_cache_ttl_is_a_validate_error(tmp_cfg):
    found = claude_issue(tmp_cfg, {"agents": {"claude": {"cache_ttl": "10m"}}}, "agents.claude.cache_ttl")
    assert found is not None and found.level == "error" and "5m" in found.message
    assert claude_issue(tmp_cfg, {"agents": {"claude": {"cache_ttl": "1h"}}}, "agents.claude.cache_ttl") is None


@pytest.mark.parametrize("key,value", [("cache_ttl", "1h"), ("lean", False), ("user_settings", True)])
def test_the_cache_keys_on_another_agent_are_a_warning(tmp_cfg, key, value):
    found = claude_issue(tmp_cfg, {"agents": {"codex": {key: value}}}, f"agents.codex.{key}")
    assert found is not None and found.level == "warning" and "only the claude agent" in found.message


@pytest.mark.parametrize("args", [["--setting-sources", "user,project"], ["--setting-sources=user"]])
def test_setting_sources_in_base_args_is_a_validate_error(tmp_cfg, args):
    found = claude_issue(tmp_cfg, {"agents": {"claude": {"base_args": ["-p", *args]}}}, "agents.claude.base_args")
    assert found is not None and found.level == "error" and "agents.claude.user_settings" in found.message
    assert claude_issue(tmp_cfg, {"agents": {"claude": {"base_args": ["-p"]}}}, "agents.claude.base_args") is None


def test_a_project_mcp_file_under_lean_is_a_warning(tmp_cfg):
    def write(root: Path) -> None:
        (root / ".mcp.json").write_text("{}", encoding="utf-8")

    assert claude_issue(tmp_cfg, None, "agents.claude.lean") is None  # no `.mcp.json` yet
    found = claude_issue(tmp_cfg, None, "agents.claude.lean", write)
    assert found is not None and found.level == "warning" and "--mcp-config" in found.message
    assert claude_issue(tmp_cfg, {"agents": {"claude": {"lean": False}}}, "agents.claude.lean", write) is None


def test_a_review_agent_outside_the_project_is_a_warning_without_user_settings(tmp_cfg):
    patch = {"overlays": {"security": {"review_agent": "auditor"}}}
    found = claude_issue(tmp_cfg, patch, "overlays.security.review_agent")
    assert found is not None and found.level == "warning" and "loads only the project's agents" in found.message

    def add(root: Path) -> None:
        (root / ".claude" / "agents").mkdir(parents=True, exist_ok=True)
        (root / ".claude" / "agents" / "auditor.md").write_text("x", encoding="utf-8")

    assert claude_issue(tmp_cfg, patch, "overlays.security.review_agent", add) is None
    both = {**patch, "agents": {"claude": {"user_settings": True}}}
    assert claude_issue(tmp_cfg, both, "overlays.security.review_agent") is None


# --------------------------------------------------------------------------- cost C2: the run brief keys


def test_cost_c2_keys_default(tmp_cfg):
    cfg = load_config(tmp_cfg())
    assert cfg.context.run_brief is True
    assert cfg.context.resume_gate_lines == 80
    assert cfg.project.agent_rules == ("CLAUDE.md",)
    cfg = load_config(tmp_cfg({"context": {"run_brief": False, "resume_gate_lines": 30},
                               "project": {"agent_rules": ["AGENTS.md", "docs/rules.md"]}}))
    assert (cfg.context.run_brief, cfg.context.resume_gate_lines) == (False, 30)
    assert cfg.project.agent_rules == ("AGENTS.md", "docs/rules.md")


def gate_lines(issues, level):
    return [i for i in issues if i.level == level and i.path == "context.resume_gate_lines"]


def test_resume_gate_lines_range(tmp_cfg):
    for bad in (9, 1001):
        assert len(gate_lines(issues_for(tmp_cfg, {"context": {"resume_gate_lines": bad}}), "error")) == 1
    for good in (10, 1000):
        issues = issues_for(tmp_cfg, {"context": {"resume_gate_lines": good, "gate_output_lines": 1000}})
        assert not gate_lines(issues, "error") and not gate_lines(issues, "warning")
    warned = gate_lines(issues_for(tmp_cfg, {"context": {"resume_gate_lines": 300, "gate_output_lines": 200}}), "warning")
    assert len(warned) == 1 and "tail" in warned[0].message


def agent_rules_issues(issues, level="error"):
    return [i for i in issues if i.level == level and i.path == "project.agent_rules"]


@pytest.mark.parametrize("entry", ["/etc/x", "../x", "docs/../x", "./.agentflow/r.md", ".AGENTFLOW/r.md", ".git/config", "-x", ""])
def test_agent_rules_entries_checked(tmp_cfg, entry):
    assert len(agent_rules_issues(issues_for(tmp_cfg, {"project": {"agent_rules": ["CLAUDE.md", entry]}}))) == 1


def test_agent_rules_must_be_a_list(tmp_cfg):
    assert len(agent_rules_issues(issues_for(tmp_cfg, {"project": {"agent_rules": "CLAUDE.md"}}))) == 1
    assert not agent_rules_issues(issues_for(tmp_cfg, {"project": {"agent_rules": ["CLAUDE.md", "docs/rules.md"]}}))


def test_rules_path_problem():
    from agentflow.config import rules_path_problem

    assert rules_path_problem("CLAUDE.md") is None and rules_path_problem("docs/a b.md") is None
    for bad in ("", "  ", 3, None, "/x", "a/../b", "-x", ".agentflow/x", ".Git/x", "./.GIT"):
        assert rules_path_problem(bad), bad


def test_memory_blockers():
    from agentflow.config import memory_blockers

    def blockers(text, paths=("CLAUDE.md",), rules=("CLAUDE.md",)):
        return memory_blockers(list(paths), lambda p: text if p in rules else None, rules)

    assert blockers("@AGENTS.md\nrules") == ["CLAUDE.md: @ import"]
    assert blockers("rules\nsee @docs/x.md here") == ["CLAUDE.md: @ import"]
    assert blockers("read @README") == ["CLAUDE.md: @ import"]
    assert blockers("```py\n@dataclass\nclass A: ...\n```\nplain") == []
    assert blockers("~~~\n@x/y\n~~~\n") == []
    assert blockers("use `@x/y` and mail me@example.com") == []
    assert blockers("```\n@a\n```\n@b") == ["CLAUDE.md: @ import"]
    assert blockers("plain", paths=("CLAUDE.md",), rules=("AGENTS.md",)) == ["CLAUDE.md is not in agent_rules"]
    assert blockers("plain", paths=("AGENTS.md",), rules=("AGENTS.md",)) == []
    assert blockers("x", paths=("CLAUDE.md", ".claude/CLAUDE.md")) == [".claude/CLAUDE.md"]
    assert blockers("x", paths=("CLAUDE.md", ".claude/rules/a.md")) == [".claude/rules/"]
    assert blockers("x", paths=("CLAUDE.md", "pkg/CLAUDE.md")) == ["pkg/CLAUDE.md"]
    assert blockers(None) == []  # an unreadable rules file has no import to find


def test_memory_blockers_fold_case_and_see_local_memory():
    from agentflow.config import memory_blockers

    def blockers(paths, rules=("CLAUDE.md",)):
        return memory_blockers(list(paths), lambda p: "x", rules)

    assert blockers(("CLAUDE.md", "CLAUDE.local.md")) == ["CLAUDE.local.md"]
    assert blockers(("CLAUDE.md", "docs/CLAUDE.local.md")) == ["docs/CLAUDE.local.md"]
    assert blockers(("CLAUDE.md", "Claude.Local.MD")) == ["Claude.Local.MD"]
    assert blockers(("CLAUDE.md", ".CLAUDE/claude.md")) == [".CLAUDE/claude.md"]
    assert blockers(("CLAUDE.md", ".Claude/RULES/a.md")) == [".claude/rules/"]
    assert blockers(("CLAUDE.md", "pkg/Claude.md")) == ["pkg/Claude.md"]
    assert blockers(("claude.md",)) == ["claude.md is not in agent_rules"]  # a case-insensitive disk loads it
    assert blockers(("claude.md",), rules=("claude.md",)) == []
    assert blockers(("CLAUDE.md", "docs/claude.mdx", "CLAUDE.md.bak")) == []


def git_repo(root: Path, files: dict[str, str | bytes]) -> None:
    import subprocess

    def git(*args):
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=root, check=True, capture_output=True)

    git("init", "-q")
    for name, content in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content if isinstance(content, bytes) else content.encode())
    git("add", "-A")
    git("commit", "-q", "-m", "c")


def test_tree_paths(tmp_path):
    from agentflow import gitops

    git_repo(tmp_path, {"CLAUDE.md": "x", "a/b.md": "y"})
    assert gitops.tree_paths(tmp_path, "HEAD") == ["CLAUDE.md", "a/b.md"]
    assert gitops.tree_paths(tmp_path, "nosuchref") is None
    assert gitops.tree_paths(tmp_path / "missing", "HEAD") is None


@pytest.mark.parametrize("files, rules, run_brief, expected", [
    ({"README.md": "x"}, ["AGENTS.md"], True, 1),
    ({"README.md": "x"}, ["CLAUDE.md"], True, 0),
    ({"CLAUDE.md": "x", "pkg/CLAUDE.md": "y"}, ["CLAUDE.md"], True, 1),
    ({"CLAUDE.md": "x" * 200_001}, ["CLAUDE.md"], True, 1),
    ({"CLAUDE.md": "x"}, ["CLAUDE.md"], True, 0),
    ({"README.md": "x"}, ["AGENTS.md"], False, 0),
    ({"CLAUDE.md": "x", "pkg/CLAUDE.md": "y"}, ["CLAUDE.md"], False, 0),
    ({"CLAUDE.md": "x" * 200_001}, ["CLAUDE.md"], False, 0),
])
def test_agent_rules_warnings(tmp_cfg, tmp_path, files, rules, run_brief, expected):
    git_repo(tmp_path, files)
    issues = issues_for(tmp_cfg, {"context": {"run_brief": run_brief}, "project": {"agent_rules": rules}})
    found = agent_rules_issues(issues, "warning")
    assert len(found) == expected, found
    for issue in found:  # a listed file that is not committed is left out, and the brief still carries the rest
        missing = "is not committed at HEAD" in issue.message
        assert ("the brief leaves it out" in issue.message) is missing, issue.message
        assert ("memory files" in issue.message) is not missing, issue.message


def test_agent_rules_unreadable_file_warning_says_why(tmp_cfg, tmp_path):
    git_repo(tmp_path, {"CLAUDE.md": "x" * 200_001})
    (found,) = agent_rules_issues(issues_for(tmp_cfg, {"project": {"agent_rules": ["CLAUDE.md"]}}), "warning")
    assert found.message.startswith("CLAUDE.md is not a regular file of at most 200000 bytes at HEAD")
    assert "failed verification" in found.message and "memory files" in found.message


def _fan_out_head(root: Path, width: int, depth: int) -> None:
    """Make HEAD a commit whose tree names `width ** depth` files through one shared subtree (a few KB of objects)."""
    import subprocess

    def git(*args, feed=None):
        return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=root, input=feed,
                              capture_output=True, text=True, check=True).stdout.strip()

    blob = git("hash-object", "-w", "--stdin", feed="x\n")
    tree = git("mktree", feed="".join(f"100644 blob {blob}\tf{i}\n" for i in range(width)))
    for _ in range(depth - 1):
        tree = git("mktree", feed="".join(f"040000 tree {tree}\td{i}\n" for i in range(width)))
    git("update-ref", "HEAD", git("commit-tree", tree, "-p", "HEAD", "-m", "bomb"))


def test_agent_rules_check_is_bounded_on_a_tree_bomb(tmp_cfg, tmp_path):
    import time

    git_repo(tmp_path, {"CLAUDE.md": "x"})
    _fan_out_head(tmp_path, 20, 4)  # 160_000 paths: past the listing's bounds
    started = time.monotonic()
    found = agent_rules_issues(issues_for(tmp_cfg, {"project": {"agent_rules": ["CLAUDE.md"]}}), "warning")
    assert time.monotonic() - started < 20
    assert [i.message for i in found] == [
        "could not list the tree at HEAD within limits (or verification failed): run sessions will let Claude load "
        "its own memory files instead"]
    off = issues_for(tmp_cfg, {"context": {"run_brief": False}, "project": {"agent_rules": ["CLAUDE.md"]}})
    assert agent_rules_issues(off, "warning") == []


@pytest.mark.parametrize("files, blocker", [
    ({"CLAUDE.md": "x", "CLAUDE.local.md": "mine"}, "CLAUDE.local.md"),
    ({"CLAUDE.md": "x", "pkg/claude.local.md": "mine"}, "pkg/claude.local.md"),
    ({"CLAUDE.md": "x", ".claude/claude.md": "y"}, ".claude/claude.md"),
    ({"CLAUDE.md": "x", ".claude/Rules/a.md": "y"}, ".claude/rules/"),
    ({"CLAUDE.md": "x", "pkg/claude.md": "y"}, "pkg/claude.md"),
    ({"claude.md": "x"}, "claude.md is not in agent_rules"),
])
def test_agent_rules_warns_on_case_variants_and_local_memory(tmp_cfg, tmp_path, files, blocker):
    git_repo(tmp_path, files)
    found = agent_rules_issues(issues_for(tmp_cfg, {"project": {"agent_rules": ["CLAUDE.md"]}}), "warning")
    assert [i.message.split(": ")[0] for i in found] == [blocker], found


def test_agent_rules_symlink_to_a_refused_target_warns(tmp_cfg, tmp_path):
    import subprocess

    (tmp_path / "x").mkdir()
    (tmp_path / "AGENTS.md").symlink_to("../outside/.git/config")
    (tmp_path / "CLAUDE.md").write_text("x", encoding="utf-8")
    for args in (["init", "-q"], ["add", "-A"], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "c"]):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)
    issues = issues_for(tmp_cfg, {"project": {"agent_rules": ["CLAUDE.md", "AGENTS.md"]}})
    assert len(agent_rules_issues(issues, "warning")) == 1


def test_agent_rules_symlink_target_is_read_like_the_brief_reads_it(tmp_cfg, tmp_path):
    import subprocess

    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "agents.md").write_text("see @README\n", encoding="utf-8")
    (tmp_path / "CLAUDE.md").symlink_to("docs/agents.md")
    for args in (["init", "-q"], ["add", "-A"], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "c"]):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)
    found = agent_rules_issues(issues_for(tmp_cfg, {"project": {"agent_rules": ["CLAUDE.md"]}}), "warning")
    assert [i.message.split(":")[0] for i in found] == ["CLAUDE.md"] and "@ import" in found[0].message


def test_memory_blockers_scan_is_linear_on_hostile_text():
    import time

    from agentflow.config import memory_blockers

    def blockers(text):
        return memory_blockers(["CLAUDE.md"], lambda p: text, ["CLAUDE.md"])

    for hostile in (" " * 4 + "`" * 20_000, "x " + "`" * 20_000 + " @x", "".join("`" * n + " " for n in range(1, 600)),
                    "` " * 50_000):
        started = time.monotonic()
        blockers(hostile)
        assert time.monotonic() - started < 1.0
    assert blockers(" " * 4 + "`" * 20_000) == []
    assert blockers("x " + "`" * 20_000 + " @x") == ["CLAUDE.md: @ import"]
    assert blockers("```py\n@dataclass\n```\n`@x/y` and ``a ` @b`` text") == []
    assert blockers("a `b` ` c @README") == ["CLAUDE.md: @ import"]
    assert blockers("read @README") == ["CLAUDE.md: @ import"]


# --------------------------------------------------------------------------- C3: read-only roles and their idle limit


def _sparse_issues(sparse_repo, **agents_claude_and_more):
    """Validate the sparse pack (the engine's default phases) with `agents.claude` and top-level keys patched."""
    target = pack.pack_config(sparse_repo)
    data = yaml.safe_load(target.read_text(encoding="utf-8"))
    data["agents"]["claude"].update(agents_claude_and_more.pop("claude", {}))
    data.update(agents_claude_and_more)
    target.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return validate(load_config(target), sparse_repo)


def test_cost_c3_keys_default(sparse_repo):
    cfg = load_config(pack.pack_config(sparse_repo))
    assert cfg.agents["claude"].read_only_roles == ("review",)
    assert cfg.agents["codex"].read_only_roles == ()
    assert cfg.watchdog.read_only_idle_min == 5.0


def test_read_only_roles_must_be_known_roles(sparse_repo):
    found = _sparse_issues(sparse_repo, claude={"read_only_roles": ["reviewer"]})
    assert [(i.path) for i in found if i.level == "error"] == ["agents.claude.read_only_roles"]
    assert not errors(_sparse_issues(sparse_repo, claude={"read_only_roles": ["review"]}))
    target = pack.pack_config(sparse_repo)
    target.write_text(yaml.safe_dump({**SPARSE_PACK, "agents": {"claude": {"read_only_roles": "review"}}}), encoding="utf-8")
    with pytest.raises(ConfigError, match=r"read_only_roles"):
        load_config(target)


def test_a_read_only_role_on_a_step_that_edits_is_an_error(sparse_repo):
    cfg = load_config(pack.pack_config(sparse_repo))
    build = {s.id: s for s in cfg.phases["build"].steps}["build"]
    found = errors(_sparse_issues(sparse_repo, claude={"read_only_roles": ["review", "build"]}))
    assert ("error", build.path) in found
    assert ("error", build.review.path) not in found  # a review step only reads
    assert not [p for _, p in found if p.startswith("phases.define.steps") and p.endswith("spec_review")]
    found = errors(_sparse_issues(sparse_repo, claude={"read_only_roles": ["plan"]}))
    steps = {s.id: s.path for s in cfg.phases["plan"].steps}
    assert ("error", steps["plan_reconcile"]) in found and ("error", steps["plan"]) in found


def test_read_only_flags_in_base_args_are_errors(sparse_repo):
    for arg in ("--tools", "--settings={}"):
        found = errors(_sparse_issues(sparse_repo, claude={"base_args": ["-p", arg, *(["Edit"] if arg == "--tools" else [])]}))
        assert found == [("error", "agents.claude.base_args")], arg
    for arg in ("--tools", "--settings={}"):
        assert not errors(_sparse_issues(sparse_repo, claude={"read_only_roles": [], "base_args": ["-p", arg]})), arg
    found = errors(_sparse_issues(sparse_repo, claude={"read_only_roles": ["review"], "base_args": ["-p", "--agent", "x"]}))
    assert found == [("error", "agents.claude.base_args")]  # a profile's own mode, tools and hooks could widen it


@pytest.mark.parametrize("arg,why", [("--tools", "read_only_roles"), ("--settings={}", "read_only_roles"),
                                     ("--agent=x", "read_only_roles"), ("--permission-mode", "settings"),
                                     ("--dangerously-skip-permissions", "settings"),
                                     ("--allowedTools=Bash", "settings"), ("--setting-sources", "user_settings")])
def test_runner_flags_in_the_command_are_errors(sparse_repo, arg, why):
    """`command` past the program reaches every argv, as `base_args` does: the same flags are refused there."""
    found = [i for i in _sparse_issues(sparse_repo, claude={"command": ["claude", arg]}) if i.level == "error"]
    assert [i.path for i in found] == ["agents.claude.command"] and why in found[0].message
    assert not errors(_sparse_issues(sparse_repo, claude={"command": ["claude", "--verbose"]}))


def test_read_only_roles_on_codex_warns(sparse_repo):
    target = pack.pack_config(sparse_repo)
    data = yaml.safe_load(target.read_text(encoding="utf-8"))
    data["agents"]["codex"] = {"read_only_roles": ["review"]}
    target.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    found = validate(load_config(target), sparse_repo)
    assert [(i.level, i.path) for i in found if i.path == "agents.codex.read_only_roles"] == [
        ("warning", "agents.codex.read_only_roles")]
    assert not [i for i in found if i.level == "error"]


def test_read_only_idle_min_checks(sparse_repo):
    for bad in (-1, float("inf")):
        found = _sparse_issues(sparse_repo, watchdog={"read_only_idle_min": bad})
        assert [i.path for i in found if i.level == "error"] == ["watchdog.read_only_idle_min"], bad
    found = _sparse_issues(sparse_repo, watchdog={"no_progress_min": 15, "read_only_idle_min": 20})
    assert [(i.level, i.path) for i in found if i.path == "watchdog.read_only_idle_min"] == [
        ("warning", "watchdog.read_only_idle_min")]
    for fine in ({"no_progress_min": 15, "read_only_idle_min": 0}, {"no_progress_min": 0, "read_only_idle_min": 20}):
        found = _sparse_issues(sparse_repo, watchdog=fine)
        assert not [i for i in found if i.path == "watchdog.read_only_idle_min"], fine


# --------------------------------------------------------------------------- C4: per-role turn and spend caps

C4_TURNS = {"review": 45, "plan": 60, "build": 60, "debug": 60, "mechanical": 15}
C4_BUDGET = {"review": 4.0, "plan": 10.0, "build": 10.0, "debug": 10.0}


def _c4_issues(tmp_cfg, patch, prefix="agents."):
    cfg_path = tmp_cfg(patch)
    return [i for i in validate(load_config(cfg_path), cfg_path.parent.parent) if i.path.startswith(prefix)]


def test_cost_c4_keys_default(sparse_repo):
    spec = load_config(pack.pack_config(sparse_repo)).agents["claude"]  # the engine's defaults carry the workflow
    assert spec.max_turns_by_role == C4_TURNS
    assert spec.session_budget_usd == C4_BUDGET
    assert spec.bash_output_chars == 15000
    codex = load_config(pack.pack_config(sparse_repo)).agents["codex"]
    assert (codex.max_turns_by_role, codex.session_budget_usd, codex.bash_output_chars) == ({}, {}, 0)
    assert DEFAULTS["autonomy"]["resolve_kinds"]["turn_cap"] == 1 and DEFAULTS["autonomy"]["resolve_kinds"]["budget_cap"] == 1
    packed = yaml.safe_load(PYTHON_CONFIG.read_text(encoding="utf-8"))["autonomy"]["resolve_kinds"]
    assert packed["turn_cap"] == 1 and packed["budget_cap"] == 1


def test_cap_maps_merge_and_switch_off(tmp_cfg):
    def agents(patch):
        return load_config(tmp_cfg(patch)).agents["claude"]

    spec = agents({"agents": {"claude": {"max_turns_by_role": {"build": 0}}}})
    assert spec.max_turns_by_role == {**C4_TURNS, "build": 0}
    def stated(key, value):  # a pack layer that states `null` (a dict patch would drop the key instead)
        def patch(data):
            data["agents"]["claude"][key] = value

        return patch

    assert agents(stated("session_budget_usd", None)).session_budget_usd == {}
    spec = agents(stated("session_budget_usd", {"review": None}))
    assert spec.session_budget_usd == {k: v for k, v in C4_BUDGET.items() if k != "review"}
    assert agents({"agents": {"claude": {"session_budget_usd": {}}}}).session_budget_usd == C4_BUDGET  # `{}` changes nothing
    alone = load_config(tmp_cfg(stated("session_budget_usd", {"review": None, "build": 3})), layered=False)
    assert alone.agents["claude"].session_budget_usd == {"build": 3.0}  # a null role is dropped at parse time


def test_resolve_turn_cap_precedence(tmp_cfg):
    from agentflow.config import resolve_turn_cap, session_budget

    cfg = load_config(tmp_cfg())
    assert cfg.agents["claude"].max_turns == 80
    step = phase_step_of(cfg, "build", "build")
    assert step.max_turns is None
    assert resolve_turn_cap(cfg, "claude", dataclasses.replace(step, max_turns=20), "build") == 20  # the step wins
    assert resolve_turn_cap(cfg, "claude", step, "build") == 60  # the role beats `max_turns: 80`
    assert resolve_turn_cap(cfg, "claude", step, "design") == 80  # no entry: `max_turns`
    off = load_config(tmp_cfg({"agents": {"claude": {"max_turns_by_role": {"build": 0}}}}))
    assert resolve_turn_cap(off, "claude", step, "build") == 0  # no cap, and no fallback to `max_turns`
    assert resolve_turn_cap(off, "claude", dataclasses.replace(step, max_turns=20), "build") == 20
    assert session_budget(cfg, "claude", "build") == 10.0
    assert session_budget(cfg, "claude", "design") is None
    assert session_budget(off, "claude", "build") == 10.0  # the two maps are independent
    zero = load_config(tmp_cfg({"agents": {"claude": {"session_budget_usd": {"build": 0}}}}))
    assert session_budget(zero, "claude", "build") is None


def test_cap_map_checks(tmp_cfg):
    for key, bad in (("max_turns_by_role", {"reviewer": 10}), ("max_turns_by_role", {"build": -1}),
                     ("max_turns_by_role", {"build": 1.5}),
                     ("session_budget_usd", {"build": float("inf")}), ("session_budget_usd", {"build": 0.001}),
                     ("session_budget_usd", {"build": 5000}), ("session_budget_usd", {"nobody": 1})):
        found = [(i.level, i.path) for i in _c4_issues(tmp_cfg, {"agents": {"claude": {key: bad}}}) if i.level == "error"]
        assert found == [("error", f"agents.claude.{key}")], (key, bad)
    for key in ("max_turns_by_role", "session_budget_usd"):  # a role the engine's caps don't name: a typo, most likely
        msg = next(i.message for i in _c4_issues(tmp_cfg, {"agents": {"claude": {key: {"nobody": 1}}}}) if i.level == "error")
        assert "agents.claude.models" in msg and "check the role's name" in msg and ": null" not in msg
    ok = {"agents": {"claude": {"max_turns_by_role": {"build": 0, "review": 7}, "session_budget_usd": {"build": 0, "plan": 0.01}}}}
    assert not [i for i in _c4_issues(tmp_cfg, ok) if i.level == "error"]
    for key in ("max_turns_by_role", "session_budget_usd"):
        with pytest.raises(ConfigError, match=key):
            load_config(tmp_cfg({"agents": {"claude": {key: {"build": "many"}}}}))
        with pytest.raises(ConfigError, match=key):
            load_config(tmp_cfg({"agents": {"claude": {key: ["build"]}}}))
        with pytest.raises(ConfigError, match=f"{key}.build"):  # YAML's `true` is no number of turns or dollars
            load_config(tmp_cfg({"agents": {"claude": {key: {"build": True}}}}))
    with pytest.raises(ConfigError, match="session_budget_usd.build"):  # too large for a float: refused, not a crash
        load_config(tmp_cfg({"agents": {"claude": {"session_budget_usd": {"build": 10 ** 400}}}}))


def test_a_pack_that_drops_a_model_role_must_null_its_caps(tmp_cfg):
    def dropping(caps):
        def patch(data):
            data["agents"]["claude"]["models"] = {"mechanical": None}
            data["agents"]["claude"].update(caps)

        return patch

    bare = [i for i in _c4_issues(tmp_cfg, dropping({})) if i.level == "error"]
    assert [i.path for i in bare] == ["agents.claude.max_turns_by_role"] and "max_turns_by_role.mechanical: null" in bare[0].message
    both = {"max_turns_by_role": {"mechanical": None}, "session_budget_usd": {"mechanical": None}}
    assert not [i for i in _c4_issues(tmp_cfg, dropping(both)) if i.level == "error"]


def test_bash_output_chars_checks(tmp_cfg):
    for bad in (500, 2_000_000, 999):
        found = [(i.level, i.path) for i in _c4_issues(tmp_cfg, {"agents": {"claude": {"bash_output_chars": bad}}})
                 if i.level == "error"]
        assert found == [("error", "agents.claude.bash_output_chars")], bad
    for good in (0, 15000, 1000, 1_000_000):
        assert not [i for i in _c4_issues(tmp_cfg, {"agents": {"claude": {"bash_output_chars": good}}}) if i.level == "error"], good
    for bad in ("lots", True, 15000.5):  # never reaches `BASH_MAX_OUTPUT_LENGTH`: refused when the config loads
        with pytest.raises(ConfigError, match="bash_output_chars"):
            load_config(tmp_cfg({"agents": {"claude": {"bash_output_chars": bad}}}))


def test_cap_keys_on_codex_warn(tmp_cfg):
    patch = {"agents": {"codex": {"max_turns_by_role": {"build": 5}, "session_budget_usd": {"build": 5}, "bash_output_chars": 2000}}}
    found = [(i.level, i.path, i.message) for i in _c4_issues(tmp_cfg, patch, "agents.codex.")]
    assert [(lv, p) for lv, p, _ in found] == [("warning", "agents.codex.max_turns_by_role"),
                                               ("warning", "agents.codex.session_budget_usd"),
                                               ("warning", "agents.codex.bash_output_chars")]
    assert all("only the claude agent's calls are capped" in m and "no effect on `codex`" in m for _, _, m in found)
    assert not [i for i in _c4_issues(tmp_cfg, None) if i.level == "warning"]


def test_step_max_turns_checks(tmp_cfg):
    def on_step(value):
        def patch(data):
            phase_step(data, "build", "build")["max_turns"] = value

        return patch

    for bad in (0, "x", True, 1.5, -3):  # refused when the config loads: a run start never calls `validate`
        with pytest.raises(ConfigError, match=r"phases\.build.*max_turns"):
            load_config(tmp_cfg(on_step(bad)))
    assert not [i for i in _c4_issues(tmp_cfg, on_step(20), "phases.") if i.level == "error"]
    assert phase_step_of(load_config(tmp_cfg(on_step(20))), "build", "build").max_turns == 20

    def on_retro(data):
        data.setdefault("retro_step", {"id": "retro", "skill": "agentflow-retro", "role": "review", "prompt": "retro.md"})
        data["retro_step"]["max_turns"] = 20

    cfg = load_config(tmp_cfg(on_retro))
    assert cfg.retro_step.max_turns == 20
    assert not [i for i in _c4_issues(tmp_cfg, on_retro, "retro_step") if i.level == "error"]

    def bad_retro(data):
        on_retro(data)
        data["retro_step"]["max_turns"] = 0

    with pytest.raises(ConfigError, match=r"retro_step\.max_turns"):
        load_config(tmp_cfg(bad_retro))


def _on_build_step(key, value):
    def patch(data):
        phase_step(data, "build", "build")[key] = value

    return patch


def test_carry_limits_default_off(tmp_cfg):
    from agentflow.config import carry_limits

    assert carry_limits(phase_step_of(load_config(tmp_cfg(None)), "build", "build")) == (1, 40)
    for carry, limits in (({"max_tasks": 4}, (4, 40)), ({"max_tasks": 4, "max_context_pct": 30}, (4, 30)),
                          ({"max_context_pct": 30}, (1, 30))):
        step = phase_step_of(load_config(tmp_cfg(_on_build_step("carry", carry))), "build", "build")
        assert step.carry == carry and carry_limits(step) == limits


@pytest.mark.parametrize("carry, message", [
    ("yes", "carry must be a mapping"),
    ({"max_tasks": 0}, "max_tasks must be a whole number of at least 1"),
    ({"max_tasks": True}, "max_tasks must be a whole number of at least 1"),
    ({"max_tasks": 2.5}, "max_tasks must be a whole number of at least 1"),
    ({"max_context_pct": 9}, "max_context_pct must be a number from 10 to 70"),
    ({"max_context_pct": 71}, "max_context_pct must be a number from 10 to 70"),
    ({"max_context_pct": "40"}, "max_context_pct must be a number from 10 to 70"),
    ({"max_context_pct": False}, "max_context_pct must be a number from 10 to 70"),
    ({"max_task": 4}, "carry takes only max_tasks and max_context_pct"),
])
def test_a_bad_carry_is_refused_when_the_config_loads(tmp_cfg, carry, message):
    with pytest.raises(ConfigError, match=r"phases\.build.*carry") as raised:  # a run start never calls `validate`
        load_config(tmp_cfg(_on_build_step("carry", carry)))
    assert message in str(raised.value)


def test_carry_off_a_plan_task_step_is_refused(tmp_cfg):
    def on_verify(data):
        step = next(s for s in data["phases"]["verify"]["steps"] if "foreach" not in s)
        step["carry"] = {"max_tasks": 4}

    with pytest.raises(ConfigError, match="only a `foreach: plan_task` step has `carry`"):
        load_config(tmp_cfg(on_verify))


def test_carry_on_the_build_step_validates_clean(tmp_cfg):
    patch = _on_build_step("carry", {"max_tasks": 4, "max_context_pct": 40})
    assert issues_for(tmp_cfg, patch) == issues_for(tmp_cfg, None)


@pytest.mark.parametrize("key,arg,owner", [("base_args", ["-p", "--max-budget-usd", "5"], "session_budget_usd"),
                                           ("command", ["claude", "--max-turns=9"], "max_turns_by_role"),
                                           ("effort_arg", ["--max-turns", "9", "--effort", "{effort}"], "max_turns_by_role"),
                                           ("effort_arg", ["--setting-sources", "user", "{effort}"], "user_settings")])
def test_runner_flags_name_their_key(tmp_cfg, key, arg, owner):
    found = [i for i in _c4_issues(tmp_cfg, {"agents": {"claude": {key: arg}}}) if i.level == "error"]
    assert [i.path for i in found] == [f"agents.claude.{key}"]
    assert f"agents.claude.{owner}" in found[0].message and "is not allowed" in found[0].message


def test_park_kinds_may_not_name_a_stop_a_task_cannot_park(tmp_cfg):
    issues = issues_for(tmp_cfg, {"hitl": {"park_kinds": ["blocked", "budget_exceeded"]}})
    bad = [i for i in issues if i.level == "error" and i.path == "hitl.park_kinds"]
    assert bad and "not a stop a task can park" in bad[0].message
    for kind in ("environment", "phase_approval", "taste"):
        assert ("error", "hitl.park_kinds") in errors(issues_for(tmp_cfg, {"hitl": {"park_kinds": [kind]}}))


@pytest.mark.parametrize("value", ["blocked", {"blocked": True}, [1], [["blocked"]]])
def test_park_kinds_must_be_a_list_of_stop_kinds(tmp_cfg, value):
    assert ("error", "hitl.park_kinds") in errors(issues_for(tmp_cfg, {"hitl": {"park_kinds": value}}))


@pytest.mark.parametrize("value", [[], ["blocked", "gate_failed"]])
def test_park_kinds_may_be_empty_or_name_parkable_stops(tmp_cfg, value):
    assert not [path for _, path in errors(issues_for(tmp_cfg, {"hitl": {"park_kinds": value}})) if path.startswith("hitl")]


@pytest.mark.parametrize("value", ["yes", 1, ["true"]])
def test_park_independent_must_be_a_bool(tmp_cfg, value):
    assert ("error", "hitl.park_independent") in errors(issues_for(tmp_cfg, {"hitl": {"park_independent": value}}))


@pytest.mark.parametrize("value", [True, False])
def test_park_independent_takes_a_bool(tmp_cfg, value):
    assert not [path for _, path in errors(issues_for(tmp_cfg, {"hitl": {"park_independent": value}})) if path.startswith("hitl")]


def test_constraint_override_loads_into_the_guardrails(tmp_cfg):
    cfg = load_config(tmp_cfg({"guardrails": {"constraint_override": {"trailer": "Constraints-Approved-By",
                                                                      "gates": ["floor"]}}}))
    assert cfg.guardrails.constraint_trailer == "Constraints-Approved-By"
    assert cfg.guardrails.constraint_gates == ["floor"]


def test_constraint_override_is_unset_by_default(tmp_cfg):
    cfg = load_config(tmp_cfg())
    assert cfg.guardrails.constraint_trailer == "" and cfg.guardrails.constraint_gates == []


@pytest.mark.parametrize("override", ["Constraints-Approved-By", {"trailer": 3, "gates": ["floor"]},
                                      {"trailer": "Constraints-Approved-By", "gates": "floor"}])
def test_a_malformed_constraint_override_does_not_load(tmp_cfg, override):
    with pytest.raises(ConfigError, match="constraint_override"):
        load_config(tmp_cfg({"guardrails": {"constraint_override": override}}))


@pytest.mark.parametrize("value", ["yes", 1, ["true"]])
def test_digest_must_be_a_bool(tmp_cfg, value):
    assert ("error", "hitl.digest") in errors(issues_for(tmp_cfg, {"hitl": {"digest": value}}))


@pytest.mark.parametrize("value", [True, False])
def test_digest_may_be_on_or_off(tmp_cfg, value):
    assert not [path for _, path in errors(issues_for(tmp_cfg, {"hitl": {"digest": value}})) if path.startswith("hitl")]


# --------------------------------------------------------------------------- parallel build


def parallel_issues(tmp_cfg, parallel, patch=None) -> list[Issue]:
    def apply(data: dict) -> None:
        data["parallel"] = parallel
        if patch:
            patch(data)
    return issues_for(tmp_cfg, apply)


def test_parallel_is_off_by_default(sparse_repo):
    cfg = load_config(pack.pack_config(sparse_repo))
    assert cfg.parallel.max_subagents == 1 and cfg.parallel.serial_paths == []
    assert cfg.skills_map["build_wave"] == "agentflow-build"
    assert cfg.route.profiles["L"].wave_review is False and cfg.route.profiles["M"].wave_review is True
    assert [i for i in validate(cfg, sparse_repo) if "parallel" in i.path or "wave" in i.path] == []


@pytest.mark.parametrize("parallel, path, text", [
    ({"max_subagents": 7}, "parallel.max_subagents", "at most 6"),
    ({"max_subagents": 0}, "parallel.max_subagents", "from 1 (off)"),
    ({"max_subagents": True}, "parallel.max_subagents", "whole number"),
    ({"max_subagents": "3"}, "parallel.max_subagents", "whole number"),
    ({"serial_paths": "x"}, "parallel.serial_paths", "list of non-empty"),
    ({"serial_paths": [""]}, "parallel.serial_paths", "list of non-empty"),
    ({"foo": 1}, "parallel.foo", "max_subagents, serial_paths"),
])
def test_parallel_rejects_bad_values(tmp_cfg, parallel, path, text):
    found = [i for i in parallel_issues(tmp_cfg, parallel) if i.level == "error" and i.path == path]
    assert found, parallel_issues(tmp_cfg, parallel)
    assert text in found[0].message


@pytest.mark.parametrize("count", [1, 6])
def test_parallel_accepts_its_bounds(tmp_cfg, count):
    found = parallel_issues(tmp_cfg, {"max_subagents": count, "serial_paths": ["uv.lock"]})
    assert [i for i in found if "parallel" in i.path and i.level == "error"] == []


def test_a_bad_parallel_value_loads_as_off(tmp_cfg):
    path = tmp_cfg(lambda data: data.update(parallel={"max_subagents": "3", "serial_paths": "x"}))
    cfg = load_config(path)
    assert cfg.parallel.max_subagents == 1 and cfg.parallel.serial_paths == []


def slot_issues(tmp_cfg, patch) -> list[Issue]:
    return issues_for(tmp_cfg, patch if callable(patch) else (lambda data: data.update(patch)))


def test_slots_and_the_queue_are_off_by_default(sparse_repo):
    cfg = load_config(pack.pack_config(sparse_repo))
    assert (cfg.slots.count, cfg.queue.human_slot) == (1, 1)
    assert [i for i in validate(cfg, sparse_repo) if i.path.startswith(("slots", "queue"))] == []


@pytest.mark.parametrize("block, path, text", [
    ({"count": 0}, "slots.count", "at most 16"),
    ({"count": 17}, "slots.count", "at most 16"),
    ({"count": True}, "slots.count", "at most 16"),
    ({"count": "2"}, "slots.count", "at most 16"),
    ({"foo": 1}, "slots.foo", "count"),
])
def test_slots_rejects_bad_values(tmp_cfg, block, path, text):
    found = [i for i in slot_issues(tmp_cfg, {"slots": block}) if i.level == "error" and i.path == path]
    assert found, block
    assert text in found[0].message


@pytest.mark.parametrize("block, path, text", [
    ({"human_slot": -1}, "queue.human_slot", "whole number"),
    ({"human_slot": True}, "queue.human_slot", "whole number"),
    ({"human_slot": "1"}, "queue.human_slot", "whole number"),
    ({"foo": 1}, "queue.foo", "human_slot"),
])
def test_queue_rejects_bad_values(tmp_cfg, block, path, text):
    found = [i for i in slot_issues(tmp_cfg, {"queue": block}) if i.level == "error" and i.path == path]
    assert found, block
    assert text in found[0].message


@pytest.mark.parametrize("slots, queue", [({"count": 1}, {"human_slot": 0}), ({"count": 16}, {"human_slot": 1}),
                                          ({"count": 1}, {"human_slot": 99})])
def test_slots_and_the_queue_accept_their_bounds(tmp_cfg, slots, queue):
    found = slot_issues(tmp_cfg, {"slots": slots, "queue": queue})
    assert [i for i in found if i.path.startswith(("slots", "queue")) and i.level == "error"] == []


def test_a_bad_slots_value_loads_as_off(tmp_cfg):
    cfg = load_config(tmp_cfg(lambda data: data.update(slots={"count": "x"}, queue={"human_slot": "y"})))
    assert cfg.slots.count == 1 and cfg.queue.human_slot == 1
    assert load_config(tmp_cfg(lambda data: data.update(slots={"count": 3}, queue={"human_slot": 0}))).slots.count == 3


def test_slots_warn_when_nothing_uses_the_slot(tmp_cfg):
    def warned(patch) -> list[Issue]:
        return [i for i in slot_issues(tmp_cfg, patch) if i.level == "warning" and i.path == "slots.count"]

    found = warned({"slots": {"count": 2}})
    assert found and "uses {slot}" in found[0].message
    assert not warned({"slots": {"count": 1}})
    assert not warned({"slots": {"count": 2}, "defaults": {"env": {"PORT": "5543{slot}"}}})

    def in_run(data: dict) -> None:
        data["slots"] = {"count": 2}
        data["gates"]["floor"]["run"] = "pytest --db app-test-{slot}"
    assert not warned(in_run)

    def in_affected(data: dict) -> None:
        data["slots"] = {"count": 2}
        data["gates"]["floor"]["affected"] = {"run": "pytest -p app-{slot} {paths}"}
    assert not warned(in_affected)

    def doubled(data: dict) -> None:
        data["slots"] = {"count": 2}
        data["defaults"]["env"] = {"A": "${slot}", "B": "{{slot}}"}
    assert warned(doubled)  # a shell `${slot}` and a doubled `{{slot}}` are not the token


def test_slots_warn_when_the_queue_keeps_them_all_free(tmp_cfg):
    def warned(slots, queue) -> list[Issue]:
        found = slot_issues(tmp_cfg, {"slots": slots, "queue": queue, "defaults": {"env": {"P": "{slot}"}}})
        return [i for i in found if i.level == "warning" and i.path == "queue.human_slot"]

    found = warned({"count": 2}, {"human_slot": 2})
    assert found and "never starts a run" in found[0].message
    assert not warned({"count": 2}, {"human_slot": 1})
    assert not warned({"count": 1}, {"human_slot": 5})


def test_affected_allows_the_slot_placeholder_too(tmp_cfg):
    def affected(run):
        return {"gates": {"floor": {"run": "true", "affected": {"run": run, "paths": ["**/*.py"]}}}}

    assert ("error", "gates.floor.affected.run") not in errors(
        issues_for(tmp_cfg, affected("pytest -p app-{slot} {paths}")))
    found = [i for i in issues_for(tmp_cfg, affected("pytest {other} {paths}")) if i.level == "error"
             and i.path == "gates.floor.affected.run"]
    assert found and "`{paths}` and `{slot}`" in found[0].message


def test_a_wrong_wave_review_is_refused(tmp_cfg):
    found = route_issues(tmp_cfg, {"profiles": {"M": {"wave_review": "no"}}})
    assert ("error", "route.profiles.M.wave_review") in errors(found)
    assert errors(route_issues(tmp_cfg, {"profiles": {"M": {"wave_review": False}}})) == []


def test_waves_warn_when_the_build_phase_is_not_on_claude(tmp_cfg):
    def codex_build(data: dict) -> None:
        data["agents"]["codex"]["enabled"] = True
        data["phases"]["build"]["agent"] = "codex"
    found = parallel_issues(tmp_cfg, {"max_subagents": 3}, codex_build)
    assert [i.level for i in found if i.path == "parallel.max_subagents"] == ["warning"]
    assert any("waves need the claude agent" in i.message for i in found if i.path == "parallel.max_subagents")
    off = parallel_issues(tmp_cfg, {"max_subagents": 1}, codex_build)
    assert not [i for i in off if i.path == "parallel.max_subagents"]
    assert not [i for i in parallel_issues(tmp_cfg, {"max_subagents": 3}) if i.path == "parallel.max_subagents"]


def test_waves_warn_when_the_packs_plan_prompt_never_asks_for_a_group(tmp_cfg):
    path = tmp_cfg(lambda data: data.update(parallel={"max_subagents": 3}))
    repo = path.parent.parent
    warn = "plans written with this prompt never form a wave"
    assert not [i for i in validate(load_config(path), repo) if warn in i.message]  # no override: the engine's asks
    own = path.parent / "prompts" / "plan.md"
    own.write_text("Write the plan.\n", encoding="utf-8")
    found = [i for i in validate(load_config(path), repo) if warn in i.message]
    assert [(i.level, i.path) for i in found] == [("warning", "prompts/plan.md")]
    own.write_text("Write the plan.\n**Parallel group:** A\n", encoding="utf-8")
    assert not [i for i in validate(load_config(path), repo) if warn in i.message]
    path.write_text(path.read_text(encoding="utf-8").replace("max_subagents: 3", "max_subagents: 1"), encoding="utf-8")
    own.write_text("Write the plan.\n", encoding="utf-8")
    assert not [i for i in validate(load_config(path), repo) if warn in i.message]


def test_this_repositorys_pack_has_no_warnings_with_waves_off(repo_root):
    cfg = load_config(repo_root / ".agentflow" / "config.yaml")
    assert cfg.parallel.max_subagents == 1
    assert [i for i in validate(cfg, repo_root) if "parallel" in i.path or "plan.md" in i.path] == []


# --------------------------------------------------------------------------- speculation and the approval ramp


def ramp_errors(tmp_cfg, autonomy: dict, edit=None) -> list[Issue]:
    """The `error` issues under `autonomy` for the `python` pack (with the doubt review, so `plan` has three steps)."""
    def patch(data: dict) -> None:
        with_doubt(data)
        if edit:
            edit(data)
        data.setdefault("autonomy", {}).update(autonomy)
    return [i for i in issues_for(tmp_cfg, patch) if i.level == "error" and i.path.startswith("autonomy")]


def test_the_autonomy_ramp_keys_default_off(sparse_repo):
    cfg = load_config(pack.pack_config(sparse_repo))
    assert cfg.autonomy["speculate"] == [] and cfg.autonomy["approvals"] == {} and cfg.autonomy["ramp_samples"] == 5
    assert not [i for i in validate(cfg, sparse_repo) if i.path.startswith("autonomy")]


def test_speculate_names_a_phase_after_an_approval(tmp_cfg):
    assert ramp_errors(tmp_cfg, {"speculate": ["plan"]}) == []
    first = ramp_errors(tmp_cfg, {"speculate": ["define"]})
    assert [i.path for i in first] == ["autonomy.speculate"] and "first phase" in first[0].message
    nope = ramp_errors(tmp_cfg, {"speculate": ["nope"]})
    assert [i.path for i in nope] == ["autonomy.speculate"] and "not a phase" in nope[0].message
    bad = ramp_errors(tmp_cfg, {"speculate": "plan"})
    assert [i.path for i in bad] == ["autonomy.speculate"]

    def no_approval(data: dict) -> None:
        data["phases"]["define"]["needs_approval"] = False
    unapproved = ramp_errors(tmp_cfg, {"speculate": ["plan"]}, no_approval)
    assert [i.path for i in unapproved] == ["autonomy.speculate"] and "define" in unapproved[0].message


def test_a_speculated_phase_holds_only_artifact_steps(tmp_cfg):
    found = ramp_errors(tmp_cfg, {"speculate": ["build"]}, lambda data: data["phases"]["plan"].update(needs_approval=True))
    assert [i.path for i in found] == ["autonomy.speculate"] and "setup is not an artifact step" in found[0].message

    def no_output(data: dict) -> None:
        del phase_step(data, "plan", "plan")["output"]
    found = ramp_errors(tmp_cfg, {"speculate": ["plan"]}, no_output)
    assert [i.path for i in found] == ["autonomy.speculate"] and "step plan is not an artifact step" in found[0].message

    def commits_review(data: dict) -> None:
        phase_step(data, "plan", "plan_review")["commit"] = True
    found = ramp_errors(tmp_cfg, {"speculate": ["plan"]}, commits_review)
    assert [i.path for i in found] == ["autonomy.speculate"] and "step plan_review is not an artifact step" in found[0].message


def test_a_speculated_review_must_run_read_only(tmp_cfg):  # Ruling AA (a)
    def not_read_only(data: dict) -> None:
        data["agents"]["claude"]["read_only_roles"] = []
    found = ramp_errors(tmp_cfg, {"speculate": ["plan"]}, not_read_only)
    assert [i.path for i in found] == ["autonomy.speculate"]
    assert "plan: step plan_review is a `role: review` step agent claude does not run read-only" in found[0].message
    assert "agents.claude.read_only_roles" in found[0].message
    assert ramp_errors(tmp_cfg, {}, not_read_only) == []  # only a speculated phase needs it

    def on_codex(data: dict) -> None:
        data["agents"]["codex"].update(enabled=True, read_only_roles=["review"])
        data["phases"]["plan"]["agent"] = "codex"
    found = ramp_errors(tmp_cfg, {"speculate": ["plan"]}, on_codex)
    assert [i.path for i in found] == ["autonomy.speculate"]
    assert "agent codex does not run read-only" in found[0].message and "has no read-only mode" in found[0].message


def test_approvals_map_an_approval_phase_to_a_mode(tmp_cfg):
    from agentflow.config import APPROVAL_MODES

    assert APPROVAL_MODES == ("off", "observe", "auto")
    found = ramp_errors(tmp_cfg, {"approvals": {"build": "observe"}})
    assert [i.path for i in found] == ["autonomy.approvals.build"] and "needs_approval" in found[0].message
    found = ramp_errors(tmp_cfg, {"approvals": {"define": "sometimes"}})
    assert [i.path for i in found] == ["autonomy.approvals.define"] and "off, observe, auto" in found[0].message
    found = ramp_errors(tmp_cfg, {"approvals": {"nope": "observe"}})
    assert [i.path for i in found] == ["autonomy.approvals.nope"]
    found = ramp_errors(tmp_cfg, {"approvals": ["define"]})
    assert [i.path for i in found] == ["autonomy.approvals"]
    assert ramp_errors(tmp_cfg, {"approvals": {"define": "auto", "plan": "off"}}) == []


def test_goal_is_never_auto(tmp_cfg):
    def goal(data: dict) -> None:
        data["phases"]["goal"] = {"needs_approval": True, "steps": [
            {"id": "goal_step", "skill": "writing-plans", "role": "plan", "prompt": "plan.md", "output": "docs/goal.md"}]}
    found = ramp_errors(tmp_cfg, {"approvals": {"goal": "auto"}}, goal)
    assert [i.path for i in found] == ["autonomy.approvals.goal"] and "never auto" in found[0].message
    assert ramp_errors(tmp_cfg, {"approvals": {"goal": "observe"}}, goal) == []


@pytest.mark.parametrize("n", [2, 51, True, "5"])
def test_ramp_samples_is_a_whole_number_from_3_to_50(tmp_cfg, n):
    found = ramp_errors(tmp_cfg, {"ramp_samples": n})
    assert [i.path for i in found] == ["autonomy.ramp_samples"] and "3 to 50" in found[0].message


@pytest.mark.parametrize("n", [3, 5, 50])
def test_ramp_samples_accepts_its_bounds(tmp_cfg, n):
    assert ramp_errors(tmp_cfg, {"ramp_samples": n}) == []


# ---- the vuln block, probe exit codes and the advisory floor


def test_vuln_defaults(cfg):
    assert cfg.vuln == VulnPolicy()  # probe "" by default: `run vuln` refuses to start until a pack names one
    assert cfg.vuln.probe == "" and cfg.vuln.source == "dependabot" and cfg.vuln.min_severity == "low"
    assert cfg.vuln.sync is False and cfg.vuln.forbid == ()
    assert "**/pyproject.toml" in cfg.vuln.dependency_files and "**/Cargo.lock" in cfg.vuln.dependency_files


def test_vuln_block_loads(tmp_cfg):
    cfg = load_config(tmp_cfg({"vuln": {"probe": "eval", "min_severity": "high", "sync": True, "forbid": ["a/**"]}}))
    assert (cfg.vuln.probe, cfg.vuln.min_severity, cfg.vuln.sync, cfg.vuln.forbid) == ("eval", "high", True, ("a/**",))


def test_advisory_floor_is_added_for_the_vuln_lane(cfg):
    ob = cfg.policy.obligations["advisory_cleared"]
    assert ob.require == (Evidence("expect", "advisory_cleared"),) and ob.lanes == ("vuln",)
    assert list(cfg.policy.obligations)[-2:] == ["advisory_cleared", "engine_floor"]


def test_advisory_cleared_is_evidence_and_advisory_present_is_an_expectation():
    assert parse_evidence("expect:advisory_cleared") == Evidence("expect", "advisory_cleared")
    assert parse_evidence("expect:advisory_present") is None


def test_advisory_floor_cannot_be_nulled(tmp_cfg):
    path = tmp_cfg(_policy({"obligations": {"advisory_cleared": None}}))  # a literal null: a patch dict would delete the key
    cfg = load_config(path)
    assert cfg.policy.obligations["advisory_cleared"].lanes == ("vuln",)
    found = [i for i in validate(cfg, path.parent.parent) if i.path == "policy.obligations.advisory_cleared"]
    assert found and all(i.level == "warning" for i in found)
    assert any("cannot be removed" in i.message for i in found)


def test_advisory_floor_cannot_be_replaced(tmp_cfg):
    path = tmp_cfg({"policy": {"obligations": {"advisory_cleared": {"lanes": ["feature"], "require": ["step:verify"]}}}})
    cfg = load_config(path)
    ob = cfg.policy.obligations["advisory_cleared"]
    assert ob.require == (Evidence("expect", "advisory_cleared"),) and ob.lanes == ("vuln",)
    found = [i for i in validate(cfg, path.parent.parent) if i.path == "policy.obligations.advisory_cleared"]
    assert [i.level for i in found] == ["warning"] and "cannot be removed or changed" in found[0].message


def test_gate_exit_codes_parse(tmp_cfg):
    cfg = load_config(tmp_cfg({"gates": {"scan": {"run": "x", "ok_exit": [0], "found_exit": []},
                                         "plain": {"run": "x"}}}))
    assert (cfg.gates["scan"].ok_exit, cfg.gates["scan"].found_exit) == ((0,), ())
    assert (cfg.gates["plain"].ok_exit, cfg.gates["plain"].found_exit) == ((0,), (1,))


@pytest.mark.parametrize("value", ["0", 0, [True], ["0"], [1.5]])
def test_gate_exit_codes_must_be_lists_of_ints(tmp_cfg, value):
    with pytest.raises(ConfigError, match="ok_exit"):
        load_config(tmp_cfg({"gates": {"scan": {"run": "x", "ok_exit": value}}}))


# --------------------------------------------------------------------------- the backlog file's checks

BACKLOG_PATCH = {"work_source": {"kind": "backlog", "path": None}}


def _blg(tmp_cfg, tmp_path, text, patch=None, files=()):
    """`validate`'s issues for a `backlog` pack whose `backlog.yml` holds `text`, as `(level, path, message)`."""
    if text is not None:
        (tmp_path / "backlog.yml").write_text(text, encoding="utf-8")
    for rel in files:
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# t\n", encoding="utf-8")
    path = tmp_cfg({**BACKLOG_PATCH, **(patch or {})})
    return [(i.level, i.path, i.message) for i in validate(load_config(path), tmp_path)]


def _at(found, prefix="backlog.yml"):
    return [(level, path) for level, path, _ in found if path.startswith(prefix)]


def _messages(found, path):
    return [message for _, where, message in found if where == path]


def test_a_clean_backlog_file_adds_no_issues(tmp_cfg, tmp_path):
    text = "version: 1\nitems:\n  - {id: a, title: A, status: done}\n  - {id: b, title: B, depends_on: [a]}\n"
    found = _blg(tmp_cfg, tmp_path, text)
    assert _at(found) == [] and not [f for f in found if f[1].startswith(("work_source", "backlog"))]


def test_a_problem_of_the_file_comes_through_at_its_own_level_with_the_file_name(tmp_cfg, tmp_path):
    text = ("version: 1\nitems:\n  - {id: a, title: A, owner: me}\n  - {id: Bad Id, title: B}\n"
            "  - {id: c, title: C, spec: ../x.md}\n")
    found = _blg(tmp_cfg, tmp_path, text)
    assert _at(found) == [("warning", "backlog.yml items[0].owner"), ("error", "backlog.yml items[1].id"),
                          ("error", "backlog.yml items[2].spec")]


def test_a_missing_or_broken_file_is_one_error_named_by_the_file(tmp_cfg, tmp_path):
    assert _at(_blg(tmp_cfg, tmp_path, None)) == [("error", "backlog.yml")]
    assert _at(_blg(tmp_cfg, tmp_path, "version: 2\nitems: []\n")) == [("error", "backlog.yml version")]
    assert _at(_blg(tmp_cfg, tmp_path, "[1, 2")) == [("error", "backlog.yml")]


def test_the_issue_path_uses_the_work_source_path(tmp_cfg, tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "todo.yml").write_text("version: 1\nitems:\n  - {id: a, title: A, lane: nope}\n", encoding="utf-8")
    path = tmp_cfg({"work_source": {"kind": "backlog", "path": "docs/todo.yml"}})
    found = [(i.level, i.path) for i in validate(load_config(path), tmp_path)]
    assert ("error", "docs/todo.yml items[0].lane") in found


def test_a_path_outside_the_repository_is_reported_once(tmp_cfg, tmp_path):
    path = tmp_cfg({"work_source": {"kind": "backlog", "path": "../backlog.yml"}})
    found = [(i.level, i.path) for i in validate(load_config(path), tmp_path) if "work_source" in i.path or "backlog" in i.path]
    assert found == [("error", "work_source.path")]


def test_a_lane_that_is_not_the_packs_is_an_error(tmp_cfg, tmp_path):
    text = "version: 1\nitems:\n  - {id: a, title: A}\n  - {id: b, title: B, lane: nope}\n"
    found = _blg(tmp_cfg, tmp_path, text)
    assert _at(found) == [("error", "backlog.yml items[1].lane")]


def test_a_lane_that_is_not_the_module_lane_is_a_warning(tmp_cfg, tmp_path):
    text = "version: 1\nitems:\n  - {id: a, title: A, lane: feature}\n  - {id: b, title: B, lane: bug}\n"
    found = _blg(tmp_cfg, tmp_path, text)
    assert _at(found) == [("warning", "backlog.yml items[1].lane")]
    assert _messages(found, "backlog.yml items[1].lane") == ["runs in bug: B1 starts module_lane items only"]


def test_a_dependency_on_an_unknown_item_is_an_error(tmp_cfg, tmp_path):
    text = "version: 1\nitems:\n  - {id: a, title: A}\n  - {id: b, title: B, depends_on: [a, zz]}\n"
    found = _blg(tmp_cfg, tmp_path, text)
    assert _at(found) == [("error", "backlog.yml items[1].depends_on")]
    assert "zz" in _messages(found, "backlog.yml items[1].depends_on")[0]


def test_a_dependency_cycle_is_one_error_that_names_the_ring(tmp_cfg, tmp_path):
    text = ("version: 1\nitems:\n  - {id: c, title: C, depends_on: [a]}\n  - {id: b, title: B, depends_on: [c]}\n"
            "  - {id: a, title: A, depends_on: [b]}\n  - {id: d, title: D, depends_on: [a]}\n")
    found = _blg(tmp_cfg, tmp_path, text)
    assert _at(found) == [("error", "backlog.yml items[2].depends_on")]  # on `a`, the smallest id of the ring
    assert "a -> b -> c -> a" in _messages(found, "backlog.yml items[2].depends_on")[0]


def test_a_dependency_on_a_dropped_item_is_a_warning(tmp_cfg, tmp_path):
    text = "version: 1\nitems:\n  - {id: a, title: A, status: dropped}\n  - {id: b, title: B, depends_on: [a]}\n"
    assert _at(_blg(tmp_cfg, tmp_path, text)) == [("warning", "backlog.yml items[1].depends_on")]


def test_the_position_in_a_path_counts_the_items_the_parser_dropped(tmp_cfg, tmp_path):
    text = "version: 1\nitems:\n  - {id: Bad Id, title: X}\n  - {id: a, title: A, lane: nope}\n"
    assert _at(_blg(tmp_cfg, tmp_path, text)) == [("error", "backlog.yml items[0].id"),
                                                  ("error", "backlog.yml items[1].lane")]


def test_a_spec_or_plan_that_is_a_symlink_or_leaves_the_repository_is_an_error(tmp_cfg, tmp_path):
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (outside / "o.md").write_text("# o\n", encoding="utf-8")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "real.md").write_text("# r\n", encoding="utf-8")
    (tmp_path / "docs" / "link.md").symlink_to(tmp_path / "docs" / "real.md")
    (tmp_path / "escape").symlink_to(outside, target_is_directory=True)
    text = ("version: 1\nitems:\n  - {id: a, title: A, spec: docs/link.md}\n  - {id: b, title: B, plan: escape/o.md}\n"
            "  - {id: c, title: C, spec: docs/real.md}\n")
    found = _blg(tmp_cfg, tmp_path, text, {"project": {"docs": {"specs": "docs"}}})
    assert [f for f in _at(found) if f[0] == "error"] == [("error", "backlog.yml items[0].spec"),
                                                          ("error", "backlog.yml items[1].plan")]
    assert ("warning", "backlog.yml items[2].spec") not in _at(found)


def test_a_missing_spec_or_plan_is_a_warning(tmp_cfg, tmp_path):
    text = "version: 1\nitems:\n  - {id: a, title: A, spec: docs/specs/a.md, plan: docs/plans/a.md}\n"
    assert _at(_blg(tmp_cfg, tmp_path, text)) == [("warning", "backlog.yml items[0].spec"),
                                                  ("warning", "backlog.yml items[0].plan")]


def test_a_spec_and_plan_that_exist_are_clean(tmp_cfg, tmp_path):
    text = "version: 1\nitems:\n  - {id: a, title: A, spec: docs/specs/a.md, plan: docs/plans/a.md}\n"
    assert _at(_blg(tmp_cfg, tmp_path, text, files=("docs/specs/a.md", "docs/plans/a.md"))) == []


def test_an_issue_number_on_two_items_is_an_error(tmp_cfg, tmp_path):
    text = "version: 1\nitems:\n  - {id: a, title: A, issue: 7}\n  - {id: b, title: B, issue: 7}\n  - {id: c, title: C, issue: 8}\n"
    found = _blg(tmp_cfg, tmp_path, text)
    assert _at(found) == [("error", "backlog.yml items[1].issue")]
    assert "items[0]" in _messages(found, "backlog.yml items[1].issue")[0]


def test_a_spec_no_item_names_is_a_warning_until_it_is_added_or_ignored(tmp_cfg, tmp_path):
    text = "version: 1\nitems:\n  - {id: a, title: A, spec: docs/specs/a.md}\n"
    files = ("docs/specs/a.md", "docs/specs/x.md", "docs/specs/notes/n.md")
    found = _blg(tmp_cfg, tmp_path, text, files=files)
    assert _at(found) == [("warning", "backlog.yml"), ("warning", "backlog.yml")]
    assert sorted(m for _, p, m in found if p == "backlog.yml") == [
        "docs/specs/notes/n.md is not on the backlog: agentflow backlog add --spec docs/specs/notes/n.md, "
        "or list it in backlog.ignore_specs",
        "docs/specs/x.md is not on the backlog: agentflow backlog add --spec docs/specs/x.md, "
        "or list it in backlog.ignore_specs"]
    ignored = _blg(tmp_cfg, tmp_path, text, {"backlog": {"ignore_specs": ["x.md", "notes/*"]}})
    assert _at(ignored) == []


def test_an_item_that_is_done_by_the_runs_but_not_in_the_file_is_a_warning(tmp_cfg, tmp_path, monkeypatch):
    from agentflow import finished

    monkeypatch.setattr(finished, "recorded", lambda repo, **kw: {"a": "merged", "b": "merged", "c": "merged", "zz": "merged"})
    text = ("version: 1\nitems:\n  - {id: a, title: A, status: ready}\n  - {id: b, title: B, status: done}\n"
            "  - {id: c, title: C, status: dropped}\n")
    found = _blg(tmp_cfg, tmp_path, text)
    assert _at(found) == [("warning", "backlog.yml items[0].status")]
    assert "agentflow backlog set a status=done" in _messages(found, "backlog.yml items[0].status")[0]


def test_a_backlog_pack_without_a_module_lane_gets_a_warning(tmp_cfg, tmp_path):
    text = "version: 1\nitems:\n  - {id: a, title: A}\n"
    found = _blg(tmp_cfg, tmp_path, text, {"dashboard": {"backlog": {"module_lane": None}}})
    assert ("warning", "dashboard.backlog.module_lane") in [(level, path) for level, path, _ in found]
    assert _at(found) == []
    other = [(i.level, i.path) for i in validate(load_config(tmp_cfg({"dashboard": {"backlog": {"module_lane": None}}})), tmp_path)]
    assert ("warning", "dashboard.backlog.module_lane") not in other  # a capability map pack is not asked for one here


@pytest.mark.parametrize("value, path", [
    ("docs/*", "backlog.ignore_specs"), (3, "backlog.ignore_specs"), ([3], "backlog.ignore_specs[0]"),
    (["a\nb"], "backlog.ignore_specs[0]"), (["/abs/*"], "backlog.ignore_specs[0]"),
    (["../x"], "backlog.ignore_specs[0]"), (["a/../b"], "backlog.ignore_specs[0]"),
    (["ok", "x" * 201], "backlog.ignore_specs[1]"), ([""], "backlog.ignore_specs[0]"),
    (["a\\b"], "backlog.ignore_specs[0]"),
])
def test_ignore_specs_must_be_a_list_of_short_relative_one_line_globs(tmp_cfg, tmp_path, value, path):
    found = _blg(tmp_cfg, tmp_path, "version: 1\nitems: []\n", {"backlog": {"ignore_specs": value}})
    assert [(level, where) for level, where, _ in found if where.startswith("backlog")] == [("error", path)]


def test_a_symlinked_directory_in_the_work_source_path_that_leaves_the_repository_is_an_error(tmp_cfg, tmp_path):
    outside = tmp_path.parent / f"{tmp_path.name}-away"
    outside.mkdir()
    (outside / "backlog.yml").write_text("version: 1\nitems: []\n", encoding="utf-8")
    (tmp_path / "link").symlink_to(outside, target_is_directory=True)
    path = tmp_cfg({"work_source": {"kind": "backlog", "path": "link/backlog.yml"}})
    found = [(i.level, i.path) for i in validate(load_config(path), tmp_path) if "work_source" in i.path]
    assert found == [("error", "work_source.path")]


def test_a_symlink_loop_in_the_work_source_path_is_an_error_not_a_crash(tmp_cfg, tmp_path):
    (tmp_path / "loop").symlink_to(tmp_path / "loop")
    path = tmp_cfg({"work_source": {"kind": "backlog", "path": "loop/backlog.yml"}})
    cfg = load_config(path)
    from agentflow import work_source

    found = [(i.level, i.path) for i in validate(cfg, tmp_path) if i.path.startswith(("work_source", "loop/backlog.yml"))]
    # Python 3.12 raises on the loop (`_unsafe` says so); newer ones resolve it and the read fails instead
    assert found in ([("error", "work_source.path")], [("error", "loop/backlog.yml")])
    assert work_source.items(cfg, tmp_path) == [] and work_source.all_items(cfg, tmp_path) == []


def test_ignore_specs_accepts_a_list_of_globs(tmp_cfg, tmp_path):
    found = _blg(tmp_cfg, tmp_path, "version: 1\nitems: []\n", {"backlog": {"ignore_specs": ["README.md", "*/index.md"]}})
    assert not [e for e in found if e[1].startswith("backlog")]


def test_backlog_claim_keys_default_and_load(tmp_cfg):
    policy = load_config(tmp_cfg(None)).backlog
    assert (policy.claims, policy.claim_ref, policy.claim_stale_h) == (True, "refs", 24)
    block = {"backlog": {"claims": False, "claim_ref": "branch", "claim_stale_h": 48}}
    policy = load_config(tmp_cfg(block)).backlog
    assert (policy.claims, policy.claim_ref, policy.claim_stale_h) == (False, "branch", 48)


@pytest.mark.parametrize("key, value", [
    ("claims", "yes"), ("claim_ref", "tag"), ("claim_stale_h", 0), ("claim_stale_h", 721),
    ("claim_stale_h", True), ("claim_stale_h", "24"),
])
def test_validate_refuses_bad_claim_keys(tmp_cfg, tmp_path, key, value):
    found = _blg(tmp_cfg, tmp_path, "version: 1\nitems: []\n", {"backlog": {key: value}})
    assert [(level, where) for level, where, _ in found if where.startswith("backlog")] == [("error", f"backlog.{key}")]


def test_a_claim_ref_of_branch_with_claims_off_is_no_error(tmp_cfg, tmp_path):
    found = _blg(tmp_cfg, tmp_path, "version: 1\nitems: []\n", {"backlog": {"claims": False, "claim_ref": "branch"}})
    assert not [e for e in found if e[1].startswith("backlog")]


def test_backlog_checks_only_run_for_a_backlog_pack(tmp_cfg, tmp_path):
    (tmp_path / "backlog.yml").write_text("version: 2\n", encoding="utf-8")
    baseline = [(i.level, i.path) for i in validate(load_config(tmp_cfg(None)), tmp_path)]
    assert not [p for _, p in baseline if p.startswith("backlog")]


def _nested_alias_bomb() -> str:
    lines = ["a0: &a0 [x, x, x, x, x, x, x, x, x]"]
    lines += [f"a{i}: &a{i} [{', '.join([f'*a{i - 1}'] * 9)}]" for i in range(1, 9)]
    return "\n".join(lines) + "\n"


def test_load_config_refuses_nested_aliases(tmp_cfg):
    import time

    path = tmp_cfg()
    path.write_text(path.read_text(encoding="utf-8") + _nested_alias_bomb(), encoding="utf-8")
    started = time.monotonic()
    with pytest.raises(ConfigError) as err:
        load_config(path)
    assert time.monotonic() - started < 2
    assert str(path) in str(err.value) and "nested aliases" in str(err.value)
    user = _user_file(_nested_alias_bomb())
    with pytest.raises(ConfigError) as err:
        load_config(PYTHON_CONFIG)
    assert str(user) in str(err.value) and "nested aliases" in str(err.value)
    with pytest.raises(layers.LayerError) as err:
        layers.load_user_settings()
    assert str(user) in str(err.value) and "nested aliases" in str(err.value)


def test_load_config_refuses_an_at_reference_bomb(tmp_cfg):
    import time

    lines = ["x0: [a, a, a, a, a, a, a, a, a]"]
    lines += [f"x{i}: [{', '.join([repr('@x' + str(i - 1))] * 9)}]" for i in range(1, 8)]
    path = tmp_cfg()
    path.write_text(path.read_text(encoding="utf-8") + "\n".join(lines) + "\n", encoding="utf-8")
    started = time.monotonic()
    with pytest.raises(ConfigError, match="@ references"):
        load_config(path)
    assert time.monotonic() - started < 2


def test_load_config_refuses_an_at_reference_chain_past_the_depth_bound(tmp_cfg):
    lines = ["y0: [x]"] + [f"y{i}: ['@y{i - 1}']" for i in range(1, 80)]
    path = tmp_cfg()
    path.write_text(path.read_text(encoding="utf-8") + "\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="levels deep"):
        load_config(path)


def test_load_config_keeps_its_messages_for_a_self_referencing_or_deep_pack(tmp_cfg):
    path = tmp_cfg()
    base = path.read_text(encoding="utf-8")
    path.write_text(base + "zz: &z {b: *z}\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="refers to itself"):
        load_config(path)
    path.write_text(base + "zz: " + "[" * 70 + "x" + "]" * 70 + "\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="nested more than 64 levels deep"):
        load_config(path)


def test_load_config_refuses_a_pack_or_a_user_file_that_is_not_utf8(tmp_cfg):
    path = tmp_cfg()
    good = path.read_bytes()
    path.write_bytes(b"\xff")
    with pytest.raises(ConfigError) as err:
        load_config(path)
    assert f"{path} is not valid UTF-8 text" in str(err.value)
    path.write_bytes(good)
    user = layers.user_config_path()
    user.parent.mkdir(parents=True, exist_ok=True)
    user.write_bytes(b"\xff")
    with pytest.raises(ConfigError) as err:
        load_config(path)
    assert f"{user} is not valid UTF-8 text" in str(err.value)


def test_load_config_refuses_a_pack_over_the_byte_cap(tmp_cfg):
    path = tmp_cfg()
    path.write_text(path.read_text(encoding="utf-8") + "#" * layers.MAX_CONFIG_BYTES + "\n", encoding="utf-8")
    with pytest.raises(ConfigError) as err:
        load_config(path)
    assert str(path) in str(err.value) and f"larger than {layers.MAX_CONFIG_BYTES} bytes" in str(err.value)


def test_load_config_refuses_a_merge_key_chain_quickly(tmp_cfg):
    import time

    from test_layers import merge_chain

    path = tmp_cfg()
    base = path.read_text(encoding="utf-8")
    path.write_text(base + merge_chain(layers.MAX_CONFIG_BYTES - len(base.encode("utf-8"))), encoding="utf-8")
    started = time.monotonic()
    with pytest.raises(ConfigError) as err:
        load_config(path)
    assert time.monotonic() - started < 2
    assert str(path) in str(err.value) and "merge keys" in str(err.value)
