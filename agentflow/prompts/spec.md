Use the `spec-driven-development` skill.

Goal: draft `docs/specs/SPEC-{module}.md` for module `{module}`.

Read, by path:
- The `{module}` row in `docs/specs/CAPABILITY-MAP.md` (scope, dependencies, build order).
- The idea doc `docs/ideas/agentic-rag-customer-support.md`: `grep -n '^## '` first, then only the sections the module touches. Never read it whole.
- The ADRs in `docs/decisions/` that the module touches, and one finished spec such as `docs/specs/SPEC-platform.md` for the format.

If `{questions}` exists, it holds a human's answers to the idea questions for this module: treat them as settled and build the spec on them. Record any product-level answer as `CLAUDE.md` "Editing the idea doc" says.

Follow the format and the rules in `CLAUDE.md`. Put anything the docs do not settle under "Open Questions" instead of guessing.
Leave the status line as pending approval: only a human approves a spec.
