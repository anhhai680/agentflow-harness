# Backlog file, slice B3 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** In a `backlog` pack, starting an item claims it on `origin`, every phase and status change moves the claim, a
second start of a claimed item is refused before any run, worktree or branch exists, and a teammate's Board shows the
claimant's phase and status within one refresh.

**Architecture:** `agentflow/claims.py` is the protocol: claim refs on `origin` as one-file commits, created and moved
with leased pushes (git's own compare-and-swap) and read with one `ls-remote` plus a fetch of what changed. It knows
nothing of runs. `agentflow/claim_sync.py` joins it to a run: the claim at start, `sync` at each phase and status
change, release at the end. `backlog.py`, `module_queue.py`, `views.py` and `board.js` read the claims the Board's
refresh caches. A claim is always written by the claimant's machine, and a Board refresh writes to `origin` only to
release the claims of its own merged runs.

**Tech Stack:** Python 3.12, stdlib + `pyyaml`, pytest with a real bare repository as `origin`, plain JS
(`agentflow/dashboard_ui/board.js`).

**Spec:** `docs/specs/agentflow-backlog-file-proposal.md` (approved 2026-11-24), slice **B3**: D4, the claim parts of
D5, D9, and the Events and Security sections. Read those, and the B2 plan (`docs/plans/2026-11-25-agentflow-harness-backlog-b2.md`) for
`backlog_writes.py`, which already decides which runs belong to an item.

### What B3 does not do

- **B4:** `init`/`detect`, the playbook's Backlog section, issue absorption, **Add to backlog**, the deprecation note.
- No heartbeat: a claim's `updated` moves on a phase or status change only (D4), so a phase longer than
  `backlog.claim_stale_h` shows *stale* on other Boards.

### Plan decisions

The spec leaves these open.

- **Two modules.** `claims.py` (the protocol: `Claim`, `create`, `advance`, `release`, `read`, `list_remote`) imports no
  run code. `claim_sync.py` (the run's side) imports `claims`, `state`, `hitl`.
- **The claim comes before the run.** `claim.json` carries the run id, so `pipeline.start` reserves it first
  (`Run.next_id`, then `Run.create(..., run_id=...)`). A refused start therefore leaves no run directory, worktree or
  branch, which is what "exactly one run exists" in the race test needs. Any failure after the claim (slots full, the
  run's creation, `_set_up`) releases it.
- **Which starts claim.** Those `backlog_writes.applies` selects (a `module` param, in the item's lane or `module_lane`),
  in a `backlog` pack with `backlog.claims` on and an `origin` remote. Any other start claims nothing. A start whose
  `origin` can't be reached refuses, naming `backlog.claims: false`.
- **Run status to claim status:** `running`, `refining` → `running`; `waiting_human`, `awaiting_approval`, `ready` →
  `waiting`; `paused` → `paused`; `error` → `stopped`; `done` → `waiting` (the PR waits for a merge). `failed` and
  `cancelled` release instead of advancing.
- **A network failure is not a lost claim.** A push that fails because `origin` can't be reached records
  `claim_sync_failed` and the run goes on; the next `sync` tries again. Only a failed lease (the ref is gone, or holds
  another run's claim) is `claim_lost`. A lease that fails because the ref holds a newer commit of the same run (two
  processes of one run) re-reads the ref and retries once.
- **`claim_lost` is a stop** (`NEVER_RESOLVED`). Approving it makes the next `sync` claim again (the ref must not
  exist): it works when the item is free, and stops again naming the new claimant when it isn't.
- **`claim_refused` has no run to live in.** The refused start's `ValueError` text is the refusal: the CLI prints it and
  the Board's `item_failed` records it. Task 11 adds that one sentence to the spec's Events list.
- **Release after a merge** runs where D4 lists: `ci_lane`'s watch, the dashboard's refresh of its own runs, and
  `archive`. All three call `claim_sync.release_merged`, which looks only at the local runs whose own `state["claim"]`
  is unreleased, so a clone with no such run pushes nothing.
- **Remote claims are cached.** `backlog.refresh` runs `claims.list_remote` and saves the result in the Board's state
  file (`claims`, `claims_note`). `snapshot` is pure and reads the cache. `agentflow backlog list` and
  `agentflow queue` read `origin` live, and `queue` starts nothing when it can't.
- **Reopen** releases through `backlog.release_on_reopen(cfg, repo, item_id, was)`, which `set_item`, `set_status`
  and the dashboard's Edit call after a `done` item becomes `ready` or `backlog`. It never raises.

## Global Constraints

- Engine dependencies stay stdlib + `pyyaml>=6`. No project strings in the engine (`test_portability.DENYLIST`).
- The golden `explain` files stay byte-identical. Nothing here touches a `capability_map` or `specs` pack.
- Claim ref: `refs/agentflow/claims/<id>`, or `refs/heads/agentflow/claims/<id>` with `backlog.claim_ref: branch`. Its
  commit holds one file, `claim.json`, with the keys `item`, `run`, `lane`, `phase`, `status`, `by`, `started`,
  `updated`. `status` is `running`, `waiting`, `paused` or `stopped`. `by` is `git config user.name`, else `unknown`,
  one line of at most 100 characters.
- Every push and ref deletion is leased (`--force-with-lease=<ref>:<expected>`, empty expected = must not exist), never
  forced, uses an explicit refspec, `--no-verify` and `gitops.HARDENED`, and runs no hook. Nothing frees a claim
  automatically except the claimant's own machine (end of its run, or its merged run). A person frees any other with
  `agentflow backlog release <id>`.
- Ids go through `sources.MODULE_ID` before they reach a ref name or an argv. `parse_claim` validates every field: a
  phase of the pack, a lane of the pack, a status of the four, ISO-8601 times, `by`. Anything else is an *unreadable*
  claim, which still blocks a start. `claim.json` is read with a 4096-byte limit.
- `backlog.claims` (bool, default `true`), `backlog.claim_ref` (`refs` | `branch`, default `refs`),
  `backlog.claim_stale_h` (int 1–720, default `24`). Each ships with the four obligations in `CLAUDE.md`.
- `uvx ruff@0.17.4 check .` before every commit. Tests run with
  `uv run --no-project --with pyyaml --with pytest pytest <file> --import-mode=prepend -q`.
- Commit messages are one imperative sentence ending with the `Co-Authored-By` trailer from `project.commit_trailer`.
  The claim commits on `origin` are the runner's own and carry no trailer.

## Review Focus

1. **A teammate deletes or overwrites your claim while your run is mid-phase.** The next advance stops the run with
   `claim_lost` and names the new claimant (or says the claim is gone). It never pushes over it. Approving after the
   other claim is released claims again (Task 6).
2. **`origin` is unreachable.** At start the run is refused and nothing exists (`could not reach origin … backlog.claims:
   false`). In the middle of a run the run goes on, records `claim_sync_failed`, and the next change catches the claim up
   (Tasks 5, 6).
3. **A claim whose `claim.json` is forged or malformed** (a phase the pack lacks, a `by` of 5,000 characters or with a
   newline, a binary blob, no file at all). It blocks a start as *unreadable*, shows as text on the Board, and runs
   nothing (Tasks 3, 10).
4. **A claim left on an item that is `done` on `origin`.** Other Boards hide it. Only the claimant's machine releases
   it, and a clone that has no run of that item pushes nothing on its refresh (Tasks 7, 9).
5. **The start fails after the claim was made** (slots full, a bad branch name, the run directory taken). No claim is
   left on `origin` (Task 5).

---

### Task 1: The custom-ref probe against this repository's `origin` (by hand)

The developer runs this once, and the plan records the result. No agent in a run can do it: it pushes to GitHub.

**Files:**
- Modify: this plan (the **Probe result** block below)

- [ ] **Step 1: Create, read, re-create, fetch and delete the probe ref.** From this repository's checkout:

```bash
BLOB=$(printf '{"probe": true}\n' | git hash-object -w --stdin)
TREE=$(printf '100644 blob %s\tclaim.json\n' "$BLOB" | git mktree)
SHA=$(GIT_AUTHOR_NAME=probe GIT_AUTHOR_EMAIL=probe@localhost GIT_COMMITTER_NAME=probe GIT_COMMITTER_EMAIL=probe@localhost \
      git commit-tree "$TREE" -m "claim probe")
REF=refs/agentflow/claims/probe
git push --force-with-lease=$REF: --no-verify origin "$SHA:$REF"            # 1: creates
git ls-remote origin 'refs/agentflow/claims/*'                              # 2: lists $SHA
git push --force-with-lease=$REF: --no-verify origin "$SHA:$REF"            # 3: must be refused (stale info)
git fetch --no-tags origin "+$REF:refs/agentflow/remote-claims/probe"       # 4: fetches the commit
git push --force-with-lease=$REF:$SHA --no-verify origin ":$REF"            # 5: leased delete
git ls-remote origin 'refs/agentflow/claims/*'                              # 6: lists nothing
git update-ref -d refs/agentflow/remote-claims/probe
```

  Then look at GitHub: the branch list and Actions tab for the probe's minutes, and whether any "Compare & pull request"
  banner appeared.

- [ ] **Step 2: Fill in the block below and commit.** If steps 1 or 5 are refused, repeat with
  `REF=refs/heads/agentflow/claims/probe`, and set `backlog: {claim_ref: branch}` in this repository's
  `.agentflow/config.yaml` yourself (agents can't edit it) before Task 5 runs here.

**Probe result** (pending: the developer fills this in)

| # | Command | Result |
|---|---|---|
| 1 | create | |
| 2 | `ls-remote` lists it | |
| 3 | create again | |
| 4 | `fetch` | |
| 5 | leased delete | |
| Actions run, branch list, PR banner | | |

- [ ] **Step 3: Commit**: `git commit -m "Record the custom claim ref probe against origin."`

### Task 2: The `backlog.claims*` config keys

**Files:**
- Modify: `agentflow/config.py` (`BACKLOG_KEYS`-style known keys for the `backlog:` block, `BacklogPolicy`,
  `_backlog_policy`, `Validator.backlog_policy`, the `known keys` message), `agentflow/config_edit.py` (Settings
  fields), `agentflow/defaults/config.yaml`, `docs/agentflow-playbook.md` §14
- Test: `agentflow/tests/test_config.py`, `agentflow/tests/test_config_edit.py`

**Interfaces:**
- Produces: `BacklogPolicy(ignore_specs: tuple[str, ...] = (), claims: bool = True, claim_ref: Literal["refs",
  "branch"] = "refs", claim_stale_h: int = 24)`, read by `cfg.backlog`.

- [ ] **Step 1: Write the failing tests**
  - `test_backlog_claim_keys_default_and_load`: no `backlog:` block gives `(True, "refs", 24)`; a block with
    `claims: false, claim_ref: branch, claim_stale_h: 48` loads them.
  - `test_validate_refuses_bad_claim_keys` (parametrised): `claims: "yes"`, `claim_ref: tag`, `claim_stale_h: 0`,
    `721`, `true`, `"24"` are each an error at `backlog.<key>`; `claim_ref: branch` with `claims: false` is no error.
  - `test_the_settings_form_saves_the_claim_keys` (`test_config_edit.py`): a bool, an enum with options
    `refs`/`branch`, and an int 1–720 field each round-trip through the form, as `backlog.ignore_specs` does.

- [ ] **Step 2: Run them and see them fail.** Expected: FAIL (`unknown key; known: ignore_specs`).

- [ ] **Step 3: Implement.** The three keys join `ignore_specs` everywhere it appears. The defaults file gets the three keys
  with their defaults beside `ignore_specs`. Playbook §14 gets one line per key, in the style of `backlog.ignore_specs`.

- [ ] **Step 4: Run the tests and see them pass.** Also `test_config.py`, `test_config_edit.py`, `test_pack.py`.

- [ ] **Step 5: Commit**: `git commit -m "Add the backlog claim settings."`

### Task 3: `claims.py`, the protocol against a real bare origin

**Files:**
- Create: `agentflow/claims.py`
- Test: `agentflow/tests/test_claims.py` (new)

**Interfaces:**
- Consumes: `sources.MODULE_ID`, `gitops.HARDENED`, `gitops.read_blob`, `cfg.backlog` (Task 2), `cfg.phases`, `cfg.lanes`.
- Produces (all in `claims.py`):
  - `@dataclass(frozen=True) class Claim: item, run, lane, phase, status, by, started, updated: str; sha: str = "";
    unreadable: str = ""` (`unreadable` says why `claim.json` could not be read; `""` when it could)
  - `class ClaimError(Exception)` with subclasses `Claimed(claim: Claim)`, `HostRefused`, `Unreachable`,
    `Lost(claim: Claim | None)` (the ref's current holder, or None when it is gone)
  - `enabled(cfg: Config, repo: Path) -> bool`: kind `backlog`, `backlog.claims`, and an `origin` remote.
  - `ref(cfg: Config, item_id: str) -> str`: the ref name for `backlog.claim_ref`; raises `ValueError` for an id that is
    not a module id.
  - `committer(repo: Path) -> str`: `git config user.name`, else `unknown`, one line, cut to 100.
  - `parse_claim(cfg: Config, item_id: str, text: str, sha: str) -> Claim`: never raises; the item in the text must
    equal `item_id`.
  - `create(cfg, repo, item_id: str, *, run: str, lane: str, phase: str, status: str = "running") -> Claim`:
    raises `Claimed`, `HostRefused` or `Unreachable`.
  - `advance(cfg, repo, held: Claim, *, phase: str, status: str) -> Claim`: raises `Lost` or `Unreachable`.
  - `release(cfg, repo, held: Claim) -> None`: a ref already gone is success; raises `Lost`, `Unreachable`.
  - `read(cfg, repo, item_id: str) -> Claim | None` and
    `list_remote(cfg, repo, seen: Mapping[str, Claim] | None = None) -> dict[str, Claim]`: one `ls-remote`, then one
    `fetch` into `refs/agentflow/remote-claims/<id>` of only the refs whose sha differs from `seen`; both raise
    `Unreachable`.
  - `is_stale(held: Claim, hours: int, now: datetime) -> bool`, `age_s(held: Claim, now: datetime) -> float | None`.

- [ ] **Step 1: Write the failing tests** (the fixtures: `flow_repo` as a `backlog` pack; `second_clone(flow_repo,
  tmp_path)` clones `flow_repo.origin` with its own `user.name`; both are helpers in this file):
  - `test_create_makes_the_ref_and_a_second_create_is_claimed`: after `create`, `git ls-remote` on the origin shows
    `refs/agentflow/claims/b`; the commit holds exactly `claim.json`, whose parsed fields equal the arguments; the
    second clone's `create` raises `Claimed` with `.claim.by == "Test"` and `.claim.phase == "define"`.
  - `test_thirty_simultaneous_races_have_one_winner`: for 30 item ids, two threads (one per clone, released by a
    `threading.Barrier`) call `create`; each id has exactly one return and one `Claimed`.
  - `test_advance_moves_the_ref_forward_and_a_moved_ref_is_lost`: `advance(phase="build", status="waiting")` makes the
    ref's commit a child of the old one with the new fields and the same `started`; if another clone deleted the ref
    (`release` by its holder, or `git push origin :ref`), `advance` raises `Lost(None)`; if it was replaced by another
    run's claim, `Lost` carries that claim. Nothing is pushed over either.
  - `test_advance_over_a_newer_commit_of_the_same_run_retries_once`: after the claimant's second process advanced,
    the first process's stale `advance` succeeds on top of it.
  - `test_release_deletes_with_a_lease`: after `release` the ref is gone; a second `release` is a no-op; a ref moved to
    another run's claim raises `Lost` and stays.
  - `test_both_ref_forms`: with `claim_ref: branch` the ref is `refs/heads/agentflow/claims/b` and everything above
    holds.
  - `test_a_host_that_hides_the_namespace_is_refused`: `git config receive.hideRefs refs/agentflow/` on the bare origin
    makes `create` raise `HostRefused`, whose message names `backlog.claim_ref: branch`; no ref exists.
  - `test_no_origin_or_claims_off_is_not_enabled`, and `test_an_unreachable_origin_is_unreachable` (the origin's
    path renamed): `create` and `list_remote` raise `Unreachable`.
  - `test_list_remote_fetches_only_what_changed`: two refs, `list_remote` reads both; after one `advance`, a second call
    with `seen=` fetches one ref (patch `claims._run` and assert the refspecs of the one `fetch` it sees).
  - `test_parse_claim_never_trusts_the_text` (parametrised, each an unreadable claim that still has its `sha`): bad
    JSON, a non-object, a missing key, an `item` that is not the ref's id, a phase the pack lacks, a lane the pack
    lacks, status `done`, `by` with a newline, `by` of 101 characters, `updated: "yesterday"`, `claim.json` over
    4096 bytes, and a commit with no `claim.json`.
  - `test_is_stale_counts_from_updated_whatever_the_status`: `waiting` and `running` claims 25 h old are stale at 24, a
    claim 23 h old is not.

- [ ] **Step 2: Run them and see them fail.** Expected: FAIL (`ModuleNotFoundError: agentflow.claims`).

- [ ] **Step 3: Implement.** The commit is built without a work tree: `hash-object -w`, `mktree`, `commit-tree` with
  `GIT_AUTHOR_*`/`GIT_COMMITTER_*` set from `by` and `agentflow@localhost`, `-c commit.gpgsign=false`. `create` pushes
  `--force-with-lease=<ref>:` and, on failure, classifies by one `ls-remote <ref>`: present means `Claimed` (read it),
  absent means `HostRefused` (git's message in the text), `ls-remote` failing means `Unreachable`. `advance` and
  `release` push or delete with `--force-with-lease=<ref>:<held.sha>`; on failure they re-read the ref, retry once when
  it holds a commit of the same `run`, and otherwise raise `Lost`. Set `GIT_TERMINAL_PROMPT=0` and a 60 s timeout on
  every call. One private `_run(repo, *args, input=None, env=None) -> subprocess.CompletedProcess` carries
  `gitops.HARDENED`.

- [ ] **Step 4: Run the tests and see them pass.** Also `test_portability.py`.

- [ ] **Step 5: Commit**: `git commit -m "Add claim refs on origin, created and moved with leased pushes."`

### Task 4: Claim events, the run's claim state, and the `claim_lost` stop

**Files:**
- Modify: `agentflow/state.py` (reducer, required fields, `initial_state`), `agentflow/activity.py`,
  `agentflow/config.py` (`NEVER_RESOLVED`), `agentflow/hitl.py` (`_how_to_go_on`)
- Test: `agentflow/tests/test_state.py`, `agentflow/tests/test_activity.py`, `agentflow/tests/test_hitl.py`

**Interfaces:**
- Produces events: `item_claimed` (`item`, `ref`, `sha`, `phase`, `status`), `claim_advanced` (`item`, `sha`, `phase`,
  `status`), `claim_released` (`item`, `reason`), `claim_lost` (`item`, `why`), `claim_sync_failed` (`item`, `action`,
  `error`), `claim_release_failed` (`item`, `error`).
- Produces `state["claim"]`: `None`, or `{"item", "ref", "sha", "phase", "status", "released": bool, "lost": bool}`.
  `item_claimed` replaces it whole (and clears `lost`), `claim_advanced` updates `sha`, `phase`, `status`,
  `claim_released` sets `released`, `claim_lost` sets `lost`.

- [ ] **Step 1: Write the failing tests**
  - `test_claim_events_fold_into_state_claim`: each event above in order gives the dict above; `claim_sync_failed` and
    `claim_release_failed` change nothing; a second `item_claimed` after `claim_lost` clears `lost`.
  - `test_claim_events_need_their_fields`: an `item_claimed` with no `sha` is refused by the reducer as the other
    required-field events are.
  - `test_activity_has_a_sentence_for_each_claim_event`: each renders text naming the item and, for `claim_advanced`,
    the phase and status.
  - `test_claim_lost_is_never_resolved_by_autonomy`: `claim_lost` is in `NEVER_RESOLVED`, and `hitl._how_to_go_on` for it
    names `agentflow approve <run> <key>` (claims it again), `agentflow backlog release <id>` and `agentflow cancel`.

- [ ] **Step 2: Run them and see them fail.** Expected: FAIL.

- [ ] **Step 3: Implement** the reducer, the required-field table entries, the sentences, the `NEVER_RESOLVED` entry and
  the way-on text.

- [ ] **Step 4: Run the tests and see them pass.** Also `test_autonomy.py`.

- [ ] **Step 5: Commit**: `git commit -m "Record claim events in the run's state and stop a run whose claim was lost."`

### Task 5: Start claims the item, and a second start is refused first

**Files:**
- Create: `agentflow/claim_sync.py` (`target`, `start`, `record`)
- Modify: `agentflow/state.py` (`Run.next_id`, `Run.create(..., run_id: str | None = None)`), `agentflow/pipeline.py`
  (`start`)
- Test: `agentflow/tests/test_claim_runs.py` (new; `ClaimFlow` is `BacklogFlow` with `backlog: {claims: true}`)

**Interfaces:**
- Consumes: `claims.create`, `claims.enabled`, `claims.ref`, `backlog_writes.plan_start`/`applies`, `lane_first_phase`.
- Produces:
  - `Run.next_id(cfg, repo, pipeline, params) -> str`: the id `create` would pick. `Run.create` with `run_id` makes
    exactly that directory, or raises `FileExistsError`.
  - `claim_sync.target(cfg, repo, pipeline, params, new_module: dict | None) -> str | None`: the item id this start
    claims, or None (no claim for this start).
  - `claim_sync.start(cfg, repo, pipeline, params, opts: RunOptions, new_module: dict | None) -> tuple[str,
    claims.Claim] | None`: the run id and its claim, or None. Raises `ValueError` with the texts below.
  - `claim_sync.record(run: Run, held: claims.Claim) -> None`: emits `item_claimed`.
  - Refusal texts: `module <id> is claimed by <by>: run <run>, phase <phase>, <status>, updated <updated>
    (agentflow backlog release <id> frees it)`; for an unreadable claim `module <id> has a claim on origin that
    agentflow can't read (<why>): agentflow backlog release <id> frees it`; for `HostRefused`
    `origin refused the claim ref <ref>: set backlog.claim_ref: branch in config.yaml, or backlog.claims: false to
    start without claims`; for `Unreachable` `could not reach origin to claim <id>: <error>; start again when it is
    reachable, or set backlog.claims: false to start without claims`.

- [ ] **Step 1: Write the failing tests**
  - `test_a_start_claims_before_any_worktree`: `start(... module="b")` leaves `claims.read(cfg, root, "b")` with
    `run == run.id`, `phase == "define"`, `status == "running"`, `by == "Test"`; `run.state["claim"]["sha"]` is the
    ref's commit; the event order is `run_started`, `item_claimed`, then `run_updated` with the worktree.
  - `test_two_starts_from_two_clones_make_one_run`: with a `Barrier`, both clones `start` module `b`. Exactly one
    `Run` exists across both state dirs, exactly one worktree and branch exist, and the loser's `ValueError` contains
    `claimed by` and the winner's `run.id`.
  - `test_refusals_leave_no_run_and_no_claim` (parametrised: a foreign claim, an unreadable one, a hidden namespace, an
    unreachable origin): the text above, no run directory, no branch, no worktree.
  - `test_a_failure_after_the_claim_releases_it`: with `slots.count: 1` held by another run, then separately with
    `_set_up` made to raise, `claims.read` is None afterwards and (for `_set_up`) the run is `failed`.
  - `test_starts_that_claim_nothing`: `claims: false`; no `origin`; a `bug` run of a module-less task; a pack whose
    kind is `capability_map`. Each starts, with `state["claim"] is None` and no ref.
  - `test_a_new_item_is_claimed_by_its_id`: `--new-module` `n` claims `n` while the item exists only in the run's first
    commit.

- [ ] **Step 2: Run them and see them fail.** Expected: FAIL.

- [ ] **Step 3: Implement.** `pipeline.start` calls `claim_sync.start` after `gather(...)` and before `slots.starting`,
  passes the reserved id to `Run.create`, emits `item_claimed` right after it, and wraps everything after the claim so
  an exception releases it (`claims.release`, errors recorded as `claim_release_failed` when a run exists).

- [ ] **Step 4: Run the tests and see them pass.** Also `test_backlog_runs.py` (its `BacklogFlow` now sets
  `backlog: {claims: false}`), `test_pipeline.py`, `test_slots.py`.

- [ ] **Step 5: Commit**: `git commit -m "Claim an item on origin when its run starts, and refuse a second start of it."`

### Task 6: Phase and status changes move the claim; the end of a run releases it

**Files:**
- Modify: `agentflow/claim_sync.py` (`sync`, `release_run`), `agentflow/pipeline.py` (`_Loop.go`, `_Loop.phase`,
  `advance`'s `finally`), `agentflow/run_control.py` (`pause`, `resume`, `cancel`, `archive`)
- Test: `agentflow/tests/test_claim_runs.py`

**Interfaces:**
- Consumes: `claims.advance`, `claims.release`, `claims.create` (the re-claim), the events of Task 4.
- Produces:
  - `claim_sync.sync(cfg: Config, run: Run) -> bool`: moves the claim to the run's `(phase, status)` (the mapping in
    the plan decisions) when it differs from `run.state["claim"]`; releases when the run is `failed` or `cancelled`. A
    run with no claim, or a released one, is a no-op returning True. When `state["claim"]["lost"]` it tries
    `claims.create` first. Returns False when the run must stop, having raised the `claim_lost` stop (summary names
    the item and the new claimant, or says the claim is gone). A network failure emits `claim_sync_failed` and
    returns True.
  - `claim_sync.release_run(cfg, run, reason: str) -> None`: a leased delete and `claim_released`; never raises; a
    failure is `claim_release_failed`.

- [ ] **Step 1: Write the failing tests**
  - `test_the_claim_follows_the_run`: scripted `define` that ends in the phase approval: `claims.read` says `define`,
    `waiting`; after `approve` and the next `advance` it says `plan`, `running`/`waiting`; the events are
    `claim_advanced` in order, one per change (a second `sync` with no change pushes nothing: count pushes by patching
    `claims._run`).
  - `test_pause_and_resume_move_the_claim`: `run_control.pause` on a waiting run gives `paused`, `resume` gives
    `running` once the runner starts; an `error` status gives `stopped`.
  - `test_cancel_fail_and_archive_release`: after `cancel`, after a `run_failed`, and after `archive`, `claims.read` is
    None and the run has `claim_released` with `reason` `cancelled`/`failed`/`archived`.
  - `test_a_done_run_keeps_its_claim_at_the_last_phase`: a run through `ship` has `phase == <last phase>`, `waiting`, and
    the ref still exists.
  - `test_a_deleted_claim_stops_the_run` (Review Focus 1): another clone deletes the ref mid-run; the next change
    ends in `waiting["kind"] == "claim_lost"` and `claim_lost` is in the log; nothing was pushed over it. Then with the
    ref free, `approve` and `advance` re-claim (`item_claimed`, `lost` cleared) and the run goes on.
  - `test_a_claim_taken_by_another_run_stops_again`: the same, but another clone claimed `b` meanwhile: after `approve`
    the run stops with `claim_lost` naming that claimant.
  - `test_origin_going_away_mid_run_does_not_stop_it`: the origin renamed during a phase: `claim_sync_failed` is
    recorded, the run's status is unchanged, and once the origin is back the next `sync` pushes the missed change.

- [ ] **Step 2: Run them and see them fail.** Expected: FAIL.

- [ ] **Step 3: Implement.** `sync` runs at `_Loop.go`'s start, at each `phase_started`, in `advance`'s `finally` (before
  the slot and issue-label release), and after the events `pause`, `resume`, `cancel`, `archive` emit. `go` and `phase`
  `return self.status()` when `sync` is False.

- [ ] **Step 4: Run the tests and see them pass.** Also `test_run_control.py`, `test_pipeline.py`, `test_hitl.py`.

- [ ] **Step 5: Commit**: `git commit -m "Move a run's claim with its phase and status, and release it when the run ends."`

### Task 7: A merged item's claim is released by its claimant's machine

**Files:**
- Modify: `agentflow/claim_sync.py` (`release_merged`), `agentflow/ci_lane.py` (the watch), `agentflow/backlog.py`
  (`refresh`), `agentflow/run_control.py` (`archive`, which already releases through Task 6)
- Test: `agentflow/tests/test_claim_runs.py`

**Interfaces:**
- Produces: `claim_sync.release_merged(cfg: Config, repo: Path) -> list[str]`: for each local run whose
  `state["claim"]` is unreleased and not lost, whose status is `done`, and whose item is `status: done` in the backlog
  file at `origin/<fork_from>` (`work_source.items_at`), it releases the claim (`reason: "merged"`). Returns the item
  ids. It reads no other run and never raises.

- [ ] **Step 1: Write the failing tests**
  - `test_the_claimants_refresh_releases_a_merged_item`: a `done` run; `land_on_origin` merges a `backlog.yml` with `b`
    done; `backlog.refresh` releases the claim and the run has `claim_released` (`merged`).
  - `test_a_second_clone_pushes_nothing` (Review Focus 4): the same merge, `backlog.refresh` on a clone with no run of
    `b` leaves the ref in place and runs no `git push` (patch `claims._run` and assert no push or delete argv).
  - `test_an_unmerged_item_keeps_its_claim`, and `test_ci_watch_releases_once_the_merge_lands` (the watch's loop, with
    the fake `gh` reporting merged and the file on `origin/<fork_from>`).

- [ ] **Step 2: Run them and see them fail.** Expected: FAIL.

- [ ] **Step 3: Implement** and call it from `backlog.refresh` (after `fetch_remote_branch`), from `ci_lane`'s watch, and
  from `archive`.

- [ ] **Step 4: Run the tests and see them pass.** Also `test_ci_lane.py`, `test_backlog.py`.

- [ ] **Step 5: Commit**: `git commit -m "Release a merged item's claim from the machine that ran it."`

### Task 8: A person frees a claim; reopening an item clears a finished one

**Files:**
- Modify: `agentflow/claims.py` (`release_item`), `agentflow/backlog.py` (`release_item`, `release_on_reopen`,
  `set_item`, `set_status`, `edit_item`), `agentflow/flow.py` (`backlog release <id>`), `agentflow/dashboard.py`
  (`/api/backlog/file` edit returns `notes`)
- Test: `agentflow/tests/test_claims.py`, `agentflow/tests/test_backlog_cli.py`, `agentflow/tests/test_dashboard.py`

**Interfaces:**
- Produces:
  - `claims.release_item(cfg, repo, item_id: str) -> Claim | None`: reads the ref and deletes it with a lease on what
    it read; returns what it freed, or None when there was nothing. Works on an unreadable claim.
  - `backlog.release_item(cfg, repo, item_id: str) -> list[str]`: the lines the CLI prints, naming the claimant and,
    when a local run holds it, that the run's next change stops with `claim_lost`.
  - `backlog.release_on_reopen(cfg, repo, item_id: str, was: str) -> list[str]`: with `was == "done"`, reads the item's
    claim; when its phase is the pack's last phase and the file at `origin/<fork_from>` has the item `done`, it
    releases it; any other claim is left, and the line says so (`left claim of <by> (<phase>, <status>)`). Never
    raises: an unreachable origin is a line.

- [ ] **Step 1: Write the failing tests**
  - `test_release_frees_any_claim_including_a_stale_one`: `agentflow backlog release b` frees a `waiting` claim 3 days old
    and an unreadable one, prints the claimant, and a second run prints `no claim on b`.
  - `test_release_names_a_live_local_run`: the line says run `<id>` will stop with `claim_lost`.
  - `test_reopen_releases_only_a_finished_claim`: `set b status=ready` on a done item whose claim is at the last phase
    and done on origin releases it; with the claim at `build`, it stays and the output says so; an item that was not
    `done` never calls origin. The dashboard's Edit returns the same lines as `notes`.
  - `test_reopen_with_origin_down_still_edits_the_file`: the file is changed, the line says the claim could not be read.

- [ ] **Step 2: Run them and see them fail.** Expected: FAIL.

- [ ] **Step 3: Implement.**

- [ ] **Step 4: Run the tests and see them pass.** Also `test_backlog_file.py`.

- [ ] **Step 5: Commit**: `git commit -m "Let a person free a claim, and clear a finished one when its item is reopened."`

### Task 9: Remote claims reach the Board, `backlog list` and `queue` (D5, D9)

**Files:**
- Modify: `agentflow/backlog.py` (`refresh`, `_picture`, `snapshot`, `begin_start`, `backlog_view`),
  `agentflow/views.py` (`board`), `agentflow/module_queue.py` (`assess_modules`, `queue_tick`, `Assessment`),
  `agentflow/flow.py` (`backlog list` columns)
- Test: `agentflow/tests/test_backlog.py`, `agentflow/tests/test_module_queue.py`, `agentflow/tests/test_backlog_cli.py`

**Interfaces:**
- Consumes: `claims.list_remote`, `claims.is_stale`, `claims.age_s`, `claims.Claim`.
- Produces:
  - Board state (`_save_state`): `claims: {id: asdict(Claim)}` and `claims_note: str` (`""`, `claims are off: <why>`, or
    `could not read claims on origin: <error>`, in which case the old `claims` stay).
  - `snapshot(...)["remote_claims"]`: one dict per claim whose item has no local run, is not `done`, and is not
    `dropped`: `{key: "claim:<id>", id, title (the file's title, else the id), lane, column (the claim's phase, or
    "Backlog" when unreadable), phase, status, by, run, updated, age_s, stale, unreadable}`. Such an item is left out of
    `cards`. `snapshot["claims_note"]` is the note. `views.board` passes both through, `remote_claims` filtered by lane.
  - `assess_modules(...)`: reads `claims.list_remote` once when `claims.enabled`; a node with no local run and a claim
    gets `entry["claim"]` and a `problems` text `claimed by <by> (<phase>, <status>)`, so `queue_tick` skips it and its
    dependents wait. A failed read sets `Assessment.claims_error`, and `queue_tick` starts nothing, saying why.

- [ ] **Step 1: Write the failing tests**
  - `test_a_teammates_board_shows_my_runs_phase_and_status` (success criterion 2): clone A starts and advances a run
    to `define`/`waiting`; clone B's `backlog.refresh` then `board(...)["remote_claims"]` has one entry with
    `column == "define"`, `status == "waiting"`, `by == "Test"`, `run == <A's run id>`, and B's `cards` no longer list
    item `b`.
  - `test_d5_precedence_table` (table-driven over the file status, done evidence, a local run, a remote claim of each
    status, and unmet dependencies): `dropped` hides it, `done` hides a claim, a local run beats a claim, a claim beats
    `blocked`, `blocked` beats `ready`/`backlog`. The same table drives `backlog_view` (`agentflow backlog list`).
  - `test_a_claim_for_an_item_this_file_lacks_is_still_shown`: a teammate's new item shows by id.
  - `test_stale_and_unreadable_claims`: a claim updated 25 h ago has `stale: true` and `age_s` near 90000 (the JS
    words it, Task 10); a forged claim is `unreadable` in the `Backlog` column with the reason.
  - `test_begin_start_refuses_a_cached_claim`: `Refused` with the claimant, before any `item_starting` record.
  - `test_refresh_keeps_old_claims_when_origin_is_unreachable`: `claims_note` says so; `snapshot` still shows the old ones.
  - `test_queue_skips_a_claimed_item_and_starts_ready_ones` (D9): with `a` claimed by a clone and `b`, `c` ready, one
    tick starts `b` and `c`; an item whose status is `backlog` is never started; with origin unreachable it starts
    nothing and says why.
  - `test_snapshot_reads_no_network`: `snapshot` with claims cached never calls `claims._run` (patch it to fail).

- [ ] **Step 2: Run them and see them fail.** Expected: FAIL.

- [ ] **Step 3: Implement.** `refresh` stores the claims after the release step of Task 7, never pushing otherwise.

- [ ] **Step 4: Run the tests and see them pass.** Also `test_views.py`, `test_dashboard.py`.

- [ ] **Step 5: Commit**: `git commit -m "Show other machines' claims on the Board, in backlog list and to the queue."`

### Task 10: The Board draws a claim as a read-only card in its phase column

**Files:**
- Modify: `agentflow/dashboard_ui/board.js` (`remoteClaimNode`, `columnEntries`), `agentflow/dashboard.html` (a
  `claims-note` line beside `backlog-note`, and the card's style)
- Test: `agentflow/tests/test_dashboard_js.py` (the node harness `conftest.py` provides)

**Interfaces:**
- Consumes: `data.remote_claims`, `data.claims_note` (Task 9).
- Produces: `remoteClaimNode(claim) -> HTMLElement`: a card with the id, the claimant's name, `phase · status`, the
  age (`3 d`) and a `stale` badge, no Start, Edit or drag. Every field is set as text.

- [ ] **Step 1: Write the failing tests**
  - `test_a_remote_claim_card_is_text_only`: `by: "<img src=x onerror=1>"` and a run id with markup render as text
    nodes; the card has no button and `draggable` is absent.
  - `test_remote_cards_sit_in_their_phase_column`: `columnEntries(data, "build", [])` includes the card for a claim
    with `column: "build"`, an unreadable claim sits in `Backlog`, and a card for an unknown column is not dropped.
  - `test_the_card_says_stale_and_the_note_shows`: `stale: true` adds the badge text `stale`; `claims_note` is shown.

- [ ] **Step 2: Run them and see them fail.** Expected: FAIL.

- [ ] **Step 3: Implement** with the existing `el`, `entry` and `patchChildren` helpers.

- [ ] **Step 4: Run the tests and see them pass.** Also `test_dashboard_js.py` as a whole.

- [ ] **Step 5: Commit**: `git commit -m "Draw other machines' claims as read-only cards in their phase column."`

### Task 11: `doctor`, docs, and the release

**Files:**
- Modify: `agentflow/doctor.py` (`_claims_check`, `run_checks`), `CLAUDE.md` (the `doctor.py` paragraph and the engine
  list), `docs/agentflow-playbook.md` (§14 is done in Task 2; add `agentflow backlog release <id>` and the `claim_lost` stop to
  the §10 cheat sheet; the Backlog section is B4's), `docs/specs/agentflow-backlog-file-proposal.md` (the Events list), `CHANGELOG.md`,
  `pyproject.toml`, `agentflow/pack.py`
- Test: `agentflow/tests/test_doctor.py`, `agentflow/tests/test_packaging.py`

**Interfaces:**
- Produces: `doctor._claims_check(cfg: Config, repo: Path) -> Check`, named `claims`: `ok` with the number of claims on
  `origin`; `fail` with the `git ls-remote` error when `origin` can't be read; run only when `claims.enabled`. It runs
  one `git ls-remote origin '<prefix>/*'` and no push.

- [ ] **Step 1: Write the failing tests**
  - `test_doctor_reads_claims_with_one_ls_remote`: with two claims on a bare origin, the check is `ok` and names `2`;
    the only commands run for it are `git remote get-url` and `git ls-remote` (patch `run_bounded`).
  - `test_doctor_fails_on_an_unreachable_origin`, and `test_doctor_skips_claims_when_off_or_not_backlog`.
  - `test_packaging` already fails until the version and changelog agree.

- [ ] **Step 2: Run them and see them fail.** Expected: FAIL.

- [ ] **Step 3: Implement the check, then the docs.** In `CLAUDE.md`, add `claims.py` and `claim_sync.py` to the engine
  list and the `ls-remote` to `doctor`'s allowed commands. In the spec's Events list, add after `claim_refused`: "a
  start that loses has no run to log it in, so the refusal is the start's error text and the Board's `item_failed`";
  and add `claim_sync_failed` and `claim_release_failed`. Under `## [Unreleased]` add one `Added` line per user-visible
  change (claims on `origin`, the three keys, `agentflow backlog release`, remote cards, queue, doctor); then bump
  `pyproject.toml` and `pack.ENGINE_VERSION` to `3.11` and rename `[Unreleased]` to `## [3.11] - <today>` with a fresh empty
  `[Unreleased]` above it.

- [ ] **Step 4: Run the touched files and the whole suite.** `test_doctor.py`, `test_packaging.py`, then the full suite
  (25–60 min), then `uvx ruff@0.17.4 check .` (must print `All checks passed!`) and `bin/agentflow validate` (`0 warnings`).

- [ ] **Step 5: Commit**: `git commit -m "Check claims in doctor, document them, and release 3.11."`

---

## Self-review

- **Spec coverage:** D4 claim and CAS (Task 3, 5), advances (6), release on fail/cancel/archive (6), after merge (7),
  stale (3, 9, 10), reopen (8), host refusal and no-origin/claims-off (3, 5), `by` (3); D5 claim rows (9); D9 (9);
  Events (4, with `claim_refused` decided in Plan decisions); Security (Global Constraints, Task 3's `parse_claim` and
  host tests, Task 7's no-push test); `doctor` (11); the config keys (2); the manual probe (1). Success criteria 2 and 3
  are Tasks 9 and 5.
- **Proportion:** each task carries test names with their assertions and signatures, not bodies.
- **Type consistency:** `Claim`, `claims.create/advance/release/read/list_remote`, `claim_sync.start/record/sync/
  release_run/release_merged`, `state["claim"]`, `backlog.release_on_reopen` are named identically wherever they recur.

Plan touches `guards`-adjacent code only through `pipeline.py` and `run_control.py`, none of the security-overlay paths
in `CLAUDE.md`, but it writes to a remote and parses data that anyone with push access controls, so run
`/doubt-driven-development` on `claims.py`'s `parse_claim`, the push classification and Task 6's `claim_lost` handling
before Build.
