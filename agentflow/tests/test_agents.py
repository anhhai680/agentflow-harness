from __future__ import annotations

import dataclasses
import json
import re
import sys
from pathlib import Path

import pytest

from agentflow.agents import (
    AgentCall,
    RUNNER_ONLY_DENY,
    ClaudeAgent,
    CodexAgent,
    StreamAccumulator,
    make_agents,
    parse_claude_line,
    parse_codex_line,
)
from agentflow.config import load_config
from agentflow.layers import user_config_path
from agentflow.state import runs_root
from conftest import PYTHON_CONFIG

FIXTURES = Path(__file__).resolve().parent / "fixtures"
STEP = "build/build/task-3/implement"


@pytest.fixture
def cfg():
    return load_config(PYTHON_CONFIG)


@pytest.fixture
def claude(cfg) -> ClaudeAgent:
    return ClaudeAgent(cfg.agents["claude"], cfg.context)


@pytest.fixture
def codex(cfg) -> CodexAgent:
    return CodexAgent(cfg.agents["codex"], cfg.context)


@pytest.fixture
def runs(tmp_path: Path) -> Path:
    path = runs_root(tmp_path / "main")
    path.mkdir(parents=True)
    return path


@pytest.fixture
def wt(tmp_path: Path) -> Path:
    path = tmp_path / "wt"
    path.mkdir()
    return path


@pytest.fixture
def call(runs: Path, wt: Path) -> AgentCall:
    return AgentCall(
        prompt="Implement task 3.",
        model="sonnet",
        effort="medium",
        cwd=wt,
        run_id="20261029-1000-feature-x",
        step_key=STEP,
        result_path=runs / "20261029-1000-feature-x" / "results" / "build__build__task-3__implement.json",
        transcript_path=runs / "20261029-1000-feature-x" / "transcripts" / "build__build__task-3__implement.jsonl",
        add_dirs=[runs],
        timeout_s=60,
    )


@pytest.fixture
def fake_claude(cfg, fake) -> ClaudeAgent:
    spec = dataclasses.replace(cfg.agents["claude"], command=fake.command)
    # the fake agent scripts `usage_pct` against a 200K window; the shipped default is checked on its own
    return ClaudeAgent(spec, dataclasses.replace(cfg.context, default_window_tokens=200000))


def pairs(argv: list[str]) -> set[tuple[str, str]]:
    return set(zip(argv, argv[1:]))


def parse_file(path: Path, parser=parse_claude_line, **acc_kwargs):
    acc = StreamAccumulator(default_window=200000, **acc_kwargs)
    events: list[tuple[str, dict]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        events.extend(parser(line, acc))
    return acc.to_outcome(exit_code=0), events


def collect():
    events: list[tuple[str, dict]] = []
    return events, lambda name, payload: events.append((name, payload))


# ----------------------------------------------------------------------------- argv and env


def test_claude_argv_fresh(claude, call, runs):
    a = claude.argv(call)
    assert a[:5] == ["claude", "-p", "--output-format", "stream-json", "--verbose"]
    assert pairs(a) >= {
        ("--model", "sonnet"),
        ("--effort", "medium"),
        ("--permission-mode", "bypassPermissions"),
        ("--permission-prompts", "none"),
        ("--max-turns", "80"),
        ("--add-dir", str(runs)),
    }
    assert "--resume" not in a and "--allowedTools" not in a and "--disallowedTools" not in a
    assert a[-1] == call.prompt


def test_claude_argv_denies_runner_only_commands(claude, call):
    # Deny rules hold in bypassPermissions: the runner alone runs `gh`, with or without forbidden paths.
    for c in (call, dataclasses.replace(call, forbidden_paths=[".claude/settings.json"])):
        a = claude.argv(c)
        deny = json.loads(a[a.index("--settings") + 1])["permissions"]["deny"]
        assert set(RUNNER_ONLY_DENY) <= set(deny)
        assert all(f"Edit(./{p})" in deny for p in c.forbidden_paths)


def claude_argv_for(repo: Path) -> list[str]:
    """The argv a run in `repo` gives `claude`: the call names the run's runs root, as the runner's does."""
    claude = ClaudeAgent(load_config(PYTHON_CONFIG).agents["claude"], load_config(PYTHON_CONFIG).context)
    root = runs_root(repo)
    return claude.argv(AgentCall(
        prompt="p", model="sonnet", effort="medium", cwd=repo, run_id="r", step_key=STEP,
        result_path=root / "r" / "result.json", transcript_path=root / "r" / "t.jsonl",
        add_dirs=[], timeout_s=60, runs_root=root,
    ))


def deny_rules(argv: list[str]) -> list[str]:
    return json.loads(argv[argv.index("--settings") + 1])["permissions"]["deny"]


def test_runner_denies_edits_to_the_runs_root(repo_root):
    assert f"Edit(/{runs_root(repo_root)}/**)" in deny_rules(claude_argv_for(repo_root))
    assert f"Edit(/{runs_root(repo_root)}/**)".startswith("Edit(//")  # `//` is an absolute path in a rule


def test_claude_argv_without_a_runs_root_adds_no_rule_for_it(claude, call):
    user_dir_rule = f"Edit(/{user_config_path().parent}/**)"
    assert not [rule for rule in deny_rules(claude.argv(call)) if rule.startswith("Edit(//") and rule != user_dir_rule]


def test_runner_denies_edits_to_the_user_config_dir(claude, call, monkeypatch, tmp_path):
    # A live run reloads the user file (`config.layer_files`): an agent that wrote it would change its own run.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    for c in (call, dataclasses.replace(call, runs_root=tmp_path / "runs")):
        assert f"Edit(/{tmp_path / 'cfg' / 'agentflow'}/**)" in deny_rules(claude.argv(c))
    monkeypatch.delenv("XDG_CONFIG_HOME")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    assert f"Edit(/{tmp_path / 'home' / '.config' / 'agentflow'}/**)" in deny_rules(claude.argv(call))


def test_runner_skips_the_user_config_dir_rule_without_a_home(claude, call, monkeypatch):
    monkeypatch.delenv("XDG_CONFIG_HOME")

    def no_home(cls):
        raise RuntimeError("Could not determine home directory.")

    monkeypatch.setattr(Path, "home", classmethod(no_home))
    deny = deny_rules(claude.argv(call))
    assert set(RUNNER_ONLY_DENY) <= set(deny) and not [rule for rule in deny if rule.startswith("Edit(//")]


def test_claude_argv_add_dir_never_directly_before_prompt(claude, call, runs):
    # `--add-dir` is variadic in the claude CLI: a directory list ending at the prompt would swallow it.
    a = claude.argv(call)
    assert a[a.index("--add-dir") + 2].startswith("--")
    assert a[-2] != str(runs)


def test_claude_argv_resume_keeps_effort(claude, call):
    resumed = dataclasses.replace(call, resume_session="abc", effort="high")
    a = claude.argv(resumed)
    assert ("--resume", "abc") in pairs(a)
    assert ("--effort", "high") in pairs(a)
    assert a[-1] == resumed.prompt


def test_claude_argv_agent_profile(claude, call):
    a = claude.argv(dataclasses.replace(call, agent_profile="security-auditor"))
    assert ("--agent", "security-auditor") in pairs(a)
    assert "--agent" not in claude.argv(call)


def test_codex_argv(codex, call, wt):
    call = dataclasses.replace(call, model="gpt-5-codex", effort="high")
    a = codex.argv(call)
    assert a[:3] == ["codex", "exec", "--json"]
    assert pairs(a) >= {
        ("-m", "gpt-5-codex"),
        ("-c", "model_reasoning_effort=high"),
        ("-p", "agentflow"),
        ("--cd", str(wt)),
    }
    assert a[-1] == call.prompt
    resumed = codex.argv(dataclasses.replace(call, resume_session="abc"))
    assert resumed[1:4] == ["exec", "resume", "abc"]
    assert ("model_reasoning_effort=high" in resumed) and resumed[-1] == call.prompt
    assert "--cd" not in a[1:4] and ("--add-dir", str(call.add_dirs[0])) in pairs(a)


def test_codex_resume_argv_passes_every_add_dir_as_writable_roots(codex, call, runs, tmp_path):
    extra = tmp_path / "other dir"
    resumed = codex.argv(dataclasses.replace(call, resume_session="abc", add_dirs=[runs, extra]))
    expected = "sandbox_workspace_write.writable_roots=" + json.dumps([str(runs), str(extra)])
    assert ("-c", expected) in pairs(resumed)
    assert "--add-dir" not in resumed and "--cd" not in resumed
    fresh = codex.argv(dataclasses.replace(call, add_dirs=[runs, extra]))
    assert ("--add-dir", str(runs)) in pairs(fresh) and ("--add-dir", str(extra)) in pairs(fresh)
    assert not any(part.startswith("sandbox_workspace_write.") for part in fresh)
    assert not any(part.startswith("sandbox_workspace_write.") for part in
                   codex.argv(dataclasses.replace(call, resume_session="abc", add_dirs=[])))


def test_env(claude, codex, call):
    env = claude.env(call)
    assert env["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"] == "60"
    assert env["AGENTFLOW_STEP"] == STEP
    assert env["AGENTFLOW_RUN"] == call.run_id
    assert env["AGENTFLOW_RESULT"] == str(call.result_path)
    assert codex.env(call)["AGENTFLOW_RESULT"] == str(call.result_path)
    assert codex.env(call) == env  # D8: both adapters get the run vars and the auto-compact override


def test_codex_env_includes_autocompact_override(codex, call):
    env = codex.env(call)
    assert env["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"] == "60"
    assert set(env) == {"AGENTFLOW_RUN", "AGENTFLOW_STEP", "AGENTFLOW_RESULT", "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"}


def test_make_agents_only_enabled(cfg):
    agents = make_agents(cfg)
    assert list(agents) == ["claude"] and agents["claude"].name == "claude"


def test_make_agents_includes_codex_when_enabled(cfg):
    cfg.agents["codex"].enabled = True
    agents = make_agents(cfg)
    assert set(agents) == {"claude", "codex"} and isinstance(agents["codex"], CodexAgent)


# ----------------------------------------------------------------------------- parsing


def test_parse_fixture():
    o, events = parse_file(FIXTURES / "claude_stream.jsonl")
    assert o.session_id == "57375424-e6e7-4894-ab1f-64076b8af89f"
    assert o.cost_usd == pytest.approx(0.00341572)
    assert o.context_window == 1_000_000
    assert [t["name"] for t in o.tool_calls] == ["Edit", "Agent"]
    assert o.tool_calls[0]["input"]["file_path"] == "src/app.py"
    assert o.subagents[0]["subagent_type"] == "Explore"
    assert o.compactions == 1
    assert o.final_text == "Edited src/app.py and checked the layout with an Explore subagent."
    assert len(o.permission_denials) == 1 and o.permission_denials[0]["tool_name"] == "Bash"
    names = [name for name, _ in events]
    assert names[0] == "agent_session"
    assert names.count("agent_turn") == 3  # main-thread assistant events only
    assert names.index("subagent_started") < names.index("subagent_finished") < names.index("compacted")
    started = next(p for n, p in events if n == "subagent_started")
    assert started == {
        "parent_session": o.session_id,
        "subagent_type": "Explore",
        "model": "inherit",
        "tool_use_id": "toolu_02",
    }


def test_parse_context_pct_uses_main_thread_usage_and_default_window():
    acc = StreamAccumulator(default_window=200000)
    main = {"type": "assistant", "parent_tool_use_id": None, "message": {"content": [], "usage": {
        "input_tokens": 10, "cache_creation_input_tokens": 90, "cache_read_input_tokens": 99900}}}
    inner = {"type": "assistant", "parent_tool_use_id": "toolu_x", "message": {"content": [], "usage": {
        "input_tokens": 190000, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}}}
    events = parse_claude_line(json.dumps(main), acc) + parse_claude_line(json.dumps(inner), acc)
    assert acc.context_pct == pytest.approx(50.0)
    assert [n for n, _ in events] == ["agent_turn"]
    assert events[0][1]["tokens"] == 100000 and events[0][1]["context_pct"] == pytest.approx(50.0)
    assert acc.to_outcome(0).context_window is None  # not reported yet


def test_parse_window_override_wins_over_result_window():
    acc = StreamAccumulator(default_window=200000, window_override=500000)
    result = {"type": "result", "session_id": "s", "total_cost_usd": 0.1, "result": "ok",
              "modelUsage": {"m": {"contextWindow": 1000000}}}
    parse_claude_line(json.dumps(result), acc)
    assert acc.to_outcome(0).context_window == 500000


def test_parse_ignores_noise():
    acc = StreamAccumulator(default_window=200000)
    for line in ["", "   ", "not json", "[1, 2]", '{"type": "system", "subtype": "hook_started"}', '{"type": "weird"}']:
        assert parse_claude_line(line, acc) == []
    assert acc.to_outcome(0).session_id is None


def test_parse_over_threshold_only_on_main_assistant_then_user():
    acc = StreamAccumulator(default_window=100, rollover_pct=80)
    assistant = {"type": "assistant", "parent_tool_use_id": None, "message": {"content": [], "usage": {
        "input_tokens": 85, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}}}
    parse_claude_line(json.dumps(assistant), acc)
    assert acc.over_threshold and acc.line_type == "assistant"
    inner_user = {"type": "user", "parent_tool_use_id": "toolu_x", "message": {"content": []}}
    parse_claude_line(json.dumps(inner_user), acc)
    assert acc.line_type == ""  # a subagent's tool result is not a main-thread turn boundary
    parse_claude_line(json.dumps({"type": "user", "parent_tool_use_id": None, "message": {"content": []}}), acc)
    assert acc.over_threshold and acc.line_type == "user"
    parse_claude_line(json.dumps({"type": "system", "subtype": "compact_boundary"}), acc)
    assert not acc.over_threshold


def test_parse_codex_fixture():
    o, events = parse_file(FIXTURES / "codex_stream.jsonl", parser=parse_codex_line)
    assert o.session_id == "0199a213-81c0-7800-8aa1-bbab2a035a53"
    assert o.cost_usd == 0
    assert [t["name"] for t in o.tool_calls] == ["command_execution", "file_change"]
    assert o.final_text == "Added the helper and ran the tests."
    assert o.context_pct == pytest.approx(24763 / 200000 * 100)
    assert [n for n, _ in events] == ["agent_session", "agent_turn"]


# ----------------------------------------------------------------------------- running the fake


def test_run_closes_stdin(fake, fake_claude, call):
    events, on_event = collect()
    fake_claude.run(call, on_event)
    assert fake.calls()[0]["stdin_devnull"] is True


def test_run_fresh_session_and_outcome(fake, fake_claude, call, wt):
    fake.script({STEP: [{
        "write": {"src/new.py": "x = 1\n"},
        "result": {"status": "done", "summary": "wrote it"},
        "tool_calls": [{"name": "Edit", "input": {"file_path": "src/new.py"}}],
        "subagents": [{"subagent_type": "Explore", "model": "haiku"}],
        "compact": True,
        "usage_pct": [30, 40],
        "cost": 0.25,
        "final_text": "all done",
    }]})
    events, on_event = collect()
    o = fake_claude.run(call, on_event)
    assert o.exit_code == 0 and not o.timed_out and not o.rolled_over and not o.resume_failed
    assert re.fullmatch(r"[0-9a-f-]{36}", o.session_id)
    assert o.cost_usd == pytest.approx(0.25) and o.final_text == "all done"
    assert o.context_window == 200000 and o.context_pct == pytest.approx(40.0)
    assert [t["name"] for t in o.tool_calls] == ["Edit", "Agent"]
    assert o.subagents[0]["subagent_type"] == "Explore" and o.subagents[0]["model"] == "haiku"
    assert o.compactions == 1
    assert (wt / "src" / "new.py").read_text() == "x = 1\n"
    assert json.loads(call.result_path.read_text()) == {"status": "done", "summary": "wrote it"}
    names = [n for n, _ in events]
    assert names[0] == "agent_started" and names[-1] == "agent_exited"
    assert {"agent_session", "agent_turn", "subagent_started", "subagent_finished", "compacted"} <= set(names)
    started = events[0][1]
    assert started["agent"] == "claude" and started["model"] == "sonnet" and started["effort"] == "medium"
    assert events[-1][1] == {"session_id": o.session_id, "exit_code": 0, "cost": pytest.approx(0.25),
                             "cost_estimated": False}  # Claude reported it


def test_run_records_argv_and_env(fake, fake_claude, call):
    events, on_event = collect()
    fake_claude.run(call, on_event)
    rec = fake.calls()[0]
    assert rec["step"] == STEP
    assert rec["argv"][-1] == call.prompt and ("--effort", "medium") in pairs(rec["argv"])
    assert rec["env"]["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"] == "60"
    assert rec["env"]["AGENTFLOW_RESULT"] == str(call.result_path)
    assert set(rec["env"]) <= {"AGENTFLOW_RUN", "AGENTFLOW_STEP", "AGENTFLOW_RESULT", "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"}


def test_run_default_act_writes_done_result(fake, fake_claude, call):
    events, on_event = collect()
    fake_claude.run(call, on_event)
    assert json.loads(call.result_path.read_text()) == {"status": "done", "summary": "ok"}


def test_run_acts_are_consumed_in_order_per_step_key(fake, fake_claude, call):
    fake.script({STEP: [{"result": {"status": "blocked"}}, {"result_raw": "not json"}]})
    events, on_event = collect()
    for expected in ('{"status": "blocked"}', "not json", '{"status": "done", "summary": "ok"}'):
        call.result_path.unlink(missing_ok=True)
        fake_claude.run(call, on_event)
        assert call.result_path.read_text() == expected


def test_run_act_without_result_writes_no_file(fake, fake_claude, call):
    fake.script({STEP: [{"final_text": "forgot"}]})
    events, on_event = collect()
    o = fake_claude.run(call, on_event)
    assert not call.result_path.exists() and o.final_text == "forgot"


def test_run_resume_keeps_session_id(fake, fake_claude, call):
    events, on_event = collect()
    o = fake_claude.run(dataclasses.replace(call, resume_session="sess-123"), on_event)
    assert o.session_id == "sess-123" and not o.resume_failed
    assert ("--resume", "sess-123") in pairs(fake.calls()[0]["argv"])


def test_run_git_commit_act(fake, fake_claude, call, wt):
    import subprocess

    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=wt, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=wt, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=wt, check=True)
    fake.script({STEP: [{"write": {"a.txt": "hi"}, "git_commit": "agent commit"}]})
    events, on_event = collect()
    fake_claude.run(call, on_event)
    log = subprocess.run(["git", "log", "--format=%s"], cwd=wt, capture_output=True, text=True, check=True)
    assert log.stdout.splitlines() == ["agent commit"]


def test_rollover_terminates_after_turn(fake, fake_claude, call):
    fake.script({STEP: [{"usage_pct": [50, 81, 90], "result": {"status": "done"}}]})
    events, on_event = collect()
    o = fake_claude.run(call, on_event)
    turn_pcts = [p["context_pct"] for n, p in events if n == "agent_turn"]
    assert o.rolled_over and not o.timed_out
    assert turn_pcts == [50.0, 81.0] and max(turn_pcts) < 90
    assert o.context_pct == pytest.approx(81.0)
    assert events[-1][0] == "agent_exited"


def test_no_rollover_below_threshold(fake, fake_claude, call):
    fake.script({STEP: [{"usage_pct": [50, 79]}]})
    events, on_event = collect()
    o = fake_claude.run(call, on_event)
    assert not o.rolled_over and o.exit_code == 0 and o.context_pct == pytest.approx(79.0)


def test_timeout_kills(fake, fake_claude, call):
    fake.script({STEP: [{"sleep": 5}]})
    events, on_event = collect()
    o = fake_claude.run(dataclasses.replace(call, timeout_s=1), on_event)
    assert o.timed_out and o.exit_code != 0 and not o.resume_failed
    assert events[-1][0] == "agent_exited"


def test_resume_failed(fake, fake_claude, call):
    fake.script({STEP: [{"no_init": True, "exit": 1}]})
    events, on_event = collect()
    o = fake_claude.run(dataclasses.replace(call, resume_session="gone"), on_event)
    assert o.resume_failed and o.exit_code == 1 and o.session_id is None


def test_fresh_crash_is_not_resume_failed(fake, fake_claude, call):
    fake.script({STEP: [{"no_init": True, "exit": 1}]})
    events, on_event = collect()
    o = fake_claude.run(call, on_event)
    assert o.exit_code == 1 and not o.resume_failed


def test_transcript_written(fake, fake_claude, call):
    fake.script({STEP: [{"tool_calls": [{"name": "Edit", "input": {}}], "usage_pct": [10]}]})
    events, on_event = collect()
    fake_claude.run(call, on_event)
    lines = call.transcript_path.read_text(encoding="utf-8").splitlines()
    types = [json.loads(line)["type"] for line in lines]
    assert types[:2] == ["system", "system"] and types[-1] == "result"  # hook event, init, ..., result
    assert types.count("assistant") == 2 and types.count("user") == 2


def test_transcript_appends_across_calls(fake, fake_claude, call):
    events, on_event = collect()
    fake_claude.run(call, on_event)
    first = len(call.transcript_path.read_text().splitlines())
    fake_claude.run(call, on_event)
    assert len(call.transcript_path.read_text().splitlines()) == 2 * first


def test_window_override_from_config_is_used(cfg, fake, call):
    cfg.context.context_window_tokens = 100000  # a fixed number beats the default and the result event
    spec = dataclasses.replace(cfg.agents["claude"], command=fake.command)
    agent = ClaudeAgent(spec, cfg.context)
    fake.script({STEP: [{"usage_pct": [30]}]})  # 60000 tokens
    events, on_event = collect()
    o = agent.run(call, on_event)
    assert o.context_window == 100000 and o.context_pct == pytest.approx(60.0)


def test_python_executable_is_used_for_fake(fake):
    assert fake.command[0] == sys.executable


def test_call_context_window_beats_the_default(fake, fake_claude, call):
    fake.script({STEP: [{"usage_pct": [45]}]})  # 90000 tokens: 45 % of the default, 90 % of a known 100k window
    events, on_event = collect()
    o = fake_claude.run(dataclasses.replace(call, context_window=100000), on_event)
    turn = next(p for n, p in events if n == "agent_turn")
    assert turn["context_pct"] == pytest.approx(90.0) and o.rolled_over


def test_config_window_override_beats_the_call_window(cfg, fake, call):
    cfg.context.context_window_tokens = 300000
    agent = ClaudeAgent(dataclasses.replace(cfg.agents["claude"], command=fake.command), cfg.context)
    fake.script({STEP: [{"usage_pct": [45]}]})
    events, on_event = collect()
    o = agent.run(dataclasses.replace(call, context_window=100000), on_event)
    assert o.context_pct == pytest.approx(30.0) and not o.rolled_over


# ------------------------------------------------------------------ cost estimate and window

OPUS = "claude-opus-5-5"


def _assistant(msg_id, usage, model=OPUS):
    return json.dumps({"type": "assistant", "parent_tool_use_id": None,
                       "message": {"id": msg_id, "model": model, "content": [], "usage": usage}})


def _acc(cfg, **extra):
    return StreamAccumulator(default_window=cfg.context.default_window_tokens, prices=cfg.agents["claude"].prices, **extra)


def test_shipped_prices_match_what_claude_billed(cfg):
    # Three `claude -p` calls on 2026-10-30: tokens as reported by modelUsage, cost as costUSD (1h cache writes).
    prices = cfg.agents["claude"].prices
    from agentflow.agents import estimate_cost

    def cost(model, **tokens):
        usage = {"input_tokens": 2, "output_tokens": 4, "cache_read_input_tokens": tokens["read"],
                 "cache_creation_input_tokens": tokens["write"],
                 "cache_creation": {"ephemeral_1h_input_tokens": tokens["write"], "ephemeral_5m_input_tokens": 0}}
        return estimate_cost(prices, {"m": (model, usage)})

    assert cost(OPUS, read=17761, write=7992) == pytest.approx(0.0675762)
    assert cost("claude-sonnet-5-5", read=9074, write=16571) == pytest.approx(0.0672354)
    assert cost("claude-haiku-5-5", read=9074, write=17394) == pytest.approx(0.00357174)


def test_default_window_is_the_models_real_window(cfg):
    assert cfg.context.default_window_tokens == 1_000_000
    acc = _acc(cfg)
    parse_claude_line(_assistant("m1", {"input_tokens": 2, "cache_read_input_tokens": 152963,
                                        "cache_creation_input_tokens": 13540}), acc)
    assert acc.context_pct == pytest.approx(16.65, abs=0.01)  # was read as 83% against a 200K guess


def test_killed_session_keeps_an_estimated_cost(cfg):
    acc = _acc(cfg, rollover_pct=80)
    usage = {"input_tokens": 2, "output_tokens": 10, "cache_read_input_tokens": 100000, "cache_creation_input_tokens": 0}
    events = parse_claude_line(_assistant("m1", usage), acc)
    assert acc.cost_usd == pytest.approx((2 * 4 + 10 * 20 + 100000 * 0.20) / 1e6)
    assert events[0][1]["cost"] == pytest.approx(acc.cost_usd)  # the live turn event carries it too
    assert not acc.cost_reported and acc.to_outcome(143, rolled_over=True).cost_usd == pytest.approx(acc.cost_usd)


def test_one_api_call_is_priced_once_however_many_lines_repeat_it(cfg):
    acc = _acc(cfg)
    usage = {"input_tokens": 0, "output_tokens": 1000, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
    for _ in range(3):  # one line per content block of the same message
        parse_claude_line(_assistant("m1", usage), acc)
    assert acc.cost_usd == pytest.approx(1000 * 20 / 1e6)


def test_reported_cost_replaces_the_estimate(cfg):
    acc = _acc(cfg)
    parse_claude_line(_assistant("m1", {"input_tokens": 1000000}), acc)
    assert acc.cost_usd == pytest.approx(4.0)
    parse_claude_line(json.dumps({"type": "result", "session_id": "s", "total_cost_usd": 0.5, "result": "ok"}), acc)
    assert acc.cost_usd == 0.5 and acc.cost_reported
    parse_claude_line(_assistant("m2", {"input_tokens": 1000000}), acc)  # a late line does not add to a reported cost
    assert acc.cost_usd == 0.5


def test_interrupted_result_with_zero_cost_keeps_the_estimate(cfg):
    acc = _acc(cfg)
    parse_claude_line(_assistant("m1", {"input_tokens": 1000000}), acc)
    parse_claude_line(json.dumps({"type": "result", "subtype": "error_during_execution", "session_id": "s",
                                  "total_cost_usd": 0, "modelUsage": {}}), acc)
    assert acc.cost_usd == pytest.approx(4.0) and not acc.cost_reported


def test_unpriced_model_adds_nothing(cfg):
    acc = _acc(cfg)
    parse_claude_line(_assistant("m1", {"input_tokens": 1000000}, model="claude-unknown-9"), acc)
    assert acc.cost_usd == 0.0


# --------------------------------------------------------------------------- why a call ended


def _limit(status, resets_at=1793439000, window="allowed"):
    return json.dumps({"type": "rate_limit_event", "session_id": "s", "rate_limit_info": {
        "status": status, "resetsAt": resets_at, "rateLimitType": "five_hour", "overageStatus": "rejected",
        "unifiedWindows": {"five_hour": {"utilization": 1.0, "resetsAt": resets_at, "status": window}}}})


def _end(is_error=False):
    return json.dumps({"type": "result", "subtype": "success", "is_error": is_error, "session_id": "s", "result": ""})


def test_a_clean_end_is_no_interruption(cfg):
    acc = _acc(cfg)
    parse_claude_line(_limit("allowed"), acc)
    parse_claude_line(_end(), acc)
    assert acc.to_outcome(0).interruption is None


def test_a_rejected_limit_gives_its_reset_time(cfg):
    acc = _acc(cfg)
    parse_claude_line(_limit("rejected"), acc)
    why = acc.to_outcome(1).interruption
    assert (why.kind, why.until, why.reached_model) == ("usage_limit", 1793439000, False)
    assert why.limit["rateLimitType"] == "five_hour"  # kept as it came: a real rejection has not been seen yet


def test_a_rejected_window_alone_refuses_nothing(cfg):
    acc = _acc(cfg)
    parse_claude_line(_limit("allowed", window="rejected"), acc)
    parse_claude_line(_end(), acc)
    assert acc.to_outcome(0).interruption is None


def test_the_latest_limit_event_decides(cfg):
    acc = _acc(cfg)
    parse_claude_line(_limit("rejected"), acc)
    parse_claude_line(_limit("allowed"), acc)
    parse_claude_line(_end(), acc)
    assert acc.to_outcome(0).interruption is None


def test_a_rejection_with_no_reset_time_is_transient(cfg):
    acc = _acc(cfg)
    parse_claude_line(_limit("rejected", resets_at=None), acc)
    assert acc.to_outcome(1).interruption.kind == "transient"


@pytest.mark.parametrize("exit_code, lines", [(1, []), (0, [_end(is_error=True)])])
def test_a_failed_call_is_transient(cfg, exit_code, lines):
    acc = _acc(cfg)
    for line in lines:
        parse_claude_line(line, acc)
    why = acc.to_outcome(exit_code).interruption
    assert (why.kind, why.until, why.reached_model) == ("transient", None, False)


def test_a_model_reply_counts_as_reaching_the_model(cfg):
    acc = _acc(cfg)
    parse_claude_line(_assistant("m1", {"input_tokens": 10}), acc)
    assert acc.to_outcome(1).interruption.reached_model is True


def test_a_synthetic_error_message_is_not_a_model_reply(cfg):
    acc = _acc(cfg)
    parse_claude_line(_assistant("m1", {"input_tokens": 0}, model="<synthetic>"), acc)
    assert acc.to_outcome(1).interruption.reached_model is False


def test_a_codex_call_that_exits_cleanly_is_no_interruption():
    acc = StreamAccumulator(default_window=1000)
    assert acc.to_outcome(0).interruption is None  # codex sends no `result` event
