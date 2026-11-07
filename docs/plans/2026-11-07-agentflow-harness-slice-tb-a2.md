# Slice TB-A2: Recognising finished modules, with a reason, and marking one by hand — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A module finished before agentflow, or by a teammate, shows as Done in the Backlog with its reason, and the Backlog, `next` and `modules_done` stop offering it. A person can mark one done by hand, or veto a wrong Done.

**Architecture:**
- **The evidence:**
  - hand marks and vetoes
  - the spec and plan files on `refs/remotes/origin/<fork_from>`, read in one bounded `git cat-file --batch` stream per assessment
  - today's merged-branch rule with `gh`, asked last
- **Fail-closed reads:** when origin can't be read, or the listing is incomplete, origin evidence decides nothing, and earlier conclusions stay as they were.
- **Where detection runs:** inside the existing `assess_modules`, under the existing `queue_lock`, on the existing refresh and queue schedule. Never in `begin_start`.
- **Where state lives:** marks and recorded reasons live in a new `agentflow/finished.py`. It imports only stdlib and `agentflow.state`, and it reads `queue.json` itself, through the shared file name.
- **Not in this slice:** the fresh re-check before a start (spec B4) stays in PR B.

**Tech Stack:** Python 3.12, stdlib + `pyyaml`, pytest with the fake `gh` (`agentflow/tests/bin/gh`) and the fixture packs.

**Spec:** `docs/plans/2026-11-07-agentflow-harness-slice-tb.md`, section "PR A", item A3 and the Done half of the A tests.

Builds on slice TB-A1 (`docs/plans/2026-11-07-agentflow-harness-slice-tb-a1.md`), which supplies:
- `sources.spec_ids`, `sources.is_module_id`, `sources.spec_header`
- `LINE_MAX`, `HEADER_MAX`
- the `specs` work source
- TB-A1's reworded messages and its `config_edit` test style

Line numbers below are as of today. TB-A1 shifts some, so find each by its name.

## Global Constraints

- **Dependencies:** engine dependencies stay stdlib + `pyyaml>=6`. Add no third-party import.
- **Portability:** `agentflow/tests/test_portability.py` must pass. The engine holds no project strings.
- **Golden files:** `agentflow/tests/fixtures/explain/*.txt` stay byte-identical.
- **Config keys:** every new or changed config key ships with:
  - a Form field or a `_PROTECTED_KEYS` entry
  - `validate` checks
  - a `tests/test_config_edit.py` case
  - a playbook §14 line

  The new key, `project.docs.done_status`, sits under the already-protected `project`.
- **Engine defaults:** `agentflow/defaults/config.yaml` does not change.
- **What changes for `capability_map` packs**, the fixture packs among them. Task 4 records each of these:
  - **(a)** A hand mark makes a module done. A veto makes it not done by origin evidence.
  - **(b)** A spec header status on origin that says done makes the module done.
  - **(c)** A newest dated plan on origin makes the module done with no `gh` call, when all three hold:
    - it is fully ticked
    - it names the module's spec file on its `**Spec:**` line
    - the origin listing was complete
  - **(d)** `modules_done` counts as met every dependency the last assessment recorded as done, whatever the reason. It asks `gh` only for the rest.
  - **(e)** `agentflow next` skips done modules in `rule_spec_without_plan` and `rule_checked_plan`.
  - **(f)** A plan belongs to a module by its dated name and the longest known id.
  - **(g)** Done entries in `queue.json` gain `done_reason`.

  Nothing else changes. An existing `done` entry with no `done_reason` stays done without `gh`. With no `origin`, or with origin unreadable, detection is exactly today's.
- **Untrusted text:** origin text is untrusted.
  - Every line of it is cut to `sources.LINE_MAX` before any regex runs, the existing `parse_plan_text` included.
  - Refs are always given in full, as `refs/remotes/origin/<fork_from>`. This follows commit 486384b.
  - Reads are bounded by count, by bytes kept in memory, and by time.
- **Commit messages:** one imperative sentence, ending with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- **Test command shape:** `uv run --no-project --with pyyaml --with pytest pytest <file>[::<test>] --import-mode=prepend -q`.

## Review Focus

1. **Module ids that are prefixes of each other** (`user`, `user-auth`, and a `user-profile` that no source knows):
   - No other module's plan finishes `user` by origin evidence.
   - An undated `user.md` is never a plan.

   Test: Task 2.
2. **Unmerged work in the working tree.** A spec or plan finished only in the working tree, with an unmerged branch checked out, does not make the module done. Test: Task 2.
3. **Upgrading.** An existing `queue.json` whose done entries have no `done_reason` keeps them done, with no `gh` call, while `gh` is failing. Test: Task 2.
4. **Origin unreadable, or too big to list in full** (git fails or times out, no `origin`, over `BLOBS_MAX` files, over `BYTES_MAX`):
   - No module flips from done to Backlog.
   - No module becomes done from a partial listing.

   Test: Task 2.
5. **Corrupt or edited by hand.** A `done_marks.json` like that (torn, wrong shape, unreadable) never un-dones a hand-marked module. The CLI says how to repair it. Test: Task 3.

---

### Task 1: `gitops.blobs_at` reads one directory at a ref, bounded and streamed

**Files:**
- Modify: `agentflow/gitops.py`: add `Blob`, `Listing` and `blobs_at`. `files_at` stays until Task 2.
- Test: `agentflow/tests/test_gitops.py` (create it if absent)

**Interfaces:**
- **Produces:**
  - `gitops.Blob(text: str, truncated: bool)`
  - `gitops.Listing(blobs: dict[str, Blob], complete: bool)`
  - `gitops.BLOB_SKIP = 4 << 20`, `gitops.BLOBS_MAX = 500`, `gitops.BYTES_MAX = 16 << 20`, `gitops.GIT_READ_TIMEOUT_S = 60`
  - `gitops.blobs_at(repo: Path, ref: str, directory: str, keep: Callable[[str], bool], limit: int) -> Listing | None`
- **What `blobs_at` returns:**
  - `None` when it **could not read**: the ref is missing, git fails, or git times out.
  - An empty, complete `Listing` when the ref exists but the directory doesn't.
- **Listing:**
  1. Strip `/` from both ends of `directory`.
  2. Run `git ls-tree -z -l <ref> -- <directory>/` with `GIT_READ_TIMEOUT_S`.
  3. Each NUL-separated record is `<mode> <type> <sha> <size>\t<path>`. Split it on the TAB first; the size is space-padded.
  4. The key is the path's basename.
  5. Keep only modes `100644` and `100755` whose basename passes `keep` and has no `\n`.
- **Incomplete:** `complete` is False when any of these happens. Each such entry is left out, not stored as an empty blob:
  - more than `BLOBS_MAX` kept entries
  - an entry over `BLOB_SKIP`
  - the running total of distinct blob sizes passing `BYTES_MAX`
- **Reading:**
  - An empty SHA list skips `cat-file`.
  - Otherwise, start `subprocess.Popen(["git", "cat-file", "--batch"], stdin=PIPE, stdout=PIPE)`. Write the deduplicated SHAs, one per line, all at once; at most 500 × 41 bytes, well under one pipe buffer. Then close stdin.
  - Stream stdout. Each header line is `<sha> <type> <size>\n`, followed by exactly `size` bytes and one `\n`.
  - Keep the first `limit` bytes of each body and read the rest in 64 KiB chunks, discarding them. Memory per blob stays at most `limit`.
  - A `<sha> missing\n` line makes `complete` False.
  - Decode with `errors="replace"`. `truncated` means the body was cut.
  - The whole read is bounded by `GIT_READ_TIMEOUT_S`. On a timeout, kill the process and return `None`.

- [ ] **Step 1: Write the failing tests** on a real tmp git repo, using `conftest.run_git`.

```python
def test_blobs_at_reads_only_regular_files_that_pass_keep_by_basename(tmp_path): ...
    # d/a.md, d/b.txt, d/sub/c.md, symlink d/ln.md committed;
    # blobs_at(repo, "refs/heads/main", "d/", lambda n: n.endswith(".md"), 100).blobs.keys() == {"a.md"}, complete True

def test_blobs_at_cuts_large_bodies_and_leaves_out_huge_ones(tmp_path): ...
    # a 300-byte file, limit 100 → truncated True, len(text) == 100;
    # a file of BLOB_SKIP + 1 bytes → absent and complete False

def test_blobs_at_parses_bodies_with_newlines_and_duplicate_content(tmp_path): ...
    # two files, identical multi-line content → both keys, both texts equal the content

def test_blobs_at_decodes_non_utf8_with_replacement(tmp_path): ...

def test_blobs_at_over_the_count_or_byte_budget_is_incomplete(tmp_path, monkeypatch): ...
    # BLOBS_MAX = 3 with 5 kept files → complete False; BYTES_MAX = 10 with two 8-byte files → complete False

def test_blobs_at_on_a_missing_ref_is_none_and_on_a_missing_directory_is_empty(tmp_path): ...

def test_a_tag_named_like_a_remote_branch_does_not_shadow_the_full_ref(tmp_path): ...
    # tag "origin/main" at an old commit; blobs_at("refs/remotes/origin/main", ...) reads the newer file
```

- [ ] **Step 2: Run them and expect failures.** `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_gitops.py --import-mode=prepend -q` should FAIL.

- [ ] **Step 3: Implement `blobs_at` as specified in Interfaces.**

- [ ] **Step 4: Run them; they should PASS.**

- [ ] **Step 5: Commit**

```bash
git add agentflow/gitops.py agentflow/tests/test_gitops.py
git commit -m "Add a bounded, streamed reader for the files of one directory at a git ref"
```

---

### Task 2: Done detection names its reason, from hand marks, origin and merged branches

**Files:**
- Create: `agentflow/finished.py`
- Modify: `agentflow/config.py`: `DEFAULT_DONE_STATUS`, `Project.done_status`, the `project.docs` loader, `Validator.project`
- Modify: `agentflow/sources.py`: `cut_lines`, `says_done`, `plan_spec`
- Modify: `agentflow/module_queue.py`:
  - `QueueState.path` uses `finished.QUEUE_FILE`
  - `assess_modules`, `_assess`, `_plan_finished`, `cached_entries`
  - `rule_spec_without_plan`, `rule_checked_plan`
- Modify: `agentflow/conditions.py`: `_modules_done`
- Modify: `agentflow/gitops.py`: remove `files_at`, whose only caller is `_plan_finished`
- Modify: `agentflow/tests/conftest.py`: add a `land_on_origin` helper
- Modify: `docs/agentflow-playbook.md` §14: a `project.docs.done_status` line next to `spec_file`
- Test: `agentflow/tests/test_finished.py` (new), `agentflow/tests/test_next_queue.py`, `agentflow/tests/test_conditions.py`, `agentflow/tests/test_work_source.py`, `agentflow/tests/test_config_edit.py`

**Interfaces:**
- **Consumes:**
  - Task 1's `blobs_at` and `Listing`
  - from TB-A1: `sources.spec_ids`, `is_module_id`, `spec_header`, `LINE_MAX`, `HEADER_MAX`
  - `sources.is_plan_name` (:206), `module_queue.plan_module` (:93) and `_PLAN_NAME` (:46)
- **`config`:**
  - `DEFAULT_DONE_STATUS = r"^[-*>\s]*Status[\s:*]+(done|shipped|implemented)(?![\w-])"`, matched per line with `re.IGNORECASE`.
  - It matches `Status: done`, `**Status:** done`, `**Status: shipped** 2026-01-02`, `- **Status:** done` and `> Status: done`.
  - It does not match `Status: done-ish`, `Status: not done` or `Statusdone`.
  - `Project.done_status: str`.
- **`sources`:**
  - `cut_lines(text: str, limit: int = LINE_MAX) -> str`: every line cut to `limit`.
  - `says_done(text: str, pattern: str) -> bool | None`: whether some line of `spec_header(text)` matches; it reads the line half of each `(line, was_cut)` pair. Returns `None` when `pattern` doesn't compile.
  - `plan_spec(text: str) -> str | None`: the basename of the first backticked path on a header `**Spec:**` line (before the first `### Task`), or `None`.
- **`finished`:**
  - Constants:
    - `QUEUE_FILE = "queue.json"`, `MARKS = "done_marks.json"`, `LOCK = "done_marks.lock"`, all in `runs_root(repo)`, which is created when missing.
    - `BY_HAND = "done by hand"`, `SPEC_SAYS = "spec says done"`, `MERGED = "merged"`.
    - `plan_complete(branch) -> str`, giving `f"plan complete on {branch}"`.
  - `read_marks(repo) -> tuple[dict[str, dict], str | None]`:
    - Returns `({module: {"state": "done" | "not_done", "by", "at"}}, error)`.
    - A missing file gives `({}, None)`.
    - An OSError, invalid JSON, or the wrong shape gives `({}, f"{path} is unreadable ({why}); fix it or delete it, then mark again")`.
  - `mark(repo, module, by)` sets `state: done`. It returns None.
  - `veto(repo, module, by)` sets `state: not_done`. It returns None.
  - `unmark(repo, module) -> bool` removes the entry and returns whether one was there.
  - Locking: each call opens `LOCK` afresh and takes `fcntl.flock(fd, LOCK_EX)`. It then reads, changes, and atomically replaces `MARKS`. If `read_marks` reported an error, it raises `ValueError(error)` instead.
  - `recorded(repo) -> dict[str, str]`: `{module: reason}`. It is built in four steps:
    1. Read `QUEUE_FILE` with `json`; a broken file counts as `{}`.
    2. Keep its done entries, reading a missing reason as `MERGED`.
    3. Drop a `BY_HAND` entry with no live done mark, and drop any entry with a live veto.
    4. Add every live done mark as `BY_HAND`.

    If `read_marks` errs, the `BY_HAND` entries from step 2 are kept, so the result fails closed.
- **`module_queue`:**
  - `OriginView(specs: Listing | None, plans: Listing | None, branch: str)`.
  - `origin_view(cfg, repo) -> OriginView`: `blobs_at` at `refs/remotes/origin/<fork_from>`.
    - For `docs.specs`, keep the names `spec_ids` finds whose ids pass `is_module_id(id, spec_file)`, with limit `HEADER_MAX`.
    - For `docs.plans`, keep the names with `_PLAN_NAME.fullmatch`, with limit `1 << 20`.
    - It is built once per `assess_modules`.
  - `known_ids(cfg, repo, view) -> set[str]`: the work source's ids, plus the ids of `view.specs` when it is a `Listing`.
  - `own_plans(names, module, known) -> list[str]`: the sorted names `n` for which both `sources.is_plan_name(n, module)` and `plan_module(Path(n), sorted(known | {module})) == module` hold.
  - `done_reason(cfg, repo, node, run, old, view, known, marks) -> tuple[str | None, str | None]`: returns `(reason, problem)`.
  - A done entry in `queue.json` or `cached_entries` carries `"done_reason"`. Other entries don't.

- [ ] **Step 1: Write the failing tests.**
  - **`test_finished.py`:**
    - `mark`, `veto` and `unmark` round-trip.
    - Two threads marking different modules both land.
    - For each corrupt form (torn JSON, a list, an unreadable file), `read_marks` gives the error text naming the path, and `mark` raises.
    - `recorded` covers each case: merges, a `BY_HAND` entry dropped without its mark, one kept when the marks file errs, a veto drops an entry, and a missing reason counts as `MERGED`.
  - **`conftest.py`:** add `land_on_origin(flow_repo, tmp_path: Path, files: dict[str, str], fetch: bool = True) -> None`. It does what `test_backlog.py:_land_on_origin` does, with two changes:
    - It clones into a fresh `tempfile.mkdtemp(dir=tmp_path)`, so it can be called more than once.
    - With `fetch`, it then runs `git fetch origin` in the checkout.

    `test_backlog.py` keeps its own helper.
  - **`test_next_queue.py`:** on the python pack (`capability_map`, `fork_from: next`), with `branch_is_merged` monkeypatched to count calls and to return a value the test sets. A plan "naming its spec" has the header line `` **Spec:** `docs/specs/SPEC-<id>.md` ``.

```python
def test_a_module_done_by_hand_is_done_without_asking_gh(...):            # BY_HAND, 0 gh calls
def test_a_veto_stops_origin_evidence_but_not_a_merged_branch(...):        # veto + "Status: done" on origin → backlog; veto + merged → MERGED
def test_a_spec_on_origin_whose_header_says_done_is_done(...):             # "**Status:** done" → SPEC_SAYS
@pytest.mark.parametrize("line", ["Status: shipped", "**Status: implemented** 2026-01-02", "- **Status:** done", "> Status: done"])
def test_each_status_form_counts(line, ...): ...
@pytest.mark.parametrize("line", ["Status: done-ish", "Status: not done", "Statusdone"])
def test_lookalike_status_lines_do_not_count(line, ...): ...
def test_a_done_status_in_a_fence_or_below_the_first_section_does_not_count(...):
def test_a_bad_done_status_pattern_is_a_problem_not_a_crash(...):          # done_status "(" set after load → entry has a problem naming the key
def test_a_spec_or_plan_finished_only_in_the_work_tree_does_not_count(...):          # Review Focus 2
def test_the_newest_dated_plan_ticked_on_origin_and_naming_its_spec_is_done_without_gh(...):  # plan_complete("next"), 0 gh calls
def test_a_ticked_plan_that_does_not_name_the_modules_spec_needs_a_merged_branch(...):  # no **Spec:** → asks gh
def test_an_older_ticked_plan_does_not_finish_a_module_whose_newer_plan_is_open(...):
def test_an_undated_plan_file_is_never_a_plan(...):                        # plans/user.md ticked → backlog
def test_a_truncated_plan_on_origin_is_not_finished(...):
def test_a_long_heading_line_in_an_origin_plan_is_cut_before_parsing(...): # plans text with a 1 MB "### Task 1:" line → every line parsed ≤ LINE_MAX
def test_no_other_modules_plan_finishes_a_prefix_module(...):              # Review Focus 1: work source has user only; ticked
                                                                           # 2026-01-01-user-auth.md (Spec: SPEC-user-auth.md) and
                                                                           # 2026-01-02-user-profile.md (Spec: SPEC-user-profile.md) on origin → user backlog
def test_an_old_done_entry_without_a_reason_stays_done_while_gh_fails(...):       # Review Focus 3
def test_origin_unreadable_or_incomplete_keeps_earlier_conclusions(...):   # Review Focus 4: assess with SPEC_SAYS done;
                                                                           # then blobs_at → None (and separately complete=False) → still SPEC_SAYS;
                                                                           # a ticked plan in an incomplete listing → not plan_complete
def test_a_merged_branch_with_a_finished_plan_is_still_done_as_merged(...):
def test_a_hand_marked_done_does_not_stick_as_merged_after_unmark(...):
def test_next_skips_done_modules_in_both_spec_and_checked_plan_rules(...): # rule_spec_without_plan, rule_checked_plan
```

  - **`test_conditions.py`:**
    - `modules_done` holds for dependencies `finished.recorded` names, with `gh` failing (`FAKE_GH_FAIL`), and makes no `gh` call when all of them are recorded.
    - With one recorded and one not, it asks `gh` for the other only.
  - **`test_work_source.py`:** `project.docs.done_status: "("` is a validate error naming that key.
  - **`test_config_edit.py`:** `propose(path, base, text=<the pack with project.docs.done_status changed>)` raises `Refused` matching `"protected"`. This is the YAML-text path; the Form path refuses unknown keys with different words.

- [ ] **Step 2: Run them and expect failures.** `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_finished.py agentflow/tests/test_next_queue.py agentflow/tests/test_conditions.py agentflow/tests/test_work_source.py agentflow/tests/test_config_edit.py --import-mode=prepend -q` should FAIL.

- [ ] **Step 3: Implement.**

  `done_reason` returns the first rule that holds:
  1. **`BY_HAND`:** a live done mark.
  2. **`SPEC_SAYS`:** no live veto, `view.specs` is a `Listing`, and `says_done(view.specs.blobs[spec_file.format(module=id)].text, done_status)` is True. A `None` from `says_done` makes `problem = "project.docs.done_status is not a valid regex"`.
  3. **`plan_complete(fork_from)`:** all of these hold:
     - there's no live veto
     - both listings are `complete`
     - the **newest** of `own_plans(view.plans.blobs, id, known)` is not truncated
     - that plan's `parse_plan_text(cut_lines(text))` is finished (tasks exist and none is open)
     - its `plan_spec` equals `spec_file.format(module=id)`
  4. **`MERGED`:** today's rule, on the **newest** own plan, read from the work-tree plans dir (`own_plans` over its file names) or from `view` when listed.
     - It applies when the plan is finished and `branch_is_merged` holds.
     - A prior done entry whose `done_reason` is `MERGED`, or missing, skips `gh`. This matches today's rule.
  5. **Unreadable or incomplete origin:** when `view.specs`/`view.plans` is `None` or incomplete, a prior entry done by `SPEC_SAYS` or `plan_complete` stays done with its reason.

  A `gh` "can't tell" is the problem, as it is today.

  Then wire it in:
  - **`_assess`:** a run's own status wins over a gone `BY_HAND` mark, as in today's code path.
  - **`_plan_finished`** takes `view`/`known` in place of `files_at`. Delete `files_at`.
  - **`cached_entries`:** applies live marks (done → `BY_HAND`, veto → not done) on top of the prior entries. When `read_marks` errs, it keeps prior `BY_HAND` entries.
  - **`rule_spec_without_plan` and `rule_checked_plan`** skip `finished.recorded(repo)`.
  - **`_modules_done`:**
    1. Take `missing = [d for d in node.deps if d not in finished.recorded(ctx.repo)]`.
    2. Return True if it is empty.
    3. Otherwise ask `_merged_heads` for those only.
  - **Imports:** import `finished` with `from agentflow import finished`. `module_queue.py:~553` already has a local named `done`.
  - **`validate`** compiles `done_status`, and reports errors at `project.docs.done_status`.

- [ ] **Step 4: Run them; they should PASS.** Then run `test_backlog.py`, `test_ready.py`, `test_dashboard.py`, `test_gitops.py` and `test_explain_golden.py`. They should PASS. `test_next_queue.py:278`'s exact entry dict doesn't change, because `done_reason` is only on done entries.

- [ ] **Step 5: Add the playbook §14 line for `project.docs.done_status`, then commit.** The line covers:
  - the default
  - header lines only, matched case-insensitively
  - the forms it accepts
  - that it is read from `origin/<fork_from>`
  - that a veto overrides it
  - that it is protected

```bash
git add agentflow/finished.py agentflow/config.py agentflow/sources.py agentflow/module_queue.py agentflow/conditions.py agentflow/gitops.py agentflow/tests/ docs/agentflow-playbook.md
git commit -m "Recognise finished modules from the base branch on origin or a hand mark, record the reason, and fail closed when origin cannot be read"
```

---

### Task 3: The Backlog shows Done modules, and `agentflow backlog done|undone` marks or vetoes one

**Files:**
- Modify: `agentflow/backlog.py`: `snapshot`
- Modify: `agentflow/views.py`: `board`, `board_table`, `_backlog_text`
- Modify: `agentflow/dashboard.html`: the Backlog column
- Modify: `agentflow/flow.py`: a `backlog` subparser with `done <module>` and `undone <module>`, each with the `--by` and `--config` flags the other subcommands declare
- Modify: `docs/agentflow-playbook.md` §10 cheat sheet
- Test: `agentflow/tests/test_backlog.py`, `agentflow/tests/test_dashboard.py`

**Interfaces:**
- **Consumes:** Task 2's `finished.mark`, `veto`, `unmark` and `read_marks`, and its done entries.
- **`backlog.snapshot`** gains:
  - `"done": list[{"key": "module:<id>", "id": str, "reason": str}]`, in work-source order. `reason` is `entry.get("done_reason") or finished.MERGED`.
  - `"done_note": str`: the error from `read_marks`, or `""`.
- **`views.board`** returns `"backlog_done"` and `"done_note"`. `backlog_done` is `[]` when `lane` is given and isn't the `module_lane`.
- **`board_table`** reads these keys with `.get`.
- **`agentflow backlog done <module>`:**
  - On success it prints `<module> marked done by hand` and exits 0.
  - It exits 1 when the module isn't in the work source ("not a module").
  - It exits 1 when the module has a live run, meaning `module_queue.feature_runs` has one whose `state["status"]` is in `module_queue._LIVE`. The message names that run.
  - It exits 1 with the repair text when `done_marks.json` is unreadable.
- **`agentflow backlog undone <module>`:**
  - If there is a hand mark, it removes it, prints `<module> hand mark removed` and exits 0.
  - Otherwise it records a veto, prints `<module> marked not done by hand: origin evidence is ignored` and exits 0.
- **After either command:** both re-assess under `module_queue.queue_lock`. If the lock is busy, they keep the change and print `the queue is running; the Backlog shows it on its next refresh`.

- [ ] **Step 1: Write the failing tests** in `test_backlog.py`. Use the `bk` `Board`, which has modules `a` and `b` in a capability map. Call the CLI through `flow.main([...])`, in the style of `test_new_module.py:148`.
  - `finished.mark(root, "b", "cli")` with **no** `refresh()`: `snapshot["done"]` holds `{"key": "module:b", "id": "b", "reason": "done by hand"}`, and `"module:b"` isn't in `cards`. After `unmark`, `b` is a card again.
  - A corrupt `done_marks.json` gives a non-empty `done_note`. A module marked earlier stays done.
  - `main(["backlog", "done", "ghost"]) == 1` and the output contains "not a module".
  - With a live run for `b` (`Spawner`), `main(["backlog", "done", "b"]) == 1`, naming the run. After that run is cancelled, it is `0`.
  - `main(["backlog", "done", "b", "--by", "pat"]) == 0`, and `read_marks` shows `by == "pat"`.
  - `undone b` removes the mark. A second `undone b` records a veto.
  - With `module_queue.queue_lock` held by the test, `backlog done b` exits 0 and prints the queue-running line.

  In `test_dashboard.py`:
  - `GET /api/board` has `backlog_done` and `done_note`.
  - With `?lane=bug`, `backlog_done == []`.

- [ ] **Step 2: Run them and expect failures.** `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_backlog.py agentflow/tests/test_dashboard.py -k "done" --import-mode=prepend -q` should FAIL.

- [ ] **Step 3: Implement.**
  - **`dashboard.html`:**
    - Render `backlog_done` under the Backlog cards as `<details>` "Done (n)", one line per module with its reason and no Start button.
    - A `toggle` handler saves the open state in the page's `ui` state, and `renderBoard` sets `open` from it on every SSE re-render.
    - A non-empty `done_note` shows as a warning line.
    - All text goes through the page's escaping helper.
  - **`board_table`** prints `done: b (done by hand), …`.

- [ ] **Step 4: Run them; they should PASS.** Then run `test_portability.py`. It should PASS too.

- [ ] **Step 5: Add `agentflow backlog done|undone <module>` to the playbook §10 cheat sheet, then commit.**

```bash
git add agentflow/backlog.py agentflow/views.py agentflow/dashboard.html agentflow/flow.py agentflow/tests/ docs/agentflow-playbook.md
git commit -m "Show finished modules with their reason in the Backlog, and add agentflow backlog done and undone"
```

---

### Task 4: The as-built record, and this repository's check (interactive session only)

**Files:**
- Modify: `docs/agentflow-harness-proposal.md` (§1d "Team backlog", from TB-A1, and §5)

- [ ] **Step 1: Record the as-built deviations in proposal §1d and §5.**
  1. Hand marks and vetoes live in `runs/done_marks.json` (`agentflow/finished.py`), not in `backlog.jsonl`. `agentflow backlog undone` removes a mark, or vetoes origin evidence.
  2. Finished work is read from `refs/remotes/origin/<fork_from>`, the branch pull requests merge into, not from `origin/<base_branch>` or from the work tree.
     - A repository with no `origin` keeps today's detection: merged branch and hand marks.
     - Unpushed commits on `fork_from` don't count.
  3. The cheap reasons come before `gh`. The (a) to (g) list of Global Constraints applies to `capability_map` packs too.
  4. A plan belongs to a module by its dated name, the longest known id, and, for the no-`gh` rule, its `**Spec:**` line. Other `next` rules still use `find_plan`.
  5. The reason words are `done by hand`, `spec says done`, `plan complete on <branch>` and `merged`. They replace the spec's "merged PR" and "plan complete".
  6. The default `done_status` accepts bold, list and quote forms and matches header lines only. It still accepts a trailing qualifier such as `Status: done pending review`; a veto fixes such a case.
  7. `modules_done` and `agentflow next` read what the last assessment recorded, and that is refreshed every 60 s by the dashboard or each queue tick. With neither running, only the hand marks are current.
  8. In A, the module list is still read from the work tree, and the fresh re-check before a start (spec B4) isn't built. Both move to PR B.
  9. An approved spec with no plan, finished before agentflow, needs `agentflow backlog done`.

- [ ] **Step 2: Run the full suite and the checks.**
  - The full suite: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests --import-mode=prepend -q`. All should pass.
  - The golden files: `git diff --stat agentflow/tests/fixtures/explain/` should be empty.
  - Manual: `agentflow backlog done harness` shows it under "Done (1)" in `bin/agentflow dashboard` with "done by hand". `agentflow backlog undone harness` brings it back.

- [ ] **Step 3: Commit**

```bash
git add docs/agentflow-harness-proposal.md
git commit -m "Record slice TB-A2 as built"
```
