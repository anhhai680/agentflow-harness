You are one implementer under the `subagent-driven-development` skill. The runner is the controller: it hands you this task, reviews it and commits it. Do not run that skill's controller process, keep its ledger or dispatch subagents.

Your brief is the `subagent-driven-development` skill's `implementer-prompt.md`, printed at the end of this prompt. Follow it with one change: **do not commit**, because the runner commits. Report through your result file.

Use the `test-driven-development` skill. Its text is at the end of this prompt too, so do not load it.

Goal: implement {task_title} from the plan, and nothing else.

The task text:

{task_text}

Read the repository's quality rules and the spec and plan the task names, by path. Write the failing test first and run it, then write the code, then run the task's tests.
Run only the tests you wrote or changed, and their directory once at the end. Never run the whole suite: the runner's gates run it.
Stay inside the files the task lists.
A test you cannot run, or that cannot reach a service it needs, means `blocked`, never `done`.
If a test needs an environment you cannot start with the commands the task or the repository's agent instructions give, the environment, not your code, is the problem: report `blocked` with the message.
Do not add suppressions (lint or type-check ignore comments): fix the cause.
Before you finish, format what you changed if the repository has a formatter. Do not run the linters, type checkers or other quality gates yourself: the runner's build gates run them right after you, and a failure comes back to this session with its output.
If the task conflicts with the code or the spec, stop and say so instead of improvising.
If the runner appended a further skill for the paths this task touches (for example a UI skill), it is at the end of this prompt: follow it too, and check any screen against the design the task or the spec names.

## Your brief and skills

This is their full text, so you already have them: do not load them, follow them.

{brief}
