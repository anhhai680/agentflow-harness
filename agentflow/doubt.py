"""The rules of a reconcile round: what the runner decides, from events and file contents, in a `needs_approval: true`
phase whose review found blocking problems (`docs/agentflow-doubt-review-proposal.md`, sections 3 to 6).

- **Numbering** (`numbered`): each blocking finding of a round gets an id, `R<round>-F<n>`. An id is unique for the
  whole run of a phase, so a note, a prompt, a `disputes` value and the approval page all mean the same finding.
- **Dispositions** (`parse_dispositions`, `answered_boundary`, `unresolved`): the reconcile step classifies every
  blocking finding. The runner checks the list and fails closed: whatever it cannot trust stays unresolved.
- **Stop rules** (`stop_reason`): when another round is not worth opening. Every rule reads only the phase's events
  since its last human round (`since_human_round`), so a human who rejects the phase gets fresh rounds.
- **What a human sees** (`page_lines`, `handoff_risks`): each finding with its disposition and status.

Pure functions over event lists and the config: nothing here writes a file or an event. `autonomy` imports
`flow_control` at module level, and `flow_control` imports this module, so `autonomy` is imported inside the functions
that need it. Model text shown to a human goes through `hitl.clean_fact`, one line.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from agentflow.config import Config, Phase, Step, for_lane
from agentflow.hitl import clean_fact
from agentflow.state import Run

RECONCILE = "reconcile"  # `phase_rejected.kind` of a round the runner opens itself on blocking findings
CLASSES = ("human_decision", "contract_misread", "actionable", "trade_off", "noise")  # precedence order
RECONCILE_ONLY = "runs only in a reconcile round"
STOP_REASONS = ("no_reconcile_step", "unchanged", "disputed", "theater", "bounded")  # check order
NOTE_MAX = 300  # characters of a disposition's note
ARTIFACT_MAX = 4_000_000  # bytes of an artifact `artifact_digest` reads

DISPUTABLE = ("noise", "trade_off")  # a review may dispute these dispositions, and ending the rounds is its right
NOT_FIXED = ("noise", "trade_off", "contract_misread")  # a round whose findings all got these fixed nothing
OPEN_STATUSES = ("still open", "unresolved", "disputed", "asked a human", "claimed", "claimed, artifact unchanged")

EARLIER_LEAD = ("Earlier reviews of this artifact found these, and the reconcile step answered as shown. "
                "Check each against the artifact as it is now; dispute an answer you think wrong with `disputes`.")
STOP_LINES = {
    "unchanged": "The reconcile step left the artifact as it was, so it was not reviewed again.",
    "disputed": "The reviewer disputes a disposition: you decide.",
    "theater": "Two rounds found blocking problems and none was fixed: the review may be validating, not doubting.",
    "bounded": "The reconcile rounds are used up.",
}
CROSS_MODEL = "A cross-model opinion is yours to run."


@dataclass(frozen=True)
class Disposition:
    finding: str  # the id of a finding of the round (`R<round>-F<n>`)
    cls: str  # one of CLASSES
    note: str  # one line


# --------------------------------------------------------------------------- numbering and keys


def numbered(findings: list, rnd: int) -> list[dict]:
    """The findings as mappings, each with an `id` of `R<rnd>-F<n>`. A reviewer's own `id` is overwritten."""
    from agentflow.autonomy import as_finding

    return [{**as_finding(raw), "id": f"R{rnd}-F{n}"} for n, raw in enumerate(findings, 1)]


def _flagged(steps: list[Step], prefix: str, lane: str) -> list[str]:
    keys: list[str] = []
    for step in for_lane(steps, lane):
        key = f"{prefix}/{step.id}"
        if step.blocks_on_findings:
            keys.append(key)
        keys += _flagged(step.steps, key, lane)
    return keys


def flagged_keys(phase: Phase, lane: str) -> list[str]:
    """The keys of the phase's `blocks_on_findings` steps that `lane` walks, groups recursed into."""
    return _flagged(phase.steps, phase.name, lane)


def with_ancestors(keys: list[str]) -> list[str]:
    """Each key plus every parent key (`a/b/c` gives `a/b`), a parent before its child, no key twice. A nested
    review only runs again when its group is reset too: `run_step` returns early for a `done` group."""
    out: list[str] = []
    for key in keys:
        parts = key.split("/")
        for n in range(2, len(parts) + 1):
            parent = "/".join(parts[:n])
            if parent not in out:
                out.append(parent)
    return out


def reconcile_steps(phase: Phase, lane: str) -> list[Step]:
    """The top-level `reconciles: true` steps of the phase that `lane` walks."""
    return [step for step in for_lane(phase.steps, lane) if step.reconciles]


def round_keys(phase: Phase, lane: str) -> list[str]:
    """The steps a reconcile round runs: the reconcile steps and the `blocks_on_findings` steps, with their groups."""
    keys = [f"{phase.name}/{step.id}" for step in reconcile_steps(phase, lane)]
    return with_ancestors([*keys, *flagged_keys(phase, lane)])


def is_doubt_reviewer(cfg: Config, phase_name: str, step: Step, lane: str) -> bool:
    """Whether `step` is the review of a phase that reconciles: a flagged `review` step in a phase with a reconcile
    step. The only test for "withhold the author's notes", so `task_review`, `branch_review` and `security_review`
    are never affected."""
    phase = cfg.phases.get(phase_name)
    return bool(step.blocks_on_findings and step.role == "review" and phase is not None and reconcile_steps(phase, lane))


def in_reconcile_round(state: dict, phase: str) -> bool:
    refine = state.get("refine") or {}
    return refine.get("phase") == phase and refine.get("kind") == RECONCILE


# --------------------------------------------------------------------------- events


def _of_phase(event: dict, phase: str) -> bool:
    return event.get("phase") == phase or str(event.get("key") or "").split("/", 1)[0] == phase


def since_human_round(events: list[dict], phase: str) -> list[dict]:
    """The phase's events after its last `phase_rejected` that is not a reconcile round, or after its last
    `phase_started` when no human has rejected it."""
    human = started = -1
    for index, event in enumerate(events):
        if event.get("type") == "phase_rejected" and event.get("phase") == phase and event.get("kind") != RECONCILE:
            human = index
        elif event.get("type") == "phase_started" and event.get("phase") == phase:
            started = index
    start = human if human >= 0 else started
    return [event for event in events[start + 1:] if _of_phase(event, phase)]


def next_round(events: list[dict], phase: str) -> int:
    """The number of the phase's next reconcile round: 1 after a human round, then 2, … (`R<n>-F<m>` ids use it)."""
    return 1 + sum(1 for e in since_human_round(events, phase)
                   if e.get("type") == "phase_rejected" and e.get("kind") == RECONCILE)


@dataclass
class _Round:
    n: int
    opened: dict  # the reconcile `phase_rejected`
    after: list[dict]  # the phase's events until the next round opens: this round's reconcile step, then its review


def _split(events: list[dict]) -> tuple[list[dict], list[_Round]]:
    """The events before the first reconcile round, and then each round with the events that follow it."""
    head: list[dict] = []
    rounds: list[_Round] = []
    for event in events:
        if event.get("type") == "phase_rejected" and event.get("kind") == RECONCILE:
            rounds.append(_Round(len(rounds) + 1, event, []))
        elif rounds:
            rounds[-1].after.append(event)
        else:
            head.append(event)
    return head, rounds


def _reconciled_of(events: list[dict]) -> dict | None:
    found = [e for e in events if e.get("type") == "findings_reconciled"]
    return found[-1] if found else None


def _dispositions_of(record: dict | None) -> dict[str, tuple[str, str]]:
    """`{finding id: (class, note)}` from a `findings_reconciled` event. The runner wrote its kept dispositions, as
    `{"finding", "class", "note"}` (`cls` is read too); anything else is skipped."""
    found: dict[str, tuple[str, str]] = {}
    for raw in (record or {}).get("dispositions") or []:
        if not isinstance(raw, dict) or not isinstance(raw.get("finding"), str):
            continue
        cls = raw.get("class", raw.get("cls"))
        if cls in CLASSES and raw["finding"] not in found:
            found[raw["finding"]] = (cls, str(raw.get("note") or ""))
    return found


def _round_findings(rnd: _Round) -> list[dict]:
    return [f for f in rnd.opened.get("findings") or [] if isinstance(f, dict) and isinstance(f.get("id"), str)]


def _review_ran(events: list[dict], flagged: set[str]) -> bool:
    """Whether a flagged step finished with a new session in `events` (a carried-over review is not one)."""
    return any(e.get("type") == "step_finished" and e.get("status", "done") == "done" and e.get("key") in flagged for e in events)


def _review_findings(events: list[dict], flagged: set[str]) -> list[dict]:
    """The findings of the flagged steps as the runner last recorded them in `events`, each a mapping."""
    last: dict[str, list] = {}
    for event in events:
        if event.get("type") == "step_findings" and event.get("key") in flagged and isinstance(event.get("findings"), list):
            last[event["key"]] = event["findings"]
    return [f for findings in last.values() for f in findings if isinstance(f, dict)]


def _valid_disputes(findings: list[dict], earlier: dict[str, tuple[str, str]]) -> set[str]:
    """The ids `findings` dispute that count: an id of an earlier round whose disposition was `noise` or
    `trade_off`. Any other value is ignored, so a first review, or a value an untrusted artifact steered the reviewer
    into writing, cannot end the rounds."""
    return {f["disputes"] for f in findings
            if isinstance(f.get("disputes"), str) and f["disputes"] in earlier and earlier[f["disputes"]][0] in DISPUTABLE}


def _all_dispositions(rounds: list[_Round]) -> dict[str, tuple[str, str]]:
    merged: dict[str, tuple[str, str]] = {}
    for rnd in rounds:
        merged.update(_dispositions_of(_reconciled_of(rnd.after)))
    return merged


def _text(finding: dict) -> str:
    from agentflow.autonomy import finding_text

    return clean_fact(finding_text(finding))


def _severity(finding: dict) -> str:
    severity = clean_fact(str(finding.get("severity") or ""))
    return f"[{severity}] " if severity else ""


# --------------------------------------------------------------------------- the note and the reviewer's history


def _earlier_lines(events: list[dict]) -> list[str]:
    """`- R<n>-F<m> [<severity>] <finding> → <class>: <note>` for every round in `events`, oldest first. A round
    whose finding text is not in `events` (only its `findings_reconciled` is) shows the id, class and note."""
    head, rounds = _split(events)
    lines: list[str] = []

    def line(ident: str, finding: dict | None, answer: tuple[str, str] | None) -> str:
        what = f"{_severity(finding)}{_text(finding)}" if finding else ""
        said = f"→ {answer[0]}: {clean_fact(answer[1])}" if answer else "→ no disposition"
        return " ".join(part for part in (f"- {ident}", what, said) if part)

    for record in [e for e in head if e.get("type") == "findings_reconciled"]:
        lines += [line(ident, None, answer) for ident, answer in _dispositions_of(record).items()]
    for rnd in rounds:
        answers = _dispositions_of(_reconciled_of(rnd.after))
        lines += [line(f["id"], f, answers.get(f["id"])) for f in _round_findings(rnd)]
    return lines


def reconcile_note(findings: list[dict], history: list[dict]) -> str:
    """The note of a reconcile round: the numbered findings, then how each earlier round's findings were answered.
    `history` is the phase's events before this round opened (`since_human_round`)."""
    from agentflow.autonomy import finding_line

    lines = "\n".join(f"- {f.get('id', '?')} {finding_line(f)[2:]}" for f in findings)
    note = f"Decide each of these findings. Name its id in your `dispositions`.\n\n{lines}"
    earlier = _earlier_lines(history)
    if earlier:
        note += "\n\nEarlier rounds, and how the reconcile step answered them:\n\n" + "\n".join(earlier)
    return note


def earlier_for_review(events: list[dict], phase: str) -> str:
    """What the next reviewer is told: every round since the last human round, with its dispositions. `""` when there
    are none. It does not reuse the code reviewer's `earlier_review`, whose wording is about code."""
    lines = _earlier_lines(since_human_round(events, phase))
    return EARLIER_LEAD + "\n\n" + "\n".join(lines) if lines else ""


# --------------------------------------------------------------------------- dispositions


def parse_dispositions(raw: object, ids: set[str]) -> list[Disposition]:
    """The entries of a result's `dispositions` that can be trusted. An entry is kept only when its `finding` is a
    string in `ids`, its `class` is in CLASSES, no other entry names the same id (a duplicate keeps neither, so the
    finding is unresolved) and its `note` is a string, which is cleaned to one line and cut to NOTE_MAX."""
    if not isinstance(raw, list):
        return []
    entries = [e for e in raw if isinstance(e, dict)]
    named = Counter(e["finding"] for e in entries if isinstance(e.get("finding"), str))
    kept: list[Disposition] = []
    for entry in entries:
        finding, cls, note = entry.get("finding"), entry.get("class"), entry.get("note")
        if not isinstance(finding, str) or finding not in ids or named[finding] != 1:
            continue
        if not isinstance(cls, str) or cls not in CLASSES or not isinstance(note, str):
            continue
        kept.append(Disposition(finding, cls, clean_fact(note)[:NOTE_MAX]))
    return kept


def answered_boundary(events: list[dict], key: str, since_seq: int) -> set[str]:
    """The finding ids a human answered in this round: the `hitl_raised` on `key` after `since_seq` (the round's
    `phase_rejected`) holds a question of kind `boundary` whose `id` is the finding's id, and a later
    `hitl_answered` on `key`, by someone other than the runner, gives that question a non-empty answer."""
    from agentflow.autonomy import AUTO_BY

    found: set[str] = set()
    for index, event in enumerate(events):
        if event.get("type") != "hitl_raised" or event.get("key") != key or int(event.get("seq") or 0) <= since_seq:
            continue
        asked = {q["id"] for q in event.get("questions") or []
                 if isinstance(q, dict) and q.get("kind") == "boundary" and isinstance(q.get("id"), str)}
        for later in events[index + 1:]:
            if later.get("type") == "hitl_answered" and later.get("key") == key and later.get("by") != AUTO_BY:
                answers = later.get("answers")
                if isinstance(answers, dict):
                    found |= {qid for qid in asked if str(answers.get(qid) or "").strip()}
    return found


def unresolved(ids: list[str], dispositions: list[Disposition], answered: set[str]) -> list[str]:
    """The ids with no kept disposition, and each `human_decision` no human has answered."""
    by_id = {d.finding: d for d in dispositions}
    return [i for i in dict.fromkeys(ids) if i not in by_id or (by_id[i].cls == "human_decision" and i not in answered)]


def artifact_digest(worktree: Path, rel: str) -> str:
    """SHA-256 of the artifact's bytes, or `""` when it is missing, outside the worktree, a symlink, not a regular
    file or over ARTIFACT_MAX."""
    import hashlib

    from agentflow import safeio

    parts = Path(rel).parts
    if not rel or Path(rel).is_absolute() or ".." in parts:
        return ""
    try:
        data = safeio.read_bytes(Path(worktree) / rel, ARTIFACT_MAX)
    except (OSError, ValueError):
        return ""
    return hashlib.sha256(data).hexdigest() if data is not None else ""


# --------------------------------------------------------------------------- the stop rules


def stop_reason(cfg: Config, events: list[dict], phase: Phase, lane: str) -> str | None:
    """Why the runner should not open another reconcile round, or None to open one. It reads only the events since
    the phase's last human round, and returns the first of STOP_REASONS that holds."""
    from agentflow.autonomy import round_limit

    if not reconcile_steps(phase, lane):
        return "no_reconcile_step"
    scoped = since_human_round(events, phase.name)
    _, rounds = _split(scoped)
    records = [e for e in scoped if e.get("type") == "findings_reconciled"]
    if records and records[-1].get("before") == records[-1].get("after"):
        return "unchanged"
    if rounds:
        flagged = set(flagged_keys(phase, lane))
        if _valid_disputes(_review_findings(rounds[-1].after, flagged), _all_dispositions(rounds)):
            return "disputed"
    fruitless = 0
    for rnd in rounds:
        record = _reconciled_of(rnd.after)
        ids = [f["id"] for f in _round_findings(rnd)]
        answers = _dispositions_of(record)
        if record is not None and ids and all(i in answers and answers[i][0] in NOT_FIXED for i in ids):
            fruitless += 1
    if fruitless >= 2:
        return "theater"
    return "bounded" if len(rounds) >= round_limit(cfg) else None


# --------------------------------------------------------------------------- what a human sees


@dataclass
class _Row:
    ident: str  # `R<n>-F<m>`, or "" for a finding that never got a round
    severity: str
    text: str
    cls: str | None
    note: str
    status: str


@dataclass
class _Doubt:
    reviews: int
    rounds: int
    rows: list[_Row]

    @property
    def fixed(self) -> int:
        return sum(1 for r in self.rows if r.status == "fixed")

    @property
    def trade_offs(self) -> int:
        return sum(1 for r in self.rows if r.cls == "trade_off" and r.status == "recorded")

    @property
    def open(self) -> int:
        return sum(1 for r in self.rows if r.status in OPEN_STATUSES)


def _status(cls: str | None, ident: str, *, changed: bool, ran: bool, next_findings: list[dict], disputed: set[str],
            unresolved_ids: set[str]) -> str:
    if cls is None:
        return "unresolved"
    if ident in disputed:
        return "disputed"
    if cls == "human_decision":
        return "asked a human" if ident in unresolved_ids else "recorded"
    if cls == "actionable":
        if not changed:
            return "claimed, artifact unchanged"
        again = any(f.get("disputes") == ident for f in next_findings)
        return "fixed" if ran and not again else "claimed"
    return "recorded" if ran else "still open"


def _model(cfg: Config, events: list[dict], phase_name: str, lane: str) -> _Doubt | None:
    """The phase's doubt review since the last human round, or None when it has no reconcile step or its review never
    finished (`done`, or `unchanged` when it was carried over)."""
    from agentflow.autonomy import is_blocking

    phase = cfg.phases.get(phase_name)
    if phase is None or not reconcile_steps(phase, lane):
        return None
    scoped = since_human_round(events, phase_name)
    flagged = set(flagged_keys(phase, lane))
    if not any(e.get("type") in ("step_finished", "step_unchanged") and (e.get("key") or e.get("step")) in flagged
               and e.get("status", "done") == "done" for e in scoped):
        return None
    head, rounds = _split(scoped)
    reviews = max(sum(1 for e in scoped if e.get("type") == "step_finished" and e.get("key") in flagged
                      and e.get("status", "done") == "done"), 1)
    earlier: dict[str, tuple[str, str]] = {}
    disputed: set[str] = set()
    for rnd in rounds:
        earlier.update(_dispositions_of(_reconciled_of(rnd.after)))
        disputed |= _valid_disputes(_review_findings(rnd.after, flagged), earlier)
    rows: list[_Row] = []
    for rnd in rounds:
        record = _reconciled_of(rnd.after)
        answers = _dispositions_of(record)
        ran = _review_ran(rnd.after, flagged)
        next_findings = _review_findings(rnd.after, flagged) if ran else []
        changed = record is not None and record.get("before") != record.get("after")
        unresolved_ids = set((record or {}).get("unresolved") or [])
        for finding in _round_findings(rnd):
            ident = finding["id"]
            cls, note = answers.get(ident, (None, ""))
            status = _status(cls, ident, changed=changed, ran=ran, next_findings=next_findings, disputed=disputed,
                             unresolved_ids=unresolved_ids)
            rows.append(_Row(ident, _severity(finding), _text(finding), cls, clean_fact(note), status))
    last = rounds[-1].after if rounds else head
    if not rounds or _review_ran(last, flagged):  # the blocking findings no round answered: the review that ended the rounds
        blocking = [f for f in _review_findings(last, flagged) if is_blocking(cfg, f)]
        for n, finding in enumerate(blocking, 1):
            rows.append(_Row(f"R{len(rounds) + 1}-F{n}", _severity(finding), _text(finding), None, "", "still open"))
    seen: set[tuple[str, str]] = set()
    for segment in [head, *(rnd.after for rnd in rounds)]:  # every review that ran: a carried-over one adds nothing new
        if segment is not head and not _review_ran(segment, flagged):
            continue
        for finding in _review_findings(segment, flagged):
            row = _Row("", _severity(finding), _text(finding), None, "", "not blocking")
            if not is_blocking(cfg, finding) and (row.severity, row.text) not in seen:
                seen.add((row.severity, row.text))
                rows.append(row)
    return _Doubt(reviews, len(rounds), rows)


def _line(row: _Row) -> str:
    said = ""
    if row.cls is not None:
        said = f" — {row.cls}: {row.note}" if row.note else f" — {row.cls}"
    return f"{row.ident + ' ' if row.ident else ''}{row.severity}{row.text}{said} ({row.status})"


def _human_skill(cfg: Config, run: Run, phase: str) -> str | None:
    """`approval_note.human_skill` when the phase has a note whose `when` holds or is unknown, as the approval page's
    own note does: an offer the human takes up, because the runner never runs a user-only skill."""
    from agentflow.conditions import evaluate, make_ctx

    note = cfg.phases[phase].approval_note
    if not note:
        return None
    ctx = make_ctx(cfg, Path(run.state.get("worktree") or run.repo), run.state["pipeline"], run.state["params"], run)
    ctx.base_ref = f"origin/{cfg.project.base_branch}"
    return None if evaluate(cfg, note["when"], ctx).value is False else str(note["human_skill"])


def page_lines(cfg: Config, run: Run, phase: str, lane: str) -> list[str]:
    """The approval page's doubt section: one summary line, a line for why the rounds ended when something is left
    open, then one line per finding. `[]` when the phase has no reconcile step for `lane`, or its review never
    finished in its current walk."""
    events = run.events.read()
    model = _model(cfg, events, phase, lane)
    if model is None:
        return []
    summary = (f"Doubt review: {model.reviews} review(s) and {model.rounds} reconcile round(s), single-model; "
               f"{model.fixed} fixed, {model.trade_offs} trade-off(s) recorded, {model.open} still open. {CROSS_MODEL}")
    skill = _human_skill(cfg, run, phase)
    if skill:
        summary += f" For example: /{skill}."
    lines = [summary]
    if model.open:
        reason = stop_reason(cfg, events, cfg.phases[phase], lane)
        if reason in STOP_LINES:
            lines.append(STOP_LINES[reason])
    return lines + [_line(row) for row in model.rows]


def settled(cfg: Config, events: list[dict], phase: str, lane: str, stop: str) -> dict | None:
    """The fields of `doubt_settled` when the phase raises its approval: what its doubt review did since the last
    human round, and why the rounds ended (`stop`: `clean` or one of STOP_REASONS). None when the review never
    finished since that round (skipped, or no reconcile step), so no signal is written for it."""
    model = _model(cfg, events, phase, lane)
    if model is None:
        return None
    _, rounds = _split(since_human_round(events, phase))
    first = len(_round_findings(rounds[0])) if rounds else sum(1 for r in model.rows if r.ident)
    classes = Counter(r.cls for r in model.rows if r.cls is not None)
    return {"reviews": model.reviews, "rounds": model.rounds, "first_blocking": first, "open": model.open,
            "stop": stop, "actionable": classes["actionable"], "noise": classes["noise"],
            "misread": classes["contract_misread"]}


def handoff_risks(cfg: Config, run: Run, phase: str, lane: str) -> list[str]:
    """For the handoff's Risks: the findings still open, the findings that do not block, and the trade-offs recorded."""
    model = _model(cfg, run.events.read(), phase, lane)
    if model is None:
        return []
    risks: list[str] = []
    for row in model.rows:
        if row.status in OPEN_STATUSES:
            risks.append(clean_fact(f"{phase}: the doubt review left this open: {_line(row)}"))
        elif row.status == "not blocking":
            risks.append(clean_fact(f"{phase}: the doubt review found, not blocking: {_line(row)}"))
        elif row.cls == "trade_off":
            risks.append(clean_fact(f"{phase}: trade-off recorded for {row.ident}: {row.note or row.text}"))
    return risks
