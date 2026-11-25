"""Backlog file B2: the runner's own writes to `backlog.yml`, and what a run may and may not do to it."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import run_git

from agentflow.config import load_config
from agentflow.pipeline import RunOptions, advance, start

BACKLOG_YML = """version: 1
items:
  - {id: a, title: Alpha, status: done}
  - {id: b, title: Bee, status: ready}
"""


class BacklogFlow:
    """`flow_repo` as a `backlog` pack with a committed `backlog.yml` (item `b` is ready, item `a` is done)."""

    def __init__(self, flow_repo) -> None:
        self.repo = flow_repo
        flow_repo.configure({"work_source": {"kind": "backlog", "path": None}})
        (flow_repo.root / "backlog.yml").write_text(BACKLOG_YML, encoding="utf-8")
        run_git(flow_repo.root, "add", "-A")
        run_git(flow_repo.root, "commit", "-q", "-m", "backlog")

    @property
    def cfg(self):
        return self.repo.cfg

    def script(self, **acts: list[dict]) -> None:
        """Script the fake agent, one list of acts per step; `define` stands for the `define/draft` step."""
        self.repo.script({"define/draft" if key == "define" else key: value for key, value in acts.items()})

    def start(self, pipeline: str, module: str | None = None, **options):
        """Start a run and advance it until a human is needed or it ends; returns the run."""
        params = {"module": module} if module else {}
        run = start(self.cfg, self.repo.root, pipeline, params, RunOptions(**options))
        advance(run, self.cfg, RunOptions(**options))
        return run

    def worktree(self, run):
        return Path(run.refresh()["worktree"])

    def events(self, run, etype: str) -> list[dict]:
        return self.repo.events(run, etype)


@pytest.fixture
def bflow(flow_repo) -> BacklogFlow:
    return BacklogFlow(flow_repo)


def test_a_backlog_pack_forbids_its_file_in_a_run(tmp_cfg):
    cfg = load_config(tmp_cfg({"work_source": {"kind": "backlog", "path": "./backlog.yml"}}))
    assert cfg.guardrails.forbidden_paths[-1] == "backlog.yml" and ".agentflow/**" in cfg.guardrails.forbidden_paths


def test_other_kinds_do_not(tmp_cfg):
    assert "backlog.yml" not in load_config(tmp_cfg({})).guardrails.forbidden_paths


def test_an_agent_step_that_edits_the_file_is_a_guard_violation(bflow):
    # `write` is the fake's own hand on the tree, with no Edit tool call: what a shell write looks like to the guard
    bflow.script(define=[{"write": {"backlog.yml": BACKLOG_YML + "# x\n"}}])
    run = bflow.start("feature", module="b")
    waiting = run.refresh()["waiting"]
    assert waiting["kind"] == "guard_violation" and waiting["key"] == "define/draft"
    assert "forbidden_path: backlog.yml" in waiting["summary"]
    assert run_git(bflow.worktree(run), "log", "--format=%s", f"{run.state['base_sha']}..HEAD") == ""

