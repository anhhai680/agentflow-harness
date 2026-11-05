Goal: make the change to a prompt, model, chunking, embedding, retrieval or CRM-field setting that is described in `{run_dir}/task.md`. Read that file first.

Read the repository's quality rules (the eval gate rule) and the eval gate part of the repository's agent instructions. Follow the repository's rules for these paths.
No TDD for prompt text. Make the change, then run the eval suite with retrieval scored separately from answers, and report both scores against the eval baseline.
Never edit the eval baseline or lower a limit: a regression blocks the change and goes to the human.
Do not commit.
