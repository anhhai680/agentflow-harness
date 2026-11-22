"""Forming a wave from a plan's parallel groups: the checks, their order, the cap and the prediction for explain."""

from __future__ import annotations

import pytest
from agentflow.sources import PlanTask, parse_plan_text
from agentflow.waves import (
    REASONS,
    TASK_DESCRIPTION,
    Attribution,
    Demotion,
    Report,
    ReviewReport,
    allowed_dirs,
    attribute,
    candidates,
    candidates_text,
    declared_path,
    declared_paths,
    depends_transitively,
    dispatch_count_violations,
    dispatch_violations,
    form_wave,
    group_candidates,
    is_test_path,
    parse_reports,
    parse_review_reports,
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


@pytest.mark.parametrize(("entry", "path"), [
    ("src/core.py", "src/core.py"),
    ("src/core.py:123-145", "src/core.py"),  # the writing-plans skill's `Modify:` form
    ("src/core.py:12", "src/core.py"),
    ("src/core.py:3,9-11", "src/core.py"),
    ("./src/core.py:1-5", "src/core.py"),
    ("tests/test_core.py::test_name", "tests/test_core.py::test_name"),  # not a line suffix: left as it is
    ("src/core.py (new)", None),
    ("run pytest -q", None),
    ("fn()", None),
])
def test_declared_path_cuts_line_suffixes_and_drops_non_paths(entry, path):
    assert declared_path(entry) == path


def test_declared_paths_read_the_writing_plans_form_once_each():
    tasks, _ = plan(task_block(2, files=("src/core.py:1-5", "src/core.py:10-20", "./tests/test_core.py", "a b.py")))
    assert declared_paths(tasks[0]) == ["src/core.py", "tests/test_core.py"]


def test_line_ranges_of_one_file_are_shared_files():
    tasks, by_n = plan(task_block(2, "Task 1", files=("src/core.py:1-5",)),
                       task_block(3, "Task 1", files=("src/core.py:10-20",)))
    assert wave_of(tasks, by_n) == ([], [Demotion(3, "shared_files")])
    assert candidates(plan(task_block(2, group=None, files=("src/core.py:1-5",)),
                           task_block(3, group=None, files=("src/core.py:10-20",)))[0], []) == []


def test_a_line_range_on_a_serial_path_is_serial():
    tasks, by_n = plan(task_block(2, "Task 1", files=("src/a.py",)),
                       task_block(3, "Task 1", files=("migrations/0001.sql:1-9",)))
    assert wave_of(tasks, by_n, serial_paths=["migrations/**"]) == ([], [Demotion(3, "serial_path")])


def test_attribution_reads_declared_line_ranges_and_reported_ones():
    tasks, _ = plan(task_block(2, files=("src/a.py:1-5", "tests/test_a.py:3")),
                    task_block(3, files=("./src/b.py:2-9",)))
    reports = {3: Report(3, "done", "", ("src/z.py:4",), ())}
    att = attribute(["src/a.py", "src/b.py", "src/z.py", "tests/test_a2.py"], tasks, reports, "p.md")
    assert att.owners == {2: ["src/a.py", "tests/test_a2.py"], 3: ["src/b.py"]}
    assert att.strays == {"src/z.py": (3,)}
    assert allowed_dirs(tasks[0]) == {"tests"}


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


def rep(n, *files, status="done"):
    return {"n": n, "status": status, "summary": "s", "files": list(files), "tests_run": []}


def test_reports_are_kept_for_members_only_and_read_into_tuples():
    got = parse_reports(
        [{"n": 2, "status": "done", "summary": "ok", "files": ["src/a.py"], "tests_run": ["pytest"]}, rep(9)],
        {2, 3},
    )
    assert got == {2: Report(2, "done", "ok", ("src/a.py",), ("pytest",))}


def test_missing_files_and_tests_run_mean_empty():
    got = parse_reports([{"n": 2, "status": "blocked", "summary": ""}], {2})
    assert got == {2: Report(2, "blocked", "", (), ())}


def test_reports_fail_closed():
    assert parse_reports([rep(2), rep(2), rep(9)], {2, 3}) == {}
    assert parse_reports([rep(2), rep(2), rep(3)], {2, 3}).keys() == {3}
    assert parse_reports("x", {2}) == {}
    assert parse_reports({"n": 2}, {2}) == {}
    assert parse_reports(None, {2}) == {}


@pytest.mark.parametrize(
    "entry",
    [
        "task 2",
        {"n": "2", "status": "done", "summary": ""},
        {"n": True, "status": "done", "summary": ""},
        {"n": 2, "status": "finished", "summary": ""},
        {"n": 2, "status": "done", "summary": None},
        {"n": 2, "status": "done", "summary": "", "files": "src/a.py"},
        {"n": 2, "status": "done", "summary": "", "files": [1]},
        {"n": 2, "status": "done", "summary": "", "tests_run": [None]},
    ],
)
def test_a_malformed_report_is_dropped(entry):
    assert parse_reports([entry], {1, 2}) == {}


def test_a_malformed_duplicate_still_voids_the_member():
    assert parse_reports([rep(2), {"n": 2, "status": "bad", "summary": ""}], {2}) == {}


def test_review_reports_read_findings_and_fail_closed():
    findings = [{"severity": "high", "text": "x"}]
    assert parse_review_reports([{"n": 2, "findings": findings}, {"n": 3}], {2, 3}) == {
        2: ReviewReport(findings, ()), 3: ReviewReport([], ())}
    assert parse_review_reports([{"n": 2, "findings": findings}, {"n": 2, "findings": []}], {2}) == {}
    assert parse_review_reports([{"n": 2, "findings": "none"}], {2}) == {}
    assert parse_review_reports([{"n": 2, "findings": ["x"]}], {2}) == {}
    assert parse_review_reports([{"n": 9, "findings": []}], {2}) == {}
    assert parse_review_reports("x", {2}) == {}


def test_review_reports_keep_risks_and_fail_closed_on_them():
    got = parse_review_reports([{"n": 2, "findings": [], "risks": ["the cache path", "the retry loop"]}], {2})
    assert got == {2: ReviewReport([], ("the cache path", "the retry loop"))}
    assert parse_review_reports([{"n": 2, "risks": "the cache path"}], {2}) == {}  # not a list
    assert parse_review_reports([{"n": 2, "risks": ["ok", 3]}], {2}) == {}  # not strings only
    assert parse_review_reports([{"n": 2, "risks": ["ok"]}, {"n": 3, "risks": [{}]}], {2, 3}) == {
        2: ReviewReport([], ("ok",))}


@pytest.mark.parametrize(
    "path, expected",
    [
        ("tests/test_a.py", True),
        ("src/a_test.go", True),
        ("src/a.test.ts", True),
        ("src/a.spec.ts", True),
        ("src/a.py", False),
        ("src/contest.py", False),
        ("tests/conftest.py", False),
        ("docs/test/notes.md", False),
    ],
)
def test_is_test_path(path, expected):
    assert is_test_path(path) is expected


SHARED = ("src/b.py", "tests/test_b.py")


def two_tasks(a_files=("src/a.py", "tests/test_a.py"), b_files=("src/b.py", "tests_b/test_b.py")):
    tasks, _ = plan(task_block(2, files=a_files), task_block(3, files=b_files))
    return tasks


def test_allowed_dirs_are_the_directories_of_declared_test_paths():
    t2 = two_tasks()[0]
    assert allowed_dirs(t2) == {"tests"}


def test_attribution_by_declared_files_and_test_dirs():
    att = attribute(
        ["src/a.py", "tests/test_a_extra.py", "README.md"], two_tasks(), {}, "docs/plans/p.md"
    )
    assert isinstance(att, Attribution)
    assert att.owners == {2: ["src/a.py", "tests/test_a_extra.py"], 3: []}
    assert att.strays == {"README.md": ()}


def test_a_member_with_no_paths_gets_an_empty_list_and_order_follows_changed():
    att = attribute(["src/b.py", "src/a.py"], two_tasks(), {}, "p.md")
    assert att.owners == {2: ["src/a.py"], 3: ["src/b.py"]}
    assert list(att.owners) == [2, 3]


def test_a_stray_is_charged_to_the_member_that_reports_it():
    reports = {3: Report(3, "done", "", ("src/z.py",), ())}
    assert attribute(["src/z.py"], two_tasks(), reports, "p.md").strays == {"src/z.py": (3,)}


def test_a_stray_reported_by_several_members_is_charged_to_each_of_them():
    reports = {n: Report(n, "done", "", ("src/z.py",), ()) for n in (2, 3)}
    assert attribute(["src/z.py"], two_tasks(), reports, "p.md").strays == {"src/z.py": (2, 3)}


def test_a_test_file_in_a_shared_dir_goes_to_the_member_that_reports_it():
    tasks = two_tasks(b_files=SHARED)
    reports = {3: Report(3, "done", "", ("tests/test_new.py",), ())}
    att = attribute(["tests/test_new.py"], tasks, reports, "p.md")
    assert att.owners == {2: [], 3: ["tests/test_new.py"]}
    assert att.strays == {}


def test_a_test_file_in_a_shared_dir_nobody_reports_is_a_stray():
    att = attribute(["tests/test_new.py"], two_tasks(b_files=SHARED), {}, "p.md")
    assert att.owners == {2: [], 3: []}
    assert att.strays == {"tests/test_new.py": ()}


def test_a_test_file_in_a_shared_dir_reported_by_two_members_is_a_stray_charged_to_both():
    reports = {n: Report(n, "done", "", ("tests/test_new.py",), ()) for n in (2, 3)}
    att = attribute(["tests/test_new.py"], two_tasks(b_files=SHARED), reports, "p.md")
    assert att.owners == {2: [], 3: []}
    assert att.strays == {"tests/test_new.py": (2, 3)}


def test_a_non_test_file_in_a_members_test_dir_is_a_stray():
    att = attribute(["tests/data.json"], two_tasks(), {}, "p.md")
    assert att.strays == {"tests/data.json": ()}


def test_a_file_declared_by_two_members_falls_through_to_the_later_rules():
    tasks = two_tasks(a_files=("src/s.py", "tests/test_a.py"), b_files=("src/s.py", "tests/test_b.py"))
    att = attribute(["src/s.py"], tasks, {}, "p.md")
    assert att.owners == {2: [], 3: []}
    assert att.strays == {"src/s.py": ()}


def test_the_plan_file_is_always_a_stray_even_when_a_member_declares_it():
    tasks = two_tasks(a_files=("docs/plans/p.md", "tests/test_a.py"))
    assert attribute(["docs/plans/p.md"], tasks, {}, "docs/plans/p.md").strays == {"docs/plans/p.md": ()}
    reports = {3: Report(3, "done", "", ("docs/plans/p.md",), ())}
    att = attribute(["docs/plans/p.md"], tasks, reports, "docs/plans/p.md")
    assert att.owners == {2: [], 3: []}
    assert att.strays == {"docs/plans/p.md": (3,)}


@pytest.mark.parametrize(
    "description, ok",
    [
        ("Task 2: add a", True),
        ("  Review Task 3: add b", True),
        ("task 2: x", False),
        ("Task 5: x", False),
        ("Review Task 5: x", False),
        ("Task 2 add a", False),
        ("Implement Task 2: x", False),
        ("", False),
    ],
)
def test_dispatch_descriptions(description, ok):
    got = dispatch_violations([{"description": description, "model": "inherit"}], {2, 3}, "sonnet")
    assert (got == []) is ok
    if not ok:
        assert len(got) == 1 and repr(description)[1:-1].strip() in got[0]


def test_task_description_pattern_captures_the_number():
    assert TASK_DESCRIPTION.match("Review Task 12: x").group(1) == "12"


def test_dispatch_models():
    ok = {"description": "Task 2: x"}
    assert dispatch_violations([ok, {**ok, "model": "inherit"}, {**ok, "model": "sonnet"}], {2}, "sonnet") == []
    got = dispatch_violations([{**ok, "model": "opus"}], {2}, "sonnet")
    assert len(got) == 1 and "opus" in got[0]


def test_dispatch_entry_without_a_string_description_is_a_violation():
    got = dispatch_violations([{"model": "inherit"}, {"description": 4}], {2}, "sonnet")
    assert len(got) == 2


def test_dispatch_names_one_line_per_offending_entry():
    entries = [
        {"description": "Task 2: x", "model": "inherit"},
        {"description": "Task 9: x", "model": "opus"},
        {"description": "Task 3: x", "model": "haiku"},
    ]
    got = dispatch_violations(entries, {2, 3}, "sonnet")
    assert len(got) == 3
    assert any("Task 9" in line for line in got) and any("haiku" in line for line in got)
    assert dispatch_violations([], {2}, "sonnet") == []


@pytest.mark.parametrize(("subagent_type", "ok"), [(None, True), ("general-purpose", True), ("Explore", False),
                                                   ("security-auditor", False), ("", False)])
def test_dispatch_agent_types(subagent_type, ok):
    # an omitted `model` on a custom agent type runs that type's own model while the stream says `inherit`
    entry = {"description": "Task 2: x", "model": "inherit"}
    if subagent_type is not None:
        entry["subagent_type"] = subagent_type
    got = dispatch_violations([entry], {2}, "sonnet")
    assert (got == []) is ok
    if not ok:
        assert len(got) == 1 and repr(subagent_type) in got[0]


def test_dispatch_a_subagent_that_errored_is_a_violation():
    # a refused `Task` call looks like a good one unless its result says it failed
    entries = [{"description": "Task 2: x", "ok": True}, {"description": "Task 3: x", "ok": False},
               {"description": "Task 3: y"}]  # no outcome recorded (an older engine's row): not counted against it
    got = dispatch_violations(entries, {2, 3}, "sonnet")
    assert len(got) == 1 and "Task 3: x" in got[0]


def test_dispatch_a_subagent_that_did_not_finish_is_a_violation():
    entries = [{"description": "Task 2: x", "done": True}, {"description": "Task 3: x", "done": False},
               {"description": "Task 3: y"}]  # no `done` recorded (an older engine's row): not counted against it
    assert dispatch_violations(entries, {2, 3}, "sonnet") == ["sub-agent did not finish: 'Task 3: x'"]


def test_dispatch_a_subagent_started_in_the_background_is_a_violation():
    # its call returns before it finishes, so `done` and `ok` say nothing about its work
    entries = [{"description": "Task 2: x", "background": False}, {"description": "Task 3: x", "background": True},
               {"description": "Task 3: y"}]  # no `background` recorded (an older engine's row): not counted against it
    assert dispatch_violations(entries, {2, 3}, "sonnet") == ["sub-agent was started in the background: 'Task 3: x'"]


def test_dispatch_count_wants_exactly_one_per_member():
    one_each = [{"description": "Task 2: a"}, {"description": "Task 3: b"}]
    assert dispatch_count_violations(one_each, {2, 3}, review=False) == []
    assert dispatch_count_violations([], {2, 3}, review=False) == [
        "no sub-agent was started for Task 2", "no sub-agent was started for Task 3"]
    assert dispatch_count_violations([*one_each, {"description": "Task 2: again"}], {2, 3}, review=False) == [
        "2 sub-agents were started for Task 2, not one"]


def test_dispatch_count_reads_builders_and_reviewers_apart():
    reviewers = [{"description": "Review Task 2: a"}, {"description": "Review Task 3: b"}]
    assert dispatch_count_violations(reviewers, {2, 3}, review=True) == []
    assert dispatch_count_violations(reviewers, {2}, review=False) == ["no sub-agent was started for Task 2"]
    assert dispatch_count_violations([{"description": "Task 2: a"}], {2}, review=True) == [
        "no sub-agent was started for Review Task 2"]
    assert dispatch_count_violations([{"description": 4}, {}], {2}, review=True) == [
        "no sub-agent was started for Review Task 2"]
