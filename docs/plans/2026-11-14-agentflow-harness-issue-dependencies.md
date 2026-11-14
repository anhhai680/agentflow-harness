# Issue dependencies on the Backlog Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A synced issue that GitHub marks *blocked by* an unmet issue shows `Blocked` with `Runs after:` on the
Backlog and cannot start (409), and an issue whose dependencies cannot be read never shows `Ready`.

**Architecture:** The issue sync asks `gh` for `blockedBy,blocking` and keeps each blocker as a bare
`{number, state, repo}` ref, with a `deps_complete` flag. `module_queue.dependency_view` gains `issue:` keys (the
board's issue cards) and `issue:` targets (any blocker), so the existing `blocked_by`/`parallel_with` logic and the
existing 409 in `begin_start` cover issues. `snapshot` adds `blocks`, the unknown and cycle notes, and
`run_start`'s live `gh issue view` recheck reads `blockedBy` too.

**Tech Stack:** Python 3.12 stdlib, pytest, the fake `gh` in `agentflow/tests/bin/gh`, vanilla JS in `dashboard.html`.

**Spec:** `docs/specs/agentflow-issue-dependencies-proposal.md` (approved 2026-11-14). No file is on the security
overlay's paths. `_parse_refs` and `_cached_refs` parse untrusted input (a `gh` reply, a cache file): apply the
`security-and-hardening` input rules there (types, bounds, URL shape, no text kept).

## Global Constraints

- Engine dependencies stay stdlib + `pyyaml>=6`; `test_portability.DENYLIST` applies to every engine file touched.
- No new config key. The feature is on whenever `issues.sync_label` is set.
- Never put an issue title, body, label or URL of a *blocker* into an argv, a module id, a map row, an event or a
  refusal. Blocker text is only `issue #N (<state>)` or `owner/repo#N (open)`.
- Never treat a missing, malformed, truncated or unreadable dependency list as "no blockers".
- `parent`, `subIssues` and `relatesTo` never order work. Issue bodies are never fetched.
- `snapshot` and `_picture` call no `gh` and no git.
- `dependency_view(...)` without `issues` returns exactly what it returns today.
- The golden files `agentflow/tests/fixtures/explain/*.txt` stay byte-identical.
- Tests: `uv run --no-project --with pyyaml --with pytest pytest <files> --import-mode=prepend -q`. Never touch `uv.lock`.
- Every commit message is one imperative sentence ending with
  `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

1. **A blocker node whose `url` is missing, or names another number.** Expect the entry to read as
   `deps_complete: false` (Dependencies unknown, Start refused), not as one blocker fewer (Task 1,
   `test_an_unreadable_blocker_node_makes_the_list_unknown`).
2. **A cross-repository blocker with the same number as a local issue whose PR merged here.** Expect it to keep
   blocking while open (Task 2, `test_a_cross_repo_blocker_never_matches_a_local_run`).
3. **A blocker whose own card left the board because its run is live.** Expect `Runs after: issue #1 (running)`, not
   `open (not on this board)` (Task 3, `test_a_blocker_with_a_live_run_reads_running`).
4. **A dashboard opened before the first refresh after upgrading** (the cache has no `blocked_by`). Expect
   `Dependencies unknown: refresh the board to read them` and Start refused, then normal after one refresh (Task 1,
   `test_a_cache_from_before_the_change_reads_as_unknown_until_refreshed`).
5. **A blocker closed on GitHub, gone from the synced list, whose run here is `done` with its PR open.** Expect the
   dependent to stay blocked until `merged[run_id]` is true (Task 2,
   `test_a_closed_blocker_is_met_only_when_its_open_run_merged`).

---

### Task 1: Read the relationships in the sync and the cache

**Files:**
- Modify: `agentflow/backlog.py` (`ISSUE_LIST_FIELDS` ~61, `_parse_issues` ~432, `sync_issues` ~473, `_synced` ~497)
- Modify: `agentflow/tests/bin/gh` (the `issue list` branch ~89, plus the docstring)
- Test: `agentflow/tests/test_backlog.py` (the synced-issues section ~776), `agentflow/tests/test_dashboard.py`
  (`idash` fixture ~979: add empty `blockedBy`/`blocking` to both issues)

**Interfaces:**
- Produces, in `agentflow/backlog.py`:
  - `ISSUE_LIST_FIELDS = "number,title,labels,url,blockedBy,blocking"`
  - `ISSUE_LIST_FIELDS_OLD = "number,title,labels,url"`
  - `REFS_MAX = 100` (more nodes than this, or `totalCount` above the nodes kept, is incomplete)
  - `ISSUE_REF_URL = re.compile(r"https://[A-Za-z0-9.-]+(?::\d{1,5})?/([\w.-]+/[\w.-]+)/issues/(\d+)", re.ASCII)`
  - `UNKNOWN_OLD_GH = "update gh"`, `UNKNOWN_CACHE = "refresh the board to read them"`,
    `UNKNOWN_TRUNCATED = "the list is longer than GitHub returned"`, `UNKNOWN_BAD = "a blocker could not be read"`
  - `def _here(url: str) -> str | None`: the `owner/repo` of a synced issue's own URL (via `ISSUE_REF_URL`), else None
  - `def _parse_refs(field: Any, here: str | None) -> tuple[list[dict], str]`: a `blockedBy`/`blocking` value
    as refs `{"number": str, "state": "open" | "closed", "repo": str | None}` and the unknown reason (`""` when
    complete). `repo` is None when the node's URL repo equals `here` (compare case-insensitively), else that
    `owner/repo`. A `field` that is not `{"nodes": list, "totalCount": int}`, a node with a bad number, a bad URL
    or a URL number that differs → `UNKNOWN_BAD` (the good nodes are still returned). The node's `title` is
    never read.
  - `def _cached_refs(value: Any) -> list[dict] | None`: a cached ref list re-validated (each ref exactly the
    shape above: `number` passes `intake.issue_number`, `state` in the two words, `repo` None or a fullmatch of
    `[\w.-]+/[\w.-]+` with `re.ASCII`); None when anything is off.
  - Each `_parse_issues` / `_synced` entry gains `blocked_by: list[ref]`, `blocks: list[ref]`,
    `deps_complete: bool`, `deps_note: str` (`""` or one of the `UNKNOWN_*` words).
  - `_parse_issues(cfg, reply, *, with_deps: bool = True)`: with `with_deps=False` every entry is
    `deps_complete: False`, `deps_note: UNKNOWN_OLD_GH`, empty lists.

- [ ] **Step 1: Write the failing tests** in `test_backlog.py`. Extend the helper to
  `_issue(number, title=..., labels=..., url=None, blocked_by=(), blocking=(), total=None)`, which writes
  `"blockedBy": {"nodes": [...], "totalCount": total if total is not None else len(nodes)}` (same for `blocking`), and add
  `_node(number, state="OPEN", repo="example/repo", title="<b>hostile</b>")` →
  `{"id": f"I_{number}", "number": number, "state": state, "title": title, "url": f"https://github.com/{repo}/issues/{number}"}`.
  Update the existing argv assertion in
  `test_a_refresh_lists_the_labelled_open_issues_and_the_board_shows_them_without_gh` to the new field list.

```python
def test_the_sync_asks_for_blocked_by_and_keeps_bare_refs(ibk, monkeypatch):
    _list_issues(monkeypatch, _issue(2, blocked_by=[_node(1), _node(9, "CLOSED"), _node(1, repo="other/lib")],
                                     blocking=[_node(3)]))
    ibk.refresh()
    assert "number,title,labels,url,blockedBy,blocking" in _gh_calls(ibk)[-1]
    entry = backlog._synced(ibk.cfg, backlog._load_state(ibk.root))[0]
    assert entry["blocked_by"] == [{"number": "1", "state": "open", "repo": None},
                                   {"number": "9", "state": "closed", "repo": None},
                                   {"number": "1", "state": "open", "repo": "other/lib"}]
    assert entry["blocks"] == [{"number": "3", "state": "open", "repo": None}]
    assert entry["deps_complete"] is True and "hostile" not in json.dumps(entry)

@pytest.mark.parametrize("node", [
    {"number": "1", "url": "https://github.com/example/repo/issues/1"},           # number is a string
    {"number": True, "url": "https://github.com/example/repo/issues/1"},
    {"number": 10**30, "url": "https://github.com/example/repo/issues/1"},
    {"number": 1, "url": "https://github.com/example/repo/issues/2"},            # url names another issue
    {"number": 1},                                                               # no url
    {"number": {"n": 1}, "url": "https://github.com/example/repo/issues/1"},
])
def test_an_unreadable_blocker_node_makes_the_list_unknown(ibk, monkeypatch, node):
    _list_issues(monkeypatch, {**_issue(2), "blockedBy": {"nodes": [node], "totalCount": 1}})
    ibk.refresh()
    entry = backlog._synced(ibk.cfg, backlog._load_state(ibk.root))[0]
    assert entry["deps_complete"] is False and entry["deps_note"] == backlog.UNKNOWN_BAD

def test_a_missing_state_reads_open(ibk, monkeypatch): ...   # node without "state" → state "open"
def test_a_truncated_list_is_unknown(ibk, monkeypatch): ...  # _issue(2, blocked_by=[_node(1)], total=5) → UNKNOWN_TRUNCATED
def test_relationships_that_do_not_order_work_change_nothing(ibk, monkeypatch): ...
    # an entry with "parent", "subIssues", "relatesTo" filled: same _synced entry as without them

def test_an_old_gh_is_asked_again_without_the_dependency_fields(ibk, monkeypatch):
    monkeypatch.setenv("FAKE_GH_OLD", "1")
    _list_issues(monkeypatch, _issue(2))
    note = ibk.refresh()["issues_note"]
    lists = [c for c in _gh_calls(ibk) if c[:2] == ["issue", "list"]]
    assert [c[c.index("--json") + 1] for c in lists] == [backlog.ISSUE_LIST_FIELDS, backlog.ISSUE_LIST_FIELDS_OLD]
    assert "update gh" in note
    entry = backlog._synced(ibk.cfg, backlog._load_state(ibk.root))[0]
    assert entry["deps_complete"] is False and entry["deps_note"] == "update gh"

def test_a_cache_from_before_the_change_reads_as_unknown_until_refreshed(ibk, monkeypatch):
    _list_issues(monkeypatch, _issue(2))
    ibk.refresh()
    state = backlog._load_state(ibk.root)
    backlog._save_state(ibk.root, issues=[{k: v for k, v in e.items() if k not in ("blocked_by", "blocks", "deps_complete", "deps_note")}
                                         for e in state["issues"]])
    entry = backlog._synced(ibk.cfg, backlog._load_state(ibk.root))[0]
    assert entry["deps_complete"] is False and entry["deps_note"] == backlog.UNKNOWN_CACHE
    ibk.refresh()
    assert backlog._synced(ibk.cfg, backlog._load_state(ibk.root))[0]["deps_complete"] is True

@pytest.mark.parametrize("bad", [[{"number": "x", "state": "open", "repo": None}], [{"number": "1", "state": "OPEN", "repo": None}],
                                 [{"number": "1", "state": "open", "repo": "a b/c"}], "nope", [[]]])
def test_a_tampered_cached_ref_reads_as_unknown(ibk, monkeypatch, bad): ...  # → deps_note UNKNOWN_CACHE
```

- [ ] **Step 2: Run them and see them fail**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_backlog.py -k "blocked_by or blocker or truncated or relationships or old_gh or cache_from or cached_ref or missing_state or labelled_open" --import-mode=prepend -q`
Expected: FAIL (`KeyError: 'blocked_by'`, the argv mismatch, no `FAKE_GH_OLD` behaviour).

- [ ] **Step 3: Teach the fake `gh` two modes** (document both in its docstring): with `$FAKE_GH_OLD` set, an
  `issue list` or `issue view` whose `--json` value contains `blockedBy` writes `Unknown JSON field: "blockedBy"` to
  stderr and exits 1 (after logging the call). `issue list --json` as the last argument prints
  `$FAKE_GH_JSON_FIELDS` (default: the gh 2.103 list, one field per line under
  `Specify one or more comma-separated fields for `--json`:`) and exits 0.

- [ ] **Step 4: Implement** the interfaces above. `sync_issues` catches the first `GhError` whose text contains
  `unknown json field` (case-insensitive), asks once more with `ISSUE_LIST_FIELDS_OLD` and parses with
  `with_deps=False`; its note becomes `"issue dependencies not read: update gh"` (joined with `"; "` to the
  `ISSUES_MAX` note when both apply). `_synced` sets `blocked_by`/`blocks` from `_cached_refs` and, when either is None
  or `deps_complete` is not `True`, sets `deps_complete: False` with the cached `deps_note` if it is one of the
  `UNKNOWN_*` words, else `UNKNOWN_CACHE`. Update the `idash` fixture's two issues with empty `blockedBy`/`blocking`.

- [ ] **Step 5: Run the synced-issue tests and the dashboard issue tests**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_backlog.py agentflow/tests/test_dashboard.py -k "issue or blocker or old_gh or cache" --import-mode=prepend -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add agentflow/backlog.py agentflow/tests/bin/gh agentflow/tests/test_backlog.py agentflow/tests/test_dashboard.py
git commit -m "Read each synced issue's blocked-by and blocking relationships as bare refs, and mark unreadable ones unknown." -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Issues in the dependency graph

**Files:**
- Modify: `agentflow/module_queue.py` (`_map_problem` ~580 for the ring helper; `dependency_view` ~733-846)
- Test: `agentflow/tests/test_backlog.py` (beside `test_dependency_view_follows_dependencies_transitively_for_parallel_with` ~537)

**Interfaces:**
- Consumes: the ref shape and entry fields from Task 1.
- Produces, in `agentflow/module_queue.py`:
  - `def issue_key(ref: dict) -> str`: `issue:<N>`, or `issue:<owner/repo>#<N>` when `ref["repo"]`.
  - `def issue_run(number: str, runs: dict[str, Run]) -> Run | None`: the newest (last in `runs`' order; `list_runs`
    is oldest first) run with `str(params.issue) == number`.
  - `def issue_ref_done(ref: dict, runs: dict[str, Run], merged: dict[str, bool | None]) -> bool`, exactly:
    ```python
    if ref["state"] != "closed":
        return False
    if ref["repo"] is not None:
        return True
    run = issue_run(ref["number"], runs)
    return run is None or run.state["status"] not in (*_LIVE, "done") or merged.get(run.id) is True
    ```
  - `def _ring(cycle: list[str]) -> list[str]`: the rotation `_map_problem` does today (members from the smallest),
    which `_map_problem` now calls.
  - `dependency_view(..., issues: list[dict] | None = None)`. Each issue dict:
    `{"number": str, "blocked_by": list[ref], "deps_complete": bool, "on_board": bool, "starting": bool}`.
    Keys: `issue:<N>` for each `on_board` entry. Deps: `[issue_key(r) for r in blocked_by]`, deduplicated in order.
    A form card whose `params.issue` (`getattr(c, "params", None) or {}`) names a synced entry gets that entry's blocker
    keys appended to its deps. Each `issue:` key's result has the three usual fields plus `"cycle": str | None`
    (`"dependency cycle: #3 -> #5 -> #3"`, from `_cycle_through` and `_ring`, labels `#N`); module and card results
    are unchanged.
  - `is_done` for an `issue:` target: `issue_ref_done` with its state (the state is `open` if any synced entry's ref
    to it says open; a board issue is always open). `label`: `issue #N`, or `owner/repo#N`. `state_word`: the
    `_RUN_WORDS` word of `issue_run` when there is one (local only), else `starting` / `in Backlog` for a board issue,
    `open (not on this board)` for an open local target, `open` for a cross-repo one.
  - `running(issue:N)` is `entry["starting"]` or a live `issue_run`; `started(issue:N)` is `entry["starting"]`.

- [ ] **Step 1: Write the failing tests** (pure: build `ModuleNode`s, `entries`, cards `[]`, and runs as
  `SimpleNamespace(id=..., state={"status": ..., "params": {"issue": "1"}})`; helpers
  `_ref(n, state="open", repo=None)` → `{"number": n, "state": state, "repo": repo}` and
  `_iss(n, *blockers, on_board=True, complete=True)` → `{"number": n, "blocked_by": list(blockers),
  "deps_complete": complete, "on_board": on_board, "starting": False}`):

```python
def test_dependency_view_without_issues_is_unchanged(): ...
    # the existing modules a,b,c,d case: dependency_view(nodes, entries, []) == dependency_view(..., issues=None) == (..., issues=[])

def test_an_issue_blocked_by_an_open_issue_is_blocked_and_unrelated_ones_are_parallel():
    view = module_queue.dependency_view([ModuleNode("m", [])], [{"id": "m", "status": "backlog", "run_id": None}], [],
                                        issues=[_iss("1"), _iss("2", _ref("1")), _iss("3")])
    assert view["issue:2"]["blocked_by"] == [{"key": "issue:1", "text": "issue #1 (in Backlog)"}]
    assert sorted(view["issue:1"]["parallel_with"]) == ["issue:3", "module:m"]
    assert "issue:1" not in view["issue:2"]["parallel_with"] and view["issue:3"]["parallel"] is True

@pytest.mark.parametrize("status, merged, free", [("running", None, False), ("done", None, False), ("done", True, True),
                                                  ("failed", None, True), ("cancelled", None, True)])
def test_a_closed_blocker_is_met_only_when_its_open_run_merged(status, merged, free): ...
    # _iss("2", _ref("1", "closed")), runs {"r1": status, params.issue "1"}, merged {"r1": merged}
    # → bool(view["issue:2"]["blocked_by"]) is (not free)

def test_an_open_blocker_holds_even_when_its_run_merged(): ...          # ref open, run done + merged True → still blocked
def test_a_cross_repo_blocker_never_matches_a_local_run(): ...
    # _iss("2", _ref("1", repo="other/lib")), local run for issue 1 done+merged → blocked, text "other/lib#1 (open)"; closed → free
def test_a_blocker_off_the_board_is_a_target_not_a_peer(): ...
    # _iss("2", _ref("7")) with no entry 7 → text "issue #7 (open (not on this board))"; "issue:7" in no parallel_with and not a key
def test_a_blocker_with_a_live_run_reads_running(): ...                 # entry 1 on_board=False, run for 1 "running" → "issue #1 (running)"
def test_an_issue_cycle_is_named_and_does_not_loop(): ...
    # 3 blocked by 5, 5 blocked by 3 → both blocked, both cycle == "dependency cycle: #3 -> #5 -> #3"
def test_a_board_issue_beside_a_running_module_is_parallel(): ...       # module m with a running run; issue:1 parallel True (no StopIteration)
def test_a_form_card_carrying_an_issue_inherits_its_blockers(): ...
    # card SimpleNamespace(id="c1", label="x", status="backlog", run_id=None, after=[], deps=[], params={"issue": "2"}),
    # issues=[_iss("2", _ref("1"), on_board=False)] → view["card:c1"]["blocked_by"][0]["key"] == "issue:1"
```

  Also add `test_map_problem_names_a_cycle_as_before` only if no existing test pins the `_map_problem` ring text
  (grep `dependency cycle` in `agentflow/tests` first).

- [ ] **Step 2: Run them and see them fail**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_backlog.py -k "dependency_view or issue_blocked or closed_blocker or open_blocker or cross_repo or off_the_board or live_run_reads or issue_cycle or beside_a_running or form_card_carrying" --import-mode=prepend -q`
Expected: FAIL (`unexpected keyword argument 'issues'`).

- [ ] **Step 3: Implement** the interfaces in `module_queue.py`. Keep the existing body's order of `keys`
  (modules, then cards, then board issues) so `parallel_with` lists stay in a stable order.

- [ ] **Step 4: Run the whole backlog file** (the view is shared by modules and cards)

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_backlog.py --import-mode=prepend -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add agentflow/module_queue.py agentflow/tests/test_backlog.py
git commit -m "Add synced issues and their blockers to the Backlog dependency view." -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Show and refuse on the board

**Files:**
- Modify: `agentflow/backlog.py` (`_picture` ~584, `snapshot` ~629-706, `begin_start` ~810-867, `_recheck_after` ~953)
- Test: `agentflow/tests/test_backlog.py`

**Interfaces:**
- Consumes: Task 1 entries; Task 2 `dependency_view(issues=)`, `issue_key`, `issue_run`.
- Produces, in `agentflow/backlog.py`:
  - `_picture` builds the dict, then the view, passing `issues=[{"number", "blocked_by", "deps_complete",
    "on_board": _issue_blocker(n, pic) is None, "starting": overlay record status == "starting"} for each synced entry]`.
  - `def _deps_unknown(key: str, params: dict, pic: dict) -> str | None`: for `issue:N`, or a form card whose
    `params.issue` is a synced N, the entry's `deps_note` when `deps_complete` is False; else None.
  - Issue cards in `snapshot` carry `deps` = blocker keys, `blocks: [{"key": issue_key(r), "label": "#N" | "owner/repo#N"}]`
    (from the entry's `blocks`; every other card gets `blocks: []`), and when `_deps_unknown` says so,
    `status: "blocked"` and `note: "Dependencies unknown: <reason>"`; else a non-None `cycle` becomes the `note`.
    The `taken` override stays as it is.
  - `begin_start` refuses, before the existing `blocked_by` refusal, with
    `Refused(f"issue #{N}: its dependencies could not be read: {reason}")` (for a form card, the same text with its
    issue's number).
  - `_recheck_after` adds to `wanted` the `issue_run(ref.number, runs).id` of every local ref in every synced
    entry's `blocked_by` (and of the blockers of form cards' issues) whose run is live or `done`.

- [ ] **Step 1: Write the failing tests** (through `ibk`: `_list_issues`, `ibk.refresh()`, `ibk.cards()`,
  `backlog.begin_start`, `ibk.start`; merges through `ibk.merges.state[<branch>] = True`):

```python
def test_an_issue_blocked_by_an_open_issue_cannot_start(ibk, monkeypatch):   # the root-cause regression
    _list_issues(monkeypatch, _issue(1, blocking=[_node(2)]), _issue(2, blocked_by=[_node(1)]))
    ibk.refresh()
    cards = ibk.cards()
    assert cards["issue:2"]["status"] == "blocked"
    assert [b["text"] for b in cards["issue:2"]["blocked_by"]] == ["issue #1 (in Backlog)"]
    assert cards["issue:1"]["blocks"] == [{"key": "issue:2", "label": "#2"}]
    assert cards["issue:1"]["status"] in ("ready", "parallel")
    with pytest.raises(Refused, match=r"blocked: runs after issue #1"):
        backlog.begin_start(ibk.cfg, ibk.root, "issue:2")

def test_unrelated_issues_are_parallel_and_a_dependent_is_not(ibk, monkeypatch): ...
    # #1, #2 blocked by #1, #3: issue:1 and issue:3 parallel_with each other; issue:1 not in issue:2's parallel_with

def test_a_chain_only_starts_at_its_root(ibk, monkeypatch): ...
    # #10; #11 after #10; #12 after #11; #13 after #10 and #12 → only issue:10 is ready/parallel; issue:10 blocks == #11, #13

def test_the_blocker_run_here_frees_its_dependent_only_once_merged(ibk, monkeypatch): ...
    # start issue:1 → issue:2 reads "issue #1 (running)"; emit run_finished → "(PR open)";
    # re-sync with #1 CLOSED in #2's ref, mark its branch merged, refresh → issue:2 ready

def test_a_blocker_outside_the_synced_set_still_blocks(ibk, monkeypatch): ...   # _node(7), no _issue(7) → "issue #7 (open (not on this board))"
def test_unknown_dependencies_show_and_refuse(ibk, monkeypatch): ...
    # FAKE_GH_OLD → status "blocked", note "Dependencies unknown: update gh",
    # begin_start raises Refused("issue #2: its dependencies could not be read: update gh")
def test_an_issue_cycle_shows_its_note_and_refuses(ibk, monkeypatch): ...     # note "dependency cycle: #3 -> #5 -> #3", Refused
def test_a_form_card_for_a_blocked_issue_is_refused(ibk, monkeypatch): ...
    # #2 blocked by open #1; backlog.add(cfg, root, "bug", {"issue": "2"}) → card blocked, begin_start Refused
def test_a_blockers_title_never_reaches_the_board_or_the_log(ibk, monkeypatch): ...
    # _node(1, title="SECRET-TITLE") as #2's blocker, #1 not synced → "SECRET-TITLE" not in json.dumps(snapshot)
    # and not in the backlog event log file; a refusal message has no "SECRET-TITLE"
def test_an_issue_in_the_module_lane_waits_for_its_blocker(ibk, monkeypatch): ...
    # module-lane issue #2 blocked by #1 → Refused; once #1 is free, start adds the row with deps []
```

- [ ] **Step 2: Run them and see them fail**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_backlog.py -k "cannot_start or unrelated_issues or chain_only or frees_its_dependent or outside_the_synced or unknown_dependencies or cycle_shows or form_card_for_a_blocked or title_never or waits_for_its_blocker" --import-mode=prepend -q`
Expected: FAIL (issue:2 is `ready`, no `blocks` key).

- [ ] **Step 3: Implement** the interfaces above.

- [ ] **Step 4: Run the backlog and dashboard files** (`test_backlog.py` also covers `views.board_table`, which reads the same snapshot)

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_backlog.py agentflow/tests/test_dashboard.py --import-mode=prepend -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add agentflow/backlog.py agentflow/tests/test_backlog.py
git commit -m "Show blocked synced issues on the Backlog and refuse to start them until their blockers are met." -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: The live recheck at start

**Files:**
- Modify: `agentflow/backlog.py` (`_taken_now` ~555, its caller `run_start` ~894)
- Test: `agentflow/tests/test_backlog.py` (beside `test_github_is_asked_again_at_start_...` ~1041)

**Interfaces:**
- Consumes: `_parse_refs`, `_here` (Task 1); `issue_ref_done`, `issue_key` (Task 2).
- Produces: `_taken_now(cfg, repo, number) -> str | None` now asks `gh issue view N --json state,labels,blockedBy`.
  After the closed and taken checks, it parses `reply["blockedBy"]` with `here` taken from the cached entry's URL.
  When the refs are unknown it returns `f"issue #{N}: its dependencies could not be read: {note}"`. When any ref is
  not `issue_ref_done` (with `list_runs` and the cached `merged`), it returns
  `f"issue #{N} is blocked: runs after " + ", ".join(<"issue #M (open)" | "owner/repo#M (open)">)`.
  When `gh` fails (including an old `gh`), it returns None, as today.

- [ ] **Step 1: Write the failing tests**; update the argv assertion in the existing parametrized test to
  `["issue", "view", "13", "--json", "state,labels,blockedBy"]`, and give its replies `"blockedBy": {"nodes": [], "totalCount": 0}`.

```python
def test_a_blocker_added_since_the_last_refresh_stops_the_start(ibk, monkeypatch):
    _list_issues(monkeypatch, _issue(13, labels=("agentflow", "bug")))
    ibk.refresh()
    monkeypatch.setenv("FAKE_GH_ISSUE", json.dumps({"state": "OPEN", "labels": [],
                                                    "blockedBy": {"nodes": [_node(5, title="SECRET")], "totalCount": 1}}))
    assert ibk.start("issue:13") is None and ibk.spawn.calls == []
    failure = ibk.cards()["issue:13"]["failure"]
    assert "is blocked: runs after issue #5 (open)" in failure and "SECRET" not in failure

def test_a_truncated_live_list_stops_the_start(ibk, monkeypatch): ...      # totalCount 3, one node → "could not be read"
def test_a_live_blocker_that_is_closed_lets_the_start_go_on(ibk, monkeypatch): ...  # _node(5, "CLOSED") → started
def test_an_old_gh_at_start_keeps_the_cached_decision(ibk, monkeypatch): ...  # FAKE_GH_OLD after refresh → started
```

- [ ] **Step 2: Run them and see them fail**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_backlog.py -k "asked_again or since_the_last_refresh or truncated_live or live_blocker or old_gh_at_start or cannot_be_asked" --import-mode=prepend -q`
Expected: FAIL (the argv, and the start goes on).

- [ ] **Step 3: Implement** `_taken_now` as above. It needs the runs and `merged`, so read them inside it
  (`list_runs(repo)`, `_load_state(repo).get("merged")`): it runs once per start, after the 202.

- [ ] **Step 4: Run the backlog file**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_backlog.py --import-mode=prepend -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add agentflow/backlog.py agentflow/tests/test_backlog.py
git commit -m "Check an issue's blockers on GitHub again at the moment it starts." -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: The `Blocks:` line on the dashboard

**Files:**
- Modify: `agentflow/dashboard.html` (`backlogNode` ~509-525)
- Test: `agentflow/tests/test_dashboard.py` (beside `test_the_board_carries_synced_issues_and_their_note` ~997)

**Interfaces:**
- Consumes: the snapshot card's `blocks: [{key, label}]` (Task 3).
- Produces: after the `Runs after:` line,
  `item.blocks && item.blocks.length ? el("div", { class: "sub", text: "Blocks: " + names(item.blocks) }) : null`
  (through `text`, never HTML).

- [ ] **Step 1: Write the failing tests** with a fixture `ddash` (as `idash`, but issue #12 blocked by #14, and #14
  `blocking` #12, and #14 without the in-progress label):

```python
def test_the_board_carries_blockers_and_blocks(ddash):
    cards = {c["key"]: c for c in payload(ddash.get("/api/board")[2])["backlog"]}
    assert cards["issue:12"]["status"] == "blocked"
    assert cards["issue:12"]["blocked_by"][0]["text"] == "issue #14 (in Backlog)"
    assert cards["issue:14"]["blocks"] == [{"key": "issue:12", "label": "#12"}]

def test_starting_a_blocked_issue_is_a_409(ddash):
    code, _, body = ddash.post("/api/backlog/start", {"item": "issue:12"})
    assert code == 409 and "runs after issue #14" in payload(body)["error"]
    assert ddash.fake.calls == []

def test_the_page_renders_the_blocks_line():
    page = (Path(__file__).parents[1] / "dashboard.html").read_text(encoding="utf-8")
    assert 'text: "Blocks: " + names(item.blocks)' in page
```

- [ ] **Step 2: Run them and see them fail**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_dashboard.py -k "blockers_and_blocks or blocked_issue_is_a_409 or blocks_line" --import-mode=prepend -q`
Expected: the page test FAILs (the two API tests may already pass after Task 3; keep them as the endpoint's pin).

- [ ] **Step 3: Add the line** to `backlogNode`.

- [ ] **Step 4: Run the dashboard and portability tests**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_dashboard.py agentflow/tests/test_portability.py --import-mode=prepend -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add agentflow/dashboard.html agentflow/tests/test_dashboard.py
git commit -m "Show which issues a Backlog card blocks." -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: `doctor`, the playbook and the changelog

**Files:**
- Modify: `agentflow/doctor.py` (new `_gh_fields_check` beside `_gh_check` ~135; `run_checks` ~292)
- Modify: `docs/agentflow-playbook.md` (after the synced-issues paragraph, ~275)
- Modify: `CHANGELOG.md` (`## [Unreleased]`)
- Test: `agentflow/tests/test_doctor.py`

**Interfaces:**
- Produces: `def _gh_fields_check(repo: Path) -> Check`, named `"gh issue fields"`, added after `_gh_check` in
  `run_checks` only when `cfg.issues.sync_label` is set and `shutil.which("gh")`. It runs
  `_run(["gh", "issue", "list", "--json"], repo)` and looks for a line whose stripped text is `blockedBy`
  (whatever the exit code). `ok`: `"this gh reads issue dependencies"`. `fail`: detail
  `"this gh cannot read issue dependencies (no blockedBy field)"`, fix
  `"update gh: the Backlog cannot read issue dependencies"`.

- [ ] **Step 1: Write the failing tests**

```python
SYNC = _patch(issues={"sync_label": "agentflow"})

def test_the_gh_fields_check_runs_only_with_issue_sync(tmp_cfg, repo):
    assert "gh issue fields" not in _checks(tmp_cfg, repo)
    assert _checks(tmp_cfg, repo, SYNC)["gh issue fields"].status == "ok"

def test_a_gh_without_blocked_by_fails_the_fields_check(tmp_cfg, repo, monkeypatch):
    monkeypatch.setenv("FAKE_GH_JSON_FIELDS", "Specify one or more comma-separated fields for `--json`:\n  number\n  title")
    check = _checks(tmp_cfg, repo, SYNC)["gh issue fields"]
    assert check.status == "fail" and "update gh" in check.fix
```

  `test_doctor_is_all_ok_with_every_tool_present_and_logged_in` stays as it is (sync off, no new check).

- [ ] **Step 2: Run them and see them fail**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_doctor.py --import-mode=prepend -q`
Expected: FAIL (`KeyError: 'gh issue fields'`).

- [ ] **Step 3: Implement** `_gh_fields_check` and the `run_checks` line.

- [ ] **Step 4: Write the docs.** Playbook, a new paragraph after the synced-issues one: an issue marked
  *blocked by* another in GitHub's Relationships menu shows `Runs after:` and cannot start until each blocker is
  closed on GitHub with no open-PR run here, or its run here merged; its blocker shows `Blocks:`; *parent*,
  *sub-issue* and *relates to* order nothing; dependencies that cannot be read (an old `gh`, a truncated list) show
  `Dependencies unknown` and Start is refused; there is no override, so remove the relationship on GitHub to run in
  parallel; `agentflow run --issue N` from the CLI is not checked. `CHANGELOG.md` under `[Unreleased]`:
  `Added`: "Synced issues that GitHub marks *blocked by* another issue show `Runs after:` / `Blocks:` on the Backlog
  and cannot start until the blocker is met, and `doctor` checks that `gh` can read issue dependencies."
  `Fixed`: "A synced issue no longer shows `Ready` while an issue it is blocked by is still open."

- [ ] **Step 5: Run the touched files, the portability gate and validate**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_doctor.py agentflow/tests/test_packaging.py agentflow/tests/test_portability.py --import-mode=prepend -q && bin/agentflow validate`
Expected: PASS, and `0 warnings`.

- [ ] **Step 6: Run the full suite** (25-60 min)

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests --import-mode=prepend -q`
Expected: PASS; `git status agentflow/tests/fixtures/explain` shows nothing.

- [ ] **Step 7: Commit**

```bash
git add agentflow/doctor.py agentflow/tests/test_doctor.py docs/agentflow-playbook.md CHANGELOG.md
git commit -m "Check that gh can read issue dependencies in doctor, and document blocked issues on the Backlog." -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```
