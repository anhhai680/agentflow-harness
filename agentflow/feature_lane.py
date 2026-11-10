"""The feature lane: plan tasks, per-task review and commits, parking, decisions, and `finish`.

`FeatureLane` is mixed into `lanes.Executor` next to `flow_control.StepControl`.

`run_foreach` (a `foreach: plan_task` step) walks the plan's tasks in order. A ticked task the run
never touched is passed over, and so is a task this run already committed. Each other task runs:

1. the implementer, `<step>/task-<n>/implement`, in a fresh session (a small plan: the previous
   task's session, with the `small_plan` skill), with the gates and the fix loop of `run_agent_step`;
2. the reviewer, `<step>/task-<n>/review`, in a fresh session on the `review.role` model, given the
   diff range `<task start sha>..worktree`; blocking findings go back to the implementer's session
   once (with the pack's review-feedback skill, the gates again), then one re-review in a fresh session that gets
   those findings as text; still blocking: a `review_unresolved` stop (an unreadable review result is
   a `blocked` stop, never a clean review). Approving it gives a fresh reviewer one last look, with
   the human's note, and the task is committed, with any finding still open listed in its commit body.
   Every review round is a new session, so no reviewer inherits an earlier verdict; only a stop inside
   a round resumes that round's own session;
3. the implementer's gates again when the tree is not the one they last passed on (a `gate_failed`
   stop on `<task>/commit` if they fail), then `tick_task` and one commit `Task N: <title>` holding
   the work and the ticked boxes.

In a refine round (a rejected `build`), every task the run committed runs again in plan order: its
implementer resumes its own session with the note, its reviewer runs again on the review prompt in a
fresh session over the refine diff (from the task's previous commit),
with blocking findings, the fix round and `review_unresolved` as above counted within the round, and
the task is committed on top as `Refine Task N: <title>` with the note in the body. A reviewer cannot
answer `unchanged`. A task parked in the round holds back only the tasks that depend on it.
A rejected stop inside one task (`refine.scope`, `<step>/task-<n>`) refines only that task: the other
committed tasks are passed over as if committed in the round, unless a step of theirs still waits on an
earlier round's refine. A stop inside a batch of several tasks refines the whole phase. An implementer
that answers `unchanged`, leaves the tree as its round started, and started from the task's committed
work gets no review (`review_skipped`) and no gate run: the task is recorded as committed in the round
with no commit (`sha: null`).

A small plan has no per-task review. A `needs_human` result with `can_continue_without` (and
`hitl.park_independent`) parks the task: the question is asked without stopping, and tasks that do
not depend on it (D12, transitively) go on. The run stops for the first parked question only when no
runnable task remains; the others stay answerable (`state["parked"]`), and one answered while the
run goes on lets its task (and its dependents) run in another walk of the plan. Each state change is an event,
so a resumed run picks every task up where it stopped.

`record_decisions` runs before an answered step resumes: an answer to a `spec_gap` question (or one
given with `--record`) runs `record_step` and commits its `message` (default `Decide <question> in the
<module> spec`) with only the files that step changed. `ambiguity`/`review_dispute`/`boundary` answers go in the task's
commit body. The pull request lists every answer the runner took itself (`by: runner`) under "Decisions the runner took".

`run_finish` first brings the latest `origin/<fork_from>` into the branch (`sync_lane`), then refuses
uncommitted changes, pushes the branch (never forced) and opens a draft pull request against `fork_from` whose
body, `runs/<run>/pr.md`, comes from `render_pr_body`, and checks GitHub finds it conflict-free.
"""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from agentflow import sources
from agentflow.autonomy import AUTO_BY, as_finding, blocking_of, finding_line
from agentflow.commit_checks import foreign_commits, runner_head
from agentflow.conditions import Ctx, fmt, forbidden_plan_files, skill_text
from agentflow.config import Config, Phase, Step, brief_files
from agentflow.conformance import audit, clean_tree, render_section
from agentflow.flow_control import current_refine, refine_note_block, refine_subject
from agentflow.gates import gates_ok, record_results, run_gates
from agentflow.gitops import (
    CommitRejected,
    GhError,
    GitError,
    NothingToCommit,
    commit,
    create_draft_pr,
    push,
)
from agentflow.guards import changed_paths, snapshot
from agentflow.hitl import ResultError, clean_fact, notify_run, raise_hitl, read_result
from agentflow.issue_status import link

if TYPE_CHECKING:
    from agentflow.state import Run

Outcome = Literal["done", "skipped", "stopped"]
PR_FOOTER = "🤖 Generated with [Claude Code](https://claude.com/claude-code)"
TASK_KEY = re.compile(r"/task-\d+/(implement|review)$")
TASK_N = re.compile(r"/task-(\d+)/")
DONE_STATUSES = ("done", "unchanged")
SUMMARY_MAX = 60
DEFAULT_RECORD = {"spec_gap": "record_step", "ambiguity": "commit_body", "review_dispute": "commit_body",
                  "boundary": "commit_body"}


def question_summary(text: str, limit: int = SUMMARY_MAX) -> str:
    """A question as a short commit-subject phrase: one line, no closing punctuation, cut at a word."""
    line = " ".join(str(text).split()).rstrip("?.!:; ")
    if len(line) > limit:
        line = line[:limit].rsplit(" ", 1)[0].rstrip(",;: ")
    return line


def earlier_review(findings: list[dict], note: str | None) -> str:
    """All a fresh re-review knows of the last one: its blocking findings and a human's note, as text."""
    parts = []
    if findings:
        lines = "\n".join(finding_line(f) for f in findings)
        parts.append(f"An earlier review found these blocking problems:\n\n{lines}\n\n"
                     "Check each one against the code as it is now, and review the whole range as well.")
    if note:
        parts.append(f"A human looked at the last review and said: {note}")
    return "\n\n".join(parts)


class FeatureLane:
    """`run_foreach`, `record_decisions`, `may_park` and `run_finish` for `lanes.Executor`; relies on its
    `cfg`, `run`, `ctx`, `worktree`, `run_agent_step`, `decide`, `_waiting_on`, `_since_step_started` and
    `_gate_report`."""

    cfg: Config
    run: Run
    ctx: Ctx
    worktree: Path

    # ------------------------------------------------------------------ plan tasks

    _task_total: int | None = None  # how many `### Task N` the plan holds, ticked or not: set by `run_foreach`
    _batch_of: dict[int, list[int]] | None = None  # task number -> its batch (`step.batch`): set by `run_foreach`

    def _task_payload(self, key: str) -> dict | None:
        """`{"n", "of"}` for a plan task's step, so the dashboard's total is the plan's, not the tasks seen so far."""
        match = TASK_N.search(key)
        if match is None or not self._task_total:
            return None
        return {"n": int(match[1]), "of": self._task_total}

    def may_park(self, key: str) -> bool:
        """Whether a `needs_human` with `can_continue_without` on `key` parks instead of stopping."""
        if not (self.cfg.hitl.get("park_independent") and TASK_KEY.search(key)):
            return False
        match = TASK_N.search(key)
        # A batch member stops instead: parked work stays in the tree uncommitted, so a park would let the
        # batch's single review miss members, or take in work that is not theirs.
        return not (match and self._batch_of and int(match[1]) in self._batch_of)

    def run_foreach(self, phase: Phase, step: Step, prefix: str) -> Outcome:
        """Every plan task in order, as described in the module docstring. "stopped" = a human is needed."""
        rel = self.ctx.values.get("plan") or ""
        plan = self.worktree / rel if rel else None
        if plan is None or not plan.is_file():
            if self._waiting_on(prefix):
                return "stopped"
            module = self.ctx.values.get("module") or "?"
            raise_hitl(self.cfg, self.run, "blocked", prefix,
                       f"{prefix} runs once per plan task, but there is no plan for `{module}` yet.")
            return "stopped"
        if self._needs_human_files(plan, prefix):
            return "stopped"
        self._task_total = len(sources.parse_plan(plan))
        self._scale_budget(plan)
        small = self._small_plan(phase, step, prefix)
        if small is None:
            return "stopped"
        self._batch_of = self._batches(phase, step, plan, prefix, small)
        while True:
            stopped, held, attempted = self._plan_pass(phase, step, plan, rel, prefix, small)
            if stopped:
                return "stopped"
            if not held:
                return "done"
            if self._stop_for_parked(prefix):
                return "stopped"
            # Held tasks but nothing parked: a parked question was answered during this pass, so its
            # task (and the tasks waiting on it) can run now. Walk the plan again.
            if not attempted:
                raise_hitl(self.cfg, self.run, "blocked", prefix,
                           f"Tasks {sorted(held)} of {prefix} are held back, but no parked question explains it.")
                return "stopped"

    def _scale_budget(self, plan: Path) -> None:
        """Once per run, raise the soft budget to fit the plan: `task_budget_usd` per unticked task on top of
        the spend so far, plus a quarter of `run_budget_usd` for verify, review and ship. It never lowers the
        budget, never raises the hard one (an estimate above it notifies once, and the run goes on), and a
        budget a human set stays as they set it. A run started before the soft budget (no `soft_budget_usd`)
        raises its one budget, as it always did."""
        per_task = float(self.cfg.defaults.task_budget_usd)
        if per_task <= 0:
            return
        events = self.run.events.read()
        if any(e["type"] == "budget_scaled" for e in events):
            return
        if any(e["type"] == "run_updated" and e.get("budget_usd") is not None and e.get("by") != "runner"
               for e in events):
            return
        unticked = sum(1 for task in sources.parse_plan(plan) if task.open)
        state = self.run.state
        spend = float(state.get("spend_usd") or 0.0)
        target = round(spend + per_task * unticked + 0.25 * self.cfg.defaults.run_budget_usd, 2)
        soft = state.get("soft_budget_usd")
        if soft is None:
            current = float(state.get("budget_usd") or self.cfg.defaults.run_budget_usd)
            raised = target > current
            if raised:
                self.run.emit("run_updated", budget_usd=target)
            self.run.emit("budget_scaled", budget_usd=max(target, current), previous=current, tasks=unticked,
                          per_task=per_task, raised=raised)
            return
        current = float(soft)
        raised = target > current
        if raised:
            self.run.emit("run_updated", soft_budget_usd=target)
        self.run.emit("budget_scaled", budget_usd=max(target, current), previous=current, tasks=unticked,
                      per_task=per_task, raised=raised, soft=True)
        hard = state.get("budget_usd")
        if hard and target > float(hard):
            self.run.emit("budget_estimate_over_hard", estimate=target, hard=float(hard))
            notify_run(self.cfg, self.run, f"the plan's estimate ${target:.2f} is above the hard budget "
                                           f"${float(hard):.2f}; the run goes on and stops there for approval")

    def _needs_human_files(self, plan: Path, prefix: str) -> bool:
        """Stop before the first task when an open task names a forbidden path: the guards would fail that task
        after every task before it was built. An approval of this stop (the human made the change, or edited the
        plan) lets the run go on; the check is not repeated. True = stopped."""
        if self._waiting_on(prefix):
            return True
        if any(e["type"] == "hitl_approved" and e.get("key") == prefix for e in self.run.events.read()):
            return False
        tasks = sources.parse_plan(plan)
        todo = [t for t in tasks if not self._committed(f"{prefix}/task-{t.n}")]
        found = forbidden_plan_files(self.cfg, todo)
        if not found:
            return False
        lines = "; ".join(f"Task {n}: {', '.join(paths)}" for n, paths in found.items())
        raise_hitl(self.cfg, self.run, "blocked", prefix,
                   f"The plan needs files that `guardrails.forbidden_paths` refuses in a run, so no agent can write "
                   f"them ({lines}). Make those changes yourself in the run's worktree and commit them, or edit the "
                   f"plan to drop them, then approve.")
        return True

    def _plan_pass(self, phase: Phase, step: Step, plan: Path, rel: str, prefix: str,
                   small: dict) -> tuple[bool, set[int], bool]:
        """One walk over the plan: `(stopped, held task numbers, whether any task was attempted)`."""
        held: set[int] = set()
        previous: str | None = None
        previous_n: int | None = None
        attempted = False
        refine = self._refine_round(phase)
        tasks = sources.parse_plan(plan)
        texts = {t.n: t for t in tasks}
        for task in tasks:
            self.run.refresh()
            tkey = f"{prefix}/task-{task.n}"
            impl_key = f"{tkey}/implement"
            if not task.open and impl_key not in self.run.state["steps"]:
                continue
            if self._committed(tkey):
                if refine and not self._committed(tkey, refine) and self._sent_back(tkey):
                    if self._parked(tkey) or any(dep in held for dep in task.depends_on):
                        held.add(task.n)
                        continue
                    attempted = True
                    if self._refine_task(phase, step, plan, rel, task, tkey, small, refine) == "stopped":
                        if not self._parked(tkey):
                            return True, held, attempted
                        held.add(task.n)  # parked in the refine round: independent tasks are refined on
                        continue
                previous, previous_n = impl_key, task.n
                continue
            if self._parked(tkey) or any(dep in held for dep in task.depends_on):
                held.add(task.n)
                continue
            batch = (self._batch_of or {}).get(task.n) if not refine else None
            in_batch = bool(batch) and previous_n is not None and previous_n in (batch or [])
            share = previous if small.get("shared_session") or in_batch else None
            attempted = True
            members = [texts[n] for n in batch] if batch else None
            if self._run_task(phase, step, plan, rel, task, tkey, small, share, batch=members,
                              prefix=prefix) == "stopped":
                if not self._parked(tkey):
                    return True, held, attempted
                held.add(task.n)
                continue
            previous, previous_n = impl_key, task.n
        return False, held, attempted

    def _batches(self, phase: Phase, step: Step, plan: Path, prefix: str, small: dict) -> dict[int, list[int]]:
        """Task number -> its batch (`step.batch`), decided once per run and kept in a `plan_batched` event, so a
        resume sees the same batches after their tasks were ticked. None in a small plan or a refine round."""
        if not step.batch or small.get("active") or self._refine_round(phase):
            return {}
        recorded = [e for e in self.run.events.read() if e["type"] == "plan_batched" and e.get("key") == prefix]
        if recorded:
            batches = [list(b) for b in recorded[-1].get("batches") or []]
        else:
            batches = sources.plan_batches(sources.parse_plan(plan), int(step.batch.get("max_tasks", 3)),
                                           int(step.batch.get("max_files", 3)))
            self.run.emit("plan_batched", key=prefix, batches=batches)
        return {n: batch for batch in batches for n in batch}

    def _small_plan(self, phase: Phase, step: Step, prefix: str) -> dict | None:
        """`small_plan` with `active: True` when its `when` holds, `{}` when it does not, None = stopped to ask."""
        small = step.small_plan
        if not small:
            return {}
        when = small.get("when")
        if when is not None:
            value, _reason = self.decide(phase, when, f"{prefix}.small_plan")
            if value is None:
                return None
            if not value:
                return {}
        return {**small, "active": True}

    def _refine_round(self, phase: Phase) -> int:
        refine = self.run.state.get("refine") or {}
        return int(refine.get("round") or 0) if refine.get("phase") == phase.name else 0

    def _round_events(self, phase: Phase) -> list[dict]:
        """The run's events, or in a refine round only those since the phase was rejected: review rounds,
        the fix round and approvals of unresolved findings count afresh in each refine round."""
        events = self.run.events.read()
        if not self._refine_round(phase):
            return events
        for index in range(len(events) - 1, -1, -1):
            if events[index]["type"] == "phase_rejected" and events[index].get("phase") == phase.name:
                return events[index + 1 :]
        return events

    def _sent_back(self, tkey: str) -> bool:
        """Whether this refine round refines committed task `tkey`: every task in a phase-level round. A round
        scoped to one task (a rejected stop inside it) passes over the others as if already committed in the
        round, unless a step of theirs still waits on an earlier round's refine (`refine_pending`)."""
        scope = (self.run.state.get("refine") or {}).get("scope")
        if not scope or tkey == scope:
            return True
        steps = self.run.state["steps"]  # the task's own steps only: a resolver's row (`…/resolve-N`) is never settled
        return any((steps.get(f"{tkey}/{part}") or {}).get("status") == "refine_pending"
                   for part in ("implement", "review", "commit"))

    def _changed_nothing(self, tkey: str) -> bool:
        """The implementer answered `unchanged`, the tree is the one its last `step_started` recorded, and that
        tree held nothing uncommitted of this task's (an earlier round's edits left by a stop are still to review)."""
        impl_key = f"{tkey}/implement"
        if (self.run.state["steps"].get(impl_key) or {}).get("status") != "unchanged":
            return False
        started = [e for e in self.run.events.read() if e["type"] == "step_started" and e.get("key") == impl_key]
        if not started:
            return False
        start_tree, start_head = str(started[-1].get("start_tree") or ""), str(started[-1].get("start_head") or "")
        if not start_tree or not start_head or start_tree != snapshot(self.worktree):
            return False
        held = self._held_paths(tkey)  # other parked tasks' files are theirs, not this task's
        return not [p for p in changed_paths(self.worktree, f"{start_head}^{{tree}}", start_tree) if p not in held]

    def _committed(self, tkey: str, round_: int = 0) -> bool:
        return any(e["type"] == "task_committed" and e.get("key") == tkey and int(e.get("round") or 0) == round_
                   for e in self.run.events.read())

    def _parked(self, tkey: str) -> bool:
        steps = self.run.state["steps"]
        return any((steps.get(f"{tkey}/{part}") or {}).get("status") == "parked" for part in ("implement", "review"))

    def _stop_for_parked(self, prefix: str) -> bool:
        """No runnable task remains: the first parked question becomes the run's wait. True = stopped;
        False when nothing is parked or waiting any more (a question was answered while the run went on)."""
        self.run.refresh()
        if self.run.state["waiting"] is not None:
            return True
        parked = self.run.state.get("parked") or {}
        items = [item for key, item in parked.items() if key.startswith(prefix + "/")]
        if not items:
            return False
        item = items[0]
        self.run.emit("hitl_raised", kind=item["kind"], key=item["key"], phase=item["phase"], summary=item["summary"],
                      questions=item["questions"], inbox=str(self.run.inbox_path(item["key"])))
        notify_run(self.cfg, self.run, f"No runnable task remains: {len(items)} parked question(s) wait.")
        return True

    def _task_extra(self, task: sources.PlanTask, rel: str) -> dict[str, str]:
        return {"task_n": str(task.n), "task_title": f"Task {task.n}: {task.title}", "task_text": task.text,
                "plan": rel}

    def _impl_step(self, step: Step, small: dict, task_text: str = "") -> Step:
        """The implementer's step: the `small_plan` variant when it is active, with the `task_briefs` the task names."""
        if small.get("active") and small.get("skill"):
            step = dataclasses.replace(step, skill=str(small["skill"]), prompt=small.get("prompt", step.prompt),
                                       brief=brief_files(small.get("brief"), "small_plan.brief"))
        named = [name for needle, name in step.task_briefs.items() if needle in task_text]
        return dataclasses.replace(step, task_brief=list(dict.fromkeys(named)))

    def _task_agent(self, phase: Phase, step: Step, key: str, extra: dict[str, str],
                    share: str | None = None, review: bool = False) -> Outcome:
        """One task agent step: decisions recorded first, then `run_agent_step` (refine mode in a refine round
        for the implementer; a reviewer always gets the review prompt, so it can never answer `unchanged`)."""
        if self.record_decisions(phase, key) == "stopped":
            return "stopped"
        self.run.refresh()
        rec = self.run.state["steps"].get(key) or {}
        refine = not review and self._refine_round(phase) and bool(rec.get("session_id") or rec.get("result"))
        return self.run_agent_step(phase, step, key, extra=extra, mode="refine" if refine else "first",
                                   share_session_of=share)

    def _run_task(self, phase: Phase, step: Step, plan: Path, rel: str, task: sources.PlanTask, tkey: str,
                  small: dict, share: str | None, round_: int = 0, batch: list[sources.PlanTask] | None = None,
                  prefix: str = "") -> Outcome:
        """Implementer, reviewer, commit. In refine round `round_`, the same in the task's own sessions.

        In a batch (`step.batch`), a member after the first continues the previous member's session on the
        short `batch.prompt`, members before the last are committed without a review, and the last one's
        review covers every member, from where the first one started."""
        impl_step = self._impl_step(step, small, task.text)
        continues = bool((self.run.state["steps"].get(share) or {}).get("session_id")) if share else False
        if batch and continues and step.batch and step.batch.get("prompt"):
            impl_step = dataclasses.replace(impl_step, prompt=str(step.batch["prompt"]), continuation=True)
        elif continues and small.get("active") and small.get("shared_session"):  # its session holds the skill text
            impl_step = dataclasses.replace(impl_step, continuation=True)
        extra = self._task_extra(task, rel)
        if self._task_agent(phase, impl_step, f"{tkey}/implement", extra, share) != "done":
            return "stopped"
        open_findings: list[dict] = []
        review = step.review if not small.get("active") or small.get("review") else None
        last = not batch or task.n == batch[-1].n
        if round_ and self._changed_nothing(tkey):  # nothing to review or gate: the tree passed before
            review_key = f"{tkey}/review"
            if review is not None and not any(e["type"] == "review_skipped" and e.get("key") == review_key
                                              for e in self._round_events(phase)):
                self.run.emit("review_skipped", key=review_key, reason="unchanged", round=round_)
            return self._commit_task(plan, task, tkey, [], round_, gates=None, phase=phase)
        if review is not None and last:
            if (self.run.state["steps"].get(f"{tkey}/review") or {}).get("status") == "skipped":
                # skipped earlier in the round, but the tree changed since (a human edited at a stop): review it
                self.run.emit("step_retry", key=f"{tkey}/review", reason="the tree changed since the review was skipped")
            start = None
            rextra = extra
            if batch and len(batch) > 1:
                start = self._task_start(phase, f"{prefix}/task-{batch[0].n}/implement")
                rextra = {**extra, "task_title": f"Tasks {batch[0].n}-{task.n} ("
                          + "; ".join(t.title for t in batch) + ")",
                          "task_text": "\n\n".join(t.text for t in batch)}
            outcome, open_findings = self._review_task(phase, impl_step, review, tkey, extra, rextra, start)
            if outcome != "done":
                return "stopped"
        return self._commit_task(plan, task, tkey, open_findings, round_, gates=impl_step.gates, phase=phase)

    def _refine_task(self, phase: Phase, step: Step, plan: Path, rel: str, task: sources.PlanTask, tkey: str,
                     small: dict, round_: int) -> Outcome:
        """A rejected build phase: the task runs again as on the first pass, its implementer resuming its
        own session with the note and a fresh reviewer on the refine diff, and is committed on top."""
        return self._run_task(phase, step, plan, rel, task, tkey, small, None, round_)

    # ------------------------------------------------------------------ review

    def _review_task(self, phase: Phase, impl_step: Step, review: Step, tkey: str, extra: dict[str, str],
                     review_extra: dict[str, str] | None = None,
                     start_sha: str | None = None) -> tuple[Outcome, list[dict]]:
        """`("done", open findings)` once the task may be committed; ("stopped", []) when a human is needed.
        A batch's review passes the range's start (its first member's) and the batch's title and texts."""
        impl_key, review_key = f"{tkey}/implement", f"{tkey}/review"
        sha = start_sha or self._task_start(phase, impl_key)
        rextra = {**(review_extra or extra), "task_start_sha": sha, "diff_range": f"{sha}..worktree"}
        first = self._review_round(phase, review, review_key, rextra, 1)
        if first is None:
            return "stopped", []
        if not first:
            return "done", []
        if self._fix_round(phase, impl_step, impl_key, extra, first) != "done":
            return "stopped", []
        second = self._review_round(phase, review, review_key, rextra, 2)
        if second is None:
            return "stopped", []
        if not second:
            return "done", []
        approved_by = self._unresolved_approver(phase, review_key)
        if approved_by is None:
            self._raise_unresolved(review_key, tkey, second)
            return "stopped", []
        last = self._review_round(phase, review, review_key, rextra, 3)
        if last is None:
            return "stopped", []
        if last and approved_by == AUTO_BY:  # the runner may ask for one more look; only a human accepts findings
            self._raise_unresolved(review_key, tkey, last)
            return "stopped", []
        return "done", last

    def _raise_unresolved(self, review_key: str, tkey: str, findings: list[dict]) -> None:
        if not self._waiting_on(review_key):
            lines = "\n".join(finding_line(f) for f in findings)
            raise_hitl(self.cfg, self.run, "review_unresolved", review_key,
                       f"{len(findings)} blocking review finding(s) are still open after the fix rounds in {tkey}.",
                       extra=f"### Open findings\n\n{lines}")

    def _task_start(self, phase: Phase, impl_key: str) -> str:
        """Where the task's diff starts: `HEAD` when its implementer started (in a refine round: started
        refining), or when it was last rebased (other tasks committed while it was parked), so the range
        holds this task's work only."""
        sha = ""
        for event in self._round_events(phase):
            if event.get("key") != impl_key:
                continue
            if event["type"] == "step_started" and not sha:
                sha = str(event.get("start_head") or "")
            elif event["type"] == "step_rebased":
                sha = str(event.get("start_head") or sha)
        return sha

    def _review_round(self, phase: Phase, review: Step, key: str, extra: dict[str, str],
                      round_: int) -> list[dict] | None:
        """Round `round_`'s blocking findings (recorded once as `review_result`); None = stopped.

        A round that has not started gets a fresh session: `step_retry`, not a resume, so the earlier
        verdict reaches the reviewer only as the findings in its prompt. A stop inside a round
        resumes that round's own session."""
        results = [e for e in self._round_events(phase) if e["type"] == "review_result" and e.get("key") == key]
        if len(results) >= round_:
            return list(results[round_ - 1].get("blocking_findings") or [])
        if results and not self._reviewer_started_since(key, results[-1]):
            self.run.emit("step_retry", key=key, reason="re-review", round=round_)
        extra = {**extra, "earlier_findings": self._earlier_findings(phase, key, results)}
        if self._task_agent(phase, review, key, extra, review=True) != "done":
            return None
        try:  # `unchanged` is no review: it is refused like any unreadable result
            findings = read_result(self.run.result_path(key)).findings
        except ResultError as exc:  # never a clean review: approving reruns the reviewer for a readable result
            if not self._waiting_on(key):
                raise_hitl(self.cfg, self.run, "blocked", key,
                           f"The review of {key} finished, but its result cannot be read: {exc}.")
            return None
        blocking = blocking_of(self.cfg, findings)
        self.run.emit("review_result", key=key, round=round_, findings=findings, blocking=len(blocking),
                      blocking_findings=blocking, tree=snapshot(self.worktree))
        return blocking

    def _reviewer_started_since(self, key: str, result: dict) -> bool:
        return any(e["type"] == "agent_started" and e.get("step") == key and e["seq"] > result["seq"]
                   for e in self.run.events.read())

    def _earlier_findings(self, phase: Phase, key: str, results: list[dict]) -> str:
        """What a re-review of a task is told: the last round's blocking findings, and a human's note on them."""
        if not results:
            return ""
        approvals = [e for e in self._round_events(phase) if e["type"] == "hitl_approved" and e.get("key") == key
                     and e.get("kind") == "review_unresolved" and e.get("note")]
        return earlier_review(list(results[-1].get("blocking_findings") or []), approvals[-1]["note"] if approvals else None)

    def _earlier_step_review(self, key: str, note: str | None) -> str:
        """What a review step of a rejected phase is told: its last blocking findings and the rejection note."""
        try:
            findings = read_result(self.run.result_path(key), refine=True).findings
        except ResultError:  # the note still reaches the reviewer
            findings = []
        return earlier_review(blocking_of(self.cfg, findings), note)

    def _fix_round(self, phase: Phase, impl_step: Step, impl_key: str, extra: dict[str, str],
                   blocking: list[dict]) -> Outcome:
        """The one round in which the implementer's session gets the blocking findings back."""
        reopened = any(e["type"] == "step_reopened" and e.get("key") == impl_key and e.get("reason") == "review"
                       for e in self._round_events(phase))
        self.run.refresh()
        status = (self.run.state["steps"].get(impl_key) or {}).get("status")
        if reopened and status in DONE_STATUSES:
            return "done"
        if not reopened:
            findings = "\n".join(finding_line(f) for f in blocking)
            name = self.cfg.skills_map.get("review_feedback")  # read from the main checkout, never the worktree
            skill = skill_text(self.cfg, self.ctx.repo, name) if name else None
            note = (f"Use the `{name}` skill: its full text is below, so do not load it.\n\n" if skill is not None else "")
            note += (f"A reviewer found blocking problems in your change:\n\n"
                     f"{findings}\n\nCheck each one against the code; fix the ones that hold (with a test where it "
                     f"fits), say in your summary why any does not, then run the gates.")
            if skill is not None:
                note += f"\n\n{skill}"
            self.run.emit("step_reopened", key=impl_key, reason="review", round=1, note=note)
        return self._task_agent(phase, impl_step, impl_key, extra)

    def _unresolved_approver(self, phase: Phase, review_key: str) -> str | None:
        """Who last approved going on past open findings: a human's approval wins over the runner's."""
        approvers = [str(e.get("by")) for e in self._round_events(phase) if e["type"] == "hitl_approved"
                     and e.get("key") == review_key and e.get("kind") == "review_unresolved"]
        humans = [by for by in approvers if by != AUTO_BY]
        return humans[-1] if humans else (AUTO_BY if approvers else None)

    # ------------------------------------------------------------------ the task commit

    def _commit_task(self, plan: Path, task: sources.PlanTask, tkey: str, open_findings: list[dict],
                     round_: int = 0, gates: str | list[str] | None = None, phase: Phase | None = None) -> Outcome:
        """Tick the task's boxes and commit them with the task's work in one commit, once the gates
        hold for the tree being committed."""
        if self._committed(tkey, round_):
            return "done"
        commit_key = f"{tkey}/commit"
        self.run.refresh()
        if self.run.state["waiting"] is not None:
            return "stopped"
        if gates and not self._gates_hold(tkey, commit_key, gates):
            return "stopped"
        sources.tick_task(plan, task.n)
        subject = f"Task {task.n}: {task.title}"
        refine = current_refine(self.run, phase.name) if phase is not None and round_ else None
        if refine is not None:
            subject = refine_subject(f"Task {task.n}", task.title)
        held = self._held_paths(tkey)
        paths = None
        if held:
            changed = changed_paths(self.worktree, "HEAD^{tree}", snapshot(self.worktree))
            paths = [p for p in changed if p not in held]
        try:
            message = self._task_message(subject, tkey, open_findings, refine)
            sha = commit(self.worktree, message, self.cfg, paths=paths, expected_head=runner_head(self.run))
        except CommitRejected as exc:
            raise_hitl(self.cfg, self.run, "guard_violation", commit_key, f"Guard violation in {commit_key}: {exc.kind}: {exc}")
            return "stopped"
        except NothingToCommit:
            sha = None
        self.run.emit("task_committed", key=tkey, n=task.n, title=task.title, sha=sha, round=round_)
        if sha:
            self.run.emit("step_committed", key=tkey, sha=sha, summary=subject)
        if commit_key in self.run.state["steps"]:
            self.run.emit("step_finished", key=commit_key, status="done", summary=subject, sha=sha)
        return "done"

    def _gates_hold(self, tkey: str, commit_key: str, gates: str | list[str]) -> bool:
        """The implementer's gates hold for the tree about to be committed. They run again unless the tree
        is the one they last passed on: a human may edit before approving a stop, and a reviewer could
        edit too. A failure is a `gate_failed` stop on `<task>/commit`; approving it runs them again."""
        tree = snapshot(self.worktree)
        finished = [e for e in self.run.events.read()
                    if e["type"] == "step_finished" and e.get("key") == f"{tkey}/implement" and e.get("status") == "done"]
        if finished and finished[-1].get("tree") == tree:
            return True
        results = run_gates(self.cfg, gates, self.worktree, self.run.gate_log_dir(commit_key))
        record_results(self.run, commit_key, results, tree)
        if gates_ok(results):
            return True
        failed = [r for r in results if not (r.passed or r.skipped)]
        raise_hitl(self.cfg, self.run, "gate_failed", commit_key,
                   f"Gates failed before committing {tkey}: {', '.join(r.name for r in failed)}.",
                   extra=self._gate_report(failed))
        return False

    def _held_paths(self, tkey: str) -> set[str]:
        """Files other parked tasks changed before they parked: theirs to commit, never this task's. A park
        stops holding once its task commits after it (a task committed earlier can park in a refine round)."""
        events = self.run.events.read()
        last_commit = {e.get("key"): e["seq"] for e in events if e["type"] == "task_committed"}
        held: set[str] = set()
        for event in events:
            if event["type"] != "step_parked":
                continue
            task = str(event.get("key", "")).rsplit("/", 1)[0]
            if task != tkey and last_commit.get(task, 0) < event["seq"]:
                held.update(event.get("paths") or [])
        return held

    def _task_message(self, subject: str, tkey: str, open_findings: list[dict], refine: dict | None = None) -> str:
        parts = [subject]
        summary = str((self.run.state["steps"].get(f"{tkey}/implement") or {}).get("result") or "").strip()
        if summary:
            parts.append(summary)
        decisions = self._body_answers(tkey)
        if decisions:
            parts.append("Decisions:\n" + "\n".join(decisions))
        if open_findings:
            parts.append("Open review findings accepted by a human:\n" + "\n".join(finding_line(f) for f in open_findings))
        if refine is not None:
            parts.append(refine_note_block(refine))
        return "\n\n".join(parts)

    def _record_mode(self) -> dict[str, str]:
        return {**DEFAULT_RECORD, **(self.cfg.hitl.get("record_decisions") or {})}

    def _body_answers(self, tkey: str) -> list[str]:
        """Answers to the task's questions whose kind goes in the commit body (`ambiguity`, `review_dispute`, `boundary`)."""
        mode = self._record_mode()
        events = self.run.events.read()
        lines = []
        for answered in events:
            if answered["type"] != "hitl_answered" or not str(answered.get("key", "")).startswith(tkey + "/"):
                continue
            answers = answered.get("answers") or {}
            for q in _questions_for(events, answered):
                kind = q.get("kind", "other")
                if mode.get(kind) == "commit_body" and not answered.get("record") and answers.get(q["id"]):
                    lines.append(f"- {q['id']} ({kind}): {' '.join(str(q.get('question', '')).split())} "
                                 f"→ {answers[q['id']]}")
        return lines

    # ------------------------------------------------------------------ decisions

    def record_decisions(self, phase: Phase, key: str) -> Outcome | None:
        """Before `key` resumes with answers: record each `spec_gap` (or `--record`) answer in the spec,
        one `record_step` run and one `Decide …` commit per question. "stopped" = a human is needed."""
        record = self.cfg.record_step
        spec = self.ctx.values.get("spec") or ""
        if record is None or not spec or not (self.worktree / spec).is_file():
            return None  # no spec to write the decision into (yet): the answer reaches the step only
        events = self.run.events.read()
        answered = _pending_answer(events, key)
        if answered is None:
            return None
        answers = answered.get("answers") or {}
        mode = self._record_mode()
        for q in _questions_for(events, answered):
            text = answers.get(q["id"])
            if not text or not (answered.get("record") or mode.get(q.get("kind", "other")) == "record_step"):
                continue
            nth = sum(1 for e in events if e["type"] == "hitl_answered" and e.get("key") == key
                      and q["id"] in (e.get("answers") or {}) and e["seq"] <= answered["seq"])
            base = f"{key}/record-{q['id']}" + (f"-{nth}" if nth > 1 else "")
            if self._record_one(phase, record, base, q, text) != "done":
                return "stopped"
        return None

    def _record_one(self, phase: Phase, rstep: Step, rkey: str, question: dict, text: str) -> Outcome:
        plain = Phase(name=phase.name, agent=phase.agent, model=None, skip_if=None, steps=[])
        extra = {
            "question": " ".join(str(question.get("question", "")).split()),
            "answer": text,
            "evidence": ", ".join(question.get("evidence") or []) or "(none given)",
            "topic": question_summary(str(question.get("question", ""))),
        }
        step = dataclasses.replace(rstep, commit=False)
        outcome = self.run_agent_step(plain, step, rkey, extra=extra, role=rstep.role or "plan")
        if outcome != "done":
            return outcome
        if any(e["type"] == "decision_recorded" and e.get("key") == rkey for e in self.run.events.read()):
            return "done"
        since = self._since_step_started(rkey)
        start_tree = str(since[0].get("start_tree") or "") if since else ""
        paths = changed_paths(self.worktree, start_tree, snapshot(self.worktree)) if start_tree else []
        subject = fmt(rstep.message or "Decide {topic} in the {module} spec", {**self.ctx.values, **extra})
        message = f"{subject}\n\nQuestion: {extra['question']}\nAnswer: {text}"
        try:
            sha = commit(self.worktree, message, self.cfg, paths=paths, expected_head=runner_head(self.run))
        except CommitRejected as exc:
            raise_hitl(self.cfg, self.run, "guard_violation", rkey, f"Guard violation in {rkey}: {exc.kind}: {exc}")
            return "stopped"
        except NothingToCommit:
            sha = None
        self.run.emit("decision_recorded", key=rkey, sha=sha, question=extra["question"], answer=text)
        if sha:
            self.run.emit("step_committed", key=rkey, sha=sha, summary=subject)
        return "done"

    # ------------------------------------------------------------------ finish

    def run_finish(self, phase: Phase, step: Step, key: str) -> Outcome:
        """Bring the latest target branch in; refuse uncommitted changes; push the branch (never forced) and open a
        draft pull request against `fork_from`, the branch the run was cut from; then check it merges cleanly."""
        self.run.refresh()
        if self.run.state["waiting"] is not None:
            return "stopped"
        branch = str(self.run.state.get("branch") or "")
        target = self.cfg.project.fork_from
        opened = [e for e in self.run.events.read() if e["type"] == "pr_opened" and e.get("key") == key]
        if opened:
            link(self.run)  # a runner stopped between `pr_opened` and its link links on resume; a no-op once linked
            self.run.emit("step_finished", key=key, status="done", summary=f"draft pull request {opened[-1]['url']}")
            return "done"
        if branch in (self.cfg.project.base_branch, target):
            raise_hitl(self.cfg, self.run, "blocked", key,
                       f"{branch} is the base branch: finish pushes a run's own branch, never the pull request's target.")
            return "stopped"
        if not clean_tree(self.cfg, self.run, self.worktree, key, resume_merge=True):  # a half-done sync resumes
            return "stopped"
        if self.run_sync(phase, step, key) != "done":
            return "stopped"
        if not self.runner_commits_only(key):
            return "stopped"
        if not audit(self.cfg, self.run, self.worktree, self.ctx, key):  # on the tree to be pushed, before the push
            return "stopped"
        body = self.write_pr_body()
        try:
            push(self.worktree, branch)
            url = create_draft_pr(self.worktree, target, branch, self._pr_title(), body)
        except (GitError, GhError) as exc:
            raise_hitl(self.cfg, self.run, "blocked", key, f"Could not push {branch} or open the pull request: {exc}")
            return "stopped"
        self.run.emit("pr_opened", url=url, key=key)
        link(self.run)  # as soon as the PR exists, before a conflict or a CI stop can park the run
        if self.confirm_mergeable(phase, step, key, url) != "done":
            return "stopped"
        self.run.emit("step_finished", key=key, status="done", summary=f"draft pull request {url}")
        return "done"

    def write_pr_body(self) -> Path:
        body = self.run.dir / "pr.md"
        body.write_text(render_pr_body(self.run, plan=self.ctx.values.get("plan") or None), encoding="utf-8")
        return body

    def runner_commits_only(self, key: str) -> bool:
        """Push only what the runner committed, each message checked again; False = stopped."""
        base = str(self.run.state.get("base_sha") or "")
        try:
            foreign = foreign_commits(self.run, self.cfg, self.worktree, base) if base else ["no base commit recorded"]
        except GitError as exc:
            foreign = [f"cannot list the commits since {base[:12]}: {exc}"]
        if foreign:
            raise_hitl(self.cfg, self.run, "guard_violation", key,
                       f"Guard violation in {key}: the branch holds commits the runner will not push.",
                       extra="### Commits\n\n" + "\n".join(f"- {line}" for line in foreign))
        return not foreign

    def _pr_title(self) -> str:
        values = self.ctx.values
        subject = values.get("module") or values.get("topic") or values.get("issue") or values.get("slug")
        return f"{self.run.state.get('pipeline')}: {subject}"


# --------------------------------------------------------------------------- event helpers


def _pending_answer(events: list[dict], key: str) -> dict | None:
    """The answers given for `key` since its last agent call, if any."""
    found = None
    for event in events:
        if event["type"] == "agent_started" and event.get("step") == key:
            found = None
        elif event["type"] == "hitl_answered" and event.get("key") == key:
            found = event
    return found


def _questions_for(events: list[dict], answered: dict) -> list[dict]:
    """The questions an answer event answers: those of the last stop or park raised on its key before it."""
    questions: list[dict] = []
    for event in events:
        if event["seq"] >= answered["seq"]:
            break
        if event["type"] in ("hitl_raised", "step_parked") and event.get("key") == answered.get("key"):
            questions = list(event.get("questions") or [])
    return questions


# --------------------------------------------------------------------------- the pull request body


def _section(title: str, lines: list[str]) -> str:
    return f"## {title}\n\n" + ("\n".join(lines) if lines else "(none)")


def _gate_lines(state: dict) -> list[str]:
    lines = []
    for key, entries in state.get("gates", {}).items():
        last: dict[str, dict] = {}
        for entry in entries:
            last[str(entry.get("gate"))] = entry
        marks = [f"{gate} {'– skipped' if e.get('skipped') else '✓' if e.get('passed') else '✗'}"
                 for gate, e in last.items()]
        lines.append(f"- `{key}`: {', '.join(marks)}")
    return lines


def _skipped_lines(state: dict) -> list[str]:
    lines = [f"- phase `{name}`: {row.get('skip_reason') or 'skipped'}"
             for name, row in state.get("phases", {}).items() if row.get("status") == "skipped"]
    lines += [f"- `{key}`: {row.get('skip_reason') or 'skipped'}"
              for key, row in state.get("steps", {}).items() if row.get("status") == "skipped"]
    return lines


def _runner_decisions(events: list[dict]) -> list[str]:
    """Every question the runner answered with the asker's recommended option: step key, question id, option, so a
    reviewer of the pull request checks each one. The question, its id, the option and its label are model text, so
    each is one clean line (`clean_fact`): a newline cannot break the list and a bidi override cannot reorder it."""
    lines = []
    for event in events:
        if event["type"] != "hitl_answered" or event.get("by") != AUTO_BY:
            continue
        chosen = event.get("answers") or {}
        for question in _questions_for(events, event):
            option = chosen.get(question["id"])
            labels = [o.get("label") for o in question.get("options") or [] if o.get("key") == option]
            qid, text = clean_fact(str(question["id"])), clean_fact(str(question.get("question", "")))
            label = clean_fact(str(labels[0] if labels else option))
            lines.append(f"- `{event.get('key')}` {qid} (option {clean_fact(str(option))}): {text} → {label}")
    return lines


def _runner_settled(events: list[dict]) -> list[str]:
    return [f"- `{e.get('key')}` ({e.get('stop_kind')}): {' '.join(str(e.get('summary') or 'no summary').split())}"
            for e in events if e["type"] == "resolve_finished"]


def _sync_lines(events: list[dict]) -> list[str]:
    """What the runner brought in from the target branch: the tip, any conflicts and how they were resolved, the gates."""
    lines = []
    for e in events:
        if e["type"] != "base_synced":
            continue
        files = f"; conflicts resolved in {', '.join(f'`{p}`' for p in e['conflicts'])}" if e.get("conflicts") else ""
        gates = f"; gates: {e['gates']}" if e.get("gates") else ""
        lines.append(f"- `origin/{e.get('branch')}` at `{str(e.get('target'))[:12]}` merged as `{str(e.get('sha'))[:12]}`"
                     f"{files}{gates}")
        lines += [f"  - {' '.join(str(n).split())}" for n in e.get("notes") or []]
    return lines


def _ci_lines(events: list[dict]) -> list[str]:
    return [f"- round {e.get('round')}: {e.get('note')}" for e in events if e["type"] == "ci_round"]


def _open_findings(state: dict, events: list[dict]) -> list[str]:
    """What reviewers raised and nothing fixed: the last round of each review, and of each flagged step."""
    last: dict[str, list] = {key: list(review.get("findings") or []) for key, review in state.get("reviews", {}).items()}
    for event in events:
        if event["type"] == "step_findings":
            last[str(event.get("key"))] = list(event.get("findings") or [])
    return [f"- `{key}`: {finding_line(as_finding(f))[2:]}" for key, findings in last.items() for f in findings]


def _conformance(run: Run, events: list[dict]) -> str | None:
    """The Conformance section from the run's last audit; None when no audit ran."""
    checks = [e for e in events if e["type"] == "conformance_checked"]
    lines = render_section(checks[-1] if checks else None, run.id)
    return _section("Conformance", lines) if lines else None


def issue_line(params: dict) -> str | None:
    """`Refs #N` for an epic, `Closes #N` for any other issue, else None. It depends on `params` alone."""
    if not params.get("issue"):
        return None
    return f"{'Refs' if params.get('issue_epic') else 'Closes'} #{params['issue']}"


def render_pr_body(run: Run, plan: str | None = None) -> str:
    """The draft pull request's body: plan link, spend vs budget, gate summary, the conformance audit, skipped steps and reasons,
    effort changes with reasons, footer mismatches, refine rounds, then the attribution line."""
    run.refresh()
    state = run.state
    branch = state.get("branch") or "?"
    plan = plan or str((state.get("params") or {}).get("plan") or "")
    spend = float(state.get("spend_usd") or 0.0)
    budget, soft = state.get("budget_usd"), state.get("soft_budget_usd")
    if budget and soft:
        spend_line = f"Spend: ${spend:.2f} of ${float(soft):.2f} soft · ${float(budget):.2f} hard budget"
    else:
        spend_line = f"Spend: ${spend:.2f}" + (f" of ${float(budget):.2f} budget" if budget else " (no budget set)")
    efforts = [f"- `{c.get('key')}`: {c.get('from')} → {c.get('to')} ({c.get('reason') or 'no reason given'})"
               for c in state.get("effort_changes", [])]
    footers = [f"- after `{m.get('phase')}`: the skill footer named `{m.get('got')}`, the route went to "
               f"`{m.get('expected')}`" for m in state.get("footer_mismatches", [])]
    events = run.events.read()
    refines = [f"- `{e.get('phase')}` round {e.get('round')} ({'the runner' if e.get('by') == AUTO_BY else e.get('by')}): "
               f"{' '.join(str(e.get('note', '')).split())}" for e in events if e["type"] == "phase_rejected"]
    marked = state.get("marked_done")
    parts = [
        f"Opened by agentflow run `{run.id}` ({state.get('pipeline')}) from branch `{branch}`.",
        issue_line(state.get("params") or {}),
        f"Marked done after `{marked['phase']}` by {marked['by']}: the phases under Skipped steps did not run."
        if marked else None,
        f"Plan: `{plan}`" if plan else "Plan: none",
        spend_line,
        _section("Gates", _gate_lines(state)),
        _conformance(run, events),
        _section("Skipped steps", _skipped_lines(state)),
        _section("Effort changes", efforts),
        _section("Footer mismatches", footers),
        _section("Refine rounds", refines),
        _section("Decisions the runner took", _runner_decisions(events)),
        _section("Stops the runner settled", _runner_settled(events)),
        _section("Merge from the target branch", _sync_lines(events)) if _sync_lines(events) else None,
        _section("CI", _ci_lines(events)),
        _section("Findings left open", _open_findings(state, events)),
        PR_FOOTER,
    ]
    return "\n\n".join(part for part in parts if part) + "\n"
