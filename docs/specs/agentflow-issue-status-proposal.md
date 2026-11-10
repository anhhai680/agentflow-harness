# Proposal: mark a GitHub issue "in progress" while an agentflow run works on it

Status: approved (2026-11-10)

Source: `docs/agentflow-harness-issue-status.md` (the design note). This proposal is the spec. It checks every claim
in that note against the code and adds the gaps the note missed.

## Objective

When a run starts from `--issue N` (on the CLI, or from a Backlog card), nothing changes on GitHub. Other people
can't see that agentflow has picked the issue up, and they may start on it too.

**User:** whoever triages the repository's issues, plus the person who started the run. Both look at the issue
on GitHub, not at the agentflow dashboard.

**Why nothing happens today** (checked on `main` at `303930c`):
1. `intake.gather` (`agentflow/intake.py:48`) calls `gh issue view N --json title,body,url` and only reads.
   `pipeline.start` (`agentflow/pipeline.py:122`) calls `gather`, then `_set_up`, and has no step after that.
2. The engine never writes to an issue. Its `gh` calls (`gitops._gh`, `gitops.py:85`) are `issue view`,
   `pr create/edit/checks/ready/view` and `run view/rerun`.
3. No config key covers issue status (`defaults/config.yaml`, `config.py`, playbook §14).
4. `backlog._refuse_duplicate_issue` (`backlog.py:332`) refuses a second run for the same issue, but it only
   sees runs in this machine's state dir.
5. `render_pr_body` (`feature_lane.py:916`) never names the issue, so the issue's page doesn't show the draft PR.

### Decided behaviour (the user's choices; not up for change in this proposal)

- **When the run starts:** add a configurable label to the issue, and post one comment that names the run id,
  lane and branch.
- **When the run ends** (`done` with the PR opened, `cancelled`, or `failed`): remove the label. The draft PR body
  says `Closes #N`, so the issue links to the PR and closes when the PR merges.
- **An epic is never closed by a PR.** When the issue carries one of `issues.epic_labels`, the PR body says
  `Refs #N` instead, so the issue and the PR are linked and the developer decides when the epic is done.
- **The PR is always in the issue's Development section** (every issue that isn't an epic). As soon as the PR is
  created, the runner makes sure GitHub lists it as a closing reference of the issue, the same link a person makes
  from the sidebar. GitHub then closes the issue when the PR merges into the default branch. `Closes #N` in the
  body makes the link when the PR targets the default branch. Otherwise the runner adds the link through
  GitHub's API (`addCloseIssueReferences`), whatever the base branch. No `issues.*` key turns this off.
- **An epic gets `Refs #N` and a comment instead.** On GitHub, every Development link closes the issue on merge, so
  an epic gets no Development link. The runner comments the PR's URL on the epic instead.
- **A run that stops for a human, or hits `error`** (resumable), keeps the label, because the work is still in progress.
- **A failed release gets a second try at archive.** `run_control.archive` also calls `release`.
- **A runner that dies without reaching `finally`** (`kill -9`, a shutdown) leaves the run `running` and the label
  on. That is correct, because the run really is unfinished. The developer cancels the run, or removes the label by
  hand. Nothing is added for this case.

### Acceptance criteria

1. `agentflow run bug --issue 7` calls `gh issue edit 7 --add-label agentflow:in-progress` and then
   `gh issue comment 7 --body-file <file>`, and records `issue_marked {issue, label, commented}`.
2. When the run reaches `done`, `failed` or `cancelled`, the runner calls `gh issue edit 7 --remove-label <label>`
   once and records `issue_released {issue, label}`.
3. A run that is `waiting` (HITL), `paused`, `ready` or `error` keeps the label: the log has no `--remove-label`.
4. The draft PR body of an issue run has `Closes #7` as its second line, and the CI lane's body refresh keeps it.
   The line doesn't depend on the `issues.*` keys: `in_progress_label: ""` and `comment: false` leave it in place.
5. If GitHub fails (gh exits non-zero, times out, or is missing), the run goes on as it would have without this
   feature, and the event log records `issue_status_failed {action, error}`.
6. A missing label is created once (`gh label create`) and the add is retried once.
7. `issues.in_progress_label: ""` turns off the label and the start comment: no edit and no start comment.
   `issues.comment: false` keeps the label and skips the start comment. Neither key turns off the PR link
   (criteria 4, 11 and 12).
8. A run without `--issue` makes no issue calls. So does a retro run and a `--dry-run`.
9. An issue labelled `epic` (the default `issues.epic_labels`, compared without case) gets `Refs #7` as the PR
   body's second line, never `Closes #7`, and its run records `params.issue_epic: true`. A label added or removed
   on GitHub after the run started doesn't change the line.
10. `agentflow archive` on a terminal run whose `release` failed calls `--remove-label` again, and records
    `issue_released` when it succeeds. The same goes for a `link` that failed. Archiving a run that was already
    released and linked makes no gh call.
11. Right after `pr_opened`, for an issue that isn't an epic, the runner calls
    `gh pr view <url> --json id,closingIssuesReferences`.
    - When issue 7 is listed, it records `issue_linked {issue, pr, how: "keyword"}` and changes nothing.
    - When it isn't listed (a base that isn't the default branch), it calls `gh issue view 7 --json id`, then
      `gh api graphql` with the `addCloseIssueReferences` mutation, then `pr view` again. It records
      `issue_linked {issue, pr, how: "api"}` only once issue 7 is listed.
    - Either way, the PR is then in issue 7's Development section and closes it on merge into the default branch.
      No comment is posted.
12. For an epic, the runner makes no Development link. It runs `gh issue comment 7 --body-file <file>` with the
    PR's URL and records `issue_linked {issue, pr, how: "comment"}`.
13. Every issue run that opens a PR ends with exactly one `issue_linked`. If GitHub fails, or still doesn't list
    the link after the mutation, the runner records `issue_status_failed {action: "link", error}` and tries again
    at archive. The run itself goes on.

## Tech Stack

Python 3.12+, stdlib + `pyyaml>=6` (the engine invariant; no new import). GitHub through the `gh` CLI, called
only via `gitops._gh` (list argv, `stdin=DEVNULL`, 120 s timeout, `OSError`/`TimeoutExpired` mapped to `GhError`).
The one GraphQL call (`gh api graphql`, the `addCloseIssueReferences` mutation) is the engine's first `gh api`
use. It needs the token's `repo` scope, which `pr create` already needs.

## Commands

```bash
# while working: the files this change touches
uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_issue_status.py agentflow/tests/test_config_edit.py agentflow/tests/test_portability.py --import-mode=prepend -q
bin/agentflow validate                                   # must print "0 warnings"
bin/agentflow config show --effective issues             # all three keys, from the `engine` layer
bin/agentflow run bug --issue 7 --dry-run                # prints the commands, makes no gh issue call

# before finishing: the full suite (25-60 min)
uv run --no-project --with pyyaml --with pytest pytest agentflow/tests --import-mode=prepend -q

# manual end to end on a scratch issue in this repository
bin/agentflow run bug --issue <N> --to define            # the issue gets the label and one comment
bin/agentflow cancel <run>                               # the label is gone
bin/agentflow run bug --issue <N>                        # at ship: the PR is under the issue's Development section
gh pr view <url> --json closingIssuesReferences          # lists <N>
```

## Project Structure

```
agentflow/issue_status.py            new: mark(), link(), release(), the mutation, the two comment bodies. Stdlib + gitops._gh only
agentflow/intake.py                  ISSUE_FIELDS adds `labels`; gather() returns the label names
agentflow/pipeline.py                start(): set params.issue_epic, mark after _set_up; advance(): release in the finally
agentflow/run_control.py             cancel(): release after the event; archive(): link, then release
agentflow/feature_lane.py            render_pr_body(): the `Closes #N` / `Refs #N` line; run_finish(): link after pr_opened
agentflow/config.py                  IssuePolicy dataclass on Config, loading, validate checks
agentflow/config_edit.py             two Settings Form fields
agentflow/defaults/config.yaml       the `issues:` block
agentflow/tests/bin/gh               new knobs: $FAKE_GH_NO_LABEL, $FAKE_GH_FAIL_ON, $FAKE_GH_CLOSING; `api graphql` adds a link
agentflow/tests/test_issue_status.py new
agentflow/tests/test_config_edit.py  a case for each key
docs/agentflow-playbook.md           §8 one sentence, §14 one line per key
CHANGELOG.md                         [Unreleased]: Added + Changed lines
```

## Design

### The module: `agentflow/issue_status.py`

Three entry points. Neither one raises for a GitHub problem, the same rule `hitl.notify` follows (`hitl.py:322`):
a GitHub hiccup must never fail or stop a run.

**`mark(cfg: Config, run: Run) -> None`.** It does nothing unless `run.state["params"]["issue"]` is set and
`cfg.issues.in_progress_label` is non-empty. Otherwise:
1. `gh issue edit N --add-label <label>`. If gh reports that the label is not found, it runs
   `gh label create <label> --description "agentflow is working on this issue"` (no `--force`, so a label someone
   already made keeps its colour and description) and retries the add once.
2. When `cfg.issues.comment` is true, it writes `runs/<run>/issue-comment.md` with `safeio.write_text` and runs
   `gh issue comment N --body-file <that file>`. The body comes **only** from the run id, lane and branch, never
   from issue text. It also says that the label goes away when the run ends.
3. It emits `issue_marked {issue, label, commented}`. A `GhError` or `OSError` from any step emits
   `issue_status_failed {action: "mark" | "label_create" | "comment", error}` and stops there. A failed
   comment after a successful add still emits `issue_marked` with `commented: false`, so `release` removes the label.

**`release(run: Run) -> None`.** It is idempotent and needs no `cfg`. It acts only when all three hold:
`run.state["status"]` is in `state.TERMINAL` (`state.py:28`), the event log has an `issue_marked`, and the log has
no `issue_released`. Then it runs `gh issue edit N --remove-label <label>`, taking the label **from the
`issue_marked` event**, so a config edit in the middle of a run can't leave a label stranded. It emits
`issue_released {issue, label}`, or `issue_status_failed {action: "release", error}`. The working directory for gh
is `run.repo`.

**`link(run: Run) -> None`.** It puts the PR in the issue's Development section. It is idempotent and needs no
`cfg`, so no config key can turn it off. It acts only when `params.issue` is set, the log has a `pr_opened`, and
the log has no `issue_linked`. The PR's URL comes from the last `pr_opened` event.

For an issue that isn't an epic:
1. `gh pr view <url> --json id,closingIssuesReferences`. GitHub lists both keyword links and manual links in
   `closingIssuesReferences`. This was checked on a PR linked by hand from the sidebar: its issue was listed. If
   the issue's number is among the `number`s, `link` emits `issue_linked {issue, pr, how: "keyword"}`. This is the
   case of a PR to the default branch, where the `Closes #N` line already linked it.
2. Otherwise, `gh issue view N --json id`, then
   `gh api graphql -f query=<LINK_MUTATION> -f issue=<issue id> -f pr=<PR id>`, where `LINK_MUTATION` is a
   constant in the module:
   `mutation($issue: ID!, $pr: ID!) { addCloseIssueReferences(input: {issueId: $issue, pullRequestIds: [$pr]}) { clientMutationId } }`.
   Per the schema, the mutation "adds one or more pull requests as manually linked closing references on an
   issue": the sidebar link. Unlike a keyword, it works whatever branch the PR targets.
3. `gh pr view <url> --json closingIssuesReferences` once more. If the issue is listed, `link` emits
   `issue_linked {issue, pr, how: "api"}`. If not, it emits
   `issue_status_failed {action: "link", error: "GitHub does not list the link"}`.

For an epic (`params.issue_epic`), `link` makes no Development link, because GitHub would close the epic on
merge. It writes `runs/<run>/issue-link.md` (the PR's URL, the run id, and a line saying that merging won't close
the issue), runs `gh issue comment N --body-file <that file>` and emits `issue_linked {issue, pr, how: "comment"}`.

A `GhError` or `OSError` from any step, or a reply without the expected `id`, emits
`issue_status_failed {action: "link", error}`. `archive` gives it a second try.

A GitHub merge closes the issue only when the PR merges into the default branch. A linked PR to another base
stays in the Development section, but merging it closes nothing. That is GitHub's rule, and the runner doesn't
work around it.

**Node ids.** The PR and issue ids come from gh's JSON. Before they go into an argv, each one must match
`^[A-Za-z0-9_=-]{1,100}$`, and anything else is an `issue_status_failed`. The mutation text is a constant. The
ids go in as `-f` variables, each one a single argv element, and are never spliced into the query. The PR's URL
reaches only the epic comment's body file, never an argv.

**Safety.** Every argv is a list. The issue number has already passed `intake.issue_number`. The label comes from
config, is validated (below) and is passed as one argv element. Nothing reaches a shell, and no issue text reaches
the comment.

### Call sites (every way a run ends is covered)

| Where | Call | Why it is enough |
|---|---|---|
| `pipeline.start`, after `_set_up` returns | `mark(cfg, run)` | Only after the worktree exists, so a start that fails earlier (`run_failed` at `pipeline.py:140`) never labels the issue. The Backlog spawns `flow run`, so it goes through here too. |
| `FeatureLane.run_finish` (`feature_lane.py:780`), right after `pr_opened` and before `confirm_mergeable` | `link(run)` | The only place a run opens its PR. The link is made as soon as the PR exists, before a conflict or CI stop can park the run. |
| `pipeline.advance`'s `finally` (`pipeline.py:225`), after `lock.release()` | `release(run)` | Covers `run_finished` (`:282`) and every `failed` path inside a run. A `runner_error` leaves the status `error`, which is not terminal, so the label stays. |
| `run_control.cancel` (`run_control.py:262`), after `run_cancelled` and outside `run.locked()` | `release(run)` | CLI and dashboard cancel both call it. The gh call never holds the event lock. |
| `run_control.archive` (`run_control.py:132`), after `run_archived` and outside `run.locked()` | `link(run)`, then `release(run)` | The second try for a link or a release that failed earlier (gh down, a timeout). `archive` only takes a terminal run, and both calls do nothing once their event (`issue_linked`, `issue_released`) is in the log, so a run that linked and released cleanly makes no gh call. CLI and dashboard archive both call it. |

`retro.py` also emits `run_failed`, `run_cancelled` and `run_finished`, but a retro run has no `issue` param, so it
needs no call.

**Race.** If a cancel and the cancelled runner's `finally` both call `release` at the same moment, both may see no
`issue_released` and both call `--remove-label`. Removing a label the issue no longer has is harmless, so this
proposal accepts a duplicate call and a duplicate event instead of adding a lock around a network call.

**Not covered: a runner that dies.** A runner killed before its `finally` (`kill -9`, a shutdown) leaves the run
`running`, so the label stays. The run really is unfinished, so that is correct. The developer cancels the run
(which releases the label) or removes the label by hand. No watchdog or sweep is added.

### Link the PR

**What GitHub does** (its docs, "Linking a pull request to an issue"):
- A closing keyword (`Closes #N`) in the PR body links the PR to the issue, so the PR shows under the issue's
  Development section. This works only when the PR targets the repository's **default** branch. On any other
  base, "these keywords are ignored, no links are created".
- A manual link (the sidebar, or the GraphQL mutation `addCloseIssueReferences`) works for any base branch. It
  shows as a "linked a pull request" (`ConnectedEvent`) entry in the issue's timeline.
- Every kind of link (a keyword, a manual link, or a branch linked to the issue) closes the issue when the PR
  merges into the default branch. GitHub has no link that doesn't close.
- The PR targets `project.fork_from` (`feature_lane.py:754`), which a pack may set to a branch other than the
  default one, such as `develop`.

So a body line alone can't always link the PR, and an epic can't have a Development link without being closed on
merge. The design uses two layers:

1. **The body line, every time.** `render_pr_body` (`feature_lane.py:916`): when `params.issue` is set, the second
   line is `Closes #N`, right after "Opened by agentflow run…". When `params.issue_epic` is true, it is `Refs #N`,
   so merging the PR never closes the epic, and whether the epic is done is the developer's call. The line depends
   on `params` alone, never on the `issues.*` keys. `ci_lane` refreshes the body with the same renderer
   (`ci_lane.py:222`), so the line stays.
2. **Checked, and linked through the API when the keyword didn't take.** Once the PR is open, `link` asks GitHub
   whether the PR is linked (`closingIssuesReferences`). If it isn't, `link` adds the link with
   `addCloseIssueReferences` and checks again. The PR ends up in the Development section whatever the base. Only
   an epic gets a comment instead.

**Which issue is an epic.** The runner decides this from GitHub data, not with a model:
1. `intake.ISSUE_FIELDS` becomes `title,body,url,labels`. `gather` keeps the label names (NFC, `name` only) and
   leaves them out of `issue.md`, so the agent sees nothing new.
2. `pipeline.start`, which has `cfg`, sets `params["issue_epic"] = True` when one of those names equals one of
   `cfg.issues.epic_labels` without regard to case. It does this before `Run.create`, so the flag is in the run's
   recorded params and doesn't follow later label edits on GitHub or in config, just as `release` reads its label
   from the event. The label names are only compared. They never reach an argv, a prompt or the PR body.
3. The flag applies to every lane. Only the issue's labels decide, not the lane. GitHub's sub-issues and issue
   types aren't checked, because `gh issue view --json` doesn't return them. A team that uses them adds an epic
   label too, or edits the PR body by hand.

An epic still gets the in-progress label and the start comment. Only the link changes: `Refs #N` and a comment,
with no Development link.

### Config (each key ships with all four obligations)

`agentflow/defaults/config.yaml`:

```yaml
issues:
  in_progress_label: "agentflow:in-progress"   # "" turns off the label and the start comment (never the PR link)
  comment: true                                # also post one comment when a run starts from an issue
  epic_labels: ["epic"]                        # an issue with one of these gets `Refs #N`, never `Closes #N`
```

| Key | `config.py` | `validate` | `config_edit.py` | `test_config_edit.py` | Playbook §14 |
|---|---|---|---|---|---|
| `issues.in_progress_label` | `IssuePolicy.in_progress_label: str`, loaded with `_optional` like `MemoryPolicy` (`config.py:889`) | a string of at most 50 characters with no newline, no comma (gh splits `--add-label` on commas), not starting with `-` | text field, like `defaults.run_budget_usd` (`config_edit.py:120`) | edit and round-trip, plus a rejected bad value | one line |
| `issues.comment` | `IssuePolicy.comment: bool` | a bool | checkbox | edit and round-trip | one line |
| `issues.epic_labels` | `IssuePolicy.epic_labels: tuple[str, ...]` | a list of non-empty strings, each at most 50 characters with no newline; `[]` means no issue is an epic | `{"kind": "list"}` field with no options, like `autonomy.non_blocking_severities` (`config_edit.py:149`) | edit and round-trip, plus a rejected non-list | one line |

These are pack keys, not personal ones, so they don't go in `layers.USER_KEYS`. Playbook §8 "Operating a run" gets
one sentence: what happens to the issue when a run starts, stops and ends.

### Other touches

- `tests/bin/gh`: today `$FAKE_GH_FAIL` fails **every** call, including the `issue view` that intake needs, so it
  can't test "the edit fails but the run starts". Add `$FAKE_GH_FAIL_ON` (a space-separated argv prefix, such as
  `issue edit`) that fails only matching calls, and `$FAKE_GH_NO_LABEL`, which makes the first `issue edit --add-label`
  fail with `could not add label: '<l>' not found`. Unknown commands already print `[]` and exit 0, so `issue edit`,
  `issue comment` and `label create` succeed and are logged without other changes. Add `$FAKE_GH_CLOSING`, a
  comma-separated list of issue numbers: `pr view ... --json id,closingIssuesReferences` prints
  `{"id": "PR_fake", "closingIssuesReferences": [{"number": n}, ...]}`, an empty list by default. `issue view N`
  adds `"id": "I_fakeN"` to its default reply. `api graphql` with `addCloseIssueReferences` in its query appends
  the issue's number to the file `$FAKE_GH_LOG.closing`, which later `pr view` replies also list, so the
  "not linked, then linked through the API" path runs end to end. `$FAKE_GH_LINK_IGNORED` makes the mutation
  succeed and change nothing. An epic needs no new knob:
  `$FAKE_GH_ISSUE` already sets the whole `issue view` reply, `labels` included. The default reply has no `labels`
  key, and `gather` must treat that as no labels.
- `doctor.py`: no change. `gh auth status` already covers it, and the label, the comment and the mutation need
  only the `repo` scope that opening a PR already needs.
- `CHANGELOG.md` under `[Unreleased]`: **Added**: "a run started from an issue labels it in progress, comments on it,
  and removes the label when the run ends (or, failing that, when it is archived)", and "an issue run's PR is
  always in the issue's Development section, linked through GitHub's API when `Closes #N` didn't link it (a base
  that isn't the default branch); an epic gets a comment with the PR's URL instead". **Changed**:
  "an issue run's draft PR body says `Closes #N`, or `Refs #N` for an issue with an epic label".
- `test_portability.py` `DENYLIST`: not affected, because the label text is generic.

## Code Style

The style follows `hitl.notify`: plain functions, a one-line docstring that says what never happens, events as the
record, and errors caught at the edge.

```python
def release(run: Run) -> None:
    """Remove the label `mark` added, once the run has ended. A GitHub failure is logged, never raised."""
    run.refresh()
    if run.state["status"] not in TERMINAL:
        return
    events = run.events.read()
    marked = next((e for e in events if e["type"] == "issue_marked"), None)
    if marked is None or any(e["type"] == "issue_released" for e in events):
        return
    number, label = str(marked["issue"]), str(marked["label"])
    try:
        _edit(run.repo, number, "--remove-label", label)
    except (GhError, OSError) as exc:
        run.emit("issue_status_failed", action="release", error=str(exc) or type(exc).__name__)
        return
    run.emit("issue_released", issue=number, label=label)
```

Conventions: event names are `snake_case` nouns + past participles. User-facing text is short and plain. No project
name appears in the engine.

## Testing Strategy

pytest, using TDD: write each test failing first. `agentflow/tests/test_issue_status.py` uses the fake agent and the
fake `gh` log (`FAKE_GH_LOG`). No test calls a real GitHub.

1. `run bug --issue 7` (fake agent, stopping at `--to define`) logs `issue view 7`, then
   `issue edit 7 --add-label agentflow:in-progress`, then `issue comment 7 --body-file …`, in that order, and records
   `issue_marked`. The comment file holds the run id, lane and branch, and nothing from the issue body.
2. `in_progress_label: ""` logs no `issue edit` and no `issue comment`.
3. `comment: false` logs the edit and no comment, and `issue_marked` has `commented: false`.
4. A run without `--issue` logs no `issue` call at all. `--dry-run` with `--issue` logs no `issue edit`.
5. `FAKE_GH_FAIL_ON="issue edit"`: the run still starts and advances, and the log records `issue_status_failed`
   with `action: "mark"`. Nothing is released later, because nothing was marked.
6. `FAKE_GH_NO_LABEL`: the log shows `label create agentflow:in-progress …` and then a second `--add-label`.
7. `cancel` logs `--remove-label` once. A second `release(run)` logs nothing.
8. A finished run (reusing an existing finish-path test's setup) logs `--remove-label`, and `render_pr_body` contains
   `Closes #7` as its second line.
9. A run that stops for a human (HITL) logs no `--remove-label`. Neither does a run whose step raised (`error`).
10. A label changed in config between `mark` and `release` doesn't matter: `release` removes the label from the event.
11. `validate` rejects a label with a comma, with a newline, longer than 50 characters, or starting with `-`, a
    non-bool `comment`, and an `epic_labels` that isn't a list of non-empty strings.
12. `FAKE_GH_ISSUE` with `labels: [{"name": "Epic"}]`: the run records `params.issue_epic: true` and the PR body's
    second line is `Refs #7`. With `labels: [{"name": "bug"}]`, or with no `labels` key, it is `Closes #7`. With
    `epic_labels: []`, an `Epic` label still gets `Closes #7`. The label names don't appear in `issue.md`.
13. A finished run whose release failed (`FAKE_GH_FAIL_ON="issue edit"` while it ends), then `archive` with gh
    working: the log shows a second `--remove-label` and the run records `issue_released`. A second `archive` is
    refused as today, and archiving a run released cleanly logs no `issue edit`.
14. A run killed before its `finally` stays `running` with no `issue_released`. `cancel` then releases it (covered
    by test 7; no new code).
15. A finished issue run with `FAKE_GH_CLOSING=7` (the keyword linked it) logs
    `pr view <url> --json id,closingIssuesReferences`, then no `api graphql` and no `issue comment` after
    `pr create`, and records `issue_linked` with `how: "keyword"`.
16. The same run without `FAKE_GH_CLOSING` (the non-default-base case) logs `issue view 7 --json id`, then
    `api graphql -f query=… -f issue=I_fake7 -f pr=PR_fake`, then a second `pr view`. It records `issue_linked`
    with `how: "api"`, and logs no `issue comment` after `pr create`. The query argv equals `LINK_MUTATION`
    exactly, and the ids are separate argv elements.
17. `FAKE_GH_LINK_IGNORED` (the mutation succeeds, but GitHub still doesn't list the link): the run reaches
    `done`, records `issue_status_failed` with `action: "link"` and the error "GitHub does not list the link",
    and records no `issue_linked`.
18. A `pr view` reply whose `id` is `"x; rm -rf /"` or empty: no `api graphql` call, and `issue_status_failed`
    is recorded.
19. An epic run logs no `closingIssuesReferences` query and no `api graphql`, and posts the link comment. The
    comment holds the PR's URL and says that merging won't close the issue. `issue_linked` has `how: "comment"`.
20. `in_progress_label: ""` together with `comment: false`: the run still has the PR body line, the
    `closingIssuesReferences` query and, without `FAKE_GH_CLOSING`, the mutation.
21. `FAKE_GH_FAIL_ON="api graphql"` while the PR opens: the run still reaches `done` and records
    `issue_status_failed` with `action: "link"`. A later `archive` with gh working runs the mutation and records
    `issue_linked` with `how: "api"`. A second `link(run)` logs nothing.

Also update every existing test that checks the exact `gh` call log, the exact `issue view` argv (it now asks for
`labels`) or the exact PR body of an issue run (grep `FAKE_GH_LOG`, `ISSUE_FIELDS` and `render_pr_body` in
`agentflow/tests`). The golden `fixtures/explain/*.txt` files must not
change: this proposal adds no step and changes no `explain` output.

## Boundaries

- **Always:** route every gh call through `gitops._gh`. Catch `GhError`/`OSError` and emit `issue_status_failed`.
  Read the label for removal from the event. Ship each config key with its four obligations. Run `bin/agentflow validate`
  after the config edit.
- **Ask first:** posting any comment beyond the start comment and the epic's link comment (for example one when
  the run ends); linking through `gh issue develop` linked branches (a public preview, and it would create the
  branch on GitHub at start); any `gh api` call other than `addCloseIssueReferences`; removing a link
  (`removeCloseIssueReferences`), for example when a run is cancelled after its PR opened; adding a GitHub Projects / status-field integration;
  closing the issue directly instead of through `Closes #N`; any `gh` scope beyond `repo`; changing
  `_refuse_duplicate_issue` to read the label from GitHub; detecting epics from sub-issues or issue types (a
  GraphQL call); a watchdog or sweep that releases the label of a run whose runner died.
- **Never:** let a GitHub failure fail, stop or block a run; put issue text (labels included) into the comment, the
  PR body or an argv; write `Closes` for an issue with an epic label; open an issue run's PR without the issue
  line in its body; let a config key turn the PR link off; settle for a comment instead of a Development link for
  an issue that isn't an epic; give an epic a Development link; splice an id or any GitHub text into the GraphQL
  query; use
  `--force` on `gh label create`; call gh through a shell; change the golden explain files; add a third-party import.

## Success Criteria

- Acceptance criteria 1–13 above each have a passing test in `test_issue_status.py`.
- `bin/agentflow validate` prints `0 warnings`, and `bin/agentflow config show --effective issues` shows all three keys.
- `test_config_edit.py` and `test_portability.py` pass, and so does the full suite.
- The manual end-to-end check on a scratch issue shows the label and one comment after start, and no label after cancel.
  A run on the scratch issue that reaches `ship` shows the PR under the issue's Development section, and so does a
  run whose `project.fork_from` isn't the default branch (the `api` path). Merging a PR to the default branch
  closes the issue. A run on an issue with an epic label shows the link comment, and its Development section stays
  empty.
- `CHANGELOG.md` has the Added and Changed lines, and playbook §8 and §14 are updated.

## Open Questions

None. The user resolved all three on 2026-11-10:

1. **A failed release leaves the label on the issue for good.** Resolved as recommended: `run_control.archive` also
   calls `release(run)`, which gives a second try with no new command (Call sites, acceptance criterion 10).
2. **A runner that dies without reaching `finally`.** Resolved: the developer handles this case by hand, by
   cancelling the run or removing the label. Nothing is added for it (Call sites, "Not covered").
3. **`Closes #N` on an epic.** Resolved: a PR never closes an epic. An issue with an `issues.epic_labels` label gets
   `Refs #N`, and the developer decides when to close it. Every other issue run keeps `Closes #N`, whatever its lane
   (Link the PR, acceptance criterion 9).
