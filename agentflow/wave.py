"""Which tasks of a parallel group form a wave: the tasks of one `**Parallel group:**` that are built together.

Pure: it reads no file, emits nothing and has no clock. The plan's author puts tasks in a group and the runner never
does (it only points out pairs that could share one); a task that fails a check here is demoted, so it is built on
its own, in plan order, as it would be with no group at all.
"""

from __future__ import annotations

import fnmatch
import posixpath
import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass

from agentflow import sources
from agentflow.guards import path_matches
from agentflow.sources import PlanTask

REASONS = (
    "no_files",
    "serial_path",
    "risk",
    "security",
    "depends_on_member",
    "dependency_open",
    "shared_files",
    "model",
    "dissolved",
    "blocked",
    "needs_human",
    "no_report",
    "scope_violation",
)

MAX_PAIR_LINES = 10
_PAIRS_LEAD = (
    "The runner found tasks with no dependency and no shared file between them. "
    "Check whether they change the same contract before suggesting a group:"
)


REPORT_STATUSES = ("done", "blocked", "needs_human")
TASK_DESCRIPTION = re.compile(r"^\s*(?:Review\s+)?Task\s+(\d+)\s*:")
GENERAL_AGENT = "general-purpose"  # the only sub-agent type a wave may start: it runs the session's model
_TEST_NAMES = ("test_*", "*_test.*", "*.test.*", "*.spec.*")


@dataclass(frozen=True)
class Demotion:
    task: int
    reason: str


@dataclass(frozen=True)
class Report:
    n: int
    status: str
    summary: str
    files: tuple[str, ...]
    tests_run: tuple[str, ...]


@dataclass(frozen=True)
class Attribution:
    owners: dict[int, list[str]]
    strays: dict[str, int | None]


def depends_transitively(by_n: Mapping[int, PlanTask], start: int, target: int) -> bool:
    """Whether `start` reaches `target` through `depends_on`, directly or through other tasks."""
    seen: set[int] = set()
    stack = [start]
    while stack:
        task = by_n.get(stack.pop())
        if task is None:
            continue
        for dep in task.depends_on:
            if dep == target:
                return True
            if dep not in seen:
                seen.add(dep)
                stack.append(dep)
    return False


def group_candidates(tasks: Sequence[PlanTask], at: PlanTask, skip: Collection[int]) -> list[PlanTask]:
    """`at`, then the open tasks after it in plan order that share its group, minus those in `skip`."""
    if at.group is None:
        return []
    position = next((i for i, t in enumerate(tasks) if t.n == at.n), len(tasks))
    later = [t for t in tasks[position + 1 :] if t.group == at.group and t.open and t.n not in skip]
    return [at, *later]


def _touches_serial(files: Sequence[str], serial_paths: Sequence[str]) -> bool:
    return any(path_matches(path, glob) for path in files for glob in serial_paths)


def form_wave(
    candidates: Sequence[PlanTask],
    by_n: Mapping[int, PlanTask],
    *,
    committed: Collection[int],
    serial_paths: Sequence[str],
    risky: Collection[int],
    security: Collection[int],
    model_of: Mapping[int, str],
    cap: int,
) -> tuple[list[int], list[Demotion]]:
    """The candidates that may be built together, and why the others may not. Each candidate gets the first reason
    that applies. At `cap` members it stops adding, and the rest are left for the next wave, not demoted. A wave of
    one is no wave: the result is then `([], demotions of the others)`."""
    members: list[PlanTask] = []
    member_files: set[str] = set()
    demotions: list[Demotion] = []
    for task in candidates:
        if len(members) >= cap:
            break
        reason = _demotion_reason(
            task, by_n, members, member_files, committed, serial_paths, risky, security, model_of
        )
        if reason is None:
            members.append(task)
            member_files.update(sources.task_files(task))
        elif not members:
            return [], [Demotion(task.n, reason)]
        else:
            demotions.append(Demotion(task.n, reason))
    if len(members) < 2:
        return [], demotions
    return [t.n for t in members], demotions


def _demotion_reason(
    task: PlanTask,
    by_n: Mapping[int, PlanTask],
    members: Sequence[PlanTask],
    member_files: set[str],
    committed: Collection[int],
    serial_paths: Sequence[str],
    risky: Collection[int],
    security: Collection[int],
    model_of: Mapping[int, str],
) -> str | None:
    files = sources.task_files(task)
    if not files:
        return "no_files"
    if _touches_serial(files, serial_paths):
        return "serial_path"
    if task.n in risky:
        return "risk"
    if task.n in security:
        return "security"
    if any(depends_transitively(by_n, task.n, member.n) for member in members):
        return "depends_on_member"
    if any(dep not in committed for dep in task.depends_on):
        return "dependency_open"
    if member_files.intersection(files):
        return "shared_files"
    if members and model_of.get(task.n) != model_of.get(members[0].n):
        return "model"
    return None


def candidates(tasks: Sequence[PlanTask], serial_paths: Sequence[str]) -> list[tuple[int, int]]:
    """Pairs `(a, b)`, `a < b`, of open ungrouped tasks that could share a group: both name files, the files are
    disjoint, neither touches a serial path and neither depends, even through other tasks, on the other."""
    by_n = {t.n: t for t in tasks}
    files = {
        t.n: sources.task_files(t)
        for t in tasks
        if t.open and t.group is None
    }
    named = sorted(n for n, paths in files.items() if paths and not _touches_serial(paths, serial_paths))
    return [
        (a, b)
        for i, a in enumerate(named)
        for b in named[i + 1 :]
        if not set(files[a]).intersection(files[b])
        and not depends_transitively(by_n, a, b)
        and not depends_transitively(by_n, b, a)
    ]


def candidates_text(pairs: Sequence[tuple[int, int]]) -> str:
    """The pairs as a note for the planner (at most `MAX_PAIR_LINES` of them), or `""` for none."""
    if not pairs:
        return ""
    lines = [
        f"- Tasks {a} and {b} could share a **Parallel group:** line: no dependency between them and no shared file."
        for a, b in pairs[:MAX_PAIR_LINES]
    ]
    return "\n".join([_PAIRS_LEAD, *lines])


def predict_waves(
    tasks: Sequence[PlanTask], cap: int, serial_paths: Sequence[str], security: Collection[int]
) -> list[list[int]]:
    """The waves the plan's groups would form, in plan order, for `explain`. Every task before the one it is at counts
    as committed, nothing is risky and every task has the same model."""
    by_n = {t.n: t for t in tasks}
    model_of = {t.n: "" for t in tasks}
    placed: set[int] = set()
    waves: list[list[int]] = []
    for i, at in enumerate(tasks):
        if at.n in placed or not at.open:
            continue
        wave, _ = form_wave(
            group_candidates(tasks, at, placed),
            by_n,
            committed={t.n for t in tasks[:i]},
            serial_paths=serial_paths,
            risky=(),
            security=security,
            model_of=model_of,
            cap=cap,
        )
        if wave:
            waves.append(wave)
            placed.update(wave)
    return waves


def _members_once(raw: object, members: Collection[int]) -> dict[int, dict]:
    """The entries of `raw` whose `n` is a member, by `n`. A member named twice is dropped, whatever the entries hold,
    and anything that is not a list of such entries gives `{}`."""
    if not isinstance(raw, list):
        return {}
    named: dict[int, list[dict]] = {}
    for entry in raw:
        n = entry.get("n") if isinstance(entry, dict) else None
        if isinstance(n, int) and not isinstance(n, bool) and n in members:
            named.setdefault(n, []).append(entry)
    return {n: entries[0] for n, entries in named.items() if len(entries) == 1}


def _strings(value: object) -> tuple[str, ...] | None:
    """`value` as a tuple of strings, `()` when it is missing, `None` when it is anything else."""
    if value is None:
        return ()
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return tuple(value)
    return None


def parse_reports(raw: object, members: Collection[int]) -> dict[int, Report]:
    """The per-task reports of a wave session's result (its `tasks` value). It fails closed: a member whose entry is
    malformed or named twice has no report, and so is set aside."""
    reports: dict[int, Report] = {}
    for n, entry in _members_once(raw, members).items():
        files = _strings(entry.get("files"))
        tests_run = _strings(entry.get("tests_run"))
        status, summary = entry.get("status"), entry.get("summary")
        if status in REPORT_STATUSES and isinstance(summary, str) and files is not None and tests_run is not None:
            reports[n] = Report(n, status, summary, files, tests_run)
    return reports


def parse_review_reports(raw: object, members: Collection[int]) -> dict[int, list[dict]]:
    """The per-task findings of a wave review session, under the rules of `parse_reports`. A missing `findings` is
    `[]`."""
    reviews: dict[int, list[dict]] = {}
    for n, entry in _members_once(raw, members).items():
        findings = entry.get("findings", [])
        if isinstance(findings, list) and all(isinstance(item, dict) for item in findings):
            reviews[n] = findings
    return reviews


def is_test_path(path: str) -> bool:
    name = posixpath.basename(path)
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in _TEST_NAMES)


def allowed_dirs(task: PlanTask) -> set[str]:
    """The directories of the task's declared files that are test paths: new test files may appear there."""
    return {posixpath.dirname(path) for path in sources.task_files(task) if is_test_path(path)}


def attribute(
    changed: Sequence[str], members: Sequence[PlanTask], reports: Mapping[int, Report], plan_rel: str
) -> Attribution:
    """Whose each changed path is. A declared file of exactly one member is that member's. A test path in the
    `allowed_dirs` of one member is that member's, and when several members hold the directory it goes to the one
    member whose report names it. Any other path is a stray, charged to the first member in plan order whose report
    names it, else to `None`; the plan file is always a stray."""
    declared = {t.n: set(sources.task_files(t)) for t in members}
    dirs = {t.n: allowed_dirs(t) for t in members}
    reported = {t.n: set(reports[t.n].files) for t in members if t.n in reports}
    owners: dict[int, list[str]] = {t.n: [] for t in members}
    strays: dict[str, int | None] = {}
    for path in dict.fromkeys(changed):
        owner = None if path == plan_rel else _owner(path, declared, dirs, reported)
        if owner is not None:
            owners[owner].append(path)
        else:
            strays[path] = next((n for n in owners if path in reported.get(n, ())), None)
    return Attribution(owners, strays)


def _owner(path: str, declared: Mapping[int, set[str]], dirs: Mapping[int, set[str]], reported: Mapping[int, set[str]]) -> int | None:
    declaring = [n for n, paths in declared.items() if path in paths]
    if len(declaring) == 1:
        return declaring[0]
    if not is_test_path(path):
        return None
    holding = [n for n, held in dirs.items() if posixpath.dirname(path) in held]
    if len(holding) == 1:
        return holding[0]
    naming = [n for n in holding if path in reported.get(n, ())]
    return naming[0] if len(naming) == 1 else None


def dispatch_violations(subagents: Sequence[Mapping], members: Collection[int], model: str) -> list[str]:
    """One line for each sub-agent a wave session started that names no member (its description must start
    `Task <n>:` or `Review Task <n>:`), runs a model that is neither `inherit`, missing, nor the wave's own, is of an
    agent type other than `general-purpose` (a custom type with no `model` runs its own model, which the stream still
    calls `inherit`), or whose call came back as an error (`ok` False: a refused call looks like a good one otherwise)."""
    lines: list[str] = []
    for entry in subagents:
        description = entry.get("description")
        match = TASK_DESCRIPTION.match(description) if isinstance(description, str) else None
        if match is None or int(match.group(1)) not in members:
            lines.append(f"sub-agent description names no member of the wave: {description!r}")
        agent_model = entry.get("model")
        if agent_model not in (None, "inherit", model):
            lines.append(f"sub-agent model is not the wave's ({model}): {agent_model!r}")
        agent_type = entry.get("subagent_type", GENERAL_AGENT)
        if agent_type not in (None, GENERAL_AGENT):
            lines.append(f"sub-agent type is not {GENERAL_AGENT}: {agent_type!r} ({description!r})")
        if entry.get("ok") is False:
            lines.append(f"sub-agent call failed: {description!r}")
    return lines
