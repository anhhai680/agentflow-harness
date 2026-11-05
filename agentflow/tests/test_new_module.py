"""A new module's row: `sources.add_module_row`, the runner's first commit, and `run --new-module`."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from agentflow import sources
from agentflow.commit_checks import foreign_commits, runner_commits
from agentflow.flow import main
from agentflow.pipeline import RunOptions, start

from conftest import AGENTFLOW, run_git

GH_BIN = AGENTFLOW / "tests" / "bin"
ROW = {"Module id": "`{module}`", "Responsibility": "{responsibility}", "Depends on": "{deps}",
       "Spec": "[SPEC-{module}](SPEC-{module}.md)"}
MAP = """# Capability map

Intro text with a | pipe that is not in the table.

| Module id | Responsibility | Depends on | Spec |
|---|---|---|---|
| `platform` | base | — | SPEC-platform |
| `crm` | crm | platform | SPEC-crm |

## Build order

1. platform
2. crm
"""


def mapfile(tmp_path: Path, text: str = MAP) -> Path:
    path = tmp_path / "map.md"
    path.write_text(text, encoding="utf-8", newline="")
    return path


# --------------------------------------------------------------------------- add_module_row


def test_the_row_goes_after_the_last_row_and_reads_back(tmp_path):
    path = mapfile(tmp_path)
    sources.add_module_row(path, ROW, {"module": "billing", "responsibility": "Thanh toán", "deps": ["platform", "crm"]})
    nodes = {n.id: n.deps for n in sources.capability_map(path)}
    assert nodes == {"platform": [], "crm": ["platform"], "billing": ["platform", "crm"]}
    text = path.read_text(encoding="utf-8")
    assert text.index("| `billing` |") > text.index("| `crm` |") and text.index("| `billing` |") < text.index("## Build order")
    assert text.endswith("1. platform\n2. crm\n") and text.startswith("# Capability map\n\nIntro text with a | pipe")
    assert "[SPEC-billing](SPEC-billing.md)" in text


def test_awkward_text_cannot_break_the_table(tmp_path):
    path = mapfile(tmp_path)
    nasty = "reads a|b tables\nand `code` with a \\ backslash |\r\n and | more"
    sources.add_module_row(path, ROW, {"module": "x", "responsibility": nasty, "deps": []})
    nodes = {n.id: n.deps for n in sources.capability_map(path)}
    assert list(nodes) == ["platform", "crm", "x"] and nodes["x"] == []
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.startswith("| `x`")]
    assert len(lines) == 1  # new lines were flattened


def test_a_column_with_no_template_is_left_empty_and_extra_templates_are_ignored(tmp_path):
    path = mapfile(tmp_path)
    sources.add_module_row(path, {"Module id": "{module}", "Depends on": "{deps}", "Nonexistent": "x"},
                           {"module": "z", "responsibility": "r", "deps": ["crm"]})
    line = next(ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.startswith("| z"))
    assert line == "| z |  | crm |  |"
    assert {n.id: n.deps for n in sources.capability_map(path)}["z"] == ["crm"]


def test_crlf_files_keep_their_line_endings(tmp_path):
    path = mapfile(tmp_path, MAP.replace("\n", "\r\n"))
    sources.add_module_row(path, ROW, {"module": "q", "responsibility": "r", "deps": []})
    raw = path.read_bytes()
    assert raw.count(b"\r\n") == raw.count(b"\n")
    assert [n.id for n in sources.capability_map(path)][-1] == "q"


def test_a_file_without_the_table_is_refused(tmp_path):
    path = mapfile(tmp_path, "# nothing here\n")
    with pytest.raises(ValueError, match="Module id"):
        sources.add_module_row(path, ROW, {"module": "q", "responsibility": "r", "deps": []})


def test_a_module_already_in_the_table_is_refused(tmp_path):
    path = mapfile(tmp_path)
    with pytest.raises(ValueError, match="crm"):
        sources.add_module_row(path, ROW, {"module": "crm", "responsibility": "r", "deps": []})


# --------------------------------------------------------------------------- the run


@pytest.fixture
def nm(flow_repo, monkeypatch):
    monkeypatch.setenv("PATH", f"{GH_BIN}{os.pathsep}{os.environ['PATH']}")
    root = flow_repo.root
    (root / "docs/specs").mkdir(parents=True, exist_ok=True)
    (root / "docs/specs/CAPABILITY-MAP.md").write_text(MAP, encoding="utf-8")
    run_git(root, "add", "-A")
    run_git(root, "commit", "-q", "-m", "map")
    return flow_repo


def test_a_new_module_run_starts_with_the_row_as_its_first_commit(nm):
    main_before = run_git(nm.root, "rev-parse", "next").strip()
    map_before = (nm.root / "docs/specs/CAPABILITY-MAP.md").read_text(encoding="utf-8")
    new = {"module": "billing", "responsibility": "Thanh toán", "deps": ["platform"]}
    run = start(nm.cfg, nm.root, "feature", {"module": "billing"}, RunOptions(new_module=new, to_phase="define"))
    worktree = Path(run.state["worktree"])
    log = run_git(worktree, "log", "--format=%s", f"{main_before}..HEAD").splitlines()
    assert log == ["Add module billing to the capability map"]
    assert "| `billing` |" in (worktree / "docs/specs/CAPABILITY-MAP.md").read_text(encoding="utf-8")
    assert [e for e in run.events.read() if e["type"] == "module_added"][0]["module"] == "billing"
    assert foreign_commits(run, nm.cfg, worktree, main_before) == []  # the push check accepts it
    assert len(runner_commits(run)) == 1
    # the base branch and the main checkout never change
    assert run_git(nm.root, "rev-parse", "next").strip() == main_before
    assert (nm.root / "docs/specs/CAPABILITY-MAP.md").read_text(encoding="utf-8") == map_before
    assert "Co-Authored-By" in run_git(worktree, "log", "-1", "--format=%B")


def test_the_new_module_must_match_the_run_and_the_config_must_have_a_row(nm, tmp_cfg):
    from agentflow.config import load_config

    with pytest.raises(ValueError, match="billing"):
        start(nm.cfg, nm.root, "feature", {"module": "other"},
              RunOptions(new_module={"module": "billing", "responsibility": "r", "deps": []}))
    bare = load_config(tmp_cfg(lambda d: d["dashboard"]["backlog"].pop("module_row"), name="norow.yaml"))
    with pytest.raises(ValueError, match="module_row"):
        start(bare, nm.root, "feature", {"module": "billing"},
              RunOptions(new_module={"module": "billing", "responsibility": "r", "deps": []}))


# --------------------------------------------------------------------------- the CLI flag


@pytest.mark.parametrize("value", [
    "not json", "[]", '{"module": "x"}', '{"module": "x", "responsibility": "r", "deps": "a"}',
    '{"module": "--evil", "responsibility": "r", "deps": []}', '{"module": "x", "responsibility": "r", "deps": ["a b"]}',
    '{"module": "x", "responsibility": "r", "deps": [], "extra": 1}',
])
def test_new_module_flag_must_be_exactly_the_expected_shape(nm, capsys, value):
    assert main(["run", "feature", "--module=x", f"--new-module={value}", "--dry-run"]) == 1
    assert "--new-module" in capsys.readouterr().out


def test_new_module_flag_is_accepted_when_well_formed(nm, capsys):
    value = json.dumps({"module": "x", "responsibility": "r", "deps": ["platform"]})
    assert main(["run", "feature", "--module=x", f"--new-module={value}", "--dry-run"]) == 0
