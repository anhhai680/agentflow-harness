"""Conformance, the pure part: what a run's events show for each obligation of the policy.

Nothing here reads a file or the clock. `evaluate` takes the run's events, the obligations that apply and the
tree that will be pushed, and returns one finding per obligation. Evidence counts only when it comes after the
last code change (steps, expectations) or was made on exactly the final tree (gates). Events from older runs
carry no `tree`, so every reader uses `.get` and a missing tree is no evidence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from agentflow.autonomy import AUTO_BY, blocking_of
from agentflow.config import Config, Evidence, Obligation
from agentflow.policy import phase_where, step_phase
from agentflow.state import MARKED_DONE_SKIP

Status = Literal["satisfied", "waived", "missing", "not_applicable"]

HUMAN_OVERRIDE = "human override"
ON_BASE = "on base"
GREEN, RED = "green", "red"


@dataclass
class Finding:
    id: str
    require: list[str]
    status: Status
    evidence: list[int]  # seqs of the events that show it
    note: str


@dataclass
class _Item:
    """One `require` item: whether it holds, the events behind it, a note, and the phase a mark-done could waive it for."""

    ok: bool
    evidence: list[int] = field(default_factory=list)
    note: str = ""
    phase: str | None = None  # None: never waived (a gate, or no phase to point at)


def last_code_change(events: list[dict]) -> int:
    """The seq of the last `step_committed` or `task_committed`, or of a `base_synced` that had conflicts to resolve
    by hand (code no review has seen). A clean merge of the base branch is not the run's own change. 0: none."""
    seqs = [e["seq"] for e in events if e.get("type") in ("step_committed", "task_committed")
            or (e.get("type") == "base_synced" and e.get("conflicts"))]
    return max(seqs, default=0)


def _last(candidates: list[dict]) -> dict | None:
    return max(candidates, key=lambda e: e["seq"], default=None)


def _gates(cfg: Config, events: list[dict], set_name: str, final_tree: str) -> _Item:
    names = cfg.gate_sets.get(set_name)
    if not names:
        return _Item(False, note=f"no gate set '{set_name}'")
    seqs: list[int] = []
    notes: list[str] = []
    unmet: list[str] = []
    for name in names:
        result = _last([e for e in events if e.get("type") == "gate_result" and e.get("gate") == name
                        and final_tree and e.get("tree") == final_tree])
        if result is None:
            unmet.append(f"{name}: no result on the final tree")
        elif result.get("skipped"):
            seqs.append(result["seq"])
            notes.append(f"{name} skipped: {result.get('reason') or 'no reason given'}")
        elif result.get("passed"):
            seqs.append(result["seq"])
        else:
            unmet.append(f"{name}: failed on the final tree")
    if unmet:
        return _Item(False, note="; ".join(unmet))
    return _Item(True, seqs, "; ".join(notes))


def _step(cfg: Config, events: list[dict], step_id: str, change: int) -> _Item:
    phase = step_phase(cfg, step_id)
    finish = _last([e for e in events if e.get("type") == "step_finished" and e.get("status") == "done"
                    and str(e.get("key") or "").split("/")[-1] == step_id])
    if finish is None:
        return _Item(False, note=f"step {step_id} never finished", phase=phase)
    if finish["seq"] < change:
        return _Item(False, note=f"step {step_id} finished at {finish['seq']}, before the last code change at {change}",
                     phase=phase)
    key = finish.get("key")
    findings = _last([e for e in events if e.get("type") == "step_findings" and e.get("key") == key
                      and e["seq"] > finish["seq"]])
    blocking = blocking_of(cfg, list(findings.get("findings") or [])) if findings else []
    if not blocking:
        return _Item(True, [finish["seq"]])
    holder = str(key).split("/")[0]
    approval = _last([e for e in events if e.get("type") == "phase_approved" and e.get("phase") == holder
                      and e.get("by") != AUTO_BY and e["seq"] > findings["seq"]])
    if approval is not None:
        return _Item(True, [finish["seq"], approval["seq"]], HUMAN_OVERRIDE)
    return _Item(False, note=f"step {step_id} has {len(blocking)} blocking finding(s) and no human approved {holder}",
                 phase=phase)


def _approval(events: list[dict], phase: str) -> _Item:
    approved = _last([e for e in events if e.get("type") == "phase_approved" and e.get("phase") == phase
                      and e.get("by") != AUTO_BY])
    if approved is not None:
        return _Item(True, [approved["seq"]])
    skipped = _last([e for e in events if e.get("type") == "phase_skipped" and e.get("phase") == phase
                     and e.get("via") == "skip_if"])  # not mark-done, not "all steps skipped"; older logs have no `via`
    if skipped is not None:
        return _Item(True, [skipped["seq"]], ON_BASE)
    return _Item(False, note=f"no human approved {phase}", phase=phase)


def _red_then_green(cfg: Config, events: list[dict], change: int) -> _Item:
    phase = phase_where(cfg, lambda step: step.expect == GREEN)
    checks = [e for e in events if e.get("type") == "expectation_checked" and e.get("ok")]
    for green in sorted((e for e in checks if e.get("expect") == GREEN and e["seq"] > change),
                        key=lambda e: e["seq"], reverse=True):
        red = _last([e for e in checks if e.get("expect") == RED and e["seq"] < green["seq"]])
        if red is not None:
            return _Item(True, [red["seq"], green["seq"]])
    return _Item(False, note="no red check followed by a green one after the last code change", phase=phase)


def _item(cfg: Config, events: list[dict], evidence: Evidence, final_tree: str, change: int) -> _Item:
    if evidence.kind == "gates":
        return _gates(cfg, events, evidence.arg, final_tree)
    if evidence.kind == "step":
        return _step(cfg, events, evidence.arg, change)
    if evidence.kind == "approval":
        return _approval(events, evidence.arg)
    if evidence.kind == "expect":
        return _red_then_green(cfg, events, change)
    return _Item(False, note=f"unknown evidence '{evidence}'")


def _waiver(events: list[dict], unmet: list[_Item]) -> Finding | None:
    """The mark-done that explains every unmet item: each one's phase was skipped after it. None if there is no
    such mark, or an item has no phase to point at (a gate, the floor)."""
    mark = _last([e for e in events if e.get("type") == "run_marked_done"])
    if mark is None or any(item.phase is None for item in unmet):
        return None
    skipped = {e.get("phase") for e in events if e.get("type") == "phase_skipped" and e["seq"] > mark["seq"]
               and str(e.get("reason") or "").startswith(MARKED_DONE_SKIP)}
    if all(item.phase in skipped for item in unmet):
        return Finding("", [], "waived", [mark["seq"]], f"waived by {mark.get('by')}")
    return None


def evaluate(cfg: Config, events: list[dict], obligations: list[Obligation], final_tree: str,
             not_applicable: list[Obligation] = ()) -> list[Finding]:
    """One finding per obligation (those that apply first, then `not_applicable` ones, in order), from `events` alone.

    An obligation is satisfied when every `require` item is. One that is not is `waived` when a human marked the
    run done and each unmet item belongs to a phase that mark-done skipped; `gates:` items never are, so the floor
    is never waived. Otherwise it is `missing`, with a note for each unmet item."""
    change = last_code_change(events)
    findings: list[Finding] = []
    for obligation in obligations:
        items = [_item(cfg, events, evidence, final_tree, change) for evidence in obligation.require]
        require = [str(evidence) for evidence in obligation.require]
        unmet = [item for item in items if not item.ok]
        evidence = sorted({seq for item in items for seq in item.evidence})
        if not unmet:
            note = "; ".join(item.note for item in items if item.note)
            findings.append(Finding(obligation.id, require, "satisfied", evidence, note))
            continue
        waiver = _waiver(events, unmet)
        if waiver is not None:
            findings.append(Finding(obligation.id, require, "waived", waiver.evidence, waiver.note))
        else:
            findings.append(Finding(obligation.id, require, "missing", evidence, "; ".join(i.note for i in unmet if i.note)))
    findings += [Finding(o.id, [str(e) for e in o.require], "not_applicable", [], "") for o in not_applicable]
    return findings


def missing_gates(cfg: Config, findings: list[Finding]) -> list[str]:
    """The gates behind the `gates:` items of the `missing` findings, each once, in `gate_sets` order."""
    wanted = {item.partition(":")[2] for f in findings if f.status == "missing" for item in f.require
              if item.startswith("gates:")}
    return list(dict.fromkeys(name for set_name, names in cfg.gate_sets.items() if set_name in wanted for name in names))
