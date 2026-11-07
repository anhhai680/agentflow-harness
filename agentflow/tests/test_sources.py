"""Plan, capability-map and spec-status parsing, against fixtures and the `python` pack's capability map."""

from __future__ import annotations

from pathlib import Path

import pytest
from agentflow.sources import (
    ModuleNode,
    PlanTask,
    capability_map,
    find_plan,
    parse_plan,
    spec_status,
    forbidden_task_files,
    task_files,
    task_size_warnings,
    tick_task,
)

from conftest import PYTHON_PACK

FIXTURES = Path(__file__).resolve().parent / "fixtures"
APPROVED = r"\*\*Status: approved"
PENDING = "pending approval"


def test_parse_plan_tasks_titles_and_checked(tmp_path):
    tasks = parse_plan(FIXTURES / "plan-sample.md")
    assert [(t.n, t.title) for t in tasks] == [
        (1, "First thing"),
        (2, "Second thing"),
        (3, "Third thing"),
        (4, "Fourth thing"),
    ]
    assert [t.checked for t in tasks] == [True, False, False, False]
    assert all(isinstance(t, PlanTask) for t in tasks)


def test_parse_plan_ignores_fenced_headings_and_boxes():
    tasks = parse_plan(FIXTURES / "plan-sample.md")
    assert 99 not in [t.n for t in tasks]
    assert "Not a heading" in tasks[0].text  # still part of task 1's text
    assert tasks[0].checked  # the fenced `- [ ]` did not count


def test_parse_plan_text_stops_at_next_level_two_heading():
    last = parse_plan(FIXTURES / "plan-sample.md")[-1]
    assert "Solo" in last.text
    assert "after the tasks" not in last.text
    assert not last.text.rstrip().endswith("---")


DEFERRED_PLAN = """# Plan

### Task 1: Built

- [x] **Step 1: done**

### Task 2: Waits on another module

> **Deferred (SPEC-demo C27, decided 2026-11-03).** Its endpoint belongs to `conversations`, not merged yet.

**Depends on:** Task 1

### Task 3: Shows the marker only as an example

```markdown
> **Deferred (example).**
```

- [ ] **Step 1: still to do**
"""


def test_parse_plan_marks_a_deferred_task_closed_without_ticking_it(tmp_path):
    plan = tmp_path / "plan.md"
    plan.write_text(DEFERRED_PLAN, encoding="utf-8")
    tasks = parse_plan(plan)
    assert [t.deferred for t in tasks] == [False, True, False]
    assert [t.checked for t in tasks] == [True, False, False]
    assert [t.open for t in tasks] == [False, False, True]  # a fenced marker defers nothing


def test_depends_on_defaults_and_overrides():
    tasks = parse_plan(FIXTURES / "plan-sample.md")
    assert [t.depends_on for t in tasks] == [[], [1], [1, 2], []]


def test_tick_task_flips_only_that_tasks_boxes(tmp_path):
    plan = tmp_path / "plan.md"
    plan.write_text((FIXTURES / "plan-sample.md").read_text(encoding="utf-8"), encoding="utf-8")
    tick_task(plan, 3)
    after = parse_plan(plan)
    assert [t.checked for t in after] == [True, False, True, False]
    text = plan.read_text(encoding="utf-8")
    assert "- [ ] not a task box" in text  # outside any task
    assert "- [ ] not a box" in text  # inside a fence
    assert "- [ ] after the tasks" in text
    assert "- [ ] **Step 2: Two**" in text  # task 2 untouched


def test_tick_task_keeps_every_other_byte(tmp_path):
    plan = tmp_path / "plan.md"
    original = (FIXTURES / "plan-sample.md").read_text(encoding="utf-8")
    plan.write_text(original, encoding="utf-8")
    tick_task(plan, 4)
    assert plan.read_text(encoding="utf-8") == original.replace("- [ ] **Step 1: Solo**", "- [x] **Step 1: Solo**")


def test_tick_task_unknown_number_raises(tmp_path):
    plan = tmp_path / "plan.md"
    plan.write_text("### Task 1: A\n\n- [ ] x\n", encoding="utf-8")
    with pytest.raises(ValueError, match="no Task 7"):
        tick_task(plan, 7)


def test_task_without_boxes_is_not_checked(tmp_path):
    plan = tmp_path / "plan.md"
    plan.write_text("### Task 1: A\n\nJust prose.\n", encoding="utf-8")
    assert parse_plan(plan)[0].checked is False


def test_find_plan_newest_matching(tmp_path):
    for name in (
        "2026-10-01-crm.md",
        "2026-10-20-crm-retry.md",
        "2026-10-15-crm-first.md",
        "2026-10-25-crm2.md",
        "2026-10-26-other.md",
        "notes.md",
    ):
        (tmp_path / name).write_text("x", encoding="utf-8")
    assert find_plan(tmp_path, "crm") == tmp_path / "2026-10-20-crm-retry.md"
    assert find_plan(tmp_path, "nothing") is None
    assert find_plan(tmp_path / "missing", "crm") is None


def test_find_plan_module_name_is_literal(tmp_path):
    (tmp_path / "2026-10-01-staff-auth.md").write_text("x", encoding="utf-8")
    assert find_plan(tmp_path, "staff.auth") is None
    assert find_plan(tmp_path, "staff-auth") is not None


def test_task_size_warnings(tmp_path):
    steps = "\n".join(f"- [ ] **Step {i}: s**" for i in range(1, 10))
    files = ", ".join(f"`f{i}.py`" for i in range(9))
    plan = tmp_path / "plan.md"
    plan.write_text(
        f"### Task 1: Big\n\n**Files:**\n- Create: {files}\n\n{steps}\n\n### Task 2: Small\n\n"
        "**Files:**\n- Create: `a.py`\n\n- [ ] **Step 1: x**\n",
        encoding="utf-8",
    )
    big, small = parse_plan(plan)
    warnings = task_size_warnings(big)
    assert any("9 checkbox steps" in w for w in warnings)
    assert any("9 files" in w for w in warnings)
    assert task_size_warnings(small) == []


def test_capability_map_fixture():
    nodes = capability_map(FIXTURES / "capability-sample.md")
    assert nodes == [
        ModuleNode("alpha", []),
        ModuleNode("beta", ["alpha"]),
        ModuleNode("gamma", ["alpha", "beta"]),
    ]


def test_capability_map_of_the_python_pack():
    assert capability_map(PYTHON_PACK / "docs" / "specs" / "CAPABILITY-MAP.md") == [ModuleNode("greet", [])]


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("SPEC-demo.md", "unknown"),
        ("SPEC-amended.md", "pending"),
        ("SPEC-reapproved.md", "approved"),
        ("SPEC-nostatus.md", "unknown"),
    ],
)
def test_spec_status_last_match_wins(name, expected):
    assert spec_status(FIXTURES / "specs" / name, APPROVED, PENDING) == expected


def test_spec_status_pending_later_on_the_same_line(tmp_path):
    amended = tmp_path / "SPEC-amended-inline.md"
    amended.write_text("# Spec\n\n**Status: approved** (2026-10-01). Amended 2026-10-20, pending approval.\n",
                       encoding="utf-8")
    assert spec_status(amended, APPROVED, PENDING) == "pending"
    assert spec_status(PYTHON_PACK / "docs" / "specs" / "SPEC-greet.md", APPROVED, PENDING) == "approved"
    assert spec_status(FIXTURES / "specs" / "SPEC-nostatus.md", APPROVED, PENDING) == "unknown"


# --------------------------------------------------------------------------- batches of small tasks


def batch_task(n: int, files: int = 1, deps: list[int] | None = None, checked: bool = False):
    from agentflow.sources import PlanTask

    lines = "\n".join(f"- Create: `src/f{n}_{i}.py`" for i in range(files))
    return PlanTask(n=n, title=f"T{n}", text=f"**Files:**\n{lines}\n", checked=checked,
                    depends_on=[n - 1] if deps is None and n > 1 else (deps or []))


def test_task_size_counts_steps_and_files():
    from agentflow.sources import task_size

    assert task_size(batch_task(1, files=3)) == (0, 3)


def test_neighbouring_small_tasks_form_batches_up_to_the_cap():
    from agentflow.sources import plan_batches

    tasks = [batch_task(n) for n in range(1, 6)]
    assert plan_batches(tasks, 3, 3) == [[1, 2, 3], [4, 5]]


def test_a_big_or_ticked_task_breaks_a_batch_and_singles_are_dropped():
    from agentflow.sources import plan_batches

    tasks = [batch_task(1), batch_task(2), batch_task(3, files=9), batch_task(4), batch_task(5, checked=True)]
    assert plan_batches(tasks, 3, 3) == [[1, 2]]


def test_a_task_joins_only_when_its_dependencies_are_in_the_batch_or_before_it():
    from agentflow.sources import plan_batches

    # 3 needs 1 (before the batch) and 2 (in it): it joins. 4 needs 5, after the batch: it starts the next one.
    tasks = [batch_task(1, deps=[]), batch_task(2, deps=[1]), batch_task(3, deps=[1, 2]), batch_task(4, deps=[5]),
             batch_task(5, deps=[])]
    assert plan_batches(tasks, 3, 3) == [[1, 2, 3], [4, 5]]
    assert plan_batches(tasks, 4, 3) == [[1, 2, 3], [4, 5]]  # 4 may not join [1, 2, 3]: 5 is not in it


def test_forbidden_task_files_reads_only_file_lines_outside_fences():
    task = PlanTask(1, "T", "### Task 1: T\n\n- Create: `eval/baseline.json`, `src/a.py`\n- Test: `tests/a.py`\n"
                    "```\n- Create: `eval/fenced.json`\n```\nNote `eval/other.json` in prose.\n", False, [])
    assert task_files(task) == ["eval/baseline.json", "src/a.py", "tests/a.py"]
    assert forbidden_task_files(task, lambda path: path.startswith("eval/")) == ["eval/baseline.json"]


# --------------------------------------------------------------------------- origin text: cut_lines, says_done, plan_spec


def test_cut_lines_cuts_every_line():
    from agentflow import sources

    text = "a" * 600 + "\n" + "b" * 10 + "\n" + "c" * 513
    assert [len(line) for line in sources.cut_lines(text).splitlines()] == [512, 10, 512]
    assert sources.cut_lines("xyz\nw", limit=2) == "xy\nw"


def test_says_done_reads_the_header_and_refuses_a_bad_pattern():
    from agentflow import sources
    from agentflow.config import DEFAULT_DONE_STATUS

    assert sources.says_done("# S\n\n**Status:** done\n\n## A\n", DEFAULT_DONE_STATUS) is True
    assert sources.says_done("# S\n\n## A\n\nStatus: done\n", DEFAULT_DONE_STATUS) is False
    assert sources.says_done("# S\n\n**Status:** done\n", "(") is None


def test_says_done_never_counts_a_header_line_cut_at_line_max():
    from agentflow import sources
    from agentflow.config import DEFAULT_DONE_STATUS

    long_line = "Status:" + " " * 501 + "done-ish, not really"  # the cut at 512 falls right after `done`
    assert long_line[: sources.LINE_MAX].endswith("done")
    assert sources.says_done(f"# S\n\n{long_line}\n\n## A\n", DEFAULT_DONE_STATUS) is False
    assert sources.says_done(f"# S\n\n{long_line}\nStatus: done\n\n## A\n", DEFAULT_DONE_STATUS) is True


def test_plan_spec_is_the_basename_of_the_header_spec_path():
    from agentflow import sources

    assert sources.plan_spec("# P\n\n**Spec:** `docs/specs/SPEC-crm.md`, section A\n\n### Task 1: x\n") == "SPEC-crm.md"
    assert sources.plan_spec("# P\n\n**Spec:** none\n\n### Task 1: x\n") is None
    assert sources.plan_spec("# P\n\n```\n**Spec:** `a/SPEC-x.md`\n```\n") is None  # a fenced example
    assert sources.plan_spec("# P\n\n### Task 1: x\n\n**Spec:** `a/SPEC-x.md`\n") is None  # below the header
    assert sources.plan_spec("# P\n\n**Spec:** " + "x" * 600 + " `a/SPEC-x.md`\n") is None  # past the cut
