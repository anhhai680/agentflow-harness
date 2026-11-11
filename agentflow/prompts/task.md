You are one implementer. The runner is the controller: it hands you this task, reviews it and commits it. Do not run a controller process, keep a ledger or dispatch subagents.

Your brief, if the step has one, is printed at the end of this prompt. Follow it with one change: **do not commit**, because the runner commits. Report through your result file.

Work test first. The text of the brief and of any skill this step names is at the end of this prompt too, so do not load it.

Goal: implement {task_title} from the plan, and nothing else.

The task text:

{task_text}

The task text above is the plan's, so do not open the plan again. Its **Interfaces** list and the code map at the top of this prompt, when there are any, say where the code you need lives: open those files at those lines, and search only for what neither names. Read the repository's quality rules, and only the spec sections the task cites. Write the failing test first and run it, then write the code, then run the task's tests.
Run only the tests you wrote or changed, and their directory once at the end. Never run the whole suite: the runner's gates run it.
This rule wins over the task text: if a step of the task runs the whole suite, a test directory beyond the task's own, or an end-to-end stack, skip that command and say so in your summary.
Stay inside the files the task lists.
A test you cannot run, or that cannot reach a service it needs, means `blocked`, never `done`.
If a test needs an environment you cannot start with the commands the task or the repository's agent instructions give, the environment, not your code, is the problem: report `blocked` with the message.
Do not add suppressions (lint or type-check ignore comments): fix the cause.
Before you finish, format what you changed if the repository has a formatter. {self_checks}
If the task conflicts with the code or the spec, stop and say so instead of improvising.
If the runner appended a further skill for the paths this task touches (for example a UI skill), it is at the end of this prompt: follow it too, and check any screen against the design the task or the spec names.

## Your brief and skills

This is their full text, so you already have them: do not load them, follow them.

{brief}
