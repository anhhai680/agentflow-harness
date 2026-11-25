"""A run's side of the claims (`claims`): the claim a start makes on its backlog item, before the run exists.

A start claims its item when `backlog_writes` would write to it (a `module` param in the item's lane, or in
`module_lane` when the item names none), in a `backlog` pack with `backlog.claims` on and an `origin` remote
(`claims.enabled`); any other start claims nothing (`target` is None). The claim names the run, so `start` reserves the
run's id first (`Run.next_id`) and the caller creates exactly that run once the claim is made. A refused claim is a
`ValueError` whose text is the refusal, with nothing created: no run, no worktree, no branch, no claim.

Text that came from `origin` (a claimant's `by`, git's messages) is validated by `claims` or cut here to
`TEXT_MAX` printable characters before it reaches an event or a refusal.
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from agentflow import backlog_writes, claims, gitops, pack, work_source
from agentflow.backlog_file import BacklogItem
from agentflow.config import Config, backlog_path
from agentflow.state import Run

if TYPE_CHECKING:
    from agentflow.pipeline import RunOptions

TEXT_MAX = 300
NO_CLAIMS = "set backlog.claims: false to start without claims"

__all__ = ["record", "release_start", "start", "target"]


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


def record(run: Run, held: claims.Claim, ref: str) -> None:
    """Record the claim `held` (on `ref`) as the run's: `item_claimed`."""
    run.emit("item_claimed", item=held.item, ref=ref, sha=held.sha, phase=held.phase, status=held.status)


def release_start(cfg: Config, repo: Path, run: Run | None, held: claims.Claim) -> str:
    """Release the claim of a start that failed after it was made (a leased delete), recording `claim_released`
    (`reason: failed`) or `claim_release_failed` on `run` when the run exists. Never raises: returns "" when the
    claim was released, else why not."""
    try:
        claims.release(cfg, Path(repo), held)
    except Exception as exc:  # never hide the failure of the start itself
        why = _cut(str(exc) or type(exc).__name__)
        if run is not None:
            run.emit("claim_release_failed", item=held.item, error=why)
        return why
    if run is not None:
        run.emit("claim_released", item=held.item, reason="failed")
    return ""
