# Dashboard slice U2 (Process) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Follow the build task by task and see what a stop holds up. The Plan tab gets a Tasks mode, with each plan task's status, attempts, review, cost and blast radius, grouped by status or by dependency wave. The Files tab shows the run's own documents in full (`issue.md`, each phase's handoff, refine notes, `pr.md`) and the branch's changes with a diff per file.

**Architecture:**
- **Server.**
  - A new read-only module, `agentflow/plan_view.py`, finds the run's plan in git and parses it with `sources.parse_plan_text`, the parser the runner itself uses. It joins each task with the run's state:
    - the step rows under `<prefix>/task-<n>/`;
    - `task_committed`, `state.reviews`, `state.gates`;
    - the agent rows for cost;
    - the waiting and parked stops.
  - From there it computes each task's status, its blast radius (the `Depends on:` closure) and its dependency wave.
  - A new module, `agentflow/run_files.py`, reads the run directory's documents by a fixed allow-list of names, under the same no-symlink rules as `views._tail`, but from the head of the file and capped at `dashboard.artifact_max_kb`.
  - `artifacts.py` gains the branch's changes since the point where the run's branch leaves `origin/<fork_from>`, with a diff per file.
  - Three routes serve them: `plan`, `files` and `changes`.
- **Page.**
  - The Plan tab has two modes: **Tasks**, the default once build has started, and **Document**, which is U1's viewer.
  - The Files tab is rebuilt as a file list with U1's Markdown viewer, plus a Changes section.

**Tech Stack:** Python 3.12 stdlib, git, pytest, plain browser JavaScript (no framework, no build step), and node (tests only, when on `PATH`).

**Spec:** `docs/specs/agentflow-dashboard-ux-proposal.md` (`Status: approved`, 2026-11-15). This plan covers slice **U2** (§7):
- §4.3's last paragraph (the Files tab);
- §4.4, Tasks mode;
- §4.11's `plan` and `files` routes;
- Verification "U2".

It also adds one route the spec describes in §4.3 but leaves out of §4.11's table, `GET /api/runs/<id>/changes`. Task 3 amends the table first.

**Builds on U1** (`docs/plans/2026-11-16-agentflow-harness-dashboard-u1.md`), which must be merged first:
- `artifacts.py`: `run_artifacts`, `read_artifact`, `NotFound`, `SHA`, `artifact_max_bytes`, `run_findings`, `PHASE_KINDS`;
- `ARTIFACT_ROUTE`;
- `markdown.js`: `parseMarkdown`, `markdownNodes`;
- `artifact.js`: `pickArtifact` and the Document viewer;
- `diffNode` in `core.js`;
- the `spec_run` fixture and the node `js` helper.

## Global Constraints

- Engine dependencies stay stdlib + `pyyaml>=6`. No JavaScript framework, bundler or CDN. node is a test-time tool only, and a test that needs it is skipped when `shutil.which("node")` is None.
- `PAGE_CSP` stays byte-identical.
- Every new route is GET only and needs the token, the Host check and the Origin check. Run ids go through `_run()`. A file name or path the client sends is only ever looked up in a list the server built: the run-file allow-list, or the run's changed files. It never becomes a filesystem path or a git argument by itself.
- Run files are read under `views._tail`'s rules: no symlink (on the file or on its folder), resolved inside its root, opened `O_NOFOLLOW`, regular files only.
- Every git call uses `guards.HARDENED`, `stdin=DEVNULL` and `gitops.GIT_READ_TIMEOUT_S`. `git diff` also passes `--no-ext-diff --no-textconv --no-color --no-renames`. Paths go after `--`.
- Every string from a run is set with `textContent`. The page sink scan, which covers every `dashboard_ui/*.js`, refuses these literals: `innerHTML`, `outerHTML`, `insertAdjacentHTML`, `document.write`, `eval(`, `new Function`, `setTimeout("`, ` style="` and `javascript:`. It also refuses any `https?://` match.
- No project strings in the engine (`test_portability.py` `DENYLIST`). Don't name the repository the spec measured: call it "the measured repository" in code, tests, docs and commits.
- No runner behaviour changes and no new config key. U2 adds no write path.
- The golden files `agentflow/tests/fixtures/explain/*.txt` stay byte-identical.
- `CHANGELOG.md` gets one line under `## [Unreleased]` for each user-visible change, as each task says.
- Never create, edit or delete `uv.lock`. Run everything with `uv run --no-project`.
- Test command: `uv run --no-project --with pyyaml --with pytest pytest <path> --import-mode=prepend -q`.
- A commit message is one imperative sentence and ends with the pack's `project.commit_trailer`.

## Review Focus

1. **Task 1 against Task 10.** A stop on `build/build/task-10/implement/resolve-2` must mark Task 10 blocked, never Task 1. Every task match uses the key `<prefix>/task-<n>` followed by `/` or the end of the key, never a bare `startswith`. The test goes in Task 1.
2. **A plan with no `**Depends on:**` lines.** `sources._depends_on` makes each such task depend on the one before it, which is how the runner walks it. So a blocked Task 3 of 6 blocks 4, 5 and 6, and no task is "independent". The page must show that, not invent parallelism. The test goes in Task 1.
3. **A dependency on a task the plan doesn't have, or a cycle** (`2 → 3 → 2`). Waves must terminate. An unknown number stays in `depends_on` but is ignored for waves and blast radius. Cycle members get `wave: null` and group under "Cycle". The test goes in Task 1.
4. **A run-file name that tries to leave the allow-list**: `handoffs/../state.json`, `refine/x-1.md/..`, `HANDOFF.md`, a `handoffs` folder that is a symlink, or `pr.md` as a symlink to `/etc/passwd`. Each answers 404. The test goes in Task 2.
5. **A branch that merged `origin/<fork_from>` in** (`finish` does this through `sync_lane`). The Changes list must show only the run's own changes, which is the PR's diff, not the files the merge brought in. The test goes in Task 3.

---

### Task 1: The plan's tasks joined with the run

**Files:**
- Create: `agentflow/plan_view.py`.
- Modify: `agentflow/artifacts.py`, which exposes `read_blob` and `normal_finding` (both pulled out of U1's `read_artifact` and `run_findings`, with no behaviour change).
- Test: `agentflow/tests/test_plan_view.py`.

**Interfaces:**
- Consumes:
  - from U1: `artifacts.run_artifacts`, `NotFound`;
  - from the engine: `sources.parse_plan_text`, `sources.task_files`, `sources.is_plan_name`, `views.step_title`, `views.TASK_KEY`, `cfg.project.docs["plans"]`.
- Produces:
  - `artifacts.read_blob(repo: Path, sha: str, path: str, max_bytes: int) -> tuple[str, bool]`: `(text, truncated)`, with U1's rules (regular files only, cut after the last newline, `NotFound` on any git failure).
  - `artifacts.normal_finding(raw: object) -> dict`, which returns `{severity, file, line, text}`, normalised exactly as U1's `run_findings` does.
  - `plan_view.PLAN_MAX_BYTES = 4 * 1024 * 1024`: the plan is parsed server-side, so it isn't held to the page cap.
  - `plan_view.STATUSES = ("blocked", "parked", "running", "failed", "committed", "deferred", "done_before", "waiting", "independent", "pending")`, in precedence order.
  - `plan_view.waves(tasks: list[PlanTask]) -> dict[int, int | None]`:
    - `0` for a task with no known dependency;
    - otherwise `1 + max(wave of each dependency)`;
    - a dependency number not in the plan is ignored;
    - a task on a cycle, or one that depends on a cycle, gets `None`.
  - `plan_view.dependents(tasks: list[PlanTask], n: int) -> list[int]`: every task whose transitive `depends_on` reaches `n`, sorted. An unknown number is ignored, and a cycle terminates.
  - `plan_view.run_plan(run: Run, repo: Path, cfg: Config) -> dict` returns:
    ```
    {path, sha, truncated, started, prefix,
     summary: {total, committed, blocked, parked, running, failed, waiting, independent, deferred},
     tasks: [{n, title, text, depends_on, files, wave, status, stop, attempts, rollovers, resolvers,
              review, commit, cost, sessions, gates, blocks, blocked_by}]}
    ```
    - **`path`** is found in this order:
      1. The run's U1 artifacts of kind `plan`, using the entry whose basename passes `is_plan_name(name, params.module)`, or else U1's `pickArtifact` rule (`.md` first, then most revisions, then path).
      2. The newest `is_plan_name` file directly in `docs.plans` at the run's branch head (`git ls-tree --name-only -z <head> -- <plans>/`), for a run that started at `build`.
      3. Otherwise `None`, which returns `{path: None, sha: None, tasks: [], started: <as below>, …}`.
    - **`sha`** is the branch head (`refs/heads/<state.branch>`) when it exists, so the ticked boxes show; otherwise the plan's newest revision.
    - **`prefix`** is the most common `<phase>/<step>` of the step keys matching `^(.+)/task-\d+(?:/|$)`, or `None` before build. `started` is `prefix is not None`.
    - **`status`** is the first of these that holds:
      - `blocked`: `state.waiting.key` is `tkey` or under it;
      - `parked`: a key in `state.parked` is `tkey` or under it;
      - `running`: a live agent's `step`, or a step row with status `running`, is under `tkey`;
      - `failed`: `<tkey>/implement` is `failed`;
      - `committed`: a `task_committed` event has `n == task.n` and key `tkey`;
      - `deferred`: `task.deferred`;
      - `done_before`: `task.checked`, and no step row is under `tkey`;
      - `waiting`: `blocked_by` isn't empty;
      - `independent`: some task is `blocked` or `parked`, and this one isn't waiting on any of them;
      - `pending`: none of the above.
    - **`stop`** is `{kind, key, summary}` from the wait or the parked entry, or `None`.
    - **`attempts`** and **`rollovers`** come from the `<tkey>/implement` row. **`resolvers`** counts the `resolve_started` events under `tkey`.
    - **`review`**, from `state.reviews["<tkey>/review"]`: `{round, blocking, minor, skipped, findings}`, where `minor = len(findings) - blocking` and each finding goes through `normal_finding`. `None` when there is no review.
    - **`commit`** is the newest `task_committed.sha`.
    - **`cost`** is the summed `spend` of the rows in `state.exited` and `state.agents` whose `step` is under `tkey`, rounded to 2 places.
    - **`sessions`**: `[{key, label, model, effort, exit_code, spend, live}]` from those rows, newest first.
    - **`gates`**: `[{key, gate, passed, skipped}]` from `state.gates` keys under `tkey`, the last 10 per key.
    - **`blocks`**: `dependents(n)` minus the committed, `done_before` and deferred tasks.
    - **`blocked_by`**: the `blocked` and `parked` tasks in this task's transitive `depends_on`.
- "Under `tkey`" means `key == tkey or key.startswith(tkey + "/")`. That one helper, `_under(key: str, tkey: str) -> bool`, is the only task match in the module.

- [ ] **Step 1: Write the failing tests**

```python
PLAN5 = """# P
### Task 1: One
- [x] done
### Task 2: Two
**Depends on:** Task 1
### Task 3: Three
**Depends on:** Task 2
### Task 4: Four
**Depends on:** Task 2
### Task 5: Five
**Depends on:** Task 1
"""

@pytest.fixture
def building(spec_run):
    """spec_run (U1), plus the plan docs/plans/2026-11-16-crm.md, task 1 committed and task 2 blocked."""
    spec_run.commit("plan/plan", write={"docs/plans/2026-11-16-crm.md": PLAN5})
    run, p = spec_run.run, "build/build"
    run.emit("phase_started", phase="build", start_sha=spec_run.head())
    run.emit("step_started", key=f"{p}/task-1/implement")
    run.emit("agent_started", key=f"{p}/task-1/implement", session_id="s1", pid=1, model="sonnet", effort="high")
    run.emit("agent_exited", session_id="s1", exit_code=0, cost=1.25)
    sha = spec_run.commit(f"{p}/task-1", write={"one.py": "x = 1\n"})
    run.emit("task_committed", key=f"{p}/task-1", n=1, title="One", sha=sha, round=0)
    run.emit("step_started", key=f"{p}/task-2/implement")
    run.emit("hitl_raised", kind="blocked", key=f"{p}/task-2/implement/resolve-2", summary="floor_guard fails")
    return spec_run

def test_a_blocked_task_names_what_it_holds_up(building, flow_repo):  # spec Verification U2
    plan = run_plan(building.run, building.repo, flow_repo.cfg)
    assert plan["path"] == "docs/plans/2026-11-16-crm.md" and plan["prefix"] == "build/build" and plan["started"]
    by = {t["n"]: t for t in plan["tasks"]}
    assert [by[n]["status"] for n in range(1, 6)] == ["committed", "blocked", "waiting", "waiting", "independent"]
    assert by[2]["blocks"] == [3, 4] and by[2]["stop"]["summary"] == "floor_guard fails"
    assert by[3]["blocked_by"] == [2] and by[5]["blocked_by"] == []
    assert by[1]["commit"] and by[1]["cost"] == 1.25 and by[1]["attempts"] == 1
    assert {n: t["wave"] for n, t in by.items()} == {1: 0, 2: 1, 3: 2, 4: 2, 5: 1}
    assert plan["summary"] == {"total": 5, "committed": 1, "blocked": 1, "parked": 0, "running": 0, "failed": 0,
                               "waiting": 2, "independent": 1, "deferred": 0}

def test_a_stop_on_task_10_never_marks_task_1():                   # Review Focus 1
    assert _under("build/build/task-1", "build/build/task-1")
    assert _under("build/build/task-1/implement/resolve-2", "build/build/task-1")
    assert not _under("build/build/task-10/implement", "build/build/task-1")
    assert not _under("build/build/task-1x", "build/build/task-1")

def test_a_plan_without_depends_lines_is_a_chain():                # Review Focus 2
    tasks = parse_plan_text("".join(f"### Task {n}: T{n}\n" for n in range(1, 7)))
    assert dependents(tasks, 3) == [4, 5, 6] and waves(tasks) == {n: n - 1 for n in range(1, 7)}

def test_unknown_dependencies_and_cycles_terminate():              # Review Focus 3
    tasks = parse_plan_text("### Task 1: A\n**Depends on:** none\n### Task 2: B\n**Depends on:** Task 3\n"
                            "### Task 3: C\n**Depends on:** Task 2\n### Task 4: D\n**Depends on:** Task 9, Task 1\n"
                            "### Task 5: E\n**Depends on:** Task 3\n")
    assert waves(tasks) == {1: 0, 2: None, 3: None, 4: 1, 5: None}
    assert dependents(tasks, 2) == [3, 5] and dependents(tasks, 9) == [4]

def test_a_run_that_started_at_build_finds_its_plan_at_the_branch_head(flow_repo, tmp_path):
    # commit docs/plans/2026-11-16-crm.md (PLAN5) on main, then `git worktree add -b flow/feature/crm <tmp>/wt`
    run = flow_repo.new_run()
    run.emit("run_updated", worktree=str(tmp_path / "wt"), base_sha=flow_repo.head(), branch="flow/feature/crm")
    run.emit("phase_started", phase="build", start_sha=flow_repo.head())
    assert run_plan(run, flow_repo.root, flow_repo.cfg)["path"] == "docs/plans/2026-11-16-crm.md"

def test_no_plan_is_an_empty_list_not_an_error(flow_repo):
    run = flow_repo.new_run()
    assert run_plan(run, flow_repo.root, flow_repo.cfg)["tasks"] == []
```

`spec_run.head()` returns the worktree's `HEAD`. Add it to the U1 fixture if it isn't there.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_plan_view.py --import-mode=prepend -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'agentflow.plan_view'`.

- [ ] **Step 3: Implement `plan_view.py` and the two `artifacts` helpers**

Compute waves with an iterative longest-path walk that marks nodes "in progress" to detect cycles. Don't recurse: a 200-task plan mustn't hit the recursion limit. Run U1's `test_artifacts.py` after the extraction, which must stay green unchanged.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_plan_view.py agentflow/tests/test_artifacts.py agentflow/tests/test_portability.py --import-mode=prepend -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agentflow/plan_view.py agentflow/artifacts.py agentflow/tests/test_plan_view.py agentflow/tests/conftest.py
git commit -m "Join each plan task with the run's steps, stops, reviews and cost, with its blast radius and wave."
```

---

### Task 2: The run's own files, by an allow-list

**Files:**
- Create: `agentflow/run_files.py`.
- Test: `agentflow/tests/test_run_files.py`.

**Interfaces:**
- Consumes: `Run.dir`, `Run.agent_dir`, `artifacts.NotFound`, `intake.ISSUE_FILE`, `LOG_FILE` and `TASK_FILE`.
- Produces:
  - `RUN_FILE = re.compile(r"^(?:issue\.md|task\.md|bug\.log|questions\.md|pr\.md|handoff\.md|progress\.md|handoffs/[a-z0-9_-]{1,64}\.md|refine/[a-z0-9_-]{1,64}-\d{1,4}\.md)$")`. A name must `fullmatch` it.
  - `GROUPS = ("request", "handoffs", "refine", "pull request", "notes")`:
    - `request`: `issue.md`, `task.md` and `bug.log`;
    - `handoffs`: `handoffs/*.md`, in `phase_approved` order;
    - `refine`: `refine/*.md`, by phase order and then round;
    - `pull request`: `pr.md`;
    - `notes`: `progress.md`, `questions.md` and `handoff.md`.
  - `list_files(run: Run) -> list[dict]` returns `[{name, group, size}]` for each allowed file that exists and passes the read rules, in `GROUPS` order. It reads the folders only, never a file's bytes.
  - `read_file(run: Run, name: str, max_bytes: int) -> dict` returns `{name, text, size, truncated}`.
    - `progress.md` is read from `run.agent_dir`, and every other name from `run.dir`.
    - A name that doesn't match, is missing, or breaks a read rule raises `NotFound("no file <name> in this run")`.
    - The file is read from its head, at most `max_bytes`, cut after the last newline when over, and decoded with `errors="replace"`.
- `_head(root: Path, path: Path, limit: int) -> tuple[bytes, int] | None` returns `(bytes, size)`. It keeps `views._tail`'s rules and adds that `path.parent` must not be a symlink.

- [ ] **Step 1: Write the failing tests**

```python
def test_every_document_of_the_run_is_listed_by_group(flow_repo):
    run = flow_repo.new_run()
    for name, text in {"issue.md": "# Ask\n", "pr.md": "body\n", "handoff.md": "all\n",
                       "handoffs/define.md": "d\n", "handoffs/build.md": "b\n", "refine/plan-1.md": "r\n"}.items():
        (run.dir / name).parent.mkdir(parents=True, exist_ok=True)
        (run.dir / name).write_text(text, encoding="utf-8")
    run.emit("phase_approved", phase="define"); run.emit("phase_approved", phase="build")
    assert [(f["group"], f["name"]) for f in list_files(run)] == [
        ("request", "issue.md"), ("handoffs", "handoffs/define.md"), ("handoffs", "handoffs/build.md"),
        ("refine", "refine/plan-1.md"), ("pull request", "pr.md"), ("notes", "handoff.md")]

def test_a_phase_handoff_reads_in_full_past_the_old_16_kb_tail(flow_repo):   # spec Verification U2
    run = flow_repo.new_run()
    (run.dir / "handoffs").mkdir()
    text = "## build\n" + "line of the build handoff\n" * 1000          # about 26 KB
    (run.dir / "handoffs" / "build.md").write_text(text, encoding="utf-8")
    got = read_file(run, "handoffs/build.md", 512 * 1024)
    assert got["text"] == text and got["truncated"] is False

@pytest.mark.parametrize("name", ["handoffs/../state.json", "refine/x-1.md/..", "HANDOFF.md", "events.jsonl",
                                  "../events.jsonl", "handoffs/a/b.md", "refine/plan.md", "/etc/passwd", ""])
def test_names_outside_the_allow_list_are_not_found(flow_repo, name):           # Review Focus 4
    with pytest.raises(NotFound):
        read_file(flow_repo.new_run(), name, 1024)

def test_symlinks_are_never_followed(flow_repo, tmp_path):                      # Review Focus 4
    run = flow_repo.new_run()
    (tmp_path / "secret.md").write_text("secret\n", encoding="utf-8")
    (run.dir / "pr.md").symlink_to(tmp_path / "secret.md")
    (tmp_path / "hand").mkdir(); (tmp_path / "hand" / "build.md").write_text("x\n", encoding="utf-8")
    (run.dir / "handoffs").symlink_to(tmp_path / "hand", target_is_directory=True)
    for name in ("pr.md", "handoffs/build.md"):
        with pytest.raises(NotFound):
            read_file(run, name, 1024)
    assert not [f for f in list_files(run) if f["name"] in ("pr.md", "handoffs/build.md")]

def test_progress_comes_from_the_agent_workspace_and_a_big_file_is_cut(flow_repo):
    run = flow_repo.new_run()
    run.agent_dir.mkdir(parents=True, exist_ok=True)
    (run.agent_dir / "progress.md").write_text("x" * 39 + "\n" * 1 + ("y" * 39 + "\n") * 2000, encoding="utf-8")
    got = read_file(run, "progress.md", 16 * 1024)
    assert got["truncated"] is True and got["text"].endswith("\n") and len(got["text"].encode()) <= 16 * 1024
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_run_files.py --import-mode=prepend -q`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement `run_files.py`**

- [ ] **Step 4: Run the tests to verify they pass**

Same command, plus `test_portability.py`. Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agentflow/run_files.py agentflow/tests/test_run_files.py
git commit -m "Read a run's own documents by a fixed allow-list of names, from the head, without following symlinks."
```

---

### Task 3: The branch's changes since it left the base

**Files:**
- Modify: `docs/specs/agentflow-dashboard-ux-proposal.md` §4.11. Add the row ``| `GET /api/runs/<id>/changes` (added with U2) | `{base, head, files: [{path, status, added, deleted}]}`; with `?path=`, that file's diff, capped | `git diff <merge-base>...<branch>` |`` and, under Verification "U2", "after a merge of `origin/<fork_from>` into the branch, `/changes` lists only the run's own files".
- Modify: `agentflow/artifacts.py`: `run_changes` and `change_diff`.
- Test: `agentflow/tests/test_artifacts.py`.

**Interfaces:**
- Consumes: `cfg.project.fork_from`, `state.base_sha` and `state.branch`, and U1's `_git` helper and `NotFound`.
- Produces:
  - `artifacts.CHANGES_MAX = 2000`.
  - `artifacts.run_changes(run: Run, repo: Path, cfg: Config) -> dict` returns `{base, head, complete, files: [{path, status, added, deleted}]}`.
    - `head` is `refs/heads/<branch>`. With no branch, it raises `NotFound("this run has no branch yet")`.
    - `base` is `git merge-base <head> refs/remotes/origin/<fork_from>` when that ref exists and a merge-base is found; otherwise `state.base_sha`.
    - `status` is one of `A`, `M`, `D` and `T`, from `git diff --name-status -z --no-renames <base> <head>`.
    - `added` and `deleted` come from `--numstat -z`. Both are `None` for a binary file.
    - Sorted by path, at most `CHANGES_MAX`, with `complete` False when cut.
  - `artifacts.change_diff(run: Run, repo: Path, cfg: Config, path: str, max_bytes: int) -> dict` returns `{path, base, head, diff, truncated}`. `path` must be in `run_changes(...)["files"]`, else `NotFound`. The diff command and the cap are as in U1's `artifact_diff`.

- [ ] **Step 1: Write the failing tests**

```python
def test_changes_are_the_runs_own_files_since_its_base(spec_run, flow_repo):
    got = run_changes(spec_run.run, spec_run.repo, flow_repo.cfg)
    assert {f["path"] for f in got["files"]} == {"docs/specs/x.md", "notes.md", "docs/plans/x.md"}
    assert all(f["status"] == "A" and f["added"] > 0 for f in got["files"]) and got["complete"] is True
    diff = change_diff(spec_run.run, spec_run.repo, flow_repo.cfg, "docs/specs/x.md", 512 * 1024)
    assert "+Scope 3 says another" in diff["diff"]

def test_a_merge_of_the_base_branch_brings_no_foreign_files_into_changes(spec_run, flow_repo, tmp_path):  # Review Focus 5
    land_on_origin(flow_repo, tmp_path, {"upstream.txt": "theirs\n"})
    run_git(spec_run.worktree, "merge", "-q", "--no-edit", f"origin/{flow_repo.cfg.project.fork_from}")
    paths = {f["path"] for f in run_changes(spec_run.run, spec_run.repo, flow_repo.cfg)["files"]}
    assert "upstream.txt" not in paths and "docs/specs/x.md" in paths

def test_a_binary_change_has_no_line_counts(spec_run, flow_repo):
    # commit bytes(range(256)) as blob.bin through spec_run.commit (write a bytes variant, or write it with open(..., "wb"))
    row = next(f for f in run_changes(spec_run.run, spec_run.repo, flow_repo.cfg)["files"] if f["path"] == "blob.bin")
    assert row["added"] is None and row["deleted"] is None

@pytest.mark.parametrize("path", ["upstream.txt", "../x", "README.md", "-p"])
def test_a_diff_of_a_path_not_in_the_changes_is_not_found(spec_run, flow_repo, path):
    with pytest.raises(NotFound):
        change_diff(spec_run.run, spec_run.repo, flow_repo.cfg, path, 1024)

def test_a_run_with_no_branch_has_no_changes(flow_repo):
    with pytest.raises(NotFound, match="no branch"):
        run_changes(flow_repo.new_run(), flow_repo.root, flow_repo.cfg)
```

`land_on_origin` is `conftest.py:361`. The merge test then needs `git fetch` in the worktree, which `land_on_origin(fetch=True)` does on the main checkout, and the worktree shares its refs.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_artifacts.py -k "changes or binary_change or no_branch" --import-mode=prepend -q`
Expected: FAIL with `ImportError: cannot import name 'run_changes'`.

- [ ] **Step 3: Amend the spec's route table, then implement the two functions**

- [ ] **Step 4: Run the tests to verify they pass**

Run all of `test_artifacts.py`. Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add docs/specs/agentflow-dashboard-ux-proposal.md agentflow/artifacts.py agentflow/tests/test_artifacts.py
git commit -m "List a run's own changes since its branch left the base, with a capped diff per file."
```

---

### Task 4: The `plan`, `files` and `changes` routes

**Files:**
- Modify: `agentflow/dashboard.py`:
  - `ARTIFACT_ROUTE`'s alternation gains `plan|files|changes`;
  - `_get`'s artifact branch dispatches them;
  - the docstring's route list.
- Test: `agentflow/tests/test_dashboard.py`.

**Interfaces:**
- Consumes: `plan_view.run_plan` (Task 1), `run_files.list_files`/`read_file` (Task 2), `artifacts.run_changes`/`change_diff` (Task 3) and `artifact_max_bytes` (U1).
- Produces:
  - `GET /api/runs/<id>/plan` → `run_plan(run, repo, cfg)`.
  - `GET /api/runs/<id>/files` → `list_files(run)`. With `?name=`, it returns `read_file(run, name, artifact_max_bytes(cfg))`.
  - `GET /api/runs/<id>/changes` → `run_changes(...)`. With `?path=`, it returns `change_diff(..., artifact_max_bytes(cfg))`.
  - Errors are U1's: a doubled parameter → 400, `NotFound` → 404, an unknown run → 404, a POST → 404.

- [ ] **Step 1: Write the failing tests**

```python
def test_the_process_routes_answer(dash, spec_run):
    rid = spec_run.run.id
    assert payload(dash.get(f"/api/runs/{rid}/plan")[2])["path"] in ("docs/plans/x.md", None)
    (spec_run.run.dir / "issue.md").write_text("# Ask\n", encoding="utf-8")
    assert {"name": "issue.md", "group": "request", "size": 6} in payload(dash.get(f"/api/runs/{rid}/files")[2])
    assert payload(dash.get(f"/api/runs/{rid}/files?name=issue.md")[2])["text"] == "# Ask\n"
    changes = payload(dash.get(f"/api/runs/{rid}/changes")[2])
    assert "docs/specs/x.md" in {f["path"] for f in changes["files"]}
    diff = payload(dash.get(f"/api/runs/{rid}/changes?path=docs%2Fspecs%2Fx.md")[2])
    assert "+Scope 3 says another" in diff["diff"]

@pytest.mark.parametrize("route", ["plan", "files", "files?name=issue.md", "changes", "changes?path=notes.md"])
def test_the_process_routes_keep_the_refusal_matrix(dash, spec_run, route):
    # the same five checks as U1's test_the_artifact_routes_keep_the_refusal_matrix
    ...

@pytest.mark.parametrize("query", ["files?name=..%2Fevents.jsonl", "files?name=state.json",
                                   "changes?path=..%2F..%2Fetc%2Fpasswd", "changes?path=README.md"])
def test_the_process_routes_refuse_what_is_not_listed(dash, spec_run, query):
    assert dash.get(f"/api/runs/{spec_run.run.id}/{query}")[0] == 404

def test_a_doubled_name_is_a_400(dash, spec_run):
    assert dash.get(f"/api/runs/{spec_run.run.id}/files?name=pr.md&name=issue.md")[0] == 400
```

If U1's refusal-matrix test was written as a helper, call it. If it wasn't, turn its body into `assert_refusal_matrix(dash, run, route)` in `test_dashboard.py` and use it in both places.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_dashboard.py -k "process_routes or doubled_name" --import-mode=prepend -q`
Expected: FAIL (each route answers 404 `not found`).

- [ ] **Step 3: Add the routes**

Add to the docstring's route list: "`GET /api/runs/<id>/plan` (the plan's tasks with status, attempts, review, cost and blast radius), `/files[?name=]` (the run's documents, from a fixed list of names) and `/changes[?path=]` (the branch's own changes since it left the base, and one file's diff)".

- [ ] **Step 4: Run the tests to verify they pass**

Run all of `test_dashboard.py`. Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agentflow/dashboard.py agentflow/tests/test_dashboard.py
git commit -m "Serve a run's plan tasks, its documents and its branch changes to the dashboard."
```

---

### Task 5: The Plan tab's Tasks mode

**Files:**
- Create: `agentflow/dashboard_ui/plan.js`, added to `UI_SCRIPTS` after `artifact.js`.
- Modify:
  - `agentflow/dashboard_ui/artifact.js`: the Plan tab picks its mode;
  - `agentflow/dashboard_ui/main.js`: `runRefresh` refetches `/plan` when the Plan tab is open in Tasks mode;
  - `agentflow/dashboard.html`: the task table's styles and the status colours, reusing `STATUS_CLASS` colours.
- Modify: `docs/agentflow-playbook.md`, the dashboard paragraph (line 329). Add one sentence: "The Plan tab's Tasks mode lists each plan task with its status, tries, review, cost and what a blocked task holds up."
- Modify: `CHANGELOG.md`.
- Test: `agentflow/tests/test_dashboard_js.py` (new) and `agentflow/tests/test_dashboard.py`.

**Interfaces:**
- Consumes: `/plan` (Task 4), U1's `parseMarkdown`/`markdownNodes`, `patchChildren`, `entry`, `money`, `goRun` and `ui.run`.
- Produces, in `plan.js`. The pure helpers sit at the top and touch no DOM at load time.
  - `STATUS_LABEL = {committed: "✓ committed", blocked: "⛔ blocked", parked: "◆ parked", running: "● running", failed: "✗ failed", waiting: "○ waiting on", independent: "◇ independent", pending: "○ pending", deferred: "deferred", done_before: "✓ done before"}`. For `waiting`, the `blocked_by` numbers are appended.
  - `STATUS_ORDER = ["blocked", "parked", "failed", "running", "waiting", "independent", "pending", "deferred", "committed", "done_before"]`, the order of groups when grouping by status.
  - `ranges(ns: number[]) -> string`: a run of three or more consecutive numbers becomes `a–b`, and the rest are listed. So `[10,11,12,20,22,24]` → `"10–12, 20, 22, 24"`, and `[3,4]` → `"3, 4"`.
  - `taskSummary(plan) -> string`, for example `"5 tasks · 1 committed · 1 blocked · 2 waiting on it · 1 independent"`. It always has the total, then the non-zero counts in the order committed, running, blocked, parked, failed, waiting ("waiting on it"), independent, deferred.
  - `filterTasks(tasks, filter: "all"|"stopped"|"findings") -> tasks`:
    - `stopped` keeps `blocked` and `parked`;
    - `findings` keeps tasks whose `review.findings` isn't empty.
  - `groupTasks(tasks, by: "status"|"wave") -> [{key, label, tasks}]`:
    - by status, in `STATUS_ORDER`, with empty groups dropped;
    - by wave, `"Wave 1"`… in wave order, then `"Cycle"` for `wave === null`.
  - `planEntries(detail, plan, sub) -> entry[]`:
    - the header (`taskSummary`, the Filter and Group controls, a **Document** switch);
    - one group heading per group;
    - one `<details data-key="task-<n>">` row per task, showing # · title · `depends_on` · status · `"<attempts> tr(y|ies)"` (+ `" · <resolvers> resolvers"`) · review (`"✓ 0"`, `"<blocking> blocking"`, `"skipped"` or `"—"`) · `money(cost)`.
    - A `blocked` or `parked` row adds a line: the stop summary, `"blocks: " + ranges(blocks)` and `"independent: " + ranges(<independent tasks>)`, plus an **Open decision** link to `#/run/<id>/overview/decision`.
    - Expanding a row shows the task text (`markdownNodes(parseMarkdown(text))`), its files, its sessions (label, model · effort, exit, `money(spend)`), its gates and its review findings. The sessions are plain rows: U3 turns them into links to the per-run Agents tab.
  - **Plan tab routing.** In `artifact.js`, `sub === "tasks"` or `/^task-\d+$/` shows Tasks, and opens and scrolls to that row for `task-<n>`. A path, or `"document"`, shows Document. No `sub` shows Tasks when `plan.started`, and Document otherwise.
  - **No tasks.** `plan.path === null` reads "No plan for this run yet." A plan with no `### Task N:` heading reads "The runner found no `### Task N:` heading in `<path>`." Both show the Document switch.
  - **UI state.** The filter and the grouping live in `ui.plan = {filter, group}` and survive refreshes. Rows patch by `task-<n>`.

- [ ] **Step 1: Write the failing tests**

Move U1's `NODE`, `LOADER`, `js` and `walk` from `test_dashboard_markdown.py` to `conftest.py`, so the new file can import them.

```python
# test_dashboard_js.py
from conftest import NODE, js
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not on PATH")

T = lambda n, status, **kw: {"n": n, "status": status, "wave": kw.get("wave", 0), "blocks": kw.get("blocks", []),
                             "blocked_by": kw.get("blocked_by", []), "review": kw.get("review")}
PLAN = {"summary": {"total": 5, "committed": 1, "blocked": 1, "parked": 0, "running": 0, "failed": 0,
                    "waiting": 2, "independent": 1, "deferred": 0},
        "tasks": [T(1, "committed"), T(2, "blocked", wave=1, blocks=[3, 4]), T(3, "waiting", wave=2, blocked_by=[2]),
                  T(4, "waiting", wave=2, blocked_by=[2]), T(5, "independent", wave=1,
                  review={"findings": [{"text": "x"}]})]}

def test_the_summary_line_reads_like_the_spec():
    assert js(["plan.js"], "taskSummary(args)", PLAN) == \
        "5 tasks · 1 committed · 1 blocked · 2 waiting on it · 1 independent"

def test_ranges_compress_runs():
    assert js(["plan.js"], "ranges(args)", [10, 11, 12, 20, 22, 24]) == "10–12, 20, 22, 24"
    assert js(["plan.js"], "ranges(args)", [3, 4]) == "3, 4"
    assert js(["plan.js"], "ranges(args)", []) == ""

def test_grouping_and_filters():
    by_status = js(["plan.js"], "groupTasks(args, 'status').map(g => [g.label, g.tasks.map(t => t.n)])", PLAN["tasks"])
    assert [g[1] for g in by_status] == [[2], [3, 4], [5], [1]]
    by_wave = js(["plan.js"], "groupTasks(args, 'wave').map(g => [g.label, g.tasks.map(t => t.n)])",
                 PLAN["tasks"] + [T(6, "pending", wave=None)])
    assert by_wave == [["Wave 1", [1]], ["Wave 2", [2, 5]], ["Wave 3", [3, 4]], ["Cycle", [6]]]
    assert js(["plan.js"], "filterTasks(args, 'stopped').map(t => t.n)", PLAN["tasks"]) == [2]
    assert js(["plan.js"], "filterTasks(args, 'findings').map(t => t.n)", PLAN["tasks"]) == [5]

# test_dashboard.py
def test_the_plan_tab_has_a_tasks_mode():
    plan_js = (UI_DIR / "plan.js").read_text(encoding="utf-8")
    for needle in ("/plan", "blocks: ", "independent: ", "No plan for this run yet", "Open decision", "ui.plan"):
        assert needle in plan_js, needle
    assert UI_SCRIPTS.index("artifact.js") < UI_SCRIPTS.index("plan.js") < UI_SCRIPTS.index("main.js")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_dashboard_js.py agentflow/tests/test_dashboard.py -k "summary_line or ranges or grouping or tasks_mode" --import-mode=prepend -q`
Expected: FAIL.

- [ ] **Step 3: Implement `plan.js` and the Plan tab's modes**

- [ ] **Step 4: Run the tests, then check in a browser**

Run `test_dashboard.py`, `test_dashboard_js.py` and `test_dashboard_markdown.py`. Expected: PASS, with no node test skipped.

In a browser on `bin/agentflow dashboard`, with a run in `build`:
- `#/run/<id>/plan` opens Tasks, and `#/run/<id>/plan/task-2` opens and scrolls to row 2;
- a blocked task shows "blocks: …";
- Group by wave reorders the rows, and the choice survives live events;
- **Document** shows U1's viewer.

- [ ] **Step 5: Add the changelog line and commit**

Under `### Added`: "The Plan tab's Tasks mode lists each plan task with its status, tries, review and cost, groups the tasks by status or by dependency wave, and shows what a blocked or parked task holds up and which tasks could go on without it."

```bash
git add agentflow/dashboard_ui agentflow/dashboard.html agentflow/dashboard.py agentflow/tests docs/agentflow-playbook.md CHANGELOG.md
git commit -m "Show the plan as a task board with status, tries, review, cost and each stop's blast radius."
```

---

### Task 6: The Files tab

**Files:**
- Create: `agentflow/dashboard_ui/files.js`, added to `UI_SCRIPTS` after `plan.js`.
- Modify: `agentflow/dashboard_ui/run.js`: `runTabEntries`'s `files` branch hands over to `fileEntries`.
- Modify: `agentflow/dashboard_ui/main.js`: `runRefresh` refetches `/files` and `/changes` when the Files tab is open.
- Modify: `agentflow/dashboard.html`: the file-list and changes styles.
- Modify: `CHANGELOG.md`.
- Test: `agentflow/tests/test_dashboard.py`.

**Interfaces:**
- Consumes: `/files` and `/changes` (Task 4), U1's `parseMarkdown`, `markdownNodes` and `diffNode`, and the `run_detail` fields `transcript`, `transcript_file` and `steps`.
- Produces, in `files.js`:
  - `fileEntries(detail, sub) -> entry[]`, where `sub` is a run-file name from `#/run/<id>/files/<name>` (`encodeURIComponent` keeps its `/`). It draws these parts, in order:
    - `files:list`: the files by group, as links to `#/run/<id>/files/<name>`, each with its size.
    - `files:doc:<name>`: the chosen file, by default the first in the list.
      - `.md` files render with `markdownNodes`, and get a **Raw** toggle.
      - `bug.log` is a `<pre>`.
      - A truncated file shows "Showing the first `<n>` KB."
    - `files:changes`: "Changes since the base: `<N>` files, +`<added>` −`<deleted>`", then one `<details data-key="change:<path>">` per file, with the status letter, the path and the counts. Opening it fetches that file's diff once and draws it with `diffNode`.
      - `complete: false` adds "Only the first 2000 files are listed."
      - A 404 (no branch yet) reads "No branch yet: nothing has changed."
    - `files:transcript` and `files:records`: U0's "Transcript tail" and "Step records" sections, kept until U3 replaces them.
  - The `progress.md` and `handoff.md` sections U0 drew go away, because both are in the list now.
  - **UI state.** Diffs and file texts are cached in `ui.files = {texts: Map, diffs: Map}` per run, keyed by name and by `path@head`, so an open diff survives a refresh and a new head refetches it.

- [ ] **Step 1: Write the failing tests**

```python
def test_the_files_tab_lists_the_run_documents_and_the_changes():
    files_js = (UI_DIR / "files.js").read_text(encoding="utf-8")
    for needle in ("/files", "/files?name=", "/changes", "/changes?path=", "Changes since the base",
                   "No branch yet", "Showing the first", "Transcript tail", "Step records", "ui.files"):
        assert needle in files_js, needle
    run_js = (UI_DIR / "run.js").read_text(encoding="utf-8")
    files_branch = run_js[run_js.index('tab === "files"'):run_js.index("}", run_js.index('tab === "files"'))]
    assert "fileEntries(" in files_branch and '"file:handoff"' not in run_js and '"file:progress"' not in run_js
    assert UI_SCRIPTS.index("plan.js") < UI_SCRIPTS.index("files.js") < UI_SCRIPTS.index("main.js")
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_dashboard.py -k "files_tab" --import-mode=prepend -q`
Expected: FAIL.

- [ ] **Step 3: Implement `files.js` and wire the tab**

- [ ] **Step 4: Run the tests, then check in a browser**

Run all of `test_dashboard.py` and `test_portability.py`. Expected: PASS.

In a browser, with a finished run whose `handoff.md` is larger than 16 KB:
- the Files tab lists `issue.md`, one handoff per phase, the refine notes and `pr.md`;
- `handoffs/build.md` reads in full;
- opening a changed file shows its diff, and the diff stays open through live events;
- `#/run/<id>/files/handoffs%2Fbuild.md` survives a reload.

- [ ] **Step 5: Add the changelog line and commit**

Under `### Added`: "The Files tab shows the run's own documents in full (`issue.md`, each phase's handoff, refine notes, `pr.md`, `progress.md`), and the branch's changes since it left the base with a diff per file: the pull request's diff, readable before the pull request exists."

```bash
git add agentflow/dashboard_ui agentflow/dashboard.html agentflow/dashboard.py agentflow/tests/test_dashboard.py CHANGELOG.md
git commit -m "Show a run's documents in full and its branch changes with a diff per file on the Files tab."
```

---

### Task 7: Whole-slice verification

There's no new code in this task. If a check fails, fix it in the task that owns the code.

- [ ] **Step 1:** Run `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests --import-mode=prepend -q`. Expected: all pass, with no node test skipped on a machine that has node. This is the full suite, which takes 25–60 min.
- [ ] **Step 2:** Run `bin/agentflow validate`. Expected: `0 warnings`.
- [ ] **Step 3:** Run `git diff main -- agentflow/tests/fixtures/explain`. Expected: empty.
- [ ] **Step 4:** Run `git diff main -- agentflow/dashboard.py | grep -n PAGE_CSP`. Expected: no changed line.
- [ ] **Step 5:** Walk Verification "U2" from the spec in a browser, with a scratch `XDG_STATE_HOME`:
  - on the 5-task fixture plan, task 2 shows blocked with "blocks: 3, 4", tasks 3 and 4 show "waiting on 2", and task 5 shows independent;
  - `handoffs/build.md` reads in full when `handoff.md` exceeds 16 KB;
  - after a merge of `origin/<fork_from>`, Changes lists only the run's own files.
