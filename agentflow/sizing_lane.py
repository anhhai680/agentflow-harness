"""Sizing a step's effort in a short read-only session before its work session starts (slice ES).

`SizingLane` is a mixin of `Executor`. `size_step` builds a task card from runner facts (the step's ask, the paths
changed since the run started, the security overlay, the failing gates), asks a sizer session the checklist in
`prompts/size.md`, and records the level `sizing.decide` gives as `effort_decided`.

A sizer gets only `Read`, `Grep` and `Glob` (`AgentCall.read_only`) and answers in its final message. The backstop is
the repository's whole state: `sizer_started` records the tree, `HEAD` and a hash of `git_state` and `tree_state`,
and any difference after the call stops the run (`guard_violation`, kind `sizer_edit`). Until that state is back, a
restart is refused (`sizer_reset_needed`). Anything else that goes wrong in sizing falls back to `effort.default`
(`high` for a judgement step) and never stops, holds or reminds.

Agent text is untrusted: only the six normalized answers and a reason cut to `sizing.REASON_MAX` are kept, and
neither reaches a commit message.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

from agentflow import overlays, run_env, sizing
from agentflow.agents import Agent, AgentCall, AgentOutcome, EventCallback
from agentflow.conditions import fmt
from agentflow.flow_control import halted
from agentflow.gitops import changed_since, git_state, head, tree_state
from agentflow.guards import changed_paths, snapshot
from agentflow.hitl import raise_hitl
from agentflow.run_control import wait_for_agents

if TYPE_CHECKING:  # `lanes` imports this module for `Executor`'s bases
    from agentflow.lanes import _StepRun

SIZE_PROMPT = "size.md"
SIZER_EDIT = "sizer_edit"
CLEARS = ("effort_decided", "plan_sized")  # a sizing outcome: the sizer before it is accounted for
STATE_FIELDS = ("tree", "head", "state")


def _owner(size_key: str) -> str:
    """The step key (or plan prefix) a sizer key sizes: `build/impl/size` -> `build/impl`."""
    at = size_key.rfind(sizing.SIZE_MARK)
    return size_key[:at] if at > 0 else size_key


def _is_sizer_edit(event: dict) -> bool:
    return any(isinstance(v, dict) and v.get("kind") == SIZER_EDIT for v in event.get("violations") or [])


class SizingLane:
    """Sizes agent steps; a mixin of `Executor` (uses `cfg`, `run`, `agents`, `ctx`, `worktree`, `overrides`)."""

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
        detail = (f"the read-only sizer changed {', '.join(shown)}{more}. Put the work tree, HEAD and the git state "
                  "back as they were before it ran, then approve: the step is sized again")
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
        if any(e["type"] in (*CLEARS, "step_started") and e.get("key") == key and e["seq"] > last for e in events):
            return None
        recorded = [e for e in tripped if _is_sizer_edit(e) and isinstance(e.get("before"), dict)]
        return recorded[-1] if recorded else None

    def sizer_reset_needed(self, key: str) -> list[str]:
        """What still differs from the state a sizer of `key` (a step, or a plan prefix) started from, while its
        `sizer_edit` stop is not cleared; [] when nothing is owed."""
        trip = self._sizer_trip(key)
        if trip is None:
            return []
        try:
            return self._changes_since(trip["before"])
        except Exception:  # noqa: BLE001
            return ["unknown"]

    def _last_sizer_started(self, size_key: str) -> dict | None:
        found = [e for e in self.run.events.read() if e["type"] == "sizer_started" and e.get("key") == size_key]
        return {name: found[-1].get(name) for name in STATE_FIELDS} if found else None

    def _orphan_check(self, size_key: str) -> list[str]:
        """For a sizer whose last `sizer_started` has no outcome after it (its runner died, or the run halted), what
        differs from that start; [] when there is none. Call it after `wait_for_agents`. Never raises."""
        owner = _owner(size_key)
        events = self.run.events.read()
        started = [e for e in events if e["type"] == "sizer_started" and e.get("key") == size_key]
        if not started:
            return []
        seq = started[-1]["seq"]
        if any(e["type"] in (*CLEARS, "guard_tripped") and e.get("key") in (owner, size_key) and e["seq"] > seq
               for e in events):
            return []
        try:
            return self._changes_since(started[-1])
        except Exception:  # noqa: BLE001
            return ["unknown"]

