Goal: fix the bug so the regression test passes, with the smallest change that addresses the root cause.

Read the debug step's result in `{run_dir}/results/` and the regression test (its command is in the earlier result's `signals.test_command`).
Run the test and see it go from red to green, then run the build gates. Do not touch the test to make it pass.
Never weaken `CONSTRAINTS.md`, skip a test or add a suppression. Do not commit: the runner commits.

Use the `test-driven-development` skill.

## The `test-driven-development` skill

This is its full text, so you already have it: do not load it, follow it.

{brief}
