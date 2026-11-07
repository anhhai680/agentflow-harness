"""Read-only views over the runs, for the dashboard and the terminal.

- `board(repo, cfg, lane)`: the Kanban board of the one workflow, for every lane or one: `Backlog` (queue
  modules not started), one column per phase, `Done`; one card per run, a ready one with the moves it can
  make; the Needs-human items of every run; header counts.
- `workflow_view(cfg)`: the workflow from `config.yaml`: its phases and each step's skill, lanes and model.
- `agents_view(repo, cfg)`: every live agent process across runs, one row each (subagents nested), and
  the ones that exited in the last `dashboard.agents.show_exited_min` minutes.
- `run_detail(run, cfg)`: one run's state, timeline, its walk of the workflow (`run_workflow`), its time and
  cost (`run_metrics_view`) and size-capped tails of its files, read only through the run's own paths (no
  symlinks, nothing outside the run directory).
- `metrics_view(repo, cfg, lane, date_from, date_to)`: time and cost by phase, bucket and `model · effort`
  for every run started in a date range (this month by default), finished and archived runs included, with
  the totals over them (`metrics.aggregate`). `metrics_table` renders it for `agentflow metrics`.
- `board_table` / `agents_table` render the same data for `agentflow status --watch` / `agentflow agents --watch`,
  and `watch` re-renders when the runs change.

Everything comes from `state.json`/`events.jsonl`, so the views cannot disagree with the CLI.
"""

from __future__ import annotations

import os
import re
import time
from collections import Counter
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from agentflow.config import Config, Phase, Step, check_lane, for_lane, resolve_model
from agentflow.events import parse_iso
from agentflow.hitl import inbox_items
from agentflow.metrics import BUCKETS, aggregate, run_metrics
from agentflow.state import TERMINAL, Run, cost_added, display_status, list_runs, runs_root

ARROWS = {"up": "↑", "down": "↓"}
BADGES = {"waiting_human": "waiting human", "awaiting_approval": "awaiting approval"}
PR_URL = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/pull/\d+")
LABEL_PARAMS = ("module", "issue", "topic", "task", "slice", "log")
TASK_KEY = re.compile(r"^.+/task-(?P<n>\d+)/(?P<part>[\w-]+)$")
ROUND_KEY = re.compile(r"/r(\d+)(?:/|$)")
NOT_REACHED = "not reached"
STEP_STATUS = {"done": "done", "unchanged": "done", "skipped": "skipped", "failed": "failed", "running": "running",
               "sizing": "running", "compacting": "running", "waiting_human": "waiting", "parked": "waiting"}
TAIL_BYTES = 16 * 1024
GATE_TAIL_BYTES = 8 * 1024
TIMELINE_MAX = 300
DEFAULT_IDLE_MIN = 5
DEFAULT_EXITED_MIN = 10
DEFAULT_DONE_DAYS = 0  # 0 keeps every Done card: history stays until a developer deletes `runs/`
STALL_MARGIN_S = 15 * 60  # past the step timeout plus this, a silent run had no runner going

__all__ = ["agents_table", "agents_view", "board", "board_table", "metrics_csv", "metrics_table", "metrics_view",
           "month_start", "run_detail", "run_metrics_view", "run_workflow", "runs_signature", "watch", "workflow_view"]


# --------------------------------------------------------------------------- helpers


def _alive(pid: object) -> bool:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return pid is None  # a row without a PID cannot be checked; it stays live until it exits
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _age(now: datetime, ts: object) -> float | None:
    parsed = parse_iso(ts)
    return None if parsed is None else max(0.0, (now - parsed).total_seconds())


def _label(state: dict, run_id: str) -> str:
    params = state.get("params") or {}
    for name in LABEL_PARAMS:
        if params.get(name) not in (None, ""):
            return Path(str(params[name])).stem if name == "log" else str(params[name])
    return run_id


def _digest(run: Run, today: datetime, events: list[dict] | None = None) -> dict:
    """One pass over the run's events: when each phase last started, today's spend, fix attempts per step."""
    started: dict[str, str] = {}
    fixes: dict[str, int] = {}
    counted: dict[str, float] = {}
    spend_today = 0.0
    for event in run.events.read() if events is None else events:
        etype, key = event.get("type"), event.get("key")
        if etype == "phase_started" and event.get("phase"):
            started[str(event["phase"])] = str(event.get("ts"))
        elif etype in ("step_started", "hitl_approved", "step_retry") and key:
            fixes.pop(str(key), None)
        elif etype == "fix_attempt" and key:
            fixes[str(key)] = int(event.get("attempt") or 0)
        elif etype == "agent_exited":
            added = cost_added(counted, event)
            ts = parse_iso(event.get("ts"))
            if ts is not None and ts.astimezone().date() == today.astimezone().date():
                spend_today += added
    return {"phase_started": started, "fixes": fixes, "spend_today": round(spend_today, 6)}


def _step_label(state: dict, key: str) -> str:
    """`task 3/7 implement` for a plan task's step, else the step id within its phase."""
    match = TASK_KEY.match(key)
    if match:
        task = state.get("task") or {}
        of = f"/{task['of']}" if task.get("n") == int(match["n"]) and task.get("of") else ""
        return f"task {match['n']}{of} {match['part']}"
    return key.split("/", 1)[1] if "/" in key else key


def _effort_reason(state: dict, key: str) -> str | None:
    for change in reversed(state.get("effort_changes") or []):
        if change.get("key") == key:
            return change.get("reason")
    return None


def _last_gates(state: dict) -> list[dict]:
    gates = state.get("gates") or {}
    if not gates:
        return []
    last: dict[str, dict] = {}
    for entry in gates[list(gates)[-1]]:
        last[str(entry.get("gate"))] = {"gate": entry.get("gate"), "passed": bool(entry.get("passed")),
                                        "skipped": bool(entry.get("skipped"))}
    return list(last.values())


def _live_agents(state: dict) -> list[dict]:
    return [row for row in state.get("agents", {}).values() if _alive(row.get("pid"))]


# --------------------------------------------------------------------------- board


def _walks(steps: list[Step], lane: str) -> bool:
    """Whether a run of `lane` has anything to run in `steps` (a group counts only if a child of it does)."""
    return any(not step.steps or _walks(step.steps, lane) for step in for_lane(steps, lane))


def lane_phases(cfg: Config, lane: str) -> list[str]:
    """The phases a run of `lane` walks, in order (a phase with nothing to run for the lane is left out)."""
    return [name for name, phase in cfg.phases.items() if _walks(phase.steps, lane)]


def lane_first_phase(cfg: Config, lane: str) -> str | None:
    """The first phase a run of `lane` walks: where a Backlog card goes, unless its run has no work there."""
    walked = lane_phases(cfg, lane)
    return walked[0] if walked else None


def _ready(state: dict, cfg: Config) -> dict | None:
    """What a ready card can do: `next`, the phase Continue starts (None when only the last phase is left, so
    Mark done is the one move), and `skips`, the phases Mark done would leave out. A `skip_if` can still
    skip `next` when the run gets there."""
    ready = state.get("ready")
    if state["status"] != "ready" or not ready:
        return None
    names = list(cfg.phases)
    start = names.index(ready["phase"]) + 1 if ready["phase"] in names else len(names)
    skips = [name for name in names[start:-1] if _walks(cfg.phases[name].steps, state.get("pipeline"))]
    return {"phase": ready["phase"], "next": skips[0] if skips else None, "skips": skips}


def _pr_link(state: dict) -> str | None:
    """`pr_url` when it is safe to render as a link: it comes from `gh`'s output, so the page does not trust it."""
    url = state.get("pr_url")
    return url if isinstance(url, str) and PR_URL.fullmatch(url) else None


def _column(state: dict, phases: list[str]) -> str:
    """The board column: a run ended early by `--to` stays at that phase, since its module is not finished;
    a run a human marked done or archived is finished, wherever it stopped."""
    if state.get("archived"):
        return "Done"
    if state["status"] == "done":
        if state.get("marked_done"):
            return "Done"
        stopped_at = (state.get("options") or {}).get("to_phase")
        return stopped_at if stopped_at in phases[:-1] else "Done"
    phase = state.get("phase")
    return phase if phase in phases else phases[0]


def _badge(state: dict, status: str, column: str) -> str:
    if state.get("archived"):
        return "archived"
    if status == "done" and column != "Done":
        return f"stopped after {column}"
    resolving = state.get("resolving")
    if resolving and status == "running":
        return f"agent fixing: {resolving.get('kind')}"
    if state.get("ci") and status == "running":
        return "waiting for CI"
    hold = state.get("hold")
    if status == "held" and hold:
        until = parse_iso(hold["until"])
        return f"held until {until.astimezone().strftime('%H:%M') if until else '?'} ({hold['reason']})"
    return BADGES.get(status, status)


def _card(run: Run, status: str, cfg: Config, now: datetime, digest: dict, metrics: dict) -> dict:
    state = run.state
    phases = list(cfg.phases)
    phase = state.get("phase")
    column = _column(state, phases)
    live = _live_agents(state)
    agent = None
    if live:
        row = live[-1]
        agent = {"agent": row.get("agent"), "model": row.get("model"), "effort": row.get("effort"),
                 "arrow": row.get("effort_changed"), "reason": _effort_reason(state, row.get("step") or ""),
                 "status": row.get("status")}
    task = state.get("task") or {}
    since = digest["phase_started"].get(phase or "")
    waiting = state.get("waiting")
    return {
        "run_id": run.id,
        "lane": state.get("pipeline"),
        "module": _label(state, run.id),
        "column": column,
        "phase": phase,
        "step": state.get("step"),
        "task": f"{task['n']}/{task['of']}" if task.get("n") and task.get("of") else None,
        "status": status,
        "badge": _badge(state, status, column),
        "refine_round": (state.get("refine") or {}).get("round"),
        "agent": agent,
        "context_pct": live[-1].get("context_pct") if live else None,
        "spend": round(float(state.get("spend_usd") or 0.0) + sum(float(r.get("spend") or 0.0) for r in live), 6),
        "budget": state.get("budget_usd"),
        "gates": _last_gates(state),
        "phase_since": since,
        "time_in_phase_s": _age(now, since),
        "waiting": {k: waiting.get(k) for k in ("kind", "key", "summary")} if waiting else None,
        "branch": state.get("branch"),
        "pr_url": state.get("pr_url"),
        "pr_link": _pr_link(state),
        "ready": _ready(state, cfg),
        "failure": state.get("failure"),
        "wall_s": metrics["wall_s"],
        "phase_wall_s": {p["phase"]: p["wall_s"] for p in metrics["phases"]},
    }


def board(repo: Path, cfg: Config, lane: str | None = None, now: datetime | None = None) -> dict:
    """The Kanban data for every lane, or one `lane` (see the module docstring)."""
    if lane is not None:
        check_lane(cfg, lane)
    now = now or datetime.now(UTC)
    phases = list(cfg.phases)
    done_days = cfg.dashboard.get("show_done_days", DEFAULT_DONE_DAYS)
    from agentflow import work_source
    from agentflow.backlog import snapshot

    pile = snapshot(cfg, repo)
    cards, counts, spend_today = [], Counter(), 0.0
    by_agent, by_model = Counter(), Counter()
    for run in list_runs(repo):
        state = run.state
        events = run.events.read()
        digest = _digest(run, now, events)
        spend_today += digest["spend_today"]
        for row in _live_agents(state):
            by_agent[str(row.get("agent"))] += 1
            by_model[str(row.get("model"))] += 1
        if lane is not None and state.get("pipeline") != lane:
            continue
        age = _age(now, state.get("updated_at"))
        if (done_days and state["status"] in TERMINAL and age is not None
                and age > timedelta(days=done_days).total_seconds()):
            continue
        status = display_status(run)
        counts[status] += 1
        cards.append(_card(run, status, cfg, now, digest, run_metrics_view(run, cfg, now, events)))
    needs = [{**item, "age_s": _age(now, item.get("since"))} for item in inbox_items(repo)]
    return {
        "lane": lane,
        "lanes": list(cfg.lanes),
        "columns": ["Backlog", *phases, "Done"],
        "cards": cards,
        "backlog": [c for c in pile["cards"] if lane is None or c["lane"] == lane],
        "sync_note": pile["sync_note"],
        "new_task": pile["new_task"],
        "lane_phases": pile["lane_phases"],
        "lane_labels": pile["lane_labels"],
        "after_choices": pile["after_choices"],
        "modules": pile["modules"],
        "backlog_skipped": pile["skipped"],
        "work_source_kind": work_source.kind(cfg),
        "needs_human": needs,
        "counts": dict(counts),
        "agents_running": {"total": sum(by_agent.values()), "by_agent": dict(by_agent), "by_model": dict(by_model)},
        "spend_today": round(spend_today, 2),
        "run_budget_usd": cfg.defaults.run_budget_usd,
        "generated_at": now.isoformat(timespec="seconds"),
    }


# --------------------------------------------------------------------------- the workflow


def _condition_text(cond: Any) -> str:
    """A short label for a condition: its name, or how its named parts combine."""
    if isinstance(cond, dict):
        if "not" in cond:
            return f"not {_condition_text(cond['not'])}"
        for key, word in (("all", " and "), ("any", " or ")):
            if key in cond:
                return word.join(_condition_text(part) for part in cond[key])
        return str(cond.get("check", "condition"))
    return str(cond)


def _runs_when(step: Step) -> str:
    parts = [_condition_text(step.when)] if step.when is not None else []
    if step.unless is not None:
        parts.append(f"unless {_condition_text(step.unless)}")
    return ", ".join(parts)


def _step_view(cfg: Config, phase: Phase, step: Step, inherited: list[str] | None = None) -> dict:
    """One step for the Workflow tab; a nested step runs in the lanes of the step around it."""
    gates = [step.gates] if isinstance(step.gates, str) else list(step.gates or [])
    lanes = step.lanes or inherited or list(cfg.lanes)
    agent_call = step.type == "agent" or (step.type == "gate" and step.fix_loop)
    return {
        "id": step.id,
        "type": step.type,
        "skill": step.skill,
        "small_plan_skill": (step.small_plan or {}).get("skill"),
        "tdd": step.tdd,
        "lanes": lanes,
        "model": resolve_model(cfg, phase, step, step.role or "build", {})[0] if agent_call else None,
        "runs_when": _runs_when(step),
        "gates": gates,
        "review": {**_step_view(cfg, phase, step.review, lanes), "id": "per-task review"} if step.review else None,
        "steps": [_step_view(cfg, phase, child, lanes) for child in step.steps],
    }


def workflow_view(cfg: Config) -> dict:
    """The one workflow from `config.yaml`: phases in order, each step with its skill, lanes and model."""
    return {
        "lanes": list(cfg.lanes),
        "phases": [{"name": name, "approval": phase.needs_approval,
                    "human_skill": (phase.approval_note or {}).get("human_skill"),
                    "steps": [_step_view(cfg, phase, step) for step in phase.steps]}
                   for name, phase in cfg.phases.items()],
        "human_skills": list(cfg.guardrails.user_only_skills),
    }


def _rolled_status(rows: list[dict]) -> str:
    seen = {STEP_STATUS.get(row.get("status"), NOT_REACHED) for row in rows}
    for status in ("failed", "waiting", "running"):
        if status in seen:
            return status
    if not seen or seen == {NOT_REACHED}:
        return NOT_REACHED
    if seen == {"skipped"}:
        return "skipped"
    return "running" if NOT_REACHED in seen else "done"


def _count(state: dict, children: dict[str, dict]) -> str | None:
    """`2/5 tasks` for a per-task step, `2 rounds` for a repeat that went round again."""
    implement = [row for key, row in children.items() if key.endswith("/implement")]
    if implement:
        done = sum(1 for row in implement if row.get("status") in ("done", "unchanged"))
        total = state.get("task_total") or (state.get("task") or {}).get("of") or len(implement)
        return f"{done}/{total} tasks"
    rounds = {match.group(1) for key in children if (match := ROUND_KEY.search(key))}
    return f"{len(rounds)} rounds" if len(rounds) > 1 else None


def _run_step(state: dict, lane: str, step: Step, key: str) -> dict:
    steps = state.get("steps") or {}
    own = steps.get(key)
    children = {k: row for k, row in steps.items() if k.startswith(f"{key}/")}
    waiting = str((state.get("waiting") or {}).get("key") or "")
    status = "waiting" if waiting == key or waiting.startswith(f"{key}/") else _rolled_status(
        [*([own] if own else []), *children.values()])
    prefix = key
    if step.repeat:
        rounds = [int(match.group(1)) for k in children if (match := ROUND_KEY.search(k))]
        prefix = f"{key}/r{max(rounds, default=1)}"
    return {
        "id": step.id,
        "skill": step.skill,
        "status": status,
        "reason": (own or {}).get("skip_reason"),
        "count": _count(state, children),
        "steps": [_run_step(state, lane, child, f"{prefix}/{child.id}") for child in for_lane(step.steps, lane)],
    }


def _phase_status(state: dict, name: str) -> dict:
    row = (state.get("phases") or {}).get(name) or {}
    status = row.get("status") or NOT_REACHED
    if status == "pending":
        status = NOT_REACHED
    if status == "running" and state.get("status") == "done":  # the last phase raises no approval
        status = "done"
    return {"name": name, "status": status, "reason": row.get("skip_reason")}


def _recorded_workflow(state: dict) -> list[dict]:
    """A run of a lane the workflow no longer has: its own step records, grouped by phase."""
    phases: dict[str, list[dict]] = {}
    for key, row in (state.get("steps") or {}).items():
        name = str(row.get("phase") or key.split("/")[0])
        phases.setdefault(name, []).append({
            "id": key.split("/", 1)[-1], "skill": None, "status": STEP_STATUS.get(row.get("status"), NOT_REACHED),
            "reason": row.get("skip_reason"), "count": None, "steps": [],
        })
    return [{**_phase_status(state, name), "steps": rows} for name, rows in phases.items()]


def run_workflow(run: Run, cfg: Config) -> list[dict]:
    """The workflow as this run walks it: only its lane's phases and steps, each with its status now."""
    state = run.state
    lane = str(state.get("pipeline"))
    if lane not in cfg.lanes:
        return _recorded_workflow(state)
    out = []
    for name, phase in cfg.phases.items():
        steps = for_lane(phase.steps, lane)
        if steps:
            out.append({**_phase_status(state, name),
                        "steps": [_run_step(state, lane, step, f"{name}/{step.id}") for step in steps]})
    return out


# --------------------------------------------------------------------------- agents


def _agent_row(run: Run, row: dict, digest: dict, now: datetime, fixes_of: int | None, idle_min: float) -> dict:
    state = run.state
    key = str(row.get("step") or "")
    phase = row.get("phase") or (key.split("/")[0] if key else None)
    record = state["steps"].get(key) or {}
    label = _step_label(state, key)
    fix = digest["fixes"].get(key)
    fix_text = (f"{fix}/{fixes_of}" if fixes_of else str(fix)) if fix else None
    rollovers = int(record.get("rollovers") or 0)
    place = f"{phase} › {label}"
    if fix_text:
        place += f" · fix {fix_text}"
    if rollovers:
        place += f" · rollover {rollovers}"
    quiet = _age(now, row.get("last_activity"))
    return {
        "run_id": run.id,
        "module": _label(state, run.id),
        "pipeline": state.get("pipeline"),
        "phase": phase,
        "key": key,
        "step": key.split("/", 1)[1] if "/" in key else key,
        "where": f"{state.get('pipeline')} · {place}",  # the CLI table
        "place": place,                                  # the dashboard's table
        "fix": fix_text,
        "rollovers": rollovers,
        "role": row.get("role"),
        "agent": row.get("agent"),
        "model": row.get("model"),
        "model_source": record.get("model_source"),
        "effort": row.get("effort"),
        "effort_arrow": row.get("effort_changed"),
        "effort_reason": _effort_reason(state, key),
        "effort_refused": record.get("effort_refused"),
        "status": row.get("status"),
        "context_pct": row.get("context_pct"),
        "spend": row.get("spend"),
        "age_s": _age(now, row.get("started_at")),
        "last_activity": row.get("last_activity"),
        "last_tool": row.get("last_tool"),
        "idle": quiet is not None and quiet > idle_min * 60,
        "pid": row.get("pid"),
        "session_id": row.get("session_id"),
        "subagents": [{"subagent_type": s.get("subagent_type"), "model": s.get("model") or "inherit",
                       "done": bool(s.get("done"))} for s in row.get("subagents") or []],
    }


def agents_view(repo: Path, cfg: Config | None = None, now: datetime | None = None) -> dict:
    """Every live agent across runs (`live`) and the recently exited ones (`exited`)."""
    now = now or datetime.now(UTC)
    settings = (cfg.dashboard.get("agents") if cfg else None) or {}
    idle_min = float(settings.get("idle_warn_min", DEFAULT_IDLE_MIN))
    exited_min = float(settings.get("show_exited_min", DEFAULT_EXITED_MIN))
    fixes_of = cfg.defaults.fix_attempts if cfg else None
    live: list[dict] = []
    exited: list[dict] = []
    window = exited_min * 60
    for run in list_runs(repo):
        digest = _digest(run, now)
        live_sessions = set()
        for row in run.state.get("agents", {}).values():
            shown = _agent_row(run, row, digest, now, fixes_of, idle_min)
            if _alive(row.get("pid")):
                live.append(shown)
                if row.get("session_id"):
                    live_sessions.add(row["session_id"])
            elif (_age(now, row.get("last_activity")) or 0.0) <= window:
                exited.append({**shown, "status": "gone", "exit_code": None, "idle": False})
        latest: dict[object, dict] = {}
        for index, row in enumerate(run.state.get("exited") or []):
            session = row.get("session_id")
            if session and session in live_sessions:
                continue  # the same session runs again (a fix-loop resume): one row, the live one
            age = _age(now, row.get("exited_at"))
            if age is None or age > window:
                continue
            shown = _agent_row(run, row, digest, now, fixes_of, idle_min)
            latest[session or ("pid", index)] = {**shown, "status": f"exit {row.get('exit_code')}", "idle": False,
                                                 "exit_code": row.get("exit_code"), "exited_at": row.get("exited_at")}
        exited.extend(latest.values())
    return {
        "live": live,
        "exited": exited,
        "counts": {"by_agent": dict(Counter(str(r["agent"]) for r in live)),
                   "by_model": dict(Counter(str(r["model"]) for r in live))},
        "idle_warn_min": idle_min,
        "show_exited_min": exited_min,
        "generated_at": now.isoformat(timespec="seconds"),
    }


# --------------------------------------------------------------------------- one run


def _tail(root: Path, path: Path, limit: int = TAIL_BYTES) -> str:
    """The last `limit` bytes of `path` as text, or "" unless it is a regular file inside `root` reached
    without a symlink. A cut first line is dropped."""
    try:
        if path.is_symlink():
            return ""
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(root.resolve()) or not resolved.is_file():
            return ""
        fd = os.open(resolved, os.O_RDONLY | os.O_NOFOLLOW)
    except (OSError, RuntimeError):
        return ""
    with os.fdopen(fd, "rb") as handle:
        size = os.fstat(handle.fileno()).st_size
        start = max(0, size - limit)
        handle.seek(start)
        data = handle.read(limit)
    if start > 0:
        cut = data.find(b"\n")
        data = data[cut + 1:] if cut >= 0 else b""
    return data.decode("utf-8", errors="replace")


def _newest(folder: Path, pattern: str) -> Path | None:
    if folder.is_symlink() or not folder.is_dir():
        return None
    files = [p for p in folder.glob(pattern) if p.is_file() and not p.is_symlink()]
    return max(files, key=lambda p: p.stat().st_mtime_ns) if files else None


def run_detail(run: Run, cfg: Config) -> dict:
    """What the card's detail drawer shows (see the module docstring)."""
    state = run.state
    root = run.dir
    gates = []
    for key, entries in (state.get("gates") or {}).items():
        for entry in entries[-10:]:
            log = entry.get("log")
            gates.append({"key": key, "gate": entry.get("gate"), "passed": entry.get("passed"),
                          "skipped": entry.get("skipped"),
                          "log_tail": _tail(root, Path(log), GATE_TAIL_BYTES) if isinstance(log, str) and log else ""})
    transcript = _newest(root / "transcripts", "*.jsonl")
    waiting = state.get("waiting") or {}
    return {
        "run_id": run.id,
        "status": display_status(run),
        "state": state,
        "timeline": run.events.read()[-TIMELINE_MAX:],
        "workflow": run_workflow(run, cfg),
        "gates": gates,
        "reviews": state.get("reviews") or {},
        "metrics": run_metrics_view(run, cfg),
        "progress": _tail(run.agent_dir, run.progress_path()),  # the agent's notes, in its workspace
        "handoff": _tail(root, run.handoff_path()),
        "inbox": _tail(root, run.inbox_path(waiting["key"])) if waiting.get("key") else "",
        "transcript": _tail(root, transcript) if transcript else "",
        "transcript_file": transcript.name if transcript else None,
        "attach": f"agentflow attach {run.id}",
    }


# --------------------------------------------------------------------------- time and cost

_METRICS_CACHE: dict[tuple, dict] = {}  # an ended run's metrics, by (events file, size, mtime): they cannot change
_METRICS_CACHE_MAX = 512


def _stall_s(cfg: Config | None) -> float:
    step_min = cfg.defaults.step_timeout_min if cfg else 45
    return float(step_min) * 60 + STALL_MARGIN_S


def run_metrics_view(run: Run, cfg: Config | None, now: datetime | None = None,
                     events: list[dict] | None = None) -> dict:
    """`metrics.run_metrics` for `run`: an ended run's figures are cached until its event log changes."""
    now = now or datetime.now(UTC)
    path = run.events.path
    try:
        stat = path.stat()
        token = (str(path), stat.st_size, stat.st_mtime_ns)
    except OSError:
        token = None
    if token is not None and token in _METRICS_CACHE:
        return _METRICS_CACHE[token]
    metrics = run_metrics(run.events.read() if events is None else events, now, _stall_s(cfg))
    if token is not None and not metrics["live"]:
        if len(_METRICS_CACHE) >= _METRICS_CACHE_MAX:
            _METRICS_CACHE.pop(next(iter(_METRICS_CACHE)))
        _METRICS_CACHE[token] = metrics
    return metrics


def month_start(now: datetime) -> date:
    """The first day of `now`'s month, in local time: where the Metrics tab starts by default."""
    return now.astimezone().date().replace(day=1)


def _started_on(run: Run, metrics: dict) -> date | None:
    started = parse_iso(metrics.get("started_at"))
    if started is None:
        try:  # a run id starts with its local start time: `20261103-2232-...`
            return datetime.strptime(run.id[:8], "%Y%m%d").date()
        except ValueError:
            return None
    return started.astimezone().date()


def metrics_view(repo: Path, cfg: Config, lane: str | None = None, date_from: date | None = None,
                 date_to: date | None = None, now: datetime | None = None, *, all_time: bool = False,
                 run_id: str | None = None) -> dict:
    """Time and cost for every run started from `date_from` to `date_to` (both days included, local time), or
    every run when `all_time`, or the one run `run_id`. With neither date, the range is this month. Old runs
    are never left out."""
    if lane is not None:
        check_lane(cfg, lane)
    now = now or datetime.now(UTC)
    all_time = all_time or run_id is not None
    if all_time:
        date_from = date_to = None
    elif date_from is None and date_to is None:
        date_from = month_start(now)
    if date_from and date_to and date_from > date_to:
        raise ValueError(f"from {date_from} is after to {date_to}")
    rows = []
    for run in list_runs(repo):
        state = run.state
        if (lane is not None and state.get("pipeline") != lane) or (run_id is not None and run.id != run_id):
            continue
        metrics = run_metrics_view(run, cfg, now)
        day = _started_on(run, metrics)
        if day is not None and ((date_from and day < date_from) or (date_to and day > date_to)):
            continue
        rows.append({
            "run_id": run.id,
            "lane": state.get("pipeline"),
            "module": _label(state, run.id),
            "status": display_status(run),
            "archived": bool(state.get("archived")),
            "budget": state.get("budget_usd"),
            "pr_link": _pr_link(state),
            "started_on": day.isoformat() if day else None,
            "metrics": metrics,
        })
    rows.sort(key=lambda r: r["metrics"].get("started_at") or r["run_id"], reverse=True)
    return {
        "lane": lane,
        "lanes": list(cfg.lanes),
        "phases": list(cfg.phases),
        "buckets": list(BUCKETS),
        "from": date_from.isoformat() if date_from else None,
        "to": date_to.isoformat() if date_to else None,
        "all_time": all_time,
        "runs": rows,
        "totals": aggregate(rows),
        "generated_at": now.isoformat(timespec="seconds"),
    }


# --------------------------------------------------------------------------- terminal


def _short(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    minutes = int(seconds // 60)
    return f"{minutes // 60}h{minutes % 60:02d}m" if minutes >= 60 else f"{minutes}m"


def _table(rows: list[list[str]]) -> list[str]:
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    return [("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row))).rstrip() for row in rows]


def _cell(card: dict) -> str:
    where = f"task {card['task']}" if card["task"] else (card["step"] or "-").split("/")[-1]
    agent = card["agent"]
    effort = f" {agent['model']}·{agent['effort']}{ARROWS.get(agent['arrow'] or '', '')}" if agent else ""
    refine = f" refine {card['refine_round']}" if card["refine_round"] else ""
    return f"{where} [{card['badge']}]{refine}{effort}"


def _backlog_text(card: dict) -> str:
    """A Backlog card in the terminal: `name [P]` when it can run in parallel, or what it runs after."""
    if card["blocked_by"]:
        return f"{card['label']} (runs after {', '.join(b['text'] for b in card['blocked_by'])})"
    return card["label"] + (" [P]" if card["parallel"] else "") + (" (starting)" if card["status"] == "starting" else "")


def board_table(data: dict) -> str:
    """The board as a compact table: one row per run, its cell under its column, then Needs human."""
    columns = data["columns"]
    rows = [["RUN", *columns, "SPEND"]]
    for card in data["cards"]:
        cells = [_cell(card) if col == card["column"] else "" for col in columns]
        budget = f"/${card['budget']:.0f}" if card["budget"] else ""
        rows.append([card["run_id"], *cells, f"${card['spend']:.2f}{budget}"])
    lines = _table(rows)
    if data["backlog"]:
        lines.append("Backlog: " + ", ".join(_backlog_text(card) for card in data["backlog"]))
    lines.append(f"Needs human ({len(data['needs_human'])}):")
    for item in data["needs_human"]:
        lines.append(f"  {item['run_id']} · {item['key']} · {item['kind']} · {_short(item['age_s'])} ago")
        if item.get("summary"):
            lines.append(f"    {item['summary']}")
    counts = " · ".join(f"{k} {v}" for k, v in sorted(data["counts"].items())) or "no runs"
    agents = data["agents_running"]
    lines.append(f"{data['lane'] or 'all lanes'}: {counts} | agents {agents['total']} | today ${data['spend_today']:.2f}")
    return "\n".join(lines)


def agents_table(view: dict) -> str:
    """The Agents view as a table, with subagents nested and the recently exited rows below."""
    live = view["live"]
    by_agent = " · ".join(f"{k} {v}" for k, v in sorted(view["counts"]["by_agent"].items()))
    by_model = " · ".join(f"{k} {v}" for k, v in sorted(view["counts"]["by_model"].items()))
    head = f"Agents running: {len(live)}" + (f"  ({by_agent} | {by_model})" if live else "")
    title = ["RUN", "LANE · PHASE › STEP", "ROLE", "AGENT", "MODEL", "EFFORT", "STATUS", "CTX", "SPEND", "FOR",
             "LAST ACTIVITY"]

    def line(r: dict) -> list[str]:
        ctx = f"{r['context_pct']:.0f}%" if isinstance(r["context_pct"], (int, float)) else "-"
        spend = f"${r['spend']:.2f}" if isinstance(r["spend"], (int, float)) else "-"
        effort = f"{r['effort'] or '-'} {ARROWS.get(r['effort_arrow'] or '', '')}".rstrip()
        last = (r["last_tool"] or "") + (" (idle)" if r["idle"] else "")
        return [r["module"], r["where"], r["role"] or "-", r["agent"] or "-", r["model"] or "-", effort,
                r["status"] or "-", ctx, spend, _short(r["age_s"]), last]

    rows = [title]
    for r in live:
        rows.append(line(r))
        rows += [["  └ sub", s["subagent_type"] or "subagent", "—", r["agent"] or "-", s["model"], "inherit",
                  "done" if s["done"] else "running", "-", "-", "-", ""] for s in r["subagents"]]
    out = [head, *(_table(rows) if live else [])]
    if view["exited"]:
        out.append(f"─── exited in the last {view['show_exited_min']:.0f} min ───")
        out += _table([title, *[line(r) for r in view["exited"]]])[(1 if live else 0):]  # one header is enough
    return "\n".join(out)


def _money(value: float, estimated: bool = False) -> str:
    return f"{'~' if estimated else ''}${value:.2f}"


def metrics_table(view: dict) -> str:
    """`agentflow metrics`: one block per run (a row per phase: wall, each bucket, cost, models), then totals."""
    shown = [b for b in view["buckets"] if any(r["metrics"]["buckets"].get(b) for r in view["runs"])]
    span = "all time" if view["all_time"] else f"{view['from'] or '…'} to {view['to'] or 'today'}"
    count = len(view["runs"])
    lines = [f"{view['lane'] or 'all lanes'} · {span} · {count} run{'' if count == 1 else 's'}"]
    for row in view["runs"]:
        m = row["metrics"]
        flags = "".join([" (stalled)" if m["stalled"] else "", " (live)" if m["live"] else "",
                         " (gate time approx)" if m["approx"] else ""])
        lines.append("")
        lines.append(f"{row['run_id']} · {row['lane']} · {row['module']} · {row['status']} · "
                     f"{_short(m['wall_s'])} · {_money(m['cost_usd'], m['cost_estimated'])}{flags}")
        table = [["PHASE", "WALL", *[b.upper() for b in shown], "COST", "REWORK", "MODELS"]]
        for p in m["phases"]:
            if p["status"] == "skipped" and not p["wall_s"] and not p["cost_usd"]:
                continue
            models = ", ".join(f"{x['model'] or '?'}·{x['effort'] or '?'} ×{x['sessions']}" for x in p["models"]) or "-"
            table.append([p["phase"], _short(p["wall_s"]), *[_short(p["buckets"][b]) for b in shown],
                          _money(p["cost_usd"]), _money(p["rework_cost_usd"]), models])
        lines += ["  " + line for line in _table(table)]
    totals = view["totals"]
    if view["runs"]:
        lines.append("")
        share = " · ".join(f"{b} {totals['bucket_pct'][b]:g}%" for b in view["buckets"] if totals["bucket_pct"][b])
        lines.append(f"total {_money(totals['cost_usd'])} · rework {totals['rework_pct']:g}% · "
                     f"median run {_short(totals['wall_s']['median'])} · {share}")
    return "\n".join(lines)


def metrics_csv(view: dict) -> str:
    """One row per run × phase × `model · effort` (a phase with no agent call gets one row with no model)."""
    import csv
    import io

    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["run_id", "lane", "module", "status", "started_on", "phase", "phase_wall_s",
                     *[f"{b}_s" for b in view["buckets"]], "phase_cost_usd", "phase_rework_cost_usd",
                     "model", "effort", "model_sessions", "model_agent_s", "model_cost_usd"])
    for row in view["runs"]:
        for p in row["metrics"]["phases"]:
            head = [row["run_id"], row["lane"], row["module"], row["status"], row["started_on"], p["phase"],
                    p["wall_s"], *[p["buckets"][b] for b in view["buckets"]], p["cost_usd"], p["rework_cost_usd"]]
            for model in p["models"] or [None]:
                tail = ([model["model"], model["effort"], model["sessions"], model["agent_s"], model["cost_usd"]]
                        if model else ["", "", 0, 0, 0])
                writer.writerow(head + tail)
    return out.getvalue()


def runs_signature(repo: Path) -> tuple:
    """Changes whenever a run's event log or `queue.json` changes (and every 30 s, so ages move on)."""
    root = runs_root(repo)
    marks: list[tuple] = [("clock", int(time.time() // 30))]
    if root.is_dir():
        for path in sorted(root.glob("*/events.jsonl")) + [root / "queue.json"]:
            try:
                info = path.stat()
            except OSError:
                continue
            marks.append((path.parent.name, info.st_size, info.st_mtime_ns))
    return tuple(marks)


def watch(render: Callable[[], str], signature: Callable[[], object], *, out: Callable[[str], object],
          sleep: Callable[[float], object] = time.sleep, stop: Callable[[], bool] = lambda: False,
          remind: Callable[[], object] | None = None, clock: Callable[[], float] = time.monotonic,
          interval: float = 0.5) -> None:
    """Call `out(render())` whenever `signature()` changes, and `remind()` once a minute, until `stop()`."""
    shown: object = object()
    next_remind = clock()
    while not stop():
        now = clock()
        if remind is not None and now >= next_remind:
            remind()
            next_remind = now + 60
        mark = signature()
        if mark != shown:
            out(render())
            shown = mark
        sleep(interval)
