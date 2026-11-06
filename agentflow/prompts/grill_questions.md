Goal: grill the design question "{topic}" into questions for a human, each with a recommended answer.

Read the idea doc, if one is configured (`{idea}`): `grep -n '^## '` first, then only the sections the topic touches (Open Questions, Decisions Log, Not Doing). Never read it whole. Read the decision records it touches.
Ask one question per decision: the options, your recommended option, and the cost of reversing it.
Put them in your result for the runner to render into `{questions}`, with `"status": "done"`: asking is this step's job, so the questions are not a `needs_human` stop. Do not edit any doc in this step.
