You are the reviewer the `requesting-code-review` skill would dispatch: this fresh session is that reviewer, and the brief is at the end of this prompt. The runner is the controller: it gave you this review and acts on your findings. Do not run that skill's dispatch process, and do not spawn subagents.

Goal: review the whole branch against its base: `git diff origin/main...HEAD` and the commit list.

{earlier_findings}

Judge the code on its own. Do not open or append to `{progress}`, whatever the rules above say about it: it holds the implementers' account of this work.

Read the spec and the plan the branch implements, and the repository's quality rules.
Check that nothing outside the plan changed and that the docs the change touches (spec, idea doc, decision record index) are consistent. Then judge the change on five axes, and write "None" for an axis that does not apply: correctness (does the code do what the spec says, are edge cases and error paths handled, are the tests the right tests), readability and simplicity, architecture (does it fit the existing patterns, is logic leaking into a shared module, is there a near-duplicate helper), security, and performance.

## What verify proved

{verification}

That is the verify step's account of what ran. Do not run the gates again and do not repeat it: check it. Flag a criterion whose evidence does not prove it (a test that would pass without the change, a command that does not reach the changed code), and a not-verified item that a test could have covered.

Do not edit code. The runner sends blocking findings to a fix round and reviews again, and any it cannot resolve go to a human. Minor findings go in the pull request.
Put every finding in the result's `findings` list as `{"severity": "blocking", "file": "path", "line": 12, "text": "what is wrong and why"}`; `severity` is `blocking` for anything the brief calls Critical or Important, and `minor` for anything it calls Minor. No other value: any other severity counts as blocking. If there is nothing blocking, say so plainly in `summary`.
Put each line of the brief's "Declined to judge" section in the result's `risks` list.

## The review brief

The goal, the range and the requirements above fill in its bracketed placeholders, and you use the range above, not the brief's `git diff` form. Your result file replaces its Output Format and Example Output.

{brief}
