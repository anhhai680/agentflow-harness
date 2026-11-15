Goal: step `{stop_key}` stopped, and no human is watching. Find out why, and fix the cause so that step can finish.

It stopped with `{stop_kind}`: {stop_summary}

What the runner recorded:

{stop_report}

Investigate before you change anything, in this work tree. The step's own session resumes after you, with your summary, and its gates and reviews still decide whether it is done, so make the tree right rather than arguing that it is.

- If a command was denied, it stays denied in every form. Find the allowed way to get the same result, such as a gate command or the runner's `setup` gates. Do not rephrase it.
- If the step needs something only a human can give, stop as `blocked` and name it exactly. That is a good result: a human then reads your diagnosis.
- Never weaken the repository's quality rules, edit the eval baseline, lower a threshold, skip a test or add a suppression.

Name the cause's class in `stop_class` when one fits: `environment` (a tool is missing or broken, a port is in use, Docker is down: nothing in this work tree can fix it), `flaky` (a gate fails and then passes when run again, or depends on the clock or on test order), `guard_false_positive`, `spec_gap`, `impact` (a public contract breaks, or a module's responsibility changes) or `repeated_failure`. `environment` is the one case where you finish as `done` without fixing the cause, and not as `blocked`: say exactly what is broken. The runner then checks the machine's tools, waits while those checks fail, and resumes the step once they pass. For `flaky`, change nothing in the tree. Leave `stop_class` out when none fits.

Say in your summary what the cause was and what you changed. If the cause is not specific to this step (an environment cause, a tool quirk), also list it in `facts`, one line each, so later sessions do not rediscover it. Do not commit: the runner commits.

## The `{skill}` skill

This is its full text, so you already have it: do not load it, follow it.

{brief}
