# Proposal: one `backlog.yml` for every backlog item, with each item's state on the Board

Status: approved (2026-11-24)

Builds on `docs/agentflow-harness-proposal.md` §1d (team backlog, slices TB-A1 and TB-A2),
`docs/specs/agentflow-issue-sync-proposal.md` (synced issues on the Backlog) and
`docs/specs/agentflow-issue-status-proposal.md` (the in-progress label as a claim). It adds a third `work_source`
kind, `backlog`, and keeps `capability_map` and `specs` working until 4.0.

## Objective

### The problem

A team using agentflow can't answer two questions from one place: **what is on the backlog**, and **what state is
each item in**.

1. **The item list is split across four stores, and none of them is complete.**
   - The work source is either a `CAPABILITY-MAP.md` table (`kind: capability_map`) or the spec files that fit one
     name pattern (`kind: specs`). A spec outside that table or pattern is not on the Backlog. This repository shows
     it: its pack lists `docs/agentflow-{module}-proposal.md`, so the Backlog has two modules (`harness`,
     `doubt-review`), and the twelve proposals in `docs/specs/` (cost, throughput, vuln-fix, issue-sync, …) are on
     no Board at all.
   - Cards added with the New task form live in `runs/backlog.jsonl` in the state dir, on one machine. Teammates
     never see them.
   - Synced GitHub issues show only with `issues.sync_label` set and `gh` working.
   - Hand "done" marks live in `runs/done_marks.json`, on one machine.
2. **The map has no state column.** A `CAPABILITY-MAP.md` row has `Module id`, `Responsibility` and `Depends on`.
   Whether a module is waiting, being defined, half built or merged comes from evidence the engine puts together
   (`module_queue.done_reason`: hand marks, a spec header, a ticked plan on origin, a merged branch). A developer
   reading the file sees none of it.
3. **A run's phase is visible on one machine only.** The Board's phase columns come from `state.json` under the
   local state dir. A teammate's board shows your item in the Backlog column as ready to start while your run is in
   `build`. Only issue-backed runs say otherwise, through `issues.in_progress_label`, and only on GitHub.
4. **So two people can start the same item.** Nothing outside GitHub issues stops it.

### What we build

- **`backlog.yml` at the repository root**: one committed file that lists every backlog item: its id, title, lane,
  spec, plan, dependencies, issue and planning status. It is the work source
  (`work_source: {kind: backlog, path: backlog.yml}`). Developers edit it by hand, with `agentflow backlog add|set`,
  or on the dashboard. The runner edits it only on a run's own branch.
- **The Board shows each item once, in its exact state:** in the Backlog column (`backlog`, `ready`, or blocked on
  a named dependency), in the column of the phase a run is in (`define`, `plan`, `build`, `verify`, `review`,
  `ship`, …, the pack's own `phases:`) with the run's status (running, waiting on a human, paused, stopped), or
  under Done.
- **A claim on `origin` for every started item.** When a run starts, the runner creates the item's claim ref, and
  the server makes that creation atomic. The runner moves the ref forward at every phase and status change. Every
  teammate's Board reads the claims. A second start of a claimed item fails before any worktree, branch or agent
  exists.
- **Specs that no item names are listed**, on the Board and in `validate`, with a one-step way to add them.

### Users and success

- *A developer opening the Board* sees every item in the repository, the phase and status each started item has
  on anyone's machine, and who started it.
- *A developer reading `backlog.yml`* in an editor or on GitHub sees every item and its planning status
  (`backlog`, `ready`, `done`, `dropped`) without running agentflow.
- *Two developers who press Start on the same item at the same time*: one run starts, and the other is refused
  with the claimant, the phase and the status.
- *agentflow itself* (a run that writes a new spec, or a feature card for a new module) adds or updates the item in
  the same commits as the work, so the file never falls behind the specs.

## Decided behaviour

Every decision below was settled in the grilling of 2026-11-24 (see Decisions). Nothing here is still open.

**D1. The file holds what people decide; the live phase and status come from the claim.** `backlog.yml` stores the
planning status (`backlog | ready | done | dropped`). It never stores `define`, `plan`, `build` or the other phases.
A run's phase and status are shown from the run (on this machine) or from its claim on `origin` (on any machine).
Storing the phase in the file was the first idea, and it fails on four counts:

- the runner never commits to the base branch, so a phase written to the file would sit on the run's branch, where
  no teammate sees it until the PR merges, which is after the work is done;
- every phase change of every run would be a commit to one shared file, which is the conflict the file is meant to
  prevent;
- a run that dies leaves its last phase written, and nothing removes it;
- `phases:` belongs to the pack, so a file written by phase name breaks when the pack renames or adds a phase.

The claim ref (D4) is the shared place that changes at every phase, and the Board puts the two together.

**D2. One item, one block, with fixed keys.** Items are a list, in priority order (the first is the next to
start, as the capability map's row order is today). The `id` is a module id (`sources.MODULE_ID`), unique and
never renamed. Text fields (`title`, `notes`) are shown on the Board and never reach an argv, a branch name, a
module id or a prompt.

**D3. The runner writes the file only on a run's branch, and only these changes:**

| When | The runner's change to `backlog.yml` on the run's branch | Merged with |
|---|---|---|
| A run starts from an item that isn't on `origin/<fork_from>` yet (a New task card, `--new-module`, a local-only item, D7) | the item's block, appended, as the branch's first commit (today's `_add_module_row`) | the run's PR |
| A run starts from a `backlog` item (D5) | `status: ready`, in the same first commit | the run's PR |
| `define` writes a spec at a path the item does not name | `spec:` set to that path | the run's PR |
| `plan` writes a plan | `plan:` set to its path | the run's PR |
| `finish`, before the push | `status: done` | the run's PR, so merging the PR marks the item done |

Agents in a run can't edit `backlog.yml`: the engine adds the work source's path to the paths a run always refuses,
as it does for `.agentflow/**`. A developer outside a run edits it like any file and merges it with a normal PR.

**D4. A claim is a custom ref on `origin`, created with a compare-and-swap.** Item `<id>`'s claim is
`refs/agentflow/claims/<id>`, or `refs/heads/agentflow/claims/<id>` with `backlog.claim_ref: branch`. Its commit
holds one file, `claim.json`:

```json
{"item": "backlog-file", "run": "20261124-backlog-file-1", "lane": "feature", "phase": "build",
 "status": "waiting", "by": "<git config user.name>", "started": "2026-11-24T09:12:03Z",
 "updated": "2026-11-24T10:40:55Z"}
```

- **Claiming** is `git push --force-with-lease=<ref>: origin <sha>:<ref>`, with an empty expected value. That
  means "the ref must not exist yet". The push sends the zero id as the ref's old value, and the server checks it
  after taking the ref's lock (`ref_transaction_update` in git's `refs.h`), so exactly one of two simultaneous
  starts wins. A local test of 30 simultaneous two-clone races had exactly one winner every time. The loser gets
  `claimed`, with the claim's `by`, `phase`, `status` and `updated`, before any worktree exists.
- **`status`** is `running`, `waiting` (a stop or approval waits on a human), `paused` or `stopped` (failed,
  cancelled or crashed, before release). **`by`** is `git config user.name`, or `unknown` when it's unset. It is
  readable by anyone who can read the repository, as it is on every commit.
- **A phase or status change** pushes a new commit on top, leased on the previous sha. A lease that fails means
  someone released or took the claim, and the run stops with `claim_lost`, which a human resolves.
- **Release** is a leased delete, and it happens only on the claimant's machine:
  - when the run ends `failed` or `cancelled`, or is archived;
  - for a `done` run, when that machine sees the item's `status: done` on `origin/<fork_from>` (in `ci_lane`'s
    watch, the dashboard's refresh of its own runs, or `archive`).

  Other Boards never write to `origin`. They hide a claim whose item is `done` on origin.
- **A stale claim** is one not updated for `backlog.claim_stale_h` hours (24 by default), whatever its status. Its
  card reads, for example, "waiting on a human · 3 d · stale". Only a person frees it, with
  `agentflow backlog release <id>` (a leased delete). Nothing frees it automatically.
- **Reopening** an item (`agentflow backlog set <id> status=ready`, or the dashboard's Edit) also deletes its
  claim, with a lease, when the claim's phase is the pack's last phase and origin had the item `done`. Any other
  claim is left alone, and the command prints it.
- **A host that refuses custom refs**: the first claim fails with `claim_refused_by_host`. The start is refused,
  and the message names `backlog.claim_ref: branch`.
- **No `origin`, or claims off** (`backlog.claims: false`): runs start with no claim, and the Board says claims are
  off. This is a single-developer setup.

Custom refs are the default because they stay out of the branch list and the "Compare & pull request" banners, and
an unfiltered `on: push` CI workflow doesn't run for them. The default fetch refspec doesn't pull them either. The
hosts don't document custom namespaces: GitHub, GitLab and Bitbucket Cloud all appear to accept them, but that
rests on community reports, and `refs/pull/`, GitLab's `merge-requests`/`pipelines`/`environments` and
Bitbucket's `pull-requests` are reserved. `claim_ref: branch` is the fallback for a host that refuses them. Neither
form is covered by branch protection or rulesets, which target branches and tags by name.

**D5. One state per item, by precedence.** The Board and `agentflow backlog list` show the first of these that
holds:

1. `dropped` (file): hidden behind "Show dropped".
2. `done`: the file says `done` on `origin/<fork_from>`, or today's done evidence holds (`done_reason` without hand
   marks, D8). When the evidence says done and the file doesn't, the card says so, and `validate` warns, with
   `--fix` setting the file. A claim on a done item is not shown.
3. **the phase and status of a local run** (today's run cards and columns).
4. **the phase and status of a claim on `origin`**: a read-only card in that phase's column, with the claimant,
   the age, and `stale` when D4's rule holds.
5. `blocked`: a `depends_on` item is not done (today's dependency rules and messages).
6. `ready` or `backlog` (file). Start on `ready` starts the run. Start on `backlog` asks for a second click, and
   the run's first commit sets `status: ready` (D3).

**D6. Every spec is on the Backlog, or shown as missing from it.** The Board's Backlog column gets a folded
**Not on the backlog (N)** list: each Markdown file under `project.docs.specs` (recursively, at most
`sources.SPECS_MAX`) that no item's `spec:` names, with an **Add** button that pre-fills the add form (id from the
file name, `status: backlog`). `validate` warns about each, and `agentflow backlog add --spec <path>` adds one.
`backlog.ignore_specs` (a list of globs) silences files that are not backlog items (indexes, templates, retired
specs).

**D7. People edit the file on the dashboard, in the working tree; issues link to items.**

- **Add and edit.** The New task form's **Add** appends an item. Every Backlog card has an **Edit** dialog
  (`title`, `lane`, `status`, `depends_on`, `issue`, `spec`, `notes`), and **Move up** / **Move down** for priority.
  There's no drag-to-reorder.
- **Every page edit goes to the main checkout's working tree, uncommitted**, keeping comments and order. The
  dashboard never commits it, and the card is badged *local: commit to share*. A write is refused when the file
  changed on disk since the page read it (its sha-256 is sent with the edit), and the page asks you to reload.
- **Start on a local-only item** (one in the working tree and not on `origin/<fork_from>`) works. The run's first
  commit carries the item's block (D3), and its claim shows it to the team. The card then says your uncommitted
  copy went into the run, and that you should remove that entry (or `git checkout backlog.yml`, when nothing else of
  yours is in the file) before pulling the merge, because git refuses a merge over a locally modified file. The
  runner never touches your working tree.
- **Start now** on the New task form does what it does today: the item goes into the run's first commit.
  `runs/backlog.jsonl` keeps only start attempts and failures, and no longer holds items.
- **Issues.** An item can name `issue: N`, and a run of it does everything an `--issue N` run does today (the
  label, the comment, the PR link). When issue #N also carries `issues.sync_label`, the Board shows one card, the
  item, with the issue's link. The synced card is hidden, and the issue's in-progress label still marks the item
  as taken. A synced issue that no item names stays a card, with **Add to backlog**.

**D8. Migration, and the old kinds' deprecation.**

- **Nothing breaks.** `capability_map` and `specs` packs keep working. The engine version gets a minor bump per
  slice.
- **`agentflow backlog import [--dry-run]`** writes `backlog.yml` from the current work source, the local form
  cards and the done marks, then prints the `work_source` line to switch to.
  - **Status:** an item with done evidence (`done_reason`, hand marks included) gets `status: done`. Every other
    item gets `ready`, except that a form card gets `backlog`. `import` never writes `dropped`.
  - **Order:** open items first, in row order for a capability map and in dependency order for `specs` (each
    item after its dependencies, alphabetical within a level), then the `done` items, alphabetically.
- **Hand marks.** With the `backlog` kind, `agentflow backlog done <id>` is `set <id> status=done` and `undone` is
  `set <id> status=ready`, both working-tree edits that you commit. `runs/done_marks.json` is read only by
  `capability_map` and `specs` packs.
- **`agentflow init`** writes `work_source: {kind: backlog, path: backlog.yml}` and an empty `backlog.yml` for a
  repository with no specs folder, and offers `import` when it finds one.
- **Deprecation.** Once slice B4 ships, `validate` prints a note (not a warning, so "0 warnings" still holds) on a
  `capability_map` or `specs` pack, pointing to `agentflow backlog import`. The playbook marks both kinds
  deprecated, and 4.0 removes them.
- **This repository** migrates in slice B1 with `import`. The spec files under `docs/` and `docs/specs/` become
  items: shipped ones as `done`, open ones as `ready`.

**D9. `agentflow queue` starts `ready` items only.** With the `backlog` kind, `queue` starts an item when it is
`ready`, its dependencies are done, and it has no claim. It never starts a `backlog` item. `--include-specs` is not
needed and is ignored with a note.

## Design

### The file

```yaml
# backlog.yml: every backlog item, highest priority first. `agentflow validate` checks it.
# status: backlog (an idea) | ready (may start) | done | dropped. A run's phase is never written here: the Board
# shows it from the run's claim (refs/agentflow/claims/<id>).
version: 1
items:
  - id: backlog-file
    title: Keep the backlog in backlog.yml
    lane: feature
    status: ready
    spec: docs/specs/agentflow-backlog-file-proposal.md
    depends_on: [issue-sync]
    issue: 61
  - id: flaky-gate-timeout
    title: The verify gate times out on a cold uv cache
    lane: bug
    status: backlog
    notes: Seen twice on CI, never locally.
  - id: cost
    title: Bounded spend, bounded time
    lane: feature
    status: done
    spec: docs/specs/agentflow-cost-proposal.md
    plan: docs/plans/2026-11-20-agentflow-harness-cost-c6.md
```

| Key | Required | Meaning | Checked by `validate` |
|---|---|---|---|
| `version` | yes | the file's schema version, `1` | an integer the engine reads |
| `items[].id` | yes | the module id: kebab-case, unique, never renamed | `sources.MODULE_ID`, unique, ≤ 64 chars |
| `items[].title` | yes | one line for the Board | a one-line string ≤ 200 chars |
| `items[].lane` | no (default `dashboard.backlog.module_lane`) | the lane a start runs | a lane of the pack |
| `items[].status` | no (default `backlog`) | `backlog`, `ready`, `done` or `dropped` | one of the four |
| `items[].spec` | no | the item's spec, relative to the repository | inside the repository, not a symlink; a missing file warns (a run's `define` writes it) |
| `items[].plan` | no | the item's plan | as `spec` |
| `items[].depends_on` | no | item ids | known ids, no cycles (today's `_cycle_through`) |
| `items[].issue` | no | a GitHub issue number | a positive integer, unique across items |
| `items[].notes` | no | free text for people | a string ≤ 4 KB |

An unknown key is a `validate` warning, so a team can carry a field such as `owner` without the engine reading it.
The file is read with `yaml.safe_load`, with a 1 MB size limit and a 2000-item limit, and a malformed file empties
the Backlog with one error line. It never raises in the Board's refresh thread.

**Where it is read from.** The Board and `agentflow next` read the main checkout's working tree, so an uncommitted
edit shows at once, badged *local*. Done and claim decisions read `origin/<fork_from>` (`gitops.read_blob`), as
TB-A2's done evidence does. A run reads the file at its fork commit (`state.base_sha`), never from a working tree.

### Engine changes

| Module | Change |
|---|---|
| `sources.py` | `backlog_file(text) -> list[BacklogItem]` parses and validates. `BacklogItem` extends `ModuleNode` with `title`, `lane`, `status`, `spec`, `plan`, `issue`. `set_item_fields(path, id, fields, expect_sha)`, `append_item`, and `move_item(path, id, up)` edit the file as text, keeping comments and order, as `config_edit.py` does for `config.yaml`. With `expect_sha`, a file that changed since it was read is refused. |
| `work_source.py` | the `backlog` kind: `items`, `add_item` (append), `set_fields`, `file`; `skipped` returns the specs no item names (D6). |
| `claims.py` (new) | `claim`, `advance` (phase and status), `release`, `reopen_release` (D4's reopen rule), `list_remote` (one `git ls-remote origin '<prefix>/*'` plus a fetch of the changed shas into `refs/agentflow/remote-claims/*`), `parse_claim`. `parse_claim` validates every field: the id is a module id, the phase is a phase of the pack, the status is one of the four, times are ISO-8601, and `by` is a one-line string ≤ 100 chars. Anything else turns the claim into "unreadable", which still blocks a start. Uses `gitops.HARDENED` and `--no-verify`, like `gitops.push`. |
| `pipeline.py` | `start`: claim before `add_worktree`, and refuse on `claimed` or `claim_refused_by_host`. The first commit carries a new or local-only item's block and a `backlog` item's `status: ready` (generalises `_add_module_row`). Each phase change calls `claims.advance`. |
| `hitl.py`, `run_control.py` | a stop, an approval wait, `pause`, `resume` and `retry` call `claims.advance` with the new status. `cancel`, `archive` and a `failed` end release the claim. |
| `flow_control.py` | `finish`: sets `status: done` in a runner commit before the conformance audit and the push. |
| `ci_lane.py` | a `done` run's watch releases its claim once `status: done` is on `origin/<fork_from>`. |
| `lanes.py` | after `define`/`plan` commit their output: set `spec:`/`plan:` when the item's path differs. |
| `guards.py` | the `backlog` work source's path joins `.agentflow/**` as always refused in a run. |
| `module_queue.py` | `_assess` puts the file's `status` and the remote claims into the precedence of D5. A remote claim occupies no local queue slot, and `queue` follows D9. With the `backlog` kind, `done_reason` skips hand marks (D8). |
| `backlog.py` | the snapshot adds remote-claim cards, the "Not on the backlog" list, the *local* badge, absorbed issue cards (D7), and **Add to backlog** on unabsorbed synced issues. Add, Edit and Move write the file (D7). |
| `dashboard.py`, `dashboard.html` | endpoints and UI for Add, Edit, Move and reopen; remote-claim cards (read-only, claimant, status, age, `stale`); the local-only Start note. |
| `flow.py` | `agentflow backlog list [--json] | add | set <id> key=value… | release <id> | import [--dry-run]`; `done`/`undone` map to `set` with the `backlog` kind. |
| `config.py` | the `backlog` kind, the `backlog.*` keys, the file checks above, and the deprecation note (D8). |
| `doctor.py` | with claims on: one read-only `git ls-remote origin '<prefix>/*'`. It runs no push. |
| `init.py`, `detect.py` | D8. |

### Config (each key ships with its four obligations: a Settings field or `_PROTECTED_KEYS` entry, `validate` checks, a `test_config_edit.py` case, a playbook §14 line)

| Key | Default | Protected | Meaning |
|---|---|---|---|
| `work_source.kind: backlog` | n/a | yes (as `work_source` is) | the new kind |
| `work_source.path` | `backlog.yml` | yes | the file, inside the repository |
| `backlog.claims` | `true` | no | claim items on `origin` (D4) |
| `backlog.claim_ref` | `refs` | no | `refs` (`refs/agentflow/claims/<id>`) or `branch` (`refs/heads/agentflow/claims/<id>`) |
| `backlog.claim_stale_h` | `24` (1–720) | no | hours after which a claim not updated shows stale, whatever its status |
| `backlog.ignore_specs` | `[]` | no | globs under `project.docs.specs` that D6 doesn't list |

### Events

`item_claimed`, `claim_advanced`, `claim_released`, `claim_lost` (a stop kind, never resolved by autonomy: it is
added to `config.NEVER_RESOLVED`), `claim_refused` (the start that lost, or `claim_refused_by_host`) and
`backlog_updated` (a runner commit to the file, with the fields set). `activity.py` gets a sentence for each.

## Security

The change touches `guards.py`, `init.py` and `detect.py`, so the security overlay applies. Plan and review it with
`security-and-hardening` and the `security-auditor` agent.

- `backlog.yml` is repository input that any contributor can write. Ids and paths are validated before use.
  `title`, `notes` and a claim's `by` reach only the page's escaped text. A run's task text comes from the spec, as
  it does today, never from `title` or `notes`.
- A claim is data that anyone with push access can write or delete, and no host's protection rules cover either
  ref form. A forged or malformed claim can block a start, but it can't run anything. A deleted claim makes the
  claimant's next advance fail its lease, and the run stops with `claim_lost`. Only validated fields are shown,
  and a person frees a claim with `release`.
- Every push and ref deletion is leased, never forced, uses an explicit refspec, and runs no hook. A Board refresh
  never writes to `origin`: only the claimant's runs and a person's `release`/reopen do.
- Agents can't edit the file in a run (D3). The runner's own edits go through `safeio` and are committed with
  `paths=[<the file>]`. The dashboard's working-tree writes go through `safeio`, refuse a symlinked file, and are
  checked against the sha-256 the page read.

## Commands

```bash
uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_backlog_file.py --import-mode=prepend -q
uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_claims.py --import-mode=prepend -q
uv run --no-project --with pyyaml --with pytest pytest agentflow/tests --import-mode=prepend -q   # before finishing
uvx ruff@0.17.4 check .
bin/agentflow validate                     # "0 warnings" on this repository after the migration
bin/agentflow backlog import --dry-run     # this repository: the items it would write
bin/agentflow explain feature --module backlog-file
```

## Project structure

```
backlog.yml                         → this repository's backlog (slice B1's migration)
agentflow/claims.py                 → claim refs on origin (new)
agentflow/sources.py, work_source.py, backlog.py, module_queue.py, pipeline.py, flow_control.py, ci_lane.py,
  lanes.py, hitl.py, run_control.py, guards.py, config.py, config_edit.py, doctor.py, init.py, detect.py, flow.py,
  dashboard.py
agentflow/dashboard.html
agentflow/tests/test_backlog_file.py, test_claims.py (new); fixtures/packs/backlog/ (a third fixture pack)
docs/agentflow-playbook.md          → §13 setup, §14 keys, a new Backlog section
```

The golden `explain` files stay byte-identical: the `python` fixture pack keeps `capability_map`, and the new
behaviour is tested on a new `backlog` fixture pack.

## Testing strategy

TDD, with `fake_agent.py` and the fake `gh`. Claims are tested against a real bare repository as `origin` (a temp
`git init --bare`), so the lease is git's own and not a mock.

- **File:** parse every key, every `validate` error and warning, comments and order kept by `set`/`append`/`move`,
  a stale `expect_sha` refused, the size limits, and a malformed file that empties the Backlog without raising.
- **Claims:** two starts of one item, in two clones against one bare origin, started together (threads): exactly
  one run exists, and the other is refused with the claimant. Both `claim_ref` forms. Phase and status changes move
  the claim. A lost lease stops the run with `claim_lost`. Release on cancel, fail and archive, and after the merge
  only on the claimant's machine (a second clone's refresh pushes nothing). A stale `waiting` claim, `release`,
  reopen releasing a last-phase claim and leaving any other, a malformed `claim.json`, a bare origin whose
  `receive.hideRefs`/hook refuses `refs/agentflow/` (`claim_refused_by_host`), no `origin`, and `claims: false`.
- **State precedence (D5):** one table-driven test over every combination of file status, done evidence, local
  run, remote claim and its status, and dependencies.
- **Runner writes (D3):** a new item's first commit, a local-only item's first commit, `status: ready` for a
  `backlog` item, `spec:` after `define`, `plan:` after `plan`, `status: done` in `finish` before the push, and an
  agent's edit of the file refused as a `guard_violation`.
- **Dashboard (D7):** Add, Edit, Move up/down and reopen write the working tree only; a changed file is refused; an
  item naming a synced issue absorbs its card; the local-only Start note.
- **Not on the backlog (D6):** an unnamed spec listed, `ignore_specs`, and **Add**.
- **Import and deprecation (D8):** from `capability_map` (row order) and from `specs` (dependency order), with form
  cards and done marks, the status of each, `--dry-run` writing nothing, and the deprecation note not counting as
  a warning.
- **Queue (D9):** `ready` items start in order, and `backlog` and claimed items never start.
- **Plug and play:** `init` on the three toy repositories writes a `backlog` pack that `validate` passes.

## Boundaries

- **Always:** keep `capability_map` and `specs` working until 4.0, lease every push and delete, read run inputs at
  the fork commit, and validate every field from the file or a claim before it is used.
- **Ask first:** writing the file on the base branch directly, having the runner open PRs that only edit
  `backlog.yml`, syncing items back to GitHub issues, and removing the old kinds before 4.0.
- **Never:** store the live phase in `backlog.yml`, force-push or force-delete a claim, free a claim without a
  person, write to `origin` from a Board refresh, commit a dashboard edit, touch a developer's working tree from
  the runner, let an agent edit the file, or let item text reach an argv or a prompt.

## Rollout (each slice is one PR, feature lane on agentflow itself)

| Slice | What | Done when |
|---|---|---|
| **B1** | The file and the `backlog` kind: parse, `validate`, the Board's Backlog column from the file, D5 without claims, D6, D9, `backlog list/add/set/import`, hand marks as `set`, the config keys. Migrate this repository with `import`. | This repository's Board lists every spec in `docs/` and `docs/specs/` as an item or under Not on the backlog, and `validate` prints 0 warnings. |
| **B2** | The runner's writes (D3), the guard, and the dashboard's Add/Edit/Move/local-only Start (D7). | A run started from a new card ends with a PR whose diff sets the item `done`. |
| **B3** | Claims (D4): `claims.py`, the start refusal, phase and status advances, release and reopen, remote cards, `doctor`. Before the slice relies on custom refs, the developer pushes and deletes one `refs/agentflow/claims/probe` against this repository's GitHub `origin` by hand, and the plan records the result. | The two-clone race test passes, and a teammate's Board shows your run's phase and status within one refresh. |
| **B4** | `init`/`detect` write the `backlog` kind, the playbook's Backlog section, issue absorption and **Add to backlog** on synced issues, and the deprecation note. | `test_plug_and_play` passes on the three toy repositories. |

## Success criteria

1. With `work_source: {kind: backlog}`, every item in `backlog.yml` is on the Board exactly once: in Backlog,
   in a phase column, or under Done.
2. A run's phase and status are shown on a second clone's Board within one refresh (60 s) of the change, with the
   claimant's name.
3. Of two starts of one item from two clones, at the same moment, exactly one creates a run. The other is refused
   before any worktree or branch exists.
4. Merging a run's PR is enough to make `backlog.yml` on `origin/<fork_from>` say `status: done` for its item.
5. Every Markdown file under `project.docs.specs` is either named by an item, matched by `backlog.ignore_specs`, or
   listed under **Not on the backlog** and in a `validate` warning.
6. Existing `capability_map` and `specs` packs, and the golden `explain` files, are unchanged, apart from the
   deprecation note.
7. No agent in a run can change `backlog.yml`, and no Board refresh writes to `origin`.

## Alternatives considered

- **The phase in `backlog.yml`** was rejected in D1.
- **One file per item (`backlog/<id>.yml`)** never conflicts, even when two branches add items at once. One file
  was chosen because a team reads it top to bottom as a priority list. Two branches that both append at the end
  conflict textually, and that conflict is small and easy to resolve. Reconsider if it happens often.
- **GitHub Issues or Projects as the only store** would need `gh` and the network for every Board render. It
  would also put the backlog outside the repository's history and leave offline and non-GitHub teams with
  nothing. Issues stay an optional link (D7).
- **The claim as a commit to the base branch** (an `owner:` field) needs a push to the base branch, which the
  runner never does and branch protection usually refuses.
- **The claim as the run's branch** (`flow/<lane>/<slug>`, pushed at start) depends on the slug, so two starts of
  one item with different slugs both succeed. The claim needs a name derived from the item id alone.
- **Claims as branches by default** work on every host, but they show in the branch list, GitHub may offer a
  "Compare & pull request" banner for each, and an unfiltered `on: push` workflow runs CI on every phase change.
  They are kept as `backlog.claim_ref: branch`.
- **Any Board releasing a merged item's claim** would make a background refresh write to `origin`. A leftover
  claim on a done item is hidden instead, and reopening clears it.

## Decisions (grilling, 2026-11-24)

| # | Question | Decision |
|---|---|---|
| Q1 | Phase in the file? | No. The file holds the planning status, and the claim holds the live phase (D1). |
| Q2 | Where the file lives | `backlog.yml` at the repository root, movable with `work_source.path`. |
| Q3 | A done run's claim | Kept at the last phase until the PR merges (D4). |
| Q4 | Start on a `backlog` item | A second click, and the run's first commit sets `ready` (D5). |
| Q5 | This repository's shipped specs | `import`ed: open as `ready`, shipped as `done` (D8). |
| Q6 | The old kinds | Deprecated once B4 ships, removed in 4.0 (D8). |
| Q7 | Claim ref form | `refs/agentflow/claims/<id>` by default, `backlog.claim_ref: branch` as the fallback (D4). |
| Q8 | Deprecation in `validate` | A note, not a warning. |
| Q9 | `import` order | Open items first (row order, or dependency order then alphabetical), then `done` alphabetically. |
| Q10 | Who releases after the merge | Only the claimant's machine; other Boards hide the claim. |
| Q11 | Status on the claim | Yes: `running`, `waiting`, `paused`, `stopped`. |
| Q12 | `done_marks.json` | Replaced by `status` in the file for the `backlog` kind; `import` carries marks over. |
| Q13 | Item naming a synced issue | One card, the item; the synced card is hidden. |
| Q14 | `queue` | Starts `ready` items only, with no flag (D9). |
| Q15 | A claim's `by` | `git config user.name`, else `unknown`. |
| Q16 | A stale `waiting` claim | Shown stale like any other; only a person frees it. |
| Q17 | Reopening an item | Clears a leftover claim at the last phase, leaving any other. |
| Q18 | Dashboard editing | Add, Edit, Move up/down, all to the working tree and uncommitted; no drag reorder. |
| Q19 | Start on a local-only item | Works; the item goes into the run's first commit, with a note to discard the local copy before pulling. |

Not asked, because each has an obvious default: the dashboard refuses a write when the file changed since the page
read it, B3 begins with a manual custom-ref probe against this repository's `origin`, and `backlog.claim_ref` ships
with its four obligations.

## Open Questions

None. Every question was settled in the grilling above.
