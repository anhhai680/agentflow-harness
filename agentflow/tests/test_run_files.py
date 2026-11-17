"""`run_files`: a run's own documents listed and read by a fixed allow-list of names, from the head."""

from __future__ import annotations

import pytest

from agentflow.artifacts import NotFound
from agentflow.run_files import list_files, read_file


def test_every_document_of_the_run_is_listed_by_group(flow_repo):
    run = flow_repo.new_run()
    for name, text in {"issue.md": "# Ask\n", "pr.md": "body\n", "handoff.md": "all\n",
                       "handoffs/define.md": "d\n", "handoffs/build.md": "b\n", "refine/plan-1.md": "r\n"}.items():
        (run.dir / name).parent.mkdir(parents=True, exist_ok=True)
        (run.dir / name).write_text(text, encoding="utf-8")
    run.emit("phase_approved", phase="define"); run.emit("phase_approved", phase="build")
    assert [(f["group"], f["name"]) for f in list_files(run)] == [
        ("request", "issue.md"), ("handoffs", "handoffs/define.md"), ("handoffs", "handoffs/build.md"),
        ("refine", "refine/plan-1.md"), ("pull request", "pr.md"), ("notes", "handoff.md")]


def test_a_phase_handoff_reads_in_full_past_the_old_16_kb_tail(flow_repo):   # spec Verification U2
    run = flow_repo.new_run()
    (run.dir / "handoffs").mkdir()
    text = "## build\n" + "line of the build handoff\n" * 1000          # about 26 KB
    (run.dir / "handoffs" / "build.md").write_text(text, encoding="utf-8")
    got = read_file(run, "handoffs/build.md", 512 * 1024)
    assert got["text"] == text and got["truncated"] is False


@pytest.mark.parametrize("name", ["handoffs/../state.json", "refine/x-1.md/..", "HANDOFF.md", "events.jsonl",
                                  "../events.jsonl", "handoffs/a/b.md", "refine/plan.md", "/etc/passwd", "",
                                  "refine/plan-\u0663.md"])
def test_names_outside_the_allow_list_are_not_found(flow_repo, name):           # Review Focus 4
    run = flow_repo.new_run()
    (run.dir / "refine").mkdir(exist_ok=True)
    (run.dir / "refine" / "plan-\u0663.md").write_text("x\n", encoding="utf-8")  # it exists: only the name refuses it
    with pytest.raises(NotFound):
        read_file(run, name, 1024)


def test_symlinks_are_never_followed(flow_repo, tmp_path):                      # Review Focus 4
    run = flow_repo.new_run()
    (tmp_path / "secret.md").write_text("secret\n", encoding="utf-8")
    (run.dir / "pr.md").symlink_to(tmp_path / "secret.md")
    (tmp_path / "hand").mkdir(); (tmp_path / "hand" / "build.md").write_text("x\n", encoding="utf-8")
    (run.dir / "handoffs").symlink_to(tmp_path / "hand", target_is_directory=True)
    for name in ("pr.md", "handoffs/build.md"):
        with pytest.raises(NotFound):
            read_file(run, name, 1024)
    assert not [f for f in list_files(run) if f["name"] in ("pr.md", "handoffs/build.md")]


def test_progress_comes_from_the_agent_workspace_and_a_big_file_is_cut(flow_repo):
    run = flow_repo.new_run()
    run.agent_dir.mkdir(parents=True, exist_ok=True)
    (run.agent_dir / "progress.md").write_text("x" * 39 + "\n" * 1 + ("y" * 39 + "\n") * 2000, encoding="utf-8")
    got = read_file(run, "progress.md", 16 * 1024)
    assert got["truncated"] is True and got["text"].endswith("\n") and len(got["text"].encode()) <= 16 * 1024
