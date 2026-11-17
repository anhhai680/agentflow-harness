# Dashboard slice U5 (Board and Activity) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A calmer Board, and the run's full history, readable.
- **Board.** Cards show a phase progress strip, a "needs you" marker with its age, the live agent count and step names in words. The Board filters by lane, status, "needs me" and a text search kept in the URL. Done shows the newest cards first, with **Show all**.
- **Activity tab.** It tells the run's whole log as sentences, filtered by kind, paginated back to the first event, with each line's raw JSON one click away.

**Architecture:**
- **Server.**
  - A new pure module, `agentflow/activity.py`, maps each event to a category (Decisions, Agents, Gates, Commits, Stops) and to a sentence. It folds runs of `agent_turn` into one "Agent active 14 min, 37 tool calls" entry, and pages the result newest first by `seq`.
  - `GET /api/runs/<id>/events?before=&kinds=&limit=&turns=` serves it.
  - `run_detail`'s 300-event `timeline` becomes `recent`, the newest 10 sentences, for Overview.
  - `views._card` gains a progress strip, the wait's age, the run's issue, a step label and an end time.
- **Page.**
  - `dashboard_ui/activity.js` holds the Activity tab.
  - `board.js` gains the strip and marker, the filters (parsed from and written to `#/board?…`), and Done paging bounded by the new key `dashboard.done_shown`.

**Tech Stack:** Python 3.12 stdlib, pytest, plain browser JavaScript (no framework, no build step), and node (tests only, when on `PATH`).

**Spec:** `docs/specs/agentflow-dashboard-ux-proposal.md` (`Status: approved`, 2026-11-15). This plan covers slice **U5** (§7):
- §4.7: cards, filters and Done (the peek shipped in U0);
- §4.9;
- §4.11's `events` route;
- §8's `dashboard.done_shown`;
- Verification "U5".

**Builds on U0–U4**, which must be merged first:
- from U0: the router, `patchChildren`, `throttle`, `run_detail`;
- from U2: `assert_refusal_matrix`;
- from U3: the node `js` helper in `conftest.py`, and `card.agents`;
- from U4: the Inbox (the Board's "needs me" filter links nowhere else).

## Global Constraints

- Engine dependencies stay stdlib + `pyyaml>=6`. No JavaScript framework, bundler or CDN. node is a test-time tool only, and a test that needs it is skipped when `shutil.which("node")` is None.
- `PAGE_CSP` stays byte-identical.
- The new route is GET only and needs the token, the Host check and the Origin check. Run ids go through `_run()`. It reads only the run's own `events.jsonl`.
- `activity.py` is pure: it reads no file and no clock, and takes the event list as an argument, like `signals.py`.
- Every string from a run is set with `textContent`. The page sink scan, which covers every `dashboard_ui/*.js`, refuses these literals: `innerHTML`, `outerHTML`, `insertAdjacentHTML`, `document.write`, `eval(`, `new Function`, `setTimeout("`, ` style="` and `javascript:`. It also refuses any `https?://` match. A search string is matched as plain text and never compiled to a `RegExp`.
- No project strings in the engine (`test_portability.py` `DENYLIST`). Don't name the repository the spec measured: call it "the measured repository" in code, tests, docs and commits.
- No runner behaviour changes. `DEFAULT_DONE_DAYS = 0` still keeps every Done card's data (§4.7). `done_shown` changes only how many are drawn before **Show all**.
- The new config key `dashboard.done_shown` ships with all four companions: a Settings form field, a `validate` check, a `test_config_edit.py` case and a playbook §14 line.
- The golden files `agentflow/tests/fixtures/explain/*.txt` stay byte-identical.
- `CHANGELOG.md` gets one line under `## [Unreleased]` for each user-visible change, as each task says.
- Never create, edit or delete `uv.lock`. Run everything with `uv run --no-project`.
- Test command: `uv run --no-project --with pyyaml --with pytest pytest <path> --import-mode=prepend -q`.
- A commit message is one imperative sentence and ends with the pack's `project.commit_trailer`.

## Review Focus

1. **New events arrive while the reader pages back** through a live run. The `before=<seq>` cursor must give the same older page whatever was appended since: no line twice, no line skipped. New lines appear only at the top when the newest page is refetched. The test goes in Task 2.
2. **A run of `agent_turn` events across a page boundary.** A folded run is one entry, built from the whole run before the page is cut. So its "Agent active N min, M tool calls" counts every turn, and the next page never starts with the rest of it. The test goes in Task 2.
3. **An event type with no template**: one of about 130 types, a future one, or an event missing its usual fields. It still gets a sentence (its type in words, plus its step label when it has a key), never a blank line or a 500. The test goes in Task 2.
4. **Filters that hide every card.** The Board says "No card matches these filters" with **Clear filters**, instead of silently empty columns. A hash with unknown parameters or values (`#/board?status=nope&x=1`) is ignored, not an error. The tests go in Task 6.
5. **A search with characters special to regular expressions or HTML** (`[`, `(`, `*`, `<b>`): a plain case-insensitive substring match that never throws. `#81` and `81` both find issue 81. The test goes in Task 6.

---

### Task 1: `dashboard.done_shown`

**Files:**
- Modify: `agentflow/config_edit.py`: a form field in the "Dashboard" section, after `show_done_days`.
- Modify: `agentflow/config.py`: `_Validator.dashboard`.
- Modify: `agentflow/dashboard.py`: `DEFAULT_DONE_SHOWN`, and `/api/info` gains `done_shown`.
- Modify: `docs/agentflow-playbook.md` §14, the `dashboard` line.
- Modify: `CHANGELOG.md`.
- Test: `agentflow/tests/test_config.py`, `test_config_edit.py` and `test_dashboard.py`.

**Interfaces:**
- Produces:
  - the key `dashboard.done_shown`, an int (not a bool) from `1` to `1000` inclusive, `10` when absent. It isn't protected.
  - `dashboard.DEFAULT_DONE_SHOWN = 10`.
  - `GET /api/info` gains `done_shown: int`. A value the check would refuse reads as `10`.
  - The form field: `(("dashboard", "done_shown"), "Done cards shown before Show all", {"kind": "int", "min": 1, "max": 1000})`.

- [ ] **Step 1: Write the failing tests**

```python
# test_config.py
def test_dashboard_done_shown_is_a_whole_number_from_1_to_1000(tmp_cfg):
    for bad in (0, 1001, 2.5, True, "10"):
        found = issues_for(tmp_cfg, {"dashboard": {"done_shown": bad}})
        assert [i.path for i in found if i.level == "error"] == ["dashboard.done_shown"], bad
    for good in (1, 10, 1000):
        assert not errors(issues_for(tmp_cfg, {"dashboard": {"done_shown": good}})), good

# test_config_edit.py
def test_the_form_sets_how_many_done_cards_show(shipped):
    config_edit.save(shipped, form(shipped, (["dashboard", "done_shown"], 25)))
    assert load_config(shipped).dashboard["done_shown"] == 25
    with pytest.raises(config_edit.EditError):
        form(shipped, (["dashboard", "done_shown"], 0))

# test_dashboard.py
def test_info_carries_how_many_done_cards_show(dash):
    assert payload(dash.get("/api/info")[2])["done_shown"] == 10
    dash.server.cfg.dashboard["done_shown"] = 0
    assert payload(dash.get("/api/info")[2])["done_shown"] == 10
    dash.server.cfg.dashboard["done_shown"] = 3
    assert payload(dash.get("/api/info")[2])["done_shown"] == 3
```

Update `test_info_names_the_engine_version` to expect the new field.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_config.py agentflow/tests/test_config_edit.py agentflow/tests/test_dashboard.py -k "done_shown or done_cards or engine_version" --import-mode=prepend -q`
Expected: FAIL.

- [ ] **Step 3: Add the check, the field and the info value**

The error text: "must be a whole number from 1 to 1000". The playbook line: "`dashboard.done_shown` (10 by default, 1–1000) is how many Done cards the Board draws, newest first, before **Show all (N)**. `show_done_days` still decides which finished runs are kept at all."

- [ ] **Step 4: Run the tests to verify they pass**

Run the same command, then `bin/agentflow validate`. Expected: PASS, and `0 warnings`.

- [ ] **Step 5: Add the changelog line and commit**

Under `### Added`: "`dashboard.done_shown` (10 by default, 1–1000): how many Done cards the Board shows before **Show all**."

```bash
git add agentflow/config.py agentflow/config_edit.py agentflow/dashboard.py docs/agentflow-playbook.md agentflow/tests CHANGELOG.md
git commit -m "Add dashboard.done_shown with its form field, check and playbook line."
```

---

### Task 2: The run's log as sentences, by kind, in pages

**Files:**
- Create: `agentflow/activity.py`.
- Test: `agentflow/tests/test_activity.py`.

**Interfaces:**
- Consumes: `views.step_title` and `events.parse_iso`.
- Produces:
  - `KINDS = ("decisions", "agents", "gates", "commits", "stops")`.
  - `CATEGORY: dict[str, str]`, an event type → kind, for the types listed below. A type not in it, and not caught by the prefix rule, is in no kind and shows only under "all".
    - **decisions:**
      - any `hitl_*` or `phase_*` type (by prefix, so a new one joins);
      - `checkpoint_cleared`;
      - the runner's decisions: `condition_decided`, `condition_kept`, `stop_classified`, `effort_decided`, `effort_escalated`, `resolve_finished`, `doubt_settled`, `findings_reconciled`, `plan_batched`, `budget_scaled`, `run_marked_done`, `decision_recorded`.
      - `hitl_raised` is a decision **and** a stop: `CATEGORY` maps it to `stops`, and `in_kind` checks the prefix first.
    - **agents:** `agent_started`, `agent_session`, `agent_exited`, `agent_turn`, `subagent_started`, `subagent_finished`, `sizer_started`, `sizer_finished`, `plan_sized`, `effort_changed`, `rolled_over`, `compacted`, `context_window`, `resolve_started`.
    - **gates:** `gate_result`, `gate_cache_hit`, `gates_reused`, `gates_prerun`, `gates_deferred`, `fix_attempt`, `preflight_checked`, `conformance_checked`, `expectation_checked`, `review_result`, `review_skipped`, `step_findings`.
    - **commits:** `step_committed`, `task_committed`, `module_added`, `pr_opened`, `pr_updated`, `pr_marked_ready`, `early_pr_failed`, `ci_watching`, `ci_round`, `base_synced`, `sync_started`, `lessons_committed`.
    - **stops:** `hitl_raised`, `step_parked`, `run_held`, `run_hold_ended`, `guard_tripped`, `watchdog_fired`, `runner_error`, `environment_waiting`, `permission_denied`, `run_failed`, `run_paused`, `run_cancelled`.
  - `in_kind(event: dict, kind: str) -> bool`: for `decisions`, the prefix rule or the decision list; for the others, `CATEGORY.get(type) == kind`.
  - `sentence(event: dict) -> tuple[str, str]`, returning `(headline, detail)`:
    - `<who>` is `"You"` for a `by` other than `runner` (or missing), else `"The runner"`;
    - `<step>` is `step_title(key)`.
    - The templates (`detail` is `""` unless named):

      | type | headline | detail |
      |---|---|---|
      | `step_finished` | "`<step>` finished" ("… failed" when `status == "failed"`) | `summary` |
      | `step_started` | "`<step>` started" | |
      | `hitl_raised` | "Stopped for you: `<step>` (`<kind>`)" | `summary` |
      | `phase_approved` | "`<who>` approved `<phase>`" | |
      | `phase_rejected` | "`<who>` sent `<phase>` back (round `<round>`)" | `note` |
      | `phase_started` | "`<phase>` started" | |
      | `phase_approval_raised` | "`<phase>` is done and waits for approval" | `summary` |
      | `hitl_approved` | "`<who>` approved `<step>`" | `note` |
      | `hitl_answered` | "`<who>` answered `<step>`" | the answers as `q: a; …` |
      | `checkpoint_cleared` | "`<who>` cleared `<step>`" | |
      | `gate_result` | "Gate `<gate>` passed on `<step>`" / "… failed …" / "… skipped …" | |
      | `step_committed` | "Committed `<step>` (`<sha7>`)" | `summary` |
      | `task_committed` | "Task `<n>` committed (`<sha7>`)" | `title` |
      | `agent_started` | "`<step>` agent started (`<model>` · `<effort>`)" | |
      | `agent_exited` | "`<step>` agent exited `<exit_code>`" | |
      | `condition_decided` | "The runner took `<condition>` as `<true/false>` for `<step>`" | |
      | `stop_classified` | "The runner classified `<step>`'s stop as `<stop_class>`" | |
      | `resolve_finished` | "Resolver for `<step>` finished" | `summary` |
      | `run_started` | "Run started" | |
      | `run_finished` | "Run finished" | |
      | `pr_opened` | "Draft pull request opened" | `url` |
      | `pr_marked_ready` | "Pull request marked ready" | `url` |

    - Any other type gives `(<type with "_" as " ", first letter capitalised> + (": " + <step> when key is set), "")`.
    - Missing fields read as `"?"`. It never raises.
  - `activity_page(events: list[dict], before: int | None, kinds: tuple[str, ...], limit: int, turns: bool) -> dict` returns `{entries, before, total}`.
    1. **Filter.** Keep the events with `seq < before` (all of them when `before` is `None`) that pass the filter: `kinds == ()` means all; otherwise `any(in_kind(e, k) for k in kinds)`. With `turns` False, `agent_turn` events are left out of the filter, and step 2 handles them.
    2. **Fold.** With `turns` False, every maximal run of consecutive `agent_turn` events in the **whole** log (by `seq`, with no other event between them) becomes one entry: `{seq: <last seq>, first_seq, ts: <last ts>, type: "agent_activity", headline: "Agent active <m> min, <n> tool calls", detail: "", raw: ""}`.
       - `<m>` is the whole minutes from the first `ts` to the last, at least 1.
       - `<n>` counts the turns with a `last_tool`.
       - That entry is kept when its `seq < before` and `kinds` is `()` or holds `"agents"`.
    3. **Page.** Sort newest first by `seq`, and take `limit` entries. Each event entry is `{seq, ts, type, by, key, headline, detail, raw}`, where `raw` is `json.dumps(event, ensure_ascii=False)` cut to 4096 characters, with `" …"` when cut.
    4. **Cursor.** `before` in the result is the smallest `seq` on the page (the folded entry's `first_seq` for a fold), or `None` when no older entry exists. `total` is the number of entries the filter allows over the whole log.

- [ ] **Step 1: Write the failing tests**

```python
def ev(seq, type, **kw):
    return {"seq": seq, "ts": f"2026-11-16T10:{seq // 60 % 60:02d}:{seq % 60:02d}+00:00", "type": type, "by": "runner", **kw}

def log_of(n):  # a 5,000-event log: a step, then turns, in a cycle
    out = []
    for seq in range(1, n + 1):
        kind = ("step_started", "agent_turn", "agent_turn", "gate_result", "step_finished")[seq % 5]
        out.append(ev(seq, kind, key="build/build/task-1/implement", last_tool="Edit", gate="pytest", passed=True))
    return out

def test_paging_reaches_the_first_event_of_5000():                       # spec Verification U5
    events, before, seen = log_of(5000), None, []
    while True:
        page = activity_page(events, before, (), 200, turns=True)
        seen += [e["seq"] for e in page["entries"]]
        before = page["before"]
        if before is None: break
    assert seen == list(range(5000, 0, -1))

def test_decisions_are_only_hitl_phase_checkpoint_and_runner_decisions():  # spec Verification U5
    events = [ev(1, "phase_started", phase="build"), ev(2, "gate_result", gate="x", passed=True),
              ev(3, "hitl_raised", kind="blocked", key="build/x"), ev(4, "condition_decided", condition="c", value=True),
              ev(5, "checkpoint_cleared", key="build/x"), ev(6, "agent_started", step="build/x"),
              ev(7, "phase_unknown_future"), ev(8, "step_committed", key="build/x", sha="a" * 40)]
    page = activity_page(events, None, ("decisions",), 200, turns=False)
    assert [e["type"] for e in page["entries"]] == ["phase_unknown_future", "checkpoint_cleared", "condition_decided",
                                                     "hitl_raised", "phase_started"]

def test_turns_fold_into_one_line_and_a_page_never_splits_a_fold():      # Review Focus 2
    events = [ev(1, "step_started", key="k")] + [ev(s, "agent_turn", last_tool="Bash" if s % 2 else None) for s in range(2, 62)] \
             + [ev(62, "step_finished", key="k", status="done", summary="ok")]
    first = activity_page(events, None, (), 2, turns=False)
    assert [e["type"] for e in first["entries"]] == ["step_finished", "agent_activity"]
    assert first["entries"][1]["headline"] == "Agent active 1 min, 30 tool calls" and first["before"] == 2
    assert [e["type"] for e in activity_page(events, first["before"], (), 2, turns=False)["entries"]] == ["step_started"]

def test_a_cursor_is_stable_while_the_log_grows():                       # Review Focus 1
    events = log_of(600)
    older = activity_page(events, 400, (), 100, turns=True)
    grown = activity_page(events + [ev(s, "gate_result", gate="g", passed=False) for s in range(601, 700)], 400, (), 100, turns=True)
    assert older == {**grown, "total": older["total"]} and older["entries"][0]["seq"] == 399

@pytest.mark.parametrize("event, headline", [
    (ev(1, "step_finished", key="build/build/task-9/implement", status="done", summary="Task 9 is complete"),
     "Task 9 · implement finished"),
    ({**ev(2, "phase_approved", phase="define"), "by": "alex"}, "You approved define"),
    (ev(3, "phase_approved", phase="build"), "The runner approved build"),
    (ev(4, "hitl_raised", kind="blocked", key="build/build/task-9/implement", summary="floor_guard"),
     "Stopped for you: Task 9 · implement (blocked)"),
    (ev(5, "brand_new_thing", key="verify/verify"), "Brand new thing: verify"),       # Review Focus 3
    ({"seq": 6, "type": "gate_result"}, "Gate ? skipped on ?"),                         # Review Focus 3
])
def test_each_event_reads_as_a_sentence(event, headline):
    assert sentence(event)[0] == headline
```

For the `{"seq": 6, "type": "gate_result"}` case, the template must take a missing `passed` as "skipped", or adjust the expectation to whatever the template reads for a missing field. Pin the result, and never let it raise.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_activity.py --import-mode=prepend -q`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement `activity.py`**

Find the folds in one pass over the whole log, sorted by `seq`, before filtering by `before`. That is what keeps a fold whole across pages.

- [ ] **Step 4: Run the tests to verify they pass**

Same command, plus `test_portability.py`. Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agentflow/activity.py agentflow/tests/test_activity.py
git commit -m "Tell a run's log as sentences by kind, fold agent turns, and page it by seq."
```

---

### Task 3: The `events` route and a slim recent list

**Files:**
- Modify: `agentflow/dashboard.py`: `ARTIFACT_ROUTE` gains `events`, a `_events_page` handler, and the docstring.
- Modify: `agentflow/views.py`: `run_detail` drops `timeline` and gains `recent`. `TIMELINE_MAX` goes away when nothing reads it.
- Modify: `agentflow/tests/test_dashboard.py:334`. The `timeline` assertion becomes one on `recent`.
- Test: `agentflow/tests/test_dashboard.py`.

**Interfaces:**
- Consumes: `activity.activity_page`, `KINDS` (Task 2) and U2's `assert_refusal_matrix`.
- Produces:
  - `GET /api/runs/<id>/events?before=<seq>&kinds=<k,k>&limit=<n>&turns=1`:
    - `before` is a positive int, or absent;
    - `kinds` is a comma list of `KINDS`, empty or absent for all;
    - `limit` is from 1 to 500, 200 by default;
    - `turns=1` shows each `agent_turn`.
    - A bad value or a doubled parameter → 400, naming the parameter.
    - It returns `activity_page(run.events.read(), …)`.
  - `run_detail(...)["recent"]`: `activity_page(events, None, (), 10, turns=False)["entries"]`, from the events `run_detail` already reads.

- [ ] **Step 1: Write the failing tests**

```python
def test_the_events_route_pages_the_whole_log(dash):
    run = awaiting(dash.flow)
    for n in range(30):
        run.emit("gate_result", key="plan/plan", gate=f"g{n}", passed=True)
    page = payload(dash.get(f"/api/runs/{run.id}/events?limit=10&kinds=gates")[2])
    assert len(page["entries"]) == 10 and page["total"] == 30
    rest = payload(dash.get(f"/api/runs/{run.id}/events?limit=50&kinds=gates&before={page['before']}")[2])
    assert len(rest["entries"]) == 20 and rest["before"] is None

@pytest.mark.parametrize("query", ["before=0", "before=x", "limit=0", "limit=501", "kinds=nope", "kinds=gates&kinds=stops",
                                   "turns=yes"])
def test_the_events_route_refuses_bad_parameters(dash, query):
    run = awaiting(dash.flow)
    assert dash.get(f"/api/runs/{run.id}/events?{query}")[0] == 400

def test_the_events_route_keeps_the_refusal_matrix(dash):
    run = awaiting(dash.flow)
    assert_refusal_matrix(dash, run, "events")

def test_run_detail_carries_ten_recent_sentences_not_the_raw_timeline(dash):
    run = awaiting(dash.flow)
    detail = payload(dash.get(f"/api/runs/{run.id}")[2])
    assert "timeline" not in detail and detail["recent"][0]["headline"] == "plan is done and waits for approval"
    assert len(detail["recent"]) <= 10
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_dashboard.py -k "events_route or recent_sentences or board_and_run_detail" --import-mode=prepend -q`
Expected: FAIL.

- [ ] **Step 3: Add the route and `recent`**

Add to the docstring's route list: "`GET /api/runs/<id>/events?before=&kinds=&limit=&turns=` (the run's whole log as sentences, newest first, by kind, in pages by `seq`)". Remove "its timeline" from the `views` module docstring's `run_detail` line, and add "its 10 newest sentences".

- [ ] **Step 4: Run the tests to verify they pass**

Run all of `test_dashboard.py`. Expected: PASS. The page still reads `detail.timeline` until Task 4, so do Task 4 before you smoke-test in a browser.

- [ ] **Step 5: Commit**

```bash
git add agentflow/dashboard.py agentflow/views.py agentflow/tests/test_dashboard.py
git commit -m "Serve a run's whole log as paged sentences and slim the run detail to ten recent ones."
```

---

### Task 4: The Activity tab

**Files:**
- Create: `agentflow/dashboard_ui/activity.js`, added to `UI_SCRIPTS` after `inbox.js`.
- Modify:
  - `agentflow/dashboard_ui/run.js`: the `activity` branch of `runTabEntries` hands over to `activityEntries`; Overview's "Recent activity" draws `detail.recent`; `patchTimeline` and `timelineShell` go away;
  - `agentflow/dashboard_ui/drawer.js`: remove `describe` once nothing calls it;
  - `agentflow/dashboard_ui/main.js`: `runRefresh` refetches the newest page when the Activity tab is open;
  - `agentflow/dashboard.html`: the filter chips, line and raw styles.
- Modify: `docs/agentflow-playbook.md`, line 329. Add one sentence on the Activity tab.
- Modify: `CHANGELOG.md`.
- Test: `agentflow/tests/test_dashboard_js.py` and `agentflow/tests/test_dashboard.py`.

**Interfaces:**
- Consumes: Task 3's route and `detail.recent`, `patchChildren`, `entry`, `ago` and `ui.run`.
- Produces, in `activity.js`:
  - `ACTIVITY_FILTERS = [["all", "All"], ["decisions", "Decisions"], ["agents", "Agents"], ["gates", "Gates"], ["commits", "Commits"], ["stops", "Stops"]]`.
  - `activityQuery(state: {filter, before, turns}) -> string` is pure:
    - it gives `"?limit=200"`, plus `"&kinds=<filter>"` unless the filter is `all`, `"&before=<n>"` when `before` is set, and `"&turns=1"` when `turns` is set;
    - so `{filter: "all", before: null, turns: false}` → `"?limit=200"`.
  - `mergePages(have: Entry[], page: Entry[]) -> Entry[]` is pure: union by `seq`, newest first, with no duplicates. An entry seen again (a refetched newest page) replaces the old one.
  - **UI state:** `ui.activity = {runId, filter: "all", turns: false, entries: [], before: null, total: 0, open: Set<seq>}`. A filter change resets `entries` and `before`.
  - `activityEntries(detail) -> entry[]`, which draws:
    - the filter chips, then a "Show agent turns" checkbox;
    - "`<shown>` of `<total>`";
    - one line per entry, keyed `seq:<seq>`, showing the time (`HH:MM:SS`), the headline, the detail in `<em>` when set, and a `<details>` "Raw" holding `raw` in a `<pre>`, opened when its `seq` is in `open`;
    - **Load older** while `before !== null`;
    - "The first event of this run." once `before === null`.
  - **Live.** A refresh refetches the first page (no `before`) and merges it, so new lines appear at the top and the older pages already loaded stay.

- [ ] **Step 1: Write the failing tests**

```python
# test_dashboard_js.py
def test_the_activity_query_reads_its_state():
    q = lambda s: js(["activity.js"], "activityQuery(args)", s)
    assert q({"filter": "all", "before": None, "turns": False}) == "?limit=200"
    assert q({"filter": "decisions", "before": 812, "turns": True}) == "?limit=200&kinds=decisions&before=812&turns=1"

def test_pages_merge_by_seq_newest_first_without_duplicates():
    have = [{"seq": 9, "headline": "old"}, {"seq": 5}]
    got = js(["activity.js"], "mergePages(args.have, args.page).map(e => [e.seq, e.headline || ''])",
             {"have": have, "page": [{"seq": 12}, {"seq": 9, "headline": "new"}]})
    assert got == [[12, ""], [9, "new"], [5, ""]]

# test_dashboard.py
def test_the_activity_tab_pages_the_log_with_filters_and_raw_lines():
    a = (UI_DIR / "activity.js").read_text(encoding="utf-8")
    for needle in ("/events", "Load older", "The first event of this run.", "Show agent turns", "Raw", "ui.activity"):
        assert needle in a, needle
    run_js = (UI_DIR / "run.js").read_text(encoding="utf-8")
    assert "detail.recent" in run_js and "detail.timeline" not in page_source() and "function describe" not in page_source()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_dashboard_js.py agentflow/tests/test_dashboard.py -k "activity" --import-mode=prepend -q`
Expected: FAIL.

- [ ] **Step 3: Implement the tab**

- [ ] **Step 4: Run the tests, then check in a browser**

Run `test_dashboard.py` and `test_dashboard_js.py`. Expected: PASS, with no node test skipped.

In a browser, on a finished run with a long log:
- **Load older** walks back to "The first event of this run.";
- **Decisions** shows only decisions;
- a Raw line stays open through live events;
- Overview's "Recent activity" reads as sentences.

- [ ] **Step 5: Add the changelog line and commit**

Under `### Changed`: "A run's Activity tab tells its whole log as sentences (\"Task 9 · implement finished: …\", \"You approved define\"), filtered to Decisions, Agents, Gates, Commits or Stops. It folds agent turns into \"Agent active 14 min, 37 tool calls\", pages back to the first event, and shows each line's raw JSON on demand. The run detail no longer sends its last 300 raw events."

```bash
git add agentflow/dashboard_ui agentflow/dashboard.html agentflow/tests docs/agentflow-playbook.md CHANGELOG.md
git commit -m "Replace the 300-event timeline with an Activity tab that reads the whole log as filtered sentences."
```

---

### Task 5: Cards with a progress strip and a "needs you" marker

**Files:**
- Modify: `agentflow/views.py`:
  - `_digest` gains `last_ts`;
  - `_card` gains `strip`, `issue`, `step_label` and `ended_at`, and `waiting` gains `since`;
  - a new `_strip(state, phases)`.
- Modify: `agentflow/dashboard_ui/board.js`: `cardNode` draws the strip, the marker and the labels.
- Modify: `agentflow/dashboard.html`: the strip and marker styles, using the `STATUS_CLASS` colours.
- Modify: `CHANGELOG.md`.
- Test: `agentflow/tests/test_dashboard.py`.

**Interfaces:**
- Consumes: `views.lane_phases(cfg, lane)`, `views.step_title`, U0's digest cache and U3's `card.agents`.
- Produces:
  - `_strip(state: dict, phases: list[str]) -> list[dict]` returns `[{phase, state}]`, one per lane phase, in order. `state` is the first of these that holds:
    - `"waiting"`: `state.waiting.phase == phase`, or the phase row's status is `awaiting_approval`;
    - `"done"`: status `approved` or `done`;
    - `"skipped"`: status `skipped`;
    - `"failed"`: status `failed`;
    - `"current"`: status `running` or `refining`, or `phase == state.phase`;
    - `"todo"`: none of the above (no row).
  - `_card(...)` gains:
    - `strip`;
    - `issue`: `params.issue` as text, or `None`;
    - `step_label`: `step_title(state.step)`, or `None`;
    - `ended_at`: the digest's `last_ts` for a terminal status, else `None`;
    - `waiting.since`.
  - **Page.** `cardNode` draws:
    - the strip: one small segment per phase, classed `strip-<state>`, titled `"<phase>: <state>"`;
    - when `card.waiting`, the marker "● needs you · `<ago(waiting.since)>`" in the waiting colour;
    - `"<n> agents"` when `card.agents.length > 1`, which U3 already prints as labels, so this is only its count chip;
    - `step_label` in place of the raw `step` in the "where" line;
    - the existing `task` (`9/24`) beside `build`'s segment.

- [ ] **Step 1: Write the failing tests**

```python
def test_a_card_has_a_strip_and_a_needs_you_marker(dash):
    awaiting(dash.flow, phase="plan")
    card = card_of(dash)
    phases = [s["phase"] for s in card["strip"]]
    states = {s["phase"]: s["state"] for s in card["strip"]}
    assert phases == lane_phases(dash.flow.cfg, "feature")
    assert states["plan"] == "waiting" and card["waiting"]["since"]
    assert all(states[p] == "todo" for p in phases[phases.index("plan") + 1:])
    assert card["ended_at"] is None

def test_a_finished_card_has_an_end_time_and_a_done_strip(dash):
    run = dash.flow.new_run()
    run.emit("phase_started", phase="define"); run.emit("phase_approved", phase="define", by="me")
    run.emit("run_finished")
    card = card_of(dash)
    assert card["ended_at"] and {s["phase"]: s["state"] for s in card["strip"]}["define"] == "done"

def test_the_card_draws_its_strip_and_marker():
    board = (UI_DIR / "board.js").read_text(encoding="utf-8")
    for needle in ("card.strip", "strip-", "needs you", "step_label"):
        assert needle in board, needle
```

`card_of` expects exactly one card. Each test starts from a fresh `dash`, so that holds.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_dashboard.py -k "strip or needs_you" --import-mode=prepend -q`
Expected: FAIL.

- [ ] **Step 3: Implement `_strip`, the card fields and the drawing**

- [ ] **Step 4: Run the tests, then check in a browser**

Run all of `test_dashboard.py`. Expected: PASS.

In a browser:
- a waiting card shows its strip with the waiting segment and "needs you · 12 min";
- a done card's strip is all done.

- [ ] **Step 5: Add the changelog line and commit**

Under `### Added`: "Board cards show a phase progress strip, a \"needs you\" marker with how long it has waited, the live agent count and the step in words."

```bash
git add agentflow/views.py agentflow/dashboard_ui/board.js agentflow/dashboard.html agentflow/tests/test_dashboard.py CHANGELOG.md
git commit -m "Give each Board card a phase progress strip, a needs-you marker and its step in words."
```

---

### Task 6: Board filters in the URL, and Done paging

**Files:**
- Modify: `agentflow/dashboard_ui/run.js`: `parseRoute`/`routeHash` carry a `query` for `#/board?…`.
- Modify: `agentflow/dashboard_ui/board.js`: the filter bar, `filterCards`, the empty-result note, and Done ordering and paging.
- Modify: `agentflow/dashboard_ui/main.js`: `done_shown` from `/api/info`.
- Modify: `agentflow/dashboard.html`: the filter bar (`#board-filters`).
- Modify: `CHANGELOG.md`.
- Test: `agentflow/tests/test_dashboard_js.py` and `agentflow/tests/test_dashboard.py`.

**Interfaces:**
- Consumes: Task 5's `card.issue` and `card.ended_at`, Task 1's `done_shown`, and U0's `parseRoute`/`routeHash`.
- Produces:
  - **The router.**
    - `parseRoute(hash)` splits off everything after the first `?`, and for `view === "board"` it returns `query: {lane, status, needs, q}`.
    - Each value comes from `URLSearchParams`. Unknown parameters are dropped. `needs` is `true` only for `"1"`. `lane` and `status` stay as text, and the board ignores a value it doesn't know.
    - `routeHash` writes `#/board?` + only the set values, in the order `lane, status, needs, q`, and `#/board` when none is set.
  - `filterCards(cards, query, lanes: string[], statuses: string[]) -> cards` is pure:
    - `lane`: kept when the value is in `lanes` and equals `card.lane`;
    - `status`: kept when the value is in `statuses` and equals `card.status`;
    - `needs`: kept when `card.waiting` is set;
    - `q`: trimmed and lower-cased, with a leading `#` dropped; kept when it is a substring of any of `card.module`, `card.issue`, `card.branch` or `card.run_id`, lower-cased, using `String.prototype.includes`, never a `RegExp`;
    - an unknown `lane` or `status` value filters nothing.
  - `doneCards(cards, shown: number, all: boolean) -> {cards, hidden: number}` is pure: Done cards sorted by `ended_at` descending (then `run_id` descending), cut to `shown` unless `all`.
  - **What the page does.**
    - The filter bar (lane select, status select, a "needs me" checkbox and a search box) writes the query with `history.replaceState(null, "", routeHash(...))`, so it doesn't fill the history. A reload keeps it.
    - When cards exist but `filterCards` leaves none, the columns are replaced by "No card matches these filters" and **Clear filters**.
    - The Done column draws `doneCards(...)`, then **Show all (`<N>`)** when `hidden > 0`. `ui.doneAll` keeps the choice.

- [ ] **Step 1: Write the failing tests**

```python
# test_dashboard_js.py
CARDS = [{"run_id": "20261116-0900-feature-crm", "lane": "feature", "status": "running", "module": "crm", "issue": "81",
          "branch": "flow/feature/crm", "waiting": None},
         {"run_id": "20261116-0800-bug-auth", "lane": "bug", "status": "waiting_human", "module": "auth[v2]", "issue": None,
          "branch": "flow/bug/auth", "waiting": {"kind": "blocked"}}]

@pytest.mark.parametrize("query, expected", [
    ({}, ["crm", "auth[v2]"]),
    ({"lane": "bug"}, ["auth[v2]"]),
    ({"needs": True}, ["auth[v2]"]),
    ({"q": "#81"}, ["crm"]), ({"q": "81"}, ["crm"]),
    ({"q": "[v2"}, ["auth[v2]"]), ({"q": "(*"}, []), ({"q": "<b>"}, []),            # Review Focus 5
    ({"status": "nope"}, ["crm", "auth[v2]"]), ({"lane": "nope"}, ["crm", "auth[v2]"]),  # Review Focus 4
])
def test_cards_filter_by_lane_status_needs_and_plain_text(query, expected):
    got = js(["board.js"], "filterCards(args.cards, args.query, ['feature', 'bug'], ['running', 'waiting_human']).map(c => c.module)",
             {"cards": CARDS, "query": query})
    assert got == expected

def test_the_board_query_round_trips_through_the_hash():                           # Review Focus 4
    assert js(["run.js"], "parseRoute(args).query", "#/board?status=nope&x=1&needs=1&q=a%20b") == \
        {"lane": None, "status": "nope", "needs": True, "q": "a b"}
    assert js(["run.js"], "routeHash({view: 'board', query: {lane: 'bug', needs: true, q: ''}})", None) == "#/board?lane=bug&needs=1"
    assert js(["run.js"], "routeHash({view: 'board', query: {}})", None) == "#/board"

def test_done_shows_the_newest_first_up_to_the_cap():
    done = [{"run_id": f"r{i}", "ended_at": f"2026-11-{10 + i:02d}T00:00:00+00:00"} for i in range(5)]
    got = js(["board.js"], "doneCards(args, 3, false)", done)
    assert [c["run_id"] for c in got["cards"]] == ["r4", "r3", "r2"] and got["hidden"] == 2
    assert js(["board.js"], "doneCards(args, 3, true).hidden", done) == 0

# test_dashboard.py
def test_the_board_says_when_filters_hide_every_card():
    board = (UI_DIR / "board.js").read_text(encoding="utf-8")
    assert "No card matches these filters" in board and "Clear filters" in board and "Show all (" in board
    assert "new RegExp" not in board and "history.replaceState" in board
```

`board.js` and `run.js` must load in node without a DOM, and `run.js`'s `parseRoute` reads `VIEWS` from `main.js`. Pass `VIEWS` into the test by loading a stub first, `js(["_views_stub.js", "run.js"], ...)`. The stub is a test fixture file `agentflow/tests/fixtures/js/_views_stub.js` holding `const VIEWS = ["inbox","board","workflow","agents","metrics","lessons","settings"];`. Extend `js()` to accept a path outside `UI_DIR` when it is absolute. If `board.js` touches the DOM at load time, move those lines into functions first.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_dashboard_js.py agentflow/tests/test_dashboard.py -k "filter or round_trips or newest_first_up_to or hide_every" --import-mode=prepend -q`
Expected: FAIL.

- [ ] **Step 3: Implement the router query, the filters and Done paging**

- [ ] **Step 4: Run the tests, then check in a browser**

Run all of `test_dashboard.py`, `test_dashboard_js.py` and `test_portability.py`. Expected: PASS.

In a browser:
- choose the bug lane and type `auth`. The URL reads `#/board?lane=bug&q=auth`, a reload keeps both, and Back doesn't step through each keystroke;
- a search that matches nothing shows **Clear filters**;
- with 12 finished runs, Done shows 10 and **Show all (12)**.

- [ ] **Step 5: Add the changelog line and commit**

Under `### Added`: "The Board filters by lane, status, \"needs me\" and a search over module, issue and branch, kept in the URL, and Done shows the newest `dashboard.done_shown` cards first with **Show all**."

```bash
git add agentflow/dashboard_ui agentflow/dashboard.html agentflow/tests CHANGELOG.md
git commit -m "Filter the Board by lane, status, needs-me and search in the URL, and page Done newest first."
```

---

### Task 7: Whole-slice verification

There's no new code in this task. If a check fails, fix it in the task that owns the code.

- [ ] **Step 1:** Run `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests --import-mode=prepend -q`. Expected: all pass, with no node test skipped on a machine that has node. This is the full suite, which takes 25–60 min.
- [ ] **Step 2:** Run `bin/agentflow validate`. Expected: `0 warnings`.
- [ ] **Step 3:** Run `git diff main -- agentflow/tests/fixtures/explain`. Expected: empty.
- [ ] **Step 4:** Run `git diff main -- agentflow/dashboard.py | grep -n PAGE_CSP`. Expected: no changed line.
- [ ] **Step 5:** Walk Verification "U5" from the spec in a browser, with a scratch `XDG_STATE_HOME` and a run whose log holds 5,000 events:
  - the Activity tab reaches its first event with **Load older**;
  - **Decisions** shows only `hitl_*`, `phase_*`, `checkpoint_cleared` and the runner's decisions.
- [ ] **Step 6:** Compare the dashboard with `docs/specs/agentflow-dashboard-ux-prototype.html`, view by view: Inbox, Board, a Run page's seven tabs, and Agents. List any difference that isn't a deliberate choice of U0–U5's plans as a follow-up note in the PR body.
