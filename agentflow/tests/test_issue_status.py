"""The `issues:` config keys: what a run started from an issue does on GitHub (the proposal's Config section)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from agentflow import config, issue_status, pipeline, run_control
from agentflow.config import ConfigError, Issue
from agentflow.feature_lane import render_pr_body
from agentflow.intake import ISSUE_FIELDS
from agentflow.pipeline import RunOptions, dry_run, start
from agentflow.run_control import ControlError
from agentflow.state import TERMINAL

BIN = Path(__file__).resolve().parent / "bin"
LABEL = "agentflow:in-progress"
ISSUE = json.dumps({"title": "Login breaks", "body": "SECRET-BODY", "url": "https://github.com/example/repo/issues/7"})


@pytest.fixture
def gh(tmp_path, monkeypatch):
    log = tmp_path / "gh.jsonl"
    monkeypatch.setenv("PATH", f"{BIN}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_GH_LOG", str(log))
    monkeypatch.setenv("FAKE_GH_ISSUE", ISSUE)
    return log


def calls(log: Path) -> list[list[str]]:
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line]


def removes(log: Path) -> list[list[str]]:
    return [c for c in calls(log) if "--remove-label" in c]


def begin(flow_repo, params=None):
    return start(flow_repo.cfg, flow_repo.root, "bug", params or {"issue": "7"}, RunOptions())


def _error_paths(flow_repo, patch: dict) -> list[str]:
    """The paths a config with `patch` under `issues:` is refused at: from `load_config` or from `validate`."""
    try:
        cfg = flow_repo.configure({"issues": patch})
    except ConfigError as exc:
        return [exc.path]
    issues: list[Issue] = config.validate(cfg, flow_repo.root)
    return [i.path for i in issues if i.level == "error"]


def test_the_issue_keys_default_from_the_engine(flow_repo):
    cfg = flow_repo.cfg
    assert (cfg.issues.in_progress_label, cfg.issues.comment, cfg.issues.epic_labels) == (
        "agentflow:in-progress", True, ("epic",))


def test_a_config_built_by_hand_has_the_default_issue_policy():
    assert config.IssuePolicy() == config.IssuePolicy("agentflow:in-progress", True, ("epic",))


@pytest.mark.parametrize("patch, path", [
    ({"in_progress_label": "a,b"}, "issues.in_progress_label"),
    ({"in_progress_label": "two\nlines"}, "issues.in_progress_label"),
    ({"in_progress_label": "x" * 51}, "issues.in_progress_label"),
    ({"in_progress_label": "-rf"}, "issues.in_progress_label"),
    ({"comment": "yes"}, "issues.comment"),
    ({"epic_labels": "epic"}, "issues.epic_labels"),
    ({"epic_labels": [""]}, "issues.epic_labels"),
    ({"epic_labels": ["x" * 51]}, "issues.epic_labels"),
])
def test_validate_refuses_bad_issue_keys(flow_repo, patch, path):
    assert path in _error_paths(flow_repo, patch)


def test_the_issue_sync_keys_default_to_off(flow_repo):
    cfg = flow_repo.cfg
    assert (cfg.issues.sync_label, cfg.issues.sync_lane, cfg.issues.lane_labels) == ("", "feature", {"bug": "bug"})


def _warnings(flow_repo, patch: dict) -> list[str]:
    cfg = flow_repo.configure({"issues": patch})
    return [i.path for i in config.validate(cfg, flow_repo.root) if i.level == "warning"]


@pytest.mark.parametrize("patch, path", [
    ({"sync_label": "a,b"}, "issues.sync_label"),
    ({"sync_label": "-x"}, "issues.sync_label"),
    ({"sync_label": "Agentflow:In-Progress"}, "issues.sync_label"),  # GitHub compares labels without case
    ({"sync_label": "agentflow", "sync_lane": "nope"}, "issues.sync_lane"),
    ({"lane_labels": {"a,b": "bug"}}, "issues.lane_labels.a,b"),
    ({"lane_labels": {"-x": "bug"}}, "issues.lane_labels.-x"),
])
def test_validate_refuses_bad_issue_sync_keys(flow_repo, patch, path):
    assert path in _error_paths(flow_repo, patch)


def test_a_lane_label_naming_no_lane_is_a_warning_only_while_the_sync_is_on(flow_repo):
    patch = {"lane_labels": {"bug": "bug", "chore": "nope"}}
    assert "issues.lane_labels.chore" not in _warnings(flow_repo, patch)
    assert "issues.lane_labels.chore" in _warnings(flow_repo, {**patch, "sync_label": "agentflow"})
    assert _error_paths(flow_repo, {**patch, "sync_label": "agentflow"}) == []


def test_a_synced_issue_in_a_capability_map_module_lane_needs_a_module_row(flow_repo):
    cfg = flow_repo.configure({"issues": {"sync_label": "agentflow"},
                               "dashboard": {"backlog": {"module_row": None,
                                                         "new_task": {"feature": ["issue"]}}}})
    paths = [i.path for i in config.validate(cfg, flow_repo.root) if i.level == "error"]
    assert "issues.sync_label" in paths and "dashboard.backlog.new_task.feature" in paths


def test_the_sync_without_a_module_lane_warns(flow_repo):
    cfg = flow_repo.configure({"issues": {"sync_label": "agentflow"},
                               "dashboard": {"backlog": {"module_lane": None}}})
    assert "issues.sync_label" in [i.path for i in config.validate(cfg, flow_repo.root) if i.level == "warning"]


def test_validate_refuses_an_epic_label_with_a_newline(flow_repo):
    assert "issues.epic_labels" in _error_paths(flow_repo, {"epic_labels": ["a\nb"]})


@pytest.mark.parametrize("patch", [
    {"in_progress_label": "", "epic_labels": []},
    {"in_progress_label": "status: in progress"},
    {"in_progress_label": "x" * 50, "epic_labels": ["x" * 50, "initiative"]},
])
def test_validate_accepts_an_empty_label_and_no_epic_labels(flow_repo, patch):
    cfg = flow_repo.configure({"issues": patch})
    assert [i for i in config.validate(cfg, flow_repo.root) if i.level == "error"] == []
    assert cfg.issues.in_progress_label == patch.get("in_progress_label", "agentflow:in-progress")
    assert cfg.issues.epic_labels == tuple(patch.get("epic_labels", ["epic"]))


# --------------------------------------------------------------------------- mark


def test_a_run_from_an_issue_labels_it_and_comments_once(flow_repo, gh):
    run = begin(flow_repo)
    log = calls(gh)
    view = log.index(["issue", "view", "7", "--json", ISSUE_FIELDS])
    edit = log.index(["issue", "edit", "7", "--add-label", LABEL])
    comment = next(i for i, c in enumerate(log) if c[:3] == ["issue", "comment", "7"])
    assert view < edit < comment
    assert log[comment][3:] == ["--body-file", str(run.dir / "issue-comment.md")]
    text = (run.dir / "issue-comment.md").read_text(encoding="utf-8")
    assert run.id in text and "(bug)" in text and run.state["branch"] in text
    assert LABEL in text and "SECRET-BODY" not in text and "Login breaks" not in text
    marked = flow_repo.events(run, "issue_marked")
    assert [{k: e[k] for k in ("issue", "label", "commented")} for e in marked] == [
        {"issue": "7", "label": LABEL, "commented": True}]
    assert len([c for c in log if c[:2] == ["issue", "comment"]]) == 1


def test_an_empty_label_makes_no_edit_and_no_comment(flow_repo, gh):
    flow_repo.configure({"issues": {"in_progress_label": ""}})
    run = begin(flow_repo)
    assert not [c for c in calls(gh) if c[:2] in (["issue", "edit"], ["issue", "comment"])]
    assert not flow_repo.events(run, "issue_marked")


def test_comment_false_labels_without_a_comment(flow_repo, gh):
    flow_repo.configure({"issues": {"comment": False}})
    run = begin(flow_repo)
    log = calls(gh)
    assert ["issue", "edit", "7", "--add-label", LABEL] in log
    assert not [c for c in log if c[:2] == ["issue", "comment"]]
    assert [e["commented"] for e in flow_repo.events(run, "issue_marked")] == [False]
    assert not (run.dir / "issue-comment.md").exists()


def test_a_run_without_an_issue_makes_no_issue_call(flow_repo, gh):
    begin(flow_repo, {"task": "x"})
    assert not [c for c in calls(gh) if c[0] == "issue"]


def test_a_dry_run_makes_no_issue_edit(flow_repo, gh, capsys):
    dry_run(flow_repo.cfg, flow_repo.root, "bug", {"issue": "7"}, RunOptions())
    assert not [c for c in calls(gh) if c[:2] in (["issue", "edit"], ["issue", "comment"])]


def test_a_failed_label_add_is_recorded_and_the_run_goes_on(flow_repo, gh, monkeypatch):
    monkeypatch.setenv("FAKE_GH_FAIL_ON", "issue edit")
    run = begin(flow_repo)
    assert run.state["status"] == "running"
    failed = flow_repo.events(run, "issue_status_failed")
    assert [e["action"] for e in failed] == ["mark"] and failed[0]["error"]
    assert not flow_repo.events(run, "issue_marked")
    assert not [c for c in calls(gh) if c[:2] == ["issue", "comment"]]


def test_a_missing_label_is_created_once_and_the_add_retried(flow_repo, gh, monkeypatch):
    monkeypatch.setenv("FAKE_GH_NO_LABEL", "1")
    run = begin(flow_repo)
    log = [c for c in calls(gh) if c[0] in ("issue", "label") and c[1] != "view"]
    add = ["issue", "edit", "7", "--add-label", LABEL]
    create = ["label", "create", LABEL, "--description", "agentflow is working on this issue"]
    assert log[:3] == [add, create, add]
    assert not [c for c in calls(gh) if "--force" in c]
    assert len(flow_repo.events(run, "issue_marked")) == 1
    assert not flow_repo.events(run, "issue_status_failed")


def test_a_failed_label_create_stops_without_marking(flow_repo, gh, monkeypatch):
    monkeypatch.setenv("FAKE_GH_NO_LABEL", "1")
    monkeypatch.setenv("FAKE_GH_FAIL_ON", "label create")
    run = begin(flow_repo)
    assert [e["action"] for e in flow_repo.events(run, "issue_status_failed")] == ["label_create"]
    assert not flow_repo.events(run, "issue_marked")
    assert not [c for c in calls(gh) if c[:2] == ["issue", "comment"]]
    assert run.state["status"] == "running"


def test_a_failed_comment_still_marks_so_the_label_comes_off(flow_repo, gh, monkeypatch):
    monkeypatch.setenv("FAKE_GH_FAIL_ON", "issue comment")
    run = begin(flow_repo)
    assert [e["action"] for e in flow_repo.events(run, "issue_status_failed")] == ["comment"]
    assert [e["commented"] for e in flow_repo.events(run, "issue_marked")] == [False]


def test_a_label_with_spaces_is_one_argv_element(flow_repo, gh):
    flow_repo.configure({"issues": {"in_progress_label": "status: in progress"}})
    begin(flow_repo)
    assert ["issue", "edit", "7", "--add-label", "status: in progress"] in calls(gh)


# --------------------------------------------------------------------------- release


def _issue_edits(log: Path) -> list[list[str]]:
    return [c for c in calls(log) if c[:2] == ["issue", "edit"]]


def test_cancel_removes_the_label_once(flow_repo, gh):
    run = begin(flow_repo)
    run_control.cancel(run, by="me", grace_s=0)
    assert removes(gh) == [["issue", "edit", "7", "--remove-label", LABEL]]
    assert [e["label"] for e in flow_repo.events(run, "issue_released")] == [LABEL]
    issue_status.release(run)
    assert len(removes(gh)) == 1
    assert len(flow_repo.events(run, "issue_released")) == 1


def test_cancel_stops_the_agents_before_it_releases_the_label(flow_repo, gh, monkeypatch):
    order: list[str] = []
    real_settle, real_release = run_control._settle, run_control.release
    monkeypatch.setattr(run_control, "_settle", lambda *a, **k: (order.append("settle"), real_settle(*a, **k))[1])
    monkeypatch.setattr(run_control, "release", lambda r: (order.append("release"), real_release(r))[1])
    run = begin(flow_repo)
    run_control.cancel(run, by="me", grace_s=0)
    assert order == ["settle", "release"]
    assert len(removes(gh)) == 1


def test_cancel_releases_the_label_even_when_stopping_the_agents_raises(flow_repo, gh, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("settle failed")

    monkeypatch.setattr(run_control, "_settle", boom)
    run = begin(flow_repo)
    with pytest.raises(RuntimeError, match="settle failed"):
        run_control.cancel(run, by="me", grace_s=0)
    assert len(removes(gh)) == 1


def test_cancelling_an_unmarked_run_makes_no_issue_edit(flow_repo, gh):
    flow_repo.configure({"issues": {"in_progress_label": ""}})
    run = begin(flow_repo)
    run_control.cancel(run, by="me", grace_s=0)
    assert not _issue_edits(gh)
    assert not flow_repo.events(run, "issue_released")


@pytest.mark.parametrize("event, data", [
    ("hitl_raised", {"kind": "blocked", "key": "build/x", "summary": "stop"}),
    ("run_paused", {"by": "me"}),
    ("phase_ready", {"by": "me", "phase": "define"}),
    ("runner_error", {"error": "x", "exception": "RuntimeError"}),
])
def test_a_run_that_is_not_over_keeps_its_label(flow_repo, gh, event, data):
    run = begin(flow_repo)
    run.emit(event, **data)
    assert run.state["status"] not in TERMINAL
    issue_status.release(run)
    assert not removes(gh)
    assert not flow_repo.events(run, "issue_released")


def test_a_runner_error_keeps_the_label(flow_repo, gh, monkeypatch):
    run = begin(flow_repo)

    def boom(self):
        raise RuntimeError("boom")

    monkeypatch.setattr(pipeline._Loop, "go", boom)
    with pytest.raises(RuntimeError, match="boom"):
        pipeline.advance(run, flow_repo.cfg)
    run.refresh()
    assert run.state["status"] == "error"
    assert not removes(gh)
    assert not flow_repo.events(run, "issue_released")


def test_an_ended_run_is_released_when_its_runner_exits(flow_repo, gh, monkeypatch):
    run = begin(flow_repo)

    def fail(self):
        self.run.emit("run_failed", reason="x")
        return "failed"

    monkeypatch.setattr(pipeline._Loop, "go", fail)
    assert pipeline.advance(run, flow_repo.cfg) == "failed"
    assert removes(gh) == [["issue", "edit", "7", "--remove-label", LABEL]]
    assert len(flow_repo.events(run, "issue_released")) == 1


def test_release_takes_the_label_from_the_event(flow_repo, gh):
    run = begin(flow_repo)
    flow_repo.configure({"issues": {"in_progress_label": "other"}})
    run_control.cancel(run, by="me", cfg=flow_repo.cfg, grace_s=0)
    assert removes(gh) == [["issue", "edit", "7", "--remove-label", LABEL]]


def test_a_failed_release_is_retried_at_archive(flow_repo, gh, monkeypatch):
    run = begin(flow_repo)
    monkeypatch.setenv("FAKE_GH_FAIL_ON", "issue edit")
    run_control.cancel(run, by="me", grace_s=0)
    failed = flow_repo.events(run, "issue_status_failed")
    assert [e["action"] for e in failed] == ["release"] and failed[0]["error"]
    assert not flow_repo.events(run, "issue_released")
    assert run.state["status"] == "cancelled"
    monkeypatch.delenv("FAKE_GH_FAIL_ON")
    run_control.archive(run, by="me")
    assert len(removes(gh)) == 2
    assert [e["label"] for e in flow_repo.events(run, "issue_released")] == [LABEL]
    before = len(calls(gh))
    with pytest.raises(ControlError, match="already archived"):
        run_control.archive(run, by="me")
    assert len(calls(gh)) == before


def test_archiving_a_released_run_makes_no_gh_call(flow_repo, gh):
    run = begin(flow_repo)
    run_control.cancel(run, by="me", grace_s=0)
    before = calls(gh)
    run_control.archive(run, by="me")
    assert calls(gh) == before
    assert len(flow_repo.events(run, "issue_released")) == 1


def test_a_killed_runner_keeps_the_label_until_cancel(flow_repo, gh):
    run = begin(flow_repo)
    run.refresh()
    assert run.state["status"] == "running"
    issue_status.release(run)
    assert not removes(gh) and not flow_repo.events(run, "issue_released")
    run_control.cancel(run, by="me", grace_s=0)
    assert len(removes(gh)) == 1
    assert len(flow_repo.events(run, "issue_released")) == 1


# --------------------------------------------------------------------------- the PR body's issue line


def second_line(run) -> str:
    return render_pr_body(run).split("\n\n")[1]


def test_an_issue_run_body_says_closes(flow_repo, gh):
    run = begin(flow_repo)
    assert second_line(run) == "Closes #7"
    assert not run.state["params"].get("issue_epic")


def test_the_line_ignores_the_issue_keys(flow_repo, gh):
    flow_repo.configure({"issues": {"in_progress_label": "", "comment": False}})
    assert second_line(begin(flow_repo)) == "Closes #7"


@pytest.mark.parametrize("labels, epic_labels, line", [
    ([{"name": "Epic"}], None, "Refs #7"),
    ([{"name": "bug"}], None, "Closes #7"),
    (None, None, "Closes #7"),  # no labels key at all
    ([{"name": "Epic"}], [], "Closes #7"),
])
def test_an_epic_label_gives_refs(flow_repo, gh, monkeypatch, labels, epic_labels, line):
    issue = json.loads(ISSUE)
    if labels is not None:
        issue["labels"] = labels
    monkeypatch.setenv("FAKE_GH_ISSUE", json.dumps(issue))
    if epic_labels is not None:
        flow_repo.configure({"issues": {"epic_labels": epic_labels}})
    run = begin(flow_repo)
    assert second_line(run) == line
    assert bool(run.state["params"].get("issue_epic")) == (line == "Refs #7")
    text = (run.dir / "issue.md").read_text(encoding="utf-8")
    assert "Epic" not in text and "bug" not in text.replace("Login breaks", "")
    assert "Epic" not in json.dumps(run.state["params"])


def test_a_later_label_edit_does_not_change_the_line(flow_repo, gh, monkeypatch):
    monkeypatch.setenv("FAKE_GH_ISSUE", json.dumps({**json.loads(ISSUE), "labels": [{"name": "epic"}]}))
    run = begin(flow_repo)
    monkeypatch.setenv("FAKE_GH_ISSUE", ISSUE)
    flow_repo.configure({"issues": {"epic_labels": []}})
    assert second_line(run) == "Refs #7"


def test_a_hash_issue_reference_gives_one_hash(flow_repo, gh):
    assert second_line(begin(flow_repo, {"issue": "#7"})) == "Closes #7"


@pytest.mark.parametrize("labels", [None, "epic", [3], [{"name": 3}], [{"title": "epic"}]])
def test_odd_label_shapes_are_not_an_epic(flow_repo, gh, monkeypatch, labels):
    monkeypatch.setenv("FAKE_GH_ISSUE", json.dumps({**json.loads(ISSUE), "labels": labels}))
    run = begin(flow_repo)
    assert second_line(run) == "Closes #7"


def test_a_run_without_an_issue_has_no_issue_line(flow_repo, gh):
    run = begin(flow_repo, {"task": "x"})
    assert second_line(run).startswith("Plan:")


# --------------------------------------------------------------------------- the Development link


SMALL = {"gate_sets": {"build": ["ok"], "verify": ["ok"]}}
PR_URL = "https://github.com/example/repo/pull/1"
CLOSING_QUERY = "id,closingIssuesReferences"


def begin_ship(flow_repo, monkeypatch, issues=None, issue=None):
    """A feature run from issue 7 that starts at `ship`, so `advance` opens its PR (test_feature_lane's setup).

    With no `issue` reply, the fake's default one answers, and it carries the issue's node id `I_fake7`."""
    flow_repo.configure({**SMALL, **({"issues": issues} if issues is not None else {})})
    if issue is None:
        monkeypatch.delenv("FAKE_GH_ISSUE", raising=False)
    else:
        monkeypatch.setenv("FAKE_GH_ISSUE", json.dumps(issue))
    return start(flow_repo.cfg, flow_repo.root, "feature", {"module": "demo", "issue": "7"},
                 RunOptions(from_phase="ship"))


def after_create(log: Path) -> list[list[str]]:
    """The gh calls after the `pr create` call."""
    log_calls = calls(log)
    create = next(i for i, c in enumerate(log_calls) if c[:2] == ["pr", "create"])
    return log_calls[create + 1:]


def closing_queries(log: Path) -> list[list[str]]:
    return [c for c in calls(log) if c[:2] == ["pr", "view"] and "--json" in c
            and "closingIssuesReferences" in c[c.index("--json") + 1]]


def api_calls(log: Path) -> list[list[str]]:
    return [c for c in calls(log) if c[0] == "api"]


def linked(flow_repo, run) -> list[dict]:
    return [{k: e[k] for k in ("issue", "pr", "how")} for e in flow_repo.events(run, "issue_linked")]


def test_a_finished_issue_run_links_by_keyword_and_releases(flow_repo, gh, monkeypatch):
    monkeypatch.setenv("FAKE_GH_CLOSING", "7")
    run = begin_ship(flow_repo, monkeypatch)
    assert pipeline.advance(run, flow_repo.cfg, RunOptions()) == "done"
    after = after_create(gh)
    assert ["pr", "view", PR_URL, "--json", CLOSING_QUERY] in after
    assert not [c for c in after if c[0] == "api"]
    assert not [c for c in after if c[:2] == ["issue", "comment"]]
    assert linked(flow_repo, run) == [{"issue": "7", "pr": PR_URL, "how": "keyword"}]
    assert removes(gh) == [["issue", "edit", "7", "--remove-label", LABEL]]
    assert (run.dir / "pr.md").read_text(encoding="utf-8").split("\n\n")[1] == "Closes #7"


def test_an_unlinked_pr_is_linked_through_the_api(flow_repo, gh, monkeypatch):
    run = begin_ship(flow_repo, monkeypatch)
    assert pipeline.advance(run, flow_repo.cfg, RunOptions()) == "done"
    after = after_create(gh)
    first = after.index(["pr", "view", PR_URL, "--json", CLOSING_QUERY])
    view = after.index(["issue", "view", "7", "--json", "id"])
    mutation = after.index(["api", "graphql", "-f", f"query={issue_status.LINK_MUTATION}",
                            "-f", "issue=I_fake7", "-f", "pr=PR_fake"])
    again = after.index(["pr", "view", PR_URL, "--json", "closingIssuesReferences"])
    assert first < view < mutation < again
    assert linked(flow_repo, run) == [{"issue": "7", "pr": PR_URL, "how": "api"}]
    assert not [c for c in after if c[:2] == ["issue", "comment"]]
    assert not flow_repo.events(run, "issue_status_failed")


def test_the_mutation_is_the_constant_from_the_spec():
    assert issue_status.LINK_MUTATION == (
        "mutation($issue: ID!, $pr: ID!) { addCloseIssueReferences(input: {issueId: $issue, pullRequestIds: [$pr]})"
        " { clientMutationId } }")


def test_a_link_github_ignores_is_a_failure(flow_repo, gh, monkeypatch):
    monkeypatch.setenv("FAKE_GH_LINK_IGNORED", "1")
    run = begin_ship(flow_repo, monkeypatch)
    assert pipeline.advance(run, flow_repo.cfg, RunOptions()) == "done"
    assert len(api_calls(gh)) == 1
    assert [{k: e[k] for k in ("action", "error")} for e in flow_repo.events(run, "issue_status_failed")] == [
        {"action": "link", "error": "GitHub does not list the link"}]
    assert not flow_repo.events(run, "issue_linked")


@pytest.mark.parametrize("pr_id", ["x; rm -rf /", ""])
def test_a_bad_node_id_never_reaches_an_argv(flow_repo, gh, monkeypatch, pr_id):
    monkeypatch.setenv("FAKE_GH_PR_ID", pr_id)
    run = begin_ship(flow_repo, monkeypatch)
    assert pipeline.advance(run, flow_repo.cfg, RunOptions()) == "done"
    assert closing_queries(gh)  # the link code ran and read the bad id
    assert not api_calls(gh)
    failed = flow_repo.events(run, "issue_status_failed")
    assert [e["action"] for e in failed] == ["link"] and "node id" in failed[0]["error"]
    assert not flow_repo.events(run, "issue_linked")
    assert not [c for c in calls(gh) if pr_id and pr_id in c]


def test_a_bad_issue_node_id_never_reaches_an_argv(flow_repo, gh, monkeypatch):
    run = begin_ship(flow_repo, monkeypatch, issue={**json.loads(ISSUE), "id": "I_7 --x"})
    assert pipeline.advance(run, flow_repo.cfg, RunOptions()) == "done"
    assert ["issue", "view", "7", "--json", "id"] in calls(gh)
    assert not api_calls(gh)
    assert [e["action"] for e in flow_repo.events(run, "issue_status_failed")] == ["link"]


def test_a_reply_that_is_not_json_is_a_failure(flow_repo, gh, monkeypatch):
    run = begin_ship(flow_repo, monkeypatch)
    monkeypatch.setattr(issue_status, "_gh_out", lambda repo, args: "not json")
    run.emit("pr_opened", url=PR_URL, key="ship/finish")
    issue_status.link(run)
    assert [e["action"] for e in flow_repo.events(run, "issue_status_failed")] == ["link"]
    assert not flow_repo.events(run, "issue_linked")


def test_deeply_nested_json_is_a_failure(flow_repo, gh, monkeypatch):
    run = begin_ship(flow_repo, monkeypatch)
    monkeypatch.setattr(issue_status, "_gh_out", lambda repo, args: "[" * 100000 + "]" * 100000)
    run.emit("pr_opened", url=PR_URL, key="ship/finish")
    issue_status.link(run)
    assert [e["action"] for e in flow_repo.events(run, "issue_status_failed")] == ["link"]
    assert not flow_repo.events(run, "issue_linked")


@pytest.mark.parametrize("closing, how", [("7", "keyword"), ("", "api")])
def test_a_resumed_finish_whose_pr_was_opened_still_links(flow_repo, gh, monkeypatch, closing, how):
    """A runner stopped between `pr_opened` and the link: on resume the finish step links instead of skipping it."""
    monkeypatch.setenv("FAKE_GH_CLOSING", closing)
    run = begin_ship(flow_repo, monkeypatch)
    run.emit("pr_opened", url=PR_URL, key="ship/finish")
    assert pipeline.advance(run, flow_repo.cfg, RunOptions()) == "done"
    assert not [c for c in calls(gh) if c[:2] == ["pr", "create"]]  # the PR was not opened again
    assert linked(flow_repo, run) == [{"issue": "7", "pr": PR_URL, "how": how}]
    assert len(api_calls(gh)) == (how == "api")


def test_an_epic_gets_a_comment_and_no_development_link(flow_repo, gh, monkeypatch):
    run = begin_ship(flow_repo, monkeypatch, issue={**json.loads(ISSUE), "labels": [{"name": "epic"}]})
    assert run.state["params"]["issue_epic"] is True
    assert pipeline.advance(run, flow_repo.cfg, RunOptions()) == "done"
    assert not closing_queries(gh)
    assert not api_calls(gh)
    comments = [c for c in after_create(gh) if c[:2] == ["issue", "comment"]]
    assert comments == [["issue", "comment", "7", "--body-file", str(run.dir / "issue-link.md")]]
    text = (run.dir / "issue-link.md").read_text(encoding="utf-8")
    assert PR_URL in text and run.id in text and "won't close" in text
    assert "SECRET-BODY" not in text and "Login breaks" not in text and "epic" not in text.replace("an epic", "")
    assert linked(flow_repo, run) == [{"issue": "7", "pr": PR_URL, "how": "comment"}]
    assert (run.dir / "pr.md").read_text(encoding="utf-8").split("\n\n")[1] == "Refs #7"
    assert not [c for c in calls(gh) if PR_URL in c and c[0] == "issue"]  # the URL goes in the file, not the argv


def test_the_issue_keys_never_turn_the_link_off(flow_repo, gh, monkeypatch):
    run = begin_ship(flow_repo, monkeypatch, issues={"in_progress_label": "", "comment": False})
    assert pipeline.advance(run, flow_repo.cfg, RunOptions()) == "done"
    assert closing_queries(gh)
    assert len(api_calls(gh)) == 1
    assert [e["how"] for e in flow_repo.events(run, "issue_linked")] == ["api"]
    assert (run.dir / "pr.md").read_text(encoding="utf-8").split("\n\n")[1] == "Closes #7"


def test_a_failed_link_is_retried_at_archive(flow_repo, gh, monkeypatch):
    monkeypatch.setenv("FAKE_GH_FAIL_ON", "api graphql")
    run = begin_ship(flow_repo, monkeypatch)
    assert pipeline.advance(run, flow_repo.cfg, RunOptions()) == "done"
    assert [e["action"] for e in flow_repo.events(run, "issue_status_failed")] == ["link"]
    assert not flow_repo.events(run, "issue_linked")
    monkeypatch.delenv("FAKE_GH_FAIL_ON")
    before = len(api_calls(gh))
    run_control.archive(run, by="me")
    assert len(api_calls(gh)) == before + 1
    assert [e["how"] for e in flow_repo.events(run, "issue_linked")] == ["api"]
    seen = len(calls(gh))
    issue_status.link(run)
    assert len(calls(gh)) == seen
    assert len(flow_repo.events(run, "issue_linked")) == 1


def test_archiving_a_linked_run_makes_no_gh_call(flow_repo, gh, monkeypatch):
    run = begin_ship(flow_repo, monkeypatch)
    assert pipeline.advance(run, flow_repo.cfg, RunOptions()) == "done"
    seen = len(calls(gh))
    run_control.archive(run, by="me")
    assert len(calls(gh)) == seen


def test_the_link_is_made_before_a_conflict_stops_the_run(flow_repo, gh, monkeypatch):
    monkeypatch.setenv("FAKE_GH_MERGEABLE", "CONFLICTING")
    run = begin_ship(flow_repo, monkeypatch)
    assert pipeline.advance(run, flow_repo.cfg, RunOptions()).startswith("waiting")
    assert [e["how"] for e in flow_repo.events(run, "issue_linked")] == ["api"]
    assert not removes(gh)


@pytest.mark.parametrize("closing, how", [("7", "keyword"), ("", "api")])
def test_a_run_on_the_derived_module_of_an_issue_marks_it_and_links_its_pr(flow_repo, gh, monkeypatch, closing, how):
    """What a synced issue or an issue-only feature card starts: `--module issue-7 --issue 7`."""
    monkeypatch.setenv("FAKE_GH_CLOSING", closing)
    flow_repo.configure(SMALL)
    monkeypatch.delenv("FAKE_GH_ISSUE", raising=False)
    run = start(flow_repo.cfg, flow_repo.root, "feature", {"issue": "7", "module": "issue-7"},
                RunOptions(from_phase="ship"))
    assert run.state["branch"].endswith("/issue-7-issue-7")
    assert (run.dir / "issue.md").is_file()
    assert ["issue", "edit", "7", "--add-label", LABEL] in calls(gh)
    assert pipeline.advance(run, flow_repo.cfg, RunOptions()) == "done"
    assert linked(flow_repo, run) == [{"issue": "7", "pr": PR_URL, "how": how}]
    assert (run.dir / "pr.md").read_text(encoding="utf-8").split("\n\n")[1] == "Closes #7"
    assert removes(gh) == [["issue", "edit", "7", "--remove-label", LABEL]]


def test_a_feature_run_without_an_issue_makes_no_link_call(flow_repo, gh):
    flow_repo.configure(SMALL)
    run = start(flow_repo.cfg, flow_repo.root, "feature", {"module": "demo"}, RunOptions(from_phase="ship"))
    assert pipeline.advance(run, flow_repo.cfg, RunOptions()) == "done"
    assert not closing_queries(gh)
    assert not api_calls(gh)
    assert not [c for c in calls(gh) if c[0] == "issue"]
    assert not flow_repo.events(run, "issue_linked")
