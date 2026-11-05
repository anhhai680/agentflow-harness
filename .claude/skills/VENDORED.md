# Vendored skills

These project skills replace three Claude Code plugins for this repo. The plugins are turned off in `.claude/settings.local.json` so their duplicate skills and the superpowers SessionStart hook don't load. Why: `docs/agent-orchestration-guide-v3.md`. The order in which they're used: `CLAUDE.md` → "Agent workflow".

All three sources are MIT-licensed; their notices are in `LICENSES/`.

| Source | Version | Upstream path in the plugin cache |
|---|---|---|
| obra/superpowers | 7.0.0 | `~/.claude/plugins/cache/claude-plugins-official/superpowers/7.0.0/skills/` |
| addyosmani/agent-skills | 0.6.12 | `~/.claude/plugins/cache/addy-agent-skills/agent-skills/0.6.12/skills/` |
| mattpocock/skills | 1.3.1 | `~/.claude/plugins/cache/claude-plugins-official/mattpocock-skills/1.3.1/skills/` |

## Skills

| Skill | Source | Mode | Local edits (besides the `## Next step` footer every skill has) |
|---|---|---|---|
| `grilling` | mattpocock `productivity/` | auto | — |
| `domain-modeling` | mattpocock `engineering/` | auto | File structure → `docs/decisions/` + idea doc first; `ADR-FORMAT.md` rewritten to the repo's ADR format; bilingual rule in `GLOSSARY-FORMAT.md`; read the idea doc by section, never whole |
| `codebase-design` | mattpocock `engineering/` | auto | — |
| `improve-codebase-architecture` | mattpocock `engineering/` | user-only | ADR path → `docs/decisions/` |
| `writing-for-agents` | mattpocock `productivity/` | user-only | — |
| `spec-driven-development` | agent-skills | auto | Plan/task phases handed to `writing-plans`; no `tasks/` folder |
| `api-and-interface-design` | agent-skills | auto | Deprecation pointer → ADR-015 forward-only migrations |
| `constraint-driven-development` | agent-skills | user-only | Commands and skill names mapped to the kept skills |
| `doubt-driven-development` | agent-skills | user-only | `/review` and dropped skills mapped to `requesting-code-review`, `systematic-debugging`, context7 |
| `security-and-hardening` | agent-skills | auto | Observability pointer → CLAUDE.md PII rule |
| `frontend-ui-engineering` | agent-skills | auto | — |
| `writing-plans` | superpowers | auto | Saves to `docs/plans/`; task = one testable slice/commit; recommend inline for ≤ 3 tasks; a new package's File Structure uses `codebase-design` |
| `executing-plans` | superpowers | auto | `superpowers:` prefixes removed; dropped `using-superpowers` reference |
| `subagent-driven-development` | superpowers | auto | Prefixes removed; "brainstorm first" → `spec-driven-development` |
| `test-driven-development` | superpowers | auto | `writing-skills` → `writing-for-agents` |
| `systematic-debugging` | superpowers | auto | Prefixes removed; dropped `CREATION-LOG.md` and `test-*.md` (authoring artifacts) |
| `verification-before-completion` | superpowers | auto | — |
| `requesting-code-review` | superpowers | auto | Plan path example → `docs/plans/` |
| `receiving-code-review` | superpowers | auto | — |
| `using-git-worktrees` | superpowers | user-only | — |
| `finishing-a-development-branch` | superpowers | auto | Tiva Care base branch and commit rules |
| `next-step` | **local** (not vendored) | auto + `/next-step` | Canonical handoff map |

Also vendored: `.claude/references/{security-checklist,orchestration-patterns,accessibility-checklist}.md` and `.claude/agents/security-auditor.md` from agent-skills, unchanged except for one dangling `docs/agents.md` link, which now points at `orchestration-patterns.md`. The skills' `../../references/` links resolve there.

Workspace note: `subagent-driven-development` and `executing-plans` keep their scratch ledger in `.superpowers/sdd/<plan>/`, and the script writes a `.gitignore` there.

## Re-sync with upstream

1. Update the plugin in a scratch project (or `/plugin update`) so the new version lands in `~/.claude/plugins/cache/`.
2. Diff each vendored skill against the new upstream folder, e.g. `diff -ru ~/.claude/plugins/cache/.../superpowers/<new>/skills/writing-plans .claude/skills/writing-plans`.
3. Take the upstream changes, then re-apply the edits listed above. Keep every `## Next step` footer, and check that no `superpowers:`, `agent-skills:` or `mattpocock-skills:` prefixes came back.
4. Bump the version in the table at the top.

Changing a stage's owner skill means updating `CLAUDE.md` → "Agent workflow", this file and `next-step` together.

Stage names, 2026-10-31: CLAUDE.md's Decide and Specify became Define, and Finish became Ship. No stage owner changed. `agentflow` runs the same stages as one workflow (`devtools/agentflow/config.yaml`).
