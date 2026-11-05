Use the `receiving-code-review` skill.

Goal: the `{phase}` phase found problems. Carry out the fix plan, with the smallest change that addresses each finding.

The findings:

{refine_note}

The plan is in `signals.fix_plan` of this phase's `plan_fixes` result, under `{run_dir}/results/`. Fix each entry with the verdict `fix`, with a test where it says so. Leave `decline` entries alone and give their reasons in your summary. If the code shows an entry is wrong, say why instead of changing the code.
The phase's checks run again after you, so do not review your own work. Run the verify gates before you finish.
Never weaken `CONSTRAINTS.md`, skip a test or add a suppression. Do not commit: the runner commits.

## The `receiving-code-review` skill

This is its full text, so you already have it: do not load it, follow it.

{brief}
