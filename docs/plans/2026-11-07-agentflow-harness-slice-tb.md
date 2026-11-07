# Slice TB: a team-shared Backlog on GitHub, with modules from spec files

## Context

Everything agentflow tracks today is local to one machine. Runs, `backlog.jsonl` and `queue.json` all live in `~/.local/state/agentflow/<repo-id>/runs/` (`state.runs_root` → `pack.state_dir`). A run's branch reaches origin only at `ship`. So teammates' dashboards show different backlogs, two people can start the same module, and nobody can see who is working on what. On top of that, the Backlog can only start modules from a `capability_map` work source plus `dashboard.backlog.module_lane`. This repo's pack, like every sparse pack `init` writes, has neither, so features have to be started one by one from the CLI.

Old work is recognised in only one way: a module is `done` when the `branch_merged` condition holds for its `flow/<lane>/<slug>` branch (`module_queue.branch_is_merged`, `cached_entries`). Work merged before agentflow, or on a hand-named branch, is never seen as done, so it would show up as startable.

**Goal:** one Backlog per repository that is identical for every team member. Every dashboard shows, with no manual checking:
- which items can run now
- which can run in parallel with which
- which are blocked, and by what
- which a teammate has picked up (who, phase, status, PR, stale or not)
- which are done, including work that finished before agentflow

Starting an item claims it atomically, so it can't be double-started.

**Decisions (user):**
- The shared store is GitHub Issues plus PRs.
- Hand-off means visibility and claim. The run keeps running on the starter's machine, and teammates continue through the pushed branch and the PR.
- Modules come from spec files, plus feature task cards.
- Multi-repo dashboard: dropped for now.
- Nothing starts by itself. The harness automates the *knowing*; a person still presses Start. This keeps backlog.py's existing principle.

Three PRs (A → B → C), each a feature-lane slice. Each one's first step saves its part of this plan as `docs/plans/2026-11-07-agentflow-harness-slice-tb-{a,b,c}.md`. Each one's last step records the as-built deviations in a new proposal section (§1d "Team backlog") and a §5 line.

---

## PR A: modules from spec files, and recognising completed work (local, no GitHub)

### A1. `work_source.kind: specs`
- `config.py`: `WorkSource.kind` becomes `Literal["capability_map", "specs"]`. `path` is optional for `specs` and defaults to `project.docs.specs`; the file pattern is `project.docs.spec_file`. Update `_work_source` (`config.py:668`) and the validator `work_source()` (`config.py:1201`): for `specs`, the path is a directory inside the repo.
- `work_source.items()`: for `specs`, reuse `sources.spec_modules` (`sources.py:305`) and return `ModuleNode(id, deps)`. Deps come from an optional `Depends on:` line in the spec's header, parsed like the capability map's cell, so parallelism works for spec modules too. The same `_unsafe` path check applies. `add_item` is a no-op for `specs`, because the spec file the define step writes is the entry.
- `pipeline._check_new_module` / `_add_module_row` (`pipeline.py:158`): with `specs`, no `module_row` is needed and no row is committed.
- `backlog._picture` (`backlog.py:359`): the `QueueState` fallback applies to `capability_map` only. `{capability_map}` (`conditions.make_ctx:77`) stays `""` for `specs`.

### A2. Feature task cards
- A `new_module` card (`dashboard.backlog.new_task.feature: [new_module, after]`) under `specs` spawns `run feature --module <id> --task <responsibility>`. `task` is already in `module_queue.SPAWN_PARAMS`, and `intake` already writes `{run_dir}/task.md`. Check that `cmd_run` accepts `--module` with `--task` (`flow.py:~535`).
- `prompts/spec.md`: add one line: "If `{run_dir}/task.md` exists, it is the developer's description of this module: build the spec on it." This is the placeholder `prompts/debug.md:3` already uses.

### A3. Done detection for old work (`module_queue.cached_entries` / `assess_modules`)
A module is `done` when any of these holds, checked in this order and recorded with the reason it matched:
1. the existing `branch_merged` check on its branch
2. its spec's status line matches a new `project.docs.done_status` regex (default `^Status:\s*(done|shipped|implemented)\b`), read with the existing `sources.spec_status` style
3. its newest plan (`sources.find_plan`) has every `- [ ]` ticked, using the existing `plan_tasks` condition logic

A Done card shows the reason ("merged PR", "spec says done", "plan complete"). For anything still wrong, A adds `agentflow backlog done <module>`, which records a local `item_done` in `backlog.jsonl`. In PR B the same command closes the shared issue.

### A4. `init` and this repo's pack
- `init.py`: when a docs folder is detected, write `work_source: {kind: specs}`, `dashboard.backlog.module_lane: feature` and `new_task: {feature: [new_module, after], bug: [issue, task, after]}`. The engine defaults are unchanged, so the fixture packs and `tests/fixtures/explain/*.txt` stay byte-identical.
- `.agentflow/config.yaml` (edited interactively): add the same three keys. Mark the shipped slices' proposals done if they need it.

### A tests
- `test_work_source.py`: `specs` listing, the deps line, path safety, `add_item` as a no-op
- `test_backlog.py`: spec cards, Start spawning `--module`, a feature card spawning `--module --task`, each done reason, `backlog done`
- `test_config.py`: validate for the kind, the path and the `done_status` regex
- `test_init.py`, `test_plug_and_play.py`: the `specs` source is written

---

## PR B: the shared Backlog on GitHub (the read side, plus creating items)

### B1. Config: a new top-level `team:` block. It is off in the engine defaults, so the fixtures don't change
`team: {backlog: github, label: agentflow, stale_h: 24, push_each_phase: true}`
- `backlog: github | none`
- `label`: the label that marks an issue as a Backlog item
- `stale_h`: hours with no status update before a claim shows as stale
- `push_each_phase`: used by PR C

`init` writes `team.backlog: github` when `origin` is on GitHub. Every key ships with the four obligations:
- `team.backlog` and `team.label` become `_PROTECTED_KEYS`, since they are shared behaviour and change through git
- `team.stale_h` and `team.push_each_phase` become Settings Form fields
- validate checks for all four
- a `test_config_edit.py` case each
- playbook §14 lines

### B2. New module `agentflow/team_backlog.py`
- `fetch(cfg, repo) -> TeamSnapshot` makes **one** `gh api graphql` call per refresh, through `gitops.gh_json` with argv only and no shell. It reads:
  - the open issues with `team.label`, plus the ones closed in the last N days, each with its labels, assignees, `authorAssociation`, `closedByPullRequestsReferences`, and the newest comment carrying the `<!-- agentflow:status v1 -->` marker
  - the PRs whose head matches `project.branch`
  - plus one `git ls-remote origin 'refs/heads/agentflow/claims/*'`
- **Item metadata** sits in a fenced ```` ```agentflow ```` YAML block in the issue body: `lane`, `module`, `after: ["#12", "module:auth"]`. It is parsed defensively; a bad block becomes a note on the card and never an error.
- **Merging into one list:**
  - Every spec module (PR A) is an item.
  - An issue whose block names `module: X` *is* that module's item.
  - Other issues are task cards: bug or feature with `task` text.
- **Status per item:**
  - `done`: the issue is closed as completed, a linked PR is merged, or one of PR A's done reasons holds
  - `picked_up`: a claim branch exists. The assignee, phase, status, PR and `updated_at` come from the status comment, and `stale` is set when `now - updated_at > stale_h`
  - `blocked`, `parallel` or `ready`: from the existing `module_queue.dependency_view`, using deps, `after` and the merged state
- **Trust:**
  - An item is startable only if the issue author's or labeller's `authorAssociation` is OWNER, MEMBER or COLLABORATOR. Other items show "needs a maintainer".
  - Status comments are read only from those associations.
  - Issue text reaches an agent only as `--task` / `--issue`, under the same posture as the bug lane today.
- **Cache:** the result goes to `runs/team_backlog.json` with a timestamp. The page renders from the cache; the refresh loop (`dashboard._refresh_loop`, every `REFRESH_EVERY_S`) updates it. When `gh` fails, the page shows "team backlog unavailable: <reason>, showing <age> old data", and Start is refused.

### B3. Wiring
- `backlog.snapshot` / `begin_start` (`backlog.py:400`, `:477`): with `team.backlog: github`, cards come from `team_backlog` instead of `backlog.jsonl`. The local log stays as the store for packs without `team`.
- Card keys: `issue:<n>` and `module:<id>`.
- New task form → a GitHub issue, always (B5).
- CLI:
  - `agentflow backlog add --lane <lane> [--task …] [--issue N] [--module id --responsibility …] [--after …]`: the same path as the form (B5)
  - `agentflow backlog list`: the same view, as text
  - `agentflow backlog publish [--yes]`: creates issues for the spec modules that aren't done and have no issue yet. Without `--yes` it only prints what it would create.
  - `agentflow backlog done <key>`: closes the issue with a comment
  - `agentflow next`: names the ready team items
- `dashboard.html`: the Backlog column gets sections **Ready**, **Can run in parallel** (with what), **Blocked** (by what), **Picked up** (avatar/login, phase, status, PR link, stale badge) and **Done** (with its reason). There is a "Mine" filter, and Start shows only on Ready and Parallel.

### B4. Always sync before starting
Every start path refreshes before it decides anything: the dashboard Start (`backlog.begin_start` / `run_start`), `agentflow run` for a Backlog item, `agentflow queue` before each spawn, and `agentflow next`. A new `team_backlog.sync(cfg, repo)`:
1. runs `git fetch origin <base_branch> 'refs/heads/agentflow/claims/*:refs/remotes/origin/agentflow/claims/*'`, plus the PR heads (with prune), reusing `gitops.fetch_remote_branch`
2. calls `fetch()` with the cache bypassed and writes the cache again
3. re-computes the item's status from that fresh snapshot

The start goes ahead only when the item is still Ready or Parallel: not done, not picked up, not blocked. The refusal says what changed, for example "picked up by @x 3 min ago", "done: PR #41 merged" or "blocked: #12 is still open". A failed sync refuses the start ("couldn't sync with GitHub: <reason>"). It never falls back to the cache. The page shows the cached view between refreshes, but Start never trusts the cache.

Spec modules and done detection (PR A) read the spec, plan and map files **from `origin/<base_branch>` after that fetch**, through `git show`/`git ls-tree`, the way `detect.py` reads the base branch, and never from the working tree. So a teammate's merged spec or ticked plan counts as soon as it reaches origin, even if the local checkout hasn't been pulled. Without team mode, PR A's local `sync_base` step (`backlog.run_start` → `gitops.sync_branch`) moves before the checks in `begin_start` for the same reason.

### B5. Every new task becomes a GitHub issue
In team mode, a task created on the dashboard (`POST /api/backlog` → `backlog.add`, `backlog.py:245`) or with `agentflow backlog add` **always** creates a GitHub issue on `origin`'s repository, and the card *is* that issue (key `issue:<n>`). No local-only card exists in team mode, so a task created on one machine shows up on every teammate's dashboard at their next refresh.

A new `team_backlog.create_item(cfg, repo, lane, fields, after, request_id)`:
1. **Syncs first** (B4), then runs the existing validation (`_text`, `_check_after`, `_new_module_fields`, `_refuse_duplicate_issue`) against the fresh snapshot. A module id that already has a spec or an open issue is refused with "module X already exists (#n)".
2. **Builds the issue:**
   - **Title:** the task's first line (cut to `LABEL_MAX`), or `<module>: <responsibility>` for a new module.
   - **Body:** the task text, then the fenced ```` ```agentflow ```` block (`lane`, `module`, `after`, `to_phase`), then `<!-- agentflow:request <request_id> -->` and "Created from the agentflow dashboard by @login".
   - **Labels:** `team.label` plus `agentflow:lane:<lane>`, created on first use with `gh label create` if missing (never `--force`, so an existing label's colour and description are never changed).
3. **Creates it** with `gh issue create --title … --body-file … --label …`, argv only, the body through a temp file. `gh` must print the issue URL, else the call failed.
4. **Writes the new issue into the cache** right away, so the creator's card appears without waiting for the next refresh, and returns `{key, number, url}`. The page shows the issue link on the card.

The details:
- **A bug card that names an existing issue** (`issue` field) doesn't create a second issue. It adds `team.label` and `agentflow:lane:bug` to that issue and posts one comment with the metadata block. It never edits someone else's issue body, and it refuses if that issue is already a Backlog item.
- **Idempotency:** the page sends a `request_id` (a UUID made when the form opens). The server keeps a short-lived `request_id → issue` map under `_SPAWN_LOCK`. When `gh` times out with an unknown outcome, it searches for the marker (`gh issue list --search "agentflow:request <id> in:body"`) before trying again, so a double click or a retry never makes two issues.
- **On failure** (no `gh` auth, network, rate limit), the request answers 502 with the reason and **nothing is stored locally**, so there's no orphan card that only one machine sees. The form keeps the text so the developer can retry.
- **Removing a card** (`/api/backlog/remove`, `backlog.remove`): only the issue's creator, or a Backlog item nobody has picked up, may be removed. It closes the issue as "not planned" with a comment, and never deletes it.
- **Limits:** the task text stays under the existing `TEXT_MAX` (16 KB), well under GitHub's 65,536-character body limit. The title is one line with control characters stripped.
- **Outward-facing:** this publishes text to GitHub. The New task form says "Creates a GitHub issue in <owner/repo>" above its submit button, and `team.backlog: github` in the pack is the team's durable permission for it.
- **Without team mode** nothing changes: cards stay in the local `backlog.jsonl`.

### B tests
- `test_team_backlog.py`, new-task cases (through the fake `gh`, which records argv):
  - the form creates exactly one issue with the title, body block, marker and labels, and the card appears from the cache without a refresh
  - a missing label is created, an existing one left alone
  - a bug card naming issue N labels and comments on N, with no new issue
  - a timeout followed by a marker match makes no duplicate
  - a `gh` failure gives 502 and leaves no local card
  - a duplicate module refused after sync
  - remove closes the issue as not planned
  - without team mode, the local `backlog.jsonl` path is unchanged
- `test_team_backlog.py`, sync cases:
  - Start refused when the fresh snapshot shows the item picked up, done or blocked, even though the cached snapshot said Ready
  - Start refused when the fetch fails
  - done detection uses `origin/<base>` content that isn't in the working tree
- Extend the fake `tests/bin/gh` to serve canned `api graphql`, `issue create` and `issue close` responses.
- `test_team_backlog.py` (new):
  - each status
  - the trust filter
  - a malformed metadata block
  - module and issue merging
  - each done reason
  - a `gh` failure falling back to the cache with Start refused
  - `publish` dry-run vs `--yes`
- `test_backlog.py` / `test_dashboard.py`: team mode cards and the New task form → issue.

---

## PR C: claiming on Start, and publishing run status

### C1. The atomic claim (`team_backlog.claim`, called from `backlog.run_start` and from `cmd_run`/`cmd_queue` when team mode is on, so the CLI and the dashboard follow the same rules)
0. **Sync first, always** (B4): fetch a fresh snapshot, re-check that the item is still Ready or Parallel, and refuse if it isn't, or if the sync failed.
1. Create a claim commit: an empty commit on `origin/<fork_from>` whose message holds JSON `{key, login, run, branch, at}`.
2. Push it with `git push --force-with-lease=refs/heads/agentflow/claims/<key>: origin <sha>:refs/heads/agentflow/claims/<key>`. The empty expected value means "create only if absent", which is atomic on the server. If it is rejected, refresh and refuse with "picked up by @x just now". Add this as `gitops.push_new_ref` (it gets a security review).
3. For a spec module with no issue yet, create the issue.
4. Assign `@me`, add the label `agentflow:picked-up`, and post the first status comment.

When the run starts, the runner pushes the work branch and opens the draft PR **at once** with "Closes #N". This reuses `gitops.push` / `create_draft_pr` (`gitops.py:430`, `:472`) and moves them earlier, so `finish` updates an existing PR instead of creating one. That needs a check of `sync_lane` / `ci_lane` and the PR body update (`update_pr_body`).

### C2. Status publishing (a new `team_sync` hook in `pipeline.py`)
- On `phase_started`, `hitl_raised`, waiting/approval, `ready`, failed, cancelled and done, plus a heartbeat at least every `stale_h/4` from the runner loop, the runner edits its one status comment on the issue. The comment holds a readable line plus a JSON block: run id, login, lane, phase, status, PR, `updated_at`. It never names a host or local paths.
- This is best effort. A `gh` failure emits `team_sync_failed` and never fails or blocks the run.
- With `push_each_phase`, the runner pushes the work branch after each phase's commits, so teammates can check it out and review.

### C3. Release and cleanup
- `agentflow backlog release <key>` (and a button for the claimer, or on a stale claim) deletes the claim branch, unassigns, and comments "released". The work branch and the PR stay, and a new claim continues from them only through a fresh run (out of scope here: resuming another machine's run).
- Cancel and archive release automatically.
- When an item goes Done, the claimer's runner, or any dashboard's refresh, deletes its claim branch.

### C tests
- A bare `origin` repo in tmp with two clones claiming the same key at once: exactly one wins and the other is refused with the winner's name.
- Status comment edits, made through the fake `gh`, per phase.
- A `gh` failure doesn't fail the run.
- Release and cancel delete the claim.
- Stale detection.
- Draft PR at start, then `finish` updating rather than creating it.

### Security (C touches `gitops.py` and `init.py`, and B handles untrusted issue text)
Plan and review B and C with `security-and-hardening` and the `security-auditor` agent. The points to cover:
- the `authorAssociation` trust filter
- no shell in `gh` or `git` calls
- claim JSON and comment JSON parsed with size limits
- no local paths or hostnames posted
- the `.agentflow/**` guard unchanged, since claims and status never write the repo's tree

---

## Critical files
- A: `agentflow/config.py`, `work_source.py`, `sources.py`, `backlog.py`, `module_queue.py`, `pipeline.py`, `prompts/spec.md`, `init.py`, `.agentflow/config.yaml`
- B: new `agentflow/team_backlog.py`, plus `backlog.py`, `dashboard.py`, `dashboard.html`, `flow.py` (`backlog` subcommand), `config.py`, `config_edit.py`, `defaults/config.yaml` (`team: {}`), `tests/bin/gh`
- C: `team_backlog.py`, `gitops.py` (`push_new_ref`), `pipeline.py` (claim, early PR, `team_sync`), `sync_lane.py`/`ci_lane.py` (an existing PR at finish), `run_control.py` (release on cancel/archive)
- Reuse: `sources.spec_modules`/`spec_status`/`find_plan`, `module_queue.dependency_view`/`branch_is_merged`/`spawn_run`, `gitops.gh_json`/`push`/`create_draft_pr`/`update_pr_body`, `intake` (task.md), `dashboard._refresh_loop`
- Must not change: `agentflow/tests/fixtures/explain/*.txt`

## Verification
- Per PR: the touched test files, plus `test_portability.py`. `bin/agentflow validate` → "0 warnings", and `git diff --stat agentflow/tests/fixtures/explain/` is empty. Then the full suite: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests --import-mode=prepend -q`.
- Manual A: `bin/agentflow dashboard` in this repo shows `harness` as Done ("spec says done" / "plan complete" once its status line is set) and any new proposal as Ready. A new feature card spawns `run feature --module=<id> --task=…` (see `runs/queue.log`; cancel it).
- Manual B/C, on a scratch GitHub repo with two clones (two state dirs via `XDG_STATE_HOME`):
  1. `backlog publish --yes` creates the issues. A new task made on clone 1's dashboard appears as a labelled issue on GitHub and on clone 2's dashboard after one refresh.
  2. Both dashboards show the same Ready, Parallel and Blocked sets.
  3. Start in clone 1 → clone 2 shows "Picked up by @you", with the phase and draft PR, within one refresh.
  4. Start of the same item in clone 2 is refused, even before clone 2's page has refreshed, because Start syncs first.
  5. Merge the PR → both show Done.
  6. `backlog release` frees an item.
