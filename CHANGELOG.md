# Changelog

All notable changes to agentflow harness are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/).

The version is `pack.ENGINE_VERSION`; `pyproject.toml` must carry the same number (`test_packaging` checks it).

## [Unreleased]

### Fixed

- The dashboard's dialogs show why a request was refused: a New task the server rejects (a 400 or 409) now names the bad field inside the open dialog, where before the message was hidden under the modal and only the browser console showed `400 (Bad Request)`.

## [2.2] - 2026-11-09

### Added

- Lessons and retros: every step's prompt carries the engine's and the pack's lessons (`agentflow/lessons.md`, `.agentflow/lessons.md`) through the new `{lessons}` slot, `agentflow lessons` shows each lesson's target signal before and after the runs that carried it, and `agentflow retro` (the `retro_step` session, run by `agentflow queue` when one is due once the first retro, run by hand, has finished) proposes lessons from the finished runs as a draft PR that changes only `.agentflow/lessons.md`. New keys `memory.lessons_max_tokens`, `memory.digest_tokens` and `memory.retro_every_days`; the dashboard gains a Lessons tab and a "Retro due" banner.

### Changed

- Run events carry more of what went wrong: `gate_result.tail`, `hitl_raised.extra`, `ci_round.failed` and `ci_round.tail`, and `step_retry.reason`, which the new `agentflow retry --reason` sets.
- The runner's `push` runs no repository hooks (a `pre-push` hook no longer runs) and pushes an explicit same-name refspec in every lane, so a `remote.origin.push` setting cannot send it elsewhere; a retro pushes its lessons commit by its sha.

## [2.1] - 2026-11-08

### Added

- A step's result may carry `code_map`: up to 8 lines naming existing code (`path:line`, signature, use). The run keeps the newest 40 for every later session.
- `gates.<name>.agent_check`: a gate the implementer runs itself before it finishes, so a lint failure is fixed in the same session instead of in a gate-fix round.
- `context.max_compactions` (default 3): a session that has auto-compacted that often is replaced by a fresh one seeded from `progress.md`. `validate` now checks the `context` limits.
- `agentflow init` finds a `docs/specs` folder (so the Backlog and "+ New task" are written for it), and sets `project.docs.spec_file` when most of its files are named `PREFIX-<module>.md`.
- The dashboard header shows the engine version (`v2.0`), served by the new `GET /api/info` route.
- This changelog, with a test that it has an entry for the current version.

### Changed

- The build prompts list a step's `agent_check` gates for the implementer to run before it finishes; with none, they read as before.
- Every session's prompt shows the run's code map, and the build prompts stop sending a fresh session back to the plan.
- The bundled plan skill asks for an `**Interfaces:**` list per task, naming the existing code the task calls by `path:line` and signature.

### Fixed

- Auto-compact now fires at 60% of a 200k window instead of 60% of the models' 1M window (600k tokens, so it never fired before the runner's 80% rollover). The runner sets `CLAUDE_CODE_AUTO_COMPACT_WINDOW` from `context.context_window_tokens`, whose default is now 200000 instead of `auto`.

## [2.0] - 2026-11-08

The first changelogged release. It sums up the work from 2026-11-05 to 2026-11-08.

### Added

- Engine and pack split: the engine is project-agnostic and a repository's `.agentflow/` pack holds only config and prompt overrides.
- Installable tool: `pyproject.toml` with an `agentflow` console script, plus bundled generic stage skills.
- Config layers (engine defaults, starter, pack, personal user file) with null deletion and a protected-settings hold; `config show --effective` and the Settings page show each value's layer.
- Starters for python-uv, node-pnpm, go and docs-only repositories.
- `agentflow init` (detects the base branch, starter, gates, docs and skills and writes a sparse pack) and `agentflow doctor`.
- A `specs` work source that lists one module per spec file, with Backlog start, finished-module detection from the base branch on origin, and `agentflow backlog done|undone`.
- A soft run budget that only notifies and a hard run budget that stops the run for approval.
- Resolver sessions and refine rounds that take recommended answers in every phase except the human-only question kinds (default `boundary`).
- `SETUP.md`, an agent-driven setup guide with an update flow, and a GitHub Actions CI that runs ruff and the test suite.

### Changed

- Worktrees live in the state dir by default, and config layer changes are noticed during and across a run.
- A refine round redoes only the task whose stop was rejected, and skips the review of a task whose refine changed nothing.
- Agents and gates no longer see leaked environment variables (`defaults.env_inherit`).
- A build stops before its first task when a plan task names a forbidden path.

### Fixed

- `cancel` falls back when a run's recorded config no longer loads.
- `validate` reads an unreadable permission settings file as a warning instead of raising.
- Format characters are stripped from model text shown in the pull request body.
- A rollover test no longer races the adapter's SIGTERM.

[Unreleased]: https://github.com/anhhai680/agentflow-harness/compare/v2.0...HEAD
[2.0]: https://github.com/anhhai680/agentflow-harness/releases/tag/v2.0
