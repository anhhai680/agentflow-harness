Goal: prove the branch is done from the verify gates' output, not by assuming.

{earlier_findings}

{gate_evidence}

That output is fresh evidence for this exact tree, so do not run those gate commands again: read the logs and report each gate's command, result line and counts. Then run only what no gate covers: the plan's own verification commands that are not among the gates (name each one, with its result). If you change a file, the runner runs every gate again after you.
If something fails, say which gate and why, and fix only the cause. Never weaken the repository's quality rules, skip a test or add a suppression to pass.
No human reads this step before the run goes on. Put anything that still fails, or that you could not run, in the result's `findings` as `{"severity": "blocking", "file": "path", "line": 1, "text": "what and why"}`: a result that only mentions it in `summary` lets it through.
Do not commit.

## The `{skill}` skill

This is its full text, so you already have it: do not load it, follow it.

{brief}
