"""Waves: the tasks of one `**Parallel group:**` that the runner proves independent, built in one session (slice T5).

`WaveLane` is mixed into `lanes.Executor` beside `feature_lane.FeatureLane`, and relies on the same attributes. While
`waves_on` holds, the plan walk (`FeatureLane._plan_pass`) asks `wave_at` before it builds a task. A wave goes through
these stages, and each first checks its own event, so a walk that comes back to a wave picks it up where it stopped:

1. `wave_started` (`<prefix>/wave-<n>`): the members `wave.form_wave` lets through, with the start `HEAD` and tree, and
   an `effort_decided` (`source: "wave"`) at the highest of the members' levels. A candidate it turns away is
   `parallel_demoted` (once per task and reason) and is built alone, as with no group.
2. One card per member in the run's agent workspace, `wave/wave-<n>/task-<m>.md`: the member's own task prompt as a fresh
   session would get it, with no result contract, then `_wave_member.md`'s rules (its allowed files among them).
3. `<W>/implement`: one session on `task_wave.md` with no gates and no sizing, which dispatches one sub-agent per card
   in one message and reports their work in its result's `tasks`.
4. Attribution (`wave_attributed`): the tree goes into the shelf `shelf_ref(run, W)` first, then a changed path no member
   owns goes back to the wave's start (`scope_violation`), and so do the files of a member that reported `blocked` or
   `needs_human`, sent no report or strayed. That member is set aside (`parallel_demoted`) and built alone.
5. The build gates, once, on the combined tree, keyed `<W>/implement`. A red result reopens the wave session with the
   gate report (`step_reopened`, reason `gates`) so its own fix loop runs, and the attribution runs again after it.
6. Projection: each kept member gets a `step_started` and a `step_finished` (with `wave`) on its own `<task>/implement`,
   so the per-task code (its review, parking, the commit) runs on it as on a task built alone. Its commit takes the
   wave's gate pass (`wave_gated`), and `FeatureLane._held_paths` holds the other members' files (`wave_held`), so the
   commit holds only that task's work.
7. Each kept member through `FeatureLane._run_task` in plan order, then `wave_finished`.

A stop of the wave session stays the run's wait for now, and a person is told of it once (`_wave_stopped`); so does a
stray the fix loop left (`_wave_failed`).
"""

from __future__ import annotations

import dataclasses
import json
import subprocess
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from agentflow import overlays, pack, park, safeio, sizing, sources, wave
from agentflow.conditions import Ctx, fmt
from agentflow.config import Config, Phase, Step, resolve_model
from agentflow.feature_lane import TASK_N, under, wait_id
from agentflow.gates import GateResult, failure_report, record_results, run_gates_reusing
from agentflow.gitops import GitError, commit_at, head, restore_paths, shelf_ref, shelve
from agentflow.guards import changed_paths, snapshot
from agentflow.hitl import clean_fact, raise_hitl

if TYPE_CHECKING:
    from agentflow.state import Run

WAVE_PROMPT = "task_wave.md"
MEMBER_RULES = "_wave_member.md"
WAVE_SKILL = "agentflow-build"  # `skills.map.build_wave` when a pack's map has none
WaveOutcome = Literal["none", "ran", "stopped"]
SET_ASIDE = ("blocked", "needs_human")  # a member that reports one of these is built alone
CARD_RESULT = "none: a wave member writes no result file (see the rules at the end of this card)"
FIX_NOTE = ("The runner ran the build gates once on the wave's combined tree, and they failed:\n\n{report}\n\n"
            "Do not edit any file yourself: dispatch one fix `Task` per member the failure points to, with the same "
            "rules and description (`Task <n>: <title>`, the prompt naming its card, `model` left out), and tell it "
            "what failed. Wait for them all, then write your result file again with the whole `tasks` array.")


class WaveLane:
    """`waves_on`, `wave_at`, `wave_held`, `wave_owned` and `wave_gated` for `lanes.Executor`; relies on its `cfg`, `run`,
    `ctx`, `worktree`, `overrides`, `handled_waits`, `run_agent_step` and `_step_extra`, and on `FeatureLane`'s
    `_impl_step`, `_task_extra`, `_task_payload`, `_committed`, `_parked`, `_parked_numbers`, `_held_paths`,
    `_sized_answers`, `_refine_round`, `_task_outcome`, `_run_task`, `_unshelve`, `_tell_once` and `_waiting_on`."""

    cfg: Config
    run: Run
    ctx: Ctx
    worktree: Path
    overrides: dict[str, str]
    handled_waits: set[tuple[str, str]]

    def waves_on(self, phase: Phase, step: Step, small: dict) -> bool:
        """Whether this walk may build waves: `parallel.max_subagents` above 1, the phase's agent is `claude` (only it
        has sub-agents), it is not a refine round and no small plan's session is active."""
        return (self.cfg.parallel.max_subagents > 1 and (phase.agent or self.cfg.defaults.agent) == "claude"
                and not self._refine_round(phase) and not small.get("active"))

    def wave_at(self, phase: Phase, step: Step, plan: Path, rel: str, prefix: str, task: sources.PlanTask,
                tasks: list[sources.PlanTask], held: set[int], small: dict) -> WaveOutcome:
        """Build the wave that holds `task`: the open one, else one formed at it. "none": no wave of two or more holds
        it (it is built alone); "ran": every kept member is committed or parked; "stopped": a human is needed."""
        started = self._open_wave(prefix, task.n)
        if started is None:
            started = self._form_wave(phase, step, prefix, task, tasks, held, small)
            if started is None:
                return "none"
        return self._run_wave(phase, step, plan, rel, prefix, started, tasks, small)

    # ------------------------------------------------------------------ what the per-task code asks

    def wave_held(self, tkey: str) -> set[str]:
        """The paths the other kept members of `tkey`'s last attributed wave own, for those not committed since: theirs
        to commit, never `tkey`'s. Empty for a task in no wave."""
        events = self.run.events.read()
        last, n = self._last_attribution(events, tkey)
        if last is None:
            return set()
        out = last.get("out") or {}
        since = {e.get("key") for e in events if e["type"] == "task_committed" and e["seq"] > last["seq"]}
        prefix = tkey.rsplit("/task-", 1)[0]
        held: set[str] = set()
        for member, paths in (last.get("owners") or {}).items():
            if member != str(n) and member not in out and f"{prefix}/task-{member}" not in since:
                held.update(str(p) for p in paths or [])
        return held

    def wave_owned(self, tkey: str) -> list[str] | None:
        """The kept member's owned paths and its declared files, or None when `tkey` is in no wave (or was set aside)."""
        events = self.run.events.read()
        last, n = self._last_attribution(events, tkey)
        if last is None or str(n) in (last.get("out") or {}):
            return None
        rel = self.ctx.values.get("plan") or ""
        try:
            task = next((t for t in sources.parse_plan(self.worktree / rel) if t.n == n), None) if rel else None
        except (OSError, UnicodeDecodeError):
            task = None
        declared = sources.task_files(task) if task is not None else []
        return sorted({*(str(p) for p in (last.get("owners") or {}).get(str(n)) or []), *declared})

    def wave_gated(self, row: dict, tree: str) -> bool:
        """Whether a member's projected implement row (`row`, its `step_finished`) holds for `tree` though the trees
        differ: only the plan file differs, and the plan is the one `HEAD` holds, so the difference is the boxes the
        runner ticked and committed for the members before it. Never true for a row a wave did not project."""
        rel = self.ctx.values.get("plan") or ""
        gated = str(row.get("tree") or "")
        if row.get("wave") is None or not rel or not gated:
            return False
        try:
            return (changed_paths(self.worktree, gated, tree) == [rel]
                    and rel not in changed_paths(self.worktree, "HEAD^{tree}", tree))
        except (GitError, subprocess.CalledProcessError):
            return False

    def _last_attribution(self, events: list[dict], tkey: str) -> tuple[dict | None, int | None]:
        """The last `wave_attributed` that names task `tkey` among its members, and the task's number."""
        match = TASK_N.search(f"{tkey}/")
        if match is None:
            return None, None
        n = int(match[1])
        prefix = tkey.rsplit("/task-", 1)[0]
        last = None
        for event in events:
            if (event["type"] == "wave_attributed" and str(event.get("key") or "").startswith(f"{prefix}/wave-")
                    and str(n) in (event.get("owners") or {})):
                last = event
        if last is not None and any(e["type"] == "wave_dissolved" and e.get("key") == last["key"] for e in events):
            return None, n
        return last, n

    # ------------------------------------------------------------------ forming a wave

    def _waves(self, prefix: str, events: list[dict]) -> list[dict]:
        return [e for e in events if e["type"] == "wave_started" and str(e.get("key") or "").startswith(f"{prefix}/wave-")]

    def _open_wave(self, prefix: str, n: int) -> dict | None:
        """The `wave_started` of the wave under `prefix` that holds task `n` and has neither finished nor dissolved."""
        events = self.run.events.read()
        closed = {e.get("key") for e in events if e["type"] in ("wave_finished", "wave_dissolved")}
        for event in reversed(self._waves(prefix, events)):
            if event["key"] not in closed and n in (event.get("tasks") or []):
                return event
        return None

    def _form_wave(self, phase: Phase, step: Step, prefix: str, task: sources.PlanTask, tasks: list[sources.PlanTask],
                   held: set[int], small: dict) -> dict | None:
        """Form a wave at `task` and record it (`wave_started`), or None when none of two or more forms. Every
        demotion is recorded once per task and reason. A task held back, parked, waiting on a held task, in an
        earlier wave (finished, dissolved or set aside) or whose implementer already started never joins."""
        if not task.open:
            return None
        events = self.run.events.read()
        earlier = self._waves(prefix, events)
        number = len(earlier) + 1
        by_n = {t.n: t for t in tasks}
        steps = self.run.state["steps"]
        skip = {*held, *self._parked_numbers(prefix), *(int(m) for e in earlier for m in e.get("tasks") or [])}
        skip |= {t.n for t in tasks if park.waits_on(t, held, by_n)}
        skip |= {t.n for t in tasks if (steps.get(f"{prefix}/task-{t.n}/implement") or {}).get("status")
                 not in (None, "pending")}
        if task.n in skip or task.group is None:
            return None
        impl_step = self._impl_step(step, small)
        model = resolve_model(self.cfg, phase, impl_step, impl_step.role or "build", self.overrides)[0]
        sized = self._sized_answers({f"{prefix}/task-{t.n}/implement" for t in tasks})
        risky = {t.n for t in tasks
                 if (answers := sized.get(f"{prefix}/task-{t.n}/implement")) is not None and sizing.flags(answers)["risk"]}
        security = {t.n for t in tasks if overlays.touches_security(self.cfg, sources.task_files(t))}
        committed = {t.n for t in tasks if not t.open or self._committed(f"{prefix}/task-{t.n}")}
        members, demotions = wave.form_wave(
            wave.group_candidates(tasks, task, skip), by_n, committed=committed,
            serial_paths=self.cfg.parallel.serial_paths, risky=risky, security=security,
            model_of=dict.fromkeys(by_n, model), cap=self.cfg.parallel.max_subagents)
        for demotion in demotions:
            self._demote(prefix, demotion.task, demotion.reason, number)
        if not members:
            return None
        key = f"{prefix}/wave-{number}"
        effort = self._wave_effort(prefix, members, events)
        self.run.emit("wave_started", key=key, wave=number, tasks=members, start_head=head(self.worktree),
                      start_tree=snapshot(self.worktree), model=model, effort=effort)
        self.run.emit("effort_decided", key=f"{key}/implement", level=effort, source="wave",
                      reason=f"the highest level of tasks {', '.join(str(m) for m in members)}")
        return self._open_wave(prefix, task.n)

    def _wave_effort(self, prefix: str, members: list[int], events: list[dict]) -> str:
        """The highest of the members' implement levels (`effort_decided`), in `effort.allowed` order; the default
        level when none was decided."""
        keys = {f"{prefix}/task-{m}/implement" for m in members}
        latest = {e["key"]: str(e["level"]) for e in events if e["type"] == "effort_decided" and e.get("key") in keys}
        if not latest:
            return self.cfg.effort.default
        allowed = self.cfg.effort.allowed
        return max(latest.values(), key=lambda level: allowed.index(level) if level in allowed else -1)

    def _demote(self, prefix: str, n: int, reason: str, number: int) -> None:
        """Record that task `n` is built alone, once per task and reason."""
        key = f"{prefix}/task-{n}"
        if not any(e["type"] == "parallel_demoted" and e.get("key") == key and e.get("reason") == reason
                   for e in self.run.events.read()):
            self.run.emit("parallel_demoted", key=key, task=n, reason=reason, wave=number)

    # ------------------------------------------------------------------ the wave's stages

    def _run_wave(self, phase: Phase, step: Step, plan: Path, rel: str, prefix: str, started: dict,
                  tasks: list[sources.PlanTask], small: dict) -> WaveOutcome:
        """The wave's stages from its session on (the module docstring)."""
        key, number = str(started["key"]), int(started["wave"])
        by_n = {t.n: t for t in tasks}
        members = [by_n[m] for m in started.get("tasks") or [] if m in by_n]
        impl_step = self._impl_step(step, small)
        while True:
            if self._wave_session(phase, step, impl_step, prefix, key, number, members, rel, small) != "done":
                return self._wave_stopped(key)
            attributed = self._attribute(prefix, key, started, members, rel)
            if attributed is None:
                return "stopped"
            kept = [t for t in members if str(t.n) not in (attributed.get("out") or {})]
            if not kept or self._projected(prefix, number, kept):
                break
            failed = self._wave_gate_failures(key, impl_step)
            if not failed:
                break
            if self._gates_reopened(key):  # its own fix loop passed on this tree: nothing changed it since
                self._wave_failed(key, f"The build gates fail on {key}'s tree after its fix loop passed.")
                return "stopped"
            note = FIX_NOTE.format(report=failure_report(failed))
            self.run.emit("step_reopened", key=f"{key}/implement", reason="gates", note=note)
            self.run.emit("fix_attempt", key=f"{key}/implement", attempt=1, gates=[r.name for r in failed])
        self._project(phase, prefix, started, kept, attributed)
        for task in kept:
            tkey = f"{prefix}/task-{task.n}"
            if self._committed(tkey) or self._parked(tkey):
                continue
            if not self._unshelve(tkey):
                return "stopped"
            settled = self._task_outcome(tkey, partial(self._run_task, phase, step, plan, rel, task, tkey, small, None,
                                                       prefix=prefix))
            if settled == "stopped":
                return "stopped"
        if not any(e["type"] == "wave_finished" and e.get("key") == key for e in self.run.events.read()):
            committed = [t.n for t in kept if self._committed(f"{prefix}/task-{t.n}")]
            self.run.emit("wave_finished", key=key, wave=number, committed=committed,
                          parked=[t.n for t in kept if t.n not in committed])
        return "ran"

    def _wave_session(self, phase: Phase, step: Step, impl_step: Step, prefix: str, key: str, number: int,
                      members: list[sources.PlanTask], rel: str, small: dict) -> str:
        """Stage 3: the wave's one session (`<W>/implement`), with the step's gates once they have reopened it."""
        session_key = f"{key}/implement"
        gates = impl_step.gates if self._gates_reopened(key) else None
        wave_step = dataclasses.replace(
            impl_step, skill=self.cfg.skills_map.get("build_wave") or WAVE_SKILL, prompt=WAVE_PROMPT, gates=gates,
            review=None, batch=None, small_plan=None, carry=None, task_briefs={}, continuation=False,
            wave_size=len(members))
        cards = {t.n: self.run.agent_dir / "wave" / f"wave-{number}" / f"task-{t.n}.md" for t in members}
        if (self.run.state["steps"].get(session_key) or {}).get("status") not in ("done", "unchanged"):
            self._write_cards(step, prefix, members, cards, rel, small)
        extra = {"wave_title": "Tasks " + ", ".join(str(t.n) for t in members),
                 "wave_cards": "\n".join(f"- Task {t.n}: {t.title}: `{cards[t.n]}`" for t in members)}
        return self.run_agent_step(phase, wave_step, session_key, extra=extra, size=False)

    def _write_cards(self, step: Step, prefix: str, members: list[sources.PlanTask], cards: dict[int, Path], rel: str,
                     small: dict) -> None:
        """Stage 2: each member's card, its own task prompt with no result contract and then the member rules. Written
        again before each call of a session that is not done, so the cards are the runner's whatever was left there."""
        from agentflow.lanes import build_prompt  # `lanes` imports this module for `Executor`'s bases

        rules_file = pack.prompt_path(self.cfg.path, MEMBER_RULES)
        if rules_file is None:
            raise FileNotFoundError(f"prompt '{MEMBER_RULES}' is not in the pack's prompts/ or the engine's")
        rules = rules_file.read_text(encoding="utf-8")
        for task in members:
            member_step = self._impl_step(step, small, task.text)
            extra = {**self._step_extra(f"{prefix}/task-{task.n}/implement", self._task_extra(task, rel)),
                     "result_file": CARD_RESULT}
            body = build_prompt(self.cfg, member_step, self.ctx, {}, extra, "first", card=True)
            allowed = [f"- `{path}`" for path in sources.task_files(task)]
            allowed += [f"- a new test file in `{folder or '.'}/`" for folder in sorted(wave.allowed_dirs(task))]
            member = fmt(rules, {"task_n": str(task.n), "allowed_files": "\n".join(allowed)})
            safeio.write_text(cards[task.n], f"{body.rstrip()}\n\n{member.strip()}\n")

    def _attribute(self, prefix: str, key: str, started: dict, members: list[sources.PlanTask],
                   rel: str) -> dict | None:
        """Stage 4: the wave's attribution since its session last finished (`wave_attributed`), made once; None when
        the run stopped instead."""
        events = self.run.events.read()
        finished = [e for e in events if e["type"] == "step_finished" and e.get("key") == f"{key}/implement"
                    and e.get("status") == "done"]
        attributed = [e for e in events if e["type"] == "wave_attributed" and e.get("key") == key]
        if attributed and (not finished or attributed[-1]["seq"] > finished[-1]["seq"]):
            return attributed[-1]
        if attributed:
            return self._attribute_fix(prefix, key, started, members, rel, attributed[-1])
        return self._attribute_first(prefix, key, started, members, rel)

    def _changed(self, key: str, start_tree: str, tree: str) -> list[str]:
        held = self._held_paths(key)  # a wave key is no task: every parked task's files are held, none is the wave's
        return [p for p in changed_paths(self.worktree, start_tree, tree) if p not in held]

    def _reports(self, key: str, members: list[sources.PlanTask]) -> dict[int, wave.Report]:
        """The members' reports in the wave session's last result (the runner's checked copy)."""
        try:
            raw = json.loads(self.run.result_path(f"{key}/implement").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return wave.parse_reports(raw.get("tasks") if isinstance(raw, dict) else None, [t.n for t in members])

    def _attribute_first(self, prefix: str, key: str, started: dict, members: list[sources.PlanTask],
                         rel: str) -> dict | None:
        """Shelve the tree, put the strays and the set-aside members' files back as at the wave's start, then record
        it. A shelf a runner wrote before it died is read instead of the tree, so the outcome is the same."""
        number, start_head, start_tree = int(started["wave"]), str(started["start_head"]), str(started["start_tree"])
        try:
            ref = shelf_ref(self.run.id, key)
        except ValueError as exc:
            return self._wave_failed(key, f"The wave's work cannot be shelved: {exc}.")
        shelf = commit_at(self.worktree, ref)
        tree = f"{shelf}^{{tree}}" if shelf else snapshot(self.worktree)
        reports = self._reports(key, members)
        found = wave.attribute(self._changed(key, start_tree, tree), members, reports, rel)
        out: dict[int, str] = {}
        for task in members:
            report = reports.get(task.n)
            if report is None:
                out[task.n] = "no_report"
            elif report.status in SET_ASIDE:
                out[task.n] = report.status
            elif task.n in found.strays.values():
                out[task.n] = "scope_violation"
        moving = sorted({*found.strays, *(p for n in out for p in found.owners.get(n, []))})
        if moving and shelf is None:
            try:
                shelf = shelve(self.worktree, tree, ref, f"Shelve {key}")
            except (GitError, subprocess.CalledProcessError) as exc:
                return self._wave_failed(key, f"The wave's work cannot be shelved: {clean_fact(str(exc))}.")
        if moving:
            restore_paths(self.worktree, moving, start_head)
        summaries = {str(n): clean_fact(r.summary) for n, r in reports.items() if n not in out}
        self.run.emit("wave_attributed", key=key, wave=number, owners={str(n): p for n, p in found.owners.items()},
                      out={str(n): reason for n, reason in out.items()}, strays=sorted(found.strays),
                      shelf=shelf if moving else None, summaries=summaries)
        self._record_strays(prefix, key, number, found.strays)
        for n, reason in out.items():
            self._demote(prefix, n, reason, number)
        return self._last_wave_attribution(key)

    def _attribute_fix(self, prefix: str, key: str, started: dict, members: list[sources.PlanTask], rel: str,
                       last: dict) -> dict | None:
        """The attribution again after the gates' fix loop, over the kept members: a path none of them owns is a new
        stray, and the run stops on it."""
        number, start_tree = int(started["wave"]), str(started["start_tree"])
        out = dict(last.get("out") or {})
        kept = [t for t in members if str(t.n) not in out]
        reports = self._reports(key, kept)
        found = wave.attribute(self._changed(key, start_tree, snapshot(self.worktree)), kept, reports, rel)
        owners = {**{n: [] for n in out}, **{str(n): p for n, p in found.owners.items()}}
        summaries = {**(last.get("summaries") or {}),
                     **{str(n): clean_fact(r.summary) for n, r in reports.items() if r.status == "done"}}
        self.run.emit("wave_attributed", key=key, wave=number, owners=owners, out=out, strays=sorted(found.strays),
                      shelf=last.get("shelf"), summaries=summaries)
        self._record_strays(prefix, key, number, found.strays)
        if found.strays:
            shown = clean_fact(", ".join(f"`{p}`" for p in sorted(found.strays)))  # names an agent chose
            return self._wave_failed(key, f"The fix of {key}'s gates changed files no member of the wave owns: {shown}.")
        return self._last_wave_attribution(key)

    def _record_strays(self, prefix: str, key: str, number: int, strays: dict[str, int | None]) -> None:
        for path, owner in sorted(strays.items()):
            self.run.emit("scope_violation", key=f"{prefix}/task-{owner}" if owner is not None else key, task=owner,
                          path=path, wave=number)

    def _last_wave_attribution(self, key: str) -> dict | None:
        found = [e for e in self.run.events.read() if e["type"] == "wave_attributed" and e.get("key") == key]
        return found[-1] if found else None

    def _gates_reopened(self, key: str) -> bool:
        return any(e["type"] == "step_reopened" and e.get("key") == f"{key}/implement" and e.get("reason") == "gates"
                   for e in self.run.events.read())

    def _wave_gate_failures(self, key: str, impl_step: Step) -> list[GateResult]:
        """Stage 5: the build gates on the combined tree, keyed `<W>/implement`; the failed ones. A pass already
        recorded on this tree (the fix loop's, or a walk's before a restart) is reused, not run again."""
        if not impl_step.gates:
            return []
        session_key = f"{key}/implement"
        tree = snapshot(self.worktree)
        results = run_gates_reusing(self.cfg, self.run, session_key, impl_step.gates, self.worktree,
                                    self.run.gate_log_dir(session_key), tree)
        record_results(self.run, session_key, results, tree, with_tail=True)
        return [r for r in results if not (r.passed or r.skipped)]

    def _projected(self, prefix: str, number: int, kept: list[sources.PlanTask]) -> bool:
        """Whether every kept member has its projected implement row."""
        keys = {e.get("key") for e in self.run.events.read()
                if e["type"] == "step_finished" and e.get("wave") == number and e.get("status") == "done"}
        return all(f"{prefix}/task-{t.n}/implement" in keys for t in kept)

    def _project(self, phase: Phase, prefix: str, started: dict, kept: list[sources.PlanTask],
                 attributed: dict) -> None:
        """Stage 6: a `step_started` and a `step_finished` on each kept member's own implement key, for those that
        have none yet: the member was built from the wave's start, by the wave's model and effort, and its gates
        passed on this tree."""
        number = int(started["wave"])
        tree = snapshot(self.worktree)
        summaries = attributed.get("summaries") or {}
        done = {e.get("key") for e in self.run.events.read()
                if e["type"] == "step_finished" and e.get("wave") == number and e.get("status") == "done"}
        for task in kept:
            key = f"{prefix}/task-{task.n}/implement"
            if key in done:
                continue
            self.run.emit("step_started", key=key, phase=phase.name, agent=phase.agent or self.cfg.defaults.agent,
                          model=started.get("model"), effort=started.get("effort"), mode="first",
                          start_head=started["start_head"], start_tree=started["start_tree"],
                          task=self._task_payload(key), wave=number)
            self.run.emit("step_finished", key=key, status="done", summary=str(summaries.get(str(task.n)) or ""),
                          tree=tree, wave=number)

    # ------------------------------------------------------------------ when the wave cannot go on

    def _wave_stopped(self, key: str) -> WaveOutcome:
        """The wave session stopped. For now its stop stays the run's wait, and a person is told of it once (a wave's
        stop is raised quietly); the walk settled it, so `pipeline.walk` does not take it over."""
        self.run.refresh()
        wait = self.run.state["waiting"]
        if wait is not None and under(str(wait.get("key") or ""), key):
            self.handled_waits.add(wait_id(wait))
            self._tell_once(wait)
        return "stopped"

    def _wave_failed(self, key: str, summary: str) -> None:
        """The runner cannot go on with the wave (its tree could not be shelved, or the gates' fix left a stray): a
        `blocked` stop on its session, told to a person at once."""
        session_key = f"{key}/implement"
        if not self._waiting_on(session_key):
            raise_hitl(self.cfg, self.run, "blocked", session_key, summary, notify=True)
        self._wave_stopped(key)
        return None
