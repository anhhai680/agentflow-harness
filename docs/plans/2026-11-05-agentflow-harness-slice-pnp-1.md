# agentflow harness, slice PnP-1: an engine that runs a sparse pack Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make agentflow installable as a tool (`uv tool install`). A hand-written sparse pack (version, engine, a starter, `gates_confirmed`, a permission line) should then run any git repository on engine defaults, generic prompts and bundled skills. `init`, `detect` and `doctor` come in slice PnP-2 (`docs/plans/2026-11-06-agentflow-harness-slice-pnp-2.md`), which builds on this plan.

**Architecture:**
- **Config layers.** A new leaf module `layers.py` merges the layers (engine defaults → starter → project pack → user file) before `config.load_config` resolves `@` references. Every existing reader of `Config` stays unchanged.
- **What the engine ships:**
  - a default workflow (`agentflow/defaults/config.yaml`)
  - four starters (`agentflow/starters/<name>/config.yaml`)
  - seven generic skills (`agentflow/skills/agentflow-<stage>/SKILL.md`)
- **Prompts.** Engine prompts become generic: they name a step's skill through `{skill}` and the base through `{base_ref}`. This repository's wording and the fixture packs' wording move unchanged into each pack's `prompts/` overrides.
- **Skill text.** A step's skill text reaches its prompt through the existing `{brief}` slot. A step with no `brief:` of its own gets its skill's SKILL.md as its first brief. The skill is resolved in the main checkout first, then in the bundled skills.

**Tech Stack:** Python 3.12, stdlib + `pyyaml>=6` at runtime (`tomllib` is stdlib), `hatchling` as the build backend only, pytest.

**Spec:** `docs/agentflow-harness-proposal.md`, §1c "Plug and play" (its "Built-in skills and prompts" and "Layered config" parts) and §5 "Slice PnP". Read §1b's engine/pack table first: it says what may live in the engine.

## Global Constraints

- Runtime dependencies stay stdlib + `pyyaml>=6`. `hatchling` appears only in `[build-system]`.
- **The denylist.** Engine files never contain: `tivacare` (any case), `CONSTRAINTS.md`, `src/`, `docs/specs`, `docs/ideas`, `CAPABILITY-MAP`, `SPEC-`, `floor_guard`, `webadmin`, `next-step`.
  - From Task 2, "engine files" means `git ls-files --cached --others --exclude-standard agentflow` minus `agentflow/tests/`: tracked files plus new files that aren't ignored. The new YAML and SKILL.md files are therefore scanned before they are committed, while `.DS_Store`, `.pytest_cache/` and `__pycache__/` (all ignored) are not.
  - A file in that list that isn't UTF-8 fails the scan, naming the file.
- The golden files `agentflow/tests/fixtures/explain/*.txt` never change.
- **The four-part rule.** Every config key this slice adds, or whose own rule changes, ships in its task with all four of:
  - a `_PROTECTED_KEYS` entry or Settings Form field (`config_edit.py`)
  - its `validate` checks
  - an `agentflow/tests/test_config_edit.py` case
  - a line in `docs/agentflow-playbook.md` §14

  Keys that become optional only because the defaults layer now supplies them are covered by §14's "Layers" paragraph (Task 7), not one by one.
- Engine floor, unchanged: a pack whose effective config has no non-empty `gate_sets.verify` is refused, `.agentflow/**` is always forbidden, and user-only skills are refused. No layer can remove these.
- A pack is config, never code. A user config file holds personal keys only (Task 6's `USER_KEYS`), and never commands, gates, guardrails or the workflow.
- **Trust.** `config_trust.py` keeps deciding which config *file* a resumed run may load. That stays sound because the engine defaults and starters are engine code, and the user layer can't hold a command. Task 8 adds a digest of every layer file, so a change between start and resume is reported.
- **Where skill and brief text comes from.** It is always read from the **main checkout** (`ctx.repo`), never from a run's worktree. An agent writes the worktree, so a skill text read there would let one step write the next step's instructions. `guards.command_allowed` reads the main checkout for the same reason.
- **Execution.** Execute this plan interactively (subagent-driven-development), not through `agentflow run`. Tasks 2, 4 and 11 edit files under `.agentflow/`, which every run forbids.
- **Security overlay.** Tasks 3, 4, 6, 7 and 8 change skill resolution, how config loads, or `config_trust.py`, so review them with `security-and-hardening`.
- **Citations.** Line numbers cited below are approximate. Find the function by name.
- Test command: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests --import-mode=prepend -q`.
- Commit messages are one imperative sentence, ending with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

**Out of scope** (later work; name it in the PR body):
- everything in PnP-2: `init`, `detect`, `doctor`, the gate baseline, and the plug-and-play end-to-end test
- the multi-project dashboard and `status --all-projects`
- `agentflow hook install`, `migrate` and `config diff-defaults`
- Settings saving to the user layer
- autonomy levels and the conductor
- mapping a repository's own skills into `skills.map` automatically

**Deviations from the proposal** (record them in the PR body, and in proposal §1c when PnP-2 marks the slice done):
1. **The bundled skills** are define, plan, build, debug, verify, review and respond (taking review feedback). The proposal's `ship` is not bundled, because no agent step ships: `finish` is runner code.
2. **The engine defaults carry no `permission_mode`.** A pack states `agents.claude.permission_mode: bypassPermissions` itself, and PnP-2's `init` writes it with a comment. The team then reviews it in the pack's PR. Decided 2026-11-06.
3. **The bug lane on a repository with no Bash allow rule.** Its red/green check (`bug_eval.py`, through `guards.command_allowed`) needs a Bash allow rule that covers the test command, whatever `permission_mode` says. A sparse pack's repository usually has none. This slice documents that and warns in `validate` (Task 11); it doesn't change `guards.py`. Decided 2026-11-06.

## Review Focus

1. **A user config file that names a command or a guardrail,** or that sets `agents.<name>.models` for an agent no lower layer defines. It must be refused with the file and the key named, never merged. Test in Task 6.
2. **A skill or starter name that is a path** (`../../etc`, `a/b`, empty, not a string). It must not resolve outside the skills directories. A symlinked skills directory, which the test fixtures and many real repositories use, still works. Tests in Tasks 3 and 10.
3. **A skill text that contains braces** (`{module}`, a JSON example). It reaches the prompt verbatim, because it is a placeholder *value* in the one-pass fill (`lanes.build_prompt`). Test in Task 4.
4. **A full pack** (this repository's, and the two fixture packs) behaves exactly as before. Two things pin it:
   - the prompt overrides make each prompt it sends byte-identical (Task 2)
   - every leaf the pack file states keeps its value (Task 7)

---

### Task 1: Package the engine as an installable tool

**Depends on:** none

**Files:**
- Create: `pyproject.toml`
- Modify:
  - `CLAUDE.md` (Commands: add `uv tool install --reinstall --from . agentflow`; drop "There is no `pyproject.toml`: packaging belongs to slice PnP")
  - `docs/agentflow-playbook.md` §2–§3 (install with `uv tool install`, while `bin/agentflow` stays for working on the engine itself)
- Test: `agentflow/tests/test_packaging.py`

**Interfaces:**
- Produces: the console script `agentflow = "agentflow.flow:main"`. `test_packaging._engine_files()` lists the denylist's file set (see Global Constraints), so later tasks' data directories are picked up without changes.

- [ ] **Step 1: Write the failing tests**

```python
def test_pyproject_declares_the_console_script_and_only_pyyaml():
    meta = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert meta["project"]["scripts"] == {"agentflow": "agentflow.flow:main"}
    assert meta["project"]["dependencies"] == ["pyyaml>=6"]
    assert meta["project"]["requires-python"] == ">=3.12"

def test_pyproject_version_is_the_engine_version():
    assert tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"] == pack.ENGINE_VERSION

def test_wheel_holds_every_engine_file_and_no_tests(tmp_path):
    wheel = _build_wheel_or_skip(tmp_path)          # `uv build --wheel -o tmp_path`
    names = set(zipfile.ZipFile(wheel).namelist())
    assert set(_engine_files()) <= names
    assert not any(n.startswith("agentflow/tests/") for n in names)

def test_installed_tool_runs_outside_this_checkout(tmp_path):
    bin_dir = _tool_install_or_skip(tmp_path)        # UV_TOOL_DIR/UV_TOOL_BIN_DIR under tmp_path; `uv tool install --reinstall --from ROOT agentflow`
    proc = subprocess.run([str(bin_dir / "agentflow"), "--help"], cwd=tmp_path, capture_output=True, text=True)
    assert proc.returncode == 0 and "validate" in proc.stdout
```

How the two build tests skip:
- They skip only when `uv` is missing, or when the failed command's stderr contains a string from the module constant `OFFLINE_MARKERS`. Fill that constant from `uv`'s actual offline errors: run both commands once with the network off and paste what they print.
- Any other failure fails the test.
- The skip reason names the marker that matched.

- [ ] **Step 2: Run them to see them fail**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_packaging.py --import-mode=prepend -q`
Expected: FAIL, `pyproject.toml` not found.

- [ ] **Step 3: Write `pyproject.toml`**

- `[build-system]`: `requires = ["hatchling"]`, `build-backend = "hatchling.build"`.
- `[project]`: `name = "agentflow"`, `version = "2.0"`, `requires-python = ">=3.12"`, `dependencies = ["pyyaml>=6"]`, and `[project.scripts]` as tested.
- `[tool.hatch.build.targets.wheel]`: `packages = ["agentflow"]`, `exclude = ["agentflow/tests", "agentflow/runs", "agentflow/workspaces"]`.
- Add `[tool.uv] cache-keys` so that a changed file under `agentflow/` rebuilds the cached wheel of a local install. Check the exact syntax against uv's documentation, and say in the playbook that `--reinstall` is the fallback.
- `flow.main` returns an exit code on every path, so the console wrapper's `sys.exit(main())` is correct.
- `flow.py`'s `sys.path` insert of `parents[1]` is a no-op from site-packages.

- [ ] **Step 4: Run the packaging tests, then the full suite**

Expected: all pass, or the two build tests skip with an offline marker. Say in the PR body that `engine_tests` skips packaging when offline.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml agentflow/tests/test_packaging.py CLAUDE.md docs/agentflow-playbook.md
git commit -m "Package agentflow as an installable tool with an agentflow console script"
```

---

### Task 2: Generic engine prompts, with each pack's wording kept as overrides

**Depends on:** none

**Files:**
- Modify:
  - `agentflow/prompts/*.md` (every template that names a skill or `origin/main`)
  - `agentflow/lanes.py` (the `skill` and `base_ref` values)
  - `agentflow/config.py` (`PROMPT_PLACEHOLDERS` gains `skill` and `base_ref`)
  - `agentflow/tests/test_portability.py` (the wider scan)
- Create (copies of the current engine templates):
  - `.agentflow/prompts/`
  - `agentflow/tests/fixtures/packs/python/.agentflow/prompts/`
  - `agentflow/tests/fixtures/packs/node/.agentflow/prompts/`
- Test: `agentflow/tests/test_agent_step.py`

**Why:** about 25 engine prompts hard-code this repository's vendored skills. Examples:
- `plan.md` "Use the `writing-plans` skill"
- `task.md` (`subagent-driven-development` and its `implementer-prompt.md`)
- `branch_review.md` and `task_review.md` ("the reviewer the `requesting-code-review` skill would dispatch")
- the `## The \`<skill>\` skill` headings

`branch_review.md` and `security_review.md` also hard-code `git diff origin/main...HEAD`.

That wording is this repository's (CLAUDE.md: "Project wording belongs in `.agentflow/prompts/`"). A pack's override always wins (`pack.prompt_path`), so copying first keeps every existing pack's prompts byte-identical by construction, whichever step, mode, overlay or agent uses them.

- [ ] **Step 1: Copy before changing.**
  - List the templates: `grep -ln '`[a-z][a-z0-9-]*` skill\|origin/main' agentflow/prompts/*.md`.
  - Copy each one, unchanged, into the three `prompts/` directories above.
  - Run the full suite. Expected: all pass, with nothing else changed. This proves the overrides resolve.
  - If `validate` warns about an override that a pack never uses, delete that copy.

- [ ] **Step 2: Write the failing tests**

```python
def test_engine_prompts_name_no_skill_and_no_base_branch():
    for p in (ENGINE / "prompts").glob("*.md"):
        text = p.read_text()
        assert not re.search(r"`[a-z][a-z0-9-]*` skill", text) and "origin/main" not in text, p.name
def test_every_engine_template_fills_with_every_placeholder():   # fmt with all PROMPT_PLACEHOLDERS → no "{name}" left
def test_skill_and_base_ref_placeholders(...)                   # skill: x → "## The `x` skill"; base_ref == ctx.base_ref
def test_engine_names_no_pack_strings()                          # test_portability: the wider file set
```

- [ ] **Step 3: Make the engine templates generic.**
  - Drop each `Use the \`<name>\` skill.` line, because `build_prompt` prepends `Use the \`<step.skill>\` skill.`.
  - Turn the headings into `## The \`{skill}\` skill`, and `origin/main` into `{base_ref}`.
  - Reword `task.md`, `task_next.md`, `task_review.md`, `branch_review.md` and `plan.md`'s package-design sentence without a skill name. Keep their contract text, such as "Critical or Important → blocking", "Declined to judge" and "do not commit".
  - In `build_prompt`, set `values["skill"] = step.skill or ""` and `values["base_ref"] = ctx.base_ref`.

- [ ] **Step 4: Run `test_agent_step.py`, `test_portability.py` and `test_explain_golden.py`, then the full suite.** Expected: all pass, with no existing test edited.

- [ ] **Step 5: Commit**

```bash
git add agentflow/prompts agentflow/lanes.py agentflow/config.py .agentflow/prompts agentflow/tests
git commit -m "Make the engine prompts generic and keep each pack's skill wording as prompt overrides"
```

---

### Task 3: Bundled generic skills, their resolution, and the new `skills.map` keys

**Depends on:** none

**Files:**
- Create: `agentflow/skills/agentflow-<stage>/SKILL.md`, seven small files: `define`, `plan`, `build`, `debug`, `verify`, `review` and `respond`
- Modify:
  - `agentflow/pack.py` (`ENGINE_SKILLS`, `SKILL_NAME`, `skill_file`, `is_bundled`)
  - `agentflow/config.py` (`_Validator.skill`, `SKILLS_MAP_KEYS`)
  - `docs/agentflow-playbook.md` §14 (`project.skills_dir`, `skills.map` and the new keys)
- Test: `agentflow/tests/test_pack.py`, `agentflow/tests/test_config.py`, `agentflow/tests/test_config_edit.py`

**Interfaces:**
- Produces:
  - `pack.ENGINE_SKILLS: Path` (`agentflow/skills`)
  - `pack.SKILL_NAME = re.compile(r"(?!-)[\w.:-]+")`: `lanes.SKILL_LINE`'s character set, with no leading `-`. A fullmatch is required, and `.` and `..` are refused.
  - `pack.skill_file(repo: Path, skills_dir: str, name: object) -> Path | None`
  - `pack.is_bundled(path: Path) -> bool`
  - `SKILLS_MAP_KEYS` gains `"review"`, `"debug"` and `"review_feedback"`. `review_feedback` is the skill sent with blocking per-task findings, and Task 4 consumes it.

**Rules:**
- **`skill_file`.**
  - It returns None unless `name` is a `str` that fully matches `SKILL_NAME` and isn't `.` or `..`.
  - It returns None unless `skills_dir` is relative and has no `..` part.
  - The candidates are built lexically: `<repo>/<skills_dir>/<name>/SKILL.md`, then `ENGINE_SKILLS/<name>/SKILL.md`. The first one that `is_file()` wins.
  - Symlinks are followed, as `_Validator.skill` and briefs do today. The test fixtures symlink `.claude/skills` out of the repo (`conftest.py`, `tmp_cfg` and `flow_repo`). The name rule is the containment; a resolved-path check is not.
- **`_Validator.skill`** uses `skill_file`.
  - A name that fails `SKILL_NAME` is "not a skill name".
  - A missing skill keeps today's message shape: it starts with `skill ` and contains `<skills_dir>/<name>/SKILL.md` (see `test_pack.py`). It adds "and no bundled skill of that name".
- **The brief rule** changes in Task 4, together with the runtime.

**What each SKILL.md says.** Each has frontmatter (`name`, `description`), is at most 80 lines, and is generic: no paths, tools or languages. Each must agree with the engine prompts that deliver it, because both land in the same prompt:
- **define:** write the spec at the step's output path with Objective, Scope, Out of scope, Acceptance criteria and `## Open Questions`, plus a `Status: proposed` line.
- **plan:** each task is a `### Task N: <title>` heading, an optional `**Depends on:** Task k | none` line, a `**Files:**` list using `- Create:`, `- Modify:` and `- Test:` lines, and checkbox steps `- [ ] **Step k: …**`. At most 8 steps and 8 files per task (`sources.MAX_TASK_STEPS`, `MAX_TASK_FILES`). A task ends with an independently testable result.
- **build:**
  - when the repository has tests, work test-first: write the failing test, run it, make it pass;
  - run only the tests you wrote or changed;
  - never run the whole suite, the linters or other gates, because the runner's gates run them after you (`task.md`, `_preamble.md`);
  - never commit.
- **debug:** reproduce, isolate the cause before fixing it, and prove the fix with a test.
- **verify:** the gates' fresh output is in this prompt, so read it and don't run those commands again (`verify.md`). Run only what no gate covers. Claim nothing without evidence.
- **review:**
  - review the diff against the plan and spec;
  - label each finding **Critical**, **Important** or **Minor**, the words `branch_review.md` and `task_review.md` map to `blocking` and `minor`;
  - end with a "Declined to judge" section;
  - include an "Output Format" with a short example.
- **respond:** for each review finding, check it against the code. Fix the ones that hold, with a test where it fits. Say why for any that don't hold. Never agree without checking.

- [ ] **Step 1: Write the failing tests**

```python
# test_pack.py
def test_skill_file_prefers_the_repos_skill(tmp_path)
def test_skill_file_falls_back_to_the_bundled_skill(tmp_path)
def test_skill_file_follows_a_symlinked_skills_dir(tmp_path)     # like the fixtures: .claude/skills -> elsewhere
@pytest.mark.parametrize("name", ["../agentflow-plan", "a/b", "", ".", "..", "x\\y", "-x", None, 3])
def test_skill_file_refuses_a_name_that_is_not_a_skill_name(tmp_path, name)
@pytest.mark.parametrize("skills_dir", ["/abs", "../out", "a/../../b"])
def test_skill_file_refuses_a_skills_dir_outside_the_repo(tmp_path, skills_dir)
def test_every_bundled_skill_names_itself_in_its_frontmatter()   # `name: <dir>`, a `description:`, ≤ 80 lines
def test_the_bundled_review_skill_uses_the_prompts_vocabulary()  # Critical, Important, Minor, "Declined to judge"
def test_bundled_build_and_verify_skills_leave_the_gates_to_the_runner()
# test_config.py
def test_validate_accepts_a_bundled_skill(...)
def test_validate_names_both_places_for_a_missing_skill(...)
def test_validate_refuses_a_path_as_a_skill_name(...)
def test_skills_map_accepts_review_debug_and_review_feedback(...)
# test_config_edit.py
@pytest.mark.parametrize("key", ["review", "debug", "review_feedback"])
def test_new_skills_map_keys_are_refused_by_the_page(key)   # passes already (`skills` is protected); pins it for the new keys
```

- [ ] **Step 2: Run them to see them fail.** Expected: FAIL, `pack.skill_file` is not defined.

- [ ] **Step 3: Write the seven SKILL.md files, then implement the rules.**

- [ ] **Step 4: Run the three test files and `test_portability.py`, then the full suite.** Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add agentflow/skills agentflow/pack.py agentflow/config.py agentflow/tests docs/agentflow-playbook.md
git commit -m "Bundle generic stage skills and resolve a step's skill in the repository first, then in the bundle"
```

---

### Task 4: A step's skill text is its brief, and the review-feedback skill comes from the pack

**Depends on:** Task 2, Task 3

**Files:**
- Modify:
  - `agentflow/lanes.py` (`build_prompt`, `_read_file_skills`)
  - `agentflow/feature_lane.py` (the per-task brief, the review note, `REVIEW_SKILL`)
  - `agentflow/config.py` (`_Validator.brief`)
  - `.agentflow/config.yaml` and `agentflow/tests/fixtures/packs/python/.agentflow/config.yaml` (each gets `skills.map.review_feedback: receiving-code-review`)
- Test: `agentflow/tests/test_agent_step.py`, `agentflow/tests/test_feature_lane.py`, `agentflow/tests/test_config.py`

**Rules:**
- **Where text is read.** All skill and brief text is read from `ctx.repo`, the main checkout (see Global Constraints).
- **`skill_text(cfg, repo, name) -> str | None`** returns the SKILL.md body without its frontmatter:
  - strip a UTF-8 BOM and normalise `\r\n`;
  - if the first line is `---` and a later line is `---`, drop everything through that later line;
  - otherwise the body is the whole file.
- **The brief.** When the step's **configured** `brief:` is empty and its skill resolves, the brief's first part is the skill text. Then come the configured `brief:` files and the `task_briefs` matches (`feature_lane`'s per-task brief), so a `task_briefs` match never suppresses the skill. The value goes through the existing one-pass fill, so braces stay verbatim.
- **A first-session template without `{brief}`, and a step that got its skill as its brief.** `build_prompt` appends `## The \`{skill}\` skill\n\nIts full text follows, so you already have it: do not load it.\n\n{brief}` to the body, before `_result.md`. This is the same trick it uses for `{gate_evidence}`.
  - A batch continuation (the `batch.prompt` session, `task_next.md`) never gets this, because its session already has the text. Find where `feature_lane` builds that prompt, and pass a flag.
- **The skill line for a skill whose text is in the prompt** reads "Use the \`<name>\` skill: its full text is in this prompt, so do not load it."
- **Codex.** `_read_file_skills(text, skills_dir, inlined=frozenset())` leaves every name in `inlined` alone. The existing two-argument call keeps working.
- **`_Validator.brief`.** A template with `{brief}` and no configured `brief:` is valid when the step's skill resolves through `skill_file`. Otherwise the error is today's.
- **The per-task review note.**
  - `feature_lane` stops reading `.claude/skills/receiving-code-review/SKILL.md` directly. It uses `skill_text` of `cfg.skills_map.get("review_feedback")`.
  - When that key is unset or doesn't resolve, the note keeps its instructions and goes without a skill text.
  - Drop `REVIEW_SKILL`.

- [ ] **Step 1: Write the failing tests**

```python
def test_a_step_without_brief_gets_its_skill_as_brief_verbatim(...):
    # bundled SKILL.md (pack.ENGINE_SKILLS read at call time) monkeypatched to hold "{module} and {\"a\": 1}"
    assert "{module} and {\"a\": 1}" in prompt and prompt.index("{module} and") < prompt.index("## When you finish")
def test_a_template_without_brief_gets_the_skill_appended_before_the_result_contract(...)
def test_a_batch_continuation_does_not_resend_the_skill(...)
def test_task_briefs_do_not_suppress_the_skill_text(...)
def test_a_repo_skill_wins_over_the_bundled_one(...)
def test_a_skill_written_only_in_the_runs_worktree_is_not_used(...)
def test_explicit_brief_files_still_win(...)                  # this repo's pack: unchanged
def test_frontmatter_is_stripped_with_crlf_and_bom_and_kept_when_unclosed(...)
def test_codex_read_file_rewrite_leaves_an_inlined_skill_alone(...)
# test_config.py
def test_a_brief_template_with_no_brief_is_valid_when_the_skill_resolves(...)
def test_a_brief_template_with_no_brief_and_no_skill_is_still_an_error(...)
# test_feature_lane.py
def test_blocking_findings_without_a_review_feedback_skill_do_not_crash(...)   # no skills dir at all
def test_blocking_findings_carry_the_review_feedback_skill_text(...)          # the existing stub-text assertion holds
```

- [ ] **Step 2: Run them to see them fail.**

- [ ] **Step 3: Implement.**

- [ ] **Step 4: Run the three test files, then the full suite.** Expected: all pass, with no existing test edited.

- [ ] **Step 5: Commit**

```bash
git add agentflow/lanes.py agentflow/feature_lane.py agentflow/config.py .agentflow/config.yaml agentflow/tests
git commit -m "Give a step without a brief its skill's text as the brief, and take the review-feedback skill from the pack"
```

---

### Task 5: `agentflow next` says when a skill is bundled

**Depends on:** Task 3

**Files:**
- Modify: `agentflow/module_queue.py` (`_skill`)
- Test: `agentflow/tests/test_next_queue.py`

`next` names `skills.map` values for an interactive human (`module_queue._skill`). A bundled skill exists only inside runner prompts, so name it as "`agentflow-plan` (bundled with agentflow: a run uses it; it is not installed for interactive sessions)".

- [ ] **Step 1: Write the failing test** `test_next_marks_a_bundled_skill`, and keep `test_next_names_a_repo_skill_plainly` passing.
- [ ] **Step 2: Run it to see it fail.**
- [ ] **Step 3: Implement**, using `pack.skill_file` and `pack.is_bundled` against the main checkout.
- [ ] **Step 4: Run `test_next_queue.py`, then the full suite.**
- [ ] **Step 5: Commit**

```bash
git add agentflow/module_queue.py agentflow/tests/test_next_queue.py
git commit -m "Say in agentflow next when the skill it names is one bundled with the engine"
```

---

### Task 6: `layers.py`: the merge, and the user layer's rules

**Depends on:** none

**Files:**
- Create: `agentflow/layers.py`
- Modify: `agentflow/config_edit.py` (import `PHASE_FREE_KEYS` from `layers`), `agentflow/tests/conftest.py` (autouse `XDG_CONFIG_HOME` under the test's tmp dir)
- Test: `agentflow/tests/test_layers.py`

**Interfaces:**
- Produces:
  - `layers.ENGINE_DEFAULTS: Path`
  - `layers.WORKFLOW_KEYS = ("lanes", "phases", "conditions", "record_step", "resolve_step", "phase_approval", "checkpoints", "autonomy", "ci", "skills")`
  - `layers.PHASE_FREE_KEYS = ("needs_approval", "resolve", "model", "agent")` (moved from `config_edit.py`)
  - `layers.USER_KEYS = ("agents.*.models", "defaults.run_budget_usd", "defaults.task_budget_usd", "hitl.notify", "hitl.remind_after_h", "dashboard.port")`
  - `class Layer(NamedTuple): name: str; data: dict; path: Path | None`, where `name` is `"engine"`, `"project"`, `"user"` or `"starter:<name>"`
  - `class LayerError(ValueError)` with `.path: str` and `.message: str`
  - `layers.user_config_path() -> Path`
  - `layers.merge(stack: list[Layer]) -> tuple[dict, dict[tuple[str, ...], str]]`. It returns the merged data, and the layer each leaf came from, keyed by the leaf's key tuple.
  - `layers.owns_workflow(data: dict) -> bool`
  - `layers.check_user(data: dict, lower: dict, path: Path) -> None`
  - `layers.origin_of(origins, dotted: str) -> str | None`: the origin of the longest key tuple whose `.`-join is `dotted` or a prefix of it. It is used only to say which file an error came from.

**Merge rules** (this is the algorithm; implement it exactly):
1. **Order and how values combine.**
   - Layers apply bottom-up.
   - Mappings merge key by key. Lists, scalars and mismatched types are replaced by the higher layer.
   - Each entry of `conditions.*` and `gates.*` is replaced whole, and so are `record_step` and `resolve_step`.
   - `merge` deep-copies, never mutates its inputs, and returns no object that is shared with an input.
2. **`null` in a higher layer deletes that key** from the merged result (`agents: {codex: null}`, `gates: null`). This matches the convention of `conftest.deep_merge`. `{}` and `[]` are ordinary values.
3. **Owning the workflow.** A layer owns the workflow when its `phases` maps at least one phase to a mapping that holds `steps`.
   - Such a layer first drops every `WORKFLOW_KEYS` key accumulated from lower layers, then merges.
   - If it has no `lanes` key, raise `LayerError` at `lanes`: "a pack that writes its own phases also writes `lanes`".
4. **Adjusting phases without owning the workflow.** A layer that sets `phases` without owning the workflow can only adjust phases.
   - `phases` must be a mapping.
   - Each `phases.<name>` must be a mapping, must already exist below the layer, and may hold only `PHASE_FREE_KEYS`. A `null` phase is not a deletion here.
   - Otherwise raise `LayerError` at `phases`, `phases.<name>` or `phases.<name>.<key>`, saying that a phase's steps (or the set of phases) are changed by writing the whole workflow.
5. **The user layer.**
   - Every leaf path must start with a `USER_KEYS` pattern, compared segment by segment, where `*` matches exactly one segment. So `agents.claude.models.build` matches `agents.*.models`, while `dashboard` alone doesn't match `dashboard.port`.
   - A `*` segment must name a key that exists in `lower`: a user file can't create an agent.
   - Otherwise raise `LayerError` with path `<user file>: <dotted key>`, and the message "not a personal setting; set it in the pack" or "no agent `<name>` in the pack".
6. **What `merge` itself does.** `merge` only applies the layers it is given, and `check_user` enforces rule 5.

- [ ] **Step 1: Write the failing tests**

```python
def test_mappings_merge_and_lists_replace()
def test_null_deletes_a_lower_key()
def test_merge_never_mutates_or_shares_its_inputs()
def test_a_named_condition_and_a_gate_are_replaced_whole()
def test_a_layer_with_steps_drops_the_lower_workflow()
def test_a_layer_with_steps_and_no_lanes_is_an_error()
def test_a_phase_knob_merges_into_the_lower_workflow()        # origins[("phases","build","needs_approval")] == "project"
@pytest.mark.parametrize("phases", [None, [], "x", {"build": None}, {"build": 3}, {"deploy": {"model": "x"}},
                                    {"build": {"skip_if": "x"}}])
def test_a_phase_adjustment_that_is_not_a_knob_is_an_error(phases)
def test_the_user_layer_overrides_the_pack_for_personal_keys()
def test_a_user_model_for_one_role_is_personal()             # agents.claude.models.build
@pytest.mark.parametrize("key", ["gates.x.run", "agents.claude.command", "guardrails.forbidden_paths",
                                 "phases.build.needs_approval", "dashboard"])
def test_the_user_layer_refuses_anything_not_personal(key)
def test_the_user_layer_cannot_create_an_agent()
def test_origin_of_finds_the_layer_for_a_dotted_error_path()
def test_user_config_path_follows_xdg_config_home(monkeypatch)   # a relative XDG_CONFIG_HOME falls back to ~/.config
```

- [ ] **Step 2: Run them to see them fail.** Expected: FAIL, `No module named 'agentflow.layers'`.

- [ ] **Step 3: Implement `layers.py`** with no import from `config.py` (it is a leaf), and the conftest fixture.

- [ ] **Step 4: Run `test_layers.py` and `test_config_edit.py`, then the full suite.** Expected: all pass. Nothing calls `merge` yet.

- [ ] **Step 5: Commit**

```bash
git add agentflow/layers.py agentflow/config_edit.py agentflow/tests
git commit -m "Add the config layer merge with null deletion, phase knobs and a personal-only user layer"
```

---

### Task 7: Engine defaults, and `load_config` over the layers

**Depends on:** Task 4, Task 6

**Files:**
- Create: `agentflow/defaults/config.yaml`
- Modify:
  - `agentflow/config.py` (`load_config`, `Config.merged`, `Config.layer_origins`, the `gate_sets.verify` message)
  - `agentflow/tests/conftest.py` (a `sparse_repo` fixture: a tmp git repo on `main`, with one commit and a sparse pack)
  - `docs/agentflow-playbook.md` §14 (a "Layers" paragraph at the top: the order, `null` deletes, and which keys the defaults supply)
- Test: `agentflow/tests/test_config.py`, `agentflow/tests/test_pack.py`

**Interfaces:**
- Consumes: `layers.merge`, `layers.check_user`, `layers.user_config_path`, `layers.origin_of`.
- Produces:
  - `config.load_config(path: Path, *, layered: bool = True, user: bool = True) -> Config`
  - `Config.merged: dict`: the merged data **before** `@` resolution. `Config.raw` stays the resolved data, as today.
  - `Config.layer_origins: dict[tuple[str, ...], str]`

**Behaviour:**
- **The stack.** `load_config` builds `[engine, project, user]` (Task 10 inserts the starter). It runs `check_user` on the user layer before merging, and reads the user file only when `user` is true and the file exists.
- **Format checks.** `version` and `engine` are checked on the project file alone, before merging.
- **Errors.** A `LayerError` becomes a `ConfigError` with the same path and message. A `ConfigError` raised later at a path whose `origin_of` is `"user"` gets the user file's path prefixed.

**The defaults file.** Write `agentflow/defaults/config.yaml` to hold exactly the following.

*Outside the workflow:*
- `project`: `base_branch: main`, `fork_from: main`, `skills_dir: .claude/skills`, `commit_trailer: ""`, `docs: {specs: docs, plans: docs/plans}`. No `worktrees_dir` and no `spec_file`.
- `gates: {}` and `gate_sets: {}`.
- `guardrails: {forbidden_paths: [], reject_trailers: [], user_only_skills: []}`.
- `agents`:
  - `claude`: `command`, `base_args`, `models`, `prices` and `skill_loading`, copied from this repository's pack, with **no** `permission_mode`.
  - `codex`: `enabled: false`, with its `command` and `models`.
- `defaults`, `context`, `effort` and `recovery`: only each key's code fallback in `load_config`. Nothing taken from this repository's choices: no `defaults.env`, and `context.default_window_tokens` is the fallback.
- `hitl: {}` (`load_config` treats it as no `hitl`) and `dashboard: {host: 127.0.0.1, port: 7788}`.

*The workflow:* this repository's `lanes`, `phases`, `conditions`, `record_step`, `resolve_step`, `phase_approval`, `ci` and `autonomy`, changed as follows:
- **Ids.** Give every step, nested ones included, an explicit `id:`, because the id must not fall back to a `@skills.map.*` reference.
- **Skills.** Replace every `skill:` with `"@skills.map.<key>"`, using this table:

  | step (by its id in this repo's pack) | key |
  |---|---|
  | spec, define steps | `define` |
  | design-doc steps, `record_step` | `design_doc` |
  | plan | `plan` |
  | build `task`, `regression_test`, `fix` | `build` |
  | `small_plan.skill` | `build_small` |
  | `debug`, `resolve_step`, the `sync` and `ci` fix steps | `debug` |
  | `verify` | `verify` |
  | `branch_review`, per-task `review` | `review` |
  | `plan_fixes`, `fix_findings` | `review_feedback` |

  Any step not in the table is a gap in this plan: stop and ask.
- **Briefs.** Remove every `brief:` and `task_briefs:`, because a step's skill is now its brief.
- **Removed outright:**
  - the `idea` group
  - the `contracts` and `security_review` steps
  - `approval_note`
  - `human_paths`
  - the conditions only those used (`spec_defines_contracts`, `spec_api_reviewed`, `spec_needs_api_design`, `security_path`)
- **Set:** `checkpoints: {}`.

*`skills.map`:* `decide`, `define` and `design_doc` → `agentflow-define`; `plan` → `agentflow-plan`; `build` and `build_small` → `agentflow-build`; `debug` → `agentflow-debug`; `verify` → `agentflow-verify`; `review` → `agentflow-review`; `review_feedback` → `agentflow-respond`. No `ship`.

- [ ] **Step 1: Write the failing tests**

```python
def test_a_sparse_pack_loads_on_the_engine_defaults(sparse_repo)
    # pack: version, engine, gates {unit: {run: "true"}}, gate_sets {setup: [], build: [unit], verify: [unit]},
    #       agents {claude: {permission_mode: bypassPermissions}}
    # phases == define…ship, lanes == [feature, bug], no validate errors
def test_explain_runs_on_a_sparse_pack(sparse_repo)              # `agentflow explain feature --module greet` and `bug` exit 0
def test_every_default_step_has_an_explicit_id_and_a_mapped_skill()
def test_every_skills_map_reference_in_the_defaults_resolves()
def test_engine_defaults_match_the_code_fallbacks()              # each defaults/context/effort/recovery value == load_config's fallback
def test_the_defaults_set_no_permission_mode()
def test_a_pack_without_gates_is_refused_with_a_starter_hint()   # error at gate_sets.verify mentioning `starter:` or `gates:`
@pytest.mark.parametrize("pack", ["python", "node", "this repo"])
def test_full_packs_keep_every_value_they_state(pack):
    cfg = load_config(p)
    for key, value in leaves(yaml.safe_load(p.read_text())):        # every leaf the file states
        assert get(cfg.merged, key) == value and cfg.layer_origins[key] == "project", key
@pytest.mark.parametrize("pack", ["python", "node", "this repo"])
def test_full_packs_differ_from_unlayered_only_where_listed(pack)
    # compare every Config field (except path, raw, merged, layer_origins) of load_config(p) and load_config(p, layered=False);
    # each difference must be a dotted leaf path in INHERITED[pack], with a reason (e.g. "agents.codex": "the engine's disabled codex")
def test_a_user_value_of_the_wrong_type_names_the_user_file(...)
def test_a_broken_user_file_is_a_config_error_naming_it(...)
def test_load_config_without_the_user_layer_ignores_a_broken_user_file(...)
```

- [ ] **Step 2: Run them to see them fail.** Expected: FAIL, `load_config() got an unexpected keyword argument 'layered'`.

- [ ] **Step 3: Write the defaults file and implement.** Then run the full suite and list every failing test. Each one is either a bug to fix, or one of these intended changes:
  - **Tests that delete a required key and expect an error:** `test_config.py`'s `test_missing_section_is_a_config_error` (`gates`) and `test_pack.py`'s `test_docs_specs_and_plans_are_required` (`project.docs.*`).
  - `tmp_cfg`'s `None` drops the key before the file is written, so the default now fills it. Those tests switch to a callable patch that writes `null` into the file (rule 2), which restores the error.

  Edit no other existing test without naming it, with the reason, in the PR body.

- [ ] **Step 4: Run `test_config.py`, `test_pack.py`, `test_explain_golden.py` and `test_portability.py`, then the full suite.** Expected: all pass, and `git status agentflow/tests/fixtures/explain` is clean.

- [ ] **Step 5: Commit**

```bash
git add agentflow/defaults agentflow/config.py agentflow/tests docs/agentflow-playbook.md
git commit -m "Load the pack over engine defaults and a personal user file"
```

---

### Task 8: Worktrees in the state dir, and seeing layer changes during and across a run

**Depends on:** Task 7

**Files:**
- Modify:
  - `agentflow/config.py` (`Project.worktrees_dir: str | None`, `worktrees_root`, the `worktrees_dir` check, `layer_files`)
  - `agentflow/pipeline.py` (`_set_up` uses `worktrees_root`; `_Loop._config_sha`)
  - `agentflow/config_trust.py` (`start_options` records `layers_sha256`, plus a check on resume)
  - `docs/agentflow-playbook.md` §14
- Test: `agentflow/tests/test_config.py`, `agentflow/tests/test_pipeline.py`, `agentflow/tests/test_config_edit.py`

**Interfaces:**
- Produces:
  - `config.worktrees_root(cfg: Config, repo: Path) -> Path`
  - `config.layer_files(cfg: Config) -> list[Path]`: the engine defaults, the starter file if any, the pack and the user file if it exists, in that order
  - `config_trust.layers_sha256(paths: list[Path]) -> str`: the SHA-256 over each file's path and bytes, in order

**Behaviour:**
- **Worktrees.**
  - `worktrees_root` returns `pack.state_dir(repo) / "worktrees"` when `project.worktrees_dir` is unset, and today's path when it is set.
  - `validate` checks a set value is relative with no `..` part.
- **During a run.**
  - `_Loop._config_sha` returns `layers_sha256(layer_files(cfg))` when the config is the repository's pack, and None otherwise, as today.
  - It hashes bytes only. It never calls `load_config`, and it catches the same exceptions it does today.
  - `_Loop.reload` is unchanged. A change to any layer file between phases therefore goes through its existing load, validate and "keeps the phases" path, including `config_reload_skipped` for a broken edit.
- **Across a resume.**
  - `start_options` also records `layers_sha256`.
  - On resume, a different digest prints one line naming which layer files changed since the run started. The run continues on the current files: `config_trust` still decides which pack file is trusted, by its own hash.
  - **Trade-off (playbook §14):** an engine upgrade that changes the default workflow is reported on resume, not refused.

- [ ] **Step 1: Write the failing tests**

```python
def test_worktrees_default_into_the_state_dir(sparse_repo)
def test_a_set_worktrees_dir_still_wins(...)
@pytest.mark.parametrize("value", ["/abs", "../up"])
def test_a_worktrees_dir_outside_the_repo_is_an_error(value)
def test_worktrees_dir_is_still_refused_by_the_page()              # `project` is protected
# test_pipeline.py
def test_a_changed_user_file_between_phases_is_reloaded(...)
def test_a_broken_user_file_between_phases_is_skipped_with_its_reason(...)
def test_resume_reports_changed_layer_files(...)
```

- [ ] **Step 2: Run them to see them fail.**

- [ ] **Step 3: Implement.**

- [ ] **Step 4: Run the three test files and `test_config_trust.py`, then the full suite.** Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add agentflow/config.py agentflow/pipeline.py agentflow/config_trust.py agentflow/tests docs/agentflow-playbook.md
git commit -m "Put worktrees in the state dir by default and notice changed config layers during and across a run"
```

---

### Task 9: `agentflow config show --effective`, and Settings on a sparse pack

**Depends on:** Task 7

**Files:**
- Modify:
  - `agentflow/flow.py` (a new `config` subcommand)
  - `agentflow/layers.py` (`rows`)
  - `agentflow/config_edit.py` (`form_fields`, `_field_index`, `_catalog`, `_options`, `_comments`, `_apply_form`, `_check`)
  - `agentflow/dashboard.html` (the source label)
  - `docs/agentflow-playbook.md` §10
- Test: `agentflow/tests/test_layers.py`, `agentflow/tests/test_config_edit.py`, `agentflow/tests/test_control_cmds.py`

**Interfaces:**
- Produces:
  - `layers.rows(merged: dict, origins) -> list[tuple[str, Any, str]]`: one `(dotted key, value, layer)` per leaf, in merged order
  - `form_fields` items gain `"source": str`

**Behaviour:**
- **The CLI.**
  - `agentflow config show [--config X]` prints the pack file.
  - `agentflow config show --effective [PREFIX] [--config X]` prints `<dotted>: <one-line JSON>  [<layer>]` per row.
  - `--config` sits on the subcommand like on every other command (the top-level parser has none). The nested subparser uses `dest="config_command"`.
- **The form reads `Config.merged`.** `_catalog`, `_options`, `_field_index` and `form_fields`' parent-presence check all use it. So `phases.*`, `agents.*` and the `@agents`/`@lanes` options exist for a sparse pack, and so do sections such as `hitl`. `present` still means "in the project file". `_comments` reads the comments of every layer file, so an inherited field keeps its help text.
- **Read-only, enforced on the server.** A field whose source is `user`, and a map field with any leaf from the user layer, gets `readonly: "set in your user config (<path>), which overrides the pack"` in its `_field_index` entry. `_apply_form` therefore refuses it on the server, not only in the page. This also keeps personal values out of the committed pack.
- **Saving a map field.** A map field (`agents.*.models`, `autonomy.resolve_kinds`, `dashboard.backlog.lane_labels`) writes only the entries whose value differs from the merge of the layers below the project.
  - `_apply_form`'s `expected` check must use that same filtered value.
  - An inherited entry can't be deleted from the page, and the help text says to write `null` for it in the YAML tab.
- **Protected keys.** `_check` keeps comparing `_protected_view` before and after, but on `load_config(...).raw` of the old and new **layered** configs.
  - A phase knob or an agent's model added to a sparse pack therefore leaves the protected view unchanged: the names of the phases and agents come from the defaults in both.
  - Protected values stay compared after `@` resolution, as today.
- **The page.** `dashboard.html` shows `from <source>` in the field's muted sub-line when the source isn't `project`.

- [ ] **Step 1: Write the failing tests**

```python
def test_rows_list_every_leaf_with_its_layer()                    # ("defaults.step_timeout_min", 45, "engine")
def test_config_show_effective_names_each_layer(sparse_repo)
def test_config_show_prints_the_pack_as_written(sparse_repo)
def test_config_show_takes_config_like_the_other_commands(sparse_repo)
def test_form_shows_the_effective_value_its_layer_and_its_help(sparse_repo)
def test_form_saves_a_phase_knob_into_a_sparse_pack(sparse_repo)  # not refused as protected; steps unchanged after load
def test_form_saves_an_agent_model_into_a_sparse_pack(sparse_repo)
def test_form_offers_agents_and_lanes_on_a_sparse_pack(sparse_repo)
def test_a_field_set_in_the_user_file_is_refused_on_save(sparse_repo)
def test_a_map_with_a_user_entry_is_refused_on_save(sparse_repo)
def test_saving_an_inherited_map_writes_only_the_changed_entry(sparse_repo)
def test_a_protected_change_on_a_sparse_pack_is_still_refused(sparse_repo)   # e.g. a new gate
```

- [ ] **Step 2: Run them to see them fail.** Expected: FAIL, `no attribute 'rows'`.

- [ ] **Step 3: Implement.**

- [ ] **Step 4: Run the three test files, then the full suite. Then check by hand:** run `bin/agentflow dashboard` in a sparse temp repo. Confirm that Settings shows "from engine" on inherited fields with their help text, and that Review and save adds only the changed key to the file.

- [ ] **Step 5: Commit**

```bash
git add agentflow/flow.py agentflow/layers.py agentflow/config_edit.py agentflow/dashboard.html agentflow/tests docs/agentflow-playbook.md
git commit -m "Show the effective config with each value's layer, in the CLI and on the Settings page"
```

---

### Task 10: Starters, and the `starter` key

**Depends on:** Task 7

**Files:**
- Create: `agentflow/starters/<name>/config.yaml`, four small files: `python-uv`, `node-pnpm`, `go` and `docs-only`
- Modify: `agentflow/layers.py` (the starter layer), `agentflow/config.py` (`load_config` inserts it, plus validate), `agentflow/config_edit.py` (`_PROTECTED_KEYS` += `"starter"`), `docs/agentflow-playbook.md` §14
- Test: `agentflow/tests/test_layers.py`, `agentflow/tests/test_config.py`, `agentflow/tests/test_config_edit.py`

**Interfaces:**
- Produces:
  - `layers.STARTERS: Path`
  - `layers.starter_names() -> list[str]` (sorted directory names)
  - `layers.base_layers(starter: object) -> list[Layer]`
- **What `base_layers` returns:**
  - **`None`** (the key is absent or null): no starter, so just the engine layer.
  - **a `str` in `starter_names()`:** the engine layer, then `Layer(f"starter:{name}", …)`.
  - **anything else**, including `""`: `LayerError` at `starter`, listing the names.
  - It never builds a path from an unchecked name.
- `load_config`'s stack becomes `base_layers(project.get("starter"))`, then the project layer, then the user layer.

Each starter is sparse, doesn't own the workflow, and keeps the default `lanes`. It defines `gates` and all three of `gate_sets.setup`, `build` and `verify`:

| starter | gates | setup / build / verify |
|---|---|---|
| python-uv | `sync: uv sync`, `tests: uv run pytest -q` | [sync] / [tests] / [tests] |
| node-pnpm | `install: pnpm install --frozen-lockfile`, `test: pnpm test` | [install] / [test] / [test] |
| go | `vet: go vet ./...`, `test: go test ./...` | [] / [vet] / [vet, test] |
| docs-only | `whitespace` (below) | [] / [whitespace] / [whitespace] |

**The `docs-only` gate** checks every tracked file against the empty tree, so it also works on a committed tree and on an unborn HEAD. It turns off `blank-at-eol`, so a Markdown line break (two trailing spaces) isn't flagged:

`git -c core.whitespace=-blank-at-eol diff --check "$(git hash-object -t tree /dev/null)"`

Check it in a tmp repo before writing it:
- an unborn HEAD passes;
- a `.md` with a two-space line break passes;
- a committed conflict marker fails.

**Trade-off (playbook §14):** it doesn't see untracked files. A docs repository should add a real gate, such as a Markdown linter.

- [ ] **Step 1: Write the failing tests**

```python
@pytest.mark.parametrize("name", ["docs-only", "go", "node-pnpm", "python-uv"])
def test_every_starter_validates_with_no_errors(name, sparse_repo)   # pack: version, engine, starter: name, the permission line
def test_no_starter_owns_the_workflow_or_sets_lanes()
def test_no_starter_is_the_same_as_an_absent_or_null_starter()
@pytest.mark.parametrize("value", ["nope", "", "../defaults", "/etc", 3, ["go"]])
def test_a_starter_that_is_not_a_known_name_is_refused(value)    # ConfigError at "starter", lists the four names
def test_the_pack_overrides_its_starter()                        # gates.tests.run in the pack wins; origin "project"
def test_the_docs_only_gate_passes_on_an_unborn_repo_and_a_markdown_line_break(tmp_path)
def test_starter_is_protected_on_the_page()
```

- [ ] **Step 2: Run them to see them fail.** Expected: FAIL, the `starter` key is ignored.

- [ ] **Step 3: Implement.**

- [ ] **Step 4: Run the three test files and `test_portability.py`, then the full suite.** Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add agentflow/starters agentflow/layers.py agentflow/config.py agentflow/config_edit.py agentflow/tests docs/agentflow-playbook.md
git commit -m "Add the python-uv, node-pnpm, go and docs-only starters as a config layer under the pack"
```

---

### Task 11: The `gates_confirmed` and `gates.<name>.baseline` keys, a gate's `cwd`, and the bug-lane warning

**Depends on:** Task 10

**Files:**
- Modify:
  - `agentflow/config.py` (`Gate.baseline`, `BASELINES`, validate)
  - `agentflow/config_edit.py` (`_PROTECTED_KEYS` += `"gates_confirmed"`; `gates` is already protected)
  - `.agentflow/config.yaml`, `agentflow/tests/fixtures/packs/python/.agentflow/config.yaml` and `agentflow/tests/fixtures/packs/node/.agentflow/config.yaml` (each gets `gates_confirmed: true`)
  - `docs/agentflow-playbook.md` §14
- Test: `agentflow/tests/test_config.py`, `agentflow/tests/test_config_edit.py`

**Rules:**
- **`gates_confirmed`.** Any value other than `true` is a warning: "detected or inherited gates are a proposal: check `gates` and `gate_sets`, then set `gates_confirmed: true`". A non-bool value is an error.
  - **Trade-off (playbook §14):** it is a bool, not bound to the commands that were confirmed. A starter's gates changing in an engine upgrade don't ask again, so the release notes of an engine that changes a starter must say so.
- **`baseline`.** `baseline: red` is a warning: "`<gate>` failed on `<base_branch>` when the pack was set up: fix it and remove `baseline`, or delete the gate". A value other than `red` or `green` is an error.
- **`cwd`.** `gates.<name>.cwd` must be relative with no `..` part (an error). Gates run at `cwd / gate.cwd`, and PnP-2 writes `cwd` from CI files.
- **The bug-lane warning** (Deviation 3). When `lanes` includes `bug` and `.claude/settings*.json` in the main checkout holds no `Bash(...)` allow rule at all, warn at `lanes`: "the bug lane's red/green check runs the regression test only when a Bash allow rule in .claude/settings.json covers it: add a narrow rule such as `Bash(<verify gate's program> *)`". Read the rules with `guards`' existing loader. This repository and the fixture packs have allow rules, so they get no new warning. Check that before relying on it.

- [ ] **Step 1: Write the failing tests**

```python
def test_unconfirmed_gates_warn()                  # exactly one warning at "gates_confirmed"
def test_gates_confirmed_must_be_a_bool()
def test_a_red_baseline_warns_naming_the_base_branch()
def test_a_baseline_other_than_red_or_green_is_an_error()
@pytest.mark.parametrize("cwd", ["/abs", "../up", "a/../../b"])
def test_a_gate_cwd_outside_the_repo_is_an_error(cwd)
def test_the_bug_lane_without_any_bash_allow_rule_warns(sparse_repo)
def test_the_bug_lane_with_a_bash_allow_rule_does_not_warn(sparse_repo)
@pytest.mark.parametrize("change", ["gates_confirmed: false", "gates.unit.baseline: red", "gates.unit.cwd: x"])
def test_gate_confirmation_and_gate_fields_are_protected(change)
```

- [ ] **Step 2: Run them to see them fail.**

- [ ] **Step 3: Implement.** Add `gates_confirmed: true` to the three full packs, and to the sparse packs that earlier tasks build in tests and expect to have no warnings.

- [ ] **Step 4: Run the two test files and `bin/agentflow validate` (expected: "0 warnings"), then the full suite.**

- [ ] **Step 5: Commit**

```bash
git add agentflow/config.py agentflow/config_edit.py .agentflow/config.yaml agentflow/tests docs/agentflow-playbook.md
git commit -m "Require a human to confirm the gates, record a gate's baseline, keep a gate's cwd inside the repo and warn about the bug lane"
```
