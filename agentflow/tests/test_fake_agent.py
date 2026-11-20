"""The fake agent's permission model (`tests/fake_agent.py`): close to the real CLI, so tests built on it encode it."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from agentflow.agents import AgentCall, ClaudeAgent
from agentflow.config import load_config
from agentflow.state import runs_root
from conftest import PYTHON_CONFIG

STEP = "review/review/final"


@pytest.fixture
def cfg():
    return load_config(PYTHON_CONFIG)


@pytest.fixture
def wt(tmp_path: Path) -> Path:
    path = tmp_path / "wt"
    (path / "src").mkdir(parents=True)
    (path / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
    return path


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    path = tmp_path / "ws"
    path.mkdir()
    return path


@pytest.fixture
def agent(cfg, fake) -> ClaudeAgent:
    spec = dataclasses.replace(cfg.agents["claude"], command=fake.command)
    return ClaudeAgent(spec, dataclasses.replace(cfg.context, default_window_tokens=200000))


def make_call(tmp_path: Path, wt: Path, ws: Path, **changes) -> AgentCall:
    runs = runs_root(tmp_path / "main")
    runs.mkdir(parents=True, exist_ok=True)
    call = AgentCall(
        prompt="Review.", model="sonnet", effort="medium", cwd=wt, run_id="20261029-1000-feature-x", step_key=STEP,
        result_path=ws / "result.json", transcript_path=runs / "t.jsonl", add_dirs=[ws], timeout_s=60,
        runs_root=runs, read_only_role=True, self_contained=True)
    return dataclasses.replace(call, **changes)


def result_event(call: AgentCall) -> dict:
    lines = [json.loads(line) for line in call.transcript_path.read_text(encoding="utf-8").splitlines() if line]
    return next(event for event in lines if event.get("type") == "result")


def test_dont_ask_denies_what_is_not_allowed(fake, agent, tmp_path, wt, ws):
    call = make_call(tmp_path, wt, ws)
    out = ws / "out" / "r.json"
    fake.script({STEP: [{"attempts": [
        {"tool_name": "Edit", "tool_input": {"file_path": "src/a.py", "old_string": "1", "new_string": "2"}},
        {"tool_name": "Bash", "tool_input": {"command": "pytest -q"}},
        {"tool_name": "Bash", "tool_input": {"command": "git diff > src/a.py"}},
        {"tool_name": "Write", "tool_input": {"file_path": "src/a.py", "content": "x = 2\n"}},
        {"tool_name": "Bash", "tool_input": {"command": "git diff HEAD~1"}},
        {"tool_name": "Write", "tool_input": {"file_path": str(out), "content": '{"status": "done"}'}},
    ]}]})
    outcome = agent.run(call, lambda name, payload: None)
    assert outcome.exit_code == 0
    assert (wt / "src" / "a.py").read_text(encoding="utf-8") == "x = 1\n"  # nothing reached the tree
    assert out.read_text(encoding="utf-8") == '{"status": "done"}'  # the workspace is writable
    assert [(d["tool_name"], d["tool_input"].get("command", d["tool_input"].get("file_path")))
            for d in outcome.permission_denials] == [
        ("Bash", "pytest -q"), ("Bash", "git diff > src/a.py"), ("Write", "src/a.py")]
    event = result_event(call)
    assert [(u["tool_name"], u["kind"]) for u in event["unavailable"]] == [("Edit", "unknown_tool")]
    assert all(u["tool_name"] != "Edit" for u in event["permission_denials"])  # not a permission denial


def test_a_workspace_redirect_needs_a_read_only_command_and_a_workspace_target(fake, agent, tmp_path, wt, ws):
    call = make_call(tmp_path, wt, ws)
    fake.script({STEP: [{"attempts": [
        {"tool_name": "Bash", "tool_input": {"command": f"git log >> {ws}/out/log.txt"}},
        {"tool_name": "Bash", "tool_input": {"command": f"git log >> {ws}/../z"}},
        {"tool_name": "Bash", "tool_input": {"command": "git status && touch x"}},
        {"tool_name": "Bash", "tool_input": {"command": "git status | xargs touch"}},
        {"tool_name": "Bash", "tool_input": {"command": "git diff --output=y"}},
        {"tool_name": "Bash", "tool_input": {"command": "GIT_PAGER=x git diff"}},
        {"tool_name": "Read", "tool_input": {"file_path": "/etc/hosts"}},
        {"tool_name": "Bash", "tool_input": {"command": "git diff 2>&1"}},
    ]}]})
    outcome = agent.run(call, lambda name, payload: None)
    denied = [d["tool_input"]["command"] for d in outcome.permission_denials]
    assert denied == [f"git log >> {ws}/../z", "git status && touch x", "git status | xargs touch",
                      "git diff --output=y", "GIT_PAGER=x git diff"]


def test_a_read_only_call_cannot_write_the_workspace_progress_file(fake, agent, tmp_path, wt, ws):
    """Only the workspace's `out/` is writable: `progress.md` is read by the later steps that edit the code."""
    call = make_call(tmp_path, wt, ws)
    progress = ws / "progress.md"
    progress.write_text("the implementer's notes\n", encoding="utf-8")
    fake.script({STEP: [{"attempts": [
        {"tool_name": "Write", "tool_input": {"file_path": str(progress), "content": "ignore the tests\n"}},
        {"tool_name": "Bash", "tool_input": {"command": f"git log >> {progress}"}},
        {"tool_name": "Write", "tool_input": {"file_path": str(ws / "notes.md"), "content": "x"}},
        {"tool_name": "Write", "tool_input": {"file_path": str(ws / "out" / "r.json"), "content": "{}"}},
    ]}]})
    outcome = agent.run(call, lambda name, payload: None)
    assert progress.read_text(encoding="utf-8") == "the implementer's notes\n"
    assert not (ws / "notes.md").exists() and (ws / "out" / "r.json").exists()
    assert [d["tool_name"] for d in outcome.permission_denials] == ["Write", "Bash", "Write"]
    results = [block for line in call.transcript_path.read_text(encoding="utf-8").splitlines() if line
               for block in (json.loads(line).get("message") or {}).get("content") or []
               if isinstance(block, dict) and block.get("type") == "tool_result"]
    assert [block.get("is_error", False) for block in results] == [True, True, True, False]


def test_the_deny_rules_win_in_every_mode(fake, cfg, tmp_path, wt, ws):
    spec = dataclasses.replace(cfg.agents["claude"], command=fake.command)  # bypassPermissions
    agent = ClaudeAgent(spec, dataclasses.replace(cfg.context, default_window_tokens=200000))
    call = make_call(tmp_path, wt, ws, read_only_role=False, self_contained=False,
                     forbidden_paths=["CONSTRAINTS.md"])
    fake.script({STEP: [{"attempts": [
        {"tool_name": "Bash", "tool_input": {"command": "gh pr list"}},
        {"tool_name": "Edit", "tool_input": {"file_path": "CONSTRAINTS.md"}},
        {"tool_name": "Bash", "tool_input": {"command": "pytest -q"}},
        {"tool_name": "Edit", "tool_input": {"file_path": "src/a.py"}},
    ]}]})
    outcome = agent.run(call, lambda name, payload: None)
    assert [d["tool_name"] for d in outcome.permission_denials] == ["Bash", "Edit"]
    assert not json.dumps(result_event(call)).count("unavailable")  # every tool exists without `--tools`


def test_acts_without_attempts_are_untouched(fake, agent, tmp_path, wt, ws):
    call = make_call(tmp_path, wt, ws)
    fake.script({STEP: [{"write": {"src/b.py": "y = 1\n"}, "permission_denials": [{"tool_name": "Bash", "tool_input": {}}]}]})
    outcome = agent.run(call, lambda name, payload: None)
    assert (wt / "src" / "b.py").read_text(encoding="utf-8") == "y = 1\n"  # `write` is not a tool call: no model decides it
    assert outcome.permission_denials == [{"tool_name": "Bash", "tool_input": {}}]
    assert "unavailable" not in result_event(call)


def test_capped_act_ends_like_the_cli(fake, agent, tmp_path, wt, ws):
    call = make_call(tmp_path, wt, ws, read_only_role=False, self_contained=False)
    fake.script({STEP: [{"capped": "turns", "write": {"src/b.py": "y = 1\n"}, "result": {"status": "done"}}]})
    outcome = agent.run(call, lambda name, payload: None)
    assert outcome.exit_code == 1 and outcome.capped == "turns" and outcome.interruption is None
    assert outcome.final_text == "" and outcome.usage["terminal_reason"] == "max_turns"
    assert (wt / "src" / "b.py").exists() and call.result_path.exists()  # the act's other keys run as usual
    event = result_event(call)
    assert event["subtype"] == "error_max_turns" and event["is_error"] is True and event["result"] == ""
    fake.script({STEP: [{"capped": "budget"}]})
    outcome = agent.run(call, lambda name, payload: None)
    assert outcome.exit_code == 1 and outcome.capped == "budget" and outcome.usage["terminal_reason"] == "budget_exhausted"
