"""`workspace_view`: the merged and the tagged Inbox and Agents, with the reads stubbed (no run needed)."""

from __future__ import annotations

import copy
from datetime import UTC, datetime
from pathlib import Path

from agentflow import repo_context, workspace_view
from agentflow.repo_context import RepoContext

NOW = datetime(2026, 11, 27, 11, 0, tzinfo=UTC)


def ctx_of(name: str, state: str = "live", error: str | None = None) -> RepoContext:
    return RepoContext(f"id-{name}", name, Path(f"/nowhere/{name}"), state=state, error=error)  # type: ignore[arg-type]


def item(run: str, since: str, key: str = "plan") -> dict:
    return {"run_id": run, "key": key, "since": since, "group": "approval"}


def inbox(items=(), watch=(), recent=(), **counts) -> dict:
    base = {"approval": len(items), "question": 0, "stop": 0, "watch": len(watch)}
    return {"items": list(items), "watch": list(watch), "recent": list(recent), "counts": {**base, **counts}}


def reader(views: dict, calls: list | None = None):
    """A stub `read` that answers from `views`, keyed by the repository's folder name; a value that is an exception
    is raised."""
    def read(repo, cfg, now):
        if calls is not None:
            calls.append(repo)
        view = views[repo.name]
        if isinstance(view, Exception):
            raise view
        return copy.deepcopy(view)
    return read


def test_merged_items_sort_oldest_first_across_repositories_and_carry_their_repo():
    a, b = ctx_of("a"), ctx_of("b")
    read = reader({"a": inbox([item("r1", "2026-11-27T10:05:00+00:00")]),
                   "b": inbox([item("r2", "2026-11-27T10:01:00+00:00")], stop=2)})
    view = workspace_view.merged_inbox([a, b], NOW, read)
    assert [(i["run_id"], i["repo"]) for i in view["items"]] == [("r2", b.id), ("r1", a.id)]
    assert view["counts"] == {"approval": 2, "question": 0, "stop": 2, "watch": 0}
    assert view["errors"] == []


def test_a_run_id_in_two_repositories_stays_two_items():
    a, b = ctx_of("a"), ctx_of("b")
    row = item("20261127-1200-same", "2026-11-27T10:00:00+00:00")
    view = workspace_view.merged_inbox([a, b], NOW, reader({"a": inbox([row]), "b": inbox([row])}))
    assert [(i["run_id"], i["key"], i["repo"]) for i in view["items"]] == [
        ("20261127-1200-same", "plan", a.id), ("20261127-1200-same", "plan", b.id)]


def test_recent_is_newest_first_and_watch_keeps_workspace_order():
    a, b = ctx_of("a"), ctx_of("b")
    read = reader({
        "a": inbox(watch=[{"run_id": "wa"}], recent=[{"run_id": "ra", "ts": "2026-11-27T10:00:00+00:00"}]),
        "b": inbox(watch=[{"run_id": "wb"}], recent=[{"run_id": "rb", "ts": "2026-11-27T10:10:00+00:00"}])})
    view = workspace_view.merged_inbox([a, b], NOW, read)
    assert [r["run_id"] for r in view["watch"]] == ["wa", "wb"]
    assert [(r["run_id"], r["repo"]) for r in view["recent"]] == [("rb", b.id), ("ra", a.id)]
    assert view["counts"]["watch"] == 2


def test_a_repository_whose_read_raises_is_an_error_line_and_the_others_still_show():
    a, b = ctx_of("a"), ctx_of("b")
    read = reader({"a": ValueError("bad events.jsonl"), "b": inbox([item("r2", "2026-11-27T10:01:00+00:00")])})
    view = workspace_view.merged_inbox([a, b], NOW, read)
    assert view["errors"] == [{"repo": a.id, "error": "bad events.jsonl"}]
    assert [i["run_id"] for i in view["items"]] == ["r2"]
    long = reader({"a": ValueError("x\n" * 400), "b": inbox()})
    (error,) = workspace_view.merged_inbox([a, b], NOW, long)["errors"]
    assert len(error["error"]) == repo_context.ERROR_MAX and "\n" not in error["error"]
    agents = workspace_view.merged_agents([a], NOW, reader({"a": RuntimeError("boom")}))
    assert agents["errors"] == [{"repo": a.id, "error": "boom"}] and agents["live"] == []


def test_only_usable_contexts_are_read():
    calls: list = []
    live, bare, broken = ctx_of("a"), ctx_of("b", "not_set_up"), ctx_of("c", "error", "its pack could not be loaded")
    view = workspace_view.merged_inbox([live, bare, broken], NOW, reader({"a": inbox()}, calls))
    assert [c.name for c in calls] == ["a"] and view["errors"] == []
    calls.clear()
    agents = workspace_view.merged_agents([bare, broken], NOW, reader({}, calls))
    assert calls == [] and agents["live"] == [] and agents["errors"] == []


def row_of(agent: str, model: str) -> dict:
    return {"agent": agent, "model": model, "run_id": "r"}


def agents(live=(), exited=(), exited_min=10.0, idle_min=5.0) -> dict:
    return {"live": list(live), "exited": list(exited), "counts": {"by_agent": {}, "by_model": {}},
            "idle_warn_min": idle_min, "show_exited_min": exited_min, "generated_at": "then"}


def test_merged_agents_recount_and_take_the_widest_window():
    a, b = ctx_of("a"), ctx_of("b")
    read = reader({"a": agents([row_of("claude", "sonnet")], exited_min=10.0, idle_min=5.0),
                   "b": agents([row_of("codex", "gpt")], [row_of("codex", "gpt")], exited_min=30.0, idle_min=2.0)})
    view = workspace_view.merged_agents([a, b], NOW, read)
    assert view["counts"] == {"by_agent": {"claude": 1, "codex": 1}, "by_model": {"sonnet": 1, "gpt": 1}}
    assert view["show_exited_min"] == 30.0 and view["idle_warn_min"] == 5.0
    assert [r["repo"] for r in view["live"]] == [a.id, b.id] and [r["repo"] for r in view["exited"]] == [b.id]
    assert view["generated_at"] == NOW.isoformat(timespec="seconds") and view["errors"] == []


def test_tagging_leaves_the_view_otherwise_unchanged():
    view = inbox([item("r1", "2026-11-27T10:05:00+00:00")], watch=[{"run_id": "w"}], recent=[{"run_id": "x"}])
    before = copy.deepcopy(view)
    tagged = workspace_view.tagged_inbox(view, "a")
    assert view == before  # not mutated
    for key in ("items", "watch", "recent"):
        assert all(row["repo"] == "a" for row in tagged[key])
        assert [{k: v for k, v in row.items() if k != "repo"} for row in tagged[key]] == view[key]
    assert {k: v for k, v in tagged.items() if k not in ("items", "watch", "recent")} == {"counts": view["counts"]}
    shown = agents([row_of("claude", "sonnet")], [row_of("codex", "gpt")])
    kept = copy.deepcopy(shown)
    out = workspace_view.tagged_agents(shown, "b")
    assert shown == kept and out["live"][0]["repo"] == "b" and out["exited"][0]["repo"] == "b"
    assert {k: v for k, v in out.items() if k not in ("live", "exited")} == {
        k: v for k, v in shown.items() if k not in ("live", "exited")}
