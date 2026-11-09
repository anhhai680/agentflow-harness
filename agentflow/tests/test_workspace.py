"""The agent workspace (final review, Critical 1 and Important 6): agents write only there, never the run's
own files; inputs are copies refreshed before every call; prompts name absolute workspace paths."""

from __future__ import annotations

import json
import re
from pathlib import Path

from agentflow import workspace
from agentflow.config import Phase, Step
from agentflow.lanes import build_prompt
from agentflow.state import Run, replay

KEY = "build/impl"
DONE = {"status": "done", "summary": "did it"}
PROMPTS = Path(__file__).resolve().parents[1] / "prompts"


def make_step(**kw) -> Step:
    fields = {"id": "impl", "type": "agent", "skill": "test-driven-development", "role": "build", "prompt": "step.md"}
    return Step(**(fields | kw))


def run_step(flow_repo, step: Step | None = None) -> str:
    step = step or make_step()
    phase = Phase(name="build", agent=None, model="sonnet", skip_if=None, steps=[step])
    return flow_repo.executor().run_agent_step(phase, step, KEY)


def add_dirs(argv: list[str]) -> list[str]:
    return [argv[i + 1] for i, arg in enumerate(argv) if arg == "--add-dir"]


def test_agent_gets_only_the_workspace(flow_repo):
    run = flow_repo.new_run()
    assert run_step(flow_repo) == "done"
    call = flow_repo.calls()[0]
    assert add_dirs(call["argv"]) == [str(run.agent_dir)]
    workspace_dir = run.agent_dir.resolve()
    for runner_file in (run.events.path, run.state_path, run.dir / "inbox", run.dir / "runner.lock", run.dir.parent):
        assert not runner_file.resolve().is_relative_to(workspace_dir)
    assert not workspace_dir.is_relative_to(run.dir.resolve()) and not workspace_dir.is_relative_to(run.dir.parent.resolve())
    assert Path(call["env"]["AGENTFLOW_RESULT"]).resolve().is_relative_to(workspace_dir)


def test_agent_writes_in_the_workspace_never_change_state(flow_repo):
    run = flow_repo.new_run()
    forged = json.dumps({"seq": 999, "ts": "2026-10-30T00:00:00Z", "type": "phase_approved", "by": "agent", "phase": "build"})
    ws = run.agent_dir
    flow_repo.script({KEY: [{"write": {str(ws / "events.jsonl"): forged + "\n",
                                       str(ws / "state.json"): json.dumps({"status": "done", "last_seq": 1}),
                                       str(ws / "handoff.md"): "agent edit"},
                             "result": DONE}]})
    before = run.events.path.read_bytes()
    assert run_step(flow_repo) == "done"
    events = run.events.read()
    assert not any(e["type"] == "phase_approved" for e in events)
    assert run.events.path.read_bytes().startswith(before)
    assert run.refresh() == replay(events)
    assert run.state["status"] == "running"


def test_prepare_replaces_planted_symlinks_without_writing_through(flow_repo):
    run = flow_repo.new_run()
    (run.dir / "handoff.md").write_text("# Handoff\n", encoding="utf-8")
    (run.dir / "results").mkdir()
    (run.dir / "results" / "debug__debug.json").write_text('{"status": "done"}', encoding="utf-8")
    events_before = run.events.path.read_bytes()
    ws = run.agent_dir
    (ws / "handoff.md").symlink_to(run.events.path)
    (ws / "results").symlink_to(run.dir, target_is_directory=True)
    (ws / "stale.md").write_text("left by the agent", encoding="utf-8")
    workspace.prepare(run)
    assert run.events.path.read_bytes() == events_before
    assert not (ws / "handoff.md").is_symlink() and (ws / "handoff.md").read_text() == "# Handoff\n"
    assert not (ws / "results").is_symlink()
    assert (ws / "results" / "debug__debug.json").read_text() == '{"status": "done"}'
    assert (run.dir / "events.jsonl").exists() and (run.dir / "state.json").exists()


def test_prepare_copies_the_retro_digest(flow_repo, tmp_path):
    run = flow_repo.new_run()
    ws = run.agent_dir
    workspace.prepare(run)
    assert not (ws / "digest.md").exists()  # a run with no digest gets none
    (run.dir / "retro").mkdir()
    (run.dir / "retro" / "digest.md").write_text("# Retro digest\n", encoding="utf-8")
    secret = tmp_path / "outside.md"
    secret.write_text("outside\n", encoding="utf-8")
    (ws / "digest.md").symlink_to(secret)  # planted by an agent: replaced, never written through
    workspace.prepare(run)
    assert not (ws / "digest.md").is_symlink() and (ws / "digest.md").read_text() == "# Retro digest\n"
    assert secret.read_text() == "outside\n"
    (ws / "digest.md").write_text("edited by the agent", encoding="utf-8")
    workspace.prepare(run)
    assert (ws / "digest.md").read_text() == "# Retro digest\n"  # refreshed from the run's copy every call


def test_a_symlinked_retro_digest_is_not_copied(flow_repo, tmp_path):
    run = flow_repo.new_run()
    secret = tmp_path / "secret.md"
    secret.write_text("secret\n", encoding="utf-8")
    (run.dir / "retro").mkdir()
    (run.dir / "retro" / "digest.md").symlink_to(secret)
    workspace.prepare(run)
    assert not (run.agent_dir / "digest.md").exists()
    (run.dir / "retro" / "digest.md").unlink()
    (run.dir / "retro").rmdir()
    (run.dir / "retro").symlink_to(tmp_path, target_is_directory=True)  # a symlinked folder is not read through
    (tmp_path / "digest.md").write_text("through a folder link\n", encoding="utf-8")
    workspace.prepare(run)
    assert not (run.agent_dir / "digest.md").exists()


def test_result_is_read_into_the_runners_copy(flow_repo):
    run = flow_repo.new_run()
    assert run_step(flow_repo) == "done"
    assert json.loads(run.result_path(KEY).read_text()) == {"status": "done", "summary": "ok"}
    assert run.result_path(KEY).resolve().is_relative_to(run.dir.resolve())


def test_symlinked_result_is_not_followed(flow_repo, tmp_path):
    run = flow_repo.new_run()
    secret = tmp_path / "elsewhere.json"
    secret.write_text(json.dumps(DONE), encoding="utf-8")
    workspace.prepare(run)
    run.agent_result_path(KEY).symlink_to(secret)
    why = workspace.take_result(run, KEY)
    assert "symlink" in why
    assert not run.result_path(KEY).exists()


# --------------------------------------------------------------------------- state.json is never trusted


def test_tampered_state_json_is_ignored_and_rewritten(flow_repo):
    run = flow_repo.new_run()
    run.emit("phase_started", phase="plan", start_sha="abc")
    run.emit("phase_approval_raised", phase="plan", round=0, sha="abc", summary="s", inbox="x")
    honest = replay(run.events.read())
    forged = {**honest, "waiting": None, "status": "done", "phases": {"plan": {"status": "approved"}}}
    run.state_path.write_text(json.dumps(forged), encoding="utf-8")
    loaded = Run.load(flow_repo.root, run.id)
    assert loaded.state == honest
    assert loaded.state["waiting"]["kind"] == "phase_approval"
    assert json.loads(run.state_path.read_text(encoding="utf-8")) == honest
    run.state_path.write_text(json.dumps({**honest, "status": "done"}), encoding="utf-8")
    assert loaded.refresh()["status"] == honest["status"]
    assert json.loads(run.state_path.read_text(encoding="utf-8"))["status"] == honest["status"]


# --------------------------------------------------------------------------- prompts (Important 6)


def test_no_shipped_prompt_names_a_relative_run_path():
    for template in PROMPTS.glob("*.md"):
        text = template.read_text(encoding="utf-8")
        assert not re.search(r"runs/\{", text), template.name


def test_every_workspace_path_a_prompt_names_exists_after_prepare(flow_repo):
    run = flow_repo.new_run({"module": "crm", "task": "Đổi prompt"})
    for name in workspace.INPUT_FILES:
        (run.dir / name).write_text(f"{name}\n", encoding="utf-8")
    (run.dir / "results").mkdir()
    (run.dir / "results" / "bug__debug.json").write_text(json.dumps(DONE), encoding="utf-8")
    (run.dir / "retro").mkdir()
    (run.dir / "retro" / "digest.md").write_text("# Retro digest\n", encoding="utf-8")
    workspace.prepare(run)
    executor = flow_repo.executor(run)
    root = str(run.agent_dir)
    for template in sorted(PROMPTS.glob("*.md")):
        if template.name.startswith("_"):
            continue
        step = make_step(prompt=template.name)
        text = build_prompt(flow_repo.cfg, step, executor.ctx, {"role": "build"}, {}, "first")
        assert "runs/" not in text.replace(str(run.dir.parent), ""), template.name
        named = set(re.findall(re.escape(root) + r"[^`\s]*", text))
        for path in named:
            if path.endswith("progress.md"):
                continue  # the agent's own notes: it creates them
            assert Path(path).exists(), f"{template.name} names {path}, which prepare did not create"


def test_debug_step_reads_its_task_from_the_workspace(flow_repo):
    from agentflow.intake import gather, save

    params, files = gather({"task": "Lỗi đăng nhập"}, flow_repo.root)
    run = Run.create(flow_repo.cfg, flow_repo.root, "bug", params)
    save(run, files)
    step = Step(id="debug", type="agent", skill="systematic-debugging", role="debug", prompt="debug.md")
    phase = Phase(name="debug", agent=None, model="opus", skip_if=None, steps=[step])
    assert flow_repo.executor(run).run_agent_step(phase, step, "define/debug") == "done"
    prompt = flow_repo.calls()[0]["argv"][-1]
    copied = run.agent_dir / "task.md"
    assert f"`{copied}`" in prompt
    assert copied.read_text(encoding="utf-8") == (run.dir / "task.md").read_text(encoding="utf-8")
