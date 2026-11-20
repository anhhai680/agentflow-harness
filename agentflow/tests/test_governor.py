"""`governor`: the usage-window verdict, finish-first ranking and the machine-wide `usage_window.json`."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

import pytest
import yaml
from agentflow import governor, pack
from agentflow.config import Usage, load_config
from agentflow.governor import (
    SLACK_S,
    SessionTicket,
    Verdict,
    WindowFile,
    account_key,
    account_of,
    ahead,
    current_usage,
    describe,
    merge_reading,
    rank_of,
    start_gate,
    valid_reading,
    verdict,
)

NOW = 1_800_000_000.0
U = Usage()


def w(u, refused=False, resets=NOW + 3600):
    return {"utilization": u, "resets_at": int(resets), "refused": refused, "ts": NOW}


def r(u, now, refused=False, resets=None):
    """A reading as a caller passes it to `record`, against the real clock (`read()` checks against it)."""
    return {"utilization": u, "resets_at": int(now) + 3600 if resets is None else resets, "refused": refused}


ACCT = account_key(None)


def test_constants():
    assert governor.VERSION == 1
    assert (governor.USAGE_HOLD, governor.GOVERNOR_STEP) == ("usage window", "governor")
    assert (governor.POLL_S, governor.SLACK_S, governor.LOCK_WAIT_S) == (30.0, 60.0, 2.0)
    assert (governor.FRESH_S, governor.READY_MAX_S, governor.INTERACTIVE_RISE) == (90.0, 600.0, 0.05)
    assert governor.SESSION_MAX_S == 6 * 3600
    assert governor.SPANS == {"five_hour": 5 * 3600, "seven_day": 7 * 86400}
    assert governor.MAX_BYTES == 256 * 1024


def test_verdict_marks():
    assert verdict(U, {"five_hour": w(0.75)}, NOW).level == "clear"  # Decided 1: strictly above
    assert verdict(U, {"five_hour": w(0.8)}, NOW) == Verdict("soft", "five_hour", 0.8, NOW + 3600 + SLACK_S)
    assert verdict(U, {"five_hour": w(0.95)}, NOW).level == "hard"
    assert verdict(U, {"seven_day": w(0.9)}, NOW).level == "soft"
    assert verdict(U, {"five_hour": w(0.1, refused=True)}, NOW).level == "hard"  # Decided 4
    assert verdict(Usage(governor="off"), {"five_hour": w(0.99)}, NOW).level == "clear"
    assert verdict(Usage(governor="observe"), {"five_hour": w(0.99)}, NOW).level == "hard"
    assert verdict(Usage(governor="bogus"), {"five_hour": w(0.99)}, NOW).level == "hard"  # R4: anything else is on
    assert verdict(U, {}, NOW) == Verdict("clear")


def test_verdict_names_the_worse_window():
    hard_over_soft = verdict(U, {"five_hour": w(0.95), "seven_day": w(0.99)}, NOW)
    assert (hard_over_soft.level, hard_over_soft.window) == ("hard", "five_hour")
    # both soft: 5h is 0.05 over 0.75, 7d is 0.10 over 0.85
    both = verdict(U, {"five_hour": w(0.8), "seven_day": w(0.95, resets=NOW + 86400)}, NOW)
    assert (both.level, both.window, both.until) == ("soft", "seven_day", int(NOW + 86400) + SLACK_S)


def test_verdict_never_raises_on_junk():
    assert verdict(U, None, NOW).level == "clear"  # type: ignore[arg-type]
    assert verdict(U, {"five_hour": "x", "seven_day": [1]}, NOW).level == "clear"


def test_a_reading_past_its_reset_governs_nothing():  # Review Focus 1
    assert verdict(U, {"five_hour": w(0.99, resets=NOW - SLACK_S - 1)}, NOW).level == "clear"
    assert verdict(U, {"five_hour": w(0.99, resets=NOW - 1)}, NOW).level == "hard"


def test_an_overage_reading_is_clamped_not_dropped():
    assert valid_reading("five_hour", w(2.0), NOW)["utilization"] == 1.5


def test_a_valid_reading_is_normalised():
    got = valid_reading("seven_day", {"utilization": 1, "resets_at": int(NOW) + 86400, "refused": False}, NOW)
    assert got == {"utilization": 1.0, "resets_at": int(NOW) + 86400, "refused": False, "ts": NOW}
    assert valid_reading("seven_day_opus", w(0.5), NOW) is None


@pytest.mark.parametrize("bad", [
    w(1.0, resets=NOW + 5 * 3600 + SLACK_S + 1), w(float("nan")), w(float("inf")), w(-0.1),
    {"utilization": "0.9", "resets_at": NOW + 60}, {"utilization": 0.9, "resets_at": True}, [], None,
    {"utilization": 0.9, "resets_at": int(NOW) + 60, "refused": "yes"}, {"utilization": True, "resets_at": int(NOW) + 60,
                                                                          "refused": False},
    {"utilization": 0.9, "resets_at": NOW + 60.5, "refused": False},
])
def test_an_invalid_reading_is_dropped(bad):  # Decided 5, Review Focus 4
    assert valid_reading("five_hour", bad, NOW) is None


def test_merge_keeps_the_newest_period_and_the_highest_reading():
    assert merge_reading(w(0.6), w(0.5))["utilization"] == 0.6
    assert merge_reading(w(0.9), w(0.1, resets=NOW + 7200))["utilization"] == 0.1
    assert merge_reading(w(0.6, refused=True), w(0.7))["refused"] is True
    assert merge_reading(w(0.6, refused=True), w(0.7))["utilization"] == 0.7
    assert merge_reading(w(0.1, resets=NOW + 7200), w(0.9))["utilization"] == 0.1  # an earlier period is ignored
    assert merge_reading(None, w(0.3)) == w(0.3)


def test_record_then_read_round_trips(tmp_path):
    now = time.time()
    wf = WindowFile(tmp_path / "f.json")
    assert wf.read() == {"windows": {}, "sessions": {}, "waiting": {}, "interactive": None, "error": None}
    wf.record(ACCT, {"five_hour": r(0.4, now), "seven_day_opus": r(0.9, now), "seven_day": {"utilization": "x"}}, now)
    stored = json.loads((tmp_path / "f.json").read_text(encoding="utf-8"))
    assert stored["version"] == 1
    got = wf.read()
    assert got["error"] is None
    assert got["windows"] == {ACCT: {"five_hour": {**r(0.4, now), "ts": now}}}
    wf.record(ACCT, {"five_hour": r(0.3, now, resets=int(now) + 3600)}, now + 5)  # a lower one of the period loses
    assert wf.read()["windows"][ACCT]["five_hour"]["utilization"] == 0.4
    assert not list(tmp_path.glob("*.tmp"))


def test_window_path_is_under_the_user_state_root(_state_home):
    assert governor.window_path() == pack.user_state_root() / "usage_window.json"
    assert pack.user_state_root() == _state_home / "agentflow"
    assert WindowFile().path == governor.window_path()


def test_a_file_of_another_version_is_read_empty_and_never_overwritten(tmp_path):
    now = time.time()
    path = tmp_path / "f.json"
    path.write_text(json.dumps({"version": 2, "windows": {ACCT: {"five_hour": r(0.9, now)}}}), encoding="utf-8")
    before = path.read_bytes()
    wf = WindowFile(path)
    got = wf.read()
    assert got["windows"] == {} and got["error"]
    wf.record(ACCT, {"five_hour": r(0.5, now)}, now)
    ticket = wf.enter_session("run", ACCT, now)
    wf.exit_session(ticket, {}, {"five_hour": r(0.5, now)}, now)
    assert path.read_bytes() == before


@pytest.mark.parametrize("content", [b"{oops", b'{"windows": {}}', b'{"version": true}', b"[]"])
def test_a_garbage_or_unversioned_file_is_replaced_by_the_next_write(tmp_path, content):
    now = time.time()
    path = tmp_path / "f.json"
    path.write_bytes(content)
    wf = WindowFile(path)
    assert wf.read()["error"]
    wf.record(ACCT, {"five_hour": {"utilization": "bad"}}, now)  # brings no reading: the file stays
    assert path.read_bytes() == content
    wf.record(ACCT, {"five_hour": r(0.5, now)}, now)
    assert json.loads(path.read_text(encoding="utf-8"))["version"] == 1
    got = wf.read()
    assert got["error"] is None and got["windows"][ACCT]["five_hour"]["utilization"] == 0.5


def test_a_heartbeat_never_rewrites_an_unusable_file(tmp_path):
    now = time.time()
    path = tmp_path / "f.json"
    path.write_bytes(b"{oops")
    wf = WindowFile(path)
    wf.wait("run/a", "/runs/a", [0.5, 1], now, True, now)
    wf.unwait("run/a")
    ticket = wf.enter_session("run/a", ACCT, now)
    assert ticket.alone is False and ticket.account == ACCT
    assert path.read_bytes() == b"{oops"


def _hold(path: Path) -> int:
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


def test_a_hardlinked_lock_fails_open(tmp_path):
    now = time.time()
    other = tmp_path / "queue.lock"
    fd = _hold(other)
    try:
        os.link(other, tmp_path / "f.lock")
        wf = WindowFile(tmp_path / "f.json")
        start = time.monotonic()
        wf.record(ACCT, {"five_hour": r(0.5, now)}, now)
        assert time.monotonic() - start < 0.5
        assert not (tmp_path / "f.json").exists()
    finally:
        os.close(fd)


def test_a_relative_xdg_state_home_is_ignored(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", "rel")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert pack.user_state_root() == tmp_path / "home" / ".local" / "state" / "agentflow"


def test_the_user_state_root_is_left_unresolved(monkeypatch, tmp_path):  # so ensure_dir still refuses a link
    (tmp_path / "real").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "real", target_is_directory=True)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "link"))
    assert pack.user_state_root() == tmp_path / "link" / "agentflow"


def _entry(**kw):
    base = {"pid": 4242, "run_dir": "/r", "rank": [0.8, 2], "since": NOW - 100, "seen": NOW - 10, "ready": True,
            "ready_since": NOW - 20}
    base.update(kw)
    return base


def _alive(pid):
    return True


def test_ahead_ranks_by_phase_then_open_tasks_then_since():
    assert ahead({"a": _entry(rank=[0.8, 2])}, "me", [0.6, 0], NOW, NOW, _alive) == ["a"]
    assert ahead({"a": _entry(rank=[0.6, 0])}, "me", [0.8, 2], NOW, NOW, _alive) == []
    assert ahead({"a": _entry(rank=[0.6, 1])}, "me", [0.6, 3], NOW, NOW, _alive) == ["a"]
    assert ahead({"a": _entry(rank=[0.6, 3])}, "me", [0.6, 1], NOW, NOW, _alive) == []
    assert ahead({"a": _entry(rank=[0.6, 1], since=NOW - 100)}, "me", [0.6, 1], NOW - 50, NOW, _alive) == ["a"]
    assert ahead({"a": _entry(rank=[0.6, 1], since=NOW - 10)}, "me", [0.6, 1], NOW - 50, NOW, _alive) == []
    assert ahead({"a": _entry(rank=[0.6, 1], since=NOW - 50)}, "me", [0.6, 1], NOW - 50, NOW, _alive) == []
    assert ahead({"me": _entry()}, "me", [0.0, 5], NOW, NOW, _alive) == []
    waiting = {"a": _entry(), "b": _entry(rank=[0.9, 0]), "c": _entry(rank=[0.1, 0])}
    assert sorted(ahead(waiting, "me", [0.5, 0], NOW, NOW, _alive)) == ["a", "b"]


@pytest.mark.parametrize("change", [
    {"ready": False}, {"ready": "yes"}, {"ready": 1},
    {"seen": NOW - governor.FRESH_S - 1}, {"seen": NOW + SLACK_S + 1}, {"since": NOW + SLACK_S + 1},
    {"ready_since": NOW + SLACK_S + 1}, {"seen": 1e999}, {"seen": float("nan")}, {"since": "x"}, {"ready_since": None},
    {"seen": True},
    {"ready_since": NOW - governor.READY_MAX_S - 1},
    {"rank": [2, 0]}, {"rank": [0.5]}, {"rank": ["a", 0]}, {"rank": [float("inf"), 0]}, {"rank": [0.9, -1]},
    {"rank": [0.9, 10**6 + 1]}, {"rank": [True, 0]}, {"rank": (0.9, 0)}, {"rank": None},
    {"pid": 0}, {"pid": 1}, {"pid": -1}, {"pid": True}, {"pid": "12"}, {"pid": 2**22}, {"pid": 12.0},
])
def test_ahead_counts_only_ready_fresh_live_entries(change):  # Review Focus 2
    assert ahead({"a": _entry()}, "me", [0.6, 0], NOW, NOW, _alive) == ["a"]
    assert ahead({"a": _entry(**change)}, "me", [0.6, 0], NOW, NOW, _alive) == []


def test_ahead_skips_a_dead_pid_and_never_raises():
    assert ahead({"a": _entry()}, "me", [0.6, 0], NOW, NOW, lambda pid: False) == []

    def boom(pid):
        raise OSError("no")

    assert ahead({"a": _entry()}, "me", [0.6, 0], NOW, NOW, boom) == []
    assert ahead({"a": "x", "b": [1], 3: _entry()}, "me", [0.6, 0], NOW, NOW, _alive) == []
    assert ahead(["a"], "me", [0.6, 0], NOW, NOW, _alive) == []  # type: ignore[arg-type]
    assert ahead(None, "me", [0.6, 0], NOW, NOW, _alive) == []  # type: ignore[arg-type]


def test_ahead_looks_up_pid_alive_at_call_time(monkeypatch):
    from agentflow import events

    monkeypatch.setattr(events, "_pid_alive", lambda pid: False)
    assert ahead({"a": _entry()}, "me", [0.6, 0], NOW, NOW) == []
    monkeypatch.setattr(events, "_pid_alive", lambda pid: True)
    assert ahead({"a": _entry()}, "me", [0.6, 0], NOW, NOW) == ["a"]


def test_rank_of_handles_an_unknown_phase_and_no_plan():
    assert rank_of(["a", "b"], "retro", None) == [0.0, 10**6]
    assert rank_of(["a", "b", "c", "d"], "c", 3) == [0.5, 3.0]
    assert rank_of(["a", "b"], "b", 10**9) == [0.5, 10**6]
    assert rank_of(["a", "b"], "b", True) == [0.5, 10**6]
    assert rank_of([], "b", -2) == [0.0, 10**6]


def test_start_gate_reasons(tmp_path):
    now = time.time()
    wf = WindowFile(tmp_path / "f.json")
    wf.record(ACCT, {"five_hour": r(0.8, now)}, now)
    v = Verdict("soft", "five_hour", 0.8, int(now) + 3600 + SLACK_S)
    assert start_gate(U, ACCT, now, wf) == describe(v, U, now)
    assert start_gate(U, account_key("/other"), now, wf) is None  # another account's readings hold nobody here
    assert start_gate(Usage(governor="observe"), ACCT, now, wf) is None
    assert start_gate(Usage(governor="off"), ACCT, now, wf) is None

    wq = WindowFile(tmp_path / "q.json")
    wq.wait("run/a", "/runs/a", [0.5, 1], now - 10, True, now)
    assert start_gate(U, ACCT, now, wq) == "1 run(s) wait on the usage window and go first"
    assert start_gate(Usage(finish_first=False), ACCT, now, wq) is None
    assert start_gate(Usage(governor="observe"), ACCT, now, wq) is None
    assert start_gate(U, ACCT, now + governor.FRESH_S + 1, wq) is None  # a stale entry
    wq.wait("run/a", "/runs/a", [0.5, 1], now - 10, False, now)
    assert start_gate(U, ACCT, now, wq) is None  # still governed itself: it blocks nobody


def test_start_gate_never_raises(tmp_path, monkeypatch):
    path = tmp_path / "f.json"
    path.write_bytes(b'{"version": 1, "windows": {"x": [1]}, "waiting": "no", "sessions": 3, "interactive": [1]}')
    assert start_gate(U, ACCT, time.time(), WindowFile(path)) is None
    path.write_bytes(b'{"version": 1, "windows": {"x": {"five_hour": {"utilization": NaN}}}}')
    assert start_gate(U, ACCT, time.time(), WindowFile(path)) is None
    monkeypatch.setattr(governor, "verdict", lambda *a: 1 / 0)
    assert start_gate(U, ACCT, time.time(), WindowFile(path)) is None


def test_waiter_entries_track_ready_since(tmp_path):
    now = time.time()
    wf = WindowFile(tmp_path / "f.json")
    wf.wait("run/a", "/runs/a", [0.5, 1], now - 10, False, now)
    entry = wf.read()["waiting"]["run/a"]
    assert entry["ready"] is False and entry["ready_since"] is None and entry["pid"] == os.getpid()
    wf.wait("run/a", "/runs/a", [0.5, 1], now - 10, True, now + 1)
    wf.wait("run/a", "/runs/a", [0.5, 1], now - 10, True, now + 2)
    entry = wf.read()["waiting"]["run/a"]
    assert (entry["ready_since"], entry["seen"]) == (now + 1, now + 2)
    wf.wait("run/a", "/runs/a", [0.5, 1], now - 10, False, now + 3)
    assert wf.read()["waiting"]["run/a"]["ready_since"] is None
    wf.unwait("run/a")
    assert wf.read()["waiting"] == {}


def test_a_ready_waiter_keeps_its_first_ready_since_past_the_cap(tmp_path):
    now = time.time()
    wf = WindowFile(tmp_path / "f.json")
    wf.wait("run/a", "/runs/a", [0.5, 1], now - 10, True, now)
    later = now + governor.READY_MAX_S + 30
    for t in range(int(now) + 30, int(later), 30):  # heartbeats within FRESH_S keep the entry live
        wf.wait("run/a", "/runs/a", [0.5, 1], now - 10, True, float(t))
    wf.wait("run/a", "/runs/a", [0.5, 1], now - 10, True, later)
    entry = wf.read()["waiting"]["run/a"]
    assert entry["ready_since"] == now  # so it stops counting after READY_MAX_S
    assert ahead({"run/a": entry}, "me", [0.0, 5], later, later) == []


def test_a_write_prunes_entries_that_no_longer_count(tmp_path):
    now = time.time()
    path = tmp_path / "f.json"
    good = {"pid": os.getpid(), "run_dir": "/r", "rank": [0.5, 1], "since": now - 5, "seen": now - 5, "ready": False,
            "ready_since": None}
    path.write_text(json.dumps({"version": 1, "windows": {}, "interactive": None, "waiting": {
        "live": good,
        "stale": {**good, "seen": now - governor.FRESH_S - 5},
        "dead": {**good, "pid": 2**22 - 1},
        "junk": {**good, "pid": "12"},
    }, "sessions": {
        "1:a": {"pid": os.getpid(), "run": "x", "since": now - 5, "account": ACCT},
        "2:b": {"pid": os.getpid(), "run": "x", "since": now - governor.SESSION_MAX_S - 5, "account": ACCT},
        "3:c": {"pid": 2**22 - 1, "run": "x", "since": now - 5, "account": ACCT},
    }}), encoding="utf-8")
    WindowFile(path).record(ACCT, {"five_hour": r(0.1, now)}, now)
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert list(stored["waiting"]) == ["live"]
    assert list(stored["sessions"]) == ["1:a"]


def _note_case(tmp_path, name, *, other=False, first_u=0.50, first_resets=None):
    now = time.time()
    resets = int(now) + 3600
    wf = WindowFile(tmp_path / f"{name}.json")
    wf.record(ACCT, {"five_hour": r(0.41, now, resets=resets)}, now)
    if other:
        wf.enter_session("run/other", ACCT, now)
    ticket = wf.enter_session("run/a", ACCT, now)
    wf.exit_session(ticket, {"five_hour": r(first_u, now, resets=first_resets or resets)},
                    {"five_hour": r(first_u + 0.02, now, resets=first_resets or resets)}, now + 10)
    return ticket, wf.read()


def test_the_interactive_note(tmp_path):
    ticket, got = _note_case(tmp_path, "alone")
    assert isinstance(ticket, SessionTicket) and ticket.alone is True
    assert ticket.token.startswith(f"{os.getpid()}:") and ticket.baseline["five_hour"]["utilization"] == 0.41
    note = got["interactive"]
    assert note["window"] == "five_hour" and note["rise"] == pytest.approx(0.09)
    assert (note["from"], note["to"]) == (0.41, 0.50)
    assert got["sessions"] == {}  # the session is dropped
    assert got["windows"][ACCT]["five_hour"]["utilization"] == pytest.approx(0.52)  # `last` is recorded

    ticket, got = _note_case(tmp_path, "crowd", other=True)
    assert ticket.alone is False and got["interactive"] is None
    assert _note_case(tmp_path, "small", first_u=0.45)[1]["interactive"] is None
    assert _note_case(tmp_path, "period", first_resets=int(time.time()) + 7200)[1]["interactive"] is None


def test_another_accounts_session_leaves_this_one_alone(tmp_path):
    now = time.time()
    wf = WindowFile(tmp_path / "f.json")
    wf.enter_session("run/b", account_key("/other"), now)
    assert wf.enter_session("run/a", ACCT, now).alone is True


def test_a_bad_interactive_note_is_dropped(tmp_path):
    path = tmp_path / "f.json"
    now = time.time()
    note = {"window": "five_hour", "rise": 0.1, "from": 0.4, "to": 0.5, "since": now, "at": now, "resets_at": now + 60}
    path.write_text(json.dumps({"version": 1, "interactive": note}), encoding="utf-8")
    assert WindowFile(path).read()["interactive"]["rise"] == 0.1
    path.write_text(json.dumps({"version": 1, "interactive": {**note, "window": "seven_day_opus"}}), encoding="utf-8")
    assert WindowFile(path).read()["interactive"] is None
    path.write_text(json.dumps({"version": 1, "interactive": {**note, "rise": 1e999}}), encoding="utf-8")
    assert WindowFile(path).read()["interactive"] is None


def test_a_held_lock_never_blocks_a_read_and_fails_a_write_open(tmp_path):
    now = time.time()
    wf = WindowFile(tmp_path / "f.json")
    wf.record(ACCT, {"five_hour": r(0.4, now)}, now)
    before = (tmp_path / "f.json").read_bytes()
    fd = _hold(tmp_path / "f.lock")
    try:
        start = time.monotonic()
        assert wf.read()["windows"][ACCT]["five_hour"]["utilization"] == 0.4
        assert time.monotonic() - start < 0.5
        start = time.monotonic()
        wf.record(ACCT, {"five_hour": r(0.6, now)}, now)
        took = time.monotonic() - start
        assert governor.LOCK_WAIT_S - 0.1 <= took < governor.LOCK_WAIT_S + 0.5
        assert (tmp_path / "f.json").read_bytes() == before
        start = time.monotonic()
        wf.record(ACCT, {"five_hour": r(0.6, now)}, now, wait_s=0)
        assert time.monotonic() - start < 0.3
    finally:
        os.close(fd)


def test_the_lock_wait_is_read_at_call_time(tmp_path, monkeypatch):
    monkeypatch.setattr(governor, "LOCK_WAIT_S", 0.0)
    fd = _hold(tmp_path / "f.lock")
    try:
        start = time.monotonic()
        WindowFile(tmp_path / "f.json").record(ACCT, {"five_hour": r(0.6, time.time())}, time.time())
        assert time.monotonic() - start < 0.3
    finally:
        os.close(fd)


def test_a_planted_hardlink_at_a_temp_name_is_never_written_through(tmp_path, monkeypatch):
    now = time.time()
    victim = tmp_path / "victim.txt"
    victim.write_text("keep", encoding="utf-8")
    fixed = uuid.UUID(int=7)
    os.link(victim, tmp_path / f"f.{fixed.hex}.tmp")
    monkeypatch.setattr(uuid, "uuid4", lambda: fixed)
    wf = WindowFile(tmp_path / "f.json")
    wf.record(ACCT, {"five_hour": r(0.5, now)}, now)
    assert victim.read_text(encoding="utf-8") == "keep"
    assert not (tmp_path / "f.json").exists()


def test_temp_names_are_unique(tmp_path, monkeypatch):
    seen: list[str] = []
    real = governor.safeio.write_new

    def spy(path, text, mode=0o644):
        seen.append(Path(path).name)
        assert mode == 0o600
        return real(path, text, mode)

    monkeypatch.setattr(governor.safeio, "write_new", spy)
    wf = WindowFile(tmp_path / "f.json")
    for i in range(3):
        wf.record(ACCT, {"five_hour": r(0.1 * i, time.time())}, time.time())
    assert len(set(seen)) == 3 and all(n.startswith("f.") and n.endswith(".tmp") for n in seen)
    assert not list(tmp_path.glob("*.tmp"))


def test_two_writers_never_tear_the_file(tmp_path):
    now = time.time()
    path = tmp_path / "f.json"
    errors: list[BaseException] = []

    def writer(offset):
        try:
            wf = WindowFile(path)
            for i in range(50):
                wf.record(ACCT, {"five_hour": r((2 * i + offset) / 100, now)}, now, wait_s=10)
                assert wf.read()["error"] is None
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(k,)) for k in (0, 1)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    got = WindowFile(path).read()
    assert got["error"] is None and got["windows"][ACCT]["five_hour"]["utilization"] == 0.99
    assert not list(tmp_path.glob("*.tmp"))


def test_a_half_written_file_reads_empty(tmp_path):
    now = time.time()
    wf = WindowFile(tmp_path / "f.json")
    wf.record(ACCT, {"five_hour": r(0.4, now)}, now)
    text = (tmp_path / "f.json").read_text(encoding="utf-8")
    (tmp_path / "f.json").write_text(text[: len(text) // 2], encoding="utf-8")
    got = wf.read()
    assert got["windows"] == {} and got["error"]


@pytest.mark.parametrize("content", [b'{"version": 1, "windows": {"a": {"five_hour": {"utilization": NaN}}}}',
                                     b'{"version": 1, "windows": {"a": {"five_hour": {"utilization": Infinity}}}}',
                                     b"\xff\xfe"])
def test_a_non_json_constant_or_non_utf8_file_reads_empty(tmp_path, content):
    (tmp_path / "f.json").write_bytes(content)
    got = WindowFile(tmp_path / "f.json").read()
    assert got["windows"] == {} and got["error"]


def test_an_oversized_file_reads_empty(tmp_path):
    (tmp_path / "f.json").write_bytes(b" " * (governor.MAX_BYTES + 1))
    got = WindowFile(tmp_path / "f.json").read()
    assert got["windows"] == {} and got["error"]


def test_far_future_and_malformed_entries_are_dropped_on_read(tmp_path):
    now = time.time()
    path = tmp_path / "f.json"
    path.write_text(json.dumps({"version": 1, "windows": {
        ACCT: {"five_hour": r(0.9, now, resets=int(now) + 30 * 86400), "seven_day": r(0.5, now, resets=int(now) + 600)},
        "b": "x",
    }, "waiting": {"a": [1], "b": {"pid": "12"}}, "sessions": {"x": None}}), encoding="utf-8")
    got = WindowFile(path).read()
    assert got["windows"] == {ACCT: {"seven_day": {**r(0.5, now, resets=int(now) + 600), "ts": pytest.approx(now, abs=5)}}}
    assert got["waiting"] == {} and got["sessions"] == {} and got["error"] is None


def test_a_symlink_at_the_file_or_lock_is_never_followed(tmp_path):
    now = time.time()
    target = tmp_path / "target.json"
    target.write_text(json.dumps({"version": 1, "windows": {ACCT: {"five_hour": r(0.9, now)}}}), encoding="utf-8")
    before = target.read_bytes()
    (tmp_path / "f.json").symlink_to(target)
    wf = WindowFile(tmp_path / "f.json")
    got = wf.read()
    assert got["windows"] == {} and got["error"]
    wf.record(ACCT, {"five_hour": r(0.5, now)}, now)
    assert target.read_bytes() == before

    lock_target = tmp_path / "lock-target"
    lock_target.write_text("x", encoding="utf-8")
    (tmp_path / "g.lock").symlink_to(lock_target)
    wg = WindowFile(tmp_path / "g.json")
    wg.record(ACCT, {"five_hour": r(0.5, now)}, now)
    wg.wait("run", "/r", [0.5, 1], now, True, now)
    assert lock_target.read_text(encoding="utf-8") == "x"
    assert not (tmp_path / "g.json").exists()


def test_a_symlinked_state_folder_is_refused(tmp_path):
    (tmp_path / "real").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "real", target_is_directory=True)
    now = time.time()
    WindowFile(tmp_path / "link" / "f.json").record(ACCT, {"five_hour": r(0.5, now)}, now)
    assert list((tmp_path / "real").iterdir()) == []


def test_describe_never_raises():
    now = time.time()
    until = now + 3600
    hm = datetime.fromtimestamp(until).strftime("%H:%M")
    assert describe(Verdict("soft", "five_hour", 0.82, until), U, now) == (
        f"the 5h usage window is at 82% (above usage.five_hour_soft 0.75) until {hm}")
    assert describe(Verdict("hard", "five_hour", 0.95, until), U, now) == (
        f"the 5h usage window is at 95% (above usage.five_hour_hard 0.92) until {hm}")
    assert describe(Verdict("hard", "five_hour", 0.1, until), U, now) == (
        f"the 5h usage window refuses requests until {hm}")
    far = now + 3 * 86400
    assert describe(Verdict("soft", "seven_day", 0.9, far), U, now) == (
        f"the 7d usage window is at 90% (above usage.seven_day_soft 0.85) until "
        f"{datetime.fromtimestamp(far).strftime('%a %H:%M')}")
    for bad in (float("nan"), 1e12, None, "x"):
        text = describe(Verdict("soft", "five_hour", 0.82, bad), U, now)
        assert text == "the 5h usage window is at 82% (above usage.five_hour_soft 0.75)"
    assert isinstance(describe(Verdict("soft", None, None, None), U, now), str)
    assert isinstance(describe(None, None, None), str)  # type: ignore[arg-type]


def test_account_key_and_account_of(cfg, monkeypatch):
    assert account_key(None) == hashlib.sha256(b"default").hexdigest()[:12]
    assert account_key("") == account_key(None)
    assert account_key("/a") == hashlib.sha256(b"/a").hexdigest()[:12] != account_key(None)
    assert account_of(cfg) == account_key(os.environ["CLAUDE_CONFIG_DIR"])
    monkeypatch.delenv("CLAUDE_CONFIG_DIR")
    assert account_of(cfg) == account_key(None)
    cfg.defaults.env["CLAUDE_CONFIG_DIR"] = "/x"
    assert account_of(cfg) == account_key("/x")


def test_current_usage_reloads_and_keeps_the_last_good(tmp_cfg):
    path = tmp_cfg({"usage": {"five_hour_soft": 0.5}})
    cfg = load_config(path)
    assert cfg.usage.five_hour_soft == 0.5

    def rewrite(block):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        data["usage"] = block
        path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    rewrite({"five_hour_soft": 0.6, "governor": "off"})
    got = current_usage(cfg)
    assert (got.five_hour_soft, got.governor) == (0.6, "off")
    rewrite({"five_hour_soft": 2})  # a validate error at usage.*: the last good one stays
    assert current_usage(cfg, got) is got
    assert current_usage(cfg) is cfg.usage
    rewrite({"five_hour_soft": True})  # a load error
    assert current_usage(cfg, got) is got
    path.write_text("{oops: [", encoding="utf-8")
    assert current_usage(cfg) is cfg.usage


def test_state_dir_default_is_under_user_state_root(monkeypatch, tmp_path, tmp_git):
    assert pack.state_dir(tmp_git).parent == pack.user_state_root()
    (tmp_path / "real").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "real", target_is_directory=True)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "link"))
    assert pack.state_dir(tmp_git).parent == pack.user_state_root().resolve()
