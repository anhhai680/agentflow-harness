"""Give each live run of a repository its own slot number, so runs can share one machine.

With `slots.count` above 1, every live run holds one number from `0` to `count - 1`, and `{slot}` in the config's
templates (`defaults.env` values, a gate's `run` and its `affected.run`) is that number: a port, a compose project
name or a database name that two runs at once never share. With `slots.count: 1` (the default) slots are off: no
event is written, no key is added to the state, nothing is refused, and `{slot}` renders as `0`.

The lease: a run holds its slot for as long as its status is not terminal (`done`, `failed`, `cancelled`). That
covers a wait for a human, a pause, an error, a stalled runner and every hold, `ci_pending` included, because the
run's containers are still up on that number. A number at or above a lowered `slots.count` is held by nobody. The
holder is read from the runs' own state (`slot_acquired` reduces to `state["slot"]`), so a crash leaves nothing to
clean up: a run that ends gives its number back by ending.

`acquire` takes the lowest free number under one file lock (`runs/slots.lock`) and records it with `slot_acquired`,
so two runners starting at once never take the same number. A run that already holds a number below `count` that no
other run holds keeps it, and writes nothing. A resumed run whose number was taken meanwhile takes another one
(`slot_acquired` carries `previous`). With no free number, `SlotsFull` names every holder. `queue_hold` is the
reason `agentflow queue` waits, so it leaves `queue.human_slot` numbers free for a run started by hand.

`render` returns a copy of the config with the token filled in. It is applied once, to the run's own copy, and its
output is never rendered again (`gates` turns `{{` into `{` when it fills `{paths}`, and `SLOT_TOKEN` leaves a shell
`${slot}` and a doubled `{{slot}}` alone).
"""

from __future__ import annotations

import contextlib
import fcntl
import os
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

from agentflow.config import SLOT_TOKEN, Config
from agentflow.state import TERMINAL, Run, list_runs, runs_root

LOCK_NAME = "slots.lock"


class SlotsFull(Exception):
    """Every slot is held by a live run."""


def enabled(cfg: Config) -> bool:
    return cfg.slots.count > 1


def _fill(text: str, slot: int) -> str:
    return SLOT_TOKEN.sub(str(slot), text)


def render(cfg: Config, slot: int) -> Config:
    """A copy of `cfg` with `{slot}` replaced by `slot` in `defaults.env` values, each gate's `run` and each
    gate's `affected.run`. Nothing else changes and `cfg` is not mutated."""
    env = {name: _fill(value, slot) for name, value in cfg.defaults.env.items()}
    gates = {}
    for name, gate in cfg.gates.items():
        affected = gate.affected
        if affected is not None:
            affected = replace(affected, run=_fill(affected.run, slot))
        gates[name] = replace(gate, run=_fill(gate.run, slot), affected=affected)
    return replace(cfg, defaults=replace(cfg.defaults, env=env), gates=gates)


def slot_of(run: Run) -> int:
    """The run's slot, or `0` when it has none (slots off, or a run from before they were on)."""
    slot = run.state.get("slot")
    return slot if isinstance(slot, int) and not isinstance(slot, bool) else 0


def _holds(cfg: Config, run: Run) -> int | None:
    slot = run.state.get("slot")
    if run.state["status"] in TERMINAL or not isinstance(slot, int) or isinstance(slot, bool):
        return None
    return slot if 0 <= slot < cfg.slots.count else None


def held(cfg: Config, repo: Path) -> dict[int, str]:
    """Slot number to the id of the live run that holds it (the oldest, were two to claim one)."""
    out: dict[int, str] = {}
    for run in list_runs(repo):
        slot = _holds(cfg, run)
        if slot is not None:
            out.setdefault(slot, run.id)
    return out


def free(cfg: Config, repo: Path) -> list[int]:
    taken = held(cfg, repo)
    return [n for n in range(cfg.slots.count) if n not in taken]


def _full(cfg: Config, taken: dict[int, str]) -> SlotsFull:
    who = ", ".join(f"{n} by {run_id}" for n, run_id in sorted(taken.items()))
    return SlotsFull(f"all {cfg.slots.count} slots are held: {who}; cancel one of them or raise slots.count")


def check_free(cfg: Config, repo: Path) -> None:
    """Raise `SlotsFull` when slots are on and every one is held. Only a quick look: `acquire` decides."""
    if not enabled(cfg):
        return
    taken = held(cfg, repo)
    if len(taken) >= cfg.slots.count:
        raise _full(cfg, taken)


@contextlib.contextmanager
def _lock(repo: Path) -> Iterator[None]:
    """The repository's slots lock: held while a run reads who holds what and records its own number."""
    root = runs_root(repo)
    root.mkdir(parents=True, exist_ok=True)
    fd = os.open(root / LOCK_NAME, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)  # closing the descriptor drops the lock


def acquire(cfg: Config, run: Run) -> int:
    """The run's slot: its own when it holds one nobody else does, else the lowest free number (recorded with
    `slot_acquired`). `0`, and nothing written, when slots are off. Raises `SlotsFull` when none is free."""
    if not enabled(cfg):
        return 0
    with _lock(run.repo):
        run.refresh()
        taken = {n: rid for n, rid in held(cfg, run.repo).items() if rid != run.id}
        own = run.state.get("slot")
        own = own if isinstance(own, int) and not isinstance(own, bool) else None
        if own is not None and 0 <= own < cfg.slots.count and own not in taken:
            return own
        for n in range(cfg.slots.count):
            if n not in taken:
                extra = {"previous": own} if own is not None else {}
                run.emit("slot_acquired", slot=n, count=cfg.slots.count, **extra)
                return n
        raise _full(cfg, taken)


def queue_hold(cfg: Config, repo: Path) -> str | None:
    """Why `agentflow queue` must not start another run now, or `None`: it keeps `queue.human_slot` slots free."""
    if not enabled(cfg):
        return None
    if len(free(cfg, repo)) > cfg.queue.human_slot:
        return None
    count, keep = cfg.slots.count, cfg.queue.human_slot
    return (f"waiting for a free slot: {len(held(cfg, repo))} of {count} held, "
            f"queue.human_slot keeps {keep} free")
