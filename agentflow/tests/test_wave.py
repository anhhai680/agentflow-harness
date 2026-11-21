"""Forming a wave from a plan's parallel groups: the checks, their order, the cap and the prediction for explain."""

from __future__ import annotations

import pytest
from agentflow.sources import PlanTask, parse_plan_text
from agentflow.wave import (
    REASONS,
    Demotion,
    candidates,
    candidates_text,
    depends_transitively,
    form_wave,
    group_candidates,
    predict_waves,
)


def task_block(n: int, deps: str = "none", group: str | None = "A", files: tuple[str, ...] = (), checked: bool = False):
    lines = [f"### Task {n}: Title {n}", "", f"**Depends on:** {deps}"]
    if group is not None:
        lines.append(f"**Parallel group:** {group}")
    lines += ["", "**Files:**"]
    lines += [f"- Create: `{path}`" for path in files]
    lines += ["", f"- [{'x' if checked else ' '}] step", ""]
    return "\n".join(lines)


def plan(*blocks: str) -> tuple[list[PlanTask], dict[int, PlanTask]]:
    tasks = parse_plan_text("\n".join(blocks))
    return tasks, {t.n: t for t in tasks}


def cfg(**over):
    base = {"committed": {1}, "serial_paths": [], "risky": (), "security": (), "model_of": {}, "cap": 8}
    return {**base, **over}


def wave_of(tasks, by_n, first=2, **over):
    at = by_n[first]
    kwargs = cfg(**over)
    kwargs["model_of"] = kwargs["model_of"] or {t.n: "m" for t in tasks}
    return form_wave(group_candidates(tasks, at, ()), by_n, **kwargs)


def test_reasons_are_the_documented_vocabulary():
    assert REASONS[:8] == (
        "no_files", "serial_path", "risk", "security", "depends_on_member", "dependency_open", "shared_files", "model"
    )


def test_two_independent_grouped_tasks_form_a_wave():
    tasks, by_n = plan(
        task_block(1, group=None, files=("src/base.py",)),
        task_block(2, "Task 1", files=("src/a.py",)),
        task_block(3, "Task 1", files=("src/b.py",)),
    )
    assert wave_of(tasks, by_n) == ([2, 3], [])


@pytest.mark.parametrize(
    "case, reason",
    [
        ("shares src/a.py", "shared_files"),
        ("depends on Task 2", "depends_on_member"),
        ("names no file", "no_files"),
        ("touches migrations/0001.sql", "serial_path"),
        ("depends on open Task 4", "dependency_open"),
    ],
)
def test_a_member_failing_the_check_is_demoted(case, reason):
    deps, files = "Task 1", ("src/b.py",)
    if case == "shares src/a.py":
        files = ("src/a.py",)
    elif case == "depends on Task 2":
        deps = "Task 2"
    elif case == "names no file":
        files = ()
    elif case == "touches migrations/0001.sql":
        files = ("migrations/0001.sql",)
    else:
        deps = "Task 1, Task 4"
    tasks, by_n = plan(
        task_block(2, "Task 1", files=("src/a.py",)),
        task_block(3, deps, files=files),
        task_block(4, "Task 1", group=None, files=("src/d.py",)),
    )
    assert wave_of(tasks, by_n, serial_paths=["migrations/**"]) == ([], [Demotion(3, reason)])


def test_a_dependency_through_another_task_is_a_dependency_on_the_member():
    tasks, by_n = plan(
        task_block(2, "Task 1", files=("src/a.py",)),
        task_block(3, "Task 2", group=None, files=("src/c.py",)),
        task_block(4, "Task 3", files=("src/d.py",)),
    )
    assert wave_of(tasks, by_n) == ([], [Demotion(4, "depends_on_member")])


def test_a_different_model_is_demoted():
    tasks, by_n = plan(task_block(2, "Task 1", files=("src/a.py",)), task_block(3, "Task 1", files=("src/b.py",)))
    assert wave_of(tasks, by_n, model_of={2: "opus", 3: "sonnet"}) == ([], [Demotion(3, "model")])


@pytest.mark.parametrize("flag, reason", [("risky", "risk"), ("security", "security")])
def test_a_risky_or_security_task_never_joins(flag, reason):
    tasks, by_n = plan(task_block(2, "Task 1", files=("src/a.py",)), task_block(3, "Task 1", files=("src/b.py",)))
    wave, demoted = wave_of(tasks, by_n, **{flag: {3}})
    assert (wave, demoted) == ([], [Demotion(3, reason)])


def test_the_first_reason_that_applies_wins():
    tasks, by_n = plan(
        task_block(2, "Task 1", files=("src/a.py",)),
        task_block(3, "Task 1, Task 4", files=("src/a.py",)),
    )
    assert wave_of(tasks, by_n, risky={3}) == ([], [Demotion(3, "risk")])


def test_the_cap_leaves_extras_for_the_next_wave():
    tasks, by_n = plan(*(task_block(n, "Task 1", files=(f"src/f{n}.py",)) for n in (2, 3, 4, 5)))
    assert wave_of(tasks, by_n, cap=3) == ([2, 3, 4], [])


def test_a_failing_first_candidate_forms_no_wave():
    tasks, by_n = plan(task_block(2, "Task 1"), task_block(3, "Task 1", files=("src/b.py",)))
    assert wave_of(tasks, by_n) == ([], [Demotion(2, "no_files")])


def test_a_wave_of_one_is_no_wave_and_keeps_the_demotions():
    tasks, by_n = plan(
        task_block(2, "Task 1", files=("src/a.py",)),
        task_block(3, "Task 1", files=("src/a.py",)),
        task_block(4, "Task 1"),
    )
    assert wave_of(tasks, by_n) == ([], [Demotion(3, "shared_files"), Demotion(4, "no_files")])


def test_group_candidates_are_the_open_later_tasks_of_the_group_minus_skipped():
    tasks, _ = plan(
        task_block(1, group="A", files=("a",)),
        task_block(2, group="B", files=("b",)),
        task_block(3, group="A", files=("c",)),
        task_block(4, group="A", files=("d",), checked=True),
        task_block(5, group="A", files=("e",)),
        task_block(6, group="A", files=("f",)),
    )
    assert [t.n for t in group_candidates(tasks, tasks[0], {5})] == [1, 3, 6]
    assert group_candidates(tasks, tasks[1], ()) == [tasks[1]]


def test_an_ungrouped_task_has_no_candidates():
    tasks, _ = plan(task_block(1, group=None, files=("a",)))
    assert group_candidates(tasks, tasks[0], ()) == []


def test_depends_transitively_follows_the_chain_and_survives_a_cycle():
    _, by_n = plan(task_block(1, "Task 3"), task_block(2, "Task 1"), task_block(3, "Task 2, Task 9"))
    assert depends_transitively(by_n, 3, 1)
    assert not depends_transitively(by_n, 1, 7)
    assert depends_transitively(by_n, 1, 2)


def test_candidates_are_ungrouped_independent_disjoint_pairs():
    tasks, _ = plan(
        task_block(1, group=None, files=("src/base.py",)),
        task_block(2, "Task 1", group="A", files=("src/g.py",)),
        task_block(3, "Task 1", group=None, files=("src/x.py",), checked=True),
        task_block(4, "Task 1", group=None, files=("src/chain.py",)),
        task_block(5, "Task 4", group=None, files=("src/after.py",)),
        task_block(6, "Task 1", group=None, files=("src/six.py",)),
        task_block(7, "Task 1", group=None, files=("src/seven.py",)),
        task_block(8, "Task 1", group=None, files=("src/six.py",)),
        task_block(9, "Task 1", group=None),
        task_block(10, "Task 1", group=None, files=("migrations/1.sql",)),
    )
    assert candidates(tasks, ["migrations/**"]) == [
        (4, 6), (4, 7), (4, 8), (5, 6), (5, 7), (5, 8), (6, 7), (7, 8)
    ]


def test_candidates_see_a_transitive_dependency():
    # Task 2 is done, so only (1, 3) could be a pair, and 3 reaches 1 only through 2.
    tasks, _ = plan(
        task_block(1, group=None, files=("src/a.py",)),
        task_block(2, "Task 1", group=None, files=("src/b.py",), checked=True),
        task_block(3, "Task 2", group=None, files=("src/c.py",)),
    )
    assert candidates(tasks, []) == []


def test_candidates_text_is_empty_for_no_pairs_and_one_line_per_pair():
    assert candidates_text([]) == ""
    text = candidates_text([(6, 7), (6, 8)])
    lines = text.splitlines()
    assert lines[0].startswith("The runner found tasks with no dependency and no shared file between them.")
    assert lines[0].endswith("before suggesting a group:")
    assert lines[1:] == [
        "- Tasks 6 and 7 could share a **Parallel group:** line: no dependency between them and no shared file.",
        "- Tasks 6 and 8 could share a **Parallel group:** line: no dependency between them and no shared file.",
    ]


def test_candidates_text_is_cut_to_ten_lines():
    pairs = [(1, n) for n in range(2, 20)]
    assert len(candidates_text(pairs).splitlines()) == 1 + 10


def test_predict_waves_for_explain():
    tasks, _ = plan(
        task_block(1, group=None, files=("src/base.py",)),
        task_block(2, "Task 1", files=("src/a.py",)),
        task_block(3, "Task 1", files=("src/b.py",)),
        task_block(4, "Task 1", group=None, files=("src/c.py",)),
    )
    assert predict_waves(tasks, 2, [], set()) == [[2, 3]]


def test_predict_waves_respects_the_cap_security_and_placed_tasks():
    tasks, _ = plan(
        task_block(1, group=None, files=("src/base.py",)),
        *(task_block(n, "Task 1", files=(f"src/f{n}.py",)) for n in (2, 3, 4, 5, 6)),
    )
    assert predict_waves(tasks, 2, [], set()) == [[2, 3], [4, 5]]
    assert predict_waves(tasks, 3, [], {3}) == [[2, 4, 5]]
    assert predict_waves(tasks, 8, [], set()) == [[2, 3, 4, 5, 6]]
