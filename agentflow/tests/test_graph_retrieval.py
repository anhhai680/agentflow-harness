"""Retrieval on this repository: 30 questions that paraphrase ARCHITECTURES.md section 5.1, each with the file it describes."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from conftest import run_git

from agentflow import graph_store, pack
from agentflow.config import load_config
from agentflow.graph_build import build

RETRIEVAL = Path(__file__).parent / "fixtures" / "graph" / "retrieval.yaml"


def test_retrieval_on_this_repository(repo_root, tmp_cfg):
    if not (repo_root / ".git").exists():
        pytest.skip("not a git checkout")
    # The architecture notes are left out so a question can't match the very text it paraphrases.
    cfg = load_config(tmp_cfg({"graph": {"enabled": True, "exclude": ["ARCHITECTURES.md", "CLAUDE.md"]}}))
    head = run_git(repo_root, "rev-parse", "HEAD").strip()
    build(repo_root, cfg, download=False, commit=head)
    conn = graph_store.open_read(graph_store.path_for(pack.state_dir(repo_root)))
    cases = yaml.safe_load(RETRIEVAL.read_text(encoding="utf-8"))
    assert len(cases) == 30
    misses = []
    for case in cases:
        paths = list(dict.fromkeys(h.node.path for h in graph_store.search(conn, case["q"], limit=50)))[:5]
        if case["file"] not in paths:
            misses.append((case["q"], case["file"], paths))
    assert len(misses) <= 6, misses  # success criterion 1: at least 24 of 30
