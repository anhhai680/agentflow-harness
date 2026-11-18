# agentflow cost, slice C2 (run brief and short resumes) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every Claude run session gets the stable text of its phase and role as a byte-identical appended system prompt, which later sessions read from cache. That text is the repository's agent rules, the engine preamble, the step's skill and `brief:` files, and the result contract. The user prompt carries only what is specific to the step, and resume turns send only what changed.

**Architecture:**
- **Prompt layout:** the step-specific lines of `_preamble.md` and `_result.md` move into a new engine prompt, `_step_head.md`.
- **Rendering and sources:** a new module, `brief.py`, reads its sources **frozen at the run's fork commit** (`state.base_sha`), using hardened `git cat-file` in the main checkout. Those sources are the rules and the repository's skill and `brief:` files. No agent of the run can change that commit.
- **Delivery:**
  - The brief is written to the run's state dir and passed with `--append-system-prompt-file`.
  - `CLAUDE_CODE_DISABLE_CLAUDE_MDS=1` is passed only when the brief carries the rules. Otherwise Claude loads its own memory files, as it does today.
- **Pinning:** a session is pinned to the brief it started with, and every resume of it passes the same file.
- **Short resumes:**
  - Fix turns get a capped gate excerpt.
  - A resumed refine round gets only the note.
  - A session with the brief gets a one-line result reminder.

**Deferred (decided 2026-11-18, doubt review):** the approved spec and plan in the brief (spec L2c item 4), and L6d. They need their own design for what "approved" means. Today a runner auto-approval looks like a human one, and `approval_sha` is taken when approval is *asked for*. They also need a rule for finding the artifact's writer and path, and for a spec that changes later. They move to a later slice.

**Tech Stack:** Python 3.12 stdlib + PyYAML; pytest with `tests/fake_agent.py`.

**Spec:** `docs/specs/agentflow-cost-proposal.md` (§2 to §9 below, then the Decided table):

| Section | What this plan implements from it |
|---|---|
| §3.2 L2c | items 1–3, plus its "Amended for the C2 plan" note |
| §3.3 L3b | all of it |
| §4 | the rows `context.run_brief`, `context.resume_gate_lines` and `project.agent_rules` |
| §5 | what stays exactly as it is |
| §7 | the "C2 Brief" row |
| §8 and §9 | the risks and the testing strategy |

## Global Constraints

**Repository rules.**
- **Dependencies:** engine dependencies stay stdlib + `pyyaml>=6`.
- **No project strings** in engine files (`test_portability.py`).
- **Golden files:** `agentflow/tests/fixtures/explain/*.txt` stay byte-identical.
- **Config keys:** every new key ships, in the same task, with a Settings form field or a `_PROTECTED_KEYS` entry, `validate` checks, a `test_config_edit.py` case and a playbook §14 line.
- **Prompt placeholders:** every new one joins `config.PROMPT_PLACEHOLDERS`.
- **Changelog:** every task with a user-visible change adds one line under `## [Unreleased]` in `CHANGELOG.md`.
- **Retired entries:** every dropped or reworded line of an engine prompt gets a `pack_fix.Retired` entry in the same task, with `retired_in="2.10"`. `test_pack_fix.test_register_matches_the_engine_prompts` must stay green.

**Defaults.**
- `context.run_brief: true`
- `context.resume_gate_lines: 80`
- `project.agent_rules: [CLAUDE.md]`

**Which sessions get a brief.**
- **Who gets it:** only calls from an `Executor` built with `briefs=True` (the default), whose adapter has `takes_brief = True` (`ClaudeAgent` only).
- **Who doesn't:**
  - the retro (`retro.py` builds its `Executor` with `briefs=False`);
  - sizers (`sizing_lane.py` builds its own call);
  - Codex.

**Trust: what the brief reads.**
- **Where:** `pack.checkout_root(run.repo)`, at `run.state["base_sha"]`, through `gitops.read_blob`. This covers:
  - the rules;
  - the repository's own skill files;
  - the step's `brief:` files;
  - a pack override of `_preamble.md` or `_result.md` (`.agentflow/prompts/<name>`).
- **Integrity:** `read_blob` checks each blob's content against the object id `ls-tree` gave, so a rewritten loose object is refused.
- **What it never reads:** the main checkout's working tree, or a run's worktree.
- **Engine files:** the engine's own prompts and bundled skills come from the engine's files.
- **No `base_sha`:** a run without one gets a brief with no rules section, no repository skill and the engine's preamble and result, or no brief when the pack overrides them.
- **When the rules can't be carried** (Review Focus 3), the session falls back to today's behaviour: Claude loads `CLAUDE.md` from the worktree itself. That is no regression, but it isn't protected either.
- **Accepted residual risk:** a detached process an earlier agent left running could swap the brief file between the runner's write and the CLI's read. The runner rewrites the file from memory right before each call. An agent with Bash in a run can already write outside its worktree, so this slice doesn't close that window.

**Fallbacks.**
- **Rules:** rules the brief can't carry faithfully fall back to today's behaviour. That means no rules section and no `CLAUDE_CODE_DISABLE_CLAUDE_MDS`, so Claude loads them itself.
- **The whole brief:** a brief that can't be built or written gives no brief. The prompt is then built in full (as with `run_brief: false`), and `agent_started` records `brief: null` and a `brief_skipped` reason. The brief is decided **before** the prompt is built, and the `_Turn` carries that decision.

**Sizes.**
- `BRIEF_FILE_MAX_BYTES = 200_000` per file.
- `BRIEF_MAX_CHARS = 150_000` for the whole brief.

**The brief file.**
- **Path:** `<run dir>/brief/<phase>-<role>-<digest12>.md`.
- **Writing:** the folder is made with `safeio.ensure_dir`, and the file is written with `safeio.replace_file`.
- **Checks on resume:** the pinned path must be that folder plus a name whose digest part matches the pinned `sha256`. The Edit tools can't change the file (the `runs_root` deny rule). Bash can, which is why the digest is checked before each resume.

**Lint and tests.** `uvx ruff@0.17.4 check .` must print `All checks passed!` before each commit. Tests run with `uv run --no-project --with pyyaml --with pytest pytest <file> --import-mode=prepend -q`.

**Security overlay.**
- **Overlay paths:** `agents.py`, `gitops.py`'s new reader and `pack_fix.py` are reviewed with `security-and-hardening` and the `security-auditor` agent. This plan adds `agentflow/brief.py` to the overlay paths (Task 3).
- **Interactive only:** this plan runs interactively. Task 3's edits to `.agentflow/config.yaml` and `CLAUDE.md` must not be delegated to an agentflow run, because `.agentflow/**` is forbidden there.

## Review Focus

1. **An agent changes `CLAUDE.md`, a skill or a pack prompt in the worktree, or in the main checkout's working tree, while the brief carries the rules.** None of it reaches any later session's system prompt. The brief reads them from `base_sha` objects, with `--no-replace-objects`, after checking their ids. *(Tasks 3 and 4.)*
2. **A resumed session, including the third and later tasks of a shared session or batch.** It keeps the brief it started with when `run_brief` is switched off or the runner restarts. Every resume call site passes the pinned file.
   - A brief that can't be rebuilt to its pinned digest after a restart starts a fresh session (with `brief_replaced`), never a stop that `retry` can't clear.
   - A session that started with no brief never gains one.

   *(Task 4.)*
3. **Memory the brief can't carry.** The cases are an `@` import (anywhere outside code), a committed `.claude/CLAUDE.md`, `.claude/rules/**`, or a `CLAUDE.md` below the root. In each, the rules section is left out, `CLAUDE_CODE_DISABLE_CLAUDE_MDS` is neither set nor inherited, and `validate` warns. Decorators inside code fences are not imports. *(Tasks 1, 3 and 4.)*
4. **A pack override of `_preamble.md` or `_result.md`.**
   - An exact old copy is repaired by `validate --fix`.
   - A copy that holds any placeholder outside `BRIEF_PLACEHOLDERS` turns the brief off for the call, and `validate` warns.
   - The run facts, code map and lessons never show twice. CRLF files and a slot at end of file are handled.

   *(Tasks 2 and 4.)*
5. **Gate output that is short, has no failure line, has very long lines, or comes from many failing gates.**
   - Short output is kept whole.
   - With no failure line, the excerpt keeps the last lines.
   - Long lines are cut to 300 chars.
   - The total stays within `resume_gate_lines`, and every gate keeps its log path.

   *(Task 5.)*

---

### Task 1: The three config keys and their `validate` checks

**Files:**
- Modify: `agentflow/config.py`:
  - `ContextPolicy` gains `run_brief: bool` and `resume_gate_lines: int`;
  - `Project` gains `agent_rules: tuple[str, ...]`;
  - the `Config` build;
  - `_Validator.context` and `_Validator.project`;
  - a new `rules_path_problem`.
- Modify: `agentflow/gitops.py`: a new `tree_paths(repo: Path, sha: str) -> list[str] | None`.
  - It runs `git *HARDENED ls-tree -r --name-only -z <sha>` through `subprocess.run` in bytes mode, with `timeout=30`.
  - A name that isn't valid UTF-8 is left out.
  - It returns None on any `CalledProcessError`, `OSError` or `TimeoutExpired`.

  `validate` uses it on `HEAD`, and Task 3 uses it on `base_sha`.
- Modify: `agentflow/defaults/config.yaml`: `project.agent_rules: [CLAUDE.md]`, `context.run_brief: true` and `context.resume_gate_lines: 80`, one comment each.
- Modify: `agentflow/config_edit.py`, in `_catalog` "Context":
  - `run_brief`, `{"kind": "bool"}`, labelled "Stable text in a cached system prompt (run brief)";
  - `resume_gate_lines`, `{"kind": "int", "min": 10, "max": 1000}`, labelled "Gate lines in a fix prompt".

  `project.agent_rules` is already covered by the protected `project` key.
- Modify: `docs/agentflow-playbook.md` §14 (three key lines), `CHANGELOG.md`
- Test: `agentflow/tests/test_config.py`, `agentflow/tests/test_config_edit.py`

**Interfaces:**
- Produces:
  - `cfg.context.run_brief` (default True);
  - `cfg.context.resume_gate_lines` (default 80);
  - `cfg.project.agent_rules` (default `("CLAUDE.md",)`).

  All three are optional, read with `_optional`.
- Produces: `config.rules_path_problem(entry: object) -> str | None`. It normalises with `PurePosixPath` and returns why the entry is refused, or None. An entry is refused when:
  - it is not a non-empty string;
  - it is absolute;
  - it has a `..` segment;
  - it starts with `-`;
  - its first segment, case-folded, is `.agentflow` or `.git`.
- Produces: `config.memory_blockers(paths: list[str], read: Callable[[str], str | None], rules: Sequence[str]) -> list[str]`. It lists why the brief can't carry the rules, given a tree's file list and a reader for a listed rules file:
  - `".claude/CLAUDE.md"`;
  - `".claude/rules/"`;
  - `"<dir>/CLAUDE.md"` for each nested one;
  - `"<file>: @ import"`. An import is any token matching `(?<![\w`])@[^\s`]+` outside fenced code blocks and inline code spans. That includes a bare `@README`. A decorator in prose is a false positive, and it fails toward today's behaviour;
  - `"CLAUDE.md is not in agent_rules"`, when a root `CLAUDE.md` is committed but not listed.

  Task 3 reuses it.

**The `validate` checks:**
- **Error, path `context.resume_gate_lines`:** not an integer from 10 to 1000.
- **Warning, path `context.resume_gate_lines`:** above `context.gate_output_lines`. The message says only the gate's tail is kept.
- **Error, path `project.agent_rules`:** not a list, or an entry `rules_path_problem` refuses.
- **Warnings at `project.agent_rules`, only while `run_brief` is true.** They come from `tree_paths(main checkout, "HEAD")`:
  - an entry other than the default `CLAUDE.md` that is not in the tree;
  - a listed file over 200_000 bytes, or a symlink whose target `rules_path_problem` refuses;
  - one warning per `memory_blockers` reason.

  Each warning says that run sessions will let Claude load its own memory files instead. A brief over its size cap can only be known at run time, where it shows as `brief_skipped`.

- [ ] **Step 1: Write the failing tests**
  - `test_config.py::test_cost_c2_keys_default`.
  - `test_config.py::test_resume_gate_lines_range`:
    - 9 and 1001 are errors, and 10 and 1000 are valid;
    - 300, with `gate_output_lines: 200`, warns.
  - `test_config.py::test_agent_rules_entries_checked`. Each of these is one error at `project.agent_rules`:
    - `"/etc/x"`, `"../x"`, `"docs/../x"`, `"./.agentflow/r.md"`, `".AGENTFLOW/r.md"`, `".git/config"`, `"-x"`, `""`;
    - a bare string.
  - `test_config.py::test_memory_blockers`:
    - `@AGENTS.md` at the line start is a blocker, and so are `see @docs/x.md` mid-line and a bare `@README`;
    - `agent_rules: [AGENTS.md]` with a committed root `CLAUDE.md` gives a blocker;
    - `@dataclass` inside a ```` ``` ```` fence is not, and neither is `` `@x/y` `` in a code span or `me@example.com`;
    - committed `.claude/CLAUDE.md`, `.claude/rules/a.md` and `pkg/CLAUDE.md` give one reason each.
  - `test_config.py::test_agent_rules_warnings`:
    - `["AGENTS.md"]`, not committed, gives one warning;
    - `["CLAUDE.md"]`, not committed, gives none;
    - a committed `pkg/CLAUDE.md` gives one warning;
    - a 200_001-byte `CLAUDE.md` gives one warning;
    - with `run_brief: false`, none of these warn.
  - `test_config_edit.py::test_context_run_brief_and_gate_lines_editable`: the two fields round-trip with comments kept, and 5 is refused.
  - `test_config_edit.py::test_agent_rules_is_protected`.

- [ ] **Step 2: Run them and see them fail**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_config.py agentflow/tests/test_config_edit.py --import-mode=prepend -q -k "c2_keys or resume_gate_lines or agent_rules or memory_blockers or run_brief"`
Expected: FAIL.

- [ ] **Step 3: Implement the keys, checks, form fields, playbook lines and changelog line**

The playbook lines say:
- **`context.run_brief`:**
  - what goes into the brief: the rules and the repository's skill and `brief:` files as of the run's fork commit, the preamble and the result contract;
  - that it is Claude only, with no brief for sizers or the retro;
  - the fallbacks (Review Focus 3 and 4);
  - that a session keeps what it started with;
  - that while the brief carries the rules, run sessions also skip your personal `~/.claude/CLAUDE.md`.
- **`context.resume_gate_lines`:** 10 to 1000, in total across a fix turn's failing gates. A fresh session still gets the whole report.
- **`project.agent_rules`:** paths relative to the repository root, read from the run's fork commit.

- [ ] **Step 4: Run the tests again**

Run: the Step 2 command, then `bin/agentflow validate`.
Expected: PASS, and `0 warnings`.

- [ ] **Step 5: Lint and commit**

```bash
uvx ruff@0.17.4 check .
git add agentflow/config.py agentflow/gitops.py agentflow/defaults/config.yaml agentflow/config_edit.py docs/agentflow-playbook.md CHANGELOG.md agentflow/tests/test_config.py agentflow/tests/test_config_edit.py
git commit -m "Add context.run_brief, context.resume_gate_lines and project.agent_rules with their validate checks."
```

---

### Task 2: Move the step-specific lines out of the preamble and the result contract

The layout becomes `_preamble.md` + `_step_head.md` + the template + `_result.md`. The wording is the same as today except for the three lines in the table below. No brief is built yet.

**Files:**
- Create: `agentflow/prompts/_step_head.md`
- Modify: `agentflow/prompts/_preamble.md`, `agentflow/prompts/_result.md`
- Modify: `agentflow/config.py`:
  - `PROMPT_PLACEHOLDERS` gains `"in_brief"`;
  - a new `BRIEF_PLACEHOLDERS = frozenset({"run", "run_dir", "progress", "handoff", "questions", "module", "module_pkg", "lane", "slug"})`;
  - a `validate` warning, path `prompts/<file>`: while `run_brief` is true, a pack `_preamble.md` or `_result.md` holds a placeholder outside `BRIEF_PLACEHOLDERS`. The message names the placeholder and says such a pack's sessions get no brief.
- Modify: `agentflow/lanes.py`:
  - `build_prompt`;
  - `EMPTY_SLOT`;
  - `_result_partial`, which from this task on gives `RESULT_REMINDER` followed by the filled `_result.md`. The rewording drops the path from `_result.md`, and every resume turn (fix, reminder, continue, effort change) must still name it. Task 5 leaves `_result.md` out when the session has the brief;
  - a new ``RESULT_REMINDER = "When you finish, write your result JSON to `{result_file}` again (the runner removed the last one), as the \"When you finish\" section of your instructions says."``;
  - a new `STEP_HEAD = "_step_head.md"`;
  - a new `PREAMBLE_SLOT = re.compile(r"^\{(run_facts|code_map|lessons)\}[ \t]*(?:\r?\n|\Z)", re.MULTILINE)`. It is applied to the preamble unconditionally.
- Modify: `agentflow/pack_fix.py` (`RETIRED`: three entries)
- Modify: `CLAUDE.md`: the line on `prompts/*.md` says `_preamble.md`, `_step_head.md` and `_result.md` are shared by all of them.
- Modify: `CHANGELOG.md` (`Changed`)
- Test:
  - `agentflow/tests/test_agent_step.py`;
  - `agentflow/tests/test_pack_fix.py`;
  - `agentflow/tests/test_autonomy.py` (line 333);
  - `agentflow/tests/test_doubt_flow.py` (line 517 partitions on `## The step`);
  - `agentflow/tests/test_config.py`.

**Exact copy.** `_step_head.md`:

```
{run_facts}

{code_map}

{lessons}

## Step `{step_id}`

Your result file is `{result_file}`.{in_brief}
```

The `Retired` entries. Each `old` is an exact substring of the file at `HEAD`.

| id | file | old | new | detect |
|---|---|---|---|---|
| `preamble-names-the-step` | `_preamble.md` | ``You are running headless as step `{step_id}` of the agentflow run `{run}`. Nobody can answer you during the step.`` | ``You are running headless as one step of the agentflow run `{run}`; the step's prompt names it. Nobody can answer you during the step.`` | ``You are running headless as step `{step_id}` `` |
| `preamble-step-slots` | `_preamble.md` | `\n\n{run_facts}\n\n{code_map}\n\n{lessons}\n\n## The step` | `""` | `{lessons}\n\n## The step` |
| `result-names-the-file` | `_result.md` | ``Write your result as JSON to `{result_file}`, then stop. Use this exact path.`` | `Write your result as JSON to the step's result file (the path after "Your result file is" in the step's prompt), then stop. Use that exact path.` | ``Write your result as JSON to `{result_file}` `` |

- [ ] **Step 1: Write the failing tests**
  - `test_agent_step.py::test_step_head_follows_the_preamble`. These come in order:
    1. "Nobody can answer you";
    2. ``## Step `<id>` ``;
    3. the template's first line;
    4. `## When you finish`.

    The prompt also holds ``Your result file is `<path>` ``, and no `{in_brief}`.
  - `test_agent_step.py::test_old_preamble_override_does_not_double_the_slots`. Use `OLD_PREAMBLE_C2`, today's engine `_preamble.md` verbatim, and a CRLF copy of it. A run with one fact gives exactly one `## What this run already found`.
  - `test_pack_fix.py::test_preamble_and_result_c2_rewords_are_fixable`:
    - `OLD_PREAMBLE_C2` gives two fixable warnings naming `2.10`, and its fix is a deletion of the override (`after is None`), because the fixed text equals the engine file;
    - `OLD_RESULT_C2` gives one warning and the same deletion;
    - with a pack line appended to each file, the fix rewrites it to the new engine text plus that line.
  - `test_agent_step.py::test_resume_turns_still_name_the_result_file`: the reminder and continue prompts hold the backticked result path. This keeps `test_agent_step.py:368` and `:809` green.

    Name the constants with the `_C2` suffix. `OLD_PREAMBLE` already exists at `test_pack_fix.py:19`.
  - `test_config.py::test_brief_placeholder_warning`: a pack `_result.md` holding `{result_file}` warns while `run_brief` is true, and gives no warning with `run_brief: false`.
  - **Existing assertions:**
    - `test_autonomy.py:333` expects ``"- {FACT}\n\n## Step `"``.
    - **Every existing assertion on `## The step`:** find them with `grep -n '## The step' agentflow/tests`. They include `test_agent_step.py:1364–1428` and `1572–1590` (including `prompt.index` and `split` on it) and `test_doubt_flow.py:517`.
      - Point each one at ``## Step ` `` instead.
      - Where an assertion spans the preamble's end and the heading, split it into one assertion on the preamble's last line and one on the step head.
      - Keep the behaviour each asserts.

- [ ] **Step 2: Run them and see them fail**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_agent_step.py agentflow/tests/test_pack_fix.py agentflow/tests/test_autonomy.py agentflow/tests/test_doubt_flow.py agentflow/tests/test_config.py --import-mode=prepend -q`
Expected: the new tests FAIL.

- [ ] **Step 3: Implement the new prompt files, the placeholder sets, the warning, the `Retired` entries and the new join in `build_prompt`**

`build_prompt` joins `(preamble, step_head, body, result)`. It applies `EMPTY_SLOT` to the step head and `PREAMBLE_SLOT` to the preamble.

- [ ] **Step 4: Run the tests again**

Run: the Step 2 command, plus `test_feature_lane.py`, `test_refine.py`, `test_headless.py`, `test_lessons.py`, `test_design_lane.py`, `test_plug_and_play.py` and `test_portability.py`. Then run `bin/agentflow validate`.
Expected: PASS, and `0 warnings`.

- [ ] **Step 5: Lint and commit**

```bash
uvx ruff@0.17.4 check .
git add agentflow/prompts/ agentflow/config.py agentflow/lanes.py agentflow/pack_fix.py CLAUDE.md CHANGELOG.md agentflow/tests/
git commit -m "Move the step's id, result path and run notes from the preamble into a step head."
```

---

### Task 3: `brief.py` and a hardened blob reader

**Files:**
- Create: `agentflow/brief.py`
- Modify: `agentflow/gitops.py` (`read_blob`)
- Modify: `agentflow/conditions.py`: take the normalisation `skill_text` applies (BOM, `\r\n` and frontmatter) out into `normalise_skill(text: str) -> str`, with no change in behaviour.
- Modify: `.agentflow/config.yaml`: `overlays.security.when_paths` gains `agentflow/brief.py`. This is an interactive edit.
- Modify: `CLAUDE.md`: the security-overlay file list gains `brief.py`.
- Test: `agentflow/tests/test_brief.py`, and `agentflow/tests/test_gitops.py` (extend it if it exists, else create it)

**Interfaces:**
- Consumes: Task 1's `rules_path_problem`, `memory_blockers` and `gitops.tree_paths`.
- Produces in `gitops.py`: `read_blob(repo: Path, sha: str, path: str, limit: int) -> bytes | None`.
  - **How it runs git:** `subprocess.run(["git", *guards.HARDENED, ...], cwd=repo, capture_output=True, timeout=30)` in **bytes** mode, not `_git`.
  - **The checks before reading:**
    - `sha` must match `^[0-9a-f]{7,64}$`;
    - `path` must be relative, have no `..` segment, not start with `-`, and not start with `.git/`. `.agentflow/` is allowed here: only `project.agent_rules` refuses it, through `rules_path_problem`;
    - `ls-tree <sha> -- <path>` must show mode `100644` or `100755`, a blob. Mode `120000` (a symlink) is followed once: the target is joined to the link's folder, normalised, must pass the same path check, and must be a regular blob;
    - `cat-file -s <object id>` must be ≤ `limit`.
  - **The read:** `cat-file blob <object id>`. The content's git object hash must equal that id: SHA-1, or SHA-256 when `rev-parse --show-object-format` says so, of `blob <len>\0<data>`.
  - **Failures:** it returns None for anything missing, refused or over the limit, and on any `CalledProcessError`, `OSError` or `TimeoutExpired`.
- Produces in `brief.py`:
  - `BRIEF_FILE_MAX_BYTES`, `BRIEF_MAX_CHARS`.
  - `Doc`: `@dataclass(frozen=True) class Doc: heading: str; text: str`.
  - **`read_rules(checkout: Path, base_sha: str | None, files: Sequence[str]) -> tuple[list[Doc], bool]`.** It returns `(docs, carries)`. `carries` is False, with `[]`, when any of these holds:
    - `base_sha` is None or `files` is empty;
    - `tree_paths(checkout, base_sha)` is None;
    - `memory_blockers(...)` is non-empty;
    - a listed file is in the tree but `read_blob(..., BRIEF_FILE_MAX_BYTES)` gives None.

    A listed file that isn't in the tree is left out. Text is decoded as UTF-8 with `errors="replace"`, and `\r\n` becomes `\n`. The heading is `## <path>`.
  - **`read_skill(checkout: Path, base_sha: str | None, skills_dir: str, name: str, repo_owned: bool) -> Doc | None`.** When `repo_owned`, it reads `<skills_dir>/<name>/SKILL.md` with `read_blob` at `base_sha`. When the read gives None, the whole call gives None: the skill then stays in the user prompt, as today. When not `repo_owned`, it reads the bundled file. The text goes through `normalise_skill`, and the heading is ``## The `<name>` skill``.
  - **`read_brief_files(checkout: Path, base_sha: str | None, files: Sequence[str]) -> list[Doc] | None`.** Each file is read with `read_blob`. It returns None when any file can't be read, and those files then stay in the user prompt.
  - **`render(*, rules: list[Doc], base_sha: str, preamble: str, skill: list[Doc], result: str) -> str`.** A pure function. The order is:
    1. When there are rules: `# The repository's agent rules`, with the lead "These are the repository's agent rules as of the commit this run started from (`<sha12>`). Claude Code does not load `CLAUDE.md` files itself in this session: follow these." Then the rule docs.
    2. The preamble, verbatim.
    3. When there is a skill: the lead "The step's skill and brief files follow in full, so you already have them: do not load them." Then the docs.
    4. The result contract, verbatim.

    The text ends with one `\n`.
  - `file_name(phase: str, role: str, text: str) -> str` returns `f"{phase}-{role}-{sha256(text)[:12]}.md"`, with any character outside `[A-Za-z0-9_-]` in `phase` or `role` replaced by `_`.
  - `digest(text: str) -> str` returns the full SHA-256 hex.

- [ ] **Step 1: Write the failing tests**
  - `test_gitops.py`:
    - `test_read_blob_ignores_replace_refs`: after `git replace <commit> <forged>`, the original content is returned.
    - `test_read_blob_refuses_a_rewritten_loose_object`: overwrite the loose object file of the blob with other zlib-compressed content. The result is None.
    - `test_read_blob_reads_a_pack_prompt_override`: `.agentflow/prompts/_preamble.md` at a commit is read.
    - `test_read_blob_refuses_bad_input_and_size`:
      - `"../x"`, `"-x"`, the sha `"HEAD"`, a tree path and a blob over the limit all give None;
      - for the last of these, assert that no `cat-file blob` ran, using a recorder on `subprocess.run`.
    - `test_read_blob_follows_an_inner_symlink_once`: an in-repo target is read, and a target that is `../outside`, under `.agentflow/` or a link to a link gives None.
  - `test_brief.py`:
    - `test_render_is_byte_identical_for_the_same_parts`: equal parts give an equal string and an equal name, and a different skill gives a different name.
    - `test_render_orders_the_sections`: the rules come first, then the preamble, the skill and the result. With no rules there is no rules heading.
    - `test_rules_are_read_at_base_sha` (Review Focus 1): commit `CLAUDE.md` "A" at sha S, then commit "B", and write "C" uncommitted in the working tree. `read_rules(checkout, S, ["CLAUDE.md"])` gives "A".
    - `test_rules_do_not_carry_when_memory_blocks` (Review Focus 3): each `memory_blockers` case gives `([], False)`, and so do `base_sha=None` and `files=[]`. A committed tree with no `CLAUDE.md` gives `([], True)`.
    - `test_repo_skill_unreadable_stays_in_the_prompt`: a repository skill that is missing at `base_sha` gives None.

- [ ] **Step 2: Run them and see them fail**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_brief.py agentflow/tests/test_gitops.py --import-mode=prepend -q`
Expected: FAIL.

- [ ] **Step 3: Implement `read_blob`, `normalise_skill` and `brief.py`, then edit the overlay paths and `CLAUDE.md`**

- [ ] **Step 4: Run the tests again**

Run: the Step 2 command, `test_conditions.py` (or the file that tests `skill_text`), `test_portability.py`, and `bin/agentflow validate`.
Expected: PASS, and `0 warnings`.

- [ ] **Step 5: Lint and commit**

```bash
uvx ruff@0.17.4 check .
git add agentflow/brief.py agentflow/gitops.py agentflow/conditions.py agentflow/tests/ .agentflow/config.yaml CLAUDE.md
git commit -m "Add brief.py, which renders a run brief from the rules and skills at the run's fork commit."
```

---

### Task 4: Give each Claude session its pinned brief, and take that text out of the prompt

**Files:**
- Modify: `agentflow/lanes.py`:
  - **`_inlined_skill(cfg, step, checkout, body) -> str | None`** comes out of `build_prompt`, without the `continuation` check. `build_prompt` keeps applying `not step.continuation` before it inlines a skill into the user prompt. `build_brief` doesn't apply that check: a session's brief holds the skill whatever the task.
  - **`_StepRun`** gains `brief: dict | None` (the session's pin) and `brief_text: str | None` (its text, kept in memory).
  - **`build_prompt(..., brief: dict | None = None)`.**
  - **A new `build_brief(cfg, run, phase_name, step, role, body) -> tuple[str, dict] | str`**, returning `(text, pin)` or a skip reason.
  - **`Executor.__init__`** gains `briefs: bool = True`.
  - **`Executor._prompt(sr, resume=None) -> str`** keeps its signature, so `_rollover` and `_fresh_refine` are unchanged. When `resume is None`, it first decides the fresh session's brief and sets `sr.brief` and `sr.brief_text`; when `resume` is set, it leaves them alone. It then builds the prompt from `sr.brief`.
  - **`run_agent_step`** loads the pin of a resumed session into `sr.brief` before the first turn. That is `rec["brief"]` for the step's own session (restart, approval, refine, continue), or the shared step's `brief` for `takes_shared`.
  - **`_call`** makes sure the brief file exists right before `agent.run`. It writes `sr.brief_text` with `safeio.replace_file`, or rebuilds the text after a restart. It returns `_Called.brief_error: str | None` when it can't.
  - **`_agent_call`** passes `sr.brief`'s path and its `rules` flag on **every** call, so the direct `_Turn(..., sr.session)` sites need no change: `_continue_prompt`, `_reminder_prompt`, `_fix_prompt` and `_effort_change`.
  - **`_loop`** handles `brief_error` first (see Pinning).
  - **`dry_agent_argv`** builds the brief without writing it.
- Modify: `agentflow/retro.py:1064`, which passes `briefs=False`.
- Modify: `agentflow/agents.py`:
  - `AgentCall.system_prompt_file: Path | None = None` and `AgentCall.carries_rules: bool = False`;
  - `_CliAgent.takes_brief = False`, and `ClaudeAgent.takes_brief = True`;
  - `ClaudeAgent.argv` appends `["--append-system-prompt-file", str(path)]` immediately after the `--settings` pair;
  - **`CLAUDE_CODE_DISABLE_CLAUDE_MDS`:** `ClaudeAgent.env` sets it to `1` when `carries_rules`, overriding `call.env`. When not `carries_rules`, `env` removes it from its result and `env_drop` adds it, so neither the pack nor the inherited environment can set it.
- Modify: `agentflow/state.py`. The `agent_started` reducer copies the payload's `brief` (the pin, or None) onto `steps[key]["brief"]` on **every** call, fresh or resumed. A shared session's later task therefore carries the pin forward.
- Modify: `agentflow/tests/fake_agent.py`: it records the brief file's text as `system_prompt` in its call log, or None.
- Modify: `CHANGELOG.md` (`Added`)
- Test: `agentflow/tests/test_agents.py`, `agentflow/tests/test_agent_step.py`, `agentflow/tests/test_feature_lane.py`, `agentflow/tests/test_retro.py`

**Interfaces:**
- Consumes: Task 3's `read_rules`, `read_skill`, `read_brief_files`, `render`, `file_name` and `digest`; Task 2's `_step_head.md`, `PREAMBLE_SLOT` and `BRIEF_PLACEHOLDERS`; Task 1's keys.
- Produces:
  - **The pin**, a `dict` holding `{"path": str, "sha256": str, "rules": bool, "skill": bool, "files": bool, "phase": str, "role": str}`. `skill` and `files` say whether the skill and the `brief:` files are in the brief.
  - **On `agent_started`:** `brief: pin | None` on every call, and `brief_skipped: str` when a fresh session was refused one.
  - **`_result_partial`** keeps its signature, and Task 5 changes its body.

**Pinning: which brief a call gets.**
- **A fresh session** (`_prompt` with `resume is None`) gets a brief only when `self.briefs`, `cfg.context.run_brief` and `agent.takes_brief` all hold.
  - `build_brief` is called before the prompt text is built.
  - A skip reason gives `sr.brief = None`, and the reason goes on `agent_started` as `brief_skipped`.
- **A resumed session** uses the pin `run_agent_step` loaded, never the live config.
  - A pin of None, or no `brief` key at all (a session from before C2), means no brief.
- **Before every call with a pin**, `_call` checks the pin and writes the file:
  - The path must sit under `<run dir>/brief/`, a folder made with `safeio.ensure_dir`.
  - The name's digest part must be `sha256[:12]`.
  - The text must have that `digest`. The text is `sr.brief_text`, or after a restart `build_brief` rerun for the pinned phase and role.
- **When the text can't be had with the pinned digest, or writing raises `OSError`:** `_loop` doesn't stop the step. It emits `brief_replaced` with the key and the reason, and starts a **fresh session** the way `_rollover` does: `_prompt(sr)`, with a lead telling the agent to carry on from `progress.md`. That covers a template changed between a shared session's tasks, an engine upgrade mid-session, and a tampered file. Retrying can't loop on a stale pin, because the fresh session records a new one.
- **The prompt is built from the session's pin:**
  - **No pin:** the full layout of Task 2.
  - **A pin:** the preamble and `_result.md` are left out, and so are the skill or the `brief:` files when the pin says they are in the brief. In their place, the `{brief}` slot is preceded by ``The `<skill>` skill's full text and the step's brief files are in your system prompt: follow them, do not load them.`` The `Use the x skill` line says "is in your system prompt". `{in_brief}` stays `""`.

**What `build_brief` reads:** `checkout = pack.checkout_root(run.repo)` and `base = run.state.get("base_sha")`.
- **Rules:** `read_rules(checkout, base, cfg.project.agent_rules)`.
- **Preamble and result:**
  - Take the engine's `_preamble.md` and `_result.md`, unless the pack overrides them.
    - A pack override is read with `read_blob(checkout, base, ".agentflow/prompts/<name>", BRIEF_FILE_MAX_BYTES)`, so the brief never reads a live pack prompt.
    - An override that is present in the working tree but not readable at `base` returns `"prompt override not at base_sha"`.
  - Strip `EFFORT_BLOCK` and `PREAMBLE_SLOT` from the preamble.
  - If either holds a placeholder outside `BRIEF_PLACEHOLDERS`, return `"prompt override holds {<name>}"`.
  - Fill them with `fmt`, using only `BRIEF_PLACEHOLDERS` values: from `ctx.values` and `_values`.
- **Skill:** `read_skill(...)`, called when `_inlined_skill` would inline the step's skill. `repo_owned` means `pack.skill_file` finds a file that isn't bundled.
- **`brief:` files:** `read_brief_files(checkout, base, step.brief)`. `step.task_brief` always stays in the prompt.
- **Size:** a rendered text over `BRIEF_MAX_CHARS` returns `"brief over <n> chars"`.
- **Errors:** everything runs under `try`, and `OSError`, `ValueError` and `UnicodeError` become a skip reason.

**Known trade-off (not reworded in this slice):** templates that wrap `{brief}` in "This is its full text, so you already have it" (`fix.md`, `ci_fix.md`, `fix_findings.md`, `plan_fixes.md`, `eval_verify.md` and the pack's copies) now read "you already have it" followed by the system-prompt pointer. It is consistent, so it's left as is, and a later prompt pass can reword it with `Retired` entries.

- [ ] **Step 0: Probe the CLI on resume (interactive, under one cent)**
  1. Write `a.md`, holding "End every reply with the token ALPHA-1."
  2. Run `claude -p --model haiku --max-turns 1 --output-format json --append-system-prompt-file a.md "Say hi"` and note its `session_id`.
  3. Write `b.md`, holding "End every reply with the token BRAVO-2."
  4. Run the same command with `--resume <id> --append-system-prompt-file b.md "Say hi again"`.
  5. Check that the second result ends with `BRAVO-2`. That token was never in the history, so this proves the appended prompt is rebuilt from the flag on every invocation, which pinning relies on.

  Record the result in the PR description. If the check fails, stop and report it.

**Fixtures.** The `flow_repo` run that `test_agent_step.py` uses comes from `new_run()`, which leaves `state.base_sha` None, and its `.claude/skills` is a committed symlink to a folder outside the repository (`conftest.py:403`). So every brief test uses a helper, `brief_ready(flow_repo, run, files)`. It:
1. replaces the `.claude/skills` symlink with a real copy of its target folder;
2. writes `files` (such as `CLAUDE.md`);
3. commits all of it;
4. emits `run_updated` with `base_sha=<that commit>`.

No test may make a brief pass by reading the working tree. The step head shows `step.id` (``## Step `impl` ``, not the key).

- [ ] **Step 1: Write the failing tests**
  - `test_agents.py::test_claude_argv_and_env_with_a_brief`:
    - **With `carries_rules`:** the flag and path come right after the `--settings` value, and the env has `CLAUDE_CODE_DISABLE_CLAUDE_MDS=1`, even when `call.env` sets it to `0`.
    - **Without it:** the flag is still there, but the variable is absent from `env` and listed by `env_drop`, even when `call.env` sets it.
    - **No file:** neither appears.
    - **Codex:** neither ever appears.
  - `test_agent_step.py::test_brief_holds_the_stable_text_and_the_prompt_does_not` (C2 acceptance). The fixture run commits `CLAUDE.md` "RULE-MARKER" before the run starts. In its build step:
    - `system_prompt` holds "RULE-MARKER", "Nobody can answer you", "## When you finish" and the skill's text;
    - `argv[-1]` holds none of them, but holds ``## Step `<step.id>` `` and the result path.
  - `test_feature_lane.py::test_batched_tasks_share_one_byte_identical_brief` (C2 acceptance and Review Focus 2). A fake 5-task plan is above the python pack's `plan_is_small` max of 3, and uses `batch: {max_tasks: 3}`, so tasks 1–3 share a session through `task_next.md`. The runner ticks boxes between tasks. All three calls get the same path and equal `system_prompt`, and that includes task 3, which resumes the session task 2 continued.
  - `test_feature_lane.py::test_small_plan_shared_session_keeps_the_brief`: the same for a 2-task `small_plan` with `shared_session`.
  - `test_feature_lane.py::test_worktree_and_checkout_edits_never_reach_the_brief` (Review Focus 1):
    - fake task 1 appends "PLANTED" to the worktree's `CLAUDE.md`, which is not a forbidden path in the fixture pack;
    - the test appends "PLANTED" to the main checkout's `CLAUDE.md` and `.agentflow/prompts/_result.md` without committing them;
    - no later `system_prompt` holds "PLANTED".
  - `test_agent_step.py::test_resumes_keep_the_pinned_brief` (Review Focus 2). A step whose gates fail once:
    - the fix call carries the same path and text as the first call;
    - with `run_brief: false` written to the pack before the fix, the fix call still carries the brief;
    - a step that started with `run_brief: false` gets none on resume after the key turns true;
    - after the runner restarts mid-step, the resume reads the pin from state, and so does a resume after a human approves a stop;
    - each direct resume site (fix, reminder, continue, effort change) passes the flag;
    - deleting the brief file before a resume after a restart rebuilds it and goes on;
    - when a mocked `build_brief` gives a different digest after a restart, `brief_replaced` is emitted, the next call is a fresh session (no `--resume`) with a new pin, and the step finishes.
  - `test_agent_step.py::test_brief_never_holds_step_values`: two steps of the same phase and role, with different ids, result paths, run facts and lessons, get the same brief, and none of those values are in it.
  - `test_agent_step.py::test_brief_skipped_falls_back_to_the_full_prompt`: a pack `_result.md` holding `{result_file}`, and an `OSError` from `safeio.replace_file`, each give:
    - `brief: null` and a `brief_skipped` reason;
    - a full prompt holding "Nobody can answer you" and the result path;
    - no flag.
  - `test_agent_step.py::test_rules_fall_back_when_memory_blocks`: a committed `CLAUDE.md` holding `@AGENTS.md` gives a brief without the rules section, `carries_rules` False, and no `CLAUDE_CODE_DISABLE_CLAUDE_MDS` in the env.
  - `test_agent_step.py::test_no_brief_for_codex_sizers_and_dry_run`: Codex and sizer calls have no flag. `run --dry-run` prints the flag and writes no `brief/` dir.
  - `test_retro.py::test_retro_session_has_no_brief`.

- [ ] **Step 2: Run them and see them fail**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_agents.py agentflow/tests/test_agent_step.py agentflow/tests/test_feature_lane.py agentflow/tests/test_retro.py --import-mode=prepend -q -k "brief"`
Expected: FAIL.

- [ ] **Step 3: Implement `build_brief`, the pinning, the prompt changes, the adapter flags and env, and the reducer**

Tests that read the preamble or the result contract from `argv[-1]` now read `system_prompt`. An assertion that spans both channels is split, one per channel, keeping what it asserts.

- [ ] **Step 4: Run the tests again**

Run:
- `test_agents.py`, `test_agent_step.py`, `test_feature_lane.py`, `test_doubt_flow.py`, `test_autonomy.py`, `test_refine.py`;
- `test_headless.py`, `test_lessons.py`, `test_design_lane.py`, `test_plug_and_play.py`;
- `test_sizing.py`, `test_sizing_lane.py`, `test_retro.py`, `test_portability.py`;
- `bin/agentflow validate`, and `git status --short agentflow/tests/fixtures/explain`.

Expected: PASS, `0 warnings`, and no change to the golden files.

- [ ] **Step 5: Lint and commit**

```bash
uvx ruff@0.17.4 check .
git add agentflow/lanes.py agentflow/retro.py agentflow/agents.py agentflow/state.py agentflow/tests/ CHANGELOG.md
git commit -m "Give each Claude run session a pinned, cached system prompt with the rules, preamble, skill and result contract."
```

---

### Task 5: Short resume prompts (L3b)

**Files:**
- Modify: `agentflow/gates.py` (`resume_report`, `FAILURE_LINE`)
- Modify: `agentflow/lanes.py`:
  - `_fix_lead(sr, failed, attempt, excerpt: bool)`. The fix turns in `_loop` pass `excerpt=True`. The `fix_first` lead in `run_agent_step` passes `excerpt=turn.resume is not None`. A gate step that starts fresh gets the whole `failure_report`, and one that resumes a session (a refine round) gets the excerpt.
  - `_result_partial`, which leaves out `_result.md` when `sr.brief` is set (Task 2 added `RESULT_REMINDER`);
  - `_prompt`'s refine branch.
- Modify: `CHANGELOG.md` (`Changed`)
- Test: `agentflow/tests/test_gates.py`, `agentflow/tests/test_agent_step.py`, `agentflow/tests/test_refine.py`

**Interfaces:**
- Consumes: Task 4's `_Turn.brief`, `sr.brief` and `_prompt -> _Turn`; Task 1's `resume_gate_lines`.
- Produces: `gates.resume_report(failed: list[GateResult], max_lines: int) -> str`.

**The excerpt rule:**
- **Budget:** the first `max(1, max_lines // 10)` failed gates get `b = max(10, max_lines // min(n, max_lines // 10))` lines each. The others get their header, their log path and "(output not shown: read the log)". The kept lines never total more than `max_lines`.
- **Line length:** every kept line is cut to 300 chars, with a trailing `…`.
- **Short tails:** a tail of `b` lines or fewer is kept whole.
- **Long tails:** keep at most `b // 2` lines starting at the first `FAILURE_LINE` match (none when nothing matches), and fill the rest of `b` with the tail's last lines. Keep them in their original order with no duplicates, and put `… <k> lines cut …` where lines are skipped.
- **`FAILURE_LINE`** matches any of these:
  - `\b(FAILED|FAIL|ERROR|Error|error)\b`;
  - `^E\s`;
  - `AssertionError`;
  - `Traceback`;
  - `✗` or `✘`;
  - `^\s*-->\s`;
  - `^[^\s:]+:\d+(:\d+)?:\s`.
- **The block** per gate:
  1. `### <name> (<reason>)`;
  2. `Full log: <path>`, when there is one;
  3. the kept lines, fenced;
  4. ``(<k> of <m> lines shown; the full log has the rest)``, when lines were cut.

**The resume changes:**
1. **The result reminder:** `_result_partial(sr)` gives `RESULT_REMINDER` (Task 2), filled in. Only when `sr.brief is None` does the filled `_result.md` follow it. This covers the fix, reminder, continue and effort-change turns.
2. **A refine round that resumes its session:** the prompt is `sr.lead` (when set), then the refine template filled in, then the step's overlay skill lines (`overlays.skill_lines`), then `_result_partial`. There is no preamble, no step head and no "The step as it was first given" body. A fresh refine keeps today's full prompt.
3. **The shared-session next task:** it keeps the full task prompt, as Task 4 left it.

- [ ] **Step 1: Write the failing tests**
  - `test_gates.py`:
    - `test_resume_report_keeps_short_output_whole`: a 15-line ruff-style tail (`E731 …`, `  --> a.py:3:1`, …, `Found 1 error.`) with `max_lines=80` is kept whole, with no "lines shown" note.
    - `test_resume_report_keeps_failures_within_budget`: a 300-line pytest tail with the first failure at line 120 and the summary at lines 290–300, with `max_lines=80`. At most 80 lines are kept, including lines 120 and 300. The output has "of 300 lines shown" and the log path.
    - `test_resume_report_without_failure_lines_keeps_the_tail`.
    - `test_resume_report_total_never_exceeds_the_budget`:
      - 3 gates at 80 give at most 26 lines each;
      - 10 gates at 80 give 8 excerpts of 10 lines and 2 "read the log" headers;
      - the total is ≤ 80 in both cases.
    - `test_resume_report_cuts_long_lines`: a 5000-char line is kept as 300 chars plus `…`.
  - `test_agent_step.py`:
    - `test_fix_prompt_is_short_with_a_brief`: with the brief and a 300-line tail, the fix call's `argv[-1]` is under 8000 chars, holds the result path and doesn't hold "## When you finish". With `run_brief: false`, it holds both.
    - `test_fresh_gate_step_keeps_the_whole_report`: a `gates_first` verify step gets the whole tail when it starts fresh, and the excerpt when a refine round resumes its session.
  - `test_refine.py::test_refine_resume_sends_only_the_note`:
    - a resumed refine sends a prompt that holds the note and the reminder, and doesn't hold "The step as it was first given";
    - a security overlay that became active after the first round still has its skill line in the prompt;
    - a refine whose session is gone still gets the full prompt.

- [ ] **Step 2: Run them and see them fail**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_gates.py agentflow/tests/test_agent_step.py agentflow/tests/test_refine.py --import-mode=prepend -q -k "resume_report or short or whole_report or refine_resume"`
Expected: FAIL.

- [ ] **Step 3: Implement the excerpt, the reminder and the refine branch**

- [ ] **Step 4: Run the tests again**

Run: `test_gates.py`, `test_agent_step.py`, `test_refine.py`, `test_doubt_flow.py`, `test_feature_lane.py`, `test_bug_eval_security.py`, `test_headless.py` and `test_autonomy.py`.
Expected: PASS.

- [ ] **Step 5: Lint and commit**

```bash
uvx ruff@0.17.4 check .
git add agentflow/gates.py agentflow/lanes.py CHANGELOG.md agentflow/tests/
git commit -m "Send only what changed on a resume: a capped gate excerpt, the refine note and a one-line result reminder."
```

---

## Finishing the slice

- [ ] **Full suite, lint and validate.** Run the full suite, `uvx ruff@0.17.4 check .` and `bin/agentflow validate`. Expect green, `All checks passed!` and `0 warnings`.
- [ ] **Final review.** Review the branch with `security-and-hardening` and the `security-auditor` agent. Focus on the hardened reader, the pinning and what reaches the system prompt.
- [ ] **Real-run check (spec §9).** Run one feature run on agentflow itself. The PR description gives:
  - **The scorecard against the C0 baseline:** `prefix_write_tokens` (target ≤ 10K), `cache_write_share`, and `gate_first_pass:*` and review findings, no worse than C1 (spec §8).
  - **Resume prompt length:** one build session's, against §3.3's 10.5K median.
  - **One resumed implement call:** the brief's tokens come as a cache read on its first message, and there is no `CLAUDE.md` text in its first user message.
  - **The Step 0 probe result.**
