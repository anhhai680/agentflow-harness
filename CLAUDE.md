# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repository is

**agentflow** is a headless runner that drives Claude Code (or Codex) through a config-driven SDLC workflow (`define → plan → build → verify → review → ship`). It makes one `claude -p` call per step and ends with a PR that is ready for review. It is being turned into a project-agnostic **agent harness**.

- `agentflow/` is the **engine**: a stdlib + `pyyaml` Python package, with `flow.py` as its CLI. It must name no project.
- `.agentflow/` is this repository's **pack**: `config.yaml`, plus `prompts/` for any prompt that overrides the engine's file of the same name. A pack is config only, never code. Here the pack is agentflow working on itself, with only the `feature` and `bug` lanes.
- `AGENTS.md` holds the rules for non-Claude agents (Codex) in a run: follow this file, read skills from `.claude/skills/`, never commit.

Design docs (read them by section, using `grep -n '^## '`):
- `docs/agentflow-harness-proposal.md`: **the current design**, status `proposed`. §1b covers the engine/pack split, §1c plug and play, and §5 the rollout slices. Slice P (engine/pack separation) and slice PnP (plug and play: the installable tool, config layers, starters, bundled skills, `init` and `doctor`) have shipped, and so has slice LR (lessons and retros, §3.10 tier 5 and step 3b of §5).
- `docs/agentflow-playbook.md`: how to operate runs. §10 is the cheat sheet, §13 covers using agentflow in another repository, §14 is the pack reference, with one line per config key, and §15 covers lessons and retros.
- `docs/plans/`: implementation plans (`YYYY-MM-DD-agentflow-harness-<slice>.md`). Completed plans are records and aren't edited afterwards.

The quality bar is the pack's `gate_sets` plus the invariants below. A module's spec is its proposal, `docs/agentflow-<module>-proposal.md`.

## Commands

The runtime needs Python 3.12+ and `uv`. `bin/agentflow` runs `agentflow/flow.py` as a `uv` script, and PyYAML comes from the script header. To put it on your PATH, run `ln -s "$PWD/bin/agentflow" ~/.local/bin/agentflow`. `pyproject.toml` packages the engine as a tool with an `agentflow` console script, and its version must equal `pack.ENGINE_VERSION`.

```bash
# Tests. --no-project keeps uv from picking up any project file, and --import-mode=prepend is required
uv run --no-project --with pyyaml --with pytest pytest agentflow/tests --import-mode=prepend -q       # full suite: 25-60 min
uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_pack.py --import-mode=prepend -q
uv run --no-project --with pyyaml --with pytest pytest "agentflow/tests/test_pack.py::test_name" --import-mode=prepend -q

# The pack's gates (.agentflow/config.yaml): `portability` gates build, and `engine_tests` (the full suite) gates verify
uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_portability.py --import-mode=prepend -q

uv tool install --reinstall --from . agentflow # install (or refresh) the packaged tool from this checkout
bin/agentflow validate                         # run after every config edit; it must print "0 warnings"
bin/agentflow validate --fix [--dry-run]       # repair the warnings validate marks (fixable)
bin/agentflow explain <lane> --module <id>     # what a lane would run, with no agent calls
bin/agentflow run <lane> ... --dry-run         # prints the commands and creates nothing
bin/agentflow next | status | inbox | dashboard
bin/agentflow retro [--since YYYY-MM-DD] [--dry-run] # lessons from the finished runs as a draft PR (--dry-run: digest and prompt only)
bin/agentflow lessons [--json]                 # each lesson's target signal before and after the runs that carried it
bin/agentflow scorecard [--freeze]            # the finished runs' signals against a frozen baseline; conformance misses are the SLO
bin/agentflow doctor                           # agent CLIs and logins, gh, origin, Docker, the state dir, each gate's programs
bin/agentflow config show --effective [PREFIX] # every value after the layers merge, with the layer it came from
agentflow init [--starter NAME] [--baseline]   # in another repository: write a sparse pack (playbook §13)
```

The full suite is slow. While you work, run the test files you touched, and run the full suite before you finish.

## Changelog

`CHANGELOG.md` records every change a user can see, in [Keep a Changelog](https://keepachangelog.com) format. A change adds one line under `## [Unreleased]` (`Added`, `Changed`, `Fixed` or `Removed`). A release bumps `pyproject.toml` and `pack.ENGINE_VERSION` together, renames `[Unreleased]` to the new version and date, and opens a fresh `[Unreleased]`. The dashboard header shows `pack.ENGINE_VERSION`. `test_packaging` fails if the changelog has no entry for it.

A change that drops or rewords text in an engine prompt (`agentflow/prompts/*.md`) also adds a `Retired` entry to `agentflow/pack_fix.py`, so `validate` can find packs whose overrides still carry the old text and `validate --fix` can repair them (`docs/specs/agentflow-validate-fix-proposal.md`).

## Engine architecture

**Core rule:** the model never decides the next phase. The runner walks the pack's `phases:`. A lane (`feature | bug`, plus `eval | design` in the `python` fixture pack) picks which steps run, and `conditions:` decide what gets skipped. Conditions are evaluated from files and git, never by a model. The harness proposal relaxes this to "the model proposes, the harness disposes", but that work isn't built yet.

How a run flows:
- `flow.py` is the argparse CLI and runs on top of `pipeline.py`, the phase loop: it starts a run, then advances it phase by phase until a human is needed or the run ends. While it advances, `keepawake.py` holds a sleep inhibitor (`caffeinate` or `systemd-inhibit`) tied to the runner's process.
- `flow_control.py` decides which step of a phase runs next and what each kind of step does (agent, gate, checkpoint, `finish`, `ci`).
- `lanes.py` runs one agent step through its lifecycle: the prompt, then `agents.py` (argv/env for `claude -p` / `codex exec`, stream parsing), then the result file, then gates (`gates.py`) and the fix loop, then guards, then the commit. It also handles context rollover.
- Lane-specific behaviour lives in its own module. `feature_lane.py` handles plan tasks, per-task review and commits that tick `- [ ]` boxes. `bug_eval.py` holds the bug lane's red/green test expectations, and `intake.py` holds what a bug or eval run starts from (`--issue`, `--log`, `--task`). In `ship`, `sync_lane.py` makes `finish` merge `origin/<fork_from>` in, and `ci_lane.py` waits for CI, fixes failures and marks the PR ready. It never merges.
- `overlays.py` applies the security overlay, which adds skills, a review agent and an approval note when the `security_path` condition holds.
- `autonomy.py` holds what the runner resolves by itself after `plan`: resolver sessions for stops (whose `facts` are kept as run facts for every later session of the run), and refine rounds on blocking findings. It takes a question's recommended answers in every phase, except the kinds in `autonomy.human_question_kinds` (default `boundary`). `hitl.py` holds the `result.json` schema, the inbox, notifications, and `answer`/`approve`/`reject`. `run_control.py` holds `pause`/`resume`/`cancel`/`retry`/`archive`/`attach` for the CLI and the dashboard.

State and the pack:
- `run_env.py` builds the base environment of every agent and gate in a run; `defaults.env_inherit` decides what is dropped.
- `events.py` keeps the append-only `events.jsonl` and the run lock. `state.py` reduces it into `state.json`, and `Run` is the only writer of both. `metrics.py` and `views.py` are read-only passes over them.
- Runs and workspaces live in the state dir (`paths.state_dir`), which defaults to `~/.local/state/agentflow/<repo-id>/`, outside the repo. `workspace.py` gives each run the one directory an agent may write to outside its worktree.
- `pack.py` finds the pack, which is always the **main checkout's** `.agentflow/` and never a linked worktree's copy. It checks `version`/`engine` and resolves prompts and skills. Prompts come from the pack first and the engine's defaults second. Skills come from the repository's `project.skills_dir` first and the engine's bundled ones second. `config.py` loads the pack into typed dataclasses, resolves `@a.b.c` references and implements `validate`. `config_trust.py` decides which config a resumed run may use.
- `layers.py` merges the config layers before `config.py` resolves references, in this order:
  1. The engine defaults (`defaults/config.yaml`), which carry the whole default workflow.
  2. The starter the pack names (`starters/<name>/config.yaml`: `python-uv`, `node-pnpm`, `go` or `docs-only`), which holds gates only.
  3. The pack.
  4. The user file, which may hold personal keys only (`USER_KEYS`).

  So a pack states only what differs, and `config show --effective` prints each value's layer.
- **A step's skill is its brief.** A step with no `brief:` of its own gets its skill's `SKILL.md` as its first brief, read from the main checkout and never from a run's worktree. `skills/agentflow-<stage>/SKILL.md` are the bundled generic skills (define, plan, build, debug, verify, review, respond), so a repository with no skills dir still works.
- `conditions.py` holds the named condition checks (`file_exists`, `grep`, `section_items`, `status_line`, `changed_paths`, `plan_tasks`, …), the prompt placeholders, and `agentflow explain`. `sources.py` reads plans and specs. `work_source.py` and `module_queue.py` handle `next` and `queue`.
- `policy.py` merges the `policy.obligations` (the engine's `verified`, `reviewed`, `plan_approved` and `red_then_green`, plus the pack's; the `engine_floor` can't be removed) and reads `policy.enforce`. `conformance.py` audits them at `finish`, before the push, from the run's own `events.jsonl` and the tree about to be pushed; it runs a gate that has no passing result there, records `conformance_checked`, and in `block` mode stops the run with `conformance_failed`. `scorecard.py` freezes a baseline of the finished runs' signals (through `safeio`) and compares the later runs with it for `agentflow scorecard` and the dashboard's Metrics tab.

Lessons and retros (playbook §15):
- `signals.py` turns one run's events and metrics into signals keyed in a fixed vocabulary (`gate_first_pass:<gate>`, `fix_attempts:<phase>`, `ci_rounds`, …). A lesson's `target` is one of these keys. Pure functions: it reads no file and no clock.
- `lessons.py` parses and renders the two lessons files: the engine's `lessons.md` (`E-NNNN`, edited by hand) and the pack's `.agentflow/lessons.md` (`L-NNNN`). The pack file has a fixed path, sits under the engine floor, and is read only from the main checkout. `select` fills a step prompt's `{lessons}` slot, dropping lessons past `review_by` and cutting to `memory.lessons_max_tokens`. `effects` compares each lesson's target before and after the runs that carried it (`lessons_injected`) and gives a verdict.
- `retro.py` runs `agentflow retro` as the engine's `retro` pipeline, which is never a lane. It builds a deterministic digest of the runs that finished since the last finished retro, runs one read-only `retro_step` session, and checks every proposal against the run logs. It then commits only the lessons file (`gitops.commit_lessons`), pushes that commit by its sha and opens a draft PR. The checkout's git state and tree are compared with how they were before the session (`gitops.git_state`, `tree_state`), so a session that changed anything stops `blocked` before any commit or push. A retro run is never resumed: a stopped one is cancelled and run again.

Setting up a repository (these commands run with no pack, or before one exists):
- `detect.py` reads a repository **from its base branch** through git and never reads its working tree. The exception is a repository with no commits, which it reads from the working tree, with a note. It proposes the base branch, the starter, gates (CI workflow steps first, then `package.json` scripts, `pyproject.toml` tool sections and `Makefile` targets), docs folders and skills. The repository is untrusted input: nothing in it is run, and a malformed file becomes a note.
- `init.py` (`agentflow init`) renders the sparse pack as YAML data with `gates_confirmed: false` and the permission line, then runs `doctor`. Only with `--baseline` does it run the gates, once, on the base branch, in a throwaway worktree in the state dir. A failing gate becomes `baseline: red`, but a missing tool or a timeout is never red. It writes `.agentflow/config.yaml` once, exclusively, and writes nothing else in the repository.
- `doctor.py` (`agentflow doctor`) checks the agent CLIs and logins, `gh`, `origin`, Docker, the state dir and each gate's programs on `PATH`. Apart from looking programs up on `PATH`, the only command it runs is the agent CLI's own login probe (`auth status` / `login status`), for a bare `claude` or `codex` on `PATH`, with any args the pack configures.

Safety and the UI:
- `guards.py` refuses a step that touches `guardrails.forbidden_paths` or `.agentflow/**` (always refused), moves `HEAD`, or uses a user-only skill, and it rejects commit messages that carry a `guardrails.reject_trailers` trailer. Only the runner commits and pushes (`gitops.py`, `commit_checks.py`), and it never merges. `safeio.py` writes files without following symlinks.
- `dashboard.py`, `dashboard.html` and `backlog.py` make up the local Kanban board: a stdlib HTTP server on 127.0.0.1 that needs a token and sends SSE updates. `config_edit.py` backs its Settings page, which edits `config.yaml` while keeping comments and anchors. Its `_PROTECTED_KEYS` lists the keys the page may not change.
- `prompts/*.md` are the engine's default prompt templates. `_preamble.md` and `_result.md` are shared by all of them.

Tests (`agentflow/tests/`):
- `fake_agent.py` is a scripted stand-in for `claude -p` (`AGENTFLOW_FAKE_SCRIPT`), and `tests/bin/gh` is a fake `gh`. No test calls a real model or GitHub.
- `conftest.py` sets `XDG_STATE_HOME` to a tmp dir for every test.
- The suite runs on two toy fixture packs, `fixtures/packs/python` (the suite's main pack) and `fixtures/packs/node`. Each one is copied into its own temp git repo.
- `test_plug_and_play.py` copies three toy repositories with no pack into temp git repos: `fixtures/repos/python` (which has a CI workflow), `node`, and `docs` (which has no skills dir). On each one it runs `init`, `doctor`, `validate`, `explain` and `run --dry-run`.

## Invariants that changes must keep

- **Engine dependencies stay stdlib + `pyyaml>=6`.** Don't add a third-party import.
- **No project strings in the engine.** `test_portability.py` has a `DENYLIST` of project-specific names and paths. It fails if one appears in any engine file: the `.py` modules, `dashboard.html`, the default prompts, or the bundled skills and data. Project wording belongs in `.agentflow/prompts/`. `agentflow/tests/` is exempt.
- **The golden files `agentflow/tests/fixtures/explain/*.txt` never change.** They pin `agentflow explain` for every lane of the `python` fixture pack, so a refactor must leave them byte-identical. They are also a forbidden path in runs.
- **Every new or changed config key** ships in the same change with all four of these:
  - a Settings Form field or a `_PROTECTED_KEYS` entry (`config_edit.py`)
  - the matching `validate` checks
  - a `tests/test_config_edit.py` case
  - a line in playbook §14 "Pack reference"
- **Engine floor:** a pack without a non-empty `gate_sets.verify` is refused, `.agentflow/**` is always forbidden in a run, and user-only skills are refused. A pack can add obligations but can't remove these.
- Changes to `guards.py`, `agents.py`, `config_trust.py`, `safeio.py`, `workspace.py`, `pack.py`, `layers.py`, `detect.py`, `init.py` or `pack_fix.py` trigger the pack's security overlay (`overlays.security.when_paths`). Plan and review that work with the `security-and-hardening` skill and the `security-auditor` agent.

## Agent workflow

The workflow skills are vendored in `.claude/skills/` (see `.claude/skills/VENDORED.md`), and each stage has one owner skill. The pack's `skills.map` records the same mapping:

| Stage | Skill |
|---|---|
| Define | `grilling` (decisions), `spec-driven-development` (a proposal in `docs/agentflow-<module>-proposal.md`), `api-and-interface-design` (contracts), `domain-modeling` (recording decisions) |
| Plan | `writing-plans`, which writes `docs/plans/YYYY-MM-DD-<feature>.md` |
| Build | `subagent-driven-development`, or `executing-plans` for ≤ 3 tasks, with `test-driven-development` inside both |
| Verify | `verification-before-completion` |
| Review | the plan executor's final review, and `receiving-code-review` for feedback |
| Ship | `finishing-a-development-branch` (only when asked) |

The lanes:
- **feature** runs the whole table.
- **bug** runs `systematic-debugging`, then a failing regression test, then the fix, then verify.
- **design-doc** is interactive only, not a pack lane, and covers Define only: edit a proposal, resolving its Open Questions and keeping its sections consistent.
- **security** is an overlay that applies on the paths above.

`next-step` (or `/next-step`) reads the repo state and names the lane and the next skill. A proposal is approved by editing its status line from `Status: proposed` to `approved`, and the pack's `spec_is_approved` condition reads that line. The pack marks `improve-codebase-architecture`, `constraint-driven-development`, `doubt-driven-development`, `using-git-worktrees` and `writing-for-agents` as user-only skills: use them only when the user asks for them.

### Automated runs vs interactive sessions

`.agentflow/config.yaml` is the durable permission for commits, pushes, draft PRs and marking a PR ready, but **only inside agentflow runs**: `agentflow run | resume | queue`, or a run started from the dashboard Backlog. Those runs use `bypassPermissions`, but the deny rules, the runner's `--settings` denies, hooks and guards still bind them. Agents in a run never commit, push, amend or rebase, because the runner does that. Interactive sessions keep the rule "commit only when asked".

A commit message is one imperative sentence and ends with the `Co-Authored-By` trailer from `project.commit_trailer`. Never add a `Constraints-Approved-By` trailer, because only a human adds one. The pack's `reject_trailers` refuses it.
