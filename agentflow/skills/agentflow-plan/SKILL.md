---
name: agentflow-plan
description: Use when a step asks for an implementation plan: split the approved spec into small tasks, each with files, test-first steps and a testable result.
---

# Plan: split the spec into tasks

The runner reads the plan file itself, so the format below is exact. A task is the unit one fresh session builds, reviews and commits.

## Format

Each task is a level-3 heading `### Task N: <title>` with N counting from 1. Under it, in this order:

- A line `**Depends on:** Task k, Task m` or `**Depends on:** none`. Give every task one: without it, the runner assumes the task depends on the one before.
- A `**Files:**` list, one line per file, each starting `- Create:`, `- Modify:` or `- Test:` and naming the path in backticks.
- Checkbox steps, each `- [ ] **Step k: <what to do>**`, followed by the details: the test to write, the command to run, the expected result.

```
### Task 2: Reject an empty name

**Depends on:** Task 1

**Files:**
- Modify: `app/names`
- Test: `tests/names_test`

- [ ] **Step 1: Write the failing test** for an empty name, and run it. Expected: it fails.
- [ ] **Step 2: Reject the empty name**, then run the test. Expected: it passes.
```

## Limits

- At most 8 steps and at most 8 files per task. Past that, split the task.
- A task ends with a result that can be tested on its own, with the exact test command and the expected output.
- Order the tasks so each depends only on earlier ones. Name every real dependency.
- A task that cannot be built until something outside this plan lands stays in the plan with no checkboxes. Its body starts with `> **Deferred (<reason>).**`.

## Rules

- Test first: a task's first step writes the failing test, and the next makes it pass.
- Say what each task changes and why, not how to type the code. Include a snippet only where a signature or exact text matters.
- Name tests by the runner's real command for the repository. Run only the tests a task touches. The runner's gates run everything else.
- If the spec is silent on a decision the design needs, do not invent it. Stop and ask.
- Write no code in this step.
