# Vendored skills

These project skills replace three Claude Code plugins for this repo. The plugins are turned off in `.claude/settings.local.json` so their duplicate skills and the superpowers SessionStart hook don't load (see "Why vendor" below). The order in which they're used: `CLAUDE.md` → "Agent workflow".

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
| `domain-modeling` | mattpocock `engineering/` | auto | — |
| `codebase-design` | mattpocock `engineering/` | auto | — |
| `improve-codebase-architecture` | mattpocock `engineering/` | user-only | Reads the design docs' decision logs as well as ADRs |
| `writing-for-agents` | mattpocock `productivity/` | user-only | — |
| `spec-driven-development` | agent-skills | auto | Plan/task phases handed to `writing-plans`; no `tasks/` folder |
| `api-and-interface-design` | agent-skills | auto | Deprecation bullet made generic (expand-then-contract) |
| `constraint-driven-development` | agent-skills | user-only | Commands and skill names mapped to the kept skills |
| `doubt-driven-development` | agent-skills | user-only | `/review` and dropped skills mapped to `requesting-code-review`, `systematic-debugging`, context7 |
| `security-and-hardening` | agent-skills | auto | — |
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
| `finishing-a-development-branch` | superpowers | auto | This repo's base branch, commit rules and verify gates |
| `next-step` | **local** (not vendored) | auto + `/next-step` | Canonical handoff map; lanes read `docs/plans/` and `docs/agentflow-*-proposal.md` |

Also vendored: `.claude/references/{security-checklist,orchestration-patterns,accessibility-checklist}.md` and `.claude/agents/security-auditor.md` from agent-skills, unchanged except for one dangling `docs/agents.md` link, which now points at `orchestration-patterns.md`. The skills' `../../references/` links resolve there.

Workspace note: `subagent-driven-development` and `executing-plans` keep their scratch ledger in `.superpowers/sdd/<plan>/`, and the script writes a `.gitignore` there.

## Re-sync with upstream

1. Update the plugin in a scratch project (or `/plugin update`) so the new version lands in `~/.claude/plugins/cache/`.
2. Diff each vendored skill against the new upstream folder, e.g. `diff -ru ~/.claude/plugins/cache/.../superpowers/<new>/skills/writing-plans .claude/skills/writing-plans`.
3. Take the upstream changes, then re-apply the edits listed above. Keep every `## Next step` footer, and check that no `superpowers:`, `agent-skills:` or `mattpocock-skills:` prefixes came back.
4. Bump the version in the table at the top.

Changing a stage's owner skill means updating `CLAUDE.md` → "Agent workflow", this file and `next-step` together.

Stage names, 2026-10-31: CLAUDE.md's Decide and Specify became Define, and Finish became Ship. No stage owner changed. `agentflow` runs the same stages as one workflow (`.agentflow/config.yaml`).

## Why vendor

- With all three plugins on, about 95 skills load, including 3 TDD, 4 code-review and 4 debugging skills. The model picks skills by their descriptions, so a precedence table can't stop the wrong one firing, and `skillOverrides` can't turn off plugin skills one by one.
- Turning the plugins off and copying one winner per stage into `.claude/skills/` removes every duplicate description and the superpowers SessionStart hook. `CLAUDE.md` replaces the hook as the router: it loads every session, survives compaction, and outranks skills.
- Heavy or rare skills are user-only (`skillOverrides: "user-invocable-only"`), so the agent doesn't load them on its own.

## Setup for another developer or machine

`.claude/settings.local.json` is git-ignored, so the plugin switches are per developer. Anyone with the plugins installed adds:

```json
"enabledPlugins": {
  "mattpocock-skills@claude-plugins-official": false,
  "superpowers@claude-plugins-official": false,
  "agent-skills@addy-agent-skills": false,
  "engineering@synced": false
},
"skillOverrides": {
  "improve-codebase-architecture": "user-invocable-only",
  "writing-for-agents": "user-invocable-only",
  "constraint-driven-development": "user-invocable-only",
  "doubt-driven-development": "user-invocable-only",
  "using-git-worktrees": "user-invocable-only"
}
```

To check it works: `/skills` in a new session lists the project skills and no `superpowers:`, `agent-skills:`, `mattpocock-skills:` or `engineering:` entries.
