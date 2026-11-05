"""The engine runs from this repository's layout: `agentflow/`, `bin/` and `hooks/` at the root."""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_repo_fixture_is_this_checkout(repo_root):
    assert (repo_root / "agentflow" / "flow.py").is_file()
    assert (repo_root / "bin" / "agentflow").is_file()


def test_flow_runs_as_a_script_from_any_cwd(tmp_path):
    proc = subprocess.run([sys.executable, str(ROOT / "agentflow" / "flow.py"), "--help"],
                          cwd=tmp_path, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
