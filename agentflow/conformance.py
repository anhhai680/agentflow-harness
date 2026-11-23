"""Conformance: what a run's events show for each obligation of the policy, and the audit at `finish`.

`evaluate` is pure: nothing in it reads a file or the clock. It takes the run's events, the obligations that apply
and the tree that will be pushed, and returns one finding per obligation. Evidence counts only when it comes after
the last code change that makes it stale (steps, expectations; see `_staling`; `advisory_cleared` goes stale on any
code change of the run) or was made on exactly the final
tree (gates), or by a gate whose `input_hash` there equals the one on the result (the files it declares as `inputs`
are the same). An affected-tier result (`tier: "affected"`, passed or skipped) is never gate evidence: it checked
only the files a fix changed. Events from older runs
carry no `tree`, so every reader uses `.get` and a missing tree is no evidence.

`audit` is the one impure entry point: `finish` calls it before the push. It snapshots the final tree, runs the
gates that have no evidence on it and, in a vuln run whose `advisory_cleared` went stale, the probe (`_reprobe`),
writes `conformance_checked`, and in `block` mode stops on a missing obligation.
`render_section` turns that event into the pull request's Conformance section, from config ids, statuses, seqs and
trees only: no step summary or finding text, which are model text, ever reaches it.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Literal

from agentflow import overlays, vuln
from agentflow.autonomy import AUTO_BY, blocking_of
from agentflow.conditions import Ctx
from agentflow.config import (
    ADVISORY_CLEARED,
    ADVISORY_PRESENT,
    CONFORMANCE_FAILED,
    Config,
    Evidence,
    Obligation,
)
from agentflow.gates import failure_report, gates_ok, input_hashes, record_results, run_gates
from agentflow.gitops import GitError, dirty_paths, head, merge_in_progress
from agentflow.guards import changed_paths, path_matches, snapshot
from agentflow.hitl import clean_fact, raise_hitl
from agentflow.policy import applicable, phase_where, step_phase
from agentflow.state import MARKED_DONE_SKIP, Run

Status = Literal["satisfied", "waived", "missing", "not_applicable"]

HUMAN_OVERRIDE = "human override"
ON_BASE = "on base"
RAMP_APPROVAL = "approval ramp"
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


def _is_code_change(event: dict) -> bool:
    return (event.get("type") in ("step_committed", "task_committed")
            or (event.get("type") == "base_synced" and bool(event.get("conflicts"))))


def last_code_change(events: list[dict]) -> int:
    """The seq of the last `step_committed` or `task_committed`, or of a `base_synced` that had conflicts to resolve
    by hand (code no review has seen). A clean merge of the base branch is not the run's own change. 0: none."""
    return max((e["seq"] for e in events if _is_code_change(e)), default=0)


def _verify_covered(cfg: Config, events: list[dict]) -> set[str]:
    """The step keys whose last result for every gate of `gate_sets.verify` passed or was skipped, under that same
    key: the runner ran the verify gates on that step's work. A gate a `gates_reused` event names counts as a pass
    under that event's key, at its place in the log: reuse takes only a full-tier pass that holds for the step's tree
    (the same tree, or the same input hash), so the gate's result on that work is known without running it."""
    names = list(cfg.gate_sets.get("verify") or [])
    if not names:
        return set()
    last: dict[tuple[str, str], dict] = {}
    for e in events:
        if e.get("type") == "gate_result" and e.get("key"):
            last[(str(e.get("key")), str(e.get("gate")))] = e
        elif e.get("type") == "gates_reused" and e.get("key"):
            for gate in e.get("gates") or []:
                last[(str(e.get("key")), str(gate))] = {"passed": True}
    keys = {key for key, _ in last}
    return {key for key in keys
            if all((r := last.get((key, name))) is not None and (r.get("passed") or r.get("skipped")) for name in names)}


def _staling(cfg: Config, events: list[dict]) -> list[dict]:
    """The code changes that make earlier `step:` and `expect:` evidence stale. A commit whose step ran the whole
    verify gate set (a refine-round fix, say) does not: today's bar re-checks such a fix with those gates, not with
    a fresh review or a fresh verify step, and the floor still needs them on the final tree. A conflicted merge
    always does: its hand-resolved code is a change no review has seen."""
    covered = _verify_covered(cfg, events)
    return [e for e in events if _is_code_change(e)
            and not (e.get("type") != "base_synced" and str(e.get("key") or "") in covered)]


def _stale_by(changes: list[dict], evidence: dict) -> int:
    """The seq of the last change after `evidence` that makes it stale, 0 when none does. A step's own commit (a
    change with the evidence's own key, as `build/fix` commits after its green check) never does."""
    key = evidence.get("key")
    return max((c["seq"] for c in changes if c["seq"] > evidence["seq"] and not (key and c.get("key") == key)), default=0)


def _last(candidates: list[dict]) -> dict | None:
    return max(candidates, key=lambda e: e["seq"], default=None)


def _gates(cfg: Config, events: list[dict], set_name: str, final_tree: str,
           hashes: Mapping[str, str] | None = None) -> _Item:
    names = cfg.gate_sets.get(set_name)
    if not names:
        return _Item(False, note=f"no gate set '{set_name}'")
    seqs: list[int] = []
    notes: list[str] = []
    unmet: list[str] = []
    for name in names:
        wanted = (hashes or {}).get(name)
        result = _last([e for e in events if e.get("type") == "gate_result" and e.get("gate") == name and final_tree
                        and e.get("tier") != "affected"  # it checked only the files a fix changed: never the floor
                        and (e.get("tree") == final_tree or (e.get("input_hash") and e.get("input_hash") == wanted))])
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


def _step(cfg: Config, events: list[dict], step_id: str, changes: list[dict]) -> _Item:
    phase = step_phase(cfg, step_id)
    finish = _last([e for e in events if e.get("type") == "step_finished" and e.get("status") == "done"
                    and str(e.get("key") or "").split("/")[-1] == step_id])
    if finish is None:
        return _Item(False, note=f"step {step_id} never finished", phase=phase)
    change = _stale_by(changes, finish)
    if change:
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


def _ramped(event: dict) -> bool:
    """Whether `event` is a `phase_approved` the runner gave on the approval ramp (`approval_ramp`): `by` the runner,
    with a `ramp` record whose `samples` is a whole number. Anything else that claims the ramp fails closed."""
    ramp = event.get("ramp")
    samples = ramp.get("samples") if isinstance(ramp, dict) else None
    return (event.get("type") == "phase_approved" and event.get("by") == AUTO_BY
            and isinstance(samples, int) and not isinstance(samples, bool))


def ramp_refused_note(phase: str) -> str:
    """The note of an `approval:<phase>` item the ramp approved where the security overlay holds (`ramp_refused`)."""
    return f"the approval ramp approved {phase}, but the security overlay holds: a person must approve it"


def ramp_refused(event: dict | None) -> list[str]:
    """The phases whose `approval:` evidence a `conformance_checked` event found missing because only the approval
    ramp gave it while the security overlay holds (`ramp_refused_note`), each once, in order: no retry of a missing
    step brings that evidence, only a person's approval of the phase."""
    phases: list[str] = []
    for finding in (event or {}).get("obligations") or []:
        if not isinstance(finding, dict) or finding.get("status") != "missing":
            continue
        for item in finding.get("require") or []:
            kind, _, phase = str(item).partition(":")
            if kind == "approval" and phase not in phases and ramp_refused_note(phase) in str(finding.get("note") or ""):
                phases.append(phase)
    return phases


def _approval(events: list[dict], phase: str, security: bool = False) -> _Item:
    approved = _last([e for e in events if e.get("type") == "phase_approved" and e.get("phase") == phase
                      and e.get("by") != AUTO_BY])
    if approved is not None:
        return _Item(True, [approved["seq"]])
    ramped = _last([e for e in events if e.get("phase") == phase and _ramped(e)])
    if ramped is not None and security:  # the ramp may have approved it before the run touched a security path
        return _Item(False, [ramped["seq"]], ramp_refused_note(phase))
    if ramped is not None:
        return _Item(True, [ramped["seq"]], RAMP_APPROVAL)
    skipped = _last([e for e in events if e.get("type") == "phase_skipped" and e.get("phase") == phase
                     and e.get("via") == "skip_if"  # not mark-done, not "all steps skipped"; older logs have no `via`
                     and e.get("decided_by") != "agent"])  # a model's guess is no proof the artifact is on base
    if skipped is not None:
        return _Item(True, [skipped["seq"]], ON_BASE)
    return _Item(False, note=f"no human approved {phase}", phase=phase)


def _red_then_green(cfg: Config, events: list[dict], changes: list[dict]) -> _Item:
    phase = phase_where(cfg, lambda step: step.expect == GREEN)
    checks = [e for e in events if e.get("type") == "expectation_checked" and e.get("ok")]
    for green in sorted((e for e in checks if e.get("expect") == GREEN and not _stale_by(changes, e)),
                        key=lambda e: e["seq"], reverse=True):
        red = _last([e for e in checks if e.get("expect") == RED and e["seq"] < green["seq"]])
        if red is not None:
            return _Item(True, [red["seq"], green["seq"]])
    return _Item(False, note="no red check followed by a green one after the last code change", phase=phase)


def _probe_ok(event: dict, expect: str) -> bool:
    """A probe check (`vuln.run_probe`) for `expect` that passed with no forbidden path in its tree."""
    return (event.get("type") == "expectation_checked" and event.get("expect") == expect and event.get("ok") is True
            and not event.get("forbidden"))


def _advisory_cleared(events: list[dict]) -> _Item:
    """The vuln lane's floor evidence: the latest `advisory_cleared` check that passed and is not stale, with an
    `advisory_present` check that passed before it (the probe named the advisory, then no longer did).

    Unlike `_red_then_green`, any later code change of the run stales it (`_is_code_change`, with no exemption for a
    fix that ran the verify gates: a review fix can change dependencies, and only the probe proves the advisory
    gone), and the item has no phase, so a mark-done never waives it, as it never waives the gates floor."""
    changes = [e for e in events if _is_code_change(e)]
    for cleared in sorted((e for e in events if _probe_ok(e, ADVISORY_CLEARED) and not _stale_by(changes, e)),
                          key=lambda e: e["seq"], reverse=True):
        present = _last([e for e in events if _probe_ok(e, ADVISORY_PRESENT) and e["seq"] < cleared["seq"]])
        if present is not None:
            return _Item(True, [present["seq"], cleared["seq"]])
    return _Item(False, note=f"no {ADVISORY_PRESENT} check followed by an {ADVISORY_CLEARED} one after the last code "
                 "change")


def _item(cfg: Config, events: list[dict], evidence: Evidence, final_tree: str, changes: list[dict],
          hashes: Mapping[str, str] | None = None, security: bool = False) -> _Item:
    if evidence.kind == "gates":
        return _gates(cfg, events, evidence.arg, final_tree, hashes)
    if evidence.kind == "step":
        return _step(cfg, events, evidence.arg, changes)
    if evidence.kind == "approval":
        return _approval(events, evidence.arg, security)
    if evidence.kind == "expect" and evidence.arg == "red_then_green":
        return _red_then_green(cfg, events, changes)
    if evidence.kind == "expect" and evidence.arg == ADVISORY_CLEARED:
        return _advisory_cleared(events)
    return _Item(False, note="unrecognised evidence item")  # `config.UNRECOGNISED`, or a kind this engine lacks


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
             not_applicable: list[Obligation] = (), *, input_hashes: Mapping[str, str] | None = None,
             security: bool = False) -> list[Finding]:
    """One finding per obligation (those that apply first, then `not_applicable` ones, in order), from `events` alone.

    An obligation is satisfied when every `require` item is. One that is not is `waived` when a human marked the
    run done and each unmet item belongs to a phase that mark-done skipped; `gates:` items never are, so the floor
    is never waived. Otherwise it is `missing`, with a note for each unmet item. An obligation with no evidence
    items is `missing`: it fails closed, never vacuously met.

    A gate's result counts when its `tree` is `final_tree`, or when it carries an `input_hash` equal to
    `input_hashes[gate]`, the gate's hash on the final tree (only a gate with `inputs` has one).

    With `security` (the security overlay holds on the tree to be pushed), an approval the runner gave on the approval
    ramp is no evidence: only a person approves such a run's phases."""
    changes = _staling(cfg, events)
    findings: list[Finding] = []
    for obligation in obligations:
        require = [str(evidence) for evidence in obligation.require]
        if not obligation.require:
            findings.append(Finding(obligation.id, require, "missing", [], "no evidence items"))
            continue
        items = [_item(cfg, events, evidence, final_tree, changes, input_hashes, security)
                 for evidence in obligation.require]
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


# --------------------------------------------------------------------------- the audit at finish


def clean_tree(cfg: Config, run: Run, worktree: Path, key: str, *, resume_merge: bool = False) -> bool:
    """True when `worktree` has no uncommitted change (tracked or untracked, not ignored); else a stop on `key`:
    `dirty_tree`, or `blocked` when git cannot say. `resume_merge`: a merge left half done counts as clean, so a
    stopped sync picks it up (`finish`'s first check, before the sync)."""
    try:
        dirty = [] if resume_merge and merge_in_progress(worktree) else dirty_paths(worktree)
    except GitError as exc:
        raise_hitl(cfg, run, "blocked", key, f"Could not read the state of {worktree}: {exc}")
        return False
    if dirty:
        shown = ", ".join(dirty[:5]) + (f" and {len(dirty) - 5} more" if len(dirty) > 5 else "")
        raise_hitl(cfg, run, "dirty_tree", key,
                   f"{worktree} has uncommitted changes ({shown}); finish pushes committed work only.")
        return False
    return True


def audit(cfg: Config, run: Run, worktree: Path, ctx: Ctx, key: str) -> bool:
    """Check the obligations against the tree `finish` is about to push; True: go on, False: stopped.

    It first checks again that the worktree holds nothing uncommitted (the sync and its gates ran since `finish`
    last looked), so the snapshot it audits is what the push sends; anything left over is a `dirty_tree` stop.
    Gates with no passing result on that snapshot run now, once, and are recorded on `key`; a failure is the usual
    `gate_failed` stop there, and approving it runs the audit again. A missing `advisory_cleared` with an earlier
    passing `advisory_present` runs the vuln probe the same way (`_reprobe`). Then `conformance_checked` is written. In
    `block` mode a missing obligation stops the run with `conformance_failed` before anything is pushed; in
    `report` mode the run goes on and the pull request shows the gap."""
    if not clean_tree(cfg, run, worktree, key):
        return False
    tree = snapshot(worktree)
    lane = str(ctx.values.get("lane") or run.state.get("pipeline") or "")
    applying = [obligation for obligation, _ in applicable(cfg, ctx, lane)]
    ids = {obligation.id for obligation in applying}
    others = [obligation for obligation in cfg.policy.obligations.values() if obligation.id not in ids]
    hashes = input_hashes(cfg, list(cfg.gates.values()), worktree, tree)
    security = overlays.security_path(cfg, ctx)  # read on the tree about to be pushed: a ramp approval is no evidence then
    findings = evaluate(cfg, run.events.read(), applying, tree, others, input_hashes=hashes, security=security)
    names = missing_gates(cfg, findings)
    if names:
        results = run_gates(cfg, names, worktree, run.gate_log_dir(key))
        record_results(run, key, results, tree)
        if not gates_ok(results):
            failed = [r for r in results if not (r.passed or r.skipped)]
            raise_hitl(cfg, run, "gate_failed", key,
                       f"Gates failed on the tree {key} would push: {', '.join(r.name for r in failed)}.",
                       extra=failure_report(failed))
            return False
        findings = evaluate(cfg, run.events.read(), applying, tree, others, input_hashes=hashes, security=security)
    if _needs_probe(cfg, run.events.read(), findings):
        if not _reprobe(cfg, run, worktree, key):
            return False
        findings = evaluate(cfg, run.events.read(), applying, tree, others, input_hashes=hashes, security=security)
    missing = [f.id for f in findings if f.status == "missing"]
    enforce = "report" if cfg.policy.enforce == "report" else "block"  # an unknown mode fails closed
    run.emit("conformance_checked", key=key, tree=tree, enforce=enforce, passed=not missing,
             obligations=[asdict(f) for f in findings])
    if missing and enforce == "block":
        raise_hitl(cfg, run, CONFORMANCE_FAILED, key,
                   f"{len(missing)} obligation(s) have no evidence on the tree to be pushed: {', '.join(missing)}.")
        return False
    return True


def _needs_probe(cfg: Config, events: list[dict], findings: list[Finding]) -> bool:
    """Whether the audit runs the vuln probe on the tree to be pushed: an `expect:advisory_cleared` item is missing
    (stale, say, after a review fix), the pack names a probe, and an `advisory_present` check passed, so a clean scan
    now completes the evidence. With no such check, no scan can: the finding stays missing."""
    wanted = f"expect:{ADVISORY_CLEARED}"
    return (bool(cfg.vuln.probe) and any(f.status == "missing" and wanted in f.require for f in findings)
            and any(_probe_ok(e, ADVISORY_PRESENT) for e in events))


def _probe_guard(cfg: Config, run: Run, worktree: Path, found: list[str]) -> Callable[[str], list[str]]:
    """The guard `vuln.run_probe` calls with the tree it ran on, as the build's probe has (`bug_eval._probe`): the
    forbidden paths (`guardrails.forbidden_paths`, and the lane's scanner ignore files) the probe changed, then those
    left uncommitted in that tree against `HEAD`. Each goes in `found` too, for the stop."""
    patterns = [*cfg.guardrails.forbidden_paths, *vuln.run_forbidden(cfg, run)]
    name = cfg.vuln.probe

    def hits(paths: list[str]) -> list[str]:
        return [p for p in paths if any(path_matches(p, g) for g in patterns)]

    def guard(before: str) -> list[str]:
        changed = hits(changed_paths(worktree, before, snapshot(worktree)))
        left = [p for p in hits(changed_paths(worktree, head(worktree) + "^{tree}", before)) if p not in changed]
        found.extend(f"{path} (changed by the probe gate `{name}`)" for path in changed)
        found.extend(f"{path} (uncommitted in the tree the probe ran on)" for path in left)
        return list(found)

    return guard


def _reprobe(cfg: Config, run: Run, worktree: Path, key: str) -> bool:
    """Run the vuln probe on the tree `finish` would push and record it on `key` (`vuln.run_probe`, guard-checked);
    True when it ran cleanly and names none of the run's advisory ids, else a stop on `key`: `guard_violation` when it
    left or found a forbidden path, `probe_failed` when it did not run cleanly, `gate_failed` when the advisory is
    still there. These stop in either `policy.enforce` mode, as a failing gate does: `advisory_cleared` is the vuln
    lane's floor. Approving the stop runs the audit, and so the probe, again."""
    found: list[str] = []
    verdict, matched, result = vuln.run_probe(cfg, run, worktree, key, ADVISORY_CLEARED,
                                              guard=_probe_guard(cfg, run, worktree, found))
    if found:
        run.emit("guard_tripped", key=key, violations=[{"kind": "forbidden_path", "detail": d} for d in found])
        raise_hitl(cfg, run, "guard_violation", key,
                   f"Guard violation in {key}: " + "; ".join(f"forbidden_path: {d}" for d in found))
        return False
    if verdict == "failed":
        raise_hitl(cfg, run, "probe_failed", key,
                   f"The probe gate `{result.name}` did not run cleanly on the tree {key} would push ({result.reason}), "
                   "so it shows nothing about the advisory and is never read as a clean scan.",
                   extra=failure_report([result]))
        return False
    if verdict == "present":
        ids = ", ".join(matched)
        raise_hitl(cfg, run, "gate_failed", key,
                   f"The probe gate `{result.name}` still names the advisory on the tree {key} would push: {ids}.",
                   extra=failure_report([replace(result, reason=f"advisory still reported: {ids}")]))
        return False
    return True


def missing_steps(steps: dict, event: dict | None) -> tuple[list[str], list[str]]:
    """The `step:` evidence a `conformance_checked` event found missing, split in two, each entry once: the run's own
    keys for the steps it walked (`agentflow retry` runs those again), and the ids of steps it never walked, which
    a retry cannot run (`run_control.retry` refuses a key the run has no row for)."""
    walked: list[str] = []
    never: list[str] = []
    for finding in (event or {}).get("obligations") or []:
        if finding.get("status") != "missing":
            continue
        for item in finding.get("require") or []:
            kind, _, step_id = str(item).partition(":")
            if kind != "step":
                continue
            keys = [k for k in steps if str(k).split("/")[-1] == step_id]
            if keys and keys[-1] not in walked:
                walked.append(keys[-1])
            elif not keys and step_id not in never:
                never.append(step_id)
    return walked, never


def render_section(event: dict | None, run_id: str) -> list[str]:
    """The lines of the pull request's Conformance section; none when no audit ran.

    Only config ids, statuses, event seqs and the tree go in, plus the evaluator's own notes, each one clean line.
    A step's summary or a reviewer's text never does, so model text cannot forge a row."""
    if not event:
        return []
    findings = [f for f in event.get("obligations") or [] if isinstance(f, dict)]
    counted = [f for f in findings if f.get("status") != "not_applicable"]
    missing = [f for f in counted if f.get("status") == "missing"]
    enforce = clean_fact(str(event.get("enforce") or "report"))
    tree = clean_fact(str(event.get("tree") or ""))[:12] or "unknown"
    if missing:
        lines = [f"Conformance: {len(missing)} missing ({enforce} mode)"]
    else:
        lines = [f"Conformance: all {len(counted)} obligations met"]
    for finding in findings:
        require = ", ".join(clean_fact(str(item)) for item in finding.get("require") or []) or "nothing"
        seqs = [s for s in finding.get("evidence") or [] if isinstance(s, int) and not isinstance(s, bool)]
        evidence = ", ".join(f"{run_id}#{seq}" for seq in seqs) or "none"
        note = clean_fact(str(finding.get("note") or ""))
        line = (f"- `{clean_fact(str(finding.get('id')))}` {clean_fact(str(finding.get('status')))}: {require}"
                f" · evidence {evidence} · tree {tree}")
        lines.append(line + (f" ({note})" if note else ""))
    return lines
