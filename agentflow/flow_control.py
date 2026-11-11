"""Step control flow: which step of a phase runs next, and what each kind of step does.

`StepControl` is mixed into `lanes.Executor`, which owns the agent step lifecycle (`run_agent_step`).
`run_steps` walks a step list in order; `run_step` settles one step:

- a step already done (or skipped) is not looked at again, so a resumed run walks quickly to where it
  stopped; `when`/`unless` are decided only before a step starts (a refine round decides them again,
  except that a step which ran is not skipped by a condition on the file it wrote itself: `condition_kept`);
- a condition that comes out unknown follows its `on_unknown`: `ask_human` raises a `condition` stop
  with one yes/no question, `agent` asks one `mechanical`-role agent call first (`true`/`false` were
  already applied by `evaluate`);
- `type: group` runs its steps (with `repeat`, once per round under `r<round>/`, and a `max_rounds`
  stop when `until` never holds), `next:` jumps forward to a later step, `type: checkpoint` stops for a human,
  gate steps run their gates (with `fix_loop` or `on_fail: block`, `bug_eval` runs them), `type: commit`
  commits; `foreach: plan_task` and `type: finish` are the feature lane's (`feature_lane`).
- a step whose `lanes:` tag leaves out this run's lane is not part of the run, so nothing records it;
- a group or foreach in which nothing ran (every child skipped, every plan task ticked) is
  recorded as skipped (`all steps skipped`), so its phase is skipped rather than approved.

A dry run walks the same steps and prints what would run (`_dry_steps`), touching nothing.
"""

from __future__ import annotations

import json
import re
import shlex
import sys
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, TextIO

from agentflow import sources
from agentflow.conditions import Ctx, evaluate, fmt, paths_read
from agentflow.commit_checks import runner_head
from agentflow.config import Config, Phase, Step, for_lane
from agentflow.gates import GateResult, gates_ok, record_results, resolve, run_gates_reusing, summarize
from agentflow.guards import snapshot
from agentflow.gitops import CommitRejected, NothingToCommit, commit
from agentflow.hitl import (
    Option,
    Question,
    ResultError,
    parse_answers,
    question_objects,
    raise_hitl,
    read_result,
    render_inbox,
    write_answers,
)
from agentflow.policy import applicable
from agentflow.state import TERMINAL

if TYPE_CHECKING:
    from agentflow.state import Run

Outcome = Literal["done", "skipped", "stopped"]
DONE_STATUSES = ("done", "unchanged")
PRERUN_SKIP = (*DONE_STATUSES, "skipped", "running", "waiting_human")  # resumed or finished: no fresh evidence
EVIDENCE_TAIL = 30  # lines of each gate's output a `gates_first` session is shown


def gate_evidence(results: list[GateResult], tree: str) -> str:
    """What the runner's gates said on `tree`, for a `gates_first` step's prompt."""
    lines = [f"The runner ran these gates on tree `{tree[:12]}` just before this session:", ""]
    for r in results:
        mark = "skipped" if r.skipped else "passed" if r.passed else f"FAILED ({r.reason})"
        log = f", log `{r.log_path}`" if r.log_path else ""
        lines.append(f"- `{r.name}`: {mark}{log}")
    for r in results:
        tail = "\n".join(r.tail.splitlines()[-EVIDENCE_TAIL:]).strip()
        if tail and not r.skipped:
            lines += ["", f"Last lines of `{r.name}`:", "```", tail, "```"]
    return "\n".join(lines)
UNSTARTED = (None, "pending", "refine_pending")
CONDITION_QID = "answer"
YES = ("y", "yes", "true", "approve", "approved")
NO = ("n", "no", "false")
QUESTIONS_SUFFIX = "questions.md"
NOTHING_RAN = "all steps skipped"
_STOPPED = "stopped"


def halted(status: str | None) -> bool:
    """Whether the runner must not start another step: the run ended, or a human paused it."""
    return status in TERMINAL or status == "paused"


def condition_label(cond: Any) -> str:
    """A condition's name for logs and questions: the name itself, or `inline condition`."""
    return cond if isinstance(cond, str) else "inline condition"


def _yes_no(text: str) -> bool | None:
    words = re.split(r"\W+", text.strip().lower())
    word = words[0] if words else ""
    return True if word in YES else False if word in NO else None


def current_refine(run: Run, phase: str) -> dict | None:
    """The refine round `phase` is in (`{phase, round, note}`), or None."""
    refine = run.state.get("refine") or {}
    return refine if refine.get("phase") == phase else None


def refine_subject(label: str, title: str) -> str:
    """The subject of a commit made in a refine round: `Refine <step id | Task N>: <title>`."""
    return f"Refine {label}: {title}"


def refine_note_block(refine: dict) -> str:
    """The rejection note, for the body of a commit made in its refine round."""
    return f"Refine round {refine.get('round')} note:\n{refine.get('note', '')}"


def writes_questions(step: Step) -> bool:
    return bool(step.output) and str(step.output).endswith(QUESTIONS_SUFFIX)


class StepControl:
    """`run_steps`/`run_step` for `lanes.Executor`; relies on its `cfg`, `run`, `ctx`, `dry_run`,
    `out`, `worktree`, `run_agent_step`, `dry_agent_argv` and `_gate_report`, and on `run_foreach`,
    `run_finish`, `record_decisions` and `_earlier_step_review` from `feature_lane.FeatureLane`."""

    cfg: Config
    run: Run
    ctx: Ctx
    dry_run: bool
    out: TextIO
    last_decided_by: str = "runner"  # who settled the last `decide`: runner, agent or human

    # ------------------------------------------------------------------ walking steps

    def run_steps(self, phase: Phase, steps: list[Step], prefix: str) -> tuple[Outcome, bool]:
        """Run `steps` in order under `prefix`. Returns `(outcome, any step ran)`; "stopped" means a
        human is needed (or the run ended). In a dry run nothing is called and the outcome is "skipped"."""
        steps = for_lane(steps, self.ctx.values["lane"])
        if self.dry_run:
            return "skipped", self._dry_steps(phase, steps, prefix, "  ")
        ran = False
        ids = [step.id for step in steps]
        index = 0
        while index < len(steps):
            step = steps[index]
            outcome, step_ran = self.run_step(phase, step, prefix)
            ran = ran or step_ran
            if outcome == "stopped":
                return "stopped", ran
            index += 1
            if outcome != "done" or not step.next:
                continue
            target = self._branch(phase, step, prefix)
            if target == _STOPPED:
                return "stopped", ran
            if target is None:
                continue
            at = ids.index(target) if target in ids else -1
            if at < index:
                raise ValueError(f"step {prefix}/{step.id}: goto {target!r} must name a later step in the same list")
            for jumped in steps[index:at]:
                self._skip(phase, f"{prefix}/{jumped.id}", None, f"goto {target} after {step.id}")
            index = at
        return "done", ran

    def run_step(self, phase: Phase, step: Step, prefix: str) -> tuple[Outcome, bool]:
        """Settle one step as `<prefix>/<id>`: `(outcome, whether it ran)`."""
        key = f"{prefix}/{step.id}"
        self.run.refresh()
        state = self.run.state
        if halted(state["status"]):  # `pause`/`cancel` take effect here, between steps
            return "stopped", False
        rec = state["steps"].get(key) or {}
        if rec.get("status") in DONE_STATUSES:
            return "done", True
        if rec.get("status") == "skipped":
            return "skipped", False
        if not self._started(step, key, rec):
            verdict, cond, reason, failed = self._should_run(phase, step, key)
            if verdict is None:
                return "stopped", False
            if not verdict and self._own_output(phase, step, rec, failed):
                verdict = True
                self.run.emit("condition_kept", key=key, condition=cond,
                              reason=f"{reason}; it reads {fmt(str(step.output), self.ctx)}, which this step wrote, "
                                     "so the refine round still runs the step")
            if not verdict:
                self._skip(phase, key, cond, reason)
                return "skipped", False
        outcome, ran = self._dispatch(phase, step, key)
        if outcome == "done" and not ran:
            self._skip(phase, key, None, NOTHING_RAN)
            return "skipped", False
        if outcome == "done":
            self.run.refresh()
            if (self.run.state["steps"].get(key) or {}).get("status") not in DONE_STATUSES:
                self.run.emit("step_finished", key=key, status="done", summary="")
        return outcome, True

    def _dispatch(self, phase: Phase, step: Step, key: str) -> tuple[Outcome, bool]:
        """Run the step; `(outcome, ran)`, where only a composite (group, foreach) can say it did
        not run, because every one of its children was skipped."""
        if step.type == "agent" and step.foreach:
            outcome = self.run_foreach(phase, step, key)
            return outcome, self._children_ran(key)
        if step.type == "group":
            return self._run_group(phase, step, key)
        handlers = {
            "agent": self._run_agent,
            "gate": self._run_gate_step,
            "checkpoint": self._run_checkpoint,
            "commit": self._run_commit_step,
            "finish": self.run_finish,
            "ci": self.run_ci,
        }
        if step.type not in handlers:
            raise ValueError(f"step {key}: unknown step type {step.type!r}")
        return handlers[step.type](phase, step, key), True

    def _children_ran(self, key: str) -> bool:
        self.run.refresh()
        child = key + "/"
        return any(k.startswith(child) and r.get("status") not in (*UNSTARTED, "skipped")
                   for k, r in self.run.state["steps"].items())

    def _started(self, step: Step, key: str, rec: dict) -> bool:
        """Whether the step has begun, so its conditions are no longer decided (they could have changed)."""
        if step.type == "group" or step.foreach:
            child = key + "/"
            return any(k.startswith(child) and r.get("status") not in UNSTARTED for k, r in self.run.state["steps"].items())
        return rec.get("status") not in UNSTARTED

    def _skip(self, phase: Phase, key: str, cond: str | None, reason: str) -> None:
        status = (self.run.state["steps"].get(key) or {}).get("status")
        if status in DONE_STATUSES or status == "skipped":
            return
        self.run.emit("step_skipped", key=key, phase=phase.name, condition=cond, reason=reason)

    def _waiting_elsewhere(self, key: str) -> bool:
        waiting = self.run.state["waiting"]
        return waiting is not None and waiting.get("key") != key

    def _waiting_on(self, key: str) -> bool:
        waiting = self.run.state["waiting"]
        return waiting is not None and waiting.get("key") == key

    # ------------------------------------------------------------------ conditions

    def _should_run(self, phase: Phase, step: Step, key: str) -> tuple[bool | None, str | None, str, Any]:
        """`(True|False|None, condition label, reason, the condition itself)`; None means the run stopped to ask."""
        if step.when is not None:
            value, reason = self.decide(phase, step.when, f"{key}.when")
            if value is None:
                return None, condition_label(step.when), reason, step.when
            if not value:
                return False, condition_label(step.when), reason, step.when
        if step.unless is not None:
            value, reason = self.decide(phase, step.unless, f"{key}.unless")
            if value is None:
                return None, condition_label(step.unless), reason, step.unless
            if value:
                return False, condition_label(step.unless), reason, step.unless
        return True, None, "", None

    def _own_output(self, phase: Phase, step: Step, rec: dict, failed: Any) -> bool:
        """In a refine round, whether the condition that would skip a step that ran reads the step's own
        `output` (as `unless: spec_exists` does for the draft that wrote the spec): it flipped because the
        step did its job, not because the refinement changed what the step is for."""
        if current_refine(self.run, phase.name) is None or not step.output or failed is None:
            return False
        if rec.get("status") != "refine_pending" or not (rec.get("session_id") or rec.get("result")):
            return False
        return fmt(str(step.output), self.ctx) in paths_read(self.cfg, failed, self.ctx)

    def decide(self, phase: Phase, cond: Any, cond_key: str) -> tuple[bool | None, str]:
        """Evaluate `cond`; when it is unknown, follow its `on_unknown`. `(None, reason)` = stopped.

        `last_decided_by` then says who decided it: `runner` (the files and git said), `agent` or `human`."""
        self.last_decided_by = "runner"
        result = evaluate(self.cfg, cond, self.ctx)
        if result.value is not None:
            return result.value, result.reason
        label = condition_label(cond)
        if result.on_unknown == "agent":
            value, why = self._ask_agent(phase, cond_key, label, result.reason)
            if value == _STOPPED:
                return None, result.reason
            if isinstance(value, bool):
                self._log_decision(cond_key, label, value, "agent", why)
                self.last_decided_by = "agent"
                return value, f"{result.reason}; an agent said {_word(value)}: {why}"
        return self._ask_human(cond_key, label, result.reason)

    def _ask_agent(self, phase: Phase, cond_key: str, label: str, reason: str) -> tuple[bool | str | None, str]:
        """One `mechanical`-role call that answers yes, no or unknown in `signals.answer`."""
        step = Step(id=cond_key.rsplit("/", 1)[-1], type="agent", role="mechanical", prompt="condition_question.md")
        plain = Phase(name=phase.name, agent=phase.agent, model=None, skip_if=None, steps=[])
        question = f"The runner could not decide the condition `{label}`: {reason}. Does `{label}` hold?"
        outcome = self.run_agent_step(plain, step, cond_key, extra={"question": question}, role="mechanical")
        if outcome != "done":
            return _STOPPED, ""
        try:
            result = read_result(self.run.result_path(cond_key))
        except ResultError as exc:
            return None, str(exc)
        return _yes_no(str(result.signals.get("answer", ""))), result.summary

    def _ask_human(self, cond_key: str, label: str, reason: str) -> tuple[bool | None, str]:
        given = self._latest_answers(cond_key)
        if given is not None:
            text = given.get(CONDITION_QID, "")
            value = _yes_no(text)
            if value is not None:
                self._log_decision(cond_key, label, value, "human", text)
                self.last_decided_by = "human"
                return value, f"{reason}; a human said {_word(value)}"
        if self._waiting_on(cond_key):
            return None, reason
        note = f" The answer {given.get(CONDITION_QID, '')!r} was neither yes nor no." if given is not None else ""
        question = Question(
            CONDITION_QID,
            f"`{label}` is unknown: {reason}. Treat as approved?",
            [Option("yes", f"Yes: treat `{label}` as true"), Option("no", f"No: treat `{label}` as false")],
            [],
            "other",
        )
        raise_hitl(self.cfg, self.run, "condition", cond_key, f"The runner could not decide `{label}`: {reason}.{note}",
                   [question])
        return None, reason

    def _latest_answers(self, key: str, after: int = 0) -> dict[str, str] | None:
        """The answers recorded for `key` since its last stop was raised (and after event `after`), if any."""
        events = self.run.events.read()
        raised = [e["seq"] for e in events if e["type"] == "hitl_raised" and e.get("key") == key]
        if not raised:
            return None
        since = max(raised[-1], after)
        answered = [e for e in events if e["type"] == "hitl_answered" and e.get("key") == key and e["seq"] > since]
        return dict(answered[-1].get("answers") or {}) if answered else None

    def _log_decision(self, cond_key: str, label: str, value: bool, by: str, why: str) -> None:
        logged = [e for e in self.run.events.read() if e["type"] == "condition_decided" and e.get("key") == cond_key]
        if logged and logged[-1].get("value") == value:
            return
        self.run.emit("condition_decided", key=cond_key, condition=label, value=value, decided_by=by, reason=why)

    def _branch(self, phase: Phase, step: Step, prefix: str) -> str | None:
        """The `goto` of the first `next:` branch that holds; decided once per pass of the step."""
        key = f"{prefix}/{step.id}"
        events = self.run.events.read()
        refined = max((e["seq"] for e in events if e["type"] == "phase_rejected" and e.get("phase") == phase.name), default=0)
        for event in reversed(events):
            if event["seq"] <= refined:
                break
            if event["type"] == "step_branched" and event.get("key") == key:
                return event.get("goto")
        for index, branch in enumerate(step.next or []):
            cond = branch.get("when")
            if cond is not None:
                value, reason = self.decide(phase, cond, f"{key}.next{index}")
                if value is None:
                    return _STOPPED
                if not value:
                    continue
            else:
                reason = "unconditional"
            self.run.emit("step_branched", key=key, goto=branch.get("goto"), reason=reason)
            return branch.get("goto")
        self.run.emit("step_branched", key=key, goto=None, reason="no branch holds")
        return None

    # ------------------------------------------------------------------ agent steps

    def _run_agent(self, phase: Phase, step: Step, key: str, extra: dict[str, str] | None = None) -> Outcome:
        if self.record_decisions(phase, key) == "stopped":
            return "stopped"
        self.run.refresh()
        rec = self.run.state["steps"].get(key) or {}
        refine = self.run.state.get("refine") or {}
        rerun = refine.get("phase") == phase.name and bool(rec.get("session_id") or rec.get("result"))
        # A review never resumes: it is told the last one's findings and the note. Nor does a step whose findings
        # hold the phase back, because `unchanged` would clear them with no check having run.
        review = step.role == "review" or step.blocks_on_findings
        if review and rerun:
            extra = {**(extra or {}), "earlier_findings": self._earlier_step_review(key, refine.get("note"))}
        mode = "refine" if rerun and not review else "first"
        fix_first = None
        if step.gates_first and not self.dry_run and rec.get("status") not in PRERUN_SKIP:
            if self.run.state["waiting"] is not None:
                return "stopped"
            evidence, fix_first = self._prerun_gates(step, key)
            extra = {**(extra or {}), "gate_evidence": evidence}
        outcome = self.run_agent_step(phase, step, key, extra=extra, mode=mode, fix_first=fix_first)
        if outcome == "done" and rec.get("status") not in DONE_STATUSES and writes_questions(step):
            self._render_questions(key, mode == "refine")
        return outcome

    def _prerun_gates(self, step: Step, key: str) -> tuple[str, list[GateResult] | None]:
        """Run a `gates_first` step's gates before its session: their output is the session's evidence.

        Returns the `{gate_evidence}` text and the failed gates (None when all passed), which start the
        session in its fix loop. The tree is recorded so the gates are not run again on it afterwards."""
        tree = snapshot(self.worktree)
        results = run_gates_reusing(self.cfg, self.run, key, step.gates or [], self.worktree,
                                    self.run.gate_log_dir(key), tree)
        record_results(self.run, key, results, tree)
        failed = [r for r in results if not (r.passed or r.skipped)]
        self.run.emit("gates_prerun", key=key, tree=tree, passed=not failed)
        return gate_evidence(results, tree), failed or None

    def _render_questions(self, key: str, refine: bool) -> None:
        """D6: questions a grilling step put in its result become `runs/<run>/questions.md`."""
        try:
            result = read_result(self.run.result_path(key), refine=refine)
        except ResultError:
            return
        if not result.questions:
            return
        path = self.run.questions_path()
        render_inbox(path, f"{self.run.id}: questions from {key}", result.summary, result.questions)
        self.run.emit("questions_rendered", key=key, path=str(path), questions=[asdict(q) for q in result.questions])

    # ------------------------------------------------------------------ groups

    def _run_group(self, phase: Phase, step: Step, key: str) -> tuple[Outcome, bool]:
        if not step.repeat:
            return self.run_steps(phase, step.steps, key)
        until = step.repeat["until"]
        rounds = int(step.repeat["max_rounds"])
        ran = False
        for round_ in range(1, rounds + 1):
            prefix = f"{key}/r{round_}"
            outcome, round_ran = self.run_steps(phase, step.steps, prefix)
            ran = ran or round_ran
            if outcome == "stopped":
                return "stopped", ran
            later = f"{key}/r{round_ + 1}/"
            if round_ < rounds and any(k.startswith(later) for k in self.run.state["steps"]):
                continue  # the next round already began: this round's `until` was decided then
            value, _reason = self.decide(phase, until, f"{prefix}.until")
            if value is None:
                return "stopped", ran
            if value:
                return "done", ran
        if self._waiting_on(key):
            return "stopped", ran
        label = condition_label(until)
        raise_hitl(self.cfg, self.run, "max_rounds", key,
                   f"`{label}` still does not hold after {rounds} rounds of {key}. Approve to go on as it is.")
        return "stopped", ran

    # ------------------------------------------------------------------ checkpoints

    def _run_checkpoint(self, phase: Phase, step: Step, key: str) -> Outcome:
        if self._waiting_elsewhere(key):
            return "stopped"
        rendered = self._questions_rendered(key.rsplit("/", 1)[0])
        rec = self.run.state["steps"].get(key) or {}
        if rec.get("status") == "refine_pending" and self._reusable(phase, key, rendered):
            self.run.emit("checkpoint_reused", key=key)
            return "done"
        if step.wait_for == "answered_file":
            return self._answered_file(key, rendered)
        if self._waiting_on(key):
            return "stopped"
        raise_hitl(self.cfg, self.run, "checkpoint", key,
                   f"Checkpoint `{step.name or step.id}`: a human decides before {phase.name} goes on.")
        return "stopped"

    def _reusable(self, phase: Phase, key: str, rendered: dict | None) -> bool:
        """In a refine round, an earlier answer stands unless a refined step wrote new questions."""
        events = self.run.events.read()
        if not any(e["type"] == "checkpoint_cleared" and e.get("key") == key for e in events):
            return False
        refined = max((e["seq"] for e in events if e["type"] == "phase_rejected" and e.get("phase") == phase.name), default=0)
        return rendered is None or rendered["seq"] < refined

    def _questions_rendered(self, prefix: str) -> dict | None:
        """The latest questions file written by a step of the same list as this checkpoint."""
        for event in reversed(self.run.events.read()):
            if event["type"] == "questions_rendered" and str(event.get("key", "")).rsplit("/", 1)[0] == prefix:
                return event
        return None

    def _answered_file(self, key: str, rendered: dict | None) -> Outcome:
        """Cleared once every `Answer:` in questions.md is filled (in the file, or via `agentflow answer`)."""
        questions = question_objects(rendered["questions"]) if rendered else []
        if not questions:
            self.run.emit("checkpoint_cleared", key=key, reason="no questions to answer", answers={})
            return "done"
        path = self.run.questions_path()
        if not path.exists():
            render_inbox(path, f"{self.run.id}: questions from {rendered['key']}", "", questions)
        given = self._latest_answers(key, after=rendered["seq"] if rendered else 0)
        if given:
            write_answers(path, given)
        answers = parse_answers(path)
        if all(answers.get(q.id) for q in questions):
            self.run.emit("checkpoint_cleared", by="questions.md", key=key, path=str(path),
                          answers={q.id: answers[q.id] for q in questions})
            return "done"
        if self._waiting_on(key):
            return "stopped"
        raise_hitl(self.cfg, self.run, "checkpoint", key,
                   f"Answer the questions in {path} (every `Answer:` line), or run `agentflow answer {self.run.id}`.",
                   questions)
        return "stopped"

    # ------------------------------------------------------------------ gates and commits

    def _run_gate_step(self, phase: Phase, step: Step, key: str) -> Outcome:
        """Run the step's gates; a failure stops for a human, and approving runs them again."""
        if self.run.state["waiting"] is not None:
            return "stopped"
        tree = snapshot(self.worktree)
        results = run_gates_reusing(self.cfg, self.run, key, step.gates or [], self.worktree,
                                    self.run.gate_log_dir(key), tree)
        record_results(self.run, key, results, tree)
        if gates_ok(results):
            self.run.emit("step_finished", key=key, status="done", summary=summarize(results))
            return "done"
        failed = [r for r in results if not (r.passed or r.skipped)]
        raise_hitl(self.cfg, self.run, "gate_failed", key, f"Gates failed in {key}: {', '.join(r.name for r in failed)}.",
                   extra=self._gate_report(failed))
        return "stopped"

    def _run_commit_step(self, phase: Phase, step: Step, key: str) -> Outcome:
        if self.run.state["waiting"] is not None:
            return "stopped"
        message = fmt(step.message or f"Step {key}", self.ctx)
        refine = current_refine(self.run, phase.name)
        if refine is not None:  # a refine round commits on top, with the note
            message = f"{refine_subject(step.id, message)}\n\n{refine_note_block(refine)}"
        try:
            sha = commit(self.worktree, message, self.cfg, expected_head=runner_head(self.run))
        except CommitRejected as exc:
            summary = f"Guard violation in {key}: {exc.kind}: {exc}"
            raise_hitl(self.cfg, self.run, "guard_violation", key, summary)
            self.run.emit("step_finished", key=key, status="failed", summary=summary)
            return "stopped"
        except NothingToCommit:
            sha = None
        if sha:
            self.run.emit("step_committed", key=key, sha=sha, summary=message)
        self.run.emit("step_finished", key=key, status="done", summary=message, sha=sha)
        return "done"

    # ------------------------------------------------------------------ dry run

    def _dry_verdict(self, step: Step) -> tuple[str, str]:
        """`run`, `skip` or `ask` (unknown, so a real run would decide it then) from `when`/`unless`."""
        for cond, skip_on in ((step.when, False), (step.unless, True)):
            if cond is None:
                continue
            result = evaluate(self.cfg, cond, self.ctx)
            if result.value is None:
                return "ask", result.reason
            if result.value is skip_on:
                return "skip", result.reason
        return "run", ""

    def _say(self, text: str) -> None:
        print(text, file=self.out or sys.stdout)

    def _dry_agent(self, phase: Phase, step: Step, key: str, indent: str, extra: dict | None = None) -> None:
        argv = self.dry_agent_argv(phase, step, key, extra)
        prompt = f"<prompt: {step.prompt or 'step.md'}, {len(argv[-1])} chars>"
        label = f" ({extra['task_title']})" if extra and extra.get("task_title") else ""
        self._say(f"{indent}{key}{label}: {shlex.join(argv[:-1])} {prompt}")
        if step.gates:
            self._dry_gates(step.gates, indent + "  ")
        for name in step.checks:
            self._say(f"{indent}  doc check {name} (on the diff since the step started)")

    def _dry_gates(self, names: str | list[str], indent: str) -> None:
        for gate in resolve(self.cfg, names):
            where = f" (in {gate.cwd})" if gate.cwd else ""
            only = f" (skipped unless {gate.when_exists} exists)" if gate.when_exists else ""
            self._say(f"{indent}gate {gate.name}: {gate.run}{where}{only}")

    def _dry_steps(self, phase: Phase, steps: list[Step], prefix: str, indent: str) -> bool:
        """Print what `steps` would do; True when any step would run."""
        ran = False
        for step in steps:
            key = f"{prefix}/{step.id}"
            verdict, why = self._dry_verdict(step)
            if verdict == "skip":
                self._say(f"{indent}– skip {key} ({why})")
                continue
            ran = True
            if verdict == "ask":
                self._say(f"{indent}? {key}: unknown condition, a real run decides it then ({why})")
            self._dry_step(phase, step, key, indent)
        return ran

    def _dry_step(self, phase: Phase, step: Step, key: str, indent: str) -> None:
        if step.type == "agent" and step.foreach:
            tasks = self._dry_tasks()
            for n, title, text in tasks:
                extra = {"task_n": n, "task_title": title, "task_text": text}
                self._dry_agent(phase, step, f"{key}/task-{n}/implement", indent, extra)
        elif step.type == "agent":
            self._dry_agent(phase, step, key, indent)
        elif step.type == "gate":
            self._say(f"{indent}{key}: gates")
            self._dry_gates(step.gates or [], indent + "  ")
        elif step.type == "group":
            repeat = step.repeat or {}
            note = (f" (repeats until {condition_label(repeat.get('until'))}, at most {repeat.get('max_rounds')} rounds)"
                    if repeat else "")
            self._say(f"{indent}{key}: group{note}")
            self._dry_steps(phase, step.steps, f"{key}/r1" if repeat else key, indent + "  ")
        elif step.type == "checkpoint":
            wait = " (answers in questions.md)" if step.wait_for == "answered_file" else ""
            self._say(f"{indent}⏸ {key}: checkpoint{wait}")
        elif step.type == "commit":
            self._say(f"{indent}{key}: git commit -m {shlex.quote(fmt(step.message or 'Step ' + key, self.ctx))}")
        elif step.type == "finish":
            base = self.cfg.project.fork_from
            self._say(f"{indent}{key}: git pull --no-commit origin {base}; check the result; git push -u origin <branch>; "
                      f"gh pr create --draft --base {base} --head <branch>")
            if step.sync is not None:
                self._say(f"{indent}  (conflicts, or a failing check on the merged tree, go to {key}/{step.sync.id}-N)")
                self._dry_agent(phase, step.sync, f"{key}/{step.sync.id}-1", indent + "  ", {"sync_report": "(the conflicted files)"})
            for obligation, why in applicable(self.cfg, self.ctx, self.ctx.values["lane"]):  # the audit before the push
                require = ", ".join(str(item) for item in obligation.require)
                self._say(f"{indent}  obligation {obligation.id}: {require} ({why})")
        elif step.type == "ci":
            self._say(f"{indent}{key}: gh pr checks <pr>; fix what fails and push; gh pr ready <pr>")

    def _dry_tasks(self) -> list[tuple[str, str, str]]:
        """The plan's unticked tasks, or one representative `Task N` when there is no plan yet."""
        rel = self.ctx.values.get("plan") or ""
        if rel and (self.ctx.repo / rel).is_file():
            tasks = [t for t in sources.parse_plan(self.ctx.repo / rel) if t.open]
            return [(str(t.n), f"Task {t.n}: {t.title}", t.text) for t in tasks]
        return [("N", "Task N (one session per plan task; no plan yet)", "(the task's text from the plan)")]


def _word(value: bool) -> str:
    return "yes" if value else "no"


def read_json(path: Path) -> Any:
    """A JSON file's content, or None when it is missing or not JSON (for reports, never for decisions)."""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
