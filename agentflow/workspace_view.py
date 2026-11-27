"""The workspace's merged Inbox and Agents, and the `repo` tag that lets a view of one repository be told apart from
another's (dashboard slice W3).

- `tagged_inbox` and `tagged_agents` copy one repository's view with its repository's id on every row.
- `merged_inbox` and `merged_agents` read every usable repository in turn, guarded one by one: a repository whose
  read raises becomes an `errors` line (`{repo, error}`) and the others still answer.

Pure apart from the `read` they are given, which defaults to the single-repository view. No request value reaches
this module: it is handed contexts the server holds.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable
from datetime import datetime
from pathlib import Path

from agentflow.config import Config
from agentflow.doctor import printable
from agentflow.events import parse_iso
from agentflow.inbox_view import inbox_view
from agentflow.repo_context import ERROR_MAX, RepoContext, usable
from agentflow.views import DEFAULT_EXITED_MIN, DEFAULT_IDLE_MIN, agents_view

INBOX_ROWS = ("items", "watch", "recent")
AGENT_ROWS = ("live", "exited")


def _tag(view: dict, repo: str, keys: tuple[str, ...]) -> dict:
    """A copy of `view` whose `keys` lists hold copies of their rows with `repo` added; nothing is mutated."""
    out = dict(view)
    for key in keys:
        out[key] = [{**row, "repo": repo} for row in view.get(key) or []]
    return out


def tagged_inbox(view: dict, repo: str) -> dict:
    """`view` with `repo` on every item, watch and recent row."""
    return _tag(view, repo, INBOX_ROWS)


def tagged_agents(view: dict, repo: str) -> dict:
    """`view` with `repo` on every live and exited row."""
    return _tag(view, repo, AGENT_ROWS)


def _error(ctx: RepoContext, exc: BaseException) -> dict:
    text = " ".join(printable(exc).split())[:ERROR_MAX]
    return {"repo": ctx.id, "error": text}


def _read_each(contexts: Iterable[RepoContext], now: datetime, read: Callable[[Path, Config, datetime], dict],
               tag: Callable[[dict, str], dict]) -> tuple[list[dict], list[dict]]:
    """Each usable context's tagged view, in order, and the error line of each one whose read raised."""
    views: list[dict] = []
    errors: list[dict] = []
    for ctx in contexts:
        if not usable(ctx):
            continue
        try:
            views.append(tag(read(ctx.repo, ctx.cfg, now), ctx.id))
        except Exception as exc:  # one repository's unreadable runs never cost the others their rows
            errors.append(_error(ctx, exc))
    return views, errors


def merged_inbox(contexts: Iterable[RepoContext], now: datetime,
                 read: Callable[[Path, Config, datetime], dict] = inbox_view) -> dict:
    """Every usable repository's Inbox as `{items, watch, recent, counts: {approval, question, stop, watch}, errors}`.
    `items` are oldest first across repositories (a stable sort, so one repository's own order is kept), `watch` is
    in workspace order, `recent` is newest first and `counts` are summed. Two runs that share an id stay two rows."""
    views, errors = _read_each(contexts, now, read, tagged_inbox)
    items = [row for view in views for row in view["items"]]
    watch = [row for view in views for row in view["watch"]]
    recent = [row for view in views for row in view["recent"]]
    items.sort(key=lambda row: parse_iso(row.get("since")) or now)
    recent.sort(key=lambda row: parse_iso(row.get("ts")) or now, reverse=True)
    counts = dict.fromkeys(("approval", "question", "stop", "watch"), 0)
    for view in views:
        for key in counts:
            counts[key] += int((view.get("counts") or {}).get(key) or 0)
    return {"items": items, "watch": watch, "recent": recent, "counts": counts, "errors": errors}


def merged_agents(contexts: Iterable[RepoContext], now: datetime,
                  read: Callable[[Path, Config, datetime], dict] = agents_view) -> dict:
    """Every usable repository's agents as `{live, exited, counts: {by_agent, by_model}, idle_warn_min,
    show_exited_min, generated_at, errors}`: rows in workspace order, `counts` recounted from the merged `live`, the
    two windows the widest any repository set, and `generated_at` the time of the merge."""
    views, errors = _read_each(contexts, now, read, tagged_agents)
    live = [row for view in views for row in view["live"]]
    exited = [row for view in views for row in view["exited"]]
    return {
        "live": live,
        "exited": exited,
        "counts": {"by_agent": dict(Counter(str(row["agent"]) for row in live)),
                   "by_model": dict(Counter(str(row["model"]) for row in live))},
        "idle_warn_min": max((view["idle_warn_min"] for view in views), default=float(DEFAULT_IDLE_MIN)),
        "show_exited_min": max((view["show_exited_min"] for view in views), default=float(DEFAULT_EXITED_MIN)),
        "generated_at": now.isoformat(timespec="seconds"),
        "errors": errors,
    }
