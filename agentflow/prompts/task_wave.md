You run a wave: {wave_title} of the plan, built at the same time by one sub-agent each. The runner is the controller: it chose these tasks, checked that they share no file and that none depends on another, and it gates, reviews and commits each of them itself.

**Do not edit any file yourself**, do not commit, and do not run the tests or the gates. The build skill named above is your sub-agents' brief, not yours: each card carries it, and the work it asks for, the test first among it, is theirs. Your only work is to dispatch the sub-agents and report what they did:

1. In **one message**, call the `Task` tool once for each task card below, all in that one message, so the sub-agents run at the same time. For each card:
   - `description`: `Task <n>: <title>`, exactly as the card's line below gives it;
   - `prompt`: "Read `<card path>` and follow it.", with the card's path from its line below;
   - leave `model` out, and `subagent_type` too: every sub-agent runs on your model.
2. Dispatch nothing else: no task that has no card here, no second sub-agent for a card, no reviewer.
3. Wait until every sub-agent has finished. Each one ends with a short report: `n`, `status` (`done`, `blocked` or `needs_human`), `summary`, `files` (the files it changed) and `tests_run`.
4. Then write your result file with `status: done` and one `tasks` entry per card, copied from its sub-agent's report:

```json
{"status": "done", "summary": "One line on the wave.", "tasks": [{"n": 2, "status": "done", "summary": "One line on the task.", "files": ["lib/a.py"], "tests_run": ["tests/test_a.py"]}]}
```

A task whose sub-agent reports `blocked` or `needs_human` keeps that status in its entry, and your own `status` is still `done`: the runner sets that task aside and builds it alone. A sub-agent that ended with no report, or a report you cannot read, gets no entry: never write one for it.

The task cards:

{wave_cards}
