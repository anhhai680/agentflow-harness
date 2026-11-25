# Backlog file, slice B2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** In a `backlog` pack, the runner keeps `backlog.yml` up to date on the run's own branch (D3), agents can't
edit it, and the dashboard adds, edits and reorders items in the working tree and starts items that exist only
there (D7). A run started from a new card ends with a PR whose diff sets the item `done`.

**Architecture:** A new `agentflow/backlog_writes.py` holds every runner edit of the file. Each edit reads the file
from the run's `HEAD` commit (`gitops.read_blob`), edits the text with `backlog_file`'s pure text functions, writes
it, and commits it with a new `gitops.commit_backlog`, which allows exactly that one path. `pipeline.start`,
`lanes._finish` and `feature_lane.run_finish` call it. Each commit records a `backlog_updated` event. The file path
joins `guardrails.forbidden_paths` at config load, so the guard, `gitops.commit` and the agents' `--settings`
denies all refuse it in a run. On the dashboard side, `backlog.py` and `/api/backlog/file` do the working-tree edits,
each checked against the sha-256 the page read.

**Tech Stack:** Python 3.12, stdlib + `pyyaml`, pytest, the dashboard's plain JS (`agentflow/dashboard_ui/board.js`).

**Spec:** `docs/specs/agentflow-backlog-file-proposal.md` (approved 2026-11-24). This plan covers slice **B2**:
D3, the guard, and D7 (without issue absorption, which is B4). It also covers two pieces the B1 plan pushed to B2:
D6's **Add** button and D5's `validate --fix` for "evidence says done". Read D3, D5 and D7 before Task 1, and the
B1 plan (`docs/plans/2026-11-24-agentflow-harness-backlog-b1.md`) for what already exists.

### What B2 does not do

- **B3:** claims (D4), the start refusal on `claimed`, the `item_claimed`/`claim_*` events, remote cards, reopen's
  claim release, and `doctor`.
- **B4:** issue absorption and **Add to backlog** on synced issues, `init`/`detect`, the deprecation note, and the
  playbook's Backlog section.
- **Deferred, decided 2026-11-25:** starting an item whose `lane` isn't `dashboard.backlog.module_lane`. Its Start
  is still refused with `NOT_STARTABLE`, and `queue` still skips it.

### Plan decisions

The spec leaves these open, or the code differs from the spec's module table.

- **The `status: done` commit stays in `finish`** (D3), after the base sync and before the conformance audit.
  Decided 2026-11-25: gate evidence carries across that one commit. A full-tier pass on tree T also counts for tree
  T' when a `backlog_updated` event with `change: "done"` records `before_tree: T, tree: T'`. Without this rule, the
  audit would run the whole verify set again on every backlog run. Only `change: "done"` carries. Every other runner
  write happens before `verify`, so none needs to.
- `finish` is `feature_lane.run_finish`, not `flow_control.py` as the spec's table says.
- **No `guards.py` change.** `config.load_config` appends the normalised `work_source.path` to
  `guardrails.forbidden_paths` when the kind is `backlog`, as it does for `pack.ENGINE_FORBIDDEN`. That one list
  already feeds `guards.check_step`, `gitops.commit`, `commit_artifact`, `commit_sync` and the agents' `Edit(./…)`
  denies.
- **Where a run reads its item.** `{spec}` and `modules_done` read the file at the sha of the run's latest
  `backlog_updated` commit (`state.backlog_sha`), falling back to `base_sha` when there is none. A new item appended
  by the first commit is therefore visible to the run's own steps. That commit is the runner's own, never an agent's.
- **Which runs write.** Writes happen only in a `backlog` pack, for a run with a `module` param whose lane is the
  item's lane, or `module_lane` when the item has none. A `module_lane` run whose id is in neither the file at the
  fork commit nor the main checkout's working tree, and that has no `--new-module`, is refused at start:
  `module <id> is not in backlog.yml: add it (agentflow backlog add <id> --title …) or start it from the dashboard`.
- **"Local-only"** means the item is not in the file at the tip of the local `fork_from` branch, which is the
  commit a run forks from. That is when the first commit appends the item's block, taken from the main checkout's
  working tree. The Board's *local* badge marks an item that is absent from, or different in, the file at that tip.
- **The New task form in a backlog pack.** A new module is written to `backlog.yml` as `status: ready`, with its
  title set to the responsibility as one line (≤ 200 characters, cut with `…`) and `depends_on` set to the chosen
  deps. It is not written to `runs/backlog.jsonl`. "Stop after" and "Runs after" are hidden for it. An issue-only
  card still goes to `backlog.jsonl`, and its run appends `issue-<N>` with `issue: N` and the title
  `GitHub issue #<N>`. Task and topic cards in other lanes stay as they are.
- **D6's Add** sends only `spec` (plus `status: backlog`). When `id` or `title` is empty, the server derives it the
  way `agentflow backlog add --spec` does (`backlog._spec_id`, `_spec_title`).
- **Move up/down** swaps the item with the nearest item in that direction whose status is `backlog` or `ready`.
  Done and dropped items stay where they are.
- **Reopen** (D4) and its endpoint ship with B3, together with claims.

## Global Constraints

- Engine dependencies stay stdlib + `pyyaml>=6`. No project strings in the engine (`test_portability.DENYLIST`).
- The golden files `agentflow/tests/fixtures/explain/*.txt` stay byte-identical. The `python` fixture pack keeps
  `capability_map`, and backlog tests switch it with `flow_repo.configure({"work_source": {"kind": "backlog",
  "path": None}})`, as `test_backlog.py::bbk` does.
- The runner commits only on top of its own commits (`expected_head=runner_head(run)`), never touches the main
  checkout's working tree, and commits `backlog.yml` only through `gitops.commit_backlog`.
- A runner edit reads the file from a commit (`gitops.read_blob`), never from a working tree, and keeps every line
  outside the edited ones byte for byte (`backlog_file`'s checks).
- `title`, `notes` and unknown keys never reach an argv, a branch name, a module id or a prompt. A new card's
  `--task` is the responsibility the developer typed, as today.
- Dashboard writes go through `backlog_file` (`safeio`, no symlink followed), are never committed, and are refused
  with 409 when the file's sha-256 is not the one the page sent.
- No new config key. If one turns out to be needed, it ships with all four obligations listed in `CLAUDE.md`.
- Commit messages are one imperative sentence. The runner's are fixed text: `Add <id> to the backlog`,
  `Mark <id> ready in the backlog`, `Record the <id> spec in the backlog`, `Record the <id> plan in the backlog`,
  `Mark <id> done in the backlog`.
- `uvx ruff@0.17.4 check .` before every commit. Tests run with
  `uv run --no-project --with pyyaml --with pytest pytest <file> --import-mode=prepend -q`.

## Review Focus

1. **The file changed in an editor while the page was open.** An Edit, Add or Move then answers 409
   `backlog.yml changed on disk since the page read it: reload`, and the file's bytes don't change (Task 6).
2. **A local-only item that depends on another uncommitted item.** The start is refused before any worktree
   exists: `<id> depends on <dep>, which is not in backlog.yml on <fork_from>: commit it first` (Task 3).
3. **An agent that writes `backlog.yml` through Bash rather than Edit, or a pack whose path is `./backlog.yml`.**
   Either way the step stops as a `guard_violation` (`forbidden_path`), and nothing is committed (Task 1).
4. **A teammate edits the same item on `fork_from` (its `notes`, say), and the base sync merges that in at
   `finish`.** The done commit is made on top of the merge, changes only the `status` line, and keeps the
   teammate's edit (Task 5).
5. **Move up over a done or dropped item, or over a block with a comment directly above it.** The two open items
   swap, the comment moves with its own item, and everything else stays in place. A neighbour written in flow style
   is refused (Task 6).

---

### Task 1: Agents can't edit the backlog file

**Files:**
- Modify: `agentflow/config.py` (the `forbidden_paths` list in `load_config`, next to `pack.ENGINE_FORBIDDEN`)
- Test: `agentflow/tests/test_backlog_runs.py` (new)

**Interfaces:**
- Produces: `cfg.guardrails.forbidden_paths` ends with `PurePosixPath(cfg.work_source.path).as_posix()` (so
  `./backlog.yml` becomes `backlog.yml`) when `work_source.kind == "backlog"`, and is unchanged for any other kind.

- [ ] **Step 1: Write the failing tests**

```python
def test_a_backlog_pack_forbids_its_file_in_a_run(tmp_cfg):
    cfg = load_config(tmp_cfg({"work_source": {"kind": "backlog", "path": "./backlog.yml"}}))
    assert cfg.guardrails.forbidden_paths[-1] == "backlog.yml" and ".agentflow/**" in cfg.guardrails.forbidden_paths

def test_other_kinds_do_not(tmp_cfg):
    assert "backlog.yml" not in load_config(tmp_cfg({})).guardrails.forbidden_paths

def test_an_agent_step_that_edits_the_file_is_a_guard_violation(bflow):
    # bflow: the python fixture pack as a backlog pack with a committed backlog.yml holding item `b` (ready)
    bflow.script(define=[{"bash": "echo '# x' >> backlog.yml"}])  # a shell write, not an Edit tool call
    run = bflow.start("feature", module="b")
    assert run.state["waiting"]["kind"] == "guard_violation"
    assert "backlog.yml" in run.state["waiting"]["text"]
    assert git(bflow.worktree(run), "log", "--format=%s", f"{run.state['base_sha']}..HEAD") == ""
```

Build the `bflow` fixture in this file from `flow_repo` and `fake_agent`, as `test_conformance.py` builds its run
fixtures. Every later task's run tests use it.

- [ ] **Step 2: Run them and see them fail**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_backlog_runs.py --import-mode=prepend -q`
Expected: FAIL. `forbidden_paths` doesn't hold `backlog.yml`, and the step commits.

- [ ] **Step 3: Implement.** Add the path after `pack.ENGINE_FORBIDDEN`, inside the same `dict.fromkeys`.

- [ ] **Step 4: Run the tests and see them pass.** Also run `test_guards.py`, `test_config.py` and
  `test_explain_golden.py`. Expected: PASS.

- [ ] **Step 5: Commit**: `git commit -m "Refuse agent edits of the backlog file in a run."`

### Task 2: The runner's backlog commit

**Files:**
- Modify: `agentflow/backlog_file.py` (split the pure text edits out of `set_fields`/`append_item`),
  `agentflow/gitops.py` (`commit_backlog`), `agentflow/commit_checks.py` (`RUNNER_COMMITS`), `agentflow/state.py`
  (the event's required fields, and `backlog_sha`), `agentflow/activity.py` (a sentence for each new event)
- Create: `agentflow/backlog_writes.py`
- Test: `agentflow/tests/test_backlog_file.py`, `agentflow/tests/test_backlog_runs.py`

**Interfaces:**
- Produces:
  - `backlog_file.set_fields_text(text: str, item_id: str, fields: dict[str, object]) -> str` and
    `append_item_text(text: str, item: BacklogItem) -> str`: the new text. They run the same checks and raise the
    same `EditRefused`. `set_fields` and `append_item` become `_load`, then the text function, then `_store`.
  - `gitops.commit_backlog(worktree: Path, message: str, cfg: Config, path: str, expected_text: str,
    expected_head: str) -> str`. It runs `commit_artifact`'s checks, minus the forbidden-path refusal, for exactly
    `path`, which must equal `cfg.work_source.path` (normalised), or the commit is refused. It also requires the
    staged blob to be `expected_text`'s UTF-8 bytes (`hash-object --no-filters`, as `commit_lessons` does).
    `NothingToCommit` when the file is unchanged. Share the code after the forbidden check with `commit_artifact`
    through one private helper rather than copying it.
  - Event `backlog_updated`: `item`, `change` (`added | ready | spec | plan | done`), `fields` (the keys set and
    their values), `sha`, `path`, `before_tree`, `tree`, and `local` (bool, `added` only). It is in
    `RUNNER_COMMITS` with `sha` as its head field. `state.py` requires `("item", "change", "sha")` and keeps
    `state["backlog_sha"]` as the newest one's `sha`.
  - Event `backlog_skipped`: `item`, `change`, `reason`.
  - `backlog_writes.write(cfg: Config, run: Run, worktree: Path, change: str, edit: Callable[[str], str],
    message: str, local: bool = False) -> str | None`. It reads the file at `runner_head(run)` with
    `gitops.read_blob(worktree, head, path, backlog_file.FILE_MAX)`, applies `edit`, writes the result with
    `safeio.write_text`, commits it with `commit_backlog`, and emits `backlog_updated`. It returns the sha, or None
    when `edit` changes nothing. It raises `CommitRejected` and `backlog_file.EditRefused` to its caller.
  - `backlog_writes.applies(cfg: Config, run: Run, item: BacklogItem | None) -> bool`: true for a `backlog` pack and
    a run with a `module` param whose `pipeline` is `item.lane or module_queue.module_lane(cfg)`.

- [ ] **Step 1: Write the failing tests**
  - `test_set_fields_text_and_append_item_text_match_the_file_functions`: on `GOOD` with CRLF endings, both give
    the text the path functions write, and both raise `EditRefused` where those do.
  - `test_commit_backlog_commits_exactly_the_file`: in a temp repo with `backlog.yml` forbidden, it commits a
    `status` change. The new commit's diff is `["backlog.yml"]`, and the message ends with `project.commit_trailer`.
  - `test_commit_backlog_refuses_another_path_a_rewritten_file_and_a_moved_head`, three cases, each a
    `CommitRejected` with the index reset: `path="docs/x.md"`; the file rewritten after the caller's write (its
    blob isn't `expected_text`); and `expected_head` not `HEAD` (`agent_commit`).
  - `test_write_reads_the_committed_file_not_the_work_tree`: put a dirty, different `backlog.yml` in the worktree,
    then call `write(..., edit=lambda t: set_fields_text(t, "b", {"status": "done"}))` and assert it raises
    `CommitRejected` (kind `outside_output`), with the dirty text left in place. This proves the edit's input is
    the `HEAD` blob and that a foreign change in the worktree is never committed.
  - `test_a_backlog_commit_is_a_runner_commit`: after `write`, `runner_head(run)` is its sha,
    `foreign_commits(...) == []`, `run.state["backlog_sha"]` is its sha, and `activity` renders a sentence naming
    the item and the change.

- [ ] **Step 2: Run them and see them fail.** Expected: FAIL with `AttributeError`.

- [ ] **Step 3: Implement** the interfaces above.

- [ ] **Step 4: Run the tests and see them pass.** Also run `test_gitops.py`, `test_activity.py` and
  `test_state.py`. Expected: PASS.

- [ ] **Step 5: Commit**: `git commit -m "Add the runner's backlog commit, which may change only the backlog file."`

### Task 3: The first commit: a new or local-only item, or `status: ready`

**Files:**
- Modify: `agentflow/backlog_writes.py` (`plan_start`, `first_commit`), `agentflow/pipeline.py` (`start`,
  `_set_up`, `_check_new_module`), `agentflow/conditions.py` (`_fork`), `agentflow/backlog.py`
  (`_issue_run_params`, and `begin_start`'s card branch)
- Test: `agentflow/tests/test_backlog_runs.py`, `agentflow/tests/test_backlog.py`

**Interfaces:**
- Consumes: `write`, `applies` and `append_item_text`/`set_fields_text` (Task 2), `work_source.item` and
  `items_at` (B1).
- Produces:
  - `@dataclass(frozen=True) class StartWrite: change: Literal["added", "ready"]; item: BacklogItem; local: bool`
  - `backlog_writes.plan_start(cfg: Config, repo: Path, pipeline: str, params: dict, new_module: dict | None) ->
    StartWrite | None`. It reads the file at `gitops.commit_at(repo, fork_from)` and the main checkout's working
    tree, and raises `ValueError` with the messages below, so `start` refuses before `Run.create`.
  - `backlog_writes.first_commit(cfg: Config, run: Run, worktree: Path, plan: StartWrite) -> str`
  - `_fork(run)` returns `(checkout, run.state.get("backlog_sha") or run.state.get("base_sha"))`.

`plan_start`'s rules, in order. They apply only in a `backlog` pack, for a run with a `module` param:

| Case | Result |
|---|---|
| `new_module` given, and the id is already in the fork's file or the working tree | `ValueError("<id> is already an item in backlog.yml: start module:<id>")` |
| `new_module` given | `added`: `BacklogItem(id, deps, title=<responsibility as one line, ≤ 200>, status="ready", issue=int(params["issue"]) if given)`, `local=False` |
| The fork's file has it, `backlog` | `ready` |
| The fork's file has it, `ready` | None |
| The fork's file has it, `done` or `dropped` | `ValueError("module <id> is <status> in backlog.yml: set it to ready first")` |
| Only the working tree has it | `added`: that item (with `status: ready` when it said `backlog`), `local=True`; `done`/`dropped` refused as above |
| Neither has it, and `pipeline == module_lane` | `ValueError("module <id> is not in backlog.yml: add it (agentflow backlog add <id> --title …) or start it from the dashboard")` |
| Neither has it, in another lane | None |

For `added`, every `depends_on` entry must be in the fork's file:
`ValueError("<id> depends on <dep>, which is not in backlog.yml on <fork_from>: commit it first")`.

- [ ] **Step 1: Write the failing tests**
  - `test_a_new_module_run_appends_the_item_as_its_first_commit`: `start(..., new_module={"module": "n",
    "responsibility": "Do the new thing", "deps": ["b"]})`. The branch's first commit changes only `backlog.yml`.
    The file at `HEAD` parses with `n` last, as `ready`, with title `Do the new thing` and `depends_on [b]`. The
    event is `backlog_updated{change: "added", local: False}`, and `make_ctx(..., run).values["spec"]` is the
    default spec path for `n`.
  - `test_a_local_only_item_goes_into_the_first_commit_and_the_work_tree_is_untouched`: append `l` to the main
    checkout's `backlog.yml` without committing, then start `module=l`. The branch's file has `l`, the event says
    `local: True`, and the main checkout's file bytes are the same as before the start.
  - `test_starting_a_backlog_status_item_sets_ready_in_the_first_commit`: one commit, whose diff is the `status`
    line only.
  - `test_a_ready_item_gets_no_first_commit`: `HEAD == base_sha` after `start`.
  - `test_refusals_leave_no_run`, parametrised over these cases, each asserting the `ValueError` text above and that
    neither `list_runs` nor `git branch` gained anything:
    - an unknown id;
    - a `done` item;
    - a local-only item that depends on an uncommitted one (Review Focus 2);
    - a `new_module` whose id is already an item.
  - `test_an_issue_card_in_the_module_lane_appends_issue_n` (in `test_backlog.py`, on `bbk` with issue sync):
    `begin_start("issue:7")` returns `new_module == {"module": "issue-7", "responsibility": "GitHub issue #7",
    "deps": []}` and `params["issue"] == "7"`.
  - `test_a_new_module_card_in_a_backlog_pack_keeps_new_module_and_task`: a `backlog.jsonl` card with `new_module`
    (written by hand into the log, as B1's import tests do) gives a `StartJob` with both `params["task"]` and
    `new_module` set.

- [ ] **Step 2: Run them and see them fail.** Expected: FAIL. `_check_new_module` refuses backlog packs.

- [ ] **Step 3: Implement**
  - `start`: call `plan_start` right after `_check_new_module`, and pass its result into `_set_up`.
  - `_set_up`: call `first_commit` after `run_updated`, where `_add_module_row` is called today.
  - `_check_new_module`: return None for `backlog` (no row template is needed) instead of raising.
  - `_issue_run_params`: return the `new_module` dict for `backlog` too.
  - `begin_start`: for a card in a `backlog` pack, keep `new_module` while setting `params["task"]`.

- [ ] **Step 4: Run the tests and see them pass.** Also run `test_new_module.py`, `test_pipeline.py`,
  `test_backlog.py`, `test_conditions.py` and `test_explain_golden.py`. Expected: PASS.

- [ ] **Step 5: Commit**: `git commit -m "Write a new or local-only item, or its ready status, in a backlog run's first commit."`

### Task 4: `spec:` and `plan:` after the step that writes them

**Files:**
- Modify: `agentflow/backlog_writes.py` (`after_output`), `agentflow/lanes.py` (`_finish`)
- Test: `agentflow/tests/test_backlog_runs.py`

**Interfaces:**
- Produces: `backlog_writes.after_output(cfg: Config, run: Run, worktree: Path, values: dict[str, str],
  output: str) -> str | None`. When `output` (the step's resolved `output`, repo-relative) equals `values["spec"]`
  and the item's `spec` differs, it sets `spec`. When it equals `values["plan_target"]` and the item's `plan`
  differs, it sets `plan`. Otherwise it writes nothing. The item is read from the file at `HEAD`. It applies only
  when `applies(...)` holds and the item is in that file.

- [ ] **Step 1: Write the failing tests**
  - `test_define_records_the_spec_path_it_wrote`: the item `b` has no `spec:`, and the define step writes the
    default spec. Right after `step_committed` for `define/draft`, the next commit is
    `Record the b spec in the backlog`, it changes only `backlog.yml`, and the file at `HEAD` has
    `spec: docs/specs/SPEC-b.md`.
  - `test_plan_records_the_plan_path`: the same check after the plan step, for `plan:`.
  - `test_a_spec_the_item_already_names_writes_nothing`: the item names its spec, and no `backlog_updated` with
    `change: "spec"` is emitted.
  - `test_a_reconcile_step_that_rewrites_the_same_spec_writes_nothing`.

- [ ] **Step 2: Run them and see them fail.** Expected: FAIL.

- [ ] **Step 3: Implement.** In `lanes._finish`, after `step_committed` and before `step_finished`, call
  `after_output` for a committed step with an `output`. A `CommitRejected` or `EditRefused` from it fails the step
  through `self._fail(sr, [Violation(kind, str(exc))])`, using `guard_violation` for an `EditRefused`.

- [ ] **Step 4: Run the tests and see them pass.** Also run `test_agent_step.py` and `test_feature_lane.py`.
  Expected: PASS.

- [ ] **Step 5: Commit**: `git commit -m "Record the spec and plan a backlog run writes in its item."`

### Task 5: `status: done` in `finish`, with its gate evidence carried

**Files:**
- Modify: `agentflow/backlog_writes.py` (`mark_done`), `agentflow/feature_lane.py` (`run_finish`),
  `agentflow/gates.py` (`carried_trees`, `reusable_passes`), `agentflow/conformance.py` (`_gates`)
- Test: `agentflow/tests/test_backlog_runs.py`, `agentflow/tests/test_conformance.py`, `agentflow/tests/test_gates.py`

**Interfaces:**
- Produces:
  - `backlog_writes.mark_done(cfg: Config, run: Run, worktree: Path) -> str | None`: sets `status: done` when
    `applies(...)` holds and the item at `HEAD` isn't `done`. When the item isn't in the file at `HEAD` (for
    example, a teammate removed it on `fork_from` and the sync merged that in), it emits
    `backlog_skipped{change: "done", reason: "<id> is not in backlog.yml at HEAD"}` and returns None.
  - `gates.carried_trees(events: list[dict], tree: str) -> frozenset[str]`: `tree`, plus the `before_tree` of every
    `backlog_updated` event with `change == "done"` whose `tree` is already in the set, followed to a fixpoint.
    Pure.
  - `reusable_passes` counts `same_tree` when `e["tree"] in carried_trees(events, tree)`, and `conformance._gates`
    counts the same way against `final_tree`.

- [ ] **Step 1: Write the failing tests**
  - `test_carried_trees_follows_only_done_commits` (`test_gates.py`):
    `carried_trees([bu("done", "T0", "T1")], "T1") == {"T0", "T1"}`;
    `carried_trees([bu("spec", "T0", "T1")], "T1") == {"T1"}`; and
    `carried_trees([bu("done", "T0", "T1")], "T2") == {"T2"}`.
  - `test_the_audit_takes_a_pass_on_the_tree_before_the_done_commit` (`test_conformance.py`): `evaluate` with a
    verify pass on `T0`, a done event `T0 → T1` and `final_tree="T1"` is met. The same events with a `spec` event
    instead are unmet.
  - `test_finish_marks_the_item_done_before_the_push_and_reruns_no_gate` (`test_backlog_runs.py`): a whole feature
    run on `bflow`, with the fake `gh`. The pushed head's `backlog.yml` has `status: done` for the item, the commit
    before it is the done commit, and no `gate_result` event comes after the `backlog_updated{change: "done"}`
    event. `conformance_checked` is met.
  - `test_finish_keeps_a_teammates_edit_merged_by_the_sync` (Review Focus 4): before `finish`, commit a `notes:`
    change to the same item on `fork_from`. The pushed file has both the new notes and `status: done`, and the done
    commit's diff is the one `status` line.
  - `test_a_resumed_finish_does_not_mark_done_twice`: stop the run after the done commit (a failing `gh pr create`
    from the fake `gh`), then resume it. There is exactly one `backlog_updated{change: "done"}`.

- [ ] **Step 2: Run them and see them fail.** Expected: FAIL.

- [ ] **Step 3: Implement.** In `run_finish`, call `mark_done` after `runner_commits_only` (which follows
  `run_sync`) and before the `deferred_full` check. A `CommitRejected` or `EditRefused` from it becomes
  `raise_hitl(..., "blocked", key, ...)`, then `"stopped"`.

- [ ] **Step 4: Run the tests and see them pass.** Also run `test_ci.py`, `test_conformance.py` and
  `test_feature_lane.py`. Expected: PASS.

- [ ] **Step 5: Commit**: `git commit -m "Mark the item done in finish before the push, carrying gate evidence across that commit."`

### Task 6: Working-tree edits: Add, Edit and Move, and the snapshot the page needs

**Files:**
- Modify: `agentflow/backlog_file.py` (`expect_sha` on the path functions, `move_item`, `Stale`),
  `agentflow/backlog.py` (`add_file_item`, `edit_item`, `move`, `fix_done`, the snapshot fields, `refresh`,
  `add`), `agentflow/dashboard.py` (`POST /api/backlog/file`, its docstring line, and `BACKLOG_ROUTES`),
  `agentflow/config.py` (mark the "done by its runs" warning `fixable`), `agentflow/flow.py` (`_fix_pack` calls
  `fix_done`)
- Test: `agentflow/tests/test_backlog_file.py`, `agentflow/tests/test_backlog.py`, `agentflow/tests/test_dashboard.py`,
  `agentflow/tests/test_backlog_cli.py`

**Interfaces:**
- Produces:
  - `class Stale(EditRefused)`. `set_fields`, `append_item` and `move_item(path: Path, item_id: str, up: bool,
    expect_sha: str | None = None) -> str` take `expect_sha` and return the new file's sha-256 hex. A file whose
    bytes hash to anything else raises `Stale("backlog.yml changed on disk since the page read it: reload")`.
  - `move_item` swaps the item's span with that of the nearest item in the chosen direction whose status is
    `backlog` or `ready`. A span is the item's block, plus the comment lines directly above its `- id:` line with
    no blank line between. It raises `EditRefused` when there is no such neighbour (`<id> is already first` or
    `last`), or when either block isn't in block style. The result is checked as `set_fields` checks its result:
    the same items, with only those two swapped, and no new problem.
  - `backlog.EDIT_KEYS = ("title", "lane", "status", "depends_on", "issue", "spec", "notes")`.
  - `backlog.add_file_item(cfg: Config, repo: Path, item: dict, sha: str) -> str`,
    `edit_item(cfg: Config, repo: Path, item_id: str, fields: dict, sha: str) -> str`, and
    `move(cfg: Config, repo: Path, item_id: str, direction: Literal["up", "down"], sha: str) -> str`. Each returns
    the new sha-256 and holds one module-level `_FILE_LOCK`. They raise `Invalid` for a key outside `EDIT_KEYS`, a
    value of the wrong type, or a lane not in `cfg.lanes`, and `Refused` when `status: done` or `dropped` is set on
    an item with a live run. `add_file_item` derives an empty `id` or `title` from `item["spec"]`.
  - `POST /api/backlog/file` with `{"op": "add" | "edit" | "move", "sha256": str, ...}`: `add` takes `item`, `edit`
    takes `id` and `fields`, and `move` takes `id` and `direction`. It answers 200 `{ok, sha256}`, 409 for
    `Stale`/`Refused`, and 400 for `Invalid`/`EditRefused`/`BadRequest`.
  - `backlog.add` raises `Invalid("in a backlog pack a new module is an item of backlog.yml: POST /api/backlog/file")`
    for `new_module` in a `backlog` pack.
  - The snapshot gains `backlog_sha256` (`""` when the file is missing), and each file card gains `local: bool` and
    `item: {title, lane, status, depends_on, issue, spec, notes}`. `refresh` stores
    `committed_items: {id: json.dumps(dataclasses.asdict(item), sort_keys=True)}` in `backlog_state.json`, read
    from the file at the `fork_from` tip. A card is `local` when its id is missing from that map or its value
    differs. The snapshot itself still runs no git.
  - `backlog.fix_done(cfg: Config, repo: Path, dry_run: bool) -> list[str]`: for each item that `validate` warns
    is "done by its runs", it prints `<id>: status set to done` and, unless `dry_run`, applies
    `set_fields(status="done")`.

- [ ] **Step 1: Write the failing tests**
  - `test_a_stale_sha_is_refused_and_the_file_is_unchanged` (Review Focus 1), for each of the three functions.
  - `test_move_swaps_with_the_nearest_open_item_and_keeps_comments` (Review Focus 5): take items `a` (ready),
    `d` (done), and `b` (ready, with a `# about b` comment directly above it). `move_item("b", up=True)` gives the
    order `b, d, a`, and the comment is still directly above `b`. The first open item moved up raises
    `already first`, and a flow-style neighbour raises `EditRefused`.
  - `test_the_file_endpoint_adds_edits_and_moves_in_the_work_tree_only` (`test_dashboard.py`): three posts, each
    answered 200 with a new `sha256`. Afterwards `git status --porcelain` shows only ` M backlog.yml`, and
    `git log` has no new commit.
  - `test_the_file_endpoint_answers_409_on_a_stale_sha_and_400_on_a_bad_key`.
  - `test_add_from_a_spec_derives_id_and_title`: posting `item: {"spec": "docs/specs/x.md", "status": "backlog"}`
    adds `x` with the spec's `# ` heading as its title.
  - `test_a_new_module_card_is_refused_in_a_backlog_pack` (`backlog.add`).
  - `test_a_card_is_local_until_its_item_is_committed`: add an item through `add_file_item` and it is
    `local: True` after `refresh`. After it is committed on `fork_from` and the Board refreshes, it is
    `local: False`.
  - `test_validate_fix_sets_done_on_items_their_runs_finished` (`test_backlog_cli.py`): `--dry-run` prints the line
    and leaves the file alone. A real run sets `status: done`, and `validate` then prints `0 warnings`.

- [ ] **Step 2: Run them and see them fail.** Expected: FAIL.

- [ ] **Step 3: Implement** the interfaces above. `_load` hashes the raw bytes it reads, and that hash is what
  `expect_sha` is compared with.

- [ ] **Step 4: Run the tests and see them pass.** Also run `test_backlog_import.py` and `test_config.py`.
  Expected: PASS.

- [ ] **Step 5: Commit**: `git commit -m "Add, edit and reorder backlog items from the dashboard in the working tree."`

### Task 7: The Board: Edit dialog, Move, the local badge, Add, and the local-only run note

**Files:**
- Modify: `agentflow/dashboard_ui/board.js`, `agentflow/views.py` (`_card`: the run card's `backlog_note`)
- Test: `agentflow/tests/test_dashboard_js.py`, `agentflow/tests/test_dashboard.py` (run cards)

**Interfaces:**
- Consumes: the snapshot fields and `/api/backlog/file` (Task 6), and `backlog_updated{local: true}` (Task 3).
- Produces:
  - Run cards gain `backlog_note: str`. For a run with a `backlog_updated{change: "added", local: true}` event it
    reads `Your uncommitted backlog.yml entry for <id> went into this run: discard it (git checkout backlog.yml)
    before you pull the merge.`, and otherwise `""`.
  - `board.js` pure helpers, so node can test them: `editBody(item, form) -> {op: "edit", id, fields, sha256}`, which
    puts only the changed keys in `fields`; `moveBody(item, up) -> {op: "move", id, direction, sha256}`;
    `addFromSpecBody(path) -> {op: "add", item: {spec: path, status: "backlog"}, sha256}`; and
    `newModuleBody(form, sha) -> {op: "add", item: {id, title, depends_on, issue?, status: "ready"}, sha256}`.

- [ ] **Step 1: Write the failing tests**
  - `test_edit_sends_only_the_changed_fields_with_the_sha`.
  - `test_move_buttons_only_on_open_file_cards`: a file card has `Move up`/`Move down`, and a form card or an issue
    card has neither.
  - `test_a_local_card_has_a_local_badge`: the text is `local: commit to share`.
  - `test_not_on_the_backlog_entries_have_an_add_button_that_sends_the_spec`.
  - `test_the_new_task_form_in_a_backlog_pack_adds_to_the_file_and_hides_stop_and_after`: `newModuleBody` takes its
    title from the responsibility as one line, and for this kind the form builds no `nt-stop` or `nt-after` field.
  - `test_a_409_tells_the_person_to_reload` (`js_async`): a stubbed `api` that rejects with 409 makes the toast read
    `backlog.yml changed on disk since the page read it: reload`.
  - `test_the_run_card_carries_the_local_only_note` (`test_dashboard.py`, through `views._card`).

- [ ] **Step 2: Run them and see them fail.** Expected: FAIL.

- [ ] **Step 3: Implement.** The Edit dialog reuses `dialog()` from `dialogs.js`, with one field per `EDIT_KEYS`
  entry: `lane` and `status` are selects, and `depends_on` is the multi-select that `newTaskDialog` uses. Text stays
  text: every value is set through `el(..., {text})` or `.value`, never as HTML. After any write the page calls
  `refreshAfter()`.

- [ ] **Step 4: Run the tests and see them pass.** Expected: PASS.

- [ ] **Step 5: Commit**: `git commit -m "Edit, reorder and add backlog items on the Board, and flag local items."`

### Task 8: The slice's acceptance test, the docs, and release 3.10

**Files:**
- Modify: `agentflow/tests/test_backlog_runs.py`, `docs/agentflow-playbook.md` (§10 and the §14 `work_source`
  line), `CHANGELOG.md`, `pyproject.toml` and `agentflow/pack.py` (`3.9` → `3.10`)

- [ ] **Step 1: Write the acceptance test** `test_a_run_from_a_new_card_ends_with_a_pr_whose_diff_sets_done`, on
  `bflow` with the fake `gh`, modelled on `test_conformance.py::test_a_whole_feature_run_meets_every_obligation`:
  - add a new module through `backlog.add_file_item` (the dashboard's path, `status: ready`);
  - start it with `begin_start` and `run_start`, using a `spawn` that calls `flow.py run` in-process;
  - advance the run to `pr_opened`;
  - assert that `git diff <fork_from>...<pushed sha> -- backlog.yml` adds the item's block, with `status: done`.

- [ ] **Step 2: Run it.** Expected: PASS (Tasks 1–7 built it). A failure here is a bug in an earlier task: fix it
  there and keep this test as it is.

- [ ] **Step 3: Docs.** Make these playbook edits:
  - §10: one line for the Board's Edit, Move and *local* badge.
  - §14 `work_source`: agents can't edit the file in a run, and the runner writes it on the run's branch: the new or
    local-only item, `ready`, `spec`, `plan` and `done` (D3).

  Add the `CHANGELOG.md` lines under `[Unreleased]`:
  - `Added`: the runner's backlog writes, and the Board's Add, Edit and Move.
  - `Changed`: agents can't edit `backlog.yml`; a `module_lane` run of an id that isn't an item is refused; the New
    task form writes new modules to `backlog.yml` in a backlog pack.

- [ ] **Step 4: Release 3.10.** Bump both versions, rename `[Unreleased]` to `## [3.10] - <today>`, and open a fresh
  `[Unreleased]`. Run `test_packaging.py`, then the full suite, then `uvx ruff@0.17.4 check .`, then
  `bin/agentflow validate`. Expected: all pass, `All checks passed!`, and `0 warnings`.

- [ ] **Step 5: Commit**: `git commit -m "Test that a new card's run ends with a PR that marks its item done, and release 3.10."`
