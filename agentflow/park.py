"""The independence rule for parking a stopped task: which tasks must wait for it and which may go on (pure).

A task waits on a held task when it depends on one (`depends_on`, so transitively, because a waiting task is held
too) or when it names a file the held task names. A task that names no file shares none. A task in `done` never waits.
`held_tasks` walks the plan in order, which is how the plan walk itself decides; `blocks` and `blast_radius` read the
same walk, so the plan view, the inbox and the runner cannot disagree.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence

from agentflow.sources import PlanTask, task_files


def waits_on(task: PlanTask, held: Collection[int], by_n: Mapping[int, PlanTask]) -> bool:
    """Whether `task` depends on a held task or names a file a held task names."""
    if any(dep in held for dep in task.depends_on):
        return True
    mine = set(task_files(task))
    if not mine:
        return False
    return any(n in by_n and mine.intersection(task_files(by_n[n])) for n in held)


def held_tasks(tasks: Sequence[PlanTask], parked: Collection[int], done: Collection[int]) -> set[int]:
    """The tasks held back: those not in `done` that are parked or wait on a task held earlier in plan order."""
    by_n = {t.n: t for t in tasks}
    held: set[int] = set()
    for task in tasks:
        if task.n in done:
            continue
        if task.n in parked or waits_on(task, held, by_n):
            held.add(task.n)
    return held


def blocks(tasks: Sequence[PlanTask], n: int, done: Collection[int]) -> list[int]:
    """The tasks that wait while task `n` is parked, sorted; never `n` itself."""
    return sorted(held_tasks(tasks, {n}, done) - {n})


def blast_radius(tasks: Sequence[PlanTask], parked: Collection[int], n: int, done: Collection[int]) -> dict[str, list[int]]:
    """`{blocks: [n, *what waits on it], continue: the open tasks that can still run}` with `n` and `parked` held."""
    held = held_tasks(tasks, {*parked, n}, done)
    return {"blocks": [n, *blocks(tasks, n, done)],
            "continue": [t.n for t in tasks if t.open and t.n not in done and t.n not in held]}


def ranges(numbers: Sequence[int]) -> str:
    """`[10, 11, 12, 13, 15]` -> "10–13, 15": a run of three or more consecutive numbers is `a–b` (an en dash)."""
    ordered = sorted(set(numbers))
    out: list[str] = []
    i = 0
    while i < len(ordered):
        j = i
        while j + 1 < len(ordered) and ordered[j + 1] == ordered[j] + 1:
            j += 1
        if j - i >= 2:
            out.append(f"{ordered[i]}–{ordered[j]}")
        else:
            out.extend(str(k) for k in ordered[i:j + 1])
        i = j + 1
    return ", ".join(out)


def radius_line(radius: Mapping[str, list[int]]) -> str:
    """One line for a person: "blocks tasks 9, 14; tasks 10–13, 15–23 continue"."""
    held, going = list(radius.get("blocks", [])), list(radius.get("continue", []))
    first = f"blocks task{'s' if len(held) > 1 else ''} {ranges(held)}"
    if not going:
        return f"{first}; no other task can run"
    return f"{first}; task{'s' if len(going) > 1 else ''} {ranges(going)} continue{'' if len(going) > 1 else 's'}"
