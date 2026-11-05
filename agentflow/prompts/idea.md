Use the `grilling` skill.

Goal: turn the new module `{module}` into a short list of product questions for a human, each with your recommended option.

Read, by path, and only what you need:
- The `{module}` row and its dependencies in `{capability_map}`.
- The idea doc, if one is configured (`{idea}`): run `grep -n '^## '` on it, then read only the sections this module touches (MVP Scope, Decisions Log, Open Questions). Never read it whole.
- The decision records' index, then only the decision records the module touches.

Do not draft the spec. Ask only what the docs do not already settle, and list what you took as settled.
Put the questions in your result for the runner to render into `{questions}`, with `"status": "done"`: one question per entry, with the options and your recommended one. Asking is this step's job, so the questions are not a `needs_human` stop.
