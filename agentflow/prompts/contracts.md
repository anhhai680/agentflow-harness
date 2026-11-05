Use the `api-and-interface-design` skill.

Goal: the API review of `docs/specs/SPEC-{module}.md`, before any plan is written. Make the routes, WebSocket messages, Pydantic models and the Python interfaces other modules call exact, in the spec's own contract sections.

In this module's own spec, read only the sections that define routes, messages, models and interfaces. Write your edits as you go, in small steps, and append to `{progress}` after each one, so a replaced session can carry on.

Read only what keeps names and shapes consistent:
- The modules this one depends on: their route, model and "Contracts this module provides" sections only.
- The modules that depend on this one: only the rows about `{module}` in their "Contracts this module needs from its dependencies" section (find it with `grep -n 'Contracts this module needs' docs/specs/SPEC-*.md`). That section is what they already expect from this module. Do not read the rest of their specs.
- The conventions earlier API reviews already decided, which you reuse and do not decide again: the one error body (`SPEC-platform` P11, R16), list envelopes and opaque cursors (`SPEC-inbox` I25), named permission booleans (I21), staff references (I22), idempotency keys (`SPEC-conversations` V21), and additive-only changes with expand-then-contract removals (ADR-015).

Keep customer-facing and staff-only response models separate. Staff-only data never appears on widget routes (`CLAUDE.md`, "Staff-only data stays off widget routes").
Edit the spec in place. Do not write code. If a route, model or interface needs a decision the docs do not settle, add it to "Open Questions" as a numbered question that continues the spec's numbering, so the grilling step can ask it. Do not guess.

Recording the review (the runner reads this):
- If you left questions, stop there. The grilling and apply steps that follow file the answers under `**Resolved (YYYY-MM-DD, API review, ...)**`.
- If you left none (the contracts were already exact, or the module has no routes and its interfaces are already fixed), add a `**Resolved (YYYY-MM-DD, API review)**` list at the end of "Open Questions" with one item saying what you checked and that nothing needed changing.
- If a contract change alters a decision an ADR covers, say so under "Open Questions" instead of editing the ADR.
