from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from agentflow.config import Gate, load_config
from agentflow.gates import GateResult, gates_ok, run_gates, summarize
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
