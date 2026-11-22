"""Speculation: while a `needs_approval: true` phase's approval waits, the runner walks the phase after it.

The rules are pure reads of the config, the run's state and its log; `pipeline._Loop.speculate` acts on them.

- `due` says whether a phase that just raised its approval (`after`) is followed by a phase in `autonomy.speculate`
  the runner may walk now: never when the run ends at `after` (`--to`), was marked done, or `after` still holds a
  blocking finding (a spec a reviewer doubts is not worth planning on).
- `basis` pins what the speculated phase is planned on: the bytes of each committed `output` of `after`.
  A revision is speculated once (`last_record`): the same basis after a finished speculation walks nothing again.
- `planned_on` and `replan_due` say whether a phase that was walked (or re-planned) on one basis now follows another:
  the runner then sends it back once, as a refine round of kind `replan` whose note (`replan_note`) names the changed
  paths and the `git diff` to read.
- `cut` reads what the person did at a step boundary: a rejection of `after` (`state["refine"]`) or an approval with
  `hold` (`state["ready"]`) ends the walk there. A plain approval lets it go on as a normal phase.

`speculation_started` and `speculation_ended` (`state.speculating`) are the only events of their own; nothing here
emits. With `autonomy.speculate: []` (the default) `due` is always None, so no event is ever written.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from agentflow import doubt
from agentflow.autonomy import blocking_of, phase_findings
from agentflow.conditions import Ctx, fmt
from agentflow.config import Config, Phase, for_lane

if TYPE_CHECKING:
    from agentflow.state import Run

OUTCOMES = ("finished", "stopped", "approved", "rejected", "halted")
REPLAN = "replan"  # `phase_rejected.kind` of the round the runner opens when the basis a phase was planned on changed
UNSTARTED = (None, "pending", "skipped")  # a phase row that has nothing to re-plan


def next_phase(cfg: Config, names: list[str], index: int, lane: str) -> str | None:
    """The first phase after `names[index]` with steps for `lane`, when it is in `autonomy.speculate`; else None."""
    listed = cfg.autonomy.get("speculate") or []
    for name in names[index + 1:]:
        if for_lane(cfg.phases[name].steps, lane):
            return name if name in listed else None
    return None


def due(cfg: Config, run: Run, names: list[str], index: int, to_phase: str | None) -> str | None:
    """The phase to walk while `names[index]`'s approval waits (`next_phase`), or None: the run ends at that phase
    (`to_phase`), it was marked done, or that phase still holds a blocking finding."""
    nxt = next_phase(cfg, names, index, run.state["pipeline"])
    if nxt is None or to_phase == names[index] or run.state.get("marked_done"):
        return None
    if blocking_of(cfg, phase_findings(cfg, run, names[index])):
        return None
    return nxt


def basis(phase: Phase, lane: str, ctx: Ctx, worktree: Path) -> dict[str, str]:
    """`{path: sha256}` of each `output` of the phase's top-level `commit: true` steps for `lane`: what a phase
    walked after it is planned on (`doubt.artifact_digest`, `""` for a missing file)."""
    found: dict[str, str] = {}
    for step in for_lane(phase.steps, lane):
        if step.commit is True and step.output:
            rel = fmt(str(step.output), ctx)
            found[rel] = doubt.artifact_digest(worktree, rel)
    return found


def last_record(events: list[dict], phase: str) -> tuple[dict | None, bool]:
    """The last `speculation_started` for `phase`, and whether a `speculation_ended` for it came after."""
    record: dict | None = None
    ended = False
    for event in events:
        if event.get("phase") != phase:
            continue
        if event["type"] == "speculation_started":
            record, ended = event, False
        elif event["type"] == "speculation_ended" and record is not None:
            ended = True
    return record, ended


def planned_on(events: list[dict], phase: str) -> tuple[dict[str, str], str] | None:
    """`(basis, sha)` of the last `speculation_started` or `phase_rejected{kind: replan}` of `phase`: what its work was
    last written on, and the commit a diff of that basis starts at. None when it was never walked behind an approval."""
    found: tuple[dict[str, str], str] | None = None
    for event in events:
        if event.get("phase") != phase:
            continue
        if event["type"] == "speculation_started" or (event["type"] == "phase_rejected" and event.get("kind") == REPLAN):
            sha = event.get("sha") if event["type"] == "speculation_started" else event.get("from_sha")
            found = (dict(event.get("basis") or {}), str(sha or ""))
    return found


def replan_due(state: dict, events: list[dict], phase: str, basis: dict[str, str]) -> bool:
    """Whether `phase` started, was planned on a basis (`planned_on`), and that basis is not `basis`."""
    if (state["phases"].get(phase) or {}).get("status") in UNSTARTED:
        return False
    planned = planned_on(events, phase)
    return planned is not None and planned[0] != basis


def changed(old: dict[str, str], new: dict[str, str]) -> list[str]:
    """The paths of `new` whose digest is not the one in `old` (a path in only one of them counts as changed)."""
    return sorted(path for path in {*old, *new} if old.get(path) != new.get(path))


def replan_note(paths: list[str], sha: str) -> str:
    """The note of a `replan` round: which of the approval phase's files changed and the diff to read."""
    names = ", ".join(f"`{path}`" for path in paths)
    return (f"The {names} changed after this phase's work was written (at {sha[:12]}). Read the change with "
            f"`git diff {sha[:12]} HEAD -- {names}`, then update what this phase wrote to match it; keep what still holds.")


def cut(state: dict, after: str) -> str | None:
    """What the person did to `after` that ends a walk at the next step boundary: `rejected` (its refine round is open)
    or `ready` (approved with `hold`), else None."""
    if (state.get("refine") or {}).get("phase") == after:
        return "rejected"
    if (state.get("ready") or {}).get("phase") == after:
        return "ready"
    return None


def page_line(after: str, phase: str) -> str:
    """The line on `after`'s approval page while the runner walks `phase` behind it."""
    return (f"While you review this, the runner drafts `{phase}` from it. Approving keeps that work; rejecting "
            f"re-plans it on your revision.")
