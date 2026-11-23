"""The phase loop: start a run, then advance it phase by phase until a human is needed or it ends.

`start` creates the run and where it works (D7, amended by D16): a worktree on `flow/<lane>/<slug>` forked
from `project.fork_from`, for every lane.

`advance` holds the run lock and walks the workflow's phases from the first (or `--from`). A phase
whose `skip_if` holds is skipped (`phase_skipped`); otherwise it starts (`phase_started`, so its step
records, and so its agent sessions, are its own) and its steps run. A phase in which nothing ran is
skipped too, which is how each lane walks the one workflow: a phase with no step for the run's lane
stops nobody. A phase that ran is then settled (`_Loop.settle`): with `needs_approval: true` it ends at
`<phase>_approved` and the run stops; with `false` the runner approves it itself once its `blocks_on_findings`
steps hold nothing blocking, opening refine rounds on what they found, up to `autonomy.finding_rounds`, and
then going to a human after all. A `needs_approval: true` phase with a `reconciles` step (a doubt review of the
spec or the plan) first opens reconcile rounds on its review's blocking findings, until `doubt.stop_reason` ends
them: only the reconcile step and the review run again, and the approval page shows each finding's disposition. Stops inside a phase go to the resolver first (`autonomy.Resolver`). After `phase_approved`
the runner writes `handoff.md`, checks the last skill footer against the next phase's first skill
(`footer_mismatch`; the route never changes), ends the run if that phase was `--to`, and goes on.
A rejected phase runs again as a refine round: every step of it again with the note, then it is settled
again, whatever the steps did (a refine round never skips the phase).
A human `pause` or `cancel` (`run_control`) is honoured before every step (`flow_control.halted`), so a
live agent finishes its step first; `advance` on a paused run records `run_resumed` and goes on.
An approved phase can rest as ready (`hitl.approve(hold=True)`): nothing runs until a human goes on, which
also lifts `--to`, or marks the run done, which skips every phase but the last, so a run stopped early
still ends with a pull request.
A runner on the repository's pack re-reads it before each phase (`_Loop.reload`), so a change saved from the
dashboard's Settings page, or by hand, reaches a live run at its next phase: `config_reloaded`. A config that
does not load or validate, or that changes the phases' names or order, is not taken (`config_reload_skipped`,
with the reason); the run goes on with the one it has, and the next `resume` loads the file as it is then.
A run on another config (`--config`) keeps it as it was loaded: `config_trust` decides when that file may change.
With `slots.count` above 1 a run holds a slot for as long as it is live (`slots.starting` at `start`, `slots.acquire`
at every `advance` and every reload, so a run that started with slots off takes one at its next phase), and the
runner's config is a copy with `{slot}` filled in (`slots.render`, also after a reload; a reload that finds no free
slot is skipped).
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from functools import partial
from pathlib import Path
from typing import TextIO

from agentflow import keepawake
from agentflow.agents import make_agents
from agentflow.autonomy import (
    AUTO_BY,
    Resolver,
    blocking_of,
    finding_line,
    finding_rounds,
    findings_note,
    phase_findings,
    round_limit,
    runner_settles,
)
from agentflow import (
    constraint, doubt, governor, pack, preflight, route_lane, run_env, safeio, slots, speculation, work_source,
)
from agentflow.commit_checks import runner_head
from agentflow.conditions import Ctx, evaluate, fmt, make_ctx
from agentflow.config_trust import layers_sha256, start_options
from agentflow.config import (
    RETRO_PIPELINE, Config, ConfigError, Phase, Step, check_lane, for_lane, layer_files, load_config, validate,
    worktrees_root,
)
from agentflow.flow_control import condition_label, halted, read_json, writes_triage
from agentflow.gitops import (
    GitError,
    add_worktree,
    branch_exists,
    changed_between,
    changed_since,
    commit,
    diff_stat,
    excluding,
    head,
    restore_paths,
    uncommitted_paths,
)
from agentflow.guards import snapshot
from agentflow.hitl import notify_run, open_refine, parks_first, raise_hitl
from agentflow.intake import gather, save
from agentflow.issue_status import mark, release
from agentflow.lanes import Executor, Outcome, footer_next
from agentflow.state import MARKED_DONE_SKIP, TERMINAL, Run, speculating

TRIAGE_PAGE_LIMIT = 20_000  # bytes of `triage.md` the approval page carries
DEFAULT_SHOW = ["handoff", "diff", "step_results", "skipped_steps", "gates", "review_findings"]
DONE_STEPS = ("done", "unchanged")

__all__ = [
    "RunLocked", "RunOptions", "advance", "dry_run", "footer_next", "needs_approval", "start",
    "write_handoff",
]


class RunLocked(Exception):
    """Another runner holds this run's lock."""


@dataclass
class RunOptions:
    from_phase: str | None = None
    to_phase: str | None = None
    model_overrides: dict[str, str] = field(default_factory=dict)
    dry_run: bool = False
    detach: bool = False
    new_module: dict | None = None  # {module, responsibility, deps}: a module not in the map yet; the run adds its row


def _holds_ship(steps: list[Step]) -> bool:
    """Whether a `finish` or `ci` step is among `steps` or inside one of their groups: a phase the usage window's soft
    mark never holds, so a run that is done with review still opens its pull request."""
    return any(step.type in ("finish", "ci") or _holds_ship(step.steps) for step in steps)


def _governor_key(phase: str) -> str:
    return f"{phase}/{governor.GOVERNOR_STEP}"


def needs_approval(phase: Phase) -> bool:
    """Whether a human approves `phase` once it ran: its own `needs_approval` in `config.yaml`, nothing else."""
    return phase.needs_approval


# --------------------------------------------------------------------------- start


def _check_range(cfg: Config, lane: str, opts: RunOptions) -> list[str]:
    check_lane(cfg, lane)
    names = list(cfg.phases)
    for flag, name in (("--from", opts.from_phase), ("--to", opts.to_phase)):
        if name is not None and name not in names:
            raise ValueError(f"{flag} {name!r} is not a phase: {', '.join(names)}")
    if opts.from_phase and opts.to_phase and names.index(opts.to_phase) < names.index(opts.from_phase):
        raise ValueError(f"--to {opts.to_phase} must be at or after --from {opts.from_phase}")
    return names


def _free_branch(repo: Path, name: str) -> str:
    branch, n = name, 1
    while branch_exists(repo, branch):
        n += 1
        branch = f"{name}-{n}"
    return branch


def start(cfg: Config, repo: Path, pipeline: str, params: dict, opts: RunOptions) -> Run:
    """Create the run and the checkout it works in (D7), recording the options for `resume`.

    The run's inputs (`--issue`, `--log`, `--task`; `intake`) are read before anything is created, so a
    bad one leaves no run, and saved in the run directory once it exists.
    """
    _check_range(cfg, pipeline, opts)
    _check_new_module(cfg, params, opts.new_module)
    repo = Path(repo).resolve()
    fork = cfg.project.fork_from
    if not branch_exists(repo, fork):
        raise GitError(f"project.fork_from {fork!r} is not a local branch of {repo}")

    if pipeline == "vuln" and not cfg.vuln.probe:
        raise ValueError("the vuln lane needs `vuln.probe`, the audit gate that must name the advisory before the patch "
                         "and not after: set it in the pack's config (the starters set `audit`)")
    slots.check_free(cfg, repo)  # the common refusal, before the inputs are read
    params, inputs = gather(params, repo, cfg.issues.epic_labels)
    # `{date}` (in `plan_target`) is the day the run started, every day it runs: a step resumed tomorrow still names
    # the file the plan step wrote today, and a reconcile step may edit only that file.
    if params.get("date") in (None, ""):
        params["date"] = date.today().isoformat()
    run = None
    try:
        # one hold of the slots lock: the check again, the run and its number, so a start that lost the last number
        # while it read its inputs raises `SlotsFull` with no run created
        with slots.starting(cfg, repo) as take:
            run = Run.create(cfg, repo, pipeline, params)
            take(run)
        _set_up(cfg, repo, run, pipeline, params, inputs, opts)
    except Exception as exc:  # once the run exists it must never be left `running` with nothing behind it
        if run is not None:
            run.emit("run_failed", reason=f"{type(exc).__name__}: {exc}")
        raise
    mark(cfg, run)
    return run


def _set_up(cfg: Config, repo: Path, run: Run, pipeline: str, params: dict, inputs: dict, opts: RunOptions) -> None:
    save(run, inputs)
    run.emit("run_options", from_phase=opts.from_phase, to_phase=opts.to_phase,
             model_overrides=dict(opts.model_overrides), **start_options(cfg.path, layer_files(cfg)))
    slug = make_ctx(cfg, repo, pipeline, params).values["slug"]
    branch = _free_branch(repo, cfg.project.branch.replace("{lane}", pipeline).replace("{slug}", slug))
    path = (worktrees_root(cfg, repo) / run.id).resolve()
    add_worktree(repo, path, branch, cfg.project.fork_from)
    run.emit("run_updated", branch=branch, worktree=str(path), base_sha=head(path))
    template = _check_new_module(cfg, params, opts.new_module)
    if template is not None:
        _add_module_row(cfg, run, path, template, opts.new_module)


def _check_new_module(cfg: Config, params: dict, new_module: dict | None) -> dict | None:
    """The module row template for `new_module`, or None when the run adds no module. Raises `ValueError` when it
    cannot be added, before any run exists."""
    if new_module is None:
        return None
    if cfg.work_source is None:
        raise ValueError("a new module needs a work_source in config.yaml: this pack has no table to add it to")
    if cfg.work_source.kind == "specs":
        raise ValueError("with work_source specs a new module has no row: run --module <id> --task <text>")
    template = (cfg.dashboard.get("backlog") or {}).get("module_row")
    if not isinstance(template, dict) or not template:
        raise ValueError("a new module needs dashboard.backlog.module_row in config.yaml")
    module = str(new_module.get("module"))
    if params.get("module") != module:
        raise ValueError(f"--new-module is for module {module!r}, but the run is for {params.get('module')!r}")
    return template


def _add_module_row(cfg: Config, run: Run, worktree: Path, template: dict, new_module: dict) -> None:
    """Write a new module's row into the work source of the run's worktree and commit it as the branch's
    first commit, so every step that reads the map finds it. The runner does this, not an agent."""
    work_source.add_item(cfg, worktree, template, new_module)
    relative = cfg.work_source.path if cfg.work_source is not None else ""
    module = str(new_module["module"])
    sha = commit(worktree, f"Add module {module} to the capability map", cfg, paths=[relative],
                 expected_head=runner_head(run))
    run.emit("module_added", module=module, sha=sha, path=relative)


# --------------------------------------------------------------------------- advance


def _effective(run: Run, opts: RunOptions | None) -> RunOptions:
    """The CLI's options where given, else what the run was started with."""
    opts = opts or RunOptions()
    stored = run.state.get("options") or {}
    return RunOptions(
        from_phase=opts.from_phase or stored.get("from_phase"),
        to_phase=opts.to_phase or stored.get("to_phase"),
        model_overrides=dict(opts.model_overrides or stored.get("model_overrides") or {}),
        dry_run=opts.dry_run,
        detach=opts.detach,
    )


def _ctx(cfg: Config, run: Run, repo: Path) -> Ctx:
    ctx = make_ctx(cfg, repo, run.state["pipeline"], run.state["params"], run)
    ctx.base_ref = f"origin/{cfg.project.base_branch}"
    return ctx


def advance(run: Run, cfg: Config, opts: RunOptions | None = None) -> str:
    """Run phases until a human is needed or the run ends; returns the run's status then.

    An unexpected exception is recorded as `runner_error` (the run's status becomes `error`, which a
    `resume` goes on from) before the lock is released and the exception goes on to the caller. Taking the run's
    slot comes first: `slots.SlotsFull` (or any failure of that step) releases the lock and is raised with no
    `runner_error`, so the run's status is left as it was."""
    if run.refresh().get("pipeline") == RETRO_PIPELINE:  # it walks no phase: resuming one would run the whole workflow
        raise ValueError(f"{run.id} is a retro run, which cannot be resumed; cancel it and run `agentflow retro` again")
    lock = run.lock()
    if not lock.acquire():
        raise RunLocked(f"another runner is working on {run.id}; see `agentflow status {run.id}`")
    try:
        slot = slots.slot_of(run) if run.refresh()["status"] in TERMINAL else slots.acquire(cfg, run)
    except BaseException:  # `SlotsFull`, or a failure reading or writing the slot: nothing ran, so no `runner_error`, and
        lock.release()  # no issue label for `release` to take off (the run is not terminal); the lock must not outlive this
        raise
    try:
        with keepawake.holding():  # until a human is needed or the run ends
            return _Loop(run, cfg, _effective(run, opts), slot).go()
    except Exception as exc:
        run.emit("runner_error", error=str(exc) or repr(exc), exception=type(exc).__name__)
        raise
    finally:
        lock.release()
        release(run)  # after the lock, and it never raises, so it cannot hide the exception on its way out


class _Loop:
    def __init__(self, run: Run, cfg: Config, opts: RunOptions, slot: int) -> None:
        self.run = run
        self.slot = slot
        self.cfg = slots.render(cfg, slot)  # the run's own copy: `{slot}` is its number from here on
        self.opts = opts
        self.agents = make_agents(self.cfg)
        self.config_sha = self._config_sha()
        self._speculating: str | None = None  # the phase walked while the approval before it waits (`speculate`)
        self._yield_if = None  # what ends that walk at a step boundary (`speculation.cut`)

    def _config_sha(self) -> str | None:
        """The digest of the layer files (engine defaults, the pack, the user file) when the config is the repository's
        pack (the only one reloaded), else None. It reads bytes only: a broken file still has a digest."""
        path = Path(self.cfg.path).resolve()
        try:
            if path != Path(pack.pack_config(self.run.repo)).resolve():
                return None
        except (pack.PackError, subprocess.CalledProcessError, OSError):
            return None
        try:
            return layers_sha256(layer_files(self.cfg))
        except OSError:
            return None

    def reload(self) -> None:
        """Take a changed pack config before a phase starts, when it loads, validates and keeps the phases."""
        if self.config_sha is None:
            return
        sha = self._config_sha()
        if sha is None or sha == self.config_sha:
            return
        self.config_sha = sha
        try:
            cfg = load_config(self.cfg.path)
            errors = [f"{i.path}: {i.message}" for i in validate(cfg, self.run.repo) if i.level == "error"]
        except ConfigError as exc:
            errors = [f"{exc.path}: {exc.message}" if exc.path else exc.message]
        if not errors and list(cfg.phases) != list(self.cfg.phases):
            errors = ["the phases' names or order changed; a live run keeps its phases (resume to take them)"]
        if not errors:
            try:  # a run that started with slots off takes its number here when they were turned on
                slot = slots.acquire(cfg, self.run)
            except slots.SlotsFull as exc:
                errors = [f"no free slot for this run: {exc}"]
        if errors:
            self.run.emit("config_reload_skipped", sha256=sha, reason="; ".join(errors)[:2000])
            return
        self.slot = slot
        self.cfg = slots.render(cfg, self.slot)
        self.agents = make_agents(self.cfg)
        self.run.emit("config_reloaded", sha256=sha)

    def status(self) -> str:
        self.run.refresh()
        return self.run.state["status"]

    def executor(self) -> Executor:
        """A fresh executor per phase, so the context (the plan path, ...) reflects the tree now. While the runner walks a
        phase behind a pending approval, it carries that phase and the check that ends the walk."""
        executor = Executor(self.cfg, self.run, self.agents, _ctx(self.cfg, self.run, self.worktree),
                            self.opts.model_overrides)
        executor.speculating = self._speculating
        executor.yield_if = self._yield_if
        return executor

    def finish(self) -> str:
        self.run.emit("run_finished")
        return "done"

    def record_env(self) -> None:
        """Record, by name only, the inherited variables this run does not pass on, when the set is not the one
        last recorded (so a start records it, and a resume only when it changed)."""
        names = sorted({name for name, _ in run_env.dropped(self.cfg)})
        if names != self.run.state.get("env_dropped", []):
            self.run.emit("env_dropped", names=names)

    def go(self) -> str:
        run = self.run
        run.refresh()
        state = run.state
        if state["status"] in TERMINAL:
            return state["status"]
        if not state.get("worktree"):
            raise ValueError(f"{run.id} has no worktree recorded; runs are started with `agentflow run`")
        self.worktree = Path(state["worktree"])
        if state.get("hold"):  # a runner starting holds nothing yet: this hold is one a dead runner left open
            run.emit("run_hold_ended", key=state["hold"]["key"])
        if state["status"] == "ready":  # a human chose to go on: `--to` no longer ends the run here
            self.opts.to_phase = None
        if state["status"] in ("paused", "error", "ready"):  # a human resumed, or the last runner stopped on an error
            run.emit("run_resumed")
        self.record_env()
        self.tell_speculative()
        names = list(self.cfg.phases)
        first = self.opts.from_phase or names[0]
        if self.cfg.preflight.enabled and self.preflight(f"{first}/{preflight.PREFLIGHT_STEP}"):
            return "waiting_human"
        # A triage in the log is checked whatever the pack says now; off with none builds nothing: today's run.
        history = route_lane.triage_history(run.events.read(), first)
        if self.cfg.route.mode in ("observe", "on") or history:
            executor = self.executor()
            if executor.route_due():
                if not executor.govern(_governor_key(first), "soft", first):  # the soft mark holds a new run's triage
                    return self.status()
                if executor.size_route(first) == "stopped":
                    return self.status()
            elif history and executor.route_backstop(first) == "stopped":
                return self.status()
        for index in range(names.index(first), len(names)):
            name = names[index]
            outcome = self.phase(names, index)
            if outcome is not None:
                return outcome
            if name == self.opts.to_phase and not run.state["marked_done"]:
                return self.finish()
        return self.finish()

    def preflight(self, key: str) -> bool:
        """Check the machine before the run's first phase; True when it stopped for a human.

        It runs once: not for a run that started a phase, and not again after a pass. A stop is the human's whatever
        the phase says (`NEVER_RESOLVED`), and approving it runs this check again. A run still waiting on the
        stop it raised stays waiting."""
        run = self.run
        events = run.events.read()
        if any(e["type"] == "phase_started" for e in events):
            return False
        checked = [e for e in events if e["type"] == "preflight_checked"]
        if any(e.get("ok") for e in checked):
            return False
        if (run.state["waiting"] or {}).get("kind") == "preflight_failed":
            return True
        outcome = preflight.run_preflight(self.cfg, run, self.worktree, key)
        if not outcome.ok:
            raise_hitl(self.cfg, run, "preflight_failed", key,
                       f"Preflight found {len(outcome.lines)} problem(s) before the run started: {outcome.lines[0]}",
                       extra="\n".join(outcome.lines))
            return True
        if checked:  # an earlier check failed and a human fixed the machine
            run.emit("step_finished", key=key, status="done", summary="preflight passed")
        return False

    def phase(self, names: list[str], index: int) -> str | None:
        """Settle one phase; a status to stop with, or None to go on to the next phase."""
        name = names[index]
        self.reload()
        phase = self.cfg.phases[name]
        run = self.run
        run.refresh()
        state = run.state
        if halted(state["status"]):  # cancelled, or paused by a human since the last phase
            return state["status"]
        self.end_left_speculation()
        state = run.state
        waiting = state["waiting"]
        if waiting is not None and waiting.get("kind") == "phase_approval":
            if waiting.get("phase") == name:  # a resumed runner picks up the walk behind this approval
                return self.speculate(names, index, self.executor())
            if self._speculating != name:
                return "awaiting_approval"
        executor = self.executor()
        if self._speculating != name and self.replan_after_approval(names, index, executor):
            state = run.state
        row = state["phases"].get(name) or {}
        if refining(state, name):
            self.discard_walked(name, executor)
            if self.walk(phase, executor)[0] == "stopped":
                return self.status()
            return self.settle(names, index, executor)
        if row.get("status") == "approved" and not retried(run.events.read(), name):
            if not row.get("handoff"):
                self.hand_off(names, index, executor)
            return None
        if row.get("status") == "skipped":
            return None
        marked = state["marked_done"]
        if marked and index < len(names) - 1 and row.get("status") in (None, "pending"):
            run.emit("phase_skipped", phase=name, reason=f"{MARKED_DONE_SKIP} {marked['phase']}")
            return None
        if row.get("status") in (None, "pending"):
            if phase.skip_if is not None:
                skip, reason = executor.decide(phase, phase.skip_if, f"{name}/skip_if")
                if skip is None:
                    return self.status()
                if skip:
                    run.emit("phase_skipped", phase=name, reason=reason, via="skip_if", decided_by=executor.last_decided_by)
                    return None
            # The soft mark holds a phase about to start, never the phase that ships the run.
            if not _holds_ship(phase.steps) and not executor.govern(_governor_key(name), "soft", name):
                return self.status()
            if executor.route_record() is not None:  # a plan bigger than the triage said raises the route first
                executor.reroute(tasks=executor.plan_open_tasks(), risk=False)
            run.emit("phase_started", phase=name, start_sha=head(self.worktree))
        outcome, ran = self.walk(phase, executor)
        if outcome == "stopped":
            return self.status()
        if not ran:
            run.emit("phase_skipped", phase=name, reason="all steps skipped")
            return None
        return self.settle(names, index, executor)

    def walk(self, phase: Phase, executor: Executor) -> tuple[Outcome, bool]:
        """Run the phase's steps, letting the resolver settle what it can; `(outcome, any step ran)`. A wait the plan
        walk already handled (it ran the resolver on it, and parked it or told a person) is never taken over again.
        Before each pass, the override commits a person approved are written (`constraint.write`); one that cannot be
        is a stop the resolver never takes."""
        resolver = Resolver(self.cfg, self.run, executor)
        ran = False
        while True:
            if not resolver.finish_open():
                return "stopped", ran
            if not constraint.write(self.cfg, self.run, self.worktree):  # a person's override, before any step runs
                return "stopped", ran
            outcome, stepped = executor.run_steps(phase, phase.steps, phase.name)
            ran = ran or stepped
            if outcome == "stopped":
                self.run.refresh()
                waiting = executor.current_wait()
                if waiting is not None and executor.handled(waiting):
                    return outcome, ran
                if waiting is not self.run.state["waiting"]:  # queued behind the approval it walks behind: not the wait
                    return outcome, ran
            if outcome != "stopped" or not resolver.take_over():
                return outcome, ran

    def settle(self, names: list[str], index: int, executor: Executor) -> str | None:
        """The phase's steps are finished: wait for its human, or go on by itself once nothing blocks it.

        Blocking findings get a refine round the runner opens itself, up to `autonomy.finding_rounds`.
        After those, the findings go to a human on the phase's approval page. A phase that needs approval and has a
        `reconciles` step gets reconcile rounds on its doubt review's findings first (`settle_doubt`)."""
        name = names[index]
        phase = self.cfg.phases[name]
        self.catch_up_handoffs(names, index, executor)
        if needs_approval(phase):
            lane = self.run.state["pipeline"]
            if doubt.reconcile_steps(phase, lane):  # any other approval phase settles exactly as it always has
                walked, outcome = self.settle_doubt(names, index, executor, lane)
                if walked:
                    return outcome
            if self.held_back(name):
                return "awaiting_approval"
            # imported here: approval_ramp reads the scorecard, which reads `retro`, which imports this module
            from agentflow import approval_ramp

            decision = approval_ramp.decide(self.cfg, self.run, name, datetime.now(UTC))
            if decision is not None and decision["auto"]:  # the ramp approves it: no person is asked
                self.run.emit("phase_approved", by=AUTO_BY, phase=name, sha=head(self.worktree),
                              ramp={"samples": decision["samples"], "profile": decision["profile"]})
                self.hand_off(names, index, executor)
                return None
            raise_phase_approval(self.cfg, self.run, name)
            return self.speculate(names, index, executor)
        findings = phase_findings(self.cfg, self.run, name)
        tree = snapshot(self.worktree) if findings else ""
        for key in dict.fromkeys(f["key"] for f in findings):  # for the pull request: this round's, kept per step
            self.run.emit("step_findings", key=key, findings=[f for f in findings if f["key"] == key], tree=tree)
        blocking = blocking_of(self.cfg, findings)
        if not blocking:
            if self.held_back(name):
                return "awaiting_approval"
            self.run.emit("phase_approved", by=AUTO_BY, phase=name, sha=head(self.worktree))
            self.hand_off(names, index, executor)
            return None
        if finding_rounds(self.run, name) < round_limit(self.cfg, name, self.run.state.get("route")):
            open_refine(self.run, name, AUTO_BY, findings_note(blocking))
            return self.phase(names, index)
        if self.held_back(name):
            return "awaiting_approval"
        raise_phase_approval(self.cfg, self.run, name)
        return self.speculate(names, index, executor)

    def held_back(self, name: str) -> bool:
        """Whether `name` is the phase walked behind a pending approval: it settles at that approval, so it never
        approves itself or raises its own (either would take the approval's place as the run's wait)."""
        record = speculating(self.run.refresh())
        return record is not None and record["phase"] == name

    def speculate(self, names: list[str], index: int, executor: Executor) -> str | None:
        """`names[index]` raised its approval: walk the phase after it while the approval waits, when
        `speculation.due` says so. The walk settles at that approval; a stop in it is queued behind the approval,
        quietly. Between steps the walk reads what the person did (`speculation.cut`): an approval lets it go on as a
        normal phase, a rejection ends it and the approval phase's refine round runs now, and an approval with `hold`
        ends it and the run rests as ready. A revision of the approval phase is walked once (`speculation.basis`); a
        runner that died or was paused mid-walk goes on with it (no new `speculation_started`)."""
        run, after = self.run, names[index]
        if self._speculating is not None or self.opts.dry_run:  # never more than one phase ahead
            return "awaiting_approval"
        run.refresh()
        nxt = speculation.due(self.cfg, run, names, index, self.opts.to_phase)
        if nxt is None:
            return "awaiting_approval"
        basis = speculation.basis(self.cfg.phases[after], run.state["pipeline"], executor.ctx, self.worktree)
        record, ended = speculation.last_record(run.events.read(), nxt)
        same = record is not None and record.get("after") == after and record.get("basis") == basis
        if same and ended:
            return "awaiting_approval"
        if not same:
            if record is not None and not ended:  # an open walk on another basis (a spec edited while it was stopped)
                run.emit("speculation_ended", phase=nxt, outcome="stopped")
            if speculation.replan_due(run.state, run.events.read(), nxt, basis):  # it was walked on an older revision
                self.replan(names, names.index(nxt), basis)
            write_handoff(self.cfg, run, after)  # provisional, for the walked phase's `{handoff}`: no `handoff_written`
            try:  # what was uncommitted before the walk: never the walk's to discard (`discard_left`)
                dirty: dict = {"dirty": uncommitted_paths(self.worktree)}
            except GitError:
                dirty = {}
            run.emit("speculation_started", phase=nxt, after=after, basis=basis, sha=head(self.worktree), **dirty)
        self._speculating = nxt
        self._yield_if = partial(speculation.cut, after=after)
        try:
            outcome = self.phase(names, names.index(nxt))
        finally:
            self._speculating = None
            self._yield_if = None
        state = run.refresh()
        cut = speculation.cut(state, after)
        if cut == "rejected":
            run.emit("speculation_ended", phase=nxt, outcome="rejected", **self.discard_left(nxt, executor))
            return self.phase(names, index)
        if state["status"] in TERMINAL:
            run.emit("speculation_ended", phase=nxt, outcome="halted")
            return state["status"]
        if state["status"] == "paused":  # the record stays open: the next `advance` goes on with the walk
            return state["status"]
        if speculating(state) is not None:  # the approval still waits
            queued = any(w.get("speculative") and str(w.get("key") or "").split("/")[0] == nxt
                         for w in state["waiting_queue"])
            left = self.discard_left(nxt, executor, keep_outputs=True) if queued else {}
            run.emit("speculation_ended", phase=nxt, outcome="stopped" if queued else "finished", **left)
            return "awaiting_approval"
        run.emit("speculation_ended", phase=nxt, outcome="approved")
        self.tell_speculative()
        if cut == "ready":  # approved with `hold`: the run rests, and the walked phase goes on from its rows later
            return self.status()
        return outcome

    def replan(self, names: list[str], index: int, basis: dict[str, str]) -> None:
        """Send `names[index]`, last planned on another basis (`speculation.planned_on`), back once: a runner refine
        round of kind `replan` whose note names the approval phase's changed paths and the `git diff` to read. It is
        not a finding round (`finding_rounds`), and its `basis` is what the phase is planned on from here."""
        name = names[index]
        planned = speculation.planned_on(self.run.events.read(), name)
        old, sha = planned if planned is not None else ({}, "")
        note = speculation.replan_note(speculation.changed(old, basis), sha)
        open_refine(self.run, name, AUTO_BY, note, extra={"kind": speculation.REPLAN, "basis": basis, "from_sha": sha})
        self.run.refresh()

    def discard_walked(self, after: str, executor: Executor) -> None:
        """Before `after`'s refine round walks: put back, to `HEAD`, the uncommitted changes to the `output`s of the phase
        last walked behind its approval (a step of it stopped mid-step), unless they were uncommitted before that walk
        (its `dirty`). The walked phase is walked or re-planned again on the revision, and its edits must never go into
        `after`'s commit (`git add -A`); anything else the walk left was discarded when it ended (`discard_left`).
        Nothing is read but the log when no phase was walked behind `after`."""
        record = next((e for e in reversed(self.run.events.read())
                       if e["type"] == "speculation_started" and e.get("after") == after), None)
        if record is None or record["phase"] not in self.cfg.phases:
            return
        outputs = speculation.outputs(self.cfg.phases[record["phase"]], self.run.state["pipeline"], executor.ctx)
        before = set(record.get("dirty") or [])
        try:
            dirty = set(uncommitted_paths(self.worktree))
            paths = [path for path in outputs if path in dirty and path not in before]
            if paths:
                restore_paths(self.worktree, paths, "HEAD")
        except GitError:
            return

    def discard_left(self, walked: str, executor: Executor, keep_outputs: bool = False) -> dict:
        """As the walk of `walked` ends behind an approval that still waits or was rejected: put back, to `HEAD`, every
        uncommitted path that was not uncommitted when it started (its `speculation_started.dirty`), and name them
        (`{"discarded": [...]}`, for `speculation_ended`; `{}` when there were none). A walked phase can change only its
        `output`s (the fence), so the rest is what a step the fence stopped left: nothing written before the approval
        may reach the branch, as `after`'s refine commit (`git add -A`) or a later commit of `walked`. With
        `keep_outputs` (the walk stopped and its stop waits behind the approval) the phase's own `output`s stay for the
        stopped step; a rejection discards them too. A record from before `dirty` (or a tree git cannot read) keeps
        everything, as before."""
        record, _ = speculation.last_record(self.run.events.read(), walked)
        if record is None or not isinstance(record.get("dirty"), list) or walked not in self.cfg.phases:
            return {}
        keep = set(record["dirty"])
        if keep_outputs:
            keep |= set(speculation.outputs(self.cfg.phases[walked], self.run.state["pipeline"], executor.ctx))
        try:
            paths = [path for path in uncommitted_paths(self.worktree) if path not in keep]
            if paths:
                restore_paths(self.worktree, paths, "HEAD")
        except GitError:
            return {}
        return {"discarded": paths} if paths else {}

    def replan_after_approval(self, names: list[str], index: int, executor: Executor) -> bool:
        """Before `names[index]` walks, not behind an approval: re-plan it (`replan`) when it was walked behind the
        approval of the phase its last `speculation_started` names, that phase is approved now, and its basis moved
        since (a revision no speculation ran on, such as one a blocking finding was left in). True when it did. A phase
        never walked behind an approval reads nothing more than the log, and an approved one is never sent back."""
        name, run = names[index], self.run
        if (run.state["phases"].get(name) or {}).get("status") == "approved":
            return False
        events = run.events.read()
        record, _ = speculation.last_record(events, name)
        after = (record or {}).get("after")
        if after not in self.cfg.phases or (run.state["phases"].get(after) or {}).get("status") != "approved":
            return False
        basis = speculation.basis(self.cfg.phases[after], run.state["pipeline"], executor.ctx, self.worktree)
        if not speculation.replan_due(run.state, events, name, basis):
            return False
        self.replan(names, index, basis)
        return True

    def end_left_speculation(self) -> None:
        """End a speculation no runner walks any more (this one was stopped, or died, mid-walk) once the person decided
        on the approval it waited behind: `rejected` for a rejection (with what the walk left discarded, `discard_left`),
        else `approved`. Before anything else of the phase runs, so a re-raised approval never finds the old record and
        queues the walked phase's stops on it."""
        if self._speculating is not None:
            return
        state = self.run.state
        record = state.get("speculation")
        if not record or speculating(state) is not None:
            return
        if speculation.cut(state, record["after"]) == "rejected":
            self.run.emit("speculation_ended", phase=record["phase"], outcome="rejected",
                          **self.discard_left(record["phase"], self.executor()))
        else:
            self.run.emit("speculation_ended", phase=record["phase"], outcome="approved")

    def catch_up_handoffs(self, names: list[str], index: int, executor: Executor) -> None:
        """Write the handoff of each phase before `names[index]` a person approved while the runner walked on (an
        approval that came mid-speculation): before this phase settles, so its handoff comes first."""
        for i, name in enumerate(names[:index]):
            row = self.run.refresh()["phases"].get(name) or {}
            if row.get("status") == "approved" and not row.get("handoff"):
                self.hand_off(names, i, executor)

    def tell_speculative(self) -> None:
        """Tell a person, once, of the run's wait when it is a stop the walked phase queued quietly behind an approval
        that has since been given (`_tell_once` keeps `hitl_notified` on it), where its raise would have told one: not
        a stop the runner settles or parks first (`raise_hitl`'s own rule), which the resolver tells of if it cannot."""
        wait = self.run.refresh()["waiting"]
        if wait is None or not wait.get("speculative"):
            return
        kind, key = str(wait.get("kind")), str(wait.get("key"))
        if runner_settles(self.cfg, self.run, kind, key, wait.get("questions") or []) or parks_first(self.cfg, kind, key):
            return
        self.executor()._tell_once(wait)

    def settle_doubt(self, names: list[str], index: int, executor: Executor, lane: str) -> tuple[bool, str | None]:
        """A `needs_approval: true` phase with a reconcile step: open a reconcile round on its review's blocking
        findings while `doubt.stop_reason` allows one, then walk the phase again. `(True, outcome)` when it did (or
        a reconcile edit stopped the run), `(False, None)` when the approval is next."""
        run, name = self.run, names[index]
        phase = self.cfg.phases[name]
        for step in doubt.reconcile_steps(phase, lane):  # a `findings_reconciled` a crash lost, before anything reads it
            executor._reconciled(phase, f"{name}/{step.id}")
        if executor.conditions_changed(phase):  # the check after the reconcile step, when a crash skipped it
            return True, self.status()
        findings = phase_findings(self.cfg, run, name)
        finished = [key for key in doubt.flagged_keys(phase, lane)
                    if (run.state["steps"].get(key) or {}).get("status") in DONE_STEPS]
        tree = snapshot(self.worktree) if finished else ""
        for key in finished:  # every pass, `[]` for a clean review: what the pull request lists as left open
            run.emit("step_findings", key=key, findings=[f for f in findings if f["key"] == key], tree=tree)
        blocking = blocking_of(self.cfg, findings)
        events = run.events.read()
        stop = doubt.stop_reason(self.cfg, events, phase, lane) if blocking else None
        if blocking and stop is None:
            history = doubt.since_human_round(events, name)
            numbered = doubt.numbered(blocking, doubt.next_round(events, name))
            digests = {f"{name}/{step.id}": doubt.artifact_digest(self.worktree, fmt(str(step.output), executor.ctx))
                       for step in doubt.reconcile_steps(phase, lane)}
            open_refine(run, name, AUTO_BY, doubt.reconcile_note(numbered, history),
                        extra={"kind": doubt.RECONCILE, "keys": doubt.round_keys(phase, lane), "findings": numbered,
                               "digests": digests, "conditions": executor.phase_conditions(phase)})
            return True, self.phase(names, index)
        if blocking:
            run.emit("reconcile_stopped", phase=name, round=doubt.next_round(events, name) - 1, reason=stop,
                     open=len(blocking))
        fields = doubt.settled(self.cfg, run.events.read(), name, lane, stop or "clean")
        if fields is not None:
            run.emit("doubt_settled", phase=name, **fields)
        return False, None

    def hand_off(self, names: list[str], index: int, executor: Executor) -> None:
        """After `phase_approved`: write the handoff and compare the skill footer with the route; last, when
        `ci.early_pr_after` names this phase, open the draft pull request (`open_early_pr`, which never stops)."""
        name = names[index]
        path = write_handoff(self.cfg, self.run, name)
        footers = [e for e in self.run.events.read() if e["type"] == "skill_footer" and e.get("phase") == name]
        got = footers[-1]["next"] if footers else None
        expected = _next_skill(self.cfg, names, index, executor.ctx)
        if got and expected and got.lstrip("/") != expected.lstrip("/"):
            self.run.emit("footer_mismatch", phase=name, expected=expected, got=got)
        self.run.emit("handoff_written", phase=name, path=str(path))
        if self.cfg.ci.get("early_pr_after") == name:
            executor.open_early_pr(name)


def retried(events: list[dict], phase: str) -> bool:
    """Whether a human retried a step of `phase` (`agentflow retry`) since the phase was last approved: an approved
    phase is then walked once more and settled again, so the step's run is real evidence and a phase that needs a
    human asks again. Event order decides it, never a step row: a retried key the walk does not visit (a task that
    already committed) would otherwise keep the phase reopening. The new `phase_approved` ends it."""
    prefix = f"{phase}/"
    retry = max((e["seq"] for e in events if e.get("type") == "step_retry" and str(e.get("key") or "").startswith(prefix)),
                default=0)
    approved = max((e["seq"] for e in events if e.get("type") == "phase_approved" and e.get("phase") == phase), default=0)
    return retry > approved


def refining(state: dict, phase: str) -> bool:
    """Whether `phase` is in a refine round. Read from `state["refine"]` and the phase row, never from the
    run's status, which a queued wait promoted after the rejection can overwrite."""
    refine = state.get("refine") or {}
    return refine.get("phase") == phase or (state["phases"].get(phase) or {}).get("status") == "refining"


def _first_skill(cfg: Config, phase: Phase, steps: list[Step], ctx: Ctx) -> str | None:
    for step in for_lane(steps, ctx.values["lane"]):
        if step.reconciles:  # it runs only in a reconcile round, never as a phase's first step
            continue
        when = evaluate(cfg, step.when, ctx).value if step.when is not None else True
        unless = evaluate(cfg, step.unless, ctx).value if step.unless is not None else False
        if when is False or unless is True:
            continue
        if step.skill:
            return step.skill
        found = _first_skill(cfg, phase, step.steps, ctx)
        if found:
            return found
    return None


def _next_skill(cfg: Config, names: list[str], index: int, ctx: Ctx) -> str | None:
    """The skill of the first step that would run in the next phase that is not skipped."""
    for name in names[index + 1 :]:
        phase = cfg.phases[name]
        if phase.skip_if is not None and evaluate(cfg, phase.skip_if, ctx).value is True:
            continue
        return _first_skill(cfg, phase, phase.steps, ctx)
    return None


# --------------------------------------------------------------------------- handoff and approval text


def _phase_steps(run: Run, phase: str) -> dict[str, dict]:
    return {key: row for key, row in run.state["steps"].items() if row.get("phase") == phase}


def _one_line(text: str) -> str:
    return " ".join(str(text).split())


def _walked_paths(run: Run, phase: str) -> list[str]:
    """`speculation.walked_paths` of the run's log and worktree."""
    worktree = run.state.get("worktree")
    return speculation.walked_paths(run.events.read(), phase, Path(worktree)) if worktree else []


def _artifacts(run: Run, phase: str) -> list[str]:
    """What the phase changed since it started, without what a phase walked behind its approval committed
    (`_walked_paths`). Once approved, such a phase lists what changed up to the commit its approval was asked on."""
    row = run.state["phases"].get(phase) or {}
    sha = row.get("start_sha")
    worktree = run.state.get("worktree")
    if not sha or not worktree:
        return []
    hidden = _walked_paths(run, phase)
    walked = any(e["type"] == "speculation_started" and e.get("after") == phase for e in run.events.read())
    try:
        if walked and row.get("status") == "approved" and row.get("approval_sha"):
            paths = changed_between(Path(worktree), sha, row["approval_sha"])
        else:
            paths = changed_since(Path(worktree), sha)
    except GitError:
        return []
    return [path for path in paths if path not in hidden]


def _decisions(run: Run, phase: str) -> list[str]:
    lines = []
    for key, row in _phase_steps(run, phase).items():
        if row.get("status") in DONE_STEPS and row.get("result"):
            lines.append(f"- `{key}` ({row['status']}): {_one_line(row['result'])}")
    prefix = f"{phase}/"
    for event in run.events.read():
        key = str(event.get("key") or "")
        if not key.startswith(prefix):
            continue
        if event["type"] in ("hitl_answered", "checkpoint_cleared"):
            for qid, text in (event.get("answers") or {}).items():
                lines.append(f"- `{key}` {qid}: {_one_line(text)}")
        elif event["type"] == "condition_decided":
            word = "true" if event.get("value") else "false"
            lines.append(f"- `{key}`: `{event.get('condition')}` taken as {word} (by {event.get('decided_by')})")
    return lines


def _skipped(run: Run, phase: str) -> list[str]:
    return [f"- `{key}`: {row.get('skip_reason') or 'skipped'}"
            for key, row in _phase_steps(run, phase).items() if row.get("status") == "skipped"]


def _gates(run: Run, phase: str) -> list[str]:
    lines = []
    for key in _phase_steps(run, phase):
        last: dict[str, dict] = {}
        for entry in run.state["gates"].get(key, []):
            last[str(entry.get("gate"))] = entry
        for gate, entry in last.items():
            mark = "– skipped" if entry.get("skipped") else "✓ passed" if entry.get("passed") else "✗ failed"
            lines.append(f"- `{key}` {gate}: {mark}")
    return lines


def _risks(cfg: Config, run: Run, phase: str) -> list[str]:
    lines = []
    for key in _phase_steps(run, phase):
        result = read_json(run.result_path(key))
        risks = result.get("risks") if isinstance(result, dict) else None
        for risk in risks if isinstance(risks, list) else []:
            lines.append(f"- `{key}`: {_one_line(risk if isinstance(risk, str) else str(risk))}")
    for event in run.events.read():  # a review wave's members' risks, which no result file of theirs holds
        key = str(event.get("key") or "")
        if event["type"] == "review_result" and event.get("wave") is not None and key.split("/")[0] == phase:
            lines += [f"- `{key}`: {_one_line(risk)}" for risk in event.get("risks") or [] if isinstance(risk, str)]
    if phase in cfg.phases:  # the doubt review's open findings, low findings and trade-offs, so `build` sees them
        lines += [f"- {_one_line(risk)}" for risk in doubt.handoff_risks(cfg, run, phase, run.state["pipeline"])]
    return lines


def _section(title: str, lines: list[str], level: str = "###") -> str:
    return f"{level} {title}\n\n" + ("\n".join(lines) if lines else "(none)")


def _handoff_text(cfg: Config, run: Run, phase: str) -> str:
    parts = [
        f"## {phase}",
        _section("Artifacts", [f"- `{p}`" for p in _artifacts(run, phase)]),
        _section("Decisions", _decisions(run, phase)),
        _section("Skipped steps", _skipped(run, phase)),
        _section("Gates", _gates(run, phase)),
        _section("Risks", _risks(cfg, run, phase)),
    ]
    return "\n\n".join(parts)


def write_handoff(cfg: Config, run: Run, phase: str) -> Path:
    """Write the phase's handoff and rebuild `handoff.md` from every approved phase, newest first."""
    run.refresh()
    folder = run.dir / "handoffs"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{phase}.md").write_text(_handoff_text(cfg, run, phase) + "\n", encoding="utf-8")
    order = [e["phase"] for e in run.events.read() if e["type"] == "phase_approved"]
    if phase not in order:
        order.append(phase)
    sections = []
    for name in reversed(list(dict.fromkeys(order))):
        file = folder / f"{name}.md"
        if file.exists():
            sections.append(file.read_text(encoding="utf-8").strip())
    head_text = (f"# Handoff for run {run.id}\n\nWhat each approved phase produced and decided, newest first. "
                 "Read the artifacts by path.")
    path = run.handoff_path()
    path.write_text("\n\n".join([head_text, *sections]) + "\n", encoding="utf-8")
    return path


def _diff_lines(run: Run, phase: str) -> list[str]:
    return _diff_since(run, (run.state["phases"].get(phase) or {}).get("start_sha"), _walked_paths(run, phase))


def _diff_since(run: Run, sha: str | None, exclude: list[str] | None = None) -> list[str]:
    """The commits and the diff stat since `sha`, without the `exclude` paths (and the commits that touch only them)."""
    worktree = run.state.get("worktree")
    if not sha or not worktree:
        return []
    try:
        stat = diff_stat(Path(worktree), sha, exclude or ())
        log = subprocess.run(["git", "log", "--oneline", f"{sha}..HEAD", *excluding(exclude or ())], cwd=worktree,
                             capture_output=True, text=True, encoding="utf-8", check=False).stdout.strip()
    except GitError:
        return []
    lines = [f"Since `{sha[:12]}`:"]
    if log:
        lines += ["", "Commits:", "", *[f"- {line}" for line in log.splitlines()]]
    lines += ["", "```", stat or "(no changes to tracked files)", "```"]
    return lines


def _step_results(run: Run, phase: str) -> list[str]:
    return [f"- `{key}`: {row.get('status')}" + (f" — {_one_line(row['result'])}" if row.get("result") else "")
            for key, row in _phase_steps(run, phase).items()]


def _round_lines(run: Run, phase: str) -> list[str]:
    """What each step did in the phase's latest refine round: `changed`, `unchanged` (with the agent's
    reason), `skipped` (with the condition's) or `failed`, from the events since the human's rejection (a reconcile
    round the runner opened is part of what followed it, not a round of its own)."""
    events = run.events.read()
    rejected = max((e["seq"] for e in events if e["type"] == "phase_rejected" and e.get("phase") == phase
                    and e.get("kind") != doubt.RECONCILE), default=0)
    last: dict[str, tuple[str, str]] = {}
    for event in events:
        key = str(event.get("key") or "")
        if event["seq"] <= rejected or not key.startswith(f"{phase}/"):
            continue
        if event["type"] == "step_finished":
            word = "changed" if event.get("status", "done") == "done" else str(event.get("status"))
            last[key] = (word, str(event.get("summary") or ""))
        elif event["type"] == "step_unchanged":
            last[key] = ("unchanged", str(event.get("reason") or ""))
        elif event["type"] == "step_skipped":
            last[key] = ("skipped", str(event.get("reason") or ""))
    return [f"- {key}: {word}" + (f" — {_one_line(why)}" if why.strip() else "") for key, (word, why) in last.items()]


def _review_findings(cfg: Config, run: Run, phase: str) -> list[str]:
    """Per-task reviews (their last round), then the phase's own `blocks_on_findings` steps, or, in a phase whose
    doubt review ran, that review's lines: each finding with its round, disposition and status."""
    lines = []
    for key, review in run.state["reviews"].items():
        if key.startswith(f"{phase}/"):
            lines += [f"`{key}`: {finding_line(f)[2:]}" for f in review.get("findings") or [] if isinstance(f, dict)]
    doubted = doubt.page_lines(cfg, run, phase, run.state["pipeline"]) if phase in cfg.phases else []
    if doubted:
        lead = [doubted[0], *(line for line in doubted[1:] if line in doubt.STOP_LINES.values())]
        rows = [line for line in doubted[1:] if line not in doubt.STOP_LINES.values()]
        return [*lead, "", *(f"- {line}" for line in [*lines, *rows])]
    lines += [f"`{f['key']}`: {finding_line(f)[2:]}" for f in phase_findings(cfg, run, phase)]
    blocking = blocking_of(cfg, phase_findings(cfg, run, phase))
    lead = [f"**{len(blocking)} blocking finding(s) are still open.** Approving ships them.", ""] if blocking else []
    return [*lead, *(f"- {line}" for line in lines)]


def _triage_section(cfg: Config, run: Run, phase: str) -> str:
    """The runner's `triage.md` (`flow_control._render_triage`) when the phase has a triage step that ran: the text
    of the file, so the approval page of `define` shows the upgrade path itself."""
    spec = cfg.phases.get(phase)
    if spec is None or not any(writes_triage(step) for step in spec.steps):
        return ""
    try:
        text = safeio.read_bytes(run.dir / "triage.md", TRIAGE_PAGE_LIMIT)
    except (OSError, ValueError):
        return ""
    if not text:
        return ""
    return _section("Triage", [line for line in text.decode("utf-8", errors="replace").splitlines()[1:] if line.strip()], "##")


def approval_text(cfg: Config, run: Run, phase: str) -> str:
    """The inbox page for `<phase>_approved`, built from `phase_approval.show`."""
    round_ = (run.state["phases"].get(phase) or {}).get("round", 0)
    lead = [
        f"# {run.id}: {phase}_approved",
        f"Phase `{phase}` is done" + (f" (refine round {round_})" if round_ else "") + ". Review it, then:",
        f"- `agentflow approve {run.id} {phase}` starts the next phase\n"
        f"- `agentflow reject {run.id} {phase} --note \"...\"` runs every step of `{phase}` again with your note",
    ]
    builders = {
        "handoff": lambda: _section("Handoff (written when you approve)", [
            "Artifacts:", *([f"- `{p}`" for p in _artifacts(run, phase)] or ["(none)"]),
            "", "Decisions:", *(_decisions(run, phase) or ["(none)"]),
            "", "Risks:", *(_risks(cfg, run, phase) or ["(none)"]),
        ], "##"),
        "diff": lambda: _section("Diff", _diff_lines(run, phase), "##"),
        "step_results": lambda: _section("Step results", _step_results(run, phase), "##"),
        "skipped_steps": lambda: _section("Skipped steps", _skipped(run, phase), "##"),
        "gates": lambda: _section("Gates", _gates(run, phase), "##"),
        "review_findings": lambda: _section("Review findings", _review_findings(cfg, run, phase), "##"),
    }
    shown = [builders[item]() for item in cfg.phase_approval.get("show", DEFAULT_SHOW) if item in builders]
    triage = _triage_section(cfg, run, phase)
    if triage:
        shown.insert(0, triage)  # what a human is approving first: the upgrade path, with its bump size
    lead += _approval_note(cfg, run, phase)
    names = list(cfg.phases)
    nxt = (speculation.due(cfg, run, names, names.index(phase), (run.state.get("options") or {}).get("to_phase"))
           if phase in names else None)
    if nxt is not None:  # the runner walks it behind this approval (`_Loop.speculate`)
        lead.append(speculation.page_line(phase, nxt))
    previous = (run.state["phases"].get(phase) or {}).get("approval_sha")
    if round_ and previous:  # after a human's refine round: what changed since they last looked, step by step
        shown = [
            _section("Since the last approval request", _diff_since(run, previous, _walked_paths(run, phase)), "##"),
            _section(f"Steps in refine round {round_}", _round_lines(run, phase), "##"),
            *shown,
        ]
    return "\n\n".join([*lead, *shown]) + "\n"


def _approval_note(cfg: Config, run: Run, phase: str) -> list[str]:
    """The phase's `approval_note`, when its condition holds or is unknown: an offer the human takes up
    themselves, because the runner never runs a user-only skill."""
    note = cfg.phases[phase].approval_note
    if not note:
        return []
    worktree = Path(run.state.get("worktree") or run.repo)
    if evaluate(cfg, note["when"], _ctx(cfg, run, worktree)).value is False:
        return []
    return [f"`{condition_label(note['when'])}` holds, so before you approve, consider running "
            f"`/{note['human_skill']}` yourself. The runner never runs it."]


def raise_phase_approval(cfg: Config, run: Run, phase: str) -> Path:
    """Stop the run at `<phase>_approved`: write the inbox page, record the event, notify."""
    run.refresh()
    round_ = (run.state["phases"].get(phase) or {}).get("round", 0)
    path = run.inbox_path(f"{phase}_approved")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(approval_text(cfg, run, phase), encoding="utf-8")
    summary = f"Phase {phase} is done: approve it to start the next phase, or reject it with a note."
    run.emit("phase_approval_raised", phase=phase, round=round_, sha=head(Path(run.state["worktree"])),
             summary=summary, inbox=str(path))
    notify_run(cfg, run, summary)
    return path


# --------------------------------------------------------------------------- dry run


def dry_run(cfg: Config, repo: Path, pipeline: str, params: dict, opts: RunOptions, out: TextIO | None = None) -> None:
    """Print each phase's agent command lines and gate commands, and the approvals between phases.

    Nothing is created or called: no run directory, no worktree, no agent. Conditions are read from
    the main checkout as it is now.
    """
    out = out or sys.stdout
    names = _check_range(cfg, pipeline, opts)
    repo = Path(repo).resolve()
    ctx = make_ctx(cfg, repo, pipeline, params)
    ctx.base_ref = f"origin/{cfg.project.base_branch}"
    run = Run(repo, f"{datetime.now():%Y%m%d-%H%M}-{pipeline}-{ctx.values['slug']}")
    executor = Executor(cfg, run, make_agents(cfg), ctx, opts.model_overrides, dry_run=True, out=out)
    first = opts.from_phase or names[0]
    print(f"dry run: {pipeline} {params} (no run, no worktree, no agent calls)", file=out)
    for index in range(names.index(first), len(names)):
        name = names[index]
        phase: Phase = cfg.phases[name]
        note = ""
        if not for_lane(phase.steps, pipeline):
            print(f"{name}: – skip (no steps for lane {pipeline})", file=out)
            continue
        if phase.skip_if is not None:
            result = evaluate(cfg, phase.skip_if, ctx)
            if result.value is True:
                print(f"{name}: – skip ({result.reason})", file=out)
                continue
            if result.value is None:
                note = f" (? {condition_label(phase.skip_if)} is unknown: a real run decides it then)"
        print(f"{name}:{note}", file=out)
        ran = executor.run_steps(phase, phase.steps, name)[1]
        if not ran:
            print("  (all steps skipped)", file=out)
        elif needs_approval(phase):
            print(f"⏸ {name}_approved", file=out)
        if ran and cfg.ci.get("early_pr_after") == name:
            print("  ⇪ early draft PR: git push origin <branch>; gh pr create --draft", file=out)
        if name == opts.to_phase:
            break
