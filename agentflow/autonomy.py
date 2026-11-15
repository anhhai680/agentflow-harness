"""What the runner does by itself in a phase that needs no human, all of it decided by `config.yaml`.

Nothing here names a phase. A phase's `needs_approval` and `resolve`, a step's `blocks_on_findings`, and the
`autonomy:` block say what applies; this module only reads them.

- **Findings** (`phase_findings`, `blocking_of`): the `findings` of a phase's `blocks_on_findings` steps. They
  fail closed: only a severity in `autonomy.non_blocking_severities` lets a phase go on, so a reviewer's
  `medium`, or no severity at all, counts as blocking.
- **Inner stops** (`Resolver`): every stop is raised as usual, and `pipeline._Loop` hands it here before a human
  hears of it. The runner acts through the ways out a human has (`answer`, a reopened step), so a stop has one
  meaning whoever settles it. The resolver's session runs as `<key>/resolve-<n>` under the same guards and gates
  as any other, and only the original step's own checks can finish that step.
- **A stop's class** (`Resolver._classify`): a resolver may name its stop's cause (`hitl.STOP_CLASSES`). Each class is
  recorded once per session (`stop_classified`); for `flaky` the runner first reruns the failed gates alone
  (`_confirm_flaky`). A stop whose attempts are used is recorded as `repeated_failure`.
- **The machine** (`environment`): the runner probes the programs of the stop's gates (`stop_gates`, `preflight.probe`).
  When they pass, the step reopens as usual. When they fail, the stop becomes one `environment` inbox item for a human
  (`_hand_over`), told once per distinct problem; `approve` checks the machine again first (`recheck_environment`), and
  the queue and the dashboard approve it themselves once the checks pass (`preflight.recheck_waiting`).
- **Questions** (`needs_human`): with `autonomy.answer_recommended`, the runner takes the one recommended option of
  every question in **any** phase, `resolve: true` or not, parked or not, records it as `by: runner` and notifies
  one line. A question whose `kind` is in `autonomy.human_question_kinds` (`boundary` by default: the docs reserve
  the decision for a human) makes the whole set a human's, as does one the runner already answered on that step
  (`asked_again`), so a step that keeps asking never loops.

What stays a human's: every other kind not in `autonomy.resolve_kinds`, any stop outside a `resolve: true` phase
that is not a question, a resolver's own stop, and any stop once its attempts are used.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentflow import preflight
from agentflow.commit_checks import runner_head
from agentflow.config import FLOOR_BLOCKING, QUESTION_KINDS, Config, Phase, Step
from agentflow.flow_control import halted
from agentflow.gates import run_gates
from agentflow.gitops import GitError, restore_paths, tree_state
from agentflow.hitl import ENVIRONMENT, ResultError, answer, clean_fact, notify_run, raise_hitl, read_result
from agentflow.state import Run

if TYPE_CHECKING:
    from agentflow.lanes import Executor
    from agentflow.preflight import Probe

AUTO_BY = "runner"  # `by` on what the runner settles itself; a human's name is never this
RESOLVE_MARK = "/resolve-"
GUARD_KIND = "guard_violation"
FORBIDDEN = "forbidden_path"
QUESTIONS_KIND = "needs_human"
DEFAULT_HUMAN_KINDS = ("boundary",)  # `autonomy.human_question_kinds` when no layer sets it
QUESTION_CMP_MAX = 2000  # characters of a question's text `asked_again` compares
OPTION_SHOWN = 40  # characters of an option key in the notification of an answer the runner took
FLAKY_RERUNS = 2  # how many times `_confirm_flaky` runs each failing gate alone; every one must pass
ENV_POLL_S = 120  # the least seconds between two checks of one `environment` stop by `preflight.recheck_waiting`
ENV_FIXED = "environment_fixed"  # `stop_classified.action` once the machine checks of an `environment` stop pass


# --------------------------------------------------------------------------- findings


def is_blocking(cfg: Config, finding: dict) -> bool:
    """Fails closed: a finding blocks unless its severity is one `config.yaml` lists as non-blocking.
    The engine floor: `critical`, `high`, `medium`, an empty severity and a missing one always block."""
    severity = finding.get("severity")
    name = "" if severity is None else str(severity).strip().lower()
    if not name or name in FLOOR_BLOCKING:
        return True
    allowed = {str(listed).lower() for listed in cfg.autonomy.get("non_blocking_severities") or ()}
    return name not in allowed


def round_limit(cfg: Config) -> int:
    """How many refine rounds the runner may open on a phase's blocking findings: `autonomy.finding_rounds`, but at
    least 1, and 1 for a value that is not a whole number or is missing (a workflow that drops the `autonomy` block)."""
    value = cfg.autonomy.get("finding_rounds")
    return max(value, 1) if isinstance(value, int) and not isinstance(value, bool) else 1


def as_finding(raw: object) -> dict:
    """A finding as a mapping: anything else a reviewer wrote stays visible rather than being dropped."""
    return raw if isinstance(raw, dict) else {"text": str(raw)}


def blocking_of(cfg: Config, findings: list) -> list[dict]:
    """The findings that hold a task or a phase back."""
    return [f for f in map(as_finding, findings) if is_blocking(cfg, f)]


def finding_text(finding: dict) -> str:
    """What the finding says: reviewers write `text`, or `title` and `detail` when a skill's format leaks in."""
    text = finding.get("text") or " ".join(str(finding.get(k) or "") for k in ("title", "detail")).strip()
    return " ".join(str(text).split())


def finding_line(finding: dict) -> str:
    where = str(finding.get("file") or "?")
    if finding.get("line") not in (None, ""):
        where += f":{finding['line']}"
    severity = f"[{finding['severity']}] " if finding.get("severity") else ""
    return f"- {severity}{where}: {finding_text(finding)}"


def _flagged(steps: list[Step], prefix: str) -> list[str]:
    keys: list[str] = []
    for step in steps:
        key = f"{prefix}/{step.id}"
        if step.blocks_on_findings:
            keys.append(key)
        keys += _flagged(step.steps, key)
    return keys


def phase_findings(cfg: Config, run: Run, phase: str) -> list[dict]:
    """Every finding of the phase's `blocks_on_findings` steps that ran, each with its step's `key`.

    A flagged step whose result cannot be read is a blocking finding: no result is no clean review.
    """
    run.refresh()
    found: list[dict] = []
    for key in _flagged(cfg.phases[phase].steps, phase):
        if (run.state["steps"].get(key) or {}).get("status") not in ("done", "unchanged"):
            continue
        try:
            findings = read_result(run.result_path(key), refine=True).findings
        except ResultError as exc:
            findings = [{"text": f"the result of {key} cannot be read: {exc}"}]
        found += [{**as_finding(raw), "key": key} for raw in findings]
    return found


def findings_note(findings: list[dict]) -> str:
    """The note of a refine round the runner opens itself."""
    lines = "\n".join(f"{finding_line(f)} (from `{f.get('key', '?')}`)" for f in findings)
    return f"These findings hold the phase back:\n\n{lines}"


def finding_rounds(run: Run, phase: str) -> int:
    """How many refine rounds the runner has opened on `phase`."""
    return sum(1 for e in run.events.read()
               if e["type"] == "phase_rejected" and e.get("phase") == phase and e.get("by") == AUTO_BY)


# --------------------------------------------------------------------------- inner stops


def _phase_of(cfg: Config, key: str) -> Phase | None:
    return cfg.phases.get(key.split("/")[0])


def recommended_answers(questions: list[dict]) -> dict[str, str] | None:
    """`{id: option key}` when every question has exactly one recommended option, else None."""
    answers: dict[str, str] = {}
    for question in questions:
        picks = [o["key"] for o in question.get("options") or [] if o.get("recommended")]
        if len(picks) != 1:
            return None
        answers[question["id"]] = picks[0]
    return answers or None


def human_kinds(cfg: Config) -> tuple[str, ...]:
    """The question kinds only a human answers. Unset is the engine's default; a value that is not a list (which
    `validate` refuses) is every kind, so a broken setting never lets the runner answer a reserved decision."""
    kinds = cfg.autonomy.get("human_question_kinds")
    if kinds is None:
        return DEFAULT_HUMAN_KINDS
    if not isinstance(kinds, (list, tuple)):
        return QUESTION_KINDS
    return tuple(str(kind) for kind in kinds)


def eligible(cfg: Config, kind: str, key: str, questions: list[dict]) -> bool:
    """Whether the runner may try to settle this stop, from the config alone. A stop it may not try is a human's at
    once.

    A stop of the resolver's own session is always a human's: it is what the resolver could not settle. Questions
    are settled in every phase; every other kind only in a `resolve: true` phase.
    """
    phase = _phase_of(cfg, key)
    if phase is None or RESOLVE_MARK in key:
        return False
    if kind == QUESTIONS_KIND:
        if any(step.reconciles and key == f"{phase.name}/{step.id}" for step in phase.steps):
            return False  # what a reconcile step asks is a human's, whatever its kind: nothing is filed into the spec unseen
        reserved = human_kinds(cfg)
        return (cfg.autonomy.get("answer_recommended") is True and recommended_answers(questions) is not None
                and not any(str(q.get("kind") or "other") in reserved for q in questions))
    if not phase.resolve:
        return False
    if kind == GUARD_KIND:
        return int(cfg.autonomy.get("revert_forbidden") or 0) > 0
    return kind in (cfg.autonomy.get("resolve_kinds") or {})


def _said(question: dict) -> str:
    """A question's text, compared case- and space-blind. Model text: cut before it is normalised."""
    return " ".join(str(question.get("question") or "")[:QUESTION_CMP_MAX].split()).casefold()


def asked_again(run: Run, key: str, questions: list[dict]) -> bool:
    """Whether a question here is one the runner already answered on `key` since a human last acted on it. Taking the
    same option again would only ask the step the same thing again, so such a set is a human's."""
    asked: list[dict] = []
    answered: set[str] = set()
    for event in run.events.read():
        if event.get("key") != key:
            continue
        if event["type"] in ("hitl_raised", "step_parked"):
            asked = list(event.get("questions") or [])
        elif event["type"] == "hitl_answered" and event.get("by") == AUTO_BY:
            answered |= {_said(q) for q in asked}
        elif event["type"] in ("hitl_answered", "hitl_approved"):  # a human's attention starts afresh
            answered = set()
    return any(_said(q) in answered for q in questions)


def runner_settles(cfg: Config, run: Run, kind: str, key: str, questions: list[dict]) -> bool:
    """Whether the runner takes this stop over (`Resolver.take_over`), so no human is told of it yet: `eligible`, and
    for questions not a set it already answered on this step."""
    if not eligible(cfg, kind, key, questions):
        return False
    return kind != QUESTIONS_KIND or not asked_again(run, key, questions)


def _since_human(events: list[dict], key: str) -> list[dict]:
    """The events after a human last settled a stop on `key`: a human's attention earns fresh attempts."""
    for index in range(len(events) - 1, -1, -1):
        event = events[index]
        if event["type"] == "hitl_approved" and event.get("key") == key and event.get("by") != AUTO_BY:
            return events[index + 1 :]
    return events


def stop_gates(cfg: Config, run: Run, key: str) -> list[str]:
    """The gates whose programs show whether this machine can go on with a stop on `key`: those that ran on the step
    or under it (its fix loop, its resolver), in order and without repeats, then `gate_sets.setup`; every gate of the
    run's lane (`preflight.route_gates`) when none ran there."""
    ran = [str(e["gate"]) for e in run.events.read()
           if e["type"] == "gate_result" and e.get("gate")
           and (e.get("key") == key or str(e.get("key") or "").startswith(f"{key}/"))]
    if not ran:
        return preflight.route_gates(cfg, run.state["pipeline"])
    return list(dict.fromkeys([*ran, *preflight.setup_gates(cfg)]))


def _first_notice(events: list[dict], digest: str) -> bool:
    """Whether a human hears of this machine problem: not when an `environment_waiting` with the same `digest` came
    after the last time the machine was found fixed. One notification per distinct problem."""
    fixed = max((i for i, e in enumerate(events) if e["type"] == "stop_classified" and e.get("action") == ENV_FIXED),
                default=-1)
    return not any(e["type"] == "environment_waiting" and e.get("digest") == digest for e in events[fixed + 1:])


def _environment_summary(key: str, probe: Probe) -> str:
    return clean_fact(f"{key} waits on this machine: {probe.lines[0]}")  # tool output: one clean line


class Resolver:
    """Settles a stop in a `resolve: true` phase, or leaves it to a human. See the module docstring."""

    def __init__(self, cfg: Config, run: Run, executor: Executor) -> None:
        self.cfg = cfg
        self.run = run
        self.executor = executor
        self._handed_over = False  # a stop went to a human as `environment` (`_hand_over`), who was told then
        self._machine: tuple[Probe, list[str]] | None = None  # the probe `_check_machine` took, and its gates

    def finish_open(self) -> bool:
        """Finish a resolve the runner left open (it died, or a human settled the resolver's own stop).
        False when that session stopped for a human. A human's approval of an `environment` stop is checked first."""
        if not self.recheck_environment():
            return False
        self.run.refresh()
        open_ = self.run.state.get("resolving")
        if not open_:
            return True
        return self._run_session(open_["key"], open_["kind"], open_.get("session_key"), {})

    def take_over(self) -> bool:
        """True when the run can go on without a human. A stop the runner tried and cannot settle notifies
        here, because `raise_hitl` left it quiet for the runner."""
        self.run.refresh()
        self._handed_over = False
        wait = self.run.state["waiting"]
        if wait is None or halted(self.run.state["status"]):
            return False
        kind, key = wait["kind"], wait["key"]
        if not runner_settles(self.cfg, self.run, kind, key, wait["questions"]):
            return False  # `raise_hitl` told a human already
        if kind == QUESTIONS_KIND:
            answers = recommended_answers(wait["questions"]) or {}
            if not answer(self.run, key, answers, AUTO_BY):
                return False
            taken = ", ".join(f"{qid}={clean_fact(option)[:OPTION_SHOWN]}" for qid, option in answers.items())  # model text
            notify_run(self.cfg, self.run, f"took the recommended answer(s) {taken} in {key}; the decision is recorded")
            return True
        settled = self._revert(key) if kind == GUARD_KIND else self._resolve(wait)
        if not settled and not self._handed_over:  # a hand-over told the human already
            self.run.refresh()
            if self.run.state["waiting"] is not None and self.run.state["waiting"]["key"] == key:
                notify_run(self.cfg, self.run, wait["summary"] or f"{key} needs a decision")
        return settled

    def _attempts(self, key: str) -> int:
        return sum(1 for e in _since_human(self.run.events.read(), key)
                   if e["type"] == "resolve_started" and e.get("key") == key and e.get("session_key"))

    def _resolve(self, wait: dict) -> bool:
        kind, key = wait["kind"], wait["key"]
        attempt = self._attempts(key) + 1
        if self.cfg.resolve_step is None:
            return False
        if attempt > int(self.cfg.autonomy["resolve_kinds"][kind]):
            self._record_repeated(key)
            return False
        sessions = sum(1 for e in self.run.events.read() if e["type"] == "resolve_started"
                       and e.get("key") == key and e.get("session_key"))
        extra = {"stop_kind": kind, "stop_key": key, "stop_summary": wait["summary"],
                 "stop_report": self.executor.stop_report(key)}
        session_key = f"{key}{RESOLVE_MARK}{sessions + 1}"  # never a finished one: a human's fresh attempts are new sessions
        self.run.emit("resolve_started", key=key, stop_kind=kind, session_key=session_key, attempt=attempt)
        return self._run_session(key, kind, session_key, extra)

    def _run_session(self, key: str, kind: str, session_key: str | None, extra: dict) -> bool:
        phase = _phase_of(self.cfg, key)
        step = self.cfg.resolve_step
        if session_key is None or step is None or phase is None:
            self._finish(key, kind, "")
            return True
        plain = Phase(name=phase.name, agent=phase.agent, model=None, skip_if=None, steps=[])
        # A session that finished before this call (a resume) has no `before`: it counts as having changed the tree.
        ran_before = (self.run.state["steps"].get(session_key) or {}).get("status") in ("done", "unchanged")
        before = None if ran_before else self._tree()
        outcome = self.executor.run_agent_step(plain, step, session_key, extra=extra, role=step.role,
                                               size=False)
        if outcome != "done":
            return False
        changed = before is None or before != self._tree()
        self.run.refresh()
        summary = str((self.run.state["steps"].get(session_key) or {}).get("result") or "")
        self._remember_facts(session_key)
        self._machine = None
        action = self._classify(key, session_key, changed)
        if action == "halted":
            return False  # a human paused or ended the run: the resolve stays open (`finish_open`)
        if action == "handed_over":
            if self._machine is None:  # a resume of a hand-over that did not finish: check the machine again
                self._check_machine(key)
            probe, gates = self._machine
            if not probe.ok:
                self._hand_over(key, kind, summary, probe, gates)
                return False
        self._finish(key, kind, summary)
        return True

    def _tree(self) -> dict | None:
        """The work tree's `gitops.tree_state`, or None when git cannot read it (which counts as a change)."""
        try:
            return tree_state(self.executor.worktree)
        except (GitError, OSError):
            return None

    # -- a stop's class (`StepResult.stop_class`): the runner acts on some, and records every one

    def _stop_class(self, session_key: str) -> str | None:
        """The class the resolver session named for the stop's cause, or None (no class, or no readable result)."""
        try:
            return read_result(self.run.result_path(session_key)).stop_class
        except ResultError:
            return None

    def _classified(self, session_key: str) -> str | None:
        """The action recorded for this session's class, or None when it was not classified yet."""
        recorded = [e for e in self.run.events.read()
                    if e["type"] == "stop_classified" and e.get("session_key") == session_key]
        return str(recorded[-1].get("action") or "") if recorded else None

    def _classify(self, key: str, session_key: str, changed: bool) -> str | None:
        """Act on the class the resolver named, once per session, and return the action recorded for it.

        A resume of the same session (`finish_open`) records nothing again and reruns nothing; only a hand-over is
        taken up again (`handed_over`), so the stop still reaches a human. `halted` when the run was halted while the
        runner acted on it."""
        cls = self._stop_class(session_key)
        if cls is None:
            return None
        recorded = self._classified(session_key)
        if recorded is not None:
            return recorded if recorded == "handed_over" else None
        if cls == "flaky":
            action = self._confirm_flaky(key, session_key, changed)
        elif cls == ENVIRONMENT:
            action = self._check_machine(key)
        else:  # every other class is recorded only
            action = "none"
        self.run.emit("stop_classified", key=key, session_key=session_key, stop_class=cls, action=action)
        return action

    # -- the machine (`environment`): a stop the probes confirm goes to a human as one inbox item

    def _check_machine(self, key: str) -> str:
        """Probe the programs of the stop's gates: `probes_green` (the step reopens; the probes cannot see every fault,
        a port in use, and the resolver's attempts still bound the loop) or `handed_over`."""
        gates = stop_gates(self.cfg, self.run, key)
        probe = preflight.probe(self.cfg, gates, self.executor.worktree)
        self._machine = (probe, gates)
        return "probes_green" if probe.ok else "handed_over"

    def _hand_over(self, key: str, kind: str, summary: str, probe: Probe, gates: list[str]) -> None:
        """Close the resolve and give the stop to a human as one `environment` item. The human is told only of a problem
        not already waiting since the machine was last found fixed."""
        notify = _first_notice(self.run.events.read(), probe.digest)
        self.run.emit("environment_waiting", key=key, digest=probe.digest, lines=probe.lines, gates=gates, kind=kind)
        self.run.emit("resolve_finished", key=key, stop_kind=kind, summary=summary)
        raise_hitl(self.cfg, self.run, ENVIRONMENT, key, _environment_summary(key, probe),
                   extra="\n".join([*probe.lines, "", "The resolver reported:", summary]), notify=notify)
        self._handed_over = True

    def recheck_environment(self) -> bool:
        """Check the machine again after a human's approval of an `environment` stop, before the step goes on. False
        when the checks still fail: the stop is raised again (and told again only for a new problem).

        Only the newest such approval counts, once (an `environment_rechecked` after it). An approval with a note is the
        human's override and is not checked; the runner's own approval (`preflight.recheck_waiting`) carries one."""
        events = self.run.events.read()
        approvals = [(i, e) for i, e in enumerate(events) if e["type"] == "hitl_approved" and e.get("kind") == ENVIRONMENT]
        if not approvals or approvals[-1][1].get("note"):
            return True
        index, approval = approvals[-1]
        key = approval["key"]
        if any(e["type"] == "environment_rechecked" and e.get("key") == key for e in events[index + 1:]):
            return True
        held = [e for e in events[:index] if e["type"] == "environment_waiting" and e.get("key") == key]
        gates = [str(name) for name in held[-1].get("gates") or []] if held else stop_gates(self.cfg, self.run, key)
        probe = preflight.probe(self.cfg, gates, self.executor.worktree)
        self.run.emit("environment_rechecked", key=key, ok=probe.ok, digest=probe.digest)
        if probe.ok:
            self.run.emit("stop_classified", key=key, stop_class=ENVIRONMENT, action=ENV_FIXED)
            return True
        events = self.run.events.read()
        notify = _first_notice(events, probe.digest)
        if notify:  # a new problem: what is waiting now, so a repeat of it is not told again
            self.run.emit("environment_waiting", key=key, digest=probe.digest, lines=probe.lines, gates=gates,
                          kind=str(held[-1].get("kind") or "") if held else "")
        raise_hitl(self.cfg, self.run, ENVIRONMENT, key, _environment_summary(key, probe),
                   extra="\n".join(probe.lines), notify=notify)
        return False

    def _failed_gates(self, key: str) -> list[str]:
        """The gates that failed (not skipped) in the most recent batch of `gate_result` events on `key`: a maximal run
        of consecutive `gate_result` events with that key, as the signals count them."""
        batches: list[list[dict]] = []
        open_ = False
        for event in self.run.events.read():
            if event["type"] == "gate_result" and event.get("key") == key:
                if not open_:
                    batches.append([])
                batches[-1].append(event)
                open_ = True
            else:
                open_ = False
        last = batches[-1] if batches else []
        return [str(e["gate"]) for e in last if e.get("gate") and not e.get("passed") and not e.get("skipped")]

    def _confirm_flaky(self, key: str, session_key: str, changed: bool) -> str:
        """Rerun each gate that failed on `key` alone, `FLAKY_RERUNS` times. When every one passes every time, record
        one `flaky:` fact. Only when the resolver changed nothing: reruns on a tree it changed prove nothing.
        The reruns record no `gate_result`: they are no evidence, and the step's own gates still decide."""
        if changed:
            return "tree_changed"
        names = self._failed_gates(key)
        if not names:
            return "no_failed_gate"
        if any(name not in self.cfg.gates for name in names):
            return "not_confirmed"  # a check that is no gate (the regression test command, a doc check) is not rerun
        log_dir = self.run.gate_log_dir(f"{key}/flaky")
        for name in names:
            for _ in range(FLAKY_RERUNS):
                self.run.refresh()
                if halted(self.run.state["status"]):
                    return "halted"
                results = run_gates(self.cfg, [name], self.executor.worktree, log_dir)
                if not all(r.passed and not r.skipped for r in results):
                    return "not_confirmed"
        text = clean_fact(f"flaky: {', '.join(names)} failed in {key}, then passed twice when run alone")
        known = {str(f.get("text")).casefold() for f in self.run.state.get("facts") or []}
        if text.casefold() not in known:
            self.run.emit("run_fact", text=text, key=session_key)
        return "confirmed"

    def _record_repeated(self, key: str) -> None:
        """A stop whose resolve attempts are used goes to a human: say so once, until a human acts on `key` again.
        Only the runner's own record (`action: human`) counts; a resolver that named the class records `none`."""
        if any(e["type"] == "stop_classified" and e.get("key") == key and e.get("stop_class") == "repeated_failure"
               and e.get("action") == "human" for e in _since_human(self.run.events.read(), key)):
            return
        self.run.emit("stop_classified", key=key, stop_class="repeated_failure", action="human")

    def _remember_facts(self, session_key: str) -> None:
        """Keep what a resolver session found for every later session of the run. Only a resolver's result is read
        for `facts`; a fact already known (any case) is not added again."""
        if RESOLVE_MARK not in session_key:
            return
        try:
            found = read_result(self.run.result_path(session_key)).facts
        except ResultError:
            return
        known = {str(f.get("text")).casefold() for f in self.run.state.get("facts") or []}
        for text in found:
            if text.casefold() not in known:
                known.add(text.casefold())
                self.run.emit("run_fact", text=text, key=session_key)

    def _finish(self, key: str, kind: str, summary: str) -> None:
        self.run.emit("resolve_finished", key=key, stop_kind=kind, summary=summary)
        if kind == "review_unresolved":  # `feature_lane` reads this as "look again": only a human's accepts findings
            self.run.emit("hitl_approved", key=key, kind=kind, summary=summary)
            return
        note = (f"The runner stopped this step ({kind}). A fresh session looked at it and reported:\n\n{summary}\n\n"
                "Check the work tree, then finish the step. Do not repeat what failed.")
        self.run.emit("step_reopened", key=key, reason="resolve", note=note)

    def _revert(self, key: str) -> bool:
        """Put back what a step changed on forbidden paths, once, and tell the step. Only those paths move."""
        events = self.run.events.read()
        tripped = [e for e in events if e["type"] == "guard_tripped" and e.get("key") == key]
        reverted = sum(1 for e in _since_human(events, key) if e["type"] == "forbidden_reverted" and e.get("key") == key)
        sha = runner_head(self.run)
        if not tripped or not sha or reverted >= int(self.cfg.autonomy.get("revert_forbidden") or 0):
            return False
        violations = tripped[-1].get("violations") or []
        if not violations or any(v.get("kind") != FORBIDDEN for v in violations):
            return False
        paths = [str(v.get("detail")) for v in violations]
        self.run.emit("resolve_started", key=key, stop_kind=GUARD_KIND, session_key=None, attempt=reverted + 1)
        restore_paths(self.executor.worktree, paths, sha)
        self.run.emit("forbidden_reverted", key=key, paths=paths)
        self.run.emit("resolve_finished", key=key, stop_kind=GUARD_KIND, summary=f"reverted {', '.join(paths)}")
        shown = ", ".join(f"`{p}`" for p in paths)
        self.run.emit("step_reopened", key=key, reason="guard",
                      note=f"You changed {shown}, which no agent may touch in a run, so the runner put it back. Finish "
                           "the step without changing it. If the step cannot be done without it, stop as `blocked` "
                           "and say what you need.")
        return True
