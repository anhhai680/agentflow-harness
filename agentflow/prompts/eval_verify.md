Goal: the eval gate of step `{step_id}` failed (its output is above). Find the cause and fix it, so the gate passes on its own merits and the claim rests on its output.

Read the repository's quality rules (the eval gate rule) and the eval gate part of the repository's agent instructions first. Run the eval gate yourself before you finish, and report the command and its result. Report the gate's scores against the baseline.
Fix only the cause, in the files the change touched. Never edit the eval baseline, lower a limit, drop a case, skip the gate or add a suppression: a regression you cannot fix goes to the human.
Do not commit: the runner commits.

## The `{skill}` skill

This is its full text, so you already have it: do not load it, follow it.

{brief}
