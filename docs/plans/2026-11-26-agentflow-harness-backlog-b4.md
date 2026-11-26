# Backlog file, slice B4 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A repository that runs `agentflow init` gets a `backlog` pack and an empty `backlog.yml`; a synced issue that an item names shows once, as that item; an unnamed synced issue has **Add to backlog**; `validate` notes that `capability_map` and `specs` are deprecated; and the playbook has a Backlog section.

**Architecture:** B1–B3 built the file, the runner's writes, the claims and the Board. B4 adds no new module. `detect.py` and `init.py` choose and write the `backlog` kind, `config.py` gains one pure function that returns the deprecation notes, `backlog.py` learns that an item can name a synced issue (one rule in `_issue_blocker`, one issue-to-item lookup), and `dashboard.py`/`board.js` get one more file op. The rest is documentation and the 3.12 release.

**Tech Stack:** Python 3.12, stdlib + `pyyaml`, pytest (`fake_agent.py`, the fake `gh`, a bare repository as `origin`), plain JS in `agentflow/dashboard_ui/board.js`.

**Spec:** `docs/specs/agentflow-backlog-file-proposal.md` (approved 2026-11-24), slice **B4**: the "Issues" bullet of D7, the `init` and "Deprecation" bullets of D8, and the Rollout row. Read the B1–B3 plans (`docs/plans/2026-11-24-agentflow-harness-backlog-b1.md`, `…-25-…-b2.md`, `…-b3.md`) for what already exists.

### What B4 does not do

- It does not remove or change `capability_map` or `specs` (4.0 removes them).
- It does not make `agentflow init` run `import`, and does not edit a pack's `work_source` for you: `import` prints the line to switch to, as it does today.
- It does not sync items back to GitHub issues (spec: Boundaries, "Ask first").

### Plan decisions

The spec leaves these open.

- **`init` and a specs folder.** A repository whose base branch has no `backlog.yml` and no specs folder gets `work_source: {kind: backlog, path: backlog.yml}` and an empty `backlog.yml`. One that has a specs folder (`project.docs.specs` detected) keeps today's `work_source: {kind: specs}` so its Board is not empty at once, and `init` prints the offer: `agentflow backlog import --dry-run`. One whose base branch already has a regular file `backlog.yml` gets the `backlog` kind and nothing is written (the file is the team's). So `init` never writes a deprecated kind except where there are spec files to import.
- **`init` writes `backlog.yml` after the pack**, with `safeio.write_new` (exclusive, no symlink followed), and only in the repository root. Any `FileExistsError` or `SymlinkRefused` is a printed note, not a failure: the pack is already written and valid. Provisional rendering for `doctor` writes no `backlog.yml`.
- **The note is not an `Issue` of `validate()`.** It is a third level, `note`, returned by `config.deprecation_notes(cfg)` and printed by `flow._print_validation` after the other issues, like `guards.settings_coverage` is. `validate()`, the Settings page and every test that asserts `validate(...) == []` are unchanged, and the "(N warnings)" count excludes notes.
- **A dropped or done item still absorbs its issue.** Any item of `backlog.yml` that names `issue: N` hides the synced card `issue:N`, whatever its status, so one issue never shows as two cards, and **Add to backlog** can't create a second item for it.
- **Only a module-lane issue can be added.** An item's lane must be `dashboard.backlog.module_lane` to start from the Board (`NOT_STARTABLE`), so **Add to backlog** is offered only for a synced issue whose lane is the module lane. A bug-lane issue stays a card that starts as today.
- **The added item** has id `issue-<N>` (`ISSUE_MODULE`, so the id never comes from the issue's text), `title` the issue's cached title cut to one line of at most `backlog_file.TITLE_MAX`, `lane` the issue's lane, `status: backlog`, `issue: N`. The server builds it from the cache; the page sends only the number and the sha.
- **Playbook section number:** the new section is `## 16. The backlog`, after §15.

## Global Constraints

- Engine dependencies stay stdlib + `pyyaml>=6`. No project strings in the engine (`test_portability.DENYLIST`).
- The golden `explain` files stay byte-identical. The `python` fixture pack keeps `capability_map`.
- `agentflow init` writes into the repository only `.agentflow/config.yaml` and, in the one case above, `backlog.yml`: both exclusively, neither over anything that exists, neither through a symlink. It reads the repository from its base branch through git, never from the working tree, and runs nothing from it.
- Item text (`title`, `notes`) and a claim's `by` reach only the page's escaped text, never an argv, a branch name, a module id or a prompt. An issue's title is item text.
- `validate` prints `(0 warnings)` on a pack with only notes.
- `uvx ruff@0.17.4 check .` must print `All checks passed!` before every commit. Tests run with `uv run --no-project --with pyyaml --with pytest pytest <file> --import-mode=prepend -q`.
- Every user-visible change adds one line under `## [Unreleased]` in `CHANGELOG.md`.
- Commit messages are one imperative sentence ending with `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>` (the `project.commit_trailer` of this pack).
- `init.py` and `detect.py` trigger the security overlay: plan and review with `security-and-hardening` and the `security-auditor` agent.

## Review Focus

1. **`backlog.yml` at the root is a symlink, a directory or a dangling symlink when `init` runs.** The pack is still written, the path is left alone, `init` says so, and nothing outside the repository is written (Task 2).
2. **The base branch already has a `backlog.yml`** (even an unparseable one). `init` writes the `backlog` kind, overwrites nothing and still finishes; detection reads it from git, not the working tree (Task 2).
3. **An issue named by a `dropped` or `done` item.** Its synced card stays hidden, never doubles as a second card, and **Add to backlog** is refused for it (Tasks 3, 4).
4. **A hostile issue title** (newline, a leading `- `, `# x`, `{a: b}`, 500 characters) added as an item: the file still parses, holds exactly one new item whose title is one line of at most 200 characters, and the id is `issue-<N>` (Task 4).
5. **Add to backlog twice, or after the file changed on disk.** A stale sha is refused (409), and a second add says the issue is already an item (Task 4). A `capability_map` or `specs` pack never shows the button.

---

### Task 1: The deprecation note in `validate`

**Files:**
- Modify: `agentflow/config.py` (`Issue.level` at line 259; new `deprecation_notes` next to `validate` at line 3638)
- Modify: `agentflow/flow.py` (`_print_validation`, lines 55-70)
- Modify: `docs/agentflow-playbook.md` (§14, the `work_source` kinds at the `capability_map` and `specs` bullets)
- Modify: `CHANGELOG.md`
- Create: `agentflow/tests/test_backlog_deprecation.py`

**Interfaces:**
- Consumes: `cfg.work_source` (`WorkSource | None`, `.kind`).
- Produces: `config.deprecation_notes(cfg: Config) -> list[Issue]`; `Issue.level` may be `"note"`; `config.WORK_SOURCE_DEPRECATED: str`, a format string with `{kind}`.

- [ ] **Step 1: Write the failing tests** in `agentflow/tests/test_backlog_deprecation.py` (build each config with `dataclasses.replace(cfg, work_source=WorkSource(kind=...))` from the `python` fixture pack's loaded config):
  - `test_a_capability_map_pack_gets_one_note`: `deprecation_notes(cfg)` is one `Issue` with `level == "note"`, `path == "work_source"`, and a message containing `capability_map`, `agentflow backlog import` and `4.0`.
  - `test_a_specs_pack_gets_one_note`: the same for `kind == "specs"`.
  - `test_a_backlog_pack_and_a_pack_with_no_work_source_get_none`: `[]` for `backlog` and for `work_source=None`.
  - `test_validate_itself_is_unchanged`: `validate(cfg, repo)` holds no issue whose level is `note`.
  - `test_the_cli_prints_the_note_and_does_not_count_it`: through `flow._print_validation` with `capsys` on the fixture repo, a line starts with `note work_source:` and the `(N warnings)` figure equals the number of lines starting with `warning `.

- [ ] **Step 2: Run them to see them fail.**
  Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_backlog_deprecation.py --import-mode=prepend -q`
  Expected: FAIL (`deprecation_notes` is not defined).

- [ ] **Step 3: Implement** `deprecation_notes` in `config.py` (extend `Issue.level` to `Literal["error", "warning", "note"]`), and in `flow._print_validation` take `issues = validate(cfg, repo) + settings_coverage(cfg, repo) + deprecation_notes(cfg)`, count `errors` as before and print `({warnings} warnings)` with `warnings = sum(1 for i in issues if i.level == "warning")`. The note's text names the kind, says 4.0 removes it, and points to `agentflow backlog import --dry-run` and playbook §16.

- [ ] **Step 4: Check the callers of `Issue.level`.** `grep -rn 'level ==' agentflow/config_edit.py agentflow/doctor.py agentflow/flow.py` : none may treat "not error" as "warning" for a `note`. Run `agentflow/tests/test_config.py`, `test_config_edit.py`, `test_pack_fix.py` and the golden `explain` tests; all pass with no edit to a golden file.

- [ ] **Step 5: Docs and changelog.** In §14 add to the `capability_map` and `specs` bullets: "Deprecated: `validate` prints a note, not a warning, and 4.0 removes the kind. `agentflow backlog import` writes the `backlog` file." Add under `## [Unreleased]` → `### Changed`: "`validate` prints a note (not a warning, so `0 warnings` still holds) on a `capability_map` or `specs` work source, pointing to `agentflow backlog import`; both kinds are deprecated and 4.0 removes them."

- [ ] **Step 6: Run** the new file, `agentflow/tests/test_portability.py`, then `uvx ruff@0.17.4 check .`. Expected: PASS, `All checks passed!`.

- [ ] **Step 7: Commit** `git add agentflow/config.py agentflow/flow.py agentflow/tests/test_backlog_deprecation.py docs/agentflow-playbook.md CHANGELOG.md && git commit -m "Print a deprecation note in validate for the capability_map and specs work sources."` (with the trailer).

---

### Task 2: `init` and `detect` write the `backlog` kind

**Files:**
- Modify: `agentflow/detect.py` (`Detected` at line 62; `detect` near line 646)
- Modify: `agentflow/init.py` (`pack_data` line 154, `render_pack` line 187, `init` line 631)
- Modify: `agentflow/tests/test_detect.py`, `agentflow/tests/test_init.py`, `agentflow/tests/test_plug_and_play.py`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Consumes: `backlog_file.FILE` (`"backlog.yml"`), `backlog_file.render([])` (the empty file's text), `safeio.write_new(path, text) -> Path` (raises `FileExistsError`, `safeio.SymlinkRefused`), `src.exists(path)` of `detect._Source`.
- Produces: `Detected.backlog_file: bool` (default `False`: a regular `backlog.yml` is at the base branch's root); `init.pack_data` puts `work_source: {kind: backlog, path: backlog.yml}` plus the fixed `dashboard.backlog` block (`module_lane: feature`, the same `new_task` forms as today) when `d.backlog_file` or `"specs" not in docs`; it keeps `{kind: specs}` when `"specs" in docs` and `not d.backlog_file`. A new `init.empty_backlog(d: Detected) -> bool` says whether `init` should create the file (the `backlog` kind chosen and `d.backlog_file` false).

- [ ] **Step 1: Write the failing detect test** in `test_detect.py`: `test_a_backlog_file_on_the_base_branch_is_detected` (a toy repo whose base commit has `backlog.yml`: `detected.backlog_file is True`; one with it only untracked in the working tree: `False`; one whose `backlog.yml` is a symlink in the tree: `False`).

- [ ] **Step 2: Write the failing init tests** in `test_init.py`:
  - `test_no_specs_directory_writes_a_backlog_work_source`: `pack_data(_detected(plans="docs/plans"), ...)["work_source"] == {"kind": "backlog", "path": "backlog.yml"}`, the `dashboard` block is the same one the specs test pins, `yaml.safe_load(render_pack(...)) == data`, and the rendered text carries a comment line saying `backlog.yml` lists every item.
  - `test_a_specs_directory_keeps_the_specs_kind` (the existing `test_a_found_specs_directory_adds_a_specs_work_source_and_a_feature_backlog` stays as it is and must still pass).
  - `test_a_backlog_file_on_the_base_branch_wins_over_a_specs_directory`: `_detected(specs="docs/proposals")` with `backlog_file=True` gives the `backlog` kind and `init.empty_backlog(...)` is `False`.
  - Through `init.init` on `toy_repo`: `test_init_creates_an_empty_backlog_file` (`backlog.yml` equals `backlog_file.render([])`, `backlog_file.read(path)` parses to no items and no problems, and the printed output names the file); `test_init_leaves_a_backlog_file_that_is_there` (a pre-existing untracked `backlog.yml` with other content keeps its bytes, the pack is still written, and a note says it was left); `test_init_does_not_follow_a_symlinked_backlog_file` (parametrised: a symlink to a file outside the repo, a dangling symlink, a directory named `backlog.yml`: each keeps its target's bytes, `.agentflow/config.yaml` exists, the exit code is 0, and a note says why no file was written); `test_init_offers_import_when_it_finds_specs` (a specs directory: no `backlog.yml` written, the output contains `agentflow backlog import --dry-run`).
  - `test_init_writes_nothing_else`: after `init` on `toy_repo`, `git status --porcelain --untracked-files=all` lists exactly `.agentflow/config.yaml` and `backlog.yml` (replaces the claim of `test_init_writes_only_the_pack`: update that test's expectation).

- [ ] **Step 3: Run them to see them fail.**
  Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_detect.py agentflow/tests/test_init.py --import-mode=prepend -q`
  Expected: FAIL.

- [ ] **Step 4: Implement `detect`**: set `Detected.backlog_file = src.exists(backlog_file.FILE)` in `detect()`. It reads through git like every other file; nothing else reads the working tree.

- [ ] **Step 5: Implement `init`.** `pack_data` and `render_pack` choose the kind per the Interfaces block (one fixed comment per kind above the `work_source` block; nothing from the repository is copied into either). In `init()`, after `safeio.write_new(target, ...)` succeeds, when `empty_backlog(found)` call `safeio.write_new(repo / backlog_file.FILE, backlog_file.render([]))`; catch `FileExistsError` and `safeio.SymlinkRefused` and remember a note, and catch `OSError` the same way (the pack stays). The `finally` and the interrupted-baseline paths are untouched: no `backlog.yml` exists before the pack does. Print `wrote <path>` for the file, or the note, beside `wrote <target>`. In "next steps", for the specs kind add `agentflow backlog import --dry-run   (writes backlog.yml from your spec files; then switch work_source)`; for the backlog kind add `agentflow backlog add <id> --title "…" --status ready`. Update the module docstring's "exactly once" sentence to mention the second file. Escape every printed path with `_show`.

- [ ] **Step 6: Update `test_plug_and_play.py`.**
  - `test_init_to_dry_run` (all three toy repos): before the `explain`/`run --dry-run` calls run `agentflow backlog add greet --title Greet --status ready` (the pack is now a `backlog` pack, and a `module_lane` run needs an item). After init, `git_status(repo)` is `"?? .agentflow/config.yaml\n?? backlog.yml"` (and the same after the `add`); the cleanup removes `.agentflow` and `backlog.yml` before asserting `git_status(repo) == ""`. `validate` still ends in `(0 warnings)` after `confirm_gates`.
  - `test_init_in_a_repository_with_specs_writes_a_specs_backlog` stays as is, and gains an assertion that the validate output has one `note work_source:` line and that `backlog.yml` was not created.
  - Add `test_init_in_a_repository_with_a_backlog_file_uses_it`: the toy `docs` repo with a committed one-item `backlog.yml` → `cfg.work_source.kind == "backlog"`, `backlog.yml` bytes unchanged, `backlog.snapshot(cfg, repo)["cards"]` holds that item.

- [ ] **Step 7: Run** `test_detect.py`, `test_init.py`, `test_plug_and_play.py` (slow: run it last), `test_portability.py`. Expected: PASS.

- [ ] **Step 8: Changelog** under `### Added`: "`agentflow init` writes a `backlog` pack and an empty `backlog.yml` for a repository with no specs folder, and uses an existing `backlog.yml` as it is. A repository with a specs folder keeps the `specs` kind, and `init` offers `agentflow backlog import`." Also `### Changed`: "`init` now writes `backlog.yml` as well as `.agentflow/config.yaml`, in the one case above; neither is written over anything or through a symlink."

- [ ] **Step 9: Lint and commit.** `uvx ruff@0.17.4 check .`; `git add agentflow/detect.py agentflow/init.py agentflow/tests/test_detect.py agentflow/tests/test_init.py agentflow/tests/test_plug_and_play.py CHANGELOG.md && git commit -m "Write the backlog kind and an empty backlog.yml from init when the repository has no specs folder."`.

---

### Task 3: An item that names a synced issue absorbs its card

**Files:**
- Modify: `agentflow/backlog.py` (`_issue_blocker` line 753, `_picture` line 971 (the `issues` list), `_module_issue` line 1021, `_deps_unknown` line 1026, `snapshot` line 1087 (the module-card loop))
- Test: `agentflow/tests/test_backlog.py` (use its `ibk` fixture and the helpers the `issue` tests at lines 944-1270 use)
- Modify: `CHANGELOG.md`

**Interfaces:**
- Consumes: `pic["file_items"]: dict[str, BacklogItem]` (each has `.id`, `.issue: int | None`, `.status`), `pic["issues"]` (the synced entries: `number`, `url`, `in_progress`, `deps_complete`, `deps_note`).
- Produces: `_issue_item(number: str, pic: dict) -> BacklogItem | None` (the item that names issue `number`, first in file order); `_module_issue(module, pic)` also returns the issue an item of that id names; a module card of an item that names a synced issue carries `url` (the issue's) and, when the issue carries the in-progress label with no live run here, `status == "taken"` with `note == TAKEN`.

- [ ] **Step 1: Write the failing tests** in `test_backlog.py` (a `backlog`-kind pack with `issues.sync_label` set and issues #7 and #8 synced; item `crm` names `issue: 7`):
  - `test_an_item_naming_a_synced_issue_hides_the_issue_card`: `snapshot(...)["cards"]` has a card `module:crm` with `url` = issue #7's URL and no card `issue:7`; `issue:8` is still a card.
  - `test_a_dropped_or_done_item_still_absorbs_its_issue`: parametrised over `dropped` and `done`; no `issue:7` card.
  - `test_the_item_is_taken_when_its_issue_carries_the_in_progress_label`: issue #7 `in_progress`, no run here → card `module:crm` has `status == "taken"`, `note == backlog.TAKEN`, and `begin_start(cfg, repo, "module:crm")` raises `Refused`; with a live run here for `crm`, the card is not `taken`.
  - `test_the_item_inherits_its_issues_blockers`: issue #7 blocked by open issue #8 → card `module:crm` has status `blocked` and `blocked_by` names #8 (an item whose id is not `issue-7`).
  - `test_starting_the_absorbed_issue_card_is_refused`: `begin_start(cfg, repo, "issue:7")` raises `Refused` with `item crm` in the message.
  - `test_an_item_with_the_issue_derived_id_is_unchanged`: item `issue-7` naming `issue: 7` shows one card, as before B4.
  - `test_a_capability_map_pack_is_unchanged`: no `url` key is added to its module cards.

- [ ] **Step 2: Run them to see them fail.**
  Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_backlog.py -k "absorb or naming or taken_when or inherits or derived_id" --import-mode=prepend -q`
  Expected: FAIL.

- [ ] **Step 3: Implement.**
  - `_issue_blocker`: first check `_issue_item(number, pic)`; when found return `f"issue #{number} is item {item.id}: start module:{item.id} instead"` (for a `dropped` item, `f"issue #{number} is the dropped item {item.id}"`). Existing checks follow unchanged.
  - `_picture`: the `issues` list's `"module"` is the id of `_issue_item` when there is one, else `ISSUE_MODULE.format(...)`, so `dependency_view` treats the item as the issue's module. `_module_issue` and `_deps_unknown` consult `pic["file_items"]` first.
  - `snapshot`: in the module-card loop, when `file_item.issue is not None` and the issue is in `pic["issues"]`, pass `url=entry["url"]` to `make`, and apply the `taken` rule used for issue cards (`number in taken and card["status"] != "starting"`).

- [ ] **Step 4: Run** the new tests, then all of `test_backlog.py`, `test_backlog_runs.py`, `test_backlog_file.py` and the dashboard tests that read `snapshot` (`test_dashboard.py -k backlog`). Expected: PASS, no golden change.

- [ ] **Step 5: Changelog** `### Added`: "In a backlog pack, an item that names `issue: N` shows once on the Board, with the issue's link, and hides issue #N's synced card; the issue's in-progress label still marks the item as taken, and the issue's blockers still hold it."

- [ ] **Step 6: Lint and commit.** `git add agentflow/backlog.py agentflow/tests/test_backlog.py CHANGELOG.md && git commit -m "Show an item that names a synced issue once, in place of the issue's card."`.

---

### Task 4: Add to backlog on a synced issue

**Files:**
- Modify: `agentflow/backlog.py` (new `add_issue_item` after `add_file_item`, line 1631)
- Modify: `agentflow/dashboard.py` (`FILE_OPS` line 137, `_file_edit` line 176)
- Modify: `agentflow/dashboard_ui/board.js` (`backlogNode` line 274)
- Modify: `agentflow/views.py` only if the card's fields do not reach the page (they come from `snapshot` cards)
- Test: `agentflow/tests/test_backlog.py`, `agentflow/tests/test_dashboard.py` (near line 1393), `agentflow/tests/test_dashboard_js.py` (near line 630)
- Modify: `docs/agentflow-playbook.md` (§8's Backlog paragraph, if it lists card buttons), `CHANGELOG.md`

**Interfaces:**
- Consumes: `_FILE_LOCK`, `_backlog_path`, `_sha_arg`, `backlog_file.append_item(path, item, expect_sha=sha) -> str`, `ISSUE_MODULE`, `module_queue.module_lane(cfg)`, `pic["issues"]`.
- Produces: `backlog.add_issue_item(cfg: Config, repo: Path, number: str, sha: str) -> str` (the file's new sha-256; `Refused` when the pack is not `backlog`, the issue is not synced, its lane is not the module lane, or an item (any status) already names it or has the id `issue-<N>`; `Invalid` for a number that is not digits; `backlog_file.Stale` for a stale sha); `FILE_OPS["add_issue"] = ("number",)`; every card `kind == "issue"` in a `backlog` pack has `addable: bool` (true only when the button applies); `board.js` draws an **Add to backlog** button on such a card that posts `{op: "add_issue", number, sha256}` to `/api/backlog/file`.

- [ ] **Step 1: Write the failing backend tests** in `test_backlog.py`:
  - `test_add_issue_item_appends_an_item_from_the_cache`: for synced #7 (lane = module lane, title "Fix the thing"), the file gains item `issue-7` with `title == "Fix the thing"`, `lane`, `status == "backlog"`, `issue == 7`, nothing else changed, the file is not committed, and the returned sha equals the file's sha-256. After the next `snapshot` there is a card `module:issue-7` and no `issue:7`.
  - `test_add_issue_item_cuts_a_hostile_title`: parametrised titles `"line one\nline two"`, `"- [x]: {a: b}"`, `"# not a comment"`, `"x" * 500`: the file parses with no new problem, the new item's title is one line of at most `backlog_file.TITLE_MAX` characters, and the id is `issue-7`.
  - `test_add_issue_item_refusals`: `Refused` for an issue an item already names (also a `dropped` one), for an item whose id is `issue-7` that names another issue, for a bug-lane issue, for an issue not synced, and for a `specs` pack; `Invalid` for `"7; x"`; `backlog_file.Stale` for a wrong sha.
  - `test_the_issue_card_says_whether_it_is_addable`: `addable` is `True` for a module-lane issue in a `backlog` pack, `False` for a bug-lane one, and absent in a `specs` pack.

- [ ] **Step 2: Write the failing dashboard tests.** In `test_dashboard.py`: `POST /api/backlog/file` with `{"op": "add_issue", "number": "7", "sha256": <fresh>}` returns 200 and the new sha; a stale sha is 409; a body with a stray key, or `number` that is not a string, is 400; a `specs` pack answers 409. In `test_dashboard_js.py`: a card `{kind: "issue", addable: true, id: "7"}` renders a button whose text is `Add to backlog` and which posts `{op: "add_issue", number: "7", sha256: SHA}` once on click; `addable: false` renders no such button; the button's label comes from fixed text, not the issue's title.

- [ ] **Step 3: Run them to see them fail.**
  Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_backlog.py agentflow/tests/test_dashboard.py agentflow/tests/test_dashboard_js.py -k "issue_item or add_issue or addable" --import-mode=prepend -q`
  Expected: FAIL.

- [ ] **Step 4: Implement `backlog.add_issue_item`** under `_FILE_LOCK`: build the picture, check the refusals above, make the `BacklogItem` per the Plan decisions (the title through `_clean_title` then `backlog_file.TITLE_MAX`), and `backlog_file.append_item(path, item, expect_sha=_sha_arg(sha))`. Add the `addable` field to issue cards in `snapshot`. `dashboard._file_edit` handles `op == "add_issue"` by `backlog.add_issue_item(cfg, repo, _text(body, "number", required=True), sha)`; map `Refused` to 409 and `Invalid` to 400 the way the other ops do.

- [ ] **Step 5: Implement the button** in `board.js`: in `backlogNode`, `if (item.kind === "issue" && item.addable) buttons.push(<button, class "", text "Add to backlog", onclick: stop propagation, post the op with the current `backlog_sha256`>)`, using the same post-and-reload helper `addSpec` uses (line 659 region), so a 409 asks the person to reload exactly as Add does.

- [ ] **Step 6: Run** the three test files in full. Expected: PASS.

- [ ] **Step 7: Changelog** `### Added`: "A synced GitHub issue that no item names has an **Add to backlog** button in a backlog pack's Board: it appends the item `issue-<N>` (status `backlog`, the issue's title and lane) to `backlog.yml` in your working tree. Only module-lane issues have it." Mention the `POST /api/backlog/file` op `add_issue` in the dashboard module docstring (`dashboard.py` line 44 region).

- [ ] **Step 8: Lint and commit.** `git add agentflow/backlog.py agentflow/dashboard.py agentflow/dashboard_ui/board.js agentflow/tests/ docs/agentflow-playbook.md CHANGELOG.md && git commit -m "Add an Add to backlog button to a synced issue in a backlog pack."`.

---

### Task 5: The playbook's Backlog section, the docs and the 3.12 release

**Files:**
- Modify: `docs/agentflow-playbook.md` (new `## 16. The backlog`; §3 and §13 where they describe what `init` writes, lines 40-77 and 523-555; §10 cheat sheet header if it lists sections)
- Modify: `CLAUDE.md` (the "Setting up a repository" paragraph on `init.py`; the playbook pointer line)
- Modify: `docs/specs/agentflow-backlog-file-proposal.md` (Rollout table: B4 row marked shipped; D8's `init` bullet: the specs-folder case as decided here)
- Modify: `pyproject.toml`, `agentflow/pack.py` (`ENGINE_VERSION`), `CHANGELOG.md`
- Test: `agentflow/tests/test_packaging.py` (runs, no edit)

**Interfaces:**
- Consumes: the shipped behaviour of Tasks 1-4 and of B1-B3 (commands, keys, claims, Board).
- Produces: playbook §16, with these subsections: *The file* (`backlog.yml`, the four statuses, fields, order is priority), *What the runner writes* (D3), *Claims and the Board* (D4, D5: phase and status come from the claim, `stale`, `release`), *Editing it* (hand, `agentflow backlog add|set|done|undone|list|import|release`, the Board's Add/Edit/Move, the `local` badge and the uncommitted working-tree copy), *Issues* (an item's `issue:`, absorption, **Add to backlog**), *Not on the backlog* (`ignore_specs`), *Migrating* (`agentflow backlog import`, the deprecated kinds), *Troubleshooting* (a `claim_lost` stop, a host that refuses custom refs, a claim left on a done item).

- [ ] **Step 1: Write the section** from the code and the existing §8/§10/§14 text, linking to them instead of repeating key semantics (§14 stays the reference for each key). Every command it names must exist: check each against `bin/agentflow backlog --help`.

- [ ] **Step 2: Update §3 and §13** (what `init` writes: the `backlog` kind and an empty `backlog.yml` in the three cases of the Plan decisions; its output offers `import` for a specs folder). Update the `init.py` sentence in `CLAUDE.md` ("writes nothing else in the repository" becomes "writes `.agentflow/config.yaml` and, when it picks the `backlog` kind and finds no file, an empty `backlog.yml`"), and its design-docs line to say slice B4 has shipped.

- [ ] **Step 3: Update the spec**: Rollout row B4 gets "(shipped in 3.12)"; D8's `init` bullet says "writes the `backlog` kind and an empty `backlog.yml` for a repository with no specs folder or with a `backlog.yml`, and offers `import` for one with a specs folder."

- [ ] **Step 4: Bump the version.** `pyproject.toml` `version = "3.12"` and `ENGINE_VERSION = "3.12"` in `agentflow/pack.py`; in `CHANGELOG.md` rename `## [Unreleased]` to `## [3.12] - 2026-11-26` and add an empty `## [Unreleased]` above it; add under `### Added`: "The playbook has a Backlog section (§16)."

- [ ] **Step 5: Verify docs and release.**
  Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_packaging.py agentflow/tests/test_portability.py --import-mode=prepend -q`
  Expected: PASS.
  Run: `bin/agentflow validate` Expected: `ok: config.yaml is valid (0 warnings)`.
  Run: `uvx ruff@0.17.4 check .` Expected: `All checks passed!`.

- [ ] **Step 6: Full suite once** (25-60 min): `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests --import-mode=prepend -q`. Expected: all pass.

- [ ] **Step 7: Commit** `git add docs CLAUDE.md pyproject.toml agentflow/pack.py CHANGELOG.md && git commit -m "Document the backlog in the playbook and release 3.12."`.

---

## Self-review

- **Spec coverage:** `init`/`detect` write the kind (Task 2); the playbook's Backlog section (Task 5); issue absorption (Task 3) and **Add to backlog** (Task 4), D7's Issues bullet; the deprecation note (Task 1); the Done-when ("`test_plug_and_play` passes on the three toy repositories", Task 2 Step 6). The Success criterion 6 ("existing packs and golden `explain` files unchanged, apart from the deprecation note") is pinned by Task 1's `validate_itself_is_unchanged` and the golden tests.
- **Consistency:** `deprecation_notes`, `Detected.backlog_file`, `init.empty_backlog`, `_issue_item`, `addable`, `add_issue_item` and the op name `add_issue` are used with the same spelling wherever they appear.
- **Not in the spec, decided here:** the specs-folder case of `init`, the module-lane limit of **Add to backlog**, and that a dropped or done item absorbs its issue (Plan decisions). Task 5 writes the first into the spec.
