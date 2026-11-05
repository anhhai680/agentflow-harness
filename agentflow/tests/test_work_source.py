"""The pack's work source (`work_source:`), the next-step skills (`skills.map`), and a pack that has neither."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from agentflow import backlog, config, module_queue, sources, work_source
from agentflow.conditions import evaluate, make_ctx
from agentflow.config import WorkSource, load_config, validate
from agentflow.module_queue import next_step, run_queue

from conftest import ENGINE, run_git

GH_BIN = ENGINE / "tests" / "bin"
MAP = "| Module id | Responsibility | Depends on |\n|---|---|---|\n| `a` | base | — |\n| `b` | bee | a |\n"
PLAN_OPEN = "# Plan\n\n### Task 1: One\n\n- [x] done\n\n### Task 2: Two\n\n- [ ] open\n"
PLAN_DONE = "# Plan\n\n### Task 1: One\n\n- [x] done\n"
APPROVED_SPEC = "# Spec\n\n**Status: approved** 2026-10-28\n"


def write(root: Path, rel: str, text: str) -> None:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def commit_all(root: Path) -> None:
    run_git(root, "add", "-A")
    run_git(root, "commit", "-q", "-m", "fixtures")


@pytest.fixture
def fr(flow_repo, monkeypatch, tmp_path):
    """`flow_repo` with the fake `gh` first on PATH (nothing merged)."""
    monkeypatch.setenv("PATH", f"{GH_BIN}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_GH_LOG", str(tmp_path / "gh.log"))
    monkeypatch.delenv("FAKE_GH_FAIL", raising=False)
    monkeypatch.delenv("FAKE_GH_JSON", raising=False)
    return flow_repo


@pytest.fixture
def bare(fr):
    """`fr` reconfigured as a pack with no work source and no next-step skills (the minimal `node`-like pack)."""
    fr.configure({"work_source": None, "skills": None})
    return fr


# --------------------------------------------------------------------------- loading


def test_work_source_and_skills_map_load_from_the_python_pack(cfg):
    assert cfg.work_source == WorkSource(kind="capability_map", path="docs/specs/CAPABILITY-MAP.md")
    assert cfg.skills_map["build"] == "subagent-driven-development"
    assert cfg.skills_map["ship"] == "finishing-a-development-branch"
    assert "capability_map" not in cfg.project.docs


def test_a_pack_without_them_loads(tmp_cfg):
    cfg = load_config(tmp_cfg({"work_source": None, "skills": None}))
    assert cfg.work_source is None and cfg.skills_map == {}


def test_capability_map_placeholder_comes_from_the_work_source(tmp_cfg, repo_root):
    cfg = load_config(tmp_cfg({"work_source": {"path": "plan/modules.md"}}))
    assert make_ctx(cfg, repo_root, "feature", {"module": "crm"}).values["capability_map"] == "plan/modules.md"
    none = load_config(tmp_cfg({"work_source": None}))
    assert make_ctx(none, repo_root, "feature", {"module": "crm"}).values["capability_map"] == ""


# --------------------------------------------------------------------------- items and add_item


def test_items_reads_the_table_at_the_work_source_path(tmp_cfg, tmp_path):
    cfg = load_config(tmp_cfg({"work_source": {"path": "plan/modules.md"}}))
    assert work_source.items(cfg, tmp_path) == []  # the file is missing
    write(tmp_path, "plan/modules.md", MAP)
    assert work_source.items(cfg, tmp_path) == [sources.ModuleNode("a", []), sources.ModuleNode("b", ["a"])]


def test_items_is_empty_without_a_work_source(tmp_cfg, tmp_path):
    write(tmp_path, "docs/specs/CAPABILITY-MAP.md", MAP)
    assert work_source.items(load_config(tmp_cfg({"work_source": None})), tmp_path) == []


def test_add_item_appends_a_row(tmp_cfg, tmp_path):
    cfg = load_config(tmp_cfg({"work_source": {"path": "plan/modules.md"}}))
    write(tmp_path, "plan/modules.md", MAP)
    work_source.add_item(cfg, tmp_path, {"Module id": "`{module}`", "Depends on": "{deps}"},
                         {"module": "c", "responsibility": "sea", "deps": ["a"]})
    assert work_source.items(cfg, tmp_path)[-1] == sources.ModuleNode("c", ["a"])


def _unsafe_paths(tmp_path: Path) -> list[str]:
    outside = tmp_path / "outside"
    outside.mkdir()
    write(outside, "modules.md", MAP)
    (tmp_path / "repo").mkdir()
    (tmp_path / "repo" / "link").symlink_to(outside, target_is_directory=True)
    return [str(outside / "modules.md"), "../outside/modules.md", "link/modules.md"]


@pytest.mark.parametrize("which", [0, 1, 2], ids=["absolute", "dotdot", "symlink"])
def test_a_work_source_path_outside_the_repository_is_refused(tmp_cfg, tmp_path, which):
    path = _unsafe_paths(tmp_path)[which]
    repo = tmp_path / "repo"
    cfg = load_config(tmp_cfg({"work_source": {"path": path}}))
    with pytest.raises(ValueError, match="work_source.path must stay inside the repository"):
        work_source.add_item(cfg, repo, {"Module id": "{module}"}, {"module": "c", "responsibility": "x", "deps": []})
    assert (tmp_path / "outside" / "modules.md").read_text(encoding="utf-8") == MAP  # nothing written
    assert work_source.items(cfg, repo) == []  # a bad path never breaks the Backlog render, it reads nothing
    assert work_source.file(cfg, repo) is None


def test_add_item_without_a_work_source_is_refused(tmp_cfg, tmp_path):
    cfg = load_config(tmp_cfg({"work_source": None}))
    with pytest.raises(ValueError, match="this pack has no work_source"):
        work_source.add_item(cfg, tmp_path, {"Module id": "{module}"}, {"module": "c"})


def test_spec_modules_reads_the_template(tmp_path):
    (tmp_path / "SPEC-crm.md").touch()
    (tmp_path / "notes.md").touch()
    assert sources.spec_modules(tmp_path, "SPEC-{module}.md") == {"crm": tmp_path / "SPEC-crm.md"}
    assert sources.spec_modules(tmp_path, "{module}.md") == {"SPEC-crm": tmp_path / "SPEC-crm.md",
                                                            "notes": tmp_path / "notes.md"}
    (tmp_path / "crm-v-crm.md").touch()
    (tmp_path / "crm-v-cms.md").touch()
    assert sources.spec_modules(tmp_path, "{module}-v-{module}.md") == {"crm": tmp_path / "crm-v-crm.md"}
    assert sources.spec_modules(tmp_path / "missing", "{module}.md") == {}


# --------------------------------------------------------------------------- next


def test_security_line_comes_from_the_overlay_and_approval_note(cfg):
    assert module_queue.security_line(cfg) == ("a security path is involved: add `security-and-hardening` at Plan "
                                               "and Review and offer `/doubt-driven-development` before Build")


def test_security_line_is_empty_without_the_overlay_or_the_note(tmp_cfg):
    assert module_queue.security_line(load_config(tmp_cfg({"overlays": {"security": {"add_skills": None}}}))) == ""
    assert module_queue.security_line(load_config(tmp_cfg({"phases": {"plan": {"approval_note": None}}}))) == ""


def test_skills_come_from_skills_map(fr):
    fr.configure({"skills": {"map": {"build": "my-build", "build_small": "my-small"}}})
    write(fr.root, "docs/plans/2026-10-28-crm.md", PLAN_OPEN)
    assert next_step(fr.cfg, fr.root).next == "my-small"


def test_without_skills_map_next_names_the_stage(bare):
    write(bare.root, "docs/plans/2026-10-28-crm.md", PLAN_OPEN)
    assert next_step(bare.cfg, bare.root).next == "Build stage"
    write(bare.root, "docs/plans/2026-10-28-crm.md", PLAN_DONE)
    answer = next_step(bare.cfg, bare.root)
    assert (answer.next, answer.reason) == ("Verify stage", "then Ship stage")


def test_rule_texts_carry_no_project_wording(fr):
    write(fr.root, "toy/knowledge/retrieval/x.py", "x = 1\n")
    assert next_step(fr.cfg, fr.root).reason == "run the eval gate; a regression blocks the change"
    (fr.root / "toy/knowledge/retrieval/x.py").unlink()
    idle = next_step(fr.cfg, fr.root)
    assert idle.next == "grilling or spec-driven-development"
    assert idle.reason == ("nothing is in flight: ask what to do; grilling for a decision, "
                           "spec-driven-development for something to build")


def test_design_doc_rule_uses_docs_idea_decisions_glossary(fr):
    write(fr.root, "docs/decisions/ADR-099.md", "# ADR\n")
    write(fr.root, "docs/idea.md", "# idea\n")
    write(fr.root, "GLOSSARY.md", "# terms\n")
    answer = next_step(fr.cfg, fr.root)
    assert answer.lane == "design-doc"
    assert answer.next == "domain-modeling"
    assert answer.reason == "finish the design-doc edits, then commit when the user asks"


def test_design_doc_rule_matches_a_directory_by_prefix_only(fr):
    write(fr.root, "docs/decisions-old/x.md", "# not an ADR\n")
    assert next_step(fr.cfg, fr.root).lane == "none"


def test_a_pack_without_design_docs_has_no_design_doc_lane(fr):
    fr.configure({"project": {"docs": {"idea": None, "decisions": None}}})
    write(fr.root, "docs/decisions/ADR-099.md", "# ADR\n")
    assert next_step(fr.cfg, fr.root).lane == "none"


def test_no_work_source_next_falls_through(bare):
    assert next_step(bare.cfg, bare.root).lane == "none"
    write(bare.root, "docs/specs/SPEC-crm.md", APPROVED_SPEC)
    commit_all(bare.root)
    answer = next_step(bare.cfg, bare.root)
    assert (answer.lane, answer.stage, answer.next) == ("feature", "Plan", "Plan stage")
    write(bare.root, "docs/plans/2026-10-28-crm.md", PLAN_DONE)
    assert (next_step(bare.cfg, bare.root).lane, next_step(bare.cfg, bare.root).stage) == ("feature", "Verify")


# --------------------------------------------------------------------------- queue and Backlog


def test_no_work_source_queue_refuses(bare):
    with pytest.raises(ValueError, match="work_source"):
        run_queue(bare.cfg, bare.root, 1, lambda module: "never", once=True)


def test_no_work_source_cmd_queue_says_why(bare, monkeypatch, capsys):
    from agentflow.flow import main

    bare.install_pack()
    monkeypatch.chdir(bare.root)
    assert main(["queue", "--once"]) == 1
    assert "work_source" in capsys.readouterr().out


def test_no_work_source_backlog_has_only_added_cards(bare):
    write(bare.root, "docs/specs/CAPABILITY-MAP.md", MAP)  # a map file the pack does not name
    commit_all(bare.root)
    assert backlog.snapshot(bare.cfg, bare.root)["cards"] == []
    item = backlog.add(bare.cfg, bare.root, "bug", {"issue": "12"})
    assert [c["key"] for c in backlog.snapshot(bare.cfg, bare.root)["cards"]] == [f"card:{item.id}"]


def test_no_work_source_new_module_run_is_refused(bare):
    from agentflow import pipeline

    with pytest.raises(ValueError, match="work_source"):
        pipeline._check_new_module(bare.cfg, {"module": "e"}, {"module": "e", "responsibility": "x", "deps": []})


def test_modules_done_reads_the_work_source(fr, monkeypatch):
    fr.configure({"work_source": {"path": "plan/modules.md"}})
    write(fr.root, "plan/modules.md", MAP)
    ctx = make_ctx(fr.cfg, fr.root, "feature", {"module": "b"})
    monkeypatch.setenv("FAKE_GH_JSON", json.dumps([{"headRefName": "flow/feature/a"}]))
    result = evaluate(fr.cfg, "deps_merged", ctx)
    assert result.value is True and result.reason.endswith(": merged: a")
    ghost = evaluate(fr.cfg, "deps_merged", make_ctx(fr.cfg, fr.root, "feature", {"module": "ghost"}))
    assert ghost.value is None and ghost.reason.endswith(": module 'ghost' is not in the capability map")


# --------------------------------------------------------------------------- validate


def errors_at(cfg, repo, path: str) -> list[str]:
    return [i.message for i in validate(cfg, repo) if i.level == "error" and i.path == path]


def test_the_python_pack_has_no_work_source_or_skills_errors(cfg, repo_root):
    assert not [i for i in validate(cfg, repo_root) if i.path.startswith(("work_source", "skills"))]


def test_a_work_source_of_another_kind_is_an_error(tmp_cfg, repo_root):
    cfg = load_config(tmp_cfg({"work_source": {"kind": "github_issues"}}))
    assert errors_at(cfg, repo_root, "work_source.kind") == ["the only work source this engine reads is capability_map"]


@pytest.mark.parametrize("path", ["", "/abs/map.md", "../map.md"])
def test_a_work_source_path_outside_the_repo_is_an_error(tmp_cfg, repo_root, path):
    cfg = load_config(tmp_cfg({"work_source": {"path": path}}))
    assert errors_at(cfg, repo_root, "work_source.path")


def test_a_leftover_docs_capability_map_points_at_work_source(tmp_cfg, repo_root):
    cfg = load_config(tmp_cfg({"project": {"docs": {"capability_map": "docs/specs/CAPABILITY-MAP.md"}}}))
    assert any("work_source" in m for m in errors_at(cfg, repo_root, "project.docs.capability_map"))


def test_skills_map_unknown_key_is_an_error(tmp_cfg, repo_root):
    cfg = load_config(tmp_cfg({"skills": {"map": {"deploy": "grilling"}}}))
    assert errors_at(cfg, repo_root, "skills.map.deploy")


def test_skills_map_value_must_be_a_skill_in_skills_dir(tmp_cfg, repo_root):
    cfg = load_config(tmp_cfg({"skills": {"map": {"build": "no-such-skill"}}}))
    assert any("no-such-skill" in m for m in errors_at(cfg, repo_root, "skills.map.build"))


def test_modules_done_from_is_refused(tmp_cfg, repo_root):
    cfg = load_config(tmp_cfg({"conditions": {"deps_merged": {"check": "modules_done", "from": "capability_map"}}}))
    assert errors_at(cfg, repo_root, "conditions.deps_merged.from") == ["modules_done reads work_source"]


@pytest.mark.parametrize("spec_file", ["{module}-{x}.md", "{module}{", "{module}{0}.md", "{module}{module.x}.md"])
def test_a_spec_file_that_cannot_be_filled_is_an_error(tmp_cfg, repo_root, spec_file):
    cfg = load_config(tmp_cfg({"project": {"docs": {"spec_file": spec_file}}}))
    assert errors_at(cfg, repo_root, "project.docs.spec_file")


def test_state_dir_check_without_git_does_not_crash(tmp_git, monkeypatch):
    from test_pack import write_pack

    cfg = load_config(write_pack(tmp_git, {"paths": {"state_dir": "state"}}))
    real = subprocess.run

    def no_git(argv, *args, **kwargs):
        if argv[:2] == ["git", "check-ignore"]:
            raise FileNotFoundError("git")
        return real(argv, *args, **kwargs)

    monkeypatch.setattr(config.subprocess, "run", no_git)
    assert not [i for i in validate(cfg, tmp_git) if i.path == "paths.state_dir"]
