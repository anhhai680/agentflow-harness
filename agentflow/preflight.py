"""Preflight: whether this machine can run a lane's gates, checked once before the run's first phase.

`probe` looks at the programs of a list of gates (found on the gates' `PATH`, able to start `--version` in the
gates' environment) and at Docker when one of them runs it. `run_preflight` adds the `setup` gate set and a cache
for the day, and records one `preflight_checked` event. Nothing here is evidence for a run: no `gate_result` is
written, so the run's gate counts stay what the run itself ran.

It imports `hitl` at module level and `autonomy` never: `autonomy` imports this module.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from agentflow import doctor, gates as gates_mod, run_env, safeio
from agentflow.config import Config, Step
from agentflow.hitl import clean_fact
from agentflow.state import Run, runs_root

PREFLIGHT_STEP = "preflight"
CACHE_NAME = "preflight.json"
CACHE_MAX_BYTES = 1_000_000  # a day's fingerprints are a few lines; a larger file is not ours


@dataclass(frozen=True)
class Probe:
    ok: bool
    lines: list[str]  # one per problem, sorted, each cut by `clean_fact`
    digest: str  # sha256 of the lines: names this problem
    fingerprint: str  # sha256 of every (program, resolved path, first line of its version output): names the machine


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _lines(raw: list[str]) -> list[str]:
    return sorted({line for line in (clean_fact(text) for text in raw) if line})


def _step_gates(cfg: Config, step: Step, lane: str, out: list[str]) -> None:
    """The gate names of `step` and of its nested steps, `review`, `fix` and `sync`, when `step` is on `lane`."""
    if step.lanes and lane not in step.lanes:
        return
    if step.gates:
        try:
            names = [gate.name for gate in gates_mod.resolve(cfg, step.gates)]
        except KeyError:
            names = []  # `validate` reports a gate no step may name
        out.extend(names)
    for inner in (*step.steps, *(s for s in (step.review, step.fix, step.sync) if s is not None)):
        _step_gates(cfg, inner, lane, out)


def route_gates(cfg: Config, lane: str) -> list[str]:
    """The gate names, in order and without repeats, of every gate set a step of `lane` names (a step with no `lanes`
    is on every lane), with `gate_sets.setup` first and `gate_sets.verify` last. A step's `when` and `skip_if` are
    not evaluated: before `define` the files they read do not exist yet."""
    names: list[str] = []
    _set_gates(cfg, "setup", names)
    for phase in cfg.phases.values():
        for step in phase.steps:
            _step_gates(cfg, step, lane, names)
    _set_gates(cfg, "verify", names)
    return list(dict.fromkeys(names))


def _set_gates(cfg: Config, name: str, out: list[str]) -> None:
    if cfg.gate_sets.get(name):
        try:
            out.extend(gate.name for gate in gates_mod.resolve(cfg, name))
        except KeyError:
            pass  # `validate` reports a member that is no gate


def _first_line(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[0][:200] if lines else ""


def probe(cfg: Config, gates: list[str], cwd: Path) -> Probe:
    """What is wrong with the programs of `gates` (names of `cfg.gates`) under `cwd`, as a `run` would find them.

    A gate whose `when_exists` target or `cwd` directory is missing, and one whose command cannot be read, is
    skipped, as a run skips it. A relative program (`./x`) is skipped: setup may create it. A program not on the
    gates' `PATH` is a problem; so is one whose `--version` cannot run (it was killed by a signal, exited 126 or
    more, timed out or could not start), but any other exit code, `--version` unknown or not, passes. Each program
    is run once, in the environment the gates get. Docker is checked once when a probed gate runs it."""
    cwd = Path(cwd)
    path = doctor.gate_path(cfg)
    env = {**run_env.base(cfg), **cfg.defaults.env}
    problems: list[str] = []
    seen: dict[str, tuple[str, str, str | None]] = {}  # program -> (resolved path, version line, its problem)
    uses_docker = False
    for name in gates:
        gate = cfg.gates.get(name)
        if gate is None:
            continue
        if gate.when_exists and not (cwd / gate.when_exists).exists():
            continue
        if not (cwd / gate.cwd if gate.cwd else cwd).is_dir():
            continue
        try:
            programs = doctor.gate_programs(gate.run)
        except ValueError:
            continue
        for program in programs:
            if "/" in program and not os.path.isabs(program):
                continue
            if program not in seen:
                seen[program] = _version(program, path, cwd, env)
            uses_docker = uses_docker or program == "docker"
            problem = seen[program][2]
            if problem is not None:
                problems.append(f"gate:{name}: {problem}")
    if uses_docker:
        check = doctor.docker_check(cwd)
        if check.status == "fail":
            problems.append(f"docker: {check.detail}")
    fingerprint = _sha(json.dumps(sorted([program, resolved, version] for program, (resolved, version, _) in seen.items())))
    lines = _lines(problems)
    return Probe(not lines, lines, _sha("\n".join(lines)), fingerprint)


def _version(program: str, path: str, cwd: Path, env: dict[str, str]) -> tuple[str, str, str | None]:
    """`(resolved path, first line of its version output, problem)` of `program`; the problem is None when it passes."""
    if not doctor.found(program, path):
        return "", "", f"`{program}` is not on PATH"
    resolved = shutil.which(program, path=path) or program
    code, output = doctor.run_bounded([resolved, "--version"], cwd, env=env)
    if code is None or code < 0 or code >= 126:
        last = [line.strip() for line in output.splitlines() if line.strip()]
        reason = last[-1] if last else (f"exit {code}" if code is not None else "no answer")
        return resolved, "", f"`{program} --version` could not run: {reason}"
    return resolved, _first_line(output), None


def _cache_path(run: Run) -> Path:
    return runs_root(run.repo) / CACHE_NAME


def _read_cache(path: Path) -> dict:
    """The cache as `{"date": ..., "fingerprints": [...]}`; an empty one when the file is missing or not ours."""
    try:
        raw = safeio.read_bytes(path, CACHE_MAX_BYTES)
        data = json.loads(raw.decode("utf-8")) if raw is not None else None
    except (OSError, ValueError):
        data = None
    if (isinstance(data, dict) and isinstance(data.get("date"), str) and isinstance(data.get("fingerprints"), list)
            and all(isinstance(item, str) for item in data["fingerprints"])):
        return data
    return {"date": "", "fingerprints": []}


def _remember(path: Path, today: str, fingerprint: str) -> None:
    """Add `fingerprint` to today's entry (an entry of another day is replaced). Best effort: a cache that cannot be
    written only means the next run probes again."""
    cache = _read_cache(path)
    held = cache["fingerprints"] if cache["date"] == today else []
    if fingerprint in held:
        return
    try:
        safeio.write_text(path, json.dumps({"date": today, "fingerprints": [*held, fingerprint]}) + "\n")
    except (OSError, ValueError):
        pass


def run_preflight(cfg: Config, run: Run, worktree: Path, key: str) -> Probe:
    """Check the machine for `run`'s lane and record one `preflight_checked`.

    A pass that the day's cache already holds (by the programs' paths and versions) is the answer as it stands.
    Otherwise `gate_sets.setup` runs on the base tree, each gate that neither passed nor was skipped is a
    problem (`setup:<gate>: <reason>`), and the programs are probed again, since setup may install one. A pass
    is remembered only when the machine was already fine before setup ran."""
    started = time.monotonic()
    before = probe(cfg, route_gates(cfg, run.state["pipeline"]), worktree)
    cache, today = _cache_path(run), date.today().isoformat()
    held = _read_cache(cache)
    if before.ok and held["date"] == today and before.fingerprint in held["fingerprints"]:
        run.emit("preflight_checked", ok=True, lines=[], digest=before.digest, cached=True,
                 duration_s=round(time.monotonic() - started, 3))
        return before
    setup_problems: list[str] = []
    if cfg.gate_sets.get("setup"):
        for result in gates_mod.run_gates(cfg, "setup", worktree, run.gate_log_dir(key)):
            if not (result.passed or result.skipped):
                setup_problems.append(f"setup:{result.name}: {result.reason}")
    after = probe(cfg, route_gates(cfg, run.state["pipeline"]), worktree)
    lines = _lines([*setup_problems, *after.lines])
    outcome = Probe(not lines, lines, _sha("\n".join(lines)), after.fingerprint)
    if outcome.ok and before.ok:
        _remember(cache, today, before.fingerprint)
    run.emit("preflight_checked", ok=outcome.ok, lines=outcome.lines, digest=outcome.digest, cached=False,
             duration_s=round(time.monotonic() - started, 3))
    return outcome
