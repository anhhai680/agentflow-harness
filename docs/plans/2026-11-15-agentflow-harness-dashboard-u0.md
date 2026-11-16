# Dashboard slice U0 (Foundations) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop the dashboard fighting its reader, and give every run an address. Live updates patch the region that changed instead of rebuilding it. A run opens on its own Run page at `#/run/<id>/<tab>`. Buttons appear only when the run's state allows them.

**Architecture:**
- **Page split (Open Question 1, taking the proposal's answer: split in U0).** The page's one inline script moves to `agentflow/dashboard_ui/*.js`. `dashboard.py` serves those files from a fixed allow-list at `/ui/<name>.js`, with no token (they hold no data) but with the Host and Origin check. Each `<script>` tag carries the page's nonce, so the CSP stays the same. The folder is `dashboard_ui/`, not the proposal's `dashboard/`, because `agentflow/dashboard/` would sit beside the module `agentflow/dashboard.py`.
- **Server.**
  - `run_control.available_actions(run)` lists the actions the run's state allows now, mirroring each action's own refusals.
  - `views.run_detail` stops embedding raw `state`. It returns the fields the page uses, plus `actions`, `label`, `stage_rail` and `live_agents`. `?raw=1` keeps the old `state`.
  - `views.board` caches each run's digest and metrics by the event log's size and mtime.
  - `/api/events?quiet=1` coalesces `agent_turn` into `agent_activity`, at most one message every 2 s per session.
- **Page.**
  - A hash router covers the six existing views and `#/run/<id>/<tab>[/<sub>]`.
  - A Run page has a header, a waiting banner, a stage rail, state-driven actions and four tabs: Overview, Activity, Gates and Files. Activity, Gates and Files carry the old drawer sections until U1–U5 replace them.
  - The drawer becomes a peek.
  - `patchChildren` re-renders only the keyed rows whose data changed, so open `<details>` elements and the scroll position survive.
  - SSE messages are bucketed by run. The Board refetches at most every `dashboard.board_refresh_s`.

**Tech Stack:** Python 3.12 stdlib, pytest, plain browser JavaScript (no framework, no build step).

**Spec:** `docs/specs/agentflow-dashboard-ux-proposal.md`. This plan covers slice **U0** (§7):
- §4.1, with the drawer as a peek;
- §4.2: the Run page header, banner, stage rail, actions and Overview;
- §4.7: only the click / Space / badge split between the Run page and the peek;
- §4.10: all of it;
- §4.11: only the changed `GET /api/runs/<id>`;
- the `dashboard.board_refresh_s` key from §8;
- Verification "U0".

Every other route, tab and key belongs to U1–U5. The spec is `Status: approved` (2026-11-15).

## Global Constraints

- Engine dependencies stay stdlib + `pyyaml>=6`. No JavaScript framework, bundler or CDN. The page stays plain JavaScript with no build step.
- `PAGE_CSP` in `dashboard.py` stays byte-identical: `script-src 'nonce-…'`, no `'unsafe-inline'`, no `'unsafe-eval'`.
- The dashboard still binds `127.0.0.1` only. Every data route still needs the token plus the Host and Origin checks. `/ui/<name>.js` needs no token, holds no run data, and still gets the Host and Origin check and the security headers.
- Every string that comes from a run is set with `textContent`. No `innerHTML`, `outerHTML`, `insertAdjacentHTML`, `eval`, `new Function`, inline `style="` or `javascript:` in any page file. The existing sink scan covers every `dashboard_ui/*.js` file.
- No project strings in the engine (`test_portability.py` `DENYLIST`). Don't name the repository the spec measured: call it "the measured repository" in code, tests, docs and commits.
- No runner behaviour changes. U0 adds no write path: the only new code in `run_control.py` is a read-only predicate.
- The golden files `agentflow/tests/fixtures/explain/*.txt` stay byte-identical.
- The new config key `dashboard.board_refresh_s` ships with all four companions: a Settings form field, a `validate` check, a `test_config_edit.py` case and a playbook §14 line.
- `CHANGELOG.md` gets one line under `## [Unreleased]` for each user-visible change, as each task says.
- Never create, edit or delete `uv.lock`. Run everything with `uv run --no-project`.
- Test command: `uv run --no-project --with pyyaml --with pytest pytest <path> --import-mode=prepend -q`.
- A commit message is one imperative sentence and ends with the pack's `project.commit_trailer`.

## Review Focus

1. **Reloading a deep link.** Today's boot code strips *any* hash (`if (location.hash) history.replaceState(…)`), so `#/run/<id>/gates` would be lost on reload. Only a `#token=` hash may be stripped. The test goes in Task 7.
2. **A hash naming a run that isn't there**: a typo, a deleted run directory or a stale bookmark. The Run page must show "No run <id>" with a way back to the Board. It must not show a blank page or retry a 404 on every event. The test goes in Task 7.
3. **Missed events across an SSE reconnect.** While the stream is down, events are lost. On `onopen`, the page must mark the Board, the Agents tab and the open run dirty, and refetch them once. Bucketing must not drop messages for runs the page hasn't seen yet, nor `backlog` messages. The test goes in Task 8.
4. **A stalled or ready run.** A stalled run must still offer **Resume**, because that is how the runner restarts. A ready run must offer **Resume** and **Mark done**, and never **Pause** or **Retry**. A retro run must offer only **Pause** and **Cancel**. The test goes in Task 2.
5. **A quiet live run's cached figures.** Once its log stops changing, the cache would freeze its wall time. A live run's cached digest expires after `DIGEST_LIVE_TTL_S`, and a log that shrinks (replaced) misses the cache. The test goes in Task 4.

---

### Task 1: Serve the page script as static files

A pure move. No behaviour changes, so a reviewer can approve the split on its own.

**Files:**
- Create: the `agentflow/dashboard_ui/` folder with `core.js`, `dialogs.js`, `board.js`, `drawer.js`, `workflow.js`, `agents.js`, `metrics.js`, `lessons.js`, `settings.js` and `main.js`.
- Modify: `agentflow/dashboard.html`. The inline `<script>` (lines 258–1553) becomes `{{SCRIPTS}}`, and the CSS stays inline.
- Modify: `agentflow/dashboard.py`: `UI_DIR`, `UI_SCRIPTS`, `UI_ROUTE`, `_page()`, `do_GET`, and the docstring's route list.
- Test: `agentflow/tests/test_dashboard.py`.

**Interfaces:**
- Produces:
  - `dashboard.UI_DIR: Path`, which is `Path(__file__).resolve().with_name("dashboard_ui")`.
  - `dashboard.UI_SCRIPTS: tuple[str, ...]`, which is `("core.js", "dialogs.js", "board.js", "drawer.js", "workflow.js", "agents.js", "metrics.js", "lessons.js", "settings.js", "main.js")`, in load order.
  - `UI_ROUTE = re.compile(r"^/ui/([a-z_]+\.js)$")`.
  - In `test_dashboard.py`, `page_source() -> str`: `dashboard.html` followed by each `UI_SCRIPTS` file in order. Every later task's source assertions use it.
- Where the code goes: split by the existing `// ------ <section>` comments.
  - `core.js`: everything from `"use strict"` through `arrow`, which is the token boot, `ui`, `$`, `el`, `toast`, `api`, `act`, `ago`, `money`, `limits` and `arrow`.
  - `dialogs.js`: `dialog`, `rejectDialog` and `answerDialog`.
  - `board.js`: the board and backlog sections, `needNode` and `newTaskDialog`.
  - `drawer.js`: `section`, `describe`, `openDrawer` and `closeDrawer`.
  - `workflow.js`, `agents.js`, `metrics.js` (with `metricsSection`), `lessons.js` and `settings.js`: their sections.
  - `main.js`: "refresh and live updates", `VIEWS`, `show`, the listeners and the boot lines.
  - Each file starts with `"use strict";`. They are classic scripts, so their top-level `const`s share one global scope.

- [ ] **Step 1: Write the failing tests**

```python
from agentflow.dashboard import UI_DIR, UI_SCRIPTS

def page_source() -> str:
    return "\n".join([(AGENTFLOW / "dashboard.html").read_text(encoding="utf-8"),
                      *((UI_DIR / name).read_text(encoding="utf-8") for name in UI_SCRIPTS)])

def test_the_page_loads_each_ui_script_once_in_order(dash):
    status, headers, body = dash.get("/", token=None)
    nonce = re.search(r"'nonce-([A-Za-z0-9_\-]+)'", headers["content-security-policy"]).group(1)
    tags = re.findall(r'<script nonce="([^"]+)" src="/ui/([a-z_]+\.js)"></script>', body.decode("utf-8"))
    assert tags == [(nonce, name) for name in UI_SCRIPTS]
    assert "<script nonce" not in body.decode("utf-8").replace('<script nonce="%s" src=' % nonce, "")  # no inline script

def test_every_ui_file_is_listed():
    assert sorted(p.name for p in UI_DIR.glob("*.js")) == sorted(UI_SCRIPTS)
    assert UI_SCRIPTS[0] == "core.js" and UI_SCRIPTS[-1] == "main.js"

def test_ui_scripts_need_no_token_but_keep_the_host_check(dash):
    status, headers, body = dash.get("/ui/core.js", token=None)
    assert status == 200 and headers["content-type"] == "text/javascript; charset=utf-8"
    assert headers["cache-control"] == "no-store" and headers["x-content-type-options"] == "nosniff"
    assert b"const TOKEN" in body
    assert dash.get("/ui/core.js", token=None, headers={"Host": "evil.example"})[0] == 403

@pytest.mark.parametrize("path", ["/ui/evil.js", "/ui/../dashboard.py", "/ui/%2e%2e/dashboard.py",
                                  "/ui/core.js/", "/ui/CORE.js", "/ui/dashboard.html"])
def test_only_listed_ui_scripts_are_served(dash, path):
    assert dash.get(path)[0] == 404
```

Also replace every `(AGENTFLOW / "dashboard.html").read_text(encoding="utf-8")` and `(Path(__file__).parents[1] / "dashboard.html").read_text(...)` in `test_dashboard.py` (20 places) with `page_source()`. In `test_page_is_served_with_a_nonce_and_no_unsafe_dom_sinks`, change the `f'<script nonce="{nonce}">'` assertion to `f'<script nonce="{nonce}" src="/ui/core.js"></script>' in page`. Add `assert PAGE_CSP == ("default-src 'none'; script-src 'nonce-{n}'; style-src 'nonce-{n}'; connect-src 'self'; img-src data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")`, which pins the CSP for every later task.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_dashboard.py --import-mode=prepend -q`
Expected: FAIL with `ImportError: cannot import name 'UI_DIR'`.

- [ ] **Step 3: Move the script and add the route**

- `_page()` replaces `{{SCRIPTS}}` with one `<script nonce="{nonce}" src="/ui/{name}"></script>` per `UI_SCRIPTS` entry, joined by `"\n"`. It does this before the existing `{{NONCE}}` replacement.
- In `do_GET`, a path matching `UI_ROUTE` is handled before `_handle`:
  - not `_same_site()` → an empty 403;
  - a name not in `UI_SCRIPTS` → `_error(404, "not found")`;
  - otherwise `(UI_DIR / name).read_bytes()`, sent as `text/javascript; charset=utf-8` with `API_CSP`.
- Add the route to the module docstring: "`GET /ui/<name>.js`, the page's scripts, from a fixed list; they hold no run data, so they need no token".

- [ ] **Step 4: Run the tests to verify they pass**

Run the same command. Expected: PASS, with every existing page-source test green on `page_source()`.

Then check the wheel carries the folder:
`uvx hatch build -t wheel "$TMPDIR/af-wheel" && python3 -m zipfile -l "$TMPDIR"/af-wheel/agentflow-*.whl | grep dashboard_ui/main.js`.
Expected: one line.

- [ ] **Step 5: Smoke-test in a browser**

Run `bin/agentflow dashboard` and open the printed URL. Each of the six tabs renders, the console shows no CSP error, and a card still opens the drawer.

- [ ] **Step 6: Commit**

```bash
git add agentflow/dashboard_ui agentflow/dashboard.html agentflow/dashboard.py agentflow/tests/test_dashboard.py
git commit -m "Serve the dashboard's script as static files from a fixed list, each with the page's nonce."
```

---

### Task 2: List the actions a run's state allows

**Files:**
- Modify: `agentflow/run_control.py`, adding `RUN_ACTIONS`, `available_actions` and `__all__` if present.
- Test: `agentflow/tests/test_dashboard.py`, which uses the `dash` fixture so each action goes through the real route.

**Interfaces:**
- Produces:
  - `run_control.RUN_ACTIONS = ("approve", "answer", "reject", "resume", "done", "pause", "retry", "cancel")`.
  - `run_control.available_actions(run: Run) -> list[str]`, a subset of `RUN_ACTIONS` in that order. Read-only: it emits nothing and takes no lock.
- The rules mirror each action's refusal, and nothing else:
  - **A terminal status** (`TERMINAL`): `[]`.
  - **`approve`:** the run waits (`state["waiting"]`), the wait has no `questions`, and the run isn't a retro run.
  - **`answer`:** `hitl.pending_item(run)` returns an item with `questions`, and the run isn't a retro run.
  - **`reject`:** the run waits, the wait's kind isn't `environment` (the check `hitl.reject` makes, `hitl.py:698`), and the run isn't a retro run.
  - **`resume`:** not a retro run, and either `status in ("paused", "ready")` or `display_status(run) == "stalled"`.
  - **`done`:** `status == "ready"`, `state["ready"]` is set, the worktree directory exists, and `_has_work(worktree, base_sha)`. A `ControlError` from `_has_work` means `done` isn't listed.
  - **`pause`:** `status not in ("paused", "ready")`.
  - **`retry`:**
    - status isn't `ready`, and the run isn't a retro run;
    - some step is `failed`, and `lock_state(run.dir) != "live"`;
    - and the run doesn't wait on that newest failed step with `phase_approval` or `questions`.
  - **`cancel`:** always, for a non-terminal run.

- [ ] **Step 1: Write the failing test**

Write one parametrized test. Each case builds a fresh run, reads `available_actions`, posts the action through the route, and checks the answer.

```python
BODIES = {"approve": {"note": "go"}, "reject": {"note": "redo"}, "answer": {"answers": {"q1": "A"}}}
ALWAYS_REFUSED_UNLISTED = ("pause", "done", "retry", "cancel")  # these routes answer 409 when the state is wrong

@pytest.mark.parametrize("build, listed", [
    ("paused",         ["resume", "cancel"]),
    ("ready",          ["resume", "cancel"]),               # no work in the checkout → no done
    ("ready_with_work",["resume", "done", "cancel"]),
    ("phase_approval", ["approve", "reject", "pause", "cancel"]),
    ("questions",      ["answer", "reject", "pause", "cancel"]),
    ("stalled",        ["resume", "pause", "cancel"]),
    ("failed_step",    ["resume", "pause", "retry", "cancel"]),
    ("done",           []),
    ("cancelled",      []),
])
@pytest.mark.parametrize("action", RUN_ACTIONS)
def test_listed_actions_are_never_refused_and_unlisted_ones_are(dash, build, listed, action):
    run = BUILDERS[build](dash.flow)
    assert available_actions(run) == listed
    waits = run.state["waiting"] is not None
    status, _, body = dash.post(f"/api/runs/{run.id}/{action}", BODIES.get(action, {}))
    if action in listed:
        assert status != 409, payload(body)
    elif action in ALWAYS_REFUSED_UNLISTED or (action in ("approve", "reject") and not waits):
        assert status == 409, (status, payload(body))
    # an unlisted resume still answers 200 (it starts a runner): that wrong press is what hiding the button prevents
```

Build `BUILDERS` from the existing helpers:
- `ready` and `with_work(ready(...))`;
- `awaiting` for `phase_approval`;
- `hitl.raise_hitl(... questions ...)` for `questions`, as `test_dashboard_answer` does;
- `phase_started` + `step_started` with no lock for `stalled`;
- a `step_finished` with `status="failed"` (the event `retry` reads) for `failed_step`;
- `finished_run` for `done`;
- `cancel(run, "t")` for `cancelled`.

Fix the `listed` values of `stalled` and `failed_step` to what the builders produce. If a builder's run displays `stalled`, `resume` belongs in its list.

Add a separate test: a retro run (`Run.create(cfg, root, RETRO_PIPELINE, {})` + `phase_started`) lists exactly `["pause", "cancel"]`.

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_dashboard.py -k "listed_actions or retro_run_lists" --import-mode=prepend -q`
Expected: FAIL with `ImportError: cannot import name 'available_actions'`.

- [ ] **Step 3: Implement `available_actions(run: Run) -> list[str]` in `agentflow/run_control.py`**

Put it beside the refusals it mirrors. Import `display_status` from `agentflow.state`, and import `hitl` inside the function if a module-level import would cycle.

- [ ] **Step 4: Run the test to verify it passes**

Same command. Expected: PASS. If a `listed` row disagrees with a route, fix the rule (the rule must match the route), not the expectation.

- [ ] **Step 5: Commit**

```bash
git add agentflow/run_control.py agentflow/tests/test_dashboard.py
git commit -m "List the actions a run's state allows, matching each action's own refusals."
```

---

### Task 3: A slim run detail with labels, a stage rail and actions

**Files:**
- Modify: `agentflow/views.py`: `step_title`, `stage_rail`, `run_detail`, and `__all__`.
- Modify: `agentflow/dashboard.py`: the `RUN_ROUTE` branch reads `?raw=1`, plus the docstring.
- Modify: `agentflow/dashboard_ui/drawer.js` and `agentflow/dashboard_ui/core.js`, so the drawer reads `detail.facts`/`detail.steps`/`detail.waiting` instead of `detail.state`.
- Modify: `CHANGELOG.md`.
- Test: `agentflow/tests/test_dashboard.py`.

**Interfaces:**
- Consumes: `run_control.available_actions(run) -> list[str]` (Task 2).
- Produces:
  - `views.step_title(key: str) -> str`: the human label of a step key. Split on `/`, then:
    - drop adjacent duplicate segments;
    - `task-<n>` → `Task <n>`, `r<n>` → `round <n>`, and `resolve-<n>` → `resolver <n>`;
    - when a `Task` segment exists, drop every segment before it;
    - join with ` · `.
  - `views.stage_rail(state: dict, workflow: list[dict], metrics: dict) -> list[dict]`: one `{"phase", "status", "wall_s", "note"}` per entry of `workflow` (the output of `run_workflow`), in order.
    - `wall_s` comes from `metrics["phases"]` by name, or `None`.
    - `note` joins with ` · `: the first non-null `count` among the phase's top-level steps (`"8/24 tasks"`, `"3 rounds"`); `"refine <n>"` when `state["refine"]` is set and `state["phase"]` is this phase; `"waiting on you"` when `state["waiting"]["phase"]` is this phase. It is `None` when there's nothing to say.
  - `views.run_detail(run: Run, cfg: Config, raw: bool = False) -> dict`. The keys are:
    - `run_id`, `label` (`_label`), `lane` (`state["pipeline"]`) and `status` (`display_status`);
    - `facts`: `phase`, `step`, `branch`, `worktree`, `pr_url`, `pr_link`, `spend_usd`, `budget_usd`, `soft_budget_usd`, `refine` and `failure`;
    - `waiting`: `kind`, `key`, `phase`, `summary`, `questions` and `since`, or `None`;
    - `steps`: `{key: {"status", "skip_reason", "result"}}`;
    - `actions` and `stage_rail`;
    - `live_agents`: `[{"label": step_title(step), "key", "model", "effort", "session_id"}]` from `_live_agents`;
    - the unchanged `timeline`, `workflow`, `gates`, `reviews`, `metrics`, `progress`, `handoff`, `inbox`, `transcript`, `transcript_file` and `attach`;
    - `state` only when `raw`.

- [ ] **Step 1: Write the failing tests**

```python
@pytest.mark.parametrize("key, title", [
    ("build/build/task-9/implement/resolve-2", "Task 9 · implement · resolver 2"),
    ("build/build/task-9/implement", "Task 9 · implement"),
    ("plan/plan", "plan"),
    ("define/draft", "define · draft"),
    ("verify/loop/r2/fix", "verify · loop · round 2 · fix"),
])
def test_a_step_key_reads_as_words(key, title):
    assert step_title(key) == title

def test_run_detail_is_slim_unless_raw_is_asked(dash):
    run = awaiting(dash.flow)
    detail = payload(dash.get(f"/api/runs/{run.id}")[2])
    assert "state" not in detail and detail["actions"] == ["approve", "reject", "pause", "cancel"]
    assert detail["label"] == "crm" and detail["waiting"]["kind"] == "phase_approval"
    rail = {p["phase"]: p for p in detail["stage_rail"]}
    assert rail["plan"]["note"] == "waiting on you" and rail["build"]["status"] == "not reached"
    assert "state" in payload(dash.get(f"/api/runs/{run.id}?raw=1")[2])

def test_the_rail_names_a_refine_round(dash):
    run = awaiting(dash.flow)
    hitl.reject(run, "plan", "me", "redo")
    rail = {p["phase"]: p for p in payload(dash.get(f"/api/runs/{run.id}")[2])["stage_rail"]}
    assert "refine 1" in rail["plan"]["note"]
```

Update the existing tests that read `detail["state"]`:
- `test_a_card_shows_the_soft_and_the_hard_budget` reads `detail["facts"]["soft_budget_usd"]`, and its needle `"st.soft_budget_usd"` becomes `"facts.soft_budget_usd"`.
- `test_untrusted_text_is_escaped_in_json` reads `payload(body)["facts"]["refine"]["note"]`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_dashboard.py --import-mode=prepend -q`
Expected: FAIL with `ImportError: cannot import name 'step_title'`.

- [ ] **Step 3: Implement `step_title`, `stage_rail` and the new `run_detail`, and switch the route and the drawer to the new fields**

The drawer keeps its current sections. Only the field names change (`const st = detail.state` → `detail.facts` / `detail.steps` / `detail.waiting`). The route passes `raw=(query.get("raw") or [""])[0] == "1"`.

- [ ] **Step 4: Run the tests to verify they pass**

Same command. Expected: PASS.

- [ ] **Step 5: Add the changelog line and commit**

Under `### Changed`: "`GET /api/runs/<id>` no longer embeds the run's whole `state.json`: it returns the fields the page shows, plus `actions` (what the run's state allows now), `label`, `stage_rail` and `live_agents`. `?raw=1` returns the old payload for debugging."

```bash
git add agentflow/views.py agentflow/dashboard.py agentflow/dashboard_ui agentflow/tests/test_dashboard.py CHANGELOG.md
git commit -m "Slim the run detail to what the page shows and add its actions, labels and stage rail."
```

---

### Task 4: Cache each run's digest and metrics on the Board

**Files:**
- Modify: `agentflow/views.py`: `_DIGEST_CACHE`, `DIGEST_LIVE_TTL_S`, `_board_figures`, and `board()`.
- Modify: `CHANGELOG.md`.
- Test: `agentflow/tests/test_dashboard.py`.

**Interfaces:**
- Produces:
  - `views._board_figures(run: Run, cfg: Config, now: datetime) -> tuple[dict, dict]`, which returns `(digest, metrics)`. `board()` calls it instead of reading the events itself.
  - The cache key is `(str(events path), st_size, st_mtime_ns, now.astimezone().date())`. The date is in the key because `spend_today` depends on it.
  - An entry whose metrics say `live` expires after `DIGEST_LIVE_TTL_S = 30.0` seconds of `now`. An ended run's entry never expires.
  - The cache is capped at `_METRICS_CACHE_MAX` entries, with the oldest evicted first, as `_METRICS_CACHE` does.
  - A failed `stat()` means no caching for that call.

- [ ] **Step 1: Write the failing test**

```python
def test_the_board_reads_an_unchanged_run_once(dash, monkeypatch):
    run = finished_run(dash.flow)
    reads = []
    real = type(run.events).read
    monkeypatch.setattr(type(run.events), "read", lambda self, *a, **k: reads.append(self.path) or real(self, *a, **k))
    board(dash.flow.root, dash.flow.cfg); board(dash.flow.root, dash.flow.cfg)
    assert reads.count(run.events.path) == 1
    run.emit("run_archived", by="me")
    board(dash.flow.root, dash.flow.cfg)
    assert reads.count(run.events.path) == 2   # a changed log is read again

def test_a_live_runs_cached_figures_expire(dash, monkeypatch):
    run = awaiting(dash.flow)            # not ended: its metrics are live
    reads = []
    real = type(run.events).read
    monkeypatch.setattr(type(run.events), "read", lambda self, *a, **k: reads.append(1) or real(self, *a, **k))
    t0 = datetime.now(UTC)
    board(dash.flow.root, dash.flow.cfg, now=t0); board(dash.flow.root, dash.flow.cfg, now=t0 + timedelta(seconds=5))
    assert len(reads) == 1
    board(dash.flow.root, dash.flow.cfg, now=t0 + timedelta(seconds=31))
    assert len(reads) == 2
```

Clear `views._DIGEST_CACHE` at the start of each test. If `awaiting`'s metrics don't come out `live`, give the run a `step_started` so they do.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_dashboard.py -k "reads_an_unchanged or cached_figures_expire" --import-mode=prepend -q`
Expected: FAIL: the first read count is 2, not 1.

- [ ] **Step 3: Implement `_board_figures` and use it in `board()`**

Keep `run_metrics_view`'s own cache as it is. Only `board()` changes.

- [ ] **Step 4: Run the tests to verify they pass**

Run the same command, then the whole of `test_dashboard.py` and `test_metrics.py`. Expected: PASS.

- [ ] **Step 5: Add the changelog line and commit**

Under `### Changed`: "The dashboard's Board reads an unchanged run's event log once, not on every refresh. A live run's figures are recomputed at least every 30 s."

```bash
git add agentflow/views.py agentflow/tests/test_dashboard.py CHANGELOG.md
git commit -m "Cache each run's board figures by its event log's size and mtime."
```

---

### Task 5: A quiet event stream for agent turns

**Files:**
- Modify: `agentflow/dashboard.py`: `QUIET_EVERY_S`, `_events(quiet)`, the `/api/events` branch, and the docstring.
- Modify: `CHANGELOG.md`.
- Test: `agentflow/tests/test_dashboard.py`.

**Interfaces:**
- Produces:
  - `GET /api/events?quiet=1`. Each `agent_turn` becomes the message `{"run_id", "event": {"type": "agent_activity", "session_id", "key", "last_tool", "context_pct", "ts"}}`.
  - Messages are coalesced per `(run_id, session_id)`. The first is sent at once. Later ones within `QUIET_EVERY_S = 2.0` s replace a pending one, which the stream loop sends once its 2 s are up, so the last turn of a burst is never lost.
  - Every other event type is sent unchanged, in order. Without `quiet`, the stream is exactly as today.
  - Task 8's page consumes this.

- [ ] **Step 1: Write the failing test**

```python
def test_a_quiet_stream_coalesces_agent_turns(dash):
    run = awaiting(dash.flow)
    run.emit("agent_started", agent="claude", pid=os.getpid(), model="opus", effort="high", step="plan/plan", role="plan")
    run.emit("agent_session", session_id="s-1", model="claude-opus-5-5", key="plan/plan", pid=os.getpid())
    conn = http.client.HTTPConnection("127.0.0.1", dash.port, timeout=5)
    try:
        conn.request("GET", f"/api/events?quiet=1&token={TOKEN}")
        response = conn.getresponse()
        assert response.fp.readline() == b": connected\n"
        for i in range(10):
            run.emit("agent_turn", session_id="s-1", key="plan/plan", tokens=100 + i, context_pct=10 + i,
                     cost=0.01 * i, last_tool=f"Edit-{i}")
        run.emit("run_paused", by="t")
        seen, deadline = [], time.monotonic() + 3.5
        while time.monotonic() < deadline:
            line = response.fp.readline().decode("utf-8")
            if line.startswith("data: "):
                seen.append(json.loads(line[6:])["event"])
    finally:
        conn.close()
    types = [e["type"] for e in seen]
    assert "agent_turn" not in types and "run_paused" in types
    activity = [e for e in seen if e["type"] == "agent_activity"]
    assert 1 <= len(activity) <= 2 and activity[-1]["last_tool"] == "Edit-9"
```

If the reducer wants other `agent_turn` fields, copy them from `agents.py:321`.

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_dashboard.py -k quiet_stream --import-mode=prepend -q`
Expected: FAIL: `agent_turn` is in `types`.

- [ ] **Step 3: Implement it in `_events`**

`_events` takes `quiet: bool` from `query.get("quiet") == ["1"]`. It keeps `last_sent: dict[tuple, float]` and `pending: dict[tuple, bytes]` by `time.monotonic()`, and flushes any due pending messages on each poll round, before the ping check.

- [ ] **Step 4: Run the tests to verify they pass**

Run `-k "sse or quiet_stream"`. Expected: PASS, with the existing SSE tests unchanged.

- [ ] **Step 5: Add the changelog line and commit**

Under `### Added`: "`/api/events?quiet=1` sends each agent session's turns as one `agent_activity` message (last tool, context %) at most every 2 s, the newest always delivered. The dashboard page uses it."

```bash
git add agentflow/dashboard.py agentflow/tests/test_dashboard.py CHANGELOG.md
git commit -m "Coalesce agent turns into a 2 s activity message on the quiet event stream."
```

---

### Task 6: `dashboard.board_refresh_s`

**Files:**
- Modify: `agentflow/config_edit.py`: a form field in the "Dashboard" section, after `show_done_days`.
- Modify: `agentflow/config.py`: the `_Validator.dashboard` check.
- Modify: `agentflow/dashboard.py`: `DEFAULT_BOARD_REFRESH_S`, and `/api/info` gains `board_refresh_s`.
- Modify: `docs/agentflow-playbook.md` §14, the `dashboard` line (line 520).
- Modify: `CHANGELOG.md`.
- Test: `agentflow/tests/test_config_edit.py`, `agentflow/tests/test_config.py` and `agentflow/tests/test_dashboard.py`.

**Interfaces:**
- Produces:
  - `dashboard.DEFAULT_BOARD_REFRESH_S = 2.0`.
  - `GET /api/info` returns `{"name", "version", "board_refresh_s": float}`, read from `cfg.dashboard.get("board_refresh_s", DEFAULT_BOARD_REFRESH_S)` on each request, so a Settings save applies at once.
  - The key isn't protected and isn't a user-file key.
- Validation: a finite number (not a bool), from `0.5` to `60` inclusive. Anything else is an `error` at `dashboard.board_refresh_s`.
- The form field: `(("dashboard", "board_refresh_s"), "Refresh the Board at most every (s)", {"kind": "number", "min": 0.5, "max": 60})`.

- [ ] **Step 1: Write the failing tests**

```python
# test_config.py
def test_dashboard_board_refresh_s_is_half_a_second_to_a_minute(tmp_cfg):
    for bad in (0, 0.4, 61, True, "2", float("inf")):
        found = issues_for(tmp_cfg, {"dashboard": {"board_refresh_s": bad}})
        assert [i.path for i in found if i.level == "error"] == ["dashboard.board_refresh_s"], bad
    for good in (0.5, 2, 60):
        assert not errors(issues_for(tmp_cfg, {"dashboard": {"board_refresh_s": good}})), good

# test_config_edit.py
def test_the_form_sets_the_board_refresh(shipped):
    proposal = form(shipped, (["dashboard", "board_refresh_s"], 5))
    config_edit.save(shipped, proposal)
    assert load_config(shipped).dashboard["board_refresh_s"] == 5
    for bad in (0.1, 120):
        with pytest.raises(config_edit.EditError):
            form(shipped, (["dashboard", "board_refresh_s"], bad))

# test_dashboard.py
def test_info_carries_the_board_refresh(dash):
    assert payload(dash.get("/api/info")[2])["board_refresh_s"] == 2.0
```

Follow the shape of `test_the_form_sets_the_ci_watch` (`test_config_edit.py:118`) and `test_ci_watch_max_h_must_be_finite_and_zero_or_more` (`test_config.py:1615`) for the helpers' exact names.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_config.py agentflow/tests/test_config_edit.py agentflow/tests/test_dashboard.py -k "board_refresh" --import-mode=prepend -q`
Expected: FAIL (no error for `0`; no form field; no `board_refresh_s` in the info).

- [ ] **Step 3: Add the check, the field, the constant and the info value**

Then add to playbook §14's `dashboard` line: "`dashboard.board_refresh_s` (2 by default, 0.5–60) is the most often the page refetches the Board while events arrive."

- [ ] **Step 4: Run the tests to verify they pass**

Run the same command, then `bin/agentflow validate`. Expected: PASS, and `0 warnings`.

- [ ] **Step 5: Add the changelog line and commit**

Under `### Added`: "`dashboard.board_refresh_s` (2 by default, 0.5–60): the most often the dashboard refetches the Board while events arrive."

```bash
git add agentflow/config.py agentflow/config_edit.py agentflow/dashboard.py docs/agentflow-playbook.md agentflow/tests CHANGELOG.md
git commit -m "Add dashboard.board_refresh_s with its form field, check and playbook line."
```

---

### Task 7: Hash routes, the Run page and the peek

**Files:**
- Create: `agentflow/dashboard_ui/run.js`, added to `UI_SCRIPTS` after `drawer.js`.
- Modify:
  - `agentflow/dashboard.html`: a `<section id="run-view" hidden>` holding `#run-head`, `#run-banner`, `#run-rail`, `#run-actions`, `#run-tabs` and `#run-body`, plus the styles for the rail, banner and tab bar, which use the existing `STATUS_CLASS` colours;
  - `agentflow/dashboard_ui/core.js`: the boot only strips a `#token=` hash, and `el()` accepts `data-*` attributes;
  - `agentflow/dashboard_ui/main.js`: the router;
  - `agentflow/dashboard_ui/board.js`: what a card's click, Enter, Space and badge do, and `needNode`'s Open;
  - `agentflow/dashboard_ui/drawer.js`: the drawer becomes the peek.
- Modify: `docs/agentflow-playbook.md`, lines 327 and 579 ("drawer" → "Run page").
- Modify: `CHANGELOG.md`.
- Test: `agentflow/tests/test_dashboard.py`.

**Interfaces:**
- Consumes: `run_detail`'s `actions`, `label`, `stage_rail`, `live_agents`, `facts`, `steps` and `waiting` (Task 3).
- Produces (in `run.js`, used by Task 8):
  - `parseRoute(hash: string) -> {view: string, run: string|null, tab: string|null, sub: string|null}`.
    - `#/<view>` for a name in `VIEWS`.
    - `#/run/<id>[/<tab>[/<sub>]]`, where `tab` defaults to `"overview"` and anything not in `RUN_TABS` becomes `"overview"`.
    - Anything else gives `{view: "board"}`.
    - Each part goes through `decodeURIComponent`.
  - `routeHash(route) -> string`, the inverse, with each part through `encodeURIComponent`.
  - `RUN_TABS = ["overview", "activity", "gates", "files"]`.
  - `ui.run = {id, tab, sub, detail}` while the Run page is shown, and `null` otherwise.
  - `openRun(id: string, tab: string, sub: string|null): Promise<void>` fetches `/api/runs/<id>` and renders it.
    - A 404 renders "No run <id>" and a "← Board" link, and sets `ui.run.detail = null`, so Task 8 never refetches it.
  - `renderRun(detail)`, `renderRunTab(detail, tab, sub)` and `stageRailNode(rail, runId)`.
  - `ACTION_BUTTONS: {[action]: {label, cls?, run(detail)}}` holds the only Pause, Resume, Retry, Mark done and Cancel buttons on the Run page and the peek. They are rendered from `detail.actions`, in the order the server sends.
    - `cancel` opens `dialog()` with the text "Cancel <label>? This stops N live agents: <labels>" (or "No agent is running.") before calling `act`.
    - `approve`, `reject` and `answer` reuse `needNode`'s existing flows.
  - `openPeek(runId)` / `closePeek()` replace `openDrawer` / `closeDrawer`.
- What the page shows:
  - **Header:** `label`, the lane label, `status`, spend "of" budget (`limits()`), the total wall time, the branch, and the PR link (`pr_link` only).
  - **Banner:** only when `waiting` is set: "Waiting on you <ago(since)> — <summary>", with an **Open decision** button that goes to `#/run/<id>/overview/decision`.
  - **Rail:** one chip per `stage_rail` entry, showing the phase, a status mark, `dur(wall_s)` and `note`. A click goes to `#/run/<id>/overview/<phase>`.
  - **Overview:**
    - the waiting decision (`waiting.summary`, the `inbox` text, and the approve, reject and answer buttons from `actions`), anchored `decision`;
    - one `<details data-key="phase:<name>">` per `workflow` phase, with `runStepNode` rows, opened and scrolled to when `sub` names it;
    - `metricsSection(detail.metrics)`;
    - the newest 10 `timeline` lines via `describe`.
  - **Activity:** the old timeline list.
  - **Gates:** the old gates section.
  - **Files:** `progress.md`, `handoff.md`, the transcript tail and the step records, each one a `<details data-key>`.
  - **Peek:** the facts, the waiting decision with its action buttons, and **Open run**.
- What the controls do:
  - **On a card:** a click or Enter goes to the Run page. Space (with `preventDefault`) or a click on the badge (with `stopPropagation`) opens the peek. Escape closes the peek.
  - **Navigation:** the header tab buttons set `location.hash = "#/" + name`. `hashchange` and the boot both call `route()`. Without a hash, the boot uses the saved `agentflow-view` and goes there by `replaceState`.

- [ ] **Step 1: Write the failing tests**

These are source assertions, as the rest of this file uses.

```python
def test_a_route_hash_survives_the_token_boot():
    source = page_source()
    boot = source[source.index("const TOKEN"):source.index("const ui")]
    assert "if (match)" in boot and "if (location.hash)" not in boot   # only #token= is stripped

def test_runs_have_hash_routes():
    source = page_source()
    for needle in ('"#/run/"', "hashchange", "function parseRoute", "function routeHash",
                   'RUN_TABS = ["overview", "activity", "gates", "files"]'):
        assert needle in source, needle

def test_run_page_and_peek_buttons_come_only_from_the_actions_list():
    run_js = (UI_DIR / "run.js").read_text(encoding="utf-8")
    drawer_js = (UI_DIR / "drawer.js").read_text(encoding="utf-8")
    block = run_js[run_js.index("const ACTION_BUTTONS"):run_js.index("\n};", run_js.index("const ACTION_BUTTONS"))]
    outside = run_js.replace(block, "") + drawer_js
    assert not re.search(r'act\([^)]*"(pause|resume|retry|cancel|done)"', outside)
    assert "detail.actions" in run_js and "detail.actions" in drawer_js

def test_an_unknown_run_says_so_and_links_back():
    assert "No run " in (UI_DIR / "run.js").read_text(encoding="utf-8")

def test_the_drawer_is_a_peek_with_an_open_run_button():
    drawer_js = (UI_DIR / "drawer.js").read_text(encoding="utf-8")
    assert "Open run" in drawer_js and "function openPeek" in drawer_js and "openDrawer" not in page_source()
```

Remove `test_run_drawer_never_hands_null_to_replace_children`'s dependence on `drawer.replaceChildren(\n` by pointing it at the peek's render call. Its rule, no `: null` argument, stays.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_dashboard.py --import-mode=prepend -q`
Expected: FAIL on the five new tests.

- [ ] **Step 3: Implement the router, the Run page and the peek**

The rendering can still use `replaceChildren` in this task. Task 8 makes it patch.

- [ ] **Step 4: Run the tests, then check in a browser**

Run the same command. Expected: PASS.

In a browser on `bin/agentflow dashboard`:
- clicking a card opens `#/run/<id>/overview`, and a reload keeps it;
- Back returns to the Board;
- `#/run/<id>/gates` reloads onto Gates;
- `#/run/19990101-0000-nope` shows "No run";
- a `done` run shows no Pause button;
- Space on a card opens the peek, and Escape closes it.

- [ ] **Step 5: Add the changelog line and commit**

Under `### Added`: "Each run has its own page in the dashboard (`#/run/<id>/<tab>`), with a stage rail, a waiting banner, Overview, Activity, Gates and Files tabs, and only the buttons the run's state allows. A card's click opens it, and Space opens a short peek."

```bash
git add agentflow/dashboard_ui agentflow/dashboard.html agentflow/dashboard.py agentflow/tests/test_dashboard.py docs/agentflow-playbook.md CHANGELOG.md
git commit -m "Give each run a Run page with its own URL and turn the drawer into a peek."
```

---

### Task 8: Live updates that keep the reader's place

**Files:**
- Modify:
  - `agentflow/dashboard_ui/core.js`: `patchChildren` and `throttle`;
  - `agentflow/dashboard_ui/main.js`: the stream, the dirty flags and the scoped refreshes;
  - `agentflow/dashboard_ui/board.js`: patched columns, `data-run` on cards, and `patchActivity`;
  - `agentflow/dashboard_ui/run.js` and `agentflow/dashboard_ui/drawer.js`: patch by key.
- Modify: `CHANGELOG.md`.
- Test: `agentflow/tests/test_dashboard.py`.

**Interfaces:**
- Consumes: `/api/events?quiet=1` (Task 5), `/api/info`'s `board_refresh_s` (Task 6), and `openRun`, `renderRun`, `ui.run` and `openPeek` (Task 7).
- Produces, in `core.js`:
  - `patchChildren(parent: Element, items: object[], keyOf: (item) => string, render: (item) => Element): void`. It works in four steps:
    1. Each rendered node gets `data-key` and `data-sig` (`JSON.stringify(item)`).
    2. A child whose key and sig are unchanged stays the same node.
    3. A changed child is replaced, and every `details[data-key]` that was open in the old node is opened in the new one, matched by `data-key`. So is the node itself, when it is a `details`.
    4. Children whose keys are gone are removed, and the order follows `items`.
  - `throttle(fn: () => Promise<void>, ms: () => number): () => void`. It runs at once when the last run was at least `ms()` ago, else once at that time. Calls in between collapse into that one run.
- Produces, in `main.js`:
  - `onStreamMessage(msg: {run_id, event})`. An `agent_activity` message calls `patchActivity(run_id, event)` and marks `dirty.agents`. Any other message marks `dirty.board` and adds `run_id` to `dirty.runs`; a run id the page hasn't seen and `backlog` are included.
  - `boardRefresh = throttle(…, () => boardRefreshMs)`, where `boardRefreshMs = 1000 * info.board_refresh_s` (2000 until `/api/info` answers). It fetches `/api/board`, and `/api/agents` only when the Agents view is shown or `dirty.agents` is set.
  - `runRefresh`, throttled the same way. When `ui.run.detail` is set and `dirty.runs.has(ui.run.id)`, it refetches only that run and calls `renderRun`, which patches.
  - `stream.onopen` marks everything dirty and calls both refreshes, so events missed while disconnected are fetched once.
  - The 30 s interval calls both refreshes through the same throttles.
- Produces, in `board.js`: `patchActivity(runId, event)` sets the text of the card's `.activity` line (`[data-run="<id>"] .activity`) to `ctx <n>% · <last_tool>`. Nothing else is rebuilt.
- What patches and what doesn't:
  - The columns use `patchChildren` keyed by `run_id`, or the backlog item key, and are still skipped mid-drag.
  - The Run page's header, rail and actions use `replaceChildren`, since they are small and hold no open state.
  - The Run page's tab body uses `patchChildren`. Overview phases are keyed `phase:<name>`, Activity lines by `seq`, Gates by `<key>/<gate>/<i>`, and Files by name.
  - The peek patches the same way.
  - Nothing touches `#dlg`, so an open dialog and its typed text survive every refresh, as today.

- [ ] **Step 1: Write the failing tests**

```python
def test_the_page_listens_quietly_and_refreshes_in_buckets():
    source = page_source()
    assert "/api/events?quiet=1&token=" in source
    on_message = source[source.index("stream.onmessage"):source.index("\n", source.index("stream.onmessage"))]
    assert "onStreamMessage" in on_message and "scheduleRefresh" not in source
    assert "agent_activity" in source and "patchActivity(" in source and "board_refresh_s" in source

def test_live_regions_patch_by_key():
    for name in ("board.js", "run.js", "drawer.js"):
        assert "patchChildren(" in (UI_DIR / name).read_text(encoding="utf-8"), name
    core = (UI_DIR / "core.js").read_text(encoding="utf-8")
    assert "function patchChildren" in core and "function throttle" in core and "data-sig" in core

def test_a_reconnect_marks_everything_dirty():
    main = (UI_DIR / "main.js").read_text(encoding="utf-8")
    onopen = main[main.index("stream.onopen"):main.index("stream.onmessage")]
    assert "dirty.board = true" in onopen and "dirty.agents = true" in onopen and "dirty.runs.add" in onopen
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_dashboard.py -k "quietly or patch_by_key or reconnect_marks" --import-mode=prepend -q`
Expected: FAIL.

- [ ] **Step 3: Implement it**

Remove `scheduleRefresh` and the `openDrawer(ui.drawer)` rebuild from `refresh`. Split `refresh` into `boardRefresh` and `runRefresh`.

- [ ] **Step 4: Run the tests to verify they pass**

Run `test_dashboard.py` and `test_portability.py`. Expected: PASS.

- [ ] **Step 5: Run the spec's U0 checks in a browser**

Use a scratch state dir, so no real run is touched.

1. In one terminal, run `XDG_STATE_HOME="$TMPDIR/af-u0" bin/agentflow dashboard`.
2. In another, with the same `XDG_STATE_HOME`, run a short `uv run --no-project --with pyyaml python` script. It loads the pack config and does `Run.create(cfg, repo, "feature", {"module": "ux-demo"})`, then `phase_started`, `step_started`, `agent_started` (`pid=os.getpid()`) and `agent_session`. After that it emits 10 `agent_turn` events a second for 60 s, each with a changing `last_tool`.
3. Open `#/run/<id>/files` and expand two sections, then scroll halfway.

Expected:
- both sections stay open, and the scroll position doesn't move for the whole 60 s;
- the card's activity line on the Board ticks at most every 2 s;
- the browser's Network panel shows `/api/board` at most once per 2 s, and no `/api/runs/<id>` while only `agent_turn` events arrive;
- `#/run/<id>/gates` survives a reload.

- [ ] **Step 6: Add the changelog line and commit**

Under `### Changed`: "Dashboard live updates no longer rebuild what you are reading. Open sections, the scroll position and dialogs survive them. The Board refetches at most every `dashboard.board_refresh_s`, and an open Run page refetches only its own run."

```bash
git add agentflow/dashboard_ui agentflow/tests/test_dashboard.py CHANGELOG.md
git commit -m "Patch live updates by key so open sections, scroll and dialogs survive them."
```

---

### Task 9: Whole-slice verification

There's no new code in this task. If a check fails, fix it in the task that owns the code.

- [ ] **Step 1:** Run `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests --import-mode=prepend -q`. Expected: all pass. This is the full suite, which takes 25–60 min.
- [ ] **Step 2:** Run `bin/agentflow validate`. Expected: `0 warnings`.
- [ ] **Step 3:** Run `git diff main -- agentflow/tests/fixtures/explain`. Expected: empty.
- [ ] **Step 4:** Run `git diff main -- agentflow/dashboard.py | grep PAGE_CSP`. Expected: no `-`/`+` line changes `PAGE_CSP`'s value.
