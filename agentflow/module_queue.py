"""`agentflow next` and `agentflow queue`.

`next_step` is a deterministic check of the next step: rules 2 to 7 in order (rule 1, reading the
conversation, needs a person, so it is skipped). Each rule is a small function that returns a `NextAnswer`
or None, and the first answer wins. The skill each answer names comes from the pack's `skills.map`; with
no entry, the answer names the stage (`Build stage`).

`queue_tick` walks the pack's work source (`work_source`) in row order and starts every module whose dependencies are
done, up to `max_parallel` runs at once. A module is done when its newest plan is fully checked (deferred tasks
aside) and its branch has a merged pull request. Module ids come from a repo file, so they only ever travel as
argv items, never through a shell.

NOTE: the file is `module_queue.py`, not `queue.py`: a module named like a stdlib module shadows it for
any process with this folder on `sys.path`.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from agentflow import finished, gitops, pack, sources, work_source
from agentflow.conditions import Ctx, evaluate, make_ctx
from agentflow.gitops import uncommitted_paths
from agentflow.guards import path_matches
from agentflow.events import lock_state, parse_iso
from agentflow.state import Run, list_runs, runs_root, slugify

if TYPE_CHECKING:
    from agentflow.config import Config

INTERVAL_S = 30.0
SPAWN_TIMEOUT_S = 180
DESIGN_DOC_KEYS = ("idea", "decisions", "glossary")  # the `project.docs` paths a design-doc change touches
_PLAN_NAME = re.compile(r"^\d{4}-\d{2}-\d{2}-(.+)\.md$")
_MODULE_ID = sources.MODULE_ID
_STARTED = re.compile(r"^Started (\S+) in ", re.MULTILINE)
_LIVE = ("running", "refining", "waiting_human", "awaiting_approval", "paused", "ready")
_ACTIVE = ("running", "refining")
STALL_GRACE_S = 120  # a run just started by `--detach` holds no lock until its background runner takes it


class StartFailed(Exception):
    """A module's run could not be started."""


class QueueBusy(Exception):
    """Another `agentflow queue` already holds the queue lock."""


# --------------------------------------------------------------------------- next


@dataclass
class NextAnswer:
    lane: str
    stage: str
    evidence: list[str]
    next: str
    reason: str = ""
    security: str = ""


def format_answer(answer: NextAnswer) -> str:
    """The skill's three-line shape, plus a `Security:` line when the overlay matches."""
    lines = [
        f"Lane: {answer.lane} · Stage: {answer.stage}",
        f"Evidence: {'; '.join(answer.evidence)}",
        f"Next: {answer.next}" + (f" — {answer.reason}" if answer.reason else ""),
    ]
    if answer.security:
        lines.append(f"Security: {answer.security}")
    return "\n".join(lines)


def newest_plan(plans_dir: Path) -> Path | None:
    """The newest plan by file name (`YYYY-MM-DD-<module>.md` sorts by date), or None."""
    plans = sorted(p for p in Path(plans_dir).glob("*.md") if p.is_file()) if Path(plans_dir).is_dir() else []
    return plans[-1] if plans else None


def plan_module(plan: Path, known: list[str]) -> str:
    """The module a plan is for: the longest known id the file name starts with, else the name sans date."""
    match = _PLAN_NAME.match(plan.name)
    stem = match.group(1) if match else plan.stem
    fits = [m for m in known if stem == m or stem.startswith(f"{m}-")]
    return max(fits, key=len) if fits else stem


def backlog_settings(cfg: Config) -> dict:
    """The `dashboard.backlog` block of `config.yaml` (empty when the repository sets none)."""
    block = cfg.dashboard.get("backlog")
    return block if isinstance(block, dict) else {}


def module_lane(cfg: Config) -> str | None:
    """The lane that runs a capability-map module (`dashboard.backlog.module_lane`), or None when unset."""
    lane = backlog_settings(cfg).get("module_lane")
    return str(lane) if lane else None


def first_phase(cfg: Config) -> str:
    """The first phase in `config.yaml`: the one a spec-only run stops after."""
    return next(iter(cfg.phases))


def feature_runs(cfg: Config, repo: Path, runs: list[Run] | None = None) -> dict[str, Run]:
    """The newest module-lane run per module. A run that stopped at the first phase, or was marked done there,
    wrote a spec and did not start the module, so it does not count."""
    lane = module_lane(cfg)
    found: dict[str, Run] = {}
    if lane is None:
        return found
    first = first_phase(cfg)
    for run in list_runs(repo) if runs is None else runs:  # oldest first, so later runs replace earlier ones
        st = run.state
        module = (st.get("params") or {}).get("module")
        spec_only = first in ((st.get("options") or {}).get("to_phase"), (st.get("marked_done") or {}).get("phase"))
        if st.get("pipeline") == lane and module and not spec_only:
            found[str(module)] = run
    return found


def module_branch(cfg: Config, module: str, run: Run | None) -> str:
    """The run's branch when there is one, else the one the runner would create."""
    if run is not None and run.state.get("branch"):
        return str(run.state["branch"])
    return cfg.project.branch.replace("{lane}", module_lane(cfg) or "").replace("{slug}", slugify(module))


def branch_is_merged(cfg: Config, repo: Path, branch: str) -> tuple[bool | None, str]:
    """`branch_merged` for `branch`; `(None, why)` when `gh` cannot tell."""
    ctx = make_ctx(cfg, repo, module_lane(cfg) or "", {})
    result = evaluate(cfg, {"check": "branch_merged", "branch": branch}, ctx)
    return result.value, result.reason


def _module_ids(cfg: Config, repo: Path) -> list[str]:
    return [n.id for n in work_source.items(cfg, repo)]


BUNDLED_NOTE = "(bundled with agentflow: a run uses it; it is not installed for interactive sessions)"


def _bundled(cfg: Config, repo: Path, key: str) -> bool:
    """Whether the skill `skills.map` names for `key` is one only the engine's bundled copy provides. `repo` is the
    main checkout, where a skill of the repository is looked up first."""
    name = cfg.skills_map.get(key)
    found = pack.skill_file(repo, cfg.project.skills_dir, name) if name else None
    return found is not None and pack.is_bundled(found)


def _skill(cfg: Config, repo: Path, key: str, stage: str) -> str:
    """The skill `skills.map` names for `key` (in backticks when it is bundled), else the stage's name (`Build
    stage`). The bundled note itself is added once per answer, by `_noted`."""
    name = cfg.skills_map.get(key)
    if not name:
        return f"{stage} stage"
    return f"`{name}`" if _bundled(cfg, repo, key) else name


def _noted(cfg: Config, repo: Path, text: str, *keys: str) -> str:
    """`text` (an answer's `next`) with `BUNDLED_NOTE` once when any skill of `keys` is bundled: such a skill
    exists in runner prompts, not for an interactive session."""
    return f"{text} {BUNDLED_NOTE}" if any(_bundled(cfg, repo, key) for key in keys) else text


def design_doc_paths(cfg: Config) -> list[str]:
    """The `project.docs` `idea`, `decisions` and `glossary` paths the pack sets."""
    docs = cfg.project.docs
    return [str(docs[k]).strip().rstrip("/") for k in DESIGN_DOC_KEYS if isinstance(docs.get(k), str) and docs[k].strip()]


def security_line(cfg: Config) -> str:
    """What `next` says when a security path is involved, from `overlays.security.add_skills` (stage -> skill) and
    `phases.plan.approval_note.human_skill`; `""` when either is missing."""
    security = cfg.overlays.get("security")
    added = security.get("add_skills") if isinstance(security, dict) else None
    plan = cfg.phases.get("plan")
    note = plan.approval_note if plan is not None else None
    human = note.get("human_skill") if isinstance(note, dict) else None
    if not isinstance(added, dict) or not added or not human:
        return ""
    skills = " and ".join(f"`{s}`" for s in dict.fromkeys(str(v) for v in added.values()))
    stages = " and ".join(str(stage).title() for stage in added)
    return f"a security path is involved: add {skills} at {stages} and offer `/{human}` before Build"


def _ctx(cfg: Config, repo: Path, plan: Path | None) -> Ctx:
    ctx = make_ctx(cfg, repo, module_lane(cfg) or "", {})
    ctx.base_ref = f"origin/{cfg.project.base_branch}"
    ctx.values["plan"] = plan.relative_to(repo).as_posix() if plan else ""
    return ctx


def _some(paths: list[str], limit: int = 3) -> str:
    """The first few paths, then how many more there are."""
    shown = ", ".join(paths[:limit])
    return shown + (f" and {len(paths) - limit} more" if len(paths) > limit else "")


def rule_eval(cfg: Config, repo: Path, dirty: list[str]) -> NextAnswer | None:
    """Rule 2: uncommitted changes under `overlays.eval.when_paths` mean the eval lane."""
    globs = [str(g) for g in ((cfg.raw.get("overlays") or {}).get("eval") or {}).get("when_paths") or []]
    hits = [p for p in dirty if any(path_matches(p, g) for g in globs)]
    if not hits:
        return None
    return NextAnswer(
        "eval", "Verify",
        [f"{len(hits)} uncommitted path(s) under overlays.eval.when_paths: {_some(hits)}"],
        _noted(cfg, repo, _skill(cfg, repo, "verify", "Verify"), "verify"),
        "run the eval gate; a regression blocks the change",
    )


def rule_unchecked_plan(cfg: Config, repo: Path, plan: Path | None, ctx: Ctx) -> NextAnswer | None:
    """Rule 3: the newest plan has unchecked tasks, so keep building."""
    if plan is None:
        return None
    tasks = sources.parse_plan(plan)
    open_tasks = [t for t in tasks if t.open]
    if not open_tasks:
        return None
    small = "plan_is_small" in cfg.conditions and evaluate(cfg, "plan_is_small", ctx).value is True
    key = "build_small" if small else "build"
    skill = _noted(cfg, repo, _skill(cfg, repo, key, "Build"), key)
    return NextAnswer(
        "feature", "Build",
        [f"{plan.relative_to(repo).as_posix()}: {len(open_tasks)} of {len(tasks)} tasks unchecked "
         f"(first open: Task {open_tasks[0].n})"],
        skill, f"continue the plan from Task {open_tasks[0].n}",
    )


def rule_checked_plan(cfg: Config, repo: Path, plan: Path | None) -> NextAnswer | None:
    """Rule 4: the newest plan is fully checked and its branch is not merged. A module the last assessment found
    done (`finished.recorded`) is skipped."""
    if plan is None or not sources.parse_plan(plan):
        return None
    module = plan_module(plan, _module_ids(cfg, repo))
    if module in finished.recorded(repo):
        return None
    branch = module_branch(cfg, module, feature_runs(cfg, repo).get(module))
    merged, why = branch_is_merged(cfg, repo, branch)
    if merged is True:
        return None
    evidence = [f"{plan.relative_to(repo).as_posix()} is fully checked",
                f"{branch} has no merged pull request" if merged is False else f"could not check {branch}: {why}"]
    verify = _noted(cfg, repo, _skill(cfg, repo, "verify", "Verify"), "verify", "ship")
    return NextAnswer("feature", "Verify", evidence, verify, f"then {_skill(cfg, repo, 'ship', 'Ship')}")


def rule_spec_without_plan(cfg: Config, repo: Path) -> NextAnswer | None:
    """Rule 5: an approved spec has no plan yet (work-source order first). A done module is skipped."""
    docs = cfg.project.docs
    specs_dir = repo / docs["specs"]
    cond = cfg.conditions.get("spec_is_approved") or {}
    if not specs_dir.is_dir() or not cond.get("approved") or not cond.get("pending"):
        return None
    by_module = sources.spec_modules(specs_dir, cfg.project.spec_file)
    order = [m for m in _module_ids(cfg, repo) if m in by_module]
    order += sorted(m for m in by_module if m not in order)
    done = finished.recorded(repo)
    for module in order:
        if module in done:
            continue
        spec = by_module[module]
        if sources.spec_status(spec, str(cond["approved"]), str(cond["pending"])) != "approved":
            continue
        if sources.find_plan(repo / docs["plans"], module) is None:
            return NextAnswer(
                "feature", "Plan", [f"{spec.relative_to(repo).as_posix()} is approved and {module} has no plan"],
                _noted(cfg, repo, _skill(cfg, repo, "plan", "Plan"), "plan"), f"plan {module} from its approved spec",
            )
    return None


def rule_design_doc(cfg: Config, repo: Path, dirty: list[str]) -> NextAnswer | None:
    """Rule 6: uncommitted changes only under the pack's `project.docs` `idea`, `decisions` or `glossary` (a
    directory matches every path under it)."""
    docs = design_doc_paths(cfg)
    if not dirty or not docs or not all(any(p == d or p.startswith(f"{d}/") for d in docs) for p in dirty):
        return None
    return NextAnswer(
        "design-doc", "Define", [f"{len(dirty)} uncommitted path(s), all design docs: {_some(dirty)}"],
        _noted(cfg, repo, _skill(cfg, repo, "design_doc", "Define"), "design_doc"),
        "finish the design-doc edits, then commit when the user asks",
    )


def rule_idle(cfg: Config, repo: Path, dirty: list[str]) -> NextAnswer:
    """Rule 7: nothing in flight."""
    evidence = (f"{len(dirty)} uncommitted path(s) fit no lane: {_some(dirty)}" if dirty
                else "no uncommitted changes, no open plan, no approved spec waiting for a plan")
    decide, define = _skill(cfg, repo, "decide", "Define"), _skill(cfg, repo, "define", "Define")
    return NextAnswer("none", "-", [evidence],
                      _noted(cfg, repo, decide if decide == define else f"{decide} or {define}", "decide", "define"),
                      f"nothing is in flight: ask what to do; {decide} for a decision, {define} for something to build")


def next_step(cfg: Config, repo: Path) -> NextAnswer:
    """The first of rules 2 to 7 that holds, with the security line when the overlay matches."""
    repo = Path(repo)
    dirty = uncommitted_paths(repo)
    plan = newest_plan(repo / cfg.project.docs["plans"])
    ctx = _ctx(cfg, repo, plan)
    answer = (
        rule_eval(cfg, repo, dirty)
        or rule_unchecked_plan(cfg, repo, plan, ctx)
        or rule_checked_plan(cfg, repo, plan)
        or rule_spec_without_plan(cfg, repo)
        or rule_design_doc(cfg, repo, dirty)
        or rule_idle(cfg, repo, dirty)
    )
    line = security_line(cfg)
    if line and "security_path" in cfg.conditions:
        security = evaluate(cfg, "security_path", ctx)
        if security.value is True:  # unknown does not count: `next` must not guess
            answer.security = line
    return answer


# --------------------------------------------------------------------------- queue


@dataclass
class QueueState:
    """`runs/queue.json`: one entry per work-source module."""

    modules: list[dict] = field(default_factory=list)

    @staticmethod
    def path(repo: Path) -> Path:
        return runs_root(repo) / finished.QUEUE_FILE

    @classmethod
    def load(cls, repo: Path) -> QueueState:
        path = cls.path(repo)
        if not path.is_file():
            return cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return cls()  # a torn or hand-edited file is rebuilt from the repo on the next tick
        modules = data.get("modules") if isinstance(data, dict) else None
        return cls([m for m in modules if isinstance(m, dict) and "id" in m] if isinstance(modules, list) else [])

    def save(self, repo: Path) -> None:
        path = self.path(repo)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps({"modules": self.modules}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        tmp.replace(path)


def _first_line(text: str) -> str:
    return next((line.strip() for line in str(text).splitlines() if line.strip()), "")


@dataclass(frozen=True)
class OriginView:
    """The spec and plan files on `refs/remotes/origin/<fork_from>`, read once per assessment. A listing is None
    when git could not read it; `complete` is False when some file was left out (count, size or byte budget)."""

    specs: gitops.Listing | None
    plans: gitops.Listing | None
    branch: str


PLAN_READ_MAX = 1 << 20  # the most bytes of one plan read from origin; a longer plan is truncated, so not finished


def origin_view(cfg: Config, repo: Path) -> OriginView:
    """`OriginView` of `docs.specs` (the module spec files, each read to `HEADER_MAX`) and `docs.plans` (the dated
    plan files, each read to `PLAN_READ_MAX`) at `refs/remotes/origin/<fork_from>`, the ref given in full."""
    docs, spec_file = cfg.project.docs, cfg.project.spec_file
    ref = f"refs/remotes/origin/{cfg.project.fork_from}"

    def is_spec(name: str) -> bool:
        ids = sources.spec_ids([name], spec_file)
        return bool(ids) and all(sources.is_module_id(i, spec_file) for i in ids)

    def is_plan(name: str) -> bool:
        return _PLAN_NAME.fullmatch(name) is not None

    def listing(key: str, keep: Callable[[str], bool], limit: int) -> gitops.Listing | None:
        directory = docs.get(key)
        if not isinstance(directory, str) or not directory.strip():
            return None
        return gitops.blobs_at(repo, ref, directory, keep, limit)

    return OriginView(listing("specs", is_spec, sources.HEADER_MAX), listing("plans", is_plan, PLAN_READ_MAX),
                      cfg.project.fork_from)


def known_ids(cfg: Config, repo: Path, view: OriginView) -> set[str]:
    """Every module id a plan name may be for: the work source's, and those of the spec files listed on origin."""
    known = set(_module_ids(cfg, repo))
    if isinstance(view.specs, gitops.Listing):
        known |= set(sources.spec_ids(view.specs.blobs, cfg.project.spec_file))
    return known


def own_plans(names: Iterable[str], module: str, known: set[str]) -> list[str]:
    """The sorted plan names of `names` that belong to `module`: dated `YYYY-MM-DD-<module>[-suffix].md`, and no
    longer known id fits them (`2026-01-01-user-auth.md` is `user-auth`'s when that id is known, never `user`'s)."""
    ids = sorted(known | {module})
    return sorted(n for n in names if sources.is_plan_name(n, module) and plan_module(Path(n), ids) == module)


def _finished(tasks: list[sources.PlanTask]) -> bool:
    """Tasks exist and none is open (a deferred task waits on later modules, not this one)."""
    return bool(tasks) and not any(t.open for t in tasks)


def _origin_plan(view: OriginView, module: str, known: set[str]) -> gitops.Blob | None:
    """The newest of `module`'s plans listed on origin, or None."""
    if view.plans is None:
        return None
    own = own_plans(view.plans.blobs, module, known)
    return view.plans.blobs[own[-1]] if own else None


def _origin_plan_finished(blob: gitops.Blob | None) -> bool:
    return blob is not None and not blob.truncated and _finished(sources.parse_plan_text(sources.cut_lines(blob.text)))


def _plan_finished(cfg: Config, repo: Path, module: str, view: OriginView, known: set[str]) -> bool:
    """Whether `module`'s newest plan is fully checked, in the work tree or on `origin/<fork_from>` (`view`). A
    merged pull request lands on origin, while the work tree may sit on another branch or lag behind it, so the work
    tree alone can leave a merged module's dependents blocked. A plan cut short on origin is not finished."""
    plans_dir = Path(repo) / cfg.project.docs["plans"]
    names = [p.name for p in plans_dir.iterdir() if p.is_file()] if plans_dir.is_dir() else []
    own = own_plans(names, module, known)
    if own and _finished(sources.parse_plan(plans_dir / own[-1])):
        return True
    return _origin_plan_finished(_origin_plan(view, module, known))


def _origin_complete(view: OriginView) -> bool:
    return all(isinstance(x, gitops.Listing) and x.complete for x in (view.specs, view.plans))


def done_reason(
    cfg: Config, repo: Path, node: sources.ModuleNode, run: Run | None, old: dict, view: OriginView,
    known: set[str], marks: tuple[dict[str, dict], str | None],
) -> tuple[str | None, str | None]:
    """`(reason, problem)`: why `node` is done (a `finished` reason), or None, and why that could not be told.

    The first that holds: a live hand mark (`BY_HAND`); with no live veto, a spec header on origin that matches
    `project.docs.done_status` (`SPEC_SAYS`); with no live veto and both origin listings complete, a newest own plan
    on origin read in full, fully ticked and naming the module's spec file (`plan_complete`); a finished newest plan
    (work tree or origin) whose branch `gh` says is merged (`MERGED`; a prior done entry recorded as merged, or from
    before reasons were kept, is not asked about again). When origin could not be read in full, a prior Done from
    origin evidence or a merge stays as it was. When the marks file is unreadable, a prior hand mark stays, and
    origin evidence makes no new Done (the file may hold a veto) but keeps an earlier one."""
    by_hand, error = marks
    mark = (by_hand.get(node.id) or {}).get("state")
    said = old.get("done_reason")
    prior = (said if isinstance(said, str) and said else finished.MERGED) if old.get("status") == "done" else None
    if mark == "done" or (error and prior == finished.BY_HAND):
        return finished.BY_HAND, None
    vetoed = mark == "not_done"
    trust_origin = not vetoed and not error  # an unreadable marks file may hide a veto: origin makes no new Done
    try:
        spec_name = cfg.project.spec_file.format(module=node.id)
    except (KeyError, IndexError, ValueError, AttributeError):  # `validate` reports it; origin then decides nothing
        spec_name = None
    problem = None
    if trust_origin and isinstance(view.specs, gitops.Listing) and spec_name in view.specs.blobs:
        blob = view.specs.blobs[spec_name]
        text = blob.text[: blob.text.rfind("\n") + 1] if blob.truncated else blob.text  # a cut last line may lie
        says = sources.says_done(text, cfg.project.done_status)
        if says is None:
            problem = "project.docs.done_status is not a valid regex"
        elif says:
            return finished.SPEC_SAYS, None
    plan = _origin_plan(view, node.id, known)
    if (trust_origin and spec_name and _origin_complete(view) and plan is not None and _origin_plan_finished(plan)
            and sources.plan_spec(plan.text) == spec_name):
        return finished.plan_complete(view.branch), None
    if _plan_finished(cfg, repo, node.id, view, known):
        if prior == finished.MERGED:
            return finished.MERGED, None
        branch = module_branch(cfg, node.id, run)
        merged, why = branch_is_merged(cfg, repo, branch)
        if merged is True:
            return finished.MERGED, None
        if merged is None:
            problem = problem or f"could not tell whether {branch} is merged: {_first_line(why)}"
    if prior and (not _origin_complete(view) or error):  # origin, or a veto, could not be read: keep what was
        if prior == finished.MERGED or (finished.from_origin(prior) and not vetoed):
            return prior, None
    return None, problem


def _assess(
    cfg: Config, repo: Path, node: sources.ModuleNode, run: Run | None, old: dict, notes: list[str],
    view: OriginView, known: set[str], marks: tuple[dict[str, dict], str | None],
) -> tuple[dict, bool, str | None]:
    """`(entry, occupies_a_slot, problem)` for one module; `problem` says why it cannot move without a person."""
    recorded = old.get("run_id")
    entry = {"id": node.id, "deps": list(node.deps), "status": "backlog", "run_id": recorded if run is None else run.id}
    if run is None and recorded:  # started by an earlier tick, but its run directory is gone (cleaned up by hand)
        notes.append(f"queue: {node.id}: run {recorded} no longer exists; treating {node.id} as not started")
        entry["run_id"] = None
    reason, problem = done_reason(cfg, repo, node, run, old, view, known, marks)
    if reason:
        entry["status"] = "done"
        entry["done_reason"] = reason
        return entry, False, None
    if run is None:
        return entry, False, problem
    status = run.state["status"]
    crashed = _crash(run)
    if crashed:  # holds no slot, and is not restarted behind the human's back
        entry["status"] = "waiting"
        return entry, False, problem or f"run {run.id} crashed: {crashed}; resume it (`agentflow resume {run.id}`)"
    if status in _ACTIVE:
        entry["status"] = "running"
        return entry, True, problem
    entry["status"] = "waiting"
    if status in ("failed", "cancelled"):  # holds no slot, is never restarted: only a person can decide
        why = f": {_first_line(run.state['failure'])}" if status == "failed" and run.state.get("failure") else ""
        problem = problem or (
            f"run {run.id} {status}{why}; a human must retry or replace it (see `agentflow status {run.id}`)"
        )
    marked = run.state.get("marked_done")
    if status == "done" and marked and not _plan_finished(cfg, repo, node.id, view, known):
        # its plan will never finish on this run: say so, don't wait
        problem = problem or f"run {run.id} was marked done after {marked['phase']} with its plan unfinished; a human decides how {node.id} goes on"
    return entry, status in _LIVE, problem


def _crash(run: Run, now: datetime | None = None) -> str | None:
    """Why `run` has no runner although it should have one (`error`, or `running`/`refining` with its runner
    gone), or None. A run with no lock yet counts only once it has been quiet for `STALL_GRACE_S`."""
    status = run.state["status"]
    if status == "error":
        return _first_line(run.state.get("failure")) or "the runner stopped on an error"
    if status not in _ACTIVE:
        return None
    held = lock_state(run.dir)
    if held == "stalled":
        return "its runner process is gone"
    if held == "idle":
        last = parse_iso(run.state.get("updated_at"))
        quiet = (now or datetime.now(UTC)) - last if last else None
        if quiet is None or quiet.total_seconds() > STALL_GRACE_S:
            return f"no runner holds it and nothing was recorded since {run.state.get('updated_at') or '?'}"
    return None


def _cycle_through(start: str, deps: dict[str, list[str]]) -> list[str] | None:
    """A dependency cycle that `start` is on, as `[start, ..., start]`, or None."""
    stack = [(start, [start])]
    seen: set[str] = set()
    while stack:
        node, path = stack.pop()
        for dep in deps.get(node, []):
            if dep == start:
                return [*path, start]
            if dep in deps and dep not in seen:
                seen.add(dep)
                stack.append((dep, [*path, dep]))
    return None


def _map_problem(node: sources.ModuleNode, deps: dict[str, list[str]], noun: str = "capability map") -> str | None:
    unknown = [d for d in node.deps if d not in deps]
    if unknown:
        return f"depends on {', '.join(unknown)}, which is not in the {noun}"
    cycle = _cycle_through(node.id, deps)
    if cycle:
        members = cycle[:-1]
        first = members.index(min(members))  # every module on the cycle names it the same way
        ring = members[first:] + members[:first]
        return "dependency cycle: " + " -> ".join([*ring, ring[0]])
    return None


def _blocked(entries: list[dict], problems: dict[str, str | None], nodes: list[sources.ModuleNode]) -> dict[str, str]:
    """The reason each stuck module gives: its own problem, else the problem of a dependency it waits on."""
    by_id = {e["id"]: e for e in entries}
    out: dict[str, str] = {}
    for node in nodes:
        entry = by_id[node.id]
        if problems.get(node.id):
            out[node.id] = str(problems[node.id])
        elif entry["status"] == "backlog" and not entry["run_id"]:
            for dep in node.deps:
                if problems.get(dep):
                    out[node.id] = f"waiting on {dep}: {problems[dep]}"
                    break
    return out


@dataclass
class Assessment:
    """What `assess_modules` found: the new `queue.json` entries and why some modules cannot move."""

    nodes: list[sources.ModuleNode]
    entries: list[dict]
    problems: dict[str, str | None]
    blocked: dict[str, str]
    occupied: int
    prior: dict[str, dict]


def _report(entry: dict, reason: str, prior: dict[str, dict], say: Callable[[str], None]) -> None:
    entry["blocked"] = reason
    if prior.get(entry["id"], {}).get("blocked") != reason:
        say(f"queue: {entry['id']} blocked \u2014 {reason}")


def assess_modules(cfg: Config, repo: Path, log: Callable[[str], None] | None = None) -> Assessment:
    """Read the work source and every module's run, write `runs/queue.json`, and start nothing.

    A module that cannot move without a person is reported through `log` as `queue: <id> blocked - <reason>`,
    once per change of reason; the reason is kept in `queue.json`, so a later tick or process stays quiet
    about it. A dependent says it is waiting on the module that is stuck.
    """
    repo = Path(repo)
    say = log or (lambda _message: None)
    nodes = work_source.items(cfg, repo)
    prior = {m["id"]: m for m in QueueState.load(repo).modules}
    runs = feature_runs(cfg, repo)
    graph = {n.id: list(n.deps) for n in nodes}
    noun = work_source.noun(cfg)
    view = origin_view(cfg, repo)  # one bounded read of origin per assessment
    known = known_ids(cfg, repo, view)
    marks = finished.read_marks(repo)
    entries, problems, occupied, notes = [], {}, 0, []
    if marks[1]:
        notes.append(f"queue: {marks[1]}; until then origin evidence marks no module newly done")
    for node in nodes:
        entry, holds, problem = _assess(cfg, repo, node, runs.get(node.id), prior.get(node.id, {}), notes,
                                        view, known, marks)
        entries.append(entry)
        problems[node.id] = _map_problem(node, graph, noun) or problem
        occupied += holds
    for message in notes:
        say(message)
    blocked = _blocked(entries, problems, nodes)
    for entry in entries:
        if entry["id"] in blocked:
            _report(entry, blocked[entry["id"]], prior, say)
    QueueState(entries).save(repo)
    return Assessment(nodes, entries, problems, blocked, occupied, prior)


def queue_tick(
    cfg: Config, repo: Path, max_parallel: int, start: Callable[[str], str],
    log: Callable[[str], None] | None = None,
) -> list[str]:
    """Start the ready modules in map order while fewer than `max_parallel` runs are live; return their ids.

    A module that cannot move without a person (a failed or cancelled run, a merge state `gh` could not
    give, an unknown dependency, a dependency cycle, a start that failed) is reported through `log`; see
    `assess_modules`.
    """
    if max_parallel < 1:
        raise ValueError(f"max_parallel must be at least 1, got {max_parallel}")
    repo = Path(repo)
    say = log or (lambda _message: None)
    found = assess_modules(cfg, repo, log=log)
    entries, blocked, occupied = found.entries, found.blocked, found.occupied
    state = QueueState(entries)
    done = {e["id"] for e in entries if e["status"] == "done"}
    started: list[str] = []
    for entry in entries:
        if entry["status"] != "backlog" or entry["run_id"] or entry["id"] in blocked or not set(entry["deps"]) <= done:
            continue
        if occupied >= max_parallel:
            break
        try:
            entry["run_id"] = start(entry["id"])
        except StartFailed as exc:
            entry["run_id"] = None
            _report(entry, f"could not start: {_first_line(str(exc))}", found.prior, say)
            continue
        entry["status"] = "running"
        occupied += 1
        started.append(entry["id"])
        state.save(repo)  # a start is recorded at once, so a crash cannot start the module twice
    state.save(repo)
    return started



# --------------------------------------------------------------------------- what may run beside what


def cached_entries(repo: Path, nodes: list[sources.ModuleNode], runs: dict[str, Run]) -> list[dict]:
    """`queue.json`-shaped entries for the map, from files only (no git, no `gh`): `done` (with its `done_reason`) is
    what the last assessment recorded with the live hand marks and vetoes on top (`finished.recorded`), a module with
    a run is `running` or `waiting`, the rest is `backlog`."""
    prior = {m["id"]: m for m in QueueState.load(repo).modules}
    done = finished.recorded(repo)
    entries = []
    for node in nodes:
        run = runs.get(node.id)
        if node.id in done:
            status = "done"
        elif run is None:
            status = "backlog"
        else:
            status = "running" if run.state["status"] in _ACTIVE else "waiting"
        entry = {"id": node.id, "deps": list(node.deps), "status": status, "run_id": run.id if run else None,
                 "blocked": prior.get(node.id, {}).get("blocked")}
        if status == "done":
            entry["done_reason"] = done[node.id]
        entries.append(entry)
    return entries


_RUN_WORDS = {"running": "running", "refining": "running", "waiting_human": "waiting", "awaiting_approval": "waiting",
              "paused": "waiting", "ready": "waiting", "done": "PR open", "failed": "failed",
              "cancelled": "cancelled", "error": "stopped on an error"}


def dependency_view(
    nodes: list[sources.ModuleNode], entries: list[dict], cards: list, *,
    merged: dict[str, bool | None] | None = None, runs: dict[str, Run] | None = None, noun: str = "capability map",
) -> dict[str, dict]:
    """For each unfinished item, `blocked_by`, `parallel_with` and `parallel`.

    Items are `module:<id>` (the work source, with the states in `entries`; `noun` is what a module missing from it
    is "not in") and `card:<id>` (cards added on the page; each has `id`, `label`, `status`, `run_id`, `after`
    and `deps`). A dependency is met when its pull request
    is merged: a map module once `entries` says `done`, a started card or a `run:<id>` once `merged[run_id]` is
    true. A card still in Backlog, a run still going, an open pull request and a failed or cancelled run all keep
    their dependents blocked, and each says which.

    `parallel_with` lists unfinished items with no dependency path to or from the item (transitively). `parallel`
    holds when the item is unblocked and at least one of those is unblocked or running now.
    """
    merged, runs = merged or {}, runs or {}
    by_entry = {e["id"]: e for e in entries}
    cards = [c for c in cards if c.status != "removed"]
    owner: dict[str, str] = {}  # run id -> the item that owns the run
    for e in entries:
        if e.get("run_id"):
            owner[e["run_id"]] = f"module:{e['id']}"
    for c in cards:
        if c.run_id:
            owner[c.run_id] = f"card:{c.id}"
    labels = {f"module:{n.id}": n.id for n in nodes} | {f"card:{c.id}": c.label for c in cards}

    def norm(ref: str) -> str:
        kind, _, name = ref.partition(":")
        return owner.get(name, ref) if kind == "run" else ref

    def run_word(run_id: str | None) -> str:
        run = runs.get(run_id) if run_id else None
        return _RUN_WORDS.get(run.state["status"], run.state["status"]) if run else "missing"

    def is_done(key: str) -> bool:
        kind, _, name = key.partition(":")
        if kind == "module":
            return name in by_entry and by_entry[name]["status"] == "done"
        if kind == "card":
            card = next((c for c in cards if c.id == name), None)
            return bool(card and card.run_id and merged.get(card.run_id) is True)
        return merged.get(name) is True

    def state_word(key: str) -> str:
        kind, _, name = key.partition(":")
        if kind == "module":
            if name not in by_entry:
                return f"not in the {noun}"
            run_id = by_entry[name].get("run_id")
            return run_word(run_id) if run_id else "not started"
        if kind == "card":
            card = next((c for c in cards if c.id == name), None)
            if card is None:
                return "missing"
            if card.run_id and card.status == "started":
                return run_word(card.run_id)
            return "starting" if card.status == "starting" else "in Backlog"
        return run_word(name)

    def label(key: str) -> str:
        kind, _, name = key.partition(":")
        if kind == "run":
            run = runs.get(name)
            params = (run.state.get("params") or {}) if run else {}
            return str(params.get("module") or (f"issue #{params['issue']}" if params.get("issue") else name))
        return labels.get(key, name)

    keys = [f"module:{n.id}" for n in nodes] + [f"card:{c.id}" for c in cards]
    deps: dict[str, list[str]] = {f"module:{n.id}": [f"module:{d}" for d in n.deps] for n in nodes}
    for c in cards:
        deps[f"card:{c.id}"] = list(dict.fromkeys(norm(r) for r in [*c.after, *c.deps]))
    unfinished = [k for k in keys if not is_done(k)]

    def ancestors(key: str) -> set[str]:
        seen: set[str] = set()
        stack = list(deps.get(key, []))
        while stack:
            dep = stack.pop()
            if dep not in seen:
                seen.add(dep)
                stack.extend(deps.get(dep, []))
        return seen

    above = {k: ancestors(k) for k in keys}
    blocked = {k: [{"key": d, "text": f"{label(d)} ({state_word(d)})"} for d in deps.get(k, []) if not is_done(d)]
               for k in unfinished}

    def running(key: str) -> bool:
        kind, _, name = key.partition(":")
        if kind == "module":
            run = runs.get(by_entry[name].get("run_id") or "")
            return bool(run and run.state["status"] in _LIVE)
        card = next(c for c in cards if c.id == name)
        run = runs.get(card.run_id or "")
        return card.status == "starting" or bool(run and run.state["status"] in _LIVE)

    def started(key: str) -> bool:
        kind, _, name = key.partition(":")
        if kind == "module":
            return bool(by_entry[name].get("run_id"))
        card = next(c for c in cards if c.id == name)
        return card.status in ("starting", "started")

    def can_run_beside(key: str) -> bool:
        """Something to run now: a started item counts while its run is live (not while it waits for its merge),
        an item still in Backlog counts when nothing blocks it."""
        return running(key) if started(key) else not blocked[key]

    peers = {k: [o for o in unfinished if o != k and o not in above[k] and k not in above[o]] for k in unfinished}
    return {k: {"blocked_by": blocked[k], "parallel_with": peers[k],
                "parallel": not blocked[k] and any(can_run_beside(o) for o in peers[k])}
            for k in unfinished}


@contextmanager
def queue_lock(repo: Path) -> Iterator[None]:
    """Hold `runs/queue.lock` so two `agentflow queue` processes cannot start the same module."""
    path = runs_root(repo) / "queue.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise QueueBusy(f"another `agentflow queue` is running (lock: {path})") from exc
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def run_queue(
    cfg: Config, repo: Path, max_parallel: int, start: Callable[[str], str], *,
    once: bool = False, interval: float = INTERVAL_S, sleep: Callable[[float], None] = time.sleep,
    log: Callable[[str], None] | None = None,
) -> int:
    """Tick every `interval` seconds until interrupted (Ctrl-C ends it cleanly); `once` ticks one time.

    A pack with no `work_source` has no modules to start, so the queue refuses to run."""
    from agentflow import hitl

    if cfg.work_source is None:
        raise ValueError("this pack has no work_source: the queue has no modules to start")
    say = log or print
    with queue_lock(repo):
        try:
            while True:
                gitops.fetch_remote_branch(repo, cfg.project.fork_from)  # where a merged plan lands (see _plan_finished)
                for module in queue_tick(cfg, repo, max_parallel, start, log=say):
                    say(f"started {module}")
                hitl.remind_due(cfg, repo, datetime.now(UTC))  # waits past `hitl.remind_after_h` are notified again
                if once:
                    return 0
                sleep(interval)
        except KeyboardInterrupt:
            say("queue stopped")
            return 0


_LANE = re.compile(r"^[a-z][a-z0-9_-]*$")
SPAWN_PARAMS = ("module", "topic", "issue", "task", "slice")  # never `log`: it reads any local path into the agent


def spawn_run(
    repo: Path, lane: str, params: dict[str, str], *, to_phase: str | None = None,
    new_module: dict | None = None, command: list[str] | None = None, config: Path | None = None,
) -> str:
    """Start `flow.py run <lane> --<param>=<value> ... --detach` and return the new run's id.

    Every value travels as one `--name=value` argv item, so text that starts with `-` cannot become a flag,
    and nothing passes through a shell. The child is a new session with stdin closed and its output in
    `runs/queue.log`. With `--detach` it exits once the run exists and a background runner has taken over.
    """
    if not _LANE.fullmatch(lane):
        raise StartFailed(f"lane {lane!r} is not a plain slug")
    if not params:
        raise StartFailed("a run needs one of " + ", ".join(f"--{p}" for p in SPAWN_PARAMS))
    argv = [*(command or [sys.executable, str(Path(__file__).resolve().parent / "flow.py")]), "run", lane]
    for name, value in params.items():
        if name not in SPAWN_PARAMS:
            raise StartFailed(f"{name!r} cannot be set from here")
        text = str(value)
        if name == "module" and not _MODULE_ID.fullmatch(text):
            raise StartFailed(f"module id {text!r} is not a plain slug")
        if "\0" in text or not text.strip():
            raise StartFailed(f"{name} must be non-empty text without NUL bytes")
        argv.append(f"--{name}={text}")
    if to_phase is not None:
        argv.append(f"--to={to_phase}")
    if new_module is not None:
        argv.append("--new-module=" + json.dumps(new_module, ensure_ascii=False))
    argv.append("--detach")
    if config is not None:
        argv += ["--config", str(config)]
    label = " ".join(f"{k}={v!r}" for k, v in params.items())
    log_path = runs_root(repo) / "queue.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a+", encoding="utf-8") as log:
        offset = log.seek(0, os.SEEK_END)
        try:
            proc = subprocess.run(argv, cwd=repo, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                  start_new_session=True, timeout=SPAWN_TIMEOUT_S, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise StartFailed(f"could not run flow.py for {label}: {exc}") from exc
        log.seek(offset)
        output = log.read()
    if proc.returncode != 0:
        detail = output.strip().splitlines()[-1] if output.strip() else f"exit {proc.returncode}"
        raise StartFailed(detail)
    match = _STARTED.search(output)
    if not match:
        raise StartFailed(f"flow.py did not report a run id for {label} (see {log_path})")
    return match.group(1)


def spawn_feature_run(
    repo: Path, module: str, *, lane: str, command: list[str] | None = None, config: Path | None = None,
) -> str:
    """`spawn_run` for one capability-map module in the module lane (`module_lane(cfg)`)."""
    return spawn_run(repo, lane, {"module": module}, command=command, config=config)


__all__ = [
    "Assessment", "NextAnswer", "OriginView", "QueueBusy", "QueueState", "StartFailed", "assess_modules",
    "cached_entries", "dependency_view", "done_reason", "format_answer", "known_ids", "origin_view", "own_plans",
    "module_lane", "next_step", "queue_lock", "queue_tick", "run_queue", "spawn_feature_run", "spawn_run",
]
