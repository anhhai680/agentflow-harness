"""The dashboard's plan task board: its pure helpers, run with node and no DOM."""
from __future__ import annotations

import pytest
from conftest import NODE, js

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not on PATH")

T = lambda n, status, **kw: {"n": n, "status": status, "wave": kw.get("wave", 0), "blocks": kw.get("blocks", []),  # noqa: E731
                             "blocked_by": kw.get("blocked_by", []), "review": kw.get("review")}
PLAN = {"summary": {"total": 5, "committed": 1, "blocked": 1, "parked": 0, "running": 0, "failed": 0,
                    "waiting": 2, "independent": 1, "deferred": 0},
        "tasks": [T(1, "committed"), T(2, "blocked", wave=1, blocks=[3, 4]), T(3, "waiting", wave=2, blocked_by=[2]),
                  T(4, "waiting", wave=2, blocked_by=[2]), T(5, "independent", wave=1,
                  review={"findings": [{"text": "x"}]})]}


def test_the_summary_line_reads_like_the_spec():
    assert js(["plan.js"], "taskSummary(args)", PLAN) == \
        "5 tasks · 1 committed · 1 blocked · 2 waiting on it · 1 independent"


def test_ranges_compress_runs():
    assert js(["plan.js"], "ranges(args)", [10, 11, 12, 20, 22, 24]) == "10–12, 20, 22, 24"
    assert js(["plan.js"], "ranges(args)", [3, 4]) == "3, 4"
    assert js(["plan.js"], "ranges(args)", []) == ""


def test_grouping_and_filters():
    by_status = js(["plan.js"], "groupTasks(args, 'status').map(g => [g.label, g.tasks.map(t => t.n)])", PLAN["tasks"])
    assert [g[1] for g in by_status] == [[2], [3, 4], [5], [1]]
    by_wave = js(["plan.js"], "groupTasks(args, 'wave').map(g => [g.label, g.tasks.map(t => t.n)])",
                 PLAN["tasks"] + [T(6, "pending", wave=None)])
    assert by_wave == [["Wave 1", [1]], ["Wave 2", [2, 5]], ["Wave 3", [3, 4]], ["Cycle", [6]]]
    assert js(["plan.js"], "filterTasks(args, 'stopped').map(t => t.n)", PLAN["tasks"]) == [2]
    assert js(["plan.js"], "filterTasks(args, 'findings').map(t => t.n)", PLAN["tasks"]) == [5]


def test_the_plan_mode_follows_the_route():
    for sub, started, mode in [("tasks", False, "tasks"), ("task-12", False, "tasks"), ("document", True, "document"),
                               ("docs/plan.md", True, "document"), (None, True, "tasks"), (None, False, "document")]:
        assert js(["plan.js"], "planMode(args[0], args[1])", [sub, started]) == mode, (sub, started)


def test_a_task_row_reads_its_numbers():
    assert js(["plan.js"], "tries(args)", {"attempts": 1}) == "1 try"
    assert js(["plan.js"], "tries(args)", {"attempts": 3, "resolvers": 2}) == "3 tries · 2 resolvers"
    assert js(["plan.js"], "args.map(reviewText)", [None, {"skipped": True}, {"blocking": 2}, {"blocking": 0}]) == \
        ["—", "skipped", "2 blocking", "✓ 0"]
    assert js(["plan.js"], "statusText(args)", T(3, "waiting", blocked_by=[2])) == "○ waiting on 2"
