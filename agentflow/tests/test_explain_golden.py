"""`agentflow explain` for every lane of the `python` pack, pinned byte for byte.

The files in `fixtures/explain/` were written once with `AGENTFLOW_WRITE_GOLDEN=1` and never change: a diff here
means the engine walks the same pack differently."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest
import yaml

from conftest import ENGINE, PYTHON_CONFIG

GOLDEN = ENGINE / "tests" / "fixtures" / "explain"
LANES = yaml.safe_load(PYTHON_CONFIG.read_text(encoding="utf-8"))["lanes"]


@pytest.mark.parametrize("lane", LANES)
def test_explain_matches_the_golden_file(lane, pack_repo_python, tmp_path):
    env = {**os.environ, "XDG_STATE_HOME": str(tmp_path / "xdg")}
    proc = subprocess.run(
        [sys.executable, str(ENGINE / "flow.py"), "explain", lane, "--module", "greet", "--topic", "x", "--issue", "1"],
        cwd=pack_repo_python, env=env, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    golden = GOLDEN / f"{lane}.txt"
    if os.environ.get("AGENTFLOW_WRITE_GOLDEN") == "1":
        golden.parent.mkdir(parents=True, exist_ok=True)
        golden.write_text(proc.stdout, encoding="utf-8")
        pytest.skip(f"wrote {golden.name}")
    assert proc.stdout == golden.read_text(encoding="utf-8")
