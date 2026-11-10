# Mark a GitHub issue "in progress" while an agentflow run works on it

## Context

When a run starts from `--issue N` (CLI, or a Backlog card), nothing changes on GitHub. Other people can't
see that agentflow picked the issue up, and they may start on it too.

### Root cause

agentflow only ever **reads** an issue. Nothing in the engine writes back to one.

1. **Read-only intake.** `intake.gather` (`agentflow/intake.py:48-62`) calls
   `gh issue view N --json title,body,url`, saves `issue.md`, and does nothing else. `pipeline.start`
   (`agentflow/pipeline.py:122-142`) calls `gather` and then `_set_up`, and has no step after that.
2. **No `gh issue` write anywhere.** The engine's only `gh` calls are `issue view`, `pr create/edit/checks/ready/view`
   and `run view/rerun` (`agentflow/gitops.py`). There is no `gh issue edit` or `gh issue comment`.
3. **No config for it.** The engine defaults (`agentflow/defaults/config.yaml`), `config.py` and
   playbook §14 have no key about issue status. The harness proposal lists `github_issues` as a work source
   (§1b, line 68), but that was never built.
4. **The duplicate guard is local only.** `backlog._refuse_duplicate_issue` (`agentflow/backlog.py:332`)
   refuses a second run for the same issue, but it can only see runs in this machine's state dir.
   Nobody else can see it.
5. **The PR isn't linked to the issue.** `render_pr_body` (`agentflow/feature_lane.py:916`) never mentions the
   issue, so the issue's page doesn't show the draft PR either.

So this isn't a broken call. The feature was never built: there is no lifecycle hook that writes to the tracker.

### Decided behaviour (the user's choices)

- **When the run starts:** add a configurable label to the issue, and post one comment naming the run id, lane and branch.
- **When the run ends** (done with the PR opened, cancelled, or failed): remove the label. The draft PR body gets `Closes #N`,
  so the issue links to the PR and closes when the PR merges.
- An issue with an epic label (`issues.epic_labels`, default `["epic"]`) gets `Refs #N` instead, so merging a PR
  never closes an epic. The developer decides when to close it.
- The PR is always in the issue's Development section, so the issue closes when the PR merges into the default
  branch. After the PR opens, the runner checks `closingIssuesReferences`. When `Closes #N` didn't link it (a base
  that isn't the default branch, where GitHub ignores the keyword), the runner adds the link through the GraphQL
  mutation `addCloseIssueReferences`, the same link a person makes from the sidebar. An epic gets no Development
  link (it would close on merge): it gets `Refs #N` and a comment with the PR's URL. No config key turns this off.
- A run that stops for a human or hits `error` (resumable) keeps the label, because it is still in progress.
- `run_control.archive` also calls `release`, which gives a failed release a second try.
- A runner that dies without reaching `finally` keeps the label. The developer cancels the run or removes the label
  by hand.

The proposal, `docs/specs/agentflow-issue-status-proposal.md`, is the spec. Where the two differ, the proposal wins.

## Design

### New engine module `agentflow/issue_status.py` (stdlib only, uses `gitops._gh`)

- `mark(cfg, run, repo)`: when `run.state.params.issue` is set and `cfg.issues.in_progress_label` is non-empty:
  - `gh issue edit N --add-label <label>`. If gh says the label doesn't exist, run
    `gh label create <label> --description "agentflow is working on this issue"` (no `--force`, so a label
    someone already made keeps its colour), then retry once.
  - When `cfg.issues.comment` is true, run `gh issue comment N --body-file <runs/<run>/issue-comment.md>`.
    The body is built from the run id, lane and branch only, never from issue text.
  - Emit `issue_marked {issue, label, commented}`. Any `GhError`/`OSError` emits `issue_status_failed {action, error}`
    and is **never raised**, following `hitl.notify`: a GitHub hiccup must not fail a run.
- `release(run)`: idempotent. When the run is in `TERMINAL` (`state.py:28`), has an `issue_marked` event and has no
  `issue_released` event, run `gh issue edit N --remove-label <label>`. The label comes from the event, so
  `release` needs no `cfg`, and a config edit in the middle of a run can't strand a label. Emit `issue_released`, or
  `issue_status_failed`. The run's `repo` comes from `run.state` (the same way `run_control` already finds it).
- Every argv is a list, the issue number has already passed `intake.issue_number`, and the label comes from config
  and is passed as one argv element, so nothing reaches a shell.

### Call sites

- `pipeline.start` (`pipeline.py:122`): after `_set_up` succeeds, call `issue_status.mark(cfg, run, repo)`.
  That means after the worktree exists, so a start that fails before then never labels the issue.
- `pipeline.advance`: in the `finally` that releases the lock, call `issue_status.release(run)`. This covers
  `run_finished` (`pipeline.py:282`) and every `failed` path inside a run.
- `run_control.cancel` (`run_control.py:262`): after `run_cancelled`, call `release(run)`.
- `pipeline.start`'s `except` path (`run_failed` at `:140`) needs nothing, because the issue was never marked.

### Link the PR

`render_pr_body` (`feature_lane.py:916`): when `params.issue` is set, add `Closes #N` as the second line,
right after "Opened by agentflow run…". The `ci_lane` body refresh (`ci_lane.py:222`) reuses the same
renderer, so the line stays.

### Config (four obligations per key, per CLAUDE.md)

Add an `issues:` block to `agentflow/defaults/config.yaml`:

```yaml
issues:
  in_progress_label: "agentflow:in-progress"   # "" turns issue status updates off
  comment: true                                # also post one comment when a run starts from an issue
```

For each key:
- `config.py`: an `IssuePolicy` dataclass on `Config`, loaded with `_optional` like `MemoryPolicy` (`config.py:889`).
- `validate`: the label must be a string of at most 50 chars with no newline or comma (gh splits `--add-label` on commas),
  and `comment` must be a bool.
- `config_edit.py`: Settings Form fields (a text field and a checkbox), following the `defaults.run_budget_usd` entry (`config_edit.py:120`).
- `tests/test_config_edit.py`: a case for each key.
- Playbook §14 "Pack reference": one line for each key. §8 "Operating a run": one sentence saying what happens to the issue.

### Other touches

- `doctor.py`: no change, because `gh auth status` already covers it. The label and comment need only the default `repo` scope.
- `tests/bin/gh`: today it prints `[]` for unknown commands, so `issue edit` and `issue comment` already succeed and get logged.
  Add `$FAKE_GH_NO_LABEL` so `issue edit --add-label` fails once with `could not add label: '<l>' not found`, to test the create-and-retry path.
- `CHANGELOG.md` under `[Unreleased]`: an **Added** line ("a run started from an issue labels it in progress, comments, and removes
  the label when the run ends") and a **Changed** line ("the draft PR body says `Closes #N`").
- The portability `DENYLIST` doesn't apply, because the label is generic.

## Tests (TDD: write them failing first)

A new file, `agentflow/tests/test_issue_status.py`, using the fake `gh` log (`FAKE_GH_LOG`):
1. `run bug --issue 7` (fake agent, `--to define` or a dry stop) logs `issue view 7`, then `issue edit 7 --add-label agentflow:in-progress`,
   then `issue comment 7 --body-file …`, and records an `issue_marked` event.
2. `in_progress_label: ""` makes no edit and no comment calls.
3. A run without `--issue` makes no issue calls.
4. With `FAKE_GH_FAIL` on the edit only, the run still starts and records `issue_status_failed`.
5. A missing label leads to `label create`, then a retried `--add-label`.
6. `cancel` logs `--remove-label` once. A second `release` call logs nothing.
7. Finishing a run (an existing finish-path test's setup) removes the label, and `render_pr_body` contains `Closes #7`.
8. A run that stops for a human (HITL) keeps the label (no `--remove-label` in the log).

Also update any existing test that checks the exact `gh` call log or the exact PR body text for an issue run
(grep `FAKE_GH_LOG` and `render_pr_body` in `agentflow/tests`).

## Verification

```bash
uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_issue_status.py agentflow/tests/test_config_edit.py agentflow/tests/test_portability.py --import-mode=prepend -q
bin/agentflow validate                      # must print "0 warnings"
bin/agentflow config show --effective issues
uv run --no-project --with pyyaml --with pytest pytest agentflow/tests --import-mode=prepend -q   # the full suite before finishing
```

Then a manual end-to-end check on a scratch issue in this repo: `bin/agentflow run bug --issue <N> --to define`. The issue should
get the label and a comment. Then `bin/agentflow cancel <run>`: the label should be gone.

## Critical files

- `agentflow/issue_status.py` (new)
- `agentflow/pipeline.py` (`start`, `advance`'s `finally`)
- `agentflow/run_control.py` (`cancel`)
- `agentflow/feature_lane.py` (`render_pr_body`)
- `agentflow/config.py`, `agentflow/config_edit.py`, `agentflow/defaults/config.yaml`
- `agentflow/tests/bin/gh`, `agentflow/tests/test_issue_status.py` (new), `agentflow/tests/test_config_edit.py`
- `docs/agentflow-playbook.md` (§8, §14), `CHANGELOG.md`
