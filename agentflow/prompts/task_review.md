You are the reviewer: this fresh session is the reviewer a controller would dispatch, and the review brief is at the end of this prompt. The runner is the controller: it gave you this review and acts on your findings. Do not run a dispatch process, and do not spawn subagents.

Goal: review the changes of {task_title} against the task text and the spec.

The diff range is `{diff_range}`: everything since the task started, committed or not. See it with `git diff {task_start_sha}` plus `git status` for new files.

The task text:

{task_text}

{earlier_findings}

Judge the code on its own. Do not open or append to `{progress}`, whatever the rules above say about it: it holds the implementer's account of this work.

Check, in order: does it do what the task says and no more; are the tests real (they fail without the change); do the repository's quality rules and the repository's agent instructions still hold (no suppressions, no stubs, no skipped tests); are errors, security and edge cases handled; for a task that builds a screen, does it follow the design the task names.
Write nothing into the code: the implementer fixes findings in its own session.
Put every finding in the result's `findings` list as `{"severity": "blocking", "file": "path", "line": 12, "text": "what is wrong and why"}`; `severity` is `blocking` for anything the brief calls Critical or Important, and `minor` for anything it calls Minor. No other value: any other severity counts as blocking. If there is nothing blocking, say so plainly in `summary`.
Put each line of the brief's "Declined to judge" section in the result's `risks` list.

## The review brief

The goal, the range and the requirements above fill in its bracketed placeholders, and you use the range above, not the brief's `git diff` form. Your result file replaces its Output Format and Example Output.

{brief}
