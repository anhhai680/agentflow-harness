Goal: turn the open items in the spec of module `{module}` (`{spec}`) into questions for a human, each with a recommended answer.

Read the spec's "Open Questions" section (ignore the "Resolved" and deferred lists), then only what each question needs: the rows it names in other specs (`grep -n` for them, do not read those specs), the matching idea-doc section (`grep -n '^## '` first, never read the idea doc whole) and the decision records it touches.
Ask one question per open item, with options and your recommended option and why.
Put them in your result for the runner to render into `{questions}`, with `"status": "done"`: asking is this step's job, so the questions are not a `needs_human` stop. Do not edit the spec in this step.
