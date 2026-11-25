"""`agentflow queue` in a `backlog` pack with claims on (D9): it reads the claims on `origin` live, never starts a
claimed item or an idea, and starts nothing when `origin` can't be read."""

from __future__ import annotations

from test_backlog import ClaimBoard

from agentflow import module_queue
from agentflow.module_queue import QueueState, queue_tick

QUEUE_YML = """version: 1
items:
  - {id: a, title: Alpha, status: ready}
  - {id: b, title: Bee, status: ready}
  - {id: c, title: Sea, status: ready}
  - {id: d, title: Dee, status: ready, depends_on: [a]}
  - {id: i, title: An idea, status: backlog}
"""


def test_queue_skips_a_claimed_item_and_starts_ready_ones(flow_repo, tmp_path):
    cb = ClaimBoard(flow_repo, tmp_path, QUEUE_YML)
    cb.claim("a", status="waiting")
    started: list[str] = []
    said: list[str] = []

    def go(module: str) -> str:
        started.append(module)
        return f"run-{module}"

    assert queue_tick(cb.cfg, cb.root, 5, go, log=said.append) == ["b", "c"]
    assert started == ["b", "c"]  # `a` is claimed, `d` waits on it, `i` is an idea
    assert "queue: a blocked — claimed by Other (plan, waiting)" in said
    assert "queue: d blocked — waiting on a: claimed by Other (plan, waiting)" in said
    entries = {e["id"]: e for e in QueueState.load(cb.root).modules}
    assert entries["a"]["claim"]["by"] == "Other" and entries["a"]["run_id"] is None
    assert entries["i"]["run_id"] is None and "claim" not in entries["b"]
    found = module_queue.assess_modules(cb.cfg, cb.root)
    assert found.claims_error == "" and found.occupied == 0  # a claim elsewhere holds no slot here

    cb.take_origin_away()
    said.clear()
    # `b` and `c` have no run (the starter made none), so with origin readable they would start again
    assert queue_tick(cb.cfg, cb.root, 5, go, log=said.append) == []
    assert started == ["b", "c"]
    assert any(s.startswith("queue: could not read claims on origin: ") and "no item starts" in s for s in said)
    assert module_queue.assess_modules(cb.cfg, cb.root).claims_error.startswith("could not read claims on origin: ")


def test_a_capability_map_queue_reads_no_claims(flow_repo, monkeypatch):
    from agentflow import claims

    def no_git(*args, **kwargs):
        raise AssertionError("a capability map pack read claims")

    monkeypatch.setattr(claims, "_run", no_git)
    found = module_queue.assess_modules(flow_repo.configure({"backlog": {"claims": True}}), flow_repo.root)
    assert found.claims_error == ""
