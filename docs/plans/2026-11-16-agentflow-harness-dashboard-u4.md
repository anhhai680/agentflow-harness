# Dashboard slice U4 (Inbox) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Clear a stop in one or two touches, with its context in front of you. "Needs human" becomes an **Inbox** tab, with a count badge in the header. It shows titled decision cards with forms in place (no `prompt()`), the context each decision needs, grouping by run or by kind, and the keyboard. The scorecard gains `informed_approval`, which measures whether approvers read what they approved.

**Architecture:**
- **Server.**
  - A new read-only module, `agentflow/inbox_view.py`, builds each inbox item from `hitl.inbox_items`, and adds:
    - a title in words;
    - the readable inbox text, with its CLI lines split off;
    - the run's facts;
    - for a phase approval, the artifacts' summary and open findings (U1);
    - for a stop inside a plan task, its blast radius (U2);
    - the actions the run's state allows (U0), and whether an approval needs a note.
  - It also returns a "Watch" list (held and stalled runs, with no buttons) and the decisions made in the last 30 minutes, so a card the reader was typing on can say who decided it.
  - `GET /api/inbox` serves it.
  - On a phase approval, the dashboard records on `phase_approved` both what the page showed (`viewed`, U1) and what the phase produced (`produced`). So `signals.informed_approval:<phase>` stays a pure function of the events.
- **Page.**
  - `dashboard_ui/inbox.js` holds the Inbox view: cards, in-place forms, grouping, the keyboard, and the "decided elsewhere" state.
  - The header shows `Inbox (N)` on every view.
  - `dashboard.inbox_legacy: true` brings back the old Needs-human list for one release.

**Tech Stack:** Python 3.12 stdlib, pytest, plain browser JavaScript (no framework, no build step), and node (tests only, when on `PATH`).

**Spec:** `docs/specs/agentflow-dashboard-ux-proposal.md` (`Status: approved`, 2026-11-15). This plan covers slice **U4** (§7):
- §4.6;
- §4.1's header badge;
- §4.11's `inbox` route;
- §6's `informed_approval`;
- §8's `dashboard.inbox_legacy`;
- Open Question 4 (a "Watch" group, taking the proposed answer: yes);
- Verification "U4".

**Builds on U0–U3**, which must be merged first:
- from U0: `run_control.available_actions`, the router, `patchChildren`, `throttle`;
- from U1: `artifacts.run_artifacts`/`run_findings`, `markdown.js`, `viewedList`, and `viewed` on approve;
- from U2: `plan_view.run_plan`;
- from U3: the node `js` helper in `conftest.py`.

## Global Constraints

- Engine dependencies stay stdlib + `pyyaml>=6`. No JavaScript framework, bundler or CDN. node is a test-time tool only, and a test that needs it is skipped when `shutil.which("node")` is None.
- `PAGE_CSP` stays byte-identical.
- `GET /api/inbox` needs the token, the Host check and the Origin check, as every data route does. Every action still goes through the existing `POST /api/runs/<id>/<action>` routes. U4 adds no new write route.
- **CLI parity** (§5): every button makes the same `hitl`/`run_control` call the CLI makes, with `by="dashboard"`. The page never decides anything the runner doesn't. A rule the server enforces (the note on `blocked_repeat`) is also checked on the page before the request, never only on the page.
- `informed_approval` is a signal, never an obligation, and no approval is refused for it (Open Question 3). The CLI's approvals carry neither `viewed` nor `produced`.
- Every string from a run is set with `textContent`. The page sink scan, which covers every `dashboard_ui/*.js`, refuses these literals: `innerHTML`, `outerHTML`, `insertAdjacentHTML`, `document.write`, `eval(`, `new Function`, `setTimeout("`, ` style="` and `javascript:`. It also refuses any `https?://` match. The Inbox uses no `prompt()` and no `confirm()`.
- No project strings in the engine (`test_portability.py` `DENYLIST`). Don't name the repository the spec measured: call it "the measured repository" in code, tests, docs and commits.
- The new config key `dashboard.inbox_legacy` ships with all four companions: a Settings form field, a `validate` check, a `test_config_edit.py` case and a playbook §14 line.
- The golden files `agentflow/tests/fixtures/explain/*.txt` stay byte-identical.
- `CHANGELOG.md` gets one line under `## [Unreleased]` for each user-visible change, as each task says.
- Never create, edit or delete `uv.lock`. Run everything with `uv run --no-project`.
- Test command: `uv run --no-project --with pyyaml --with pytest pytest <path> --import-mode=prepend -q`.
- A commit message is one imperative sentence and ends with the pack's `project.commit_trailer`.

## Review Focus

1. **A note where the server refuses one.** `hitl.approve` rejects a note on a phase approval, on the kinds in `CLEARED_BY_APPROVE` and on `preflight_failed`, and requires one on `blocked_repeat`. The card must offer the note field only where it is allowed, and require it only where it is required. The server sends `approve_note: "required"|"optional"|"none"`, and the page follows it. The tests go in Task 2 and Task 5.
2. **A parked question next to the run's own wait.** One run can carry both: a `blocked` wait, plus a parked `needs_human` on another task. They are two cards with their own keys. Answering the parked one must post its `key`, never the wait's. Rejecting is never offered on a parked card. The tests go in Task 2 and Task 5.
3. **A decision made elsewhere while text is typed**, from the CLI or another tab. The card stays, its text stays selectable, and it reads "Approved from the CLI at 14:02 by <name>" (or "from the dashboard", or "by the runner"). It does not vanish or post a stale request. The tests go in Task 2 and Task 6.
4. **A keystroke while typing.** `j`, `k`, `o`, `a` and `r` must do nothing while the focus is in an input, a textarea or an open dialog. A single `a` must never approve: it arms the approval, and a second `a` within 4 s confirms it. The test goes in Task 6.
5. **An inbox file that is missing or huge.** The card still shows the wait's `summary`, and the inbox text is cut to `artifact_max_kb` with "Showing the first N KB". `inbox_items` already returns `inbox_path: None` for a missing file. The test goes in Task 2.

---

### Task 1: `dashboard.inbox_legacy`

**Files:**
- Modify: `agentflow/config_edit.py`: a form field in the "Dashboard" section, after `artifact_max_kb`.
- Modify: `agentflow/config.py`: `_Validator.dashboard`.
- Modify: `agentflow/dashboard.py`: `/api/info` gains `inbox_legacy`.
- Modify: `docs/agentflow-playbook.md` §14, the `dashboard` line.
- Modify: `CHANGELOG.md`.
- Test: `agentflow/tests/test_config.py`, `test_config_edit.py` and `test_dashboard.py`.

**Interfaces:**
- Produces:
  - the key `dashboard.inbox_legacy`, a bool, `false` when absent. It isn't protected.
  - The form field: `(("dashboard", "inbox_legacy"), "Show the old Needs-human list instead of the Inbox", {"kind": "bool"})`.
  - `GET /api/info` returns `{name, version, board_refresh_s, inbox_legacy: bool}`. A value that isn't a bool reads as `false`.

- [ ] **Step 1: Write the failing tests**

```python
# test_config.py
def test_dashboard_inbox_legacy_is_true_or_false(tmp_cfg):
    for bad in ("yes", 1, None):
        found = issues_for(tmp_cfg, {"dashboard": {"inbox_legacy": bad}})
        assert [i.path for i in found if i.level == "error"] == ["dashboard.inbox_legacy"], bad
    assert not errors(issues_for(tmp_cfg, {"dashboard": {"inbox_legacy": True}}))

# test_config_edit.py
def test_the_form_sets_the_inbox_legacy_switch(shipped):
    config_edit.save(shipped, form(shipped, (["dashboard", "inbox_legacy"], True)))
    assert load_config(shipped).dashboard["inbox_legacy"] is True

# test_dashboard.py
def test_info_says_whether_the_legacy_inbox_is_on(dash):
    assert payload(dash.get("/api/info")[2])["inbox_legacy"] is False
    dash.server.cfg.dashboard["inbox_legacy"] = "yes"
    assert payload(dash.get("/api/info")[2])["inbox_legacy"] is False
    dash.server.cfg.dashboard["inbox_legacy"] = True
    assert payload(dash.get("/api/info")[2])["inbox_legacy"] is True
```

Update `test_info_names_the_engine_version` (`test_dashboard.py:129`) to expect the new field.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_config.py agentflow/tests/test_config_edit.py agentflow/tests/test_dashboard.py -k "inbox_legacy or engine_version" --import-mode=prepend -q`
Expected: FAIL.

- [ ] **Step 3: Add the check, the field and the info value**

The error text: "must be true or false". The playbook line: "`dashboard.inbox_legacy` (false by default) shows the old Needs-human list at the bottom of the Board instead of the Inbox tab. It is a fallback for one release."

- [ ] **Step 4: Run the tests to verify they pass**

Run the same command, then `bin/agentflow validate`. Expected: PASS, and `0 warnings`.

- [ ] **Step 5: Add the changelog line and commit**

Under `### Added`: "`dashboard.inbox_legacy` (false by default): show the old Needs-human list instead of the Inbox, for one release."

```bash
git add agentflow/config.py agentflow/config_edit.py agentflow/dashboard.py docs/agentflow-playbook.md agentflow/tests CHANGELOG.md
git commit -m "Add dashboard.inbox_legacy with its form field, check and playbook line."
```

---

### Task 2: Inbox items as decisions

**Files:**
- Create: `agentflow/inbox_view.py`.
- Test: `agentflow/tests/test_inbox_view.py`.

**Interfaces:**
- Consumes:
  - `hitl.inbox_items`, `hitl.CLEARED_BY_APPROVE`, `hitl.ENVIRONMENT` and `run_control.available_actions`;
  - `views.step_title`, `views._label` and `views.display_status`;
  - from U1: `artifacts.run_artifacts`, `run_findings` and `PHASE_KINDS`;
  - from U2: `plan_view.run_plan` and `run_files.read_file`'s read rules (through `_head`).
- Produces:
  - `RECENT_S = 30 * 60`.
  - `split_cli(text: str) -> tuple[str, list[str]]`: the inbox Markdown without its CLI paragraphs, plus those paragraphs. A paragraph is CLI when it holds a backticked `` `agentflow `` command.
  - `item_title(item: dict) -> str` is pure. `<label>` is `step_title(key)`:

    | kind | title |
    |---|---|
    | `phase_approval` | "Approve the spec" or "Approve the plan" (`PHASE_KINDS`), else "Approve <phase>" |
    | non-empty `questions` (the `parked` kind included) | "Answer 1 question" / "Answer <n> questions" (`<label>`) |
    | `blocked` | "<label> is blocked" |
    | `blocked_repeat` | "<label> blocked again" |
    | `guard_violation` | "A guard stopped <label>" |
    | `gate_failed` | "<label>: gates still fail" |
    | `ci_failed` | "CI failed" |
    | `budget_exceeded` | "The budget is reached" |
    | `environment` | "This machine can't run <label>'s gates" |
    | `preflight_failed` | "Preflight failed" |
    | `checkpoint` | "Checkpoint: <label>" |
    | `max_rounds` | "<label> ran out of rounds" |
    | `review_unresolved` | "Review findings left on <label>" |
    | anything else | "<label> stopped (<kind>)" |
  - `approve_note(item: dict) -> str`:
    - `"none"` for a phase approval, a kind in `CLEARED_BY_APPROVE`, `preflight_failed`, or an item with questions;
    - `"required"` for `blocked_repeat`;
    - `"optional"` otherwise.
  - `group_of(item: dict) -> str`: `"approval"` for `phase_approval`, `"question"` when it has questions, else `"stop"`.
  - `inbox_view(repo: Path, cfg: Config, now: datetime) -> dict` returns `{items, watch, recent, counts: {approval, question, stop, watch}}`.
    - **`items`** is each `inbox_items` entry plus:
      - `title`, `group`, `run_label`, `age_s` and `approve_note`;
      - `actions`: `available_actions(run)` ∩ `("approve", "answer", "reject")`. A `parked` item gets `["answer"]` only;
      - `text` and `cli`, from `split_cli` of the inbox file read through `_head` with `artifact_max_bytes(cfg)` (`""` and `[]` when it is missing), and `text_truncated`;
      - `facts`: `state.facts`;
      - `artifacts`, for a phase approval only: `[{path, kind, revisions, newest}]` from `run_artifacts` where `phase` is the item's phase;
      - `findings`: the counts by severity of the open `run_findings` rows of steps under the phase, for a phase approval only;
      - `blast_radius`: when the key lies under a plan task of `run_plan`'s `prefix`, `{task, blocks, independent}` from that task, else `None`.
      - Oldest first.
    - **`watch`**: `[{run_id, run_label, reason: "held"|"stalled", since, text}]` for each non-terminal run with `state.hold` set (`text` is the hold's `reason` and `until`), or whose `display_status` is `stalled`. A run with an item is never in `watch`.
    - **`recent`**: `[{run_id, key, phase, action, by, ts}]`, one per `phase_approved`, `hitl_approved`, `checkpoint_cleared`, `hitl_answered` or `phase_rejected` event in the last `RECENT_S` of each non-terminal run, with `by != "runner"`. `action` is one of `approved`, `answered` and `rejected`.
    - A run whose `run_artifacts` or `run_plan` fails (`NotFound`) still gets its card, with those fields empty.

- [ ] **Step 1: Write the failing tests**

```python
@pytest.mark.parametrize("item, title", [
    ({"kind": "phase_approval", "phase": "define", "key": "define_approved", "questions": []}, "Approve the spec"),
    ({"kind": "phase_approval", "phase": "verify", "key": "verify_approved", "questions": []}, "Approve verify"),
    ({"kind": "blocked", "key": "build/build/task-9/implement", "questions": []}, "Task 9 · implement is blocked"),
    ({"kind": "parked", "key": "build/build/task-4/implement", "questions": [{"id": "q1"}, {"id": "q2"}]},
     "Answer 2 questions (Task 4 · implement)"),
    ({"kind": "dirty_tree", "key": "ship/finish", "questions": []}, "ship · finish stopped (dirty_tree)"),
])
def test_items_have_titles_in_words(item, title):
    assert item_title(item) == title

@pytest.mark.parametrize("kind, questions, expected", [
    ("phase_approval", [], "none"), ("checkpoint", [], "none"), ("preflight_failed", [], "none"),
    ("blocked_repeat", [], "required"), ("blocked", [], "optional"), ("needs_human", [{"id": "q"}], "none")])
def test_the_note_follows_what_hitl_approve_accepts(kind, questions, expected):  # Review Focus 1
    assert approve_note({"kind": kind, "questions": questions}) == expected

def test_cli_lines_are_split_off():
    body, cli = split_cli("# r: k\n\nfloor_guard fails.\n\nRun `agentflow approve r k --note \"…\"` to go on.\n\nMore.")
    assert body == "# r: k\n\nfloor_guard fails.\n\nMore." and cli == ["Run `agentflow approve r k --note \"…\"` to go on."]

def test_a_parked_question_and_a_wait_are_two_cards(flow_repo):                   # Review Focus 2
    run = flow_repo.new_run(); run.emit("phase_started", phase="build")
    run.emit("step_parked", key="build/build/task-4/implement", summary="which db?", questions=[
        {"id": "q1", "question": "Which db?", "options": [{"key": "A", "label": "pg", "recommended": True}],
         "evidence": [], "kind": "choice"}])
    run.emit("hitl_raised", kind="blocked", key="build/build/task-9/implement", summary="floor_guard")
    items = inbox_view(flow_repo.root, flow_repo.cfg, datetime.now(UTC))["items"]
    assert sorted((i["kind"], i["actions"]) for i in items) == [("blocked", ["approve", "reject"]), ("parked", ["answer"])]

def test_a_decision_made_elsewhere_is_listed_as_recent(flow_repo):                # Review Focus 3
    run = awaiting(flow_repo, phase="define")
    hitl.approve(run, "define", by="alex")
    (recent,) = inbox_view(flow_repo.root, flow_repo.cfg, datetime.now(UTC))["recent"]
    assert (recent["phase"], recent["action"], recent["by"]) == ("define", "approved", "alex")

def test_a_missing_inbox_file_keeps_the_summary_and_a_huge_one_is_cut(flow_repo): # Review Focus 5
    run = flow_repo.new_run(); run.emit("phase_started", phase="build")
    run.emit("hitl_raised", kind="blocked", key="build/x", summary="why")
    (item,) = inbox_view(flow_repo.root, flow_repo.cfg, datetime.now(UTC))["items"]
    assert item["summary"] == "why" and item["text"] == "" and item["cli"] == []
    run.inbox_path("build/x").write_text("x\n" * 400_000, encoding="utf-8")
    flow_repo.cfg.dashboard["artifact_max_kb"] = 16
    (item,) = inbox_view(flow_repo.root, flow_repo.cfg, datetime.now(UTC))["items"]
    assert item["text_truncated"] is True and len(item["text"].encode()) <= 16 * 1024

def test_a_stop_in_a_plan_task_carries_its_blast_radius(building, flow_repo):     # U2's `building` fixture
    (item,) = inbox_view(building.repo, flow_repo.cfg, datetime.now(UTC))["items"]
    assert item["blast_radius"]["task"] == 2 and item["blast_radius"]["blocks"] == [3, 4]
    assert item["blast_radius"]["independent"] == [5]

def test_a_phase_approval_carries_its_artifacts(spec_run, flow_repo):              # U1's `spec_run`
    spec_run.run.emit("phase_approval_raised", phase="plan", round=0, summary="plan is done")
    (item,) = inbox_view(spec_run.repo, flow_repo.cfg, datetime.now(UTC))["items"]
    assert [(a["path"], a["kind"]) for a in item["artifacts"]] == [("docs/plans/x.md", "plan")]

def test_held_and_stalled_runs_are_watched_without_buttons(flow_repo, monkeypatch):
    run = flow_repo.new_run(); run.emit("phase_started", phase="build")
    run.emit("run_held", key="build/x", reason="usage limit", until="2026-11-16T18:00:00+00:00")
    (row,) = inbox_view(flow_repo.root, flow_repo.cfg, datetime.now(UTC))["watch"]
    assert row["reason"] == "held" and "usage limit" in row["text"]
```

`awaiting` lives in `test_dashboard.py`, and U2's `building` fixture lives in `test_plan_view.py`. Move both to `conftest.py`, as U1–U3 did with their helpers. `spec_run` is already there from U1.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_inbox_view.py --import-mode=prepend -q`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement `inbox_view.py`**

Read each run's events once, and pass them to whatever needs them.

- [ ] **Step 4: Run the tests to verify they pass**

Same command, plus `test_portability.py`. Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agentflow/inbox_view.py agentflow/tests/test_inbox_view.py agentflow/tests/conftest.py agentflow/tests/test_dashboard.py agentflow/tests/test_plan_view.py
git commit -m "Build each inbox item as a titled decision with its text, facts, artifacts and blast radius."
```

---

### Task 3: `GET /api/inbox`

**Files:**
- Modify: `agentflow/dashboard.py`: an `/api/inbox` branch in `_get`, and the docstring.
- Test: `agentflow/tests/test_dashboard.py`.

**Interfaces:**
- Consumes: `inbox_view.inbox_view` (Task 2).
- Produces: `GET /api/inbox` → `inbox_view(repo, cfg, datetime.now(UTC))`. A query parameter is ignored.

- [ ] **Step 1: Write the failing tests**

```python
def test_the_inbox_lists_each_decision(dash):
    run = awaiting(dash.flow, phase="define")
    view = payload(dash.get("/api/inbox")[2])
    (item,) = view["items"]
    assert item["run_id"] == run.id and item["title"] == "Approve the spec" and item["approve_note"] == "none"
    assert view["counts"] == {"approval": 1, "question": 0, "stop": 0, "watch": 0}

def test_the_inbox_needs_the_token_and_our_host(dash):
    assert dash.get("/api/inbox", token=None)[0] == 403
    assert dash.get("/api/inbox", headers={"Host": "evil.example"})[0] == 403
    assert dash.post("/api/inbox")[0] == 404

def test_untrusted_inbox_text_is_escaped_in_json(dash):
    run = dash.flow.new_run(); run.emit("phase_started", phase="build")
    hitl.raise_hitl(dash.flow.cfg, run, "blocked", "build/x", "<script>alert(1)</script>")
    body = dash.get("/api/inbox")[2]
    assert b"<script>" not in body and b"\\u003cscript\\u003e" in body
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_dashboard.py -k "inbox" --import-mode=prepend -q`
Expected: FAIL (`/api/inbox` answers 404).

- [ ] **Step 3: Add the route**

Add to the docstring's route list: "`GET /api/inbox` (each decision waiting on a person, titled, with its context, blast radius and allowed actions; the held and stalled runs to watch; the decisions of the last 30 minutes)".

- [ ] **Step 4: Run the tests to verify they pass**

Run all of `test_dashboard.py`. Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agentflow/dashboard.py agentflow/tests/test_dashboard.py
git commit -m "Serve the inbox of decisions to the dashboard."
```

---

### Task 4: `informed_approval`

**Files:**
- Modify: `agentflow/hitl.py`: `approve` gains `produced`.
- Modify: `agentflow/dashboard.py`: `_approve` computes `produced` for a phase approval.
- Modify: `agentflow/signals.py`: the `informed_approval` family, and the module docstring.
- Modify: `docs/agentflow-playbook.md`: the lesson `target` vocabulary (§15, line 626) gains `informed_approval:<phase>`.
- Modify: `CHANGELOG.md`.
- Test: `agentflow/tests/test_signals.py`, `test_scorecard.py` and `test_dashboard.py`.

**Interfaces:**
- Consumes: U1's `viewed` on `hitl.approve` and `artifacts.run_artifacts`.
- Produces:
  - `hitl.approve(..., viewed=None, produced: list[dict] | None = None)`. On a phase approval, `produced` is recorded as `phase_approved.produced` when it isn't `None` (an empty list included, which means the phase produced nothing). It is ignored on other approvals.
  - **The dashboard's `_approve`, for a phase approval:** `produced = [{"path": a["path"], "sha": a["revisions"][-1]["sha"]} for a in run_artifacts(run, repo) if a["phase"] == phase]`. A `NotFound` from `run_artifacts` sends no `produced`.
  - `signals.FAMILIES["informed_approval"] = ("higher", True)`, whose parameter is a phase. It is not in `COUNT_FAMILIES`.
  - In `run_signals`, for each phase with at least one counted approval, `informed_approval:<phase>` is the share of them that are informed, rounded to 4 places.
    - **Counted:** a `phase_approved` with `by` not `"runner"` and `produced` not `[]`. A CLI approval has no `produced`, so it counts, as uninformed.
    - **Informed:** a non-empty `produced` that is a subset of `viewed`, comparing `(path, sha)` pairs.

- [ ] **Step 1: Write the failing tests**

```python
# test_signals.py
def approved(phase, by="dashboard", **kw):
    return {"type": "phase_approved", "phase": phase, "by": by, **kw}

def test_informed_approval_is_the_share_of_human_approvals_that_viewed_every_newest_revision():
    spec = {"path": "s.md", "sha": "a" * 40}
    events = [approved("define", produced=[spec], viewed=[spec, {"path": "x", "sha": "b" * 40}]),
              approved("define", produced=[spec], viewed=[]),
              approved("plan", by="alex"),                       # the CLI: no produced → counted, not informed
              approved("plan", produced=[]),                      # nothing produced → not counted
              approved("build", by="runner", produced=[spec])]   # the runner's → never counted
    got = run_signals(events, {"phases": []})
    assert got["informed_approval:define"] == 0.5 and got["informed_approval:plan"] == 0.0
    assert "informed_approval:build" not in got
    assert parse_target("informed_approval:define") == ("informed_approval", "define")

# test_dashboard.py
def test_a_dashboard_phase_approval_records_what_the_phase_produced(dash, spec_run):
    spec_run.run.emit("phase_approval_raised", phase="plan", round=0, summary="plan is done")
    newest = spec_run.revisions("docs/plans/x.md")[-1]["sha"]
    viewed = [{"path": "docs/plans/x.md", "sha": newest}]
    assert dash.post(f"/api/runs/{spec_run.run.id}/approve", {"viewed": viewed})[0] == 200
    event = [e for e in spec_run.run.events.read() if e["type"] == "phase_approved"][-1]
    assert event["produced"] == viewed and event["viewed"] == viewed

# test_scorecard.py, in the shape of test_run_row_carries_the_throughput_signals
def test_informed_approval_reaches_the_scorecard_row(flow_repo):
    spec = {"path": "s.md", "sha": "a" * 40}
    run = flow_repo.new_run()
    run.emit("phase_started", phase="define")
    run.emit("phase_approval_raised", phase="define", round=0, summary="s")
    run.emit("phase_approved", phase="define", by="dashboard", produced=[spec], viewed=[spec])
    run.emit("run_finished")
    assert scorecard.run_row(run, datetime.now(UTC))["informed_approval:define"] == 1.0
```

`scorecard_view` builds its rows from `run_row` through `aggregate_signals` and `_compare`, which keep every key. So the row reaching `run_row` is the whole path, and `test_compare_marks_direction_and_slo` already covers the comparison.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_signals.py agentflow/tests/test_scorecard.py agentflow/tests/test_dashboard.py -k "informed" --import-mode=prepend -q`
Expected: FAIL.

- [ ] **Step 3: Implement `produced` and the family**

Add to the `signals` docstring: "`informed_approval:<phase>` is the share of a person's approvals of the phase whose `viewed` (the revisions the dashboard showed) covers every newest revision the phase produced. A CLI approval counts as uninformed, and an approval of a phase that produced nothing isn't counted. Present only for a phase with a counted approval."

- [ ] **Step 4: Run the tests to verify they pass**

Run the same command, then `test_lessons.py`, `test_retro.py` and `bin/agentflow validate`. Expected: PASS, and `0 warnings`.

- [ ] **Step 5: Add the changelog line and commit**

Under `### Added`: "`informed_approval:<phase>` in signals, `agentflow scorecard` and the Metrics tab: the share of a person's phase approvals that viewed every newest revision the phase produced on the dashboard."

```bash
git add agentflow/hitl.py agentflow/dashboard.py agentflow/signals.py docs/agentflow-playbook.md agentflow/tests CHANGELOG.md
git commit -m "Measure informed approvals: record what a phase produced on its dashboard approval and compare it with what was viewed."
```

---

### Task 5: The Inbox tab, its cards and forms

**Files:**
- Create: `agentflow/dashboard_ui/inbox.js`, added to `UI_SCRIPTS` after `sessions.js`.
- Modify:
  - `agentflow/dashboard.html`: a `#tab-inbox` header button with a `#inbox-count` badge, before `#tab-board`; a `<section id="inbox-view" hidden>`; the card styles;
  - `agentflow/dashboard_ui/main.js`: `VIEWS` gains `"inbox"`, and the badge updates on each board refresh;
  - `agentflow/dashboard_ui/board.js`: the Needs-human list renders only when `inbox_legacy`, otherwise one line, "<n> decisions wait in the Inbox", linking to `#/inbox`.
- Modify: `docs/agentflow-playbook.md`, line 329 and the "Needs human" mentions. Add a sentence on the Inbox.
- Modify: `CHANGELOG.md`.
- Test: `agentflow/tests/test_dashboard_js.py` and `agentflow/tests/test_dashboard.py`.

**Interfaces:**
- Consumes: `/api/inbox` (Task 3), `inbox_legacy` from `/api/info` (Task 1), U1's `parseMarkdown`/`markdownNodes` and `viewedList`, `act`, `goRun`, `ago` and `patchChildren`.
- Produces, in `inbox.js`. The pure helpers sit at the top, with no DOM at load time.
  - `answerDefaults(questions) -> {[qid]: string}`: each question's `recommended` option key, else `""`.
  - `approvePayload(item, form: {note: string}) -> {ok: true, body} | {ok: false, error}`:
    - `approve_note === "required"` with a blank note gives `{ok: false, error: "This stop blocked again with nothing changed: say what changed or what to do."}`;
    - otherwise `body = {target, note?}`, where `target` is the phase for a phase approval, else `key`;
    - the note is sent only when `approve_note !== "none"` and it isn't blank;
    - a phase approval adds `viewed: viewedList(run_id)` when that list isn't empty.
  - `answerPayload(item, form: {[qid]: {choice: string, other: string}}) -> {ok, body | error}`:
    - `body = {key: item.key, answers}`, where each answer is `other.trim()` when it isn't blank, else `choice`;
    - with no answer at all: `{ok: false, error: "Choose or write at least one answer."}`.
  - `rejectPayload(item, note: string) -> {ok, body | error}`: blank gives `{ok: false, error: "A rejection needs a note."}`; otherwise `{target, note}`.
  - **UI state:** `ui.inbox = {view, drafts: Map<itemKey, {note, answers, reject}>, armed: itemKey|null, focus: index, group: "run"|"kind"}`, where `itemKey = run_id + "\0" + key`. Drafts live outside the nodes, so a patch rebuilds a card with what was typed.
  - `inboxCard(item) -> Element`, keyed `item:<itemKey>`. It holds:
    - **Head:** the title, then `run_label · phase · ago(age_s)`, plus `"blocks <n> task(s)"` when `blast_radius.blocks` isn't empty.
    - **Body:**
      - the summary;
      - `details` "Full text", holding the rendered `text` (with "Showing the first N KB" when `text_truncated`);
      - `details` "CLI equivalent", holding `cli` as `<pre>`;
      - `details` "Facts the resolvers found (<n>)", when `facts` isn't empty.
    - **Phase approval:** one line per artifact, `<path> · <revisions> revisions`, the open findings by severity, and a **Read the spec/plan** button to `#/run/<id>/<kind>/<path>`.
    - **Stop in a task:** `"blocks: " + ranges(blocks) + " · independent: " + ranges(independent)`, using U2's `ranges`.
    - **Forms:**
      - `approve_note` `"required"` or `"optional"` gives a textarea, "Note to the agent (what changed or what to do)", marked required when required;
      - each question gives a radio group of its options, with the recommended one checked and labelled "(recommended)", plus an "Other" text field and its evidence. §4.6 also asks for the recommendation's rationale, but `hitl.Option` has only `key`, `label` and `recommended`. So the card shows the question's `evidence` instead. Adding a rationale would change the result schema, which is outside this slice;
      - a **Request changes…** toggle opens a reject textarea in the card.
    - **Buttons**, only those in `actions`: **Approve** / **Approve and resume** (for a stop), **Answer**, **Request changes…**, and always **Open run** (to `#/run/<id>/overview/decision`).
      - A refused payload shows its `error` on the card and sends nothing.
      - A click on Approve arms the button ("Confirm approve") for 4 s, and a second click sends.
  - `renderInbox(view)` patches `#inbox-view` by `item:<itemKey>`, with groups (Task 6), then the "Watch" group: one muted row per `watch` entry, with no buttons.

- [ ] **Step 1: Write the failing tests**

```python
# test_dashboard_js.py
def test_a_repeated_block_cannot_be_approved_without_a_note():   # spec Verification U4, Review Focus 1
    item = {"run_id": "r", "kind": "blocked_repeat", "key": "build/x", "approve_note": "required"}
    refused = js(["inbox.js"], "approvePayload(args, {note: '   '})", item)
    assert refused["ok"] is False and "blocked again" in refused["error"]
    assert js(["inbox.js"], "approvePayload(args, {note: 'fixed the fixture'})", item) == \
        {"ok": True, "body": {"target": "build/x", "note": "fixed the fixture"}}

def test_a_phase_approval_never_sends_a_note():
    item = {"run_id": "r", "kind": "phase_approval", "phase": "define", "key": "define_approved", "approve_note": "none"}
    assert js(["inbox.js"], "approvePayload(args, {note: 'x'})", item) == {"ok": True, "body": {"target": "define"}}

def test_questions_start_on_their_recommended_option():           # spec Verification U4
    qs = [{"id": "q1", "options": [{"key": "A"}, {"key": "B", "recommended": True}]}, {"id": "q2", "options": []}]
    assert js(["inbox.js"], "answerDefaults(args)", qs) == {"q1": "B", "q2": ""}

def test_an_answer_posts_its_own_key_and_prefers_written_text():  # Review Focus 2
    item = {"key": "build/build/task-4/implement"}
    got = js(["inbox.js"], "answerPayload(args, {q1: {choice: 'B', other: ''}, q2: {choice: '', other: ' pg '}})", item)
    assert got == {"ok": True, "body": {"key": "build/build/task-4/implement", "answers": {"q1": "B", "q2": "pg"}}}

# test_dashboard.py
def test_the_inbox_is_a_tab_with_a_badge_and_no_browser_prompts():
    page = page_source()
    assert 'id="tab-inbox"' in page and 'id="inbox-count"' in page and 'id="inbox-view"' in page
    inbox = (UI_DIR / "inbox.js").read_text(encoding="utf-8")
    for needle in ("/api/inbox", "Approve and resume", "Request changes", "CLI equivalent", "Facts the resolvers found",
                   "(recommended)", "Confirm approve", "Watch", "ui.inbox"):
        assert needle in inbox, needle
    assert "prompt(" not in inbox and "confirm(" not in inbox
    assert '"inbox"' in (UI_DIR / "main.js").read_text(encoding="utf-8")
    assert "inbox_legacy" in (UI_DIR / "board.js").read_text(encoding="utf-8")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_dashboard_js.py agentflow/tests/test_dashboard.py -k "note or recommended or own_key or inbox_is_a_tab" --import-mode=prepend -q`
Expected: FAIL.

- [ ] **Step 3: Implement the tab, the cards and the forms**

Refetch `/api/inbox` through the same `throttle` as the Board, while the Inbox view is shown and a non-`agent_turn` event arrived. The badge reads the Board's `needs_human.length`, which every view already refreshes.

- [ ] **Step 4: Run the tests, then check in a browser**

Run `test_dashboard.py` and `test_dashboard_js.py`. Expected: PASS, with no node test skipped.

In a browser, with three scratch runs waiting (a `define` approval, a parked question, a `blocked_repeat` stop):
- the header reads `Inbox (3)` on every view;
- the spec card's **Read the spec** opens U1's Spec tab;
- the question card starts on the recommended option;
- **Approve and resume** on the `blocked_repeat` card with an empty note shows the error, and the Network panel shows no request.

- [ ] **Step 5: Add the changelog line and commit**

Under `### Changed`: "Decisions waiting on you move from the Board's Needs-human list to an Inbox tab with a count in the header. Each card has a title in words, the inbox text with its CLI lines folded away, the facts and artifacts it needs, what a stop holds up, and forms in place of the browser's prompt boxes. `dashboard.inbox_legacy: true` brings the old list back."

```bash
git add agentflow/dashboard_ui agentflow/dashboard.html agentflow/dashboard.py agentflow/tests docs/agentflow-playbook.md CHANGELOG.md
git commit -m "Turn Needs human into an Inbox tab of titled decision cards with their context and forms in place."
```

---

### Task 6: Grouping, the keyboard, and decisions made elsewhere

**Files:**
- Modify: `agentflow/dashboard_ui/inbox.js`.
- Modify: `agentflow/dashboard.html`: the group switch, and the focus ring of the focused card.
- Modify: `CHANGELOG.md`.
- Test: `agentflow/tests/test_dashboard_js.py` and `agentflow/tests/test_dashboard.py`.

**Interfaces:**
- Consumes: Task 5's `ui.inbox`, `inboxCard` and `renderInbox`, and `/api/inbox`'s `recent`.
- Produces, in `inbox.js`:
  - `groupItems(items, by: "run"|"kind") -> [{key, label, items}]`:
    - by run, one group per run in the order of its oldest item, labelled with `run_label`;
    - by kind, the groups `approval` ("Approvals"), `question` ("Questions") and `stop` ("Stops"), in that order, with empty ones dropped.
    - Items keep their oldest-first order.
  - `goneText(item, recent: object[]) -> string|null`. For an item no longer in `items`, it finds the newest `recent` entry with the same `run_id` and the same `key`, or the same `phase` for a phase approval.
    - It reads `"<Approved|Answered|Rejected> <where> at <HH:MM>"`, where `<where>` is `"from the dashboard"` for `by === "dashboard"`, `"by the runner"` for `"runner"`, else `"from the CLI by <by>"`.
    - With no entry, it reads `"No longer waiting on you"`.
  - **Kept cards.** A card whose item is gone, but which has a non-empty draft, stays in place:
    - it gets class `gone` and the `goneText` line;
    - its fields are `readOnly`, so the text stays selectable and copyable;
    - its buttons are removed;
    - **Dismiss** drops it and its draft.
  - `inboxKey(ev: KeyboardEvent) -> void`, bound on `document` while the Inbox is shown. It does nothing when `ev.target` is an input, a textarea, a select or a `contenteditable`, when `#dlg` is open, or when a modifier key is held.
    - `j`/`k` move `ui.inbox.focus` and scroll the card into view;
    - `o` opens the run;
    - `r` opens the card's Request changes textarea and focuses it;
    - `a` arms the focused card's approval (`ui.inbox.armed`, shown as "Press a again to approve"), and a second `a` within 4000 ms sends it, through `approvePayload`;
    - any other key, or the 4 s passing, disarms.
  - Every shortcut has a visible button. A one-line hint under the header reads "j/k move · o open · a approve (press twice) · r request changes".

- [ ] **Step 1: Write the failing tests**

```python
# test_dashboard_js.py
def test_items_group_by_run_or_by_kind():
    items = [{"run_id": "r1", "run_label": "a", "group": "stop", "key": "1"},
             {"run_id": "r2", "run_label": "b", "group": "approval", "key": "2"},
             {"run_id": "r1", "run_label": "a", "group": "question", "key": "3"}]
    by_run = js(["inbox.js"], "groupItems(args, 'run').map(g => [g.label, g.items.map(i => i.key)])", items)
    by_kind = js(["inbox.js"], "groupItems(args, 'kind').map(g => [g.label, g.items.map(i => i.key)])", items)
    assert by_run == [["a", ["1", "3"]], ["b", ["2"]]]
    assert by_kind == [["Approvals", ["2"]], ["Questions", ["3"]], ["Stops", ["1"]]]

def test_a_card_decided_elsewhere_says_by_whom():                  # spec Verification U4, Review Focus 3
    item = {"run_id": "r", "kind": "phase_approval", "phase": "define", "key": "define_approved"}
    recent = [{"run_id": "r", "phase": "define", "key": None, "action": "approved", "by": "alex",
               "ts": "2026-11-16T14:02:10+00:00"}]
    text = js(["inbox.js"], "goneText(args.item, args.recent)", {"item": item, "recent": recent})
    assert text.startswith("Approved from the CLI by alex at ")
    assert js(["inbox.js"], "goneText(args, [])", item) == "No longer waiting on you"

# test_dashboard.py
def test_inbox_shortcuts_stay_out_of_text_fields_and_approve_takes_two_presses():  # Review Focus 4
    inbox = (UI_DIR / "inbox.js").read_text(encoding="utf-8")
    handler = inbox[inbox.index("function inboxKey"):inbox.index("\n}\n", inbox.index("function inboxKey"))]
    for needle in ("INPUT", "TEXTAREA", "SELECT", "isContentEditable", "dlg", "4000", "armed"):
        assert needle in handler, needle
    assert "Press a again to approve" in inbox and "Dismiss" in inbox and "readOnly" in inbox
```

The time in `goneText` is formatted locally (`HH:MM`), so the test checks only the start of the line.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_dashboard_js.py agentflow/tests/test_dashboard.py -k "group_by_run or decided_elsewhere or shortcuts" --import-mode=prepend -q`
Expected: FAIL.

- [ ] **Step 3: Implement grouping, the keyboard and kept cards**

- [ ] **Step 4: Run the tests, then check in a browser**

Run `test_dashboard.py`, `test_dashboard_js.py` and `test_portability.py`. Expected: PASS.

In a browser:
- type a note on a `blocked` card, then `bin/agentflow approve <run> <key>` from a terminal. Within a refresh, the card reads "Approved from the CLI by <you> at HH:MM", and the note is still there and selectable;
- `j`/`k` move the focus, and typing `a` in the note field types an "a";
- `a`, `a` approves the focused card; a single `a` doesn't;
- group by kind lists the approvals together.

- [ ] **Step 5: Add the changelog line and commit**

Under `### Added`: "The Inbox groups decisions by run or by kind, takes `j`/`k`/`o`/`a`/`r` from the keyboard (approve takes two presses), and keeps a card you were typing on when the decision is made elsewhere, saying who made it."

```bash
git add agentflow/dashboard_ui agentflow/dashboard.html agentflow/tests CHANGELOG.md
git commit -m "Group the Inbox, add its keyboard, and keep a card you typed on when someone else decides it."
```

---

### Task 7: Whole-slice verification

There's no new code in this task. If a check fails, fix it in the task that owns the code.

- [ ] **Step 1:** Run `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests --import-mode=prepend -q`. Expected: all pass, with no node test skipped on a machine that has node. This is the full suite, which takes 25–60 min.
- [ ] **Step 2:** Run `bin/agentflow validate`. Expected: `0 warnings`.
- [ ] **Step 3:** Run `git diff main -- agentflow/tests/fixtures/explain`. Expected: empty.
- [ ] **Step 4:** Run `git diff main -- agentflow/dashboard.py | grep -n PAGE_CSP`. Expected: no changed line.
- [ ] **Step 5:** Walk Verification "U4" from the spec in a browser, with a scratch `XDG_STATE_HOME`:
  - a `blocked_repeat` card refuses an empty note before any request;
  - a question starts on its recommended option;
  - a CLI approval while the card is open shows "Approved from the CLI…" and keeps the typed text;
  - `agentflow scorecard` lists `informed_approval:define` after one informed dashboard approval of a finished run.
- [ ] **Step 6:** Set `dashboard.inbox_legacy: true` on the Settings page. The Board shows the old Needs-human list, and the Inbox tab still works.
