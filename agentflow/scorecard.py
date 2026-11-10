"""The scorecard: the signals of the finished runs against a baseline frozen on this machine.

`freeze` records, per lane, the mean of every signal over the finished runs of a date range, in
`<runs root>/scorecard_baseline.json` (a file of this machine, never of the repository). `scorecard_view` compares
the runs that ended after the baseline's `to` with it, signal by signal, and says for each whether it got `better`,
`worse` or stayed the `same`: the direction of a signal is its family's in `signals.FAMILIES`, or in `EFFICIENCY`
for the three this module adds (they are not lesson targets: no run's signals carry them). `conformance_missing`
is the one signal with a hard limit: an obligation with no evidence at the last audit, on any current run (an SLO
breach), and its row comes first.

A run's row is its signals (`signals.run_signals`) plus `cost_usd`, `wall_h` and `human_wait_h`. The date range
of both commands is the days the runs *ended* (local time). Nothing here changes a run.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from agentflow import safeio
from agentflow.events import parse_iso
from agentflow.metrics import run_metrics
from agentflow.pack import ENGINE_VERSION
from agentflow.retro import _ended_at, finished_runs
from agentflow.signals import FAMILIES, aggregate_signals, run_signals
from agentflow.state import Run, runs_root

BASELINE = "scorecard_baseline.json"
SLO_SIGNAL = "conformance_missing"
BASELINE_LIMIT = 4 * 1024 * 1024
NO_BASELINE = "No baseline yet: run `agentflow scorecard --freeze` first."
# the signals `run_row` adds to a run's own, with the direction that is better (like `signals.FAMILIES`)
EFFICIENCY: dict[str, tuple[str, bool]] = {"cost_usd": ("lower", False), "wall_h": ("lower", False),
                                           "human_wait_h": ("lower", False)}


def baseline_path(repo: Path) -> Path:
    return runs_root(repo) / BASELINE


def run_row(run: Run, now: datetime) -> dict[str, float]:
    """The signals of `run`, plus its cost in dollars and its wall and human-wait time in hours."""
    events = run.events.read()
    metrics = run_metrics(events, now)
    row = run_signals(events, metrics)
    row["cost_usd"] = float(metrics.get("cost_usd") or 0.0)
    row["wall_h"] = float(metrics.get("wall_s") or 0.0) / 3600
    row["human_wait_h"] = float((metrics.get("buckets") or {}).get("human") or 0.0) / 3600
    return row


def _select(repo: Path, lane: str | None, date_from: date | None, date_to: date | None,
            since: datetime | None = None) -> list[tuple[str, datetime, Run]]:
    """`(lane, ended, run)` for the finished runs of `lane` (every lane for None) that ended on `date_from` to
    `date_to` (both included, local time) and after `since`, oldest first."""
    if date_from and date_to and date_from > date_to:
        raise ValueError(f"from {date_from} is after to {date_to}")
    out = []
    for run in finished_runs(repo, since):
        name = run.state.get("pipeline")
        ended = _ended_at(run.events.read())
        if ended is None or not name or (lane is not None and name != lane):
            continue
        day = ended.astimezone().date()
        if (date_from and day < date_from) or (date_to and day > date_to):
            continue
        out.append((str(name), ended, run))
    out.sort(key=lambda item: (item[1], item[2].id))
    return out


def load_baseline(repo: Path) -> dict | None:
    """The frozen baseline, or None when there is none. A symlink at its path is `safeio.SymlinkRefused`, a file
    that is not a JSON object is a `ValueError`."""
    raw = safeio.read_bytes(baseline_path(repo), BASELINE_LIMIT)
    if raw is None:
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        data = None
    if not isinstance(data, dict) or not isinstance(data.get("lanes"), dict):
        raise ValueError(f"{baseline_path(repo)} is not a scorecard baseline: fix it or freeze again with --replace")
    return data


def freeze(repo: Path, lane: str | None, date_from: date | None, date_to: date | None, replace: bool,
           now: datetime) -> dict:
    """Write the baseline from the finished runs in range: per lane its run ids, the first and last end time, the
    engine version and the mean of every signal. `FileExistsError` when a baseline exists and `replace` is false;
    with `lane` and `replace`, the other lanes of the old baseline stay."""
    path = baseline_path(repo)
    if not replace and (path.is_symlink() or path.exists()):
        raise FileExistsError(f"a baseline exists at {path}: use --replace to freeze a new one")
    chosen = _select(repo, lane, date_from, date_to)
    if not chosen:
        raise ValueError("no finished runs in range to freeze")
    lanes: dict[str, Any] = {}
    if replace and lane is not None:
        lanes = dict((load_baseline(repo) or {}).get("lanes") or {})
    for name in dict.fromkeys(n for n, _e, _r in chosen):
        group = [(e, r) for n, e, r in chosen if n == name]
        lanes[name] = {"runs": [r.id for _e, r in group], "from": group[0][0].isoformat(),
                       "to": group[-1][0].isoformat(), "engine": ENGINE_VERSION,
                       "signals": aggregate_signals([run_row(r, now) for _e, r in group])}
    data = {"frozen_at": now.astimezone(UTC).isoformat(timespec="seconds"), "lanes": lanes}
    safeio.write_text(path, json.dumps(data, indent=2, sort_keys=True) + "\n")
    return data


def _verdict(signal: str, delta: float | None) -> str:
    if delta is None or abs(delta) < 1e-9:
        return "same"
    family = signal.partition(":")[0]
    spec = FAMILIES.get(family) or EFFICIENCY.get(family)
    if spec is None:
        return "same"
    return "better" if (delta > 0) == (spec[0] == "higher") else "worse"


def _compare(base: dict, current: dict) -> list[dict]:
    rows = []
    for signal in sorted(set(base) | set(current)):
        b, c = base.get(signal), current.get(signal)
        bm = b["mean"] if b else None
        cm = c["mean"] if c else None
        delta = cm - bm if bm is not None and cm is not None else None
        rows.append({"signal": signal, "baseline": bm, "baseline_n": b["runs"] if b else 0,
                     "current": cm, "current_n": c["runs"] if c else 0, "delta": delta,
                     "verdict": _verdict(signal, delta), "slo": signal == SLO_SIGNAL and bool(cm and cm > 0)})
    rows.sort(key=lambda r: (not r["slo"], r["signal"]))
    return rows


def scorecard_view(repo: Path, lane: str | None, date_from: date | None, date_to: date | None,
                   now: datetime) -> dict:
    """`{"baseline": bool, "lanes": {lane: [row, ...]}}`. With no dates, a lane's current runs are those that ended
    after its baseline's `to`; with a date, the runs that ended in that range (the baseline's own runs included)."""
    base = load_baseline(repo)
    if base is None:
        return {"baseline": False, "lanes": {}}
    dated = date_from is not None or date_to is not None
    frozen_lanes = base["lanes"]
    lanes: dict[str, list[dict]] = {}
    for name, frozen in sorted(frozen_lanes.items()):
        if lane is not None and name != lane:
            continue
        since = None if dated else parse_iso(frozen.get("to"))
        current = [run_row(run, now) for _n, _e, run in _select(repo, name, date_from, date_to, since)]
        lanes[name] = _compare(frozen.get("signals") or {}, aggregate_signals(current))
    # A lane the baseline never saw has no "before", but its conformance still counts: all its finished runs in range.
    extra: dict[str, list[dict]] = {}
    for name, _e, run in _select(repo, lane, date_from, date_to):
        if name not in frozen_lanes:
            extra.setdefault(name, []).append(run_row(run, now))
    for name, rows in sorted(extra.items()):
        lanes[name] = _compare({}, aggregate_signals(rows))
    return {"baseline": True, "lanes": lanes}


def _num(value: float | None) -> str:
    return "-" if value is None else f"{value:.3g}"


def scorecard_table(view: dict) -> str:
    if not view.get("baseline"):
        return NO_BASELINE
    out = []
    for name, rows in view["lanes"].items():
        out.append(f"{name}")
        if not rows:
            out.append("  (no signals)")
            continue
        width = max(len(r["signal"]) for r in rows)
        out.append(f"  {'signal'.ljust(width)}  {'baseline':>9}  {'current':>9}  {'delta':>9}  verdict")
        for r in rows:
            mark = "  SLO BREACH" if r["slo"] else ""
            out.append(f"  {r['signal'].ljust(width)}  {_num(r['baseline']) + f' ({r['baseline_n']})':>9}"
                       f"  {_num(r['current']) + f' ({r['current_n']})':>9}  {_num(r['delta']):>9}  {r['verdict']}{mark}")
    return "\n".join(out) if out else "The baseline holds no lanes."
