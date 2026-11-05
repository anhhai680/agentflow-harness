"""What a headless run needs so a stop is cheap: an environment set up for it, the integration tests in
its gates, the settings the agent runs under, and human stops that can change something."""

from __future__ import annotations

import dataclasses
import json

import pytest

from agentflow.agents import RUNNER_ONLY_DENY
from agentflow.config import ConfigError, Gate, Phase, Step, load_config, resolve_model
from agentflow.pack import ENGINE_PROMPTS
from conftest import PYTHON_CONFIG
from agentflow.gates import resolve, run_gates
from agentflow.hitl import NotWaiting, approve, raise_hitl
from agentflow.run_control import retry

KEY = "build/impl"
DENIED = {"tool_name": "Bash", "tool_input": {"command": "uv lock --project devtools/token-issuer"}}


def blocked(summary: str = "uv is denied", **act) -> dict:
    return {"result": {"status": "blocked", "summary": summary}, **act}


def impl() -> Step:
    return Step(id="impl", type="agent", skill="test-driven-development", role="build", prompt="step.md")


def run_impl(flow_repo, run):
    steps = [impl()]
    phase = Phase(name="build", agent=None, model="sonnet", skip_if=None, steps=steps)
    return flow_repo.executor(run).run_steps(phase, steps, "build")


def raised(flow_repo, run) -> list[str]:
    return [e["kind"] for e in flow_repo.events(run, "hitl_raised")]


@pytest.fixture
def shipped():
    return load_config(PYTHON_CONFIG)


def phase_named(cfg, name: str) -> Phase:
    return cfg.phases[name]


# --------------------------------------------------------------------------- the shipped config


def test_verify_runs_integration_between_unit_and_the_coverage_check(shipped):
    names = [g.name for g in resolve(shipped, "verify")]
    assert names.index("py_unit") < names.index("py_integration") < names.index("py_cov_diff")


def test_the_python_test_gates_write_the_coverage_the_check_reads(shipped):
    assert shipped.gates["py_unit"].run == "python3 -m unittest -q"  # the toy's unit tests write no coverage
    integration = shipped.gates["py_integration"].run
    assert "tests/integration" in integration
    assert "--cov-append" in integration and "--cov-report=xml" in integration


@pytest.mark.parametrize("phase", ["build", "verify"])
def test_setup_runs_before_any_agent_in_the_phase(shipped, phase):
    steps = phase_named(shipped, phase).steps
    setup = next(i for i, s in enumerate(steps) if s.id == "setup")
    assert steps[setup].type == "gate"
    assert not steps[setup].fix_loop  # an environment failure goes to a human, never to a paid agent
    assert all(s.type != "agent" for s in steps[:setup])


def test_a_review_fix_gets_the_environment_back_first(shipped):
    steps = phase_named(shipped, "review").steps
    assert [s.id for s in steps[:3]] == ["setup", "plan_fixes", "fix_findings"]
    assert steps[0].when == "refine_round"


def test_setup_covers_python_web_and_the_test_database(shipped):
    assert {g.name for g in resolve(shipped, "setup")} == {"setup_py", "setup_web", "setup_db"}


def test_the_verify_session_starts_from_the_verify_gates_output(shipped):
    verify = next(s for s in phase_named(shipped, "verify").steps if s.id == "verify")
    assert verify.gates_first and verify.gates == ["verify", "web_e2e"] and verify.fix_loop


def test_only_the_verify_step_runs_the_e2e_suite_through_the_runner_owned_stack(shipped):
    verify = next(s for s in phase_named(shipped, "verify").steps if s.id == "verify")
    assert [g.name for g in resolve(shipped, verify.gates)][-1] == "web_e2e"  # last: slowest, needs the port lock
    e2e = shipped.gates["web_e2e"]
    assert e2e.run == "true"  # the toy has no e2e stack
    assert e2e.when_exists == "web/e2e"
    # not in the shared sets: fix loops, sync and the CI fix would rebuild the stack on every attempt
    assert "web_e2e" not in {g.name for g in resolve(shipped, "build")}
    assert "web_e2e" not in {g.name for g in resolve(shipped, "verify")}


def test_runs_use_their_own_postgres_port_and_the_short_cache(shipped):
    assert shipped.defaults.env["POSTGRES_HOST_PORT"] != "5432"
    assert shipped.defaults.env["FORCE_PROMPT_CACHING_5M"] == "1"


def test_recording_a_decision_runs_on_sonnet_and_planning_stays_on_opus(shipped):
    plan = phase_named(shipped, "plan")
    assert resolve_model(shipped, plan, shipped.record_step, shipped.record_step.role, {})[0] == "sonnet"
    assert resolve_model(shipped, plan, plan.steps[0], plan.steps[0].role, {})[0] == "opus"


# --------------------------------------------------------------------------- env reaches gates and agents


def test_gates_get_the_configured_env(tmp_git, tmp_path):
    cfg = load_config(PYTHON_CONFIG)
    cfg = dataclasses.replace(
        cfg,
        gates={"probe": Gate(name="probe", run='test "$AGENTFLOW_PROBE" = yes')},
        defaults=dataclasses.replace(cfg.defaults, env={"AGENTFLOW_PROBE": "yes"}),
    )
    (result,) = run_gates(cfg, ["probe"], tmp_git, tmp_path / "logs")
    assert result.passed


def test_an_agent_call_gets_the_configured_env(flow_repo):
    flow_repo.configure({"defaults": {"env": {"AGENTFLOW_PROBE": "yes"}}})
    run = flow_repo.new_run()
    flow_repo.script({KEY: [{"result": {"status": "done", "summary": "ok"}}]})
    run_impl(flow_repo, run)
    assert flow_repo.calls()[0]["env"]["AGENTFLOW_PROBE"] == "yes"


def test_env_values_must_be_strings(flow_repo):
    with pytest.raises(ConfigError, match="defaults.env"):
        flow_repo.configure({"defaults": {"env": {"POSTGRES_HOST_PORT": 55432}}})


# --------------------------------------------------------------------------- forbidden paths


def test_forbidden_paths_are_denied_before_the_edit(flow_repo):
    flow_repo.configure({"guardrails": {"forbidden_paths": ["CONSTRAINTS.md", ".claude/**"]}})
    run = flow_repo.new_run()
    flow_repo.script({KEY: [{"result": {"status": "done", "summary": "ok"}}]})
    run_impl(flow_repo, run)
    argv = flow_repo.calls()[0]["argv"]
    settings = json.loads(argv[argv.index("--settings") + 1])
    # `./`, not `/`: real `claude` ignored `Edit(/CONSTRAINTS.md)` from --settings and let the write through
    assert settings == {"permissions": {"deny": ["Edit(./CONSTRAINTS.md)", "Edit(./.claude/**)", "Edit(./.agentflow/**)",
                                                 *RUNNER_ONLY_DENY]}}


def test_settings_hold_only_the_engine_floor_and_the_runner_only_denies_when_the_pack_forbids_nothing(flow_repo):
    cfg = flow_repo.configure({"guardrails": {"forbidden_paths": []}})
    run = flow_repo.new_run()
    flow_repo.script({KEY: [{"result": {"status": "done", "summary": "ok"}}]})
    run_impl(flow_repo, run)
    assert cfg.guardrails.forbidden_paths == [".agentflow/**"]  # the engine's floor: the pack itself
    argv = flow_repo.calls()[0]["argv"]
    assert json.loads(argv[argv.index("--settings") + 1]) == {"permissions": {"deny": ["Edit(./.agentflow/**)",
                                                                                      *RUNNER_ONLY_DENY]}}


def test_the_settings_come_before_the_prompt(flow_repo):
    run = flow_repo.new_run()
    flow_repo.script({KEY: [{"result": {"status": "done", "summary": "ok"}}]})
    run_impl(flow_repo, run)
    argv = flow_repo.calls()[0]["argv"]
    assert argv.index("--settings") < len(argv) - 2  # the last item is the prompt, the one before it the value


# --------------------------------------------------------------------------- a note on an approval


def test_an_approval_note_reaches_the_resumed_session(flow_repo):
    run = flow_repo.new_run()
    flow_repo.script({KEY: [blocked(), {"result": {"status": "done", "summary": "ok"}}]})
    assert run_impl(flow_repo, run) == ("stopped", True)
    approve(run, KEY, by="me", note="Write .env.example through Bash.")
    assert run_impl(flow_repo, run) == ("done", True)
    assert "Write .env.example through Bash." in flow_repo.calls()[1]["argv"][-1]
    assert flow_repo.events(run, "hitl_approved")[-1]["note"] == "Write .env.example through Bash."


def test_a_resumed_session_is_told_to_try_a_denied_command_again(flow_repo):
    run = flow_repo.new_run()
    flow_repo.script({KEY: [blocked(), {"result": {"status": "done", "summary": "ok"}}]})
    run_impl(flow_repo, run)
    approve(run, KEY, by="me")
    run_impl(flow_repo, run)
    assert "try it once more" in flow_repo.calls()[1]["argv"][-1]


def test_a_note_is_refused_where_no_agent_would_read_it(flow_repo):
    run = flow_repo.new_run()
    raise_hitl(flow_repo.cfg, run, "checkpoint", "build/doubt_offer", "Offer it")
    with pytest.raises(ValueError, match="no agent"):
        approve(run, "build/doubt_offer", by="me", note="ignored otherwise")


# --------------------------------------------------------------------------- what the human sees


def test_a_blocked_inbox_lists_each_denied_command_once(flow_repo):
    run = flow_repo.new_run()
    flow_repo.script({KEY: [blocked(permission_denials=[DENIED, DENIED])]})
    run_impl(flow_repo, run)
    inbox = run.inbox_path(KEY).read_text(encoding="utf-8")
    assert inbox.count("uv lock --project devtools/token-issuer") == 1
    # the session reads the worktree's settings, where `settings.json` is a forbidden path
    assert ".claude/settings.local.json" in inbox


def test_a_blocked_inbox_drops_denials_from_before_the_last_approval(flow_repo):
    run = flow_repo.new_run()
    later = {"tool_name": "Bash", "tool_input": {"command": "pnpm dlx something"}}
    flow_repo.script({KEY: [blocked(permission_denials=[DENIED]), blocked(permission_denials=[later])]})
    run_impl(flow_repo, run)
    approve(run, KEY, by="me")
    run_impl(flow_repo, run)
    inbox = run.inbox_path(KEY).read_text(encoding="utf-8")
    assert "pnpm dlx something" in inbox and "uv lock" not in inbox


def test_a_blocked_inbox_with_no_denials_has_no_denial_section(flow_repo):
    run = flow_repo.new_run()
    flow_repo.script({KEY: [blocked()]})
    run_impl(flow_repo, run)
    assert "settings.local.json" not in run.inbox_path(KEY).read_text(encoding="utf-8")


# --------------------------------------------------------------------------- a repeat stop


def test_blocking_again_with_nothing_changed_is_a_repeat_stop(flow_repo):
    run = flow_repo.new_run()
    flow_repo.script({KEY: [blocked(), blocked("uv is still denied")]})
    run_impl(flow_repo, run)
    approve(run, KEY, by="me")
    run_impl(flow_repo, run)
    assert raised(flow_repo, run) == ["blocked", "blocked_repeat"]


def test_a_repeat_stop_refuses_a_blind_approve(flow_repo):
    run = flow_repo.new_run()
    flow_repo.script({KEY: [blocked(), blocked()]})
    run_impl(flow_repo, run)
    approve(run, KEY, by="me")
    run_impl(flow_repo, run)
    with pytest.raises(NotWaiting, match="--note"):
        approve(run, KEY, by="me")
    assert run.state["waiting"]["kind"] == "blocked_repeat"


def test_a_repeat_stop_takes_a_note(flow_repo):
    run = flow_repo.new_run()
    flow_repo.script({KEY: [blocked(), blocked(), {"result": {"status": "done", "summary": "ok"}}]})
    run_impl(flow_repo, run)
    approve(run, KEY, by="me")
    run_impl(flow_repo, run)
    approve(run, KEY, by="me", note="uv is allowed now")
    assert run_impl(flow_repo, run) == ("done", True)


def test_a_repeat_stop_restarts_in_a_fresh_session(flow_repo):
    run = flow_repo.new_run()
    flow_repo.script({KEY: [blocked(), blocked(), {"result": {"status": "done", "summary": "ok"}}]})
    run_impl(flow_repo, run)
    approve(run, KEY, by="me")
    run_impl(flow_repo, run)
    retry(run, KEY, by="me")
    assert run_impl(flow_repo, run) == ("done", True)
    assert "--resume" not in flow_repo.calls()[2]["argv"]


def test_a_resume_that_changed_files_is_not_a_repeat(flow_repo):
    run = flow_repo.new_run()
    flow_repo.script({KEY: [blocked(), blocked(write={"progress_made.txt": "more"})]})
    run_impl(flow_repo, run)
    approve(run, KEY, by="me")
    run_impl(flow_repo, run)
    assert raised(flow_repo, run) == ["blocked", "blocked"]


def test_a_reminder_does_not_hide_a_repeat(flow_repo):
    run = flow_repo.new_run()
    flow_repo.script({KEY: [blocked(), {"result_raw": "not json"}, blocked()]})
    run_impl(flow_repo, run)
    approve(run, KEY, by="me")
    run_impl(flow_repo, run)
    assert raised(flow_repo, run) == ["blocked", "blocked_repeat"]


def test_a_first_block_is_never_a_repeat(flow_repo):
    run = flow_repo.new_run()
    flow_repo.script({KEY: [blocked()]})
    run_impl(flow_repo, run)
    assert raised(flow_repo, run) == ["blocked"]


# --------------------------------------------------------------------------- the prompts


@pytest.mark.parametrize("line", ["never `cd`", "One command per Bash call", "Run only the tests you wrote or changed"])
def test_the_preamble_gives_the_shell_rules(shipped, line):
    text = (ENGINE_PROMPTS / "_preamble.md").read_text(encoding="utf-8")
    assert line in text


@pytest.mark.parametrize("line", ["format what you changed", "the runner's build gates run them", "`blocked`"])
def test_the_task_prompt_names_what_to_run(shipped, line):
    text = (ENGINE_PROMPTS / "task.md").read_text(encoding="utf-8")
    assert line in text


@pytest.mark.parametrize("prompt", ["task.md", "task_next.md"])
def test_implementers_format_and_leave_lint_types_and_the_floor_to_the_build_gates(shipped, prompt):
    text = (ENGINE_PROMPTS / prompt).read_text(encoding="utf-8")
    assert "format what you changed" in text and "the runner's build gates run them" in text
    assert "floor_guard" not in text  # the floor is a pack's gate, run after the session; no prompt names it
