# AGENTS.md

Instructions for non-Claude agents (Codex) that agentflow runs in this repo.

- Follow `CLAUDE.md` and `CONSTRAINTS.md`; they apply to you as they do to Claude.
- Skills live at `.claude/skills/<name>/SKILL.md`. When a prompt says "use the `<skill>` skill", read that file and follow it.
- Never commit, push, amend or rebase in an agentflow run; the runner is the only thing that does.
- Write your result file at the path in `$AGENTFLOW_RESULT` before you finish.
