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
then going to a human after all. Stops inside a phase go to the resolver first (`autonomy.Resolver`). After `phase_approved`
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
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TextIO

from agentflow.agents import make_agents
from agentflow.autonomy import (
    AUTO_BY,
    Resolver,
    blocking_of,
    finding_line,
    finding_rounds,
    findings_note,
    phase_findings,
)
from agentflow import pack, run_env, work_source
from agentflow.commit_checks import runner_head
from agentflow.conditions import Ctx, evaluate, make_ctx
from agentflow.config_trust import layers_sha256, start_options
from agentflow.config import (
    RETRO_PIPELINE, Config, ConfigError, Phase, Step, check_lane, for_lane, layer_files, load_config, validate,
    worktrees_root,
)
from agentflow.flow_control import condition_label, halted, read_json
from agentflow.gitops import (
    GitError,
    add_worktree,
    branch_exists,
    changed_since,
    commit,
    diff_stat,
    head,
)
from agentflow.hitl import notify_run, open_refine
from agentflow.intake import gather, save
from agentflow.issue_status import mark, release
from agentflow.lanes import Executor, Outcome, footer_next
from agentflow.state import TERMINAL, Run

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

    params, inputs = gather(params, repo, cfg.issues.epic_labels)
    run = Run.create(cfg, repo, pipeline, params)
    try:
        _set_up(cfg, repo, run, pipeline, params, inputs, opts)
    except Exception as exc:  # the run exists now: it must never be left `running` with nothing behind it
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
    `resume` goes on from) before the lock is released and the exception goes on to the caller."""
    if run.refresh().get("pipeline") == RETRO_PIPELINE:  # it walks no phase: resuming one would run the whole workflow
        raise ValueError(f"{run.id} is a retro run, which cannot be resumed; cancel it and run `agentflow retro` again")
    lock = run.lock()
    if not lock.acquire():
        raise RunLocked(f"another runner is working on {run.id}; see `agentflow status {run.id}`")
    try:
        return _Loop(run, cfg, _effective(run, opts)).go()
    except Exception as exc:
        run.emit("runner_error", error=str(exc) or repr(exc), exception=type(exc).__name__)
        raise
    finally:
        lock.release()
        release(run)  # after the lock, and it never raises, so it cannot hide the exception on its way out


class _Loop:
    def __init__(self, run: Run, cfg: Config, opts: RunOptions) -> None:
        self.run = run
        self.cfg = cfg
        self.opts = opts
        self.agents = make_agents(cfg)
        self.config_sha = self._config_sha()

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
        if errors:
            self.run.emit("config_reload_skipped", sha256=sha, reason="; ".join(errors)[:2000])
            return
        self.cfg = cfg
        self.agents = make_agents(cfg)
        self.run.emit("config_reloaded", sha256=sha)

    def status(self) -> str:
        self.run.refresh()
        return self.run.state["status"]

    def executor(self) -> Executor:
        """A fresh executor per phase, so the context (the plan path, ...) reflects the tree now."""
        return Executor(self.cfg, self.run, self.agents, _ctx(self.cfg, self.run, self.worktree), self.opts.model_overrides)

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
        if state["status"] == "ready":  # a human chose to go on: `--to` no longer ends the run here
            self.opts.to_phase = None
        if state["status"] in ("paused", "error", "ready"):  # a human resumed, or the last runner stopped on an error
            run.emit("run_resumed")
        self.record_env()
        names = list(self.cfg.phases)
        first = self.opts.from_phase or names[0]
        for index in range(names.index(first), len(names)):
            name = names[index]
            outcome = self.phase(names, index)
            if outcome is not None:
                return outcome
            if name == self.opts.to_phase and not run.state["marked_done"]:
                return self.finish()
        return self.finish()

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
        waiting = state["waiting"]
        if waiting is not None and waiting.get("kind") == "phase_approval":
            return "awaiting_approval"
        row = state["phases"].get(name) or {}
        executor = self.executor()
        if refining(state, name):
            if self.walk(phase, executor)[0] == "stopped":
                return self.status()
            return self.settle(names, index, executor)
        if row.get("status") == "approved":
            if not row.get("handoff"):
                self.hand_off(names, index, executor)
            return None
        if row.get("status") == "skipped":
            return None
        marked = state["marked_done"]
        if marked and index < len(names) - 1 and row.get("status") in (None, "pending"):
            run.emit("phase_skipped", phase=name, reason=f"marked done after {marked['phase']}")
            return None
        if row.get("status") in (None, "pending"):
            if phase.skip_if is not None:
                skip, reason = executor.decide(phase, phase.skip_if, f"{name}/skip_if")
                if skip is None:
                    return self.status()
                if skip:
                    run.emit("phase_skipped", phase=name, reason=reason)
                    return None
            run.emit("phase_started", phase=name, start_sha=head(self.worktree))
        outcome, ran = self.walk(phase, executor)
        if outcome == "stopped":
            return self.status()
        if not ran:
            run.emit("phase_skipped", phase=name, reason="all steps skipped")
            return None
        return self.settle(names, index, executor)

    def walk(self, phase: Phase, executor: Executor) -> tuple[Outcome, bool]:
        """Run the phase's steps, letting the resolver settle what it can; `(outcome, any step ran)`."""
        resolver = Resolver(self.cfg, self.run, executor)
        ran = False
        while True:
            if not resolver.finish_open():
                return "stopped", ran
            outcome, stepped = executor.run_steps(phase, phase.steps, phase.name)
            ran = ran or stepped
            if outcome != "stopped" or not resolver.take_over():
                return outcome, ran

    def settle(self, names: list[str], index: int, executor: Executor) -> str | None:
        """The phase's steps are finished: wait for its human, or go on by itself once nothing blocks it.

        Blocking findings get a refine round the runner opens itself, up to `autonomy.finding_rounds`.
        After those, the findings go to a human on the phase's approval page."""
        name = names[index]
        phase = self.cfg.phases[name]
        if needs_approval(phase):
            raise_phase_approval(self.cfg, self.run, name)
            return "awaiting_approval"
        findings = phase_findings(self.cfg, self.run, name)
        for key in dict.fromkeys(f["key"] for f in findings):  # for the pull request: this round's, kept per step
            self.run.emit("step_findings", key=key, findings=[f for f in findings if f["key"] == key])
        blocking = blocking_of(self.cfg, findings)
        if not blocking:
            self.run.emit("phase_approved", by=AUTO_BY, phase=name, sha=head(self.worktree))
            self.hand_off(names, index, executor)
            return None
        if finding_rounds(self.run, name) < int(self.cfg.autonomy.get("finding_rounds") or 0):
            open_refine(self.run, name, AUTO_BY, findings_note(blocking))
            return self.phase(names, index)
        raise_phase_approval(self.cfg, self.run, name)
        return "awaiting_approval"

    def hand_off(self, names: list[str], index: int, executor: Executor) -> None:
        """After `phase_approved`: write the handoff and compare the skill footer with the route."""
        name = names[index]
        path = write_handoff(self.run, name)
        footers = [e for e in self.run.events.read() if e["type"] == "skill_footer" and e.get("phase") == name]
        got = footers[-1]["next"] if footers else None
        expected = _next_skill(self.cfg, names, index, executor.ctx)
        if got and expected and got.lstrip("/") != expected.lstrip("/"):
            self.run.emit("footer_mismatch", phase=name, expected=expected, got=got)
        self.run.emit("handoff_written", phase=name, path=str(path))


def refining(state: dict, phase: str) -> bool:
    """Whether `phase` is in a refine round. Read from `state["refine"]` and the phase row, never from the
    run's status, which a queued wait promoted after the rejection can overwrite."""
    refine = state.get("refine") or {}
    return refine.get("phase") == phase or (state["phases"].get(phase) or {}).get("status") == "refining"


def _first_skill(cfg: Config, phase: Phase, steps: list[Step], ctx: Ctx) -> str | None:
    for step in for_lane(steps, ctx.values["lane"]):
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


def _artifacts(run: Run, phase: str) -> list[str]:
    sha = (run.state["phases"].get(phase) or {}).get("start_sha")
    worktree = run.state.get("worktree")
    if not sha or not worktree:
        return []
    try:
        return changed_since(Path(worktree), sha)
    except GitError:
        return []


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


def _risks(run: Run, phase: str) -> list[str]:
    lines = []
    for key in _phase_steps(run, phase):
        result = read_json(run.result_path(key))
        risks = result.get("risks") if isinstance(result, dict) else None
        for risk in risks if isinstance(risks, list) else []:
            lines.append(f"- `{key}`: {_one_line(risk if isinstance(risk, str) else str(risk))}")
    return lines


def _section(title: str, lines: list[str], level: str = "###") -> str:
    return f"{level} {title}\n\n" + ("\n".join(lines) if lines else "(none)")


def _handoff_text(run: Run, phase: str) -> str:
    parts = [
        f"## {phase}",
        _section("Artifacts", [f"- `{p}`" for p in _artifacts(run, phase)]),
        _section("Decisions", _decisions(run, phase)),
        _section("Skipped steps", _skipped(run, phase)),
        _section("Gates", _gates(run, phase)),
        _section("Risks", _risks(run, phase)),
    ]
    return "\n\n".join(parts)


def write_handoff(run: Run, phase: str) -> Path:
    """Write the phase's handoff and rebuild `handoff.md` from every approved phase, newest first."""
    run.refresh()
    folder = run.dir / "handoffs"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{phase}.md").write_text(_handoff_text(run, phase) + "\n", encoding="utf-8")
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
    return _diff_since(run, (run.state["phases"].get(phase) or {}).get("start_sha"))


def _diff_since(run: Run, sha: str | None) -> list[str]:
    worktree = run.state.get("worktree")
    if not sha or not worktree:
        return []
    try:
        stat = diff_stat(Path(worktree), sha)
        log = subprocess.run(["git", "log", "--oneline", f"{sha}..HEAD"], cwd=worktree, capture_output=True,
                             text=True, encoding="utf-8", check=False).stdout.strip()
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
    reason), `skipped` (with the condition's) or `failed`, from the events since the rejection."""
    events = run.events.read()
    rejected = max((e["seq"] for e in events if e["type"] == "phase_rejected" and e.get("phase") == phase), default=0)
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
    """Per-task reviews (their last round), then the phase's own `blocks_on_findings` steps."""
    lines = []
    for key, review in run.state["reviews"].items():
        if key.startswith(f"{phase}/"):
            lines += [f"`{key}`: {finding_line(f)[2:]}" for f in review.get("findings") or [] if isinstance(f, dict)]
    lines += [f"`{f['key']}`: {finding_line(f)[2:]}" for f in phase_findings(cfg, run, phase)]
    blocking = blocking_of(cfg, phase_findings(cfg, run, phase))
    lead = [f"**{len(blocking)} blocking finding(s) are still open.** Approving ships them.", ""] if blocking else []
    return [*lead, *(f"- {line}" for line in lines)]


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
            "", "Risks:", *(_risks(run, phase) or ["(none)"]),
        ], "##"),
        "diff": lambda: _section("Diff", _diff_lines(run, phase), "##"),
        "step_results": lambda: _section("Step results", _step_results(run, phase), "##"),
        "skipped_steps": lambda: _section("Skipped steps", _skipped(run, phase), "##"),
        "gates": lambda: _section("Gates", _gates(run, phase), "##"),
        "review_findings": lambda: _section("Review findings", _review_findings(cfg, run, phase), "##"),
    }
    shown = [builders[item]() for item in cfg.phase_approval.get("show", DEFAULT_SHOW) if item in builders]
    lead += _approval_note(cfg, run, phase)
    if round_:  # after a refine round: what changed since the human last looked, step by step
        previous = (run.state["phases"].get(phase) or {}).get("approval_sha")
        shown = [
            _section("Since the last approval request", _diff_since(run, previous), "##"),
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
        if name == opts.to_phase:
            break
