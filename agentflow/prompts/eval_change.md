Goal: make the change described in `{run_dir}/task.md`. Read that file first.

Read the repository's quality rules (the eval gate rule) and the eval gate part of the repository's agent instructions. Follow the repository's rules for these paths.
No TDD for prompt text. Make the change, then run the eval gate and report its scores against the baseline.
Never edit the eval baseline or lower a limit: a regression blocks the change and goes to the human.
Do not commit.
