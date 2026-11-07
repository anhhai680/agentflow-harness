from __future__ import annotations

import dataclasses
import os
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
