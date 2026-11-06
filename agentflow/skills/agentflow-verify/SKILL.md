---
name: agentflow-verify
description: Use when a step asks you to prove the work is done: read the gates' fresh output, run only what no gate covers, and claim nothing without evidence.
---

# Verify: evidence before claims

A claim that the work is done, fixed or passing needs fresh evidence from this exact tree.

## Steps

1. The runner has already run the gates and put their output in this prompt. Read it. For each gate, record its command, its result line and its counts.
2. Do not run those gate commands again. Their output is the evidence.
3. Run only what no gate covers: the plan's or the spec's own verification commands that are not among the gates. Name each one, with its result.
4. Compare the result against the spec's acceptance criteria and the plan's tasks, one by one. Note any that has no evidence.
5. Report each gate and each extra command with its real result.

## Rules

- Claim nothing you did not see. "Should pass" and "looks right" are not results.
- If something fails or could not be run, say which gate or command and why, and put it in the result's findings as blocking. A failure mentioned only in the summary slips through.
- Fix only the cause of a failure. Never weaken a quality rule, skip a test or add a suppression to make it pass.
- If you change a file, the runner runs every gate again after you, so keep changes to what a failure demands.
- Never commit.
