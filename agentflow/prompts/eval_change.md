Goal: make the change to a prompt, model, chunking, embedding, retrieval or CRM-field setting that is described in `{run_dir}/task.md`. Read that file first.

Read `CONSTRAINTS.md` (the eval gate row) and the "Eval gate" part of `CLAUDE.md`. Follow the repo's lane for these paths.
No TDD for prompt text. Make the change, then run the eval suite with retrieval scored separately from answers, and report both scores against `eval/baseline.json`.
Never edit `eval/baseline.json` or lower a limit: a regression blocks the change and goes to the human.
Do not commit.
