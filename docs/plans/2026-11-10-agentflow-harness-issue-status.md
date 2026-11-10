# Issue status Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A run started from `--issue N` labels the issue in progress and comments on it, removes the label when the run ends, and puts its draft PR in the issue's Development section (an epic gets `Refs #N` and a comment instead).

**Architecture:** A new module, `agentflow/issue_status.py`, holds three entry points (`mark`, `release`, `link`) that never raise for a GitHub problem. Each records what it did as an event, and `release`/`link` are idempotent because they read the event log. They are called from the five places a run starts, opens its PR or ends (spec, "Call sites"). The PR body's issue line and the epic flag come from the run's recorded `params` only, so no config key and no later label edit can change them.

**Tech Stack:** Python 3.12+, stdlib + `pyyaml>=6`, `gh` through `gitops._gh`, pytest with the fake agent and the fake `gh` (`agentflow/tests/bin/gh`).

**Spec:** `docs/specs/agentflow-issue-status-proposal.md` (status `approved`, 2026-11-10). Its design note is `docs/agentflow-harness-issue-status.md`. Read the spec's Design and Boundaries sections before any task: this plan doesn't repeat them.

## Global Constraints

- Engine dependencies stay stdlib + `pyyaml>=6`. No third-party import.
- Every `gh` call goes through `gitops._gh` (list argv, `stdin=DEVNULL`, 120 s timeout). Nothing reaches a shell.
- A GitHub failure (`GhError` or `OSError`) never fails, stops or blocks a run: catch it at the edge and emit `issue_status_failed {action, error}`.
- No issue text (title, body or label names) goes into a comment, the PR body or an argv. The comment bodies are built from the run id, lane, branch, label and PR URL only.
- Node ids must match `^[A-Za-z0-9_=-]{1,100}$` before they go into an argv, each one as its own `-f` element. The mutation text is a constant and nothing is spliced into it.
- Never use `--force` on `gh label create`. Never write `Closes` for an epic. No `issues.*` key turns the PR link off.
- Event names: `issue_marked {issue, label, commented}`, `issue_released {issue, label}`, `issue_linked {issue, pr, how}` (`how` ∈ `keyword | api | comment`), `issue_status_failed {action, error}` (`action` ∈ `mark | label_create | comment | release | link`).
- Every new config key ships with a Settings Form field, `validate` checks, a `test_config_edit.py` case and a playbook §14 line, in the same task.
- No project strings in the engine (`test_portability.py`). The golden files `agentflow/tests/fixtures/explain/*.txt` stay byte-identical.
- A user-visible change adds one line under `## [Unreleased]` in `CHANGELOG.md`.
- Interactive session: commit only when the developer asks. A commit message is one imperative sentence ending with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Test command (`T` below): `uv run --no-project --with pyyaml --with pytest pytest <files> --import-mode=prepend -q`. Never create or edit `uv.lock`.

## Decisions this plan adds to the spec

1. **Three Settings fields, not two.** The spec's Project Structure says "two Settings Form fields", but its config table gives each of the three keys a field. This plan follows the table.
2. **The label field accepts `""`.** `config_edit._check_value` refuses empty `text` today, and `""` is the label's off switch. A field definition may now carry `"allow_empty": True`, and only `issues.in_progress_label` uses it. `dashboard.html` needs no change: a field that isn't optional already sends the empty string.
3. **`gather` decides the epic flag.** `intake.gather(params, repo, epic_labels=())` compares the label names and sets `params["issue_epic"] = True` (the key is absent otherwise). `pipeline.start` passes `cfg.issues.epic_labels`. The effect is what the spec asks for (the flag is set before `Run.create`), and the label names never leave `intake`.
4. **`IssuePolicy` has defaults** (`field(default_factory=IssuePolicy)` on `Config`), so code that builds a `Config` by hand keeps working.

## Review Focus

- **The comment fails after the label was added.** Expected: `issue_marked` with `commented: false` is recorded, and the run's end still removes the label. Pinned in Task 2 (`test_a_failed_comment_still_marks_so_the_label_comes_off`).
- **The PR opens, then the run stops (a merge conflict).** Expected: `issue_linked` is already in the log, and the label stays because the run is `waiting`. Pinned in Task 5 (`test_the_link_is_made_before_a_conflict_stops_the_run`).
- **`--issue '#7'`.** Expected: the body line is `Closes #7`, never `Closes ##7`, and every gh argv says `7`. Pinned in Task 4 (`test_a_hash_issue_reference_gives_one_hash`).
- **A label with a space or a colon** (`status: in progress`). Expected: `validate` accepts it, and gh gets it as one argv element. Pinned in Task 2 (`test_a_label_with_spaces_is_one_argv_element`).
- **`labels` in an odd shape** (`null`, a non-list, an entry with no string `name`). Expected: not an epic, and the run starts. Pinned in Task 4 (`test_odd_label_shapes_are_not_an_epic`).

---

### Task 1: The `issues:` config keys

**Files:**
- Modify: `agentflow/defaults/config.yaml` (a new `issues:` block after `memory:`, line 72)
- Modify: `agentflow/config.py` (`IssuePolicy` beside `MemoryPolicy` at line 285, `Config.issues`, loading beside line 889, a `Validator.issues()` check called next to `v.memory()` at line 1720)
- Modify: `agentflow/config_edit.py` (a new "Issues" section in `_SECTIONS`, `allow_empty` in `_check_value`)
- Modify: `docs/agentflow-playbook.md` §14 (three lines, after the `memory.*` lines at 447-449)
- Test: `agentflow/tests/test_issue_status.py` (new), `agentflow/tests/test_config_edit.py`

**Interfaces:**
- Produces: `config.IssuePolicy(in_progress_label: str = "agentflow:in-progress", comment: bool = True, epic_labels: tuple[str, ...] = ("epic",))`, reached as `cfg.issues`.

- [ ] **Step 1: Write the failing tests**

In `test_issue_status.py`, using `flow_repo.configure(patch)` and `config.validate(cfg, repo)`:

```python
def test_the_issue_keys_default_from_the_engine(flow_repo):
    cfg = flow_repo.cfg
    assert (cfg.issues.in_progress_label, cfg.issues.comment, cfg.issues.epic_labels) == (
        "agentflow:in-progress", True, ("epic",))

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
    # either load_config raises ConfigError with this path, or validate returns an error Issue with it
    ...

def test_validate_accepts_an_empty_label_and_no_epic_labels(flow_repo):
    # {"in_progress_label": "", "epic_labels": []} and {"in_progress_label": "status: in progress"}: no error Issue
```

In `test_config_edit.py`, against `sparse_repo` (the shipped fixture text has no `issues:` block, so its form leaves these fields out):

```python
def test_the_issues_section_edits_round_trip(sparse_repo):
    path = _sparse(sparse_repo)
    fields = _fields(path)
    assert fields[("issues", "in_progress_label")]["source"] == "engine"
    proposal = _sparse_form(path, (["issues", "in_progress_label"], ""), (["issues", "comment"], False),
                            (["issues", "epic_labels"], ["epic", "initiative"]))
    assert not proposal.errors, proposal.errors
    config_edit.save(path, proposal)
    cfg = load_config(path)
    assert (cfg.issues.in_progress_label, cfg.issues.comment, cfg.issues.epic_labels) == ("", False, ("epic", "initiative"))

@pytest.mark.parametrize("key, value", [
    (["issues", "in_progress_label"], "a,b"),    # validate refuses it
    (["issues", "comment"], "no"),               # the field's kind
    (["issues", "epic_labels"], "epic"),         # not a list
])
def test_the_issues_section_refuses_bad_values(sparse_repo, key, value):
    path = _sparse(sparse_repo)
    try:
        proposal = _sparse_form(path, (key, value))
    except EditError:
        return
    assert proposal.errors
```

- [ ] **Step 2: Run them to verify they fail**

Run: `T agentflow/tests/test_issue_status.py agentflow/tests/test_config_edit.py -k "issue"`
Expected: FAIL (`Config` has no attribute `issues`, and no form field `issues.*`).

- [ ] **Step 3: Implement**

- `defaults/config.yaml`: the `issues:` block exactly as in the spec's Config section, comments included.
- `config.py`: load the block with `_optional(raw, "issues", "", dict, {})` and `_optional` per key, the same way `memory` is loaded. `epic_labels` becomes a tuple, and a non-list value is an error at `issues.epic_labels`. In `validate`, `in_progress_label` must be a string, at most 50 characters, with no newline, no comma, and no leading `-` (`""` is allowed). `comment` must be a bool. Each `epic_labels` entry must be a non-empty string of at most 50 characters with no newline.
- `config_edit.py`: a section `("Issues", "What a run started from an issue does on GitHub.", [...])` with `in_progress_label` (`{"kind": "text", "allow_empty": True}`), `comment` (`{"kind": "bool"}`) and `epic_labels` (`{"kind": "list"}`). In `_check_value`, a `text` field with `allow_empty` accepts `""`, but the newline and length checks still apply.
- Playbook §14: one line per key in the `memory.*` lines' form: the default, what `validate` wants, what `""` and `[]` mean, and "Not protected."

- [ ] **Step 4: Run the tests, then validate**

Run: `T agentflow/tests/test_issue_status.py agentflow/tests/test_config_edit.py agentflow/tests/test_portability.py`, then `bin/agentflow validate`, then `bin/agentflow config show --effective issues`.
Expected: the tests PASS, validate prints `0 warnings`, and all three keys show with the `engine` layer.

- [ ] **Step 5: Commit** (when the developer asks)

```bash
git add agentflow/defaults/config.yaml agentflow/config.py agentflow/config_edit.py docs/agentflow-playbook.md agentflow/tests/test_issue_status.py agentflow/tests/test_config_edit.py
git commit -m "Add the issues config keys for a run started from an issue."
```

---

### Task 2: Mark the issue when a run starts

**Files:**
- Create: `agentflow/issue_status.py`
- Modify: `agentflow/pipeline.py:122-143` (`start`: call `mark` after the `_set_up` try block)
- Modify: `agentflow/tests/bin/gh` (`$FAKE_GH_FAIL_ON`, `$FAKE_GH_NO_LABEL`, and the docstring)
- Test: `agentflow/tests/test_issue_status.py`

**Interfaces:**
- Consumes: `cfg.issues` (Task 1); `gitops._gh(args, cwd, timeout=120) -> CompletedProcess`; `gitops.GhError`; `safeio.write_text(path, text)`.
- Produces:
  - `issue_status.mark(cfg: Config, run: Run) -> None`
  - private `_gh_out(repo: Path, args: list[str]) -> str`: `_gh`'s stdout, with a non-zero exit raised as `GhError(stderr.strip() or f"gh exited {code}")`. Tasks 3 and 5 use it.
  - private `_failed(run: Run, action: str, exc: BaseException) -> None`: emits `issue_status_failed` with `error=str(exc) or type(exc).__name__`.
  - constants `COMMENT_FILE = "issue-comment.md"`, `LABEL_DESCRIPTION = "agentflow is working on this issue"`.

- [ ] **Step 1: Extend the fake gh**

`$FAKE_GH_FAIL_ON` is a space-separated argv prefix (`"issue edit"`). A call whose argv starts with it exits 1 and writes `fake gh: failing as asked` to stderr, after the call is logged. `$FAKE_GH_NO_LABEL`: the first `issue edit ... --add-label <l>` exits 1 with `could not add label: '<l>' not found` on stderr. A marker file `$FAKE_GH_LOG.nolabel` makes it fire once. `issue edit`, `issue comment` and `label create` otherwise fall through to the default `[]` / exit 0 branch.

- [ ] **Step 2: Write the failing tests**

Add a `gh` fixture like `test_feature_lane.gh` (PATH, `FAKE_GH_LOG`) and a `calls(log)` helper. Start runs with `start(flow_repo.cfg, flow_repo.root, "bug", {"issue": "7"}, RunOptions())`.

```python
def test_a_run_from_an_issue_labels_it_and_comments_once(flow_repo, gh):
    run = start(...)
    log = calls(gh)
    view = log.index(["issue", "view", "7", "--json", ISSUE_FIELDS])
    edit = log.index(["issue", "edit", "7", "--add-label", "agentflow:in-progress"])
    comment = next(i for i, c in enumerate(log) if c[:3] == ["issue", "comment", "7"])
    assert view < edit < comment
    assert log[comment][3:] == ["--body-file", str(run.dir / "issue-comment.md")]
    text = (run.dir / "issue-comment.md").read_text(encoding="utf-8")
    assert run.id in text and "bug" in text and run.state["branch"] in text
    assert "agentflow:in-progress" in text and "SECRET-BODY" not in text  # FAKE_GH_ISSUE body holds SECRET-BODY
    assert flow_repo.events(run, "issue_marked") == [... {"issue": "7", "label": "agentflow:in-progress", "commented": True}]

def test_an_empty_label_makes_no_edit_and_no_comment(flow_repo, gh): ...       # in_progress_label: ""
def test_comment_false_labels_without_a_comment(flow_repo, gh): ...           # commented: False
def test_a_run_without_an_issue_makes_no_issue_call(flow_repo, gh): ...       # {"task": "x"}: no call starts with "issue"
def test_a_dry_run_makes_no_issue_edit(flow_repo, gh): ...                    # pipeline.dry_run(..., {"issue": "7"}, ...)
def test_a_failed_label_add_is_recorded_and_the_run_goes_on(flow_repo, gh, monkeypatch):
    # FAKE_GH_FAIL_ON="issue edit": start returns a run, its status is "running",
    # issue_status_failed action "mark", no issue_marked, no issue comment
def test_a_missing_label_is_created_once_and_the_add_retried(flow_repo, gh, monkeypatch):
    # FAKE_GH_NO_LABEL=1: the log holds, in order, --add-label, ["label","create","agentflow:in-progress",
    # "--description","agentflow is working on this issue"], --add-label; no "--force" anywhere
def test_a_failed_comment_still_marks_so_the_label_comes_off(flow_repo, gh, monkeypatch):
    # FAKE_GH_FAIL_ON="issue comment": issue_status_failed action "comment", issue_marked commented False
def test_a_label_with_spaces_is_one_argv_element(flow_repo, gh):
    # in_progress_label: "status: in progress" -> ["issue","edit","7","--add-label","status: in progress"]
```

- [ ] **Step 3: Run them to verify they fail**

Run: `T agentflow/tests/test_issue_status.py`
Expected: the new tests FAIL (no `issue edit` in the log). The exceptions are the no-issue and dry-run tests, which already pass: they guard behaviour that must not change.

- [ ] **Step 4: Implement `mark` and call it from `pipeline.start`**

`mark` follows the spec's steps 1 to 3, with gh's cwd set to `run.repo`. "Label not found" is a `GhError` whose message contains `not found`: create the label (a failure is `action: "label_create"`, and the function stops), then try the add once more. The comment copy:

```
agentflow run `<run id>` (<lane>) is working on this issue, on branch `<branch>`.

The `<label>` label comes off when the run ends.
```

In `pipeline.start`, call `mark(cfg, run)` after the `try/except` around `_set_up` and before `return run`. A start that fails inside `_set_up` never reaches it. Update the module docstring of `tests/bin/gh` for the two knobs.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `T agentflow/tests/test_issue_status.py agentflow/tests/test_bug_eval_security.py agentflow/tests/test_backlog.py agentflow/tests/test_portability.py`
Expected: PASS. Fix any existing test that pins the exact gh log of an issue run: it now has the edit and the comment after `issue view`.

- [ ] **Step 6: Commit** (when the developer asks)

```bash
git add agentflow/issue_status.py agentflow/pipeline.py agentflow/tests/bin/gh agentflow/tests/test_issue_status.py
git commit -m "Label and comment on the issue a run starts from."
```

---

### Task 3: Release the label when the run ends

**Files:**
- Modify: `agentflow/issue_status.py` (`release`)
- Modify: `agentflow/pipeline.py:225-230` (`advance`'s `finally`, after `lock.release()`)
- Modify: `agentflow/run_control.py:132-140` (`archive`: after the `with run.locked()` block), `:262-275` (`cancel`: after the `with run.locked()` block)
- Modify: `docs/agentflow-playbook.md` §8 (one sentence after the table at line 255), `CHANGELOG.md` (`### Added`)
- Test: `agentflow/tests/test_issue_status.py`

**Interfaces:**
- Consumes: `mark` and `_gh_out` (Task 2); `state.TERMINAL`.
- Produces: `issue_status.release(run: Run) -> None` (the spec's Code Style shows it). Task 5 adds `link(run)` to `archive` ahead of it.

- [ ] **Step 1: Write the failing tests**

```python
def test_cancel_removes_the_label_once(flow_repo, gh):
    run = start(...); run_control.cancel(run, by="me", grace_s=0)
    assert removes(gh) == [["issue", "edit", "7", "--remove-label", "agentflow:in-progress"]]
    assert [e["label"] for e in flow_repo.events(run, "issue_released")] == ["agentflow:in-progress"]
    issue_status.release(run)
    assert len(removes(gh)) == 1

def test_cancelling_an_unmarked_run_makes_no_issue_edit(flow_repo, gh): ...   # in_progress_label: ""

@pytest.mark.parametrize("event, data", [("hitl_raised", {...}), ("run_paused", {...}), ("phase_ready", {...}),
                                         ("runner_error", {"error": "x", "exception": "RuntimeError"})])
def test_a_run_that_is_not_over_keeps_its_label(flow_repo, gh, event, data):
    # emit the event (field shapes as in test_run_control.py), check run.state["status"] is not terminal,
    # then release(run): no --remove-label
def test_a_runner_error_keeps_the_label(flow_repo, gh, monkeypatch):
    # monkeypatch pipeline._Loop.go to raise RuntimeError; advance raises; status "error"; no --remove-label
def test_release_takes_the_label_from_the_event(flow_repo, gh):
    # mark with the default label, flow_repo.configure({"issues": {"in_progress_label": "other"}}), cancel:
    # --remove-label agentflow:in-progress
def test_a_failed_release_is_retried_at_archive(flow_repo, gh, monkeypatch):
    # FAKE_GH_FAIL_ON="issue edit" set after start, then cancel: issue_status_failed action "release";
    # unset it, archive(run, by="me"): a second --remove-label and one issue_released;
    # a second archive raises ControlError, and the log has no further issue edit
def test_archiving_a_released_run_makes_no_gh_call(flow_repo, gh): ...
def test_a_killed_runner_keeps_the_label_until_cancel(flow_repo, gh):
    # a started run with no runner (status running): no issue_released; cancel then releases it
```

The finished-run path (`advance` reaching `done`) is pinned in Task 5, which runs `finish`.

- [ ] **Step 2: Run them to verify they fail**

Run: `T agentflow/tests/test_issue_status.py`
Expected: the new tests FAIL (no `--remove-label`).

- [ ] **Step 3: Implement `release` and its three call sites**

`release` is the spec's Code Style block. In `advance`, call `release(run)` in the `finally` after `lock.release()`. It never raises a `GhError`, so it can't hide the exception that is on its way out. In `cancel` and `archive`, call it after the `with run.locked():` block, so the gh call never holds the event lock.

Playbook §8, one sentence: a run started with `--issue N` adds the `issues.in_progress_label` label and one comment to the issue, keeps the label while the run waits, pauses or stops on an error, and removes it when the run ends `done`, `failed` or `cancelled` (or, if GitHub failed then, when the run is archived). CHANGELOG `### Added`: the spec's first Added line.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `T agentflow/tests/test_issue_status.py agentflow/tests/test_run_control.py agentflow/tests/test_portability.py`
Expected: PASS.

- [ ] **Step 5: Commit** (when the developer asks)

```bash
git add agentflow/issue_status.py agentflow/pipeline.py agentflow/run_control.py docs/agentflow-playbook.md CHANGELOG.md agentflow/tests/test_issue_status.py
git commit -m "Remove the in-progress label when an issue run ends, or at archive."
```

---

### Task 4: The PR body's issue line and the epic flag

**Files:**
- Modify: `agentflow/intake.py:27` (`ISSUE_FIELDS = "title,body,url,labels"`), `:48` (`gather(params, repo, epic_labels=())`)
- Modify: `agentflow/pipeline.py:135` (pass `cfg.issues.epic_labels`)
- Modify: `agentflow/feature_lane.py:916-955` (`render_pr_body`: the second part of `parts`)
- Modify: `CHANGELOG.md` (`### Changed`)
- Modify: `agentflow/tests/test_bug_eval_security.py:180` (the argv now asks for `title,body,url,labels`)
- Test: `agentflow/tests/test_issue_status.py`

**Interfaces:**
- Consumes: `cfg.issues.epic_labels` (Task 1).
- Produces:
  - `intake.gather(params: dict, repo: Path, epic_labels: Iterable[str] = ()) -> tuple[dict, dict[str, bytes]]`, which sets `params["issue_epic"] = True` when an issue label matches.
  - `intake.is_epic(data: dict, epic_labels: Iterable[str]) -> bool`: compares the NFC `name` strings of `data["labels"]` with `epic_labels` using `casefold()`. An odd shape (not a list, an entry that isn't a dict, a `name` that isn't a string) counts as no label.
  - `feature_lane.issue_line(params: dict) -> str | None`: `"Refs #N"` when `params.get("issue_epic")`, `"Closes #N"` when `params.get("issue")`, else None. Task 5 relies on this line being in the body.

- [ ] **Step 1: Write the failing tests**

Use `FAKE_GH_ISSUE` for the issue reply (its body holds `SECRET-BODY`), and `render_pr_body(run)` on the started run.

```python
def second_line(run):
    return render_pr_body(run).split("\n\n")[1]

def test_an_issue_run_body_says_closes(flow_repo, gh): ...            # == "Closes #7"
def test_the_line_ignores_the_issue_keys(flow_repo, gh): ...           # label "" + comment false: still "Closes #7"
@pytest.mark.parametrize("labels, epic_labels, line", [
    ([{"name": "Epic"}], None, "Refs #7"),
    ([{"name": "bug"}], None, "Closes #7"),
    (None, None, "Closes #7"),                       # no labels key at all
    ([{"name": "Epic"}], [], "Closes #7"),
])
def test_an_epic_label_gives_refs(flow_repo, gh, monkeypatch, labels, epic_labels, line):
    # assert second_line(run) == line, run.state["params"].get("issue_epic") is (line == "Refs #7" or None),
    # and no label name appears in issue.md
def test_a_later_label_edit_does_not_change_the_line(flow_repo, gh, monkeypatch):
    # start as an epic, change FAKE_GH_ISSUE to no labels and epic_labels to []: still "Refs #7"
def test_a_hash_issue_reference_gives_one_hash(flow_repo, gh): ...     # {"issue": "#7"} -> "Closes #7"
@pytest.mark.parametrize("labels", [None, "epic", [3], [{"name": 3}], [{"title": "epic"}]])
def test_odd_label_shapes_are_not_an_epic(flow_repo, gh, monkeypatch, labels): ...  # starts, "Closes #7"
def test_a_run_without_an_issue_has_no_issue_line(flow_repo, gh): ... # second part is the Plan line
```

Also extend a `test_ci.py` case that refreshes the body (`_ready` → `update_pr_body`) so the refreshed `pr.md` of an issue run still has `Closes #7` as its second line.

- [ ] **Step 2: Run them to verify they fail**

Run: `T agentflow/tests/test_issue_status.py -k "line or epic or hash or odd"`
Expected: FAIL.

- [ ] **Step 3: Implement**

`gather` keeps the labels out of `issue.md` (`_issue_text` is unchanged) and out of `params`. Only the flag goes in. `render_pr_body` adds `issue_line(state["params"] or {})` to `parts` right after the "Opened by agentflow run…" line, ahead of "Marked done…". CHANGELOG `### Changed`: the spec's Changed line.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `T agentflow/tests/test_issue_status.py agentflow/tests/test_bug_eval_security.py agentflow/tests/test_feature_lane.py agentflow/tests/test_ci.py agentflow/tests/test_workspace.py agentflow/tests/test_explain_golden.py`
Expected: PASS, and the golden explain files are unchanged (`git status agentflow/tests/fixtures/explain` is clean).

- [ ] **Step 5: Commit** (when the developer asks)

```bash
git add agentflow/intake.py agentflow/pipeline.py agentflow/feature_lane.py CHANGELOG.md agentflow/tests/test_issue_status.py agentflow/tests/test_bug_eval_security.py agentflow/tests/test_ci.py
git commit -m "Say Closes #N, or Refs #N for an epic, in an issue run's pull request body."
```

---

### Task 5: Put the PR in the issue's Development section

**Files:**
- Modify: `agentflow/issue_status.py` (`link`, `LINK_MUTATION`, `NODE_ID`, `LINK_FILE`)
- Modify: `agentflow/feature_lane.py:784` (`run_finish`: `link(self.run)` right after `pr_opened`, before `confirm_mergeable`)
- Modify: `agentflow/run_control.py` (`archive`: `link(run)` before the `release(run)` from Task 3)
- Modify: `agentflow/tests/bin/gh` (`$FAKE_GH_CLOSING`, the `id` in `issue view`, `api graphql`, `$FAKE_GH_LINK_IGNORED`, `$FAKE_GH_PR_ID`, and the docstring)
- Modify: `CHANGELOG.md` (`### Added`)
- Test: `agentflow/tests/test_issue_status.py`

**Interfaces:**
- Consumes: `_gh_out`, `_failed` (Task 2); `release` in `archive` (Task 3); the body line (Task 4); `params["issue_epic"]`.
- Produces:
  - `issue_status.link(run: Run) -> None`
  - `LINK_MUTATION`: the spec's mutation string, verbatim.
  - `NODE_ID = re.compile(r"^[A-Za-z0-9_=-]{1,100}$")`, `LINK_FILE = "issue-link.md"`.

- [ ] **Step 1: Extend the fake gh**

- `pr view <url> --json id,closingIssuesReferences` (and `--json closingIssuesReferences`) prints `{"id": $FAKE_GH_PR_ID or "PR_fake", "closingIssuesReferences": [{"number": n}, ...]}`. The numbers come from `$FAKE_GH_CLOSING` (comma-separated) plus each line of `$FAKE_GH_LOG.closing`. Match this branch before the existing `state` branch.
- `issue view N` adds `"id": "I_fakeN"` to its default reply only.
- `api graphql` with `addCloseIssueReferences` in its `query=` argument appends the issue's number to `$FAKE_GH_LOG.closing`, unless `$FAKE_GH_LINK_IGNORED` is set. The number is found by mapping the `issue=I_fakeN` variable back to `N`. It prints `{"data": {"addCloseIssueReferences": {"clientMutationId": null}}}`.

- [ ] **Step 2: Write the failing tests**

Drive the finish path with `test_feature_lane`'s setup (`SMALL`, `RunOptions(from_phase="ship")`), plus `"issue": "7"` in the params, and `advance(run, flow_repo.cfg, RunOptions())`. `after_create(log)` is the calls after `pr create`.

```python
def test_a_finished_issue_run_links_by_keyword_and_releases(flow_repo, gh, monkeypatch):
    # FAKE_GH_CLOSING=7: advance == "done"; after_create has ["pr","view",URL,"--json","id,closingIssuesReferences"],
    # no "api" call, no issue comment; issue_linked == [{issue "7", pr URL, how "keyword"}];
    # one --remove-label; pr.md's second part is "Closes #7"
def test_an_unlinked_pr_is_linked_through_the_api(flow_repo, gh):
    # no FAKE_GH_CLOSING: after_create holds, in order, the pr view, ["issue","view","7","--json","id"],
    # ["api","graphql","-f",f"query={LINK_MUTATION}","-f","issue=I_fake7","-f","pr=PR_fake"], a second pr view;
    # issue_linked how "api"; no issue comment after pr create
def test_a_link_github_ignores_is_a_failure(flow_repo, gh, monkeypatch):
    # FAKE_GH_LINK_IGNORED=1: "done", issue_status_failed {action "link", error "GitHub does not list the link"},
    # no issue_linked
@pytest.mark.parametrize("pr_id", ["x; rm -rf /", ""])
def test_a_bad_node_id_never_reaches_an_argv(flow_repo, gh, monkeypatch, pr_id):
    # FAKE_GH_PR_ID=pr_id: no "api" call, issue_status_failed action "link"
def test_an_epic_gets_a_comment_and_no_development_link(flow_repo, gh, monkeypatch):
    # FAKE_GH_ISSUE with labels [{"name": "epic"}]: no closingIssuesReferences query, no api call;
    # one issue comment after pr create with --body-file run.dir/"issue-link.md"; the file holds the PR URL,
    # the run id and "won't close"; issue_linked how "comment"
def test_the_issue_keys_never_turn_the_link_off(flow_repo, gh):
    # in_progress_label "" + comment false: the closingIssuesReferences query and the api call still happen
def test_a_failed_link_is_retried_at_archive(flow_repo, gh, monkeypatch):
    # FAKE_GH_FAIL_ON="api graphql" until done: issue_status_failed action "link"; unset, archive:
    # an api graphql call and issue_linked how "api"; issue_status.link(run) again logs nothing
def test_the_link_is_made_before_a_conflict_stops_the_run(flow_repo, gh, monkeypatch):
    # FAKE_GH_MERGEABLE=CONFLICTING: advance stops ("waiting"); issue_linked is in the log; no --remove-label
def test_a_feature_run_without_an_issue_makes_no_link_call(flow_repo, gh): ...  # no closingIssuesReferences
```

`test_feature_lane.test_finish_pushes_and_opens_draft_pr` keeps its exact log `[["pr","create"],["pr","view"]]`: a run without an issue makes no link call.

- [ ] **Step 3: Run them to verify they fail**

Run: `T agentflow/tests/test_issue_status.py -k "link or epic or keyword or api or node"`
Expected: FAIL.

- [ ] **Step 4: Implement `link` and its two call sites**

`link` follows the spec's "`link(run: Run) -> None`" section step by step. Each gh reply is parsed with `json.loads` inside the `try`, so bad JSON is an `issue_status_failed` too. The PR's `id` and the issue's `id` are each checked with `NODE_ID.fullmatch` before the mutation. The listed numbers are compared as strings with `str(params["issue"])`. The epic comment copy:

```
agentflow run `<run id>` opened <PR url> for this issue.

This issue has an epic label, so the pull request is not linked as closing it: merging it won't close this issue.
```

CHANGELOG `### Added`: the spec's second Added line.

- [ ] **Step 5: Run the touched tests, then the full suite**

Run: `T agentflow/tests/test_issue_status.py agentflow/tests/test_feature_lane.py agentflow/tests/test_ci.py agentflow/tests/test_run_control.py agentflow/tests/test_retro.py agentflow/tests/test_portability.py`, then `bin/agentflow validate`, then the full suite: `T agentflow/tests` (25-60 min).
Expected: all PASS, `0 warnings`, and `git status agentflow/tests/fixtures/explain` is clean.

- [ ] **Step 6: Commit** (when the developer asks)

```bash
git add agentflow/issue_status.py agentflow/feature_lane.py agentflow/run_control.py agentflow/tests/bin/gh CHANGELOG.md agentflow/tests/test_issue_status.py
git commit -m "Link an issue run's pull request into the issue's Development section, or comment on an epic."
```

---

### After the tasks: the manual check (the developer runs it)

This check writes to a real GitHub issue, so it's the developer's to run, on a scratch issue, using the spec's Commands block ("manual end to end"). It covers: the label and one comment after start, no label after cancel, the PR under Development for a run to the default branch (`keyword`) and for one whose `project.fork_from` isn't the default branch (`api`), and an epic that gets a comment and an empty Development section.

## Spec coverage

| Spec item | Task |
|---|---|
| AC 1 (mark), 6 (label create), 7 (keys), 8 (no issue, dry run) | 2 (7: also 4 and 5) |
| AC 2, 3 (release and keep), 10 (archive retry for release), 14 (killed runner) | 3 |
| AC 4 (`Closes #7`, CI refresh), 9 (epic `Refs`) | 4 |
| AC 5 (GitHub failures) | 2, 3, 5 |
| AC 10 (archive retry for link), 11, 12, 13 (link) | 5 |
| Config keys with the four obligations | 1 |
| Playbook §8, §14; CHANGELOG Added and Changed | 3, 1; 3, 4, 5 |
| Testing Strategy 1-21 | 2 (1-6), 3 (7, 9, 10, 13, 14), 1 (11), 4 (8 body, 12, 20 body), 5 (8 release, 15-21) |
