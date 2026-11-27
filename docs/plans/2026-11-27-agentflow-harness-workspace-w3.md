# Workspace, slice W3 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The page serves the whole workspace. A repository switcher in the header has a waiting badge on each chip and a greyed group for repositories that are not set up. The Inbox and Agents views list every repository's items, and an action posts to the item's own repository. The page reads one merged event stream, and the tab title and notification titles name the repository.

**Architecture:** On the server, a new pure module `workspace_view.py` merges and tags the per-repository inbox and agents views. When the server holds more than one repository, the legacy `/api/inbox` and `/api/agents` return those merged views, and they and `/api/events?all=1` no longer depend on the default repository. `/api/repos` counts come from a short-lived cache. On the page, a new `repos.js` keeps the selection (`ui.repo`) and draws the switcher. `core.js`'s `api()` prefixes every per-repository path with `/api/repos/<id>`. `run.js`'s routes carry the repository, and a run's identity on the page is `(repo, run_id)` everywhere (keys, drafts, groups, links, actions). With one repository, `ui.repo` stays `null`, so the page sends today's legacy requests. The only exceptions are the stream (`?all=1`) and one `/api/repos` read.

**Tech Stack:** Python 3.12, stdlib + `pyyaml`, pytest (real tmp git repos, `http.client` against a real server on port 0). The page is plain browser JavaScript with no build step, and its pure helpers are tested under `node` through `conftest.py`'s `js` and `js_async`.

**Spec:** `docs/specs/agentflow-workspace-proposal.md` (approved 2026-11-26), slice **W3**: section 4.4, the W3 rows of section 4.3, section 6, the Rollout row, and the "Dashboard, W3" and `test_dashboard_js.py` bullets of the Testing strategy. Read it alongside `docs/plans/2026-11-26-agentflow-harness-workspace-w2.md` (its plan decisions describe the server this slice builds on).

**Order and execution:** branch from an up-to-date `main` (3.15). W3 and W4 are independent, but both touch `dashboard.py`, `dashboard.html` and `settings.js`. Whichever lands second rebases and bumps past the other's version. W3 changes dashboard request handling (the routes that no longer need a default), so the spec's Security section applies: the dashboard tasks are reviewed with the `security-auditor` agent.

### What W3 does not do

- No User section in Settings, no `/api/user-config`, and no onboarding page (W4). With no live repository, the page shows a single line saying so (Task 3).
- No combined Board, no merged Metrics or Lessons (spec Non-goals): those views always show the selected repository.
- No run cap and no merged digest (W5).

### Plan decisions

- **"Several repositories" has one definition, used on both sides: the snapshot holds more than one context, whatever their states.** The server tests `len(snap.repos) > 1` (`several(snap)`), and the page tests `/api/repos`'s `repos.length > 1` (`ui.multi`). A workspace of one repository behaves exactly like a single-repository dashboard: no switcher, no column, and untagged legacy views.
- **Workspace routes need no default.** `/api/events?all=1` always counts as one, and so do the legacy `/api/inbox` and `/api/agents` when `several(snap)`. `_handle` skips the default's `refusal` for these routes and serves them with `self._ctx = None`. They cover every `usable` context, plus the `missing` ones for the stream, as W2 already does. When the default is broken or gone, the Board answers 409/404, but the page still gets its stream, its Inbox and its Agents. With a single repository, `/api/inbox` and `/api/agents` keep the default refusal and stay byte for byte as before.
- **A view served for one repository of several is tagged.** This follows the rule W2 gave events (`tagged = self._scoped or merged`). The scoped `/api/repos/<id>/inbox` and `/agents` add `repo: <id>` to every item, watch and recent row (inbox) and to every live and exited row (agents). The page can then send an action to the item's own repository whichever view it came from. Legacy views on a single-repository server stay untagged.
- **A merged view never fails because one repository fails.** Each repository's read is guarded. A failure adds `{"repo": id, "error": text}` to the view's `errors` list, and the other repositories still answer. A merged inbox sorts `items` oldest first across repositories (by `since`, stable, as `inbox_view` does), keeps `watch` in workspace order, sorts `recent` newest first, and sums `counts`. A merged agents view concatenates `live` and `exited` in workspace order, recounts `counts` from the merged `live`, and takes the largest `show_exited_min` and `idle_warn_min`. `generated_at` is the time of the merge.
- **`/api/repos` counts are cached for `COUNTS_TTL_S = 5.0` seconds per context object.** W2 trade-off (d) requires this before the page polls the route. A rescan that replaces a context invalidates its count, because the cache compares the context object, not just its id. The page refetches `/api/repos` on stream messages through the Board's throttle and every 30 s, so a badge can lag an action by up to 5 s.
- **The selection rides in the route as `#/repo/<id>/…` and in `sessionStorage` (`agentflow-repo`).** The spec writes `#repo=<id>`, but the hash is already the page's router (`#/board?…`, `#/run/<id>/<tab>`), so the repository becomes a prefix of every route: `#/repo/<id>/board?lane=bug`, `#/repo/<id>/run/<run>/plan`. Task 5 amends section 4.4 to match. A hash without the prefix, as on a single-repository page, routes as today. The page only uses an id that `/api/repos` lists as `live`. Anything else in the hash (a stale bookmark, a not-set-up repository, a hand-typed id) falls back to the next candidate: then `sessionStorage`, then `/api/repos`'s `default`, then the first live repository, then none.
- **`/api/info` is unchanged, and the page builds the tab title from `/api/repos`.** The spec has `/api/info`'s `name` name the repository, but `name` is the harness name that the version pill's tooltip shows, and a repository-scoped `/api/info` already exists. The title is `agentflow · <name>` for one repository, `agentflow · <n> repositories` for several, and `agentflow` for none. Task 5 amends section 4.4.
- **Switching repositories only changes what the page shows.** A chip click navigates to the same view under the new prefix (a Run page goes to the Board) and resets the per-repository caches (`ui.board`, `ui.workflow`, `ui.agents`, the open run and the peek, the Settings data). If Settings holds unsaved edits, the switch first asks `confirm("Discard the unsaved Settings changes in <name>?")`. Nothing on the server changes.
- **When the repository set changes while the page is open, the view doesn't jump.** If `ui.multi` turns true (a rescan added a second repository), the page selects `/api/repos`'s `default`, which is the repository the legacy routes were showing. If the selected repository stops being `live`, the selection stays, its chip shows the state, and the summary line says why (the route's own 409 text). The page never moves to another repository on its own, for the same reason W2 made the default sticky.
- **The notification title names the repository whenever the user file names a workspace** (`workspace.roots` or `workspace.repos` non-empty). The spec says "when more than one repository is live", but the runner process that sends the notification doesn't know what is live, and scanning the workspace for every notification costs a `git rev-parse` per child. The default title `agentflow: {run} needs you` becomes `agentflow · <folder>: {run} needs you`, and the digest title `agentflow` becomes `agentflow · <folder>`. `<folder>` is `run.repo.name`. A `title` that the user configured in `hitl.notify` is kept as written. If the user file can't be read, the title is the plain one, so telling a person never raises. Task 5 amends section 6.

## Global Constraints

- Engine dependencies stay stdlib + `pyyaml>=6`. No project strings in the engine (`test_portability.DENYLIST`), and that includes `dashboard_ui/*.js`.
- A single-repository dashboard (`make_server`, `--config`, or a workspace of one) answers every legacy route byte for byte as before. The existing `test_dashboard.py` single-repository tests and every `test_dashboard_js.py` test pass unchanged, apart from the W2 tests Task 1 names, which pinned the W2-only legacy inbox, agents and stream. The golden `explain` files don't change.
- No request value becomes a path. The page sends only repository ids that `/api/repos` listed, always through `encodeURIComponent`. The server looks every id up in its own map, as in W2.
- Every string from a repository or a run (names, ids, error texts, labels) reaches the page through `el()`'s `textContent`, never as markup.
- An action on an item posts to `/api/repos/<item.repo>/runs/<run>/<action>` on a page that shows several repositories, never to the selected or the default repository.
- No new config key. If one turns out to be needed, it ships with the four parts CLAUDE.md requires, and the plan goes back to the user first.
- `uvx ruff@0.17.4 check .` must print `All checks passed!` before every commit. Tests run with `uv run --no-project --with pyyaml --with pytest pytest <file> --import-mode=prepend -q`.
- Every user-visible change adds one line under `## [Unreleased]` in `CHANGELOG.md`. The last task releases 3.16 (or the next version past whatever `main` has by then). Commit messages are one imperative sentence ending with the trailer the executing session is given (`Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>` in the commands below).

## Review Focus

1. **Two repositories that share a run id** (`20261127-1200-same` in A and in B, both waiting): in the All-repositories Inbox they are two cards. Typing in one doesn't fill the other, arming one doesn't arm the other, and Approve on A's card, with B selected, posts to `/api/repos/<A>/runs/20261127-1200-same/approve` and changes only A's run (Tasks 1 and 4).
2. **A stream message from another repository whose run id matches a card on the selected Board:** it refreshes the badges and the merged Inbox and Agents only, never the selected repository's card, open run or peek (Task 3).
3. **A hash or a stored selection that names no live repository** (a bookmark to a removed repository, a not-set-up one, `#/repo/..%2f..%2fetc/board`, an empty `#/repo//board`): the page falls back down the candidate list and never sends that id to the server (Task 3).
4. **The repository set changes under an open page:** a rescan adds the second repository, so the switcher appears with the default selected and the view stays put. The selected repository's pack breaks, so its chip and the summary say so and nothing jumps. The default becomes unusable, and the page's stream, Inbox and Agents keep working (Tasks 1 and 3).
5. **One repository whose inbox or agents read raises** (a corrupt `events.jsonl`): the merged view answers with every other repository's rows plus one `errors` entry naming it, and the page shows that line as text (Tasks 1 and 4).

---

### Task 1: The merged and tagged Inbox and Agents, workspace routes without a default, cached counts

**Files:**
- Create: `agentflow/workspace_view.py`
- Modify: `agentflow/dashboard.py` (`_handle`, `_get`'s `/api/inbox` and `/api/agents` branches, the `/api/repos` branch, `DashboardServer.__init__`, the module docstring's routes)
- Modify: `agentflow/repo_context.py` (`CountCache`, `repos_view` takes a `count` callable)
- Modify: `CHANGELOG.md` (an `Added` line for the merged Inbox and Agents, and a `Changed` line saying that the workspace routes need no default and that scoped Inbox and Agents rows carry `repo`)
- Test: `agentflow/tests/test_workspace_view.py` (new), `agentflow/tests/test_dashboard.py`, `agentflow/tests/test_repo_context.py`

**Interfaces:**
- Consumes: `inbox_view.inbox_view(repo, cfg, now)`, `views.agents_view(repo, cfg, now)`, `repo_context.usable`, `repo_context.counts`, `repo_context.ERROR_MAX`, `events.parse_iso`, `doctor.printable`.
- Produces:
  ```python
  # workspace_view.py: pure apart from the `read` it is given; never raises for one repository
  def tagged_inbox(view: dict, repo: str) -> dict          # items, watch and recent rows each with "repo"
  def tagged_agents(view: dict, repo: str) -> dict         # live and exited rows each with "repo"
  def merged_inbox(contexts: Iterable[RepoContext], now: datetime,
                   read: Callable[[Path, Config, datetime], dict] = inbox_view) -> dict
      # {items, watch, recent, counts: {approval, question, stop, watch}, errors: [{repo, error}]}
  def merged_agents(contexts: Iterable[RepoContext], now: datetime,
                    read: Callable[[Path, Config, datetime], dict] = agents_view) -> dict
      # {live, exited, counts: {by_agent, by_model}, idle_warn_min, show_exited_min, generated_at, errors}
  # dashboard.py
  def several(snap: Snapshot) -> bool                      # len(snap.repos) > 1
  # repo_context.py
  COUNTS_TTL_S = 5.0
  class CountCache:
      def __init__(self, ttl: float = COUNTS_TTL_S, clock: Callable[[], float] = time.monotonic) -> None
      def get(self, ctx: RepoContext) -> dict[str, int]    # counts(ctx), reused while the same object is younger than ttl
  def repos_view(contexts, skipped, default, user_error, count: Callable[[RepoContext], dict] = counts) -> dict
  ```
  `DashboardServer.counts: repo_context.CountCache`, which the `/api/repos` branch passes as `count=self.server.counts.get`.

- [ ] **Step 1: Write the failing unit tests in `test_workspace_view.py`**

Build contexts with `dataclasses.replace(repo_context.single_context(path, cfg), id=..., name=...)` and a stub `read` keyed by `ctx.repo`, so no run is needed:
- `test_merged_items_sort_oldest_first_across_repositories_and_carry_their_repo`: A's item `since` 10:05 and B's 10:01 give `[B, A]`, each with its `repo`. `counts` sum A's and B's.
- `test_a_run_id_in_two_repositories_stays_two_items`: same `run_id` and `key` in A and B, so two items with different `repo`.
- `test_recent_is_newest_first_and_watch_keeps_workspace_order`.
- `test_a_repository_whose_read_raises_is_an_error_line_and_the_others_still_show`: stub raises `ValueError("bad events.jsonl")` for A. `errors == [{"repo": A.id, "error": "bad events.jsonl"}]`, and B's items are present. An error over `ERROR_MAX` characters is cut.
- `test_only_usable_contexts_are_read`: a `not_set_up` and an `error` context are never passed to `read` (record the calls).
- `test_merged_agents_recount_and_take_the_widest_window`: A has live `claude/sonnet` and B has live `codex/gpt`, so `counts.by_agent == {"claude": 1, "codex": 1}`. `show_exited_min` is `max` of the two. Rows are tagged.
- `test_tagging_leaves_the_view_otherwise_unchanged`: `tagged_inbox(v, "a")` minus the `repo` keys equals `v`, and the input isn't mutated.

- [ ] **Step 2: Write the failing server tests in `test_dashboard.py` and `test_repo_context.py`**

Using the `two` fixture (A is waiting on its plan approval, B is the default):
- `test_with_several_repositories_the_legacy_inbox_merges_every_one_tagged(two)`: `/api/inbox` holds A's item with `repo == two.a.id`. Every item, watch and recent row has a `repo` among `{a.id, b.id}`, and `errors == []`.
- `test_with_several_repositories_the_legacy_agents_merge_every_one(two)`: the keys are those of `merged_agents`, and every row has a `repo`.
- `test_scoped_inbox_and_agents_rows_carry_their_repo(two)`: `at(two.a, "inbox")` items all have `repo == two.a.id`.
- `test_the_workspace_routes_need_no_default(two, tmp_path)`: `_b_pack(two, paths={"state_dir": ...})` makes the default B unusable. `/api/board` is 409, while `/api/inbox` is 200 with A's item, `/api/agents` is 200, and `/api/events?all=1` streams an event A emits (`_open`, `stream.wait`).
- `test_the_merged_stream_opens_with_no_repository(empty_dash)`: `/api/events?all=1` answers 200 `text/event-stream`, while `/api/inbox` stays 404 `no repository` (with zero contexts, `several` is false).
- `test_a_one_repository_server_keeps_the_legacy_inbox_and_agents_untagged(dash)`: no `repo` key in any row of `/api/inbox` or `/api/agents`. The same holds for a `make_workspace_server` over one context.
- Change `_get_scoped` (around line 3044): for `inbox` and `agents`, the expected value is no longer the legacy body. It is the merged legacy body's rows whose `repo` is `this.id` (`items` and `watch`; `live` and `exited`), compared with the scoped body's same lists. The other routes are unchanged.
- In `test_repo_context.py`, `test_counts_are_read_once_per_ttl_and_a_new_context_object_reads_again`: monkeypatch `repo_context.counts` to count its calls, use a fake clock, and call `get(ctx)` twice inside the TTL, which is 1 call. Advancing past the TTL makes it 2. `dataclasses.replace(ctx)` (a new object with the same id) makes it 3.

- [ ] **Step 3: Run them to verify they fail**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_workspace_view.py agentflow/tests/test_dashboard.py agentflow/tests/test_repo_context.py --import-mode=prepend -q`
Expected: the new tests FAIL (`No module named 'agentflow.workspace_view'`, untagged rows, 409 on the workspace routes, `CountCache` undefined). Every other test passes.

- [ ] **Step 4: Implement `workspace_view.py`, `CountCache`, and the dashboard routing**

In `_handle`, before the default's `refusal`, decide `workspace = path == "/api/events" and query.get("all") == ["1"] or path in ("/api/inbox", "/api/agents") and several(snap)`. A workspace route sets `self._ctx = None` and skips the refusal. This applies only to unprefixed paths, because a prefixed path is always one repository. `_get`'s `/api/inbox` and `/api/agents` serve `merged_*(self.server.live_contexts(snap), now)` when `self._ctx is None`. They serve `tagged_*(…, self._ctx.id)` when `self._scoped` and `several(snap)` (a scoped route is tagged whenever the server holds several). Otherwise they serve the legacy view unchanged. Check that `_events` with `merged=True` reads nothing from `self._ctx`. `CountCache` holds `{id: (ctx, at, counts)}` under a `threading.Lock`. Add `workspace_view` to the module docstring's routes paragraph.

- [ ] **Step 5: Run the tests to verify they pass**

Run the Step 3 command, then `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_inbox_view.py agentflow/tests/test_portability.py --import-mode=prepend -q`
Expected: PASS.

- [ ] **Step 6: Lint and commit**

```bash
uvx ruff@0.17.4 check .
git add agentflow/workspace_view.py agentflow/dashboard.py agentflow/repo_context.py agentflow/tests/test_workspace_view.py agentflow/tests/test_dashboard.py agentflow/tests/test_repo_context.py CHANGELOG.md
git commit -m "Merge every repository's Inbox and Agents on the legacy routes, tagged, with no default needed.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: The notification title names the repository

**Files:**
- Modify: `agentflow/hitl.py` (`_title_for`, `notify_run`'s digest title, a new `_repo_label`)
- Modify: `CHANGELOG.md` (one `Changed` line)
- Test: `agentflow/tests/test_hitl.py`

**Interfaces:**
- Consumes: `layers.load_user_settings()`, `layers.LayerError`.
- Produces: `hitl._repo_label(repo: Path) -> str | None`, which returns the folder name when the user file names a workspace and `None` otherwise (no workspace, or the file can't be read). It never raises. `DEFAULT_TITLE` and `DIGEST_TITLE` keep their values, and the label is inserted after `agentflow` as ` · <folder>`.

- [ ] **Step 1: Write the failing tests**

Write the user file the way `test_dashboard.write_workspace` does (`layers.user_config_path()`, `{"workspace": {"roots": [str(tmp_path)]}}`), and reuse `macos_calls` and `_digest_cfg`:
- `test_the_default_title_names_the_repository_when_a_workspace_is_set`: `notify_run(cfg, run, "m")` gives `with title "agentflow · <repo.name>: <run.id> needs you"` in `macos_calls[0][2]`.
- `test_the_digest_title_names_the_repository_too`: `'with title "agentflow · <repo.name>"'`.
- `test_a_configured_title_is_kept_as_written_in_a_workspace`: a `hitl.notify` macos entry with `title: "mine {run}"` gives `"mine <run.id>"`.
- `test_an_unreadable_user_file_keeps_the_plain_title`: the user file holds `": : :"`. The title is `agentflow: <run.id> needs you` and nothing raises.
- The existing `test_an_unchanged_digest_is_not_sent_again` (`'with title "agentflow"'`, with no user file) must still pass unchanged.

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_hitl.py --import-mode=prepend -q`
Expected: the three workspace tests FAIL on the plain title, and the unreadable-file test passes (it pins behavior that must be kept).

- [ ] **Step 3: Implement `_repo_label` and use it in both titles**

Catch `LayerError`, `OSError` and `ValueError` around `load_user_settings()`. Pass the folder name through `_one_line`.

- [ ] **Step 4: Run the tests to verify they pass**

Run the Step 2 command. Expected: PASS.

- [ ] **Step 5: Lint and commit**

```bash
uvx ruff@0.17.4 check .
git add agentflow/hitl.py agentflow/tests/test_hitl.py CHANGELOG.md
git commit -m "Name the repository in the default notification and digest titles when a workspace is set.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: The switcher, the path prefix, repository routes and the merged stream

**Files:**
- Create: `agentflow/dashboard_ui/repos.js`
- Modify: `agentflow/dashboard_ui/core.js` (`ui.repo`, `ui.repos`, `ui.multi`, `ui.allRepos`, `repoPath`, `runKey`, `api`, `act`)
- Modify: `agentflow/dashboard_ui/run.js` (`parseRoute`, `routeHash`, `goRun`)
- Modify: `agentflow/dashboard_ui/main.js` (`connect`, `onStreamMessage`, `refreshAfter`, `refreshBoardNow`, `route`, the boot and the 30 s timer)
- Modify: `agentflow/dashboard_ui/artifact.js` (`viewedList`, and the map it reads, keyed by `runKey`)
- Modify: `agentflow/dashboard.html` (a `<nav id="repos" hidden aria-label="Repositories">` after the `h1`, `.repo-chip` styles: selected, disabled and greyed, a badge)
- Modify: `agentflow/dashboard.py` (`UI_SCRIPTS`: `"repos.js"` right after `"core.js"`)
- Modify: `CHANGELOG.md` (an `Added` line for the switcher and the tab title)
- Test: `agentflow/tests/test_dashboard_js.py`, `agentflow/tests/test_dashboard.py`

**Interfaces:**
- Consumes: `/api/repos` (`{repos: [{id, name, state, waiting, active, failed, error}], skipped, default}`), and tagged stream messages `{repo, run_id, event}` (Task 1).
- Produces (later tasks rely on these exact names):
  ```js
  // core.js
  ui.repo      // the selected repository's id on a page of several, else null
  ui.repos     // the last /api/repos body, or null
  ui.multi     // ui.repos.repos.length > 1
  ui.allRepos  // the Inbox and Agents show every repository (true by default)
  function repoPath(path, repo)        // "/api/x" -> "/api/repos/<enc repo>/x" when repo is a string; else path
  const runKey = (repo, runId) => ...  // repo ? repo + "\0" + runId : runId
  async function api(path, body, repo) // repo undefined -> ui.repo; null -> unprefixed (a workspace route)
  async function act(runId, action, body, repo)  // posts through api(..., repo); refreshAfter(runId, repo)
  // run.js
  parseRoute(hash) -> {view, run, tab, sub, query?, repo}   // repo: the decoded `#/repo/<id>/` prefix, or null
  routeHash(route)  // prefixes "#/repo/<enc id>" when route.repo is a string; a route with no `repo` key takes
                    // currentRepo()
  const currentRepo = () => (typeof ui === "object" && ui ? ui.repo : null);  // in run.js: safe in a node test without core.js
  goRun(id, tab, sub, repo)
  // repos.js: pure helpers first, touching no DOM at load
  isMulti(view) -> boolean                           // !!view && view.repos.length > 1: what ui.multi is set from
  selectable(row) -> boolean                         // row.state === "live"
  pickRepo(view, hashRepo, stored) -> string | null  // the first selectable of: hashRepo, stored, view.default, the first live row
  chipModel(view, selected) -> [{id, name, waiting, state, selected, disabled, note}]  // live, error and missing rows in order, then not_set_up
  tabTitle(view) -> string
  waitingTotal(view) -> number
  streamOwn(msg, multi, repo) -> boolean             // !multi || msg.repo === repo
  repoName(id) -> string                             // the name /api/repos gives, else the id
  async function loadRepos()   // fetch /api/repos (repo null), set ui.repos/ui.multi, settle ui.repo, title, chips, badge
  function selectRepo(id)      // a chip click: the confirm on unsaved Settings, sessionStorage, the hash under the new prefix
  function applyRepo(id)       // route() calls it when the hash's repo differs from ui.repo: reset the per-repository caches
  const NO_REPO_TEXT = "No repository to show: add a folder to workspace.roots in your user config, or start agentflow dashboard inside a repository."
  ```

- [ ] **Step 1: Write the failing JS tests in `test_dashboard_js.py`**

```python
REPOS = {"repos": [{"id": "api-1a2b3c4d", "name": "api", "state": "live", "waiting": 2, "active": 1, "failed": 0, "error": None},
                   {"id": "web-5e6f7a8b", "name": "web", "state": "live", "waiting": 0, "active": 0, "failed": 0, "error": None},
                   {"id": "docs-9c0d1e2f", "name": "docs", "state": "not_set_up", "waiting": 0, "active": 0, "failed": 0, "error": None},
                   {"id": "lib-0a1b2c3d", "name": "lib", "state": "error", "waiting": 0, "active": 0, "failed": 0, "error": "bad pack"}],
         "skipped": [], "default": "web-5e6f7a8b"}

def test_repo_path_prefixes_only_with_a_repository():
    assert js(["core.js"], "repoPath('/api/board', 'api-1a2b3c4d')", None) == "/api/repos/api-1a2b3c4d/board"
    assert js(["core.js"], "repoPath('/api/runs/r/approve', 'a/../b')", None) == "/api/repos/a%2F..%2Fb/runs/r/approve"
    assert js(["core.js"], "[repoPath('/api/board', null), repoPath('/api/board', undefined)]", None) == ["/api/board"] * 2
```
`core.js` reads `location` and `sessionStorage` at load. Give the test its own stub file (an absolute path loaded first, as `_js_paths` allows) that defines `location = {hash: ""}`, `history`, `sessionStorage`, `localStorage` and `document`. Then:
- `test_a_one_repository_view_shows_no_switcher` (spec Testing strategy): `isMulti` is false for a one-row view, for `{repos: []}` and for `null`, and true for `REPOS`.
- `test_a_route_carries_its_repository_both_ways` (load `run.js` the way `test_the_board_query_round_trips_through_the_hash` does): `parseRoute("#/repo/api-1a2b3c4d/run/20261127-1200-x/plan")` has `repo == "api-1a2b3c4d"`, `view == "run"` and `tab == "plan"`, and `routeHash` of it gives back the same hash. `#/repo/api-1a2b3c4d/board?lane=bug` keeps its query. `parseRoute("#/board")["repo"] is None`, and `routeHash({view: "inbox", repo: null}) == "#/inbox"`. `#/repo/` and `#/repo//board` give the Board with `repo` null. A malformed `%` gives the Board.
- `test_the_selection_takes_the_hash_then_the_session_then_the_default`: `pickRepo(REPOS, "api-1a2b3c4d", "web-5e6f7a8b") == "api-1a2b3c4d"`. A hash naming `docs-…`, `lib-…` or `nope` falls through to the stored value. A stored `docs-…` falls through to `default`. With no live row, the result is `None`.
- `test_chips_put_not_set_up_last_and_disable_what_is_not_live`: the order is `[api, web, lib, docs]`. `disabled` is true for `lib` and `docs`, `lib`'s `note` is `"bad pack"`, and `api`'s `waiting == 2`.
- `test_the_tab_title_names_the_repository_or_counts_them`: `agentflow · 4 repositories`. A one-row view gives `agentflow · api`. An empty view gives `agentflow`.
- `test_a_message_of_another_repository_is_not_the_pages_own`: `streamOwn({repo: "a"}, true, "b") is False`, `streamOwn({repo: "a"}, true, "a") is True`, and `streamOwn({repo: "a"}, false, null) is True`.
- `test_waiting_total_sums_every_row`: 2.

- [ ] **Step 2: Write the failing page tests in `test_dashboard.py`**

- `test_the_page_loads_repos_js_after_core_js`: `UI_SCRIPTS[1] == "repos.js"` (the existing order assertions at lines 470-487 still hold). `GET /ui/repos.js` is 200, and the shell holds `id="repos"`.
- `test_the_page_streams_every_repository`: `main.js` holds `"/api/events?all=1&quiet=1&token="` and no longer holds `"/api/events?quiet=1&token="`.
- `test_repos_js_draws_repository_text_as_text`: `repos.js` has no `innerHTML`, as the existing script checks do for the other files (extend the loop at line 2309 if it lists names).

- [ ] **Step 3: Run them to verify they fail**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_dashboard_js.py agentflow/tests/test_dashboard.py --import-mode=prepend -q`
Expected: the new tests FAIL (`repoPath is not defined`, `repos.js` not in `UI_SCRIPTS`). Every existing test passes.

- [ ] **Step 4: Implement**

Pure helpers come first in `repos.js`. `main.js` boots `loadRepos()` before the first `boardRefresh()`, and calls it from the 30 s timer and, through `const repoRefresh = throttle(loadRepos, () => boardRefreshMs)`, on every stream message when `ui.multi`. Each part behaves as follows:

- **Stream messages.** `onStreamMessage` sends a message for which `streamOwn` is false only to `repoRefresh()`, plus `dirty.inbox` or `dirty.agents` with `boardRefresh()` when that view is shown with `ui.allRepos`. It never calls `patchActivity` and never touches `dirty.runs`. `refreshAfter(runId, repo)` adds `runId` to `dirty.runs` only when `repo` is the page's own (`streamOwn({repo}, ui.multi, ui.repo)`).
- **No live repository.** When `ui.multi` is true and `ui.repo` is `null`, or `/api/repos` lists no live row, `refreshBoardNow` fetches nothing and `#summary` shows `NO_REPO_TEXT`.
- **Header badge.** In multi mode the header's Inbox badge is `waitingTotal(ui.repos)`, set in `loadRepos`. Board rounds stop setting it from `needs_human`, which they keep doing on a page of one repository.
- **Routes.** `route()` calls `applyRepo` when `where.repo` (or, with none, the stored selection) differs from `ui.repo`. Every `routeHash` caller keeps working through `currentRepo()`. Grep `dashboard_ui/` for `"#/run/"` and `"#/` literals outside `run.js`, and route them through `routeHash`.
- **Artifact keys.** `viewedList(runId, repo)` and its recorder in `artifact.js` key their map by `runKey(repo === undefined ? ui.repo : repo, runId)`.
- **Chips.** They are `<button>`s with `textContent` and `aria-pressed`. Disabled chips get a `title` holding the `note` or `not set up: run agentflow init in it`. The `#repos` nav is `hidden` unless `ui.multi`. `document.title = tabTitle(ui.repos)`.

- [ ] **Step 5: Run the tests to verify they pass**

Run the Step 3 command, then `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_dashboard_markdown.py agentflow/tests/test_portability.py --import-mode=prepend -q`
Expected: PASS.

- [ ] **Step 6: Check it in a browser**

Run the dashboard over a workspace of the two fixture packs, then the one-repository dashboard inside this checkout (`bin/agentflow dashboard --port 0`), and walk through them: switch repositories on the Board and in a run, reload (the selection stays), open a bookmark to a missing repository, and check the tab title in both. On the one-repository page, check that the header looks as it does on `main`.

- [ ] **Step 7: Lint and commit**

```bash
uvx ruff@0.17.4 check .
git add agentflow/dashboard_ui/repos.js agentflow/dashboard_ui/core.js agentflow/dashboard_ui/run.js agentflow/dashboard_ui/main.js agentflow/dashboard_ui/artifact.js agentflow/dashboard.html agentflow/dashboard.py agentflow/tests/test_dashboard_js.py agentflow/tests/test_dashboard.py CHANGELOG.md
git commit -m "Add the repository switcher, prefix the page's requests with the selected repository and stream every repository.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: The All-repositories Inbox and Agents act on each item's own repository

**Files:**
- Modify: `agentflow/dashboard_ui/inbox.js` (`inboxItemKey`, `groupItems`, `goneText`, `inboxSend`, `approveAllItems`, `inboxWatchRow`, `inboxCard`, `inboxGroupHead`, `inboxArtifacts`, `inboxKey`, `approvePayload`'s `viewedList` call, `renderInbox`'s `errors` line)
- Modify: `agentflow/dashboard_ui/agents.js` (`agentsByRun`, `agentTable`, `renderAgents`)
- Modify: `agentflow/dashboard_ui/main.js` (`refreshBoardNow` fetches the merged or the scoped view by `ui.allRepos`)
- Modify: `agentflow/dashboard.html` (`<button id="inbox-all" type="button" aria-pressed="true" hidden>All repositories</button>` in the Inbox toolbar beside the grouping buttons, and `#agents-all` above `#agents-summary`)
- Modify: `CHANGELOG.md` (an `Added` line)
- Test: `agentflow/tests/test_dashboard_js.py`, `agentflow/tests/test_dashboard.py`

**Interfaces:**
- Consumes: `ui.repo`, `ui.multi`, `ui.allRepos`, `runKey`, `api(path, body, repo)`, `act(runId, action, body, repo)`, `routeHash`/`goRun` with `repo`, and `repoName` (Task 3). It also consumes the tagged rows and `errors` (Task 1).
- Produces:
  ```js
  const inboxItemKey = (item) => runKey(item.repo, item.run_id) + "\0" + item.key;
  function inboxRepo(item, multi)  // item.repo when it is a string; null when !multi; false (refuse) when multi and untagged
  groupItems(items, by)            // a run group's key is runKey(item.repo, item.run_id) and the group carries `repo`
  agentsByRun(rows)                // groups by runKey(r.repo, r.run_id); each group carries `repo`
  agentTable(rows, withSubs, withRepo)  // withRepo: a first "Repository" column (repoName(r.repo))
  function setAllRepos(on)         // the two chips share ui.allRepos; marks inbox and agents dirty and refreshes
  ```

- [ ] **Step 1: Write the failing JS tests**

- `test_items_of_two_repositories_with_one_run_id_have_two_keys`: `inboxItemKey` of `{repo: "a", run_id: "r", key: "k"}` differs from the same item with `repo: "b"`. An item with no `repo` keeps today's key `"r\0k"`.
- `test_items_group_by_repository_and_run`: two items with the same `run_id` in `a` and `b` make two run groups with `repo` `a` and `b`. The existing `test_items_group_by_run_or_by_kind` passes unchanged.
- `test_gone_text_reads_only_its_own_repositorys_recent`: a `recent` entry for run `r` in `b` doesn't explain the item of run `r` in `a`, so the result is `INBOX_NONE`.
- `test_an_inbox_action_posts_to_the_items_repository_not_the_selected_one` (spec Testing strategy; Review Focus 1), with `js_async`, stubbing `fetch` to record its URL and answer `{ok: true}`:
  ```js
  ui.repo = "b"; ui.multi = true;
  await inboxSend({repo: "a", run_id: "20261127-1200-same", key: "plan_approved", kind: "phase_approval", phase: "plan"}, "approve", {target: "plan"});
  // recorded == ["/api/repos/a/runs/20261127-1200-same/approve"]
  ```
  On a one-repository page (`ui.multi = false`, no `repo`), the URL is `"/api/runs/20261127-1200-same/approve"`. With `ui.multi = true` and no `repo`, nothing is fetched and the card's draft error reads `"This card names no repository; reload the page."`.
- `test_approve_all_posts_each_approval_to_its_own_repository`: this extends the existing `approve_all` test pattern. `act` is stubbed to record `run + ":" + repo`, and the result is `["r1:a", "r2:b"]`.
- `test_agents_of_two_repositories_with_one_run_id_are_two_groups`: `agentsByRun` gives two groups, and the existing `test_agents_group_by_run_with_idle_rows_first` passes unchanged.

- [ ] **Step 2: Write the failing page test in `test_dashboard.py`**

- `test_the_inbox_and_agents_views_have_an_all_repositories_chip`: the shell holds `id="inbox-all"` and `id="agents-all"`, both `hidden`.

- [ ] **Step 3: Run them to verify they fail**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_dashboard_js.py agentflow/tests/test_dashboard.py --import-mode=prepend -q`
Expected: the new tests FAIL. Every existing test passes.

- [ ] **Step 4: Implement**

- **Fetching.** `refreshBoardNow` fetches `api("/api/inbox", undefined, null)` and `api("/api/agents", undefined, null)` when `ui.multi && ui.allRepos`. Otherwise it fetches the prefixed views, which are tagged on a page of several.
- **Sending.** `inboxSend` resolves `inboxRepo(item, ui.multi)`, refuses `false` through `inboxRefuse`, and passes the repository to `act`. Every `goRun` and link in `inbox.js` and `agents.js` passes the row's `repo`.
- **Repository column.** On a page of several, a card's header line and the run group's head start with `repoName(item.repo)` as text, and so does the Agents table's first column (`withRepo` is `ui.multi && ui.allRepos`).
- **Errors.** `renderInbox` and `renderAgents` draw each `errors` entry as one muted line: `<repo name>: <error>`, as text.
- **The chips.** `#inbox-all` and `#agents-all` are shown only when `ui.multi`.

- [ ] **Step 5: Run the tests to verify they pass**

Run the Step 3 command. Expected: PASS.

- [ ] **Step 6: Check it in a browser**

On the two-repository workspace from Task 3, with one waiting item in each repository, select B and approve A's item from the All-repositories Inbox. A's run moves on and B's doesn't. Turn the chip off, and the Inbox shows only B's items. Open A's run from its card, and the switcher moves to A.

- [ ] **Step 7: Lint and commit**

```bash
uvx ruff@0.17.4 check .
git add agentflow/dashboard_ui/inbox.js agentflow/dashboard_ui/agents.js agentflow/dashboard_ui/main.js agentflow/dashboard.html agentflow/tests/test_dashboard_js.py agentflow/tests/test_dashboard.py CHANGELOG.md
git commit -m "List every repository's Inbox and Agents rows and send each action to its own repository.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Documentation, the spec's amendments and the release

**Files:**
- Modify: `docs/agentflow-playbook.md` (§17, and §10 if a line changes)
- Modify: `docs/specs/agentflow-workspace-proposal.md` (sections 4.3, 4.4, 6 and the Testing strategy)
- Modify: `CLAUDE.md` (one line for `workspace_view.py` under "Safety and the UI")
- Modify: `CHANGELOG.md`, `pyproject.toml`, `agentflow/pack.py`
- Test: `agentflow/tests/test_packaging.py`

- [ ] **Step 1: Update the playbook, the spec and CLAUDE.md**

Playbook §17, "The dashboard": replace "the page still shows the default repository until W3" with how the page works now: the switcher and its badges, the not-set-up group, `#/repo/<id>/…` and the selection that survives a reload, the All-repositories chip on the Inbox and Agents, that an action goes to the item's own repository, the tab title, the notification title rule (named when your user file names a workspace; a configured `title` is kept), and that the badges can lag an action by up to 5 seconds. Remove the known limit "the legacy routes keep the default repository until W3's switcher". Keep "the default never moves on its own", because the legacy Board routes still follow it.

The spec gets four amendments:
- **Section 4.3.** In the inbox/agents row, write that they merge when the server holds more than one repository, that they and `/api/events?all=1` need no default, and that the scoped inbox and agents are tagged.
- **Section 4.4.** Replace `#repo=<id>` with the `#/repo/<id>/…` prefix, and say that the tab title comes from `/api/repos` while `/api/info`'s `name` stays the harness name.
- **Section 6.** Replace "when more than one repository is live" with "when the user file names a workspace", and give the reason.
- **Testing strategy.** Mark the W3 bullets as done.

`CLAUDE.md`: under "Safety and the UI", add one line: `workspace_view.py` merges and tags each repository's inbox and agents views for the workspace routes, and `dashboard_ui/repos.js` holds the page's repository selection.

- [ ] **Step 2: Release**

Rename `## [Unreleased]` to `## [3.16] - <today>` and open a fresh, empty `## [Unreleased]` above it. If `main` already has 3.16, use the next version past it. Set `version = "3.16"` in `pyproject.toml` and `ENGINE_VERSION = "3.16"` in `agentflow/pack.py`.

- [ ] **Step 3: Verify**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_packaging.py agentflow/tests/test_portability.py --import-mode=prepend -q`, then `bin/agentflow validate` (it must print `0 warnings`), then the full suite (`uv run --no-project --with pyyaml --with pytest pytest agentflow/tests --import-mode=prepend -q`).
Expected: every test passes, with `0 warnings`.

- [ ] **Step 4: Lint and commit**

```bash
uvx ruff@0.17.4 check .
git add docs/agentflow-playbook.md docs/specs/agentflow-workspace-proposal.md CLAUDE.md CHANGELOG.md pyproject.toml agentflow/pack.py
git commit -m "Document the multi-repository page and release 3.16.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```
