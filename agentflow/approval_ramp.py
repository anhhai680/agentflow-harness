"""The approval ramp (slice T7): `off → observe → auto` for each phase approval, as a pure function of event logs.

`_Loop.settle` calls `decide` just before a phase raises its approval, for the phases `autonomy.approvals` names.
Under `observe` and `auto` it records `would_auto_approve` with the checks (`CHECKS`) that held and those that didn't;
under `auto` the runner may also approve the phase itself (`phase_approved{by: AUTO_BY, ramp}`), which `conformance`
accepts as the approval's evidence. Under `off` (or no mode, or YAML's bare `off`) nothing is read or written.

The runner approves only when every check holds, the phase is not in `config.NEVER_AUTO`, and `eligible` says so:
the last `autonomy.ramp_samples` human decisions on the same lane, route profile and phase all agreed with a true
verdict, no run the ramp approved since the oldest of them missed an obligation at its audit, a scorecard baseline
exists, and the lane's conformance is not worse than it. The ramp's own approvals never count towards promotion,
and a run where the security overlay holds is never approved by it (`no_security_overlay`).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from agentflow import overlays, scorecard
from agentflow.autonomy import AUTO_BY, blocking_of, phase_findings
from agentflow.conditions import Ctx
from agentflow.config import CONFORMANCE_FAILED, NEVER_AUTO, RAMP_SAMPLES_MAX, RAMP_SAMPLES_MIN, ROUTE_PROFILES, Config
from agentflow.events import parse_iso
from agentflow.lessons import run_logs
from agentflow.pipeline import _ctx  # loaded by `scorecard` already (through `retro`); pipeline imports this module late
from agentflow.retro import _ended_at, finished_runs
from agentflow.state import Run

CHECKS = ("no_open_findings", "small_route", "no_security_overlay", "first_raise")
RAMP_PROFILES = ROUTE_PROFILES[:2]  # XS and S
MODES = ("observe", "auto")  # the modes that do something; anything else is `off`
EVENT = "would_auto_approve"
AUTO_REGRESSED = "an auto-approved run since missed an obligation"
NO_BASELINE = "no scorecard baseline"
BAD_BASELINE = "the scorecard baseline cannot be read"
WORSE = "conformance is worse than the baseline"
UNCOVERED = "the scorecard baseline has no conformance for {lane}"
DEFAULT_SAMPLES = 5


@dataclass(frozen=True)
class Sample:
    """One finished run's decision on a phase the runner would have approved: a person's (`auto` False; `agree` when
    they approved it) or the ramp's (`auto` True; `agree` unless the run's last audit missed an obligation)."""

    run_id: str
    ended: datetime
    auto: bool
    agree: bool


def mode_of(cfg: Config, phase: str) -> str:
    """The phase's ramp mode: `observe`, `auto`, or `off` for anything else (no entry, YAML's bare `off`)."""
    modes = cfg.autonomy.get("approvals") or {}
    mode = modes.get(phase) if isinstance(modes, dict) else None
    return mode if isinstance(mode, str) and mode in MODES else "off"


def ramp_samples(cfg: Config) -> int:
    """`autonomy.ramp_samples`, or the default for anything `validate` would refuse (fail closed: never fewer)."""
    value = cfg.autonomy.get("ramp_samples", DEFAULT_SAMPLES)
    whole = isinstance(value, int) and not isinstance(value, bool)
    return value if whole and RAMP_SAMPLES_MIN <= value <= RAMP_SAMPLES_MAX else DEFAULT_SAMPLES


def profile_of(run: Run) -> str | None:
    """The run's route profile (its last `route_profile`, applied or not), or None when it was never sized."""
    route = run.state.get("route")
    profile = route.get("profile") if isinstance(route, dict) else None
    return profile if isinstance(profile, str) else None


def fresh_ctx(cfg: Config, run: Run) -> Ctx:
    """The run's context built now, on its worktree: a phase's context is built before it walks, so its `{plan}` (and
    so `security_path`'s keyword check) would miss a plan the phase itself just wrote."""
    return _ctx(cfg, run, Path(run.state.get("worktree") or run.repo))


def verdict(cfg: Config, run: Run, phase: str) -> tuple[list[str], list[str]]:
    """`(held, missing)`: the `CHECKS` that hold for the phase now and those that don't, each in `CHECKS` order. The
    context is built here (`fresh_ctx`), so no caller can hand in one older than the tree."""
    ctx = fresh_ctx(cfg, run)
    held = {
        "no_open_findings": not blocking_of(cfg, phase_findings(cfg, run, phase)),
        "small_route": profile_of(run) in RAMP_PROFILES,
        "no_security_overlay": not overlays.security_path(cfg, ctx),
        "first_raise": not any(e["type"] == "phase_rejected" and e.get("phase") == phase and e.get("by") != AUTO_BY
                               for e in run.events.read()),
    }
    return [c for c in CHECKS if held[c]], [c for c in CHECKS if not held[c]]


def _sample(events: list[dict], run_id: str, profile: str, phase: str, ended: datetime | None) -> Sample | None:
    first = next((i for i, e in enumerate(events) if e.get("type") == EVENT and e.get("phase") == phase
                  and e.get("verdict") is True), None)
    if first is None or ended is None or events[first].get("profile") != profile:
        return None
    for event in events[first + 1:]:
        if event.get("phase") != phase or event.get("type") not in ("phase_approved", "phase_rejected"):
            continue
        if event["type"] == "phase_approved" and event.get("by") == AUTO_BY and event.get("ramp"):
            audits = [e for e in events if e.get("type") == "conformance_checked"]
            missed = bool(audits) and any(isinstance(o, dict) and o.get("status") == "missing"
                                          for o in audits[-1].get("obligations") or [])
            return Sample(run_id, ended, True, not missed)
        if event.get("by") != AUTO_BY:  # a runner's own refine round on the phase is no decision on its approval
            return Sample(run_id, ended, False, event["type"] == "phase_approved")
    return None


def samples(runs: list[Run], lane: str, profile: str, phase: str) -> list[Sample]:
    """The samples of `runs` of `lane` for `profile` and `phase`, oldest first. A run gives one when its first
    `would_auto_approve` on the phase with a true verdict was for `profile` and a decision on the phase followed."""
    out = []
    for run in runs:
        if run.state.get("pipeline") != lane:
            continue
        events = run.events.read()
        sample = _sample(events, run.id, profile, phase, _ended_at(events))
        if sample is not None:
            out.append(sample)
    out.sort(key=lambda s: (s.ended, s.run_id))
    return out


def blocked(repo: Path, lane: str, profile: str, phase: str) -> list[Sample]:
    """The ramp's samples of runs of `lane` that have not ended but were stopped at their audit (`conformance_failed`):
    each a regression, at the time of its last such stop. A blocked run counts before anyone cancels it."""
    out = []
    for name, events in run_logs(repo):
        if _ended_at(events) is not None:  # an ended run is one of `samples`
            continue
        started = next((e for e in events if e.get("type") == "run_started"), None)
        stops = [e for e in events if e.get("type") == "hitl_raised" and e.get("kind") == CONFORMANCE_FAILED]
        if started is None or started.get("pipeline") != lane or not stops:
            continue
        sample = _sample(events, name, profile, phase, parse_iso(stops[-1].get("ts")))
        if sample is not None and sample.auto:
            out.append(Sample(sample.run_id, sample.ended, True, False))
    return out


def _with_blocked(found: list[Sample], repo: Path, lane: str, profile: str, phase: str) -> list[Sample]:
    return sorted([*found, *blocked(repo, lane, profile, phase)], key=lambda s: (s.ended, s.run_id))


def _eligible(found: list[Sample], repo: Path, cfg: Config, lane: str, now: datetime) -> tuple[bool, str, int]:
    """Check `eligible`'s four rules on `found` (the samples and the blocked runs, oldest first)."""
    n = ramp_samples(cfg)
    human = [s for s in found if not s.auto]
    last = human[-n:]
    agreeing = sum(s.agree for s in last)
    counted = f"{agreeing} of {n} agreeing human decisions"
    if len(last) < n or agreeing < n:
        return False, counted, agreeing
    since = found.index(last[0])
    if any(s.auto and not s.agree for s in found[since + 1:]):
        return False, AUTO_REGRESSED, agreeing
    try:
        base = scorecard.load_baseline(repo)
    except (OSError, ValueError):  # a symlink (`safeio.SymlinkRefused`) or not a baseline: fail closed
        return False, BAD_BASELINE, agreeing
    if base is None:
        return False, NO_BASELINE, agreeing
    frozen = (base.get("lanes") or {}).get(lane)
    if not isinstance(frozen, dict) or scorecard.SLO_SIGNAL not in (frozen.get("signals") or {}):
        return False, UNCOVERED.format(lane=lane), agreeing  # nothing to compare with: `_verdict` would say "same"
    rows = scorecard.scorecard_view(repo, lane, None, None, now)["lanes"].get(lane) or []
    if any(r["signal"] == scorecard.SLO_SIGNAL and r["verdict"] == "worse" for r in rows):
        return False, WORSE, agreeing
    return True, counted, agreeing


def eligible(repo: Path, cfg: Config, lane: str, profile: str, phase: str, now: datetime) -> tuple[bool, str, int]:
    """`(ok, why, agreeing)`: whether the runner may approve `phase` itself for runs of `lane` with `profile`, read
    from the finished runs (`retro.finished_runs`) and the scorecard baseline. `agreeing` counts the agreeing ones of
    the last `ramp_samples` human samples. A run the ramp approved that is stopped at its audit (`blocked`) is a
    regression already."""
    found = _with_blocked(samples(finished_runs(repo, None), lane, profile, phase), repo, lane, profile, phase)
    return _eligible(found, repo, cfg, lane, now)


def decide(cfg: Config, run: Run, phase: str, now: datetime) -> dict | None:
    """Before `phase` raises its approval: None under `off`; otherwise record `would_auto_approve` and return
    `{"auto", "samples", "profile"}`. `auto` holds only under `auto`, with every check held, a phase not in
    `NEVER_AUTO` and `eligible` ok; `eligible` and `why` are on the event only under `auto`. The checks read the
    run's tree as it is now (`verdict`)."""
    mode = mode_of(cfg, phase)
    if mode == "off":
        return None
    held, missing = verdict(cfg, run, phase)
    profile = profile_of(run)
    fields: dict = {"phase": phase, "mode": mode, "verdict": not missing, "held": held, "missing": missing}
    if profile is not None:
        fields["profile"] = profile
    auto, agreeing = False, 0
    if mode == "auto":
        if missing:
            why = f"{', '.join(missing)} did not hold"
        elif phase in NEVER_AUTO:
            why = f"{phase} is always approved by a person"
        else:
            auto, why, agreeing = eligible(run.repo, cfg, str(run.state["pipeline"]), str(profile), phase, now)
        fields.update(eligible=auto, why=why)
    run.emit(EVENT, **fields)
    return {"auto": auto, "samples": agreeing, "profile": profile}


def ramp_view(repo: Path, cfg: Config, now: datetime) -> list[dict]:
    """One row per lane, phase in `autonomy.approvals` and profile in `RAMP_PROFILES` that has samples: its mode, the
    human samples and how many agreed, the ramp's own and how many of those missed an obligation, and `eligible`."""
    runs = finished_runs(repo, None)
    modes = cfg.autonomy.get("approvals") or {}
    rows = []
    for lane in sorted({str(r.state.get("pipeline")) for r in runs if r.state.get("pipeline")}):
        for phase in (modes if isinstance(modes, dict) else {}):
            for profile in RAMP_PROFILES:
                found = _with_blocked(samples(runs, lane, profile, phase), repo, lane, profile, phase)
                if not found:
                    continue
                ok, why, _agreeing = _eligible(found, repo, cfg, lane, now)
                human = [s for s in found if not s.auto]
                auto = [s for s in found if s.auto]
                rows.append({"lane": lane, "phase": phase, "profile": profile, "mode": mode_of(cfg, phase),
                             "human": len(human), "agreeing": sum(s.agree for s in human), "auto": len(auto),
                             "regressions": sum(not s.agree for s in auto), "eligible": ok, "why": why})
    return rows
