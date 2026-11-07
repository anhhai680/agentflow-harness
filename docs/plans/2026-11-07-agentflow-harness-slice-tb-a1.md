# Slice TB-A1: Backlog modules from spec files, and feature cards that start them — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A pack with no capability map lists one Backlog module per spec file. The dashboard can start any of them, or a new feature card that becomes a new spec.

**Architecture:**
- **A second work-source kind.** `specs` sits beside `capability_map` in `work_source.py`. It lists `project.docs.specs` and reads each spec's header, cached by file stamp.
- **Consumers.** Every consumer (`backlog`, `module_queue`, `next`, `modules_done`) already goes through `work_source.items`, so each picks the new kind up.
- **Messages.** Wording that names the capability map now comes from `work_source.noun(cfg)`, so a `capability_map` pack's messages stay byte-identical.
- **New feature cards.** In a `specs` pack, a new feature card starts `run feature --module <id> --task <text>`, so the define step writes the new spec.

**Tech Stack:** Python 3.12, stdlib + `pyyaml`, pytest with the fake `gh` (`agentflow/tests/bin/gh`) and the fixture packs.

**Spec:** `docs/plans/2026-11-07-agentflow-harness-slice-tb.md`, section "PR A", items A1, A2 and A4. A3 (done detection) is slice TB-A2 (`docs/plans/2026-11-07-agentflow-harness-slice-tb-a2.md`). B4 (sync and re-check before a start) stays in PR B, where the spec puts it.

**Line numbers** below are as of commit 80bbb1c. The main checkout has unrelated uncommitted edits in `sources.py` and `conditions.py`, so find each site by its name.

## Global Constraints

- **Dependencies:** engine dependencies stay stdlib + `pyyaml>=6`. No new third-party import.
- **No project strings in the engine:** `agentflow/tests/test_portability.py` must pass. Project wording goes only in `.agentflow/`. Spell the `specs` directory name as `detect.py` does, with `_SPEC_DIR`.
- **Golden files:** `agentflow/tests/fixtures/explain/*.txt` stay byte-identical. `git diff --stat agentflow/tests/fixtures/explain/` is empty at the end.
- **Config keys:** every new or changed config key ships with a Form field or a `_PROTECTED_KEYS` entry, its `validate` checks, a `tests/test_config_edit.py` case, and a playbook §14 line. The one key changed here, `work_source.kind: specs`, sits under the already-protected `work_source`.
- **Engine defaults:** `agentflow/defaults/config.yaml` does not change.
- **`capability_map` packs**, the fixture packs among them: every existing message and output stays byte-identical. The changes are additive only:
  - a `skipped` / `backlog_skipped` key (always `[]` for them)
  - a `work_source_kind` key on the board payload
  - `agentflow queue --include-specs` (ignored for them)

  No existing test needs changing. If one does, the implementation is wrong.
- **Untrusted text:** spec headers and file names come from the repository and are untrusted.
  - Every regex over them is linear: no adjacent optional groups that can match the same characters.
  - Lines are cut to `sources.LINE_MAX = 512` before matching.
  - Ids are checked with `fullmatch`.
  - Dependency text that isn't printable ASCII of at most 80 characters becomes the placeholder `<unreadable dependency>`. That placeholder still blocks, and it keeps raw text out of the terminal, `queue.json` and notifications.
  - The page shows repository text only through its escaping helper.
- **Security path:** `init.py` is a security-overlay path. Task 3 is planned and reviewed with `security-and-hardening`.
- **Commit messages:** one imperative sentence ending with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- **Test command shape:** `uv run --no-project --with pyyaml --with pytest pytest <file>[::<test>] --import-mode=prepend -q`.

## Review Focus

1. **Dependency forms never drop a dependency.** These are the cases, and the tests for them are in Task 1:
   - A `Depends on:` line followed by a bullet list, a value wrapped onto the next line, two `Depends on:` lines, or a line cut at `LINE_MAX`: every dependency is kept, or the module is blocked.
   - `none-pay` and `none, a` are dependencies.
   - Only `none`, `none.` or `none (…)` means no dependencies.
2. **Files in the specs directory that are not modules**, under the bare `{module}.md` pattern, never become cards or `validate` warnings. These are `README.md`, `index.md`, `template.md`, `Foo Bar.md`, a symlink, a subdirectory and an unreadable file. Under a prefixed pattern such as `SPEC-{module}.md`, `SPEC-index.md` *is* a module. Test: Task 1.
3. **A `specs` pack whose Backlog would be silently empty** gets a `validate` error or warning naming the cause. The causes are a bad `project.docs.specs`, a `work_source.path` set for `specs`, or no `dashboard.backlog.module_lane`. Test: Task 1.
4. **`agentflow queue` on a `specs` pack** refuses without `--include-specs` and names how many modules it lists. Test: Task 1.
5. **A new feature card whose id clashes with a spec.** It is refused when it is added. If that spec appears between adding the card and pressing Start, it is refused at Start too, so the card's text is never silently dropped. Test: Task 2.

---

### Task 1: `work_source.kind: specs` lists one module per spec file

**Files:**
- Modify: `agentflow/config.py`: `WORK_SOURCE_KINDS` :180, `WorkSource` :198, `_work_source` :668, `Validator.work_source` :1201, `Validator.backlog` :1454
- Modify: `agentflow/work_source.py`
- Modify: `agentflow/sources.py`:
  - move `MODULE_ID` here
  - factor `deps_cell` out of `capability_map`
  - factor `spec_ids` out of `spec_modules`
  - add the header helpers
- Modify: `agentflow/module_queue.py`: `_MODULE_ID` (:47) becomes an alias of `sources.MODULE_ID`; the "unknown dependency" messages (near :463 and :649) use `work_source.noun`
- Modify: `agentflow/conditions.py`: `{capability_map}` (:76); the `_modules_done` message (:274) uses `work_source.noun`
- Modify: `agentflow/backlog.py`: the `_picture` fallback (:359); the messages at :234, :237, in `begin_start` and in `remove` (:323) use `work_source.noun`
- Modify: `agentflow/dashboard.py:490`, which uses `work_source.noun`
- Modify: `agentflow/pipeline.py:158`: `_check_new_module`
- Modify: `agentflow/flow.py`: `cmd_queue` (:640) gets `--include-specs`
- Modify: `docs/agentflow-playbook.md`: §14's `work_source` line (:408), and §10 for `queue --include-specs`
- Test: `agentflow/tests/test_work_source.py`, `agentflow/tests/test_backlog.py`, `agentflow/tests/test_config_edit.py`, `agentflow/tests/test_next_queue.py`

**Interfaces:**
- **Config:**
  - `config.WORK_SOURCE_KINDS = ("capability_map", "specs")`
  - `config.WorkSource(kind: Literal["capability_map", "specs"], path: str)`. For `specs`, `path` must be `""`: the directory is always `project.docs.specs`, the same one `{spec}` uses.
- **`sources`:**
  - `sources.MODULE_ID = re.compile(r"^[a-z0-9][a-z0-9_-]*$")`. Use it only with `fullmatch`.
  - `sources.NOT_MODULES = frozenset({"index", "readme", "template", "changelog"})`. It applies only when `spec_file == "{module}.md"`.
  - `sources.is_module_id(module: str, spec_file: str) -> bool`: `MODULE_ID.fullmatch`, and not in `NOT_MODULES` under the bare pattern.
  - `sources.HEADER_MAX = 8192`, `sources.LINE_MAX = 512` and `sources.SPECS_MAX = 500`, the most directory entries examined.
  - `sources.spec_ids(names: Iterable[str], spec_file: str) -> dict[str, str]`: `{id: name}` for the names that fit `spec_file`. This is the matching half of `spec_modules`, which now calls it. Slice TB-A2 uses it on names read from origin.
  - `sources.read_header(path: Path, limit: int = HEADER_MAX) -> str`: reads at most `limit` bytes, decoding with `errors="replace"`.
  - `sources.spec_header(text: str) -> list[tuple[str, bool]]`: `(line, was_cut)` for each line before the first `## ` heading, leaving out fenced lines (reuse `_fence_flags`). Each line is cut to `LINE_MAX`.
  - `sources.SPEC_DEPENDS = re.compile(r"^[-*>\s]*Depends on\**\s*:[\s*]*(.*)$", re.IGNORECASE)`. The colon is required. It matches `**Depends on:** a`, `**Depends on**: a`, `Depends on: a` and `- Depends on: a`. It doesn't match `Depends on the auth module`.
  - `sources.NO_DEPS = re.compile(r"^none\.?(\s*\(.*\))?\.?$", re.IGNORECASE)`.
  - `sources.CUT_DEP = "<Depends on line too long>"` and `sources.BAD_DEP = "<unreadable dependency>"`.
  - `sources.spec_deps(text: str) -> list[str]`, built as follows:
    1. **Find the lines.** It finds every `spec_header` line matching `SPEC_DEPENDS`. For each one, its value is the captured text, plus:
       - when the captured text is empty or ends with `,`: the following header lines, while they are `-`, `*` or `+` bullets, or more of a comma list
       - when the line was cut: `CUT_DEP`
    2. **Clean the values.**
       - A value that is exactly `NO_DEPS` adds nothing.
       - Otherwise, `deps_cell` runs on the value. Each id is stripped of backticks, `*` and `.` at both ends, and each kept id that isn't printable ASCII of at most 80 characters becomes `BAD_DEP`.
    3. **Combine.** The result is the union of all values in order, with duplicates removed. Every id is kept, so an unknown one blocks.
- **`work_source`:**
  - `work_source.noun(cfg: Config) -> str`: `"capability map"` for `capability_map`, `"spec files"` for `specs`, and `"capability map"` when there is no work source. That is today's wording.
  - `work_source.directory(cfg, repo) -> Path | None`: `repo / project.docs.specs` for `specs`. It returns `None` when the path is absolute, resolves outside `repo`, is a symlink, or isn't a directory, and always `None` for other kinds.
  - `work_source.file(cfg, repo) -> Path | None`: unchanged for `capability_map`, `None` for `specs`.
  - `work_source.items(cfg, repo) -> list[sources.ModuleNode]`: for `specs`, sorted by id. Headers are read only for files whose `(name, st_mtime_ns, st_size)` changed since the last call, using a module-level cache keyed by directory.
  - `work_source.skipped(cfg, repo) -> list[str]`: `[]` for other kinds. For `specs`, the file names that fit `spec_file` but are not modules:
    - an id failing `is_module_id`
    - a symlink, or not a regular file
    - a file that raised `OSError` when read, listed as `"<name> (unreadable)"`
    - the entries past `SPECS_MAX`, listed once as `"… and <n> more (over 500 files)"`

    The list is sorted, and its consumer is Task 2's Backlog line.
- **`agentflow queue`:** on a `specs` pack without `--include-specs`, it exits 1 with `this pack's work source is its spec files: <n> modules are listed; pass --include-specs to queue them`, where `<n>` is `len(work_source.items(cfg, repo))`. The guard sits in `cmd_queue` only; `run_queue` and `queue_tick` are internal.

- [ ] **Step 1: Write the failing tests.**

  In `test_work_source.py`:
  - The fixture `specs` is `fr.configure({"work_source": {"kind": "specs", "path": None}})`. The `None` deletes the python pack's map path. Spec files go in `docs/specs/SPEC-<id>.md`.
  - The fixture `plain` is the same plus `{"project": {"docs": {"spec_file": None}}}`, which gives the default `{module}.md`.

```python
def test_a_specs_source_lists_one_module_per_spec_file_with_header_deps(specs):
    write(specs.root, "docs/specs/SPEC-b.md", "# B\n\n**Depends on:** `a`\n\n## Scope\n**Depends on:** z\n")
    write(specs.root, "docs/specs/SPEC-a.md", "# A\n\nDepends on: None (standalone).\n")
    write(specs.root, "docs/specs/notes.md", "# not a spec\n")
    assert work_source.items(specs.cfg, specs.root) == [sources.ModuleNode("a", []), sources.ModuleNode("b", ["a"])]

@pytest.mark.parametrize("header, deps", [
    ("**Depends on**: a, b", ["a", "b"]),
    ("- Depends on: `a`.", ["a"]),
    ("Depends on the auth module for tokens.", []),
    ("**Depends on:** ghost, Auth", ["ghost", "Auth"]),
    ("**Depends on:**\n- a\n- b\n\nmore", ["a", "b"]),
    ("Depends on: a,\n  b", ["a", "b"]),
    ("Depends on: a\nDepends on: c", ["a", "c"]),
    ("Depends on: none-pay", ["none-pay"]),
    ("Depends on: none, a", ["none", "a"]),
    ("Depends on: a" + "b" * 600, [sources.BAD_DEP, sources.CUT_DEP]),   # an id over 80 chars, on a cut line
    ("Depends on: \x1b[31mred", [sources.BAD_DEP]),
])
def test_dependency_line_forms(header, deps):
    assert sources.spec_deps(f"# X\n\n{header}\n") == deps

def test_files_that_are_not_modules_are_skipped_only_under_the_bare_pattern(plain, specs): ...
    # plain: docs/specs holds ok.md, README.md, index.md, template.md, "Foo Bar.md", a symlink ln.md, a directory dir.md
    #   → item ids == ["ok"]; skipped() == the other six, sorted; no validate issue on work_source
    # specs: SPEC-index.md → "index" is a module

def test_an_unreadable_spec_is_skipped_not_raised(plain, monkeypatch): ...
    # monkeypatch sources.read_header to raise PermissionError for one file → that file is in skipped() as "<name> (unreadable)"

def test_headers_are_reread_only_when_a_file_changes(plain, monkeypatch): ...
    # count read_header calls: two items() calls → one read per file; touch one file → one more read

def test_a_specs_pack_with_a_path_a_bad_docs_dir_or_no_module_lane_is_reported(specs, tmp_path): ...
    # work_source.path "docs/specs" → error on work_source.path ("specs reads project.docs.specs");
    # project.docs.specs a symlink to tmp_path → error on project.docs.specs; and pointing at a file → error on project.docs.specs;
    # dashboard.backlog.module_lane removed → warning on dashboard.backlog.module_lane

def test_capability_map_wording_is_unchanged_and_specs_wording_names_spec_files(fr, specs): ...
    # work_source.noun(fr.cfg) == "capability map"; work_source.noun(specs.cfg) == "spec files"

def test_add_item_is_a_no_op_and_capability_map_placeholder_is_empty_for_specs(specs): ...
    # add_item leaves docs/specs unchanged; make_ctx(...).values["capability_map"] == ""; file() is None

def test_a_new_module_flag_is_refused_for_a_specs_pack(specs): ...
    # pipeline._check_new_module(cfg, {"module": "e"}, {"module": "e", ...}) raises ValueError matching "--task"
```

  More tests:
  - **`test_work_source.py`:**
    - Change `test_a_work_source_of_another_kind_is_an_error` (:260) to expect `"the work source kinds are capability_map and specs"`. This message isn't one a `capability_map` pack produces.
    - `{kind: specs}` validates clean.
    - `dashboard.backlog.new_task.feature: [new_module, after]` with **no** `module_row` validates clean for a `specs` pack. It is still an error for a `capability_map` pack and for a pack with no work source.
  - **`test_backlog.py`:**
    - Add an `sbk` fixture: `bk`'s `Board` over `flow_repo.configure({"work_source": {"kind": "specs", "path": None}})`, with `docs/specs/SPEC-a.md` committed and `a` merged through `Board.merge_module`, then one `refresh()`.
    - In it, `SPEC-b.md` with `**Depends on:** ghost` gives the card `module:b` the status `"blocked"`, and its `blocked_by` text names `ghost` and says "spec files".
  - **`test_next_queue.py`:** add a `specs` variant of `qrepo` (two spec files). Monkeypatch `module_queue.spawn_feature_run` as the existing CLI case at :455 does.
    - `main(["queue", "--once"])` exits 1 and prints `2 modules are listed`.
    - With `--include-specs`, it calls the patched starter.
  - **`test_config_edit.py`:** `propose(path, base, text=<the pack with work_source.kind: specs>)` raises `Refused` matching `"protected"`. This uses the YAML-text path, as at config_edit.py:481; the Form path refuses keys outside its index with other words.

- [ ] **Step 2: Run them and check they fail.** Run `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_work_source.py agentflow/tests/test_backlog.py agentflow/tests/test_config_edit.py agentflow/tests/test_next_queue.py --import-mode=prepend -q`. Expected: the new and changed tests FAIL.

- [ ] **Step 3: Implement.**
  - **Config:**
    - `_work_source` requires `path` only for `capability_map`.
    - For `specs`, the validator rules are:
      - A non-empty `path` is an error on `work_source.path`: "specs reads project.docs.specs; remove path".
      - `project.docs.specs` must be relative, without `..`, not a symlink, and a directory if it exists. A violation is an error on `project.docs.specs`.
      - A missing `dashboard.backlog.module_lane` is a warning on that key: "a specs work source lists modules only for the module_lane".
    - `Validator.backlog` keeps requiring `module_row` for `new_module`, **unless** `work_source.kind == "specs"`.
  - **Listing:** `items()` for `specs` lists up to `SPECS_MAX` entries through `spec_ids`. It leaves out what `skipped` names, and reads headers through the stamp cache, catching `OSError` per file.
  - **Wording:** every site under Files builds its "capability map" phrase from `work_source.noun(cfg)`. "a capability-map module lives in the map; it cannot be removed here" becomes `f"a module from the {noun} lives there; it cannot be removed here"` only when the noun isn't "capability map". Otherwise the text is unchanged.
  - **Callers:**
    - `{capability_map}` is a path only for `capability_map`.
    - `_picture`'s `QueueState` fallback runs only for `capability_map`.
    - `_check_new_module` raises `"with work_source specs a new module has no row: run --module <id> --task <text>"`.
    - `add_item` returns without writing for `specs`.
    - `cmd_queue` adds `--include-specs`.

- [ ] **Step 4: Run the same command; expected PASS.** Then run `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_new_module.py agentflow/tests/test_conditions.py agentflow/tests/test_dashboard.py agentflow/tests/test_explain_golden.py agentflow/tests/test_portability.py --import-mode=prepend -q`. Expected: PASS, with **no** edits to existing tests other than :260 above.

- [ ] **Step 5: Update the playbook, then commit.**
  - **§14 `work_source`:** two kinds. For `specs`:
    - it takes no `path`; the directory is `project.docs.specs`
    - modules come from `project.docs.spec_file`
    - dependencies come from header `Depends on:` lines and their bullets, and `none` means none
    - under `{module}.md`, `index`, `readme`, `template` and `changelog` are not modules
    - non-slug names, symlinks, directories and unreadable files are skipped and shown on the Backlog
  - **§10:** `queue --include-specs`.

```bash
git add agentflow/config.py agentflow/work_source.py agentflow/sources.py agentflow/module_queue.py agentflow/conditions.py agentflow/backlog.py agentflow/dashboard.py agentflow/pipeline.py agentflow/flow.py agentflow/tests/ docs/agentflow-playbook.md
git commit -m "Add a specs work source that lists one module per spec file, with dependencies from its header"
```

---

### Task 2: The Backlog starts spec modules and new feature cards, and names the skipped files

**Files:**
- Modify: `agentflow/backlog.py`: `begin_start` (:477) and `snapshot` (:400)
- Modify: `agentflow/views.py`: `board` (:254) gains `backlog_skipped` and `work_source_kind`
- Modify: `agentflow/dashboard.html`: one muted line in the Backlog column, and the "Only modules already in the map." hint (:506) taken from `work_source_kind`
- Modify: `agentflow/prompts/spec.md`
- Test: `agentflow/tests/test_backlog.py`, `agentflow/tests/test_dashboard.py`, `agentflow/tests/test_pipeline.py`

**Interfaces:**
- **Consumes:** Task 1's `specs` listing, `work_source.skipped` and `noun`, and the `spec_deps` header syntax.
- **Produces:**
  - **The feature card's start:** for a `new_module` card in a `specs` pack, it calls `spawn(repo, lane, {"module": id, "task": text}, new_module=None, ...)`.
    - `text` is the responsibility, a blank line, then `Depends on: a, b`, or `Depends on: none`.
    - The start is refused with `f"a spec for {id} now exists; start module:{id} instead"` when `id` is among `work_source.items` at that moment.
    - Until the run starts, the card's deps still block it through `Item.deps`.
  - **New payload keys:**
    - `backlog.snapshot(...)["skipped"]: list[str]`
    - `views.board(...)["backlog_skipped"]: list[str]`
    - `views.board(...)["work_source_kind"]: str | None`

- [ ] **Step 1: Write the failing tests** in `test_backlog.py`, using Task 1's `sbk` fixture.

```python
def test_a_spec_module_card_starts_a_feature_run_for_that_module(sbk):
    write(sbk.root, "docs/specs/SPEC-c.md", "# C\n")
    sbk.refresh()
    assert "module:a" not in sbk.cards() and sbk.cards()["module:c"]["status"] in ("ready", "parallel")
    sbk.start("module:c")
    assert sbk.spawn.calls[-1]["params"] == {"module": "c"} and sbk.spawn.calls[-1]["lane"] == "feature"

def test_a_new_feature_card_carries_its_text_and_dependencies_as_the_task(sbk):
    item = backlog.add(sbk.cfg, sbk.root, "feature",
                       {"new_module": {"module": "e", "responsibility": "Export  reports", "deps": ["a"]}})
    sbk.start(f"card:{item.id}")
    call = sbk.spawn.calls[-1]
    assert call["params"] == {"module": "e", "task": "Export reports\n\nDepends on: a"} and call["new_module"] is None

def test_a_new_feature_card_cannot_reuse_an_existing_spec_module_id(sbk): ...
    # backlog.add(... module "a" ...) raises backlog.Invalid matching "already a module"

def test_a_new_feature_card_whose_spec_appeared_since_is_refused_at_start(sbk): ...
    # add card for "e"; then write docs/specs/SPEC-e.md; begin_start(card) raises Refused matching "start module:e instead"

def test_the_backlog_names_the_files_that_are_not_modules(sbk): ...
    # "SPEC-Foo Bar.md" in docs/specs → snapshot["skipped"] == ["SPEC-Foo Bar.md"]
```

  In other test files:
  - **`test_dashboard.py`:** `GET /api/board` carries `backlog_skipped` and `work_source_kind`. On the default python pack these are `[]` and `"capability_map"`.
  - **`test_pipeline.py`:** next to the existing `main(["run", "feature", "--module", "demo", "--dry-run", …])` case at :512, add `main(["run", "feature", "--module", "e", "--task", "t", "--dry-run", "--config", …]) == 0`. It already passes. It is a regression pin for the path the card now uses.
  - **`test_backlog.py`:** a plain string check that `agentflow/prompts/spec.md` contains both `{run_dir}/task.md` and `**Depends on:**`.

- [ ] **Step 2: Run them; they should fail.** Run `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_backlog.py agentflow/tests/test_dashboard.py agentflow/tests/test_pipeline.py --import-mode=prepend -q`. Expected: the new `test_backlog.py` and `test_dashboard.py` tests FAIL, and the `test_pipeline.py` pin passes.

- [ ] **Step 3: Implement.**
  - **`begin_start`, card branch:** builds the params and text above, refuses the clash, and sets `new_module = None`.
  - **`snapshot` and `board`:** carry the new keys.
  - **`dashboard.html`:**
    - When `backlog_skipped` isn't empty, show one muted line under the Backlog cards: "Not modules: …".
    - The new-module form's hint says "Only modules already in the spec files." when `work_source_kind == "specs"`, and is unchanged otherwise.
    - All text goes through the escaping helper.
  - **`agentflow/prompts/spec.md`:** add this after the "Read, by path" list:

    > If `{run_dir}/task.md` exists, it is the developer's description of this module: build the spec on it. Put its `Depends on:` line in the spec's header, above the first `## ` heading, as `**Depends on:** <ids>` (or `**Depends on:** none`).

- [ ] **Step 4: Run them; expected PASS.** Then run `test_new_module.py` and `test_portability.py`. Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agentflow/backlog.py agentflow/views.py agentflow/dashboard.html agentflow/prompts/spec.md agentflow/tests/
git commit -m "Start spec modules and new feature cards from the Backlog, and name the files that are not modules"
```

---

### Task 3: `init` writes a `specs` work source when it found a specs directory

Plan and review this task with `security-and-hardening`, because `init.py` is an overlay path. The task only adds fixed data to the rendered pack, gated on a key that `detect` already returns. It reads nothing new from the repository and doesn't change `detect.py`.

**Files:**
- Modify: `agentflow/init.py`: `pack_data` (:153) and `render_pack` (:180)
- Modify: `docs/agentflow-playbook.md` §13, which describes what `init` writes
- Test: `agentflow/tests/test_init.py`, `agentflow/tests/test_plug_and_play.py`

**Interfaces:**
- **Consumes:** Task 1's `specs` kind and its no-`module_row` rule.
- **Produces:** when `"specs" in d.docs` (detect found top-level `specs/`, `docs/rfcs` or `docs/proposals`; see `detect._DOC_DIRS`), the pack gains this block:

```yaml
# the Backlog: one module per spec file in project.docs.specs; a new feature card writes a new spec
work_source: {kind: specs}
dashboard:
  backlog:
    module_lane: feature
    new_task: {feature: [new_module, after], bug: [issue, task, after]}
```

  Otherwise the pack has neither key.

- [ ] **Step 1: Write the failing tests.**
  - **`test_init.py`:**
    - A `Detected` with `docs={"specs": "docs/proposals", ...}`: `pack_data(...)["work_source"] == {"kind": "specs"}`, `["dashboard"]["backlog"]["module_lane"] == "feature"`, and the rendered text has the comment line.
    - A `Detected` without `specs`: neither key is present.
  - **`test_plug_and_play.py`:**
    - The existing loop keeps passing unchanged. It doesn't exercise the new code.
    - Add a fourth case: the `docs` fixture repository with `docs/proposals/greet.md`, `docs/proposals/index.md` and `docs/proposals/README.md` committed. After `init`:
      - the pack has `work_source`
      - `validate` reports exactly the `gates_confirmed` warning (proposal §1c, deviation 4)
      - `backlog.snapshot` lists only `greet` and skips `README.md` and `index.md`
      - `run feature --module greet --dry-run` exits 0

- [ ] **Step 2: Run them and check they fail.** Run `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_init.py agentflow/tests/test_plug_and_play.py --import-mode=prepend -q`. Expected: the new tests FAIL.

- [ ] **Step 3: Implement.**
  - **`pack_data`:** add the gated keys.
  - **`render_pack`:** add one more block, after `project`.
  - **Tests:** update any assertion in `test_init.py` that pins the whole rendered text.

- [ ] **Step 4: Run `test_init.py` and `test_plug_and_play.py`. Expected: PASS.**

- [ ] **Step 5: Commit**

```bash
git add agentflow/init.py agentflow/tests/test_init.py agentflow/tests/test_plug_and_play.py docs/agentflow-playbook.md
git commit -m "Have init write a specs work source and a feature-starting Backlog when it finds a specs directory"
```

---

### Task 4: This repository's pack, and the as-built record (interactive session only)

`.agentflow/**` is always forbidden in a run, so a person does this task in an interactive session, never through an agentflow run.

**Files:**
- Modify: `.agentflow/config.yaml`: `work_source`, `dashboard.backlog`
- Modify: `.agentflow/prompts/spec.md`, the pack's copy of the engine prompt that Task 2 changed
- Modify: `docs/agentflow-harness-proposal.md`: a new §1d "Team backlog" listing the as-built deviations, and a §5 line for slice TB-A1

- [ ] **Step 1: Edit the pack.**
  - Add `work_source: {kind: specs}`.
  - Under `dashboard.backlog`, add `module_lane: feature`.
  - Under `new_task:`, above the `bug:` line, add the line `feature: [new_module, after]   # a feature card writes docs/agentflow-<id>-proposal.md`.
  - Remove the "no work source …" comment from the `bug:` line (:331).
  - Add Task 2's two sentences to `.agentflow/prompts/spec.md`, after its "Read, by path" list.

- [ ] **Step 2: Validate.** Run `bin/agentflow validate`. Expected: `0 warnings`. Then run `bin/agentflow explain feature --module harness`. Expected: it prints without an error.

- [ ] **Step 3: Record the as-built deviations in proposal §1d and §5.**
  1. **The spec header's `Depends on:` line.** It can be bold or plain, with a colon, and its bullets or comma continuation count. An unknown, unreadable or cut dependency blocks the module.
  2. **The task of a new feature card.** It carries the card's dependencies as a `Depends on:` line, and the spec prompt gains two sentences, not one. A new spec's dependencies are kept only when the define agent writes them into the header. Before that, the card blocks on them, and the spec approval checkpoint shows them to the person who approves.
  3. **`--new-module` in a `specs` pack.** `_check_new_module` refuses it, pointing to `--module --task`. `add_item` is a no-op for `specs`.
  4. **When `init` writes the `specs` source.** It writes it only when `detect` finds top-level `specs/`, `docs/rfcs` or `docs/proposals`. A repository with only `docs/`, or with `docs/specs/`, gets no `specs` source. For `docs/specs/`, detect prints a note, and the source is a manual addition.
  5. **Files that are not modules.** Non-slug ids, symlinks, directories and unreadable files are skipped. Under the bare `{module}.md` pattern, `index`, `readme`, `template` and `changelog` are skipped too.
  6. **`work_source.path`.** A `specs` source takes none: it always reads `project.docs.specs`.
  7. **`agentflow queue` on a `specs` pack.** It needs `--include-specs`. Each run still runs its `define` and `plan` agent steps before it stops for approval.
  8. **Modules finished before agentflow.** Until slice TB-A2, such a module shows as startable unless its plan is finished and its branch is merged, which is today's rule.
  9. **No engine version bump.** An older 2.x engine refuses `kind: specs` loudly in `validate`.
  10. **No shipped-slice proposals to mark.** Spec A4's "mark the shipped slices' proposals done" has nothing to do here: this repository has one proposal, and it is still in progress.

- [ ] **Step 4: Run the full suite and the checks.**
  - Run the full suite: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests --import-mode=prepend -q`. Expected: all pass.
  - Check the golden files: `git diff --stat agentflow/tests/fixtures/explain/`. Expected: empty.
  - Check by hand:
    - `bin/agentflow dashboard` shows `harness` as a Backlog card.
    - A new feature card's Start writes `run feature --module=<id> --task=…` to the state dir's `runs/queue.log`. Cancel that run straight away with `agentflow cancel <run>`.

- [ ] **Step 5: Commit**

```bash
git add .agentflow/config.yaml .agentflow/prompts/spec.md docs/agentflow-harness-proposal.md
git commit -m "Turn on the specs work source for this repository and record slice TB-A1 as built"
```
