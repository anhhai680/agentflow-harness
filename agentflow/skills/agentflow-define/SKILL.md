---
name: agentflow-define
description: Use when a step asks for a spec: write the spec file at the path the step names, with its scope, acceptance criteria and open questions.
---

# Define: write the spec

A spec says what will be built and how anyone can tell it is done. It does not say how to code it.

## Steps

1. Read what the step points you to: the idea or request, the decisions already made, and one finished spec for the format if the repository has one. Read only the sections you need.
2. Write the spec at the output path the step names, and nowhere else.
3. Put anything the documents do not settle under `## Open Questions` instead of guessing. A human answers them.
4. Leave the status line as pending approval. Only a human approves a spec.

## What the spec holds

Use these headings, in this order:

- `# <Title>` and a line `Status: proposed`.
- `## Objective`: one short paragraph. Who needs this, and what changes for them.
- `## Scope`: what is built. A list of behaviours or interfaces, each one testable.
- `## Out of scope`: what a reader might expect but will not get, so nobody builds it by accident.
- `## Acceptance criteria`: a checklist. Each item is a fact an automated test or a person can check, with the expected result stated. No "works well" or "is fast".
- `## Open Questions`: one question per item, with the options you see and the one you recommend. Write "None" if there are none.

## Rules

- Be specific. Name inputs, outputs, error cases and limits.
- Do not write code, and do not write the implementation plan. A separate step does that.
- Do not repeat what the repository's own documents already say. Link to them by path.
- If the request conflicts with an existing decision, stop and ask. Do not choose a side.
