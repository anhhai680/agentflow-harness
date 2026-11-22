## You are one sub-agent of a wave

A wave session dispatched you, beside other sub-agents that build their own plan tasks in the same working tree at the same time. These rules win over anything above them in this card:

- Edit only these files, and format only them (never run a formatter on the whole repository):

{allowed_files}

- Run only your own task's unit tests: no integration suite, no end-to-end stack, no Docker, no database. The runner's gates run the rest once every task of the wave is in.
- Never commit, and never stage, stash, reset or check out anything: the runner commits each task on its own.
- Write no result file, whatever the card above says about one: the wave session reports for you.
- End with your report as your final message, this JSON and nothing after it:

```json
{"n": {task_n}, "status": "done", "summary": "One line on what you did.", "files": ["the files you changed"], "tests_run": ["the tests you ran"]}
```

`status` is `done`, `blocked` (you cannot go on; say why in `summary`) or `needs_human` (a decision the docs do not settle; say which in `summary`).
