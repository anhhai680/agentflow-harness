"""`finished`: hand marks and vetoes in `runs/done_marks.json`, and the done modules the last assessment recorded."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest
from agentflow import finished
from agentflow.state import runs_root


def _queue(repo: Path, modules: list[dict]) -> None:
    path = runs_root(repo) / finished.QUEUE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"modules": modules}), encoding="utf-8")


def test_reasons_are_the_words_the_backlog_shows():
    assert (finished.BY_HAND, finished.SPEC_SAYS, finished.MERGED) == ("done by hand", "spec says done", "merged")
    assert finished.plan_complete("next") == "plan complete on next"


def test_mark_veto_and_unmark_round_trip(tmp_git):
    assert finished.read_marks(tmp_git) == ({}, None)
    finished.mark(tmp_git, "crm", "cli")
    finished.veto(tmp_git, "platform", "pat")
    marks, error = finished.read_marks(tmp_git)
    assert error is None
    assert {m: (v["state"], v["by"]) for m, v in marks.items()} == {"crm": ("done", "cli"), "platform": ("not_done", "pat")}
    assert all(v["at"] for v in marks.values())
    finished.veto(tmp_git, "crm", "pat")  # a veto replaces a mark
    assert finished.read_marks(tmp_git)[0]["crm"]["state"] == "not_done"
    assert finished.unmark(tmp_git, "crm") is True
    assert finished.unmark(tmp_git, "crm") is False
    assert set(finished.read_marks(tmp_git)[0]) == {"platform"}
    assert not list(runs_root(tmp_git).glob(f"{finished.MARKS}.*"))  # no temporary file is left behind


def test_two_threads_marking_different_modules_both_land(tmp_git):
    ids = [f"m{n}" for n in range(12)]
    threads = [threading.Thread(target=finished.mark, args=(tmp_git, m, "t")) for m in ids]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(finished.read_marks(tmp_git)[0]) == sorted(ids)


def _torn(path: Path) -> None:
    path.write_text('{"crm": {"state": "do', encoding="utf-8")


def _a_list(path: Path) -> None:
    path.write_text('[{"crm": "done"}]', encoding="utf-8")


def _bad_entry(path: Path) -> None:
    path.write_text('{"crm": {"state": "maybe", "by": "x", "at": "y"}}', encoding="utf-8")


def _unreadable(path: Path) -> None:
    path.mkdir()  # a directory where the file should be: reading it is an OSError


@pytest.mark.parametrize("corrupt", [_torn, _a_list, _bad_entry, _unreadable])
def test_a_corrupt_marks_file_is_an_error_naming_it_and_mark_refuses(tmp_git, corrupt):
    path = runs_root(tmp_git) / finished.MARKS
    path.parent.mkdir(parents=True, exist_ok=True)
    corrupt(path)
    marks, error = finished.read_marks(tmp_git)
    assert marks == {}
    assert error and error.startswith(f"{path} is unreadable (") and error.endswith("fix it or delete it, then mark again")
    for change in (lambda: finished.mark(tmp_git, "b", "cli"), lambda: finished.veto(tmp_git, "b", "cli"),
                   lambda: finished.unmark(tmp_git, "b")):
        with pytest.raises(ValueError, match="unreadable"):
            change()


def test_recorded_merges_queue_entries_and_live_marks(tmp_git):
    _queue(tmp_git, [
        {"id": "a", "status": "done", "done_reason": finished.SPEC_SAYS},
        {"id": "b", "status": "done"},                                   # written before reasons: merged
        {"id": "c", "status": "backlog"},
        {"id": "d", "status": "done", "done_reason": finished.BY_HAND},  # its mark is gone
        {"id": "e", "status": "done", "done_reason": finished.MERGED},   # vetoed since
        {"id": "f", "status": "done", "done_reason": finished.BY_HAND},  # still marked
    ])
    finished.veto(tmp_git, "e", "pat")
    finished.mark(tmp_git, "f", "pat")
    finished.mark(tmp_git, "g", "pat")  # marked after the last assessment
    assert finished.recorded(tmp_git) == {
        "a": finished.SPEC_SAYS, "b": finished.MERGED, "f": finished.BY_HAND, "g": finished.BY_HAND,
    }


def test_recorded_keeps_hand_marked_entries_when_the_marks_file_is_unreadable(tmp_git):
    _queue(tmp_git, [{"id": "a", "status": "done", "done_reason": finished.BY_HAND},
                     {"id": "b", "status": "done", "done_reason": finished.MERGED}])
    _torn(runs_root(tmp_git) / finished.MARKS)
    assert finished.recorded(tmp_git) == {"a": finished.BY_HAND, "b": finished.MERGED}


def test_recorded_reads_a_broken_or_missing_queue_file_as_empty(tmp_git):
    assert finished.recorded(tmp_git) == {}
    path = runs_root(tmp_git) / finished.QUEUE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    assert finished.recorded(tmp_git) == {}
    finished.mark(tmp_git, "a", "cli")
    assert finished.recorded(tmp_git) == {"a": finished.BY_HAND}
