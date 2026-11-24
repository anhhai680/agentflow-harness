"""`agentflow backlog import`: the first `backlog.yml`, written from a capability map or from spec files."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from agentflow import backlog, backlog_file, backlog_import, finished, module_queue, work_source
from agentflow.flow import main
from agentflow.state import runs_root

from conftest import ENGINE, land_on_origin

GH_BIN = ENGINE / "tests" / "bin"
MAP = """# Capability map

| Module id | Responsibility | Depends on | Spec |
|---|---|---|---|
| `platform` | The base layer | — | SPEC-platform |
| `staff-auth` | Staff sign-in | platform | SPEC-staff-auth |
| `crm` | The customer record | platform | SPEC-crm |
| `settings` | Settings | staff-auth | SPEC-settings |
"""
PLAN_DONE = "# Plan\n\n**Spec:** `docs/specs/{spec}`\n\n### Task 1: One\n\n- [x] done\n"
PLAN_OPEN = "# Plan\n\n**Spec:** `docs/specs/{spec}`\n\n### Task 1: One\n\n- [x] done\n\n### Task 2: Two\n\n- [ ] open\n"


def write(root: Path, rel: str, text: str) -> None:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


@pytest.fixture
def fr(flow_repo, monkeypatch, tmp_path):
    """`flow_repo` with the fake `gh` first on PATH (nothing merged) and the pack installed for the CLI."""
    monkeypatch.setenv("PATH", f"{GH_BIN}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_GH_LOG", str(tmp_path / "gh.log"))
    monkeypatch.delenv("FAKE_GH_FAIL", raising=False)
    monkeypatch.delenv("FAKE_GH_JSON", raising=False)
    flow_repo.install_pack()
    return flow_repo


@pytest.fixture
def cm(fr):
    """A repository whose work source is a capability map of four modules."""
    write(fr.root, "docs/specs/CAPABILITY-MAP.md", MAP)
    return fr


@pytest.fixture
def sp(fr):
    """A repository whose work source is `specs`: `b` and `c` have no dependencies, `a` depends on `c`."""
    fr.configure({"work_source": {"kind": "specs", "path": None}})
    fr.install_pack()
    write(fr.root, "docs/specs/SPEC-a.md", "# Spec: A\n\nDepends on: c\n\n## Goal\n")
    write(fr.root, "docs/specs/SPEC-b.md", "# Spec: B\n\nDepends on: none\n\n## Goal\n")
    write(fr.root, "docs/specs/SPEC-c.md", "# Spec: C\n\nDepends on: none\n\n## Goal\n")
    return fr


def built(fr):
    items, notes = backlog_import.build(fr.cfg, fr.root)
    return {i.id: i for i in items}, [i.id for i in items], notes


# --------------------------------------------------------------------------- from a capability map


def test_a_capability_map_gives_open_items_in_row_order_then_done_alphabetically(cm):
    finished.mark(cm.root, "settings", "me")
    finished.mark(cm.root, "crm", "me")
    by_id, order, _ = built(cm)
    assert order == ["platform", "staff-auth", "crm", "settings"]
    assert [by_id[i].status for i in order] == ["ready", "ready", "done", "done"]
    assert by_id["staff-auth"].deps == ["platform"]
    assert by_id["staff-auth"].title == "Staff sign-in"  # the row's responsibility


def test_row_order_is_kept_when_it_is_not_alphabetical(cm):
    write(cm.root, "docs/specs/CAPABILITY-MAP.md", MAP.replace("`crm`", "`aaa`", 1))
    _, order, _ = built(cm)
    assert order == ["platform", "staff-auth", "aaa", "settings"]


def test_a_module_the_last_assessment_recorded_done_is_done(cm, tmp_path):
    land_on_origin(cm, tmp_path, {"docs/specs/SPEC-platform.md": "# Spec: Platform\n\nStatus: shipped\n"})
    by_id, order, _ = built(cm)
    assert by_id["platform"].status == "done"
    assert order == ["staff-auth", "crm", "settings", "platform"]
    assert finished.recorded(cm.root)["platform"] == finished.SPEC_SAYS


def test_spec_and_plan_are_set_when_the_files_exist(cm):
    write(cm.root, "docs/specs/SPEC-crm.md", "# Spec: CRM\n\nbody\n")
    write(cm.root, "docs/plans/2026-10-01-crm.md", PLAN_OPEN.format(spec="SPEC-crm.md"))
    write(cm.root, "docs/plans/2026-11-01-crm-later.md", PLAN_DONE.format(spec="SPEC-crm.md"))
    write(cm.root, "docs/plans/2026-12-01-other.md", PLAN_DONE.format(spec="SPEC-other.md"))
    by_id, _, _ = built(cm)
    assert (by_id["crm"].spec, by_id["crm"].plan) == ("docs/specs/SPEC-crm.md", "docs/plans/2026-11-01-crm-later.md")
    assert (by_id["settings"].spec, by_id["settings"].plan) == ("", "")


def test_a_symlinked_spec_is_not_followed(cm, tmp_path):
    outside = tmp_path / "outside.md"
    outside.write_text("# Outside\n", encoding="utf-8")
    (cm.root / "docs/specs/SPEC-crm.md").symlink_to(outside)
    by_id, _, _ = built(cm)
    assert by_id["crm"].spec == ""


def test_the_capability_map_file_and_readmes_are_not_orphans(cm):
    write(cm.root, "docs/specs/README.md", "# Readme\n")
    by_id, _, notes = built(cm)
    assert set(by_id) == {"platform", "staff-auth", "crm", "settings"}
    assert any("README.md" in n for n in notes)


# --------------------------------------------------------------------------- from specs


def test_specs_give_dependency_order_then_done(sp):
    _, order, _ = built(sp)
    assert order == ["b", "c", "a"]
    finished.mark(sp.root, "b", "me")
    by_id, order, _ = built(sp)
    assert order == ["c", "a", "b"]
    assert by_id["a"].deps == ["c"]
    assert by_id["b"].title == "Spec: B"  # the spec's first heading
    assert by_id["b"].status == "done"
    assert by_id["b"].spec == "docs/specs/SPEC-b.md"


def test_a_dependency_cycle_is_listed_last_with_a_note(sp):
    write(sp.root, "docs/specs/SPEC-c.md", "# Spec: C\n\nDepends on: a\n\n## Goal\n")
    _, order, notes = built(sp)
    assert order == ["b", "a", "c"]
    assert any("cycle" in n for n in notes)


def test_a_dependency_that_is_done_does_not_hold_an_item_back(sp):
    finished.mark(sp.root, "c", "me")
    _, order, _ = built(sp)
    assert order == ["a", "b", "c"]


# --------------------------------------------------------------------------- orphans


def test_orphan_specs_follow_the_source_open_items(sp):
    write(sp.root, "docs/specs/notes-on-x.md", "# Notes on X\n\nStatus: done\n")
    write(sp.root, "docs/specs/y-idea.md", "# The Y idea\n")
    write(sp.root, "docs/specs/z-sub/z-plan.md", "# Z\n\n## Goal\n")
    write(sp.root, "docs/plans/2026-10-01-y.md", PLAN_OPEN.format(spec="y-idea.md"))
    by_id, order, notes = built(sp)
    assert order == ["b", "c", "a", "y-idea", "z-plan", "notes-on-x"]
    assert (by_id["y-idea"].status, by_id["y-idea"].title) == ("ready", "The Y idea")
    assert by_id["y-idea"].spec == "docs/specs/y-idea.md"
    assert by_id["y-idea"].plan == "docs/plans/2026-10-01-y.md"
    assert by_id["notes-on-x"].status == "done"  # its header says so
    assert not any(i.spec.startswith("docs/plans") for i in by_id.values())  # a plan is not an orphan
    assert any("3 spec file" in n for n in notes)


def test_an_orphan_is_done_when_every_plan_naming_it_is_ticked(sp):
    write(sp.root, "docs/specs/y-idea.md", "# Y\n")
    write(sp.root, "docs/specs/w-idea.md", "# W\n")
    write(sp.root, "docs/specs/v-idea.md", "# V\n")
    write(sp.root, "docs/plans/2026-10-01-y-one.md", PLAN_DONE.format(spec="y-idea.md"))
    write(sp.root, "docs/plans/2026-10-02-y-two.md", PLAN_DONE.format(spec="y-idea.md"))
    write(sp.root, "docs/plans/2026-10-01-w-one.md", PLAN_DONE.format(spec="w-idea.md"))
    write(sp.root, "docs/plans/2026-10-02-w-two.md", PLAN_OPEN.format(spec="w-idea.md"))
    by_id, _, _ = built(sp)
    assert (by_id["y-idea"].status, by_id["w-idea"].status, by_id["v-idea"].status) == ("done", "ready", "ready")


def test_an_orphan_id_comes_from_the_spec_file_pattern_and_is_made_unique(sp):
    write(sp.root, "docs/specs/z/SPEC-b.md", "# Another B\n")  # fits spec_file: id `b`, which is taken
    write(sp.root, "docs/specs/My Notes_v2.md", "# Notes\n")
    by_id, order, notes = built(sp)
    assert "b-2" in by_id and "my-notes_v2" in by_id
    assert by_id["b-2"].spec == "docs/specs/z/SPEC-b.md"
    assert any("b-2" in n for n in notes)
    assert len(set(order)) == len(order)


# --------------------------------------------------------------------------- form cards


def add_card(repo: Path, card_id: str, label: str, new_module: dict | None = None, then: tuple[str, ...] = ()) -> None:
    from agentflow.events import EventLog

    log = EventLog(runs_root(repo) / backlog.LOG_NAME)
    log.append("item_added", id=card_id, lane="feature", kind="module", label=label, source="form",
               params={"module": (new_module or {}).get("module", "x")}, new_module=new_module)
    for kind in then:
        log.append(kind, id=card_id)


def test_a_card_with_a_new_module_becomes_a_backlog_item_and_another_is_left_out_with_a_note(sp):
    add_card(sp.root, "card-1", "idea-x — try X", {"module": "idea-x", "responsibility": "try X", "deps": ["b"]})
    add_card(sp.root, "card-2", "fix the thing")
    add_card(sp.root, "card-3", "started", {"module": "idea-y", "responsibility": "y", "deps": []}, ("item_started",))
    by_id, order, notes = built(sp)
    assert order[-1] == "idea-x"
    assert (by_id["idea-x"].status, by_id["idea-x"].title, by_id["idea-x"].deps) == ("backlog", "idea-x — try X", ["b"])
    assert "idea-y" not in by_id
    assert any("card-2" in n for n in notes) and any("card-3" in n for n in notes)


# --------------------------------------------------------------------------- the queue lock


def test_a_queue_that_holds_the_lock_is_not_waited_for(cm):
    finished.mark(cm.root, "crm", "me")
    with module_queue.queue_lock(cm.root):
        # flock is per open file description, so a second open in this process finds the lock held
        by_id, _, notes = built(cm)
    assert by_id["crm"].status == "done"
    assert any("queue lock" in n for n in notes)


# --------------------------------------------------------------------------- the command


def read(root: Path) -> list[backlog_file.BacklogItem]:
    found, problems = backlog_file.read(root / "backlog.yml")
    assert problems == []
    return found


def test_dry_run_prints_the_file_and_creates_nothing(cm, capsys):
    assert main(["backlog", "import", "--dry-run"]) == 0
    out, err = capsys.readouterr()
    assert out.startswith(backlog_file.HEADER)
    assert [i.id for i in backlog_file.parse(out)[0]] == ["platform", "staff-auth", "crm", "settings"]
    assert err == ""
    assert not (cm.root / "backlog.yml").exists()


def test_dry_run_puts_the_notes_on_stderr(sp, capsys):
    write(sp.root, "docs/specs/README.md", "# Readme\n")
    assert main(["backlog", "import", "--dry-run"]) == 0
    out, err = capsys.readouterr()
    assert "README" not in out
    assert "note: " in err


def test_the_real_run_writes_the_file_and_prints_the_line_to_switch_to(cm, capsys):
    assert main(["backlog", "import"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith(f"wrote {cm.root / 'backlog.yml'}")
    assert out[-1] == "work_source: {kind: backlog, path: backlog.yml}"
    assert [i.id for i in read(cm.root)] == ["platform", "staff-auth", "crm", "settings"]
    assert "capability_map" in (cm.root / ".agentflow/config.yaml").read_text(encoding="utf-8")  # the config is not edited


def test_the_written_file_is_a_work_source_that_reads_back(cm):
    finished.mark(cm.root, "platform", "me")
    assert main(["backlog", "import"]) == 0
    cm.configure({"work_source": {"kind": "backlog", "path": None}})
    assert [i.id for i in work_source.items(cm.cfg, cm.root)] == ["staff-auth", "crm", "settings", "platform"]
    assert work_source.item(cm.cfg, cm.root, "platform").status == "done"
    assert work_source.problems(cm.cfg, cm.root) == []


def test_an_existing_backlog_file_is_refused_and_left_alone(cm, capsys):
    write(cm.root, "backlog.yml", "mine\n")
    finished.mark(cm.root, "crm", "me")
    assert main(["backlog", "import"]) == 1
    assert capsys.readouterr().out.startswith("error: ")
    assert (cm.root / "backlog.yml").read_text(encoding="utf-8") == "mine\n"
    assert (runs_root(cm.root) / finished.MARKS).exists()  # nothing moved


def test_a_backlog_pack_has_nothing_to_import(cm, capsys):
    cm.configure({"work_source": {"kind": "backlog", "path": None}})
    cm.install_pack()
    assert main(["backlog", "import"]) == 1
    assert "nothing to import" in capsys.readouterr().out
    assert not (cm.root / "backlog.yml").exists()


def test_a_symlinked_backlog_file_is_refused(cm, tmp_path, capsys):
    (cm.root / "backlog.yml").symlink_to(tmp_path / "elsewhere.yml")
    assert main(["backlog", "import"]) == 1
    assert capsys.readouterr().out.startswith("error: ")
    assert not (tmp_path / "elsewhere.yml").exists()


# --------------------------------------------------------------------------- the done marks


def test_import_moves_the_done_marks_aside_so_none_still_counts(cm):
    finished.mark(cm.root, "crm", "me")
    assert finished.recorded(cm.root)["crm"] == finished.BY_HAND
    assert main(["backlog", "import"]) == 0
    root = runs_root(cm.root)
    assert not (root / finished.MARKS).exists()
    assert json.loads((root / backlog_import.IMPORTED).read_text(encoding="utf-8"))["crm"]["state"] == "done"
    assert read(cm.root)[-1].id == "crm" and read(cm.root)[-1].status == "done"
    assert "crm" not in finished.recorded(cm.root)  # a mark-only module is no longer done


def test_a_mark_left_over_after_import_does_not_make_a_ready_item_done(cm):
    finished.mark(cm.root, "crm", "me")
    assert main(["backlog", "import"]) == 0
    backlog_file.set_fields(cm.root / "backlog.yml", "crm", {"status": "ready"})
    finished.mark(cm.root, "crm", "me")  # a stale mark written again (an old tool, or a copy of the state dir)
    cm.configure({"work_source": {"kind": "backlog", "path": None}})
    assert "crm" not in finished.recorded(cm.root, hand_marks=work_source.hand_marks(cm.cfg))
    assert work_source.item(cm.cfg, cm.root, "crm").status == "ready"


def test_import_refuses_when_the_imported_marks_file_exists_and_writes_nothing(cm, capsys):
    finished.mark(cm.root, "crm", "me")
    imported = runs_root(cm.root) / backlog_import.IMPORTED
    imported.write_text("{}\n", encoding="utf-8")
    assert main(["backlog", "import"]) == 1
    assert "already exists" in capsys.readouterr().out
    assert not (cm.root / "backlog.yml").exists()
    assert imported.read_text(encoding="utf-8") == "{}\n"
    assert (runs_root(cm.root) / finished.MARKS).exists()


def test_a_dry_run_leaves_the_done_marks_in_place(cm, capsys):
    finished.mark(cm.root, "crm", "me")
    assert main(["backlog", "import", "--dry-run"]) == 0
    assert (runs_root(cm.root) / finished.MARKS).exists()
    assert "crm" in capsys.readouterr().out


def test_a_marks_file_that_is_a_directory_or_a_symlink_is_refused_before_anything_is_written(cm, capsys):
    marks = runs_root(cm.root) / finished.MARKS
    marks.mkdir(parents=True)
    assert main(["backlog", "import"]) == 1
    assert "not a regular file" in capsys.readouterr().out
    assert not (cm.root / "backlog.yml").exists()
    marks.rmdir()
    marks.symlink_to(cm.root / "README.md")
    assert main(["backlog", "import"]) == 1
    assert "not a regular file" in capsys.readouterr().out
    assert not (cm.root / "backlog.yml").exists()
    assert marks.is_symlink()


def test_a_symlinked_lock_file_is_refused_before_anything_is_written(cm, capsys):
    finished.mark(cm.root, "crm", "me")
    lock = runs_root(cm.root) / finished.LOCK
    lock.unlink()
    lock.symlink_to(cm.root / "README.md")
    assert main(["backlog", "import"]) == 1
    assert "symlink" in capsys.readouterr().out
    assert not (cm.root / "backlog.yml").exists()
    assert (runs_root(cm.root) / finished.MARKS).exists()


def test_a_dry_run_says_when_a_real_import_would_be_refused(cm, capsys):
    finished.mark(cm.root, "crm", "me")
    (runs_root(cm.root) / backlog_import.IMPORTED).write_text("{}\n", encoding="utf-8")
    assert main(["backlog", "import", "--dry-run"]) == 0
    assert "a real import would be refused" in capsys.readouterr().err


def test_a_move_that_fails_after_the_write_names_the_file_the_notes_the_switch_line_and_the_failure(cm, monkeypatch, capsys):
    finished.mark(cm.root, "crm", "me")
    real = os.rename

    def refuse(src, dst, *args, **kwargs):
        if Path(src).name == finished.MARKS:
            raise PermissionError(13, "Permission denied")
        return real(src, dst, *args, **kwargs)

    monkeypatch.setattr(backlog_import.os, "rename", refuse)
    assert main(["backlog", "import"]) == 1
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith(f"wrote {cm.root / 'backlog.yml'}")
    assert any(line.startswith("note: ") for line in out)
    assert any("could not move" in line and "Permission denied" in line for line in out)
    assert out[-1] == "work_source: {kind: backlog, path: backlog.yml}"
    assert [i.id for i in read(cm.root)][-1] == "crm"  # the file is there and fine
    assert (runs_root(cm.root) / finished.MARKS).exists()
    assert not (runs_root(cm.root) / backlog_import.IMPORTED).exists()


def test_the_moved_line_is_printed_only_when_the_marks_were_moved(cm, capsys):
    assert main(["backlog", "import"]) == 0  # no marks file: nothing to move
    assert "moved runs/" not in capsys.readouterr().out
