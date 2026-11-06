---
name: agentflow-respond
description: Use when a step hands you review findings to act on: check each one against the code, fix the ones that hold, and say why for any that do not.
---

# Respond: act on review findings with evidence

A finding is a claim about the code. Test the claim before you act on it.

## Steps

1. Read all the findings first. Note any that are unclear. If the right reading changes the outcome, stop and ask.
2. For each finding, open the code it names and check whether it is true. Reproduce it where you can, with a test or a command.
3. If it holds, fix it. Write a failing test first where a test fits, then the fix, then run that test.
4. If it does not hold, say why: name the code, the test or the spec line that shows it. Do not change the code to please the reviewer.
5. Report each finding with one line: fixed (with the test), or not a problem (with the reason).

## Rules

- Never agree without checking. "Good catch" and "you are right" are not an answer.
- Fix one finding at a time and run the test for it, so a fix that breaks something shows at once.
- Run only the tests you wrote or changed, and the ones next to the code. The runner's gates run the rest.
- Do not make changes the findings did not ask for. A finding that would need a change to the spec or the plan goes back as a question.
- When two findings conflict, or a finding conflicts with the spec, stop and ask. Do not choose silently.
- Never commit. The runner commits.
