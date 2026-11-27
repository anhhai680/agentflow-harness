"""The dashboard's Inbox: what a human can decide now, each item with the words, text and facts to decide on
(dashboard slice U4, read-only).

- `inbox_view(repo, cfg, now)`: `{items, watch, recent, counts}`. `items` are `hitl.inbox_items` plus a title, a group,
  the run's label, an age, the actions and the approval note that fit, the inbox file's text (its `agentflow ...`
  command paragraphs split off), the run's facts, and, where they apply, a phase approval's artifacts and open
  findings and a plan task's blast radius. `watch` lists the runs that wait on a limit or lost their runner and need
  no decision. `recent` lists what a person decided in the last `RECENT_S`.
- `item_title`, `approve_note`, `group_of` and `split_cli` are pure.

Each run's events are read once and passed to whatever needs them. A run whose artifacts or plan cannot be read still
gets its card, with those fields empty.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from agentflow import hitl
from agentflow.artifacts import PHASE_KINDS, NotFound, artifact_max_bytes, run_artifacts, run_findings
from agentflow.config import Config, ConfigError, load_config
from agentflow.config_trust import run_config_path
from agentflow.events import parse_iso
from agentflow.pack import PackError
from agentflow.plan_view import run_plan
from agentflow.run_control import available_actions
from agentflow.run_files import _head
from agentflow.state import OPEN_HOLDS, TERMINAL, Run, display_status, list_runs
from agentflow.views import _label, step_title

RECENT_S = 30 * 60
DECISIONS = ("approve", "answer", "reject")
_CLI = re.compile(r"`agentflow\s")
_BLANK_LINE = re.compile(r"\n[ \t]*\n")
_BACKTICKED = re.compile(r"`[^`]*`")
_LIST_MARK = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+", re.M)
CLI_PROSE_WORDS = 6  # a paragraph with more words than this outside its commands is guidance, not just a command
_RECENT_ACTIONS = {"phase_approved": "approved", "hitl_approved": "approved", "checkpoint_cleared": "approved",
                   "hitl_answered": "answered", "phase_rejected": "rejected"}
_STOP_TITLES = {
    "blocked": "{label} is blocked",
    "blocked_repeat": "{label} blocked again",
    "guard_violation": "A guard stopped {label}",
    "gate_failed": "{label}: gates still fail",
    "ci_failed": "CI failed",
    "budget_exceeded": "The budget is reached",
    hitl.ENVIRONMENT: "This machine can't run {label}'s gates",
    "preflight_failed": "Preflight failed",
    "checkpoint": "Checkpoint: {label}",
    "max_rounds": "{label} ran out of rounds",
    "review_unresolved": "Review findings left on {label}",
}


def _prose_words(paragraph: str) -> int:
    rest = _LIST_MARK.sub("", _BACKTICKED.sub(" ", paragraph))
    return len(re.findall(r"\w+", rest))


def split_cli(text: str) -> tuple[str, list[str]]:
    """`text` without its command paragraphs, and the paragraphs that hold a backticked `agentflow <command>` (the
    dashboard has buttons for those). A paragraph that is mostly a command (at most `CLI_PROSE_WORDS` words of prose
    around it) leaves the text; one that also carries guidance stays in the text and is listed as well."""
    body: list[str] = []
    cli: list[str] = []
    for paragraph in _BLANK_LINE.split(text):
        if _CLI.search(paragraph):
            cli.append(paragraph.strip())
            if _prose_words(paragraph) > CLI_PROSE_WORDS:
                body.append(paragraph)
        else:
            body.append(paragraph)
    return "\n\n".join(body), cli


def item_title(item: dict) -> str:
    """The item in words, from its kind, phase, key and questions."""
    kind = item.get("kind")
    label = step_title(str(item.get("key") or ""))
    if kind == "phase_approval":
        phase = str(item.get("phase") or "")
        return f"Approve the {PHASE_KINDS[phase]}" if phase in PHASE_KINDS else f"Approve {phase}"
    questions = item.get("questions") or []
    if questions:
        n = len(questions)
        return f"Answer {n} question{'' if n == 1 else 's'} ({label})"
    if kind in _STOP_TITLES:
        return _STOP_TITLES[kind].format(label=label)
    return f"{label} stopped ({kind})"


def approve_note(item: dict) -> str:
    """Whether an approval takes a note, as `hitl.approve` decides: `none` (it refuses one, or there is no approval),
    `required` or `optional`. A parked item follows the kind of the stop that parked (`stop_kind`)."""
    kind = item.get("stop_kind") or item.get("kind")
    # An environment stop takes no note here: a note makes `autonomy.recheck_environment` skip its machine recheck,
    # so only the CLI's `--note` overrides it.
    if kind == "phase_approval" or kind in hitl.CLEARED_BY_APPROVE or kind in ("preflight_failed", hitl.ENVIRONMENT) \
            or item.get("questions"):
        return "none"
    return "required" if kind == "blocked_repeat" else "optional"


def group_of(item: dict) -> str:
    if item.get("kind") == "phase_approval":
        return "approval"
    return "question" if item.get("questions") else "stop"


def _read_text(run: Run, key: object, limit: int) -> tuple[str, bool]:
    """The head of the item's inbox file (`("", False)` when there is none), cut after a newline when over `limit`."""
    if not isinstance(key, str) or not key:
        return "", False
    got = _head(run.dir, run.inbox_path(key), limit)
    if got is None:
        return "", False
    data, size = got
    truncated = size > limit
    if truncated:
        cut = data.rfind(b"\n")
        data = data[:cut + 1] if cut >= 0 else data
    return data.decode("utf-8", errors="replace"), truncated


def _events(run: Run) -> list[dict]:
    try:
        return run.events.read()
    except OSError:
        return []


def _severities(rows: list[dict], phase: object) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        if row["open"] and row["key"].split("/", 1)[0] == phase:
            for finding in row["findings"]:
                severity = finding["severity"] or "unrated"
                counts[severity] = counts.get(severity, 0) + 1
    return counts


def _blast_radius(plan: dict, key: object) -> dict | None:
    prefix = plan.get("prefix")
    if not prefix or not isinstance(key, str):
        return None
    match = re.match(rf"{re.escape(prefix)}/task-(\d+)(?:/|$)", key)
    if match is None:
        return None
    n = int(match.group(1))
    task = next((t for t in plan["tasks"] if t["n"] == n), None)
    if task is None:
        return None
    return {"task": n, "blocks": task["blocks"],
            "independent": [t["n"] for t in plan["tasks"] if t["status"] == "independent"]}


def _constraint_offered(run: Run, item: dict, events: list[dict]) -> bool:
    """Whether `agentflow approve --constraint` would be accepted on this stop: no questions, a stop and not a phase
    approval or a checkpoint, and a gate of the run's own trusted config (never the dashboard's) failed on its key. A
    run whose config cannot be loaded gets no button, as the approval would refuse it too."""
    kind, key = item["kind"], str(item.get("key") or "")
    if item.get("questions") or kind == "phase_approval" or kind in hitl.CLEARED_BY_APPROVE or not key:
        return False
    if not any(e.get("type") == "gate_result" and e.get("passed") is False
               and (str(e.get("key")) == key or str(e.get("key")).startswith(key + "/")) for e in events):
        return False  # no gate failed here, so no config is loaded to ask which gates are constraint gates
    try:
        return bool(hitl.constraint_gates_failed(load_config(run_config_path(run)), run, key))
    except (ConfigError, PackError, OSError):
        return False


def _card(run: Run, repo: Path, cfg: Config, item: dict, events: list[dict], now: datetime, limit: int) -> dict:
    parked = item["kind"] == "parked"
    actions = [a for a in available_actions(run) if a in DECISIONS]
    if parked:
        actions = ["answer"] if item["questions"] else ["approve"]
    elif not item["questions"]:
        actions = [a for a in actions if a != "answer"]  # an answer is for the item that has questions
    if "approve" in actions and _constraint_offered(run, item, events):
        actions.append("approve_constraint")
    text, truncated = _read_text(run, item["key"], limit)
    body, cli = split_cli(text)
    since = parse_iso(item["since"])
    card = {**item, "title": item_title(item), "group": group_of(item), "run_label": _label(run.state, run.id),
            "age_s": max(0, int((now - since).total_seconds())) if since else 0,
            "approve_note": approve_note(item), "actions": actions, "text": body, "cli": cli,
            "text_truncated": truncated, "facts": list(run.state.get("facts") or []), "artifacts": [],
            "findings": {}, "blast_radius": None}
    try:
        if item["kind"] == "phase_approval":
            phase = item["phase"]
            card["artifacts"] = [{"path": a["path"], "kind": a["kind"], "revisions": a["revisions"],
                                  "newest": a["revisions"][-1] if a["revisions"] else None}
                                 for a in run_artifacts(run, repo) if a["phase"] == phase]
            card["findings"] = _severities(run_findings(run, events), phase)
        elif "/task-" in str(item["key"]):
            card["blast_radius"] = _blast_radius(run_plan(run, repo, cfg, events), item["key"])
    except (NotFound, OSError):
        pass
    return card


def _watch(run: Run, events: list[dict]) -> dict | None:
    hold = run.state.get("hold")
    if hold:
        since = next((e.get("ts") for e in reversed(events) if e.get("type") == "run_held"), None)
        until = f" until {hold.get('until')}" if hold.get("until") and hold.get("reason") not in OPEN_HOLDS else ""
        return {"reason": "held", "since": since, "text": f"{hold.get('reason') or 'held'}{until}"}
    if display_status(run) == "stalled":
        since = events[-1].get("ts") if events else None
        return {"reason": "stalled", "since": since, "text": "No runner is working on this run."}
    return None


def _recent(run: Run, events: list[dict], now: datetime) -> list[dict]:
    out = []
    for ev in events:
        action = _RECENT_ACTIONS.get(ev.get("type"))
        when = parse_iso(ev.get("ts"))
        if action is None or ev.get("by") == "runner" or when is None or (now - when).total_seconds() > RECENT_S:
            continue
        key = ev.get("key") if isinstance(ev.get("key"), str) else None
        phase = ev.get("phase") if isinstance(ev.get("phase"), str) else (key or "").split("/", 1)[0] or None
        out.append({"run_id": run.id, "key": key or phase, "phase": phase, "action": action, "by": ev.get("by"),
                    "ts": ev.get("ts")})
    return out


def inbox_view(repo: Path, cfg: Config, now: datetime) -> dict:
    """The Inbox as `{items, watch, recent, counts: {approval, question, stop, watch}}` (see the module doc)."""
    repo = Path(repo)
    runs = [run for run in list_runs(repo) if run.state["status"] not in TERMINAL]
    events = {run.id: _events(run) for run in runs}
    by_id = {run.id: run for run in runs}
    limit = artifact_max_bytes(cfg)
    items = [_card(by_id[item["run_id"]], repo, cfg, item, events[item["run_id"]], now, limit)
             for item in hitl.inbox_items(repo, runs)]
    items.sort(key=lambda i: parse_iso(i["since"]) or now)  # oldest first; a stable sort keeps a run's own order
    with_items = {item["run_id"] for item in items}
    watch, recent = [], []
    for run in runs:
        recent += _recent(run, events[run.id], now)
        row = None if run.id in with_items else _watch(run, events[run.id])
        if row is not None:
            watch.append({"run_id": run.id, "run_label": _label(run.state, run.id), **row})
    recent.sort(key=lambda r: parse_iso(r["ts"]) or now, reverse=True)
    counts = {group: sum(1 for i in items if i["group"] == group) for group in ("approval", "question", "stop")}
    return {"items": items, "watch": watch, "recent": recent, "counts": {**counts, "watch": len(watch)}}
