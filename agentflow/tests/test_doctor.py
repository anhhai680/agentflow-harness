"""`agentflow doctor`: the tools and logins a run needs, each reported as ok, warn or fail with a fix."""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

from agentflow import doctor, flow
from agentflow.config import load_config
from agentflow.doctor import Check, gate_programs, render, run_checks

ENGINE_BIN = Path(__file__).resolve().parent / "bin"
SYSTEM_PATH = os.environ.get("PATH", "")


def _script(path: Path, body: str) -> Path:
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


@pytest.fixture
def bindir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A directory first on PATH holding a fake claude, codex, gh (the tests' own), docker and a gate tool."""
    bins = tmp_path / "fakebin"
    bins.mkdir()
    for name in ("claude", "codex"):
        _script(bins / name, 'if [ -n "$FAKE_LOGGED_OUT" ]; then echo "not logged in" >&2; exit 1; fi\nexit 0\n')
    _script(bins / "docker", 'if [ -n "$FAKE_DOCKER_DOWN" ]; then exit 1; fi\nexit 0\n')
    _script(bins / "buildtool", "exit 0\n")
    (bins / "gh").symlink_to(ENGINE_BIN / "gh")
    monkeypatch.setenv("PATH", f"{bins}{os.pathsep}{SYSTEM_PATH}")
    return bins


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path, bindir: Path) -> Path:
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "remote", "add", "origin", "https://example.invalid/r.git")
    return tmp_path


def _patch(**overrides):
    """A tmp_cfg patch: one enabled agent (claude), the gates `buildtool --x`, and `overrides` applied last."""

    def apply(data: dict) -> None:
        data["agents"] = {"claude": {**data["agents"]["claude"], "enabled": True, "command": ["claude"]}}
        data["gates"] = {"build": {"run": "buildtool --x"}}
        data["gate_sets"] = {"verify": ["build"], "build": ["build"]}
        for key, value in overrides.items():
            data[key] = value

    return apply


def _checks(tmp_cfg, repo: Path, patch=None) -> dict[str, Check]:
    cfg = load_config(tmp_cfg(patch or _patch()))
    return {c.name: c for c in run_checks(cfg, repo)}


def test_doctor_is_all_ok_with_every_tool_present_and_logged_in(tmp_cfg, repo):
    checks = _checks(tmp_cfg, repo)
    assert {n: c.status for n, c in checks.items()} == {
        "agent:claude": "ok", "gh": "ok", "remote": "ok", "state dir": "ok", "inherited environment": "ok",
        "gate:build": "ok"}
    assert "network" in checks["gh"].detail


def test_a_missing_agent_cli_fails_with_an_install_hint(tmp_cfg, repo):
    def patch(data):
        _patch()(data)
        data["agents"]["claude"]["command"] = ["nosuchagent-cli"]

    check = _checks(tmp_cfg, repo, patch)["agent:claude"]
    assert check.status == "fail"
    assert "install `nosuchagent-cli`" in check.fix and "agents.claude.enabled: false" in check.fix


def test_a_logged_out_agent_fails_with_a_login_hint(tmp_cfg, repo, monkeypatch):
    monkeypatch.setenv("FAKE_LOGGED_OUT", "1")
    check = _checks(tmp_cfg, repo)["agent:claude"]
    assert check.status == "fail" and "claude auth login" in check.fix


def test_a_logged_out_codex_names_codex_login(tmp_cfg, repo, monkeypatch):
    monkeypatch.setenv("FAKE_LOGGED_OUT", "1")

    def patch(data):
        _patch()(data)
        data["agents"]["codex"] = {**data["agents"]["claude"], "command": ["codex"], "enabled": True}

    checks = _checks(tmp_cfg, repo, patch)
    assert checks["agent:codex"].status == "fail" and "codex login" in checks["agent:codex"].fix


def test_an_agent_that_is_not_claude_or_codex_warns_that_login_is_not_checked(tmp_cfg, repo, bindir):
    _script(bindir / "myagent", "exit 7\n")

    def patch(data):
        _patch()(data)
        data["agents"]["claude"]["command"] = ["myagent"]

    check = _checks(tmp_cfg, repo, patch)["agent:claude"]
    assert check.status == "warn" and "login not checked" in check.detail


def test_an_agent_command_that_is_a_path_is_never_executed(tmp_cfg, repo, tmp_path):
    marker = tmp_path / "ran"
    _script(tmp_path / "evil", f"touch {marker}\n")

    def patch(data):
        _patch()(data)
        data["agents"]["claude"]["command"] = ["./evil"]

    check = _checks(tmp_cfg, repo, patch)["agent:claude"]
    assert check.status == "fail" and not marker.exists()
    absolute = _checks(tmp_cfg, repo, lambda d: (_patch()(d), d["agents"]["claude"].update(command=[str(tmp_path / "evil")])))
    assert absolute["agent:claude"].status == "fail" and not marker.exists()


def test_a_disabled_agent_is_not_checked(tmp_cfg, repo):
    def patch(data):
        _patch()(data)
        data["agents"]["codex"] = {**data["agents"]["claude"], "command": ["nosuchagent-cli"], "enabled": False}

    assert "agent:codex" not in _checks(tmp_cfg, repo, patch)


def test_gh_logged_out_fails(tmp_cfg, repo, monkeypatch):
    monkeypatch.setenv("FAKE_GH_FAIL", "1")
    check = _checks(tmp_cfg, repo)["gh"]
    assert check.status == "fail" and "gh auth login" in check.fix


def test_a_missing_gh_fails_with_an_install_hint(tmp_cfg, repo, bindir):
    (bindir / "gh").unlink()
    stripped = [p for p in SYSTEM_PATH.split(os.pathsep) if not (Path(p) / "gh").exists()]
    os.environ["PATH"] = os.pathsep.join([str(bindir), *stripped])
    check = _checks(tmp_cfg, repo)["gh"]
    assert check.status == "fail" and "install gh" in check.fix


def test_no_origin_remote_fails(tmp_cfg, repo):
    _git(repo, "remote", "remove", "origin")
    check = _checks(tmp_cfg, repo)["remote"]
    assert check.status == "fail" and "origin" in check.fix


def test_docker_is_checked_only_when_a_gate_runs_it(tmp_cfg, repo, monkeypatch):
    monkeypatch.setenv("FAKE_DOCKER_DOWN", "1")
    assert "docker" not in _checks(tmp_cfg, repo, _patch(gates={"build": {"run": "echo docker"}}))
    checks = _checks(tmp_cfg, repo, _patch(gates={"build": {"run": "docker compose up -d && buildtool"}}))
    assert checks["docker"].status == "fail" and "start Docker" in checks["docker"].fix
    monkeypatch.delenv("FAKE_DOCKER_DOWN")
    ok = _checks(tmp_cfg, repo, _patch(gates={"build": {"run": "docker compose up -d && buildtool"}}))
    assert ok["docker"].status == "ok"


def test_an_unwritable_state_dir_fails(tmp_cfg, repo, tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("a file, not a directory", encoding="utf-8")
    check = _checks(tmp_cfg, repo, _patch(paths={"state_dir": str(blocker / "state")}))["state dir"]
    assert check.status == "fail"


@pytest.mark.parametrize("command,programs", [
    ("FOO=1 nosuchtool --x", ["nosuchtool"]), ("cd web && npm test", ["npm"]),
    ("python -c \"import x; y\"", ["python"]), ("set -e\n# note\npytest -q", ["pytest"]),
    ("${PYTHON:-python} -m pytest", []), ("pytest | tee out", ["pytest", "tee"]),
    ("env FOO=1 pytest", []), ("true", []), ("(cd a; make) && ./run.sh", ["make", "./run.sh"])])
def test_gate_programs(command, programs):
    assert gate_programs(command) == programs


def test_gate_programs_on_an_unbalanced_quote_raises_value_error():
    with pytest.raises(ValueError):
        gate_programs("echo 'unterminated")


def test_a_missing_gate_program_fails_without_setup_and_warns_with_setup(tmp_cfg, repo):
    gates = {"build": {"run": "nosuchtool --x"}, "setup_it": {"run": "true"}}
    failed = _checks(tmp_cfg, repo, _patch(gates=gates))["gate:build"]
    assert failed.status == "fail" and "install `nosuchtool`" in failed.fix

    with_setup = _checks(tmp_cfg, repo, _patch(gates=gates, gate_sets={
        "verify": ["build"], "build": ["build"], "setup": ["setup_it"]}))
    assert with_setup["gate:build"].status == "warn" and "gate_sets.setup" in with_setup["gate:build"].detail


def test_a_relative_program_and_an_unparseable_gate_are_warnings(tmp_cfg, repo):
    gates = {"build": {"run": "./gradlew test"}, "odd": {"run": "echo 'unterminated"}}
    checks = _checks(tmp_cfg, repo, _patch(gates=gates, gate_sets={"verify": ["build", "odd"]}))
    assert checks["gate:build"].status == "warn" and "created by setup" in checks["gate:build"].detail
    assert checks["gate:odd"].status == "warn"


def test_gate_programs_are_looked_up_on_defaults_env_path(tmp_cfg, repo, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    _script(other / "specialtool", "exit 0\n")
    gates = {"build": {"run": "specialtool"}}
    assert _checks(tmp_cfg, repo, _patch(gates=gates))["gate:build"].status == "fail"

    def patch(data):
        _patch(gates=gates)(data)
        data["defaults"]["env"]["PATH"] = f"{other}{os.pathsep}{SYSTEM_PATH}"

    assert _checks(tmp_cfg, repo, patch)["gate:build"].status == "ok"


def test_a_broken_user_file_is_a_failed_check_not_a_crash(tmp_cfg, repo, tmp_path, capsys, monkeypatch):
    user = tmp_path / "xdg-config" / "agentflow" / "config.yaml"
    user.parent.mkdir(parents=True)
    user.write_text("defaults: [not, a, mapping\n", encoding="utf-8")
    monkeypatch.chdir(repo)
    path = tmp_cfg(_patch())
    assert flow.main(["doctor", "--config", str(path)]) == 1
    out = capsys.readouterr().out
    assert "user config" in out and str(user) in out and "fail" in out
    assert "agent:claude" in out  # it went on with the user file left out


def test_a_check_that_hangs_times_out(tmp_cfg, repo, bindir, monkeypatch):
    _script(bindir / "claude", "exec sleep 30\n")
    monkeypatch.setattr(doctor, "TIMEOUT_S", 0.5)
    check = _checks(tmp_cfg, repo)["agent:claude"]
    assert check.status == "fail" and "timed out" in check.detail


def test_render_lists_each_check_with_its_fix_and_a_summary():
    text = render([Check("a", "ok", "fine"), Check("b", "warn", "hmm"), Check("c", "fail", "broken", "do this")])
    assert "ok" in text and "do this" in text and "1 failed" in text and "1 warning" in text


def test_cli_doctor_exits_1_on_any_failure_and_0_otherwise(tmp_cfg, repo, monkeypatch, capsys):
    monkeypatch.chdir(repo)
    path = tmp_cfg(_patch())
    assert flow.main(["doctor", "--config", str(path)]) == 0
    assert "agent:claude" in capsys.readouterr().out
    monkeypatch.setenv("FAKE_GH_FAIL", "1")
    assert flow.main(["doctor", "--config", str(path)]) == 1
    assert "gh auth login" in capsys.readouterr().out


def test_inherited_environment_is_ok_when_nothing_is_dropped(tmp_cfg, repo):
    check = _checks(tmp_cfg, repo)["inherited environment"]
    assert check.status == "ok" and "nothing" in check.detail


def test_inherited_environment_warns_with_names_and_the_file_never_values(tmp_cfg, repo, monkeypatch):
    (repo / ".env").write_text("X_URL=leak-sentinel-7f3a\nY_KEY=leak-sentinel-7f3a\n", encoding="utf-8")
    monkeypatch.setenv("X_URL", "leak-sentinel-7f3a")
    monkeypatch.setenv("Y_KEY", "leak-sentinel-7f3a")
    check = _checks(tmp_cfg, repo)["inherited environment"]
    assert check.status == "warn"
    assert "X_URL" in check.detail and "Y_KEY" in check.detail and ".env" in check.detail
    assert "leak-sentinel-7f3a" not in check.detail + check.fix
    assert "dropped from runs" in check.fix and "defaults.env" in check.fix


def test_inherited_environment_names_the_drop_pattern(tmp_cfg, repo, monkeypatch):
    monkeypatch.setenv("APP_DATABASE_URL", "leak-sentinel-7f3a")
    patch = _patch()

    def both(data):
        patch(data)
        data["defaults"] = {**data.get("defaults", {}), "env_inherit": {"drop": ["*_DATABASE_URL"], "drop_dotenv": []}}

    check = _checks(tmp_cfg, repo, both)["inherited environment"]
    assert check.status == "warn" and "APP_DATABASE_URL" in check.detail and "*_DATABASE_URL" in check.detail
    assert "leak-sentinel-7f3a" not in check.detail


def test_inherited_environment_lists_a_long_set_briefly(tmp_cfg, repo, monkeypatch):
    for i in range(60):
        monkeypatch.setenv(f"LEAK_{i:02d}", "v")
    patch = _patch()

    def both(data):
        patch(data)
        data["defaults"] = {**data.get("defaults", {}), "env_inherit": {"drop": ["LEAK_*"], "drop_dotenv": []}}

    check = _checks(tmp_cfg, repo, both)["inherited environment"]
    assert check.status == "warn" and "more" in check.detail and len(check.detail) < 600
