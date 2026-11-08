Next task in this same session: you are still the implementer under the `subagent-driven-development` skill, and every rule from the first task's prompt still holds (do not commit, use the `test-driven-development` skill, stay inside the files the task lists, no suppressions, run only the test files you wrote or changed, report through your result file). The runner committed your previous task after its gates passed. It reviews this batch once, after its last task.

Goal: implement {task_title} from the plan, and nothing else.

The task text:

{task_text}

The task text above is the plan's, so do not open the plan again. Use its **Interfaces** list and the code map, and read only the spec sections it cites that you have not read yet. Write the failing test first and run it, then write the code, then run the task's tests.
Before you finish, format what you changed if the repository has a formatter. {self_checks}
If the task conflicts with the code or the spec, stop and say so instead of improvising.
