"""The independence rule: which tasks wait while one is parked (agentflow/park.py)."""

from agentflow.park import blast_radius, blocks, held_tasks, radius_line, ranges
from agentflow.sources import parse_plan_text


def _plan(*tasks: tuple[int, str, str]) -> list:
    """`(n, depends, extra lines)` -> parsed tasks."""
    parts = ["# Plan\n"]
    for n, depends, extra in tasks:
        parts.append(f"### Task {n}: Thing {n}\n\n**Depends on:** {depends}\n\n{extra}\n- [ ] **Step 1: Go**\n")
    return parse_plan_text("\n".join(parts))


def test_a_dependent_waits_transitively():
    tasks = _plan((1, "none", ""), (2, "none", ""), (3, "Task 2", ""), (4, "Task 3", ""), (5, "none", ""))
    assert held_tasks(tasks, {2}, set()) == {2, 3, 4}


def test_a_task_naming_a_held_tasks_file_waits():
    tasks = _plan((1, "none", ""), (2, "none", "- Modify: `src/a.py`\n"), (3, "none", ""),
                  (5, "none", "- Modify: `src/a.py`\n"))
    assert held_tasks(tasks, {2}, set()) == {2, 5}


def test_a_task_naming_no_file_shares_none():
    tasks = _plan((1, "none", ""), (2, "none", "- Modify: `src/a.py`\n"), (3, "none", ""))
    assert held_tasks(tasks, {2}, set()) == {2}


def test_done_tasks_never_wait():
    tasks = _plan((2, "none", ""), (3, "none", ""), (4, "Task 2, Task 3", ""))
    assert held_tasks(tasks, {2}, {3}) == {2, 4}


def test_blocks_leaves_the_task_out():
    tasks = _plan((1, "none", ""), (2, "Task 1", ""), (3, "none", ""))
    assert blocks(tasks, 1, set()) == [2]


def test_the_blast_radius_reads_like_the_spec():
    tasks = _plan(*[(n, "Task 9" if n == 14 else "none", "") for n in range(1, 24)])
    radius = blast_radius(tasks, set(), 9, set(range(1, 9)))
    assert radius == {"blocks": [9, 14], "continue": [10, 11, 12, 13, *range(15, 24)]}
    assert radius_line(radius) == "blocks tasks 9, 14; tasks 10–13, 15–23 continue"
    assert radius_line({"blocks": [9], "continue": [10]}) == "blocks task 9; task 10 continues"
    assert radius_line({"blocks": [9, 14], "continue": []}) == "blocks tasks 9, 14; no other task can run"


def test_ranges_match_the_dashboard():
    assert ranges([10, 11, 12, 20, 22, 24]) == "10–12, 20, 22, 24"
    assert ranges([3, 4]) == "3, 4"
    assert ranges([]) == ""
