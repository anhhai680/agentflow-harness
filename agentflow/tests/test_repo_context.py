"""`repo_context`: one repository's context, the pure rescan, the shared-runs-directory rule and the counts."""

from __future__ import annotations

import dataclasses
import json
import os
import pathlib
import shutil
from pathlib import Path

import pytest
import yaml

from agentflow import config, layers, pack, repo_context, state
from agentflow.events import LOCK_NAME, EventError
from agentflow.repo_context import RepoContext
from agentflow.repo_set import RepoRef, RepoSet, Skipped
from agentflow.state import Run
from agentflow.tests.conftest import SPARSE_PACK
from agentflow.tests.test_workspace_cli import make_repo, write_user_file


def ref_of(path: Path) -> RepoRef:
    path = path.resolve()
    live = (path / ".agentflow" / "config.yaml").is_file()
    return RepoRef(pack.repo_id(path), path.name, path, "live" if live else "not_set_up")


def found(*paths: Path) -> RepoSet:
    return RepoSet(layers.WorkspaceSettings((), (), ()), tuple(ref_of(p) for p in paths), ())


def write_pack(repo: Path, **extra) -> None:
    (repo / ".agentflow").mkdir(exist_ok=True)
    data = {**SPARSE_PACK, **extra}
    (repo / ".agentflow" / "config.yaml").write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def default_dir(repo: Path) -> Path:
    """The repository's own default state dir: `paths.state_dir` set to it is its default."""
    return pack.user_state_root() / pack.repo_id(repo.resolve())


def with_state_dir(repo: Path, value: str | Path) -> Path:
    write_pack(repo, paths={"state_dir": str(value)})
    return repo


def ctx_of(path: Path) -> RepoContext:
    return repo_context.load_context(ref_of(path))


ZERO = {"waiting": 0, "active": 0, "failed": 0}


# --------------------------------------------------------------------------- load_context


def test_load_context_loads_a_live_pack(tmp_path):
    repo = make_repo(tmp_path, "alpha")
    ctx = ctx_of(repo)
    assert ctx.state == "live" and ctx.error is None and ctx.config is None
    assert ctx.cfg is not None and ctx.cfg.path == repo.resolve() / ".agentflow" / "config.yaml"
    assert ctx.id == pack.repo_id(repo.resolve()) and ctx.name == "alpha" and ctx.repo == repo.resolve()
    assert ctx.runs == (default_dir(repo) / "runs").resolve()

    bare = ctx_of(make_repo(tmp_path, "bare", with_pack=False))
    assert bare.state == "not_set_up" and bare.cfg is None and bare.error is None


@pytest.mark.parametrize("breakage", ["phases", "bytes", "nosuchuser", "nul"])
def test_load_context_turns_a_broken_pack_into_an_error(tmp_path, breakage):
    repo = make_repo(tmp_path, "broken")
    path = repo / ".agentflow" / "config.yaml"
    if breakage == "phases":
        write_pack(repo, phases=3)
    elif breakage == "bytes":
        path.write_bytes(b"\xff")  # not UTF-8 text
    elif breakage == "nosuchuser":
        with_state_dir(repo, "~nosuchuser/x")
    else:
        with_state_dir(repo, "state\0dir")
    ref = RepoRef(pack.repo_id(repo.resolve()), "broken", repo.resolve(), "live")
    ctx = repo_context.load_context(ref)
    assert ctx.state == "error" and ctx.cfg is None
    assert ctx.error and "\n" not in ctx.error and len(ctx.error) <= repo_context.ERROR_MAX


def test_load_context_cuts_a_long_error_to_one_line(tmp_path, monkeypatch):
    repo = make_repo(tmp_path, "alpha")

    def boom(path, **kwargs):
        raise ValueError("first line\n\tsecond   line " + "x" * 600)

    monkeypatch.setattr(repo_context, "load_config", boom)
    ctx = ctx_of(repo)
    assert ctx.state == "error"
    assert ctx.error.startswith("first line second line x") and len(ctx.error) == repo_context.ERROR_MAX
    assert "\n" not in ctx.error and "\t" not in ctx.error


def test_a_symlinked_agentflow_directory_is_an_error_context(tmp_path, monkeypatch):
    other = make_repo(tmp_path, "other")
    repo = make_repo(tmp_path, "linked", with_pack=False)
    (repo / ".agentflow").symlink_to(other / ".agentflow", target_is_directory=True)
    loaded: list[Path] = []
    monkeypatch.setattr(repo_context, "load_config", lambda path, **kw: loaded.append(path))
    ctx = ctx_of(repo)
    assert ctx.state == "error" and ctx.cfg is None
    assert ".agentflow" in ctx.error and "symlink" in ctx.error
    assert loaded == []


# --------------------------------------------------------------------------- counts and in_flight


def make_runs(repo: Path) -> dict[str, Run]:
    cfg = config.load_config(pack.pack_config(repo))
    waiting = Run.create(cfg, repo, "feature", {"module": "waiting"})
    waiting.emit("phase_started", phase="plan")
    waiting.emit("phase_approval_raised", phase="plan", round=0, summary="Phase plan is done")
    active = Run.create(cfg, repo, "feature", {"module": "active"})
    (active.dir / LOCK_NAME).write_text(str(os.getpid()), encoding="utf-8")
    failed = Run.create(cfg, repo, "feature", {"module": "failed"})
    failed.emit("run_failed", reason="x")
    done = Run.create(cfg, repo, "feature", {"module": "done"})
    done.emit("run_finished")
    return {"waiting": waiting, "active": active, "failed": failed, "done": done}


def test_counts(tmp_path, monkeypatch):
    repo = make_repo(tmp_path, "alpha")
    runs = make_runs(repo)
    ctx = ctx_of(repo)
    assert repo_context.counts(ctx) == {"waiting": 1, "active": 1, "failed": 1}
    assert repo_context.in_flight(ctx) is True

    # a run whose log can't be loaded is skipped; the others still count, and another run's live lock still shows
    cfg = config.load_config(pack.pack_config(repo))
    corrupt = Run.create(cfg, repo, "feature", {"module": "corrupt"})  # loaded, it would count as stalled
    assert repo_context.counts(ctx) == {"waiting": 1, "active": 1, "failed": 2}
    with corrupt.events.path.open("ab") as handle:
        handle.write(b'{"seq": 2, "type": "phase_started"\n')
    real_load = Run.load.__func__

    def load(cls, repo_path, run_id):
        if run_id == corrupt.id:
            raise EventError("phase_started", "corrupt")
        return real_load(cls, repo_path, run_id)

    monkeypatch.setattr(Run, "load", classmethod(load))
    assert repo_context.counts(ctx) == {"waiting": 1, "active": 1, "failed": 1}
    assert repo_context.in_flight(ctx) is True

    # a parked stop is something a person can act on too
    runs["active"].emit("step_parked", key="build/build/task-2/implement", phase="build", kind="blocked", summary="x")
    assert repo_context.counts(ctx)["waiting"] == 2

    # a run whose inbox can't be read costs only its own items
    real_items = repo_context.hitl.inbox_items

    def items(repo_path, run_list=None):
        if run_list and run_list[0].id == runs["waiting"].id:
            raise KeyError("waiting")
        return real_items(repo_path, run_list)

    monkeypatch.setattr(repo_context.hitl, "inbox_items", items)
    assert repo_context.counts(ctx)["waiting"] == 1


def test_counts_are_zero_when_the_context_is_not_live_or_the_repository_is_gone(tmp_path):
    bare = ctx_of(make_repo(tmp_path, "bare", with_pack=False))
    assert repo_context.counts(bare) == ZERO
    repo = make_repo(tmp_path, "gone")
    ctx = ctx_of(repo)
    shutil.rmtree(repo)
    assert repo_context.counts(ctx) == ZERO
    assert repo_context.in_flight(ctx) is False
    assert repo_context.counts(dataclasses.replace(ctx, state="error", cfg=None)) == ZERO


def test_counts_are_zero_once_the_packs_state_dir_changes(tmp_path):
    billing = make_repo(tmp_path, "billing")
    make_runs(billing)
    repo = make_repo(tmp_path, "alpha")
    ctx = ctx_of(repo)
    assert repo_context.counts(ctx_of(billing)) == {"waiting": 1, "active": 1, "failed": 1}
    with_state_dir(repo, default_dir(billing))  # a pull aims the pack at another repository's runs
    assert not repo_context.usable(ctx)
    assert repo_context.counts(ctx) == ZERO


def test_counts_are_zero_on_any_exception(tmp_path, monkeypatch):
    repo = make_repo(tmp_path, "alpha")
    make_runs(repo)
    ctx = ctx_of(repo)
    monkeypatch.setattr(repo_context.state, "runs_root", lambda repo_path: 1 / 0)
    assert repo_context.counts(ctx) == ZERO


def test_in_flight_is_false_without_a_live_lock(tmp_path):
    repo = make_repo(tmp_path, "alpha")
    runs = make_runs(repo)
    (runs["active"].dir / LOCK_NAME).unlink()
    assert repo_context.in_flight(ctx_of(repo)) is False


def test_in_flight_keeps_a_context_it_cannot_read(tmp_path, monkeypatch):
    repo = make_repo(tmp_path, "alpha")
    make_runs(repo)
    ctx = ctx_of(repo)
    root = state.runs_root(repo)
    real_iterdir = pathlib.Path.iterdir

    def iterdir(self):
        if self == root:
            raise PermissionError(13, "Permission denied", str(self))
        return real_iterdir(self)

    monkeypatch.setattr(pathlib.Path, "iterdir", iterdir)
    assert repo_context.in_flight(ctx) is True
    assert repo_context.counts(ctx) == ZERO
    gone = repo_context.reconcile({ctx.id: ctx}, found())
    assert gone[ctx.id].state == "missing"


# --------------------------------------------------------------------------- reconcile


def test_reconcile_keeps_a_live_context_by_identity_and_follows_workspace_order(tmp_path):
    a, b = make_repo(tmp_path, "a"), make_repo(tmp_path, "b")
    ctx_a = ctx_of(a)
    result = repo_context.reconcile({ctx_a.id: ctx_a}, found(b, a))
    assert list(result) == [ref_of(b).id, ctx_a.id]
    assert result[ctx_a.id] is ctx_a
    assert result[ref_of(b).id].state == "live"


def test_reconcile_retries_an_error_context_and_promotes_a_pack_that_appears(tmp_path):
    broken, bare = make_repo(tmp_path, "broken"), make_repo(tmp_path, "bare", with_pack=False)
    write_pack(broken, phases=3)
    before = repo_context.reconcile({}, found(broken, bare))
    assert [c.state for c in before.values()] == ["error", "not_set_up"]
    write_pack(broken)
    write_pack(bare)
    after = repo_context.reconcile(before, found(broken, bare))
    assert [c.state for c in after.values()] == ["live", "live"]
    assert all(c.cfg is not None and c.error is None for c in after.values())


def test_reconcile_keeps_a_vanished_repository_only_while_it_is_busy(tmp_path):
    a, gone = make_repo(tmp_path, "a"), make_repo(tmp_path, "gone")
    current = repo_context.reconcile({}, found(gone, a))
    kept = repo_context.reconcile(current, found(a), busy=lambda ctx: True)
    assert list(kept) == [ref_of(a).id, ref_of(gone).id]
    left = kept[ref_of(gone).id]
    assert left.state == "missing" and left.cfg is current[ref_of(gone).id].cfg
    assert current[ref_of(gone).id].state == "live"  # a copy: the context a request may hold is not changed
    dropped = repo_context.reconcile(kept, found(a), busy=lambda ctx: False)
    assert list(dropped) == [ref_of(a).id]


def shipped_live_run(runs: Path, run_id: str = "20260101-0000-shipped") -> Path:
    """A run a clone could ship: an event log and a lock naming PID 1, which always exists, so it reads as live."""
    run_dir = runs / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "events.jsonl").write_text("", encoding="utf-8")
    (run_dir / LOCK_NAME).write_text("1", encoding="utf-8")
    return run_dir


def test_reconcile_drops_a_refused_or_unset_context_that_left_whatever_its_locks_say(tmp_path):
    clone = with_state_dir(make_repo(tmp_path, "clone"), ".state")
    current = repo_context.reconcile({}, found(clone))
    refused = current[ref_of(clone).id]
    assert refused.state == "error" and "inside the repository" in refused.error
    shipped_live_run(refused.runs)
    assert repo_context.in_flight(refused) is True  # the shipped lock reads as live ...
    assert repo_context.reconcile(current, found()) == {}  # ... and still keeps nothing
    bare = make_repo(tmp_path, "bare", with_pack=False)
    unset = repo_context.reconcile({}, found(bare))
    assert repo_context.reconcile(unset, found(), busy=lambda ctx: True) == {}


def test_in_flight_reads_the_runs_directory_the_context_was_built_with(tmp_path):
    repo = make_repo(tmp_path, "alpha")
    ctx = ctx_of(repo)
    with_state_dir(repo, tmp_path / "moved")
    shipped_live_run(tmp_path / "moved" / "runs")
    assert repo_context.in_flight(ctx) is False  # not the directory the pack names now
    shipped_live_run(ctx.runs)
    assert repo_context.in_flight(ctx) is True

    # a repository whose checkout is gone is still seen in its custom directory
    custom = tmp_path / "custom-state"
    gone = with_state_dir(make_repo(tmp_path, "gone"), custom)
    left = ctx_of(gone)
    shipped_live_run(custom / "runs")
    shutil.rmtree(gone)
    assert repo_context.in_flight(left) is True
    kept = repo_context.reconcile({left.id: left}, found())
    assert kept[left.id].state == "missing" and kept[left.id].runs == left.runs


def test_reconcile_never_reads_a_repository_that_left(tmp_path, monkeypatch):
    a, gone = make_repo(tmp_path, "a"), make_repo(tmp_path, "gone")
    current = repo_context.reconcile({}, found(a, gone))
    asked: list[RepoContext] = []
    loaded: list[RepoRef] = []
    real_load = repo_context.load_context

    def spy(ref):
        loaded.append(ref)
        return real_load(ref)

    monkeypatch.setattr(repo_context, "load_context", spy)

    def busy(ctx):
        asked.append(ctx)
        return True

    repo_context.reconcile(current, found(a), busy=busy)
    assert asked == [current[ref_of(gone).id]]
    assert all(ref.id != ref_of(gone).id for ref in loaded)


# --------------------------------------------------------------------------- the runs directory rule


def test_settle_dirs_gives_a_shared_runs_directory_to_the_repository_it_belongs_to(tmp_path):
    billing = make_repo(tmp_path, "billing")
    evil = with_state_dir(make_repo(tmp_path, "aaa-evil"), default_dir(billing))
    for order in ((evil, billing), (billing, evil)):
        settled = {c.name: c for c in repo_context.settle_dirs([ctx_of(p) for p in order])}
        assert settled["billing"].state == "live"
        assert settled["aaa-evil"].state == "error" and "billing" in settled["aaa-evil"].error
    through_reconcile = repo_context.reconcile({}, found(evil, billing))
    assert [c.state for c in through_reconcile.values()] == ["error", "live"]

    shared = tmp_path / "shared-state"
    one = with_state_dir(make_repo(tmp_path, "one"), shared)
    two = with_state_dir(make_repo(tmp_path, "two"), shared)
    settled = repo_context.settle_dirs([ctx_of(one), ctx_of(two)])
    assert [c.state for c in settled] == ["error", "error"]
    assert "two" in settled[0].error and "one" in settled[1].error

    lost = [with_state_dir(make_repo(tmp_path, name), "~nosuchuser/x") for name in ("lost1", "lost2")]
    unresolved = [RepoContext(ref_of(p).id, p.name, p.resolve(), state="live") for p in lost]
    assert [repo_context.runs_dir(c) for c in unresolved] == [None, None]
    settled = repo_context.settle_dirs(unresolved)
    assert [c.state for c in settled] == ["error", "error"]
    assert all("resolve" in c.error and "lost" not in c.error for c in settled)  # not treated as sharing


def test_settle_dirs_returns_the_same_object_when_nothing_changes(tmp_path):
    ctx = ctx_of(make_repo(tmp_path, "alpha"))
    assert repo_context.settle_dirs([ctx])[0] is ctx


def test_settle_dirs_refuses_a_directory_under_the_user_state_root_that_is_another_repositorys(tmp_path):
    aimed = with_state_dir(make_repo(tmp_path, "aimed"), pack.user_state_root() / "other-1a2b3c4d")
    ctx = repo_context.settle_dirs([ctx_of(aimed)])[0]
    assert ctx.state == "error" and ctx.error

    own = make_repo(tmp_path, "own")
    with_state_dir(own, default_dir(own))
    elsewhere = with_state_dir(make_repo(tmp_path, "elsewhere"), tmp_path / "elsewhere-state")
    assert [c.state for c in repo_context.settle_dirs([ctx_of(own), ctx_of(elsewhere)])] == ["live", "live"]

    # a repository that just left still holds its directory: a new one that aims at it is refused
    leaver = make_repo(tmp_path, "leaver")
    newcomer = with_state_dir(make_repo(tmp_path, "newcomer"), default_dir(leaver))
    current = repo_context.reconcile({}, found(leaver))
    result = repo_context.reconcile(current, found(newcomer), busy=lambda ctx: True)
    assert result[ref_of(leaver).id].state == "missing"
    assert result[ref_of(newcomer).id].state == "error" and "leaver" in result[ref_of(newcomer).id].error

    custom = tmp_path / "custom-state"
    gone = with_state_dir(make_repo(tmp_path, "gone"), custom)
    joiner = with_state_dir(make_repo(tmp_path, "joiner"), custom)
    current = repo_context.reconcile({}, found(gone))
    assert current[ref_of(gone).id].state == "live"
    result = repo_context.reconcile(current, found(joiner), busy=lambda ctx: True)
    assert result[ref_of(gone).id].state == "missing"
    assert result[ref_of(joiner).id].state == "error" and "gone" in result[ref_of(joiner).id].error


def test_a_runs_directory_inside_its_own_checkout_is_an_error_for_a_workspace_context(tmp_path):
    relative = with_state_dir(make_repo(tmp_path, "relative"), ".state")
    absolute = with_state_dir(make_repo(tmp_path, "absolute"), tmp_path / "absolute" / "deep" / "state")
    for repo in (relative, absolute):
        loaded = ctx_of(repo)
        assert loaded.state == "live"
        (ctx,) = repo_context.settle_dirs([loaded])
        assert ctx.state == "error" and "its state directory is inside the repository" in ctx.error
    cfg = config.load_config(pack.pack_config(relative))
    single = repo_context.single_context(relative, cfg)
    assert single.state == "live" and single.runs is None and repo_context.usable(single)


def found_in(root: Path, *paths: Path, exclude: tuple[str, ...] = ()) -> RepoSet:
    """`paths` as a workspace whose settings name the root `root`."""
    return RepoSet(layers.WorkspaceSettings((str(root),), (), exclude), tuple(ref_of(p) for p in paths), ())


def test_a_runs_directory_inside_another_workspace_checkout_is_an_error(tmp_path):
    root = tmp_path / "ws"
    beta = make_repo(root, "beta")
    alpha = make_repo(root, "alpha")
    for value in ("../beta/shipped-state", beta / "shipped-state"):
        with_state_dir(alpha, value)
        for order in ((alpha, beta), (beta, alpha)):
            settled = {c.name: c for c in repo_context.settle_dirs([ctx_of(p) for p in order])}
            assert settled["beta"].state == "live"
            assert settled["alpha"].state == "error"
            assert "its state directory is inside beta's checkout" in settled["alpha"].error
        result = repo_context.reconcile({}, found(alpha, beta))
        assert [c.state for c in result.values()] == ["error", "live"]
    # the other checkout counts in any state: one with no pack, and one that left while busy
    bare = make_repo(root, "bare", with_pack=False)
    with_state_dir(alpha, bare / "shipped-state")
    settled = repo_context.settle_dirs([ctx_of(alpha), ctx_of(bare)])
    assert [c.state for c in settled] == ["error", "not_set_up"] and "bare's checkout" in settled[0].error
    leaver = make_repo(tmp_path, "leaver")
    with_state_dir(alpha, leaver / "shipped-state")
    current = repo_context.reconcile({}, found(leaver))
    result = repo_context.reconcile(current, found(alpha), busy=lambda ctx: True)
    assert result[ref_of(leaver).id].state == "missing"
    assert result[ref_of(alpha).id].state == "error" and "leaver's checkout" in result[ref_of(alpha).id].error


def test_a_runs_directory_under_a_workspace_root_is_an_error(tmp_path):
    root = tmp_path / "ws"
    alpha = make_repo(root, "alpha")
    beta = make_repo(root, "beta")
    plain = root / "plain"
    plain.mkdir()
    for target in (beta / "shipped-state", plain / "shipped-state", root / "shipped-state"):
        with_state_dir(alpha, target)
        # beta is not a context: excluded, or never a repository at all
        (ctx,) = repo_context.settle_dirs([ctx_of(alpha)], roots=(root.resolve(),))
        assert ctx.state == "error"
        assert f"its state directory is inside the workspace root {root.resolve()}" in ctx.error
        result = repo_context.reconcile({}, found_in(root, alpha, exclude=("beta",)))
        assert result[ref_of(alpha).id].state == "error" and "workspace root" in result[ref_of(alpha).id].error
    # a root that can't be resolved is skipped, not a refusal of every repository
    with_state_dir(alpha, tmp_path / "elsewhere-state")
    for settings_root in (tmp_path / "nowhere", "~nosuchuser/ws"):
        result = repo_context.reconcile({}, found_in(settings_root, alpha))
        assert result[ref_of(alpha).id].state == "live"


def test_a_runs_directory_outside_every_root_and_checkout_stays_live(tmp_path):
    root = tmp_path / "ws"
    alpha = with_state_dir(make_repo(root, "alpha"), tmp_path / "elsewhere-state")
    beta = make_repo(root, "beta")  # its own default under the user state root
    result = repo_context.reconcile({}, found_in(root, alpha, beta))
    assert [c.state for c in result.values()] == ["live", "live"]
    # the user state root may itself lie under a root (`roots: [~]` with the default `~/.local/state`): a
    # repository's own default there stays live
    assert pack.user_state_root().resolve().is_relative_to(tmp_path.resolve())
    result = repo_context.reconcile({}, found_in(tmp_path, beta))
    assert [c.state for c in result.values()] == ["live"]
    with_state_dir(beta, default_dir(beta))
    assert repo_context.settle_dirs([ctx_of(beta)], roots=(tmp_path.resolve(),))[0].state == "live"


def test_usable_is_false_once_the_packs_state_dir_changes(tmp_path):
    repo = make_repo(tmp_path, "alpha")
    ctx = ctx_of(repo)
    single = repo_context.single_context(repo, ctx.cfg, Path("/elsewhere/config.yaml"))
    assert single.config == Path("/elsewhere/config.yaml") and single.id == ctx.id and single.name == "alpha"
    assert repo_context.usable(ctx) and repo_context.usable(single)
    with_state_dir(repo, tmp_path / "moved")
    assert not repo_context.usable(ctx)
    assert repo_context.usable(single)  # a single repository follows its pack, as today
    rebuilt = repo_context.reconcile({ctx.id: ctx}, found(repo))[ctx.id]
    assert rebuilt is not ctx and repo_context.usable(rebuilt)
    assert rebuilt.runs == (tmp_path / "moved" / "runs").resolve()
    for other in ("not_set_up", "missing", "error"):
        assert not repo_context.usable(dataclasses.replace(rebuilt, state=other))


# --------------------------------------------------------------------------- the user settings and the view


def test_engine_user_settings_is_the_defaults_without_a_user_file(tmp_path):
    path = layers.user_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"workspace": {"roots": [str(tmp_path)]}}), encoding="utf-8")
    assert layers.load_user_settings().workspace.roots == (str(tmp_path),)
    settings = repo_context.engine_user_settings()
    assert settings.workspace == layers.WorkspaceSettings((), (), ())
    assert settings.dashboard["port"] == 7788 and settings.dashboard["host"] == "127.0.0.1"
    assert settings.path is None


def test_repos_view(tmp_path):
    live = make_repo(tmp_path, "live")
    bare = make_repo(tmp_path, "bare", with_pack=False)
    broken = make_repo(tmp_path, "broken")
    write_pack(broken, phases=3)
    left = make_repo(tmp_path, "left")
    current = repo_context.reconcile({}, found(live, bare, broken, left))
    contexts = repo_context.reconcile(current, found(live, bare, broken), busy=lambda ctx: True)
    skipped = [Skipped("~/code", "is not a folder")]
    view = repo_context.repos_view(contexts.values(), skipped, ref_of(live).id, "bad user file")
    assert view["skipped"] == [{"entry": "~/code", "reason": "is not a folder"}]
    assert view["default"] == ref_of(live).id and view["user_config_error"] == "bad user file"
    rows = view["repos"]
    assert [r["state"] for r in rows] == ["live", "not_set_up", "error", "missing"]
    assert [r["name"] for r in rows] == ["live", "bare", "broken", "left"]
    for row in rows:
        assert set(row) == {"id", "name", "state", "pack_version", "waiting", "active", "failed", "error"}
        assert (row["waiting"], row["active"], row["failed"]) == (0, 0, 0)
    assert [r["pack_version"] for r in rows] == [2, None, None, None]
    assert rows[2]["error"] and [r["error"] for r in rows[:2] + rows[3:]] == [None, None, None]
    text = json.dumps(view)
    assert str(live.resolve()) not in text and str(bare.resolve()) not in text and str(left.resolve()) not in text
    empty = repo_context.repos_view([], [], None, None)
    assert empty == {"repos": [], "skipped": [], "default": None, "user_config_error": None}


# --------------------------------------------------------------------------- plan_start


def user_text(text: str | bytes) -> Path:
    target = layers.user_config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(text, bytes):
        target.write_bytes(text)
    else:
        target.write_text(text, encoding="utf-8")
    return target


def workspace_of(tmp_path: Path) -> dict[str, Path]:
    """`ws/alpha` and `ws/beta` (with a pack) and `ws/gamma` (none), each resolved; a user file naming `ws`."""
    root = tmp_path / "ws"
    made = {name: make_repo(root, name, with_pack=name != "gamma").resolve() for name in ("alpha", "beta", "gamma")}
    write_user_file([root])
    return made


def test_plan_start_with_no_workspace_in_a_repository_is_the_single_repository_mode(tmp_path):
    repo = make_repo(tmp_path, "alpha").resolve()
    plan = repo_context.plan_start(repo, None)
    (ctx,) = plan.contexts
    assert plan.workspace is False and plan.user_error is None and plan.left_out is None and plan.skipped == ()
    assert ctx.state == "live" and ctx.runs is None and ctx.config is None and ctx.repo == repo
    assert ctx.cfg.path == repo / ".agentflow" / "config.yaml"
    assert plan.prefer == ctx.id == pack.repo_id(repo)
    bare = make_repo(tmp_path, "bare", with_pack=False).resolve()
    with pytest.raises(pack.PackError, match="no .agentflow/config.yaml"):
        repo_context.plan_start(bare, None)


def test_plan_start_in_a_workspace_repository_serves_the_workspace_and_prefers_it(tmp_path):
    made = workspace_of(tmp_path)
    plan = repo_context.plan_start(made["beta"], None)
    assert plan.workspace is True and plan.user_error is None and plan.left_out is None and plan.skipped == ()
    assert [(c.name, c.state) for c in plan.contexts] == [("alpha", "live"), ("beta", "live"),
                                                          ("gamma", "not_set_up")]
    assert all(c.runs is not None for c in plan.contexts if c.state == "live")
    assert plan.prefer == pack.repo_id(made["beta"])
    assert plan.user.workspace.roots == (str(tmp_path / "ws"),) and plan.user.path == layers.user_config_path()
    (made["beta"] / ".agentflow" / "config.yaml").write_text("phases: [unclosed\n", encoding="utf-8")
    for start, state_ in ((made["beta"], "error"), (made["gamma"], "not_set_up")):
        plan = repo_context.plan_start(start, None)
        ctx = next(c for c in plan.contexts if c.repo == start)
        assert ctx.state == state_ and plan.prefer == ctx.id == pack.repo_id(start) and plan.left_out is None


def test_plan_start_applies_the_shared_runs_directory_rule_from_the_first_request(tmp_path):
    root = tmp_path / "ws"
    billing = make_repo(root, "billing")
    with_state_dir(make_repo(root, "aaa-evil"), default_dir(billing))
    write_user_file([root])
    plan = repo_context.plan_start(None, None)
    by = {c.name: c for c in plan.contexts}
    assert list(by) == ["aaa-evil", "billing"]
    assert by["aaa-evil"].state == "error" and "billing" in by["aaa-evil"].error
    assert by["billing"].state == "live"


def test_plan_start_refuses_a_state_directory_in_another_checkout_or_under_a_root(tmp_path):
    root = tmp_path / "ws"
    make_repo(root, "beta")
    with_state_dir(make_repo(root, "alpha"), "../beta/shipped-state")
    excluded = make_repo(root, "excluded")
    with_state_dir(make_repo(root, "gamma"), excluded / "shipped-state")
    user_text(yaml.safe_dump({"workspace": {"roots": [str(root)], "exclude": ["excluded"]}}))
    plan = repo_context.plan_start(None, None)
    by = {c.name: c for c in plan.contexts}
    assert list(by) == ["alpha", "beta", "gamma"]
    assert by["alpha"].state == "error" and "beta's checkout" in by["alpha"].error
    assert by["beta"].state == "live"
    assert by["gamma"].state == "error" and "workspace root" in by["gamma"].error


def test_plan_start_outside_the_workspace_serves_the_workspace_alone(tmp_path):
    made = workspace_of(tmp_path)
    other = make_repo(tmp_path, "other").resolve()
    plan = repo_context.plan_start(other, None)
    assert plan.workspace is True and plan.prefer is None and plan.left_out == "other"
    assert [c.repo for c in plan.contexts] == [made["alpha"], made["beta"], made["gamma"]]
    plan = repo_context.plan_start(None, None)
    assert plan.workspace is True and plan.prefer is None and plan.left_out is None
    assert [c.name for c in plan.contexts] == ["alpha", "beta", "gamma"]


def test_plan_start_with_no_workspace_and_no_repository_is_the_empty_start():
    plan = repo_context.plan_start(None, None)
    assert plan.contexts == [] and plan.workspace is True and plan.prefer is None
    assert plan.user_error is None and plan.left_out is None and plan.skipped == ()


def test_plan_start_carries_a_workspace_that_resolved_to_nothing(tmp_path):
    write_user_file([tmp_path / "mistyped"])
    plan = repo_context.plan_start(None, None)
    assert plan.contexts == [] and plan.workspace is True and plan.user_error is None
    assert plan.skipped == (Skipped(str(tmp_path / "mistyped"), "is not a folder"),)


def test_plan_start_with_config_serves_that_one_repository_and_reads_no_workspace(tmp_path):
    made = workspace_of(tmp_path)
    given = made["beta"] / ".agentflow" / "config.yaml"
    plan = repo_context.plan_start(made["alpha"], given)
    (ctx,) = plan.contexts
    assert plan.workspace is False and plan.user_error is None and plan.prefer == ctx.id
    assert ctx.config == given and ctx.cfg.path == given and ctx.id == pack.repo_id(made["alpha"])
    assert ctx.repo == made["alpha"] and ctx.runs is None
    user_text(yaml.safe_dump({"workspace": {"roots": ["relative/root"]}}))  # unusable as a workspace
    with pytest.raises(layers.LayerError):
        layers.load_user_settings()
    (ctx,) = repo_context.plan_start(made["alpha"], given).contexts
    assert ctx.state == "live" and ctx.config == given
    with pytest.raises(config.ConfigError, match="not inside a git checkout"):
        repo_context.plan_start(None, given)


@pytest.mark.parametrize("text", [b"\xff", "workspace: [unclosed\n"])
def test_plan_start_with_an_unusable_user_file(tmp_path, text):
    target = user_text(text)
    plan = repo_context.plan_start(None, None)
    assert plan.contexts == [] and plan.workspace is True and plan.prefer is None
    assert plan.user_error and str(target) in plan.user_error and "\n" not in plan.user_error
    assert plan.user == repo_context.engine_user_settings()
    repo = make_repo(tmp_path, "alpha").resolve()
    with pytest.raises(config.ConfigError) as err:  # as `load_config` raises today
        repo_context.plan_start(repo, None)
    assert str(target) in str(err.value)


def test_plan_start_in_a_repository_with_an_unusable_workspace_is_the_single_mode_with_the_error(tmp_path):
    repo = make_repo(tmp_path, "alpha").resolve()
    user_text(yaml.safe_dump({"workspace": {"roots": ["relative/root"]}}))  # `load_config` reads it, the workspace not
    plan = repo_context.plan_start(repo, None)
    (ctx,) = plan.contexts
    assert plan.workspace is False and ctx.state == "live" and ctx.runs is None
    assert plan.user_error and "workspace.roots" in plan.user_error


# --------------------------------------------------------------------------- the cached counts (W3)


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def test_counts_are_read_once_per_ttl_and_a_new_context_object_reads_again(tmp_path, monkeypatch):
    calls: list[str] = []

    def fake(ctx):
        calls.append(ctx.id)
        return {"waiting": len(calls), "active": 0, "failed": 0}

    monkeypatch.setattr(repo_context, "counts", fake)
    clock = Clock()
    cache = repo_context.CountCache(ttl=5.0, clock=clock)
    ctx = RepoContext("one", "one", tmp_path)
    assert cache.get(ctx) == {"waiting": 1, "active": 0, "failed": 0}
    clock.now += 4.9
    assert cache.get(ctx) == {"waiting": 1, "active": 0, "failed": 0} and len(calls) == 1
    clock.now += 0.2  # past the TTL
    assert cache.get(ctx)["waiting"] == 2 and len(calls) == 2
    assert cache.get(dataclasses.replace(ctx))["waiting"] == 3 and len(calls) == 3  # a rescan's new object
    assert repo_context.COUNTS_TTL_S == 5.0


def test_repos_view_takes_its_counts_from_the_callable_it_is_given(tmp_path):
    ctx = RepoContext("one", "one", tmp_path, cfg=None, state="not_set_up")
    view = repo_context.repos_view([ctx], [], None, None, count=lambda c: {"waiting": 7, "active": 1, "failed": 2})
    assert (view["repos"][0]["waiting"], view["repos"][0]["active"], view["repos"][0]["failed"]) == (7, 1, 2)
