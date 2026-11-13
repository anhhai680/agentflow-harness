"""Human-in-the-loop records: the `result.json` schema, the inbox files, notifications, and the
actions a human takes (`answer`, `approve`, `reject`).

These functions only record. Each one appends an event (so `state.json` follows) and, for a
rejection, writes the refine note. Continuing the run is the runner's job. The inbox Markdown is
rendered by the runner and parsed back for the `Answer:` lines, so a human can fill it in an editor
or through `agentflow answer`; Vietnamese text keeps its exact characters (Unicode NFC).
"""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
import sys
import unicodedata
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .config import CONFORMANCE_FAILED, QUESTION_KINDS, RECONCILE_CHANGED, RETRO_PIPELINE, Config
from .events import nfc, parse_iso
from .state import TERMINAL, Run, list_runs

RESULT_STATUSES = ("done", "blocked", "needs_human", "effort_change", "unchanged")
QUESTION_ID = re.compile(r"^[\w.\-]+$")
HEADING = re.compile(r"^### (\S+) — (.*)$", re.MULTILINE)
ANSWER_LINE = re.compile(r"^Answer:[ \t]*(.*)$", re.MULTILINE)
FACT_MAX = 300  # characters of one `facts` entry
FACTS_PER_RESULT = 5
CODE_MAP_PER_RESULT = 8  # lines of one `code_map`: existing code the step found, kept for the run
LESSONS_CITED_PER_RESULT = 10  # ids in one `lessons_cited`
LESSON_ID = re.compile(r"[LE]-\d{4}\Z")
_INVISIBLE_CATEGORIES = ("Cc", "Cf")  # control characters, and format ones: bidi overrides, zero-width, BOM
DEFAULT_TITLE = "agentflow: {run} needs you"
NOTIFY_TIMEOUT_S = 10
MESSAGE_MAX = 240
# Stops that `approve` settles for good (the row becomes done). Every other stop without questions is
# one the runner raised on a step that has not finished, so `approve` makes that step resumable.
# `RECONCILE_CHANGED`: a reconcile step's edit changed which steps of its phase run. The step finished, and `approve`
# accepts the edit as it is.
CLEARED_BY_APPROVE = ("checkpoint", "max_rounds", RECONCILE_CHANGED)
# A retro run walks no phase and is never resumed (`pipeline.advance` refuses it), so a stop of one has one way out.
RETRO_WAY_OUT = "cancel it and run `agentflow retro` again"


class ResultError(Exception):
    """A `result.json` that is missing, not valid JSON, or breaks the schema."""


class NotWaiting(Exception):
    """The run is not waiting on what the caller tried to answer, approve or reject."""


class NotifyError(Exception):
    """A notifier that cannot deliver (logged as an event, never raised to the caller)."""


@dataclass
class Option:
    key: str
    label: str
    recommended: bool = False


@dataclass
class Question:
    id: str
    question: str
    options: list[Option]
    evidence: list[str]
    kind: str


@dataclass
class StepResult:
    status: str
    summary: str
    next: str | None = None
    effort: str | None = None
    reason: str | None = None
    questions: list[Question] = field(default_factory=list)
    can_continue_without: bool = False
    signals: dict = field(default_factory=dict)
    findings: list = field(default_factory=list)
    dispositions: list = field(default_factory=list)  # a reconcile step's answer to each finding (`doubt.parse_dispositions`)
    risks: list = field(default_factory=list)
    facts: list[str] = field(default_factory=list)
    code_map: list[str] = field(default_factory=list)
    lessons_cited: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- result.json


def _text(value: Any, name: str) -> str:
    if isinstance(value, str) and value.strip():
        return nfc(value)
    raise ResultError(f"{name} must be a non-empty string")


def clean_fact(text: str) -> str:
    """One `facts` entry as the run keeps it: NFC, control and format characters dropped (a bidi override or a
    zero-width character could reorder or hide what a reader sees), whitespace collapsed to single spaces, and cut
    to `FACT_MAX`. It is model text, so it is cut before anything reads it further. Any one line of model text
    shown to a human (a notification, the pull request body) goes through it too."""
    text = "".join(ch for ch in text[: FACT_MAX * 4]
                   if ch.isspace() or unicodedata.category(ch) not in _INVISIBLE_CATEGORIES)
    return nfc(" ".join(text.split()))[:FACT_MAX].rstrip()


def _parse_lines(raw: Any, name: str, cap: int) -> list[str]:
    """The `name` field of a result: a list of strings, the first `cap` of them, each cleaned by `clean_fact`."""
    if not isinstance(raw, list) or not all(isinstance(f, str) for f in raw):
        raise ResultError(f"{name} must be a list of strings")
    return [line for line in (clean_fact(f) for f in raw[:cap]) if line]


def _parse_lesson_ids(raw: Any) -> list[str]:
    """The `lessons_cited` field of a result: a list of lesson ids (`L-0007`, `E-0003`), the first
    `LESSONS_CITED_PER_RESULT` of them kept. Anything else is refused: the ids are only looked up, never trusted."""
    if not isinstance(raw, list) or not all(isinstance(i, str) and LESSON_ID.match(i) for i in raw):
        raise ResultError("lessons_cited must be a list of lesson ids such as L-0007 or E-0003")
    return raw[:LESSONS_CITED_PER_RESULT]


def _parse_question(raw: Any, index: int) -> Question:
    where = f"questions[{index}]"
    if not isinstance(raw, dict):
        raise ResultError(f"{where} must be an object")
    qid = _text(raw.get("id"), f"{where}.id")
    if not QUESTION_ID.fullmatch(qid):
        raise ResultError(f"{where}.id {qid!r} may only hold letters, digits, '_', '.' and '-'")
    options = raw.get("options", [])
    if not isinstance(options, list):
        raise ResultError(f"{where}.options must be a list")
    parsed = []
    for j, opt in enumerate(options):
        if not isinstance(opt, dict):
            raise ResultError(f"{where}.options[{j}] must be an object")
        parsed.append(Option(
            _text(opt.get("key"), f"{where}.options[{j}].key"),
            _text(opt.get("label"), f"{where}.options[{j}].label"),
            bool(opt.get("recommended", False)),
        ))
    evidence = raw.get("evidence", [])
    if not isinstance(evidence, list) or not all(isinstance(e, str) for e in evidence):
        raise ResultError(f"{where}.evidence must be a list of strings")
    kind = raw.get("kind", "other")
    if kind not in QUESTION_KINDS:
        raise ResultError(f"{where}.kind {kind!r} must be one of {', '.join(QUESTION_KINDS)}")
    return Question(qid, _text(raw.get("question"), f"{where}.question"), parsed, [nfc(e) for e in evidence], kind)


def read_result(path: Path, refine: bool = False) -> StepResult:
    """Parse and validate a step's `result.json`; anything off raises `ResultError`."""
    path = Path(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ResultError(f"result file not found: {path}") from None
    except (ValueError, UnicodeDecodeError) as exc:
        raise ResultError(f"result file is not valid JSON: {exc}") from None
    if not isinstance(raw, dict):
        raise ResultError("result must be a JSON object")
    status = raw.get("status")
    if status not in RESULT_STATUSES:
        raise ResultError(f"status {status!r} must be one of {', '.join(RESULT_STATUSES)}")
    if status == "unchanged" and not refine:
        raise ResultError("status 'unchanged' is only valid in a refine round")
    summary = raw.get("summary", "")
    if not isinstance(summary, str):
        raise ResultError("summary must be a string")
    questions_raw = raw.get("questions", [])
    if not isinstance(questions_raw, list):
        raise ResultError("questions must be a list")
    questions = [_parse_question(q, i) for i, q in enumerate(questions_raw)]
    ids = [q.id for q in questions]
    if len(set(ids)) != len(ids):
        raise ResultError("duplicate question ids")
    if status == "needs_human" and not questions:
        raise ResultError("status 'needs_human' needs at least one question")
    effort = raw.get("effort")
    if status == "effort_change" and not (isinstance(effort, str) and effort.strip()):
        raise ResultError("status 'effort_change' needs an effort level")
    signals, findings, risks = raw.get("signals", {}), raw.get("findings", []), raw.get("risks", [])
    if not isinstance(signals, dict) or not isinstance(findings, list) or not isinstance(risks, list):
        raise ResultError("signals must be an object; findings and risks must be lists")
    dispositions = raw.get("dispositions", [])
    if not isinstance(dispositions, list):
        raise ResultError("dispositions must be a list")
    facts = _parse_lines(raw.get("facts", []), "facts", FACTS_PER_RESULT)
    code_map = _parse_lines(raw.get("code_map", []), "code_map", CODE_MAP_PER_RESULT)
    lessons_cited = _parse_lesson_ids(raw.get("lessons_cited", []))
    return StepResult(
        status=status,
        summary=nfc(summary),
        next=raw.get("next"),
        effort=effort,
        reason=raw.get("reason"),
        questions=questions,
        can_continue_without=bool(raw.get("can_continue_without", False)),
        signals=nfc(signals),
        findings=nfc(findings),
        dispositions=nfc(dispositions),
        risks=nfc(risks),
        facts=facts,
        code_map=code_map,
        lessons_cited=lessons_cited,
    )


# --------------------------------------------------------------------------- inbox files


def _one_line(text: str) -> str:
    return re.sub(r"\s*\n\s*", " ", text).strip()


def render_inbox(path: Path, title: str, summary: str, questions: list[Question], extra: str = "") -> None:
    """Write the inbox Markdown: title, summary, `extra`, then one `###` block per question."""
    parts = [f"# {_one_line(title)}", summary.strip()]
    if extra.strip():
        parts.append(extra.strip())
    for q in questions:
        block = [f"### {q.id} — {_one_line(q.question)}", "", f"Kind: {q.kind}"]
        if q.evidence:
            block += ["Evidence:", *[f"- {e}" for e in q.evidence]]
        if q.options:
            block += ["Options:", *[f"- {o.key}: {o.label}{' (recommended)' if o.recommended else ''}" for o in q.options]]
        block += ["", "Answer: "]
        parts.append("\n".join(block))
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(nfc("\n\n".join(p for p in parts if p) + "\n"), encoding="utf-8")


def _sections(text: str) -> list[tuple[str, int, int]]:
    """`(question id, body start, body end)` for each `### id — …` block."""
    found = list(HEADING.finditer(text))
    return [(m.group(1), m.end(), found[i + 1].start() if i + 1 < len(found) else len(text)) for i, m in enumerate(found)]


def parse_answers(path: Path) -> dict[str, str]:
    """Each question id mapped to what follows its `Answer:` line (`""` when still blank)."""
    text = nfc(Path(path).read_text(encoding="utf-8"))
    answers = {}
    for qid, start, end in _sections(text):
        body = text[start:end]
        found = ANSWER_LINE.search(body)
        answers[qid] = (found.group(1) + body[found.end():]).strip() if found else ""
    return answers


def write_answers(path: Path, answers: dict[str, str]) -> None:
    """Put `answers` after the matching `Answer:` lines, leaving the rest of the file as it is."""
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    sections = _sections(text)
    for index in range(len(sections) - 1, -1, -1):
        qid, start, end = sections[index]
        if qid not in answers:
            continue
        body = text[start:end]
        found = ANSWER_LINE.search(body)
        head = body[:found.start()] if found else body.rstrip("\n") + "\n\n"
        tail = "\n" if index == len(sections) - 1 else "\n\n"
        text = text[:start] + head + f"Answer: {answers[qid]}" + tail + text[end:]
    path.write_text(nfc(text), encoding="utf-8")


def _questions_from_state(raw: list[dict]) -> list[Question]:
    return [
        Question(
            q["id"], q.get("question", ""),
            [Option(o["key"], o.get("label", ""), bool(o.get("recommended"))) for o in q.get("options", [])],
            list(q.get("evidence", [])), q.get("kind", "other"),
        )
        for q in raw
    ]


def question_objects(raw: list[dict]) -> list[Question]:
    """Questions stored in an event (as dicts) turned back into `Question`s."""
    return _questions_from_state(raw)


# --------------------------------------------------------------------------- notifications


def _log_failure(run: Run | None, notifier: str, error: str) -> None:
    if run is not None:
        run.emit("notify_failed", notifier=notifier, error=error)
    else:
        sys.stderr.write(f"agentflow: notifier {notifier} failed: {error}\n")


def _applescript_quote(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _send(entry: dict, title: str, message: str) -> None:
    kind = entry.get("type")
    if kind == "terminal_bell":
        sys.stderr.write("\a")
        sys.stderr.flush()
    elif kind == "macos":
        script = f"display notification {_applescript_quote(message)} with title {_applescript_quote(title)}"
        done = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=NOTIFY_TIMEOUT_S, check=False)
        if done.returncode != 0:
            raise NotifyError((done.stderr or "").strip() or f"osascript exited {done.returncode}")
    elif kind == "webhook":
        env = entry.get("url_env") or ""
        url = os.environ.get(env, "") if env else ""
        if not url:
            raise NotifyError(f"environment variable {env or '(url_env not set)'} is empty")
        body = json.dumps({"title": title, "message": message}, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(request, timeout=NOTIFY_TIMEOUT_S) as response:
            response.read()
    else:
        raise NotifyError(f"unknown notifier type {kind!r}")


def notify(cfg: Config, title: str, message: str, run: Run | None = None) -> None:
    """Tell the human through every enabled `hitl.notify` entry. A failing notifier is logged, never raised."""
    message = _one_line(message)[:MESSAGE_MAX]
    for entry in cfg.hitl.get("notify", []):
        if not isinstance(entry, dict) or not entry.get("enabled", True):
            continue
        try:
            _send(entry, title, message)
        except (OSError, subprocess.SubprocessError, ValueError, NotifyError) as exc:
            _log_failure(run, str(entry.get("type")), str(exc) or type(exc).__name__)


def notify_run(cfg: Config, run: Run, message: str) -> None:
    """`notify` with the run's configured title."""
    notify(cfg, _title_for(cfg, run), message, run=run)


def _title_for(cfg: Config, run: Run) -> str:
    for entry in cfg.hitl.get("notify", []):
        if isinstance(entry, dict) and entry.get("type") == "macos" and entry.get("title"):
            return str(entry["title"]).replace("{run}", run.id)
    return DEFAULT_TITLE.replace("{run}", run.id)


# --------------------------------------------------------------------------- stopping for a human


EVENT_EXTRA_MAX = 4000  # the stop report kept in `hitl_raised`: the last characters, where a log's cause is


def raise_hitl(cfg: Config, run: Run, kind: str, key: str, summary: str, questions: list[Question] | tuple = (), extra: str = "") -> Path:
    """Stop the run for a human: write the inbox file, record `hitl_raised`, notify. Returns the inbox path."""
    questions = list(questions)
    how = [f"Run `{run.id}` is waiting on `{key}` ({kind}).", _how_to_go_on(run, kind, key, bool(questions), cfg)]
    path = run.inbox_path(key)
    render_inbox(path, f"{run.id}: {key}", summary, questions, extra="\n\n".join(["\n".join(how), extra]))
    run.emit("hitl_raised", kind=kind, key=key, phase=key.split("/")[0], summary=summary,
             questions=[asdict(q) for q in questions], inbox=str(path), extra=extra[-EVENT_EXTRA_MAX:])
    from agentflow.autonomy import runner_settles  # the runner tries this stop first; it notifies if it cannot settle it

    if not runner_settles(cfg, run, kind, key, [asdict(q) for q in questions]):
        notify(cfg, _title_for(cfg, run), summary or f"{key} needs a decision", run=run)
    return path


def park_hitl(cfg: Config, run: Run, kind: str, key: str, summary: str, questions: list[Question],
              paths: list[str] | tuple = ()) -> Path:
    """Ask a human without stopping: write the inbox file, record `step_parked`, notify.

    The run goes on with tasks that do not depend on this one; it stops for this question only when no
    runnable task remains. `paths` are the files the parked step changed so far: other tasks' commits
    leave them out. Returns the inbox path.
    """
    how = [f"Run `{run.id}` parked `{key}` ({kind}) and goes on with the tasks that do not depend on it.",
           f"Fill in the `Answer:` lines below, or run `agentflow answer {run.id} --key {key}`."]
    path = run.inbox_path(key)
    render_inbox(path, f"{run.id}: {key}", summary, questions, extra="\n".join(how))
    run.emit("step_parked", kind=kind, key=key, phase=key.split("/")[0], summary=summary,
             questions=[asdict(q) for q in questions], inbox=str(path), paths=list(paths))
    notify(cfg, _title_for(cfg, run), f"parked: {summary or key}", run=run)
    return path


def _how_to_go_on(run: Run, kind: str, key: str, has_questions: bool, cfg: Config) -> str:
    """The inbox line that says what each way out of this stop does."""
    if run.state.get("pipeline") == RETRO_PIPELINE:
        return (f"A retro run cannot be resumed, approved or answered: `agentflow cancel {run.id}`, then run "
                "`agentflow retro` again (it reads the same runs, since this retro did not finish).")
    scoped = rejected_scope(run, key.split("/")[0], key)
    back = f"task {scoped[1]}" if scoped else "the phase"
    reject = f"`agentflow reject {run.id} {key} --note \"...\"` sends {back} back for a refine round."
    if has_questions:
        return f"Fill in the `Answer:` lines below, or run `agentflow answer {run.id}`."
    if kind in CLEARED_BY_APPROVE:
        return f"`agentflow approve {run.id} {key}` accepts it and the run goes on; {reject}"
    if kind == "budget_exceeded" and run.state.get("soft_budget_usd") is not None:
        hard = float(run.state.get("budget_usd") or 0.0)
        return (f"The run stopped at its hard budget of ${hard:.2f}. `agentflow approve {run.id} {key}` sets the hard "
                f"budget to the spend so far plus ${cfg.defaults.run_budget_usd:.2f} (one soft budget, "
                f"`defaults.run_budget_usd`) and goes on, then asks again (add `--budget <usd>` to set the hard budget "
                f"yourself); {reject}")
    if kind == "budget_exceeded":
        return (f"`agentflow approve {run.id} {key}` goes on with one more budget's worth of spend, then asks again "
                f"(add `--budget <usd>` to set the limit yourself); {reject}")
    if kind == "guard_violation":
        return (f"Revert or fix the files yourself, then `agentflow approve {run.id} {key}` restarts the step in a "
                f"fresh session (the runner checks the tree again first); {reject}")
    if kind == "gate_failed":
        return f"Fix the cause, then `agentflow approve {run.id} {key}` runs the gates again; {reject}"
    if kind == "blocked_repeat":
        return (f"This session blocked again with nothing changed, so a plain approve is refused. Fix the cause, then "
                f"`agentflow retry {run.id} {key}` starts a fresh session, or `agentflow approve {run.id} {key} --note "
                f"\"...\"` tells this session what changed; {reject}")
    if kind == "eval_regression":
        return (f"A regression blocks the change, and `eval/baseline.json` is never edited to pass it. Fix the cause "
                f"(or let the agent try again), then `agentflow approve {run.id} {key}` resumes the fix session (or runs the "
                f"gates again when no session started) with fresh fix attempts; {reject}")
    if kind == "red_test_passes":
        return (f"`agentflow approve {run.id} {key}` resumes the step's session to make the test fail for the bug's reason, "
                f"and the runner checks it again; {reject}")
    if kind == "review_unresolved":
        return (f"Fix the code yourself if you want, then `agentflow approve {run.id} {key}` gives the reviewer one last "
                f"look, runs the gates again and commits the task, with any finding still open listed in the commit body; "
                f"{reject}")
    if kind == "dirty_tree":
        return f"Commit or remove the changes yourself, then `agentflow approve {run.id} {key}` runs the step again; {reject}"
    if kind == CONFORMANCE_FAILED:
        return _conformance_way_on(run, key, cfg)
    return (f"Fix what the stop needs, then `agentflow approve {run.id} {key}` resumes the step's agent session where "
            f"it stopped (it is not marked done); {reject}")


def _conformance_way_on(run: Run, key: str, cfg: Config) -> str:
    """For a `conformance_failed` stop: the `agentflow retry` that runs each missing step the run walked again (it
    clears this stop, so `key` audits again); for a step this run never walked, that no retry can bring it; and
    the retry of `key` itself, which audits again."""
    from agentflow.conformance import missing_steps  # it imports this module

    checks = [e for e in run.events.read() if e["type"] == "conformance_checked" and e.get("key") == key]
    walked, never = missing_steps(run.state.get("steps") or {}, checks[-1] if checks else None)
    lines = ["Nothing was pushed."]
    lines += [f"`agentflow retry {run.id} {step}` runs `{step}` again, then `{key}` audits again." for step in walked]
    lines += [f"This run's lane never ran `{step}`, so no retry can bring its evidence: change the pack's `policy` "
              f"(or the lane's steps), then `agentflow retry {run.id} {key}` audits again." for step in never]
    lines.append(f"Once the evidence is there some other way, `agentflow retry {run.id} {key}` audits again. "
                 "An approval does not waive an obligation: the audit runs again and stops again.")
    return "\n".join(lines)


def _not_waiting(run: Run, target: str) -> NotWaiting:
    waiting = run.state["waiting"]
    now = f" (it is waiting on {waiting['key']!r})" if waiting else ""
    return NotWaiting(f"{run.id} is not awaiting approval of {target!r}{now}")


def _refuse_ended(run: Run) -> None:
    """An ended run (done, failed, cancelled) has nothing left to approve, reject or answer."""
    status = run.state["status"]
    if status in TERMINAL:
        raise NotWaiting(f"{run.id} is {status}; there is nothing to approve, reject or answer")
    refuse_retro(run)


def refuse_retro(run: Run) -> None:
    """`NotWaiting` for a retro run: approving, rejecting, answering or retrying one would record a way on that no
    runner takes (a retro is never resumed), leaving it running with nothing behind it. Checked before any event."""
    if run.state.get("pipeline") == RETRO_PIPELINE:
        raise NotWaiting(f"{run.id} is a retro run, which cannot be resumed; {RETRO_WAY_OUT}")


def _resolve(run: Run, target: str) -> tuple[str, str]:
    """`("phase", name)` or `("checkpoint", key)` for what the run is waiting on, else `NotWaiting`."""
    _refuse_ended(run)
    waiting = run.state["waiting"]
    if waiting is None:
        raise _not_waiting(run, target)
    if waiting["kind"] == "phase_approval":
        if target in (waiting["phase"], waiting["key"]):
            return "phase", waiting["phase"]
        raise _not_waiting(run, target)
    if target == waiting["key"]:
        return "checkpoint", waiting["key"]
    raise _not_waiting(run, target)


def pending_item(run: Run, key: str | None = None) -> dict | None:
    """The question set a human can answer now: the wait on `key` or a parked one (the run's wait, else
    the first parked item, when `key` is None). None when there is nothing to answer there."""
    waiting = run.state["waiting"]
    parked = run.state.get("parked") or {}
    if waiting is not None and waiting["kind"] != "phase_approval" and key in (None, waiting["key"]):
        return waiting
    if key is None:
        return next(iter(parked.values()), None)
    return parked.get(key)


def answer(run: Run, key: str, answers: dict[str, str], by: str, record: bool = False) -> bool:
    """Record answers to the questions the run waits on, or to a parked step's questions. True once every
    question has one (the wait clears). `record` asks for the answers to be written into the spec as a
    decision (as a `spec_gap` answer always is)."""
    answers = {qid: nfc(value).strip() for qid, value in answers.items()}
    with run.locked():
        _refuse_ended(run)
        waiting = pending_item(run, key)
        if waiting is None:
            raise NotWaiting(f"{run.id} is not waiting for answers on {key!r}")
        ids = [q["id"] for q in waiting["questions"]]
        if not ids:
            raise NotWaiting(f"{key!r} has no questions to answer; use approve or reject")
        unknown = [qid for qid in answers if qid not in ids]
        if unknown:
            raise ValueError(f"unknown question id(s) {', '.join(unknown)}; the questions are {', '.join(ids)}")
        path = run.inbox_path(key)
        if not path.exists():
            render_inbox(path, f"{run.id}: {key}", waiting["summary"], _questions_from_state(waiting["questions"]))
        if answers:
            write_answers(path, answers)
        recorded = parse_answers(path)
        final = {qid: recorded.get(qid, "") for qid in ids}
        if not all(final.values()):
            return False
        extra = {"record": True} if record else {}
        run.emit("hitl_answered", by=by, key=key, answers=final, **extra)
        return True


def approve(run: Run, target: str, by: str, budget: float | None = None, hold: bool = False,
            note: str | None = None, budget_step: float | None = None) -> bool:
    """Approve a phase (`plan` or `plan_approved`), clear an inner checkpoint, or resume a runner stop.

    `hold` rests an approved phase as ready (`phase_ready`) instead of leaving the run to go on, and True is
    returned when it did. It means nothing for the other two: those sit inside a phase, which must finish.
    A `type: checkpoint` step or a repeat group past `max_rounds` is cleared (`checkpoint_cleared`).
    Any other stop is one the runner raised on an unfinished step (blocked, fix attempts exhausted,
    timeout, ...): `hitl_approved` puts that step back to resumable, so the runner resumes its session
    and still verifies and commits it. A `budget_exceeded` stop (the hard budget) is a request for approval:
    `budget` sets the hard budget exactly; with none, the hard budget becomes the spend so far plus
    `budget_step` (the callers pass the pack's `defaults.run_budget_usd`, the soft budget), else plus the
    run's own soft budget, so the run goes on and asks again after that. The soft budget is left alone. A run
    started before the soft budget existed allows one more of its one budget's worth, as it always did.

    `note` goes to the agent that the approval resumes. A `blocked_repeat` stop needs one: the session already
    blocked again with nothing changed, so a plain approval would only repeat that.
    """
    note = nfc(note).strip() if note else None
    if budget is not None and not math.isfinite(budget):
        raise ValueError(f"a budget must be a finite number of USD, not {budget!r}")
    with run.locked():
        kind, name = _resolve(run, target)
        waiting_kind = (run.state["waiting"] or {}).get("kind")
        if note and (kind == "phase" or waiting_kind in CLEARED_BY_APPROVE):
            raise ValueError(f"no agent resumes from {name!r}, so nothing would read the note; use `agentflow reject`")
        if waiting_kind == "blocked_repeat" and not note:
            raise NotWaiting(f"{name!r} blocked again with nothing changed, so another plain approval would repeat it. "
                             f"Fix the cause, then `agentflow retry {run.id} {name}` for a fresh session, "
                             f"or approve with --note to tell this session what changed.")
        if budget is not None:
            spend = float(run.state["spend_usd"] or 0.0)
            if budget <= spend:
                raise ValueError(f"a budget of ${budget:.2f} is not above the ${spend:.2f} {run.id} has spent")
        if kind == "phase":
            if budget is not None:
                run.emit("run_updated", by=by, budget_usd=budget)
            run.emit("phase_approved", by=by, phase=name)
            if hold:
                run.emit("phase_ready", by=by, phase=name)
            return run.state["status"] == "ready"
        waiting = run.state["waiting"]
        if waiting["questions"]:
            raise NotWaiting(f"{name!r} is waiting for answers, not an approval; use `agentflow answer {run.id}`")
        if waiting["kind"] == "budget_exceeded" and budget is None:
            spend = float(run.state["spend_usd"] or 0.0)
            soft = run.state.get("soft_budget_usd")
            if soft is None:  # a run started before the soft budget: one more of its one budget
                budget = spend + float(run.state["budget_usd"] or spend)
            else:
                sound = budget_step is not None and math.isfinite(budget_step) and budget_step > 0
                step = budget_step if sound else soft
                budget = spend + float(step)
        if budget is not None:
            run.emit("run_updated", by=by, budget_usd=budget)
        if waiting["kind"] in CLEARED_BY_APPROVE:
            run.emit("checkpoint_cleared", by=by, key=name)
        else:
            extra = {"note": note} if note else {}
            run.emit("hitl_approved", by=by, key=name, kind=waiting["kind"], summary=waiting["summary"], **extra)
        return False


def task_scope(phase: str, key: str) -> tuple[str, int] | None:
    """`("<phase>/<step>/task-<n>", n)` when `key` is a stop inside plan task n of `phase`, else None."""
    match = re.match(rf"^{re.escape(phase)}/[^/]+/task-(\d+)(?=/|$)", key)
    return (match.group(0), int(match.group(1))) if match else None


def rejected_scope(run: Run, phase: str, key: str) -> tuple[str, int] | None:
    """`task_scope`, unless the task was one of a batch of several (`plan_batched`): the batch's members before
    its last were committed without a review of their own, so a stop in the batch sends the whole phase back."""
    scoped = task_scope(phase, key)
    if scoped is None:
        return None
    prefix = scoped[0].rsplit("/", 1)[0]
    batched = [e for e in run.events.read() if e["type"] == "plan_batched" and e.get("key") == prefix]
    batches = (batched[-1].get("batches") or []) if batched else []
    if any(isinstance(b, list) and len(b) > 1 and scoped[1] in b for b in batches):
        return None
    return scoped


def reject(run: Run, target: str, by: str, note: str) -> None:
    """Reject a phase (or the phase an inner checkpoint belongs to): record the note and start a refine round.
    A stop inside a plan task sends only that task back (`scope`), unless the task was batched with others;
    any other target refines the whole phase."""
    note = nfc(note).strip()
    if not note:
        raise ValueError("a rejection needs a note")
    with run.locked():
        kind, name = _resolve(run, target)
        phase = name if kind == "phase" else (run.state["waiting"]["phase"] or name.split("/")[0])
        scoped = rejected_scope(run, phase, name) if kind == "checkpoint" else None
        open_refine(run, phase, by, note, {"key": name} if kind == "checkpoint" else {},
                    scope=scoped[0] if scoped else None)


def open_refine(run: Run, phase: str, by: str, note: str, extra: dict | None = None,
                scope: str | None = None) -> None:
    """Send `phase` back for a refine round: what a rejection does, and what the runner does with findings.
    With `scope` (`<phase>/<step>/task-<n>`) only that plan task's steps are refined.

    A reconcile round (`extra["kind"] == "reconcile"`, which the runner opens on a doubt review's findings) is not a
    human's round: its `round` stays the phase's current one, and `reconcile_round` counts the reconcile rounds since
    the last human round, so a human's first rejection after them is still round 1."""
    from agentflow.doubt import RECONCILE, next_round  # it imports this module

    note = nfc(note).strip()
    extra = dict(extra or {})
    current = run.state["phases"].get(phase, {}).get("round", 0)
    if extra.get("kind") == RECONCILE:
        round_, reconcile_round = current, next_round(run.events.read(), phase)
        extra["reconcile_round"] = reconcile_round
        note_path = run.refine_note_path(phase, f"{round_}-r{reconcile_round}")
        title = f"Reconcile round {reconcile_round} of {phase}"
    else:
        round_ = current + 1
        note_path = run.refine_note_path(phase, round_)
        title = f"Refine round {round_} of {phase}"
    note_path.parent.mkdir(parents=True, exist_ok=True)
    note_path.write_text(nfc(f"# {title}\n\nRejected by {by}.\n\n{note}\n"), encoding="utf-8")
    scoped = {"scope": scope} if scope else {}
    run.emit("phase_rejected", by=by, phase=phase, round=round_, note=note, **extra, **scoped)


def remind_due(cfg: Config, repo: Path, now: datetime | None = None) -> list[str]:
    """Notify again about every run that has waited `hitl.remind_after_h` hours since its wait began or
    since its last reminder (`hitl_reminded`); returns their ids. Only a reminder: a wait never times out
    into a decision. Safe to call often and from several processes (the check and the event share a lock)."""
    hours = cfg.hitl.get("remind_after_h")
    if not isinstance(hours, (int, float)) or isinstance(hours, bool) or hours <= 0:
        return []
    now = now or datetime.now(UTC)
    due: list[str] = []
    for run in list_runs(repo):
        if run.state["status"] in TERMINAL or run.state["waiting"] is None:
            continue
        with run.locked():
            waiting = run.state["waiting"]
            if run.state["status"] in TERMINAL or waiting is None:
                continue
            last = parse_iso(waiting.get("reminded_at") or waiting.get("since"))
            if last is None or now - last < timedelta(hours=hours):
                continue
            run.emit("hitl_reminded", key=waiting["key"], kind=waiting["kind"], at=now.isoformat(timespec="milliseconds"))
        since = waiting.get("since") or "?"
        notify_run(cfg, run, f"Reminder: still waiting on {waiting['key']} ({waiting['kind']}) since {since}. "
                             f"{waiting.get('summary') or ''}")
        due.append(run.id)
    return due


def inbox_items(repo: Path) -> list[dict]:
    """One dict per thing a human can act on: each run's wait, then its parked questions (kind `parked`)."""
    items = []
    for run in list_runs(repo):
        waiting = run.state["waiting"]
        if run.state["status"] in TERMINAL:
            continue
        path = run.inbox_path(waiting["key"]) if waiting and waiting["key"] else None
        if waiting is not None:
            items.append({
                "run_id": run.id,
                "pipeline": run.state["pipeline"],
                "status": run.state["status"],
                "kind": waiting["kind"],
                "key": waiting["key"],
                "phase": waiting["phase"],
                "summary": waiting["summary"],
                "questions": waiting["questions"],
                "since": waiting["since"],
                "inbox_path": str(path) if path is not None and path.exists() else None,
            })
        for item in (run.state.get("parked") or {}).values():
            path = run.inbox_path(item["key"])
            items.append({
                "run_id": run.id, "pipeline": run.state["pipeline"], "status": run.state["status"], "kind": "parked",
                "key": item["key"], "phase": item["phase"], "summary": item["summary"],
                "questions": item["questions"], "since": item["since"],
                "inbox_path": str(path) if path.exists() else None,
            })
    return items
