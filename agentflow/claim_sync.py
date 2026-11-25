"""A run's side of the claims (`claims`): the claim a start makes on its backlog item, before the run exists.

A start claims its item when `backlog_writes` would write to it (a `module` param in the item's lane, or in
`module_lane` when the item names none), in a `backlog` pack with `backlog.claims` on and an `origin` remote
(`claims.enabled`); any other start claims nothing (`target` is None). The claim names the run, so `start` reserves the
run's id first (`Run.next_id`) and the caller creates exactly that run once the claim is made. A refused claim is a
`ValueError` whose text is the refusal, with nothing created: no run, no worktree, no branch, no claim.

Once the run exists, `sync` moves the claim with the run's phase and status (`CLAIM_STATUS`), and releases it when
the run fails, is cancelled or is archived; a done run keeps its claim, `waiting`, until its PR merges. A claim another
clone deleted or replaced is never pushed over: the run stops with `claim_lost` (a person's stop, `NEVER_RESOLVED`),
and once that stop is approved the next `sync` claims the item again, or stops again naming whoever holds it. A run
that holds no claim (`state["claim"]` is None) makes no git call here. `origin` that can't be reached is not a lost
claim: `claim_sync_failed` is recorded, the run goes on, and the next `sync` catches the claim up. The syncs of one
run are serialised by a lock file in its directory, so two processes of it never race their own pushes.

Text that came from `origin` (a claimant's `by`, git's messages) is validated by `claims` or cut here to
`TEXT_MAX` printable characters before it reaches an event or a refusal.
"""
from __future__ import annotations

import fcntl
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from agentflow import backlog_writes, claims, gitops, pack, work_source
from agentflow.backlog_file import BacklogItem
from agentflow.config import CLAIM_STEP_ID, Config, backlog_path
from agentflow.hitl import raise_hitl
from agentflow.state import TERMINAL, Run

if TYPE_CHECKING:
    from agentflow.pipeline import RunOptions

TEXT_MAX = 300
NO_CLAIMS = "set backlog.claims: false to start without claims"

CLAIM_STEP = CLAIM_STEP_ID  # the stop of a lost claim is `<phase>/claim`, a step id `validate` reserves
LOCK_FILE = "claim.lock"
# The run's status to its claim's (`claims.STATUSES`); `failed` and `cancelled` release the claim instead.
CLAIM_STATUS = {"running": "running", "refining": "running", "waiting_human": "waiting",
                "awaiting_approval": "waiting", "ready": "waiting", "paused": "paused", "error": "stopped",
                "done": "waiting"}
RELEASED_BY = ("failed", "cancelled")

__all__ = ["CLAIM_STATUS", "record", "release_run", "release_start", "start", "sync", "sync_quietly", "target"]


def _cut(text: object) -> str:
    """`text` as one printable line of at most `TEXT_MAX` characters."""
    return "".join(c if c.isprintable() else "?" for c in str(text))[:TEXT_MAX]


def _item(cfg: Config, repo: Path, item_id: str) -> BacklogItem | None:
    """Item `item_id` as `backlog_writes.plan_start` finds it: in the file at the tip of `project.fork_from`, else in
    the main checkout's work tree (read only), else None."""
    sha = gitops.commit_at(repo, cfg.project.fork_from)
    found = next((i for i in work_source.items_at(cfg, repo, sha) if i.id == item_id), None)
    return found if found is not None else work_source.item(cfg, pack.checkout_root(repo), item_id)


def target(cfg: Config, repo: Path, pipeline: str, params: dict, new_module: dict | None) -> str | None:
    """The item id a start of `pipeline` with `params` claims, or None when it claims nothing. A `--new-module` item
    is claimed by its id, though it exists only in the run's first commit: its lane is the run's (`plan_start`)."""
    item_id = str(params.get("module") or "")
    if not item_id or backlog_path(cfg.work_source) is None or not claims.enabled(cfg, Path(repo)):
        return None
    if new_module is not None:
        return item_id
    return item_id if backlog_writes.applies_to(cfg, pipeline, params, _item(cfg, Path(repo), item_id)) else None


def _first_phase(cfg: Config, pipeline: str, opts: RunOptions) -> str:
    from agentflow.views import lane_first_phase  # here, not at the top: `views` imports `run_control`

    return opts.from_phase or lane_first_phase(cfg, pipeline) or next(iter(cfg.phases))


def _claimed(item_id: str, held: claims.Claim) -> str:
    if held.unreadable:
        return (f"module {item_id} has a claim on origin that agentflow can't read ({_cut(held.unreadable)}): "
                f"agentflow backlog release {item_id} frees it")
    return (f"module {item_id} is claimed by {held.by}: run {held.run}, phase {held.phase}, {held.status}, "
            f"updated {held.updated} (agentflow backlog release {item_id} frees it)")


def start(cfg: Config, repo: Path, pipeline: str, params: dict, opts: RunOptions,
          new_module: dict | None) -> tuple[str, claims.Claim] | None:
    """Claim the item of a start that claims one (`target`), for the run id `Run.next_id` reserves; the id and the
    claim, or None when the start claims nothing. A claim that can't be made raises `ValueError` with the refusal:
    the item is claimed (by whom, or that the claim can't be read), `origin` refused the ref, or `origin` can't be
    reached. Nothing is left behind by a refusal."""
    item_id = target(cfg, repo, pipeline, params, new_module)
    if item_id is None:
        return None
    repo = Path(repo)
    run_id = Run.next_id(cfg, repo, pipeline, params)
    try:
        held = claims.create(cfg, repo, item_id, run=run_id, lane=pipeline, phase=_first_phase(cfg, pipeline, opts))
    except claims.Claimed as exc:
        raise ValueError(_claimed(item_id, exc.claim)) from exc
    except claims.HostRefused as exc:
        raise ValueError(f"origin refused the claim ref {claims.ref(cfg, item_id)}: set backlog.claim_ref: branch in "
                         "config.yaml, or backlog.claims: false to start without claims") from exc
    except claims.Unreachable as exc:
        raise ValueError(f"could not reach origin to claim {item_id}: {_cut(exc)}; start again when it is reachable, "
                         f"or {NO_CLAIMS}") from exc
    except claims.ClaimError as exc:  # a local git failure while the claim's commit was built
        raise ValueError(f"could not claim {item_id}: {_cut(exc)}; start again, or {NO_CLAIMS}") from exc
    return run_id, held


def record(cfg: Config, run: Run, held: claims.Claim) -> None:
    """Record the claim `held` as the run's: `item_claimed`, with its ref."""
    run.emit("item_claimed", item=held.item, ref=claims.ref(cfg, held.item), sha=held.sha, phase=held.phase,
             status=held.status)


def release_start(cfg: Config, repo: Path, run: Run | None, held: claims.Claim, reason: str = "failed") -> str:
    """Release the claim of a start that failed (`reason: failed`) or was interrupted (`interrupted`) after it was
    made (a leased delete), recording `claim_released` or `claim_release_failed` on `run` when the run exists. Never
    raises: returns "" when the claim was released, else why not. A run interrupted before it recorded the claim
    records it first, so the release marks it and the run's next `sync` stops it rather than run unclaimed."""
    if run is not None and _row(run.refresh()) is None:
        record(cfg, run, held)
    try:
        claims.release(cfg, Path(repo), held)
    except Exception as exc:  # never hide the failure of the start itself
        why = _error(exc)
        if run is not None:
            run.emit("claim_release_failed", item=held.item, error=why)
        return why
    if run is not None:
        run.emit("claim_released", item=held.item, reason=reason)
    return ""


# --------------------------------------------------------------------------- the claim follows the run


def _error(exc: BaseException) -> str:
    return _cut(str(exc) or type(exc).__name__)


@contextmanager
def _serial(run: Run) -> Iterator[None]:
    """Hold the run's claim lock: one `sync` of the run at a time, across its processes, so the claim's pushes and
    their events stay in order. It is never held while the run's log lock is."""
    fd = os.open(run.dir / LOCK_FILE, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)  # closing it releases the lock


def _row(state: dict) -> dict | None:
    row = state.get("claim")
    return row if isinstance(row, dict) else None


def _idle(state: dict) -> bool:
    """Nothing for `sync` to do, decided from the state alone: no claim, or an ended run whose claim is released or
    lost (a lost claim is not the run's to release)."""
    row = _row(state)
    return row is None or (state["status"] in TERMINAL and bool(row.get("released") or row.get("lost")))


def _lost_stop_open(state: dict) -> bool:
    """A `claim_lost` stop no person has settled yet: the run's wait, queued behind an approval, or parked."""
    waits = [state.get("waiting") or {}, *state.get("waiting_queue", []), *(state.get("parked") or {}).values()]
    return any(w.get("kind") == "claim_lost" for w in waits)


def _phase(state: dict, row: dict) -> str:
    return str(state.get("phase") or row.get("phase"))


def _summary(item: str, why: str) -> str:
    return f"Stopped before changing the claim on {item} on origin: {why}. Nothing was pushed over it."


def _lose(cfg: Config, run: Run, item: str, phase: str, why: str) -> bool:
    """Record that the run no longer holds its claim (`claim_lost`, naming its stop's key) and stop the run for a
    person (`claim_lost`, never resolved by the runner); False, the value `sync` returns."""
    why, key = _cut(why), f"{phase}/{CLAIM_STEP}"
    run.emit("claim_lost", item=item, why=why, key=key)
    raise_hitl(cfg, run, "claim_lost", key, _summary(item, why))
    return False


def _last_lost(run: Run, phase: str) -> tuple[dict, str, bool]:
    """The run's last `claim_lost` event, its stop's key, and whether a person approved that stop since: a
    `hitl_approved` on the key after the event (`approve`, or `retry` of the key). Only that settles it: a stop that
    left the wait any other way (dropped with a speculated phase, never raised) is not an approval."""
    events = run.events.read()
    lost = next((e for e in reversed(events) if e["type"] == "claim_lost"), {})
    key = str(lost.get("key") or f"{phase}/{CLAIM_STEP}")
    approved = any(e["type"] == "hitl_approved" and e.get("key") == key and e["seq"] > lost.get("seq", -1)
                   for e in events)
    return lost, key, approved


def sync(cfg: Config, run: Run) -> bool:
    """Bring the run's claim on `origin` to its phase and status now; False when the run must stop, having raised
    (or still waiting on) its `claim_lost` stop.

    The claim moves (`claims.advance`, leased on the sha the run holds) only when `(phase, CLAIM_STATUS[status])`
    differs from what `state["claim"]` records, so a second `sync` with no change pushes nothing. A `failed` or
    `cancelled` run, or an archived one, releases it (`release_run`). A claim that is gone from `origin` or held by
    another claim stops the run (`claim_lost`); so does a live run whose claim was released (its start was
    interrupted after the claim): it never claims again by itself. Only once a person approved that stop
    (`hitl_approved` on its key, after the last `claim_lost`) does the next `sync` claim the item again
    (`claims.create`, which needs the ref not to exist), and it stops again naming the claimant when another holds
    it; a stop that left the wait without an approval is raised again. When the run's own claim commit can't be read
    here, `origin` is asked: its claim is adopted only when it is this run's. A claim `origin` could not be reached for is `claim_sync_failed`, and True: the run goes on.
    A run with no claim, or a released one, is a no-op: True."""
    if _idle(run.refresh()):
        return True
    with _serial(run):
        return _sync(cfg, run)


def _sync(cfg: Config, run: Run) -> bool:
    state = run.refresh()  # again, under the claim lock: another process may have moved the claim meanwhile
    row = _row(state)
    if row is None or _idle(state):
        return True
    status, item, phase = state["status"], str(row["item"]), _phase(state, row)
    if status in RELEASED_BY or (status in TERMINAL and state.get("archived")):
        if not row["released"] and not row["lost"]:
            release_run(cfg, run, status if status in RELEASED_BY else "archived")
        return True
    if row["lost"]:
        if _lost_stop_open(state):
            return False
        lost, key, approved = _last_lost(run, phase)
        if not approved:  # its stop is not open and no person approved it: raise it again, never claim by itself
            raise_hitl(cfg, run, "claim_lost", key, _summary(item, _cut(lost.get("why") or "the claim was lost")))
            return False
        return _claim_again(cfg, run, state, item, phase, key)
    if row["released"]:
        if status in TERMINAL:
            return True
        return _lose(cfg, run, item, phase, f"the claim on {item} was released when this run's start was "
                                            "interrupted, and the run claims nothing again by itself")
    want = CLAIM_STATUS.get(status)
    if want is None or (phase, want) == (row["phase"], row["status"]):
        return True
    repo = Path(run.repo)
    try:
        held = _held(cfg, repo, run, item, str(row["sha"]))
        if isinstance(held, str):
            return _lose(cfg, run, item, phase, held)
        moved = claims.advance(cfg, repo, held, phase=phase, status=want)
    except claims.Lost as exc:
        if claims.own(repo, str(row["sha"]), run.id, exc.claim):  # another process of this run moved it twice
            run.emit("claim_sync_failed", item=item, action="advance", error=_error(exc))
            return True
        return _lose(cfg, run, item, phase, str(exc))
    except (claims.ClaimError, ValueError, OSError) as exc:  # `origin` can't be reached, or a local git failure
        run.emit("claim_sync_failed", item=item, action="advance", error=_error(exc))
        return True
    run.emit("claim_advanced", item=item, sha=moved.sha, phase=moved.phase, status=moved.status)
    return True


def _held(cfg: Config, repo: Path, run: Run, item: str, sha: str) -> claims.Claim | str:
    """The claim the run holds, to move: its own commit `sha`, read here; when that can't be read here (the object
    is gone), the claim on `origin` if it is this run's (`sha` itself, or a newer commit of the run built on it).
    Else why the claim is lost, as text. `Unreachable` when `origin` can't be read."""
    try:
        return claims.held(cfg, repo, item, sha, run.id)
    except claims.ClaimError:
        pass
    current = claims.read(cfg, repo, item)  # fetching it also brings `sha` back when it is an ancestor
    if current is None:
        return str(claims.Lost(None, item))
    if current.unreadable or current.run != run.id or not (current.sha == sha or claims.own(repo, sha, run.id,
                                                                                           current)):
        return str(claims.Lost(current, item))
    return current


def _claim_again(cfg: Config, run: Run, state: dict, item: str, phase: str, key: str) -> bool:
    """Claim `item` again after a person approved the `claim_lost` stop `key`: the ref must not exist. A claim in the
    way stops the run again, naming it; so does an `origin` that can't be reached, since the run must not go on
    unclaimed. Each new stop needs its own approval."""
    status = CLAIM_STATUS.get(state["status"], "running")
    try:
        held = claims.create(cfg, Path(run.repo), item, run=run.id, lane=str(state["pipeline"]), phase=phase,
                             status=status)
    except claims.Claimed as exc:
        return _lose(cfg, run, item, phase, f"the claim on {item} was taken: {claims.describe(exc.claim)}")
    except claims.HostRefused as exc:
        return _lose(cfg, run, item, phase, f"origin refused the claim ref: {_error(exc)}")
    except (claims.ClaimError, ValueError, OSError) as exc:
        error = _error(exc)
        run.emit("claim_sync_failed", item=item, action="claim", error=error)
        return _lose(cfg, run, item, phase, f"could not claim {item} on origin again ({error}); approve once origin "
                                            "is reachable to try again")
    record(cfg, run, held)
    row = run.state["steps"].get(key)
    if row is not None and row.get("status") != "done":  # the approved stop's own row, as preflight settles its own
        run.emit("step_finished", key=key, status="done", summary=f"claimed {item} again")
    return True


def release_run(cfg: Config, run: Run, reason: str) -> None:
    """Release the run's claim (a leased delete on the sha it holds) and record `claim_released` with `reason`.
    Never raises: a failure is `claim_release_failed`, tried again by the next `sync`. A claim another clone holds
    now is not the run's to delete: it is recorded as `claim_lost`, and stays."""
    row = _row(run.refresh())
    if row is None or row.get("released"):
        return
    item = str(row["item"])
    held = claims.Claim(item=item, run=run.id, lane="", phase="", status="", by="", started="", updated="",
                        sha=str(row.get("sha") or ""))
    repo = Path(run.repo)
    try:
        claims.release(cfg, repo, held)
    except claims.Lost as exc:
        if claims.own(repo, held.sha, run.id, exc.claim):
            run.emit("claim_release_failed", item=item, error=_error(exc))
        else:
            run.emit("claim_lost", item=item, why=_error(exc))
        return
    except Exception as exc:  # `origin` can't be reached, or anything else: the run's own end goes on
        run.emit("claim_release_failed", item=item, error=_error(exc))
        return
    run.emit("claim_released", item=item, reason=reason)


def sync_quietly(cfg: Config | None, run: Run) -> None:
    """`sync` where nothing may fail: at the end of a runner's turn and after a run-control action. With no `cfg`
    the run's own config is loaded, and only when the run has a claim to sync. A failure is `claim_sync_failed`."""
    try:
        state = run.refresh()
        if _idle(state):
            return
        if cfg is None:
            from agentflow.config import load_config  # only a run with a claim loads its config here
            from agentflow.config_trust import run_config_path

            cfg = load_config(run_config_path(run))
        sync(cfg, run)
    except Exception as exc:
        row = _row(run.state) or {}
        try:
            run.emit("claim_sync_failed", item=str(row.get("item") or "?"), action="sync", error=_error(exc))
        except Exception:  # the log itself can't be written: nothing more to do here
            pass
