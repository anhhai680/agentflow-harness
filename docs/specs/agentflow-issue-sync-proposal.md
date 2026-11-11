# Proposal: sync labelled GitHub issues into the Backlog, and mark every issue-backed run

Status: approved (2026-11-11)

Builds on `docs/specs/agentflow-issue-status-proposal.md` (mark, release and link a run's issue). That proposal
covers what a run does on its issue. This one covers how an issue reaches a run.

## Objective

1. **The dashboard never showed GitHub issues.** Backlog cards came only from the work source and the New task
   form, so picking up an issue meant typing it in by hand.
2. **A hand-typed feature card could not carry an issue number.** A specs pack's feature form was
   `[new_module, after]`. A card made for an issue got `{module, task}` and no `issue` param, so:
   - the run never labelled or commented on the issue;
   - its PR didn't say `Closes #N`;
   - its PR never reached the issue's Development section.

   Other people saw nothing on the issue.

## Decided behaviour (the user's choices)

- **Which issues sync:** only open issues carrying the opt-in label `issues.sync_label`. `""` turns the sync off.
- **Lane:** `issues.lane_labels` (issue label → lane, first match wins, compared without case) picks the lane;
  otherwise it is `issues.sync_lane`.
- **Claimed issues:** an issue that carries `issues.in_progress_label` while no run on this machine works on it
  is *taken elsewhere*. Its card shows, and Start is refused (409).
- **Feature form:** the feature form takes an `issue` field.
- **Development link:** every PR an issue run opens is linked in the issue's Development section. An epic gets
  a comment instead. A failed link is retried on every CI round as well as at archive.

## Design

- **Sync.**
  - `backlog.sync_issues` runs once per Backlog refresh (every minute in the dashboard). It calls
    `gh issue list --label L --state open --json number,title,labels,url --limit 100` as an argv list with no
    shell, and caches the result in `backlog_state.json`.
  - `snapshot` reads only the cache. A `gh` failure keeps the last list and sets `issues_note`.
  - A pack with no `dashboard.backlog` block still syncs.
- **Parsing.** For each entry:
  - the number must be a positive int and pass `intake.issue_number`;
  - the title is NFC-normalised, printable, one line and at most 200 characters, and it is only ever shown;
  - the URL is kept only when it is the `https` URL of that same issue;
  - label names are compared and then dropped.
- **Cards.**
  - A synced issue is the card `issue:<N>`.
  - It is hidden while a run here works on the issue or opened its PR, while a form card names the issue, or
    when a work-source module `issue-<N>` exists. That module carries the issue when it starts.
- **Run params.**
  - Outside the `module_lane`: `{issue}`.
  - In the `module_lane`: `{issue, module: "issue-<N>"}`. A capability map also gets the row
    `{module: issue-<N>, responsibility: "GitHub issue #N"}`.
  - The module id and the row are fixed text, never issue text.
  - The spec and plan prompts read the run's `issue.md` as data.

## Config (each key ships with its four obligations)

| Key | Default | validate | Settings |
|---|---|---|---|
| `issues.sync_label` | `""` | label shape (≤50 chars, no newline or comma, no leading `-`), and differs from `in_progress_label` | text |
| `issues.sync_lane` | `feature` | while the sync is on: a lane; a `module_lane` (warning); a `module_row` for a capability-map module lane | enum of lanes |
| `issues.lane_labels` | `{bug: bug}` | label shape for each key; while the sync is on, a lane the pack doesn't have is a warning | map |

## Boundaries

- **Always:**
  - call `gh` through `gh_json` or `_gh` (argv, no shell);
  - read the cache only from `snapshot`;
  - recheck the cache at Start.
- **Never:**
  - put issue text into an argv, a comment, the PR body, a module id, a map row, an event or a refusal message;
  - store or show label names;
  - start an issue that is taken elsewhere;
  - let a sync failure break the Backlog.
- **Supersedes:** the issue-status proposal's "Ask first" line on reading the label from GitHub. The label is now
  read from the sync's cache, and is used only to show *taken elsewhere* and to refuse Start.

## Testing

- `test_backlog.py`:
  - sync on and off, and the argv;
  - lane mapping;
  - failure notes;
  - hostile replies;
  - hiding, taken and start params for each work-source kind;
  - issue-only and issue-plus-module feature cards.
- `test_dashboard.py`: the start, 409 and remove routes for `issue:<N>`, and a sync without a backlog block.
- `test_issue_status.py`:
  - validation of the keys;
  - a `--module issue-7 --issue 7` run that is labelled, says `Closes #7` and is linked (`keyword` and `api`).
- `test_ci.py`: a link that failed at `finish` is retried each CI round until it holds.
- `test_config_edit.py`: the three fields' round trip and refusals.

## Open Questions

None.
