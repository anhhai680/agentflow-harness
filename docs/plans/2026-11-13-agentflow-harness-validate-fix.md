# `agentflow validate --fix` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `agentflow validate` warns about pack prompt overrides that hold text the engine retired, or that are identical to the engine's file, and `agentflow validate --fix [--dry-run]` repairs the ones with one right answer.

**Architecture:** A new stdlib module `agentflow/pack_fix.py` holds the retired-text register and one pure-ish pass, `findings(config_path)`, that reads the pack's own prompts and returns the issues, the fixes and the skipped files. `_Validator` adds those issues, and `Issue` gains a `fixable` flag. `cmd_validate` prints `(fixable)` and a hint. With `--fix` it runs the safety checks, prints a diff for each fix and applies it through `safeio`, then validates again.

**Tech Stack:** Python 3.12 stdlib (`difflib`, `dataclasses`, `os`), `safeio`, pytest.

**Spec:** `docs/specs/agentflow-validate-fix-proposal.md` (approved 2026-11-13). Plan and review it with `security-and-hardening`, and dispatch the `security-auditor` agent at review: it deletes and rewrites files in the repository.

## Global Constraints

- Engine dependencies stay stdlib + `pyyaml>=6`.
- `test_portability.DENYLIST` applies to `pack_fix.py`. In particular, no `docs/specs` path in its docstrings or comments.
- `--fix` writes only regular files directly under `<main checkout>/.agentflow/prompts/`, through `safeio`, and never commits, stages or pushes.
- Retired text is always a warning, never an error. No new config key.
- The `explain` goldens (`agentflow/tests/fixtures/explain/*.txt`) stay byte-identical, `test_python_pack_validates_clean` stays green, and `bin/agentflow validate` on this repository prints `0 warnings`.
- Every commit message is one imperative sentence ending with the `Co-Authored-By` trailer.

## Review Focus

1. **A 2.4 `_preamble.md` copy.** Its effort-check block also contains `effort_change`. Expect exactly one issue, which is fixable, and no extra "edited" warning from the status entry's token (Task 1, `test_old_preamble_block_is_one_fixable_issue`).
2. **A verbatim 2.4 `_result.md` copy.** With the retired line replaced, it equals the engine's file. Expect `--fix` to delete it, not rewrite it into a redundant override (Task 1, `test_fix_that_lands_on_the_engine_text_deletes`).
3. **A CRLF prompt, or retired text pasted twice.** Expect a match, every occurrence replaced, and the CRLF endings kept (Task 1, `test_crlf_and_repeated_text`).
4. **A prompt that is not UTF-8, is over 1 MB, or is a symlink.** Expect `validate` not to crash, no fix, and `--fix` to name it as skipped (Task 1, `test_unreadable_prompts_are_skipped`).
5. **A file edited between the check and the write, or a symlinked `.agentflow/prompts`.** Expect that file skipped and named, and a refusal before anything is written (Task 2, `test_changed_since_read_is_skipped` and `test_symlinked_prompts_dir_is_refused`).

---

### Task 1: The register, the drift check and the `(fixable)` warnings

**Files:**
- Create: `agentflow/pack_fix.py`
- Modify: `agentflow/config.py` (`Issue` at ~186; `_Validator`: new `prompt_drift()`, called from `validate()` right after `v.templates()` at ~1982)
- Modify: `agentflow/flow.py` (`cmd_validate`, ~55-77)
- Test: `agentflow/tests/test_pack_fix.py`

**Interfaces:**
- Produces, in `agentflow/pack_fix.py`:
  - `@dataclass(frozen=True) class Retired: id: str; file: str; retired_in: str; detect: str; old: str; new: str`. `file` is the engine prompt the text came from (`"_result.md"`).
  - `RETIRED: tuple[Retired, ...]`
  - `MAX_PROMPT_BYTES = 1_000_000`
  - `@dataclass(frozen=True) class Fix: path: Path; before: bytes; after: bytes | None` (`None` deletes the override)
  - `@dataclass(frozen=True) class Skipped: path: Path; reason: str`
  - `@dataclass class Findings: issues: list[Issue]; fixes: list[Fix]; skipped: list[Skipped]`
  - `def findings(config_path: Path) -> Findings`
- Produces, in `agentflow/config.py`: `Issue.fixable: bool = False`, a new last field with a default, so every existing positional `Issue(...)` call still works.

**The register (exact values):**
- `effort-check-block`: `file="_preamble.md"`, `retired_in="2.5"`, `detect="<!-- effort-check -->"`, `new=""`. `old` is lines 3–9 of `git show e911766:agentflow/prompts/_preamble.md` (from `<!-- effort-check -->` to `<!-- /effort-check -->`), each ending in `\n`, plus one more `"\n"` for the blank line after the block.
- `effort-change-status`: `file="_result.md"`, `retired_in="2.5"`, `detect="effort_change"`. `old` is line 9 of `git show e911766:agentflow/prompts/_result.md`, and `new` is line 9 of the current `agentflow/prompts/_result.md`, both without the trailing newline.

**The pass (`findings`):** for each `*.md` directly under `config_path.parent / "prompts"`, in sorted order:
1. Read it with `safeio.read_bytes(path, MAX_PROMPT_BYTES)`. On `SymlinkRefused`, `ValueError` or `UnicodeDecodeError`, add a `Skipped` (`"a symlink"`, `"not a regular file or larger than 1 MB"`, `"not UTF-8"`) and add no issue.
2. If the text contains `"\r\n"`, match on `old`/`new` with `"\n"` turned into `"\r\n"`.
3. For each entry whose `old` is in the text, replace every occurrence and add a fixable warning.
4. Then, for each entry whose `detect` is still in the replaced text, add a non-fixable warning.
5. If the replaced text equals the engine's file of the same name (`pack.ENGINE_PROMPTS / name`), the fix is a delete (`after=None`). An unchanged file equal to the engine's gets the identical-override warning and a delete. Otherwise a changed file gets `after=<replaced bytes>`.

**Issue copy** (the path is `pack.prompt_label(config_path, path)`):
- Fixable retired: `holds text the engine retired in {retired_in} ({id}); validate --fix replaces it with the engine's current text`. When `new == ""`, end with `validate --fix removes it`.
- Edited retired: `mentions {detect!r}, which the engine retired in {retired_in} ({id}), in text that was edited, so validate --fix leaves it: compare it with engine:prompts/{file}`.
- Identical: `is identical to engine:prompts/{name}: it changes nothing and hides later engine fixes to that file; validate --fix deletes it`.

- [ ] **Step 1: Write the failing tests** in `agentflow/tests/test_pack_fix.py`. They use `pack_repo_python`, write prompts into its `.agentflow/prompts/`, and call `findings(pack.pack_config(repo))` and `validate(load_config(...), repo)`. Use these helpers: `OLD_PREAMBLE = engine("_preamble.md").replace("## Rules for this step", BLOCK.old + "## Rules for this step", 1)` and `OLD_RESULT = engine("_result.md").replace(STATUS.new, STATUS.old)`.
  - `test_register_matches_the_engine_prompts`: for every entry, `old` and `detect` are in no file under `pack.ENGINE_PROMPTS`, and a non-empty `new` is in `engine(entry.file)`.
  - `test_old_result_line_is_fixable`: a `_result.md` holding `OLD_RESULT` plus an extra line `"\nPack wording.\n"` gives exactly one issue: `level == "warning"`, `fixable`, path `prompts/_result.md`, and `"2.5"` in the message. Its fix's `after` decodes to `engine("_result.md") + "\nPack wording.\n"`.
  - `test_old_preamble_block_is_one_fixable_issue`: `OLD_PREAMBLE` plus `"\nPack rule.\n"` gives exactly one issue, which is fixable. The fix's `after` has no `"effort"` in it.
  - `test_fix_that_lands_on_the_engine_text_deletes`: `_result.md` = `OLD_RESULT` exactly gives one fixable issue and `fixes[0].after is None`.
  - `test_edited_retired_text_is_not_fixable`: `engine("_result.md")` plus `"\n- also `effort_change` when unsure\n"` gives one issue with `fixable` False, `"effort_change"` in the message, and no fixes.
  - `test_identical_override_is_fixable`: a `task.md` equal to `engine("task.md")` gives one fixable issue whose message has `"identical"`, plus a delete fix.
  - `test_crlf_and_repeated_text`: `_result.md` = `(OLD_RESULT + "\n" + STATUS.old + "\n").replace("\n", "\r\n")` gives a fix whose `after` has no `b"effort_change"`, has `b"\r\n"`, and has no bare `b"\n"` that isn't part of `b"\r\n"`.
  - `test_unreadable_prompts_are_skipped`: a symlink `x.md` pointing to the engine's `task.md`, a 2 MB `big.md`, and a `bad.md` holding `b"\xff\xfe"` give three `Skipped` entries, no issues, no fixes, and `validate` doesn't raise.
  - `test_python_pack_has_no_drift`: `findings` on the unchanged fixture is empty in all three lists.
  - `test_cli_marks_fixable_and_hints`: with `OLD_RESULT` written, `flow.main(["validate"]) == 0`, and the output has the line `warning prompts/_result.md: ... (fixable)` and ends with the line `1 fixable: run agentflow validate --fix`.

- [ ] **Step 2: Run the tests and see them fail**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_pack_fix.py --import-mode=prepend -q`
Expected: collection error, `ModuleNotFoundError: agentflow.pack_fix`.

- [ ] **Step 3: Implement `pack_fix.py` as above.** Add `Issue.fixable`. Add `_Validator.prompt_drift()`, which imports `pack_fix` inside the method (`pack_fix` imports `Issue` from `config`) and calls `self.add` with the fixable flag kept. Since `add` takes no flag, append the `Issue` directly while keeping its dedup check. In `cmd_validate`, print ` (fixable)` after a fixable issue's message. After the `ok:` line, or after the issues when there are errors, print `{n} fixable: run agentflow validate --fix` when `n > 0`. The `ok: ... ({len(issues)} warnings)` line itself is unchanged.

- [ ] **Step 4: Run the tests and see them pass**

Run: the Step 2 command, then `... pytest agentflow/tests/test_pack.py agentflow/tests/test_config.py agentflow/tests/test_portability.py --import-mode=prepend -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add agentflow/pack_fix.py agentflow/config.py agentflow/flow.py agentflow/tests/test_pack_fix.py
git commit -m "Warn about pack prompts that hold retired engine text or copy an engine prompt unchanged.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: `validate --fix [--dry-run]`, its safety checks and its docs

**Files:**
- Modify: `agentflow/pack_fix.py`
- Modify: `agentflow/flow.py` (`cmd_validate`; the `validate` parser at ~1006)
- Modify: `CHANGELOG.md`, `docs/agentflow-playbook.md` (§10 at ~328, §13 at ~395), `CLAUDE.md` (Commands block, and the security-overlay invariant's file list)
- Modify: `.agentflow/config.yaml` (`overlays.security.when_paths` at ~107: add `agentflow/pack_fix.py`)
- Test: `agentflow/tests/test_pack_fix.py`

**Interfaces:**
- Consumes: `findings`, `Fix`, `Skipped` (Task 1).
- Produces, in `agentflow/pack_fix.py`:
  - `class FixRefused(Exception)`
  - `def check_pack(repo: Path) -> None`: raises `FixRefused` when `os.environ` has `AGENTFLOW_RUN` (`"validate --fix never runs inside an agentflow run"`), or when `repo / ".agentflow"` or `repo / ".agentflow" / "prompts"` is a symlink (`"<path> is a symlink"`).
  - `def render(fix: Fix, repo: Path) -> str`: a `difflib.unified_diff` with `a/<rel>` and `b/<rel>` headers, where `<rel>` is relative to `repo`, or `delete <rel>` for `after is None`.
  - `def apply(fixes: list[Fix]) -> tuple[list[Fix], list[Skipped]]`: for each fix, re-read the file with `safeio.read_bytes(path, MAX_PROMPT_BYTES)`. If it differs from `before`, or the read raises, skip it with `"changed since it was read"`. Otherwise write `after` with `safeio.write_text`, or remove the file with `safeio.remove` when `after is None`. Before the operation, assert that `path.parent` is the resolved `.agentflow/prompts` and that `path.name` matches `^[A-Za-z0-9_.-]+\.md$`, and skip the file with `"outside .agentflow/prompts"` if not.
- CLI: `validate --fix` and `validate --fix --dry-run`. `--dry-run` without `--fix` is `parser.error("--dry-run needs --fix")` (exit 2). `--fix` with `--config` prints `error validate --fix works only on the repository's own pack` and returns 1.

- [ ] **Step 1: Write the failing tests** (CLI tests use `monkeypatch.chdir(repo)`, `flow.main`, `capsys`):
  - `test_dry_run_prints_the_diff_and_writes_nothing`: with `OLD_RESULT` + `"\nPack wording.\n"`, `main(["validate", "--fix", "--dry-run"]) == 0`. The output has `--- a/.agentflow/prompts/_result.md`, a `-` line with `effort_change`, and `nothing written`. The file's bytes are unchanged.
  - `test_fix_rewrites_then_validates_clean`: the same setup with `main(["validate", "--fix"]) == 0`. The file now equals `engine("_result.md") + "\nPack wording.\n"`, the output ends with the second validation's `ok: config.yaml is valid (0 warnings)`, and `git status --porcelain` lists only that file, with nothing staged.
  - `test_fix_deletes_identical_and_landed_overrides`: `task.md` identical to the engine's and `_result.md` = `OLD_RESULT` are both removed. The output has `delete .agentflow/prompts/task.md`.
  - `test_fix_leaves_edited_text`: the edited `effort_change` line from Task 1 stays byte-identical, the exit code is 0 and the warning is still printed.
  - `test_changed_since_read_is_skipped`: call `findings`, then rewrite the file, then `apply`. The file is skipped with `"changed since it was read"` and keeps its new bytes.
  - `test_symlinked_prompts_dir_is_refused`: `.agentflow/prompts` replaced with a symlink to a tmp dir holding `OLD_RESULT` gives `main(["validate", "--fix"]) == 1`, `is a symlink` in the output, and the tmp file unchanged.
  - `test_refused_inside_a_run`: `monkeypatch.setenv("AGENTFLOW_RUN", "r1")` gives exit 1, `never runs inside an agentflow run`, and the file unchanged.
  - `test_fix_with_config_is_refused` and `test_dry_run_without_fix_is_a_usage_error` (`pytest.raises(SystemExit)` with code 2).
  - `test_fix_names_skipped_files`: with the symlinked `x.md` from Task 1, `--fix` prints `skipped .agentflow/prompts/x.md: a symlink`.

- [ ] **Step 2: Run the tests and see them fail**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_pack_fix.py --import-mode=prepend -q`
Expected: the new tests fail (unknown `--fix`), and the Task 1 tests still pass.

- [ ] **Step 3: Implement it.** Split `cmd_validate`'s printing into a helper, `_print_validation(cfg, repo, config_path) -> int`, that prints the issues and the summary lines and returns the exit code, so `--fix` can run it twice.

  With `--fix`:
  1. Refuse `--config`, then call `check_pack(repo)`. A `FixRefused` prints `error {exc}` and returns 1.
  2. Run `findings`, then print `render` for each fix and `skipped <rel>: <reason>` for each skipped file.
  3. With `--dry-run`: print `dry run: {n} files would change; nothing written` and return the first validation's code.
  4. Without it: call `apply`, print the files it skipped, print `fixed {n} files`, then print the second `_print_validation` and return its code.

- [ ] **Step 4: Update the docs.**
  - `CHANGELOG.md`, under `## [Unreleased]`, `### Added`:
    - `` `agentflow validate --fix [--dry-run]` repairs pack prompts that hold text the engine retired or that copy an engine prompt unchanged; it writes only `.agentflow/prompts/`, never commits, and refuses to run inside a run. ``
    - `` `validate` warns about those prompts, marks a warning it can repair `(fixable)` and ends with `N fixable: run agentflow validate --fix`. ``
  - Playbook §10: add a cheat-sheet line `agentflow validate --fix [--dry-run]   # repair what validate marks (fixable); never commits`.
  - Playbook §13, after the "Prompts:" paragraph, add: "After upgrading the engine, run `agentflow validate`: a prompt override that still holds text the engine retired, or that copies an engine prompt unchanged, is a warning, and `agentflow validate --fix --dry-run` shows what `--fix` would change."
  - `CLAUDE.md` Commands block: add `bin/agentflow validate --fix [--dry-run]  # repair the warnings validate marks (fixable)`.
  - Add `agentflow/pack_fix.py` to `overlays.security.when_paths` in `.agentflow/config.yaml`, and to the list in `CLAUDE.md`'s invariant "Changes to `guards.py`, … trigger the pack's security overlay". It deletes and rewrites files in the repository, so later changes to it get the security review too. Run `bin/agentflow validate` afterwards.

- [ ] **Step 5: Run the touched tests, then the full suite**

Run: `... pytest agentflow/tests/test_pack_fix.py agentflow/tests/test_pack.py agentflow/tests/test_config.py agentflow/tests/test_portability.py agentflow/tests/test_packaging.py --import-mode=prepend -q`, then the full suite, `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests --import-mode=prepend -q`, then `bin/agentflow validate`.
Expected: all pass, `git diff --exit-code agentflow/tests/fixtures/explain` is clean, and `validate` prints `0 warnings`.

- [ ] **Step 6: Commit**

```bash
git add agentflow/pack_fix.py agentflow/flow.py agentflow/tests/test_pack_fix.py CHANGELOG.md docs/agentflow-playbook.md CLAUDE.md .agentflow/config.yaml
git commit -m "Add validate --fix to repair retired and redundant pack prompts, with a dry run and refusals inside runs and symlinked packs.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```
