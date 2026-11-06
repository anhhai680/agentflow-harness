Goal: apply the human's answers in `{questions}` to the spec of module `{module}` (`{spec}`).

For each answered question: update the spec section it touches, move the item from "Open Questions" to "Resolved", and keep cross-references consistent.
When the questions came from the API review (the `contracts` step), file them under one list headed `**Resolved (YYYY-MM-DD, API review, each as recommended)**` (drop "each as recommended" if any answer differs), one `- **Qn — Topic.** Decision (spec rows, requirements)` line per question, and record the decisions as new numbered decision rows in the spec. That heading is how the runner knows the review is done: without it the review runs again.
If an answer is a product-level decision, also follow what the repository's agent instructions say about editing the design docs: update every idea-doc section it touches (found with `grep -n '^## '`, never read whole), add a Decisions Log row, remove it from Open Questions, and add a decision record that supersedes an old one rather than editing it.
Do not invent answers for blank `Answer:` lines. Ask instead.

## The `{skill}` skill

This is its full text, so you already have it: do not load it, follow it. Its decision record format follows it.

{brief}
