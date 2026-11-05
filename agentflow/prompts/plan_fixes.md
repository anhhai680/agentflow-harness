Use the `receiving-code-review` skill.

Goal: the `{phase}` phase found problems the runner must resolve. Make the plan that resolves them. Do not edit anything: `fix_findings` runs next and follows your plan, then the phase's checks run again.

This is round {refine_round}.

The findings:

{refine_note}

For each finding, read the code it names and decide whether it holds. Do not trust the finding, and do not trust an earlier round: check the code as it is now. Earlier rounds' plans are in `{run_dir}/results/`, if any.

Put the plan in the result's `signals`, as `{"fix_plan": [{"finding": "file:line, in a few words", "verdict": "fix", "why": "...", "change": "what to change and where", "files": ["path"], "test": "the test that fails without the change, or why none fits"}]}`. `verdict` is `fix`, or `decline` when the finding is wrong, with the evidence in `why`.

Fix the cause, in the smallest change that does it. A fix may never weaken `CONSTRAINTS.md`, skip a test or add a suppression. If a finding can only be resolved by a decision the docs do not settle, say so as a question with your recommended option.

## The `receiving-code-review` skill

This is its full text, so you already have it: do not load it, follow it.

{brief}
