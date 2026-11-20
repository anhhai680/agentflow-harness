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

from . import pack, safeio
from .config import CONFORMANCE_FAILED, QUESTION_KINDS, RECONCILE_CHANGED, RETRO_PIPELINE, Config
from .events import nfc, parse_iso
from .park import radius_line
from .state import TERMINAL, Run, list_runs

RESULT_STATUSES = ("done", "blocked", "needs_human", "effort_change", "unchanged")
# What a resolver may name as a stop's cause (`StepResult.stop_class`). Advisory: the runner acts on `flaky` and
# `environment` (`autonomy.Resolver`) and records the rest.
STOP_CLASSES = ("environment", "flaky", "guard_false_positive", "spec_gap", "impact", "repeated_failure")
# The stop a resolver's `environment` becomes when the machine checks fail (`autonomy.Resolver._hand_over`): one inbox
# item, which `approve` checks again first and `preflight.recheck_waiting` approves once the checks pass.
ENVIRONMENT = "environment"
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
CAP_STOPS = ("turn_cap", "budget_cap")  # a session that hit its turn or spend cap (`lanes.CAP_KINDS`)
# The stops a plan task may park (`hitl.park_kinds`). `budget_exceeded` and `environment` are the run's own, not a
# task's, so they are never parked: they end or pause the whole run.
PARKABLE = ("needs_human", "blocked", "blocked_repeat", "gate_failed", "fix_attempts_exhausted", "review_unresolved",
            "guard_violation", "step_timeout", "turn_cap", "budget_cap")
# A stop on one of a plan task's own steps. A resolver's stop (`.../implement/resolve-1`) has a longer key.
TASK_STOP = re.compile(r"/task-\d+/(implement|review|commit)$")


def park_kinds(cfg: Config) -> tuple[str, ...]:
    """The stop kinds a task may park: `hitl.park_kinds`, or every `PARKABLE` one when unset. Only `PARKABLE` kinds
    count, and a value that is not a list (which `validate` refuses) parks nothing."""
    kinds = cfg.hitl.get("park_kinds")
    if kinds is None:
        return PARKABLE
    if not isinstance(kinds, (list, tuple)):
        return ()
    return tuple(kind for kind in kinds if kind in PARKABLE)


def parks_first(cfg: Config, kind: str, key: str) -> bool:
    """Whether a stop is parked as it is raised: the setting is on (a bare `true`), the kind is one a task may
    park, and the key is a plan task's own step."""
    return (cfg.hitl.get("park_independent") is True and kind in park_kinds(cfg)
            and TASK_STOP.search(key) is not None)


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
    stop_class: str | None = None  # a resolver's class of the stop's cause: one of `STOP_CLASSES`, else None


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
        stop_class=_stop_class(raw.get("stop_class")),
    )


def _stop_class(raw: Any) -> str | None:
    """`stop_class` is advisory, so it never makes a result unreadable: anything but one of `STOP_CLASSES` is None."""
    return raw if isinstance(raw, str) and raw in STOP_CLASSES else None


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


DIGEST_TITLE = "agentflow"
DIGEST_FILE = "notify_digest.txt"  # in the state dir: the digest line last sent
DIGEST_MAX_BYTES = 1024
PROGRESSING = ("running", "refining")  # a run no one waits on and that is neither done, paused nor ready


def digest_text(repo: Path) -> str:
    """The one machine-wide line of `hitl.digest`: the items that need a person, and the runs going on.
    An item "holds a run" when it is a run's wait; a parked item needs a person but holds nothing, and its run still
    counts as progressing."""
    runs = list_runs(repo)
    items = inbox_items(repo, runs)
    holding = sum(1 for item in items if item["kind"] != "parked")
    going = sum(1 for run in runs if run.state["status"] in PROGRESSING)
    count = len(items)
    need = "Nothing needs you" if not count else f"{count} item{'s' if count != 1 else ''} need{'' if count != 1 else 's'} you"
    held = f" ({holding} hold{'s' if holding == 1 else ''} a run)" if holding else ""
    return f"{need}{held}; {going} run{'' if going == 1 else 's'} progressing"


def _digest_unchanged(path: Path, text: str) -> bool:
    try:
        last = safeio.read_bytes(path, DIGEST_MAX_BYTES)
    except (OSError, ValueError):
        return False  # it can't be read: send the digest
    return last is not None and last.decode("utf-8", "replace") == text


def notify_run(cfg: Config, run: Run, message: str) -> None:
    """`notify` with the run's configured title. With `hitl.digest` on it sends `digest_text` instead of `message`,
    and only when that differs from the line last sent (kept in the state dir): a note that changes no count is
    never sent."""
    if cfg.hitl.get("digest") is not True:
        notify(cfg, _title_for(cfg, run), message, run=run)
        return
    text = digest_text(run.repo)
    path = pack.state_dir(run.repo) / DIGEST_FILE
    if _digest_unchanged(path, text):
        return
    notify(cfg, DIGEST_TITLE, text, run=run)
    try:
        safeio.write_text(path, text)
    except (OSError, ValueError) as exc:
        _log_failure(run, "digest", f"could not keep the digest sent: {exc}")


def _title_for(cfg: Config, run: Run) -> str:
    for entry in cfg.hitl.get("notify", []):
        if isinstance(entry, dict) and entry.get("type") == "macos" and entry.get("title"):
            return str(entry["title"]).replace("{run}", run.id)
    return DEFAULT_TITLE.replace("{run}", run.id)


# --------------------------------------------------------------------------- stopping for a human


EVENT_EXTRA_MAX = 4000  # the stop report kept in `hitl_raised`: the last characters, where a log's cause is


def raise_hitl(cfg: Config, run: Run, kind: str, key: str, summary: str, questions: list[Question] | tuple = (),
               extra: str = "", notify: bool | None = None) -> Path:
    """Stop the run for a human: write the inbox file, record `hitl_raised`, notify. Returns the inbox path.

    `notify`: None is the usual rule (quiet while the runner settles the stop itself, or while a plan task's stop may
    still park: the runner tells a human if it cannot settle or park it); True always tells a human, False never does
    (a stop a human was already told of)."""
    questions = list(questions)
    counted = {"times": _times_back(run, key)} if kind == "blocked_repeat" else {}
    how = [f"Run `{run.id}` is waiting on `{key}` ({kind}).",
           _how_to_go_on(run, kind, key, bool(questions), cfg, **counted)]
    path = run.inbox_path(key)
    render_inbox(path, f"{run.id}: {key}", summary, questions, extra="\n\n".join(["\n".join(how), extra]))
    if notify is None:
        from agentflow.autonomy import runner_settles  # the runner tries this stop first; it notifies if it cannot settle it

        notify = not runner_settles(cfg, run, kind, key, [asdict(q) for q in questions]) and not parks_first(cfg, kind, key)
    run.emit("hitl_raised", kind=kind, key=key, phase=key.split("/")[0], summary=summary,
             questions=[asdict(q) for q in questions], inbox=str(path), extra=extra[-EVENT_EXTRA_MAX:],
             notified=bool(notify), **counted)
    if notify:
        notify_run(cfg, run, notice_text({"key": key, "summary": summary, **counted}) or f"{key} needs a decision")
    return path


def _times_back(run: Run, key: str) -> int:
    """How many times this `blocked_repeat` stop has been raised on `key`: 1 plus the approvals a human gave a
    `blocked` or `blocked_repeat` stop of `key` since its last `agentflow retry`. The summary is not compared: it is
    model prose, and `blocked_repeat` already means blocked again with the tree unchanged. A raise the runner settled
    itself (`AUTO_BY`) and every other kind are not counted."""
    from agentflow.autonomy import AUTO_BY  # autonomy imports this module

    approved = 0
    for event in run.events.read():
        if event.get("key") != key:
            continue
        if event["type"] == "step_retry":
            approved = 0
        elif (event["type"] == "hitl_approved" and event.get("by") != AUTO_BY
                and event.get("kind") in ("blocked", "blocked_repeat")):
            approved += 1
    return 1 + approved


def notice_text(wait: dict) -> str:
    """The line a human is told when a stop is raised: its summary, led by how often a human's approval did not
    settle it when the same `blocked_repeat` stop came back (`wait["times"]` above 1)."""
    summary = wait.get("summary") or ""
    times = int(wait.get("times") or 1)
    if times > 1:
        return (f"{wait.get('key')} is blocked again for the same reason after {times - 1} approval(s): "
                f"{clean_fact(summary)}")
    return summary


def park_wait(cfg: Config, run: Run, wait: dict, *, blast_radius: dict, shelf: str | None, shelved: list[str],
              stop_class: str | None, extra: str = "") -> Path:
    """Park a task stop's wait away so the run goes on with the tasks that do not wait on it: rewrite its inbox file
    with the ways out and the blast radius, record `step_parked` (which clears the wait on the same key), and tell a
    person `parked <key>: <blast radius>`. `shelf` and `shelved` are the ref and the paths the task's unfinished
    work was put aside in; `extra` is the stop's report. Returns the inbox path."""
    key, kind = str(wait["key"]), str(wait["kind"])
    questions = _questions_from_state(wait.get("questions") or [])
    radius = {"blocks": list(blast_radius.get("blocks") or []), "continue": list(blast_radius.get("continue") or [])}
    line = radius_line(radius)
    how = [f"Run `{run.id}` parked `{key}` ({kind}) and goes on with the tasks that do not wait on it.",
           _parked_ways_out(run, kind, key, bool(questions)),
           *filter(None, [constraint_line(cfg, run, key, bool(questions))]),
           f"Blast radius: {line}"]
    path = run.inbox_path(key)
    summary = str(wait.get("summary") or "")
    render_inbox(path, f"{run.id}: {key}", summary, questions, extra="\n\n".join(["\n".join(how), extra]))
    run.emit("step_parked", kind=kind, key=key, phase=wait.get("phase") or key.split("/")[0], summary=summary,
             questions=[asdict(q) for q in questions], inbox=str(path), paths=[], stop_class=stop_class,
             blast_radius=radius, shelf=shelf, shelved=list(shelved))
    notify_run(cfg, run, f"parked {key}: {line}")
    return path


def _parked_ways_out(run: Run, kind: str, key: str, has_questions: bool) -> str:
    """The inbox line for a parked stop: answer it by its key, or approve it, or retry it once no runner is live."""
    if has_questions:
        return f"Fill in the `Answer:` lines below, or run `agentflow answer {run.id} --key {key}`."
    note = ' --note "..."' if kind == "blocked_repeat" else ' [--note "..."]'
    return (f"`agentflow approve {run.id} {key}{note}` settles it, and the run takes the task up again; or "
            f"`agentflow pause {run.id}`, then `agentflow retry {run.id} {key}` starts the step in a fresh session.")


def _how_to_go_on(run: Run, kind: str, key: str, has_questions: bool, cfg: Config, times: int = 1) -> str:
    """The inbox line that says what each way out of this stop does, and the constraint line (`constraint_line`) when
    a constraint gate failed on `key`. `times` above 1 is a `blocked_repeat` stop that came back after a human's
    approval."""
    way = _way_on(run, kind, key, has_questions, cfg, times)
    lifted = constraint_line(cfg, run, key, has_questions)
    return f"{way}\n\n{lifted}" if lifted else way


def constraint_line(cfg: Config, run: Run, key: str, has_questions: bool = False) -> str:
    """The inbox line for a stop a constraint gate failed on (`constraint_gates_failed`), else "": only a person lifts
    it, with one approval."""
    if has_questions or run.state.get("pipeline") == RETRO_PIPELINE or not constraint_gates_failed(cfg, run, key):
        return ""
    return f"Only a person can lift this constraint: `agentflow approve {run.id} {key} --constraint`."


def constraint_gates_failed(cfg: Config, run: Run, key: str) -> list[str]:
    """The gates of `guardrails.constraint_override.gates` whose last `gate_result` at `key`, or under `key/`, did not
    pass (a skipped result is no failure), in the config's order."""
    gates = cfg.guardrails.constraint_gates
    if not gates or not cfg.guardrails.constraint_trailer:
        return []
    last: dict[str, dict] = {}
    for event in run.events.read():
        where = str(event.get("key") or "")
        if event["type"] == "gate_result" and event.get("gate") in gates and (where == key or where.startswith(key + "/")):
            last[str(event["gate"])] = event
    return [g for g in gates if g in last and last[g].get("passed") is False and not last[g].get("skipped")]


def _way_on(run: Run, kind: str, key: str, has_questions: bool, cfg: Config, times: int = 1) -> str:
    """What each way out of a stop of this `kind` does (`_how_to_go_on`)."""
    if run.state.get("pipeline") == RETRO_PIPELINE:
        return (f"A retro run cannot be resumed, approved or answered: `agentflow cancel {run.id}`, then run "
                "`agentflow retro` again (it reads the same runs, since this retro did not finish).")
    if kind in CAP_STOPS:
        return (f"- Raise the cap once: `agentflow approve {run.id} {key}` resumes the session, and each call has its own "
                f"cap.\n"
                f"- Split the step: `agentflow approve {run.id} {key} --note \"Finish <part> as done now and stop\"`, then "
                f"add the rest as a new task.\n"
                f"- Descope: `agentflow approve {run.id} {key} --note \"Drop <part>: <why>\"`.\n"
                f"- Start over: `agentflow retry {run.id} {key}` starts a fresh session.")
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
        again = (f"This exact stop came back after {times - 1} approval(s), so a plain approval would repeat it: fix "
                 f"the cause, then `agentflow retry {run.id} {key}` starts a fresh session, or approve with --note to "
                 f"say what changed.\n\n") if times > 1 else ""
        return (f"{again}This session blocked again with nothing changed, so a plain approve is refused. Fix the cause, then "
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
    if kind == "preflight_failed":
        return (f"Fix what is listed below, then `agentflow approve {run.id} {key}` runs preflight again; nothing has "
                f"been spent yet. If a listed setup gate fails on the base branch itself, fix it there, or set "
                f"`preflight.enabled: false`; `agentflow cancel {run.id}` ends the run.")
    if kind == ENVIRONMENT:
        return ("The runner checks the machine again whenever `agentflow queue` or the dashboard is running, and "
                f"approves this itself once the checks pass. To go on now, fix it and `agentflow approve {run.id} {key}` "
                f"(it checks again first), or add `--note \"...\"` to go on without the check; `agentflow cancel {run.id}` "
                "ends the run.")
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
    """`("phase", name)` or `("checkpoint", key)` for what the run is waiting on, `("parked", key)` for a parked
    stop that is not the run's wait, else `NotWaiting`."""
    _refuse_ended(run)
    waiting = run.state["waiting"]
    if target in (run.state.get("parked") or {}) and (waiting is None or waiting.get("key") != target):
        return "parked", target
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
        demote_promoted(run, key)
        return True


def approve(run: Run, target: str, by: str, budget: float | None = None, hold: bool = False,
            note: str | None = None, budget_step: float | None = None,
            viewed: list[dict] | None = None, produced: list[dict] | None = None,
            constraint: bool = False, cfg: Config | None = None) -> bool:
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

    A parked stop (`state["parked"]`) is approved by its key the same way, by the rules of its own kind; a promoted
    wait is then parked again (`demote_promoted`), so the run goes on with the approved stop's task.

    `viewed` (`[{path, sha}]`, the artifact revisions the dashboard page showed) is informational: a phase approval
    records it on `phase_approved` when it is not empty, and any other approval ignores it.
    `produced` (`[{path, sha}]`, the newest revision of each artifact the phase produced) is recorded the same way
    when it is not None, an empty list included: that says the phase produced nothing. `signals` reads both back.

    `constraint` lifts a constraint gate (`guardrails.constraint_override`) as a person: `hitl_approved` carries
    `constraint: True`, and the runner then writes the override commit with the person's name in the pack's human-only
    trailer (`constraint.write`). Only a person does this, so it is refused, before any event is written, for the
    runner's own `by` (`AUTO_BY`), inside an agent session (`AGENTFLOW_STEP` or `AGENTFLOW_RUN` set), for a name that
    cleans to nothing, for a phase or a checkpoint, for an item with questions, and when no constraint gate failed on
    the key (`constraint_gates_failed`, read with `cfg`, else the config the run continues with).
    """
    if constraint:
        _refuse_constraint_by(by)
    note = nfc(note).strip() if note else None
    if budget is not None and not math.isfinite(budget):
        raise ValueError(f"a budget must be a finite number of USD, not {budget!r}")
    with run.locked():
        kind, name = _resolve(run, target)
        item = run.state["parked"][name] if kind == "parked" else run.state["waiting"]  # every rule reads its kind
        waiting_kind = (item or {}).get("kind")
        if note and (kind == "phase" or waiting_kind in CLEARED_BY_APPROVE):
            raise ValueError(f"no agent resumes from {name!r}, so nothing would read the note; use `agentflow reject`")
        if note and waiting_kind == "preflight_failed":
            raise ValueError(f"no agent resumes from {name!r}: on approval preflight runs again, so a note changes "
                             f"nothing; approve it without --note once the machine is fixed")
        if waiting_kind == "blocked_repeat" and not note:
            raise NotWaiting(f"{name!r} blocked again with nothing changed, so another plain approval would repeat it. "
                             f"Fix the cause, then `agentflow retry {run.id} {name}` for a fresh session, "
                             f"or approve with --note to tell this session what changed.")
        if budget is not None:
            spend = float(run.state["spend_usd"] or 0.0)
            if budget <= spend:
                raise ValueError(f"a budget of ${budget:.2f} is not above the ${spend:.2f} {run.id} has spent")
        if constraint:
            _refuse_constraint_item(run, kind, name, item, cfg)
        if kind == "phase":
            if budget is not None:
                run.emit("run_updated", by=by, budget_usd=budget)
            run.emit("phase_approved", by=by, phase=name, **({"viewed": viewed} if viewed else {}),
                     **({"produced": produced} if produced is not None else {}))
            if hold:
                run.emit("phase_ready", by=by, phase=name)
            return run.state["status"] == "ready"
        waiting = item
        if waiting["questions"]:
            how = f"agentflow answer {run.id}" + (f" --key {name}" if kind == "parked" else "")
            raise NotWaiting(f"{name!r} is waiting for answers, not an approval; use `{how}`")
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
        if waiting["kind"] in CLEARED_BY_APPROVE and kind != "parked":
            run.emit("checkpoint_cleared", by=by, key=name)
        else:
            extra = {"note": note} if note else {}
            if constraint:
                extra["constraint"] = True
            run.emit("hitl_approved", by=by, key=name, kind=waiting["kind"], summary=waiting["summary"], **extra)
        demote_promoted(run, name)
        return False


def _refuse_constraint_by(by: str) -> None:
    """`NotWaiting` unless a person, outside any agent session, approves a constraint: the name the trailer would carry
    (`clean_fact(by)[:100]`) is not empty and not the runner's, and no agent session's variables are set."""
    from agentflow.autonomy import AUTO_BY  # autonomy imports this module

    name = clean_fact(str(by or ""))[:100]
    if name.casefold() == AUTO_BY:
        raise NotWaiting("the runner cannot approve a constraint: only a person lifts it")
    if "AGENTFLOW_STEP" in os.environ or "AGENTFLOW_RUN" in os.environ:  # set for every agent session, even empty
        raise NotWaiting("a constraint cannot be approved from inside an agent session: only a person lifts it, "
                         "from their own terminal or the dashboard")
    if not name:
        raise NotWaiting("a constraint approval needs the name of the person who gives it (--by)")


def _refuse_constraint_item(run: Run, kind: str, name: str, item: dict | None, cfg: Config | None) -> None:
    """`NotWaiting` unless `name` is a stop with no questions that a constraint gate failed on."""
    if kind == "phase" or item is None:
        raise NotWaiting(f"{name!r} is a phase approval; a constraint is lifted on the stop its gate failed on")
    if item.get("questions"):
        raise NotWaiting(f"{name!r} is waiting for answers, not a constraint approval")
    if item.get("kind") in CLEARED_BY_APPROVE and kind != "parked":
        raise NotWaiting(f"{name!r} is a checkpoint, which no constraint gate stops")
    if cfg is None:
        from agentflow.config import load_config  # only a constraint approval made with no config loads one
        from agentflow.config_trust import run_config_path

        cfg = load_config(run_config_path(run))
    if not constraint_gates_failed(cfg, run, name):
        gates = ", ".join(cfg.guardrails.constraint_gates) or "none: the pack sets no guardrails.constraint_override"
        raise NotWaiting(f"no constraint gate failed on {name!r} (the constraint gates are {gates}); approve it "
                         "without --constraint")


def demote_promoted(run: Run, settled: str) -> None:
    """Put the run's promoted wait back among the parked stops once a person settled another one (`settled`), so the
    run goes on with that one's task: the last `step_parked` of the wait's key is recorded again with `demoted`.
    Nothing happens when the wait is not promoted or is `settled` itself."""
    with run.locked():
        waiting = run.state["waiting"]
        if waiting is None or not waiting.get("promoted") or waiting.get("key") == settled:
            return
        parks = [e for e in run.events.read() if e["type"] == "step_parked" and e.get("key") == waiting["key"]]
        if not parks:
            return
        again = {k: v for k, v in parks[-1].items() if k not in ("seq", "ts", "type", "by", "demoted")}
        run.emit("step_parked", **again, demoted=True)


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
        if kind == "parked":
            raise NotWaiting(f"{name} is parked: approve, answer or retry it; reject it once it is the run's wait")
        if (run.state["waiting"] or {}).get("kind") == "preflight_failed":
            raise NotWaiting("preflight has nothing to refine: approve it once the machine is fixed, or cancel the run")
        if (run.state["waiting"] or {}).get("kind") == ENVIRONMENT:
            raise NotWaiting("an environment stop has nothing to refine: approve it once the machine is fixed, "
                             "or cancel the run")
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


def inbox_items(repo: Path, runs: list[Run] | None = None) -> list[dict]:
    """One dict per thing a human can act on: each run's wait, then its parked stops (kind `parked`, with the park's
    own kind as `stop_kind` and its `blast_radius`).
    `runs` are the runs to read when the caller has loaded them already; else every run of `repo`."""
    items = []
    for run in list_runs(repo) if runs is None else runs:
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
                "stop_kind": item["kind"], "blast_radius": item.get("blast_radius"),
            })
    return items
