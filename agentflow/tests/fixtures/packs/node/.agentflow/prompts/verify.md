Use the `verification-before-completion` skill.

Goal: prove the branch is done from the verify gates' output, not by assuming.

{earlier_findings}

{gate_evidence}

That output is fresh evidence for this exact tree, so do not run those gate commands again: read the logs and report each gate's command, result line and counts. Then run only what no gate covers: the plan's own verification commands that are not among the gates (name each one, with its result). If you change a file, the runner runs every gate again after you.
If a gate failed, stop the line and debug it: reproduce the failure, find where it comes from, fix the cause and guard it with a test. Say whether the cause is in the branch or in the environment (a clock, a database, a port, a flaky test the branch does not touch). Never weaken the repository's quality rules, skip a test or add a suppression to pass.
Do not judge the code by reading it against the spec: the review phase does that. For each acceptance criterion in the spec and each verification command in the plan, name the test or command that ran and its result. A criterion nothing ran for is not verified: list it with the reason, never as met.
Put that account in the result's `signals` as `{"verification": {"gates": [{"gate": "name", "cmd": "command", "result": "result line", "passed": true}], "criteria": [{"criterion": "Success Criterion 3", "evidence": "the test or command", "ran": true, "passed": true}], "not_verified": [{"what": "Success Criterion 7", "why": "needs a browser"}]}}`.
No human reads this step before the run goes on. Put anything that still fails, or that you could not run, in the result's `findings` as `{"severity": "blocking", "file": "path", "line": 1, "text": "what and why"}`: a result that only mentions it in `summary` lets it through.
Do not commit.

## The `verification-before-completion` skill

This is its full text, so you already have it: do not load it, follow it.

{brief}
