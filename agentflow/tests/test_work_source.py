"""The pack's work source (`work_source:`), the next-step skills (`skills.map`), and a pack that has neither."""

from __future__ import annotations

import dataclasses
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

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
    assert errors_at(cfg, repo_root, "work_source.kind") == ["the work source kinds are capability_map and specs"]


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


# --------------------------------------------------------------------------- the `specs` kind

SPECS = {"work_source": {"kind": "specs", "path": None}}  # `None` deletes the python pack's map path


def _pack(fr, tmp_cfg, patch: dict, name: str) -> SimpleNamespace:
    """`fr`'s repository with the python pack plus `patch`, loaded from its own file so `fr.cfg` stays as it was."""
    return SimpleNamespace(root=fr.root, cfg=load_config(tmp_cfg(patch, name=name)), fr=fr)


@pytest.fixture
def specs(fr, tmp_cfg):
    """A pack whose work source is its spec files, `docs/specs/SPEC-<id>.md`."""
    return _pack(fr, tmp_cfg, SPECS, "specs.yaml")


@pytest.fixture
def plain(fr, tmp_cfg):
    """`specs` with the default `{module}.md` spec file name."""
    return _pack(fr, tmp_cfg, {**SPECS, "project": {"docs": {"spec_file": None}}}, "plain.yaml")


def issues_at(cfg, repo, prefixes: tuple[str, ...]) -> list:
    return [i for i in validate(cfg, repo) if i.path.startswith(prefixes)]


def warnings_at(cfg, repo, path: str) -> list[str]:
    return [i.message for i in validate(cfg, repo) if i.level == "warning" and i.path == path]


def test_a_specs_source_lists_one_module_per_spec_file_with_header_deps(specs):
    write(specs.root, "docs/specs/SPEC-b.md", "# B\n\n**Depends on:** `a`\n\n## Scope\n**Depends on:** z\n")
    write(specs.root, "docs/specs/SPEC-a.md", "# A\n\nDepends on: None (standalone).\n")
    write(specs.root, "docs/specs/notes.md", "# not a spec\n")
    assert work_source.items(specs.cfg, specs.root) == [sources.ModuleNode("a", []), sources.ModuleNode("b", ["a"])]
    assert work_source.skipped(specs.cfg, specs.root) == []  # notes.md does not fit the spec file name


@pytest.mark.parametrize("header, deps", [
    ("**Depends on**: a, b", ["a", "b"]),
    ("- Depends on: `a`.", ["a"]),
    ("Depends on the auth module for tokens.", []),
    ("**Depends on:** ghost, Auth", ["ghost", "Auth"]),
    ("**Depends on:**\n- a\n- b\n\nmore", ["a", "b"]),
    ("Depends on: a,\n  b", ["a", "b"]),
    ("Depends on: a\nDepends on: c", ["a", "c"]),
    ("Depends on: none-pay", ["none-pay"]),
    ("Depends on: none, a", ["none", "a"]),
    ("Depends on: a" + "b" * 600, [sources.BAD_DEP, sources.CUT_DEP]),   # an id over 80 chars, on a cut line
    ("Depends on: \x1b[31mred", [sources.BAD_DEP]),
])
def test_dependency_line_forms(header, deps):
    assert sources.spec_deps(f"# X\n\n{header}\n") == deps


@pytest.mark.parametrize("header", ["Depends on: none", "**Depends on:** None.", "Depends on: none (standalone)",
                                    "Depends on: NONE (yet)."])
def test_only_none_means_no_dependencies(header):
    assert sources.spec_deps(f"# X\n\n{header}\n") == []


def test_dependency_lines_in_a_fence_or_past_the_header_do_not_count():
    assert sources.spec_deps("# X\n\n```\nDepends on: a\n```\n\n## Scope\nDepends on: b\n") == []
    assert sources.spec_header("# X\n```\nfenced\n```\nkept\n## Next\nafter\n") == [("# X", False), ("kept", False)]
    assert sources.spec_header("x" * 600) == [("x" * sources.LINE_MAX, True)]


def test_spec_ids_names_every_file_that_fits_and_is_module_id_filters_them():
    assert sources.spec_ids(["Foo Bar.md", "ok.md", "x.txt", "index.md"], "{module}.md") == {
        "Foo Bar": "Foo Bar.md", "ok": "ok.md", "index": "index.md"}
    assert sources.spec_ids(["SPEC-a.md", "a.md"], "SPEC-{module}.md") == {"a": "SPEC-a.md"}
    assert sources.is_module_id("ok", "{module}.md") and not sources.is_module_id("Foo Bar", "{module}.md")
    assert not sources.is_module_id("index", "{module}.md") and sources.is_module_id("index", "SPEC-{module}.md")


def test_read_header_reads_at_most_its_limit_and_replaces_bad_bytes(tmp_path):
    path = tmp_path / "x.md"
    path.write_bytes(b"# X\n\xff" + b"y" * 100)
    assert sources.read_header(path, limit=6) == "# X\n�y"


def test_files_that_are_not_modules_are_skipped_only_under_the_bare_pattern(plain, specs):
    folder = plain.root / "docs" / "specs"
    for name in ("ok.md", "README.md", "index.md", "template.md", "Foo Bar.md"):
        write(plain.root, f"docs/specs/{name}", "# spec\n")
    (folder / "ln.md").symlink_to(folder / "ok.md")
    (folder / "dir.md").mkdir()
    assert [n.id for n in work_source.items(plain.cfg, plain.root)] == ["ok"]
    assert work_source.skipped(plain.cfg, plain.root) == sorted(
        ["README.md", "index.md", "template.md", "Foo Bar.md", "ln.md", "dir.md"])
    assert not issues_at(plain.cfg, plain.root, ("work_source", "project.docs.specs"))
    write(specs.root, "docs/specs/SPEC-index.md", "# Index module\n")
    assert [n.id for n in work_source.items(specs.cfg, specs.root)] == ["index"]
    assert work_source.skipped(specs.cfg, specs.root) == []


def test_an_unreadable_spec_is_skipped_not_raised(plain, monkeypatch):
    write(plain.root, "docs/specs/ok.md", "# ok\n")
    write(plain.root, "docs/specs/locked.md", "# locked\n")
    real = sources.read_header

    def read_header(path, limit=sources.HEADER_MAX):
        if Path(path).name == "locked.md":
            raise PermissionError(13, "Permission denied", str(path))
        return real(path, limit)

    monkeypatch.setattr(sources, "read_header", read_header)
    assert [n.id for n in work_source.items(plain.cfg, plain.root)] == ["ok"]
    assert work_source.skipped(plain.cfg, plain.root) == ["locked.md (unreadable)"]


def test_entries_past_specs_max_are_skipped_once(plain, monkeypatch):
    for name in ("a", "b", "c", "d"):
        write(plain.root, f"docs/specs/{name}.md", "# spec\n")
    monkeypatch.setattr(sources, "SPECS_MAX", 2)
    assert [n.id for n in work_source.items(plain.cfg, plain.root)] == ["a", "b"]
    assert work_source.skipped(plain.cfg, plain.root) == ["… and 2 more (over 2 files)"]


def test_headers_are_reread_only_when_a_file_changes(plain, monkeypatch):
    write(plain.root, "docs/specs/a.md", "# A\n")
    write(plain.root, "docs/specs/b.md", "# B\n\nDepends on: a\n")
    calls: list[str] = []
    real = sources.read_header

    def read_header(path, limit=sources.HEADER_MAX):
        calls.append(Path(path).name)
        return real(path, limit)

    monkeypatch.setattr(sources, "read_header", read_header)
    first = work_source.items(plain.cfg, plain.root)
    assert work_source.items(plain.cfg, plain.root) == first
    assert sorted(calls) == ["a.md", "b.md"]
    target = plain.root / "docs" / "specs" / "a.md"
    st = target.stat()
    os.utime(target, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))  # a touch
    work_source.items(plain.cfg, plain.root)
    assert sorted(calls) == ["a.md", "a.md", "b.md"]
    write(plain.root, "docs/specs/a.md", "# A\n\nDepends on: c, d\n")  # a new size: read again, new deps
    assert work_source.items(plain.cfg, plain.root)[0] == sources.ModuleNode("a", ["c", "d"])
    assert sorted(calls) == ["a.md", "a.md", "a.md", "b.md"]


def test_a_specs_pack_validates_clean(specs):
    write(specs.root, "docs/specs/SPEC-a.md", "# A\n")
    assert not issues_at(specs.cfg, specs.root, ("work_source", "project.docs", "dashboard.backlog"))


def test_a_specs_pack_with_a_path_a_bad_docs_dir_or_no_module_lane_is_reported(specs, tmp_cfg, tmp_path):
    with_path = load_config(tmp_cfg({"work_source": {"kind": "specs", "path": "docs/specs"}}, name="p.yaml"))
    assert errors_at(with_path, specs.root, "work_source.path") == ["specs reads project.docs.specs; remove path"]
    (tmp_path / "elsewhere").mkdir()
    (specs.root / "speclink").symlink_to(tmp_path / "elsewhere", target_is_directory=True)
    linked = load_config(tmp_cfg({**SPECS, "project": {"docs": {"specs": "speclink"}}}, name="l.yaml"))
    assert errors_at(linked, specs.root, "project.docs.specs")
    assert work_source.directory(linked, specs.root) is None
    write(specs.root, "notadir.md", "# a file\n")
    a_file = load_config(tmp_cfg({**SPECS, "project": {"docs": {"specs": "notadir.md"}}}, name="f.yaml"))
    assert errors_at(a_file, specs.root, "project.docs.specs")
    assert work_source.directory(a_file, specs.root) is None
    for bad in ("../outside", str(tmp_path / "elsewhere")):
        outside = load_config(tmp_cfg({**SPECS, "project": {"docs": {"specs": bad}}}, name="o.yaml"))
        assert errors_at(outside, specs.root, "project.docs.specs"), bad
        assert work_source.directory(outside, specs.root) is None
    no_lane = load_config(tmp_cfg({**SPECS, "dashboard": {"backlog": {"module_lane": None}}}, name="n.yaml"))
    assert warnings_at(no_lane, specs.root, "dashboard.backlog.module_lane") == [
        "a specs work source lists modules only for the module_lane"]
    assert not warnings_at(specs.cfg, specs.root, "dashboard.backlog.module_lane")


def test_a_new_module_field_without_module_row_is_clean_only_for_a_specs_pack(fr, tmp_cfg):
    form = {"dashboard": {"backlog": {"new_task": {"feature": ["new_module", "after"]}, "module_row": None}}}
    where = "dashboard.backlog.new_task.feature"
    write(fr.root, "docs/specs/SPEC-a.md", "# A\n")
    assert not errors_at(load_config(tmp_cfg({**SPECS, **form}, name="s.yaml")), fr.root, where)
    assert errors_at(load_config(tmp_cfg(form, name="m.yaml")), fr.root, where)
    assert errors_at(load_config(tmp_cfg({"work_source": None, **form}, name="z.yaml")), fr.root, where)


def test_capability_map_wording_is_unchanged_and_specs_wording_names_spec_files(fr, specs):
    assert work_source.noun(fr.cfg) == "capability map"
    assert work_source.noun(specs.cfg) == "spec files"
    assert work_source.noun(dataclasses.replace(fr.cfg, work_source=None)) == "capability map"


def test_add_item_is_a_no_op_and_capability_map_placeholder_is_empty_for_specs(specs):
    write(specs.root, "docs/specs/SPEC-a.md", "# A\n")
    folder = specs.root / "docs" / "specs"
    before = {p.name: p.read_bytes() for p in folder.iterdir()}
    work_source.add_item(specs.cfg, specs.root, {"Module id": "{module}", "Depends on": "{deps}"},
                         {"module": "c", "responsibility": "sea", "deps": ["a"]})
    assert {p.name: p.read_bytes() for p in folder.iterdir()} == before
    assert make_ctx(specs.cfg, specs.root, "feature", {"module": "a"}).values["capability_map"] == ""
    assert work_source.file(specs.cfg, specs.root) is None
    assert work_source.directory(specs.cfg, specs.root) == specs.root / "docs" / "specs"


def test_a_new_module_flag_is_refused_for_a_specs_pack(specs):
    from agentflow import pipeline

    with pytest.raises(ValueError, match="--task"):
        pipeline._check_new_module(specs.cfg, {"module": "e"}, {"module": "e", "responsibility": "x", "deps": []})


def test_modules_done_names_the_spec_files_for_an_unknown_module(specs):
    write(specs.root, "docs/specs/SPEC-a.md", "# A\n")
    ghost = evaluate(specs.cfg, "deps_merged", make_ctx(specs.cfg, specs.root, "feature", {"module": "ghost"}))
    assert ghost.value is None and ghost.reason.endswith(": module 'ghost' is not in the spec files")


def _long_spec(root: Path, name: str, before: str, after: str) -> Path:
    """A spec of `before`, then `x` padding (no `## ` heading) up to byte `HEADER_MAX`, then `after`."""
    path = root / "docs" / "specs" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    pad = sources.HEADER_MAX - len(before.encode("utf-8"))
    path.write_bytes(before.encode("utf-8") + b"x" * max(pad, 0) + after.encode("utf-8"))
    return path


def _deps(cfg, root: Path, module: str) -> list[str]:
    return next(n.deps for n in work_source.items(cfg, root) if n.id == module)


def test_a_header_past_header_max_with_no_heading_read_blocks(plain):
    # ~9.6 KB of preamble with no `## ` (only `#` / `###` headings), then the dependency line past the cut
    preamble = "# Long\n\n### Context\n" + ("prose line of the preamble\n" * 360)
    write(plain.root, "docs/specs/long.md", preamble + "\nDepends on: a\n")
    assert (plain.root / "docs/specs/long.md").stat().st_size > sources.HEADER_MAX
    assert _deps(plain.cfg, plain.root, "long") == [sources.CUT_DEP]
    node = sources.ModuleNode("long", [sources.CUT_DEP])
    assert "which is not in the capability map" in module_queue._map_problem(node, {"long": [sources.CUT_DEP]})


def test_a_dependency_line_straddling_the_cut_blocks_not_a_shorter_id(plain):
    # the read stops after `Depends on: crm`, so the truncated id must never stand alone
    path = plain.root / "docs" / "specs" / "straddle.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    head = "# S\n\n" + "x" * (sources.HEADER_MAX - len("# S\n\n") - len("\nDepends on: crm")) + "\nDepends on: crm"
    path.write_bytes(head.encode("utf-8") + b"-api\n\n## Scope\n")
    assert sources.read_header(path).endswith("Depends on: crm")
    assert _deps(plain.cfg, plain.root, "straddle") == ["crm", sources.CUT_DEP]  # blocked: CUT_DEP is no module


def test_a_long_spec_whose_header_ends_inside_the_read_is_unchanged(plain):
    write(plain.root, "docs/specs/ended.md", "# E\n\nDepends on: a\n\n## Scope\n" + "body\n" * 3000)
    assert (plain.root / "docs/specs/ended.md").stat().st_size > sources.HEADER_MAX
    assert _deps(plain.cfg, plain.root, "ended") == ["a"]


@pytest.mark.parametrize("extra", [0, -1])
def test_a_spec_of_at_most_header_max_bytes_is_unchanged(plain, extra):
    body = "# U\n\nDepends on: a\n"
    path = _long_spec(plain.root, "under.md", body, "")
    path.write_bytes(path.read_bytes()[: sources.HEADER_MAX + extra])
    assert path.stat().st_size == sources.HEADER_MAX + extra
    assert _deps(plain.cfg, plain.root, "under") == ["a"]
