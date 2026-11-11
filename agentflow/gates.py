"""Run the configured shell gates and summarise the results.

A gate is a named shell command from `config.yaml`. It passes when it exits 0. A gate whose
`when_exists` target is missing is reported **skipped**, never passed: the check it stands
for did not run. Each gate runs in its own process group with stdin closed; past
`defaults.gate_timeout_min` the whole group is killed and the gate fails ("timed out after N min"),
and the group is killed too whenever the runner leaves early. A missing `cwd` fails the gate (the
reason names the path). Gate logs are written fresh and never through a symlink (`safeio`). `gates_ok` treats skipped gates as not failing, so a missing optional code
root does not block a step, but the summary always shows them.

Doc checks (`run_doc_checks`) are the design lane's built-in gates, named in a step's `checks:`. They
read `git diff` from a base commit to the working tree as it is now (committed, staged, unstaged and
untracked changes, through a `guards.snapshot` tree, so the real index is untouched). Lines are placed
in a section by their line numbers in the hunk headers against the section headings, which come from
`git grep '^## '` on each side, so the idea doc is never parsed whole. A section runs from its `## `
heading to the next `## ` heading (`###` subsections stay inside it).

Reuse (`run_gates_reusing`): a gate that already passed in this run on exactly the tree about to be gated, with the
same command (`run`, `cwd`, `when_exists` and `defaults.env`, as the `cmd` digest on its `gate_result`), is not run
again: the earlier pass is the evidence, the rule `conformance` already holds the final tree to. A failure, a skip,
an event with no `tree` or no `cmd`, and the `setup` set's gates (they start things) are never reused.
"""

from __future__ import annotations

import contextlib
import fnmatch
import hashlib
import json
import os
import re
import signal
import subprocess
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from agentflow import run_env, safeio
from agentflow.config import Config, Gate
from agentflow.guards import snapshot

if TYPE_CHECKING:
    from agentflow.state import Run

KILL_WAIT_S = 5


@dataclass
class GateResult:
    name: str
    passed: bool
    skipped: bool
    reason: str
    log_path: Path | None
    tail: str
    duration_s: float = 0.0  # wall time the gate ran, for the run metrics
    cmd: str = ""  # `command_digest` of what ran; "" for a doc check
    reused_from: str | None = None  # the step key of the earlier pass this result stands for (`run_gates_reusing`)


def _resolve(cfg: Config, names: str | list[str]) -> list[Gate]:
    """Turn a gate-set name or a list of gate (or gate-set) names into gates, in order."""
    wanted = cfg.gate_sets[names] if isinstance(names, str) else names
    if isinstance(names, str) and names not in cfg.gate_sets:
        raise KeyError(names)
    gates: list[Gate] = []
    for name in wanted:
        if name in cfg.gates:
            gates.append(cfg.gates[name])
        elif name in cfg.gate_sets:
            gates.extend(_resolve(cfg, name))
        else:
            raise KeyError(name)
    return gates


def resolve(cfg: Config, names: str | list[str]) -> list[Gate]:
    """The gates a step's `gates:` names, in order, without running them (for dry runs)."""
    return _resolve(cfg, names)


def agent_checks(cfg: Config, names: str | list[str] | None, worktree: Path) -> list[Gate]:
    """The gates a step's `gates:` names that the implementer may run itself: flagged `agent_check`, and not
    ones the runner would skip for a missing `when_exists` path in `worktree`. In order, each name once."""
    if not names:
        return []
    try:
        gates = _resolve(cfg, names)
    except KeyError:
        return []
    seen: set[str] = set()
    out: list[Gate] = []
    for gate in gates:
        if not gate.agent_check or gate.name in seen:
            continue
        if gate.when_exists and not (worktree / gate.when_exists).exists():
            continue
        seen.add(gate.name)
        out.append(gate)
    return out


def _kill_group(proc: subprocess.Popen) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):  # it may have ended on its own meanwhile
        os.killpg(proc.pid, signal.SIGKILL)


def _shell(command: str, cwd: Path, timeout_s: float, env: dict[str, str] | None = None,
           env_base: Mapping[str, str] | None = None) -> tuple[int | None, str]:
    """`(exit code, output)` of `command` run by the shell in its own process group; `(None, output)` when
    it was killed at `timeout_s`. The group is killed whenever this is left with it still running. The
    environment is `env_base` (default: this process's) with `env` on top."""
    base = os.environ if env_base is None else env_base
    proc = subprocess.Popen(command, shell=True, cwd=cwd, env={**base, **(env or {})}, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                            errors="replace", start_new_session=True)
    try:
        try:
            output, _ = proc.communicate(timeout=timeout_s)
            return proc.returncode, output or ""
        except subprocess.TimeoutExpired:
            _kill_group(proc)
            try:
                output, _ = proc.communicate(timeout=KILL_WAIT_S)
            except subprocess.TimeoutExpired:
                output = ""
            return None, output or ""
    finally:
        if proc.returncode is None:  # left early (timeout, an exception, Ctrl-C): take its children too
            _kill_group(proc)


def _minutes(value: float) -> str:
    return f"{value:g} min"


def command_digest(gate: Gate, env: Mapping[str, str] | None) -> str:
    """What makes two runs of `gate` the same check: its command, cwd, `when_exists` and the run's `defaults.env`."""
    raw = json.dumps([gate.run, gate.cwd or "", gate.when_exists or "", sorted((env or {}).items())])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def _run_one(gate: Gate, cwd: Path, log_dir: Path, tail_lines: int, timeout_min: float,
             env: dict[str, str] | None = None, env_base: Mapping[str, str] | None = None) -> GateResult:
    started = time.monotonic()
    result = _run_gate(gate, cwd, log_dir, tail_lines, timeout_min, env, env_base)
    return replace(result, duration_s=round(time.monotonic() - started, 3), cmd=command_digest(gate, env))


def _run_gate(gate: Gate, cwd: Path, log_dir: Path, tail_lines: int, timeout_min: float,
              env: dict[str, str] | None, env_base: Mapping[str, str] | None = None) -> GateResult:
    if gate.when_exists and not (cwd / gate.when_exists).exists():
        return GateResult(gate.name, False, True, f"{gate.when_exists} missing", None, "")
    run_dir = cwd / gate.cwd if gate.cwd else cwd
    if not run_dir.is_dir():
        reason = f"cwd {gate.cwd} does not exist in {cwd}"
        return GateResult(gate.name, False, False, reason, None, f"The gate did not run: {reason}.")
    code, output = _shell(gate.run, run_dir, max(1.0, timeout_min * 60), env, env_base)
    if code is None:
        output += f"\n[agentflow: killed after {_minutes(timeout_min)}]\n"
    log_path = log_dir / f"{gate.name}.log"
    try:
        safeio.write_text(log_path, f"$ {gate.run}\n{output}")
    except OSError as exc:
        return GateResult(gate.name, False, False, f"cannot write its log: {exc}", None, output[-2000:])
    tail = "\n".join(output.splitlines()[-tail_lines:]) if tail_lines > 0 else ""
    if code is None:
        return GateResult(gate.name, False, False, f"timed out after {_minutes(timeout_min)}", log_path, tail)
    passed = code == 0
    return GateResult(gate.name, passed, False, "" if passed else f"exit {code}", log_path, tail)


def run_gates(cfg: Config, names: str | list[str], cwd: Path, log_dir: Path, *,
              env_base: Mapping[str, str] | None = None) -> list[GateResult]:
    """Run gates in order (all of them, even after a failure) with `cwd` as the worktree root.

    A `str` names a `gate_sets` entry. Each gate's full output goes to `<log_dir>/<gate>.log`
    after a `$ <command>` line; `tail` keeps the last `context.gate_output_lines` lines. Each gate
    gets `defaults.gate_timeout_min`. Its environment is `env_base` (default: `run_env.base`, this process's
    environment without the variables a run does not inherit) with `defaults.env` on top.
    """
    cwd, log_dir = Path(cwd), Path(log_dir)
    if env_base is None:
        env_base = run_env.base(cfg)
    timeout = cfg.defaults.gate_timeout_min
    return [_run_one(g, cwd, log_dir, cfg.context.gate_output_lines, timeout, cfg.defaults.env, env_base)
            for g in _resolve(cfg, names)]


def setup_gate_names(cfg: Config) -> set[str]:
    """The gates of the `setup` set: they bring the environment up, so a pass of theirs is never reused."""
    try:
        return {g.name for g in _resolve(cfg, "setup")} if "setup" in cfg.gate_sets else set()
    except KeyError:
        return set()


def reusable_passes(events: list[dict], gates: list[Gate], tree: str | None, digests: Mapping[str, str],
                    never: set[str] | frozenset[str] = frozenset()) -> dict[str, dict]:
    """Gate name -> the latest `gate_result` that passed (not skipped) on exactly `tree` with the digest in `digests`.

    Pure. A gate in `never`, an event with no `tree` or no `cmd` (older runs), a failure and a skip never count."""
    if not tree:
        return {}
    wanted = {g.name for g in gates if g.name not in never and g.name in digests}
    found: dict[str, dict] = {}
    for e in events:
        if e.get("type") != "gate_result" or not e.get("passed") or e.get("skipped"):
            continue
        name = e.get("gate")
        if name in wanted and e.get("tree") == tree and e.get("cmd") and e.get("cmd") == digests[name]:
            found[name] = e
    return found


def run_gates_reusing(cfg: Config, run: Run, key: str, names: str | list[str], cwd: Path, log_dir: Path,
                      tree: str | None) -> list[GateResult]:
    """`run_gates`, except that a gate with a reusable pass on `tree` (`reusable_passes`) is not run again.

    The results keep the gates' order; a reused one is a pass with `reused_from` set, its old log and no tail.
    One `gates_reused` event names them (`record_results` writes no `gate_result` for a reused result, so the
    run's evidence stays one event per gate actually run)."""
    gates = _resolve(cfg, names)
    env = cfg.defaults.env
    digests = {g.name: command_digest(g, env) for g in gates}
    reused = reusable_passes(run.events.read(), gates, tree, digests, setup_gate_names(cfg))
    fresh = iter(run_gates(cfg, [g.name for g in gates if g.name not in reused], cwd, log_dir)
                 if len(reused) < len(gates) else [])
    results: list[GateResult] = []
    for g in gates:
        e = reused.get(g.name)
        if e is None:
            results.append(next(fresh))
            continue
        log = Path(e["log"]) if e.get("log") else None
        results.append(GateResult(g.name, True, False, f"reused: passed on this tree in {e.get('key')}", log, "",
                                  cmd=digests[g.name], reused_from=str(e.get("key"))))
    if reused:
        run.emit("gates_reused", key=key, tree=tree, gates=sorted(reused),
                 **{"from": sorted({str(e.get("key")) for e in reused.values()})})
    return results


def gates_ok(results: list[GateResult]) -> bool:
    """True when no gate that actually ran failed (skipped gates do not count as failures)."""
    return all(r.passed or r.skipped for r in results)


def record_results(run: Run, key: str, results: list[GateResult], tree: str, *, with_tail: bool = False) -> None:
    """Write one `gate_result` per result that ran, each stamped with the `tree` (a `guards.snapshot`) the gates ran
    on and the gate's `cmd` digest (a reused result writes none).

    The caller snapshots the worktree before the gates run. `with_tail` adds a `tail` to every event: a failed
    gate's output tail (the fix loop's and the retro's evidence), `""` for a gate that passed or was skipped."""
    for r in results:
        if r.reused_from is not None:  # `run_gates_reusing` already said so in its `gates_reused` event
            continue
        data = {"key": key, "gate": r.name, "passed": r.passed, "skipped": r.skipped, "reason": r.reason,
                "log": str(r.log_path) if r.log_path else None, "duration_s": r.duration_s, "tree": tree}
        if r.cmd:
            data["cmd"] = r.cmd
        if with_tail:  # "" for a gate that passed or was skipped
            data["tail"] = "" if (r.passed or r.skipped) else r.tail
        run.emit("gate_result", **data)


def failure_report(failed: list[GateResult]) -> str:
    """The stop report for failed gates: one `### <gate> (<reason>)` block each, with its log path and output tail."""
    blocks = []
    for r in failed:
        log = f"Full log: {r.log_path}\n" if r.log_path else ""
        blocks.append(f"### {r.name} ({r.reason})\n{log}```\n{r.tail}\n```")
    return "\n\n".join(blocks)


def summarize(results: list[GateResult]) -> str:
    """One line per gate: `✓ floor`, `✗ py_lint`, `– web_types (skipped: <reason>)`."""
    lines = []
    for r in results:
        if r.skipped:
            lines.append(f"– {r.name} (skipped: {r.reason})")
        elif r.passed:
            lines.append(f"✓ {r.name}")
        else:
            lines.append(f"✗ {r.name}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- doc checks

DECISIONS_LOG = "## Decisions Log"
OPEN_QUESTIONS = "## Open Questions"
HUNK = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")
TABLE_SEPARATOR = re.compile(r"^\|[\s|:-]*$")
ADR_FILE = "ADR-*.md"
EDITING_RULES = "the design doc's editing rules"
WORK_TREE = "the work tree"


@dataclass
class _DocDiff:
    """One run of the doc checks: the repo, the base commit and the snapshot tree of the work tree."""

    worktree: Path
    base: str
    tree: str
    idea: str
    decisions_dir: str

    def git(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["git", *args], cwd=self.worktree, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", check=False)

    def idea_lines(self) -> tuple[list[tuple[int, str]], list[tuple[int, str]]]:
        """`(removed, added)` lines of the idea doc as `(line number, text)`: old numbers, new numbers."""
        proc = self.git("diff-tree", "-p", "-U0", "--no-renames", "--no-color", "--no-ext-diff",
                        self.base, self.tree, "--", self.idea)
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip() or f"git diff-tree exited {proc.returncode}")
        removed: list[tuple[int, str]] = []
        added: list[tuple[int, str]] = []
        old = new = 0
        in_hunk = False
        for line in proc.stdout.splitlines():
            found = HUNK.match(line)
            if found:
                old, new, in_hunk = int(found.group(1)), int(found.group(2)), True
            elif in_hunk and line.startswith("-"):
                removed.append((old, line[1:]))
                old += 1
            elif in_hunk and line.startswith("+"):
                added.append((new, line[1:]))
                new += 1
        return removed, added

    def section(self, rev: str, heading: str) -> tuple[int, float] | None:
        """`(heading line, end line)` of the `heading` section of the idea doc at `rev` (end exclusive),
        or None when the doc or the heading is missing there."""
        if self.git("cat-file", "-e", f"{rev}:{self.idea}").returncode != 0:
            return None
        proc = self.git("grep", "-h", "-n", "-E", "^## ", rev, "--", self.idea)
        if proc.returncode not in (0, 1):
            raise RuntimeError(proc.stderr.strip() or f"git grep exited {proc.returncode}")
        headings = []
        for line in proc.stdout.splitlines():
            number, _, text = line.partition(":")
            headings.append((int(number), text.rstrip()))
        wanted = re.compile(rf"^{re.escape(heading)}\b")
        starts = [n for n, text in headings if wanted.match(text)]
        if not starts:
            return None
        start = starts[0]
        later = [n for n, _ in headings if n > start]
        return start, (later[0] if later else float("inf"))

    def changed_decisions(self) -> list[tuple[str, str]]:
        """`(status letter, path)` for each file under `decisions_dir` that differs from the base."""
        proc = self.git("diff-tree", "-r", "-z", "--name-status", "--no-renames", self.base, self.tree,
                        "--", self.decisions_dir)
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip() or f"git diff-tree exited {proc.returncode}")
        fields = [f for f in proc.stdout.split("\0") if f]
        return list(zip(fields[0::2], fields[1::2]))


def _inside(lines: list[tuple[int, str]], span: tuple[int, float]) -> list[str]:
    start, end = span
    return [text for number, text in lines if start < number < end]


def _result(name: str, passed: bool, reason: str, detail: str = "") -> GateResult:
    return GateResult(name, passed, False, reason, None, detail)


def _missing(name: str, d: _DocDiff, heading: str, where: str) -> GateResult:
    return _result(name, False, f"no `{heading}` section in {d.idea} in {where}",
                   f"{d.idea} has no `{heading}` heading in {where} (or the file is missing there), "
                   "so the runner cannot check it. Keep the idea doc and its sections in place.")


def _decisions_log_row(d: _DocDiff) -> GateResult:
    name = "decisions_log_row"
    span = d.section(d.tree, DECISIONS_LOG)
    if span is None:
        return _missing(name, d, DECISIONS_LOG, WORK_TREE)
    rows = [t for t in _inside(d.idea_lines()[1], span)
            if t.strip().startswith("|") and not TABLE_SEPARATOR.match(t.strip())]
    if rows:
        return _result(name, True, f"{len(rows)} row(s) added")
    return _result(name, False, f"no table row added under `{DECISIONS_LOG}`",
                   f"The diff from {d.base[:12]} adds no `| ... |` table row inside the `{DECISIONS_LOG}` "
                   f"section of {d.idea}. Add the decision's row there ({EDITING_RULES}).")


def _open_question_removed(d: _DocDiff) -> GateResult:
    name = "open_question_removed"
    span = d.section(d.base, OPEN_QUESTIONS)
    if span is None:
        return _missing(name, d, OPEN_QUESTIONS, f"base {d.base[:12]}")
    if d.section(d.tree, OPEN_QUESTIONS) is None:  # dropping the doc or the whole section resolves nothing
        return _missing(name, d, OPEN_QUESTIONS, WORK_TREE)
    gone = [t for t in _inside(d.idea_lines()[0], span) if t.strip()]
    if gone:
        return _result(name, True, f"{len(gone)} line(s) removed")
    return _result(name, False, f"nothing removed from `{OPEN_QUESTIONS}`",
                   f"The diff from {d.base[:12]} removes no line inside the `{OPEN_QUESTIONS}` section of "
                   f"{d.idea}. Remove the question the decision resolves ({EDITING_RULES}).")


def _adr_index_updated(d: _DocDiff) -> GateResult:
    name = "adr_index_updated"
    changed = d.changed_decisions()
    index = str(PurePosixPath(d.decisions_dir) / "README.md")
    new = [p for status, p in changed if status == "A" and str(PurePosixPath(p).parent) == d.decisions_dir
           and fnmatch.fnmatchcase(PurePosixPath(p).name, ADR_FILE)]
    if not new:
        return _result(name, True, "no new ADR")
    if any(p == index for _, p in changed):
        return _result(name, True, f"{len(new)} new ADR(s), index updated")
    return _result(name, False, f"new ADR without an index row: {', '.join(new)}",
                   f"The diff from {d.base[:12]} adds {', '.join(new)} but does not change {index}. "
                   f"Add the new ADR's row to the index (and the superseded ADR's status) ({EDITING_RULES}).")


# The names here are `config.DOC_CHECK_NAMES` (config cannot import this module); a test keeps them equal.
_DOC_CHECKS: dict[str, Callable[[_DocDiff], GateResult]] = {
    "decisions_log_row": _decisions_log_row,
    "open_question_removed": _open_question_removed,
    "adr_index_updated": _adr_index_updated,
}


def run_doc_checks(names: list[str], worktree: Path, base: str, idea: str, decisions_dir: str) -> list[GateResult]:
    """Run the named doc checks, in order, on the diff from `base` to the work tree as it is now.

    - `decisions_log_row`: the diff adds a `|` table row (not a `|---|` separator) inside `## Decisions Log`;
    - `open_question_removed`: the diff removes a non-blank line inside `## Open Questions`;
    - `adr_index_updated`: a new `<decisions_dir>/ADR-*.md` implies a changed `<decisions_dir>/README.md`
      (passes, as "no new ADR", when there is none).

    A missing doc or section, or a git error, fails the check with the reason. Each result is a
    `GateResult` (never skipped, no log file; `tail` tells the agent what to fix), so failures feed a
    step's fix loop like gate failures. Raises `ValueError` for an unknown check name.
    """
    unknown = [n for n in names if n not in _DOC_CHECKS]
    if unknown:
        raise ValueError(f"unknown doc check(s): {', '.join(unknown)}")
    worktree = Path(worktree)
    d = _DocDiff(worktree, base, snapshot(worktree), idea, decisions_dir.rstrip("/"))
    results = []
    for name in names:
        try:
            results.append(_DOC_CHECKS[name](d))
        except RuntimeError as exc:
            results.append(_result(name, False, f"git failed: {exc}", f"The runner could not read the diff: {exc}"))
    return results
