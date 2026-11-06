Next task in this same session: you are still the implementer under the `subagent-driven-development` skill, and every rule from the first task's prompt still holds (do not commit, use the `test-driven-development` skill, stay inside the files the task lists, no suppressions, run only the test files you wrote or changed, report through your result file). The runner committed your previous task after its gates passed. It reviews this batch once, after its last task.

Goal: implement {task_title} from the plan, and nothing else.

The task text:

{task_text}

Read the spec and plan sections this task names, by path, if you have not yet. Write the failing test first and run it, then write the code, then run the task's tests.
Before you finish, format what you changed if the repository has a formatter. Do not run the linters, type checkers or other quality gates yourself: the runner's build gates run them right after you, and a failure comes back to this session with its output.
If the task conflicts with the code or the spec, stop and say so instead of improvising.
