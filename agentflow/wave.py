"""Which tasks of a parallel group form a wave: the tasks of one `**Parallel group:**` that are built together.

Pure: it reads no file, emits nothing and has no clock. The plan's author puts tasks in a group and the runner never
does (it only points out pairs that could share one); a task that fails a check here is demoted, so it is built on
its own, in plan order, as it would be with no group at all.
"""

from __future__ import annotations

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


@dataclass(frozen=True)
class Demotion:
    task: int
    reason: str


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
