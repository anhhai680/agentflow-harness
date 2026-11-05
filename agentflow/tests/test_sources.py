"""Plan, capability-map and spec-status parsing, against fixtures and the real capability map."""

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
    task_size_warnings,
    tick_task,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
REPO = Path(__file__).resolve().parents[2]
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


def test_capability_map_real():
    nodes = capability_map(REPO / "docs" / "specs" / "CAPABILITY-MAP.md")
    assert nodes[0] == ModuleNode("platform", [])
    by_id = {n.id: n for n in nodes}
    assert by_id["settings"].deps == ["staff-auth"]
    assert by_id["staff-auth"].deps == ["platform"]
    assert [n.id for n in nodes[:3]] == ["platform", "staff-auth", "settings"]
    assert len(nodes) == 13


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


def test_spec_status_pending_later_on_the_same_line():
    # SPEC-agent and SPEC-conversations say "Status: approved ... Amended ..., pending approval" on one line
    assert spec_status(REPO / "docs" / "specs" / "SPEC-agent.md", APPROVED, PENDING) == "pending"
    assert spec_status(REPO / "docs" / "specs" / "SPEC-crm.md", APPROVED, PENDING) == "approved"
    assert spec_status(REPO / "docs" / "specs" / "SPEC-settings.md", APPROVED, PENDING) == "unknown"


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
