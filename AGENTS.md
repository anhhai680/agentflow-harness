# AGENTS.md

Instructions for non-Claude agents (Codex) that agentflow runs in this repo.

- Follow `CLAUDE.md`; it applies to you as it does to Claude. Its invariants (stdlib + `pyyaml` only, no project strings in the engine, golden `explain` files never change, the four companions of a config key) are what the review checks.
- Skills live at `.claude/skills/<name>/SKILL.md`, with the engine's bundled fallbacks at `agentflow/skills/agentflow-<stage>/SKILL.md`. When a prompt says "use the `<skill>` skill", read that file and follow it.
- Never commit, push, amend or rebase in an agentflow run; the runner is the only thing that does. Don't move `HEAD` either.
- Never edit `.agentflow/**`, `.claude/**` or `agentflow/tests/fixtures/explain/**`: a diff there fails the step.
- Run the tests you touched with `uv run --no-project --with pyyaml --with pytest pytest <file> --import-mode=prepend -q`, and `uvx ruff@0.17.4 check .` before you finish.
- Write your result file at the path in `$AGENTFLOW_RESULT` before you finish.
