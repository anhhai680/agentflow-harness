"""The dashboard Backlog: cards, dependencies (`Runs after` / `Parallel with`), and starting by hand."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from agentflow import backlog, finished, module_queue
from agentflow.flow import main
from agentflow.backlog import Invalid, Refused
from agentflow.state import Run, runs_root

from conftest import ENGINE, run_git

GH_BIN = ENGINE / "tests" / "bin"
# A is merged, B and C depend on A, D depends on B (the plan's example).
MAP = """# Capability map

| Module id | Responsibility | Depends on | Spec |
|---|---|---|---|
| `a` | base | — | SPEC-a |
| `b` | bee | a | SPEC-b |
| `c` | sea | a | SPEC-c |
| `d` | dee | b | SPEC-d |
"""
PLAN_DONE = "# Plan\n\n### Task 1: One\n\n- [x] done\n"
PLAN_OPEN = "# Plan\n\n### Task 1: One\n\n- [ ] to do\n"
SPEC_DONE = "# Spec\n\n**Status: approved 2026-10-01.**\n\n## Open Questions\n\nNone for this module.\n"
SPEC_OPEN_QUESTION = "# Spec\n\n**Status: approved 2026-10-01.**\n\n## Open Questions\n\n- Q1: how long is the cache kept?\n"
SPEC_NO_API_REVIEW = ("# Spec\n\n**Status: approved 2026-10-01.**\n\nGET /things returns the list.\n\n"
                      "## Open Questions\n\nNone for this module.\n")
SPEC_PENDING = ("# Spec\n\n**Status: approved 2026-10-01.** **Amended 2026-10-25, pending approval.**\n\n"
                "## Open Questions\n\nNone for this module.\n")
SPEC_NO_STATUS_LINE = "# Spec\n\n## Open Questions\n\nNone for this module.\n"


def write(root: Path, rel: str, text: str) -> None:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


class Merges:
    """Which branches count as merged: the test sets `merges[branch] = True | False | None`."""

    def __init__(self) -> None:
        self.state: dict[str, bool | None] = {}

    def __call__(self, cfg, repo, branch):
        value = self.state.get(branch, False)
        return value, "" if value is not None else "gh could not tell"


class Spawner:
    """A `spawn` that records each call and creates the run the real one would."""

    def __init__(self, bk) -> None:
        self.bk = bk
        self.calls: list[dict] = []
        self.fail: str | None = None

    def __call__(self, repo, lane, params, **kw):
        self.calls.append({"lane": lane, "params": dict(params), **kw})
        if self.fail:
            raise module_queue.StartFailed(self.fail)
        run = Run.create(self.bk.cfg, repo, lane, params)
        slug = params.get("module") or params.get("issue") or "x"
        run.emit("run_updated", branch=f"flow/{lane}/{slug}")
        return run.id


class Board:
    """`flow_repo` with the fake `gh`, a capability map and the `Merges` / `Spawner` doubles."""

    def __init__(self, fr, merges: Merges) -> None:
        self.fr = fr
        self.merges = merges
        self.root = fr.root
        self.spawn = Spawner(self)

    @property
    def cfg(self):
        return self.fr.cfg

    def refresh(self):
        return backlog.refresh(self.cfg, self.root)

    def cards(self) -> dict[str, dict]:
        return {c["key"]: c for c in backlog.snapshot(self.cfg, self.root)["cards"]}

    def start(self, key: str) -> str | None:
        job = backlog.begin_start(self.cfg, self.root, key)
        return backlog.run_start(self.cfg, self.root, job, spawn=self.spawn)

    def merge_module(self, module: str) -> None:
        write(self.root, f"docs/plans/2026-10-28-{module}.md", PLAN_DONE)
        self.merges.state[f"flow/feature/{module}"] = True


@pytest.fixture
def bk(flow_repo, monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", f"{GH_BIN}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_GH_LOG", str(tmp_path / "gh.log"))
    merges = Merges()
    monkeypatch.setattr(module_queue, "branch_is_merged", merges)
    write(flow_repo.root, "docs/specs/CAPABILITY-MAP.md", MAP)
    run_git(flow_repo.root, "add", "-A")
    run_git(flow_repo.root, "commit", "-q", "-m", "map")
    board = Board(flow_repo, merges)
    board.merge_module("a")
    return board


def keys(cards: dict[str, dict], status: str) -> list[str]:
    return sorted(k for k, c in cards.items() if c["status"] == status)


# --------------------------------------------------------------------------- labels


def test_independent_modules_get_the_parallel_mark_and_their_dependent_is_blocked(bk):
    bk.refresh()
    cards = bk.cards()
    assert "module:a" not in cards  # merged, so not in Backlog
    assert keys(cards, "parallel") == ["module:b", "module:c"]
    assert [p["key"] for p in cards["module:b"]["parallel_with"]] == ["module:c"]
    assert [p["key"] for p in cards["module:c"]["parallel_with"]] == ["module:b", "module:d"]  # D is not C's
    blocked = cards["module:d"]
    assert blocked["status"] == "blocked"
    assert [(b["key"], b["text"]) for b in blocked["blocked_by"]] == [("module:b", "b (not started)")]
    assert [p["key"] for p in blocked["parallel_with"]] == ["module:c"]  # independent of D, later on
    assert not blocked["parallel"]


def test_a_card_with_no_independent_peer_is_ready_not_parallel(bk):
    bk.merges.state["flow/feature/c"] = True
    bk.merge_module("c")
    bk.refresh()
    cards = bk.cards()
    assert cards["module:b"]["status"] == "ready" and cards["module:b"]["parallel_with"] == []


def test_a_running_peer_still_makes_the_other_card_parallel(bk):
    bk.refresh()
    bk.start("module:b")
    cards = bk.cards()
    assert cards["module:c"]["status"] == "parallel"
    assert cards["module:d"]["blocked_by"][0]["text"] == "b (running)"


def test_the_board_works_before_the_first_refresh_and_with_queue_json_missing(bk):
    assert not module_queue.QueueState.path(bk.root).exists()
    cards = bk.cards()
    assert {"module:a", "module:b", "module:c", "module:d"} >= set(cards)
    assert cards["module:d"]["status"] == "blocked"


# --------------------------------------------------------------------------- a repository with other names


def test_nothing_depends_on_this_repositorys_lane_branch_or_map_names(flow_repo, monkeypatch, tmp_cfg):
    """Other lane names, another map path and another base branch: the labels and the first phase follow the config."""
    from agentflow.config import load_config

    renames = {"feature": "build", "bug": "fix", "eval": "tune", "design": "shape"}

    def rename(node):
        if isinstance(node, dict):
            for key, value in list(node.items()):
                if key == "lanes" and isinstance(value, list):
                    node[key] = [renames.get(v, v) for v in value]
                else:
                    rename(value)
        elif isinstance(node, list):
            for item in node:
                rename(item)

    def patch(data: dict) -> None:
        rename(data)
        data["lanes"] = [renames.get(v, v) for v in data["lanes"]]
        data["conditions"]["build_done"] = {"all": [{"check": "lane", "is": "build"}, "plan_done"]}
        data["work_source"] = {"kind": "capability_map", "path": "map/modules.md"}
        data["project"]["fork_from"] = "next"
        data["project"]["base_branch"] = "trunk"
        data["project"]["branch"] = "wip/{lane}/{slug}"
        data["dashboard"]["backlog"] = {
            "sync_base": False, "module_lane": "build",
            "new_task": {"build": ["new_module"], "fix": ["issue", "after"]},
            "module_row": {"Module id": "{module}", "Depends on": "{deps}"},
        }

    cfg = load_config(tmp_cfg(patch, name="other.yaml"))
    write(flow_repo.root, "map/modules.md", "| Module id | Depends on |\n|---|---|\n| `x` | — |\n| `y` | — |\n| `z` | x |\n")
    merges = Merges()
    monkeypatch.setattr(module_queue, "branch_is_merged", merges)
    flow_repo.cfg = cfg
    snap = backlog.snapshot(cfg, flow_repo.root)
    by_key = {c["key"]: c for c in snap["cards"]}
    assert by_key["module:x"]["status"] == "parallel" and by_key["module:y"]["status"] == "parallel"
    assert by_key["module:z"]["status"] == "blocked"
    assert by_key["module:x"]["lane"] == "build"
    assert by_key["module:x"]["first_phase"] == next(iter(cfg.phases))
    assert snap["new_task"] == {"build": ["new_module"], "fix": ["issue", "after"]}
    item = backlog.add(cfg, flow_repo.root, "fix", {"issue": "9"})
    assert item.lane == "fix"
    with pytest.raises(Invalid):
        backlog.add(cfg, flow_repo.root, "bug", {"issue": "9"})  # not one of this repository's lanes


def test_a_repository_with_no_map_shows_only_added_cards(bk):
    (bk.root / "docs/specs/CAPABILITY-MAP.md").unlink()
    assert bk.cards() == {}
    item = backlog.add(bk.cfg, bk.root, "bug", {"issue": "12"})
    cards = bk.cards()
    assert list(cards) == [f"card:{item.id}"]
    assert cards[f"card:{item.id}"]["status"] in ("ready", "parallel")
    assert cards[f"card:{item.id}"]["removable"] is True


def test_no_module_lane_means_map_modules_are_not_offered(bk, flow_repo, tmp_cfg):
    from agentflow.config import load_config

    cfg = load_config(tmp_cfg(lambda d: d["dashboard"].pop("backlog"), name="nolane.yaml"))
    snap = backlog.snapshot(cfg, bk.root)
    assert snap["cards"] == [] and snap["new_task"] == {} and snap["lane_labels"] == {}


def test_lane_labels_come_from_the_config_for_lanes_that_exist(bk, tmp_cfg):
    from agentflow.config import load_config

    def patch(data):
        data["dashboard"]["backlog"]["lane_labels"] = {"bug": "Bug fix", "gone": "Not a lane any more"}

    cfg = load_config(tmp_cfg(patch, name="labels.yaml"))
    assert backlog.snapshot(cfg, bk.root)["lane_labels"] == {"bug": "Bug fix"}


# --------------------------------------------------------------------------- starting


@pytest.mark.parametrize(("files", "column"), [
    pytest.param({}, "define", id="no-spec-yet"),
    pytest.param({"docs/specs/SPEC-b.md": SPEC_DONE}, "plan", id="finished-spec"),
    pytest.param({"docs/specs/SPEC-b.md": SPEC_OPEN_QUESTION}, "define", id="open-question"),
    pytest.param({"docs/specs/SPEC-b.md": SPEC_NO_API_REVIEW}, "define", id="api-review-still-due"),
    pytest.param({"docs/specs/SPEC-b.md": SPEC_PENDING}, "define", id="amendment-pending-approval"),
    pytest.param({"docs/specs/SPEC-b.md": SPEC_NO_STATUS_LINE}, "plan", id="no-status-line"),
    pytest.param({"docs/specs/SPEC-b.md": SPEC_DONE, "docs/plans/2026-10-28-b.md": PLAN_OPEN}, "build",
                 id="finished-spec-and-a-plan-still-open"),
])
def test_a_map_module_moves_to_the_first_phase_with_work_left(bk, files, column):
    for rel, text in files.items():
        write(bk.root, rel, text)
    assert bk.cards()["module:b"]["first_phase"] == column


def test_a_card_stopping_at_define_keeps_define_even_with_a_finished_spec(bk):
    """Its run ends at `define`, so offering Plan would promise a phase it never reaches."""
    write(bk.root, "docs/specs/SPEC-e.md", SPEC_DONE)
    fields = {"new_module": {"module": "e", "responsibility": "eee", "deps": ["a"]}}
    item = backlog.add(bk.cfg, bk.root, "feature", fields, to_phase="define")
    assert bk.cards()[f"card:{item.id}"]["first_phase"] == "define"


def test_a_blocked_card_is_refused_naming_what_it_waits_for(bk):
    bk.refresh()
    with pytest.raises(Refused, match="b"):
        bk.start("module:d")
    assert bk.spawn.calls == []


def test_a_ready_card_starts_in_its_lane_and_the_run_replaces_the_card(bk):
    bk.refresh()
    run_id = bk.start("module:b")
    assert bk.spawn.calls == [{"lane": "feature", "params": {"module": "b"}, "to_phase": None, "new_module": None,
                               "config": None}]
    assert run_id and "module:b" not in bk.cards()  # now a run, on the board as a normal card


def test_a_failed_start_keeps_the_card_with_its_reason_and_start_tries_again(bk):
    bk.refresh()
    bk.spawn.fail = "error: gh is down"
    assert bk.start("module:b") is None
    card = bk.cards()["module:b"]
    assert card["failure"] == "error: gh is down" and card["status"] in ("ready", "parallel")
    bk.spawn.fail = None
    assert bk.start("module:b")
    assert len(bk.spawn.calls) == 2


def test_the_same_module_cannot_be_started_twice_while_a_run_is_live(bk):
    bk.refresh()
    bk.start("module:b")
    with pytest.raises(Refused, match="already"):
        bk.start("module:b")


def test_an_interrupted_start_is_never_repeated(bk):
    bk.refresh()
    backlog.begin_start(bk.cfg, bk.root, "module:b")  # recorded `item_starting`, then the dashboard died
    assert bk.cards()["module:b"]["status"] == "starting"
    settled = backlog.settle_interrupted(bk.root)
    assert len(settled) == 1
    card = bk.cards()["module:b"]
    assert "interrupted" in card["failure"] and card["status"] in ("ready", "parallel")
    assert bk.spawn.calls == []  # nothing was started on the way
    assert backlog.settle_interrupted(bk.root) == []


def test_a_card_that_unlocks_does_not_start_by_itself(bk):
    bk.refresh()
    assert bk.cards()["module:d"]["status"] == "blocked"
    bk.start("module:b")
    bk.merge_module("b")
    bk.merges.state["flow/feature/b"] = True
    bk.refresh()
    cards = bk.cards()
    assert cards["module:d"]["status"] in ("ready", "parallel") and cards["module:d"]["blocked_by"] == []
    assert len(bk.spawn.calls) == 1  # only B, started by hand


def _land_on_origin(bk, tmp_path: Path, rel: str, text: str) -> None:
    """Merge `text` at `rel` into origin's `fork_from` the way a merged pull request does: from another clone."""
    clone = tmp_path / "merger"
    run_git(tmp_path, "clone", "-q", str(bk.fr.origin), str(clone))
    for key, value in (("user.name", "Merger"), ("user.email", "merger@example.com"), ("commit.gpgsign", "false")):
        run_git(clone, "config", key, value)
    run_git(clone, "checkout", "-q", bk.cfg.project.fork_from)
    write(clone, rel, text)
    run_git(clone, "add", "-A")
    run_git(clone, "commit", "-q", "-m", "merge pull request")
    run_git(clone, "push", "-q", "origin", bk.cfg.project.fork_from)


def _merge_b_while_the_checkout_is_elsewhere(bk, tmp_path: Path) -> None:
    """B starts, the developer's checkout moves to their own branch (its copy of B's plan still open), and B's
    pull request merges with the plan fully ticked: only origin has the finished plan."""
    bk.refresh()
    bk.start("module:b")
    run_git(bk.root, "checkout", "-q", "-b", "my-work")
    write(bk.root, "docs/plans/2026-10-28-b.md", PLAN_OPEN)
    run_git(bk.root, "add", "-A")
    run_git(bk.root, "commit", "-q", "-m", "B's plan as my branch saw it")
    _land_on_origin(bk, tmp_path, "docs/plans/2026-10-28-b.md", PLAN_DONE)
    bk.merges.state["flow/feature/b"] = True


def test_a_merge_unblocks_dependents_while_the_checkout_is_on_another_branch(bk, tmp_path):
    _merge_b_while_the_checkout_is_elsewhere(bk, tmp_path)
    assert bk.cards()["module:d"]["status"] == "blocked"
    bk.refresh()
    cards = bk.cards()
    assert cards["module:d"]["status"] in ("ready", "parallel") and cards["module:d"]["blocked_by"] == []
    assert {m["id"]: m["status"] for m in module_queue.QueueState.load(bk.root).modules}["b"] == "done"
    assert run_git(bk.root, "symbolic-ref", "--short", "HEAD").strip() == "my-work"  # the checkout is left alone


def test_a_merge_unblocks_dependents_with_sync_base_off(bk, tmp_path, tmp_cfg):
    from agentflow.config import load_config

    def patch(data: dict) -> None:
        data["project"]["fork_from"] = "next"
        data["dashboard"]["backlog"]["sync_base"] = False

    bk.fr.cfg = load_config(tmp_cfg(patch, name="nosync.yaml"))
    _merge_b_while_the_checkout_is_elsewhere(bk, tmp_path)
    bk.refresh()
    assert bk.cards()["module:d"]["blocked_by"] == []
    assert run_git(bk.root, "rev-parse", "next").strip() != run_git(bk.root, "rev-parse", "origin/next").strip()


# --------------------------------------------------------------------------- Runs after


def _started_task(bk, issue: str = "7") -> tuple[str, str]:
    item = backlog.add(bk.cfg, bk.root, "bug", {"issue": issue})
    run_id = bk.start(f"card:{item.id}")
    return item.id, run_id


def test_runs_after_is_met_by_a_merged_pull_request_not_by_a_done_card(bk):
    first, run_id = _started_task(bk)
    run = Run.load(bk.root, run_id)
    run.emit("run_finished")  # the card is Done: its pull request is open
    follower = backlog.add(bk.cfg, bk.root, "bug", {"issue": "8"}, after=[f"card:{first}"])
    bk.refresh()
    card = bk.cards()[f"card:{follower.id}"]
    assert card["status"] == "blocked" and card["blocked_by"][0]["text"].endswith("(PR open)")
    with pytest.raises(Refused):
        bk.start(f"card:{follower.id}")
    bk.merges.state["flow/bug/7"] = True
    bk.refresh()
    assert bk.cards()[f"card:{follower.id}"]["blocked_by"] == []
    assert bk.start(f"card:{follower.id}")


@pytest.mark.parametrize("event,word", [("run_failed", "failed"), ("run_cancelled", "cancelled")])
def test_a_failed_or_cancelled_dependency_keeps_its_dependents_blocked(bk, event, word):
    first, run_id = _started_task(bk)
    fields = {"reason": "boom"} if event == "run_failed" else {}
    Run.load(bk.root, run_id).emit(event, **fields)
    follower = backlog.add(bk.cfg, bk.root, "bug", {"issue": "8"}, after=[f"card:{first}"])
    bk.refresh()
    card = bk.cards()[f"card:{follower.id}"]
    assert card["status"] == "blocked" and word in card["blocked_by"][0]["text"]


def test_runs_after_can_name_a_live_run_and_an_unmerged_module(bk):
    bk.refresh()
    run_id = bk.start("module:b")
    item = backlog.add(bk.cfg, bk.root, "bug", {"issue": "5"}, after=[f"run:{run_id}", "module:c"])
    texts = [b["text"] for b in bk.cards()[f"card:{item.id}"]["blocked_by"]]
    assert len(texts) == 2 and any("running" in t for t in texts) and any(t.startswith("c") for t in texts)


def test_after_must_name_an_existing_item(bk):
    for bad in ("card:nope", "module:zzz", "run:20990101-0000-nothing", "plain", "card:"):
        with pytest.raises(Invalid):
            backlog.add(bk.cfg, bk.root, "bug", {"issue": "5"}, after=[bad])


def test_removing_a_card_others_run_after_is_refused_until_they_are_gone(bk):
    first = backlog.add(bk.cfg, bk.root, "bug", {"issue": "1"})
    second = backlog.add(bk.cfg, bk.root, "bug", {"issue": "2"}, after=[f"card:{first.id}"])
    with pytest.raises(Refused, match=second.id):
        backlog.remove(bk.cfg, bk.root, first.id)
    backlog.remove(bk.cfg, bk.root, second.id)
    backlog.remove(bk.cfg, bk.root, first.id)
    assert not [k for k in bk.cards() if k.startswith("card:")]


def test_a_started_card_cannot_be_removed(bk):
    item_id, _ = _started_task(bk)
    with pytest.raises(Refused):
        backlog.remove(bk.cfg, bk.root, item_id)


# --------------------------------------------------------------------------- adding cards


def test_the_form_fields_decide_what_a_lane_accepts(bk):
    with pytest.raises(Invalid):
        backlog.add(bk.cfg, bk.root, "bug", {})  # one of issue / task is needed
    with pytest.raises(Invalid):
        backlog.add(bk.cfg, bk.root, "bug", {"topic": "x"})  # not a bug field
    with pytest.raises(Invalid):
        backlog.add(bk.cfg, bk.root, "bug", {"issue": "x; rm -rf"})
    with pytest.raises(Invalid):
        backlog.add(bk.cfg, bk.root, "bug", {"task": "a\0b"})
    with pytest.raises(Invalid):
        backlog.add(bk.cfg, bk.root, "bug", {"task": "x" * (backlog.TEXT_MAX + 1)})
    with pytest.raises(Invalid):
        backlog.add(bk.cfg, bk.root, "nonsense", {"task": "x"})
    with pytest.raises(Invalid):
        backlog.add(bk.cfg, bk.root, "bug", {"task": "x"}, to_phase="not-a-phase")
    ok = backlog.add(bk.cfg, bk.root, "design", {"topic": "Khác biệt"}, to_phase=next(iter(bk.cfg.phases)))
    assert ok.to_phase == next(iter(bk.cfg.phases)) and ok.params == {"topic": "Khác biệt"}


def test_a_log_pasted_into_a_task_travels_as_text(bk):
    item = backlog.add(bk.cfg, bk.root, "bug", {"task": "Traceback:\n  File x\nValueError: --evil"})
    bk.start(f"card:{item.id}")
    assert bk.spawn.calls[0]["params"] == {"task": "Traceback:\n  File x\nValueError: --evil"}


def test_the_same_issue_cannot_be_added_twice(bk):
    backlog.add(bk.cfg, bk.root, "bug", {"issue": "#12"})
    with pytest.raises(Refused, match="12"):
        backlog.add(bk.cfg, bk.root, "bug", {"issue": "12"})


def test_an_issue_with_a_live_run_cannot_be_added_again(bk):
    _started_task(bk, "30")
    with pytest.raises(Refused, match="30"):
        backlog.add(bk.cfg, bk.root, "bug", {"issue": "30"})


def test_a_new_module_needs_a_fresh_id_and_dependencies_already_in_the_map(bk):
    fields = {"new_module": {"module": "e", "responsibility": "eee", "deps": ["a"]}}
    bk.refresh()
    ok = backlog.add(bk.cfg, bk.root, "feature", fields)
    assert ok.kind == "module" and ok.params == {"module": "e"}
    with pytest.raises(Refused):  # an id already in Backlog
        backlog.add(bk.cfg, bk.root, "feature", fields)
    for bad in ({"module": "b", "responsibility": "x", "deps": []},  # already in the map
                {"module": "f", "responsibility": "x", "deps": ["ghost"]},  # not in the map
                {"module": "f", "responsibility": "x", "deps": ["e"]},  # another unmerged new module
                {"module": "Bad Id", "responsibility": "x", "deps": []},
                {"module": "g", "responsibility": "", "deps": []}):
        with pytest.raises((Invalid, Refused)):
            backlog.add(bk.cfg, bk.root, "feature", {"new_module": bad})
    cards = bk.cards()
    assert f"card:{ok.id}" in cards and cards[f"card:{ok.id}"]["label"].startswith("e")
    assert cards[f"card:{ok.id}"]["status"] in ("ready", "parallel")  # its dependency a is merged


def test_a_new_module_card_starts_with_its_row_and_the_stop_phase(bk):
    fields = {"new_module": {"module": "e", "responsibility": "eee", "deps": ["a"]}}
    bk.refresh()
    item = backlog.add(bk.cfg, bk.root, "feature", fields, to_phase=next(iter(bk.cfg.phases)))
    bk.start(f"card:{item.id}")
    call = bk.spawn.calls[0]
    assert call["params"] == {"module": "e"} and call["to_phase"] == next(iter(bk.cfg.phases))
    assert call["new_module"] == {"module": "e", "responsibility": "eee", "deps": ["a"]}


def test_cards_survive_a_restart_of_the_dashboard(bk):
    item = backlog.add(bk.cfg, bk.root, "bug", {"issue": "3"}, to_phase=None)
    assert f"card:{item.id}" in bk.cards()
    log = runs_root(bk.root) / "backlog.jsonl"
    assert log.is_file() and json.loads(log.read_text().splitlines()[0])["type"] == "item_added"
    reloaded = backlog.Backlog.load(bk.root)
    assert reloaded.items[item.id].params == {"issue": "3"}


# --------------------------------------------------------------------------- dependency_view (pure)


def test_dependency_view_follows_dependencies_transitively_for_parallel_with():
    from agentflow.sources import ModuleNode

    nodes = [ModuleNode("a", []), ModuleNode("b", ["a"]), ModuleNode("c", ["b"]), ModuleNode("d", [])]
    entries = [{"id": i, "status": "backlog", "run_id": None} for i in "abcd"]
    view = module_queue.dependency_view(nodes, entries, [])
    assert [p for p in view["module:a"]["parallel_with"]] == ["module:d"]  # c is a's descendant, not a peer
    assert view["module:c"]["parallel_with"] == ["module:d"]
    assert view["module:d"]["parallel_with"] == ["module:a", "module:b", "module:c"]
    assert [b["key"] for b in view["module:c"]["blocked_by"]] == ["module:b"]
    assert view["module:a"]["parallel"] is True and view["module:c"]["parallel"] is False


# --------------------------------------------------------------------------- the terminal board


def test_board_table_marks_parallel_and_blocked_cards(bk):
    from agentflow.views import board, board_table

    bk.refresh()
    text = board_table(board(bk.root, bk.cfg))
    line = next(ln for ln in text.splitlines() if ln.startswith("Backlog:"))
    assert "b [P]" in line and "c [P]" in line
    assert "d (runs after b (not started))" in line


def test_a_peer_whose_run_has_finished_does_not_make_a_card_parallel(bk):
    """P means something else can run beside this card now: a peer awaiting its merge is not running."""
    bk.refresh()
    bk.start("module:b")
    assert bk.cards()["module:c"]["status"] == "parallel"  # b is running
    from agentflow.state import list_runs

    list_runs(bk.root)[-1].emit("run_finished")  # b's run is Done: its PR is open
    card = bk.cards()["module:c"]
    assert card["status"] == "ready" and "module:b" in [p["key"] for p in card["parallel_with"]]


# --------------------------------------------------------------------------- a pack whose work source is its spec files


@pytest.fixture
def sbk(flow_repo, monkeypatch, tmp_path):
    """`bk`'s Board over a `specs` pack: `docs/specs/SPEC-a.md` committed, `a` merged, one refresh."""
    monkeypatch.setenv("PATH", f"{GH_BIN}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_GH_LOG", str(tmp_path / "gh.log"))
    merges = Merges()
    monkeypatch.setattr(module_queue, "branch_is_merged", merges)
    flow_repo.configure({"work_source": {"kind": "specs", "path": None}})
    write(flow_repo.root, "docs/specs/SPEC-a.md", "# A\n\n**Depends on:** none\n")
    run_git(flow_repo.root, "add", "-A")
    run_git(flow_repo.root, "commit", "-q", "-m", "spec a")
    board = Board(flow_repo, merges)
    board.merge_module("a")
    board.refresh()
    return board


def test_a_spec_with_an_unknown_dependency_is_blocked_naming_the_spec_files(sbk):
    write(sbk.root, "docs/specs/SPEC-b.md", "# B\n\n**Depends on:** ghost\n")
    cards = sbk.cards()
    assert "module:a" not in cards  # merged, so not in Backlog
    card = cards["module:b"]
    assert card["status"] == "blocked"
    assert [b["text"] for b in card["blocked_by"]] == ["ghost (not in the spec files)"]
    with pytest.raises(Refused, match="ghost"):
        sbk.start("module:b")
    with pytest.raises(Refused, match="module ghost is not in the spec files"):
        sbk.start("module:ghost")


def test_a_spec_module_card_starts_a_feature_run_for_that_module(sbk):
    write(sbk.root, "docs/specs/SPEC-c.md", "# C\n")
    sbk.refresh()
    assert "module:a" not in sbk.cards() and sbk.cards()["module:c"]["status"] in ("ready", "parallel")
    sbk.start("module:c")
    assert sbk.spawn.calls[-1]["params"] == {"module": "c"} and sbk.spawn.calls[-1]["lane"] == "feature"


def test_a_new_feature_card_carries_its_text_and_dependencies_as_the_task(sbk):
    item = backlog.add(sbk.cfg, sbk.root, "feature",
                       {"new_module": {"module": "e", "responsibility": "Export  reports", "deps": ["a"]}})
    sbk.start(f"card:{item.id}")
    call = sbk.spawn.calls[-1]
    assert call["params"] == {"module": "e", "task": "Export reports\n\nDepends on: a"} and call["new_module"] is None
    assert call["lane"] == "feature"


def test_a_new_feature_card_with_no_dependencies_says_none(sbk):
    item = backlog.add(sbk.cfg, sbk.root, "feature",
                       {"new_module": {"module": "e", "responsibility": "Export reports", "deps": []}})
    sbk.start(f"card:{item.id}")
    assert sbk.spawn.calls[-1]["params"]["task"] == "Export reports\n\nDepends on: none"


def test_a_new_feature_card_cannot_reuse_an_existing_spec_module_id(sbk):
    with pytest.raises(backlog.Invalid, match="already a module"):
        backlog.add(sbk.cfg, sbk.root, "feature",
                    {"new_module": {"module": "a", "responsibility": "Again", "deps": []}})


def test_a_new_feature_card_whose_spec_appeared_since_is_refused_at_start(sbk):
    item = backlog.add(sbk.cfg, sbk.root, "feature",
                       {"new_module": {"module": "e", "responsibility": "Export reports", "deps": []}})
    write(sbk.root, "docs/specs/SPEC-e.md", "# E\n\n**Depends on:** none\n")
    with pytest.raises(Refused, match="a spec for e now exists; start module:e instead"):
        backlog.begin_start(sbk.cfg, sbk.root, f"card:{item.id}")
    assert sbk.spawn.calls == []


def test_the_backlog_names_the_files_that_are_not_modules(sbk):
    assert backlog.snapshot(sbk.cfg, sbk.root)["skipped"] == []
    write(sbk.root, "docs/specs/SPEC-Foo Bar.md", "# Foo\n")
    assert backlog.snapshot(sbk.cfg, sbk.root)["skipped"] == ["SPEC-Foo Bar.md"]


def test_a_capability_map_backlog_names_no_skipped_files(bk):
    assert backlog.snapshot(bk.cfg, bk.root)["skipped"] == []


def test_the_spec_prompt_tells_the_agent_about_task_md_and_the_depends_on_header():
    text = (Path(backlog.__file__).parent / "prompts" / "spec.md").read_text(encoding="utf-8")
    assert "{run_dir}/task.md" in text and "**Depends on:**" in text


# --------------------------------------------------------------------------- finished modules and `backlog done|undone`


def test_a_module_marked_done_by_hand_leaves_the_cards_and_is_listed_as_done_with_no_refresh(bk):
    finished.mark(bk.root, "b", "cli")
    pile = backlog.snapshot(bk.cfg, bk.root)
    assert {"key": "module:b", "id": "b", "reason": "done by hand"} in pile["done"]
    assert "module:b" not in bk.cards() and pile["done_note"] == ""
    finished.unmark(bk.root, "b")
    assert "module:b" in bk.cards()
    assert all(d["id"] != "b" for d in backlog.snapshot(bk.cfg, bk.root)["done"])


def test_done_modules_come_in_work_source_order_with_the_reason_the_assessment_recorded(bk):
    bk.refresh()  # a is merged
    finished.mark(bk.root, "c", "cli")
    done = backlog.snapshot(bk.cfg, bk.root)["done"]
    assert [(d["id"], d["reason"]) for d in done] == [("a", finished.MERGED), ("c", finished.BY_HAND)]


def test_a_corrupt_marks_file_is_noted_and_a_module_marked_earlier_stays_done(bk):
    finished.mark(bk.root, "b", "cli")
    bk.refresh()
    (runs_root(bk.root) / finished.MARKS).write_text("{not json", encoding="utf-8")
    pile = backlog.snapshot(bk.cfg, bk.root)
    assert pile["done_note"] and "done_marks.json" in pile["done_note"]
    assert "b" in [d["id"] for d in pile["done"]] and "module:b" not in bk.cards()


def test_backlog_done_refuses_a_name_that_is_not_a_module(bk, capsys):
    bk.fr.install_pack()
    assert main(["backlog", "done", "ghost"]) == 1
    assert "not a module" in capsys.readouterr().out
    assert finished.read_marks(bk.root)[0] == {}


def test_backlog_done_refuses_a_module_with_a_live_run_until_it_is_cancelled(bk, capsys):
    bk.fr.install_pack()
    bk.refresh()
    bk.start("module:b")
    (run,) = module_queue.feature_runs(bk.cfg, bk.root).values()
    assert main(["backlog", "done", "b"]) == 1
    assert run.id in capsys.readouterr().out
    run.emit("run_cancelled", by="test", killed=[], skipped=[])
    assert main(["backlog", "done", "b"]) == 0


def test_backlog_done_records_who_marked_it_and_prints_it(bk, capsys):
    bk.fr.install_pack()
    assert main(["backlog", "done", "b", "--by", "pat"]) == 0
    assert "b marked done by hand" in capsys.readouterr().out
    assert finished.read_marks(bk.root)[0]["b"]["by"] == "pat"
    assert "b" in finished.recorded(bk.root)  # the assessment under the lock recorded it too


def test_backlog_done_defaults_the_actor_to_the_current_user(bk, monkeypatch):
    import getpass

    bk.fr.install_pack()
    monkeypatch.setattr(getpass, "getuser", lambda: "sam")
    assert main(["backlog", "done", "b"]) == 0
    assert finished.read_marks(bk.root)[0]["b"]["by"] == "sam"


def test_backlog_undone_removes_a_hand_mark_and_then_records_a_veto(bk, capsys):
    bk.fr.install_pack()
    assert main(["backlog", "done", "b"]) == 0
    capsys.readouterr()
    assert main(["backlog", "undone", "b"]) == 0
    assert "b hand mark removed" in capsys.readouterr().out
    assert finished.read_marks(bk.root)[0] == {} and "module:b" in bk.cards()
    assert main(["backlog", "undone", "b"]) == 0
    assert "b marked not done by hand: origin evidence is ignored" in capsys.readouterr().out
    assert finished.read_marks(bk.root)[0]["b"]["state"] == "not_done"


def test_a_veto_does_not_unmerge_a_merged_module(bk, capsys):
    bk.fr.install_pack()
    assert main(["backlog", "undone", "a"]) == 0
    assert "a" in [d["id"] for d in backlog.snapshot(bk.cfg, bk.root)["done"]]


def test_the_commands_keep_the_change_and_say_so_when_the_queue_is_running(bk, capsys):
    bk.fr.install_pack()
    with module_queue.queue_lock(bk.root):
        assert main(["backlog", "done", "b"]) == 0
    out = capsys.readouterr().out
    assert "the queue is running; the Backlog shows it on its next refresh" in out
    assert "b" in [d["id"] for d in backlog.snapshot(bk.cfg, bk.root)["done"]]


def test_the_commands_exit_1_with_the_repair_text_when_the_marks_file_is_unreadable(bk, capsys):
    bk.fr.install_pack()
    root = runs_root(bk.root)
    root.mkdir(parents=True, exist_ok=True)
    (root / finished.MARKS).write_text("[]", encoding="utf-8")
    for verb in ("done", "undone"):
        assert main(["backlog", verb, "b"]) == 1
        assert "fix it or delete it" in capsys.readouterr().out


def test_the_text_board_lists_the_done_modules(bk):
    from agentflow.views import board, board_table

    finished.mark(bk.root, "b", "cli")
    text = board_table(board(bk.root, bk.cfg))
    assert "done: b (done by hand)" in text
    assert "done:" not in board_table({**board(bk.root, bk.cfg), "backlog_done": [], "done_note": ""})
