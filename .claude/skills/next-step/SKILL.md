---
name: next-step
description: Use when unsure which workflow skill comes next, when resuming work in a new session, or when the user types /next-step. Reads the repo state and names the current lane, the stage, and one next skill.
---

# Next Step

<!-- Tiva Care skill (not vendored). Canonical copy of the handoff map; each skill's "## Next step" footer repeats only its own row. -->

Work out where the work stands and name **one** next skill. Don't run it: say it and let the developer (or your next turn) decide.

## 1. Read the state

Run these and read nothing else unless one points somewhere:

```bash
git status --short
git branch --show-current
ls -t docs/plans/*.md 2>/dev/null | head -1
ls -t docs/specs/*.md 2>/dev/null | head -3
```

## 2. Pick the lane and stage (first match wins)

1. An error or failing test is in the conversation → **bug** lane → `systematic-debugging`.
2. The change touches prompts, model choice, chunking or embeddings → **eval** lane → Verify by re-running the eval suite.
3. The newest plan in `docs/plans/` has unchecked `- [ ]` tasks → **feature** lane, Build → continue with the plan's execution method (`executing-plans` or `subagent-driven-development`).
4. The newest plan is fully checked and its branch isn't merged → **feature** lane, Verify → `verification-before-completion`, then Ship with `finishing-a-development-branch`.
5. A spec in `docs/specs/` was approved but has no plan in `docs/plans/` → **feature** lane → `writing-plans`.
6. Uncommitted changes only under `docs/ideas/`, `docs/decisions/` or `GLOSSARY.md` → **design-doc** lane → finish the idea-doc edits (`CLAUDE.md` → "Editing the idea doc"), then commit when the user asks.
7. Nothing in flight → ask what the user wants to do. Use `grilling` for a decision and `spec-driven-development` for something to build (both are Define).

If a security path is involved (Entra/IS4 token validation, `customer_code` injection, `visibility = public` filtering, staff-only data on widget routes, attachments, redaction), add the **security** lane: `security-and-hardening`, and offer `/doubt-driven-development` before Build.

## 3. Answer in this shape

```
Lane: <lane> · Stage: <stage>
Evidence: <the one or two facts from step 1 that decided it>
Next: <skill or /command> — <reason>
```

## Handoff map

| After | Next (condition) |
|---|---|
| `grilling` | `domain-modeling` (term resolved, or the decision is hard to reverse) · `writing-plans` (a candidate from `/improve-codebase-architecture`) · `spec-driven-development` (ready to build) |
| `domain-modeling` | `spec-driven-development` (building) · otherwise the doc lane is done, commit when asked |
| `codebase-design` | `grilling` (choosing candidates) · `spec-driven-development` / `writing-plans` |
| `/improve-codebase-architecture` | `grilling` on the picked candidate → `writing-plans` |
| `spec-driven-development` | `api-and-interface-design` (routes, WebSocket, contracts) · `writing-plans` |
| `api-and-interface-design` | `domain-modeling` (changes an ADR'd decision) · `writing-plans` |
| `writing-plans` | `/doubt-driven-development` (security path) · `/using-git-worktrees` (optional) · `executing-plans` (≤ 3 tasks) or `subagent-driven-development` |
| `executing-plans` / `subagent-driven-development` | `verification-before-completion` → `finishing-a-development-branch` (their final review is the branch review) |
| `test-driven-development` | the next task · `verification-before-completion` (all done) |
| `systematic-debugging` | `test-driven-development` (regression test) → `verification-before-completion` |
| `frontend-ui-engineering` | `verification-before-completion` |
| `verification-before-completion` | eval delta (eval lane) · the next task · `finishing-a-development-branch` (from a plan) · `requesting-code-review` (outside a plan) |
| `security-and-hardening` / `/doubt-driven-development` | back to the calling stage · `security-auditor` agent at Review |
| `requesting-code-review` | `receiving-code-review` |
| `receiving-code-review` | `test-driven-development` → `verification-before-completion` → `finishing-a-development-branch` |
| `finishing-a-development-branch` | done · `/improve-codebase-architecture` at a milestone |
| `/constraint-driven-development` | back to the calling stage |
| `/using-git-worktrees` | the chosen execution method |
