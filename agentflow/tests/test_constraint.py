"""A person's override for a constraint gate (T4 Task 7): `approve --constraint` refuses everyone but a person outside
an agent session, before any event; `constraint.pending`; `constraint.write`; `foreign_commits`; and `validate`."""

from __future__ import annotations

import pytest

from agentflow import constraint
from agentflow.autonomy import AUTO_BY
from agentflow.commit_checks import foreign_commits, runner_head
from agentflow.config import load_config, validate
from agentflow.events import EventError
from agentflow.hitl import NotWaiting, Question, approve, constraint_gates_failed, raise_hitl
from conftest import run_git

K = "build/build/task-2/commit"
TRAILER = "Constraints-Approved-By"
OVERRIDE = {"guardrails": {"constraint_override": {"trailer": TRAILER, "gates": ["floor"]}}}


@pytest.fixture(autouse=True)
def _a_persons_terminal(monkeypatch):
    """These tests are a person's terminal, even when an agent session runs the suite."""
    monkeypatch.delenv("AGENTFLOW_STEP", raising=False)
    monkeypatch.delenv("AGENTFLOW_RUN", raising=False)


@pytest.fixture
def ccfg(tmp_cfg):
    """The `python` pack with `floor` as its constraint gate."""
    return load_config(tmp_cfg(OVERRIDE))


def gate_failed_on(run, ccfg, key: str = K, gate: str = "floor", passed: bool = False) -> None:
    """A constraint gate's result on `key`, then the `gate_failed` stop the run waits on."""
    run.emit("gate_result", key=key, gate=gate, passed=passed, skipped=False, reason="", log=None, duration_s=0.1,
             tree="t")
    raise_hitl(ccfg, run, "gate_failed", key, f"Gates failed before committing: {gate}.", notify=False)


def kinds(run) -> list[str]:
    return [e["type"] for e in run.events.read()]


# --------------------------------------------------------------------------- who may approve


def test_a_constraint_approval_from_an_agent_session_is_refused(run, ccfg, monkeypatch):  # Review Focus 4
    gate_failed_on(run, ccfg)
    before = kinds(run)
    monkeypatch.setenv("AGENTFLOW_STEP", "build/build/task-2/implement")
    with pytest.raises(NotWaiting, match="agent session"):
        approve(run, K, by="me", constraint=True, cfg=ccfg)
    assert not [e for e in run.events.read() if e["type"] == "hitl_approved"]
    assert kinds(run) == before and run.state["waiting"]["key"] == K


def test_an_agent_sessions_run_variable_alone_is_refused_too(run, ccfg, monkeypatch):
    gate_failed_on(run, ccfg)
    monkeypatch.delenv("AGENTFLOW_STEP", raising=False)
    monkeypatch.setenv("AGENTFLOW_RUN", "")  # set, even empty: an agent session's
    with pytest.raises(NotWaiting, match="agent session"):
        approve(run, K, by="me", constraint=True, cfg=ccfg)
    assert "hitl_approved" not in kinds(run)


@pytest.mark.parametrize("by", [AUTO_BY, "Runner", " runner​ "])
def test_the_runner_cannot_approve_a_constraint(run, ccfg, by):
    gate_failed_on(run, ccfg)
    before = kinds(run)
    with pytest.raises(NotWaiting, match="only a person"):
        approve(run, K, by=by, constraint=True, cfg=ccfg)
    assert kinds(run) == before


def test_a_name_that_cleans_to_nothing_is_refused(run, ccfg):
    gate_failed_on(run, ccfg)
    with pytest.raises(NotWaiting, match="name"):
        approve(run, K, by="‮​ \n", constraint=True, cfg=ccfg)
    assert "hitl_approved" not in kinds(run)


def test_no_constraint_approval_without_a_failed_constraint_gate(run, ccfg):
    gate_failed_on(run, ccfg, gate="py_lint")  # a failed gate, but not a constraint gate
    before = kinds(run)
    with pytest.raises(NotWaiting, match="no constraint gate failed"):
        approve(run, K, by="me", constraint=True, cfg=ccfg)
    assert kinds(run) == before
    approve(run, K, by="me", cfg=ccfg)  # a plain approval still settles it
    (ok,) = [e for e in run.events.read() if e["type"] == "hitl_approved"]
    assert "constraint" not in ok


def test_a_constraint_gate_that_passed_last_is_no_failure(run, ccfg):
    run.emit("gate_result", key=K, gate="floor", passed=False, skipped=False, reason="", log=None, duration_s=0.1,
             tree="t")
    gate_failed_on(run, ccfg, passed=True)
    assert constraint_gates_failed(ccfg, run, K) == []
    with pytest.raises(NotWaiting, match="no constraint gate failed"):
        approve(run, K, by="me", constraint=True, cfg=ccfg)


def test_without_an_override_in_the_pack_nothing_is_a_constraint_gate(run, cfg):
    gate_failed_on(run, cfg)
    with pytest.raises(NotWaiting, match="no constraint gate failed"):
        approve(run, K, by="me", constraint=True, cfg=cfg)
    assert "hitl_approved" not in kinds(run)


def test_an_item_with_questions_is_refused(run, ccfg):
    run.emit("gate_result", key=K, gate="floor", passed=False, skipped=False, reason="", log=None, duration_s=0.1,
             tree="t")
    raise_hitl(ccfg, run, "needs_human", K, "a decision", [Question("q1", "Which?", [], [], "ambiguity")], notify=False)
    before = kinds(run)
    with pytest.raises(NotWaiting, match="answers"):
        approve(run, K, by="me", constraint=True, cfg=ccfg)
    assert kinds(run) == before


def test_a_phase_approval_is_never_a_constraint(run, ccfg):
    run.emit("gate_result", key="plan/plan", gate="floor", passed=False, skipped=False, reason="", log=None,
             duration_s=0.1, tree="t")
    run.emit("phase_approval_raised", phase="plan", key="plan", inbox="x")
    before = kinds(run)
    with pytest.raises(NotWaiting, match="phase"):
        approve(run, "plan", by="me", constraint=True, cfg=ccfg)
    assert kinds(run) == before


def test_a_person_approves_a_constraint_gate_failure(run, ccfg):
    gate_failed_on(run, ccfg)
    assert constraint_gates_failed(ccfg, run, K) == ["floor"]
    inbox = run.inbox_path(K).read_text(encoding="utf-8")
    assert f"Only a person can lift this constraint: `agentflow approve {run.id} {K} --constraint`." in inbox
    approve(run, K, by="Hải", constraint=True, cfg=ccfg)
    (ok,) = [e for e in run.events.read() if e["type"] == "hitl_approved"]
    assert ok["constraint"] is True and ok["by"] == "Hải" and ok["key"] == K
    assert run.state["waiting"] is None


def test_a_stop_with_no_constraint_gate_failed_has_no_constraint_line(run, ccfg):
    gate_failed_on(run, ccfg, gate="py_lint")
    assert "--constraint" not in run.inbox_path(K).read_text(encoding="utf-8")


# --------------------------------------------------------------------------- pending and the event


def test_pending_is_each_constraint_approval_with_no_override_after_it():
    a = {"type": "hitl_approved", "key": "a", "constraint": True}
    plain = {"type": "hitl_approved", "key": "b"}
    c = {"type": "hitl_approved", "key": "c", "constraint": True}
    early = {"type": "constraint_approved", "key": "c", "sha": "1"}  # before the approval: not its override
    done = {"type": "constraint_approved", "key": "a", "sha": "2"}
    assert constraint.pending([a, plain, early, c]) == [a, c]
    assert constraint.pending([a, plain, early, c, done]) == [c]
    assert constraint.pending([{"type": "hitl_approved", "key": "a", "constraint": "yes"}]) == []


def test_constraint_approved_needs_its_key_and_sha(run):
    with pytest.raises(EventError, match="sha"):
        run.emit("constraint_approved", key=K, gates=["floor"])
    with pytest.raises(EventError, match="key"):
        run.emit("constraint_approved", sha="abc", gates=["floor"])


def test_write_commits_once_moves_the_runner_head_and_touches_no_file(run, ccfg, repo):
    gate_failed_on(run, ccfg)
    approve(run, K, by="  Hải‮  ", constraint=True, cfg=ccfg)
    run.emit("run_updated", base_sha=run_git(repo, "rev-parse", "HEAD").strip())
    (repo / "README.md").write_text("work in progress\n", encoding="utf-8")
    status = run_git(repo, "status", "--porcelain")
    assert constraint.write(ccfg, run, repo) is True
    (ok,) = [e for e in run.events.read() if e["type"] == "constraint_approved"]
    assert ok["by"] == "Hải" and ok["key"] == K and ok["gates"] == ["floor"]
    assert run_git(repo, "rev-parse", "HEAD").strip() == ok["sha"] == runner_head(run)
    assert run_git(repo, "log", "-1", "--format=%B", ok["sha"]).rstrip() == (
        f"Approve the floor constraint for {K}\n\nHải approved what `floor` refused in `{K}` of run {run.id}.\n\n"
        f"{TRAILER}: Hải")
    assert run_git(repo, "rev-parse", f"{ok['sha']}^{{tree}}") == run_git(repo, "rev-parse", f"{ok['sha']}^^{{tree}}")
    assert run_git(repo, "status", "--porcelain") == status  # the index and the tree as they were
    assert constraint.write(ccfg, run, repo) is True  # nothing pending: nothing written again
    assert len([e for e in run.events.read() if e["type"] == "constraint_approved"]) == 1
    assert foreign_commits(run, ccfg, repo, run_git(repo, "rev-parse", "HEAD^").strip()) == []


def test_write_waits_while_the_run_waits_on_a_person(run, ccfg, repo):
    gate_failed_on(run, ccfg)
    approve(run, K, by="me", constraint=True, cfg=ccfg)
    run.emit("run_updated", base_sha=run_git(repo, "rev-parse", "HEAD").strip())
    raise_hitl(ccfg, run, "blocked", "build/build/task-3/implement", "stuck", notify=False)
    head = run_git(repo, "rev-parse", "HEAD")
    assert constraint.write(ccfg, run, repo) is True
    assert run_git(repo, "rev-parse", "HEAD") == head
    assert constraint.pending(run.events.read())


def test_a_moved_head_is_a_blocked_stop_and_nothing_is_written(run, ccfg, repo, monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr("agentflow.hitl.notify", lambda cfg, title, message, run=None: seen.append(message))
    gate_failed_on(run, ccfg)
    approve(run, K, by="me", constraint=True, cfg=ccfg)
    run.emit("run_updated", base_sha=run_git(repo, "rev-parse", "HEAD").strip())
    run_git(repo, "commit", "-q", "--allow-empty", "-m", "someone else's")
    head = run_git(repo, "rev-parse", "HEAD")
    assert constraint.write(ccfg, run, repo) is False
    assert run_git(repo, "rev-parse", "HEAD") == head
    assert "constraint_approved" not in kinds(run)
    waiting = run.state["waiting"]
    assert waiting["key"] == K and waiting["kind"] == "blocked" and "HEAD" in waiting["summary"]
    assert len(seen) == 1  # told at once: no resolver takes the person's override
    run_git(repo, "reset", "-q", "--soft", "HEAD^")  # the person puts HEAD back, then approves
    approve(run, K, by="me", cfg=ccfg)
    assert constraint.write(ccfg, run, repo) is True
    (ok,) = [e for e in run.events.read() if e["type"] == "constraint_approved"]
    assert ok["sha"] == run_git(repo, "rev-parse", "HEAD").strip()


def test_a_logged_approval_by_the_runner_writes_no_trailer(run, ccfg, repo):
    gate_failed_on(run, ccfg)
    run.emit("run_updated", base_sha=run_git(repo, "rev-parse", "HEAD").strip())
    run.emit("hitl_approved", by=AUTO_BY, key=K, kind="gate_failed", summary="x", constraint=True)  # not via approve
    head = run_git(repo, "rev-parse", "HEAD")
    assert constraint.write(ccfg, run, repo) is False
    assert run_git(repo, "rev-parse", "HEAD") == head and "constraint_approved" not in kinds(run)
    assert run.state["waiting"]["kind"] == "blocked"


def test_an_override_a_dead_runner_wrote_is_taken_not_written_twice(run, ccfg, repo, monkeypatch):
    gate_failed_on(run, ccfg)
    approve(run, K, by="me", constraint=True, cfg=ccfg)
    run.emit("run_updated", base_sha=run_git(repo, "rev-parse", "HEAD").strip())
    real = run.emit

    def die(etype, *args, **kwargs):
        if etype == "constraint_approved":
            raise KeyboardInterrupt  # the runner killed between the commit and its event
        return real(etype, *args, **kwargs)

    monkeypatch.setattr(run, "emit", die)
    with pytest.raises(KeyboardInterrupt):
        constraint.write(ccfg, run, repo)
    monkeypatch.setattr(run, "emit", real)
    written = run_git(repo, "rev-parse", "HEAD").strip()
    assert constraint.write(ccfg, run, repo) is True
    (ok,) = [e for e in run.events.read() if e["type"] == "constraint_approved"]
    assert ok["sha"] == written == run_git(repo, "rev-parse", "HEAD").strip()
    assert run_git(repo, "rev-list", "--count", "HEAD").strip() == "2"  # the initial commit and one override


def test_a_rejected_trailer_on_any_other_commit_still_counts(run, ccfg, repo):
    base = run_git(repo, "rev-parse", "HEAD").strip()
    gate_failed_on(run, ccfg)
    approve(run, K, by="me", constraint=True, cfg=ccfg)
    run.emit("run_updated", base_sha=base)
    assert constraint.write(ccfg, run, repo) is True
    run_git(repo, "commit", "-q", "--allow-empty", "-m", f"Sneak\n\n{TRAILER}: me")
    sneak = run_git(repo, "rev-parse", "HEAD").strip()
    run.emit("step_committed", key="build/build/task-3", sha=sneak, summary="Sneak")  # even a commit the runner made
    (line,) = foreign_commits(run, ccfg, repo, base)
    assert line.startswith(sneak[:12]) and f"rejected trailer {TRAILER}" in line


def test_the_override_sha_keeps_every_other_rejected_trailer(run, tmp_cfg, repo):
    cfg = load_config(tmp_cfg({"guardrails": {"reject_trailers": [TRAILER, "Signed-Off-By-Bot"],
                                              "constraint_override": {"trailer": TRAILER, "gates": ["floor"]}}}))
    base = run_git(repo, "rev-parse", "HEAD").strip()
    run_git(repo, "commit", "-q", "--allow-empty", "-m", f"Override\n\n{TRAILER}: me\nSigned-Off-By-Bot: x")
    sha = run_git(repo, "rev-parse", "HEAD").strip()
    run.emit("constraint_approved", by="me", key=K, sha=sha, gates=["floor"])
    (line,) = foreign_commits(run, cfg, repo, base)
    assert "Signed-Off-By-Bot" in line and f"{TRAILER}," not in line and not line.endswith(TRAILER)


# --------------------------------------------------------------------------- validate


def constraint_errors(tmp_cfg, override, **guardrails) -> list[tuple[str, str]]:
    path = tmp_cfg({"guardrails": {**guardrails, "constraint_override": override}})
    issues = validate(load_config(path), path.parent.parent)
    return [(i.path, i.message) for i in issues if i.level == "error" and "constraint_override" in i.path]


def test_validate_takes_a_full_override(tmp_cfg):
    assert constraint_errors(tmp_cfg, {"trailer": TRAILER, "gates": ["floor"]}) == []
    assert constraint_errors(tmp_cfg, None) == []  # unset
    assert constraint_errors(tmp_cfg, {}) == []


def test_validate_needs_the_trailer_in_reject_trailers(tmp_cfg):
    (error,) = constraint_errors(tmp_cfg, {"trailer": TRAILER, "gates": ["floor"]}, reject_trailers=[])
    assert error[0] == "guardrails.constraint_override.trailer"
    assert "a trailer only a person writes must be one the runner refuses from agents" in error[1]


@pytest.mark.parametrize("override, where", [
    ({"gates": ["floor"]}, "trailer"),
    ({"trailer": TRAILER}, "gates"),
    ({"trailer": TRAILER, "gates": []}, "gates"),
    ({"trailer": TRAILER, "gates": ["no_such_gate"]}, "gates"),
    ({"trailer": "Constraints Approved", "gates": ["floor"]}, "trailer"),
    ({"trailer": "X: y\nOther", "gates": ["floor"]}, "trailer"),
    ({"trailer": TRAILER, "gates": ["floor"], "gate": ["floor"]}, "gate"),
])
def test_validate_refuses_a_partial_or_unknown_override(tmp_cfg, override, where):
    assert [p for p, _ in constraint_errors(tmp_cfg, override)] == [f"guardrails.constraint_override.{where}"]


# --------------------------------------------------------------------------- the command line


def test_approve_constraint_on_the_command_line(run, ccfg, capsys):
    from agentflow.flow import main

    gate_failed_on(run, ccfg)
    assert main(["approve", run.id, K, "--constraint", "--by", "runner", "--no-continue",
                 "--config", str(ccfg.path)]) == 1
    assert "only a person" in capsys.readouterr().out and "hitl_approved" not in kinds(run)
    assert main(["approve", run.id, K, "--constraint", "--by", "Hải", "--no-continue", "--config", str(ccfg.path)]) == 0
    (ok,) = [e for e in run.events.read() if e["type"] == "hitl_approved"]
    assert ok["constraint"] is True and ok["by"] == "Hải"
