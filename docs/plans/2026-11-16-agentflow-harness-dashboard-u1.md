# Dashboard slice U1 (Spec and plan) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Read and judge the spec and the plan in the dashboard. A run's Spec and Plan tabs show each revision `define` and `plan` committed, rendered or raw, with a diff against the previous revision and the review findings in the gutter. Approve records what the approver looked at, and Request changes turns line comments into one reject note.

**Architecture:**
- **Server.**
  - A new read-only module, `agentflow/artifacts.py`, lists a run's artifacts from its own events (`phase_started.start_sha`, `step_committed.sha`), with one `git log --no-walk` call. It reads a revision with `git cat-file` and diffs two revisions with `git diff`, always from the main checkout's object store, so a deleted worktree changes nothing. It also gathers the findings from `step_findings` and `review_result`.
  - `dashboard.py` gains four GET routes. A file is read only when its path is in the run's artifact list and its sha is one of that path's revisions (or `head`, the run's branch). Every read is capped by the new key `dashboard.artifact_max_kb`.
  - `POST /api/runs/<id>/approve` accepts an optional `viewed: [{path, sha}]`, and `hitl.approve` records it on `phase_approved`. The CLI never sends it.
  - `run_detail` gains `artifact_tabs`, so the page never names a phase.
- **Page.**
  - `dashboard_ui/markdown.js` is a small safe Markdown parser. It turns text into a plain data tree (pure, so node tests it with no DOM), and a builder turns that tree into DOM nodes with `el()` and `textContent` only.
  - `dashboard_ui/artifact.js` holds the Spec and Plan tabs: the file picker, revision chips, Rendered / Raw / Diff, the findings panel, gutter dots, line comments and the decision bar.

**Tech Stack:** Python 3.12 stdlib, git, pytest, plain browser JavaScript (no framework, no build step), and node (tests only, when on `PATH`).

**Spec:** `docs/specs/agentflow-dashboard-ux-proposal.md` (`Status: approved`, 2026-11-15). Where the spec says `step_finished.sha` and `changed_since`, the events carry the sha as `step_committed.sha`, and the file list comes from `git log --name-only` per commit. This plan covers slice **U1** (§7):
- §4.3, all of it except the Files-tab paragraph, which is U2;
- §4.4, Document mode only (Tasks mode is U2);
- §4.11: the `artifacts`, `artifact`, `artifact/diff` and `findings` routes, and `viewed` on approve;
- §6: only `viewed` on `phase_approved` (`informed_approval` is U4);
- the `dashboard.artifact_max_kb` key from §8;
- §7's U1 test bullets, and Verification "U1".

U0 is shipped: routes, the Run page, `patchChildren` and `throttle` exist (`docs/plans/2026-11-15-agentflow-harness-dashboard-u0.md`).

## Global Constraints

- Engine dependencies stay stdlib + `pyyaml>=6`. No JavaScript framework, bundler or CDN. node is a test-time tool only: the engine never runs it, and a test that needs it is skipped when `shutil.which("node")` is None.
- `PAGE_CSP` stays byte-identical. `test_page_is_served_with_a_nonce_and_no_unsafe_dom_sinks` pins it.
- Every new route is GET only and needs the token, the Host check and the Origin check. Run ids go through `_run()`. No route reads a path the client names unless it is in the run's artifact list. Every file comes from git objects, never the filesystem.
- Every string from a run is set with `textContent`. The page sink scan, which covers every `dashboard_ui/*.js`, refuses these literals: `innerHTML`, `outerHTML`, `insertAdjacentHTML`, `document.write`, `eval(`, `new Function`, `setTimeout("`, ` style="` and `javascript:`. It also refuses any `https?://` match. So `markdown.js` checks a scheme with an allow-list written as `/^https?:\/\//i`, never as a string literal of the URL.
- Every git call uses `guards.HARDENED`, `stdin=DEVNULL` and `gitops.GIT_READ_TIMEOUT_S`, and `git diff` also passes `--no-ext-diff --no-textconv --no-color`. Shas match `^[0-9a-f]{7,64}$` before they reach git. Paths go after `--`.
- No project strings in the engine (`test_portability.py` `DENYLIST`). Don't name the repository the spec measured: call it "the measured repository" in code, tests, docs and commits. Phase names appear in the engine only as `artifacts.PHASE_KINDS = {"define": "spec", "plan": "plan"}`, the engine defaults' own phase names.
- No runner behaviour changes. The one write-side change is the optional, informational `viewed` field on `phase_approved`. No obligation reads it.
- The golden files `agentflow/tests/fixtures/explain/*.txt` stay byte-identical.
- The new config key `dashboard.artifact_max_kb` ships with all four companions: a Settings form field, a `validate` check, a `test_config_edit.py` case and a playbook §14 line.
- `CHANGELOG.md` gets one line under `## [Unreleased]` for each user-visible change, as each task says.
- Never create, edit or delete `uv.lock`. Run everything with `uv run --no-project`.
- Test command: `uv run --no-project --with pyyaml --with pytest pytest <path> --import-mode=prepend -q`.
- A commit message is one imperative sentence and ends with the pack's `project.commit_trailer`.

## Review Focus

1. **A path that isn't a regular file at a revision**: a symlink, a submodule, a deleted file, or binary bytes. The server must answer 404 ("not a regular file at <sha7>", "<path> does not exist at <sha7>"), never follow the link, and decode text with `errors="replace"`. The test goes in Task 2.
2. **A run that hasn't committed anything yet**, for example `define` still drafting. `/artifacts` returns `[]`, and the Spec tab says "No spec yet: this phase has not committed one." It must not show a blank tab or an error toast. The tests go in Task 2 and Task 6.
3. **A revision git no longer has**: the branch was deleted and gc'd, or the main checkout was re-cloned. The server must answer 404 "revision <sha7> is no longer in git", not a 500 with a traceback. The test goes in Task 2.
4. **A finding's `line` in another shape**: `"34"`, `"34-40"`, `"L34"`, `0`, a negative number, past the end of the file, or missing. The server normalises it to a positive int or `null`. A line past the end, or `null`, appears in the panel with no dot. The tests go in Task 3 and Task 5.
5. **An artifact or diff larger than `artifact_max_kb`.** The server cuts it at the last newline before the cap and says `truncated: true`. The page shows "Showing the first N KB" with **Copy path**, and doesn't hang rendering 5 MB. The test goes in Task 2.

---

### Task 1: `dashboard.artifact_max_kb`

**Files:**
- Modify: `agentflow/config_edit.py`: a form field in the "Dashboard" section, after `board_refresh_s` (line 190).
- Modify: `agentflow/config.py`: `_Validator.dashboard`, beside the `board_refresh_s` check (lines 2238-2240).
- Modify: `docs/agentflow-playbook.md` §14, the `dashboard` line (line 528).
- Modify: `CHANGELOG.md`.
- Test: `agentflow/tests/test_config.py`, `agentflow/tests/test_config_edit.py`.

**Interfaces:**
- Produces: the key `dashboard.artifact_max_kb`, an int (not a bool) from `16` to `8192` inclusive, `512` when absent. It isn't protected and isn't a user-file key.
- The form field: `(("dashboard", "artifact_max_kb"), "Largest spec, plan or diff the page loads (KB)", {"kind": "int", "min": 16, "max": 8192})`.

- [ ] **Step 1: Write the failing tests**

```python
# test_config.py, beside test_dashboard_board_refresh_s_is_half_a_second_to_a_minute
def test_dashboard_artifact_max_kb_is_a_whole_number_from_16_to_8192(tmp_cfg):
    for bad in (0, 15, 8193, 512.5, True, "512"):
        found = issues_for(tmp_cfg, {"dashboard": {"artifact_max_kb": bad}})
        assert [i.path for i in found if i.level == "error"] == ["dashboard.artifact_max_kb"], bad
    for good in (16, 512, 8192):
        assert not errors(issues_for(tmp_cfg, {"dashboard": {"artifact_max_kb": good}})), good

# test_config_edit.py, beside test_the_form_sets_the_board_refresh
def test_the_form_sets_the_artifact_cap(shipped):
    config_edit.save(shipped, form(shipped, (["dashboard", "artifact_max_kb"], 1024)))
    assert load_config(shipped).dashboard["artifact_max_kb"] == 1024
    for bad in (8, 10000):
        with pytest.raises(config_edit.EditError):
            form(shipped, (["dashboard", "artifact_max_kb"], bad))
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_config.py agentflow/tests/test_config_edit.py -k "artifact_max_kb or artifact_cap" --import-mode=prepend -q`
Expected: FAIL (no error for `0`; no form field).

- [ ] **Step 3: Add the check and the field**

The error text: "must be a whole number of KB, from 16 to 8192". Add to playbook §14's `dashboard` line: "`dashboard.artifact_max_kb` (512 by default, 16–8192) is the largest spec, plan revision or diff the page loads; above it the page shows the head and says so."

- [ ] **Step 4: Run the tests to verify they pass**

Run the same command, then `bin/agentflow validate`. Expected: PASS, and `0 warnings`.

- [ ] **Step 5: Add the changelog line and commit**

Under `### Added`: "`dashboard.artifact_max_kb` (512 by default, 16–8192): the largest spec, plan revision or diff the dashboard loads."

```bash
git add agentflow/config.py agentflow/config_edit.py docs/agentflow-playbook.md agentflow/tests/test_config.py agentflow/tests/test_config_edit.py CHANGELOG.md
git commit -m "Add dashboard.artifact_max_kb with its form field, check and playbook line."
```

---

### Task 2: List, read and diff a run's artifacts from git

**Files:**
- Create: `agentflow/artifacts.py`.
- Modify: `agentflow/tests/conftest.py`: the `spec_run` fixture below, so Task 3's dashboard tests share it.
- Test: `agentflow/tests/test_artifacts.py`.

**Interfaces:**
- Consumes: `guards.HARDENED`, `gitops.GIT_READ_TIMEOUT_S`, `state.Run`, and `Config.dashboard` (Task 1's key).
- Produces, in `agentflow/artifacts.py`:
  - `PHASE_KINDS: dict[str, str] = {"define": "spec", "plan": "plan"}`. Any other phase's files are kind `"file"`.
  - `DEFAULT_MAX_KB = 512`. `SHA = re.compile(r"^[0-9a-f]{7,64}$")`.
  - `class NotFound(LookupError)`. Its message is what the 404 says.
  - `artifact_max_bytes(cfg: Config) -> int`: `1024 * cfg.dashboard.get("artifact_max_kb", DEFAULT_MAX_KB)`. A value Task 1's check would refuse falls back to the default, so a hand-edited file never breaks the page.
  - `artifact_tabs(phases: list[str]) -> list[str]`: the kinds of `PHASE_KINDS` whose phase is in `phases`, in `PHASE_KINDS` order.
  - `run_artifacts(run: Run, repo: Path) -> list[dict]` returns `[{path, phase, kind, revisions: [{sha, step, label, round, ts}]}]`:
    - **Phases:** the phases in the order of their first `phase_started` event that carries a `start_sha`.
    - **Commits:** each `step_committed` event whose `key` starts with `<phase>/` is one commit of that phase, in `seq` order.
      - `step` is the event's key.
      - `label` is the key's last part, plus `" r<round>"` when `round > 0`, for example `draft` or `spec_reconcile r1`.
      - `round` is the number of `phase_rejected` events of that phase with a lower `seq`.
      - `ts` is the event's `ts`.
    - **Files per commit:** one call, `git log --no-walk=unsorted --format=%x01%H --name-only --no-renames -z <sha>...` in `repo`.
    - **Entries:** one per path, its `phase` and `kind` from the first phase that touched it, and its revisions in commit order across phases. Sorted by phase order, then path.
    - **Cache:** keyed by `(events.jsonl path, size, mtime_ns)`, at most 128 entries, like `views._METRICS_CACHE`.
    - **No commits:** `[]`. A commit git no longer has makes the call fail; in that case, drop the commit and keep the rest.
  - `read_artifact(run: Run, repo: Path, path: str, sha: str, max_bytes: int) -> dict` returns `{path, sha, text, lines, truncated}`.
    - `path` must be an entry of `run_artifacts`.
    - `sha` must be one of that entry's revision shas, or the literal `"head"`, which resolves to `git rev-parse --verify --quiet refs/heads/<state.branch>^{commit}`.
    - Anything else raises `NotFound`. The response's `sha` is the resolved full sha.
    - It reads with `git ls-tree -l -z <sha> -- <path>`:
      - mode `100644`/`100755` and type `blob` only, else `NotFound("<path> is not a regular file at <sha7>")`;
      - an empty entry gives `NotFound("<path> does not exist at <sha7>")`.
    - Then it streams `git cat-file blob <blob>`, reading at most `max_bytes + 1` bytes and killing the process after that. Over the cap, the text is cut after the last `\n` and `truncated` is True.
    - Decoded as UTF-8 with `errors="replace"`. `lines` is the line count of the returned text.
  - `artifact_diff(run: Run, repo: Path, path: str, from_sha: str, to_sha: str, max_bytes: int) -> dict` returns `{path, from, to, diff, truncated}`.
    - Both shas pass the same revision check as `read_artifact`.
    - The command is `git diff --no-ext-diff --no-textconv --no-color --no-renames <from> <to> -- <path>`, capped and cut the same way.
  - Every git failure or timeout becomes `NotFound("revision <sha7> is no longer in git")` for a read or diff. Nothing raises past these functions except `NotFound` and `ValueError`.
- A private helper `_git(repo: Path, *args: str, limit: int | None = None) -> tuple[bytes, bool]` returns `(stdout, cut)`. It uses `subprocess.Popen(["git", *HARDENED, *args], cwd=repo, stdin=DEVNULL, stdout=PIPE, stderr=PIPE)`, and a non-zero exit or a timeout raises `NotFound`.

- [ ] **Step 1: Write the failing tests**

Build the fixture in a real linked worktree, so a test can delete it.

```python
@pytest.fixture
def spec_run(flow_repo, tmp_path):
    """A feature run in a linked worktree on branch flow/feature/crm. define committed docs/specs/x.md three times:
    define/draft ("Scope 3 says one thing", plus notes.md), define/spec_reconcile after a phase_rejected round 1
    ("Scope 3 says another"), and define/spec_reconcile again after round 2 (another line added, the Scope line kept).
    Then phase_approved define, phase_started plan, and plan/plan committed docs/plans/x.md."""
    # git worktree add -b flow/feature/crm <tmp>/wt; run_updated worktree/base_sha/branch;
    # phase_started define start_sha=<head>; then the commits above, each through commit().
    ...

def test_artifacts_list_each_revision_by_phase(spec_run):
    run, repo = spec_run.run, spec_run.repo
    found = {a["path"]: a for a in run_artifacts(run, repo)}
    spec = found["docs/specs/x.md"]
    assert (spec["phase"], spec["kind"]) == ("define", "spec")
    assert [r["label"] for r in spec["revisions"]] == ["draft", "spec_reconcile r1", "spec_reconcile r2"]
    assert [r["round"] for r in spec["revisions"]] == [0, 1, 2]
    assert found["docs/plans/x.md"]["kind"] == "plan" and found["notes.md"]["kind"] == "spec"
    assert list(found) == ["docs/specs/x.md", "notes.md", "docs/plans/x.md"]

def test_a_diff_between_two_revisions_shows_the_change(spec_run):
    r1, r2 = (r["sha"] for r in spec_run.revisions("docs/specs/x.md")[:2])
    diff = artifact_diff(spec_run.run, spec_run.repo, "docs/specs/x.md", r1, r2, 512 * 1024)
    assert "-Scope 3 says one thing" in diff["diff"] and "+Scope 3 says another" in diff["diff"]
    assert diff["truncated"] is False

def test_an_artifact_still_reads_after_its_worktree_is_gone(spec_run):
    run_git(spec_run.repo, "worktree", "remove", "--force", str(spec_run.worktree))
    newest = spec_run.revisions("docs/specs/x.md")[-1]["sha"]
    got = read_artifact(spec_run.run, spec_run.repo, "docs/specs/x.md", newest, 512 * 1024)
    assert got["sha"] == newest and "Scope 3 says another" in got["text"] and got["truncated"] is False

def test_head_reads_the_branch_tip(spec_run):
    got = read_artifact(spec_run.run, spec_run.repo, "docs/plans/x.md", "head", 512 * 1024)
    assert got["sha"] == run_git(spec_run.repo, "rev-parse", "flow/feature/crm").strip()

@pytest.mark.parametrize("path", ["../../state.json", "/etc/passwd", "docs/specs/../plans/x.md", "README.md",
                                  "-p", "docs/specs/x.md\0"])
def test_a_path_outside_the_list_is_not_found(spec_run, path):
    with pytest.raises(NotFound):
        read_artifact(spec_run.run, spec_run.repo, path, "head", 512 * 1024)

@pytest.mark.parametrize("sha", ["0" * 40, "HEAD", "main", "--output=/tmp/x", "abc", "<plan sha>"])
def test_a_sha_that_is_not_a_revision_of_the_path_is_not_found(spec_run, sha):
    sha = spec_run.revisions("docs/plans/x.md")[0]["sha"] if sha == "<plan sha>" else sha
    with pytest.raises(NotFound):
        read_artifact(spec_run.run, spec_run.repo, "docs/specs/x.md", sha, 512 * 1024)

def test_a_big_revision_is_cut_at_a_line(spec_run):            # Review Focus 5
    big = spec_run.commit("define/spec_reconcile", write={"docs/specs/x.md": ("x" * 39 + "\n") * 3000})
    got = read_artifact(spec_run.run, spec_run.repo, "docs/specs/x.md", big, 16 * 1024)
    assert got["truncated"] is True and len(got["text"].encode()) <= 16 * 1024 and got["text"].endswith("\n")

def test_a_symlink_or_a_deleted_path_is_not_read(spec_run):    # Review Focus 1
    linked = spec_run.commit("define/spec_reconcile", link={"notes.md": "/etc/passwd"})
    gone = spec_run.commit("define/spec_reconcile", delete=["notes.md"])
    with pytest.raises(NotFound, match="not a regular file"):
        read_artifact(spec_run.run, spec_run.repo, "notes.md", linked, 512 * 1024)
    with pytest.raises(NotFound, match="does not exist"):
        read_artifact(spec_run.run, spec_run.repo, "notes.md", gone, 512 * 1024)

def test_a_run_with_no_commit_has_no_artifacts(flow_repo):     # Review Focus 2
    run = flow_repo.new_run(); run.emit("phase_started", phase="define", start_sha=flow_repo.head())
    assert run_artifacts(run, flow_repo.root) == []

def test_a_revision_git_lost_is_not_found_not_an_error(spec_run):  # Review Focus 3
    newest = spec_run.revisions("docs/specs/x.md")[-1]["sha"]  # listed (and cached) while git still has it
    run_git(spec_run.repo, "worktree", "remove", "--force", str(spec_run.worktree))
    run_git(spec_run.repo, "branch", "-D", "flow/feature/crm")
    run_git(spec_run.repo, "reflog", "expire", "--expire=now", "--all")
    run_git(spec_run.repo, "gc", "-q", "--prune=now")
    with pytest.raises(NotFound, match="no longer in git"):
        read_artifact(spec_run.run, spec_run.repo, "docs/specs/x.md", newest, 512 * 1024)

def test_a_commit_git_cannot_read_is_dropped_from_the_list(spec_run):
    spec_run.run.emit("step_committed", key="define/spec_reconcile", sha="ab" * 20, summary="lost")
    spec = next(a for a in run_artifacts(spec_run.run, spec_run.repo) if a["path"] == "docs/specs/x.md")
    assert len(spec["revisions"]) == 3 and "ab" * 20 not in {r["sha"] for r in spec["revisions"]}

def test_the_cap_comes_from_the_config_and_survives_a_bad_value(flow_repo):
    for value, expect in ((64, 64 * 1024), (None, 512 * 1024), (0, 512 * 1024), (True, 512 * 1024)):
        flow_repo.cfg.dashboard.pop("artifact_max_kb", None)
        if value is not None: flow_repo.cfg.dashboard["artifact_max_kb"] = value
        assert artifact_max_bytes(flow_repo.cfg) == expect

def test_artifact_tabs_follow_the_lane_phases():
    assert artifact_tabs(["define", "plan", "build"]) == ["spec", "plan"]
    assert artifact_tabs(["define", "build"]) == ["spec"] and artifact_tabs(["build"]) == []
```

`spec_run` lives in `conftest.py` (`flow_repo`, `run_git` and `Run` are already there) and returns a small object with:
- `run`, `repo` (the main checkout) and `worktree`;
- `revisions(path)`, which reads `run_artifacts`;
- `commit(key: str, write: dict[str, str] | None = None, link: dict[str, str] | None = None, delete: list[str] | None = None) -> str`. It changes the worktree, commits, emits `step_committed` with `key` and the new sha, and returns that sha.

Its two `phase_rejected` events use `round=1` and `round=2`. The lost-revision test relies on the cache: the listing it read before `gc` still names the sha, so the read gets past the allow-list and fails in git.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_artifacts.py --import-mode=prepend -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'agentflow.artifacts'`.

- [ ] **Step 3: Implement the module**

For the `--no-walk` parse, split stdout on `\x01`. Each record is a sha, then NUL-separated paths. A `git log` failure (an object is gone) falls back to one `git log` call per sha, dropping the shas that fail.

- [ ] **Step 4: Run the tests to verify they pass**

Run the same command, plus `agentflow/tests/test_portability.py`. Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agentflow/artifacts.py agentflow/tests/test_artifacts.py
git commit -m "List, read and diff a run's artifacts from its own commits in git, capped and allow-listed."
```

---

### Task 3: The artifact and findings routes

**Files:**
- Modify: `agentflow/artifacts.py`: `run_findings`.
- Modify: `agentflow/dashboard.py`: `ARTIFACT_ROUTE`, a `_artifact_get` branch in `_get`, and the docstring's route list.
- Modify: `agentflow/views.py`: `run_detail` gains `artifact_tabs`.
- Test: `agentflow/tests/test_artifacts.py` and `agentflow/tests/test_dashboard.py`.

**Interfaces:**
- Consumes: Task 2's functions and `NotFound`, `views.step_title`, and `autonomy.finding_text` and `as_finding`.
- Produces:
  - `artifacts.run_findings(run: Run) -> list[dict]` returns `[{key, label, round, ts, open, findings: [{severity, file, line, text}]}]`:
    - one row per `step_findings` event (`round` is its 1-based position among that key's `step_findings`), and one per `review_result` event (its own `round`), in `seq` order;
    - `label` is `views.step_title(key)` (import it inside the function: `views` imports `artifacts`);
    - `open` is True only on the newest row of each key;
    - `severity` is lower-cased text or `null`;
    - `text` is `finding_text(f)`.
    - `line`: an int > 0, or the first run of digits in a string (`"34-40"` → 34, `"L34"` → 34). Anything else, `0` or less included, is `null`.
  - `dashboard.ARTIFACT_ROUTE = re.compile(r"^/api/runs/([^/]+)/(artifacts|artifact|artifact/diff|findings)$")`, matched before `RUN_ROUTE`:
    - `GET /api/runs/<id>/artifacts` → `run_artifacts(run, repo)`, a JSON list.
    - `GET /api/runs/<id>/artifact?path=&sha=` → `read_artifact(…, artifact_max_bytes(cfg))`.
    - `GET /api/runs/<id>/artifact/diff?path=&from=&to=` → `artifact_diff(…)`.
    - `GET /api/runs/<id>/findings` → `run_findings(run)`.
    - Errors:
      - a query parameter that is missing, or given twice → 400 "send exactly one <name>";
      - a run that doesn't exist → 404 "no such run";
      - `NotFound` → 404 with its message.
    - `repo` is `self.server.repo`, the main checkout.
  - `run_detail(...)["artifact_tabs"] = artifact_tabs(lane_phases(cfg, state["pipeline"]))`.

- [ ] **Step 1: Write the failing tests**

```python
# test_artifacts.py
def test_findings_come_by_step_and_round_with_the_newest_open(flow_repo):
    run = flow_repo.new_run()
    run.emit("step_findings", key="define/spec_review", tree="", findings=[
        {"severity": "HIGH", "file": "docs/specs/x.md", "line": "34-40", "text": "Scope 3 contradicts the code"}])
    run.emit("step_findings", key="define/spec_review", tree="", findings=[
        {"severity": "medium", "file": "docs/specs/x.md", "line": 65, "title": "Locks", "detail": "after row locks"},
        {"severity": "low", "file": "docs/specs/x.md", "line": "L0"}, {"text": "no file"}])
    rows = run_findings(run)
    assert [(r["round"], r["open"]) for r in rows] == [(1, False), (2, True)]
    assert rows[0]["findings"][0] == {"severity": "high", "file": "docs/specs/x.md", "line": 34,
                                      "text": "Scope 3 contradicts the code"}
    assert [f["line"] for f in rows[1]["findings"]] == [65, None, None]
    assert rows[1]["findings"][0]["text"] == "Locks after row locks"

# test_dashboard.py: uses the `spec_run` fixture from conftest.py (Task 2)
def test_the_spec_routes_read_through_the_artifact_list(dash, spec_run):
    rid = spec_run.run.id
    listed = payload(dash.get(f"/api/runs/{rid}/artifacts")[2])
    spec = next(a for a in listed if a["path"] == "docs/specs/x.md")
    r1, r2 = spec["revisions"][0]["sha"], spec["revisions"][1]["sha"]
    got = payload(dash.get(f"/api/runs/{rid}/artifact?path=docs/specs/x.md&sha={r2}")[2])
    assert got["sha"] == r2 and got["lines"] > 0
    diff = payload(dash.get(f"/api/runs/{rid}/artifact/diff?path=docs/specs/x.md&from={r1}&to={r2}")[2])
    assert "+Scope 3 says another" in diff["diff"]
    assert payload(dash.get(f"/api/runs/{rid}/findings")[2]) == []

@pytest.mark.parametrize("query", ["path=..%2F..%2Fstate.json&sha=head", "path=README.md&sha=head",
                                   "path=docs%2Fspecs%2Fx.md&sha=" + "0" * 40])
def test_the_artifact_route_refuses_what_is_not_listed(dash, spec_run, query):
    assert dash.get(f"/api/runs/{spec_run.run.id}/artifact?{query}")[0] == 404

@pytest.mark.parametrize("route", ["artifacts", "artifact?path=docs%2Fspecs%2Fx.md&sha=head",
                                   "artifact/diff?path=docs%2Fspecs%2Fx.md&from=head&to=head", "findings"])
def test_the_artifact_routes_keep_the_refusal_matrix(dash, spec_run, route):
    rid = spec_run.run.id
    assert dash.get(f"/api/runs/{rid}/{route}", token=None)[0] == 403
    assert dash.get(f"/api/runs/{rid}/{route}", headers={"Host": "evil.example"})[0] == 403
    assert dash.get(f"/api/runs/20261030-0000-feature-nope/{route}")[0] == 404
    assert dash.post(f"/api/runs/{rid}/{route}")[0] == 404
    link = runs_root(dash.flow.root) / "20261030-0000-feature-link"
    link.symlink_to(spec_run.run.dir, target_is_directory=True)
    assert dash.get(f"/api/runs/{link.name}/{route}")[0] == 404

def test_a_missing_or_doubled_parameter_is_a_400(dash, spec_run):
    rid = spec_run.run.id
    assert dash.get(f"/api/runs/{rid}/artifact?path=docs%2Fspecs%2Fx.md")[0] == 400
    assert dash.get(f"/api/runs/{rid}/artifact?path=a&path=b&sha=head")[0] == 400

def test_a_big_artifact_is_cut_to_the_configured_cap(dash, spec_run):
    dash.server.cfg.dashboard["artifact_max_kb"] = 16
    big = spec_run.commit("define/spec_reconcile", write={"docs/specs/x.md": ("x" * 39 + "\n") * 3000})
    got = payload(dash.get(f"/api/runs/{spec_run.run.id}/artifact?path=docs%2Fspecs%2Fx.md&sha={big}")[2])
    assert got["truncated"] is True and len(got["text"].encode()) <= 16 * 1024

def test_run_detail_names_the_artifact_tabs_its_lane_walks(dash):
    run = awaiting(dash.flow)
    assert payload(dash.get(f"/api/runs/{run.id}")[2])["artifact_tabs"] == ["spec", "plan"]
```

`POST` to these routes answers 404, because `_post` matches only `ACTION_ROUTE`'s action names, and `artifacts` isn't one. If it answers anything else, fix the route, not the test.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_artifacts.py agentflow/tests/test_dashboard.py -k "findings or spec_routes or artifact" --import-mode=prepend -q`
Expected: FAIL (`run_findings` is missing; the routes answer 404 `not found`).

- [ ] **Step 3: Implement `run_findings`, the routes and `artifact_tabs`**

Add to the module docstring's route list: "`GET /api/runs/<id>/artifacts`, `/artifact?path=&sha=` (`sha` a revision of `path` or `head`), `/artifact/diff?path=&from=&to=` and `/findings`: the run's spec, plan and other committed files, read from git and capped at `dashboard.artifact_max_kb`; a path outside the run's artifact list is a 404".

- [ ] **Step 4: Run the tests to verify they pass**

Run the same command, then all of `test_dashboard.py`. Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agentflow/artifacts.py agentflow/dashboard.py agentflow/views.py agentflow/tests
git commit -m "Serve a run's artifacts, revisions, diffs and findings to the dashboard, behind the token and the artifact list."
```

---

### Task 4: Approve records what was viewed

**Files:**
- Modify: `agentflow/hitl.py`: `approve` gains `viewed`.
- Modify: `agentflow/dashboard.py`: `_approve` validates and passes `viewed`, plus the docstring.
- Modify: `CHANGELOG.md`.
- Test: `agentflow/tests/test_dashboard.py` and `agentflow/tests/test_hitl.py`.

**Interfaces:**
- Produces:
  - `hitl.approve(run, target, by, budget=None, hold=False, note=None, budget_step=None, viewed: list[dict] | None = None) -> bool`.
    - On a phase approval, a non-empty `viewed` is emitted as `phase_approved.viewed`.
    - On any other approval it is ignored. No field is written when it is `None` or `[]`.
  - The `viewed` body field on `POST /api/runs/<id>/approve` is optional. When given, it must be a list of at most 100 objects, each with exactly two keys: `path` (text of 1–1024 characters) and `sha` (matching `artifacts.SHA`). Otherwise → 400 "viewed must be a list of {path, sha}". The server records it as sent: it is informational, and nothing checks it against the artifacts.

- [ ] **Step 1: Write the failing tests**

```python
# test_dashboard.py
def test_approve_records_what_the_page_showed(dash):
    run = awaiting(dash.flow, phase="define")
    viewed = [{"path": "docs/specs/x.md", "sha": "a" * 40}]
    assert dash.post(f"/api/runs/{run.id}/approve", {"viewed": viewed})[0] == 200
    (event,) = [e for e in run.events.read() if e["type"] == "phase_approved"]
    assert event["viewed"] == viewed and event["by"] == "dashboard"

@pytest.mark.parametrize("viewed", ["x", [{"path": "a"}], [{"path": "a", "sha": "zz"}],
                                    [{"path": "", "sha": "a" * 40}], [{"path": "a", "sha": "a" * 40, "x": 1}],
                                    [{"path": "a", "sha": "a" * 40}] * 101])
def test_a_malformed_viewed_is_a_400_and_approves_nothing(dash, viewed):
    run = awaiting(dash.flow, phase="define")
    assert dash.post(f"/api/runs/{run.id}/approve", {"viewed": viewed})[0] == 400
    assert not [e for e in run.events.read() if e["type"] == "phase_approved"]

# test_hitl.py
def test_the_cli_approval_carries_no_viewed(flow_repo):
    run = flow_repo.new_run(); run.emit("phase_started", phase="define")
    run.emit("phase_approval_raised", phase="define", round=0, summary="s")
    hitl.approve(run, "define", by="me")
    assert "viewed" not in [e for e in run.events.read() if e["type"] == "phase_approved"][-1]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_dashboard.py agentflow/tests/test_hitl.py -k "viewed" --import-mode=prepend -q`
Expected: FAIL (`viewed` isn't recorded; the malformed body answers 200).

- [ ] **Step 3: Implement it**

Validate `viewed` in `_approve` before calling `hitl.approve`, so a 400 changes nothing. Add to the docstring's approve line: "an approve body may carry `viewed: [{path, sha}]`, the revisions the page showed, recorded on `phase_approved`".

- [ ] **Step 4: Run the tests to verify they pass**

Run the same command, then `agentflow/tests/test_conformance.py` and `agentflow/tests/test_metrics.py`, which read `phase_approved`. Expected: PASS.

- [ ] **Step 5: Add the changelog line and commit**

Under `### Added`: "A dashboard approval of a phase records the spec and plan revisions the page showed (`viewed` on `phase_approved`). The CLI's approvals are unchanged."

```bash
git add agentflow/hitl.py agentflow/dashboard.py agentflow/tests CHANGELOG.md
git commit -m "Record on a dashboard phase approval which artifact revisions the page showed."
```

---

### Task 5: A safe Markdown renderer

**Files:**
- Create: `agentflow/dashboard_ui/markdown.js`, added to `UI_SCRIPTS` after `dialogs.js`.
- Modify: `agentflow/dashboard.html`: the styles for `.md` (headings, lists, tables, code, blockquote) and `.gutter`, using the existing colour tokens.
- Test: `agentflow/tests/test_dashboard_markdown.py` (new) and `test_dashboard.py`'s UI list tests.

**Interfaces:**
- Produces, in `markdown.js`. The top level declares only functions and literal constants, so node can load the file with no DOM.
  - `MD_TAGS = ["h1","h2","h3","h4","h5","h6","p","ul","ol","li","table","thead","tbody","tr","th","td","pre","code","blockquote","hr","strong","em","a","ref","text"]`.
  - `parseMarkdown(text: string) -> Block[]`. A block is `{t, line, end, c?: Node[], v?: string, align?: string[]}`. `line` and `end` are 1-based source lines.
    - **Blocks:** ATX headings, paragraphs, `-`/`*`/`1.` lists (nested by indent), GFM tables, fenced code (```` ``` ```` and `~~~`, `v` = the raw text), blockquotes and `---` rules.
    - **Inline nodes:** `{t: "text", v}`, `{t: "code", v}`, `{t: "strong", c}`, `{t: "em", c}`, `{t: "a", href, c}` and `{t: "ref", path, c}`.
    - Anything else, raw HTML included, is text. Entities aren't decoded: `&lt;` stays the four characters.
  - `safeTarget(target: string) -> {t: "a", href} | {t: "ref", path} | null`:
    - an `http:`/`https:` URL (`/^https?:\/\//i`) → `a`;
    - a target with no scheme (`/^[A-Za-z][A-Za-z0-9+.-]*:/` doesn't match) that doesn't start with `/`, `\` or `//` → `ref`;
    - everything else → `null`, and the link's text renders as text.
  - `markdownNodes(blocks: Block[]) -> Element[]` builds the DOM with `el()`. Each block element carries `data-line` and `data-end`.
    - `a` gets `target: "_blank"` and `rel: "noopener noreferrer"`.
    - `ref` renders as its text followed by the path in a `<code>` (no navigation in U1).
    - Every string goes through `textContent`.

- [ ] **Step 1: Write the failing tests**

```python
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not on PATH")
LOADER = ("const fs=require('fs'),vm=require('vm');const ctx=vm.createContext({});"
          "for (const f of JSON.parse(process.argv[1])) vm.runInContext(fs.readFileSync(f,'utf8'),ctx);"
          "ctx.args=JSON.parse(fs.readFileSync(0,'utf8'));"
          "process.stdout.write(JSON.stringify(vm.runInContext(process.argv[2],ctx)));")

def js(files: list[str], expr: str, args) -> object:
    out = subprocess.run([NODE, "-e", LOADER, json.dumps([str(UI_DIR / f) for f in files]), expr],
                         input=json.dumps(args), capture_output=True, text=True, check=True, timeout=30)
    return json.loads(out.stdout)

def walk(nodes):
    for node in nodes:
        yield node
        yield from walk(node.get("c") or [])

HOSTILE = "\n".join([
    "<script>alert(1)</script>", "", "[a](javascript:alert(1)) [b](JAVASCRIPT:x) [c]( javascript:x)",
    "[d](//evil.example/x) [e](data:text/html,x) [f](vbscript:x) [g](/etc/passwd) [h](\\\\host\\share)",
    "<img src=x onerror=alert(1)> &lt;b&gt; &#60;", "", "| <b>x</b> | y |", "|---|---|", "| 1 | 2 |",
])

def test_hostile_markdown_comes_out_as_text():
    blocks = js(["markdown.js"], "parseMarkdown(args)", HOSTILE)
    nodes = list(walk(blocks))
    assert {n["t"] for n in nodes} <= set(js(["markdown.js"], "MD_TAGS", None))
    assert not [n for n in nodes if n["t"] in ("a", "ref")]
    texts = "".join(n.get("v", "") for n in nodes)
    assert "<script>alert(1)</script>" in texts and "&lt;b&gt; &#60;" in texts and "<b>x</b>" in texts

def test_only_web_and_repo_links_survive():
    nodes = list(walk(js(["markdown.js"], "parseMarkdown(args)", "[w](https://example.com/a) [r](docs/x.md#s)")))
    assert [(n["t"], n.get("href") or n.get("path")) for n in nodes if n["t"] in ("a", "ref")] == [
        ("a", "https://example.com/a"), ("ref", "docs/x.md#s")]

def test_blocks_carry_their_source_lines():
    text = "# T\n\npara one\npara two\n\n- a\n- b\n\n```py\nx = 1\n```\n"
    blocks = js(["markdown.js"], "parseMarkdown(args)", text)
    assert [(b["t"], b["line"], b["end"]) for b in blocks] == [("h1", 1, 1), ("p", 3, 4), ("ul", 6, 7), ("pre", 9, 11)]
    assert blocks[3]["v"] == "x = 1"

# test_dashboard.py: extend test_every_ui_file_is_listed / the load-order test
def test_markdown_loads_before_the_run_page():
    assert UI_SCRIPTS.index("markdown.js") < UI_SCRIPTS.index("run.js")
    assert "innerHTML" not in (UI_DIR / "markdown.js").read_text(encoding="utf-8")
```

The `https://example.com` literal lives only in the test file. `agentflow/tests/` is exempt from the page scan.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_dashboard_markdown.py agentflow/tests/test_dashboard.py -k "markdown or hostile or links or source_lines or every_ui_file" --import-mode=prepend -q`
Expected: FAIL (`markdown.js` doesn't exist). If node is missing, the node tests show as skipped; install node and run again before you go on.

- [ ] **Step 3: Implement `markdown.js` and its styles**

Parse line by line into blocks, then parse the inline nodes of each block's text with one left-to-right scanner: code spans first, then links, then `**`, then `*`/`_`. Keep it to about 200 lines.

- [ ] **Step 4: Run the tests to verify they pass**

Run the same command, plus `test_page_is_served_with_a_nonce_and_no_unsafe_dom_sinks` and `test_portability.py`. Expected: PASS, with no test skipped.

- [ ] **Step 5: Commit**

```bash
git add agentflow/dashboard_ui/markdown.js agentflow/dashboard.html agentflow/dashboard.py agentflow/tests
git commit -m "Add a Markdown renderer that builds a data tree and DOM nodes, never HTML, with a link allow-list."
```

---

### Task 6: The Spec and Plan tabs

**Files:**
- Create: `agentflow/dashboard_ui/artifact.js`, added to `UI_SCRIPTS` after `run.js`.
- Modify: `agentflow/dashboard_ui/run.js`:
  - `RUN_TABS` becomes `["overview", "spec", "plan", "activity", "gates", "files"]`, and `RUN_TAB_LABEL` gains Spec and Plan;
  - `renderRun` draws only the tabs in `visibleTabs(detail)`;
  - `runTabEntries` hands `spec` and `plan` to `artifactEntries`.
- Modify: `agentflow/dashboard_ui/core.js` and `agentflow/dashboard_ui/settings.js`: `diffNode` moves from the second to the first.
- Modify: `agentflow/dashboard_ui/main.js`: `refreshRunsNow` (line 33) also refetches `/artifacts` and `/findings` when the open tab is `spec` or `plan`.
- Modify: `agentflow/dashboard.html`: the styles for the artifact header, chips, the two-column body, the findings panel and severity dots (reusing `--bad`, `--warn`, `--accent`, `--muted`).
- Modify: `docs/agentflow-playbook.md`, line 329, which gains one sentence on the Spec and Plan tabs.
- Modify: `CHANGELOG.md`.
- Test: `agentflow/tests/test_dashboard.py` and `agentflow/tests/test_dashboard_markdown.py`.

**Interfaces:**
- Consumes:
  - from Task 3: the routes and `detail.artifact_tabs`;
  - from Task 5: `parseMarkdown` and `markdownNodes`;
  - from U0: `patchChildren`, `entry`, `ui.run`, `routeHash` and `diffNode`. `diffNode(text)` already exists in `settings.js:168` (it renders `.d-add`/`.d-del`/`.d-hunk` lines). Move it to `core.js` unchanged, so `artifact.js` and `settings.js` share it; `.diff` styles are in `dashboard.html:146`.
- Produces, in `artifact.js`:
  - **Pure helpers**, at the top, with no DOM at load time:
    - `SEVERITY_RANK = ["critical", "high", "medium", "low"]`.
    - `pickArtifact(list: object[], kind: string, path: string|null) -> object|null`. It returns the entry named by `path` when it is of that kind. Otherwise, among that kind: `.md` paths first, then the most revisions, then by path.
    - `rawMarks(rows: object[], path: string) -> {[line: number]: string}`: the worst severity of the open rows' findings on `path`, by line. A `null` line is left out.
    - `gutterMarks(blocks: Block[], rows: object[], path: string) -> {[line: number]: string}`: the same, keyed by the start `line` of the block whose `line..end` holds the finding's line. A line past the last block is left out.
  - **State:** `ui.artifact = {runId, kind, path, sha, mode: "rendered"|"raw"|"diff", texts: Map, diffs: Map}`, kept per run. A `(path, sha)` text is fetched once, because a revision never changes.
  - `visibleTabs(detail) -> string[]`: `RUN_TABS` minus `spec`/`plan` when they aren't in `detail.artifact_tabs`.
  - `artifactEntries(detail, kind, sub) -> entry[]`, where `sub` is the URL-decoded path from `#/run/<id>/spec/<path>`. It draws these parts, each keyed:
    - `art:head`: the path picker (when there are 2 or more files), one chip per revision label, and an **as merged** chip (sha `head`) when `detail.status === "done"`.
      - **Diff with previous**, disabled on the first revision.
      - **Raw** / **Rendered**.
      - **Copy path**.
      - "`<lines>` lines".
      - When `truncated`: "Showing the first `<n>` KB of `<path>`."
    - `art:doc:<path>@<sha>:<mode>`, built by one of three modes:
      - Rendered: `markdownNodes`, each block in a row with a gutter cell holding its start line and a dot from `gutterMarks`;
      - Raw: one row per line, with a number and a dot from `rawMarks`;
      - Diff: `diffNode`.
    - `art:findings`: the rows, open rows first, each finding as a severity dot, `L<line>` and its text.
      - Clicking a finding with a line scrolls the document to it.
      - A finding on another file is listed under "Other files" with its path.
    - `art:empty`, when no file of this kind exists: "No `<kind>` yet: this phase has not committed one."
  - **Selected revision:** the newest one, unless the reader picked another. A refresh that adds a revision keeps the reader's pick and adds the chip.

- [ ] **Step 1: Write the failing tests**

```python
# test_dashboard_markdown.py
def test_a_finding_on_line_34_marks_line_34_raw_and_its_block_rendered():
    text = "\n".join(["# T", ""] + ["" if n in (32, 36) else f"line {n}" for n in range(3, 41)])  # 33–35 is one paragraph
    rows = [{"open": True, "findings": [{"severity": "high", "file": "s.md", "line": 34, "text": "x"},
                                        {"severity": "low", "file": "s.md", "line": 34, "text": "y"},
                                        {"severity": "high", "file": "s.md", "line": 999, "text": "past the end"},
                                        {"severity": "high", "file": "other.md", "line": 3, "text": "z"}]},
            {"open": False, "findings": [{"severity": "critical", "file": "s.md", "line": 3, "text": "old"}]}]
    assert js(["artifact.js"], "rawMarks(args.rows, 's.md')", {"rows": rows}) == {"34": "high", "999": "high"}
    marks = js(["markdown.js", "artifact.js"], "gutterMarks(parseMarkdown(args.text), args.rows, 's.md')",
               {"text": text, "rows": rows})
    block = next(b for b in js(["markdown.js"], "parseMarkdown(args)", text) if b["line"] <= 34 <= b["end"])
    assert marks == {str(block["line"]): "high"}

def test_the_default_artifact_is_the_markdown_file_with_most_revisions():
    files = [{"path": "a.txt", "kind": "spec", "revisions": [1, 2, 3]}, {"path": "b.md", "kind": "spec", "revisions": [1]},
             {"path": "c.md", "kind": "spec", "revisions": [1, 2]}, {"path": "p.md", "kind": "plan", "revisions": [1]}]
    assert js(["artifact.js"], "pickArtifact(args, 'spec', null).path", files) == "c.md"
    assert js(["artifact.js"], "pickArtifact(args, 'spec', 'b.md').path", files) == "b.md"
    assert js(["artifact.js"], "pickArtifact(args, 'spec', 'p.md').path", files) == "c.md"
    assert js(["artifact.js"], "pickArtifact(args, 'plan', null) && 1", []) is None

# test_dashboard.py
def test_the_run_page_has_spec_and_plan_tabs_only_where_the_lane_walks_them():
    run_js = (UI_DIR / "run.js").read_text(encoding="utf-8")
    assert 'RUN_TABS = ["overview", "spec", "plan", "activity", "gates", "files"]' in run_js
    assert "visibleTabs(detail)" in run_js and "artifact_tabs" in (UI_DIR / "artifact.js").read_text(encoding="utf-8")

def test_the_spec_tab_reads_the_artifact_routes_and_says_when_there_is_none():
    art = (UI_DIR / "artifact.js").read_text(encoding="utf-8")
    for needle in ("/artifacts", "/artifact?path=", "/artifact/diff?path=", "/findings", "has not committed one",
                   "Showing the first", "Copy path", "as merged", "Diff with previous"):
        assert needle in art, needle
    assert UI_SCRIPTS.index("run.js") < UI_SCRIPTS.index("artifact.js") < UI_SCRIPTS.index("main.js")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_dashboard_markdown.py agentflow/tests/test_dashboard.py -k "line_34 or default_artifact or spec_and_plan_tabs or spec_tab_reads" --import-mode=prepend -q`
Expected: FAIL.

- [ ] **Step 3: Implement the tabs**

Fetch the artifact list and the findings once per visit and again on `refreshRunsNow` for that run. A failed fetch of the list says why in the tab, as `loadRun` does. A failed revision read keeps the last text and toasts.

- [ ] **Step 4: Run the tests, then check in a browser**

Run all of `test_dashboard.py` and `test_dashboard_markdown.py`. Expected: PASS.

In a browser on `bin/agentflow dashboard`, with a run whose `define` committed a spec:
- `#/run/<id>/spec` shows the newest revision rendered, and a reload keeps the tab;
- each chip switches the revision, **Diff with previous** shows the change, and **Raw** shows numbered lines;
- a finding dot sits on its line in Raw, and a click on the finding scrolls there;
- a bug-lane run with no plan phase has no Plan tab;
- live events don't reset the picked revision or the scroll.

- [ ] **Step 5: Add the changelog line and commit**

Under `### Added`: "A run's page in the dashboard has Spec and Plan tabs: each revision `define` and `plan` committed, rendered or raw, a diff with the previous one, and the review findings by round with a dot on their line."

```bash
git add agentflow/dashboard_ui agentflow/dashboard.html agentflow/dashboard.py agentflow/tests docs/agentflow-playbook.md CHANGELOG.md
git commit -m "Show a run's spec and plan revisions in the dashboard, with a diff and the findings on their lines."
```

---

### Task 7: Approve and Request changes from the document

**Files:**
- Modify:
  - `agentflow/dashboard_ui/artifact.js`: line comments, the decision bar, and the record of what was viewed;
  - `agentflow/dashboard_ui/dialogs.js`: `rejectDialog` takes initial text;
  - `agentflow/dashboard_ui/board.js`: `approveFlow` sends `viewed` on a phase approval.
- Modify: `CHANGELOG.md`.
- Test: `agentflow/tests/test_dashboard_markdown.py` and `agentflow/tests/test_dashboard.py`.

**Interfaces:**
- Consumes: Task 4's `viewed` body field, and Task 6's `ui.artifact` and document rows.
- Produces:
  - `NOTE_MAX = 16384` in `artifact.js`, equal to `dashboard.TEXT_MAX`.
  - `composeNote(path: string, sha: string, comments: [number, string][]) -> string`. It is pure and is the only place the note's format lives. Comments come out sorted by line, blank ones dropped, and each comment's whitespace collapsed to single spaces:
    ```
    Comments on <path> at <sha first 7>:
    L34: <text>
    L65: <text>
    ```
  - `ui.comments`: a `Map` of `"<runId>\0<path>\0<sha>"` to a `Map` of line to text. It lives outside every rendered node, so a re-render rebuilds each open comment box with its text. A click on a gutter line number opens the box for that line (the block's start line when rendered).
  - `ui.viewed`: a `Map` of run id to a `Map` of `"<path>\0<sha>"` to `{path, sha}`. A revision is added when its Rendered or Raw text is drawn, with the resolved sha for `head`. A diff isn't a view.
  - `viewedList(runId: string) -> {path, sha}[]`.
  - `rejectDialog(runId, target, initial = "")` pre-fills the note.
  - In `approveFlow`, a `phase_approval` item sends `{target, viewed: viewedList(item.run_id)}`, leaving `viewed` out when the list is empty.
  - **The decision bar**, `art:decision`, shows when `detail.waiting.kind === "phase_approval"` and `detail.waiting.phase` is the artifact's `phase`:
    - "Waiting on you: approve `<phase>`";
    - **Approve `<kind>`**, which runs `approveFlow(decisionItem(detail, "approve"))`;
    - **Request changes (N comments)**, shown only when `detail.actions` has `reject`. It opens `rejectDialog(runId, phase, composeNote(...))`, and refuses with a toast when the note is longer than `NOTE_MAX`.
  - **When the wait is gone** (approved elsewhere) while comments exist, the bar reads "This phase is no longer waiting on you", and the note stays visible as text the reader can select.

- [ ] **Step 1: Write the failing tests**

```python
# test_dashboard_markdown.py
def test_comments_compose_into_one_line_anchored_note():
    note = js(["artifact.js"], "composeNote('docs/specs/x.md', 'abcdef0123', args)",
              [[65, "repeat loop\n takes  locks"], [34, "Scope 3 contradicts the code"], [50, "  "]])
    assert note == ("Comments on docs/specs/x.md at abcdef0:\n"
                    "L34: Scope 3 contradicts the code\nL65: repeat loop takes locks")

# test_dashboard.py
def test_the_note_cap_matches_the_server():
    from agentflow.dashboard import TEXT_MAX
    assert f"NOTE_MAX = {TEXT_MAX}" in (UI_DIR / "artifact.js").read_text(encoding="utf-8")

def test_a_phase_approval_from_the_page_sends_what_was_viewed():
    board_js = (UI_DIR / "board.js").read_text(encoding="utf-8")
    flow = board_js[board_js.index("function approveFlow"):board_js.index("function needNode")]
    assert "viewedList(item.run_id)" in flow and "viewed" in flow
    art = (UI_DIR / "artifact.js").read_text(encoding="utf-8")
    assert "ui.viewed" in art and "ui.comments" in art and "no longer waiting on you" in art
    assert "function rejectDialog(runId, target, initial" in (UI_DIR / "dialogs.js").read_text(encoding="utf-8")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_dashboard_markdown.py agentflow/tests/test_dashboard.py -k "compose or note_cap or sends_what_was_viewed" --import-mode=prepend -q`
Expected: FAIL.

- [ ] **Step 3: Implement the comments, the bar and `viewed`**

`viewedList` must exist before `approveFlow` runs. `board.js` loads first, but `approveFlow` only calls it when a button is clicked, so a top-level `function viewedList` in `artifact.js` is in scope by then.

- [ ] **Step 4: Run the tests, then check in a browser**

Run all of `test_dashboard.py` and `test_dashboard_markdown.py`. Expected: PASS.

In a browser, with a run awaiting `define` approval:
- open the Spec tab, then comment on two lines;
- **Request changes (2 comments)** opens the reject dialog with the composed note. Edit it, then send it. The run's `phase_rejected.note` is that text;
- on another awaiting run, open the spec and **Approve spec**. The `phase_approved` event in `events.jsonl` carries `viewed` with that path and sha;
- approve a third run from the CLI while its Spec tab is open, with a comment typed. The bar says it is no longer waiting, and the comment text is still there.

- [ ] **Step 5: Add the changelog line and commit**

Under `### Added`: "On the Spec and Plan tabs you can comment on lines. **Request changes** turns the comments into one reject note (`L34: …`) that you edit before it is sent, and **Approve** sits under the document it approves."

```bash
git add agentflow/dashboard_ui agentflow/tests CHANGELOG.md
git commit -m "Approve a spec or plan under the document and turn line comments into one reject note."
```

---

### Task 8: Whole-slice verification

There's no new code in this task. If a check fails, fix it in the task that owns the code.

- [ ] **Step 1:** Run `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests --import-mode=prepend -q`. Expected: all pass, with no node test skipped on a machine that has node. This is the full suite, which takes 25–60 min.
- [ ] **Step 2:** Run `bin/agentflow validate`. Expected: `0 warnings`.
- [ ] **Step 3:** Run `git diff main -- agentflow/tests/fixtures/explain`. Expected: empty.
- [ ] **Step 4:** Run `git diff main -- agentflow/dashboard.py | grep -n PAGE_CSP`. Expected: no changed line.
- [ ] **Step 5:** Walk Verification "U1" from the spec in a browser, with a scratch `XDG_STATE_HOME`:
  - a 3-revision spec lists 3 chips, and the diff r1 → r2 shows the change;
  - a `step_findings` finding on line 34 shows a dot on line 34 in Raw;
  - `?path=../../state.json` answers 404;
  - after `git worktree remove`, the spec still reads;
  - Approve records `viewed`;
  - the hostile fixture, pasted into a spec revision, renders as text.
