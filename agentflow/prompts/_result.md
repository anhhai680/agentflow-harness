## When you finish

Write your result as JSON to `{result_file}`, then stop. Use this exact path. The runner reads only that file, not your final message, and the step does not count as done without a valid one.

```json
{"status": "done", "summary": "One to three lines on what you did."}
```

- `status`: `done` (the step is finished), `blocked` (you cannot go on; say why in `summary`), `needs_human` (a decision the docs do not settle; add `questions`), `effort_change` (only from the effort check, with `effort` and `reason`), or `unchanged` (only in a refine round, when the note needs no change; give the reason in `summary`).
- `summary`: what you did, for the human and the commit message. Never put git trailers in it.
- `questions` (with `needs_human`, or with `done` in a step whose job is to ask them): a list of `{"id": "q1", "question": "...", "options": [{"key": "A", "label": "...", "recommended": true}], "evidence": ["path#section"], "kind": "spec_gap"}`; `kind` is one of `spec_gap`, `ambiguity`, `conflict`, `review_dispute`, `boundary` (the docs reserve the decision for a human), `other`.
- `can_continue_without`: `true` when later independent tasks can go on while your question waits.
- Optional: `signals` (an object), `findings` and `risks` (lists).
- `facts`: only in a resolver session: up to 5 one-line statements that hold for the whole run (an environment cause, a tool quirk), so later sessions do not rediscover them.
- `lessons_cited`: optional. The ids of the lessons (`L-0007`, `E-0003`) that changed what you did in this step, at most 10. List only a lesson that the prompt carried and that you used.
- `code_map`: optional, in any step that read code. Up to 8 one-line entries for existing code you had to search for and that a later task will likely need, each as `path:line` `signature`: what it is for. Leave out what the plan's Interfaces list already names. Later sessions of this run see these at the top of their prompt.
