---
name: agentflow-verify
description: Use when a step asks you to prove the work is done: read the gates' fresh output, run only what no gate covers, debug what fails, and claim nothing without evidence.
---

# Verify: prove it works

A claim that the work is done, fixed or passing needs fresh evidence from this exact tree. Verify tests and debugs. It does not judge the code against the spec: review does that.

## Steps

1. The runner has already run the gates and put their output in this prompt. Read it. For each gate, record its command, its result line and its counts.
2. Do not run those gate commands again. Their output is the evidence.
3. For each acceptance criterion in the spec and each verification command in the plan, name the test or command that ran and its result.
4. Run what no gate covers: the plan's or the spec's own verification commands that are not among the gates. Name each one, with its result.
5. List what could not run as not verified, with the reason. A criterion nothing ran for is never met.
6. Report each gate, each extra command and each criterion with its real result.

## When a gate fails

1. Stop the line. Do not go on to the next check as if nothing happened.
2. Reproduce the failure. Run the failing command or test on its own and see it fail.
3. Find where it comes from. Read the error, trace it back to the first wrong value, and test one hypothesis at a time.
4. Fix the cause, not the symptom, and guard it with a test that fails without the fix.
5. Name the cause as in the branch or in the environment (a clock, a database, a port, a flaky test the branch does not touch). An environment failure you cannot fix in the worktree is blocking, with that reason.
6. The runner runs every gate again after you. Keep changes to what the failure demands.

## Rules

- Claim nothing you did not see. "Should pass" and "looks right" are not results.
- Never judge the code by reading it: review does that.
- If something fails or could not be run, say which gate or command and why, and put it in the result's findings as blocking. A failure mentioned only in the summary slips through.
- Fix only the cause of a failure. Never weaken a quality rule, skip a test or add a suppression to make it pass.
- If you change a file, the runner runs every gate again after you, so keep changes to what a failure demands.
- Never commit.
