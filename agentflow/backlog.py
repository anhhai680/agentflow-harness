"""The dashboard's Backlog: the cards a developer pulls into a run.

Two kinds of items share the column. Capability-map modules come from the map file (cached in
`runs/queue.json`); cards added on the page live in `runs/backlog.jsonl`, an append-only `EventLog` that is
replayed into `Backlog`. A card is deliberately not a run: it has no worktree, so it forks from a current base
branch when it starts, and the run state machine stays untouched.

Nothing starts by itself. `begin_start` re-checks the rules and records `item_starting`; `run_start` (on a worker
thread, one start at a time) calls `spawn_run` and records `item_started` or `item_failed`. A start that was
interrupted is never repeated: `settle_interrupted` turns it into a failure for a person to look at.

Names here are agentflow's own vocabulary (`issue`, `task`, `topic`, `new_module`, `after`). Lanes, phases, the map
file and the base branch all come from `config.yaml`; this module names none of them.

Item keys are typed so they cannot clash: `module:<id>`, `card:<id>` and `run:<id>`.
"""

from __future__ import annotations

import json
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agentflow import intake, module_queue, sources
from agentflow.conditions import first_phase_with_work, make_ctx
from agentflow.config import check_lane
from agentflow.events import EventLog, nfc
from agentflow.gitops import fetch_remote_branch, sync_branch
from agentflow.state import TERMINAL, Run, list_runs, runs_root
from agentflow.views import lane_first_phase, lane_phases

if TYPE_CHECKING:
    from agentflow.config import Config

TEXT_MAX = 16 * 1024
LABEL_MAX = 80
BY = "dashboard"
LOG_NAME = "backlog.jsonl"
STATE_NAME = "backlog_state.json"
INPUT_FIELDS = ("issue", "task", "topic", "new_module")
FIELDS = (*INPUT_FIELDS, "after")
LIVE = ("backlog", "starting")
INTERRUPTED = ("interrupted: the dashboard stopped while this card was starting. Check `git branch` and "
               "`agentflow status` for a run it may have created, then press Start if there is none")

_SPAWN_LOCK = threading.Lock()  # one start at a time: also closes the check-then-create race on a branch name


class Invalid(ValueError):
    """A request that is wrong in itself (a bad field): the dashboard answers 400."""


class Refused(Exception):
    """A request the Backlog's rules refuse right now (blocked, duplicate): the dashboard answers 409."""


# --------------------------------------------------------------------------- the card log


@dataclass
class Item:
    """A card, replayed from `backlog.jsonl`.

    `source` is `form` for a card added on the page and `map` for the record a capability-map module gets when
    someone first presses Start on it (so a failed start has somewhere to keep its reason).
    """

    id: str
    lane: str
    kind: str  # "task" or "module"
    params: dict
    new_module: dict | None
    after: list[str]
    to_phase: str | None
    label: str
    source: str = "form"
    status: str = "backlog"  # backlog | starting | started | removed
    run_id: str | None = None
    failure: str | None = None

    @property
    def key(self) -> str:
        return f"module:{self.params['module']}" if self.source == "map" else f"card:{self.id}"

    @property
    def deps(self) -> list[str]:
        """The `module:` keys a new module's own dependencies add to `after`."""
        return [f"module:{d}" for d in (self.new_module or {}).get("deps", [])]


class Backlog:
    """Every item in `backlog.jsonl`, in the order they were added."""

    def __init__(self, items: dict[str, Item]) -> None:
        self.items = items

    @classmethod
    def load(cls, repo: Path) -> Backlog:
        items: dict[str, Item] = {}
        for ev in EventLog(runs_root(repo) / LOG_NAME).read():
            kind, ident = ev.get("type"), ev.get("id")
            if kind == "item_added" and isinstance(ident, str):
                items[ident] = Item(
                    id=ident, lane=str(ev.get("lane")), kind=str(ev.get("kind") or "task"),
                    params=dict(ev.get("params") or {}), new_module=ev.get("new_module"),
                    after=list(ev.get("after") or []), to_phase=ev.get("to_phase"),
                    label=str(ev.get("label") or ident), source=str(ev.get("source") or "form"),
                )
            elif ident in items:
                item = items[ident]
                if kind == "item_starting":
                    item.status, item.failure = "starting", None
                elif kind == "item_started":
                    item.status, item.run_id, item.failure = "started", ev.get("run_id"), None
                elif kind == "item_failed":
                    item.status, item.failure = "backlog", str(ev.get("reason") or "the start failed")
                elif kind == "item_removed":
                    item.status = "removed"
        return cls(items)

    def live(self) -> list[Item]:
        return [i for i in self.items.values() if i.status in LIVE]

    def cards(self) -> list[Item]:
        """The cards added on the page that are still on the board (not removed)."""
        return [i for i in self.items.values() if i.source == "form" and i.status != "removed"]

    def overlay(self, module: str) -> Item | None:
        """The newest record of a start attempt for map module `module`."""
        found = [i for i in self.items.values() if i.source == "map" and i.params.get("module") == module]
        return found[-1] if found else None


def _log(repo: Path) -> EventLog:
    return EventLog(runs_root(repo) / LOG_NAME)


# --------------------------------------------------------------------------- settings


def new_task(cfg: Config) -> dict[str, list[str]]:
    """`lane: its form fields` from `dashboard.backlog.new_task`, for lanes that exist."""
    block = module_queue.backlog_settings(cfg).get("new_task")
    if not isinstance(block, dict):
        return {}
    return {str(lane): [str(f) for f in fields] for lane, fields in block.items()
            if lane in cfg.lanes and isinstance(fields, list)}


def lane_labels(cfg: Config) -> dict[str, str]:
    """`lane: the words the page shows for it` from `dashboard.backlog.lane_labels`, for lanes that exist."""
    block = module_queue.backlog_settings(cfg).get("lane_labels")
    if not isinstance(block, dict):
        return {}
    return {str(lane): str(label) for lane, label in block.items() if lane in cfg.lanes}


def _map_nodes(cfg: Config, repo: Path) -> list[sources.ModuleNode]:
    path = repo / cfg.project.docs["capability_map"]
    if module_queue.module_lane(cfg) is None or not path.is_file():
        return []
    return sources.capability_map(path)


# --------------------------------------------------------------------------- validation


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise Invalid(f"{name} must be text")
    text = nfc(value).strip()
    if not text:
        raise Invalid(f"{name} must not be empty")
    if "\0" in text:
        raise Invalid(f"{name} must not contain NUL bytes")
    if len(text) > TEXT_MAX:
        raise Invalid(f"{name} must be at most {TEXT_MAX} characters")
    return text


def _one_line(text: str, limit: int = LABEL_MAX) -> str:
    line = " ".join(text.split())
    return line if len(line) <= limit else line[: limit - 1] + "…"


def _live_runs(runs: list[Run]) -> list[Run]:
    return [r for r in runs if r.state["status"] not in TERMINAL]


def _label_of_run(run: Run) -> str:
    params = run.state.get("params") or {}
    if params.get("module"):
        return str(params["module"])
    if params.get("issue"):
        return f"issue #{params['issue']}"
    return _one_line(str(params.get("task") or params.get("topic") or run.id), 40)


def _check_after(cfg: Config, repo: Path, after: Any, nodes: list[sources.ModuleNode], book: Backlog) -> list[str]:
    if after in (None, ""):
        return []
    if not isinstance(after, (list, tuple)) or not all(isinstance(a, str) for a in after):
        raise Invalid("after must be a list of item keys")
    modules = {n.id for n in nodes}
    cards = {i.id for i in book.items.values() if i.source == "form" and i.status != "removed"}
    out: list[str] = []
    for ref in after:
        kind, _, name = ref.partition(":")
        known = ((kind == "module" and name in modules) or (kind == "card" and name in cards)
                 or (kind == "run" and name != "" and (runs_root(repo) / name / "events.jsonl").is_file()
                     and "/" not in name))
        if not known:
            raise Invalid(f"runs after {ref!r}: no such item")
        if ref not in out:
            out.append(ref)
    return out


def _new_module_fields(value: Any, nodes: list[sources.ModuleNode], book: Backlog) -> dict:
    if not isinstance(value, dict):
        raise Invalid("new_module must have a module id, a responsibility and dependencies")
    module = str(value.get("module") or "")
    if not module_queue._MODULE_ID.fullmatch(module):
        raise Invalid(f"module id {module!r} must be a lowercase slug (letters, digits, - and _)")
    responsibility = " ".join(_text(value.get("responsibility"), "responsibility").split())
    deps = value.get("deps") or []
    if not isinstance(deps, list) or not all(isinstance(d, str) for d in deps):
        raise Invalid("deps must be a list of module ids")
    in_map = {n.id for n in nodes}
    if module in in_map:
        raise Invalid(f"{module} is already in the capability map")
    outside = [d for d in deps if d not in in_map]
    if outside:
        raise Invalid(f"{', '.join(outside)} is not in the capability map: a new module can only depend on modules "
                      "already there (add the second one after the first merges)")
    if any(i.new_module and i.params.get("module") == module for i in book.items.values()
           if i.status in (*LIVE, "started") and i.source == "form"):
        raise Refused(f"a card for module {module} already exists")
    return {"module": module, "responsibility": responsibility, "deps": list(dict.fromkeys(deps))}


def add(cfg: Config, repo: Path, lane: str, fields: dict, after: Any = (), to_phase: str | None = None) -> Item:
    """Add a card to Backlog without starting it. `Invalid` for a bad field, `Refused` for a duplicate."""
    repo = Path(repo)
    allowed = new_task(cfg).get(lane)
    try:
        check_lane(cfg, lane)
    except ValueError as exc:
        raise Invalid(str(exc)) from exc
    if allowed is None:
        raise Invalid(f"lane {lane!r} cannot be added from the dashboard")
    if not isinstance(fields, dict):
        raise Invalid("fields must be an object")
    stray = [k for k in fields if k not in allowed or k not in INPUT_FIELDS]
    if stray:
        raise Invalid(f"lane {lane} takes {', '.join(f for f in allowed if f != 'after')}, not {', '.join(stray)}")
    filled = [k for k in INPUT_FIELDS if k in allowed and fields.get(k) not in (None, "")]
    if not filled:
        raise Invalid(f"fill in {' or '.join(f for f in allowed if f in INPUT_FIELDS)}")
    if to_phase is not None:
        from agentflow.pipeline import RunOptions, _check_range

        try:
            _check_range(cfg, lane, RunOptions(to_phase=to_phase))
        except ValueError as exc:
            raise Invalid(str(exc)) from exc
    nodes = _map_nodes(cfg, repo)
    log = _log(repo)
    with log.locked():
        book = Backlog.load(repo)
        params: dict[str, str] = {}
        new_module: dict | None = None
        kind = "task"
        for name in filled:
            if name == "issue":
                try:
                    params["issue"] = intake.issue_number(_text(fields["issue"], "issue"))
                except ValueError as exc:
                    raise Invalid(str(exc)) from exc
            elif name in ("task", "topic"):
                params[name] = _text(fields[name], name)
            else:
                new_module = _new_module_fields(fields["new_module"], nodes, book)
                params["module"], kind = new_module["module"], "module"
        refs = _check_after(cfg, repo, after, nodes, book)
        if "issue" in params:
            _refuse_duplicate_issue(params["issue"], book, list_runs(repo))
        if new_module is not None:
            clash = [r for r in _live_runs(list_runs(repo)) if (r.state.get("params") or {}).get("module") == params["module"]]
            if clash:
                raise Refused(f"module {params['module']} already has a live run ({clash[0].id})")
        label = (f"{new_module['module']} — {_one_line(new_module['responsibility'], 50)}" if new_module
                 else f"issue #{params['issue']}" if "issue" in params
                 else _one_line(params.get("task") or params.get("topic") or ""))
        ident = uuid.uuid4().hex[:8]
        log.append("item_added", by=BY, id=ident, lane=lane, kind=kind, source="form", params=params,
                   new_module=new_module, after=refs, to_phase=to_phase, label=label)
    return Backlog.load(repo).items[ident]


def _refuse_duplicate_issue(issue: str, book: Backlog, runs: list[Run]) -> None:
    for item in book.items.values():
        if item.status in (*LIVE, "started") and item.params.get("issue") == issue and item.status != "started":
            raise Refused(f"issue #{issue} is already in Backlog")
    for run in _live_runs(runs):
        if str((run.state.get("params") or {}).get("issue")) == issue:
            raise Refused(f"issue #{issue} already has a live run ({run.id})")


def remove(cfg: Config, repo: Path, item_id: str) -> None:
    """Take a card off Backlog. Refused while another card runs after it, or once it has become a run."""
    repo = Path(repo)
    log = _log(repo)
    with log.locked():
        book = Backlog.load(repo)
        item = book.items.get(item_id)
        if item is None or item.status == "removed":
            raise Refused("no such card")
        if item.source != "form":
            raise Refused("a capability-map module lives in the map; it cannot be removed here")
        if item.status != "backlog":
            raise Refused(f"the card is {'starting' if item.status == 'starting' else 'already a run'}")
        waiting = [i for i in book.items.values()
                   if i.status in LIVE and i.id != item.id and f"card:{item.id}" in i.after]
        if waiting:
            names = ", ".join(f"{w.label} ({w.id})" for w in waiting)
            raise Refused(f"{item.label} cannot be removed: {names} runs after it. Remove those first")
        log.append("item_removed", by=BY, id=item.id)


# --------------------------------------------------------------------------- the picture


def _state_path(repo: Path) -> Path:
    return runs_root(repo) / STATE_NAME


def _load_state(repo: Path) -> dict:
    try:
        data = json.loads(_state_path(repo).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_state(repo: Path, **changes: Any) -> None:
    data = _load_state(repo)
    data.update(changes)
    path = _state_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _picture(cfg: Config, repo: Path) -> dict:
    """Everything the board and the start rules need, read from files (no `gh`, no git)."""
    runs = list_runs(repo)
    nodes = _map_nodes(cfg, repo)
    if not nodes and module_queue.module_lane(cfg) and not (repo / cfg.project.docs["capability_map"]).is_file():
        # no map file here: show what an earlier `agentflow queue` recorded, if anything
        nodes = [sources.ModuleNode(m["id"], list(m.get("deps") or [])) for m in module_queue.QueueState.load(repo).modules]
    mod_runs = module_queue.feature_runs(cfg, repo, runs)
    entries = module_queue.cached_entries(repo, nodes, mod_runs)
    book = Backlog.load(repo)
    state = _load_state(repo)
    merged = {k: v for k, v in (state.get("merged") or {}).items() if isinstance(k, str)}
    view = module_queue.dependency_view(
        nodes, entries, book.cards(), merged=merged, runs={r.id: r for r in runs},
    )
    return {"runs": runs, "nodes": nodes, "entries": entries, "book": book, "view": view, "state": state}


def _label_of(key: str, pic: dict) -> str:
    kind, _, name = key.partition(":")
    if kind == "card":
        item = pic["book"].items.get(name)
        return item.label if item else name
    if kind == "run":
        run = next((r for r in pic["runs"] if r.id == name), None)
        return _label_of_run(run) if run else name
    return name


def _first_phase(cfg: Config, repo: Path, lane: str, module: str | None) -> str | None:
    """The column a card moves to when it starts: its lane's first phase, or for a map module, the first phase its
    run has work in. A finished spec has nothing left in `define`, so that module's run begins with `plan`."""
    if module is not None:
        found = first_phase_with_work(cfg, lane, make_ctx(cfg, repo, lane, {"module": module}))
        if found is not None:
            return found
    return lane_first_phase(cfg, lane)


def snapshot(cfg: Config, repo: Path) -> dict:
    """The Backlog column for the board: `cards`, the New task form (`new_task`, `lane_phases`, `after_choices`)
    and the last base-branch sync note."""
    repo = Path(repo)
    pic = _picture(cfg, repo)
    view, book, lane = pic["view"], pic["book"], module_queue.module_lane(cfg)
    cards: list[dict] = []

    def make(key: str, kind: str, ident: str, label: str, item_lane: str, item: Item | None, **extra: Any) -> dict:
        info = view.get(key, {"blocked_by": [], "parallel_with": [], "parallel": False})
        failure = item.failure if item is not None else None
        if item is not None and item.status == "starting":
            status = "starting"
        elif info["blocked_by"]:
            status = "blocked"
        else:
            status = "parallel" if info["parallel"] else "ready"
        return {
            "key": key, "kind": kind, "id": ident, "label": label, "lane": item_lane, "status": status,
            "blocked_by": info["blocked_by"],
            "parallel_with": [{"key": k, "label": _label_of(k, pic)} for k in info["parallel_with"]],
            "parallel": info["parallel"], "failure": failure,
            "first_phase": _first_phase(cfg, repo, item_lane, ident if kind == "module" else None),
            "removable": item is not None and item.source == "form" and item.status == "backlog",
            "to_phase": item.to_phase if item else None, **extra,
        }

    by_entry = {e["id"]: e for e in pic["entries"]}
    for node in pic["nodes"]:
        entry = by_entry[node.id]
        if entry["status"] != "backlog":
            continue
        cards.append(make(f"module:{node.id}", "module", node.id, node.id, lane or "", book.overlay(node.id),
                          deps=list(node.deps), note=entry.get("blocked")))
    for item in book.cards():
        if item.status in ("started", "removed"):
            continue
        cards.append(make(f"card:{item.id}", "card", item.id, item.label, item.lane, item,
                          deps=[*item.after, *item.deps], new=item.new_module is not None))
    live_runs = [{"key": f"run:{r.id}", "label": _label_of_run(r), "kind": "run"} for r in _live_runs(pic["runs"])]
    choices = [{"key": c["key"], "label": c["label"], "kind": c["kind"]} for c in cards if c["kind"] == "card"]
    choices += live_runs
    choices += [{"key": f"module:{n.id}", "label": n.id, "kind": "module"} for n in pic["nodes"]
                if by_entry[n.id]["status"] != "done"]
    forms = new_task(cfg)
    return {
        "cards": cards,
        "new_task": forms,
        "lane_phases": {ln: lane_phases(cfg, ln) for ln in forms},
        "lane_labels": lane_labels(cfg),
        "after_choices": choices,
        "modules": [n.id for n in pic["nodes"]],
        "sync_note": pic["state"].get("sync_note") or "",
    }


# --------------------------------------------------------------------------- starting


@dataclass
class StartJob:
    item_id: str
    key: str
    lane: str
    params: dict
    to_phase: str | None
    new_module: dict | None
    label: str = ""


def parse_key(key: Any) -> tuple[str, str]:
    kind, _, name = str(key).partition(":")
    if kind not in ("module", "card") or not name:
        raise Invalid("name a card (card:<id>) or a module (module:<id>)")
    return kind, name


def begin_start(cfg: Config, repo: Path, key: str) -> StartJob:
    """Check that `key` may start now and record `item_starting`. `Refused` (409) says why not."""
    repo = Path(repo)
    kind, name = parse_key(key)
    log = _log(repo)
    with log.locked():
        pic = _picture(cfg, repo)
        book: Backlog = pic["book"]
        lane = module_queue.module_lane(cfg)
        if kind == "module":
            node = next((n for n in pic["nodes"] if n.id == name), None)
            if node is None:
                raise Refused(f"module {name} is not in the capability map"
                              + ("" if lane else " (dashboard.backlog.module_lane is not set)"))
            if {e["id"]: e for e in pic["entries"]}[name]["status"] != "backlog":
                raise Refused(f"module {name} already has a run or is done")
            item = book.overlay(name)
            params, new_module, to_phase, item_lane = {"module": name}, None, None, lane or ""
        else:
            item = book.items.get(name)
            if item is None or item.source != "form" or item.status == "removed":
                raise Refused("no such card")
            params, new_module, to_phase, item_lane = dict(item.params), item.new_module, item.to_phase, item.lane
        if item is not None and item.status == "starting":
            raise Refused(f"{key} is already starting")
        if item is not None and item.status == "started" and kind == "card":
            raise Refused(f"{key} has already started")
        info = pic["view"].get(key)
        if info and info["blocked_by"]:
            raise Refused(f"{_label_of(key, pic)} is blocked: runs after "
                          + ", ".join(b["text"] for b in info["blocked_by"]))
        clash = _live_clash(params, item_lane, pic["runs"])
        if clash:
            raise Refused(clash)
        if item is None or (kind == "module" and item.status == "started"):  # a card was found above, so None is a module
            ident = uuid.uuid4().hex[:8]
            log.append("item_added", by=BY, id=ident, lane=item_lane, kind="module", source="map", params=params,
                       new_module=None, after=[], to_phase=None, label=name)
        else:
            ident = item.id
        log.append("item_starting", by=BY, id=ident)
    return StartJob(ident, key, item_lane, params, to_phase, new_module, _label_of(key, pic))


def _live_clash(params: dict, lane: str, runs: list[Run]) -> str | None:
    for run in _live_runs(runs):
        have = run.state.get("params") or {}
        for name in ("module", "issue"):
            if name in params and str(have.get(name)) == str(params[name]) and (name != "module" or run.state.get("pipeline") == lane):
                return f"{name} {params[name]} already has a live run ({run.id})"
    return None


def _reason(exc: BaseException) -> str:
    text = next((ln.strip() for ln in str(exc).splitlines() if ln.strip()), "") or type(exc).__name__
    return text[:300]


def run_start(
    cfg: Config, repo: Path, job: StartJob, *, spawn: Any = None, config: Path | None = None,
) -> str | None:
    """Start the run for `job` (slow: it may call `gh` and `git`), recording `item_started` or `item_failed`.

    Returns the run id, or None when the start failed (the card stays in Backlog with the reason)."""
    repo = Path(repo)
    spawn = spawn or module_queue.spawn_run
    log = _log(repo)
    with _SPAWN_LOCK:
        if module_queue.backlog_settings(cfg).get("sync_base", False):
            _save_state(repo, sync_note=sync_branch(repo, cfg.project.fork_from))
        try:
            run_id = spawn(repo, job.lane, job.params, to_phase=job.to_phase, new_module=job.new_module, config=config)
        except (module_queue.StartFailed, OSError, ValueError) as exc:
            log.append("item_failed", by=BY, id=job.item_id, reason=_reason(exc))
            return None
        except BaseException as exc:
            log.append("item_failed", by=BY, id=job.item_id, reason=f"{type(exc).__name__}: {_reason(exc)}")
            raise
        log.append("item_started", by=BY, id=job.item_id, run_id=run_id)
    return run_id


def settle_interrupted(repo: Path) -> list[str]:
    """Turn every start left half-done (a crash or a stop between `item_starting` and its outcome) into a failure."""
    repo = Path(repo)
    log = _log(repo)
    settled: list[str] = []
    with log.locked():
        for item in Backlog.load(repo).items.values():
            if item.status == "starting":
                log.append("item_failed", by=BY, id=item.id, reason=INTERRUPTED)
                settled.append(item.id)
    return settled


# --------------------------------------------------------------------------- keeping it current


def refresh(cfg: Config, repo: Path) -> dict:
    """Bring the base branch up to date, re-assess the map modules and re-check what cards run after.

    It only refreshes what the cards show and never starts anything. It skips the assessment when
    `agentflow queue` holds the queue lock."""
    repo = Path(repo)
    note = sync_branch(repo, cfg.project.fork_from) if module_queue.backlog_settings(cfg).get("sync_base", False) else ""
    # origin/<fork_from> is where a merged plan lands; fetching it never touches a local branch or the work tree
    fetch_remote_branch(repo, cfg.project.fork_from)
    skipped = False
    try:
        with module_queue.queue_lock(repo):
            if _map_nodes(cfg, repo):
                module_queue.assess_modules(cfg, repo)
            merged = _recheck_after(cfg, repo)
    except module_queue.QueueBusy:
        skipped, merged = True, None
    changes: dict[str, Any] = {"sync_note": note, "at": datetime.now(UTC).isoformat(timespec="seconds")}
    if merged is not None:
        changes["merged"] = merged
    _save_state(repo, **changes)
    return {"note": note, "skipped": skipped}


def _recheck_after(cfg: Config, repo: Path) -> dict[str, bool | None]:
    """Whether the pull request of every run that some card or run-ref waits on is merged (once true, always)."""
    book = Backlog.load(repo)
    runs = {r.id: r for r in list_runs(repo)}
    wanted: set[str] = set()
    for item in book.live():
        for ref in item.after:
            kind, _, name = ref.partition(":")
            target = book.items.get(name) if kind == "card" else None
            if kind == "run":
                wanted.add(name)
            elif target is not None and target.run_id:
                wanted.add(target.run_id)
    previous = _load_state(repo).get("merged") or {}
    result: dict[str, bool | None] = {k: v for k, v in previous.items() if v is True}
    for run_id in wanted:
        if result.get(run_id) is True:
            continue
        run = runs.get(run_id)
        branch = run.state.get("branch") if run else None
        result[run_id] = module_queue.branch_is_merged(cfg, repo, str(branch))[0] if branch else None
    return result
