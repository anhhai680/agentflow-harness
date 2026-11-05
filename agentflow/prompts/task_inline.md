Use the `test-driven-development` skill.

Goal: implement {task_title} from the plan, and nothing else.

The task text:

{task_text}

Read the repository's quality rules and the spec and plan the task names, by path. Write the failing test first and run it, then write the code, then run the task's tests.
Stay inside the files the task lists. Run the build gates before you finish. Do not commit: the runner commits.
If the task conflicts with the code or the spec, stop and say so instead of improvising.
If the runner appended a further skill for the paths this task touches (for example a UI skill), it is at the end of this prompt: follow it too, and check any screen against the design the task or the spec names.

## The `test-driven-development` skill

This is its full text, so you already have it: do not load it, follow it.

{brief}
