Use the `verification-before-completion` skill.

Goal: the eval gate of step `{step_id}` failed (its output is above). Find the cause and fix it, so the gate passes on its own merits and the claim rests on its output.

Read `CONSTRAINTS.md` (the eval gate row) and the "Eval gate" part of `CLAUDE.md` first. Run the eval gate yourself before you finish, and report the command and its result. Report retrieval and answer scores separately, each against `eval/baseline.json`.
Fix only the cause, in the files the change touched. Never edit `eval/baseline.json`, lower a limit, drop a case, skip the gate or add a suppression: a regression you cannot fix goes to the human.
Do not commit: the runner commits.

## The `verification-before-completion` skill

This is its full text, so you already have it: do not load it, follow it.

{brief}
