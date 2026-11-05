Use the `systematic-debugging` skill.

Goal: step `{stop_key}` stopped, and no human is watching. Find out why, and fix the cause so that step can finish.

It stopped with `{stop_kind}`: {stop_summary}

What the runner recorded:

{stop_report}

Investigate before you change anything, in this work tree. The step's own session resumes after you, with your summary, and its gates and reviews still decide whether it is done, so make the tree right rather than arguing that it is.

- If a command was denied, it stays denied in every form. Find the allowed way to get the same result, such as a gate command or the runner's `setup` gates. Do not rephrase it.
- If the step needs something only a human can give, stop as `blocked` and name it exactly. That is a good result: a human then reads your diagnosis.
- Never weaken `CONSTRAINTS.md`, edit `eval/baseline.json`, lower a threshold, skip a test or add a suppression.

Say in your summary what the cause was and what you changed. Do not commit: the runner commits.

## The `systematic-debugging` skill

This is its full text, so you already have it: do not load it, follow it.

{brief}
