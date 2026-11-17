Use the `domain-modeling` skill.

Goal: record the decision "{topic}" in the spec of module `{module}` (`{spec}`).

A human answered a question that the spec did not settle while the run was building:

- Question: {question}
- Answer: {answer}
- Evidence: {evidence}

If the question is listed under the spec's "Open Questions", take it out of that list and file it under a `**Resolved (YYYY-MM-DD, <how it was decided>)**` heading at the end of that section, one `- **Qn — Topic.** Decision` line. That bold heading is how the runner tells answered questions from open ones: a plain `Resolved:` line is read as one more open question.

Read the spec section the evidence points at and write the decision into it, keeping cross-references consistent. If it is product-level (it changes the idea doc), say so in your result instead of editing the idea doc: that goes through the design lane.
Change only the spec. Do not commit: the runner commits.

## The `domain-modeling` skill

This is its full text, so you already have it: do not load it, follow it.

{brief}
