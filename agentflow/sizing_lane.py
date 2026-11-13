"""Sizing a step's effort in a short read-only session before its work session starts (slice ES).

`SizingLane` is a mixin of `Executor`. `size_step` builds a task card from runner facts (the step's ask, the paths
changed since the run started, the security overlay, the failing gates), asks a sizer session the checklist in
`prompts/size.md`, and records the level `sizing.decide` gives as `effort_decided`. `escalate_effort` raises a step one allowed level, once
(`effort_escalated`), on its first failed gate or when a review reopens its session; a rollover keeps the level.

A sizer gets only `Read`, `Grep` and `Glob` (`AgentCall.read_only`) and answers in its final message. The backstop is
the repository's whole state: `sizer_started` records the tree, `HEAD` and a hash of `git_state` and `tree_state`,
and any difference after the call stops the run (`guard_violation`, kind `sizer_edit`); a comparison that came back
clean is recorded as `sizer_finished`. Until that state is back, a restart is refused (`sizer_reset_needed`). A sizer
with no outcome (its runner died) is waited for and compared before anything else runs for its step or plan, sized or
not (`check_open_sizers`). The one stop is a sizer that changed the repository, or whose state cannot be compared:
anything else that goes wrong in sizing falls back to `effort.default` (`high` for a judgement step) and never stops,
holds or reminds.

`size_plan` sizes a plan's open tasks that have not started when the build phase's `foreach` starts: one plan-sizer
call (`prompts/size_plan.md`) per `sizing.PLAN_SIZE_CHUNK` task cards, on the phase's `plan`-role model. It records
an `effort_decided` (`plan_sizer` or `fallback`) for each task's implement step and its review, then a `plan_sized`
with the plan's digest when no task fell back, so ticking boxes sizes nothing again, an edit sizes only the tasks
that have not started, and a fallback is tried again on the next entry. The sizer is asked only for the tasks with no
plan-sizer answer for their current text (`task_digest`): one that fell back, a new one or an edited one. A plan
sizer that changed the repository stops build on the plan's prefix, the same way as a step's sizer.

Agent text is untrusted: only the six normalized answers and a reason cut to `sizing.REASON_MAX` are kept, and
neither reaches a commit message.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from typing import TYPE_CHECKING, Literal

from agentflow import overlays, run_env, safeio, sizing, sources
from agentflow.agents import Agent, AgentCall, AgentOutcome, EventCallback
from agentflow.conditions import fmt
from agentflow.config import Phase, Step, resolve_model
from agentflow.flow_control import halted
from agentflow.gitops import changed_since, git_state, head, tree_state
from agentflow.guards import changed_paths, snapshot
from agentflow.hitl import raise_hitl
from agentflow.run_control import wait_for_agents

if TYPE_CHECKING:  # `lanes` imports this module for `Executor`'s bases
    from pathlib import Path

    from agentflow.lanes import _StepRun

SIZE_PROMPT = "size.md"
PLAN_SIZE_PROMPT = "size_plan.md"
LINE_COUNT_MAX_BYTES = 2 * 1024 * 1024  # a named file larger than this is shown as unreadable
SIZER_EDIT = "sizer_edit"
FINISHED = "sizer_finished"  # a sizer whose comparison came back clean
CLEARS = ("effort_decided", "plan_sized")  # a sizing outcome: the sizer before it is accounted for
STATE_FIELDS = ("tree", "head", "state")


def _owner(size_key: str) -> str:
    """The step key (or plan prefix) a sizer key sizes: `build/impl/size` -> `build/impl`."""
    at = size_key.rfind(sizing.SIZE_MARK)
    return size_key[:at] if at > 0 else size_key


def _sized_by(event: dict) -> str | None:
    """The plan-sizer key an `effort_decided` came from (`<prefix>/size-<i>`), or None."""
    found = event.get("size_key") if event["type"] == "effort_decided" else None
    return found if isinstance(found, str) else None


def _finished(event: dict) -> str | None:
    """The sizer key a `sizer_finished` names, or None."""
    found = event.get("key") if event["type"] == FINISHED else None
    return found if isinstance(found, str) else None


def _is_sizer_edit(event: dict) -> bool:
    return any(isinstance(v, dict) and v.get("kind") == SIZER_EDIT for v in event.get("violations") or [])


class SizingLane:
    """Sizes agent steps and plans; a mixin of `Executor` (uses `cfg`, `run`, `agents`, `ctx`, `worktree`, `overrides`,
    `dry_run`, `_under_budget`, `_waiting_on`, and `FeatureLane`'s `_refine_round` and `_batch_of`)."""

    # -- one step

    def size_step(self, sr: _StepRun, failing_gates: list[str]) -> str | None:
        """Size `sr`'s step, record `effort_decided` and return the level; None when the run stopped (a sizer changed
        the repository, or a human paused or ended the run). Any other failure falls back to the default."""
        from agentflow.lanes import DEFAULT_TEMPLATE, _template

        judgement = sizing.is_judgement(sr.phase.name, sr.role)
        policy = self.cfg.effort
        size_key = sr.key + sizing.SIZE_MARK
        model: str | None = sr.model
        files, security = 0, False
        orphan_checked = stopping = False
        try:
            # A sizer left behind by a runner that died: wait for it, then compare with what it started from.
            wait_for_agents(self.run, self.cfg, size_key, sizing.SIZE_TIMEOUT_S)
            left = self._orphan_check(size_key)
            orphan_checked = True
            if left:
                stopping = True
                self._sizer_stop(sr.key, left, self._last_sizer_started(size_key) or {})
                return None

            if not judgement:  # a judgement step is never sized by the `mechanical` model
                spec = self.cfg.agents.get(sr.agent_name)
                model = (spec.models.get("mechanical") if spec is not None else None) or sr.model
            effort = sizing.sizer_effort(judgement, policy.allowed)
            ask = fmt(_template(self.cfg, sr.step.prompt or DEFAULT_TEMPLATE), self._values(sr))
            base = self.run.state.get("base_sha")
            changed = changed_since(self.worktree, base) if base else []
            files = len(changed)
            security = overlays.security_path(self.cfg, self.ctx)
            card = sizing.task_card(key=sr.key, phase=sr.phase.name, role=sr.role, ask=ask, files=[], changed=changed,
                                    security=security, failing_gates=list(failing_gates))
            prompt = fmt(_template(self.cfg, SIZE_PROMPT), {"task_card": card})

            parsed, edited = self._sizer_call(size_key, prompt, sr.agent, str(model), effort, sr.phase.name, sr.role,
                                              sizing.SIZE_MAX_TURNS, sizing.SIZE_TIMEOUT_S)
            if edited:
                stopping = True
                self._sizer_stop(sr.key, edited, self._last_sizer_started(size_key) or {})
                return None
            if halted(self.run.refresh()["status"]):
                return None

            raw = parsed if isinstance(parsed, dict) else {}
            answers = sizing.parse_answers(raw.get("answers"))
            level, fell_back = sizing.decide(answers, files, security, judgement, policy)
            source = "fallback" if fell_back else ("judge" if judgement else "sizer")
            self.run.emit("effort_decided", key=sr.key, level=level, source=source, answers=answers,
                          reason=" ".join(sizing.reason_text(raw.get("reason")).split()), sizer_model=model,
                          files=files, security=security)
            return level
        except Exception:  # noqa: BLE001 - a sizing failure never stops a run; only a sizer's edit does
            if stopping:
                return None
            if not orphan_checked:  # never fall back past a sizer that may have changed the repository
                left = self._orphan_check(size_key)
                if left:
                    self._sizer_stop(sr.key, left, self._last_sizer_started(size_key) or {})
                    return None
            level, _ = sizing.decide(None, files, security, judgement, policy)
            self.run.emit("effort_decided", key=sr.key, level=level, source="fallback", answers=None, reason="",
                          sizer_model=model, files=files, security=security)
            try:
                if halted(self.run.refresh()["status"]):
                    return None
            except Exception:  # noqa: BLE001
                pass
            return level

    def escalate_effort(self, sr: _StepRun, trigger: str) -> None:
        """Raise `sr`'s effort one allowed level (`effort_escalated`, `trigger` `gate` or `review`), once per step:
        nothing with sizing off, after an earlier escalation of the step, or at the top allowed level. Anything that
        goes wrong leaves the level as it is: an escalation never stops a run."""
        if not self.cfg.effort.sizing:
            return
        try:
            if any(e["type"] == "effort_escalated" and e.get("key") == sr.key for e in self.run.events.read()):
                return
            to = sizing.escalate(sr.effort, self.cfg.effort.allowed)
            if to is None:
                return
            self.run.emit("effort_escalated", key=sr.key, trigger=trigger, **{"from": sr.effort, "to": to})
        except Exception:  # noqa: BLE001 - e.g. a level `effort.allowed` does not hold
            return
        sr.effort = to

    # -- a plan's tasks

    def size_plan(self, phase: Phase, step: Step, plan: Path, prefix: str, small: dict) -> Literal["done", "stopped"]:
        """Size the open tasks of `plan` that have not started, and their reviews, before `run_foreach` walks it.

        "stopped" when a plan sizer changed the repository (now or before a restart that did not put it back), or a
        human paused or ended the run; else "done". Any other failure falls back per task and never stops."""
        if self.dry_run:
            return "done"
        self.run.refresh()
        # A plan sizer changed the repository: nothing runs, sized or not, until it is back as the sizer found it.
        left = self.sizer_reset_needed(prefix)
        if left:
            if not self._waiting_on(prefix):
                shown = ", ".join(left[:sizing.CHANGED_SHOWN])
                detail = f"reset {shown} first"
                self.run.emit("guard_tripped", key=prefix, violations=[{"kind": SIZER_EDIT, "detail": detail}])
                raise_hitl(self.cfg, self.run, "guard_violation", prefix,
                           f"Guard violation in {prefix}: {SIZER_EDIT}: {detail}")
            return "stopped"
        if not self.check_open_sizers(prefix):  # before any skip: a sizer left open may have changed the repository
            return "stopped"
        entry_trip = self._sizer_trip(prefix)
        resized = entry_trip is not None  # cleared: the plan is sized again, whatever was recorded
        policy = self.cfg.effort
        if not policy.sizing or self._refine_round(phase) or not self._under_budget():
            if resized:  # settle the cleared stop, so later work is never read as the sizer's edit
                self.run.emit("plan_sized", key=prefix, digest=None, tasks=[])
            return "done"

        todo: list[sources.PlanTask] = []
        facts: dict[int, tuple[int, bool]] = {}
        answered: dict[int, tuple[dict | None, str, str]] = {}  # task -> (answers, reason, the sizer key)
        decided: set[str] = set()  # the step keys given an `effort_decided` here
        model: str | None = None
        stopping = False
        try:
            tasks = sources.parse_plan(plan)
            digest = sizing.plan_digest((t.n, t.title, t.text) for t in tasks)
            recorded = [e for e in self.run.events.read() if e["type"] == "plan_sized" and e.get("key") == prefix]
            if recorded and recorded[-1].get("digest") == digest and not resized:
                return "done"
            steps = self.run.state["steps"]
            todo = [t for t in tasks
                    if t.open and not (steps.get(f"{prefix}/task-{t.n}/implement") or {}).get("session_id")]
            if todo and (self.run.state["waiting"] is not None or halted(self.run.state["status"])):
                return "stopped" if halted(self.run.state["status"]) else "done"  # a stop is the lane's to handle

            role = step.role or "build"
            judgement = sizing.is_judgement(phase.name, role)
            digests = {t.n: sizing.plan_digest([(t.n, t.title, t.text)]) for t in todo}
            kept = self._kept_answers(prefix, todo, digests)  # a good answer is never asked for again
            answered.update({n: entry for n, (entry, _model) in kept.items()})
            ask = [t for t in todo if t.n not in answered]
            cards: dict[int, str] = {}
            for task in todo:
                named = sources.task_files(task)
                files = [(path, self._line_count(path)) for path in named]
                security = overlays.touches_security(self.cfg, named)
                facts[task.n] = (len(files), security)
                if task.n in answered:
                    continue
                card = sizing.task_card(key=f"{prefix}/task-{task.n}/implement", phase=phase.name, role=role,
                                        ask=f"Task {task.n}: {task.title}\n\n{task.text}", files=files, changed=[],
                                        security=security, failing_gates=[])
                cards[task.n] = f"== Task {task.n} ==\n\n{card}"

            size = sizing.PLAN_SIZE_CHUNK
            for i, start in enumerate(range(0, len(ask), size), 1):
                chunk = ask[start:start + size]
                size_key = f"{prefix}{sizing.SIZE_MARK}-{i}"
                replies, stopped, model = self._plan_chunk(phase, prefix, size_key, [cards[t.n] for t in chunk], model)
                if stopped:
                    stopping = True
                    return "stopped"
                for task in chunk:
                    entry = (replies or {}).get(str(task.n))
                    entry = entry if isinstance(entry, dict) else {}
                    answers = sizing.parse_answers(entry.get("answers"))
                    reason = " ".join(sizing.reason_text(entry.get("reason")).split())
                    answered[task.n] = (answers, reason, size_key)

            implement: dict[int, str] = {}
            review: dict[int, str] = {}
            for task in todo:
                answers = answered[task.n][0]
                files, security = facts[task.n]
                implement[task.n] = sizing.decide(answers, files, security, judgement, policy)[0]
                review[task.n] = sizing.decide(answers, files, security, True, policy)[0]
            batch_of = self._batch_of or {}
            for n in implement:
                members = [m for m in batch_of.get(n) or [] if m in implement]
                if members:  # one session for the batch, and one review on its last member
                    implement[n] = sizing.highest(implement[m] for m in members)
                    review[n] = sizing.highest(review[m] for m in members)
            if implement and small.get("active") and small.get("shared_session"):  # one session for the whole plan
                top = sizing.highest(implement.values())
                implement = dict.fromkeys(implement, top)
            has_review = step.review is not None and (not small.get("active") or bool(small.get("review")))

            fell_back = False
            for task in todo:
                answers, reason, size_key = answered[task.n]
                files, security = facts[task.n]
                source = "plan_sizer" if answers is not None else "fallback"
                fell_back = fell_back or answers is None
                keys = [("implement", implement[task.n])]
                batch = batch_of.get(task.n)
                if has_review and (not batch or batch[-1] == task.n):
                    keys.append(("review", review[task.n]))
                for part, level in keys:
                    self.run.emit("effort_decided", key=f"{prefix}/task-{task.n}/{part}", level=level, source=source,
                                  answers=answers, reason=reason, files=files, security=security, size_key=size_key,
                                  sizer_model=kept[task.n][1] if task.n in kept else model,
                                  task_digest=digests[task.n])
                    decided.add(f"{prefix}/task-{task.n}/{part}")
            if not fell_back:  # a fallback is tried again on the next entry; its decisions still settle a cleared stop
                self.run.emit("plan_sized", key=prefix, digest=digest, tasks=[t.n for t in todo])
            return "done"
        except Exception:  # noqa: BLE001 - a sizing failure never stops a run; only a sizer's edit does
            if stopping or self._new_trip(prefix, entry_trip):
                return "stopped"
            self._plan_fallback(phase, step, prefix, todo, facts, answered, decided, model, small, resized)
            return "done"

    def _kept_answers(self, prefix: str, todo: list[sources.PlanTask],
                      digests: dict[int, str]) -> dict[int, tuple[tuple[dict | None, str, str], object]]:
        """The tasks of `todo` whose implement step's last `effort_decided` is a plan sizer's answer for the task as
        it reads now (`task_digest`): `{task: ((answers, reason, the sizer key), the sizer's model)}`. Only a task
        with none, a fallback, or a changed text is asked again, so a retried plan sizer never asks for a good answer
        twice."""
        last: dict[str, dict] = {}
        for event in self.run.events.read():
            if event["type"] == "effort_decided" and isinstance(event.get("key"), str):
                last[event["key"]] = event
        kept: dict[int, tuple[tuple[dict | None, str, str], object]] = {}
        for task in todo:
            found = last.get(f"{prefix}/task-{task.n}/implement") or {}
            answers = sizing.parse_answers(found.get("answers"))
            size_key = found.get("size_key")
            if (found.get("source") == "plan_sizer" and answers is not None and isinstance(size_key, str)
                    and found.get("task_digest") == digests[task.n]):
                reason = " ".join(sizing.reason_text(found.get("reason")).split())
                kept[task.n] = ((answers, reason, size_key), found.get("sizer_model"))
        return kept

    def _new_trip(self, prefix: str, entry_trip: dict | None) -> bool:
        """Whether a plan sizer of `prefix` was found to have changed the repository since `size_plan` began (a
        `sizer_edit` stop it recorded, even one whose inbox write then failed), so no fallback may clear it."""
        try:
            now = self._sizer_trip(prefix)
        except Exception:  # noqa: BLE001 - the events cannot be read: neither can a fallback be recorded
            return False
        return now is not None and (entry_trip is None or now.get("seq") != entry_trip.get("seq"))

    def _plan_fallback(self, phase: Phase, step: Step, prefix: str, todo: list[sources.PlanTask],
                       facts: dict[int, tuple[int, bool]], answered: dict[int, tuple[dict | None, str, str]],
                       decided: set[str], model: str | None, small: dict, resized: bool) -> None:
        """After a failure in `size_plan` itself: a `fallback` decision for each step of `todo` not decided yet, with
        the key of the chunk sizer its card was (or would have been) in, and no `plan_sized`, so the next entry sizes
        the plan again. A cleared stop with no decision to settle it gets `plan_sized` with no digest, as with sizing
        off. Never raises: a step left undecided is sized on its own when it starts."""
        try:
            policy = self.cfg.effort
            judgement = sizing.is_judgement(phase.name, step.role or "build")
            implement = sizing.decide(None, 0, False, judgement, policy)[0]
            review = sizing.decide(None, 0, False, True, policy)[0]
            has_review = step.review is not None and (not small.get("active") or bool(small.get("review")))
            batch_of = self._batch_of or {}
            for i, task in enumerate(todo):
                size_key = answered[task.n][2] if task.n in answered else \
                    f"{prefix}{sizing.SIZE_MARK}-{i // sizing.PLAN_SIZE_CHUNK + 1}"
                files, security = facts.get(task.n, (0, False))
                keys = [("implement", implement)]
                batch = batch_of.get(task.n)
                if has_review and (not batch or batch[-1] == task.n):
                    keys.append(("review", review))
                for part, level in keys:
                    key = f"{prefix}/task-{task.n}/{part}"
                    if key in decided:
                        continue
                    self.run.emit("effort_decided", key=key, level=level, source="fallback", answers=None, reason="",
                                  sizer_model=model, files=files, security=security, size_key=size_key)
                    decided.add(key)
            if resized and not decided:  # settle the cleared stop, so later work is never read as the sizer's edit
                self.run.emit("plan_sized", key=prefix, digest=None, tasks=[])
        except Exception:  # noqa: BLE001
            pass

    def _plan_chunk(self, phase: Phase, prefix: str, size_key: str, cards: list[str],
                    model: str | None) -> tuple[dict | None, bool, str | None]:
        """One plan-sizer call over `cards`: `(its per-task replies or None, whether the run stopped, the model)`.
        Anything that goes wrong gives no replies (each task falls back), unless a sizer changed the repository."""
        from agentflow.lanes import _template

        orphan_checked = stopping = False
        try:
            wait_for_agents(self.run, self.cfg, size_key, sizing.PLAN_SIZE_TIMEOUT_S)
            left = self._orphan_check(size_key)
            orphan_checked = True
            if left:
                stopping = True
                self._sizer_stop(prefix, left, self._last_sizer_started(size_key) or {})
                return None, True, model
            if halted(self.run.refresh()["status"]):
                return None, True, model
            agent_name = phase.agent or self.cfg.defaults.agent
            agent = self.agents[agent_name]
            # The phase's `plan`-role model, never the foreach step's own `model:`; `--model <phase>=…` still wins.
            model, _source = resolve_model(self.cfg, phase, Step(id="size", type="agent", role="plan"), "plan",
                                           self.overrides)
            effort = sizing.sizer_effort(True, self.cfg.effort.allowed)
            prompt = fmt(_template(self.cfg, PLAN_SIZE_PROMPT), {"task_cards": "\n\n".join(cards)})
            parsed, edited = self._sizer_call(size_key, prompt, agent, str(model), effort, phase.name, "plan",
                                              sizing.PLAN_SIZE_MAX_TURNS, sizing.PLAN_SIZE_TIMEOUT_S)
            if edited:
                stopping = True
                self._sizer_stop(prefix, edited, self._last_sizer_started(size_key) or {})
                return None, True, model
            if halted(self.run.refresh()["status"]):
                return None, True, model
            replies = parsed.get("tasks") if isinstance(parsed, dict) else None
            return (replies if isinstance(replies, dict) else None), False, model
        except Exception:  # noqa: BLE001 - a sizing failure never stops a run; only a sizer's edit does
            if stopping:
                return None, True, model
            if not orphan_checked:  # never fall back past a sizer that may have changed the repository
                left = self._orphan_check(size_key)
                if left:
                    try:
                        self._sizer_stop(prefix, left, self._last_sizer_started(size_key) or {})
                    except Exception:  # noqa: BLE001 - fail closed: the next entry finds the edit again and stops
                        pass
                    return None, True, model
            try:
                if halted(self.run.refresh()["status"]):
                    return None, True, model
            except Exception:  # noqa: BLE001
                pass
            return None, False, model

    def _line_count(self, rel: str) -> int | None:
        """The lines of the regular file `rel` names in the worktree, for a task card; None for a path that leaves the
        worktree once resolved, a symlink, anything but a regular file, a file over `LINE_COUNT_MAX_BYTES`, or one that
        cannot be read. The path comes from plan text, so it is never trusted to stay inside."""
        try:
            root = self.worktree.resolve()
            path = self.worktree / rel
            if not path.resolve().is_relative_to(root):
                return None
            info = os.lstat(path)
            if not stat.S_ISREG(info.st_mode) or info.st_size > LINE_COUNT_MAX_BYTES:
                return None
            data = safeio.read_bytes(path, LINE_COUNT_MAX_BYTES)  # no symlink, still a regular file, still small
        except (OSError, ValueError, RuntimeError):
            return None
        return None if data is None else data.count(b"\n")

    # -- one sizer session

    def _repo_state(self) -> dict:
        """What a sizer must leave as it found it: the work tree, `HEAD`, and a hash of the git state and the checkout's
        state (ignored and untracked paths, index flags) that the tree snapshot does not see."""
        worktree = self.worktree
        shared = git_state(worktree, self.run.state.get("branch"))  # other branches' keys: other runs may write them
        state = hashlib.sha256(json.dumps([shared, tree_state(worktree)], sort_keys=True, default=str).encode())
        return {"tree": snapshot(worktree), "head": head(worktree), "state": state.hexdigest()}

    def _changes_since(self, before: dict) -> list[str]:
        """What differs between `before` (a `_repo_state()`) and now: the changed paths, `HEAD`, or `git state` when
        nothing else explains the difference. Raises when the state cannot be read."""
        now = self._repo_state()
        out: list[str] = []
        if now["tree"] != before.get("tree"):
            try:
                out += changed_paths(self.worktree, str(before.get("tree")), now["tree"])
            except Exception:  # noqa: BLE001 - a tree object that is gone: the tree still differs
                pass
            out = out or ["work tree"]
        if now["head"] != before.get("head"):
            out.append("HEAD")
        if now["state"] != before.get("state") and not out:
            out.append("git state")
        return out

    def _sizer_call(self, key: str, prompt: str, agent: Agent, model: str, effort: str, phase: str, role: str,
                    turns: int, timeout_s: int) -> tuple[dict | None, list[str]]:
        """One fresh read-only session: its parsed answer (None when it gave none) and what it changed. Never holds,
        reminds or stops: an interruption, a timeout or an exception gives no answer."""
        before = self._repo_state()
        self.run.emit("sizer_started", key=key, **before)
        call = AgentCall(
            prompt=prompt, model=model, effort=effort, cwd=self.worktree, run_id=self.run.id, step_key=key,
            result_path=self.run.agent_result_path(key), transcript_path=self.run.transcript_path(key, 1),
            add_dirs=[], runs_root=self.run.dir.parent, timeout_s=timeout_s,
            resume_session=None,  # always fresh: a resumed Codex session keeps its own sandbox
            agent_profile=None, context_window=self.context_window_for(key, model), env=self.cfg.defaults.env,
            env_base=run_env.base(self.cfg), forbidden_paths=list(self.cfg.guardrails.forbidden_paths),
            max_turns=turns, read_only=True,
        )
        seen: dict[str, object] = {"pid": None}

        def on_event(name: str, payload: dict) -> None:
            data = {**payload, "sizer": True}  # never a step of its own (`state._on_agent_started`)
            if name == "agent_started":
                seen["pid"] = data.get("pid")
                data.update(sizing=True, phase=phase, role=role)
            elif name == "agent_session":
                data.update(key=key, pid=seen["pid"])
            elif name == "agent_exited":
                data["key"] = key
            self.run.emit(name, **data)

        outcome: AgentOutcome | None = None
        changed = ["unknown"]
        try:
            outcome = self._run_sizer(agent, call, on_event)
        except Exception:  # noqa: BLE001 - no answer; what it changed is still checked below
            outcome = None
        finally:
            try:
                changed = self._changes_since(before)
            except Exception:  # noqa: BLE001 - what cannot be compared cannot be vouched for
                changed = ["unknown"]
        if not changed:  # accounted for now: a pause or a crash before its decision never leaves it open
            self._settle(key)
        if outcome is None or outcome.timed_out or outcome.interruption is not None:
            return None, changed
        try:
            return sizing.parse_result(outcome.final_text or ""), changed
        except Exception:  # noqa: BLE001 - e.g. JSON nested too deep
            return None, changed

    def _run_sizer(self, agent: Agent, call: AgentCall, on_event: EventCallback) -> AgentOutcome:
        """The sizer's agent call: a seam for tests (`StepControl._run_agent` is another method)."""
        return agent.run(call, on_event)

    # -- a sizer that changed the repository

    def _sizer_stop(self, key: str, changed: list[str], before: dict) -> None:
        """Stop the run for a human: the step stays unsized (no `step_finished`), and its restart is refused until the
        repository is back to `before` (`sizer_reset_needed`)."""
        shown = changed[:sizing.CHANGED_SHOWN]
        more = f" and {len(changed) - len(shown)} more" if len(changed) > len(shown) else ""
        if all(before.get(name) is None for name in STATE_FIELDS):  # no `sizer_started` on record
            detail = (f"the read-only sizer changed {', '.join(shown)}{more}, and no state from before it ran was "
                      "recorded, so there is nothing to restore: cancel the run and run it again")
        else:
            detail = (f"the read-only sizer changed {', '.join(shown)}{more}. Put the work tree, HEAD and the git "
                      "state back as they were before it ran, then approve: the step is sized again")
        self.run.emit("guard_tripped", key=key, violations=[{"kind": SIZER_EDIT, "detail": detail}],
                      before={name: before.get(name) for name in STATE_FIELDS})
        raise_hitl(self.cfg, self.run, "guard_violation", key, f"Guard violation in {key}: {SIZER_EDIT}: {detail}")

    def _sizer_trip(self, key: str) -> dict | None:
        """The `guard_tripped` holding the state to restore for `key`: when its last `guard_tripped` is a `sizer_edit`
        that no later `effort_decided` or `plan_sized` cleared, nor a `step_started` (a step starts only after this
        check passed, and from then on the tree holds its own work). Else None."""
        events = self.run.events.read()
        tripped = [e for e in events if e["type"] == "guard_tripped" and e.get("key") == key]
        if not tripped or not _is_sizer_edit(tripped[-1]):
            return None
        last = tripped[-1]["seq"]
        if any(e["seq"] > last and ((e["type"] in (*CLEARS, "step_started") and e.get("key") == key)
                                    or _owner(_sized_by(e) or "") == key) for e in events):
            return None
        recorded = [e for e in tripped if _is_sizer_edit(e) and isinstance(e.get("before"), dict)]
        return recorded[-1] if recorded else None

    def sizer_reset_needed(self, key: str) -> list[str]:
        """What still differs from the state a sizer of `key` (a step, or a plan prefix) started from, while its
        `sizer_edit` stop is not cleared; [] when nothing is owed. A sizer of `key` that finished clean after the stop
        (`sizer_finished`) started only once this check had found the state back, so nothing is owed after it, even
        when the step is still to be sized (a pause in that sizer)."""
        trip = self._sizer_trip(key)
        if trip is None:
            return []
        if any(e["seq"] > trip["seq"] and _owner(_finished(e) or "") == key for e in self.run.events.read()):
            return []
        try:
            return self._changes_since(trip["before"])
        except Exception:  # noqa: BLE001
            return ["unknown"]

    def _last_sizer_started(self, size_key: str) -> dict | None:
        found = [e for e in self.run.events.read() if e["type"] == "sizer_started" and e.get("key") == size_key]
        return {name: found[-1].get(name) for name in STATE_FIELDS} if found else None

    def _open_start(self, size_key: str) -> dict | None:
        """The last `sizer_started` of `size_key` when no outcome follows it (its runner died before comparing), else
        None. An outcome is a `sizer_finished`, a decision or a `plan_sized` for it or its owner, a decision that names
        it (`size_key`), or a `guard_tripped` of its owner."""
        owner = _owner(size_key)
        events = self.run.events.read()
        started = [e for e in events if e["type"] == "sizer_started" and e.get("key") == size_key]
        if not started:
            return None
        seq = started[-1]["seq"]
        if any(e["seq"] > seq and ((e["type"] in (*CLEARS, "guard_tripped") and e.get("key") in (owner, size_key))
                                   or size_key in (_sized_by(e), _finished(e))) for e in events):
            return None
        return started[-1]

    def _orphan_check(self, size_key: str) -> list[str]:
        """For a sizer left open (`_open_start`), what differs from its start; [] when there is none, and then it is
        settled (`sizer_finished`), so a later change is never read as its own. Call it after `wait_for_agents`.
        Never raises: what cannot be compared is `["unknown"]`."""
        try:
            started = self._open_start(size_key)
            if started is None:
                return []
            left = self._changes_since(started)
        except Exception:  # noqa: BLE001
            return ["unknown"]
        if not left:
            self._settle(size_key)
        return left

    def _settle(self, size_key: str) -> None:
        """Record that the sizer `size_key` left the repository as it found it. A failure leaves it open: it is
        compared again, from the same start, on the next entry."""
        try:
            self.run.emit(FINISHED, key=size_key)
        except Exception:  # noqa: BLE001
            pass

    def check_open_sizers(self, owner: str) -> bool:
        """Before anything runs for `owner` (a step key or a plan prefix), sized or not: wait for each sizer of it left
        open (`_open_start`) and compare it with the state it started from. False when one changed the repository or
        cannot be compared: the run is stopped (`sizer_edit`), even when recording the stop fails, since the open
        sizer is found again on the next entry. True otherwise, with each clean one settled. Not for a dry run."""
        if self.dry_run:
            return True
        keys = dict.fromkeys(e["key"] for e in self.run.events.read()
                             if e["type"] == "sizer_started" and isinstance(e.get("key"), str)
                             and sizing.is_sizer_key(e["key"]) and _owner(e["key"]) == owner)
        for size_key in keys:
            try:
                if self._open_start(size_key) is None:
                    continue
            except Exception:  # noqa: BLE001 - compared below, which fails closed
                pass
            timeout = sizing.SIZE_TIMEOUT_S if size_key == owner + sizing.SIZE_MARK else sizing.PLAN_SIZE_TIMEOUT_S
            try:
                wait_for_agents(self.run, self.cfg, size_key, timeout)
            except Exception:  # noqa: BLE001 - compared all the same
                pass
            left = self._orphan_check(size_key)
            if left:
                try:
                    self._sizer_stop(owner, left, self._last_sizer_started(size_key) or {})
                except Exception:  # noqa: BLE001 - fail closed: the next entry finds it again and stops
                    pass
                return False
        return True

