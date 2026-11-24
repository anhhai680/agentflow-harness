# Backlog file, slice B1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A pack can set `work_source: {kind: backlog, path: backlog.yml}`. The Backlog, `agentflow next`,
`agentflow queue`, `modules_done` and `validate` then read their items from that file, and this repository
migrates to it.

**Architecture:** A new `agentflow/backlog_file.py` parses, validates, renders and edits the file. It is the only
module that knows the file's shape. `work_source.py` gains the `backlog` kind on top of it, so `module_queue`,
`conditions`, `backlog` and `flow` keep calling `work_source.items`/`file`/`skipped` and change only where the
item's own fields (`spec`, `status`, `title`, `lane`, `issue`) matter. `agentflow/backlog_import.py` builds the
first file from today's work source, form cards, done marks and spec files.

**Tech Stack:** Python 3.12, stdlib + `pyyaml` (`yaml.safe_load` only), pytest, the dashboard's plain JS
(`agentflow/dashboard_ui/board.js`).

**Spec:** `docs/specs/agentflow-backlog-file-proposal.md` (approved 2026-11-24). This plan covers slice **B1**
(Rollout table): D2, D5 without claims, D6, D8's `import` and hand marks, D9, the `backlog` kind, and
`backlog.ignore_specs`. Read the spec's "The file" table and D5 before Task 1.

### What B1 does not do (later slices, per the spec's Rollout)

- **B2:** the runner's writes (D3: a new item's block, `spec:`/`plan:`, `status: done` at `finish`), the guard
  that keeps agents off the file, the dashboard's Add/Edit/Move and the **Add** button on Not-on-the-backlog
  entries (D7), and `validate --fix` for "evidence says done". In B1, a new feature card in a `backlog` pack
  behaves as in a `specs` pack: the run writes the spec and no item is added. The "evidence says done" warning
  names `agentflow backlog set <id> status=done` instead.
- **B3:** claims (D4), remote cards, `backlog.claims`, `backlog.claim_ref` and `backlog.claim_stale_h`.
- **B4:** `init`/`detect`, the deprecation note, and issue absorption.
- **Not in the spec's slices yet:** starting an item whose `lane` isn't `dashboard.backlog.module_lane`. `run
  <lane> --module` is only known to work for the module lane today. B1 lists and validates such items, but their
  Start is refused with a fixed message, and `queue` skips them. Raise this with the user before B2.

**Plan decisions** (the spec leaves these open):

- The parser lives in a new `backlog_file.py`, not in `sources.py`, which stays the Markdown reader.
- The Not-on-the-backlog scan (D6) skips everything under `project.docs.plans`: plans are not specs, and this
  repository's `project.docs.specs` is `docs`, which holds `docs/plans/`.
- `import` moves `runs/done_marks.json` aside to `runs/done_marks.json.imported` once it has written the file, so
  `finished.recorded` can't keep a hand mark alive that the file no longer says.
- B1 needs no third fixture pack. Tests switch the `python` pack with `tmp_cfg`/`flow_repo.configure`, as the
  `specs` tests do.

## Global Constraints

- Engine dependencies stay stdlib + `pyyaml>=6`, and the file is read with `yaml.safe_load` only.
- No project strings in the engine (`test_portability.DENYLIST`).
- The golden files `agentflow/tests/fixtures/explain/*.txt` stay byte-identical, and the `python` fixture pack
  keeps `capability_map`.
- Limits: file ≤ 1 MB (`FILE_MAX = 1_000_000`), ≤ 2000 items, `id` ≤ 64 chars and `sources.MODULE_ID`,
  `title` one line ≤ 200 chars, `notes` ≤ 4096 chars, `issue` a positive integer.
- `status` ∈ `backlog | ready | done | dropped`, default `backlog`. `version` must be `1`.
- `title`, `notes` and unknown keys never reach an argv, a branch name, a module id or a prompt. Start passes only
  `--module=<id>` and, when the item has one, `--issue=<n>`.
- Every new or changed config key ships with all four of these: a Settings field or a `_PROTECTED_KEYS` entry, a
  `validate` check, a `test_config_edit.py` case, and a playbook §14 line.
- Reads of the file never raise in the Board's refresh thread: a bad file means no items and one error.
- Run `uvx ruff@0.17.4 check .` before every commit.
- Tests: `uv run --no-project --with pyyaml --with pytest pytest <file> --import-mode=prepend -q`.

## Review Focus

1. **A file saved by another editor:** a BOM, CRLF line endings, or a comment between items. It still parses,
   and `set`/`append` keep the CRLF endings and the comments byte for byte outside the edited lines (Task 2).
2. **Two branches that each appended the same id and were merged:** the second copy gets an error naming both
   positions, the first copy is kept, and the Board still renders (Task 1).
3. **An item whose `spec:` is absolute, has `..`, or names a symlink:** an error, and the item is kept with
   `spec = ""`, so `{spec}` falls back to `project.docs.specs/spec_file`. A file outside the repository is never
   read (Task 1, Task 5).
4. **A `done_marks.json` left over after `import`** that says done for an item the file says `ready`: it must not
   make `modules_done` or the queue treat the item as done (Task 8).
5. **The Board refreshing while an editor is half-way through writing `backlog.yml`:** the snapshot reads no
   items and returns one `backlog_note`, without raising, and the next refresh recovers (Task 6).

---

### Task 1: Parse and validate `backlog.yml`

**Files:**
- Create: `agentflow/backlog_file.py`
- Test: `agentflow/tests/test_backlog_file.py`

**Interfaces:**
- Produces:
  - `STATUSES = ("backlog", "ready", "done", "dropped")`; `FILE_MAX = 1_000_000`; `ITEMS_MAX = 2000`;
    `KEYS = ("id", "title", "lane", "status", "spec", "plan", "depends_on", "issue", "notes")`
  - `@dataclass class BacklogItem(sources.ModuleNode)`, with fields `title: str = ""`, `lane: str = ""`,
    `status: str = "backlog"`, `spec: str = ""`, `plan: str = ""`, `issue: int | None = None`, `notes: str = ""`.
    `deps` is `depends_on`.
  - `@dataclass(frozen=True) class Problem: level: Literal["error", "warning"]; where: str; message: str`. `where`
    looks like `items[3].lane`, or `backlog.yml` for the whole file.
  - `parse(text: str) -> tuple[list[BacklogItem], list[Problem]]`
  - `read(path: Path) -> tuple[list[BacklogItem], list[Problem]]`: reads through `safeio.read_bytes(path,
    FILE_MAX + 1)`, so it never follows a symlink. A missing file is `([], [Problem("error", "backlog.yml",
    "missing: agentflow backlog import writes it")])`.

- [ ] **Step 1: Write the failing tests**

```python
GOOD = "version: 1\nitems:\n  - id: a\n    title: A\n    status: ready\n  - id: b\n    title: B\n    depends_on: [a]\n    issue: 7\n"

def test_parse_reads_every_key_with_defaults():
    items, problems = backlog_file.parse(GOOD)
    assert problems == []
    assert items == [BacklogItem("a", [], title="A", status="ready"),
                     BacklogItem("b", ["a"], title="B", status="backlog", issue=7)]

def test_parse_accepts_a_bom_crlf_and_comments():
    text = "﻿# top\r\n" + GOOD.replace("\n", "\r\n").replace("  - id: b", "  # between\r\n  - id: b")
    assert [i.id for i in backlog_file.parse(text)[0]] == ["a", "b"]

@pytest.mark.parametrize("text,where", [
    ("- a\n", "backlog.yml"), ("version: 2\nitems: []\n", "version"), ("version: 1\nitems: {}\n", "items"),
    ("{{{", "backlog.yml"), ("version: 1\nitems:\n" + "  - {id: x, title: X}\n" * 2001, "items")])
def test_a_broken_file_has_no_items_and_one_error(text, where):
    items, problems = backlog_file.parse(text)
    assert items == [] and [(p.level, p.where) for p in problems] == [("error", where)]

@pytest.mark.parametrize("item,where", [
    ({"id": "Bad Id", "title": "t"}, "items[0].id"), ({"id": "a"}, "items[0].title"),
    ({"id": "a", "title": "two\nlines"}, "items[0].title"), ({"id": "a", "title": "t", "status": "wip"}, "items[0].status"),
    ({"id": "a", "title": "t", "issue": 0}, "items[0].issue"), ({"id": "a", "title": "t", "notes": "x" * 4097}, "items[0].notes")])
def test_a_bad_item_is_dropped_with_an_error(item, where):
    items, problems = backlog_file.parse(yaml.safe_dump({"version": 1, "items": [item]}))
    assert items == [] and [(p.level, p.where) for p in problems] == [("error", where)]

@pytest.mark.parametrize("spec", ["/etc/passwd", "../x.md", "docs/../../x.md"])
def test_a_spec_path_leaving_the_repository_is_cleared_with_an_error(spec):
    items, problems = backlog_file.parse(yaml.safe_dump({"version": 1, "items": [{"id": "a", "title": "t", "spec": spec}]}))
    assert items[0].spec == "" and problems[0].where == "items[0].spec"

def test_a_duplicate_id_keeps_the_first_and_names_both_positions():
    items, problems = backlog_file.parse(GOOD + "  - id: a\n    title: again\n")
    assert [i.title for i in items] == ["A", "B"]
    assert problems == [Problem("error", "items[2].id", "duplicate id a (first at items[0])")]

def test_an_unknown_key_is_a_warning_and_the_item_stays():
    items, problems = backlog_file.parse(GOOD.replace("    issue: 7\n", "    owner: me\n"))
    assert len(items) == 2 and [(p.level, p.where) for p in problems] == [("warning", "items[1].owner")]

def test_read_refuses_a_symlink_and_an_oversize_file(tmp_path): ...  # both: items == [], one error
```

- [ ] **Step 2: Run them and see them fail**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_backlog_file.py --import-mode=prepend -q`
Expected: FAIL with `ModuleNotFoundError: agentflow.backlog_file`.

- [ ] **Step 3: Implement `parse` and `read` in `agentflow/backlog_file.py`**

Strip a leading BOM before `yaml.safe_load`. A file-level problem returns `([], [that one error])`. An item-level
error drops only that item. A bad `spec`/`plan` path clears that field and keeps the item. A path is bad when it
is absolute or has a `..` segment after `posixpath.normpath`. Whether the file exists or is a symlink is checked
by `validate` (Task 4), not by the parser. `depends_on` takes a list of ids or one comma-separated string.

- [ ] **Step 4: Run the tests and see them pass**

Same command. Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agentflow/backlog_file.py agentflow/tests/test_backlog_file.py
git commit -m "Parse and validate the backlog file."
```

### Task 2: Render and edit the file as text

**Files:**
- Modify: `agentflow/backlog_file.py`
- Test: `agentflow/tests/test_backlog_file.py`

**Interfaces:**
- Consumes: `parse`, `BacklogItem`, `KEYS` (Task 1).
- Produces:
  - `HEADER: str`: the comment block that `render` writes first. It is the two comment lines in the spec's "The
    file" example, with `refs/agentflow/claims/<id>` kept as written.
  - `render(items: list[BacklogItem]) -> str`: `version: 1` and `items:`, keys in `KEYS` order, empty and default
    fields left out (`status` is always written), lists in flow style (`[a, b]`), and any string that YAML would
    read as something else quoted with `json.dumps`.
  - `class EditRefused(ValueError)`
  - `set_fields(path: Path, item_id: str, fields: dict[str, object]) -> None`
  - `append_item(path: Path, item: BacklogItem) -> None`

- [ ] **Step 1: Write the failing tests**

```python
def test_render_round_trips():
    items = [BacklogItem("a", [], title="A: colon", status="ready", spec="docs/a.md"),
             BacklogItem("b", ["a"], title="yes", issue=3, notes="multi\nline")]
    assert backlog_file.parse(backlog_file.render(items)) == (items, [])

def test_set_fields_changes_only_the_named_lines(tmp_path):
    path = tmp_path / "backlog.yml"; path.write_text("# keep me\r\n" + GOOD.replace("\n", "\r\n"), newline="")
    backlog_file.set_fields(path, "a", {"status": "done", "plan": "docs/plans/p.md"})
    text = path.read_text(newline="")
    assert text.startswith("# keep me\r\n") and "\n" not in text.replace("\r\n", "")
    items, _ = backlog_file.parse(text)
    assert (items[0].status, items[0].plan) == ("done", "docs/plans/p.md") and items[1] == backlog_file.parse(GOOD)[0][1]

def test_set_fields_refuses_an_unknown_id_a_bad_value_and_a_flow_style_item(tmp_path): ...
    # each raises EditRefused, and the file's bytes are unchanged

def test_append_item_adds_after_the_last_item_and_refuses_a_duplicate(tmp_path): ...
```

- [ ] **Step 2: Run them and see them fail**

Run: `… pytest agentflow/tests/test_backlog_file.py --import-mode=prepend -q`
Expected: FAIL with `AttributeError: ... 'render'`.

- [ ] **Step 3: Implement**

`set_fields` finds the item's block: the `- id: <id>` line at the list's indent, up to the next line at that
indent or less that isn't blank or a comment. It replaces each `key:` line in the block, or inserts the key after
the `- id:` line, using `render`'s value formatting. It keeps each line's own ending. It refuses (`EditRefused`,
file untouched) in these cases:
- the id isn't in the file;
- the block isn't one-key-per-line block style;
- the key being replaced has a multi-line value;
- the key isn't in `KEYS`.

Every edit is checked before it is written: `parse` the new text and require that the edited item has the new
values, every other item is equal to before, and no new error appeared. Then write with `safeio.write_text`.
`append_item` inserts `render`'s block for the item at the end of the `items:` list and is checked the same way.

- [ ] **Step 4: Run the tests and see them pass.** Expected: PASS.

- [ ] **Step 5: Commit** — `git commit -m "Render the backlog file and edit it in place, keeping comments and line endings."`

### Task 3: The `backlog` work source kind

**Files:**
- Modify: `agentflow/config.py` (`WORK_SOURCE_KINDS`, `WorkSource.kind`, `_work_source`, a new `BacklogPolicy` and
  `Config.backlog`), `agentflow/defaults/config.yaml` (a top-level `backlog: {ignore_specs: []}`),
  `agentflow/work_source.py`, `agentflow/pipeline.py:239` (`_check_new_module`), `agentflow/backlog.py:1178`
- Test: `agentflow/tests/test_work_source.py`

**Interfaces:**
- Consumes: `backlog_file.read`, `BacklogItem` (Task 1).
- Produces:
  - `WORK_SOURCE_KINDS = ("capability_map", "specs", "backlog")`. With `kind: backlog`, `path` defaults to
    `"backlog.yml"`.
  - `@dataclass class BacklogPolicy: ignore_specs: tuple[str, ...] = ()`, read from `backlog.ignore_specs`.
  - `work_source.items(cfg, repo)`: for `backlog`, the file's items in file order, without `dropped`.
  - `work_source.all_items(cfg, repo) -> list[BacklogItem]`: dropped ones too; `[]` for other kinds.
  - `work_source.item(cfg, repo, module: str) -> BacklogItem | None`
  - `work_source.problems(cfg, repo) -> list[backlog_file.Problem]`: `[]` for other kinds.
  - `work_source.skipped(cfg, repo)`: for `backlog`, the sorted repo-relative paths that D6 lists.
  - `work_source.noun(cfg)`: `"backlog"` for this kind.
  - `work_source.add_item` is a no-op for `backlog`, as it is for `specs` (B2 writes the block).

- [ ] **Step 1: Write the failing tests**

```python
BACKLOG = "version: 1\nitems:\n  - {id: a, title: A, status: done}\n  - {id: b, title: B, depends_on: [a], spec: docs/specs/b.md}\n  - {id: c, title: C, status: dropped}\n"

def test_items_reads_the_backlog_file_without_dropped(tmp_cfg, tmp_path):
    cfg = load_config(tmp_cfg({"work_source": {"kind": "backlog", "path": None}}))
    assert cfg.work_source.path == "backlog.yml"
    write(tmp_path, "backlog.yml", BACKLOG)
    assert [i.id for i in work_source.items(cfg, tmp_path)] == ["a", "b"]
    assert [i.id for i in work_source.all_items(cfg, tmp_path)] == ["a", "b", "c"]
    assert work_source.item(cfg, tmp_path, "b").spec == "docs/specs/b.md" and work_source.noun(cfg) == "backlog"

def test_skipped_lists_specs_no_item_names_outside_plans_and_ignore_specs(tmp_cfg, tmp_path):
    cfg = load_config(tmp_cfg({"work_source": {"kind": "backlog", "path": None},
                               "project": {"docs": {"specs": "docs", "plans": "docs/plans"}},
                               "backlog": {"ignore_specs": ["notes/*"]}}))
    write(tmp_path, "backlog.yml", BACKLOG)
    for rel in ("docs/specs/b.md", "docs/specs/x.md", "docs/plans/p.md", "docs/notes/n.md", "docs/specs/y.txt"):
        write(tmp_path, rel, "# t\n")
    (tmp_path / "docs/specs/link.md").symlink_to(tmp_path / "docs/specs/x.md")
    assert work_source.skipped(cfg, tmp_path) == ["docs/specs/x.md"]

def test_a_missing_or_broken_backlog_file_has_no_items_and_says_why(tmp_cfg, tmp_path): ...
def test_a_new_module_in_a_backlog_pack_is_refused_like_a_specs_pack(tmp_cfg): ...  # pipeline._check_new_module
```

- [ ] **Step 2: Run them and see them fail.** Run: `… pytest agentflow/tests/test_work_source.py --import-mode=prepend -q`. Expected: FAIL.

- [ ] **Step 3: Implement.** The D6 scan walks `project.docs.specs` with `os.walk(followlinks=False)`, skips
  symlinked files and anything under `project.docs.plans`, and examines at most `sources.SPECS_MAX` files. It
  takes `*.md` and drops each file that any item's `spec` names, dropped items included. A file whose path
  relative to `project.docs.specs` matches a `backlog.ignore_specs` glob (`fnmatch.fnmatchcase`) is dropped too.
  `file()` applies the existing `_unsafe` check to `backlog` as well.

- [ ] **Step 4: Run the tests and see them pass.** Also run `test_explain_golden.py` and `test_config.py`. Expected: PASS.

- [ ] **Step 5: Commit** — `git commit -m "Add the backlog work source kind."`

### Task 4: `validate` checks, the Settings field and the pack reference

**Files:**
- Modify: `agentflow/config.py` (`Validator.work_source`, a new `Validator.backlog_source`, a new
  `Validator.backlog_policy`, called from the same place as `v.work_source()`), `agentflow/config_edit.py`
  (a Form field), `docs/agentflow-playbook.md` §14 (the `work_source` line, plus a new `backlog.ignore_specs` line)
- Test: `agentflow/tests/test_config.py`, `agentflow/tests/test_config_edit.py`

**Interfaces:**
- Consumes: `work_source.all_items/problems/skipped` (Task 3), `finished.recorded`.
- Produces: `validate` issues whose path is the problem's `where` prefixed with the file name, for example
  `backlog.yml items[1].lane`.

- [ ] **Step 1: Write the failing tests** (`test_config.py`), one per check, each asserting its `(level, path)`:
  - Each `Problem` from the file comes through at its own level.
  - `lane` not in `cfg.lanes`: error.
  - `lane` not equal to `module_lane`: warning `"runs in <lane>: B1 starts module_lane items only"`.
  - `depends_on` an unknown id: error. A dependency cycle: error naming the ring.
  - `depends_on` a `dropped` item: warning.
  - A `spec`/`plan` that is a symlink or resolves outside the repository: error. A missing one: warning.
  - Each Not-on-the-backlog path: warning `"<path> is not on the backlog: agentflow backlog add --spec <path>, or list it in backlog.ignore_specs"`.
  - An id that `finished.recorded` holds as done while the file says otherwise: warning naming `agentflow backlog set <id> status=done`.
  - No `module_lane`: warning.
  - `backlog.ignore_specs` not a list of one-line, relative, `..`-free strings ≤ 200 chars: error.

  `test_config_edit.py` gets two cases: the Form saves `backlog.ignore_specs` as a list and refuses a non-list;
  and YAML text may not switch `work_source` to `backlog` (like `test_yaml_text_may_not_switch_the_work_source_to_specs`).

- [ ] **Step 2: Run them and see them fail.** Expected: FAIL.

- [ ] **Step 3: Implement.** Reuse `module_queue._cycle_through` for the cycle check. The Form field is
  `(("backlog", "ignore_specs"), "Spec files that are not backlog items (globs under the specs folder)", {"kind": "list"})`.
  The playbook lines follow §14's style. They say `kind: backlog` takes `path` (default `backlog.yml`), what the
  file holds, and that `ignore_specs` is not protected.

- [ ] **Step 4: Run the tests and see them pass.** Expected: PASS.

- [ ] **Step 5: Commit** — `git commit -m "Validate the backlog file and its settings."`

### Task 5: Runs, done detection and the queue read the item

**Files:**
- Modify: `agentflow/conditions.py:make_ctx` (`{spec}`), `agentflow/finished.py` (`FILE_SAYS`),
  `agentflow/module_queue.py` (`done_reason`, `assess_modules`, `queue_tick`), `agentflow/flow.py:cmd_queue`
- Test: `agentflow/tests/test_work_source.py`, `agentflow/tests/test_next_queue.py`

**Interfaces:**
- Consumes: `work_source.item` (Task 3).
- Produces:
  - `finished.FILE_SAYS = "backlog says done"`. It is not vetoable (`vetoable` stays false for it).
  - `make_ctx(...).values["spec"]` is the item's `spec` when the pack is `backlog` and the item names one, else
    the value it has today.

- [ ] **Step 1: Write the failing tests**
  - `test_spec_placeholder_is_the_items_spec`: an item with `spec: docs/specs/b.md` gives `make_ctx(cfg, repo,
    "feature", {"module": "b"}).values["spec"] == "docs/specs/b.md"`, and an item without one gives
    `docs/<spec_file>`.
  - `test_a_done_item_is_done_with_file_says`: after `assess_modules`, the entry for `a` has
    `status == "done"` and `done_reason == "backlog says done"`, with no `gh` call (assert the fake `gh` log is
    empty).
  - `test_hand_marks_are_ignored_by_a_backlog_pack`: `finished.mark(repo, "b", "me")`, then assess: `b` is not done.
  - `test_queue_starts_ready_items_only`: items `r` (ready), `i` (backlog) and `x` (ready, `lane: bug`); one
    `queue_tick`: only `r` is spawned.
  - `test_cmd_queue_ignores_include_specs_with_a_note`: the output names `--include-specs is ignored` and the
    command doesn't refuse.

- [ ] **Step 2: Run them and see them fail.** Expected: FAIL.

- [ ] **Step 3: Implement.**
  - `done_reason`: first, for a `BacklogItem` with `status == "done"`, return `(finished.FILE_SAYS, None)`. For
    its spec name, use `Path(item.spec).name` when the item has a spec.
  - `assess_modules`: pass `({}, None)` for marks when the kind is `backlog`.
  - `queue_tick`: skip an entry whose `BacklogItem` has `status != "ready"` or `lane` not in `("", module_lane(cfg))`.

- [ ] **Step 4: Run the tests and see them pass.** Also run `test_conditions.py` and `test_explain_golden.py`. Expected: PASS.

- [ ] **Step 5: Commit** — `git commit -m "Read each backlog item's spec and status in runs, done detection and the queue."`

### Task 6: The Board shows the file

**Files:**
- Modify: `agentflow/backlog.py` (`_picture`, the module cards in the snapshot, `begin_start`, `snapshot`'s
  return), `agentflow/dashboard.py` (the start route passes `confirm`), `agentflow/views.py` (pass `backlog_dropped`
  and `backlog_note` through), `agentflow/dashboard_ui/board.js`
- Test: `agentflow/tests/test_backlog.py`, `agentflow/tests/test_dashboard_js.py`

**Interfaces:**
- Consumes: `work_source.items/all_items/problems/skipped` (Task 3).
- Produces:
  - Module cards gain `title`, `planning` (`"backlog"` or `"ready"`) and `startable: bool`. A `backlog` item's
    `label` is its `title`.
  - The snapshot gains `dropped: [{"id", "title"}]` and `backlog_note: str`, the first file-level error or `""`.
  - `begin_start(cfg, repo, key, confirm: bool = False) -> StartJob`.

- [ ] **Step 1: Write the failing tests** (a `bbk` fixture: `sbk`, but with `work_source: {kind: backlog}` and a
  committed `backlog.yml`):
  - `test_a_backlog_card_shows_its_title_and_planning_status`
  - `test_dropped_items_are_listed_apart_and_not_as_cards`
  - `test_starting_a_backlog_status_item_needs_confirm`: `begin_start(..., "module:i")` raises `Refused` whose text
    says the item is an idea (status backlog) and must be confirmed. With `confirm=True` it starts.
  - `test_an_item_in_another_lane_is_not_startable`: `startable is False`, and `begin_start` raises `Refused` with
    `runs in bug: only dashboard.backlog.module_lane items start from the Board`.
  - `test_an_item_with_an_issue_starts_with_the_issue_param`: the spawned argv holds `--module=b` and `--issue=7`,
    and no title text.
  - `test_a_half_written_file_gives_a_note_and_no_cards`: write `"version: 1\nitems:\n  - id: a\n    title: [unclosed"`;
    the snapshot has no module cards, a non-empty `backlog_note`, and no exception. After the file is fixed, the
    next snapshot has the cards.
  - `test_dashboard_js`: the skipped list's heading is `Not on the backlog` when `work_source_kind === "backlog"`;
    an idea card's Start arms on the first click and sends `confirm: true` on a second click within 4 s.

- [ ] **Step 2: Run them and see them fail.** Expected: FAIL.

- [ ] **Step 3: Implement.** `board.js`:
  - an "idea" chip on `planning === "backlog"` cards;
  - Start disabled with the note on `startable === false`;
  - a folded "Dropped (N)" under the Backlog column;
  - `backlog_note` shown like `sync_note`;
  - the deps-picker hint says "Only items already in backlog.yml." for this kind.

  The two-click arming copies the Inbox's 4-second approve pattern.

- [ ] **Step 4: Run the tests and see them pass.** Expected: PASS.

- [ ] **Step 5: Commit** — `git commit -m "Show backlog items on the Board with their title, planning status and dropped list."`

### Task 7: `agentflow backlog list | add | set`, and `done`/`undone` as `set`

**Files:**
- Modify: `agentflow/flow.py` (the `backlog` subparser, `cmd_backlog`), `agentflow/backlog.py` (a new `backlog_view`)
- Test: `agentflow/tests/test_backlog_cli.py` (new)

**Interfaces:**
- Consumes: `backlog_file.set_fields/append_item` (Task 2), `work_source` (Task 3), `_picture` (Task 6).
- Produces: `backlog_view(cfg, repo) -> list[dict]`, one dict per item in file order with keys `id`, `title`,
  `lane`, `status` (the file's), `state` and `detail`. `state` follows D5 without claims: `dropped`, `done`, the
  local run's `run.state["phase"]`, `blocked`, `ready` or `backlog`. `detail` is the done reason, the run id, or
  the blocking ids.

- [ ] **Step 1: Write the failing tests**
  - `list` prints one line per item, `<id>  <state>  <title>`; `--json` prints `backlog_view`; dropped items show
    only with `--all`.
  - `add z --title Z --depends-on a --issue 9` appends the item.
  - `add --spec docs/specs/agentflow-q-proposal.md` derives the id from `project.docs.spec_file`, or the file's
    lowercased slug stem when the name doesn't fit it, takes the title from the spec's first `# ` heading
    (≤ 200 chars), and sets `status: backlog`.
  - `set a status=ready notes="x y"` edits the file; `set a owner=me` exits 1 naming the keys `set` takes.
  - With the `backlog` kind, `done a` writes `status: done` and `undone a` writes `status: ready`, and neither
    touches `runs/done_marks.json`.
  - `list`, `add` and `set` exit 1 on another kind with `needs work_source kind backlog`.

- [ ] **Step 2: Run them and see them fail.** Expected: FAIL.

- [ ] **Step 3: Implement.** Every write goes through `backlog_file` (Task 2). An `EditRefused` prints
  `error: <message>` and exits 1. `--depends-on` takes a comma list. No subcommand commits.

- [ ] **Step 4: Run the tests and see them pass.** Expected: PASS.

- [ ] **Step 5: Commit** — `git commit -m "Add backlog list, add and set, and make done and undone edit the backlog file."`

### Task 8: `agentflow backlog import`

**Files:**
- Create: `agentflow/backlog_import.py`
- Modify: `agentflow/flow.py` (`backlog import [--dry-run]`)
- Test: `agentflow/tests/test_backlog_import.py` (new)

**Interfaces:**
- Consumes: `work_source.items/skipped`, `module_queue.assess_modules`, `queue_lock`, `finished.recorded`,
  `Backlog.load`, `sources.read_header/says_done/plan_spec/parse_plan_text`, `backlog_file.render`.
- Produces: `build(cfg: Config, repo: Path) -> tuple[list[BacklogItem], list[str]]` (the items and notes for the
  user), and `write(cfg, repo, items) -> Path`.

- [ ] **Step 1: Write the failing tests**
  - **From a capability map:** row order for open items, then `done` alphabetically. A module the last assessment
    recorded done gets `done`, and the others get `ready`.
  - **From `specs`:** dependency order, alphabetical within a level, e.g. `b` (no deps), `c` (no deps), `a`
    (depends on `c`) gives `[b, c, a]`, then the `done` items.
  - **Orphan specs** (Not on the backlog in the old kind's terms: `.md` files under `project.docs.specs`, not in
    `project.docs.plans`, that no source item covers) are appended after the source's open items. Each is `done`
    when its header says done (`project.docs.done_status`), or when every plan whose `**Spec:**` names it is fully
    ticked and there's at least one such plan. Otherwise it is `ready`.
  - **A form card with `new_module`** becomes `status: backlog` with the card's label as `title`. A card without
    one is left out, with a note naming its card id.
  - **`spec`/`plan`:** set when the module's spec file or newest plan exists.
  - **`--dry-run`** prints `render(items)` and creates nothing.
  - **The real run** writes the file through `safeio.write_new`, so an existing file is refused, and moves
    `runs/done_marks.json` to `runs/done_marks.json.imported`. Afterwards, `finished.recorded` no longer holds a
    mark-only module. The command prints `work_source: {kind: backlog, path: backlog.yml}`.

- [ ] **Step 2: Run them and see them fail.** Expected: FAIL.

- [ ] **Step 3: Implement.** `build` runs `assess_modules` under `queue_lock` first. When the queue holds the
  lock, `build` uses `finished.recorded` as it stands and adds a note saying so. Ordering is the
  algorithm above; render with Task 2's `render`.

- [ ] **Step 4: Run the tests and see them pass.** Expected: PASS.

- [ ] **Step 5: Commit** — `git commit -m "Add backlog import, which writes the first backlog file from the current work source."`

### Task 9: Migrate this repository and release

**Files:**
- Create: `backlog.yml`
- Modify: `.agentflow/config.yaml` (`work_source`, `backlog.ignore_specs`, the `new_task` comment),
  `docs/agentflow-playbook.md` (§10 cheat-sheet lines for `backlog list/add/set/import`, and §14's
  `agentflow queue --include-specs` lines), `pyproject.toml` and `agentflow/pack.py` (`3.8` → `3.9`), `CHANGELOG.md`

- [ ] **Step 1: Preview the import.** `bin/agentflow backlog import --dry-run`. Expected: items for `harness`,
  `doubt-review` and the twelve `docs/specs/*.md` proposals, plus `docs/agentflow-harness-issue-status.md` and
  `docs/agentflow-playbook.md` as orphans.
- [ ] **Step 2: Run it for real** (`bin/agentflow backlog import`). Then make these hand edits, which are
  judgement calls to confirm with the user before committing:
  - check each item's `done`/`ready` against what has shipped (a multi-slice spec such as throughput may show
    `done` once all its planned slices are ticked);
  - set `backlog-file` to `ready`.
- [ ] **Step 3: Switch the pack.** Set `work_source: {kind: backlog, path: backlog.yml}` and
  `backlog: {ignore_specs: ["agentflow-playbook.md", "agentflow-harness-issue-status.md"]}`. Run
  `bin/agentflow validate`. Expected: `0 warnings`.
- [ ] **Step 4: Check the Board.** `bin/agentflow dashboard`. Every item is a card or under Done, and the Not on
  the backlog list is empty.
- [ ] **Step 5: Release 3.9.** Bump both versions, rename `[Unreleased]` to `## [3.9] - <today>`, and add an
  `Added` line for the `backlog` kind and the `backlog` subcommands. Run `test_packaging.py`, then the full suite,
  then `uvx ruff@0.17.4 check .`. Expected: all pass, `All checks passed!`.
- [ ] **Step 6: Commit** — `git commit -m "Move this repository's backlog to backlog.yml and release 3.9."`
