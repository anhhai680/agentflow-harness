"""The agent step lifecycle: run one config step through an agent until it is done or needs a human.

`Executor.run_agent_step` goes round one loop per agent call, in this order: budget check, then
model and effort (from state; `effort.default` for a new step), prompt, tree snapshot, the call
(`agent_started` ... `agent_exited`, spend, one `permission_denied` per denial), guards, the
`result.json`, gates and doc checks (`checks:`, which always have the fix loop) with the fix loop,
rollover; and once the result is `done` and the gates pass: the progress check, the commit
(`commit: true`) and `step_finished`. Anything the runner cannot settle stops the run for a human
(`raise_hitl`) and returns "stopped". Everything needed to go on later is in the event log, so a
new `Executor` resumes the step's recorded session.

The agent's only extra directory is the run's workspace (`workspace.py`): before each call the runner
refreshes the input copies there, and after it reads the result and `progress.md` from there only. The
runner commits only on top of the last `HEAD` it recorded (`commit_checks.runner_head`).
"""

from __future__ import annotations

import dataclasses
import re
import shlex
import sys
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, TextIO

from agentflow import overlays, pack, workspace
from agentflow.agents import Agent, AgentCall, AgentOutcome, Interruption
from agentflow.autonomy import AUTO_BY, eligible
from agentflow.bug_eval import BugEvalLane, exhausted_kind
from agentflow.ci_lane import CILane
from agentflow.commit_checks import RUNNER_COMMITS, runner_head
from agentflow.conditions import Ctx, fmt
from agentflow.config import Config, Phase, Step, resolve_model
from agentflow.feature_lane import FeatureLane
from agentflow.flow_control import (
    StepControl,
    current_refine,
    halted,
    refine_note_block,
    refine_subject,
    writes_questions,
)
from agentflow.gates import GateResult, gates_ok, run_doc_checks, run_gates
from agentflow.gitops import CommitRejected, GitError, NothingToCommit, changed_since, commit, commits_between, head
from agentflow.guards import Violation, changed_paths, check_step, path_matches, snapshot
from agentflow.hitl import ResultError, StepResult, notify_run, park_hitl, raise_hitl, read_result
from agentflow.run_control import wait_for_agents
from agentflow.sync_lane import SyncLane
from agentflow.state import TERMINAL, Run, workspaces_root

Outcome = Literal["done", "skipped", "stopped"]
Mode = Literal["first", "refine"]

HOLD_POLL_S = 30.0  # short sleeps against the wall clock: a machine asleep must not stretch a wait
HOLD_SLACK_S = 60.0  # past `resetsAt`: clock drift must not spend the first call after a limit
BACKOFF_START_S = 60.0
BACKOFF_MAX_S = 900.0

PREAMBLE = "_preamble.md"
RESULT = "_result.md"
DEFAULT_TEMPLATE = "step.md"
DEFAULT_REFINE = "refine.md"
DEFAULT_ROLE = "build"
EFFORT_BLOCK = re.compile(r"<!-- effort-check -->\n(.*?)<!-- /effort-check -->\n?", re.DOTALL)
SKILL_LINE = re.compile(r"\b([Uu])se the `([\w.:-]+)` skill")
RESUMABLE = ("running", "waiting_human")
UNSTARTED_STEP = (None, "pending")
FOOTER = re.compile(r"^\s*Next:\s*`?([\w.:/-]+)`?\s+(?:—|–|--?)\s", re.MULTILINE)
HANDOFF_LINE = "Read the handoff from the earlier phases first: `{handoff}`."


def footer_next(text: str) -> str | None:
    """The skill named by the last `Next: <skill> — <reason>` footer line in `text`, if any."""
    found = FOOTER.findall(text or "")
    return found[-1] if found else None


# --------------------------------------------------------------------------- prompts


def _template(cfg: Config, name: str) -> str:
    file = pack.prompt_path(cfg.path, name)
    if file is None:
        raise FileNotFoundError(f"prompt '{name}' is not in the pack's prompts/ or the engine's")
    return file.read_text(encoding="utf-8")


def _values(cfg: Config, step: Step, ctx: Ctx, extra: dict) -> dict[str, str]:
    """Placeholder values for one step: the run's context, the step's paths, then `extra`.

    The paths are absolute and inside the run's agent workspace (`workspace.py`), the copies the runner
    refreshes before every call: the agent works in another directory and may write nowhere else."""
    values = dict(ctx.values)
    values["step_id"] = step.id
    run = ctx.run
    if run is not None:
        values["run"] = run.id
        agent_dir = run.agent_dir
    else:
        agent_dir = workspaces_root(ctx.repo) / values.get("run", "run")
    values["run_dir"] = str(agent_dir)
    values["progress"] = str(agent_dir / "progress.md")
    values["handoff"] = str(agent_dir / "handoff.md")
    values["questions"] = str(agent_dir / "questions.md")
    values.update({name: str(value) for name, value in extra.items()})
    return values


def _refine_values(ctx: Ctx, rec: dict) -> dict[str, str]:
    refine = (ctx.run.state.get("refine") if ctx.run is not None else None) or {}
    return {
        "refine_round": str(refine.get("round", "")),
        "refine_note": str(refine.get("note", "")),
        "previous_summary": str(rec.get("result") or "(no summary recorded)"),
        "refine_artifacts": _phase_artifacts(ctx, rec.get("phase")),
    }


def _phase_artifacts(ctx: Ctx, phase: str | None) -> str:
    """The files the phase has changed so far (committed or not), one `- path` line each."""
    run = ctx.run
    row = (run.state["phases"].get(phase) or {}) if run is not None and phase else {}
    sha = row.get("start_sha")
    if not sha:
        return "(not recorded)"
    try:
        paths = changed_since(Path(run.state.get("worktree") or ctx.repo), sha)
    except GitError as exc:
        return f"(could not list them: {exc})"
    return "\n".join(f"- `{path}`" for path in paths) or "(none yet)"


def _read_file_skills(text: str, skills_dir: str) -> str:
    """For agents that cannot load skills by name: point them at the skill file in `skills_dir` instead."""

    def replace(match: re.Match[str]) -> str:
        read = "Read" if match.group(1) == "U" else "read"
        return f"{read} `{skills_dir}/{match.group(2)}/SKILL.md` and follow it"

    return SKILL_LINE.sub(replace, text)


def build_prompt(cfg: Config, step: Step, ctx: Ctx, rec: dict, extra: dict, mode: Mode) -> str:
    """`_preamble.md` + the step's template (after the refine template in a refine round) + `_result.md`.

    The preamble's effort check is kept only while `rec["sized"]` is false. Placeholders are
    filled in one pass over the joined text, so values that hold braces go in verbatim.
    """
    preamble = EFFORT_BLOCK.sub("" if rec.get("sized") else r"\1", _template(cfg, PREAMBLE))
    body = _template(cfg, step.prompt or DEFAULT_TEMPLATE).strip()
    if step.skill and f"`{step.skill}` skill" not in body:
        body = f"Use the `{step.skill}` skill.\n\n{body}"
    extra_skills = overlays.skill_lines(cfg, ctx, str(rec.get("role") or step.role or DEFAULT_ROLE), body)
    if extra_skills:
        body = f"{body}\n\n{extra_skills}"
    if extra.get("gate_evidence") and "{gate_evidence}" not in body:  # a `gates_first` template that has no slot
        body = f"{body}\n\n{{gate_evidence}}"
    values = _values(cfg, step, ctx, {})
    values["brief"] = "\n\n".join((ctx.repo / name).read_text(encoding="utf-8") for name in step.brief)
    values["skill"] = step.skill or ""
    values["base_ref"] = ctx.base_ref
    values["earlier_findings"] = ""
    values["gate_evidence"] = ""
    if ctx.run is not None and ctx.run.handoff_path().exists():
        body = f"{HANDOFF_LINE}\n\n{body}"
    if mode == "refine":
        refine = _template(cfg, cfg.phase_approval.get("refine_prompt") or DEFAULT_REFINE).strip()
        body = f"{refine}\n\nThe step as it was first given:\n\n{body}"
    if mode == "refine" or (ctx.run is not None and ctx.run.state.get("refine")):  # a step can first run in a refine round
        values.update(_refine_values(ctx, rec))
    values.update({name: str(value) for name, value in extra.items()})
    text = "\n\n".join(part.strip() for part in (preamble, body, _template(cfg, RESULT)))
    agent = cfg.agents.get(rec.get("agent") or cfg.defaults.agent)
    if agent is not None and agent.skill_loading == "read_file":
        text = _read_file_skills(text, cfg.project.skills_dir)
    return fmt(text, values) + "\n"


# --------------------------------------------------------------------------- executor state


@dataclass
class _Turn:
    """The next agent call: its prompt, the session it resumes (None = fresh), and whether it sizes the step."""

    prompt: str
    resume: str | None
    sizing: bool = False


@dataclass
class _Called:
    outcome: AgentOutcome
    sizing: bool
    before: str
    after: str


@dataclass
class _StepRun:
    """One `run_agent_step` invocation's working state."""

    phase: Phase
    step: Step
    key: str
    mode: Mode
    role: str
    agent_name: str
    agent: Agent
    model: str
    effort: str
    extra: dict
    session: str | None
    agent_profile: str | None = None
    lead: str = ""  # put before every fresh prompt until the gates run again (a gate step's first fix)
    start_head: str = ""
    start_tree: str = ""
    resume_tree: str | None = None  # the tree when a human's approval resumed the session
    progress_mtime: float | None = None
    fixes: int = 0
    rollovers: int = 0
    reminded: bool = False
    holds: int = 0  # since the last result; these three reset once the agent can work again
    agent_failures: int = 0
    down_since: float | None = None


def _mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except FileNotFoundError:
        return None


def _count(events: list[dict], etype: str, key: str) -> int:
    return sum(1 for e in events if e["type"] == etype and e.get("key") == key)


def _answers(events: list[dict], key: str) -> dict[str, str]:
    """The human's answers for `key` given since its last agent call, if any."""
    found: dict[str, str] = {}
    for event in events:
        if event["type"] == "agent_started" and event.get("step") == key:
            found = {}
        elif event["type"] == "hitl_answered" and event.get("key") == key:
            found = dict(event.get("answers") or {})
    return found


REOPENING = ("hitl_approved", "step_reopened")
DENIED_SHOWN = 15  # commands listed in a blocked inbox
DENIED_TEXT_MAX = 200


def _approved(events: list[dict], key: str) -> dict | None:
    """Since the last agent call on `key`: the latest approval of a runner stop on it, or the runner
    reopening it (`step_reopened`, with the note for the session), if any."""
    found = None
    for event in events:
        if event["type"] == "agent_started" and event.get("step") == key:
            found = None
        elif event["type"] in REOPENING and event.get("key") == key:
            found = event
    return found


def _after_approval(events: list[dict], key: str) -> list[dict]:
    """The events after the step was last approved or reopened: limits like fix attempts restart there."""
    for index in range(len(events) - 1, -1, -1):
        if events[index]["type"] in REOPENING and events[index].get("key") == key:
            return events[index + 1 :]
    return events


def _violation_text(violations: list[Violation]) -> str:
    return "; ".join(f"{v.kind}: {v.detail}" for v in violations)


# --------------------------------------------------------------------------- executor


class Executor(BugEvalLane, StepControl, FeatureLane, SyncLane, CILane):
    """Runs a run's steps: `run_steps`/`run_step` (control flow, in `flow_control`), plan tasks and
    `finish` (in `feature_lane`), test expectations and fix-looping gate steps (in `bug_eval`) and the
    agent step lifecycle here. `dry_run` prints each agent's command line and calls nothing."""

    def __init__(
        self,
        cfg: Config,
        run: Run,
        agents: dict[str, Agent],
        ctx: Ctx,
        overrides: dict[str, str],
        dry_run: bool = False,
        out: TextIO = sys.stdout,
    ) -> None:
        self.cfg = cfg
        self.run = run
        self.agents = agents
        if ctx.run is None:
            ctx = dataclasses.replace(ctx, run=run, values={**ctx.values, "run": run.id})
        self.ctx = ctx
        self.overrides = overrides
        self.dry_run = dry_run
        self.out = out

    @property
    def worktree(self) -> Path:
        return Path(self.run.state.get("worktree") or self.ctx.repo)

    # -- the lifecycle

    def run_agent_step(
        self,
        phase: Phase,
        step: Step,
        key: str,
        extra: dict[str, str] | None = None,
        mode: Mode = "first",
        role: str | None = None,
        share_session_of: str | None = None,
        fix_first: list[GateResult] | None = None,
    ) -> Outcome:
        """Run `step` as `key` until it is done ("done"), or a human is needed ("stopped").

        A step already done is not run again; a step skipped by its condition, or any step in a
        dry run, is "skipped". A step whose record is `running` or `waiting_human` (the runner was
        stopped, or a human answered) resumes its recorded session at its recorded effort.
        `share_session_of` names a step whose session a new step continues (at that step's effort,
        with no effort check), as the tasks of a small plan do. A step parked by a question that later
        tasks need not wait for returns "stopped" with its record `parked`.
        `fix_first` (gates that already failed, as for a gate step) starts a step that is not resuming
        in its fix loop: the first call is fix attempt 1, with the gate output.
        """
        extra = self._step_extra(key, extra)
        self.run.refresh()
        state = self.run.state
        rec = state["steps"].get(key) or {}
        status = rec.get("status")
        if status == "skipped":
            return "skipped"
        if status in ("done", "unchanged"):
            return "done"
        if state["waiting"] is not None or halted(state["status"]):
            return "stopped"

        agent_name = phase.agent or self.cfg.defaults.agent
        agent = self.agents.get(agent_name)
        if agent is None:
            raise ValueError(f"agent {agent_name!r} is not enabled; phase {phase.name!r} needs it")
        role = role or step.role or DEFAULT_ROLE
        since = self._since_step_started(key)
        started = since[0] if since else None
        resuming = status in RESUMABLE and started is not None
        session = rec.get("session_id")
        reuses_session = bool(session) and (resuming or mode == "refine")
        if reuses_session and started is not None and started.get("model"):
            model = started["model"]
            source = started.get("model_source") or "state"
        else:
            model, source = resolve_model(self.cfg, phase, step, role, self.overrides)
        sr = _StepRun(
            phase=phase, step=step, key=key, mode=mode, role=role, agent_name=agent_name, agent=agent,
            model=model, effort=rec.get("effort") or self.cfg.effort.default, extra=extra, session=session,
            agent_profile=overlays.agent_profile(self.cfg, step),
        )

        shared = (state["steps"].get(share_session_of) or {}) if share_session_of else {}
        if resuming and session:
            turn = _Turn(self._continue_prompt(sr, _answers(since, key), _approved(since, key)), session)
        elif mode == "refine" and session:
            turn = _Turn(self._prompt(sr, sized=True), session)
        elif not session and status in UNSTARTED_STEP and shared.get("session_id"):
            sr.session = shared["session_id"]
            sr.effort = shared.get("effort") or sr.effort
            turn = _Turn(self._prompt(sr, sized=True), sr.session)
        else:
            sized = bool(rec.get("sized"))
            turn = _Turn(self._prompt(sr, sized=sized), None, sizing=not sized)
        fixing = bool(fix_first) and not resuming
        if fixing:  # a gate step's session starts from the gate output (and a refine round's note)
            refine = current_refine(self.run, phase.name)
            note = f"\n\n{refine_note_block(refine)}" if refine is not None else ""
            sr.lead = f"{self._fix_lead(sr, fix_first or [], 1)}{note}"
            turn = dataclasses.replace(turn, prompt=f"{sr.lead}\n\n{turn.prompt}")

        if self.dry_run:
            call = self._agent_call(sr, turn, attempt=rec.get("attempts", 0) + 1)
            print(shlex.join(agent.argv(call)), file=self.out)
            return "skipped"

        if resuming and started is not None:
            committed = [e for e in since if e["type"] == "step_committed" and e.get("key") == key]
            if committed:
                last = committed[-1]
                self.run.emit("step_finished", key=key, status="done", summary=last.get("summary", ""), sha=last["sha"])
                return "done"
            wait_for_agents(self.run, self.cfg, key, self._timeout_s())
            baseline = self._baseline(key, since, started)
            if isinstance(baseline, list):
                return self._fail(sr, baseline)
            sr.start_head, sr.start_tree = baseline
            if (_approved(since, key) or {}).get("type") == "hitl_approved":
                sr.resume_tree = snapshot(self.worktree)
            fresh = _after_approval(since, key)
            sr.fixes = _count(fresh, "fix_attempt", key)
            sr.rollovers = _count(fresh, "rolled_over", key)
        else:
            sr.start_head = head(self.worktree)
            sr.start_tree = snapshot(self.worktree)
            if "failed" in (status, rec.get("retry_of")):  # a restart must not adopt the earlier violation
                known = runner_head(self.run)
                if known and sr.start_head != known:  # e.g. the commit an agent made: never the new baseline
                    return self._fail(sr, [Violation("agent_commit", f"HEAD is {sr.start_head[:12]}, not the "
                                                     f"{known[:12]} the runner last recorded: reset it first")])
                dirty = self._uncommitted_forbidden(sr.start_tree)
                if dirty:
                    return self._fail(sr, [Violation("forbidden_path", p) for p in dirty])
            refine = state.get("refine") or {}
            round_ = refine.get("round") if mode == "refine" and refine.get("phase") == phase.name else None
            self.run.emit(
                "step_started", key=key, phase=phase.name, agent=agent_name, model=model, model_source=source,
                effort=sr.effort, mode=mode, round=round_, start_head=sr.start_head, start_tree=sr.start_tree,
                task=self._task_payload(key),
            )
            if fixing:
                sr.fixes = 1
                self.run.emit("fix_attempt", key=key, attempt=1, gates=[r.name for r in fix_first or []])
        sr.progress_mtime = _mtime(self.run.progress_path())
        return self._loop(sr, turn)

    def dry_agent_argv(self, phase: Phase, step: Step, key: str, extra: dict | None = None,
                       role: str | None = None) -> list[str]:
        """The command line a fresh first call of `step` would get, built without touching the run."""
        agent_name = phase.agent or self.cfg.defaults.agent
        agent = self.agents.get(agent_name)
        if agent is None:
            raise ValueError(f"agent {agent_name!r} is not enabled; phase {phase.name!r} needs it")
        role = role or step.role or DEFAULT_ROLE
        model, _source = resolve_model(self.cfg, phase, step, role, self.overrides)
        sr = _StepRun(
            phase=phase, step=step, key=key, mode="first", role=role, agent_name=agent_name, agent=agent,
            model=model, effort=self.cfg.effort.default, extra=self._step_extra(key, extra), session=None,
            agent_profile=overlays.agent_profile(self.cfg, step),
        )
        turn = _Turn(self._prompt(sr, sized=False), None, sizing=True)
        return agent.argv(self._agent_call(sr, turn, attempt=1))

    def _loop(self, sr: _StepRun, turn: _Turn) -> Outcome:
        while True:
            budget = self.run.state.get("budget_usd") or self.cfg.defaults.run_budget_usd
            spend = float(self.run.state.get("spend_usd") or 0.0)
            if budget and spend >= budget:
                return self._stop(sr, "budget_exceeded", f"The run has spent ${spend:.2f} of its ${budget:.2f} budget.")

            called = self._call(sr, turn)
            if self.run.refresh()["status"] in TERMINAL:
                return "stopped"  # cancelled while the agent ran: nothing more is called or recorded
            outcome = called.outcome
            violations = check_step(self.cfg, self.worktree, sr.start_head, sr.start_tree, called.after,
                                    outcome.tool_calls, sr.step.forbid)
            if violations:
                return self._fail(sr, violations)
            if outcome.resume_failed:
                if sr.mode == "refine" and turn.resume:
                    turn = self._fresh_refine(sr, turn.resume)
                    continue
                return self._stop(sr, "blocked", f"Could not resume agent session {turn.resume}.")
            if outcome.timed_out:
                limit = self.cfg.defaults.step_timeout_min
                return self._stop(sr, "step_timeout", f"The agent call ran past the step timeout ({limit} min).")

            result, error = self._read_result(sr)
            if result is None:
                if outcome.rolled_over:
                    next_turn = self._rollover(sr, outcome)
                elif outcome.interruption is not None:
                    next_turn = self._hold(sr, turn, outcome.interruption)
                elif not sr.reminded:
                    sr.reminded = True
                    self.run.emit("result_reminded", key=sr.key, error=error)
                    next_turn = _Turn(self._reminder_prompt(sr, error), sr.session)
                else:
                    self._stop(sr, "blocked", f"No valid result.json after a reminder: {error}")
                    next_turn = None
                if next_turn is None:
                    return "stopped"
                turn = next_turn
                continue
            sr.reminded = False
            sr.holds, sr.agent_failures, sr.down_since = 0, 0, None

            if result.status == "effort_change":
                turn = self._effort_change(sr, called, result)
                continue
            if result.status == "needs_human" and not writes_questions(sr.step):  # a grill's questions are its output
                questions = [asdict(q) for q in result.questions]
                if (result.can_continue_without and self.may_park(sr.key)
                        and not eligible(self.cfg, "needs_human", sr.key, questions)):
                    held = changed_paths(self.worktree, sr.start_tree, called.after)
                    park_hitl(self.cfg, self.run, "needs_human", sr.key, result.summary, result.questions, held)
                    return "stopped"
                raise_hitl(self.cfg, self.run, "needs_human", sr.key, result.summary, result.questions)
                return "stopped"
            if result.status == "blocked":
                kind = "blocked_repeat" if called.after == sr.resume_tree else "blocked"
                return self._stop(sr, kind, result.summary or "The agent reported the step as blocked.",
                                  extra=self._denied_report(sr.key))
            if result.status == "unchanged":
                edited = changed_paths(self.worktree, sr.start_tree, called.after)
                if edited:  # never leave edits uncommitted and ungated under an `unchanged` label
                    shown = ", ".join(edited[:5]) + (f" and {len(edited) - 5} more" if len(edited) > 5 else "")
                    return self._stop(sr, "blocked", f"The agent answered `unchanged` but changed {shown}. "
                                      "Approve to resume the step: it must revert those edits or finish as `done`.")
                self.run.emit("step_unchanged", key=sr.key, reason=result.summary)
                return "done"

            failed = self._failed_gates(sr)
            sr.lead = ""  # the gate output it carried is stale now; fix prompts carry the new output
            expected = self.check_expect(sr, result, gates_failed=bool(failed))
            if expected == "stopped":
                return "stopped"
            failed = [*failed, *expected]
            if failed:
                if (sr.step.fix_loop or sr.step.checks) and sr.fixes < self.cfg.defaults.fix_attempts:
                    sr.fixes += 1
                    self.run.emit("fix_attempt", key=sr.key, attempt=sr.fixes, gates=[r.name for r in failed])
                    turn = _Turn(self._fix_prompt(sr, failed), sr.session)
                    continue
                names = ", ".join(r.name for r in failed)
                return self._stop(
                    sr, exhausted_kind(sr.step),
                    f"Gates still failing after {sr.fixes} fix attempt(s): {names}.", extra=self._gate_report(failed),
                )
            return self._finish(sr, result)

    # -- one call

    def _timeout_s(self) -> int:
        return max(1, round(self.cfg.defaults.step_timeout_min * 60))

    def _agent_call(self, sr: _StepRun, turn: _Turn, attempt: int) -> AgentCall:
        return AgentCall(
            prompt=turn.prompt,
            model=sr.model,
            effort=sr.effort,
            cwd=self.worktree,
            run_id=self.run.id,
            step_key=sr.key,
            result_path=self.run.agent_result_path(sr.key),
            transcript_path=self.run.transcript_path(sr.key, attempt),
            add_dirs=[self.run.agent_dir],  # the workspace only: never the runs root (events, state, inbox)
            runs_root=self.run.dir.parent,  # denied to the agent's edits, wherever the pack puts the state dir
            timeout_s=self._timeout_s(),
            resume_session=turn.resume,
            agent_profile=sr.agent_profile,
            context_window=self.context_window_for(sr.key, sr.model),
            env=self.cfg.defaults.env,
            forbidden_paths=[*self.cfg.guardrails.forbidden_paths, *sr.step.forbid],
        )

    def context_window_for(self, key: str, model: str) -> int | None:
        """The window the run has seen for this step's model: by the stream's model id when the
        step's record names one, else by the configured alias (`sonnet`); None when unseen."""
        windows = self.run.state["context_windows"]
        recorded = (self.run.state["steps"].get(key) or {}).get("model")
        for name in (recorded, model):
            if name and windows.get(name):
                return int(windows[name])
        return None

    def _call(self, sr: _StepRun, turn: _Turn) -> _Called:
        workspace.prepare(self.run)  # fresh copies of the inputs; whatever the agent left there is replaced
        workspace.clear_result(self.run, sr.key)  # a stale result must never count
        before = snapshot(self.worktree)
        attempt = (self.run.state["steps"].get(sr.key) or {}).get("attempts", 0) + 1
        call = self._agent_call(sr, turn, attempt)
        seen: dict[str, object] = {"model": None, "pid": None}

        def on_event(name: str, payload: dict) -> None:
            data = dict(payload)
            if name == "agent_started":
                seen["pid"] = data.get("pid")
                data.update(sizing=turn.sizing, phase=sr.phase.name, role=sr.role)
            elif name == "agent_session":
                seen["model"] = data.get("model")
                data.update(key=sr.key, pid=seen["pid"])
            elif name == "agent_exited":
                data["key"] = sr.key
            self.run.emit(name, **data)

        outcome = sr.agent.run(call, on_event)
        sr.session = outcome.session_id or turn.resume or sr.session
        window = outcome.context_window
        if window:  # cached under the stream's model id and the alias, so the next call finds it either way
            for name in dict.fromkeys((str(seen["model"] or sr.model), sr.model)):
                if self.run.state["context_windows"].get(name) != window:
                    self.run.emit("context_window", model=name, tokens=window)
        footer = footer_next(outcome.final_text)
        if footer:
            self.run.emit("skill_footer", key=sr.key, phase=sr.phase.name, next=footer)
        for denial in outcome.permission_denials:
            self.run.emit(
                "permission_denied", key=sr.key,
                tool=denial.get("tool_name") or denial.get("tool"),
                input=denial.get("tool_input", denial.get("input")),
            )
        return _Called(outcome, turn.sizing, before, snapshot(self.worktree))

    def _read_result(self, sr: _StepRun) -> tuple[StepResult | None, str]:
        """The agent's result, read from the workspace into the run directory's copy and checked there."""
        why = workspace.take_result(self.run, sr.key)
        if why:
            return None, why
        try:
            return read_result(self.run.result_path(sr.key), refine=sr.mode == "refine"), ""
        except ResultError as exc:
            return None, str(exc)

    # -- outcomes

    def _effort_change(self, sr: _StepRun, called: _Called, result: StepResult) -> _Turn:
        """D11: accept only from the sizing session, before any work, and within `effort.allowed`."""
        requested = str(result.effort)
        allowed = self.cfg.effort.allowed
        why = ""
        if not called.sizing:
            why = "the step is already sized, and a resumed session keeps its level"
        elif called.before != called.after:
            why = "work has already started (the step's diff is not empty)"
        elif requested not in allowed:
            why = f"'{requested}' is not one of the allowed levels ({', '.join(allowed)})"
        elif requested == sr.effort:
            why = f"the step already runs at {requested}"
        if why:
            self.run.emit("effort_refused", key=sr.key, requested=requested, reason=why, agent_reason=result.reason)
            note = f"Your effort change to `{requested}` was refused: {why}. Carry on with the step at `{sr.effort}`."
            return _Turn(f"{note}\n\n{self._result_partial(sr)}", sr.session)
        self.run.emit("effort_changed", key=sr.key, session_id=called.outcome.session_id,
                      **{"from": sr.effort, "to": requested, "reason": result.reason})
        sr.effort = requested
        sr.session = None
        return _Turn(self._prompt(sr, sized=True), None)

    def _hold(self, sr: _StepRun, turn: _Turn, why: Interruption) -> _Turn | None:
        """The agent cannot work for now: wait, then return the next turn, or None when the run stopped.

        A usage limit waits for its reset, with no cap, since waiting is free. An outage backs off until
        `recovery.give_up_h`, and a call that reached the model and died counts against
        `recovery.failed_attempts`, since those cost money. A call no model answered is sent again as it was."""
        now = time.time()
        if why.kind == "usage_limit":
            reason = "usage limit"
            until = max(float(why.until or 0.0) + HOLD_SLACK_S, now + BACKOFF_START_S)
        else:
            policy = self.cfg.recovery
            sr.agent_failures += why.reached_model
            sr.down_since = sr.down_since or now
            if sr.agent_failures >= policy.failed_attempts or now - sr.down_since >= policy.give_up_h * 3600:
                self._stop(sr, "agent_unavailable",
                           f"The agent could not finish a call: {sr.agent_failures} died after reaching the model, "
                           f"{(now - sr.down_since) / 60:.0f} min since the first failure. Check the transcript.")
                return None
            reason = "agent died" if why.reached_model else "outage"
            until = now + min(BACKOFF_START_S * 2 ** sr.holds, BACKOFF_MAX_S)
        self.run.emit("run_held", key=sr.key, reason=reason, limit=why.limit,
                      until=datetime.fromtimestamp(until, UTC).isoformat(timespec="seconds"))
        if not sr.holds:
            notify_run(self.cfg, self.run, f"{sr.key} is held ({reason}); it carries on by itself.")
        sr.holds += 1
        waited = self._wait_until(until)
        self.run.emit("run_hold_ended", key=sr.key)
        if not waited:
            return None
        return _Turn(self._continue_prompt(sr, {}), sr.session) if why.reached_model else turn

    def _wait_until(self, until: float) -> bool:
        """Sleep until `until` (epoch seconds); False as soon as a human paused or ended the run."""
        while time.time() < until:
            if halted(self.run.refresh()["status"]):
                return False
            time.sleep(min(HOLD_POLL_S, max(0.0, until - time.time())))
        return not halted(self.run.refresh()["status"])

    def _rollover(self, sr: _StepRun, outcome: AgentOutcome) -> _Turn | None:
        """The session crossed `rollover_pct`: continue in a fresh one, or stop past `max_rollovers`."""
        self.run.emit("rolled_over", key=sr.key, session_id=outcome.session_id, context_pct=outcome.context_pct)
        sr.rollovers += 1
        limit = self.cfg.context.max_rollovers
        if sr.rollovers > limit:
            self._stop(sr, "max_rollovers",
                       f"The step ran out of context {sr.rollovers} times (limit {limit}); split it into smaller steps.")
            return None
        sr.session = None
        values = self._values(sr)
        lead = (
            f"This is a fresh session: the previous one for this step ran out of context. "
            f"Continue the step from `{values['progress']}` and `{values['handoff']}`: read them first, "
            "check the work tree, and do not redo finished work."
        )
        return _Turn(f"{lead}\n\n{self._prompt(sr, sized=True)}", None)

    def _fresh_refine(self, sr: _StepRun, gone: str) -> _Turn:
        """A refine round could not resume the step's session (expired, rolled over): a fresh session gets
        the same prompt plus `progress.md` and `handoff.md`, at the same effort and with no effort check."""
        self.run.emit("resume_failed", key=sr.key, session_id=gone, effort=sr.effort)
        sr.session = None
        values = self._values(sr)
        lead = (
            f"This is a fresh session: the runner could not resume this step's earlier session. "
            f"Read `{values['progress']}` and `{values['handoff']}` first: they say what the step did before. "
            "Then check the work tree and refine the step's existing output; do not start over."
        )
        return _Turn(f"{lead}\n\n{self._prompt(sr, sized=True)}", None)

    def _failed_gates(self, sr: _StepRun) -> list[GateResult]:
        """The step's gates, then its doc checks (`checks:`, on the diff since the step started); the failed ones.

        A `gates_first` step whose gates passed before the session, on the tree it still has, does not run
        them again: those results are the evidence it was given."""
        results: list[GateResult] = []
        if sr.step.gates and self._prerun_still_holds(sr):
            self.run.emit("gates_reused", key=sr.key)
        elif sr.step.gates:
            results = run_gates(self.cfg, sr.step.gates, self.worktree, self.run.gate_log_dir(sr.key))
        if sr.step.checks:
            docs = self.cfg.project.docs
            results += run_doc_checks(sr.step.checks, self.worktree, sr.start_head, docs["idea"], docs["decisions"])
        for r in results:
            self.run.emit("gate_result", key=sr.key, gate=r.name, passed=r.passed, skipped=r.skipped,
                          reason=r.reason, log=str(r.log_path) if r.log_path else None,
                          duration_s=r.duration_s)
        return [] if gates_ok(results) else [r for r in results if not (r.passed or r.skipped)]

    def _prerun_still_holds(self, sr: _StepRun) -> bool:
        if not sr.step.gates_first:
            return False
        pre = [e for e in self.run.events.read() if e["type"] == "gates_prerun" and e.get("key") == sr.key]
        # the tree is what makes it safe: a pass on exactly this tree is a pass, whenever it ran
        return bool(pre) and bool(pre[-1].get("passed")) and pre[-1].get("tree") == snapshot(self.worktree)

    def _finish(self, sr: _StepRun, result: StepResult) -> Outcome:
        if _mtime(self.run.progress_path()) in (None, sr.progress_mtime):
            self.run.emit("progress_not_updated", key=sr.key)
        sha = None
        tree = snapshot(self.worktree) if sr.step.gates else None  # what the gates passed on, for later checks
        if sr.step.commit is True:
            subject = fmt(sr.step.message or f"Step {sr.key}", self._values(sr))
            refine = current_refine(self.run, sr.phase.name)
            parts = [subject, result.summary.strip()]
            if refine is not None:  # refined work goes on top as a new commit, with the note
                parts = [refine_subject(sr.step.id, subject), result.summary.strip(), refine_note_block(refine)]
            message = "\n\n".join(part for part in parts if part)
            try:
                sha = commit(self.worktree, message, self.cfg, expected_head=runner_head(self.run))
            except CommitRejected as exc:
                return self._fail(sr, [Violation(exc.kind, str(exc))])
            except NothingToCommit:
                sha = None
            if sha:
                self.run.emit("step_committed", key=sr.key, sha=sha, summary=result.summary)
        extra = {"tree": tree} if tree else {}
        self.run.emit("step_finished", key=sr.key, status="done", summary=result.summary, sha=sha, **extra)
        return "done"

    def _stop(self, sr: _StepRun, kind: str, summary: str, extra: str = "") -> Outcome:
        raise_hitl(self.cfg, self.run, kind, sr.key, summary, extra=extra)
        return "stopped"

    def _fail(self, sr: _StepRun, violations: list[Violation]) -> Outcome:
        """A guard tripped: the step fails with no retry, and a human looks at the tree."""
        summary = f"Guard violation in {sr.key}: {_violation_text(violations)}"
        self.run.emit("guard_tripped", key=sr.key, violations=[{"kind": v.kind, "detail": v.detail} for v in violations])
        raise_hitl(self.cfg, self.run, "guard_violation", sr.key, summary)
        self.run.emit("step_finished", key=sr.key, status="failed", summary=summary)
        return "stopped"

    # -- prompt pieces

    def _rec(self, sr: _StepRun, sized: bool) -> dict:
        return {**(self.run.state["steps"].get(sr.key) or {}), "sized": sized, "agent": sr.agent_name, "role": sr.role}

    def _step_extra(self, key: str, extra: dict | None) -> dict:
        """`extra` plus the result path, written out because a headless agent cannot read its environment."""
        return {**(extra or {}), "result_file": str(self.run.agent_result_path(key)), "phase": key.split("/")[0]}

    def _values(self, sr: _StepRun) -> dict[str, str]:
        return _values(self.cfg, sr.step, self.ctx, sr.extra)

    def _prompt(self, sr: _StepRun, sized: bool) -> str:
        prompt = build_prompt(self.cfg, sr.step, self.ctx, self._rec(sr, sized), sr.extra, sr.mode)
        return f"{sr.lead}\n\n{prompt}" if sr.lead else prompt

    def _result_partial(self, sr: _StepRun) -> str:
        return fmt(_template(self.cfg, RESULT), self._values(sr)).strip() + "\n"

    def _continue_prompt(self, sr: _StepRun, answers: dict[str, str], approved: dict | None = None) -> str:
        if answers:
            lines = "\n".join(f"{qid}: {text}" for qid, text in answers.items())
            who = "The runner took the option you recommended for each question" if self._answered_by_runner(sr.key) \
                else "A human answered your questions"
            lead = f"{who}:\n\n{lines}\n\nCarry on with the step using these answers."
        elif approved is not None and approved["type"] == "step_reopened":
            lead = str(approved.get("note") or "The runner reopened this step: carry on with it.")
        elif approved is not None:
            progress = self._values(sr)["progress"]
            note = f"Their note: {approved['note']}\n\n" if approved.get("note") else ""
            lead = (
                f"The runner stopped this step ({approved.get('kind')}: {approved.get('summary') or 'no summary'}). "
                f"A human looked at it and approved going on. {note}Carry on where you left off: "
                f"check the work tree and `{progress}`, then finish the step. If a command was denied earlier, "
                "try it once more, because the rules may have changed. If it is denied again, stop as `blocked` "
                "and name the exact command."
            )
        else:
            progress = self._values(sr)["progress"]
            lead = (
                "The runner stopped while this step was running. Carry on where you left off: "
                f"check the work tree and `{progress}`, then finish the step."
            )
        return f"{lead}\n\n{self._result_partial(sr)}"

    def _answered_by_runner(self, key: str) -> bool:
        answered = [e for e in self.run.events.read() if e["type"] == "hitl_answered" and e.get("key") == key]
        return bool(answered) and answered[-1].get("by") == AUTO_BY

    def _reminder_prompt(self, sr: _StepRun, error: str) -> str:
        return (
            f"The runner found no valid result.json for this step ({error}). "
            "If the step is finished, write the result file now; otherwise finish the step first.\n\n"
            f"{self._result_partial(sr)}"
        )

    def stop_report(self, key: str) -> str:
        """What the resolver is told beyond the stop's summary: the inbox page a human would have read."""
        path = self.run.inbox_path(key)
        return path.read_text(encoding="utf-8") if path.is_file() else "(no report)"

    def _denied_report(self, key: str) -> str:
        """The inbox section naming what was denied since the last approval, each once, so one fix covers it."""
        seen: dict[str, None] = {}
        for event in _after_approval(self.run.events.read(), key):
            if event["type"] == "permission_denied" and event.get("key") == key:
                tool_input = event.get("input") if isinstance(event.get("input"), dict) else {}
                target = tool_input.get("command") or tool_input.get("file_path") or ""
                seen[f"{event.get('tool')}: {str(target)[:DENIED_TEXT_MAX]}"] = None
        if not seen:
            return ""
        shown = list(seen)[:DENIED_SHOWN]
        more = f"\n- and {len(seen) - len(shown)} more" if len(seen) > len(shown) else ""
        lines = "\n".join(f"- `{line}`" for line in shown)
        rules = self.worktree / ".claude" / "settings.local.json"
        return (f"### Denied in this step\n\n{lines}{more}\n\nTo allow one in this run, add a narrow rule to `{rules}`. "
                "The session reads the worktree's settings, and a change to `.claude/settings.json` there is a guard "
                "violation. To keep it for later runs, add it to `.claude/settings.json` on the main checkout.")

    def _gate_report(self, failed: list[GateResult]) -> str:
        blocks = []
        for r in failed:
            log = f"Full log: {r.log_path}\n" if r.log_path else ""
            blocks.append(f"### {r.name} ({r.reason})\n{log}```\n{r.tail}\n```")
        return "\n\n".join(blocks)

    def _fix_lead(self, sr: _StepRun, failed: list[GateResult], attempt: int) -> str:
        lead = (
            f"The gates failed (fix attempt {attempt} of {self.cfg.defaults.fix_attempts}). "
            "Fix the cause in this session: never weaken a check, skip a test or add a suppression. "
            "Run the failing gates yourself, then write the result file again."
        )
        return f"{lead}\n\n{self._gate_report(failed)}"

    def _fix_prompt(self, sr: _StepRun, failed: list[GateResult]) -> str:
        return f"{self._fix_lead(sr, failed, sr.fixes)}\n\n{self._result_partial(sr)}"

    def _uncommitted_forbidden(self, tree: str) -> list[str]:
        """Forbidden paths that differ between `HEAD` and `tree` (changes nobody has committed)."""
        base = head(self.worktree) + "^{tree}"
        patterns = self.cfg.guardrails.forbidden_paths
        return [p for p in changed_paths(self.worktree, base, tree) if any(path_matches(p, g) for g in patterns)]

    # -- event log

    def _baseline(self, key: str, since: list[dict], started: dict) -> tuple[str, str] | list[Violation]:
        """`(start_head, start_tree)` the guards compare a resumed step against, or the violations that
        make the step fail before any call.

        The recorded start, unless the runner committed other work since (another task while this one
        was parked, a recorded decision): then `HEAD` and the tree as they are now (`step_rebased`), so
        the runner's own commits never count as the agent moving `HEAD`. A rebase is refused when a
        commit since the start is not one the runner recorded (an agent committed), or when the tree
        holds an uncommitted change to a forbidden path: adopting either would hide it from the guards.
        """
        head_sha = started.get("start_head") or head(self.worktree)
        tree = started.get("start_tree") or snapshot(self.worktree)
        rebased = 0
        for event in since:
            if event["type"] == "step_rebased" and event.get("key") == key:
                head_sha, tree, rebased = event["start_head"], event["start_tree"], event["seq"]
        others = [e for e in since if e["type"] == "step_committed" and e.get("key") != key and e.get("sha")
                  and e["seq"] > rebased]
        if not others:
            return head_sha, tree
        ours = {e["sha"] for e in self.run.events.read()
                if e["type"] in RUNNER_COMMITS and e.get("sha")}
        try:
            moved = commits_between(self.worktree, head_sha)
        except GitError as exc:
            return [Violation("agent_commit", f"cannot tell what moved HEAD since {head_sha[:12]}: {exc}")]
        foreign = [sha for sha in moved if sha not in ours]
        if foreign:
            shown = ", ".join(sha[:12] for sha in foreign)
            return [Violation("agent_commit", f"HEAD moved by commit(s) the runner did not make: {shown}")]
        now_tree = snapshot(self.worktree)
        dirty = self._uncommitted_forbidden(now_tree)
        if dirty:
            return [Violation("forbidden_path", path) for path in dirty]
        now_head = head(self.worktree)
        self.run.emit("step_rebased", key=key, start_head=now_head, start_tree=now_tree,
                      reason=f"the runner committed {len(others)} other change(s) since the step started")
        return now_head, now_tree

    def _since_step_started(self, key: str) -> list[dict]:
        """Events from the step's last `step_started` on (empty when it never started)."""
        events = self.run.events.read()
        for index in range(len(events) - 1, -1, -1):
            if events[index]["type"] == "step_started" and events[index].get("key") == key:
                return events[index:]
        return []
