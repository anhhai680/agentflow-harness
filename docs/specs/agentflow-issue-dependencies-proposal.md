# Proposal: show and enforce GitHub issue dependencies on the Backlog

Status: approved

Builds on `docs/specs/agentflow-issue-sync-proposal.md` (how a labelled issue becomes a Backlog card). That proposal
syncs issues but never reads the relationships between them. This one reads GitHub's own **Relationships** field
(the sidebar menu: *Add parent*, *Mark as blocked by*, *Mark as blocking*, *Add relates to*) and uses the
*blocked by / blocking* pair to order the Backlog. It adds no field, label, body convention or config key of its own.

## Objective

**Problem.** Two synced issues where #B is *blocked by* #A both show as `Ready` on the Backlog with an enabled
**Start** button. Nothing says that #B has to wait for #A. A developer starts both, so they run in parallel, and #B
is built on a base branch that does not yet have #A's change. The result is a conflicting or wrong PR and wasted runs.
The board even shows a *green light* (`Ready`, and `Parallel with` for form cards), which is worse than showing
nothing.

**Data already exists on GitHub.** The relationship is a first-class issue field: `Issue.blockedBy`,
`Issue.blocking`, `issueDependenciesSummary`, `parent`, `subIssues` and `relatesTo` are in the GraphQL schema, and
`gh issue list --json blockedBy,blocking,parent,subIssues` returns them (checked against the installed `gh` 2.103:
`"blockedBy": {"nodes": [...], "totalCount": N}`). agentflow only has to read it. Teams that mark the order in
GitHub's Relationships menu get it on the board with no other step. (An issue with nothing marked has no order, and
the board can only show that as unordered: the order has to be recorded in GitHub for the board to follow it.)

**Root cause.** Dependency information never enters the pipeline for a synced issue, and nothing in the pipeline
notices that it is missing. There are four gaps, in the order the data flows:

1. **The sync never asks for it.** `backlog.ISSUE_LIST_FIELDS = "number,title,labels,url"` (`backlog.py:61`) and
   `_parse_issues` keep `{number, title, lane, in_progress, url}`. `gh issue list` has offered `blockedBy`,
   `blocking`, `parent` and `subIssues` fields for a while, so the data is one flag away.
2. **A synced issue is built with no dependencies.** `snapshot` makes the card with `deps=[]`
   (`backlog.py:~683`). For the module lane, `_issue_run_params` adds the derived module `issue-<N>` with
   `"deps": []` (`backlog.py:544`), so the queue and the capability map see it as independent too.
3. **The dependency engine never receives issues.** `_picture` calls `module_queue.dependency_view(nodes, entries,
   book.cards(), ...)`. Its graph holds only `module:<id>` and `card:<id>` keys, so a synced `issue:<N>` is not a
   node, and it is not a *peer* of anything either. `snapshot` then does
   `view.get(key, {"blocked_by": [], "parallel_with": [], "parallel": False})`, and that default means
   "no blockers, not parallel", which becomes `status = "ready"`.
4. **The start rule trusts the same default.** `start` refuses a blocked item with
   `info = pic["view"].get(key); if info and info["blocked_by"]` (`backlog.py:849`). For an issue `info` is `None`,
   so the refusal never fires. The server cannot catch what the UI hid.

In short, **a missing entry is read as "independent"**. `dependency_view` is correct for modules and form cards
(they have `Runs after:` / `Parallel with:` lines and a 409 on Start); the issue source was added later
(`agentflow-issue-sync-proposal.md`) and was never connected to it. No test pins the missing behaviour.

## Decided behaviour

(The user's choices, confirmed 2026-11-14.)

- **One source: GitHub's native Relationships.** Edges come from `blockedBy` (and `blocking`, for display). A blocker
  is met when the blocker issue is **closed on GitHub** *or* its run here has a **merged** PR. An open PR, a live
  run, a failed run and an open issue all keep the dependent blocked, as for modules today.
- **Closed but still running here:** a blocker that is closed on GitHub but has a local run whose PR is not merged
  counts as **not met**. The more conservative reading wins, as `dependency_view` already does.
- **Blocker not in the synced set** (no sync label, another lane filtered out, or a different repository): still
  blocks while it is open. It cannot be started from this board, and the card says so.
- **Parent / sub-issue and *relates to* do not order work.** They group or link issues and imply no sequence, so v1
  does not let them block anything. They stay available for a later "Part of #P" display line.
- **Issue text is not read.** Titles are shown as today; bodies are not fetched. The sync proposal's boundary ("never
  put issue text into an argv, a module id, a map row or a refusal") stays exactly as it is.
- **Absence must not look like independence.** When dependencies could not be read (see Failure behaviour), the
  card says so and Start is refused, instead of showing `Ready`. `agentflow doctor` flags a `gh` that lacks
  `blockedBy`.
- **A blocker in another repository holds the dependent** until it is closed on GitHub. It is identified by its
  repository, so it never matches a local issue of the same number.
- **No override.** There is no "Start anyway". The blocked card has no Start button and the server answers 409. To run
  in parallel on purpose, remove the relationship on GitHub.
- **The module lane's `issue-<N>` row keeps `deps: []`.** The Backlog is the only place that starts issue runs, and it
  enforces the order. Revisit if `agentflow queue` ever starts issue modules itself.
- **No new config key.** The feature is on whenever `issues.sync_label` is set. A key would need all four
  obligations (Settings field, `validate` checks, `test_config_edit` case, playbook §14 line) for no gain.
- **Out of scope: `agentflow run <lane> --issue N` from the CLI.** A human typing that command chooses the issue
  on purpose, and the run's intake does not consult the Backlog. Only the Backlog (issue cards and form cards that
  carry an issue) enforces the order. If `agentflow queue` ever starts issue runs, it gets the check.
- **A closed blocker with an abandoned run here is met.** Only a live or open-PR run (the newest run with
  `params.issue == N`) holds a closed blocker; a failed, cancelled or archived one does not. Closing an issue as
  "not planned" frees its dependents.
- **A form card that carries a synced issue inherits that issue's blockers** and is refused in the same way, so the
  New task form is not a way round "No override".
- **A blocker with no board card is a dependency target only.** It appears in `Runs after:`, never in
  `Parallel with:`, as `run:<id>` refs do today.
- **`doctor` checks the field list offline** (`gh issue list --json` with no fields), not with a networked call.

## Design

### 1. Read the relationships

- `ISSUE_LIST_FIELDS` becomes `number,title,labels,url,blockedBy,blocking`.
  A non-empty `nodes` entry was observed on public repositories (2026-11-14):
  `{"id": "...", "number": 311, "state": "CLOSED"|"OPEN", "title": "...", "url": "https://github.com/<owner>/<repo>/issues/311"}`.
  There is **no `repository` field**, and a blocker in another repository carries an ordinary `number` that can equal
  a local one, so a ref's identity is the `(owner/repo, number)` parsed from its `url` (the existing `ISSUE_URL`
  shape, plus the repo capture), never the number alone. Those recorded replies are the test fixtures.
- `_parse_issues` adds `blocked_by: list[ref]` and `blocks: list[ref]` to each entry, where `ref` is
  `{number: str, state: "open"|"closed", repo: str|None}`. Rules, as for `number` today:
  - `number` must be a positive int and pass `intake.issue_number`, and must equal the number in the node's `url`;
    anything else is dropped, and the entry gets `deps_complete: false` (a blocker that cannot be read is not
    "no blocker");
  - `state` is compared case-insensitively; anything but `closed` is `open`, which is the safe side;
  - `repo` is the `owner/repo` of the node's `url` when it fits `[\w.-]+/[\w.-]+` and differs from this repository
    (else `None`), and it is shown only as `owner/repo#N`. The node's `title` is never read, kept or shown;
  - a ref whose state is missing is `open` and blocks;
  - a cross-repository ref never matches a local issue or run, even with the same number (regression test);
  - when `totalCount` is larger than the `nodes` returned, the entry gets `deps_complete: false` (a truncated
    list is not a complete one). The card is then treated like an unreadable one.
- The cache keeps the same `issues` list in `backlog_state.json`. `_synced` re-validates each ref as it already
  does for `url` and `title` (the cache is a file, so it is read as untrusted). An entry with no `blocked_by` key (a
  cache written before this change) or with a malformed one gets `deps_complete: false`, with the reason
  `refresh the board to read them`.
- **Old `gh`.** When `gh issue list` fails with `Unknown JSON field` (matched case-insensitively in the `GhError`
  text), the sync asks once more with `number,title,labels,url`, and every entry gets `deps_complete: false` with
  the reason `update gh`. `issues_note` says the same.

### 2. Put issues into the dependency graph

`module_queue.dependency_view` gets a third kind of node, `issue:<N>`, beside `module:` and `card:`. A cross-repository
blocker is `issue:<owner>/<repo>#<N>`, so it can never equal a local key.

- New keyword parameter `issues: list[dict] | None = None`: the synced entries that have a card on the board (those
  `_issue_blocker` lets through), each with `number`, `blocked_by` and `deps_complete`. With `None` or `[]` the result
  is exactly today's (regression test).
- **Keys and targets.** Only those board issues are *keys* (they get an entry in the result and can be
  `parallel_with` something). A blocker that is not a board issue (closed, not synced, another repository, or hidden
  because its run is live) is a dependency *target* only, as a `run:<id>` ref is today. So `parallel_with` never
  lists something the board has no card for.
- `deps["issue:N"] = [issue:<b> for b in blocked_by]`, so `ancestors` and `peers` work on issues unchanged. Two
  board issues with no path between them are `parallel_with` each other, and so are an issue and an unrelated module
  or form card.
- **The run of an issue.** The run of `issue:N` is the newest run here with `params.issue == N`, whatever started
  it (the issue card or a form card that carries the issue).
- `is_done("issue:N")` is true when the issue is closed (its ref says `closed`) **and** its run, if that run is
  live or `done` (PR open), has `merged[run_id] is True`. A failed, cancelled or archived run does not hold a closed
  issue, so closing an abandoned issue frees its dependents. An open issue is never done, even when its PR merged
  (GitHub closes it on merge, and the next sync reads that).
- `state_word("issue:N")`: the run's `_RUN_WORDS` word when it has a run, else `in Backlog` for a board issue,
  `open (not on this board)` for an open target, `closed` for a closed one. A cross-repository target is always
  `open` while it blocks.
- `running`, `started` and `can_run_beside` gain an `issue` branch (today they assume any non-module key is a form
  card and would raise `StopIteration` on one): a board issue is `starting` while its overlay record is, and never
  `started` (its card leaves the board when its run starts).
- `owner` is unchanged: a form card's run stays owned by `card:<id>`. Only the blocker lookup above goes from an
  issue to its run.
- **Cycles.** `dependency_view` has no cycle check today (`ancestors` keeps a seen set, so it does not loop, and each
  member of a cycle is blocked by the next). The issue keys reuse `_cycle_through`: every issue on a cycle gets
  `cycle: "#3 -> #5 -> #3"`, which `snapshot` shows as the card's note.
- The text of a blocker is always `issue #N (<state>)` or `owner/repo#N (open)`, never a title, so `blocked_by`
  carries no issue text, as the boundary requires.

`backlog._picture` passes `issues=` the board issues, and `_recheck_after` also collects the run ids of the issue
runs that a synced issue waits on, so `merged[...]` is current for them.

**A form card that carries an issue** (`params.issue == N`, from the form's issue field) gets issue N's blockers as
its own deps, from the synced entry, and is refused in the same way. Without this the form would be a way round
"No override". When N is not synced, the card has no issue blockers (as today).

### 3. Show it

`snapshot` fills the issue card's `blocked_by`, `parallel_with` and `parallel` from the view like every other card,
and adds `blocks` (the reverse edges, as `[{key, label}]`). `dashboard.html` already renders `Runs after:` and
`Parallel with:`, and a card whose status is `blocked` already has the style and no Start button. The change in the
UI is small:

- a `Blocks: #12, #14` line (new, from `blocks`), so a developer sees the order from the *first* issue as well;
- `Runs after:` lists each blocker with its state (`issue #7 (running)`), as for modules;
- the unreadable case shows `Dependencies unknown` with its reason (`item.note`, already rendered).

### 4. Enforce it on the server

- `start` finds the issue in the view, so the existing `blocked_by` refusal (`409`) now fires. No new refusal path.
- The refusal for an unreadable card (`deps_complete: false`, or the sync could not ask for the fields) is
  `issue #N: its dependencies could not be read: <reason>` with fixed text only.
- The live recheck `_taken_now` (in `run_start`, after `start` has answered) already runs
  `gh issue view N --json state,labels`. It adds `blockedBy`, parses the refs with the same function as the sync, and
  when a blocker is not met (with the `is_done` rule) the start ends as `item_failed` with
  `issue #N is blocked: runs after ...`, so the card stays in Backlog with that reason (it is not a 409, because the
  answer has already gone). When GitHub cannot answer, the decision already made from the cache stands, as for
  closed/taken today. A blocker added since the last refresh is caught here.

### Failure behaviour

| Situation | Board | Start |
|---|---|---|
| Sync works, no blockers | `Ready` or `Parallel` | allowed |
| Open blocker (synced, not synced, other repo) | `Blocked`, `Runs after: ...` | refused, 409 |
| Blocker's PR merged here, or blocker closed | not a blocker | allowed |
| `gh` too old for `blockedBy` (`unknown JSON field`) | sync retries once without the dependency fields, shows `Dependencies unknown: update gh` | refused, 409 |
| `gh` fails once the last list is cached | last list stays (with its dependencies) and `issues_note` says so, as today | decided by the cached dependencies |
| `blockedBy` truncated (`totalCount` > nodes) | `Dependencies unknown` | refused, 409 |
| Dependency cycle between issues | `Blocked`, note `dependency cycle: #3 -> #5 -> #3` (with `_cycle_through`) | refused, 409 |
| Cache written before this change | `Dependencies unknown: refresh the board to read them` | refused, 409 until the next refresh |
| Blocker added on GitHub after the last refresh | `Ready` until the next refresh | caught by the live recheck: `item_failed`, card stays |

`agentflow doctor` gains one check after `gh`, only when `issues.sync_label` is set. It runs `gh issue list --json`
with no field list, which prints the fields this `gh` knows and needs no network or login (checked with gh 2.103).
`ok` when `blockedBy` is listed. Otherwise `fail` with the fix `update gh: the Backlog cannot read issue
dependencies`.

## Boundaries

- **Always:** read the relationships through the existing `gh_json` argv call (no shell, no extra call at
  render time); re-validate every ref read from the cache; show `Runs after` / `Blocks` from numbers and states
  only.
- **Never:** put an issue title, body, label or URL of a *blocker* into an argv, a module id, a map row, an event or
  a refusal; fetch or parse an issue body; treat a missing or unreadable dependency as "none"; start a
  blocked issue because the browser let it (the check lives on the server).
- **Ask first:** letting `parent` / `subIssues` / `relatesTo` order work; any config key for this feature.

## Testing

- `test_backlog.py` (new cases, with recorded `gh issue list` replies):
  - the sync's argv carries `blockedBy,blocking`;
  - a parsed entry's refs: valid, closed, other-repo, hostile (non-int, huge, bool, nested), truncated;
  - a three-level chain from recorded `blockedBy` replies: #10 root, #11 after #10, #12 after #11, #13 after #10 and
    #12 → only #10 is startable, and #10 shows `Blocks: #11, #13`;
  - `parent`, `subIssues` and `relatesTo` present in a reply change nothing;
  - **regression for the root cause:** two issues, #2 blocked by open #1 → #2 is `blocked` with
    `Runs after: issue #1 (open)`, #1 shows `Blocks`, and `start issue:2` raises `Refused` (409);
  - #1 and #3 with no relation are `parallel` with each other, and #2 is not parallel with #1;
  - the blocker's run here: running, PR open, merged (`merged[run_id]`) → blocked, blocked, free;
  - blocker closed on GitHub but its local PR is open → still blocked; closed with only a failed or cancelled run
    here → free;
  - blocker outside the synced set is still a blocker and is not in anyone's `parallel_with`;
  - a cross-repository `#1` blocks local `#2` and is not the local `#1`;
  - the old-`gh` fallback: the retry without the fields, the note, and Start refused;
  - a cache entry with no `blocked_by` → `Dependencies unknown`, Start refused;
  - a cycle between issues is reported and not looped on;
  - a form card with `issue: 2`, where #2 is blocked by open #1 → refused;
  - the live recheck: the cache says free, `gh issue view` says an open blocker → `item_failed`, card stays;
  - in the module lane, an unblocked issue-backed module and a blocked one.
- `test_backlog.py`, beside `test_dependency_view_follows_dependencies_transitively_for_parallel_with` (there is no
  `test_module_queue.py`): `dependency_view` with `issue:` keys (including a board issue that is `parallel` with a
  running module, which today would raise `StopIteration`), and a byte-equal result with `issues=None` for the
  existing module and card cases.
- `test_dashboard.py`: `snapshot` JSON carries `blocked_by`, `blocks`; `POST` start on a blocked issue is `409`.
- `test_doctor.py`: the dependency check on (`blockedBy` listed, not listed) and absent when `sync_label` is unset.
- `tests/bin/gh`: `issue list --json` with no value prints a field list (`$FAKE_GH_JSON_FIELDS`), and
  `$FAKE_GH_OLD` makes a `--json` containing `blockedBy` fail with `Unknown JSON field: "blockedBy"`.
- The golden files `agentflow/tests/fixtures/explain/*.txt` are untouched (no lane or phase change).
- Playbook §14 pack reference: no new key, so only a note in the Backlog/issues section that blockers hold an issue
  back; `CHANGELOG.md`: one `Added` line (blocked issues show and cannot start) and one `Fixed` line (issues no
  longer show `Ready` while blocked).

## Files

| File | Change |
|---|---|
| `agentflow/backlog.py` | `ISSUE_LIST_FIELDS`, a `_parse_refs` helper used by `_parse_issues`, `_synced` and `_taken_now`; old-`gh` retry; `_picture` passes `issues=`; `snapshot` adds `blocks` and the unknown/cycle notes; `start` refuses unknown deps; form-card issue deps; `_recheck_after` collects issue runs |
| `agentflow/module_queue.py` | `dependency_view(..., issues=)`: `issue:` keys and targets, `is_done`/`state_word`/`running`/`started` branches, cycle note |
| `agentflow/doctor.py` | the `gh` dependency-fields check |
| `agentflow/dashboard.html` | the `Blocks:` line |
| `agentflow/tests/bin/gh` | field list and old-`gh` modes |
| `agentflow/tests/test_backlog.py`, `test_dashboard.py`, `test_doctor.py` | the cases under Testing |
| `docs/agentflow-playbook.md`, `CHANGELOG.md` | the note and the two lines |

None of these is on the security overlay's paths. The change still parses untrusted `gh` output and a cache file,
so the plan applies the `security-and-hardening` input rules to `_parse_refs` (types, bounds, URL shape, no text
kept).

## Commands

```bash
uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_backlog.py agentflow/tests/test_module_queue.py agentflow/tests/test_dashboard.py agentflow/tests/test_doctor.py --import-mode=prepend -q
uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_portability.py --import-mode=prepend -q
uv run --no-project --with pyyaml --with pytest pytest agentflow/tests --import-mode=prepend -q   # before finishing
bin/agentflow validate     # 0 warnings (no config change expected)
bin/agentflow doctor       # the new line, in a repo with issues.sync_label set
```

## Code style

As the module already writes it: small pure helpers, `str` issue numbers, refusals as fixed text plus numbers.

```python
def _parse_refs(field: Any, here: str | None) -> tuple[list[dict], bool]:
    """`blockedBy`/`blocking` as `[{number, state, repo}]` and whether the list is complete. A node with no valid
    number, or whose URL names a different number, is dropped; its title is never read."""
```

## Success criteria

1. Two synced issues where #2 is blocked by open #1: the board shows #2 as `Blocked` with
   `Runs after: issue #1 (open)` and no Start button, #1 shows `Blocks: #2`, and `POST` start of `issue:2` is 409.
2. When #1's run has a merged PR or #1 is closed (with no live or open-PR run here), #2 is `Ready` after the next
   refresh and starts.
3. No path shows `Ready` for an issue whose dependencies are unknown (old `gh`, truncated list, old cache):
   each shows `Dependencies unknown: <reason>` and Start is refused.
4. No issue title, body, label or URL of a blocker appears in an argv, module id, map row, event or refusal (a
   test with a hostile blocker title asserts it is absent from the snapshot's `blocked_by` and from the event log).
5. `dependency_view` called without `issues` gives the same result as today for every existing test.
6. The golden explain files are byte-identical; `test_portability` passes; the full suite passes.

## Alternatives considered

- **Parse `Depends on` sections in issue bodies.** Rejected: it would be a second, spoofable source that differs
  from what GitHub's own UI shows, and it puts issue text in the path. The native field is the one place to
  record order.
- **Show a warning only, leave Start enabled.** It is the smallest change, but the failure is a click that starts
  parallel runs, and the server would still accept it. Rejected.
- **A `depends-on:<N>` label or a new agentflow field.** Needs a convention and new config. GitHub's native
  Relationships already exist.
- **Ask GitHub per card at render time.** N calls per refresh, and `snapshot` is documented as `gh`-free. Rejected;
  the sync call already has the data.

## Open Questions

None. The five questions raised in review were answered on 2026-11-14 (see Decided behaviour); the node shape was
read from public repositories (see Design 1). The five raised by checking the design against the code were also
answered on 2026-11-14 and are listed at the end of Decided behaviour.
