from __future__ import annotations

import dataclasses
import os
from pathlib import Path

import pytest

from agentflow.config import Affected, Gate, load_config
from agentflow.gates import (GateResult, affected_gate, command_digest, deferred_full, gates_ok, hash_inputs,
                             input_hashes, record_results, resume_report, reusable_passes, run_gates, run_gates_reusing,
                             summarize)
from agentflow.guards import snapshot
from conftest import PYTHON_CONFIG


def cfg_with(gates: dict[str, str | Gate], sets: dict[str, list[str]] | None = None, lines: int | None = None):
    cfg = load_config(PYTHON_CONFIG)
    built = {n: g if isinstance(g, Gate) else Gate(name=n, run=g) for n, g in gates.items()}
    context = cfg.context if lines is None else dataclasses.replace(cfg.context, gate_output_lines=lines)
    return dataclasses.replace(cfg, gates=built, gate_sets=sets or {}, context=context)


def test_gate_pass_fail_and_log(tmp_git, tmp_path):
    cfg = cfg_with({"ok": "true", "bad": "echo boom; exit 3"})
    r = run_gates(cfg, ["ok", "bad"], tmp_git, tmp_path / "logs")
    assert [g.passed for g in r] == [True, False]
    assert [g.skipped for g in r] == [False, False]
    assert "boom" in r[1].tail
    assert r[1].log_path.read_text().startswith("$ echo boom")
    assert r[1].log_path == tmp_path / "logs" / "bad.log"
    assert "exit 3" in r[1].reason
    assert not gates_ok(r)
    assert gates_ok(r[:1])


def test_gate_stderr_is_captured(tmp_git, tmp_path):
    cfg = cfg_with({"err": "echo oops 1>&2; exit 1"})
    (g,) = run_gates(cfg, ["err"], tmp_git, tmp_path / "logs")
    assert "oops" in g.tail and "oops" in g.log_path.read_text()


def test_gate_set_name_expands(tmp_git, tmp_path):
    cfg = cfg_with({"a": "true", "b": "true"}, {"both": ["a", "b"]})
    assert [g.name for g in run_gates(cfg, "both", tmp_git, tmp_path / "logs")] == ["a", "b"]


def test_unknown_gate_raises(tmp_git, tmp_path):
    with pytest.raises(KeyError, match="nope"):
        run_gates(cfg_with({"a": "true"}), ["nope"], tmp_git, tmp_path / "logs")


def test_gate_when_exists_missing_is_skipped(tmp_git, tmp_path):
    cfg = cfg_with({"web_types": Gate("web_types", "exit 1", cwd="console", when_exists="console/package.json")})
    (g,) = run_gates(cfg, ["web_types"], tmp_git, tmp_path / "logs")
    assert g.skipped and not g.passed and gates_ok([g])
    assert g.reason == "console/package.json missing"
    assert g.log_path is None
    assert summarize([g]) == "– web_types (skipped: console/package.json missing)"


def test_shipped_web_gate_skipped_without_console(tmp_git, tmp_path):
    cfg = load_config(PYTHON_CONFIG)
    (g,) = run_gates(cfg, ["web_types"], tmp_git, tmp_path / "logs")
    assert g.skipped and gates_ok([g])


def test_gate_tail_capped(tmp_git, tmp_path):
    cfg = cfg_with({"loud": "seq 1 500; exit 1"}, lines=200)
    (g,) = run_gates(cfg, ["loud"], tmp_git, tmp_path / "logs")
    assert g.tail.splitlines() == [str(n) for n in range(301, 501)]
    log_lines = g.log_path.read_text().splitlines()
    assert log_lines[0] == "$ seq 1 500; exit 1"
    assert log_lines[1:] == [str(n) for n in range(1, 501)]


def test_gate_cwd(tmp_git, tmp_path):
    (tmp_git / "console").mkdir()
    (tmp_git / "console" / "package.json").write_text("{}", encoding="utf-8")
    cfg = cfg_with({"where": Gate("where", "pwd", cwd="console", when_exists="console/package.json")})
    (g,) = run_gates(cfg, ["where"], tmp_git, tmp_path / "logs")
    assert g.passed
    assert Path(g.tail.strip()).resolve() == (tmp_git / "console").resolve()


def test_gate_without_cwd_runs_at_root(tmp_git, tmp_path):
    (g,) = run_gates(cfg_with({"where": "pwd"}), ["where"], tmp_git, tmp_path / "logs")
    assert Path(g.tail.strip()).resolve() == tmp_git.resolve()


def test_gate_stdin_is_closed(tmp_git, tmp_path):
    (g,) = run_gates(cfg_with({"in": "cat; echo done"}), ["in"], tmp_git, tmp_path / "logs")
    assert g.passed and "done" in g.tail


def test_summarize_lines():
    results = [
        GateResult("floor", True, False, "", None, ""),
        GateResult("py_lint", False, False, "exit 1", None, ""),
        GateResult("web_types", False, True, "console/package.json missing", None, ""),
    ]
    assert summarize(results).splitlines() == [
        "✓ floor",
        "✗ py_lint",
        "– web_types (skipped: console/package.json missing)",
    ]


def test_each_gate_records_how_long_it_ran(tmp_git, tmp_path):
    cfg = cfg_with({"slow": "sleep 0.2", "fast": "true", "gone": Gate(name="gone", run="true", when_exists="nope")})
    slow, fast, gone = run_gates(cfg, ["slow", "fast", "gone"], tmp_git, tmp_path / "logs")
    assert slow.duration_s >= 0.2 > fast.duration_s
    assert gone.skipped and gone.duration_s < 0.2


def test_run_gates_env_base_replaces_the_inherited_environment(tmp_git, tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTFLOW_INHERITED", "from-the-caller")
    cfg = cfg_with({"show": "echo inherited=${AGENTFLOW_INHERITED:-unset} base=${AGENTFLOW_BASE:-unset} "
                            "pack=${AGENTFLOW_PACK:-unset}"})
    cfg = dataclasses.replace(cfg, defaults=dataclasses.replace(cfg.defaults, env={"AGENTFLOW_PACK": "p"}))
    (default,) = run_gates(cfg, ["show"], tmp_git, tmp_path / "a")
    assert "inherited=from-the-caller base=unset pack=p" in default.tail
    base = {k: v for k, v in os.environ.items() if k != "AGENTFLOW_INHERITED"} | {"AGENTFLOW_BASE": "b",
                                                                                  "AGENTFLOW_PACK": "overridden"}
    (replaced,) = run_gates(cfg, ["show"], tmp_git, tmp_path / "b", env_base=base)
    assert "inherited=unset base=b pack=p" in replaced.tail  # defaults.env still goes on top


def test_a_gate_does_not_see_a_variable_the_dotenv_file_repeats(tmp_git, tmp_path, monkeypatch):
    (tmp_git / ".env").write_text("LEAKED_URL=leak-sentinel-7f3a\n", encoding="utf-8")
    monkeypatch.setenv("LEAKED_URL", "leak-sentinel-7f3a")
    monkeypatch.setenv("KEPT_URL", "kept-value")
    cfg = cfg_with({"show": "env"})
    cfg = dataclasses.replace(cfg, path=tmp_git / ".agentflow" / "config.yaml")
    (g,) = run_gates(cfg, ["show"], tmp_git, tmp_path / "logs")
    log = g.log_path.read_text()
    assert "LEAKED_URL" not in log and "KEPT_URL=kept-value" in log


def test_defaults_env_reaches_a_gate_even_when_the_name_is_dropped(tmp_git, tmp_path, monkeypatch):
    monkeypatch.setenv("LEAKED_URL", "leak-sentinel-7f3a")
    cfg = cfg_with({"show": "env"})
    inherit = dataclasses.replace(cfg.defaults.env_inherit, drop=["LEAKED_*"])
    defaults = dataclasses.replace(cfg.defaults, env_inherit=inherit, env={"LEAKED_URL": "from-pack"})
    cfg = dataclasses.replace(cfg, defaults=defaults, path=tmp_git / ".agentflow" / "config.yaml")
    (g,) = run_gates(cfg, ["show"], tmp_git, tmp_path / "logs")
    log = g.log_path.read_text()
    assert "LEAKED_URL=from-pack" in log and "leak-sentinel-7f3a" not in log


def test_an_explicit_env_base_still_wins_over_the_default(tmp_git, tmp_path, monkeypatch):
    monkeypatch.setenv("LEAKED_URL", "leak-sentinel-7f3a")
    (g,) = run_gates(cfg_with({"show": "env"}), ["show"], tmp_git, tmp_path / "logs", env_base={"PATH": os.environ["PATH"]})
    assert "LEAKED_URL" not in g.log_path.read_text()


def test_record_results_writes_tree_and_a_tail_only_when_asked(tmp_path):
    from agentflow.gates import record_results

    class FakeRun:
        def __init__(self):
            self.events = []

        def emit(self, type, **data):
            self.events.append({"type": type, **data})

    ok = GateResult("ok", True, False, "", None, "")
    bad = GateResult("bad", False, False, "exit 3", tmp_path / "bad.log", "boom")

    plain, tailed = FakeRun(), FakeRun()
    record_results(plain, "verify/verify", [ok, bad], "t1")
    record_results(tailed, "verify/verify", [ok, bad], "t1", with_tail=True)

    assert [e["type"] for e in plain.events] == ["gate_result", "gate_result"]
    assert [e["tree"] for e in plain.events + tailed.events] == ["t1"] * 4
    assert all("tail" not in e for e in plain.events)
    assert tailed.events[0]["tail"] == "" and tailed.events[1]["tail"] == "boom"
    assert tailed.events[1]["gate"] == "bad" and tailed.events[1]["passed"] is False
    assert tailed.events[1]["log"] == str(tmp_path / "bad.log")


# --------------------------------------------------------------------------- reuse by tree


def _pass(gate: str, tree: str | None, cmd: str | None, *, passed: bool = True, skipped: bool = False,
          key: str = "verify/verify") -> dict:
    e = {"type": "gate_result", "key": key, "gate": gate, "passed": passed, "skipped": skipped, "log": None}
    if tree is not None:
        e["tree"] = tree
    if cmd is not None:
        e["cmd"] = cmd
    return e


def test_command_digest_follows_the_command_cwd_when_exists_and_env():
    base = Gate("t", "pytest")
    d = command_digest(base, {"A": "1"})
    assert d == command_digest(Gate("t", "pytest"), {"A": "1"})
    assert len(d) == 12
    assert d != command_digest(Gate("t", "pytest -x"), {"A": "1"})
    assert d != command_digest(Gate("t", "pytest", cwd="web"), {"A": "1"})
    assert d != command_digest(Gate("t", "pytest", when_exists="x"), {"A": "1"})
    assert d != command_digest(base, {"A": "2"})


def test_a_pass_on_the_same_tree_and_command_is_reusable():
    g = Gate("t", "pytest")
    digests = {"t": command_digest(g, {})}
    found = reusable_passes([_pass("t", "T1", digests["t"])], [g], "T1", digests)
    assert list(found) == ["t"] and found["t"]["key"] == "verify/verify"


@pytest.mark.parametrize("event", [
    _pass("t", "T2", "same"),                    # another tree
    _pass("t", "T1", "other"),                   # the command changed
    _pass("t", "T1", "same", passed=False),      # a failure
    _pass("t", "T1", "same", passed=False, skipped=True),  # a skip
    _pass("t", None, "same"),                    # an older event with no tree
    _pass("t", "T1", None),                      # an older event with no command digest
])
def test_nothing_else_is_reusable(event):
    assert reusable_passes([event], [Gate("t", "pytest")], "T1", {"t": "same"}) == {}


def test_a_setup_gate_is_never_reused():
    events = [_pass("db_up", "T1", "same")]
    assert reusable_passes(events, [Gate("db_up", "up")], "T1", {"db_up": "same"}, never={"db_up"}) == {}


def test_no_tree_reuses_nothing():
    assert reusable_passes([_pass("t", "T1", "same")], [Gate("t", "x")], None, {"t": "same"}) == {}


# --------------------------------------------------------------------------- groups and resources


def _meet(me: str, other: str, wait_s: int) -> str:
    """A gate script: leave `<me>.on`, then wait up to `wait_s` for `<other>.on` (exit 1 if it never shows)."""
    return (f"touch {me}.on; i=0; while [ ! -e {other}.on ]; do i=$((i+1)); [ $i -gt {wait_s * 20} ] && exit 1; "
            "sleep 0.05; done")


def test_two_groups_run_at_the_same_time(tmp_git, tmp_path):
    cfg = cfg_with({"a": Gate("a", _meet("a", "b", 5), group="x"), "b": Gate("b", _meet("b", "a", 5), group="y")})
    results = run_gates(cfg, ["a", "b"], tmp_git, tmp_path / "logs")
    assert [(r.name, r.passed) for r in results] == [("a", True), ("b", True)]


def test_ungrouped_gates_still_run_one_after_another(tmp_git, tmp_path):
    cfg = cfg_with({"a": _meet("a", "b", 1), "b": _meet("b", "a", 1)})
    results = run_gates(cfg, ["a", "b"], tmp_git, tmp_path / "logs")
    assert [(r.name, r.passed) for r in results] == [("a", False), ("b", True)]


def test_a_group_runs_in_set_order_and_results_keep_set_order(tmp_git, tmp_path):
    def note(name: str) -> str:
        return f"sleep 0.2; echo {name} >> order.txt"
    cfg = cfg_with({"c": note("c"), "a": Gate("a", note("a"), group="x"), "b": Gate("b", note("b"), group="x")})
    results = run_gates(cfg, ["c", "a", "b"], tmp_git, tmp_path / "logs")
    order = (tmp_git / "order.txt").read_text().split()
    assert order.index("a") < order.index("b")
    assert sorted(order) == ["a", "b", "c"]
    assert [r.name for r in results] == ["c", "a", "b"]


def test_a_failed_gate_does_not_stop_the_rest_of_its_group(tmp_git, tmp_path):
    cfg = cfg_with({"a": Gate("a", "exit 1", group="x"), "b": Gate("b", "true", group="x"), "c": "true"})
    results = run_gates(cfg, ["a", "b", "c"], tmp_git, tmp_path / "logs")
    assert [(r.name, r.passed) for r in results] == [("a", False), ("b", True), ("c", True)]


def test_a_shared_resource_never_overlaps(tmp_git, tmp_path):
    script = "mkdir db.lock || exit 1; sleep 0.3; rmdir db.lock"
    cfg = cfg_with({"a": Gate("a", script, group="x", resource="db"), "b": Gate("b", script, group="y", resource="db")})
    results = run_gates(cfg, ["a", "b"], tmp_git, tmp_path / "logs")
    assert [(r.name, r.passed) for r in results] == [("a", True), ("b", True)]


_CHILD = """
import json, sys, time
from pathlib import Path
sys.path[:0] = [sys.argv[1], sys.argv[2]]
from test_gates import cfg_with
from agentflow.config import Gate
from agentflow.gates import run_gates
shared, wt, with_resource = Path(sys.argv[3]), Path(sys.argv[4]), sys.argv[5] == "1"
script = f"mkdir {shared}/db.lock || exit 1; sleep 0.5; rmdir {shared}/db.lock"
cfg = cfg_with({"g": Gate("g", script, resource="db" if with_resource else None)})
(shared / f"ready-{wt.name}").write_text("")
while not (shared / "go").exists():
    time.sleep(0.01)
(r,) = run_gates(cfg, ["g"], wt, shared / ("logs-" + wt.name))
print(json.dumps({"passed": r.passed, "waited_s": r.waited_s, "duration_s": r.duration_s}))
"""


def _two_processes(tmp_git, tmp_path, with_resource: bool) -> list[dict]:
    import json
    import subprocess
    import sys
    import time

    from conftest import run_git

    shared = tmp_path / "shared"
    shared.mkdir()
    trees = []
    for n in ("one", "two"):
        run_git(tmp_git, "worktree", "add", "-q", "-b", f"w-{n}", str(tmp_path / n))
        trees.append(tmp_path / n)
    root = str(Path(__file__).resolve().parents[2])
    tests = str(Path(__file__).resolve().parent)
    kids = [subprocess.Popen([sys.executable, "-c", _CHILD, root, tests, str(shared), str(wt), "1" if with_resource else "0"],
                             stdout=subprocess.PIPE, text=True) for wt in trees]
    deadline = time.monotonic() + 30
    while not all((shared / f"ready-{wt.name}").exists() for wt in trees):  # both children are up and polling for `go`
        assert time.monotonic() < deadline, "a child never got ready"
        time.sleep(0.01)
    (shared / "go").write_text("", encoding="utf-8")
    outs = [kid.communicate(timeout=60)[0] for kid in kids]
    assert [kid.returncode for kid in kids] == [0, 0], outs
    return [json.loads(out.splitlines()[-1]) for out in outs]


def test_a_resource_holds_across_two_processes(tmp_git, tmp_path):
    results = _two_processes(tmp_git, tmp_path, with_resource=True)
    assert [r["passed"] for r in results] == [True, True]
    assert max(r["waited_s"] for r in results) > 0
    assert all(r["duration_s"] < max(r["waited_s"] for r in results) + 5 for r in results)


def test_without_a_resource_two_processes_do_overlap(tmp_git, tmp_path):
    results = _two_processes(tmp_git, tmp_path, with_resource=False)
    assert sorted(r["passed"] for r in results) == [False, True]


def test_an_ungrouped_gate_with_a_resource_still_takes_the_lock(tmp_git, tmp_path):
    import threading
    import time

    from agentflow.gates import resource_lock

    held, release = threading.Event(), threading.Event()

    def hold():
        with resource_lock(tmp_git, "db"):
            held.set()
            release.wait(0.6)

    thread = threading.Thread(target=hold)
    thread.start()
    assert held.wait(5)
    started = time.monotonic()
    (r,) = run_gates(cfg_with({"a": Gate("a", "true", resource="db")}), ["a"], tmp_git, tmp_path / "logs")
    thread.join()
    assert r.passed and r.waited_s >= 0.4
    assert r.duration_s < 0.4 < time.monotonic() - started  # the wait is not in the command's own time


def test_a_free_resource_waits_for_nothing(tmp_git, tmp_path):
    (r,) = run_gates(cfg_with({"a": Gate("a", "true", resource="db")}), ["a"], tmp_git, tmp_path / "logs")
    assert r.passed and r.waited_s == 0.0
    (r2,) = run_gates(cfg_with({"a": "true"}), ["a"], tmp_git, tmp_path / "logs")
    assert r2.waited_s == 0.0


def test_a_gate_skipped_at_its_tier_takes_no_lock(tmp_git, tmp_path):
    import threading

    from agentflow.gates import resource_lock

    gate = Gate("a", "touch ran", resource="db", affected=Affected(run="touch ran {paths}", paths=["*.nothing"]))
    held, release = threading.Event(), threading.Event()

    def hold():
        with resource_lock(tmp_git, "db"):
            held.set()
            release.wait(5)

    thread = threading.Thread(target=hold)
    thread.start()
    assert held.wait(5)
    try:
        (r,) = run_gates(cfg_with({"a": gate}), ["a"], tmp_git, tmp_path / "logs", tier="affected", changed=["x.py"])
    finally:
        release.set()
        thread.join()
    assert r.skipped and r.waited_s == 0.0


def test_a_gate_waiting_on_a_resource_stops_when_the_run_leaves(tmp_git, tmp_path, monkeypatch):
    import threading
    import time

    from agentflow import gates

    monkeypatch.setattr(gates, "RESOURCE_POLL_S", 0.05)
    polls: list[str] = []
    real_flock = gates.fcntl.flock

    def counting(fd, op):
        if threading.current_thread().name.startswith("gates"):
            polls.append(threading.current_thread().name)
        return real_flock(fd, op)

    monkeypatch.setattr(gates.fcntl, "flock", counting)
    cfg = cfg_with({"a": Gate("a", "touch a.ran", group="x", resource="db"), "b": Gate("b", "true", group="y")})
    left: list[float] = []

    def interrupted(futures):
        deadline = time.monotonic() + 10
        while len(polls) < 2:
            assert time.monotonic() < deadline, "the gate never polled"
            time.sleep(0.01)
        left.append(time.monotonic())
        raise KeyboardInterrupt

    monkeypatch.setattr(gates, "_collect", interrupted)
    held, release = threading.Event(), threading.Event()

    def hold():
        with gates.resource_lock(tmp_git, "db"):
            held.set()
            release.wait(10)

    thread = threading.Thread(target=hold)
    thread.start()
    assert held.wait(5)
    try:
        with pytest.raises(KeyboardInterrupt):
            run_gates(cfg, ["a", "b"], tmp_git, tmp_path / "logs")
        returned = time.monotonic()
    finally:
        release.set()
        thread.join()
    assert returned - left[0] < gates.RESOURCE_POLL_S * 5
    assert not (tmp_git / "a.ran").exists()


def test_a_gate_that_gets_its_resource_after_the_run_left_never_starts(tmp_git, tmp_path, monkeypatch):
    import threading
    import time

    from agentflow import gates

    monkeypatch.setattr(gates, "RESOURCE_POLL_S", 0.3)
    held, release, polled = threading.Event(), threading.Event(), threading.Event()
    real_flock = gates.fcntl.flock

    def noticing(fd, op):
        if threading.current_thread().name.startswith("gates"):
            polled.set()
        return real_flock(fd, op)

    monkeypatch.setattr(gates.fcntl, "flock", noticing)
    real_kill_all = gates._Live.kill_all

    def kill_all_then_release(self):
        real_kill_all(self)
        release.set()  # the other run lets go of the resource right after this run left

    monkeypatch.setattr(gates._Live, "kill_all", kill_all_then_release)

    def interrupted(futures):
        assert polled.wait(10), "the gate never polled"
        raise KeyboardInterrupt

    monkeypatch.setattr(gates, "_collect", interrupted)

    def hold():
        with gates.resource_lock(tmp_git, "db"):
            held.set()
            release.wait(10)

    thread = threading.Thread(target=hold)
    thread.start()
    assert held.wait(5)
    cfg = cfg_with({"a": Gate("a", "touch a.ran", group="x", resource="db"), "b": Gate("b", "true", group="y")})
    try:
        with pytest.raises(KeyboardInterrupt):
            run_gates(cfg, ["a", "b"], tmp_git, tmp_path / "logs")
    finally:
        release.set()
        thread.join()
    time.sleep(0.1)
    assert not (tmp_git / "a.ran").exists()
    assert not (tmp_path / "logs" / "a.log").exists()


def test_a_waited_gate_records_its_wait():
    class FakeRun:
        def __init__(self):
            self.events = []

        def emit(self, type, **data):
            self.events.append({"type": type, **data})

    fake = FakeRun()
    record_results(fake, "k", [GateResult("w", True, False, "", None, "", waited_s=1.25),
                               GateResult("n", True, False, "", None, "", waited_s=0.0)], "t")
    assert fake.events[0]["resource_wait_s"] == 1.25
    assert "resource_wait_s" not in fake.events[1]


def test_leaving_early_kills_every_groups_process(tmp_git, tmp_path, monkeypatch):
    from agentflow import gates

    def sleeper(name: str) -> str:
        return f"echo $$ > {name}.pid; exec sleep 30"
    cfg = cfg_with({"a": Gate("a", sleeper("a"), group="x"), "b": Gate("b", sleeper("b"), group="y")})

    def interrupted(futures):
        import time
        deadline = time.monotonic() + 10
        while not ((tmp_git / "a.pid").exists() and (tmp_git / "b.pid").exists()
                   and (tmp_git / "a.pid").read_text().strip() and (tmp_git / "b.pid").read_text().strip()):
            assert time.monotonic() < deadline, "the gates never started"
            time.sleep(0.05)
        raise KeyboardInterrupt

    monkeypatch.setattr(gates, "_collect", interrupted)
    with pytest.raises(KeyboardInterrupt):
        run_gates(cfg, ["a", "b"], tmp_git, tmp_path / "logs")
    import time
    pids = [int((tmp_git / f"{n}.pid").read_text()) for n in "ab"]
    deadline = time.monotonic() + gates.KILL_WAIT_S
    for pid in pids:
        while True:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            assert time.monotonic() < deadline, f"process {pid} is still alive"
            time.sleep(0.05)


# --------------------------------------------------------------------------- reuse by input hash


def test_hash_inputs_ignores_paths_outside_the_globs():
    entries = [("api/a.py", "100644", "aaa"), ("web/b.ts", "100644", "bbb")]
    base = hash_inputs(entries, ["api/**"], "cmd")
    assert len(base) == 16
    assert hash_inputs([entries[0], ("web/b.ts", "100644", "other")], ["api/**"], "cmd") == base
    assert hash_inputs([("api/a.py", "100644", "changed"), entries[1]], ["api/**"], "cmd") != base
    assert hash_inputs([entries[1]], ["api/**"], "cmd") != base  # the matching entry removed
    assert hash_inputs([("api/a.py", "100755", "aaa"), entries[1]], ["api/**"], "cmd") != base  # the mode counts


def test_hash_inputs_follows_the_command():
    entries = [("api/a.py", "100644", "aaa")]
    assert hash_inputs(entries, ["api/**"], "one") != hash_inputs(entries, ["api/**"], "two")
    assert hash_inputs(entries, ["api/**"], "one") == hash_inputs(list(reversed(entries)), ["api/**"], "one")


def test_a_path_holding_a_newline_cannot_collide_with_two_entries():
    two = [("api/a", "100644", "X"), ("api/b", "100644", "Y")]
    one = [("api/a\n100644 Y api/b", "100644", "X")]  # git allows a newline in a file name
    for patterns in (["api/**"], ["**"]):
        assert hash_inputs(two, patterns, "cmd") != hash_inputs(one, patterns, "cmd")


def test_input_hashes_count_untracked_files(tmp_git):
    cfg = cfg_with({"api": Gate("api", "true", inputs=["api/**"]), "plain": "true"})
    gates = list(cfg.gates.values())
    (tmp_git / "api").mkdir()
    (tmp_git / "api" / "a.py").write_text("a = 1\n", encoding="utf-8")
    first = input_hashes(cfg, gates, tmp_git, snapshot(tmp_git))
    assert set(first) == {"api"}  # a gate with no `inputs` is absent
    (tmp_git / "api" / "new.py").write_text("n = 1\n", encoding="utf-8")  # untracked
    second = input_hashes(cfg, gates, tmp_git, snapshot(tmp_git))
    assert second["api"] != first["api"]
    (tmp_git / "README.md").write_text("changed\n", encoding="utf-8")  # outside the globs
    assert input_hashes(cfg, gates, tmp_git, snapshot(tmp_git)) == second


def _two_trees(run, cfg, tmp_git, tmp_path, name="api"):
    (tmp_git / "api").mkdir()
    (tmp_git / "api" / "a.py").write_text("a = 1\n", encoding="utf-8")
    tree_a = snapshot(tmp_git)
    first = run_gates_reusing(cfg, run, "build/one", [name], tmp_git, tmp_path / "logs", tree_a)
    record_results(run, "build/one", first, tree_a)
    (tmp_git / "web").mkdir()
    (tmp_git / "web" / "x.ts").write_text("export {}\n", encoding="utf-8")
    tree_b = snapshot(tmp_git)
    second = run_gates_reusing(cfg, run, "build/two", [name], tmp_git, tmp_path / "logs", tree_b)
    record_results(run, "build/two", second, tree_b)
    return first, second, tree_a, tree_b


def test_a_change_outside_a_gates_inputs_reuses_its_pass(run, tmp_git, tmp_path):
    cfg = cfg_with({"api": Gate("api", "true", inputs=["api/**"])})
    first, second, tree_a, tree_b = _two_trees(run, cfg, tmp_git, tmp_path)
    assert tree_a != tree_b
    assert first[0].reused_from is None and first[0].input_hash
    assert second[0].reused_from == "build/one" and second[0].passed
    events = run.events.read()
    results = [e for e in events if e["type"] == "gate_result" and e["gate"] == "api"]
    assert len(results) == 1 and results[0]["input_hash"] == first[0].input_hash
    [hit] = [e for e in events if e["type"] == "gate_cache_hit"]
    assert (hit["gate"], hit["from"], hit["input_hash"], hit["key"]) == ("api", "build/one", first[0].input_hash, "build/two")
    [reused] = [e for e in events if e["type"] == "gates_reused"]
    assert reused["gates"] == ["api"]


def test_a_gate_without_inputs_is_not_reused_on_another_tree(run, tmp_git, tmp_path):
    cfg = cfg_with({"api": "true"})
    first, second, _, _ = _two_trees(run, cfg, tmp_git, tmp_path)
    assert first[0].input_hash is None and second[0].reused_from is None
    events = run.events.read()
    assert len([e for e in events if e["type"] == "gate_result" and e["gate"] == "api"]) == 2
    assert not [e for e in events if e["type"] in ("gate_cache_hit", "gates_reused")]
    assert all("input_hash" not in e for e in events if e["type"] == "gate_result")


def test_a_change_inside_a_gates_inputs_runs_it_again(run, tmp_git, tmp_path):
    cfg = cfg_with({"api": Gate("api", "true", inputs=["api/**"])})
    _two_trees(run, cfg, tmp_git, tmp_path)
    (tmp_git / "api" / "a.py").write_text("a = 2\n", encoding="utf-8")
    tree_c = snapshot(tmp_git)
    (third,) = run_gates_reusing(cfg, run, "build/three", ["api"], tmp_git, tmp_path / "logs", tree_c)
    assert third.reused_from is None


def test_a_gate_cache_hit_needs_its_key_gate_and_input_hash(run):
    from agentflow.events import EventError

    for missing in ("key", "gate", "input_hash"):
        fields = {"key": "build/two", "gate": "api", "input_hash": "h", "from": "build/one"}
        del fields[missing]
        with pytest.raises(EventError, match=missing):
            run.emit("gate_cache_hit", **fields)
    run.emit("gate_cache_hit", key="build/two", gate="api", input_hash="h", **{"from": "build/one"})


# --------------------------------------------------------------------------- the affected tier


AFFECTED = Affected(run="printf '%s\\n' {paths} > out.txt", paths=["**/*.py"])


def _affected(tmp_git, files: list[str], **gate_kw):
    for rel in files:
        target = tmp_git / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x = 1\n", encoding="utf-8")
    gate_kw.setdefault("affected", AFFECTED)
    return cfg_with({"t": Gate("t", gate_kw.pop("run", "echo full > out.txt"), **gate_kw)})


def test_affected_runs_the_template_with_the_changed_python_paths(tmp_git, tmp_path):
    changed = ["a.py", "src/x.py", "docs/y.md"]
    cfg = _affected(tmp_git, changed)
    (result,) = run_gates(cfg, ["t"], tmp_git, tmp_path / "logs", tier="affected", changed=changed)
    assert (tmp_git / "out.txt").read_text() == "a.py\nsrc/x.py\n"
    assert result.passed and result.tier == "affected"
    assert result.log_path.read_text().startswith("$ printf '%s\\n' a.py src/x.py > out.txt")


def test_changed_paths_are_quoted_and_dash_safe(tmp_git, tmp_path):
    changed = ["$(touch pwned).py", "a b.py", "-rf.py"]
    cfg = _affected(tmp_git, changed)
    (result,) = run_gates(cfg, ["t"], tmp_git, tmp_path / "logs", tier="affected", changed=changed)
    assert result.passed and result.tier == "affected"
    assert sorted((tmp_git / "out.txt").read_text().splitlines()) == sorted(["$(touch pwned).py", "a b.py", "./-rf.py"])
    assert not (tmp_git / "pwned").exists()


def test_hostile_names_reach_the_command_as_one_inert_argument_each(tmp_git, tmp_path):
    changed = ["it's.py", "*.py", "`touch pwned2`.py", "a;touch pwned3.py", "{paths}.py", "--x.py",
               "q\"uote.py", "back\\slash.py", "$HOME.py"]
    affected = Affected(run="printf '%s\\0' {paths} > out.bin", paths=["**/*.py"])
    cfg = _affected(tmp_git, changed, affected=affected)
    (result,) = run_gates(cfg, ["t"], tmp_git, tmp_path / "logs", tier="affected", changed=changed)
    assert result.passed
    got = (tmp_git / "out.bin").read_bytes().decode().split("\0")[:-1]
    assert sorted(got) == sorted(["./--x.py" if p == "--x.py" else p for p in changed])
    assert not list(tmp_git.glob("pwned*"))


def test_a_name_with_a_control_character_is_dropped(tmp_git):
    names = ["new\nline.py", "cr\rx.py", "tab\tx.py", "esc\x1bx.py", "del\x7fx.py"]
    for name in names:
        (tmp_git / name).write_text("x = 1\n", encoding="utf-8")
    gate = Gate("t", "true", affected=Affected(run="echo {paths}", paths=["**"]))
    assert affected_gate(gate, names, tmp_git) is None
    assert affected_gate(gate, [*names, "src/app.py"], tmp_git).run == "echo src/app.py"


@pytest.mark.parametrize("template", ["echo '{paths}'", 'echo "{paths}"', "echo `{paths}`", "echo $({paths})",
                                      "echo ${{x:-{paths}}}", "echo # {paths}", "echo x{paths}"])
def test_a_template_that_would_not_keep_the_names_inert_runs_the_full_command(tmp_git, tmp_path, template):
    """Defence in depth for a pack that skipped `validate`: a `{paths}` that is not its own unquoted word."""
    cfg = _affected(tmp_git, ["$(touch pwned).py"], affected=Affected(run=template, paths=["**/*.py"]))
    (result,) = run_gates(cfg, ["t"], tmp_git, tmp_path / "logs", tier="affected", changed=["$(touch pwned).py"])
    assert result.tier == "full" and (tmp_git / "out.txt").read_text() == "full\n"
    assert not (tmp_git / "pwned").exists()


@pytest.mark.parametrize("template", __import__("test_config").HOSTILE_TEMPLATES)
def test_a_template_with_hostile_quoting_runs_the_full_command(tmp_git, tmp_path, template):
    """The runtime check refuses what `validate` refuses: no file name runs, whatever the quoting tricks."""
    name = "x$(touch pwned)y.py"
    cfg = _affected(tmp_git, [name], affected=Affected(run=template, paths=["**/*.py"]))
    (result,) = run_gates(cfg, ["t"], tmp_git, tmp_path / "logs", tier="affected", changed=[name])
    assert result.tier == "full" and (tmp_git / "out.txt").read_text() == "full\n"
    assert affected_gate(cfg.gates["t"], [name], tmp_git) is None
    assert not (tmp_git / "pwned").exists()


@pytest.mark.parametrize("template", [
    "echo {paths}", "ruff check {paths}", "cmd --flag {paths} | tail -n 5", "cmd {paths} && other",
    "echo $(date) {paths}", "echo \"$HOME\" {paths}",
])
def test_an_accepted_template_runs_no_hostile_name(tmp_git, template):
    import subprocess

    from agentflow.gates import _render_affected

    name = "x$(touch PWNED)y`touch PWNED`;touch PWNED.py"
    gate = Gate("t", "true", affected=Affected(run=template, paths=["**"]))
    (tmp_git / name).write_text("x = 1\n", encoding="utf-8")
    ran = affected_gate(gate, [name], tmp_git)
    assert ran is not None and ran.run == _render_affected(template, [name])
    bindir = tmp_git / "bin"
    bindir.mkdir()
    for tool in ("ruff", "cmd", "other"):
        (bindir / tool).write_text("#!/bin/sh\nexit 0\n")
        (bindir / tool).chmod(0o755)
    env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}"}
    subprocess.run(["sh", "-c", ran.run], cwd=tmp_git, env=env, capture_output=True, check=False)
    assert not (tmp_git / "PWNED").exists()


_FUZZ_FRAGMENTS = ("'", '"', "`", "\\", "$(", ")", "(", "${{", "}}", "{{", "#", ";", "&", "|", "\n", " ", " ", "$'",
                   "case x in ", "esac", "echo ", "a", "<<", "((", "$[", "]", "\\\n", "&&", ";;", "=", "<(", "$\"",
                   "x)", "${", "}", "{", "if ", "then ", "fi", "for i in ", "do ", "done", "[[ ", " ]]", "!", "*",
                   "<<<", "%", "/", ":", "-", "$", "<")
_FUZZ_NAMES = ["x$(touch PWNED)y.py", "q'$(touch PWNED)'q.py", "d\"$(touch PWNED)\"d.py", "b`touch PWNED`b.py"]


def _fuzz_template(rng) -> str:
    def pick() -> str:
        return "".join(rng.choice(_FUZZ_FRAGMENTS) for _ in range(rng.randint(0, 7)))

    return pick() + rng.choice([" ", " ", "\n", ""]) + "{paths}" + rng.choice([" ", "", ";", "\n", "|", "&"]) + pick()


def test_no_template_the_scanner_accepts_lets_a_hostile_name_run(tmp_path):
    """Differential check of `unquoted_paths_word` against real shells: random templates of shell metacharacters,
    the ones the scanner accepts rendered with hostile names and run under each shell present; none may run a name."""
    import random
    import shutil
    import subprocess
    from concurrent.futures import ThreadPoolExecutor

    from agentflow.config import unquoted_paths_word
    from agentflow.gates import _render_affected

    shells = list(dict.fromkeys(s for s in ("/bin/sh", "/bin/dash", "/bin/bash", shutil.which("bash"))
                                if s and os.path.exists(s)))
    rng = random.Random(20261116)
    accepted: list[str] = []
    for _ in range(40000):
        template = _fuzz_template(rng)
        if unquoted_paths_word(template):
            accepted.append(template)
            if len(accepted) == 400:
                break

    def run(job: tuple[int, str]) -> list[str]:
        index, template = job
        bad = []
        for k, sh in enumerate(shells):
            cwd = tmp_path / f"{index}-{k}"
            cwd.mkdir()
            try:
                subprocess.run([sh, "-c", _render_affected(template, _FUZZ_NAMES)], cwd=cwd, capture_output=True,
                               stdin=subprocess.DEVNULL, timeout=5, check=False,
                               env={"PATH": "/usr/bin:/bin", "HOME": str(cwd)})
            except subprocess.TimeoutExpired:
                bad.append(f"{template!r} hangs {sh}")
            if (cwd / "PWNED").exists():
                bad.append(f"{template!r} runs a name under {sh}")
        return bad

    assert len(accepted) == 400 and shells
    with ThreadPoolExecutor(8) as pool:
        found = [line for bad in pool.map(run, enumerate(accepted)) for line in bad]
    assert not found, "\n".join(found)


def test_a_rendered_command_over_the_byte_limit_runs_the_full_command(tmp_git, tmp_path):
    from agentflow.gates import MAX_AFFECTED_COMMAND_BYTES, MAX_AFFECTED_PATHS

    assert MAX_AFFECTED_COMMAND_BYTES == 64 * 1024
    deep = "/".join(["d" * 240] * 3)  # about 800 bytes a path, under every PATH_MAX
    names = [f"{deep}/t{i:02}.py" for i in range(90)]  # well under MAX_AFFECTED_PATHS, over the byte limit
    assert len(names) < MAX_AFFECTED_PATHS and sum(len(n) + 1 for n in names) > MAX_AFFECTED_COMMAND_BYTES
    cfg = _affected(tmp_git, names)
    (result,) = run_gates(cfg, ["t"], tmp_git, tmp_path / "logs", tier="affected", changed=names)
    assert result.passed and result.tier == "full" and (tmp_git / "out.txt").read_text() == "full\n"
    (result,) = run_gates(cfg, ["t"], tmp_git, tmp_path / "logs", tier="affected", changed=names[:10])
    assert result.tier == "affected" and len((tmp_git / "out.txt").read_text().splitlines()) == 10


def test_names_outside_the_worktree_are_never_passed(tmp_git, tmp_path):
    (tmp_path / "outside.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_git / "link.py").symlink_to(tmp_path / "outside.py")  # a symlink out of the worktree
    gate = Gate("t", "true", affected=Affected(run="echo {paths}", paths=["**"]))
    for changed in (["../outside.py"], [str(tmp_path / "outside.py")], ["link.py"], ["src/../../outside.py"], [""]):
        assert affected_gate(gate, changed, tmp_git) is None, changed
    inside = affected_gate(gate, ["src/app.py", "link.py"], tmp_git)
    assert inside is not None and inside.run == "echo src/app.py"


def test_double_braces_are_literal_and_only_paths_is_replaced(tmp_git):
    gate = Gate("t", "true", affected=Affected(run="echo {{x}} {paths} {other} ${{HOME}}", paths=["**"]))
    assert affected_gate(gate, ["src/app.py"], tmp_git).run == "echo {x} src/app.py {other} ${HOME}"


def test_a_deleted_path_is_not_passed(tmp_git, tmp_path):
    cfg = _affected(tmp_git, ["a.py"])
    (result,) = run_gates(cfg, ["t"], tmp_git, tmp_path / "logs", tier="affected", changed=["a.py", "gone.py"])
    assert (tmp_git / "out.txt").read_text() == "a.py\n" and result.tier == "affected"


def test_no_matching_path_skips_at_the_affected_tier(tmp_git, tmp_path):
    cfg = _affected(tmp_git, ["docs/y.md"])
    (result,) = run_gates(cfg, ["t"], tmp_git, tmp_path / "logs", tier="affected", changed=["docs/y.md"])
    assert result.skipped and not result.passed
    assert (result.reason, result.tier) == ("no affected paths", "affected")
    assert not (tmp_git / "out.txt").exists()


def test_a_gate_without_affected_runs_its_full_command_at_the_affected_tier(tmp_git, tmp_path):
    cfg = _affected(tmp_git, ["a.py"], affected=None)
    (result,) = run_gates(cfg, ["t"], tmp_git, tmp_path / "logs", tier="affected", changed=["a.py"])
    assert (tmp_git / "out.txt").read_text() == "full\n" and result.tier == "full"


def test_the_full_tier_runs_the_full_command(tmp_git, tmp_path):
    cfg = _affected(tmp_git, ["a.py"])
    (result,) = run_gates(cfg, ["t"], tmp_git, tmp_path / "logs", changed=["a.py"])
    assert (tmp_git / "out.txt").read_text() == "full\n" and result.tier == "full"


def test_too_many_paths_run_the_full_command(tmp_git, tmp_path):
    from agentflow.gates import MAX_AFFECTED_PATHS

    assert MAX_AFFECTED_PATHS == 200
    many = [f"m/t{i:03}.py" for i in range(MAX_AFFECTED_PATHS + 1)]
    cfg = _affected(tmp_git, many)
    (result,) = run_gates(cfg, ["t"], tmp_git, tmp_path / "logs", tier="affected", changed=many)
    assert (tmp_git / "out.txt").read_text() == "full\n"
    assert result.passed and result.tier == "full"
    (result,) = run_gates(cfg, ["t"], tmp_git, tmp_path / "logs", tier="affected", changed=many[:MAX_AFFECTED_PATHS])
    assert len((tmp_git / "out.txt").read_text().splitlines()) == MAX_AFFECTED_PATHS and result.tier == "affected"


def test_paths_are_relative_to_the_gates_cwd(tmp_git, tmp_path):
    cfg = _affected(tmp_git, ["api/a.py", "web/b.py"], cwd="api")
    (result,) = run_gates(cfg, ["t"], tmp_git, tmp_path / "logs", tier="affected", changed=["api/a.py", "web/b.py"])
    assert (tmp_git / "api" / "out.txt").read_text() == "a.py\n" and result.tier == "affected"
    (result,) = run_gates(cfg, ["t"], tmp_git, tmp_path / "logs", tier="affected", changed=["web/b.py"])
    assert result.skipped and result.reason == "no affected paths"


def test_record_results_marks_only_affected_results(run):
    full = GateResult("a", True, False, "", None, "")
    affected = GateResult("b", True, False, "", None, "", tier="affected")
    record_results(run, "review/fix_findings", [full, affected], "T")
    events = [e for e in run.events.read() if e["type"] == "gate_result"]
    assert "tier" not in events[0] and events[1]["tier"] == "affected"
    assert deferred_full(run.events.read()) is True
    assert deferred_full(events[:1]) is False


def test_an_affected_pass_is_never_reused(run, tmp_git, tmp_path):
    cfg = _affected(tmp_git, ["a.py"], inputs=["**/*.py"])
    tree = snapshot(tmp_git)
    first = run_gates_reusing(cfg, run, "review/fix_findings", ["t"], tmp_git, tmp_path / "logs", tree,
                              tier="affected", changed=["a.py"])
    record_results(run, "review/fix_findings", first, tree)
    assert first[0].passed and first[0].tier == "affected" and first[0].input_hash is None
    (tmp_git / "out.txt").unlink()
    again = run_gates_reusing(cfg, run, "verify/verify", ["t"], tmp_git, tmp_path / "logs", tree)
    assert again[0].reused_from is None and again[0].tier == "full"
    assert (tmp_git / "out.txt").read_text() == "full\n"
    events = run.events.read()
    stamped = [{**e, "input_hash": "h"} for e in events if e["type"] == "gate_result"]  # even one that says it matches
    assert reusable_passes(stamped, [cfg.gates["t"]], tree, {"t": first[0].cmd}, hashes={"t": "h"}) == {}
    assert not [e for e in events if e["type"] in ("gates_reused", "gate_cache_hit")]


def test_a_full_pass_is_reused_at_the_affected_tier(run, tmp_git, tmp_path):
    cfg = _affected(tmp_git, ["a.py"])
    tree = snapshot(tmp_git)
    first = run_gates_reusing(cfg, run, "verify/verify", ["t"], tmp_git, tmp_path / "logs", tree)
    record_results(run, "verify/verify", first, tree)
    (again,) = run_gates_reusing(cfg, run, "review/fix_findings", ["t"], tmp_git, tmp_path / "logs", tree,
                                 tier="affected", changed=["a.py"])
    assert again.reused_from == "verify/verify" and again.tier == "full"


# --------------------------------------------------------------------------- the excerpt a fix turn carries


def failed_gate(name: str, tail: str, reason: str = "exit 1", log: bool = True) -> GateResult:
    return GateResult(name, False, False, reason, Path(f"/logs/{name}.log") if log else None, tail)


def fenced(report: str) -> list[str]:
    """The lines inside the report's code fences, with the `… <k> lines cut …` markers counted out."""
    lines, inside = [], False
    for line in report.splitlines():
        if line == "```":
            inside = not inside
        elif inside and not (line.startswith("… ") and line.endswith(" lines cut …")):
            lines.append(line)
    return lines


def test_resume_report_keeps_short_output_whole():
    tail = "\n".join(["E731 Do not assign a lambda expression", "  --> a.py:3:1", *[f"   | line {i}" for i in range(12)],
                      "Found 1 error."])
    assert len(tail.splitlines()) == 15
    report = resume_report([failed_gate("py_lint", tail)], 80)
    assert fenced(report) == tail.splitlines()
    assert "lines shown" not in report and "### py_lint (exit 1)" in report and "Full log: /logs/py_lint.log" in report


def test_resume_report_keeps_failures_within_budget():
    tail = "\n".join(f"line {n}" + (" FAILED tests/test_x.py::test_y" if n == 120 else "") for n in range(1, 301))
    report = resume_report([failed_gate("engine_tests", tail)], 80)
    kept = fenced(report)
    assert len(kept) <= 80
    assert "line 120 FAILED tests/test_x.py::test_y" in kept and "line 300" in kept and "line 290" in kept
    assert f"({len(kept)} of 300 lines shown; the full log has the rest)" in report
    assert "Full log: /logs/engine_tests.log" in report and "lines cut" in report
    assert [int(line.split()[1]) for line in kept] == sorted(int(line.split()[1]) for line in kept)  # original order


def test_resume_report_without_failure_lines_keeps_the_tail():
    tail = "\n".join(f"line {n}" for n in range(1, 201))
    kept = fenced(resume_report([failed_gate("g", tail)], 80))
    assert kept == [f"line {n}" for n in range(121, 201)]


def test_resume_report_total_never_exceeds_the_budget():
    tail = "\n".join(f"line {n} error" for n in range(300))
    three = resume_report([failed_gate(f"g{i}", tail) for i in range(3)], 80)
    blocks = three.split("### ")[1:]
    assert len(blocks) == 3 and all(len(fenced(b)) <= 26 for b in blocks) and len(fenced(three)) <= 80
    ten = resume_report([failed_gate(f"g{i}", tail) for i in range(10)], 80)
    blocks = ten.split("### ")[1:]
    shown = [b for b in blocks if "```" in b]
    assert len(shown) == 8 and all(len(fenced(b)) == 10 for b in shown) and len(fenced(ten)) <= 80
    hidden = [b for b in blocks if "```" not in b]
    assert len(hidden) == 2 and all("(output not shown: read the log)" in b and "Full log:" in b for b in hidden)


def test_resume_report_cuts_long_lines():
    kept = fenced(resume_report([failed_gate("g", "x" * 5000 + "\nshort")], 80))
    assert kept == ["x" * 300 + "\u2026", "short"]


def test_resume_report_names_a_gate_without_a_log():
    report = resume_report([failed_gate("doc", "bad", reason="doc check", log=False)], 80)
    assert "### doc (doc check)\n```\nbad\n```" in report and "Full log" not in report


# --------------------------------------------------------------------------- the results table a read-only review gets


def _result(gate: str, *, passed: bool = True, skipped: bool = False, reason: str = "", tier: str | None = None,
            key: str = "build/implement", log: str | None = "/logs/x.log", duration_s: float | None = 1.4) -> dict:
    e = {"type": "gate_result", "key": key, "gate": gate, "passed": passed, "skipped": skipped, "reason": reason,
         "log": log, "duration_s": duration_s}
    if tier:
        e["tier"] = tier
    return e


def test_results_table_keeps_the_newest_result_per_gate():
    from agentflow.gates import results_table

    events = [
        _result("lint", passed=False, key="build/implement"),
        {"type": "step_started", "key": "x"},
        _result("lint", key="build/fix", log="/logs/lint2.log", duration_s=2.6),
        _result("tests", passed=True, skipped=True, reason="no tests", log=None, duration_s=None),
        _result("types", tier="affected", key="build/task-2"),
        _result("a|b", log="/l/a|b.log"),
    ]
    lines = results_table(events).splitlines()
    assert lines[0] == "The runner already ran these gates on this run's work; each row is a gate's newest result."
    assert lines[1] == ""
    assert lines[2:4] == ["| Gate | Result | Time | Step | Log |", "|---|---|---|---|---|"]
    rows = lines[4:]
    assert rows == [  # newest first, one row per gate
        r"| a\|b | pass | 1s | build/implement | /l/a\|b.log |",
        "| types | pass (affected files) | 1s | build/task-2 | /logs/x.log |",
        "| tests | skipped: no tests | — | build/implement | — |",
        "| lint | pass | 3s | build/fix | /logs/lint2.log |",
    ]


def test_results_table_marks_a_failure_and_flattens_newlines():
    from agentflow.gates import results_table

    rows = results_table([_result("lint", passed=False, key="build/a\nb")]).splitlines()[4:]
    assert rows == ["| lint | FAIL | 1s | build/a b | /logs/x.log |"]


def test_results_table_shows_a_non_finite_duration_as_a_dash():
    from agentflow.gates import results_table

    for seconds in (float("inf"), float("-inf"), float("nan")):
        rows = results_table([_result("lint", duration_s=seconds)]).splitlines()[4:]
        assert rows == ["| lint | pass | — | build/implement | /logs/x.log |"], seconds


def test_results_table_caps_its_rows():
    from agentflow.gates import GATE_TABLE_MAX_ROWS, results_table

    assert GATE_TABLE_MAX_ROWS == 20
    text = results_table([_result(f"g{i:02d}") for i in range(25)])
    lines = text.splitlines()
    rows = [line for line in lines if line.startswith("| g")]
    assert len(rows) == 20
    assert rows[0].startswith("| g24 ") and rows[-1].startswith("| g05 ")  # the newest 20
    assert lines[-1] == "(5 older gates not shown)"


def test_results_table_is_empty_without_results():
    from agentflow.gates import results_table

    assert results_table([]) == ""
    assert results_table([{"type": "step_started", "key": "x"}]) == ""
