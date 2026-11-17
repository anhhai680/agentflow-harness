"""A run's plan tasks joined with the run's state, for the dashboard's Plan tab (read-only).

- `run_plan(run, repo, cfg)`: the plan's tasks, each with its status, stop, review, cost, sessions, gates, wave and
  blast radius. The plan is read from git (`artifacts.read_blob`), never from a worktree.
- `waves(tasks)` and `dependents(tasks, n)`: the dependency graph's longest-path layers and a task's blast radius.
  Both ignore a dependency the plan does not have and always terminate on a cycle.

A task's step keys are `<prefix>/task-<n>` followed by `/` or the end of the key (`_under`), never a bare prefix, so
a stop on task 10 never marks task 1.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

from agentflow.artifacts import NotFound, _git, normal_finding, read_blob, run_artifacts
from agentflow.config import Config
from agentflow.sources import PlanTask, is_plan_name, parse_plan_text, task_files
from agentflow.state import Run
from agentflow.views import step_title

PLAN_MAX_BYTES = 4 * 1024 * 1024  # parsed here, so it is not held to the page cap
STATUSES = ("blocked", "parked", "running", "failed", "committed", "deferred", "done_before", "waiting",
            "independent", "pending")
GATES_PER_KEY = 10
_TASK_STEP = re.compile(r"^(.+)/task-\d+(?:/|$)")
_SUMMARY = ("committed", "blocked", "parked", "running", "failed", "waiting", "independent", "deferred")


def _under(key: str, tkey: str) -> bool:
    """Whether step `key` is task step `tkey` or one of its sub-steps."""
    return key == tkey or key.startswith(tkey + "/")


def waves(tasks: list[PlanTask]) -> dict[int, int | None]:
    """`{n: wave}`: 0 for a task with no known dependency, else 1 + the highest wave of its dependencies. A task on a
    cycle, or one that depends on a cycle, is None. A dependency the plan lacks is ignored. Iterative."""
    deps = {t.n: t.depends_on for t in tasks}
    known = set(deps)
    result: dict[int, int | None] = {}
    active: set[int] = set()  # the nodes on the current walk
    for root in deps:
        if root in result:
            continue
        stack = [(root, iter([d for d in deps[root] if d in known]))]
        active.add(root)
        best = {root: 0}  # the highest wave seen among a node's finished dependencies; -1 once one is on a cycle
        while stack:
            node, it = stack[-1]
            advanced = False
            for dep in it:
                if dep in result:
                    got = result[dep]
                    best[node] = -1 if got is None or best[node] == -1 else max(best[node], got + 1)
                elif dep in active:  # back on the walk: a cycle
                    best[node] = -1
                else:
                    active.add(dep)
                    best[dep] = 0
                    stack.append((dep, iter([d for d in deps[dep] if d in known])))
                    advanced = True
                    break
            if advanced:
                continue
            stack.pop()
            active.discard(node)
            result[node] = None if best[node] == -1 else best[node]
            if stack:
                parent = stack[-1][0]
                best[parent] = -1 if result[node] is None or best[parent] == -1 else max(best[parent], result[node] + 1)
    return {t.n: result[t.n] for t in tasks}


def dependents(tasks: list[PlanTask], n: int) -> list[int]:
    """Every task whose transitive `depends_on` reaches `n`, sorted; never `n` itself."""
    forward: dict[int, list[int]] = {}
    for task in tasks:
        for dep in task.depends_on:
            forward.setdefault(dep, []).append(task.n)
    seen: set[int] = set()
    todo = [n]
    while todo:
        for nxt in forward.get(todo.pop(), []):
            if nxt not in seen:
                seen.add(nxt)
                todo.append(nxt)
    seen.discard(n)
    return sorted(seen)


def _ancestors(by: dict[int, PlanTask], n: int) -> set[int]:
    seen: set[int] = set()
    todo = list(by[n].depends_on)
    while todo:
        dep = todo.pop()
        if dep in by and dep not in seen:
            seen.add(dep)
            todo.extend(by[dep].depends_on)
    seen.discard(n)
    return seen


# -- where the plan is


def _pick(entries: list[dict]) -> dict | None:
    """U1's `pickArtifact` order: a `.md` first, then the most revisions, then the path."""
    return min(entries, key=lambda a: (0 if a["path"].lower().endswith(".md") else 1, -len(a["revisions"]),
                                       a["path"]), default=None)


def _branch_head(repo: Path, branch: object) -> str | None:
    if not isinstance(branch, str) or not branch:
        return None
    try:
        out, _ = _git(repo, "rev-parse", "--verify", "-q", f"refs/heads/{branch}")
    except NotFound:
        return None
    return out.decode("ascii", errors="replace").strip() or None


def _plan_at_head(repo: Path, head: str, plans: str, module: str) -> str | None:
    folder = plans.strip("/")
    try:
        out, _ = _git(repo, "--literal-pathspecs", "ls-tree", "--name-only", "-z", head, "--", folder + "/")
    except NotFound:
        return None
    names = [p.decode("utf-8", errors="replace") for p in out.split(b"\0") if p]
    found = [p for p in names if p.rpartition("/")[0] == folder and is_plan_name(p.rpartition("/")[2], module)]
    return max(found, key=lambda p: p.rpartition("/")[2], default=None)


def _locate(run: Run, repo: Path, cfg: Config, head: str | None) -> tuple[str | None, str | None]:
    """`(path, newest revision or None)` of the run's plan."""
    module = str(run.state.get("params", {}).get("module") or "")
    plans = [a for a in run_artifacts(run, repo) if a["kind"] == "plan"]
    named = [a for a in plans if is_plan_name(a["path"].rpartition("/")[2], module)]
    chosen = _pick(named) or _pick(plans)
    if chosen is not None:
        revisions = chosen["revisions"]
        return chosen["path"], revisions[-1]["sha"] if revisions else None
    if head is not None:
        return _plan_at_head(repo, head, cfg.project.docs.get("plans", ""), module), None
    return None, None


# -- the join


def _events(run: Run) -> list[dict]:
    try:
        return run.events.read()
    except OSError:
        return []


def _review(state: dict, tkey: str) -> dict | None:
    row = state.get("reviews", {}).get(f"{tkey}/review")
    if not isinstance(row, dict):
        return None
    found = row.get("findings")
    findings = [normal_finding(f) for f in found] if isinstance(found, list) else []
    blocking = row.get("blocking")
    blocking = len(blocking) if isinstance(blocking, list) else blocking if isinstance(blocking, int) else 0
    return {"round": row.get("round"), "blocking": blocking, "minor": len(findings) - blocking,
            "skipped": bool(row.get("skipped")), "findings": findings}


def _sessions(state: dict, tkey: str) -> list[dict]:
    rows = [(r, False) for r in state.get("exited", [])] + [(r, True) for r in state.get("agents", {}).values()]
    out = []
    for row, live in rows:
        step = row.get("step")
        if isinstance(step, str) and _under(step, tkey):
            out.append({"key": step, "label": step_title(step), "model": row.get("model"),
                        "effort": row.get("effort"), "exit_code": row.get("exit_code"),
                        "spend": row.get("spend") or 0.0, "live": live})
    out.reverse()
    return out


def _gates(state: dict, tkey: str) -> list[dict]:
    out = []
    for key in sorted(k for k in state.get("gates", {}) if _under(k, tkey)):
        for row in state["gates"][key][-GATES_PER_KEY:]:
            out.append({"key": key, "gate": row.get("gate"), "passed": row.get("passed"),
                        "skipped": bool(row.get("skipped"))})
    return out


def _stop(state: dict, tkey: str) -> dict | None:
    wait = state.get("waiting")
    entries = ([wait] if isinstance(wait, dict) else []) + list(state.get("parked", {}).values())
    for entry in entries:
        if isinstance(entry.get("key"), str) and _under(entry["key"], tkey):
            return {"kind": entry.get("kind"), "key": entry["key"], "summary": entry.get("summary", "")}
    return None


def _base_status(task: PlanTask, tkey: str | None, state: dict, committed: set[int],
                 restarted: frozenset[int] = frozenset()) -> str | None:
    """The first of `STATUSES` up to `done_before`, or None when the task is `waiting` or later."""
    if tkey is not None:
        wait = state.get("waiting")
        if isinstance(wait, dict) and isinstance(wait.get("key"), str) and _under(wait["key"], tkey):
            return "blocked"
        if any(_under(k, tkey) for k in state.get("parked", {})):
            return "parked"
        if any(isinstance(a.get("step"), str) and _under(a["step"], tkey) for a in state.get("agents", {}).values()) \
                or ((task.n not in committed or task.n in restarted)  # a step started after the commit is a redo
                    and any(r.get("status") == "running" and _under(k, tkey) for k, r in state.get("steps", {}).items())):
            return "running"
        if state.get("steps", {}).get(f"{tkey}/implement", {}).get("status") == "failed":
            return "failed"
        if task.n in committed:
            return "committed"
    if task.deferred:
        return "deferred"
    if task.checked and (tkey is None or not any(_under(k, tkey) for k in state.get("steps", {}))):
        return "done_before"
    return None


def _restarted(events: list[dict], tkey: str, n: int) -> bool:
    """True when the newest `step_started` under `tkey` comes after the task's newest `task_committed`."""
    started = committed = -1
    for i, e in enumerate(events):
        key = e.get("key")
        if e.get("type") == "step_started" and isinstance(key, str) and _under(key, tkey):
            started = i
        elif e.get("type") == "task_committed" and e.get("n") == n and key == tkey:
            committed = i
    return committed >= 0 and started > committed


def run_plan(run: Run, repo: Path, cfg: Config) -> dict:
    """The run's plan as `{path, sha, truncated, started, prefix, summary, tasks}` (see the module doc)."""
    repo = Path(repo)
    state = run.state
    steps = state.get("steps", {})
    counted = Counter(m.group(1) for k in steps if (m := _TASK_STEP.match(k)))
    prefix = counted.most_common(1)[0][0] if counted else None
    empty = {"path": None, "sha": None, "truncated": False, "started": prefix is not None, "prefix": prefix,
             "summary": dict.fromkeys(("total", *_SUMMARY), 0), "tasks": []}
    head = _branch_head(repo, state.get("branch"))
    path, newest = _locate(run, repo, cfg, head)
    if path is None:
        return empty
    text = None
    for sha in (head, newest):
        if sha is None:
            continue
        try:
            text, truncated = read_blob(repo, sha, path, PLAN_MAX_BYTES)
        except NotFound:
            continue
        break
    if text is None:
        return {**empty, "path": path, "sha": head or newest}
    tasks = parse_plan_text(text)
    by = {t.n: t for t in tasks}
    events = _events(run)

    def tkey_of(n: int) -> str | None:
        return f"{prefix}/task-{n}" if prefix is not None else None

    committed = {t.n for t in tasks if tkey_of(t.n) is not None and any(
        e.get("type") == "task_committed" and e.get("n") == t.n and e.get("key") == tkey_of(t.n) for e in events)}
    restarted = frozenset(t.n for t in tasks if tkey_of(t.n) is not None and _restarted(events, tkey_of(t.n), t.n))
    base = {t.n: _base_status(t, tkey_of(t.n), state, committed, restarted) for t in tasks}
    stopped = {n for n, s in base.items() if s in ("blocked", "parked")}
    layers = waves(tasks)
    rows = []
    for task in tasks:
        tkey = tkey_of(task.n)
        blocked_by = sorted(_ancestors(by, task.n) & stopped)
        status = base[task.n] or ("waiting" if blocked_by else "independent" if stopped else "pending")
        done = {n for n, s in base.items() if s in ("committed", "deferred", "done_before")}
        row = {"n": task.n, "title": task.title, "text": task.text, "depends_on": list(task.depends_on),
               "files": task_files(task), "wave": layers[task.n], "status": status, "stop": None, "attempts": 0,
               "rollovers": 0, "resolvers": 0, "review": None, "commit": None, "cost": 0.0, "sessions": [],
               "gates": [], "blocks": [n for n in dependents(tasks, task.n) if n not in done],
               "blocked_by": blocked_by}
        if tkey is not None:
            implement = steps.get(f"{tkey}/implement", {})
            sessions = _sessions(state, tkey)
            shas = [e.get("sha") for e in events if e.get("type") == "task_committed" and e.get("n") == task.n
                    and e.get("key") == tkey]
            row.update(stop=_stop(state, tkey), attempts=implement.get("attempts", 0),
                       rollovers=implement.get("rollovers", 0),
                       resolvers=sum(1 for e in events if e.get("type") == "resolve_started"
                                     and isinstance(e.get("key"), str) and _under(e["key"], tkey)),
                       review=_review(state, tkey), commit=shas[-1] if shas else None,
                       cost=round(sum(s["spend"] for s in sessions), 2), sessions=sessions,
                       gates=_gates(state, tkey))
        rows.append(row)
    summary = {"total": len(rows), **{k: sum(1 for r in rows if r["status"] == k) for k in _SUMMARY}}
    return {"path": path, "sha": head or newest, "truncated": truncated, "started": prefix is not None,
            "prefix": prefix, "summary": summary, "tasks": rows}
