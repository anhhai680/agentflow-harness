"""The engine installs as a tool: pyproject.toml declares the `agentflow` console script, the wheel holds every engine
file and no tests, and the installed script runs from outside this checkout."""

from __future__ import annotations

import os
import shutil
import subprocess
import tomllib
import zipfile
from pathlib import Path

import pytest

from agentflow import pack
from conftest import ROOT

# A build or install whose stderr holds one of these could not reach the package index: the two build tests skip.
# Any other failure fails the test. Copied from what uv 0.5.9 prints with `--offline`, with an unreachable index
# (connection refused) and with an unresolvable host; see the task report.
OFFLINE_MARKERS = (
    "the network was disabled",
    "Could not connect, are you offline?",
    "Connection refused",
    "dns error",
    "failed to lookup address information",
    "Temporary failure in name resolution",
)


def _engine_files() -> list[str]:
    """Every engine file git knows (tracked, or untracked and not ignored) under `agentflow/`, minus the tests and
    the checkout's own `.gitignore`."""
    out = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "agentflow"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout
    return sorted(p for p in out.splitlines()
                  if p and not p.startswith("agentflow/tests/") and p != "agentflow/.gitignore")


def _uv_or_skip() -> str:
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is not installed")
    return uv


def _run_or_skip(cmd: list[str], env: dict[str, str] | None = None) -> None:
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, env={**os.environ, **(env or {})})
    if proc.returncode == 0:
        return
    for marker in OFFLINE_MARKERS:
        if marker in proc.stderr:
            pytest.skip(f"offline: uv stderr contains {marker!r}")
    pytest.fail(f"{' '.join(cmd)} failed ({proc.returncode}):\n{proc.stderr}")


def _build_wheel_or_skip(out_dir: Path) -> Path:
    _run_or_skip([_uv_or_skip(), "build", "--wheel", "-o", str(out_dir)])
    wheels = list(out_dir.glob("agentflow-*.whl"))
    assert len(wheels) == 1, wheels
    return wheels[0]


def _tool_install_or_skip(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "tool-bin"
    env = {"UV_TOOL_DIR": str(tmp_path / "tools"), "UV_TOOL_BIN_DIR": str(bin_dir)}
    _run_or_skip([_uv_or_skip(), "tool", "install", "--reinstall", "--from", str(ROOT), "agentflow"], env)
    return bin_dir


def test_pyproject_declares_the_console_script_and_only_pyyaml():
    meta = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert meta["project"]["scripts"] == {"agentflow": "agentflow.flow:main"}
    assert meta["project"]["dependencies"] == ["pyyaml>=6"]
    assert meta["project"]["requires-python"] == ">=3.12"


def test_pyproject_version_is_the_engine_version():
    assert tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"] == pack.ENGINE_VERSION


def test_changelog_has_an_entry_for_the_current_version():
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "## [Unreleased]" in text
    assert f"## [{pack.ENGINE_VERSION}]" in text


def test_wheel_holds_every_engine_file_and_no_tests(tmp_path):
    wheel = _build_wheel_or_skip(tmp_path)
    names = set(zipfile.ZipFile(wheel).namelist())
    assert set(_engine_files()) <= names
    assert not any(n.startswith("agentflow/tests/") for n in names)
    assert "agentflow/.gitignore" not in names


def test_wheel_excludes_the_checkouts_gitignore():
    exclude = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["hatch"]["build"]["targets"]["wheel"]["exclude"]
    assert "agentflow/.gitignore" in exclude


def test_installed_tool_runs_outside_this_checkout(tmp_path):
    bin_dir = _tool_install_or_skip(tmp_path)
    proc = subprocess.run([str(bin_dir / "agentflow"), "--help"], cwd=tmp_path, capture_output=True, text=True)
    assert proc.returncode == 0 and "validate" in proc.stdout
