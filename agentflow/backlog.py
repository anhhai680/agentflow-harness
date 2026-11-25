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

Open GitHub issues that carry `issues.sync_label` (every open issue, when it is `*`) are a third kind: `refresh` reads them with `gh issue list` into
`backlog_state.json`, and `snapshot` shows them from there (it never calls `gh`). An issue that carries
`issues.in_progress_label` with no run on this machine is "taken": someone else picked it up, so it cannot start.
Nothing from an issue's text reaches an argv, a module id, a map row or a refusal: an issue-backed run in the
module lane works on the derived module `issue-<N>`, and the issue is named only by its number.

Open Dependabot alerts are a fourth kind, when `vuln.sync` is on: `refresh` reads them with one `gh api` call (and
one `gh pr list`) into `backlog_state.json`, grouped by package and manifest. Only `vuln.py`'s validators let an
alert's package, manifest or advisory ids into the cache; its titles and summaries never reach the board.

A group's card is `alert:<N>` (N: its lowest alert number). Start runs the vuln lane on the lowest alert in the
group that has a patched version, and the card reads "in progress" while a run of that package and manifest holds it.

Item keys are typed so they cannot clash: `module:<id>`, `card:<id>`, `issue:<number>`, `alert:<number>` and `run:<id>`.
"""

from __future__ import annotations

import dataclasses
import json
import re
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Literal

from agentflow import backlog_file, claim_sync, claims, finished, intake, module_queue, sources, vuln, work_source
from agentflow.backlog_file import BacklogItem
from agentflow.conditions import first_phase_with_work, make_ctx
from agentflow.config import check_lane
from agentflow.events import EventLog, nfc
from agentflow.gitops import GhError, commit_at, fetch_remote_branch, gh_json, sync_branch
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
NEW_MODULE_IN_FILE = "in a backlog pack a new module is an item of backlog.yml: POST /api/backlog/file"
INTERRUPTED = ("interrupted: the dashboard stopped while this card was starting. Check `git branch` and "
               "`agentflow status` for a run it may have created, then press Start if there is none")

ISSUE_MODULE = "issue-{number}"  # the module of an issue-backed run in the module lane: never from the issue's text
ISSUE_RESPONSIBILITY = "GitHub issue #{number}"  # its capability-map row's responsibility, fixed text too
ISSUES_MAX = 100  # the most synced issues the Backlog shows
ALL_ISSUES = "*"  # an `issues.sync_label` that syncs every open issue, whatever its labels
TITLE_MAX = 200
ISSUE_LIST_FIELDS = "number,title,labels,url,blockedBy,blocking"
ISSUE_LIST_FIELDS_OLD = "number,title,labels,url"  # what a `gh` without the relationship fields can list
REFS_MAX = 100  # more nodes than this, or a `totalCount` above the nodes kept, is an incomplete list
REF_URL_MAX = 512
ISSUE_URL = re.compile(r"https://[A-Za-z0-9.-]+(?::\d{1,5})?/[\w.-]+/[\w.-]+/issues/(\d+)")
ISSUE_REF_URL = re.compile(r"https://[A-Za-z0-9.-]+(?::\d{1,5})?/([\w.-]+/[\w.-]+)/issues/(\d+)", re.ASCII)
REPO_NAME = re.compile(r"[\w.-]+/[\w.-]+", re.ASCII)
REPO_MAX = 200
# why an issue's blocked-by / blocking lists are not known: the only words a cached `deps_note` may carry
UNKNOWN_OLD_GH = "update gh"
UNKNOWN_CACHE = "refresh the board to read them"
UNKNOWN_TRUNCATED = "the list is longer than GitHub returned"
UNKNOWN_BAD = "a blocker could not be read"
UNKNOWN_REASONS = (UNKNOWN_OLD_GH, UNKNOWN_CACHE, UNKNOWN_TRUNCATED, UNKNOWN_BAD)
TAKEN = ("carries the in-progress label, but no run on this machine is working on it: someone else may be. "
         "Remove the label on GitHub to free it")

_SPAWN_LOCK = threading.Lock()  # one start at a time: also closes the check-then-create race on a branch name
_STATE_LOCK = threading.Lock()  # backlog_state.json is read, changed and written by the refresh thread and by starts
_FILE_LOCK = threading.Lock()  # one page edit of the backlog file at a time: its sha check, read and write are one step


class Invalid(ValueError):
    """A request that is wrong in itself (a bad field): the dashboard answers 400."""


class Refused(Exception):
    """A request the Backlog's rules refuse right now (blocked, duplicate): the dashboard answers 409."""


# --------------------------------------------------------------------------- the card log


@dataclass
class Item:
    """A card, replayed from `backlog.jsonl`.

    `source` is `form` for a card added on the page, `map` for the record a capability-map module gets when
    someone first presses Start on it (so a failed start has somewhere to keep its reason), and `issue` for the
    same record of a synced GitHub issue, and `alert` for that of a Dependabot alert group.
    """

    id: str
    lane: str
    kind: str  # "task", "module", "issue" or "alert"
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
        if self.source == "map":
            return f"module:{self.params['module']}"
        if self.source == "issue":
            return f"issue:{self.params['issue']}"
        if self.source == "alert":
            return f"alert:{self.params['alert']}"
        return f"card:{self.id}"

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

    def issue_overlay(self, number: str) -> Item | None:
        """The newest record of a start attempt for synced issue `number`."""
        found = [i for i in self.items.values() if i.source == "issue" and i.params.get("issue") == number]
        return found[-1] if found else None

    def alert_overlay(self, number: str) -> Item | None:
        """The newest record of a start attempt for the alert group whose lowest alert is `number`."""
        found = [i for i in self.items.values() if i.source == "alert" and i.params.get("alert") == number]
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
    if module_queue.module_lane(cfg) is None:
        return []
    return work_source.items(cfg, repo)


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
    if params.get("alert"):
        return f"alert #{params['alert']}"
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


def unremovable(cfg: Config) -> str:
    """Why a module listed by the work source cannot be removed from the Backlog."""
    noun = work_source.noun(cfg)
    if noun == "capability map":
        return "a capability-map module lives in the map; it cannot be removed here"
    return f"a module from the {noun} lives there; it cannot be removed here"


def _reopen_hint(module: str, reason: str) -> str | None:
    """How to add work to a module that is done for `reason`; None when only a new module id will do (merged)."""
    done_on = finished.plan_complete("")
    if reason.startswith(done_on):
        return (f"add unticked `- [ ]` steps for the new work to its plan on {reason[len(done_on):]}, "
                "and it comes back to Backlog")
    if reason in (finished.SPEC_SAYS, finished.BY_HAND):
        return f"run `agentflow backlog undone {module}`"
    if reason == finished.ISSUE_CLOSED:
        return f"reopen its issue, then run `agentflow backlog undone {module}`"
    if reason == finished.FILE_SAYS:
        return f"set its status in the backlog file: `agentflow backlog set {module} status=ready`"
    return None


def _new_module_fields(value: Any, nodes: list[sources.ModuleNode], book: Backlog,
                       noun: str = "capability map", done: dict[str, str] | None = None) -> dict:
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
    if module in in_map and module in (done or {}):  # a done module has no Start button to point at
        reason = (done or {})[module]
        hint = _reopen_hint(module, reason)
        message = f"{module} is already done ({reason}). Give the new work its own module id"
        raise Invalid(message + (f", or reopen {module}: {hint}" if hint else ""))
    if module in in_map:
        if noun == "capability map":
            raise Invalid(f"{module} is already in the {noun}")
        raise Invalid(f"{module} is already a module in the {noun}: start module:{module} instead")
    outside = [d for d in deps if d not in in_map]
    if outside:
        raise Invalid(f"{', '.join(outside)} is not in the {noun}: a new module can only depend on modules "
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
    if "new_module" in filled and work_source.kind(cfg) == "backlog":
        raise Invalid(NEW_MODULE_IN_FILE)
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
                new_module = _new_module_fields(fields["new_module"], nodes, book, work_source.noun(cfg),
                                                finished.recorded(repo, hand_marks=work_source.hand_marks(cfg)))
                params["module"], kind = new_module["module"], "module"
        refs = _check_after(cfg, repo, after, nodes, book)
        if "issue" in params:
            _refuse_duplicate_issue(params["issue"], book, list_runs(repo))
        if new_module is not None:
            clash = [r for r in _live_runs(list_runs(repo)) if (r.state.get("params") or {}).get("module") == params["module"]]
            if clash:
                raise Refused(f"module {params['module']} already has a live run ({clash[0].id})")
        label = (f"{new_module['module']} — {_one_line(new_module['responsibility'], 50)}"
                 + (f" · issue #{params['issue']}" if "issue" in params else "") if new_module
                 else f"issue #{params['issue']}" if "issue" in params
                 else _one_line(params.get("task") or params.get("topic") or ""))
        ident = uuid.uuid4().hex[:8]
        log.append("item_added", by=BY, id=ident, lane=lane, kind=kind, source="form", params=params,
                   new_module=new_module, after=refs, to_phase=to_phase, label=label)
    return Backlog.load(repo).items[ident]


def _refuse_duplicate_issue(issue: str, book: Backlog, runs: list[Run]) -> None:
    for item in book.items.values():
        if item.status in LIVE and item.source == "form" and item.params.get("issue") == issue:
            raise Refused(f"issue #{issue} is already in Backlog")
    _refuse_starting_issue(issue, book)
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
            raise Refused(unremovable(cfg))
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
    with _STATE_LOCK:
        data = _load_state(repo)
        data.update(changes)
        path = _state_path(repo)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{threading.get_ident()}.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)


# --------------------------------------------------------------------------- synced GitHub issues


def _fold(name: str) -> str:
    """A label name as GitHub compares it: without case (NFC first, as `intake.is_epic` does)."""
    return nfc(name).casefold()


def _clean_title(value: Any) -> str:
    """An issue title fit to show: printable, one line, at most `TITLE_MAX` characters. It is only ever shown."""
    if not isinstance(value, str):
        return ""
    return _one_line("".join(ch if ch.isprintable() else " " for ch in nfc(value)), TITLE_MAX)


def _here(url: str) -> str | None:
    """The `owner/repo` of a synced issue's own URL, or None when it is not an issue URL."""
    match = ISSUE_REF_URL.fullmatch(url) if isinstance(url, str) and len(url) <= REF_URL_MAX else None
    return match.group(1) if match else None


def _parse_refs(field: Any, here: str | None) -> tuple[list[dict], str]:
    """A `blockedBy` / `blocking` value as refs `{number, state, repo}` and why the list is not known ("" when
    it is complete). `repo` is None for an issue of `here` (compared without case), else its `owner/repo`. The
    reply is untrusted: only a number, a state word and a repo name are kept, never a title or any other text, and
    a node that cannot be read makes the whole list unknown (the good nodes are still returned)."""
    if not isinstance(field, dict):
        return [], UNKNOWN_BAD
    nodes, total = field.get("nodes"), field.get("totalCount")
    if not isinstance(nodes, list) or isinstance(total, bool) or not isinstance(total, int) or total < 0:
        return [], UNKNOWN_BAD
    refs: list[dict] = []
    bad = False
    for node in nodes[:REFS_MAX]:
        number = node.get("number") if isinstance(node, dict) else None
        url = node.get("url") if isinstance(node, dict) else None
        match = ISSUE_REF_URL.fullmatch(url) if isinstance(url, str) and len(url) <= REF_URL_MAX else None
        if (isinstance(number, bool) or not isinstance(number, int) or number <= 0 or match is None
                or match.group(2) != str(number)):
            bad = True
            continue
        try:
            ident = intake.issue_number(str(number))
        except ValueError:
            bad = True
            continue
        repo = match.group(1)
        state = node.get("state")
        refs.append({"number": ident, "state": "closed" if isinstance(state, str) and state.upper() == "CLOSED" else "open",
                     "repo": None if here is not None and repo.casefold() == here.casefold() else repo})
    if bad:
        return refs, UNKNOWN_BAD
    return refs, UNKNOWN_TRUNCATED if len(nodes) > REFS_MAX or total > len(refs) else ""


def _cached_refs(value: Any) -> list[dict] | None:
    """A cached ref list, checked again as untrusted input (the cache file can be edited): each ref has exactly
    the shape `_parse_refs` writes, or the whole list is None."""
    if not isinstance(value, list) or len(value) > REFS_MAX:
        return None
    out: list[dict] = []
    for ref in value:
        if not (isinstance(ref, dict) and set(ref) == {"number", "state", "repo"}):
            return None
        number, state, repo = ref["number"], ref["state"], ref["repo"]
        if not isinstance(number, str) or state not in ("open", "closed"):
            return None
        try:
            if intake.issue_number(number) != number:
                return None
        except ValueError:
            return None
        if repo is not None and not (isinstance(repo, str) and len(repo) <= REPO_MAX and REPO_NAME.fullmatch(repo)):
            return None
        out.append({"number": number, "state": state, "repo": repo})
    return out


def _parse_issues(cfg: Config, reply: Any, *, with_deps: bool = True) -> list[dict]:
    """`gh issue list`'s reply as `{number, title, lane, in_progress, url, blocked_by, blocks, deps_complete,
    deps_note}` entries. Label names are compared and then dropped; an entry with no usable number or no lane is
    left out. With `with_deps` False (an old `gh` was asked without the relationship fields) every entry has no
    refs and its lists are unknown. A reply that is not a list is a `ValueError`."""
    if not isinstance(reply, list):
        raise ValueError("gh issue list printed no list")
    pol = cfg.issues
    by_label = [(_fold(name), lane) for name, lane in pol.lane_labels.items() if lane in cfg.lanes]
    fallback = pol.sync_lane if pol.sync_lane in cfg.lanes else None
    taken = _fold(pol.in_progress_label) if pol.in_progress_label else None
    out: list[dict] = []
    seen: set[str] = set()
    for entry in reply:
        if len(out) >= ISSUES_MAX:
            break
        if not isinstance(entry, dict):
            continue
        number = entry.get("number")
        if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
            continue
        try:
            ident = intake.issue_number(str(number))
        except ValueError:
            continue
        if ident in seen:
            continue
        labels = entry.get("labels")
        names = {_fold(label["name"]) for label in (labels if isinstance(labels, list) else [])
                 if isinstance(label, dict) and isinstance(label.get("name"), str)}
        lane = next((lane for name, lane in by_label if name in names), fallback)
        if lane is None:
            continue
        url = entry.get("url")
        match = ISSUE_URL.fullmatch(url) if isinstance(url, str) else None
        seen.add(ident)
        url = url if match and match.group(1) == ident else ""
        if with_deps:
            blocked_by, why_by = _parse_refs(entry.get("blockedBy"), _here(url))
            blocks, why_blocks = _parse_refs(entry.get("blocking"), _here(url))
            why = UNKNOWN_BAD if UNKNOWN_BAD in (why_by, why_blocks) else why_by or why_blocks
        else:
            blocked_by, blocks, why = [], [], UNKNOWN_OLD_GH
        out.append({"number": ident, "title": _clean_title(entry.get("title")), "lane": lane,
                    "in_progress": taken is not None and taken in names, "url": url,
                    "blocked_by": blocked_by, "blocks": blocks, "deps_complete": not why, "deps_note": why})
    return out


def _old_gh(exc: GhError) -> bool:
    """Whether `gh` failed because it does not know a `--json` field (one older than the relationship fields)."""
    return "unknown json field" in str(exc).lower()


def _synced_words(label: str) -> str:
    """How the board names the synced set: open issues `labelled 'x'`, or no words for `*` (every open issue)."""
    return "" if label == ALL_ISSUES else f" labelled {label!r}"


def sync_issues(cfg: Config, repo: Path) -> str:
    """Read the open issues that carry `issues.sync_label` (every open issue for `*`) into `backlog_state.json`;
    returns the note the board shows ("" when all is well). It never raises: when `gh` fails, the last list stays
    and the note says why."""
    repo = Path(repo)
    label = cfg.issues.sync_label
    if not label:
        if _load_state(repo).get("issues_label"):
            _save_state(repo, issues=[], issues_label="", issues_note="")
        return ""
    old_gh = False
    which = [] if label == ALL_ISSUES else ["--label", label]
    try:
        try:
            reply = gh_json(["issue", "list", *which, "--state", "open", "--json", ISSUE_LIST_FIELDS,
                             "--limit", str(ISSUES_MAX)], cwd=repo)
        except GhError as exc:
            if not _old_gh(exc):
                raise
            old_gh = True  # a `gh` without the relationship fields: ask once more for the rest
            reply = gh_json(["issue", "list", *which, "--state", "open", "--json", ISSUE_LIST_FIELDS_OLD,
                             "--limit", str(ISSUES_MAX)], cwd=repo)
        issues = _parse_issues(cfg, reply, with_deps=not old_gh)
    except (GhError, OSError, ValueError, TypeError, RecursionError) as exc:  # RecursionError: absurdly nested JSON
        note = f"GitHub issues not updated: {_reason(exc)}"
        _save_state(repo, issues_note=note)
        return note
    notes = [f"only the first {ISSUES_MAX} open issues{_synced_words(label)} are shown"] if len(reply) >= ISSUES_MAX else []
    if old_gh:
        notes.append(f"issue dependencies not read: {UNKNOWN_OLD_GH}")
    note = "; ".join(notes)
    _save_state(repo, issues=issues, issues_label=label, issues_note=note,
                issues_at=datetime.now(UTC).isoformat(timespec="seconds"))
    return note


def _synced(cfg: Config, state: dict) -> list[dict]:
    """The cached issues, while they were read for the current `issues.sync_label` and their lane still exists."""
    label = cfg.issues.sync_label
    if not label or state.get("issues_label") != label or not isinstance(state.get("issues"), list):
        return []
    out = []
    for entry in state["issues"]:
        if not (isinstance(entry, dict) and isinstance(entry.get("number"), str) and entry.get("lane") in cfg.lanes):
            continue
        url = entry.get("url")
        match = ISSUE_URL.fullmatch(url) if isinstance(url, str) else None
        blocked_by, blocks = _cached_refs(entry.get("blocked_by")), _cached_refs(entry.get("blocks"))
        complete = blocked_by is not None and blocks is not None and entry.get("deps_complete") is True
        cached_note = entry.get("deps_note")
        out.append({**entry, "url": url if match and match.group(1) == entry["number"] else "",
                    "title": _clean_title(entry.get("title")), "blocked_by": blocked_by or [], "blocks": blocks or [],
                    "deps_complete": complete,
                    "deps_note": "" if complete else cached_note if cached_note in UNKNOWN_REASONS else UNKNOWN_CACHE})
    return out


# --------------------------------------------------------------------------- synced Dependabot alerts


def _alerts_on(cfg: Config) -> bool:
    return bool(cfg.vuln.sync and cfg.vuln.source == "dependabot" and "vuln" in cfg.lanes)


def sync_alerts(cfg: Config, repo: Path) -> str:
    """Read the open Dependabot alerts at or above `vuln.min_severity` into `backlog_state.json`, grouped by package
    and manifest; returns the note the board shows ("" when all is well). It never raises: when `gh` fails, the last
    list stays and the note says why. One call reads the alerts and one the open Dependabot PRs."""
    repo = Path(repo)
    if not _alerts_on(cfg):
        if _load_state(repo).get("alerts"):
            _save_state(repo, alerts=[], alerts_note="")
        return ""
    floor = cfg.vuln.min_severity
    try:
        listing = gh_json(["api", f"{vuln.ALERTS_API}?state=open&severity={vuln.severity_query(floor)}"
                           f"&per_page={vuln.ALERTS_MAX}"], cwd=repo)
        groups, skipped = vuln.group_alerts(listing, floor)
    except (GhError, OSError, ValueError, TypeError, RecursionError) as exc:  # AlertError is a ValueError
        note = f"Dependabot alerts not updated: {_reason(exc)}"
        _save_state(repo, alerts_note=note)
        return note
    notes = []
    try:
        prs = gh_json(["pr", "list", "--author", "app/dependabot", "--state", "open", "--json", "number,title",
                       "--limit", "100"], cwd=repo)
    except (GhError, OSError, ValueError, TypeError, RecursionError):
        prs = []
        notes.append("Dependabot pull requests not read")
    for group in groups:
        group["dependabot_prs"] = vuln.prs_naming(group["package"], prs)
    if skipped:
        notes.append(f"{skipped} alert(s) could not be read and are not shown")
    if len(listing) >= vuln.ALERTS_MAX:
        notes.append(f"only the first {vuln.ALERTS_MAX} open alerts are shown")
    note = "; ".join(notes)
    _save_state(repo, alerts=groups, alerts_note=note, alerts_at=datetime.now(UTC).isoformat(timespec="seconds"))
    return note


def _synced_alerts(cfg: Config, state: dict) -> list[dict]:
    """The cached alert groups that pass `vuln.check_group` and sit at or above the current `vuln.min_severity`."""
    if not _alerts_on(cfg) or not isinstance(state.get("alerts"), list):
        return []
    # A group is filtered by its highest severity only, so after `vuln.min_severity` is raised a below-floor fixable
    # member can stay as Start's target until the next refresh; intake then refuses it with a visible reason.
    groups = (vuln.check_group(entry) for entry in state["alerts"])
    kept: list[dict] = []
    taken: set[int] = set()
    for g in groups:
        if not g or not vuln.severity_at_least(g["severity"], cfg.vuln.min_severity):
            continue
        if taken & set(g["alerts"]):  # a tampered cache: no two cards may share an `alert:N` key
            continue
        taken.update(g["alerts"])
        kept.append(g)
    return kept


def _vuln_runs(runs: list[Run]) -> list[tuple[Run, dict | None]]:
    """Each vuln run here that may hold an alert group, with its `alert.json` facts read once (None when they can't
    be: a run whose facts are unreadable holds a group only by the alert number it was started with)."""
    found: list[tuple[Run, dict | None]] = []
    for run in runs:
        if run.state.get("pipeline") != "vuln" or not module_queue.issue_run_holds(run):
            continue
        try:
            found.append((run, vuln.read_alert(run.dir)))
        except vuln.AlertError:
            found.append((run, None))
    return found


def _int_set(value: Any) -> set[int]:
    return {n for n in value if isinstance(n, int) and not isinstance(n, bool)} if isinstance(value, list) else set()


def _alert_holder(group: dict, vuln_runs: list[tuple[Run, dict | None]]) -> Run | None:
    """The vuln run that holds `group`, preferring a live one and then the newest (`vuln_runs` is oldest first).
    A run that is not finished (live, or stopped on an error, since it can be resumed) holds the group's package in
    its manifest whatever its alerts: its `alert.json` names the group's ecosystem, package and manifest, or the
    alert it was started with (`params.alert`) is in the group, even before `alert.json` exists. A `done` run with
    its pull request open holds it only while one of its alerts is still in the group: once none is open, its fix
    landed and the run is history. A failed, cancelled or archived run does not hold it."""
    want = (group["ecosystem"], group["package"], group["manifest"])
    alerts = set(group["alerts"])
    live: list[Run] = []
    finished_: list[Run] = []
    for run, facts in vuln_runs:
        named = facts is not None and (facts.get("ecosystem"), facts.get("package"), facts.get("manifest")) == want
        if run.state["status"] not in TERMINAL:
            started = str((run.state.get("params") or {}).get("alert") or "")
            if named or (started.isascii() and started.isdigit() and int(started) in alerts):
                live.append(run)
        elif named and facts is not None and _int_set(facts.get("alerts")) & alerts:
            finished_.append(run)
    held = live or finished_
    return held[-1] if held else None


def _issue_blocker(number: str, pic: dict) -> str | None:
    """Why synced issue `number` is not a Backlog card of its own: a run, a card or a module here has it."""
    for run in pic["runs"]:
        if str((run.state.get("params") or {}).get("issue") or "") != number:
            continue
        if run.state["status"] not in TERMINAL:
            return f"issue #{number} already has a live run ({run.id})"
        if run.state["status"] == "done":
            return f"issue #{number} already has a run ({run.id}) whose pull request is open"
    for item in pic["book"].cards():
        if item.status in LIVE and item.params.get("issue") == number:
            return f"issue #{number} is already card:{item.id}"
    module = ISSUE_MODULE.format(number=number)
    if any(n.id == module for n in pic["nodes"]):
        return f"issue #{number} is module {module}: start module:{module} instead"
    return None


def _issue_run_params(cfg: Config, lane: str, number: str, pic: dict) -> tuple[dict, dict | None]:
    """`(params, new_module)` for a run of `lane` from issue `number`. The module lane works on the derived module
    `issue-<N>` (a new map row in a capability map, a new item appended by the run's first commit in a `backlog`
    pack); nothing in either comes from the issue's text."""
    params: dict = {"issue": number}
    if lane != module_queue.module_lane(cfg):
        return params, None
    module = ISSUE_MODULE.format(number=number)
    params["module"] = module
    if any(n.id == module for n in pic["nodes"]):
        if {e["id"]: e for e in pic["entries"]}[module]["status"] != "backlog":
            raise Refused(f"module {module} already has a run or is done")
        return params, None
    if work_source.kind(cfg) in ("capability_map", "backlog"):
        return params, {"module": module, "responsibility": ISSUE_RESPONSIBILITY.format(number=number), "deps": []}
    return params, None


def _refuse_starting_issue(number: str, book: Backlog) -> None:
    """A synced issue whose start is in flight has no run yet, so only its record says it is taken here."""
    record = book.issue_overlay(number)
    if record is not None and record.status == "starting":
        raise Refused(f"issue #{number} is starting from its synced card")


def _taken_now(cfg: Config, repo: Path, number: str) -> str | None:
    """Why issue `number` can't start, asked of GitHub at the moment of starting (the board's list can be a minute
    old): it was closed, someone put the in-progress label on it, or an issue it runs after is not done. None when
    it is free or GitHub can't say (a failed call: the cached decision stands). An old `gh` with no `blockedBy` is
    asked once more for the state and labels alone, and the cached decision on the blockers stands."""
    if not cfg.issues.sync_label:
        return None
    old_gh = False
    try:
        try:
            reply = gh_json(["issue", "view", number, "--json", "state,labels,blockedBy"], cwd=repo)
        except GhError as exc:
            if not _old_gh(exc):
                raise
            old_gh = True
            reply = gh_json(["issue", "view", number, "--json", "state,labels"], cwd=repo)
    except (GhError, OSError, ValueError, RecursionError):
        return None  # the run's own intake asks GitHub again and stops on a real failure
    if not isinstance(reply, dict):
        return None
    if str(reply.get("state") or "OPEN").upper() == "CLOSED":
        return f"issue #{number} was closed on GitHub"
    labels = reply.get("labels")
    taken = _fold(cfg.issues.in_progress_label) if cfg.issues.in_progress_label else None
    if taken and isinstance(labels, list) and any(
            isinstance(label, dict) and isinstance(label.get("name"), str) and _fold(label["name"]) == taken
            for label in labels):
        return f"issue #{number} {TAKEN}"
    if old_gh:
        return None
    state = _load_state(repo)
    synced = _synced(cfg, state)
    # this repository is the one in the issue's own cached url, else in any synced issue's: all are of this one
    urls = [i["url"] for i in synced if i["number"] == number] + [i["url"] for i in synced]
    here = next((found for found in map(_here, urls) if found), None)
    refs, note = _parse_refs(reply.get("blockedBy"), here)
    if note:
        return f"issue #{number}: its dependencies could not be read: {note}"
    runs = {r.id: r for r in list_runs(repo)}
    merged = {k: v for k, v in (state.get("merged") or {}).items() if isinstance(k, str)}
    waiting = [ref for ref in refs if not module_queue.issue_ref_done(ref, runs, merged)]
    if waiting:  # a number, a repository name and a state word only: never a title
        names = ", ".join(f"{ref['repo']}#{ref['number']} (open)" if ref["repo"] else f"issue #{ref['number']} (open)"
                          for ref in waiting)
        return f"issue #{number} is blocked: runs after {names}"
    return None


def _refuse_taken(number: str, pic: dict) -> None:
    entry = next((i for i in pic["issues"] if i["number"] == number), None)
    if entry is not None and entry.get("in_progress") and not any(
            str((r.state.get("params") or {}).get("issue") or "") == number for r in _live_runs(pic["runs"])):
        raise Refused(f"issue #{number} {TAKEN}")


NOTE_MAX = 300


def _file_note(problems: list[backlog_file.Problem]) -> str:
    """The Board's note for a backlog file that cannot be read: its first file-level error (an item's own error
    keeps the item on the Board and is `validate`'s to report), or `""`."""
    for problem in problems:
        if problem.level == "error" and not problem.where.startswith("items["):
            return _one_line(f"backlog file {problem.where} {problem.message}", NOTE_MAX)
    return ""


NOT_STARTABLE = "runs in {lane}: only dashboard.backlog.module_lane items start from the Board"


def _startable(item: BacklogItem | None, lane: str | None) -> bool:
    """Whether a backlog item belongs to the module lane (an item with no `lane:` does)."""
    return item is None or not item.lane or item.lane == lane


def _picture(cfg: Config, repo: Path) -> dict:
    """Everything the board and the start rules need, read from files (no `gh`, no git)."""
    runs = list_runs(repo)
    file_items: list[BacklogItem] = []
    problems: list[backlog_file.Problem] = []
    digest = ""
    if work_source.kind(cfg) == "backlog":  # one read of the file, so the cards, dropped list, note and sha agree
        found, problems, digest = work_source.indexed_digest(cfg, repo)
        file_items = [item for _, item in found]
        nodes: list[sources.ModuleNode] = (
            [i for i in file_items if i.status != "dropped"] if module_queue.module_lane(cfg) else [])
    else:
        nodes = _map_nodes(cfg, repo)
    source = work_source.file(cfg, repo)
    mapped = cfg.work_source is not None and cfg.work_source.kind == "capability_map"
    if not nodes and mapped and module_queue.module_lane(cfg) and source is not None and not source.is_file():
        # no map file here: show what an earlier `agentflow queue` recorded, if anything (a pack with no work
        # source has no modules at all, so the board holds only the cards added on the page)
        nodes = [sources.ModuleNode(m["id"], list(m.get("deps") or [])) for m in module_queue.QueueState.load(repo).modules]
    mod_runs = module_queue.feature_runs(cfg, repo, runs)
    entries = module_queue.cached_entries(repo, nodes, mod_runs, hand_marks=work_source.hand_marks(cfg))
    book = Backlog.load(repo)
    state = _load_state(repo)
    merged = {k: v for k, v in (state.get("merged") or {}).items() if isinstance(k, str)}
    pic = {"runs": runs, "nodes": nodes, "entries": entries, "book": book, "state": state,
           "issues": _synced(cfg, state), "file_items": {i.id: i for i in file_items},
           "dropped": [{"id": i.id, "title": i.title} for i in file_items if i.status == "dropped"],
           "file_note": _file_note(problems), "backlog_sha256": digest}
    pic["alerts"] = _synced_alerts(cfg, state)
    pic["alert_runs"] = {}  # the lowest alert number of each held group -> the run that holds it
    vuln_runs = _vuln_runs(runs) if pic["alerts"] else []
    for group in pic["alerts"]:
        holder = _alert_holder(group, vuln_runs)
        if holder is not None:
            pic["alert_runs"][group["alerts"][0]] = holder
    issues = []
    for entry in pic["issues"]:
        record = book.issue_overlay(entry["number"])
        issues.append({"number": entry["number"], "blocked_by": entry["blocked_by"],
                       "deps_complete": entry["deps_complete"], "on_board": _issue_blocker(entry["number"], pic) is None,
                       "starting": record is not None and record.status == "starting",
                       "module": ISSUE_MODULE.format(number=entry["number"])})
    pic["view"] = module_queue.dependency_view(
        nodes, entries, book.cards(), merged=merged, runs={r.id: r for r in runs}, noun=work_source.noun(cfg),
        issues=issues,
    )
    return pic


def _module_issue(module: str, pic: dict) -> str | None:
    """The synced issue whose derived module (`ISSUE_MODULE`) is `module`, or None."""
    return next((i["number"] for i in pic["issues"] if ISSUE_MODULE.format(number=i["number"]) == module), None)


def _deps_unknown(key: str, params: dict, pic: dict) -> str | None:
    """Why the blockers of the synced issue behind `key` (`issue:N`, a form card whose `params.issue` is a synced
    N, or the module derived from a synced N) are not known: the entry's `deps_note`. None when they are known, or
    `key` is not tied to a synced issue."""
    kind, _, name = key.partition(":")
    if kind == "issue":
        number = name
    elif kind == "card":
        number = str(params.get("issue") or "")
    else:
        number = (_module_issue(name, pic) or "") if kind == "module" else ""
    entry = next((i for i in pic["issues"] if i["number"] == number), None)
    if entry is None or entry["deps_complete"]:
        return None
    return entry["deps_note"] or UNKNOWN_CACHE


def _label_of(key: str, pic: dict) -> str:
    kind, _, name = key.partition(":")
    if kind == "card":
        item = pic["book"].items.get(name)
        return item.label if item else name
    if kind == "run":
        run = next((r for r in pic["runs"] if r.id == name), None)
        return _label_of_run(run) if run else name
    if kind == "issue":
        return f"issue #{name}"
    if kind == "alert":
        return f"alert #{name}"
    return name


def _first_phase(cfg: Config, repo: Path, lane: str, module: str | None) -> str | None:
    """The column a card moves to when it starts: its lane's first phase, or for a map module, the first phase its
    run has work in. A finished spec has nothing left in `define`, so that module's run begins with `plan`."""
    if module is not None:
        found = first_phase_with_work(cfg, lane, make_ctx(cfg, repo, lane, {"module": module}))
        if found is not None:
            return found
    return lane_first_phase(cfg, lane)


def _committed(item: BacklogItem) -> str:
    """How `refresh` records an item of the file at the `fork_from` tip, to compare with the work tree's."""
    return json.dumps(dataclasses.asdict(item), sort_keys=True)


def _is_local(item: BacklogItem, committed: Any) -> bool:
    """Whether the work tree's `item` is missing from, or differs from, the file at the `fork_from` tip as the last
    `refresh` recorded it (`committed_items`). False before any refresh has recorded it: not known yet."""
    if not isinstance(committed, dict):
        return False
    return committed.get(item.id) != _committed(item)


def _item_fields(item: BacklogItem) -> dict:
    """The fields the page's Edit form shows and sends back (`EDIT_KEYS`)."""
    return {"title": item.title, "lane": item.lane, "status": item.status, "depends_on": list(item.deps),
            "issue": item.issue, "spec": item.spec, "notes": item.notes}


def snapshot(cfg: Config, repo: Path) -> dict:
    """The Backlog column for the board: `cards`, the finished modules (`done`, each with its `reason`, and
    `done_note`, the error from reading the hand marks or `""`), the New task form (`new_task`, `lane_phases`,
    `after_choices`) and the last base-branch sync note. In a `backlog` pack, `backlog_sha256` is the sha-256 of the
    file's bytes the cards were read from (`""` when it was not read), and each file card carries `item` (its
    `EDIT_KEYS` fields) and `local` (`_is_local`). It runs no git and no `gh`."""
    repo = Path(repo)
    pic = _picture(cfg, repo)
    view, book, lane = pic["view"], pic["book"], module_queue.module_lane(cfg)
    cards: list[dict] = []

    def make(key: str, kind: str, ident: str, label: str, item_lane: str, item: Item | None,
             module: str | None = None, **extra: Any) -> dict:
        info = view.get(key, {"blocked_by": [], "parallel_with": [], "parallel": False})
        failure = item.failure if item is not None else None
        unknown = _deps_unknown(key, getattr(item, "params", None) or {}, pic)
        if item is not None and item.status == "starting":
            status = "starting"
        elif info["blocked_by"] or unknown:
            status = "blocked"
        else:
            status = "parallel" if info["parallel"] else "ready"
        if unknown and status != "starting":
            extra["note"] = f"Dependencies unknown: {unknown}"
        elif info.get("cycle") and kind == "issue":
            extra["note"] = info["cycle"]
        extra.setdefault("blocks", [])
        return {
            "key": key, "kind": kind, "id": ident, "label": label, "lane": item_lane, "status": status,
            "blocked_by": info["blocked_by"],
            "parallel_with": [{"key": k, "label": _label_of(k, pic)} for k in info["parallel_with"]],
            "parallel": info["parallel"], "failure": failure,
            "first_phase": _first_phase(cfg, repo, item_lane, ident if kind == "module" else module),
            "removable": item is not None and item.source == "form" and item.status == "backlog",
            "to_phase": item.to_phase if item else None, **extra,
        }

    by_entry = {e["id"]: e for e in pic["entries"]}
    committed = pic["state"].get("committed_items")
    live_issues = {str((r.state.get("params") or {}).get("issue") or "") for r in _live_runs(pic["runs"])}
    taken = {i["number"] for i in pic["issues"] if i.get("in_progress") and i["number"] not in live_issues}
    for node in pic["nodes"]:
        entry = by_entry[node.id]
        if entry["status"] != "backlog":
            continue
        file_item = pic["file_items"].get(node.id)
        if file_item is None:
            cards.append(make(f"module:{node.id}", "module", node.id, node.id, lane or "", book.overlay(node.id),
                              deps=list(node.deps), note=entry.get("blocked")))
            continue
        startable = _startable(file_item, lane)
        title = file_item.title or node.id
        card = make(f"module:{node.id}", "module", node.id, title, file_item.lane or lane or "", book.overlay(node.id),
                    deps=list(node.deps), note=entry.get("blocked"), title=title, planning=file_item.status,
                    startable=startable, local=_is_local(file_item, committed))
        card["item"] = _item_fields(file_item)  # not through `make`, whose `item` is the card's log record
        if not startable:
            card["note"] = NOT_STARTABLE.format(lane=file_item.lane)
        cards.append(card)
    for item in book.cards():
        if item.status in ("started", "removed"):
            continue
        card = make(f"card:{item.id}", "card", item.id, item.label, item.lane, item,
                    deps=[*item.after, *item.deps], new=item.new_module is not None)
        if item.params.get("issue") in taken and card["status"] != "starting":
            card["status"], card["note"] = "taken", TAKEN
        cards.append(card)
    for entry in pic["issues"]:
        number = entry["number"]
        if _issue_blocker(number, pic) is not None:
            continue
        module = ISSUE_MODULE.format(number=number) if entry["lane"] == lane else None
        blockers = list(dict.fromkeys(module_queue.issue_key(r) for r in entry["blocked_by"]))
        blocks = [{"key": module_queue.issue_key(r), "label": f"{r['repo']}#{r['number']}" if r["repo"] else f"#{r['number']}"}
                  for r in entry["blocks"]]
        card = make(f"issue:{number}", "issue", number, f"#{number} · {entry.get('title') or ''}".rstrip(" ·"),
                    entry["lane"], book.issue_overlay(number), module=module, deps=blockers, issue=number,
                    url=entry.get("url") or "", blocks=blocks)
        if number in taken and card["status"] != "starting":
            card["status"], card["note"] = "taken", TAKEN
        cards.append(card)
    for group in pic["alerts"]:
        lowest = group["alerts"][0]
        number, holder = str(lowest), pic["alert_runs"].get(lowest)
        card = make(f"alert:{number}", "alert", number, f"{group['package']} ({group['ecosystem']})", "vuln",
                    book.alert_overlay(number), severity=group["severity"], manifest=group["manifest"],
                    alerts=group["alerts"], ids=group["ids"], dependabot_prs=group["dependabot_prs"],
                    url=group["url"])
        if card["status"] != "starting":
            if holder is not None:
                card["status"], card["note"] = "in_progress", f"run {holder.id} holds it"
            elif not group["fixable"]:
                card["status"], card["note"] = "no_fix", "no patched version yet"
            else:
                card["status"] = "ready"
        cards.append(card)
    done = [{"key": f"module:{n.id}", "id": n.id, "reason": by_entry[n.id].get("done_reason") or finished.MERGED}
            for n in pic["nodes"] if by_entry[n.id]["status"] == "done"]
    live_runs = [{"key": f"run:{r.id}", "label": _label_of_run(r), "kind": "run"} for r in _live_runs(pic["runs"])]
    choices = [{"key": c["key"], "label": c["label"], "kind": c["kind"]} for c in cards if c["kind"] == "card"]
    choices += live_runs
    choices += [{"key": f"module:{n.id}", "label": n.id, "kind": "module"} for n in pic["nodes"]
                if by_entry[n.id]["status"] != "done"]
    forms = new_task(cfg)
    return {
        "cards": cards,
        "done": done,
        "done_note": (finished.read_marks(repo)[1] or "") if work_source.hand_marks(cfg) else "",
        "new_task": forms,
        "lane_phases": {ln: lane_phases(cfg, ln) for ln in forms},
        "lane_labels": lane_labels(cfg),
        "after_choices": choices,
        "modules": [n.id for n in pic["nodes"]],
        "skipped": work_source.skipped(cfg, repo),
        "dropped": pic["dropped"],
        "backlog_note": pic["file_note"],
        "backlog_sha256": pic["backlog_sha256"],
        "sync_note": pic["state"].get("sync_note") or "",
        "issues_note": pic["state"].get("issues_note") or "" if cfg.issues.sync_label else "",
        "alerts_note": pic["state"].get("alerts_note") or "" if _alerts_on(cfg) else "",
    }


# --------------------------------------------------------------------------- marking a module done by hand

QUEUE_RUNNING = "the queue is running; the Backlog shows it on its next refresh"


def _known_module(cfg: Config, repo: Path, module: str) -> None:
    if module not in [n.id for n in _map_nodes(cfg, repo)]:
        raise Invalid(f"{module!r} is not a module of this {work_source.noun(cfg)}")


def _reassess(cfg: Config, repo: Path) -> list[str]:
    """Record the new state in `queue.json` under the queue lock; when `agentflow queue` holds it, the change
    stays and the Backlog shows it from the marks file until the next refresh."""
    try:
        with module_queue.queue_lock(repo):
            if _map_nodes(cfg, repo):
                module_queue.assess_modules(cfg, repo)
    except module_queue.QueueBusy:
        return [QUEUE_RUNNING]
    return []


def mark_done(cfg: Config, repo: Path, module: str, by: str) -> list[str]:
    """Mark `module` done by hand; the lines to print. `Invalid` when it is not a module, `Refused` when it has a
    live run, `ValueError` (with how to repair it) when the marks file is unreadable."""
    repo = Path(repo)
    _known_module(cfg, repo, module)
    error = finished.read_marks(repo)[1]
    if error:
        raise ValueError(error)
    run = module_queue.feature_runs(cfg, repo).get(module)
    if run is not None and run.state["status"] in module_queue._LIVE:
        raise Refused(f"{module} has a live run, {run.id} ({run.state['status']}); cancel it first or let it finish")
    finished.mark(repo, module, by)
    return [f"{module} marked done by hand", *_reassess(cfg, repo)]


def mark_not_done(cfg: Config, repo: Path, module: str, by: str) -> list[str]:
    """Take a hand mark off `module`, or, with none, veto the origin evidence or closed issue that says it is done (a
    merged module stays done). The lines to print; the errors are those of `mark_done`, less the live run."""
    repo = Path(repo)
    _known_module(cfg, repo, module)
    marks, error = finished.read_marks(repo)
    if error:
        raise ValueError(error)
    if (marks.get(module) or {}).get("state") == "done":
        finished.unmark(repo, module)  # a veto stays as it is: only the hand mark goes
        message = f"{module} hand mark removed"
    else:
        finished.veto(repo, module, by)
        message = f"{module} marked not done by hand: origin evidence and a closed issue are ignored"
    return [message, *_reassess(cfg, repo)]


# --------------------------------------------------------------------------- the backlog file from the command line

NEEDS_BACKLOG = "this needs work_source kind backlog (it is {noun} here)"
SET_KEYS = tuple(k for k in backlog_file.KEYS if k != "id")


def backlog_view(cfg: Config, repo: Path) -> list[dict]:
    """One dict per item of the backlog file, in file order, for `agentflow backlog list`: `id`, `title`, `lane`,
    `status` (the file's), `state` and `detail`. `state` is the first that holds: `dropped`, `done` (`detail` is
    the reason), the phase of the item's local run (`detail` is the run id), `blocked` (`detail` names the items
    it waits for), else the file's `ready` or `backlog`. A claim on `origin` is not read here."""
    repo = Path(repo)
    if work_source.kind(cfg) != "backlog":
        return []
    pic = _picture(cfg, repo)
    by_entry = {e["id"]: e for e in pic["entries"]}
    runs = {r.id: r for r in pic["runs"]}
    out = []
    for item in pic["file_items"].values():
        entry = by_entry.get(item.id)
        state, detail = item.status, ""
        if item.status == "dropped":
            state = "dropped"
        elif entry is not None and entry["status"] == "done":
            state, detail = "done", entry.get("done_reason") or finished.MERGED
        elif item.status == "done":
            state, detail = "done", finished.FILE_SAYS
        elif entry is not None and entry.get("run_id") in runs:
            state, detail = str(runs[entry["run_id"]].state.get("phase") or "running"), entry["run_id"]
        else:
            waiting = [b["key"].partition(":")[2] for b in pic["view"].get(f"module:{item.id}", {}).get("blocked_by", [])]
            if waiting:
                state, detail = "blocked", ", ".join(waiting)
        out.append({"id": item.id, "title": item.title, "lane": item.lane, "status": item.status,
                    "state": state, "detail": detail})
    return out


def _backlog_path(cfg: Config, repo: Path) -> Path:
    path = work_source.file(cfg, repo) if work_source.kind(cfg) == "backlog" else None
    if path is None:
        raise Invalid(NEEDS_BACKLOG.format(noun=work_source.noun(cfg)))
    return path


def _comma_list(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def _positive(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise Invalid(f"issue must be a positive whole number, not {value!r}") from None
    if number < 1:
        raise Invalid(f"issue must be a positive whole number, not {value!r}")
    return number


def set_item(cfg: Config, repo: Path, item_id: str, pairs: list[str]) -> list[str]:
    """`set <id> key=value...`: edit the item in the backlog file, in the working tree (nothing is committed).
    Every pair is read before the file is touched. `Invalid` for a pair that cannot be read or a file that is not
    a backlog; `backlog_file.EditRefused` when the file cannot take the edit."""
    path = _backlog_path(cfg, repo)
    fields: dict[str, object] = {}
    for pair in pairs:
        key, equals, value = pair.partition("=")
        if not equals:
            raise Invalid(f"{pair!r} is not key=value")
        if key not in SET_KEYS:
            raise Invalid(f"set takes the keys {', '.join(SET_KEYS)}, not {key!r}")
        if key in fields:
            raise Invalid(f"{key} is given twice")
        if key == "status" and not value:
            raise Invalid("status cannot be empty: it is one of " + ", ".join(backlog_file.STATUSES))
        if key == "depends_on":
            fields[key] = _comma_list(value)
        elif key == "issue":
            fields[key] = _positive(value) if value else None
        else:
            fields[key] = value
    if not fields:
        raise Invalid("nothing to set: give key=value pairs")
    was = _status_of(cfg, repo, item_id)
    backlog_file.set_fields(path, item_id, fields)
    lines = [f"{item_id}: set {', '.join(fields)} in {path.name} (not committed)"]
    if fields.get("status") in REOPENED:
        lines += release_on_reopen(cfg, repo, item_id, was)
    return lines


def set_status(cfg: Config, repo: Path, item_id: str, status: str) -> list[str]:
    """`done` and `undone` on a backlog pack: `set <id> status=<status>`. `done` is refused while the item has a
    live run, as for the other work sources; it never touches `runs/done_marks.json`."""
    repo = Path(repo)
    path = _backlog_path(cfg, repo)
    if status == "done":
        _refuse_live(cfg, repo, item_id)
    was = _status_of(cfg, repo, item_id)
    backlog_file.set_fields(path, item_id, {"status": status})
    lines = [f"{item_id}: status set to {status} in {path.name} (not committed)"]
    if status in REOPENED:
        lines += release_on_reopen(cfg, repo, item_id, was)
    return lines


# --------------------------------------------------------------------------- claims: a person frees one

REOPENED = ("ready", "backlog")  # the statuses a `done` item is reopened to


def _status_of(cfg: Config, repo: Path, item_id: str) -> str:
    """The status of `item_id` in the backlog file in the working tree now, or "" when it is not there."""
    try:
        found = work_source.item(cfg, Path(repo), item_id)
    except Exception:  # the edit that follows says what is wrong with the file
        return ""
    return found.status if found is not None else ""


def _by(held: claims.Claim) -> str:
    """Who holds `held`, for a line a person reads: only validated fields, one line, cut short."""
    return claim_sync._cut(f"{held.by} ({held.phase}, {held.status})")


def release_item(cfg: Config, repo: Path, item_id: str) -> list[str]:
    """`backlog release <id>`: free the claim on `item_id` on `origin`, whoever holds it and however old it is (an
    unreadable one too). The lines to print: the claimant, and, when a run of this clone holds that claim (its
    `state["claim"]` names the item and is unreleased), that its next change to the claim stops it with `claim_lost`;
    `no claim on <id>` when there was none. `Invalid` outside a `backlog` pack, `ValueError` for a bad id and
    `Refused` when `origin` can't be reached or another claim took the ref meanwhile."""
    repo = Path(repo)
    _backlog_path(cfg, repo)
    try:
        freed = claims.release_item(cfg, repo, item_id)
    except claims.ClaimError as exc:
        raise Refused(f"could not free the claim on {item_id}: {claim_sync._cut(exc)}") from exc
    if freed is None:
        return [f"no claim on {item_id}"]
    if freed.unreadable:
        lines = [f"{item_id}: released an unreadable claim ({claim_sync._cut(freed.unreadable)})"]
    else:
        lines = [f"{item_id}: released the claim of {claim_sync._cut(freed.by)} (run {freed.run}, {freed.phase}, "
                 f"{freed.status}, updated {freed.updated})"]
    for run in list_runs(repo):
        try:
            row = run.refresh().get("claim")
        except Exception:  # one unreadable run is not a reason to look at no other
            continue
        if isinstance(row, dict) and row.get("item") == item_id and not row.get("released"):
            lines.append(f"run {run.id} holds this claim here: its next change stops it with claim_lost")
    return lines


def release_on_reopen(cfg: Config, repo: Path, item_id: str, was: str) -> list[str]:
    """After an item that was `done` (`was`) becomes `ready` or `backlog`: free its claim when the claim is a finished
    one, the claim of the pack's last phase on an item that is `done` in the file at `origin/<fork_from>`. Any other
    claim stays, and the line says whose it is (`left claim of <by> (<phase>, <status>)`). The lines to print; `[]`
    when there is no claim or the item was not `done` (origin is not asked then). Never raises: a claim that can't be
    read or freed is a line."""
    if was != "done":
        return []
    repo = Path(repo)
    try:
        if not claims.enabled(cfg, repo):
            return []
        held = claims.read(cfg, repo, item_id)
    except Exception as exc:
        return [f"{item_id}: could not read the claim on origin ({claim_sync._cut(exc)}); it was left as it is"]
    if held is None:
        return []
    try:
        if held.unreadable:
            return [f"{item_id}: left an unreadable claim ({claim_sync._cut(held.unreadable)}): "
                    f"agentflow backlog release {item_id} frees it"]
        fetch_remote_branch(repo, cfg.project.fork_from)
        if held.phase == list(cfg.phases)[-1] and item_id in claim_sync._merged_ids(cfg, repo):
            claims.release(cfg, repo, held)
            return [f"{item_id}: released the finished claim of {_by(held)}"]
        return [f"{item_id}: left claim of {_by(held)}; agentflow backlog release {item_id} frees it"]
    except Exception as exc:
        return [f"{item_id}: could not free the claim of {_by(held)} ({claim_sync._cut(exc)}); it was left"]


def _spec_file(repo: Path, spec: str) -> str:
    """`spec` (a path inside `repo`, as the command line gave it) when it is a regular file reached through no
    symlink; the heading text is read from it by the caller. `Invalid` otherwise."""
    pure = PurePosixPath(spec)
    if not spec or spec != str(pure) or ".." in pure.parts or backlog_file.bad_path(spec):
        raise Invalid(f"--spec must be a path inside the repository, not {spec!r}")
    why = backlog_file.path_problem(Path(repo), spec)
    if why is not None:
        raise Invalid(f"--spec {spec} {why}")
    at = Path(repo) / spec
    if not at.is_file():
        raise Invalid(f"--spec {spec} is not a file")
    return str(pure)


def _spec_id(cfg: Config, spec: str) -> str:
    """The module id a spec file stands for: the `{module}` of `project.docs.spec_file` when the file name fits it,
    else the file's lowercased slug stem. `Invalid` when that is not a module id."""
    name = PurePosixPath(spec).name
    pattern = re.escape(cfg.project.spec_file).replace(re.escape("{module}"), "(?P<module>.+)")
    found = re.fullmatch(pattern, name)
    if found:
        derived = found.group("module")
    else:
        derived = re.sub(r"[^a-z0-9_-]+", "-", PurePosixPath(name).stem.lower()).strip("-")
    if sources.MODULE_ID.fullmatch(derived) is None or len(derived) > backlog_file.ID_MAX:
        raise Invalid(f"cannot make an item id from {name}: give one (a lowercase slug, at most "
                      f"{backlog_file.ID_MAX} characters)")
    return derived


def _spec_title(repo: Path, spec: str) -> str:
    try:
        header = sources.read_header(Path(repo) / spec)
    except OSError as exc:
        raise Invalid(f"cannot read {spec}: {exc}") from None
    for line, _ in sources.spec_header(header):
        if line.startswith("# "):
            return line[2:].strip()[: backlog_file.TITLE_MAX].rstrip()
    return ""


def add_item(cfg: Config, repo: Path, item_id: str | None, *, spec: str | None = None, title: str | None = None,
             lane: str = "", status: str = "backlog", depends_on: str = "", issue: str | None = None,
             notes: str = "") -> list[str]:
    """`add [<id>] [--spec PATH] ...`: append an item to the backlog file, in the working tree (nothing is committed).
    With `--spec` the id and the title come from the spec file unless given. `Invalid` for a bad argument;
    `backlog_file.EditRefused` when the file cannot take the item."""
    repo = Path(repo)
    path = _backlog_path(cfg, repo)
    if spec is not None:
        spec = _spec_file(repo, spec)
        item_id = item_id or _spec_id(cfg, spec)
        title = title if title is not None else _spec_title(repo, spec)
    if not item_id:
        raise Invalid("give an item id, or --spec")
    if not title:
        raise Invalid("give --title (the spec has no '# ' heading)" if spec else "give --title")
    item = BacklogItem(id=item_id, deps=_comma_list(depends_on), title=title, lane=lane, status=status,
                       spec=spec or "", issue=_positive(issue) if issue else None, notes=notes)
    backlog_file.append_item(path, item)
    return [f"{item_id}: added to {path.name} as {status} (not committed)"]


def _refuse_live(cfg: Config, repo: Path, item_id: str) -> None:
    """`Refused` while the item `item_id` has a live module-lane run (the run the Board ties to its card)."""
    run = module_queue.feature_runs(cfg, repo).get(item_id)
    if run is not None and run.state["status"] in module_queue._LIVE:
        raise Refused(f"{item_id} has a live run, {run.id} ({run.state['status']}); cancel it first or let it finish")


def fix_done(cfg: Config, repo: Path, dry_run: bool) -> list[str]:
    """`validate --fix` for the backlog file: set `status: done` on each open item that `validate` warns is done by
    its runs. The lines to print: `<id>: status set to done` for each (also on a `dry_run`, which writes nothing), or
    `skipped <id>: <why>` when the file cannot take the edit. `[]` outside a `backlog` pack."""
    repo = Path(repo)
    path = work_source.file(cfg, repo) if work_source.kind(cfg) == "backlog" else None
    if path is None:
        return []
    try:
        done = finished.recorded(repo, hand_marks=False)  # as `validate` reads it: a backlog file has no marks
    except (OSError, ValueError):
        return []
    lines = []
    for item in work_source.all_items(cfg, repo):
        if item.status not in backlog_file.OPEN or item.id not in done:
            continue
        if not dry_run:
            try:
                backlog_file.set_fields(path, item.id, {"status": "done"})
            except backlog_file.EditRefused as exc:
                lines.append(f"skipped {item.id}: {exc}")
                continue
        lines.append(f"{item.id}: status set to done")
    return lines


# --------------------------------------------------------------------------- the backlog file from the page

EDIT_KEYS = ("title", "lane", "status", "depends_on", "issue", "spec", "notes")


def _sha_arg(sha: Any) -> str:
    if not isinstance(sha, str):
        raise Invalid("sha256 must be the sha-256 of backlog.yml the page read")
    return sha


def _edit_value(cfg: Config, repo: Path, key: str, value: Any) -> object:
    """`value` for the item key `key` from the page, checked for its type: `Invalid` otherwise, before the file is
    touched (`backlog_file` checks the item as a whole)."""
    if key not in EDIT_KEYS:
        raise Invalid(f"{key!r} is not a key the page edits: it edits {', '.join(EDIT_KEYS)}")
    if key == "depends_on":
        if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
            raise Invalid("depends_on must be a list of item ids")
        return list(dict.fromkeys(v.strip() for v in value))
    if key == "issue":
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise Invalid("issue must be a positive whole number, or null")
        return value
    if not isinstance(value, str):
        raise Invalid(f"{key} must be text")
    if key in ("title", "status") and not value.strip():
        raise Invalid(f"{key} must not be empty")
    if key == "status" and value not in backlog_file.STATUSES:
        raise Invalid("status must be one of " + ", ".join(backlog_file.STATUSES))
    if key == "lane" and value:
        try:
            check_lane(cfg, value)
        except ValueError:
            raise Invalid(f"lane {value!r} is not a lane: it is one of {', '.join(cfg.lanes)}") from None
    if key == "spec" and value:
        if backlog_file.bad_path(value):
            raise Invalid(f"spec must be a path inside the repository, not {value[:80]!r}")
        why = backlog_file.path_problem(Path(repo), value)
        if why is not None:
            raise Invalid(f"spec {value} {why}")
    return value


def _edit_fields(cfg: Config, repo: Path, fields: Any) -> dict[str, object]:
    if not isinstance(fields, dict):
        raise Invalid("fields must be an object")
    return {str(k): _edit_value(cfg, repo, str(k), v) for k, v in fields.items()}


def _refuse_closing_live(cfg: Config, repo: Path, item_id: str, fields: dict[str, object]) -> None:
    if fields.get("status") in ("done", "dropped"):
        _refuse_live(cfg, repo, item_id)


def add_file_item(cfg: Config, repo: Path, item: dict, sha: str) -> str:
    """Append an item to the backlog file in the work tree (nothing is committed) and return the file's new sha-256.
    `item` holds `id` and `EDIT_KEYS`; an empty `id` or `title` comes from `item["spec"]` as `backlog add --spec`
    derives it. `Invalid` for a bad key or value, `Refused` for `done`/`dropped` on an id with a live run,
    `backlog_file.Stale` when the file's bytes do not hash to `sha`, `backlog_file.EditRefused` when the file
    cannot take the item."""
    repo = Path(repo)
    path = _backlog_path(cfg, repo)
    sha = _sha_arg(sha)
    if not isinstance(item, dict):
        raise Invalid("item must be an object")
    item_id = item.get("id")
    if item_id is None:
        item_id = ""
    if not isinstance(item_id, str):
        raise Invalid("id must be text")
    # an empty value is a value not given: `id` and `title` then come from the spec, `status` is `backlog`
    fields = _edit_fields(cfg, repo, {k: v for k, v in item.items() if k != "id" and v not in ("", None)})
    title = str(fields.get("title") or "")
    spec = str(fields.get("spec") or "")
    if spec and (not item_id or not title):
        spec = _spec_file(repo, spec)
        item_id = item_id or _spec_id(cfg, spec)
        title = title or _spec_title(repo, spec)
    if not item_id:
        raise Invalid("give an id, or a spec to take it from")
    if not title:
        raise Invalid("give a title (the spec has no '# ' heading)" if spec else "give a title")
    status = str(fields.get("status") or "backlog")
    deps, issue = fields.get("depends_on"), fields.get("issue")
    new = BacklogItem(id=item_id, deps=deps if isinstance(deps, list) else [], title=title,
                      lane=str(fields.get("lane") or ""), status=status, spec=spec,
                      issue=issue if isinstance(issue, int) else None, notes=str(fields.get("notes") or ""))
    with _FILE_LOCK:
        _refuse_closing_live(cfg, repo, item_id, {"status": status})
        return backlog_file.append_item(path, new, expect_sha=sha)


def edit_item(cfg: Config, repo: Path, item_id: str, fields: dict, sha: str) -> tuple[str, list[str]]:
    """Set `fields` (`EDIT_KEYS` only) of the item `item_id` in the work tree's backlog file (nothing is committed)
    and return the file's new sha-256, and the lines `release_on_reopen` has when a `done` item became `ready` or
    `backlog`. The errors are those of `add_file_item`."""
    repo = Path(repo)
    path = _backlog_path(cfg, repo)
    sha = _sha_arg(sha)
    if not isinstance(item_id, str) or not item_id:
        raise Invalid("id must be an item id")
    checked = _edit_fields(cfg, repo, fields)
    if not checked:
        raise Invalid("nothing to set: send the fields to change")
    with _FILE_LOCK:
        _refuse_closing_live(cfg, repo, item_id, checked)
        was = _status_of(cfg, repo, item_id)
        new_sha = backlog_file.set_fields(path, item_id, checked, expect_sha=sha)
    notes = release_on_reopen(cfg, repo, item_id, was) if checked.get("status") in REOPENED else []
    return new_sha, notes  # the claim is freed outside the lock: it waits on the network


def move(cfg: Config, repo: Path, item_id: str, direction: Literal["up", "down"], sha: str) -> str:
    """Swap the item `item_id` with the nearest open item above or below it in the work tree's backlog file (nothing
    is committed; `backlog_file.move_item`) and return the file's new sha-256. `Invalid` for a bad id or direction;
    `backlog_file.Stale` and `backlog_file.EditRefused` as `add_file_item` says."""
    repo = Path(repo)
    path = _backlog_path(cfg, repo)
    sha = _sha_arg(sha)
    if not isinstance(item_id, str) or not item_id:
        raise Invalid("id must be an item id")
    if direction not in ("up", "down"):
        raise Invalid("direction must be up or down")
    with _FILE_LOCK:
        return backlog_file.move_item(path, item_id, up=direction == "up", expect_sha=sha)


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


def _spec_task(new_module: dict) -> str:
    """The `--task` text of a new module's run in a `specs` pack: its responsibility, then its dependencies."""
    deps = ", ".join(new_module.get("deps") or []) or "none"
    return f"{new_module['responsibility']}\n\nDepends on: {deps}"


def parse_key(key: Any) -> tuple[str, str]:
    kind, _, name = str(key).partition(":")
    if kind not in ("module", "card", "issue", "alert") or not name:
        raise Invalid("name a card (card:<id>), a module (module:<id>), an issue (issue:<number>) "
                      "or an alert group (alert:<number>)")
    if kind in ("issue", "alert"):
        try:
            name = str(int(intake.issue_number(name) if kind == "issue" else vuln.alert_number(name)))
        except ValueError as exc:
            raise Invalid(str(exc)) from exc
    return kind, name


def begin_start(cfg: Config, repo: Path, key: str, confirm: bool = False) -> StartJob:
    """Check that `key` may start now and record `item_starting`. `Refused` (409) says why not.

    A backlog item whose status is `backlog` (an idea) starts only with `confirm`; the caller asks for a second click."""
    repo = Path(repo)
    kind, name = parse_key(key)
    reason = module_queue.usage_hold(cfg)
    if reason is not None:
        raise Refused(f"the usage window holds new runs: {reason}")
    log = _log(repo)
    with log.locked():
        pic = _picture(cfg, repo)
        book: Backlog = pic["book"]
        lane = module_queue.module_lane(cfg)
        if kind == "module":
            node = next((n for n in pic["nodes"] if n.id == name), None)
            if node is None:
                raise Refused(f"module {name} is not in the {work_source.noun(cfg)}"
                              + ("" if lane else " (dashboard.backlog.module_lane is not set)"))
            if {e["id"]: e for e in pic["entries"]}[name]["status"] != "backlog":
                raise Refused(f"module {name} already has a run or is done")
            item = book.overlay(name)
            params, new_module, to_phase, item_lane = {"module": name}, None, None, lane or ""
            file_item = pic["file_items"].get(name)
            if not _startable(file_item, lane):
                raise Refused(NOT_STARTABLE.format(lane=file_item.lane))
            number = _module_issue(name, pic)
            if file_item is not None and file_item.issue is not None:
                number = str(file_item.issue)
            if number is not None:
                unknown = _deps_unknown(f"module:{name}", {}, pic)
                if unknown:
                    raise Refused(f"issue #{number}: its dependencies could not be read: {unknown}")
                _refuse_taken(number, pic)
                params["issue"] = number  # so the run marks the issue and its PR closes it
        elif kind == "issue":
            entry = next((i for i in pic["issues"] if i["number"] == name), None)
            if entry is None:
                label = cfg.issues.sync_label
                raise Refused(("issue sync is off" if not label else
                               f"issue #{name} is not open as of the last refresh" if label == ALL_ISSUES else
                               f"issue #{name} is not open with the {label!r} label as of the last refresh"))
            blocker = _issue_blocker(name, pic)
            if blocker is not None:
                raise Refused(blocker)
            unknown = _deps_unknown(key, {}, pic)
            if unknown:
                raise Refused(f"issue #{name}: its dependencies could not be read: {unknown}")
            _refuse_taken(name, pic)
            item_lane = entry["lane"]
            item = book.issue_overlay(name)
            params, new_module = _issue_run_params(cfg, item_lane, name, pic)
            to_phase = None
        elif kind == "alert":
            if not _alerts_on(cfg):
                raise Refused("alert sync is off")
            group = next((g for g in pic["alerts"] if str(g["alerts"][0]) == name), None)
            if group is None:
                raise Refused(f"alert #{name} is not an open alert group at or above {cfg.vuln.min_severity} "
                              "as of the last refresh")
            if not group["fixable"]:
                raise Refused(f"alert #{name}: no patched version yet")
            holder = pic["alert_runs"].get(group["alerts"][0])
            if holder is not None:
                raise Refused(f"alert #{name}: run {holder.id} holds {group['package']} in {group['manifest']}")
            item_lane, item = "vuln", book.alert_overlay(name)
            params, new_module, to_phase = {"alert": str(group["fixable"][0])}, None, None
        else:
            item = book.items.get(name)
            if item is None or item.source != "form" or item.status == "removed":
                raise Refused("no such card")
            params, new_module, to_phase, item_lane = dict(item.params), item.new_module, item.to_phase, item.lane
        if item is not None and item.status == "starting":
            raise Refused(f"{key} is already starting")
        if item is not None and item.status == "started" and kind == "card":
            raise Refused(f"{key} has already started")
        if kind == "card" and new_module is not None and work_source.kind(cfg) in ("specs", "backlog"):
            if any(n.id == params.get("module") for n in pic["nodes"]):
                raise Refused(f"a spec for {params['module']} now exists; start module:{params['module']} instead")
            # a specs pack has no file to add the module to; a backlog pack's run appends it in its first commit
            params["task"] = _spec_task(new_module)
            if work_source.kind(cfg) == "specs":
                new_module = None
        if kind == "card" and params.get("issue"):
            unknown = _deps_unknown(key, params, pic)
            if unknown:
                raise Refused(f"issue #{params['issue']}: its dependencies could not be read: {unknown}")
            _refuse_taken(str(params["issue"]), pic)
            _refuse_starting_issue(str(params["issue"]), book)
            if item_lane == lane and "module" not in params:  # an issue-only card in the module lane
                extra, new_module = _issue_run_params(cfg, item_lane, str(params["issue"]), pic)
                params.update(extra)
        info = pic["view"].get(key)
        if info and info["blocked_by"]:
            raise Refused(f"{_label_of(key, pic)} is blocked: runs after "
                          + ", ".join(b["text"] for b in info["blocked_by"]))
        file_item = pic["file_items"].get(name) if kind == "module" else None
        if file_item is not None and file_item.status == "backlog" and not confirm:
            raise Refused(f"module {name} is an idea (status backlog) and must be confirmed to start")
        clash = _live_clash(params, item_lane, pic["runs"])
        if clash:
            raise Refused(clash)
        if item is None or (kind in ("module", "issue", "alert") and item.status == "started"):  # a card always has its item
            ident = uuid.uuid4().hex[:8]
            if kind == "issue":
                log.append("item_added", by=BY, id=ident, lane=item_lane, kind="issue", source="issue",
                           params={"issue": name}, new_module=None, after=[], to_phase=None, label=f"issue #{name}")
            elif kind == "alert":
                log.append("item_added", by=BY, id=ident, lane=item_lane, kind="alert", source="alert",
                           params={"alert": name}, new_module=None, after=[], to_phase=None, label=f"alert #{name}")
            else:
                log.append("item_added", by=BY, id=ident, lane=item_lane, kind="module", source="map",
                           params={"module": name}, new_module=None, after=[], to_phase=None, label=name)
        else:
            ident = item.id
        log.append("item_starting", by=BY, id=ident)
    return StartJob(ident, key, item_lane, params, to_phase, new_module, _label_of(key, pic))


def _live_clash(params: dict, lane: str, runs: list[Run]) -> str | None:
    for run in _live_runs(runs):
        have = run.state.get("params") or {}
        for name in ("module", "issue", "alert"):
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
        taken = _taken_now(cfg, repo, str(job.params["issue"])) if job.params.get("issue") else None
        if taken is not None:
            log.append("item_failed", by=BY, id=job.item_id, reason=taken)
            return None
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
    """Bring the base branch up to date, re-assess the map modules and re-check what cards run after. In a `backlog`
    pack it records the items of the file at the `fork_from` tip (`committed_items`), for the cards' `local` badge.

    It only refreshes what the cards show and never starts anything. It skips the assessment when
    `agentflow queue` holds the queue lock."""
    repo = Path(repo)
    issues_note = sync_issues(cfg, repo)  # never raises, and touches no git
    alerts_note = sync_alerts(cfg, repo)  # the same
    note = sync_branch(repo, cfg.project.fork_from) if module_queue.backlog_settings(cfg).get("sync_base", False) else ""
    # origin/<fork_from> is where a merged plan lands; fetching it never touches a local branch or the work tree
    fetch_remote_branch(repo, cfg.project.fork_from)
    claim_sync.release_merged(cfg, repo)  # this clone's own merged runs only; it pushes nothing for any other
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
    if work_source.kind(cfg) == "backlog":  # what the Board's `local` badge compares with: the commit a run forks from
        tip = commit_at(repo, cfg.project.fork_from)
        changes["committed_items"] = {i.id: _committed(i) for i in work_source.items_at(cfg, repo, tip)} if tip else {}
    _save_state(repo, **changes)
    return {"note": note, "skipped": skipped, "issues_note": issues_note, "alerts_note": alerts_note}


def _blocker_runs(entry: dict, runs: dict[str, Run]) -> set[str]:
    """The runs here of the local issues in `entry`'s `blocked_by` that still hold their issue
    (`module_queue.issue_run_holds`): whether each is merged decides whether the issue it blocks may start."""
    found: set[str] = set()
    for ref in entry["blocked_by"]:
        run = module_queue.issue_run(ref["number"], runs) if ref["repo"] is None else None
        if run is not None and module_queue.issue_run_holds(run):
            found.add(run.id)
    return found


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
    state = _load_state(repo)
    for entry in _synced(cfg, state):  # also the blockers of a form card's issue: it is in the same list
        wanted |= _blocker_runs(entry, runs)
    previous = state.get("merged") or {}
    result: dict[str, bool | None] = {k: v for k, v in previous.items() if v is True}
    for run_id in wanted:
        if result.get(run_id) is True:
            continue
        run = runs.get(run_id)
        branch = run.state.get("branch") if run else None
        result[run_id] = module_queue.branch_is_merged(cfg, repo, str(branch))[0] if branch else None
    return result
