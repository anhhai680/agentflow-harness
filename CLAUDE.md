# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repository is

**agentflow**, a headless runner that drives Claude Code (or Codex) through a config-driven SDLC workflow (`define → plan → build → verify → review → ship`), one step per `claude -p` call, ending in a PR that is ready for review. It was cut out of the product repository it was built in, and it is being turned into a project-agnostic **agent harness**.

- `agentflow/` is the **engine**: a stdlib + `pyyaml` Python package, with `flow.py` as its CLI. It must name no project.
- `.agentflow/` is this repository's **pack**: `config.yaml`, plus `prompts/` for any prompt that overrides the engine's file of the same name. A pack is config only, never code. Here it is agentflow working on itself, with the `feature` and `bug` lanes only.

Design docs (read by section, using `grep -n '^## '`):
- `docs/agentflow-harness-proposal.md`: **the current design**, status `proposed`. §1b covers the engine/pack split and §5 the rollout slices. Slice P (engine/pack separation) and slice PnP (plug and play: the installable tool, config layers, starters, bundled skills, `init` and `doctor`) have shipped.
- `docs/agentflow-playbook.md`: how to operate runs. §10 is the cheat sheet and §14 the pack reference, one line per config key.
- `docs/plans/`: implementation plans (`YYYY-MM-DD-agentflow-harness-<slice>.md`). Completed plans are records and aren't edited afterwards.

There is no `CONSTRAINTS.md`, so the quality bar is the pack's `gate_sets` plus the invariants below. There is no `docs/ideas/`, `docs/decisions/` or `docs/specs/`. A module's spec is its proposal, `docs/agentflow-<module>-proposal.md`.

## Commands

The runtime needs Python 3.12+ and `uv`. `bin/agentflow` runs `agentflow/flow.py` as a `uv` script, and PyYAML comes from the script header. To put it on your PATH: `ln -s "$PWD/bin/agentflow" ~/.local/bin/agentflow`. `pyproject.toml` packages the engine as a tool with an `agentflow` console script (`uv tool install --from . agentflow`).

```bash
# Tests. --no-project keeps uv from picking up any project file, and --import-mode=prepend is required
uv run --no-project --with pyyaml --with pytest pytest agentflow/tests --import-mode=prepend -q       # full suite: 25-60 min
uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_pack.py --import-mode=prepend -q
uv run --no-project --with pyyaml --with pytest pytest "agentflow/tests/test_pack.py::test_name" --import-mode=prepend -q

# The pack's gates (.agentflow/config.yaml): `portability` gates build, and `engine_tests` (the full suite) gates verify
uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_portability.py --import-mode=prepend -q

uv tool install --reinstall --from . agentflow # install (or refresh) the packaged tool from this checkout
bin/agentflow validate                         # after every config edit; it must print "0 warnings"
bin/agentflow explain <lane> --module <id>     # what a lane would run, with no agent calls
bin/agentflow run <lane> ... --dry-run         # prints the commands and creates nothing
bin/agentflow next | status | inbox | dashboard
bin/agentflow doctor                           # agent CLIs and logins, gh, origin, Docker, the state dir, each gate's programs
bin/agentflow config show --effective [PREFIX] # every value after the layers merge, with the layer it came from
agentflow init [--starter NAME] [--baseline]   # in another repository: write a sparse pack (playbook §13)
```

The full suite is slow, so run the test files you touched while you work and the full suite before you finish.

## Engine architecture

**Core rule:** the model never decides the next phase. The runner walks the pack's `phases:`. A lane (`feature | bug`, with `eval | design` in other packs) picks which steps run, and `conditions:` decide what gets skipped. Conditions are evaluated from files and git, never by a model. The harness proposal relaxes this to "the model proposes, the harness disposes", but that work isn't built yet.

How a run flows:
- `flow.py` is the argparse CLI and runs on top of `pipeline.py` (the phase loop: start a run, then advance phase by phase until a human is needed or the run ends).
- `flow_control.py` decides which step of a phase runs next and what each kind of step does (agent, gate, checkpoint, `finish`, `ci`).
- `lanes.py` runs one agent step through its lifecycle: prompt → `agents.py` (argv/env for `claude -p` / `codex exec`, stream parsing) → result file → gates and the fix loop → guards → commit. It also handles context rollover.
- Lane-specific behaviour lives in its own module: `feature_lane.py` (plan tasks, per-task review, commits that tick `- [ ]` boxes), `bug_eval.py` (the bug lane's red/green test expectations), `sync_lane.py` (`finish` merges `origin/<fork_from>` in) and `ci_lane.py` (waits for CI, fixes failures, marks the PR ready, never merges).
- `autonomy.py` holds what the runner resolves by itself after `plan`: resolver sessions for stops, and refine rounds on blocking findings. `hitl.py` holds the `result.json` schema, the inbox, notifications, and `answer`/`approve`/`reject`.

State and the pack:
- `events.py` keeps the append-only `events.jsonl` and the run lock. `state.py` reduces it into `state.json`, and `Run` is the only writer of both. `metrics.py` and `views.py` are read-only passes over them.
- Runs and workspaces live in the state dir (`paths.state_dir`), which defaults to `~/.local/state/agentflow/<repo-id>/`, outside the repo. `workspace.py` gives each run the one directory an agent may write to outside its worktree.
- `pack.py` finds the pack (always the **main checkout's** `.agentflow/`, never a linked worktree's copy), checks `version`/`engine`, and resolves prompts with the pack first and the engine's defaults second, and skills with the repository's `project.skills_dir` first and the engine's bundled ones second. `config.py` loads it into typed dataclasses, resolves `@a.b.c` references and implements `validate`. `config_trust.py` decides which config a resumed run may use.
- `layers.py` merges the config layers before `config.py` resolves references: the engine defaults (`defaults/config.yaml`, which carry the whole default workflow), the starter the pack names (`starters/<name>/config.yaml`: `python-uv`, `node-pnpm`, `go`, `docs-only`, gates only), the pack, then the user file, which may hold personal keys only (`USER_KEYS`). So a pack states only what differs, and `config show --effective` prints each value's layer.
- **A step's skill is its brief.** A step with no `brief:` of its own gets its skill's `SKILL.md` as its first brief, read from the main checkout, never a run's worktree. `skills/agentflow-<stage>/SKILL.md` are the bundled generic skills (define, plan, build, debug, verify, review, respond), so a repository with no skills dir works.
- `conditions.py` holds the named condition checks (`file_exists`, `grep`, `section_items`, `status_line`, `changed_paths`, `plan_tasks`, …), prompt placeholders, and `agentflow explain`. `sources.py` reads plans and specs. `work_source.py` and `module_queue.py` handle `next` and `queue`.

Setting up a repository (these run with no pack, or before one exists):
- `detect.py` reads a repository **from its base branch** through git, never its working tree, and proposes the base branch, starter, gates (CI workflow steps first, then `package.json` scripts, `pyproject.toml` tool sections and `Makefile` targets), docs folders and skills. The repository is untrusted input: nothing in it is run, and a malformed file becomes a note.
- `init.py` (`agentflow init`) renders the sparse pack as YAML data with `gates_confirmed: false` and the permission line, runs `doctor`, and with `--baseline` only runs the gates once on the base branch in a throwaway worktree in the state dir (a failing gate becomes `baseline: red`; a missing tool or timeout is never red). It writes `.agentflow/config.yaml` once, exclusively, and nothing else in the repository.
- `doctor.py` (`agentflow doctor`) checks the agent CLIs and logins, `gh`, `origin`, Docker, the state dir and each gate's programs on `PATH`. It never runs a pack's command.

Safety and the UI:
- `guards.py` refuses a step that touches `guardrails.forbidden_paths` or `.agentflow/**` (always), moves `HEAD`, or uses a user-only skill. Only the runner commits and pushes (`gitops.py`, `commit_checks.py`), and it never merges. `safeio.py` writes files without following symlinks.
- `dashboard.py` + `dashboard.html` + `backlog.py` make up the local Kanban board: a stdlib HTTP server on 127.0.0.1 that needs a token, with SSE updates. `config_edit.py` backs its Settings page, which edits `config.yaml` while keeping comments and anchors, and its `_PROTECTED_KEYS` lists what the page may not change.
- `prompts/*.md` are the engine's default prompt templates. `_preamble.md` and `_result.md` are shared by all of them.

Tests (`agentflow/tests/`):
- `fake_agent.py` is a scripted stand-in for `claude -p` (`AGENTFLOW_FAKE_SCRIPT`), and `tests/bin/gh` is a fake `gh`. No test calls a real model or GitHub.
- `conftest.py` sets `XDG_STATE_HOME` to a tmp dir for every test.
- The suite runs on two toy fixture packs, `fixtures/packs/python` (the suite's main pack) and `fixtures/packs/node`, each copied into its own temp git repo.
- `test_plug_and_play.py` copies three toy repositories with no pack (`fixtures/repos/python` with a CI workflow, `node`, and `docs` with no skills dir) into temp git repos and runs `init` → `doctor` → `validate` → `explain` → `run --dry-run` on each.

## Invariants that changes must keep

- **Engine dependencies stay stdlib + `pyyaml>=6`.** Don't add a third-party import.
- **No project strings in the engine.** `test_portability.py` has a denylist (`tivacare`, `CONSTRAINTS.md`, `src/`, `docs/specs`, `docs/ideas`, `CAPABILITY-MAP`, `SPEC-`, `floor_guard`, `webadmin`, `next-step`) that fails on any engine `.py`, `dashboard.html` or default prompt containing one of them. Project wording belongs in `.agentflow/prompts/`. `agentflow/tests/` is exempt.
- **The golden files `agentflow/tests/fixtures/explain/*.txt` never change.** They pin `agentflow explain` for every lane of the `python` pack, so a refactor must leave them byte-identical. They are also a forbidden path in runs.
- **Every new or changed config key** ships in the same change with all four of: a Settings Form field or a `_PROTECTED_KEYS` entry (`config_edit.py`), the matching `validate` checks, a `tests/test_config_edit.py` case, and a line in playbook §14 "Pack reference".
- **Engine floor:** a pack without a non-empty `gate_sets.verify` is refused, `.agentflow/**` is always forbidden in a run, and user-only skills are refused. A pack can add obligations but can't remove these.
- Changes to `guards.py`, `agents.py`, `config_trust.py`, `safeio.py`, `workspace.py`, `pack.py`, `layers.py`, `detect.py` or `init.py` trigger the pack's security overlay. Plan and review that work with `security-and-hardening` and the `security-auditor` agent.

## Agent workflow

The workflow skills are vendored in `.claude/skills/` (see `.claude/skills/VENDORED.md`), and each stage has one owner skill. The pack's `skills.map` records the same mapping:

| Stage | Skill |
|---|---|
| Define | `grilling` (decisions), `spec-driven-development` (a proposal in `docs/agentflow-<module>-proposal.md`), `api-and-interface-design` (contracts), `domain-modeling` (recording decisions) |
| Plan | `writing-plans` → `docs/plans/YYYY-MM-DD-<feature>.md` |
| Build | `subagent-driven-development`, or `executing-plans` for ≤ 3 tasks, with `test-driven-development` inside both |
| Verify | `verification-before-completion` |
| Review | the plan executor's final review; `receiving-code-review` for feedback |
| Ship | `finishing-a-development-branch` (only when asked) |

Lanes: **feature** (the whole table), **bug** (`systematic-debugging` → a failing regression test → fix → verify), **design-doc** (Define only: edit a proposal, resolving its Open Questions and keeping its sections consistent), and a **security** overlay on the pack's `overlays.security.when_paths` (see Invariants). `next-step` (or `/next-step`) reads the repo state and names the lane and next skill. A proposal is approved by editing its status line (`Status: proposed` → `approved`), and the pack's `spec_is_approved` condition reads that line. Use `improve-codebase-architecture`, `constraint-driven-development`, `doubt-driven-development`, `using-git-worktrees` and `writing-for-agents` only when the user asks for them.

### Automated runs vs interactive sessions

`.agentflow/config.yaml` is the durable permission for commits, pushes, draft PRs and marking a PR ready, but **only inside agentflow runs** (`agentflow run | resume | queue`, or a run started from the dashboard Backlog). Those runs use `bypassPermissions`, and the deny rules, the runner's `--settings` denies, hooks and guards still bind them. Agents in a run never commit, push, amend or rebase, because the runner does that (see `AGENTS.md`). Interactive sessions keep "commit only when asked".

Commit messages are one imperative sentence, ending with the `Co-Authored-By` trailer from `project.commit_trailer`. Never add a `Constraints-Approved-By` trailer: only a human does that.
