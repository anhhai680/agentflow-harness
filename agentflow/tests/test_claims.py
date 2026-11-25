"""`claims`: claim refs on `origin`, created and moved with leased pushes, against a real bare repository and two
real clones."""
from __future__ import annotations

import json
import shutil
import subprocess
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from agentflow import claims
from conftest import run_git

RUN = "20261125-0900-feature-b"
OTHER = "20261125-0930-feature-b"


def pack(flow_repo, **policy):
    """`flow_repo` configured as a `backlog` pack, with `policy` as its `backlog:` block."""
    return flow_repo.configure({"work_source": {"kind": "backlog", "path": None}, "backlog": policy or None})


def second_clone(flow_repo, tmp_path: Path, name: str = "Other") -> Path:
    """A clone of `flow_repo.origin` with its own `user.name`."""
    clone = tmp_path / f"clone-{name.lower()}"
    run_git(tmp_path, "clone", "-q", str(flow_repo.origin), str(clone))
    for key, value in (("user.name", name), ("user.email", f"{name.lower()}@example.com"), ("commit.gpgsign", "false")):
        run_git(clone, "config", key, value)
    return clone


def remote_refs(flow_repo) -> dict[str, str]:
    """`{ref: sha}` of every ref on the origin."""
    out = run_git(flow_repo.origin, "for-each-ref", "--format=%(refname) %(objectname)")
    return dict(line.split(" ", 1) for line in out.splitlines())


@pytest.fixture(params=["refs", "branch"])
def form(request) -> str:
    return request.param


@pytest.fixture
def cfg(flow_repo, form):
    return pack(flow_repo, claim_ref=form)


# --------------------------------------------------------------------------- create


def test_create_makes_the_ref_and_a_second_create_is_claimed(flow_repo, tmp_path, cfg):
    held = claims.create(cfg, flow_repo.root, "b", run=RUN, lane="feature", phase="define")
    name = claims.ref(cfg, "b")
    assert remote_refs(flow_repo)[name] == held.sha
    assert run_git(flow_repo.origin, "ls-tree", "--name-only", held.sha).split() == ["claim.json"]
    text = run_git(flow_repo.origin, "show", f"{held.sha}:claim.json")
    parsed = claims.parse_claim(cfg, "b", text, held.sha)
    assert parsed == held
    assert (parsed.item, parsed.run, parsed.lane, parsed.phase, parsed.status, parsed.by) == (
        "b", RUN, "feature", "define", "running", "Test")
    assert parsed.started == parsed.updated and parsed.unreadable == ""

    other = second_clone(flow_repo, tmp_path)
    with pytest.raises(claims.Claimed) as caught:
        claims.create(cfg, other, "b", run=OTHER, lane="feature", phase="plan")
    assert caught.value.claim.by == "Test" and caught.value.claim.phase == "define"
    assert caught.value.claim.sha == held.sha
    assert remote_refs(flow_repo)[name] == held.sha


def test_thirty_simultaneous_races_have_one_winner(flow_repo, tmp_path, cfg):
    clones = [flow_repo.root, second_clone(flow_repo, tmp_path)]
    for n in range(30):
        item = f"race-{n}"
        barrier = threading.Barrier(2)
        results: list[object] = [None, None]

        def start(i: int, item: str = item, barrier: threading.Barrier = barrier,
                  results: list[object] = results) -> None:
            barrier.wait()
            try:
                results[i] = claims.create(cfg, clones[i], item, run=f"{RUN}-{i}", lane="feature", phase="define")
            except claims.ClaimError as exc:
                results[i] = exc

        threads = [threading.Thread(target=start, args=(i,)) for i in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        won = [r for r in results if isinstance(r, claims.Claim)]
        lost = [r for r in results if isinstance(r, claims.Claimed)]
        assert len(won) == 1 and len(lost) == 1, (item, results)
        assert lost[0].claim.run == won[0].run
        assert remote_refs(flow_repo)[claims.ref(cfg, item)] == won[0].sha


# --------------------------------------------------------------------------- advance


def test_advance_moves_the_ref_forward_and_a_moved_ref_is_lost(flow_repo, tmp_path, cfg):
    name = claims.ref(cfg, "b")
    held = claims.create(cfg, flow_repo.root, "b", run=RUN, lane="feature", phase="define")
    moved = claims.advance(cfg, flow_repo.root, held, phase="build", status="waiting")
    assert remote_refs(flow_repo)[name] == moved.sha
    assert run_git(flow_repo.origin, "rev-parse", f"{moved.sha}^").strip() == held.sha
    assert (moved.phase, moved.status, moved.started, moved.run) == ("build", "waiting", held.started, RUN)
    assert claims.read(cfg, flow_repo.root, "b") == moved

    other = second_clone(flow_repo, tmp_path)
    run_git(other, "push", "-q", "origin", f":{name}")
    with pytest.raises(claims.Lost) as caught:
        claims.advance(cfg, flow_repo.root, moved, phase="verify", status="running")
    assert caught.value.claim is None
    assert name not in remote_refs(flow_repo)

    theirs = claims.create(cfg, other, "b", run=OTHER, lane="bug", phase="plan")
    with pytest.raises(claims.Lost) as caught:
        claims.advance(cfg, flow_repo.root, moved, phase="verify", status="running")
    assert caught.value.claim == theirs
    assert remote_refs(flow_repo)[name] == theirs.sha


def test_advance_after_the_holder_released_is_lost(flow_repo, cfg):
    held = claims.create(cfg, flow_repo.root, "b", run=RUN, lane="feature", phase="define")
    claims.release(cfg, flow_repo.root, held)
    with pytest.raises(claims.Lost) as caught:
        claims.advance(cfg, flow_repo.root, held, phase="build", status="running")
    assert caught.value.claim is None
    assert claims.ref(cfg, "b") not in remote_refs(flow_repo)


def test_advance_over_a_newer_commit_of_the_same_run_retries_once(flow_repo, tmp_path, cfg):
    held = claims.create(cfg, flow_repo.root, "b", run=RUN, lane="feature", phase="define")
    second = second_clone(flow_repo, tmp_path)
    seen = claims.read(cfg, second, "b")
    assert seen == held
    newer = claims.advance(cfg, second, seen, phase="plan", status="running")

    stale = claims.advance(cfg, flow_repo.root, held, phase="build", status="waiting")
    assert remote_refs(flow_repo)[claims.ref(cfg, "b")] == stale.sha
    assert run_git(flow_repo.origin, "rev-parse", f"{stale.sha}^").strip() == newer.sha
    assert (stale.phase, stale.status, stale.run, stale.started) == ("build", "waiting", RUN, held.started)


# --------------------------------------------------------------------------- release


def test_another_machines_claim_with_the_same_run_id_is_never_pushed_over(flow_repo, tmp_path, cfg):
    """Run ids are not unique across machines: a claim that only shares the id, not the lineage, is lost."""
    name = claims.ref(cfg, "b")
    held = claims.create(cfg, flow_repo.root, "b", run=RUN, lane="feature", phase="define")
    other = second_clone(flow_repo, tmp_path)
    run_git(other, "push", "-q", "origin", f":{name}")
    theirs = claims.create(cfg, other, "b", run=RUN, lane="feature", phase="define")
    assert theirs.run == held.run and theirs.sha != held.sha

    with pytest.raises(claims.Lost) as caught:
        claims.advance(cfg, flow_repo.root, held, phase="build", status="running")
    assert caught.value.claim == theirs
    assert remote_refs(flow_repo)[name] == theirs.sha
    with pytest.raises(claims.Lost) as caught:
        claims.release(cfg, flow_repo.root, held)
    assert caught.value.claim == theirs
    assert remote_refs(flow_repo)[name] == theirs.sha


def test_a_create_whose_answer_was_lost_is_still_made(flow_repo, cfg, monkeypatch):
    real = claims._push

    def lost_answer(repo, name, expected, source):
        proc = real(repo, name, expected, source)
        assert proc.returncode == 0
        return subprocess.CompletedProcess(proc.args, 124, "", "git push timed out")

    monkeypatch.setattr(claims, "_push", lost_answer)
    held = claims.create(cfg, flow_repo.root, "b", run=RUN, lane="feature", phase="define")
    assert remote_refs(flow_repo)[claims.ref(cfg, "b")] == held.sha
    assert claims.read(cfg, flow_repo.root, "b") == held


def test_two_claim_commits_from_identical_arguments_differ(flow_repo):
    claim = claims.Claim(item="b", run=RUN, lane="feature", phase="define", status="running", by="Test",
                         started="2026-11-25T09:00:00Z", updated="2026-11-25T09:00:00Z")
    first = claims._commit(flow_repo.root, claim, None)
    second = claims._commit(flow_repo.root, claim, None)
    assert first != second
    assert run_git(flow_repo.root, "rev-parse", f"{first}^{{tree}}") == run_git(
        flow_repo.root, "rev-parse", f"{second}^{{tree}}")


def test_release_deletes_with_a_lease(flow_repo, tmp_path, cfg):
    name = claims.ref(cfg, "b")
    held = claims.create(cfg, flow_repo.root, "b", run=RUN, lane="feature", phase="define")
    claims.release(cfg, flow_repo.root, held)
    assert name not in remote_refs(flow_repo)
    claims.release(cfg, flow_repo.root, held)  # already gone: success
    assert claims.read(cfg, flow_repo.root, "b") is None

    other = second_clone(flow_repo, tmp_path)
    theirs = claims.create(cfg, other, "b", run=OTHER, lane="feature", phase="build")
    with pytest.raises(claims.Lost) as caught:
        claims.release(cfg, flow_repo.root, held)
    assert caught.value.claim == theirs
    assert remote_refs(flow_repo)[name] == theirs.sha


def test_both_ref_forms(flow_repo):
    assert claims.ref(pack(flow_repo), "b") == "refs/agentflow/claims/b"
    assert claims.ref(pack(flow_repo, claim_ref="refs"), "b") == "refs/agentflow/claims/b"
    branch = pack(flow_repo, claim_ref="branch")
    assert claims.ref(branch, "b") == "refs/heads/agentflow/claims/b"
    held = claims.create(branch, flow_repo.root, "b", run=RUN, lane="feature", phase="define")
    assert remote_refs(flow_repo)["refs/heads/agentflow/claims/b"] == held.sha
    assert "refs/agentflow/claims/b" not in remote_refs(flow_repo)
    claims.release(branch, flow_repo.root, held)
    assert "refs/heads/agentflow/claims/b" not in remote_refs(flow_repo)


@pytest.mark.parametrize("bad", ["", "B", "-b", "a/b", "a..b", "a b", "a\nb", "../x", "b.lock", None, 7])
def test_ref_refuses_an_id_that_is_not_a_module_id(flow_repo, bad):
    with pytest.raises(ValueError):
        claims.ref(pack(flow_repo), bad)


# --------------------------------------------------------------------------- hosts and origins


def test_a_host_that_hides_the_namespace_is_refused(flow_repo):
    cfg = pack(flow_repo)
    run_git(flow_repo.origin, "config", "receive.hideRefs", "refs/agentflow/")
    with pytest.raises(claims.HostRefused) as caught:
        claims.create(cfg, flow_repo.root, "b", run=RUN, lane="feature", phase="define")
    assert "backlog.claim_ref: branch" in str(caught.value)
    assert not any("claims" in name for name in remote_refs(flow_repo))


def _failing_push(monkeypatch, stderr: str, times: int) -> list[str]:
    """Make the first `times` claim pushes fail with `stderr` (the rest go through); the names pushed to."""
    real, pushed = claims._push, []

    def push(repo, name, expected, source):
        pushed.append(name)
        if len(pushed) <= times:
            return subprocess.CompletedProcess(["git", "push"], 128, "", stderr)
        return real(repo, name, expected, source)

    monkeypatch.setattr(claims, "_push", push)
    return pushed


def test_a_push_refused_for_another_reason_keeps_gits_text(flow_repo, cfg, form, monkeypatch):
    denied = "remote: Permission to origin.git denied to someone.\nfatal: unable to access: \x1b[31m403\x1b[0m"
    pushed = _failing_push(monkeypatch, denied, times=2)
    with pytest.raises(claims.HostRefused) as caught:
        claims.create(cfg, flow_repo.root, "b", run=RUN, lane="feature", phase="define")
    text = str(caught.value)
    assert len(pushed) == 2  # pushed once more before calling it a refusal
    assert caught.value.detail == "fatal: unable to access: ?[31m403?[0m" and caught.value.detail in text
    assert "\n" not in text and "\x1b" not in text
    assert ("backlog.claim_ref: branch" in text) is (form == "refs")
    assert claims.ref(cfg, "b") not in remote_refs(flow_repo)


def test_a_claim_released_between_the_push_and_the_listing_is_pushed_again(flow_repo, cfg, monkeypatch):
    pushed = _failing_push(monkeypatch, "! [rejected] (stale info)", times=1)
    held = claims.create(cfg, flow_repo.root, "b", run=RUN, lane="feature", phase="define")
    assert len(pushed) == 2 and remote_refs(flow_repo)[claims.ref(cfg, "b")] == held.sha


def test_no_origin_or_claims_off_is_not_enabled(flow_repo):
    assert claims.enabled(pack(flow_repo), flow_repo.root)
    assert not claims.enabled(pack(flow_repo, claims=False), flow_repo.root)
    assert not claims.enabled(flow_repo.configure(), flow_repo.root)  # not a backlog pack
    cfg = pack(flow_repo)
    run_git(flow_repo.root, "remote", "remove", "origin")
    assert not claims.enabled(cfg, flow_repo.root)


def test_an_unreachable_origin_is_unreachable(flow_repo, tmp_path):
    cfg = pack(flow_repo)
    held = claims.create(cfg, flow_repo.root, "a", run=RUN, lane="feature", phase="define")
    shutil.move(flow_repo.origin, tmp_path / "moved.git")
    with pytest.raises(claims.Unreachable):
        claims.create(cfg, flow_repo.root, "b", run=RUN, lane="feature", phase="define")
    with pytest.raises(claims.Unreachable):
        claims.list_remote(cfg, flow_repo.root)
    with pytest.raises(claims.Unreachable):
        claims.read(cfg, flow_repo.root, "a")
    with pytest.raises(claims.Unreachable):
        claims.advance(cfg, flow_repo.root, held, phase="build", status="running")
    with pytest.raises(claims.Unreachable):
        claims.release(cfg, flow_repo.root, held)


# --------------------------------------------------------------------------- reading


def test_list_remote_fetches_only_what_changed(flow_repo, cfg, monkeypatch):
    a = claims.create(cfg, flow_repo.root, "a", run=RUN, lane="feature", phase="define")
    b = claims.create(cfg, flow_repo.root, "b", run=OTHER, lane="bug", phase="plan")
    first = claims.list_remote(cfg, flow_repo.root)
    assert first == {"a": a, "b": b}

    moved = claims.advance(cfg, flow_repo.root, b, phase="build", status="paused")
    calls: list[tuple[str, ...]] = []
    real = claims._run

    def spy(repo, *args, **kwargs):
        calls.append(args)
        return real(repo, *args, **kwargs)

    monkeypatch.setattr(claims, "_run", spy)
    second = claims.list_remote(cfg, flow_repo.root, seen=first)
    assert second == {"a": a, "b": moved}
    fetches = [call for call in calls if "fetch" in call]
    assert len(fetches) == 1
    refspecs = [arg for arg in fetches[0] if ":" in arg]
    assert refspecs == [f"+{claims.ref(cfg, 'b')}:refs/agentflow/remote-claims/b"]
    assert len([call for call in calls if "ls-remote" in call]) == 1

    calls.clear()
    assert claims.list_remote(cfg, flow_repo.root, seen=second) == second
    assert not [call for call in calls if "fetch" in call]


def test_list_remote_skips_refs_that_are_not_claims(flow_repo, tmp_path):
    cfg = pack(flow_repo)
    held = claims.create(cfg, flow_repo.root, "b", run=RUN, lane="feature", phase="define")
    other = second_clone(flow_repo, tmp_path)
    for bad in ("refs/agentflow/claims/Bad", "refs/agentflow/claims/x/y", "refs/agentflow/claimsx"):
        run_git(other, "push", "-q", "origin", f"{held.sha}:{bad}")
    assert claims.list_remote(cfg, flow_repo.root) == {"b": held}


VALID = {"item": "b", "run": RUN, "lane": "feature", "phase": "build", "status": "waiting", "by": "Test",
         "started": "2026-11-24T09:12:03Z", "updated": "2026-11-24T10:40:55Z"}


def _with(**changes) -> bytes:
    data = {**VALID, **changes}
    return json.dumps({k: v for k, v in data.items() if v is not None}).encode()


def _without(key: str) -> bytes:
    return json.dumps({k: v for k, v in VALID.items() if k != key}).encode()


@pytest.mark.parametrize("name, data", [
    ("bad json", b"{not json"),
    ("not an object", b"[1, 2]"),
    ("missing key", _without("by")),
    ("another item", _with(item="other")),
    ("unknown phase", _with(phase="deploy")),
    ("unknown lane", _with(lane="chore")),
    ("status done", _with(status="done")),
    ("by with a newline", _with(by="Mallory\nrm -rf")),
    ("by of 101 characters", _with(by="x" * 101)),
    ("updated yesterday", _with(updated="yesterday")),
    ("a newline for the T", _with(started="2026-01-01\n00:00:00+00:00")),
    ("a carriage return for the T", _with(updated="2026-01-01\r00:00:00+00:00")),
    ("an escape for the T", _with(updated="2026-01-01\x1b00:00:00+00:00")),
    ("over 4096 bytes", _with(pad="x" * 5000)),
    ("not utf-8", b'{"item": "\xff"}'),
    ("no claim.json", None),
])
def test_parse_claim_never_trusts_the_text(flow_repo, tmp_path, name, data):
    cfg = pack(flow_repo)
    other = second_clone(flow_repo, tmp_path)
    blob_file = tmp_path / "blob"
    blob_file.write_bytes(data if data is not None else b"{}")
    blob = run_git(other, "hash-object", "-w", str(blob_file)).strip()
    filename = "claim.json" if data is not None else "other.json"
    tree = run_git_input(other, f"100644 blob {blob}\t{filename}\n", "mktree").strip()
    sha = run_git(other, "commit-tree", tree, "-m", "forged").strip()
    run_git(other, "push", "-q", "origin", f"{sha}:refs/agentflow/claims/b")

    found = claims.read(cfg, flow_repo.root, "b")
    assert found is not None and found.unreadable, name
    assert found.sha == sha and found.item == "b"
    assert claims.list_remote(cfg, flow_repo.root)["b"] == found
    with pytest.raises(claims.Claimed) as caught:
        claims.create(cfg, flow_repo.root, "b", run=RUN, lane="feature", phase="define")
    assert caught.value.claim.unreadable


def run_git_input(cwd: Path, text: str, *args: str) -> str:
    import subprocess

    return subprocess.run(["git", *args], cwd=cwd, input=text, capture_output=True, text=True, check=True).stdout


@pytest.mark.parametrize("text", ["", "null", "1e999", "[" * 5000, '"' + "\\u0000" * 100 + '"', "{}" * 3000,
                                  json.dumps({**VALID, "started": "2026-13-45T99:00:00Z"}),
                                  json.dumps({**VALID, "updated": "2026-11-24T10:40:55"}),
                                  json.dumps({**VALID, "run": "../../x"}),
                                  json.dumps({**VALID, "by": ""}),
                                  json.dumps({**VALID, "by": "a b"}),
                                  json.dumps({**VALID, "phase": ["build"]})])
def test_parse_claim_never_raises(flow_repo, text):
    found = claims.parse_claim(pack(flow_repo), "b", text, "a" * 40)
    assert found.unreadable and found.sha == "a" * 40 and found.item == "b"


def test_describe_prints_every_field_on_one_line():
    odd = claims.Claim(item="b", run="r\x1b[2J", lane="feature", phase="plan\n", status="running\r", by="T\x07",
                       started="x", updated="2026-01-01\n00:00:00+00:00", sha="a" * 40)
    assert claims.describe(odd).isprintable()
    assert claims.describe(claims.Claim(**VALID, sha="a" * 40)) == (
        "b is claimed by Test (run 20261125-0900-feature-b, build, waiting, updated 2026-11-24T10:40:55Z)")


def test_parse_claim_reads_a_valid_claim(flow_repo):
    found = claims.parse_claim(pack(flow_repo), "b", json.dumps(VALID), "a" * 40)
    assert found == claims.Claim(**VALID, sha="a" * 40)


def test_committer_is_one_line_of_at_most_100(flow_repo, tmp_path, monkeypatch):
    assert claims.committer(flow_repo.root) == "Test"
    run_git(flow_repo.root, "config", "user.name", "y" * 150)
    assert claims.committer(flow_repo.root) == "y" * 100
    run_git(flow_repo.root, "config", "user.name", "Ann\nB")
    assert claims.committer(flow_repo.root) == "Ann B"
    empty = tmp_path / "gitconfig"
    empty.write_text("", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    run_git(flow_repo.root, "config", "--unset", "user.name")
    assert claims.committer(flow_repo.root) == "unknown"


def test_a_claim_by_an_odd_committer_still_parses(flow_repo):
    cfg = pack(flow_repo)
    run_git(flow_repo.root, "config", "user.name", "<é O'Brien>")
    held = claims.create(cfg, flow_repo.root, "b", run=RUN, lane="feature", phase="define")
    assert held.by == "<é O'Brien>"
    assert claims.read(cfg, flow_repo.root, "b") == held


# --------------------------------------------------------------------------- staleness


def _aged(hours: float, status: str) -> claims.Claim:
    now = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)
    updated = (now - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return claims.Claim(item="b", run=RUN, lane="feature", phase="build", status=status, by="Test",
                        started="2026-11-01T00:00:00Z", updated=updated)


def test_is_stale_counts_from_updated_whatever_the_status():
    now = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)
    assert claims.is_stale(_aged(25, "waiting"), 24, now)
    assert claims.is_stale(_aged(25, "running"), 24, now)
    assert not claims.is_stale(_aged(23, "running"), 24, now)
    assert claims.age_s(_aged(2, "paused"), now) == 7200.0
    unreadable = claims.Claim(item="b", run="", lane="", phase="", status="", by="", started="", updated="",
                              unreadable="bad")
    assert claims.age_s(unreadable, now) is None
    assert not claims.is_stale(unreadable, 24, now)


# --------------------------------------------------------------------------- a person frees a claim


def test_release_item_frees_what_it_read_and_reports_nothing_twice(flow_repo, tmp_path, cfg):
    held = claims.create(cfg, flow_repo.root, "b", run=RUN, lane="feature", phase="plan", status="waiting")
    other = second_clone(flow_repo, tmp_path)
    freed = claims.release_item(cfg, other, "b")  # a person on another machine
    assert freed == held and claims.ref(cfg, "b") not in remote_refs(flow_repo)
    assert claims.release_item(cfg, other, "b") is None
    with pytest.raises(ValueError):
        claims.release_item(cfg, other, "../x")


def test_release_item_frees_an_unreadable_claim_and_never_another_one(flow_repo, tmp_path, cfg, monkeypatch):
    name = claims.ref(cfg, "b")
    blob = run_git_input(flow_repo.root, "{}\n", "hash-object", "-w", "--stdin").strip()
    tree = run_git_input(flow_repo.root, f"100644 blob {blob}\tother.json\n", "mktree").strip()
    sha = run_git(flow_repo.root, "commit-tree", tree, "-m", "forged").strip()
    run_git(flow_repo.root, "push", "-q", "origin", f"{sha}:{name}")
    freed = claims.release_item(cfg, flow_repo.root, "b")
    assert freed is not None and freed.unreadable and freed.sha == sha and name not in remote_refs(flow_repo)

    # a claim made between the read and the delete is not deleted: the lease is on what was read
    first = claims.create(cfg, flow_repo.root, "b", run=RUN, lane="feature", phase="plan")
    other = second_clone(flow_repo, tmp_path)
    real = claims.read
    swaps: list[int] = []

    def swapped(cfg, repo, item_id):
        found = real(cfg, repo, item_id)
        if not swaps:
            swaps.append(1)
            claims.release(cfg, flow_repo.root, first)
            claims.create(cfg, other, "b", run=OTHER, lane="feature", phase="build")
        return found

    monkeypatch.setattr(claims, "read", swapped)
    with pytest.raises(claims.Lost):
        claims.release_item(cfg, flow_repo.root, "b")
    assert name in remote_refs(flow_repo)
