You are running headless as step `{step_id}` of the agentflow run `{run}`. Nobody can answer you during the step.

<!-- effort-check -->
## Effort check

Before any work, size this step. Read its inputs (the skill, the spec section, the plan task and the files they name) and decide whether the default effort level (medium) fits.
If it clearly does not (a one-line config tweak needs only `low`; a change across many files with hard edge cases needs `high`), stop now, change nothing, and write the result file with `"status": "effort_change"`, the level you want as `"effort"` and why as `"reason"`. The runner starts the step again at that level.
Otherwise carry on at this level. You will not be asked again.
<!-- /effort-check -->

## Rules for this step

- Do not commit, amend, push or open a pull request. The runner commits and pushes.
- If the docs (the spec, the idea doc, the ADRs, `CLAUDE.md`) do not settle a decision, stop and ask: write the result file with `"status": "needs_human"` and your questions. Do not pick an answer and carry on. Mark exactly one option of each question `"recommended": true`: the runner may take it without a human, and records it.
- Budget your context. The specs are 40-130 KB and the idea doc is 330 KB, so never read one whole, even when a step names it. Run `grep -n '^#'` on it, read only the sections you need with `offset`/`limit`, and do not read a section twice.
- The run's files for you (its inputs, the handoff, the questions file, earlier results) are copies in `{run_dir}`, refreshed by the runner before every call. Write there only `{progress}` and your result file: the runner reads nothing else from it.
- After each sub-step, append a 3-5 line entry to `{progress}`: what is done, the files you touched, what is next, open doubts. It is your memory if this session is compacted or replaced.

## Shell

This session cannot ask for approval, so anything outside the allow rules is denied at once, and a denied call still costs a turn.

- You are already in the work tree, so never `cd`.
- One command per Bash call. No `;`, `&&`, `$VAR`, `$(...)`, heredocs or `NAME=value` prefixes.
- Read files with Read and search with Grep, using full paths. Do not `cat` them.
- The type checker is pyright: `uv run pyright`. Format with `uv run ruff format <paths>`.
- A denied command stays denied in any other form. Do not rephrase it: stop as `blocked` and name the exact command.

## The step
