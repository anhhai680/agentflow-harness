"""The deprecation note `validate` prints for the `capability_map` and `specs` work sources (slice B4)."""

from __future__ import annotations

import dataclasses

from agentflow import flow
from agentflow.config import Issue, WorkSource, deprecation_notes, validate


def _with_kind(cfg, kind):
    return dataclasses.replace(cfg, work_source=None if kind is None else WorkSource(kind=kind, path=""))


def _check_note(notes, kind):
    assert len(notes) == 1
    note = notes[0]
    assert isinstance(note, Issue)
    assert note.level == "note"
    assert note.path == "work_source"
    for text in (kind, "agentflow backlog import", "4.0"):
        assert text in note.message


def test_a_capability_map_pack_gets_one_note(cfg):
    _check_note(deprecation_notes(_with_kind(cfg, "capability_map")), "capability_map")


def test_a_specs_pack_gets_one_note(cfg):
    _check_note(deprecation_notes(_with_kind(cfg, "specs")), "specs")


def test_a_backlog_pack_and_a_pack_with_no_work_source_get_none(cfg):
    assert deprecation_notes(_with_kind(cfg, "backlog")) == []
    assert deprecation_notes(_with_kind(cfg, None)) == []


def test_validate_itself_is_unchanged(cfg, pack_repo_python):
    cfg = _with_kind(cfg, "capability_map")
    assert not [i for i in validate(cfg, pack_repo_python) if i.level == "note"]


def test_the_cli_prints_the_note_and_does_not_count_it(cfg, pack_repo_python, capsys):
    cfg = _with_kind(cfg, "specs")
    flow._print_validation(cfg, pack_repo_python, pack_repo_python / ".agentflow" / "config.yaml")
    lines = capsys.readouterr().out.splitlines()
    assert any(line.startswith("note work_source:") for line in lines)
    warnings = sum(1 for line in lines if line.startswith("warning "))
    assert f"({warnings} warnings)" in "\n".join(lines)
